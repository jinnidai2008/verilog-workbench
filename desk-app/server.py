#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地台面服务器 —— 只监听 127.0.0.1，只服务本目录。

  GET  /                  -> workbench.html
  GET  /board.json        -> 当前数据（禁缓存）
  POST /__refresh         -> 真跑一次 refresh.py，重新探测 Verilog 工程
  GET  其他文件            -> 目录内静态文件

用法: python server.py [端口]
"""
from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = "workbench.html"
APP_TAG = "dsh-verilog-workbench"      # 用来辨认「这个端口上是不是我们自己」
_lock = threading.Lock()

# ---------------------------------------------------------------- 访问控制
# 部署到网上之后，光靠 127.0.0.1 就不够了 —— 任何拿到网址的人都能浏览
# 你的整个磁盘、上传文件、让 EDA 工具在磁盘上写文件（实测确认过）。
# 所以加两道锁：
#   ① 口令（token）：URL 里带 ?token=xxx 才能进，进来后种 cookie 记住
#   ② 目录围栏：所有工程必须落在 WORKSPACE 目录树内，挡住整盘浏览
TOKEN_FILE = HERE / "access_token.txt"
WORKSPACE = HERE.parent                 # 围栏根：只有这底下的目录能被测
PUBLIC_PATHS = {"/__whoami", "/__login"}   # 不需要口令的端点


def load_or_create_token() -> str:
    """读口令；没有就生成一个并落盘。

    落盘是为了「重启后网址不变」—— 否则每次重启都要重新发给别人。
    """
    env = os.environ.get("DSH_WORKBENCH_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.is_file():
        t = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if t:
            return t
    t = secrets.token_urlsafe(12)
    TOKEN_FILE.write_text(t + "\n", encoding="utf-8")
    print(f"  已生成访问口令，存在 {TOKEN_FILE.name}")
    return t


TOKEN = load_or_create_token()

# 是否强制口令？**只有对外监听时才强制。**
# 理由：只绑 127.0.0.1 时，能访问的只有本机自己，加口令纯属给自己添堵
# （每次打开都要输）。一旦绑到 0.0.0.0，别人就能连进来，那时口令是必需的。
# 这个开关在 main() 里根据 --host 设置。
REQUIRE_TOKEN = True


def demo_mode() -> bool:
    return os.environ.get("DSH_WORKBENCH_OPEN_DEMO", "1") not in ("0", "false", "no")


def auth_enabled() -> bool:
    return REQUIRE_TOKEN and bool(TOKEN)


def is_public_file(path: str) -> bool:
    """演示版和发布版是给人看的静态页面，**故意不要口令**。

    理由：它们的用途就是「发个链接谁都能看」。里面只有已经跑完的探测结果，
    没有任何能操作服务器的东西（没有 /__refresh、没有 /__drop）。
    把它们锁起来没有意义，只会让收到链接的人一脸问号。
    """
    if not demo_mode():
        return False
    p = (path or "").split("?")[0]
    return p in ("/demo.html", "/demo", "/release.html", "/release")


def inside_workspace(p: Path) -> bool:
    """这个路径在不在允许的目录树里？

    必须用 resolve() 之后再比 —— 否则 `..\\..\\` 这种能骗过字符串前缀检查。
    """
    try:
        rp = Path(p).resolve()
        ws = WORKSPACE.resolve()
        return rp == ws or ws in rp.parents
    except Exception:  # noqa: BLE001
        return False


def request_token(handler) -> str:
    """从 URL / Cookie / 请求头里把口令取出来。"""
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path or "").query)
    t = (q.get("token") or [""])[0]
    if t:
        return t
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    t = handler.headers.get("X-Workbench-Token") or ""
    if t:
        return t
    cookie = handler.headers.get("Cookie") or ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "wb_token":
            return v
    return ""


def token_ok(handler) -> bool:
    if not auth_enabled():
        return True
    got = request_token(handler)
    if not got or not TOKEN:
        return False
    return hmac.compare_digest(got, TOKEN)
_media_cache: dict = {"mtime": None, "videos": {}}
_video_cache: dict[str, tuple[float, bytes]] = {}
_precheck_cache: dict = {}

sys.path.insert(0, str(HERE))
try:
    from project_config import load_config, validate   # 复用同一份契约
except Exception:  # noqa: BLE001
    load_config = validate = None


def scan_projects(max_depth: int = 2, limit: int = 60) -> list[dict]:
    """扫出本机有哪些工程可以测。

    「工程」的判据：目录里有 project.json（那是第 1 步定的契约）。
    另外把「只有 runs 存档、工程目录已删」的也算上 —— 那种能看历史但不能测，
    界面会灰显提示。
    """
    base = WORKSPACE
    found: list[dict] = []
    seen: set[str] = set()
    # 这些是我们自己的基础设施目录，不是被测工程
    skip = {"desk-app", "quench", "runs", "node_modules", ".git", "__pycache__"}

    def consider(d: Path):
        key = str(d.resolve())
        if key in seen or not d.is_dir() or d.name in skip:
            return
        pj = d / "project.json"
        if not pj.is_file():
            return
        cfg = {}
        try:
            cfg = json.loads(pj.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            cfg = {}
        runs = d.parent / "runs" / d.name
        n_rounds = 0
        if runs.is_dir():
            n_rounds = sum(1 for c in runs.iterdir()
                           if c.is_dir() and c.name.startswith("round-"))
        vfiles = sorted(p.name for p in d.glob("*.v"))
        seen.add(key)
        found.append({
            "name": str(cfg.get("name") or d.name),
            "dir": str(d.resolve()),
            "top": str(cfg.get("top") or ""),
            "dut": str(cfg.get("dut") or ""),
            "tb": str(cfg.get("tb") or ""),
            "vcd": str(cfg.get("vcd") or ""),
            "valid": bool(cfg.get("dut") and cfg.get("tb")),
            "rounds": n_rounds,
            "verilogFiles": vfiles,
        })

    # 常见位置：与 desk-app 平级、再往下一层
    if base.is_dir():
        for child in sorted(base.iterdir()):
            consider(child)
            if max_depth >= 2 and child.is_dir():
                try:
                    for sub in sorted(child.iterdir()):
                        consider(sub)
                except OSError:
                    pass
    # 只有存档、没有工程目录的，也列出来（能看历史）
    runs_root = base / "runs"
    if runs_root.is_dir():
        for rc in sorted(runs_root.iterdir()):
            if not rc.is_dir():
                continue
            proj = base / rc.name
            if str(proj.resolve()) in seen:
                continue
            summ = {}
            sf = rc / "summary.json"
            if sf.is_file():
                try:
                    summ = json.loads(sf.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    summ = {}
            n = sum(1 for c in rc.iterdir()
                    if c.is_dir() and c.name.startswith("round-"))
            seen.add(str(proj.resolve()))
            found.append({
                "name": rc.name, "dir": str(proj.resolve()),
                "top": "", "dut": "", "tb": "", "vcd": "",
                "valid": False, "archivedOnly": True,
                "rounds": n, "ok": bool(summ.get("ok")),
                "verilogFiles": [],
            })
    found.sort(key=lambda x: (not x["valid"], -x["rounds"], x["name"]))
    return found[:limit]


def import_dropped(files: list[dict]) -> dict:
    """把拖进来的文件建成一个可测工程。

    files: [{"name": "counter.v", "text": "..."}] —— 浏览器只能给出文件名和内容，
           给不出原始路径（安全限制），所以这里在 workspace 下新建一个目录收好。

    自动做的事：
      · 若拖进来的包含 project.json → 直接用它
      · 否则自动识别：tb_*.v / *_tb.v / testbench*.v 当测试平台，其余 .v 当设计
      · 生成 project.json 并用同一个契约校验
    """
    import re as _re
    verilog = [f for f in files if str(f.get("name", "")).lower().endswith((".v", ".sv"))]
    pj = next((f for f in files if str(f.get("name", "")).lower() == "project.json"), None)
    if not verilog and not pj:
        return {"ok": False, "error": "没看到 .v / .sv 或 project.json 文件"}

    # 收件目录名：优先用 project.json 里的 name，其次用第一个 verilog 文件名
    slug = ""
    cfg: dict = {}
    if pj:
        try:
            cfg = json.loads(pj.get("text") or "{}")
            slug = str(cfg.get("name") or "").strip()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"拖进来的 project.json 不是合法 JSON：{exc}"}
    if not slug and verilog:
        slug = _re.sub(r"[^A-Za-z0-9_-]+", "-", Path(verilog[0]["name"]).stem).strip("-")
    slug = slug or "dropped"
    dest = WORKSPACE / f"drop-{slug}"
    if not inside_workspace(dest):
        return {"ok": False, "error": f"目标目录不在允许范围内：{dest}"}
    dest.mkdir(parents=True, exist_ok=True)

    written = []
    for f in verilog:
        name = Path(str(f.get("name"))).name          # 只取文件名，避免路径穿越
        target = (dest / name).resolve()
        if not inside_workspace(target):
            return {"ok": False, "error": f"文件名不合法：{name}"}
        target.write_text(str(f.get("text") or ""), encoding="utf-8")
        written.append(name)

    if not cfg:
        # 自动识别被测设计与测试平台
        def is_tb(n: str) -> bool:
            s = n.lower()
            return (s.startswith("tb_") or s.endswith("_tb.v") or "testbench" in s
                    or s.startswith("tb.") or s == "tb.v")
        tbs = [n for n in written if is_tb(n)]
        duts = [n for n in written if n not in tbs]
        if not tbs or not duts:
            return {"ok": False, "written": written,
                    "error": ("识别不出哪个是设计、哪个是测试平台。"
                              "请把文件命名成 counter.v + tb_counter.v 这种形式，"
                              "或者一起拖一个 project.json 进来。")}
        tb, dut = tbs[0], duts[0]
        top_m = None
        try:
            top_m = _re.search(r"(?m)^\s*module\s+([A-Za-z_]\w*)",
                               (dest / dut).read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            top_m = None
        cfg = {
            "version": 1, "name": slug, "top": top_m.group(1) if top_m else Path(dut).stem,
            "dut": dut, "tb": tb, "vcd": "dut.vcd",
            "timeout": 240, "distro": "Ubuntu-24.04",
        }
        # 顺手把 tb 里的 $dumpfile 名字对齐，免得第 4 步找不到波形
        try:
            tb_text = (dest / tb).read_text(encoding="utf-8")
            new_tb = _re.sub(r'(\$dumpfile\s*\(\s*")[^"]+(")',
                             r"\g<1>dut.vcd\g<2>", tb_text)
            if new_tb != tb_text:
                (dest / tb).write_text(new_tb, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    (dest / "project.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 用同一个契约校验，不合格就说清为什么
    problems = precheck_dir(dest)
    if problems:
        return {"ok": False, "dir": str(dest), "written": written,
                "error": "；".join(problems[:3])}
    return {"ok": True, "dir": str(dest), "name": cfg.get("name") or slug,
            "written": written + ["project.json"],
            "config": {k: cfg.get(k) for k in ("name", "top", "dut", "tb", "vcd")}}


def precheck_dir(project_dir: Path) -> list[str]:
    """用第 1 步的契约校验任意一个目录。返回错误消息列表。"""
    if load_config is None:
        return ["服务端无法导入 project_config.py"]
    if not Path(project_dir).is_dir():
        return [f"目录不存在：{project_dir}"]
    raw, issues = load_config(Path(project_dir))
    if raw is None:
        return [i.message for i in issues]
    _, more = validate(raw, Path(project_dir))
    return [i.message for i in issues + more if i.level == "error"]


def _resolve_project() -> tuple[Path | None, str]:
    """要和 refresh.py 用同一套优先级，否则界面预检和实际探测会对不上。"""
    import os
    env = os.environ.get("DSH_VERILOG_PROJECT")
    if env:
        return Path(env).expanduser().resolve(), "环境变量"
    pointer = HERE / "project.json"
    if pointer.is_file():
        try:
            rel = (json.loads(pointer.read_text(encoding="utf-8-sig")) or {}).get("projectDir")
            if isinstance(rel, str) and rel.strip():
                p = Path(rel)
                return (p if p.is_absolute() else HERE / p).resolve(), "desk-app/project.json"
        except Exception:  # noqa: BLE001
            pass
    return WORKSPACE / "vibe-verilog-lab", "默认同级目录"


def precheck_project() -> dict:
    """跑 EDA 之前先验配置。有问题就直接告诉界面，别让用户等一场空。

    结果按 (工程路径, project.json 的修改时间) 缓存 —— 以前每次请求都要跑两遍
    （一次预检、一次算超时），白读两遍文件。配置文件一改，时间戳变了就自动重算。
    """
    global _precheck_cache
    proj, source = _resolve_project()
    try:
        stamp = (proj / "project.json").stat().st_mtime
    except OSError:
        stamp = None
    key = (str(proj), stamp)
    if _precheck_cache.get("key") == key:
        return _precheck_cache["value"]

    out = {"projectDir": str(proj), "source": source,
           "ok": False, "errors": [], "config": None}
    if load_config is None:
        out["errors"].append({"message": "server 端无法导入 project_config.py"})
    elif not proj.is_dir():
        out["errors"].append({"message": f"找不到工程目录：{proj}（来源：{source}）"})
    else:
        raw, issues = load_config(proj)
        cfg = {}
        if raw is not None:
            cfg, more = validate(raw, proj)
            issues += more
        errs = [i for i in issues if i.level == "error"]
        out["errors"] = [{"message": i.message, "fix": i.fix} for i in errs]
        out["ok"] = not errs
        out["config"] = cfg if not errs else None

    _precheck_cache = {"key": key, "value": out}
    return out


def probe_timeout() -> int:
    """探测超时 = 五个步骤各自的超时之和，再留 60 秒余量。"""
    cfg = precheck_project().get("config") or {}
    return int(cfg.get("timeout", 240)) * 5 + 60


def _bgvideo_name(src: str) -> str | None:
    """把 media.src 归一化成 /bgvideo/<文件名> 里的 <文件名>；不是本地文件则返回 None。"""
    if not src or src.startswith(("data:", "http://", "https://", "/bgvideo/")):
        return None
    try:
        return Path(src).name
    except Exception:  # noqa: BLE001
        return None


def media_videos() -> dict[str, str]:
    """从 board.json 得到「文件名 -> 本地绝对路径」映射，用于流式播放背景视频。

    同时接受两种写法：
      · media.videos 里显式登记的表
      · media.src 直接写本地绝对路径（构建时才归一化，这里兜底自己算）
    """
    bp = HERE / "board.json"
    if not bp.exists():
        return {}
    try:
        mtime = bp.stat().st_mtime
        if _media_cache["mtime"] == mtime:
            return _media_cache["videos"]
        data = json.loads(bp.read_text(encoding="utf-8"))
        media = data.get("media") or {}
        vids = dict(media.get("videos") or {})
        name = _bgvideo_name(media.get("src") or "")
        if name:
            vids[name] = media["src"]
        _media_cache["mtime"] = mtime
        _media_cache["videos"] = vids
        return vids
    except Exception:  # noqa: BLE001
        return {}


def load_bgvideo(path: Path) -> bytes:
    """把背景视频整段读进内存（按路径+mtime 缓存）。

    4K 视频逐块读盘会拖慢起播、造成一顿一顿；内存直出就顺了。
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return b""
    cached = _video_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    data = path.read_bytes()
    _video_cache[key] = (mtime, data)
    print(f"  · 已把背景视频载入内存：{path.name} （{len(data) / 1024 / 1024:.1f} MB）")
    return data


