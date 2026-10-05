"""屏幕 <-> guest 坐标变换：缩放 / 裁剪 / 旋转全部可逆，且不对任何尺寸硬编码。

布局（画布像素）→ guest 像素：
    1. 减去居中偏移、除以缩放，得到“旋转后内容”内的坐标
    2. 逆旋转回“旋转前内容”坐标
    3. 加上裁剪原点，就是 guest 帧缓冲坐标

旋转方向约定为顺时针，与 QImage/QTransform 一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ViewTransform:
    sx: float                      # 横向缩放（画布像素 / guest 像素）
    sy: float
    ox: float                      # 内容左上角在画布中的偏移
    oy: float
    rotation: int                  # 0 | 90 | 180 | 270（顺时针）
    content_w: int                 # 内容尺寸（裁剪后、旋转前）
    content_h: int
    crop_x: int                    # 内容在 guest 帧缓冲中的原点
    crop_y: int
    guest_w: int
    guest_h: int

    @property
    def rot_w(self) -> int:
        return self.content_h if self.rotation in (90, 270) else self.content_w

    @property
    def rot_h(self) -> int:
        return self.content_w if self.rotation in (90, 270) else self.content_h

    @property
    def disp_w(self) -> float:
        return self.rot_w * self.sx

    @property
    def disp_h(self) -> float:
        return self.rot_h * self.sy

    @property
    def uniform(self) -> bool:
        return abs(self.sx - self.sy) < 1e-9

    def describe(self) -> str:
        return (f"{self.content_w}x{self.content_h}@{self.sx:.3g}"
                f"{'°' + str(self.rotation) if self.rotation else ''}")


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _content_rect(guest_w: int, guest_h: int,
                  crop: Optional[dict]) -> tuple[int, int, int, int]:
    """裁剪后的内容尺寸与原点；裁剪越界时自动收敛。"""
    if crop and crop.get("enabled"):
        cx, cy = int(crop.get("x", 0)), int(crop.get("y", 0))
        cw, ch = int(crop.get("w", 0)), int(crop.get("h", 0))
        cw = max(1, min(cw, guest_w - cx))
        ch = max(1, min(ch, guest_h - cy))
        return cw, ch, max(0, cx), max(0, cy)
    return max(1, guest_w), max(1, guest_h), 0, 0


def build_transform(widget_w: int, widget_h: int, guest_w: int, guest_h: int, *,
                    mode: str = "fit", rotation: int = 0, crop: Optional[dict] = None,
                    zoom: float = 1.0, aspect: str = "keep",
                    dpi_scale: float = 1.0) -> ViewTransform:
    """按显示模式算出缩放与偏移。widget_w/h 为逻辑像素。"""
    rotation = rotation % 360
    if rotation not in (0, 90, 180, 270):
        rotation = 0
    cw, ch, crop_x, crop_y = _content_rect(guest_w, guest_h, crop)
    rot_w = ch if rotation in (90, 270) else cw
    rot_h = cw if rotation in (90, 270) else ch
    W = max(1, widget_w)
    H = max(1, widget_h)

    uniform = True
    if mode == "1:1":
        sx = sy = max(0.05, dpi_scale)
    elif mode == "free":
        sx = sy = max(0.05, zoom * dpi_scale)
    elif mode in ("stretch",) or (mode == "fit" and aspect == "ignore"):
        sx, sy = W / rot_w, H / rot_h
        uniform = False
    else:                                     # fit / fullscreen
        s = min(W / rot_w, H / rot_h) * dpi_scale
        sx = sy = max(0.01, s)

    if uniform:
        ox = (W - rot_w * sx) / 2.0
        oy = (H - rot_h * sy) / 2.0
    else:
        ox = oy = 0.0
    return ViewTransform(sx=sx, sy=sy, ox=ox, oy=oy, rotation=rotation,
                         content_w=cw, content_h=ch, crop_x=crop_x, crop_y=crop_y,
                         guest_w=max(1, guest_w), guest_h=max(1, guest_h))


def inverse_rotate(u: float, v: float, rotation: int,
                   cw: int, ch: int) -> tuple[float, float]:
    """旋转后坐标 -> 旋转前坐标。"""
    if rotation == 90:
        return v, (ch - 1) - u
    if rotation == 180:
        return (cw - 1) - u, (ch - 1) - v
    if rotation == 270:
        return (cw - 1) - v, u
    return u, v


def rotate(x: float, y: float, rotation: int, cw: int, ch: int) -> tuple[float, float]:
    """旋转前坐标 -> 旋转后坐标。"""
    if rotation == 90:
        return (ch - 1) - y, x
    if rotation == 180:
        return (cw - 1) - x, (ch - 1) - y
    if rotation == 270:
        return y, (cw - 1) - x
    return x, y


def screen_to_guest(px: float, py: float, t: ViewTransform) -> tuple[int, int]:
    """画布坐标 -> guest 像素（越界钳制到画面内）。"""
    u = (px - t.ox) / t.sx
    v = (py - t.oy) / t.sy
    cx, cy = inverse_rotate(u, v, t.rotation, t.content_w, t.content_h)
    gx = _clamp(cx + t.crop_x, 0, t.guest_w - 1)
    gy = _clamp(cy + t.crop_y, 0, t.guest_h - 1)
    return int(round(gx)), int(round(gy))


def guest_to_screen(gx: float, gy: float, t: ViewTransform) -> Optional[tuple[float, float]]:
    """guest 像素 -> 画布坐标；不在显示区域内时返回 None。"""
    cx, cy = gx - t.crop_x, gy - t.crop_y
    if not (0 <= cx < t.content_w and 0 <= cy < t.content_h):
        return None
    u, v = rotate(cx, cy, t.rotation, t.content_w, t.content_h)
    return t.ox + u * t.sx, t.oy + v * t.sy


def inside(px: float, py: float, t: ViewTransform) -> bool:
    return t.ox <= px < t.ox + t.disp_w and t.oy <= py < t.oy + t.disp_h