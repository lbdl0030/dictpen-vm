"""画布：把 guest 帧按显示模式画出来，并把鼠标位置交给上层做坐标变换。

分辨率变化时只改内部状态与 sizeHint（不重建窗口/控件），因此不会闪、也不会丢焦点。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..backends.base import (BUTTON_LEFT, BUTTON_MIDDLE, BUTTON_RIGHT, Frame)
from ..input import mapper
from . import scaling


class Canvas(QWidget):
    mouse_moved = Signal(QPointF, int)
    mouse_pressed = Signal(QPointF, int)
    mouse_released = Signal(QPointF, int)

    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._frame: Optional[Frame] = None
        self._image: Optional[QImage] = None
        self._display: Optional[QImage] = None
        self._display_key = None
        self._guest_size = (0, 0)
        self._transform: Optional[mapper.ViewTransform] = None
        self._marker: Optional[tuple[int, int]] = None
        self._held = False

    # ---- 对外 ---------------------------------------------------------- #
    @property
    def guest_size(self) -> tuple[int, int]:
        return self._guest_size

    @property
    def has_frame(self) -> bool:
        return self._image is not None

    def transform(self) -> Optional[mapper.ViewTransform]:
        return self._transform

    def current_image(self) -> Optional[QImage]:
        return self._image

    def set_config(self, cfg) -> None:
        self.cfg = cfg
        self._invalidate()
        self.update()

    def set_frame(self, frame: Frame) -> None:
        """分辨率/内容变化时更新；内容完全一致则跳过（省一次转换与重绘）。"""
        old = self._frame
        if (old is not None and old.width == frame.width and old.height == frame.height
                and old.pixels == frame.pixels):
            return
        self._frame = frame
        self._image = scaling.to_qimage(frame)
        self._guest_size = (frame.width, frame.height)
        self._invalidate()
        self.update()

    def clear(self) -> None:
        self._frame = None
        self._image = None
        self._invalidate()
        self.update()

    def set_marker(self, pos: Optional[tuple[int, int]]) -> None:
        if pos != self._marker:
            self._marker = pos
            self.update()

    def sizeHint(self) -> QSize:                      # noqa: N802 (Qt 命名)
        w, h = self._guest_size
        if not w or not h:
            return QSize(720, 400)
        z = float(self.cfg.get("display.initial_zoom", 2.0))
        return QSize(int(w * z), int(h * z))

    # ---- 绘制 ---------------------------------------------------------- #
    def paintEvent(self, event) -> None:             # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.cfg.get("display.background", "#101010")))
        if self._image is None:
            p.setPen(QPen(QColor("#808080")))
            p.drawText(self.rect(), Qt.AlignCenter, "等待虚拟机画面…")
            p.end()
            return

        gw, gh = self._guest_size
        t = mapper.build_transform(
            self.width(), self.height(), gw, gh,
            mode=self.cfg.get("display.mode", "fit"),
            rotation=int(self.cfg.get("display.rotation", 0)),
            crop=self.cfg.get("display.crop"),
            zoom=float(self.cfg.get("display.zoom", 1.0)),
            aspect=self.cfg.get("display.aspect", "keep"),
            dpi_scale=self._dpi_scale(),
        )
        self._transform = t
        disp_w = max(1, int(round(t.disp_w)))
        disp_h = max(1, int(round(t.disp_h)))
        algo = self.cfg.get("display.scale_algo", "nearest")
        key = (self._image.cacheKey(), t.rotation, disp_w, disp_h, algo,
               t.crop_x, t.crop_y, t.content_w, t.content_h)
        if self._display_key != key:
            src = self._image
            if (t.crop_x, t.crop_y, t.content_w, t.content_h) != (0, 0, gw, gh):
                src = src.copy(t.crop_x, t.crop_y, t.content_w, t.content_h)
            rot = scaling.rotate_image(src, t.rotation)
            self._display = scaling.scale_image(rot, disp_w, disp_h, algo)
            self._display_key = key
        if self._display is not None:
            p.drawImage(QPointF(t.ox, t.oy), self._display)

        if self.cfg.get("display.pointer_marker", True) and self._marker is not None:
            self._draw_marker(p, t)
        p.end()

    def _draw_marker(self, p: QPainter, t: mapper.ViewTransform) -> None:
        spot = mapper.guest_to_screen(self._marker[0], self._marker[1], t)
        if spot is None:
            return
        r = max(4.0, 5.0 * max(t.sx, t.sy))
        cx, cy = spot
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor("#ff3b30" if self._held else "#00b0ff"), 2))
        p.drawEllipse(QPointF(cx, cy), max(1.0, r - 2), max(1.0, r - 2))
        if self._held:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ff3b30"))
            p.drawEllipse(QPointF(cx, cy), 2.0, 2.0)
            p.setBrush(Qt.NoBrush)

    def _dpi_scale(self) -> float:
        hint = int(self.cfg.get("display.dpi_hint", 0))
        return (hint / 96.0) if hint > 0 else 1.0

    def _invalidate(self) -> None:
        self._display = None
        self._display_key = None

    # ---- 鼠标 ---------------------------------------------------------- #
    @staticmethod
    def _buttons(event) -> int:
        b = 0
        btns = event.buttons()
        if btns & Qt.LeftButton:
            b |= BUTTON_LEFT
        if btns & Qt.MiddleButton:
            b |= BUTTON_MIDDLE
        if btns & Qt.RightButton:
            b |= BUTTON_RIGHT
        return b

    def mouseMoveEvent(self, event) -> None:          # noqa: N802
        self.mouse_moved.emit(QPointF(event.position()), self._buttons(event))

    def mousePressEvent(self, event) -> None:         # noqa: N802
        self._held = True
        self.mouse_pressed.emit(QPointF(event.position()), self._buttons(event))

    def mouseReleaseEvent(self, event) -> None:       # noqa: N802
        self._held = False
        self.mouse_released.emit(QPointF(event.position()), self._buttons(event))

    def leaveEvent(self, event) -> None:              # noqa: N802
        self.set_marker(None)