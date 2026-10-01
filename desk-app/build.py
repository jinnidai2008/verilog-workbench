#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从模板生成两个版本：

  workbench.html   可编辑版 —— 带编辑面板，需要口令才能进编辑模式
  release.html     发布版   —— 编辑功能整体移除、配置烤死，别人改不了

真实数据（board.json）在构建时就写进 HTML，所以双击打开即为最新状态，
不依赖 fetch（file:// 下浏览器会拦截 fetch）。

用法: python build.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "index.html"
BOARD = HERE / "board.json"
OUT_EDIT = HERE / "workbench.html"
OUT_RELEASE = HERE / "release.html"

BEGIN = "/*BOARD_DATA*/"
END = "/*/BOARD_DATA*/"
BH_BEGIN = "/*BLACKHOLE*/"
BH_END = "/*/BLACKHOLE*/"
VID_BEGIN = "/*BGVIDEO*/"
VID_END = "/*/BGVIDEO*/"
VENDOR_BEGIN = "/*VENDOR*/"
VENDOR_END = "/*/VENDOR*/"
AVATAR_BEGIN = "/*AVATAR*/"
AVATAR_END = "/*/AVATAR*/"


def inject_avatar(html: str, board: dict) -> str:
    """把头像内联成 data URI（文件很小，内联后单文件自带头像）。"""
    pattern = re.compile(re.escape(AVATAR_BEGIN) + r".*?" + re.escape(AVATAR_END), re.S)
    if not pattern.search(html):
        return html
    media = board.get("media") or {}
    name = media.get("avatar") or "avatar.webp"
    path = Path(name) if Path(name).is_absolute() else (HERE / name)
    if not path.is_file():
        print(f"提示：找不到头像 {name}，将回退为文字标识")
        return pattern.sub(lambda m: AVATAR_BEGIN + '""' + AVATAR_END, html, count=1)
    import base64
    ext = path.suffix.lower().lstrip(".")
    mime = {"webp": "image/webp", "png": "image/png",
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}.get(ext, "image/webp")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    print(f"已内联头像 {path.name}  {path.stat().st_size / 1024:.1f} KB")
    return pattern.sub(lambda m: AVATAR_BEGIN + f'"data:{mime};base64,{b64}"' + AVATAR_END,
                       html, count=1)


def inject_vendor(html: str) -> str:
    """把 GSAP + ScrollTrigger 内联进来，保持单文件离线可用。"""
    pattern = re.compile(re.escape(VENDOR_BEGIN) + r".*?" + re.escape(VENDOR_END), re.S)
    if not pattern.search(html):
        return html
    parts, total = [], 0
    for name in ("gsap.min.js", "ScrollTrigger.min.js"):
        f = HERE / "vendor" / name
        if f.exists():
            parts.append(f.read_text(encoding="utf-8"))
            total += f.stat().st_size
    if not parts:
        print("提示：vendor 里没有 GSAP，动效将自动跳过（页面仍是完整静态版）")
        return pattern.sub(lambda m: VENDOR_BEGIN + '""' + VENDOR_END, html, count=1)
    print(f"已内联动效库 GSAP + ScrollTrigger  {total / 1024:.0f} KB")
    # 注意：用 lambda 做替换，避免 re.sub 把 JS 源码里的 \d 之类当转义解析
    payload = "\n" + "\n".join(parts) + "\n"
    return pattern.sub(lambda m: VENDOR_BEGIN + payload + VENDOR_END, html, count=1)
# 小于这个体积就内联进 HTML（这样单文件到哪都能播）
# 内联上限 —— 为什么压到 1 MB
#
# 原来是 12 MB，于是 7.44 MB 的背景视频被内联成了 9.92 MB 的
# data:video/mp4;base64,... URI。实测结果：**浏览器直接拒绝解码**
#     video.error.code = 4  (MEDIA_ERR_SRC_NOT_SUPPORTED)
#     readyState = 0, videoWidth = 0, paused = true
# 也就是说「单文件版」的背景视频从来就没播出来过，页面一直是白底。
# 这个结论是在 workbench.html 和 release.html 上分别验证过的，都一样。
#
# 所以改成超过 1 MB 就不内联 —— 让它退回「引用同目录的 bgvideo.mp4」，
# 那条路是好的（拆分版就是这么做的，视频正常）。
# 视频用文件引用更省体积，也不用担心浏览器的大小限制。
INLINE_LIMIT = 1 * 1024 * 1024


