
import io, os, ast, sys, collections, json
sys.stdout.reconfigure(encoding='utf-8')
APP = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\app'

def modname(path, root):
    rel = os.path.relpath(path, root).replace(os.sep, '/')
    return rel[:-3].replace('/', '.')

def scan(root):
    mods = {}
    for r, d, fs in os.walk(root):
        d[:] = [x for x in d if x not in ('__pycache__', 'static', 'templates', 'prompts')]
        for f in fs:
            if f.endswith('.py'):
                p = os.path.join(r, f)
                mods[modname(p, root)] = p
    return mods

mods = scan(APP)
# 顶层模块名集合（用于解析 import x / from x import y）
TOPS = set()
for m in mods:
    TOPS.add(m.split('.')[0])

def resolve(name):
    """把 import 名解析成 app 内模块（返回模块名或 None）"""
    if not name:
        return None
    parts = name.split('.')
    if parts[0] not in TOPS:
        return None
    # 精确命中
    if name in mods:
        return name
    # from app.xxx import y 之类
    for i in range(len(parts), 0, -1):
        cand = '.'.join(parts[:i])
        if cand in mods:
            return cand
    return '.'.join(parts[:2]) if len(parts) > 1 and '.'.join(parts[:2]) in mods else parts[0]

edges = collections.defaultdict(set)   # src -> {dst}
top_edges = collections.defaultdict(set)
lineno = collections.defaultdict(list)  # (src,dst) -> [行号]
for m, p in mods.items():
    try:
        tree = ast.parse(io.open(p, encoding='utf-8', errors='replace').read())
    except Exception as e:
        print('PARSE ERR', m, e); continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                t = resolve(a.name)
                if t and t != m:
                    edges[m].add(t); lineno[(m, t)].append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # 相对导入
            base = node.module or ''
            for a in node.names:
                t = resolve((base + '.' + a.name) if base else a.name)
                if t and t != m:
                    edges[m].add(t); lineno[(m, t)].append(node.lineno)
print('模块数 =', len(mods), '含内部依赖的边数 =', sum(len(v) for v in edges.values()))
# 保存供后续步骤使用
json.dump({'edges': {k: sorted(v) for k, v in edges.items()},
           'lineno': {'%s->%s' % k: v for k, v in lineno.items()}},
          io.open(r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\_t12_graph.json', 'w', encoding='utf-8'),
          ensure_ascii=False)
# 目录标签
def tag(m):
    return m.split('.')[0] if '.' in m else ('APP' if m in ('app',) else 'TOP')
print()
print('=== 目录 × 目录 的依赖矩阵（行=源，列=目标，数字=边数） ===')
dirs = collections.Counter(tag(m) for m in mods)
labels = [d for d, _ in dirs.most_common()]
mat = collections.Counter()
for s, ts in edges.items():
    for t in ts:
        mat[(tag(s), tag(t))] += 1
hdr = 'src\\dst'.ljust(12) + ''.join(l[:10].rjust(12) for l in labels)
print(hdr)
for s in labels:
    line = s[:10].ljust(12)
    for t in labels:
        line += str(mat.get((s, t), 0)).rjust(12)
    print(line)

