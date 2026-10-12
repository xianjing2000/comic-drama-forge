# -*- coding: utf-8 -*-
r"""文字类道具**后期叠加**（任务 t6）回归测试 —— 不依赖 GPU。

被测对象：app/video_text_overlay.py（新模块）+ pipeline.step_final 的接线。
覆盖：时间轴换算（与 step_final 既有口径逐值一致）、目标框/等比缩放/裁切几何、
      文本来源（资产图 / canon 原文，禁止读 shot.surface_text）、叠加计划、
      滤镜串构造（Windows 路径不进滤镜）、ffmpeg 命令契约与音轨保持。

真实数据基准（进境_整本小说 第1集 + 其成片 scene_01.mp4）：
  · 剧本 60 镜，剧本总时长 309.5s；成片 ffprobe 实测 164.542s
  · shot#21-28 是「楼层安全守则」镜头（换算后 53.96-77.62s）
  · shot#12-14 / 18 是「天合科技员工卡」镜头
  · 资产图 1248x1248 RGBA（守则纸面几乎铺满）；成片 960x544
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import video_text_overlay as V  # noqa: E402

_FFMPEG = bool(shutil.which('ffmpeg'))

# ---- 真实剧本镜头（durations 逐字抄自 进境_整本小说 第1集.json 的前 30 镜）----
# 真实 60 镜 duration（逐字抄自 进境_整本小说 第1集.json；合计 309.5s）
REAL_DURATIONS = ([5.0] * 7 + [6.5] + [5.0] * 14 + [7.5, 5.0, 7.0]
                  + [5.0] * 33 + [8.0, 5.5])
REAL_TOTAL = 164.542


def _shots(durations=None, items=None):
    durations = durations or REAL_DURATIONS
    items = items or []
    out = []
    for i, d in enumerate(durations):
        s = {'shot_id': i + 1, 'duration': d}
        if i in items:
            s['items_in_shot'] = list(items[i])
        out.append(s)
    return out


def _old_step_final_formula(shots, total_sec):
    """pipeline.step_final 改造**前**的内联口径（逐字抄自原 L1492-1505），用于等价性断言。"""
    subs, cur = [], 0.0
    for s in shots:
        dur = float(s.get("duration") or 5)
        _script_total = sum(float(x.get("duration") or 5) for x in shots) or 1.0
        if total_sec > 0:
            dur = dur / _script_total * total_sec
        subs.append({"start": round(cur, 3), "end": round(cur + dur, 3)})
        cur += dur
    return subs


class TestTimeline(unittest.TestCase):
    """① 时间轴：与 step_final 既有换算口径逐值一致（不得自创算法）。"""

    def test_matches_legacy_inline_formula(self):
        shots = _shots()
        spans = V.shot_time_spans(shots, REAL_TOTAL)
        legacy = _old_step_final_formula(shots, REAL_TOTAL)
        self.assertEqual(len(spans), len(legacy))
        for sp, lg in zip(spans, legacy):
            self.assertAlmostEqual(sp['start'], lg['start'], places=3)
            self.assertAlmostEqual(sp['end'], lg['end'], places=3)

    def test_spans_sum_to_measured_total(self):
        spans = V.shot_time_spans(_shots(), REAL_TOTAL)
        self.assertAlmostEqual(spans[-1]['end'], REAL_TOTAL, places=2)

    def test_real_prop_windows(self):
        """真实换算式（60 镜 / 剧本 309.5s / 成片实测 164.542s）：
        守则首镜 shot#21 → 53.961s 起；员工卡首镜 shot#12 → 30.038s 起。"""
        spans = V.shot_time_spans(_shots(), REAL_TOTAL)
        self.assertAlmostEqual(spans[20]['start'], 53.961, places=3)
        self.assertAlmostEqual(spans[11]['start'], 30.038, places=3)
        self.assertAlmostEqual(spans[24]['start'], 65.923, places=3)
        self.assertEqual(len(spans), 60)

    def test_zero_total_falls_back_to_script_durations(self):
        spans = V.shot_time_spans([{'shot_id': 1, 'duration': 4}, {'shot_id': 2, 'duration': 6}], 0)
        self.assertEqual([(s['start'], s['end']) for s in spans], [(0.0, 4.0), (4.0, 10.0)])

    def test_missing_duration_defaults_to_five(self):
        spans = V.shot_time_spans([{'shot_id': 1}, {'shot_id': 2}], 10)
        self.assertEqual([s['duration'] for s in spans], [5.0, 5.0])

    def test_bad_total_and_empty_shots(self):
        self.assertEqual(V.shot_time_spans([], 10), [])
        self.assertEqual(V.shot_time_spans(None, None), [])
        spans = V.shot_time_spans([{'shot_id': 1, 'duration': 4}], 'abc')
        self.assertEqual((spans[0]['start'], spans[0]['end']), (0.0, 4.0))


