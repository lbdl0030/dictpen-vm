"""单元测试：配置优先级、坐标逆变换（含 crop/rotation/stretch）、缩放、键映射、指针模式。

跑法（需要 PySide6，因此用 Python 3.13）：
    python -m unittest discover -s vmgui/tests -t .
或直接：
    python vmgui/tests/test_units.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # 离屏建窗口（控制台那几测要用）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import QEvent, QPointF, QSize, Qt                  # noqa: E402
from PySide6.QtGui import QKeyEvent                                     # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

from vmgui.backends.base import Capabilities, Frame, PointerMode     # noqa: E402
from vmgui.config import Config, DEFAULTS, deep_merge, get_path, set_path  # noqa: E402
from vmgui.console import theme                                       # noqa: E402
from vmgui.core import rules as R                                     # noqa: E402
from vmgui.core.session import Session                                # noqa: E402
from vmgui.input import mapper                                        # noqa: E402
from vmgui.view import scaling                                        # noqa: E402


class TestTheme(unittest.TestCase):
    """#65 的 QSS 皮肤：令牌必须**一个不剩**地被替换掉，且互相不为前缀残片。"""

    def test_all_tokens_substituted(self):
        qss = theme.stylesheet()
        self.assertNotIn("@", qss, "QSS 里还有没替换掉的令牌占位符")

    def test_prefix_tokens_do_not_collide(self):
        """`fs` 与 `fs_small`、`bg` 与 `bg_panel`、`text` 与 `text_muted` 互为前缀。

        正序替换会让 `@fs` 先吃掉 `@fs_small` 的开头 → `13px_small` 这种非法值 →
        Qt **静默丢掉**那条声明（离屏实测：日志格保持白底）。所以按长度倒序替换。
        """
        qss = theme.stylesheet()
        for key, value in theme.tokens().items():
            for other in theme.tokens():
                if other != key and other.startswith(key):
                    self.assertNotIn(f"{value}_{other[len(key):]}", qss,
                                     f"{key} 先替换吃掉了 {other}")

    def test_key_rules_present(self):
        qss = theme.stylesheet()
        for sel in ("QToolBar QToolButton", "QPlainTextEdit", "QPushButton#primary",
                    "QWidget#drawer", "QLabel#emptyTitle", "QStatusBar",
                    "QLabel#rate", "QCheckBox::indicator:checked"):
            self.assertIn(sel, qss, f"缺少 {sel} 的样式")

    def test_disabled_has_its_own_token(self):
        """`39` §2 #1：禁用件不能只降不透明度 —— 深色底上「启动」会看不见。

        单独一枚 `text_disabled`，且**必须被用进样式表**（否则改了令牌也没生效）。
        """
        t = theme.tokens()
        self.assertIn("text_disabled", t)
        self.assertNotEqual(t["text_disabled"], t["text_faint"])
        self.assertIn(t["text_disabled"], theme.stylesheet())


class TestPowerVerbs(unittest.TestCase):
    """P0 电源四件里**只能靠静态手段验**的两条（决策 #70 / #71）。

    其余部分（真按一次「关机」）会停掉人正在跑的虚拟机，属于千问的实测范围。
    """

    def test_stop_helpers_leaves_the_console_alone(self):
        """`dpctl down` **不许**再顺手收掉控制台（#70 / Q39 选 (a)）。

        反例就是原点：控制台按自己的「关机」会 `taskkill` **自己**（窗口跟着消失，
        人看到的不是空态 #2 而是整个界面没了）。
        """
        from vmgui.console import harness
        d = harness.DPCTL
        called = []

        def make(n):
            def fn():
                called.append(n)
                return None
            return fn

        saved = {n: getattr(d, n) for n in ("stop_vmgui", "stop_console",
                                            "stop_bt_emu", "stop_pointer")}
        for n in saved:
            setattr(d, n, make(n))
        try:
            d._stop_helpers()
        finally:
            for n, fn in saved.items():
                setattr(d, n, fn)
        self.assertIn("stop_vmgui", called)
        self.assertNotIn("stop_console", called, "down 又去收控制台了")

    def test_qmp_owner_is_none_when_vm_is_down(self):
        """QEMU 不在跑时**没有**任何客户端能占着 QMP —— 记账文件在也不作数（#70）。

        否则空态 #4b 会对"谁还活着"说谎：QEMU 早关了，界面还在说"另一个实例占着画面"。
        """
        from vmgui.console import harness
        d = harness.DPCTL
        saved = d.vm_running
        d.vm_running = lambda: False
        try:
            self.assertIsNone(d.qmp_owner())
        finally:
            d.vm_running = saved


class _SpawnOnHotPath(BaseException):
    """**故意**不是 `Exception` 的子类：`pid_alive`/`qemu_pids` 旧写法外面套着

    `try: … except Exception: return False`。要护栏只在"没被吞"时才响，等于把回归
    装回原样它就哑了 —— 所以继承 `BaseException`，那层 `except Exception` 拦不住它。
    （单测里会报成 ERROR 而不是 FAIL，消息一样可读。）
    """


class _NoSpawn:
    """顶掉某个模块的 `subprocess`：一有人在这条路径上 spawn 进程就当场炸（#Q60 的护栏）。

    只拦"起进程"那几个入口，其余属性（`TimeoutExpired` 之类的异常、`DEVNULL` 等常量）
    照旧转发 —— 否则被测代码 `except subprocess.TimeoutExpired` 会先炸在无关的地方。
    """

    SPAWNERS = ("run", "Popen", "call", "check_call", "check_output")

    def __init__(self, owner):
        self._owner = owner

    def __getattr__(self, name):
        if name in self.SPAWNERS:
            raise _SpawnOnHotPath(
                f"{self._owner}.subprocess.{name}()：每帧都要走的路径上不许 spawn 进程（#Q60）")
        return getattr(subprocess, name)


