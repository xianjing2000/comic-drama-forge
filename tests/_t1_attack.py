# -*- coding: utf-8 -*-
import subprocess, sys, os, re, types, logging
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

def load_sb(commit):
    if commit == 'WORKTREE':
        src = open('app/sb_helpers.py', encoding='utf-8').read()
    else:
        src = subprocess.check_output(['git','show',commit+':app/sb_helpers.py'], encoding='utf-8')
    sp = types.ModuleType('shared_project')
    sp._first_existing = lambda *a,**k: None
    sp._shot_num_key = lambda *a,**k: 0
    sp.comfyui_client = types.SimpleNamespace()
    sys.modules['shared_project'] = sp
    ns = {'logging':logging,'re':re,'shared_project':sp,'__name__':'stub','__file__':commit}
    exec(compile(src,'sb_helpers.py','exec'), ns)
    return ns['_panel_dup_hit']

versions = [('ac27d91^','修复前'),('ac27d91','两级否定'),('44d3977','WEAK6字'),('WORKTREE','同义族')]

# 44d3977 放过的生产真缺陷
prod_miss = "九宫格中多数格子为近似机位的客厅全景，构图差异不足构成面板雷同，另有个别格物体形态失真与场景结构缺失，未达可直接使用标准。"

print("=== 生产真缺陷「构图差异不足构成面板雷同」三版本行为 ===")
for c, label in versions:
    h = load_sb(c)
    print(f'  {label:8s} ({c:8s}) -> hit={h(prod_miss)}')

# 最小复现
mini = "构图差异不足构成面板雷同"
print()
print("=== 最小复现:", mini, "===")
for c, label in versions:
    h = load_sb(c)
    print(f'  {label:8s} -> hit={h(mini)}')
