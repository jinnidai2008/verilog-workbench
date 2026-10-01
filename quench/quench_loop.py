#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 10：生成 → 探测 → 失败了回喂重试  的主循环。

截图里说的「淬火的心脏」就是这里。

三条护栏（缺一条这工具就是玩具）：
  ① testbench 冻结 —— 生成后算哈希锁定，之后每轮对比。想偷改判据 → 直接拒绝该轮。
  ② 错误指纹去重   —— 同一错误连续出现 N 次就停，别在一个坑里无限绕。
  ③ 轮数上限 + 单步超时 —— 防它烧你的 API 余额。

流程：
    生成 ──► 探测 ──► 通过？ ──► 结束
              │
              └─ 失败 ──► 把(报错 + 日志 + 上一轮源码)回喂模型 ──► 重新生成（最多 N 轮）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quench_api as API            # noqa: E402
import quench_feedback as FB        # noqa: E402
import quench_guard as GUARD        # noqa: E402
import quench_parse as PARSE        # noqa: E402
import quench_prompt as PROMPT      # noqa: E402
import quench_runs as RUNS          # noqa: E402

DESK_APP = HERE.parent / "desk-app"

DEFAULT_MAX_ROUNDS = 5
DEFAULT_REPEAT_LIMIT = 2
PROBE_TIMEOUT = 900


class LoopResult:
    def __init__(self):
        self.ok = False
        self.rounds: list[dict] = []
        self.stop_reason = ""
        self.final_dut = ""
        self.final_tb = ""
        self.tb_hash = ""
        self.tokens = 0
        self.seconds = 0.0
        self.project_dir: Path | None = None

    def summary(self) -> dict:
        """第 4 步的统计、第 5 步的界面都读这个。"""
        by_round = []
        for r in self.rounds:
            by_round.append({
                "round": r["round"],
                "outcome": r["outcome"],         # ok / compile-failed / probe-failed / tb-tampered / api-error
                "failureKind": r.get("failure_kind", ""),
                "errorFingerprint": r.get("fingerprint", ""),
                "headline": r.get("headline", ""),
                "tokens": r.get("tokens", 0),
                "seconds": round(r.get("seconds", 0.0), 2),
                "tbChanged": r.get("tb_changed", False),
            })
        return {
            "ok": self.ok,
            "stopReason": self.stop_reason,
            "roundsUsed": len(self.rounds),
            "totalTokens": self.tokens,
            "totalSeconds": round(self.seconds, 2),
            "tbHash": self.tb_hash,
            "rounds": by_round,
        }


