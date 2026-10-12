# -*- coding: utf-8 -*-
"""t18 cache_name 分场隔离回归（2026-10-12 修复）。

背景：MiniMaxH3Director 分段缓存旧版按 node_id 分目录，而工作流里 Director 节点
id 恒为 12 → 第 2 场从 seg_0000 重写、覆盖第 1 场的段（「段号莫名回到 0」）。
修复：给 Director 节点注入 cache_name 控件 → 插件 resolve_segment_cache_key 把
目录变成 <name>_<node_id>，不同场次/集次彼此隔离。
本测试锁住注入行为 + 命名口径，防止回退成「共用 node_id」。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import comfyui_client as cc  # noqa: E402


def _wf():
    return {"nodes": [
        {"id": 12, "type": "MiniMaxH3Director", "widgets_values_named": {}},
        {"id": 13, "type": "KSampler", "widgets_values_named": {}},
    ], "links": []}


class TestInjectDirectorCacheName(unittest.TestCase):
    """_inject_director_cache_name：只给 Director 节点写入 cache_name 控件。"""

    def test_inject_writes_cache_name(self):
        wf = _wf()
        n = cc.ComfyUIClient._inject_director_cache_name(wf, "proj__ep01__s02")
        self.assertEqual(n, 1)
        self.assertEqual(wf["nodes"][0]["widgets_values_named"]["cache_name"],
                         "proj__ep01__s02")

    def test_different_scenes_produce_different_cache_dirs(self):
        """不同场次 → 不同 cache_name → 插件侧落到不同目录（不再共用 node_id）。"""
        def name(ep, sn):
            return "proj__ep%02d__s%02d" % (ep, sn)
        keys = {name(1, 1), name(1, 2), name(1, 3)}
        self.assertEqual(len(keys), 3)
        # 插件侧 resolve_segment_cache_key(cache_name, node_id) = <name>_<node_id>
        dirs = {"%s_12" % k for k in keys}
        self.assertEqual(len(dirs), 3)

    def test_same_scene_same_cache_name(self):
        """同一场次重跑 → 同名（断点续跑的缓存收益保住）。"""
        def name(ep, sn):
            return "proj__ep%02d__s%02d" % (ep, sn)
        self.assertEqual(name(1, 2), name(1, 2))

    def test_empty_cache_name_is_noop(self):
        wf = _wf()
        n = cc.ComfyUIClient._inject_director_cache_name(wf, "")
        self.assertEqual(n, 0)
        self.assertNotIn("cache_name", wf["nodes"][0]["widgets_values_named"])

    def test_only_director_nodes_injected(self):
        wf = _wf()
        cc.ComfyUIClient._inject_director_cache_name(wf, "proj__ep01__s02")
        self.assertNotIn("cache_name", wf["nodes"][1]["widgets_values_named"])

    def test_overwrite_changed_cache_name(self):
        wf = _wf()
        wf["nodes"][0]["widgets_values_named"]["cache_name"] = "old"
        n = cc.ComfyUIClient._inject_director_cache_name(wf, "new")
        self.assertEqual(n, 1)
        self.assertEqual(wf["nodes"][0]["widgets_values_named"]["cache_name"], "new")


class TestLoadWorkflowCacheName(unittest.TestCase):
    """load_workflow(cache_name=...) 真实链路：读文件 → 注入 → to_api。"""

    def test_load_workflow_injects_cache_name(self):
        cli = cc.ComfyUIClient(base_url="http://127.0.0.1:9")
        with tempfile.TemporaryDirectory() as td:
            wf_path = os.path.join(td, "sim_h3_wf.json")
            with open(wf_path, "w", encoding="utf-8") as fh:
                json.dump(_wf(), fh)
            with mock.patch.object(cli, "to_api", return_value={"sentinel": True}) as m:
                cli.load_workflow(wf_path, cache_name="proj__ep01__s02")
                self.assertTrue(m.called)
                wf_arg = m.call_args[0][0]
                self.assertEqual(
                    wf_arg["nodes"][0]["widgets_values_named"]["cache_name"],
                    "proj__ep01__s02")


class TestH3CacheName(unittest.TestCase):
    """h3_cache_name：区分性字段前置，长项目名截断后不同场次不撞名（t72 加固）。"""

    def test_long_project_name_scenes_distinct_after_truncation(self):
        """60 字符项目名：s01..s10 截断到 64 字符后仍互不相同（消除 KEY_MAX_LEN 隐性依赖）。"""
        long_proj = "漫" * 60
        names = [cc.h3_cache_name(long_proj, 1, sn) for sn in range(1, 11)]
        self.assertEqual(len(set(names)), 10, "完整名应互不相同")
        # 模拟插件 normalize_cache_name 截断到 H3_CACHE_NAME_MAX_LEN=64
        truncated = [n[:cc.H3_CACHE_NAME_MAX_LEN] for n in names]
        self.assertEqual(len(set(truncated)), 10,
                         "截断后不同场次撞名：%s" % truncated)

    def test_batch_distinct_after_truncation(self):
        """同一场次内的分批（b00/b01）截断后也互不相同。"""
        long_proj = "漫" * 60
        names = [cc.h3_cache_name(long_proj, 1, 1, batch=bi) for bi in range(3)]
        truncated = [n[:cc.H3_CACHE_NAME_MAX_LEN] for n in names]
        self.assertEqual(len(set(truncated)), 3)

    def test_cache_name_readable(self):
        """排障可读：能看出场次(s07)/集号(ep03)/项目名。"""
        name = cc.h3_cache_name("我的项目", 3, 7)
        self.assertIn("s07", name)
        self.assertIn("ep03", name)
        self.assertIn("我的项目", name)

    def test_same_scene_same_name(self):
        """同场重跑同名（断点续跑的缓存收益保住）。"""
        self.assertEqual(cc.h3_cache_name("proj", 1, 2), cc.h3_cache_name("proj", 1, 2))


if __name__ == "__main__":
    unittest.main()
