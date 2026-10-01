#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 2：提示词契约。

设计原则：**这里写的每条规则，都必须和 quench_parse.py 的校验一一对应。**
提示词要求什么，解析层就检查什么；解析层检查什么，提示词就必须要求什么。
两边不对齐的话，会出现「模型觉得合规、解析层却拦住」的死循环 —— 第 3 步
的自动重试会一直重试同一个错误，永远修不好。

对应关系：
  提示词条款                解析层错误码
  ─────────────────────    ──────────────────────
  必须 $dumpfile            tb/no-dumpfile
  必须 $dumpvars            tb/no-dumpvars
  必须打印 === TEST PASSED ===   tb/no-pass-marker
  必须打印 === TEST FAILED ===   tb/no-fail-marker
  必须用 task 逐条比对       tb/no-check-task
  必须有 errors 计数        tb/no-errors-var
  必须有 checks 计数        tb/no-checks-var
  必须 $finish              tb/no-finish
  必须实例化被测试设计       tb/not-instantiate
  dut 必须是合法 module      dut/no-module / dut/no-endmodule
  测试代码不许混进 dut       dut/contains:$dumpfile 等
"""
from __future__ import annotations

import json

MARK_PASS = "=== TEST PASSED ==="
MARK_FAIL = "=== TEST FAILED ==="

# 生成结果里约定的文件名。解析层用的是同一组（quench_parse.DUT_NAME / TB_NAME）。
DUT_FILE = "dut.v"
TB_FILE = "tb_dut.v"
VCD_FILE = "dut.vcd"

# ------------------------------------------------------------------ 系统提示词
SYSTEM_PROMPT = f"""你是一名严谨的 Verilog 验证工程师。你的输出会被一个自动化工具直接落盘、
送进 iverilog / verilator / yosys 真实执行，所以**任何格式偏差都会导致整条流水线失败**。

## 输出格式（最重要，先看这条）

只输出一个 JSON 对象，不要 markdown 围栏，不要任何解释文字：

{{"dut": "<dut.v 的完整内容>", "tb": "<tb_dut.v 的完整内容>", "top": "<dut 里的顶层模块名>", "notes": "<一句话说明你的设计取舍>"}}

JSON 字符串里换行必须写成 \\n。不要输出 {{"files": ...}} 之外的任何包装。

## 文件 1：{DUT_FILE} —— 被测设计

- 必须是**可综合**的 RTL，不要用 initial 块、延时 `#10`、`$display` 这些仿真专用语法
- 顶层模块名就叫 `dut`
- 端口用 ANSI 风格声明：`module dut(input wire clk, ...);`
- 需要参数化时用 `#(parameter W = 8)`
- 不要写测试代码，测试代码属于文件 2

## 文件 2：{TB_FILE} —— 测试平台

**以下每一条都是硬性要求，缺一条整个流程就会失败：**

1. 第一行写 `` `timescale 1ns/1ps ``
2. 模块名 `tb_dut`
3. 必须实例化文件 1 里的 `dut` 模块（名字必须出现在测试平台里）
4. 必须产生波形，且文件名**必须正好是 `{VCD_FILE}`**：
   ```
   $dumpfile("{VCD_FILE}");
   $dumpvars(0, tb_dut);
   ```
5. 时钟用 `always #5 clk = ~clk;`（100 MHz）
6. **必须定义 `task check(...)` 做逐条比对** —— 这是保证测试真实有效的关键：
   ```
   task check(input [W-1:0] ta, input [W-1:0] tb_, input [W:0] want);
       begin
           a = ta; b = tb_;
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
   ```
7. **必须声明这两个计数器**：`integer errors = 0;` 和 `integer checks = 0;`
8. 测试结束前**必须**打印检查点数量：`$display("checks = %0d", checks);`
9. 最后必须**恰好**打印下面两行之一（**等号数量和空格都不能变**，工具靠字符串精确匹配来判定成败）：
   - 全部通过时：`$display("{MARK_PASS}");`
   - 有失败时：  `$display("{MARK_FAIL}");`
   判定条件必须是 `if (errors == 0)`，**不许无条件打印 PASSED**
10. 测试结束时调用 `$finish`

## 测试要真的覆盖

不要只测一个用例就收工。至少覆盖：
- 复位行为（复位后输出应为确定值）
- 典型值（中间范围）
- **边界值**（最小、最大、以及会进位/溢出的那组）
- 若有时序要求，测**保持**或**使能**行为

## 常见坑（请主动避开）

- **绝对不要给 `wire` 赋值。** DUT 的输出端口在测试平台里接的是 `wire`，
  只能读、不能写。想测边界值（比如"255 加 1 回绕到 0"），
  必须**通过合法输入一步一步驱动过去**（比如先复位、再使能计数若干拍），
  或者给设计加一个 `load`/预置端口。
  ❌ `q = 8'd254;`（q 是 wire，编译直接报 `not a valid l-value`）
  ✅ 通过 `en` 连续计数把它推到目标值，再用 `check()` 验证
