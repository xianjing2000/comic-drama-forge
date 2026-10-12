
import ast, io, os, sys, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.join(os.getcwd(), "app")
def modname(p):
    return os.path.relpath(p, ROOT).replace(os.sep,"/")[:-3].replace("/",".")
mods = {}
for r,d,fs in os.walk(ROOT):
    d[:] = [x for x in d if x not in ("__pycache__","static","templates","prompts")]
    for f in fs:
        if f.endswith(".py"):
            p=os.path.join(r,f); mods[modname(p)]=p
tops={m.split(".")[0] for m in mods}
def resolve(name):
    if not name: return None
    parts=name.split(".")
    if parts[0] not in tops: return None
    if name in mods: return name
    for i in range(len(parts),0,-1):
        c=".".join(parts[:i])
        if c in mods: return c
    return parts[0]
edges=collections.defaultdict(dict)
for m,p in mods.items():
    try: tree=ast.parse(io.open(p,encoding="utf-8",errors="replace").read())
    except SyntaxError: continue
    def walk(node, in_func):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch,(ast.Import,ast.ImportFrom)):
                tg=[a.name for a in ch.names] if isinstance(ch,ast.Import) else [((ch.module or "")+"."+a.name) if ch.module else a.name for a in ch.names]
                for t in tg:
                    r=resolve(t)
                    if r and r!=m:
                        edges[m].setdefault(r,{"module":[],"func":[]})["func" if in_func else "module"].append(ch.lineno)
                continue
            walk(ch, in_func or isinstance(ch,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)))
    walk(tree, False)
sccs = [["continuity","coverage","novel_to_script","script_consistency"],
        ["agent_core","autopilot","project_store"],["ai_config","ai_credentials_db"],
        ["prompt_enhance","prompt_qc"],["lesson_helpers","workers.audio"],["app","routes.keyframes"]]
for comp in sccs:
    print("SCC", comp)
    for a in comp:
        for b in sorted(edges.get(a,{})):
            if b in comp:
                i=edges[a][b]
                print("   %-22s -> %-22s module=%s func=%s" % (a,b,i["module"],i["func"]))
# total module-level edges inside any SCC counted
