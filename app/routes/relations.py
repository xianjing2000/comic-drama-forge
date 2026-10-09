# -*- coding: utf-8 -*-
"""人物关系图（读写 · 同步 · 冲突检测 · SVG 导出） API 蓝图（Blueprint 拆分第十一批，2026-10-08）。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @relations_bp.route。
跨域助手在 routes/_shared.py；日志器一律用上下文安全的 _app_logger()。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

from relation_manager import RelationConflictDetector, RelationManager
from routes._shared import PROJECT_OUTPUT_DIR, _autopilot_guard, _project_or_400

relations_bp = Blueprint('relations', __name__)

@relations_bp.route('/api/relations/graph', methods=['GET'])
@_autopilot_guard
def api_get_relation_graph():
    """获取角色关系图谱数据"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    graph = rmgr.get_graph_data()
    return jsonify({"success": True, "graph": graph})
@relations_bp.route('/api/relations', methods=['GET'])
@_autopilot_guard
def api_list_relations():
    """列出项目所有角色关系"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    relations = rmgr.get_all_relations()
    return jsonify({"success": True, "relations": relations})
@relations_bp.route('/api/relations', methods=['POST'])
@_autopilot_guard
def api_add_relation():
    """添加角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    char_a = data.get('char_a', '')
    char_b = data.get('char_b', '')
    rel_type = data.get('type', 'friend')
    strength = data.get('strength', 'medium')
    note = data.get('note', '')

    if not char_a or not char_b:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    rel_id = rmgr.add_relation(char_a, char_b, rel_type, strength, note)
    return jsonify({"success": True, "relation_id": rel_id})
@relations_bp.route('/api/relations/<rel_id>', methods=['PUT'])
@_autopilot_guard
def api_update_relation(rel_id):
    """更新角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    try:
        rmgr.update_relation(rel_id, **{k: v for k, v in data.items() if k != 'project'})
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404
@relations_bp.route('/api/relations/<rel_id>', methods=['DELETE'])
@_autopilot_guard
def api_delete_relation(rel_id):
    """删除角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    try:
        rmgr.delete_relation(rel_id)
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404
@relations_bp.route('/api/relations/sync', methods=['POST'])
@_autopilot_guard
def api_sync_relations():
    """同步角色数据到关系库"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    characters = data.get('characters', {})

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    rmgr.sync_characters(characters)
    return jsonify({"success": True})
@relations_bp.route('/api/relations/conflicts', methods=['GET'])
@_autopilot_guard
def api_check_relation_conflicts():
    """检测关系冲突"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    detector = RelationConflictDetector(rmgr)
    summary = detector.get_conflict_summary()
    return jsonify({"success": True, "conflicts": summary})
@relations_bp.route('/api/relations/svg', methods=['GET'])
@_autopilot_guard
def api_export_relation_svg():
    """导出关系图谱为SVG"""
    project_name = request.args.get('project')
    width = int(request.args.get('width', 800))
    height = int(request.args.get('height', 600))

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    svg_content = rmgr.export_svg(width, height)
    return jsonify({"success": True, "svg": svg_content})
