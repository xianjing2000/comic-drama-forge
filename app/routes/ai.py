# -*- coding: utf-8 -*-
"""AI 配置 / AI 对话总控 API 蓝图（Blueprint 拆分第六批，2026-10-08）。

15 条 /api/ai/* 路由（配置读写与测试、密钥校验、自检、对话总控的收发与草稿/设置、
对话历史与归档），外加它们私有的 9 个助手（_ai_config_view、_save_ai_module、
_ai_credentials_verify、_sync_project_config_style、_chat_state、_chat_project、
_archive_project_key、_wm_load_cfg 等）。依赖闭包不含生成状态机。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @ai_bp.route。
"""
import json
import os
import time

from flask import Blueprint, current_app, jsonify, request

import ai_chat
import ai_config
import ai_credentials_db
import project_store
from config import AI_CONFIG_PATH, LLM_CONFIG_PATH, PROJECT_OUTPUT_DIR
from routes._shared import _app_logger, _body, _wm_load_cfg

ai_bp = Blueprint('ai', __name__)

import qc_client, re, video_watermark
from config import AI_CHAT_HISTORY_PATH, AI_MODULES, AI_SETTINGS_PATH, COMFYUI_URL, LLM_REQUEST_TIMEOUT, QC_CONFIG_PATH, WATERMARK_CONFIG_PATH
from llm_client import LLMClient, LLMError, LLMGatewayUnavailable
from routes._shared import AI_MODULE_LABEL, _ai_client_for_module
@ai_bp.route('/api/ai/config', methods=['GET'])
def api_ai_config_get():
    """读取统一 AI 设置（三模块，api_key 一律脱敏）"""
    return jsonify({"success": True, "config": _ai_config_view()})
@ai_bp.route('/api/ai/config/reveal', methods=['GET'])
def api_ai_config_reveal():
    """按需回显某个模块已保存的 api_key 明文（前端「眼睛」按钮点开时调用）。

    默认 GET /api/ai/config 仍一律脱敏（module_public_view 永不含明文）；
    本端点只在用户显式点「显示」时被调用，把明文回填进输入框——否则已保存的
    密钥在前端只是 placeholder 圆点，切 type 什么都显不出来。
    本应用是本地单用户工具（仅 127.0.0.1），密钥明文本就只存在本机。
    """
    module = (request.args.get('module') or '').strip()
    if module not in ai_config.MODULES:
        return jsonify({"success": False, "error": f"未知的 AI 模块：{module}"}), 400
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, module)
    key = ep.get("api_key") or ""
    return jsonify({"success": True, "module": module,
                    "has_api_key": bool(key), "api_key": key})
@ai_bp.route('/api/ai/config', methods=['POST'])
def api_ai_config_save():
    """保存单个模块：{module: text|qc|chat, base_url, model, api_key?}"""
    return _save_ai_module(request.json or {})
@ai_bp.route('/api/ai/config/clear', methods=['POST'])
def api_ai_config_clear():
    """清空单个模块；不传 module 则整体重置三个模块"""
    data = request.json or {}
    module = (data.get("module") or "").strip() or None
    if module and module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module}"}), 400
    # A-22（M5）：clear_module 有落盘副作用（清空模块配置 + 同步清密钥库 + **镜像清 DB**），
    # 必须保留调用；返回值此前被赋给 cfg 却从未使用（响应改由下方 _ai_config_view() 重新取整份视图），故去掉赋值。
    ai_config.clear_module(AI_CONFIG_PATH, module=module, legacy_path=LLM_CONFIG_PATH)
    # ⭐ 与「保存」对称：清库同样由 `ai_config.clear_module` 内部闭合（`_mirror_credentials_db(
    # clear=True)`），这里只**读回核对** —— 避免「AI 设置显示已清空，任务却仍读到 DB 旧凭证」
    # 这种页面上看不出来的漂移（清库失败会让任务继续用旧密钥跑）。
    db_clear_note, db_clear_error = _ai_credentials_verify(module, cleared=True)
    if db_clear_error:
        _app_logger().error("AI 凭证库清空核对不通过（module=%s）：%s", module, db_clear_error)
    reset_note, reset_error = "", ""
    if module in (None, "qc"):
        try:
            qc_client.reset_endpoint(QC_CONFIG_PATH)
            reset_note = "，质检接口已同步重置"
        except Exception as e:  # noqa: BLE001
            reset_error = f"{type(e).__name__}: {e}"
            _app_logger().warning(f"质检接口重置失败（AI 设置已清空，质检可能仍用旧接口）：{reset_error}")
    return jsonify({
        "success": True,
        "module": module,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "credentials_db_cleared": bool(not db_clear_error),
        "credentials_db_error": db_clear_error,
        "qc_reset": bool(module in (None, "qc") and not reset_error),
        "qc_reset_error": reset_error,
        "message": ((f"{AI_MODULE_LABEL.get(module, module)}配置已清除" if module else "AI 设置已整体重置")
                    + db_clear_note
                    + reset_note
                    + (f"；但凭证清空核对未通过：{db_clear_error}" if db_clear_error else "")
                    + (f"；但质检接口重置失败：{reset_error}" if reset_error else "")),
    })
