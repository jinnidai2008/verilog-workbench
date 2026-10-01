#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词契约 ↔ 解析层校验 的对齐测试。

这是模块 2 里最值钱的一个测试：
如果提示词要求了某条规则、但解析层不检查它 → 模型违反时没人发现（漏检）。
如果解析层检查了某条规则、但提示词没说   → 模型无从遵守（必被拦，第 3 步会死循环）。

用法: python test_prompt_alignment.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quench_parse as QP  # noqa: E402
import quench_prompt as QPm  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# 一份完全合规的样本：故意照着「提示词要求」写，用来反证解析层认它
COMPLIANT_DUT = """module dut #(parameter W = 8) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          en,
    input  wire [W-1:0]  a,
    input  wire [W-1:0]  b,
    output reg  [W:0]    sum
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)      sum <= {(W+1){1'b0}};
        else if (en)     sum <= a + b;
    end
endmodule
"""

COMPLIANT_TB = f"""`timescale 1ns/1ps
module tb_dut;
    reg          clk = 0;
    reg          rst_n = 0;
    reg          en = 0;
    reg  [7:0]   a = 0;
    reg  [7:0]   b = 0;
    wire [8:0]   sum;
    integer errors = 0;
    integer checks = 0;

    dut #(.W(8)) u_dut (.clk(clk), .rst_n(rst_n), .en(en), .a(a), .b(b), .sum(sum));

    always #5 clk = ~clk;

    task check(input [7:0] ta, input [7:0] tb_, input [8:0] want);
        begin
            a = ta; b = tb_; en = 1;
            @(posedge clk); #1;
            checks = checks + 1;
            if (sum !== want) begin
                errors = errors + 1;
                $display("FAIL: %0d + %0d = %0d, expected %0d", ta, tb_, sum, want);
            end else begin
                $display("ok: %0d + %0d = %0d", ta, tb_, sum);
            end
        end
    endtask

    initial begin
        $dumpfile("{QPm.VCD_FILE}");
        $dumpvars(0, tb_dut);
        #12 rst_n = 1;
        check(8'd0,   8'd0,   9'd0);
        check(8'd1,   8'd1,   9'd2);
        check(8'd127, 8'd1,   9'd128);
        check(8'd255, 8'd1,   9'd256);
        check(8'd255, 8'd255, 9'd510);
        en = 0; a = 8'd9; b = 8'd9;
        @(posedge clk); #1;
        checks = checks + 1;
        if (sum !== 9'd510) errors = errors + 1;
        $display("checks = %0d", checks);
        if (errors == 0) $display("{QPm.MARK_PASS}");
        else             $display("{QPm.MARK_FAIL}");
        $finish;
    end
endmodule
"""


def codes(text: str) -> list[str]:
    _, iss = QP.parse_response(text, compile_check=False)
    return [i.code for i in iss]


