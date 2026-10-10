# -*- coding: utf-8 -*-
"""纯函数回归测试：锁定「确定性输入 → 输出」，防止重构/下沉时静默改变行为。

为什么选这些函数：
  · comfyui_client 是全项目最大模块（5622 行）却**零测试**，其中的提示词槽位判定 /
    节点排序 / 分辨率取整都是纯逻辑，改坏了不会报错、只会让出图悄悄变差；
  · aspect_size / round_to_multiple 是「分辨率对齐的唯一出口」，项目里明确要求所有
    动态画幅都走它们（避免有的对齐 8、有的对齐 32 的漂移）；
  · _is_negative_slot 的 docstring 记录过一次真实事故：退回裸关键词匹配会把正向的
    「不得出现…水印」判成负向槽位，**实测导致 77% 的正向提示词被追加裸负面词**；
  · _ref_canvas_target / _fit_ref_to_canvas 出自本轮下沉的 storyboard_helpers。
"""
import os
import sys
import unittest

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import style_kit  # noqa: E402
import comfyui_client as CC  # noqa: E402
import storyboard_helpers as SB  # noqa: E402
import prompt_qc as Q  # noqa: E402
import style_kit as S  # noqa: E402  （别名，供新增用例使用）


class TestRoundToMultiple(unittest.TestCase):
    """分辨率取整的唯一出口。"""

    def test_aligns_to_multiple(self):
        self.assertEqual(style_kit.round_to_multiple(100, 32), 96)
        self.assertEqual(style_kit.round_to_multiple(96, 32), 96)
        self.assertEqual(style_kit.round_to_multiple(97, 32), 96)
        self.assertEqual(style_kit.round_to_multiple(113, 32), 128)

    def test_negative_or_none_returns_zero(self):
        self.assertEqual(style_kit.round_to_multiple(-5, 32), 0)
        self.assertEqual(style_kit.round_to_multiple(None, 32), 0)

    def test_illegal_multiple_falls_back_to_one(self):
        """multiple 非法时回落 1（原值取整），绝不抛异常阻断生成。"""
        self.assertEqual(style_kit.round_to_multiple(100, 0), 100)
        self.assertEqual(style_kit.round_to_multiple(100, -8), 100)


class TestAspectSize(unittest.TestCase):
    """docstring 里给了权威预期值，这里直接锁死。"""

    def test_documented_values(self):
        # style_kit.aspect_size 文档：「(9,16) + 0.5MP + 32 → (544, 960)；(16,9) → (960, 544)」
        self.assertEqual(style_kit.aspect_size((9, 16), 0.5, 32), (544, 960))
        self.assertEqual(style_kit.aspect_size((16, 9), 0.5, 32), (960, 544))

    def test_invalid_returns_none(self):
        self.assertIsNone(style_kit.aspect_size(None))
        self.assertIsNone(style_kit.aspect_size((0, 9)))
        self.assertIsNone(style_kit.aspect_size((9, 16), 0.5, 0))

    def test_output_is_multiple_aligned(self):
        """输出必须严格对齐到 multiple（潜空间要求）。"""
        for ratio in ((9, 16), (16, 9), (1, 1), (4, 3)):
            w, h = style_kit.aspect_size(ratio, 1.0, 32)
            self.assertEqual(w % 32, 0, '%s 宽未对齐: %s' % (ratio, w))
            self.assertEqual(h % 32, 0, '%s 高未对齐: %s' % (ratio, h))


class TestComfyPureHelpers(unittest.TestCase):
    """comfyui_client 内 9 个纯函数里最易被改坏的几个。"""

    def test_slot_index(self):
        self.assertEqual(CC._slot_index('images.image_3'), 3)
        self.assertEqual(CC._slot_index('image3'), 3)
        self.assertEqual(CC._slot_index('images.image_1'), 1)
        self.assertEqual(CC._slot_index('image'), 0)
        self.assertEqual(CC._slot_index(''), 0)

    def test_node_sort_key(self):
        self.assertEqual(CC._node_sort_key('12'), 12)
        self.assertEqual(CC._node_sort_key(12), 12)
        self.assertEqual(CC._node_sort_key('abc'), 0)
        self.assertEqual(CC._node_sort_key(None), 0)

    def test_negative_slot_empty_is_false(self):
        self.assertFalse(CC._is_negative_slot(''))

    def test_negative_slot_positive_constraint_is_not_negative(self):
        """⚠️ 关键边界：正向约束「不得出现 X」**不是**负向槽位。

        docstring 记录：退回裸关键词命中曾把这类正向提示词判成负向，
        实测导致 77% 的正向提示词被追加裸负面词。
        """
        for text in ('不得出现 watermark', '不得出现水印', '不出现文字'):
            self.assertFalse(CC._is_negative_slot(text),
                             '正向约束 %r 被误判为负向槽位' % text)


