# -*- coding: utf-8 -*-
"""冒烟测试：所有业务模块可导入，且每个 raise 的异常类都能解析（2026-10-10 建立）。

为什么建：曾引入一处真实缺陷 —— novel_to_script.py 写了
    raise NeedsHumanError(...)
而 NeedsHumanError 定义在 pipeline.py（本模块无法导入它，会循环依赖），
运行时抛 NameError：
    WARNING:autopilot:[集间流水线] 下一集剧本预热失败：NameError: name 'NeedsHumanError' is not defined
当时的验证只 grep 了这个词"文件里出现过"，没验证"能不能真解析到"。
这个测试用 ast 遍历所有 raise，逐一确认异常类在模块命名空间里存在 ——
同类错误以后再犯会立刻被拦下。

⚠️ 2026-10-12（t48）：_MODULES 由手工清单改为**自动收集** app/*.py（排除入口/私有），
新模块自动纳入冒烟范围，不再手工维护清单。
运行：python -m unittest discover -s tests -v
"""
import ast
import builtins
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
sys.path.insert(0, _APP)
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")  # 防止 import autopilot 拉起托管线程

#: 内置异常名（含 PermissionError / FileNotFoundError 等 OSError 子类）。
#: 用 builtins 动态枚举，避免「新模块 raise 某个标准内置异常」时漏登记而误报。
_BUILTIN_OK = set(dir(builtins))

#: 自动收集 app/ 顶层模块；排除入口/WSGI 服务器（import 会启动服务）与私有（_ 开头）。
_AUTO_EXCLUDE = {"app", "serve"}


def _auto_modules():
    """返回 app/ 顶层 .py 模块名（排除了入口与私有模块）。"""
    mods = []
    for fn in sorted(os.listdir(_APP)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        name = fn[:-3]
        if name in _AUTO_EXCLUDE:
            continue
        mods.append(name)
    return mods


_MODULES = _auto_modules()


def _unresolvable_raises(module_name):
    """返回该模块里『raise 的异常类无法解析』的清单 [(名字, 行号)]。"""
    path = os.path.join(_APP, module_name + '.py')
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    mod = __import__(module_name)
    ns = set(dir(mod))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        # 只校验**类名**（约定首字母大写）。raise first_err / raise last 这类
        # 是保存的异常**变量**（小写），属于正常写法，不应误报。
        if (isinstance(exc, ast.Name) and exc.id[:1].isupper()
                and exc.id not in ns and exc.id not in _BUILTIN_OK):
            bad.append((exc.id, node.lineno))
        # raise SomeModule.Error(...) 形式：只校验顶层名
        elif isinstance(exc, ast.Attribute):
            base = exc
            while isinstance(base, ast.Attribute):
                base = base.value
            if base.id[:1].isupper() and base.id not in ns and base.id not in _BUILTIN_OK:
                bad.append((base.id, node.lineno))
    return bad


class TestSmoke(unittest.TestCase):
    def test_modules_importable(self):
        for name in _MODULES:
            try:
                __import__(name)
            except Exception as e:  # noqa: BLE001
                self.fail('模块 %s 导入失败：%s: %s' % (name, type(e).__name__, e))

    def test_all_raises_resolvable(self):
        problems = {}
        for name in _MODULES:
            bad = _unresolvable_raises(name)
            if bad:
                problems[name] = bad
        self.assertEqual(problems, {},
                         '存在无法解析的 raise 异常类（运行时会 NameError）：%s' % problems)


if __name__ == '__main__':
    unittest.main()
