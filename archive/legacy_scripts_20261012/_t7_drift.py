
import io, os, hashlib, sys, json, datetime
sys.stdout.reconfigure(encoding='utf-8')
WS = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\app'
def h(p):
    try:
        with open(p,'rb') as f: return hashlib.sha1(f.read()).hexdigest()
    except Exception: return 'ERR'
def snap(d, skip_bak=True):
    out={}
    for f in os.listdir(d):
        if not f.endswith('.py'): continue
        if skip_bak and ('.bak' in f or '.rollback' in f): continue
        out[f]=h(os.path.join(d,f))
    return out
COPIES = {
 'A-workspace/app': WS,
 'B-production/C:\\mjscxt_p139\\resources\\backend': r'C:\mjscxt_p139\resources\backend',
 'C-mirror/%APPDATA%/resource-mirror/app': os.path.join(os.environ['APPDATA'],'mjscxt-desktop','resource-mirror','app'),
 'D-portable/packaging/_dist/app': r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\packaging\_dist\漫剧工坊_便携版\app',
 'E-dist/electron-app/dist/win-unpacked/resources/backend': r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\electron-app\dist\win-unpacked\resources\backend',
}
A = snap(WS)
print('A(workspace) py =', len(A))
for name, d in COPIES.items():
    if not os.path.isdir(d):
        print('%-58s NOT FOUND' % name); continue
    B = snap(d)
    same = sum(1 for k,v in A.items() if B.get(k)==v)
    diff = sorted(k for k,v in A.items() if k in B and B[k]!=v)
    onlyB = sorted(set(B)-set(A))
    onlyA = sorted(set(A)-set(B))
    print('%-58s py=%3d same=%3d diff=%2d A_only=%d B_only=%d' % (name.split('/')[-1], len(B), same, len(diff), len(onlyA), len(onlyB)))
    if diff: print('    DIFF:', diff[:12])
    if onlyA: print('    A_only:', onlyA[:12])
    if onlyB: print('    B_only:', onlyB[:12])
# 快照残留计数
for name, d in COPIES.items():
    if not os.path.isdir(d): continue
    n = sum(1 for f in os.listdir(d) if '.bak' in f or '.rollback' in f)
    if n: print('快照残留 %-40s = %d 个' % (name.split('/')[-1], n))

