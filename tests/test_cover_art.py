# -*- coding: utf-8 -*-
"""cover_art 单测：选字 / 分行 / 字号（纯逻辑）+ 渲染（有字体才真跑）。

渲染路径在沙箱可能没有中文字体 → 允许两条合法路径：排版成功，或「无字体
回退原图拷贝」。真机（Windows 系统字体）走排版成功路径。
"""
import glob
import os
import shutil
import tempfile
import unittest

import cover_art


class TestSplitTitle(unittest.TestCase):
    def test_sep_split_near_mid(self):
        self.assertEqual(cover_art.split_title("逆天系统·废物觉醒"), ["逆天系统", "废物觉醒"])
        self.assertEqual(cover_art.split_title("赘婿：开局即巅峰"), ["赘婿", "开局即巅峰"])

    def test_short_single_line(self):
        self.assertEqual(cover_art.split_title("测试"), ["测试"])
        self.assertEqual(cover_art.split_title("「暗影」"), ["暗影"])

    def test_empty(self):
        self.assertEqual(cover_art.split_title(""), [])
        self.assertEqual(cover_art.split_title("   "), [])

    def test_long_no_sep_balanced(self):
        lines = cover_art.split_title("我在精神病院学斩神")
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), "我在精神病院学斩神")
        self.assertLessEqual(abs(len(lines[0]) - len(lines[1])), 1)


class TestLayout(unittest.TestCase):
    def test_font_size_bounds(self):
        lay = cover_art.layout_title("逆天系统·废物觉醒", 1280, 736)
        self.assertEqual(lay["lines"], ["逆天系统", "废物觉醒"])
        self.assertGreaterEqual(lay["font_size"], 18)
        self.assertLessEqual(lay["font_size"], int(736 * 0.15))
        # 最宽行按「中文字形≈1em」估算必须收进可用宽度
        self.assertLessEqual(lay["font_size"] * 4 * 1.06, lay["max_width"])

    def test_long_title_gets_smaller_font(self):
        a = cover_art.layout_title("测试", 1280, 736)["font_size"]
        b = cover_art.layout_title("一二三四五六七八九十", 1280, 736)["font_size"]
        self.assertGreater(a, b)

    def test_empty_layout(self):
        self.assertIsNone(cover_art.layout_title("", 1280, 736))


class TestChooseTexts(unittest.TestCase):
    def test_precedence_bible_script_project(self):
        self.assertEqual(cover_art.choose_cover_texts("项目名", "剧名", "集标题")[0], "剧名")
        self.assertEqual(cover_art.choose_cover_texts("项目名", "", "集标题")[0], "集标题")
        self.assertEqual(cover_art.choose_cover_texts("项目名", "", "")[0], "项目名")

    def test_tagline_bounds(self):
        plan = {"creative_proposition": {"one_sentence_value": "废物觉醒逆天改命"}}
        self.assertEqual(cover_art.choose_cover_texts("p", "", "", plan)[1], "废物觉醒逆天改命")
        too_long = {"creative_proposition": {"one_sentence_value": "一" * 19}}
        self.assertEqual(cover_art.choose_cover_texts("p", "", "", too_long)[1], "")
        too_short = {"creative_proposition": {"one_sentence_value": "爽"}}
        self.assertEqual(cover_art.choose_cover_texts("p", "", "", too_short)[1], "")

    def test_clean_quotes(self):
        self.assertEqual(cover_art.choose_cover_texts("《逆天系统》")[0], "逆天系统")


class TestBurnTitle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        from PIL import Image
        self.src = os.path.join(self.tmp, "raw.png")
        Image.new("RGB", (640, 360), (20, 30, 40)).save(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_font_falls_back_to_copy(self):
        """沙箱无中文字体 → 必须是「原样拷贝」，封面生成不受影响；真机渲染成功也合法。"""
        dst = os.path.join(self.tmp, "cover.png")
        ok = cover_art.burn_title(self.src, dst, "逆天系统", font_file="")
        self.assertTrue(os.path.isfile(dst))
        if not ok:
            with open(self.src, "rb") as f1, open(dst, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())

    def test_burn_with_available_font(self):
        fonts = sorted(glob.glob("/usr/share/fonts/**/*.ttf", recursive=True))
        if not fonts:
            self.skipTest("沙箱无 TTF 字体，渲染路径真机覆盖")
        dst = os.path.join(self.tmp, "cover.png")
        self.assertTrue(cover_art.burn_title(
            self.src, dst, "TEST", tagline="DEMO", font_file=fonts[0]))
        self.assertGreater(os.path.getsize(dst), 0)


if __name__ == "__main__":
    unittest.main()
