#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 3 步验收（第二轮）：边界与稳健性 —— 专挑刺。

用法: python test_loop_edge.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_guard as GUARD      # noqa: E402
import quench_loop as LOOP        # noqa: E402
import quench_runs as RUNS        # noqa: E402


class Scripted:
    """按剧本返回的假模型。剧本用完就重复最后一个。"""

    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, messages, **kw):
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        item = self.script[idx]
        if isinstance(item, Exception):
            raise item
        return item

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# 顶层模块**不叫 dut** 的设计 —— 提示词要求叫 dut，但模型不一定听
DUT_NAMED_OTHER = """module my_counter #(parameter W = 4) (
    input  wire         clk,
    input  wire         rst_n,
    output reg  [W-1:0] q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= {W{1'b0}};
        else        q <= q + 1'b1;
    end
endmodule
"""

TB_FOR_OTHER = """`timescale 1ns/1ps
module tb_dut;
    reg        clk = 0;
    reg        rst_n = 0;
    wire [3:0] q;
    integer errors = 0;
    integer checks = 0;

    my_counter #(.W(4)) u_dut (.clk(clk), .rst_n(rst_n), .q(q));
    always #5 clk = ~clk;

    task check(input [3:0] want);
        begin
            @(posedge clk); #1;
            checks = checks + 1;
            if (q !== want) begin
                errors = errors + 1;
                $display("FAIL: q=%0d want=%0d", q, want);
            end else $display("ok: q=%0d", q);
        end
    endtask

    initial begin
        $dumpfile("dut.vcd");
        $dumpvars(0, tb_dut);
        @(negedge clk);
        rst_n = 1;
        check(4'd1);
        check(4'd2);
        $display("checks = %0d", checks);
        if (errors == 0) $display("=== TEST PASSED ===");
        else             $display("=== TEST FAILED ===");
        $finish;
    end
endmodule
"""

GOOD_DUT = """module dut #(parameter W = 4) (
    input  wire         clk,
    input  wire         rst_n,
    output reg  [W-1:0] q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= {W{1'b0}};
        else        q <= q + 1'b1;
    end
endmodule
"""

GOOD_TB = TB_FOR_OTHER.replace("my_counter", "dut")

# 设计有真 bug：q 一直不增加，探测必然失败
BROKEN_DUT = GOOD_DUT.replace("q <= q + 1'b1;", "q <= q;")

# 被"改松"的测试平台：加了常量互比（恒为真），削弱检测应当抓到
TAMPERED_TB = GOOD_TB.replace(
    "            if (q !== want) begin",
    "            if (1'b0 !== 1'b0) begin").replace(
    "        check(4'd1);", "        check(4'd1);\n        if (1'b1 == 1'b1) checks = checks;")


def reply(dut=GOOD_DUT, tb=GOOD_TB, **extra) -> dict:
    obj = {"dut": dut, "tb": tb, "top": "dut", "notes": ""}
    obj.update(extra)
    return {"text": json.dumps(obj, ensure_ascii=False),
            "usage": {"total_tokens": 100}, "model": "fake"}


class Scripted:
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, messages, **kw):
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        item = self.script[idx]
        if isinstance(item, Exception):
            raise item
        return item


def run(script, name, **kw):
    """跑一次循环，把断言放进回调里，保证临时目录还活着时才能看内容。

    TemporaryDirectory 退出时会连目录一起删掉 —— 在 with 外面拿到的 proj
    其实已经不存在了（这个坑实测踩过，白查了半天"文件没写出来"）。
    """
    model = Scripted(script)
    archive = kw.pop("archive", False)
    with patch.object(LOOP.API, "chat", model):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / name
            res = LOOP.quench("测试需求", proj, archive=archive,
                              log_fn=lambda s: None, **kw)
            return res, proj, model


def run_in(script, name, fn, **kw):
    """同上，但在 with 块内部调用 fn(res, proj) 做断言。"""
    model = Scripted(script)
    archive = kw.pop("archive", False)
    with patch.object(LOOP.API, "chat", model):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / name
            res = LOOP.quench("测试需求", proj, archive=archive,
                              log_fn=lambda s: None, **kw)
            return fn(res, proj), model