def guess_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".mp4": "video/mp4", ".webm": "video/webm", ".m4v": "video/mp4",
        ".mov": "video/quicktime", ".ogv": "video/ogg",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    # 静音默认日志，换成人话
    def log_message(self, fmt, *args):  # noqa: A003
        if "__refresh" in (self.path or "") or self.path in ("/", "/board.json"):
            sys.stderr.write(f"  · {self.path}\n")

    def end_headers(self):
        # 开发期页面/数据一律禁缓存，否则改了 HTML 浏览器还读旧的
        p = (self.path or "").split("?")[0]
        if p in ("/", "/index.html", "/" + ENTRY, "/release.html", "/board.json"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _needs_token(self) -> bool:
        """这个请求要不要口令？"""
        if not auth_enabled():
            return False
        p = (self.path or "").split("?")[0]
        if p in PUBLIC_PATHS:
            return False
        # 演示版故意开放：它的用途就是「发个链接谁都能看」，
        # 而且里面只有跑完的结果，没有任何能操作服务器的接口。
        if is_public_file(p):
            return False
        return True

    def _deny(self):
        """没口令时给一个能自己解释的页面，而不是干巴巴的 403。"""
        from urllib.parse import urlparse
        if (self.path or "").split("?")[0].startswith("/__"):
            self._json(401, {"ok": False, "error": "需要访问口令（token）"})
            return
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        target = urlparse(self.path or "/").path or "/"
        body = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要访问口令</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#faf9f6;
     font:15px/1.8 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1d1d1f}}