- **不要用 `force`/`release` 去改 DUT 内部信号**，那会让测试失去意义
- 位宽不匹配：比较时用 `!==` 而不是 `!=`，并保证两边位宽一致（如 `if (sum !== want)`）
- 输入输出命名冲突（如端口的 `tb` 和测试平台的 `tb_`）
- 忘记 `endmodule`
- 组合逻辑里用了阻塞/非阻塞赋值搞混
- 忘记声明 wire/reg
- 测试平台里给 `reg` 型信号做连续赋值（`assign`）

> 生成的代码会被 `iverilog` **真实编译一遍**。编译不过的代码会被直接退回重做，
> 所以请确保语法和赋值语义都正确。

## 这段代码的要求

用户会用中文描述需求。请把描述里的**每一个**功能点都实现并测到。
如果描述有歧义，选最合理的解释，并在 `notes` 里说明你选了什么。
"""


def build_user_prompt(requirement: str, feedback: str = "", round_no: int = 1) -> str:
    """拼出用户消息。

    feedback 用于第 3 步的「失败回喂」——把上一轮的报错和上一轮源码塞回去。
    现在先把参数留好，第 3 步直接接上。
    """
    parts = [f"## 需求（第 {round_no} 轮）\n\n{requirement.strip()}"]
    if feedback:
        parts.append(
            "\n## 上一轮失败了，请针对下面的问题修正\n\n"
            f"{feedback.strip()}\n\n"
            "请重新输出完整的两个文件（不要只给 diff 或补丁）。"
        )
    return "\n".join(parts)


def build_repair_prompt(raw_reply: str) -> str:
    """当模型没按 JSON 格式回答时，让它只做格式转换。

    单独走一次便宜的小请求，比让它重新生成一遍代码划算得多。
    """
    return (
        "下面是一次回答，但格式不对。请**只把它转换成约定的 JSON 对象**，"
        "不要改动代码内容，不要加任何解释：\n\n"
        f"```\n{raw_reply[:12000]}\n```\n\n"
        f'输出格式：{{"dut": "...", "tb": "...", "top": "...", "notes": "..."}}'
    )


def prompt_contract_codes() -> dict:
    """给测试用：提示词里的每条硬性要求 → 它对应的解析层错误码。

    这张表是「两边对齐」这件事的可执行定义 —— 测试会逐条验证，
    提示词要求的东西解析层真的会检查，反之亦然。

    一个要求可能同时约束两个文件（比如 $dumpfile 出现在 dut 里也是问题），
    所以值是「错误码集合」。
    """
    return {
        "$dumpfile": {"tb/no-dumpfile", "dut/contains:$dumpfile"},
        "$dumpvars": {"tb/no-dumpvars"},
        MARK_PASS: {"tb/no-pass-marker"},
        MARK_FAIL: {"tb/no-fail-marker"},
        "task check": {"tb/no-check-task"},
        "integer errors": {"tb/no-errors-var"},
        "integer checks": {"tb/no-checks-var"},
        "$finish": {"tb/no-finish"},
        "实例化": {"tb/not-instantiate"},
        VCD_FILE: {"tb/dumpfile-name"},
        "module": {"dut/no-module"},
        "endmodule": {"dut/no-endmodule"},
    }


def all_contract_codes() -> set:
    """契约表覆盖到的全部错误码（拍平）。"""
    out = set()
    for v in prompt_contract_codes().values():
        out |= v if isinstance(v, (set, list, tuple)) else {v}
    return out


def self_check() -> list[str]:
    """检查提示词本身有没有明显毛病（发给模型之前先自检）。"""
    problems = []
    for needle in ("$dumpfile", "$dumpvars", MARK_PASS, MARK_FAIL,
                   "task check", "integer errors", "integer checks",
                   "$finish", VCD_FILE, DUT_FILE, TB_FILE):
        if needle not in SYSTEM_PROMPT:
            problems.append(f"提示词里缺少硬性要求：{needle}")
    if "```" in SYSTEM_PROMPT.split("## 输出格式")[1].split("## 文件 1")[0]:
        problems.append("输出格式那一段里出现了 markdown 围栏，会诱导模型也带围栏")
    # JSON 示例必须真的能解析
    demo = '{"dut": "<dut.v 的完整内容>", "tb": "<tb_dut.v 的完整内容>", "top": "<dut 里的顶层模块名>", "notes": "<一句话说明你的设计取舍>"}'
    try:
        json.loads(demo)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"输出格式示例不是合法 JSON：{exc}")
    return problems


if __name__ == "__main__":
    issues = self_check()
    if issues:
        print("提示词自检未通过：")
        for i in issues:
            print("  ✗", i)
        raise SystemExit(1)
    print("提示词自检通过")
    print(f"  系统提示词长度：{len(SYSTEM_PROMPT)} 字符")
    print(f"  硬性要求条数  ：{len(prompt_contract_codes())}")
