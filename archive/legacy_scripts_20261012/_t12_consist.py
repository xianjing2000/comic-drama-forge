
import io, os, re, sys, collections
sys.stdout.reconfigure(encoding='utf-8')
APP = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\app'
FILES = []
for r, d, fs in os.walk(APP):
    d[:] = [x for x in d if x not in ('__pycache__',)]
    for f in fs:
        if f.endswith('.py'): FILES.append(os.path.join(r, f))
def rel(p): return os.path.relpath(p, APP).replace(os.sep, '/')

# 1) _now() 的各种实现体
print('=== 各模块 _now() 实现 ===')
for p in sorted(FILES):
    txt = io.open(p, encoding='utf-8', errors='replace').read().splitlines()
    for i, line in enumerate(txt):
        m = re.match(r'\s*def _now\(\s*\)\s*(->\s*[\w\[\]]+)?\s*:', line)
        if m:
            body = []
            for j in range(i+1, min(i+4, len(txt))):
                if txt[j].strip(): body.append(txt[j].strip())
                else: break
            print('  %-34s L%-5d %-22s -> %s' % (rel(p), i+1, (m.group(1) or '').strip(), ' | '.join(body)[:90]))
print()
def scan(pat, label, show=10):
    rx = re.compile(pat)
    hits = []
    for p in FILES:
        for i, line in enumerate(io.open(p, encoding='utf-8', errors='replace'), 1):
            if rx.search(line): hits.append((rel(p), i, line.strip()[:130]))
    print('=== %s : %d 处（%d 文件）===' % (label, len(hits), len({h[0] for h in hits})))
    c = collections.Counter(h[0] for h in hits)
    print('   ', ', '.join('%s(%d)' % (f, n) for f, n in c.most_common(12)))
    for h in hits[:show]: print('      %s:%d  %s' % h)
    print()
    return hits
scan(r'def (atomic_write_json|_atomic_write|_write_atomic|write_json_atomic|_atomic_json)', '原子 JSON 写函数定义')
scan(r'def (_move_with_retry|_trash_move|_move_to_trash|trash_move|_recycle|move_to_recycle)', '文件搬移/回收函数定义')
scan(r'def (.*retry.*|.*_rotate.*|.*_trash.*)\(', '其它含 retry/trash 的函数定义', 12)

