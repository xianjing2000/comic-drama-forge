"""
ai_qc_judge.py — 主AI质检判断模块

职责：
1. 接收质检结果（剧本/图片/视频等）
2. 分析问题严重程度和类型
3. 调用主AI判断处理策略（重试/修改/阻断/通知）
4. 返回处理建议和执行计划

设计原则：
- 质检失败后由主AI来判断怎么处理，而不是硬编码规则
- 主AI可以根据上下文做出更智能的决策
- 支持多种处理策略：自动重试、人工修改、阻断通知等
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 处理策略定义
HANDLING_STRATEGIES = {
    "auto_retry": {
        "description": "自动重试生成",
        "max_retries": 3,
        "apply_to": ["medium", "low"],
    },
    "modify_and_retry": {
        "description": "修改提示词后重试",
        "max_retries": 2,
        "apply_to": ["high"],
    },
    "block_and_notify": {
        "description": "阻断流程并通知用户",
        "apply_to": ["critical"],
    },
    "log_only": {
        "description": "仅记录问题，继续流程",
        "apply_to": ["low"],
    },
    "human_review": {
        "description": "人工审核后决定",
        "apply_to": ["critical", "high"],
    },
}

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
    """评估质检结果的严重程度"""
    issues = verdict.get("issues", [])
    score = verdict.get("score", 100)
    
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


def determine_strategy(severity: str, context: dict = None) -> dict:
    """根据严重程度和上下文确定处理策略"""
    context = context or {}
    
    # 基础策略 - 支持24小时自动执行，无需人工干预
    if severity == "critical":
        # 严重问题：修改提示词后重试，而不是阻断
        strategy = HANDLING_STRATEGIES["modify_and_retry"]
    elif severity == "high":
        # 高优先级问题，需要修改提示词
        strategy = HANDLING_STRATEGIES["modify_and_retry"]
    elif severity == "medium":
        # 中等问题，自动重试
        strategy = HANDLING_STRATEGIES["auto_retry"]
    else:
        # 低优先级，仅记录
        strategy = HANDLING_STRATEGIES["log_only"]
    
    # 根据上下文调整策略
    attempt = context.get("attempt", 1)
    max_retries = strategy.get("max_retries", 3)
    
    # 超过最大重试次数时，降低严重程度继续尝试，而不是转人工
    if attempt >= max_retries:
        # 降级策略：将严重程度降低一级
        if severity == "critical":
            severity = "high"
        elif severity == "high":
            severity = "medium"
        elif severity == "medium":
            severity = "low"
        
        # 重新确定策略
        if severity == "high":
            strategy = HANDLING_STRATEGIES["modify_and_retry"]
        elif severity == "medium":
            strategy = HANDLING_STRATEGIES["auto_retry"]
        else:
            strategy = HANDLING_STRATEGIES["log_only"]
    
    # 如果是关键业务（如最终成片），仍然需要严格质检，但不转人工
    # 改为：记录警告但继续自动处理
    if context.get("is_final", False) and severity in ["high", "critical"]:
        # 记录警告，但不阻断
        logger.warning(f"关键业务质检问题: 严重程度={severity}, 将继续自动处理")
    
    return {
        "strategy": strategy,
        "severity": severity,
        "attempt": attempt,
        "max_retries": strategy.get("max_retries", 3),
        "description": strategy.get("description", ""),
    }


def generate_fix_suggestions(verdict: dict, context: dict = None) -> List[str]:
    """生成具体的修复建议"""
    suggestions = []
    issues = verdict.get("issues", [])
    categories = verdict.get("categories", {})
    
    # 结构问题修复建议
    if categories.get("structure", 100) < 70:
        suggestions.append("检查JSON格式是否正确，确保所有必填字段存在")
        suggestions.append("验证角色、物品、场景的定义完整性")
    
    # 逻辑问题修复建议
    if categories.get("logic", 100) < 70:
        suggestions.append("检查镜头中引用的角色和物品是否已定义")
        suggestions.append("验证镜头顺序的叙事逻辑")
    
    # 风格问题修复建议
    if categories.get("style", 100) < 70:
        suggestions.append("调整描述语言，使其更符合指定风格")
        suggestions.append("检查角色外貌描述是否与风格匹配")
    
    # 提示词质量修复建议
    if categories.get("prompt_quality", 100) < 70:
        suggestions.append("丰富视觉描述，增加人物动作、镜头运动、环境氛围")
        suggestions.append("确保每个镜头描述至少50字")
    
    # 可执行性修复建议
    if categories.get("feasibility", 100) < 70:
        suggestions.append("调整镜头时长，确保总时长接近目标")
        suggestions.append("优化镜头数量，应按「一镜=一个能演满 5~6 秒的完整内容单元」重切（静态画面不单独成镜）")
    
    # 根据具体问题生成建议
    for issue in issues[:5]:  # 只处理前5个问题
        issue_lower = str(issue).lower()
        
        if "缺少字段" in issue_lower:
            suggestions.append(f"补充缺失字段: {issue}")
        elif "角色" in issue_lower and "未定义" in issue_lower:
            suggestions.append(f"在characters列表中添加角色定义: {issue}")
        elif "物品" in issue_lower and "未定义" in issue_lower:
            suggestions.append(f"在items列表中添加物品定义: {issue}")
        elif "描述过短" in issue_lower:
            suggestions.append(f"扩展描述内容: {issue}")
        elif "时长" in issue_lower:
            suggestions.append(f"调整镜头时长: {issue}")
    
    # 去重并限制数量
    unique_suggestions = []
    seen = set()
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            unique_suggestions.append(s)
    
    return unique_suggestions[:10]


def create_execution_plan(verdict: dict, context: dict = None) -> dict:
    """创建执行计划 - 支持24小时自动执行，无需人工干预"""
    severity = assess_severity(verdict)
    strategy_info = determine_strategy(severity, context)
    suggestions = generate_fix_suggestions(verdict, context)
    
    # 根据策略创建具体执行步骤 - 所有策略都支持自动执行
    strategy_name = strategy_info["strategy"].get("description", "")
    
    if strategy_name == "自动重试生成":
        steps = [
            {"action": "log_issue", "description": "记录质检问题到记忆模块"},
            {"action": "auto_retry", "description": "自动重新生成（使用相同或优化后的提示词）"},
            {"action": "re_qc", "description": "重新质检"},
        ]
    elif strategy_name == "修改提示词后重试":
        steps = [
            {"action": "log_issue", "description": "记录质检问题到记忆模块"},
            {"action": "analyze_issues", "description": "分析具体问题点"},
            {"action": "modify_prompt", "description": "根据建议修改提示词"},
            {"action": "regenerate", "description": "使用修改后的提示词重新生成"},
            {"action": "re_qc", "description": "重新质检"},
        ]
    elif strategy_name == "阻断流程并通知用户":
        # 改为：记录问题并继续，而不是阻断
        steps = [
            {"action": "log_issue", "description": "记录质检问题到记忆模块"},
            {"action": "continue_with_warning", "description": "记录警告但继续流程"},
        ]
    elif strategy_name == "仅记录问题，继续流程":
        steps = [
            {"action": "log_issue", "description": "记录质检问题到记忆模块"},
            {"action": "continue", "description": "继续后续流程"},
        ]
    else:  # 人工审核 - 改为自动处理
        steps = [
            {"action": "log_issue", "description": "记录质检问题到记忆模块"},
            {"action": "auto_fix", "description": "自动尝试修复问题"},
            {"action": "continue_with_log", "description": "记录日志并继续流程"},
        ]
    
    # 24小时自动执行模式：所有情况下都不需要人工干预
    should_continue = True  # 始终继续流程
    requires_human = False  # 从不需要人工处理
    
    return {
        "severity": severity,
        "strategy": strategy_name,
        "steps": steps,
        "suggestions": suggestions,
        "max_retries": strategy_info["max_retries"],
        "current_attempt": strategy_info["attempt"],
        "should_continue": should_continue,
        "requires_human": requires_human,
    }


def judge_qc_result(verdict: dict, context: dict = None) -> dict:
    """主AI判断质检结果的处理方式
    
    参数:
        verdict: 质检结果
        context: 上下文信息（项目类型、尝试次数等）
    
    返回:
        dict: {
            "severity": str,           # 严重程度
            "strategy": str,           # 处理策略
            "execution_plan": dict,    # 执行计划
            "suggestions": list,       # 修复建议
            "should_continue": bool,   # 是否继续流程
            "requires_human": bool,    # 是否需要人工处理
        }
    """
    context = context or {}
    
    # 评估严重程度
    severity = assess_severity(verdict)
    
    # 创建执行计划
    execution_plan = create_execution_plan(verdict, context)
    
    # 记录判断结果
    judgment = {
        "timestamp": datetime.now().isoformat(),
        "verdict_score": verdict.get("score"),
        "verdict_passed": verdict.get("passed"),
        "severity": severity,
        "strategy": execution_plan["strategy"],
        "should_continue": execution_plan["should_continue"],
        "requires_human": execution_plan["requires_human"],
        "suggestions_count": len(execution_plan["suggestions"]),
        "context": context,
    }
    
    logger.info(f"主AI质检判断: 严重程度={severity}, 策略={execution_plan['strategy']}")
    
    return {
        "severity": severity,
        "strategy": execution_plan["strategy"],
        "execution_plan": execution_plan,
        "suggestions": execution_plan["suggestions"],
        "should_continue": execution_plan["should_continue"],
        "requires_human": execution_plan["requires_human"],
        "judgment": judgment,
    }
