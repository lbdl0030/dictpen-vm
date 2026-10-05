"""配置：schema、合并、优先级、来源追踪、保存 / 重载 / 恢复默认。

原则（见设计文档 §3）：核心代码里不出现任何机型 / 分辨率 / 设备名常量，
一切来自配置或运行时探测。

优先级（高 -> 低）：命令行 > --profile（可叠加，后者覆盖前者）> 用户配置
（GUI 面板写入）> 内置默认。启动时可打印“生效配置 + 每键来源”。
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any

try:
    import tomllib                      # Python 3.11+
except ModuleNotFoundError:             # pragma: no cover
    tomllib = None

APP = "vmgui"

# --------------------------------------------------------------------------- #
# 内置默认
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, Any] = {
    "connection": {
        "kind": "qmp",
        "uri": "tcp:127.0.0.1:4446",
        "timeout_ms": 3000,
        "reconnect": {"enabled": True, "backoff_ms": 500, "max_backoff_ms": 5000},
    },
    "display": {
        "mode": "fit",                 # 1:1 | fit | stretch | free | fullscreen
        "scale_algo": "nearest",       # nearest | bilinear | lanczos
        "rotation": 0,                 # 0 | 90 | 180 | 270（顺时针）
        "aspect": "keep",              # keep | ignore
        "dpi_hint": 0,                 # 0 = 自动
        "frame_poll_ms": 30,
        "zoom": 2.0,                   # mode="free" 时的缩放倍数
        "initial_zoom": 2.0,           # 初始窗口大小 = guest 尺寸 * 该值
        "background": "#101010",
        "pointer_marker": True,        # 画布内画触摸反馈环
        "crop": {"enabled": False, "x": 0, "y": 0, "w": 0, "h": 0},
    },
    "input": {
        "pointer_mode": "auto",        # auto | absolute | relative
        "pointer_device": "",
        "touch_button": 330,           # BTN_TOUCH（笔屏要触摸，不要 BTN_LEFT）
        "send_click_with_touch": True,
        "keymap": "auto",              # auto | us | de | ...
        "grab": {"enabled": True, "toggle_key": "Ctrl+Alt+G",
                 "release_on_focus_loss": True},
        "hotkeys": {
            "fullscreen": "F11",
            "screenshot": "Ctrl+Alt+S",
            "reset": "Ctrl+Alt+R",
            "quit": "Ctrl+Alt+Q",
            "settings": "Ctrl+Alt+P",
        },
    },
    "audio": {"enabled": False},
    "clipboard": {"enabled": False},
    "usb": {"redirection": False},
    "logging": {"level": "info", "file": ""},
}

# --------------------------------------------------------------------------- #
# schema：类型 / 取值校验 + 设置面板用到的元信息
# --------------------------------------------------------------------------- #
SCHEMA: dict[str, dict[str, Any]] = {
    "connection.kind": {"type": str, "choices": ["qmp", "vnc"], "label": "连接类型"},
    "connection.uri": {"type": str, "label": "连接地址"},
    "connection.timeout_ms": {"type": int, "min": 100, "label": "超时 (ms)"},
    "connection.reconnect.enabled": {"type": bool, "label": "断线自动重连"},
    "connection.reconnect.backoff_ms": {"type": int, "min": 0, "label": "重连退避 (ms)"},
    "connection.reconnect.max_backoff_ms": {"type": int, "min": 0, "label": "退避上限 (ms)"},
    "display.mode": {"type": str, "choices": ["1:1", "fit", "stretch", "free", "fullscreen"],
                     "label": "显示模式"},
    "display.scale_algo": {"type": str, "choices": ["nearest", "bilinear", "lanczos"],
                           "label": "缩放算法"},
    "display.rotation": {"type": int, "choices": [0, 90, 180, 270], "label": "旋转"},
    "display.aspect": {"type": str, "choices": ["keep", "ignore"], "label": "保持宽高比"},
    "display.dpi_hint": {"type": int, "min": 0, "label": "DPI 提示 (0=自动)"},
    "display.frame_poll_ms": {"type": int, "min": 5, "label": "拉帧间隔 (ms)"},
    "display.zoom": {"type": float, "min": 0.1, "label": "free 缩放倍数"},
    "display.initial_zoom": {"type": float, "min": 0.1, "label": "初始窗口倍数"},
    "display.background": {"type": str, "label": "背景色"},
    "display.pointer_marker": {"type": bool, "label": "画布内指针标记"},
    "display.crop.enabled": {"type": bool, "label": "启用裁剪"},
    "display.crop.x": {"type": int, "min": 0, "label": "裁剪 x"},
    "display.crop.y": {"type": int, "min": 0, "label": "裁剪 y"},
    "display.crop.w": {"type": int, "min": 0, "label": "裁剪宽"},
    "display.crop.h": {"type": int, "min": 0, "label": "裁剪高"},
    "input.pointer_mode": {"type": str, "choices": ["auto", "absolute", "relative"],
                           "label": "指针模式"},
    "input.pointer_device": {"type": str, "label": "指针设备 (留空=自动)"},
    "input.touch_button": {"type": int, "label": "触摸按键码"},
    "input.send_click_with_touch": {"type": bool, "label": "同时发左键"},
    "input.keymap": {"type": str, "label": "键盘布局"},
    "input.grab.enabled": {"type": bool, "label": "启用鼠标捕获"},
    "input.grab.toggle_key": {"type": str, "label": "捕获切换键"},
    "input.grab.release_on_focus_loss": {"type": bool, "label": "失焦自动释放"},
    "input.hotkeys.fullscreen": {"type": str, "label": "全屏"},
    "input.hotkeys.screenshot": {"type": str, "label": "截图"},
    "input.hotkeys.reset": {"type": str, "label": "重置视图"},
    "input.hotkeys.quit": {"type": str, "label": "退出"},
    "input.hotkeys.settings": {"type": str, "label": "设置"},
    "audio.enabled": {"type": bool, "label": "音频"},
    "clipboard.enabled": {"type": bool, "label": "剪贴板"},
    "usb.redirection": {"type": bool, "label": "USB 重定向"},
    "logging.level": {"type": str, "choices": ["debug", "info", "warning", "error"],
                      "label": "日志级别"},
    "logging.file": {"type": str, "label": "日志文件"},
}


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def profiles_dir() -> str:
    return os.path.join(package_dir(), "profiles")


def user_config_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP, "config.toml")


# --------------------------------------------------------------------------- #
# 合并工具
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并，overlay 覆盖 base；返回新 dict，不修改入参。"""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_path(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def del_path(data: dict, dotted: str) -> None:
    parts = dotted.split(".")
    stack = []
    cur = data
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            return
        stack.append((cur, part))
        cur = cur[part]
    cur.pop(parts[-1], None)
    for parent, key in reversed(stack):      # 清掉空 section
        if isinstance(parent.get(key), dict) and not parent[key]:
            parent.pop(key)


def leaves(data: dict, prefix: str = ""):
    for k, v in data.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from leaves(v, path + ".")
        else:
            yield path, v


def _record_sources(data: dict, layer: str, sources: dict, prefix: str = "") -> None:
    for path, _ in leaves(data, prefix):
        sources[path] = layer


# --------------------------------------------------------------------------- #
# TOML 写出（tomllib 只读，无写出器；这里手写一个够用的）
# --------------------------------------------------------------------------- #
def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot serialize {v!r} to TOML")


def dump_toml(data: dict, prefix: str = "") -> str:
    scalars, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict) and v:
            tables.append((k, v))
        else:
            scalars.append((k, v))
    out = []
    for k, v in scalars:
        out.append(f"{k} = {_toml_value(v)}")
    for k, v in tables:
        name = f"{prefix}{k}"
        out.append("")
        out.append(f"[{name}]")
        body = dump_toml(v, prefix + k + ".")
        out.append(body)
    return "\n".join(x for x in out if x is not None).strip() + "\n"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    pass


