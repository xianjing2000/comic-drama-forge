# -*- coding: utf-8 -*-
"""设定一致性基线（canon）的回归测试。

背景（用户 2026-10-12 成片质检报告）：
    · 同一集里既有「47层」又有「7层」（画面守则纸写「共四十七层」，配音说「七层楼」）；
    · 工牌印「天合科技」，配音 / 字幕出现「天河科技」。

本文件钉住三条路径（实现任务 t1 的验收口径）：
    ① 设定表加载：落盘 JSON 可解析、四个必备类别齐全、每条事实都有原文出处、
       内嵌兜底副本与 JSON 不漂移、坏文件回落不抛；
    ② 生成期注入：渲染出的设定基准进提示词、日志说明注入了哪几条、未登记小说零变化、
       异常 fail-open；
    ③ 输出期校验：互斥表述判冲突并给明细，且**不误报**（四十七层 / 27层 / 第七层）；
       另外还有一条「引文逐条对回小说原文」的巡检测试（原文不在本机时自动跳过）。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import canon  # noqa: E402


JIN = '进境_整本小说'
NI = '逆天系统_全书'


class TestCanonTableLoad(unittest.TestCase):
    """① 设定表加载 / 结构 / 出处标注 / 兜底副本。"""

    def test_json_file_parses_and_has_books(self):
        raw = io.open(canon.CANON_PATH, encoding='utf-8').read()
        data = json.loads(raw)          # 落盘必须是**合法 JSON**（可读、可 diff、可外部消费）
        self.assertGreaterEqual(data.get('schema_version'), 1)
        keys = [b['key'] for b in data['books']]
        self.assertIn(JIN, keys)
        self.assertIn(NI, keys)

    def test_required_categories_covered(self):
        """四个必备类别（楼层数 / 公司名 / 地址 / 关键数字）必须都有条目。"""
        book = canon.resolve_book(JIN)
        cats = set(canon.categories_of(book))
        for need in canon.REQUIRED_CATEGORIES:
            self.assertIn(need, cats, '设定表缺类别：%s' % need)

    def test_every_fact_has_source_with_quote(self):
        """每条事实都要标注原文出处（文件 + 行号 + 逐字引文）。"""
        for b in canon.books():
            for f in canon.facts_of(b):
                src = f.get('source') or {}
                self.assertTrue(src.get('file'), '%s/%s 缺 source.file' % (b['key'], f['key']))
                self.assertTrue(int(src.get('line') or 0) > 0,
                                '%s/%s 缺 source.line' % (b['key'], f['key']))
                self.assertTrue(str(src.get('quote') or '').strip(),
                                '%s/%s 缺 source.quote' % (b['key'], f['key']))

    def test_floor_and_company_facts_are_the_reported_defect(self):
        """报告里的两条缺陷必须在表里有对应条目（否则「修了缺陷」就无从谈起）。"""
        book = canon.resolve_book(JIN)
        self.assertEqual(canon.fact_value(book, 'floor_total'), '四十七层')
        self.assertIn('七层', canon.fact_by_key(book, 'floor_total')['conflicts'])
        self.assertEqual(canon.fact_value(book, 'company_name'), '天合科技')
        self.assertIn('天河科技', canon.fact_by_key(book, 'company_name')['conflicts'])

    def test_embedded_copy_matches_json_file(self):
        """内嵌兜底副本与落盘 JSON 必须逐字一致 —— 防「两份漂移」（打包版走内嵌）。"""
        on_disk = json.loads(io.open(canon.CANON_PATH, encoding='utf-8').read())
        embedded = canon._embed_default()
        self.assertEqual(embedded, on_disk,
                         'app/canon.py 的内嵌副本与 app/canon_data.json 已漂移，必须同步')

    def test_broken_json_falls_back_to_embedded(self):
        """坏文件（JSON 语法错 / 结构错）必须回落内嵌副本，且**不抛异常**。"""
        for content in ('{ not json', '{"books": "not-a-list"}'):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, 'canon_data.json')
                with io.open(p, 'w', encoding='utf-8') as fh:
                    fh.write(content)
                data = canon.load_canon(p)
                self.assertEqual([b['key'] for b in data['books']],
                                 [b['key'] for b in canon._embed_default()['books']])

    def test_missing_file_falls_back_to_embedded(self):
        data = canon.load_canon(os.path.join(tempfile.gettempdir(), 'no_such_canon_9f3a.json'))
        self.assertTrue(canon.facts_of(canon.resolve_book(JIN, data)))

    def test_normalize_rejects_bad_structure(self):
        with self.assertRaises(canon.CanonError):
            canon._normalize(['not', 'a', 'dict'])
        with self.assertRaises(canon.CanonError):
            canon._normalize({'books': None})

    def test_resolve_book_by_name_title_and_project_key(self):
        for hint in ('进境_整本小说', '《进境》', '《进境》·第一章', '进境_整本小说#ep1'):
            b = canon.resolve_book(hint)
            self.assertIsNotNone(b, hint)
            self.assertEqual(b['key'], JIN)
        for hint in ('逆天系统_全书', '逆天系统', '逆天系统_全书#ep3'):
            self.assertEqual(canon.resolve_book(hint)['key'], NI)

    def test_resolve_book_unknown_returns_none(self):
        """未登记小说必须返回 None（不猜）—— 这是「行为零变化」的前提。"""
        self.assertIsNone(canon.resolve_book('一部没登记过的小说'))
        self.assertIsNone(canon.resolve_book(''))
        self.assertIsNone(canon.resolve_book(None))

    def test_facts_summary_lists_categories_with_values(self):
        s = canon.facts_summary(canon.resolve_book(JIN))
        joined = '；'.join(s)
        for token in ('楼层数=四十七层', '公司名=天合科技', '地址=江城东区观澜路四十七号'):
            self.assertIn(token, joined)


class TestCanonInject(unittest.TestCase):
    """② 生成期注入（渲染 + 追加 + 日志「注入了哪几条」+ fail-open）。"""

    def setUp(self):
        self.book = canon.resolve_book(JIN)

    def test_render_block_contains_values_and_bans(self):
        block = canon.render_canon_block(self.book)
        self.assertIn('【本剧设定基准', block)
        for token in ('楼层数：四十七层', '公司名：天合科技', '地址：江城东区观澜路四十七号',
                      '关键数字：27楼', '禁写', '七层', '天河科技'):
            self.assertIn(token, block)

    def test_unregistered_book_renders_empty(self):
        self.assertEqual(canon.render_canon_block(None), '')
        self.assertEqual(canon.render_canon_block({'key': 'x', 'facts': []}), '')

    def test_inject_appends_block_and_logs_which_facts(self):
        with self.assertLogs('canon', level='INFO') as cm:
            out = canon.inject_canon_into_prompt('BASE-PROMPT', self.book, label='shots#1')
        self.assertTrue(out.startswith('BASE-PROMPT'))
        self.assertIn('【本剧设定基准', out)
        self.assertGreater(len(out), len('BASE-PROMPT'))
        log = '\n'.join(cm.output)
        self.assertIn('[canon注入]', log)
        self.assertIn('shots#1', log)
        self.assertIn('楼层数=四十七层', log)      # 「注入了哪几条」必须能看到
        self.assertIn('公司名=天合科技', log)

    def test_inject_records_event(self):
        events = []
        canon.inject_canon_into_prompt('P', self.book, label='shots#7', events=events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'canon_inject')
        self.assertEqual(events[0]['label'], 'shots#7')
        self.assertIn('floor_total', events[0]['facts'])

    def test_inject_noop_without_book(self):
        for book in (None, {}, {'key': 'x', 'facts': []}):
            self.assertEqual(canon.inject_canon_into_prompt('P', book), 'P')
        self.assertEqual(canon.inject_canon_into_prompt('', self.book), '')

    def test_inject_fail_open_on_exception(self):
        class Boom(dict):
            def get(self, k, d=None):
                raise RuntimeError('boom')

        self.assertEqual(canon.inject_canon_into_prompt('P', Boom()), 'P')


class TestCanonConflictDetection(unittest.TestCase):
    """③ 输出期校验：判冲突 + 给明细 + 不误报。"""

    def setUp(self):
        self.book = canon.resolve_book(JIN)

    def test_47_vs_7_is_contradiction_with_detail(self):
        """验收口径：含「47层」与「7层」的文本必须判冲突，并给出冲突明细。"""
        text = '画面：守则纸上写着本栋楼共47层。配音：「这是7层，别上去。」'
        cs = canon.detect_conflicts(text, self.book)
        self.assertEqual(len(cs), 1, cs)
        c = cs[0]
        self.assertEqual(c['key'], 'floor_total')
        self.assertEqual(c['category'], '楼层数')
        self.assertEqual(c['kind'], 'contradiction')
        self.assertEqual(c['value'], '四十七层')
        self.assertIn(c['form'], ('7层', '七层', '7楼', '七楼'))
        self.assertEqual(c['count'], 1)
        self.assertTrue(c['contexts'][0])
        self.assertEqual(c['source']['line'], 102)
        self.assertIn('四十七层', c['message'])

    def test_chinese_numerals_also_detected(self):
        text = '本栋楼共四十七层。旁白却说这楼七层。'
        cs = canon.detect_conflicts(text, self.book)
        self.assertEqual([c['kind'] for c in cs], ['contradiction'])
        self.assertEqual(cs[0]['form'], '七层')

    def test_forbidden_form_alone_is_reported(self):
        cs = canon.detect_conflicts('配音台词：这是七层楼，电梯不会停。', self.book)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]['kind'], 'forbidden_form')
        self.assertEqual(cs[0]['form'], '七层')

    def test_company_variant_is_reported(self):
        cs = canon.detect_conflicts('工牌上印着天河科技四个字。', self.book)
        self.assertEqual([c['key'] for c in cs], ['company_name'])
        self.assertEqual(cs[0]['kind'], 'forbidden_form')
        self.assertEqual(cs[0]['source']['line'], 57)

    def test_no_false_positive_on_legit_numbers(self):
        """假阳性防线：四十七层 / 二十七层 / 27层 / 2704 / 第七层（测试层级）都不该告警。"""
        for text in ('1. 本栋楼共四十七层，请勿前往四十八层及以上。',
                     '6 请勿在二十七层停留超过十分钟',
                     '27层·2704',
                     '他上了27楼，进了2704号房间。',
                     '第四层：便利店。',
                     '第五层：地铁站。',
                     '这是第七层：便利店。',
                     '守则v1.0到v5.0，五份守则，五层陷阱。'):
            self.assertEqual(canon.detect_conflicts(text, self.book), [],
                             '误报：%s' % text)

    def test_address_variants(self):
        self.assertEqual(canon.detect_conflicts('地址：观澜路四十七号', self.book), [])
        cs = canon.detect_conflicts('地址：观澜路十七号', self.book)
        self.assertEqual([c['key'] for c in cs], ['address'])

    def test_detect_accepts_novel_name_and_unknown(self):
        self.assertEqual(len(canon.detect_conflicts('共七层', JIN)), 1)
        self.assertEqual(canon.detect_conflicts('共七层', '未登记小说'), [])

    def test_format_conflicts_lines(self):
        cs = canon.detect_conflicts('本栋楼共47层。这是7层。', self.book)
        lines = canon.format_conflicts(cs)
        self.assertEqual(len(lines), 1)
        self.assertIn('[canon冲突]', lines[0])
        self.assertIn('floor_total', lines[0])
        self.assertIn(':第102行', lines[0])

    def test_validate_text_never_raises(self):
        r = canon.validate_text('本栋楼共47层。这是7层。', novel=JIN)
        self.assertFalse(r['ok'])
        self.assertEqual(r['novel'], JIN)
        self.assertTrue(r['conflicts'])
        self.assertEqual(r['error'], '')
        ok = canon.validate_text('本栋楼共四十七层。', novel=JIN)
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['conflicts'], [])

    def test_validate_script_reports_where(self):
        script = {
            'title': '《进境》',
            'items': [{'name': '员工卡', 'surface_text': '天合科技 姓名 羡进 员工证'}],
            'shots': [
                {'description': '守则纸上写着共四十七层。', 'dialogue': []},
                {'description': '他把卡举起来。',
                 'dialogue': [{'speaker': '羡进', 'text': '天河科技……我的照片？'}]},
                {'caption': {'text': '27层', 'kind': '时间地点'}, 'dialogue': []},
            ],
        }
        r = canon.validate_script(script, novel=JIN)
        self.assertFalse(r['ok'])
        self.assertEqual([c['key'] for c in r['conflicts']], ['company_name'])
        c = r['conflicts'][0]
        self.assertEqual(c['where'], ['shots[1].dialogue[0].text'])
        self.assertEqual(c['kind'], 'forbidden_form')

    def test_iter_script_texts_covers_key_fields(self):
        script = {
            'items': [{'surface_text': 'S1'}],
            'scenes': [{'surface_text': 'S2'}],
            'characters': [{'identity': 'I1'}],
            'shots': [{'description': 'D1', 'visual_detail': 'V1', 'surface_text': 'ST1',
                       'audio_cues': 'A1', 'caption_text': 'C1',
                       'caption': {'text': 'CT1'},
                       'dialogue': [{'text': 'L1'}, 'L2']}],
        }
        got = dict(canon.iter_script_texts(script))
        for pos in ('items[0].surface_text', 'scenes[0].surface_text',
                    'characters[0].identity', 'shots[0].description',
                    'shots[0].visual_detail', 'shots[0].surface_text',
                    'shots[0].audio_cues', 'shots[0].caption_text',
                    'shots[0].caption.text', 'shots[0].dialogue[0].text',
                    'shots[0].dialogue[1]'):
            self.assertIn(pos, got)
        self.assertEqual([t for _, t in canon.iter_script_texts('RAW')], ['RAW'])
        self.assertEqual(list(canon.iter_script_texts(None)), [])


class TestCanonSourcesMatchNovelOriginal(unittest.TestCase):
    """巡检验证：表里的引文必须真的在小说原文对应行上（原文不在本机则跳过）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = canon.verify_sources()
        if cls.report['ok'] == 0:
            raise unittest.SkipTest(
                '本机找不到小说原文（可设 MJSCXT_CANON_NOVEL_DIR 指定 novels 目录）：%s'
                % (cls.report['missing_files'],))

    def test_all_quotes_match_declared_lines(self):
        self.assertEqual(self.report['bad'], [],
                         '设定表引文与原文对不上（行号漂了 / 引文抄错）：%s'
                         % json.dumps(self.report['bad'], ensure_ascii=False))

    def test_quotes_are_many(self):
        self.assertGreaterEqual(self.report['ok'], 10)


