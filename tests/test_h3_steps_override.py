# -*- coding: utf-8 -*-
"""H3 采样步数覆盖的回归测试（B-2 代码侧，2026-10-16）。

`MJSCXT_H3_STEPS` 是「少步蒸馏加速」A/B 的开关：>0 时构建器覆写 Director 的 steps
widget；否则不改（用模板值）。本测试钉住「只认正整数、其余一律不改」这一契约 ——
它直接决定 A/B 是否安全（设错值绝不能悄悄改掉全量出片口径）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import h3_director_builder as B  # noqa: E402


class TestH3StepsOverride(unittest.TestCase):
    def _set(self, val):
        os.environ["MJSCXT_H3_STEPS"] = val
        self.addCleanup(lambda: os.environ.pop("MJSCXT_H3_STEPS", None))

    def test_default_off(self):
        os.environ.pop("MJSCXT_H3_STEPS", None)
        self.assertEqual(B._h3_steps_override(), 0, "未设环境变量必须不改步数")

    def test_positive_int(self):
        self._set("8")
        self.assertEqual(B._h3_steps_override(), 8)

    def test_zero_means_off(self):
        self._set("0")
        self.assertEqual(B._h3_steps_override(), 0, "0 = 不改")

    def test_invalid_is_off(self):
        for bad in ("abc", "", " ", "-3", "1e", "None"):
            self._set(bad)
            self.assertEqual(B._h3_steps_override(), 0, "'%s' 应视为不改" % bad)

    def test_float_string_is_int(self):
        self._set("6.0")
        self.assertEqual(B._h3_steps_override(), 6)

    def test_wired_into_build(self):
        """结构断言：build() 内必须存在 steps 覆盖的写入点（防日后被误删）。"""
        src = open(os.path.join(os.path.dirname(B.__file__), "h3_director_builder.py"),
                   encoding="utf-8").read()
        self.assertIn('_h3_steps_override()', src)
        self.assertIn('_DIRECTOR_POS, "steps"', src)


if __name__ == "__main__":
    unittest.main()
