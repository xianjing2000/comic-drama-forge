# -*- coding: utf-8 -*-
"""任务队列（列表 / 详情 / 续跑预览） API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @tasks_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import task_store
from config import TASKS_DB_PATH, TASK_UNIT_MIN_BYTES
from routes._shared import _task_analytics_hook, _task_queue_status

tasks_bp = Blueprint('tasks', __name__)

task_db = task_store.get_store(TASKS_DB_PATH, on_change=_task_analytics_hook)
@tasks_bp.route('/api/tasks', methods=['GET'])
def api_tasks_list():
    """查询任务列表（可按项目 / 状态 / 类型过滤），用于重启后查看进度与续跑提示"""
    project = (request.args.get('project') or '').strip()
    status = (request.args.get('status') or '').strip()
    kind = (request.args.get('kind') or '').strip()
    try:
        limit = max(1, min(500, int(request.args.get('limit') or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        items = task_db.list(project=project or None, status=status or None,
                             kind=kind or None, limit=limit)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"任务查询失败：{e}"}), 500
    return jsonify({"success": True, "count": len(items), "items": items,
                    "queue": _task_queue_status()})
@tasks_bp.route('/api/tasks/<task_id>', methods=['GET'])
def api_task_detail(task_id):
    """查询单个任务详情（含单元级进度，用于展示断点续跑可跳过的部分）"""
    try:
        t = task_db.get(task_id)
        if not t:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        units = task_db.list_units(task_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"任务查询失败：{e}"}), 500
    done = [u for u in units if u.get("status") == task_store.ST_DONE]
    t["units"] = units
    t["unit_summary"] = {"total": len(units), "done": len(done),
                         "pending": len(units) - len(done)}
    return jsonify({"success": True, "task": t})
@tasks_bp.route('/api/tasks/<task_id>/resume-preview', methods=['GET'])
def api_task_resume_preview(task_id):
    """断点续跑预检：给出该任务「已完成 / 待重跑」的单元清单

    判据以磁盘产物为准（产物存在且非空即视为已完成），
    因此即使任务状态表丢失，也能正确识别可跳过的部分。
    """
    try:
        t = task_db.get(task_id)
        if not t:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        units = task_db.list_units(task_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"查询失败：{e}"}), 500

    done_units, pending_units = [], []
    for u in units:
        path = u.get("result_path") or ""
        if path and task_store.is_unit_done(path, min_bytes=TASK_UNIT_MIN_BYTES):
            done_units.append(u.get("unit_key"))
        else:
            pending_units.append(u.get("unit_key"))
    return jsonify({"success": True, "task_id": task_id,
                    "status": t.get("status"),
                    "done_units": done_units, "pending_units": pending_units,
                    "resumable": t.get("status") in (task_store.ST_INTERRUPTED,
                                                     task_store.ST_FAILED)})
