#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 7：重试循环的护栏地基。

两个纯函数模块，零依赖、零副作用，所以可以彻底测干净：

  ① testbench 冻结 —— 每轮算哈希对比。
     防的是：模型为了让测试变绿，去改**测试**而不是改**设计**。
     这是真实会发生的事，而且是最危险的一种"成功"——测试全绿但设计是错的。

  ② 错误指纹 —— 把报错归一化成可比较的指纹。
     防的是：在同一个坑里无限绕（改了第 3 行又错第 7 行，问题其实是同一个）。

这两件事都必须是**纯函数**：给定同样输入必得同样输出，否则第 4 步
统计"同一错误重复几次"就不可靠了。
"""
from __future__ import annotations

import hashlib
import re

# ------------------------------------------------------------------ ① 冻结
def normalize_verilog(src: str) -> str:
    """去掉注释、压缩空白，用于「语义对比」。

    只忽略格式差异和注释 —— 这两样改了不影响测试逻辑。
    反过来，只要**任何一条语句**变了，归一化结果就会变，哈希就对不上。
    """
    if not src:
        return ""
    # 去块注释（非贪婪）
    s = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    # 去行注释
    s = re.sub(r"//[^\n]*", " ", s)
    # `` `timescale `` 之类的编译指令保留，它们有语义
    # 压缩所有空白（含换行）为单个空格
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def tb_hash(src: str) -> str:
    """测试平台的指纹。长度 12 的十六进制串，够短好放日志，又够长不撞。"""
    return hashlib.sha256(normalize_verilog(src).encode("utf-8")).hexdigest()[:12]


def code_fingerprint(*sources: str) -> str:
    """源码内容的指纹（用于「这一轮和上一轮是不是一模一样」）。"""
    h = hashlib.sha256()
    for s in sources:
        h.update(normalize_verilog(s).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def tb_changed(baseline_hash: str, new_src: str) -> tuple[bool, str]:
    """测试平台是否被改动过。返回 (是否变了, 新哈希)。"""
    new_hash = tb_hash(new_src)
    return (new_hash != baseline_hash), new_hash


# ------------------------------------------------------------------ ② 指纹
# 报错行里这些部分每轮都会变（行号、列号、临时路径、时间戳），
# 必须抹掉，否则"同一个错误"会被当成"新错误"，去重就失效了。
#
# 顺序很重要：**先抹 file:line:col，再抹别的大数字**。
# 反过来的话，\b\d{4,}\b 会把三位数以上的行号当成"大数字"抹掉，
# 污染开头，导致同一个错误换个行号就变成不同指纹（这个坑实测踩到过）。
_NOISE_PATTERNS = [
    (re.compile(r"/tmp/[A-Za-z0-9_./-]+"), "<TMP>"),
    (re.compile(r"quench_chk_[A-Za-z0-9_]+"), "<TMPDIR>"),
    (re.compile(r"[A-Za-z]:\\[^\s]+"), "<PATH>"),
    (re.compile(r"/mnt/[a-z]/[^\s]+"), "<PATH>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "0x<HEX>"),
    # 行号/列号：放前面，避免被下面的"大数字"规则误伤
    (re.compile(r":\d+:\d+:"), ":<L>:<C>:"),
    (re.compile(r":\d+:"), ":<L>:"),
    (re.compile(r"\bline\s+\d+\b", re.I), "line <L>"),
    (re.compile(r"\b\d+\s*(?:error|warning)s?\b", re.I), "<COUNT> errors"),
    # 最后才抹别的大数字（时长、token 数等）
    (re.compile(r"\b\d{5,}\b"), "<N>"),
]

# 只把这些工具的错误行当"错误"来看
_ERROR_LINE_SIGNS = (
    "error", "not a valid", "syntax", "cannot", "undeclared",
    "unknown", "mismatch", "FAIL", "failed", "Warning", "warning",
    "%Error", "%Warning", "unsynthesizable", "not found",
)


def normalize_error_line(line: str) -> str:
    """把一行报错抹掉易变部分，留下稳定的"语义骨架"。"""
    s = line.strip()
    if not s:
        return ""
    # 去掉开头的 file.v:12:34: 或 file.v:12: 或 file.v: 前缀
    s = re.sub(r"^\S*\.(?:v|sv|vvp|log):\d+(?::\d+)?:?\s*", "", s)
    s = re.sub(r"^\S*\.(?:v|sv|vvp|log):\s*", "", s)
    for pat, rep in _NOISE_PATTERNS:
        s = pat.sub(rep, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_error_lines(output: str) -> list[str]:
    """从工具输出里挑出报错行。"""
    out = []
    for raw in (output or "").replace("\r\n", "\n").splitlines():
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        if any(sign.lower() in low for sign in _ERROR_LINE_SIGNS):
            out.append(s)
    return out


def error_fingerprint(output: str) -> str:
    """把一次失败归一化成指纹。

    同样的错误 → 同样的指纹（哪怕行号变了）。
    这是"同一错误重复 2 次就停"这条护栏的唯一依据。
    """
    lines = [normalize_error_line(l) for l in extract_error_lines(output)]
    lines = [l for l in lines if l]
    if not lines:
        # 没有可识别的错误行时，用整段输出的骨架兜底，至少别退化成空串
        fallback = normalize_error_line((output or "")[:400])
        lines = [fallback or "(空输出)"]
    # 排序后再拼：错误出现的顺序不该影响"是不是同一个错"的判断
    key = "\n".join(sorted(set(lines)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def error_summary(output: str, limit: int = 6) -> list[str]:
    """给人看的错误摘要（进日志、也进「失败回喂」）。"""
    lines = extract_error_lines(output)
    if not lines:
        lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    return lines[:limit]


# ------------------------------------------------------------------ ③ 阴性对照
_STUB_TEMPLATE = """// 阴性对照用的桩设计：输出恒为 0，什么都不做。
// 一个真正在验证的测试平台，面对这个桩**必须失败**。
module {name} #(parameter W = 32) (
    output wire [W-1:0] q,
    output wire [W-1:0] sum,
    output wire [W-1:0] dout,
    output wire [W-1:0] count,
    output wire [W-1:0] y,
    output wire [W-1:0] result,
    output wire [W-1:0] data_out,
    output wire [W-1:0] out_q,
    output wire         overflow,
    output wire         carry,
    output wire         done,
    output wire         valid,
    output wire         ready,
    output wire         zero,
    output wire         flag
);
    assign q = {{W{{1'b0}}}};
    assign sum = {{W{{1'b0}}}};
    assign dout = {{W{{1'b0}}}};
    assign count = {{W{{1'b0}}}};
    assign y = {{W{{1'b0}}}};
    assign result = {{W{{1'b0}}}};
    assign data_out = {{W{{1'b0}}}};
    assign out_q = {{W{{1'b0}}}};
    assign overflow = 1'b0;
    assign carry = 1'b0;
    assign done = 1'b0;
    assign valid = 1'b0;
    assign ready = 1'b0;
    assign zero = 1'b0;
    assign flag = 1'b0;
endmodule
"""


def _port_decls(src: str) -> tuple[list[tuple[str, str]], str]:
    """从 dut 源码里抽出端口声明。

    返回 ([(方向, 端口名), ...], 方向字符串)。
    支持 ANSI 风格：`input wire [7:0] a, b` 和 `output reg [7:0] q`。
    认不出来就返回空列表。
    """
    code = strip_verilog_comments(src)
    m = re.search(r"\bmodule\s+\w+\s*(?:#\s*\([^)]*\))?\s*\((.*?)\)\s*;", code, re.S)
    if not m:
        return [], ""
    body = m.group(1)
    # 去掉参数块之外的换行带来的干扰，按顶层逗号切分端口
    depth = 0
    cur = ""
    chunks = []
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            chunks.append(cur)
            cur = ""
        else:
            cur += ch
    chunks.append(cur)

    ports: list[tuple[str, str]] = []
    direction = ""
    for raw in chunks:
        s = " ".join(raw.split())
        if not s:
            continue
        dm = re.match(r"(input|output|inout)\b(.*)", s)
        if dm:
            direction = dm.group(1)
            s = dm.group(2).strip()
        if not direction:
            continue
        # 去掉类型与位宽，取名字
        s = re.sub(r"\b(wire|reg|logic|signed|unsigned)\b", " ", s)
        s = re.sub(r"\[[^\]]*\]", " ", s)
        name = s.strip().split()[0] if s.strip() else ""
        name = name.strip(";").strip()
        if re.fullmatch(r"[A-Za-z_]\w*", name or ""):
            ports.append((direction, name))
    return ports, direction


def make_stub_from_dut(dut_src: str, module_name: str) -> str | None:
    """按真实 DUT 生成「输出恒为 0」的桩。

    为什么不能自己拼端口列表 —— 实测连栽两次：
      ① 用写死的猜测端口名 → 测试平台接的 `clk` 不存在，编译报
         "port clk is not a port of u_dut"
      ② 从端口列表重新拼模块 → 参数 `#(.W(4))` 丢了，编译报
         "parameter W not found in tb_dut.u_dut"

    所以最稳的做法是：**原样保留模块头（含参数）和端口声明**，
    只把 `module ... ;` 和 `endmodule` 之间的实现换成一堆 `assign <out> = 0;`。
    这样参数、端口、位宽、顺序全都和真的一模一样，测试平台必然接得上。
    """
    code = strip_verilog_comments(dut_src)
    if not re.search(rf"\bmodule\s+{re.escape(module_name)}\b", code):
        return None
    # 找模块头结束的那个分号：从 module 关键字起，跳过配对的括号后再找 ';'
    m = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", code)
    i = m.start()
    depth = 0
    head_end = -1
    for j in range(m.end(), len(code)):
        ch = code[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            head_end = j
            break
    if head_end < 0:
        return None

    # 头部：从 module 到那个分号（含）。用**原始源码**这段，保留参数和注释。
    orig = dut_src
    om = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", strip_verilog_comments(orig))
    if not om:
        return None
    # 在原始源码里按同样规则找头部的结尾
    odepth, ohead_end = 0, -1
    for j in range(om.end(), len(orig)):
        ch = orig[j]
        if ch in "([{":
            odepth += 1
        elif ch in ")]}":
            odepth -= 1
        elif ch == ";" and odepth == 0:
            ohead_end = j
            break
    if ohead_end < 0:
        return None
    head = orig[om.start():ohead_end + 1]

    ports, direction = _port_decls(dut_src)
    if not ports or direction != "output":
        # 端口方向解析不出来就不硬猜，放弃（宁可跳过对照，也不要误判）
        return None
    outputs = [n for d, n in ports if d == "output"]
    if not outputs:
        return None

    assigns = "\n".join(f"    assign {n} = 0;" for n in outputs)
    # 关键：`output reg q` 不能配 `assign`（连续赋值要求 wire）。
    # 把头部里的 reg/logic 换成 wire —— 这是第三次踩接口不一致的坑了。
    head_wire = re.sub(r"\b(reg|logic)\b", "wire", head)
    return (f"// 阴性对照用的桩：端口与参数原样保留，输出强制为 0\n"
            f"{head_wire}\n"
            f"{assigns}\n"
            f"endmodule\n")


def strip_verilog_comments(src: str) -> str:
    s = re.sub(r"/\*.*?\*/", " ", src or "", flags=re.S)
    return re.sub(r"//[^\n]*", " ", s)


def liveness_check(dut_src: str, tb_src: str, dut_module: str,
                   compile_fn) -> tuple[bool, str]:
    """阴性对照：把 dut 换成恒零桩，测试平台应当失败。

    返回 (可疑?, 说明)。
      可疑 = 桩设计下测试平台**照样通过** → 它没在验证任何东西。

    注意两种"不算可疑"的情况：
      · 桩接不上端口，编译就失败 → 无法判断，不报可疑
      · 测试平台本来就没跑断言 → 交给解析层的 tb/no-check-task 去管
    """
    if not dut_module:
        return False, "不知道顶层模块名，跳过阴性对照"

    stub = make_stub_dut(dut_module)
    try:
        res = compile_fn(stub, tb_src)
    except Exception as exc:  # noqa: BLE001
        return False, f"阴性对照无法执行（{type(exc).__name__}），跳过"

    if res.skipped:
        return False, "环境里没有 iverilog，阴性对照跳过"

    if not res.ok:
        # 桩设计编译不过（多半是端口名对不上）—— 这恰恰说明测试平台
        # 依赖了真实端口，是好事，不算可疑。
        return False, "桩设计接不上测试平台（端口不匹配），说明测试依赖真实接口 ✓"

    # 桩编译过了 —— 那就要看仿真结果。这里只能判断"编译得过的桩"，
    # 真正跑仿真由调用方通过 probe 完成。
    return True, "桩设计能编译通过，需要进一步跑仿真确认测试是否会失败"


class NegativeControl:
    """阴性对照的完整版：需要能跑仿真的调用方配合。

    用法：
        nc = NegativeControl(run_fn)
        ok, msg = nc.run(dut_src, tb_src, dut_module)
    """

    def __init__(self, run_fn):
        self.run_fn = run_fn      # (桩源码, tb源码, 模块名) -> (探测全绿?, 日志)

    def run(self, dut_src: str, tb_src: str, dut_module: str) -> tuple[bool, str]:
        """跑对照。返回 (测试是活的?, 说明)。"""
        if not dut_module:
            return True, "不知道顶层模块名，跳过阴性对照"
        stub = make_stub_from_dut(dut_src, dut_module)
        if stub is None:
            return True, "解析不出 dut 的端口（可能不是 ANSI 风格），跳过阴性对照"
        try:
            ok, log = self.run_fn(stub, tb_src, dut_module)
        except Exception as exc:  # noqa: BLE001
            return True, f"阴性对照无法执行（{type(exc).__name__}），跳过"
        if ok:
            return False, ("把 dut 换成「输出恒为 0」的桩之后，测试**依然通过** —— "
                           "说明它没有在真正验证设计")
        # 桩编译不过时不能下结论（端口没对上），要说清楚
        if "is not a port of" in (log or "") or "not a port" in (log or ""):
            return True, "桩的端口和测试平台对不上，阴性对照无法判定（跳过）"
        return True, "阴性对照通过（桩设计下测试确实失败了）"
# ------------------------------------------------------------------ ③.5 削弱检测
# 实测发现：模型"修不好设计就改测试"的手法很隐蔽，不是简单删断言，而是
# 把「验证设计输出」偷换成「验证自己写死的常量数组」。
# 例如把 check_adjacent(prev, gray)（prev/gray 来自设计输出）
# 改成 check_adjacent(seq[i], seq[i+1])（seq 是硬编码的期望序列）——
# 后者恒为真，等于删掉了这个检查，但文本上没有"删断言"的痕迹。
_WEAKEN_PATTERNS = [
    (re.compile(r"\bif\s*\(\s*0\s*\)"), "出现 `if (0)` —— 条件恒为假，断言被架空了"),
    (re.compile(r"\bif\s*\(\s*1\s*\)"), "出现 `if (1)` —— 条件恒为真"),
    (re.compile(r"\bassert\s*\(\s*1\s*\)"), "出现 `assert(1)` —— 断言恒为真"),
    (re.compile(r"#\s*0\s*;"), "出现零延时，可能掩盖时序问题"),
]

# 拿"设计输出信号"和"常量"比较，是可疑信号（多半是空比较或写死的自证）
_LITERAL_CMP = re.compile(r"(\w+)\s*!==?\s*(\d+'[bdh][0-9a-fA-F_xz]+)")
_DECL_SEQ = re.compile(r"\b(?:reg|wire)\s*(?:\[[^\]]*\])?\s*(\w+)\s*\[\s*\d+\s*:\s*\d+\s*\]")


def detect_weakening(old_tb: str, new_tb: str) -> list[str]:
    """对比新旧测试平台，找出"变松"的地方。

    返回问题列表（空 = 没发现削弱）。这是**静态**判断，不跑仿真 —— 快、稳、可解释。
    """
    problems: list[str] = []
    if not old_tb or not new_tb:
        return problems

    old_code = strip_verilog_comments(old_tb)
    new_code = strip_verilog_comments(new_tb)

    # 1) 恒真/恒假的条件
    for pat, why in _WEAKEN_PATTERNS:
        if pat.search(new_code) and not pat.search(old_code):
            problems.append(why)

    # 2) 检查点/断言数量明显变少
    #    不能只数 `checks = checks + 1` —— 那通常写在 task 内部，
    #    测试平台的 initial 块里只看到 `check(...)` 调用。
    #    所以要把「自增」和「任务调用」都算上，否则删掉两个 check() 调用都发现不了。
    _CALL_ONLY = {"if", "for", "while", "case", "display", "dumpfile", "dumpvars",
                  "finish", "monitor", "write", "readmemh", "readmemb", "time",
                  "random", "signed", "unsigned", "posedge", "negedge", "repeat",
                  "forever", "initial", "always", "assign", "begin", "end"}

    def count_checks(src: str) -> int:
        incr = len(re.findall(r"\bchecks\s*=\s*checks\s*\+\s*1", src))
        calls = 0
        for m in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*\(", src):
            name = m.group(1)
            if name in _CALL_ONLY:
                continue
            calls += 1
        # 直接写在 initial 里的比较（没走 task 的那种）
        direct = len(re.findall(r"\bif\s*\([^)]*(?:!==?|==?=)", src))
        return max(incr, calls) + direct

    n_old, n_new = count_checks(old_code), count_checks(new_code)
    if n_old >= 2 and n_new < n_old:
        problems.append(f"检查点数量从 {n_old} 处降到 {n_new} 处"
                        f"（少了 {n_old - n_new} 处）")

    # 2.5) 比较里全是常量、没有设计信号参与
    #      例如把 `q !== want` 改成 `1'b0 !== 1'b0` —— 恒为假，等于没有断言。
    const_cmp = re.compile(r"(\d+'[bdh][0-9a-fA-F_xz]+|\d+)\s*(?:!==?|==?=)\s*"
                           r"(\d+'[bdh][0-9a-fA-F_xz]+|\d+)")
    old_const = len(const_cmp.findall(old_code))
    new_const = len(const_cmp.findall(new_code))
    if new_const > old_const:
        # 再确认一下：原来参与比较的设计信号是不是变少了
        def cmp_idents(src: str) -> set[str]:
            return {m.group(1) for m in
                    re.finditer(r"\b([A-Za-z_]\w*)\s*(?:!==?|==?=)", src)}
        lost = cmp_idents(old_code) - cmp_idents(new_code)
        # 排除纯常量名和关键字
        lost = {n for n in lost if not re.fullmatch(r"\d.*", n)
                and n not in ("if", "while", "for")}
        if lost:
            problems.append(f"常量之间的比较增加了（{old_const} → {new_const}），"
                            f"而设计信号 {', '.join(sorted(lost))} 不再参与比较 —— "
                            f"这类比较恒为真/假，等于没有断言")

    # 3) 把"比较设计输出"换成"比较硬编码数组"
    #    判据：原先比较里出现、现在只拿另一个本地数组互相比较
    seq_arrays = set(_DECL_SEQ.findall(old_code)) | set(_DECL_SEQ.findall(new_code))
    def cmp_targets(src: str) -> set[str]:
        return {m.group(1) for m in _LITERAL_CMP.finditer(src)}
    old_targets, new_targets = cmp_targets(old_code), cmp_targets(new_code)
    dropped = old_targets - new_targets
    # 只报"被删掉且它原来是数组元素之外的设计信号"的那种
    if dropped:
        for name in sorted(dropped):
            if name in seq_arrays:
                continue
            # 该信号在新版里是否只出现在赋值左侧（不再被比较）
            still_compared = re.search(rf"\b{re.escape(name)}\b[^\n]*!==?", new_code)
            if not still_compared:
                problems.append(f"设计信号 `{name}` 不再参与比较了")

    # 4) 任务调用的实参"从设计信号换成了硬编码数组元素"
    #    这是实测遇到的真实案例：
    #      check_adjacent(prev, gray)             ← prev/gray 来自设计输出
    #      改成 check_adjacent(seq[i], seq[i+1])  ← seq 是写死的期望序列，恒为真
    array_names = set(_DECL_SEQ.findall(old_code)) | set(_DECL_SEQ.findall(new_code))

    _BUILTINS = {"if", "for", "while", "case", "display", "dumpfile", "dumpvars",
                 "finish", "monitor", "write", "readmemh", "readmemb", "time",
                 "random", "signed", "unsigned", "posedge", "negedge"}

    def call_args(src: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for m in re.finditer(r"\b(\w+)\s*\(([^()]*)\)\s*;", src):
            fn, args = m.group(1), m.group(2)
            if fn in _BUILTINS:
                continue
            names = set()
            for a in args.split(","):
                nm = re.match(r"\s*([A-Za-z_]\w*)", a)
                if nm:
                    names.add(nm.group(1))
            if names:
                out.setdefault(fn, set()).update(names)
        return out

    old_calls, new_calls = call_args(old_code), call_args(new_code)
    for fn, old_names in old_calls.items():
        new_names = new_calls.get(fn)
        if not new_names:
            continue
        design_signals = {n for n in old_names if n not in array_names}
        new_array_args = {n for n in new_names if n in array_names}
        if design_signals and new_array_args and not (design_signals & new_names):
            problems.append(
                f"`{fn}()` 的实参从设计信号（{', '.join(sorted(design_signals))}）"
                f"换成了硬编码数组（{', '.join(sorted(new_array_args))}）—— "
                f"这样只能证明常量自洽，证明不了设计正确")

    return problems


def tb_is_weaker(old_tb: str, new_tb: str) -> tuple[bool, list[str]]:
    """便捷包装：返回 (是否变松, 问题列表)。"""
    problems = detect_weakening(old_tb, new_tb)
    return bool(problems), problems


# ------------------------------------------------------------------ ④ 重复检测
class RepeatTracker:
    """记录每轮的错误指纹，判断是不是在原地打转。

    两条停止条件（截图里要求的第一条护栏）：
      · 同一指纹连续出现 N 次
      · 或者同一指纹累计出现 N 次（中间夹了别的错误也算）
    默认用「连续」，因为它更能说明"这一改没解决问题"。
    """

    def __init__(self, limit: int = 2, consecutive: bool = True):
        self.limit = max(1, limit)
        self.consecutive = consecutive
        self.history: list[str] = []

    def add(self, fingerprint: str) -> bool:
        """记一轮。返回 True 表示「撞到重复上限了，该停了」。"""
        self.history.append(fingerprint)
        if self.consecutive:
            tail = self.history[-self.limit:]
            return len(tail) >= self.limit and len(set(tail)) == 1
        return self.history.count(fingerprint) >= self.limit

    def describe(self) -> str:
        if not self.history:
            return "还没有记录"
        if self.consecutive:
            n = 0
            for f in reversed(self.history):
                if f == self.history[-1]:
                    n += 1
                else:
                    break
            return f"最近同一个错误连续出现 {n} 次（指纹 {self.history[-1]}）"
        return f"错误指纹历史：{self.history}"

    def repeats_of(self, fingerprint: str) -> int:
        return self.history.count(fingerprint)


if __name__ == "__main__":
    # 自查：拿真实踩到过的那条报错验证，**并且真的断言**
    # （第一版这里只打印不断言，差点把"行号变了指纹也变"这个 bug 放过去）
    ok = True

    real = """tb_dut.v:67: error: q is not a valid l-value in tb_dut.
tb_dut.v:8:      : q is declared here as wire.
1 error(s) during elaboration."""
    moved = """tb_dut.v:102: error: q is not a valid l-value in tb_dut.
tb_dut.v:15:      : q is declared here as wire.
1 error(s) during elaboration."""
    f1, f2 = error_fingerprint(real), error_fingerprint(moved)

    print("真实报错指纹   :", f1)
    print("行号变了之后   :", f2)
    same = (f1 == f2)
    print("两者相同吗     :", same, "← 必须为 True，否则去重失效")
    ok &= same

    print()
    print("错误摘要:")
    for l in error_summary(real):
        print("   ", l)

    print()
    h1 = tb_hash("module tb; endmodule")
    h2 = tb_hash("module tb; endmodule // 注释")
    h3 = tb_hash("module tb; initial $finish; endmodule")
    print("testbench 哈希 :", h1)
    print("只加注释       :", h2, "同哈希 =", h1 == h2, "（格式差异不该算改）")
    print("加一条语句     :", h3, "同哈希 =", h1 == h3, "（语义一变必须算改）")
    ok &= (h1 == h2) and (h1 != h3)

    print()
    diff = error_fingerprint("tb.v:1: error: 完全不同的错误 A")
    same_fp = error_fingerprint(real)
    print("不同错误指纹不同:", diff != same_fp, "← 必须为 True")
    ok &= (diff != same_fp)

    print()
    print("护栏自查：", "全部通过 ✓" if ok else "有失败 ✗")
    raise SystemExit(0 if ok else 1)
