# -*- coding: utf-8 -*-
"""预热规则（2026-10-11 用户口径）：
     · 本集预热（资产提示词预热）→ **恒开**，任何情况都不关；
     · 跨集预热（下一集剧本）    → **仅当生产计划是多集**时才开。
这两条是用户明确定下的口径，改动容易回退，故用测试锁住。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))


def _multi(episodes):
    """复刻 autopilot/pipeline 里的判定（保持与实现同口径）。"""
    s = str(episodes).strip().lower()
    if s == "all":
        return True
    if isinstance(episodes, (list, tuple)):
        return len([x for x in episodes if str(x).strip()]) > 1
    return False


class TestPrewarmRules(unittest.TestCase):
    def test_single_episode_plan_is_not_multi(self):
        """单集计划（用户口径「只跑第一集」）→ 跨集预热必须不开。"""
        for eps in ([1], [1], (1,), [1, ""], "1"):
            self.assertFalse(_multi(eps), "单集计划不该判为多集：%r" % (eps,))

    def test_multi_episode_plan_is_multi(self):
        """多集计划 → 跨集预热开启。"""
        for eps in ([1, 2], [1, 2, 3], "all", "ALL", (1, 2, 3), [1, 2] ):
            self.assertTrue(_multi(eps), "多集计划应判为多集：%r" % (eps,))

    def test_asset_prewarm_is_hardcoded_on(self):
        """本集预热（资产提示词预热）在 asset_worker 里必须是恒开 —— 不得再出现关闭分支。"""
        import io
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "app", "asset_worker.py")
        src = io.open(p, encoding="utf-8").read()
        self.assertIn("_pw_on = True", src, "本集预热应恒开")
        # 不允许再出现把它置 False 的判断
        self.assertNotIn("_pw_on = False", src, "本集预热不应存在关闭分支")

    def test_cross_episode_prewarm_has_multi_guard(self):
        """跨集预热的两个触发点都必须带多集判定。"""
        import io
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("app/autopilot.py", "app/pipeline.py"):
            src = io.open(os.path.join(base, rel), encoding="utf-8").read()
            self.assertIn('_eps_s == "all"', src, '%s 缺少多集判定' % rel)


if __name__ == "__main__":
    unittest.main()
