#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_config.py 的契约测试 —— 覆盖通过与各类失败情形。

用法: python test_project_config.py
退出码 0 = 全部符合预期，1 = 有用例不符
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VALIDATOR = HERE / "project_config.py"

# 每个用例: (名称, 写进去的文件内容或 None, 额外要创建的文件, 期望通过?, 期望错误码包含)
CASES = [
    (
        "⓪ 配置带 UTF-8 BOM（记事本 / PowerShell 存出来的）",
        "\ufeff" + json.dumps({"name": "a", "top": "a", "dut": "adder.v",
                               "tb": "tb_adder.v", "vcd": "a.vcd"}, ensure_ascii=False),
        ["adder.v", "tb_adder.v"],
        True, [],
    ),
    (
        "① 只填必填项，其余走默认值",
        {"name": "adder", "top": "adder", "dut": "adder.v", "tb": "tb_adder.v", "vcd": "adder.vcd"},
        ["adder.v", "tb_adder.v"],
        True, [],
    ),
    (
        "② 缺 project.json",
        None,
        [],
        False, ["config/missing"],
    ),
    (
        "③ JSON 语法错（多了一个逗号）",
        '{"name": "adder", "top": "adder",}',
        [],
        False, ["config/bad-json"],
    ),
    (
        "④ 缺必填字段 top 和 vcd",
        {"name": "adder", "dut": "adder.v", "tb": "tb_adder.v"},
        ["adder.v", "tb_adder.v"],
        False, ["field/missing:top", "field/missing:vcd"],
    ),
    (
        "⑤ 类型错：timeout 写成了字符串",
        {"name": "a", "top": "a", "dut": "adder.v", "tb": "tb_adder.v", "vcd": "a.vcd",
         "timeout": "240"},
        ["adder.v", "tb_adder.v"],
        False, ["field/type:timeout"],
    ),
    (
        "⑥ 类型错：version 写成了字符串",
        {"version": "1", "name": "a", "top": "a", "dut": "adder.v", "tb": "tb_adder.v",
         "vcd": "a.vcd"},
        ["adder.v", "tb_adder.v"],
        False, ["version/type"],
    ),
    (
        "⑦ 文件不存在：dut 指错了名字",
        {"name": "a", "top": "a", "dut": "nope.v", "tb": "tb_adder.v", "vcd": "a.vcd"},
        ["adder.v", "tb_adder.v"],
        False, ["file/not-found:dut"],
    ),
    (
        "⑧ timeout 超出范围",
        {"name": "a", "top": "a", "dut": "adder.v", "tb": "tb_adder.v", "vcd": "a.vcd",
         "timeout": 5},
        ["adder.v", "tb_adder.v"],
        False, ["field/range:timeout"],
    ),
    (
        "⑨ 未知字段只警告，不算错",
        {"name": "a", "top": "a", "dut": "adder.v", "tb": "tb_adder.v", "vcd": "a.vcd",
         "whatever": 123, "_备注": "注释应被忽略"},
        ["adder.v", "tb_adder.v"],
        True, [],
    ),
    (
        "⑩ dut 与 tb 指向同一个文件",
        {"name": "a", "top": "a", "dut": "adder.v", "tb": "adder.v", "vcd": "a.vcd"},
        ["adder.v"],
        False, ["cross/dut-eq-tb"],
    ),
    (
        "⑪ 必填字段是空字符串",
        {"name": "   ", "top": "a", "dut": "adder.v", "tb": "tb_adder.v", "vcd": "a.vcd"},
        ["adder.v", "tb_adder.v"],
        False, ["field/empty:name"],
    ),
    (
        "⑫ 不支持的配置版本",
        {"version": 99, "name": "a", "top": "a", "dut": "adder.v", "tb": "tb_adder.v",
         "vcd": "a.vcd"},
        ["adder.v", "tb_adder.v"],
        False, ["version/unsupported"],
    ),
]


def run_case(name, cfg, files, expect_ok, expect_codes):
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for f in files:
            (d / f).write_text("// stub\n", encoding="utf-8")
        if cfg is not None:
            text = cfg if isinstance(cfg, str) else json.dumps(cfg, ensure_ascii=False, indent=2)
            (d / "project.json").write_text(text, encoding="utf-8")

        p = subprocess.run([sys.executable, str(VALIDATOR), str(d), "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            res = json.loads(p.stdout)
        except Exception:
            print(f"  ✗ {name}\n      校验器没吐出 JSON，stdout={p.stdout[:200]!r} stderr={p.stderr[:200]!r}")
            return False

        ok = res.get("ok")
        codes = [e["code"] for e in res.get("errors", [])]
        problems = []
        if ok != expect_ok:
            problems.append(f"通过与否不符：期望 ok={expect_ok}，实际 ok={ok}")
        for c in expect_codes:
            if not any(x.startswith(c) or c in x for x in codes):
                problems.append(f"缺少期望错误码 {c}（实际 {codes}）")

        if problems:
            print(f"  ✗ {name}")
            for x in problems:
                print(f"      {x}")
            for e in res.get("errors", []):
                print(f"      [{e['code']}] {e['message'][:70]}")
            return False
        print(f"  ✓ {name}")
        return True


def main() -> int:
    print(f"契约测试：{VALIDATOR.name}   共 {len(CASES)} 个用例\n")
    passed = sum(1 for c in CASES if run_case(*c))
    print(f"\n结果：{passed}/{len(CASES)} 通过")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
