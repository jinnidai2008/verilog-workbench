#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检 —— 回答「我到底配好了没有、还差什么」。

为什么要有这个
--------------
别人 clone 下来之后最容易卡住的地方是：不知道缺哪个工具、
不知道该装哪个、装完也不知道对不对。README 再详细也没法替他判断。

所以做成一条命令：

    python doctor.py

它会逐项检查并直接给出**下一步该敲什么**。

检查项
------
  1. Python 版本
  2. EDA 工具链（iverilog / verilator / yosys）—— Windows 原生和 WSL 都查
  3. API key（环境变量 / 本地文件 / 网页里填的）
  4. 必要的目录和文件在不在
  5. 用一个小例子真的跑一遍（可选，--probe）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# quench 是 desk-app 的**同级**目录，不是子目录。
# 一开始写成 HERE / "quench" 了，于是自检报「没有 quench 目录」——
# 但它是存在的，只是我找错了地方。同类的错误在 gen_proxy.py 里也是
# 用 HERE.parent / "quench"，这里对齐。
QUENCH = HERE.parent / "quench"

OK, BAD, WARN, INFO = "OK  ", "缺  ", "注意", "    "


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.fixes: list[str] = []
        self.bad = 0
        self.warn = 0

    def ok(self, msg: str) -> None:
        self.lines.append((OK, msg))

    def bad(self, msg: str, fix: str = "") -> None:
        self.lines.append((BAD, msg))
        self.bad += 1
        if fix:
            self.fixes.append(fix)

    def note(self, msg: str, fix: str = "") -> None:
        self.lines.append((WARN, msg))
        self.warn += 1
        if fix:
            self.fixes.append(fix)

    def info(self, msg: str) -> None:
        self.lines.append((INFO, msg))

    def dump(self) -> None:
        for tag, msg in self.lines:
            print(f"  [{tag}] {msg}")


def check_python(r: Report) -> None:
    v = sys.version_info
    s = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        r.ok(f"Python {s}")
    else:
        r.bad(f"Python {s} 太旧了（需要 3.10 以上）",
              "去 https://www.python.org/downloads/ 装新版，"
              "安装时勾选 Add python.exe to PATH")


def _which(name: str) -> str | None:
    return shutil.which(name)


def check_tools_windows(r: Report) -> dict[str, str]:
    """Windows 侧直接找得到几个工具。"""
    found: dict[str, str] = {}
    for t in ("iverilog", "verilator", "yosys"):
        p = _which(t)
        if p:
            found[t] = p
    return found


def check_tools_wsl(r: Report) -> dict[str, str]:
    """WSL 里能不能找到工具。找不到 wsl.exe 就返回空。"""
    if os.name != "nt":
        return {}
    if not _which("wsl"):
        return {}
    found: dict[str, str] = {}
    for t in ("iverilog", "verilator", "yosys"):
        try:
            p = subprocess.run(
                ["wsl.exe", "-d", "Ubuntu-24.04", "--", "bash", "-lc",
                 f"command -v {t}"],
                capture_output=True, text=True, timeout=25,
                encoding="utf-8", errors="replace",
            )
            out = (p.stdout or "").strip()
            if out and "/" in out:
                found[t] = out
        except Exception:  # noqa: BLE001
            pass
    return found


def check_toolchain(r: Report) -> None:
    need = ("iverilog", "verilator", "yosys")
    win = check_tools_windows(r)
    wsl = check_tools_wsl(r)

    if all(t in win for t in need):
        r.ok("EDA 工具链（Windows 原生）：iverilog / verilator / yosys 都在")
        for t in need:
            r.info(f"      {t:10} -> {win[t]}")
        return
    if all(t in wsl for t in need):
        r.ok("EDA 工具链（WSL）：iverilog / verilator / yosys 都在")
        for t in need:
            r.info(f"      {t:10} -> {wsl[t]}")
        r.info("      启动探测时加 --backend wsl")
        return

    have = sorted(set(win) | set(wsl))
    if have:
        r.note(f"EDA 工具链只装了一部分：有 {'、'.join(have)}，"
               f"缺 {'、'.join(t for t in need if t not in have)}",
               "补齐缺的几个（见下），或者直接用便携包一次装全")
    else:
        r.bad("EDA 工具链一个都没有（iverilog / verilator / yosys）")

    r.info("")
    r.info("      两条路选一条：")
    r.info("      (A) Windows 便携包 —— 最省事，不用 WSL")
    r.info("          下载 https://github.com/YosysHQ/oss-cad-suite-build/releases")
    r.info("          里的 oss-cad-suite-windows-x64（约 570 MB），解压到任意目录，")
    r.info("          然后这样用：")
    r.info("          python refresh.py --backend windows "
           "--toolchain-dir <解压目录>\\bin")
    r.info("      (B) 用 WSL")
    r.info("          wsl -d Ubuntu-24.04 -- sudo apt update")
    r.info("          wsl -d Ubuntu-24.04 -- sudo apt install -y "
           "iverilog verilator yosys")
    r.info("          然后：python refresh.py --backend wsl")


def check_api_key(r: Report) -> None:
    # 环境变量
    for name in ("DEEPSEEK_API_KEY", "DSH_DEEPSEEK_API_KEY"):
        if os.environ.get(name):
            r.ok(f"API key：来自环境变量 {name}")
            return
    # 本地文件
    f = QUENCH / "api_key.txt"
    if f.is_file() and f.read_text(encoding="utf-8-sig").strip():
        r.ok(f"API key：来自 {f.relative_to(HERE)}")
        return
    # DSH 凭证
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    if cred.is_file() and "DEEPSEEK_API_KEY" in cred.read_text(
            encoding="utf-8", errors="replace"):
        r.ok(f"API key：来自 {cred} 的 refs 段")
        return

    r.note("API key：命令行里没找到",
           "只在「生成新 Verilog」时需要。两种填法：\n"
           "          · 网页里点右下角「API」按钮填（只存在浏览器里）\n"
           "          · 或者写到 quench/api_key.txt\n"
           "          去 https://platform.deepseek.com/api_keys 建一个")
    r.info("      注意：只做探测（refresh.py）不需要 key")


