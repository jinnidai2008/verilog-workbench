#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 3 步（失败回喂 + 重试循环）的测试。

核心手法：**用一个「脚本化的假模型」替掉真 API** —— 它按剧本一轮一轮地返回
不同的回答（第一轮给错的、第二轮给对的），这样就能在不花钱、不联网的前提下
把整个循环路走一遍，包括偷改测试平台这种恶劣情况。

用法: python test_loop.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_feedback as FB      # noqa: E402
import quench_guard as GUARD      # noqa: E402
import quench_loop as LOOP        # noqa: E402
import quench_parse as PARSE      # noqa: E402
import quench_runs as RUNS        # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ 测试用素材
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

GOOD_TB = """`timescale 1ns/1ps
module tb_dut;
    reg        clk = 0;
    reg        rst_n = 0;
    wire [3:0] q;
    integer errors = 0;
    integer checks = 0;

    dut #(.W(4)) u_dut (.clk(clk), .rst_n(rst_n), .q(q));
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
        /* 时序说明（实测插桩确认过）：
           clk 初值 0，`always #5 clk = ~clk` → 上升沿在 t=5,15,25…
           rst_n 初值 0，所以 t=5 那下就把 q 清成 0。
           在下降沿（t=10）释放复位后，**第一个上升沿 t=15 就会自增一次**：
             t=15000 读数 q=0（NBA 还没更）
             t=16000 读数 q=1（NBA 已更）
           而 `@(posedge clk); #1;` 落在 t=+1ns，读到的是**自增后**的值。
           所以释放复位后的第一次检查应当期望 1，不是 0。 */
        @(negedge clk);
        rst_n = 1;
        check(4'd1);
        check(4'd2);
        check(4'd3);
        $display("checks = %0d", checks);
        if (errors == 0) $display("=== TEST PASSED ===");
        else             $display("=== TEST FAILED ===");
        $finish;
    end
endmodule
"""

# 编译不过的 tb：给 wire 赋值（我们真的踩过这个坑）
# 注意：要注入到 initial 块里。注入到 task 里的话，wire 在 task 作用域里
# 会被当成新变量，反而不报错 —— 这个坑也踩过。
BAD_TB = GOOD_TB.replace("        @(negedge clk);",
                         "        q = 4'd9;      // 非法：q 是 wire\n        @(negedge clk);")
# 语义改过的 tb：把判据放宽了（假装的"修好"）
TAMPERED_TB = GOOD_TB.replace("if (q !== want)", "if (0)")

# 设计里有真实 bug：q 一直不增加
BROKEN_DUT = GOOD_DUT.replace("q <= q + 1'b1;", "q <= q;")


def reply(dut=GOOD_DUT, tb=GOOD_TB, notes="") -> dict:
    """造一个「API 回复」对象。"""
    return {
        "text": json.dumps({"dut": dut, "tb": tb, "top": "dut", "notes": notes},
                           ensure_ascii=False),
        "usage": {"total_tokens": 100},
        "model": "fake",
    }


class ScriptedModel:
    """按剧本返回的假模型。剧本用完就重复最后一个。"""

    def __init__(self, script: list):
        self.script = script
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **kw):
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        item = self.script[idx]
        if isinstance(item, Exception):
            raise item
        return item

    def prompts(self) -> list[str]:
        return [m[-1]["content"] for m in self.calls]


def patch_api(model):
    """把 quench_loop 里的 API.chat 换成假模型。"""
    original = LOOP.API.chat
    LOOP.API.chat = model
    return original


def unpatch_api(original):
    LOOP.API.chat = original


