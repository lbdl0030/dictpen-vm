"""主窗口：画布 + 状态栏 + 菜单/快捷键 + 设置面板，把后台事件桥到 UI 线程。"""
from __future__ import annotations

import datetime
import os
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QSettings, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QStatusBar

from ..backends.base import PointerMode
from ..core import events as ev
from ..core import logging as vlog
from ..input import mapper
from ..input.grab import PointerGrab
from ..input.keymap import KeyMapper
from .canvas import Canvas
from .settings_dialog import SettingsDialog

log = vlog.get("window")

# 这些键是在后端构造时烘进去的，改了才需要重建后端；其余（显示模式/算法/标记/
# 快捷键…）只刷新画布与菜单，避免无谓地断开重连 QMP。
BACKEND_KEYS = {
    "connection.kind", "connection.uri", "connection.timeout_ms",
    "connection.reconnect.enabled", "connection.reconnect.backoff_ms",
    "connection.reconnect.max_backoff_ms",
    "display.frame_poll_ms",
    "input.touch_button", "input.send_click_with_touch",
}


class SignalBridge(QObject):
    """后台线程 -> UI 线程的转接。bus 在 QMP 线程 emit，Qt 自动排队到此对象所在线程。"""
    frame = Signal(object)
    resolution = Signal(int, int)
    state = Signal(str, str)

    def push_frame(self, frame) -> None:
        self.frame.emit(frame)

    def push_resolution(self, w: int, h: int) -> None:
        self.resolution.emit(w, h)

    def push_state(self, state: str, detail: str) -> None:
        self.state.emit(state, detail)


