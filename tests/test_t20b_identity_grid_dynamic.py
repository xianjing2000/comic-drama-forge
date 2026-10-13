# -*- coding: utf-8 -*-
"""身份基准网格「按剧情动态布局」回归（2026-10-13 用户口径，T20b）。

用户口径演进：
  · 起因：T20 补齐（d3018eb）把收尾镜降级单帧的同时，把身份基准网格的触发条件从
    _sb_grid_mode 收紧为 _p_grid → 10 个收尾镜一并失去身份锚定（实测 shot=13
    分镜计时里「身份基准网格=0.0s」整段跳过）。
  · 定稿：「我只是举例子比例，具体比例要根据具体剧情来啊」+「每格 = 一张视图」+
    「门槛 ≥2」→ 一行 = 一个出场角色、列 = 该角色的视图/服装变体。

本测试锁定：
  ① 角色多视图收集（命中本集服装变体 → [变体, 基础设定图]，本集优先）；
  ② rows_paths 布局：同角色同行、行内按视图顺序、整图尺寸逐像素 = 目标尺寸；
  ③ 九宫格旧路径行为不变（repeat 循环填充）；单帧不重复填充（空格留背景色）；
  ④ 提示词段：(3,3) 与原常量逐字一致（防指纹漂移）；动态时形状与「同行同角色」都在；
  ⑤ label 布局解析回环（生成端写什么、提示词端就能读回什么）。
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from PIL import Image  # noqa: E402

import storyboard_helpers as sb  # noqa: E402
import comfyui_client as cc  # noqa: E402

_BG = (12, 12, 16)


def _mk_img(path, color):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (64, 64), color).save(path)


class TestIdentityViews(unittest.TestCase):
    def test_views_for_outfit_variant(self):
        """命中本集服装变体 → [变体, 基础设定图]（本集服装优先，剧情相关性更强）。"""
        with tempfile.TemporaryDirectory() as td:
            char_dir = os.path.join(td, "羡进")
            base = os.path.join(char_dir, "base.png")
            variant = os.path.join(char_dir, "outfits", "ep01_abc", "base.png")
            _mk_img(base, (10, 10, 10))
            _mk_img(variant, (200, 200, 200))
            self.assertEqual(sb._identity_views_for(variant), [variant, base])
            self.assertEqual(sb._identity_views_for(base), [base])
            self.assertEqual(sb._identity_views_for(os.path.join(td, "无.png")), [])

    def test_views_use_episode_outfit_override(self):
        """命中主设定图 + 本集服装 key → [本集变体, 主设定图]（本集优先）。

        这正是生产里 shot=13 的真实情形：命中的是主设定图（_shot_outfit_dir 在分镜侧
        恒为空），必须靠 t16 的覆盖表才能把「本集服装」补进网格。
        """
        with tempfile.TemporaryDirectory() as td:
            char_dir = os.path.join(td, "羡进")
            base = os.path.join(char_dir, "base.png")
            variant = os.path.join(char_dir, "outfits", "ep01_3f97355a", "base.png")
            _mk_img(base, (10, 10, 10))
            _mk_img(variant, (200, 200, 200))
            self.assertEqual(sb._identity_views_for(base, "ep01_3f97355a"), [variant, base])
            self.assertEqual(sb._identity_views_for(base, "ep01_不存在"), [base])
            self.assertEqual(sb._identity_views_for(base), [base])


class TestIdentityGridBuild(unittest.TestCase):
    def test_rows_paths_two_characters(self):
        """2 角色 × 2 视图 → 2×2；同角色同行，行内按视图顺序。"""
        with tempfile.TemporaryDirectory() as td:
            def mk(name, color):
                p = os.path.join(td, name)
                _mk_img(p, color)
                return p
            a1, a2 = mk("a1.png", (255, 0, 0)), mk("a2.png", (255, 128, 0))
            b1, b2 = mk("b1.png", (0, 255, 0)), mk("b2.png", (0, 128, 255))
            out = os.path.join(td, "grid.png")
            sb._build_identity_ref_grid(None, out, target_size=(120, 80),
                                        rows_paths=[[a1, a2], [b1, b2]])
            im = Image.open(out)
            self.assertEqual(im.size, (120, 80))
            self.assertEqual(im.getpixel((5, 5)), (255, 0, 0))
            self.assertEqual(im.getpixel((82, 5)), (255, 128, 0))
            self.assertEqual(im.getpixel((5, 45)), (0, 255, 0))
            self.assertEqual(im.getpixel((82, 45)), (0, 128, 255))

    def test_rows_paths_single_character_two_views(self):
        """1 角色 × 2 视图（基础 + 本集服装）→ 1×2：这正是收尾镜的最低门槛场景（≥2）。"""
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "v1.png"); p2 = os.path.join(td, "v2.png")
            _mk_img(p1, (255, 0, 0)); _mk_img(p2, (0, 0, 255))
            out = os.path.join(td, "g.png")
            sb._build_identity_ref_grid(None, out, target_size=(120, 80),
                                        rows_paths=[[p1, p2]])
            im = Image.open(out)
            self.assertEqual(im.size, (120, 80))
            # 只有一行 → 该行铺满整高（不是只占上半）
            self.assertEqual(im.getpixel((5, 5)), (255, 0, 0))
            self.assertEqual(im.getpixel((82, 5)), (0, 0, 255))
            self.assertEqual(im.getpixel((5, 45)), (255, 0, 0))
            self.assertEqual(im.getpixel((82, 45)), (0, 0, 255))

    def test_legacy_paths_unchanged(self):
        """九宫格旧路径：repeat=True 循环填满；单帧 repeat=False 不重复。"""
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
                p = os.path.join(td, "c%d.png" % i)
                _mk_img(p, color)
                paths.append(p)
            out = os.path.join(td, "grid.png")
            sb._build_identity_ref_grid(paths, out, target_size=(120, 80),
                                        cols=3, rows=2, repeat=False)
            self.assertEqual(Image.open(out).getpixel((5, 45)), _BG)
            out2 = os.path.join(td, "grid_repeat.png")
            sb._build_identity_ref_grid(paths, out2, target_size=(120, 80),
                                        cols=3, rows=2, repeat=True)
            self.assertEqual(Image.open(out2).getpixel((5, 45)), (255, 0, 0))

    def test_raises_on_empty(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                sb._build_identity_ref_grid([], os.path.join(td, "x.png"))
            with self.assertRaises(ValueError):
                sb._build_identity_ref_grid(None, os.path.join(td, "y.png"),
                                            rows_paths=[[]])


class TestIdentityGridPrompt(unittest.TestCase):
    def test_3x3_unchanged(self):
        """(3,3) 必须与原常量逐字一致 —— 否则提示词指纹漂移、打断断点缓存。"""
        self.assertEqual(cc.identity_baseline_grid_section(2),
                         cc.IDENTITY_BASELINE_GRID_SECTION.format(pos=2))
        self.assertEqual(cc.identity_baseline_grid_section(2, cols=3, rows=3, cells=9),
                         cc.IDENTITY_BASELINE_GRID_SECTION.format(pos=2))

    def test_dynamic_shape_text(self):
        """动态形状：说清行列/格数，并声明「同行 = 同角色」。"""
        seg = cc.identity_baseline_grid_section(3, cols=3, rows=1, cells=3)
        self.assertIn("3x1 grid", seg)
        self.assertIn("3 cells", seg)
        self.assertIn("<image3>", seg)
        self.assertIn("all cells in the same row show", seg)
        seg2 = cc.identity_baseline_grid_section(1, cols=2, rows=2, cells=4)
        self.assertIn("2x2 grid", seg2)

    def test_label_parse_roundtrip(self):
        """生成端写的 label（行×列）→ 提示词端解析回 (cols, rows, cells)。"""
        lab = ("参考图1（<image1>）【%s】是本镜出场角色的身份基准网格（2×2 共 4 格，"
               "同一行 = 同一角色：羡进(2视图)/赵天霸(2视图)）：输出画面中的角色必须与它"
               "保持面部身份、发型与服装一致" % cc.IDENTITY_GRID_REF_MARK)
        self.assertEqual(cc._parse_identity_grid_layout(lab), (2, 2, 4))
        lab2 = lab.replace("（2×2 共 4 格", "（1×2 共 2 格")
        self.assertEqual(cc._parse_identity_grid_layout(lab2), (2, 1, 2))
        self.assertIsNone(cc._parse_identity_grid_layout("参考图1（<image1>）无形状串"))


if __name__ == "__main__":
    unittest.main()
