# -*- coding: utf-8 -*-
"""concat_videos 音轨一致性回归测试（t22 测试层新增，2026-10-12）。

背景（t11 审计 P0，实测依据）：
    ```text
    [有音轨, 无音轨] → 判据 has_any_audio and len(audio_params) > 1 判不出混搭，
                        走 concat demuxer + -c copy「成功」，但音轨只覆盖第一段
                        （实测成片视频 4.000s / 音轨 2.021s，后 2 秒全无声，零告警）；
    [无音轨, 有音轨] → 第二段音轨被**整条丢弃**（实测输出无音频流）。
    ```
    本轮（2026-10-12）修法：只要**存在无音轨片段**或音轨参数不一致，一律走
    concat filter 重编码分支；缺音轨的片段先用 anullsrc 补一条等长静音轨。

为什么必须有本测试：这类缺陷**不会报错**（返回码 0、文件可播、时长只差一截），
人工验收时「后 2 秒没声音」常被当成「本来就没有台词」。没有客观断言就永远拦不住。

分层（见 t22 交付的测试策略）：
    · TestConcatRouting      —— 单元，**零 ffmpeg 依赖**：只钉住「走哪个分支」的判定，
                                用 monkeypatch 截住 _concat_reencode 避免真跑。
    · TestConcatAudioReal    —— 集成，**必须真 ffmpeg**：真造段 → 真 concat → 真 ffprobe
                                量时长/音频流。缺 ffmpeg 时 skip（不假装通过）。
    · TestSegSortKey         —— 单元，纯函数（本会话已修的 _seg_sort_key 回归）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.abspath(os.path.join(_HERE, os.pardir, "app"))
if _APP not in sys.path:
    sys.path.insert(0, _APP)


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# --------------------------------------------------------------------------- #
# 单元层：分支路由（不跑 ffmpeg）
# --------------------------------------------------------------------------- #
class TestConcatRouting(unittest.TestCase):
    """只验「该走 demuxer 还是该走重编码」的判定，不真拼。"""

    def setUp(self):
        import video_postprocess as vp
        self.vp = vp
        self.p = vp.VideoPostProcessor()
        self._td = tempfile.TemporaryDirectory(prefix="t22_routing_")
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def _route_for(self, probe_list):
        """用假的 probe_media + 假的 subprocess.run 驱动分流，返回 'reencode' / 'demuxer'。

        ⚠️ 不能靠「demuxer 失败后降级重编码」来反推分支 —— 那条路会把两个分支
        混在一起（第一版测试就是这么写错的）。这里直接截住 subprocess.run，
        看**第一条** ffmpeg 命令长什么样：
            · 含 `-filter_complex` → 走的是重编码分支
            · 含 `-f concat`/`-c copy` → 走的是 demuxer 快路径
        两个分支都只在内存里被观察，不真跑 ffmpeg。
        """
        seen = []

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, **kw):
            seen.append(list(cmd))
            # demuxer 快路径要求「返回码 0 **且** 产物非空」才算成功；这里顺手把
            # 最后那个位置参数当成输出路径写一个非空文件，让快路径能正常收尾
            # （否则它会降级到重编码分支，把两个分支混在一起）。
            if cmd:
                try:
                    with open(cmd[-1], "wb") as f:
                        f.write(b"fake")
                except OSError:
                    pass
            return _R()

        p1 = os.path.join(self.tmp, "a.mp4")
        p2 = os.path.join(self.tmp, "b.mp4")
        for _p in (p1, p2):
            with open(_p, "wb") as f:
                f.write(b"x")
        orig_probe, orig_run = self.vp.probe_media, self.vp.subprocess.run
        def _fake_probe(p):
            b = os.path.basename(p)
            if b.endswith(".work"):          # 补静音轨的临时副本，本测试只关心分支
                return self._audio()
            return dict(probe_list[b])
        self.vp.probe_media = _fake_probe
        self.vp.subprocess.run = _fake_run
        try:
            out = os.path.join(self.tmp, "out.mp4")
            self.p.concat_videos([p1, p2], out, caller="t22.routing")
        finally:
            self.vp.probe_media = orig_probe
            self.vp.subprocess.run = orig_run
        self.assertTrue(seen, "concat_videos 应当至少发起一次 ffmpeg 调用")
        # 补静音轨（anullsrc）与 demuxer 建 list 文件都属于辅助动作，跳过后再判主命令。
        main = [c for c in seen if "-filter_complex" in c
                or ("-f" in c and "concat" in c and "-c" in c and "copy" in c)]
        self.assertTrue(main, "找不到主拼接命令，实际命令：%s" % seen)
        if "-filter_complex" in main[0]:
            return "reencode"
        return "demuxer"

    @staticmethod
    def _audio(codec="aac", sr="44100", ch=2):
        return {"has_audio": True, "audio_codec": codec, "sample_rate": sr,
                "channels": ch, "has_video": True, "width": 720, "height": 1280,
                "fps": 30.0, "duration": 2.0}

    @staticmethod
    def _no_audio():
        return {"has_audio": False, "has_video": True, "width": 720, "height": 1280,
                "fps": 30.0, "duration": 2.0}

    def test_both_have_identical_audio_uses_demuxer(self):
        """场景 A：两段都有同参数音轨 → 才允许走 -c copy 快路径。"""
        probes = {"a.mp4": self._audio(), "b.mp4": self._audio()}
        route = self._route_for(probes)
        self.assertEqual(route, "demuxer",
                         "同参数音轨应走 demuxer；误判成重编码只是变慢，不算缺陷，但要留痕")

    def test_first_has_audio_second_missing_forces_reencode(self):
        """⭐ 场景 B（本轮真缺陷一）：[有, 无] 必须重编码，否则后半段整段无声。"""
        probes = {"a.mp4": self._audio(), "b.mp4": self._no_audio()}
        route = self._route_for(probes)
        self.assertEqual(route, "reencode",
                         "[有音轨, 无音轨] 被判成「音轨一致」→ 走 -c copy → 音轨只覆盖第一段")

    def test_first_missing_audio_second_has_audio_forces_reencode(self):
        """⭐ 场景 C（本轮真缺陷二）：[无, 有] 必须重编码，否则第二段音轨整条被丢。"""
        probes = {"a.mp4": self._no_audio(), "b.mp4": self._audio()}
        route = self._route_for(probes)
        self.assertEqual(route, "reencode",
                         "[无音轨, 有音轨] 被判成「音轨一致」→ -c copy → 第二段音轨整条丢失")

    def test_mismatched_audio_params_forces_reencode(self):
        """场景 D：有音轨但采样率/声道不一致 → 重编码统一（既有行为，防回归）。"""
        probes = {"a.mp4": self._audio(sr="44100", ch=2),
                  "b.mp4": self._audio(sr="24000", ch=1)}
        route = self._route_for(probes)
        self.assertEqual(route, "reencode")

    def test_all_missing_audio_goes_pure_video_reencode(self):
        """场景 E：全都没有音轨 → 重编码，但**不得凭空造音轨**（走纯视频图 + -an）。"""
        probes = {"a.mp4": self._no_audio(), "b.mp4": self._no_audio()}
        route = self._route_for(probes)
        self.assertEqual(route, "reencode",
                         "全无音轨也必须走重编码：demuxer 对无音轨多段同样会有时长/参数问题")

    def test_empty_input_returns_empty_string(self):
        """契约：空输入返回 ""（调用方必须判空）。"""
        self.assertEqual(self.p.concat_videos([], os.path.join(self.tmp, "o.mp4"),
                                              caller="t22.empty"), "")

    def test_h265_mixed_codec_audio_forces_reencode(self):
        """场景 F：音轨编码器不同（aac vs mp3）→ 必须重编码。"""
        probes = {"a.mp4": self._audio(codec="aac"), "b.mp4": self._audio(codec="mp3")}
        route = self._route_for(probes)
        self.assertEqual(route, "reencode")


# --------------------------------------------------------------------------- #
# 集成层：真 ffmpeg（缺环境则 skip，绝不假装通过）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_have_ffmpeg(), "环境缺 ffmpeg/ffprobe，集成层跳过（不算通过）")
class TestConcatAudioReal(unittest.TestCase):
    """真造段 + 真 concat + 真 ffprobe 量结果。"""

    SEG = 2.0  # 每段 2 秒

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="t22_concat_")
        cls.seg_audio = cls._mksrc("with_audio.mp4", with_audio=True, freq=440)
        cls.seg_audio2 = cls._mksrc("with_audio2.mp4", with_audio=True, freq=660)
        cls.seg_silent = cls._mksrc("silent.mp4", with_audio=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _mksrc(cls, name, with_audio, freq=440):
        p = os.path.join(cls.tmp, name)
        base = ["ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=%.2f" % cls.SEG]
        if with_audio:
            base += ["-f", "lavfi", "-i", "sine=frequency=%d:duration=%.2f" % (freq, cls.SEG),
                     "-c:a", "aac", "-b:a", "128k"]
        else:
            base += ["-an"]
        base += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", p]
        r = subprocess.run(base, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        if r.returncode != 0 or not os.path.isfile(p) or os.path.getsize(p) == 0:
            raise RuntimeError("造测试段失败：%s / %s" % (r.returncode, (r.stderr or "")[-400:]))
        return p

    def _concat(self, paths, tag):
        import video_postprocess as vp
        out = os.path.join(self.tmp, tag + ".mp4")
        ret = vp.VideoPostProcessor().concat_videos(paths, out, caller="t22.real." + tag)
        self.assertTrue(ret and os.path.isfile(ret) and os.path.getsize(ret) > 0,
                        "concat 应成功并产出非空文件，实际返回 %r" % (ret,))
        return ret

    def _probe(self, path):
        from media_probe import probe_media
        info = probe_media(path)
        self.assertTrue(info.get("ok"), "ffprobe 应能读产物：%s" % info.get("error"))
        return info

    def test_both_segments_with_audio_keeps_full_audio(self):
        """两段都有音轨 → 成片音轨长度 ≈ 视频长度 ≈ 段长之和（快路径不得截断）。"""
        out = self._concat([self.seg_audio, self.seg_audio2], "both_audio")
        info = self._probe(out)
        self.assertTrue(info["has_audio"], "成片必须有音轨")
        self.assertAlmostEqual(info["duration"], self.SEG * 2, delta=0.35,
                               msg="成片总时长应≈两段之和")

    def test_mixed_audio_video_not_truncated_to_first_segment(self):
        """⭐⭐ 场景 B 真测：[有音轨, 无音轨] —— 修前成片视频 4s / 音轨 2s。

        断言：成片总时长必须≈4s（若音轨只有 2s，-shortest 之外的容器时长也会塌成 2s），
        且音轨流存在。这条断言在修前会红：实测 4.000s 视频 / 2.021s 音轨。
        """
        out = self._concat([self.seg_audio, self.seg_silent], "mixed_a_then_none")
        info = self._probe(out)
        self.assertTrue(info["has_audio"],
                        "混搭场景必须补静音轨（否则第二段整段无声）")
        self.assertAlmostEqual(info["duration"], self.SEG * 2, delta=0.4,
                               msg="成片时长被第一段的音轨截断了（典型症状：后段全无声）")

    def test_mixed_audio_reverse_order_does_not_drop_second_track(self):
        """⭐⭐ 场景 C 真测：[无音轨, 有音轨] —— 修前输出**无音频流**。"""
        out = self._concat([self.seg_silent, self.seg_audio], "none_then_mixed")
        info = self._probe(out)
        self.assertTrue(info["has_audio"],
                        "第二段的音轨被整条丢弃了（修前实测输出无音频流）")
        self.assertAlmostEqual(info["duration"], self.SEG * 2, delta=0.4)

    def test_all_silent_segments_stay_silent(self):
        """场景 E：全无音轨 → 成片**不得**凭空多出一条音轨（-an 纯视频图）。"""
        out = self._concat([self.seg_silent, self.seg_silent], "all_silent")
        info = self._probe(out)
        self.assertFalse(info["has_audio"],
                         "本来就没有音频的工程不该被凭空造出一条音轨")
        self.assertAlmostEqual(info["duration"], self.SEG * 2, delta=0.4)

    def test_no_padaudio_temp_left_behind(self):
        """G8 约定：补音轨的 .work 临时副本必须清理干净（成功路径）。"""
        out = self._concat([self.seg_audio, self.seg_silent], "cleanup_check")
        leftovers = [f for f in os.listdir(os.path.dirname(out)) if ".padaudio" in f]
        self.assertEqual(leftovers, [], "补音轨临时副本未清理：%s" % leftovers)


# --------------------------------------------------------------------------- #
# 单元层：_seg_sort_key（本会话已修的 bug 的回归钉）
# --------------------------------------------------------------------------- #
class TestSegSortKey(unittest.TestCase):
    """分段 mp4 排序键：必须先剥扩展名再取数字。

    实测坑：`re.findall(r"[0-9]+", "a_00010_.mp4") -> ['00010', '4']` —— mp4 自带 4，
    取 nums[-1] 恒等于 4，5 个不同文件排序键全变 (0, 4) = 等于没排序 → 成片画面乱序。
    """

    def setUp(self):
        import video_helpers as vh
        self.key = vh._seg_sort_key

    def test_extension_digits_do_not_dominate(self):
        """⭐ 逐文件断言：扩展名里的 4 不得成为排序键。"""
        for name, want in (("a_00010_.mp4", 10), ("a_00001_.mp4", 1),
                           ("seg_0007.mp4", 7), ("x_00000_.mp4", 0)):
            self.assertEqual(self.key(name), (0, want),
                             "%s 的排序键应取扩展名前的序号" % name)

    def test_shuffled_segment_list_sorts_numerically(self):
        """⭐ 真缺陷复现：乱序输入的 5 段必须排成 1,2,10,11,21（修前全部并列）。"""
        got = [os.path.basename(p) for p in
               sorted(["ep_00010_.mp4", "ep_00002_.mp4", "ep_00021_.mp4",
                       "ep_00001_.mp4", "ep_00011_.mp4"], key=self.key)]
        self.assertEqual(got, ["ep_00001_.mp4", "ep_00002_.mp4", "ep_00010_.mp4",
                               "ep_00011_.mp4", "ep_00021_.mp4"])

    def test_keys_are_not_all_equal(self):
        """修前的直接症状：5 个不同文件的排序键全部相同。"""
        keys = {self.key(n) for n in ["ep_00010_.mp4", "ep_00002_.mp4",
                                      "ep_00021_.mp4", "ep_00001_.mp4"]}
        self.assertEqual(len(keys), 4, "排序键退化（扩展名里的数字赢了）：%s" % keys)

    def test_takes_last_number_before_extension(self):
        """一个文件名里有多个数字时，取**扩展名前最后一个**（镜头/段号在后）。"""
        self.assertEqual(self.key("ep01_shot07_00003_.mp4"), (0, 3))

    def test_no_digit_sorts_last_and_does_not_raise(self):
        """异常命名不抛异常，且排在所有有号段之后（保持相对稳定）。"""
        with_digits = self.key("a_00001_.mp4")
        no_digit = self.key("readme_final.mp4")
        self.assertEqual(no_digit[0], 1, "取不到数字的应打降级标记")
        self.assertLess(with_digits[0], no_digit[0], "有号段必须排在无号段之前")

    def test_empty_and_none_are_safe(self):
        for bad in ("", None, "   "):
            self.assertEqual(self.key(bad)[0], 1, "空/None 输入不得抛异常")

    def test_handles_uppercase_extension_and_no_extension(self):
        self.assertEqual(self.key("seg_0012.MP4"), (0, 12))
        self.assertEqual(self.key("seg_0013"), (0, 13))


if __name__ == "__main__":
    unittest.main(verbosity=2)
