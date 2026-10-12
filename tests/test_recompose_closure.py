# -*- coding: utf-8 -*-
"""read_json_strict 调用契约 + 「单镜返工自动重拼」闭环回归测试（t22 测试层新增）。

两个真缺陷（本轮审计定案），此前**没有任何测试能拦住**：

① `read_json_strict(path, default)` 是**两个必填参数**（app/fs_atomic.py:202）。
   `video_helpers._recompose_episode_video` 曾漏传 default → TypeError →
   被该函数的宽 except 吞成「[重新合成] 异常（保留旧成片）」→
   **「返工后自动重拼」从来没生效过**，成片永远停在返工前那一版，
   而日志里只有一行不起眼的 error，没有任何测试报警。

② 同一个函数还依赖 `seg_map.json` 的字段口径（episode_tag 以映射表为准、
   shot_to_seg 的段位替换、多子段只占第一段）。返工闭环是「用户唯一能单镜返工」
   的路，坏了就等于整个需求没落地 —— 必须有断言钉住。

分层：
    · TestReadJsonStrictCallSites  —— 契约层（AST 静态扫描，零运行期依赖）：
                                     全库每个调用点都必须传满参数。
    · TestRecomposeClosure         —— 集成层（真文件系统 + 假 concat）：
                                     不阻塞、不改产物、顺序与替换都正确。
"""
import ast
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "archive", "electron-app",
              "dist", "resources", "site-packages"}


def _iter_app_py(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and not d.endswith("_backup")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


# --------------------------------------------------------------------------- #
# 契约层：read_json_strict 的调用点必须传满 (path, default)
# --------------------------------------------------------------------------- #
class TestReadJsonStrictCallSites(unittest.TestCase):
    """AST 静态扫描：任何漏传 default 的调用点都当场变红。

    为什么用 AST 而不是「跑一遍」：漏参的后果是被**宽 except 吞掉**，
    运行期只表现为「某个功能静默不生效」，跑测试也不会报 —— 只有静态扫描
    能在提交前把这种缺陷按在墙上。
    """

    def setUp(self):
        self.files = sorted(_iter_app_py(_APP))

    def test_scanner_can_see_the_codebase(self):
        """自检：扫描器必须真的看到足够多的文件（否则测试会静默变空转）。"""
        self.assertGreater(len(self.files), 30,
                           "只扫到 %d 个 .py，扫描范围可疑" % len(self.files))

    def test_every_call_passes_default(self):
        bad = []
        total = 0
        for path in self.files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
                tree = ast.parse(src, filename=path)
            except (OSError, SyntaxError):
                continue          # 语法错误不归本测试管（另有 import/编译门禁）
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else getattr(fn, "attr", None) if isinstance(fn, ast.Attribute)
                        else None)
                if name != "read_json_strict":
                    continue
                total += 1
                has_kw = any(k.arg == "default" for k in node.keywords)
                if len(node.args) + (1 if has_kw else 0) < 2:
                    bad.append("%s:%d" % (os.path.relpath(path, _ROOT), node.lineno))
        self.assertGreaterEqual(total, 25,
                                "只找到 %d 个 read_json_strict 调用点，AST 匹配可能失效" % total)
        self.assertEqual(
            bad, [],
            "以下调用点漏传 default（形参是两个必填）→ 运行期 TypeError，"
            "且常被宽 except 吞成「功能静默失效」：%s" % bad)

    def test_signature_is_two_positional_required(self):
        """钉住签名本身：default 一旦被改成可选，本组测试的前提就变了。"""
        import fs_atomic
        sig = inspect.signature(fs_atomic.read_json_strict)
        params = list(sig.parameters.values())
        self.assertEqual([p.name for p in params[:2]], ["path", "default"])
        self.assertIs(params[1].default, inspect.Parameter.empty,
                      "default 变成可选后，漏传不再报错 —— 上面的 AST 门禁需同步复核")

    def test_missing_default_actually_raises(self):
        """运行期证据：漏一个参数确实 TypeError（就是当年被吞掉的那个异常）。"""
        import fs_atomic
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(TypeError):
                fs_atomic.read_json_strict(os.path.join(td, "nope.json"))

    def test_missing_file_returns_default(self):
        """三态语义的「缺失」分支：文件不存在 → 返回 default。"""
        import fs_atomic
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(fs_atomic.read_json_strict(os.path.join(td, "nope.json"), {}), {})
            self.assertIsNone(fs_atomic.read_json_strict(os.path.join(td, "nope.json"), None))


