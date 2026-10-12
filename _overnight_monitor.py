# -*- coding: utf-8 -*-
"""过夜监控：逆天系统_全书 第1集生产（只看上次检查之后的新增日志）。

用法: python _overnight_monitor.py
状态文件 .monitor_state.json 记录上次读到的日志偏移，避免把历史日志当成新异常。
"""
import io, json, os, sys, time, datetime
sys.stdout.reconfigure(encoding='utf-8')
D = os.path.join(os.environ['APPDATA'], 'mjscxt-desktop', 'mjscxt-data')
lg = os.path.join(D, 'logs', 'serve_stdout.log')
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.monitor_state.json')
PROJ = '逆天系统_全书'
OTHER = '进境_整本小说'

def load_state():
    try:
        return json.load(io.open(STATE, encoding='utf-8'))
    except Exception:
        return {}

def save_state(st):
    try: io.open(STATE, 'w', encoding='utf-8').write(json.dumps(st, ensure_ascii=False))
    except Exception: pass

def read_new():
    st = load_state()
    off = int(st.get('offset') or 0)
    try:
        size = os.path.getsize(lg)
    except Exception:
        return [], off, off
    if size < off:
        # 日志轮转/重置
        off = 0
    try:
        with io.open(lg, 'r', encoding='utf-8', errors='replace') as fh:
            fh.seek(off)
            data = fh.read()
            new_off = fh.tell()
    except Exception:
        return [], off, off
    lines = [l for l in data.splitlines() if l.strip()]
    st['offset'] = new_off
    st['checked_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_state(st)
    return lines, off, new_off

def dir_stat(sub):
    p = os.path.join(D, 'output', sub, PROJ)
    if not os.path.isdir(p):
        return None
    n = 0; sz = 0; best = None; names = []
    for root, dirs, files in os.walk(p):
        for f in files:
            fp = os.path.join(root, f); n += 1
            try:
                sz += os.path.getsize(fp); mt = os.path.getmtime(fp)
                if best is None or mt > best[0]: best = (mt, fp)
                names.append(f)
            except Exception: pass
    return {'n': n, 'size': sz, 'latest': best, 'names': names}

def main():
    now = datetime.datetime.now().strftime('%H:%M:%S')
    print('=' * 74)
    print('[%s] 过夜监控 —— %s 第1集' % (now, PROJ))
    print('=' * 74)
    # 进度
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:5211/api/autopilot/status', timeout=8) as r:
            s = json.loads(r.read().decode('utf-8'))
        c = s.get('current') or {}
        print('  托管 running=%s paused=%s' % (s.get('running'), s.get('paused')))
        if c:
            print('  当前 %s' % c.get('describe'))
            print('       project=%s step=%s %s%% retries=%s 停滞=%ss' % (
                  c.get('project'), c.get('step'), c.get('percent'), c.get('retries'), c.get('step_stalled_sec')))
            sd = c.get('steps_done') or []
            print('       已完成步骤 %s' % (', '.join(sd) if sd else '(无)'))
            if c.get('stall_warning'): print('       ⚠️ %s' % c['stall_warning'])
        else:
            print('  当前 (无活跃任务)  ← 若已结束需确认是完成还是失败')
    except Exception as e:
        print('  ❌ 后端查询失败: %s' % e)
    # 产物
    print()
    print('  产物:')
    for k in ('scripts', 'storyboards', 'videos', 'dub', 'assets'):
        v = dir_stat(k)
        if not v:
            print('    %-12s -' % k); continue
        lt = v['latest']
        print('    %-12s %3d 个 / %7.2f MB   最近 %s' % (
              k, v['n'], v['size']/1048576.0,
              datetime.datetime.fromtimestamp(lt[0]).strftime('%H:%M:%S') if lt else '-'))
    # 新增日志分析
    lines, o1, o2 = read_new()
    print()
    print('  新增日志 %d 行（偏移 %d -> %d）' % (len(lines), o1, o2))
    txt = '\n'.join(lines)
    # ⚠️ 2026-10-12 实测教训：**「无新增日志」不等于卡死**。
    #   「编写分镜」这类 LLM 调用要 5-10 分钟才返回（上游 wb2api → 远端 API 慢），
    #   这期间后端 CPU 接近 0、日志一行不写，看着像死了，实际正常。
    #   所以判据改用 API 的 step_stalled_sec，阈值放到 15 分钟。
    stall_sec = 0
    try:
        import urllib.request as _u
        with _u.urlopen('http://127.0.0.1:5211/api/autopilot/status', timeout=8) as _r:
            _s = json.loads(_r.read().decode('utf-8'))
        stall_sec = int((_s.get('current') or {}).get('step_stalled_sec') or 0)
    except Exception:
        pass
    STALL_WARN = 900   # 15 分钟
    # ⚠️ 2026-10-12 实测教训（第二次纠正停滞判据）：
    #   `assets` 是一个**大步骤**（含角色/物品/场景/分镜），它的 percent 会长时间
    #   停在同一个值（如 20%），于是 step_stalled_sec 一路涨到 16 分钟 —— 但那期间
    #   场景 png 正以每 1.5~2.5 分钟一张的速度稳定产出（实测时间线可证）。
    #   所以「停滞」不能只看步骤计时，必须**结合产物是否在增长**：
    #   只要最近有产物落盘（30 分钟内），就判定为「正常推进」。
    import glob as _glob
    _recent_artifact = None
    _now = time.time()
    for _sub in ('assets', 'storyboards', 'videos', 'dub', 'scripts'):
        _base = os.path.join(D, 'output', _sub)
        try:
            for _root, _dirs, _files in os.walk(_base):
                for _f in _files:
                    _fp = os.path.join(_root, _f)
                    try: _mt = os.path.getmtime(_fp)
                    except Exception: continue
                    if _recent_artifact is None or _mt > _recent_artifact:
                        _recent_artifact = _mt
        except Exception:
            pass
    _art_age = (int(_now - _recent_artifact) if _recent_artifact else None)
    _art_ok = (_art_age is not None and _art_age <= 1800)   # 30 分钟内有产物
    # ⚠️ 第三次纠正停滞判据：分镜阶段有一段很长的「前置段」——
    #   LLM 九宫格规划（45~65s）+ 提示词预检（90~105s），**这一整段不落盘任何产物文件**，
    #   所以「最近产物」会越来越老（实测到 12 分钟），产物判据在这里失效。
    #   但日志在这段时间是持续增长的（每镜写「[分镜计时] shot=N ...」）。
    #   故最终判据 = 产物在增长 **或** 本区间有新增日志，任一成立即视为正常推进。
    _log_ok = bool(lines)      # lines 是本区间新增日志（read_new 的返回值）
    # ⚠️ 第四次修正（2026-10-12）：还要看 **ComfyUI 自己的输出目录**。
    #   分镜阶段有两个产物落点：
    #     · storyboards/        ← 主分镜图（只有质检通过才落盘，重试期间不动）
    #     · ComfyUI output/     ← 每镜的参考图 sb_ref_<项目>_shot_NN_K.png（每次都写）
    #   实测：shot=2 的质检重试期间 storyboards/ 41 分钟没动静（看着像卡死），
    #   但 ComfyUI output/ 在 03:13:02 写出了 shot_03 的参考图 —— 任务其实在推进。
    #   所以把 ComfyUI 输出目录的最新时间也纳入活性判据。
    _comfy_ok = False
    _comfy_age = None
    for _cd in (r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output',):
        if os.path.isdir(_cd):
            _cm = None
            try:
                for _r2, _d2, _f2 in os.walk(_cd):
                    for _f3 in _f2:
                        try: _mt2 = os.path.getmtime(os.path.join(_r2, _f3))
                        except Exception: continue
                        if _cm is None or _mt2 > _cm: _cm = _mt2
            except Exception:
                pass
            if _cm:
                _comfy_age = int(_now - _cm)
                # 实测：出一张主分镜图（29 节点 + 5-9 张参考图）约需 20 分钟，
                # 期间 ComfyUI 输出目录不会写任何文件。故阈值取 25 分钟，避免误报。
                _comfy_ok = _comfy_age <= 1500   # 25 分钟内有 ComfyUI 产物
    if _comfy_age is not None:
        print('    ComfyUI 输出: %d 分钟前（阈值 25 分钟）%s' % (_comfy_age / 60.0,
              '→ 正在出图' if _comfy_ok else '→ 超 25 分钟无输出，可疑'))
    _alive_ok = bool(_art_ok or _log_ok or _comfy_ok)
    if _art_age is not None:
        print('    最近产物: %d 分钟前（阈值 30 分钟）%s' % (_art_age / 60.0,
              '→ 正常' if _art_ok else '→ 可疑（超过 30 分钟无新产物）'))
    if not lines:
        if stall_sec >= STALL_WARN and not _alive_ok:
            print('    🚨 停滞 %.0f 分钟 且 无新增日志 且 产物超 30 分钟未增长 → 高度可疑，需介入'
                  % (stall_sec / 60.0))
        elif stall_sec >= STALL_WARN:
            print('    ℹ️ 步骤计时已 %.0f 分钟，但产物仍在增长 → 大步骤正常推进（如资产生成）'
                  % (stall_sec / 60.0))
        else:
            print('    ℹ️ 本区间无新增日志，但停滞仅 %.1f 分钟 → 正常（LLM 调用中）'
                  % (stall_sec / 60.0))
        problems = []
    else:
        # ⚠️ 第四次纠正（2026-10-12）：有新增日志就说明**确实在推进**，
        #    哪怕是「质检不通过 → 优化提示词 → 重试」这种循环，也是在正常工作。
        #    实测：分镜 stage 一次重试周期就要 15-22 分钟（出图 + AI质检 + 提示词优化 + 重出图），
        #    期间 step_stalled_sec 会超过 15 分钟阈值，但日志一直在写。
        #    所以这里**不再报 🚨**，只在日志也停了时才由上面那个分支告警。
        if stall_sec >= STALL_WARN:
            print('    ℹ️ 步骤计时 %.1f 分钟，但本区间有 %d 行新日志 → 正在推进（可能是质检重试循环）'
                  % (stall_sec / 60.0, len(lines)))
        problems = [l for l in lines if ('ERROR' in l or 'CRITICAL' in l)]
        enc = [l for l in lines if ('UnicodeEncodeError' in l or 'UnicodeDecodeError' in l)]
        cross = [l for l in lines if OTHER in l]
        # ⚠️ 2026-10-12 实测误报：直接匹配「失败」二字会把说明性文案算进来，
        #   例如 `[心跳] 已启动（每 20s 探一次；连续 5 次失败自动重启）` ——
        #   那是功能描述，不是错误。所以只在 WARNING/ERROR 级别行里匹配「失败」。
        fails = [l for l in lines
                 if ('Traceback' in l
                     or (('WARNING' in l or 'ERROR' in l) and ('失败' in l or '异常' in l or '中止' in l)))]
        print('    ERROR %d ｜ 编码错误 %d ｜ 跨项目串用 %d ｜ 失败/回溯 %d' % (
              len(problems), len(enc), len(cross), len(fails)))
        if enc:
            print('    🚨 编码错误（本轮修复失效！）:')
            for l in enc[:3]: print('       ' + l[:150])
        if cross:
            print('    🚨 出现其他项目「%s」的路径/数据:' % OTHER)
            for l in cross[:5]: print('       ' + l[:150])
        if problems:
            print('    ERROR 明细:')
            for l in problems[:6]: print('       ' + l[:150])
        if fails:
            print('    失败/回溯:')
            for l in fails[:6]: print('       ' + l[:150])
        # 修复验证（只看新增）
        print()
        print('    本轮修复生效证据:')
        checks = [
            ('分镜过碎自愈', '自愈重切' in txt),
            ('提示词达标', '提示词达标' in txt or 'accept=True' in txt),
            ('角色四档/群像', any(x in txt for x in ('群像合影', '双格正侧', '单张正面立绘', 'crowd', 'supporting', 'primary'))),
            ('断点缓存复用', '命中断点缓存' in txt),
            ('风格注入', '补写风格' in txt),
            ('H3 音色声明', '音色声明' in txt),
            ('无静音段修复', 'room tone' in txt or 'room_tone' in txt or 'continuous audible' in txt),
        ]
        for nm, ok in checks:
            if ok: print('      ✅ %s' % nm)
    print()
    print('  最近 6 行:')
    for l in lines[-6:]: print('    ' + l[:150])

if __name__ == '__main__':
    main()