_MIN_SHOT = {
    'camera': '中景固定', 'shot_type': '中景', 'emotion': '警惕', 'beat': '触发',
    'description': '他把手伸进口袋→掏出员工卡→举到画面正中，卡片正面清晰可见，人物在画面左侧。',
    'dialogue': [],
}


class TestCanonWiredIntoGeneration(unittest.TestCase):
    """端到端接线：分镜提示词生成时真的注入了设定表（mock 掉模型调用，只看 prompt）。"""

    def _run(self, bible_title, canon_book=None):
        import logging

        import novel_to_script as N

        captured = {}
        lines = []

        class _Collect(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        def _fake_robust(client, prompt, **kw):
            captured['prompt'] = prompt
            return {'shots': [dict(_MIN_SHOT)]}

        # ⚠️ 注入日志打在 canon 这个 logger 上，assertLogs('novel_to_script') 收不到它
        # —— 所以这里自己在 root 上挂 handler，按「实际打了什么」断言。
        root = logging.getLogger()
        old_level, handler = root.level, _Collect()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            with mock.patch.object(N, '_robust_json', _fake_robust):
                N.build_shots_for_chunk(
                    object(),
                    {'title': bible_title, 'characters': [], 'items': [], 'scenes': []},
                    {'summary': '摘要', 'key_beats': []},
                    {'index': 1, 'total': 1, 'title': '第1章', 'text': '原文' * 80},
                    1, _no_heal=True, canon_book=canon_book)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        return captured.get('prompt', ''), '\n'.join(lines)

    def test_prompt_gets_canon_block_and_log_lists_facts(self):
        prompt, log = self._run('《进境》')
        self.assertIn('【本剧设定基准', prompt)
        self.assertIn('楼层数：四十七层', prompt)
        self.assertIn('禁写', prompt)
        self.assertIn('[canon注入]', log)
        self.assertIn('shots#1', log)
        self.assertIn('楼层数=四十七层', log)

    def test_unregistered_novel_prompt_unchanged(self):
        prompt, _log = self._run('一部没登记过的小说')
        self.assertNotIn('【本剧设定基准', prompt)

    def test_explicit_canon_book_wins_over_title(self):
        """③ 逐块路径显式传入的设定表优先于 bible.title 猜测（标题被模型改名也不失效）。"""
        prompt, log = self._run('模型乱起的剧名', canon_book=canon.resolve_book(JIN))
        self.assertIn('【本剧设定基准', prompt)
        self.assertIn('楼层数=四十七层', log)

    def test_qc_helpers_exposes_canon_checks(self):
        import qc_helpers as Q
        r = Q._qc_canon_conflicts('本栋楼共47层。这是7层。', novel=JIN)
        self.assertFalse(r['ok'])
        self.assertEqual(r['novel'], JIN)
        r2 = Q._qc_canon_scan_shots([{'description': '共四十七层'}, {'description': '天河科技'}],
                                    novel=JIN)
        self.assertFalse(r2['ok'])
        self.assertEqual([c['form'] for c in r2['conflicts']], ['天河科技'])
        self.assertTrue(Q._qc_canon_conflicts(None)['ok'])

    def test_qc_canon_fail_open(self):
        import qc_helpers as Q
        with mock.patch.object(Q.canon, 'validate_script', side_effect=RuntimeError('boom')):
            r = Q._qc_canon_conflicts({'shots': []}, novel=JIN)
        self.assertTrue(r['ok'])
        self.assertIn('RuntimeError', r['error'])
        self.assertEqual(Q._qc_canon_prompt_block('未登记小说'), '')


if __name__ == '__main__':
    unittest.main()
