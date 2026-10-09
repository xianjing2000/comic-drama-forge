# -*- coding: utf-8 -*-
"""跨集连贯性 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @continuity_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import continuity
import novel_to_script
from config import CONTINUITY_DIR, SCRIPT_DIR
from novel_parser import NovelParseError
from routes._shared import _ai_guide_response, _current_llm_client, _resolve_continuity_key

continuity_bp = Blueprint('continuity', __name__)

@continuity_bp.route('/api/continuity/<novel_id>', methods=['GET'])
def api_continuity_overview(novel_id):
    """项目级连贯性总览：bible / style_guide / 金句清单 / 口吻词典 / 运镜术语表 / 各集文件清单"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.continuity_overview(CONTINUITY_DIR, key)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", ""),
                 "novel_title": meta.get("title") or meta.get("name")})
    return jsonify(data)
@continuity_bp.route('/api/continuity/<novel_id>/<int:episode_no>', methods=['GET'])
def api_continuity_episode(novel_id, episode_no):
    """单集连贯性：上集摘要卡 / 本集 state_in·state_out / 跨集一致性校验结果"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.episode_continuity_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)
@continuity_bp.route('/api/continuity/<novel_id>/<int:episode_no>/revalidate', methods=['POST'])
def api_continuity_revalidate(novel_id, episode_no):
    """对已落盘剧本重跑「相邻集六类一致性校验」（D⑨ 可选闸门；body.rewrite=true 时命中问题会局部重写）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法执行一致性校验")
    try:
        script = novel_to_script.load_episode_script(SCRIPT_DIR, key, episode_no)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 404

    state = continuity.load_state(CONTINUITY_DIR, key, episode_no) or {
        "state_in": script.get("state_in") or {},
        "state_out": script.get("state_out") or {},
        "key_events": (script.get("continuity") or {}).get("key_events") or [],
    }
    prev_state = continuity.load_state(CONTINUITY_DIR, key, episode_no - 1)
    prev_script = continuity._load_prev_script(SCRIPT_DIR, key, episode_no - 1)
    rule_issues = continuity.rule_timeline_check(prev_state, state, script) if prev_state else []
    events = []
    validation = continuity.validate_continuity(
        client, script, prev_script, prev_state, state, episode_no,
        events=events, rule_issues=rule_issues)
    validation["quotes"] = continuity.check_quotes_in_script(CONTINUITY_DIR, key, script, episode_no)

    rewrite = {"triggered": False, "rounds": 0, "rewritten_shot_ids": [], "notes": []}
    body = request.json or {}
    if body.get("rewrite") and validation.get("rewrite_needed") and int(episode_no) > 1:
        issues = [i for i in (validation.get("issues") or [])
                  if i.get("severity") in ("high", "medium")]
        ctx = continuity.build_continuity_context(
            CONTINUITY_DIR, key, episode_no, script.get("style") or "3D动漫渲染")
        rw = continuity.rewrite_shots_for_issues(client, script, issues, episode_no,
                                                 continuity_ctx=ctx, events=events,
                                                 shot_ids=validation.get("rewrite_shot_ids"))
        if rw.get("rewritten_shot_ids"):
            rewrite.update({"triggered": True, "rounds": 1,
                            "rewritten_shot_ids": rw["rewritten_shot_ids"],
                            "notes": rw.get("notes") or []})
            novel_to_script.save_episode_script(script, SCRIPT_DIR, key, episode_no, key)
            state = continuity.extract_episode_state(client, script, prev_state, episode_no,
                                                     events=events)
            script["state_in"], script["state_out"] = state["state_in"], state["state_out"]
            rule_issues = continuity.rule_timeline_check(prev_state, state, script) if prev_state else []
            validation = continuity.validate_continuity(
                client, script, prev_script, prev_state, state, episode_no,
                events=events, rule_issues=rule_issues)
            validation["quotes"] = continuity.check_quotes_in_script(
                CONTINUITY_DIR, key, script, episode_no)
            continuity.save_state(CONTINUITY_DIR, key, state)
            novel_to_script.save_episode_script(script, SCRIPT_DIR, key, episode_no, key)
        else:
            rewrite["error"] = rw.get("error") or "未产生修改"

    validation["rewrite"] = rewrite
    validation["generated_at"] = continuity._now()
    continuity.save_json(continuity.validation_path(CONTINUITY_DIR, key, episode_no), validation)
    # P0-3 剧本↔原著一致性摘要：直接复用生成时写入剧本的结论（三件套为确定性计算，
    # 不在本接口重算，避免无章节正文时误报）
    consistency = (script.get("metadata") or {}).get("script_consistency") or {}
    return jsonify({"success": True, "novel_id": novel_id, "episode_no": episode_no,
                    "project_key": key, "validation": validation, "rewrite": rewrite,
                    "consistency": consistency,
                    "events": events})
