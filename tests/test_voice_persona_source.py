# -*- coding: utf-8 -*-
"""角色音色「人设来源」回归（t8 · 2026-10-13，audio-dev）。

被修的真实缺陷
--------------
用户实测「每个角色的声音音色都一样」。根因：参考音色的人设（voice_style）**只从第 1 集
剧本读**（video_helpers 硬编码 第1集.json），而「要生成哪些角色」来自角色资产全集 ——
两者不重合时多出来的角色拿不到 voice_style → mode=preset + speaker=Ryan + seed=0
+ instruct 空 → 这些角色是同一个嗓子（实测 9 个角色里 5 个如此）。

本文件钉住四件事：
  1. 取值优先序 voice_map 显式 > 首次出场集 > 台词最多集 > 兜底拼装；
  2. **跨集取值稳定**（与文件创建顺序无关、与调用次数无关、清缓存后一致）；
  3. 无人设时 speaker 按名字稳定散开（不再人人 Ryan）；
  4. 用户上传的参考音频**永不被自动生成覆盖**（force=True 也不覆盖）。

不联网、不调 TTS：需要 TTS 的用例用替身客户端。
"""
import io
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
os.environ.setdefault('MJSCXT_DATA_DIR', tempfile.mkdtemp(prefix='t8vbtest_'))

import tts_client          # noqa: E402
import video_helpers       # noqa: E402
import voice_bank          # noqa: E402


def _write_wav(path, seconds=0.2, freq=440.0, rate=24000):
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


def _char(name, voice_style='', **kw):
    rec = {'name': name, 'voice_style': voice_style}
    rec.update(kw)
    return rec


class _Base(unittest.TestCase):
    """每个用例一个独立：剧本根 + dub 根（都指向临时目录）。"""

    def setUp(self):
        self.scripts = tempfile.mkdtemp(prefix='t8scripts_')
        self.dub = tempfile.mkdtemp(prefix='t8dub_')
        self.addCleanup(shutil.rmtree, self.scripts, True)
        self.addCleanup(shutil.rmtree, self.dub, True)
        for target, attr, value in ((voice_bank, 'SCRIPT_DIR', self.scripts),
                                    (video_helpers, '_dub_project_dir',
                                     (lambda _pn, d=self.dub: d))):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        voice_bank.clear_cache()

    def script(self, project, ep, chars, shots=None):
        """写一集剧本：<scripts>/<project>/第N集.json。"""
        d = os.path.join(self.scripts, project)
        os.makedirs(d, exist_ok=True)
        payload = {'episode_no': ep, 'characters': list(chars),
                   'shots': list(shots or [])}
        p = os.path.join(d, '第%d集.json' % ep)
        io.open(p, 'w', encoding='utf-8').write(
            json.dumps(payload, ensure_ascii=False))
        return p

    def shots_with_lines(self, name, lines, shots=0):
        """构造 lines 句台词 + shots 个出场镜（importance 统计用）。"""
        out = []
        for i in range(max(lines, shots)):
            out.append({'shot_id': i + 1,
                        'characters_in_shot': [name],
                        'dialogue': ([{'speaker': name, 'text': '台词'}]
                                     if i < lines else [])})
        return out


# ===================== 1. 取值优先序 =====================

