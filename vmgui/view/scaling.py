"""缩放：把 guest 帧变成画布要显示的图像。

QImage 自带 FastTransformation（最近邻）与 SmoothTransformation（平滑）。
Qt 没有独立的 lanczos，这里把 lanczos 归到平滑（如需真 lanczos，可在此处接
Pillow 后用 ``fromPILImage``）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QTransform

from ..backends.base import Frame

_ALGO = {
    "nearest": Qt.FastTransformation,
    "bilinear": Qt.SmoothTransformation,
    "lanczos": Qt.SmoothTransformation,
}


def algo_names() -> list[str]:
    return list(_ALGO)


def to_qimage(frame: Frame) -> QImage:
    """Frame -> QImage。.copy() 让 QImage 自持数据，避免引用已释放的 bytes。"""
    fmt_map = {
        "rgb888": QImage.Format_RGB888,
        "bgr888": QImage.Format_BGR888,
        "xrgb8888": QImage.Format_RGB32,
        "rgb565": QImage.Format_RGB16,
    }
    fmt = fmt_map.get(frame.fmt)
    if fmt is None:
        raise ValueError(f"不支持的像素格式: {frame.fmt}")
    img = QImage(frame.pixels, frame.width, frame.height, frame.stride, fmt)
    return img.copy()


def rotate_image(img: QImage, rotation: int) -> QImage:
    rotation = rotation % 360
    if rotation not in (90, 180, 270):
        return img
    return img.transformed(QTransform().rotate(rotation), Qt.FastTransformation)


def scale_image(img: QImage, w: int, h: int, algo: str = "nearest") -> QImage:
    w, h = max(1, int(w)), max(1, int(h))
    if img.width() == w and img.height() == h:
        return img
    return img.scaled(w, h, Qt.IgnoreAspectRatio, _ALGO.get(algo, Qt.FastTransformation))


# 初始缩放档的封顶：眼睛看得清，又不至于把宿主屏撑爆。
_MAX_INITIAL_ZOOM = 3.0


def initial_scale(fw: int, fh: int, avail_w: int, avail_h: int) -> float:
    """首帧到达前该把窗口开多大（`32-first-frame-sizing.md` §0）。

    一条数学巧合使得这里没必要新增显示模式：``fit`` 的缩放就是 ``min(W/w, H/h)``，
    所以只要画布盒子**恰好**是 ``帧 × z``，``fit`` 渲染出来的倍率就正好等于 ``z`` ——
    整数倍、无裁切、无意义黑边都不必额外实现，用户之后拖窗口仍然自动跟随。

    ``avail_*`` 是**画布控件**的可用区（工具条 + 状态栏 + 边框要先扣掉），不是窗口外框。
    返回的 z 直接写进 ``display.initial_zoom``（它从"机型默认"退化为"用户覆盖项"）。
    """
    fw, fh = int(fw), int(fh)
    avail_w, avail_h = int(avail_w), int(avail_h)
    if fw <= 0 or fh <= 0 or avail_w <= 0 or avail_h <= 0:
        return 1.0
    z_raw = min(avail_w / fw, avail_h / fh)
    if z_raw < 1.0:                       # 宿主屏连 1× 都放不下 → 退回纯 fit
        return z_raw
    z = min(int(z_raw), _MAX_INITIAL_ZOOM)
    if z < 2 and z_raw >= 1.5:            # 唯一非整数例外：别白留半屏
        z = 1.5
    return max(float(z), 1.0)