class TestConsoleWindow(unittest.TestCase):
    """控制台那几处只能离屏验的接线：空态不许撑爆窗口（Q48）、缩放档位（`39` §4.2）。

    这里**不连 QMP、不跑事件循环**（信号是同线程直连，emit 即调用）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, pack="x6pro"):
        from vmgui.console.window import ConsoleWindow
        cfg = Config.load(cli_overrides={
            "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                           "reconnect": {"enabled": False}},
            "display": {"frame_poll_ms": 200}}, profiles=[], user_path=os.devnull)
        win = ConsoleWindow(cfg, Session(cfg), pack)
        win._probe_timer.stop()          # 别让 5 s 探针去戳真机器
        win._serial_timer.stop()
        # 「取帧」那一格的 1 s 心跳在测试里**必须停**：不断流的窗口测试里它会把
        # `取帧 220 ms` 刷成 `取帧 停了 N`（测试跑过 2 s 就翻脸），断言就成了看时序运气。
        win._rate_timer.stop()
        self.addCleanup(win.deleteLater)
        return win

    def test_wrap_hard_breaks_unbreakable_lines(self):
        """Q48：没有空格的 4111 字符行要能被**硬折**（QLabel 的 wordWrap 对它无效）。"""
        from vmgui.console.window import TAIL_LINE_MAX, _wrap_hard
        long_line = "A" * 4111
        out = _wrap_hard(long_line)
        self.assertEqual(len(out.splitlines()), (4111 + TAIL_LINE_MAX - 1) // TAIL_LINE_MAX)
        for line in out.splitlines():
            self.assertLessEqual(len(line), TAIL_LINE_MAX)

    def test_empty_state_does_not_blow_up_the_window(self):
        """千问实测：那条长行曾把窗口撑到 **5950 px**（屏 2560）→ `抽屉▾` 够不着。"""
        from vmgui.console import window as W
        from vmgui.console.window import TAIL_LINE_MAX
        win = self._window()
        saved = W.harness.serial_tail
        saved_run = W.harness.vm_running
        W.harness.serial_tail = lambda *a, **k: "B" * 4111
        W.harness.vm_running = lambda: False          # 这台机器"没在跑"
        try:
            sig = R.Signals(packs=[{"name": "x6pro", "ready": True, "missing": [],
                                    "md5_bad": [], "runtime_ok": True}],
                            packs_known=True, pack="x6pro", vm_running=False,
                            frames_ok=False, probe_ok="no-shell", probe={})
            win._on_decision((sig, None))
            win.bridge.resolution.emit(960, 480)
            win.bridge.frame.emit(Frame(pixels=bytes(960 * 480 * 3), width=960, height=480,
                                        stride=960 * 3))
            win._on_decision((sig, None))
        finally:
            W.harness.serial_tail = saved
            W.harness.vm_running = saved_run
        self.assertEqual(win._decision.view, R.VIEW_VM_DOWN)
        self.assertLessEqual(win.empty.maximumWidth(), 1100)
        for line in win.empty.body.text().splitlines():
            self.assertLessEqual(len(line), TAIL_LINE_MAX)
        # 机器停了、画布上还留着最后一帧 → 那帧必须被标成"上次"（#30 同一条）
        self.assertIn("（上次）", win.lbl_status.text())

    def test_fresh_warning_text_names_the_disk_and_its_size(self):
        """#15/#71：不可逆操作的弹窗**必须**让人认得出自己丢的是什么。

        两条禁令：不许写"清除数据"这类抽象词；不许承诺可恢复。作用域必须写"只这一台"。
        """
        from vmgui.console.window import fresh_warning_text
        txt = fresh_warning_text("a6pro", r"D:\download\vm\vm\state-a6pro.img", 512 * 1024 * 1024)
        self.assertIn("state-a6pro.img", txt)
        self.assertIn("512 MiB", txt)
        self.assertIn("只影响这一台", txt)
        self.assertIn("不可恢复", txt)
        self.assertNotIn("清除数据", txt)
        self.assertNotIn("可以恢复", txt)
        # 非整 GiB 的盘也要有人读得懂的大小
        from vmgui.console.harness import format_size, state_image
        self.assertEqual(format_size(0), "还没有状态盘")
        self.assertEqual(format_size(4 * 1024 ** 3), "4.0 GiB")
        self.assertEqual(format_size(512 * 1024 ** 2), "512 MiB")
        self.assertEqual(state_image("__no_such_pack__")[1], 0)

    def test_disk_full_note_separates_space_from_stability(self):
        """Q49：`disk-full` 的判据是**或**，可"目录条目超 500"不是"盘快满"。

        a6pro 实测：盘只用 **20%**、`/userdisk/corefile` **626 项**（真 core 转储 0 个，全是
        Capframe 日志）—— 旧文案印成「状态盘快满：20%」，一句话里自己打自己。
        """
        from vmgui.console.window import disk_full_note
        text, key = disk_full_note(20, 626)
        self.assertEqual(key, "core")
        self.assertNotIn("快满", text)
        self.assertIn("不是故障", text)
        text, key = disk_full_note(97, None)          # 数不到那半句：不许印字面 None（#30）
        self.assertEqual(key, "pct")
        self.assertIn("97%", text)
        self.assertNotIn("None", text)
        text, key = disk_full_note(97, 626)
        self.assertEqual(key, "pct+core")
        self.assertIn("97%", text)
        self.assertIn("626", text)

    def test_persistent_warning_logs_once_even_while_the_count_climbs(self):
        """警告还在响就不许每轮重播 —— 但**换了档必须再出声**。

        实测（截图 `vm/_qwen_ui_after.png`）：抽屉「命令」页连着 10 行
        `[警告] … corefile 592→626`。根因是去重键用了**整句话**，而句里那个数每 5 s 就在涨
        （a6pro 的 Capframe 日志实测 0.86 个/秒）→ 键永远在变 → 等于没去重。
        """
        from vmgui.core.rules import Decision, Flag, FLAG_DISK_FULL
        win = self._window()

        def warn(core):
            win._note_flags(Decision(flags=(Flag(
                FLAG_DISK_FULL, {"state_pct": 20, "core_count": core}),)))

        for core in (592, 605, 626):
            warn(core)
        lines = [l for l in win.cmdlog.toPlainText().splitlines() if l.startswith("[警告]")]
        self.assertEqual(len(lines), 1, "同一档不许刷第二行")
        self.assertIn("592", lines[0])                # 留下的是**第一次**那条
        warn(700)                                     # 数字再动，还是不播
        self.assertEqual(len([l for l in win.cmdlog.toPlainText().splitlines()
                              if l.startswith("[警告]")]), 1)
        win._note_flags(Decision(flags=(Flag(            # 盘真的开始满了 → 必须再出声
            FLAG_DISK_FULL, {"state_pct": 96, "core_count": 626}),)))
        lines = [l for l in win.cmdlog.toPlainText().splitlines() if l.startswith("[警告]")]
        self.assertEqual(len(lines), 2)
        self.assertIn("96%", lines[1])

    def test_corefile_exit_button_follows_the_flag_not_the_log(self):
        """Q49 出口键的行为规格：警告在就出现、警告走就消失（平时不占位、不留残影）。

        两条踩过的坑：① 断言用 `isHidden()` 不用 `isVisible()` —— 离屏窗口从没 `show()` 过，
        后者恒为 False，拿它断言会**永远假过**；② 这条**测不出** `setVisible` 该放在去重早退
        前面还是后面（千问把那一行挪到早退之后重跑，照样绿）：键里含 `disk-full:<branch>`，
        可见性要变必然带着键一起变，没有输入能让两种放法分开。那一处是按判断放的
        （理由见 `window.py:_note_flags` 里的注释），**别把这条当成它的证据**。
        """
        from vmgui.core.rules import Decision, Flag, FLAG_DISK_FULL
        win = self._window()
        self.assertTrue(win.btn_corefile.isHidden())              # 没警告 → 整行不占位

        def flag(core):
            return Decision(flags=(Flag(
                FLAG_DISK_FULL, {"state_pct": 20, "core_count": core}),))

        win._note_flags(flag(592))
        self.assertFalse(win.btn_corefile.isHidden())
        win._note_flags(flag(626))                                # 键没变 → 日志那段早退
        self.assertFalse(win.btn_corefile.isHidden(), "早退把按钮留在上一轮状态")
        win._note_flags(Decision())                               # 警告消了
        self.assertTrue(win.btn_corefile.isHidden(), "警告没了还亮着 = 界面在说谎")

    def test_corefile_exit_fills_the_listing_and_starts_nothing(self):
        """出口只**填命令**，回车由人按；那格里已有内容时先回显再覆盖。

        为什么不是「文件」页"取回来"那格（`31` §9 原来那么写的）：`dpctl get` 先 sftp `get`
        再回落 `ssh cat`，两头**都只吃单个文件**（`dpctl.py:1128-1148`），填目录必然换来一次
        "不是常规文件"的失败回执 —— 比不给出口更糟。
        """
        from vmgui.console import window as W
        win = self._window()
        ran = []
        win._shell_submit = lambda: ran.append(1)     # 出口若自己发起执行，这里就记账
        win._look_corefile()
        self.assertEqual(win.shell_in.text(), W.COREFILE_LS_CMD)
        self.assertIn("ls -l /userdisk/corefile", win.shell_in.text())
        self.assertEqual(ran, [], "警告是每 5 s 刷出来的，出口不许替用户跑 SSH")
        win.shell_in.setText("uptime")
        win._look_corefile()
        self.assertIn("uptime", win.cmdlog.toPlainText())          # 旧命令留痕，不是被吃掉

    def test_no_frame_note_does_not_claim_qmp_is_up(self):
        """`拿不到帧` 是**两件事**：连上了没画面 / 压根没连上。旧文案对后者说谎。

        实测依据：没起 VM 的控制台在后台重连 22 次（`vm/_qwen_ui_am.out`，07:17 起每 7 s 一条
        `QMP 重连第 n 次`），而抽屉里那句是"QMP 通了但没有画面"。判据 `frames_ok` 就是
        `canvas.has_frame`，两种情况共用一支；`_on_state` 本来收得到真状态，只是把它丢了。
        """
        from vmgui.core.rules import Decision, Flag, FLAG_NO_FRAME
        win = self._window()
        dec = Decision(flags=(Flag(FLAG_NO_FRAME, {}),))
        win._sess_state = "connecting"
        win._note_flags(dec)
        t = win.cmdlog.toPlainText()
        self.assertIn("QMP 还没连上", t)
        self.assertNotIn("QMP 通了", t)
        win._sess_state = "connected"          # 换了一支 → 必须再出一条，不许被去重吞掉
        win._note_flags(dec)
        lines = [l for l in win.cmdlog.toPlainText().splitlines() if l.startswith("[警告]")]
        self.assertEqual(len(lines), 2, "去重键没带分支，换了成因界面不再说新的那件")
        self.assertIn("QMP 通了但没有画面", lines[1])

    def test_no_frame_note_does_not_replay_when_qmp_flaps(self):
        """`connecting` ↔ `disconnected` 抖动**不许**每轮重播一条警告。

        实测：没有 VM 时后端每 ~3 s 抖一次（`vm/_qwen_state.py` 打出的时间线：
        0.1s connecting → 6.4s disconnected → 6.9s connecting …）。这一支的文案两档都是
        "QMP 还没连上"，把 `_sess_state` **原值**放进键 = 每 3 s 一行重复，正是 Q49 刚修掉的
        形状（那次是键里含一个每轮都在涨的 corefile 数）。所以键里只放粗档。
        """
        from vmgui.core.rules import Decision, Flag, FLAG_NO_FRAME
        win = self._window()
        dec = Decision(flags=(Flag(FLAG_NO_FRAME, {}),))
        warned = lambda: [l for l in win.cmdlog.toPlainText().splitlines()
                          if l.startswith("[警告]")]
        for state in ("connecting", "disconnected", "connecting", "disconnected"):
            win._sess_state = state
            win._note_flags(dec)
        self.assertEqual(len(warned()), 1, "抖动被当成了新故障，抽屉开始刷屏")
        self.assertIn("QMP 还没连上", warned()[0])
        win._sess_state = "connected"          # 粗档真变了 → 必须说得出另一件事
        win._note_flags(dec)
        self.assertEqual(len(warned()), 2)

    def test_no_frame_note_names_who_turned_reconnect_off(self):
        """重连被关掉时那句**要给出口**（§7 禁第 2 条：不给出口的检查等于没说话）—— 两支都要走。

        措辞改过三轮，每轮是被"查证"逼的：① "自动重连被配置关掉了（键名）" —— 只给键名，
        人不知道去哪改；② 改成"是 `config.toml` 里那一行" —— 可这项在 **SCHEMA** 里
        （`config.py:75`），profile / 命令行也设得了，只说 config.toml 会让人翻错文件；
        ③ 现在这版三个来源都列出，并明说**控制台里没有这颗按钮**（免得人去设置里找）。
        ⚠ 不许说"去设置里打开"：`SettingsDialog` 只挂在老客户端（`view/window.py:407`），
        `vmgui/console/` 里零命中 —— 那是指一个不存在的按钮。

        **为什么另起一台窗口而不用 `self._window()`**：那个夹具把
        `connection.reconnect.enabled` 钉在 **cli 层**（`False`，为了不让测试去戳死端口），
        而 `Config.set()` 写的是**用户层**，cli 永远赢（`config.py:326` + 分层合并顺序）——
        所以 `win.cfg.set(..., True)` 是**空操作**，两支里只走得到关掉那一支。
        这条不只关于措辞：**任何"运行时改配置"的路都改不动被 cli/profile 盖过的键**，
        将来给控制台加设置入口时要记得这点。
        """
        from vmgui.console.window import ConsoleWindow
        from vmgui.core.rules import Decision, Flag, FLAG_NO_FRAME
        dec = Decision(flags=(Flag(FLAG_NO_FRAME, {}),))

        def build(enabled):
            cfg = Config.load(cli_overrides={
                "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                               "reconnect": {"enabled": enabled}},
                "display": {"frame_poll_ms": 200}},
                profiles=[], user_path=os.devnull)
            win = ConsoleWindow(cfg, Session(cfg), "x6pro")
            win._probe_timer.stop()
            win._serial_timer.stop()
            self.addCleanup(win.deleteLater)
            return win

        for enabled, want, banned in (
                (True, "后台在自动重连，连上会自己出画面", "不会自己重连"),
                (False, "控制台里没有改它的按钮", "后台在自动重连")):
            win = build(enabled)
            self.assertIs(win.cfg.get("connection.reconnect.enabled"), enabled)  # 夹具别再骗人
            win._sess_state = "disconnected"
            win._note_flags(dec)
            line = [l for l in win.cmdlog.toPlainText().splitlines()
                    if l.startswith("[警告]")][-1]
            self.assertIn(want, line)
            self.assertNotIn(banned, line)
        win.cfg.set("connection.reconnect.enabled", True)      # 用户层盖不过 cli 层（见 docstring）
        self.assertIs(win.cfg.get("connection.reconnect.enabled"), False)

    def test_status_bar_says_why_the_picture_is_missing(self):
        """抽屉是关着的，所以"为什么没画面"必须能在状态栏看到 —— 但**三种例外都不许出现**。"""
        from vmgui.console import window as W
        win = self._window()
        saved = W.harness.vm_running
        W.harness.vm_running = lambda: True          # 机器确实在跑
        self.addCleanup(setattr, W.harness, "vm_running", saved)
        self.assertFalse(win.canvas.has_frame)
        win._sess_state = ""                          # ① 还没收到状态 ≠ 没连上（#30）
        self.assertNotIn("画面 未连上", win._status_text(R.Decision()))
        win._sess_state = "connecting"                # ② 真没连上 → 要说
        self.assertIn("画面 未连上", win._status_text(R.Decision()))
        win._sess_state = "connected"                 # ③ 连上了 → 不说
        self.assertNotIn("画面 未连上", win._status_text(R.Decision()))
        win._sess_state = "connecting"                # ④ 用户自己按了暂停 → 让位给"画面 暂停"
        win._paused = True
        text = win._status_text(R.Decision())
        self.assertIn("画面 暂停", text)
        self.assertNotIn("画面 未连上", text)

    def test_uptime_token_never_borrows_the_console_clock(self):
        """`运行` 那一格只说客机 uptime；采不到就说**未知**，不许拿"界面开了多久"顶。

        旧写法（`window.py` 改掉的那两行）在 uptime 缺失时回落
        `time.time() - self._started_at` —— 标签还是"运行"，数字却是控制台自己的年龄：
        接管一台已经跑 3 小时的机器印 `运行 0 分`；机器中途自己重启（`30-console-ui.md`
        §4 明写这一格的用途就是"判是不是刚自己重启过"）→ 数字继续单调涨，**把重启抹平**。
        `31-console-copy.md` §5:150 早就禁过墙钟（"重启过会倒退"），这条路径没照做。
        """
        import time
        from vmgui.console import window as W
        from vmgui.core.probe import Probe
        win = self._window()
        saved = W.harness.vm_running
        W.harness.vm_running = lambda: True          # 机器在跑、只是这一轮没回 uptime
        self.addCleanup(setattr, W.harness, "vm_running", saved)
        saved_packs = W.harness.packs_state
        W.harness.packs_state = lambda: "fresh"      # 否则提示行被"正在读固件包清单"占了（一条 tip）
        self.addCleanup(setattr, W.harness, "packs_state", saved_packs)
        win._started_at = time.time() - 3600         # 控制台开了一小时

        self.assertEqual(win._uptime_token({"uptime": "1883.98"}, True), "31 分")
        self.assertEqual(win._uptime_token({"state": "20%"}, True), "未知")
        self.assertEqual(win._uptime_token({}, False), "—")

        win._probe = Probe(values={"state": "20%", "core": "5"}, channel="ssh")
        text = win._status_text(R.Decision())
        self.assertIn("运行 未知", text)
        self.assertNotIn("运行 60 分", text)          # 借来的那个数一旦回来就是说谎
        self.assertIn("cut -d' ' -f1 /proc/uptime", win.lbl_status.toolTip())

    def test_status_bar_has_three_states_per_probe_field(self):
        """每一格要分得清**三档**：整轮没结果 / 这一轮没这个字段 / 有结果但未达阈值。

        旧写法只有两档 —— `alive` 却缺字段时那一格**直接蒸发**，而注释里明明写着
        "采不到时仍写 `—`"（`#30`：不许把"没采到"显示成"没有这件事"）。
        `触摸` 那一格还额外借了 `holder_pid()` 的懒：它对"字段没回来"和"字段是空的"
        回同一个 `None`（`probe.py:205-214`），于是"不知道"被印成"没人打开触摸节点"
        —— 那是一个**结论**，不能拿不知道当证据。
        """
        from vmgui.console import window as W
        from vmgui.core.probe import Probe
        win = self._window()
        saved = W.harness.packs_state
        W.harness.packs_state = lambda: "fresh"
        self.addCleanup(setattr, W.harness, "packs_state", saved)

        win._probe = None                                   # ① 整轮没结果
        text = win._status_text(R.Decision())
        for want in ("状态盘 —", "corefile 目录 —", "触摸 —", "运行 —"):
            self.assertIn(want, text)

        win._probe = Probe(values={"uptime": "100.0", "ap": "cfg"}, channel="ssh")
        text = win._status_text(R.Decision())               # ② 这一轮没这些字段
        self.assertIn("状态盘 未知", text)
        self.assertIn("corefile 目录 未知", text)
        self.assertIn("触摸 未知", text)
        self.assertNotIn("触摸 无人", text)                 # "不知道"不许冒充结论
        self.assertIn("运行 1 分", text)                    # 带回来的那个字段照常用

        win._probe = Probe(values={"holder": "", "state": "20%", "core": "5",
                                   "uptime": "100.0"}, channel="ssh")
        text = win._status_text(R.Decision())               # ③ 字段在、值说"没事"
        self.assertIn("触摸 无人", text)                    # 这才轮得到下结论
        self.assertNotIn("状态盘", text)                    # 20% 未达阈值 → 不占位
        self.assertNotIn("corefile 目录", text)             # 5 项未达阈值 → 不占位

    def test_app_slot_in_status_bar_has_three_states(self):
        """Q70 那一格与上面两格同一套三档；`apps=-`（这台没注册表）**不占位**。

        钉住两件事：**没读到**不许冒充"全好"（不占位就等于没这回事，那是 #30 的老坑）；
        而"这台压根没有应用注册表"是**采到了的确定答案**，也不该每 5 s 占一格 —— 解释归
        「guest 应用」页那行。
        """
        from vmgui.core.probe import Probe
        win = self._window()
        win._probe = Probe(values={"uptime": "100.0"}, channel="ssh")
        self.assertIn("应用 未知", win._status_text(R.Decision()))     # 这一轮没带回 apps

        win._probe = Probe(values={"uptime": "100.0", "apps": "-", "apps_bad": ""},
                           channel="ssh")
        self.assertNotIn("应用", win._status_text(R.Decision()))       # 没注册表 → 不占位

        win._probe = Probe(values={"uptime": "100.0", "apps": "22", "apps_bad": "0",
                                   "apps_ids": "", "apps_at": ""}, channel="ssh")
        self.assertNotIn("应用", win._status_text(R.Decision()))       # 都在 → 不占位

        win._probe = Probe(values={"uptime": "100.0", "apps": "22", "apps_bad": "5",
                                   "apps_ids": " 1 2 3 4 5",
                                   "apps_at": " /a /b /c /d /e"}, channel="ssh")
        self.assertIn("应用 5 个没装上", win._status_text(R.Decision()))

    def test_rate_label_stops_promise_when_frames_stop_coming(self):
        """工具条「取帧」那一格**不许用现在时报一个已经结束的节奏**。

        旧写法只在**帧到达**和**按暂停**时刷这一格（`_on_frame` / `_pause_toggled`），
        于是流一断（VM 关了、QMP 掉了、后端不再出帧）就再没人敲它 —— 那一格永远停在
        `取帧 220 ms（205~240）`。而**同一屏**的状态栏此刻诚实写着 `帧 960×480（上次）`
        （#80 / Q56）：一个说"上次"，一个说"正在每 220 ms 出一帧"，两套真相并排挂着。
        """
        import time
        win = self._window()
        self.assertEqual(win.lbl_rate.text(), "取帧 —")        # ① 一帧没收到 = 没量过

        win._frame_gaps.extend([200.0, 220.0, 240.0])
        win._last_frame_t = time.monotonic()
        win._refresh_rate_label()
        self.assertEqual(win.lbl_rate.text(), "取帧 220 ms（200~240）")   # ② 在流

        win._last_frame_t = time.monotonic() - 1.0
        win._refresh_rate_label()
        self.assertEqual(win.lbl_rate.text(), "取帧 220 ms（200~240）")   # 慢但活着：不翻脸

        win._last_frame_t = time.monotonic() - 30
        win._rate_timer.timeout.emit()      # 走**真接线**：断流后就是这一敲救场
        self.assertEqual(win.lbl_rate.text(), "取帧 停了 30 s")           # ③ 断了
        self.assertNotIn("220 ms", win.lbl_rate.text())

        win._paused = True
        win._refresh_rate_label()
        self.assertEqual(win.lbl_rate.text(), "取帧 暂停")                # ④ 用户按的优先

    def test_pause_boundary_is_not_a_frame_interval(self):
        """暂停期间强制抓的那一帧，间隔里装着**整段暂停时长**，不许当节奏样本。

        `qmp.py:287` 暂停时仍认 `force`（「存图」和「继续」都要能立刻抓一帧），所以
        暂停 3 分钟再继续，`_on_frame` 会收到一帧与上一帧相隔 180 000 ms 的"帧"。
        旧写法照记 → 24 格里多进一个 180 000，读数变成
        `取帧 7 711 ms（220~180 000）`（23 个 220 ms 样本 + 那一个，均值算过），
        **把"你刚才没在取帧"讲成"机器取一帧要 7.7 秒"**（替没发生过的事背书，
        与 #80 / Q56 同族）。
        """
        import time
        win = self._window()
        f = Frame(pixels=bytes(4 * 2 * 3), width=4, height=2, stride=12)
        win._on_frame(f)
        win._on_frame(f)                    # 两个时间戳才有一个正常样本
        self.assertEqual(len(win._frame_gaps), 1)

        win._pause_toggled(True)            # 暂停 3 分钟（用倒拨时间戳代替干等）
        win._pause_toggled(False)
        win._last_frame_t = time.monotonic() - 180
        win._on_frame(f)

        self.assertEqual(len(win._frame_gaps), 1, "跨暂停边界的那一帧被记成了节奏样本")
        self.assertLess(max(win._frame_gaps), 1000.0)
        # 继续之后必须还在"节奏"那一档（测试里两帧是真连着的，间隔就是 1 ms 量级，
        # 所以这里**不比数字**，只比形状：有 `ms（` 就是节奏档，出现 `停了` 才算翻脸）。
        self.assertIn("ms（", win.lbl_rate.text())
        self.assertNotIn("停了", win.lbl_rate.text())

    def test_status_bar_corefile_token_is_a_count_not_an_alarm(self):
        """状态栏那一格与抽屉必须同一套说法：`corefile 目录 N 项`，且**过阈值才占位**。

        旧写法 `if core:` —— 没事的机器也常驻一个数，而抽屉里根本不报警（两个面两套真相）。
        """
        from vmgui.core.probe import Probe
        from vmgui.core.rules import DEGRADE_CORE
        win = self._window()
        win._probe = None
        text = win._status_text(R.Decision())
        self.assertNotIn("core 文件", text)
        win._probe = Probe(values={"state": "20%", "core": str(DEGRADE_CORE + 126)},
                           channel="ssh")
        self.assertIn("corefile 目录 626 项", win._status_text(R.Decision()))
        win._probe = Probe(values={"state": "20%", "core": "5"}, channel="ssh")
        self.assertNotIn("corefile 目录 5", win._status_text(R.Decision()))

    def test_status_bar_says_panel_when_no_frame_ever_arrived(self):
        """Q56：**一帧没收过**的那一格要写 `面板 W×H`，不写"帧…（上次）"。

        机制（千问 2026-10-19 读码更正，Q56 原话里"包声明的面板尺寸"说错了一环）：
        `content_size()` 在无帧时兜底的是 **`display.crop`**（`canvas.py:53-56`），而裁剪线是
        上一次会话写进 `config.toml` 的 —— 所以"空控制台、VM 从没起过、一帧没收过"照样有个
        尺寸可印。旧写法只问"机器在不在跑"，于是印成 `帧 960×480（上次）`：**替没发生过的事
        背书"上次"**。判据换成 `has_frame`（与"存图"那颗键同一个，`window.py` 存图那段），
        而第三态的尺寸**改读包声明** `harness.panel(self.pack)` —— 否则换过一次包之后，
        这一格会把**上一个包**的尺寸当成本包的说。
        """
        win = self._window()                                     # pack = x6pro（声明 960×480）
        win.cfg.set("display.crop", {"enabled": True, "w": 800, "h": 254,
                                     "x": 0, "y": 0})           # 上一会话（x5pro）留下的裁剪线
        self.assertFalse(win.canvas.has_frame)
        self.assertEqual(win.canvas.oriented_size(), (800, 254))    # 兜底确实是它
        text = win._status_text(R.Decision())
        self.assertIn("面板 960×480", text)                      # 读的是**本包声明**
        self.assertNotIn("800×254", text)                        # 不许把上一会话的尺寸顶过来
        self.assertNotIn("（上次）", text)
        self.assertNotIn("帧 960×480", text)

    def test_status_bar_path_spawns_no_process(self):
        """#Q60 的护栏：状态栏**每帧**建一次，这条路径上 spawn 一个进程 = 界面卡死。

        断言的是"不 spawn"，不是"够快" —— 计时断言换台机器/换个负载就漂，抓不住回归。
        回归的前身：`pid_alive()` / `qemu_pids()` 各 `subprocess.run(["tasklist" …])`
        一次（实测 **475~545 ms/次**），而取帧节奏实测 **224 ms** → UI 线程占空比 ≈237 %
        → 事件队列永远排不空 → 用任务管理器的判据量（`SendMessageTimeout WM_NULL`）**5 s 不回话**。
        反向对照（把回归装回原样、确认这条会响）：`vm/_qwen_guard.py`。
        """
        if os.name != "nt":
            self.skipTest("POSIX 分支的 `pid_alive`/`qemu_pids` 本来就走 tasklist/pgrep，不是这条坑")
        from PySide6.QtGui import QImage
        from vmgui.console import window as W
        win = self._window()
        # **要造一帧**才走得到每帧那条路：`vm_running()` 挂在 `_status_text` 的
        # `if canvas.has_frame:` 分支里（Q56 之后，空控制台压根不问"机器在不在跑"）。
        # 不造帧的话，这一腿等于没测 —— 反向对照 `vm/_qwen_guard.py` 第一步就是这么哑的。
        win.canvas._image = QImage(560, 170, QImage.Format_RGB32)
        d = W.harness.DPCTL
        targets = [(d, "dpctl"), (W.harness, "harness")]
        saved = [(m, m.subprocess) for m, _ in targets]
        for m, name in targets:
            m.subprocess = _NoSpawn(name)
        self.addCleanup(lambda: [setattr(m, "subprocess", old) for m, old in saved])
        self.assertTrue(d.pid_alive(os.getpid()))         # 自己：不 spawn 也答得出来
        self.assertIsInstance(d.qemu_pids(), list)
        self.assertIsInstance(W.harness.vm_running(), bool)
        self.assertTrue(win._status_text(R.Decision()))   # 整条每帧路径走通

    def test_identity_note_lists_both_keys_and_never_concludes(self):
        """#75/#78：两件事并列；**不许**给单一结论（拦路页三种成因同形）。"""
        from vmgui.console.window import identity_note
        note = identity_note({"ap": "off", "sn": "empty", "uuid": "empty"})
        self.assertIn("虚拟热点：未开", note)
        self.assertIn("sn 空", note)
        self.assertIn("uuid 空", note)
        self.assertIn("都可能是原因", note)
        self.assertEqual(identity_note({}), "")

    def test_identity_note_marks_unknown_not_empty(self):
        """没采到要写"未知"，**不许**写成"空" —— 那会凭空制造一个故障。"""
        from vmgui.console.window import identity_note
        note = identity_note({"ap": "up:639"})
        self.assertIn("虚拟热点：已开", note)
        self.assertIn("sn 未知", note)
        self.assertNotIn("sn 空", note)

    def test_app_adapt_note_keeps_four_states_apart(self):
        """Q70 四态各说各的话：整轮没值 ／ 这台没注册表 ／ 都在 ／ 有缺。

        钉的是"没读到"与"读到了、没事"不许共用一句话（#30 那一族），与 #75 那两行同规矩。
        """
        from vmgui.console.window import app_adapt_note
        self.assertEqual(app_adapt_note({}), "")
        self.assertIn("没带回 `apps` 这个键", app_adapt_note({"uptime": "1"}))
        dash = app_adapt_note({"apps": "-"})
        self.assertIn("没有应用注册表", dash)
        self.assertIn("不判", dash)
        self.assertNotIn("0 条", dash)                       # 不许把"没有"说成"全好"
        ok = app_adapt_note({"apps": "22", "apps_bad": "0", "apps_ids": "", "apps_at": ""})
        self.assertIn("22 条", ok)
        self.assertIn("逐条都在盘上", ok)
        bad = app_adapt_note({"apps": "22", "apps_bad": "2",
                              "apps_ids": " 8080272425914438 8080212246010681",
                              "apps_at": " /x/a/app.js.bin /x/b/app_icon.png"})
        self.assertIn("2 条盘上没有文件", bad)
        self.assertIn("8080272425914438", bad)

    def test_app_adapt_note_says_the_two_symptoms(self):
        """警告要说**后果**：人报的是"丢图标"与"点开黑屏"，不是 `res=4`。"""
        from vmgui.console.window import app_adapt_note
        bad = app_adapt_note({"apps": "22", "apps_bad": "2", "apps_ids": " 111 222",
                              "apps_at": " /a /b"})
        self.assertIn("没图标", bad)
        self.assertIn("黑屏", bad)
        self.assertIn("「文件」页", bad)                     # 出口得指得着

    def test_app_missing_ls_cmd_only_takes_absolute_paths(self):
        """探针给 `installPath` 缺失的条目发的是 `?` —— 那种 token 进命令等于让客机 glob 单字符。"""
        from vmgui.console.window import app_missing_ls_cmd
        self.assertEqual(app_missing_ls_cmd([]), "")
        self.assertEqual(app_missing_ls_cmd(["?", "?"]), "")
        cmd = app_missing_ls_cmd(["/userdata/x/a/app.js.bin", "?", "/userdata/y/b/app_icon.png"])
        self.assertTrue(cmd.startswith("ls -ld "))
        self.assertNotIn("?", cmd)
        self.assertEqual(cmd.count("/app"), 2)

    def test_app_missing_drawer_key_ignores_the_count(self):
        """Q49 那一族的护栏：抽屉去重键里**不许**有会动的数字，只记"是哪几条"。

        装机过程中 `apps_bad` 会从 5 一路掉到 0，若把条数写进键，每 5 s 一轮就重播一行
        一模一样的警告（实测截图连 10 行那次就是这么来的）。
        """
        def dec(bad, ids):
            v = {"holder": "894:weston", "apps": "22", "apps_bad": str(bad),
                 "apps_ids": ids, "apps_at": " /a/app.js.bin /b/app.js.bin"}
            return R.decide(R.Signals(packs=[{"name": "x", "ready": True}], pack="x",
                                      vm_running=True, frames_ok=True, probe_ok="ok",
                                      probe=v))
        win = self._window("s7pro")
        win._last_flags_key = ""
        win.cmdlog.setPlainText("")
        win._note_flags(dec(2, " 111 222"))
        key1 = win._last_flags_key
        t1 = win.cmdlog.toPlainText()
        self.assertIn("app-missing:111,222", key1)
        self.assertIn("应用没装上：2/22", t1)                  # 确实落了一行进抽屉
        win._note_flags(dec(2, " 111 222"))                  # 同一批，再来一轮
        self.assertEqual(win.cmdlog.toPlainText(), t1)        # 不该又追加
        win._note_flags(dec(1, " 111 222"))                  # 条数变了、还是那几条 → 键不变
        self.assertEqual(win._last_flags_key, key1)
        self.assertEqual(win.cmdlog.toPlainText(), t1)
        win._note_flags(dec(1, " 111"))                      # **哪几条**变了 → 键必须变
        self.assertNotEqual(win._last_flags_key, key1)
        self.assertNotEqual(win.cmdlog.toPlainText(), t1)

    def test_zoom_gear_locks_the_scale(self):
        """选固定倍率就锁住；点回「适合窗口」才恢复自动（`39` §4.2）。

        用 s7pro 的 1020×240（帧 == 面板 → 不裁），免得把"按面板声明裁剪"也算进来。
        """
        win = self._window("s7pro")
        win.bridge.resolution.emit(1020, 240)
        win.bridge.frame.emit(Frame(pixels=bytes(1020 * 240 * 3), width=1020, height=240,
                                    stride=1020 * 3))
        win._set_zoom(2.0)
        self.assertEqual(win.cfg.get("display.initial_zoom"), 2.0)
        self.assertEqual(win.canvas.sizeHint(), QSize(2040, 480))
        self.assertIn("2×", win.btn_zoom.text())
        win._set_zoom(None)
        want = scaling.initial_scale(1020, 240, *win._canvas_avail())
        self.assertAlmostEqual(win.cfg.get("display.initial_zoom"), want)
        self.assertIn("适合", win.btn_zoom.text())

    def _stub_file_actions(self, *, backup_note="", put_rc=0, install=(0, "CMD", "OUT")):
        """把「放进去 / 装 miniapp」这条链上的三个 guest 动作换成**记账桩**。

        返回调用顺序的 list。`miniapp_cli` 到底在不在笔里、装成功是什么样子，本仓
        **没有实测记录**（VM 停着），所以这里只钉"发出去的是什么、按什么顺序发"，
        不钉 guest 真实回执的形状。
        """
        from vmgui.console import harness
        calls: list = []

        def backup(remote):
            calls.append(("backup", remote))
            return backup_note

        def put(local, remote):
            calls.append(("put", local, remote))
            return put_rc

        def inst(remote, *a, **k):
            calls.append(("install", remote))
            return install
        for name, fn in (("backup_remote", backup), ("put", put), ("install_miniapp", inst)):
            saved = getattr(harness, name)
            setattr(harness, name, fn)
            self.addCleanup(setattr, harness, name, saved)
        # `_on_file_op` 收尾会派生两股后台线程（重采盘用量、起一轮探针）—— 测试不许去戳真机器。
        self.win._refresh_space = lambda: None
        self.win._kick_probe = lambda: None
        return calls

    def test_amr_target_row_follows_the_field(self):
        """落点那一格**跟着上面那一格走**：粘进来的路径和选出来的走同一句显示。"""
        self.win = win = self._window()
        self.assertEqual(win.lbl_amr_target.text(), "落点 —（还没选文件）")
        win.f_amr_local.setText(r"D:\vm\包 A.amr")
        self.assertEqual(win.lbl_amr_target.text(), "落点 /userdata/包 A.amr")
        win.f_amr_local.setText("   ")
        self.assertEqual(win.lbl_amr_target.text(), "落点 —（还没选文件）")

    def test_install_row_refuses_and_names_the_way_out(self):
        """三种"不该发命令"的形状：空格、非 `.amr`、本地没有。

        非 `.amr` 那条必须**带出口**（§7 禁第 2 条：不许只说不行）—— 这一格旁边就是通用
        那行「放进去」，误选一个 `.so` 装下去是要改设备的。三条都不许发命令。
        """
        self.win = win = self._window()
        calls = self._stub_file_actions()
        log = lambda: win.filelog.toPlainText()
        win._install_clicked()
        self.assertIn("先选一个", log())
        win.f_amr_local.setText(r"D:\vm\lib.so")
        win._install_clicked()
        self.assertIn("只装", log())
        self.assertIn("放进去", log())
        win.f_amr_local.setText(r"D:\vm\没有这个.amr")
        win._install_clicked()
        self.assertIn("本地没有这个文件", log())
        self.assertEqual(calls, [])
        self.assertNotIn("$", log())              # 一条命令的痕迹都没有

    def test_install_lands_first_then_prints_the_command_it_sent(self):
        """顺序是 留档 → 放进去 → 安装；回执里要有**发出去的那条命令**、rc、字节数。

        命令要原样印出来：这是改设备状态的动作，只印一个 `rc` 出了问题无从对账。
        信号是同线程直连，所以可以直接调 `_install_worker`（不必起线程）。
        """
        import tempfile
        self.win = win = self._window()
        cmd = "miniapp_cli install '/userdata/包 A.amr'"
        calls = self._stub_file_actions(backup_note="已留档 /userdata/包 A.amr.bak-1",
                                       install=(0, cmd, "install ok\nDone."))
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "包 A.amr")
            with open(local, "wb") as f:
                f.write(b"x" * 4096)
            win._install_worker(local, "/userdata/包 A.amr")
        remote = "/userdata/包 A.amr"
        self.assertEqual(calls, [("backup", remote), ("put", local, remote),
                                 ("install", remote)])
        txt = win.filelog.toPlainText()
        self.assertIn(f"$ {cmd}", txt)            # 是 guest 那条，不是"放进去"那条
        self.assertIn("install ok", txt)
        self.assertIn("Done.", txt)
        self.assertIn("文件已落地 4096 B", txt)
        self.assertIn("已留档", txt)
        self.assertTrue(win.btn_install.isEnabled())     # 忙标记收回来了

    def test_install_does_not_fire_when_the_land_fails(self):
        """落地没成 → 安装那条**不许发**。

        为什么较真：文件没进去（或只进去半截）时 `miniapp_cli` 也会回一个错，那个错长得
        像"包有问题"，会把人从"传输没成"带到"安装失败"上去。日志里要写明"没发"。
        """
        import tempfile
        self.win = win = self._window()
        calls = self._stub_file_actions(put_rc=1)
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "a.amr")
            with open(local, "wb") as f:
                f.write(b"y")
            win._install_worker(local, "/userdata/a.amr")
        self.assertEqual([c[0] for c in calls], ["backup", "put"])
        txt = win.filelog.toPlainText()
        self.assertIn("没发", txt)
        self.assertIn("先解决传输", txt)
        self.assertNotIn("miniapp_cli", txt)

    def test_file_page_log_indents_every_line_of_a_receipt(self):
        """多行回执（安装要把 guest 输出带回来）每行都要缩进对齐，否则读不出条目边界。

        单行说明的老形状不许变 ——「取出来 / 放进去」那两条也走这个槽。
        """
        self.win = win = self._window()
        win._refresh_space = lambda: None
        win._kick_probe = lambda: None
        win.bridge.file_op.emit("放进去 a.amr → /userdata/a.amr", 0, "已留档 /userdata/a.amr.bak-1")
        win.bridge.file_op.emit("miniapp_cli install '/userdata/a.amr'", 0,
                               "文件已落地 1 B ｜ 原本没有同名文件\ninstall ok\nDone.")
        lines = win.filelog.toPlainText().splitlines()
        self.assertEqual(lines[0], "$ 放进去 a.amr → /userdata/a.amr")
        self.assertEqual(lines[1], "    rc=0  已留档 /userdata/a.amr.bak-1")
        self.assertEqual(lines[2], "$ miniapp_cli install '/userdata/a.amr'")
        self.assertEqual(lines[3], "    rc=0  文件已落地 1 B ｜ 原本没有同名文件")
        self.assertEqual(lines[4], "    install ok")
        self.assertEqual(lines[5], "    Done.")

    # ------------------------------------------------------------ 「配置」页 --
    def _stub_machine_actions(self, *, set_machine=({"memory": "3G"}, ""),
                              grow=(0, "resized 1024 MiB"),
                              growfs=(0, "resize2fs 说：done")):
        """「配置」页那三个动宾动作换成记账桩；返回调用顺序 list。

        `_refresh_machine` 里那两问（`MemTotal` / `df`）走 SSH，测试**一律不许真发**：
        整段重采换成 no-op，两个读数由测试自己填。日志那一格**不打桩** —— 文案是这一页
        的全部产出，断言要落在 `machlog` 上才量得到（和「文件」页那批同一形状）。
        """
        from vmgui.console import harness
        calls: list = []
        seq = set_machine if isinstance(set_machine, list) else None

        def set_m(pack, **kv):
            calls.append(("set", kv))
            return seq.pop(0) if seq else set_machine

        def grow_state(pack, want):
            calls.append(("grow", want))
            return grow

        def guest_grow_fs():
            calls.append(("growfs",))
            return growfs

        for name, fn in (("set_machine", set_m), ("grow_state", grow_state),
                         ("guest_grow_fs", guest_grow_fs)):
            saved = getattr(harness, name)
            setattr(harness, name, fn)
            self.addCleanup(setattr, harness, name, saved)
        saved = harness.state_image
        harness.state_image = lambda pack: ("/vm/state/a6pro.img", 512 * 1024 * 1024)
        self.addCleanup(setattr, harness, "state_image", saved)
        self.win._refresh_machine = lambda: None
        return calls

    def test_machine_page_puts_both_readings_side_by_side(self):
        """每个旋钮**两个读数**：下次起机用什么（谁定的）／客机现在看到什么。

        为什么较真：`-m` 是命令行参数，跑着的机器改不了。只写一个数，读的人必然按
        "已经生效"理解；两个数并排 + 各带来源，"改了但还没生效"才是看得见的状态。
        """
        self.win = win = self._window()
        win._m_next = {"memory": "3G", "smp": "2", "memory_src": "machine.toml",
                       "smp_src": "runtime 默认", "disk_mib": 1024, "pack": "x6pro"}
        win._m_live = {"mem_mib": 2925, "cpu": 2, "fs_mib": 487}
        win._render_machine()
        self.assertEqual(win.lbl_mem.text(), "下次 3G（machine.toml）｜客机看到 2.86 GiB")
        self.assertEqual(win.lbl_smp.text(), "下次 2（runtime 默认）｜客机看到 2 个")

    def test_machine_page_stops_promise_when_the_vm_is_down(self):
        """没在跑就不写「客机看到」，也不许写 0 —— 状态盘那一格改口说"读不到"。

        （与 Q56/Q64 同一族：把"没量过"写成"量到 0"，读的人当故障查。）
        """
        self.win = win = self._window()
        win._m_next = {"memory": "2G", "smp": "4", "memory_src": "runtime 默认",
                       "smp_src": "runtime 默认", "disk_mib": 512, "pack": "x6pro"}
        win._m_live = {}
        win._render_machine()
        self.assertNotIn("客机看到", win.lbl_mem.text())
        self.assertNotIn("客机看到", win.lbl_smp.text())
        self.assertIn("客机未运行", win.lbl_disk.text())

    def test_a_normal_disk_does_not_claim_a_gap(self):
        """`盘 512 / fs 487` 是 ext4 元数据的正常差，不许喊"扩了没用上"。

        阈值 `disk - fs > disk // 10`：512 vs 487 差 25（<51）不响；1024 vs 487 差 537 才响。
        乱响一次，人就不再信那一格 —— 下一次真忘了跑 resize2fs 就没人看了。
        """
        self.win = win = self._window()
        win._m_next = {"memory": "2G", "smp": "4", "memory_src": "x", "smp_src": "x",
                       "disk_mib": 512, "pack": "x6pro"}
        win._m_live = {"fs_mib": 487}
        win._render_machine()
        self.assertNotIn("差得多", win.lbl_disk.text())
        win._m_next["disk_mib"] = 1024
        win._render_machine()
        self.assertIn("差得多", win.lbl_disk.text())
        self.assertIn("按下面那颗", win.lbl_disk.text())

    def test_machine_write_sends_only_the_named_key(self):
        """按「改」只转发**点名的那一格** —— 另一格不许跟着变成"删掉"。

        这是实测抓出来的真 bug（`vm/_qwen_machine_write.py`）：先 `memory=3G` 再 `smp=2`，
        旧写法把 `memory=None` 一起转给 dpctl，而那边 `None` 的意思是"删了这个键" ——
        内存那格的覆盖被顺手抹掉，界面上"只改这一格，另一格不动"当场成假话。
        """
        self.win = win = self._window()
        calls = self._stub_machine_actions()
        win.f_smp.setText("2")
        win._machine_write("smp", win.f_smp, "vCPU")
        self.assertEqual([c[0] for c in calls], ["set"])
        self.assertEqual(calls[0][1], {"smp": "2"})

    def test_machine_write_names_the_next_boot_promise(self):
        """改成功那条要同时说三件：写进哪份文件、**下次起机才生效**、立刻生效按哪颗。"""
        self.win = win = self._window()
        self._stub_machine_actions()
        win.f_mem.setText("3G")
        win._machine_write("memory", win.f_mem, "内存")
        txt = win.machlog.toPlainText()
        self.assertIn("$ 内存 → 3G", txt)
        self.assertIn("rc=0", txt)
        self.assertIn("vm/machine.toml", txt)
        self.assertIn("下次起机才生效", txt)
        self.assertIn("重启现在生效", txt)

    def test_refused_machine_write_does_not_claim_a_change(self):
        """校验没过（`dpctl` 给的理由）→ 日志只说"没改"，**不许**出现"写进了"。

        这一族是"把没成功说成成功"：半句 `写进了 vm/machine.toml` 会让人去查一个
        根本没发生的改动。拒绝的理由照原样端出来，不在界面里重讲一遍。
        """
        self.win = win = self._window()
        self._stub_machine_actions(
            set_machine=({}, "内存要写成 `512M` / `2G` 这样（现在：'abc'）"))
        win.f_mem.setText("abc")
        win._machine_write("memory", win.f_mem, "内存")
        txt = win.machlog.toPlainText()
        self.assertIn("$ 内存 没改", txt)
        self.assertIn("rc=1", txt)
        self.assertIn("512M", txt)
        self.assertNotIn("写进了", txt)

    def test_grow_needs_a_number_and_ends_at_the_refusal(self):
        """扩容：空着不许发；`dpctl` 拒绝的理由（在跑／要变小）原样端出来。

        跑着的时候 `qemu-img resize` 换的是活文件的底 —— 拒绝归 `grow_state_image`，
        界面只负责把那句话送到人眼前，不另写一套（两处说法迟早分家）。
        """
        self.win = win = self._window()
        calls = self._stub_machine_actions(grow=(1, "VM 在跑 —— 先关机（跑着扩＝撕裂）"))
        win.f_disk.setText("")
        win._grow_clicked()
        self.assertIn("扩容没发", win.machlog.toPlainText())
        self.assertEqual([c for c in calls if c[0] == "grow"], [])
        win.f_disk.setText(" 1024 MiB ")
        win._grow_clicked()
        self.assertIn(("grow", 1024), calls)
        txt = win.machlog.toPlainText()
        self.assertIn("$ qemu-img resize a6pro.img → 1024 MiB", txt)
        self.assertIn("rc=1", txt)
        self.assertIn("先关机", txt)

    def test_resize2fs_asks_off_the_ui_thread(self):
        """点下去只许按下忙标记，SSH 那条（最坏 60 s）必须在**别的线程**（Q60 那一族）。"""
        import threading
        import time
        self.win = win = self._window()
        seen: dict = {}

        def growfs():
            seen["thread"] = threading.current_thread()
            return 0, "done"

        from vmgui.console import harness
        saved = harness.guest_grow_fs
        harness.guest_grow_fs = growfs
        self.addCleanup(setattr, harness, "guest_grow_fs", saved)
        win._refresh_machine = lambda: None
        win.btn_fs.setEnabled(True)
        win._growfs_clicked()
        self.assertFalse(win.btn_fs.isEnabled())        # 忙标记当场按下，没等回执
        for _ in range(400):
            if "thread" in seen:
                break
            time.sleep(0.005)
        self.assertIs(seen.get("thread"), seen.get("thread"))
        self.assertIsNot(seen.get("thread"), threading.main_thread())

    def test_resize2fs_receipt_lands_and_releases_the_button(self):
        """回执落回这一页、rc 原样、忙标记收回 —— 三个都要，否则按钮成砖。"""
        self.win = win = self._window()
        calls = self._stub_machine_actions(growfs=(1, "resize2fs: Permission denied"))
        win.btn_fs.setEnabled(False)
        win._growfs_worker()                             # 线程里那两句，同线程直连
        self.assertEqual(calls, [("growfs",)])
        txt = win.machlog.toPlainText()
        self.assertIn("$ resize2fs /dev/vdb", txt)
        self.assertIn("rc=1", txt)
        self.assertIn("Permission denied", txt)
        self.assertTrue(win.btn_fs.isEnabled())