class TestVoiceStylePriority(_Base):

    def test_first_appearance_episode_wins(self):
        self.script('p1', 1, [_char('甲', '少年音A')])
        self.script('p2', 2, [_char('甲', '青年音B')])
        self.script('p3', 3, [_char('甲', '中年音C')])
        vs, src, ep = voice_bank.resolve_voice_style(
            voice_bank.persona_index('p1')['甲'])
        self.assertEqual(vs, '少年音A', '首次出场集应优先于后续集')
        self.assertIn('首次出场集', src)
        self.assertEqual(ep, 1)

    def test_voice_map_explicit_beats_first_episode(self):
        self.script('p1', 1, [_char('甲', '少年音A')])
        vm = {'characters': {'甲': {'instruct': '手工指定音色'}}}
        vs, src, _ep = voice_bank.resolve_voice_style(
            voice_bank.persona_index('p1')['甲'], voice_map=vm)
        self.assertEqual(vs, '手工指定音色')
        self.assertEqual(src, 'voice_map 显式配置')

    def test_most_lines_episode_when_first_appearance_has_none(self):
        self.script('p1', 1, [_char('甲', '')], shots=self.shots_with_lines('甲', 1, 1))
        self.script('p1', 2, [_char('甲', '台词最多集的音')],
                    shots=self.shots_with_lines('甲', 5, 5))
        vs, src, ep = voice_bank.resolve_voice_style(
            voice_bank.persona_index('p1')['甲'])
        self.assertEqual(vs, '台词最多集的音', '首次出场集没写说明时应取台词最多集')
        self.assertIn('台词最多集', src)
        self.assertIn('5 句', src)
        self.assertEqual(ep, 2)

    def test_composed_fallback_when_no_voice_style_anywhere(self):
        self.script('p1', 1, [_char('甲', '', gender='男', age='中年',
                                    identity='杂役管事', personality='沉稳')])
        entry = voice_bank.persona_index('p1')['甲']
        vs, src, _ep = voice_bank.resolve_voice_style(entry)
        self.assertTrue(vs, '有 gender/age/identity/personality 时应拼出兜底描述')
        self.assertIn('杂役管事', vs)
        self.assertIn('兜底拼装', src)
        spec = voice_bank.build_voice_spec('p1', '甲', entry=entry)
        self.assertEqual(spec['mode'], 'design', '兜底描述也要走 VoiceDesign')

    def test_nothing_at_all_falls_back_to_preset_but_spreads(self):
        self.script('p1', 1, [_char('甲'), _char('乙'), _char('丙')])
        idx = voice_bank.persona_index('p1')
        modes = [voice_bank.build_voice_spec('p1', n, entry=idx[n])['mode']
                 for n in ('甲', '乙', '丙')]
        self.assertEqual(modes, ['preset'] * 3)
        speakers = {voice_bank.build_voice_spec('p1', n, entry=idx[n])['speaker']
                    for n in ('甲', '乙', '丙')}
        self.assertGreater(len(speakers), 1,
                           '无人设时 speaker 必须按名字散开（旧行为人人 Ryan）')


# ===================== 2. 跨集取值稳定性（核心回归） =====================

class TestCrossEpisodeStability(_Base):

    def test_order_of_file_creation_does_not_matter(self):
        for name, order in (('pa', (1, 2, 3)), ('pb', (3, 1, 2))):
            for ep in order:
                self.script(name, ep, [_char('甲', '音色%d' % ep)])
        a = voice_bank.persona_index('pa')['甲']
        b = voice_bank.persona_index('pb')['甲']
        self.assertEqual(voice_bank.resolve_voice_style(a)[0],
                         voice_bank.resolve_voice_style(b)[0],
                         '同一角色跨集取值必须与文件创建顺序无关')
        self.assertEqual(voice_bank.resolve_voice_style(a)[0], '音色1')

    def test_repeated_calls_and_cache_clear_are_stable(self):
        self.script('p1', 1, [_char('甲', '少年音A')])
        self.script('p2', 2, [_char('甲', '青年音B')])
        first = voice_bank.build_voice_spec('p1', '甲')
        for _ in range(3):
            self.assertEqual(voice_bank.build_voice_spec('p1', '甲'), first)
        voice_bank.clear_cache()
        again = voice_bank.build_voice_spec('p1', '甲')
        self.assertEqual(again, first, '清缓存后取值必须完全一致（含 seed）')
        self.assertEqual(again['instruct'], '少年音A')
        self.assertEqual(again['source_episode'], 1)

    def test_same_character_same_seed_across_projects_episodes(self):
        self.script('p1', 1, [_char('甲', '少年音A')])
        self.script('p1', 2, [_char('甲', '青年音B')])
        spec = voice_bank.build_voice_spec('p1', '甲')
        self.assertEqual(spec['seed'], int(tts_client.stable_seed('p1|甲')))


