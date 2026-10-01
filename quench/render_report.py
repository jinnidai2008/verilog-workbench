#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把统计结果渲染成一张可视化的 HTML（给报告截图用）。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_classify as CL   # noqa: E402
import quench_report as RP     # noqa: E402
import quench_stats as ST      # noqa: E402

CSS = """
body{margin:0;padding:30px 34px 44px;background:#faf9f6;
     font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1d1d1f}
h1{font-size:24px;margin:0 0 4px}
.sub{color:#8e8e93;font-size:13px;margin:0 0 26px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:26px}
.card{background:#fff;border:1px solid #e6e2d9;border-radius:12px;padding:14px 20px;min-width:148px}
.card .k{font-size:12px;color:#8e8e93}
.card .v{font-size:26px;font-weight:600;line-height:1.3}
.card .v.small{font-size:19px}
.charts{display:flex;gap:22px;flex-wrap:wrap}
.chart{background:#fff;border:1px solid #e6e2d9;border-radius:12px;padding:10px}
h2{font-size:17px;margin:28px 0 12px}
table{border-collapse:collapse;background:#fff;border:1px solid #e6e2d9;
      border-radius:10px;overflow:hidden;font-size:13.5px}
th,td{padding:8px 14px;text-align:left;border-bottom:1px solid #f0ede6}
th{background:#fcfbf8;font-weight:600;font-size:12.5px;color:#5c5c60}
tr:last-child td{border-bottom:0}
.ok{color:#1f8a52;font-weight:600}
.bad{color:#c0392b;font-weight:600}
.note{background:#fff;border:1px solid #e6e2d9;border-radius:12px;
      padding:14px 18px;margin-top:22px;font-size:13.5px;color:#3a3a3c}
.note b{color:#1d1d1f}
code{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
     background:#f4f2ee;padding:1px 6px;border-radius:4px}
"""

NOTE = """
<div class="note">
<b>这份报告里为什么只有 4 个工程？</b><br>
这里展示的是「刁难套件」：锁存器陷阱 / 窄位宽 / 混合复位 / 奇数分频器。
另有一套基础套件（计数器 / 移位寄存器 / 序列检测 / 加法器 / 交通灯 / 优先编码器），
当时 6/6 一次通过，但没有留存存档，所以没进统计。<br><br>
<b>值得注意的失败案例：</b><code>hard-04</code>（1/3 分频、50% 占空比）——
模型修不好设计，转而试图删掉 2 个检查点让测试变绿，被<b>削弱检测护栏</b>拦下并停止。
这比任何漂亮数字都更能说明工具在做真实的工程判断。<br><br>
<b>关键设计：</b>报告只依赖 <code>runs/</code> 里的逐轮存档，工程目录删掉也照样复现。
每一轮的源码、日志、结论都在存档里，可逐条核对。
</div>
"""


def render(stats: dict, out_path: Path) -> Path:
    t = stats["totals"]
    cats = {CL.CATEGORIES_ALL.get(k, k): v for k, v in stats["categories"].items()}
    svg_a = RP.svg_bar_chart(stats["reasons"], "停止原因分布")
    svg_b = RP.svg_bar_chart(cats, "错误类型分布（8 类）")

    rows = []
    for p in stats["perProject"]:
        cls = "ok" if p["ok"] else "bad"
        res = "✅ 通过" if p["ok"] else "❌ 未通过"
        fixed = f"第 {p['fixedAtRound']} 轮" if p["fixedAtRound"] else "—"
        rows.append(
            f"<tr><td><code>{p['name']}</code></td>"
            f"<td class={cls}>{res}</td><td>{fixed}</td>"
            f"<td>{p['roundsUsed']}</td><td>{p['reason']}</td>"
            f"<td>{p['tokens']:,}</td></tr>")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>quench 质量报告</title>
<style>{CSS}</style></head><body>
<h1>quench 生成质量报告</h1>
<p class="sub">数据全部来自 runs/ 里 {t['projects']} 个工程的逐轮存档 ——
工程目录已清理，报告仍可复现</p>
<div class="cards">
  <div class="card"><div class="k">生成工程数</div><div class="v">{t['projects']}</div></div>
  <div class="card"><div class="k">最终通过</div><div class="v">{t['ok']}/{t['projects']}</div></div>
  <div class="card"><div class="k">一次通过率</div><div class="v">{t['firstPassRate']:.0%}</div></div>
  <div class="card"><div class="k">平均迭代</div><div class="v">{t['avgRounds']} 轮</div></div>
  <div class="card"><div class="k">总 tokens</div><div class="v small">{t['tokens']:,}</div></div>
</div>
<div class="charts">
  <div class="chart">{svg_a}</div>
  <div class="chart">{svg_b}</div>
</div>
<h2>逐个工程明细</h2>
<table>
<tr><th>工程</th><th>结果</th><th>第几轮修好</th><th>轮数</th>
    <th>停止原因</th><th>tokens</th></tr>
{''.join(rows)}
</table>
{NOTE}
</body></html>"""
    out_path.write_text(html, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    st = ST.aggregate()
    out = render(st, HERE / "_report" / "visual.html")
    print(f"  渲染完成 → {out}")
