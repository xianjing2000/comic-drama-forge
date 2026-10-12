# -*- coding: utf-8 -*-
"""T10：设定表注入 H3 视频提示词层（canon x canon_facts_block）的回归测试。

背景（用户 2026-10-12 成片质检报告）：
    画面守则纸写「共四十七层」（小说原文，正确），配音却说「七层楼」；
    工牌印「天合科技」，配音 / 字幕出现「天河科技」。

链路真相（T10 立项理由，已核实）：
    · asset_refs._h3_shot_ref_components 只把**本镜出场文字道具的 surface_text**
      收进 shot["_canon_facts"]；
    · h3_prompt_kit.canon_facts_block 只把 _canon_facts 排成一句话；
    · 而 canon_facts_block **只挂在 build_base**（无参考图的 T2VA 分支），
      带参考图的主路径走 build_ref2va —— 也就是说主路径一直是「零事实基准」。

本文件钉住四件事：
    ① 项目级注入：无文字道具的镜头（原链路盲区）也能拿到设定基准；
    ② 幂等：surface_text 已逐字写了同一事实 → 不重复堆叠（重复堆叠会稀释画面重点）；
    ③ 按镜选择性：不涉及设定的镜头**不注入**（提示词不无谓变大）；
    ④ 挂点：asset_refs 把项目名挂到 shot 上（两条调用路径都能拿到），且保持赋值语义。
"""
import logging
import os
import sys
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import asset_refs as AR      # noqa: E402
import canon                 # noqa: E402
import h3_prompt_kit as K    # noqa: E402

BOOK = '进境_整本小说'

#: 真实形态的物品索引（url 由 _build_asset_index 生成，带项目指纹）
ITEM_IDX = {
    '天合科技员工卡': {
        'name': '天合科技员工卡',
        'surface_text': '天合科技 姓名 羡进 员工证，请勿转借',
        'image': '',
        '_dir': '',
        'url': '/api/assets/items/进境_整本小说/天合科技员工卡/front.png',
    },
    '楼层安全守则': {
        'name': '楼层安全守则',
        'surface_text': '1. 本栋楼共四十七层，请勿前往四十八层及以上。 2. 电梯仅停靠偶数层。',
        'image': '',
        '_dir': '/data/output/assets/items/进境_整本小说/楼层安全守则',
        'url': '',
    },
}


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class _LogCtx:
    """收集 root 上的日志（canon / h3_prompt_kit 都经 root 传播）。"""

    def __enter__(self):
        self.h = _Collect()
        self.root = logging.getLogger()
        self.old = self.root.level
        self.root.addHandler(self.h)
        self.root.setLevel(logging.INFO)
        return self.h

    def __exit__(self, *a):
        self.root.removeHandler(self.h)
        self.root.setLevel(self.old)


class TestProjectHint(unittest.TestCase):
    """挂点①：项目名反解（视频 worker 那条路径不传 project_name）。"""

    def test_from_url(self):
        self.assertEqual(AR._h3_project_hint({}, ITEM_IDX, {}), BOOK)

    def test_from_dir_and_image(self):
        self.assertEqual(AR._h3_project_hint(
            {}, {'道具': {'_dir': '/d/output/assets/items/逆天系统_全书/筑基丹'}}, {}),
            '逆天系统_全书')
        self.assertEqual(AR._h3_project_hint(
            {'角色': {'image': r'C:\d\output\assets\characters\进境_整本小说\羡进\front.png'}},
            {}, {}), BOOK)

    def test_empty_when_no_index(self):
        self.assertEqual(AR._h3_project_hint({}, {}, {}), '')
        self.assertEqual(AR._h3_project_hint(None, [], 'x'), '')


class TestShotRefComponentsHooks(unittest.TestCase):
    """挂点②：shot['_canon_project'] 落点 + _canon_facts 保持赋值语义。"""

    def _shot(self, items):
        return {'shot_id': 'shot_01', 'items_in_shot': items, 'characters_in_shot': []}

    def test_sets_project_and_facts(self):
        shot = self._shot(['天合科技员工卡'])
        comps = AR._h3_shot_ref_components(shot, {}, ITEM_IDX, {})
        self.assertEqual(shot.get('_canon_project'), BOOK)
        self.assertEqual(len(shot.get('_canon_facts') or []), 1)
        self.assertIn('天合科技', shot['_canon_facts'][0])
        self.assertTrue(all(c.get('kind') == 'item' for c in comps))

    def test_repeat_call_does_not_grow(self):
        """赋值语义：重复解析不增长（对比 shot['_ref_warnings'] 的追加语义）。"""
        shot = self._shot(['天合科技员工卡', '楼层安全守则'])
        AR._h3_shot_ref_components(shot, {}, ITEM_IDX, {})
        first = list(shot['_canon_facts'])
        for _ in range(3):
            AR._h3_shot_ref_components(shot, {}, ITEM_IDX, {})
        self.assertEqual(shot['_canon_facts'], first)

    def test_explicit_project_name_wins(self):
        shot = self._shot([])
        AR._h3_shot_ref_components(shot, {}, {}, {}, project_name='逆天系统_全书')
        self.assertEqual(shot.get('_canon_project'), '逆天系统_全书')

    def test_no_hint_no_field(self):
        shot = self._shot([])
        AR._h3_shot_ref_components(shot, {}, {}, {})
        self.assertNotIn('_canon_project', shot)


