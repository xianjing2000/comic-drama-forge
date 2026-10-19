# -*- coding: utf-8 -*-
"""voice_bank 参考音色：读取契约 + 缺失降级 回归测试（2026-10-12，音频链路 T4）

链路口径（与代码逐条对应）
--------------------------
1. tts_pre 只做「每个角色一段参考音色」：pipeline.step_tts_pre（pipeline.py:1158）
   → video_helpers._ensure_voice_bank_refs（video_helpers.py:487，逐角色合成）
   → tts_client.save_voice_bank_ref（tts_client.py:843）
   → <DUB_DIR>/<项目>/voice_bank/<角色安全名>/ref.wav + ref.json。
2. 该参考音色被 H3 捡取为公共参考音频：asset_refs._h3_common_ref_audios
   （asset_refs.py:732，缺音色的角色 **跳过**、绝不误挂别人的声音）
   → comfyui_client 上传并写进 global.refAudios（comfyui_client.py:5316-5339）
   → audioMode=generate（comfyui_client.py:5460-5475）＝ 音色被锁定、对白由 H3 自生成。
3. 成片音轨因此来自 H3 原生音轨（config.H3_EMIT_AUDIO=True，config.py:801），
   mix 不在 STEP_SEQUENCE（pipeline.py:212-213）、enable_mix 默认 False（pipeline.py:270）。

本测试**不联网、不依赖 GPU / ComfyUI**：所有 TTS 调用都用本地假客户端替换，
仅在临时目录里读写文件。覆盖的降级路径：
  · 参考音色缺失 → 自动补齐；补齐失败（ok=False / 抛异常）→ 只告警、不抛、不落半成品；
  · step_tts_pre 整步 fail-open（异常也返回 ok=True + skipped=True，绝不拖垮整集）；
  · enable_tts_pre / enable_tts 两个开关的真实语义（前者控整步，后者控合成）。

运行：python -m unittest discover -s tests
"""
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)
# 与其它测试一致：数据根落到临时目录，避免读写真实产物（桌面版必设）
os.environ.setdefault('MJSCXT_DATA_DIR',
                      tempfile.mkdtemp(prefix='vbtest_'))

import asset_refs        # noqa: E402
import autopilot         # noqa: E402
import pipeline          # noqa: E402
import tts_client        # noqa: E402
import video_helpers     # noqa: E402

CHAR_A = '测试角色甲'
CHAR_B = '测试角色乙'


def _write_wav(path, seconds=0.2, freq=440.0, rate=24000):
    """写一段真实可探测的 16bit 单声道 WAV（不用 ffmpeg/numpy）。"""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    n = max(1, int(rate * seconds))
    frames = b''.join(struct.pack('<h', int(12000 * math.sin(2 * math.pi * freq * i / rate)))
                      for i in range(n))
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


class _FakeTTSClient:
    """替身：不连 ComfyUI。behavior = ok / fail / raise。"""
    calls = []
    behavior = 'ok'

    def __init__(self, out_root=None, params=None):
        self.out_root = out_root

    def synthesize_one(self, text, voice, out_path, timeout=60):
        type(self).calls.append({'text': text, 'voice': dict(voice or {}),
                                 'out_path': out_path})
        if type(self).behavior == 'raise':
            raise RuntimeError('ComfyUI 不可达（模拟）')
        if type(self).behavior == 'fail':
            return {'ok': False, 'error': 'ComfyUI 不可达（模拟）'}
        _write_wav(out_path, 0.2)
        return {'ok': True, 'path': out_path, 'duration': 0.2}


class _FakeTTSModule:
    """给 step_tts_pre 的 A.tts_client 替身。"""

    def __init__(self, rows):
        self._rows = rows

    def list_voice_bank(self, dub_dir):
        return list(self._rows)


