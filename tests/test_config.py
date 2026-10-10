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


if __name__ == '__main__':
    unittest.main()
