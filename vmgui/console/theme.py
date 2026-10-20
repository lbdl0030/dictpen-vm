"""控制台的视觉令牌 + QSS 皮肤（`38-modern-ui.md` §2(a)，决策 #65）。

**这里只有样式，没有任何逻辑** —— QSS 就是 CSS 语法（选择器 / 盒模型 / `border-radius` /
伪态 `:hover`/`:checked`/`:disabled`），改一个文件就能改全局。三条纪律照 `16` 抄：

* 无发光、无彩色块、无渐变卡片；分隔靠 **hairline**（1 px 线），不靠投影；
* **单一强调色**（`accent`），只给"当前选中 / 焦点"用；
* 语义色（`danger`/`warn`）只留给**真状态**（故障/降级），不做装饰。

用法：`theme.apply(app)` —— 只在控制台模式调（`__main__.py`），
**不碰** vmgui CLI 那侧的窗口手感（同决策 #29 的理由：那是别人的入口）。

Qt 的 QSS **没有** flex/grid、`:has()`、`transition`、`box-shadow` —— 这里也一个都没用；
排版仍由 Qt 布局器拼，本文件只管"长什么样"。
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# 令牌：颜色 / 间距 / 圆角 / 字号。想调皮肤只改这一块。
# --------------------------------------------------------------------------- #
TOKENS = {
    "bg":         "#1b1d20",     # 窗口底
    "bg_panel":   "#202327",     # 状态栏 / 抽屉
    "bg_raised":  "#26292e",     # 悬停 / 次按钮
    "bg_sunken":  "#141619",     # 日志格（比窗口更沉，让文字浮出来）
    "bg_press":   "#2d3137",
    "hairline":   "#34383e",     # 唯一的分隔手段
    "hairline_hi": "#4a5057",    # 悬停时才提亮的那条线
    "hairline_off": "#2f343a",   # 禁用件的线（比 hairline 更沉，但仍看得见）
    "text":       "#e8eaed",
    "text_muted": "#9aa0a6",
    "text_faint": "#6d7278",     # 弱化（信息位、说明文字）
    # 禁用件的字色**单独一枚**（`39` §2 第 1 条）：深色底上只降不透明度会让
    # 「启动」几乎看不见 —— 对比度实测 4.9:1（对 @bg），够看清"它在这儿、只是不能按"。
    "text_disabled": "#828890",
    "accent":     "#5b8def",     # 单一强调：选中 / 焦点
    "danger":     "#e5484d",     # 真故障（不进装饰）
    "warn":       "#d7a13b",     # 降级
    "mono":       "'Cascadia Mono', Consolas, 'DejaVu Sans Mono', monospace",
    "ui":         "'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', sans-serif",
    "fs":         "13px",
    "fs_small":   "12px",
    "fs_title":   "15px",
}

# --------------------------------------------------------------------------- #
# QSS 本体。`@name` 是令牌占位符，`stylesheet()` 里替换（用 `.format()` 要转义
# QSS 的每一对花括号，用 replace 更清楚）。
# --------------------------------------------------------------------------- #
_QSS = """
QMainWindow, QDialog { background: @bg; }
QWidget { color: @text; font-family: @ui; font-size: @fs; }

/* ---- 工具条：动词都在这里，收敛成一行 ---- */
QToolBar { background: @bg; border: 0; border-bottom: 1px solid @hairline;
           padding: 6px 8px; spacing: 4px; }
QToolBar::separator { background: @hairline; width: 1px; margin: 5px 7px; }
QToolBar QToolButton { color: @text; background: transparent;
                       border: 1px solid transparent; border-radius: 6px;
                       padding: 5px 11px; }
QToolBar QToolButton:hover { background: @bg_raised; border-color: @hairline; }
QToolBar QToolButton:pressed { background: @bg_press; }
QToolBar QToolButton:checked { color: @text; background: @bg_raised;
                               border-color: @hairline_hi; }
QToolBar QToolButton:disabled { color: @text_disabled; }
QToolBar QToolButton:checked:disabled { color: @text_disabled;
                                        border-color: @hairline_off; }

/* ---- 状态栏：一行读数，形状（前缀/尾注）比颜色重要 ---- */
QStatusBar { background: @bg_panel; border-top: 1px solid @hairline; }
QStatusBar::item { border: 0; }
QStatusBar QLabel { color: @text_muted; font-size: @fs_small; padding-left: 8px; }

/* `39` §2 #3：工具条里那条"我们自己量的"取帧读数 —— 弱化到不抢动词 */
QLabel#rate { color: @text_faint; font-size: @fs_small; padding-right: 10px; }

