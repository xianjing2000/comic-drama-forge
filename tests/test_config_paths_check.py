# -*- coding: utf-8 -*-
"""路径类配置的运行时值自检 —— 回归测试。

背景（2026-10-11 真实事故）：
  config.py 的 MJSCXT_COMFYUI_DIR 自动检测成功，但 COMFYUI_ROOT 没有回退到它，
  于是 _DERIVED 全空、COMFYUI_OUTPUT_DIR=''，产物搬移的 src 退化成相对路径 →
  WinError 3 →「ComfyUI 出图完全正常，却逐个资产入库失败并被隔离」。
  当时 116 个测试 / 58 模块导入 / 243 路由**全绿**，没有任何检查发现它。

本测试锁住那道后来补上的防线：
  · 当前配置下，路径自检必须通过（无空值、无相对路径）；
  · 人为把路径造空 / 造成相对路径时，自检**必须**抓出来（反向验证）。
"""
import os
import sys
import unittest

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import config as C  # noqa: E402
import config_paths_check as PC  # noqa: E402


class TestPathsCheckCurrentState(unittest.TestCase):
    def test_current_config_has_no_empty_or_relative_paths(self):
        """当前配置下不允许存在空值/相对路径的路径常量。

        （空值 = 运行时必然退化成相对路径；相对路径 = 依赖进程 CWD。）
        """
        r = PC.check(C)
        self.assertGreater(r['total'], 20, '路径常量数量异常少，收集逻辑可能失效')
        self.assertEqual(r['empty'], [], '存在空值路径常量：%s' % r['empty'])
        self.assertEqual([x[0] for x in r['relative']], [],
                         '存在相对路径常量：%s' % [x[0] for x in r['relative']])


class TestPathsCheckDetectsFailure(unittest.TestCase):
    """反向验证：自检必须能抓到本次事故的两种形态。"""

    def setUp(self):
        self._saved = {k: getattr(C, k) for k in ('COMFYUI_OUTPUT_DIR', 'MJSCXT_COMFYUI_DIR')}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(C, k, v)

    def test_detects_empty_path(self):
        C.COMFYUI_OUTPUT_DIR = ''
        r = PC.check(C)
        self.assertIn('COMFYUI_OUTPUT_DIR', r['empty'],
                      '自检没能发现空路径 —— 这正是事故当时的形态')

    def test_detects_relative_path(self):
        C.MJSCXT_COMFYUI_DIR = 'relative/not/absolute'
        r = PC.check(C)
        names = [x[0] for x in r['relative']]
        self.assertIn('MJSCXT_COMFYUI_DIR', names,
                      '自检没能发现相对路径')

    def test_report_marks_not_ok(self):
        C.COMFYUI_OUTPUT_DIR = ''
        r = PC.startup_report(C)  # 不得抛异常
        self.assertFalse(r.get('ok'), '发现空值时 ok 应为 False')


if __name__ == '__main__':
    unittest.main()