@ai_bp.route('/api/ai/selfcheck', methods=['GET'])
def api_ai_selfcheck():
    """AI 前置自检（P0-5）：三个模块（text/qc/chat）的配置完整性 + 可选端点可达性。

    query:
      probe=1  附带端点可达性探测（GET {base_url}/models，结果带 60s 缓存）
      fresh=1  强制绕过探测缓存（刚改完配置时用）

    返回的 `ok=False` 即「当前配置一开跑就会失败」，前端据此在 AI 设置页 / 项目页
    显示红字阻断提示；`modules[*].hint` 给出逐模块的修复指引。
    """
    _probe = str(request.args.get("probe") or "").strip().lower() in ("1", "true", "yes", "on")
    _fresh = str(request.args.get("fresh") or "").strip().lower() in ("1", "true", "yes", "on")
    try:
        import ai_selfcheck
        rep = ai_selfcheck.check_modules(probe=_probe, force_probe=_fresh)
        _gate_off = ai_selfcheck.gate_disabled()
    except Exception as e:  # noqa: BLE001  自检失败按 500 明确报出，不假装通过
        return jsonify({"success": False,
                        "error": f"自检执行失败：{type(e).__name__}: {e}"}), 500
    return jsonify({
        "success": True,
        "ok": rep["ok"],
        "probed": rep["probed"],
        "message": rep["message"],
        "hint": rep["hint"],
        "blocked_modules": rep["blocked_modules"],
        "blocked_labels": rep["blocked_labels"],
        "modules": rep["modules"],
        "gate_off": _gate_off,
    })
