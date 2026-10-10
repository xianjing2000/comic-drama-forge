# -*- coding: utf-8 -*-
"""校验「三步法搬迁」生成的转发函数是否安全。

背景（2026-10-11 实际发现）：把路由从 app.py 搬到 routes/*.py 时，生成器不区分符号类型，
一律产出：

    def _STATIC_DIR(*_a, **_kw):
        """该函数是 app.py 里的路由视图…"""
        import app as _root_app
        return getattr(_root_app, '_STATIC_DIR')(*_a, **_kw)

但如果 app 侧那个名字其实是**常量**（dict/tuple/str）或**根本不存在**，那么这个「转发函数」
既遮蔽了原名，又会在被调用时抛 TypeError / AttributeError —— 而且因为无人调用，
静态检查与冒烟测试都发现不了，属于典型的「埋雷」。

本工具逐个核对：转发目标必须**存在且可调用**。
退出码：0 = 全部安全；1 = 发现常量包装或目标缺失。
"""
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_APP = os.path.join(_ROOT, 'app')


def scan(app_module, routes_dir):
    problems = []
    checked = 0
    for root, _dirs, files in os.walk(routes_dir):
        if '__pycache__' in root:
            continue
        for f in sorted(files):
            if not f.endswith('.py'):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, _APP).replace(os.sep, '/')
            src = io.open(p, encoding='utf-8').read()
            for nm in re.findall(r'^def (\w+)\(\*_a, \*\*_kw\):', src, re.M):
                checked += 1
                v = getattr(app_module, nm, None)
                if v is None:
                    problems.append((rel, nm, '目标在 app 侧不存在'))
                elif not callable(v):
                    problems.append((rel, nm, '目标是 %s（常量不能当函数调用）' % type(v).__name__))
    return checked, problems


def main():
    sys.path.insert(0, _APP)
    os.environ.setdefault('MJSCXT_DATA_DIR',
                          os.path.join(os.environ.get('APPDATA', ''), 'mjscxt-desktop', 'mjscxt-data'))
    import importlib
    app_module = importlib.import_module('app')
    checked, problems = scan(app_module, os.path.join(_APP, 'routes'))
    print('  转发函数共 %d 个' % checked)
    if problems:
        print('  [XX] 发现 %d 个不安全转发：' % len(problems))
        for rel, nm, why in problems:
            print('       %-30s %-28s %s' % (rel, nm, why))
        return 1
    print('  [OK] 全部转发的目标都存在且可调用')
    return 0


if __name__ == '__main__':
    sys.exit(main())