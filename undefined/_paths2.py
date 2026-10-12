
import ast, io, os, sys, re, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.join(os.getcwd(), "app", "routes")
KEY_RE = re.compile(r"(^|_)(path|dir|dirpath|folder|file|filename|target|source|input|output)$|_path$|_dir$|_file$")
def txt(node):
    try: return ast.unparse(node)
    except Exception: return "<?>"
hits = []
for fn in sorted(os.listdir(ROOT)):
    if not fn.endswith(".py"): continue
    p = os.path.join(ROOT, fn)
    try: tree = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
    except SyntaxError: continue
    # any Call to os.path.* / abspath / str(pathvar)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = ""
            if isinstance(f, ast.Attribute):
                name = (getattr(f.value, "id", "") or "") + "." + f.attr
            elif isinstance(f, ast.Name):
                name = f.id
            if name in ("os.path.join", "os.path.abspath", "os.path.realpath", "os.path.normpath") or f.__class__ is ast.Name and f.id.endswith("_path"):
                hits.append((fn, node.lineno, "expr", name))
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and KEY_RE.search(k.value):
                    hits.append((fn, node.lineno, k.value, txt(v)[:60]))
print("broad hits:", len(hits))
agg = collections.Counter(h[2] for h in hits if h[2] != "expr")
print("by key:", agg.most_common(20))
byfile = collections.Counter(h[0] for h in hits)
print("by file:", byfile.most_common(25))
# unique dict-key hits only in responses (jsonify / return dict)