class TestH3CanonFactsBlock(unittest.TestCase):
    """核心：按镜选择性注入 + 幂等 + 上限。"""

    def test_blind_spot_shot_gets_baseline(self):
        """原链路盲区：本镜没有文字道具，但台词说了错形「七层楼」。"""
        shot = {'shot_id': 'shot_07', '_canon_project': BOOK,
                'description': '电梯门开，羡进抬头看楼层显示屏。',
                'dialogue': [{'speaker': '羡进', 'text': '这是七层楼，电梯不会停。'}]}
        with _LogCtx() as log:
            blk = K.canon_facts_block(shot)
        self.assertIn('设定｜楼层数=四十七层', blk)
        self.assertIn('七层', blk)                  # 禁写项被列出
        self.assertIn('必须写成「四十七层」', blk)
        joined = '\n'.join(log.lines)
        self.assertIn('[canon注入·H3]', joined)
        self.assertIn('shot_07', joined)
        self.assertIn('楼层数=四十七层←七层@dialogue[0].text', joined)   # 注入了哪几条 + 触发点

    def test_canonical_value_in_description_hits(self):
        shot = {'shot_id': 's2', '_canon_project': BOOK,
                'description': '羡进把天合科技员工卡举到画面正中。'}
        blk = K.canon_facts_block(shot)
        self.assertIn('设定｜公司名=天合科技', blk)

    def test_unrelated_shot_not_injected(self):
        shot = {'shot_id': 's3', '_canon_project': BOOK,
                'description': '走廊尽头，他握紧了拳头。',
                'dialogue': [{'speaker': '羡进', 'text': '我不走。'}]}
        self.assertEqual(K.canon_facts_block(shot), '')

    def test_no_project_no_injection(self):
        shot = {'shot_id': 's4', 'description': '电梯里有人说这是七层楼。'}
        self.assertEqual(K.canon_facts_block(shot), '')

    def test_unregistered_project_no_injection(self):
        shot = {'shot_id': 's5', '_canon_project': '没登记的小说',
                'description': '共四十七层。'}
        self.assertEqual(K.canon_facts_block(shot), '')

    def test_idempotent_when_surface_text_already_has_value(self):
        """幂等：本镜描述与 surface_text 提到同一事实时，只保留 surface_text 那一份。

        （真实形态：第1集 shot_21 的 description 写「共四十七层、请勿进入2704…」，
          而它的 surface_text 是回填的守则原文 —— 不做去重就会把「四十七层」写两遍。）
        """
        shot = {'shot_id': 's6', '_canon_project': BOOK,
                '_canon_facts': ['楼层安全守则：1. 本栋楼共四十七层，请勿前往四十八层及以上。'],
                'description': '镜头推至守则纸面，共四十七层、请勿进入2704 四条规则清晰可读。'}
        with _LogCtx() as log:
            blk = K.canon_facts_block(shot)
        # 被去重的那条：只剩 surface_text 里的一份，不重复堆叠
        self.assertNotIn('设定｜楼层数', blk)
        self.assertEqual(blk.count('四十七层'), 1)
        # 同镜里真正「涉及但 surface_text 没写」的那条照常注入（2704）
        self.assertIn('设定｜关键数字=2704', blk)
        joined = '\n'.join(log.lines)
        self.assertIn('幂等跳过 1 条', joined)
        self.assertIn('关键数字=2704←2704@description', joined)

    def test_conflict_still_injected_even_if_surface_text_has_value(self):
        """例外：本镜出现错形时按「纠正」处理，不因 surface_text 已有规范值而跳过。"""
        shot = {'shot_id': 's7', '_canon_project': BOOK,
                '_canon_facts': ['楼层安全守则：1. 本栋楼共四十七层。'],
                'dialogue': [{'speaker': '女人', 'text': '这是七层楼。'}]}
        blk = K.canon_facts_block(shot)
        self.assertIn('设定｜楼层数=四十七层', blk)
        self.assertIn('必须写成「四十七层」', blk)

    def test_surface_text_alone_keeps_old_output(self):
        """未命中项目级时，输出与 T10 之前逐字一致（纯 _canon_facts 那句）。"""
        shot = {'shot_id': 's8', '_canon_facts': ['员工卡：天合科技 姓名 羡进']}
        blk = K.canon_facts_block(shot)
        self.assertTrue(blk.endswith('员工卡：天合科技 姓名 羡进.'))
        self.assertNotIn('设定｜', blk)

    def test_char_cap_drops_extra_facts(self):
        """字符上限：事实条数很多时截断并记账（dropped），且总量不超上限。"""
        facts = []
        vals = []
        for i in range(40):
            v = '项目数字%02d' % i
            vals.append(v)
            facts.append({'key': 'k%d' % i, 'category': '关键数字',
                          'value': v, 'aliases': [v], 'conflicts': []})
        fake = {'key': '假书', 'facts': facts}
        shot = {'shot_id': 's9', '_canon_project': '假书',
                'description': '、'.join(vals)}
        with mock.patch.object(canon, 'resolve_book', return_value=fake):
            lines, notes, skipped, dropped, book, skip_notes = \
                K._h3_project_fact_lines(shot)
        self.assertEqual(book, '假书')
        self.assertGreater(len(lines), 0)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(sum(len(x) for x in lines) + len(lines), K.H3_CANON_MAX_CHARS)
        self.assertEqual(len(notes), len(lines))

    def test_fail_open_on_bad_book(self):
        shot = {'shot_id': 's10', '_canon_project': BOOK, 'description': '共四十七层'}
        with mock.patch.object(canon, 'facts_of', side_effect=RuntimeError('boom')):
            self.assertEqual(K.canon_facts_block(shot), '')


