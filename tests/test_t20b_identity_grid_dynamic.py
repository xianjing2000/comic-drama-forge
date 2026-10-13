# -*- coding: utf-8 -*-
"""身份基准网格「动态布局」回归（2026-10-13 用户口径，T20b）。

用户原话：「收尾镜可以动态调整成 2x3、1x3、2x2 等等」。
背景：T20 补齐（d3018eb）把收尾镜降级单帧的同时，把身份基准网格的触发条件从
_sb_grid_mode 收紧为 _p_grid → 10 个收尾镜一并失去身份锚定（实测 shot=13
分镜计时里「身份基准网格=0.0s」整段跳过）。

本测试锁定四件事：
  ① 布局选择：张数 → 行列（覆盖用户举例的 1×3 / 2×2 / 2×3）；
  ② 网格实拼：整图尺寸 = 目标尺寸；单帧路径**不重复填充**（空格留背景色）；
  ③ 提示词段：(3,3) 与原常量逐字一致（防提示词指纹漂移）；动态时形状正确；
  ④ label 布局解析回环（生成端写什么、提示词端就能读回什么）。
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
    Image.new("RGB", (64, 64), color).save(path)


class TestIdentityGridLayout(unittest.TestCase):
    def test_layout_choices(self):
        """张数 → (cols, rows)：覆盖用户举例与边界。"""
        self.assertEqual(sb._identity_grid_layout(1), (1, 1))
        self.assertEqual(sb._identity_grid_layout(2), (2, 1))
        self.assertEqual(sb._identity_grid_layout(3), (3, 1))   # 用户口径 1×3（1 行 3 列）
        self.assertEqual(sb._identity_grid_layout(4), (2, 2))   # 2×2
        self.assertEqual(sb._identity_grid_layout(5), (3, 2))   # 2×3
        self.assertEqual(sb._identity_grid_layout(6), (3, 2))
        self.assertEqual(sb._identity_grid_layout(7), (3, 3))
        self.assertEqual(sb._identity_grid_layout(9), (3, 3))
        self.assertEqual(sb._identity_grid_layout(0), (1, 1))   # 非法输入不炸

    def test_grid_size_and_no_repeat(self):
        """单帧路径：整图尺寸逐像素 = 目标尺寸；不重复填充 → 末尾空格保持背景色。"""
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
                p = os.path.join(td, "c%d.png" % i)
                _mk_img(p, color)
                paths.append(p)
            out = os.path.join(td, "grid.png")
            sb._build_identity_ref_grid(paths, out, target_size=(120, 80),
                                        cols=3, rows=2, repeat=False)
            im = Image.open(out)
            self.assertEqual(im.size, (120, 80))
            self.assertEqual(im.getpixel((5, 5)), (255, 0, 0))
            self.assertEqual(im.getpixel((45, 5)), (0, 255, 0))
            self.assertEqual(im.getpixel((82, 5)), (0, 0, 255))
            self.assertEqual(im.getpixel((5, 45)), _BG)      # 第 4 格 = 空（不重复填充）
            out2 = os.path.join(td, "grid_repeat.png")
            sb._build_identity_ref_grid(paths, out2, target_size=(120, 80),
                                        cols=3, rows=2, repeat=True)
            self.assertEqual(Image.open(out2).getpixel((5, 45)), (255, 0, 0))

    def test_grid_raises_on_empty(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                sb._build_identity_ref_grid([], os.path.join(td, "x.png"))


class TestIdentityGridPrompt(unittest.TestCase):
    def test_3x3_unchanged(self):
        """(3,3) 必须与原常量逐字一致 —— 否则提示词指纹漂移、打断断点缓存。"""
        self.assertEqual(cc.identity_baseline_grid_section(2),
                         cc.IDENTITY_BASELINE_GRID_SECTION.format(pos=2))
        self.assertEqual(cc.identity_baseline_grid_section(2, cols=3, rows=3, cells=9),
                         cc.IDENTITY_BASELINE_GRID_SECTION.format(pos=2))

    def test_dynamic_shape_text(self):
        seg = cc.identity_baseline_grid_section(3, cols=3, rows=1, cells=3)
        self.assertIn("3x1 grid", seg)
        self.assertIn("3 cells", seg)
        self.assertIn("<image3>", seg)
        seg2 = cc.identity_baseline_grid_section(1, cols=2, rows=2, cells=4)
        self.assertIn("2x2 grid", seg2)

    def test_label_parse_roundtrip(self):
        """生成端写的 label（行×列）→ 提示词端解析回 (cols, rows, cells)。"""
        lab = ("参考图1（<image1>）【%s】是本镜出场角色的身份基准网格（1×3 共 3 格"
               "角色基准外观）：输出画面中的角色必须与它保持面部身份、发型与服装一致"
               % cc.IDENTITY_GRID_REF_MARK)
        self.assertEqual(cc._parse_identity_grid_layout(lab), (3, 1, 3))
        lab2 = lab.replace("（1×3 共 3 格", "（2×3 共 6 格")
        self.assertEqual(cc._parse_identity_grid_layout(lab2), (3, 2, 6))
        self.assertIsNone(cc._parse_identity_grid_layout("参考图1（<image1>）无形状串"))


if __name__ == "__main__":
    unittest.main()