class TestFileTransfer(unittest.TestCase):
    """`dpctl get/put` 换 sftp 通道后的两条纯逻辑（`42-file-transfer-features.md` §2.3/§2.8）。

    真机那半在 a6pro 上跑过：`get /etc/hostname` 10 B、`put` 带空格的文件名原样落地、
    取回 sha256 一致、`/tmp` 清理干净。
    """

    def test_sftp_quote_wraps_paths(self):
        from vmgui.console import harness
        q = harness.DPCTL._sftp_quote
        self.assertEqual(q("/tmp/qw en.txt"), '"/tmp/qw en.txt"')
        self.assertEqual(q("C:\\Users\\我带 空格\\a.so"), '"C:\\Users\\我带 空格\\a.so"')
        self.assertEqual(q('has"quote'), '"has\\"quote"')

    def test_shq_escapes_single_quotes(self):
        """guest 侧那条 `mkdir -p` / `cat >` 的参数必须包死，否则空格会截断路径。"""
        from vmgui.console import harness
        shq = harness.DPCTL._shq
        self.assertEqual(shq("/tmp/a b"), "'/tmp/a b'")
        self.assertEqual(shq("it's"), "'it'\\''s'")

    def test_size_text_and_space_note(self):
        """`42` §2.2 那行剩余空间：**采不到写"未知"，不许写 0 B**（#30 同一条）。

        另外 `_size_text` 与 `harness.format_size` 的 0 含义不同：后者 0 是"还没有状态盘"
        （重建弹窗），这里 0 就是 0 —— 两者不许共用一个函数。
        """
        from vmgui.console.window import _size_text, space_note
        self.assertEqual(_size_text(0), "0 B")
        self.assertEqual(_size_text(381 * 1024 ** 2), "381.0 MiB")
        self.assertEqual(_size_text(4 * 1024 ** 3), "4.0 GiB")
        self.assertIn("还剩 380.8 MiB", space_note("/userdata", 399319040))  # a6pro 真机那个数
        self.assertIn("未知", space_note("/userdata", None))
        self.assertIn("未知", space_note("/userdata", -1))

    def test_bundle_collects_what_exists_and_marks_the_rest_missing(self):
        """取证包（`42` §1 ③）：**纯读、只写宿主**；取不到的东西记成"缺失"，
        **不编一个空文件**充数（与 #30 同一条纪律）。

        跑在临时目录里，所以连 VM 都不用起。
        """
        import tempfile
        from vmgui.console import harness
        with tempfile.TemporaryDirectory() as d:
            dest, written, missing = harness.make_bundle(
                "a6pro", d, probe_values={"holder": "1:miniapp", "core": "3"},
                probe_ok="ok", serial_lines=40)
            self.assertEqual(dest, d)
            self.assertIn("probe.txt", written)
            self.assertIn("README.txt", written)
            self.assertIn("pack.toml", written)                 # a6pro 的包声明在仓库里
            self.assertTrue(os.path.exists(os.path.join(d, "README.txt")))
            with open(os.path.join(d, "README.txt"), encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("包：a6pro", txt)
            self.assertIn("不包含设备身份", txt)
            with open(os.path.join(d, "probe.txt"), encoding="utf-8") as f:
                pv = f.read()
            self.assertIn("core=3", pv)
            self.assertIn("# 通道=ok", pv)
            # 一个不存在的包 → 声明记成缺失，而不是凭空造一个 pack.toml
            _, w2, m2 = harness.make_bundle("__nopack__", d, serial_lines=40)
            self.assertTrue(any("pack.toml" in x for x in m2), m2)
            self.assertNotIn("pack.toml", w2)

    def test_escape_tap_point_uses_ratio_not_pixels(self):
        """`#74` 的应急键落点：**按比例**（绝对像素换族就废）。

        千问那两颗样本都在 s7pro 1020×240：`×`≈(44,42)、`<`≈(43,37) → 比例 4.2%/16%
        在这两块屏上都落在厂商那颗返回位附近。
        """
        from vmgui.console.window import ESCAPE_TAP_RATIO, escape_tap_point
        x, y = escape_tap_point(1020, 240)
        self.assertEqual((x, y), (43, 38))
        x2, y2 = escape_tap_point(560, 170)            # a6pro 的屏
        self.assertEqual((x2, y2), (24, 27))
        # 按包覆盖：填一行就生效，不必改算法
        ESCAPE_TAP_RATIO["__test_pack__"] = (0.1, 0.5)
        try:
            self.assertEqual(escape_tap_point(1000, 200, "__test_pack__"), (100, 100))
        finally:
            ESCAPE_TAP_RATIO.pop("__test_pack__", None)

    def test_push_file_reports_missing_source_without_touching_the_guest(self):
        from vmgui.console import harness
        rc, out = harness.DPCTL.push_file("/no/such/local/file.bin", "/tmp/x.bin")
        self.assertEqual(rc, 1)
        self.assertIn("no such file", out)

    def test_amr_target_keeps_the_original_name(self):
        """落点 = `/userdata/<原名>`：中文、空格照原样带过去，两种宿主分隔符都吃。

        这条不许"清洗"文件名 —— `put` 那条通道本来就是名字照原样的前提，装的时候换一个名，
        留档说明和回执就跟人对不上账。
        """
        from vmgui.console.window import amr_target
        self.assertEqual(amr_target(r"D:\download\vm\包 A.amr"), "/userdata/包 A.amr")
        self.assertEqual(amr_target("D:/download/vm/包 A.amr"), "/userdata/包 A.amr")
        self.assertEqual(amr_target(r"C:\x\y\miniapp.amr"), "/userdata/miniapp.amr")

    def test_install_quotes_the_remote_path_and_returns_the_command(self):
        """`install_miniapp`：路径引死、命令原样带回、guest 的失败**照原样**不许粉饰。

        `miniapp_cli` 在不在 PATH 没实测（VM 停着），所以这条只钉"发出去的是什么"，
        不钉"笔里有没有这个命令"。`not found` / `127` 要能被上层原样带进日志。
        """
        from vmgui.console import harness
        saved = harness.DPCTL.guest_run
        seen = {}

        def fake(cmd, timeout=60, force_serial=False):
            seen["cmd"] = cmd
            seen["timeout"] = timeout
            return 127, "sh: miniapp_cli: not found"
        harness.DPCTL.guest_run = fake
        try:
            rc, cmd, out = harness.install_miniapp("/userdata/中文 包.amr")
        finally:
            harness.DPCTL.guest_run = saved
        self.assertEqual(cmd, "miniapp_cli install '/userdata/中文 包.amr'")
        self.assertEqual(seen["cmd"], cmd)
        self.assertEqual(seen["timeout"], harness.MINIAPP_INSTALL_TIMEOUT_S)
        self.assertEqual(rc, 127)
        self.assertIn("not found", out)


class TestMachineOverrides(unittest.TestCase):
    """`vm/machine.toml` 这一层的纯逻辑：合并、拒绝、坏了不重写（Q67）。

    真机那半跑过一遍（`vm/_qwen_machine_write.py`，a6pro）：`内存 → 3G` 再 `vCPU → 2`
    两格都在、`abc` / `99` 被拒且文件一个字节没动、清空内存只删内存那一个键、测完恢复原样。
    这里钉同一批契约，用的是临时文件 —— **单测不许碰真的 `vm/machine.toml`**。
    """

    def setUp(self):
        import tempfile
        from vmgui.console import harness
        self.d = harness.DPCTL
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, True))
        self.path = os.path.join(self.td, "machine.toml")
        saved = self.d.MACHINE_FILE
        self.d.MACHINE_FILE = self.path
        self.addCleanup(setattr, self.d, "MACHINE_FILE", saved)

    def read(self) -> str:
        return self.read_b().decode("utf-8")

    def read_b(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def test_harness_forwards_only_the_named_key(self):
        """harness 那一层的**转发契约**：没点名的键不许出现在 kwargs 里。

        这条钉的是真 bug 的**发生处**（上一条 `test_machine_write_sends_only_the_named_key`
        钉的是界面只点名一格）。旧写法 `set_machine_override(pack, memory=memory, smp=smp)`
        把 `None` 一起送下去，而 dpctl 那边 `None`＝删键 —— 改一格抹两格。
        """
        from vmgui.console import harness
        seen = []
        saved = harness.DPCTL.set_machine_override

        def fake(pack, **kv):
            seen.append(kv)
            return {}, ""
        harness.DPCTL.set_machine_override = fake
        self.addCleanup(setattr, harness.DPCTL, "set_machine_override", saved)
        harness.set_machine("a6pro", smp="2")
        harness.set_machine("a6pro", memory="")
        harness.set_machine("a6pro")
        self.assertEqual(seen, [{"smp": "2"}, {"memory": ""}, {}])

    def test_parse_size_mb_reads_four_shapes_and_refuses_the_rest(self):
        """`2G` / `512M` / `2048`（裸数字＝MiB）/ `1024K` 都要认；认不出来回 **0**（调用方据此拒绝）。

        这一条是被 `1024K → 1024` 那版写法教出来的：单位分支少除一次，`-m` 就比人写的
        大 1024 倍，而界面上那个数**看着是对的**。
        """
        p = self.d.parse_size_mb
        self.assertEqual((p("2G"), p("512M"), p("2048"), p("1024K")), (2048, 512, 2048, 1))
        for bad in ("", "abc", "2 GB", "1.5G", "-1", "G", None, 0):
            self.assertEqual(p(bad), 0, repr(bad))

    def test_writing_one_key_never_drops_the_other(self):
        """「只改这一格，另一格不动」的那半句是**契约**，不是装饰。

        真 bug（实测抓的）：harness 无条件把 `memory=None` 转下来，而 dpctl 那侧 `None`
        ＝"删掉这个键" —— 按「改 vCPU」顺手把内存覆盖抹了。界面上写的和文件里留的不一样。
        """
        d = self.d
        self.assertEqual(d.set_machine_override("a6pro", memory="3G")[:2],
                         ({"memory": "3G"}, ""))
        eff, err = d.set_machine_override("a6pro", smp="2")
        self.assertEqual((eff, err), ({"memory": "3G", "smp": 2}, ""))
        raw = self.read()
        self.assertIn('memory = "3G"', raw)
        self.assertIn("smp = 2", raw)
        self.assertEqual(d.machine_plan("a6pro"),
                         ("3G", 2, {"memory": "machine.toml", "smp": "machine.toml"}, ""))

    def test_clearing_a_key_removes_only_that_key(self):
        """清空＝回到 runtime 默认，**只删点名的那一个**；两个都清 → 整节消失。"""
        d = self.d
        d.set_machine_override("a6pro", memory="3G", smp="2")
        self.assertEqual(d.set_machine_override("a6pro", memory="")[0], {"smp": 2})
        self.assertEqual(d.machine_plan("a6pro")[:2], ("2G", 2))
        self.assertEqual(d.set_machine_override("a6pro", smp=None)[0], {})
        self.assertNotIn("[a6pro]", self.read())

    def test_a_refused_value_leaves_the_file_byte_for_byte(self):
        """坏值不许落盘（也不许半途写坏）。理由里要带**出口**：该怎么写。"""
        d = self.d
        d.set_machine_override("a6pro", memory="3G")
        before = self.read_b()
        for kv, hint in (({"memory": "abc"}, "512M"), ({"smp": "0"}, "1~64"),
                         ({"smp": "99"}, "1~64"), ({"smp": "x"}, "整数"),
                         ({"npu": "1"}, "不认的机器参数")):
            eff, err = d.set_machine_override("a6pro", **kv)
            self.assertEqual(eff, {}, repr(kv))
            self.assertIn(hint, err)
            self.assertEqual(self.read_b(), before, repr(kv))

    def test_a_write_keeps_other_packs_sections(self):
        """这台改那台的节不许跟着没 —— 文件是**所有包共用的一份**。"""
        d = self.d
        d.set_machine_override("x6pro", memory="4G", smp="6")
        d.set_machine_override("a6pro", memory="3G")
        self.assertEqual(d.machine_overrides("x6pro"), {"memory": "4G", "smp": 6})
        self.assertEqual(d.machine_overrides("a6pro"), {"memory": "3G"})
        d.set_machine_override("x6pro", smp="2")
        self.assertEqual(d.machine_overrides("x6pro"), {"memory": "4G", "smp": 2})

    def test_a_broken_file_is_never_rewritten(self):
        """读不出来 → 整份不动（重写会把别的包一起丢掉），但**坏在哪儿要能看见**。

        「文件坏了就当成没覆盖」单独看是宽容，配上一条静默就变成"我明明改了怎么没生效"。
        所以 `machine_plan` 第 4 个返回值一路带到起机横幅和 `dpctl packs` 那一行。
        """
        d = self.d
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[a6pro\nmemory = \n")
        before = self.read_b()
        eff, err = d.set_machine_override("a6pro", memory="3G")
        self.assertEqual(eff, {})
        self.assertIn("读不出来", err)
        self.assertEqual(self.read_b(), before)
        self.assertEqual(d.machine_plan("a6pro")[3], err)
        self.assertIn("读不出来", d.machine_detail("a6pro"))

    def test_plan_says_who_decided_each_number(self):
        """两个读数要能各自说清是谁定的（runtime 默认 / machine.toml）—— 否则"改了没生效"永远说不清。"""
        d = self.d
        d.set_machine_override("a6pro", memory="3G")
        self.assertEqual(d.machine_plan("a6pro", None),
                         ("3G", 4, {"memory": "machine.toml", "smp": "runtime 默认"}, ""))
        self.assertEqual(
            d.machine_detail("a6pro"),
            "-m 3G -smp 4（内存=machine.toml、vCPU=runtime 默认；下次起机生效）")

    def test_grow_three_boundaries_and_the_resize2fs_followup(self):
        """扩状态盘：没盘不做、跑着不做、只增不减；成功那条**必须**提下一步 `resize2fs`。

        少提那一步就是"扩了但用不上"：`qemu-img resize` 只动宿主文件，`/state` 那个
        ext4（实测：整盘、无分区表）还停在旧大小。
        """
        import shutil
        import tempfile
        d = self.d
        td = tempfile.mkdtemp()
        img = os.path.join(td, "state-x6pro.img")
        saved_for, saved_run, saved_vm = d.state_img_for, d.subprocess, d.vm_running

        def restore():
            d.state_img_for, d.subprocess, d.vm_running = saved_for, saved_run, saved_vm
            shutil.rmtree(td, True)
        self.addCleanup(restore)
        d.state_img_for = lambda name: img

        rc, note = d.grow_state_image("x6pro", 1024)
        self.assertEqual(rc, 1)
        self.assertIn("还没有状态盘", note)

        with open(img, "wb"):
            pass
        d.vm_running = lambda: True
        rc, note = d.grow_state_image("x6pro", 1024)
        self.assertEqual(rc, 1)
        self.assertIn("先按「关机」", note)

        d.vm_running = lambda: False
        rc, note = d.grow_state_image("x6pro", 0)
        self.assertEqual(rc, 1)
        self.assertIn("只许变大", note)
        self.assertIn("重建状态盘", note)

        seen = {}

        class Proc:
            returncode = 0
            stdout = b"Image resized."
            stderr = b""

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            os.truncate(img, int(str(argv[-1]).rstrip("M")) * 1024 * 1024)
            return Proc()

        d.subprocess = type("M", (), {"run": staticmethod(fake_run)})
        rc, note = d.grow_state_image("x6pro", 4)
        self.assertEqual(rc, 0)
        # `-f raw` 必须在 `resize` **之后**（实测放前面 → `unknown option -- -f`，rc=1）。
        # 不写它也能成功，但 qemu-img 会 probe 格式并警告"写 block 0 会被限制"——
        # 这条警告 2026-10-19 真机 512→1024 那趟**原样出现在回执里**，不是假想。
        self.assertEqual(seen["argv"][1:], ["resize", "-f", "raw", img, "4M"])
        self.assertIn("盘文件 0 → 4 MiB", note)
        self.assertIn("resize2fs /dev/vdb", note)


class TestSerialTail(unittest.TestCase):
    """串口尾巴的两种"找不到标记"（Q38 与 Q48b）—— 真机撞到过一次，必须钉住。"""

    def setUp(self):
        import tempfile
        from vmgui.console import harness
        self.h = harness
        self.tmp = tempfile.mkdtemp(prefix="serial-")
        self.saved = harness.SERIAL_LOG
        self.addCleanup(self._restore)

    def _restore(self):
        self.h.SERIAL_LOG = self.saved
        self.h._BOOT_SEG.update(offset=-1, size=0)

    def _write(self, text: str) -> int:
        path = os.path.join(self.tmp, "serial.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.h.SERIAL_LOG = path
        self.h._BOOT_SEG.update(offset=-1, size=0)
        return os.path.getsize(path)

    def test_marker_far_behind_still_gives_content(self):
        """标记落在窗口之外 ≠ 本次开机没开始。日志长到 1.8 MB 时旧写法会整段返回空。

        真机表现：串口页与空态正文**全空**，取证包报"串口尾缺失"。
        """
        self._write("Booting Linux on physical CPU 0x0\n" + "x" * (600 * 1024)
                    + "\nlast line here\n")
        tail = self.h.serial_tail(5)
        self.assertIn("last line here", tail)

    def test_no_marker_at_all_is_empty(self):
        """全文件都没有标记 = 本次开机还没开始 → **必须空**（Q38：不许放行旧 needle）。"""
        self._write("old boot noise\n" * 100)
        self.assertEqual(self.h.serial_tail(5), "")

    def test_marker_inside_window_starts_there(self):
        self._write("old stuff that must not show\n" * 10
                    + "Booting Linux on physical CPU 0x0\nnew\n")
        tail = self.h.serial_tail(20)
        self.assertIn("new", tail)
        self.assertNotIn("old stuff", tail)

    def test_dpctl_child_is_forced_to_utf8(self):
        """`_argv` 必须带 `-X utf8`：子进程默认按宿主码页（这台 cp936）写自己的 stdout，
        而 `run_dpctl` 按 utf-8 解 —— 实测 a7pro 上 `dpctl sh --serial` 撞上回显里的
        UTF-8 中文就抛 `UnicodeEncodeError`，**rc=1、一个字节都不回**（「命令」页只剩 traceback）。
        """
        from vmgui.console import harness
        argv = harness._argv(["sh", "-c", "uname -a"])
        i = argv.index("-X")
        self.assertEqual(argv[i + 1], "utf8")
        script = next(k for k, a in enumerate(argv)
                      if os.path.basename(str(a)) == "dpctl.py")
        self.assertLess(i, script)        # 旗标要在脚本名之前才算喂给解释器


class TestConfig(unittest.TestCase):
    def test_deep_merge(self):
        a = {"x": {"y": 1, "z": 2}}
        b = {"x": {"z": 3, "w": 4}}
        self.assertEqual(deep_merge(a, b), {"x": {"y": 1, "z": 3, "w": 4}})

    def test_path_helpers(self):
        d: dict = {}
        set_path(d, "a.b.c", 5)
        self.assertEqual(get_path(d, "a.b.c"), 5)
        self.assertIsNone(get_path(d, "a.b.d"))

    def test_precedence_cli_over_profile_over_default(self):
        cfg = Config.load(cli_overrides={"display": {"mode": "stretch"}},
                          profiles=["pear-a6pro"], user_path=os.devnull)
        self.assertEqual(cfg.get("display.mode"), "stretch")
        self.assertEqual(cfg.source_of("display.mode"), "cli")
        self.assertEqual(cfg.get("input.touch_button"), 330)
        self.assertEqual(cfg.source_of("input.touch_button"), "profile:pear-a6pro")
        self.assertEqual(cfg.source_of("connection.uri"), "default")

    def test_user_layer_beats_default_but_loses_to_cli(self):
        cfg = Config.load(user_path=os.devnull)
        cfg.set("display.scale_algo", "bilinear")
        self.assertEqual(cfg.get("display.scale_algo"), "bilinear")
        self.assertEqual(cfg.source_of("display.scale_algo"), "user")
        cfg.reset("display.scale_algo")
        self.assertEqual(cfg.get("display.scale_algo"), DEFAULTS["display"]["scale_algo"])
        self.assertEqual(cfg.source_of("display.scale_algo"), "default")

    def test_validation_flags_bad_values(self):
        cfg = Config.load(user_path=os.devnull)
        cfg.set("display.rotation", 45)
        self.assertTrue(any("display.rotation" in w for w in cfg.warnings))

    def test_crop_requires_size(self):
        cfg = Config.load(user_path=os.devnull)
        cfg.set("display.crop.enabled", True)
        cfg.set("display.crop.w", 0)
        self.assertTrue(any("display.crop" in w for w in cfg.warnings))


class TestMapper(unittest.TestCase):
    def test_roundtrip_all_rotations_fills_widget_exactly(self):
        gw, gh = 560, 170
        for rot in (0, 90, 180, 270):
            rot_w, rot_h = (gh, gw) if rot in (90, 270) else (gw, gh)
            t = mapper.build_transform(rot_w, rot_h, gw, gh, mode="1:1", rotation=rot)
            self.assertEqual((t.sx, t.sy), (1.0, 1.0))
            self.assertEqual((t.ox, t.oy), (0.0, 0.0))
            for gx, gy in [(0, 0), (10, 20), (gw - 1, gh - 1), (280, 85)]:
                s = mapper.guest_to_screen(gx, gy, t)
                self.assertIsNotNone(s)
                self.assertEqual(mapper.screen_to_guest(s[0], s[1], t), (gx, gy),
                                 f"rot={rot} guest=({gx},{gy})")

    def test_crop_declaration_with_offset_shows_panel_corner(self):
        """#35：裁剪原点**恒为 0**（面板永远取左上角），声明里的 x/y 不参与。

        旧行为（原点照抄声明、按 `帧-原点` 钳制）已经随 `35-mapper-shadowed-copy.md`
        里那份被遮蔽的副本一起删掉了。
        """
        crop = {"enabled": True, "x": 10, "y": 5, "w": 100, "h": 60}
        t = mapper.build_transform(100, 60, 560, 170, mode="1:1", crop=crop)
        self.assertEqual((t.content_w, t.content_h), (100, 60))
        self.assertEqual((t.crop_x, t.crop_y), (0, 0))
        self.assertEqual(mapper.screen_to_guest(0, 0, t), (0, 0))
        s = mapper.guest_to_screen(50, 30, t)
        self.assertEqual(mapper.screen_to_guest(s[0], s[1], t), (50, 30))
        # 裁剪区域外的点不在显示范围内
        self.assertIsNone(mapper.guest_to_screen(200, 30, t))

    def test_crop_clamped_to_frame(self):
        crop = {"enabled": True, "x": 500, "y": 160, "w": 1000, "h": 1000}
        t = mapper.build_transform(100, 100, 560, 170, mode="1:1", crop=crop)
        self.assertEqual((t.content_w, t.content_h, t.crop_x, t.crop_y), (560, 170, 0, 0))

    def test_fit_letterboxes_and_centers(self):
        t = mapper.build_transform(1000, 1000, 560, 170, mode="fit")
        self.assertAlmostEqual(t.sx, 1000 / 560, places=6)
        self.assertAlmostEqual(t.disp_h, 170 * (1000 / 560), places=4)
        self.assertAlmostEqual(t.ox, 0.0, places=6)
        self.assertAlmostEqual(t.oy, (1000 - t.disp_h) / 2, places=6)
        gx, gy = mapper.screen_to_guest(500, 500, t)      # 画布正中 -> guest 正中
        self.assertAlmostEqual(gx, 280, delta=1)
        self.assertAlmostEqual(gy, 85, delta=1)

    def test_fit_with_ignore_aspect_becomes_stretch(self):
        keep = mapper.build_transform(800, 170, 560, 170, mode="fit", aspect="keep")
        self.assertAlmostEqual(keep.sx, keep.sy, places=9)
        self.assertAlmostEqual(keep.ox, (800 - 560 * keep.sx) / 2, places=6)
        ig = mapper.build_transform(800, 170, 560, 170, mode="fit", aspect="ignore")
        self.assertAlmostEqual(ig.sx, 800 / 560, places=6)
        self.assertAlmostEqual(ig.sy, 1.0, places=6)
        self.assertNotAlmostEqual(ig.sx, ig.sy, places=3)

    def test_stretch_fills_widget(self):
        t = mapper.build_transform(800, 200, 560, 170, mode="stretch")
        self.assertEqual((t.ox, t.oy), (0.0, 0.0))
        self.assertAlmostEqual(t.disp_w, 800, places=6)
        self.assertAlmostEqual(t.disp_h, 200, places=6)
        self.assertEqual(mapper.screen_to_guest(799, 199, t), (559, 169))
        self.assertEqual(mapper.screen_to_guest(0, 0, t), (0, 0))

    def test_free_uses_zoom(self):
        t = mapper.build_transform(1000, 1000, 560, 170, mode="free", zoom=3.0)
        self.assertEqual((t.sx, t.sy), (3.0, 3.0))

    def test_rotation_swaps_displayed_size(self):
        t = mapper.build_transform(1000, 1000, 560, 170, mode="fit", rotation=90)
        self.assertEqual((t.rot_w, t.rot_h), (170, 560))
        self.assertAlmostEqual(t.sx, min(1000 / 170, 1000 / 560), places=6)

    def test_inside_helper(self):
        t = mapper.build_transform(560, 170, 560, 170, mode="1:1")
        self.assertTrue(mapper.inside(10, 10, t))
        self.assertFalse(mapper.inside(561, 10, t))
        self.assertFalse(mapper.inside(10, -1, t))

    def test_bad_rotation_falls_back_to_zero(self):
        t = mapper.build_transform(100, 100, 20, 10, mode="1:1", rotation=45)
        self.assertEqual(t.rotation, 0)

    def test_fit_returns_exact_z_when_box_is_frame_times_z(self):
        # `32-first-frame-sizing.md` §0 的那条数学巧合：盒子恰为 帧×z 时 fit 正返回 z
        t = mapper.build_transform(1120, 340, 560, 170, mode="fit")
        self.assertAlmostEqual(t.sx, 2.0, places=6)
        self.assertAlmostEqual(t.sy, 2.0, places=6)
        self.assertAlmostEqual(t.ox, 0.0, places=6)
        self.assertAlmostEqual(t.oy, 0.0, places=6)


class TestInitialScale(unittest.TestCase):
    """`32-first-frame-sizing.md` §3 的七包落点表（宿主 1920x1080，画布可用区 1200x620）。"""

    def test_pack_table(self):
        avail = (1200, 620)
        for (fw, fh), want in {(560, 170): 2.0, (640, 172): 1.5, (640, 260): 1.5,
                               (1020, 240): 1.0, (800, 254): 1.5, (960, 480): 1.0,
                               (960, 266): 1.0}.items():
            self.assertEqual(scaling.initial_scale(fw, fh, *avail), want, f"{fw}x{fh}")

    def test_below_one_falls_back_to_plain_fit(self):
        z = scaling.initial_scale(1020, 240, 800, 300)     # 1366x768 宿主上的 s7pro
        self.assertLess(z, 1.0)
        self.assertAlmostEqual(z, min(800 / 1020, 300 / 240), places=9)

    def test_degenerate_inputs_are_one(self):
        self.assertEqual(scaling.initial_scale(0, 240, 1200, 620), 1.0)
        self.assertEqual(scaling.initial_scale(960, 480, 0, 0), 1.0)

    def test_capped_at_three(self):
        self.assertEqual(scaling.initial_scale(100, 100, 4000, 4000), 3.0)


class TestScaling(unittest.TestCase):
    def test_to_qimage_from_frame(self):
        frame = Frame(pixels=bytes(4 * 2 * 3), width=4, height=2, stride=12, fmt="rgb888")
        img = scaling.to_qimage(frame)
        self.assertEqual((img.width(), img.height()), (4, 2))

    def test_unsupported_format_raises(self):
        frame = Frame(pixels=b"", width=1, height=1, stride=1, fmt="nope")
        with self.assertRaises(ValueError):
            scaling.to_qimage(frame)

    def test_scale_and_rotate(self):
        frame = Frame(pixels=bytes(4 * 2 * 3), width=4, height=2, stride=12)
        img = scaling.to_qimage(frame)
        self.assertEqual((scaling.scale_image(img, 8, 4).width(),
                          scaling.scale_image(img, 8, 4).height()), (8, 4))
        rot = scaling.rotate_image(img, 90)
        self.assertEqual((rot.width(), rot.height()), (2, 4))
        self.assertIs(scaling.rotate_image(img, 0), img)

    def test_algo_names(self):
        self.assertEqual(set(scaling.algo_names()), {"nearest", "bilinear", "lanczos"})


class TestPointerMode(unittest.TestCase):
    def test_auto_picks_absolute_when_available(self):
        self.assertEqual(PointerMode.resolve("auto", Capabilities(absolute_pointer=True)),
                         "absolute")
        self.assertEqual(PointerMode.resolve("auto", Capabilities()), "relative")

    def test_explicit_wins_but_degrades_when_unsupported(self):
        self.assertEqual(PointerMode.resolve("relative", Capabilities(absolute_pointer=True)),
                         "relative")
        self.assertEqual(PointerMode.resolve("absolute", Capabilities(absolute_pointer=True)),
                         "absolute")
        self.assertEqual(PointerMode.resolve("absolute", Capabilities()), "relative")


class TestKeymap(unittest.TestCase):
    def test_letters_and_specials(self):
        from vmgui.input.keymap import KeyMapper
        km = KeyMapper("us")
        self.assertEqual(km.code_for(0, "a"), 30)
        self.assertEqual(km.code_for(0, "1"), 2)
        self.assertEqual(km.code_for(0, " "), 57)
        from PySide6.QtCore import Qt
        self.assertEqual(km.code_for(Qt.Key_Escape), 1)
        self.assertEqual(km.code_for(Qt.Key_Return), 28)

    def test_de_layout_swaps_y_and_z(self):
        from vmgui.input.keymap import KeyMapper
        km = KeyMapper("de")
        self.assertEqual(km.code_for(0, "z"), 21)
        self.assertEqual(km.code_for(0, "y"), 44)

    def test_unknown_layout_falls_back_to_us(self):
        from vmgui.input.keymap import KeyMapper
        self.assertEqual(KeyMapper("klingon").layout, "us")

    def test_unknown_key_returns_none(self):
        from vmgui.input.keymap import KeyMapper
        self.assertIsNone(KeyMapper("us").code_for(0x0BADF00D))


class TestPackSwitcher(unittest.TestCase):
    """「设备」下拉（人点名：控制台没有切换设备选项）—— 只验接线，不碰真 VM。

    三条硬规矩各一条测试，都是这次实现里**差点写歪**的地方：
    **①** 换目标要一起换掉**按包算**的读数，尤其旧那台的**客机**值 —— 留着就是
    「下次 2G（新包）｜客机看到 1.93 GiB（旧那台）」，两个数不是同一台机器（#30）。
    **②** 菜单每 5 s 被探针敲一次，**没变就不许重建**（这条 UI 线程为"每帧重排"付过两次账）。
    **③** VM 在跑的时候不许静默换目标：确认框默认键是「取消」，同意后**只关机、不起机**，
    而且**关机没成就不换**（关不成却把界面切成另一台 = 一屏假读数）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    PACKS = [
        {"name": "x6pro", "ready": True, "panel": "960x480", "soc": "rk3562",
         "runtime": "rockchip-linux-ext4", "missing": [], "md5_bad": []},
        {"name": "x7", "ready": True, "panel": "960x266", "soc": "rv1106",
         "runtime": "rv1106-linux-erofs", "missing": [], "md5_bad": []},
        {"name": "a6pro", "ready": False, "panel": "560x170", "soc": "rv1106",
         "runtime": "rv1106-linux-erofs",
         "missing": ["system_a.img 不在"], "md5_bad": []},
    ]

    def _window(self, pack="x6pro", current="x6pro"):
        from vmgui.console import window as W
        from vmgui.console.window import ConsoleWindow
        # 「没有目标」这个形状必须在**构造之前**钉住：`__init__` 里写的是
        # `pack or harness.current_pack()`，构造完再打桩就晚了 —— 窗口已经从盘上
        # 读到上次起过的那台（`vm/current.pack`），"没目标"的夹具其实带着目标。
        saved_current = W.harness.current_pack
        W.harness.current_pack = lambda: current
        self.addCleanup(setattr, W.harness, "current_pack", saved_current)
        cfg = Config.load(cli_overrides={
            "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                           "reconnect": {"enabled": False}},
            "display": {"frame_poll_ms": 200}}, profiles=[], user_path=os.devnull)
        win = ConsoleWindow(cfg, Session(cfg), pack)
        win._probe_timer.stop()
        win._serial_timer.stop()
        win._rate_timer.stop()
        # 探针线程在离屏测试里就是"去戳真机器"（`_window` 上面停的三个 timer 同一条理由），
        # 换成记账用的桩：断言"该敲的时候敲了"，而不是真起一个线程。
        win._kicks = []
        win._kick_probe = lambda: win._kicks.append(1)
        self.addCleanup(win.deleteLater)
        return win

    def _feed(self, win, packs=None, running=False):
        from vmgui.core import rules as R
        sig = R.Signals(packs=packs if packs is not None else self.PACKS,
                        packs_known=True, pack=win.pack or "", vm_running=running,
                        frames_ok=False, probe_ok="no-shell", probe={})
        win._on_decision((sig, None))
        return sig

    def _labels(self, win):
        return [(a.text(), a.isEnabled(), a.isChecked()) for a in win.pack_menu.actions()]

    def test_menu_lists_packs_checks_current_and_greys_missing(self):
        """#28 的老规矩在新入口上的形状：**缺镜像的那台在菜单里就选不动**，不是点了才报错。"""
        win = self._window()
        self._feed(win)
        self.assertEqual(win.btn_pack.text(), "设备 x6pro")
        labels = self._labels(win)
        self.assertIn(("x6pro  960x480", True, True), labels)
        self.assertIn(("x7  960x266", True, False), labels)
        self.assertIn(("a6pro  不可用", False, False), labels)
        bad = [a for a in win.pack_menu.actions() if not a.isEnabled()][0]
        self.assertIn("system_a.img 不在", bad.toolTip())   # packlib 原文，不自己造文案

    def test_menu_rebuilds_only_when_the_signature_changes(self):
        """探针 5 s 一轮，每轮都走 `_on_decision` → 不设闸门就是每轮 new 三个 QAction。"""
        win = self._window()
        self._feed(win)
        clears = []
        orig = win.pack_menu.clear
        win.pack_menu.clear = lambda: clears.append(1) or orig()
        self._feed(win)
        self._feed(win)
        self.assertEqual(clears, [], "清单和选中项都没变，菜单不该被重建")
        changed = [dict(p) for p in self.PACKS]
        changed[2]["ready"] = True                      # 补完镜像：那行要从灰变亮
        changed[2]["missing"] = []
        self._feed(win, packs=changed)
        self.assertEqual(len(clears), 1)
        self.assertIn(("a6pro  560x170", True, False), self._labels(win))

    def test_switch_while_stopped_moves_target_and_drops_old_live_numbers(self):
        from vmgui.console import window as W
        win = self._window()
        self._feed(win)
        saved = W.harness.vm_running
        W.harness.vm_running = lambda: False
        self.addCleanup(setattr, W.harness, "vm_running", saved)
        win._m_live = {"mem_mib": 1983, "cpu": 4, "fs_mib": 500}   # 旧那台（x6pro）的实测值
        win._pack_chosen("x7")
        self.assertEqual(win.pack, "x7")
        self.assertIn("x7", win.windowTitle())
        self.assertEqual(win._m_live, {}, "旧那台的客机读数不能留给新目标作证（#30）")
        self.assertIn("x7", win.lbl_status.text())
        self.assertEqual(len(win._kicks), 1, "换完目标要立刻重采一轮（「启动」的可用性跟着包走）")
        self.assertIn("目标已切换为 x7", win.cmdlog.toPlainText())
        self.assertIn(("x7  960x266", True, True), self._labels(win))

    def test_clicking_current_device_does_nothing(self):
        """打勾的那行本来就在管着 —— 点它不该写日志、不该重采、不该弹框。"""
        from vmgui.console import window as W
        win = self._window()
        self._feed(win)
        saved = W.harness.vm_running
        W.harness.vm_running = lambda: True
        self.addCleanup(setattr, W.harness, "vm_running", saved)
        win._pack_chosen("x6pro")
        self.assertEqual(win._kicks, [])
        self.assertEqual(win._pending_pack, "")
        self.assertNotIn("目标已切换为", win.cmdlog.toPlainText())
        self.assertNotIn("操作已取消", win.cmdlog.toPlainText())

    def test_switch_while_running_needs_confirm_then_only_stops(self):
        """同意之后**只做关机**：起机是人的决定（#71「看得见两步」，起机还要 3~6 分钟）。

        桩线程是**同步**跑的，所以 `_on_dpctl` 在这一行里就已经收到回执 ——
        断言看的是"回执来了之后换成了 x7，而且只起过一趟线程（关机）"。
        """
        from vmgui.console import window as W
        win = self._window()
        self._feed(win)
        runs, threads = self._patch_sync(win)
        self.addCleanup(setattr, W.harness, "vm_running", runs[0])
        self.addCleanup(setattr, W.harness, "current_pack", runs[1])
        self.addCleanup(setattr, W.harness, "stop_vm", runs[2])
        self.addCleanup(setattr, W, "Thread", runs[3])
        self.addCleanup(setattr, W, "QMessageBox", runs[4])
        stopped = []
        W.harness.stop_vm = lambda force=False: stopped.append(force) or (0, "poweroff ok")
        Box = _make_box("ok")
        W.QMessageBox = Box
        win._pack_chosen("x7")
        self.assertEqual(stopped, [False], "该请客机自己 poweroff，不是拔插头")
        self.assertEqual(len(threads), 1, "确认之后只该起「关机」那一趟，不许顺手起机")
        self.assertEqual(win.pack, "x7")
        self.assertEqual(win._pending_pack, "")
        self.assertIn("关机：rc=0", win.cmdlog.toPlainText())
        self.assertIn("目标已切换为 x7", win.cmdlog.toPlainText())
        self.assertEqual(Box.default, "取消", "默认键不能落在「停掉正在看的这台」上（#15/#71）")
        self.assertIn("x6pro", Box.informative)
        self.assertIn("x7", Box.informative)

    def test_confirm_cancel_keeps_everything(self):
        from vmgui.console import window as W
        win = self._window()
        self._feed(win)
        runs, _threads = self._patch_sync(win)
        self.addCleanup(setattr, W.harness, "vm_running", runs[0])
        self.addCleanup(setattr, W.harness, "current_pack", runs[1])
        self.addCleanup(setattr, W.harness, "stop_vm", runs[2])
        self.addCleanup(setattr, W, "QMessageBox", runs[4])
        called = []
        W.harness.stop_vm = lambda force=False: called.append(1) or (0, "")
        Box = _make_box("cancel")
        W.QMessageBox = Box
        win._pack_chosen("x7")
        self.assertEqual(win.pack, "x6pro")
        self.assertEqual(called, [], "按「取消」绝不能顺手把正在看的这台关掉")
        self.assertIn("操作已取消", win.cmdlog.toPlainText())
        self.assertEqual(Box.default, "取消")

    def test_failed_poweroff_does_not_switch_target(self):
        from vmgui.console import window as W
        win = self._window()
        self._feed(win)
        runs, _threads = self._patch_sync(win)
        self.addCleanup(setattr, W.harness, "vm_running", runs[0])
        self.addCleanup(setattr, W.harness, "current_pack", runs[1])
        self.addCleanup(setattr, W.harness, "stop_vm", runs[2])
        self.addCleanup(setattr, W, "Thread", runs[3])
        self.addCleanup(setattr, W, "QMessageBox", runs[4])
        W.harness.stop_vm = lambda force=False: (1, "guest 不认")
        W.QMessageBox = _make_box("ok")
        win._pack_chosen("x7")
        self.assertEqual(win.pack, "x6pro", "没关掉就切成 x7，那一屏读数全是假的")
        self.assertEqual(win._pending_pack, "", "半截状态不能留着，下一轮回执会把它当成新指令")
        self.assertIn("关机失败", win.cmdlog.toPlainText())

    def test_start_without_a_target_opens_the_menu_instead_of_nagging_cli(self):
        """旧文案让人回命令行 `--pack` —— 在这个界面里那等于"这条路没有"。"""
        win = self._window(pack="", current="")
        self._feed(win)
        self.assertEqual(win.pack, "")
        shown = []
        win.btn_pack.showMenu = lambda: shown.append(1)
        win._start_clicked()
        self.assertEqual(shown, [1], "该把「设备」菜单展开，而不是把人支去命令行")
        self.assertIn("请先在「设备」中选择", win.cmdlog.toPlainText())
        self.assertNotIn("--pack", win.cmdlog.toPlainText())

    def test_start_with_only_one_ready_pack_picks_it_and_says_so(self):
        from vmgui.console import window as W
        win = self._window(pack="", current="")      # current=""：这台盘上从没起过机
        one = [dict(self.PACKS[1])]                    # 只有 x7 就位
        one[0]["ready"] = True
        self._feed(win, packs=one)
        runs, threads = self._patch_sync(win)
        self.addCleanup(setattr, W.harness, "vm_running", runs[0])
        self.addCleanup(setattr, W.harness, "current_pack", runs[1])
        self.addCleanup(setattr, W.harness, "stop_vm", runs[2])
        self.addCleanup(setattr, W, "Thread", runs[3])
        self.addCleanup(setattr, W, "QMessageBox", runs[4])
        W.harness.start_vm = lambda pack, fresh=False, **k: (0, f"up {pack}")
        win._start_clicked()
        self.assertEqual(win.pack, "x7")
        self.assertIn("仅 x7 可用", win.cmdlog.toPlainText())

    def test_switch_clears_the_old_machines_view_state(self):
        """换目标时：旧那台的**最后一帧**、缩放档、被拖过的窗口形状都要一起收掉。

        人 2026-10-20 的猜想：「快捷换设备可能会导致显示问题」。不收拾这三样，新设备名字下面
        挂着的就是旧机器的画面（再叠上旧面板的裁切线/旧窗口比例），看着像被裁、像错位。
        """
        win = self._window()
        self._feed(win)
        win.canvas.set_frame(Frame(pixels=b"\x00" * (960 * 266 * 3),
                                   width=960, height=266, stride=960 * 3))
        self.assertTrue(win.canvas.has_frame)
        win._set_zoom(2.0)
        win._user_sized = True
        win._apply_pack("x7", announce=False)
        self.assertFalse(win.canvas.has_frame, "旧那台的最后一帧不许挂在新名字下面")
        self.assertIsNone(win._zoom, "缩放档是给旧那台的画面挑的")
        self.assertFalse(win._user_sized, "窗口形状也属于旧面板：新机第一帧要按它自己套")
        self.assertIn("适合", win.btn_zoom.text())
        # 清帧/换标题会带出一个"意料之外"的 resize，把 `_user_sized` 又置回 True ——
        # 所以重新套窗口这件事要绑在**下一帧**上（`_refit_next_frame`）。
        self.assertTrue(win._refit_next_frame)
        win._user_sized = True
        win._on_frame(Frame(pixels=b"\x00" * (960 * 266 * 3),
                            width=960, height=266, stride=960 * 3))
        self.assertFalse(win._user_sized, "换设备后的第一帧要按新面板重套窗口")
        self.assertFalse(win._refit_next_frame)

    def test_empty_state_names_the_target_device(self):
        """空态正文要写清"现在管的是哪台"：下面那格串口尾是**上一次开机**的（可能是别的包），
        刚换完设备时不写目标，人会以为画面/日志都是新机器的。"""
        win = self._window()
        self._feed(win, running=False)
        _title, body, _actions = win._empty_content(win._decision)
        self.assertIn("目标设备", body)
        self.assertIn("x6pro", body)

    def test_f11_toggles_fullscreen_instead_of_pressing_the_pen(self):
        """人点名「控制台全屏显示有问题」：以前 F11 落到 keymap 上（它映射成客机键码 87），
        按下去只是把 F11 塞给笔 —— 笔不认，人看到的就是"全屏没反应"。"""
        win = self._window()
        sent = []
        win.session.key = lambda code, down: sent.append(code)
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F11, Qt.NoModifier))
        self.assertTrue(win.isFullScreen(), "F11 该让控制台全屏")
        self.assertNotIn(87, sent, "F11 不能再发给客机")
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertFalse(win.isFullScreen(), "全屏下 Esc 要能退出来")
        win.keyReleaseEvent(QKeyEvent(QEvent.KeyRelease, Qt.Key_F11, Qt.NoModifier))
        self.assertNotIn(87, sent, "F11 的**抬起**也不能发给笔（孤立的 key-up）")
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a"))
        self.assertTrue(sent, "普通键仍要照旧发给客机")
        self.assertNotIn(87, sent)

    def _patch_sync(self, win):
        """把 `window` 模块里的 Thread / harness 三个函数与 QMessageBox 换成桩，
        返回 `(旧值列表, 线程里真正跑过的目标名)`。桩线程**同步执行**，
        于是 `emit` 直接落到 `_on_dpctl`，断言不用等真线程。"""
        from vmgui.console import window as W
        old = (W.harness.vm_running, W.harness.current_pack, W.harness.stop_vm,
               W.threading.Thread, W.QMessageBox)
        ran = []

        class _Sync:
            """生产写法是 `Thread(target=…, args=(pack,), daemon=True)` ——
            `args` 不转发就等于把「启动」那趟的包名丢了（2026-10-19 就丢过一次）。"""

            def __init__(self, target=None, args=(), kwargs=None, **_ignored):
                self._t, self._a, self._k = target, tuple(args), dict(kwargs or {})

            def start(self):
                ran.append("thread")
                if self._t:
                    self._t(*self._a, **self._k)

            def is_alive(self):
                return False

        W.threading.Thread = _Sync
        W.harness.vm_running = lambda: True
        W.harness.current_pack = lambda: "x6pro"
        return old, ran


class TestConsoleKeys(unittest.TestCase):
    """实体键工具条（Q88/Q90）：只验接线、命令形状与"按包亮灰"，不起真 VM。

    播放键 164 **故意不在**工具条里：厂商 `hal_key_config.json` 的 RK3562_Melon_V0
    没有 play_key，固件连播放模块都不建（实测按下零回调）。一颗按不动的键不如没有。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, pack="x6pro", current="x6pro"):
        from vmgui.console import window as W
        from vmgui.console.window import ConsoleWindow
        saved_current = W.harness.current_pack
        W.harness.current_pack = lambda: current
        self.addCleanup(setattr, W.harness, "current_pack", saved_current)
        cfg = Config.load(cli_overrides={
            "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                           "reconnect": {"enabled": False}},
            "display": {"frame_poll_ms": 200}}, profiles=[], user_path=os.devnull)
        win = ConsoleWindow(cfg, Session(cfg), pack)
        win._probe_timer.stop()
        win._serial_timer.stop()
        win._rate_timer.stop()
        win._kick_probe = lambda: None
        self.addCleanup(win.deleteLater)
        return win

    def _patch_send(self, win, running=True):
        from vmgui.console import window as W
        old = (W.harness.vm_running, W.harness.run_dpctl, W.threading.Thread)
        calls, notes = [], []
        win._note = lambda msg: notes.append(msg)

        class _Sync:
            def __init__(self, target=None, args=(), kwargs=None, **_ignored):
                self._t, self._a, self._k = target, tuple(args), dict(kwargs or {})

            def start(self):
                if self._t:
                    self._t(*self._a, **self._k)

            def is_alive(self):
                return False

        def _run(argv, **kw):
            calls.append((list(argv), dict(kw)))
            return 0, "SENT"

        W.harness.vm_running = lambda: running
        W.harness.run_dpctl = _run
        W.threading.Thread = _Sync
        self.addCleanup(setattr, W.harness, "vm_running", old[0])
        self.addCleanup(setattr, W.harness, "run_dpctl", old[1])
        self.addCleanup(setattr, W.threading, "Thread", old[2])
        return calls, notes

    def test_toolbar_has_home_and_volume_only(self):
        win = self._window()
        self.assertEqual([a.text() for a in win._key_actions.values()],
                         ["Home", "音量－", "音量＋"])
        self.assertTrue(all(a.isEnabled() for a in win._key_actions.values()),
                        "x6pro 的 vm-keys.env 三颗码都有，应该全亮")
        self.assertNotIn("播放", [a.text() for a in win._key_actions.values()])

    def test_keys_follow_pack_capability(self):
        from vmgui.console import harness as H
        self.assertEqual(H.keys_for("x6pro"), {"home": 102, "vol_down": 114, "vol_up": 115})
        self.assertEqual(H.keys_for("s6pro"), {"home": 139, "vol_down": None, "vol_up": None})
        self.assertEqual(H.keys_for("x7"), {"home": None, "vol_down": None, "vol_up": None})
        win = self._window()
        win._apply_pack("s6pro", announce=False)
        self.assertTrue(win._key_actions["home"].isEnabled())
        self.assertFalse(win._key_actions["vol_down"].isEnabled(),
                         "s6pro 的 key config 只有 home/head/power，音量键该灰")
        win._apply_pack("x7", announce=False)
        self.assertFalse(any(a.isEnabled() for a in win._key_actions.values()),
                         "x7 没有按键节，三颗都该灰")
        win._apply_pack("x6pro", announce=False)
        self.assertTrue(all(a.isEnabled() for a in win._key_actions.values()))

    def test_send_key_writes_the_guest_fifo(self):
        win = self._window()
        calls, _notes = self._patch_send(win, running=True)
        win._send_key("home")
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertIn("[ -p /tmp/fake-keys.fifo ]", argv[2],
                      "节点不在时要报出来，不能闷头建一个普通文件")
        self.assertIn("echo 102 > /tmp/fake-keys.fifo", argv[2])
        self.assertEqual(kw.get("timeout"), 20)

    def test_send_key_uses_pack_home_code(self):
        win = self._window(pack="s6pro", current="s6pro")
        calls, notes = self._patch_send(win, running=True)
        win._send_key("home")
        self.assertIn("echo 139 > /tmp/fake-keys.fifo", calls[0][0][2],
                      "s6pro 的 Home 是 139，不能沿用 x6pro 的 102")
        calls2, notes2 = self._patch_send(win, running=True)
        win._send_key("vol_down")
        self.assertEqual(calls2, [], "s6pro 没有音量键，不该发命令")
        self.assertTrue(notes2 and "音量减" in notes2[0] and "未配置" in notes2[0])

    def test_send_key_with_vm_down_only_notes(self):
        win = self._window()
        calls, notes = self._patch_send(win, running=False)
        win._send_key("vol_down")
        self.assertEqual(calls, [], "VM 没跑就不该往客机发命令")
        self.assertTrue(notes and "虚拟机未运行" in notes[0])


class TestConsoleFast(unittest.TestCase):
    """快速启动按钮（Q93）：快照状态、只停机可存、快启走 `up --fast`。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, pack="x6pro", current="x6pro"):
        from vmgui.console import window as W
        from vmgui.console.window import ConsoleWindow
        saved = W.harness.current_pack
        W.harness.current_pack = lambda: current
        self.addCleanup(setattr, W.harness, "current_pack", saved)
        cfg = Config.load(cli_overrides={
            "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                           "reconnect": {"enabled": False}},
            "display": {"frame_poll_ms": 200}}, profiles=[], user_path=os.devnull)
        win = ConsoleWindow(cfg, Session(cfg), pack)
        win._probe_timer.stop()
        win._serial_timer.stop()
        win._rate_timer.stop()
        win._kick_probe = lambda: None
        self.addCleanup(win.deleteLater)
        return win

    def _feed(self, win, running=False):
        from vmgui.core import rules as R
        sig = R.Signals(packs=[{"name": "x6pro", "ready": True, "panel": "960x480"}],
                        packs_known=True, pack=win.pack or "", vm_running=running,
                        frames_ok=False, probe_ok="no-shell", probe={})
        win._on_decision((sig, None))

    def _patch_fast(self, win, ready=True, running=False, state_size=1024):
        from vmgui.console import window as W
        old = (W.harness.fast_status, W.harness.state_image, W.harness.vm_running)
        W.harness.fast_status = lambda pack: {"ready": ready, "pack": pack}
        W.harness.state_image = lambda pack: ("state.img", state_size)
        W.harness.vm_running = lambda: running
        for name, fn in zip(("fast_status", "state_image", "vm_running"), old):
            self.addCleanup(setattr, W.harness, name, fn)

    def test_fast_buttons_follow_snapshot_and_vm(self):
        win = self._window()
        self._patch_fast(win, ready=True, running=False)
        self._feed(win, running=False)
        self.assertTrue(win.act_fast.isEnabled())
        self.assertTrue(win.act_fast_save.isEnabled())
        self._feed(win, running=True)
        self.assertFalse(win.act_fast.isEnabled())
        self.assertFalse(win.act_fast_save.isEnabled())

    def test_fast_without_snapshot_is_disabled(self):
        win = self._window()
        self._patch_fast(win, ready=False, running=False)
        self._feed(win, running=False)
        self.assertFalse(win.act_fast.isEnabled())
        self.assertIn("请先使用「保存快照」", win.act_fast.toolTip())

    def test_fast_start_calls_up_fast(self):
        from vmgui.console import window as W
        win = self._window()
        self._patch_fast(win, ready=True, running=False)
        self._feed(win, running=False)
        calls = []
        old = (W.harness.start_vm_fast, W.threading.Thread)

        class _Sync:
            def __init__(self, target=None, args=(), kwargs=None, **_ignored):
                self._t, self._a, self._k = target, tuple(args), dict(kwargs or {})

            def start(self):
                if self._t:
                    self._t(*self._a, **self._k)

            def is_alive(self):
                return False

        W.harness.start_vm_fast = lambda pack: (calls.append(pack) or (0, "ok"))
        W.threading.Thread = _Sync
        self.addCleanup(setattr, W.harness, "start_vm_fast", old[0])
        self.addCleanup(setattr, W.threading, "Thread", old[1])
        win._fast_start_clicked()
        self.assertEqual(calls, ["x6pro"])

    def test_fast_save_requires_stopped_vm(self):
        win = self._window()
        self._patch_fast(win, ready=True, running=True)
        notes = []
        win._note = lambda msg: notes.append(msg)
        win._fast_save_clicked()
        self.assertTrue(notes and "请先关机" in notes[0])


