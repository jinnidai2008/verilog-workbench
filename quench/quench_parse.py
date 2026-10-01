#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 3：把模型回复解析成两份 Verilog 源文件。

为什么先做这一层：模型最爱在这几个地方翻车，而它们都能用假数据反复测，不花 API 钱。
  · 把 JSON 包在 ```json 围栏里
  · 前后各写一段"好的，我来为你生成…"的客套话
  · JSON 里带尾随逗号
  · 字符串里塞了没转义的换行

对外只暴露一个函数：
    parse_response(text) -> (files: dict[str,str], issues: list[str])
"""
from __future__ import annotations

import json
import re

# ------------------------------------------------------------------ 常量
RESULT_PASS = "=== TEST PASSED ==="
RESULT_FAIL = "=== TEST FAILED ==="

DUT_NAME = "dut.v"
TB_NAME = "tb_dut.v"
VCD_NAME = "dut.vcd"


class ParseIssue:
    def __init__(self, level: str, code: str, message: str, fix: str = ""):
        self.level = level      # "error" | "warn"
        self.code = code
        self.message = message
        self.fix = fix

    def as_dict(self) -> dict:
        d = {"level": self.level, "code": self.code, "message": self.message}
        if self.fix:
            d["fix"] = self.fix
        return d

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.level} {self.code}: {self.message}>"


# ------------------------------------------------------------------ 第一步：取出 JSON
def strip_fences(text: str) -> str:
    """剥掉 ```json ... ``` 围栏。没有围栏就原样返回。"""
    t = text.strip()
    m = re.search(r"```[A-Za-z0-9_+-]*\s*\n(.*?)```", t, re.S)
    if m:
        return m.group(1).strip()
    # 只有开头围栏没结尾（模型被截断）时，去掉第一行围栏
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            return t[first_nl + 1:].strip()
    return t


def extract_json_object(text: str) -> str | None:
    """从任意文字里抠出最外层那个完整的 JSON 对象。

    用括号配对而不是正则 —— 正则在嵌套和字符串里的花括号上一定会翻车。
    配平时会跳过字符串内部，避免 "{" 这种字面量把计数搞乱。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def repair_json(raw: str) -> str:
    """修掉尾随逗号 —— 必须跳过字符串内部。

    一开始图省事写成 re.sub(r",(\\s*[}\\]])", ...)，结果会把
    $display("ok: {\\"nested\\": %0d}", ta); 这种字符串里的逗号也吃掉。
    Verilog 里括号逗号满地都是，所以这里老老实实逐字符走一遍。
    """
    out = []
    in_str = False
    escaped = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue

        if ch == ",":
            # 往后看：只有「空白 + } 或 ]」才算尾随逗号
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                i += 1          # 丢掉这个逗号
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def parse_json_blob(text: str):
    """尽最大努力从回复里解析出 dict。返回 (obj_or_None, issues)。"""
    issues: list[ParseIssue] = []
    candidate = extract_json_object(strip_fences(text))
    if candidate is None:
        issues.append(ParseIssue("error", "json/not-found",
                                 "回复里找不到 JSON 对象（没有 { 或者括号不配对）",
                                 "让模型严格按约定的 JSON 格式回答"))
        return None, issues

    for label, blob in (("原样", candidate), ("去掉尾随逗号后", repair_json(candidate))):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError as exc:
            last = exc
            continue
        if label != "原样":
            issues.append(ParseIssue("warn", "json/repaired", "JSON 有尾随逗号，已自动修掉"))
        if not isinstance(obj, dict):
            issues.append(ParseIssue("error", "json/not-object",
                                     f"JSON 最外层是 {type(obj).__name__}，应该是对象"))
            return None, issues
        return obj, issues

    issues.append(ParseIssue("error", "json/bad",
                             f"JSON 解析失败：{last.msg}（第 {last.lineno} 行）",
                             "多半是字符串里有没转义的换行或引号"))
    return None, issues


# ------------------------------------------------------------------ 第二步：Verilog 检查
def _module_names(src: str) -> list[str]:
    return re.findall(r"(?m)^\s*module\s+([A-Za-z_]\w*)", src)


def strip_comments(src: str) -> str:
    """去掉 // 和 /* */ 注释。

    做 Verilog 关键词检查前必须先去注释，否则：
      · 注释里写着的 `// $dumpfile 手滑删了` 会让检查误判为「有」
      · 把代码注释掉做实验时，检查还会当成代码还在
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def check_dut(src: str) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    if not src.strip():
        issues.append(ParseIssue("error", "dut/empty", "dut 内容为空"))
        return issues
    names = _module_names(src)
    if not names:
        issues.append(ParseIssue("error", "dut/no-module",
                                 "dut 里没有 module 声明",
                                 "Verilog 源文件必须以 module <名字> 开头"))
    if "endmodule" not in strip_comments(src):
        issues.append(ParseIssue("error", "dut/no-endmodule", "dut 里没有 endmodule"))
    # 设计文件里出现这些，说明模型把测试代码混进来了（先去掉注释再判断）
    code = strip_comments(src)
    for bad, why in (("$dumpfile", "波形导出属于测试平台"),
                     ("$finish", "$finish 属于测试平台"),
                     ("initial begin", "initial 块通常不该出现在可综合设计里")):
        if bad in code:
            issues.append(ParseIssue("warn", f"dut/contains:{bad}",
                                     f"dut 里出现了 {bad} —— {why}"))
    return issues


