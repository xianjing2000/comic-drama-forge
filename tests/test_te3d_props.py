# -*- coding: utf-8 -*-
"""道具层回归（2026-10-13 接入官方 addProp：cube / cylinder / sphere）。

数据源是剧本逐镜就有的 items_in_shot（不改 prompt、不加人工标注），形状取名字、
位置取 description 的方位词 —— 本测试锁定这些确定性规则，防止以后改坏。
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from PIL import Image  # noqa: E402

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


class TestBodyTypes(unittest.TestCase):
    """体型差异化：角色档案 appearance/age -> heightScale/girth。"""

    def tearDown(self):
        D.set_char_profiles({})          # 复位模块级登记，避免污染其他用例

    def test_infer_body(self):
        self.assertEqual(D._infer_body("男性，清瘦少年，脸型清削", "十六岁"), (0.99, 0.88))
        hs, girth = D._infer_body("男性，魁梧青年，肌肉虬结，体格壮硕", "十八岁")
        self.assertGreater(hs, 1.0)
        self.assertGreater(girth, 1.0)

    def test_build_char_profiles(self):
        prof = D.build_char_profiles([
            {"name": "羡进", "appearance": "清瘦少年", "age": "十六岁"},
            {"name": "赵天霸", "appearance": "魁梧青年，体格壮硕", "age": "十八岁"},
        ])
        self.assertLess(prof["羡进"]["girth"], 1.0)
        self.assertGreater(prof["赵天霸"]["girth"], 1.0)
        self.assertGreater(prof["赵天霸"]["height"], prof["羡进"]["height"])

    def test_scene_uses_profiles(self):
        D.set_char_profiles({"甲": {"heightScale": 1.1, "girth": 1.25}})
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["甲"]}, aspect="16:9")
        ent = [e for e in scene["entities"] if e.get("type") == "character"][0]
        self.assertAlmostEqual(ent["heightScale"], 1.1, places=3)
        self.assertAlmostEqual(ent["girth"], 1.25, places=3)

    def test_default_zero_regression(self):
        D.set_char_profiles({})
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["无名氏"]}, aspect="16:9")
        ent = [e for e in scene["entities"] if e.get("type") == "character"][0]
        self.assertEqual(ent["heightScale"], 1.0)
        self.assertEqual(ent["girth"], 1.0)


class TestPanorama(unittest.TestCase):
    """全景 / 场景背板（官方 scene.panorama 口径）。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def tearDown(self):
        D.set_scene_images({})

    def _img(self, name, size, color=(30, 30, 30)):
        p = os.path.join(self.td.name, name)
        Image.new("RGB", size, color).save(p)
        return p

    def test_no_scene_image_keeps_old_behavior(self):
        D.set_scene_images({})
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["甲"], "location": "没有的景"},
                                   aspect="16:9")
        self.assertIsNone(scene["scene"]["panorama"])
        self.assertTrue(scene["scene"]["groundVisible"])     # 灰地面照旧

    def test_scene_image_billboard_and_hides_ground(self):
        p = self._img("base.png", (100, 100))                 # 1:1 -> 不能贴球
        D.set_scene_images({"广场": p})
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["甲"], "location": "广场"},
                                   aspect="16:9")
        pano = scene["scene"]["panorama"]
        self.assertTrue(pano["enabled"])
        self.assertEqual(pano["mode"], "billboard")
        self.assertEqual(pano["radius"], D.PANORAMA_RADIUS)
        self.assertEqual(pano["localPath"], p)
        # 有背板 -> 关掉灰地面与网格（否则背板下半会被盖住）
        self.assertFalse(scene["scene"]["groundVisible"])
        self.assertFalse(scene["scene"]["gridVisible"])

    def test_two_to_one_is_spherical(self):
        p = self._img("pano.png", (200, 100))                 # 真 2:1 -> 贴球
        D.set_scene_images({"廣場": p})
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["甲"], "location": "廣場"},
                                   aspect="16:9")
        self.assertEqual(scene["scene"]["panorama"]["mode"], "spherical")

    def test_missing_file_is_ignored(self):
        D.set_scene_images({"广场": os.path.join(self.td.name, "不存在.png")})
        scene = D.build_scene_json({"shot_id": 1, "camera": "全景",
                                    "characters_in_shot": ["甲"], "location": "广场"},
                                   aspect="16:9")
        self.assertIsNone(scene["scene"]["panorama"])


if __name__ == "__main__":
    unittest.main()
