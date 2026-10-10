# -*- coding: utf-8 -*-
"""common_util 单元测试（2026-10-10 建立最小回归保护）。

为什么建：设计审查发现项目有 410 个 .local/test_*.py 手工脚本，但**没有一个
可重复运行的测试**，导致 shot_duration_max、COVERAGE_MAX_ROUNDS 这类不一致
能长期存在而无人发现。本套件用标准库 unittest（零依赖，不要求装 pytest）。
运行：python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import common_util as U  # noqa: E402


class TestNow(unittest.TestCase):
    def test_format(self):
        s = U.now()
        self.assertRegex(s, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_two_calls_monotonic(self):
        self.assertLessEqual(U.now(), U.now())


class TestNonempty(unittest.TestCase):
    def test_falsy(self):
        for v in (None, '', '   ', [], {}, set()):
            self.assertFalse(U.nonempty(v), 'expect empty: %r' % (v,))

    def test_truthy(self):
        for v in ('x', ' x ', [0], {'a': 1}, 0, False):
            self.assertTrue(U.nonempty(v), 'expect non-empty: %r' % (v,))


class TestAsDict(unittest.TestCase):
    """as_dict 的三个分支缺一不可 —— 第一版曾漏掉 list 包裹分支。"""

    def test_plain_dict(self):
        self.assertEqual(U.as_dict({'a': 1}), {'a': 1})

    def test_list_wrapped(self):
        # 兼容部分模型把 JSON 对象包在数组里返回
        self.assertEqual(U.as_dict([{'a': 1}]), {'a': 1})
        self.assertEqual(U.as_dict([1, 2, {'b': 2}]), {'b': 2})

    def test_json_string(self):
        self.assertEqual(U.as_dict('{"a": 1}'), {'a': 1})

    def test_json_array_string(self):
        self.assertEqual(U.as_dict('[{"c": 3}]'), {'c': 3})

    def test_bad_input(self):
        for v in ('{bad', None, '', 123, [1, 2], '[]'):
            self.assertEqual(U.as_dict(v), {})


class TestMaskKey(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(U.mask_key(''), '')
        self.assertEqual(U.mask_key(None), '')

    def test_short(self):
        self.assertEqual(U.mask_key('abc'), '***')

    def test_long(self):
        self.assertEqual(U.mask_key('sk-1234567890'), 'sk-123****')


class TestJsonIO(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_roundtrip(self):
        p = os.path.join(self.d, 'sub', 'x.json')
        self.assertTrue(U.write_json(p, {'k': 'v'}))
        self.assertEqual(U.read_json(p), {'k': 'v'})

    def test_atomic_no_tmp_left(self):
        p = os.path.join(self.d, 'y.json')
        U.write_json(p, {'a': 1})
        self.assertEqual(sorted(os.listdir(self.d)), ['y.json'])

    def test_missing_returns_default(self):
        self.assertEqual(U.read_json(os.path.join(self.d, 'no.json'), {'d': 1}), {'d': 1})

    def test_corrupt_returns_default(self):
        p = os.path.join(self.d, 'z.json')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('{broken')
        self.assertEqual(U.read_json(p, {'d': 2}), {'d': 2})

    def test_unicode_preserved(self):
        p = os.path.join(self.d, 'cn.json')
        U.write_json(p, {'名': '中文'})
        self.assertEqual(U.read_json(p), {'名': '中文'})


class TestNoBusinessImport(unittest.TestCase):
    """common_util 不得 import 任何业务模块 —— 否则会引入新的循环依赖。"""

    def test_no_business_import(self):
        with open(os.path.join(os.path.dirname(U.__file__), 'common_util.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        for bad in ('import config', 'import pipeline', 'import autopilot',
                    'import qc_client', 'import llm_client', 'import continuity'):
            self.assertNotIn(bad, src)


if __name__ == '__main__':
    unittest.main()
