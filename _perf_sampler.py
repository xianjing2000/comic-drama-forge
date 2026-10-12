
# -*- coding: utf-8 -*-
"""只读性能采样器：nvidia-smi + 进程内存 + 系统内存，CSV 落盘。不接触任何生产服务。"""
import subprocess, time, os, csv, io, sys, datetime, json
OUT = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\_perf_samples.csv'
DUR = float(os.environ.get('PERF_DUR', '900'))
IV = float(os.environ.get('PERF_IV', '3'))
def sh(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           encoding='utf-8', errors='replace', shell=True)
        return (p.stdout or '').strip()
    except Exception as e:
        return 'ERR:' + str(e)[:80]
f = io.open(OUT, 'w', encoding='utf-8', newline='')
w = csv.writer(f)
w.writerow(['ts','gpu_util','gpu_power_w','gpu_mem_used_mib','gpu_temp','gpu_sm_mhz',
            'comfyui_ws_mb','comfyui_priv_mb','flask_ws_mb','flask_priv_mb',
            'sys_free_gb','sys_commit_gb','pages_sec','top_proc','top_proc_mb',
            'comfyui_cpu_s','flask_cpu_s','gpu_procs'])
f.flush()
t0 = time.time()
n = 0
while time.time() - t0 < DUR:
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    g = sh('nvidia-smi --query-gpu=utilization.gpu,power.draw,memory.used,temperature.gpu,clocks.current.sm --format=csv,noheader,nounits')
    parts = [x.strip() for x in g.split(',')] if g and not g.startswith('ERR') else ['','','','','']
    gp = sh('nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader')
    ps = sh('powershell -NoProfile -Command "Get-Process python | Select-Object Id,WorkingSet64,PrivateMemorySize64,CPU | ConvertTo-Csv -NoTypeInformation"')
    rows = []
    for line in (ps or '').splitlines()[1:]:
        cells = [c.strip('"') for c in line.split(',')]
        if len(cells) >= 4:
            try: rows.append((int(cells[0]), float(cells[1])/1048576.0, float(cells[2])/1048576.0, float(cells[3])))
            except: pass
    comfy = next((r for r in rows if r[0] == 9876), (0,0,0,0))
    flask = next((r for r in rows if r[0] == 15264), (0,0,0,0))
    topp = max(rows, key=lambda r: r[1]) if rows else (0,0,0,0)
    osm = sh('powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem) | Select-Object -Property FreePhysicalMemory,TotalVirtualMemorySize,FreeVirtualMemory | ConvertTo-Csv -NoTypeInformation"')
    free_gb = commit_gb = ''
    try:
        cells = [c.strip('"') for c in (osm or '').splitlines()[1].split(',')]
        free_gb = round(float(cells[0])/1048576.0, 2)          # FreePhysicalMemory KB -> GB
        commit_gb = round((float(cells[1])-float(cells[2]))/1048576.0, 2)
    except Exception: pass
    w.writerow([ts, parts[0], parts[1], parts[2], parts[3], parts[4],
                round(comfy[1],1), round(comfy[2],1), round(flask[1],1), round(flask[2],1),
                free_gb, commit_gb, '', topp[0], round(topp[1],1), comfy[3], flask[3], gp.replace('\n',' | ')])
    f.flush(); n += 1
    time.sleep(IV)
f.close()
print('DONE samples=', n, 'file=', OUT)

