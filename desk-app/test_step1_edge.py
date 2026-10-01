#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 1 步验收（第二轮）：边界与安全 —— 专找难看的失败。

用法: python test_step1_edge.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFRESH = HERE / "refresh.py"
COUNTER = HERE.parent / "vibe-verilog-lab"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def run(args, env_extra=None, timeout=900, cwd=HERE):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, str(REFRESH)] + args, cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    # ---------------------------------------------------------- G. 奇怪的路径
    print("\n【G】路径 / 文件名里的坑（空格、中文、& 符号）")
    base = Path(tempfile.mkdtemp(prefix="dsh edge "))
    try:
        proj = base / "我的 工程 & 测试"
        proj.mkdir(parents=True)
        shutil.copy(COUNTER / "counter.v", proj / "counter.v")
        shutil.copy(COUNTER / "tb_counter.v", proj / "tb_counter.v")
        (proj / "project.json").write_text(json.dumps({
            "name": "带空格与中文的工程", "top": "counter", "dut": "counter.v",
            "tb": "tb_counter.v", "vcd": "counter.vcd", "timeout": 240,
        }, ensure_ascii=False), encoding="utf-8")

        p, out = run([str(proj), "--json"], timeout=900)
        board = None
        try:
            board = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        states = {k: v.get("state") for k, v in (board or {}).get("steps", {}).items()}
        record("路径含空格/中文/& → 无堆栈", "Traceback" not in out, out.strip()[-300:])
        record("路径含空格/中文/& → 五步全 ok",
               bool(states) and all(s == "ok" for s in states.values()), str(states))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # ---------------------------------------------------------- H. 注入
    print("\n【H】配置里塞 shell 命令（必须被当普通文件名，不能被执行）")
    canary = Path(tempfile.gettempdir()) / "dsh_canary_injected.txt"
    if canary.exists():
        canary.unlink()
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ok.v").write_text("// stub\n", encoding="utf-8")
        (d / "project.json").write_text(json.dumps({
            "name": "inj", "top": "x",
            "dut": f"ok.v; touch {canary.as_posix()}",
            "tb": "ok.v", "vcd": "a.vcd", "timeout": 30,
        }), encoding="utf-8")
        p, out = run([str(d), "--json"], timeout=300)
        record("注入串 → 无堆栈", "Traceback" not in out, out.strip()[-300:])
        record("注入串 → 没被执行（没生成 canary 文件）", not canary.exists(),
               f"canary 被创建了！{canary}")
        if canary.exists():
            canary.unlink()

    # ---------------------------------------------------------- I. 优先级
    print("\n【I】工程定位的优先级（命令行 > 环境变量 > 指路牌 > 默认）")
    # 只给环境变量，不给命令行参数
    p, out = run(["--json"], env_extra={"DSH_VERILOG_PROJECT": str(HERE.parent / "adder-lab")},
                 timeout=900)
    try:
        b = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
        got = b.get("project", {}).get("path", "")
    except Exception:  # noqa: BLE001
        got = ""
    record("环境变量生效（选到了 adder-lab）", "adder-lab" in got.replace("\\", "/"), got)

    # 命令行参数应当压过环境变量
    p, out = run([str(COUNTER), "--json"],
                 env_extra={"DSH_VERILOG_PROJECT": str(HERE.parent / "adder-lab")},
                 timeout=900)
    try:
        b = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
        got = b.get("project", {}).get("path", "")
    except Exception:  # noqa: BLE001
        got = ""
    record("命令行参数压过环境变量（选到了 counter）",
           got.replace("\\", "/").endswith("vibe-verilog-lab"), got)

    # ---------------------------------------------------------- J. 波形兜底
    print("\n【J】波形文件的兜底与歧义")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        shutil.copy(COUNTER / "counter.v", d / "counter.v")
        shutil.copy(COUNTER / "tb_counter.v", d / "tb_counter.v")
        # 配置里故意写错波形名，但目录里只有一个真实 vcd
        (d / "project.json").write_text(json.dumps({
            "name": "vcd-fallback", "top": "counter", "dut": "counter.v",
            "tb": "tb_counter.v", "vcd": "WRONG_NAME.vcd", "timeout": 240,
        }), encoding="utf-8")
        p, out = run([str(d), "--json"], timeout=900)
        try:
            b = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            b = {}
        wave = (b.get("steps") or {}).get("wave", {})
        record("波形名写错但目录只有一个 .vcd → 自动认领并成功",
               wave.get("state") == "ok", f"wave={wave.get('state')} log={wave.get('log','')[:90]}")
        record("自动认领时在日志里说明了", "自动改用" in (wave.get("log") or ""),
               wave.get("log", "")[:120])

    # ---------------------------------------------------------- K. 无波形
    print("\n【K】工程没有波形文件（应当 idle，不是 fail）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "m.v").write_text("module m(input a, output b); assign b = a; endmodule\n", encoding="utf-8")
        (d / "tb_m.v").write_text("module tb_m; endmodule\n", encoding="utf-8")
        (d / "project.json").write_text(json.dumps({
            "name": "nowave", "top": "m", "dut": "m.v", "tb": "tb_m.v",
            "vcd": "m.vcd", "timeout": 60,
        }), encoding="utf-8")
        p, out = run([str(d), "--json"], timeout=600)
        try:
            b = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            b = {}
        wave = (b.get("steps") or {}).get("wave", {})
        record("没有波形 → state=idle（不是 fail）", wave.get("state") == "idle",
               f"state={wave.get('state')}")
        record("没有波形 → 无堆栈", "Traceback" not in out)

    # ---------------------------------------------------------- L. 只读/缺失
    print("\n【L】desk-app 下没有指路牌时的默认行为")
    pointer = HERE / "project.json"
    had = pointer.exists()
    backup = None
    if had:
        backup = pointer.read_text(encoding="utf-8")
        pointer.unlink()
    try:
        p, out = run(["--json"], timeout=900)
        try:
            b = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
            got = b.get("project", {}).get("path", "")
        except Exception:  # noqa: BLE001
            got = ""
        record("无指路牌 → 回落到同级 vibe-verilog-lab",
               got.replace("\\", "/").endswith("vibe-verilog-lab"), got)
    finally:
        if had and backup is not None:
            pointer.write_text(backup, encoding="utf-8")

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 66)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:400]}")
    print("=" * 66)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
