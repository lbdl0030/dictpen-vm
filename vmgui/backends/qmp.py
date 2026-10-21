"""QMP 显示 / 输入后端（参考实现）。

QEMU 的 QMP 通道同时提供两件事：

* ``screendump`` —— 把当前 scanout 落到 PPM / PNG 文件。文件尺寸即 guest 当前
  console 分辨率，所以**分辨率靠取帧本身获得**，不需要任何假设。
* ``input-send-event`` —— 发送绝对坐标 / 相对增量 / 按键 / 按钮。笔屏 UI 要的是
  ABS_X/ABS_Y + BTN_TOUCH（不是 BTN_LEFT），这两者 virtio-tablet 都宣告了，可以
  手工按需组合。

一个后台线程包办取帧与输入发送：取帧间隔 ``frame_poll_ms``，输入走一个短队列
（连续移动会合并），空闲时 5ms 醒来一次，因此输入延迟约 5ms（取帧时会短暂让出）。
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from collections import deque
from typing import Any, Optional

from ..core import events as ev
from ..core import logging as vlog
from .base import (BUTTON_LEFT, BUTTON_MIDDLE, BUTTON_RIGHT, Capabilities, Frame,
                   content_rect)

log = vlog.get("backend.qmp")

ABS_MAX = 32767           # QEMU 绝对轴满量程
FAIL_THRESHOLD = 3        # 连续失败多少次判定为断线


class QmpError(Exception):
    pass


def parse_uri(uri: str) -> tuple[str, Any]:
    """'tcp:host:port' / 'host:port' / 'unix:/path' -> (kind, target)."""
    if uri.startswith("unix:"):
        return "unix", uri[len("unix:"):]
    body = uri[len("tcp:"):] if uri.startswith("tcp:") else uri
    if ":" not in body:
        raise QmpError(f"无法解析 QMP 地址: {uri!r}（形如 tcp:127.0.0.1:4446）")
    host, _, port = body.rpartition(":")
    try:
        return "tcp", (host or "127.0.0.1", int(port))
    except ValueError as e:
        raise QmpError(f"端口无效: {uri!r}") from e


class QmpConnection:
    """一条 QMP 连接的握手 / 命令 / 关闭。非线程安全，由持有线程独占。"""

    def __init__(self, uri: str, timeout_ms: int = 3000) -> None:
        self.uri = uri
        self.timeout = max(0.1, timeout_ms / 1000.0)
        self._sock: Optional[socket.socket] = None
        self._file = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        """建连 + 握手。任何失败都归一成 QmpError，交给上层退避重连。

        注意握手要"等到问候语为止"而不是超时就弃连：QEMU 的 socket chardev 只服务
        一个客户端，被半开即弃的连接会一直占着它，之后所有连接都被拒（实测：
        vmgui 比 QEMU 早就绪时会把 4446 卡死到重启 QEMU 为止）。
        """
        self.close()
        kind, target = parse_uri(self.uri)
        try:
            if kind == "unix":
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(self.timeout)
                s.connect(target)
            else:
                s = socket.create_connection(target, timeout=self.timeout)
            s.settimeout(self.timeout)
            self._sock = s
            self._file = s.makefile("rwb")
            self._read_greeting()
            self.execute("qmp_capabilities")
        except QmpError:
            self.close()
            raise
        except OSError as e:                          # 含 TimeoutError
            self.close()
            raise QmpError(f"连接 {self.uri} 失败: {e}") from e

    def _read_greeting(self) -> None:
        """读 QMP 问候语；QEMU 还没就绪时在同一个连接上继续等，不弃连。"""
        deadline = time.monotonic() + max(10.0, self.timeout)
        while True:
            try:
                line = self._file.readline()
            except OSError:                           # 读超时：连接还在，接着等
                if time.monotonic() >= deadline:
                    raise QmpError(f"等待 {self.uri} 的问候语超时")
                continue
            if line:
                return
            raise QmpError("QMP 连接被关闭")

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        if not self._file:
            raise QmpError("QMP 未连接")
        cmd: dict[str, Any] = {"execute": command}
        if arguments:
            cmd["arguments"] = arguments
        try:
            self._file.write((_json(cmd) + "\n").encode())
            self._file.flush()
            while True:
                line = self._file.readline()
                if not line:
                    raise QmpError("QMP 连接被关闭")
                obj = _loads(line)
                if "error" in obj:
                    err = obj["error"]
                    raise QmpError(f"{command}: {err.get('desc', err)}")
                if "return" in obj:
                    return obj["return"]
                # 否则是异步事件，忽略（本后端只关心命令应答）
        except (OSError, ValueError) as e:
            self.close()
            raise QmpError(f"{command} 发送失败: {e}") from e

    def close(self) -> None:
        for obj in (self._file, self._sock):
            try:
                obj and obj.close()
            except OSError:
                pass
        self._file = None
        self._sock = None


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj)


def _loads(b: bytes) -> dict:
    import json
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except ValueError as e:
        raise QmpError(f"QMP 应答不是 JSON: {b[:80]!r}") from e


def parse_ppm(raw: bytes) -> tuple[int, int, int, bytes]:
    """解析二值 PPM(P6)，返回 (w, h, stride, rgb)。"""
    if not raw.startswith(b"P6"):
        raise QmpError("不是 P6 PPM 数据")
    i, fields = 2, []
    while len(fields) < 3:
        while i < len(raw) and raw[i:i + 1].isspace():
            i += 1
        if raw[i:i + 1] == b"#":
            while i < len(raw) and raw[i:i + 1] != b"\n":
                i += 1
            continue
        j = i
        while j < len(raw) and not raw[j:j + 1].isspace():
            j += 1
        fields.append(int(raw[i:j]))
        i = j
    i += 1                                    # 跳掉 maxval 后那一个空白
    w, h, maxval = fields
    if maxval != 255:
        raise QmpError(f"暂不支持的 PPM maxval={maxval}")
    need = w * h * 3
    data = raw[i:i + need]
    if len(data) < need:
        raise QmpError(f"PPM 数据不完整: 期望 {need} 字节，实际 {len(data)}")
    return w, h, w * 3, data


class QmpBackend:
    """FrameSource + InputSink 的 QMP 实现。"""

    def __init__(self, cfg, bus: ev.EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self._uri = cfg.get("connection.uri", "tcp:127.0.0.1:4446")
        self._timeout_ms = int(cfg.get("connection.timeout_ms", 3000))
        self._poll_s = max(0.005, int(cfg.get("display.frame_poll_ms", 30)) / 1000.0)
        self._touch_button = int(cfg.get("input.touch_button", 330))
        self._click_with_touch = bool(cfg.get("input.send_click_with_touch", True))
        self._backoff_ms = int(cfg.get("connection.reconnect.backoff_ms", 500))
        self._backoff_max_ms = int(cfg.get("connection.reconnect.max_backoff_ms", 5000))
        self._reconnect = bool(cfg.get("connection.reconnect.enabled", True))

        self._conn = QmpConnection(self._uri, self._timeout_ms)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False                             # `39` §2 #7：暂停取帧
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, tuple]] = deque()
        self._shots: deque[tuple[str, str]] = deque()   # 存图请求（dest, fmt）
        self._force = False                             # 手动刷新：立刻抓一帧
        self._last_frame: Optional[Frame] = None
        self._size: Optional[tuple[int, int]] = None
        self._buttons = 0
        self._state = "disconnected"
        self._fails = 0
        self._dump = os.path.join(tempfile.gettempdir(), "vmgui-frame.ppm")
        # 某些固件（RV1106 falcon）输出 R/B 与扫描线相反；由会话按包置位。
        self.swap_rb = False

    # ---- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="qmp", daemon=True)
        self._thread.start()
        log.debug("QMP 后端线程已启动 uri=%s poll=%.0fms", self._uri, self._poll_s * 1000)

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._conn.close()
        with self._lock:
            self._queue.clear()
            self._shots.clear()
        self._set_state("disconnected", "已停止")

    # ---- FrameSource --------------------------------------------------- #
    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._last_frame

    def resolution(self) -> Optional[tuple[int, int]]:
        with self._lock:
            return self._size

    def capabilities(self) -> Capabilities:
        return Capabilities(absolute_pointer=True, send_keys=True,
                            resolution_events=False)

    # ---- InputSink ----------------------------------------------------- #
    def pointer(self, x: int, y: int, buttons: int, absolute: bool) -> None:
        """absolute=True 时 x,y 为 guest 像素；否则为相对增量。"""
        with self._lock:
            self._queue.append(("pointer", (int(x), int(y), int(buttons), bool(absolute))))
        self._wake.set()

    def key(self, code: int, down: bool, mods: int = 0) -> None:
        with self._lock:
            self._queue.append(("key", (int(code), bool(down), int(mods))))
        self._wake.set()

    def screenshot(self, dest: str, fmt: str = "png") -> None:
        """把当前 scanout 落成文件。PNG 走 **QMP 侧**编码（实测 160 699 B，PPM 是它的
        8.6 倍），而 PPM 是裸 RGB —— 帧循环用 PPM、存图用 PNG（决策 #17）。

        必须排进后端线程的队列：QMP 连接归那个线程独有（单客户端 chardev）。
        结果通过 ``ev.SHOT`` 广播。
        """
        with self._lock:
            self._shots.append((os.path.abspath(dest), fmt))
        self._wake.set()

    def force_frame(self) -> None:
        """手动刷新钮：**绕开** frame_poll_ms 的节奏，立刻抓一帧（决策 #29 / Q19）。

        不能只是"把 poll 调快" —— 那会把 200 ms 的默认节奏一起改掉。
        """
        self._force = True
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        """暂停 / 继续按节奏取帧（`39` §2 #7）。

        **暂停时仍然认 `force_frame`** —— 存图与"继续时补一帧"都靠它；恢复时补一帧，
        免得人对着旧画面等下一个 200 ms 节奏点。
        """
        self._paused = bool(paused)
        if not self._paused:
            self._force = True
        self._wake.set()

    # ---- 线程主体 ------------------------------------------------------ #
    def _run(self) -> None:
        backoff = self._backoff_ms / 1000.0
        next_frame = 0.0
        while self._running:
            try:
                if not self._conn.connected:
                    self._set_state("connecting", self._uri)
                    self._conn.connect()
                    self._set_state("connected", self._uri)
                    self._fails = 0
                    backoff = self._backoff_ms / 1000.0
                    self._size = None
                self._drain_input()
                self._drain_shots()
                now = time.monotonic()
                force = self._force                 # 手动刷新：不等下一个节奏点
                self._force = False
                # `39` §2 #7：暂停时不按节奏取帧，但**仍然认 force**（继续/存图要用）。
                if force or (now >= next_frame and not self._paused):
                    self._grab_frame()
                    next_frame = time.monotonic() + self._poll_s
            except (QmpError, OSError) as e:
                self._fails += 1
                # 过阈值之后分母不再有意义（旧写法会一路打 `4/3 … 11/3`，千问 Q48 顺带报的）：
                # 头两次是"失败 n/3"，之后是"重连第 n 次"。
                if self._fails <= FAIL_THRESHOLD:
                    log.warning("QMP 失败(%d/%d): %s", self._fails, FAIL_THRESHOLD, e)
                else:
                    log.warning("QMP 重连第 %d 次: %s", self._fails - FAIL_THRESHOLD, e)
                # `ev.ERROR` **只在首次**失败时发：否则重连期每次失败都发一条一模一样的字，
                # UI 一旦把它写进提示行就是刷屏（同 Q48 顺带报的那半条）。
                if self._fails == 1:
                    self.bus.emit(ev.ERROR, str(e))
                self._conn.close()
                if self._fails >= FAIL_THRESHOLD:
                    self._set_state("disconnected", str(e))
                    if not self._reconnect:
                        self._running = False
                        break
                    self._sleep(backoff)
                    backoff = min(self._backoff_max_ms / 1000.0, max(backoff, 0.2) * 2)
                else:
                    self._sleep(0.05)
                continue
            self._sleep(0.005)
        self._conn.close()

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _set_state(self, state: str, detail: str = "") -> None:
        if state != self._state or detail:
            self._state = state
            self.bus.emit(ev.STATE, state, detail)

    def _grab_frame(self) -> None:
        try:
            if os.path.exists(self._dump):
                os.remove(self._dump)
        except OSError:
            pass
        self._conn.execute("screendump", {"filename": self._dump, "format": "ppm"})
        with open(self._dump, "rb") as f:
            raw = f.read()
        w, h, stride, rgb = parse_ppm(raw)
        frame = Frame(pixels=rgb, width=w, height=h, stride=stride,
                      fmt="bgr888" if self.swap_rb else "rgb888")
        with self._lock:
            self._last_frame = frame
            new_size = (w, h) != self._size
            self._size = (w, h)
        if new_size:
            log.info("guest 分辨率 %dx%d", w, h)
            self.bus.emit(ev.RESOLUTION, w, h)
        self.bus.emit(ev.FRAME, frame)

    # ---- 存图 ---------------------------------------------------------- #
    def _drain_shots(self) -> None:
        """处理排队的存图请求。单个失败只广播错误，不拖垮连接。"""
        with self._lock:
            if not self._shots:
                return
            pending = list(self._shots)
            self._shots.clear()
        for dest, fmt in pending:
            try:
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                except OSError:
                    pass
                if self.swap_rb and fmt == "png":
                    # QMP 的 PNG 是解码后的宿主图像，不能再靠 Frame.fmt 纠正；
                    # 先落 PPM，再用 QImage 交换 R/B 并存 PNG。
                    tmp = dest + ".ppm"
                    try:
                        self._conn.execute("screendump", {"filename": tmp, "format": "ppm"})
                        from PySide6.QtGui import QImage
                        img = QImage(tmp)
                        if img.isNull():
                            raise RuntimeError("PPM 解码失败")
                        img.rgbSwapped().save(dest, "PNG")
                    finally:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                else:
                    self._conn.execute("screendump", {"filename": dest, "format": fmt})
            except (QmpError, OSError, RuntimeError) as e:
                log.warning("存图失败 %s: %s", dest, e)
                self.bus.emit(ev.SHOT, dest, False, str(e))
            else:
                self.bus.emit(ev.SHOT, dest, True, "")

    # ---- 输入 ---------------------------------------------------------- #
    def _drain_input(self) -> None:
        with self._lock:
            if not self._queue:
                return
            pending = list(self._queue)
            self._queue.clear()
        events: list[dict] = []
        size = self._size
        for kind, args in pending:
            if kind == "key":
                code, down, _mods = args
                events.append({"type": "key",
                               "data": {"down": down,
                                        "key": {"type": "number", "data": code}}})
            else:
                x, y, buttons, absolute = args
                events.extend(self._pointer_events(x, y, buttons, absolute, size))
        if events:
            self._conn.execute("input-send-event", {"events": events})

    def _abs_rect(self, w: int, h: int) -> tuple[int, int, int, int]:
        """绝对轴该用的内容矩形 ``(cw, ch, x0, y0)`` —— 与视图层**同一个函数**。

        面板声明是**裁剪线**（决策 #27）：falcon 的 DRM mode 可能比面板宽，照帧算会把
        面板上看不见的边也画出来 —— 而且手势会偏：画面裁了、坐标映射没裁。
        共用 `base.content_rect` 而不是各写一遍，是因为两处公式一旦不同（一边
        `max(1,…)`、一边 `cw<=0` 回落整帧），表现就是"点了没反应 / 右半屏失灵"（决策 #35）。
        """
        return content_rect(self.cfg.get("display.crop"), w, h)

    def _pointer_events(self, x: int, y: int, buttons: int, absolute: bool,
                        size: Optional[tuple[int, int]]) -> list[dict]:
        out: list[dict] = []
        if absolute:
            w, h = size or (1, 1)
            cw, ch, cx, cy = self._abs_rect(w, h)
            ax = int(round((x - cx) * ABS_MAX / cw))
            ay = int(round((y - cy) * ABS_MAX / ch))
            out.append({"type": "abs", "data": {"axis": "x", "value": ax}})
            out.append({"type": "abs", "data": {"axis": "y", "value": ay}})
        else:
            if x:
                out.append({"type": "rel", "data": {"axis": "x", "value": int(x)}})
            if y:
                out.append({"type": "rel", "data": {"axis": "y", "value": int(y)}})

        changed = buttons ^ self._buttons
        if changed & BUTTON_LEFT:
            down = bool(buttons & BUTTON_LEFT)
            if self._click_with_touch:
                out.append(_key_event(self._touch_button, down))
            out.append({"type": "btn", "data": {"down": down, "button": "left"}})
        if changed & BUTTON_RIGHT:
            out.append({"type": "btn",
                        "data": {"down": bool(buttons & BUTTON_RIGHT), "button": "right"}})
        if changed & BUTTON_MIDDLE:
            out.append({"type": "btn",
                        "data": {"down": bool(buttons & BUTTON_MIDDLE), "button": "middle"}})
        self._buttons = buttons
        return out


def _key_event(code: int, down: bool) -> dict:
    return {"type": "key", "data": {"down": down, "key": {"type": "number", "data": code}}}