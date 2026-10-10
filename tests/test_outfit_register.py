# -*- coding: utf-8 -*-
"""角色换装登记（2026-10-10，用户要求「在剧本提取资产时就要进行」）的回归测试。

被测对象：continuity._register_outfit_variants —— 剧本入库时登记本集换装
（只落 outfits/<key>/outfit.json，出图留给资产阶段）。

覆盖：正常登记 / 幂等 / 无服装不登记 / current_outfit 回落 / key 稳定可复现 /
      desc 与 key 的对应关系。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


class TestRegisterOutfitVariants(unittest.TestCase):

    def setUp(self):
        import config
        self.tmp = tempfile.mkdtemp(prefix="outfit_test_")
        self._old = config.CHARACTERS_DIR
        config.CHARACTERS_DIR = self.tmp
        import continuity
        self.C = continuity
        self.proj = "_test_proj"

    def tearDown(self):
        import config
        config.CHARACTERS_DIR = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bible(self, chars):
        return {"characters": chars}

    def test_registers_new_outfit(self):
        bible = self._bible([{"name": "羡进",
                              "outfit_by_episode": {"2": "深灰连帽外套、黑长裤"}}])
        added = self.C._register_outfit_variants(bible, self.proj, 2)
        self.assertEqual(len(added), 1)
        nm, key = added[0]
        self.assertEqual(nm, "羡进")
        rec = os.path.join(self.tmp, self.proj, "羡进", "outfits", key, "outfit.json")
        self.assertTrue(os.path.isfile(rec), "outfit.json 应已落盘")
        info = json.load(open(rec, encoding="utf-8"))
        self.assertEqual(info["desc"], "深灰连帽外套、黑长裤")
        self.assertEqual(info["episode"], 2)
        self.assertTrue(info["pending"])

    def test_idempotent(self):
        bible = self._bible([{"name": "羡进",
                              "outfit_by_episode": {"2": "深灰连帽外套、黑长裤"}}])
        first = self.C._register_outfit_variants(bible, self.proj, 2)
        second = self.C._register_outfit_variants(bible, self.proj, 2)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "重复登记应返回空（幂等）")

    def test_skips_character_without_outfit(self):
        bible = self._bible([{"name": "无脸员工", "outfit_by_episode": {},
                              "current_outfit": ""}])
        self.assertEqual(self.C._register_outfit_variants(bible, self.proj, 2), [])

    def test_falls_back_to_current_outfit(self):
        bible = self._bible([{"name": "林栖", "outfit_by_episode": {},
                              "current_outfit": "米白色针织开衫"}])
        added = self.C._register_outfit_variants(bible, self.proj, 3)
        self.assertEqual(len(added), 1)

    def test_key_is_stable(self):
        """同集同服装必得同一 key（可复现），否则匹配侧会找不到变体。"""
        b1 = self._bible([{"name": "羡进", "outfit_by_episode": {"2": "深灰外套"}}])
        k1 = self.C._register_outfit_variants(b1, self.proj, 2)[0][1]
        shutil.rmtree(os.path.join(self.tmp, self.proj), ignore_errors=True)
        b2 = self._bible([{"name": "羡进", "outfit_by_episode": {"2": "深灰外套"}}])
        k2 = self.C._register_outfit_variants(b2, self.proj, 2)[0][1]
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("ep02_"))

    def test_different_episode_different_key(self):
        b = self._bible([{"name": "羡进", "outfit_by_episode": {"2": "深灰外套"}}])
        k2 = self.C._register_outfit_variants(b, self.proj, 2)[0][1]
        b2 = self._bible([{"name": "羡进", "outfit_by_episode": {"5": "白色衬衫"}}])
        k5 = self.C._register_outfit_variants(b2, self.proj, 5)[0][1]
        self.assertNotEqual(k2, k5)
        self.assertTrue(k5.startswith("ep05_"))

    def test_multi_character(self):
        bible = self._bible([
            {"name": "A", "outfit_by_episode": {"1": "红色上衣"}},
            {"name": "B", "outfit_by_episode": {"1": "蓝色外套"}},
        ])
        added = self.C._register_outfit_variants(bible, self.proj, 1)
        self.assertEqual(len(added), 2)
        self.assertEqual({x[0] for x in added}, {"A", "B"})

    def test_new_outfit_same_episode_registers_second_variant(self):
        """同一集里换了第二套衣服 → 应登记第二个变体（不是覆盖）。"""
        bible = self._bible([{"name": "羡进", "outfit_by_episode": {"2": "深灰外套"}}])
        self.C._register_outfit_variants(bible, self.proj, 2)
        b2 = self._bible([{"name": "羡进", "outfit_by_episode": {"2": "白色病号服"}}])
        added = self.C._register_outfit_variants(b2, self.proj, 2)
        self.assertEqual(len(added), 1)
        odir = os.path.join(self.tmp, self.proj, "羡进", "outfits")
        self.assertEqual(len(os.listdir(odir)), 2)


if __name__ == "__main__":
    unittest.main()
