#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 9：「失败回喂」的结构化反馈。

第 3 步的心脏是「失败了要把问题讲清楚再让它重来」。
讲不清楚 = 它只能瞎改 = 烧你的 API 余额。

三条设计原则：

① **要截断。** 一轮探测日志可能上千行，5 轮全塞回去会撑爆上下文，
   而且模型在噪声里反而抓不住重点。这里只留：
   结论行 + 错误行 + 日志头部 + 日志尾部。

② **要说明"测试平台不许动"。** 这是护栏，必须在每一轮的文字里重复，
   不能只靠代码层面的事后检测 —— 事后检测只能"发现"，提前告知才能"避免"。

③ **要随轮次升级措辞。** 第 1 轮平和，第 3 轮开始点名批评，
   第 5 轮要求它换思路（"别再小修小补，重新设计"）。
   同一段话重复 5 次，模型会当耳旁风。
"""
from __future__ import annotations

MAX_FEEDBACK_CHARS = 6000
LOG_HEAD_LINES = 8
LOG_TAIL_LINES = 14


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """安全截断，返回 (结果, 是否被截断)。"""
    text = text or ""
    if len(text) <= limit:
        return text, False
    keep_head = limit * 2 // 3
    keep_tail = limit - keep_head - 40
    return (text[:keep_head]
            + f"\n…（此处省略 {len(text) - keep_head - keep_tail} 字符）…\n"
            + text[-keep_tail:]), True


def condense_log(log: str, head: int = LOG_HEAD_LINES,
                 tail: int = LOG_TAIL_LINES, max_chars: int = 2500) -> str:
    """把长日志压缩成「头 + 尾」，中间省略。"""
    lines = [l for l in (log or "").splitlines() if l.strip()]
    if not lines:
        return "(没有日志)"
    if len(lines) <= head + tail:
        body = "\n".join(lines)
    else:
        omitted = len(lines) - head - tail
        body = "\n".join(lines[:head] + [f"…（中间省略 {omitted} 行）…"] + lines[-tail:])
    out, _ = truncate_text(body, max_chars)
    return out


def _dedupe(lines: list[str]) -> list[str]:
    seen, out = set(), []
    for l in lines:
        s = l.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _escalation(round_no: int, max_rounds: int) -> str:
    """随轮次升级的措辞 —— 同一句话重复 5 次会被当耳旁风。"""
    left = max(0, max_rounds - round_no)
    if round_no <= 1:
        return (f"这是第 {round_no} 轮。请仔细看下面的报错，定位根因后再改，"
                f"不要只改表面症状。")
    if round_no == 2:
        return (f"这是第 {round_no} 轮，上一轮的修改**没有解决问题**。"
                f"说明你对根因的判断是错的，请换一个角度分析。")
    if left >= 1:
        return (f"这是第 {round_no} 轮，同一个问题已经反复出现。"
                f"**不要再小修小补** —— 请重新读一遍需求，从设计的正确性重新推一遍，"
                f"必要时整段重写。只剩 {left} 轮机会。")
    return (f"这是最后一轮（第 {round_no} 轮）。请**完全重新设计**这两个文件，"
            f"不要沿用之前任何有问题的写法。")


def build_feedback(*, round_no: int, max_rounds: int,
                   compile_output: str = "",
                   sim_log: str = "",
                   probe_log: str = "",
                   dut_src: str = "",
                   previous_dut: str = "",
                   failure_kind: str = "",
                   tb_hash: str = "",
                   repeated: int = 0) -> str:
    """拼出这一轮的回喂正文。会自己控制总长度。"""
    parts: list[str] = []

    parts.append(f"## 上一轮（第 {round_no} 轮）的结果\n")
    parts.append(_escalation(round_no, max_rounds))

    if repeated >= 2:
        parts.append(f"\n⚠️ 同一个错误已经连续出现 {repeated} 次。"
                     f"**你之前的方向是错的**，请彻底换思路。")

    if failure_kind:
        parts.append(f"\n失败类型：**{failure_kind}**")

    # ---- 1. 编译错误（最硬的信息，放最前）----
    if compile_output.strip():
        errs = _dedupe([
            l for l in compile_output.splitlines()
            if any(k in l.lower() for k in
                   ("error", "warning", "not a valid", "syntax", "undeclared",
                    "cannot", "mismatch", "not found"))
        ])
        body, cut = truncate_text("\n".join(errs[:20]) or compile_output, 1800)
        parts.append("\n### 编译报错（iverilog）\n\n```\n" + body + "\n```")
        if not errs:
            parts.append("\n（iverilog 没有给出可识别的错误行，请通读上面的输出）")

    # ---- 2. 仿真结论 ----
    if sim_log.strip():
        parts.append("\n### 仿真输出（尾部）\n\n```\n"
                     + condense_log(sim_log, head=6, tail=16, max_chars=1600)
                     + "\n```")

    # ---- 3. 探测日志 ----
    if probe_log.strip():
        parts.append("\n### 完整探测日志（压缩）\n\n```\n"
                     + condense_log(probe_log)
                     + "\n```")

    # ---- 4. 让它说清改了什么 ----
    if previous_dut.strip():
        body, _ = truncate_text(previous_dut, 2600)
        parts.append("\n### 你上一轮写的 dut.v（供参考，可以整段重写）\n\n"
                     "```verilog\n" + body + "\n```")

    # ---- 5. 护栏：测试平台不许动 ----
    parts.append(
        "\n## 重要约束\n\n"
        "1. **测试平台（tb）本轮的哈希被锁定了，改动它会被直接拒绝。**\n"
        f"   当前锁定的 tb 哈希：`{tb_hash}`\n"
        "   如果测试本身写错了，**不要偷偷改绿它**，请在 `notes` 里明确说明"
        "「测试平台的第 X 行有问题，因为……」，我会人工判断。\n"
        "2. 不要为了让报错消失而删掉测试用例、放宽断言、或改变比较条件。\n"
        "3. 修的是 **dut.v**（被测设计）。如果确实需要改 tb，"
        "也请照常输出 tb，我会检测并用哈希挡住。\n"
        "4. 仍然只输出那一个 JSON 对象，不要任何解释性文字。\n"
        "5. 在 `notes` 里用一句话说明：**根因是什么、你改了什么**。\n"
    )

    text = "\n".join(parts)
    text, was_cut = truncate_text(text, MAX_FEEDBACK_CHARS)
    if was_cut:
        text += "\n\n（反馈内容过长已截断，请聚焦上面靠前的报错）"
    return text


def build_tb_tamper_message(baseline_hash: str, new_hash: str,
                            notes: str = "") -> str:
    """测试平台被偷改时，用来生成下一轮的回喂。

    注意：**不把被改后的 tb 传下去**，强制它回到被锁定的版本。
    """
    msg = [
        "## ⛔ 你的修改被拒绝了\n",
        f"你**改动了测试平台**，而它在本轮是被锁定的。\n",
        f"- 锁定的哈希：`{baseline_hash}`",
        f"- 你提交的哈希：`{new_hash}`\n",
        "这是不允许的：测试是判据，不能为了让结果变绿而修改判据。\n",
        "**请重新输出，并且 tb 必须和锁定版本逐字符一致**（包括注释和空行）。",
        "如果你认为测试平台本身有错，请在 `notes` 里明确写出来，"
        "并**照原样保留 tb**，我会人工判断。\n",
    ]
    if notes:
        msg.append(f"（你上一轮的 notes：{notes[:400]}）")
    return "\n".join(msg)


if __name__ == "__main__":
    # 自查：长度控制和内容完整性
    long_log = "\n".join(f"[{i}] 这是第 {i} 行探测日志内容" for i in range(500))
    fb = build_feedback(
        round_no=3, max_rounds=5,
        compile_output="tb_dut.v:67: error: q is not a valid l-value in tb_dut.\n"
                       "tb_dut.v:8:      : q is declared here as wire.\n"
                       "1 error(s) during elaboration.",
        sim_log=long_log,
        probe_log=long_log,
        previous_dut="module dut; // 占位\n" * 100,
        failure_kind="编译失败",
        tb_hash="abc123def456",
        repeated=2,
    )
    print("回喂长度:", len(fb), "字符（上限", MAX_FEEDBACK_CHARS, "）")
    print("在限长之内:", len(fb) <= MAX_FEEDBACK_CHARS + 60)
    print()
    src = "module dut; // 占位\n" * 100
    print("压缩后的日志提到了省略:", "省略" in fb)
    print("包含编译报错:", "l-value" in fb)
    print("包含 tb 哈希:", "abc123def456" in fb)
    print("包含反偷改警告:", "哈希被锁定" in fb)
    print()
    print("=== 回喂前 40 行 ===")
    for l in fb.splitlines()[:40]:
        print("  ", l)
