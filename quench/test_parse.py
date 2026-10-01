#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块 3（解析层）的测试 —— 全部用假数据，不花 API 钱。

用法: python test_parse.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quench_parse as _QP  # noqa: E402
from quench_parse import (  # noqa: E402
    RESULT_FAIL, RESULT_PASS, extract_json_object, format_issues,
    has_blocking_errors, repair_json, strip_fences,
)


def parse_response(text, **kw):
    """本组测试只测「文本解析」，不测编译。

    编译检查单独测（那个要跑 iverilog）。这里统一关掉，好处是单测能纯离线跑，
    也不会因为环境里有没有 iverilog 而结果不同。
    """
    kw.setdefault("compile_check", False)
    return _QP.parse_response(text, **kw)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ 干净样本
GOOD_DUT = """module adder #(parameter W=8) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [W-1:0] a, b,
    output reg  [W:0]   sum
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) sum <= 0;
        else        sum <= a + b;
    end
endmodule"""

GOOD_TB = """`timescale 1ns/1ps
module tb_dut;
    reg clk = 0, rst_n = 0;
    reg  [7:0] a = 0, b = 0;
    wire [8:0] sum;
    integer errors = 0;
    integer checks = 0;

    adder #(.W(8)) u_dut (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .sum(sum));

    always #5 clk = ~clk;

    task check(input [7:0] ta, input [7:0] tb_, input [8:0] want);
        begin
            a = ta; b = tb_;
            @(posedge clk); #1;
            checks = checks + 1;
            if (sum !== want) begin
                errors = errors + 1;
                $display("FAIL: %0d+%0d=%0d want %0d", ta, tb_, sum, want);
            end else $display("ok: %0d+%0d=%0d", ta, tb_, sum);
        end
    endtask

    initial begin
        $dumpfile("dut.vcd");
        $dumpvars(0, tb_dut);
        #12 rst_n = 1;
        check(8'd1, 8'd2, 9'd3);
        check(8'd255, 8'd1, 9'd256);
        $display("----");
        $display("checks = %0d", checks);
        if (errors == 0) $display("=== TEST PASSED ===");
        else             $display("=== TEST FAILED ===");
        $finish;
    end
endmodule"""


def wrap(dut=GOOD_DUT, tb=GOOD_TB, **extra) -> str:
    obj = {"dut": dut, "tb": tb}
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False)