class _StubApp:
    """pipeline._A() 的替身：只提供 step_tts_pre 用到的 5 个入口。"""

    def __init__(self, rows=None, env_ok=True, raise_in_ensure=False):
        self.tts_client = _FakeTTSModule(rows or [])
        self._rows = rows or []
        self._env_ok = env_ok
        self._raise = raise_in_ensure
        self.ensure_calls = []
        self.dub_dir = tempfile.mkdtemp(prefix='vbdub_')

    def _build_asset_index(self, characters, project_name, kind):
        return {str(c.get('name')): c for c in (characters or []) if c.get('name')}

    def tts_env_check(self):
        return {'available': self._env_ok,
                'reasons': [] if self._env_ok else ['ComfyUI 未启动（模拟）']}

    def _ensure_voice_bank_refs(self, common, project_name, all_characters=None):
        self.ensure_calls.append({'common': list(common or []),
                                  'all_characters': list((all_characters or {}).keys())})
        if self._raise:
            raise RuntimeError('磁盘写满（模拟）')

    def _dub_project_dir(self, project_name):
        return self.dub_dir


class _VBTmpBase(unittest.TestCase):
    """每个用例一个临时项目 dub 目录，并把 _dub_project_dir 指过去（完全独立于数据根）。"""

    def setUp(self):
        self.dub = tempfile.mkdtemp(prefix='vbproj_')
        self.addCleanup(shutil.rmtree, self.dub, True)
        for mod in (video_helpers, asset_refs):
            p = mock.patch.object(mod, '_dub_project_dir', lambda _pn, d=self.dub: d)
            p.start()
            self.addCleanup(p.stop)

    def _ref_path(self, character, ext='.wav'):
        return os.path.join(self.dub, 'voice_bank', character, 'ref' + ext)


# ===================== 1. voice_bank 存取契约 =====================

class TestVoiceBankStore(_VBTmpBase):

    def test_find_missing_character_returns_empty(self):
        self.assertEqual(tts_client.find_voice_bank_ref(self.dub, CHAR_A), ('', ''))
        # 目录存在但没有任何音频 → 仍然是空（不误判为已绑定）
        os.makedirs(os.path.join(self.dub, 'voice_bank', CHAR_A), exist_ok=True)
        self.assertEqual(tts_client.find_voice_bank_ref(self.dub, CHAR_A), ('', ''))

    def test_save_then_find_and_list_roundtrip(self):
        src = _write_wav(os.path.join(self.dub, '_src.wav'), 0.5)
        dst = tts_client.save_voice_bank_ref(self.dub, CHAR_A, src,
                                             ref_text='我是测试角色甲。',
                                             original_filename='upload.wav')
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(os.path.basename(dst), 'ref.wav')
        ref, ref_text = tts_client.find_voice_bank_ref(self.dub, CHAR_A)
        self.assertEqual(os.path.normcase(ref), os.path.normcase(dst))
        self.assertEqual(ref_text, '我是测试角色甲。')

        rows = tts_client.list_voice_bank(self.dub)
        self.assertEqual([r['character'] for r in rows], [CHAR_A])
        row = rows[0]
        self.assertTrue(row['exists'])
        self.assertEqual(row['ref_text'], '我是测试角色甲。')
        self.assertEqual(row['original_filename'], 'upload.wav')
        self.assertIn('duration_sec', row)
        meta = json.loads(open(os.path.join(os.path.dirname(dst), 'ref.json'),
                               encoding='utf-8').read())
        for k in ('character', 'ref_text', 'original_filename',
                  'duration_sec', 'updated_at'):
            self.assertIn(k, meta, 'ref.json 字段契约漂移：缺 %s' % k)

    def test_broken_ref_json_falls_back_to_empty_text(self):
        """ref.json 损坏只丢元数据，音频本体照样可用（tts_client.py:826-831）。"""
        src = _write_wav(os.path.join(self.dub, '_src.wav'), 0.3)
        dst = tts_client.save_voice_bank_ref(self.dub, CHAR_A, src, ref_text='x')
        with open(os.path.join(os.path.dirname(dst), 'ref.json'), 'w',
                  encoding='utf-8') as f:
            f.write('{ this is not json')
        ref, ref_text = tts_client.find_voice_bank_ref(self.dub, CHAR_A)
        self.assertEqual(os.path.normcase(ref), os.path.normcase(dst))
        self.assertEqual(ref_text, '')

    def test_find_ignores_non_audio_and_empty_files(self):
        d = os.path.join(self.dub, 'voice_bank', CHAR_A)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, 'ref.txt'), 'w').close()
        open(os.path.join(d, 'ref.wav'), 'wb').close()      # 0 字节：不算可用
        self.assertEqual(tts_client.find_voice_bank_ref(self.dub, CHAR_A), ('', ''))

    def test_save_replaces_other_extensions(self):
        """换格式时不留两份（否则 find 会取到旧的那份）。"""
        wav = _write_wav(os.path.join(self.dub, '_a.wav'), 0.2)
        tts_client.save_voice_bank_ref(self.dub, CHAR_A, wav)
        mp3 = os.path.join(self.dub, '_b.mp3')
        with open(mp3, 'wb') as f:
            f.write(b'\xff\xfb\x90\x00' + b'\x00' * 64)
        dst = tts_client.save_voice_bank_ref(self.dub, CHAR_A, mp3)
        names = sorted(os.listdir(os.path.dirname(dst)))
        self.assertEqual([n for n in names if os.path.splitext(n)[1].lower()
                          in tts_client.VOICE_BANK_EXTS], ['ref.mp3'])

    def test_voice_bank_dir_is_a_child_of_dub(self):
        self.assertEqual(os.path.dirname(os.path.dirname(
            tts_client.voice_bank_dir(self.dub, CHAR_A))), os.path.abspath(self.dub))