@ai_bp.route('/api/ai/test', methods=['POST'])
def api_ai_test():
    """测试单个模块连通性（可用页面暂存参数，不落盘）。

    - text / chat：文本连通性对话测试
    - qc：默认做「视觉能力」探测（发一张极小图片），确认模型支持图像输入；
          也可传 probe="text" 只测文本连通性。
    """
    data = request.json or {}
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module or '(空)'}"}), 400

    # 用户点了「测试连接」= 刚改完配置想验证 → 必须清掉旧熔断。
    # 否则网关恢复/换好网关后，这里会被上一轮的熔断状态直接拦下并报"不可用"，
    # 用户会以为新配置也没用。
    LLMClient.reset_gateway_circuits()
    # 同理清掉前置自检的端点探测缓存：测试通过后马上开跑，门禁不该还拿着改之前的旧结论
    try:
        import ai_selfcheck
        ai_selfcheck.reset_probe_cache()
    except Exception as e:  # noqa: BLE001
        _app_logger().debug("重置 AI 前置自检探测缓存失败（忽略）：%s", e)

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    # 备用模型「测试连接」：前端拿不到已保存备用的明文密钥（对外视图恒脱敏），
    # 所以按 fallback_index 在**服务端**取那条备用的完整配置（含解密后的 key）再探。
    # 显式传了 base_url/model/api_key 时以显式值为准（用于「还没保存」的草稿条目）。
    _fb = None
    _fb_idx_raw = data.get("fallback_index")
    if _fb_idx_raw is not None and str(_fb_idx_raw).strip() != "":
        try:
            _fbs_saved = saved.get("fallbacks") or []
            _fb_i = int(_fb_idx_raw)
            if 0 <= _fb_i < len(_fbs_saved):
                _fb = _fbs_saved[_fb_i]
        except (TypeError, ValueError):
            _fb = None
        if _fb is None:
            return jsonify({"success": False, "module": module,
                            "error": f"备用模型 #{_fb_idx_raw} 不存在（可能已被删除），请刷新页面后重试"}), 400
    _fb_base = str((_fb or {}).get("base_url") or "").strip()
    _fb_model = str((_fb or {}).get("model") or "").strip()
    _fb_key = str((_fb or {}).get("api_key") or "").strip()
    base_url = (data.get("base_url") or _fb_base or saved["base_url"] or "").strip()
    model = (data.get("model") or _fb_model or saved["model"] or "").strip()
    api_key = data.get("api_key")
    if not api_key or not str(api_key).strip() or "*" in str(api_key):
        api_key = _fb_key or saved["api_key"] or ""
    # 思考档位：页面暂存值优先，否则沿用该备用 / 主模型的已保存值
    _re = data.get("reasoning_effort")
    if _re is None:
        _re = (_fb or {}).get("reasoning_effort") or saved.get("reasoning_effort") or ""
    ep = {"base_url": base_url, "api_key": str(api_key).strip(), "model": model,
          "reasoning_effort": str(_re).strip()}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return jsonify({"success": False, "module": module, "endpoint": {k: v for k, v in ep.items() if k != "api_key"},
                        "error": "base_url / api_key / model 均为必填，请填写完整后再测试"}), 400

    probe = (data.get("probe") or ("vision" if module == "qc" else "text")).strip()
    if module == "qc" and probe == "vision":
        result = qc_client.test_vision(ep, timeout=int(data.get("timeout") or 60))
        result.update({"module": module, "probe": "vision", "model": ep["model"],
                       "base_url": ep["base_url"]})
        if not result.get("success"):
            # ⚠️ 2026-10-11：探测失败的归因必须区分，否则文案会把排查方向带偏。
            # 实战教训：上游对 1x1 探测图回 400 image_invalid（"use a real/valid image"），
            # 而这里一律写成「该接口或模型不支持图像输入」，导致去查模型能力 ——
            # 实际模型（cn:deepseek-v4.1-flash）完全支持视觉，换 64x64 图即通过。
            _err = str(result.get("error") or "")
            if "image_invalid" in _err or "real/valid image" in _err:
                result["guide"] = (
                    "上游拒绝了这次的探测图（image_invalid）。**这不代表模型不支持视觉** —— "
                    "通常是探测图被判为无效（如 1x1 像素图）。若你看到此提示，说明探测图本身"
                    "需要更换；运行态的质检用真实分镜图，不受影响。")
            elif getattr(result, "get", None) and result.get("uncertain"):
                result["guide"] = ("接口可达、模型有响应，但额度被思考占用、未返回正文，"
                                   "无法确认是否支持图像；请提高该模块的 max_tokens 后重测。")
            else:
                result["guide"] = ("该接口或模型不支持图像输入（或不可达）。质检需要多模态模型，"
                                   "请改用支持视觉的模型（如 gpt-4o-mini / qwen-vl-max / glm-4v）。")
        return jsonify(result), (200 if result.get("success") else 400)

    try:
        # ⭐ P0-5 第②项收尾：探针与运行态**必须走同一条客户端构造路径**。
        # 此前这里自建 LLMClient(AI_CONFIG_PATH, config=ep)，运行态走 _ai_client_for_module，
        # 两边各写一份「参数取谁 / reasoning_effort 怎么注入 / 默认超时多少」的推导 ——
        # 一旦分叉就是「测试连接通过、运行时行为不一致」的经典温床。现在只此一条路径。
        # with_fallbacks=False：测试连接必须**只测这一个端点**（详见 _ai_client_for_module 的说明）。
        # 运行态照旧带备用链，两者在「参数推导」上仍然同源，只是探针不参与故障转移。
        client = _ai_client_for_module(
            module, base_url=ep["base_url"], api_key=ep["api_key"], model=ep["model"],
            timeout=int(data.get("timeout") or 60), reasoning_effort=ep["reasoning_effort"],
            with_fallbacks=False)
        result = dict(client.test_connection() or {})
    except LLMError as e:
        # 与运行态同一个报错：未配置时的提示语完全一致，不再出现"测试说没问题、跑起来报未配置"
        result = {"success": False, "error": str(e)}
    except LLMGatewayUnavailable as e:
        # 网关整体不可用：明确区分于「密钥写错」
        result = {"success": False, "verdict": "gateway_unavailable",
                  "error": str(e), "hint": e.hint, "streak": e.streak,
                  "guide": "连通测试已跳过重试（上游报无可用算力时重试无意义）。"
                           "请更换 base_url 或模型，本项目的 AI 设置是三个模块各自独立的。"}
    except Exception as e:  # noqa: BLE001
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}
    result.update({"module": module, "probe": "text", "model": ep["model"], "base_url": ep["base_url"],
                   "circuit": LLMClient.gateway_circuit_state(ep["base_url"])})
    return jsonify(result), (200 if result.get("success") else 400)
