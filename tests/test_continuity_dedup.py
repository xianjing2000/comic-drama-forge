# -*- coding: utf-8 -*-
r"""跨集台词重复检测（任务 t2）回归测试。

用户反馈「台词跨集重复」：第 N 集念过的台词，第 N+1 集又原样念一遍。
旧实现只有 continuity.rule_dialogue_dedup（**只看本集内部**），跨集重复没有拦截点。

本文件钉住四条验收口径：
  ① 新台词生成后自动写入 quotes.json（episode_no 正确、last_checked_episode 递增）；
  ② 可调用的重复检测接口（输入候选台词 → 是否与历史集重复 + 来源：哪一集哪一句）；
  ③ 真实已存在重复样本能命中，且误报率为 0（明显不同的台词不判重）；
  ④ 无重复 / 精确重复 / 近似重复 三类路径各有单测。

真实样本出处（本机真实剧本，非构造数据；文件缺失时相关用例自动 skip）：
  · 进境_整本小说 第2集「2704守则」(109 镜) / 第9集「进境·隧道」(48 镜)
    %APPDATA%\mjscxt-desktop\_script_backup_20261010173117\第2集.json、第9集.json
      - 精确重复：第2集#79「什么意思？」 ↔ 第9集#11「什么意思？」
      - 近似重复：第2集#95「守则不是筛选工具，是陷阱的说明书。」
                 ↔ 第9集#24「守则不是筛选工具。守则是陷阱的说明书。」（2-gram Jaccard 0.8125）
  · 无重复对照组：逆天系统_全书 第1集(46 镜/47 句) vs 第2集(39 镜/37 句)
    %APPDATA%\mjscxt-desktop\mjscxt-data\output\scripts\逆天系统_全书\
      - 47×37 = 1739 组有序配对 → 判定 0 命中（最像"同一句话"的那组只有 0.50，被阈值挡掉）
  · 全语料扫描（2026-10-12 实测，见 TestRealCorpusSweep）：8 个真实剧本语料 /
    约 15502 组有序配对 → 命中 64 条，逐条独立复算**全部为真重复**（误报 0）。

⚠️ 阈值标定证据（两个方向都实测过，别再"顺手调低"）：
    真重复「守则不是筛选工具…」0.8125 能命中；
    无关台词「这个废物……怎么会？」vs「这个废物……」= 0.50；
    无关台词「你……你到底是谁？」vs「你到底是什么人？」= 0.375。
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import continuity  # noqa: E402
import continuity_helpers as H  # noqa: E402


APPDATA = os.environ.get('APPDATA') or os.path.expanduser('~\\AppData\\Roaming')
JIN_BACKUP = os.path.join(APPDATA, 'mjscxt-desktop', '_script_backup_20261010173117')
NI_SCRIPTS = os.path.join(APPDATA, 'mjscxt-desktop', 'mjscxt-data', 'output', 'scripts',
                          '逆天系统_全书')

# ---- 真实台词样本（逐字抄自上面标注的真实剧本文件；不要在测试里改写它们）----
JIN_EP2 = '第2集.json'
JIN_EP9 = '第9集.json'
REAL_EXACT = '什么意思？'                                     # ep2#79 ↔ ep9#11
REAL_NEAR_A = '守则不是筛选工具，是陷阱的说明书。'              # ep2#95
REAL_NEAR_B = '守则不是筛选工具。守则是陷阱的说明书。'          # ep9#24
# 逆天系统 ep1/ep2 里"最像重复"的两组：相似度分别只有 0.50 / 0.375 → 必须判为不重复
REAL_DIFFERENT = [
    ('这个废物……怎么会？', '这个废物……'),
    ('你……你到底是谁？', '你到底是什么人？'),
]


def _plain_ref(text):
    """测试内**独立**实现：去标点只留中英数字（用于第三方复算，不调用被测模块）。"""
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '', str(text or ''))


def _sim_ref(a, b):
    """测试内**独立**实现的 2-gram Jaccard（判定"真重复"的复核口径，不调用被测模块）。"""
    a, b = _plain_ref(a), _plain_ref(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / max(1, len(ga | gb))


def _ledger_row(text, episode_no, shot_id=None, speaker=''):
    return {'episode_no': episode_no, 'text': text, 'speaker_hint': speaker,
            'shot_id': shot_id, 'source': H.SOURCE_SCRIPT, 'in_script': True}


def _script(shots, characters=None, episode_no=2):
    return {'episode_no': episode_no, 'characters': characters or [], 'shots': shots}


def _load_json(path):
    with io.open(path, encoding='utf-8') as f:
        return json.load(f)


# ============================ ① 纯判定（无 IO） ============================

class TestJudgeDuplicate(unittest.TestCase):
    """judge_duplicate：无重复 / 精确重复 / 近似重复三条路径 + 阈值边界。"""

    def test_exact_duplicate_real_sample(self):
        """真实精确重复样本：进境 ep2#79 ↔ ep9#11。"""
        v = H.judge_duplicate(REAL_EXACT, REAL_EXACT)
        self.assertTrue(v['duplicate'])
        self.assertEqual(v['kind'], 'exact')
        self.assertEqual(v['score'], 1.0)

    def test_exact_duplicate_ignores_punctuation(self):
        """去标点后相同即精确重复（「你退下罢。」是真实剧本里的跨集重复）。"""
        v = H.judge_duplicate('你退下罢。', '你退下罢')
        self.assertTrue(v['duplicate'])
        self.assertEqual(v['kind'], 'exact')

    def test_near_duplicate_real_sample(self):
        """真实近似重复样本：「守则不是筛选工具，是陷阱的说明书。」↔「…。守则是陷阱的说明书。」"""
        v = H.judge_duplicate(REAL_NEAR_A, REAL_NEAR_B)
        self.assertTrue(v['duplicate'])
        self.assertEqual(v['kind'], 'near')
        self.assertGreaterEqual(v['score'], H.THRESHOLDS["near_sim"])
        self.assertLess(v['score'], 1.0)

    def test_no_duplicate_on_real_different_lines(self):
        """误报率为 0：真实剧本里最像"重复"的两组无关台词必须判为不重复。"""
        for a, b in REAL_DIFFERENT:
            with self.subTest(pair=(a, b)):
                v = H.judge_duplicate(a, b)
                self.assertFalse(v['duplicate'], '%s / %s 被误判为重复（%s）' % (a, b, v['reason']))
        self.assertLess(H.similar_text(*REAL_DIFFERENT[0]), H.THRESHOLDS["near_sim"])
        self.assertLess(H.similar_text(*REAL_DIFFERENT[1]), H.THRESHOLDS["near_sim"])

    def test_short_lines_only_match_exactly(self):
        """短句（<8 字）只认精确：既不漏真重复，也不让无关短句互判。"""
        self.assertFalse(H.judge_duplicate('你走吧。', '你留下。')['duplicate'])
        self.assertFalse(H.judge_duplicate('好。', '好？')['duplicate'])   # 过短，不参与判重

    def test_length_ratio_guard(self):
        """篇幅差太多（长度比 < 0.7）不是同一句的改写。"""
        short, long_ = '我不知道。', '我不知道，我什么都不知道，你别再逼我了好不好？'
        self.assertLess(min(len(_plain_ref(short)), len(_plain_ref(long_)))
                        / max(len(_plain_ref(short)), len(_plain_ref(long_))), H.THRESHOLDS["near_len_ratio"])
        self.assertFalse(H.judge_duplicate(short, long_)['duplicate'])

    def test_empty_and_none(self):
        self.assertFalse(H.judge_duplicate('', '什么意思？')['duplicate'])
        self.assertFalse(H.judge_duplicate(None, None)['duplicate'])

    def test_similar_text_matches_legacy_continuity_helper(self):
        """口径钉死：continuity._similar_text / _plain 与 helpers 必须逐字同值。

        否则「集内判重（continuity）」与「跨集判重（helpers）」会各判各的。
        """
        for a, b in [(REAL_NEAR_A, REAL_NEAR_B), ('甲：你好！', '甲 你好'), ('', 'x')] + REAL_DIFFERENT:
            with self.subTest(pair=(a, b)):
                self.assertAlmostEqual(continuity._similar_text(a, b), H.similar_text(a, b), places=9)
        self.assertEqual(continuity._plain('甲：你好！'), H.plain_text('甲：你好！'))


