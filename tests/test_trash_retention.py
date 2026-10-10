# -*- coding: utf-8 -*-
"""回收站保留策略的回归测试。

背景（2026-10-10 设计审查）：
  软删除只有入口没有出口 —— 12+ 处代码往 output/projects/_trash 搬东西，
  却没有任何代码清理它，前端也没有入口，实测两处数据根累计 6.42 GB。

本测试钉住两件事：
  ① 保留策略算得对（分类桶 / 每项目 / 天数三条规则）；
  ② **安全闸**：dry_run 绝不落盘；执行时只删 _trash 的直接子项，绝不越界。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import output_reclaim as OR  # noqa: E402


def _mkentry(base: Path, name: str, age_days: float, n_files: int = 1, size: int = 1024):
    """在 base 下造一个条目，其内部最新文件的时间为 age_days 天前。"""
    p = base / name
    p.mkdir(parents=True, exist_ok=True)
    ts = time.time() - age_days * 86400
    for i in range(n_files):
        f = p / ('f%d.bin' % i)
        f.write_bytes(b'x' * size)
        os.utime(f, (ts, ts))
    os.utime(p, (ts, ts))
    return p


class TestTrashPlan(unittest.TestCase):
    def test_missing_trash_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = OR.trash_plan(tmp)
            self.assertTrue(plan['ok'])
            self.assertEqual(plan['keep'], [])
            self.assertEqual(plan['drop'], [])

    def test_category_bucket_keeps_recent_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            bucket = trash / 'qc_reject'
            for i, age in enumerate([0, 1, 2, 3, 4, 5, 6, 7]):
                _mkentry(bucket, '2026010%d_120000_proj' % (i + 1), age_days=age)
            plan = OR.trash_plan(str(root))
            self.assertTrue(plan['ok'])
            limit = OR.TRASH_KEEP_PER_CATEGORY['qc_reject']
            self.assertEqual(len(plan['keep']), limit)
            self.assertEqual(len(plan['drop']), 8 - limit)
            # 留下的是最新的那几个
            kept_ages = sorted(x['age_days'] for x in plan['keep'])
            self.assertLess(max(kept_ages), min(x['age_days'] for x in plan['drop']))

    def test_project_snapshot_keeps_fresh_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            _mkentry(trash, '20261010_120000_alpha', age_days=1)
            _mkentry(trash, '20261009_120000_alpha', age_days=2)
            plan = OR.trash_plan(str(root), keep_days=7, keep_per_project=1)
            self.assertEqual(len(plan['drop']), 0, '7 天内的都应保留')

    def test_project_snapshot_keeps_recent_per_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            # 同一项目 5 个旧快照（都超过 keep_days），另一项目 1 个
            for i in range(5):
                _mkentry(trash, '2026010%d_120000_alpha' % (i + 1), age_days=30 + i)
            _mkentry(trash, '20260101_120000_beta', age_days=40)
            plan = OR.trash_plan(str(root), keep_days=7, keep_per_project=3)
            labels = sorted(x['label'] for x in plan['drop'])
            self.assertEqual(len(plan['keep']), 4, 'alpha 留 3 + beta 留 1（各项目至少留 kp 个）')
            self.assertEqual(len(labels), 2, 'alpha 5 个里只应丢 2 个')

    def test_totals_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            for i in range(4):
                _mkentry(trash, '2026010%d_120000_p' % (i + 1), age_days=30 + i, n_files=2, size=1000)
            plan = OR.trash_plan(str(root), keep_days=7, keep_per_project=1)
            self.assertGreater(plan['total_bytes'], 0)
            self.assertGreater(plan['drop_bytes'], 0)
            self.assertLess(plan['drop_bytes'], plan['total_bytes'])


class TestTrashReclaimSafety(unittest.TestCase):
    def test_dry_run_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            for i in range(5):
                _mkentry(trash, '2026010%d_120000_p' % (i + 1), age_days=30 + i)
            OR.trash_reclaim(str(root), keep_days=7, keep_per_project=1, dry_run=True)
            self.assertEqual(len(os.listdir(trash)), 5, 'dry-run 不得删除任何条目')

    def test_apply_removes_only_planned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            for i in range(5):
                _mkentry(trash, '2026010%d_120000_p' % (i + 1), age_days=30 + i)
            plan = OR.trash_plan(str(root), keep_days=7, keep_per_project=2)
            expect_keep = {x['label'] for x in plan['keep']}
            res = OR.trash_reclaim(str(root), keep_days=7, keep_per_project=2, dry_run=False)
            left = set(os.listdir(trash))
            self.assertEqual(left, expect_keep)
            self.assertEqual(len(res['removed']), len(plan['drop']))

    def test_never_touches_trash_root_or_siblings(self):
        """安全闸：_trash 自身与 projects/ 下的其它目录都不能被删。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            _mkentry(trash, '20250101_120000_old', age_days=400)
            keep_dir = root / '进境_整本小说'          # 正常项目目录，绝不能碰
            (keep_dir).mkdir()
            (keep_dir / 'config.json').write_text('{}', encoding='utf-8')
            OR.trash_reclaim(str(root), keep_days=1, keep_per_project=1, dry_run=False)
            self.assertTrue(trash.exists(), '_trash 目录本身必须保留')
            self.assertTrue(keep_dir.exists(), 'projects/ 下的正常目录绝不能被删')
            self.assertTrue((keep_dir / 'config.json').exists())

    def test_empty_projects_dir_is_reported_not_crash(self):
        plan = OR.trash_plan('')
        self.assertFalse(plan['ok'])
        self.assertTrue(plan['error'])


class TestStartupReport(unittest.TestCase):
    def test_report_is_one_line_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = root / '_trash'
            for i in range(4):
                _mkentry(trash, '2026010%d_120000_p' % (i + 1), age_days=30 + i)
            line = OR.trash_startup_report(str(root), apply=False)
            self.assertIsInstance(line, str)
            self.assertIn('回收站', line)
            self.assertEqual(len(os.listdir(trash)), 4, 'apply=False 时不得删除')

    def test_report_on_bad_path_does_not_raise(self):
        self.assertIsInstance(OR.trash_startup_report('Z:/definitely/not/here', apply=True), str)


if __name__ == '__main__':
    unittest.main()
