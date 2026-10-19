# -*- coding: utf-8 -*-
"""描述/对白比「按时长折算」回归（2026-10-19 用户拍板 A 方案）

口径：单镜允许比值 = DESC_TO_DIALOGUE_RATIO_MAX / 5.0 × duration
      = 0.6 × duration（10s→6 倍，8s→4.8 倍，5s→3 倍）。
聚合：全片允许描述字数 = Σ(0.6 × 各镜时长 × 该镜台词字数)；
      全片 description+visual_detail 合计超过该预算才判「描述臃肿」。

背景：旧的「全片 ≤3 倍」与「description 100~120 字 / 台词 ≤30 字」（理论 5~9 倍）
自相矛盾，v2 实测 5.3 倍被误判。本测试锁定 A 方案的行为边界。

运行：python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py"
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ.setdefault('MJSCXT_DATA_DIR', tempfile.mkdtemp(prefix='bloat_'))

import config_doctor  # noqa: E402
import qc_client      # noqa: E402


def _shot(sid, dur, desc, vd='', dlg=None):
    return {'shot_id': sid, 'duration': dur, 'description': desc,
            'visual_detail': vd, 'dialogue': list(dlg or [])}


def _line(text):
    return [{'speaker': 'A', 'text': text}]


def _bloat_issues(script):
    return [x for x in qc_client._validate_script_bloat(script)
            if str(x).startswith('描述臃肿')]


V2_LIKE = {'shots': [
    _shot(1, 10, 'x' * 86, 'y' * 33, dlg=_line('跪下。还要我再说第二遍？')),            # 12 字
    _shot(2, 10, 'x' * 81, 'y' * 20, dlg=_line('宗门丹房的筑基丹，怎么会在你铺盖底下？人赃并获，你还有什么话说？')),  # 32 字
    _shot(3, 10, 'x' * 75, 'y' * 27, dlg=_line('经脉堵了十八年的废物，也配进内门？偷丹换灵石，这算盘打得真响。')),      # 31 字
    _shot(4, 10, 'x' * 73, 'y' * 22, dlg=_line('我没偷。那瓶丹药，我连见都没见过。')),      # 17 字
    _shot(5, 10, 'x' * 71, 'y' * 21, dlg=_line('嘴硬？今天我就替宗门教教你，什么叫规矩。')),  # 20 字
    _shot(6, 10, 'x' * 78, 'y' * 26, dlg=_line('我忍了十八年，不是怕你，是在等一个能站直的机会。')),  # 24 字
    _shot(7, 10, 'x' * 72, 'y' * 23, dlg=_line('这……是什么……')),                          # 8 字
    _shot(8, 10, 'x' * 75, 'y' * 26, dlg=_line('你敢还手？一个废物，也敢动我？')),          # 15 字
    _shot(9, 10, 'x' * 74, 'y' * 20, dlg=_line('这一拳，还你十八年的账。从今往后，别再来惹我。')),  # 23 字
    _shot(10, 8, 'x' * 85, 'y' * 16, dlg=[]),                                              # 无台词
    _shot(11, 10, 'x' * 83, 'y' * 20, dlg=_line('从今天起，我不跪了。谁再来，我奉陪到底。')),  # 20 字
    _shot(12, 9, 'x' * 72, 'y' * 23, dlg=_line('经脉堵塞的人，怎么可能接下筑基期一拳？他到底是谁……')),  # 26 字
]}


class TimeScaledRatioTests(unittest.TestCase):

    def test_constants_registered(self):
        self.assertEqual(qc_client.DESC_TO_DIALOGUE_RATIO_MAX, 3.0)
        meta = config_doctor.REGISTRY.get('desc_to_dialogue_ratio_max')
        self.assertIsNotNone(meta, 'config_doctor 未登记 desc_to_dialogue_ratio_max')
        self.assertEqual(meta['owner'], 'qc_client.DESC_TO_DIALOGUE_RATIO_MAX')
        self.assertEqual(float(meta['default']), 3.0)

    def test_v2_like_script_passes_under_time_scaled_rule(self):
        """v2 实测 ~5.3 倍：旧 3 倍口径误报，按时长折算必须放行。"""
        shots = V2_LIKE['shots']
        desc = sum(len(s['description']) + len(s['visual_detail']) for s in shots)
        dlg = sum(len(l['text']) for s in shots for l in s['dialogue'])
        self.assertGreater(desc / float(dlg), 3.0, '样本没有复现「>3 倍」的旧误报条件')
        self.assertEqual(_bloat_issues(V2_LIKE), [])

    def test_10s_shot_allows_6x(self):
        s = {'shots': [_shot(1, 10, 'x' * 59, dlg=_line('x' * 10))]}   # 59/10=5.9 ≤ 6
        self.assertEqual(_bloat_issues(s), [])
        s['shots'][0]['description'] = 'x' * 61                    # 6.1 > 6
        self.assertEqual(len(_bloat_issues(s)), 1)

    def test_8s_shot_allows_4_8x(self):
        s = {'shots': [_shot(1, 8, 'x' * 47, dlg=_line('x' * 10))]}    # 4.7 ≤ 4.8
        self.assertEqual(_bloat_issues(s), [])
        s['shots'][0]['description'] = 'x' * 49                    # 4.9 > 4.8
        self.assertEqual(len(_bloat_issues(s)), 1)

    def test_5s_shot_stays_at_3x(self):
        s = {'shots': [_shot(1, 5, 'x' * 30, dlg=_line('x' * 10))]}    # 3.0 = 3.0
        self.assertEqual(_bloat_issues(s), [])
        s['shots'][0]['description'] = 'x' * 31                    # 3.1 > 3
        self.assertEqual(len(_bloat_issues(s)), 1)

    def test_missing_duration_defaults_to_10s(self):
        s = {'shots': [_shot(1, None, 'x' * 59, dlg=_line('x' * 10))]}
        self.assertEqual(_bloat_issues(s), [])
        s['shots'][0]['description'] = 'x' * 61
        self.assertEqual(len(_bloat_issues(s)), 1)

    def test_dialogue_free_shot_consumes_but_earns_no_budget(self):
        # 台词镜 10s×20 字 → 预算 120；台词镜描述 40 + 无台词镜 80 = 120（边界放行）
        s = {'shots': [_shot(1, 10, 'x' * 40, dlg=_line('x' * 20)),
                       _shot(2, 10, 'x' * 80, dlg=[])]}
        self.assertEqual(_bloat_issues(s), [])
        s['shots'][1]['description'] = 'x' * 81
        self.assertEqual(len(_bloat_issues(s)), 1)

    def test_message_names_offending_shots(self):
        s = {'shots': [_shot(1, 10, 'x' * 50, dlg=_line('x' * 20)),
                       _shot(2, 10, 'x' * 500, dlg=_line('x' * 10))]}
        issues = _bloat_issues(s)
        self.assertEqual(len(issues), 1)
        self.assertIn('shot_2', issues[0])
        self.assertIn('0.6×时长', issues[0])

    def test_all_no_dialogue_still_flagged(self):
        s = {'shots': [_shot(1, 10, 'x' * 100, dlg=[]), _shot(2, 10, 'x' * 100, dlg=[])]}
        issues = qc_client._validate_script_bloat(s)
        self.assertTrue(any(str(x).startswith('全片无台词') for x in issues))
        self.assertFalse(any(str(x).startswith('描述臃肿') for x in issues))


if __name__ == '__main__':
    unittest.main()
