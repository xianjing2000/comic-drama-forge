# -*- coding: utf-8 -*-
"""质检配置 / 闸门 / 历史记录（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 6 步，2026-10-10）

core 模块（asset_worker / keyframe_helpers / mix_helpers / video_helpers …）需要这组能力，
却只能从 routes/_shared 取 —— 它们与 HTTP 毫无关系。上移到 app/ 层后，
core 侧依赖的是 app 层模块，而不是路由层私有模块。

## 铁律

不得反向依赖 routes/*。所依赖的业务模块均已核对不导入 routes._shared。
"""
from __future__ import annotations

import qc_client
import style_kit
from config import QC_CONFIG_PATH, QC_DIR
from shared_base import _app_logger


def _qc_load_cfg() -> dict:
    return qc_client.load_config(QC_CONFIG_PATH)


def _qc_gate(verdict: dict) -> dict:
    """统一质检入库闸门（P0）：ok=false 或 不达标 一律不得静默入库。
    - skipped=True   → 质检未执行（总开关/类型开关关闭、接口未配置），按「放行」处理并明确标注；
    - ok=False       → 质检调用异常，结果不可判定，一律阻断（不得静默入库）；
    - accepted=False → 不通过；命中关键缺陷时 blocked=True（关键缺陷阻断）。

    P0 加固（不盲信 verdict.accepted）：无论上游 verdict 的 accepted / passed / blocked
    给什么值，本函数都会用本地关键缺陷词表对 issues（并集上游显式 critical_issues）做一次
    独立复核；一旦命中关键缺陷（画面崩坏 / 拼接 / 人物重复 等），**强制阻断**，不得因为
    模型自评 accepted=True 而放行。放行条件是 accepted 与 passed **同时**为真（任一为假即阻断）。
    """
    verdict = verdict or {}
    if verdict.get("skipped"):
        return {"accept": True, "blocked": False, "skipped": True, "label": "质检未执行",
                "reason": verdict.get("reason") or "质检未执行（跳过）", "critical_issues": []}
    if not verdict.get("ok"):
        # P0-2：区分「接口级故障」与「内容不合格」。
        # interface_fault=True（鉴权 401/403、超时、网络抖动、未配置 key）= 根本没拿到
        # 模型判定，**不等于**产物不合格——ComfyUI 已出好的图/片不能因质检 key 失效被丢弃。
        # 此时 fail-open：放行已产出资产 + 响亮告警；但若客观层已命中致命缺陷
        # （全黑/无音轨等确定性闸门，见 critical_issues）仍强制阻断，不放行真坏帧。
        if verdict.get("interface_fault"):
            crit = [str(x) for x in (verdict.get("critical_issues") or [])]
            if crit:
                return {"accept": False, "blocked": True, "skipped": False,
                        "fault_open": False, "label": "客观层致命缺陷（AI 质检接口不可用）",
                        "reason": "；".join(crit[:3]), "critical_issues": crit}
            _app_logger().warning(
                "质检接口故障，已 fail-open 放行本资产（结果不可判定）：%s",
                verdict.get("error") or "质检接口不可用")
            return {"accept": True, "blocked": False, "skipped": False,
                    "fault_open": True, "label": "质检接口故障·已放行",
                    "reason": (verdict.get("error") or "质检接口不可用")
                              + "（接口级故障，未做内容判定，已放行）",
                    "critical_issues": []}
        return {"accept": False, "blocked": True, "skipped": False, "label": "质检调用异常",
                "reason": verdict.get("error") or "质检调用失败，结果不可判定", "critical_issues": []}

    # ---- 独立复核：本地词表命中 ∪ 上游显式 critical_issues（不依赖模型自评结论） ----
    local_hits = qc_client.find_critical_issues(verdict.get("issues") or [])
    declared = [str(x) for x in (verdict.get("critical_issues") or [])]
    crit = list(dict.fromkeys(list(local_hits) + declared))
    if crit:
        _app_logger().warning(f"质检闸门独立复核命中关键缺陷，强制阻断：{crit[:3]}")
        return {"accept": False, "blocked": True, "skipped": False,
                "label": "关键缺陷阻断（独立复核）" if local_hits else "关键缺陷阻断",
                "reason": verdict.get("reason") or ("命中关键缺陷：" + "；".join(crit[:3])),
                "critical_issues": crit}

    accepted = verdict.get("accepted")
    passed = verdict.get("passed")
    if accepted is None:
        accepted = bool(passed)
    if passed is None:
        passed = bool(accepted)
    if not (bool(accepted) and bool(passed)):
        if verdict.get("style_mismatch"):
            return {"accept": False, "blocked": bool(verdict.get("blocked")),
                    "skipped": False, "style_blocked": True, "label": "风格不达标",
                    "reason": verdict.get("reason") or "画面风格与目标风格不符",
                    "critical_issues": [],
                    "style_issues": verdict.get("style_issues") or []}
        return {"accept": False, "blocked": bool(verdict.get("blocked")), "skipped": False,
                "label": "质检不达标", "reason": verdict.get("reason") or "质检未达标",
                "critical_issues": []}
    return {"accept": True, "blocked": False, "skipped": False, "label": "质检达标",
            "reason": verdict.get("reason") or "", "critical_issues": []}


def _qc_record(project_name: str, kind: str, shot_id, payload: dict) -> str:
    """写一条质检/重试历史（失败也不影响主流程）"""
    try:
        return qc_client.append_history(QC_DIR, project_name, kind, shot_id, payload)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"质检历史写入失败（忽略）：{e}")
        return ""


def _qc_record_verdict(project_name: str, kind: str, shot_key, stage: str,
                       attempt: int, seed, file_path: str, verdict: dict,
                       extra: dict = None, style: str = "") -> dict:
    """把一次质检结论整理成历史记录并落盘，返回该记录（含 history_file）

    style：本次质检所用的目标风格串。连同 style_mismatch / style_issues 一并落盘，
    供教训库识别「风格不达标」并触发改写提示词重生成。
    """
    rec = {"attempt": attempt, "seed": seed, "file": file_path, "stage": stage,
           "ok": bool(verdict.get("ok")), "passed": bool(verdict.get("passed")),
           "accepted": bool(verdict.get("accepted") if verdict.get("accepted") is not None
                            else verdict.get("passed")),
           "blocked": bool(verdict.get("blocked")),
           "score": verdict.get("score"), "reason": verdict.get("reason"),
           "issues": verdict.get("issues") or [],
           "critical_issues": verdict.get("critical_issues") or [],
           "style_mismatch": bool(verdict.get("style_mismatch")),
           "style_issues": verdict.get("style_issues") or [],
           "style": style_kit.normalize_style(style),
           "error": verdict.get("error"), "latency_ms": verdict.get("latency_ms"),
           # P0-2：接口级故障（鉴权/超时/网络）标记，供 _qc_summary 区分「故障放行」与「内容不合格」
           "interface_fault": bool(verdict.get("interface_fault")),
           # ★ 二次复核留档：首次判不过时用同一张图再判一次（判官抖动实测极大）。
           #   落盘后可直接统计「多少重跑是被复核拦下来的」，用于评估该机制收益。
           "recheck": verdict.get("recheck") or None,
           "recheck_first": verdict.get("recheck_first") or None}
    if extra:
        rec.update(extra)
    rec["history_file"] = _qc_record(project_name, kind, shot_key, rec)
    return rec