class MainWindow(QMainWindow):
    def __init__(self, cfg, session) -> None:
        super().__init__()
        self.cfg = cfg
        self.session = session
        self.keymap = KeyMapper(cfg.get("input.keymap"))
        self._pointer_mode = "absolute"
        self._last_pos: Optional[QPointF] = None
        self._rel_buttons = 0
        self._mode_before_fs: Optional[str] = None
        self._frames = 0
        self._fps = 0.0
        self._guest_size = (0, 0)
        self._geometry_restored = False
        self._settings = QSettings("vmgui", "vmgui")

        self.canvas = Canvas(cfg)
        self.setCentralWidget(self.canvas)
        self.setWindowTitle("vmgui — QEMU 虚拟机客户端")
        self.grab = PointerGrab(self.canvas)

        self._build_status()
        self._build_actions()

        self.bridge = SignalBridge()
        session.bus.on(ev.FRAME, self.bridge.push_frame)
        session.bus.on(ev.RESOLUTION, self.bridge.push_resolution)
        session.bus.on(ev.STATE, self.bridge.push_state)
        self.bridge.frame.connect(self._on_frame)
        self.bridge.resolution.connect(self._on_resolution)
        self.bridge.state.connect(self._on_state)
        session.bus.on(ev.ERROR, self._on_error)

        self.canvas.mouse_moved.connect(self._on_mouse_move)
        self.canvas.mouse_pressed.connect(self._on_mouse_press)
        self.canvas.mouse_released.connect(self._on_mouse_release)
        self.grab.changed.connect(self._on_grab_changed)

        geo = self._settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
            self._geometry_restored = True

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_status(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.lbl_state = QLabel("未连接")
        self.lbl_res = QLabel("--x--")
        self.lbl_mode = QLabel("")
        self.lbl_fps = QLabel("")
        self.lbl_hint = QLabel("")
        for w in (self.lbl_state, self.lbl_res, self.lbl_mode, self.lbl_fps):
            bar.addWidget(w)
        bar.addPermanentWidget(self.lbl_hint)

    def _build_actions(self) -> None:
        mb = self.menuBar()

        # 显示模式
        m = mb.addMenu("显示(&D)")
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_actions = {}
        for mode in ["1:1", "fit", "stretch", "free", "fullscreen"]:
            a = QAction(mode, self, checkable=True)
            a.triggered.connect(lambda _=False, m=mode: self._set_mode(m))
            self.mode_group.addAction(a)
            m.addAction(a)
            self.mode_actions[mode] = a
        m.addSeparator()
        self.algo_group = QActionGroup(self)
        self.algo_group.setExclusive(True)
        self.algo_actions = {}
        for algo in ["nearest", "bilinear", "lanczos"]:
            a = QAction(algo, self, checkable=True)
            a.triggered.connect(lambda _=False, a=algo: self._set_config("display.scale_algo", a))
            self.algo_group.addAction(a)
            m.addAction(a)
            self.algo_actions[algo] = a
        m.addSeparator()
        self.act_marker = QAction("画布内指针标记", self, checkable=True)
        self.act_marker.toggled.connect(
            lambda v: self._set_config("display.pointer_marker", bool(v)))
        m.addAction(self.act_marker)

        # 视图
        m = mb.addMenu("视图(&V)")
        self.act_fullscreen = self._mk_action("全屏", "input.hotkeys.fullscreen",
                                              self._toggle_fullscreen, m)
        self.act_reset = self._mk_action("重置视图", "input.hotkeys.reset",
                                         self._reset_view, m)
        self.act_shot = self._mk_action("截图", "input.hotkeys.screenshot",
                                        self._screenshot, m)

        # 输入
        m = mb.addMenu("输入(&I)")
        self.act_grab = QAction("捕获鼠标", self, checkable=True)
        self.act_grab.toggled.connect(self._on_grab_toggled)
        m.addAction(self.act_grab)
        self._mk_action("切换捕获", "input.grab.toggle_key", self._toggle_grab, m)
        m.addSeparator()
        self.pm_group = QActionGroup(self)
        self.pm_group.setExclusive(True)
        self.pm_actions = {}
        for pm in ["auto", "absolute", "relative"]:
            a = QAction(pm, self, checkable=True)
            a.triggered.connect(lambda _=False, pm=pm: self._set_config("input.pointer_mode", pm))
            self.pm_group.addAction(a)
            m.addAction(a)
            self.pm_actions[pm] = a

        m = mb.addMenu("更多(&M)")
        self._mk_action("设置…", "input.hotkeys.settings", self._open_settings, m)
        self._mk_action("退出", "input.hotkeys.quit", self.close, m)
        self._sync_actions()

    def _mk_action(self, text: str, hotkey_path: str, slot, menu) -> QAction:
        """建一个带快捷键的动作并挂到菜单上（快捷键解析失败只告警不致命）。"""
        a = QAction(text, self)
        spec = self.cfg.get(hotkey_path, "")
        if spec:
            seq = QKeySequence(spec)
            if seq.isEmpty():
                log.warning("快捷键 %r 无法解析（%s）", spec, hotkey_path)
            else:
                a.setShortcut(seq)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    # ------------------------------------------------------------------ #
    # 配置 / 状态同步
    # ------------------------------------------------------------------ #
    def _set_mode(self, mode: str) -> None:
        self._set_config("display.mode", mode)

    def _set_config(self, path: str, value) -> None:
        self.cfg.set(path, value)
        self._apply_config([path])

    def _apply_config(self, changed: Optional[list[str]] = None) -> None:
        """配置热更新：只刷需要刷的部件，不重建窗口。

        changed 为 None 表示改动范围未知（重载/恢复默认），保守地重建后端。
        """
        self.keymap = KeyMapper(self.cfg.get("input.keymap"))
        self.canvas.set_config(self.cfg)
        self.canvas.updateGeometry()
        if changed is None or any(p in BACKEND_KEYS for p in changed):
            self.session.reconfigure(self.cfg)
        self._update_pointer_mode()
        self._sync_actions()
        self._refresh_status()

    def _sync_actions(self) -> None:
        mode = self.cfg.get("display.mode", "fit")
        if mode in self.mode_actions:
            self.mode_actions[mode].setChecked(True)
        algo = self.cfg.get("display.scale_algo", "nearest")
        if algo in self.algo_actions:
            self.algo_actions[algo].setChecked(True)
        pm = self.cfg.get("input.pointer_mode", "auto")
        if pm in self.pm_actions:
            self.pm_actions[pm].setChecked(True)
        self._sync_check(self.act_marker, bool(self.cfg.get("display.pointer_marker", True)))
        self._sync_check(self.act_grab, self.grab.active)

    def _sync_check(self, action, value: bool) -> None:
        """程序化同步勾选状态：屏蔽 toggled，免得反手又写配置、重建后端。"""
        action.blockSignals(True)
        action.setChecked(value)
        action.blockSignals(False)

    def _update_pointer_mode(self) -> None:
        caps = self.session.capabilities()
        mode = PointerMode.resolve(self.cfg.get("input.pointer_mode", "auto"), caps)
        if mode != self._pointer_mode:
            log.info("指针模式: %s（配置 %s, 后端绝对坐标=%s）",
                     mode, self.cfg.get("input.pointer_mode"), caps.absolute_pointer)
        self._pointer_mode = mode
        self.grab.set_mode(mode)

    def _refresh_status(self) -> None:
        st = {"connected": "已连接", "connecting": "连接中…",
              "disconnected": "已断开"}.get(self.session.state, self.session.state)
        if self.session.last_error and self.session.state != "connected":
            st += f"（{self.session.last_error[:40]}）"
        self.lbl_state.setText(st)
        w, h = self._guest_size
        self.lbl_res.setText(f"{w}x{h}" if w else "--x--")
        self.lbl_mode.setText(
            f"{self.cfg.get('display.mode')}/{self.cfg.get('display.scale_algo')}"
            f"/{self._pointer_mode}")
        self.lbl_fps.setText(f"{self._fps:.1f} fps")
        profiles = self.cfg.profile_names()
        self.lbl_hint.setText(self.grab.hint(self.cfg.get("input.grab.toggle_key", ""))
                              + (f"  |  profile: {','.join(profiles)}" if profiles else ""))

    # ------------------------------------------------------------------ #
    # 后台事件
    # ------------------------------------------------------------------ #
    def _on_frame(self, frame) -> None:
        self._frames += 1
        self.canvas.set_frame(frame)

    def _on_resolution(self, w: int, h: int) -> None:
        prev, self._guest_size = self._guest_size, (w, h)
        self.canvas.updateGeometry()
        resize = (prev and prev != (w, h)) or not self._geometry_restored
        if resize and not self.isFullScreen() and not self.isMaximized():
            self.resize(self.canvas.sizeHint())
            self._geometry_restored = True
        log.info("分辨率变为 %dx%d（画布 %d x %d）", w, h, self.canvas.width(),
                 self.canvas.height())
        self._refresh_status()

    def _on_state(self, state: str, detail: str) -> None:
        if state == "connected":
            self._update_pointer_mode()
            if self.cfg.get("input.grab.enabled", True) and not self.grab.active:
                self.grab.acquire()
            self._sync_check(self.act_grab, self.grab.active)
        self._refresh_status()

    def _on_error(self, message: str) -> None:
        log.debug("后端错误: %s", message)

    def _tick(self) -> None:
        self._fps = self._frames * 2.0        # 500ms 一次
        self._frames = 0
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # 鼠标 / 键盘
    # ------------------------------------------------------------------ #
    def _on_mouse_move(self, pos: QPointF, buttons: int) -> None:
        if self._pointer_mode == "relative":
            self._send_relative(pos, buttons)
            return
        t = self.canvas.transform()
        if t is None:
            return
        gx, gy = mapper.screen_to_guest(pos.x(), pos.y(), t)
        self.canvas.set_marker((gx, gy))
        self.session.pointer(gx, gy, buttons, True)

    def _on_mouse_press(self, pos: QPointF, buttons: int) -> None:
        self._deliver(pos, buttons)

    def _on_mouse_release(self, pos: QPointF, buttons: int) -> None:
        self._deliver(pos, buttons)

    def _deliver(self, pos: QPointF, buttons: int) -> None:
        if self._pointer_mode == "relative":
            self._last_pos = pos
            self.session.pointer(0, 0, buttons, False)
            return
        t = self.canvas.transform()
        if t is None:
            return
        gx, gy = mapper.screen_to_guest(pos.x(), pos.y(), t)
        self.canvas.set_marker((gx, gy))
        self.session.pointer(gx, gy, buttons, True)

    def _send_relative(self, pos: QPointF, buttons: int) -> None:
        if self._last_pos is None:
            self._last_pos = pos
            return
        dx = int(round(pos.x() - self._last_pos.x()))
        dy = int(round(pos.y() - self._last_pos.y()))
        if dx or dy or buttons != self._rel_buttons:
            self.session.pointer(dx, dy, buttons, False)
        self._rel_buttons = buttons
        if self.grab.active:
            self.grab.recenter()                       # 光标钉回中心，保持增量语义
            self._last_pos = QPointF(self.canvas.width() / 2, self.canvas.height() / 2)
        else:
            self._last_pos = pos

    def keyPressEvent(self, event) -> None:            # noqa: N802
        if event.isAutoRepeat() and not event.text():
            return
        code = self.keymap.code_for(event.key(), event.text())
        if code:
            self.session.key(code, True)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:          # noqa: N802
        if event.isAutoRepeat() and not event.text():
            return
        code = self.keymap.code_for(event.key(), event.text())
        if code:
            self.session.key(code, False)
        else:
            super().keyReleaseEvent(event)

    # ------------------------------------------------------------------ #
    # 捕获 / 视图动作
    # ------------------------------------------------------------------ #
    def _toggle_grab(self) -> None:
        self.grab.toggle()
        self._sync_check(self.act_grab, self.grab.active)

    def _on_grab_toggled(self, on: bool) -> None:
        if on != self.grab.active:                      # 用户点了菜单项
            self.grab.toggle()
        self._refresh_status()

    def _on_grab_changed(self, active: bool) -> None:
        self._last_pos = None
        self._refresh_status()

    def event(self, e):
        # 窗口失去激活 -> 按配置释放捕获，免得用户被"困"在没有光标的窗口里
        if (e.type() == QEvent.WindowDeactivate
                and self.cfg.get("input.grab.release_on_focus_loss", True)):
            if self.grab.active:
                self.grab.release()
                self.act_grab.setChecked(False)
        return super().event(e)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._set_mode(self._mode_before_fs or "fit")
        else:
            self._mode_before_fs = self.cfg.get("display.mode", "fit")
            self._set_mode("fullscreen")
            self.showFullScreen()

    def _reset_view(self) -> None:
        self._set_mode("fit")
        if not self.isFullScreen():
            self.resize(self.canvas.sizeHint())

    def _screenshot(self) -> None:
        img = self.canvas.current_image()
        if img is None:
            self.lbl_hint.setText("还没有画面可截图")
            return
        name = "vmgui-screen-%s.png" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(os.getcwd(), name)
        if img.save(path):
            self.lbl_hint.setText(f"已保存 {path}")
            log.info("截图已保存 %s", path)
        else:
            self.lbl_hint.setText(f"截图保存失败: {path}")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self._apply_config, self)
        dlg.exec()

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:               # noqa: N802
        self._settings.setValue("geometry", self.saveGeometry())
        self.grab.release()
        self.session.stop()
        super().closeEvent(event)