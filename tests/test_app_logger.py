# -*- coding: utf-8 -*-
"""_app_logger 的回归测试（上下文安全日志器）。

历史缺陷（2026-10-10 发现并修复）：
  实现写作

      try:
          return _app_logger()        # ← 递归调用自己
      except RuntimeError:
          return logging.getLogger("app")

  它会一路递归到 RecursionError，而 RecursionError 是 RuntimeError 的子类，
  于是每次都被 except 接住 —— **Flask 的 app.logger 从未生效过**，
  与 docstring 声明的意图（请求内保留 app.logger 的 handler/格式）完全相反。
  每次调用还要付一次约 1000 层栈展开 + 异常构造的代价。

本测试同时钉住行为与「不得再写成自调用」这条静态约束。
"""
import ast
import logging
import os
import sys
import unittest

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from flask import Flask  # noqa: E402

from routes._shared import _app_logger  # noqa: E402

_SHARED_PY = os.path.join(_APP, 'routes', '_shared.py')


class TestAppLoggerBehaviour(unittest.TestCase):
    def test_no_app_context_falls_back_to_stdlib(self):
        lg = _app_logger()
        self.assertIsInstance(lg, logging.Logger)
        self.assertEqual(lg.name, 'app')

    def test_inside_app_context_returns_flask_logger(self):
        app = Flask('t')
        with app.app_context():
            self.assertIs(_app_logger(), app.logger,
                          '有上下文时必须返回 Flask 的 app.logger（否则 handler/格式丢失）')

    def test_repeated_calls_are_cheap_and_stable(self):
        """自调用版每次要递归到 RecursionError；修好后应当立即返回同一对象。"""
        a = _app_logger()
        b = _app_logger()
        self.assertIs(a, b)


class TestNoSelfRecursionInSource(unittest.TestCase):
    def test_app_logger_body_does_not_call_itself(self):
        tree = ast.parse(open(_SHARED_PY, encoding='utf-8').read())
        target = None
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.name == '_app_logger':
                target = n
                break
        self.assertIsNotNone(target, '未找到 _app_logger 定义')
        calls = [c for c in ast.walk(target)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == '_app_logger']
        self.assertEqual(calls, [], '_app_logger 不得调用自身（会递归到 RecursionError）')


if __name__ == '__main__':
    unittest.main()
