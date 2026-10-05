"""鼠标捕获 / 释放与焦点联动。

* 绝对模式（tablet / 触摸屏）：不需要真正抓鼠标（用绝对坐标定位），只需隐藏系统
  光标，让画布内的触摸反馈环成为唯一指针；
* 相对模式（PS/2 之类）：必须抓鼠标并把光标钉在窗口中心，用增量喂给 guest。

故意不做“把光标锁死在窗口内”的强捕获，避免用户被 GUI 困住。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtGui import QCursor
from PySide6.QtCore import Qt


class PointerGrab(QObject):
    changed = Signal(bool)

    def __init__(self, widget, parent=None) -> None:
        super().__init__(parent)
        self.widget = widget
        self.mode = "absolute"
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        if self._active:
            self.release()
        self.mode = mode

    def acquire(self) -> bool:
        if self._active:
            return True
        if self.mode == "relative":
            self.widget.grabMouse()
        self.widget.setMouseTracking(True)
        self.widget.setCursor(Qt.BlankCursor)
        if self.mode == "relative":
            self.recenter()
        self._active = True
        self.changed.emit(True)
        return True

    def release(self) -> None:
        if not self._active:
            return
        try:
            self.widget.releaseMouse()
        except RuntimeError:                     # 窗口已销毁
            pass
        self.widget.unsetCursor()
        self._active = False
        self.changed.emit(False)

    def toggle(self) -> None:
        self.release() if self._active else self.acquire()

    def recenter(self) -> None:
        """相对模式：把光标钉在窗口中心，后续增量都相对这一点。"""
        w, h = self.widget.width(), self.widget.height()
        QCursor.setPos(self.widget.mapToGlobal(QPoint(w // 2, h // 2)))

    def hint(self, toggle_key: str) -> str:
        if self._active:
            return f"已捕获鼠标（{toggle_key} 释放）"
        return f"鼠标未捕获（{toggle_key} 捕获）"