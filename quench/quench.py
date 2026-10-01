#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 4 + 5：生成编排与命令行入口。

    quench.py new "8位带进位加法器"                 # 一句话 → 新工程目录
    quench.py new --from-file 需求.txt -o adder-lab # 从文件读需求（避免命令行转义问题）
    quench.py new "..." --dry-run                  # 只调 API 看结果，不落盘
    quench.py check <工程目录>                      # 只校验已有工程，不调 API

它会复用 desk-app 的 project_config.py 来校验自己生成的 project.json ——
**写进去的东西，必须自己先认。** 这样第 1 步和第 2 步的契约不会各说各话。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_api as API        # noqa: E402
import quench_parse as PARSE    # noqa: E402
import quench_prompt as PROMPT  # noqa: E402

# 复用第 1 步的契约校验器 —— 让它来判定我们生成的 project.json 合不合格
DESK_APP = HERE.parent / "desk-app"
_CONFIG_LOADED = False
load_config = validate = None
if (DESK_APP / "project_config.py").is_file():
    sys.path.insert(0, str(DESK_APP))
    try:
        from project_config import load_config, validate  # noqa: E402
        _CONFIG_LOADED = True
    except Exception:  # noqa: BLE001
        pass

PROJECTS_ROOT = HERE.parent          # 与 desk-app 平级，新工程都建在这里
VCD_NAME = PARSE.VCD_NAME
DEFAULT_TIMEOUT = 240
DEFAULT_DISTRO = "Ubuntu-24.04"


# ------------------------------------------------------------------ 工具
class QuenchError(Exception):
    """流程性错误（目录冲突、模型格式不对等）。消息直接给用户看。"""


def slugify(text: str, fallback: str = "proj") -> str:
    """把任意文字变成长度合适、文件系统安全的目录名。

    中文没有好的音译办法，与其硬造拼音（容易出错且不可读），不如退回 fallback，
    同时让用户可以用 --name 自己指定 —— 这条会写在提示里。
    """
    t = (text or "").strip().lower()
    # 只保留 ascii 字母数字和连字符
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    if not t or len(t) < 2:
        return fallback
    return t[:40].rstrip("-")


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def build_project_json(name: str, top: str, distro: str = DEFAULT_DISTRO,
                       timeout: int = DEFAULT_TIMEOUT) -> dict:
    """生成 project.json 的内容 —— 字段必须落在第 1 步的契约之内。"""
    return {
        "version": 1,
        "name": name,
        "top": top,
        "dut": PARSE.DUT_NAME,
        "tb": PARSE.TB_NAME,
        "vcd": VCD_NAME,
        "timeout": timeout,
        "distro": distro,
    }


def verify_project_json(obj: dict, project_dir: Path) -> list[str]:
    """用自己的契约校验器验一遍。返回问题列表（空 = 通过）。"""
    if not _CONFIG_LOADED:
        return ["（提示）没找到 desk-app/project_config.py，跳过了 project.json 自校验"]

    tmp = project_dir / "project.json"
    backup = tmp.read_text(encoding="utf-8") if tmp.is_file() else None
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        raw, issues = load_config(project_dir)
        if raw is None:
            return [i.message for i in issues]
        _, more = validate(raw, project_dir)
        issues += more
        return [f"[{i.level}] {i.message}" for i in issues if i.level == "error"]
    finally:
        if backup is None:
            tmp.unlink(missing_ok=True)
        else:
            tmp.write_text(backup, encoding="utf-8")


