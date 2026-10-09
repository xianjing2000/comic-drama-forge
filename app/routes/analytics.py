# -*- coding: utf-8 -*-
"""数据统计看板 API 蓝图（Blueprint 拆分第十二批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @analytics_bp.route。
日志器用上下文安全的 _app_logger()。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import analytics
from routes._shared import _safe_project

analytics_bp = Blueprint('analytics', __name__)

@analytics_bp.route('/api/analytics/summary', methods=['GET'])
def api_analytics_summary():
    """全局或按项目的成本/耗时汇总"""
    project = (request.args.get('project') or '').strip()
    try:
        limit = max(1, min(500, int(request.args.get('recent') or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        data = analytics.summarize(project=project or None, recent_limit=limit)
        data["projects"] = analytics.list_projects()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"统计读取失败：{e}"}), 500
    return jsonify({"success": True, **data})
@analytics_bp.route('/api/analytics/project/<path:project_name>', methods=['GET'])
def api_analytics_project(project_name):
    """单项目成本/耗时"""
    project_name = _safe_project(project_name)
    try:
        data = analytics.summarize(project=project_name)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"统计读取失败：{e}"}), 500
    return jsonify({"success": True, **data})
@analytics_bp.route('/api/analytics/event', methods=['POST'])
def api_analytics_record():
    """手工登记一条耗时事件（供前端/外部脚本补充统计）"""
    data = request.json or {}
    ok = analytics.record_event(
        kind=str(data.get('kind') or 'other'),
        project=_safe_project(data.get('project') or ''),
        label=str(data.get('label') or ''),
        duration_sec=float(data.get('duration_sec') or 0),
        units=int(data.get('units') or 0),
        success=bool(data.get('success', True)),
        meta=data.get('meta') if isinstance(data.get('meta'), dict) else None,
    )
    return jsonify({"success": bool(ok)})
@analytics_bp.route('/api/analytics/reset', methods=['POST'])
def api_analytics_reset():
    """清空统计（project 为空则整体清空）"""
    data = request.json or {}
    project = _safe_project(data.get('project') or '') if data.get('project') else None
    return jsonify({"success": True, **analytics.reset(project=project)})