class Config:
    """合并后的生效配置 + 分层来源信息。"""

    def __init__(self) -> None:
        self.user_layer: dict[str, Any] = {}
        self.profile_layers: list[tuple[str, dict]] = []
        self.cli_layer: dict[str, Any] = {}
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.sources: dict[str, str] = {}
        self.user_path = user_config_path()
        self.warnings: list[str] = []

    # ---- 装载 ---------------------------------------------------------- #
    @classmethod
    def load(cls, cli_overrides: dict | None = None,
             profiles: list[str] | None = None,
             user_path: str | None = None) -> "Config":
        cfg = cls()
        if user_path:
            cfg.user_path = user_path
        cfg.user_layer = cfg._read_user()
        cfg.profile_layers = cfg._read_profiles(profiles or [])
        cfg.cli_layer = cli_overrides or {}
        cfg._rebuild()
        return cfg

    def _read_user(self) -> dict:
        if not os.path.exists(self.user_path):
            return {}
        try:
            with open(self.user_path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:                       # 坏文件不该让客户端起不来
            self.warnings.append(f"用户配置 {self.user_path} 解析失败: {e}")
            return {}

    def _read_profiles(self, names: list[str]) -> list[tuple[str, dict]]:
        """按名字/路径读机型 profile。

        找不到或解析失败只告警、不中断：profile 只影响显示模式/缩放这类装饰性参数，
        画面尺寸本来就是从帧里自适应出来的，不该因为一个 profile 名字写错就让整个
        客户端起不来（dpctl 会把机型 preset 的 client.profile 传进来）。
        """
        layers = []
        for name in names:
            path = name if os.path.isfile(name) else os.path.join(profiles_dir(), name + ".toml")
            if not os.path.exists(path):
                self.warnings.append(
                    f"找不到 profile: {name}（找过 {path}）—— 已跳过，显示参数用内置默认")
                continue
            try:
                with open(path, "rb") as f:
                    layers.append((name, tomllib.load(f)))
            except Exception as e:
                self.warnings.append(f"profile {name} 解析失败（已跳过）: {e}")
        return layers

    def _rebuild(self) -> None:
        self.data = copy.deepcopy(DEFAULTS)
        self.sources = {}
        _record_sources(DEFAULTS, "default", self.sources)
        for name, layer in self.profile_layers:
            self.data = deep_merge(self.data, layer)
            _record_sources(layer, f"profile:{name}", self.sources)
        if self.user_layer:
            self.data = deep_merge(self.data, self.user_layer)
            _record_sources(self.user_layer, "user", self.sources)
        if self.cli_layer:
            self.data = deep_merge(self.data, self.cli_layer)
            _record_sources(self.cli_layer, "cli", self.sources)
        self.warnings = [w for w in self.warnings if not w.startswith("校验")]
        self.warnings += self._validate()

    # ---- 读取 ---------------------------------------------------------- #
    def get(self, dotted: str, default: Any = None) -> Any:
        return get_path(self.data, dotted, default)

    def source_of(self, dotted: str) -> str:
        return self.sources.get(dotted, "?")

    def as_dict(self) -> dict:
        return copy.deepcopy(self.data)

    # ---- 修改 / 持久化 ------------------------------------------------- #
    def set(self, dotted: str, value: Any) -> None:
        set_path(self.user_layer, dotted, value)
        self._rebuild()

    def reset(self, dotted: str | None = None) -> None:
        """恢复默认：给 dotted 只清该键，否则清空整个用户层。"""
        if dotted is None:
            self.user_layer = {}
        else:
            del_path(self.user_layer, dotted)
        self._rebuild()

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.user_path), exist_ok=True)
        with open(self.user_path, "w", encoding="utf-8") as f:
            f.write("# vmgui 用户配置（设置面板写入）。删除本文件即恢复内置默认。\n")
            f.write(dump_toml(self.user_layer))
        return self.user_path

    def reload(self) -> None:
        self.user_layer = self._read_user()
        self._rebuild()

    def set_profiles(self, names: list[str]) -> None:
        self.profile_layers = self._read_profiles(names)
        self._rebuild()

    def profile_names(self) -> list[str]:
        return [n for n, _ in self.profile_layers]

    # ---- 校验 ---------------------------------------------------------- #
    def _validate(self) -> list[str]:
        out: list[str] = []
        known = set(SCHEMA)
        for path, value in leaves(self.data):
            if path in known:
                spec = SCHEMA[path]
                t = spec["type"]
                if t is float and isinstance(value, int) and not isinstance(value, bool):
                    continue
                if not isinstance(value, t) or isinstance(value, bool) != (t is bool):
                    out.append(f"校验 {path}: 期望 {t.__name__}，实际 {type(value).__name__}")
                    continue
                if "choices" in spec and value not in spec["choices"]:
                    out.append(f"校验 {path}: {value!r} 不在 {spec['choices']}")
                if "min" in spec and isinstance(value, (int, float)) and value < spec["min"]:
                    out.append(f"校验 {path}: {value} < 最小 {spec['min']}")
            else:
                out.append(f"校验 {path}: 未知配置键（忽略）")
        for _, spec in SCHEMA.items():
            pass
        # crop 的尺寸约束
        if self.get("display.crop.enabled"):
            if self.get("display.crop.w", 0) <= 0 or self.get("display.crop.h", 0) <= 0:
                out.append("校验 display.crop: 启用裁剪时 w/h 必须 > 0")
        if (self.get("connection.reconnect.max_backoff_ms", 0)
                < self.get("connection.reconnect.backoff_ms", 0)):
            out.append("校验 connection.reconnect: max_backoff_ms < backoff_ms")
        return out

    # ---- 打印 ---------------------------------------------------------- #
    def dump(self, with_sources: bool = True) -> str:
        lines = []
        for path, value in sorted(leaves(self.data)):
            src = f"   <- {self.source_of(path)}" if with_sources else ""
            lines.append(f"{path} = {value!r}{src}")
        return "\n".join(lines)


def list_profiles() -> list[str]:
    d = profiles_dir()
    if not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith(".toml"))