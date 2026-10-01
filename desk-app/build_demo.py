#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「演示版」单文件网页 —— 给老师/同学看用，手机秒开。

背景：正式版把 7.44 MB 的背景视频内联成 base64，占整页 97.8%，
      整页 10.15 MB。通过内网穿透发出去时，手机上要等很久，
      结果就是「只看到文字，视频永远加载不出来」。
      实测确认过：内联视频 base64 9.92 MB / 整页 10.15 MB。

演示版做的事：
  1. 去掉内联视频，换成 CSS 渐变背景         → 体积 10.15 MB 降到 ~250 KB
  2. 探测结果照常内联（本来就只有几 KB）      → 老师看到的是真实跑出来的数据
  3. 入口按钮改成「查看上次探测结果」         → 没有后端也能看到内容
  4. 顶部加一条说明，写清这是什么、数据从哪来

用法：
    python build_demo.py            # 生成 demo.html
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELEASE = HERE / "release.html"
OUT = HERE / "demo.html"

# 内联视频：占位符之间的那一段 data:video/...;base64,xxx
VID_RE = re.compile(r'(/\*BGVIDEO\*/)("data:video/[a-z0-9]+;base64,[A-Za-z0-9+/=]+")'
                    r'(/\*/BGVIDEO\*/)')

# 没有视频时用的背景：深海底色的渐变。
#
# 为什么偏暗：页面上的标题和正文是白字。原来视频画面本身就暗，
# 所以文字清楚；一开始我用的渐变偏亮（中间是亮青绿），白字压上去就发灰、
# 几乎看不清（截图验证发现的）。深色底 + 一点点亮部，既像深海又不影响阅读。
GRADIENT = (
    "radial-gradient(120% 100% at 78% 18%,"
    "rgba(38,158,150,.42) 0%, rgba(20,96,104,.30) 34%, rgba(6,34,46,0) 68%),"
    "radial-gradient(90% 80% at 15% 88%,"
    "rgba(26,120,116,.30) 0%, rgba(8,40,52,0) 62%),"
    "linear-gradient(168deg,"
    "#06222e 0%,#08303c 22%,#0a3d4a 44%,"
    "#0b4750 62%,#083039 82%,#041a24 100%)"
)

BANNER_ID = "demoBanner"


