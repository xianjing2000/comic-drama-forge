# -*- coding: utf-8 -*-
"""提示词模板 / 增强配置 API 蓝图（Blueprint 拆分第三批）。

5 条路由：/api/prompts（模板列表·读取·保存·重置）+ /api/prompt-enhance/config。
依赖全是叶子：prompt_templates、config（PROMPT_ENHANCE_CONFIG_PATH、
_prompt_enhance_file_flags、save_prompt_enhance_config）与 routes/_shared._body。

⚠️ URL 规则与响应体一字不改，只把 @prompt_bp.route 换成 @prompt_bp.route。
"""
import os

from flask import Blueprint, jsonify, request

import prompt_templates
from config import (PROMPT_ENHANCE_CONFIG_PATH, _prompt_enhance_file_flags,
                    save_prompt_enhance_config)
from routes._shared import _body

prompt_bp = Blueprint('prompt', __name__)

@prompt_bp.route('/api/prompt_enhance/config', methods=['GET', 'POST'])
def api_prompt_enhance_config():
    """提示词增强总开关（前端「AI 配置」页的开关）。

    - GET  返回当前生效值 + 各来源（env 覆盖 / 文件开关 / 代码默认），让页面如实标注状态；
    - POST body {enhance_enabled?, review_enabled?} 写文件级开关，**立即生效、无需重启**：
      prompt_enhance.enhance_enabled()/review_enabled() 每次调用都重读该文件。
      优先级：env（MJSCXT_PROMPT_ENHANCE / MJSCXT_PROMPT_MODEL_REVIEW，运维最高）> 文件 > 代码默认。
    """
    import prompt_enhance
    if request.method == "POST":
        data = request.json or {}
        if not any(k in data for k in ("enhance_enabled", "review_enabled")):
            return jsonify({"success": False, "error": "缺少 enhance_enabled / review_enabled"}), 400
        cfg = save_prompt_enhance_config(data)
    else:
        cfg = _prompt_enhance_file_flags()
    flags = _prompt_enhance_file_flags()
    return jsonify({
        "success": True,
        "config": cfg,
        "file_config": flags,
        "effective": {
            "enhance_enabled": prompt_enhance.enhance_enabled(),
            "review_enabled": prompt_enhance.review_enabled(),
        },
        "env_overridden": {
            "enhance": os.environ.get("MJSCXT_PROMPT_ENHANCE", "").strip() != "",
            "review": os.environ.get("MJSCXT_PROMPT_MODEL_REVIEW", "").strip() != "",
        },
        "config_path": os.path.abspath(PROMPT_ENHANCE_CONFIG_PATH),
        "message": "提示词增强开关已保存，立即对后续生成生效（无需重启）",
    })
@prompt_bp.route('/api/prompts/list', methods=['GET'])
def api_prompts_list():
    """列出全部可编辑提示词模板（text 为文件原文、含头注释，供编辑器回显）"""
    try:
        return jsonify({"success": True, "prompts": prompt_templates.list_prompts()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500
@prompt_bp.route('/api/prompts/get', methods=['GET'])
def api_prompts_get():
    """读取单个模板：GET /api/prompts/get?name=script_rewrite_rules"""
    try:
        name = (request.args.get("name") or "").strip()
        if name not in prompt_templates.REGISTRY:
            return jsonify({"success": False,
                            "error": f"未知的提示词模板：{name}"
                                     f"（可选：{list(prompt_templates.REGISTRY)}）"}), 400
        entry = prompt_templates.REGISTRY[name]
        # 回显与 /api/prompts/list 同源（文件原文，所见即所存）；
        # 实际送模型的生效文本是 prompt_templates.load(name)（已剥头注释）。
        rows = {r["name"]: r for r in prompt_templates.list_prompts()}
        return jsonify({"success": True, "name": name,
                        "title": entry["title"],
                        "text": (rows.get(name) or {}).get("text") or "",
                        "variables": entry["variables"],
                        "source": prompt_templates.source_of(name)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500
@prompt_bp.route('/api/prompts/save', methods=['POST'])
def api_prompts_save():
    """保存模板为用户覆盖：{name, text}。text 非空、name 在注册表、注册变量占位符仍在，否则 400。"""
    try:
        data = _body()
        name = str(data.get("name") or "").strip()
        text = data.get("text")
        if name not in prompt_templates.REGISTRY:
            return jsonify({"success": False,
                            "error": f"未知的提示词模板：{name}"
                                     f"（可选：{list(prompt_templates.REGISTRY)}）"}), 400
        if not isinstance(text, str) or not text.strip():
            return jsonify({"success": False, "error": "text 不能为空"}), 400
        # 注册变量占位符必须仍在：{script_data} 之类被删掉，运行时注入会静默失效
        # （生成/质检链路对缺失占位符不做兜底补写，必须在这里拦住）。
        missing = [v for v in prompt_templates.REGISTRY[name]["variables"]
                   if ("{" + v + "}") not in text]
        if missing:
            return jsonify({"success": False,
                            "error": "模板缺少必需占位符："
                                     + "、".join("{" + v + "}" for v in missing)
                                     + "（这些变量由程序在运行时注入，删除会使对应注入失效）"}), 400
        path = prompt_templates.save_override(name, text)
        return jsonify({"success": True, "name": name, "source": "override",
                        "override_path": os.path.abspath(path),
                        "length": len(text),
                        "message": "提示词模板已保存（下一次生成/质检即生效，无需重启）"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500
@prompt_bp.route('/api/prompts/reset', methods=['POST'])
def api_prompts_reset():
    """删除用户覆盖、恢复出厂默认模板：{name}"""
    try:
        data = _body()
        name = str(data.get("name") or "").strip()
        if name not in prompt_templates.REGISTRY:
            return jsonify({"success": False,
                            "error": f"未知的提示词模板：{name}"
                                     f"（可选：{list(prompt_templates.REGISTRY)}）"}), 400
        removed = prompt_templates.reset_override(name)
        return jsonify({"success": True, "name": name, "removed": bool(removed),
                        "source": "default",
                        "message": "已恢复出厂默认模板" if removed else "本没有用户覆盖，无需恢复"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500

