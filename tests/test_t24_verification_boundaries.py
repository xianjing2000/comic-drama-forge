# -*- coding: utf-8 -*-
"""T24 验证门：对 t16~t19 修复与 4 条「特别核查项」的独立边界/反例断言。

与 t22 的测试不同 —— t22 测「修复后的行为对不对」，本文件测
**「修复本身的覆盖边界在哪、哪里会被绕过」**。所有结论都必须能被别人重跑复现。

四条特别核查项（任务书点名）：
  S1  t16 的 concat 音轨一致性，是否覆盖「部分有音轨」全部排列组合；
  S2  t17 的 read_json_strict 漏 default 类缺陷（「宽 except 吞掉真错误」）是否还有同类残留；
  S3  t18 的 _panel_dup_hit 正则是否会被否定语境绕过；
  S4  本会话把 export_mode 默认从 segments 回退为 all —— 是否无残留影响。

凡是「当前实现已知不满足」的，写成 KNOWN 基线断言（钉住现状 + 将来收紧时自动变红），
而不是假装通过。
"""
import ast
import io
import itertools
import os
import re
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_ROOT, "app")
for _p in (_APP, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# =========================================================================== #
# S1：concat 音轨一致性 —— 「部分有音轨」的全部排列组合
# =========================================================================== #
class S1_ConcatAudioPermutations(unittest.TestCase):
    """t16 声称覆盖 B/C/E 三场景。这里把「有/无音轨 + 参数一致性」的全组合跑一遍。

    只截住 subprocess.run 观察主拼接命令，不真跑 ffmpeg（可秒级跑完 50+ 组合）。
    grid：长度 n in {1,2,3}，每段 = 有音轨参数 a / 有音轨参数 b / 无音轨。
    """

    def setUp(self):
        import video_postprocess as vp
        self.vp = vp
        self.p = vp.VideoPostProcessor()
        self._td = tempfile.TemporaryDirectory(prefix="t24_route_")
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()

    @staticmethod
    def _probe(kind):
        if kind == "a":
            return {"has_audio": True, "audio_codec": "aac", "sample_rate": "44100",
                    "channels": 2, "has_video": True, "width": 320, "height": 240,
                    "fps": 25.0, "duration": 2.0}
        if kind == "b":
            return {"has_audio": True, "audio_codec": "aac", "sample_rate": "24000",
                    "channels": 1, "has_video": True, "width": 320, "height": 240,
                    "fps": 25.0, "duration": 2.0}
        return {"has_audio": False, "has_video": True, "width": 320, "height": 240,
                "fps": 25.0, "duration": 2.0}

    def _route(self, kinds):
        seen = []

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, **kw):
            seen.append(list(cmd))
            if cmd:
                try:
                    with open(cmd[-1], "wb") as f:
                        f.write(b"fake")
                except OSError:
                    pass
            return _R()

        paths = []
        for i, k in enumerate(kinds):
            p = os.path.join(self.tmp, "s%d.mp4" % i)
            with open(p, "wb") as f:
                f.write(b"x")
            paths.append(p)
        table = {os.path.basename(p): self._probe(k) for p, k in zip(paths, kinds)}

        def _fake_probe(p):
            b = os.path.basename(p)
            if b.endswith(".work"):
                return self._probe("a")
            return dict(table[b])

        orig_probe, orig_run = self.vp.probe_media, self.vp.subprocess.run
        self.vp.probe_media, self.vp.subprocess.run = _fake_probe, _fake_run
        try:
            self.p.concat_videos(paths, os.path.join(self.tmp, "out.mp4"),
                                 caller="t24.perm")
        finally:
            self.vp.probe_media, self.vp.subprocess.run = orig_probe, orig_run
        main = [c for c in seen if "-filter_complex" in c
                or ("-f" in c and "concat" in c and "-c" in c and "copy" in c)]
        self.assertTrue(main, "找不到主拼接命令：%s" % seen)
        return "reencode" if "-filter_complex" in main[0] else "demuxer"

    @staticmethod
    def _expected(kinds):
        audios = [k for k in kinds if k in ("a", "b")]
        if not audios:
            return "reencode"
        if len(audios) != len(kinds):
            return "reencode"
        if len(set(audios)) > 1:
            return "reencode"
        return "demuxer"

    def test_all_permutations_of_n2(self):
        for kinds in itertools.product(("a", "b", "x"), repeat=2):
            with self.subTest(kinds=kinds):
                self.assertEqual(self._route(kinds), self._expected(kinds),
                                 "组合 %s 分流判定错" % (kinds,))

    def test_all_permutations_of_n3(self):
        """n=3 全部 27 种 —— t16 报告只测了 E=[有,无,有(异参)] 一种。"""
        for kinds in itertools.product(("a", "b", "x"), repeat=3):
            with self.subTest(kinds=kinds):
                self.assertEqual(self._route(kinds), self._expected(kinds),
                                 "组合 %s 分流判定错" % (kinds,))

    def test_single_segment(self):
        for kinds in (("a",), ("x",)):
            with self.subTest(kinds=kinds):
                self.assertEqual(self._route(kinds), self._expected(kinds))

    def test_no_missing_audio_is_ever_demuxer_eligible(self):
        bad = [k for k in itertools.product(("a", "b", "x"), repeat=3)
               if "x" in k and self._route(k) == "demuxer"]
        self.assertEqual(bad, [], "这些组合走了 -c copy（音轨会被截断/丢弃）：%s" % bad)

    def test_no_param_mismatch_is_demuxer_eligible(self):
        bad = [k for k in itertools.product(("a", "b", "x"), repeat=2)
               if ("a" in k and "b" in k) and self._route(k) == "demuxer"]
        self.assertEqual(bad, [], "参数不一致却走了 -c copy：%s" % bad)


