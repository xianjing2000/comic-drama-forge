
import ast, io, os, sys, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.join(os.getcwd(), "app", "routes")
hits = []
def is_path_expr(v):
    if isinstance(v, ast.Call):
        f = v.func
        if isinstance(f, ast.Attribute) and f.attr in ("join", "abspath", "normpath", "realpath"):
            if isinstance(f.value, ast.Name) and f.value.id == "os":
                return "os.path." + f.attr
        if isinstance(f, ast.Name) and f.id in ("_p", "_path", "p"):
            return f.id + "()"
    if isinstance(v, ast.Name) and (v.id.endswith("_path") or v.id.endswith("_dir") or v.id.endswith("_dirpath")):
        return v.id
    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.attr in ("path", "dir", "file"):
        return v.value.id + "." + v.attr
    return None
for fn in sorted(os.listdir(ROOT)):
    if not fn.endswith(".py"): continue
    p = os.path.join(ROOT, fn)
    try: tree = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
    except SyntaxError: continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if not isinstance(k, ast.Constant) or not isinstance(k.value, str): continue
                kind = is_path_expr(v)
                if kind:
                    hits.append((fn, node.lineno, k.value, kind))
print("dict-literal path-ish exposures in routes/:", len(hits))
agg = collections.Counter(h[2] for h in hits)
for k, n in agg.most_common(25):
    print("   key %-18s x%d" % (k, n))
byfile = collections.Counter(h[0] for h in hits)
for f, n in byfile.most_common(20):
    print("   file %-26s x%d" % (f, n))
for h in hits[:30]:
    print("   %s:%d  %s <- %s" % h)
