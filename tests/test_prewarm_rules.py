# -*- coding: utf-8 -*-
"""预热规则（2026-10-11 用户口径）：
     · 本集预热（资产提示词预热）→ **恒开**，任何情况都不关；
     · 跨集预热（下一集剧本）    → **仅当生产计划是多集**时才开。
这两条是用户明确定下的口径，改动容易回退，故用测试锁住。

⚠️ 2026-10-12（t48 修复假绿隐患）：原测试用自己复刻的 _multi 副本函数 + grep
源码字符串，生产判定一旦改写（副本不感知）就假绿。现已改为直接调用真实生产函数
autopilot.prewarm_next_script / pipeline._prefetch_next_scripts 断言行为；
grep 源码仅保留给「恒开」这类没有独立判定函数的结构断言。
"""
import os
import sys
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")  # 防止 import autopilot 拉起托管线程

import autopilot  # noqa: E402


class TestCrossEpisodePrewarmMultiGuard(unittest.TestCase):
    """跨集预热只在多集计划时开启 —— 直接调用真实生产函数，不 grep 源码。"""

    def setUp(self):
        # 确保跨集预热开关不被 env 强制关闭（否则会绕过我们要测的多集判定）
        os.environ.pop("MJSCXT_PREWARM_NEXT_SCRIPT", None)

    def test_single_episode_plan_does_not_prewarm(self):
        """单集计划（用户口径「只跑第一集」）→ 直接 False，不派发、不读小说元数据。"""
        with mock.patch.object(autopilot, "_novel_meta", return_value=None) as m:
            result = autopilot.prewarm_next_script("proj", {"episodes": [1]}, 1)
        self.assertFalse(result)
        m.assert_not_called()  # 多集判定拦截：根本没走到「读小说元数据」

    def test_multi_episode_plan_passes_multi_guard(self):
        """多集计划 → 通过多集判定（走到 _novel_meta），不再被「单集」拦截。"""
        for eps in ([1, 2], [1, 2, 3], (1, 2, 3), [1, 2]):
            with mock.patch.object(autopilot, "_novel_meta", return_value=None) as m:
                autopilot.prewarm_next_script("proj", {"episodes": eps}, 1)
            m.assert_called()  # 多集判定通过 → 走到小说解析（_novel_meta）

    def test_all_keyword_is_multi(self):
        """episodes="all" → 多集。"""
        with mock.patch.object(autopilot, "_novel_meta", return_value=None) as m:
            autopilot.prewarm_next_script("proj", {"episodes": "all"}, 1)
        m.assert_called()

    def test_pipeline_prefetch_single_episode_noop(self):
        """另一触发点 pipeline._prefetch_next_scripts：单集不派发预取线程。"""
        import pipeline
        # D-1（2026-10-16）：`pipeline._A()` 服务定位器已删除，无需再桩它。
        with mock.patch.object(pipeline.threading, "Thread") as m_thread:
            pipeline._prefetch_next_scripts({"config": {"episodes": [1]}, "episode_no": 1})
        m_thread.assert_not_called()


class TestAssetPrewarmAlwaysOn(unittest.TestCase):
    """本集预热（资产提示词预热）恒开 —— 无独立判定函数，用结构断言钉住「写死 True」。"""

    def test_asset_prewarm_is_hardcoded_on(self):
        """asset_worker 里本集预热必须恒开，不得再出现关闭分支。

        说明：这是**结构断言**（grep 源码）而非行为断言 —— 因为「恒开」在实现里就是
        一句 _pw_on = True 字面量赋值，没有可独立调用的判定函数。局限是等价改名
        （_pw_on → _pw_enabled）会假红；但「删掉恒开、加回关闭分支」这类真实回归会
        被当场拦下。跨集预热的**行为**判定已由 TestCrossEpisodePrewarmMultiGuard 覆盖。
        """
        import io
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "app", "asset_worker.py")
        src = io.open(p, encoding="utf-8").read()
        self.assertIn("_pw_on = True", src, "本集预热应恒开")
        self.assertNotIn("_pw_on = False", src, "本集预热不应存在关闭分支")


if __name__ == "__main__":
    unittest.main()
