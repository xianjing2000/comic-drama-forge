# -*- coding: utf-8 -*-
"""切分粒度自愈的回归测试。

背景（2026-10-10）：
  CHARS_PER_SHOT=240 只是**下给模型的引导口径**，此前没有任何事后校验。
  实测《进境》第2集引导约 15 镜，经「模型超产 + 补局部插入镜 + 覆盖率补镜 + 长台词拆镜」
  四步累积后落盘 **109 镜**（34 字/镜，引导密度的 1/7），成片节奏碎、分镜前置就要 3.6 小时。

  本测试钉住两件事：
    ① 密度判据的边界（含「短段落不判定」这条防灌水的例外）；
    ② 自愈的**保守性** —— 只有明确更好才采纳，任何异常都必须保留原结果，绝不阻断生产。
"""
import os
import sys
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import novel_to_script as N  # noqa: E402


def _shots(n):
    return [{"description": "d%d" % i, "camera": "中景固定"} for i in range(n)]


class TestShotsDensityOk(unittest.TestCase):
    CPS = 240

    def test_guide_density_passes(self):
        # 3600 字 / 240 = 引导 15 镜
        self.assertTrue(N.shots_density_ok(15, 3600, chars_per_shot=self.CPS))

    def test_within_tolerance_passes(self):
        # 容差 2 倍 -> 30 镜仍算正常
        self.assertTrue(N.shots_density_ok(30, 3600, chars_per_shot=self.CPS))

    def test_beyond_tolerance_fails(self):
        self.assertFalse(N.shots_density_ok(31, 3600, chars_per_shot=self.CPS))

    def test_episode2_real_case_fails(self):
        """真实回归：第2集 3600 字切 109 镜必须被判过碎。"""
        self.assertFalse(N.shots_density_ok(109, 3600, chars_per_shot=self.CPS))

    def test_short_chunk_always_passes(self):
        """短段落（引导镜数 < 2）不做判定 —— 否则会逼模型给 300 字灌出 2 镜。"""
        self.assertTrue(N.shots_density_ok(3, 300, chars_per_shot=self.CPS))
        self.assertTrue(N.shots_density_ok(1, 100, chars_per_shot=self.CPS))

    def test_degenerate_inputs_pass(self):
        self.assertTrue(N.shots_density_ok(0, 3600))
        self.assertTrue(N.shots_density_ok(10, 0))
        self.assertTrue(N.shots_density_ok(10, 3600, chars_per_shot=0))


class TestHealOverSplit(unittest.TestCase):
    """自愈的采纳条件与失败兜底。"""

    CHUNK = {"text": "字" * 3600, "char_count": 3600, "title": "第2集"}   # 引导 15 镜
    ARGS = (None, {}, {}, CHUNK)

    def test_does_not_retry_when_density_ok(self):
        shots = _shots(20)
        with mock.patch.object(N, 'build_shots_for_chunk') as m:
            out = N._heal_over_split(*self.ARGS, shots, 'T')
        m.assert_not_called()
        self.assertEqual(out, shots)

    def test_adopts_denser_result(self):
        shots = _shots(109)
        with mock.patch.object(N, 'build_shots_for_chunk', return_value=_shots(15)) as m:
            out = N._heal_over_split(*self.ARGS, shots, 'T')
        self.assertEqual(len(out), 15, '重切达标后应采纳新结果')
        self.assertEqual(m.call_count, 1)
        # 必须带 extra_hint 且禁止再次自愈（防无限递归）
        _, kwargs = m.call_args
        self.assertIn('extra_hint', kwargs)
        self.assertIn('109', kwargs['extra_hint'])
        self.assertTrue(kwargs.get('_no_heal'))

    def test_keeps_original_when_retry_empty(self):
        shots = _shots(109)
        with mock.patch.object(N, 'build_shots_for_chunk', return_value=[]):
            out = N._heal_over_split(*self.ARGS, shots, 'T')
        self.assertEqual(len(out), 109, '重切无产出时必须保留原结果')

    def test_keeps_original_when_retry_barely_better(self):
        """重切只少一点点、且密度仍不达标 -> 不采纳（避免丢掉内容换来毫无意义的改善）。"""
        shots = _shots(109)
        with mock.patch.object(N, 'build_shots_for_chunk', return_value=_shots(100)):
            out = N._heal_over_split(*self.ARGS, shots, 'T')
        self.assertEqual(len(out), 109)

    def test_adopts_when_shrunk_enough_even_if_still_dense(self):
        """密度仍未达标但镜数已缩到 70% 以下 -> 采纳（阶段性改善也要拿住）。"""
        shots = _shots(109)
        with mock.patch.object(N, 'build_shots_for_chunk', return_value=_shots(60)):
            out = N._heal_over_split(*self.ARGS, shots, 'T')
        self.assertEqual(len(out), 60)

    def test_never_raises_and_keeps_original(self):
        shots = _shots(109)
        with mock.patch.object(N, 'build_shots_for_chunk', side_effect=RuntimeError('boom')):
            out = N._heal_over_split(*self.ARGS, shots, 'T')
        self.assertEqual(len(out), 109, '自愈异常绝不能影响生产')

    def test_events_recorded(self):
        events = []
        shots = _shots(109)
        with mock.patch.object(N, 'build_shots_for_chunk', return_value=_shots(15)):
            N._heal_over_split(*self.ARGS, shots, 'T', events=events)
        kinds = [e['event'] for e in events]
        self.assertIn('over_split', kinds)
        self.assertIn('over_split_healed', kinds)
        heal = [e for e in events if e['event'] == 'over_split_healed'][0]
        self.assertEqual((heal['before'], heal['after']), (109, 15))

    def test_empty_shots_is_noop(self):
        with mock.patch.object(N, 'build_shots_for_chunk') as m:
            out = N._heal_over_split(*self.ARGS, [], 'T')
        m.assert_not_called()
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
