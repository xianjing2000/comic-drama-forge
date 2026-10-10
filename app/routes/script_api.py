# -*- coding: utf-8 -*-
'''剧本生成 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
import logging
from flask import Blueprint, jsonify, request, send_file, abort, current_app  # noqa: F401
import os    # noqa: F401
from PIL import Image  # noqa: F401
import sys   # noqa: F401
import re    # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
import threading   # noqa: F401
import hashlib     # noqa: F401
import shutil      # noqa: F401
import random      # noqa: F401
import base64      # noqa: F401
import datetime    # noqa: F401
import traceback   # noqa: F401
import subprocess  # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from agent_core import logger
from llm_client import LLMError
from routes._shared import _ai_gate_or_400
from routes._shared import _ai_guide_response
from routes._shared import _apply_project_settings
from routes._shared import _body
from routes._shared import _current_llm_client
from routes._shared import _qc_load_cfg
from routes._shared import _safe_project
import qc_client
from script_generator import ScriptGenerator  # noqa: F401  script_gen 的构造依赖

# 注意：本对象必须在 import ScriptGenerator 之后、任何使用之前构造
script_gen = ScriptGenerator()

logger = logging.getLogger(__name__)

script_api_bp = Blueprint('script_api', __name__)

@script_api_bp.route('/api/script/generate', methods=['POST'])
def api_generate_script():
    # P0-5 门禁：文本分析模型未配置 / 端点不可达 → 直接阻断，不给「静默跑进执行中」的机会
    _gate = _ai_gate_or_400("script")
    if _gate is not None:
        return _gate
    data = _body()
    theme = data.get('theme', '')
    episodes = data.get('episodes', 1)
    duration = data.get('duration', 60)
    style = data.get('style', '古风仙侠')
    style = _apply_project_settings(style, _safe_project(data.get('project_name') or (theme or '')[:20] or 'project'))

    if not theme:
        return jsonify({"error": "请提供主题"}), 400

    try:
        # 统一走前端「AI 设置 → 文本分析模型」的密钥（OpenAI 兼容，任意厂商），
        # 不再读环境变量 ANTHROPIC_API_KEY / 硬编码 CLAUDE_MODEL。
        client = _current_llm_client()
        script = script_gen.generate_script_with_client(
            client, theme=theme, episodes=episodes,
            duration_per_episode=duration, style=style
        )
        project_name = _safe_project(theme[:20])
        script_path = script_gen.save_script(script, project_name,
                                             provider="openai-compatible")
        script["metadata"]["script_path"] = script_path

        # S1：剧本质检接线（原为死代码——生产路径只调无质检的 generate_script，
        # 结构缺陷直接流入分镜/视频后才暴露）。这里在剧本落盘后跑一次 check_script，
        # 按 script_qc_ready 门控（与 image/video qc 同模式，开关关时 no-op、不报错），
        # 结果透出给前端如实回显「剧本是否已质检」。单次检测（非 generate_script_with_qc
        # 的 3 次重试+AI 判断重链路），止血优先、行为保守。
        qc_cfg = _qc_load_cfg()
        qc_result = {"skipped": True, "reason": "剧本质检未启用"}
        if qc_client.script_qc_ready(qc_cfg):
            qc_result = qc_client.check_script(
                script_data=script, style=style, target_duration=duration, cfg=qc_cfg)
            logger.info("剧本质检：passed=%s score=%s reason=%s",
                            qc_result.get("passed"), qc_result.get("score"),
                            qc_result.get("reason"))

        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "script_qc_active": qc_client.script_qc_ready(qc_cfg),
            "qc_result": qc_result,
        })
    except LLMError as e:
        # 未配置「文本分析模型」或密钥错误：给引导（400）而非裸 500，
        # 引导用户去 AI 设置配置 base_url / api_key / model。
        # _ai_guide_response 已返回 (jsonify, code) 元组，直接透传。
        logger.error(f"剧本生成 LLM 调用失败: {e}")
        return _ai_guide_response(str(e))
    except Exception as e:
        logger.error(f"生成剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


@script_api_bp.route('/api/script/fallback', methods=['POST'])
def api_fallback_script():
    """一键加载本地兜底剧本（免 API Key 演示）"""
    try:
        script = script_gen.load_fallback_script()
        if not script:
            return jsonify({"error": "未找到本地兜底剧本（output/scripts/剑心初醒_兼容版.json）"}), 404
        data = request.json or {}
        project_name = _safe_project(data.get('project_name') or script.get("title", "fallback"))
        script_path = script_gen.save_script(script, project_name)
        script.setdefault("metadata", {})["script_path"] = script_path
        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "fallback": True
        })
    except Exception as e:
        logger.error(f"加载兜底剧本失败: {e}")
        return jsonify({"error": str(e)}), 500
