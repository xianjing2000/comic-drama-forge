# -*- coding: utf-8 -*-
"""AI 记忆系统 —— **质检教训库（prompt_memory）的只读门面**。

2026-10-16 收敛（原「自进化」死壳）
----------------------------------
本模块原是一套独立记忆 + 「自进化引擎」（`AIEvolutionEngine`），自建
`output/memory/memories.json`。但**没有任何生产链路往里写**（实测只有 3 条 test
数据）→ 是个死壳，且与**真正在用的** `prompt_memory`（生成链路的质检教训库：
不达标自动沉淀 → 重试/首建召回改提示词）形成「同一件事两套数据」，记忆页得同时读
两份才看得到成果。

现收敛为**唯一来源 = prompt_memory**：
  · 本模块**不再自建存储**；读取/统计/洞察全部由 prompt_memory 派生；
  · 公开 API 名保持不变（`get_memory_system` / `AIMemorySystem` / `list_all` /
    `get_stats` / `.evolution.*` …），故 `routes/memory.py`、`project_store`（删项目
    清记忆）、前端 `MemoryPage` **无需改动**；
  · 写入类方法（`record_lesson` / `record_success` / `add_memory`）改为**写进
    prompt_memory**，语义统一，不再维护第二份存储。

为什么保留本模块而非删除：`/api/memory/*` 若干端点 + 「删项目清记忆」+ 记忆页仍按
这些函数名调用；做成门面比全量改路由 + 前端 + 测试的风险低得多。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import prompt_memory
from config import PROJECT_OUTPUT_DIR

logger = logging.getLogger(__name__)


def _map_lesson(l: dict) -> Dict[str, Any]:
    """prompt_memory 教训 → 前端/历史 API 期望的记忆形状（字段名沿用历史）。"""
    l = l or {}
    tags: List[str] = []
    for x in ([l.get("kind"), l.get("category")] + list(l.get("terms") or [])):
        if x and str(x) not in tags:
            tags.append(str(x))
    try:
        conf = round(float(l.get("score") or 100) / 100.0, 3)
    except (TypeError, ValueError):
        conf = 1.0
    return {
        "mem_id": l.get("lesson_id") or "",
        "mem_type": "lesson",
        "content": (l.get("reason")
                    or "；".join(str(x) for x in (l.get("issues") or []))
                    or l.get("prompt") or ""),
        "context": {"project": l.get("project"), "kind": l.get("kind"),
                    "category": l.get("category"), "priority": l.get("priority")},
        "confidence": conf,
        "tags": tags,
        "source": "prompt_memory",
        "created_at": l.get("ts") or "",
        "usage_count": int(l.get("use_count") or 0),
        "last_used": l.get("last_used") or None,
    }


class AIMemoryEntry:
    """单条记忆（prompt_memory 教训的**视图**；字段名沿用历史以兼容调用方）。"""

    def __init__(self, mem_id: str, mem_type: str, content: str,
                 context: Dict[str, Any], confidence: float = 1.0,
                 tags: Optional[List[str]] = None, source: Optional[str] = None,
                 created_at: Optional[str] = None):
        self.mem_id = mem_id
        self.mem_type = mem_type
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
            "mem_id": self.mem_id, "mem_type": self.mem_type, "content": self.content,
            "context": self.context, "confidence": self.confidence, "tags": self.tags,
            "source": self.source, "created_at": self.created_at,
            "usage_count": self.usage_count, "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AIMemoryEntry":
        d = data or {}
        mem = cls(
            mem_id=d.get("mem_id", ""), mem_type=d.get("mem_type", "lesson"),
            content=d.get("content", ""), context=d.get("context", {}) or {},
            confidence=d.get("confidence", 1.0), tags=d.get("tags", []) or [],
            source=d.get("source"), created_at=d.get("created_at"))
        mem.usage_count = d.get("usage_count", 0)
        mem.last_used = d.get("last_used")
        return mem


class AIEvolutionEngine:
    """自进化引擎（**收敛后**：基于 prompt_memory 教训库的统计给出趋势/洞察）。

    ⚠️ 它不再维护自己的数据；`analyze_trends` / `generate_insights` 都是对唯一来源
    `prompt_memory` 的**只读推导**。`auto_optimize_prompt` 直接委托教训召回。
    """

    def __init__(self, memory_system: "AIMemorySystem"):
        self.memory = memory_system

    def analyze_trends(self) -> Dict[str, Any]:
        st = self.memory._pm().stats()
        by_kind = st.get("by_kind") or {}
        total = int(st.get("total") or 0)
        dead = int(st.get("dead_lessons") or 0)
        return {
            "total_memories": total,
            "type_distribution": {"lesson": total},
            "category_issues": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])[:10]),
            "success_rate": 0,
            "high_confidence_ratio": 0,
            "avg_confidence": 1.0,
            "trend": "improving" if (total and dead < total) else "stable",
        }

    def generate_insights(self, limit: int = 5) -> List[Dict[str, Any]]:
        st = self.memory._pm().stats()
        insights: List[Dict[str, Any]] = []
        for kind, n in sorted((st.get("by_kind") or {}).items(), key=lambda kv: -kv[1])[:5]:
            insights.append({
                "type": "kind_pattern", "category": kind, "count": int(n),
                "priority": "high" if n >= 20 else "medium" if n >= 5 else "low",
                "suggestion": f"「{kind}」环节已沉淀 {n} 条质检教训，建议生成前召回以降低同类缺陷复发。",
            })
        dead = int(st.get("dead_lessons") or 0)
        if dead:
            insights.append({
                "type": "dead_lessons", "count": dead, "priority": "medium",
                "suggestion": f"{dead} 条教训从未被召回（死教训），建议核对召回键（prompt 指纹）是否命中。",
            })
        return insights[:limit]

    def auto_optimize_prompt(self, base_prompt: str, issues: List[str]) -> str:
        try:
            lp = prompt_memory.learned_prompt(
                kind="storyboard", prompt=base_prompt, root_dir=self.memory.root_dir)
            return lp or base_prompt
        except Exception as e:  # noqa: BLE001
            logger.warning("提示词自动优化失败（返回原提示词）：%s", e)
            return base_prompt


class AIMemorySystem:
    """AI 记忆系统（**prompt_memory 只读门面**）。"""

    def __init__(self, root_dir: str = None):
        self.root_dir = root_dir or PROJECT_OUTPUT_DIR
        self.lock = threading.Lock()
        self.evolution = AIEvolutionEngine(self)

    # ---------- 内部 ----------
    def _pm(self) -> "prompt_memory.PromptMemory":
        return prompt_memory.get_memory(self.root_dir or PROJECT_OUTPUT_DIR)

    def _lessons(self) -> list:
        try:
            return self._pm().list(limit=100000) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("读取教训库失败（按空处理）：%s", e)
            return []

    # ---------- 只读 ----------
    def list_all(self, mem_type: Optional[str] = None, limit: int = 100) -> List[AIMemoryEntry]:
        entries = [AIMemoryEntry.from_dict(_map_lesson(l)) for l in self._lessons()]
        if mem_type:
            entries = [e for e in entries if e.mem_type == mem_type]
        return entries[:limit]

    def search_by_pattern(self, query: str, mem_type: Optional[str] = None,
                          limit: int = 10) -> List[AIMemoryEntry]:
        qn = str(query or "").lower()
        if not qn:
            return []
        out: List[AIMemoryEntry] = []
        for l in self._lessons():
            hay = " ".join(str(x) for x in (
                [l.get("reason"), l.get("prompt"),
                 " ".join(str(t) for t in (l.get("issues") or [])),
                 " ".join(str(t) for t in (l.get("terms") or []))]))
            if qn in hay.lower():
                e = AIMemoryEntry.from_dict(_map_lesson(l))
                if not mem_type or e.mem_type == mem_type:
                    out.append(e)
                if len(out) >= limit:
                    break
        return out

    def get_stats(self) -> Dict[str, Any]:
        st = self._pm().stats()
        total = int(st.get("total") or 0)
        return {"total": total, "by_type": {"lesson": total},
                "recent_7d": 0, "recent_30d": 0}

    # ---------- 写入（收敛：写进 prompt_memory，不再自建第二份存储）----------
    def add_memory(self, mem_type: str, content: str, context: Dict[str, Any],
                   confidence: float = 1.0, tags: Optional[List[str]] = None,
                   source: Optional[str] = None) -> AIMemoryEntry:
        ctx = context or {}
        try:
            l = self._pm().record(project=str(ctx.get("project") or ""),
                                  kind=str(mem_type or "manual"), prompt=str(content or ""),
                                  issues=[str(content or "")], reason=str(content or ""))
            if l:
                return AIMemoryEntry.from_dict(_map_lesson(l))
        except Exception as e:  # noqa: BLE001
            logger.warning("登记记忆失败（忽略）：%s", e)
        return AIMemoryEntry("", mem_type or "insight", content, ctx, confidence, tags, source)

    def record_lesson(self, project: str, episode: int, prompt: str,
                      issues: List[str], category: str) -> AIMemoryEntry:
        issues = [str(x) for x in (issues or []) if str(x).strip()]
        try:
            l = self._pm().record(project=project, kind="lesson", prompt=prompt,
                                  issues=issues, reason="；".join(issues))
            if l:
                return AIMemoryEntry.from_dict(_map_lesson(l))
        except Exception as e:  # noqa: BLE001
            logger.warning("记录教训失败（忽略）：%s", e)
        return AIMemoryEntry("", "lesson", "；".join(issues), {"project": project}, 1.0,
                             [category] if category else [])

    def record_success(self, project: str, episode: int, prompt: str,
                       highlights: List[str], category: str) -> AIMemoryEntry:
        highlights = [str(x) for x in (highlights or []) if str(x).strip()]
        try:
            l = self._pm().record(project=project, kind="success", prompt=prompt,
                                  issues=highlights or ["成功产出"],
                                  reason="；".join(highlights) or "成功产出")
            if l:
                return AIMemoryEntry.from_dict(_map_lesson(l))
        except Exception as e:  # noqa: BLE001
            logger.warning("记录成功经验失败（忽略）：%s", e)
        return AIMemoryEntry("", "success", "；".join(highlights), {"project": project}, 1.0,
                             [category] if category else [])

    def clear_old(self, days: int = 90) -> int:
        try:
            return int(self._pm().decay_old_lessons(days_threshold=int(days)) or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("衰减过期记忆失败（忽略）：%s", e)
            return 0

    def purge_project(self, projects: List[str]) -> int:
        """项目被删除时，摘除教训库中 ``project`` 命中该项目别名的条目（关联清理）。

        收敛后 prompt_memory 是唯一来源 —— 直接按 project 删除对应教训（含破坏性，
        但项目已删，教训失去归属）。任何失败只 warning，绝不阻断删除主流程。
        """
        vals = {str(p).strip() for p in (projects or []) if p and str(p).strip()}
        if not vals:
            return 0
        removed = 0
        try:
            m = self._pm()
            for l in list(getattr(m, "_lessons", []) or []):
                if str(l.get("project") or "").strip() in vals:
                    try:
                        if m.delete(l.get("lesson_id")):
                            removed += 1
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as e:  # noqa: BLE001
            logger.warning("摘除项目教训失败（忽略）：%s", e)
        if removed:
            logger.info("教训库已随项目删除摘除 %d 条（project ∈ %s）", removed, sorted(vals))
        return removed


# 全局单例
_instance: Optional[AIMemorySystem] = None
_instance_lock = threading.Lock()


def get_memory_system(root_dir: str = None) -> AIMemorySystem:
    """获取全局记忆系统实例（prompt_memory 门面）。"""
    global _instance
    with _instance_lock:
        if _instance is None or _instance.root_dir != (root_dir or _instance.root_dir):
            _instance = AIMemorySystem(root_dir)
        return _instance


# t56：历史拼写别名（保留以免历史/插件按旧名 import 崩溃）。
AISemoryEntry = AIMemoryEntry
AISemorySystem = AIMemorySystem


if __name__ == "__main__":
    mem = AIMemorySystem()
    print("Stats:", mem.get_stats())
    print("Trends:", mem.evolution.analyze_trends())
    print("Insights:", mem.evolution.generate_insights())
