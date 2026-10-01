#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
编译检查层（quench_compile.py）的测试。

会真的调用 iverilog（约 0.1 秒一次）。环境里没有 iverilog 时自动降级为跳过。

用法: python test_compile.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quench_compile as C  # noqa: E402
import quench_parse as P  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


GOOD_DUT = """module dut #(parameter W = 8) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         en,
    output reg  [W-1:0] q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)   q <= {W{1'b0}};
        else if (en)  q <= q + 1'b1;
    end
endmodule
"""

GOOD_TB = """`timescale 1ns/1ps
module tb_dut;
    reg        clk = 0;
    reg        rst_n = 0;
    reg        en = 0;
    wire [7:0] q;
    integer errors = 0;
    integer checks = 0;

    dut #(.W(8)) u_dut (.clk(clk), .rst_n(rst_n), .en(en), .q(q));

    always #5 clk = ~clk;

    task check(input [7:0] want);
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
        #12 rst_n = 1;
        en = 1;
        check(8'd0);
        check(8'd1);
        check(8'd2);
        $display("checks = %0d", checks);
        if (errors == 0) $display("=== TEST PASSED ===");
        else             $display("=== TEST FAILED ===");
        $finish;
    end
endmodule
"""

# 这就是真实踩到的那个坑：给 wire 赋值去"预置"边界条件
BAD_TB_WIRE_ASSIGN = GOOD_TB.replace(
    "        en = 1;\n",
    "        en = 1;\n        q = 8'd254;      // 非法：q 是 wire\n")


def main() -> int:
    print("=" * 70)
    print("编译检查层测试")
    print("=" * 70)

    mode, desc = C.find_iverilog()
    print(f"\niverilog 探测：{mode} —— {desc}")
    if mode == "none":
        print("  环境里没有 iverilog，本组测试只验证「优雅跳过」")
    offline = (mode == "none")

    # ---------------------------------------------------------- A. 合规代码
    print("\n【A】合规代码必须通过")
    r = C.compile_check(GOOD_DUT, GOOD_TB)
    if offline:
        check("合规代码 → 跳过而不是报失败", r.ok and r.skipped, r.summary())
    else:
        check("合规代码 → 编译通过", r.ok and not r.skipped, r.summary())
        check("耗时在合理范围（< 10 秒）", r.seconds < 10, f"{r.seconds:.2f}s")

    # ---------------------------------------------------------- B. 真实踩过的坑
    print("\n【B】给 wire 赋值（这是端到端实测踩到的真坑）")
    r2 = C.compile_check(GOOD_DUT, BAD_TB_WIRE_ASSIGN)
    if offline:
        check("跳过（无 iverilog）", r2.skipped)
    else:
        check("必须被判为失败", not r2.ok, r2.summary())
        check("错误信息里有 l-value 提示",
              any("l-value" in l for l in r2.error_lines()), str(r2.error_lines()[:3]))
        check("错误信息能指到具体行",
              any(":7:" in l or "tb_dut.v" in l for l in r2.error_lines()),
              str(r2.error_lines()[:3]))

    # ---------------------------------------------------------- C. 别的坏法
    print("\n【C】其他坏法也要抓到")
    cases = [
        ("语法错（少分号）", GOOD_DUT, GOOD_TB.replace("en = 1;", "en = 1")),
        ("模块名拼错（接不上）", GOOD_DUT, GOOD_TB.replace("dut #(.W(8))", "dutt #(.W(8))")),
        ("dut 本身语法错", GOOD_DUT.replace("endmodule", ""), GOOD_TB),
        ("端口名写错", GOOD_DUT, GOOD_TB.replace(".q(q)", ".qq(q)")),
    ]
    for label, dut, tb in cases:
        r = C.compile_check(dut, tb)
        if offline:
            check(f"{label} → 跳过", r.skipped)
        else:
            check(f"{label} → 判为失败", not r.ok, r.summary())

    # ---------------------------------------------------------- D. 与解析层联动
    print("\n【D】接进解析层后，编译失败要变成阻断性错误")
    import json
    good_reply = json.dumps({"dut": GOOD_DUT, "tb": GOOD_TB}, ensure_ascii=False)
    files, issues = P.parse_response(good_reply, compile_check=True)
    if offline:
        check("合规代码 → 只有 skip 提醒",
              bool(files) and not P.has_blocking_errors(issues), P.format_issues(issues))
    else:
        check("合规代码 → 零问题通过",
              bool(files) and not issues, P.format_issues(issues))

    bad_reply = json.dumps({"dut": GOOD_DUT, "tb": BAD_TB_WIRE_ASSIGN}, ensure_ascii=False)
    files2, issues2 = P.parse_response(bad_reply, compile_check=True)
    if offline:
        check("坏代码 → 跳过（无 iverilog 时无法判）", True)
    else:
        codes = [i.code for i in issues2]
        check("坏代码 → 报 compile/failed", "compile/failed" in codes, str(codes))
        check("坏代码 → 判定为阻断", P.has_blocking_errors(issues2), str(codes))
        check("文件仍然返回（留给失败回喂用）", set(files2) == {"dut.v", "tb_dut.v"},
              str(list(files2)))
        msg = P.format_issues([i for i in issues2 if i.code == "compile/failed"][:1])
        check("错误信息里带上了完整的 iverilog 报错", "l-value" in msg, msg[:200])

    # ---------------------------------------------------------- E. 优雅降级
    print("\n【E】环境没有工具链时要优雅跳过，不能假装成功也不能崩")
    r = C.compile_check(GOOD_DUT, GOOD_TB, mode="none")
    check("mode=none → skipped=True 且 ok=True（不算失败）", r.skipped and r.ok, r.summary())
    check("summary 说明了跳过原因", "跳过" in r.summary(), r.summary())

    r = C.compile_check(GOOD_DUT, GOOD_TB, mode="wsl", distro="NoSuchDistro-9.9")
    check("发行版写错 → 不崩（要么跳过要么失败）", isinstance(r.ok, bool), r.summary())

    # ---------------------------------------------------------- F. 错误行提取
    print("\n【F】error_lines() 要挑出有用的行给「失败回喂」用")
    fake = C.CompileResult(False, output=(
        "tb_dut.v:7: error: q is not a valid l-value in tb_dut.\n"
        "tb_dut.v:2:      : q is declared here as wire.\n"
        "1 error(s) during elaboration.\n"))
    lines = fake.error_lines()
    check("挑出了 error 行", any("l-value" in l for l in lines), str(lines))
    check("没把无关行全丢掉", len(lines) >= 2, str(lines))

    empty = C.CompileResult(False, output="")
    check("空输出时不崩", isinstance(empty.error_lines(), list))

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 70)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过"
          + ("  [离线降级模式]" if offline else ""))
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
