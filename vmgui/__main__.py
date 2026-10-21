"""vmgui 命令行入口：``python -m vmgui``。

配置优先级：命令行 > --profile（可叠加）> 用户配置 > 内置默认。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import Config, ConfigError, list_profiles
from .core import logging as vlog


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vmgui",
        description="QEMU 虚拟机客户端 GUI（显示 + 输入，适配不同固件/虚拟硬件）")
    ap.add_argument("--version", action="version", version=f"vmgui {__version__}")
    ap.add_argument("--config", metavar="PATH", help="用户配置文件路径（默认见设置面板）")
    ap.add_argument("--profile", action="append", default=[], metavar="NAME",
                    help="机型/固件 profile（可多次给出，后者覆盖前者）")
    ap.add_argument("--list-profiles", action="store_true", help="列出可用 profile 并退出")
    ap.add_argument("--print-config", action="store_true",
                    help="打印生效配置与每个键的来源并退出")
    ap.add_argument("--console", action="store_true",
                    help="以控制台模式启动（可视化操作：设备视图 + 状态栏 + 抽屉）")
    ap.add_argument("--pack", metavar="NAME",
                    help="控制台要操作的固件包名（起机与体检都用它）")

    g = ap.add_argument_group("连接")
    g.add_argument("--kind", choices=["qmp", "vnc"], help="显示协议")
    g.add_argument("--uri", help="连接地址，如 tcp:127.0.0.1:4446")
    g.add_argument("--timeout-ms", type=int, help="连接/命令超时 (ms)")

    g = ap.add_argument_group("显示")
    g.add_argument("--mode", choices=["1:1", "fit", "stretch", "free", "fullscreen"],
                   help="显示模式")
    g.add_argument("--scale-algo", choices=["nearest", "bilinear", "lanczos"], help="缩放算法")
    g.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], help="旋转角度")
    g.add_argument("--aspect", choices=["keep", "ignore"], help="是否保持宽高比")
    g.add_argument("--zoom", type=float, help="free 模式的缩放倍数")
    g.add_argument("--dpi-hint", type=int, help="DPI 提示（0=自动）")
    g.add_argument("--frame-poll-ms", type=int, help="拉帧间隔 (ms)")
    g.add_argument("--crop", metavar="X,Y,W,H", help="只显示画面的一部分（帧缓冲坐标）")
    g.add_argument("--fullscreen", action="store_true", help="以全屏启动")

    g = ap.add_argument_group("输入")
    g.add_argument("--pointer-mode", choices=["auto", "absolute", "relative"], help="指针模式")
    g.add_argument("--touch-button", type=int, help="触摸按键码（笔屏默认 330=BTN_TOUCH）")
    g.add_argument("--keymap", help="键盘布局（auto/us/de/...）")
    g.add_argument("--no-grab", action="store_true", help="启动时不捕获鼠标")

    g = ap.add_argument_group("日志")
    g.add_argument("--log-level", choices=["debug", "info", "warning", "error"])
    g.add_argument("--log-file", metavar="PATH")
    return ap


def _crop(spec: str) -> dict:
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--crop 需要 X,Y,W,H")
    x, y, w, h = (int(p) for p in parts)
    if x or y:
        # 决策 #35：裁剪原点恒 0（面板永远取左上角）。说出来，别静默忽略用户给的数。
        print(f"vmgui: --crop 的 X={x} Y={y} 会被忽略（裁剪原点恒 0，取左上角）",
              file=sys.stderr)
    return {"enabled": True, "x": 0, "y": 0, "w": w, "h": h}


def cli_overrides(args) -> dict:
    out: dict = {}
    simple = {
        "connection.kind": args.kind,
        "connection.uri": args.uri,
        "connection.timeout_ms": args.timeout_ms,
        "display.mode": "fullscreen" if args.fullscreen else args.mode,
        "display.scale_algo": args.scale_algo,
        "display.rotation": args.rotation,
        "display.aspect": args.aspect,
        "display.zoom": args.zoom,
        "display.dpi_hint": args.dpi_hint,
        "display.frame_poll_ms": args.frame_poll_ms,
        "input.pointer_mode": args.pointer_mode,
        "input.touch_button": args.touch_button,
        "input.keymap": args.keymap,
        "logging.level": args.log_level,
        "logging.file": args.log_file,
    }
    for path, value in simple.items():
        if value is not None:
            section, key = path.split(".", 1)
            out.setdefault(section, {})[key] = value
    if args.crop:
        out.setdefault("display", {})["crop"] = _crop(args.crop)
    if args.no_grab:
        out.setdefault("input", {}).setdefault("grab", {})["enabled"] = False
    if getattr(args, "console", False):
        out.setdefault("display", {})["frame_poll_ms"] = 200      # 决策 #29：不动 DEFAULTS 的 30
        _console_panel_crop(out, getattr(args, "pack", None))
    return out


def _console_panel_crop(out: dict, pack: str | None) -> None:
    """控制台把**包声明的面板尺寸**当裁剪线传进来（决策 #27）。

    falcon 的 DRM mode 可能比面板宽，照帧开窗口会把面板上看不见的边画出来，而且手势会偏。
    取不到面板就给 0，退回整帧。
    """
    if out.get("display", {}).get("crop"):
        return                                                # --crop 显式给了，用它
    try:
        from .console import harness
        pw, ph = harness.panel(pack)
    except Exception as e:                                    # 包信息拿不到不该挡住起窗口
        vlog.get("main").warning("读不到面板尺寸（%s），改按整帧显示", e)
        return
    if pw and ph:
        out.setdefault("display", {})["crop"] = harness.crop_for(pw, ph)



def _pack_swap_rb(name: str | None) -> bool:
    """机型包里声明了 [display].swap_rb 时，宿主呈现层交换 R/B。"""
    if not name:
        return False
    try:
        from scripts import packlib
        return bool(packlib.load_pack(name).swap_rb)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_profiles:
        names = list_profiles()
        print("\n".join(names) if names else "(没有可用 profile)")
        return 0

    try:
        cfg = Config.load(cli_overrides(args), args.profile, args.config)
    except ConfigError as e:
        print(f"vmgui: {e}", file=sys.stderr)
        return 2

    vlog.setup(cfg.get("logging.level", "info"), cfg.get("logging.file", ""))
    for w in cfg.warnings:
        vlog.get().warning("%s", w)

    if args.print_config:
        print(cfg.dump(with_sources=True))
        return 0

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("vmgui: 需要 PySide6（pip install PySide6）；"
              "注意 PySide6 目前要求 Python < 3.15", file=sys.stderr)
        return 3

    app = QApplication(sys.argv[:1])
    app.setApplicationVersion(__version__)

    if args.console:
        from .console import harness
        from .console import theme
        from .console.window import ConsoleWindow
        from .core.session import Session

        app.setApplicationName("dictpen-console")
        theme.apply(app)                          # 决策 #65：控制台的 QSS 皮肤
        harness.claim_console_pid()               # 决策 #24：控制台自己记账；#70：down 不收它
        session = Session(cfg)
        session.swap_rb = _pack_swap_rb(args.pack or harness.current_pack())
        window = ConsoleWindow(cfg, session, args.pack)
        session.start()
        window.show()
        rc = app.exec()
        harness.release_console_pid()
        return rc

    from .core.session import Session
    from .view.window import MainWindow

    app.setApplicationName("vmgui")
    session = Session(cfg)
    session.swap_rb = _pack_swap_rb(args.pack)
    window = MainWindow(cfg, session)
    session.start()

    if cfg.get("display.mode") == "fullscreen":
        window.showFullScreen()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())