class _FakeButton:
    def __init__(self, text, role):
        self.text = text
        self.role = role


def _make_box(picked):
    """`QMessageBox` 的桩**工厂**：`_make_box("ok")` 给出「人按了那颗 AcceptRole 键」的类。

    为什么是工厂不是实例：生产代码写的是 `QMessageBox(self)`（带 parent），还要读
    `QMessageBox.Question` / `AcceptRole` 这些**类属性** —— 塞个实例进去第一步就炸。
    `addButton` 返回的那个对象必须原样从 `clickedButton()` 回来，因为生产判的是
    `box.clickedButton() is not ok`（**身份**，不是文本）。
    三条证据留给断言：`buttons`（摆出来的标签与 role）、`default`（默认键标签 ——
    必须是「取消」，#15/#71：回车不该落在「停掉正在看的这台」上）、
    `informative`（正文里得同时出现两台包名，不然人不知道谁要被关）。
    """
    class _Box:
        Question = Warning = Information = 0
        AcceptRole, RejectRole, DestructiveRole, ActionRole = 0, 1, 2, 3
        buttons = {}
        default = None
        informative = ""
        title = ""

        def __init__(self, parent=None):
            self._b = {}

        def setIcon(self, *_a):                                   # noqa: N802
            pass

        def setWindowTitle(self, text):                           # noqa: N802
            type(self).title = text

        def setText(self, *_a):                                   # noqa: N802
            pass

        def setInformativeText(self, text):                       # noqa: N802
            type(self).informative = text

        def addButton(self, text, role):                          # noqa: N802
            btn = _FakeButton(text, role)
            self._b[text] = btn
            type(self).buttons[text] = role
            return btn

        def setDefaultButton(self, btn):                          # noqa: N802
            type(self).default = btn.text

        def exec(self):                                           # noqa: N802
            return 0

        def clickedButton(self):                                  # noqa: N802
            want = 0 if picked == "ok" else 1
            return next((b for b in self._b.values() if b.role == want), None)

    return _Box


