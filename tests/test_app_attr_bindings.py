# -*- coding: utf-8 -*-
"""回归测试：全项目通过 `A.X` / `_A().X` 对 app 模块的**延迟绑定**访问必须可解析。

背景（2026-10-11 真实故障，我造成的）：
  为清理拆分过程累积的 51 个冗余再导出块，我扫描了全项目的 `from app import`，
  确认无消费者后删除 —— 但**漏掉了本项目真正的延迟绑定写法**：
      A = _A()            # _A() 返回宿主 app 模块
      client = A._current_llm_client()
      ep_dir = _A()._ep_dir(...)
  这类访问共 **A. 53 个名字 + _A(). 5 个名字**，全部依赖 app.py 的再导出。
  删除后生产立即报：AttributeError: module 'app' has no attribute '_current_llm_client'

实现要点（前两版踩过的坑）：
  · 不能用正则扫 `app.X` —— 真实代码写的是 `A.X`，而 `app.X` 大多出现在**注释/docstring**
    里（如 chapter_llm.py 的说明文字），正则会大量误报；
  · 也不能只认 `app.` —— 会匹配到文件名 `app.py`；
  · 故用 **AST**：只取 ast.Attribute，且 value 为 Name('A') 或 Call(func=Name('_A'))。
  · AST 解析 app.py 顶层名字，**不 import**，避免启动托管线程等副作用。
"""
import ast
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, 'app')
_APP_PY = os.path.join(_APP, 'app.py')


def _top_level_names(path):
    """AST 解析模块顶层的可见名字（定义/赋值/导入/再导出），不执行任何代码。"""
    tree = ast.parse(open(path, encoding='utf-8').read())
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.asname or a.name.split('.')[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
    return out


def _delayed_bind_refs(path):
    """AST 收集真实代码中的 A.X 与 _A().X 访问（排除字符串/注释里的同形文字）。"""
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        v = node.value
        is_A = isinstance(v, ast.Name) and v.id == 'A'
        is_A_call = (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                     and v.func.id == '_A')
        if is_A or is_A_call:
            out.append((node.attr, node.lineno))
    return out


class TestAppDelayedBindings(unittest.TestCase):
    def test_all_A_dot_x_resolve(self):
        avail = _top_level_names(_APP_PY)
        missing, checked = [], 0
        for root, dirs, files in os.walk(_APP):
            if '__pycache__' in root:
                continue
            for f in sorted(files):
                if not f.endswith('.py'):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, _ROOT).replace(os.sep, '/')
                for name, lineno in _delayed_bind_refs(p):
                    checked += 1
                    if name not in avail:
                        missing.append('%s:%d  A.%s' % (rel, lineno, name))
        # 30 处以上是本项目现状；过少说明扫描失效（防止测试静默变空转）
        self.assertGreater(checked, 30, '未扫描到 A.X 延迟绑定，测试可能失效')
        self.assertEqual(
            missing, [],
            '以下 A.X / _A().X 延迟绑定在 app.py 顶层不存在（删除对应再导出会破坏生产）：\n  '
            + '\n  '.join(sorted(set(missing))))


if __name__ == '__main__':
    unittest.main()