# ===================== 3. 真实缺陷形态：5 个角色不再退化成 preset =====================

class TestNoMoreRyanDegradation(_Base):

    def test_characters_only_in_later_episodes_still_get_persona(self):
        # 第 1 集只有 4 个角色（与生产同一形态），另 5 个只在第 2/3 集出现
        self.script('p1', 1, [_char('甲', '清冷少年音，先抑后扬'),
                              _char('乙', '粗豪暴戾男声'),
                              _char('丙', '清冷少女音，语速慢'),
                              _char('丁', '嘈杂群杂声，齐声哄笑')])
        self.script('p1', 2, [_char('戊', '憨厚少年音'), _char('己', '')])
        self.script('p1', 3, [_char('庚', '锐利男声，急促'), _char('辛', '粗直少年音'),
                              _char('壬', '低沉平稳男声'), _char('己', '憨直少年音')])
        idx = voice_bank.persona_index('p1')
        self.assertEqual(len(idx), 9, '三集角色并集应为 9 个')
        specs = {n: voice_bank.build_voice_spec('p1', n, entry=idx[n]) for n in idx}
        bad = [n for n, s in specs.items() if s['mode'] != 'design' or not s['instruct']]
        self.assertEqual(bad, [], '仍有人设缺失（退化成 preset）：%s' % bad)
        # 5 个「只在后集出现」的角色必须拿到各自的人设说明
        self.assertEqual(specs['戊']['instruct'], '憨厚少年音')
        self.assertEqual(specs['庚']['instruct'], '锐利男声，急促')
        self.assertEqual(specs['壬']['instruct'], '低沉平稳男声')
        self.assertEqual(specs['戊']['source_episode'], 2)
        self.assertEqual(specs['壬']['source_episode'], 3)

    def test_ref_text_carries_persona(self):
        self.script('p1', 1, [_char('甲', '少年音', identity='青云宗外门弟子',
                                    age='十六岁', personality='隐忍克制')])
        spec = voice_bank.build_voice_spec('p1', '甲')
        self.assertIn('青云宗外门弟子', spec['ref_text'])
        self.assertIn('十六岁', spec['ref_text'])

    def test_reason_log_text_names_episode_and_why(self):
        self.script('p1', 2, [_char('甲', '憨厚少年音')])
        spec = voice_bank.build_voice_spec('p1', '甲')
        self.assertIn('取第 2 集说明', spec['reason'])
        self.assertIn('首次出场集', spec['reason'])


# ===================== 4. 用户上传的参考音频永不覆盖 =====================

class _FakeTTSClient(object):
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
        _write_wav(out_path, 0.2, freq=330)
        # 与真实 tts_client.synthesize_one 的返回形状对齐（路由用 rec['out_path']）
        return {'ok': True, 'out_path': os.path.abspath(out_path),
                'path': os.path.abspath(out_path), 'duration': 0.2}


