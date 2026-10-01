#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 4 步（批量跑）的测试。用注入的假 loop 函数，不花 API 钱。

用法: python test_bench.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_bench as BENCH   # noqa: E402
import quench_runs as RUNS     # noqa: E402
import quench_stats as ST      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


SUITE = """--- 8位计数器
带同步复位的 8 位二进制计数器。

--- 移位寄存器
4 位左移移位寄存器。

--- 序列检测器
检测 1011 的有限状态机。
"""


class FakeResult:
    def __init__(self, ok, rounds, tokens=100):
        self.ok = ok
        self.rounds = [{"round": i + 1} for i in range(rounds)]
        self.tokens = tokens
        self.stop_reason = "第 {} 轮通过".format(rounds) if ok else "用满 3 轮仍未通过"
        self.tb_hash = "deadbeef"


def make_loop(script: list[tuple[bool, int]]):
    """按剧本返回结果的假 loop。script 用完就重复最后一个。"""
    calls: list[dict] = []

    def loop(requirement, project_dir, **kw):
        calls.append({"requirement": requirement, "dir": Path(project_dir)})
        idx = min(len(calls) - 1, len(script) - 1)
        ok, rounds = script[idx]
        # 模拟真实 loop 会写存档
        # 注意：**必须连 project.json 一起写**。统计层就是靠它认定「这是个工程」的，
        # 只写 runs 存档不写 project.json 的话，聚合会一个都找不到
        # （桩和真实行为不一致，这个坑实测踩到过）。
        Path(project_dir).mkdir(parents=True, exist_ok=True)
        # 三个文件都要写 —— 真实 loop 的 write_project 就是写这三个。
        # 桩少写一个，"目录已存在"的判断就会不一致（这个坑踩了三次，
        # 每次都是"假桩和真实行为不一致"导致的假失败）。
        (Path(project_dir) / "dut.v").write_text("module dut; endmodule\n", encoding="utf-8")
        (Path(project_dir) / "tb_dut.v").write_text("module tb_dut; endmodule\n", encoding="utf-8")
        (Path(project_dir) / "project.json").write_text(
            json.dumps({"version": 1, "name": Path(project_dir).name, "top": "dut",
                        "dut": "dut.v", "tb": "tb_dut.v", "vcd": "dut.vcd"},
                       ensure_ascii=False), encoding="utf-8")
        for i in range(rounds):
            outcome = "ok" if (ok and i == rounds - 1) else "probe-failed"
            RUNS.write_round(project_dir, i + 1,
                             dut="module dut; endmodule",
                             tb="module tb_dut; endmodule",
                             log="=== TEST FAILED ===" if outcome != "ok"
                                 else "=== TEST PASSED ===",
                             result={"round": i + 1, "outcome": outcome,
                                     "failure_kind": "" if outcome == "ok" else "探测未通过",
                                     "headline": "五步全绿" if outcome == "ok" else "sim: TEST FAILED",
                                     "tokens": 100, "seconds": 2.0},
                             meta={"round": i + 1, "outcome": outcome,
                                   "failure_kind": "" if outcome == "ok" else "探测未通过",
                                   "headline": "五步全绿" if outcome == "ok" else "sim: TEST FAILED",
                                   "tokens": 100, "seconds": 2.0},
                             log_fn=lambda s: None)
        RUNS.write_summary(project_dir, {
            "ok": ok, "roundsUsed": rounds, "totalTokens": 100 * rounds,
            "totalSeconds": 2.0 * rounds,
            "stopReason": ("第 {} 轮通过".format(rounds) if ok else "用满 3 轮仍未通过"),
        }, log_fn=lambda s: None)
        return FakeResult(ok, rounds, 100 * rounds)

    return loop, calls