# ------------------------------------------------------------------ 核心流程
def generate(requirement: str, *, model: str = API.DEFAULT_MODEL,
             temperature: float = API.DEFAULT_TEMPERATURE,
             retries: int = API.DEFAULT_RETRIES,
             feedback: str = "", round_no: int = 1,
             log=print) -> tuple[dict[str, str], list, dict]:
    """调一次 API 并把回复解析成文件。返回 (files, issues, api_result)。

    格式不对时会自动补一次「只做格式转换」的小请求 —— 那比让模型重写一遍代码便宜。
    """
    messages = [
        {"role": "system", "content": PROMPT.SYSTEM_PROMPT},
        {"role": "user", "content": PROMPT.build_user_prompt(requirement, feedback, round_no)},
    ]
    log(f"  调用 {model}（temperature={temperature}）…")
    t0 = time.time()
    result = API.chat(messages, model=model, temperature=temperature, retries=retries)
    cost = time.time() - t0
    usage = result.get("usage") or {}
    log(f"  收到回复：{len(result['text'])} 字符，耗时 {cost:.1f} 秒，"
        f"用量 {usage.get('total_tokens', '?')} tokens")

    files, issues = PARSE.parse_response(result["text"])

    # 解析不出来（多半是没按 JSON 格式答）→ 补一次便宜的格式转换请求
    if not files and any(i.code.startswith("json/") for i in issues):
        log("  回复不是约定的 JSON 格式，补一次「只做格式转换」的请求…")
        try:
            fix = API.chat(
                [{"role": "system", "content": "你只做格式转换，不写新代码。"},
                 {"role": "user", "content": PROMPT.build_repair_prompt(result["text"])}],
                model=model, temperature=0.0, retries=2)
            files2, issues2 = PARSE.parse_response(fix["text"])
            if files2:
                log("  格式转换成功，采用转换后的结果")
                # 转换请求的用量也计入
                u2 = fix.get("usage") or {}
                result = dict(result)
                result["usage"] = {
                    "prompt_tokens": usage.get("prompt_tokens", 0) + u2.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0) + u2.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0) + u2.get("total_tokens", 0),
                }
                return files2, issues2, result
        except API.ApiError as exc:
            log(f"  格式转换也失败了：{exc}")
        log("  仍无法解析，保留原始错误")
    return files, issues, result


