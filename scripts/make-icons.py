"""生成面板的手机主屏图标（web/icon-192.png、icon-512.png、apple-touch-icon.png）。

改动图标后跑一次即可：

    python scripts/make-icons.py

设计：主题绿（面板的 --accent #3aa675）竖向渐变铺满整块，中央一个白色气泡，
气泡里是「三」（三哥的三弟）。整块铺满 + 内容收在中央 80% 安全区内，这样
Android 自己裁圆角、iOS 自己裁圆角都不会切到主体，purpose=any/maskable 可共用一张。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WEB = Path(__file__).resolve().parent.parent / "web"

ACCENT_TOP = (63, 179, 126)      # #3fb37e
ACCENT_BOTTOM = (32, 122, 84)    # #207a54
BUBBLE = (255, 255, 255)
INK = (7, 45, 26)                # 深绿，写在白气泡上
FONT_PATH = r"C:\Windows\Fonts\msyhbd.ttc"

# 以下坐标都以 512×512 为基准，画的时候按比例缩放。
BOX = (96, 120, 416, 360)
TAIL = [(150, 345), (150, 438), (232, 345)]
RADIUS = 64
FONT_SIZE = 150


def draw_icon(size: int = 1024) -> Image.Image:
    """画一张 size×size 的图（先大后小，缩小后边缘更干净）。"""
    s = size / 512.0
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)

    # 竖向渐变：逐行画线，1024 行足够细腻，也不用装 numpy
    for y in range(size):
        t = y / (size - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(ACCENT_TOP, ACCENT_BOTTOM))
        draw.line([(0, y), (size, y)], fill=color)

    box = tuple(round(v * s) for v in BOX)
    draw.rounded_rectangle(box, radius=round(RADIUS * s), fill=BUBBLE)
    draw.polygon([(round(x * s), round(y * s)) for x, y in TAIL], fill=BUBBLE)

    font = ImageFont.truetype(FONT_PATH, round(FONT_SIZE * s))
    left, top, right, bottom = draw.textbbox((0, 0), "三", font=font)
    draw.text(
        ((box[0] + box[2]) / 2 - (right - left) / 2 - left,
         (box[1] + box[3]) / 2 - (bottom - top) / 2 - top),
        "三", font=font, fill=INK,
    )
    return img


def main() -> int:
    base = draw_icon(1024)
    for name, px in (("icon-512.png", 512), ("icon-192.png", 192),
                     ("apple-touch-icon.png", 180), ("favicon.png", 48)):
        out = WEB / name
        base.resize((px, px), Image.LANCZOS).save(out, "PNG", optimize=True)
        print("写出 %s (%d×%d, %d 字节)" % (out.name, px, px, out.stat().st_size))

    # Windows 图标：桌面快捷方式只能吃 .ico，PNG 不行
    out = WEB / "favicon.ico"
    base.save(out, "ICO", sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
    print("写出 %s (%d 字节)" % (out.name, out.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
