#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 12：统计聚合。

数据来源就是第 3 步落下的 runs/<项目>/ 存档 —— 不改数据层，只做汇总。

产出结构（第 5 步界面直接读它）：
    {
      "totals":  {projects, ok, firstPass, firstPassRate, avgRounds, tokens, seconds},
      "reasons": {通过: 3, 编译失败: 2, ...},        # 停止原因分布
      "categories": {syntax: 2, testbench: 5, ...},  # 8 类错误分布
      "perProject": [ {...}, ... ]
    }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_classify as CL   # noqa: E402
import quench_runs as RUNS     # noqa: E402

DEFAULT_RUNS_ROOT = HERE.parent / "runs"

# 停止原因 → 归类用的短标签。
# **顺序即优先级，而且要从"最具体"排到"最笼统"。**
# 例如「同一个编译错误已连续出现 2 次，停止」里同时含"编译"和"连续出现"，
# 应该归类成「重复卡住」而不是「编译失败」—— 后者丢了"它卡住了"这个关键信息。
_REASON_KEYS = [
    ("通过",        ("通过",)),
    ("测试被削弱",   ("削弱", "偷改")),
    ("测试无验证力", ("没有验证力", "恒为 0")),
    ("重复卡住",     ("连续出现", "反复")),
    ("用满轮数",     ("用满",)),
    ("API 失败",     ("API",)),
    ("编译失败",     ("编译",)),
    ("其它",        ()),
]


def reason_label(stop_reason: str) -> str:
    for label, keys in _REASON_KEYS:
        if any(k in (stop_reason or "") for k in keys):
            return label
    return "其它"


def classify_round(meta: dict) -> str:
    """给一轮存档判定错误类别。

    优先用存档时已经算好的失败类型；没有就靠 headline 再分一次。
    """
    kind = (meta or {}).get("failure_kind", "")
    headline = (meta or {}).get("headline", "")
    # 测试被削弱 / 没有验证力 这两类不进 8 类统计（它们不是 EDA 报错）
    if "削弱" in kind or "偷改" in kind or "验证力" in kind:
        return "testbench" if "验证力" in kind else "testbench"
    text = f"{kind}\n{headline}"
    cat, _, _ = CL.classify(text)
    return cat


def project_stats(project_dir: Path) -> dict:
    """汇总单个工程的所有轮次。

    project_dir 既可能是工程目录，也可能是存档目录（工程目录被清理时）。
    两种情况都要能用 —— 判据是「runs_root 能不能算出来、里面有没有 round-N」。
    """
    project_dir = Path(project_dir)
    # 如果传进来的是存档目录（自身就在 runs/ 下面），直接用
    if project_dir.parent.name == "runs" and not (project_dir / "project.json").is_file():
        archive_root = project_dir
        name = project_dir.name
        proj_path = project_dir.parent.parent / name     # 猜一个工程路径（可能不存在）
    else:
        archive_root = RUNS.runs_root(project_dir)
        name = project_dir.name
        proj_path = project_dir

    nums = sorted(
        int(c.name.split("-", 1)[1])
        for c in archive_root.iterdir()
        if c.is_dir() and c.name.startswith("round-")
        and c.name.split("-", 1)[1].isdigit()
    ) if archive_root.is_dir() else []

    rounds = []
    for n in nums:
        r = RUNS.read_round(proj_path, n)
        meta = r.get("meta") or r.get("result") or {}
        rounds.append({
            "round": n,
            "outcome": meta.get("outcome", "?"),
            "failureKind": meta.get("failure_kind", ""),
            "headline": str(meta.get("headline", ""))[:120],
            "tokens": meta.get("tokens", 0),
            "seconds": round(float(meta.get("seconds", 0) or 0), 2),
            "category": classify_round(meta),
            "tbChanged": bool(meta.get("tb_changed")),
        })

    summary_file = archive_root / "summary.json"
    summary = {}
    if summary_file.is_file():
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            summary = {}

    ok = bool(summary.get("ok"))
    if not summary and rounds:
        ok = any(r["outcome"] == "ok" for r in rounds)
    fixed_at = next((r["round"] for r in rounds if r["outcome"] == "ok"), None)
    return {
        "name": name,
        "path": str(proj_path),
        "archived": not (proj_path / "project.json").is_file(),
        "ok": ok,
        "roundsUsed": len(rounds) or summary.get("roundsUsed", 0),
        "fixedAtRound": fixed_at,
        "firstPass": bool(fixed_at == 1),
        "stopReason": summary.get("stopReason", ""),
        "reason": reason_label(summary.get("stopReason", "")),
        "tokens": summary.get("totalTokens") or sum(r["tokens"] for r in rounds),
        "seconds": round(float(summary.get("totalSeconds")
                               or sum(r["seconds"] for r in rounds)), 2),
        "categories": _count_categories(rounds),
        "rounds": rounds,
    }


