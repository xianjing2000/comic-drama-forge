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

#: _app_logger 的定义位置 —— 2026-10-10 拆分第 1 步把它从 routes/_shared.py
#: 上移到了 app/shared_base.py（routes/_shared.py 只做再导出）。
#: 本测试跟随定义位置，避免「搬家即红」这种伪失败；同时另有用例守住再导出关系。
_CANDIDATES = [os.path.join(_APP, 'shared_base.py'),
               os.path.join(_APP, 'routes', '_shared.py')]


def _find_app_logger_def():
    """返回 (文件路径, FunctionDef)；两处都没有则 (None, None)。"""
    for path in _CANDIDATES:
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding='utf-8').read())
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.name == '_app_logger':
                return path, n
    return None, None


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
        path, target = _find_app_logger_def()
        self.assertIsNotNone(target, '未找到 _app_logger 定义（shared_base.py / routes/_shared.py）')
        calls = [c for c in ast.walk(target)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == '_app_logger']
        self.assertEqual(calls, [],
                         '%s 里的 _app_logger 不得调用自身（会递归到 RecursionError）'
                         % os.path.basename(path))

    def test_shared_reexports_instead_of_redefining(self):
        """拆分后 routes/_shared.py 只能**再导出** _app_logger，不得再定义一份。"""
        shared = os.path.join(_APP, 'routes', '_shared.py')
        tree = ast.parse(open(shared, encoding='utf-8').read())
        defined = [n.name for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name in
                   ('_app_logger', '_move_with_retry', '_trash_move')]
        self.assertEqual(defined, [],
                         '这些底层工具已上移到 shared_base.py，_shared.py 不得重复定义：%s' % defined)
        src = open(shared, encoding='utf-8').read()
        self.assertIn('from shared_base import', src, '_shared.py 必须从 shared_base 再导出')


if __name__ == '__main__':
    unittest.main()
