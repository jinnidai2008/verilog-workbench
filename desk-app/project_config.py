#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project.json 配置契约 —— 单一事实来源 + 独立校验器。

契约目标：把「探测哪个工程、工程里叫什么」从脚本里搬到工程自己的配置文件里，
让 refresh.py 能对任意 Verilog 工程工作。

用法:
    python project_config.py <工程目录>        # 校验，输出人话报告
    python project_config.py <工程目录> --json # 输出机器可读结果
退出码: 0 通过 / 1 配置有问题 / 2 用法错误

这一层不执行任何 EDA 命令，只做「读 + 校验 + 合并默认值」。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CONFIG_NAME = "project.json"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 契约定义
# required=True 的字段缺失就报错；有 default 的字段可省略。
FIELDS = {
    "version": {
        "type": int,
        "default": SCHEMA_VERSION,
        "desc": "配置格式版本。现在只支持 1，留作以后不兼容升级的判据。",
    },
    "name": {
        "type": str,
        "required": True,
        "nonempty": True,
        "desc": "项目名。显示在界面上，同时在 board.json 里标识这是哪个项目。",
    },
    "top": {
        "type": str,
        "required": True,
        "nonempty": True,
        "desc": "综合的顶层模块名，对应 yosys 的 synth -top <top>。必须是 Verilog 里 module 后面的名字，不是文件名。",
    },
    "dut": {
        "type": str,
        "required": True,
        "nonempty": True,
        "file": True,
        "desc": "被测设计的源文件路径，相对工程目录。",
    },
    "tb": {
        "type": str,
        "required": True,
        "nonempty": True,
        "file": True,
        "desc": "测试平台文件路径，相对工程目录。",
    },
    "vcd": {
        "type": str,
        "required": True,
        "nonempty": True,
        "desc": "波形文件名。必须和测试平台里 $dumpfile(\"...\") 写的一致。",
    },
    "assertPattern": {
        "type": str,
        "default": r"\b\w+\s*=\s*(\d+)",
        "nonempty": True,
        "desc": "从仿真输出里抓检查点的正则（第一个捕获组是要取的数字）。"
                "默认抓 `名字 = 数字` 这种打印；如果你的测试平台打印格式不同，改这里。",
    },
    "timeout": {
        "type": int,
        "default": 240,
        "min": 10,
        "max": 3600,
        "desc": "每条 EDA 命令的超时（秒）。大工程综合慢就调大。",
    },
    "distro": {
        "type": str,
        "default": "Ubuntu-24.04",
        "nonempty": True,
        "desc": "运行工具链的 WSL 发行版名（wsl -l -v 里看到的那个）。",
    },
}

TYPE_CN = {str: "字符串", int: "整数", bool: "布尔", list: "数组", dict: "对象"}


class Issue:
    def __init__(self, level: str, code: str, message: str, fix: str = ""):
        self.level = level          # "error" | "warn"
        self.code = code
        self.message = message
        self.fix = fix

    def as_dict(self):
        d = {"level": self.level, "code": self.code, "message": self.message}
        if self.fix:
            d["fix"] = self.fix
        return d


def _type_ok(value, expected) -> bool:
    if expected is int:
        # 注意 bool 是 int 的子类，这里要排除，避免 true 被当成合法数字
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def load_config(project_dir: Path):
    """读 project.json。返回 (raw_dict, issues)。文件不存在也算一种 issue。"""
    issues: list[Issue] = []
    cfg_path = project_dir / CONFIG_NAME
    if not cfg_path.is_file():
        issues.append(Issue(
            "error", "config/missing",
            f"工程目录里没有 {CONFIG_NAME}：{project_dir}",
            f"照着 desk-app/project.example.json 建一个，至少要有 name / top / dut / tb / vcd 五项。",
        ))
        return None, issues
    try:
        # 用 utf-8-sig：Windows 上记事本 / PowerShell 存 UTF-8 常带 BOM，
        # 直接按 utf-8 读会报 "Unexpected UTF-8 BOM"，那对用户是莫名其妙的一堵墙。
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        issues.append(Issue(
            "error", "config/bad-json",
            f"{CONFIG_NAME} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}",
            "常见原因：多写了一个逗号、用了单引号、漏了引号，或存成了带 BOM 的编码。",
        ))
        return None, issues
    except UnicodeDecodeError as exc:
        issues.append(Issue(
            "error", "config/bad-encoding",
            f"{CONFIG_NAME} 不是 UTF-8 编码：{exc}",
            "用记事本「另存为」时把编码选成 UTF-8。",
        ))
        return None, issues
    if not isinstance(raw, dict):
        issues.append(Issue(
            "error", "config/not-object",
            f"{CONFIG_NAME} 的最外层必须是一个对象（用 {{ }} 包起来），现在是 {TYPE_CN.get(type(raw), type(raw).__name__)}。",
        ))
        return None, issues
    return raw, issues