class TestGeometry(unittest.TestCase):
    """② 几何：目标区域归一、素材裁切、等比缩放（不得拉伸变形）。"""

    def test_norm_box_fraction_and_pixel(self):
        self.assertEqual(V.norm_box((0.1, 0.2, 0.3, 0.4), 1000, 500), (100, 100, 300, 200))
        self.assertEqual(V.norm_box((10, 20, 30, 40), 1000, 500), (10, 20, 30, 40))

    def test_norm_box_clamps_inside_frame(self):
        x, y, w, h = V.norm_box((0.9, 0.9, 0.5, 0.5), 1000, 500)
        self.assertLessEqual(x + w, 1000)
        self.assertLessEqual(y + h, 500)
        self.assertGreaterEqual(w, 2)
        self.assertGreaterEqual(h, 2)

    def test_norm_box_invalid(self):
        for bad in [(), (1, 2, 3), (0, 0, 0, 0), ('a', 'b', 'c', 'd'), (0.1, 0.1, 0.2, -1)]:
            with self.subTest(bad=bad):
                self.assertEqual(V.norm_box(bad, 1000, 500), ())

    def test_crop_px(self):
        self.assertEqual(V.crop_px((0.02, 0.30, 0.46, 0.40), 1248, 1248), (574, 499, 25, 374))
        self.assertEqual(V.crop_px(None, 1248, 1248), ())
        self.assertEqual(V.crop_px((0, 0, 0, 0), 100, 100), ())

    def test_fit_box_preserves_aspect_and_centers(self):
        """1248x1248 资产图放进 288x457 的框 → 内切 288x288 且居中（不拉伸）。"""
        w, h, x, y = V.fit_box(1248, 1248, (20, 43, 288, 457))
        self.assertEqual((w, h), (288, 288))
        self.assertEqual(x, 20)
        self.assertEqual(y, 43 + (457 - 288) // 2)
        self.assertAlmostEqual(w / h, 1.0, places=2)

    def test_fit_box_aspect_for_card_crop(self):
        """员工卡正面裁切 574x499 放进 0.36x0.34 的框（960x544 → 346x185）：等比内切。"""
        box = V.norm_box((0.6, 0.6, 0.36, 0.34), 960, 544)
        self.assertEqual(box, (576, 326, 346, 185))
        w, h, x, y = V.fit_box(574, 499, box)
        self.assertAlmostEqual(w / h, 574 / 499, places=2)
        self.assertLessEqual(w, box[2])
        self.assertLessEqual(h, box[3])
        self.assertGreaterEqual(x, box[0])
        self.assertGreaterEqual(y, box[1])

    def test_fit_box_even_dims_for_x264(self):
        w, h, _x, _y = V.fit_box(1248, 1248, (0, 0, 301, 301))
        self.assertEqual(w % 2, 0)
        self.assertEqual(h % 2, 0)


class TestTextSource(unittest.TestCase):
    """③ 文本来源：资产图 / 小说原文（canon），**禁止**读 shot.surface_text。"""

    def test_registry_lines_and_canon_verification(self):
        r = V.resolve_prop_text('进境_整本小说', '楼层安全守则')
        self.assertEqual(len(r['lines']), 8)
        self.assertEqual(r['source'], 'registry+canon')
        self.assertIn('1. 本栋楼共四十七层，请勿前往四十八层及以上。', r['verified_lines'])
        self.assertIn('4. 夜间（23:00-05:00）请勿乘坐楼梯。', r['verified_lines'])
        self.assertGreaterEqual(len(r['verified_lines']), 3)

    def test_alias_maps_to_asset_dir_name(self):
        for alias in ('天合科技员工证', '员工证', '员工卡', '工牌'):
            with self.subTest(alias=alias):
                self.assertEqual(V.resolve_item_alias(alias), '天合科技员工卡')
                r = V.resolve_prop_text('进境_整本小说', alias)
                self.assertEqual(r['asset_name'], '天合科技员工卡')
                self.assertTrue(r['lines'])

    def test_unknown_item_refuses_to_guess(self):
        r = V.resolve_prop_text('进境_整本小说', '不存在的道具')
        self.assertEqual(r['lines'], [])
        self.assertEqual(r['source'], 'none')
        self.assertTrue(r['error'])

    def test_canon_missing_degrades_to_registry(self):
        r = V.resolve_prop_text('进境_整本小说', '楼层安全守则',
                                canon_path=os.path.join(tempfile.mkdtemp(), 'nope.json'))
        self.assertEqual(r['source'], 'registry')
        self.assertEqual(r['verified_lines'], [])
        self.assertEqual(len(r['lines']), 8)

    def test_plan_ignores_shot_surface_text(self):
        """剧本 surface_text 里放一份**错的**守则，叠加计划不得受影响（文本只认台账）。"""
        tmp = tempfile.mkdtemp(prefix='t6txt_')
        d = os.path.join(tmp, 'items', '进境_整本小说', '楼层安全守则')
        os.makedirs(d, exist_ok=True)
        io.open(os.path.join(d, 'base.png'), 'wb').write(b'x')
        shots = [{'shot_id': 1, 'duration': 5, 'items_in_shot': ['楼层安全守则'],
                  'surface_text': '1. 这是剧本里那套错的守则文本（4 条）'}]
        plan = V.plan_prop_overlays('进境_整本小说', shots,
                                    V.shot_time_spans(shots, 5), assets_dir=tmp)
        it = plan['items'][0]
        self.assertEqual(it['text_lines'], 8)
        self.assertEqual(it['text_source'], 'registry+canon')
        self.assertNotIn('错的守则', json.dumps(plan, ensure_ascii=False))


class TestPlan(unittest.TestCase):
    """④ 叠加计划：镜头 × 道具 × 时间轴 → 可核验条目。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='t6plan_')
        self.proj = 'P'
        for name in ('楼层安全守则', '天合科技员工卡'):
            d = os.path.join(self.tmp, 'items', self.proj, name)
            os.makedirs(d, exist_ok=True)
            io.open(os.path.join(d, 'base.png'), 'wb').write(b'x')

    def _shots(self):
        return [{'shot_id': 1, 'duration': 5, 'items_in_shot': ['楼层安全守则']},
                {'shot_id': 2, 'duration': 5, 'items_in_shot': ['天合科技员工证']},
                {'shot_id': 3, 'duration': 5, 'items_in_shot': ['不认识的器物']},
                {'shot_id': 4, 'duration': 5, 'items_in_shot': ['楼层安全守则', '天合科技员工卡']}]

    def test_items_carry_asset_box_and_window(self):
        shots = self._shots()
        spans = V.shot_time_spans(shots, 20)
        plan = V.plan_prop_overlays(self.proj, shots, spans, assets_dir=self.tmp)
        self.assertEqual(len(plan['items']), 4)      # #1、#2、#4(两件) → 4 条
        first = plan['items'][0]
        self.assertEqual(first['shot_id'], 1)
        self.assertEqual((first['start'], first['end']), (0.0, 5.0))
        self.assertTrue(first['asset'].endswith(os.path.join('楼层安全守则', 'base.png')))
        self.assertEqual(first['box'], V.DEFAULT_PROP_OVERLAYS['楼层安全守则']['box'])
        self.assertEqual(first['alpha'], 1.0)
        card = [i for i in plan['items'] if i['item'] == '天合科技员工卡']
        self.assertEqual(len(card), 2)
        self.assertEqual(card[0]['requested'], '天合科技员工证')   # 别名登记生效
        self.assertEqual(card[0]['crop_frac'], V.DEFAULT_PROP_OVERLAYS['天合科技员工卡']['crop_frac'])

    def test_missing_asset_is_skipped_with_reason(self):
        shots = [{'shot_id': 1, 'duration': 5, 'items_in_shot': ['楼层安全守则']}]
        os.remove(os.path.join(self.tmp, 'items', self.proj, '楼层安全守则', 'base.png'))
        plan = V.plan_prop_overlays(self.proj, shots, V.shot_time_spans(shots, 5), assets_dir=self.tmp)
        self.assertEqual(plan['items'], [])
        self.assertEqual(plan['skipped'][0]['reason'], '资产图不存在')

    def test_policy_switch_and_cap(self):
        shots = self._shots()
        spans = V.shot_time_spans(shots, 20)
        off = V.plan_prop_overlays(self.proj, shots, spans, assets_dir=self.tmp,
                                   policy={'enabled': False})
        self.assertEqual(off['items'], [])
        capped = V.plan_prop_overlays(self.proj, shots, spans, assets_dir=self.tmp,
                                      policy={'max_items': 2})
        self.assertEqual(len(capped['items']), 2)
        self.assertTrue(capped['skipped'])

    def test_short_span_filtered(self):
        shots = [{'shot_id': 1, 'duration': 0.2, 'items_in_shot': ['楼层安全守则']}]
        plan = V.plan_prop_overlays(self.proj, shots, V.shot_time_spans(shots, 0.2),
                                    assets_dir=self.tmp)
        self.assertEqual(plan['items'], [])

    def test_report_is_human_readable(self):
        shots = self._shots()
        plan = V.plan_prop_overlays(self.proj, shots, V.shot_time_spans(shots, 20),
                                    assets_dir=self.tmp)
        rep = V.overlay_report(plan)
        self.assertIn('叠加条目 4 条', rep)
        self.assertIn('shot#1', rep)


class TestFilterChain(unittest.TestCase):
    """⑤ 滤镜构造：RGBA/等比/时间段 + **Windows 路径不进滤镜**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='t6chain_')
        self.img = os.path.join(self.tmp, 'assets', 'items', 'P', '楼层安全守则', 'base.png')
        os.makedirs(os.path.dirname(self.img), exist_ok=True)
        from PIL import Image
        Image.new('RGBA', (1248, 1248), (255, 255, 255, 255)).save(self.img)

    def _item(self, **kw):
        it = {'shot_id': 7, 'asset': self.img, 'box': (0.02, 0.08, 0.30, 0.84),
              'crop_frac': None, 'alpha': 1.0, 'start': 53.96, 'end': 56.62}
        it.update(kw)
        return it

    def test_chain_has_rgba_scale_and_window(self):
        flt = V.build_overlay_chain([self._item()], (960, 544))
        self.assertEqual(flt['error'], '')
        fc = flt['filter_complex']
        self.assertIn('format=rgba', fc)
        self.assertIn('scale=288:288', fc)
        self.assertIn('enable=between(t\\,53.96\\,56.62)', fc)
        self.assertEqual(flt['vmap'], '[v0]')
        self.assertEqual(flt['extra_inputs'], [self.img])

    def test_no_filesystem_path_leaks_into_filter(self):
        """Windows 路径安全：滤镜串里不得出现任何路径（反斜杠/盘符/目录名）。"""
        flt = V.build_overlay_chain([self._item()], (960, 544))
        fc = flt['filter_complex']
        self.assertNotIn('\\\\', fc.replace('\\\\,', ''))       # 除转义逗号外无反斜杠
        self.assertNotIn(self.tmp, fc)
        self.assertNotIn('assets', fc)
        self.assertNotIn('.png', fc)

    def test_scale_is_aspect_preserving(self):
        """1248x1248 放进 288x457 的框 → 288x288（不是 288x457 的拉伸）。"""
        flt = V.build_overlay_chain([self._item()], (960, 544))
        self.assertIn('scale=288:288', flt['filter_complex'])
        self.assertNotIn('scale=288:457', flt['filter_complex'])

    def test_alpha_applied_only_when_below_one(self):
        opaque = V.build_overlay_chain([self._item()], (960, 544))['filter_complex']
        self.assertNotIn('colorchannelmixer', opaque)
        half = V.build_overlay_chain([self._item(alpha=0.5)], (960, 544))['filter_complex']
        self.assertIn('colorchannelmixer=aa=0.5', half)

    def test_two_items_chain_sequentially(self):
        flt = V.build_overlay_chain([self._item(), self._item(shot_id=8, start=1, end=2)],
                                    (960, 544))
        self.assertIn('[v0][ov1]overlay', flt['filter_complex'])
        self.assertEqual(flt['vmap'], '[v1]')

    def test_errors(self):
        self.assertIn('不存在', V.build_overlay_chain([self._item(asset='/nope/x.png')], (960, 544))['error'])
        self.assertIn('没有可叠加', V.build_overlay_chain([], (960, 544))['error'])
        self.assertIn('目标区域非法', V.build_overlay_chain([self._item(box=())], (960, 544))['error'])
        self.assertIn('时间段为空', V.build_overlay_chain([self._item(start=9, end=9)], (960, 544))['error'])

    def test_font_is_bare_filename_with_cwd(self):
        """drawtext 的 fontfile 必须是**裸文件名**（调用方 cwd 切到字体目录）。"""
        vf = V.build_text_overlay_vf(['1. 本栋楼共四十七层。'], start=1, end=2)
        if not vf:
            self.skipTest('本机无可用中文字体')
        self.assertIn("fontfile='", vf)
        self.assertNotIn(':', vf.split("fontfile='")[1].split("'")[0])
        self.assertNotIn('\\\\', vf.split("fontfile='")[1].split("'")[0])

    def test_text_escape_and_multiline(self):
        vf = V.build_text_overlay_vf(['A:B 100%', '第二行'], start=0, end=1)
        if not vf:
            self.skipTest('本机无可用中文字体')
        self.assertIn('A\\:B 100\\%', vf)
        self.assertIn('\\n', vf)     # 多行用字面量反斜杠+n（ffmpeg drawtext 约定）

    def test_no_font_returns_empty(self):
        with mock.patch.object(V._wm, 'resolve_font', return_value=''):
            self.assertEqual(V.build_text_overlay_vf(['x'], start=0, end=1), '')
            flt = V.build_overlay_chain([self._item()], (960, 544))
            self.assertEqual(flt['font'], '')


