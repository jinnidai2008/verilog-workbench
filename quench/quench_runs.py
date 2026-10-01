#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 8：轮次存档。

每跑一轮就把证据落盘，目录结构（按截图要求）：

    runs/<项目名>/round-1/
        dut.v        本轮的设计源码
        tb.v         本轮的测试平台（用于对比"有没有被偷改"）
        log.txt      完整的探测日志
        result.json  结构化的本轮结论
        meta.json    轮次元信息（用量、耗时、指纹、是否通过）

为什么值得单独做一个模块：
  · 第 4 步要统计「一次通过率 / 平均迭代次数 / 错误类型分布」——全靠这些存档
  · 第 5 步界面要「翻看每一轮：看 diff、看错误类型、看第几轮修好的」——也全靠它
  所以存档格式定下来之后，后面两步不用再改数据层。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

RUNS_DIRNAME = "runs"


def runs_root(project_dir: Path | str) -> Path:
    """存档根目录。放在工程目录的上一级还是里面？

    放**上一级**：工程目录要保持干净（refresh.py 第 5 步会扫描产物目录并列出文件，
    塞一堆 rounds 进去会把「产物清单」弄脏）。
    """
    return Path(project_dir).resolve().parent / RUNS_DIRNAME / Path(project_dir).resolve().name


def round_dir(project_dir: Path | str, round_no: int) -> Path:
    return runs_root(project_dir) / f"round-{round_no}"


def ensure_trailing_newline(s: str) -> str:
    """difflib 要求每一行都带换行，否则会把两行粘成一行。"""
    if not s:
        return ""
    return s if s.endswith("\n") else s + "\n"


def write_round(project_dir: Path | str, round_no: int, *,
                dut: str = "", tb: str = "", log: str = "",
                result: dict | None = None, meta: dict | None = None,
                log_fn=print) -> Path:
    """写一轮的存档。返回该轮目录。已存在则覆盖（重跑同一轮时用）。"""
    d = round_dir(project_dir, round_no)
    d.mkdir(parents=True, exist_ok=True)

    if dut:
        (d / "dut.v").write_text(dut, encoding="utf-8")
    if tb:
        (d / "tb.v").write_text(tb, encoding="utf-8")
    (d / "log.txt").write_text(log or "", encoding="utf-8")

    payload = dict(result or {})
    payload.setdefault("round", round_no)
    payload.setdefault("archivedAt", time.strftime("%Y-%m-%d %H:%M:%S"))
    (d / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (d / "meta.json").write_text(
        json.dumps(dict(meta or {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_fn(f"  存档 → {d}")
    return d


def read_round(project_dir: Path | str, round_no: int) -> dict:
    """读回一轮存档。缺文件时对应键为 None，不抛异常（界面翻旧档时要容错）。"""
    d = round_dir(project_dir, round_no)
    out: dict = {"dir": str(d), "exists": d.is_dir()}
    for key, fname in (("dut", "dut.v"), ("tb", "tb.v"), ("log", "log.txt")):
        f = d / fname
        out[key] = f.read_text(encoding="utf-8") if f.is_file() else None
    for key, fname in (("result", "result.json"), ("meta", "meta.json")):
        f = d / fname
        try:
            out[key] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
        except Exception:  # noqa: BLE001
            out[key] = None
    return out


def list_rounds(project_dir: Path | str) -> list[int]:
    """列出已经存档的轮次编号（升序）。"""
    root = runs_root(project_dir)
    if not root.is_dir():
        return []
    nums = []
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith("round-"):
            try:
                nums.append(int(child.name.split("-", 1)[1]))
            except ValueError:
                continue
    return sorted(nums)


def latest_round(project_dir: Path | str) -> int | None:
    nums = list_rounds(project_dir)
    return nums[-1] if nums else None


def unified_diff(a: str, b: str, a_name: str = "上一轮", b_name: str = "这一轮") -> str:
    """两轮源码的 diff —— 第 5 步界面要显示的东西，这里先做好。"""
    import difflib
    lines = difflib.unified_diff(
        ensure_trailing_newline(a or "").splitlines(keepends=True),
        ensure_trailing_newline(b or "").splitlines(keepends=True),
        fromfile=a_name, tofile=b_name, n=2)
    return "".join(lines)


def write_summary(project_dir: Path | str, summary: dict, log_fn=print) -> Path:
    """把整次生成（所有轮次）的总结果写到 runs/<项目>/summary.json。

    第 4 步的统计报告、第 5 步的界面都读这个文件。
    """
    root = runs_root(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    f = root / "summary.json"
    payload = dict(summary)
    payload.setdefault("updatedAt", time.strftime("%Y-%m-%d %H:%M:%S"))
    payload.setdefault("projectDir", str(Path(project_dir).resolve()))
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_fn(f"  总结 → {f}")
    return f


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "demo-proj"
        proj.mkdir()
        print("存档根目录:", runs_root(proj))
        write_round(proj, 1, dut="module dut; endmodule", tb="module tb; endmodule",
                    log="第 1 轮日志", result={"ok": False, "errors": 1},
                    meta={"tokens": 1234})
        write_round(proj, 2, dut="module dut; /* 修好了 */ endmodule",
                    tb="module tb; endmodule", log="第 2 轮日志",
                    result={"ok": True}, meta={"tokens": 2345})
        print("已存档轮次:", list_rounds(proj))
        print("最新轮次  :", latest_round(proj))
        r = read_round(proj, 1)
        print("第 1 轮 result:", r["result"])
        print()
        print("两轮 diff（第 5 步界面要用的）:")
        print(unified_diff(r["dut"], read_round(proj, 2)["dut"]))
