# -*- coding: utf-8 -*-
"""B-01 P1-12：GPU 任务并发闸门 + /free 互斥守卫

解决的问题
----------
- 单 GPU 场景下，手动接口 + 托管守护线程 + 总控 AI 三方并发时无并发上限，
  同时打 ComfyUI → OOM / 任务随机失败 / 显存反复换入换出。
- 超分 / TTS 调用 ComfyUI /free（unload_models=True）时**无条件**卸载其它
  任务正在使用的模型 → 反复换入换出，比「无上限」更直接的伤害来源。

设计
----
1. **进程级 Semaphore(TASK_QUEUE_CONCURRENCY)**：覆盖手动 GPU 链路
   （分镜 / 视频 / 关键帧 / 超分），超出上限时排队 + 告警，不静默丢弃。
2. **running 计数**：供 /free 调用方判断「本进程是否有其它 running GPU 任务」，
   有则不发 /free（避免卸掉他人正在用的模型）。
3. **可观测性**：status() 暴露 running/concurrency/queued，
   供 /api/system/status 展示，修复 TaskQueue.status() 失真（恒 running:false）。

不做什么
--------
- 不接管 task_db 生命周期（start/finish/fail）——各 worker 内部已写好，
  gate 只做并发限制 + 互斥守卫。
- 不删除 TaskQueue 模块（保留 .status() 可观测性，但**不真正接线 submit**——
  改用 Semaphore 方案对齐 P1-12 修复建议 ②，改动面最小且最稳）。
- 不接管非 GPU worker（dub / mix / novel / episodes / analyze）——它们不占
  GPU，无需闸门。
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Optional, Set

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


# ===================== 资源护栏：可用内存/显存只读探测（B-5，2026-10-16） =====================
# 本机 8G 显存 + 32G 内存跑 19.5G 模型时，内存耗尽会出现「86 分钟/镜」的全线停摆
#（docs/性能画像_内存并发GPU_20261012.md）。在每次 GPU 任务开始前探测余量、低于阈值告警，
# 并把数字并进 /api/status。**只读、fail-open**：探测失败绝不阻断任务。
_MEM_CACHE = {"t": 0.0, "data": {}}
_MEM_TTL_SEC = 5.0


def _probe_mem() -> dict:
    """探测可用内存/显存（只读）。任何失败返回已探到的部分（可能为空 dict）。"""
    info: dict = {}
    try:
        if os.name == "nt":
            import ctypes

            class _MEMSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            _st = _MEMSTATUSEX()
            _st.dwLength = ctypes.sizeof(_MEMSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(_st)):
                info["ram_total_gb"] = round(_st.ullTotalPhys / (1024 ** 3), 1)
                info["ram_free_gb"] = round(_st.ullAvailPhys / (1024 ** 3), 1)
        else:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                _m = {}
                for _line in f:
                    _k, _, _v = _line.partition(":")
                    _m[_k.strip()] = int((_v.strip().split() or ["0"])[0] or 0)
            info["ram_total_gb"] = round(_m.get("MemTotal", 0) / (1024 ** 2), 1)
            info["ram_free_gb"] = round(
                _m.get("MemAvailable", _m.get("MemFree", 0)) / (1024 ** 2), 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess
        _p = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if _p.returncode == 0 and (_p.stdout or "").strip():
            _parts = [x.strip() for x in _p.stdout.strip().splitlines()[0].split(",")]
            if len(_parts) >= 2 and _parts[0].isdigit() and _parts[1].isdigit():
                info["vram_free_mb"] = int(_parts[0])
                info["vram_total_mb"] = int(_parts[1])
    except Exception:  # noqa: BLE001
        pass
    return info


def _probe_mem_cached() -> dict:
    """带 5s 缓存的探测（/api/status 会被前端轮询，避免每次都起 nvidia-smi）。"""
    import time as _t
    _now = _t.time()
    if _now - _MEM_CACHE["t"] > _MEM_TTL_SEC:
        _MEM_CACHE["t"] = _now
        _MEM_CACHE["data"] = _probe_mem()
    return _MEM_CACHE["data"]


def _mem_guard(task_id: str, label: str) -> dict:
    """任务开始前只读探测 + 阈值告警（fail-open）。返回探测快照。"""
    try:
        from config import (GPU_GUARD_ENABLED, GPU_GUARD_RAM_WARN_GB,
                            GPU_GUARD_VRAM_WARN_MB)
    except Exception:  # noqa: BLE001
        return {}
    if not GPU_GUARD_ENABLED:
        return {}
    _m = _probe_mem_cached()
    if not _m:
        return {}
    _warns = []
    _rf = _m.get("ram_free_gb")
    if isinstance(_rf, (int, float)) and _rf < GPU_GUARD_RAM_WARN_GB:
        _warns.append("可用内存 %.1fGB < %sGB" % (_rf, GPU_GUARD_RAM_WARN_GB))
    _vf = _m.get("vram_free_mb")
    if isinstance(_vf, (int, float)) and _vf < GPU_GUARD_VRAM_WARN_MB:
        _warns.append("可用显存 %dMB < %sMB" % (_vf, GPU_GUARD_VRAM_WARN_MB))
    if _warns:
        logger.warning(
            "⚠ GPU 任务 %s（%s）开始前资源偏紧：%s —— 可能触发内存换页/变慢，"
            "建议暂停非必要前置段或稍后再跑", task_id, label or "-", "；".join(_warns))
    return _m

_concurrency_loaded = False
_concurrency = 1


def _ensure_loaded() -> None:
    global _concurrency_loaded, _concurrency
    if _concurrency_loaded:
        return
    try:
        from config import TASK_QUEUE_CONCURRENCY
        _concurrency = max(1, int(TASK_QUEUE_CONCURRENCY))
    except Exception:  # noqa: BLE001
        _concurrency = 1
    _concurrency_loaded = True


def _ensure_state() -> None:
    """延迟初始化（避免 import 时 config 未就绪）"""
    _ensure_loaded()
    with _LOCK:
        if _module_state._sem is None:
            _module_state._sem = threading.Semaphore(_concurrency)


class _State:
    """模块级状态容器"""
    _sem: Optional[threading.Semaphore] = None
    _running_count: int = 0
    _running_tasks: "Set[str]" = set()


_module_state = _State()


def concurrency() -> int:
    """当前配置的 GPU 并发上限"""
    _ensure_loaded()
    return _concurrency


@contextmanager
def run_gpu_task(task_id: str, label: str = ""):
    """GPU 任务并发闸门（context manager）

    用法：
        with gpu_task_gate.run_gpu_task(task_id, "视频生成第1集"):
            do_gpu_work()

    行为：
    - 获取 Semaphore（超出并发上限时阻塞排队）
    - running 计数 +1
    - 执行主体
    - finally 中 running 计数 -1，释放 semaphore
    - 排队超过 30 秒打 warning（可观测性：不静默）

    注意：本函数**不吞异常**——主体抛错向上传播，由调用方（worker）
    自行记录 task_db.fail()。
    本函数**不接管 task_db 生命周期**（start/finish/fail 保留在 worker 内）。
    """
    _ensure_state()
    sem = _module_state._sem

    import time as _time
    _t0 = _time.time()
    acquired = sem.acquire(timeout=3600 * 24)  # 长期等待，实际永不超时
    waited = _time.time() - _t0
    if not acquired:
        # 审计 P2-1（2026-09-29）：@contextmanager 生成器不 yield 就 return，会让
        # with 语句抛 RuntimeError: generator didn't yield —— 等待方拿到的是费解的
        # 异常且任务态可能卡 running。显式 raise 给出可读失败原因，由 worker 的
        # catch-all 回写任务失败态。
        logger.error("GPU 任务 %s 排队超时（24h），放弃执行", task_id)
        raise RuntimeError(f"GPU 任务 {task_id} 排队超时（24h），放弃执行")
    if waited > 30:
        with _LOCK:
            _cur = _module_state._running_count
        logger.warning("GPU 任务 %s 排队 %.1fs（当前并发 %d/%d）",
                       task_id, waited, _cur, _concurrency)

    with _LOCK:
        _module_state._running_count += 1
        _module_state._running_tasks.add(task_id)
        _cur = _module_state._running_count
    logger.info("GPU 任务 %s 开始执行（%s；当前 running=%d/%d）",
                task_id, label, _cur, _concurrency)
    _mem_guard(task_id, label)   # B-5：开始前只读探测余量，偏紧则告警（fail-open）

    try:
        yield
    finally:
        with _LOCK:
            _module_state._running_count -= 1
            _module_state._running_tasks.discard(task_id)
        sem.release()
        logger.info("GPU 任务 %s 执行结束（%s）", task_id, label)


def has_other_running_gpu_tasks(exclude_task_id: str = "") -> bool:
    """本进程是否有其它 running 的 GPU 任务（/free 互斥守卫用）

    调用方：upscale_client / tts_client 在发 /free 前检查。
    若 exclude_task_id 非空，排除自身（「我」不算「他人」）。
    本函数**无阻塞**（仅读 running 计数），不影响调用方性能。
    """
    _ensure_state()
    with _LOCK:
        if _module_state._running_count == 0:
            return False
        if exclude_task_id and _module_state._running_tasks == {exclude_task_id}:
            return False
        return True


def status() -> dict:
    """当前闸门状态（供 /api/system/status 暴露，修复失真可观测性）。

    B-5（2026-10-16）：附上可用内存/显存只读探测与阈值告警位，给运维处置窗口。
    """
    _ensure_state()
    with _LOCK:
        out = {
            "running": _module_state._running_count,
            "concurrency": _concurrency,
            "running_tasks": list(_module_state._running_tasks),
        }
    try:
        from config import (GPU_GUARD_ENABLED, GPU_GUARD_RAM_WARN_GB,
                            GPU_GUARD_VRAM_WARN_MB)
        _m = _probe_mem_cached()
        if _m:
            out["memory"] = _m
            _rf = _m.get("ram_free_gb")
            _vf = _m.get("vram_free_mb")
            out["memory_warn"] = bool(
                (isinstance(_rf, (int, float)) and _rf < GPU_GUARD_RAM_WARN_GB) or
                (isinstance(_vf, (int, float)) and _vf < GPU_GUARD_VRAM_WARN_MB))
            out["guard_enabled"] = bool(GPU_GUARD_ENABLED)
    except Exception:  # noqa: BLE001
        pass
    return out
