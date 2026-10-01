#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 11：8 类错误分类器。

截图要求的 8 类：
    语法 / 未绑定 / 位宽 / 锁存器 / 不可综合 / 功能不符 / 超时 / testbench 自身错误

设计原则：
  ① **先看输出，再看退出码。** 第 1 步已经证明 wsl.exe 不转发 EDA 工具的退出码，
     判断成败只能看文本 —— 分类同理。
  ② **按优先级匹配，取第一个命中的。** 一条输出里往往同时有根因和连带报错
     （比如"未声明 → 后面一串语法错"），必须先认根因，否则统计出来的
     "错误类型分布"全是噪声。
  ③ **纯函数、可解释。** 每个判定都要能说出"凭哪一行判的"，否则第 5 步
     界面没法给用户解释。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 8 个类别
CATEGORIES = {
    "syntax":        "语法错误",
    "unbound":       "未绑定/未声明",
    "width":         "位宽不匹配",
    "latch":         "推断出锁存器",
    "unsynth":       "不可综合",
    "functional":    "功能不符",
    "timeout":       "超时",
    "testbench":     "testbench 自身错误",
}
UNKNOWN = "unknown"
CATEGORIES_ALL = dict(CATEGORIES)
CATEGORIES_ALL[UNKNOWN] = "未分类"

# 优先级：**从"最像根因/最像结论"到"最像过程噪声"**。
#
# 排序依据（全部来自真实输出验证）：
#   1. timeout     —— 最硬的中断
#   2. functional  —— **仿真已经给出结论**（TEST FAILED / FAIL:），
#                     这是最终答案，必须压过下面所有过程性警告。
#                     实测踩到：iverilog 顺带打印一句 "Numeric constant truncated"
#                     就把 functional 抢走了，报告里于是显示"位宽问题"而不是"功能不对"。
#   3. testbench   —— 测试平台自身写错了
#   4. unbound     —— 未声明（后面会跟一串语法错，必须抢在 syntax 前）
#   5. latch       —— 只在**警告行或综合出的单元名**上匹配，不匹配 yosys 的 pass 日志
#   6. width
#   7. unsynth
#   8. syntax      —— 最泛，放最后兜底
PRIORITY = ["timeout", "functional", "testbench", "unbound",
            "latch", "width", "unsynth", "syntax"]

