#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 Verilog 工程的真实状态导出成 board.json —— 供工作台读取。

本脚本**对任意 Verilog 工程通用**：它不写死任何一个工程，而是读工程目录里的
project.json（字段定义见 PROJECT-CONFIG.md）。

每次运行都会真实执行：
  1. iverilog + vvp          仿真，抓 PASS/FAIL
  2. verilator --lint-only   检查警告数
  3. yosys 综合              抓资源占用
  4. 解析波形 .vcd           波形时长、信号、时钟沿
  5. 扫描产物文件

两种输出：
  默认      逐步骤打印可读日志（供流式推送到控制台）
  --json    把日志按行收集成 JSON 一起输出

用法:
  python refresh.py [工程目录] [--json]

工程目录的确定顺序（先找到先用）：
  1. 命令行第一个参数
  2. 环境变量 DSH_VERILOG_PROJECT
  3. APP_DIR/project.json 里的 "projectDir" 字段
  4. APP_DIR 的同级目录 vibe-verilog-lab（沿用习惯布局）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_config import load_config, validate  # noqa: E402  同一个契约

APP_DIR = Path(__file__).resolve().parent
OUT = APP_DIR / "board.json"
APP_POINTER = APP_DIR / "project.json"          # 可选的「默认工程指路牌」
FALLBACK_PROJ = APP_DIR.parent / "vibe-verilog-lab"

COLLECT: list[str] | None = [] if "--json" in sys.argv else None


def say(line: str = "") -> None:
    """统一输出口：普通模式直接打印，--json 模式收集。"""
    if COLLECT is not None:
        COLLECT.append(line)
    else:
        print(line, flush=True)


