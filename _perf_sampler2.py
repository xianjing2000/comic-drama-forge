
# -*- coding: utf-8 -*-
"""长周期只读采样（轻量，15s 一次，10 分钟）：用于视频阶段 GPU/内存画像。"""
import subprocess, time, os, csv, io, datetime, json
OUT = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\_perf_samples_long.csv'
DUR = float(os.environ.get('PERF_DUR','600')); IV = 15.0
def sh(cmd, t=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=t, encoding='utf-8',
                           errors='replace', shell=True)
        return (p.stdout or '').strip()
    except Exception as e: return 'ERR:'+str(e)[:60]
f = io.open(OUT,'w',encoding='utf-8',newline=''); w=csv.writer(f)
w.writerow(['ts','gpu_util','gpu_power_w','gpu_mem_mib','gpu_temp','sm_mhz','comfy_ws_mb','comfy_priv_mb',
            'sys_free_gb','commit_gb','pf_cur_gb','pf_peak_gb','comfy_cpu_s','top3'])
f.flush(); t0=time.time()
while time.time()-t0 < DUR:
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    g = sh('nvidia-smi --query-gpu=utilization.gpu,power.draw,memory.used,temperature.gpu,clocks.current.sm --format=csv,noheader,nounits')
    p = [x.strip() for x in g.split(',')] if g and not g.startswith('ERR') else ['']*5
    ps = sh('powershell -NoProfile -Command "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 4 Id,ProcessName,WorkingSet64 | ConvertTo-Csv -NoTypeInformation"')
    top=[]; comfy=(0,0,0)
    for line in (ps or '').splitlines()[1:]:
        c=[x.strip('"') for x in line.split(',')]
        if len(c)>=3:
            try:
                mb=round(float(c[2])/1048576.0,1); top.append(f"{c[1]}#{c[0]}:{mb}MB")
                if c[0]=='9876': comfy=(0,0,0)
            except: pass
    cs = sh('powershell -NoProfile -Command "$p=Get-Process -Id 9876 -ErrorAction SilentlyContinue; if($p){[math]::Round($p.CPU,1).ToString()+\'|\'+[math]::Round($p.WorkingSet64/1MB,1)+\'|\'+[math]::Round($p.PrivateMemorySize64/1MB,1)}else{\'0|0|0\'}"')
    try:
        cc = [x.strip() for x in (cs or '0|0|0').split('|')]
        cpu_s = float(cc[0]); cws = float(cc[1]); cpriv = float(cc[2])
    except Exception: cpu_s=cws=cpriv=0.0
    om = sh('powershell -NoProfile -Command "$o=Get-CimInstance Win32_OperatingSystem; $pf=Get-CimInstance Win32_PageFileUsage; [string]::Join(\'|\', @([math]::Round($o.FreePhysicalMemory/1MB,2), [math]::Round(($o.TotalVirtualMemorySize-$o.FreeVirtualMemory)/1MB,2), [math]::Round($pf.CurrentUsage/1024,2), [math]::Round($pf.PeakUsage/1024,2)))"')
    try:
        oo=[float(x) for x in (om or '0|0|0|0').split('|')]
    except Exception: oo=[0,0,0,0]
    w.writerow([ts,p[0],p[1],p[2],p[3],p[4],cws,cpriv,oo[0],oo[1],oo[2],oo[3],cpu_s,' | '.join(top)])
    f.flush(); time.sleep(IV)
f.close(); print('DONE', OUT)

