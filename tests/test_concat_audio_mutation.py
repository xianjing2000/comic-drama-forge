# -*- coding: utf-8 -*-
"""音轨混搭缺陷的**旧实现**变异验证（真 ffmpeg 端到端）。

t22 的诚实性要求：新增的 `concat_videos` 音轨一致性测试是针对**已经修好**的缺陷写的，
跑绿只能说明「现在没问题」。这里把 `video_postprocess.concat_videos` 里那一行判定
**还原成修复前的写法**（内存里 exec 一个变异副本，不落盘、不改 app/），
再用真 ffmpeg 拼两段（一段有声、一段无声），证明：

    · 修复前：成片时长塌成**只有第一段**（后段画面+声音全丢 / 后段整段无声）；
    · 修复后：成片时长 = 两段之和。

如果哪天有人把这条判定改回去，`tests/test_video_concat_audio.TestConcatAudioReal`
会红；本文件则证明「它红得有道理」。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.abspath(os.path.join(_HERE, os.pardir, "app"))
for _p in (_APP, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SRC_PATH = os.path.join(_APP, "video_postprocess.py")

#: 修复前的那一行判定（t11 审计 P0）。
_OLD_COND = "need_reencode = has_any_audio and len(audio_params) > 1"
#: 当前的那一行（用作替换锚点，改了就说明口径又动了 —— 本测试会红并要求复核）。
_NEW_COND = "need_reencode = bool(n_missing_audio) or (has_any_audio and len(audio_params) > 1)"

_HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _load_mutated_concat():
    """把 video_postprocess.py 的判定行换回旧写法，在内存里 exec 出一个模块副本。"""
    with open(_SRC_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    assert _NEW_COND in src, (
        "找不到当前的判定行，说明有人改了 concat 分流口径："
        "请同步更新本变异测试与被它守护的断言")
    mod = types.ModuleType("t22_video_postprocess_mutated")
    mod.__file__ = _SRC_PATH
    exec(compile(src.replace(_NEW_COND, _OLD_COND), "<t22-mutated>", "exec"),
         mod.__dict__)
    return mod


@unittest.skipUnless(_HAVE_FFMPEG, "环境缺 ffmpeg/ffprobe，端到端变异验证跳过")
class TestConcatMutationReproducesDefect(unittest.TestCase):

    SEG = 2.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="t22_mut_concat_")
        cls.with_audio = cls._mk("a.mp4", True)
        cls.without_audio = cls._mk("b.mp4", False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _mk(cls, name, audio):
        p = os.path.join(cls.tmp, name)
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=%.2f" % cls.SEG]
        if audio:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=%.2f" % cls.SEG,
                    "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", p]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        if r.returncode != 0:
            raise RuntimeError("造段失败：%s" % (r.stderr or "")[-400:])
        return p

    def _dur(self, path):
        """容器时长（= 最长流的时长；视频流通常在，所以这条量不出音轨被截）。"""
        from media_probe import probe_media
        return float(probe_media(path).get("duration") or 0)

    def _audio_dur(self, path):
        """**音轨本身**的时长 —— 这才是被 demuxer 快路径截掉的东西。

        实测症状就是这么来的：成片视频 4.000s / 音轨 2.021s（后 2 秒全无声），
        只看容器时长会以为「一切正常」而放过。
        """
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        try:
            return float((r.stdout or "").strip().splitlines()[0])
        except (ValueError, IndexError):
            return 0.0

    def test_old_impl_loses_or_truncates_audio_and_duration(self):
        """⭐ 旧实现：[有音轨, 无音轨] → 音轨只覆盖第一段、成片时长塌一半。"""
        mod = _load_mutated_concat()
        out = os.path.join(self.tmp, "old_impl.mp4")
        ret = mod.VideoPostProcessor().concat_videos(
            [self.with_audio, self.without_audio], out, caller="t22.mut.old")
        self.assertTrue(ret, "旧实现的 demuxer 路径会「成功」返回 —— 这正是它危险的地方")
        d_video = self._dur(out)
        d_audio = self._audio_dur(out)
        # 缺陷的准确形状：**视频是全长，音轨只有第一段**（后段全无声，零告警）。
        self.assertAlmostEqual(d_video, self.SEG * 2, delta=0.4,
                               msg="旧实现的视频应是全长 4s（缺陷是静音，不是丢画面）")
        self.assertLess(d_audio, self.SEG * 1.5,
                        "旧实现的音轨应只覆盖第一段（%.3fs）—— 实测 4.000s 视频 / 2.021s 音轨"
                        % d_audio)

    def test_current_impl_keeps_full_length(self):
        """对照：当前实现必须保留全长时间（上面那条的红才有意义）。"""
        import video_postprocess as vp
        out = os.path.join(self.tmp, "new_impl.mp4")
        ret = vp.VideoPostProcessor().concat_videos(
            [self.with_audio, self.without_audio], out, caller="t22.mut.new")
        self.assertTrue(ret)
        self.assertAlmostEqual(self._dur(out), self.SEG * 2, delta=0.4)
        self.assertAlmostEqual(self._audio_dur(out), self.SEG * 2, delta=0.5,
                               msg="修好后音轨必须与视频等长（后段补的是等长静音轨）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
