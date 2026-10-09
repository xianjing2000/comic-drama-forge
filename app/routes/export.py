# -*- coding: utf-8 -*-
"""导出 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @export_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import nle_export
from config import PROJECT_OUTPUT_DIR
from flask import abort
from routes._shared import _app_logger, _autopilot_guard, _load_script_for, _project_or_400, _safe_project

export_bp = Blueprint('export', __name__)

def _serve_attachment(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的附件下载统一入口（as_attachment + download_name）。

    与 _serve_safe 防护逻辑相同，但额外指定 as_attachment=True 与 download_name。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    send_kw.setdefault("as_attachment", True)
    send_kw.setdefault("download_name", os.path.basename(target))
    return send_file(target, **send_kw)
@export_bp.route('/api/export/run', methods=['POST'])
def api_export_run():
    """一键导出：剪映草稿 + FCPML(FCPXML 风格) + SRT + 帧序列清单

    body: {project_name, episode_no?, formats?: ["jianying","fcpxml","srt","frames"]}
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    formats = data.get('formats')
    # B-17 P2-13：传集号给 nle_export，按集号过滤视频目录，避免跨集混用素材
    ep_no = data.get('episode_no')
    try:
        results = nle_export.export_all(project, script,
                                        formats=formats if isinstance(formats, list) else None,
                                        episode=ep_no)
    except Exception as e:  # noqa: BLE001
        _app_logger().exception("NLE 导出失败")
        return jsonify({"success": False, "error": f"导出失败：{e}"}), 500
    return jsonify({"success": bool(results.get("ok")), "project": project, **results})
@export_bp.route('/api/export/list', methods=['GET'])
def api_export_list():
    """导出记录列表（可按项目过滤）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else None
    try:
        items = nle_export.list_exports(project)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 500
    # 前端（ExportPage / 工作台 ExportTab）统一消费 `files` 字段：
    # 这里在保留 `items` 原始结构的同时，补一份前端可直接渲染的规范化列表。
    files = []
    for it in items:
        p = it.get("path") or ""
        name = os.path.basename(p) if p else ""
        files.append({
            "format": it.get("format"),
            "filename": name,
            "exists": bool(p and os.path.isfile(p)),
            "path": p,
            "dir": it.get("dir"),
            "project": it.get("project"),
            "exported_at": it.get("exported_at"),
            "shot_count": it.get("shot_count"),
            "total_sec": it.get("total_sec"),
            "size_mb": it.get("size_mb"),
        })

    return jsonify({"success": True, "count": len(files), "items": items, "files": files})
@export_bp.route('/api/export/download/<path:filename>')
def api_export_download(filename):
    """导出产物下载（限导出根目录内）"""
    return _serve_attachment(nle_export.EXPORT_DIR, filename)
@export_bp.route('/api/export/current', methods=['POST'])
@_autopilot_guard
def api_export_current():
    """导出当前项目的所有格式"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project_name', '')
    formats = data.get('formats', ['fcpml', 'edl', 'json'])

    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err

    export_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project_name)
    os.makedirs(export_dir, exist_ok=True)

    result = {}
    for fmt in formats:
        if fmt == 'fcpml':
            filename = f"{project_name}_fcpml.xml"
        elif fmt == 'edl':
            filename = f"{project_name}_edl.edl"
        elif fmt == 'json':
            filename = f"{project_name}_timeline.json"
        else:
            continue
        filepath = os.path.join(export_dir, filename)
        if os.path.exists(filepath):
            result[fmt] = filepath

    return jsonify({"success": True, "files": result})
