# -*- coding: utf-8 -*-
"""字幕校验 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @caption_verify_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import caption_verify
from routes._shared import _body, _project_or_400, _safe_project

caption_verify_bp = Blueprint('caption_verify', __name__)

@caption_verify_bp.route('/api/caption_verify/status', methods=['GET'])
def api_caption_verify_status():
    """查询校对环境与某集校对状态：GET ?project=&episode_no=

    available/reasons 始终返回（前端可先探环境再决定要不要跑）；
    project 缺省时 result=None（G4 口径：判空看原始入参，不走 _safe_project('') 兜底键）。
    """
    raw_project = (request.args.get('project') or '').strip()
    try:
        episode_no = int(request.args.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    episode_no = max(1, episode_no)
    env = caption_verify.available()
    project = _safe_project(raw_project) if raw_project else ""
    result = caption_verify.get_result(project, episode_no) if project else None
    return jsonify({"success": True, "project": project, "episode_no": episode_no,
                    "available": env.get("available"),
                    "reasons": env.get("reasons") or [],
                    "exe": env.get("exe"), "model": env.get("model"),
                    "result": result})
@caption_verify_bp.route('/api/caption_verify/run', methods=['POST'])
def api_caption_verify_run():
    """起一个后台校对任务：POST {project, episode_no}

    环境不齐 400（reasons 直接给前端指引装哪件）；同集已在跑 409。
    任务进度与结论走 GET /api/caption_verify/status 轮询。
    """
    data = _body()
    project, err = _project_or_400(data.get('project') or '', field_name="project")
    if err is not None:
        return err
    try:
        episode_no = int(data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    episode_no = max(1, episode_no)
    env = caption_verify.available()
    if not env.get("available"):
        return jsonify({"success": False,
                        "error": "caption 校对环境不可用：" + "；".join(env.get("reasons") or []),
                        "reasons": env.get("reasons") or []}), 400
    started = caption_verify.start_verify(project, episode_no)
    if not started.get("started"):
        return jsonify({"success": False, "error": started.get("error") or "任务启动失败",
                        "task_key": started.get("task_key")}), 409
    return jsonify({"success": True, "started": True,
                    "task_key": started.get("task_key"),
                    "project": project, "episode_no": episode_no,
                    "note": "已开始后台校对；结论请轮询 /api/caption_verify/status"})
