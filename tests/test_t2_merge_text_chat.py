# -*- coding: utf-8 -*-
"""t2 合并 text/chat 回归（2026-10-12）。

用户口径「text 和 chat 都不需要图片功能」→ 合并为一个纯文本模块（text 优先）。
MODULE_META 只剩 text/qc；chat → text 兼容别名；_migrate_merge_chat 幂等。
本测试锁住：模块结构、别名、迁移幂等、text 优先。
"""
import os
import sys
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import ai_config  # noqa: E402


class _FakeStore:
    def __init__(self):
        self.keys = {}
        self.calls = []

    def get_api_key(self, ns):
        return self.keys.get(ns, "")

    def set_api_key(self, ns, key):
        self.keys[ns] = key
        self.calls.append(("set", ns))
        return True

    def clear_api_key(self, ns):
        self.keys.pop(ns, None)
        self.calls.append(("clear", ns))
        return True


class TestModuleMergeStructure(unittest.TestCase):
    def test_module_meta_only_text_and_qc(self):
        self.assertEqual(set(ai_config.MODULE_META), {"text", "qc"})
        self.assertNotIn("chat", ai_config.MODULE_META)

    def test_modules_only_text_and_qc(self):
        self.assertEqual(list(ai_config.MODULES), ["text", "qc"])

    def test_chat_alias_maps_to_text(self):
        self.assertEqual(ai_config._canonical_module("chat"), "text")
        self.assertEqual(ai_config._canonical_module("CHAT"), "text")
        self.assertEqual(ai_config._canonical_module("Chat"), "text")

    def test_invalid_module_raises(self):
        with self.assertRaises(ValueError):
            ai_config._canonical_module("bogus")


class TestMigrateMergeChat(unittest.TestCase):
    """_migrate_merge_chat：chat 合并进 text（text 优先），幂等。"""

    def _raw(self, text=None, chat=None):
        modules = {"text": text if text is not None else {}, "qc": {}}
        if chat is not None:
            modules["chat"] = chat
        return {"version": 1, "modules": modules}

    def _call(self, raw):
        store = _FakeStore()
        with mock.patch.object(ai_config, "_store", return_value=store), \
             mock.patch.object(ai_config, "_write_file"), \
             mock.patch.object(ai_config, "_backup_for_migration"):
            return ai_config._migrate_merge_chat(raw, "tmp.json"), store

    def test_merge_chat_fields_into_text(self):
        raw = self._raw(text={"base_url": ""}, chat={"base_url": "http://chat", "model": "m"})
        (out, migrated), _ = self._call(raw)
        self.assertTrue(migrated)
        self.assertNotIn("chat", out["modules"])
        self.assertEqual(out["modules"]["text"]["base_url"], "http://chat")
        self.assertEqual(out["modules"]["text"]["model"], "m")

    def test_text_priority_over_chat(self):
        raw = self._raw(text={"base_url": "http://text"}, chat={"base_url": "http://chat"})
        (out, migrated), _ = self._call(raw)
        self.assertTrue(migrated)
        self.assertEqual(out["modules"]["text"]["base_url"], "http://text")

    def test_idempotent_after_merge(self):
        raw = self._raw(text={}, chat={"base_url": "http://chat"})
        (out, migrated), _ = self._call(raw)
        self.assertTrue(migrated)
        self.assertNotIn("chat", out["modules"])
        # 第二次：已无 chat → 跳过（幂等）
        (out2, migrated2), _ = self._call(out)
        self.assertFalse(migrated2)
        self.assertNotIn("chat", out2["modules"])

    def test_no_chat_no_migration(self):
        raw = self._raw(text={"base_url": "http://text"})
        (out, migrated), _ = self._call(raw)
        self.assertFalse(migrated)
        self.assertEqual(out["modules"]["text"]["base_url"], "http://text")


if __name__ == "__main__":
    unittest.main()
