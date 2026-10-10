# -*- coding: utf-8 -*-
"""跨集串帧回归测试（2026-10-10 用户要求）。

用户口径：「第一集的分镜最后一帧要传到第二集的视频生成第一个分镜里面去」。
即把既有的**集内**跨镜链式（上一镜尾帧 = 下一镜首帧）延伸到**集与集之间**。

目录约定（shared_project._ep_dir）：第 1 集**平铺**在 <项目>/，第 2 集起 epNN/。
因此「上一集目录」对第 2 集而言就是项目根，对第 3 集才是 ep02 —— 这个不对称
正是最容易写错的地方，本测试专门钉它。
"""
import os
import sys
import tempfile
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


def _touch(path, size=32):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


class TestPrevEpisodeEndFrame(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("MJSCXT_DATA_DIR",
                              os.path.join(os.environ.get("APPDATA", ""),
                                           "mjscxt-desktop", "mjscxt-data"))
        os.environ["MJSCXT_AUTOPILOT"] = "0"
        import keyframe
        self.KF = keyframe
        self.tmp = tempfile.mkdtemp(prefix="xep_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_episode1_has_no_previous(self):
        """第 1 集（平铺目录）没有上一集，必须返回空字符串。"""
        d = os.path.join(self.tmp, "proj")          # 平铺 = 第1集
        _touch(os.path.join(d, "shot_03_end.png"))
        self.assertEqual(self.KF.prev_episode_end_frame(d), "")

    def test_ep2_takes_from_flat_root(self):
        """第 2 集：上一集是**平铺的项目根**（不是 ep01）。"""
        proj = os.path.join(self.tmp, "proj")
        _touch(os.path.join(proj, "shot_05_end.png"))        # 第1集末镜
        _touch(os.path.join(proj, "shot_02_end.png"))
        ep2 = os.path.join(proj, "ep02")
        os.makedirs(ep2, exist_ok=True)
        got = self.KF.prev_episode_end_frame(ep2)
        self.assertTrue(got.endswith("shot_05_end.png"),
                        "第2集应取第1集序号最大的尾帧，实得：%s" % got)

    def test_ep3_takes_from_ep02(self):
        """第 3 集：上一集是 ep02。"""
        proj = os.path.join(self.tmp, "proj")
        _touch(os.path.join(proj, "shot_09_end.png"))         # 第1集
        _touch(os.path.join(proj, "ep02", "shot_04_end.png"))  # 第2集末镜
        ep3 = os.path.join(proj, "ep03")
        os.makedirs(ep3, exist_ok=True)
        got = self.KF.prev_episode_end_frame(ep3)
        self.assertTrue(got.endswith(os.path.join("ep02", "shot_04_end.png")),
                        "第3集应取 ep02 的末镜尾帧，实得：%s" % got)

    def test_zero_byte_ignored(self):
        """0 字节的尾帧视为无效（避免把写坏的图当首帧）。"""
        proj = os.path.join(self.tmp, "proj")
        _touch(os.path.join(proj, "shot_07_end.png"), size=0)
        _touch(os.path.join(proj, "shot_06_end.png"), size=64)
        ep2 = os.path.join(proj, "ep02")
        os.makedirs(ep2, exist_ok=True)
        got = self.KF.prev_episode_end_frame(ep2)
        self.assertTrue(got.endswith("shot_06_end.png"))

    def test_missing_prev_dir_returns_empty(self):
        """上一集目录不存在 → 返回空（调用方回退到本镜分镜图）。"""
        ep5 = os.path.join(self.tmp, "proj", "ep05")
        os.makedirs(ep5, exist_ok=True)
        self.assertEqual(self.KF.prev_episode_end_frame(ep5), "")


class TestPlanKeyframesCrossEpisode(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("MJSCXT_DATA_DIR",
                              os.path.join(os.environ.get("APPDATA", ""),
                                           "mjscxt-desktop", "mjscxt-data"))
        os.environ["MJSCXT_AUTOPILOT"] = "0"
        import keyframe
        self.KF = keyframe
        self.tmp = tempfile.mkdtemp(prefix="xep2_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_shot_chains_to_prev_episode(self):
        """本集第一镜的 chain_src 应为上一集末帧，chain_from == 'prev_episode'。"""
        proj = os.path.join(self.tmp, "proj")
        prev_end = os.path.join(proj, "shot_08_end.png")
        _touch(prev_end)
        ep2 = os.path.join(proj, "ep02")
        os.makedirs(ep2, exist_ok=True)
        shots = [{"shot_id": 1, "location": "B"}, {"shot_id": 2, "location": "B"}]
        plan = self.KF.plan_keyframes(shots, {}, ep2, only_missing=False,
                                      chain_mode="auto")
        first = plan[0]
        self.assertEqual(first.get("chain_from"), "prev_episode")
        self.assertTrue(str(first.get("chain_src") or "").endswith("shot_08_end.png"))

    def test_ep1_first_shot_not_chained(self):
        """第 1 集第一镜没有上一集，不该有跨集链式。"""
        d = os.path.join(self.tmp, "proj")
        os.makedirs(d, exist_ok=True)
        shots = [{"shot_id": 1, "location": "A"}]
        plan = self.KF.plan_keyframes(shots, {}, d, only_missing=False,
                                      chain_mode="auto")
        self.assertIsNone(plan[0].get("chain_from"))

    def test_chain_off_disables_cross_episode(self):
        """chain_mode=off 时跨集串帧也必须关闭。"""
        proj = os.path.join(self.tmp, "proj")
        _touch(os.path.join(proj, "shot_08_end.png"))
        ep2 = os.path.join(proj, "ep02")
        os.makedirs(ep2, exist_ok=True)
        shots = [{"shot_id": 1, "location": "B"}]
        plan = self.KF.plan_keyframes(shots, {}, ep2, only_missing=False,
                                      chain_mode="off")
        self.assertIsNone(plan[0].get("chain_from"))


if __name__ == "__main__":
    unittest.main()