# ------------------------------------------------------------------ 探测
def run_probe(project_dir: Path, log_fn=print) -> tuple[bool, str, dict]:
    """调第 1 步的 refresh.py 真探测。返回 (是否全绿, 日志, board)。"""
    script = DESK_APP / "refresh.py"
    if not script.is_file():
        return False, f"(找不到 {script})", {}
    try:
        p = subprocess.run([sys.executable, str(script), str(project_dir)],
                           cwd=str(DESK_APP), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT)
        log = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"(探测超时，超过 {PROBE_TIMEOUT} 秒)", {}

    board = {}
    bp = DESK_APP / "board.json"
    if bp.is_file():
        try:
            board = json.loads(bp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            board = {}
    steps = board.get("steps") or {}
    all_ok = bool(steps) and all(s.get("state") == "ok" for s in steps.values())
    return all_ok, log, board


def sim_failure_output(board: dict) -> str:
    """从 board 里取出仿真那一步的日志，喂给回喂。"""
    steps = board.get("steps") or {}
    sim = steps.get("sim") or {}
    parts = []
    if sim.get("log"):
        parts.append(str(sim["log"]))
    if sim.get("headline"):
        parts.append(f"结论：{sim['headline']}")
    for key in ("lint", "synth", "wave"):
        st = steps.get(key) or {}
        if st.get("state") in ("fail", "warn") and st.get("log"):
            parts.append(f"[{key}] {st.get('headline')}\n{st['log']}")
    return "\n".join(parts)


def first_bad_headline(board: dict) -> str:
    for key in ("sim", "lint", "synth", "wave", "files"):
        st = (board.get("steps") or {}).get(key) or {}
        if st.get("state") != "ok":
            return f"{key}: {st.get('headline', '未通过')}"
    return ""


def _module_name(src: str) -> str:
    m = __import__("re").search(r"(?m)^\s*module\s+([A-Za-z_]\w*)", src or "")
    return m.group(1) if m else "dut"


def normalize_top_name(dut_src: str, tb_src: str,
                       want: str = "dut") -> tuple[str, str, str]:
    """把顶层模块名统一成 want（默认 dut）。

    为什么必须做：verilator 会对「文件名与模块名不符」报 DECLFILENAME 警告。
    我们的工程约定文件名是 dut.v / tb_dut.v，所以模块必须叫 dut；否则
    探测永远有一两条警告，"全部通过"的灯永远亮不起来（实测踩到过）。

    做法：在原源码上按词边界替换模块名。Verilog 里模块名和信号名是不同命名空间，
    同名的信号被一起改掉也无害 —— 文本一致性反而更好。

    返回 (新的 dut, 新的 tb, 改了什么说明)。没改则说明为空串。
    """
    import re as _re
    old = _module_name(dut_src)
    if not old or old == want:
        return dut_src, tb_src, ""

    # 只有模块级出现的位置才替换：
    #   module <old>        —— 模块声明
    #   <old> #(           —— 带参数的例化
    #   <old> <实例名> (    —— 普通例化
    pat = _re.compile(rf"(?<![\w.]){_re.escape(old)}(?=\s*(?:#\s*\(|[A-Za-z_]\w*\s*\())")
    new_dut = pat.sub(want, dut_src)
    # 模块声明那一处（`module my_counter`）后面跟的是 `(` 或 `#(`，上面已经覆盖；
    # 但若写法是 `module my_counter;`（无端口），单独再补一次
    new_dut = _re.sub(rf"(?m)^(\s*module\s+){_re.escape(old)}\b", rf"\g<1>{want}", new_dut)

    new_tb = pat.sub(want, tb_src)
    new_tb = _re.sub(rf"(?<![\w.]){_re.escape(old)}(?=\s+[A-Za-z_]\w*\s*\()", want, new_tb)

    changed = (new_dut != dut_src) or (new_tb != tb_src)
    note = (f"顶层模块名 {old} → {want}（为了让文件名与模块名一致，"
            f"避免 verilator 的 DECLFILENAME 警告）") if changed else ""
    return new_dut, new_tb, note


def _run_negative_control(dut_src: str, tb_src: str, dut_module: str,
                          project_dir: Path, log_fn) -> tuple[bool, str]:
    """在临时目录里跑阴性对照，不影响真实工程目录。"""
    import tempfile
    import quench_guard as G

    def runner(stub_dut: str, tb: str, module: str):
        with tempfile.TemporaryDirectory(prefix="quench_nc_") as td:
            p = Path(td)
            (p / "dut.v").write_text(stub_dut, encoding="utf-8")
            (p / "tb_dut.v").write_text(tb, encoding="utf-8")
            (p / "project.json").write_text(json.dumps({
                "version": 1, "name": "negative-control", "top": module,
                "dut": "dut.v", "tb": "tb_dut.v", "vcd": "dut.vcd",
                "timeout": 120, "distro": "Ubuntu-24.04",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            _, log, board = run_probe(p, log_fn)
            # 关键：只看**仿真那一步**过没过。
            # 桩设计必然有 lint 警告（输出没被驱动、输入没被使用），
            # 用"整体全绿"当判据的话，永远判不出"测试是死的"。
            sim_ok = ((board.get("steps") or {}).get("sim") or {}).get("state") == "ok"
            return sim_ok, log

    nc = G.NegativeControl(runner)
    return nc.run(dut_src, tb_src, dut_module)


# ------------------------------------------------------------------ 主循环
def quench(requirement: str, project_dir: Path, *,
           max_rounds: int = DEFAULT_MAX_ROUNDS,
           repeat_limit: int = DEFAULT_REPEAT_LIMIT,
           model: str = API.DEFAULT_MODEL,
           temperature: float = API.DEFAULT_TEMPERATURE,
           retries: int = API.DEFAULT_RETRIES,
           project_meta: dict | None = None,
           write_files: bool = True,
           archive: bool = True,
           log_fn=print) -> LoopResult:
    """跑完整的「生成 → 探测 → 回喂」循环。"""
    from quench import build_project_json, write_project  # 延迟导入避免循环依赖

    res = LoopResult()
    res.project_dir = Path(project_dir)
    t_start = time.time()

    baseline_tb_hash = ""
    latest_tb_src = ""
    tracker = GUARD.RepeatTracker(limit=repeat_limit, consecutive=True)
    feedback = ""
    previous_dut = ""
    liveness_cache: tuple[bool, str] | None = None

    for round_no in range(1, max_rounds + 1):
        log_fn("")
        log_fn("=" * 66)
        log_fn(f"第 {round_no}/{max_rounds} 轮")
        log_fn("=" * 66)

        round_info: dict = {"round": round_no, "tokens": 0, "seconds": 0.0,
                            "outcome": "", "tb_changed": False}
        t_round = time.time()

        # ---------------- 1. 生成 ----------------
        messages = [
            {"role": "system", "content": PROMPT.SYSTEM_PROMPT},
            {"role": "user", "content": PROMPT.build_user_prompt(
                requirement, feedback, round_no)},
        ]
        try:
            log_fn(f"  调用 {model} …")
            reply = API.chat(messages, model=model, temperature=temperature, retries=retries)
        except API.ApiError as exc:
            round_info.update(outcome="api-error", failure_kind="API 调用失败")
            res.rounds.append(round_info)
            res.stop_reason = f"API 调用失败：{exc}"
            log_fn(f"  [错误] {exc}")
            break

        usage = reply.get("usage") or {}
        round_info["tokens"] = usage.get("total_tokens", 0)
        res.tokens += round_info["tokens"]
        log_fn(f"  回复 {len(reply['text'])} 字符，{round_info['tokens']} tokens")

        # 解析（含真编译检查）
        files, issues = PARSE.parse_response(reply["text"], compile_check=True)
        for i in issues:
            tag = "错误" if i.level == "error" else "提醒"
            log_fn(f"    [{tag}] {i.message}")

        # 解析/编译失败 → 回喂重试
        if PARSE.has_blocking_errors(issues) or not files:
            errs = [i for i in issues if i.level == "error"]
            compile_out = "\n".join(
                (i.fix or "") for i in errs if i.code == "compile/failed") or \
                "\n".join(i.message for i in errs)
            fp = GUARD.error_fingerprint(compile_out)
            round_info.update(outcome="compile-failed", failure_kind="编译失败",
                              fingerprint=fp, headline=errs[0].message if errs else "解析失败")
            res.rounds.append(round_info)

            if tracker.add(fp):
                res.stop_reason = f"同一个编译错误已连续出现 {repeat_limit} 次，停止"
                log_fn(f"  ⛔ {res.stop_reason}")
                log_fn(f"     {tracker.describe()}")
                _archive(archive, project_dir, round_no, files, reply["text"],
                         round_info, log_fn)
                break

            feedback = FB.build_feedback(
                round_no=round_no, max_rounds=max_rounds,
                compile_output=compile_out,
                failure_kind="编译失败",
                previous_dut=previous_dut or (files.get(PARSE.DUT_NAME) or ""),
                tb_hash=baseline_tb_hash or "(本轮未锁定)",
                repeated=tracker.repeats_of(fp),
            )
            _archive(archive, project_dir, round_no, files, reply["text"],
                     round_info, log_fn)
            log_fn(f"  ↻ 回喂重试（指纹 {fp}）")
            continue

        # 统一顶层模块名，让文件名与模块名一致（否则 verilator 报 DECLFILENAME）
        import json as _json
        fixed_dut, fixed_tb, rename_note = normalize_top_name(
            files[PARSE.DUT_NAME], files[PARSE.TB_NAME], "dut")
        if rename_note:
            # 改名有可能把代码改坏，所以先拿改名后的版本重新编译检查一遍
            _, recheck = PARSE.parse_response(
                _json.dumps({"dut": fixed_dut, "tb": fixed_tb}, ensure_ascii=False),
                compile_check=True)
            bad = [i for i in recheck if i.level == "error"]
            if bad:
                log_fn(f"  ⛔ 改名后编译不过（{bad[0].message[:60]}），保留原名")
            else:
                log_fn(f"  ℹ {rename_note}")
                files[PARSE.DUT_NAME], files[PARSE.TB_NAME] = fixed_dut, fixed_tb

        dut_src = files[PARSE.DUT_NAME]
        tb_src = files[PARSE.TB_NAME]
        round_info["seconds"] = time.time() - t_round
        # 注意：latest_tb_src 必须在**削弱比对之后**才更新，
        # 否则就成了"自己和自己比"，永远判不出削弱（这个顺序坑实测踩到过）。
        # 策略：**变更不是原罪，削弱才是。**
        # 实测遇到过两种改动：
        #   · 模型把比较对象从"设计输出"换成"自己写死的期望数组" → 真削弱，必须拦
        #   · 模型发现测试平台自己的时序写错了去修 → 可能合理
        # 所以改成：改了就先做削弱检测，变松了就拒绝；没变松则放行并重新锁定。
        if baseline_tb_hash:
            changed, new_hash = GUARD.tb_changed(baseline_tb_hash, tb_src)
            if changed:
                weaker, why = GUARD.tb_is_weaker(latest_tb_src, tb_src)
                if weaker:
                    round_info.update(outcome="tb-tampered", failure_kind="削弱测试",
                                      tb_changed=True, fingerprint="tb-tampered",
                                      headline=f"tb 被改松：{'；'.join(why)[:80]}")
                    res.rounds.append(round_info)
                    if tracker.add("tb-tampered"):
                        res.stop_reason = f"模型反复试图削弱测试，已连续 {repeat_limit} 次，停止"
                        log_fn(f"  ⛔ {res.stop_reason}")
                        # 停止前也要存档！否则最后一轮的证据就丢了 ——
                        # 结果就是 summary 说"用了 5 轮"、但 runs 里只有 4 个 round 目录，
                        # 界面翻不到最后一轮（这个不一致实测抓到过）。
                        _archive(archive, project_dir, round_no, files,
                                 reply["text"], round_info, log_fn)
                        break
                    log_fn(f"  ⛔ 测试平台被改松（{baseline_tb_hash} → {new_hash}），本轮拒绝")
                    for w in why:
                        log_fn(f"     · {w}")
                    _archive(archive, project_dir, round_no, files, reply["text"],
                             round_info, log_fn)
                    feedback = FB.build_tb_tamper_message(baseline_tb_hash, new_hash) \
                        + "\n\n## 检测到的具体削弱\n\n" \
                        + "\n".join(f"- {w}" for w in why) \
                        + ("\n\n特别注意：**不要拿硬编码的期望数组互相比较** —— "
                           "那只能证明你写的常量自洽，证明不了设计是对的。"
                           "一定要比较**设计实际输出的信号**。\n")
                    previous_dut = dut_src
                    continue
                log_fn(f"  ⚠ 测试平台有改动（{baseline_tb_hash} → {new_hash}），"
                       f"但未检测到削弱，放行并重新锁定")
                baseline_tb_hash = new_hash
                res.tb_hash = new_hash
        # 比对完成，更新"最近一次看到的版本"，供下一轮比较
        latest_tb_src = tb_src

        # ---------------- 3. 阴性对照：测试是不是"活的" ----------------
        # 顺序很关键：**必须在锁定基线之前**做。
        # 否则第一轮就交一个「永远为真」的测试时，会被当成合法基线锁住，
        # 之后就再也判不出它是死的了（这个顺序错误实测踩到过）。
        if liveness_cache is None:
            dut_mod = _module_name(dut_src)
            log_fn("  阴性对照：用「输出恒为 0」的桩替换 dut，看测试会不会失败…")
            liveness_cache = _run_negative_control(
                dut_src, tb_src, dut_mod, Path(project_dir), log_fn)
            alive, msg = liveness_cache
            if alive:
                log_fn(f"    ✓ {msg}")
            else:
                log_fn(f"    ⛔ {msg}")
                round_info.update(outcome="dead-test", failure_kind="测试没有验证力",
                                  fingerprint="dead-test", headline=msg)
                res.rounds.append(round_info)
                if tracker.add("dead-test"):
                    res.stop_reason = "测试平台没有验证力（换桩设计照样通过），停止"
                    log_fn(f"  ⛔ {res.stop_reason}")
                    break
                feedback = FB.build_feedback(
                    round_no=round_no, max_rounds=max_rounds,
                    failure_kind="测试平台没有验证力",
                    previous_dut=dut_src,
                    tb_hash=baseline_tb_hash or "(尚未锁定)",
                    repeated=tracker.repeats_of("dead-test"),
                ) + ("\n\n## 这次的具体问题\n\n"
                     "我把 dut.v 换成一个「所有输出恒为 0、什么都不做」的桩设计，"
                     "你的测试平台**依然报告通过**。这说明断言没有真正检查设计行为。\n\n"
                     "请检查：\n"
                     "1. 断言条件是不是恒为真（例如 `if (0)`、比较错了信号）\n"
                     "2. 是否真的在错误时把 `errors` 累加上去了\n"
                     "3. 是否在信号稳定之后才比较\n")
                _archive(archive, project_dir, round_no, files, "", round_info, log_fn)
                previous_dut = dut_src
                continue

        # 阴性对照过了，这才锁定基线
        if not baseline_tb_hash:
            baseline_tb_hash = GUARD.tb_hash(tb_src)
            res.tb_hash = baseline_tb_hash
            log_fn(f"  锁定测试平台哈希：{baseline_tb_hash}")

        # ---------------- 4. 落盘 + 探测 ----------------
        # 注意：**探测前必须写盘**，因为 refresh.py 是从磁盘读文件去编译的。
        # 若跳过写盘直接探测，测的就是目录里上一次留下的旧内容 ——
        # 探测结果和实际生成的东西对不上（这个坑实测踩到过）。
        # write_files=False 的语义是「探完不留」，不是「不写也能探」。
        log_fn("  写入工程目录 …")
        # top 必须取**实际生成出来的顶层模块名**，不能写死 "dut"。
        # 提示词虽然要求模块叫 dut，但模型不一定听 —— 写死的话 yosys 的
        # `synth -top dut` 会找不到模块，探测必挂（这个 bug 实测抓到过）。
        top_name = _module_name(dut_src)
        project_meta_round = project_meta or build_project_json(
            project_dir.name, top_name)
        try:
            write_project(Path(project_dir), files, project_meta_round,
                          overwrite=True, log=lambda s: None)
        except Exception as exc:  # noqa: BLE001
            round_info.update(outcome="write-failed", failure_kind="写入失败",
                              headline=str(exc))
            res.rounds.append(round_info)
            res.stop_reason = f"写入工程目录失败：{exc}"
            break

        log_fn("  真探测（iverilog / verilator / yosys）…")
        all_ok, probe_log, board = run_probe(Path(project_dir), log_fn)
        headline = first_bad_headline(board) if not all_ok else "五步全绿"

        # ---------------- 4. 通过 ----------------
        if all_ok:
            round_info.update(outcome="ok", headline=headline)
            res.rounds.append(round_info)
            res.ok = True
            res.final_dut, res.final_tb = dut_src, tb_src
            res.stop_reason = f"第 {round_no} 轮通过"
            log_fn(f"  ✅ 第 {round_no} 轮通过")
            _archive(archive, project_dir, round_no, files, probe_log, round_info, log_fn)
            break

        # ---------------- 5. 探测失败 → 回喂 ----------------
        sim_out = sim_failure_output(board)
        fp = GUARD.error_fingerprint(sim_out or probe_log)
        round_info.update(outcome="probe-failed", failure_kind="探测未通过",
                          fingerprint=fp, headline=headline)
        res.rounds.append(round_info)
        log_fn(f"  ✗ 未通过：{headline}")

        if tracker.add(fp):
            res.stop_reason = f"同一个失败已连续出现 {repeat_limit} 次，停止"
            log_fn(f"  ⛔ {res.stop_reason}")
            _archive(archive, project_dir, round_no, files, probe_log, round_info, log_fn)
            break

        feedback = FB.build_feedback(
            round_no=round_no, max_rounds=max_rounds,
            sim_log=sim_out,
            probe_log=probe_log,
            failure_kind="探测未通过",
            previous_dut=dut_src,
            tb_hash=baseline_tb_hash,
            repeated=tracker.repeats_of(fp),
        )
        previous_dut = dut_src
        _archive(archive, project_dir, round_no, files, probe_log, round_info, log_fn)
        round_info["seconds"] = time.time() - t_round
        log_fn(f"  ↻ 回喂重试（指纹 {fp}）")

    # 收尾统计
    for r in res.rounds:
        if not r.get("seconds"):
            r["seconds"] = 0.0
    res.seconds = time.time() - t_start
    if not res.stop_reason:
        res.stop_reason = f"用满 {max_rounds} 轮仍未通过"

    if archive:
        try:
            RUNS.write_summary(project_dir, res.summary(), log_fn=log_fn)
        except Exception as exc:  # noqa: BLE001
            log_fn(f"  （写总结失败：{exc}）")
    return res


def _archive(enabled: bool, project_dir: Path, round_no: int,
             files: dict, log: str, meta: dict, log_fn):
    if not enabled:
        return
    try:
        RUNS.write_round(project_dir, round_no,
                         dut=files.get(PARSE.DUT_NAME, ""),
                         tb=files.get(PARSE.TB_NAME, ""),
                         log=log, result=meta, meta=meta, log_fn=log_fn)
    except Exception as exc:  # noqa: BLE001
        log_fn(f"  （存档失败：{exc}）")


if __name__ == "__main__":
    print("这是编排模块。用 quench.py retry 子命令来跑：")
    print("    python quench.py retry --from-file 需求.txt --name my-proj")
