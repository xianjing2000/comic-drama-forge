# -*- coding: utf-8 -*-
"""总控内核（工具表 / 任务 / 对话） API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @agent_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import agent_core
import ai_chat
import ai_config
from config import AI_CHAT_HISTORY_PATH, AI_CONFIG_PATH, LLM_CONFIG_PATH, LLM_REQUEST_TIMEOUT
from datetime import datetime
from routes.ai import LLM_NOT_CONFIGURED_GUIDE_MAP, _chat_project, _chat_state
from routes._shared import _ai_gate_or_400

agent_bp = Blueprint('agent', __name__)

@agent_bp.route('/api/agent/tools', methods=['GET'])
def api_agent_tools():
    """列出总控 AI 可调用的工具与当前护栏状态（供前端展示能力边界）"""
    return jsonify({
        "success": True,
        "count": len(agent_core.TOOLS),
        "tools": agent_core.tool_index(),
        "guards": {
            "max_steps": agent_core.MAX_STEPS,
            "max_expensive": agent_core.MAX_EXPENSIVE,
            "max_turn_sec": agent_core.MAX_TURN_SEC,
            "cooldown_sec": agent_core.COOLDOWN_SEC,
        },
        "kill": agent_core.kill_state(),
    })
@agent_bp.route('/api/agent/kill', methods=['GET', 'POST'])
def api_agent_kill():
    """急停开关：一键中止所有正在跑的总控动作（无人值守时的刹车）"""
    if request.method == 'GET':
        return jsonify({"success": True, "kill": agent_core.kill_state()})
    data = request.json or {}
    on = bool(data.get("on", True))
    reason = str(data.get("reason") or "").strip() or ("手动急停" if on else "")
    return jsonify({"success": True, "kill": agent_core.set_kill(on, reason)})
@agent_bp.route('/api/agent/chat', methods=['POST'])
def api_agent_chat():
    """下发一条自然语言指令，总控 AI 自主决策并执行（异步任务，返回 job_id 供轮询）"""
    # P0-5 门禁：总控对话模型未配置 → 闸在门口，避免 issue 落库后才发现跑不动
    _gate = _ai_gate_or_400("chat")
    if _gate is not None:
        return _gate
    data = request.json or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message 不能为空"}), 400
    if len(message) > ai_chat.MAX_CHARS_PER_MESSAGE:
        message = message[:ai_chat.MAX_CHARS_PER_MESSAGE]

    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    # ⚠️ _chat_project 只认 project_name，而本接口/前端传的是 project。
    # 不转换的话总控会静默落到「上一个活跃项目」上，把 A 项目的指令干到 B 项目头上。
    _explicit = (data.get("project_name") or data.get("project") or "").strip()
    project = _chat_project({"project_name": _explicit} if _explicit else (data or {}), history)

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, "chat")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return jsonify({
            "success": False,
            "error": "「对话总控模型」尚未配置（base_url / api_key / model），总控无法自主执行",
            "guide": LLM_NOT_CONFIGURED_GUIDE_MAP["chat"],
            "need_config": True,
            "state": _chat_state(project),
        }), 400

    history["active_project"] = project
    ai_chat.append_message(history, "user", message, project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    started = agent_core.start_job(
        message=message,
        project=project,
        ep=ep,
        history=ai_chat.project_messages(history, project)[:-1],
        timeout=LLM_REQUEST_TIMEOUT,
    )
    if not started.get("ok"):
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": started.get("error"),
                        "state": _chat_state(project)}), 409
    return jsonify({"success": True, "job_id": started["job_id"], "project": project,
                    "state": _chat_state(project)})
@agent_bp.route('/api/agent/job/<job_id>', methods=['GET'])
def api_agent_job(job_id):
    """轮询总控任务进度（steps 逐条追加，status: running/done/failed/killed/timeout）"""
    job = agent_core.get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": f"未找到任务 {job_id}"}), 404
    # P1-1：最终回复已在 agent 线程内即时落盘（agent_core._finish → _persist_agent_reply）；
    # 这里仅作幂等兜底——线程内失败/尚未完成落盘时由 persist_job_reply 补写（共用同一份
    # 认领/去重逻辑，保证回复只追加一次，不重复不丢失）。前端刷新/离开不再导致回复丢失。
    if job.get("status") in ("done", "failed", "killed", "timeout") and job.get("reply"):
        agent_core.persist_job_reply(job_id)
    return jsonify({"success": True, **{k: v for k, v in job.items() if not k.startswith("_")}})
@agent_bp.route('/api/agent/log', methods=['GET'])
def api_agent_log():
    """查看总控 AI 的审计日志（干了什么、成功没有、花了多久）"""
    try:
        limit = max(1, min(int(request.args.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        limit = 100
    path = os.path.join(agent_core.AUDIT_DIR,
                        f"audit-{datetime.now().strftime('%Y%m%d')}.jsonl")
    items = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            for ln in lines:
                try:
                    items.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"读取审计日志失败：{e}"}), 500
    return jsonify({"success": True, "count": len(items), "items": items})