# --------------------------------------------------------------------------- #
# 集成层：单镜返工 → 自动重拼闭环
# --------------------------------------------------------------------------- #
class TestRecomposeClosure(unittest.TestCase):
    """`_recompose_episode_video` 的闭环行为（真文件系统 + 假 concat）。"""

    EP_TAG = "ep01"

    def setUp(self):
        import video_helpers as vh
        self.vh = vh
        self.td = tempfile.mkdtemp(prefix="t22_recompose_")
        self.videos = os.path.join(self.td, "videos")
        os.makedirs(os.path.join(self.videos, "rework"))
        # 4 个已落盘的段
        self.segs = []
        for i in range(4):
            p = os.path.join(self.videos, "seg_%04d.mp4" % i)
            with open(p, "wb") as f:
                f.write(b"seg%d" % i)
            self.segs.append(p)
        # shot 2 占段 1、2（长镜切段：shot_02 -> seg 1,2）；shot 3 占段 3
        self._write_map({"1": [0], "2": [1, 2], "3": [3]})
        # 用户对 shot 2 不满意 → 单镜返工产物落到 rework/
        self.rework = os.path.join(self.videos, "rework", "shot_02.mp4")
        with open(self.rework, "wb") as f:
            f.write(b"reworked")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_map(self, mapping, tag="ep01"):
        with open(os.path.join(self.videos, "seg_map.json"), "w", encoding="utf-8") as f:
            json.dump({"episode_tag": tag, "total_segments": 4, "shot_to_seg": mapping,
                       "order": [os.path.basename(p) for p in self.segs]}, f)

    def _patch_concat(self, record):
        """把 VideoPostProcessor.concat_videos 换成「记录入参 + 产出非空文件」。"""
        import video_postprocess as vp

        def _fake(self_, video_paths, output_path, audio_paths=None, caller=""):
            record.append({"paths": list(video_paths), "out": output_path, "caller": caller})
            with open(output_path, "wb") as f:
                f.write(b",".join(os.path.basename(str(p)).encode() for p in video_paths))
            return output_path

        self._orig = vp.VideoPostProcessor.concat_videos
        vp.VideoPostProcessor.concat_videos = _fake

    def _restore(self):
        import video_postprocess as vp
        vp.VideoPostProcessor.concat_videos = self._orig

    def _run(self):
        rec = []
        self._patch_concat(rec)
        try:
            out = self.vh._recompose_episode_video(self.videos, self.EP_TAG)
        finally:
            self._restore()
        return out, rec

    def test_rework_shot_replaces_its_first_segment_slot(self):
        """⭐ 闭环核心：返工段必须顶掉 shot 2 的**第一个**段位（段 1），顺序不变。"""
        out, rec = self._run()
        self.assertTrue(out, "重拼应成功（修前因漏传 default 恒返回空串）")
        self.assertEqual(len(rec), 1, "应只发起一次 concat")
        got = [os.path.basename(p) for p in rec[0]["paths"]]
        self.assertEqual(got, ["seg_0000.mp4", "shot_02.mp4", "seg_0002.mp4", "seg_0003.mp4"],
                         "返工段未在正确段位落位（多子段只占第一个段位，其余置空）")
        self.assertTrue(os.path.isfile(out))

    def test_output_name_follows_seg_map_episode_tag(self):
        """段位表的 episode_tag 优先于调用方推导的名字（防覆盖到错误的成片）。"""
        self._write_map({"1": [0], "2": [1, 2], "3": [3]}, tag="episode")
        out, _ = self._run()
        self.assertEqual(os.path.basename(out), "episode_full.mp4",
                         "写错了 tag 会覆盖到别的成片（静默毁数据）")

    def test_no_rework_dir_still_recomposes_all_segments(self):
        """没有返工产物时也要能按现有段重拼（顺序不乱）。"""
        os.remove(self.rework)
        out, rec = self._run()
        self.assertTrue(out)
        self.assertEqual([os.path.basename(p) for p in rec[0]["paths"]],
                         ["seg_0000.mp4", "seg_0001.mp4", "seg_0002.mp4", "seg_0003.mp4"])

    def test_missing_seg_map_returns_empty_and_touches_nothing(self):
        """旧数据 / 未开分段导出 → 返回 ""（fail-open，保留旧成片）。"""
        os.remove(os.path.join(self.videos, "seg_map.json"))
        out, rec = self._run()
        self.assertEqual(out, "")
        self.assertEqual(rec, [], "无映射表时不得发起 concat")

    def test_empty_map_returns_empty(self):
        self._write_map({})
        out, rec = self._run()
        self.assertEqual(out, "")
        self.assertEqual(rec, [])

    def test_concat_failure_keeps_old_film_and_returns_empty(self):
        """⭐ concat 失败必须 fail-open：旧成片原地保留，绝不删成片。"""
        old = os.path.join(self.videos, "%s_full.mp4" % self.EP_TAG)
        with open(old, "wb") as f:
            f.write(b"OLD-FILM")
        import video_postprocess as vp
        self._orig = vp.VideoPostProcessor.concat_videos
        vp.VideoPostProcessor.concat_videos = lambda *a, **k: ""
        try:
            out = self.vh._recompose_episode_video(self.videos, self.EP_TAG)
        finally:
            self._restore()
        self.assertEqual(out, "", "concat 失败应返回空串")
        self.assertTrue(os.path.isfile(old), "旧成片不能被删（fail-open）")
        with open(old, "rb") as f:
            self.assertEqual(f.read(), b"OLD-FILM", "旧成片内容不得被改动")

    def test_exception_is_swallowed_into_empty_string(self):
        """任何异常都返回 ""，绝不把异常抛给生产主流程。"""
        import video_postprocess as vp
        self._orig = vp.VideoPostProcessor.concat_videos
        vp.VideoPostProcessor.concat_videos = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = self.vh._recompose_episode_video(self.videos, self.EP_TAG)
        finally:
            self._restore()
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
