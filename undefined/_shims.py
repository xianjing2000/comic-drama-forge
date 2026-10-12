
import ast, io, os, sys, re, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.join(os.getcwd(), "app")
src = io.open(os.path.join(ROOT,"routes","keyframes.py"), encoding="utf-8", errors="replace").read()
tree = ast.parse(src)
names = []
for n in tree.body:
    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
        body = n.body
        s = "".join(io.open(os.path.join(ROOT,"routes","keyframes.py"),encoding="utf-8",errors="replace").read().splitlines(True)[n.lineno-1:n.end_lineno])
        if "getattr(_root_app" in s:
            m = re.search(r"getattr\(_root_app, '([^']+)'\)", s)
            names.append((n.name, m.group(1), n.lineno))
print("forwarders:", len(names))
# find defining modules for each target name
defs = collections.defaultdict(list)
for r,d,fs in os.walk(ROOT):
    d[:] = [x for x in d if x not in ("__pycache__","static","templates","prompts")]
    for f in fs:
        if not f.endswith(".py"): continue
        p = os.path.join(r,f)
        rel = os.path.relpath(p, ROOT).replace(os.sep,"/")[:-3].replace("/",".")
        try: t = ast.parse(io.open(p,encoding="utf-8",errors="replace").read())
        except SyntaxError: continue
        for node in t.body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                defs[node.name].append(rel)
            if isinstance(node, ast.Assign):
                for tg in node.targets:
                    if isinstance(tg, ast.Name): defs[tg.id].append(rel)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defs[node.target.id].append(rel)
for nm, tgt, ln in names:
    print("%-34s L%-4d -> app.%-30s defs=%s" % (nm, ln, tgt, defs.get(tgt, ["?"])))
