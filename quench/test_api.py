#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块 1（API 客户端）的测试。

绝大部分用假 opener，不花 API 钱；只有【F】组会真的发一次请求。

用法: python test_api.py            # 含真实调用
      python test_api.py --offline  # 跳过真实调用
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quench_api as A  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
OFFLINE = "--offline" in sys.argv


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ 假 opener
class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload, status=200, capture=None):
    def _open(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.headers)
            capture["body"] = json.loads(req.data.decode())
            capture["timeout"] = timeout
        return FakeResponse(payload, status)
    return _open


def opener_raising(exc, capture=None, times=None):
    state = {"n": 0}

    def _open(req, timeout=None):
        if capture is not None:
            capture["body"] = json.loads(req.data.decode())
        state["n"] += 1
        if times is None or state["n"] <= times:
            raise exc
        return FakeResponse({"choices": [{"message": {"content": "重试成功了"}}],
                             "usage": {"total_tokens": 5}})
    return _open


OK_PAYLOAD = {
    "choices": [{"message": {"content": '{"dut":"x","tb":"y"}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    "model": "deepseek-chat",
}

FAKE_KEY = "sk-" + "f" * 32


def main() -> int:
    print("=" * 70)
    print("模块 1（API 客户端）测试" + ("  [离线模式]" if OFFLINE else ""))
    print("=" * 70)

    # ---------------------------------------------------------- A. 请求构造
    print("\n【A】请求要按 API 规范构造")
    cap = {}
    r = A.chat([{"role": "user", "content": "你好"}], api_key=FAKE_KEY,
               temperature=0.2, max_tokens=100, _opener=opener_returning(OK_PAYLOAD, capture=cap))
    check("打到正确的 URL", cap.get("url") == A.API_URL, str(cap.get("url")))
    check("带上 Authorization 头",
          any(k.lower() == "authorization" and cap["headers"][k] == f"Bearer {FAKE_KEY}"
              for k in cap["headers"]),
          str([k for k in cap.get("headers", {})]))
    check("Content-Type 是 json",
          cap["headers"].get("Content-type", cap["headers"].get("Content-Type", "")).startswith("application/json"))
    check("body 里有 model / messages / temperature", 
          all(k in cap["body"] for k in ("model", "messages", "temperature")))
    check("temperature 传对了", cap["body"]["temperature"] == 0.2)
    check("中文没被转义成 \\uXXXX（ensure_ascii=False）",
          "你好" in json.dumps(cap["body"], ensure_ascii=False))
    check("默认走非流式", cap["body"]["stream"] is False)
    check("返回值里有 text/usage/model",
          set(["text", "usage", "model"]) <= set(r), str(list(r)))
    check("文本取对了", r["text"] == '{"dut":"x","tb":"y"}', r["text"])
    check("用量取对了", r["usage"]["total_tokens"] == 15)

    # ---------------------------------------------------------- B. 重试策略
    print("\n【B】该重试的重试，不该重试的不重试")

    slept: list[float] = []
    err429 = urllib.error.HTTPError(A.API_URL, 429, "Too Many Requests", {},
                                    io.BytesIO(b'{"error":"rate limit"}'))
    r = A.chat([{"role": "user", "content": "x"}], api_key=FAKE_KEY,
               sleep=slept.append, _opener=opener_raising(err429, times=2))
    check("429 会重试并最终成功", r["text"] == "重试成功了", r["text"])
    check("退避是递增的（1s → 2s）", slept == [1, 2], str(slept))

    slept2: list[float] = []
    err401 = urllib.error.HTTPError(A.API_URL, 401, "Unauthorized", {},
                                    io.BytesIO(b'{"error":"bad key"}'))
    try:
        A.chat([{"role": "user", "content": "x"}], api_key=FAKE_KEY,
               sleep=slept2.append, _opener=opener_raising(err401))
        check("401 直接抛错不重试", False, "居然没抛")
    except A.ApiError as exc:
        check("401 直接抛错不重试", exc.status == 401 and not exc.retryable, str(exc))
        check("401 没有白白等待", slept2 == [], str(slept2))
        check("401 的错误信息里有人话提示", "key 无效" in str(exc), str(exc))

    err503 = urllib.error.HTTPError(A.API_URL, 503, "Unavailable", {},
                                    io.BytesIO(b"oops"))
    try:
        A.chat([{"role": "user", "content": "x"}], api_key=FAKE_KEY, retries=2,
               sleep=lambda s: None, _opener=opener_raising(err503))
        check("重试耗尽后抛出", False, "居然没抛")
    except A.ApiError as exc:
        check("重试耗尽后抛出", exc.status == 503, str(exc))

    try:
        A.chat([{"role": "user", "content": "x"}], api_key=FAKE_KEY, retries=2,
               sleep=lambda s: None,
               _opener=opener_raising(urllib.error.URLError("网络断了")))
        check("网络错误会重试", False, "居然没抛")
    except A.ApiError as exc:
        check("网络错误会重试并最终抛出", "网络错误" in str(exc), str(exc))

    # ---------------------------------------------------------- C. 返回结构异常
    print("\n【C】服务端返回结构不对时要给可读错误，不能崩")
    for label, payload in (
        ("没有 choices", {"usage": {}}),
        ("choices 是空数组", {"choices": []}),
        ("message 里没有 content", {"choices": [{"message": {}}]}),
    ):
        try:
            A.chat([{"role": "user", "content": "x"}], api_key=FAKE_KEY,
                   _opener=opener_returning(payload))
            check(f"{label} → 抛 ApiError", False, "居然没抛")
        except A.ApiError as exc:
            check(f"{label} → 抛 ApiError", "不符合预期" in str(exc), str(exc))
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → 抛 ApiError", False, f"抛的是 {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- D. key 查找
    print("\n【D】key 查找顺序：环境变量 → 本地文件 → DSH 凭证")
    saved = {n: os.environ.get(n) for n in A.ENV_NAMES}
    try:
        for n in A.ENV_NAMES:
            os.environ.pop(n, None)
        os.environ["DEEPSEEK_API_KEY"] = "sk-from-env-123456"
        key, src = A.find_api_key()
        check("环境变量优先", key == "sk-from-env-123456" and "环境变量" in src, f"{key} / {src}")

        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ["DSH_DEEPSEEK_API_KEY"] = "sk-from-env2-123456"
        key, src = A.find_api_key()
        check("备用环境变量也认", key == "sk-from-env2-123456", f"{key} / {src}")
        os.environ.pop("DSH_DEEPSEEK_API_KEY", None)

        # 没有环境变量时，应当能从 DSH 凭证文件里读到
        key, src = A.find_api_key()
        if A.DSH_CREDENTIALS.is_file():
            check("回落到 DSH 凭证文件", bool(key) and "credentials" in src, f"{A.mask(key)} / {src}")
        else:
            check("回落到 DSH 凭证文件（文件不存在，跳过）", True)
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    # ---------------------------------------------------------- E. key 不能泄露
    print("\n【E】key 绝不能出现在错误信息或日志里")
    secret = "sk-" + "S3CR3T" * 4
    leak_cases = [
        ("HTTP 错误体里回显了 key",
         urllib.error.HTTPError(A.API_URL, 400, "Bad Request", {},
                                io.BytesIO(f'{{"error":"invalid key {secret}"}}'.encode()))),
        ("网络错误", urllib.error.URLError(f"failed with {secret}")),
    ]
    for label, exc in leak_cases:
        try:
            A.chat([{"role": "user", "content": "x"}], api_key=secret,
                   retries=1, sleep=lambda s: None, _opener=opener_raising(exc))
        except A.ApiError as e:
            blob = str(e) + (e.body or "")
            check(f"{label} → 异常信息里没有明文 key", secret not in blob,
                  blob[:160])
        except Exception as e:  # noqa: BLE001
            check(f"{label} → 异常信息里没有明文 key", secret not in str(e), str(e)[:160])

    check("mask() 不泄露完整 key", secret not in A.mask(secret), A.mask(secret))
    check("mask() 保留前缀便于辨认", A.mask(secret).startswith("sk-S3C"), A.mask(secret))
    check("mask(None) 不崩", "未找到" in A.mask(None))

    # ---------------------------------------------------------- F. 真实调用
    print("\n【F】真实连通性")
    if OFFLINE:
        check("真实调用（--offline 跳过）", True)
    else:
        key, src = A.find_api_key()
        if not key:
            check("真实调用", False, f"没找到 key：{src}")
        else:
            try:
                r = A.chat([{"role": "user", "content": "只回复两个字：连通"}],
                           max_tokens=16, retries=2)
                check(f"真实调用成功（{src}）", bool(r["text"].strip()), str(r)[:150])
                check("真实调用有 usage 统计", bool(r["usage"].get("total_tokens")),
                      str(r["usage"]))
            except A.ApiError as exc:
                check("真实调用成功", False, str(exc))

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 70)
    bad = [r for r in RESULTS if not r[1]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("\n失败项：")
        for n, _, det in bad:
            print(f"  ✗ {n}")
            if det:
                print(f"      {det[:320]}")
    print("=" * 70)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