def check_files(r: Report) -> None:
    need = ["server.py", "refresh.py", "project_config.py",
            "index.html", "build.py"]
    missing = [f for f in need if not (HERE / f).is_file()]
    if missing:
        r.bad(f"缺少文件：{'、'.join(missing)}",
              "确认是完整 clone 下来的，不是只复制了其中几个")
    else:
        r.ok("项目文件齐全（server.py / refresh.py / index.html 等）")

    if QUENCH.is_dir():
        r.ok("quench 目录在（生成引擎）")
    else:
        r.note("没有 quench 目录 —— 只能做探测，不能生成新代码")

    if not (HERE / "workbench.html").is_file():
        r.note("还没有构建产物 workbench.html",
               "跑一下：python build.py")
    else:
        r.ok("构建产物在（workbench.html）")


def check_probe(r: Report) -> None:
    """真的跑一个小例子，验证端到端能通。"""
    import tempfile
    import json

    r.info("")
    r.info("      端到端试跑一个小例子……")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "doctor-probe"
        d.mkdir()
        (d / "dut.v").write_text(
            "module dut(input wire clk, input wire rst_n, output reg [3:0] q);\n"
            "  always @(posedge clk or negedge rst_n)\n"
            "    if(!rst_n) q <= 4'd0; else q <= q + 1'b1;\n"
            "endmodule\n", encoding="utf-8")
        (d / "tb_dut.v").write_text(
            '`timescale 1ns/1ps\n'
            "module tb_dut;\n"
            "  reg clk = 0, rst_n = 0; wire [3:0] q;\n"
            "  integer errors = 0, checks = 0;\n"
            "  dut u(.clk(clk), .rst_n(rst_n), .q(q));\n"
            "  always #5 clk = ~clk;\n"
            "  initial begin\n"
            '    $dumpfile("dut.vcd"); $dumpvars(0, tb_dut);\n'
            "    @(negedge clk); rst_n = 1;\n"
            "    @(posedge clk); #1; checks = checks + 1;\n"
            "    if(q !== 4'd1) begin errors = errors + 1; "
            '$display("FAIL q=%0d", q); end\n'
            "    $display(\"checks = %0d\", checks);\n"
            '    if(errors == 0) $display("=== TEST PASSED ===");\n'
            '    else            $display("=== TEST FAILED === ");\n'
            "    $finish;\n  end\nendmodule\n", encoding="utf-8")
        (d / "project.json").write_text(json.dumps({
            "version": 1, "name": "doctor-probe", "top": "dut",
            "dut": "dut.v", "tb": "tb_dut.v", "vcd": "dut.vcd",
            "timeout": 120, "distro": "Ubuntu-24.04",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        try:
            p = subprocess.run(
                [sys.executable, str(HERE / "refresh.py"), str(d)],
                capture_output=True, text=True, timeout=300, cwd=str(HERE),
                encoding="utf-8", errors="replace",
            )
            out = (p.stdout or "") + (p.stderr or "")
            if "5/5" in out:
                r.ok("端到端试跑通过：五步全绿（仿真/语法/综合/波形/产物）")
            elif "探测完成" in out:
                line = next((l for l in out.splitlines() if "探测完成" in l), "")
                r.note(f"端到端跑通了，但不是全绿：{line.strip()}")
            else:
                tail = "\n".join(out.strip().splitlines()[-6:])
                r.bad("端到端试跑没跑通", "下面是最后几行输出：\n" +
                      "\n".join("          " + l for l in tail.splitlines()))
        except Exception as exc:  # noqa: BLE001
            r.bad(f"端到端试跑失败：{type(exc).__name__}: {exc}")


def main() -> int:
    do_probe = "--probe" in sys.argv
    r = Report()

    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║  Workbench 环境自检                                  ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print()
    print("  [1] 运行环境")
    check_python(r)
    check_files(r)
    r.dump()

    print("  [2] EDA 工具链（跑仿真/综合必需）")
    n0 = len(r.lines)
    check_toolchain(r)
    for tag, msg in r.lines[n0:]:
        print(f"  [{tag}] {msg}")

    print("  [3] API key（只有「生成新代码」需要）")
    n1 = len(r.lines)
    check_api_key(r)
    for tag, msg in r.lines[n1:]:
        print(f"  [{tag}] {msg}")

    if do_probe:
        print("  [4] 端到端试跑")
        n2 = len(r.lines)
        check_probe(r)
        for tag, msg in r.lines[n2:]:
            print(f"  [{tag}] {msg}")

    print()
    print("  ──────────────────────────────────────────────────────")
    if r.bad == 0 and r.warn == 0:
        print("  结论：一切就绪。可以开始了：")
        print("        python server.py 8787 --no-browser")
    elif r.bad == 0:
        print(f"  结论：能跑，但有 {r.warn} 处需要注意（见上面 [注意] 的条目）。")
    else:
        print(f"  结论：还差 {r.bad} 项，按下面的步骤补齐：")
        print()
        for i, f in enumerate(dict.fromkeys(r.fixes), 1):
            print(f"  {i}) {f}")
    print("  ──────────────────────────────────────────────────────")
    print()
    print("  提示：加 --probe 会真的跑一个小例子，端到端验证一遍。")
    print()
    return 1 if r.bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
