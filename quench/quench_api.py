#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quench 模块 1：DeepSeek API 客户端。

设计要点：
  1. **零第三方依赖** —— 只用标准库 urllib，不引入 requests / openai。
     理由：这个引擎要能在只有 Python 的机器上跑起来。
  2. **key 只存在于内存** —— 不写日志、不落盘、报错信息里也不回显。
     这一点有专门的测试守着（test_api.py 的【E】组）。
  3. key 的查找顺序：环境变量 → quench/api_key.txt → ~/.dsh/.credentials.yaml
     （截图里建议的「环境变量优先」+ 一个便携的本地文件 + DSH 自带凭证）
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT = 180
DEFAULT_RETRIES = 3

ENV_NAMES = ("DEEPSEEK_API_KEY", "DSH_DEEPSEEK_API_KEY")
LOCAL_KEY_FILE = HERE / "api_key.txt"
DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

# 可重试的 HTTP 状态码：限流和 5xx 值得再试，4xx 通常是请求本身有问题，重试没意义
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ApiError(Exception):
    """带可读原因的 API 异常。**绝不包含 key。**"""

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False, body: str = ""):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body


# ------------------------------------------------------------------ key 查找
def _parse_credentials_yaml(text: str) -> dict:
    """从 DSH 凭证文件里抽出 refs 段的键值。

    样本长得这样（值没有引号）：
        version: 1
        records:
          client-connection/browser-session:
            kind: grant
        refs:
          DEEPSEEK_API_KEY: sk-xxxxxxxx
          DSH_WEIXIN_BOT_TOKEN_XXX: 5ea1...
    所以只做「缩进 + key: value」的浅解析就够了，不需要完整 YAML。
    """
    refs: dict[str, str] = {}
    in_refs = False
    refs_indent = -1
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()

        if not in_refs:
            if key == "refs" and not value:
                in_refs = True
                refs_indent = indent
            continue
        # 已经进了 refs 段：缩进回退说明段落结束
        if indent <= refs_indent:
            break
        if value:
            refs[key] = value.strip("'\"")
    return refs


def find_api_key(verbose: bool = False) -> tuple[str | None, str]:
    """按优先级找 key。返回 (key, 来源说明)。key 为空时来源说明里写清找过哪些地方。"""
    tried: list[str] = []

    for name in ENV_NAMES:
        val = os.environ.get(name, "").strip()
        tried.append(f"环境变量 {name}")
        if val:
            return val, f"环境变量 {name}"

    tried.append(str(LOCAL_KEY_FILE))
    if LOCAL_KEY_FILE.is_file():
        val = LOCAL_KEY_FILE.read_text(encoding="utf-8-sig").strip()
        # 允许文件里只放 key，也允许写 DEEPSEEK_API_KEY=sk-xxx
        if "=" in val.splitlines()[0]:
            for line in val.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip() in ENV_NAMES:
                        val = v.strip().strip("'\"")
                        break
        if val:
            return val, str(LOCAL_KEY_FILE)

    tried.append(str(DSH_CREDENTIALS))
    if DSH_CREDENTIALS.is_file():
        try:
            refs = _parse_credentials_yaml(
                DSH_CREDENTIALS.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            refs = {}
        for name in ENV_NAMES:
            if refs.get(name):
                return refs[name], f"{DSH_CREDENTIALS} 的 refs.{name}"

    return None, "依次找过：\n    " + "\n    ".join(tried)


def mask(key: str | None) -> str:
    """给人看的遮蔽形式 —— 只留前 6 位。"""
    if not key:
        return "(未找到)"
    return key[:6] + "…" + f"（共 {len(key)} 字符）"


# ------------------------------------------------------------------ 调用
def chat(messages: list[dict], *, api_key: str | None = None,
         model: str = DEFAULT_MODEL, temperature: float = DEFAULT_TEMPERATURE,
         max_tokens: int = 8192, timeout: int = DEFAULT_TIMEOUT,
         retries: int = DEFAULT_RETRIES, sleep=time.sleep,
         _opener=None) -> dict:
    """调一次 chat/completions。返回 {"text":..., "usage":..., "model":..., "raw":...}

    重试策略：只重试「限流 / 5xx / 网络抖动」，且指数退避。
    4xx（key 无效、请求格式错）不重试 —— 重试只是浪费时间和额度。
    """
    if api_key is None:
        api_key, source = find_api_key()
        if not api_key:
            raise ApiError(f"没找到 DeepSeek API key。{source}")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    last_exc: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            opener = _opener or urllib.request.urlopen
            with opener(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return _normalize(data, model)

        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            retryable = exc.code in RETRYABLE_STATUS
            last_exc = ApiError(
                f"API 返回 HTTP {exc.code}" + (_friendly_http(exc.code)),
                status=exc.code, retryable=retryable, body=_redact(detail, api_key))
            if not retryable or attempt >= retries:
                raise last_exc

        except urllib.error.URLError as exc:
            # 必须擦除：网络错误的 reason 里可能带上请求上下文，回显 key 就麻烦了
            last_exc = ApiError(f"网络错误：{_redact(str(exc.reason), api_key)}",
                                retryable=True)
            if attempt >= retries:
                raise last_exc

        except json.JSONDecodeError as exc:
            raise ApiError(f"API 返回的不是合法 JSON：{_redact(str(exc), api_key)}") from exc

        except TimeoutError:
            last_exc = ApiError(f"请求超时（{timeout} 秒）", retryable=True)
            if attempt >= retries:
                raise last_exc

        # 指数退避：1s, 2s, 4s…
        if attempt < retries:
            sleep(min(2 ** (attempt - 1), 8))

    raise last_exc or ApiError("未知失败")


# ------------------------------------------------------------------ 内部工具
def _friendly_http(code: int) -> str:
    return {
        400: "（请求格式有问题）",
        401: "（key 无效或已过期）",
        402: "（账户余额不足）",
        403: "（没有权限）",
        404: "（模型名不对或接口地址变了）",
        422: "（参数不合法）",
        429: "（请求过于频繁，稍后重试）",
        500: "（服务端错误）",
        503: "（服务暂时不可用）",
    }.get(code, "")


def _redact(text: str, key: str | None) -> str:
    """把可能出现在服务端回显里的 key 擦掉。"""
    if not text:
        return text
    if key:
        text = text.replace(key, "sk-***REDACTED***")
    # 兜底：任何 sk- 开头的长串都擦掉
    return re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***REDACTED***", text)


def _normalize(data: dict, model: str) -> dict:
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError(f"API 回复结构不符合预期：{exc}；原始内容片段="
                       f"{json.dumps(data, ensure_ascii=False)[:300]}") from exc
    return {
        "text": text or "",
        "usage": data.get("usage") or {},
        "model": data.get("model") or model,
        "finishReason": choice.get("finish_reason"),
        "raw": data,
    }


if __name__ == "__main__":
    key, source = find_api_key()
    print("key 查找结果：", mask(key))
    print("来源        ：", source)
    if key:
        print("\n发一次最小请求验证连通性…")
        try:
            r = chat([{"role": "user", "content": "只回复：通"}], api_key=key, max_tokens=8)
            print("  回复：", r["text"].strip())
            print("  用量：", r["usage"].get("total_tokens"), "tokens")
        except ApiError as exc:
            print("  失败：", exc)
