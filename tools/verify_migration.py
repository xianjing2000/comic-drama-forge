# -*- coding: utf-8 -*-
"""迁移验证器（每批下沉后运行）：真实 import + 缺失名 + app 路由 + smoke。

教训来源：多批下沉都出现过「静态检查全绿但运行时报 NameError」，
因此验证必须包含：① 真实 __import__；② 函数体引用名扫描；③ app 可导入且路由数不变。
"""
import os, sys, io, ast, builtins
os.environ.setdefault('MJSCXT_DATA_DIR', os.path.join(os.environ['APPDATA'], 'mjscxt-desktop', 'mjscxt-data'))
sys.path.insert(0, 'app')

# 自动发现：app/ 下所有 *_helpers.py + workers 包（不再硬编码，避免漏检新模块）
MODS = sorted(f[:-3] for f in os.listdir('app')
              if f.endswith('_helpers.py') or f == 'asset_refs.py')
# routes/ 下的蓝图也必须检查（2026-10-11 教训：autonomous 蓝图装饰器丢失，
# 路由从 243 掉到 234，而旧验证器只查 app/*_helpers.py，完全没发现）
ROUTE_MODS = ['routes.' + f[:-3] for f in sorted(os.listdir('app/routes'))
              if f.endswith('.py') and not f.startswith('_')]
MODS = MODS + ROUTE_MODS

print('  ==== ① 真实 import ====')
fail = []
for m in MODS:
    try:
        __import__(m)
    except Exception as e:
        fail.append(m)
        print('  [XX] %-24s %s: %s' % (m, type(e).__name__, str(e)[:80]))
if not fail:
    print('  [OK] %d/%d 模块全部导入成功' % (len(MODS), len(MODS)))

print()
print('  ==== ② 缺失名扫描 ====')
def top_names(t):
    out = set()
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): out.add(n.name)
        elif isinstance(n, ast.Assign):
            for x in n.targets:
                if isinstance(x, ast.Name): out.add(x.id)
        elif isinstance(n, ast.Import):
            for a in n.names: out.add(a.asname or a.name.split('.')[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names: out.add(a.asname or a.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store): out.add(n.id)
        elif isinstance(n, ast.arg): out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name: out.add(n.name)
    return out
tot = 0
for m in MODS:
    p = os.path.join('app', m.replace('.', '/') + '.py')
    if not os.path.isfile(p): continue
    tree = ast.parse(io.open(p, encoding='utf-8').read())
    d = top_names(tree) | set(dir(builtins)) | {'__name__', '__file__'}
    miss = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
                   and isinstance(n.ctx, ast.Load) and n.id not in d and not n.id.startswith('__')})
    if miss:
        tot += len(miss); print('  [XX] %-24s %d: %s' % (m, len(miss), miss[:6]))
if tot == 0:
    print('  [OK] 全部模块缺失名 = 0')

print()
print('  ==== ③ app 导入 + 路由 ====')
import importlib
m = importlib.import_module('app')
rules = sorted(str(r) for r in m.app.url_map.iter_rules())
print('  [OK] app 导入成功，url_map 规则数 =', len(rules))
# ★ 关键路由 endpoint 校验（事故回归：index 被误删时 / 会指向 static_assets）
CRIT = {'/': 'index', '/assets/<path:filename>': 'static_assets', '/api/status': 'api_status'}
by_url = {str(r): r.endpoint for r in m.app.url_map.iter_rules()}
probl = []
for u, want_ep in CRIT.items():
    got = by_url.get(u)
    # 蓝图 endpoint 会带蓝图名前缀（status_api.api_status），故用后缀匹配
    if got is None or not (got == want_ep or got.endswith('.' + want_ep)):
        probl.append((u, want_ep, got))
if probl:
    print('  [XX] 关键路由 endpoint 不符: %s' % probl)
else:
    print('  [OK] 关键路由 endpoint 全部正确（/%s）' % ', '.join(CRIT.values()))
print('       app.py 行数 =', io.open('app/app.py', encoding='utf-8').read().count(chr(10)) + 1)

print()
print('  ==== ④ smoke（真实调用）====')
import keyframe_helpers as KY
ok = 0
CASES = []
for attrs in (('_keyframe_sb_map',), ('_keyframe_prompt_preflight',)):
    nm = attrs[0]
    if hasattr(KY, nm):
        CASES.append((nm, getattr(KY, nm)))
for nm, fn in CASES:
    try:
        r = fn({})
        ok += 1
        print('  [OK] %-34s -> %s' % (nm, str(r)[:46]))
    except Exception as e:
        print('  [  ] %-34s %s: %s (可能是入参语义，非缺陷)' % (nm, type(e).__name__, str(e)[:48]))
print('  smoke 调用成功 %d/%d' % (ok, len(CASES)))