/* ---- 右抽屉：标签页当"分段控件"用 ---- */
QWidget#drawer { background: @bg_panel; border-left: 1px solid @hairline; }
/* 抽屉顶上的「暂停画面」开关：选中用**中性灰**填充，不引颜色（#16 / `39` §5 #10） */
QCheckBox { color: @text_muted; font-size: @fs_small; }
QCheckBox::indicator { width: 12px; height: 12px; background: @bg_sunken;
                       border: 1px solid @hairline; border-radius: 3px; }
QCheckBox::indicator:checked { background: @text_muted; border-color: @text_muted; }
QCheckBox::indicator:disabled { background: transparent; border-color: @hairline_off; }
QTabWidget::pane { border: 0; border-top: 1px solid @hairline; background: @bg_panel; }
QTabBar::tab { background: transparent; color: @text_muted; padding: 7px 12px;
               border: 0; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { color: @text; }
QTabBar::tab:selected { color: @text; border-bottom-color: @accent; }

/* ---- 日志格 / 命令行：等宽、沉底、无边框 ---- */
QPlainTextEdit { background: @bg_sunken; color: #c9ced4; border: 0;
                 font-family: @mono; font-size: @fs_small; padding: 6px;
                 selection-background-color: @accent; }
QLineEdit { background: @bg_sunken; color: @text; border: 1px solid @hairline;
            border-radius: 6px; padding: 6px 9px; font-family: @mono;
            font-size: @fs_small; margin: 6px; }
QLineEdit:focus { border-color: @accent; }
QLineEdit:disabled { color: @text_disabled; border-color: @hairline_off; }

/* ---- 按钮：默认是"次按钮"（描边），主按钮由 objectName=primary 提权 ---- */
QPushButton { background: @bg_raised; color: @text; border: 1px solid @hairline;
              border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: @bg_press; border-color: @hairline_hi; }
QPushButton:pressed { background: #22262b; }
QPushButton:disabled { color: @text_disabled; border-color: @hairline_off;
                       background: transparent; }
/* 一组按钮里的**第一个**是主按钮（空态的出口）：唯一用强调色填充的地方 */
QPushButton#primary { background: @accent; color: #0d1117; border-color: @accent;
                      font-weight: 600; }
QPushButton#primary:hover { background: #6f9bf2; border-color: #6f9bf2; }
QPushButton#primary:pressed { background: #4f7fd8; border-color: #4f7fd8; }
QPushButton#primary:disabled { background: transparent; color: @text_disabled;
                               border-color: @hairline_off; }

/* ---- 空态正文：标题重、正文轻（`31-console-copy.md` §3 的排版） ---- */
QLabel#emptyTitle { font-size: @fs_title; font-weight: 600; color: @text; }
QLabel#emptyBody { color: @text_muted; font-size: @fs_small; }
/* 抽屉「guest 应用」页的首行（决策 #75）：身份/热点是**信息位**，弱化但不隐身 */
QLabel#identityNote { color: @text_muted; font-size: @fs_small; padding: 8px 10px;
                      border-bottom: 1px solid @hairline; }
/* 「文件」页（`42` §2）：小节标题用弱化大写感，剩余空间那行单独提一档（它不是装饰） */
QLabel#fileSection { color: @text_faint; font-size: @fs_small; padding-top: 6px; }
QLabel#spaceNote { color: @text_muted; font-size: @fs_small; padding: 2px 0 6px 0;
                   border-bottom: 1px solid @hairline; }

/* ---- 滚动条 / 提示 / 菜单 ---- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a3f46; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: @hairline_hi; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #2b2f35; color: @text; border: 1px solid @hairline; padding: 4px 6px; }
QMenu { background: @bg_panel; color: @text; border: 1px solid @hairline; padding: 4px; }
QMenu::item { padding: 5px 18px; border-radius: 4px; }
QMenu::item:selected { background: @bg_raised; }
QMenu::separator { height: 1px; background: @hairline; margin: 4px 8px; }
"""


def tokens() -> dict:
    return dict(TOKENS)


def stylesheet() -> str:
    """令牌替换后的 QSS 串。纯函数，可直接单测。

    **按名字长度倒序替换** —— 令牌名互为前缀（`fs` / `fs_small`、`bg` / `bg_panel`、
    `text` / `text_muted`），正序替换会让 `@fs` 先吃掉 `@fs_small` 的开头，变成
    `13px_small` 这种非法值 → 那条声明被 Qt 静默丢掉（离屏实测：日志格因此仍是白底）。
    """
    out = _QSS
    for key in sorted(TOKENS, key=len, reverse=True):
        out = out.replace("@" + key, TOKENS[key])
    return out


def apply(app) -> None:
    """把皮肤套到 `QApplication` 上（控制台模式下整进程只有这一个窗口）。"""
    app.setStyleSheet(stylesheet())
