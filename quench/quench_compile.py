#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 的语法检查层：生成的代码**必须先能编译过**，再谈别的。

为什么单独做这一层：
  解析层只能查「格式约定」（有没有 $dumpfile、有没有 PASSED 标记），
  查不出「q 被声明成 wire 却拿来赋值」这类真实语法/elaboration 错误。
  实测发现：一个格式全对的测试平台照样编译不过，白跑了 1.5 秒仿真 + 一整轮 API。
  而 iverilog 只编译不仿真只要 **0.08 秒** —— 便宜到没有理由不加。

调用方式：
    result = compile_check("module dut(...); endmodule", "module tb_dut; ...")
    if not result.ok: print(result.summary())
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_DISTRO = "Ubuntu-24.04"
COMPILE_TIMEOUT = 60

# 判断「是不是在 WSL 里」的可靠标志
_IS_WSL = ("microsoft" in platform.uname().release.lower()
           or "WSL_DISTRO_NAME" in os.environ)
_IS_WINDOWS = sys.platform.startswith("win")


class CompileResult:
    def __init__(self, ok: bool, output: str = "", error: str = "",
                 skipped: bool = False, seconds: float = 0.0, tool: str = ""):
        self.ok = ok
        self.output = output
        self.error = error
        self.skipped = skipped        # True = 环境里没有 iverilog，没检查
        self.seconds = seconds
        self.tool = tool

    def summary(self) -> str:
        if self.skipped:
            return f"跳过编译检查：{self.error}"
        if self.ok:
            return f"编译通过（{self.seconds:.2f} 秒）"
        return f"编译失败：{self.error}"

    def error_lines(self) -> list[str]:
        """挑出像报错的行，给「失败回喂」用。"""
        out = []
        for line in (self.output or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if ("error" in s.lower() or "not a valid" in s.lower()
                    or "syntax" in s.lower() or "declared here" in s.lower()):
                out.append(s)
        return out or (self.output or "").splitlines()[:6]


def _to_wsl_path(p: Path) -> str:
    s = p.resolve().as_posix()
    if len(s) > 1 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def _shq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def find_iverilog() -> tuple[str, str]:
    """探测 iverilog 在哪。返回 (方式, 说明)，方式 ∈ {native, wsl, none}。"""
    if _IS_WSL or platform.system() == "Linux":
        return "native", "本机 iverilog"
    if _IS_WINDOWS:
        # Windows 侧有就直接用，没有就走 WSL
        try:
            p = subprocess.run(["iverilog", "-V"], capture_output=True, text=True,
                               timeout=15, encoding="utf-8", errors="replace")
            if p.returncode == 0:
                return "native", "Windows 侧 iverilog"
        except Exception:  # noqa: BLE001
            pass
        try:
            p = subprocess.run(["wsl.exe", "-d", DEFAULT_DISTRO, "--", "bash", "-lc",
                                "command -v iverilog"],
                               capture_output=True, text=True, timeout=25,
                               encoding="utf-8", errors="replace")
            if p.returncode == 0 and (p.stdout or "").strip():
                return "wsl", f"WSL {DEFAULT_DISTRO} 的 iverilog"
        except Exception:  # noqa: BLE001
            pass
        return "none", "没找到 iverilog（Windows 侧和 WSL 侧都没有）"
    return "none", f"不支持的操作系统：{platform.system()}"


def compile_check(dut_src: str, tb_src: str, *, distro: str = DEFAULT_DISTRO,
                  dut_name: str = "dut.v", tb_name: str = "tb_dut.v",
                  mode: str | None = None) -> CompileResult:
    """把两份源码写进临时目录，用 iverilog 只编译不仿真。

    mode 可以强制指定 "native" / "wsl"；不给就自动探测。
    """
    import time
    if mode is None:
        mode, desc = find_iverilog()
    else:
        desc = mode
    if mode == "none":
        return CompileResult(True, skipped=True,
                             error="环境里没有 iverilog，跳过编译检查（不算失败）")

    with tempfile.TemporaryDirectory(prefix="quench_chk_") as td:
        d = Path(td)
        (d / dut_name).write_text(dut_src, encoding="utf-8")
        (d / tb_name).write_text(tb_src, encoding="utf-8")

        if mode == "native":
            wd = str(d)
            cmd = ["iverilog", "-g2012", "-o", str(d / "_chk.vvp"), dut_name, tb_name]
        else:
            wd = None
            wsl_dir = _to_wsl_path(d)
            shell = (f"cd {_shq(wsl_dir)} && iverilog -g2012 -o _chk.vvp "
                     f"{_shq(dut_name)} {_shq(tb_name)}")
            cmd = ["wsl.exe", "-d", distro, "--", "bash", "-lc", shell]

        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=wd, capture_output=True, text=True,
                               timeout=COMPILE_TIMEOUT, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return CompileResult(False, error=f"编译超时（{COMPILE_TIMEOUT} 秒）",
                                 tool=desc, seconds=time.time() - t0)
        except FileNotFoundError as exc:
            return CompileResult(True, skipped=True,
                                 error=f"调用 {desc} 失败：{exc}", tool=desc)
        except Exception as exc:  # noqa: BLE001
            return CompileResult(True, skipped=True,
                                 error=f"编译检查异常，已跳过：{type(exc).__name__}: {exc}",
                                 tool=desc)

        secs = time.time() - t0
        out = ((p.stdout or "") + (p.stderr or "")).strip()

        # wsl.exe 不转发退出码（第 1 步踩过这个坑），所以用输出判断，
        # 只有确认「输出干净」才算通过。
        bad = ("error" in out.lower() or "syntax" in out.lower()
               or "not a valid" in out.lower())
        ok = (not bad) and (p.returncode == 0 or mode == "wsl")

        err = ""
        if not ok:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            err = lines[0] if lines else f"退出码 {p.returncode}"
        return CompileResult(ok, output=out, error=err, tool=desc, seconds=secs)


if __name__ == "__main__":
    mode, desc = find_iverilog()
    print(f"iverilog 探测：{mode} —— {desc}")

    good_dut = "module dut(input wire clk, input wire rst_n, output reg [7:0] q);\n" \
               "  always @(posedge clk or negedge rst_n) if(!rst_n) q<=0; else q<=q+1;\n" \
               "endmodule\n"
    good_tb = "module tb_dut;\n  reg clk=0, rst_n=0; wire [7:0] q;\n" \
              "  dut u(.clk(clk),.rst_n(rst_n),.q(q));\n  always #5 clk=~clk;\n" \
              "  initial begin $dumpfile(\"dut.vcd\"); $dumpvars(0,tb_dut);\n" \
              "    #12 rst_n=1; #50;\n" \
              "    if (q!==0) $display(\"=== TEST FAILED ===\"); else $display(\"=== TEST PASSED ===\");\n" \
              "    $finish; end\nendmodule\n"

    r = compile_check(good_dut, good_tb)
    print("合规代码 ->", r.summary())

    bad_tb = good_tb.replace("#12 rst_n=1; #50;", "#12 rst_n=1; #50;\n    q = 8'd254;")
    r2 = compile_check(good_dut, bad_tb)
    print("非法赋值 ->", r2.summary())
    for l in r2.error_lines()[:3]:
        print("     ", l)