class TestH3PromptWiring(unittest.TestCase):
    """接线：build_base（无参考图分支）与 build_ref2va（带参考图主路径）都要有基准。"""

    BLIND = {'shot_id': 'shot_07', 'duration': 5,
             'description': '电梯门开，羡进抬头看楼层显示屏。',
             'dialogue': [{'speaker': '羡进', 'text': '这是七层楼，电梯不会停。'}]}
    PICS = [('<Picture 1>', 'storyboard ref'), ('<Picture 2>', 'character ref')]
    SUBJS = [{'name': '羡进', 'desc': 'young man'}]

    def test_build_base_gets_project_facts(self):
        before = K.build_base(dict(self.BLIND), 'T2VA', style='3D动漫渲染')
        after = K.build_base({**self.BLIND, '_canon_project': BOOK}, 'T2VA', style='3D动漫渲染')
        self.assertNotIn('设定｜', before)
        self.assertIn('设定｜楼层数=四十七层', after)
        self.assertGreater(len(after), len(before))

    def test_build_ref2va_main_path_gets_project_facts(self):
        """主路径（带参考图）此前**完全没有** canon 注入 —— 本用例钉住它现在有。"""
        before = K.build_ref2va(dict(self.BLIND), self.PICS, self.SUBJS, style='3D动漫渲染')
        after = K.build_ref2va({**self.BLIND, '_canon_project': BOOK}, self.PICS, self.SUBJS,
                               style='3D动漫渲染')
        self.assertNotIn('设定｜', before)
        self.assertIn('设定｜楼层数=四十七层', after)
        self.assertIn('detailed_description:', after)

    def test_build_ref2va_unrelated_shot_unchanged(self):
        shot = {'shot_id': 's3', 'duration': 5, 'description': '走廊尽头，他握紧了拳头。',
                'dialogue': [{'speaker': '羡进', 'text': '我不走。'}]}
        a = K.build_ref2va(dict(shot), self.PICS, self.SUBJS, style='3D动漫渲染')
        b = K.build_ref2va({**shot, '_canon_project': BOOK}, self.PICS, self.SUBJS,
                           style='3D动漫渲染')
        self.assertEqual(a, b)          # 不涉及设定 → 提示词逐字不变

    def test_repeat_build_is_byte_identical(self):
        """幂等：同一 shot 连续构建两次产物逐字相同（不会越拼越长）。"""
        shot = {**self.BLIND, '_canon_project': BOOK}
        one = K.build_ref2va(shot, self.PICS, self.SUBJS, style='3D动漫渲染')
        two = K.build_ref2va(shot, self.PICS, self.SUBJS, style='3D动漫渲染')
        self.assertEqual(one, two)

    def test_sections_still_valid(self):
        """注入不破坏六段结构（H3 validate 必须仍然通过）。"""
        shot = {**self.BLIND, '_canon_project': BOOK}
        built = K.build_ref2va(shot, self.PICS, self.SUBJS, style='3D动漫渲染')
        verdict = K.validate(built)
        self.assertTrue(verdict['valid'], verdict)


