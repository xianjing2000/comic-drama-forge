# -*- coding: utf-8 -*-
"""回归测试：模块名与单例名同名，导致的必然 AttributeError。

本项目有两条真实故障记录，同一个根因：

  1. routes/status_api.py —— 顶层 import comfyui_client 后调用
     comfyui_client.get_status()：日志里 64 次
     AttributeError: module 'comfyui_client' has no attribute 'get_status'
  2. routes/projects.py —— 同样写法，调用 generate_storyboard() /
     generate_scene_base()：「生成项目封面」100% 失败，只因该按钮从未被点过
     而长期沉默（2026-10-10 修）。

为什么容易犯：app.py 里有 comfyui_client = ComfyUIClient()，
from routes._shared import comfyui_client 拿到的是**实例**，
import comfyui_client 拿到的是**模块** —— 同名不同物，写错了只有运行时才炸。

本测试做纯静态检查（AST，不 import，无副作用）：
  对每个「顶层把 X 绑定成模块对象」的文件，检查 X.member 里的 member
  是否真存在于该模块顶层（def/class/赋值）；不存在即为必然 AttributeError。
"""
import ast
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, 'app')
_SHARED = os.path.join(_APP, 'routes', '_shared.py')


def _module_files():
    out = {}
    for dp, dn, fn in os.walk(_APP):
        dn[:] = [d for d in dn if d != '__pycache__']
        for f in fn:
            if f.endswith('.py'):
                rel = os.path.relpath(os.path.join(dp, f), _APP).replace(os.sep, '/')
                out[rel[:-3].replace('/', '.')] = os.path.join(dp, f)
    return out


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _top_names(path):
    """模块顶层可见名字（定义/赋值/导入）。"""
    names = set()
    tree = ast.parse(_read(path))
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add(a.asname or a.name.split('.')[0])
    return names


#: 既从 _shared.py 动态发现单例名，也补上这份固定名单 ——
#: 有些单例是带参构造（xxx = SomeClient(cfg)），正则抓不到，只靠动态发现会让
#: 扫描面缩到 1 处调用，测试看着通过其实等于空转（本测试首版就踩了这个坑）。
CANDIDATE_SINGLETONS = (
    'comfyui_client', 'llm_client', 'qc_client', 'tts_client', 'upscale_client',
    'project_store', 'secret_store', 'ai_config', 'comfyui_models', 'prompt_qc',
)


def _shared_singletons():
    """routes/_shared.py 里被实例化的单例名：形如 xxx = SomeClient()。"""
    src = _read(_SHARED)
    return set(re.findall(r'^(\w+)\s*=\s*\w+\(\)\s*$', src, re.M))


def _top_binding_kind(lines, base):
    """该文件顶层对 base 的最终绑定：'module' / 'other' / None。"""
    kind = None
    for l in lines:
        if re.match(r'^\s', l):
            continue                                   # 只看顶层，忽略函数内局部 import
        s = l.strip()
        if re.match(r'^import %s\s*$' % re.escape(base), s):
            kind = 'module'
        elif re.match(r'^import %s\s+as\s' % re.escape(base), s):
            kind = 'module'
        elif re.match(r'^%s\s*=' % re.escape(base), s):
            kind = 'other'
        elif re.search(r'from [\w.]+ import .*\b%s\b' % re.escape(base), s):
            kind = 'other'
    return kind


class TestModuleInstanceShadow(unittest.TestCase):
    def test_no_call_into_module_that_lacks_member(self):
        mods = _module_files()
        top = {m: _top_names(p) for m, p in mods.items()}
        singletons = _shared_singletons() | set(CANDIDATE_SINGLETONS)
        self.assertIn('comfyui_client', singletons,
                      '未识别出 comfyui_client 单例，测试前提失败')

        problems, ok_calls = [], 0
        for m, p in sorted(mods.items()):
            lines = _read(p).splitlines()
            for base in sorted(singletons):
                if base not in top or base == m:
                    continue
                if _top_binding_kind(lines, base) != 'module':
                    continue
                for i, l in enumerate(lines, 1):
                    if re.match(r'^\s*(?:#|from|import)', l):
                        continue
                    for mm in re.finditer(r'\b%s\.(\w+)\s*\(' % re.escape(base), l):
                        member = mm.group(1)
                        if member in top[base]:
                            ok_calls += 1
                        else:
                            problems.append('%s:%d  模块 %s 无成员 %s()'
                                            % (m.replace('.', '/') + '.py', i, base, member))

        # 本项目现状：合法调用上百处；过少说明扫描失效（防测试静默空转）
        self.assertGreater(ok_calls, 100, '未扫描到足够的模块级调用，检查逻辑可能失效')
        self.assertEqual(
            problems, [],
            '以下位置把「模块」当「实例」用，运行时必然 AttributeError：\n  '
            + '\n  '.join(problems))


if __name__ == '__main__':
    unittest.main()
