# -*- coding: utf-8 -*-
"""回归测试：`pipeline` / `autopilot` 的**显式依赖 import** 必须可解析。

背景（2026-10-16，D-1「拆服务定位器」）：这两个模块原先经 `_A()`（`sys.modules['app']`）
延迟取宿主符号；现 `_A()` 已删除，改为一律**函数内显式 import**（来源由 app.py 的实际导入
锚定）。本测试静态校验：每个指向 `app/` 下模块的 `from MOD import ...` / `import MOD`，
其目标模块文件存在、且被导入的名字在该模块里能出现（防拼写错误 / 再导出漂移）。

实现要点：AST 解析，**不 import**（避免启动托管线程等副作用）；只检查 app/ 内的模块，
标准库/第三方自动跳过。
"""
import ast
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, 'app')
_TARGETS = [os.path.join(_APP, 'pipeline.py'), os.path.join(_APP, 'autopilot.py')]


def _imports(path):
    """收集文件内所有 import：返回 [(module, [names] 或 None, lineno)]。"""
    tree = ast.parse(open(path, encoding='utf-8').read())  # noqa: SIM115 (short-lived)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.append((n.module, [a.name for a in n.names], n.lineno))
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.append((a.name, None, n.lineno))
    return out


def _modfile(mod):
    rel = mod.replace('.', '/')
    for p in (os.path.join(_APP, rel + '.py'), os.path.join(_APP, rel, '__init__.py')):
        if os.path.isfile(p):
            return p
    return None


class TestExplicitDeps(unittest.TestCase):
    def test_explicit_imports_resolve(self):
        missing, checked = [], 0
        for path in _TARGETS:
            rel = os.path.relpath(path, _ROOT).replace(os.sep, '/')
            for mod, names, ln in _imports(path):
                f = _modfile(mod)
                if not f:
                    continue  # 标准库 / 第三方：不在 app/ 下，跳过
                checked += 1
                src = open(f, encoding='utf-8', errors='replace').read()  # noqa: SIM115
                for nm in (names or []):
                    if not re.search(r'\b' + re.escape(nm) + r'\b', src):
                        missing.append('%s:%d  from %s import %s' % (rel, ln, mod, nm))
        # app/ 内的显式依赖应远超此数；过少说明扫描失效（防静默空转）
        self.assertGreater(checked, 20, '未扫描到 app/ 内的显式 import，测试可能失效')
        self.assertEqual(
            missing, [],
            '以下显式依赖 import 无法解析（模块文件缺失或名字不在其中）：\n  '
            + '\n  '.join(missing))


if __name__ == '__main__':
    unittest.main()
