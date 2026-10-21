"""会话：连接生命周期、后端构建、状态上报。

真正的重连退避在后端线程里（它才知道连接何时断），会话负责把
"connecting / connected / disconnected / error" 归一化后交给 UI。

后端可由配置切换（qmp / vnc），会话不关心具体协议；配置变更时直接换一个新后端，
避免在旧对象上打补丁。
"""
from __future__ import annotations

import time
from typing import Optional

from ..backends.base import Backend, Capabilities, Frame
from ..core import events as ev
from ..core import logging as vlog

log = vlog.get("session")


def create_backend(cfg, bus: ev.EventBus):
    kind = cfg.get("connection.kind", "qmp")
    if kind == "qmp":
        from ..backends.qmp import QmpBackend
        return QmpBackend(cfg, bus)
    if kind == "vnc":
        try:
            from ..backends.vnc import VncBackend           # 可选后端（尚未实现）
        except ImportError as e:
            raise ValueError("connection.kind='vnc' 尚未实现；"
                             "目前只有 qmp 后端，见设计文档可扩展项") from e
        return VncBackend(cfg, bus)
    raise ValueError(f"未知的 connection.kind: {kind!r}")


class Session:
    def __init__(self, cfg, bus: Optional[ev.EventBus] = None) -> None:
        self.cfg = cfg
        self.bus = bus or ev.EventBus()
        self.backend: Optional[Backend] = None
        self.swap_rb = False
        self.state = "disconnected"
        self.detail = ""
        self.last_error = ""
        self.last_state_change = time.time()
        self.connected_since: Optional[float] = None
        self.bus.on(ev.STATE, self._on_state)
        self.bus.on(ev.ERROR, self._on_error)

    # ---- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        if self.backend is not None:
            return
        self.backend = create_backend(self.cfg, self.bus)
        if hasattr(self.backend, "swap_rb"):
            self.backend.swap_rb = self.swap_rb
        self.backend.start()
        log.info("会话已启动: %s (%s)", self.cfg.get("connection.kind"),
                 self.cfg.get("connection.uri"))

    def stop(self) -> None:
        if self.backend is not None:
            self.backend.stop()
            self.backend = None

    def reconfigure(self, cfg) -> None:
        """配置变更后热更新：换后端对象，只重建必要部件。"""
        log.info("应用新配置（重建后端）")
        self.stop()
        self.cfg = cfg
        self.start()

    # ---- 对外 ---------------------------------------------------------- #
    @property
    def source(self):
        return self.backend.source if self.backend else None

    @property
    def sink(self):
        return self.backend.sink if self.backend else None

    def capabilities(self) -> Capabilities:
        b = self.backend
        if b is None:
            return Capabilities()
        return b.capabilities()

    def latest(self) -> Optional[Frame]:
        b = self.backend
        return b.latest() if b else None

    def resolution(self) -> Optional[tuple[int, int]]:
        b = self.backend
        return b.resolution() if b else None

    def pointer(self, x: int, y: int, buttons: int, absolute: bool) -> None:
        b = self.backend
        if b:
            b.pointer(x, y, buttons, absolute)

    def key(self, code: int, down: bool, mods: int = 0) -> None:
        b = self.backend
        if b:
            b.key(code, down, mods)

    def screenshot(self, dest: str, fmt: str = "png") -> bool:
        """请后端把当前画面落成文件（PNG 走 QMP 侧编码）。返回是否受理。

        不在协议里（``FrameSource`` 只管帧），所以用 ``getattr`` 探测；结果走 ``ev.SHOT``。
        """
        fn = getattr(self.backend, "screenshot", None)
        if fn is None:
            return False
        fn(dest, fmt)
        return True

    def force_frame(self) -> None:
        """手动刷新钮：绕开 ``frame_poll_ms`` 的节奏，立刻抓一帧（决策 #29 / Q19）。"""
        fn = getattr(self.backend, "force_frame", None)
        if fn is not None:
            fn()

    def set_paused(self, paused: bool) -> None:
        """暂停/继续取帧（`39` §2 #7：原「手动刷新」改成一颗开关）。

        不在协议里（``FrameSource`` 只管帧），所以用 ``getattr`` 探测；后端不支持就静默不动。
        """
        fn = getattr(self.backend, "set_paused", None)
        if fn is not None:
            fn(paused)

    # ---- 内部 ---------------------------------------------------------- #
    def _on_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self.last_state_change = time.time()
        if state == "connected":
            self.connected_since = time.time()
        elif state == "disconnected":
            self.connected_since = None

    def _on_error(self, message: str) -> None:
        self.last_error = message