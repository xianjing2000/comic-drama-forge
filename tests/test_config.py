# -*- coding: utf-8 -*-
"""配置中心 / 体检 回归测试（2026-10-10）。

覆盖本轮修过的真实缺陷：
  · range 推断错误导致正常值写不进去（NOVEL_DEFAULT_SHOTS 曾被推成 (0,1)）；
  · 别名 key 读写（target_shots → novel_default_shots）；
  · must_match 跨模块同步（改一处两处一起改）；
  · 孤儿检测（可配置但未登记）；
  · REGISTRY 无重复 key。
运行：python -m unittest discover -s tests -v
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'app'))
# 用临时数据目录，避免污染真实配置库
_TMP = tempfile.mkdtemp(prefix='cfgtest_')
os.environ['MJSCXT_DATA_DIR'] = _TMP

import config_center as CC  # noqa: E402
import config_doctor as CD  # noqa: E402


class TestRegistryShape(unittest.TestCase):
    def test_no_duplicate_literal_keys(self):
        with open(os.path.join(os.path.dirname(CD.__file__), 'config_doctor.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        i = src.index('REGISTRY: Dict[str, Dict[str, Any]] = {')
        j = src.index(chr(10) + '}', i)
        keys = re.findall(r"^    '([a-z0-9_]+)': \{", src[i:j], re.M)
        self.assertEqual(len(keys), len(set(keys)), 'REGISTRY 有重复 key: %s'
                         % [k for k in keys if keys.count(k) > 1])

    def test_every_entry_has_required_fields(self):
        for key, meta in CD.REGISTRY.items():
            for f in ('default', 'type', 'group', 'owner'):
                self.assertIn(f, meta, '%s 缺字段 %s' % (key, f))
            self.assertIn(meta['type'], ('int', 'float', 'bool', 'str'), key)

    def test_owners_resolvable(self):
        """每个 owner / must_match 都必须能逐段解析到真实属性。"""
        bad = []
        for key, meta in CD.REGISTRY.items():
            for dotted in [str(meta.get('owner') or '')] + list(meta.get('must_match') or []):
                parts = dotted.split('.')
                try:
                    obj = __import__(parts[0])
                    for p in parts[1:]:
                        obj = obj[p] if isinstance(obj, dict) else getattr(obj, p)
                except Exception as e:  # noqa: BLE001
                    bad.append('%s (%s)' % (dotted, type(e).__name__))
        self.assertEqual(bad, [], 'owner 解析失败: %s' % bad)

    def test_no_wrong_int_range(self):
        """int 类型不应出现 (0,1) 这种明显错误的范围（曾在 NOVEL_DEFAULT_SHOTS 发生）。"""
        bad = [k for k, m in CD.REGISTRY.items()
               if m.get('type') == 'int' and tuple(m.get('range') or ()) == (0, 1)]
        self.assertEqual(bad, [], 'int 项范围异常: %s' % bad)


class TestAliasAndSync(unittest.TestCase):
    def test_alias_maps_to_canonical(self):
        self.assertEqual(CC.canonical_key('target_shots'), 'novel_default_shots')
        self.assertEqual(CC.canonical_key('novel_default_shots'), 'novel_default_shots')

    def test_alias_read_write(self):
        CC.set_value('target_shots', 12, note='test')
        self.assertEqual(CC.get_value('novel_default_shots'), 12)
        CC.set_value('novel_default_shots', 0, note='test-restore')

    def test_range_rejects_absurd(self):
        r = CC.set_value('novel_default_shots', 999999, note='test')
        self.assertFalse(r.get('ok'))
        self.assertIn('范围', r.get('error', ''))

    def test_unregistered_key_rejected(self):
        r = CC.set_value('definitely_not_a_key', 1)
        self.assertFalse(r.get('ok'))

    def test_must_match_writes_both(self):
        """COVERAGE_MAX_ROUNDS 在 continuity 与 novel_to_script 两处，必须同步。"""
        import continuity
        import novel_to_script
        CC.set_value('coverage_max_rounds', 4, note='test')
        self.assertEqual(continuity.COVERAGE_MAX_ROUNDS, 4)
        self.assertEqual(novel_to_script.COVERAGE_MAX_ROUNDS, 4)
        CC.set_value('coverage_max_rounds', 3, note='test-restore')


class TestDoctor(unittest.TestCase):
    def test_doctor_ok_and_no_orphans(self):
        rep = CD.doctor()
        self.assertTrue(rep.get('ok'),
                        '配置体检未通过: %s' % (rep.get('warnings') or [])[:5])
        self.assertEqual((rep.get('orphans') or {}).get('count'), 0,
                         '存在未登记的可配置常量: %s'
                         % [(x['module'], x['name']) for x in (rep['orphans'].get('items') or [])])

    def test_doctor_never_raises(self):
        self.assertIsInstance(CD.doctor(), dict)
        self.assertIsInstance(CD.summary_line(), str)


class TestAudioSilenceLockstep(unittest.TestCase):
    """静音判据的跨模块等值（2026-10-12 T12/T14）。

    为什么必须成对：quality_stage.py:262-263 的 A 层 near-silent 是一个 or 表达式 ——
        mean_db < AUDIO_NEAR_SILENT_DB   or   ratio < AUDIO_HARD_SILENT_RATIO
    两个字段各自在 quality_stage 与 audio_qc 各定义一份（同义、只有作用域是整轨/单段）。
    只锁 ratio 不锁 dB，就会出现「dB 判死、ratio 判活」的自相矛盾。
    两条 dB 常量还是 orphan 扫描的**盲区**（关键词表不认 DB 类后缀），只能人工声明。
    """

    #: (REGISTRY 键, owner, must_match 目标)
    PAIRS = (('audio_hard_silent_ratio', 'quality_stage.AUDIO_HARD_SILENT_RATIO',
              'audio_qc.HARD_SILENT_RATIO'),
             ('audio_near_silent_db', 'quality_stage.AUDIO_NEAR_SILENT_DB',
              'audio_qc.NEAR_SILENT_MEAN_DB'))

    def test_both_halves_are_declared(self):
        for key, owner, other in self.PAIRS:
            meta = CD.REGISTRY.get(key) or {}
            self.assertEqual(meta.get('owner'), owner, key)
            self.assertIn(other, meta.get('must_match') or [],
                          '%s 未声明 must_match=%s（只锁一半会自相矛盾）' % (key, other))
        # audio_qc 侧的 owner 条目也必须登记（否则 orphan 扫描看不见 —— dB 因为命名盲区尤其危险）
        for key, owner in (('hard_silent_ratio', 'audio_qc.HARD_SILENT_RATIO'),
                           ('near_silent_mean_db', 'audio_qc.NEAR_SILENT_MEAN_DB')):
            self.assertEqual((CD.REGISTRY.get(key) or {}).get('owner'), owner, key)

    def test_values_unchanged(self):
        """数值不动：ratio 仍 0.15、dB 仍 -50.0（登记值 == 代码值）。"""
        import audio_qc
        import quality_stage
        self.assertEqual(CD.REGISTRY['hard_silent_ratio']['default'], 0.15)
        self.assertEqual(CD.REGISTRY['audio_hard_silent_ratio']['default'], 0.15)
        self.assertEqual(CD.REGISTRY['near_silent_mean_db']['default'], -50.0)
        self.assertEqual(CD.REGISTRY['audio_near_silent_db']['default'], -50.0)
        self.assertEqual(audio_qc.HARD_SILENT_RATIO, 0.15)
        self.assertEqual(quality_stage.AUDIO_HARD_SILENT_RATIO, 0.15)
        self.assertEqual(audio_qc.NEAR_SILENT_MEAN_DB, -50.0)
        self.assertEqual(quality_stage.AUDIO_NEAR_SILENT_DB, -50.0)

    def test_change_one_side_moves_both(self):
        """apply_to_runtime 回写 owner 时会一并写到 must_match 目标 —— 改一处两处一起变。"""
        import audio_qc
        import quality_stage
        before = (audio_qc.HARD_SILENT_RATIO, quality_stage.AUDIO_HARD_SILENT_RATIO,
                  audio_qc.NEAR_SILENT_MEAN_DB, quality_stage.AUDIO_NEAR_SILENT_DB)
        forced = {'audio_hard_silent_ratio': 0.25, 'audio_near_silent_db': -60.0}
        resolved = {}
        for k, v in forced.items():
            meta = CD.REGISTRY[k]
            resolved[k] = {'owner': meta['owner'],
                           'must_match': meta.get('must_match') or [],
                           'value': v, 'source': 'db'}
        try:
            with mock.patch.object(CC, 'resolve_all', lambda: dict(resolved)):
                CC.apply_to_runtime()
            self.assertEqual(audio_qc.HARD_SILENT_RATIO, 0.25)
            self.assertEqual(quality_stage.AUDIO_HARD_SILENT_RATIO, 0.25,
                             'ratio 两处未联动')
            self.assertEqual(audio_qc.NEAR_SILENT_MEAN_DB, -60.0)
            self.assertEqual(quality_stage.AUDIO_NEAR_SILENT_DB, -60.0,
                             'dB 两处未联动')
        finally:
            (audio_qc.HARD_SILENT_RATIO, quality_stage.AUDIO_HARD_SILENT_RATIO,
             audio_qc.NEAR_SILENT_MEAN_DB, quality_stage.AUDIO_NEAR_SILENT_DB) = before

    def test_single_sided_change_is_detected(self):
        """只改一处不改另一处 → doctor 必须报 duplicates 且 ok=False（护栏的意义）。"""
        import audio_qc
        old = audio_qc.HARD_SILENT_RATIO
        try:
            audio_qc.HARD_SILENT_RATIO = 0.99
            rep = CD.doctor()
            self.assertFalse(rep['ok'], '单边改动没有被体检拦下')
            self.assertTrue(
                any(d.get('other') == 'audio_qc.HARD_SILENT_RATIO'
                    for d in (rep.get('duplicates') or [])),
                'duplicates 未点名 audio_qc.HARD_SILENT_RATIO：%s' % rep.get('duplicates'))
        finally:
            audio_qc.HARD_SILENT_RATIO = old

    def test_silence_thresholds_are_not_orphans(self):
        """dB 是扫描盲区，登记后仍要确保它们不在孤儿名单里（防回退）。"""
        names = {(x['module'], x['name']) for x in (CD.orphan_candidates().get('items') or [])}
        self.assertNotIn(('audio_qc', 'NEAR_SILENT_MEAN_DB'), names)
        self.assertNotIn(('quality_stage', 'AUDIO_NEAR_SILENT_DB'), names)


class TestRegistrationMatchesCodeDefaults(unittest.TestCase):
    """登记值必须与代码实际默认一致（2026-10-12 T14）。

    背景：max_tokens_ceiling 的登记 default 长期是 32768，而 llm_client.py:99 的实际默认是 65536
    （可被 MJSCXT_MAX_TOKENS_CEILING 覆盖）—— 于是**冷进程** doctor() 恒 ok=False；
    只有在同进程里先跑过 config_center.apply_to_runtime()（把登记值回写进模块属性）才会变绿，
    这正好**掩盖**了登记漂移。本任务按契约「以更新登记解决、不改 llm_client.py」。
    """

    def test_max_tokens_ceiling_registration_and_desc(self):
        import llm_client
        meta = CD.REGISTRY['max_tokens_ceiling']
        self.assertEqual(meta['default'], 65536)
        self.assertEqual(meta['default'], llm_client.MAX_TOKENS_CEILING,
                         '登记 default 与 llm_client.MAX_TOKENS_CEILING 漂移')
        self.assertIn('MJSCXT_MAX_TOKENS_CEILING', meta['desc'],
                      'desc 未说明该上限可被环境变量覆盖')

    def test_doctor_has_no_registration_mismatch(self):
        """doctor 的「登记默认 vs 实际值」不符项必须为空（冷/热进程都成立）。"""
        rep = CD.doctor()
        bad = [(r['key'], r['expect'], r['actual'])
               for r in (rep.get('params') or []) if not r.get('match')]
        self.assertEqual(bad, [], '登记与代码实际值不符：%s' % bad)
        self.assertTrue(rep.get('ok'), 'doctor 未通过：%s' % (rep.get('warnings') or [])[:3])


if __name__ == '__main__':
    unittest.main()
