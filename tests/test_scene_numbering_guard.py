# -*- coding: utf-8 -*-
"""t7 场次编号收口回归（2026-10-12，P0 出片缺陷）。

## 缺陷（生产实证，用户可见的坏成片）

`output/scripts/逆天系统_全书/第1集.json`：46 镜，但尾镜 scene_no **退回 1/2**
（shots[41..45].scene_no=1、shots[46].scene_no=2，location 分别是青云宗外门广场 /
偏僻山崖），而 scene_flow 只到第 8 场。根因是「剧本质检 → LLM 后置重写 → 落盘」这条
**后置改写**路径直接 save_episode_script，没有重跑场次编号（编号只在主路径
novel_to_script 覆盖率补镜**之前**跑）。

渲染侧因此产出坏成片：分组按「连续 run」得 10 组，但输出文件名/缓存名只按 scene_no
→ 第 9 组命中第 1 组的断点续跑分支被复用（该组 6 镜不渲染）+ 同一路径重复进 concat
清单 → **前两场重复、尾部 6 镜丢失**。

## 本测试锁住的三件事

1. `normalize_scene_numbering` 能修好「尾镜退回」的镜头表，且幂等、不误伤正常剧本；
2. 渲染分组键（文件名口径 `scene_{sn:02d}.mp4`）收口后**互不撞车**、总镜数不丢；
3. 落盘路径不得绕开收口（continuity 编排 / pipeline 后置重写兜底 / video_helpers 渲染前）。

数据驱动部分用**真实生产剧本**（存在才断言，不存在则 skip —— 绝不伪造数据）。
"""
import ast
import io
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import novel_to_script as nts  # noqa: E402


def _mk_shots(pairs):
    """pairs: [(location, scene_no), ...] → 镜头列表（shot_id 从 1 连续）。"""
    return [{"shot_id": i + 1, "location": loc, "scene_no": sn,
             "scene_heading": "【第%d场】外景·%s·日" % (sn, loc),
             "duration": 5.0}
            for i, (loc, sn) in enumerate(pairs)]


def _rollback_pairs():
    """与生产第 1 集同形的「尾镜退回」：8 场正常 + 5 镜退回场1 + 1 镜退回场2。

    每次调用返回**全新列表**（绝不共享可变对象，避免测试间互相污染）。
    """
    return ([("广场", 1)] * 10 + [("山门", 2)] + [("静室", 3)] * 2 + [("广场", 4)] * 5
            + [("意识空间", 5)] * 6 + [("广场", 6)] * 12 + [("山崖", 7)] * 3
            + [("石阶", 8)] + [("广场", 1)] * 5 + [("山崖", 2)])


def _plan(shots):
    """与 video_helpers 完全同口径的「按连续 run 分组」（只读）。"""
    out = []
    for i, s in enumerate(shots):
        try:
            sn = int(s.get("scene_no") or 1)
        except (TypeError, ValueError):
            sn = 1
        if out and out[-1][0] == sn:
            out[-1][1].append(i)
        else:
            out.append((sn, [i]))
    return out


def _file_keys(groups):
    """渲染侧输出文件名口径（scene_{sn:02d}.mp4）—— 撞车即复现坏成片。"""
    return ["scene_%02d.mp4" % sn for sn, _ in groups]