def to_wsl(p: Path) -> str:
    s = p.resolve().as_posix()
    if len(s) > 1 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def shq(s: str) -> str:
    """给 shell 单引号转义，避免文件名里的引号把命令拆坏。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 工程定位
def resolve_project_dir(argv: list[str]) -> tuple[Path | None, str]:
    """按优先级找出要探测的工程目录。返回 (目录, 来源说明)。"""
    positional = [a for a in argv if not a.startswith("-")]
    if positional:
        return Path(positional[0]).expanduser().resolve(), "命令行参数"

    env = os.environ.get("DSH_VERILOG_PROJECT")
    if env:
        return Path(env).expanduser().resolve(), "环境变量 DSH_VERILOG_PROJECT"

    if APP_POINTER.is_file():
        try:
            pointer = json.loads(APP_POINTER.read_text(encoding="utf-8-sig"))
            rel = pointer.get("projectDir")
            if isinstance(rel, str) and rel.strip():
                base = APP_DIR if Path(rel).is_absolute() else APP_DIR
                return (base / rel).resolve() if not Path(rel).is_absolute() \
                    else Path(rel).resolve(), "desk-app/project.json 的 projectDir"
        except Exception:  # noqa: BLE001
            pass

    return FALLBACK_PROJ, "默认同级目录"


# ---------------------------------------------------------------- 多轮视图（第 5 步）
def runs_dir_for(project_dir: Path) -> Path:
    """第 3 步把每轮证据放在哪：<工程目录的上一级>/runs/<工程名>/。"""
    p = Path(project_dir).resolve()
    return p.parent / "runs" / p.name


def _quench_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "quench"


def _unified_diff(a: str, b: str, an: str, bn: str) -> str:
    """两轮源码的 diff。优先用 quench 的实现（带换行兜底），没有就自己算。"""
    try:
        sys.path.insert(0, str(_quench_dir()))
        from quench_runs import unified_diff as qdiff  # type: ignore
        return qdiff(a, b, an, bn)
    except Exception:  # noqa: BLE001
        import difflib

        def nl(s: str) -> str:
            s = s or ""
            return s if (not s or s.endswith("\n")) else s + "\n"

        return "".join(difflib.unified_diff(
            nl(a).splitlines(keepends=True), nl(b).splitlines(keepends=True),
            fromfile=an, tofile=bn, n=2))


def _classify_round_text(text: str) -> tuple[str, str]:
    """给一轮的失败归类（第 4 步的 8 类）。拿不到 quench 就退回粗糙判断。"""
    try:
        sys.path.insert(0, str(_quench_dir()))
        from quench_classify import CATEGORIES_ALL, classify  # type: ignore
        cat, _why, _ev = classify(text)
        return cat, CATEGORIES_ALL.get(cat, cat)
    except Exception:  # noqa: BLE001
        t = (text or "").upper()
        if "TEST FAILED" in t or "FAIL:" in t:
            return "functional", "功能不符"
        if "ERROR" in t or "SYNTAX" in t:
            return "syntax", "语法错误"
        return "unknown", "未分类"


def classify_round(meta: dict, log: str = "") -> tuple[str, str]:
    """给一轮定类别。

    ⚠️ 优先级很关键，实测踩到过：
    `failure_kind` 和 `headline` 是编排层**已经提炼好的语义信息**，
    而 `log.txt` 存的是模型回复的**原始文本**（含 JSON 源码，里面什么字样都有）。
    一开始把日志一起丢进去分类，结果第 4 轮明明是"测试被削弱"，
    却因为原始 JSON 里出现了 "error" 字样被判成"语法错误"。
    所以：**先只用 kind + headline 判；判不出来才拿日志兜底。**
    """
    kind = str((meta or {}).get("failure_kind", ""))
    headline = str((meta or {}).get("headline", ""))

    # 编排层的固定说法直接映射，不用正则猜
    fixed = {
        "削弱测试": ("testbench", "削弱测试平台"),
        "偷改测试平台": ("testbench", "削弱测试平台"),
        "测试没有验证力": ("testbench", "测试无验证力"),
        "编译失败": ("syntax", "编译失败"),
        "探测未通过": ("functional", "功能不符"),
        "写入失败": ("unknown", "写入失败"),
        "API 调用失败": ("unknown", "API 失败"),
    }
    if kind in fixed:
        return fixed[kind]

    cat, label = _classify_round_text(f"{kind}\n{headline}")
    if cat != "unknown":
        return cat, label
    # 兜底才看日志
    return _classify_round_text((log or "")[:1500])


def collect_rounds(project_dir: Path, limit_rounds: int = 30,
                   diff_max_chars: int = 6000) -> dict:
    """读第 3 步的轮次存档，整理成界面能直接用的结构。

    **工程目录被删掉也要能读** —— 存档才是持久数据（第 4 步踩过这个坑：
    一开始要求工程目录存在，结果清理产物后报告就全空了）。
    所以这里只依赖 runs 目录。
    """
    root = runs_dir_for(project_dir)
    out: dict = {"available": False, "dir": str(root), "rounds": [],
                 "summary": None, "count": 0, "fixedAtRound": None}
    if not root.is_dir():
        return out

    summary = None
    sf = root / "summary.json"
    if sf.is_file():
        try:
            summary = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            summary = None

    nums = sorted(
        int(c.name.split("-", 1)[1])
        for c in root.iterdir()
        if c.is_dir() and c.name.startswith("round-")
        and c.name.split("-", 1)[1].isdigit()
    )
    if not nums:
        return out

    prev_dut = ""
    for n in nums[-limit_rounds:]:
        d = root / f"round-{n}"

        def rd(name: str, _d=d) -> str:
            f = _d / name
            try:
                return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
            except Exception:  # noqa: BLE001
                return ""

        meta = {}
        for mf in ("meta.json", "result.json"):
            f = d / mf
            if f.is_file():
                try:
                    meta = json.loads(f.read_text(encoding="utf-8"))
                    break
                except Exception:  # noqa: BLE001
                    pass

        dut_src, tb_src, log = rd("dut.v"), rd("tb.v"), rd("log.txt")
        cat, cat_label = classify_round(meta, log)

        diff = ""
        if prev_dut and dut_src and dut_src != prev_dut:
            diff = _unified_diff(prev_dut, dut_src,
                                 f"第 {n-1} 轮", f"第 {n} 轮")[:diff_max_chars]
        prev_dut = dut_src or prev_dut

        out["rounds"].append({
            "round": n,
            "outcome": meta.get("outcome", "?"),
            "failureKind": meta.get("failure_kind", ""),
            "headline": str(meta.get("headline", ""))[:200],
            "category": cat,
            "categoryLabel": cat_label,
            "tokens": meta.get("tokens", 0),
            "seconds": round(float(meta.get("seconds", 0) or 0), 2),
            "tbChanged": bool(meta.get("tb_changed")),
            "fingerprint": meta.get("fingerprint", ""),
            "diff": diff,
            "logTail": "\n".join(log.splitlines()[-40:]),
            "dutLines": len(dut_src.splitlines()),
            "tbLines": len(tb_src.splitlines()),
        })

    out["available"] = True
    out["count"] = len(nums)
    out["fixedAtRound"] = next((r["round"] for r in out["rounds"]
                                if r["outcome"] == "ok"), None)
    if summary:
        out["summary"] = {
            "ok": bool(summary.get("ok")),
            "stopReason": str(summary.get("stopReason", ""))[:200],
            "roundsUsed": summary.get("roundsUsed", len(nums)),
            "totalTokens": summary.get("totalTokens", 0),
            "totalSeconds": round(float(summary.get("totalSeconds", 0) or 0), 1),
            "tbHash": summary.get("tbHash", ""),
        }
    return out


def collect_projects(project_dir: Path, scan_root: Path | None = None,
                     limit: int = 12) -> list[dict]:
    """扫出「跑过并被记录」的同级工程，给界面做项目切换。

    判据和第 4 步一致：**只要有 runs 存档就算**，工程目录在不在都认。
    """
    p = Path(project_dir).resolve()
    base = Path(scan_root).resolve() if scan_root else p.parent
    runs_root = base / "runs"
    if not runs_root.is_dir():
        return []

    out = []
    for child in sorted(runs_root.iterdir()):
        if not child.is_dir():
            continue
        has = (child / "summary.json").is_file() or any(
            c.name.startswith("round-") for c in child.iterdir())
        if not has:
            continue
        proj = base / child.name
        summ = {}
        sf = child / "summary.json"
        if sf.is_file():
            try:
                summ = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                summ = {}
        nums = [c for c in child.iterdir()
                if c.is_dir() and c.name.startswith("round-")]
        out.append({
            "name": child.name,
            "path": str(proj),
            "exists": proj.is_dir(),
            "current": str(proj) == str(p),
            "ok": bool(summ.get("ok")),
            "rounds": summ.get("roundsUsed", len(nums)),
            "stopReason": str(summ.get("stopReason", ""))[:120],
            "tokens": summ.get("totalTokens", 0),
        })
    out.sort(key=lambda x: (not x["current"], x["ok"], -x["rounds"], x["name"]))
    return out[:limit]


# ---------------------------------------------------------------- WSL 执行
def wsl(cmd: str, distro: str, timeout: int):
    """在 WSL 里跑命令，返回 (ok, 输出)。"""
    try:
        p = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "bash", "-lc", cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"命令超时（{timeout} 秒）：{cmd}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 执行后端
# 这一层是为「别人也能用」加的：原来只会走 wsl.exe，于是使用者必须先装
# WSL2 + Ubuntu + iverilog + verilator + yosys（几个 GB），门槛太高。
# 而 oss-cad-suite 有 Windows 原生便携包（解压即用，里面全都有），
# 所以把「在哪跑命令」抽象出来：
#
#   wsl     —— 通过 wsl.exe 在 Linux 里跑
#   windows —— 直接调 Windows 上的 exe（对使用者最省事）
#
# 四个步骤只调 run_tool()，不再关心后端。
BACKEND = "auto"            # auto | wsl | windows
_TOOLCHAIN_DIR = None       # oss-cad-suite 的 bin 目录（可选）
_CURRENT_DISTRO = "Ubuntu-24.04"


def set_backend(name: str, toolchain_dir=None, distro: str | None = None) -> None:
    """设置执行后端。toolchain_dir 指 oss-cad-suite 解压出来的 bin 目录。"""
    global BACKEND, _TOOLCHAIN_DIR, _CURRENT_DISTRO
    BACKEND = (name or "auto").strip().lower()
    if toolchain_dir:
        _TOOLCHAIN_DIR = Path(toolchain_dir).expanduser().resolve()
    if distro:
        _CURRENT_DISTRO = distro


def find_windows_tool(name: str):
    """在 Windows 上找工具：先看显式指定的工具目录，再看 PATH。

    显式目录优先 —— oss-cad-suite 是「解压到哪就在哪」，
    使用者不一定把它的 bin 加进 PATH。
    """
    exe = name + (".exe" if os.name == "nt" else "")
    if _TOOLCHAIN_DIR:
        cand = _TOOLCHAIN_DIR / exe
        if cand.is_file():
            return str(cand)
    import shutil as _sh
    return _sh.which(name)


def detect_backend() -> str:
    """auto 模式下决定用哪个后端：优先 Windows 原生，找不到工具再退回 WSL。"""
    if BACKEND != "auto":
        return BACKEND
    if os.name != "nt":
        return "windows"           # 本来就在 Linux 上，直接本地跑
    for tool in ("iverilog", "verilator", "yosys"):
        if find_windows_tool(tool):
            return "windows"
    return "wsl"


def to_native(p) -> str:
    """把路径转成当前后端能用的写法。"""
    if detect_backend() == "wsl":
        return to_wsl(Path(p))
    return str(Path(p))


def shq_native(s) -> str:
    """按当前后端的规则加引号。"""
    if detect_backend() == "wsl":
        return shq(str(s))
    return '"' + str(s).replace('"', '\\"') + '"'


def run_tool(cmd: str, timeout: int, cwd=None):
    """按当前后端跑一条命令，返回 (ok, 输出)。

    cmd 里已经用 shq_native 转义过参数；cwd 是要在其中执行的目录。
    """
    be = detect_backend()
    if be == "wsl":
        full = (f"cd {shq(to_wsl(Path(cwd)))} && " if cwd else "") + cmd
        return wsl(full, _CURRENT_DISTRO, timeout)
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"命令超时（{timeout} 秒）：{cmd}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def tool_missing_hint(name: str) -> str:
    """工具找不到时给一段能直接照做的提示。"""
    if detect_backend() == "wsl":
        pkg = {"iverilog": "iverilog", "verilator": "verilator",
               "yosys": "yosys"}.get(name, name)
        return (f"WSL 里没有 {name}。装上它：\n"
                f"    wsl -d {_CURRENT_DISTRO} -- sudo apt install -y {pkg}")
    return (f"Windows 上找不到 {name}。两种办法：\n"
            f"    1) 下载 oss-cad-suite 的 Windows 便携包（约 572 MB，解压即用）：\n"
            f"       https://github.com/YosysHQ/oss-cad-suite-build/releases\n"
            f"       解压后把它的 bin 目录传给 --toolchain-dir，例如：\n"
            f"       python refresh.py --backend windows "
            f"--toolchain-dir D:\\oss-cad-suite\\bin\n"
            f"    2) 或者装好后把它加进 PATH\n"
            f"    （已有 WSL 环境的话也可以加 --backend wsl）")


def last_stats_block(out: str) -> str:
    """yosys 会把统计块打印两遍，只取最后一段，否则数字会翻倍。"""
    norm = out.replace("\r\n", "\n")
    starts = [m.start() for m in re.finditer(r"(?m)^=== (\S+) ===\s*$", norm)]
    if not starts:
        return norm
    body = norm[starts[-1]:]
    nxt = re.search(r"(?m)^=== \S+ ===\s*$", body[3:])
    return body[: nxt.start() + 3] if nxt else body


# ---------------------------------------------------------------- 结果判定
# 重要：wsl.exe 不转发 Linux 命令的退出码（verilator 报错时它照样返回 0），
# 所以判断成败必须看输出文本，不能信 returncode。
_ANY_ERROR = re.compile(r"(?m)^%Error")
# verilator 用 -Wall 时有警告也会打印 "%Error: Exiting due to N warning(s)"，
# 那是它把警告升级成错误的收尾语，不是真的写错了 —— 这一行要排除掉。
# （不要用负向前瞻写成一个正则：\s* 会回溯到零宽，前瞻就失效了。）
_UPGRADED = re.compile(r"^%Error:\s*Exiting due to\b")
_LAUNCH_FAIL = re.compile(
    r"command not found|No such file or directory|"
    r"WSL_E_DISTRO|Cannot|Permission denied", re.I)


def real_errors(out: str) -> list[str]:
    """挑出真正的错误行，排除 verilator 的「因 N 个警告退出」收尾语。"""
    hits = []
    for line in out.replace("\r\n", "\n").splitlines():
        if _ANY_ERROR.match(line) and not _UPGRADED.match(line):
            hits.append(line.strip())
    return hits


def classify(out: str, ok: bool):
    """看输出文本判断这条命令到底成没成。返回 (state, 说明)。

    state: ok（干净） / warn（有警告但跑通了） / fail（真出错）
    """
    if not out.strip():
        return ("ok" if ok else "fail"), ("" if ok else "命令没有输出，可能工具链没装好")
    if _LAUNCH_FAIL.search(out) and not _ANY_ERROR.search(out):
        return "fail", "命令没能启动 —— 检查 WSL 发行版和工具链是否装好"
    if real_errors(out):
        return "fail", "有真正的错误"
    if "%Warning" in out:
        return "warn", "有警告，但工具跑完了"
    return ("ok" if ok else "warn"), ("" if ok else "退出码非 0 但输出里没有明确错误")


def pick_vcd(root_win: Path, declared: str) -> tuple[Path | None, str]:
    """找波形文件。配置里写的最优先；找不到时自动认领唯一的 .vcd。"""
    target = root_win / declared
    if target.is_file():
        return target, ""
    found = sorted(root_win.glob("*.vcd"))
    if len(found) == 1:
        return found[0], f"配置里的 {declared} 不存在，自动改用目录里唯一的 {found[0].name}"
    if len(found) > 1:
        names = "、".join(f.name for f in found)
        return None, f"配置里的 {declared} 不存在，而目录里有多个 .vcd（{names}）——请把 project.json 的 vcd 改成正确的那个"
    return None, ""


# ---------------------------------------------------------------- 五个步骤
# 每个步骤都接收 cfg（配置字典），不再依赖任何写死的工程细节。
def step_sim(root_win: Path, cfg: dict):
    root = to_wsl(root_win)
    cmd_shown = f"iverilog -g2012 -Wall -o sim.vvp {cfg['dut']} {cfg['tb']} && vvp sim.vvp"
    say(f"  $ {cmd_shown}")
    ok, out = run_tool(
        f"iverilog -g2012 -Wall -o sim.vvp "
        f"{shq_native(cfg['dut'])} {shq_native(cfg['tb'])} && vvp sim.vvp",
        cfg["timeout"], cwd=root_win,
    )

    passed = "TEST PASSED" in out
    failed = "TEST FAILED" in out
    state, why = classify(out, ok)
    # 有明确的 TEST 结论时以结论为准，否则以 classify 的判断为准
    if passed:
        state = "ok"
    elif failed:
        state = "fail"
    elif state == "ok":
        # 跑是跑完了，但没有 TEST PASSED/FAILED —— 测试平台没按约定打印
        state = "idle"
        why = "仿真跑完了，但输出里没有 TEST PASSED / TEST FAILED"

    # 检查点：默认抓 `名字 = 数字` 这种打印（testbench 里很常见），
    # 格式不同的话改 project.json 的 assertPattern。
    nums = [int(x) for x in re.findall(cfg["assertPattern"], out)]
    samples = len(nums)
    if samples == 0:
        samples = len(re.findall(r"(?i)\b(pass|ok)\b", out))

    for line in out.strip().splitlines():
        if line.strip():
            say("    " + line.strip()[:110])
    if state == "ok":
        say(f"    -> 通过：{samples} 个检查点")
    elif state == "fail":
        say(f"    -> {'未通过' if failed else '出错'}"
            + (f"：{samples} 个检查点" if failed else f"：{why}"))
    else:
        say(f"    -> 未完成：{why}")

    if state == "ok":
        headline = "TEST PASSED"
    elif state == "fail":
        headline = "TEST FAILED" if failed else "仿真出错"
    else:
        headline = "未完成"

    return {
        "state": state,
        "headline": headline,
        "metrics": [("断言采样点", samples)],
        "q": nums,
        "log": "\n".join(out.strip().splitlines()[-6:]),
        "cmd": cmd_shown,
    }


def step_lint(root_win: Path, cfg: dict):
    root = to_wsl(root_win)
    cmd_shown = f"verilator --lint-only -Wall {cfg['dut']}"
    say(f"  $ {cmd_shown}")
    ok, out = run_tool(f"verilator --lint-only -Wall {shq_native(cfg['dut'])}",
                       cfg["timeout"], cwd=root_win)
    warn = len(re.findall(r"%Warning", out))
    errs = real_errors(out)
    real_err = len(errs)
    state, why = classify(out, ok)
    if real_err:
        state = "fail"
    elif state == "warn":
        # 政策决定：**风格警告不算探测失败。**
        # 理由：verilator -Wall 会为很多完全正确的写法报警（比如
        # `shamt >= 8` 这种 4 位与 32 位常量比较，报 WIDTHEXPAND），
        # 就像 gcc -Wall 的警告不该让构建失败一样。
        # 一开始我把它当失败，结果一个正确的桶形移位器白跑了一轮 API。
        # 现在：有警告照常记录在指标和日志里，但 state 归为 ok，不拦住流程。
        state = "ok"
        why = f"{warn} 条风格警告（不算失败，详见日志）"

    for line in [l for l in out.strip().splitlines() if l.strip()][:6]:
        say("    " + line.strip()[:110])
    if real_err:
        say(f"    -> 出错：{why or 'verilator 报告了错误'}"
            + (f"（另有 {warn} 条警告）" if warn else ""))
    elif warn:
        say(f"    -> {warn} 条风格警告（不影响正确性，不拦住流程）")
    else:
        say("    -> 无警告通过")

    # headline 用"通过/警告/出错"而不是"无警告"，避免有警告时显示成失败
    if real_err:
        headline = f"出错（{real_err} 处）"
    elif warn:
        headline = f"通过（{warn} 条风格警告）"
    else:
        headline = "无警告"

    return {
        "state": state,
        "headline": headline,
        "metrics": [("警告数", warn), ("错误数", real_err)],
        "log": "\n".join([l for l in out.strip().splitlines() if l.strip()][-6:]) or "(无输出)",
        "cmd": cmd_shown,
    }


def step_synth(root_win: Path, cfg: dict):
    root = to_wsl(root_win)
    yosys_script = f"read_verilog {cfg['dut']}; synth -top {cfg['top']}; stat"
    cmd_shown = f"yosys -p '{yosys_script}'"
    say(f"  $ {cmd_shown}")
    ok, out = run_tool(f"yosys -p {shq_native(yosys_script)}",
                       cfg["timeout"], cwd=root_win)
    blk = last_stats_block(out)
    cells = re.search(r"Number of cells:\s+(\d+)", blk)
    wires = re.search(r"Number of wires:\s+(\d+)", blk)
    ffs = sum(int(m) for m in re.findall(r"(?m)^\s+\$_SDFF\w*\s+(\d+)\s*$", blk))
    kinds = len(re.findall(r"(?m)^\s+\$_\w+\s+\d+\s*$", blk))
    state, why = classify(out, ok)
    if not cells and state == "ok":
        state, why = "fail", "yosys 没吐出统计块 —— 综合可能没真正完成"

    for line in [l for l in blk.strip().splitlines() if l.strip()][:8]:
        say("    " + line.strip()[:110])
    if state == "fail":
        say(f"    -> 综合出错：{why or 'yosys 报告了错误'}")
    else:
        say(f"    -> 综合单元 {cells.group(1) if cells else '?'} 个（其中触发器 {ffs} 个）")

    return {
        "state": "fail" if state == "fail" else ("ok" if cells else "fail"),
        "headline": (f"{cells.group(1)} 个单元" if cells and state != "fail" else "综合出错"),
        "metrics": [
            ("综合单元", int(cells.group(1)) if cells else 0),
            ("触发器", ffs),
            ("单元种类", kinds),
            ("连线", int(wires.group(1)) if wires else 0),
        ],
        "log": "\n".join([l for l in blk.strip().splitlines() if l.strip()][:6]) or (why or ""),
        "cmd": cmd_shown,
    }


def step_wave(root_win: Path, cfg: dict):
    declared = cfg["vcd"]
    vcd, note = pick_vcd(root_win, declared)
    say(f"  $ 解析 {declared}")
    if note:
        say(f"    ! {note}")
    if vcd is None:
        say("    -> 没有波形文件")
        return {"state": "idle", "headline": "无波形", "metrics": [("时长", "0 ns")],
                "log": note or f"尚未生成 {declared}", "cmd": f"gtkwave {declared}"}

    text = vcd.read_text(encoding="utf-8", errors="replace")
    times = [int(m) for m in re.findall(r"(?m)^#(\d+)", text)]
    end_ps = max(times) if times else 0
    names = set()
    for m in re.finditer(r"(?m)^\$var\s+\S+\s+\d+\s+\S+\s+(\S+)", text):
        names.add(m.group(1))
    edges = len(re.findall(r'(?m)^1"', text))
    say(f"    -> 时长 {end_ps // 1000} ns · 信号 {len(names)} 个 · 时钟上升沿 {edges} 次")

    return {
        "state": "ok",
        "headline": f"{end_ps // 1000} ns",
        "metrics": [("仿真时长", f"{end_ps // 1000} ns"), ("信号数", len(names)), ("时钟上升沿", edges)],
        "log": f"{vcd.name} · {vcd.stat().st_size} bytes" + (f"  ← {note}" if note else ""),
        "cmd": f"gtkwave {vcd.name}",
    }


def step_files(root_win: Path, cfg: dict):
    say("  $ 扫描产物目录")
    rows = []
    for f in sorted(root_win.iterdir()):
        if f.is_file():
            rows.append((f.name, f"{f.stat().st_size / 1024:.1f} KB"))
    say(f"    -> {len(rows)} 个文件：" + "、".join(r[0] for r in rows[:5]) + ("…" if len(rows) > 5 else ""))
    return {
        "state": "ok",
        "headline": f"{len(rows)} 个文件",
        "metrics": [("产物文件", len(rows))],
        "rows": rows,
        "log": "、".join(r[0] for r in rows[:6]),
        "cmd": "ls -la",
    }


STEPS = [
    ("sim",   "1/5 仿真",      step_sim),
    ("lint",  "2/5 语法检查",  step_lint),
    ("synth", "3/5 综合",      step_synth),
    ("wave",  "4/5 波形",      step_wave),
    ("files", "5/5 产物",      step_files),
]


def fail(msg: str) -> int:
    say(f"[错误] {msg}")
    if COLLECT is not None:
        print(json.dumps({"ok": False, "error": msg, "lines": COLLECT},
                         ensure_ascii=False))
    return 1


def _pop_opt(args: list, name: str) -> str | None:
    """从参数表里取一个 --xxx 值，取到就把它和它的值一起删掉。"""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
        if a.startswith(name + "="):
            v = a.split("=", 1)[1]
            del args[i]
            return v
    return None


def main() -> int:
    args = list(sys.argv[1:])
    # 执行后端：换成 Windows 原生工具链时不用碰 WSL
    be = _pop_opt(args, "--backend")
    tdir = _pop_opt(args, "--toolchain-dir")
    if be or tdir:
        set_backend(be or "auto", tdir)
    argv = [a for a in args if not a.startswith("--")]
    proj, source = resolve_project_dir(argv)

    if not proj.is_dir():
        return fail(f"找不到工程目录：{proj}（来源：{source}）。"
                    f"用法：python refresh.py <工程目录>")

    # ---- 读配置。配置有问题就地停下，绝不把残缺配置喂给 EDA 命令 ----
    raw, issues = load_config(proj)
    if raw is not None:
        cfg, more = validate(raw, proj)
        issues += more
    else:
        cfg = {}
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warn"]

    say(f"[{datetime.now():%H:%M:%S}] 开始探测 Verilog 工程")
    say(f"  工程目录: {proj}")
    say(f"  来源    : {source}")
    say(f"  配置文件: {proj / 'project.json'}")

    if errors:
        say("")
        say("  配置有误，已停止（不会执行任何 EDA 命令）：")
        for i in errors:
            say(f"    [错误] {i.message}")
            if i.fix:
                say(f"           建议：{i.fix}")
        say("")
        return fail(f"project.json 有 {len(errors)} 个错误")

    for i in warns:
        say(f"  [提醒] {i.message}")

    say(f"  项目名  : {cfg['name']}")
    say(f"  顶层模块: {cfg['top']}    设计: {cfg['dut']}    测试: {cfg['tb']}")
    be_now = detect_backend()
    say(f"  波形    : {cfg['vcd']}    超时: {cfg['timeout']}s")
    if be_now == "wsl":
        set_backend("wsl", distro=cfg.get("distro"))
        say(f"  执行后端: WSL ({cfg['distro']})")
        say(f"  WSL 路径: {to_wsl(proj)}")
    else:
        say(f"  执行后端: Windows 原生工具链")
        missing = [t for t in ("iverilog", "verilator", "yosys")
                   if not find_windows_tool(t)]
        if missing:
            say("")
            # 只提示一次：三个工具缺的是同一件事（没装工具链），
            # 每个都刷一遍长提示太吵，反而看不清重点。
            say(f"  [错误] 缺少 EDA 工具：{'、'.join(missing)}")
            say("")
            for line in tool_missing_hint(missing[0]).splitlines():
                say("  " + line)
            say("")
            return fail("缺少 EDA 工具：" + "、".join(missing))
        for t in ("iverilog", "verilator", "yosys"):
            say(f"    {t:10} -> {find_windows_tool(t)}")
    say("")

    # 让 run_tool 知道用哪个 WSL 发行版（Windows 后端用不到，但传着无害）
    set_backend(detect_backend(), tdir, distro=cfg.get("distro"))

    steps = {}
    for key, title, fn in STEPS:
        say(f"[{key}] {title}")
        try:
            steps[key] = fn(proj, cfg)
        except Exception as exc:  # noqa: BLE001
            steps[key] = {"state": "fail", "headline": "探测失败", "metrics": [],
                          "log": f"{type(exc).__name__}: {exc}", "cmd": ""}
            say(f"    -> 失败: {exc}")
        say("")

    payload = {
        "schema": 1,
        "project": {
            "name": cfg["name"],
            "path": str(proj),
            "wslPath": to_wsl(proj),
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 下面这些是改造后新增的元信息，不改动上面已有的键，
            # 所以工作台的现有读取逻辑完全不受影响。
            "top": cfg["top"],
            "dut": cfg["dut"],
            "tb": cfg["tb"],
            "vcd": cfg["vcd"],
            "distro": cfg["distro"],
            "configPath": str(proj / "project.json"),
        },
        "steps": steps,
    }

    # ---- 第 5 步：多轮视图 / 多项目视图 ----
    # 顶层只**新增**键，不动 project 和 steps，保证旧界面继续能用。
    try:
        rounds = collect_rounds(proj)
        payload["rounds"] = rounds
        payload["projects"] = collect_projects(proj)
        if rounds.get("available"):
            s = rounds.get("summary") or {}
            say(f"  轮次存档: {rounds['count']} 轮"
                + (f"，第 {rounds['fixedAtRound']} 轮修好"
                   if rounds.get("fixedAtRound") else "，尚未修好"))
            if s.get("stopReason"):
                say(f"  停止原因: {s['stopReason']}")
        if payload["projects"]:
            say(f"  同级工程: {len(payload['projects'])} 个"
                f"（{'、'.join(p['name'] for p in payload['projects'][:5])}"
                f"{'…' if len(payload['projects']) > 5 else ''}）")
    except Exception as exc:  # noqa: BLE001
        say(f"  [提醒] 读取轮次存档失败，已跳过：{type(exc).__name__}: {exc}")
        payload.setdefault("rounds", {"available": False, "rounds": []})
        payload.setdefault("projects", [])

    # 保留已有的 media 配置（背景图/视频）——探测不该把它冲掉
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            if isinstance(old.get("media"), dict):
                payload["media"] = old["media"]
        except Exception:  # noqa: BLE001
            pass

    # 把 media.src 里的本机绝对路径改成同目录文件名。
    #
    # 为什么必须做：board.json 里存的可能是作者机器上的绝对路径
    # （实测见过 "D:\\steam\\Documents\\dsh\\desk-app\\bgvideo.mp4"）。
    # 浏览器拿到这种值只会加载失败 —— 服务器模式下视频就不播了，
    # 页面上那条「背景视频没加载出来」的提示反而成了误报。
    # 改成文件名之后：服务器模式从同目录取，发给别人也能照样用。
    _media = payload.get("media")
    if isinstance(_media, dict):
        _src = str(_media.get("src") or "")
        if _src and not _src.startswith(("data:", "http://", "https://", "/")):
            _p = Path(_src)
            if _p.is_absolute():
                _media["src"] = _p.name
                payload["media"] = _media
                say(f"  媒体路径已规范化: {_src} -> {_p.name}")

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    ok_n = sum(1 for s in steps.values() if s["state"] == "ok")
    say(f"[{datetime.now():%H:%M:%S}] 探测完成：{ok_n}/{len(STEPS)} 项通过")
    say(f"  数据已写入 {OUT.name}")

    if COLLECT is not None:
        print(json.dumps({"ok": True, "lines": COLLECT, "board": payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