class TestFFmpegApply(unittest.TestCase):
    """⑥ 可调用接口 + 命令契约 + 音轨保持（本机无 ffmpeg 时自动 skip）。"""

    def setUp(self):
        if not _FFMPEG:
            self.skipTest('本机没有 ffmpeg')
        self.tmp = tempfile.mkdtemp(prefix='t6ff_')
        self.src = os.path.join(self.tmp, 'src.mp4')
        r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
                            '-i', 'color=c=navy:s=320x180:d=3', '-f', 'lavfi',
                            '-i', 'sine=frequency=440:duration=3',
                            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                            '-c:a', 'aac', '-shortest', self.src],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(self.src):
            self.skipTest('测试视频构造失败：%s' % (r.stderr or '')[:200])
        from PIL import Image
        self.img = os.path.join(self.tmp, 'item.png')
        Image.new('RGBA', (400, 400), (240, 240, 240, 255)).save(self.img)

    def _plan(self, **kw):
        it = {'shot_id': 1, 'item': '楼层安全守则', 'asset': self.img,
              'box': (0.05, 0.10, 0.40, 0.80), 'crop_frac': None, 'alpha': 1.0,
              'start': 0.2, 'end': 2.0}
        it.update(kw)
        return {'items': [it]}

    def test_apply_keeps_audio_and_uses_contract_flags(self):
        dst = os.path.join(self.tmp, 'out.mp4')
        res = V.apply_overlays(self.src, dst, self._plan(), video_size=(320, 180),
                               clip=(0, 2.5))
        self.assertTrue(res['ok'], res.get('error'))
        self.assertTrue(os.path.isfile(res['out_path']))
        # 图片贴片走 filter_complex：视频流 = 输入 0 的视频经 overlay 滤镜（[0:v] → [v0]）。
        # 这里**不能**写 -map 0:v:0：那会映射未叠加的原始画面、把叠加结果丢掉；
        # 纯文字路径 apply_text_overlays 才是字面量 -map 0:v:0（见下一个用例）。
        self.assertIn('[0:v]', res['filter_complex'])
        self.assertIn('-map [v0]', res['cmd'])
        self.assertIn('-map 0:a?', res['cmd'])
        self.assertIn('-c:a copy', res['cmd'])
        aud = res['audio']
        self.assertTrue(aud.get('has_audio'), '输出必须仍有音轨')
        self.assertEqual(aud['audio'][0]['codec'], 'aac')
        # 独立 ffprobe 复核（不只看模块自报）
        probe = V.probe_audio_streams(dst)
        self.assertTrue(probe['has_audio'])
        self.assertEqual(probe['audio'][0]['codec'], 'aac')

    def test_text_path_uses_literal_video_map(self):
        """纯 drawtext 路径：命令里就是字面量 -map 0:v:0（video_watermark 文字路径同口径）。"""
        dst = os.path.join(self.tmp, 'text.mp4')
        items = [{'lines': ['1. 本栋楼共四十七层。', '2. 电梯仅停靠偶数层。'],
                  'start': 0.2, 'end': 2.0, 'pos': 'top-left'}]
        res = V.apply_text_overlays(self.src, dst, items)
        self.assertTrue(res['ok'], res.get('error'))
        self.assertIn('-map 0:v:0', res['cmd'])
        self.assertIn('-map 0:a?', res['cmd'])
        self.assertIn('-c:a copy', res['cmd'])
        self.assertTrue(res['audio'].get('has_audio'))
        self.assertEqual(V.probe_audio_streams(dst)['audio'][0]['codec'], 'aac')

    def test_clip_shifts_time_window(self):
        dst = os.path.join(self.tmp, 'clip.mp4')
        res = V.apply_overlays(self.src, dst, self._plan(start=1.0, end=2.0),
                               video_size=(320, 180), clip=(0.5, 1.0))
        self.assertTrue(res['ok'], res.get('error'))
        self.assertIn('between(t\\,0.5\\,1.5)', res['filter_complex'])

    def test_clip_outside_window_is_clean_error(self):
        res = V.apply_overlays(self.src, os.path.join(self.tmp, 'x.mp4'),
                               self._plan(start=0.1, end=0.2), video_size=(320, 180),
                               clip=(2.0, 1.0))
        self.assertFalse(res['ok'])
        self.assertTrue(res['error'])

    def test_fail_open_contract(self):
        self.assertFalse(V.apply_overlays('/nope.mp4', '/tmp/x.mp4', self._plan())['ok'])
        self.assertIn('叠加计划为空', V.apply_overlays(self.src, '/tmp/x.mp4', {'items': []})['error'])
        bad = self._plan(asset='/nope/a.png')
        res = V.apply_overlays(self.src, os.path.join(self.tmp, 'y.mp4'), bad,
                               video_size=(320, 180))
        self.assertFalse(res['ok'])
        self.assertIn('素材不存在', res['error'])

    def test_sample_frame_extraction(self):
        dst = os.path.join(self.tmp, 'f.mp4')
        png = os.path.join(self.tmp, 'frame.png')
        self.assertTrue(V.overlay_sample_frame(self.src, png, 1.0)['ok'])
        self.assertTrue(os.path.isfile(png))


