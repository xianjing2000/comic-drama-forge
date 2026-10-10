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

P = 'app/app.py'
src = io.open(P, encoding='utf-8').read()
lines = src.split(chr(10))
tree = ast.parse(src)

IMPORT_LINES = []
for n in tree.body:
    if isinstance(n, ast.ImportFrom):
        IMPORT_LINES.append(([a.asname or a.name for a in n.names],
                             chr(10).join(lines[n.lineno - 1:n.end_lineno])))
    elif isinstance(n, ast.Import):
        IMPORT_LINES.append(([(a.asname or a.name.split('.')[0]) for a in n.names],
                             chr(10).join(lines[n.lineno - 1:n.end_lineno])))


def import_stmt_for(name):
    for nm, txt in IMPORT_LINES:
        if name in nm:
            return txt
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

    body_txt = chr(10).join(chr(10).join(lines[n.lineno - 1:n.end_lineno]) for n, _, _ in hits)
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
        if not st:
            unresolved.append(r); continue
        if re.search(r'from\\s+app\\s+import', st) or st.strip() == 'import app':
            unresolved.append(r); continue
        imports.append(st)
    imports = sorted(set(imports))
    print('  引用外部名 %d ｜ 可复制 import %d ｜ 需人工处理 %d: %s'
          % (len(refs), len(imports), len(unresolved), unresolved))

    DOC = chr(39) * 3
    head = [
        '# -*- coding: utf-8 -*-',
        DOC + (note or modname) + ' API 蓝图（2026-10-11 从 app.py 迁出）。' + DOC,
        '',
        '# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；',
        '# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。',
        'from flask import Blueprint, jsonify, request, send_file, abort  # noqa: F401',
        'import os    # noqa: F401',
        'import json  # noqa: F401',
        'import time  # noqa: F401',
        'import uuid  # noqa: F401',
    ]
    head += imports
    head += ['', "%s_bp = Blueprint('%s', __name__)" % (modname, modname), '', '']

    blocks = []
    for n, url, d in hits:
        # ⚠️ 必须向前吞掉装饰器行：n.lineno-1 是 def 行，装饰器在它**上面**。
        #    首版没做这一步，导致搬出来的函数全都没有路由装饰器，
        #    蓝图注册了 0 条规则（url_map 少 9 条）。
        s0 = n.lineno - 1
        while s0 > 0 and lines[s0 - 1].startswith('@'):
            s0 -= 1
        seg = lines[s0:n.end_lineno]
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
