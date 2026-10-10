# -*- coding: utf-8 -*-
"""镜像快照清理器的回归测试。

背景（2026-10-10）：
  开发时随手复制的 <文件>.bak_<主题> / <文件>.rollback_bpNN_ 会被一起同步进
  运行时镜像，越攒越多。清理器一旦「保留数算错」或「dry-run 真删了文件」，
  后果比不清理更糟，所以把这两点钉死。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import prune_mirror_backups as PM  # noqa: E402


class TestParseSnapshotName(unittest.TestCase):
    def test_bak_form(self):
        self.assertEqual(PM.parse_snapshot_name('app.py.bak_heartbeat_20261008'),
                         ('app.py', 'heartbeat_20261008'))

    def test_rollback_form(self):
        self.assertEqual(PM.parse_snapshot_name('comfyui_client.py.rollback_bp16_20261008'),
                         ('comfyui_client.py', 'bp16_20261008'))

    def test_plain_file_is_not_snapshot(self):
        self.assertIsNone(PM.parse_snapshot_name('app.py'))

    def test_dot_bak_without_underscore_is_not_snapshot(self):
        """fs_atomic 的 <path>.bak 是设计内的回退副本，不能当垃圾删。"""
        self.assertIsNone(PM.parse_snapshot_name('tasks.db.bak'))


class TestCollectAndPlan(unittest.TestCase):
    def _mk(self, root: Path, name: str, age_days: float = 0.0):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('x', encoding='utf-8')
        ts = time.time() - age_days * 86400
        os.utime(p, (ts, ts))
        return p

    def test_collect_is_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._mk(root, 'app.py.bak_a_1')
            self._mk(root, 'routes/sub.py.rollback_bp1_2')
            self._mk(root, 'normal.py')
            names = sorted(p.name for p, _b, _t, _m in PM.collect_snapshots(root))
            self.assertEqual(names, ['app.py.bak_a_1', 'sub.py.rollback_bp1_2'])

    def test_keep_latest_n_per_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, age in enumerate([0, 1, 2, 3]):
                self._mk(root, 'app.py.bak_x_%d' % i, age_days=age)
            entries = PM.collect_snapshots(root)
            delete = PM.plan_deletions(entries, keep=2)
            self.assertEqual(len(delete), 2)
            # 保留的是「最新两个」——即 age=0、1
            kept = [e[0].name for e in entries if e[0] not in [d[0][0] for d in delete]]
            self.assertEqual(sorted(kept), ['app.py.bak_x_0', 'app.py.bak_x_1'])

    def test_groups_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(3):
                self._mk(root, 'a.py.bak_t_%d' % i, age_days=i)
            for i in range(3):
                self._mk(root, 'b.py.bak_t_%d' % i, age_days=i)
            delete = PM.plan_deletions(PM.collect_snapshots(root), keep=2)
            self.assertEqual(len(delete), 2)

    def test_days_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._mk(root, 'app.py.bak_new_1', age_days=0)
            self._mk(root, 'app.py.bak_old_2', age_days=40)
            delete = PM.plan_deletions(PM.collect_snapshots(root), keep=3, days=30)
            self.assertEqual([d[0][0].name for d in delete], ['app.py.bak_old_2'])

    def test_days_zero_means_no_age_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._mk(root, 'app.py.bak_old_1', age_days=400)
            self.assertEqual(PM.plan_deletions(PM.collect_snapshots(root), keep=3, days=0), [])


class TestMain(unittest.TestCase):
    def _prepare(self, root: Path, n=4):
        for i in range(n):
            p = root / ('app.py.bak_x_%d' % i)
            p.write_text('x', encoding='utf-8')
            ts = time.time() - i * 86400
            os.utime(p, (ts, ts))

    def test_dry_run_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root)
            rc = PM.main(['--dir', str(root), '--keep', '1'])
            self.assertEqual(rc, 0)
            self.assertEqual(len(PM.collect_snapshots(root)), 4, 'dry-run 不得删除任何文件')

    def test_apply_keeps_n_and_deletes_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root)
            rc = PM.main(['--dir', str(root), '--keep', '1', '--apply'])
            self.assertEqual(rc, 0)
            remaining = [p.name for p, _b, _t, _m in PM.collect_snapshots(root)]
            self.assertEqual(remaining, ['app.py.bak_x_0'], '应只保留最新一个')

    def test_check_mode_reports_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare(root, n=1)
            self.assertEqual(PM.main(['--dir', str(root), '--check']), 1)

    def test_check_mode_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('x', encoding='utf-8')
            self.assertEqual(PM.main(['--dir', str(root), '--check']), 0)

    def test_missing_dir_is_not_an_error(self):
        self.assertEqual(PM.main(['--dir', 'Z:/nope', '--check']), 0)


if __name__ == '__main__':
    unittest.main()
