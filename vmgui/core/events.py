"""极简事件总线：后端线程 emit，UI 线程 on()。

QMP 后端在后台线程里回调，消费方（Qt 窗口）通过这里的 handler 再转成 Qt 信号，
从而把线程边界收敛到一处。
"""
from __future__ import annotations

import threading
from typing import Callable, Any

Handler = Callable[..., Any]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._lock = threading.Lock()

    def on(self, name: str, handler: Handler) -> None:
        with self._lock:
            self._handlers.setdefault(name, []).append(handler)

    def off(self, name: str, handler: Handler) -> None:
        with self._lock:
            if handler in self._handlers.get(name, []):
                self._handlers[name].remove(handler)

    def emit(self, name: str, *args, **kwargs) -> None:
        with self._lock:
            handlers = list(self._handlers.get(name, []))
        for h in handlers:
            try:
                h(*args, **kwargs)
            except Exception:                      # 单个 handler 出错不影响其它
                import logging
                logging.getLogger("vmgui.events").exception("事件 %s 的处理器异常", name)


# 事件名（约定）
FRAME = "frame"                # (Frame)
RESOLUTION = "resolution"      # (w, h)
STATE = "state"                # ("connected" | "connecting" | "disconnected", detail)
ERROR = "error"                # (message)
SHOT = "shot"                  # (dest, ok, error)  —— 存图（PNG 走 QMP format=png）