# ===================== 2. tts_pre 自动补齐 + 失败降级 =====================

class TestEnsureVoiceBankRefs(_VBTmpBase):

    def setUp(self):
        super().setUp()
        _FakeTTSClient.calls = []
        _FakeTTSClient.behavior = 'ok'
        p = mock.patch.object(tts_client, 'QwenTTSClient', _FakeTTSClient)
        p.start()
        self.addCleanup(p.stop)

    def test_auto_fills_missing_character_only(self):
        """已有参考音色的角色不动；缺的角色自动补一段（video_helpers.py:554-558）。"""
        kept = _write_wav(self._ref_path(CHAR_A), 0.25, freq=300)
        before = open(kept, 'rb').read()
        video_helpers._ensure_voice_bank_refs(
            [], 'proj', all_characters={CHAR_A: {'identity': '甲'}, CHAR_B: {'identity': '乙'}})
        self.assertEqual(len(_FakeTTSClient.calls), 1, '已绑定角色被重复合成')
        self.assertIn(CHAR_B, _FakeTTSClient.calls[0]['text'],
                      '补齐的参考文本未带角色名（人设派生失效）')
        self.assertEqual(open(kept, 'rb').read(), before, '已有参考音被改写')
        ref_b, text_b = tts_client.find_voice_bank_ref(self.dub, CHAR_B)
        self.assertTrue(os.path.isfile(ref_b), '缺失角色未被补齐')
        self.assertIn(CHAR_B, text_b)
        self.assertIn(CHAR_B, open(os.path.join(os.path.dirname(ref_b), 'ref.json'),
                                   encoding='utf-8').read())

    def test_tts_failure_is_silent_and_leaves_no_ref(self):
        _FakeTTSClient.behavior = 'fail'
        with self.assertLogs('video_helpers', level='WARNING') as cm:
            video_helpers._ensure_voice_bank_refs(
                [], 'proj', all_characters={CHAR_B: {}})
        self.assertEqual(len(_FakeTTSClient.calls), 1)
        self.assertFalse(os.path.exists(self._ref_path(CHAR_B)), '失败却落了产物')
        self.assertTrue(any(CHAR_B in line for line in cm.output),
                        '失败未留下可追溯的告警：%s' % cm.output)

    def test_tts_exception_is_swallowed(self):
        _FakeTTSClient.behavior = 'raise'
        with self.assertLogs('video_helpers', level='WARNING') as cm:
            video_helpers._ensure_voice_bank_refs(
                [], 'proj', all_characters={CHAR_B: {}})
        self.assertFalse(os.path.exists(self._ref_path(CHAR_B)))
        self.assertTrue(cm.output, '异常被吞且无日志')

    def test_no_characters_is_noop(self):
        video_helpers._ensure_voice_bank_refs([], 'proj', all_characters=None)
        video_helpers._ensure_voice_bank_refs([], 'proj', all_characters={})
        self.assertEqual(_FakeTTSClient.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.dub, 'voice_bank')))

    def test_existing_ref_is_idempotent(self):
        """第二次调用不再合成（幂等：重跑 tts_pre 不重复烧 GPU）。"""
        video_helpers._ensure_voice_bank_refs([], 'proj', all_characters={CHAR_B: {}})
        self.assertEqual(len(_FakeTTSClient.calls), 1)
        video_helpers._ensure_voice_bank_refs([], 'proj', all_characters={CHAR_B: {}})
        self.assertEqual(len(_FakeTTSClient.calls), 1)