# ---------------------------------------------------------------- 规则表
# 每条：(正则, 面向用户的解释模板)
RULES: dict[str, list[tuple[re.Pattern, str]]] = {

    "timeout": [
        (re.compile(r"命令超时|timed?\s*out|TimeoutExpired|探测超时", re.I),
         "工具运行超过设定的时间上限"),
    ],

    # testbench 自身的问题：给 wire 赋值、端口接错、参数丢了、忘了例化
    "testbench": [
        (re.compile(r"is not a valid l-value", re.I),
         "给 wire 型信号赋值了（DUT 的输出在测试平台里是 wire，只能读不能写）"),
        (re.compile(r"is not a port of", re.I),
         "例化时接了一个不存在的端口名"),
        (re.compile(r"parameter\s+`?\w+`?\s+not found", re.I),
         "例化时覆盖了 DUT 里不存在的参数"),
        (re.compile(r"too many|too few|Wrong number of ports|"
                    r"expects?\s+\d+\s+port", re.I),
         "例化的端口数量对不上"),
        (re.compile(r"\$dumpfile|\$dumpvars", re.I),
         "波形导出语句有问题（$dumpfile / $dumpvars）"),
        (re.compile(r"no top level modules", re.I),
         "没有找到顶层模块（模块名或文件名对不上）"),
    ],

    # 未声明 / 未绑定：最像根因，优先于 syntax
    "unbound": [
        (re.compile(r"undeclared|not declared|Unknown identifier|"
                    r"implicitly declared|未声明", re.I),
         "用了没有声明的标识符"),
        (re.compile(r"Unable to bind|cannot find|not found in", re.I),
         "绑定不到对应的模块或信号"),
        (re.compile(r"Unknown module type|module\s+\S+\s+not found", re.I),
         "例化了一个不存在的模块"),
    ],

    # 锁存器：verilator 的 LATCH 警告，或 yosys 统计块里的 latch 单元。
    #
    # ⚠️ 这里踩过一个坑：一开始用 `LATCH|inferre?d?\s+latch` 裸匹配，
    # 结果 yosys 的**流程日志** `Executing PROC_DLATCH pass` 也被匹配上 ——
    # 那是每次综合都会打印的东西，导致"干净设计"被误判成锁存器。
    # 所以必须限定：要么是带 %Warning 前缀的告警行，要么是统计块里的单元名。
    "latch": [
        (re.compile(r"%Warning[-\w]*LATCH|Latch\s+inferred", re.I),
         "组合逻辑分支不完备，verilator 推断出了锁存器"),
        (re.compile(r"(?m)^\s*\$_DLATCH\w*\s+\d+", re.I),
         "综合结果里出现了锁存器单元（$_DLATCH…）—— 组合逻辑分支不完备"),
        (re.compile(r"(?m)^\s*\$_SR\w*\s+\d+", re.I),
         "综合结果里出现了锁存器单元（$_SR…）—— 组合逻辑分支不完备"),
        (re.compile(r"Warning[-\w]*:\s*Latch", re.I),
         "工具推断出了锁存器"),
    ],

    # 位宽：只能用**真正的告警格式**匹配。
    #
    # ⚠️ 和 latch 一样踩过坑：一开始裸匹配 `WIDTH`，结果 yosys 的模板日志
    # `Using template $paramod\...\WIDTH=32'000...` 也被匹配上 ——
    # WIDTH 出现在变量名里，根本不是告警。干净设计因此被误判成位宽问题。
    # 所以必须限定：`%Warning-WIDTH` 前缀，或 iverilog 的 "truncated" 之类明确措辞。
    "width": [
        (re.compile(r"%Warning[-\w]*WIDTH", re.I),
         "赋值或比较的两边位宽不一致"),
        (re.compile(r"WIDTH\s*:\s*\d+|width\s+mismatch", re.I),
         "赋值或比较的两边位宽不一致"),
        (re.compile(r"truncat(ed|ing)\s+to\s+\d+\s+bits?", re.I),
         "常量或表达式被截断到更少的位"),
        (re.compile(r"%Warning[-\w]*(SELRANGE|RANGE)", re.I),
         "位选范围越界"),
    ],

    # 不可综合：规则全部来自**实测的 yosys 真实输出**。
    # 注意 `initial` + 延时**不算** —— yosys 会静默把它丢掉，不报错
    # （我一开始就是这么误判的，拿它当样本结果分类器"失败"，
    #   其实是样本本身不产生任何错误输出）。
    "unsynth": [
        (re.compile(r"While loops are only allowed in constant functions", re.I),
         "综合时遇到了非常量边界的 while 循环 —— 不可综合"),
        (re.compile(r"found logic loop", re.I),
         "综合时发现组合逻辑环 —— 不可综合"),
        (re.compile(r"unsynthesizable|不可综合|not synthesizable", re.I),
         "用了不可综合的语法"),
        (re.compile(r"is not supported by yosys|not supported in synthesis", re.I),
         "yosys 不支持这个语法"),
        (re.compile(r"ERROR:\s*(?:syntax error, unexpected TOK_REAL|"
                    r"Unsupported|Cannot)", re.I),
         "综合器不支持这个构造"),
        (re.compile(r"PROCASSIGN|SYNTH_\d+", re.I),
         "综合过程中出现问题（过程赋值）"),
        (re.compile(r"(?m)^\s*\$_DLATCH\w*\s+\d+", re.I),
         "综合结果里出现了锁存器单元"),
    ],

    "functional": [
        (re.compile(r"TEST FAILED", re.I),
         "仿真跑完了，但断言没过 —— 设计的功能和需求不符"),
        (re.compile(r"^\s*FAIL:", re.M),
         "有检查点不通过"),
        (re.compile(r"errors\s*=\s*[1-9]\d*", re.I),
         "错误计数大于 0"),
    ],

    "syntax": [
        (re.compile(r"syntax error", re.I),
         "语法错误（多半是少分号、括号不配对、关键字拼错）"),
        (re.compile(r"unexpected\s+\S+", re.I),
         "出现了不该出现的符号"),
        (re.compile(r"expected\s+\S+", re.I),
         "缺少了应有的符号"),
        (re.compile(r"error:\s", re.I),
         "iverilog 报错（未细分）"),
    ],
}

# 只有这些行才值得拿去分类 —— 避免把无关输出当报错
_NOISE = re.compile(
    r"(?i)^(\s*$|VCD info|warning:\s*Some design elements|"
    r":\s*time precision|:\s*Affected design|:\s*--\s*module|"
    r"\$finish called|checks\s*=|ok:|----|==========)")


