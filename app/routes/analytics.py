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
    data = (request.get_json(silent=True) or {})
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
    data = (request.get_json(silent=True) or {})
    project = _safe_project(data.get('project') or '') if data.get('project') else None
    return jsonify({"success": True, **analytics.reset(project=project)})


@analytics_bp.route('/api/analytics/failure_summary', methods=['GET'])
def api_analytics_failure_summary():
    """失败码聚合看板（E-2，2026-10-16）：把死信队列按**结构化失败码**归类下发前端。

    背景：`failure_codes` 此前只在 `pipeline` 写死信时打 `code`/`code_label`，`summarize()`
    全仓无调用 —— 失败原因无法按码聚合展示。这里给一个只读端点补齐（不改任何写路径）。

    取数：`?project=` 指定单项目，缺省则汇总所有项目；只统计**未解决**的死信。
    口径：复用 `failure_codes.summarize`（死信项含 code/reason，classify 可直接吃）。
    全程 fail-open：任何异常返回 500 + 文案，不影响死信读写本身。
    """
    project = (request.args.get('project') or '').strip()
    try:
        import failure_codes
        import pipeline
        if project:
            projects = [_safe_project(project)]
        else:
            import autopilot
            projects = [p.get('project') for p in (autopilot.list_plans() or [])
                        if p.get('project')]
        items = []
        for pj in projects:
            for d in (pipeline.list_dead_letters(pj) or []):
                if not d.get('resolved'):
                    items.append(d)
        summary = failure_codes.summarize(items)
        counts = summary.get("counts") or {}
        codes = [{"code": c, "label": failure_codes.explain(c), "count": n}
                 for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        return jsonify({"success": True, "project": project, "projects": len(projects),
                        "open_dead_letters": len(items), "summary": summary, "codes": codes})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"失败汇总读取失败：{e}"}), 500