# ===================== 3. step_tts_pre 开关与 fail-open =====================

class TestStepTtsPre(_VBTmpBase):

    def _ctx(self, script=None, **cfg_over):
        cfg = dict(pipeline.DEFAULT_CONFIG)
        cfg.update(cfg_over)
        return {'config': cfg, 'episode_no': 1, 'project_name': 'proj',
                'script': (script if script is not None else
                           {'characters': [{'name': CHAR_A}],
                            'shots': [{'shot_id': 'shot_01'}]})}

    def _run(self, stub, ctx):
        # D-1（2026-10-16）：`pipeline._A()` 服务定位器已删除，step_tts_pre 改为函数内
        # 显式 import → 直接 patch 各**来源模块**的名字（原 _StubApp 是 _A() 的替身）。
        with mock.patch.object(asset_refs, '_build_asset_index', stub._build_asset_index), \
             mock.patch.object(video_helpers, '_ensure_voice_bank_refs',
                               stub._ensure_voice_bank_refs), \
             mock.patch('routes.tts._dub_project_dir', stub._dub_project_dir), \
             mock.patch.object(tts_client, 'check_environment', stub.tts_env_check), \
             mock.patch.object(tts_client, 'list_voice_bank',
                               stub.tts_client.list_voice_bank):
            return pipeline.step_tts_pre(ctx)

    def test_off_by_enable_tts_pre(self):
        r = self._run(_StubApp(), self._ctx(enable_tts_pre=False))
        self.assertTrue(r['ok'] and r['skipped'])
        self.assertIn('enable_tts_pre', r['detail']['note'])

    def test_off_by_enable_tts(self):
        """enable_tts=False 时整步跳过（不合成参考音色，pipeline.py:1185-1187）。"""
        r = self._run(_StubApp(), self._ctx(enable_tts=False))
        self.assertTrue(r['ok'] and r['skipped'])
        self.assertIn('enable_tts', r['detail']['note'])

    def test_empty_script_skipped(self):
        r = self._run(_StubApp(), self._ctx(script={'characters': [], 'shots': []}))
        self.assertTrue(r['ok'] and r['skipped'])

    def test_env_unavailable_is_skipped_with_reason(self):
        r = self._run(_StubApp(env_ok=False), self._ctx())
        self.assertTrue(r['ok'] and r['skipped'])
        self.assertFalse(r['detail'].get('env_available', True))
        self.assertIn('环境不可用', r['detail']['note'])

    def test_exception_is_fail_open(self):
        """纯增强环节：任何异常都只告警 + skipped，绝不让整集失败。"""
        stub = _StubApp(raise_in_ensure=True)
        r = self._run(stub, self._ctx())
        self.assertTrue(r['ok'], 'fail-open 被破坏：整步把异常抛给了调用方')
        self.assertTrue(r['skipped'])
        self.assertIn('不阻断', r['detail']['note'])
        self.assertEqual(len(stub.ensure_calls), 1)

    def test_success_reports_characters(self):
        rows = [{'character': CHAR_A}, {'character': CHAR_B}, {'character': ''}]
        r = self._run(_StubApp(rows=rows), self._ctx())
        self.assertTrue(r['ok'] and not r['skipped'])
        self.assertEqual(r['detail']['characters'], [CHAR_A, CHAR_B])
        self.assertIn('2 个角色参考音色就绪', r['detail']['note'])


