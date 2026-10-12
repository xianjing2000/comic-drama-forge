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
import threading
import time
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request

import comfyui_client
import comfyui_models
import deps_check
import log_viewer
import providers
from config import COMFYUI_URL, PROJECT_ROOT_DIR

logger = logging.getLogger(__name__)

system_bp = Blueprint('system', __name__)

# ===================== 提供商模型清单检测（2026-10-12） =====================
# 用户口径「直接检测提供商模型清单让用户选主/备模型」：填 base_url + api_key →
# 调 <base_url>/v1/models（OpenAI 兼容）返回模型清单，供前端做「选 provider → 选模型」下拉。
# 参考 providers 的 TTL 缓存 + 可用性探测：默认 30s 内复用结果，refresh=true 绕过，
# 避免前端刷新把列表接口拖慢。任何异常都收敛成结构化结果，绝不抛成裸 500。
_MODELS_TTL = float(os.getenv("MJSCXT_AI_MODELS_TTL", "30") or 30)
_MODELS_TIMEOUT = 8.0
_MODELS_CACHE = {}
_MODELS_LOCK = threading.RLock()

# --------------------------------------------------------------------------- #
# SSRF 防护（2026-10-12 t71，t65 独立验证发现的 P1）
# --------------------------------------------------------------------------- #
# 原实现对 base_url 零协议白名单/零内网黑名单/零重定向防护，实测 file:// 可读本地文件。
# 修复：① 协议白名单 http/https；② 主机解析后校验实际 IP（回环/私有/链路本地/云元数据/
#   ULA/CGNAT/保留/多播/未指定全拒，防 DNS rebinding 与十进制 IP）；③ 禁用重定向；
#   ④ 显式白名单放行本地 LLM 网关（精确 host:port，默认 127.0.0.1:7863 等生产在用），
#   env MJSCXT_AI_MODELS_ALLOW_HOSTS 可追加（逗号分隔 host 或 host:port）。
# 取舍说明：默认**只放行 7863 端口**的本地网关，其余回环/内网一律拒绝 —— 既保住
#   生产在用的本地 LLM，又不给任意 SSRF 开「回环全放」的口子；用户自建其它端口需显式加白。
_DEFAULT_MODELS_ALLOW_HOSTS = ("127.0.0.1:7863", "localhost:7863", "::1:7863", "[::1]:7863")


def _models_allow_hosts() -> set:
    """显式放行白名单（精确 host 或 host:port；默认含生产本地网关，env 可追加）。"""
    raw = os.getenv("MJSCXT_AI_MODELS_ALLOW_HOSTS", "").strip()
    out = set(_DEFAULT_MODELS_ALLOW_HOSTS)
    for item in raw.split(","):
        item = item.strip().lower()
        if item:
            out.add(item)
    return out


def _is_blocked_ip(addr) -> bool:
    """是否属于禁止访问的 IP（回环/私有/链路本地/云元数据/ULA/CGNAT/保留/多播/未指定）。"""
    import ipaddress as _ipa
    if isinstance(addr, _ipa.IPv6Address):
        if addr.ipv4_mapped is not None:
            return _is_blocked_ip(_ipa.ip_address(addr.ipv4_mapped))
        return (addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_unspecified or addr.is_private or addr.is_reserved)
    return (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_multicast or addr.is_unspecified or addr.is_reserved
            or addr in _ipa.ip_network("100.64.0.0/10")     # CGNAT
            or addr in _ipa.ip_network("0.0.0.0/8")
            or addr in _ipa.ip_network("198.18.0.0/15"))    # benchmark


