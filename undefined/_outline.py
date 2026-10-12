
import ast, io, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
target = sys.argv[1]
src = io.open(target, encoding="utf-8", errors="replace").read()
tree = ast.parse(src)
lines = src.splitlines()
print("FILE", target, "lines=", len(lines))
def sig(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("def " if not isinstance(node, ast.AsyncFunctionDef) else "async def ") + node.name
    if isinstance(node, ast.ClassDef):
        return "class " + node.name
    return None
for node in tree.body:
    s = sig(node)
    if s is None:
        continue
    end = getattr(node, "end_lineno", node.lineno)
    n = end - node.lineno + 1
    kind = "CLASS" if isinstance(node, ast.ClassDef) else "func"
    print("%6d-%-6d %5d  %-6s %s" % (node.lineno, end, n, kind, s))
    if isinstance(node, ast.ClassDef):
        for sub in node.body:
            ss = sig(sub)
            if ss is None: continue
            send = getattr(sub, "end_lineno", sub.lineno)
            print("        %6d-%-6d %5d  %s.%s" % (sub.lineno, send, send-sub.lineno+1, node.name, sub.name))
