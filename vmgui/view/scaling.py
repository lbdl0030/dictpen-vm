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