# ============================ ② 可调用判重接口 ============================

class TestFindDuplicate(unittest.TestCase):
    """find_duplicate：输入候选台词 → 是否重复 + 来源（哪一集哪一句）。"""

    def test_hit_reports_source_episode_and_line(self):
        history = [_ledger_row('别废话，跟我走。', 1, shot_id=12), _ledger_row(REAL_EXACT, 9, shot_id=11)]
        res = H.find_duplicate(REAL_EXACT, history, exclude_episode_no=2)
        self.assertTrue(res['duplicate'])
        self.assertEqual(res['match']['episode_no'], 9)
        self.assertEqual(res['match']['shot_id'], 11)
        self.assertEqual(res['match']['text'], REAL_EXACT)
        self.assertIn('第', res['reason'] + '第')     # reason 可读

    def test_near_hit_reports_source(self):
        res = H.find_duplicate(REAL_NEAR_B, [_ledger_row(REAL_NEAR_A, 2, shot_id=95)],
                               exclude_episode_no=9)
        self.assertTrue(res['duplicate'])
        self.assertEqual(res['kind'], 'near')
        self.assertEqual((res['match']['episode_no'], res['match']['shot_id']), (2, 95))

    def test_exclude_same_episode(self):
        """本集自己的台账行必须跳过：重跑同一集不得自己和自己重复。"""
        history = [_ledger_row(REAL_EXACT, 2, shot_id=79)]
        self.assertFalse(H.find_duplicate(REAL_EXACT, history, exclude_episode_no=2)['duplicate'])
        self.assertTrue(H.find_duplicate(REAL_EXACT, history, exclude_episode_no=3)['duplicate'])

    def test_no_history_is_not_duplicate(self):
        res = H.find_duplicate(REAL_EXACT, [])
        self.assertFalse(res['duplicate'])
        self.assertIsNone(res['match'])
        self.assertEqual(res['compared'], 0)

    def test_prefers_exact_then_earliest_episode(self):
        """同一句在 3、5 两集都出现过 → 报最早那一集（人要看"这句最初是哪集说的"）。"""
        history = [_ledger_row('我们得想办法出去。', 5, shot_id=3),
                   _ledger_row('我们得想办法出去。', 3, shot_id=7)]
        res = H.find_duplicate('我们得想办法出去。', history, exclude_episode_no=6)
        self.assertEqual(res['match']['episode_no'], 3)
        # 精确命中优先于相似度更高的近似命中
        history2 = [_ledger_row('我们得马上想办法出去。', 4, shot_id=1),
                    _ledger_row('我们得想办法出去。', 8, shot_id=2)]
        res2 = H.find_duplicate('我们得想办法出去。', history2, exclude_episode_no=6)
        self.assertEqual(res2['kind'], 'exact')
        self.assertEqual(res2['match']['episode_no'], 8)

    def test_real_no_duplicate_corpus_zero_hits(self):
        """负样本对照组（构造口径与真实语料一致）：明显不同的台词一句都不判重。"""
        lines_ep1 = ['林风，你竟敢偷学《焚天诀》！', '我没有偷，是它自己选的我。',
                     '把他押下去，明日问斩。']
        rows = [_ledger_row(t, 1, shot_id=i + 1) for i, t in enumerate(lines_ep1)]
        cand = ['弟子知错，请长老责罚。', '这一剑，我要还给所有人。', '别过来，再过来我就跳下去。']
        for text in cand:
            with self.subTest(text=text):
                self.assertFalse(H.find_duplicate(text, rows, exclude_episode_no=2)['duplicate'])