def _count_categories(rounds: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rounds:
        if r["outcome"] in ("ok",):
            continue
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return counts


def find_projects(runs_root: Path | None = None,
                  projects_root: Path | None = None) -> list[Path]:
    """找出所有「跑过并被记录」的工程。

    判据：**只要有 runs 存档就算**，工程目录在不在都认。

    ⚠️ 这里改过一次设计。一开始要求「工程目录里有 project.json」才算，
    结果发现：工程目录是**可以删的产物**（临时建、验收完清理掉），
    而 runs 存档才是**持久的历史记录**。删了工程目录之后报告就全空了 ——
    实测踩到过。所以现在以存档为准，工程目录存在只是加分项。

    返回的路径可能是两种：
      · 工程目录（如果还在）
      · 存档目录（工程目录已被清理时）
    project_stats 两种都能处理。
    """
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path):
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)

    # A) runs 根目录下的每一个存档目录
    root = Path(runs_root or DEFAULT_RUNS_ROOT)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            # 存档目录里得有 round-N 或 summary.json
            has_archive = (child / "summary.json").is_file() or any(
                c.name.startswith("round-") for c in child.iterdir())
            if not has_archive:
                continue
            proj = root.parent / child.name
            add(proj if proj.is_dir() else child)

    # B) 显式给的工程根目录（批量跑用的）
    if projects_root:
        proot = Path(projects_root)
        if proot.is_dir():
            for child in sorted(proot.iterdir()):
                if child.is_dir() and RUNS.runs_root(child).is_dir():
                    add(child)

    return sorted(out, key=lambda p: p.name)


def aggregate(runs_root: Path | None = None,
              projects: list[Path] | None = None,
              projects_root: Path | None = None) -> dict:
    """汇总全部工程。

    projects 直接给了就用它；否则按 runs_root / projects_root 去找。
    """
    if projects is not None:
        projs = projects
    else:
        projs = find_projects(runs_root, projects_root)
    per = [project_stats(p) for p in projs]
    per = [p for p in per if p["roundsUsed"] > 0]
    per.sort(key=lambda p: (not p["ok"], -p["roundsUsed"], p["name"]))

    total = len(per)
    ok_n = sum(1 for p in per if p["ok"])
    first_n = sum(1 for p in per if p["firstPass"])
    rounds_sum = sum(p["roundsUsed"] for p in per)

    reasons: dict[str, int] = {}
    categories: dict[str, int] = {c: 0 for c in CL.CATEGORIES_ALL}
    for p in per:
        reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
        for c, n in p["categories"].items():
            categories[c] = categories.get(c, 0) + n

    return {
        "totals": {
            "projects": total,
            "ok": ok_n,
            "failed": total - ok_n,
            "passRate": round(ok_n / total, 4) if total else 0.0,
            "firstPass": first_n,
            "firstPassRate": round(first_n / total, 4) if total else 0.0,
            "avgRounds": round(rounds_sum / total, 2) if total else 0.0,
            "tokens": sum(p["tokens"] for p in per),
            "seconds": round(sum(p["seconds"] for p in per), 1),
        },
        "reasons": reasons,
        "categories": categories,
        "perProject": per,
    }


def load_or_aggregate(runs_root: Path | None = None) -> dict:
    return aggregate(runs_root)


def refresh_report(projects_root: Path, out_dir: Path, log_fn=print):
    """批量跑完之后出报告的统一入口（bench 模块调它）。"""
    import quench_report as RP
    stats = aggregate(projects_root=projects_root)
    return RP.write_report(stats, out_dir, log_fn=log_fn)


if __name__ == "__main__":
    stats = aggregate()
    t = stats["totals"]
    print("=== 总计 ===")
    if not t["projects"]:
        print("  runs 目录下还没有任何记录")
    else:
        print(f"  工程数        : {t['projects']}")
        print(f"  通过          : {t['ok']} / {t['projects']}  ({t['passRate']:.0%})")
        print(f"  一次通过      : {t['firstPass']}  ({t['firstPassRate']:.0%})")
        print(f"  平均迭代次数  : {t['avgRounds']}")
        print(f"  总 tokens     : {t['tokens']}")
        print(f"  总耗时        : {t['seconds']} 秒")
        print()
        print("  停止原因分布  :", stats["reasons"])
        print("  错误类别分布  :",
              {k: v for k, v in stats["categories"].items() if v})
