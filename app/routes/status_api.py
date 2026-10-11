# -*- coding: utf-8 -*-
'''系统状态 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
import logging
from flask import Blueprint, jsonify, request, send_file, abort, current_app  # noqa: F401
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
        "interrupted_tasks": job_state.interrupted_tasks,
    })