def check_tb(src: str, dut_module: str | None) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    if not src.strip():
        issues.append(ParseIssue("error", "tb/empty", "tb 内容为空"))
        return issues

    code = strip_comments(src)      # 先去掉注释再做关键词检查

    if not _module_names(src):
        issues.append(ParseIssue("error", "tb/no-module", "tb 里没有 module 声明"))

    # 硬性约定 A：必须产生波形
    if "$dumpfile" not in code:
        issues.append(ParseIssue("error", "tb/no-dumpfile",
                                 "tb 里没有 $dumpfile —— 约定要求必须导出波形",
                                 '加上 $dumpfile("dut.vcd");'))
    else:
        m = re.search(r'\$dumpfile\s*\(\s*"([^"]+)"', code)
        if m and m.group(1) != VCD_NAME:
            issues.append(ParseIssue("warn", "tb/dumpfile-name",
                                     f'$dumpfile 写的是 "{m.group(1)}"，'
                                     f'但 project.json 会记成 "{VCD_NAME}"',
                                     "两边要一致，否则第 4 步找不到波形"))
    if "$dumpvars" not in code:
        issues.append(ParseIssue("error", "tb/no-dumpvars",
                                 "tb 里没有 $dumpvars —— 波形里不会有信号"))

    # 硬性约定 B：必须打印固定格式的结论
    has_pass = RESULT_PASS in code
    has_fail = RESULT_FAIL in code
    if not has_pass:
        issues.append(ParseIssue("error", "tb/no-pass-marker",
                                 f'tb 里没有 "{RESULT_PASS}"',
                                 "探测器就是靠这个字符串判定成败的"))
    if not has_fail:
        issues.append(ParseIssue("error", "tb/no-fail-marker",
                                 f'tb 里没有 "{RESULT_FAIL}"',
                                 "失败分支也必须打印，否则出错时探测器看不出来"))

    # 硬性约定 C：必须真的自检，不能永远打印 PASSED
    # 注意：必须匹配「声明」而不是「出现过」。
    # 一开始写成 \berrors?\b / \bchecks?\b，结果 `errors = errors + 1` 和
    # $display("checks = %0d", checks) 里的字符串都会命中 —— 变量被改名了也发现不了。
    if not re.search(r"\b(?:task|function)\s+[A-Za-z_]\w*", code):
        issues.append(ParseIssue("warn", "tb/no-check-task",
                                 "tb 里没有 task/function 定义 —— 怀疑没有逐条比对",
                                 "应该有 check 任务做逐条断言"))
    if not re.search(r"\binteger\s+errors?\b", code):
        issues.append(ParseIssue("warn", "tb/no-errors-var",
                                 "tb 里没有 `integer errors` 声明",
                                 "没有错误计数就无法保证 PASSED 是真的通过"))
    if not re.search(r"\binteger\s+checks?\b", code):
        issues.append(ParseIssue("warn", "tb/no-checks-var",
                                 "tb 里没有 `integer checks` 声明",
                                 "建议统计检查点数量，第 4 步要用来做报告"))
    if "$finish" not in code:
        issues.append(ParseIssue("warn", "tb/no-finish", "tb 里没有 $finish，仿真可能不结束"))

    # 必须真的实例化被测试设计。
    # 注意：不能用 \bdut\b —— `tb_dut` 和 `"dut.vcd"` 里都含 dut，
    # 那样写的话「根本没接上被测设计」永远检查不出来。
    if dut_module and not is_instantiated(code, dut_module):
        issues.append(ParseIssue(
            "error", "tb/not-instantiate",
            f"tb 里没有实例化 dut 的模块 {dut_module} —— 没接上被测设计",
            f"应该有类似 `{dut_module} u_dut (...);` 的例化语句"))
    return issues


