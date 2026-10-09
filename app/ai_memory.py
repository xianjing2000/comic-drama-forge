# -*- coding: utf-8 -*-
"""
AI Memory System - 自动进化记忆模块

功能：
1. 语义记忆 - 存储和检索历史经验
2. 模式识别 - 发现重复问题和优化方向
3. 经验总结 - 从失败中学习，自动改进提示词
4. 自进化 - 基于历史数据优化生产流程

数据落盘：output/memory/
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 原子写（删除项目时按项目键摘除 memories.json 条目后写回，2026-10-06）。
# fs_atomic 只依赖标准库，可被任意业务模块顶层 import，无循环导入风险。
from fs_atomic import atomic_write_json

logger = logging.getLogger(__name__)


class AISemoryEntry:
    """单条记忆条目"""

    def __init__(
        self,
        mem_id: str,
        mem_type: str,
        content: str,
        context: Dict[str, Any],
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
        source: Optional[str] = None,
        created_at: Optional[str] = None,
    ):
        self.mem_id = mem_id
        self.mem_type = mem_type  # 'pattern', 'lesson', 'success', 'failure', 'insight'
        self.content = content
        self.context = context
        self.confidence = confidence
        self.tags = tags or []
        self.source = source
        self.created_at = created_at or datetime.now().isoformat()
        self.usage_count = 0
        self.last_used = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mem_id": self.mem_id,
            "mem_type": self.mem_type,
            "content": self.content,
            "context": self.context,
            "confidence": self.confidence,
            "tags": self.tags,
            "source": self.source,
            "created_at": self.created_at,
            "usage_count": self.usage_count,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AISemoryEntry":
        mem = cls(
            mem_id=data.get("mem_id", ""),
            mem_type=data.get("mem_type", "insight"),
            content=data.get("content", ""),
            context=data.get("context", {}),
            confidence=data.get("confidence", 1.0),
            tags=data.get("tags", []),
            source=data.get("source"),
            created_at=data.get("created_at"),
        )
        mem.usage_count = data.get("usage_count", 0)
        mem.last_used = data.get("last_used")
        return mem


class AIPatternRecognizer:
    """模式识别器 - 发现重复问题和成功模式"""

    # 已知问题模式
    PATTERN_RULES = [
        {"keywords": ["发色", "头发颜色"], "pattern": "hair_color", "category": "character_consistency"},
        {"keywords": ["服饰", "服装", "衣服"], "pattern": "costume", "category": "character_consistency"},
        {"keywords": ["面部", "五官", "脸型"], "pattern": "face", "category": "quality"},
        {"keywords": ["手部", "手指"], "pattern": "hands", "category": "quality"},
        {"keywords": ["构图", "主体", "前景"], "pattern": "composition", "category": "storyboard"},
        {"keywords": ["光影", "照明", "光线"], "pattern": "lighting", "category": "quality"},
        {"keywords": ["背景", "场景"], "pattern": "background", "category": "scene"},
        {"keywords": ["比例", "透视"], "pattern": "perspective", "category": "quality"},
        {"keywords": ["模糊", "噪点", "清晰度"], "pattern": "clarity", "category": "quality"},
        {"keywords": ["水印", "logo", "字幕"], "pattern": "watermark", "category": "quality"},
    ]

    # 成功模式关键词
    SUCCESS_PATTERNS = [
        {"keywords": ["完美", "满意", "优秀", "高质量"], "pattern": "success_high", "weight": 1.5},
        {"keywords": ["符合", "一致", "准确"], "pattern": "success_accurate", "weight": 1.2},
        {"keywords": ["自然", "流畅", "生动"], "pattern": "success_natural", "weight": 1.1},
    ]

    def detect_pattern(self, text: str) -> List[Dict[str, Any]]:
        """检测文本中的问题模式"""
        detected = []
        text_lower = text.lower()

        for rule in self.PATTERN_RULES:
            if any(kw in text_lower for kw in rule["keywords"]):
                detected.append({
                    "pattern": rule["pattern"],
                    "category": rule["category"],
                    "severity": "high" if len(rule["keywords"]) > 2 else "medium",
                })

        return detected

    def detect_success(self, text: str) -> List[Dict[str, Any]]:
        """检测成功模式"""
        detected = []
        text_lower = text.lower()

        for pattern in self.SUCCESS_PATTERNS:
            if any(kw in text_lower for kw in pattern["keywords"]):
                detected.append({
                    "pattern": pattern["pattern"],
                    "weight": pattern["weight"],
                })

        return detected


class AIEvolutionEngine:
    """自进化引擎 - 基于历史数据优化生产"""

    def __init__(self, memory_system: "AISemorySystem"):
        self.memory = memory_system
        self.pattern_recognizer = AIPatternRecognizer()

    def analyze_trends(self) -> Dict[str, Any]:
        """分析生产趋势"""
        all_memories = self.memory.list_all()

        # 按类型统计
        type_counts = {}
        for mem in all_memories:
            t = mem.mem_type
            type_counts[t] = type_counts.get(t, 0) + 1

        # 按类别统计问题
        category_issues = {}
        for mem in all_memories:
            if mem.mem_type == "lesson":
                cat = mem.context.get("category", "unknown")
                category_issues[cat] = category_issues.get(cat, 0) + 1

        # 成功率趋势
        total_lessons = type_counts.get("lesson", 0)
        total_success = type_counts.get("success", 0)
        success_rate = total_success / (total_lessons + total_success) if (total_lessons + total_success) > 0 else 0

        # 置信度分布
        high_conf = sum(1 for m in all_memories if m.confidence >= 0.8)
        avg_conf = sum(m.confidence for m in all_memories) / len(all_memories) if all_memories else 0

        return {
            "total_memories": len(all_memories),
            "type_distribution": type_counts,
            "category_issues": dict(sorted(category_issues.items(), key=lambda x: -x[1])[:10]),
            "success_rate": round(success_rate * 100, 1),
            "high_confidence_ratio": round(high_conf / len(all_memories) * 100, 1) if all_memories else 0,
            "avg_confidence": round(avg_conf, 2),
            "trend": "improving" if success_rate > 0.6 else "stable" if success_rate > 0.4 else "needs_attention",
        }

    def generate_insights(self, limit: int = 5) -> List[Dict[str, Any]]:
        """生成优化建议"""
        insights = []

        # 分析高频问题
        trends = self.analyze_trends()
        top_issues = trends.get("category_issues", {})

        for category, count in list(top_issues.items())[:3]:
            if count >= 3:
                insights.append({
                    "type": "issue_pattern",
                    "category": category,
                    "count": count,
                    "suggestion": self._get_suggestion(category),
                    "priority": "high" if count >= 5 else "medium",
                })

        # 成功模式分析
        all_memories = self.memory.list_all()
        successes = [m for m in all_memories if m.mem_type == "success"]
        if successes:
            # 找出共同特征
            common_tags = self._find_common_tags(successes)
            if common_tags:
                insights.append({
                    "type": "success_pattern",
                    "tags": common_tags[:5],
                    "count": len(successes),
                    "suggestion": f"成功产出常见特征：{', '.join(common_tags[:3])}",
                    "priority": "low",
                })

        # 置信度分析
        low_conf = [m for m in all_memories if m.confidence < 0.5]
        if low_conf:
            insights.append({
                "type": "confidence_issue",
                "count": len(low_conf),
                "suggestion": "发现低置信度记忆，建议重新验证关键经验",
                "priority": "medium",
            })

        return sorted(insights, key=lambda x: ({"high": 0, "medium": 1, "low": 2}.get(x["priority"], 3), -x.get("count", 0)))[:limit]

    def _get_suggestion(self, category: str) -> str:
        """获取类别优化建议"""
        suggestions = {
            "character_consistency": "加强角色一致性控制，使用参考图约束生成",
            "quality": "优化质检阈值，增加重试次数",
            "storyboard": "改进分镜设计，参考经典构图规则",
            "scene": "增强场景描述细节，提供更多参考素材",
        }
        return suggestions.get(category, "持续优化生产流程")

    def _find_common_tags(self, memories: List[AISemoryEntry]) -> List[str]:
        """找出成功产出的共同标签"""
        tag_counts = {}
        for mem in memories:
            for tag in mem.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return sorted(tag_counts.keys(), key=lambda x: -tag_counts[x])

    def auto_optimize_prompt(self, base_prompt: str, issues: List[str]) -> str:
        """基于历史教训自动优化提示词"""
        optimized = base_prompt

        # 查询相关教训
        related_lessons = self.memory.search_by_pattern(base_prompt, limit=5)
        lessons = [m for m in related_lessons if m.mem_type == "lesson"]

        if lessons:
            optimizations = []
            for lesson in lessons[:3]:
                optimization = lesson.content
                if optimization not in optimized:
                    optimizations.append(optimization)

            if optimizations:
                optimized += "\n\n【自动优化建议】\n" + "\n".join(f"- {opt}" for opt in optimizations)

        return optimized


class AISemorySystem:
    """AI 记忆系统主类"""

    def __init__(self, root_dir: str = None):
        self.root_dir = root_dir or os.path.join(os.getcwd(), "output", "memory")
        self.entries: List[AISemoryEntry] = []
        self.lock = threading.Lock()
        self.evolution = AIEvolutionEngine(self)
        self.pattern_recognizer = AIPatternRecognizer()

        # 确保目录存在
        os.makedirs(self.root_dir, exist_ok=True)

        # 加载已有记忆
        self._load()

    def _load(self):
        """从磁盘加载记忆"""
        memory_file = os.path.join(self.root_dir, "memories.json")
        if os.path.exists(memory_file):
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.entries = [AISemoryEntry.from_dict(e) for e in data.get("entries", [])]
                logger.info(f"Loaded {len(self.entries)} memories from disk")
            except Exception as e:
                logger.error(f"Failed to load memories: {e}")

    def _save(self):
        """保存到磁盘"""
        memory_file = os.path.join(self.root_dir, "memories.json")
        try:
            data = {"entries": [e.to_dict() for e in self.entries], "updated_at": datetime.now().isoformat()}
            with open(memory_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"Saved {len(self.entries)} memories to disk")
        except Exception as e:
            logger.error(f"Failed to save memories: {e}")

    def add_memory(
        self,
        mem_type: str,
        content: str,
        context: Dict[str, Any],
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
        source: Optional[str] = None,
    ) -> AISemoryEntry:
        """添加新记忆"""
        mem_id = self._generate_id(mem_type, content)
        entry = AISemoryEntry(
            mem_id=mem_id,
            mem_type=mem_type,
            content=content,
            context=context,
            confidence=confidence,
            tags=tags or [],
            source=source,
        )

        with self.lock:
            self.entries.append(entry)
            self._save()

        logger.info(f"Added memory: {mem_type} - {content[:50]}...")
        return entry

    def search_by_pattern(
        self, query: str, mem_type: Optional[str] = None, limit: int = 10
    ) -> List[AISemoryEntry]:
        """基于模式搜索记忆"""
        results = []
        query_lower = query.lower()

        with self.lock:
            for entry in self.entries:
                if mem_type and entry.mem_type != mem_type:
                    continue

                # 匹配内容
                if query_lower in entry.content.lower():
                    results.append(entry)
                    continue

                # 匹配标签
                if any(query_lower in tag.lower() for tag in entry.tags):
                    results.append(entry)
                    continue

                # 匹配上下文
                for k, v in entry.context.items():
                    if isinstance(v, str) and query_lower in v.lower():
                        results.append(entry)
                        break

                if len(results) >= limit:
                    break

        # 按相关度排序
        results.sort(key=lambda x: self._calculate_relevance(x, query), reverse=True)
        return results[:limit]


    def list_all(self, mem_type: Optional[str] = None, limit: int = 100) -> List[AISemoryEntry]:
        """列出所有记忆"""
        with self.lock:
            if mem_type:
                return [e for e in self.entries if e.mem_type == mem_type][:limit]
            return self.entries[:limit]

    def record_lesson(
        self,
        project: str,
        episode: int,
        prompt: str,
        issues: List[str],
        category: str,
    ) -> AISemoryEntry:
        """记录质检教训"""
        content = f"在项目 {project} 第 {episode} 集中，使用提示词时遇到以下问题：{'、'.join(issues[:3])}"
        context = {
            "project": project,
            "episode": episode,
            "prompt_hash": self._hash(prompt),
            "category": category,
            "issues": issues,
        }

        # 检测模式
        patterns = self.pattern_recognizer.detect_pattern(" ".join(issues))
        tags = [p["pattern"] for p in patterns] + [category]

        entry = self.add_memory(
            mem_type="lesson",
            content=content,
            context=context,
            confidence=0.9,
            tags=tags,
            source="qc_judge",
        )
        return entry

    def record_success(
        self,
        project: str,
        episode: int,
        prompt: str,
        highlights: List[str],
        category: str,
    ) -> AISemoryEntry:
        """记录成功经验"""
        content = f"项目 {project} 第 {episode} 集成功产出，关键要素：{'、'.join(highlights[:3])}"
        context = {
            "project": project,
            "episode": episode,
            "prompt_hash": self._hash(prompt),
            "category": category,
            "highlights": highlights,
        }

        # 检测成功模式
        success_patterns = self.pattern_recognizer.detect_success(" ".join(highlights))
        tags = [p["pattern"] for p in success_patterns] + [category, "success"]

        entry = self.add_memory(
            mem_type="success",
            content=content,
            context=context,
            confidence=0.95,
            tags=tags,
            source="qc_judge",
        )
        return entry


    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计"""
        with self.lock:
            total = len(self.entries)
            by_type = {}
            for e in self.entries:
                by_type[e.mem_type] = by_type.get(e.mem_type, 0) + 1

            return {
                "total": total,
                "by_type": by_type,
                "recent_7d": sum(1 for e in self.entries if self._days_since(e.created_at) <= 7),
                "recent_30d": sum(1 for e in self.entries if self._days_since(e.created_at) <= 30),
            }

    def clear_old(self, days: int = 90) -> int:
        """清理过期记忆"""
        cutoff = datetime.now().timestamp() - days * 86400
        before = len(self.entries)
        with self.lock:
            self.entries = [e for e in self.entries if self._parse_time(e.created_at).timestamp() > cutoff]
            self._save()
        return before - len(self.entries)

    def purge_project(self, projects: List[str]) -> int:
        """项目被删除时，摘除 context.project 命中该项目别名的记忆条目（关联清理）。

        为什么必须做：memories.json 是**全局单文件**、条目级按 ``context.project``
        归属（record_lesson / record_success 落的 project 字段）——软删产物目录收不走，
        必须按键摘除后原子写回，否则同名重建会把旧项目的教训/成功经验挂到新项目头上。

        ``projects`` 传该项目的全部别名（dir_key + 显示名），精确匹配 context.project。
        写回走 fs_atomic.atomic_write_json（唯一临时名 + fsync + .bak 快照 + replace），
        与本类 ``_save`` 的「整文件覆盖」相比多一层防并发截断保护。
        返回摘除条数；任何失败只 warning（记忆是旁路数据，绝不阻断删除主流程）。
        """
        vals = {str(p).strip() for p in (projects or []) if p and str(p).strip()}
        if not vals:
            return 0
        removed = 0
        with self.lock:
            before = len(self.entries)

            def _keep(e: AISemoryEntry) -> bool:
                ctx = e.context if isinstance(e.context, dict) else {}
                proj = str(ctx.get("project") or "").strip()
                return proj not in vals

            self.entries = [e for e in self.entries if _keep(e)]
            removed = before - len(self.entries)
            if removed:
                try:
                    atomic_write_json(
                        os.path.join(self.root_dir, "memories.json"),
                        {"entries": [e.to_dict() for e in self.entries],
                         "updated_at": datetime.now().isoformat()})
                except Exception as e:  # noqa: BLE001
                    logger.warning("AI 记忆摘除写回失败（不影响删除主流程）：%s", e)
        if removed:
            logger.info("AI 记忆已随项目删除摘除 %d 条（context.project ∈ %s）",
                        removed, sorted(vals))
        return removed

    def _generate_id(self, mem_type: str, content: str) -> str:
        """生成记忆 ID"""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        content_hash = self._hash(content)[:8]
        return f"{mem_type}_{timestamp}_{content_hash}"

    def _hash(self, text: str) -> str:
        """计算文本哈希"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

    def _calculate_relevance(self, entry: AISemoryEntry, query: str) -> float:
        """计算相关度"""
        score = 0.0
        query_lower = query.lower()

        # 内容匹配
        if query_lower in entry.content.lower():
            score += 1.0

        # 标签匹配
        tag_matches = sum(1 for tag in entry.tags if query_lower in tag.lower())
        score += tag_matches * 0.5

        # 上下文匹配
        for v in entry.context.values():
            if isinstance(v, str) and query_lower in v.lower():
                score += 0.3
                break

        # 置信度加权
        score *= entry.confidence

        return score

    def _days_since(self, iso_str: str) -> int:
        """计算距离今天的天数"""
        try:
            dt = self._parse_time(iso_str)
            return (datetime.now() - dt).days
        except:
            return 999

    def _parse_time(self, iso_str: str) -> datetime:
        """解析 ISO 时间字符串"""
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except:
            return datetime.now()


# 全局单例
_instance: Optional[AISemorySystem] = None
_instance_lock = threading.Lock()


def get_memory_system(root_dir: str = None) -> AISemorySystem:
    """获取全局记忆系统实例"""
    global _instance
    with _instance_lock:
        if _instance is None or _instance.root_dir != (root_dir or _instance.root_dir):
            _instance = AISemorySystem(root_dir)
        return _instance


if __name__ == "__main__":
    # 测试
    mem = AISemorySystem()

    # 记录一些教训
    mem.record_lesson(
        project="test_novel",
        episode=1,
        prompt="beautiful girl with long black hair",
        issues=["hair color wrong", "face blurred"],
        category="character_consistency",
    )

    mem.record_success(
        project="test_novel",
        episode=2,
        prompt="cinematic lighting with dramatic shadows",
        highlights=["perfect lighting", "great composition"],
        category="quality",
    )

    # 查询
    print("All memories:", len(mem.list_all()))
    print("Lessons:", len(mem.list_all(mem_type="lesson")))
    print("Success:", len(mem.list_all(mem_type="success")))

    # 统计
    stats = mem.get_stats()
    print("Stats:", json.dumps(stats, indent=2, ensure_ascii=False))

    # 趋势分析
    trends = mem.evolution.analyze_trends()
    print("Trends:", json.dumps(trends, indent=2, ensure_ascii=False))

    # 生成洞察
    insights = mem.evolution.generate_insights()
    print("Insights:", json.dumps(insights, indent=2, ensure_ascii=False))