.box{{background:#fff;border:1px solid #e6e2d9;border-radius:16px;padding:30px 34px;
      max-width:520px;box-shadow:0 8px 30px rgba(0,0,0,.06)}}
h1{{font-size:19px;margin:0 0 12px}}
code{{font-family:ui-monospace,Consolas,monospace;font-size:13px;background:#f4f2ee;
      padding:2px 7px;border-radius:5px;word-break:break-all}}
ol{{padding-left:20px;margin:14px 0 0}} li{{margin:6px 0}}
.note{{color:#8e8e93;font-size:13px;margin-top:16px}}
</style></head><body><div class="box">
<h1>🔒 这个工作台需要访问口令</h1>
<p>你打开的地址里没有带口令，或者口令不对。</p>
<ol>
  <li>向工作台的主人要 <b>完整链接</b>（里面带 <code>?token=...</code>）</li>
  <li>或者用这条地址进来：<br>
      <code>http://{html.escape(host)}{html.escape(target)}?token=你的口令</code></li>
  <li>口令正确之后浏览器会记住，之后直接开这个网址就行</li>
</ol>
<p class="note">口令存在服务器上的 <code>access_token.txt</code>，
命令行的启动信息里也会打印出来。</p>
</div></body></html>"""
        raw = body.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _set_cookie_and_redirect(self, token: str):
        """口令对了：种 cookie，然后把 URL 里的 token 抹掉再跳一次。

        抹掉是为了不让口令留在地址栏、历史记录、以及别人截图里。
        """
        from urllib.parse import urlencode, urlparse, parse_qs
        u = urlparse(self.path or "/")
        q = {k: v[0] for k, v in parse_qs(u.query).items() if k != "token"}
        clean = u.path + (("?" + urlencode(q)) if q else "")
        self.send_response(302)
        self.send_header("Location", clean or "/")
        self.send_header("Set-Cookie",
                         f"wb_token={token}; Path=/; Max-Age=2592000; SameSite=Lax")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        p = self.path.split("?")[0]
        if p == "/__whoami":
            self._json(200, {"app": APP_TAG, "port": self.server.server_address[1],
                             "dir": str(HERE)})
            return
        if p == "/__login":
            self._json(200 if token_ok(self) else 401,
                       {"ok": token_ok(self)})
            return
        if self._needs_token():
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path or "").query)
            url_token = (q.get("token") or [""])[0]
            if url_token and TOKEN and url_token == TOKEN:
                # 口令是从 URL 带进来的 → 种 cookie 并抹掉 URL 里的口令
                self._set_cookie_and_redirect(url_token)
                return
            if not token_ok(self):
                self._deny()
                return
        if p == "/__projects":
            cur, _src = _resolve_project()
            items = scan_projects()
            for it in items:
                it["current"] = (str(Path(it["dir"]).resolve())
                                 == str(Path(cur).resolve()) if cur else False)
            self._json(200, {"ok": True, "projects": items,
                             "current": str(cur) if cur else ""})
            return
        if p == "/__genstatus":
            # 告诉前端「生成功能这一侧能不能用」。不返回任何 key，
            # 只回一个掩码用于提示「服务端已经配了 key」。
            try:
                import gen_proxy
                self._json(200, gen_proxy.status())
            except Exception as exc:  # noqa: BLE001
                self._json(200, {"ok": False, "quenchFound": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
            return
        if self.path in ("/", "/index.html"):
            self.path = "/" + ENTRY
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/bgvideo/"):
            self._serve_bgvideo()
            return
        super().do_GET()

    # ---- 流式播放背景视频（支持 Range，127 MB 也不用整体载入内存）----
    def _serve_bgvideo(self):
        import urllib.parse
        from urllib.parse import unquote

        name = unquote(self.path[len("/bgvideo/"):].split("?")[0])
        videos = media_videos()
        target = videos.get(name)
        if not target:
            self.send_error(404, "background video not registered")
            return
        p = Path(target)
        if not p.is_file():
            self.send_error(404, f"file not found: {target}")
            return

        size = p.stat().st_size
        ctype = guess_type(p)
        rng = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:                      # 后缀范围：bytes=-N
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                partial = True
            except ValueError:
                partial = False
                start, end = 0, size - 1

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()

        # 直接从内存切片直出，比逐块读盘顺得多
        try:
            data = load_bgvideo(p)
            if data:
                for off in range(start, end + 1, 512 * 1024):
                    try:
                        self.wfile.write(data[off:min(off + 512 * 1024, end + 1)])
                    except (BrokenPipeError, ConnectionResetError):
                        return
                return
        except Exception:  # noqa: BLE001
            pass

        # 兜底：内存方案不可用时退回读盘
        with p.open("rb") as fh:
            fh.seek(start)
            remain = length
            while remain > 0:
                chunk = fh.read(min(256 * 1024, remain))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remain -= len(chunk)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path not in PUBLIC_PATHS and not token_ok(self):
            self._json(401, {"ok": False, "error": "需要访问口令（token）"})
            return
        if path == "/__setproject":
            self._set_project()
            return
        if path == "/__drop":
            self._drop_files()
            return
        if path == "/__gen":
            self._generate()
            return
        if path != "/__refresh":
            self.send_error(404, "unknown endpoint")
            return

        script = HERE / "refresh.py"
        if not script.exists():
            self._json(500, {"ok": False, "error": "refresh.py 不存在"})
            return
        if not _lock.acquire(blocking=False):
            self._json(429, {"ok": False, "error": "上一次探测还没跑完"})
            return

        stream = "stream=1" in (self.path or "")
        try:
            if stream:
                self._stream_probe(script)
            else:
                self._json_probe(script)
        finally:
            _lock.release()

    def _ndjson(self, obj: dict):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _drop_files(self):
        """接收拖进来的文件，建成工程，并切换过去。

        浏览器出于安全只给文件名和内容、不给原始路径，所以这里在 desk-app
        旁边新建一个 `drop-<名字>/` 目录收好，再把它设成当前工程。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 32 * 1024 * 1024:
                self._json(413, {"ok": False, "error": "文件太大（上限 32 MB）"})
                return
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"读不出上传内容：{exc}"})
            return

        files = payload.get("files") or []
        if not isinstance(files, list) or not files:
            self._json(400, {"ok": False, "error": "没有收到任何文件"})
            return

        try:
            res = import_dropped(files)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        if not res.get("ok"):
            self._json(400, res)
            return

        # 切换到这个新工程
        target = Path(res["dir"])
        try:
            rel = os.path.relpath(target, HERE)
        except ValueError:
            rel = str(target)
        (HERE / "project.json").write_text(
            json.dumps({"projectDir": rel}, ensure_ascii=False, indent=2), encoding="utf-8")
        global _precheck_cache
        _precheck_cache = {}
        self._json(200, res)

    def _set_project(self):
        """切换界面探测哪个工程：改写 desk-app/project.json 这个指路牌。

        只允许切成**确实存在且有 project.json** 的目录 —— 不然界面会切到一个
        空目录然后一片报错，用户不知道发生了什么。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        raw = str(payload.get("path") or "").strip()
        if not raw:
            self._json(400, {"ok": False, "error": "缺少 path"})
            return

        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = (HERE / target).resolve()
        else:
            target = target.resolve()

        if not target.is_dir():
            self._json(400, {"ok": False,
                             "error": f"目录不存在：{target}（可能已被清理）"})
            return
        if not (target / "project.json").is_file():
            self._json(400, {"ok": False,
                             "error": f"这个目录里没有 project.json：{target}"})
            return
        # 围栏：只能切到 WORKSPACE 目录树里的工程。
        # 没有这道锁的话，任何拿到网址的人都能把服务指向磁盘上任意位置，
        # 再用 EDA 工具去读写那里的文件（实测确认过这个风险）。
        if not inside_workspace(target):
            self._json(403, {"ok": False,
                             "error": f"这个目录不在允许的范围内：{target}\n"
                                      f"只允许 {WORKSPACE} 下面的工程。"})
            return

        try:
            rel = os.path.relpath(target, HERE)
        except ValueError:
            rel = str(target)
        pointer = HERE / "project.json"
        pointer.write_text(json.dumps({"projectDir": rel}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        # 指路牌换了，预检缓存必须作废，否则还会用旧的
        global _precheck_cache
        _precheck_cache = {}
        name = target.name
        try:
            raw_cfg, _ = load_config(target)
            if isinstance(raw_cfg, dict) and raw_cfg.get("name"):
                name = str(raw_cfg["name"])
        except Exception:  # noqa: BLE001
            pass
        self._json(200, {"ok": True, "name": name, "path": str(target)})

    def _stream_probe(self, script: Path):
        """NDJSON 流式推送：探测脚本每打一行就转发一行。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            # 先验配置：有问题就别去跑那五条 EDA 命令了
            pre = precheck_project()
            if not pre["ok"]:
                self._ndjson({"type": "log", "lines": [
                    f"工程目录: {pre['projectDir']}",
                    f"来源    : {pre['source']}",
                    "",
                    "配置有误，已停止（未执行任何 EDA 命令）：",
                ]})
                for e in pre["errors"]:
                    self._ndjson({"type": "log", "lines": [f"  [错误] {e['message']}"]})
                    if e.get("fix"):
                        self._ndjson({"type": "log", "lines": [f"         建议：{e['fix']}"]})
                self._ndjson({"type": "done", "ok": False, "board": {},
                              "error": f"project.json 有 {len(pre['errors'])} 个错误"})
                return

            c = pre.get("config") or {}
            if c:
                self._ndjson({"type": "log", "lines": [
                    f"项目名  : {c.get('name')}",
                    f"顶层模块: {c.get('top')}    设计: {c.get('dut')}    测试: {c.get('tb')}",
                    f"波形    : {c.get('vcd')}    WSL: {c.get('distro')}    超时: {c.get('timeout')}s",
                    "",
                ]})

            proc = subprocess.Popen(
                [sys.executable, "-u", str(script)],
                cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            buf: list[str] = []
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                buf.append(line)
                self._ndjson({"type": "log", "lines": [line]})
            proc.wait(timeout=60)

            board = {}
            bp = HERE / "board.json"
            if bp.exists():
                board = json.loads(bp.read_text(encoding="utf-8"))
            build = HERE / "build.py"
            if proc.returncode == 0 and build.exists():
                self._ndjson({"type": "log", "lines": ["", "正在把数据写入 workbench.html …"]})
                subprocess.run([sys.executable, str(build)], cwd=str(HERE),
                               capture_output=True, text=True, timeout=300)
            # 失败时把原因一并送出去，界面才知道该怎么提示
            reason = ""
            if proc.returncode != 0:
                errs = [l.strip() for l in buf if "[错误]" in l]
                reason = errs[0].replace("[错误]", "").strip() if errs else "探测未完成"
            self._ndjson({"type": "done", "ok": proc.returncode == 0,
                          "board": board, "error": reason})
        except subprocess.TimeoutExpired:
            self._ndjson({"type": "error", "message": "探测超时"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._ndjson({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass

    def _json_probe(self, script: Path):
        try:
            pre = precheck_project()
            if not pre["ok"]:
                self._json(400, {"ok": False, "projectDir": pre["projectDir"],
                                 "errors": pre["errors"],
                                 "error": f"project.json 有 {len(pre['errors'])} 个错误"})
                return
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(HERE), capture_output=True, text=True,
                timeout=probe_timeout(), encoding="utf-8", errors="replace",
            )
            ok = proc.returncode == 0
            tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-8:])
            build = HERE / "build.py"
            if ok and build.exists():
                subprocess.run([sys.executable, str(build)], cwd=str(HERE),
                               capture_output=True, text=True, timeout=300)
            self._json(200 if ok else 500, {"ok": ok, "tail": tail})
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "探测超时"})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _generate(self):
        """网页里输 API key → 生成 Verilog。

        key 是**使用者自己**在浏览器里填的，随请求头过来，这里只转发给
        DeepSeek，用完就丢 —— 不写日志、不落盘。

        为什么 key 要由前端传：这个仓库是要发到 GitHub 上给别人用的，
        如果服务端自己去翻环境变量或凭证文件，别人 clone 下来就没法用；
        而且一旦有人把自己的 key 提交进去就是事故。让使用者自带 key，
        谁的请求花谁的钱，仓库里也不可能出现任何 key。
        """
        try:
            import gen_proxy
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False,
                             "error": f"加载 gen_proxy 失败：{type(exc).__name__}: {exc}"})
            return

        # key 优先从请求头拿（前端 localStorage → 请求头），
        # 兼容放在 body 里的情况，方便直接用 curl 测。
        key = (self.headers.get("X-Api-Key") or "").strip()

        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"读不出请求内容：{exc}"})
            return

        if not key:
            key = str(payload.get("apiKey") or "").strip()
        if not key:
            self._json(400, {"ok": False, "kind": "no_key",
                             "error": "还没有填 API key。请点右上角的「⚙ API」填一个。"})
            return

        try:
            res = gen_proxy.generate(
                str(payload.get("requirement") or ""),
                key,
                model=str(payload.get("model") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False,
                             "error": f"{type(exc).__name__}: {exc}"})
            return

        # 出错时用 500 会让前端不好分辨「key 不对」和「服务坏了」，
        # 所以按 kind 给不同状态码。
        code = 200
        if not res.get("ok"):
            code = {"bad_key": 400, "no_key": 400, "auth": 401,
                    "bad_req": 400, "parse": 422}.get(res.get("kind"), 502)
        self._json(code, res)

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def reachable_urls(host: str, port: int, with_token: bool) -> list[tuple[str, str]]:
    """列出**实际能打开**的地址，每个附一句说明。

    为什么不再只给一个地址：实测出现过"给了个公网 IP，用户自己都打不开"的情况。
    宁可把可用地址全列出来，让人挨个试，也不要把人引到一个死地址上。
    """
    tok = f"?token={TOKEN}" if with_token else ""
    out: list[tuple[str, str]] = []
    out.append((f"http://127.0.0.1:{port}/{ENTRY}{tok}",
                "本机（就在这台电脑上打开，一定能开）"))
    if host in ("127.0.0.1", "localhost"):
        return out
    lip = lan_ip()
    if lip:
        out.append((f"http://{lip}:{port}/{ENTRY}{tok}",
                    f"局域网（同一网络里的其他电脑用这个）"))
    # 公网地址单独标出来，并说明它不一定能打开
    for ip in all_ipv4():
        if ip.startswith(("127.", "169.254.")) or ip == lip:
            continue
        if ip.startswith(("192.168.", "10.")) or ip.startswith("172."):
            continue
        out.append((f"http://{ip}:{port}/{ENTRY}{tok}",
                    "公网地址（**不一定能打开** —— 如果你在路由器/校园网后面，"
                    "需要做端口映射；自己从内部访问常常打不开）"))
    return out
    """那个端口上是不是**我们自己的**工作台服务？

    为什么必须区分：`pick_port` 一发现端口被占就顺延，于是
      · 旧服务还在 8787（窗口关了但进程没死）
      · 新启动的服务跑到 8788，浏览器打开 8788
      · 用户手上的旧标签还停在 8787 → 点「重新运行」→ 连不上 → "连接失败"
    这是实测抓到的真实故障。所以启动前先问一句：8787 上是不是已经在跑工作台？
    是的话直接复用它，不要再起一个新的。
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/__whoami",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("app") == APP_TAG
    except Exception:  # noqa: BLE001
        return False


def pick_port(preferred: int) -> int:
    import socket
    for port in range(preferred, preferred + 12):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def all_ipv4() -> list[str]:
    """列出本机所有 IPv4 地址。"""
    ips: list[str] = []
    try:
        import socket
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    # 再补一个：连外部地址看默认走哪张网卡
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return ips


def lan_ip() -> str | None:
    """挑一个**真正能在局域网里用**的地址。

    实测踩过的坑：我一开始用「连 8.8.8.8 看默认网卡」这个常见技巧，
    在你这台机器上返回的是 `117.64.142.177` —— 那是**出口公网地址**，
    不是局域网地址。用户拿它在自己浏览器里打不开（很多网络不允许
    从内部访问自己的公网 IP），于是看起来就像"服务坏了"。

    所以这里**优先挑私有网段**（192.168 / 10. / 172.16-31），
    并且**排除**：
      · 127.x       本机回环
      · 169.254.x   自动私有地址（说明那张网卡根本没拿到 IP）
      · 172.30.x    WSL 之类虚拟网卡常用段，宿主机上访问不到
    都没有就返回 None —— 宁可说"没有局域网地址"，也不要给一个打不开的。
    """
    cands = [ip for ip in all_ipv4()]
    priv: list[str] = []
    for ip in cands:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        parts = ip.split(".")
        if len(parts) != 4:
            continue
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if a == 192 and b == 168:
            priv.append(ip)
        elif a == 10:
            priv.append(ip)
        elif a == 172 and 16 <= b <= 31 and b != 30:   # 172.30.x 通常是 WSL
            priv.append(ip)
    if priv:
        return priv[0]
    return None


def local_ip() -> str:
    """兼容旧调用：优先局域网地址，没有就退回回环。"""
    return lan_ip() or "127.0.0.1"


def is_our_server(port: int, timeout: float = 1.5) -> bool:
    """那个端口上是不是**我们自己的**工作台服务？

    为什么必须区分：`pick_port` 一发现端口被占就顺延，于是
      · 旧服务还在 8787（窗口关了但进程没死）
      · 新启动的服务跑到 8788，浏览器打开 8788
      · 用户手上的旧标签还停在 8787 → 点「重新运行」→ 连不上 → "连接失败"
    这是实测抓到的真实故障。所以启动前先问一句：8787 上是不是已经在跑工作台？
    是的话直接复用它，不要再起一个新的。
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/__whoami",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("app") == APP_TAG
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="server.py",
        description="Verilog 工作台本地/局域网服务（带访问口令）")
    ap.add_argument("port", nargs="?", type=int, default=8787, help="端口，默认 8787")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。127.0.0.1=只本机；0.0.0.0=局域网/公网可访问")
    ap.add_argument("--token", default="", help="临时指定访问口令（不给就用 access_token.txt）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    ap.add_argument("--show-token", action="store_true", help="只打印口令就退出")
    args = ap.parse_args()

    global TOKEN, REQUIRE_TOKEN
    if args.token:
        TOKEN = args.token.strip()
    if args.show_token:
        print(TOKEN)
        return 0

    preferred = args.port
    host = args.host
    # 只在对外监听时强制口令；本机模式保持零摩擦
    REQUIRE_TOKEN = host not in ("127.0.0.1", "localhost")

    # 端口上已经有一个工作台在跑 → 直接复用，别再起一个（避免端口漂移）
    if is_our_server(preferred):
        url = f"http://127.0.0.1:{preferred}/{ENTRY}"
        print()
        print("  ┌──────────────────────────────────────────────┐")
        print("  │  工作台已经在跑了，直接复用                  │")
        print("  └──────────────────────────────────────────────┘")
        print(f"   地址: {url}")
        print()
        print("   提示：如果你手上的页面显示「连接失败」，")
        print("        说明它开在别的端口上 —— 用上面这个地址重开即可。")
        print()
        if not args.no_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        return 0

    port = pick_port(preferred)
    if port != preferred:
        print()
        print(f"  [提示] {preferred} 端口被别的程序占用了，改用 {port}。")

    httpd = ThreadingHTTPServer((host, port), Handler)
    use_token = auth_enabled()
    print()
    print("  ┌────────────────────────────────────────────────────────┐")
    print("  │  工作台已启动                                          │")
    print("  └────────────────────────────────────────────────────────┘")
    print(f"   监听      : {host}:{port}"
          + ("   （只本机可访问）" if host in ("127.0.0.1", "localhost")
             else "   （局域网/公网可访问）"))
    if use_token:
        print(f"   访问口令  : {TOKEN}")
    print()
    print("   能打开的地址（按推荐顺序）：")
    for u, why in reachable_urls(host, port, use_token):
        print(f"     {u}")
        print(f"         {why}")
    print()
    if use_token:
        print("   把上面那个【局域网】地址整条发给要用的人（里面带 token）。")
        print("   对方打开一次之后浏览器会记住口令，之后不用再带。")
        print("   改口令：编辑 access_token.txt，或设环境变量 DSH_WORKBENCH_TOKEN。")
    else:
        print("   本机模式，没有口令 —— 因为只绑了 127.0.0.1，外面连不进来。")
        print("   想让别人访问：双击 start-lan.bat（会自动开口令）。")
    print()
    if not lan_ip() and host not in ("127.0.0.1", "localhost"):
        print("   ⚠ 没检测到局域网地址（192.168.x / 10.x / 172.16-31.x）。")
        print("     你这台机器可能直接在运营商/校园网的大内网里，")
        print("     别的电脑要用「公网地址 + 端口映射」，或者用内网穿透。")
        print("     详见《部署与安全说明.md》。")
        print()
    print("   可访问的工程范围：")
    print(f"     {WORKSPACE}")
    print("   这个范围之外的目录会被拒绝（防止整盘浏览）。")
    print()
    print("   关掉这个窗口即可停止服务。")
    print()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
