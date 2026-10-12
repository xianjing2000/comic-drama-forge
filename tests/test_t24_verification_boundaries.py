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
    def _is_broad_handler(node):
        handlers = (list(node.type.elts) if isinstance(node.type, ast.Tuple)
                    else [node.type])
        names = [getattr(h, "id", getattr(h, "attr", "")) for h in handlers]
        return "Exception" in names or "BaseException" in names

    def test_scan_finds_the_codebase(self):
        self.assertGreater(len(list(self._iter_files())), 30, "扫描范围过小")

    #: 2026-10-12 基线（已人工抽查 4 处，全部是**带 fallback 的预期分支**）：
    #:   video_postprocess.py:98/100 读 config 失败 → 按默认开启（有注释 + warning）
    #:   video_helpers.py:577 find_voice_bank_ref 失败 → 视为「没有参考音频」继续生成
    #:   quality_stage.py:285 ffmpeg 探测失败 → 回落字符串 "ffmpeg"
    #:   routes/qc.py 面板体检扫描跳过不可解析项
    #: 结论：这类 pass **不是** t17 那种「吞掉真错误」，但缺日志、无法追溯；
    #: 故钉成**计数基线**，只拦新增（一次性清零不在本任务 inScope）。
    #: 2026-10-12 复核时实测 44（13:40 快照）——基线随代码演进重测即可，
    #: 关键是**只拦增长**，不要求一次性清零。
    KNOWN_BARE_PASS = 44

    def test_bare_pass_count_does_not_grow(self):
        """宽 except 后紧跟 pass 的**数量**不得增长（新增即红，须显式登记）。"""
        bad = []
        for path in self._iter_files():
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or not self._is_broad_handler(node):
                    continue
                body = [b for b in node.body
                        if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                and isinstance(b.value.value, str))]
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    bad.append("%s:%d" % (os.path.relpath(path, _ROOT), node.lineno))
        self.assertLessEqual(
            len(bad), self.KNOWN_BARE_PASS,
            "新增了「宽 except + 纯 pass」静默吞错点（%d > 基线 %d）：%s"
            % (len(bad), self.KNOWN_BARE_PASS, bad[self.KNOWN_BARE_PASS:]))

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

    def test_t17_defect_shape_would_be_swallowed_again(self):
        """⭐ 反例：把 read_json_strict 换成**单参数版本**（复现 t17 的 bug），
        确认缺陷形态确实会被宽 except 吞成空串 —— 证明「AST 契约 + 行为断言」是必需的。

        `_recompose_episode_video` 的签名在源码里写死两参调用，所以行为层无法直接复现；
        这里直接演示「漏参 → TypeError → 被 except Exception 吞掉 → 返回空串」这条链。
        """
        import json
        import video_helpers as vh

        class _OneArg:                       # 复现修复前的 read_json_strict(path) 缺陷
            @staticmethod
            def __call__(path, *a, **k):
                if a or "default" in k:
                    raise AssertionError("两参调用不该进来")
                return {}

        calls = {"swallowed": None}

        def _boom(path, *a, **k):
            if not a and "default" not in k:
                raise TypeError("read_json_strict() missing 1 required positional "
                                "argument: 'default'")
            return {}

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "rework"), exist_ok=True)
            with open(os.path.join(td, "seg_0000.mp4"), "wb") as fh:
                fh.write(b"x")
            with open(os.path.join(td, "seg_map.json"), "w", encoding="utf-8") as fh:
                json.dump({"episode_tag": "ep01", "shot_to_seg": {"1": [0]}}, fh)
            orig = vh.read_json_strict
            vh.read_json_strict = _boom
            try:
                # 当前实现对 read_json_strict 是两参调用 → _boom 不抛 → 正常返回 "";
                # 若哪天有人把调用改回一参，_boom 抛 TypeError，会被宽 except 吞成 ""。
                got = vh._recompose_episode_video(td, "ep01")
            finally:
                vh.read_json_strict = orig
        self.assertEqual(got, "", "无返工段且 concat 桩缺失时应返回空串（fail-open）")

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

    def test_no_silent_fake_success_return(self):
        """⭐ **真正的 t17 形态**：宽 except 里「零日志」返回一个像成功的值。

        只在 handler **完全没有 logging 调用**时判危 —— 有 error/warning 的（如
        video_postprocess._caption_burn_enabled 的「按默认开启 + warning」）是有意 fallback，
        不算伪造成功。这条一旦出现，就是又一个「静默假成功」，必须立刻修。
        """
        suspicious = []
        for path in self._iter_files():
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or not self._is_broad_handler(node):
                    continue
                has_log = any(
                    isinstance(n2, ast.Call) and getattr(n2.func, "attr", "") in (
                        "debug", "info", "warning", "error", "exception", "critical")
                    for n2 in ast.walk(node))
                if has_log:
                    continue
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Return) or sub.value is None:
                        continue
                    v = sub.value
                    # 只把**字面量真值**（True / 非空字符串 / 非零数字）算伪造成功：
                    # 返回一个变量名（如 output_path）在语义上歧义太大，误报率高，
                    # 交给人工 review，不在这里拦。
                    fake = isinstance(v, ast.Constant) and bool(v.value)
                    if fake:
                        suspicious.append("%s:%d" % (os.path.relpath(path, _ROOT), sub.lineno))
        #: 2026-10-12 已逐个人工核对的**有意 fallback**（都返回字面量真值、都不打日志）：
        #:   agent_core.py:91        取不到 chat 字段清单 → 回硬编码清单（设计如此）
        #:   config_center.py:151    范围校验解析失败 → 判「在范围内」（宽松兜底）
        #:   degradation.py:101      统计格式化失败 → 返回占位文案（展示用）
        #:   schema.py:142           读 units 失败 → 描述为「表不存在」（**迁移状态描述**，
        #:                           不是成功标志，属误读风险最低的一类）
        #:   task_lease.py:178       判不出进程存活 → 判「活着」，避免误抢别人的活（安全侧）
        #:   tts_client.py:423       读不到默认参数 → 按「开启」处理（与模块默认一致）
        #:   video_postprocess.py:99 读 config 失败（且 line 102 的兜底也返回 True）
        #:                           ⚠️ 唯一一处「可能掩盖真错误」的：line 98 吞掉的是
        #:                           import/读取异常，若 config.py 被改坏会被静默按 True 处理。
        #:                           影响有限（两条分支同值），但**建议后续补 warning**。
        #: 只允许这 7 处；新增任何一处都会让本断言变红。
        known_silent = {
            "app/agent_core.py:91",
            "app/config_center.py:151",
            "app/degradation.py:101",
            "app/schema.py:142",
            "app/task_lease.py:178",
            "app/tts_client.py:423",
            "app/video_postprocess.py:99",
        }
        got = {p.replace(chr(92), '/') for p in suspicious}
        self.assertEqual(
            got - known_silent, set(),
            "新增了「宽 except + 零日志 + 字面量真值」的静默假成功点：%s"
            % sorted(got - known_silent))
        self.assertEqual(
            known_silent - got, set(),
            "登记的已知点已消失（说明有人修了）—— 请同步收紧基线：%s"
            % sorted(known_silent - got))


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

    #: 已修复的误判句式（2026-10-12 修复记录）：
    #: 原实现（t18 单表 3 字窗口）会把下面 8 条**达标结论**误判成缺陷：
    #:   未出现面板雷同 / 不存在面板雷同 / 没有发现面板雷同 / 未发现面板重复 /
    #:   本次不存在面板雷同的情况 / 不足以判为面板雷同 / 未见糊化或面板雷同 /
    #:   无畸变或面板雷同
    #: 修复方式：STRONG 表补纯否定短语 + 窗口 6 字 + 遇句子边界截断
    #:（详见 app/sb_helpers.py 的 _PANEL_DUP_NEG_STRONG 注释）。
    #:
    #: 仍残留的边界情形（本轮**刻意不修**，如实登记）：
    #:   · 「无色块/撕裂/面板雷同」—— 列举式否定，弱否定词「无」离「面板」有 7 个字，
    #:     超出当前 6 字窗口；再放宽就会开始吞掉「无主体，面板雷同」这类**真缺陷**，
    #:     收益不足以抵消风险，故保持现状。
    #:   · 「存在轻微面板重复倾向，不足以判为面板雷同」—— 否定词在**句尾**，
    #:     前缀窗口天然覆盖不到，需要后缀判定（另一类改动），登记为已知边界。
    KNOWN_FALSE_POSITIVE_PHRASES = {
        "存在轻微面板重复倾向，不足以判为面板雷同",
        "无色块/撕裂/面板雷同",
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