class TestPipelineWiring(unittest.TestCase):
    """⑦ pipeline.step_final 的接线（源码级 + 行为级）。"""

    def test_step_final_uses_shared_timeline_and_overlay(self):
        import inspect
        import pipeline
        src = inspect.getsource(pipeline.step_final)
        self.assertIn('shot_time_spans', src, '字幕/叠加必须共用同一时间轴实现')
        self.assertIn('plan_prop_overlays', src, 'step_final 必须调用叠加计划')
        self.assertIn('apply_overlays', src, 'step_final 必须真的执行叠加')
        self.assertIn('不影响成片', src, '叠加必须 fail-open（沿用字幕范式）')

    def test_subtitle_loop_consumes_spans(self):
        """字幕循环消费 _spans（改造后不再是内联公式的唯一来源）。"""
        import inspect
        import pipeline
        src = inspect.getsource(pipeline.step_final)
        self.assertIn('_spans[_i]["start"]', src)




class TestT18ShouzePlacement(unittest.TestCase):
    r"""⑧ t18：守则叠加位置/占比优化 —— 不再遮挡画面主体。

    定案依据（两条都是实测，不是拍脑袋）：
      · 位置：逐镜视觉定位，shot#21/25/28/52 的右上背景区在四镜里都是空的
      · 尺寸：5 档候选实测标定（见 .local/t18_cand.txt），26% 宽 8 条仍逐字可读
    另一半是「明确不做透视变换」—— 钉在常量与实现上，防后人误以为漏做。
    """

    #: 视觉复核给出的跨镜主体占位（近似像素，成片 960x544）。
    #: 守则贴片与它们的交集必须为空，否则就是「盖住主体」。
    SUBJECT_BOXES = {
        21: (345, 82, 605, 544),    # 手机屏幕（画面正中）
        25: (448, 356, 586, 544),   # 手持员工卡
        28: (340, 300, 560, 544),   # 前景手持牌
        52: (0, 350, 960, 544),     # 手持文件（下方对角）
    }

    KEY = '楼层安全守则'

    def _rect_of(self, box):
        """给定相对 box → 960x544 下的实际贴片矩形 (x1,y1,x2,y2)。"""
        px = V.norm_box(box, 960, 544)
        w, h, x, y = V.fit_box(1248, 1248, px)   # 资产图 1248x1248 RGBA
        return (x, y, x + w, y + h)

    def _shouze_rect(self):
        return self._rect_of(V.DEFAULT_PROP_OVERLAYS[self.KEY]['box'])

    @staticmethod
    def _overlap(a, b):
        ox = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        oy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        return ox * oy

    def test_overlay_does_not_cover_any_subject(self):
        """硬底线：守则贴片与任何一个守则镜的主体都不重叠。"""
        r = self._shouze_rect()
        for shot, sb in sorted(self.SUBJECT_BOXES.items()):
            self.assertEqual(self._overlap(r, sb), 0,
                             '守则贴片 %s 与 shot#%d 主体 %s 重叠' % (r, shot, sb))

    def test_old_placement_did_cover_subject(self):
        """反证：t6 的旧位置确实压住主体 —— 证明这次优化不是无的放矢。"""
        r = self._rect_of(V.PROP_OVERLAY_HISTORY[self.KEY][0]['box'])
        covered = [shot for shot, sb in sorted(self.SUBJECT_BOXES.items())
                   if self._overlap(r, sb) > 0]
        self.assertTrue(covered, '旧位置本应压住主体，否则这条反证没意义')

    def test_kept_readable_size_budget(self):
        """尺寸落在实测可读档内：面积 <= 15%，且不压到 19% 那档下限。"""
        x1, y1, x2, y2 = self._shouze_rect()
        area = (x2 - x1) * (y2 - y1) / (960 * 544) * 100.0
        width = (x2 - x1) / 960 * 100.0
        self.assertLessEqual(area, 15.0, '面积占比 %.1f 过大' % area)
        self.assertGreaterEqual(width, 20.0,
                                '占宽 %.1f 低于实测可读下限（19 档需眯眼）' % width)

    def test_stays_inside_frame(self):
        """贴片完整落在画幅内（不越界裁字）。"""
        x1, y1, x2, y2 = self._shouze_rect()
        self.assertGreaterEqual(x1, 0)
        self.assertGreaterEqual(y1, 0)
        self.assertLessEqual(x2, 960)
        self.assertLessEqual(y2, 544)

    def test_aspect_still_preserved(self):
        """t6 验收项回归：等比不变形（1248x1248 源 → 仍 1:1）。"""
        x1, y1, x2, y2 = self._shouze_rect()
        self.assertAlmostEqual((x2 - x1) / (y2 - y1), 1.0, places=2)

    def test_history_records_both_versions(self):
        """历史对照：t6 与 t18 两版都在，且当前值 == 注册表里的值。"""
        hist = V.PROP_OVERLAY_HISTORY[self.KEY]
        self.assertEqual([h['version'] for h in hist], ['t6', 't18'])
        self.assertEqual(hist[-1]['box'], V.DEFAULT_PROP_OVERLAYS[self.KEY]['box'])

    def test_no_perspective_transform_is_declared(self):
        """明确不做透视变换：常量写明理由 + 实现里不得出现透视类滤镜。"""
        note = V.NO_PERSPECTIVE_TRANSFORM_NOTE
        self.assertIn('透视', note)
        self.assertIn('PIL', note)
        tmp = tempfile.mkdtemp(prefix='t18flt_')
        p = os.path.join(tmp, 'a.png')
        io.open(p, 'wb').write(b'x')
        flt = V.build_overlay_chain(
            [{'asset': p, 'box': (0.72, 0.04, 0.26, 0.58), 'crop_frac': None,
              'alpha': 1.0, 'start': 0.0, 'end': 1.0}], (960, 544))
        self.assertEqual(flt.get('error'), '')
        for banned in ('perspective', 'rotate', 'warp', 'geq'):
            self.assertNotIn(banned, flt.get('filter_complex', ''),
                             '不得引入透视类滤镜（t18 裁决不做）')
        shutil.rmtree(tmp, ignore_errors=True)

    def test_box_is_data_driven_not_hardcoded(self):
        """位置/占比可配置：改注册表即生效（不是写死在算法里）。"""
        tmp = tempfile.mkdtemp(prefix='t18cfg_')
        d = os.path.join(tmp, 'items', '进境_整本小说', self.KEY)
        os.makedirs(d, exist_ok=True)
        io.open(os.path.join(d, 'base.png'), 'wb').write(b'x')
        shots = [{'shot_id': 21, 'duration': 5, 'items_in_shot': [self.KEY]}]
        spans = V.shot_time_spans(shots, 5)
        orig = V.DEFAULT_PROP_OVERLAYS[self.KEY]
        try:
            V.DEFAULT_PROP_OVERLAYS[self.KEY] = dict(orig, box=(0.10, 0.10, 0.20, 0.30))
            plan = V.plan_prop_overlays('进境_整本小说', shots, spans, assets_dir=tmp)
            self.assertEqual(plan['items'][0]['box'], (0.10, 0.10, 0.20, 0.30))
        finally:
            V.DEFAULT_PROP_OVERLAYS[self.KEY] = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestT18SealFeasibility(unittest.TestCase):
    r"""⑨ t18：印章的可行性判定 —— 缺资产图 + 缺权威文本 → 维持 fail-open 不猜。

    判定（2026-10-12 实测）：
      · 道具资产目录只有 3 个（守则/员工卡/水瓶）→ **没有印章资产图**
      · 「印章/红章/公章」在剧本、canon_data.json、小说原文里均 0 次
      · 用户反馈的「印章」更可能是守则纸上的红色圆珠笔手写批注
    结论：两者不齐备 → 不登记、不猜，维持 fail-open。
    """

    SEAL_NAMES = ('印章', '红章', '公章')

    def test_seal_has_no_registry_entry(self):
        """印章不在台账里 → 明确不登记（不猜内容）。"""
        for key in V.PROP_TEXT_REGISTRY:
            for nm in self.SEAL_NAMES:
                self.assertNotIn(nm, str(key))

    def test_seal_not_in_default_overlays(self):
        """印章也没有默认叠加配置 → 不会被计划选中。"""
        for nm in self.SEAL_NAMES:
            self.assertNotIn(nm, V.DEFAULT_PROP_OVERLAYS)

    def test_unknown_item_fails_open_with_error(self):
        """未登记道具：返回 error，不抛异常、不编造内容 —— 与 t6 原则一致。"""
        for nm in self.SEAL_NAMES:
            out = V.resolve_prop_text('进境_整本小说', nm)
            self.assertEqual(out['lines'], [])
            self.assertTrue(out['error'], '%s 应给出缺失说明而非静默成功' % nm)
            self.assertEqual(out['source'], 'none')

    def test_plan_skips_seal_item(self):
        """镜头即使声明了印章，计划里也不会产出叠加条目（fail-open）。"""
        tmp = tempfile.mkdtemp(prefix='t18seal_')
        d = os.path.join(tmp, 'items', '进境_整本小说', '印章')
        os.makedirs(d, exist_ok=True)
        io.open(os.path.join(d, 'base.png'), 'wb').write(b'x')
        shots = [{'shot_id': 1, 'duration': 5, 'items_in_shot': ['印章']}]
        spans = V.shot_time_spans(shots, 5)
        plan = V.plan_prop_overlays('进境_整本小说', shots, spans, assets_dir=tmp)
        self.assertEqual(plan['items'], [])
        shutil.rmtree(tmp, ignore_errors=True)

    def test_red_annotation_is_the_likely_real_target(self):
        """把「红色圆珠笔手写批注」钉住：它有权威原文出处，是后续纳入的候选。"""
        #: 小说原文（进境 20261010_184008_8d5152.txt）:112-114 / :261
        self.assertIn('第五条和第三条矛盾', V.CANONICAL_RED_ANNOTATION)
        self.assertIn('红色按钮不是用来求救的', V.CANONICAL_RED_ANNOTATION)
        # 判定常量本身也要自洽：结论 + 根因 + 缺口 + 所需提供方都必须写明
        feas = V.SEAL_FEASIBILITY
        self.assertEqual(feas['verdict'], 'not_feasible_yet')
        self.assertTrue(feas['missing'] and feas['needed_from'])
        self.assertIn('幻觉', feas['root_cause'])
        self.assertIn('生成侧', feas['note'])
        # 原文证据：不是"没提到印章"，而是"写死了没有印章"
        self.assertIn('无落款', feas['novel_evidence'])
        self.assertIn('背面空白', feas['novel_evidence'])
        self.assertEqual(feas['recommended_action'], '生成侧抑制（让 H3 别画），而非叠加侧补一个')


if __name__ == '__main__':
    unittest.main()
