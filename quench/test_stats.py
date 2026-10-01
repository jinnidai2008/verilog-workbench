#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 4 步的测试：错误分类 + 统计聚合 + 报告生成。

用法: python test_stats.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_classify as CL   # noqa: E402
import quench_report as RP     # noqa: E402
import quench_runs as RUNS     # noqa: E402
import quench_stats as ST      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------- 真实报错样本
# 全部来自本项目实测抓到的工具原始输出，不是编的
SAMPLES = [
    ("testbench", "给 wire 赋值",
     "tb_dut.v:67: error: q is not a valid l-value in tb_dut.\n"
     "tb_dut.v:8:      : q is declared here as wire.\n"
     "1 error(s) during elaboration."),
    ("testbench", "端口不符",
     "tb_dut.v:9: error: port ``clk'' is not a port of u_dut."),
    ("testbench", "参数丢失",
     "tb_dut.v:9: error: parameter `W` not found in `tb_dut.u_dut`."),
    ("unbound", "未声明（后面跟一串语法错，必须归到根因）",
     "m.v:3: error: Unable to bind wire/reg/memory `foo' in `m'\n"
     "m.v:3: error: syntax error\n"
     "m.v:4: error: unexpected ';'"),
    ("functional", "功能不符",
     "FAIL: q=1 want=0\nFAIL: q=2 want=1\nchecks = 3\n=== TEST FAILED ==="),
    ("latch", "verilator 锁存器警告",
     "%Warning-LATCH: m.v:5:7: Latch inferred for signal 'q' from always block"),
    ("latch", "yosys 推断出锁存器",
     "=== m ===\n  Number of cells: 3\n    $_DLATCH_P_ 1\n"),
    ("width", "位宽警告",
     "%Warning-WIDTH: m.v:4:12: Operator ASSIGN expects 8 bits on the Assign RHS, "
     "but Assign RHS's WIDTH is 4"),
    ("unsynth", "不可综合",
     "ERROR: Found unsynthesizable construct: initial block"),
    ("timeout", "命令超时",
     "命令超时（240 秒）：yosys -p 'read_verilog dut.v; synth'"),
    ("unknown", "干净输出不该误报",
     "VCD info: dumpfile dut.vcd opened for output.\nok: q=0\nchecks = 3\n"
     "=== TEST PASSED ===\ntb_dut.v:42: $finish called at 36000 (1ps)"),
    ("unknown", "只有格式风格警告",
     "%Warning-DECLFILENAME: dut.v:1:8: Filename 'dut' does not match "
     "MODULE name: 'my_counter'"),
]


def make_fake_project(root: Path, name: str, rounds: list[dict],
                      summary: dict | None = None) -> Path:
    """按真实存档结构造一个假工程（用于测汇总，不花 API 钱）。"""
    proj = root / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "dut.v").write_text("module dut; endmodule\n", encoding="utf-8")
    (proj / "tb_dut.v").write_text("module tb_dut; endmodule\n", encoding="utf-8")
    (proj / "project.json").write_text(json.dumps({
        "version": 1, "name": name, "top": "dut", "dut": "dut.v",
        "tb": "tb_dut.v", "vcd": "dut.vcd"}), encoding="utf-8")

    for r in rounds:
        RUNS.write_round(proj, r["round"], dut=r.get("dut", "module dut; endmodule"),
                         tb=r.get("tb", "module tb_dut; endmodule"),
                         log=r.get("log", ""), result=r.get("meta", {}),
                         meta=r.get("meta", {}), log_fn=lambda s: None)
    if summary is not None:
        RUNS.write_summary(proj, summary, log_fn=lambda s: None)
    return proj