def main() -> int:
    print("=" * 72)
    print("第 3 步验收（第二轮）：边界与稳健性")
    print("=" * 72)

    # ============================================================ A. top 名字
    print("\n【A】顶层模块名：project.json 的 top 必须和 dut.v 里的模块名一致")

    def assert_top_matches(res, proj):
        pj = proj / "project.json"
        dutv = proj / "dut.v"
        if not pj.is_file() or not dutv.is_file():
            return False, f"缺文件：{pj.is_file()=} {dutv.is_file()=}"
        cfg = json.loads(pj.read_text(encoding="utf-8"))
        import re as _re
        m = _re.search(r"(?m)^\s*module\s+([A-Za-z_]\w*)", dutv.read_text(encoding="utf-8"))
        actual = m.group(1) if m else None
        if cfg.get("top") != actual:
            return False, f"top={cfg.get('top')!r} 但 dut.v 里的模块名是 {actual!r}"
        return True, ""

    (ok, why), _ = run_in([reply(dut=DUT_NAMED_OTHER, tb=TB_FOR_OTHER)], "topname",
                          assert_top_matches, max_rounds=1)
    check("project.json 的 top 与 dut.v 的模块名一致", ok, why)

    def assert_top_is_dut(res, proj):
        cfg = json.loads((proj / "project.json").read_text(encoding="utf-8"))
        return cfg.get("top") == "dut", f"top={cfg.get('top')!r}"

    (ok_dut, why_dut), _ = run_in([reply(dut=DUT_NAMED_OTHER, tb=TB_FOR_OTHER)],
                                  "topname2", assert_top_is_dut, max_rounds=1)
    check("模块名被统一成 dut（这样文件名与模块名才相符）", ok_dut, why_dut)

    def assert_green(res, proj):
        return res.ok, f"ok={res.ok} stop={res.stop_reason} | " + \
            str([r.get("headline") for r in res.rounds])

    (ok2, why2), _ = run_in([reply(dut=DUT_NAMED_OTHER, tb=TB_FOR_OTHER)], "topname3",
                            assert_green, max_rounds=1)
    check("模型把模块叫成别的名字时，仍然五步全绿（不再有 DECLFILENAME 警告）",
          ok2, why2)

    # ============================================================ B. 需求边界
    print("\n【B】奇怪的需求不该把程序弄崩")
    for label, req in (("空字符串", ""), ("只有空白", "   \n  "), ("超长需求", "计数器。" * 3000)):
        try:
            res, _, _ = run([reply()], "req-edge", max_rounds=1)
            check(f"{label} → 不崩", True)
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → 不崩", False, f"{type(exc).__name__}: {exc}")

    # ============================================================ C. 异常回复
    print("\n【C】模型返回各种畸形内容时不能崩")
    weird = [
        ("空回复", {"text": "", "usage": {}, "model": "x"}),
        ("纯文本不是 JSON", {"text": "我给你写个计数器吧", "usage": {}, "model": "x"}),
        ("JSON 但缺 tb", {"text": json.dumps({"dut": GOOD_DUT}), "usage": {}, "model": "x"}),
        ("JSON 是数组", {"text": "[1,2,3]", "usage": {}, "model": "x"}),
        ("dut 是数字", {"text": json.dumps({"dut": 123, "tb": GOOD_TB}), "usage": {}, "model": "x"}),
        ("usage 是 None", {"text": json.dumps({"dut": GOOD_DUT, "tb": GOOD_TB}),
                           "usage": None, "model": "x"}),
    ]
    for label, payload in weird:
        try:
            res, _, _ = run([payload], "weird", max_rounds=2, repeat_limit=9)
            check(f"{label} → 不崩且有结论", isinstance(res.ok, bool),
                  f"stop={res.stop_reason}")
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → 不崩", False, f"{type(exc).__name__}: {exc}")

    # ============================================================ D. 削弱检测
    print("\n【D】削弱检测的边界（不能误报，也不能漏报）")
    base = GOOD_TB
    cases = [
        ("只加注释", base + "\n// 加个注释\n", False),
        ("调整空白缩进", base.replace("    ", "  "), False),
        ("把比较改成恒假", base.replace("if (q !== want)", "if (0)"), True),
        ("删掉一半检查点",
         base.replace("check(4'd1);", "").replace("check(4'd2);", ""), True),
        ("把 q 的比较换掉", base.replace("q !== want", "1'b0 !== 1'b0"), True),
    ]
    for label, new_tb, want_weaker in cases:
        weaker, why = GUARD.tb_is_weaker(base, new_tb)
        mark = "OK " if weaker == want_weaker else "BAD"
        check(f"{mark} {label:18} → 削弱={weaker}（期望 {want_weaker}）",
              weaker == want_weaker, f"理由={why}")

    # ============================================================ E. 错误指纹
    print("\n【E】错误指纹：同一个错必须同指纹，即便报错格式不同")
    variants = [
        "tb_dut.v:67: error: q is not a valid l-value in tb_dut.",
        "tb_dut.v:67: error: q is not a valid l-value in tb_dut.\n",
        "  tb_dut.v:67: error: q is not a valid l-value in tb_dut.  ",
        "tb_dut.v:670: error: q is not a valid l-value in tb_dut.",
    ]
    fps = {GUARD.error_fingerprint(v) for v in variants}
    check("四种写法（空白/行号不同）指纹一致", len(fps) == 1, str(fps))
    other = GUARD.error_fingerprint("tb_dut.v:1: error: undeclared identifier zzz")
    check("不同的错误指纹不同", other not in fps)

    # ============================================================ F. 存档稳健
    print("\n【F】存档读取要容错（界面翻旧档时用）")
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "arc"
        RUNS.write_round(proj, 1, dut="module d; endmodule", tb="module t; endmodule",
                         log="x", result={"ok": False}, meta={"tokens": 1},
                         log_fn=lambda s: None)
        (RUNS.round_dir(proj, 1) / "result.json").write_text("{坏掉的 JSON", encoding="utf-8")
        r = RUNS.read_round(proj, 1)
        check("result.json 坏掉时不抛异常", r["result"] is None and r["dut"] is not None,
              str({k: type(v).__name__ for k, v in r.items()}))
        missing = RUNS.read_round(proj, 99)
        check("读不存在的轮次不抛异常", missing["exists"] is False)
        check("list_rounds 忽略非法目录名",
              (RUNS.runs_root(proj) / "round-abc").mkdir(parents=True) is None
              and RUNS.list_rounds(proj) == [1],
              str(RUNS.list_rounds(proj)))

    # ============================================================ F2. 最后一轮也要存档
    print("\n【F2】撞上限停止时，最后一轮的证据也必须落盘")
    # 实测踩到过：tamper 分支撞上限时直接 break，没存档 ——
    # 结果 summary 说"用了 5 轮"、runs 里只有 4 个 round 目录，
    # 界面上翻不到最后一轮，统计和存档对不上。
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "archive-last"
        # 用本文件自己的 Scripted（test_loop.py 里那个不在这儿）
        model = Scripted([reply(dut=BROKEN_DUT), reply(tb=TAMPERED_TB)])
        with patch.object(LOOP.API, "chat", model):
            res = LOOP.quench("4 位计数器", proj, max_rounds=6, repeat_limit=2,
                              archive=True, log_fn=lambda s: None)
        nums = RUNS.list_rounds(proj)
        check(f"存档轮数({len(nums)}) 等于实际跑的轮数({len(res.rounds)})",
              len(nums) == len(res.rounds), f"存档={nums} 实际={len(res.rounds)}")
        check("最后一轮（撞上限那次）也有存档",
              bool(nums) and nums[-1] == len(res.rounds), f"存档={nums}")

    # ============================================================ G. 参数组合
    print("\n【G】参数边界")
    res, _, model = run([reply(dut=DUT_NAMED_OTHER.replace("q + 1'b1", "q"))], "params",
                        max_rounds=1, repeat_limit=1)
    check("max_rounds=1 时只跑一轮", len(model.calls) == 1, str(len(model.calls)))

    res, _, model = run([reply(dut=DUT_NAMED_OTHER.replace("q + 1'b1", "q"))], "params2",
                        max_rounds=3, repeat_limit=1)
    check("repeat_limit=1 时第一轮失败就停（不浪费后续轮）",
          len(model.calls) == 1, f"调了 {len(model.calls)} 次")

    # ============================================================ 汇总
    print("\n" + "=" * 72)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:320]}")
    print("=" * 72)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