class TestRefOriginAndUpsert(_Base):

    def _put_ref(self, character, original_filename, ref_text, marker=None):
        d = os.path.join(self.dub, 'voice_bank', character)
        os.makedirs(d, exist_ok=True)
        _write_wav(os.path.join(d, 'ref.wav'), 0.2, freq=200)
        meta = {'character': character, 'original_filename': original_filename,
                'ref_text': ref_text, 'duration_sec': 0.2, 'updated_at': 'x'}
        if marker:
            meta['source'] = marker
        io.open(os.path.join(d, 'ref.json'), 'w', encoding='utf-8').write(
            json.dumps(meta, ensure_ascii=False))
        return os.path.join(d, 'ref.wav'), os.path.join(d, 'ref.json')

    def test_origin_none(self):
        self.assertEqual(voice_bank.ref_origin(self.dub, '无此人'), 'none')

    def test_origin_auto_heuristic(self):
        self._put_ref('甲', 'tmpab12cd34.wav', '我是甲，这是我的声音样本。')
        self.assertEqual(voice_bank.ref_origin(self.dub, '甲'), 'auto')

    def test_origin_uploaded_heuristic(self):
        self._put_ref('甲', '我的录音.mp3', '这是我自己录的一句话')
        self.assertEqual(voice_bank.ref_origin(self.dub, '甲'), 'uploaded')

    def test_explicit_marker_wins_over_filename(self):
        self._put_ref('甲', 'tmpab12cd34.wav', '我是甲，这是我的声音样本。',
                      marker='uploaded')
        self.assertEqual(voice_bank.ref_origin(self.dub, '甲'), 'uploaded',
                         'ref.json 的 source 标记必须优先于文件名启发式')

    def test_mark_auto_ref_roundtrip(self):
        self._put_ref('甲', 'tmpab12cd34.wav', '我是甲，这是我的声音样本。')
        voice_bank.mark_auto_ref(self.dub, '甲', mode='design', instruct='少年音',
                                 seed=7, source_episode=2,
                                 voice_style_source='首次出场集(第2集)')
        meta = voice_bank.read_ref_meta(self.dub, '甲')
        self.assertEqual(meta['source'], 'auto')
        self.assertEqual(meta['mode'], 'design')
        self.assertEqual(meta['instruct'], '少年音')
        self.assertEqual(meta['seed'], 7)
        self.assertEqual(meta['source_episode'], 2)
        self.assertEqual(voice_bank.ref_origin(self.dub, '甲'), 'auto')

    def test_ensure_never_overwrites_uploaded_even_when_forced(self):
        """force=True（重做音色）也必须跳过用户上传的参考音频。"""
        _FakeTTSClient.calls = []
        _FakeTTSClient.behavior = 'ok'
        ref_a, meta_a = self._put_ref('甲', '我的录音.mp3', '这是我自己录的', marker='uploaded')
        before_wav = io.open(ref_a, 'rb').read()
        before_meta = io.open(meta_a, 'rb').read()
        self.script('proj', 1, [_char('甲', '少年音'), _char('乙', '憨厚少年音')])
        with mock.patch.object(tts_client, 'QwenTTSClient', _FakeTTSClient):
            video_helpers._ensure_voice_bank_refs(
                [], 'proj', all_characters={'甲': {}, '乙': {}}, force=True)
        self.assertEqual([c['voice']['character'] if 'character' in c['voice'] else None
                          for c in _FakeTTSClient.calls], [None],
                         '用户上传的角色被重新合成了')
        self.assertEqual(len(_FakeTTSClient.calls), 1, '只应合成未绑定的乙')
        self.assertIn('乙', _FakeTTSClient.calls[0]['text'])
        self.assertEqual(io.open(ref_a, 'rb').read(), before_wav, '用户上传的音频被改写')
        self.assertEqual(io.open(meta_a, 'rb').read(), before_meta, '用户上传的元数据被改写')
        self.assertTrue(os.path.isfile(os.path.join(self.dub, 'voice_bank', '乙', 'ref.wav')))

    def test_ensure_marks_auto_ref_and_is_idempotent(self):
        _FakeTTSClient.calls = []
        _FakeTTSClient.behavior = 'ok'
        self.script('proj', 3, [_char('乙', '粗粝男声，怒吼', identity='外门第一')])
        with mock.patch.object(tts_client, 'QwenTTSClient', _FakeTTSClient):
            video_helpers._ensure_voice_bank_refs([], 'proj',
                                                  all_characters={'乙': {}})
            self.assertEqual(len(_FakeTTSClient.calls), 1)
            video_helpers._ensure_voice_bank_refs([], 'proj',
                                                  all_characters={'乙': {}})
            self.assertEqual(len(_FakeTTSClient.calls), 1, '已有自动音色不应重复合成')
        meta = voice_bank.read_ref_meta(self.dub, '乙')
        self.assertEqual(meta['source'], 'auto')
        self.assertEqual(meta['instruct'], '粗粝男声，怒吼')
        self.assertEqual(meta['source_episode'], 3)
        self.assertEqual(voice_bank.ref_origin(self.dub, '乙'), 'auto')

    def test_ensure_logs_source_episode_and_reason(self):
        _FakeTTSClient.calls = []
        _FakeTTSClient.behavior = 'ok'
        self.script('proj', 2, [_char('甲', '憨厚少年音')])
        with mock.patch.object(tts_client, 'QwenTTSClient', _FakeTTSClient):
            with self.assertLogs('video_helpers', level='INFO') as cm:
                video_helpers._ensure_voice_bank_refs([], 'proj',
                                                      all_characters={'甲': {}})
        joined = '\n'.join(cm.output)
        self.assertIn('取第 2 集说明', joined, '日志未写明取自哪一集')
        self.assertIn('首次出场集', joined, '日志未写明理由')
        self.assertIn('mode=design', joined)


