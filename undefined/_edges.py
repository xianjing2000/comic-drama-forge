
import ast, io, os, sys, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.join(os.getcwd(), "app")
def modname(p):
    rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
    return rel[:-3].replace("/", ".")
mods = {}
for r, d, fs in os.walk(ROOT):
    d[:] = [x for x in d if x not in ("__pycache__","static","templates","prompts")]
    for f in fs:
        if f.endswith(".py"):
            p = os.path.join(r,f); mods[modname(p)] = p
tops = {m.split(".")[0] for m in mods}
def resolve(name):
    if not name: return None
    parts = name.split(".")
    if parts[0] not in tops: return None
    if name in mods: return name
    for i in range(len(parts),0,-1):
        c = ".".join(parts[:i])
        if c in mods: return c
    return parts[0]
# build edges with nesting info
edges = collections.defaultdict(dict)  # src -> dst -> {"module":[...], "func":[...]}
for m,p in mods.items():
    src = io.open(p, encoding="utf-8", errors="replace").read()
    try: tree = ast.parse(src)
    except SyntaxError: continue
    topnodes = set()
    for node in ast.walk(tree):
        pass
    # determine module-level statements
    def visit(node, nested):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                targets = [a.name for a in child.names] if isinstance(child, ast.Import) else [((child.module or "")+"."+a.name) if child.module else a.name for a in child.names]
                for t in targets:
                    r = resolve(t)
                    if r and r != m:
                        key = "module" if not nested else "func"
                        edges[m].setdefault(r, {"module":[], "func":[]})[key].append(child.lineno)
                continue
            nxt = nested or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.Try, ast.With, ast.For, ast.While))
            visit(child, nxt)
    visit(tree, False)
import json
out = {}
for s in sorted(edges):
    out[s] = {d: v for d,v in edges[s].items()}
json.dump(out, io.open(os.path.join(os.environ.get("TEMP"), "_edges.json"), "w", encoding="utf-8"), ensure_ascii=False)
print("edges dumped", sum(len(v) for v in out.values()))

def find_cycles(edges):
    nodes = set(edges) | {t for v in edges.values() for t in v}
    index, low, onstk, stk, idx, sccs = {}, {}, {}, [], [0], []
    def strong(v):
        index[v]=low[v]=idx[0]; idx[0]+=1; stk.append(v); onstk[v]=True
        for w in edges.get(v, ()):
            if w not in index:
                strong(w); low[v]=min(low[v], low[w])
            elif onstk.get(w): low[v]=min(low[v], index[w])
        if low[v]==index[v]:
            comp=[]
            while True:
                w=stk.pop(); onstk[w]=False; comp.append(w)
                if w==v: break
            if len(comp)>1: sccs.append(sorted(comp))
    for v in sorted(nodes):
        if v not in index: strong(v)
    return sccs
sccs = find_cycles({s: set(d) for s,d in edges.items()})
for comp in sccs:
    print("=== SCC", comp)
    for a in comp:
        for b in sorted(edges.get(a,{})):
            if b in comp:
                info = edges[a][b]
                print("   %-24s -> %-24s top=%s func=%s" % (a, b, info["module"][:6], info["func"][:6]))
