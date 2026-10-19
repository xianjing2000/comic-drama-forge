# -*- coding: utf-8 -*-
"""批间末帧锚定（2026-10-19，用户 P0「5 段与 5 段之间卡顿」）

缺陷：场次内按 MJSCXT_H3_BATCH_SEGMENTS=5 分批后，批与批是彼此独立的 ComfyUI
提交。H3 插件的「段间引导」只在**同一次提交内**把上一段尾帧钉进下一段
conditioning（``continuityFromPrev=(i>0)``）；新批第 1 段 i=0 拿不到上一批尾帧，
批间只是 ffmpeg 硬拼 → 第 5 段与第 6 段之间出现可见跳变/卡顿。

修法（与「跨场首镜挂上一场末镜」同构，但必须同步声明提示词）：
  1. 从上一批成片抽末帧（ffmpeg -sseof -0.5）；
  2. 挂到本批首段 ``reference_images`` 末尾 = 下一个 ``<Picture K>`` 槽位；
  3. 在 ``subject_definitions`` 声明该图用途、在 ``detailed_description`` 写
     开场承接句 —— 构建器按提示词声明的最大编号截断参考图
     （``h3_director_builder.picture_capacity``），**只挂图不写标签会被静默丢弃**。

本测试不联网、不依赖 GPU / ComfyUI / ffmpeg（抽取函数只测 fail-open 路径）。
运行：python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py"
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ.setdefault('MJSCXT_DATA_DIR', tempfile.mkdtemp(prefix='tailanchor_'))

import h3_director_builder   # noqa: E402
import video_helpers         # noqa: E402

_PROMPT = (
    "subject_definitions:\n"
    "<Picture 1> is the reference image defining the appearance of A.\n"
    "<Picture 2> is the reference image defining the appearance of B.\n"
    "<Picture 3> is the storyboard reference for this shot.\n"
    "\n"
    "summary:\nA meets B in the alley.\n"
    "\n"
    "detailed_description:\n"
    "[Shot 1] A medium shot: A turns and looks at B.\n"
    "\n"
    "overall_soundscape:\nQuiet alley ambience.\n"
    "\n"
    "non_diegetic_music:\nLow sustained strings."
)


def _touch(path, payload=b'\x89PNG\r\n\x1a\n'):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(payload)
    return path


class PromptSectionAppendTests(unittest.TestCase):
    """提示词手术：只能动目标 section，六段结构不能被破坏。"""

    def test_append_to_subject_definitions_stays_in_section(self):
        out = video_helpers._prompt_section_append_line(
            _PROMPT, 'subject_definitions', 'NEW-LINE-A')
        self.assertIn('NEW-LINE-A', out)
        # 必须落在 subject_definitions 与 summary 之间
        self.assertLess(out.index('NEW-LINE-A'), out.index('summary:'))
        self.assertLess(out.index('<Picture 3>'), out.index('NEW-LINE-A'))
        # 其它段原样
        self.assertIn('[Shot 1] A medium shot', out)
        self.assertTrue(out.endswith('Low sustained strings.'))

    def test_append_to_detailed_description_stays_in_section(self):
        out = video_helpers._prompt_section_append_line(
            _PROMPT, 'detailed_description', 'NEW-LINE-B')
        self.assertIn('NEW-LINE-B', out)
        self.assertGreater(out.index('NEW-LINE-B'), out.index('[Shot 1]'))
        self.assertLess(out.index('NEW-LINE-B'), out.index('overall_soundscape:'))
        # 不得污染上一段
        self.assertNotIn('NEW-LINE-B', out[:out.index('detailed_description:')])

    def test_append_to_last_section(self):
        out = video_helpers._prompt_section_append_line(
            _PROMPT, 'non_diegetic_music', 'NEW-LINE-C')
        self.assertTrue(out.endswith('NEW-LINE-C'))

    def test_missing_section_appends_at_end(self):
        out = video_helpers._prompt_section_append_line(_PROMPT, 'no_such_section', 'X')
        self.assertTrue(out.endswith('X'))
        self.assertIn(_PROMPT, out)

    def test_empty_line_is_noop(self):
        self.assertEqual(video_helpers._prompt_section_append_line(_PROMPT, 'summary', ''),
                         _PROMPT)


class AnchorBatchHeadTests(unittest.TestCase):
    """核心行为：末帧 → <Picture K>，图与提示词标签必须同时到位。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tailanchor_case_')
        self.ref = _touch(os.path.join(self.tmp, 'storyboard.png'))
        self.tail = _touch(os.path.join(self.tmp, 'tail.png'))
        self.segs = [
            {'name': 'shot_06', 'duration': 4.0, 'prompt': _PROMPT,
             'reference_images': [self.ref]},
            {'name': 'shot_07', 'duration': 4.0, 'prompt': 'p2',
             'reference_images': [self.ref]},
        ]

    def test_tail_is_appended_and_declared_in_prompt(self):
        # _PROMPT 声明 <Picture 1..3> = 公共 2 张 + 分镜 1 张；本段 reference_images
        # 只放段级图（分镜），公共张数由 common_count 传入（与批循环调用同口径）。
        out = video_helpers._anchor_batch_head_to_prev_tail(
            self.segs, self.tail, common_count=2)
        head = out[0]
        self.assertEqual(head['reference_images'], [self.ref, self.tail])
        # 公共 2 + 本段 1 → 新图必须是槽位 <Picture 4>
        self.assertIn('<Picture 4>', head['prompt'])
        self.assertEqual(h3_director_builder.picture_capacity(head['prompt']), 4)
        self.assertEqual(head.get('batch_tail_anchor'), self.tail)
        # 声明必须同时出现在两个 section
        subj = head['prompt'][:head['prompt'].index('summary:')]
        desc = head['prompt'][head['prompt'].index('detailed_description:'):
                               head['prompt'].index('overall_soundscape:')]
        self.assertIn('<Picture 4>', subj)
        self.assertIn('<Picture 4>', desc)
        self.assertIn('Opening continuity', desc)

    def test_original_dicts_are_not_mutated(self):
        video_helpers._anchor_batch_head_to_prev_tail(
            self.segs, self.tail, common_count=2)
        self.assertEqual(self.segs[0]['reference_images'], [self.ref])
        self.assertEqual(self.segs[0]['prompt'], _PROMPT)
        self.assertNotIn('batch_tail_anchor', self.segs[0])

    def test_label_follows_actual_slot_not_prompt_max(self):
        """标签 = 公共图 + 本段**实际存在**的图 + 1（插件按槽位编号）。

        提示词历史编号可能因图缺失/未上传而偏大；若盲取「提示词最大号 + 1」，
        声明会指向不存在的空槽（H3 拿到「有指代、无图」会自行脑补）。
        """
        out = video_helpers._anchor_batch_head_to_prev_tail(
            self.segs, self.tail, common_count=0)
        self.assertIn('continue seamlessly from <Picture 2>', out[0]['prompt'])

    def test_missing_ref_files_do_not_shift_label(self):
        """不存在的参考图不占槽（构建器上传时会跳过）→ 标签按真实槽位算。"""
        segs = [{'name': 'shot_06', 'prompt': _PROMPT,
                 'reference_images': [os.path.join(self.tmp, 'missing.png')]},]
        out = video_helpers._anchor_batch_head_to_prev_tail(segs, self.tail)
        # 唯一存在的图是末帧自己 → 槽位 1（用锚点句核验，原 prompt 本来就有 <Picture 1>）
        self.assertIn('continue seamlessly from <Picture 1>', out[0]['prompt'])

    def test_slots_full_skips_without_mutation(self):
        full = [_touch(os.path.join(self.tmp, 'r%d.png' % i)) for i in range(9)]
        segs = [{'name': 'shot_06', 'prompt': _PROMPT, 'reference_images': full}]
        out = video_helpers._anchor_batch_head_to_prev_tail(segs, self.tail)
        self.assertIs(out, segs)
        self.assertNotIn('batch_tail_anchor', segs[0])
        self.assertEqual(segs[0]['reference_images'], full)

    def test_missing_tail_is_noop(self):
        out = video_helpers._anchor_batch_head_to_prev_tail(
            self.segs, os.path.join(self.tmp, 'nope.png'))
        self.assertIs(out, self.segs)
        self.assertNotIn('batch_tail_anchor', self.segs[0])

    def test_empty_input_is_noop(self):
        self.assertEqual(video_helpers._anchor_batch_head_to_prev_tail([], self.tail), [])


class ExtractTailFailOpenTests(unittest.TestCase):
    """抽取失败必须返回 ""（绝不抛），批间退化为原有硬切。"""

    def test_empty_path(self):
        self.assertEqual(
            video_helpers._extract_batch_tail_frame('', tempfile.mkdtemp(), 1, 0), '')

    def test_missing_file(self):
        self.assertEqual(
            video_helpers._extract_batch_tail_frame(
                os.path.join(tempfile.mkdtemp(), 'nope.mp4'),
                tempfile.mkdtemp(), 1, 0), '')

    def test_corrupt_video(self):
        tmp = tempfile.mkdtemp(prefix='tailanchor_bad_')
        bad = _touch(os.path.join(tmp, 'bad.mp4'), b'not-a-video')
        # 有 ffmpeg 时 rc!=0；无 ffmpeg 时 FileNotFoundError —— 两者都必须返回 ""
        self.assertEqual(
            video_helpers._extract_batch_tail_frame(bad, tmp, 1, 0), '')


if __name__ == '__main__':
    unittest.main()
