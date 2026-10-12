# -*- coding: utf-8 -*-
"""task_store.create 的 upsert 契约测试（2026-10-12 数据层审计 T23 的 P0 数据丢失 bug）。

被锁定的缺陷：task_store.create 原来用 ``INSERT OR REPLACE``。SQLite 的 OR REPLACE 语义是
「先 DELETE 再 INSERT」，而 units 表带 ``FOREIGN KEY ... ON DELETE CASCADE``（schema.py:62-69）
→ 同一 task_id 二次 create 会**静默清空该任务的全部 units**（= 断点续跑进度），实测 units 1 → 0。
真实触发点：routes/keyframes.py:260 用 `keyframe_{project}_{秒级时间戳}` 作 task_id。

这些用例在旧实现下会变红，在当前 upsert 实现下必须全绿。
"""
import importlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


class TaskStoreUpsertTest(unittest.TestCase):
    """同一 task_id 二次 create 不得级联清空 units。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="taskstore_upsert_")
        self.db = os.path.join(self.tmp, "tasks.db")
        os.environ["MJSCXT_AUTOPILOT"] = "0"
        os.environ["MJSCXT_DATA_DIR"] = self.tmp
        for mod in ("config", "task_store", "schema"):
            sys.modules.pop(mod, None)
        self.ts = importlib.import_module("task_store")
        self.store = self.ts.TaskStore(self.db)

    def tearDown(self):
        for mod in ("config", "task_store", "schema"):
            sys.modules.pop(mod, None)

    def test_recreate_same_task_id_keeps_units(self):
        """核心契约：二次 create 后 units 必须还在（旧 INSERT OR REPLACE 会级联删掉）。"""
        tid = "fixed-task-id-for-upsert-test"
        self.store.create(kind="storyboard", project="p", label="L1", task_id=tid)
        self.store.mark_unit(tid, "shot_01", "done")
        self.store.mark_unit(tid, "shot_02", "done")
        before = len(self.store.list_units(tid))
        self.assertEqual(before, 2, "前置条件：应已有 2 个 unit")

        # ★ 二次 create 同一个 task_id —— 旧实现会在这里级联删掉全部 units
        self.store.create(kind="storyboard", project="p", label="L2", task_id=tid)

        after = len(self.store.list_units(tid))
        self.assertEqual(
            after, 2,
            "二次 create 清空了 units（INSERT OR REPLACE 的级联删除回归）—— "
            "断点续跑进度会因此静默丢失")
        self.assertEqual(self.store.done_units(tid), {"shot_01", "shot_02"},
                         "done_units 也应保持完整")

    def test_recreate_preserves_created_at(self):
        """upsert 应保留原 created_at，只更新 updated_at。"""
        tid = "fixed-task-id-created-at"
        self.store.create(kind="k", project="p", label="A", task_id=tid)
        conn = self.store._conn()
        try:
            row1 = conn.execute(
                "SELECT created_at, label FROM tasks WHERE id=?", (tid,)).fetchone()
        finally:
            conn.close()
        self.store.create(kind="k", project="p", label="B", task_id=tid)
        conn = self.store._conn()
        try:
            row2 = conn.execute(
                "SELECT created_at, label FROM tasks WHERE id=?", (tid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row1[0], row2[0], "created_at 不应被二次 create 改写")
        self.assertEqual(row2[1], "B", "二次 create 应更新 label")

    def test_source_has_no_insert_or_replace_on_tasks(self):
        """文本守卫：tasks 表不得再出现 INSERT OR REPLACE（防复发）。"""
        with open(os.path.join(APP, "task_store.py"), encoding="utf-8") as fh:
            src = fh.read()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue   # 注释里提到历史 bug 是允许的
            self.assertNotIn(
                "INSERT OR REPLACE INTO tasks", stripped,
                "tasks 表出现 INSERT OR REPLACE —— 会级联删 units，必须用 upsert")


if __name__ == "__main__":
    unittest.main()