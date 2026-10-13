# -*- coding: utf-8 -*-
"""TE MAN 3D导演台 · 姿势层回归（2026-10-13）。

背景：分镜的 <image1> 构图基准图此前 poseValues 恒空 → 人偶永远 idle 站姿，
剧本里写的「跪坐压膝」「抱拳行礼」全部白写。本测试锁定：

  ① 与官方数据一致：JOINTS 27 条、PRESETS 10 条、STAND 基准逐值相同
     （逆向自 te_3d_director.js，键名/数值必须与导演台一致，否则 scene_json 回读漂移）；
  ② 剧本动作 -> 预设的自动映射（跪坐/跑/走/鞠躬/伸手/匍匐/躺）；
  ③ 手型 -> 20 根手骨角度（左右各 10），且拇指与其余四指角度不同；
  ④ 零回归：未命中任何动作关键词 -> poseValues 为空（渲染页继续播 idle）。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import te_3d_poses as P  # noqa: E402


class TestOfficialData(unittest.TestCase):
    def test_joints_table(self):
        self.assertEqual(len(P.JOINTS), 27)
        # 抽样逐条比对官方 JOINTS（骨骼名 / 轴 / 范围）
        self.assertEqual(P.JOINTS["bodyX"][:4], ("mixamorig:Hips", "x", -100, 100))
        self.assertEqual(P.JOINTS["lArmFwd"][:4], ("mixamorig:LeftArm", "x", -90, 180))
        self.assertEqual(P.JOINTS["lArmAbd"][:4], ("mixamorig:LeftArm", "z", -95, 35))
        self.assertEqual(P.JOINTS["lFore"][:4], ("mixamorig:LeftForeArm", "y", -110, 10))
        self.assertEqual(P.JOINTS["lKnee"][:4], ("mixamorig:LeftLeg", "x", 0, 130))
        self.assertEqual(P.JOINTS["rFoot"][:4], ("mixamorig:RightFoot", "x", -40, 40))

    def test_presets_match_official(self):
        self.assertEqual(len(P.PRESETS), 10)
        self.assertEqual(sorted(P.PRESETS), sorted([
            "stand", "tpose", "walk", "run", "bow", "kick", "reach",
            "kneelTwo", "prone", "lieDown"]))
        self.assertEqual(P.STAND, {"lArmAbd": -80, "rArmAbd": 80, "lFore": -8, "rFore": 8})
        self.assertEqual(P.preset_values("stand"), dict(P.STAND))
        self.assertEqual(P.preset_values("tpose"), {})
        self.assertEqual(P.preset_values("kneelTwo")["lKnee"], 97)
        self.assertEqual(P.preset_values("kneelTwo")["lArmAbd"], -80)   # STAND 已展开
        self.assertEqual(P.preset_values("bow")["spineX"], 35)
        self.assertEqual(P.preset_values("lieDown")["bodyX"], -88)
        self.assertEqual(P.preset_values("不存在"), {})


class TestResolve(unittest.TestCase):
    def test_action_to_preset(self):
        cases = {
            "他跪坐在地上，双手压在膝头": "kneelTwo",
            "羡进在石阶上奔跑": "run",
            "两人走过长廊": "walk",
            "他抱拳行礼": "bow",
            "她伸手去取木剑": "reach",
            "他趴在地上不起": "prone",
            "她仰卧在榻上": "lieDown",
            "一脚踢开破门": "kick",
        }
        for text, want in cases.items():
            self.assertEqual(P.resolve_pose(text)[0], want, text)

    def test_zero_regression_when_no_match(self):
        """未命中关键词 -> 空姿势（渲染页继续播 idle），这是刻意的零回归。"""
        built = P.build_pose("他站在原地，神色平静")
        self.assertEqual(built["poseValues"], {})
        self.assertEqual(built["currentPreset"], "")
        self.assertEqual(built["reason"], "no-match")
        self.assertEqual(P.build_pose("")["poseValues"], {})

    def test_clamp(self):
        got = P.clamp_joints({"lKnee": 999, "bodyX": -999, "未知键": 5})
        self.assertEqual(got["lKnee"], 130)
        self.assertEqual(got["bodyX"], -100)
        self.assertEqual(got["未知键"], 5)


class TestHandShape(unittest.TestCase):
    def test_fist_covers_both_hands(self):
        """握拳 = 5 指 × 3 节 × 左右手 = 30 个关节（每指第 4 节是指尖、跟随不单独驱动）。"""
        fist = P.HAND_SHAPES["fist"]
        self.assertEqual(len(fist), 30)
        self.assertEqual(sum(1 for k in fist if k.startswith("mixamorig:Left")), 15)
        self.assertEqual(sum(1 for k in fist if k.startswith("mixamorig:Right")), 15)
        self.assertIn("mixamorig:LeftHandThumb1", fist)
        self.assertIn("mixamorig:RightHandPinky3", fist)
        # 拇指与食指弯曲角不同（拇指是半量）
        self.assertLess(fist["mixamorig:LeftHandThumb1"], fist["mixamorig:LeftHandIndex1"])

    def test_point_only_index_straight(self):
        pt = P.HAND_SHAPES["point"]
        self.assertNotIn("mixamorig:LeftHandIndex1", pt)   # 食指伸直（0 → 不入表）
        self.assertIn("mixamorig:LeftHandMiddle1", pt)     # 其余三指弯曲

    def test_open_is_empty(self):
        self.assertEqual(P.HAND_SHAPES["open"], {})

    def test_hand_keywords(self):
        self.assertEqual(P.resolve_hand_shape("他右手缓缓握拳"), "fist")
        self.assertEqual(P.resolve_hand_shape("双手撑地起身"), "press")
        self.assertEqual(P.resolve_hand_shape("他握住剑柄"), "grip")
        self.assertEqual(P.resolve_hand_shape("神色平静"), "")

    def test_hand_shape_applies_even_without_action(self):
        built = P.build_pose("他右手缓缓握拳")
        self.assertEqual(built["handShape"], "fist")
        self.assertTrue(any("Hand" in k for k in built["poseValues"]))


if __name__ == "__main__":
    unittest.main()
