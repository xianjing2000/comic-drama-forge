# -*- coding: utf-8 -*-
"""守卫脚本 fail-open 回归（t48）。

t9 审查发现 .workbuddy/test/verify_audio_qc.py 与 verify_h3_twopass.py 在
「缺失依赖 / 模板缺失 / 回退」时用 sys.exit(0) 退出 —— run_guards.py 以
returncode==0 判 PASS，于是环境缺失被当成「通过」（假绿）。

修复口径：守卫退出码 0=PASS、1=FAIL、2=SKIP（缺失依赖/环境不允许）。
本测试扫描 .workbuddy/test/ 下守卫脚本源码，断言「跳过/缺失/回退语境的
sys.exit(0)」已全部改为非 0，且 run_guards.py 区分 SKIP。
"""
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_GUARDS = os.path.join(_ROOT, ".workbuddy", "test")


def _skip_context_exit0(path):
    """扫描单个守卫脚本，返回「跳过/缺失语境下的 sys.exit(0)」行号清单。"""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    bad = []
    for i, line in enumerate(lines):
        if not re.search(r"sys\.exit\(0\)|exit\(0\)", line):
            continue
        ctx = "\n".join(lines[max(0, i - 12):i + 1])
        if re.search(r"跳过|SKIP|skip|不存在|不可用|无法|missing|not available|"
                     r"未找到|not found|回退|降级|退化|no ffmpeg", ctx):
            bad.append(i + 1)
    return bad


@unittest.skipUnless(os.path.isdir(_GUARDS), ".workbuddy/test 目录不存在，跳过")
class TestGuardFailOpen(unittest.TestCase):
    def test_skip_context_not_exit0(self):
        """任何守卫在「跳过/缺失/回退」语境都不得 exit(0)（假绿）。"""
        bad_files = {}
        for fn in sorted(os.listdir(_GUARDS)):
            if not fn.startswith("verify_") or not fn.endswith(".py") or ".bak" in fn:
                continue
            hits = _skip_context_exit0(os.path.join(_GUARDS, fn))
            if hits:
                bad_files[fn] = hits
        self.assertEqual(
            bad_files, {},
            "这些守卫在跳过语境仍 exit(0)（会被 run_guards 判 PASS 假绿）：%s" % bad_files)

    def test_run_guards_distinguishes_skip(self):
        """run_guards.py 必须区分 SKIP（exit 2）与 PASS（exit 0）。"""
        rg = os.path.join(_GUARDS, "run_guards.py")
        if not os.path.isfile(rg):
            self.skipTest("run_guards.py 不存在")
        with open(rg, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("SKIP", src, "run_guards.py 未识别 SKIP 码")
        self.assertIn("code == 2", src, "run_guards.py 未对 exit 2 做 SKIP 判定")


if __name__ == "__main__":
    unittest.main()
