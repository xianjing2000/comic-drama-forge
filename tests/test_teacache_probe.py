# -*- coding: utf-8 -*-
"""TeaCache 节点探针回归（B-7，2026-10-16）。

`probe_teacache_node` 把「确认 ComfyUI 侧是否装了 TeaCache 提速节点」从人工翻目录
变成一次自动检查。本测试用 mock 钉住三态：可用 / 可达但无节点 / 不可达（fail-open）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import comfyui_shared as S  # noqa: E402


def _resp(payload):
    r = mock.MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


class TestTeaCacheProbe(unittest.TestCase):
    def test_available_when_node_present(self):
        with mock.patch.object(S.requests, "get",
                               return_value=_resp({"ComfyUI-TeaCache": {}, "KSampler": {}})):
            out = S.probe_teacache_node()
        self.assertTrue(out["reachable"])
        self.assertTrue(out["available"])
        self.assertIn("ComfyUI-TeaCache", out["classes"])

    def test_reachable_but_no_node(self):
        with mock.patch.object(S.requests, "get",
                               return_value=_resp({"KSampler": {}, "UNETLoader": {}})):
            out = S.probe_teacache_node()
        self.assertTrue(out["reachable"])
        self.assertFalse(out["available"])
        self.assertEqual(out["classes"], [])

    def test_unreachable_is_fail_open(self):
        with mock.patch.object(S.requests, "get", side_effect=ConnectionError("boom")):
            out = S.probe_teacache_node()
        self.assertFalse(out["reachable"])
        self.assertFalse(out["available"])
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