def main() -> int:
    print("=" * 70)
    print("提示词契约 ↔ 解析层校验  对齐测试")
    print("=" * 70)

    # ---------------------------------------------------------- A. 基线
    print("\n【A】照提示词写的样本，必须零问题通过")
    text = json.dumps({"dut": COMPLIANT_DUT, "tb": COMPLIANT_TB,
                       "top": "dut", "notes": "x"}, ensure_ascii=False)
    files, issues = QP.parse_response(text, compile_check=False)
    check("产出两个文件", set(files) == {QPm.DUT_FILE, QPm.TB_FILE}, str(set(files)))
    check("没有任何 error 或 warn", not issues, QP.format_issues(issues))

    # ---------------------------------------------------------- B. 逐条对齐
    print("\n【B】提示词要求的每一条，解析层都真的会检查")
    print("     （做法：把该条从合规样本里拿掉，看解析层是否报出对应错误码）")

    def verify(needle, code, mutate, prompt_needle=None):
        broken = mutate(COMPLIANT_TB, COMPLIANT_DUT)
        got = codes(json.dumps(broken, ensure_ascii=False))
        hit = any(c == code or c.startswith(code) for c in got)
        # 顺带确认：提示词里确实写了这条要求。
        # 注意要用提示词里真实存在的写法去搜 —— 用中文意译去搜必然搜不到。
        probe = prompt_needle if prompt_needle is not None else needle
        in_prompt = probe in QPm.SYSTEM_PROMPT or probe in (
            QPm.VCD_FILE + QPm.DUT_FILE + QPm.TB_FILE)
        check(f"{needle:26} → {code}", hit and in_prompt,
              f"解析层命中={hit}(codes={got}) 提示词提及={in_prompt}(搜的是 {probe!r})")

    verify("$dumpfile", "tb/no-dumpfile",
           lambda tb, dut: {"dut": dut, "tb": tb.replace(f'$dumpfile("{QPm.VCD_FILE}");', "")})
    verify("$dumpvars", "tb/no-dumpvars",
           lambda tb, dut: {"dut": dut, "tb": tb.replace("$dumpvars(0, tb_dut);", "")})
    verify(QPm.MARK_PASS, "tb/no-pass-marker",
           lambda tb, dut: {"dut": dut, "tb": tb.replace(QPm.MARK_PASS, "PASSED")})
    verify(QPm.MARK_FAIL, "tb/no-fail-marker",
           lambda tb, dut: {"dut": dut, "tb": tb.replace(QPm.MARK_FAIL, "FAILED")})
    verify("task check", "tb/no-check-task",
           lambda tb, dut: {"dut": dut, "tb": tb.replace("task check(", "// 没有 task 了(")},
           prompt_needle="task check")
    verify("integer errors", "tb/no-errors-var",
           lambda tb, dut: {"dut": dut, "tb": tb.replace("integer errors", "integer errs")
                                              .replace("errors = errors", "errs = errs")
                                              .replace("errors == 0", "errs == 0")},
           prompt_needle="integer errors")
    verify("integer checks", "tb/no-checks-var",
           lambda tb, dut: {"dut": dut, "tb": tb.replace("integer checks", "integer cnt")
                                              .replace("checks = checks", "cnt = cnt")},
           prompt_needle="integer checks")
    verify("$finish", "tb/no-finish",
           lambda tb, dut: {"dut": dut, "tb": tb.replace("$finish;", "")})
    verify("例化 dut", "tb/not-instantiate",
           lambda tb, dut: {"dut": dut, "tb": tb.replace("dut #(.W(8)) u_dut", "other u_x")},
           prompt_needle="实例化")
    verify(QPm.VCD_FILE, "tb/dumpfile-name",
           lambda tb, dut: {"dut": dut, "tb": tb.replace(QPm.VCD_FILE, "wave.vcd")})
    verify("module", "dut/no-module",
           lambda tb, dut: {"dut": "这不是 Verilog", "tb": tb})
    verify("endmodule", "dut/no-endmodule",
           lambda tb, dut: {"dut": dut.replace("endmodule", ""), "tb": tb})

    # ---------------------------------------------------------- C. 反向对齐
    print("\n【C】解析层报的每个错误码，提示词里都得有对应要求")
    contract_codes = QPm.all_contract_codes()

    # 收集解析层会报的所有错误码（含 warn），用各种破坏方式触发
    observed = set()
    samples = [
        json.dumps({"dut": "x"}),                                   # 缺 tb
        json.dumps({"tb": COMPLIANT_TB}),                           # 缺 dut
        "完全不是 JSON",                                             # 无 JSON
        json.dumps({"dut": COMPLIANT_DUT, "tb": COMPLIANT_TB}),      # 正常
    ]
    for s in samples:
        observed |= set(codes(s))
    # 再把 B 组那些破坏方式的结果并进来
    mutations = [
        lambda tb, dut: {"dut": dut, "tb": tb.replace(f'$dumpfile("{QPm.VCD_FILE}");', "")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace("$dumpvars(0, tb_dut);", "")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace(QPm.MARK_PASS, "P")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace(QPm.MARK_FAIL, "F")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace("task check", "//")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace("integer errors", "integer e")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace("integer checks", "integer c")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace("$finish;", "")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace("dut #(.W(8)) u_dut", "o u_x")},
        lambda tb, dut: {"dut": dut, "tb": tb.replace(QPm.VCD_FILE, "w.vcd")},
        lambda tb, dut: {"dut": "nope", "tb": tb},
        lambda tb, dut: {"dut": dut.replace("endmodule", ""), "tb": tb},
        lambda tb, dut: {"dut": dut.replace("endmodule", 'initial $dumpfile("z.vcd");\nendmodule'), "tb": tb},
    ]
    for m in mutations:
        observed |= set(codes(json.dumps(m(COMPLIANT_TB, COMPLIANT_DUT), ensure_ascii=False)))

    # 非 Verilog 检查类的错误码不需要提示词条款（它们是流程错误，不是模型违规）
    flow_prefixes = ("json/", "field/", "dut/empty", "tb/empty", "dut/multi-module")
    unexplained = {c for c in observed
                   if not c.startswith(flow_prefixes) and c not in contract_codes}
    check("没有「解析层会报但提示词没要求」的错误码",
          not unexplained, f"未覆盖：{sorted(unexplained)}")

    uncovered = {c for c in contract_codes if c not in observed}
    check("契约表里每个错误码都真的能被触发（没有死条款）",
          not uncovered, f"从未触发：{sorted(uncovered)}")

    # ---------------------------------------------------------- D. 条款覆盖
    print("\n【D】提示词里不允许出现「无害但会误导」的内容")
    problems = QPm.self_check()
    check("提示词自检通过", not problems, "; ".join(problems))

    # 关键：不能让模型以为可以无条件打印 PASSED
    check("提示词明确禁止无条件打印 PASSED",
          "不许无条件打印 PASSED" in QPm.SYSTEM_PROMPT or "errors == 0" in QPm.SYSTEM_PROMPT)
    check("提示词要求覆盖边界值", "边界值" in QPm.SYSTEM_PROMPT)
    check("提示词强调可综合（不许 initial/#delay）",
          "可综合" in QPm.SYSTEM_PROMPT and "不要用 initial" in QPm.SYSTEM_PROMPT)

    # ---------------------------------------------------------- E. 回喂提示词
    print("\n【E】第 3 步要用的「失败回喂」提示词")
    fb = QPm.build_user_prompt("8位加法器", feedback="第 3 行报错了", round_no=2)
    check("回喂里带上了需求", "8位加法器" in fb)
    check("回喂里带上了报错", "第 3 行报错了" in fb)
    check("回喂里带上了轮次", "第 2 轮" in fb)
    check("回喂要求重出完整文件（不要 diff）", "不要只给 diff" in fb)
    fb0 = QPm.build_user_prompt("8位加法器")
    check("第一轮不带回喂段落", "上一轮失败了" not in fb0)

    rp = QPm.build_repair_prompt("这是模型的一段非 JSON 回答")
    check("格式修复提示词要求只做转换", "只把它转换" in rp)

    # ---------------------------------------------------------- 汇总
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