def _validate_base_url(base_url: str) -> tuple:
    """SSRF 防护校验。返回 (ok, error)。"""
    import ipaddress as _ipa
    import socket as _socket

    u = (base_url or "").strip()
    if not u:
        return False, "base_url 不能为空"
    try:
        p = urlparse(u)
    except ValueError:
        return False, "base_url 非法"

    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "仅支持 http/https 协议（收到 %s）" % (scheme or "空")

    host = (p.hostname or "").strip().lower()
    if not host:
        return False, "base_url 缺主机名"
    port = str(p.port or (443 if scheme == "https" else 80))

    # 显式白名单（精确 host 或 host:port）—— 生产本地网关在此放行
    allowed = _models_allow_hosts()
    if host in allowed or (host + ":" + port) in allowed:
        return True, ""

    # 解析实际 IP（防 DNS rebinding / 十进制 IP / IPv6 映射）
    try:
        infos = _socket.getaddrinfo(host, port, type=_socket.SOCK_STREAM)
    except _socket.gaierror:
        return False, "主机名无法解析：%s" % host
    if not infos:
        return False, "主机名无法解析：%s" % host
    for info in infos:
        ip_str = info[4][0]
        try:
            addr = _ipa.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(addr):
            return False, "拒绝访问内网/保留地址：%s（解析为 %s）" % (host, ip_str)
    return True, ""


def _safe_models_opener():
    """禁用 HTTP 重定向 + 禁用代理的 opener（防重定向/代理绕过 SSRF 校验）。

    用 build_opener（与标准 urlopen 同构）：_NoRedirect 替代默认 HTTPRedirectHandler，
    ProxyHandler({}) 禁用环境代理。协议白名单已挡 file/ftp，故无需额外 handler。
    """
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """禁用 HTTP 重定向：redirect_request 返回 None = 不跟随跳转（3xx 将被当错误抛出）。"""
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))


def _build_models_url(base_url: str) -> str:
    """把 base_url 规范化为 OpenAI 兼容的模型清单地址。

    - https://api.deepseek.com        → https://api.deepseek.com/v1/models
    - https://api.openai.com/v1       → https://api.openai.com/v1/models
    - http://127.0.0.1:8899/v1        → http://127.0.0.1:8899/v1/models
    - .../v1/models（已是清单地址）     → 原样返回
    """
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    path = urlparse(u).path.rstrip("/")
    if path.endswith("/models"):
        return u
    if path in ("", "/"):
        return u + "/v1/models"
    return u + "/models"


