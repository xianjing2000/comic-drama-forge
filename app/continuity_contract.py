# -*- coding: utf-8 -*-
"""P0-2 全局一致性契约 + 跨镜头校验器（借鉴 penshot GlobalConsistencyContract /
CrossChunkValidator，2026-09-28 落地）。

## 为什么做
系统有 ``characters_in_shot`` 但无「跨镜头契约」，人物/场景「不连戏」是系统性缺口：
某角色中途凭空消失、某角色毫无铺垫突然出镜、场景跳切无承接。这三类问题靠
单镜 LLM 自查发现不了，需要**全局视角**扫一遍整集。

## 设计口径（低风险、纯只读软告警）
- **纯确定性、无 LLM、无 I/O**：输入就是 ``script["shots"]``，输出一组 issue。
  可离线跑、可复现、不依赖 ComfyUI/网关。
- **三类检测**（全部 ``severity="low"``）：
  1. ``character_disappears``：某角色在集中段出镜后到集尾再无出场（且非首尾相邻），
     提示「是否漏了该角色收尾/转场」。
  2. ``appears_suddenly``：某角色在集中段**首次**出镜、且距离集首超过阈值镜头，
     提示「出场缺乏铺垫」。
  3. ``scene_jump``：相邻两镜 ``location`` 既不同、也无「场景切换」类 edit_reason
     标注，提示「场景跳切缺承接」。
- **不进 LLM 重写**：gap/jump 不是改单镜头能修的（要补镜头/改分镜），若设
  ``rewrite_needed`` 会被 :func:`continuity.rewrite_shots_for_issues` 拉去做局部重写、
  反而可能改坏。故 :func:`merge_contract_issues` 只把 issue 追加进 ``validation`` 桶、
  更新 ``issue_stats["low"]``，**不动** ``rewrite_needed`` / ``rewrite_shot_ids``。

## 阈值（模块级常量，探针可覆盖）
- ``CHAR_SURPRISE_APPEAR_GAP``：首出镜序号 ≥ 此值判「突兀出场」。
- ``CHAR_DISAPPEAR_GAP``：末出镜到集尾镜头数 ≥ 此值判「中途消失」。
- ``MAX_CONTRACT_ISSUES``：单集最多报多少条 contract issue。**2026-10-10 起由 12 放宽到 500**
  （用户要求不设上限；500 只是防失控天花板，正常一集不会触到）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 阈值（集中可调；探针可直接改写这些值做边界测试）
# --------------------------------------------------------------------------- #
#: 首次出镜序号（0-based）≥ 此值判「突兀出场」（前 3 镜内的首次出镜视为正常开场）。
CHAR_SURPRISE_APPEAR_GAP: int = 3
#: 末次出镜到集尾剩余镜头数 ≥ 此值判「中途消失」（角色应自然收尾而非断在中间）。
CHAR_DISAPPEAR_GAP: int = 4
#: 单集最多报告多少条 contract 类 issue（防止角色多时刷爆校验桶）。
# ⚠️ 2026-10-10（用户明确要求「不要设置上限」）：
#    原值 12，实测已出现「13 条 > 上限 12，截断保留前 12 条（第2集）」——
#    被截掉的很可能正是最该修的那条连续性问题（用户最在意的就是
#    「人物不能突然出现在别的地方」）。这里放宽到 500：
#    实践中等于「不设上限」，同时保留一个防失控的天花板 ——
#    这些 issue 会被拼进局部重写的提示词，无限增长会把 prompt 撑爆。
MAX_CONTRACT_ISSUES: int = 500
#: edit_reason 里含这些子词视为「已声明场景切换/转场」，scene_jump 不报。
_SCENE_SWITCH_MARKERS = ("转场", "切换", "切至", "切到", "切换场景", "转场至", "cross-cut", "cut to")

# ---- P2-4 关键道具跨镜状态追踪（props with state，软告警不阻断）----
#: 关键道具在相邻两次出镜之间「消失」≥ 此镜数后再次出现，判「道具不连戏、缺交接」。
PROP_REAPPEAR_GAP: int = 3
#: importance 含这些子词视为「关键道具」（在 bible 声明重要道具名单时，只跟踪名单内，避免一般道具刷噪）。
_PROP_IMPORTANT_MARKERS = ("high", "高", "主要", "核心", "重要", "key")

#: 校验器输出的 category 常量（供下游/前端按类聚合）。
CATEGORY_CHARACTER_GAP = "角色连贯"
CATEGORY_APPEAR_SODDEN = "角色连贯"
CATEGORY_SCENE_JUMP = "场景连贯"
CATEGORY_PROP_GAP = "道具连贯"


def _shot_items(s: Dict[str, Any]) -> List[str]:
    """取镜头出场物品（``items_in_shot``），去重保序、去空。"""
    seen: List[str] = []
    for c in (s.get("items_in_shot") or []):
        name = str(c or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _important_prop_names(bible: Optional[Dict[str, Any]]) -> List[str]:
    """从 bible 取出「关键道具」名单（importance 命中 _PROP_IMPORTANT_MARKERS）。

    未声明重要道具（名单为空）时返回 [] → 调用方退化为「跟踪所有出现 ≥2 镜的道具」。
    """
    out: List[str] = []
    for it in ((bible or {}).get("items") or []):
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name") or "").strip()
        imp = str(it.get("importance") or it.get("level") or "").strip().lower()
        if nm and any(m in imp for m in _PROP_IMPORTANT_MARKERS):
            out.append(nm)
    return out


def _shot_location(s: Dict[str, Any]) -> str:
    return str(s.get("location") or "").strip()


def _shot_chars(s: Dict[str, Any]) -> List[str]:
    """取镜头出场角色（``characters_in_shot``），去重保序、去空。"""
    seen: List[str] = []
    for c in (s.get("characters_in_shot") or []):
        name = str(c or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _shot_edit_reason(s: Dict[str, Any]) -> str:
    return str(s.get("edit_reason") or "").strip()


class GlobalConsistencyContract:
    """整集「出场/场景占用」契约表：从 ``shots`` 构建，供校验器查询。

    记录每个角色的**首次/末次出镜序号**与出镜序号列表，以及每个镜头的 ``location``。
    本身不做判定，只把「全局事实」摊平，判定交给 :class:`CrossChunkValidator`。
    """

    def __init__(self, shots: List[Dict[str, Any]]):
        shots = [s for s in (shots or []) if isinstance(s, dict)]
        self._raw: List[Dict[str, Any]] = shots
        self.shot_count = len(shots)
        self.locations: List[str] = [_shot_location(s) for s in shots]
        # 角色 → 出镜序号列表（升序，首次构建即有序）
        self.char_shots: Dict[str, List[int]] = {}
        # 道具 → 出镜序号列表（P2-4：关键道具跨镜状态追踪，口径同角色占用）
        self.prop_shots: Dict[str, List[int]] = {}
        for idx, s in enumerate(shots):
            for name in _shot_chars(s):
                self.char_shots.setdefault(name, []).append(idx)
            for name in _shot_items(s):
                self.prop_shots.setdefault(name, []).append(idx)
        # 去重后每角色/每道具出镜序号（防御重复追加）
        for name, seq in self.char_shots.items():
            self.char_shots[name] = sorted({i for i in seq})
        for name, seq in self.prop_shots.items():
            self.prop_shots[name] = sorted({i for i in seq})

    @property
    def characters(self) -> List[str]:
        return sorted(self.char_shots.keys())

    @property
    def props(self) -> List[str]:
        """出过镜的道具名（升序）。P2-4 状态追踪的占用来源。"""
        return sorted(self.prop_shots.keys())

    @property
    def all_shots(self) -> List[Dict[str, Any]]:
        """原始镜头（校验器取 shot_id / 证据用）。"""
        return list(self._raw)


def _make_issue(category: str, detail: str, evidence: str, fix: str,
                episode_no: int, shot_ids: List[Any]) -> Dict[str, Any]:
    """构造与 :func:`continuity.rule_dialogue_dedup` 同形状的 issue（7 字段）。

    ⚠️ ``severity`` 恒为 ``"low"``——contract 类问题只软告警、不触发 LLM 重写。
    """
    return {
        "severity": "low",
        "category": category,
        "episode_no": int(episode_no),
        "detail": detail,
        "evidence": evidence,
        "fix": fix,
        "shot_ids": list(shot_ids),
    }


class CrossChunkValidator:
    """跨镜头一致性校验器：基于 :class:`GlobalConsistencyContract` 跑三类软告警。

    全部确定性、可复现；返回 issue 列表（≤ ``MAX_CONTRACT_ISSUES`` 条）。
    """

    def __init__(self,
                 surprise_appear_gap: int = CHAR_SURPRISE_APPEAR_GAP,
                 disappear_gap: int = CHAR_DISAPPEAR_GAP,
                 max_issues: int = MAX_CONTRACT_ISSUES,
                 prop_reappear_gap: int = PROP_REAPPEAR_GAP):
        self.surprise_appear_gap = int(surprise_appear_gap)
        self.disappear_gap = int(disappear_gap)
        self.max_issues = int(max_issues)
        self.prop_reappear_gap = int(prop_reappear_gap)

    def validate(self, contract: GlobalConsistencyContract,
                 episode_no: int = 1,
                 prop_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """跨镜头一致性校验（角色 3 类 + 场景跳切 + P2-4 道具 1 类，全软告警）。

        prop_filter（P2-4）：非空时**只**跟踪名单内的关键道具（由调用方从 bible 解析）；
        为 None / 空时退化为「跟踪所有出现 ≥2 镜的道具」。道具出镜间隔 ≥ prop_reappear_gap
        视为「中途消失、缺交接」（severity=low，不阻断、不触发 LLM 重写）。
        """
        issues: List[Dict[str, Any]] = []
        for name in contract.characters:
            seq = contract.char_shots.get(name) or []
            if not seq:
                continue
            first, last = seq[0], seq[-1]
            tail_gap = (contract.shot_count - 1) - last
            # 1) 突兀出场：首次出镜晚（不在集中段，且出镜后仍有后续镜才谈得上"中途"）
            if first >= self.surprise_appear_gap and len(seq) >= 1:
                sid = contract.all_shots[first].get("shot_id")
                issues.append(_make_issue(
                    CATEGORY_APPEAR_SODDEN,
                    f"角色「{name}」在第 {first + 1} 镜才首次出镜（出场缺乏铺垫）",
                    f"首现镜 #{sid}；该角色共出镜 {len(seq)} 镜",
                    "若该角色为本集重要角色，建议在开场镜补充其出场/亮相；否则无需处理",
                    episode_no, [sid],
                ))
            # 2) 中途消失：末次出镜过早、之后还有若干镜没该角色
            if tail_gap >= self.disappear_gap:
                sid = contract.all_shots[last].get("shot_id")
                issues.append(_make_issue(
                    CATEGORY_CHARACTER_GAP,
                    f"角色「{name}」末次出镜在第 {last + 1} 镜，其后还有 {tail_gap} 镜未再出现",
                    f"末现镜 #{sid}；该角色共出镜 {len(seq)} 镜",
                    "若该角色应当贯穿本集，建议补一个收尾/转场镜头；若剧情上确已离场则无需处理",
                    episode_no, [sid],
                ))
        # 3) 场景跳切（相邻两镜 location 不同且未声明转场）
        prev_loc = ""
        for idx in range(1, contract.shot_count):
            loc = contract.locations[idx]
            if prev_loc and loc and prev_loc != loc:
                if not any(m in _shot_edit_reason(contract.all_shots[idx])
                           for m in _SCENE_SWITCH_MARKERS):
                    sid = contract.all_shots[idx].get("shot_id")
                    issues.append(_make_issue(
                        CATEGORY_SCENE_JUMP,
                        f"第 {idx + 1} 镜场景由「{prev_loc}」跳至「{loc}」，且未标注转场",
                        f"镜 #{sid}（前镜「{prev_loc}」→ 本镜「{loc}」）",
                        "若为有意跳切，建议在该镜 edit_reason 补「转场/切换场景」；否则确认两场景是否笔误",
                        episode_no, [sid],
                    ))
            prev_loc = loc
        # 4) P2-4 关键道具跨镜状态：出镜序列中间有「缺口」（相邻两次出镜间隔 ≥ 阈值）
        #    → 道具中途消失、缺乏交接镜头（软告警，severity=low，不阻断不重写）。
        tracked_props = (prop_filter if prop_filter else contract.props)
        for pname in tracked_props:
            seq = contract.prop_shots.get(pname) or []
            if len(seq) < 2:
                continue  # 只出过 0/1 镜谈不上「中途消失再出现」
            for a, b in zip(seq, seq[1:]):
                gap = b - a - 1          # 两次出镜之间的镜头数（= a 之后、b 之前有多少镜没它）
                if gap >= self.prop_reappear_gap:
                    sid_b = contract.all_shots[b].get("shot_id")
                    issues.append(_make_issue(
                        CATEGORY_PROP_GAP,
                        f"道具「{pname}」在第 {a + 1} 镜出现后，中间隔了 {gap} 镜才在第 {b + 1} 镜再出现（缺交接）",
                        f"再现镜 #{sid_b}（前次第 {a + 1} 镜）；该道具共出镜 {len(seq)} 镜",
                        "若该道具为关键道具，建议在缺口处补一个交代其下落/仍持有的镜头；"
                        "若剧情上确已离场/转移则无需处理",
                        episode_no, [sid_b],
                    ))
        if len(issues) > self.max_issues:
            logger.warning(
                "P0-2 contract 告警 %d 条 > 上限 %d，截断保留前 %d 条（第%d集）",
                len(issues), self.max_issues, self.max_issues, episode_no)
            issues = issues[: self.max_issues]
        return issues


def build_contract(script: Dict[str, Any]) -> GlobalConsistencyContract:
    """便捷入口：从 ``script`` 取 shots 建契约表。"""
    return GlobalConsistencyContract((script or {}).get("shots") or [])


def merge_contract_issues(validation: Dict[str, Any], script: Dict[str, Any],
                          bible: Optional[Dict[str, Any]] = None,
                          episode_no: int = 1) -> Dict[str, Any]:
    """把 P0-2 contract 软告警并入 ``validation`` 桶——**只追加 low issue + 更新
    ``issue_stats["low"]``，不动 ``rewrite_needed`` / ``rewrite_shot_ids``**。

    与 :func:`continuity.merge_dedup_issues` 对称（那个会把命中计入 rewrite_needed，
    因为它改单句就能修）；contract 类问题改单镜修不好，故刻意**不**置 rewrite_needed，
    纯软告警供人工/下游审查。
    """
    # P2-4：从 bible 取「关键道具」名单作为 prop_filter；名单为空时退化为跟踪所有出现
    # ≥2 镜的道具（validate 内 prop_filter=None 的默认分支）。
    prop_filter = _important_prop_names(bible)
    issues = CrossChunkValidator().validate(
        build_contract(script), int(episode_no), prop_filter=prop_filter)
    if not issues:
        return validation
    validation.setdefault("issues", [])
    validation["issues"] = list(validation.get("issues") or []) + issues
    stats = validation.get("issue_stats") or {"high": 0, "medium": 0, "low": 0}
    for it in issues:
        sev = it.get("severity") or "low"
        stats[sev] = int(stats.get(sev) or 0) + 1
    validation["issue_stats"] = stats
    # 刻意不动 rewrite_needed / rewrite_shot_ids（contract 不触发 LLM 局部重写）。
    return validation
