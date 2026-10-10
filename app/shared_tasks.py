# -*- coding: utf-8 -*-
"""任务注册表清理与队列状态（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 6 步，2026-10-10）

core 模块（asset_worker / keyframe_helpers / mix_helpers / video_helpers …）需要这组能力，
却只能从 routes/_shared 取 —— 它们与 HTTP 毫无关系。上移到 app/ 层后，
core 侧依赖的是 app 层模块，而不是路由层私有模块。

## 铁律

不得反向依赖 routes/*。所依赖的业务模块均已核对不导入 routes._shared。
"""
from __future__ import annotations

from config import TASKS_DB_PATH
from shared_base import _app_logger

import analytics
import task_store


_TASK_STATE_KEEP_DONE = 40


_TASK_TERMINAL_STATUSES = ("completed", "done", "failed", "error", "cancelled")


def _prune_task_registry(registry: dict) -> int:
    """清理一个任务注册表里超量的终态条目，返回清理条数（须持有该注册表的锁）"""
    if not isinstance(registry, dict):
        return 0
    _done = sum(1 for v in registry.values()
                if isinstance(v, dict)
                and str(v.get("status") or "") in _TASK_TERMINAL_STATUSES)
    _excess = _done - _TASK_STATE_KEEP_DONE
    if _excess <= 0:
        return 0
    _pruned = 0
    for _k in list(registry):
        if _excess <= 0:
            break
        _v = registry.get(_k)
        if isinstance(_v, dict) and \
                str(_v.get("status") or "") in _TASK_TERMINAL_STATUSES:
            registry.pop(_k, None)
            _excess -= 1
            _pruned += 1
    return _pruned


def _task_analytics_hook(task: dict, event: str) -> None:
    try:
        analytics.record_from_task(task, analytics_kind=task.get("kind"))
    except Exception as e:  # noqa: BLE001  统计失败不得影响任务
        _app_logger().warning(f"任务统计写入失败（忽略）：{e}")


def _task_queue_status() -> dict:
    """TaskQueue 状态 + 「未接线」显式标注（N2，2026-09-22 复验）。

    ``TaskQueue.submit`` 在本项目**没有生产调用方**：单 GPU 并发由 ``gpu_task_gate``
    的进程级 Semaphore 承担（见 ``gpu_task_gate.py`` 模块头「不做什么」）。D-07 给
    submit 加的背压/去重是该模块**自身契约**的加固，供嵌入使用与测试。这里显式标注
    ``wired=False``，避免 ``/api/status`` 的 ``task_queue`` 字段让调用方误以为它是
    生产并发闸门（即「已修但不可达」的假象）。

    既有字段（running/concurrency/queued/max_queue/pending/current/current_elapsed_sec）
    原样保留，``wired`` / ``note`` 均为**新增**字段。
    """
    try:
        st = dict(task_queue.status())
    except Exception as e:  # noqa: BLE001  可观测性接口自身绝不能把 /api/status 打挂
        return {"wired": False, "error": f"{type(e).__name__}: {e}"}
    st["wired"] = False
    st["note"] = ("本进程 GPU 并发由 gpu_gate 承担；TaskQueue.submit 未接线"
                  "（D-07 加固属模块自身契约，非生产路径）")
    return st


task_queue = task_store.get_queue(TASKS_DB_PATH)