def _mask_key(key: str) -> str:
    """api_key 脱敏（与 llm_client.mask_key 同口径；本地实现，避免跨模块依赖）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "******" + key[-4:]


def _parse_models_payload(payload) -> list:
    """解析各 provider 的模型清单返回体，归一成 id 列表。

    兼容三种常见形态：顶层数组 / {"data":[{id|name}]} / {"models":[...]}。
    """
    out = []

    def _pick(item) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            return str(item.get("id") or item.get("name") or "").strip()
        return ""

    if isinstance(payload, list):
        out = [_pick(x) for x in payload]
    elif isinstance(payload, dict):
        for key in ("data", "models"):
            val = payload.get(key)
            if isinstance(val, list):
                out = [_pick(x) for x in val]
                break
    return [x for x in out if x]


def _detect_provider_models(base_url: str, api_key: str, timeout: float) -> dict:
    """真连网关取模型清单（不抛异常，任何失败收敛成结构化结果）。"""
    import urllib.error
    import urllib.request

    # ⭐ SSRF 防护（t71）：协议白名单 + 主机解析后 IP 校验 + 显式白名单
    _ok, _err = _validate_base_url(base_url)
    if not _ok:
        return {"success": False, "code": "ssrf_blocked", "error": _err, "models_url": ""}
    url = _build_models_url(base_url)
    if not url:
        return {"success": False, "code": "invalid_url", "error": "base_url 非法", "models_url": ""}
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"})
    opener = _safe_models_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200) or 200
            body = resp.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body) if body.strip() else {}
            except ValueError:
                payload = {}
            models = _parse_models_payload(payload)
            if not models:
                return {"success": False, "code": "empty", "models_url": url,
                        "error": ("HTTP %d 但未解析到模型清单（该网关可能不是标准 "
                                  "OpenAI 兼容格式）" % code)}
            return {"success": True, "models_url": url, "models": models, "count": len(models)}
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        if code in (401, 403):
            return {"success": False, "code": "unauthorized", "models_url": url,
                    "error": "密钥被拒绝（HTTP %d），请检查 api_key" % code}
        if code in (404, 405):
            return {"success": False, "code": "models_not_supported", "models_url": url,
                    "error": ("该网关不提供模型清单接口（HTTP %d），请手动填写模型名" % code)}
        return {"success": False, "code": "http_error", "models_url": url,
                "error": "网关返回 HTTP %d" % code}
    except Exception as e:  # noqa: BLE001  连接失败/超时/DNS/SSL 一律视为不可达
        return {"success": False, "code": "unreachable", "models_url": url,
                "error": "端点不可达：%s: %s" % (type(e).__name__, e)}


def _list_provider_models(base_url: str, api_key: str, timeout: float = None,
                          refresh: bool = False) -> dict:
    """提供商模型清单检测（含超时/容错/脱敏 + TTL 缓存）。"""
    import hashlib
    url = _build_models_url(base_url)
    key = (url, hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16])
    now = time.time()
    if not refresh:
        with _MODELS_LOCK:
            hit = _MODELS_CACHE.get(key)
            if hit and (now - hit["ts"]) < _MODELS_TTL:
                out = dict(hit["result"])
                out["cached"] = True
                out["api_key_masked"] = _mask_key(api_key)
                out["base_url"] = (base_url or "").strip()
                return out
    started = time.time()
    result = _detect_provider_models(base_url, api_key, timeout or _MODELS_TIMEOUT)
    result["base_url"] = (base_url or "").strip()
    result["api_key_masked"] = _mask_key(api_key)
    result["cached"] = False
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    with _MODELS_LOCK:
        _MODELS_CACHE[key] = {"ts": time.time(), "result": result}
    return dict(result)


@system_bp.route('/api/ai/models', methods=['POST'])
def api_ai_models():
    """提供商模型清单检测：填 base_url + api_key → 自动列出可用模型（供前端选主/备模型）。

    body: {base_url, api_key, module?, refresh?, timeout?}
      - base_url / api_key：必填（若给 module 则可用该模块已保存的 base_url/key 补缺）；
      - module：text / qc（可选；text 兼作对话总控，chat 旧名会透明归一为 text）；
      - refresh：true 绕过缓存；timeout：秒（默认 8，上限 30）。

    返回（成功）：{success, base_url, models_url, models[模型名清单], count,
                   api_key_masked(脱敏，绝不明文回传), cached, elapsed_ms}
    返回（失败，HTTP 400）：{success, error, code, models_url, base_url, api_key_masked,
                            hint(友好提示)}
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "body 必须是 JSON 对象"}), 400
    base_url = str(data.get("base_url") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    module = str(data.get("module") or "").strip().lower()
    refresh = bool(data.get("refresh"))
    try:
        timeout = float(data.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    timeout = max(0.0, min(timeout, 30.0)) if timeout else 0.0
    if module and (not base_url or not api_key):
        try:
            import ai_config
            from config import AI_CONFIG_PATH, LLM_CONFIG_PATH
            cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
            ep = ai_config.get_module(cfg, module)
            base_url = base_url or str(ep.get("base_url") or "").strip()
            api_key = api_key or str(ep.get("api_key") or "").strip()
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": "读取模块配置失败：%s" % e}), 400
    if not base_url:
        return jsonify({"success": False, "error": "base_url 不能为空"}), 400
    if not api_key:
        return jsonify({"success": False, "error": "api_key 不能为空"}), 400
    result = _list_provider_models(base_url, api_key, timeout=timeout or None, refresh=refresh)
    if not result.get("success"):
        result["hint"] = (
            "请确认 base_url 是否为 OpenAI 兼容网关、api_key 是否正确；"
            "若网关不提供 /models 清单接口，请手动填写模型名。")
    status = 200 if result.get("success") else 400
    return jsonify(result), status

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
