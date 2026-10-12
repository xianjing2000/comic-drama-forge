# -*- coding: utf-8 -*-
"""t1 独立复核：三版本 _panel_dup_hit 生产语料回放。"""
import subprocess, sys, os, re, json, types, logging
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

QC_DIR = os.environ.get("T1_QC_DIR") or r"C:\Users\liujianghua\AppData\Roaming\mjscxt-desktop\mjscxt-data\output\qc"
print('QC_DIR =', QC_DIR, '| exists =', os.path.isdir(QC_DIR))

def load_sb(commit):
    if commit == 'WORKTREE':
        with open('app/sb_helpers.py', encoding='utf-8') as f:
            src = f.read()
    else:
        src = subprocess.check_output(
            ['git', 'show', commit + ':app/sb_helpers.py'], encoding='utf-8')
    sp = types.ModuleType('shared_project')
    sp._first_existing = lambda *a, **k: None
    sp._shot_num_key = lambda *a, **k: 0
    sp.comfyui_client = types.SimpleNamespace()
    sys.modules['shared_project'] = sp
    ns = {'logging': logging, 're': re, 'shared_project': sp,
          '__name__': 'sb_helpers_stub', '__file__': commit}
    exec(compile(src, 'sb_helpers.py', 'exec'), ns)
    return ns['_panel_dup_hit']

VERSIONS = [
    ('ac27d91^  (修复前)', 'ac27d91^'),
    ('ac27d91  (两级否定+截断)', 'ac27d91'),
    ('44d3977  (+WEAK6字/HEAD)', '44d3977'),
    ('WORKTREE (+同义族扩面)', 'WORKTREE'),
]

def collect():
    rows = []
    njson = 0
    for dp, dn, fns in os.walk(QC_DIR):
        for fn in fns:
            if not fn.endswith('.json'):
                continue
            njson += 1
            p = os.path.join(dp, fn)
            try:
                j = json.load(open(p, encoding='utf-8'))
            except Exception:
                continue
            recs = j.get('records') if isinstance(j, dict) else None
            if not recs:
                continue
            for r in recs:
                if isinstance(r, dict):
                    rows.append(r)
    print('json 文件数:', njson)
    return rows

rows = collect()
print('总 records:', len(rows))
print('passed=true:', sum(1 for r in rows if r.get('passed') is True))
print('passed=false:', sum(1 for r in rows if r.get('passed') is False))

DUP_RE = re.compile(r'(雷同|重复|趋同|单一)')

for label, commit in VERSIONS:
    try:
        hit = load_sb(commit)
    except Exception as e:
        print('\n[%s] 加载失败: %r' % (label, e))
        continue
    fp = 0
    tp = 0
    fn_list = []
    for r in rows:
        passed = r.get('passed')
        reason = str(r.get('reason') or '')
        h = hit(reason)
        if passed is True and h:
            fp += 1
        if passed is False and h:
            tp += 1
        if passed is False and not h and DUP_RE.search(reason):
            fn_list.append(reason)
    print('\n=== [%s] ===' % label)
    print('  达标误判(passed=T 判缺陷): %d / %d' % (fp, sum(1 for r in rows if r.get('passed') is True)))
    print('  真缺陷命中(passed=F 命中): %d / %d' % (tp, sum(1 for r in rows if r.get('passed') is False)))
    print('  漏判(passed=F 含雷同族词 未命中): %d' % len(fn_list))
    for s in fn_list[:12]:
        print('    ·', s[:110])
