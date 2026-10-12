
import io, os, re, sys, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = os.path.join(os.getcwd(), "frontend", "src")
CN = re.compile(r"[\u4e00-\u9fff]")
per_file = collections.Counter()
total_cn_lines = 0
t_calls = 0
for r, d, fs in os.walk(SRC):
    d[:] = [x for x in d if x not in ("node_modules", "assets")]
    for f in fs:
        if not f.endswith((".tsx", ".ts")): continue
        p = os.path.join(r, f)
        rel = os.path.relpath(p, SRC)
        src = io.open(p, encoding="utf-8", errors="replace").read()
        t_calls += len(re.findall(r"\bt\(", src))
        for ln in src.splitlines():
            s = ln.strip()
            if not s or s.startswith("//") or s.startswith("*") or s.startswith("/*") or s.startswith("<!--"):
                continue
            # drop trailing line comment
            code = re.split(r"\s//\s", s)[0]
            if CN.search(code):
                per_file[rel] += 1
                total_cn_lines += 1
print("files with Chinese in code lines:", len(per_file), " lines:", total_cn_lines, " t() calls:", t_calls)
for f, n in per_file.most_common(30):
    print("   %-46s %d" % (f, n))
