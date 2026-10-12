# -*- coding: utf-8 -*-
"""统一数据隔离 helper 回归（t48）。

验证 tests/isolation_helpers.py 提供的统一隔离函数可用：
  - isolate_data_root() 幂等，返回临时目录；
  - 子进程先隔离再 import config，config 数据根落在临时目录（不是生产根）。
"""
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

sys.path.insert(0, _HERE)
import isolation_helpers as iso  # noqa: E402


class TestIsolationHelper(unittest.TestCase):
    def test_isolate_data_root_idempotent(self):
        """重复调用返回同一临时目录（幂等）。"""
        d1 = iso.isolate_data_root()
        d2 = iso.isolate_data_root()
        self.assertEqual(d1, d2)
        self.assertTrue(iso._is_tempish(d1), "数据根应落在系统临时目录：%s" % d1)

    def test_isolation_report(self):
        rep = iso.isolation_report()
        self.assertTrue(rep["is_temp"], "隔离报告 is_temp 应为 True：%s" % rep)

    def test_subprocess_isolates_before_import_config(self):
        """子进程：先 isolate_data_root 再 import config，config 数据根在临时目录。"""
        code = """import sys, os
sys.path.insert(0, os.environ["ISO_TESTS"])
import isolation_helpers as iso
iso.isolate_data_root()
sys.path.insert(0, os.environ["ISO_APP"])
import config
p = config.PROJECT_DATA_DIR or ""
print("TEMP" if iso._is_tempish(p) else "REAL:" + p)
"""
        env = dict(os.environ)
        env.pop("MJSCXT_DATA_DIR", None)
        env["ISO_TESTS"] = _HERE
        env["ISO_APP"] = os.path.join(_ROOT, "app")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(out.returncode, 0, "子进程执行失败：%s" % out.stderr)
        self.assertEqual(out.stdout.strip(), "TEMP",
                         "隔离后 config 数据根应落在临时目录：%s" % out.stdout)


if __name__ == "__main__":
    unittest.main()