class TestH3CanonTextsDialogueForms(unittest.TestCase):
    """t19：_h3_canon_texts 对 dialogue 的三种形态（list / str / None）。

    verifier-2 在 t7 报的 O-1：只处理 list 形态；裸字符串会被 enumerate **逐字符拆分**
    （'七层楼' → '七'/'层'/'楼'），于是「七层」这类禁写错形再也命中不到 → 纠错静默失效。
    真实剧本 145 镜当前 str 形态 = 0（数据形态侥幸，不是代码正确），所以修它必须同时证明
    ①list 形态零回归 ②str 形态真修复。
    """

    def test_list_form_unchanged(self):
        """list 形态：字段名与文本必须与修复前逐字一致（零回归硬底线）。"""
        shot = {'shot_id': 1, 'dialogue': [{'speaker': '羡进', 'text': '我们上到七层楼就到了'}]}
        self.assertEqual([t for t in K._h3_canon_texts(shot) if t[0].startswith('dialogue')],
                         [('dialogue[0].text', '我们上到七层楼就到了')])

    def test_list_of_plain_strings_unchanged(self):
        """list 里放裸字符串（非 dict）的既有分支也必须不变。"""
        shot = {'shot_id': 1, 'dialogue': ['第一句', '第二句']}
        self.assertEqual([t for t in K._h3_canon_texts(shot) if t[0].startswith('dialogue')],
                         [('dialogue[0]', '第一句'), ('dialogue[1]', '第二句')])

    def test_bare_string_is_one_text_not_chars(self):
        """str 形态：整条文本，不得逐字符拆分 —— 本用例钉死这一点。"""
        shot = {'shot_id': 3, 'dialogue': '七层楼'}
        got = [t for t in K._h3_canon_texts(shot) if t[0].startswith('dialogue')]
        self.assertEqual(got, [('dialogue', '七层楼')])
        self.assertEqual(len(got), 1)
        # 逐字符拆分的特征：每个文本长度为 1 —— 必须不成立
        self.assertFalse(all(len(t[1]) == 1 for t in got))
        self.assertEqual(''.join(t[1] for t in got), '七层楼')

    def test_bare_string_conflict_is_detected(self):
        """真修复：裸字符串里的错形现在能被检测并注入纠正。"""
        shot = {'shot_id': 3, '_canon_project': BOOK, 'dialogue': '我们上到七层楼就到了'}
        blk = K.canon_facts_block(shot)
        self.assertIn('设定｜楼层数=四十七层', blk)
        self.assertIn('必须写成「四十七层」', blk)

    def test_bare_string_equals_list_form(self):
        """同一句台词：str 形态与 list 形态必须产出同一个 block（形态不该影响结果）。"""
        same = '我们上到七层楼就到了'
        as_str = K.canon_facts_block({'shot_id': 3, '_canon_project': BOOK, 'dialogue': same})
        as_list = K.canon_facts_block(
            {'shot_id': 3, '_canon_project': BOOK,
             'dialogue': [{'speaker': '羡进', 'text': same}]})
        self.assertEqual(as_str, as_list)
        self.assertNotEqual(as_str, '')

    def test_empty_and_blank_string_skipped(self):
        """空串 / 纯空白不产生条目（与 list 形态的空元素一致）。"""
        self.assertEqual([t for t in K._h3_canon_texts({'dialogue': ''})
                          if t[0].startswith('dialogue')], [])
        self.assertEqual([t for t in K._h3_canon_texts({'dialogue': '   '})
                          if t[0].startswith('dialogue')], [])

    def test_none_and_missing_unchanged(self):
        """None / 缺失：不产生任何 dialogue 条目（既有行为）。"""
        self.assertEqual([t for t in K._h3_canon_texts({'dialogue': None})
                          if t[0].startswith('dialogue')], [])
        self.assertEqual([t for t in K._h3_canon_texts({})
                          if t[0].startswith('dialogue')], [])

    def test_non_dict_shot_still_empty(self):
        self.assertEqual(K._h3_canon_texts(None), [])
        self.assertEqual(K._h3_canon_texts('not a shot'), [])

    def test_unrelated_bare_string_not_injected(self):
        """不误命中：裸字符串里没有设定词时仍不注入。"""
        shot = {'shot_id': 5, '_canon_project': BOOK, 'dialogue': '我不走。'}
        self.assertEqual(K.canon_facts_block(shot), '')


if __name__ == '__main__':
    unittest.main()