# ===================== 4. 步骤接线与开关语义 =====================

class TestPipelineWiring(unittest.TestCase):

    def test_tts_pre_in_sequence_tts_and_mix_are_not(self):
        self.assertIn('tts_pre', pipeline.STEP_SEQUENCE)
        self.assertNotIn('tts', pipeline.STEP_SEQUENCE)
        self.assertNotIn('mix', pipeline.STEP_SEQUENCE)
        self.assertEqual(pipeline.STEP_RUNNERS['tts_pre'], pipeline.step_tts_pre)
        # 旧步骤函数保留（手工端点/回滚用），但不在序列里
        self.assertIn('mix', pipeline.STEP_RUNNERS)

    def test_default_config_switches(self):
        self.assertIs(pipeline.DEFAULT_CONFIG['enable_tts'], True)
        self.assertIs(pipeline.DEFAULT_CONFIG['enable_tts_pre'], True)
        self.assertIs(pipeline.DEFAULT_CONFIG['enable_mix'], False)

    def test_bool_switches_parse_strings(self):
        """前端传字符串时 bool 开关要按真值解析，不能被 bool("false") 当成开启。

        2026-10-12 修复前实测：normalize_config({'enable_tts_pre': 'false'}) 仍是 True
        —— 「关掉参考音色」的设置被静默忽略，照跑一遍 TTS。
        """
        off = ('false', '0', 'off', 'no', '', 'FALSE', ' 0 ')
        on = ('true', '1', 'on', 'yes')
        for raw in off:
            c = pipeline.normalize_config({'enable_tts_pre': raw, 'enable_tts': raw,
                                           'enable_mix': raw, 'enable_upscale': raw})
            for k in ('enable_tts_pre', 'enable_tts', 'enable_mix', 'enable_upscale'):
                self.assertIs(c[k], False, '%s=%r 应解析为 False，实际 %r'
                              % (k, raw, c[k]))
        for raw in on:
            c = pipeline.normalize_config({'enable_tts_pre': raw, 'enable_tts': raw})
            self.assertIs(c['enable_tts_pre'], True, 'enable_tts_pre=%r' % raw)
            self.assertIs(c['enable_tts'], True, 'enable_tts=%r' % raw)
        # 真实布尔/整数同样收敛正确
        c = pipeline.normalize_config({'enable_tts_pre': 0, 'enable_tts': False})
        self.assertIs(c['enable_tts_pre'], False)
        self.assertIs(c['enable_tts'], False)

    def test_step_enabled_semantics(self):
        cfg = dict(pipeline.DEFAULT_CONFIG)
        ctx = {'config': cfg, 'episode_no': 1}
        # tts_pre 只受 enable_tts_pre 控制（关掉 TTS 不该连带关掉参考音色步）
        cfg['enable_tts_pre'], cfg['enable_tts'] = True, False
        self.assertTrue(pipeline.step_enabled('tts_pre', ctx))
        cfg['enable_tts_pre'] = False
        self.assertFalse(pipeline.step_enabled('tts_pre', ctx))
        # 旧步骤各自受自己的开关控制
        cfg['enable_tts'], cfg['enable_mix'] = True, False
        self.assertTrue(pipeline.step_enabled('tts', ctx))
        self.assertFalse(pipeline.step_enabled('mix', ctx))
        cfg['enable_mix'] = True
        self.assertTrue(pipeline.step_enabled('mix', ctx))


