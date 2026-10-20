"""探针解析与判定的单测（不需要 PySide6，可直接跑）。

fixtures 就是 `30-console-ui.md` §3 的 x6pro 实测输出 + 2026-10-18 的四条实测结论
（F1 精确匹配 / F3 core 不写 0 / F4 桥记集合 / F2 桥重启的假报）。

跑法：
    python -m unittest discover -s vmgui/tests -t .
或直接：
    python vmgui/tests/test_probe_rules.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vmgui.backends.base import content_rect                          # noqa: E402
from vmgui.core import probe as P                                     # noqa: E402
from vmgui.core import rules as R                                     # noqa: E402
from vmgui.input import mapper                                        # noqa: E402

# x6pro 实测输出（2026-10-18）
X6PRO = """holder=894:weston
readers=perl weston
mice=
tp=/dev/input/by-path/axs_ts
uptime=1883.98
core=1
state=36%
bridge=up:529
"""


def _sig(**kw):
    base = dict(packs=[{"name": "x6pro", "runtime": "rk-ext4", "ready": True}],
                pack="x6pro", vm_running=True, frames_ok=True, probe_ok="ok",
                probe=P.parse(X6PRO))
    base.update(kw)
    return R.Signals(**base)


class TestParse(unittest.TestCase):
    def test_fixture_keys(self):
        v = P.parse(X6PRO)
        self.assertEqual(v["holder"], "894:weston")
        self.assertEqual(v["readers"], "perl weston")
        self.assertEqual(v["uptime"], "1883.98")
        self.assertEqual(v["core"], "1")
        self.assertEqual(v["state"], "36%")
        self.assertEqual(v["bridge"], "up:529")
        self.assertEqual(v["tp"], "/dev/input/by-path/axs_ts")

    def test_unknown_and_blank_lines_ignored(self):
        v = P.parse("\n bogus=x\nholder=1:a\n=== \n")
        self.assertEqual(v, {"holder": "1:a"})

    def test_crlf_and_first_wins(self):
        v = P.parse("bridge=down\r\nbridge=up:9\r\n")
        self.assertEqual(v["bridge"], "down")

    def test_empty_holder_is_empty_string(self):
        self.assertEqual(P.parse("holder=\n")["holder"], "")

    def test_accessors(self):
        v = P.parse(X6PRO)
        self.assertEqual(P.holder_pid(v), (894, "weston"))
        self.assertIsNone(P.holder_pid({"holder": ""}))
        self.assertIsNone(P.holder_pid({"holder": "junk"}))
        self.assertEqual(P.readers(v), ["perl", "weston"])
        self.assertEqual(P.ui_readers(v), ["weston"])
        self.assertEqual(P.ui_readers(P.parse("readers=perl\n")), [])
        self.assertEqual(P.mice(P.parse("mice=981:miniapp\n")), ["981:miniapp"])
        self.assertEqual(P.bridge_state(v), "up")
        self.assertEqual(P.bridge_pids({"bridge": "up:529,700"}), [529, 700])
        self.assertEqual(P.bridge_pids({"bridge": "down"}), [])
        self.assertEqual(P.uptime_s(v), 1883.98)
        self.assertEqual(P.core_count(v), 1)
        self.assertEqual(P.state_pct(v), 36)
        self.assertIsNone(P.state_pct({"state": "-"}))
        self.assertTrue(P.touches_available(v))
        self.assertFalse(P.touches_available({"uinput": "missing"}))
        self.assertFalse(P.touches_available({"axs_ts": "missing"}))

    def test_core_dash_is_not_zero(self):
        """F3：目录不存在时脚本发 `core=-`，解析必须给 None —— 不能变成"0 个崩溃文件"。"""
        self.assertIsNone(P.core_count(P.parse("core=-\n")))

    def test_command_carries_the_whole_script(self):
        """命令里必须带着**完整脚本**（这条以前按 base64 解码验，改用 sh -c 之后按转义验）。"""
        cmd = P.command()
        inner = cmd[len("sh -c '"):-1].replace("'\\''", "'")
        self.assertIn("readlink -f", inner)
        self.assertEqual(inner, P.script())
        # 单趟扫 /proc 纪律：不许逐个 pid fork ls
        self.assertIn("ls -l /proc/[0-9]*/fd", P.script())

    def test_shipped_script_has_no_comment_lines(self):
        """发给客机的脚本里**不许有整行注释**：走串口那一通道时客机 shell 会把脚本
        逐行回显进 `vm/serial.log`，而 `rules.serial_state_error` 读的就是这份日志
        —— 判据的载体不该往判据的输入里写自己的话（实测注释占载荷 45.1%）。
        理由留在 `_SCRIPT_SRC` 里，读代码的人照样看得到。"""
        for ln in P.script().split("\n"):
            self.assertFalse(ln.lstrip().startswith("#"),
                             "脚本里还有整行注释：%r" % ln[:60])
        self.assertIn("精确等值", P._SCRIPT_SRC)      # 说明没被删，只是不发货
        self.assertLess(len(P.command().encode("utf-8")), 4600)

    def test_script_matches_exactly_not_by_substring(self):
        """F1：`/dev/input/event2` 不许命中 `/dev/input/event21` —— 靠等值，不靠 index()。"""
        self.assertIn('if (t == a[i])', P.script())
        self.assertNotIn('index($0, a[i])', P.script())
        # 按**真代码**断言：awk 里剥掉内核后缀写的是 `\(deleted\)`。先前这条断言的是
        # `(deleted)`，命中的其实是脚本里那行注释 —— 注释一剔掉就假红了（Q72）。
        self.assertIn(r'sub(/ \(deleted\)$/, "", t)', P.script())


class TestProbeExtendedKeys(unittest.TestCase):
    """#75 新加的三个键：`ap`（虚拟热点三态）与身份尾号（`sn`/`uuid`）。"""

    def test_new_keys_are_whitelisted(self):
        """不在 KEYS 里的键会被 `parse()` 丢掉 —— 新键必须先登记，否则白采。"""
        for k in ("ap", "sn", "uuid"):
            self.assertIn(k, P.KEYS)

    def test_ap_state_three_plus_one_states(self):
        self.assertEqual(P.ap_state({"ap": "up:639"}), "up")
        self.assertEqual(P.ap_state({"ap": "cfg"}), "cfg")
        self.assertEqual(P.ap_state({"ap": "off"}), "off")
        self.assertEqual(P.ap_state({}), "?")            # 没采到 ≠ off（#62 的数据闸）

    def test_transport_does_not_use_base64(self):
        """rv1106 那族的客机**没有 `base64`**（busybox 里唯一没做符号链接的 applet）。

        旧写法 `echo <b64> | base64 -d | sh` 在那族上整段哑掉（`sh: base64: not found`、
        stdout 空、rc=0）→ 一轮探针一个键都回不来。改成 `sh -c '<转义后的脚本>'`。
        """
        self.assertNotIn("base64", P.command())
        self.assertTrue(P.command().startswith("sh -c '"))

    def test_shell_quoting_round_trips(self):
        """单引号转义必须让脚本原样到达 guest（脚本里本来就有单引号）。"""
        cmd = P.command()
        self.assertIn("'\\''", cmd)                 # 转义形态在位
        inner = cmd[len("sh -c '"):-1]
        self.assertEqual(inner.replace("'\\''", "'"), P.script())

    def test_bridge_scan_filters_out_the_probe_itself(self):
        """探针自己的 cmdline 里就写着 `touchbridge\\.pl`（脚本是 `sh -c` 发进去的）。

        实测 a7pro 上一度得到 `bridge=up:13041,13098,497` —— 前两个是 sh 与 grep 自己。
        所以按 cmdline 认完之后**必须**再按 `comm=perl` 过一遍。
        """
        self.assertIn('= "perl"', P.script())

    def test_identity_tail_keeps_three_states_apart(self):
        """`None`（没采到）/ `""`（采到了但设备里就是空的）/ 尾号 —— 三者不许混。"""
        self.assertEqual(P.identity_tail({"sn": "ok:12345", "uuid": "ok:abcde"}),
                         ("12345", "abcde"))
        self.assertEqual(P.identity_tail({"sn": "empty", "uuid": "empty"}), ("", ""))
        self.assertEqual(P.identity_tail({"sn": "na"}), (None, None))
        self.assertEqual(P.identity_tail({}), (None, None))
        self.assertEqual(P.identity_tail({"sn": "weird-value"}), (None, None))