class TestRootLaunchers(unittest.TestCase):
    """根目录那几个 `.cmd` 是**双击的入口**，形状错了不会有人报"报错"，只会闪一下窗。

    三条都是 2026-10-19 加 `vm-console.cmd` 时真撞过的：
    **①** 文件必须纯 ASCII、且**不许被 POSIX shell 过一遍手** —— cmd.exe 按 OEM 码页
    读 `.cmd`，UTF-8 中文会碎成乱码，还能把一行 `echo` 劈成一条不存在的命令
    （`ssh-vm.cmd` 的注释早写了这条，但没人钉）。同一条坑的第二个形状更要命：
    shell 会把 argv 里的 `nul` 重写成 `/dev/null`，于是从 heredoc 写出来的
    `>nul` 落到磁盘上是 `>/dev/null`，cmd.exe 每跑一次就多印一行
    「系统找不到指定的路径。」（当时误判成「探测行本身的毛病」，
    拿六个变体在真 cmd 下重测才分清 —— 见 `vm/_qwen_probe_isolation.py`）。
    **②** `if (…)` 块里的 `echo` 带 `)` 会**提前结束代码块**：实测成功路径也跟着把
    「QMP busy / the real error」那几行失败提示整段印出来，然后停在 `pause` 上。
    **③** 启动器只许转发 —— 挑解释器那件事住在 `dpctl.find_pyside_python()`，
    两处各挑一遍早晚漂移（`run-vm.cmd` 不自己拼 QEMU 命令行是同一条理由）。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _cmd_files(self):
        return [f for f in sorted(os.listdir(self.ROOT)) if f.lower().endswith(".cmd")]

    def _exec_lines(self, txt):
        """只留**会执行**的行。注释里讨论这些坑（连 `nul`、`/dev/null` 都写出来）是允许的，
        钉的是 cmd.exe 真读到的那条命令 —— 上一版把整文件当靶子，结果第一条测试死于
        自己想解释这条规矩的那行 `rem`。"""
        out = []
        for ln in txt.splitlines():
            s = ln.strip()
            if not s or s.lower().startswith(("rem ", "rem\t", "::")):
                continue
            out.append(ln)
        return out

    def test_every_launcher_is_ascii_and_crlf(self):
        self.assertTrue(self._cmd_files(), "根目录一个 .cmd 都没有？")
        for name in self._cmd_files():
            with open(os.path.join(self.ROOT, name), "rb") as f:
                raw = f.read()
            try:
                raw.decode("ascii")
            except UnicodeDecodeError as e:
                self.fail(f"{name} 不是纯 ASCII（cmd.exe 按 OEM 码页读它）：{e}")
            self.assertIn(b"\r\n", raw, f"{name} 不是 CRLF（.gitattributes 钉过 eol=crlf）")
            for ln in self._exec_lines(raw.decode("ascii", "replace")):
                self.assertNotIn("/dev/null", ln.replace(" ", ""),
                                 f"{name} 里有 shell 改写过的重定向，"
                                 f"cmd.exe 会每次多印一行错误：{ln.strip()}")

    def test_console_launcher_forwards_and_has_no_paren_in_echo(self):
        path = os.path.join(self.ROOT, "vm-console.cmd")
        self.assertTrue(os.path.exists(path), "双击启动控制台的入口没了")
        with open(path, encoding="ascii") as f:
            txt = f.read()
        self.assertIn("dpctl.py console", txt,
                      "启动器不再转发了？子命令改名会在这里露出来")
        for ln in self._exec_lines(txt):
            self.assertNotIn("where ", ln,
                             "不许在这里再挑一遍解释器：那件事住在 dpctl.find_pyside_python()")
            if ln.strip().lower().startswith("echo"):
                self.assertNotIn("(", ln, f"echo 里的括号会劈开 if 块：{ln}")
                self.assertNotIn(")", ln, f"echo 里的括号会劈开 if 块：{ln}")

    def test_dpctl_still_has_the_console_subcommand(self):
        with open(os.path.join(self.ROOT, "scripts", "dpctl.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('add_parser("console"', src,
                      "dpctl 的 console 子命令没了，而 vm-console.cmd 还指着它")
        self.assertIn("find_pyside_python()", src,
                      "控制台若不再走那条探测链，就得同步改启动器与这段断言")


class TestConsolePointerBounds(unittest.TestCase):
    """画面外的点击/悬停**不许**发出去（人点名：「在控制台里点，落点跟看见的不是一回事」）。

    画布比画面高得多（x7：960×266 的画面塞进 960×541 的画布，上下各一大条黑边）。
    `screen_to_guest()` 会把越界点**钳到画面边缘**，所以以前点黑边＝在画面最上/最下一行点了一下：
    人看到的是"点在画面外"，客机却在别处动了。判据：按下必须落在画面矩形内；一旦在画面内按下，
    后续拖到画面外仍要发（否则客机那边的按压永远松不开）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from vmgui.console.window import ConsoleWindow
        from vmgui.input import mapper
        cfg = Config.load(cli_overrides={
            "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                           "reconnect": {"enabled": False}},
            "display": {"frame_poll_ms": 200}}, profiles=[], user_path=os.devnull)
        self.win = ConsoleWindow(cfg, Session(cfg), "x7")
        for t in (self.win._probe_timer, self.win._serial_timer, self.win._rate_timer):
            t.stop()
        self.addCleanup(self.win.deleteLater)
        self.sent = []
        self.win.session.pointer = lambda *a, **k: self.sent.append(a)
        c = self.win.canvas
        # 直接钉一个已知的 transform（真值由 paintEvent 算，这里只验"边界那条闸"）
        c._transform = mapper.build_transform(c.width(), c.height(), 960, 266, mode="fit")
        self.t = c._transform
        self.assertGreater(self.t.oy, 20, "夹具本身要真的字盒（否则测不到黑边）")

    def _press(self, x, y):
        self.win._on_mouse_press(QPointF(x, y), Qt.LeftButton)

    def _move(self, x, y, buttons=Qt.NoButton):
        self.win._on_mouse_move(QPointF(x, y), buttons)

    def _release(self, x, y):
        self.win._on_mouse_release(QPointF(x, y), Qt.NoButton)

    def test_press_above_or_below_the_picture_is_ignored(self):
        self._press(self.t.ox + 100, self.t.oy - 10)          # 上黑边
        self._press(self.t.ox + 100, self.t.oy + self.t.disp_h + 10)   # 下黑边
        self.assertEqual(self.sent, [], "黑边上的按下被钳到画面边缘发出去了")

    def test_press_inside_is_delivered_with_the_right_guest_pixel(self):
        self._press(self.t.ox + 730 * self.t.sx, self.t.oy + 130 * self.t.sy)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:2], (730, 130))

    def test_hover_outside_is_ignored(self):
        self._move(self.t.ox + 100, self.t.oy - 10)
        self.assertEqual(self.sent, [])

    def test_drag_that_leaves_the_picture_keeps_delivering(self):
        self._press(self.t.ox + 500 * self.t.sx, self.t.oy + 130 * self.t.sy)
        self._move(self.t.ox + 500 * self.t.sx, self.t.oy - 30, Qt.LeftButton)
        self._release(self.t.ox - 20, self.t.oy - 30)
        self.assertEqual(len(self.sent), 3, "按下/拖出画面/抬起，三次都要发")
        self.assertFalse(self.win._touch_active, "抬起之后要清掉，别让下一次悬停继续发给客机")

    def test_release_without_press_is_ignored(self):
        self._release(self.t.ox + 100, self.t.oy + self.t.disp_h - 5)
        self.assertEqual(self.sent, [])