# ============================ ③ 台账读写（quotes.json） ============================

class TestDialogueLedger(unittest.TestCase):
    """record_script_dialogue / dialogue_ledger_rows / 金句与台账互不污染。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='t2dedup_')
        self.dir = self.tmp
        self.key = '进境_整本小说'

    def _quotes(self):
        return continuity.load_quotes(self.dir, self.key)

    def test_record_writes_episode_no_and_increments_watermark(self):
        script1 = _script([{'shot_id': 1, 'dialogue': [{'speaker': '羡进', 'text': '这栋楼不对劲。'}]},
                           {'shot_id': 2, 'dialogue': '羡进：我们得上去看看。'}], episode_no=1)
        info1 = continuity.record_script_dialogue(self.dir, self.key, script1, 1)
        self.assertEqual(info1['recorded'], 2)
        store = self._quotes()
        rows = [q for q in store['quotes'] if q['source'] == H.SOURCE_SCRIPT]
        self.assertEqual({q['episode_no'] for q in rows}, {1})
        self.assertEqual(store['last_checked_episode'], 1)
        self.assertTrue(all(q['in_script'] for q in rows))

        script2 = _script([{'shot_id': 3, 'dialogue': [{'speaker': '羡进', 'text': '什么意思？'}]}],
                          episode_no=2)
        info2 = continuity.record_script_dialogue(self.dir, self.key, script2, 2)
        self.assertEqual(info2['recorded'], 1)
        store = self._quotes()
        self.assertEqual(store['last_checked_episode'], 2)
        self.assertEqual(sorted({q['episode_no'] for q in store['quotes']
                                 if q['source'] == H.SOURCE_SCRIPT}), [1, 2])

    def test_record_is_idempotent_per_episode(self):
        """同集重跑=替换（不追加重复行）；过短台词（<4 字）不入台账（它也不参与判重）。"""
        script = _script([{'shot_id': 1, 'dialogue': [{'speaker': '甲', 'text': '你先别动，听我说。'},
                                                     {'speaker': '乙', 'text': '好。'}]}], episode_no=2)
        first = continuity.record_script_dialogue(self.dir, self.key, script, 2)
        self.assertEqual(first['recorded'], 1)          # 「好。」过短，不入台账
        again = continuity.record_script_dialogue(self.dir, self.key, script, 2)
        self.assertEqual(again['replaced'], 1)
        rows = [q for q in self._quotes()['quotes'] if q['source'] == H.SOURCE_SCRIPT]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], '你先别动，听我说。')

    def test_watermark_never_goes_backwards(self):
        """回跑旧集 / 质检域回看某集：水位线只增不减。"""
        continuity.record_script_dialogue(self.dir, self.key,
                                          _script([{'shot_id': 1, 'dialogue': '甲：我们走。'}], episode_no=3), 3)
        store = self._quotes()
        store['last_checked_episode'] = 3
        continuity.save_json(continuity.quotes_path(self.dir, self.key), store)
        self.assertEqual(continuity.check_quotes_in_script(self.dir, self.key, {'shots': []}, 1)['required'], 0)
        self.assertEqual(self._quotes()['last_checked_episode'], 3)
        continuity.record_script_dialogue(self.dir, self.key,
                                          _script([{'shot_id': 1, 'dialogue': '甲：我们走。'}], episode_no=1), 1)
        self.assertEqual(self._quotes()['last_checked_episode'], 3)

    def test_novel_quotes_preserved_and_not_polluted(self):
        """原文金句行必须原样保留；金句抽取不得把台词台账当成"原文金句"（自证循环）。"""
        store = {'quotes': [{'episode_no': 2, 'text': '红色按钮不是用来求救的。',
                             'speaker_hint': '神秘红字', 'why': '反转', 'source': 'novel_chapter',
                             'in_script': True}],
                 'updated_at': 'x', 'last_checked_episode': 2}
        continuity.save_json(continuity.quotes_path(self.dir, self.key), store)
        continuity.record_script_dialogue(self.dir, self.key,
                                          _script([{'shot_id': 1, 'dialogue': '羡进：我们得上去看看。'}], episode_no=2), 2)
        after = self._quotes()
        self.assertEqual([q['text'] for q in continuity._quote_rows(after)], ['红色按钮不是用来求救的。'])
        self.assertEqual(continuity._quote_rows(after)[0]['in_script'], True)

        # ensure_quotes_for_episode：本集已有金句 → 直接返回，不再调模型（台账行不算金句）
        client = mock.Mock()
        client.chat.side_effect = AssertionError('不该调用模型：本集已有关卡金句')
        got = continuity.ensure_quotes_for_episode(client, self.dir, self.key, '原文', 2)
        self.assertEqual([q['text'] for q in got], ['红色按钮不是用来求救的。'])

    def test_check_quotes_in_script_counts_only_novel_quotes(self):
        """金句落位校验的 required 只能数金句 —— 混入台词台账会让命中率变成无意义的 100%。"""
        store = {'quotes': [
            {'episode_no': 2, 'text': '这句金句不在剧本里。', 'source': 'novel_chapter', 'in_script': False},
            {'episode_no': 2, 'text': '我们得上去看看。', 'source': H.SOURCE_SCRIPT, 'in_script': True},
        ], 'updated_at': 'x', 'last_checked_episode': 1}
        continuity.save_json(continuity.quotes_path(self.dir, self.key), store)
        script = {'shots': [{'shot_id': 1, 'dialogue': '羡进：我们得上去看看。'}]}
        res = continuity.check_quotes_in_script(self.dir, self.key, script, 2)
        self.assertEqual(res['required'], 1)
        self.assertEqual(res['missed'], ['这句金句不在剧本里。'])
        self.assertEqual(self._quotes()['last_checked_episode'], 2)

    def test_ledger_ignores_novel_quotes_never_spoken(self):
        """老项目只有金句行时用它做判重源，但 in_script=False 的金句（没进过台词）不算。"""
        store = {'quotes': [
            {'episode_no': 1, 'text': '这句金句没进过第1集台词。', 'source': 'novel_chapter', 'in_script': False},
            {'episode_no': 1, 'text': '红色按钮不是用来求救的。', 'source': 'novel_chapter', 'in_script': True},
        ], 'updated_at': 'x', 'last_checked_episode': 1}
        continuity.save_json(continuity.quotes_path(self.dir, self.key), store)
        rows = continuity.dialogue_ledger_rows(self.dir, self.key)
        self.assertEqual([q['text'] for q in rows], ['红色按钮不是用来求救的。'])
        self.assertFalse(continuity.check_dialogue_duplicate(
            self.dir, self.key, '这句金句没进过第1集台词。', episode_no=2)['duplicate'])
        self.assertTrue(continuity.check_dialogue_duplicate(
            self.dir, self.key, '红色按钮不是用来求救的。', episode_no=2)['duplicate'])
        # 显式要求"不过滤"时仍能取到全部行（排查用）
        self.assertEqual(len(continuity.dialogue_ledger_rows(self.dir, self.key, spoken_only=False)), 2)

    def test_ledger_rows_filter_and_exclude(self):
        script = _script([{'shot_id': 1, 'dialogue': '甲：先别动。'}], episode_no=1)
        continuity.record_script_dialogue(self.dir, self.key, script, 1)
        script2 = _script([{'shot_id': 2, 'dialogue': '乙：别过来。'}], episode_no=2)
        continuity.record_script_dialogue(self.dir, self.key, script2, 2)
        self.assertEqual(len(continuity.dialogue_ledger_rows(self.dir, self.key)), 2)
        self.assertEqual(len(continuity.dialogue_ledger_rows(self.dir, self.key, exclude_episode_no=2)), 1)
        self.assertEqual(continuity.dialogue_ledger_rows(self.dir, self.key, source='novel_chapter'), [])


# ============================ ② 可调用接口（走 quotes.json） ============================

class TestCheckDialogueDuplicateAPI(unittest.TestCase):
    """continuity.check_dialogue_duplicate：跨集判重的公开入口（输入台词 → 来源集/句）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='t2api_')
        self.key = '进境_整本小说'
        # 真实台账：第9集的两句（来自真实剧本）
        continuity.record_script_dialogue(
            self.dir, self.key,
            _script([{'shot_id': 11, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]},
                     {'shot_id': 24, 'dialogue': [{'speaker': '羡进', 'text': REAL_NEAR_B}]}],
                    episode_no=9), 9)

    def test_exact_hit_with_source(self):
        res = continuity.check_dialogue_duplicate(self.dir, self.key, REAL_EXACT, episode_no=2)
        self.assertTrue(res['duplicate'])
        self.assertEqual(res['kind'], 'exact')
        self.assertEqual(res['match']['episode_no'], 9)
        self.assertEqual(res['match']['shot_id'], 11)
        self.assertEqual(res['history_size'], 2)

    def test_near_hit_with_source(self):
        res = continuity.check_dialogue_duplicate(self.dir, self.key, REAL_NEAR_A, episode_no=2)
        self.assertTrue(res['duplicate'])
        self.assertEqual(res['kind'], 'near')
        self.assertEqual(res['match']['text'], REAL_NEAR_B)

    def test_different_line_not_duplicate(self):
        res = continuity.check_dialogue_duplicate(self.dir, self.key, REAL_DIFFERENT[0][0], episode_no=2)
        self.assertFalse(res['duplicate'])

    def test_same_episode_not_duplicate(self):
        res = continuity.check_dialogue_duplicate(self.dir, self.key, REAL_EXACT, episode_no=9)
        self.assertFalse(res['duplicate'])

    def test_fail_open_on_broken_ledger(self):
        """台账损坏（无 .bak 可恢复）→ 返回"不重复"+error，不抛异常（整集落盘不能被它拖垮）。"""
        path = continuity.quotes_path(self.dir, self.key)
        with io.open(path, 'w', encoding='utf-8') as f:
            f.write('{ not json')
        for suffix in ('.bak',):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        res = continuity.check_dialogue_duplicate(self.dir, self.key, REAL_EXACT, episode_no=2)
        self.assertFalse(res['duplicate'])
        self.assertIn('error', res)
        self.assertEqual(res['match'], None)