def validate(raw: dict, project_dir: Path):
    """校验 + 合并默认值。返回 (resolved_dict, issues)。"""
    issues: list[Issue] = []
    resolved: dict = {}

    # 1) 版本先看，版本不对后面就不要再报了
    if "version" in raw:
        v = raw["version"]
        if not _type_ok(v, int):
            issues.append(Issue("error", "version/type",
                                f"version 必须是整数，现在是 {TYPE_CN.get(type(v), type(v).__name__)}。",
                                f"改成 {SCHEMA_VERSION}。"))
        elif v != SCHEMA_VERSION:
            issues.append(Issue("error", "version/unsupported",
                                f"不支持的配置版本 {v}，当前工具只认 {SCHEMA_VERSION}。",
                                "要么升级工具，要么把 version 改成 1。"))

    # 2) 未知字段：只警告，不当错误（向前兼容，也让 _注释 之类能活下来）
    for key in raw:
        if key not in FIELDS:
            if key.startswith("_"):
                continue
            issues.append(Issue("warn", "field/unknown",
                                f"未知字段 \"{key}\"，工具会忽略它。",
                                f"已知字段：{', '.join(FIELDS)}"))

    # 3) 逐字段校验
    for key, spec in FIELDS.items():
        present = key in raw
        if not present:
            if spec.get("required"):
                issues.append(Issue("error", f"field/missing:{key}",
                                    f"缺少必填字段 \"{key}\"：{spec['desc']}",
                                    f"在 {CONFIG_NAME} 里加上 \"{key}\": ..."))
                continue
            resolved[key] = spec.get("default")
            continue

        value = raw[key]
        expected = spec["type"]
        if not _type_ok(value, expected):
            issues.append(Issue("error", f"field/type:{key}",
                                f"\"{key}\" 应为{TYPE_CN[expected]}，实际是"
                                f"{TYPE_CN.get(type(value), type(value).__name__)}（{value!r}）。",
                                f"{spec['desc']}"))
            continue

        if spec.get("nonempty") and isinstance(value, str) and not value.strip():
            issues.append(Issue("error", f"field/empty:{key}",
                                f"\"{key}\" 不能是空字符串。", f"{spec['desc']}"))
            continue

        if "min" in spec and value < spec["min"]:
            issues.append(Issue("error", f"field/range:{key}",
                                f"\"{key}\" 不能小于 {spec['min']}，现在是 {value}。",
                                f"{spec['desc']}"))
            continue
        if "max" in spec and value > spec["max"]:
            issues.append(Issue("error", f"field/range:{key}",
                                f"\"{key}\" 不能大于 {spec['max']}，现在是 {value}。",
                                f"{spec['desc']}"))
            continue

        # 文件类字段：确认真的存在
        if spec.get("file"):
            target = (project_dir / value).resolve()
            if not target.is_file():
                same_ext = sorted(p.name for p in project_dir.glob("*.v")) if project_dir.is_dir() else []
                hint = ("工程目录里现有的 .v 文件：" + "、".join(same_ext)) if same_ext \
                    else "工程目录里一个 .v 文件都没有。"
                issues.append(Issue("error", f"file/not-found:{key}",
                                    f"\"{key}\" 指向的文件不存在：{value}（完整路径 {target}）",
                                    hint))
                continue

        resolved[key] = value

    # 4) 交叉检查：dut / tb 不能是同一个文件
    if resolved.get("dut") and resolved.get("tb") and resolved["dut"] == resolved["tb"]:
        issues.append(Issue("error", "cross/dut-eq-tb",
                            f"dut 和 tb 指向同一个文件（{resolved['dut']}）。",
                            "被测设计和测试平台必须是两个文件。"))

    # 5) assertPattern 必须能编译，否则等跑到一半才炸就很难查
    if resolved.get("assertPattern"):
        try:
            re.compile(resolved["assertPattern"])
        except re.error as exc:
            issues.append(Issue("error", "field/bad-regex:assertPattern",
                                f"assertPattern 不是合法正则：{exc}",
                                "检查括号和反斜杠；默认值是 \\b\\w+\\s*=\\s*(\\d+)"))

    return resolved, issues


def report(project_dir: Path, resolved, issues, as_json: bool) -> int:
    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warn"]

    if as_json:
        print(json.dumps({
            "projectDir": str(project_dir),
            "ok": not errors,
            "config": resolved if not errors else None,
            "errors": [i.as_dict() for i in errors],
            "warnings": [i.as_dict() for i in warns],
        }, ensure_ascii=False, indent=2))
        return 1 if errors else 0

    print(f"校验配置：{project_dir / CONFIG_NAME}")
    if errors:
        print()
        for i in errors:
            print(f"  [错误] {i.message}")
            if i.fix:
                print(f"         建议：{i.fix}")
    if warns:
        print()
        for i in warns:
            print(f"  [提醒] {i.message}")
            if i.fix:
                print(f"         建议：{i.fix}")
    print()
    if errors:
        print(f"未通过：{len(errors)} 个错误，{len(warns)} 个提醒。")
        return 1
    print("配置有效。解析结果（已合并默认值）：")
    for key in FIELDS:
        if key in resolved:
            print(f"    {key:<9} = {resolved[key]!r}")
    if warns:
        print(f"（另有 {len(warns)} 个提醒，不影响使用）")
    return 0


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv[1:]
    if len(argv) != 1:
        print(__doc__)
        return 2
    project_dir = Path(argv[0]).resolve()
    if not project_dir.is_dir():
        print(f"不是有效的目录：{project_dir}", file=sys.stderr)
        return 2

    raw, issues = load_config(project_dir)
    resolved = None
    if raw is not None:
        resolved, more = validate(raw, project_dir)
        issues += more
    return report(project_dir, resolved, issues, as_json)


if __name__ == "__main__":
    raise SystemExit(main())
