# -*- coding: utf-8 -*-
"""场景空间关联（2026-10-10，用户需求①②③）的回归测试。

覆盖点：提示词构造 / 编造名丢弃 / fail-open / 幂等写回 / 参考图存在性 /
固定结构进提示词 / 生成函数签名。
"""
import inspect
import os
import sys
import tempfile
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


class _FakeClient:
    """可控的假 LLM 客户端。"""

    def __init__(self, payload=None, raise_exc=None):
        self.payload = payload
        self.raise_exc = raise_exc

    def chat_json(self, prompt, system=None, temperature=0.4):
        if self.raise_exc:
            raise self.raise_exc
        return self.payload


SCENES = [
    {"name": "2704房间", "appearance": "封闭办公室，灰地毯"},
    {"name": "2704门口", "appearance": "房门敞开，昏暗门槛"},
    {"name": "27F走廊", "appearance": "米黄色墙壁，日光灯全亮"},
]


class TestRelateScenes(unittest.TestCase):

    def test_prompt_contains_all_scene_names(self):
        import scene_relations as SR
        p = SR.build_relate_prompt(SCENES)
        for s in SCENES:
            self.assertIn(s["name"], p)
        self.assertIn("space_group", p)
        self.assertIn("fixed_structure", p)

    def test_parses_valid_result(self):
        import scene_relations as SR
        payload = {"scenes": [
            {"name": "2704房间", "space_group": "27F-2704",
             "refs": ["2704门口"], "anchor": False, "fixed_structure": "斑驳木质门框"},
            {"name": "27F走廊", "space_group": "27F-2704",
             "refs": [], "anchor": True, "fixed_structure": "米黄色涂料墙面"},
        ]}
        out = SR.relate_scenes(_FakeClient(payload), SCENES)
        self.assertEqual(out["2704房间"]["refs"], ["2704门口"])
        self.assertTrue(out["27F走廊"]["anchor"])

    def test_drops_fabricated_scene_names(self):
        """模型编造的场景名必须丢弃（否则会挂出不存在的参考图）。"""
        import scene_relations as SR
        payload = {"scenes": [
            {"name": "不存在的场景", "space_group": "x", "refs": ["2704房间"]},
            {"name": "2704房间", "space_group": "27F-2704", "refs": ["2704门口"]},
        ]}
        out = SR.relate_scenes(_FakeClient(payload), SCENES)
        self.assertNotIn("不存在的场景", out)
        self.assertIn("2704房间", out)

    def test_refs_excludes_self_and_unknown(self):
        import scene_relations as SR
        payload = {"scenes": [
            {"name": "2704房间", "space_group": "g",
             "refs": ["2704房间", "幽灵场景", "2704门口", "27F走廊"]},
        ]}
        out = SR.relate_scenes(_FakeClient(payload), SCENES)
        self.assertEqual(out["2704房间"]["refs"], ["2704门口", "27F走廊"])

    def test_fail_open_on_exception(self):
        import scene_relations as SR
        out = SR.relate_scenes(_FakeClient(raise_exc=RuntimeError("boom")), SCENES)
        self.assertEqual(out, {})

    def test_empty_inputs(self):
        import scene_relations as SR
        self.assertEqual(SR.relate_scenes(_FakeClient({}), []), {})
        self.assertEqual(SR.relate_scenes(None, SCENES), {})


