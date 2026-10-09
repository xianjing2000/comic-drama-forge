# -*- coding: utf-8 -*-
"""系统 / 环境信息类 API 蓝图（Blueprint 拆分第一批，2026-10-08）。

为什么先拆这一批：这 9 条路由（依赖自检 / i18n / 运行日志 / provider 目录 / ComfyUI 模型管理）
只依赖叶子模块（deps_check、log_viewer、providers、comfyui_client、comfyui_models、config），
不需要 app.py 里的任何私有助手 —— 在 1.8 万行的 app.py 里，它们是最干净的切口。

⚠️ 拆分铁律：**URL 规则与响应体一字不改**，只把注册位置从 @app.route 换成蓝图，app.py 里
register_blueprint。endpoint 名会从 api_logs 变成 system.api_logs（Flask 内部用，前端只认 URL）。
"""
import json
import logging
import os

from flask import Blueprint, current_app, jsonify, request

import comfyui_client
import comfyui_models
import deps_check
import log_viewer
import providers
from config import COMFYUI_URL, PROJECT_ROOT_DIR

logger = logging.getLogger(__name__)

system_bp = Blueprint('system', __name__)

@system_bp.route('/api/deps/check', methods=['GET'])
def api_deps_check():
    """依赖自检：核查 ComfyUI 侧「插件节点」与「模型权重」是否齐备（只读，绝不阻断）。

    依据 docs/依赖清单.md：模板 JSON 已内置，但跑起来还差①插件包（custom_nodes/）
    ②模型权重（models/）。本端点把两层逐一报出「就位 / 缺失 / 无法判定」。

    query 参数：
      offline=1        强制离线（不探 ComfyUI，只扫本地 custom_nodes/ 目录名，标注不可靠）
      url=<host:port>  指定 ComfyUI 地址（覆盖 config.COMFYUI_URL）
    返回：comfyui / plugins / models / workflows / summary（见 deps_check.check_deps 契约）
    """
    offline = request.args.get('offline', '').strip().lower() in ('1', 'true', 'yes')
    url_arg = (request.args.get('url') or '').strip()
    try:
        result = deps_check.check_deps(comfyui_url=(url_arg or None), force_offline=offline)
    except Exception as e:
        # 检测本身是「锦上添花」，任何异常都降级为 200 + 明确标记，不把自检打挂
        return jsonify({"error": str(e), "comfyui": {"online": False},
                        "summary": {"all_ok": False, "blockers": [f"检测异常：{e}"]}})
    result["docs"] = "docs/依赖清单.md"
    return jsonify(result)


@system_bp.route('/api/i18n/<lang>', methods=['GET'])
def api_i18n(lang):
    """读取前端语言包（zh-CN / en-US）"""
    lang = (lang or 'zh-CN').strip()
    safe = "".join(c for c in lang if c.isalnum() or c in "-_") or "zh-CN"
    locale_dir = os.path.join(PROJECT_ROOT_DIR, "locales")
    path = os.path.join(locale_dir, f"{safe}.json")
    if not os.path.isfile(path):
        fallback = os.path.join(locale_dir, "zh-CN.json")
        if not os.path.isfile(fallback):
            return jsonify({"success": False, "error": f"语言包不存在：{safe}",
                            "available": []}), 404
        path = fallback
        safe = "zh-CN"
    try:
        with open(path, "r", encoding="utf-8") as f:
            pack = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"语言包解析失败：{e}"}), 500
    available = []
    if os.path.isdir(locale_dir):
        available = sorted(os.path.splitext(f)[0] for f in os.listdir(locale_dir)
                           if f.lower().endswith(".json"))
    return jsonify({"success": True, "lang": safe, "messages": pack,
                    "available": available})


@system_bp.route('/api/logs', methods=['GET'])
def api_logs():
    """查看后台服务日志（只读）。

    服务由计划任务后台启动、没有终端窗口，日志原本只能去翻磁盘上的
    ``.workbuddy/test/_out/serve_stdout.log``。这里把它接到 Web 上，方便实时排查。

    查询参数:
        source: serve（默认） / comfyui —— **仅接受白名单 key，绝不接受路径**（防穿越）
        tail:   返回尾部多少行，默认 300，上限 2000
        since:  字节偏移，>0 时只返回此后新增的内容（前端「自动刷新」用）
        q:      关键字过滤；level: ERROR / WARNING / INFO / DEBUG
    """
    src = str(request.args.get('source') or '').strip()
    try:
        tail = int(request.args.get('tail') or 0)
        since = int(request.args.get('since') or 0)
    except ValueError:
        return jsonify({"success": False, "error": "tail / since 必须是整数"}), 400
    data = log_viewer.tail_lines(
        source=src or log_viewer.DEFAULT_SOURCE,
        tail=tail or log_viewer.DEFAULT_TAIL,
        since=since,
        query=str(request.args.get('q') or '').strip(),
        level=str(request.args.get('level') or '').strip().upper(),
    )
    status = 200 if data.get("success") else 400
    return jsonify(data), status


@system_bp.route('/api/logs/sources', methods=['GET'])
def api_logs_sources():
    """列出可查看的日志源及其大小 / 最后更新时间。"""
    return jsonify({"success": True, "sources": log_viewer.available_sources()})


