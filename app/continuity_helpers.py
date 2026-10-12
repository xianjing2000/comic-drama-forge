# -*- coding: utf-8 -*-
"""跨集台词重复检测的**纯函数层**（无 IO / 无 LLM / 确定性可复现）。

背景（用户 2026-10-12 成片质检报告）：「台词跨集重复」——同一句台词在第 N 集念完，
第 N+1 集又原样念一遍。旧实现只有 continuity.rule_dialogue_dedup，它**只看本集内部**
（见其 docstring「同一集内同一角色台词近似重复检测」），跨集重复一直没有拦截点。

为什么单独拆一个叶子模块（而不是直接写在 continuity.py 里）：
  · continuity.py 有 2400+ 行，依赖 LLM / 网络 / 文件 IO，纯函数塞进去只能靠 mock 才测；
  · 判定口径（阈值 / 相似度 / 最短长度）是本任务唯一需要反复标定、且被多个入口复用的
    部分：生成链路（continuity.merge_cross_episode_dedup_issues）与可调用接口
    （continuity.check_dialogue_duplicate）必须逐字同口径，否则两处各判各的；
  · 本模块只依赖标准库 + dialogue_utils（同为叶子模块），可被任意链路安全导入。

判定口径（2026-10-12 用**真实剧本**标定，样本与出处见 tests/test_continuity_dedup.py）：
  · plain_text()：去掉标点 / 空白，只留中英数字（与 continuity._plain 同口径）；
  · similar_text()：字符 2-gram Jaccard（与 continuity._similar_text 同口径）；
  · **精确重复**：去标点后逐字相同，且两侧长度都 >= THRESHOLDS["exact_min_chars"]；
  · **近似重复**：两侧长度都 >= THRESHOLDS["near_min_chars"]、长度比 >= ["near_len_ratio"]、
    相似度 >= ["near_sim"]（默认 8 / 0.7 / 0.72，标定证据见下）。

阈值为什么这么定（错误的两个方向都实测过）：
  · 短句（< 8 字）只认精确：真实剧本里「什么意思？」「你退下罢。」这类短句跨集逐字复用是
    真缺陷（可判），但短句的 2-gram 集合太小，无关台词极易被顶到 0.5+（真实反例：
    「这个废物……怎么会？」vs「这个废物……」sim=0.50；「你……你到底是谁？」vs
    「你到底是什么人？」sim=0.375）→ 短句做近似判定必然误报，故不判；
  · 近似阈值 0.72 + 长度比 0.7：真实近似重复样本「守则不是筛选工具，是陷阱的说明书。」
    vs「守则不是筛选工具。守则是陷阱的说明书。」sim=0.812 能命中，而上面两组无关台词
    （0.50 / 0.375）稳在阈值以下 —— 宁可漏一次近似，也不能把无关台词判成重复。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from dialogue_utils import normalize_lines as _normalize_lines

# 判重规则标记：写进 issue["rule"]，用于「重写后复检」时**精确识别并撤回**上一轮由本规则
# 追加的 issue（否则 validation 的 issues / issue_stats 会一轮轮越加越多）。
RULE = "cross_episode_dialogue_dedup"

# 台账行来源：金句行 = 原文金句（历史既有），台词行 = 本任务新增的「成片台词台账」。
SOURCE_NOVEL = "novel_chapter"
SOURCE_SCRIPT = "script_dialogue"

# ---- 判定阈值（2026-10-12 真实剧本标定，理由见模块 docstring）----
#
# ⚠️ 阈值必须**成套**看（单独调一个没有任何意义），因此集中成一张表而不是散成多个模块常量；
#    也正因如此它们不该被当成"可配置常量"去 config_center 登记（不是用户可调项，而是算法标定值）。
#    调用点统一写 THRESHOLDS["near_sim"] 这种形式：改阈值只有一个地方，不会"改一处漏一处"。
THRESHOLDS = {
    "exact_min_chars": 4,    # 精确重复的最短长度（「什么意思？」5 字、「你退下罢」4 字，都是真缺陷）
    "near_min_chars": 8,     # 近似重复两侧都要够长（短句做近似判定必然误报）
    "near_len_ratio": 0.7,   # 长度比下限（差了 30% 以上就不是同一句的改写）
    "near_sim": 0.72,        # 2-gram Jaccard 下限
    "ledger_min_chars": 4,   # 入台账 / 参与判重的最短长度（短于此不写台账）
}

_PLAIN_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def plain_text(text) -> str:
    """去标点 / 空白，只留中英数字（与 continuity._plain 同口径，勿单独改动）。"""
    return _PLAIN_RE.sub("", str(text or ""))


def similar_text(a, b) -> float:
    """字符 2-gram Jaccard 相似度（与 continuity._similar_text 同口径）。

    逐字相同返回 1.0；任一为空返回 0.0。单字符串退化为一元组（与旧实现一致）。
    """
    a, b = plain_text(a), plain_text(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / max(1, len(ga | gb))


def judge_duplicate(candidate, prior, *, near_sim: float = None,
                    min_exact: int = None, min_near: int = None,
                    len_ratio: float = None) -> dict:
    """判定「候选台词」与「某一条历史台词」是否重复。

    返回 {"duplicate": bool, "kind": "exact"|"near"|"", "score": float, "reason": str}。
    判据与阈值可在调用处覆盖（只为单测标定用；生产一律用 THRESHOLDS 默认值）。
    """
    near_sim = THRESHOLDS["near_sim"] if near_sim is None else near_sim
    min_exact = THRESHOLDS["exact_min_chars"] if min_exact is None else min_exact
    min_near = THRESHOLDS["near_min_chars"] if min_near is None else min_near
    len_ratio = THRESHOLDS["near_len_ratio"] if len_ratio is None else len_ratio
    pc, pp = plain_text(candidate), plain_text(prior)
    score = similar_text(candidate, prior)
    if not pc or not pp:
        return {"duplicate": False, "kind": "", "score": 0.0, "reason": "空台词不参与判重"}
    if len(pc) < min_exact or len(pp) < min_exact:
        return {"duplicate": False, "kind": "", "score": score,
                "reason": f"过短（<{min_exact} 字）不参与判重"}
    if pc == pp:
        return {"duplicate": True, "kind": "exact", "score": 1.0,
                "reason": "去标点后逐字相同"}
    short, long_ = min(len(pc), len(pp)), max(len(pc), len(pp))
    if short < min_near:
        return {"duplicate": False, "kind": "", "score": score,
                "reason": f"短句（<{min_near} 字）只认精确重复，不做近似判定"}
    ratio = short / long_
    if ratio < len_ratio:
        return {"duplicate": False, "kind": "", "score": score,
                "reason": f"长度比 {ratio:.2f} < {len_ratio}（篇幅差太多，不是同一句）"}
    if score >= near_sim:
        return {"duplicate": True, "kind": "near", "score": score,
                "reason": f"相似度 {score:.3f} ≥ {near_sim}"}
    return {"duplicate": False, "kind": "", "score": score,
            "reason": f"相似度 {score:.3f} < {near_sim}"}


def _row_episode_no(row) -> Optional[int]:
    if not isinstance(row, dict):
        return None
    raw = str(row.get("episode_no") if row.get("episode_no") is not None else "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _history_row(row) -> dict:
    """把台账行 / 裸字符串统一成 {"text","episode_no","speaker_hint","shot_id","source"}。"""
    if isinstance(row, dict):
        return {"text": str(row.get("text") or "").strip(),
                "episode_no": _row_episode_no(row),
                "speaker_hint": str(row.get("speaker_hint") or "").strip(),
                "shot_id": row.get("shot_id"),
                "source": str(row.get("source") or "").strip()}
    return {"text": str(row or "").strip(), "episode_no": None,
            "speaker_hint": "", "shot_id": None, "source": ""}


def _better_match(cand: dict, best: dict) -> bool:
    """挑「最该被报出来的那一条」来源：精确优先 → 相似度高优先 → 集号小优先 → 镜号小优先。

    集号小优先：同一句话在早期集出现过、后面集又复用，最该指向的是**最早那次**，
    这样人（和重写提示词）能直接看到「这句是第 N 集就说过的」。
    """
    rank = {"exact": 2, "near": 1}
    if rank.get(cand["kind"], 0) != rank.get(best["kind"], 0):
        return rank.get(cand["kind"], 0) > rank.get(best["kind"], 0)
    if float(cand.get("score") or 0) != float(best.get("score") or 0):
        return float(cand.get("score") or 0) > float(best.get("score") or 0)
    ce, be = cand.get("episode_no"), best.get("episode_no")
    if (ce is None) != (be is None):
        return ce is not None
    if ce is not None and be is not None and ce != be:
        return ce < be
    cs, bs = cand.get("shot_id"), best.get("shot_id")
    if isinstance(cs, int) and isinstance(bs, int) and cs != bs:
        return cs < bs
    return False


def find_duplicate(candidate, history: Iterable, *, exclude_episode_no=None,
                   near_sim: float = None, min_exact: int = None,
                   min_near: int = None, len_ratio: float = None) -> dict:
    """在历史台词台账里找候选台词的最后一条重复来源（跨集去重的**可调用接口**）。

    exclude_episode_no：本集集号（跳过本集自己的台账行，避免「自己和自己重复」）。
    返回 {"text","plain","duplicate","kind","score","match","reason","compared"}；
    match 命中时为 {"episode_no","text","speaker_hint","shot_id","source"}（哪一集哪一句）。
    """
    text = str(candidate or "").strip()
    best, compared = None, 0
    for row in history or []:
        h = _history_row(row)
        if not h["text"]:
            continue
        if exclude_episode_no is not None and h["episode_no"] is not None \
                and h["episode_no"] == int(exclude_episode_no):
            continue
        compared += 1
        v = judge_duplicate(text, h["text"], near_sim=near_sim, min_exact=min_exact,
                            min_near=min_near, len_ratio=len_ratio)
        if not v["duplicate"]:
            continue
        cand = {"kind": v["kind"], "score": v["score"], **h}
        if best is None or _better_match(cand, best):
            best = cand
    if best is None:
        return {"text": text, "plain": plain_text(text), "duplicate": False, "kind": "",
                "score": 0.0, "match": None, "compared": compared,
                "reason": f"与 {compared} 条历史台词比对：无可判定的重复"}
    return {"text": text, "plain": plain_text(text), "duplicate": True, "kind": best["kind"],
            "score": best["score"], "match": best, "compared": compared,
            "reason": ("去标点后逐字相同" if best["kind"] == "exact"
                       else f"近似重复（相似度 {best['score']:.3f}）")}


# ===================== 剧本 → 台词行 =====================

def dialogue_rows_of_script(script: dict) -> List[dict]:
    """把剧本里**会进成片**的台词抽成行（顺序稳定，含 shot_id / speaker / text）。

    走 dialogue_utils.normalize_lines：配音（tts_client）、字幕（video_postprocess）、
    质检（script_prompt_analyzer）读的就是它 —— 台账必须与「实际被念出来的那些行」同源，
    否则会出现「判重用的是另一份解析结果」的口径漂移。
    """
    script = script if isinstance(script, dict) else {}
    chars = [c.get("name") for c in (script.get("characters") or []) if isinstance(c, dict)]
    rows: List[dict] = []
    seen = set()
    for idx, shot in enumerate(script.get("shots") or []):
        if not isinstance(shot, dict):
            continue
        sid = shot.get("shot_id")
        if not isinstance(sid, int):
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                sid = None
        raw = shot.get("dialogue")
        if not raw:
            raw = shot.get("dialogue_text")
        cast = shot.get("characters_in_shot")
        for line in _normalize_lines(raw, chars, cast):
            text = str(line.get("text") or "").strip()
            key = (sid, plain_text(text))
            if not plain_text(text) or key in seen:
                continue
            seen.add(key)
            rows.append({"shot_id": sid, "index": idx, "speaker": str(line.get("speaker") or "").strip(),
                         "text": text, "scene_no": shot.get("scene_no"),
                         "episode": shot.get("episode")})
    rows.sort(key=lambda r: (r["shot_id"] if isinstance(r["shot_id"], int) else 10 ** 6, r["index"]))
    return rows


def find_script_duplicates(script: dict, history: Iterable, *, exclude_episode_no=None,
                           **kw) -> List[dict]:
    """对整集剧本逐句判重（跨集），返回命中列表（含本镜镜号与来源集/句）。

    同一条台词在多个镜头里各出现一次时，**每个镜头都报一次**（重写是按镜改的）。
    """
    hist = list(history or [])
    out = []
    for row in dialogue_rows_of_script(script):
        res = find_duplicate(row["text"], hist, exclude_episode_no=exclude_episode_no, **kw)
        if res["duplicate"]:
            out.append({"shot_id": row["shot_id"], "speaker": row["speaker"], "text": row["text"],
                        "kind": res["kind"], "score": res["score"], "match": res["match"],
                        "reason": res["reason"]})
    out.sort(key=lambda d: (0 if d["kind"] == "exact" else 1, -float(d.get("score") or 0),
                            d["shot_id"] if isinstance(d["shot_id"], int) else 10 ** 6))
    return out


def summarize_duplicates(dups: Iterable) -> dict:
    """命中汇总（写进 script.metadata / validation，供前端与排查用）。"""
    dups = list(dups or [])
    return {
        "count": len(dups),
        "exact": len([d for d in dups if d.get("kind") == "exact"]),
        "near": len([d for d in dups if d.get("kind") == "near"]),
        "shot_ids": sorted({d["shot_id"] for d in dups if isinstance(d.get("shot_id"), int)}),
        "sources": sorted({(d.get("match") or {}).get("episode_no")
                           for d in dups if (d.get("match") or {}).get("episode_no") is not None}),
    }


def duplicate_issue(dup: dict, episode_no: int) -> dict:
    """把一条命中转成校验 issue（字段与 continuity.rule_dialogue_dedup 同形，
    重写链路 rewrite_shots_for_issues 直接消费 severity/category/detail/evidence/fix）。
    """
    m = dup.get("match") or {}
    sid = dup.get("shot_id")
    mep = m.get("episode_no")
    mtext = str(m.get("text") or "")
    mshot = m.get("shot_id")
    msrc = f"第{mep}集" if mep is not None else "历史集"
    if mshot is not None:
        msrc += f"#{mshot}"
    text = str(dup.get("text") or "")
    return {
        "severity": "high" if dup.get("kind") == "exact" else "medium",
        "category": "台词一致性",
        "rule": RULE,
        "episode_no": int(episode_no),
        "detail": (f"第{episode_no}集台词与{msrc}重复（"
                   + ("逐字相同" if dup.get("kind") == "exact" else f"近似，相似度 {dup.get('score'):.2f}")
                   + f"）：#{sid}「{text[:40]}」"),
        "evidence": f"本集#{sid}「{text[:60]}」 ↔ {msrc}「{mtext[:60]}」",
        "fix": "改写本镜台词：保留信息但换措辞，不要与该集已有表达逐字或近似",
        "shot_ids": [sid] if sid is not None else [],
    }
