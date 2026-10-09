# -*- coding: utf-8 -*-
"""LLM 配置（读写 / 测试 / 清空）API 蓝图（Blueprint 拆分第十三批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @llm_bp.route。
测试/保存逻辑复用 routes/ai.py 的实现（单向依赖：llm → ai，无环）。日志器用 _app_logger()。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request

import ai_config
from config import AI_CONFIG_PATH, LLM_CONFIG_PATH
from routes.ai import _save_ai_module, api_ai_test

llm_bp = Blueprint('llm', __name__)

LLM_NOT_CONFIGURED_GUIDE = (
    "尚未配置自定义 AI 接口。请点击页面右上角「AI 设置」，切换到对应模块后依次填写：\n"
    "① base_url：OpenAI 兼容接口地址，例如 https://api.deepseek.com/v1\n"
    "② api_key：接口密钥（保存后页面只显示脱敏结果）\n"
    "③ model：模型名，例如 deepseek-chat / gpt-4o-mini\n"
    "填写后先点「测试连接」，成功再点「保存配置」。文本分析 / 质检 / 对话总控 三个模型相互独立。"
)
@llm_bp.route('/api/llm/config', methods=['GET'])
def api_llm_config_get():
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
    view["guide"] = None if view["configured"] else LLM_NOT_CONFIGURED_GUIDE
    view["deprecated"] = "该接口为兼容旧前端保留，等价于 /api/ai/config 的 text 模块"
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(AI_CONFIG_PATH)})
@llm_bp.route('/api/llm/config', methods=['POST'])
def api_llm_config_save():
    """兼容旧前端：强制保存 text 模块（= 文本分析模型 = LLM 引擎）

    旧实现是 `data["module"]="text"; return api_ai_config_save()`，
    但 `api_ai_config_save` 内部会重新从 `request.json` 取值，本地 dict 的修改
    完全无效 —— 结果是旧接口永远存不进 text 模块（调用方不传 module 时直接报
    "unknown module：(空)"）。这里改为把改写后的 data 显式传进共用的实现。
    """
    data = dict(request.json or {})
    data["module"] = "text"
    return _save_ai_module(data)
@llm_bp.route('/api/llm/config/clear', methods=['POST'])
def api_llm_config_clear():
    cfg = ai_config.clear_module(AI_CONFIG_PATH, module="text", legacy_path=LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(AI_CONFIG_PATH),
                    "message": "文本分析模型配置已清除"})
@llm_bp.route('/api/llm/test', methods=['POST'])
def api_llm_test():
    data = request.json or {}
    data["module"] = "text"
    data.setdefault("probe", "text")
    return api_ai_test()
