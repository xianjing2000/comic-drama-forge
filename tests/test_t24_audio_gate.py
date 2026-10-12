# -*- coding: utf-8 -*-
"""T24 验证门 · t19 成片音频门禁的独立复核（真 ffmpeg / 真生产产物）。

t19 自述：新增音频客观层（整轨口径），静音成片会被判 failed，有声成片 passed。
本文件不复用它的脚本，而是**直接调 quality_stage 的公开函数**重新量一遍。

关键设计（为什么这么写）：
  · 「静音」夹具用 anullsrc 真造（mean 会被压到 -90dB 级），不 mock 电平；
  · 「有声」用**生产真实产物**——证明整轨口径没有把有台词的成片误杀
    （这是 t19 的整改动机：拿单句 50% 占比去卡整轨必然误报）；
  · 同时验证「探针不可用 → 未判定」与环境隔绝（不得把环境问题当内容缺陷）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

_PROD = os.path.join(os.environ.get("APPDATA", ""), "mjscxt-desktop",
                     "mjscxt-data")
#: 生产真实「有台词」成片（进境_整本小说），t19 报的 mean -14.6dB / speech 0.89
_PROD_VIDEO = os.path.join(_PROD, "output", "videos", "进境_整本小说", "scene_01.mp4")

_HAVE_FFMPEG = bool(shutil.which("ffmpeg"))


class T19_AudioGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import quality_stage
        cls.qs = quality_stage
        cls.tmp = tempfile.mkdtemp(prefix="t24_audio_")
        cls.silent = None
        if _HAVE_FFMPEG:
            p = os.path.join(cls.tmp, "silent.mp4")
            r = subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=8",
                 "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                 "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", p],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180)
            if r.returncode == 0 and os.path.getsize(p) > 0:
                cls.silent = p

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @unittest.skipUnless(_HAVE_FFMPEG, "缺 ffmpeg")
    def test_silent_track_is_flagged(self):
        """⭐ 静音轨必须判 blocked/failed —— 这是 t19 要堵的核心缺陷。"""
        if not self.silent:
            self.skipTest("静音夹具造失败")
        v = self.qs.audio_quality_verdict(self.silent)
        self.assertTrue(v.get("available"), "应拿到电平：%s" % v.get("error"))
        self.assertTrue(v.get("blocked"), "静音轨未被拦：%s" % v)
        m = v.get("metrics") or {}
        self.assertLess(float(m.get("mean_db") or 0), -50.0,
                        "静音轨 mean_db 应极低，实测 %s" % m.get("mean_db"))

    @unittest.skipUnless(os.path.isfile(_PROD_VIDEO), "无生产真实成片可用")
    def test_real_production_video_is_not_false_positive(self):
        """⭐ 生产真实成片（有台词）不得被整轨口径误杀。"""
        v = self.qs.audio_quality_verdict(_PROD_VIDEO)
        self.assertTrue(v.get("available"), "应拿到电平：%s" % v.get("error"))
        self.assertFalse(v.get("blocked"),
                         "生产有台词成片被误判为不合格（会白拦真交付）：%s" % v)
        self.assertFalse(v.get("check_speech_ratio"),
                         "整轨口径必须关掉「有声占比下限」，否则必然误报")

    def test_missing_file_is_undetermined_not_defect(self):
        """反例：文件不存在 → 未判定（available=False），绝不能判成「无声缺陷」。"""
        v = self.qs.audio_quality_verdict(os.path.join(self.tmp, "nope.mp4"))
        self.assertFalse(v.get("available"), "不存在文件不该有电平结论")
        self.assertFalse(v.get("blocked"), "环境/输入问题不得判成内容缺陷")
        self.assertIsNone(v.get("passed"), "未判定时 passed 必须为 None（不伪造结论）")

    def test_probe_unavailable_fails_open(self):
        """反例：探针抛异常 → available=False、不 blocked（fail-open）。"""
        def _boom(_p):
            raise RuntimeError("ffmpeg 挂了")

        v = self.qs.audio_quality_verdict("whatever.mp4", probe=_boom)
        self.assertFalse(v.get("available"))
        self.assertFalse(v.get("blocked"))
        self.assertTrue(v.get("error"), "必须留下错误原因")

    @unittest.skipUnless(_HAVE_FFMPEG, "缺 ffmpeg")
    def test_silent_shots_are_located_by_shot(self):
        """静音必须能**定位到镜号**（t19 的交付清单诉求）。"""
        if not self.silent:
            self.skipTest("静音夹具造失败")
        shots = [{"shot_id": i + 1, "duration": 2.0} for i in range(4)]
        v = self.qs.audio_quality_verdict(self.silent, shots=shots)
        if not v.get("available"):
            self.skipTest("未取到电平：%s" % v.get("error"))
        # 时长 8s / 4 镜 × 2s，整轨静音 → 应至少定位到若干镜
        self.assertTrue(v.get("silent_shots"),
                        "8s 全静音应能定位到无声镜号，实际：%s" % v.get("silent_shots")[:2])
        for s in v["silent_shots"]:
            self.assertIn("shot_id", s)
            self.assertIn("start", s)
            self.assertIn("end", s)


class T19_GateIsRecordOnlyByDefault(unittest.TestCase):
    """默认只记录不阻断 —— 这是**生产约束下的有意取舍**，不是缺陷；钉住它。"""

    def test_pipeline_gate_default_is_not_block(self):
        src = open(os.path.join(_APP, "pipeline.py"), encoding="utf-8").read()
        self.assertIn('MJSCXT_FINAL_AUDIO_GATE', src)
        # 只有显式 block 才硬拦；关闭值是 off/0/false/no
        self.assertIn('in ("0", "off", "false", "no")', src,
                      "关闭口径变了 —— 请同步复核「默认不阻断」结论")

    def test_gate_does_not_return_failure_on_silent(self):
        """静态复核：静音分支只 logger.warning，不 return 失败（不阻断成片落盘）。"""
        src = open(os.path.join(_APP, "pipeline.py"), encoding="utf-8").read()
        i = src.find("成片音频客观层判定不合格")
        self.assertGreater(i, 0, "找不到音频不合格告警分支")
        seg = src[max(0, i - 120):i + 900]        # 往前取一点，把 logger.warning 本身包进来
        self.assertIn("logger.warning", seg)
        self.assertNotIn("return False", seg,
                         "静音分支若开始 return 失败 = 硬阻断，需 captain 决策")


if __name__ == "__main__":
    unittest.main(verbosity=2)