@system_bp.route('/api/providers', methods=['GET'])
def api_providers():
    """列出各环节可用引擎与当前生效实现

    refresh=1 时绕过可用性探测缓存重新探测（可用性探测会真连 ComfyUI / TTS，
    因此默认走 TTL 缓存，避免前端刷新把列表接口拖到数秒）。
    """
    force = str(request.args.get('refresh') or '').strip() in ('1', 'true', 'yes')
    try:
        data = providers.catalog(force=force)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"引擎目录读取失败：{e}"}), 500
    env_keys = {kind: info.get("env_key") for kind, info in (data or {}).items()}
    return jsonify({"success": True, "kinds": data, "env_keys": env_keys})


@system_bp.route('/api/providers/select', methods=['POST'])
def api_providers_select():
    """切换某环节引擎（写入运行时环境变量；持久化请改 .env 的 MJSCXT_PROVIDER_*）"""
    data = request.json or {}
    kind = str(data.get('kind') or '').strip().lower()
    name = str(data.get('name') or '').strip()
    if kind not in ('image', 'video', 'tts') or not name:
        return jsonify({"success": False, "error": "参数非法（kind ∈ image/video/tts）"}), 400
    try:
        info = providers.set_active(kind, name)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "kind": kind, **info})


@system_bp.route('/api/comfyui/models', methods=['GET'])
def api_comfyui_models():
    """扫描 ComfyUI 真实可用的模型槽位候选值 + 已安装自定义节点包。

    refresh=1 强制绕过 object_info 缓存重新拉取（前端「重新扫描」按钮用）。
    返回体含 success；ComfyUI 离线时 success=False 且带 error，不抛异常。
    """
    force = str(request.args.get('refresh') or '').strip() in ('1', 'true', 'yes')
    data = comfyui_models.scan(comfyui_client.ComfyUIClient(), force=force)
    status = 200 if data.get("success") else 503
    return jsonify(data), status


@system_bp.route('/api/comfyui/models', methods=['POST'])
def api_comfyui_models_select():
    """保存用户手动指定的模型（按槽位）。

    body: {"unet_main": "<模型名>", ...}
    显式传 null 或 "" 表示**清空**该槽位，回落到工作流模板自身的取值。
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "body 必须是 JSON 对象"}), 400
    # 只允许已知槽位，脏 key 直接忽略（不报错，避免前后端版本差导致整体失败）
    known = {k: v for k, v in data.items() if k in comfyui_models.SLOTS}
    try:
        selection = comfyui_models.save_selection(known)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"保存失败：{e}"}), 500
    return jsonify({"success": True, "selection": selection})


@system_bp.route('/api/comfyui/refmod-status', methods=['GET'])
def api_comfyui_refmod_status():
    """探测 ComfyUI 是否安装 RefMod / MiniMaxH3Mod 节点（运维重启验证用）。

    RefMod：把一个角色的多张参考图/视频打包成单个 .safetensors，像 LoRA 一样
    直接喂给 H3 context（免训练、不占参考图槽位）。未安装时先 clone
    ComfyUI-MiniMaxH3Mod 到 custom_nodes 并重启 ComfyUI，再回来看本接口。

    在既有返回键（success / comfyui / installed / nodes / hint，不可达时
    success=False + error + 502）基础上追加 ``refmod`` 结构化结果：

        {"refmod": {"reachable": bool, "nodes": [{"class", "input"}]}}

    fail-open：ComfyUI 不可达 / 节点不存在 / 任何异常都返回结构化结果，
    绝不抛错、不影响任何生成链路。
    """
    # 模块级函数必须从模块对象 import（本文件里 `comfyui_client` 名字被实例占用，
    # 见文件顶部 import 处注释；函数内 from-import 命中的是 sys.modules 里的模块）
    from comfyui_client import probe_refmod_nodes
    try:
        probe = probe_refmod_nodes(timeout=15)
        # 与旧实现一致按类名排序（object_info 键序不保证稳定）
        nodes = sorted(probe.get("nodes") or [],
                       key=lambda n: str((n or {}).get("class") or ""))
        names = [n.get("class") for n in nodes if isinstance(n, dict) and n.get("class")]
        base = {
            "success": bool(probe.get("reachable")),
            "comfyui": COMFYUI_URL,
            "installed": bool(names),
            "nodes": names,
            "hint": ("已安装" if names else
                     "未安装：clone ComfyUI-MiniMaxH3Mod 到 custom_nodes 后重启 ComfyUI"),
        }
        refmod = {"reachable": bool(probe.get("reachable")), "nodes": nodes}
        if probe.get("error"):
            refmod["error"] = probe.get("error")
        base["refmod"] = refmod
        if not probe.get("reachable"):
            # 保持既有 502 契约（success=False + error），并附结构化 refmod 不可用结果
            base["error"] = probe.get("error") or "ComfyUI 不可达"
            return jsonify(base), 502
        return jsonify(base)
    except Exception as e:  # noqa: BLE001  整体 fail-open：绝不把异常抛成裸 HTML 500
        err = f"{type(e).__name__}: {e}"
        return jsonify({"success": False, "comfyui": COMFYUI_URL, "installed": False,
                        "nodes": [], "hint": "RefMod 探测/自检异常", "error": err,
                        "refmod": {"reachable": False, "nodes": [], "error": err}}), 502
