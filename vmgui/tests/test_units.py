"""单元测试：配置优先级、坐标逆变换（含 crop/rotation/stretch）、缩放、键映射、指针模式。

跑法（需要 PySide6，因此用 Python 3.13）：
    python -m unittest discover -s vmgui/tests -t .
或直接：
    python vmgui/tests/test_units.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vmgui.backends.base import Capabilities, Frame, PointerMode     # noqa: E402
from vmgui.config import Config, DEFAULTS, deep_merge, get_path, set_path  # noqa: E402
from vmgui.input import mapper                                        # noqa: E402
from vmgui.view import scaling                                        # noqa: E402


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

    def test_crop_offset_is_applied_and_reversible(self):
        crop = {"enabled": True, "x": 10, "y": 5, "w": 100, "h": 60}
        t = mapper.build_transform(100, 60, 560, 170, mode="1:1", crop=crop)
        self.assertEqual((t.content_w, t.content_h), (100, 60))
        self.assertEqual((t.crop_x, t.crop_y), (10, 5))
        self.assertEqual(mapper.screen_to_guest(0, 0, t), (10, 5))
        s = mapper.guest_to_screen(50, 30, t)
        self.assertEqual(mapper.screen_to_guest(s[0], s[1], t), (50, 30))
        # 裁剪区域外的点不在显示范围内
        self.assertIsNone(mapper.guest_to_screen(200, 30, t))

    def test_crop_clamped_to_guest_bounds(self):
        crop = {"enabled": True, "x": 500, "y": 160, "w": 1000, "h": 1000}
        t = mapper.build_transform(100, 100, 560, 170, mode="1:1", crop=crop)
        self.assertEqual((t.content_w, t.content_h), (60, 10))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)