def build_demo(release_html: str, board_stamp: str) -> str:
    html = release_html

    # ---- 1. 拿掉内联视频，换成渐变 ----
    m = VID_RE.search(html)
    if m:
        classes = ""  # 保持原有 style 属性，只把 src 换掉由 CSS 兜底
        html = VID_RE.sub(
            lambda _m: (_m.group(1) + '""' + _m.group(3)), html, count=1)
        # 给 <video> 打上标记，CSS 里用它显示渐变背景
        html = html.replace("<video", '<video data-novideo="1"', 1)
        removed_mb = len(m.group(2)) * 3 / 4 / 1024 / 1024
        print(f"  已移除内联视频（原约 {removed_mb:.2f} MB）")
    else:
        print("  [提醒] 没找到内联视频，可能 release.html 已经是轻量版")

    # ---- 2. 渐变背景的 CSS ----
    css = f"""
  /* ---------------- 演示版：无视频背景 ---------------- */
  /* 正式版内联了 7.44 MB 的视频，手机通过内网穿透加载很慢；
     演示版换成同色系渐变，体积降到零，观感一致。

     注意层级：原有的 #bgmedia 已经是 position:fixed; z-index:0 的背景层，
     而 .landing 是 z-index:1。所以直接把渐变刷在 #bgmedia 自己身上最稳 ——
     再插一个同层级的 div 会盖住正文（一开始就是这么错的，
     截图发现整屏被渐变糊住，文字全不见了）。 */
  body.demo video[data-novideo]{{
    display:none !important;
  }}
  body.demo #bgmedia{{
    display:block !important;
    background:{GRADIENT} !important;
  }}
  /* 演示版是静态展示页，动画只会添乱：
     实测发现 #viewLanding 的淡入被冻在 opacity:0.11，
     整页看起来灰蒙蒙的（用 JS 查计算样式查出来的）。
     所以这里干脆把入场动画关掉，一切直接到位。 */
  body.demo .view,
  body.demo .view.on,
  body.demo .landing,
  body.demo #viewLanding,
  body.demo #viewRun{{
    opacity:1 !important;
    transform:none !important;
    filter:none !important;
    clip-path:none !important;
  }}  /* 文字颜色：正式版靠视频的暗背景衬托白字，这里背景是渐变，
     所以把浅色文字显式定死，免得落在深色底上看不见。 */
  body.demo .landtop,
  body.demo #heroTitle,
  body.demo #heroTitle .tl-i,
  body.demo #heroLede,
  body.demo .brand,
  body.demo #brandName{{
    color:#f2fbf9 !important;
  }}
  body.demo #heroLede{{ color:rgba(238,250,248,.86) !important; }}
  body.demo .mark{{ color:#0b2b33 !important; }}
  .demo-banner{{
    position:fixed; left:0; right:0; bottom:0; z-index:40;
    background:rgba(10,20,26,.82); color:#e8f4f2;
    font:12.5px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    padding:9px 16px; backdrop-filter:blur(6px);
    display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  }}
  .demo-banner b{{color:#7fe3cd}}
  .demo-banner .sp{{flex:1}}
  .demo-banner button{{
    border:1px solid rgba(255,255,255,.26); background:transparent; color:#e8f4f2;
    font:inherit; padding:3px 11px; border-radius:999px; cursor:pointer;
  }}
  .demo-banner button:hover{{background:rgba(255,255,255,.1)}}
</style>"""
    html = html.replace("</style>", css, 1)

    # ---- 3. 底部说明条 ----
    banner = f"""
<div class="demo-banner" id="{BANNER_ID}">
  <span><b>演示版</b> · 数据来自真实跑过的 iverilog / verilator / yosys</span>
  <span style="opacity:.6">构建于 {board_stamp}</span>
  <span class="sp"></span>
  <span style="opacity:.72">已录制的结果，不会真的重新跑</span>
  <button id="demoHide">知道了</button>
</div>
</body>"""
    html = html.replace("</body>", banner, 1)

    html = html.replace("<body", '<body class="demo"', 1)

    # ---- 4. 入口按钮改成「看上次结果」，并加一段说明 ----
    # 正式版点它是去调后端探测（showRun(false)）；演示版没有后端，
    # 改成回放已录制的日志（showRun(true)）。
    # 按钮的真实写法是  <button class="pill dark big" id="btnStart">▶ 开始运行</button>
    html = html.replace(
        '<button class="pill dark big" id="btnStart">▶ 开始运行</button>',
        '<button class="pill dark big" id="btnStart" data-demo-replay="1">'
        '▶ 查看录制结果</button>')
    # 兜底：万一上面的精确串没匹配上，就标记 id 本身
    if "data-demo-replay" not in html:
        html = html.replace('id="btnStart"', 'id="btnStart" data-demo-replay="1"', 1)

    # 回放逻辑：拦截点击，走 replaySaved 而不是 runProbe
    patch = """
<script>
/* 演示版补丁：没有后端，所以「开始运行」改成回放已录制的探测日志。 */
(function(){
  var hide = document.getElementById('demoHide');
  var bar  = document.getElementById('demoBanner');
  if (hide && bar) hide.addEventListener('click', function(){ bar.style.display = 'none'; });

  var btn = document.getElementById('btnStart');
  if (!btn) return;
  // 用捕获阶段先下手，拦掉原来的 runProbe
  btn.addEventListener('click', function(e){
    e.stopImmediatePropagation();
    e.preventDefault();
    if (typeof showRun === 'function') showRun(true);   // true = replay
  }, true);
})();
</script>
</body>"""
    html = html.replace("</body>", patch, 1)

    # ---- 5. 关掉入场动效 ----
    # 为什么必须关：页面的启动顺序是
    #     renderStatic();   // 把「上次探测：2026-xx-xx」写进 DOM
    #     ... FX.opening(); // GSAP 入场动画，会把这些元素重置回去
    # 结果界面上只剩一个破折号（实测：内存里 board.project.updatedAt 有值，
    # 但 #landHint 的 textContent 是 "上次探测：—"，手动再调一次 renderStatic
    # 就正常了 —— 于是定位到是动画覆盖的）。
    # 页面本来就支持 ?nofx=1 跳过全部动效，这里直接把它固化成默认行为。
    html = html.replace(
        "  const nofx = q.has(\"nofx\");",
        "  const nofx = true;  /* 演示版：固定跳过入场动效，见 build_demo.py 的说明 */")
    return html


def main() -> int:
    if not RELEASE.is_file():
        raise SystemExit(f"缺少 {RELEASE.name}，先跑 build.py")

    src = RELEASE.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 尽量用 board 里的探测时间来标注
    m = re.search(r'"updatedAt"\s*:\s*"([^"]+)"', src)
    if m:
        stamp = m.group(1)

    html = build_demo(src, stamp)
    OUT.write_text(html, encoding="utf-8")

    before = RELEASE.stat().st_size
    after = OUT.stat().st_size
    print(f"  演示版 -> {OUT.name}  "
          f"({before/1024/1024:.2f} MB -> {after/1024:.0f} KB，"
          f"缩小到 {after/before*100:.1f}%)")

    # 顺手拷一份到桌面：它是一个自包含的单文件，
    # 直接发给别人（微信/邮件）对方双击就能看，不需要服务器。
    try:
        import os
        desk = Path(os.path.expanduser("~")) / "Desktop"
        if desk.is_dir():
            target = desk / "工作台演示.html"
            target.write_text(html, encoding="utf-8")
            print(f"  已放到桌面 -> {target.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [提醒] 拷到桌面失败：{exc}")

    # 自检
    checks = [
        ("视频已移除", "data:video/" not in html),
        ("渐变背景已注入", "body.demo #bgmedia" in html),
        ("说明条已注入", BANNER_ID in html),
        ("回放补丁已注入", "data-demo-replay" in html),
        ("探测数据还在", '"steps"' in html),
        ("编辑入口已移除", 'id="btnEdit"' not in html),
    ]
    for name, ok in checks:
        print(f"    自检 {name}: {'OK' if ok else '失败'}")
    return 0 if all(ok for _n, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