# =========================================================================== #
# S2：read_json_strict 漏 default —— 「宽 except 吞真错误」还有没有同类
# =========================================================================== #
class S2_SilentSwallowScan(unittest.TestCase):

    def _iter_files(self):
        for dirpath, dirnames, filenames in os.walk(_APP):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)

    @staticmethod
    def _is_broad(node):
        handlers = list(node.type) if isinstance(node.type, ast.Tuple) else [node.type]
        names = [getattr(h, "id", getattr(h, "attr", "")) for h in handlers]
        return "Exception" in names or "BaseException" in names

    def test_scan_finds_the_codebase(self):
        self.assertGreater(len(list(self._iter_files())), 30, "扫描范围过小")

    def test_no_silent_except_pass(self):
        """宽 except 后紧跟 pass（零日志）= 完全静默，这类必须为零。"""
        bad = []
        for path in self._iter_files():
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or not self._is_broad(node):
                    continue
                body = [b for b in node.body
                        if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                and isinstance(b.value.value, str))]
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    bad.append("%s:%d" % (os.path.relpath(path, _ROOT), node.lineno))
        self.assertEqual(bad, [], "宽 except 完全静默（pass）：%s" % bad)

    def test_t17_fixed_call_really_has_default(self):
        p = os.path.join(_APP, "video_helpers.py")
        tree = ast.parse(io.open(p, encoding="utf-8").read())
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_recompose_episode_video":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                        if nm == "read_json_strict":
                            hits.append((sub.lineno,
                                         len(sub.args) + sum(1 for k in sub.keywords
                                                             if k.arg == "default")))
        self.assertTrue(hits, "没找到 read_json_strict 调用（函数改名？）")
        for line, nargs in hits:
            self.assertGreaterEqual(nargs, 2, "video_helpers.py:%d 仍漏传 default" % line)

    def test_ast_gate_covers_whole_app(self):
        bad = []
        for path in self._iter_files():
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if nm != "read_json_strict":
                    continue
                n = len(node.args) + (1 if any(k.arg == "default" for k in node.keywords) else 0)
                if n < 2:
                    bad.append("%s:%d" % (os.path.relpath(path, _ROOT), node.lineno))
        self.assertEqual(bad, [], "漏传 default 的调用点：%s" % bad)

    def test_no_fake_success_return_in_broad_except(self):
        """宽 except 里返回「像成功」的值（会伪造成功）必须为零。"""
        suspicious = []
        for path in self._iter_files():
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or not self._is_broad(node):
                    continue
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Return) or sub.value is None:
                        continue
                    v = sub.value
                    ok = False
                    if isinstance(v, ast.Constant) and v.value:
                        ok = True
                    if isinstance(v, ast.Name) and v.id in ("output_path", "True"):
                        ok = True
                    if ok:
                        suspicious.append("%s:%d" % (os.path.relpath(path, _ROOT), sub.lineno))
        self.assertEqual(suspicious, [],
                         "宽 except 里返回像成功的值（伪造成功）：%s" % suspicious)