class TestBtReport(unittest.TestCase):
    """`parse_bt_report`：三种形状各一条（人点名「蓝牙不是所有设备都有，只需做有的」）。"""

    def _p(self, text):
        from vmgui.console.window import parse_bt_report
        return parse_bt_report(text)

    def test_controller_present_but_down(self):
        rep = self._p("@@status\n"
                      "hci0:\tPrimary controller\n"
                      "\taddr 01:00:00:00:00:02 version 7 manufacturer 15\n"
                      "\tcurrent settings: br/edr\n"
                      "\tname dictpen-vm\n"
                      "@@devices\n")
        self.assertEqual(rep["state"], "down")
        self.assertEqual(rep["addr"], "01:00:00:00:00:02")
        self.assertEqual(rep["name"], "dictpen-vm")
        self.assertTrue(rep["powered_off"])
        self.assertFalse(rep["absent"])

    def test_powered_on_with_devices(self):
        rep = self._p("@@status\n"
                      "\taddr 01:00:00:00:00:02 version 7\n"
                      "\tcurrent settings: powered connectable discoverable\n"
                      "@@devices\n"
                      "Device AA:BB:CC:DD:EE:FF  My Speaker\n")
        self.assertEqual(rep["state"], "up")
        self.assertFalse(rep["powered_off"])
        self.assertEqual(rep["devices"], [("AA:BB:CC:DD:EE:FF", "My Speaker")])

    def test_no_controller_is_absent_not_an_error(self):
        rep = self._p("@@status\nIndex list with 0 items\n@@devices\n")
        self.assertTrue(rep["absent"], "没控制器的机器要写「这台没有蓝牙」，不是「出错」")