def inject_bgvideo(html: str, board: dict) -> str:
    """把体积够小的背景视频内联成 data URI。

    内联的好处：不管用 file:// 还是服务器打开都能播，文件发给别人也一样。
    """
    pattern = re.compile(re.escape(VID_BEGIN) + r".*?" + re.escape(VID_END), re.S)
    if not pattern.search(html):
        return html

    media = board.get("media") or {}
    src = media.get("src") or ""
    kind = media.get("kind")
    if kind != "video" or not src or src.startswith("data:"):
        return pattern.sub(VID_BEGIN + '""' + VID_END, html, count=1)

    # /bgvideo/<文件名> 或本地绝对路径，都解析到磁盘上的文件
    path = None
    if src.startswith("/bgvideo/"):
        name = src[len("/bgvideo/"):].split("?")[0]
        from urllib.parse import unquote
        name = unquote(name)
        cand = (media.get("videos") or {}).get(name) or str(HERE / name)
        path = Path(cand)
    else:
        path = Path(src)

    if not path or not path.is_file():
        print(f"提示：找不到背景视频文件（{src}），改由服务流式提供")
        return pattern.sub(VID_BEGIN + '""' + VID_END, html, count=1)

    size = path.stat().st_size
    if size > INLINE_LIMIT:
        # 这句提示原来写的是「file:// 打开时不播」—— 那是内联方案的遗留假设，
        # 实测是错的：视频作为同目录文件引用时，file:// 下照样正常播放
        # （readyState=4 / 1920x1080 / paused=false / error=无，浏览器实测过）。
        # 发给别人的拆分版正是靠这条路，所以别再用错的提示吓人。
        print(f"提示：背景视频 {size / 1024 / 1024:.1f} MB 超过内联上限，"
              f"改为引用同目录的 {path.name}（file:// 下也能播）")
        return pattern.sub(VID_BEGIN + '""' + VID_END, html, count=1)

    import base64
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    print(f"已内联背景视频 {path.name}  {size / 1024 / 1024:.2f} MB -> data URI")
    return pattern.sub(VID_BEGIN + f'"data:video/mp4;base64,{b64}"' + VID_END, html, count=1)


def inject_blackhole(html: str) -> str:
    """把 blackhole.gif 内联成 data URI，保证单文件离线可用。"""
    pattern = re.compile(re.escape(BH_BEGIN) + r".*?" + re.escape(BH_END), re.S)
    if not pattern.search(html):
        return html
    gif = HERE / "blackhole.gif"
    if not gif.exists():
        print("警告：没有 blackhole.gif，先跑 tools/make_blackhole.py")
        return pattern.sub(BH_BEGIN + '""' + BH_END, html, count=1)
    import base64
    b64 = base64.b64encode(gif.read_bytes()).decode("ascii")
    print(f"已内联黑洞动画 {gif.stat().st_size / 1024:.0f} KB -> data URI")
    return pattern.sub(BH_BEGIN + f'"data:image/gif;base64,{b64}"' + BH_END, html, count=1)


def normalize_media(board: dict) -> dict:
    """把 media.src 里的本地绝对路径换成 /bgvideo/<文件名>，并登记到 media.videos。

    这样大视频不进 HTML，由本地服务流式播放。
    """
    media = board.get("media")
    if not isinstance(media, dict):
        return board
    src = media.get("src") or ""
    videos = dict(media.get("videos") or {})
    if src and not src.startswith(("data:", "http://", "https://", "/bgvideo/")):
        p = Path(src)
        videos.setdefault(p.name, str(p))
        media["src"] = "/bgvideo/" + p.name
        print(f"背景媒体走流式：{p.name}")
    media["videos"] = videos
    board["media"] = media
    return board