class TestStoryboardHelpers(unittest.TestCase):
    """本轮下沉到 storyboard_helpers 的纯函数（下沉前无测试）。"""

    def test_ref_canvas_target(self):
        self.assertEqual(SB._ref_canvas_target((768, 1024)), (768, 1024))
        self.assertEqual(SB._ref_canvas_target([768, 1024]), (768, 1024))
        self.assertIsNone(SB._ref_canvas_target(None))
        self.assertIsNone(SB._ref_canvas_target((0, 100)))
        self.assertIsNone(SB._ref_canvas_target((100,)))
        self.assertIsNone(SB._ref_canvas_target(('a', 'b')))

    def test_blocking_spec_text_returns_str(self):
        for arg in ({}, {'reason': 'x'}, None):
            out = SB._blocking_spec_text(arg)
            self.assertIsInstance(out, str, '_blocking_spec_text 应恒返回 str（入参 %r）' % (arg,))


class TestPromptTidy(unittest.TestCase):
    """prompt_qc._tidy：自愈后留下的标点垃圾清理。"""

    def test_mixed_punctuation_normalized(self):
        """⚠️ docstring 记录的真实缺陷：删词后留下「，,。」全角+半角混排。

        只按「同一字符重复」去重抓不到它（([。；，])\\1+ 匹配不到「，,。」），
        所以必须按「任意标点串归一到最重终止符」处理。"""
        self.assertEqual(Q._tidy('你好，,。世界'), '你好。世界')
        self.assertEqual(Q._tidy('文字，、,;。'), '文字。')

    def test_punctuation_run_collapses_to_terminator(self):
        self.assertEqual(Q._tidy('a。！？；b'), 'a。b')

    def test_all_punctuation_returns_empty(self):
        self.assertEqual(Q._tidy('，，，'), '')
        self.assertEqual(Q._tidy(''), '')

    def test_strips_leading_and_trailing(self):
        self.assertEqual(Q._tidy('  ，测试。， '), '测试。')
        self.assertEqual(Q._tidy('结尾标点，'), '结尾标点')


class TestExtractActions(unittest.TestCase):
    """prompt_qc._extract_actions：从「→」分解里取关键动作。"""

    def test_no_arrow_returns_empty(self):
        self.assertEqual(Q._extract_actions('无箭头'), [])
        self.assertEqual(Q._extract_actions(''), [])

    def test_skips_first_segment(self):
        """箭头**之后**的段才是动作（首段是起点）。"""
        self.assertEqual(Q._extract_actions('站起→转身→走出门'), ['转身', '走出门'])

    def test_short_segments_dropped(self):
        """长度 < 2 的段丢弃；括号备注剔除后变短也算。"""
        self.assertEqual(Q._extract_actions('a→b→a'), [])
        self.assertEqual(Q._extract_actions('A（注）→B'), [])

    def test_dedupes_preserving_order(self):
        self.assertEqual(Q._extract_actions('起点->目标动作->目标动作'), ['目标动作'])


class TestTranslateTokenEn(unittest.TestCase):
    """style_kit._translate_token_en：中文风格 token → 英文短语。"""

    def test_never_leaves_chinese_in_english(self):
        """⚠️ 历史缺陷：「2D现代都市风」翻完只剩「2D风」，英文提示词里混进中文。

        这对图像模型是噪声源，必须保证输出短语**不含任何中文字符**。"""
        for tok in ('2D现代都市风', '中国古风', '国漫3D渲染', '写实摄影风', '古风'):
            for w in S._translate_token_en(tok):
                self.assertFalse(any('\u4e00' <= c <= '\u9fff' for c in w),
                                 '「%s」译出的 %r 仍含中文' % (tok, w))

    def test_longest_key_priority(self):
        """「中国古风」不能被更短的「古风」先吃掉。"""
        out = S._translate_token_en('中国古风')
        self.assertIn('ancient Chinese style', out)

    def test_empty_token(self):
        self.assertEqual(S._translate_token_en(''), [])


class TestSanitizePromptEn(unittest.TestCase):
    """style_kit.sanitize_prompt_en：剥掉模型自写的质量/风格声明。

    docstring 说明：风格与质量声明一律剥掉，改由 with_style_en 在末尾统一给，
    保证风格只出现一次（模型自写会与程序后缀重复）。"""

    def test_strips_quality_words(self):
        self.assertEqual(
            S.sanitize_prompt_en('masterpiece, best quality, a girl with sword'),
            'a girl with sword')

    def test_strips_style_declaration(self):
        self.assertEqual(S.sanitize_prompt_en('Chinese animated style, a boy'), 'a boy')

    def test_empty(self):
        self.assertEqual(S.sanitize_prompt_en(''), '')

if __name__ == '__main__':
    unittest.main()