# ===================== 5. 试听方案（走人设而非旧克隆样本） =====================

class TestPreviewSpec(_Base):

    def test_preview_uses_persona_design_for_auto_ref(self):
        self.script('proj', 1, [_char('甲', '清冷少年音，先抑后扬')])
        self._put_auto('甲')
        plan = voice_bank.preview_spec(self.dub, 'proj', '甲')
        self.assertEqual(plan['mode'], 'design')
        self.assertEqual(plan['instruct'], '清冷少年音，先抑后扬')
        self.assertEqual(plan['ref_origin'], 'auto')
        self.assertIn('VoiceDesign', plan['reason'])

    def test_preview_clones_user_uploaded_ref(self):
        self.script('proj', 1, [_char('甲', '清冷少年音')])
        d = os.path.join(self.dub, 'voice_bank', '甲')
        os.makedirs(d, exist_ok=True)
        _write_wav(os.path.join(d, 'ref.wav'), 0.2, freq=210)
        io.open(os.path.join(d, 'ref.json'), 'w', encoding='utf-8').write(
            json.dumps({'character': '甲', 'source': 'uploaded',
                        'original_filename': '我的录音.mp3',
                        'ref_text': '我自己录的'}, ensure_ascii=False))
        plan = voice_bank.preview_spec(self.dub, 'proj', '甲')
        self.assertEqual(plan['mode'], 'clone', '用户上传的音频必须被克隆试听')
        self.assertTrue(plan['ref_audio'])

    def test_preview_without_any_ref_still_uses_persona(self):
        self.script('proj', 1, [_char('甲', '低沉平稳男声')])
        plan = voice_bank.preview_spec(self.dub, 'proj', '甲')
        self.assertEqual(plan['mode'], 'design')
        self.assertEqual(plan['ref_origin'], 'none')

    def _put_auto(self, character):
        d = os.path.join(self.dub, 'voice_bank', character)
        os.makedirs(d, exist_ok=True)
        _write_wav(os.path.join(d, 'ref.wav'), 0.2, freq=200)
        io.open(os.path.join(d, 'ref.json'), 'w', encoding='utf-8').write(
            json.dumps({'character': character, 'source': 'auto',
                        'original_filename': 'tmpabcd1234.wav',
                        'ref_text': '我是%s，这是我的声音样本。' % character},
                       ensure_ascii=False))


# ===================== 6. 防回归：不得再硬编码「第1集」 =====================

class TestSourceGuard(unittest.TestCase):

    def test_ensure_voice_bank_refs_no_longer_hardcodes_episode_one(self):
        import ast
        src = io.open(os.path.join(_APP, 'video_helpers.py'), encoding='utf-8').read()
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_ensure_voice_bank_refs':
                fn = node
        self.assertIsNotNone(fn, '找不到 _ensure_voice_bank_refs')
        body = ast.get_source_segment(src, fn) or ''
        self.assertNotIn('第1集.json', body,
                         '人设来源又被写死成第 1 集（本缺陷的根因）')
        self.assertIn('voice_bank', body,
                      '_ensure_voice_bank_refs 必须走 voice_bank 的统一取值实现')

    def test_voice_bank_module_has_no_network_or_llm(self):
        src = io.open(os.path.join(_APP, 'voice_bank.py'), encoding='utf-8').read()
        for bad in ('requests', 'urllib', 'http', 'openai', 'llm'):
            self.assertNotIn('import ' + bad, src,
                             'voice_bank 不得引入网络/LLM 依赖：%s' % bad)


