# -*- coding: utf-8 -*-
"""AI 模块客户端构造与前置门禁（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 3 步，2026-10-10）

前两步（shared_base / shared_web）抽的是**零业务依赖**的地基。本步情况不同：
这一组确实依赖 ai_config / llm_client / config —— 但那些是**业务基础设施**，
不是 HTTP 路由层。把它们从 routes/_shared.py 上移到 app/ 层之后，
core 模块（如 storyboard_helpers）若要继续取用，依赖的就只是 app 层模块，
而不是「HTTP 路由层的私有模块」—— 分层倒置照旧被修正。

## 内容

  · AI_MODULE_LABEL       —— 模块显示名（text / qc / chat），给错误文案用
  · _ai_client_for_module —— 构造某模块客户端（含备用模型故障转移链）
  · _current_llm_client   —— 文本分析链路客户端（未配置抛 LLMError）
  · _optional_llm_client  —— best-effort 版本（未配置返回 None，不抛）
  · _ai_guide_response    —— 未配置时的可读引导响应
  · _ai_gate_or_400       —— 开跑前 AI 前置门禁（fail-open）

⚠️ 不得让本模块反向依赖 routes/*（否则只是把倒置换个方向）。
ai_selfcheck 仍在 _ai_gate_or_400 内部延迟导入，保持原样。
"""
from __future__ import annotations

from flask import jsonify

import ai_config
from config import AI_CONFIG_PATH, AI_MODULES, LLM_CONFIG_PATH, LLM_REQUEST_TIMEOUT
from llm_client import FailoverLLMClient, LLMClient, LLMError, build_failover_chain
from shared_base import _app_logger


AI_MODULE_LABEL = {m: ai_config.MODULE_META[m]["label"] for m in AI_MODULES}


def _ai_client_for_module(module: str, base_url: str = None, api_key: str = None,
                          model: str = None, timeout: int = None,
                          reasoning_effort: str = None,
                          with_fallbacks: bool = True) -> LLMClient:
    """构造某个 AI 模块的客户端；传参可用于「测试连接」（不落盘）

    ⚠️ reasoning_effort 必须一起透传：它决定请求体注入哪个思考档位。
    漏掉它会导致两种事故——(1) 测试连接时按「不注入档位」探测，与保存后的真实行为不一致；
    (2) always-on reasoning 的模型（GLM-5.3-Flash）拿不到额度下限，max_tokens 太小 →
    正文全空只剩 reasoning_content，被误判成「模型不支持」。
    """
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    _re = None if reasoning_effort is None else str(reasoning_effort).strip()
    ep = {
        "base_url": (base_url or "").strip() or saved["base_url"],
        "api_key": (api_key or "").strip() or saved["api_key"],
        "model": (model or "").strip() or saved["model"],
        # 档位：本次显式传参优先，否则沿用已保存值（None 表示「不改动」语义）
        "reasoning_effort": (saved.get("reasoning_effort") or "") if _re is None else _re,
    }
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        raise LLMError(f"「{AI_MODULE_LABEL.get(module, module)}」尚未配置"
                       "（base_url / api_key / model 均为必填）")
    primary = LLMClient(AI_CONFIG_PATH, config=ep, timeout=timeout or LLM_REQUEST_TIMEOUT)
    # ⭐ t74：三层故障转移链 = 主模型 → 同提供商 model_fallbacks（复用主 api_key）→
    #   其它提供商 fallbacks（各自 base_url/api_key/model）。组装逻辑统一在
    #   llm_client.build_failover_chain，这里只把 saved 的两层备用传进去。
    # with_fallbacks=False 用于「测试连接」：探针只应该测这一个端点，不带故障转移链。
    if with_fallbacks:
        return build_failover_chain(
            primary,
            model_fallbacks=saved.get("model_fallbacks") or [],
            fallbacks=saved.get("fallbacks") or [],
            config_path=AI_CONFIG_PATH,
            timeout=timeout,
            module=module,
        )
    return primary


def _current_llm_client() -> LLMClient:
    """文本分析链路（小说转剧本 / 章节转剧本 / 提示词分析）专用客户端：只用「文本分析模型」"""
    return _ai_client_for_module("text")


def _optional_llm_client():
    """best-effort 取「文本分析模型」客户端：已配置返回 LLMClient，未配置/异常返回 None。

    用于「LLM 锦上添花但不可阻断主流程」的场景（如上传时用 LLM 归纳章节标题正则，
    失败则退回纯正则切分）。与 _current_llm_client 的区别是**不抛 LLMError**。"""
    try:
        return _current_llm_client()
    except LLMError:
        return None


def _ai_guide_response(message: str, code: int = 400, module: str = "text"):
    label = AI_MODULE_LABEL.get(module, module)
    return jsonify({
        "success": False,
        "error": message,
        "code": "LLM_NOT_CONFIGURED",
        "module": module,
        "guide": (f"请点击顶部「AI 设置」→「{label}」，填写 ① base_url ② api_key ③ model，"
                  "先点「测试连接」通过后再点「保存」。三个模型相互独立配置，互不影响。"),
        "config": ai_config.public_view(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)),
    }), code


def _ai_gate_or_400(action: str, probe: bool = True):
    """开跑前 AI 前置门禁（P0-5 收尾）。

    通过 → 返回 None（调用方继续）；未通过 → 返回可直接 `return` 的 (response, 400) 元组。

    为什么放在每个生产入口而不是散在内部：修复前的故障是「配置页测试通过、运行时 401、
    整条流水线静默失败」，用户看不到任何原因。门禁要在**进入执行前**就把话说明白
    （缺什么、去哪修），而不是跑 4 小时后再炸。

    门禁自身异常按 fail-open 放行并响亮告警 —— 门禁是护栏，不是业务本身，
    绝不能因为护栏故障把整条线堵死。
    """
    try:
        import ai_selfcheck
        rep = ai_selfcheck.gate(action, probe=probe)
    except Exception as e:  # noqa: BLE001
        _app_logger().error(f"AI 门禁执行异常（按放行处理，action={action}）：{type(e).__name__}: {e}")
        return None
    if rep.get("ok"):
        return None
    return jsonify({
        "success": False,
        "error": rep.get("message") or "AI 前置自检未通过，已阻断执行",
        "message": rep.get("message") or "",
        "hint": rep.get("hint") or "",
        "ai_selfcheck": {
            "action": action,
            "ok": False,
            "blocked_modules": rep.get("blocked_modules") or [],
            "blocked_labels": rep.get("blocked_labels") or [],
            "modules": rep.get("modules") or {},
            "hint": rep.get("hint") or "",
            "gate_off": False,
            "fix_url": "/api/ai/selfcheck?probe=1",
        },
    }), 400