def encode_payload(board: dict) -> str:
    """把数据编码成纯 ASCII 的 base64。

    这样载荷里不会有引号、反斜杠、换行，绝不会干扰 HTML/CSS/JS 的解析。
    """
    import base64
    raw = json.dumps(board, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def inject_board(html: str, board: dict) -> str:
    payload = f'"{encode_payload(board)}"'
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if not pattern.search(html):
        raise SystemExit("模板里找不到 BOARD_DATA 注入点")
    return pattern.sub(BEGIN + payload + END, html, count=1)


def make_release(html: str, cfg_patch: dict) -> str:
    """发布版：锁死配置 + 移除全部编辑入口。"""
    # 0. 给元素查询函数加一层兜底（**这一步是必须的，不是可选的**）
    #
    # 为什么：上面删掉的是编辑面板的 HTML，但绑定这些控件的 JS 还在。
    # 实测扫描发现，发布版里有 43 处 $("xxx") 会拿到 null 然后立刻调方法：
    #     $("radius").addEventListener(...)
    # 第一个这样的调用就抛 TypeError，把整个 <script> 块打断 ——
    # 后面的 const cfg / const FX 全都没机会定义。
    # 症状极具迷惑性：页面 HTML 已经渲染出来了，看着正常，
    # 但「上次探测」永远是空破折号、按钮点了没反应、配置不生效。
    #
    # 这里换个思路：不让调用方去防，而是让**查不到元素时返回一个哑对象**，
    # 所有属性和方法调用都变成无害的空操作。这样 JS 能完整跑完，
    # 本机版的行为一点不变（本机版所有 id 都存在，走的是正常分支）。
    html = html.replace(
        'const $ = id => document.getElementById(id);',
        '''/* 发布版兜底：编辑面板的 HTML 被删掉了，但绑定它的 JS 还在。
   若查不到元素就返回 null，第一次 .addEventListener 就会抛异常并打断
   整个脚本块 —— 后面的 cfg / FX 都定义不出来，页面看着能显示、
   实际所有交互都是死的（实测：43 处这样的调用）。
   所以查不到时返回一个哑对象，属性读写和方法调用都当无事发生。
   本机版所有 id 都存在，走的是正常分支，行为完全不变。 */
const __stub = new Proxy(function(){}, {
  get(t, k){
    if(k === "style") return __stub;
    if(k === "classList") return { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
    if(k === "dataset") return {};
    if(k === "value" || k === "checked" || k === "textContent"
       || k === "innerHTML" || k === "href" || k === "src") return "";
    if(k === "children" || k === "childNodes") return [];
    if(k === "length") return 0;
    if(k === Symbol.toPrimitive || k === "toString") return () => "";
    return __stub;
  },
  set(){ return true; },
  apply(){ return __stub; }
});
const $ = (id => {
  const cache = new Map();
  return key => {
    const el = document.getElementById(key);
    if(el) return el;
    if(!cache.has(key)) cache.set(key, __stub);
    return cache.get(key);
  };
})();''')

    # 1. 锁死
    html = html.replace(
        '  password: "2008",\n  locked: false',
        f'  password: "{cfg_patch["password"]}",\n  locked: true',
    )
    # 2. 移除编辑入口按钮 + 编辑面板 + 密码弹窗（发布版必须一个不留）
    html = re.sub(r'<button[^>]*class="[^"]*editentry[^"]*"[^>]*>.*?</button>\s*',
                  "", html, flags=re.S)
    html = re.sub(r'<button[^>]*class="[^"]*editchrome[^"]*"[^>]*>.*?</button>\s*',
                  "", html, flags=re.S)
    html = re.sub(r'<div class="panel editchrome" id="panel">.*?\n</div>\n', "\n", html, flags=re.S)
    html = re.sub(r'<div class="modal" id="modal">.*?\n</div>\n', "\n", html, flags=re.S)
    # 4. 发布说明
    html = html.replace("本地运行 · 不联网 · 单文件", "发布版 · 仅供查看")
    return html


def main() -> int:
    if not TEMPLATE.exists():
        raise SystemExit(f"缺少模板 {TEMPLATE}")

    html = TEMPLATE.read_text(encoding="utf-8")

    if BOARD.exists():
        board = json.loads(BOARD.read_text(encoding="utf-8"))
        board = normalize_media(board)
        html = inject_board(html, board)
        stamp = board.get("project", {}).get("updatedAt", "—")
        print(f"已注入真实数据（{stamp}）")
    else:
        print("警告：没有 board.json，先跑 refresh.py；本次生成使用内置占位数据")

    html = inject_blackhole(html)
    html = inject_bgvideo(html, board if BOARD.exists() else {})
    html = inject_avatar(html, board if BOARD.exists() else {})
    html = inject_vendor(html)

    OUT_EDIT.write_text(html, encoding="utf-8")
    print(f"可编辑版 -> {OUT_EDIT.name}  ({OUT_EDIT.stat().st_size / 1024:.1f} KB)")

    release = make_release(html, {"password": "2008"})
    OUT_RELEASE.write_text(release, encoding="utf-8")
    print(f"发布版   -> {OUT_RELEASE.name}  ({OUT_RELEASE.stat().st_size / 1024:.1f} KB)")

    # 自检
    locked_ok = "locked: true" in release
    edit_gone = 'id="btnEdit"' not in release and 'id="panel"' not in release
    print(f"自检: 锁定={locked_ok} 编辑入口已移除={edit_gone}")
    print(f"构建时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
