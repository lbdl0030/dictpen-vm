"""设置面板：schema 驱动生成控件，支持应用 / 保存 / 重载 / 恢复默认 / profile 切换。

面板只负责把用户改动写进用户配置层（``Config.set``），再回调窗口做热更新；不直接
碰后端与视图。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from ..config import SCHEMA, list_profiles

SECTION_TITLES = {
    "connection": "连接",
    "display": "显示",
    "input": "输入",
    "audio": "音频",
    "clipboard": "剪贴板",
    "usb": "USB",
    "logging": "日志",
}
SECTION_ORDER = ["connection", "display", "input", "audio", "clipboard", "usb", "logging"]


class SettingsDialog(QDialog):
    def __init__(self, cfg, on_apply: Callable[[list[str] | None], None], parent=None) -> None:
        """on_apply 收到本次改动的配置键列表；None 表示范围未知（重载/恢复默认/
        切了 profile），由窗口决定要不要重建后端。面板与窗口共用同一个 Config 对象。"""
        super().__init__(parent)
        self.cfg = cfg
        self.on_apply = on_apply
        self.fields: list[tuple[str, str, QWidget]] = []
        self.setWindowTitle("vmgui 设置")
        self.setMinimumWidth(520)
        self._build()

    # ---- 构建 ---------------------------------------------------------- #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        root.addWidget(QLabel("机型 / 固件 profile（可多选，后者覆盖前者）:"))
        self.profile_list = QListWidget()
        self.profile_list.setMaximumHeight(96)
        selected = set(self.cfg.profile_names())
        for name in list_profiles():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in selected else Qt.Unchecked)
            self.profile_list.addItem(item)
        if self.profile_list.count() == 0:
            self.profile_list.addItem(QListWidgetItem("(profiles/ 下暂无 profile)"))
            self.profile_list.setEnabled(False)
        root.addWidget(self.profile_list)

        tabs = QTabWidget()
        by_section: dict[str, list[str]] = {}
        for path in SCHEMA:
            by_section.setdefault(path.split(".")[0], []).append(path)
        for section in SECTION_ORDER:
            if section not in by_section:
                continue
            page = QWidget()
            form = QFormLayout(page)
            form.setLabelAlignment(Qt.AlignRight)
            for path in by_section[section]:
                spec = SCHEMA[path]
                w = self._make_widget(path, spec)
                self.fields.append((path, spec["type"].__name__, w))
                form.addRow(spec.get("label", path), w)
            box = QGroupBox(SECTION_TITLES.get(section, section))
            inner = QVBoxLayout(box)
            inner.addWidget(page)
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.addWidget(box)
            wl.addStretch(1)
            tabs.addTab(wrap, SECTION_TITLES.get(section, section))
        root.addWidget(tabs, 1)

        self.status = QLabel(self._source_hint())
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        bar = QHBoxLayout()
        for text, slot in [("应用", self._apply), ("保存到用户配置", self._save),
                           ("从文件重载", self._reload), ("恢复默认", self._reset)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _make_widget(self, path: str, spec: dict) -> QWidget:
        t = spec["type"]
        value = self.cfg.get(path)
        if t is bool:
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if t is int and "choices" in spec:
            w = QComboBox()
            for c in spec["choices"]:
                w.addItem(str(c), c)
            idx = w.findData(int(value))
            w.setCurrentIndex(max(0, idx))
            return w
        if t is int:
            w = QSpinBox()
            w.setRange(int(spec.get("min", 0)), 10_000_000)
            w.setValue(int(value))
            return w
        if t is float:
            w = QDoubleSpinBox()
            w.setDecimals(2)
            w.setRange(float(spec.get("min", 0.1)), 1000.0)
            w.setSingleStep(0.5)
            w.setValue(float(value))
            return w
        if "choices" in spec:
            w = QComboBox()
            for c in spec["choices"]:
                w.addItem(str(c), c)
            idx = w.findData(str(value))
            w.setCurrentIndex(max(0, idx))
            return w
        w = QLineEdit(str(value))
        return w

    # ---- 取值 / 动作 --------------------------------------------------- #
    def _value(self, kind: str, w: QWidget):
        if kind == "bool":
            return w.isChecked()
        if kind == "int":
            return w.currentData() if isinstance(w, QComboBox) else w.value()
        if kind == "float":
            return float(w.value())
        if isinstance(w, QComboBox):
            return w.currentData()
        return w.text()

    def _selected_profiles(self) -> list[str]:
        out = []
        for i in range(self.profile_list.count()):
            it = self.profile_list.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.text())
        return out

    def _apply(self) -> None:
        changed = []
        for path, kind, w in self.fields:
            new = self._value(kind, w)
            if new != self.cfg.get(path):
                self.cfg.set(path, new)
                changed.append(path)
        profiles = self._selected_profiles()
        reselect = profiles != self.cfg.profile_names()
        if reselect:
            try:
                self.cfg.set_profiles(profiles)
                changed.append(f"profiles={profiles}")
            except Exception as e:
                self.status.setText(f"profile 加载失败: {e}")
                return
        self.status.setText(self._source_hint(changed))
        if self.on_apply:
            # profile 会覆盖任意键（包括连接参数），范围未知 -> 保守重建后端
            self.on_apply(None if reselect else changed)

    def _save(self) -> None:
        path = self.cfg.save()
        self.status.setText(f"已保存到 {path}")

    def _reload(self) -> None:
        self.cfg.reload()
        self._refresh()
        if self.on_apply:
            self.on_apply(None)

    def _reset(self) -> None:
        self.cfg.reset()
        self._refresh()
        if self.on_apply:
            self.on_apply(None)

    def _refresh(self) -> None:
        for path, kind, w in self.fields:
            value = self.cfg.get(path)
            if kind == "bool":
                w.setChecked(bool(value))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(value)))
            elif isinstance(w, QSpinBox):
                w.setValue(int(value))
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(float(value))
            elif isinstance(w, QLineEdit):
                w.setText(str(value))
        self.status.setText(self._source_hint())

    def _source_hint(self, changed: list[str] | None = None) -> str:
        head = f"用户配置: {self.cfg.user_path}"
        if changed:
            head += "；本次改动: " + ", ".join(changed)
        if self.cfg.warnings:
            head += "\n" + "\n".join(self.cfg.warnings[:4])
        return head