"""
ai_qc_judge.py — 质检严重度判定（单一来源）。

职责：
- assess_severity：把质检 verdict 判成 critical/high/medium/low 严重度。

⚠️ 2026-10-12 t68：原「主AI质检判断」链路（judge_qc_result / create_execution_plan /
determine_strategy / generate_fix_suggestions / HANDLING_STRATEGIES）在 generate_script_with_qc
被 t40 删除后已成死代码（grep 全仓无生产调用方），本次一并删除。
生产路径的质检阻断已由 routes/script_api.py 的确定性阻断承接（passed=False → 不交付），
不再依赖「主AI判断」重链路。

唯一对外接口：
- assess_severity：被 prompt_memory.assess_priority 复用（构造等价 verdict 判严重度）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# 严重程度判断关键词
CRITICAL_KEYWORDS = [
    "结构错误", "JSON格式错误", "逻辑矛盾", "角色缺失", "物品缺失",
    "严重畸变", "崩坏", "黑屏", "白屏", "主体缺失",
]

HIGH_KEYWORDS = [
    "风格不符", "时长偏差过大", "提示词不完整", "关键动作缺失",
    "景别不符", "构图严重问题",
]

MEDIUM_KEYWORDS = [
    "描述过短", "描述不详细", "时长偏差", "镜头数量不足",
    "轻微构图问题", "颜色偏差",
]


def assess_severity(verdict: dict) -> str:
    """评估质检结果的严重程度。

    2026-10-12 t68：verdict 的 passed=False 是硬信号 —— 质检明确「未通过」时，
    不得因 score 高/无关键词而降级为 low 放行（architect 实测反例：passed=False +
    score=85 且无关键词 → 原判 low → should_continue=True → 放行）。
    passed 缺失（如 prompt_memory.assess_priority 构造的无 passed 等价 verdict）时，
    沿用原关键词/分数判据，行为不变。
    """
    issues = verdict.get("issues", [])
    score = verdict.get("score", 100)

    # 质检明确「未通过」：passed=False → 至少 medium（低分则更重），绝不放行为 low。
    if verdict.get("passed") is False:
        if score < 50:
            return "critical"
        if score < 70:
            return "high"
        return "medium"

    # 检查关键缺陷
    critical_hits = verdict.get("critical_issues", [])
    if critical_hits:
        return "critical"

    # 检查关键词
    all_issues_text = " ".join(str(i) for i in issues).lower()

    for keyword in CRITICAL_KEYWORDS:
        if keyword.lower() in all_issues_text:
            return "critical"

    for keyword in HIGH_KEYWORDS:
        if keyword.lower() in all_issues_text:
            return "high"

    # 根据分数判断
    if score < 50:
        return "critical"
    elif score < 70:
        return "high"
    elif score < 80:
        return "medium"
    else:
        return "low"
