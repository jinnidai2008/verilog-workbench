#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 4 步自查：用**真实工具输出**验证分类器（不是手写字符串）。

这是本项目一路踩坑得出的教训：手写的样本只能证明"我写的规则能匹配我写的字符串"，
证明不了"真实工具的输出能被正确识别"。

做法：真的调 iverilog / verilator / yosys 造出各类错误，把原始输出喂给分类器。

用法: python test_classify_real.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_classify as CL     # noqa: E402
import quench_compile as CC      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def run_in_wsl(files: dict[str, str], cmd: str, timeout: int = 90) -> str:
    """把文件写进临时目录，在 WSL 里跑命令，返回合并输出。"""
    with tempfile.TemporaryDirectory(prefix="clsreal_") as td:
        d = Path(td)
        for name, content in files.items():
            (d / name).write_text(content, encoding="utf-8")
        p = d.as_posix()
        wsl = "/mnt/" + p[0].lower() + p[2:]
        shell = f"cd '{wsl}' && {cmd}"
        try:
            r = subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "--", "bash", "-lc", shell],
                               capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"（执行失败：{exc}）"
        return ((r.stdout or "") + (r.stderr or "")).strip()


# ------------------------------------------------------------------ 样本设计
GOOD_HEAD = "`timescale 1ns/1ps\n"

TB_MIN = """module tb_dut;
    reg clk = 0; reg rst_n = 0;
    always #5 clk = ~clk;
    initial begin $dumpfile("dut.vcd"); $dumpvars(0, tb_dut); #20 $finish; end
endmodule
"""

# 1) 锁存器：组合逻辑分支不完备
LATCH_DUT = """module dut(input wire en, input wire [7:0] a, output reg [7:0] q);
    always @(*) begin
        if (en) q = a;
    end
endmodule
"""

# 2) 位宽：4 位赋给 8 位
WIDTH_DUT = """module dut(input wire [3:0] a, output reg [7:0] q);
    always @(*) q = a;
endmodule
"""

# 3) 未声明
UNBOUND_DUT = """module dut(input wire clk, output reg q);
    always @(posedge clk) q <= undefined_signal;
endmodule
"""

# 4) 语法错
SYNTAX_DUT = """module dut(input wire clk, output reg q)
    always @(posedge clk) q <= 1'b0;
endmodule
"""

# 5) 真的不可综合：非常量边界的 while 循环。
# ⚠️ 一开始我用「initial 块 + 延时」当不可综合的样本，结果发现
# **yosys 会静默把它丢掉、既不报错也不警告** —— 样本本身不产生错误输出，
# 分类器当然判不出东西。换成实测确认会报错的构造。
UNSYNTH_DUT = """module dut(input wire [7:0] n, output reg [7:0] s);
    integer i;
    always @(*) begin
        i = 0; s = 0;
        while (i < n) begin
            s = s + i[7:0];
            i = i + 1;
        end
    end
endmodule
"""

# 组合环：也是实测确认 yosys 会警告的
LOOP_DUT = """module dut(input wire a, output wire b);
    assign b = ~b & a;
endmodule
"""

# 6) testbench 给 wire 赋值
TB_WIRE_ASSIGN = TB_MIN.replace(
    "    initial begin",
    "    wire [3:0] q;\n    initial begin\n        q = 4'd9;")