# =========================================================================== #
# S3：_panel_dup_hit 是否会被否定语境绕过
# =========================================================================== #
class S3_PanelDupNegation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import sb_helpers
        cls.sb = sb_helpers

    TRUE_CASES = [
        "九宫格面板严重雷同（第3、4、6、7格动作构图几乎相同）",
        "判定为面板雷同，不合格",
        "构成面板雷同，不满足可直接使用的分镜要求",
        "面板重复",
        "格间重复",
    ]
    FALSE_CASES = [
        "角色动作推进连贯，未达面板雷同阈值，可判合格。",
        "无面板雷同",
        "未见面板雷同",
        "不构成面板雷同",
        "不存在雷同",
        "本镜无雷同",
        "未出现面板雷同",
        "不存在面板雷同",
        "没有发现面板雷同",
        "未发现面板重复",
        "本次不存在面板雷同的情况",
        "不足以判为面板雷同",
        "存在轻微面板重复倾向，不足以判为面板雷同",
        "未见糊化或面板雷同",
        "无畸变或面板雷同",
        "无色块/撕裂/面板雷同",
    ]

    #: 2026-10-12 实测确认会被**误判**的否定句式（t18 的 _PANEL_DUP_NEG 只含
    #: 「未达/未超/未触及/未见/没有/无/不构成/不算/达标/合格」，
    #: 「未出现 / 不存在 / 没有发现 / 未发现 / 不足以」都不在里面）。
    KNOWN_FALSE_POSITIVE_PHRASES = {
        "未出现面板雷同", "不存在面板雷同", "没有发现面板雷同",
        "未发现面板重复", "本次不存在面板雷同的情况", "不足以判为面板雷同",
        "存在轻微面板重复倾向，不足以判为面板雷同",
        "未见糊化或面板雷同", "无畸变或面板雷同", "无色块/撕裂/面板雷同",
    }

    def test_true_cases_all_detected(self):
        miss = [t for t in self.TRUE_CASES if not self.sb._panel_dup_hit(t)]
        self.assertEqual(miss, [], "真缺陷被漏判（→ 软放行入库）：%s" % miss)

    def test_known_false_positives_match_baseline(self):
        """把「已知误判」钉成基线：将来补齐否定词表 → 本断言变红 → 收紧基线。"""
        actual = {t for t in self.FALSE_CASES if self.sb._panel_dup_hit(t)}
        self.assertEqual(actual, self.KNOWN_FALSE_POSITIVE_PHRASES,
                         "误判集合与登记基线不一致（新增误判或已修复，需同步基线）")

    def test_double_negation_known_gap(self):
        """双重否定（「并非未达…实为严重雷同」）当前判 False —— 登记为已知盲区。"""
        self.assertFalse(self.sb._panel_dup_hit("并非未达面板雷同阈值，实为严重雷同"),
                         "若这里变 True，说明否定逻辑已改进 —— 请同步更新基线与注释")

    def test_real_defect_not_swallowed_by_distant_negation(self):
        self.assertTrue(self.sb._panel_dup_hit("第5格无主体，此后各格面板严重雷同"))


# =========================================================================== #
# S4：export_mode 回退为 all 的残留影响
# =========================================================================== #
class S4_ExportModeReverted(unittest.TestCase):

    def test_default_is_all(self):
        src = io.open(os.path.join(_APP, "comfyui_client.py"), encoding="utf-8").read()
        m = re.search(r'export_mode = .*?or "(all|segments)"\)', src, re.S)
        self.assertIsNotNone(m, "找不到 export_mode 默认值那一行（被改写？）")
        self.assertEqual(m.group(1), "all", "export_mode 默认值不是 all")

    def test_no_env_override_in_repo(self):
        hits = [rel for rel in (".env", ".env.example")
                if os.path.isfile(os.path.join(_ROOT, rel))
                and "MJSCXT_H3_EXPORT_MODE" in io.open(
                    os.path.join(_ROOT, rel), encoding="utf-8", errors="replace").read()]
        self.assertEqual(hits, [], "这些文件里仍有 MJSCXT_H3_EXPORT_MODE：%s" % hits)

    def test_unknown_value_falls_back_to_all(self):
        src = io.open(os.path.join(_APP, "comfyui_client.py"), encoding="utf-8").read()
        self.assertIn('if export_mode not in ("all", "segments")', src)
        self.assertIn('export_mode = "all"', src)

    def test_builder_default_is_all(self):
        src = io.open(os.path.join(_APP, "h3_director_builder.py"), encoding="utf-8").read()
        self.assertRegex(src, r'export_mode:\s*str\s*=\s*"all"')

    def test_per_scene_default_is_still_1(self):
        """⭐ 残留影响确认：默认仍走按场次 → segments 分支在生产不可达。

        这不是回退没做完，而是**已知功能缺口**（用户「单镜返工再拼回去」默认配置下失效）。
        本断言把事实钉住：哪天默认改成整集，这里会红并要求同步评估。
        """
        src = io.open(os.path.join(_APP, "video_helpers.py"), encoding="utf-8").read()
        n = len(re.findall(r'MJSCXT_VIDEO_PER_SCENE"\) or "1"', src))
        self.assertGreaterEqual(n, 3,
                                "按场次默认值出现次数变了（%d）—— 若默认已改为整集，"
                                "请同步复核「segments 分支可达性」结论" % n)

    def test_stale_comment_refs_default_segments(self):
        """注释仍写「默认 segments」——登记为文档不一致（非功能缺陷）。"""
        src = io.open(os.path.join(_APP, "comfyui_client.py"), encoding="utf-8").read()
        self.assertIn("为什么默认从 all 改为 segments", src,
                      "注释已更新 —— 请同步更新「文档不一致」清单")
        vsrc = io.open(os.path.join(_APP, "video_helpers.py"), encoding="utf-8").read()
        self.assertIn("export_mode 已改默认 segments", vsrc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