# ===================== 5. H3 侧如何消费参考音色 =====================

class TestH3Consumption(_VBTmpBase):

    def setUp(self):
        super().setUp()
        self.common = [{'kind': 'character', 'name': CHAR_A},
                       {'kind': 'character', 'name': CHAR_B}]

    def test_only_bound_characters_are_attached(self):
        _write_wav(self._ref_path(CHAR_A), 0.2)
        pairs = asset_refs._h3_common_ref_audios(self.common, 'proj',
                                                 all_names=[CHAR_A, CHAR_B])
        self.assertEqual([n for n, _p in pairs], [CHAR_A])
        self.assertEqual(os.path.normcase(pairs[0][1]),
                         os.path.normcase(self._ref_path(CHAR_A)))

    def test_no_binding_returns_empty(self):
        self.assertEqual(asset_refs._h3_common_ref_audios(self.common, 'proj'), [])

    def test_audio_defs_numbering_follows_attached_order(self):
        """<Audio N> 必须与 global.refAudios 同序同数（asset_refs.py:804-807）。"""
        _write_wav(self._ref_path(CHAR_A), 0.2)
        _write_wav(self._ref_path(CHAR_B), 0.2)
        defs = asset_refs._h3_audio_defs_for(self.common, 'proj',
                                             characters=[{'name': CHAR_A, 'voice_style': ''},
                                                         {'name': CHAR_B, 'voice_style': ''}])
        self.assertEqual([(d['name'], d['label'], d['speaker']) for d in defs],
                         [(CHAR_A, '<Audio 1>', 'S1'), (CHAR_B, '<Audio 2>', 'S2')])


# ===================== 6. 计划接口覆盖现状（characterization） =====================

class TestPlanCoverage(unittest.TestCase):

    def test_plan_defaults_covers_the_audio_switches(self):
        """现状（2026-10-12 T11 起）：enable_tts / enable_tts_pre / enable_mix 三键都在
        PLAN_DEFAULTS 里 —— 计划接口写进去能生效、也能读回。

        ⚠️ 本断言在 T4 时是「enable_tts 不在 PLAN_DEFAULTS」（有意的现状固化，当时注释
        已明确写「若将来把 enable_tts 补进 PLAN_DEFAULTS，请同步改本断言」）。T11 修掉了
        这条假通道，故同步更新为「接受的键＝PLAN_DEFAULTS 的键；不在集合里的键由
        split_plan_patch 显式回报」。三通道一致性的完整回归见 tests/test_config_channels.py。
        """
        self.assertIn('enable_tts_pre', autopilot.PLAN_DEFAULTS)
        self.assertIn('enable_tts', autopilot.PLAN_DEFAULTS)
        self.assertIn('enable_mix', autopilot.PLAN_DEFAULTS)
        for _k in ('enable_tts', 'enable_tts_pre', 'enable_mix'):
            self.assertEqual(autopilot.split_plan_patch({_k: False})['rejected'], {},
                             '%s 仍写不进计划' % _k)

    def test_unlisted_key_is_reported_not_dropped(self):
        """写了但不生效的键必须显式回报（T11 之前是静默丢弃 + 200 OK）。"""
        split = autopilot.split_plan_patch({'enable_keyframe': True})
        self.assertEqual(split['accepted'], {})
        self.assertEqual(sorted(split['rejected']), ['enable_keyframe'])
        self.assertTrue(str(split['reasons']['enable_keyframe']).strip())


if __name__ == '__main__':
    unittest.main()