@ai_bp.route('/api/ai/chat/history', methods=['GET'])
def api_ai_chat_history():
    """读取会话历史 + 当前草稿 + 已生效设定（供界面恢复）"""
    project = (request.args.get("project") or "").strip()
    return jsonify({"success": True, "state": _chat_state(project)})
@ai_bp.route('/api/ai/chat/archive', methods=['GET'])
def api_ai_chat_archive():
    """列出某项目归档的日期与每日条数（只读；全量真相源的浏览入口）。

    query: project（可空 → 回退当前活跃项目）
    → {success, project, dates:[{date,count}], total}
    """
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    key = _archive_project_key(request.args.get("project") or "", history)
    if not key:
        return jsonify({"success": True, "project": "", "dates": [], "total": 0})
    root = os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH))
    dates = ai_chat.archive_dates(root, key)
    return jsonify({"success": True, "project": key, "dates": dates,
                    "total": sum(int(d.get("count") or 0) for d in dates)})
@ai_bp.route('/api/ai/chat/archive/<date>', methods=['GET'])
def api_ai_chat_archive_date(date):
    """读取某项目某天的归档消息（只读分页）。

    query: project（可空 → 回退当前活跃项目）/ limit（默认 0=全部）/ offset
    → {success, date, total, messages:[...]}
    """
    date = str(date or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify({"success": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    key = _archive_project_key(request.args.get("project") or "", history)
    if not key:
        return jsonify({"success": True, "date": date, "total": 0, "messages": []})
    try:
        limit = max(0, int(request.args.get("limit", 0)))
    except (TypeError, ValueError):
        limit = 0
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    root = os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH))
    messages = ai_chat.load_archive(root, key, date, limit=limit, offset=offset)
    return jsonify({"success": True, "date": date,
                    "total": ai_chat.archive_count(root, key, date),
                    "messages": messages})
@ai_bp.route('/api/ai/chat/clear', methods=['POST'])
def api_ai_chat_clear():
    """清空会话（默认保留创作设定草稿与已生效设定）"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    history = ai_chat.clear_history(AI_CHAT_HISTORY_PATH,
                                    keep_settings=bool(data.get("keep_settings", True)),
                                    project=project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
    return jsonify({"success": True, "message": "会话已清空", "state": _chat_state(project)})
@ai_bp.route('/api/ai/chat', methods=['POST'])
def api_ai_chat():
    """一轮对话：调用「对话总控模型」，返回回复并同步更新创作设定草稿"""
    data = request.json or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message 不能为空"}), 400
    if len(message) > ai_chat.MAX_CHARS_PER_MESSAGE:
        message = message[:ai_chat.MAX_CHARS_PER_MESSAGE]

    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    history["active_project"] = project
    ai_chat.append_message(history, "user", message, project)

    draft = ai_chat.get_draft(history, project)
    if isinstance(data.get("draft"), dict) and data["draft"]:
        draft = ai_chat.merge_settings(draft, data["draft"])       # 界面手工补充的设定

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, "chat")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        ai_chat.drop_last_message(history, project)               # 未配置则不落用户消息，避免脏历史
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({
            "success": False,
            "error": "「对话总控模型」尚未配置（base_url / api_key / model）",
            "guide": LLM_NOT_CONFIGURED_GUIDE_MAP["chat"],
            "need_config": True,
            "state": _chat_state(project),
        }), 400

    messages = ai_chat.build_messages(history, draft, project)
    try:
        client = LLMClient(AI_CONFIG_PATH, config=ep, timeout=LLM_REQUEST_TIMEOUT)
        reply = client.chat(messages, temperature=0.7, max_tokens=2048)
    except LLMError as e:
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": f"对话总控模型调用失败：{e}",
                        "state": _chat_state(project)}), 400
    except Exception as e:  # noqa: BLE001
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": f"对话总控模型调用异常：{e}",
                        "state": _chat_state(project)}), 500

    new_settings = ai_chat.extract_settings(reply)
    if new_settings:
        draft = ai_chat.merge_settings(draft, new_settings)
        ai_chat.set_draft(history, project, draft)
    display = ai_chat.strip_json_block(reply)
    ai_chat.append_message(history, "assistant", display, project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    state = _chat_state(project)
    return jsonify({"success": True, "reply": display, "raw_reply": reply,
                    "new_settings": new_settings, "draft": draft,
                    "model": {"base_url": ep["base_url"], "model": ep["model"]},
                    "state": state})
@ai_bp.route('/api/ai/chat/apply', methods=['POST'])
def api_ai_chat_apply():
    """应用设定：把当前草稿（可叠加 patch）落盘为项目创作设定配置"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    draft = ai_chat.get_draft(history, project)
    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    merged = ai_chat.merge_settings(draft, patch)
    if not merged:
        return jsonify({"success": False, "error": "当前没有任何已确认的创作设定，无法应用",
                        "state": _chat_state(project)}), 400

    ai_chat.save_project_settings(AI_SETTINGS_PATH, project, merged)
    ai_chat.set_draft(history, project, merged)
    history["active_project"] = project
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    # 同步风格到项目 config.json：否则 config.json 的 style 停在建项目时的默认值
    # （如 3D动漫渲染），与总控刚敲定的设定不一致，用户会以为「设定没生效」。
    state = _chat_state(project)
    _sync_project_config_style(project, ai_chat.style_brief(state.get("settings") or {}))
    # ⚠️ normalize_settings 只保留白名单字段，其余**静默丢弃**。
    # 模型自造键名（实测出现过 color_tone / camera_language）时，
    # 用户以为「冷色调、克制镜头」已经写进去了，落盘却只剩 style —— 白沟通一场。
    # 这里把被丢弃的键如实回传，总控才能纠正键名并如实告知用户。
    dropped = sorted(k for k in (patch or {}).keys()
                     if k not in ai_chat.FIELD_LABELS)
    message = f"创作设定已应用（{len(merged)} 项）"
    if dropped:
        message += (f"；以下字段名不被支持，已忽略：{'、'.join(dropped)}"
                    "（合法字段见 allowed_fields，请用合法键名重试）")
    return jsonify({
        "success": True,
        "message": message,
        "dropped_fields": dropped,
        "allowed_fields": list(ai_chat.FIELD_LABELS.keys()),
        "settings": state["settings"],
        "draft": merged,
        "state": state,
    })
@ai_bp.route('/api/ai/chat/draft', methods=['POST'])
def api_ai_chat_draft():
    """直接补充 / 修改创作设定草稿（不调用模型，供界面表单编辑）"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    if data.get("replace"):
        history.setdefault("drafts", {})[project] = ai_chat.normalize_settings(patch)
    else:
        ai_chat.set_draft(history, project, ai_chat.merge_settings(
            ai_chat.get_draft(history, project), patch))
    history["active_project"] = project
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
    return jsonify({"success": True, "state": _chat_state(project)})
@ai_bp.route('/api/ai/chat/settings', methods=['GET'])
def api_ai_chat_settings():
    """当前已生效的创作设定（供界面查看，也供剧本 / 提示词 / 分镜链路引用）"""
    project = (request.args.get("project") or "").strip()
    return jsonify({"success": True, "settings": ai_chat.settings_view(AI_SETTINGS_PATH, project),
                    "settings_file": os.path.abspath(AI_SETTINGS_PATH)})
@ai_bp.route('/api/ai/settings', methods=['GET', 'POST'])
def api_ai_settings():
    """统一读取/保存创作设定（简版）"""
    project = (request.args.get("project") or "").strip()

    if request.method == 'POST':
        data = request.json or {}
        message = "设置已保存"

        # 「LLM 引擎」= 「文本分析模型」——同一个模块、同一份配置。
        # 后端自己也标注了：/api/llm/config 返回的 `deprecated` 字段写着
        # 「等价于 /api/ai/config 的 text 模块」。
        #
        # 旧实现在这里调 `load_llm_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)`，
        # 但这个别名来自 **llm_client**（只需 1 个 path 参数），却被按 ai_config 的
        # 2 参签名调用 → TypeError → 整个「保存系统设置」按钮必然 500
        # （实测报错：load_config() takes 1 positional argument but 2 were given）。
        # 现在统一落到 text 模块，不再往 llm_config 形态的扁平键里写死配置。
        if 'llm_api_key' in data or 'llm_provider' in data:
            cur = ai_config.get_module(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH), "text")
            key = str(data.get('llm_api_key') or '').strip()
            if key and '*' not in key and cur.get("base_url") and cur.get("model"):
                ai_config.save_module(AI_CONFIG_PATH, "text",
                                      base_url=cur["base_url"], model=cur["model"],
                                      api_key=key, legacy_path=LLM_CONFIG_PATH)
                message = "LLM 引擎密钥已更新（与「文本分析模型」是同一份配置）"
            else:
                message = ("「LLM 引擎」就是「文本分析模型」，"
                           "请在上方「文本分析模型」卡片里填写 base_url / model / api_key")

        # ComfyUI 地址：全项目只有这里写、没有任何地方读（各 ComfyUI 客户端都直接取
        # 模块级常量 COMFYUI_URL，来源是环境变量）。所以旧实现只是「假装保存成功」。
        # 这里如实告知，避免用户以为改了地址就生效。
        if 'comfyui_url' in data:
            message = (f"ComfyUI 地址由环境变量 COMFYUI_URL 决定，当前为 {COMFYUI_URL}；"
                       "如需修改请改环境变量后重启服务")

        # Save watermark config if provided
        if 'watermark_enabled' in data or 'watermark_text' in data:
            wm_cfg = _wm_load_cfg()
            if 'watermark_enabled' in data:
                wm_cfg['enabled'] = data['watermark_enabled']
            if 'watermark_text' in data:
                wm_cfg['text'] = data['watermark_text']
            video_watermark.save_config(WATERMARK_CONFIG_PATH, wm_cfg)
            message = "水印设置已保存"

        return jsonify({"success": True, "message": message})

    view = ai_chat.settings_view(AI_SETTINGS_PATH, project)
    return jsonify({"success": True, "project_name": view.get("project_name"),
                    "active": view.get("active"), "settings": view.get("settings"),
                    "style_brief": view.get("style_brief"),
                    "settings_file": view.get("settings_file")})
LLM_NOT_CONFIGURED_GUIDE_MAP = {
    "text": ("尚未配置「文本分析模型」。请在「AI 设置」中填写 base_url / api_key / model 并保存，"
             "之后即可使用小说转剧本与提示词分析。"),
    "qc": ("尚未配置「质检模型」。请在「AI 设置 → 质检模型」中填写独立的 base_url / api_key / model"
           "（需支持图像输入的多模态模型）。未配置时生成流程会自动跳过质检，不会报错。"),
    "chat": ("尚未配置「对话总控模型」。请在「AI 设置 → 对话总控模型」中填写 base_url / api_key / model，"
             "之后即可使用 AI 对话来确定创作设定。"),
}
def _ai_config_view() -> dict:
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.public_view(cfg)
    view["config_path"] = os.path.abspath(AI_CONFIG_PATH)
    view["legacy_path"] = os.path.abspath(LLM_CONFIG_PATH)
    view["modules_meta"] = ai_config.module_meta()
    # ComfyUI 地址如实下发：它由环境变量 COMFYUI_URL 决定，写进配置文件也没有任何
    # 代码读取（历史遗留的死配置）。前端据此只做只读展示，不再给一个「改了没用」的输入框。
    view["comfyui"] = {
        "url": COMFYUI_URL,
        "source": "环境变量 COMFYUI_URL",
        "editable": False,
    }
    # 网关熔断状态：上游整体挂掉时前端要能一眼看到「不是模型配错，是网关没算力」，
    # 否则用户会反复改 base_url/model 却越改越乱。
    view["gateways"] = {
        m: {"base_url": (v or {}).get("base_url") or "",
            "circuit": LLMClient.gateway_circuit_state((v or {}).get("base_url") or "")}
        for m, v in (view.get("modules") or {}).items()
    }
    return view
def _ai_credentials_verify(module: str, base_url: str = "", model: str = "",
                           new_key: str = "", cleared: bool = False) -> tuple:
    """保存/清空后**读回核对**：任务实际读的那份凭证（tasks.db）是否等于本次提交的值。

    职责分工：写侧由 `ai_config.save_module` / `clear_module` 内部镜像闭合
    （任何调用方都自动同步，见 `ai_config._mirror_credentials_db` 的长注释）。
    这里**只做独立核对**，因为最危险的故障恰恰是「写没成功但接口照样返回 200」——
    磁盘满 / tasks.db 不可写 / env 覆盖，都会让「前端配的」与「任务实际用的」分叉，
    而页面上完全看不出来（用户会以为配置已生效，直到任务 401 或跑出别的账号的结果）。

    返回 `(note, error)`：`error` 非空 → 响应必须响亮告警。
    """
    try:
        import ai_credentials_db
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    if cleared:
        # module=None = 整体重置 → 三个模块都要核对（`get_credentials(None)` 会抛 ValueError）
        mods = [module] if module else list(AI_MODULES)
        stuck = []
        for m in mods:
            try:
                if ai_credentials_db.get_credentials(m).get("api_key"):
                    stuck.append(m)
            except Exception as e:  # noqa: BLE001
                return "", f"{type(e).__name__}: {e}"
        if stuck:
            return "", (f"AI 凭证库（tasks.db）中 {'、'.join(stuck)} 模块的密钥未被清空 → "
                        "任务仍会读到旧凭证，请检查 output/tasks.db 可写性后重试")
        return "，AI 凭证库已同步清空", ""

    try:
        db = ai_credentials_db.get_credentials(module)
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    # env 覆盖是**设计内**的最高优先级（运维部署用），但它同样意味着「页面填的不作数」，
    # 必须明说而不是报成错误。
    try:
        import secret_store
        env_name = secret_store.ENV_KEY_MAP.get(f"ai.{module}") or ""
    except Exception:  # noqa: BLE001
        env_name = ""
    env_key = ((os.getenv(env_name) or "").strip() if env_name else "")
    if env_key and env_key != new_key:
        return (f"注意：{module} 模块密钥被环境变量 {env_name} 覆盖，"
                "任务实际使用的是该环境变量的值，不是页面上填写的值", "")

    def _norm(u):
        return (u or "").strip().rstrip("/").lower()

    diff = []
    if _norm(db.get("base_url")) != _norm(base_url):
        diff.append("base_url")
    if (db.get("model") or "") != (model or ""):
        diff.append("model")
    if new_key and (db.get("api_key") or "") != new_key:
        diff.append("api_key")
    if diff:
        return "", (f"AI 凭证库（tasks.db）与本次保存不一致（{'、'.join(diff)}）→ "
                    "任务可能仍用旧凭证。请检查 output/tasks.db 是否可写、磁盘是否已满后重试")
    return "，已写入 AI 凭证库（任务下次调用立即生效，无需重启）", ""
def _archive_project_key(raw: str, history: dict) -> str:
    """归档接口的项目键：**看原始入参**决定是否回退到活跃项目。

    ⚠️ 不能用 `_safe_project(x) or <兜底>` 判空：`_safe_project('')` 返回字面量
    `'project'`（真值），兜底永不生效。必须看原始 query 是否为空。
    """
    raw = (raw or "").strip()
    if raw:
        return ai_chat.canonical_project_key(raw)
    return str(history.get("active_project") or "")
def _chat_project(data: dict = None, history: dict = None) -> str:
    data = data or {}
    # ⚠️ Ưu tiên request field (project_name hoặc project), sau đó mới fallback history
    name = (data.get("project_name") or data.get("project") or "").strip()
    if not name and isinstance(history, dict):
        name = (history.get("active_project") or "").strip()
    return ai_chat.project_key(name or "default")
def _chat_state(project: str = "") -> dict:
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = ai_chat.project_key(project or history.get("active_project") or "default")
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    model_view = ai_config.module_public_view(ai_config.get_module(cfg, "chat"))
    return {
        "project_name": project,
        "messages": ai_chat.project_messages(history, project),
        "draft": ai_chat.get_draft(history, project),
        "settings": ai_chat.settings_view(AI_SETTINGS_PATH, project),
        "fields": ai_chat.fields_meta(),
        "model": model_view,
        "history_file": os.path.abspath(AI_CHAT_HISTORY_PATH),
        "settings_file": os.path.abspath(AI_SETTINGS_PATH),
    }
def _save_ai_module(data: dict):
    """保存单个 AI 模块的核心实现（/api/ai/config 与兼容路由 /api/llm/config 共用）"""
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False,
                        "error": f"unknown module：{module or '(空)'}，可选 {list(AI_MODULES)}"}), 400
    base_url = (data.get("base_url") or '').strip()
    model = (data.get("model") or '').strip()
    api_key = data.get("api_key")
    # 思考档位（可选项）：字段缺失 = 不改动；传空串 = 清空。非法值由 ai_config 归一化成 ""
    has_reasoning_effort = "reasoning_effort" in data
    reasoning_effort = data.get("reasoning_effort")
    if not base_url or not model:
        return jsonify({"success": False, "error": "base_url 与 model 均为必填项"}), 400

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    old = ai_config.get_module(cfg, module)
    key = "" if api_key is None else str(api_key).strip()
    keep = (not key) or ("*" in key)   # 留空或脱敏回显 → 不改动原密钥
    if keep and not old.get("api_key"):
        return jsonify({"success": False, "error": "该模块首次配置必须填写 api_key"}), 400

    # 备用模型（故障转移链）：字段缺失 = 不改动；传 list = 整体覆盖
    has_fallbacks = "fallbacks" in data
    _fb = data.get("fallbacks") if has_fallbacks else None
    cfg = ai_config.save_module(AI_CONFIG_PATH, module, base_url=base_url, model=model,
                                api_key=None if keep else key, legacy_path=LLM_CONFIG_PATH,
                                reasoning_effort=(reasoning_effort if has_reasoning_effort else None),
                                fallbacks=_fb)
    # ⭐ 凭证单一事实源由 `ai_config.save_module` **内部**镜像闭合（json + 加密库 + tasks.db
    # 一次写完，见其 `_mirror_credentials_db` 长注释）—— 这里不再重复写库，只**读回核对**，
    # 避免同一份值有两个写点（将来谁改一处就会漂移）。核对能抓到「接口返回成功但写没落地」
    # 以及 env 覆盖这类页面上看不出来的分叉。
    db_note, db_error = _ai_credentials_verify(
        module, base_url=base_url, model=model,
        new_key="" if keep else key)
    if db_error:
        _app_logger().error("AI 凭证库核对不通过（module=%s）：%s", module, db_error)
    # 配置刚变 → 清掉前置自检的端点探测缓存，避免出现「明明确认改好了，开跑还是被拦」
    try:
        import ai_selfcheck
        ai_selfcheck.reset_probe_cache()
    except Exception as e:  # noqa: BLE001
        _app_logger().debug("重置 AI 前置自检探测缓存失败（忽略）：%s", e)
    view = ai_config.module_public_view(ai_config.get_module(cfg, module))
    return jsonify({
        "success": True,
        "module": module,
        "module_config": view,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "credentials_db_updated": bool(not db_error),
        "credentials_db_error": db_error,
        "message": (f"{AI_MODULE_LABEL.get(module, module)}配置已保存"
                    + ("（api_key 保持不变）" if keep else "")
                    + db_note
                    + (f"；但凭证核对未通过：{db_error}" if db_error else "")),
    })
def _sync_project_config_style(project_name: str, brief: str) -> bool:
    """把 AI 总控敲定的风格纲要同步到项目 config.json 的 style 字段（2026-09-22 P-2）。

    总控 apply 设定原本只写 ai_chat/project_settings.json，项目 config.json 的 style
    停在「建项目时的默认值（3D动漫渲染）」，用户看到「配置要求国漫2D 实际却还是 3D」
    即由此。这里把生效的 style_brief 回写进 config.json，让两份配置口径一致。

    - 仅当项目在 project_store 里真实存在、且 brief 非空时才写；
    - 失败只降级告警、不阻断 apply（config.json 同步属后置簿记，主链路必须成功）。
    返回是否真正落盘。
    """
    brief = (brief or "").strip()
    if not brief:
        return False
    rec = project_store.get_project(project_name or "")
    if not rec:
        return False
    try:
        project_store.update_config(rec["dir_key"], {"style": brief})
        return True
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("AI 总控风格同步到项目 config.json 失败（不影响设定应用）：%s", e)
        return False
