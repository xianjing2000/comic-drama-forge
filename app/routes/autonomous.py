# -*- coding: utf-8 -*-
'''托管控制面 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
from flask import Blueprint, jsonify, request, send_file, abort  # noqa: F401
import os    # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from flask import Flask, request, jsonify, send_file, abort, redirect, send_from_directory
from routes._shared import (  # noqa: F401  再导出：既有装饰器/调用零改动
    _autopilot_guard, _friendly_error, _prompt_memory_dead_count,
    _prompt_memory_used_total, _prompt_memory_view)
from routes._shared import _ai_gate_or_400, _app_logger, _project_or_400, _safe_project, _shot_seq, _trash_move  # noqa: F401  再导出
from style_helpers import _style_aspect_confirmed, _style_aspect_guard  # noqa: F401, E402
import autopilot
import autonomous
import os

autonomous_bp = Blueprint('autonomous', __name__)

@autonomous_bp.route('/api/autonomous/start', methods=['POST'])
@_autopilot_guard
def api_autonomous_start():
    """一键启动全自动生产：上传小说后，AI对话定风格，然后一键启动24h自动生产"""
    data = (request.get_json(silent=True) or {})
    # A-01（F-01）：统一走 _project_or_400（缺失/越界 → 400，不落共享 'project' 命名空间）
    project_name, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    novel_id = str(data.get('novel_id') or '').strip()
    # ⭐ 2026-10-12 T11：入参白名单**不再另写一份** —— 唯一权威是
    #    autopilot.PLAN_DEFAULTS（set_plan 实际接受的集合）。此前这里是一张手写清单，
    #    里面还留着 enable_keyframe / prewarm_asset_prompt / video_mode 这些**计划不接受**
    #    的键：请求过了这一关、却在 set_plan 被静默丢弃 —— 典型的「假通道」。
    #    现在：不在 PLAN_DEFAULTS 的键一律进 rejected，随响应显式回给调用方并记日志。
    _owned = {'project_name', 'novel_id'}
    _split = autopilot.split_plan_patch(
        {k: v for k, v in (data or {}).items() if k not in _owned})
    plan_overrides = _split["accepted"]
    rejected = _split["rejected"]
    _reasons = _split["reasons"]

    if not novel_id and project_name:
        # 尝试从现有计划获取 novel_id
        plan = autopilot.get_plan(project_name)
        novel_id = plan.get('novel_id', '')

    if not novel_id:
        return jsonify({"success": False, "error": "缺少 novel_id，请先上传小说"}), 400

    _g = _style_aspect_guard(project_name, override_style=str(plan_overrides.get('style') or ''))
    if _g is not None:
        return _g

    result = autonomous.start_autonomous(project_name or novel_id, novel_id, plan_overrides)
    # D5：返回给前端的错误统一脱敏，避免把异常栈/模块名直接显示在错误框里
    if isinstance(result, dict) and result.get('error'):
        result['error'] = _friendly_error(result['error'])
    # ⭐ 2026-10-12 T11：不支持的入参**不再静默丢弃** —— 逐键给原因，写进响应
    #    （ignored/reasons）并拼一句人话进 warning；同时记一条后端日志便于排查。
    if rejected and isinstance(result, dict):
        _tip = "以下入参未生效：" + "；".join(
            "%s（%s）" % (k, _reasons.get(k) or "不是托管计划字段，写它不会生效")
            for k in sorted(rejected))
        _app_logger().warning("一键启动：%d 个入参不受支持，已显式回给调用方：%s",
                              len(_reasons), _tip)
        result["ignored"] = _reasons
        result["rejected"] = sorted(_reasons)
        result["warning"] = (_tip if not str(result.get("warning") or "").strip()
                             else str(result.get("warning")).strip() + "；" + _tip)
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@autonomous_bp.route('/api/autonomous/stop', methods=['POST'])
@_autopilot_guard
def api_autonomous_stop():
    """停止全自动生产"""
    data = (request.get_json(silent=True) or {})
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    result = autonomous.stop_autonomous(project)
    return jsonify(result)


@autonomous_bp.route('/api/autonomous/resume', methods=['POST'])
@_autopilot_guard
def api_autonomous_resume():
    """恢复全自动生产"""
    data = (request.get_json(silent=True) or {})
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    result = autonomous.resume_autonomous(project)
    return jsonify(result)


@autonomous_bp.route('/api/autonomous/status', methods=['GET'])
@_autopilot_guard
def api_autonomous_status():
    """查询全自动生产状态"""
    project, err = _project_or_400(request.args.get('project_name', ''))
    if err is not None:
        return err
    result = autonomous.status(project)
    return jsonify({"success": True, **result})


@autonomous_bp.route('/api/autonomous/chat', methods=['POST'])
@_autopilot_guard
def api_autonomous_chat():
    """AI 对话指令解析：将用户的自然语言指令转化为生产动作"""
    data = (request.get_json(silent=True) or {})
    message = str(data.get('message') or '').strip()
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err

    if not message:
        return jsonify({"success": False, "error": "消息不能为空"}), 400

    result = autonomous.interpret_chat_command(message, project)
    return jsonify(result)


@autonomous_bp.route('/api/autonomous/report/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report(project_name):
    """获取生产报告"""
    project, err = _project_or_400(project_name)
    if err is not None:
        return err
    episode_no = request.args.get('episode_no', type=int)
    result = autonomous.generate_report(project, episode_no)
    return jsonify(result)


@autonomous_bp.route('/api/autonomous/report/export/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report_export(project_name):
    """导出生产报告"""
    project, err = _project_or_400(project_name)
    if err is not None:
        return err
    fmt = request.args.get('format', 'json')
    filepath = autonomous.export_report(project, fmt)
    if not filepath:
        return jsonify({"success": False, "error": "暂无生产记录"}), 404
    return send_file(filepath, as_attachment=True,
                     download_name=os.path.basename(filepath))


@autonomous_bp.route('/api/autonomous/projects', methods=['GET'])
@_autopilot_guard
def api_autonomous_projects():
    """列出所有有生产记录的项目"""
    projects = autonomous.list_all_projects()
    return jsonify({"success": True, "projects": projects})


@autonomous_bp.route('/api/autonomous/deliverables/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_deliverables(project_name):
    """获取项目的交付物列表"""
    project, err = _project_or_400(project_name)
    if err is not None:
        return err
    deliverables = autonomous.get_deliverables(project)
    return jsonify({"success": True, "project": project, "deliverables": deliverables})