def is_instantiated(code: str, module: str) -> bool:
    """判断 code 里是否真的例化了 module（而不是只提到名字）。

    真实例化的形态：`dut u_dut (...)` / `dut #(...) u_dut (...)` / `dut (...)`。
    要排除的干扰：`tb_dut`（名字的一部分）、`"dut.vcd"`（文件名）、`dut.x`（层级引用）。
    """
    name = re.escape(module)
    patterns = [
        rf"(?<![\w.]){name}\s*#\s*\(",            # 带参数例化
        rf"(?<![\w.]){name}\s+[A-Za-z_]\w*\s*\(",  # dut u_dut (
        rf"(?<![\w.]){name}\s*\(",                 # dut (          —— 少见但合法
    ]
    return any(re.search(p, code) for p in patterns)


# ------------------------------------------------------------------ 对外入口
def parse_response(text: str, *, compile_check: bool = True,
                   compiler=None) -> tuple[dict[str, str], list[ParseIssue]]:
    """把模型回复解析成 {"dut.v": ..., "tb_dut.v": ...}。

    契约：**本函数不替你决定要不要落盘**。
      · 能不能解析出东西 → 看返回的 files 是否为空
      · 该不该落盘       → 由调用方看 has_blocking_errors(issues)
    这样调用方能拿到完整的 files 用于「失败回喂」（第 3 步要把上一轮源码塞回提示词），
    而不是被这里悄悄丢掉。

    compile_check=True 时会额外用 iverilog 只编译不仿真（约 0.1 秒）。
    这一步很关键：格式全对但编译不过的代码，光靠文本规则是查不出来的。
    """
    obj, issues = parse_json_blob(text or "")
    if obj is None:
        return {}, issues

    # 模型可能用 dut / tb / files 等不同键名，都认
    dut = obj.get("dut") or obj.get("dut_v") or obj.get("design")
    tb = obj.get("tb") or obj.get("tb_dut") or obj.get("testbench")
    if isinstance(obj.get("files"), dict):
        f = obj["files"]
        dut = dut or f.get(DUT_NAME) or f.get("dut") or f.get("dut.v")
        tb = tb or f.get(TB_NAME) or f.get("tb") or f.get("tb_dut.v")

    ok_dut, ok_tb = isinstance(dut, str) and dut.strip(), isinstance(tb, str) and tb.strip()
    if not ok_dut:
        issues.append(ParseIssue("error", "field/missing:dut", "回复里没有 dut 源代码"))
    if not ok_tb:
        issues.append(ParseIssue("error", "field/missing:tb", "回复里没有 tb 源代码"))

    files: dict[str, str] = {}
    if ok_dut and ok_tb:
        dut_names = _module_names(dut)
        if len(dut_names) > 1:
            issues.append(ParseIssue("warn", "dut/multi-module",
                                     f"dut 里有多个 module（{', '.join(dut_names)}），"
                                     f"综合时会用 top 指定的那个"))
        top = dut_names[0] if dut_names else None
        issues += check_dut(dut)
        issues += check_tb(tb, top)
        files = {DUT_NAME: dut.strip() + "\n", TB_NAME: tb.strip() + "\n"}

        # 只有文本检查都过了才值得花时间去编译 —— 省下无谓的 0.1 秒
        if compile_check and not any(i.level == "error" for i in issues):
            issues += _compile_issues(dut, tb, compiler)

    return files, issues


def _compile_issues(dut: str, tb: str, compiler=None) -> list[ParseIssue]:
    """用 iverilog 只编译不仿真。环境里没有 iverilog 时静默跳过（不算失败）。"""
    try:
        if compiler is None:
            from quench_compile import compile_check as compiler
        res = compiler(dut, tb)
    except Exception as exc:  # noqa: BLE001
        return [ParseIssue("warn", "compile/skipped",
                           f"编译检查没能执行，已跳过：{type(exc).__name__}: {exc}")]
    if res.skipped:
        return [ParseIssue("warn", "compile/skipped", res.summary())]
    if res.ok:
        return []
    lines = res.error_lines()
    return [ParseIssue("error", "compile/failed",
                       f"代码编译不过：{res.error}",
                       "完整报错：\n        " + "\n        ".join(lines[:8]))]


def has_blocking_errors(issues: list[ParseIssue]) -> bool:
    return any(i.level == "error" for i in issues)


def format_issues(issues: list[ParseIssue]) -> str:
    if not issues:
        return "没有发现问题"
    out = []
    for i in issues:
        tag = "[错误]" if i.level == "error" else "[提醒]"
        out.append(f"  {tag} {i.message}")
        if i.fix:
            out.append(f"         建议：{i.fix}")
    return "\n".join(out)
