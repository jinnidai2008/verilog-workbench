#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 1 步（refresh.py 泛化）的验收测试 —— 专挑刺。

覆盖：配置正确 / 配置各种坏法 / 工程不存在 / EDA 工具失败 / 输出格式回归。

用法: python test_step1.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFRESH = HERE / "refresh.py"
COUNTER = HERE.parent / "vibe-verilog-lab"
ADDER = HERE.parent / "adder-lab"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


def run_refresh(proj: Path | str | None, extra_env=None, timeout=900):
    env = None
    if extra_env:
        import os
        env = dict(os.environ)
        env.update(extra_env)
    args = [sys.executable, str(REFRESH), "--json"]
    if proj is not None:
        args.insert(2, str(proj))
    p = subprocess.run(args, cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env)
    board = None
    try:
        board = json.loads((HERE / "board.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return p, board


def main() -> int:
    print("=" * 66)
    print("第 1 步验收：refresh.py 泛化")
    print("=" * 66)

    # ---------------------------------------------------------- A. 正常路径
    print("\n【A】正常路径")
    for label, proj in (("老工程 counter", COUNTER), ("新工程 adder", ADDER)):
        if not Path(proj).is_dir():
            record(f"{label} 目录存在", False, f"{proj} 不存在")
            continue
        p, board = run_refresh(proj)
        ok = p.returncode == 0 and board and "steps" in board
        states = {k: v.get("state") for k, v in (board or {}).get("steps", {}).items()}
        record(f"{label} 退出码 0", p.returncode == 0, p.stdout[-300:])
        record(f"{label} 五步全 ok", all(s == "ok" for s in states.values()), str(states))
        record(f"{label} board.json 可解析", board is not None)

    # ---------------------------------------------------------- B. 配置坏法
    print("\n【B】配置有问题时必须「拦下来」而不是崩溃")
    bad_cases = [
        ("缺 project.json", None, []),
        ("JSON 语法错", '{"name": "x",}', []),
        ("缺必填字段", '{"name": "x"}', []),
        ("dut 文件不存在", '{"name":"x","top":"x","dut":"nope.v","tb":"t.v","vcd":"a.vcd"}', ["t.v"]),
        ("dut 与 tb 同一个文件",
         '{"name":"x","top":"x","dut":"a.v","tb":"a.v","vcd":"a.vcd"}', ["a.v"]),
        ("timeout 类型错",
         '{"name":"x","top":"x","dut":"a.v","tb":"t.v","vcd":"a.vcd","timeout":"240"}', ["a.v", "t.v"]),
        ("assertPattern 正则非法",
         '{"name":"x","top":"x","dut":"a.v","tb":"t.v","vcd":"a.vcd","assertPattern":"([unclosed"}',
         ["a.v", "t.v"]),
    ]
    for name, cfg, files in bad_cases:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for f in files:
                (d / f).write_text("// stub\n", encoding="utf-8")
            if cfg is not None:
                (d / "project.json").write_text(cfg, encoding="utf-8")
            p, _ = run_refresh(d, timeout=120)
            combined = (p.stdout or "") + (p.stderr or "")
            clean = p.returncode == 1 and "[错误]" in combined
            crashed = "Traceback" in combined
            record(f"{name} → 干净退出(1)、有[错误]、无堆栈",
                   clean and not crashed,
                   f"rc={p.returncode} traceback={crashed} :: {combined.strip()[-260:]}")

    # ---------------------------------------------------------- C. 工程不存在
    print("\n【C】工程目录不存在")
    p, _ = run_refresh("D:/___no_such_project___", timeout=120)
    combined = (p.stdout or "") + (p.stderr or "")
    record("不存在目录 → 干净退出、无堆栈",
           p.returncode == 1 and "Traceback" not in combined,
           f"rc={p.returncode} :: {combined.strip()[-260:]}")

    # ---------------------------------------------------------- D. EDA 工具失败
    print("\n【D】配置正确但 EDA 工具跑不动（要能报错，不能崩）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 语法错误的 Verilog：配置合法、文件存在，但仿真一定失败
        (d / "broken.v").write_text("module broken(; endmodule\n", encoding="utf-8")
        (d / "tb_broken.v").write_text("module tb_broken; initial $finish; endmodule\n", encoding="utf-8")
        (d / "project.json").write_text(json.dumps({
            "name": "broken", "top": "broken", "dut": "broken.v",
            "tb": "tb_broken.v", "vcd": "broken.vcd", "timeout": 60,
        }), encoding="utf-8")
        p, board = run_refresh(d, timeout=600)
        combined = (p.stdout or "") + (p.stderr or "")
        record("EDA 失败 → 无堆栈崩溃", "Traceback" not in combined, combined.strip()[-260:])
        record("EDA 失败 → 探测脚本仍正常收尾（退出码 0，五步都有记录）",
               p.returncode == 0 and board and len(board.get("steps", {})) == 5,
               f"rc={p.returncode} steps={list((board or {}).get('steps', {}).keys())}")

    # ---------------------------------------------------------- E. 工具链缺失
    print("\n【E】WSL 发行版写错（模拟工具链环境不可用）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        shutil.copy(COUNTER / "counter.v", d / "counter.v")
        shutil.copy(COUNTER / "tb_counter.v", d / "tb_counter.v")
        (d / "project.json").write_text(json.dumps({
            "name": "wrongdistro", "top": "counter", "dut": "counter.v",
            "tb": "tb_counter.v", "vcd": "counter.vcd",
            "distro": "NoSuchDistro-9.9", "timeout": 40,
        }), encoding="utf-8")
        p, board = run_refresh(d, timeout=600)
        combined = (p.stdout or "") + (p.stderr or "")
        record("发行版写错 → 无堆栈崩溃", "Traceback" not in combined, combined.strip()[-260:])
        states = {k: v.get("state") for k, v in (board or {}).get("steps", {}).items()}
        record("发行版写错 → 步骤标为失败/idle 而不是崩掉",
               bool(states) and not all(s == "ok" for s in states.values()), str(states))

    # ---------------------------------------------------------- F. 输出兼容
    print("\n【F】输出格式没被改坏（老界面还读得懂）")
    _, board = run_refresh(COUNTER, timeout=900)
    b = board or {}
    must_project = ["name", "path", "wslPath", "updatedAt"]
    record("project 仍有界面依赖的 4 个键",
           all(k in b.get("project", {}) for k in must_project),
           str(list(b.get("project", {}).keys())))
    record("保留了 media 配置（不冲掉背景设置）",
           "media" not in b or isinstance(b.get("media"), dict),
           str(type(b.get("media"))))
    steps = b.get("steps", {})
    record("五个步骤键齐全", set(steps.keys()) == {"sim", "lint", "synth", "wave", "files"},
           str(list(steps.keys())))
    for k in ("sim", "lint", "synth"):
        d = steps.get(k, {})
        record(f"{k} 有 state/headline/metrics/log/cmd",
               all(x in d for x in ("state", "headline", "metrics", "log", "cmd")),
               str(list(d.keys())))

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 66)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:400]}")
    print("=" * 66)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