def main() -> int:
    print("=" * 70)
    print("第 4 步（批量跑）测试")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as td:
        suite = Path(td) / "suite.txt"
        suite.write_text(SUITE, encoding="utf-8")
        root = Path(td) / "projects"
        root.mkdir()

        # ==================================================== A. 用例解析
        print("\n【A】用例文件解析")
        cases = BENCH.parse_suite(suite)
        check("解析出 3 个用例", len(cases) == 3, str(len(cases)))
        check("用例名取对了",
              [c["name"] for c in cases] == ["8位计数器", "移位寄存器", "序列检测器"],
              str([c["name"] for c in cases]))
        check("需求正文不含名字行", "---" not in cases[0]["requirement"],
              cases[0]["requirement"][:60])
        check("需求正文取对了", "同步复位" in cases[0]["requirement"])

        # ==================================================== B. 目录命名
        print("\n【B】目录命名")
        check("中文名回退到编号", BENCH.slug("8位计数器", 1) == "bench-01",
              BENCH.slug("8位计数器", 1))
        check("英文名直接可用", BENCH.slug("shift-register", 2) == "bench-shift-register",
              BENCH.slug("shift-register", 2))
        check("带前缀时前缀生效",
              BENCH.slug("8位计数器", 1, "hard") == "hard-01",
              BENCH.slug("8位计数器", 1, "hard"))
        check("名字太长会被截断", len(BENCH.slug("a" * 80, 3)) <= 40,
              str(len(BENCH.slug("a" * 80, 3))))
        check("不同套件不会撞名",
              BENCH.slug("8位计数器", 1, "basic") != BENCH.slug("8位计数器", 1, "hard"),
              f"{BENCH.slug('8位计数器',1,'basic')} vs {BENCH.slug('8位计数器',1,'hard')}")

        # ==================================================== C. 批量跑
        print("\n【C】批量跑（假 loop，不花 API 钱）")
        loop, calls = make_loop([(True, 1), (True, 2), (False, 3)])
        out = BENCH.run_bench(suite, root, loop_fn=loop, log_fn=lambda s: None)
        t = out["totals"]
        check("跑了 3 个用例", t["cases"] == 3, str(t))
        check("通过 2 个（第 3 个失败）", t["ok"] == 2, str(t["ok"]))
        check("一次通过 = 1 个", t["firstPass"] == 1, str(t["firstPass"]))
        check("平均迭代 = (1+2+3)/3 = 2.0", t["avgRounds"] == 2.0, str(t["avgRounds"]))
        check("tokens 合计 = 100+200+300", t["tokens"] == 600, str(t["tokens"]))
        check("三个需求都传下去了", len(calls) == 3, str(len(calls)))
        check("需求内容正确", "1011" in calls[2]["requirement"], calls[2]["requirement"][:60])

        # ==================================================== D. --only
        # 教训：**每个测试用独立的 root**。共用 root 时，前一个测试留下的目录
        # 会让后面的"目录已存在"判断生效，测试之间互相污染 ——
        # 这个编排问题在本文件里反复出现，所以统一成每个测试一个 root。
        print("\n【D】--only 过滤")
        root_only = Path(td) / "projects-only"
        root_only.mkdir()
        loop2, calls2 = make_loop([(True, 1)])
        out2 = BENCH.run_bench(suite, root_only, only=["移位"], loop_fn=loop2,
                               log_fn=lambda s: None)
        check("只跑匹配的那一个", len(calls2) == 1, str(len(calls2)))
        check("过滤后统计只有 1 个", out2["totals"]["cases"] == 1,
              str(out2["totals"]))
        check("匹配到的是移位寄存器",
              bool(calls2) and "移位" in calls2[0]["requirement"],
              str(calls2)[:120])

        # 已存在目录要跳过 —— 用独立的 root，别和前面的调用互相影响
        # 注意目录名要带套件前缀（默认取套件文件名 "suite"）
        root3 = Path(td) / "projects3"
        (root3 / "suite-01").mkdir(parents=True)
        (root3 / "suite-01" / "dut.v").write_text("// 占位", encoding="utf-8")
        loop3, calls3 = make_loop([(True, 1)])
        out3 = BENCH.run_bench(suite, root3, loop_fn=loop3, log_fn=lambda s: None)
        check("目录已存在时跳过（不覆盖）",
              out3["totals"]["skipped"] == 1 and out3["totals"]["cases"] == 2,
              str(out3["totals"]))
        check("跳过时不再为那个用例调用 loop", len(calls3) == 2, str(len(calls3)))

        # --overwrite 时应当覆盖
        loop3b, calls3b = make_loop([(True, 1)])
        out3b = BENCH.run_bench(suite, root3, loop_fn=loop3b, overwrite=True,
                                log_fn=lambda s: None)
        check("--overwrite 时会覆盖", out3b["totals"]["skipped"] == 0
              and len(calls3b) == 3, str(out3b["totals"]))

        # ==================================================== E. 异常隔离
        print("\n【E】单个用例崩了不能拖垮整批")
        root2 = Path(td) / "projects2"
        root2.mkdir()

        def boom_loop(requirement, project_dir, **kw):
            if "移位" in requirement:
                raise RuntimeError("模拟的意外错误")
            return FakeResult(True, 1)

        out4 = BENCH.run_bench(suite, root2, loop_fn=boom_loop,
                               log_fn=lambda s: None)
        check("其他用例照常跑完", out4["totals"]["cases"] == 3,
              str(out4["totals"]))
        check("崩掉的那个被记为未通过", out4["totals"]["ok"] == 2,
              str(out4["totals"]["ok"]))
        bad = [c for c in out4["cases"] if c.get("error")]
        check("错误信息被记录", len(bad) == 1 and "模拟的意外错误" in bad[0]["error"],
              str(bad)[:160])

        # ==================================================== F. 出报告
        print("\n【F】批量跑完能出报告")
        stats = ST.aggregate(projects_root=root)
        # 注意：aggregate 还会扫真实的 runs 目录（同级的），所以**不能写死总数**。
        # 只断言"本测试造的这几个都在"。写死总数会被真实数据干扰 —— 实测踩到过。
        names = {p["name"] for p in stats["perProject"]}
        mine = {"suite-01", "suite-02", "suite-03"}
        check("聚合把本测试造的工程都找到了", mine <= names,
              f"找到 {sorted(names)}")
        check("批量跑出来的工程都被正确聚合", stats["totals"]["projects"] >= 3,
              str(stats["totals"]))
        import quench_report as RP
        md, _ = RP.write_report(stats, Path(td) / "rep", log_fn=lambda s: None)
        check("报告生成成功", md.is_file())
        text = md.read_text(encoding="utf-8")
        check("报告里含批量跑出来的数字", "一次通过率" in text and "平均迭代次数" in text)

        # ==================================================== G. 边界
        print("\n【G】边界")
        empty_suite = Path(td) / "empty.txt"
        empty_suite.write_text("", encoding="utf-8")
        out5 = BENCH.run_bench(empty_suite, Path(td) / "never", loop_fn=loop,
                               log_fn=lambda s: None)
        check("空用例文件不崩", out5["totals"] == {}, str(out5["totals"]))

        only_nomatch = BENCH.run_bench(suite, Path(td) / "never2", only=["不存在的关键词"],
                                       loop_fn=loop, log_fn=lambda s: None)
        check("--only 无匹配时不崩", only_nomatch["totals"] == {},
              str(only_nomatch["totals"]))

        # 只有名字没有正文
        weird = Path(td) / "weird.txt"
        weird.write_text("--- 只有名字\n--- 有名字和正文\n做点东西\n", encoding="utf-8")
        cs = BENCH.parse_suite(weird)
        check("只有名字的用例不崩", len(cs) >= 1, str(cs))

        # ==================================================== 汇总（必须在 with 内！）
        # 教训：断言和汇总都要写在 TemporaryDirectory 的 with 块里。
        # 出了这个块目录就被删了，之后再检查文件必然失败（这个坑踩了三次）。
        print("\n" + "=" * 70)
        bad = [r for r in RESULTS if not r[1]]
        print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
        if bad:
            print("\n失败项：")
            for n, _, det in bad:
                print(f"  ✗ {n}")
                if det:
                    print(f"      {det[:320]}")
        print("=" * 70)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
