#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 13：报告生成。

产出 report.md + 一张图表（手写 SVG，零依赖）。

为什么图表用 SVG 而不是 PNG：
  · 不用装 matplotlib / Pillow，零依赖
  · 矢量图，放大不糊，嵌在 md 里也能直接看
  · 生成只要几毫秒
"""
from __future__ import annotations

import html
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_classify as CL   # noqa: E402
import quench_stats as ST      # noqa: E402

# 图表配色（和 desk-app 的浅色调一致）
PALETTE = ["#4b7bec", "#26a65b", "#e58e26", "#c0392b", "#8e44ad",
           "#16a085", "#d35400", "#7f8c8d", "#2c3e50"]


def svg_bar_chart(data: dict[str, int], title: str = "",
                  width: int = 660, bar_h: int = 30, gap: int = 10) -> str:
    """手写柱状图。data 是 {标签: 数值}，按数值降序。"""
    items = [(k, v) for k, v in sorted(data.items(), key=lambda x: -x[1]) if v > 0]
    if not items:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="60">'
                '<text x="10" y="34" font-family="sans-serif" font-size="14" '
                'fill="#8e8e93">没有数据</text></svg>')

    label_w = 130
    value_w = 56
    top = 44 if title else 16
    height = top + len(items) * (bar_h + gap) + 16
    maxv = max(v for _, v in items)
    plot_w = width - label_w - value_w - 20

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
             f'height="{height}" viewBox="0 0 {width} {height}">',
             '<style>'
             '.lb{font:13px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;fill:#3a3a3c}'
             '.vl{font:600 13px ui-monospace,Consolas,monospace;fill:#1d1d1f}'
             '.ti{font:600 14px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;fill:#1d1d1f}'
             '</style>',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    if title:
        parts.append(f'<text class="ti" x="10" y="24">{html.escape(title)}</text>')

    for i, (label, v) in enumerate(items):
        y = top + i * (bar_h + gap)
        w = max(3, round(plot_w * v / maxv))
        color = PALETTE[i % len(PALETTE)]
        parts.append(f'<text class="lb" x="10" y="{y + bar_h * 0.7:.0f}">'
                     f'{html.escape(str(label))}</text>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{w}" height="{bar_h}" '
                     f'rx="4" fill="{color}" opacity="0.9"/>')
        parts.append(f'<text class="vl" x="{label_w + w + 10}" '
                     f'y="{y + bar_h * 0.7:.0f}">{v}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def build_report(stats: dict) -> str:
    t = stats["totals"]
    lines: list[str] = []
    A = lines.append

    A("# quench 生成质量报告")
    A("")
    A(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A("")

    # ------------------------------------------------------------ 总览
    A("## 一、总体指标")
    A("")
    if not t["projects"]:
        A("> 还没有任何生成记录。先跑几次 `quench.py retry`，再来生成报告。")
        return "\n".join(lines)
    A("| 指标 | 数值 | 说明 |")
    A("|---|---|---|")
    A(f"| 生成工程数 | **{t['projects']}** | 一共尝试生成了几个工程 |")
    A(f"| 最终通过 | **{t['ok']} / {t['projects']}**（{_pct(t['passRate'])}） "
      f"| 经过重试后能跑通的 |")
    A(f"| **一次通过率** | **{t['firstPass']} / {t['projects']}**"
      f"（**{_pct(t['firstPassRate'])}**） | 第一轮就成功的比例，最能反映模型水平 |")
    A(f"| 平均迭代次数 | **{t['avgRounds']}** 轮 | 平均要几轮才修好 |")
    A(f"| 总 token 消耗 | {t['tokens']:,} | 换算出成本 |")
    A(f"| 总耗时 | {t['seconds']:.0f} 秒 | 含真实仿真/综合的时间 |")
    A("")

    # ------------------------------------------------------------ 停止原因
    A("## 二、停止原因分布")
    A("")
    reason_chart = svg_bar_chart(stats["reasons"], "每类停止原因的工程数")
    A("```")
    A(reason_chart)
    A("```")
    A("")
    A("![停止原因分布](chart-reasons.svg)")
    A("")

    # ------------------------------------------------------------ 错误类别
    A("## 三、错误类型分布（8 类）")
    A("")
    cats = {CL.CATEGORIES_ALL.get(k, k): v for k, v in stats["categories"].items()}
    if not any(cats.values()):
        A("> 没有记录到失败轮次 —— 要么全是一次通过，要么还没有失败样本。")
    else:
        A("```")
        A(svg_bar_chart(cats, "每类错误出现的轮次数"))
        A("```")
        A("")
        A("![错误类型分布](chart-categories.svg)")
        A("")
    A("")

    # ------------------------------------------------------------ 每个工程
    A("## 四、逐个工程明细")
    A("")
    A("| 工程 | 结果 | 第几轮修好 | 轮数 | 停止原因 | tokens | 类别 |")
    A("|---|---|---|---:|---|---:|---|")
    for p in stats["perProject"]:
        result = "✅ 通过" if p["ok"] else "❌ 未通过"
        fixed = f"第 {p['fixedAtRound']} 轮" if p["fixedAtRound"] else "—"
        cats_s = "、".join(f"{CL.CATEGORIES_ALL.get(k, k)}×{v}"
                          for k, v in p["categories"].items() if v) or "—"
        A(f"| `{p['name']}` | {result} | {fixed} | {p['roundsUsed']} | "
          f"{p['reason']} | {p['tokens']:,} | {cats_s} |")
    A("")

    # ------------------------------------------------------------ 失败详情
    failed = [p for p in stats["perProject"] if not p["ok"]]
    if failed:
        A("## 五、未通过的工程（要人工看的）")
        A("")
        for p in failed:
            A(f"### `{p['name']}`")
            A("")
            A(f"- 停止原因：{p['stopReason']}")
            A(f"- 用了 {p['roundsUsed']} 轮")
            A("")
            A("| 轮次 | 结果 | 类别 | 摘要 |")
            A("|---|---|---|---|")
            for r in p["rounds"]:
                cat = CL.CATEGORIES_ALL.get(r["category"], r["category"])
                A(f"| {r['round']} | {r['outcome']} | {cat} | "
                  f"{html.escape(r['headline'][:80])} |")
            A("")

    # ------------------------------------------------------------ 附注
    A("---")
    A("")
    A("## 附：这份报告是怎么来的")
    A("")
    A("数据全部来自 `runs/<工程>/round-N/` 里第 3 步落下的存档 —— "
      "每一轮的源码、日志、结论都在那儿，可以逐条核对。")
    A("")
    A("- 「一次通过率」= 第 1 轮就 `ok` 的比例")
    A("- 「错误类型分布」按轮次统计，不是按工程")
    A("- 分类规则见 `quench_classify.py`，每类都有对应的正则和解释")
    A("")
    return "\n".join(lines)


def write_report(stats: dict, out_dir: Path, log_fn=print) -> tuple[Path, Path]:
    """写 report.md 和两张 SVG 图。返回 (md 路径, 图表目录)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cats = {CL.CATEGORIES_ALL.get(k, k): v for k, v in stats["categories"].items()}
    svg_a = out_dir / "chart-reasons.svg"
    svg_b = out_dir / "chart-categories.svg"
    svg_a.write_text(svg_bar_chart(stats["reasons"], "停止原因分布"),
                     encoding="utf-8")
    svg_b.write_text(svg_bar_chart(cats, "错误类型分布"), encoding="utf-8")

    md = out_dir / "report.md"
    md.write_text(build_report(stats), encoding="utf-8")

    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_fn(f"  报告 → {md}")
    log_fn(f"  图表 → {svg_a.name} / {svg_b.name}")
    log_fn(f"  原始 → stats.json")
    return md, out_dir


if __name__ == "__main__":
    st = ST.aggregate()
    d = HERE / "_report_demo"
    md, _ = write_report(st, d)
    print()
    print("=== 报告预览（前 40 行）===")
    for line in md.read_text(encoding="utf-8").splitlines()[:40]:
        print(" ", line)