class TestSndReport(unittest.TestCase):
    """`parse_snd_report`：声卡在 / 不在 / 静音三种形状。"""

    def _p(self, text):
        from vmgui.console.window import parse_snd_report
        return parse_snd_report(text)

    def test_card_present_with_volume(self):
        rep = self._p("@@cards\n"
                      " 0 [Audio          ]: USB-Audio - QEMU USB Audio\n"
                      "                      QEMU QEMU USB Audio at usb-0000:00:02.0-1, full speed\n"
                      "@@mix\n"
                      "Simple mixer control 'Audio Output Volume Control',0\n"
                      "  Front Left: Playback 240 [94%] [-0.50dB] [on]\n")
        self.assertFalse(rep["absent"])
        self.assertEqual(rep["card"], "Audio")
        self.assertEqual(rep["vol"], 94)
        self.assertFalse(rep["muted"])

    def test_muted_flag(self):
        rep = self._p("@@cards\n 0 [Audio ]: USB-Audio - QEMU USB Audio\n@@mix\n"
                      "  Front Left: Playback 240 [94%] [-0.50dB] [off]\n")
        self.assertTrue(rep["muted"])

    def test_no_card(self):
        rep = self._p("@@cards\n--- no soundcards ---\n@@mix\n")
        self.assertTrue(rep["absent"], "没声卡要写「快速启动没有音频」，不是「出错」")


