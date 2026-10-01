#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页里「输 API key → 生成 Verilog」这条链路的后端。

为什么要有这个文件
------------------
原来生成 Verilog 只能走命令行：`python quench.py new "8位计数器"`，
API key 由服务端自己去找（环境变量 / api_key.txt / .credentials.yaml）。

但那个模型对「上传到 GitHub 给别人用」是行不通的 ——
别人 clone 下来没有 key，也没法用你工作台网页上的那个按钮。

所以改成：**key 由使用者自己在网页里输入**，存在他自己的浏览器里
（localStorage），每次请求随请求头发给服务端，服务端只做转发。

这样带来三个好处：
  · 别人 clone 下来，填自己的 key 就能用，不用碰你的任何凭证
  · key 不落盘到服务端，仓库里更不可能有 key
  · 谁的请求花谁的钱，账不会算到仓库作者头上

放在单独文件里而不是塞进 server.py：那边已经 900 多行了，
再加一整块生成逻辑会很难读。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUENCH_DIR = HERE.parent / "quench"

# 复用 quench 那套已经测过的代码，不重新造轮子
API = None
PROMPT = None
PARSE = None
_import_error = ""


def _load():
    """惰性导入 quench 的模块。

    做成惰性是因为：desk-app 应该在没有 quench 目录时也能正常跑
    （比如只想要探测功能的人）。只有真的点「生成」时才需要它。
    """
    global API, PROMPT, PARSE, _import_error
    if API is not None:
        return True
    if not QUENCH_DIR.is_dir():
        _import_error = f"找不到 quench 目录：{QUENCH_DIR}"
        return False
    if str(QUENCH_DIR) not in sys.path:
        sys.path.insert(0, str(QUENCH_DIR))
    try:
        import quench_api as _api          # type: ignore
        import quench_parse as _parse      # type: ignore
        import quench_prompt as _prompt    # type: ignore
    except Exception as exc:  # noqa: BLE001
        _import_error = f"导入 quench 模块失败：{type(exc).__name__}: {exc}"
        return False
    API, PARSE, PROMPT = _api, _parse, _prompt
    return True


# ------------------------------------------------------------------ key 校验
def looks_like_key(key: str) -> tuple[bool, str]:
    """粗查一下 key 的形状，早点拦住明显的错。

    只做形状检查，不真的去请求 —— 真正有没有效由调用结果说了算。
    """
    k = (key or "").strip()
    if not k:
        return False, "没有填 API key"
    if len(k) < 20:
        return False, f"这个 key 太短了（{len(k)} 个字符），正常的大概 35 个字符"
    if not k.startswith("sk-"):
        return False, "DeepSeek 的 API key 一般以 sk- 开头，请检查是否复制完整"
    if any(c.isspace() for c in k):
        return False, "key 里混进了空格或换行，请重新复制一次"
    return True, ""


# ------------------------------------------------------------------ 主流程
def generate(requirement: str, api_key: str, *, model: str = "",
             max_rounds: int = 1, temperature: float | None = None) -> dict:
    """按一句话需求生成 dut.v + tb_dut.v。返回给网页用的字典。

    只做一轮生成（不跑多轮回喂循环）—— 网页上要的是"快"，而且
    真正的验收应该走命令行那套完整的 quench.py。
    """
    if not _load():
        return {"ok": False, "error": _import_error}

    ok, why = looks_like_key(api_key)
    if not ok:
        return {"ok": False, "error": why, "kind": "bad_key"}

    req = (requirement or "").strip()
    if not req:
        return {"ok": False, "error": "还没有写需求", "kind": "bad_req"}

    kw = {"api_key": api_key.strip(), "max_tokens": 8192}
    if model:
        kw["model"] = model
    if temperature is not None:
        kw["temperature"] = temperature

    # 第一轮：让模型按系统提示词写代码
    messages = [
        {"role": "system", "content": PROMPT.SYSTEM_PROMPT},
        {"role": "user", "content": PROMPT.build_user_prompt(req, None, 1)},
    ]

    try:
        reply = API.chat(messages, **kw)
    except Exception as exc:  # noqa: BLE001
        # 错误信息可能带 key，必须过一遍脱敏
        msg = str(exc)
        try:
            msg = API._redact(msg, api_key)      # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
        low = msg.lower()
        kind = "auth" if ("401" in msg or "invalid" in low or "auth" in low) else "api"
        return {"ok": False, "error": msg, "kind": kind}

    text = (reply or {}).get("text") or ""
    usage = (reply or {}).get("usage") or {}

    # 解析 + 编译检查（quench_parse 会尽力救回半截 JSON，不会直接丢文件）
    try:
        files, issues = PARSE.parse_response(text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"解析模型回复失败：{type(exc).__name__}: {exc}",
                "kind": "parse", "raw": text[:2000]}

    dut = files.get(PARSE.DUT_NAME) or files.get("dut.v") or ""
    tb = files.get(PARSE.TB_NAME) or files.get("tb_dut.v") or ""
    if not dut or not tb:
        return {"ok": False, "kind": "parse",
                "error": "模型回复里没有找到 dut.v 和 tb_dut.v 两个文件",
                "issues": [str(i) for i in (issues or [])][:8],
                "raw": text[:2000]}

    return {
        "ok": True,
        "requirement": req,
        "dut": dut,
        "tb": tb,
        "issues": [str(i) for i in (issues or [])][:8],
        "usage": usage,
        "model": (reply or {}).get("model") or model or "deepseek-chat",
    }


def status() -> dict:
    """告诉网页这一侧能不能用（让别人一眼看出缺什么）。"""
    ok = _load()
    return {
        "ok": ok,
        "error": _import_error if not ok else "",
        "hasServerKey": bool(_server_key_hint()),
        "quenchDir": str(QUENCH_DIR),
        "quenchFound": QUENCH_DIR.is_dir(),
    }


def _server_key_hint() -> str:
    """服务端自己有没有配 key —— 只回一个掩码，绝不回原文。"""
    try:
        if not _load():
            return ""
        key, _src = API.find_api_key()
        return API.mask(key) if key else ""
    except Exception:  # noqa: BLE001
        return ""