def main() -> int:
    print("=" * 68)
    print("模块 3（解析层）测试")
    print("=" * 68)

    # ---------------------------------------------------------- A. 剥围栏
    print("\n【A】剥 markdown 围栏")
    check("```json 围栏", strip_fences('```json\n{"a":1}\n```') == '{"a":1}')
    check("无语言围栏", strip_fences('```\n{"a":1}\n```') == '{"a":1}')
    check("无围栏原样返回", strip_fences('{"a":1}') == '{"a":1}')
    check("只有开头围栏（被截断）", strip_fences('```json\n{"a":1}') == '{"a":1}')

    # ---------------------------------------------------------- B. 抠 JSON
    print("\n【B】从一堆废话里抠出 JSON")
    check("前后有客套话",
          extract_json_object('好的，我来生成：\n{"dut":"x"}\n希望有帮助！') == '{"dut":"x"}')
    check("嵌套对象只取最外层",
          extract_json_object('前言 {"a":{"b":{"c":1}},"d":2} 后记') == '{"a":{"b":{"c":1}},"d":2}')
    check("字符串里的花括号不干扰配对",
          extract_json_object('{"code":"module m; endmodule","n":1}') == '{"code":"module m; endmodule","n":1}')
    check("字符串里的转义引号不干扰",
          extract_json_object(r'{"s":"he said \"hi\" {ok}"}') == r'{"s":"he said \"hi\" {ok}"}')
    check("括号不配对时返回 None", extract_json_object('{"a":1') is None)
    check("没有花括号时返回 None", extract_json_object('完全没有 JSON') is None)

    # ---------------------------------------------------------- C. 修尾随逗号
    print("\n【C】修尾随逗号")
    check("对象尾随逗号", json.loads(repair_json('{"a":1,}')) == {"a": 1})
    check("数组尾随逗号", json.loads(repair_json('{"a":[1,2,]}')) == {"a": [1, 2]})
    check("正常 JSON 不受影响", repair_json('{"a":1}') == '{"a":1}')
    check("字符串里的逗号不动", repair_json('{"a":"x,}"}') == '{"a":"x,}"}')

    # ---------------------------------------------------------- D. 正常解析
    print("\n【D】正常回复应当通过")
    files, issues = parse_response(wrap())
    check("产出了两个文件", set(files) == {"dut.v", "tb_dut.v"}, str(set(files)))
    check("无阻断性错误", not has_blocking_errors(issues), format_issues(issues))
    check("无任何提醒", not issues, format_issues(issues))

    # ---------------------------------------------------------- E. 脏回复
    print("\n【E】脏回复也要能解析")
    dirty_cases = [
        ("包在 ```json 围栏里", f"好的：\n```json\n{wrap()}\n```\n完成！"),
        ("尾随逗号", wrap().replace('"tb":', '"tb":').rstrip()[:-1] + ',}'),
        ("前面一大段解释", "我分析了需求，决定用带参数的模块。\n\n" + wrap()),
        ("前后都有解释", "这是代码：\n" + wrap() + "\n\n需要我调整吗？"),
    ]
    for label, text in dirty_cases:
        f, iss = parse_response(text)
        check(f"{label} → 能解析出两个文件",
              set(f) == {"dut.v", "tb_dut.v"} and not has_blocking_errors(iss),
              format_issues(iss))

    # ---------------------------------------------------------- F. 换键名
    print("\n【F】模型换用别的键名也要认")
    alt = [
        ("files 嵌套", json.dumps({"files": {"dut.v": GOOD_DUT, "tb_dut.v": GOOD_TB}}, ensure_ascii=False)),
        ("dut_v / tb_dut", json.dumps({"dut_v": GOOD_DUT, "tb_dut": GOOD_TB}, ensure_ascii=False)),
        ("design / testbench", json.dumps({"design": GOOD_DUT, "testbench": GOOD_TB}, ensure_ascii=False)),
    ]
    for label, text in alt:
        f, iss = parse_response(text)
        check(f"{label} → 能解析", set(f) == {"dut.v", "tb_dut.v"}, format_issues(iss))

    # ---------------------------------------------------------- G. 该拦住
    print("\n【G】有问题的回复必须被拦住")
    block_cases = [
        ("完全不是 JSON", "我给你写一个加法器吧，代码如下……", "json/not-found"),
        ("JSON 但缺 dut", json.dumps({"tb": GOOD_TB}), "field/missing:dut"),
        ("JSON 但缺 tb", json.dumps({"dut": GOOD_DUT}), "field/missing:tb"),
        ("dut 里没有 module", json.dumps({"dut": "随便写点什么", "tb": GOOD_TB}), "dut/no-module"),
        ("tb 里没有 $dumpfile",
         json.dumps({"dut": GOOD_DUT, "tb": GOOD_TB.replace('$dumpfile("dut.vcd");', "")}),
         "tb/no-dumpfile"),
        ("tb 里没有 PASSED 标记",
         json.dumps({"dut": GOOD_DUT, "tb": GOOD_TB.replace(RESULT_PASS, "PASS")}),
         "tb/no-pass-marker"),
        ("tb 里没有 FAILED 标记",
         json.dumps({"dut": GOOD_DUT, "tb": GOOD_TB.replace(RESULT_FAIL, "FAIL")}),
         "tb/no-fail-marker"),
        ("tb 没实例化 dut",
         json.dumps({"dut": GOOD_DUT, "tb": GOOD_TB.replace("adder #(.W(8)) u_dut", "其他模块 u_x")}),
         "tb/not-instantiate"),
    ]
    for label, text, want_code in block_cases:
        f, iss = parse_response(text)
        codes = [i.code for i in iss]
        # 契约：parse_response 不替调用方丢弃文件，拦截由 has_blocking_errors 决定
        blocked = has_blocking_errors(iss)
        hit = any(c == want_code or c.startswith(want_code) for c in codes)
        check(f"{label} → 判定为阻断且报 {want_code}", blocked and hit,
              f"blocked={blocked} codes={codes} files={list(f)}")

    # ---------------------------------------------------------- H. 该提醒但不拦
    print("\n【H】可疑但不该拦的情况")
    warn_cases = [
        ("dut 里混进了 $dumpfile",
         json.dumps({"dut": GOOD_DUT.replace("endmodule", 'initial $dumpfile("x.vcd");\nendmodule'),
                     "tb": GOOD_TB}), "dut/contains:$dumpfile"),
    ]
    for label, text, want_code in warn_cases:
        f, iss = parse_response(text)
        codes = [i.code for i in iss]
        ok = (not has_blocking_errors(iss)) and bool(f) and want_code in codes
        check(f"{label} → 只提醒不拦", ok, f"codes={codes} blocked={has_blocking_errors(iss)}")
    # 单独把「tb 没有 task」做一次。注意不能只把 task 注释掉 ——
    # 注释里仍带 "task" 字样，那测的就是别的东西了。
    tb_no_task = """`timescale 1ns/1ps
module tb_dut;
    reg clk = 0, rst_n = 0;
    reg  [7:0] a = 0, b = 0;
    wire [8:0] sum;
    integer errors = 0;
    integer checks = 0;

    adder #(.W(8)) u_dut (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .sum(sum));
    always #5 clk = ~clk;

    initial begin
        $dumpfile("dut.vcd");
        $dumpvars(0, tb_dut);
        #12 rst_n = 1;
        a = 8'd1; b = 8'd2; @(posedge clk); #1; checks = checks + 1;
        if (sum !== 9'd3) errors = errors + 1;
        a = 8'd255; b = 8'd1; @(posedge clk); #1; checks = checks + 1;
        if (sum !== 9'd256) errors = errors + 1;
        $display("checks = %0d", checks);
        if (errors == 0) $display("=== TEST PASSED ===");
        else             $display("=== TEST FAILED ===");
        $finish;
    end
endmodule
"""
    f, iss = parse_response(json.dumps({"dut": GOOD_DUT, "tb": tb_no_task}))
    codes = [i.code for i in iss]
    check("tb 真的没有 task/function 时会提醒",
          "tb/no-check-task" in codes and not has_blocking_errors(iss),
          f"codes={codes}")

    # ---------------------------------------------------------- I. 大括号地狱
    print("\n【I】Verilog 里全是花括号/引号，别把配对搞乱")
    tricky_dut = GOOD_DUT.replace("output reg  [W:0]   sum",
                                  'output reg  [W:0]   sum  /* { 假装是注释 } */')
    tricky_tb = GOOD_TB.replace('$display("ok: %0d+%0d=%0d", ta, tb_, sum);',
                                '$display("ok: {\\"nested\\": %0d}", ta);')
    f, iss = parse_response(json.dumps({"dut": tricky_dut, "tb": tricky_tb}, ensure_ascii=False))
    check("含花括号与转义引号仍能解析", set(f) == {"dut.v", "tb_dut.v"} and not has_blocking_errors(iss),
          format_issues(iss))

    # ---------------------------------------------------------- J. 空输入
    print("\n【J】空/异常输入不该崩")
    for label, text in (("空字符串", ""), ("全是空白", "   \n\t  "), ("None", None)):
        try:
            f, iss = parse_response(text)
            check(f"{label} → 不崩且被拦住", not f and has_blocking_errors(iss))
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → 不崩", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 68)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:300]}")
    print("=" * 68)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