class TestApplyRelations(unittest.TestCase):

    def test_writes_fields(self):
        import scene_relations as SR
        bible = {"scenes": [dict(s) for s in SCENES]}
        rel = {"2704房间": {"space_group": "27F-2704", "refs": ["2704门口"],
                          "anchor": False, "fixed_structure": "斑驳木质门框"}}
        stat = SR.apply_scene_relations(bible, rel)
        self.assertEqual(stat["updated"], 1)
        row = bible["scenes"][0]
        self.assertEqual(row["space_group"], "27F-2704")
        self.assertEqual(row["refs"], ["2704门口"])
        self.assertEqual(row["fixed_structure"], "斑驳木质门框")

    def test_idempotent_does_not_overwrite(self):
        """已有值不被覆盖 —— 人工修正过的 refs 不会被模型重跑冲掉。"""
        import scene_relations as SR
        bible = {"scenes": [{"name": "2704房间", "refs": ["人工指定"],
                            "fixed_structure": "人工结构"}]}
        rel = {"2704房间": {"refs": ["2704门口"], "fixed_structure": "模型结构"}}
        SR.apply_scene_relations(bible, rel)
        self.assertEqual(bible["scenes"][0]["refs"], ["人工指定"])
        self.assertEqual(bible["scenes"][0]["fixed_structure"], "人工结构")

    def test_overwrite_flag(self):
        import scene_relations as SR
        bible = {"scenes": [{"name": "2704房间", "refs": ["旧"]}]}
        SR.apply_scene_relations(bible, {"2704房间": {"refs": ["新"]}}, overwrite=True)
        self.assertEqual(bible["scenes"][0]["refs"], ["新"])

    def test_empty_refs_not_written(self):
        """空 refs 不写 —— 避免给每个场景塞空数组。"""
        import scene_relations as SR
        bible = {"scenes": [{"name": "2704房间"}]}
        SR.apply_scene_relations(bible, {"2704房间": {"refs": []}})
        self.assertNotIn("refs", bible["scenes"][0])


class TestSceneRefImages(unittest.TestCase):

    def test_returns_only_existing(self):
        import scene_relations as SR
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "proj", "2704门口")
            os.makedirs(d)
            open(os.path.join(d, "base.png"), "wb").write(b"x")
            scene = {"refs": ["2704门口", "不存在"]}
            out = SR.scene_ref_images(scene, td, "proj")
            self.assertEqual(len(out), 1)
            self.assertTrue(out[0].endswith("base.png"))

    def test_empty_refs(self):
        import scene_relations as SR
        self.assertEqual(SR.scene_ref_images({}, "x", "y"), [])
        self.assertEqual(SR.scene_ref_images({"refs": []}, "x", "y"), [])

    def test_zero_size_file_skipped(self):
        import scene_relations as SR
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "proj", "s1")
            os.makedirs(d)
            open(os.path.join(d, "base.png"), "wb").close()      # 0 字节
            self.assertEqual(SR.scene_ref_images({"refs": ["s1"]}, td, "proj"), [])


class TestFixedStructureInPrompt(unittest.TestCase):

    def test_injects_fixed_structure(self):
        import asset_prompt_kit as K
        out = K.ensure_scene_layout("从门口向房内正视：方形小房间",
                                    {"fixed_structure": "斑驳木质门框"})
        self.assertIn("斑驳木质门框", out)
        self.assertIn(K.SCENE_FIXED_STRUCTURE_MARKER, out)

    def test_idempotent(self):
        import asset_prompt_kit as K
        sc = {"fixed_structure": "斑驳木质门框"}
        a = K.ensure_scene_layout("场景描述", sc)
        b = K.ensure_scene_layout(a, sc)
        self.assertEqual(a, b)
        self.assertEqual(b.count(K.SCENE_FIXED_STRUCTURE_MARKER), 1)

    def test_no_field_no_change(self):
        """没有 fixed_structure 时与从前一致（零回归）。"""
        import asset_prompt_kit as K
        out = K.ensure_scene_layout("场景描述", {})
        self.assertNotIn("固定结构（跨镜头不变", out)


class TestSceneGenSignature(unittest.TestCase):

    def test_ref_images_param_exists(self):
        import comfyui_client as C
        sig = inspect.signature(C.ComfyUIClient.generate_scene_base)
        self.assertIn("ref_images", sig.parameters)
        self.assertIsNone(sig.parameters["ref_images"].default)

    def test_scene_with_refs_helper_exists(self):
        import comfyui_client as C
        self.assertTrue(hasattr(C.ComfyUIClient, "_generate_scene_with_refs"))

    def test_relation_ref_hint_exists(self):
        import comfyui_client as C
        self.assertTrue(C.SCENE_RELATION_REF_HINT)
        self.assertIn("建筑结构", C.SCENE_RELATION_REF_HINT)


if __name__ == "__main__":
    unittest.main()