class TestNormalizeSceneNumbering(unittest.TestCase):
    """收口函数本身：修得对、修得幂等、不误伤。"""

    def test_rollback_is_detected(self):
        aud = nts.audit_scene_numbering(_mk_shots(_rollback_pairs()))
        self.assertFalse(aud["ok"], "退回的场次号必须被判为不自洽")
        ids = {it["id"] for it in aud["issues"]}
        self.assertIn("run_duplicated", ids)
        self.assertEqual(len(aud["group_runs"]), 10, "10 个连续 run")
        self.assertEqual(len({r["scene_no"] for r in aud["group_runs"]}), 8,
                         "但只有 8 个不同场号 = 同号两段")

    def test_normalize_fixes_rollback_and_is_idempotent(self):
        shots = _mk_shots(_rollback_pairs())
        warn = []
        r = nts.normalize_scene_numbering(shots, warn)
        self.assertEqual(r["renumbered"], 6, "应修正尾镜 6 个（5+1）")
        self.assertTrue(r["ok"], "收口后必须自洽")
        self.assertEqual([s["scene_no"] for s in shots[-6:]], [9, 9, 9, 9, 9, 10])
        self.assertTrue(nts.audit_scene_numbering(shots)["ok"])
        warn2 = []
        r2 = nts.normalize_scene_numbering(shots, warn2)
        self.assertEqual(r2["renumbered"], 0, "必须幂等")
        self.assertEqual(warn2, [], "幂等时不得再告警")

    def test_scene_flow_rebuilt_consistently(self):
        shots = _mk_shots(_rollback_pairs())
        r = nts.normalize_scene_numbering(shots)
        flow = nts.collect_scene_flow(shots)
        self.assertEqual(len(flow), r["scene_count"])
        self.assertEqual([f["scene_no"] for f in flow], list(range(1, len(flow) + 1)))
        self.assertEqual(sum(f["shot_count"] for f in flow), len(shots))

    def test_healthy_script_is_untouched(self):
        """正常剧本：零改动、零告警（防「修复」把正常数据改坏）。"""
        shots = _mk_shots([("甲", 1), ("甲", 1), ("乙", 2), ("丙", 3), ("丙", 3)])
        before = [s["scene_no"] for s in shots]
        warn = []
        r = nts.normalize_scene_numbering(shots, warn)
        self.assertEqual(r["renumbered"], 0)
        self.assertEqual([s["scene_no"] for s in shots], before)
        self.assertEqual(warn, [])


class TestRenderKeyUniqueness(unittest.TestCase):
    """渲染侧硬不变量：收口后「同号两段」消失 → 文件名/缓存名不撞车、不丢镜。"""

    def test_rollback_produces_duplicate_keys_without_fix(self):
        """先证明缺陷真的存在（否则本测试会变成空转）。"""
        keys = _file_keys(_plan(_mk_shots(_rollback_pairs())))
        self.assertEqual(len(keys), 10)
        self.assertNotEqual(len(set(keys)), len(keys),
                            "退回的场次号必须真的产出重复文件名，否则测试失去意义")
        self.assertEqual(keys[-2:], ["scene_01.mp4", "scene_02.mp4"])

    def test_no_duplicate_keys_after_normalize(self):
        shots = _mk_shots(_rollback_pairs())
        nts.normalize_scene_numbering(shots)
        groups = _plan(shots)
        keys = _file_keys(groups)
        self.assertEqual(len(set(keys)), len(keys),
                         "concat 清单绝不能有重复项: %s" % keys)
        self.assertEqual(sum(len(ids) for _, ids in groups), len(shots), "不能丢镜")
        self.assertTrue(all(ids for _, ids in groups), "不能有空的场次组")


def _calls_of(path, func_name):
    """AST：某函数体内出现的所有被调用函数名（浅层聚合）。"""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name:
                        out.append(name)
            break
    return out


class TestSavePathWiring(unittest.TestCase):
    """落盘路径必须调用收口（AST 层，防后人删掉这行让缺陷复活）。"""

    def test_continuity_save_path_calls_normalize(self):
        fl = _calls_of(os.path.join(_APP, "continuity.py"),
                       "convert_chapter_with_continuity")
        self.assertTrue(fl, "未解析到该函数 —— 函数被改名时本测试必须跟着更新")
        self.assertIn("save_episode_script", fl, "该函数是剧本落盘编排，必须仍在此落盘")
        self.assertIn("normalize_scene_numbering", fl,
                      "落盘前必须调用场次编号收口（t7 根修）—— 删掉它坏成片会复活")
        self.assertIn("collect_scene_flow", fl, "落盘前必须重建场次表")

    def test_pipeline_post_rewrite_calls_normalize(self):
        fl = _calls_of(os.path.join(_APP, "pipeline.py"), "step_script")
        self.assertIn("save_episode_script", fl)
        self.assertIn("normalize_scene_numbering", fl,
                      "剧本质检重写落盘前必须收口（t7 后置重写兜底）")

    def test_video_helpers_guards_before_grouping(self):
        src = io.open(os.path.join(_APP, "video_helpers.py"), encoding="utf-8").read()
        self.assertIn("normalize_scene_numbering(shots)", src,
                      "渲染侧必须在分组前收口（上游编号错了也不产重复成片）")
        self.assertIn("_scene_plan", src, "分组实现必须还在（防测试锚点漂移）")


