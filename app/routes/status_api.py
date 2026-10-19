# -*- coding: utf-8 -*-
'''系统状态 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
import logging
from flask import Blueprint, jsonify, request, send_file, abort, current_app, Response, stream_with_context  # noqa: F401
import os    # noqa: F401
import sys   # noqa: F401
import re    # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
import threading   # noqa: F401
import hashlib     # noqa: F401
import shutil      # noqa: F401
import random      # noqa: F401
import base64      # noqa: F401
import datetime    # noqa: F401
import traceback   # noqa: F401
import subprocess  # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from config import CHARACTERS_DIR
from config import ITEMS_DIR
from config import SCENES_DIR
from routes._shared import _task_queue_status
from routes._shared import comfyui_client  # ComfyUIClient 实例（非模块！get_status 是实例方法）
import gpu_task_gate
import job_state

logger = logging.getLogger(__name__)

status_api_bp = Blueprint('status_api', __name__)

@status_api_bp.route('/api/system/identity')
def api_system_identity():
    """返回本进程的「数据根指纹」——供 Electron 判断能否复用已有后端。

    ⭐⭐ 2026-10-11 用户要求：「两个服务进程并存这个问题要解决，就和打开你现在运行
    一样，检测到已打开的程序直接显示窗口就行了」。

    ## 为什么之前会并存
    main.js 的 resolvePort() 写着「桌面版**永不复用外部后端**」：旧逻辑只要端口上
    有健康 Flask 就直接复用，结果项目数据写到了**别的进程的数据根**，用户表现是
    「桌面版每次打开都是空项目」。为了堵这个坑，改成「默认端口被占用就另找空闲
    端口」→ 于是手工起的服务与桌面版服务同时存在，互抢租约（日志里的
    「正在被另一个执行体生产」即由此而来）。

    ## 正确解法
    复用前**校验数据根是否一致**：一致 → 直接复用（单实例，符合用户预期）；
    不一致 → 仍走另找端口（保住原设计要防的数据根混淆）。
    本接口就是那个判据。
    """
    try:
        from config import PROJECT_DATA_DIR as _PDD
        data_dir = os.path.normcase(os.path.abspath(str(_PDD or "")))
        fp = hashlib.sha256(data_dir.encode("utf-8", "replace")).hexdigest()[:16]
        return jsonify({
            "app": "mjscxt",
            "data_dir": str(_PDD or ""),
            "fingerprint": fp,
            "pid": os.getpid(),
            "port": int(os.environ.get("APP_PORT") or 0),
        })
    except Exception as e:  # noqa: BLE001 - 可观测性接口自身绝不能抛
        logger.warning("system/identity 生成失败：%s", e)
        return jsonify({"app": "mjscxt", "error": str(e)}), 500


@status_api_bp.route('/api/status')
def api_status():
    comfyui_status = comfyui_client.get_status()
    return jsonify({
        "comfyui": comfyui_status,
        "assets": {
            "characters": sum(
                len(files) for _, _, files in os.walk(CHARACTERS_DIR)
            ) if os.path.exists(CHARACTERS_DIR) else 0,
            "items": sum(
                len(files) for _, _, files in os.walk(ITEMS_DIR)
            ) if os.path.exists(ITEMS_DIR) else 0,
            "scenes": sum(
                len(files) for _, _, files in os.walk(SCENES_DIR)
            ) if os.path.exists(SCENES_DIR) else 0,
        },
        "task_queue": _task_queue_status(),
        "gpu_gate": gpu_task_gate.status(),
        "interrupted_tasks": job_state.get_interrupted_tasks(),
    })


@status_api_bp.route('/api/events/stream')
def api_events_stream():
    """SSE 进度推送（E-1，2026-10-16）：把 ``job_state.generation_state`` 里进行中任务的
    快照按秒推送，替代前端 1~30s 轮询。只读、fail-open：快照有变化才推 ``data:``，否则
    推一行 SSE 注释心跳（``: keep-alive``）；Waitress 长连接已就绪
    （``serve.py`` channel_timeout=1800）。

    前端配套：``frontend/src/hooks/useEventStream.ts``。断开由浏览器 EventSource 自动重连。
    """
    def _snapshot():
        tasks = []
        try:
            with job_state.lock:
                for tid, st in list(job_state.generation_state.items()):
                    if not isinstance(st, dict):
                        continue
                    tasks.append({
                        "task_id": tid,
                        "status": st.get("status"),
                        "progress": st.get("progress"),
                        "total": st.get("total"),
                        "phase": st.get("phase"),
                        "project_name": st.get("project_name"),
                        "step": st.get("step"),
                        "episode_no": st.get("episode_no"),
                        "error": st.get("error") or "",
                        # live：分镜重试循环「当前正在用的提示词/镜头」快照（前端实时渲染用）
                        "live": st.get("live"),
                    })
        except Exception as e:  # noqa: BLE001 - 快照失败不中断流
            logger.debug("events/stream 快照失败：%s", e)
        return {"tasks": tasks, "count": len(tasks), "ts": time.time()}

    def _gen():
        last = None
        yield ": connected\n\n"
        while True:
            try:
                payload = json.dumps(_snapshot(), ensure_ascii=False)
            except Exception:  # noqa: BLE001
                payload = '{"tasks": []}'
            if payload != last:
                yield "data: %s\n\n" % payload
                last = payload
            else:
                yield ": keep-alive\n\n"
            time.sleep(1.0)

    return Response(stream_with_context(_gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "Connection": "keep-alive",
                             "X-Accel-Buffering": "no"})
