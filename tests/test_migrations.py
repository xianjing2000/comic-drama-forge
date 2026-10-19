# -*- coding: utf-8 -*-
"""app/migrations.py 回归测试（T23 §2.2，2026-10-16）。

钉住四条不变量：
  · 无迁移时 migrate() 幂等且不改版本；
  · 有迁移时**顺序应用**、写 user_version、再跑不重复应用；
  · 单条失败**回滚**（DDL 也回滚）且**不推进版本**；
  · 迁移前**生成备份**。
运行：python -m unittest discover -s tests -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import migrations as M  # noqa: E402


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="migr_")
        self.db = os.path.join(self.d, "t.db")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _mk(self):
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE t(id INTEGER, name TEXT)")
        c.execute("INSERT INTO t(id, name) VALUES (0, 'seed')")
        c.commit()
        c.close()

    def _cols(self):
        c = sqlite3.connect(self.db)
        try:
            return [r[1] for r in c.execute("PRAGMA table_info(t)")]
        finally:
            c.close()

    def test_no_migrations_is_noop(self):
        self._mk()
        r = M.migrate(self.db)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["applied"], [])
        self.assertEqual(M.current_version(self.db), 0)

    def test_applies_in_order_and_is_idempotent(self):
        self._mk()
        calls = []

        def m1(conn):
            conn.execute("ALTER TABLE t ADD COLUMN age INTEGER")
            calls.append(1)

        def m2(conn):
            conn.execute("UPDATE t SET age=7 WHERE id=0")
            calls.append(2)

        old = M.MIGRATIONS[:]
        M.MIGRATIONS.extend([(2, "set age", m2), (1, "add age col", m1)])  # 乱序登记
        try:
            r = M.migrate(self.db)
            self.assertTrue(r["ok"], r)
            self.assertEqual([v for v, _ in r["applied"]], [1, 2])   # 按版本号顺序
            self.assertEqual(M.current_version(self.db), 2)
            self.assertIn("age", self._cols())
            self.assertEqual(calls, [1, 2])
            # 幂等：再跑不重复应用
            r2 = M.migrate(self.db)
            self.assertEqual(r2["applied"], [])
            self.assertEqual(calls, [1, 2])
            # 数据仍在 + 新列有值
            c = sqlite3.connect(self.db)
            self.assertEqual(c.execute("SELECT age FROM t WHERE id=0").fetchone()[0], 7)
            self.assertEqual(c.execute("SELECT name FROM t WHERE id=0").fetchone()[0], 'seed')
            c.close()
            # 备份生成
            self.assertTrue(os.path.isfile(r["backup"]), r["backup"])
        finally:
            M.MIGRATIONS[:] = old

    def test_failure_rolls_back_ddl_and_keeps_version(self):
        self._mk()

        def bad(conn):
            conn.execute("ALTER TABLE t ADD COLUMN boom TEXT")
            raise RuntimeError("boom")

        old = M.MIGRATIONS[:]
        M.MIGRATIONS.append((5, "bad migration", bad))
        try:
            r = M.migrate(self.db)
            self.assertFalse(r["ok"])
            self.assertEqual(r["failed"][0][0], 5)
            self.assertEqual(M.current_version(self.db), 0)      # 未推进
            self.assertNotIn("boom", self._cols())               # DDL 已回滚
        finally:
            M.MIGRATIONS[:] = old

    def test_pending_lists_unapplied(self):
        self._mk()
        old = M.MIGRATIONS[:]
        M.MIGRATIONS.append((9, "占位", lambda c: None))
        try:
            self.assertEqual([v for v, _ in M.pending(self.db)], [9])
        finally:
            M.MIGRATIONS[:] = old


if __name__ == '__main__':
    unittest.main()