class TestSaveClosure(unittest.TestCase):
    """落盘闭环：坏剧本经 save_episode_script（全库唯一落盘入口）之后必须自洽。"""

    def test_save_repairs_scene_numbering_on_disk(self):
        import tempfile
        shots = _mk_shots(_rollback_pairs())
        script = {"title": "t7", "shots": shots,
                  "scene_flow": nts.collect_scene_flow(shots),
                  "production_notes": {"scene_count": 8}}
        with tempfile.TemporaryDirectory(prefix="t7_save_") as tmp:
            p = nts.save_episode_script(script, tmp, "t7项目", 1, "t7项目")
            with io.open(p, encoding="utf-8") as f:
                disk = json.load(f)
        self.assertTrue(nts.audit_scene_numbering(disk["shots"])["ok"],
                        "落盘后的剧本必须自洽（否则渲染侧会产出重复成片）")
        self.assertEqual(len(disk["scene_flow"]), 10)
        self.assertEqual(disk["production_notes"]["scene_count"], 10)
        keys = _file_keys(_plan(disk["shots"]))
        self.assertEqual(len(set(keys)), len(keys), "concat 清单不得重复: %s" % keys)
        self.assertEqual(sum(len(i) for _, i in _plan(disk["shots"])), len(disk["shots"]))

    def test_save_is_idempotent_for_healthy_script(self):
        import tempfile
        shots = _mk_shots([("甲", 1), ("甲", 1), ("乙", 2)])
        script = {"title": "t7", "shots": shots}
        with tempfile.TemporaryDirectory(prefix="t7_save_ok_") as tmp:
            p = nts.save_episode_script(script, tmp, "t7项目2", 1, "t7项目2")
            with io.open(p, encoding="utf-8") as f:
                disk = json.load(f)
        self.assertEqual([s["scene_no"] for s in disk["shots"]], [1, 1, 2],
                         "正常剧本不得被改号")
        self.assertEqual(len(disk["scene_flow"]), 2)

    def test_save_closure_has_no_llm_or_network_dependency(self):
        """收口必须纯确定性：不得引入任何 LLM/网络调用点。"""
        src = io.open(os.path.join(_APP, "novel_to_script.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "save_episode_script":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        nm = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                        if nm:
                            names.add(nm)
                break
        self.assertIn("normalize_scene_numbering", names, "落盘入口必须收口")
        for banned in ("chat", "chat_json", "_json_call", "urlopen"):
            self.assertNotIn(banned, names, "落盘收口不得依赖 %s" % banned)


_PROD_SCRIPT = (r"C:\Users\liujianghua\AppData\Roaming\mjscxt-desktop\mjscxt-data"
                r"\output\scripts\逆天系统_全书\第1集.json")


class TestProductionDataDriven(unittest.TestCase):
    """数据驱动：真实生产剧本（存在即断言，不存在则 skip —— 绝不伪造数据）。"""

    def _load(self):
        if not os.path.isfile(_PROD_SCRIPT):
            self.skipTest("生产剧本不在本机：%s" % _PROD_SCRIPT)
        with io.open(_PROD_SCRIPT, encoding="utf-8") as f:
            return json.load(f)

    def test_production_script_keys_are_unique_after_normalize(self):
        data = self._load()
        shots = data.get("shots") or []
        if len(shots) < 10:
            self.skipTest("生产剧本镜头数异常，跳过")
        fresh = [dict(s) for s in shots]
        keys_before = _file_keys(_plan(fresh))
        aud = nts.audit_scene_numbering(fresh)
        if not aud["ok"]:
            self.assertNotEqual(len(set(keys_before)), len(keys_before),
                                "不自洽的剧本必然产出重复 concat 清单")
        nts.normalize_scene_numbering(fresh)
        keys_after = _file_keys(_plan(fresh))
        self.assertEqual(len(set(keys_after)), len(keys_after),
                         "收口后文件名必须唯一：%s" % keys_after)
        self.assertEqual(sum(len(i) for _, i in _plan(fresh)), len(fresh), "不能丢镜")
        self.assertTrue(nts.audit_scene_numbering(fresh)["ok"])


if __name__ == "__main__":
    unittest.main()