def main() -> int:
    mode, desc = CC.find_iverilog()
    print("=" * 74)
    print("第 4 步自查：用真实工具输出验证分类器")
    print(f"iverilog：{mode} —— {desc}")
    print("=" * 74)
    if mode == "none":
        print("环境里没有 iverilog，无法做真实输出验证")
        return 0

    # ============================================================ A. 真编译错误
    print("\n【A】iverilog 真实编译错误")

    real_cases = [
        ("语法错误", {"dut.v": GOOD_HEAD + SYNTAX_DUT, "tb_dut.v": TB_MIN},
         "iverilog -g2012 -o x.vvp dut.v tb_dut.v", "syntax"),
        ("未声明信号", {"dut.v": GOOD_HEAD + UNBOUND_DUT, "tb_dut.v": TB_MIN},
         "iverilog -g2012 -o x.vvp dut.v tb_dut.v", "unbound"),
        ("给 wire 赋值", {"dut.v": GOOD_HEAD + WIDTH_DUT, "tb_dut.v": TB_WIRE_ASSIGN},
         "iverilog -g2012 -o x.vvp dut.v tb_dut.v", "testbench"),
    ]
    for label, files, cmd, want in real_cases:
        out = run_in_wsl(files, cmd)
        got, why, ev = CL.classify(out)
        check(f"{label:12} → {got}", got == want,
              f"期望 {want}\n      实际输出：{out[:260]}\n      判据行：{ev!r}")

    # ============================================================ B. verilator
    print("\n【B】verilator 真实警告")
    lint_cases = [
        ("锁存器", {"dut.v": GOOD_HEAD + LATCH_DUT},
         "verilator --lint-only -Wall dut.v", "latch"),
        ("位宽", {"dut.v": GOOD_HEAD + WIDTH_DUT},
         "verilator --lint-only -Wall dut.v", "width"),
        ("干净设计（不该误报）",
         {"dut.v": GOOD_HEAD + "module dut(input wire clk, output reg q);\n"
                               "  always @(posedge clk) q <= ~q;\nendmodule\n"},
         "verilator --lint-only -Wall dut.v", "unknown"),
    ]
    for label, files, cmd, want in lint_cases:
        out = run_in_wsl(files, cmd)
        got, why, ev = CL.classify(out)
        check(f"{label:20} → {got}", got == want,
              f"期望 {want}\n      实际输出：{out[:260]}\n      判据行：{ev!r}")
        if want != "unknown" and got == want:
            print(f"        {why}")

    # ============================================================ C. yosys
    print("\n【C】yosys 真实综合输出")
    yosys_cases = [
        ("锁存器（综合出 DLATCH）", GOOD_HEAD + LATCH_DUT,
         "yosys -p 'read_verilog dut.v; synth -top dut; stat'"),
        ("不可综合（非常量 while 循环）", GOOD_HEAD + UNSYNTH_DUT,
         "yosys -p 'read_verilog dut.v; synth -top dut; stat'"),
        ("组合逻辑环", GOOD_HEAD + LOOP_DUT,
         "yosys -p 'read_verilog dut.v; synth -top dut; stat'"),
        ("干净设计（不该误报）",
         GOOD_HEAD + "module dut(input wire clk, input wire rst_n, output reg [3:0] q);\n"
                     "  always @(posedge clk or negedge rst_n)\n"
                     "    if (!rst_n) q <= 0; else q <= q + 1'b1;\nendmodule\n",
         "yosys -p 'read_verilog dut.v; synth -top dut; stat'"),
    ]
    for label, dut, cmd in yosys_cases:
        out = run_in_wsl({"dut.v": dut}, cmd)
        got, why, ev = CL.classify(out)
        print(f"        {label:28} → {got:10} {why[:42] if why else ''}")
        if "干净" in label:
            check(f"{label}", got == "unknown", f"实际 {got}，判据行 {ev!r}")
        else:
            check(f"{label} 被分类（不是 unknown）", got != "unknown",
                  f"判据行 {ev!r}\n      输出片段：{out[:220]}")

    # ============================================================ D. 功能不符
    print("\n【D】功能不符（仿真跑完但断言没过）")
    fail_tb = """`timescale 1ns/1ps
module tb_dut;
    reg clk = 0; reg rst_n = 0;
    wire [3:0] q;
    integer errors = 0, checks = 0;
    dut u(.clk(clk), .rst_n(rst_n), .q(q));
    always #5 clk = ~clk;
    initial begin
        $dumpfile("dut.vcd"); $dumpvars(0, tb_dut);
        #12 rst_n = 1;
        #20; checks = checks + 1;
        if (q !== 4'd99) begin errors = errors + 1; $display("FAIL: q=%0d", q); end
        $display("checks = %0d", checks);
        if (errors == 0) $display("=== TEST PASSED ===");
        else $display("=== TEST FAILED ===");
        $finish;
    end
endmodule
"""
    good_dut = GOOD_HEAD + ("module dut(input wire clk, input wire rst_n, output reg [3:0] q);\n"
                            "  always @(posedge clk or negedge rst_n)\n"
                            "    if (!rst_n) q <= 0; else q <= q + 1'b1;\nendmodule\n")
    out = run_in_wsl({"dut.v": good_dut, "tb_dut.v": fail_tb},
                     "iverilog -g2012 -o x.vvp dut.v tb_dut.v && vvp x.vvp")
    got, why, ev = CL.classify(out)
    check(f"断言失败 → {got}", got == "functional",
          f"实际输出：{out[:260]}")
    if got == "functional":
        print(f"        {why}")

    # ============================================================ E. 不误报
    print("\n【E】正常输出绝不能误报")
    # 注意期望值要和设计实际行为对上：复位释放后第一个上升沿 q 就变成 1，
    # `@(posedge clk); #1;` 读到的是自增后的值 —— 所以这里期望 4'd1 会失败。
    # 用 4'd2 才能通过（这个坑第 3 步也踩过，时序要算准）。
    pass_tb = fail_tb.replace("if (q !== 4'd99)", "if (q !== 4'd2)")
    out_ok = run_in_wsl({"dut.v": good_dut, "tb_dut.v": pass_tb},
                        "iverilog -g2012 -o x.vvp dut.v tb_dut.v && vvp x.vvp")
    got2, _, ev2 = CL.classify(out_ok)
    check(f"TEST PASSED 的输出 → {got2}", got2 == "unknown",
          f"判据行 {ev2!r}\n      输出：{out_ok[:200]}")

    # ============================================================ 汇总
    print("\n" + "=" * 74)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:400]}")
    print("=" * 74)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
