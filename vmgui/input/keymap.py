"""键映射：Qt 按键 -> Linux input key code（guest 走 evdev 键码）。

guest 侧是竖屏 / 笔屏 UI，键盘不是主角，所以这里覆盖常用键 + 字母数字，
并留出按布局（us / de）切换字符键的通路；未知键返回 None 由调用方决定是否忽略。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt

# 字母 / 数字：物理位置 -> Linux 键码
_LETTERS = {
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25, "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35,
    "j": 36, "k": 37, "l": 38, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48,
    "n": 49, "m": 50,
}
_DIGITS = {c: 2 + i for i, c in enumerate("1234567890")}

# 字符 -> 键码（按下 Shift 时字符不同，但键码相同）
_US_PUNCT = {
    "-": 13, "=": 12, "[": 26, "]": 27, ";": 39, "'": 40, "`": 41, "\\": 43,
    ",": 51, ".": 52, "/": 53, " ": 57,
    "_": 13, "+": 12, "{": 26, "}": 27, ":": 39, '"': 40, "~": 41, "|": 43,
    "<": 51, ">": 52, "?": 53,
}

_LAYOUTS = {
    "us": {},
    # 德语：z/y 互换，另加几个变音符键位
    "de": {"z": 21, "y": 44, "ü": 26, "ö": 39, "ä": 40, "ß": 13},
}

_SPECIAL = {
    Qt.Key_Escape: 1,
    Qt.Key_Backspace: 14,
    Qt.Key_Tab: 15, Qt.Key_Backtab: 15,
    Qt.Key_Return: 28, Qt.Key_Enter: 28,
    Qt.Key_Insert: 110, Qt.Key_Delete: 111,
    Qt.Key_Home: 102, Qt.Key_End: 107,
    Qt.Key_PageUp: 104, Qt.Key_PageDown: 109,
    Qt.Key_Left: 105, Qt.Key_Right: 106, Qt.Key_Up: 103, Qt.Key_Down: 108,
    Qt.Key_Space: 57, Qt.Key_CapsLock: 58,
    Qt.Key_NumLock: 69, Qt.Key_ScrollLock: 70,
    Qt.Key_Print: 99, Qt.Key_Pause: 119,
    Qt.Key_Shift: 42, Qt.Key_Control: 29, Qt.Key_Alt: 56, Qt.Key_Meta: 125,
    Qt.Key_F1: 59, Qt.Key_F2: 60, Qt.Key_F3: 61, Qt.Key_F4: 62, Qt.Key_F5: 63,
    Qt.Key_F6: 64, Qt.Key_F7: 65, Qt.Key_F8: 66, Qt.Key_F9: 67, Qt.Key_F10: 68,
    Qt.Key_F11: 87, Qt.Key_F12: 88,
}

# 字母 / 数字键用 Key_A..Key_Z / Key_0..Key_9 的常量直接注册进 _SPECIAL
for _c, _code in list(_LETTERS.items()):
    _SPECIAL[getattr(Qt, f"Key_{_c.upper()}")] = _code
for _c, _code in list(_DIGITS.items()):
    _SPECIAL[getattr(Qt, f"Key_{_c}")] = _code


class KeyMapper:
    def __init__(self, layout: str = "auto") -> None:
        self.layout = layout if layout in _LAYOUTS else "us"

    def code_for(self, qt_key: int, text: str = "") -> Optional[int]:
        """Qt 键值（+ 可选文本）-> Linux 键码。"""
        if text:
            ch = text.lower()
            table = _US_PUNCT
            code = _LAYOUTS[self.layout].get(ch) or _LETTERS.get(ch) \
                or _DIGITS.get(ch) or table.get(ch)
            if code:
                return code
        return _SPECIAL.get(qt_key)

    def available_layouts(self) -> list[str]:
        return sorted(_LAYOUTS)