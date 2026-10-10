'''风格/画幅判定助手（2026-10-11 从 app.py 下沉，助手域第三批）。'''

# 依赖闭包（实测 2 个对象 75 行）：_style_aspect_confirmed 46（0 依赖）
#   ｜ _style_aspect_guard 29。无 app 依赖（连 logger 都不需要）。
import json
import logging
import re

import ai_chat
import project_store
from flask import jsonify

from config import AI_SETTINGS_PATH
from shared_project import _safe_project

logger = logging.getLogger(__name__)


def _style_aspect_confirmed(project_name: str) -> dict:
    """生成前置确认门判据：用户是否已与总控 AI 确认「风格」与「视频比例」。

    单一事实源 = 已应用的总控设定（ai_chat/project_settings.json，按项目）。
    - 风格确认：settings 的风格基调(style) 或 画风(art_style) 任一非空；
    - 比例确认：settings 的画面比例(aspect_ratio，即视频画幅，如 9:16) 非空。
    资产图的画幅已按类型内置写死（见 style_kit.asset_aspect_ratio），不依赖此比例；
    这里确认比例只为「视频 / 分镜」画幅服务。
    """
    try:
        view = ai_chat.settings_view(AI_SETTINGS_PATH, project_name or "") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"确认门读取总控设定失败（按未确认处理）：{e}")
        view = {}
    s = view.get("settings") or {}
    style_confirmed = bool((s.get("style") or s.get("art_style") or "").strip())
    # 2026-09-23（建项目选风格）：用户「新建项目」时下拉/自定义的风格写进 config.json 的
    # style，也算「风格已确认」——否则用户明明选了风格，生成仍被 409 拦在「尚未确认风格」，
    # 与「建项目时就能选风格」的体验自相矛盾。总控 AI 敲定（project_settings）仍是第一优先级。
    if not style_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_style = str(project_store.read_config(rec["dir_key"]).get("style") or "").strip()
                style_confirmed = bool(cfg_style)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"确认门读取 config.style 失败（忽略）：{e}")
    aspect_confirmed = bool((s.get("aspect_ratio") or "").strip())
    # 2026-09-23（建项目选比例）：用户「新建项目」时选的画面比例写进 config.json 的
    # aspect_ratio，也算「比例已确认」，与 style 的同源兜底保持一致。总控 AI 敲定仍是第一优先级。
    if not aspect_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_ar = str(project_store.read_config(rec["dir_key"]).get("aspect_ratio") or "").strip()
                aspect_confirmed = bool(cfg_ar)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"确认门读取 config.aspect_ratio 失败（忽略）：{e}")
    missing = []
    if not style_confirmed:
        missing.append("风格")
    if not aspect_confirmed:
        missing.append("视频比例")
    return {"confirmed": style_confirmed and aspect_confirmed,
            "style_confirmed": style_confirmed, "aspect_confirmed": aspect_confirmed,
            "missing": missing, "settings": s}


def _style_aspect_guard(project_name: str, override_style: str = ""):
    """生成入口前置校验门（2026-09-22 需求）。

    用户未与总控 AI 确认「风格 / 视频比例」时拦截生成：返回 409 + 可读提醒响应；
    已确认则返回 None（放行）。各生成端点在解析出 project_name 后调用它。
    前端 client.ts readError 会自动弹出 error + guide 文案，提示去总控确认。

    ``override_style``：个别入口（如托管 /api/autonomous/start）允许调用方**显式传风格**
    （plan_overrides.style）——此时视「风格」为已确认，但「视频比例」仍须总控确认。
    """
    chk = _style_aspect_confirmed(project_name)
    if override_style and not chk["style_confirmed"]:
        chk["style_confirmed"] = True
        chk["missing"] = [m for m in chk["missing"] if m != "风格"]
        chk["confirmed"] = chk["style_confirmed"] and chk["aspect_confirmed"]
    if chk["confirmed"]:
        return None
    _names = "、".join(chk["missing"])
    return jsonify({
        "success": False,
        "requires_confirm": True,
        "error": f"尚未与总控 AI 确认{_names}，暂不开展生成。",
        "guide": ("请先在「AI 对话 · 创作总控」里与 AI 敲定" + _names
                  + "（风格：画风/基调；视频比例：画面画幅，如 9:16 / 16:9 / 1:1），"
                    "点击「应用设定」落盘后再开始生成。"),
        "missing": chk["missing"],
        "style_confirmed": chk["style_confirmed"],
        "aspect_confirmed": chk["aspect_confirmed"],
    }), 409
