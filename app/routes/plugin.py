# -*- coding: utf-8 -*-
"""插件 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @plugin_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import plugin_registry
from routes._shared import _load_script_for, _safe_project

plugin_bp = Blueprint('plugin', __name__)

@plugin_bp.route('/api/plugins', methods=['GET'])
def api_plugins():
    """插件目录（内置环节 + plugins/ 目录下用户自定义 Agent）"""
    try:
        data = plugin_registry.catalog()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"插件目录读取失败：{e}"}), 500
    return jsonify({"success": True, **data,
                    "dependency_order": plugin_registry.get_registry().dependency_order()})
@plugin_bp.route('/api/plugins/run', methods=['POST'])
def api_plugins_run():
    """执行指定插件（把剧本等上下文传入插件，返回插件产出）"""
    data = request.json or {}
    pid = str(data.get('plugin_id') or '').strip()
    if not pid:
        return jsonify({"success": False, "error": "缺少 plugin_id"}), 400
    ctx = data.get('context') if isinstance(data.get('context'), dict) else {}
    project = _safe_project(data.get('project_name') or '')
    if project and 'script' not in ctx:
        ctx['script'] = _load_script_for(project, data.get('episode_no'))
        ctx['project_name'] = project
    try:
        result = plugin_registry.run(pid, ctx)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"插件执行失败：{e}"}), 500
    return jsonify({"success": bool(result.get("ok")), "plugin_id": pid, "result": result})
