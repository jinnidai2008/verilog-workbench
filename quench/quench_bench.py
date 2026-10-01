#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 14：批量跑（bench）。

截图要求「连续跑 5-8 个模块，统计一次通过率 / 平均迭代次数 / 错误类型分布」，
这个模块就是那件事的入口。

用法：
    python quench.py bench --suite suites/basic.txt
    python quench.py bench --suite suites/basic.txt --only "计数器,移位"

需求文件的格式（`---` 分隔，第一行是名字）：
    --- 8位计数器
    带同步复位的 8 位二进制计数器，输出 q。
    使能 en 为高时每个上升沿加 1。

    --- 4位移位寄存器
    ...
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_report as RP      # noqa: E402
import quench_stats as ST       # noqa: E402


def parse_suite(path: Path) -> list[dict]:
    """解析需求文件。返回 [{"name":..., "requirement":...}, ...]"""
    text = Path(path).read_text(encoding="utf-8-sig")
    blocks = [b.strip() for b in text.split("\n---") if b.strip()]
    out = []
    for i, blk in enumerate(blocks, 1):
        blk = blk.lstrip("-").strip()
        lines = blk.splitlines()
        if not lines:
            continue
        head = lines[0].strip()
        # 第一行是名字（去掉可能的 "--- " 前缀），其余是需求正文
        name = head.lstrip("-").strip() or f"case-{i}"
        body = "\n".join(lines[1:]).strip()
        if not body:
            body, name = name, f"case-{i}"
        out.append({"name": name, "requirement": body})
    return out


def slug(name: str, idx: int, prefix: str = "") -> str:
    """给批量用例生成目录名。

    中文没法音译，统一用 <前缀>NN。**前缀必须有**：不然两套用例都用
    bench-01、bench-02…，互相覆盖 —— 实测踩到过（跑第二套时全被当成
    "目录已存在"跳过）。默认拿套件文件名当前缀。
    """
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    base = s[:30] if len(s) >= 3 else f"{idx:02d}"
    p = re.sub(r"[^a-z0-9]+", "-", (prefix or "").lower()).strip("-")
    return f"{p}-{base}" if p else f"bench-{base}"


def run_bench(suite_path: Path, projects_root: Path, *,
              only: list[str] | None = None,
              max_rounds: int = 5, repeat_limit: int = 2,
              model: str | None = None, temperature: float | None = None,
              overwrite: bool = False, prefix: str | None = None,
              loop_fn=None, log_fn=print) -> dict:
    """跑一整套用例。返回汇总。

    loop_fn 可注入（测试时用假模型替掉真 API）。
    prefix 用于给目录加前缀，避免不同套件重名 —— 默认取套件文件名。
    """
    cases = parse_suite(suite_path)
    if only:
        keys = [o.strip() for o in only if o.strip()]
        cases = [c for c in cases if any(k in c["name"] for k in keys)]
    if not cases:
        log_fn("（没有匹配的用例）")
        return {"cases": [], "totals": {}}

    if prefix is None:
        import re
        prefix = re.sub(r"[^a-z0-9]+", "-", Path(suite_path).stem.lower()).strip("-") or "bench"

    if loop_fn is None:
        import quench_loop as LOOP
        loop_fn = LOOP.quench

    log_fn(f"批量用例：{len(cases)} 个（目录前缀：{prefix}-）")
    log_fn("")

    results = []
    for i, case in enumerate(cases, 1):
        name = case["name"]
        proj = Path(projects_root) / slug(name, i, prefix)
        log_fn("=" * 68)
        log_fn(f"[{i}/{len(cases)}] {name}")
        log_fn(f"    目录：{proj}")
        log_fn("=" * 68)

        if proj.exists() and any(proj.iterdir()) and not overwrite:
            log_fn("  目录已存在且非空，跳过（加 --overwrite 覆盖）")
            results.append({"name": name, "dir": str(proj), "skipped": True})
            continue

        kwargs = {"max_rounds": max_rounds, "repeat_limit": repeat_limit,
                  "archive": True, "log_fn": log_fn}
        if model:
            kwargs["model"] = model
        if temperature is not None:
            kwargs["temperature"] = temperature

        t0 = time.time()
        try:
            res = loop_fn(case["requirement"], proj, **kwargs)
            results.append({
                "name": name, "dir": str(proj), "skipped": False,
                "ok": res.ok, "rounds": len(res.rounds),
                "tokens": res.tokens, "seconds": round(time.time() - t0, 1),
                "stopReason": res.stop_reason,
            })
            log_fn(f"  → {'✅ 通过' if res.ok else '❌ 未通过'}"
                   f"（{len(res.rounds)} 轮，{res.tokens} tokens）")
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "dir": str(proj), "skipped": False,
                            "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            log_fn(f"  → ❌ 异常：{type(exc).__name__}: {exc}")
        log_fn("")

    done = [r for r in results if not r.get("skipped")]
    ok_n = sum(1 for r in done if r.get("ok"))
    totals = {
        "cases": len(done),
        "skipped": len(results) - len(done),
        "ok": ok_n,
        "passRate": round(ok_n / len(done), 4) if done else 0.0,
        "firstPass": sum(1 for r in done if r.get("rounds") == 1 and r.get("ok")),
        "avgRounds": round(sum(r.get("rounds", 0) for r in done) / len(done), 2) if done else 0.0,
        "tokens": sum(r.get("tokens", 0) for r in done),
        "seconds": round(sum(r.get("seconds", 0) for r in done), 1),
    }
    return {"cases": results, "totals": totals}


def cmd_bench_report(projects_root: Path, out_dir: Path, log_fn=print) -> Path:
    """批量跑完之后生成报告。"""
    stats = ST.aggregate(projects_root=projects_root)
    md, _ = RP.write_report(stats, out_dir, log_fn=log_fn)
    return md


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="批量跑用例")
    ap.add_argument("--suite", required=True, help="需求文件路径")
    ap.add_argument("--root", default=str(HERE.parent), help="工程放哪")
    ap.add_argument("--report-dir", default=str(HERE / "_report"))
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument("--only", help="只跑名字里含这些关键词的（逗号分隔）")
    ap.add_argument("--prefix", help="工程目录前缀（默认取套件文件名）")
    args = ap.parse_args()

    out = run_bench(Path(args.suite), Path(args.root),
                    only=(args.only.split(",") if args.only else None),
                    max_rounds=args.max_rounds, prefix=args.prefix)
    print()
    print("=== 批量结果 ===")
    for k, v in out["totals"].items():
        print(f"  {k:12} = {v}")
    md = cmd_bench_report(Path(args.root), Path(args.report_dir))
    print(f"\n报告：{md}")