def write_project(project_dir: Path, files: dict[str, str], project: dict,
                  *, overwrite: bool = False, log=print) -> list[str]:
    """落盘。返回写出的文件名列表。目录已存在且非空时默认拒绝覆盖。"""
    if project_dir.exists() and any(project_dir.iterdir()) and not overwrite:
        raise QuenchError(
            f"目录已存在且不为空：{project_dir}\n"
            f"  加 --overwrite 覆盖，或用 --name 换一个目录名")

    project_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, content in files.items():
        (project_dir / fname).write_text(content, encoding="utf-8")
        written.append(fname)
        log(f"  写入 {fname}（{len(content)} 字符）")

    (project_dir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append("project.json")
    log(f"  写入 project.json（top={project['top']}）")
    return written


# ------------------------------------------------------------------ 子命令
def cmd_new(args) -> int:
    # 需求来源：命令行文本 或 文件
    if args.from_file:
        src = Path(args.from_file)
        if not src.is_file():
            print(f"[错误] 找不到需求文件：{src}")
            return 1
        requirement = src.read_text(encoding="utf-8-sig").strip()
    else:
        requirement = (args.text or "").strip()

    if not requirement:
        print("[错误] 没有需求。用法：quench.py new \"8位带进位加法器\"")
        print("       或者：quench.py new --from-file 需求.txt")
        return 1

    print(f"需求（{len(requirement)} 字符）：")
    for line in requirement.splitlines()[:8]:
        print(f"    {line}")
    if len(requirement.splitlines()) > 8:
        print("    …")
    print()

    # 目录名
    fallback = "proj-" + time.strftime("%m%d-%H%M%S")
    slug = args.name or slugify(requirement, fallback)
    project_dir = Path(args.out) if args.out else (PROJECTS_ROOT / slug)
    project_dir = project_dir.resolve()
    print(f"目标目录：{project_dir}")
    if not args.name and slug == fallback:
        print("  （需求是中文，没法自动取英文目录名，用了时间戳）")
        print("   想指定目录名就加：--name adder-lab")
    print()

    # 调 API 生成
    try:
        files, issues, result = generate(
            requirement, model=args.model, temperature=args.temperature,
            retries=args.retries)
    except API.ApiError as exc:
        print(f"\n[错误] 调用 API 失败：{exc}")
        return 1

    # 报告解析结果
    print()
    if issues:
        print("解析结果：")
        print(PARSE.format_issues(issues))
        print()

    if PARSE.has_blocking_errors(issues) or not files:
        print("[错误] 生成的代码不满足硬性约定，未落盘。")
        raw_dump = HERE / "last_raw_reply.txt"
        raw_dump.write_text(result["text"], encoding="utf-8")
        print(f"       模型的原始回复已存到 {raw_dump}，方便排查")
        print(f"       原始回复预览：{result['text'][:200]}…")
        return 1

    # 生成 project.json 并自校验
    dut_names = re.findall(r"(?m)^\s*module\s+([A-Za-z_]\w*)", files[PARSE.DUT_NAME])
    top = dut_names[0] if dut_names else "dut"
    project = build_project_json(args.project_name or slug, top,
                                 distro=args.distro, timeout=args.timeout)

    if args.dry_run:
        print("[dry-run] 不落盘。生成内容的摘要：")
        m = re.search(r'"notes"\s*:\s*"([^"]*)"', result["text"])
        if m:
            print(f"  设计说明：{m.group(1)[:200]}")
        for fname, content in files.items():
            print(f"  {fname}: {len(content.splitlines())} 行")
        print(f"  project.json: top={top}, name={project['name']}")
        return 0

    try:
        write_project(project_dir, files, project,
                      overwrite=args.overwrite, log=lambda s: print(s))
    except QuenchError as exc:
        print(f"\n[错误] {exc}")
        return 1

    # 用第 1 步的契约校验器验一遍自己写的东西
    problems = verify_project_json(project, project_dir)
    print()
    if problems:
        print("[警告] 自己生成的 project.json 没通过契约校验：")
        for p in problems:
            print("   ", p)
    else:
        print("project.json 已通过第 1 步的契约校验 ✓")

    # 提示下一步
    print()
    print("下一步可以这样验证它：")
    print(f"    python {DESK_APP / 'refresh.py'} \"{project_dir}\"")
    if args.with_refresh:
        print()
        print("（--with-refresh：现在就跑一遍探测）")
        return run_refresh(project_dir)

    return 0


def run_refresh(project_dir: Path) -> int:
    import subprocess
    script = DESK_APP / "refresh.py"
    if not script.is_file():
        print(f"[错误] 找不到 {script}")
        return 1
    p = subprocess.run([sys.executable, str(script), str(project_dir)],
                       cwd=str(DESK_APP), text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode


def cmd_retry(args) -> int:
    """生成 → 探测 → 失败回喂重试 的完整循环（第 3 步的核心）。"""
    import quench_loop as LOOP

    # 需求来源
    if args.from_file:
        src = Path(args.from_file)
        if not src.is_file():
            print(f"[错误] 找不到需求文件：{src}")
            return 1
        requirement = src.read_text(encoding="utf-8-sig").strip()
    else:
        requirement = (args.text or "").strip()
    if not requirement:
        print('[错误] 没有需求。用法：quench.py retry "8位带进位加法器"')
        return 1

    fallback = "proj-" + time.strftime("%m%d-%H%M%S")
    slug = args.name or slugify(requirement, fallback)
    project_dir = Path(args.out) if args.out else (PROJECTS_ROOT / slug)
    project_dir = project_dir.resolve()

    print("=" * 66)
    print("quench 重试循环（第 3 步）")
    print("=" * 66)
    print(f"需求    ：{requirement.splitlines()[0][:60]}")
    print(f"目标目录：{project_dir}")
    print(f"轮数上限：{args.max_rounds}    重复上限：{args.repeat_limit}")
    print(f"模型    ：{args.model}  temperature={args.temperature}")
    print(f"存档    ：{'开' if not args.no_archive else '关'}")
    print()

    project_meta = build_project_json(
        args.project_name or slug, "__TOP__",   # top 会在探测前被真实模块名替换
        distro=args.distro, timeout=args.timeout)

    def log(s=""):
        print(s, flush=True)

    res = LOOP.quench(
        requirement, project_dir,
        max_rounds=args.max_rounds,
        repeat_limit=args.repeat_limit,
        model=args.model,
        temperature=args.temperature,
        retries=args.retries,
        project_meta=None,          # 让 loop 自己算 top
        archive=not args.no_archive,
        log_fn=log,
    )

    print()
    print("=" * 66)
    print("结果")
    print("=" * 66)
    print(f"  {'✅ 通过' if res.ok else '❌ 未通过'}    停止原因：{res.stop_reason}")
    print(f"  用了 {len(res.rounds)}/{args.max_rounds} 轮，"
          f"共 {res.tokens} tokens，耗时 {res.seconds:.1f} 秒")
    if res.tb_hash:
        print(f"  锁定的 tb 哈希：{res.tb_hash}")
    print()
    print("  每轮情况：")
    for r in res.rounds:
        mark = {"ok": "✅", "compile-failed": "✗编译", "probe-failed": "✗探测",
                "tb-tampered": "⛔偷改", "api-error": "✗API"}.get(r["outcome"], "?")
        print(f"    第 {r['round']} 轮  {mark:6} {r.get('headline','')[:46]}"
              f"  {r.get('tokens',0)} tokens")
    print()
    if not args.no_archive:
        import quench_runs as RUNS
        root = RUNS.runs_root(project_dir)
        print(f"  证据存档：{root}")
        print(f"    可用 quench.py rounds \"{project_dir}\" 查看")

    if res.ok:
        print()
        print("下一步：")
        print(f"    python {DESK_APP / 'refresh.py'} \"{project_dir}\"")
    return 0 if res.ok else 1


def cmd_rounds(args) -> int:
    """列出一个工程的所有轮次存档，并显示相邻两轮的 diff（第 5 步界面的数据来源）。"""
    import quench_runs as RUNS
    project_dir = Path(args.dir).resolve()
    nums = RUNS.list_rounds(project_dir)
    if not nums:
        print(f"没有找到轮次存档：{RUNS.runs_root(project_dir)}")
        return 1

    print(f"工程：{project_dir}")
    print(f"存档：{RUNS.runs_root(project_dir)}")
    print()
    print(f"{'轮次':<6}{'结果':<12}{'失败类型':<14}{'tokens':<9}{'耗时':<8}摘要")
    print("-" * 96)
    for n in nums:
        r = RUNS.read_round(project_dir, n)
        meta = r.get("meta") or {}
        print(f"{n:<8}{str(meta.get('outcome','?')):<14}"
              f"{str(meta.get('failure_kind','')):<16}"
              f"{str(meta.get('tokens',0)):<10}"
              f"{str(round(meta.get('seconds',0),1))+'s':<9}"
              f"{str(meta.get('headline',''))[:34]}")

    if args.diff and len(nums) >= 2:
        for i in range(1, len(nums)):
            a = RUNS.read_round(project_dir, nums[i - 1]).get("dut") or ""
            b = RUNS.read_round(project_dir, nums[i]).get("dut") or ""
            if a != b:
                print()
                print(f"=== dut.v 变化：第 {nums[i-1]} 轮 → 第 {nums[i]} 轮 ===")
                print(RUNS.unified_diff(a, b, f"round-{nums[i-1]}", f"round-{nums[i]}"))
            ta = RUNS.read_round(project_dir, nums[i - 1]).get("tb") or ""
            tb_ = RUNS.read_round(project_dir, nums[i]).get("tb") or ""
            if ta != tb_:
                print(f"  ⚠️ 第 {nums[i]} 轮的 tb 和第 {nums[i-1]} 轮不同"
                      f"（哈希 {GUARD_HASH(ta)} → {GUARD_HASH(tb_)}）")
    return 0


def cmd_report(args) -> int:
    """生成质量报告（第 4 步）。"""
    import quench_report as RP
    import quench_stats as ST

    if args.dir:
        projects = [Path(args.dir).resolve()]
    else:
        projects = None      # 让聚合层自己去 runs 里找

    stats = ST.aggregate(projects=projects)
    t = stats["totals"]
    print("=" * 66)
    print("quench 生成质量报告")
    print("=" * 66)
    if not t.get("projects"):
        print("  还没有任何生成记录。")
        print("  先跑几次：python quench.py retry \"...\"")
        return 1
    print(f"  工程数       : {t['projects']}")
    print(f"  最终通过     : {t['ok']}/{t['projects']}  ({t['passRate']:.0%})")
    print(f"  一次通过率   : {t['firstPass']}/{t['projects']}  ({t['firstPassRate']:.0%})")
    print(f"  平均迭代次数 : {t['avgRounds']} 轮")
    print(f"  总 tokens    : {t['tokens']:,}")
    print()
    out = Path(args.out) if args.out else (HERE / "_report")
    md, _ = RP.write_report(stats, out)
    print()
    print("报告已生成，用浏览器或编辑器打开即可。")
    # 退出码反映的是「有没有未通过的工程」，便于脚本判断；
    # 但**报告生成本身是成功的**，所以这里不再用 1 表示"命令失败"。
    # 用 0=全通过，3=有未通过（和 1/2 的用法错误/配置错误区分开）。
    return 0 if t["failed"] == 0 else 3


def cmd_bench(args) -> int:
    """批量跑用例并出报告（第 4 步）。"""
    import quench_bench as BENCH

    suite = Path(args.suite)
    if not suite.is_file():
        print(f"[错误] 找不到用例文件：{suite}")
        return 1
    root = Path(args.root).resolve() if args.root else PROJECTS_ROOT
    only = [s.strip() for s in args.only.split(",")] if args.only else None

    print("=" * 66)
    print("quench 批量跑")
    print("=" * 66)
    print(f"用例文件：{suite}")
    print(f"工程根目录：{root}")
    print(f"轮数上限：{args.max_rounds}")
    print()

    out = BENCH.run_bench(suite, root, only=only, max_rounds=args.max_rounds,
                          model=args.model, overwrite=args.overwrite,
                          prefix=args.prefix)
    t = out["totals"]
    print("=" * 66)
    print("批量结果")
    print("=" * 66)
    if not t.get("cases"):
        print("  没有跑任何用例")
        return 1
    print(f"  跑完 {t['cases']} 个（跳过 {t['skipped']} 个）")
    print(f"  通过        : {t['ok']}/{t['cases']}  ({t['passRate']:.0%})")
    print(f"  一次通过    : {t['firstPass']}  ({t['firstPass'] / t['cases']:.0%})")
    print(f"  平均迭代    : {t['avgRounds']} 轮")
    print(f"  tokens      : {t['tokens']:,}")
    print(f"  耗时        : {t['seconds']:.0f} 秒")

    report_dir = Path(args.report_dir) if args.report_dir else (HERE / "_report")
    md = BENCH.cmd_bench_report(root, report_dir)
    print()
    print(f"报告：{md}")
    return 0


def GUARD_HASH(src: str) -> str:
    import quench_guard as G
    return G.tb_hash(src)


def cmd_check(args) -> int:
    """只校验一个已有工程的 project.json，不调 API、不跑 EDA。"""
    project_dir = Path(args.dir).resolve()
    if not _CONFIG_LOADED:
        print("[错误] 找不到 desk-app/project_config.py，无法校验")
        return 1
    if not project_dir.is_dir():
        print(f"[错误] 不是目录：{project_dir}")
        return 1
    raw, issues = load_config(project_dir)
    if raw is None:
        print(PARSE.format_issues(issues))
        return 1
    cfg, more = validate(raw, project_dir)
    errs = [i for i in issues + more if i.level == "error"]
    if errs:
        for e in errs:
            print(f"  [错误] {e.message}")
            if e.fix:
                print(f"         建议：{e.fix}")
        return 1
    print("配置有效：")
    for k, v in cfg.items():
        print(f"  {k:9} = {v!r}")
    return 0


def cmd_refresh(args) -> int:
    return run_refresh(Path(args.dir).resolve())


# ------------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quench",
        description="用一句话生成一个可跑的 Verilog 工程（第 2 步生成 + 第 3 步重试）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例子:
  # 第 2 步：一次性生成（不重试）
  quench.py new "8位带进位加法器"
  quench.py new --from-file 需求.txt --name my-alu

  # 第 3 步：带失败回喂的完整循环（推荐）
  quench.py retry --from-file 需求.txt --name my-alu
  quench.py retry "带同步复位的4位移位寄存器" --max-rounds 5

  # 查看某次生成的每一轮都发生了什么
  quench.py rounds D:\\...\\my-alu --diff

  # 其它
  quench.py check D:\\...\\adder-lab      只校验已有工程的配置
  quench.py refresh D:\\...\\adder-lab    只跑探测（第 1 步的脚本）
""")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="生成一个新工程")
    n.add_argument("text", nargs="?", help="需求描述（一句话）")
    n.add_argument("--from-file", help="从文件读需求（中文/多行推荐用这个）")
    n.add_argument("-o", "--out", help="输出目录（默认建在 desk-app 旁边）")
    n.add_argument("--name", help="目录名/项目名（不给就从需求里猜）")
    n.add_argument("--project-name", help="project.json 里的项目显示名")
    n.add_argument("--model", default=API.DEFAULT_MODEL)
    n.add_argument("--temperature", type=float, default=API.DEFAULT_TEMPERATURE)
    n.add_argument("--retries", type=int, default=API.DEFAULT_RETRIES)
    n.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="工程里每条 EDA 命令的超时（秒）")
    n.add_argument("--distro", default=DEFAULT_DISTRO, help="WSL 发行版名")
    n.add_argument("--dry-run", action="store_true", help="不落盘")
    n.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的目录")
    n.add_argument("--with-refresh", action="store_true", help="生成后立刻跑一遍探测")
    n.set_defaults(func=cmd_new)

    c = sub.add_parser("check", help="校验已有工程的 project.json")
    c.add_argument("dir")
    c.set_defaults(func=cmd_check)

    rt = sub.add_parser("retry", help="生成→探测→失败回喂重试（第 3 步的完整循环）")
    rt.add_argument("text", nargs="?", help="需求描述")
    rt.add_argument("--from-file", help="从文件读需求（中文/多行推荐）")
    rt.add_argument("-o", "--out", help="输出目录")
    rt.add_argument("--name", help="目录名/项目名")
    rt.add_argument("--project-name", help="project.json 里的项目显示名")
    rt.add_argument("--max-rounds", type=int, default=5, help="最多重试几轮（默认 5）")
    rt.add_argument("--repeat-limit", type=int, default=2,
                    help="同一错误连续出现几次就停（默认 2）")
    rt.add_argument("--model", default=API.DEFAULT_MODEL)
    rt.add_argument("--temperature", type=float, default=API.DEFAULT_TEMPERATURE)
    rt.add_argument("--retries", type=int, default=API.DEFAULT_RETRIES)
    rt.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    rt.add_argument("--distro", default=DEFAULT_DISTRO)
    rt.add_argument("--no-archive", action="store_true", help="不写轮次存档")
    rt.set_defaults(func=cmd_retry)

    rd = sub.add_parser("rounds", help="查看某个工程的轮次存档与 diff")
    rd.add_argument("dir")
    rd.add_argument("--diff", action="store_true", help="显示相邻两轮 dut.v 的 diff")
    rd.set_defaults(func=cmd_rounds)

    rp = sub.add_parser("report", help="生成质量报告（第 4 步）")
    rp.add_argument("dir", nargs="?", help="只统计某个工程；不给就统计 runs 下全部")
    rp.add_argument("-o", "--out", help="报告输出目录")
    rp.set_defaults(func=cmd_report)

    b = sub.add_parser("bench", help="批量跑用例并出报告（第 4 步）")
    b.add_argument("--suite", required=True, help="用例文件（--- 分隔）")
    b.add_argument("--root", help="工程放哪（默认与 desk-app 平级）")
    b.add_argument("--report-dir", help="报告输出目录")
    b.add_argument("--max-rounds", type=int, default=5)
    b.add_argument("--only", help="只跑名字含这些关键词的（逗号分隔）")
    b.add_argument("--prefix", help="工程目录前缀（默认取套件文件名，避免不同套件重名）")
    b.add_argument("--model", default=API.DEFAULT_MODEL)
    b.add_argument("--overwrite", action="store_true")
    b.set_defaults(func=cmd_bench)

    r = sub.add_parser("refresh", help="对已有工程跑一遍探测")
    r.add_argument("dir")
    r.set_defaults(func=cmd_refresh)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except QuenchError as exc:
        print(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
