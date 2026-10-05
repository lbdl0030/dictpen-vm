"""离屏 GUI 冒烟：主窗口/画布/设置面板的接线检查。

不需要显示器，也不需要真机——用一份假帧直接喂画布；后端指向一个死端口，
所以不会碰到正在跑的 vmgui/QMP。

    python vmgui/tests/smoke_gui.py
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtWidgets import QApplication                        # noqa: E402

from vmgui.backends.base import Frame                             # noqa: E402
from vmgui.config import Config                                   # noqa: E402
from vmgui.core import logging as vlog                            # noqa: E402
from vmgui.core.session import Session                            # noqa: E402
from vmgui.view.settings_dialog import SettingsDialog             # noqa: E402
from vmgui.view.window import MainWindow                          # noqa: E402

W, H = 560, 170
FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def main() -> int:
    vlog.setup("warning")
    tmp = tempfile.mkdtemp(prefix="vmgui-smoke-")
    cfg = Config.load(
        cli_overrides={"connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                                      "reconnect": {"enabled": False}}},
        profiles=["pear-a6pro"],
        user_path=os.path.join(tmp, "config.toml"))

    app = QApplication(sys.argv[:1])
    session = Session(cfg)
    window = MainWindow(cfg, session)
    window.resize(1120, 340)
    window.show()

    frame = Frame(pixels=bytes(W * H * 3), width=W, height=H, stride=W * 3)
    window.bridge.push_resolution(W, H)
    window.bridge.push_frame(frame)
    window.bridge.push_state("connected", "")
    frame2 = Frame(pixels=b"\xff" * (W * H * 3), width=W, height=H, stride=W * 3)
    window.bridge.push_frame(frame2)
    app.processEvents()

    print("窗口/画布：")
    check(window._guest_size == (W, H), "分辨率事件更新了 guest 尺寸")
    check(window.canvas.guest_size == (W, H), "画布记录了 guest 尺寸")
    check(window.canvas.has_frame, "画布拿到了帧")
    check(window.canvas.transform() is not None, "画布算出了变换")
    img = window.canvas.grab()
    check(img.width() > 0 and img.height() > 0, "画布能渲染出图像")

    print("换分辨率（零改码自适应）：")
    # 喂一个完全不同尺寸的帧（模拟换机型/固件），客户端应当自己跟上，
    # 不需要任何针对分辨率的代码分支。
    big = Frame(pixels=bytes(960 * 266 * 3), width=960, height=266, stride=960 * 3)
    window.bridge.push_resolution(960, 266)
    window.bridge.push_frame(big)
    app.processEvents()
    check(window._guest_size == (960, 266), "960x266 的帧被接受（窗口侧）")
    check(window.canvas.guest_size == (960, 266), "画布跟随新尺寸")
    t_big = window.canvas.transform()
    check(t_big is not None and t_big.guest_w == 960 and t_big.guest_h == 266,
          "变换按新尺寸重算")
    check(window.canvas.grab().width() > 0, "新尺寸下仍能渲染")
    window.bridge.push_resolution(W, H)
    window.bridge.push_frame(frame2)
    app.processEvents()
    check(window._guest_size == (W, H), "切回原尺寸同样跟随")

    print("显示模式 / 算法 / 指针模式：")
    backend_before = session.backend
    for mode in ("1:1", "stretch", "free", "fullscreen", "fit"):
        window.mode_actions[mode].trigger()
        app.processEvents()
        if cfg.get("display.mode") != mode:
            check(False, f"切到 {mode}")
            break
    else:
        check(True, "五种显示模式都能切换")
    for algo in ("bilinear", "nearest"):
        window.algo_actions[algo].trigger()
        app.processEvents()
    check(cfg.get("display.scale_algo") == "nearest", "缩放算法可切换")
    for pm in ("relative", "absolute", "auto"):
        window.pm_actions[pm].trigger()
        app.processEvents()
    check(cfg.get("input.pointer_mode") == "auto", "指针模式可切换")
    check(session.backend is backend_before,
          "纯显示参数改动不重建后端（不重连 QMP）")
    cfg.set("connection.uri", "tcp:127.0.0.1:8")
    window._apply_config(["connection.uri"])
    app.processEvents()
    check(session.backend is not backend_before, "连接参数改动会重建后端")

    print("画布参数直改：")
    cfg.set("display.rotation", 90)
    window.canvas.set_config(cfg)
    window.canvas.grab()
    check(window.canvas.transform().rotation == 90, "旋转 90 生效")
    cfg.set("display.crop", {"enabled": True, "x": 10, "y": 10, "w": 100, "h": 60})
    window.canvas.set_config(cfg)
    window.canvas.grab()
    check(window.canvas.transform().content_w == 100, "裁剪生效")
    cfg.set("display.crop", {"enabled": False, "x": 0, "y": 0, "w": 0, "h": 0})
    cfg.set("display.rotation", 0)
    window.canvas.set_config(cfg)

    print("快捷键 / 动作：")
    check(window.act_fullscreen.shortcut().toString() == "F11", "全屏快捷键来自配置")
    check(window.act_shot.shortcut().toString() == "Ctrl+Alt+S", "截图快捷键来自配置")

    print("设置面板：")
    dlg = SettingsDialog(cfg, window._apply_config)
    check(len(dlg.fields) > 20, f"面板生成了 {len(dlg.fields)} 个字段")
    cfg.set("display.scale_algo", "bilinear")          # 模拟用户改动
    dlg._refresh()
    dlg._apply()
    app.processEvents()
    check(cfg.get("display.scale_algo") == "bilinear", "面板应用后配置生效")
    dlg._save()
    check(os.path.exists(cfg.user_path), "面板保存写出了用户配置")
    saved = open(cfg.user_path, encoding="utf-8").read()
    check("bilinear" in saved, "保存的内容包含改动")
    dlg._reset()
    check(cfg.get("display.scale_algo") == "nearest", "恢复默认后回到内置值")

    window.close()
    app.processEvents()
    print(f"\n{'FAIL: ' + str(FAILS) if FAILS else 'PASS: 全部通过'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())