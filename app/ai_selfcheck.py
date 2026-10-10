"""
AI 可用性前置自检门禁（P0-5 密钥单源化 · 收尾件）

职责：在「服务启动」与「任务开跑」两个时机，确定性地回答三个问题——
  1. 三个 AI 模块（text / qc / chat）的 base_url / model / api_key 是否齐全？
  2. 已配置模块的端点是否可达、密钥是否被网关接受？
  3. 若不满足，用户具体该去哪里修？

背景（P0-5 要解决的故障）：`ai_config.json` 三模块 api_key 为空、`qc_config.json`
的 api_key 与 endpoint_override 为空、旧 `llm_config.json` 指向本地 mock，
配置页「测试连接」与运行时读的不是同一份副本 → 「测试通过、运行时 401」，
整条流水线静默失败且错误信息不透出。本模块就是那道「不允许带病开跑」的闸门。

设计约束：
- **只读**：不改任何配置、不落盘。任何异常都收敛成结构化结果，绝不打断调用方主流程。
- **单一事实源**：配置一律走 `ai_config.get_module()`（DB 优先，env > DB），
  与运行态 `_ai_client_for_module()` 同一条读取路径 —— 从根上杜绝「测 A 读 B」。
- **探测轻量且带缓存**：`GET {base_url}/models`，结果缓存 60s，
  避免把「开跑门禁」变成每生产一集都打一次外网。
- **逃生口**：`MJSCXT_AI_GATE_OFF=1` 时 `gate()` 恒放行（仅告警），
  供断网联调 / 局部单测使用。
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# ⭐ 2026-10-10：原先在代码目录找 ai_config.json / llm_config.json，而它们在数据根。
#   config 已有这两个路径常量，直接复用（避免第二事实源）。
from config import AI_CONFIG_PATH as _AI_CONFIG_PATH, LLM_CONFIG_PATH as _LLM_CONFIG_PATH

# ⭐ 2026-10-10：原为本地重复定义（3 处同值），统一到 config.AI_MODULES ——
# 改一处即可，不再有「改了这本、漏了那本」。
from config import AI_MODULES as MODULES

MODULE_LABELS = {
    "text": "文本分析模型",
    "qc": "质检模型",
    "chat": "对话总控模型",
}

MODULE_USED_BY = {
    "text": "小说转剧本 / 章节转剧本 / 提示词分析 / 剧本生成",
    "qc": "分镜图片质检 / 视频质检",
    "chat": "AI 对话总控（创作设定）",
}

# 动作 → 必需模块。**只列「缺了就必然炸」的 LLM 动作**。
#
# 加新动作前先自问一句：这一步真的读 AI 凭证吗？
#   ・读（调 LLM 生成文本 / 对话）        → 列入。
#   ・不读（ComfyUI / Qwen-Edit / H3 出图出片）→ **不要列**。出图所用的提示词全部在
#     上游步骤产出并落到剧本/资产数据里，图生成只是消费；列进来等于「AI key 没配就
#     连本来能出的图也拦掉」，护栏误伤业务（assets/storyboards/keyframes 已因此摘除）。
#   ・qc 质检                            → **不要列**。P0-2 起质检是 fail-open（放行 + 告警），
#     「接口不可用」要与「内容不合格」在链路内区分，不该在门口一刀切。
ACTION_MODULES = {
    "script": ("text",),    # 小说转剧本 / 章节转剧本 / 提示词分析：直接驱动 LLM
    "chat": ("chat",),      # AI 对话总控：直接驱动 LLM
    "episode": ("text",),   # 整集流水线：第一步就是章节转剧本，缺 key 必炸
}

_PROBE_CACHE = {}
_PROBE_LOCK = threading.RLock()
_PROBE_TTL = 60.0


def gate_disabled() -> bool:
    """逃生口：MJSCXT_AI_GATE_OFF=1 时门禁恒放行（仍记录告警）。"""
    val = (os.getenv("MJSCXT_AI_GATE_OFF") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _missing_fields(ep: dict) -> list:
    missing = []
    if not str(ep.get("base_url") or "").strip():
        missing.append("base_url")
    if not str(ep.get("model") or "").strip():
        missing.append("model")
    if not str(ep.get("api_key") or "").strip():
        missing.append("api_key")
    return missing


def _probe_endpoint(base_url: str, api_key: str, timeout: float = 6.0) -> tuple:
    """轻量可达性探测：GET {base_url}/models（OpenAI 兼容网关通用）。

    返回 (reachable, detail)：
    - 2xx            → 可达且密钥被接受
    - 401 / 403      → 端点可达但密钥被拒（**必须阻断**：这正是「测试通过、运行时 401」的正主）
    - 其它 HTTP 码   → 端点可达（网关在，只是不提供 /models），不阻断
    - 连接/超时异常  → 不可达

    任何情况下都不抛异常。
    """
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 固定 https/http 网关
            code = getattr(resp, "status", 200) or 200
            return True, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        if code in (401, 403):
            return False, f"端点可达但密钥被拒绝（HTTP {code}）"
        return True, f"HTTP {code}（端点可达，未校验密钥）"
    except Exception as e:  # noqa: BLE001  连接失败/超时/DNS/SSL 一律视为不可达
        return False, f"端点不可达：{type(e).__name__}: {e}"


def _cached_probe(module: str, base_url: str, api_key: str,
                  timeout: float, force: bool = False) -> dict:
    now = time.time()
    if not force:
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(module)
            if hit and (now - hit["ts"]) < _PROBE_TTL:
                return dict(hit)
    reachable, detail = _probe_endpoint(base_url, api_key, timeout)
    rec = {"ts": time.time(), "reachable": reachable, "detail": detail}
    with _PROBE_LOCK:
        _PROBE_CACHE[module] = rec
    return dict(rec)


def reset_probe_cache() -> None:
    """清空端点探测缓存。

    保存 AI 配置后**必须**调用：否则刚把 key 改对，接下来 60s 内的开跑门禁仍会读到
    改之前那次探测的结论，出现「明明改好了还是被拦」的假故障。
    """
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


# 兼容旧调用名
_reset_probe_cache = reset_probe_cache


def check_module(module: str, probe: bool = False, timeout: float = 6,
                 force_probe: bool = False) -> dict:
    """体检单个 AI 模块。

    返回结构（前端红字/接口回显共用）：
    {module, label, configured, missing[], reachable, probed, reason, hint, used_by}
    """
    label = MODULE_LABELS.get(module, module)
    out = {
        "module": module,
        "label": label,
        "used_by": MODULE_USED_BY.get(module, ""),
        "configured": False,
        "missing": [],
        "reachable": None,
        "probed": False,
        "probe_detail": "",
        "reason": "",
        "hint": "",
    }
    try:
        import ai_config
        cfg = ai_config.load_config(_AI_CONFIG_PATH, _LLM_CONFIG_PATH)
        ep = ai_config.get_module(cfg, module)
    except Exception as e:  # noqa: BLE001  读配置失败不得抛给调用方
        out["reason"] = f"配置读取失败：{type(e).__name__}: {e}"
        out["hint"] = "检查 ai_config.json / tasks.db 是否可读后重试"
        return out

    missing = _missing_fields(ep)
    out["missing"] = missing
    out["configured"] = not missing
    if missing:
        out["reason"] = "缺少 " + "、".join(missing)
        out["hint"] = (f"打开「AI 设置」→「{label}」，补齐 {'、'.join(missing)} 并保存"
                       f"（{MODULE_USED_BY.get(module, '')} 读的都是这一份配置）")
        return out

    if not probe:
        out["reason"] = "已配置（未做网络探测）"
        return out

    rec = _cached_probe(module, ep["base_url"], ep["api_key"], timeout, force_probe)
    out["probed"] = True
    out["reachable"] = rec["reachable"]
    out["probe_detail"] = rec["detail"]
    out["reason"] = rec["detail"]
    if not rec["reachable"]:
        out["hint"] = (f"「{label}」{rec['detail']}。请在「AI 设置」页核对 base_url / api_key / "
                       "model 后点「测试连接」确认；若只是网关临时抖动，可稍后重试。")
    return out


def _block_message(blocked: list, results: dict) -> str:
    if not blocked:
        return "AI 前置自检通过"
    parts = [f"「{results[m]['label']}」{results[m]['reason']}" for m in blocked]
    return "AI 前置自检未通过：" + "；".join(parts)


def check_modules(modules=None, probe: bool = False, timeout: float = 6,
                  force_probe: bool = False) -> dict:
    """批量体检（默认三个模块全查）。probe=True 时附带端点可达性探测。"""
    mods = tuple(modules or MODULES)
    results = {}
    for m in mods:
        results[m] = check_module(m, probe=probe, timeout=timeout, force_probe=force_probe)
    blocked = [m for m in mods
               if (not results[m]["configured"])
               or (probe and results[m].get("reachable") is False)]
    hints = []
    for m in blocked:
        h = results[m].get("hint") or ""
        if h and h not in hints:
            hints.append(h)
    return {
        "ok": not blocked,
        "probed": bool(probe),
        "modules": results,
        "blocked_modules": blocked,
        "blocked_labels": [results[m]["label"] for m in blocked],
        "message": _block_message(blocked, results),
        "hint": " ；".join(hints),
    }


def gate(action: str, probe: bool = True, timeout: float = 6,
         force_probe: bool = False) -> dict:
    """开跑前门禁：动作开跑前调用。

    `ok=False` 时调用方**必须直接阻断**（HTTP 400 + 修复指引），不允许进入「执行中」。
    probe 默认 True —— 「配置齐全但端点 401」同样要拦，那正是 P0-5 记载的原始故障。
    """
    mods = ACTION_MODULES.get(action)
    if not mods:
        return {"ok": True, "action": action, "skipped": True, "blocked_modules": [],
                "message": f"{action} 无需 AI 门禁", "hint": ""}
    rep = check_modules(mods, probe=probe, timeout=timeout, force_probe=force_probe)
    rep["action"] = action
    if not rep["ok"] and gate_disabled():
        logger.warning("AI 门禁被 MJSCXT_AI_GATE_OFF 跳过（action=%s）：%s", action, rep["message"])
        rep["bypassed"] = True
        rep["ok"] = True
        rep["message"] = "（已按 MJSCXT_AI_GATE_OFF 跳过门禁）" + rep["message"]
    elif not rep["ok"]:
        logger.error("AI 门禁阻断（action=%s）：%s", action, rep["message"])
    return rep


def startup_report(probe: bool = False) -> dict:
    """服务启动时的 AI 配置体检（默认不做网络探测 —— 不拖慢启动、不依赖外网）。

    返回与 check_modules 同构的结果，供 app.py 打日志 / 暴露接口。
    """
    rep = check_modules(MODULES, probe=probe)
    try:
        if rep["ok"]:
            logger.info("AI 前置自检：三个模块均已配置（未做网络探测）")
        else:
            logger.error("=" * 76)
            logger.error("【AI 前置自检】未通过 —— 相关任务会在开跑前被阻断，请补齐后重试：")
            for m in rep["blocked_modules"]:
                r = rep["modules"][m]
                logger.error("  - %s：%s", r["label"], r["reason"])
                logger.error("    修复：%s", r["hint"])
            logger.error("=" * 76)
    except Exception:  # noqa: BLE001  日志失败绝不影响启动
        pass
    return rep