# ============================ ③ 生成链路：命中 → issue / 标记 / 重写闸门 ============================

class TestMergeCrossEpisodeDedupIssues(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='t2merge_')
        self.key = 'p'
        continuity.record_script_dialogue(
            self.dir, self.key,
            _script([{'shot_id': 11, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]},
                     {'shot_id': 24, 'dialogue': [{'speaker': '羡进', 'text': REAL_NEAR_B}]}],
                    episode_no=9), 9)

    def _validation(self):
        return {'episode_no': 2, 'issues': [], 'issue_stats': {'high': 0, 'medium': 0, 'low': 0},
                'rewrite_needed': False, 'rewrite_shot_ids': []}

    def test_exact_duplicate_triggers_rewrite_and_marks_shot(self):
        script = _script([{'shot_id': 79, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]}],
                         episode_no=2)
        val = self._validation()
        info = continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertEqual(info['count'], 1)
        self.assertEqual(info['exact'], 1)
        self.assertTrue(info['rewrite_triggered'])
        self.assertEqual(info['sources'], [9])
        issue = val['issues'][0]
        self.assertEqual(issue['severity'], 'high')
        self.assertEqual(issue['rule'], H.RULE)
        self.assertEqual(issue['shot_ids'], [79])
        self.assertIn('第9集', issue['detail'])
        self.assertIn(REAL_EXACT, issue['evidence'])
        self.assertTrue(val['rewrite_needed'])
        self.assertEqual(val['rewrite_shot_ids'], [79])
        self.assertEqual(val['issue_stats']['high'], 1)
        mark = script['shots'][0]['dialogue_dedup']
        self.assertEqual((mark['kind'], mark['match_episode_no'], mark['match_shot_id']), ('exact', 9, 11))

    def test_near_duplicate_is_marked_but_not_a_rewrite_trigger(self):
        script = _script([{'shot_id': 95, 'dialogue': [{'speaker': '羡进', 'text': REAL_NEAR_A}]}],
                         episode_no=2)
        val = self._validation()
        info = continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertEqual((info['count'], info['near'], info['exact']), (1, 1, 0))
        self.assertFalse(info['rewrite_triggered'])
        self.assertFalse(val['rewrite_needed'])
        self.assertEqual(val['issues'][0]['severity'], 'medium')
        self.assertIsNotNone(script['shots'][0].get('dialogue_dedup'))

    def test_no_duplicate_leaves_validation_untouched(self):
        script = _script([{'shot_id': 1, 'dialogue': '羡进：我们得上去看看。'}], episode_no=2)
        val = self._validation()
        info = continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertEqual(info['count'], 0)
        self.assertEqual(val['issues'], [])
        self.assertFalse(val['rewrite_needed'])
        self.assertNotIn('dialogue_dedup', script['shots'][0])
        self.assertEqual(info['history_size'], 2)

    def test_merge_is_idempotent(self):
        """重写后复检：同一份 validation 上重复合并，issue 与 stats 不得越加越多。"""
        script = _script([{'shot_id': 79, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]}],
                         episode_no=2)
        val = self._validation()
        continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertEqual(len(val['issues']), 1)
        self.assertEqual(val['issue_stats']['high'], 1)
        self.assertEqual(val['rewrite_shot_ids'], [79])

    def test_mark_cleared_when_rewrite_fixed_it(self):
        """重写改掉后：镜头上的 dialogue_dedup 标记必须被清掉，issue 也要撤回。"""
        script = _script([{'shot_id': 79, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]}],
                         episode_no=2)
        val = self._validation()
        continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        script['shots'][0]['dialogue'] = [{'speaker': '羡进', 'text': '我不明白你在说什么。'}]
        info = continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertEqual(info['count'], 0)
        self.assertEqual(val['issues'], [])
        self.assertEqual(val['issue_stats']['high'], 0)
        self.assertNotIn('dialogue_dedup', script['shots'][0])

    def test_string_shot_id_still_marked(self):
        """老剧本里 shot_id 偶有字符串写法：标记与重写镜号仍要按 int 归一命中。"""
        script = _script([{'shot_id': '79', 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]}],
                         episode_no=2)
        val = self._validation()
        continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertEqual([i for i in val['issues'] if i.get('rule') == H.RULE][0]['shot_ids'], [79])
        self.assertEqual(script['shots'][0]['dialogue_dedup']['match_shot_id'], 11)
        self.assertEqual(val['rewrite_shot_ids'], [79])

    def test_fail_open_when_ledger_unreadable(self):
        val = self._validation()
        script = _script([{'shot_id': 1, 'dialogue': '甲：先别动。'}], episode_no=2)
        with mock.patch.object(continuity, 'load_quotes', side_effect=RuntimeError('boom')):
            info = continuity.merge_cross_episode_dedup_issues(val, script, 2, self.dir, self.key)
        self.assertIn('error', info)
        self.assertEqual(info['count'], 0)
        self.assertFalse(val['rewrite_needed'])
        self.assertIn('dialogue_dedup', script['metadata'])