# ------------------------------------------------------------------ 测试
def main() -> int:
    print("=" * 72)
    print("第 3 步（失败回喂 + 重试循环）测试")
    print("=" * 72)

    # ================================================================ A. 护栏①
    print("\n【A】护栏①：testbench 冻结（防偷改判据）")
    check("改动语句 → 哈希变化",
          GUARD.tb_hash(GOOD_TB) != GUARD.tb_hash(TAMPERED_TB))
    check("只加注释/空白 → 哈希不变",
          GUARD.tb_hash(GOOD_TB) == GUARD.tb_hash(GOOD_TB + "\n// 加个注释\n"))
    changed, new_h = GUARD.tb_changed(GUARD.tb_hash(GOOD_TB), TAMPERED_TB)
    check("tb_changed 能检出改动", changed and new_h != GUARD.tb_hash(GOOD_TB))

    # 场景 A：第一轮交一个「永远为真」的测试 → 应当被阴性对照拦下
    model = ScriptedModel([reply(tb=TAMPERED_TB), reply()])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "deadtest"
            res = LOOP.quench("4 位计数器", proj, max_rounds=4, archive=False,
                              write_files=False, log_fn=lambda s: None)
            check("永远为真的测试 → 第一轮就被拦（阴性对照）",
                  any(r["outcome"] == "dead-test" for r in res.rounds),
                  str([r["outcome"] for r in res.rounds]))
            check("拦住之后换正常测试 → 最终通过", res.ok,
                  f"ok={res.ok} stop={res.stop_reason}")
            check("基线哈希是在阴性对照之后才锁定的（锁的是正常版）",
                  res.tb_hash == GUARD.tb_hash(GOOD_TB),
                  f"锁了 {res.tb_hash}，正常版是 {GUARD.tb_hash(GOOD_TB)}")
            second = model.prompts()[1] if len(model.prompts()) > 1 else ""
            check("回喂里说明了「换桩设计照样通过」",
                  "恒为 0" in second and "依然报告通过" in second, second[-400:])
    finally:
        unpatch_api(orig)

    # 场景 B：先建立合法基线，之后偷改 tb → 应当被哈希护栏拦下
    model = ScriptedModel([reply(dut=BROKEN_DUT), reply(tb=TAMPERED_TB), reply()])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "tamper-test"
            res = LOOP.quench("4 位计数器", proj, max_rounds=4, repeat_limit=99,
                              archive=False, write_files=False, log_fn=lambda s: None)
            check("有基线后偷改 tb → 该轮被拒绝",
                  any(r["outcome"] == "tb-tampered" for r in res.rounds),
                  str([r["outcome"] for r in res.rounds]))
            check("被拒绝后回退到正常 tb → 最终通过", res.ok,
                  f"ok={res.ok} stop={res.stop_reason}")
            second = model.prompts()[1] if len(model.prompts()) > 1 else ""
            third = model.prompts()[2] if len(model.prompts()) > 2 else ""
            check("回喂里明确告知「改动了测试平台」",
                  "改动了测试平台" in third or "被拒绝" in third, third[:300])
            check("回喂里带上了锁定的哈希", res.tb_hash in third, f"hash={res.tb_hash}")
            check("回喂要求 tb 逐字符一致", "逐字符一致" in third, third[-300:])
    finally:
        unpatch_api(orig)

    # 场景 C：反复交「削弱版」测试 → 撞重复上限停下
    model = ScriptedModel([reply(dut=BROKEN_DUT), reply(tb=TAMPERED_TB)])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "tamper-loop"
            res = LOOP.quench("4 位计数器", proj, max_rounds=6, repeat_limit=2,
                              archive=False, write_files=False, log_fn=lambda s: None)
            check("反复交削弱版测试 → 撞重复上限停下",
                  not res.ok and "削弱" in res.stop_reason,
                  res.stop_reason)
            check("反复交削弱版测试 → 没有烧满 6 轮",
                  len(res.rounds) <= 4, f"跑了 {len(res.rounds)} 轮")
    finally:
        unpatch_api(orig)

    # ================================================================ B. 护栏②
    print("\n【B】护栏②：错误指纹去重（防在同一个坑里绕）")
    e1 = "tb_dut.v:67: error: q is not a valid l-value in tb_dut.\n1 error(s)"
    e2 = "tb_dut.v:120: error: q is not a valid l-value in tb_dut.\n1 error(s)"
    e3 = "tb_dut.v:9: error: undeclared identifier foo"
    check("同错误、行号不同 → 指纹相同",
          GUARD.error_fingerprint(e1) == GUARD.error_fingerprint(e2))
    check("不同错误 → 指纹不同",
          GUARD.error_fingerprint(e1) != GUARD.error_fingerprint(e3))

    t = GUARD.RepeatTracker(limit=2)
    check("第 1 次不算重复", t.add("aaa") is False)
    check("第 2 次相同 → 触发停止", t.add("aaa") is True)
    t2 = GUARD.RepeatTracker(limit=2)
    t2.add("aaa"); t2.add("bbb")
    check("不同指纹交替 → 不误判为重复", t2.add("aaa") is False)

    # 走一遍真实循环：模型反复给同一个编译错误
    model = ScriptedModel([reply(tb=BAD_TB)])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "compile-loop"
            res = LOOP.quench("4 位计数器", proj, max_rounds=6, repeat_limit=2,
                              archive=False, write_files=False, log_fn=lambda s: None)
            check("反复同一编译错误 → 撞上限停下",
                  not res.ok and "编译错误已连续出现" in res.stop_reason, res.stop_reason)
            check("反复同一编译错误 → 只跑了 2 轮",
                  len(res.rounds) == 2, f"跑了 {len(res.rounds)} 轮")
            check("每轮都记录了编译失败的指纹",
                  all(r.get("fingerprint") for r in res.rounds),
                  str([r.get("fingerprint") for r in res.rounds]))
    finally:
        unpatch_api(orig)

    # ================================================================ C. 护栏③
    print("\n【C】护栏③：轮数上限 + API 失败处理")
    model = ScriptedModel([reply(dut=BROKEN_DUT)])   # 设计真的错，探测永远不过
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "rounds-cap"
            # repeat_limit 设很大，逼它只能靠轮数上限停下
            res = LOOP.quench("4 位计数器", proj, max_rounds=3, repeat_limit=99,
                              archive=False, write_files=False, log_fn=lambda s: None)
            check("用满轮数上限就停（不会无限跑）",
                  len(res.rounds) == 3, f"跑了 {len(res.rounds)} 轮")
            check("停止原因说明了用满轮数",
                  "用满" in res.stop_reason, res.stop_reason)
    finally:
        unpatch_api(orig)

    # API 报错
    from quench_api import ApiError
    model = ScriptedModel([ApiError("余额不足", status=402)])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "api-err"
            res = LOOP.quench("4 位计数器", proj, max_rounds=5, archive=False,
                              write_files=False, log_fn=lambda s: None)
            check("API 报错 → 干净停止并记录原因",
                  not res.ok and "API 调用失败" in res.stop_reason, res.stop_reason)
            check("API 报错 → 只调了一次", len(model.calls) == 1, str(len(model.calls)))
    finally:
        unpatch_api(orig)

    # ================================================================ D. 回喂内容
    print("\n【D】回喂内容：讲得清、不超长、不泄密")
    big = "\n".join(f"line {i}: something happened" for i in range(800))
    fb = FB.build_feedback(round_no=2, max_rounds=5,
                           compile_output="tb_dut.v:1: error: boom",
                           sim_log=big, probe_log=big,
                           previous_dut=GOOD_DUT, failure_kind="探测未通过",
                           tb_hash="deadbeef1234", repeated=1)
    check("回喂长度受控（< 7000 字符）", len(fb) < 7000, f"{len(fb)} 字符")
    check("长日志被压缩（有省略标记）", "省略" in fb)
    check("带上了编译报错", "boom" in fb)
    check("带上了 tb 哈希", "deadbeef1234" in fb)
    check("带上了上一轮的 dut", "module dut" in fb)
    check("明确禁止偷改测试平台", "不要偷偷改绿它" in fb or "哈希被锁定" in fb)

    f1 = FB.build_feedback(round_no=1, max_rounds=5, failure_kind="x")
    f5 = FB.build_feedback(round_no=5, max_rounds=5, failure_kind="x")
    check("第 1 轮措辞平和", "仔细看下面的报错" in f1)
    check("最后一轮要求完全重设计", "完全重新设计" in f5)
    check("不同轮次措辞不同（不是复读机）", f1 != f5)

    # ================================================================ E. 存档
    print("\n【E】轮次存档：每轮的证据要落盘")
    model = ScriptedModel([reply(dut=BROKEN_DUT), reply()])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "archive-test"
            res = LOOP.quench("4 位计数器", proj, max_rounds=4, repeat_limit=99,
                              write_files=False, archive=True, log_fn=lambda s: None)
            nums = RUNS.list_rounds(proj)
            check("每轮都建了 round-N 目录", nums == [1, 2], str(nums))
            r1 = RUNS.read_round(proj, 1)
            check("round-1 有 dut.v / tb.v / log.txt / result.json / meta.json",
                  all(r1.get(k) is not None for k in ("dut", "tb", "log", "result", "meta")),
                  str({k: (r1.get(k) is not None) for k in ("dut", "tb", "log", "result", "meta")}))
            check("result.json 里记了 outcome",
                  (r1.get("result") or {}).get("outcome") == "probe-failed",
                  str(r1.get("result")))
            summ = RUNS.runs_root(proj) / "summary.json"
            check("写了 summary.json（第 4/5 步的数据源）", summ.is_file())
            if summ.is_file():
                s = json.loads(summ.read_text(encoding="utf-8"))
                check("summary 里有每轮统计",
                      isinstance(s.get("rounds"), list) and len(s["rounds"]) >= 1,
                      str(s)[:200])
                check("summary 里记了总 tokens", "totalTokens" in s, str(list(s.keys())))

            # diff 能力（第 5 步界面要用）
            a = RUNS.read_round(proj, 1).get("dut") or ""
            b = RUNS.read_round(proj, 2).get("dut") or ""
            diff = RUNS.unified_diff(a, b)
            check("能算出两轮 diff", bool(diff) and "+++" in diff, diff[:120])
            check("diff 没把两行粘成一行",
                  diff.count("\n") >= 3, repr(diff[:120]))
    finally:
        unpatch_api(orig)

    # ================================================================ F. 成功路径
    print("\n【F】第一轮就对：不该有多余重试")
    model = ScriptedModel([reply()])
    orig = patch_api(model)
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "first-try"
            # write_files=True 会真写盘 + 真探测（约 2 秒）
            res = LOOP.quench("4 位环形计数器", proj, max_rounds=5,
                              archive=False, write_files=True, log_fn=lambda s: None)
            check("一次就通过（只调了一次 API）", res.ok and len(model.calls) == 1,
                  f"ok={res.ok} calls={len(model.calls)} stop={res.stop_reason}")
            check("rounds 里只有一轮且 outcome=ok",
                  len(res.rounds) == 1 and res.rounds[0]["outcome"] == "ok",
                  str([r["outcome"] for r in res.rounds]))
            check("工程目录里真的写进了文件",
                  (proj / "dut.v").is_file() and (proj / "tb_dut.v").is_file()
                  and (proj / "project.json").is_file(),
                  str(sorted(p.name for p in proj.iterdir())) if proj.is_dir() else "目录不存在")
            check("锁定了 tb 哈希", bool(res.tb_hash), res.tb_hash)
            check("summary 里 ok=True", res.summary()["ok"] is True)
    finally:
        unpatch_api(orig)

    # ================================================================ 汇总
    print("\n" + "=" * 72)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:400]}")
    print("=" * 72)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
