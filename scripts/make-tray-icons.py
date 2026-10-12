"""生成托盘图标：运行中 / 已暂停 / 出错。

纯标准库实现（zlib + struct），不依赖 Pillow —— 这台机器上装没装 Pillow 不确定，
而图标只是三个纯色气泡，没必要为它引入一个图像库。

用法：
    python scripts/make-tray-icons.py

输出到 desktop/src-tauri/icons/tray-{run,paused,error}.png（64x64 RGBA）。
生成逻辑用 4 倍超采样做抗锯齿：托盘实际显示约 16px，硬边会很难看。
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZE = 64
SS = 4  # 超采样倍率（每边采 SS 个点后取平均当覆盖率）

# 状态 -> 填充色（与面板上的状态点保持同一套语义：绿=在跑，灰=暂停，红=出错）
STATES: dict[str, tuple[int, int, int]] = {
    "tray-run": (0x16, 0xA3, 0x4A),
    "tray-paused": (0x6B, 0x72, 0x80),
    "tray-error": (0xDC, 0x26, 0x26),
}

# 气泡外形（64x64 坐标系内）
BUBBLE = (6.0, 6.0, 58.0, 48.0, 12.0)  # left, top, right, bottom, radius
TAIL = ((17.0, 42.0), (17.0, 62.0), (37.0, 42.0))  # 左下角的小尾巴
DOTS = ((22.0, 27.0), (32.0, 27.0), (42.0, 27.0))
DOT_R = 3.6


def _rounded_rect_sdf(x: float, y: float, l: float, t: float, r: float, b: float, rad: float) -> float:
    """圆角矩形的有向距离：<=0 表示在内部。"""
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    hw, hh = (r - l) / 2.0, (b - t) / 2.0
    qx = abs(x - cx) - (hw - rad)
    qy = abs(y - cy) - (hh - rad)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - rad


def _in_triangle(x: float, y: float, tri) -> bool:
    """重心坐标同号法判点在三角形内（不关心边界的半像素误差）。"""
    (x1, y1), (x2, y2), (x3, y3) = tri
    d1 = (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
    d2 = (x - x3) * (y2 - y3) - (x2 - x3) * (y - y3)
    d3 = (x - x1) * (y3 - y1) - (x3 - x1) * (y - y1)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _sample(x: float, y: float) -> tuple[bool, bool]:
    """返回 (是否属于气泡, 是否属于白点)。"""
    l, t, r, b, rad = BUBBLE
    in_bubble = _rounded_rect_sdf(x, y, l, t, r, b, rad) <= 0.0 or _in_triangle(x, y, TAIL)
    if not in_bubble:
        return False, False
    for dx, dy in DOTS:
        if math.hypot(x - dx, y - dy) <= DOT_R:
            return True, True
    return True, False


def render(color: tuple[int, int, int]) -> bytes:
    """按 4 倍超采样渲染一张 RGBA 图。"""
    px = bytearray(SIZE * SIZE * 4)
    step = 1.0 / SS
    for py in range(SIZE):
        for pxx in range(SIZE):
            hits = 0
            dot_hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = pxx + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    in_b, in_d = _sample(x, y)
                    if in_b:
                        hits += 1
                        if in_d:
                            dot_hits += 1
            if hits == 0:
                continue
            total = SS * SS
            cov = hits / total
            dot_cov = dot_hits / total
            # 白点覆盖处向白色混合，混合量 = 白点覆盖率 / 气泡覆盖率
            mix = (dot_cov / cov) if cov > 0 else 0.0
            r = int(round(color[0] * (1 - mix) + 255 * mix))
            g = int(round(color[1] * (1 - mix) + 255 * mix))
            bl = int(round(color[2] * (1 - mix) + 255 * mix))
            i = (py * SIZE + pxx) * 4
            px[i] = r
            px[i + 1] = g
            px[i + 2] = bl
            px[i + 3] = int(round(cov * 255))
    return bytes(px)


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    raw = b"".join(
        b"\x00" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, color in STATES.items():
        target = out_dir / (name + ".png")
        write_png(target, SIZE, SIZE, render(color))
        print("已生成 %s（%d 字节）" % (target.name, target.stat().st_size))


if __name__ == "__main__":
    main()