def main() -> int:
    print("=" * 74)
    print("第 4 步测试：错误分类 + 统计聚合 + 报告生成")
    print("=" * 74)

    # ============================================================ A. 分类器
    print("\n【A】8 类分类器（用实测抓到的真实工具输出）")
    for want, label, text in SAMPLES:
        got, why, ev = CL.classify(text)
        check(f"{label:34} → {got}", got == want,
              f"期望 {want}，实际 {got}；判据行={ev!r}")

    check("每类都有中文标签", all(CL.CATEGORIES_ALL.get(c) for c in CL.CATEGORIES_ALL))
    check("8 类齐全", len(CL.CATEGORIES) == 8, str(list(CL.CATEGORIES)))

    # 优先级：未声明优先于语法错
    cat, _, _ = CL.classify("m.v:1: error: Unable to bind wire `x'\n"
                            "m.v:1: error: syntax error")
    check("优先级：unbound 压过 syntax", cat == "unbound", cat)
    # 超时优先级最高
    cat2, _, _ = CL.classify("命令超时（240 秒）\n...syntax error...")
    check("优先级：timeout 最高", cat2 == "timeout", cat2)

    d = CL.classify_details("%Warning-LATCH: m.v:5: Latch inferred")
    check("classify_details 给出解释和证据",
          d["category"] == "latch" and d["why"] and d["evidence"], str(d)[:120])

    counts = CL.classify_counts([s[2] for s in SAMPLES])
    check("批量分类不丢样本",
          sum(counts.values()) == len(SAMPLES), str(counts))

    # ============================================================ B. 聚合
    print("\n【B】统计聚合（用真实存档结构）")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        # 工程甲：一次通过
        make_fake_project(root.parent, "alpha", [
            {"round": 1, "meta": {"round": 1, "outcome": "ok", "tokens": 1000,
                                  "seconds": 5.0, "headline": "五步全绿"}},
        ], summary={"ok": True, "stopReason": "第 1 轮通过",
                    "roundsUsed": 1, "totalTokens": 1000, "totalSeconds": 5.0})
        # 工程乙：第 2 轮修好
        make_fake_project(root.parent, "beta", [
            {"round": 1, "meta": {"round": 1, "outcome": "probe-failed",
                                  "failure_kind": "探测未通过",
                                  "headline": "sim: TEST FAILED",
                                  "tokens": 2000, "seconds": 10.0}},
            {"round": 2, "meta": {"round": 2, "outcome": "ok", "tokens": 1500,
                                  "seconds": 6.0, "headline": "五步全绿"}},
        ], summary={"ok": True, "stopReason": "第 2 轮通过",
                    "roundsUsed": 2, "totalTokens": 3500, "totalSeconds": 16.0})
        # 工程丙：没通过（编译失败卡住）
        make_fake_project(root.parent, "gamma", [
            {"round": 1, "meta": {"round": 1, "outcome": "compile-failed",
                                  "failure_kind": "编译失败",
                                  "headline": "代码编译不过：q is not a valid l-value",
                                  "tokens": 800, "seconds": 3.0}},
            {"round": 2, "meta": {"round": 2, "outcome": "compile-failed",
                                  "failure_kind": "编译失败",
                                  "headline": "代码编译不过：q is not a valid l-value",
                                  "tokens": 900, "seconds": 3.5}},
        ], summary={"ok": False,
                    "stopReason": "同一个编译错误已连续出现 2 次，停止",
                    "roundsUsed": 2, "totalTokens": 1700, "totalSeconds": 6.5})

        st = ST.aggregate(root)
        t = st["totals"]
        check("找到 3 个工程", t["projects"] == 3, str(t))
        check("通过 2 个", t["ok"] == 2, str(t["ok"]))
        check("一次通过率 = 1/3", abs(t["firstPassRate"] - 1 / 3) < 0.01,
              f"{t['firstPassRate']}")
        check("平均迭代次数 = (1+2+2)/3 ≈ 1.67",
              abs(t["avgRounds"] - 5 / 3) < 0.01, str(t["avgRounds"]))
        check("tokens 合计 = 1000+3500+1700", t["tokens"] == 6200, str(t["tokens"]))
        check("停止原因：编译失败卡住的那次归到「重复卡住」而不是「编译失败」",
              st["reasons"].get("通过") == 2 and st["reasons"].get("重复卡住") == 1,
              str(st["reasons"]))
        check("错误类别统计到 testbench 类",
              st["categories"].get("testbench", 0) >= 2,
              str({k: v for k, v in st["categories"].items() if v}))

        names = [p["name"] for p in st["perProject"]]
        # 排序规则：先按通过与否（通过的在后），再按轮数多的在前，最后按名字
        check("排序：通过的在未通过之后", names[-1] == "gamma", str(names))
        check("排序：通过的两个里，轮数多的在前",
              names[:2] == ["beta", "alpha"], str(names))
        beta = next(p for p in st["perProject"] if p["name"] == "beta")
        check("beta 记录为第 2 轮修好", beta["fixedAtRound"] == 2 and not beta["firstPass"],
              str(beta)[:150])
        alpha = next(p for p in st["perProject"] if p["name"] == "alpha")
        check("alpha 记录为一次通过", alpha["firstPass"] is True)
        check("每轮明细都在", len(beta["rounds"]) == 2, str(len(beta["rounds"])))

        # 空目录不该崩
        empty = ST.aggregate(Path(td) / "nope")
        check("空的 runs 目录不崩", empty["totals"]["projects"] == 0)

        # ======================================================== B2. 只有存档
        print("\n【B2】工程目录被删掉后，报告仍要从存档里出得来")
        # 这是实测踩到的设计缺陷：一开始 find_projects 要求"工程目录里有
        # project.json"才算，结果验收完清理掉工程目录之后报告就全空了。
        # 存档是持久数据，工程目录只是产物 —— 结论必须只靠存档就能得出。
        with tempfile.TemporaryDirectory() as td2:
            root2 = Path(td2) / "runs"
            proj2 = make_fake_project(root2.parent, "solo", [
                {"round": 1, "meta": {"round": 1, "outcome": "ok", "tokens": 500,
                                      "seconds": 3.0, "headline": "五步全绿"}},
            ], summary={"ok": True, "stopReason": "第 1 轮通过", "roundsUsed": 1,
                        "totalTokens": 500, "totalSeconds": 3.0})
            st_before = ST.aggregate(root2)
            check("删之前能找到工程", st_before["totals"]["projects"] == 1,
                  str(st_before["totals"]))

            import shutil
            shutil.rmtree(proj2)          # 模拟验收后清理工程目录
            check("工程目录确实被删了", not proj2.exists())

            st_after = ST.aggregate(root2)
            check("删了工程目录后仍能找到（靠存档）",
                  st_after["totals"]["projects"] == 1, str(st_after["totals"]))
            check("删了之后统计数值不变",
                  st_after["totals"]["ok"] == st_before["totals"]["ok"]
                  and st_after["totals"]["tokens"] == st_before["totals"]["tokens"],
                  f"{st_before['totals']}\n      {st_after['totals']}")
            p0 = st_after["perProject"][0]
            check("标记出这是「只有存档」的工程", p0.get("archived") is True, str(p0)[:150])
            check("逐轮明细仍然读得到", len(p0["rounds"]) == 1, str(p0["rounds"]))

        # ======================================================== C. 报告
        print("\n【C】报告生成")
        out = Path(td) / "report"
        md_path, _ = RP.write_report(st, out, log_fn=lambda s: None)
        text = md_path.read_text(encoding="utf-8")
        check("report.md 生成了", md_path.is_file())
        check("包含总体指标", "一次通过率" in text and "平均迭代次数" in text)
        check("包含停止原因分布", "停止原因分布" in text)
        check("包含错误类型分布", "错误类型分布" in text)
        check("包含逐个工程明细", "`beta`" in text and "`gamma`" in text)
        check("包含未通过的工程章节", "未通过的工程" in text)
        check("一次通过率数值写对了",
              bool(re.search(r"\*\*33%\*\*", text)), text[:2000])
        check("生成了两张 SVG", (out / "chart-reasons.svg").is_file()
              and (out / "chart-categories.svg").is_file())
        check("生成了 stats.json", (out / "stats.json").is_file())
        svg = (out / "chart-categories.svg").read_text(encoding="utf-8")
        check("SVG 是合法的（有 svg 根标签和 rect）",
              svg.startswith("<svg") and "<rect" in svg and svg.rstrip().endswith("</svg>"),
              svg[:80])
        check("SVG 里的中文标签被正确转义/保留", "testbench" in svg or "未分类" in svg)

        # 空数据报告
        md2, _ = RP.write_report(ST.aggregate(Path(td) / "nope"),
                                 Path(td) / "report-empty", log_fn=lambda s: None)
        check("空数据也能生成报告（不崩）", md2.is_file())

    # ============================================================ D. 图表
    print("\n【D】图表函数的边界")
    check("空数据返回占位 SVG", "没有数据" in RP.svg_bar_chart({}))
    check("全零数据也返回占位", "没有数据" in RP.svg_bar_chart({"a": 0, "b": 0}))
    one = RP.svg_bar_chart({"唯一": 1})
    check("单个数据点正常", one.startswith("<svg") and "唯一" in one)
    big = RP.svg_bar_chart({f"类{i}": i for i in range(1, 15)})
    check("14 个数据点不崩", big.startswith("<svg") and big.count("<rect") >= 14)
    check("负数不崩（应当被过滤）",
          "没有数据" in RP.svg_bar_chart({"负": -3}))

    # ============================================================ 汇总
    print("\n" + "=" * 74)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:320]}")
    print("=" * 74)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