# ===================== 7. 端点级：试听真的走人设（替身 TTS，不占 GPU） =====================

class TestPreviewEndpoint(_Base):
    """POST /api/tts/voice-bank/preview 的端到端接线（替身 TTS 客户端 + 真 Flask 路由）。

    为什么要端点级：用户看到的「试听」就是这条链路 —— 只测 voice_bank.preview_spec
    不能证明路由真的把它用上了（历史上这里就是「函数写对了、接线没接」）。
    """

    def setUp(self):
        super().setUp()
        from flask import Flask
        import routes.tts as tts_routes
        self.tts_routes = tts_routes
        _FakeTTSClient.calls = []
        _FakeTTSClient.behavior = 'ok'
        app = Flask(__name__)
        app.register_blueprint(tts_routes.tts_bp)
        self.client = app.test_client()
        for target, attr, value in ((tts_routes, 'QwenTTSClient', _FakeTTSClient),
                                    (tts_routes, '_dub_project_dir',
                                     (lambda _pn, d=self.dub: d)),
                                    (tts_routes, '_dub_audio_url',
                                     (lambda _pn, p, **kw: '/audio/' + os.path.basename(p)))):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_preview_goes_through_persona_voice_design(self):
        self.script('projX', 2, [_char('张铁柱', '憨厚少年音', identity='青云宗外门弟子')])
        r = self.client.post('/api/tts/voice-bank/preview',
                             json={'project_name': 'projX', 'character': '张铁柱'})
        self.assertEqual(r.status_code, 200, r.data[:200])
        j = r.get_json()
        self.assertTrue(j.get('success'), j)
        self.assertEqual(j.get('voice_mode'), 'design', '试听必须走 VoiceDesign（人设）')
        self.assertEqual(j.get('instruct'), '憨厚少年音')
        self.assertEqual(j.get('source_episode'), 2)
        self.assertIn('第 2 集', j.get('preview_note') or '')
        self.assertEqual(len(_FakeTTSClient.calls), 1)
        call = _FakeTTSClient.calls[0]
        self.assertEqual(call['voice']['mode'], 'design')
        self.assertEqual(call['voice']['instruct'], '憨厚少年音')
        self.assertEqual(call['voice']['character'], '张铁柱',
                         'normalize_voice 会丢 character，必须补回')
        self.assertIn('张铁柱', call['text'])

    def test_preview_clones_when_user_uploaded(self):
        self.script('projX', 1, [_char('甲', '清冷少年音')])
        d = os.path.join(self.dub, 'voice_bank', '甲')
        os.makedirs(d, exist_ok=True)
        _write_wav(os.path.join(d, 'ref.wav'), 0.25, freq=210)
        io.open(os.path.join(d, 'ref.json'), 'w', encoding='utf-8').write(
            json.dumps({'character': '甲', 'source': 'uploaded',
                        'original_filename': '我的录音.mp3', 'ref_text': '我自己录的'},
                       ensure_ascii=False))
        r = self.client.post('/api/tts/voice-bank/preview',
                             json={'project_name': 'projX', 'character': '甲'})
        j = r.get_json()
        self.assertTrue(j.get('success'), j)
        self.assertEqual(j.get('voice_mode'), 'clone', '用户上传的参考音频必须被克隆')
        self.assertEqual(j.get('ref_origin'), 'uploaded')
        self.assertEqual(_FakeTTSClient.calls[0]['voice']['mode'], 'clone')

    def test_preview_mode_can_be_forced(self):
        self.script('projX', 1, [_char('甲', '清冷少年音')])
        r = self.client.post('/api/tts/voice-bank/preview',
                             json={'project_name': 'projX', 'character': '甲', 'mode': 'preset'})
        j = r.get_json()
        self.assertTrue(j.get('success'), j)
        self.assertEqual(j.get('voice_mode'), 'preset')



if __name__ == '__main__':
    unittest.main()