# ============================ ① 端到端接线：整集生成后自动落台账 ============================

class TestEpisodeFlowWiring(unittest.TestCase):
    """convert_chapter_with_continuity：不需要任何真实模型调用，验证"新台词自动进台账"的接线。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='t2flow_')
        self.key = '进境_整本小说'
        # 第1集台账：真实存在的台词（供第2集判重）
        continuity.record_script_dialogue(
            self.dir, self.key,
            _script([{'shot_id': 79, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]}],
                    episode_no=1), 1)

    def _run(self, script):
        cov = {'plot_coverage_percent': 100.0, 'plot_covered_count': 1, 'plot_unit_count': 1,
               'detail_coverage_percent': 100.0, 'missing_count': 0, 'supplement_shots': 0,
               'supplement_rounds': 0, 'passed': True, 'threshold_percent': 70}
        state = {'state_in': {'time': '当夜', 'location': '楼内', 'transition': '承接',
                              'character_states': []},
                 'state_out': {'time': '当夜', 'location': '楼内', 'transition': '待续',
                               'character_states': [], 'open_foreshadows': []},
                 'key_events': ['事件'], 'plot_points': ['要点'], 'dialogue_highlights': []}
        with mock.patch.object(continuity, 'ensure_style_guide', return_value={'style_guide': 'x'}), \
             mock.patch.object(continuity, 'ensure_voice_dict', return_value={'characters': []}), \
             mock.patch.object(continuity, 'ensure_quotes_for_episode', return_value=[]), \
             mock.patch.object(continuity, 'convert_chapter_to_script', return_value=script), \
             mock.patch.object(continuity, 'reconcile_outfit_with_shots', return_value={'fixed_count': 0, 'change_count': 0}), \
             mock.patch.object(continuity, 'merge_bible_from_episode', return_value={'bible': {}, 'added': [], 'conflicts': []}), \
             mock.patch.object(continuity, 'align_script_assets', return_value={}), \
             mock.patch.object(continuity, 'extract_episode_state', return_value=state), \
             mock.patch.object(continuity, 'validate_continuity',
                               return_value={'episode_no': 2, 'issues': [], 'issue_stats':
                                             {'high': 0, 'medium': 0, 'low': 0},
                                             'rewrite_needed': False, 'rewrite_shot_ids': [], 'score': 9}), \
             mock.patch.object(continuity.cc_contract, 'merge_contract_issues'), \
             mock.patch.object(continuity.coverage_mod, 'run_coverage_check', return_value=cov), \
             mock.patch.object(continuity.coverage_mod, 'summary_for_meta', return_value={}):
            import script_consistency
            with mock.patch.object(script_consistency, 'run_script_consistency_check', return_value={}), \
                 mock.patch.object(script_consistency, 'summary_for_meta', return_value={}):
                return continuity.convert_chapter_with_continuity(
                    mock.Mock(), {'title': '进境'}, '正文' * 400,
                    {'index': 2, 'title': '第二章', 'start': 0, 'end': 800},
                    self.key, self.dir, episode_no=2, save_dir=None, enable_rewrite=False)

    def test_ledger_written_and_duplicate_flagged(self):
        script = _script([{'shot_id': 79, 'dialogue': [{'speaker': '羡进', 'text': REAL_EXACT}]},
                          {'shot_id': 80, 'dialogue': '羡进：我们得上去看看。'}], episode_no=2)
        out = self._run(script)
        # ① 新台词自动写入 quotes.json：episode_no 正确、水位线递增
        store = continuity.load_quotes(self.dir, self.key)
        rows2 = [q for q in store['quotes'] if q['source'] == H.SOURCE_SCRIPT and q['episode_no'] == 2]
        self.assertEqual(len(rows2), 2)
        self.assertEqual(store['last_checked_episode'], 2)
        self.assertTrue(all(q['shot_id'] in (79, 80) for q in rows2))
        # ② 跨集重复被命中：来源写清"哪一集哪一句"（第1集#79）
        val = out['validation']
        dup_issues = [i for i in val['issues'] if i.get('rule') == H.RULE]
        self.assertEqual(len(dup_issues), 1)
        self.assertIn('第1集#79', dup_issues[0]['evidence'])
        # ③ 命中进 metadata（可查）与镜头标记
        meta = out['script']['metadata']['dialogue_dedup']
        self.assertEqual((meta['count'], meta['exact'], meta['sources']), (1, 1, [1]))
        cont = out['script']['metadata']['continuity']['dialogue_dedup']
        self.assertEqual(cont['recorded'], 2)
        self.assertEqual(cont['last_checked_episode'], 2)
        self.assertEqual(out['script']['shots'][0]['dialogue_dedup']['match_shot_id'], 79)
        # ④ 精确重复按 high 级拉起重写闸门（enable_rewrite=False 时只立闸门、不真跑模型）
        self.assertTrue(val['rewrite_needed'])
        self.assertEqual(val['rewrite_shot_ids'], [79])

    def test_no_duplicate_episode_records_ledger_without_issues(self):
        script = _script([{'shot_id': 1, 'dialogue': '羡进：我们得上去看看。'}], episode_no=2)
        out = self._run(script)
        store = continuity.load_quotes(self.dir, self.key)
        self.assertEqual(sorted({q['episode_no'] for q in store['quotes']
                                 if q['source'] == H.SOURCE_SCRIPT}), [1, 2])
        self.assertEqual([i for i in out['validation']['issues'] if i.get('rule') == H.RULE], [])
        self.assertEqual(out['script']['metadata']['dialogue_dedup']['count'], 0)


# ============================ ③ 真实剧本语料自测 ============================

class TestRealScripts(unittest.TestCase):
    """真实已存在重复样本：命中的是"哪一集哪一句"，且不误报。"""

    def _load_real(self):
        ep2 = _load_json(os.path.join(JIN_BACKUP, JIN_EP2))
        ep9 = _load_json(os.path.join(JIN_BACKUP, JIN_EP9))
        return ep2, ep9

    @unittest.skipUnless(os.path.isfile(os.path.join(JIN_BACKUP, JIN_EP2))
                         and os.path.isfile(os.path.join(JIN_BACKUP, JIN_EP9)),
                         '真实剧本备份不存在，跳过（证据见任务 t2 自测输出）')
    def test_jin_project_cross_episode_duplicates(self):
        ep2, ep9 = self._load_real()
        rows9 = H.dialogue_rows_of_script(ep9)
        history = [_ledger_row(r['text'], 9, r.get('shot_id'), r.get('speaker')) for r in rows9]
        dups = H.find_script_duplicates(ep2, history, exclude_episode_no=2)
        got = {(d['shot_id'], d['kind']) for d in dups}
        self.assertEqual(got, {(79, 'exact'), (95, 'near')},
                         '真实重复样本命中集合变了：%s' % sorted(got))
        exact = [d for d in dups if d['kind'] == 'exact'][0]
        self.assertEqual((exact['text'], exact['match']['episode_no'], exact['match']['shot_id']),
                         (REAL_EXACT, 9, 11))
        near = [d for d in dups if d['kind'] == 'near'][0]
        self.assertEqual((near['match']['episode_no'], near['match']['shot_id']), (9, 24))
        # 独立复算：每条命中都确实是"同一句"，误报 0
        for d in dups:
            if d['kind'] == 'exact':
                self.assertEqual(_plain_ref(d['text']), _plain_ref(d['match']['text']))
            else:
                self.assertGreaterEqual(_sim_ref(d['text'], d['match']['text']), H.THRESHOLDS["near_sim"])

    @unittest.skipUnless(os.path.isfile(os.path.join(NI_SCRIPTS, '第1集.json'))
                         and os.path.isfile(os.path.join(NI_SCRIPTS, '第2集.json')),
                         '逆天系统_全书 剧本不存在，跳过')
    def test_ni_project_has_no_cross_episode_duplicates(self):
        """负样本对照组：真实两集逐句互比 47×37=1739 组 → 0 命中（误报率 0 的实测证据）。"""
        ep1 = _load_json(os.path.join(NI_SCRIPTS, '第1集.json'))
        ep2 = _load_json(os.path.join(NI_SCRIPTS, '第2集.json'))
        rows1 = H.dialogue_rows_of_script(ep1)
        history = [_ledger_row(r['text'], 1, r.get('shot_id'), r.get('speaker')) for r in rows1]
        dups = H.find_script_duplicates(ep2, history, exclude_episode_no=2)
        self.assertEqual(dups, [])
        self.assertGreaterEqual(len(rows1) * len(H.dialogue_rows_of_script(ep2)), 1000)
        # 语料里确实存在"看着像"的组合，靠阈值而不是靠运气挡掉
        self.assertGreater(H.similar_text('这个废物……怎么会？', '这个废物……'), 0.4)


class TestRealCorpusSweep(unittest.TestCase):
    """全语料扫描：命中的每一条都必须能独立复算为真重复（误报率 0）。"""

    ROOTS = [os.path.join(APPDATA, 'mjscxt-desktop', 'mjscxt-data', 'output', 'scripts'),
             JIN_BACKUP]

    @unittest.skipUnless(os.path.isdir(JIN_BACKUP), '真实剧本目录不存在，跳过')
    def test_every_hit_is_independently_verified(self):
        corpora = 0
        pairs = 0
        hits = []
        for root in self.ROOTS:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                eps = {}
                for fn in filenames:
                    m = re.match(r'第(\d+)集\.json$', fn)
                    if not m:
                        continue
                    try:
                        eps[int(m.group(1))] = _load_json(os.path.join(dirpath, fn))
                    except Exception:  # noqa: BLE001
                        continue
                if len(eps) < 2:
                    continue
                corpora += 1
                names = sorted(eps)
                for i, a in enumerate(names):
                    for b in names:
                        if a == b:
                            continue
                        rows_b = H.dialogue_rows_of_script(eps[b])
                        hist = [_ledger_row(r['text'], b, r.get('shot_id'), r.get('speaker'))
                                for r in rows_b]
                        found = H.find_script_duplicates(eps[a], hist, exclude_episode_no=a)
                        pairs += len(H.dialogue_rows_of_script(eps[a])) * len(rows_b)
                        for d in found:
                            hits.append((a, d, b))
        self.assertGreaterEqual(corpora, 1)
        self.assertGreater(pairs, 500, '扫描规模太小，证据力度不足')
        for a, d, b in hits:
            with self.subTest(corpus_ep=a, shot=d['shot_id'], text=d['text'][:20]):
                m = d['match']
                self.assertEqual(m['episode_no'], b)
                if d['kind'] == 'exact':
                    self.assertEqual(_plain_ref(d['text']), _plain_ref(m['text']))
                else:
                    self.assertGreaterEqual(_sim_ref(d['text'], m['text']), H.THRESHOLDS["near_sim"])
                    ratio = (min(len(_plain_ref(d['text'])), len(_plain_ref(m['text'])))
                             / max(len(_plain_ref(d['text'])), len(_plain_ref(m['text']))))
                    self.assertGreaterEqual(ratio, H.THRESHOLDS["near_len_ratio"])


if __name__ == '__main__':
    unittest.main()