def candidate_lines(text: str) -> list[str]:
    """挑出可能带报错的行（去掉明显无关的噪声）。"""
    out = []
    for raw in (text or "").replace("\r\n", "\n").splitlines():
        s = raw.strip()
        if not s or _NOISE.match(s):
            continue
        out.append(s)
    return out


def classify(text: str) -> tuple[str, str, str]:
    """给一段工具输出分类。

    返回 (类别, 面向用户的解释, 判据那一行)。
    没命中任何规则时返回 ("unknown", "", "")。
    """
    lines = candidate_lines(text)
    if not lines:
        return UNKNOWN, "", ""

    # 按优先级逐类扫描：**先看高优先级的整段，再看低优先级**，
    # 这样"未声明 + 后续语法错"会被正确归到 unbound。
    for cat in PRIORITY:
        for pat, why in RULES.get(cat, []):
            for line in lines:
                if pat.search(line):
                    return cat, why, line[:200]
    return UNKNOWN, "", ""


def classify_details(text: str, limit: int = 5) -> dict:
    """更详细的分类结果：主类别 + 所有命中的类别 + 证据行。"""
    lines = candidate_lines(text)
    hits: dict[str, list[str]] = {}
    for cat in PRIORITY:
        for pat, _why in RULES.get(cat, []):
            for line in lines:
                if pat.search(line):
                    hits.setdefault(cat, [])
                    if line[:200] not in hits[cat]:
                        hits[cat].append(line[:200])
    primary = next((c for c in PRIORITY if c in hits), UNKNOWN)
    why = ""
    if primary in CATEGORIES:
        for pat, w in RULES[primary]:
            if hits.get(primary) and any(pat.search(h) for h in hits[primary]):
                why = w
                break
    return {
        "category": primary,
        "label": CATEGORIES_ALL.get(primary, primary),
        "why": why,
        "evidence": hits.get(primary, [])[:limit],
        "alsoMatched": [c for c in hits if c != primary],
    }


def classify_counts(texts: list[str]) -> dict[str, int]:
    """给一批输出做分类统计（第 4 步报告要用）。"""
    counts = {c: 0 for c in CATEGORIES_ALL}
    for t in texts:
        cat, _, _ = classify(t)
        counts[cat] = counts.get(cat, 0) + 1
    return counts


if __name__ == "__main__":
    # 自查：拿真实踩到过的报错逐条验证
    samples = [
        ("给 wire 赋值",
         "tb_dut.v:67: error: q is not a valid l-value in tb_dut.\n"
         "tb_dut.v:8:      : q is declared here as wire.\n"
         "1 error(s) during elaboration.", "testbench"),
        ("文件名与模块名不符",
         "%Warning-DECLFILENAME: dut.v:1:8: Filename 'dut' does not match "
         "MODULE name: 'my_counter'", "unknown"),
        ("参数丢了",
         "tb_dut.v:9: error: parameter `W` not found in `tb_dut.u_dut`.",
         "testbench"),
        ("端口不符",
         "tb_dut.v:9: error: port ``clk'' is not a port of u_dut.",
         "testbench"),
        ("未声明 + 连带语法错",
         "m.v:3: error: Unable to bind wire/reg/memory `foo' in `m'\n"
         "m.v:3: error: syntax error", "unbound"),
        ("功能不符",
         "FAIL: q=1 want=0\n=== TEST FAILED ===\nchecks = 3", "functional"),
        ("verilator 锁存器",
         "%Warning-LATCH: m.v:5:7: Latch inferred for signal 'q'",
         "latch"),
        ("位宽",
         "%Warning-WIDTH: m.v:4:12: Operator ASSIGN expects 8 bits, got 4",
         "width"),
        ("超时",
         "命令超时（240 秒）：yosys -p 'synth'", "timeout"),
        ("不可综合",
         "ERROR: Found unsynthesizable construct: initial block", "unsynth"),
        ("干净输出不该误报",
         "VCD info: dumpfile dut.vcd opened\nok: q=0\nchecks = 3\n"
         "=== TEST PASSED ===", "unknown"),
    ]
    ok = True
    for label, text, want in samples:
        got, why, ev = classify(text)
        good = (got == want)
        ok &= good
        print(f"  {'OK ' if good else 'BAD'} {label:20} -> {got:10} (期望 {want})")
        if not good:
            print(f"        判据行: {ev}")
        elif got != "unknown":
            print(f"        {why}")
    print()
    print("分类器自查：", "全部通过 ✓" if ok else "有失败 ✗")
    raise SystemExit(0 if ok else 1)
