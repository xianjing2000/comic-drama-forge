# -*- coding: utf-8 -*-
"""schema 模块回归测试（2026-10-10）。

覆盖：幂等建表建索引、版本记录、预留表的标注、只读报告不抛异常。
运行：python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'app'))

import schema as S  # noqa: E402


class TestEnsureSchema(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='schematest_')
        self.db = os.path.join(self.d, 'output', 'tasks.db')
        # 先建好父目录：本测试要直接用 sqlite3.connect 造既有数据，
        # 而 ensure_schema 才会建目录 —— 不先建会 "unable to open database file"。
        os.makedirs(os.path.dirname(self.db), exist_ok=True)

    def test_creates_from_scratch(self):
        r = S.ensure_schema(self.db)
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['version'], S.SCHEMA_VERSION)
        self.assertIn('tasks', r['created_tables'])
        self.assertIn('app_settings', r['created_tables'])

    def test_idempotent(self):
        S.ensure_schema(self.db)
        r2 = S.ensure_schema(self.db)
        self.assertTrue(r2['ok'])
        self.assertEqual(r2['created_tables'], [], '第二次不应再建表')
        self.assertEqual(r2['created_indexes'], [], '第二次不应再建索引')

    def test_indexes_exist(self):
        S.ensure_schema(self.db)
        c = sqlite3.connect(self.db)
        idx = set(x[0] for x in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall())
        c.close()
        for want in ('idx_tasks_project_status', 'idx_tasks_created_at', 'idx_units_task'):
            self.assertIn(want, idx, '缺索引 %s' % want)

    def test_preserves_existing_data(self):
        """已存在的表与数据不能被破坏 —— 生产库就在用同一个 ensure_schema。"""
        c = sqlite3.connect(self.db)
        c.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT,'
                  ' value_type TEXT, updated_at TEXT, note TEXT)')
        c.execute("INSERT INTO app_settings(key, value) VALUES('k', 'v')")
        c.commit()
        c.close()
        S.ensure_schema(self.db)
        c = sqlite3.connect(self.db)
        row = c.execute("SELECT value FROM app_settings WHERE key='k'").fetchone()
        c.close()
        self.assertEqual(row[0], 'v', '既有数据被破坏')

    def test_bad_path_fails_open(self):
        r = S.ensure_schema('')
        self.assertFalse(r['ok'])
        self.assertTrue(r.get('error'))


class TestReport(unittest.TestCase):
    def test_report_marks_reserved_tables(self):
        d = tempfile.mkdtemp(prefix='schemarep_')
        db = os.path.join(d, 'output', 'tasks.db')
        S.ensure_schema(db)
        rep = S.report(db)
        self.assertTrue(rep['ok'], rep.get('error'))
        self.assertIn('tasks', rep['not_connected'],
                      'tasks 是预留未接入的表，应被标注')
        self.assertIn('units', rep['not_connected'])

    def test_report_missing_db_fails_open(self):
        rep = S.report(os.path.join(tempfile.gettempdir(), 'definitely_missing_x.db'))
        self.assertFalse(rep['ok'])
        self.assertTrue(rep.get('error'))


if __name__ == '__main__':
    unittest.main()