class TestCollect(unittest.TestCase):
    def test_ssh_channel_is_ok(self):
        p = P.collect(lambda cmd, t: (0, X6PRO, "ssh"))
        self.assertEqual(p.ok, "ok")
        self.assertEqual(p.values["core"], "1")

    def test_serial_channel(self):
        p = P.collect(lambda cmd, t: (0, X6PRO, "serial"))
        self.assertEqual(p.ok, "serial")

    def test_no_shell(self):
        p = P.collect(lambda cmd, t: (1, "no transport available", "none"))
        self.assertEqual(p.ok, "no-shell")
        self.assertIn("no transport", p.error)

    def test_transport_exception_is_contained(self):
        def boom(cmd, t):
            raise OSError("broken pipe")
        p = P.collect(boom)
        self.assertEqual(p.ok, "no-shell")
        self.assertIn("broken pipe", p.error)

    def test_channel_ok_but_no_values_is_not_a_sample(self):
        """现象 A：通道在、一个键都没回来 —— 别把它当成功样本（`37-…` §4）。"""
        p = P.collect(lambda cmd, t: (0, "", "ssh"))
        self.assertEqual(p.values, {})
        self.assertTrue(p.error)
        self.assertFalse(bool(p.values))

    def test_script_does_not_use_pidof(self):
        """B(a)：rv1106 客机里**没有 `pidof`** → 旧写法让 `bridge` 恒 `down`。

        只看**代码行**（注释里提到 `pidof` 是为了讲清原因，不算违规）。
        """
        code = "\n".join(line for line in P.script().splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("pidof", code)
        self.assertIn("touchbridge", code)            # 改扫 /proc cmdline
        self.assertIn("echo \"bridge=down\"", code)


class TestCropRect(unittest.TestCase):
    """`33-crop-and-coordinates.md` §2：视图与后端**必须**给出同一个矩形（决策 #35）。"""

    # `35-mapper-shadowed-copy.md` §1 的两组反例（当时 `mapper` 里有一份被遮蔽的旧副本）
    CASES = (
        ({"enabled": True, "x": 10, "y": 10, "w": 800, "h": 254}, 1024, 260),
        ({"enabled": True, "x": 0, "y": 0, "w": 0, "h": 0}, 960, 480),
        ({"enabled": True, "w": 1020, "h": 240}, 960, 480),
        ({"enabled": False, "w": 800, "h": 254}, 960, 480),
        (None, 960, 480),
    )

    def test_build_transform_agrees_with_content_rect(self):
        """**渲染路径**必须走同一份公式 —— 光单测 `content_rect` 抓不到分叉。

        `mapper.py` 顶层曾把 `_content_rect` **定义两次**，Python 让后一个生效，
        于是 `build_transform` 用的是旧副本，而 `content_rect` 是另一份：
        同一份 crop 给出 `(800,254,0,0)` 与 `(800,250,10,10)`；`w/h=0` 时给 `(960,480)` 与 `(1,1)`。
        """
        for crop, fw, fh in self.CASES:
            want = content_rect(crop, fw, fh)
            t = mapper.build_transform(1200, 620, fw, fh, mode="fit", crop=crop)
            got = (t.content_w, t.content_h, t.crop_x, t.crop_y)
            self.assertEqual(got, want, f"crop={crop} 帧={fw}x{fh}")

    def test_origin_is_always_zero_even_if_declared_nonzero(self):
        # 仓库里现成的触发器：tests/smoke_gui.py 用的就是 x:10, y:10
        crop = {"enabled": True, "x": 10, "y": 10, "w": 800, "h": 254}
        self.assertEqual(content_rect(crop, 1024, 260), (800, 254, 0, 0))

    def test_clamped_to_frame(self):
        self.assertEqual(content_rect({"enabled": True, "w": 1020, "h": 240}, 960, 480),
                         (960, 240, 0, 0))

    def test_disabled_is_whole_frame(self):
        self.assertEqual(content_rect({"enabled": False, "w": 800, "h": 254}, 960, 480),
                         (960, 480, 0, 0))
        self.assertEqual(content_rect(None, 960, 480), (960, 480, 0, 0))

    def test_zero_size_declaration_falls_back_to_frame(self):
        self.assertEqual(content_rect({"enabled": True, "w": 0, "h": 0}, 960, 480),
                         (960, 480, 0, 0))


class TestRulesView(unittest.TestCase):
    """view 层：**接管视图的唯一资格是"没有帧可画"**（F7）。"""

    def test_healthy(self):
        d = R.decide(_sig())
        self.assertEqual(d.view, R.VIEW_NONE)
        self.assertEqual(d.flags, ())

    def test_no_packs(self):
        self.assertEqual(R.decide(_sig(packs=[])).view, R.VIEW_NO_PACK)

    def test_packs_unknown_is_not_no_pack(self):
        """首次扫描在后台线程跑（要算镜像 MD5，几十秒），`packs=[]` **不许**当成"没有固件包"。

        实测过：把扫描挂在 5 s 探针上，探针线程被堵住，12 s 里一条判定都没送到。
        """
        d = R.decide(_sig(packs=[], packs_known=False, vm_running=False))
        self.assertEqual(d.view, R.VIEW_VM_DOWN)          # 不知道清单就退一步按别的判
        self.assertNotEqual(d.view, R.VIEW_NO_PACK)
        d2 = R.decide(_sig(packs=[], packs_known=False, vm_running=True))
        self.assertEqual(d2.view, R.VIEW_NONE)
        self.assertEqual(R.decide(_sig(packs=[], packs_known=True)).view, R.VIEW_NO_PACK)

    def test_packs_unknown_does_not_fake_missing_images(self):
        """清单未知时 `chosen={}` 也不能算成"这个包缺文件"。"""
        d = R.decide(_sig(packs=[], packs_known=False, pack="x6pro"))
        self.assertNotEqual(d.view, R.VIEW_NO_PACK)

    def test_pack_missing_images(self):
        packs = [{"name": "x6pro", "runtime": "rk-ext4",
                  "missing": ["rootfs"], "md5_bad": [], "runtime_ok": True}]
        d = R.decide(_sig(packs=packs, pack="x6pro"))
        self.assertEqual(d.view, R.VIEW_NO_PACK)
        self.assertEqual(d.vars["missing"], ["rootfs"])

    def test_md5_mismatch_is_not_ready(self):
        packs = [{"name": "x6pro", "missing": [], "md5_bad": ["a.img"], "runtime_ok": True}]
        self.assertEqual(R.decide(_sig(packs=packs, pack="x6pro")).view, R.VIEW_NO_PACK)

    def test_runtime_unimplemented_is_not_ready(self):
        packs = [{"name": "x6pro", "missing": [], "md5_bad": [], "runtime_ok": False}]
        self.assertEqual(R.decide(_sig(packs=packs, pack="x6pro")).view, R.VIEW_NO_PACK)

    def test_vm_down(self):
        self.assertEqual(R.decide(_sig(vm_running=False)).view, R.VIEW_VM_DOWN)

    def test_booting_when_no_shell_yet(self):
        d = R.decide(_sig(probe_ok="no-shell", frames_ok=False, probe={}))
        self.assertEqual(d.view, R.VIEW_BOOTING)

    def test_qmp_busy(self):
        d = R.decide(_sig(frames_ok=False, other_owner="vmgui pid 1234"))
        self.assertEqual(d.view, R.VIEW_QMP_BUSY)
        self.assertEqual(d.vars["owner"], "vmgui pid 1234")

    def test_disk_full_does_not_take_over_the_view(self):
        """F7 的核心：盘 95% ≠ 没得看 —— 有帧就继续画，只加一条警告。"""
        p = P.parse("state=96%\ncore=1\nholder=894:weston\nreaders=perl weston\nbridge=up:529\n")
        d = R.decide(_sig(probe=p, frames_ok=True))
        self.assertEqual(d.view, R.VIEW_NONE)
        self.assertTrue(d.has(R.FLAG_DISK_FULL))
        self.assertEqual(d.flag(R.FLAG_DISK_FULL).vars["state_pct"], 96)

    def test_state_ro_coexists_with_disk_full_and_touch(self):
        p = P.parse("holder=\nreaders=perl\nbridge=down\nstate=96%\ncore=1\n")
        d = R.decide(_sig(probe=p, serial_state_error=True))
        self.assertEqual(d.view, R.VIEW_NONE)
        self.assertTrue(d.has(R.FLAG_STATE_RO))
        self.assertTrue(d.has(R.FLAG_DISK_FULL))
        self.assertTrue(d.has(R.FLAG_TOUCH))
        self.assertEqual(d.flag(R.FLAG_TOUCH).vars["reason"], R.RING_NO_BRIDGE)

    def test_no_frame_is_a_flag_not_a_view(self):
        d = R.decide(_sig(frames_ok=False))
        self.assertEqual(d.view, R.VIEW_NONE)
        self.assertTrue(d.has(R.FLAG_NO_FRAME))

    def test_empty_probe_values_are_neither_safe_nor_a_fault(self):
        """现象 A（`37-verify-findings.md` §4）：`values={}` 时**整轮不出 guest 警告**。

        实测 rv1106 起机期能连报 12 轮（t=0~154.8 s）`probe_ok=ok / values={}` ——
        旧实现给出 `touch/no-bridge` 假故障，亮三分多钟。
        """
        d = R.decide(_sig(probe={}, probe_ok="ok"))
        self.assertEqual(d.view, R.VIEW_NONE)
        self.assertEqual(d.flags, ())          # 既不出平安（字段是 —），也不出故障
        # 宿主侧事实照留
        d2 = R.decide(_sig(probe={}, probe_ok="ok", frames_ok=False))
        self.assertTrue(d2.has(R.FLAG_NO_FRAME))
        self.assertFalse(d2.has(R.FLAG_TOUCH))
        # 磁盘/串口这类"半条"也不许出现
        d3 = R.decide(_sig(probe={}, probe_ok="serial", serial_state_error=True))
        self.assertTrue(d3.has(R.FLAG_STATE_RO))   # 宿主侧判据仍生效
        self.assertFalse(d3.has(R.FLAG_TOUCH))

    def test_bridge_state_unknown_is_not_no_bridge(self):
        """`bridge` 这个键**没采到**（`bridge_state({}) == '?'`）不许判成"桥没在跑"。"""
        p = P.parse("holder=\nreaders=perl\nstate=36%\ncore=1\nuptime=60\n")   # 没有 bridge=
        d = R.decide(_sig(probe=p))
        self.assertFalse(d.has(R.FLAG_TOUCH))
        self.assertFalse(d.has(R.FLAG_UI_STARTING))

    def test_frames_fail_with_other_owner_is_qmp_busy(self):
        d = R.decide(_sig(frames_ok=False, other_owner="vmgui pid 1234"))
        self.assertEqual(d.view, R.VIEW_QMP_BUSY)
        self.assertFalse(d.has(R.FLAG_NO_FRAME))


class TestRulesTouch(unittest.TestCase):
    def test_no_reader_when_ui_is_up(self):
        p = P.parse("holder=\nreaders=perl weston\ntp=/dev/input/by-path/axs_ts\n"
                    "bridge=up:529\nstate=36%\ncore=1\nuptime=800\n")
        d = R.decide(_sig(probe=p))
        self.assertTrue(d.has(R.FLAG_TOUCH))
        self.assertEqual(d.flag(R.FLAG_TOUCH).vars["reason"], R.RING_NO_READER)
        self.assertEqual(d.flag(R.FLAG_TOUCH).vars["tp"], "/dev/input/by-path/axs_ts")

    def test_no_bridge(self):
        p = P.parse("holder=\nreaders=weston\nbridge=down\nstate=36%\ncore=1\nuptime=800\n")
        self.assertEqual(R.decide(_sig(probe=p)).flag(R.FLAG_TOUCH).vars["reason"],
                         R.RING_NO_BRIDGE)

    def test_no_uinput(self):
        p = P.parse("holder=\nreaders=perl weston\nbridge=up:529\nuinput=missing\n"
                    "state=36%\ncore=1\nuptime=800\n")
        self.assertEqual(R.decide(_sig(probe=p)).flag(R.FLAG_TOUCH).vars["reason"],
                         R.RING_NO_UINPUT)

    def test_ui_not_attached_yet_is_not_a_fault(self):
        """实测：起机 ~38 s 时 readers 只有 perl、holder 为空 —— 那是在起，不是故障。"""
        p = P.parse("holder=\nreaders=perl\nbridge=up:530\nstate=36%\ncore=1\nuptime=38\n")
        d = R.decide(_sig(probe=p))                    # 有帧
        self.assertEqual(d.view, R.VIEW_NONE)           # Q29：有帧就不接管视图
        self.assertTrue(d.has(R.FLAG_UI_STARTING))
        self.assertFalse(d.has(R.FLAG_TOUCH))

    def test_ui_not_attached_without_frame_takes_over_but_keeps_flags(self):
        """Q29：`defer` 期间**不许**把别的警告一起吞掉（旧实现清空 flags）。"""
        p = P.parse("holder=\nreaders=perl\nbridge=up:530\nstate=96%\ncore=1\nuptime=38\n")
        d = R.decide(_sig(probe=p, frames_ok=False))
        self.assertEqual(d.view, R.VIEW_BOOTING)
        self.assertTrue(d.has(R.FLAG_UI_STARTING))
        self.assertTrue(d.has(R.FLAG_DISK_FULL))

    def test_ui_started_hint_does_not_end_the_defer(self):
        """Q1 实测：两族的 marker/holder 先后**相反** ——

        x6pro：holder 33.1 s 先、marker 53.3 s 后；**a7pro：marker (172.7,181.8] 先、holder 后**。
        所以"看见 marker＝界面已起"在 rv1106 上是假的，用它结束 defer 会报 `no-reader` 假故障
        → `ui_started_hint` **不参与判据**，只当信息位，defer 只按 600 s 兜底。
        """
        p = P.parse("holder=\nreaders=perl\nbridge=up:530\nstate=36%\ncore=1\nuptime=60\n")
        d = R.decide(_sig(probe=p, ui_started_hint=True))
        self.assertEqual(d.view, R.VIEW_NONE)          # 有帧 → 不接管视图（#55）
        self.assertTrue(d.has(R.FLAG_UI_STARTING))
        self.assertFalse(d.has(R.FLAG_TOUCH))          # 不许因 hint 就报故障
        # 没帧时才落到 booting，flag 照旧
        d2 = R.decide(_sig(probe=p, ui_started_hint=True, frames_ok=False))
        self.assertEqual(d2.view, R.VIEW_BOOTING)
        self.assertTrue(d2.has(R.FLAG_UI_STARTING))

    def test_ui_never_attached_past_grace_is_a_fault(self):
        """容忍窗给到 600 s；超了必须开口报，否则 x5pro 那族会被永远吞掉（F5）。"""
        p = P.parse("holder=\nreaders=perl\nbridge=up:530\nstate=36%\ncore=1\nuptime=900\n")
        d = R.decide(_sig(probe=p))
        self.assertEqual(d.view, R.VIEW_NONE)
        self.assertTrue(d.has(R.FLAG_TOUCH))
        self.assertFalse(d.has(R.FLAG_UI_STARTING))

    def test_bridge_pid_change_is_only_an_informational_flag(self):
        """F2 实测：桥 kill 掉再手动起回，weston **同一个 pid 894** 重开了新节点。

        所以"pid 没变"根本不能说明它握着旧节点 —— 判据废弃，只留信息位，不许升级成警告。
        """
        p = P.parse("holder=894:weston\nreaders=perl weston\nbridge=up:700\n"
                    "state=36%\ncore=1\nuptime=800\n")
        d = R.decide(_sig(probe=p, prev_bridge_pids=(529,), prev_holder_pid=894))
        self.assertTrue(d.has(R.FLAG_BRIDGE_RESTARTED))
        self.assertFalse(d.has(R.FLAG_TOUCH))            # 健康机器不许假报
        self.assertEqual(d.next_bridge_pids, (700,))

    def test_bridge_history_is_a_set_not_a_single_pid(self):
        """F4：`pidof perl` 顺序不保证 —— 同一个集合重排不算"重启过"。"""
        p = P.parse("holder=894:weston\nreaders=perl weston\nbridge=up:529,700\n"
                    "state=36%\ncore=1\nuptime=800\n")
        d = R.decide(_sig(probe=p, prev_bridge_pids=(700, 529)))
        self.assertFalse(d.has(R.FLAG_BRIDGE_RESTARTED))
        self.assertEqual(d.next_bridge_pids, (529, 700))

    def test_first_probe_records_bridge_pids(self):
        d = R.decide(_sig())
        self.assertEqual(d.next_bridge_pids, (529,))
        self.assertEqual(d.next_holder_pid, 894)


# ---- Q70 设备适配性体检：注册表列着、盘上没文件 -------------------------------- #
# a6pro 实测（2026-10-19）：修前 5 条命中、修后 22 条全绿。三态与"图标文件名不是常量"
# 这两条是拿真机换来的，回归护栏就钉这两条。
APPS_OK = "apps=22\napps_bad=0\napps_ids=\napps_at=\n"
APPS_BAD = ("apps=22\napps_bad=2\napps_ids= 8080272425914438 8080212246010681\n"
            "apps_at= /userdisk/secondary/miniapp/data/mini_app/pkg/8080272425914438/a/app.js.bin"
            " /userdisk/secondary/miniapp/data/mini_app/pkg/8080212246010681/a/app_icon.png\n")


class TestAppAdapt(unittest.TestCase):
    def test_parses_the_apps_block(self):
        v = P.parse(X6PRO + APPS_OK)
        self.assertEqual(P.app_adapt(v), (22, 0, [], []))

    def test_bad_entries_carry_ids_and_paths(self):
        total, bad, ids, at = P.app_adapt(P.parse(X6PRO + APPS_BAD))
        self.assertEqual((total, bad), (22, 2))
        self.assertEqual(ids, ["8080272425914438", "8080212246010681"])
        self.assertEqual(len(at), 2)
        self.assertTrue(all(p.startswith("/") for p in at))

    def test_no_registry_is_not_the_same_as_zero_apps(self):
        """`apps=-`（这台没有注册表）必须是 None = **不判**，不能是 (0,0) = 平安。"""
        self.assertIsNone(P.app_adapt(P.parse(X6PRO + "apps=-\n")))
        self.assertIsNone(P.app_adapt(P.parse(X6PRO)))          # 键整个没回来
        self.assertIsNone(P.app_adapt(P.parse(X6PRO + "apps=abc\napps_bad=0\n")))

    def test_flag_fires_only_when_something_is_missing(self):
        d = R.decide(_sig(probe=P.parse(X6PRO + APPS_BAD)))
        f = d.flag(R.FLAG_APP_MISSING)
        self.assertIsNotNone(f)
        self.assertEqual(f.vars["apps_bad"], 2)
        self.assertEqual(f.vars["apps_total"], 22)
        self.assertFalse(R.decide(_sig(probe=P.parse(X6PRO + APPS_OK)))
                         .has(R.FLAG_APP_MISSING))

    def test_unsampled_registry_never_fires(self):
        for text in (X6PRO, X6PRO + "apps=-\n"):
            self.assertFalse(R.decide(_sig(probe=P.parse(text)))
                             .has(R.FLAG_APP_MISSING))

    def test_coexists_with_disk_full(self):
        """F7：两条警告能同时响，谁也不许吞掉谁（一条 95% 的盘不该盖住丢了的图标）。"""
        v = P.parse(X6PRO.replace("state=36%", "state=97%") + APPS_BAD)
        d = R.decide(_sig(probe=v))
        self.assertTrue(d.has(R.FLAG_DISK_FULL))
        self.assertTrue(d.has(R.FLAG_APP_MISSING))
        self.assertEqual(d.view, R.VIEW_NONE)        # 警告不接管画面

    def test_no_data_gate_does_not_fire_it(self):
        """整轮一个键都没回来时（起机期实测能连报 12 轮）什么都不判。"""
        d = R.decide(_sig(probe={}, probe_ok="ok"))
        self.assertFalse(d.has(R.FLAG_APP_MISSING))

    def test_script_reads_the_icon_field_not_a_hardcoded_name(self):
        """判据必须去注册表读 `icon`：a6pro 22 条里预设包是 `app_icon.png`、商店包是
        `icon.png`、10 条服务类是**空串**。写死名字 = 误报 10 条 + 漏掉真丢图标的。"""
        awk = P.apps_awk()
        self.assertIn('"icon": "', awk)               # 读字段
        self.assertNotIn("app_icon.png", awk)         # 不写死任何一种名字
        self.assertIn("ic != \"\"", awk)              # 空串那一档要跳过

    def test_script_avoids_the_tools_rv1106_does_not_have(self):
        """rv1106 的 busybox：没有 `stat`、没有 `pidof`、`base64` 连符号链接都没做（#84）。

        与 `test_script_does_not_use_pidof` 同一口径：**只看代码行**，注释里提这些词是
        为了讲清为什么不用。
        """
        code = "\n".join(line for line in P.script().splitlines()
                         if not line.lstrip().startswith("#"))
        for word in ("stat ", "pidof", "base64"):
            self.assertNotIn(word, code, f"探针用了客机没有的东西：{word}")

    def test_all_registry_bases_are_tried(self):
        """**三棵**树同名不同物，少一棵就把"有注册表"说成"没得体检"。

        实测（2026-10-19 逐型号）：a6pro/a7pro 的注册表在 `/userdata/miniapp`，条目却散在
        `/userdisk/secondary/miniapp`；**x5pro 的 24 条全在 `/userdisk/miniapp`**，前两棵
        根本没有这份文件 —— 只列两棵时 x5pro 发 `apps=-`。
        """
        s = P.script()
        for base in ("/userdata/miniapp/data/mini_app/pkg/packages.json",
                     "/userdisk/secondary/miniapp/data/mini_app/pkg/packages.json",
                     "/userdisk/miniapp/data/mini_app/pkg/packages.json"):
            self.assertIn(base, s)
        # 顺序也是判据的一部分：谁先 `[ -f ]` 中就用谁，所以新加的这棵要**排在后面**
        # （x5pro 只有它，加在前面的话会改变 a6pro/a7pro 走的那棵）。
        self.assertLess(s.index("/userdata/miniapp/data"),
                        s.index("/userdisk/miniapp/data"))


@unittest.skipIf(shutil.which("awk") is None, "这台宿主没有 awk")
class TestAppAdaptAwkProgram(unittest.TestCase):
    """拿**同一段 awk** 在宿主上跑临时注册表：判据本身也要有回归护栏。

    只用 busybox awk 与 GNU awk 行为一致的子集（`match`/`RSTART`/`substr`/
    `getline < 文件`/`close`），所以这段在宿主跑绿就等于在客机跑绿。
    """

    @staticmethod
    def _entry(appid, icon, path):
        return ('{"appid": "%s", "b": false, "category": "X", "flag": 3, '
                '"icon": "%s", "installPath": "%s", "name": "n", "props": {}, '
                '"version": "1.0.0"}' % (appid, icon, path))

    def _run(self, registry_text):
        import subprocess
        d = tempfile.mkdtemp()
        try:
            reg = os.path.join(d, "packages.json").replace("\\", "/")
            with open(reg, "w", encoding="utf-8", newline="") as f:
                f.write(registry_text)
            r = subprocess.run(["awk", "-v", "max=12", P.apps_awk(), reg],
                               capture_output=True, text=True, encoding="utf-8")
            return P.parse(r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_healthy_dir_passes(self):
        d = tempfile.mkdtemp().replace("\\", "/")
        try:
            os.makedirs(d + "/a")
            for n in ("app.js.bin", "app_icon.png"):
                with open(d + "/a/" + n, "wb") as f:
                    f.write(b"x")
            v = self._run('{"packages": [%s]}' % self._entry("111", "app_icon.png", d + "/a/"))
            self.assertEqual((v["apps"], v["apps_bad"]), ("1", "0"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_missing_files_are_counted(self):
        d = tempfile.mkdtemp().replace("\\", "/")
        try:
            ok = (d + "/ok/a")
            os.makedirs(ok)
            for n in ("app.js.bin", "icon.png"):
                with open(ok + "/" + n, "wb") as f:
                    f.write(b"x")
            svc = d + "/svc/b"            # 服务类：有代码、`icon` 字段是空串（a6pro 22 条里 10 条这样）
            os.makedirs(svc)
            with open(svc + "/app.js.bin", "wb") as f:
                f.write(b"x")
            reg = '{"packages": [%s, %s, %s, %s]}' % (
                self._entry("111", "app_icon.png", d + "/gone/a/"),   # 整个目录没了＝修前那五个
                self._entry("222", "icon.png", ok + "/"),             # 好的
                self._entry("333", "no_such.png", ok + "/"),          # 只丢图标＝列表丢图标
                self._entry("444", "", svc + "/"))                    # 空 icon **不算问题**
            v = self._run(reg)
            self.assertEqual(v["apps"], "4")
            self.assertEqual(v["apps_bad"], "2")
            self.assertIn("111", v["apps_ids"])
            self.assertIn("333", v["apps_ids"])
            self.assertNotIn("444", v["apps_ids"])
            self.assertNotIn("222", v["apps_ids"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
