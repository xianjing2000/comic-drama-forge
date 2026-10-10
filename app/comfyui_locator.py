# -*- coding: utf-8 -*-
"""ComfyUI 安装位置自动检测（2026-10-10 用户需求）。

## 为什么需要
此前 ``config.MJSCXT_COMFYUI_DIR`` 的默认值是**开发机上写死的路径**
(``D:\\ComfyUI_portable_TE_v260619\\ComfyUI``)。用户机器上该目录通常不存在，
于是「心跳重启 ComfyUI」必然失败（日志：找不到启动脚本），
而重启失败又会让生产卡死。必须能自动找到用户自己的 ComfyUI。

## 检测顺序（可靠性从高到低）
1. **环境变量** ``MJSCXT_COMFYUI_DIR`` —— 显式指定，最高优先级；
2. **上一步的检测结果缓存**（``<data>/comfyui_dir.json``）—— 命中即返回，最快；
3. **正在运行的 ComfyUI 进程**反查 —— 从命令行里取出 ``ComfyUI\\main.py`` 的所在目录。
   这是最可靠的「用户实际在用哪一个」的证据（进程就在跑，路径一定对）；
4. **常见路径扫描** —— 各盘符下 ``ComfyUI*`` / ``*ComfyUI*`` 目录，
   用「有 main.py 或 custom_nodes 或 run_*.bat」判定为候选；
5. 全都找不到 → 返回空串，调用方按「未配置」处理（不猜、不硬编码）。

## 纪律
- 全程 fail-open：任何一步异常都跳过，绝不抛给调用方；
- 只读：不创建、不修改 ComfyUI 目录里的任何东西；
- 有界耗时：扫描最多 N 秒、最多看 M 个候选，避免拖慢启动；
- 结果持久化，只有显式 refresh=True 才重新全扫。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

#: 判定「这是不是 ComfyUI 根目录」的标志物（命中任意一个即可）
_MARKERS = ('main.py', 'custom_nodes', 'comfy', 'run_nvidia_gpu.bat',
            'run_nvidia_gpu_fixed.bat', 'run_cpu.bat')
#: 启动脚本候选（重启时按顺序找第一个存在的）
LAUNCH_SCRIPTS = ('run_nvidia_gpu_fixed.bat', 'run_nvidia_gpu.bat',
                  'run_cpu.bat', 'run.bat')
#: 扫描上限（防止在某些机器上遍历整盘导致启动极慢）
_SCAN_MAX_SECONDS = 12.0
_SCAN_MAX_DIRS = 4000

_CACHE: dict = {'dir': None, 'source': '', 'ts': 0.0}


def _data_dir() -> str:
    """数据根目录（放检测结果缓存）。

    ⚠️ 不能 import config：config.py 启动时就要调用本模块做默认值解析，
    反向 import 会造成循环导入。这里直接用 env_loader 的常量（config 也用它）。
    """
    try:
        from env_loader import PROJECT_DATA_DIR
        return str(PROJECT_DATA_DIR or '')
    except Exception:  # noqa: BLE001
        return ''


def _cache_path() -> str:
    d = _data_dir()
    return os.path.join(d, 'comfyui_dir.json') if d else ''


def _looks_like_comfyui(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        names = set(os.listdir(path))
    except Exception:  # noqa: BLE001
        return False
    hit = [m for m in _MARKERS if m in names]
    # 至少要同时看到 main.py 与 custom_nodes（ComfyUI 的稳定特征），
    # 或者看到启动脚本 —— 只看单个 custom_nodes 容易把别的东西误判进来。
    if 'main.py' in names and 'custom_nodes' in names:
        return True
    if any(s in names for s in LAUNCH_SCRIPTS):
        return True
    return len(hit) >= 3


def _norm(path: str) -> str:
    p = os.path.normpath(str(path or '').strip().strip('"'))
    return p if _looks_like_comfyui(p) else ''


def _from_running_process() -> str:
    r"""从正在运行的 ComfyUI 进程反查安装目录（最可靠的证据）。

    ⚠️ 2026-10-10 实测：便携版的 CommandLine 里 main.py 是**相对路径**
    （"python_embeded\python.exe -s ComfyUI\main.py …"），单靠它推不出绝对目录。
    但同一进程的 ExecutablePath 是绝对的：
        D:\ComfyUI_portable_TE_v260619\ComfyUI\python_embeded\python.exe
    取其**上两级**（跳过 python_embeded）= 便携包根，再遍历其下的子目录找
    ComfyUI 根 —— 正是我们要的。
    """
    if os.name != 'nt':
        return ''
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*main.py*' -and "
              "$_.CommandLine -like '*ComfyUI*' } | "
              "ForEach-Object { $_.ExecutablePath }")
        r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                           capture_output=True, timeout=15, check=False,
                           text=True, encoding='utf-8', errors='replace')
        for raw in (r.stdout or '').splitlines():
            exe = (raw or '').strip().strip('"')
            if not exe or not os.path.isfile(exe):
                continue
            exe_p = os.path.normpath(exe)
            # ① 若是 python_embeded\python.exe → 上两级即便携包根
            pkg_root = os.path.dirname(os.path.dirname(exe_p))
            # ⚠️ 便携包常见「ComfyUI_portable_xxx/ComfyUI/ComfyUI」**双层同名**结构：
            #    外层有 run_nvidia_gpu_fixed.bat（真正的启动目录），内层有 main.py+custom_nodes。
            #    两者都能通过 _looks_like_comfyui，但**重启必须用外层**（脚本在那）。
            #    故这里分两轮：先只认含启动脚本的，找不到再放宽。
            for want_script in (True, False):
                for base in (pkg_root, os.path.dirname(exe_p), os.path.dirname(pkg_root)):
                    try:
                        for name in os.listdir(base):
                            cand = os.path.join(base, name)
                            got = _norm(cand)
                            if not got:
                                continue
                            # ⚠️ 必须判「**该目录自身**含启动脚本」，不能用
                            #    launch_script(got) —— 后者会回退到父目录找，
                            #    于是内层 ComfyUI\ComfyUI 也会「有」脚本，判据失效。
                            if want_script and not any(
                                    os.path.isfile(os.path.join(got, _s))
                                    for _s in LAUNCH_SCRIPTS):
                                continue
                            return got
                    except Exception:  # noqa: BLE001
                        continue
            # ② 若 python.exe 本身就在 ComfyUI 根下（非便携版）
            got = _norm(os.path.dirname(exe_p))
            if got:
                return got
    except Exception as e:  # noqa: BLE001
        logger.debug('从进程反查 ComfyUI 目录失败（忽略）：%s', e)
    return ''


def _drive_roots() -> List[str]:
    if os.name != 'nt':
        return ['/']
    out = []
    try:
        import string
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask >> i & 1:
                out.append(f'{letter}:\\')
    except Exception:  # noqa: BLE001
        out = [f'{c}:\\' for c in 'CDEFG']
    return out


def _scan_common() -> str:
    """扫描各盘符下名字像 ComfyUI 的目录"""
    t0 = time.time()
    seen = 0
    patterns = ['ComfyUI*', '*ComfyUI*', '*comfyui*', 'AI\\ComfyUI*',
                'Program Files\\ComfyUI*', 'tools\\ComfyUI*']
    for root in _drive_roots():
        for pat in patterns:
            try:
                hits = glob.glob(os.path.join(root, pat))
            except Exception:  # noqa: BLE001
                continue
            for p in hits:
                seen += 1
                if seen > _SCAN_MAX_DIRS or time.time() - t0 > _SCAN_MAX_SECONDS:
                    logger.info('ComfyUI 扫描达到上限（%d 个候选 / %.1fs），停止',
                                seen, time.time() - t0)
                    return ''
                got = _norm(p)
                if got:
                    return got
                # 再往里看一层（便携包常见：外层目录名不含 ComfyUI，内层才是）
                try:
                    for sub in os.listdir(p):
                        got = _norm(os.path.join(p, sub))
                        if got:
                            return got
                except Exception:  # noqa: BLE001
                    continue
    return ''


def _load_cache() -> str:
    p = _cache_path()
    if not p or not os.path.isfile(p):
        return ''
    try:
        with open(p, 'r', encoding='utf-8') as f:
            d = json.load(f) or {}
        got = _norm(d.get('dir') or '')
        if got:
            _CACHE.update({'dir': got, 'source': 'cache', 'ts': time.time()})
        return got
    except Exception:  # noqa: BLE001
        return ''


def _save_cache(path: str, source: str) -> None:
    p = _cache_path()
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'dir': path, 'source': source, 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.debug('写入 ComfyUI 目录缓存失败（忽略）：%s', e)


def find_comfyui_dir(*, refresh: bool = False, use_cache: bool = True) -> str:
    """返回检测到的 ComfyUI 根目录；找不到返回空串。全程只读、fail-open。

    :param refresh: True 时忽略内存/磁盘缓存，重新按顺序检测
    :param use_cache: False 时不读磁盘缓存（仍然会写）
    """
    if not refresh and _CACHE.get('dir') and _looks_like_comfyui(_CACHE['dir']):
        return _CACHE['dir']
    # 1. 环境变量
    try:
        got = _norm(os.environ.get('MJSCXT_COMFYUI_DIR') or '')
        if got:
            _CACHE.update({'dir': got, 'source': 'env', 'ts': time.time()})
            return got
    except Exception:  # noqa: BLE001
        pass
    # 2. 磁盘缓存
    if use_cache and not refresh:
        got = _load_cache()
        if got:
            logger.info('ComfyUI 目录来自缓存：%s', got)
            return got
    # 3. 运行中的进程
    got = _from_running_process()
    if got:
        logger.info('ComfyUI 目录检测自运行中的进程：%s', got)
        _CACHE.update({'dir': got, 'source': 'process', 'ts': time.time()})
        _save_cache(got, 'process')
        return got
    # 4. 常见路径扫描
    got = _scan_common()
    if got:
        logger.info('ComfyUI 目录检测自路径扫描：%s', got)
        _CACHE.update({'dir': got, 'source': 'scan', 'ts': time.time()})
        _save_cache(got, 'scan')
        return got
    logger.warning('未能自动检测到 ComfyUI 安装目录。请设置环境变量 MJSCXT_COMFYUI_DIR '
                   '指向 ComfyUI 根目录（含 main.py 与 custom_nodes），或在界面上手动指定。')
    return ''


def launch_script(comfy_dir: str) -> str:
    """在给定 ComfyUI 目录里找启动脚本；找不到返回空串"""
    if not comfy_dir or not os.path.isdir(comfy_dir):
        return ''
    for name in LAUNCH_SCRIPTS:
        p = os.path.join(comfy_dir, name)
        if os.path.isfile(p):
            return p
    # 有些包把启动脚本放在上一级（如 ComfyUI_portable_xxx/run_nvidia_gpu_fixed.bat）
    parent = os.path.dirname(os.path.normpath(comfy_dir))
    for name in LAUNCH_SCRIPTS:
        p = os.path.join(parent, name)
        if os.path.isfile(p):
            return p
    return ''


def describe() -> dict:
    """给 API/界面用的自检摘要（只读、永不抛）"""
    try:
        d = find_comfyui_dir()
        return {'dir': d, 'found': bool(d), 'source': _CACHE.get('source') or '',
                'launch_script': launch_script(d), 'cache_path': _cache_path()}
    except Exception as e:  # noqa: BLE001
        return {'dir': '', 'found': False, 'source': '', 'error': str(e)[:200]}
