# -*- coding: utf-8 -*-
"""道具层回归（2026-10-13 接入官方 addProp：cube / cylinder / sphere）。

数据源是剧本逐镜就有的 items_in_shot（不改 prompt、不加人工标注），形状取名字、
位置取 description 的方位词 —— 本测试锁定这些确定性规则，防止以后改坏。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import te_3d_director as D  # noqa: E402


class TestPropKind(unittest.TestCase):
    def test_kind_by_name(self):
        self.assertEqual(D._prop_kind("旧木剑"), "cylinder")
        self.assertEqual(D._prop_kind("筑基丹"), "sphere")
        self.assertEqual(D._prop_kind("九转天罡诀"), "cube")
        self.assertEqual(D._prop_kind(""), "cube")

    def test_size_by_name(self):
        self.assertLess(D._prop_size("碎玉佩"), D._prop_size("石碑"))
        self.assertEqual(D._prop_size("普通道具"), 0.40)


class TestPropEntities(unittest.TestCase):
    def test_empty_items(self):
        self.assertEqual(D._prop_entities({"items_in_shot": []}), [])
        self.assertEqual(D._prop_entities({}), [])

    def test_hand_placement(self):
        ents = D._prop_entities({"items_in_shot": ["旧木剑"],
                                 "description": "他手中握着旧木剑"})
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["type"], "prop")
        self.assertEqual(ents[0]["kind"], "cylinder")
        self.assertAlmostEqual(ents[0]["transform"]["position"][1], 1.05, places=2)

    def test_table_placement(self):
        ents = D._prop_entities({"items_in_shot": ["筑基丹"],
                                 "description": "桌上放着一枚筑基丹"})
        self.assertAlmostEqual(ents[0]["transform"]["position"][1], 0.98, places=2)

    def test_ground_placement_sits_on_floor(self):
        ents = D._prop_entities({"items_in_shot": ["长剑"],
                                 "description": "地上插着一把长剑"})
        self.assertGreater(ents[0]["transform"]["position"][1], 0.0)
        # 贴地 = 中心抬到半个尺寸（0.4m 的剑 → 0.2m），不会浮空也不会埋进地面
        self.assertLessEqual(ents[0]["transform"]["position"][1], 0.21)

    def test_left_right_hint(self):
        left = D._prop_entities({"items_in_shot": ["木桌"], "description": "左边地上有张木桌"})
        right = D._prop_entities({"items_in_shot": ["木桌"], "description": "右边地上有张木桌"})
        self.assertLess(left[0]["transform"]["position"][0], 0.0)
        self.assertGreater(right[0]["transform"]["position"][0], 0.0)

    def test_multiple_props_do_not_overlap(self):
        ents = D._prop_entities({"items_in_shot": ["九转天罡诀", "长剑"],
                                 "description": "羡进握着书与剑"})
        self.assertEqual(len(ents), 2)
        xs = [e["transform"]["position"][0] for e in ents]
        self.assertNotEqual(xs[0], xs[1])

    def test_scale_is_meters(self):
        ents = D._prop_entities({"items_in_shot": ["大石碑"], "description": "地上立着大石碑"})
        self.assertGreater(ents[0]["transform"]["scale"][0], 0.5)


class TestSceneIntegration(unittest.TestCase):
    def test_scene_contains_props(self):
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["羡进"],
                                    "items_in_shot": ["旧木剑"],
                                    "description": "羡进握着旧木剑"}, aspect="16:9")
        props = [e for e in scene["entities"] if e.get("type") == "prop"]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["name"], "旧木剑")

    def test_no_items_means_no_prop(self):
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["羡进"],
                                    "description": "羡进站着"}, aspect="16:9")
        self.assertFalse([e for e in scene["entities"] if e.get("type") == "prop"])


class TestCrowd(unittest.TestCase):
    def test_crowd_role_detection(self):
        self.assertTrue(D._is_crowd_role("外门围观弟子"))
        self.assertTrue(D._is_crowd_role("围观群众"))
        self.assertFalse(D._is_crowd_role("羡进"))
        self.assertFalse(D._is_crowd_role("赵天霸"))

    def test_crowd_expands_into_array(self):
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["羡进", "外门围观弟子"]},
                                   aspect="16:9")
        chars = [e for e in scene["entities"] if e.get("type") == "character"]
        crowd = [e for e in chars if str(e.get("name") or "").startswith("外门围观弟子")]
        self.assertGreater(len(crowd), 1, "群演角色应展开成多个人偶")
        self.assertIn("外门围观弟子", scene.get("crowdMap") or {})
        xs = [e["transform"]["position"][0] for e in crowd]
        self.assertEqual(len(set(xs)), len(xs), "群演阵列不应重合")

    def test_single_role_not_expanded(self):
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["羡进"]}, aspect="16:9")
        chars = [e for e in scene["entities"] if e.get("type") == "character"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(scene.get("crowdMap"), {})

    def test_explicit_crowd_count_wins(self):
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["外门围观弟子"],
                                    "crowd": {"count": 5}}, aspect="16:9")
        chars = [e for e in scene["entities"] if e.get("type") == "character"]
        self.assertEqual(len(chars), 5)

    def test_crowd_members_spread(self):
        ms = D._crowd_members([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 5, 2.0)
        self.assertEqual(len(ms), 5)
        xs = [m[0][0] for m in ms]
        self.assertLess(min(xs), 0.0)
        self.assertGreater(max(xs), 0.0)


if __name__ == "__main__":
    unittest.main()
