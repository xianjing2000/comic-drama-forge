# -*- coding: utf-8 -*-
"""路由下沉工具：把 app.py 里某个 URL 前缀下的路由搬进蓝图。

用法：python tools/sink_routes.py <URL前缀> <蓝图模块名> ["说明"]

安全设计（沿用 sink_helpers 的教训）：
  · 只搬 URL 以指定前缀开头的路由；@app.route 换成 @<name>_bp.route，URL/methods 不改；
  · import 从 app.py 的原始语句复制（不猜来源），剔除 from app import 这类反向依赖；
  · 生成后 ast.parse，失败不落盘；app.py 删除后再次校验；
  · 硬断言：不得搬迁 index / static_assets（历史事故）。
"""
import io
import ast
import re
import sys
import os

P = 'app/app.py'
src = io.open(P, encoding='utf-8').read()
lines = src.split(chr(10))
tree = ast.parse(src)

# ---- 建立「名字 -> 来源模块」索引（不依赖 app.py 的再导出语句）----
def _top_names(path):
    out = set()
    try:
        t = ast.parse(io.open(path, encoding='utf-8').read())
    except Exception:
        return out
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for x in n.targets:
                if isinstance(x, ast.Name):
                    out.add(x.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out

# routes/_shared.py 的导出（最优先）
SHARED = _top_names('app/routes/_shared.py')
# 各 helper 模块
HELPER_ORIGIN = {}
for f in sorted(os.listdir('app')):
    if not f.endswith('.py') or '.bak' in f:
        continue
    mod = f[:-3]
    if mod in ('app', 'serve', 'config'):
        continue
    for nm in _top_names(os.path.join('app', f)):
        HELPER_ORIGIN.setdefault(nm, mod)
# workers
for sub in ('audio', 'episodes', 'screenplay'):
    p = 'app/workers/%s.py' % sub
    if os.path.isfile(p):
        for nm in _top_names(p):
            HELPER_ORIGIN.setdefault(nm, 'workers.%s' % sub)
# config 常量
CONF = _top_names('app/config.py')

STDLIB = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else set()


# 生成的蓝图头部已 import 的名字（无需再解析）
BUILTIN_OK = {
    'BP',  # refs 计算时 @app.route( 的占位符
    'Blueprint', 'jsonify', 'request', 'send_file', 'abort', 'render_template',
    'redirect', 'send_from_directory', 'Response', 'make_response', 'url_for',
    'session', 'g', 'current_app', 'Flask',
    'os', 'sys', 'json', 'time', 'uuid', 'threading', 're', 'io', 'hashlib',
    'shutil', 'random', 'base64', 'subprocess', 'datetime', 'pathlib',
    'collections', 'math', 'glob', 'tempfile', 'copy', 'traceback', 'logging',
    'functools', 'itertools', 'typing', 'zipfile', 'csv', 'struct', 'socket',
    'textwrap', 'string', 'platform', 'signal', 'urllib', 'asyncio',
}


def _module_exists(name):
    """app/<name>.py 或 app/<pkg>/<name>.py 是否存在。"""
    if os.path.isfile('app/%s.py' % name):
        return name
    for pkg in ('workers',):
        if os.path.isfile('app/%s/%s.py' % (pkg, name)):
            return '%s.%s' % (pkg, name)
    return None


# app.py 真正的 import 语句（只扫前 400 行，排除后来追加的再导出区）
RCEXPORT_SKIP = 400

def import_stmt_for(name):
    """返回能提供 name 的 import 语句（按优先级，不猜）。"""
    if name in BUILTIN_OK:
        return None
    m = _module_exists(name)
    if m:
        return 'import %s' % m if m == name else 'from %s import %s' % (m.split('.')[0], name)
    if name in SHARED:
        return 'from routes._shared import %s' % name
    if name in HELPER_ORIGIN:
        return 'from %s import %s' % (HELPER_ORIGIN[name], name)
    if name in CONF:
        return 'from config import %s' % name
    return None


def route_of(n):
    for d in n.decorator_list:
        if isinstance(d, ast.Call) and getattr(d.func, 'attr', '') == 'route':
            if d.args and isinstance(d.args[0], ast.Constant):
                return d
    return None


def main(prefix, modname, note=''):
    hits = []
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        d = route_of(n)
        if d is None:
            continue
        url = d.args[0].value
        if url.startswith(prefix):
            hits.append((n, url, d))
    print('  匹配 %s* 的路由 = %d 个' % (prefix, len(hits)))
    if not hits:
        print('  [XX] 无匹配'); return 1
    names = [h[0].name for h in hits]
    if any(x in ('index', 'static_assets') for x in names):
        print('  [XX] 匹配到受保护视图 —— 中止'); return 1

    # 必须含装饰器行：@_autopilot_guard 这类名字只在装饰器里出现，
    # 若从 def 行开始取，refs 会漏掉它们（曾导致生成的蓝图未导入 _autopilot_guard）
    def _block(n):
        s0 = n.lineno - 1
        while s0 > 0 and lines[s0 - 1].startswith('@'):
            s0 -= 1
        return chr(10).join(lines[s0:n.end_lineno])
    body_txt = chr(10).join(_block(n) for n, _, _ in hits)
    # refs 必须基于**替换后**的文本计算：app.logger / app.test_request_context 会被
    # 换成 logger / current_app.*，若按原文算会把 'app' 误判为未解析名字。
    body_txt = body_txt.replace('app.logger', 'logger')
    body_txt = body_txt.replace('app.test_request_context', 'current_app.test_request_context')
    body_txt = body_txt.replace('app.app_context()', 'current_app.app_context()')
    body_txt = body_txt.replace('@app.route(', '@BP.route(')
    defined = set()
    for x in ast.walk(ast.parse(body_txt)):
        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
            defined.add(x.id)
        elif isinstance(x, ast.arg):
            defined.add(x.arg)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(x.name)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            defined.add(x.name)
    import builtins
    refs = sorted({x.id for x in ast.walk(ast.parse(body_txt))
                   if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
                   and x.id not in defined and x.id not in dir(builtins)
                   and not x.id.startswith('__')})

    imports, unresolved = [], []
    for r in refs:
        st = import_stmt_for(r)
        if r in BUILTIN_OK:
            continue
        if not st:
            unresolved.append(r); continue
        if re.search(r'from\\s+app\\s+import', st) or st.strip() == 'import app':
            unresolved.append(r); continue
        imports.append(st)
    imports = sorted(set(imports))
    print('  引用外部名 %d ｜ 可复制 import %d ｜ 需人工处理 %d: %s'
          % (len(refs), len(imports), len(unresolved), unresolved))

    # ⚠️ 硬约束：有未定位名字就**中止**，不生成蓝图也不改 app.py。
    #    首版没拦，导致 routes/video_api.py 里 _autopilot_guard 未定义，
    #    app.py import 直接 NameError（服务起不来）。
    if unresolved:
        print('  [XX] 有 %d 个名字无法自动解析：%s' % (len(unresolved), unresolved))
        print('       请先下沉它们或手工处理，本批中止（未修改任何文件）')
        return 1

    DOC = chr(39) * 3
    head = [
        '# -*- coding: utf-8 -*-',
        DOC + (note or modname) + ' API 蓝图（2026-10-11 从 app.py 迁出）。' + DOC,
        '',
        '# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；',
        '# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。',
        'import logging',
        'from flask import Blueprint, jsonify, request, send_file, abort, current_app  # noqa: F401',
        'import os    # noqa: F401',
        "import sys   # noqa: F401",
        "import re    # noqa: F401",
        "import time  # noqa: F401",
        "import uuid  # noqa: F401",
        "import threading   # noqa: F401",
        "import hashlib     # noqa: F401",
        "import shutil      # noqa: F401",
        "import random      # noqa: F401",
        "import base64      # noqa: F401",
        "import datetime    # noqa: F401",
        "import traceback   # noqa: F401",
        "import subprocess  # noqa: F401",
        'import json  # noqa: F401',
        'import time  # noqa: F401',
        'import uuid  # noqa: F401',
    ]
    head += imports
    head += ['', 'logger = logging.getLogger(__name__)', '',
             "%s_bp = Blueprint('%s', __name__)" % (modname, modname), '', '']

    blocks = []
    for n, url, d in hits:
        # ⚠️ 必须向前吞掉装饰器行：n.lineno-1 是 def 行，装饰器在它**上面**。
        #    首版没做这一步，导致搬出来的函数全都没有路由装饰器，
        #    蓝图注册了 0 条规则（url_map 少 9 条）。
        s0 = n.lineno - 1
        while s0 > 0 and lines[s0 - 1].startswith('@'):
            s0 -= 1
        seg = lines[s0:n.end_lineno]
        # app.logger -> logger（蓝图模块自带 logger，避免依赖 app 实例）
        seg = [s.replace('app.logger', 'logger') for s in seg]
        seg = [s.replace('app.test_request_context', 'current_app.test_request_context') for s in seg]
        seg = [s.replace('app.app_context()', 'current_app.app_context()') for s in seg]
        b = [s.replace('@app.route(', "@%s_bp.route(" % modname) for s in seg]
        assert any('_bp.route(' in x for x in b), 'block %s 未包含蓝图装饰器' % n.name
        blocks.append(chr(10).join(b))
    mod = chr(10).join(head) + (chr(10) + chr(10) + chr(10)).join(blocks) + chr(10)

    path = 'app/routes/%s.py' % modname
    try:
        ast.parse(mod)
    except SyntaxError as e:
        print('  [XX] 蓝图语法错 L%s %s' % (e.lineno, e.msg))
        L = mod.split(chr(10))
        for x in range(max(0, e.lineno - 3), min(len(L), e.lineno + 2)):
            print('    %5d| %s' % (x + 1, L[x][:110]))
        return 1
    io.open(path, 'w', encoding='utf-8').write(mod)
    print('  [OK] %s（%d 行）' % (path, mod.count(chr(10)) + 1))

    spans2 = []
    for n, _, _ in hits:
        s = n.lineno - 1
        while s > 0 and lines[s - 1].startswith('@'):
            s -= 1
        spans2.append((s, n.end_lineno))
    for s, e in sorted(set(spans2), reverse=True):
        del lines[s:e]

    ins = max(i for i, l in enumerate(lines) if 'register_blueprint' in l) + 1
    rex = ['', '# 2026-10-11 路由拆分：%s 已迁至 routes/%s.py。' % (note or prefix, modname),
           'from routes.%s import %s_bp  # noqa: E402' % (modname, modname),
           'app.register_blueprint(%s_bp)' % modname,
           'from routes.%s import (' % modname]
    PER = 3
    for i in range(0, len(names), PER):
        ch = names[i:i + PER]
        last = (i + PER >= len(names))
        rex.append('    ' + ', '.join(ch) + ('' if last else ',') + (')  # noqa: F401' if last else ''))
    for i, l in enumerate(rex):
        lines.insert(ins + i, l)
    s2 = chr(10).join(lines)
    try:
        ast.parse(s2)
    except SyntaxError as e:
        print('  [XX] app.py 语法错 L%s %s（未落盘）' % (e.lineno, e.msg))
        return 1
    io.open(P, 'w', encoding='utf-8').write(s2)
    print('  [OK] app.py 剩 %d 行' % len(lines))
    if unresolved:
        print('  ⚠️ 仍需人工确认的名字: %s' % unresolved)
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ''))
