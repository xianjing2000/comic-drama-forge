# -*- coding: utf-8 -*-
"""分镜「内容撑不满单镜下限」合并（2026-10-10 用户口径 C）的回归测试。

背景：时长模型里无台词镜头的内容上限仅约 1.6 秒，而单镜下限 5 秒。
第9集实测 48 镜中 29 镜内容撑不满 5 秒（60%）、21 镜无台词（44%）。
本模块做确定性合并兜底。
"""
import os
import sys
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


def _shot(**kw):
    base = {"shot_id": "1", "location": "走廊", "description": "", "dialogue": ""}
    base.update(kw)
    return base


class TestMergeUnderfilled(unittest.TestCase):

    def setUp(self):
        import novel_to_script as N
        self.N = N

    def test_merges_undershot_into_previous_same_scene(self):
        """内容不足的静态画面镜并入前一镜（同场景）。"""
        shots = [
            _shot(shot_id="1", description="羡进走进走廊，停在门前，抬手敲门"),
            _shot(shot_id="2", description="门缝光带切过地面"),
        ]
        r = self.N.merge_underfilled_shots(shots)
        self.assertEqual(r["merged"], 1)
        self.assertEqual(len(r["shots"]), 1)
        self.assertIn("门缝光带", r["shots"][0]["description"])
        self.assertIn("抬手敲门", r["shots"][0]["description"])

    def test_does_not_merge_across_scenes(self):
        """跨场景不合并 —— 会把两场戏黏在一起。"""
        shots = [
            _shot(shot_id="1", location="走廊", description="羡进走进走廊"),
            _shot(shot_id="2", location="办公室", description="纸面特写"),
        ]
        r = self.N.merge_underfilled_shots(shots)
        self.assertEqual(r["merged"], 0)
        self.assertEqual(len(r["shots"]), 2)

    def test_does_not_merge_when_result_exceeds_max(self):
        """合并后会超单镜上限则不合并（避免造出超长镜）。"""
        from novel_to_script import required_shot_duration
        from config import SHOT_DURATION_MAX
        big = _shot(shot_id="1", description="台词" * 40,
                    dialogue=[{"speaker": "A", "text": "这是一段很长的台词内容" * 6}])
        self.assertGreaterEqual(required_shot_duration(big), SHOT_DURATION_MAX)
        small = _shot(shot_id="2", description="影子")
        r = self.N.merge_underfilled_shots([big, small])
        self.assertEqual(r["merged"], 0)
        self.assertEqual(len(r["shots"]), 2)

    def test_original_list_not_mutated(self):
        """原列表不被就地修改（便于重跑对账）。"""
        shots = [
            _shot(shot_id="1", description="完整动作过程，走进房间并坐下"),
            _shot(shot_id="2", description="影子"),
        ]
        snapshot = [dict(x) for x in shots]
        self.N.merge_underfilled_shots(shots)
        self.assertEqual(shots, snapshot)

    def test_merges_dialogue(self):
        """合并时台词要一并带过去，不能丢。"""
        shots = [
            _shot(shot_id="1", description="完整动作", dialogue=[{"speaker": "A", "text": "你好"}]),
            _shot(shot_id="2", description="静态画面", dialogue=[{"speaker": "B", "text": "嗯"}]),
        ]
        # 第 1 镜内容不足也允许被合并 → 但要保证台词不丢
        r = self.N.merge_underfilled_shots(shots)
        texts = []
        for s in r["shots"]:
            d = s.get("dialogue")
            if isinstance(d, list):
                texts += [x.get("text") for x in d if isinstance(x, dict)]
            elif d:
                texts.append(str(d))
        self.assertIn("你好", texts)

    def test_keeps_shot_when_no_previous(self):
        """首镜内容不足且无前镜可并 → 原样保留（不强合）。"""
        shots = [_shot(shot_id="1", description="影子")]
        r = self.N.merge_underfilled_shots(shots)
        self.assertEqual(len(r["shots"]), 1)

    def test_empty_input(self):
        self.assertEqual(self.N.merge_underfilled_shots([])["shots"], [])
        self.assertEqual(self.N.merge_underfilled_shots(None)["shots"], [])

    def test_skips_non_dict(self):
        r = self.N.merge_underfilled_shots([None, "x", _shot(shot_id="1")])
        self.assertEqual(len(r["shots"]), 1)

    def test_reduces_undershot_count(self):
        """批量：内容不足的镜头数应下降。"""
        shots = []
        for i in range(10):
            shots.append(_shot(shot_id=str(i * 2 + 1),
                               description="羡进推开房门走进房间，环视一圈后坐下"))
            shots.append(_shot(shot_id=str(i * 2 + 2), description="纸面特写填满画面"))
        from novel_to_script import required_shot_duration
        before = sum(1 for s in shots if required_shot_duration(s) < 5.0)
        r = self.N.merge_underfilled_shots(shots)
        after = sum(1 for s in r["shots"] if required_shot_duration(s) < 5.0)
        self.assertLess(after, before)
        self.assertLess(len(r["shots"]), len(shots))


class TestMergeDialogueHelper(unittest.TestCase):

    def test_structured_lists_concatenated(self):
        import novel_to_script as N
        out = N._merge_dialogue([{"speaker": "A", "text": "1"}], [{"speaker": "B", "text": "2"}])
        self.assertEqual(len(out), 2)

    def test_string_joined(self):
        import novel_to_script as N
        out = N._merge_dialogue("甲：一", "乙：二")
        self.assertIn("甲：一", out)
        self.assertIn("乙：二", out)

    def test_empty_sides(self):
        import novel_to_script as N
        self.assertEqual(N._merge_dialogue("", "乙：二"), ["乙：二"])
        self.assertEqual(N._merge_dialogue("甲：一", ""), ["甲：一"])
        self.assertEqual(N._merge_dialogue(None, None), None)


if __name__ == "__main__":
    unittest.main()
