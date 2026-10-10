# -*- coding: utf-8 -*-
"""output_reclaim 回归测试（2026-10-10）。

重点是**安全性质**而非功能：绝不能把正式产物列入清理白名单，
绝不能删除正在写入的目录，默认必须是预演模式。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'app'))

import output_reclaim as OR  # noqa: E402


class TestSafety(unittest.TestCase):
    def test_whitelist_never_targets_real_products(self):
        """白名单里绝不能出现正式产物/系统数据目录。"""
        for p in OR.SAFE_CACHE_PATHS:
            for dangerous in ('projects', 'asset_lib', 'dub', 'qc', 'lessons', 'novels'):
                self.assertNotIn(dangerous, p.split('/'),
                                 '安全白名单里出现了正式目录 %s：%s' % (dangerous, p))

    def test_default_is_dry_run(self):
        r = OR.reclaim('')
        self.assertTrue(r['dry_run'], '默认必须是预演模式，不能直接删')

    def test_skips_recently_written(self):
        """刚写入的目录必须被跳过（渲染器可能正在用）。"""
        d = tempfile.mkdtemp(prefix='orrec_')
        # 造一个白名单内的目录，且刚写过
        target = os.path.join(d, '_te3d_render', '_browser_profile', 'Default', 'Cache')
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, 'x.bin'), 'wb') as f:
            f.write(b'0' * 1024)
        r = OR.reclaim(d, dry_run=False, min_age_hours=6.0)
        self.assertTrue(os.path.isdir(target), '刚写入的缓存目录不应被删除')
        self.assertTrue(any('仍在写入' in s['reason'] for s in r['skipped']))

    def test_removes_old_cache_when_executed(self):
        """超龄的缓存目录在 dry_run=False 时应被删除。"""
        d = tempfile.mkdtemp(prefix='orrec2_')
        target = os.path.join(d, '_te3d_render', '_browser_profile', 'Default', 'Code Cache')
        os.makedirs(target, exist_ok=True)
        fp = os.path.join(target, 'old.bin')
        with open(fp, 'wb') as f:
            f.write(b'0' * 2048)
        old = time.time() - 48 * 3600
        os.utime(fp, (old, old))
        r = OR.reclaim(d, dry_run=True)
        self.assertTrue(os.path.isdir(target), '预演模式不能真的删')
        r2 = OR.reclaim(d, dry_run=False, min_age_hours=6.0)
        self.assertFalse(os.path.isdir(target), '超龄缓存在执行模式应被删除')
        self.assertGreater(r2['freed_bytes'], 0)


class TestReport(unittest.TestCase):
    def test_report_missing_dir_fails_open(self):
        r = OR.report(os.path.join(tempfile.gettempdir(), 'definitely_missing_out_xyz'))
        self.assertFalse(r['ok'])
        self.assertTrue(r['error'])

    def test_report_marks_manual_review(self):
        d = tempfile.mkdtemp(prefix='orrep_')
        os.makedirs(os.path.join(d, '10镜待判_某项目', 'a'), exist_ok=True)
        os.makedirs(os.path.join(d, 'projects', 'p1'), exist_ok=True)
        with open(os.path.join(d, 'projects', 'p1', 'v.mp4'), 'wb') as f:
            f.write(b'0' * 1024)
        r = OR.report(d)
        self.assertTrue(r['ok'], r.get('error'))
        self.assertIn('10镜待判_某项目', r['manual_review'])
        kinds = {i['name']: i['kind'] for i in r['items']}
        self.assertIn('勿删', kinds.get('projects', ''))

    def test_summary_never_raises(self):
        self.assertIsInstance(OR.summary_line('/definitely/not/here'), str)


if __name__ == '__main__':
    unittest.main()
