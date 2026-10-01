#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「拆分版」—— 视频不内联，跟 HTML 放在同一个文件夹里。

为什么要有这个版本
------------------
内联成 base64 会让体积**膨胀 33%**：
    7.44 MB 的视频  →  9.92 MB 的 base64  →  整页 10.15 MB
改成引用同级文件之后：
    236 KB 的 HTML  +  7.44 MB 的视频  =  7.67 MB
省下 2.5 MB，而且**双击就能看、视频照播、不需要联网、不需要服务器**。

实测确认过：file:// 下用相对路径引用同目录的 mp4，浏览器正常播放
（readyState=4、1920x1080、22 秒、error=无）。

产物
----
    pub-split/index.html      ← 双击这个
    pub-split/bgvideo.mp4     ← 视频，必须跟 HTML 待在同一个文件夹
    pub-split/avatar.webp     ← 头像（顺手一起放进去，省得还要内联）
    pub-split/工作台.zip       ← 打包好的，直接微信发这个

用法
----
    python build_split.py
"""
from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 用发布版做底：锁死配置、去掉编辑入口和密码弹窗。
# 早先这里用的是 workbench.html（可编辑版），自检里"编辑入口已移除"就过不了 ——
# 发给别人的东西不该带编辑功能。
SRC_HTML = HERE / "release.html"
OUT_DIR = HERE / "pub-split"
VIDEO = HERE / "bgvideo.mp4"
AVATAR = HERE / "avatar.webp"

VID_RE = re.compile(r'(/\*BGVIDEO\*/)(.*?)(/\*/BGVIDEO\*/)', re.S)
AVATAR_RE = re.compile(r'(/\*AVATAR\*/)(.*?)(/\*/AVATAR\*/)', re.S)

# 构建时用的哨兵值：页面看到它就走「引用同级 bgvideo.mp4」这条分支
SENTINEL = '"__EXTERNAL__"'


def strip_inline_video(html: str) -> tuple[str, bool]:
    """把内联的视频换成哨兵值。

    返回 (html, 是否确实去掉了内联视频)
    """
    m = VID_RE.search(html)
    if not m:
        return html, False
    had_data = m.group(2).strip().startswith('"data:')
    mb = len(m.group(2)) * 3 / 4 / 1024 / 1024
    html = VID_RE.sub(lambda _m: _m.group(1) + SENTINEL + _m.group(3), html, count=1)
    if had_data:
        print(f"  已去掉内联视频（原约 {mb:.2f} MB 的 base64）")
    else:
        print("  视频段本来就是空的，改为引用同级文件")
    return html, had_data


def strip_inline_avatar(html: str) -> str:
    """头像也改成引用同级文件，省掉内联的那几 KB。

    头像很小（5.5 KB），但既然已经是多个文件了，放成独立文件更清爽，
    也方便以后单独换。
    """
    m = AVATAR_RE.search(html)
    if not m or not m.group(2).strip().startswith('"data:'):
        return html
    return AVATAR_RE.sub(lambda _m: _m.group(1) + '"avatar.webp"' + _m.group(3), html, count=1)


def make_zip(files: list[Path], out: Path) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            # 放进 zip 时统一用 ASCII 文件名：
            # 一是个别解压工具会把中文名搞乱，
            # 二是对方在英文系统上也能正常解压。
            arc = {"index.html": "index.html",
                   "bgvideo.mp4": "bgvideo.mp4",
                   "avatar.webp": "avatar.webp",
                   "README.txt": "README.txt"}.get(f.name, f.name)
            z.write(f, arc)


def main() -> int:
    if not SRC_HTML.is_file():
        raise SystemExit(f"缺少 {SRC_HTML.name}，先跑 build.py")
    if not VIDEO.is_file():
        raise SystemExit(f"缺少 {VIDEO.name}")

    html = SRC_HTML.read_text(encoding="utf-8")
    html, _ = strip_inline_video(html)
    html = strip_inline_avatar(html)

    # 顺带把「本地运行 · 不联网 · 单文件」这种说法改准确
    html = html.replace("本地运行 · 不联网 · 单文件", "本地运行 · 不联网 · 双击即看")
    html = html.replace("发布版 · 仅供查看", "发布版 · 双击即看")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")
    shutil.copy2(VIDEO, OUT_DIR / VIDEO.name)
    if AVATAR.is_file():
        shutil.copy2(AVATAR, OUT_DIR / AVATAR.name)

    files = [OUT_DIR / "index.html", OUT_DIR / VIDEO.name]
    if (OUT_DIR / AVATAR.name).is_file():
        files.append(OUT_DIR / AVATAR.name)
    # 附一份说明：对方解压后可能不知道该点哪个，
    # 也可能从压缩包里直接双击 index.html（那样视频找不到）。
    for extra in ("README.txt", "HOW-TO-RUN.txt"):
        f = OUT_DIR / extra
        if f.is_file():
            files.append(f)

    zp = OUT_DIR / "工作台.zip"
    make_zip(files, zp)

    total = sum(f.stat().st_size for f in files)
    print()
    print(f"  输出目录 -> {OUT_DIR}")
    for f in files:
        print(f"    {f.name:16} {f.stat().st_size/1024:9.1f} KB")
    print(f"    {'合计':16} {total/1024/1024:9.2f} MB")
    print(f"    {zp.name:16} {zp.stat().st_size/1024/1024:9.2f} MB   ← 发这个")
    print(f"  对比内联版: {SRC_HTML.stat().st_size/1024/1024:.2f} MB"
          f"  →  省 {(SRC_HTML.stat().st_size-total)/1024/1024:.2f} MB")

    # 自检
    txt = (OUT_DIR / "index.html").read_text(encoding="utf-8")
    checks = [
        ("内联视频已移除", "data:video/" not in txt),
        ("哨兵值已注入", "__EXTERNAL__" in txt),
        ("视频是相对路径", '"bgvideo.mp4"' in txt or "bgvideo.mp4" in txt),
        ("没有留下盘符绝对路径", not re.search(r'[A-Za-z]:\\\\+[^"\']*bgvideo', txt)),
        ("编辑入口已移除", 'id="btnEdit"' not in txt),
        ("探测数据还在", '"steps"' in txt),
        ("视频文件已就位", (OUT_DIR / "bgvideo.mp4").is_file()),
        ("zip 已生成", zp.is_file()),
    ]
    print()
    for name, ok in checks:
        print(f"    自检 {name}: {'OK' if ok else '失败'}")
    return 0 if all(ok for _n, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