class TestConsoleSndCancel(unittest.TestCase):
    """声音页：新操作顶掉上一条；旧回执作废；没声卡时指向快速启动。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from vmgui.console.window import ConsoleWindow
        cfg = Config.load(cli_overrides={
            "connection": {"uri": "tcp:127.0.0.1:9", "timeout_ms": 200,
                           "reconnect": {"enabled": False}},
            "display": {"frame_poll_ms": 200}}, profiles=[], user_path=os.devnull)
        win = ConsoleWindow(cfg, Session(cfg), "x6pro")
        win._probe_timer.stop()
        win._serial_timer.stop()
        win._rate_timer.stop()
        self.addCleanup(win.deleteLater)
        return win

    def _patch(self, win):
        from vmgui.console import window as W
        old = (W.harness.vm_running, W.harness.GuestJob, W.threading.Thread)
        jobs = []

        class _Job:
            def __init__(self, args, timeout=300):
                self.args = list(args)
                self.timeout = timeout
                self.canceled = False
                jobs.append(self)

            def cancel(self):
                self.canceled = True

            def run(self):
                return 0, ""

        class _Thread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                self.target = target
                self.args = tuple(args)

            def start(self):
                pass

        W.harness.vm_running = lambda: True
        W.harness.GuestJob = _Job
        W.threading.Thread = _Thread
        self.addCleanup(setattr, W.harness, "vm_running", old[0])
        self.addCleanup(setattr, W.harness, "GuestJob", old[1])
        self.addCleanup(setattr, W.threading, "Thread", old[2])
        return jobs

    def test_new_action_interrupts_previous(self):
        win = self._window()
        jobs = self._patch(win)
        win._snd_run("refresh", "cmd-1", 30)
        first = jobs[-1]
        win._snd_run("mute", "cmd-2", 30)
        second = jobs[-1]
        self.assertTrue(first.canceled, "选了新操作时必须杀掉上一条")
        self.assertIsNot(second, first)
        self.assertIs(win._snd_job, second)
        self.assertNotIn("上一条命令尚未返回", win.lbl_snd.text())
        self.assertIn("上一条已中断", win.snd_out.toPlainText())

    def test_stale_reply_ignored(self):
        win = self._window()
        jobs = self._patch(win)
        win._snd_run("refresh", "cmd-1", 30)
        win._snd_run("mute", "cmd-2", 30)
        win.lbl_snd.setText("sentinel")
        win._on_snd(win._snd_gen - 1, "refresh", 0,
                    "@@cards\n 0 [Audio ]: USB-Audio - QEMU USB Audio\n@@mix\n")
        self.assertEqual(win.lbl_snd.text(), "sentinel",
                         "被顶掉的命令回执不许再改界面")
        self.assertIs(win._snd_job, jobs[-1])

    def test_no_card_points_to_fast_start(self):
        win = self._window()
        win._snd_render({"absent": True, "card": "", "desc": "", "vol": None,
                         "muted": False, "ctl": ""})
        self.assertIn("快速启动", win.lbl_snd.text())
        self.assertFalse(win.sld_vol.isEnabled())
        self.assertFalse(win._snd_buttons[1].isEnabled())


class TestGuestJob(unittest.TestCase):
    def test_cancel_before_run_does_not_spawn(self):
        from vmgui.console import harness
        job = harness.GuestJob(["__unused__"], timeout=5)
        job.cancel()
        rc, out = job.run()
        self.assertEqual(rc, 130)
        self.assertIn("已中断", out)


class TestEnsureTone(unittest.TestCase):
    def test_generates_playable_wav(self):
        import struct
        import tempfile
        import wave
        from vmgui.console import harness
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tone.wav")
            self.assertEqual(harness.ensure_tone(path, seconds=0.1), path)
            with wave.open(path, "rb") as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()),
                                 (1, 44100, 2))
                data = w.readframes(w.getnframes())
        vals = struct.unpack("<%dh" % (len(data) // 2), data)
        self.assertGreater(max(vals), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)