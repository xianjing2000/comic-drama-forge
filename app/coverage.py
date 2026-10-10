# -*- coding: utf-8 -*-
"""原文覆盖率校验 + 遗漏自动补生成（小说 → 剧本「压缩提炼仍保留情节」的守门人）

设计要点
--------
1. 切分口径：把原文章节按「空行分段 → 句读（。！？；…）切句」切成原文单元，
   拼回后 = 原文全部非空白字符，切分本身不丢字（split_source_units）。
2. 承载判定（两段式漏斗，省 token 且可解释）：
   ① 规则层：单元前 12 字（短句用整句）在「全部镜头文本（画面/台词/旁白/提示词）」中
      连续命中 → 直接判「已承载」，无需 LLM（心理→旁白、对话→台词会保留原词句，命中率高）；
   ② 模型层：规则未命中的单元批量送 LLM 逐条判定是否被镜头承载（允许体裁改写），
      未判定到的批次自动降半批重试一次，仍失败则记为「未判定」（保守计入遗漏）。
3. 双指标（v2 收紧口径）：
   ① 情节级覆盖 plot_coverage = 已承载「情节单元」数 / 情节单元总数；章节标题这类非正文行
      从考核中排除，但会在报告 excluded_title_units 里逐条列明（含是否被片头/字幕承载）；
   ② 细节级覆盖 detail_coverage = 已承载正文字符数 / 正文字符总长（字级加权，反映原文单句内的
      修饰细节有多少字落入镜头）。正文字符总长 = 各正文单元长度之和，不再用含换行空行的
      原始文本长度做分母（旧口径分子只统计正文单元，分母含空白，即使 100% 承载也只能到 ~90%）。
   另附 literal_ratio（gram 字面措辞保留度）作为「原文措辞是否以画面/旁白原样保留」的诊断证据。
4. 补生成触发（v3 压缩提炼）：情节级或细节级覆盖率低于阈值即补生成；2026-10-10 起
   默认最多 **3 轮**（原 1 轮）—— 用户要求「还原小说的所有细节」，一轮补不干净就继续补；
   ⭐ 2026-10-10（用户要求「还原小说的所有细节」）：**环境描写与背景补叙也算原文内容**，
   不再由判定层主动判 covered=true 放行 —— 它们同样是小说细节，应当落到镜头里
   （可并入相邻镜头，不要求单独成镜）。仅为「凑字面覆盖率」补空镜仍然禁止。
   ⭐ 2026-10-10（用户要求「还原小说的所有细节，不能过度改写」）：阈值默认由 0.70 提到 **0.98**
   （env MJSCXT_COVERAGE_THRESHOLD 可覆盖），且**环境描写同样计入考核** ——
   原口径「只考核情节单元、明确不覆盖纯环境描写」会让大量原文细节合法地留在剧本之外
   （实测 96.5% 覆盖率下仍遗漏 7 条，而 96.5% 远高于旧阈值 0.70，从未触发补生成）。
5. 结果落盘：output/continuity/<项目键>/episodes/第N集_覆盖率.json，
   同时写回剧本 script["metadata"]["coverage"]，供前端直接展示覆盖率与遗漏清单。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

from config import CONTINUITY_DIR, COVERAGE_THRESHOLD
from llm_client import LLMError, LLMTruncatedError


from common_util import now as _now
logger = logging.getLogger(__name__)

COVERAGE_VERSION = "coverage_v2"
DEFAULT_THRESHOLD = float(COVERAGE_THRESHOLD or 0.70)   # 2026-10-08 解耦：阈值取 config 唯一事实源，不再 import 期依赖 novel_to_script
DETAIL_THRESHOLD = DEFAULT_THRESHOLD   # 细节级（字级）覆盖率阈值，默认与情节级同为 70%
LITERAL_GRAM = 4                       # 字面措辞保留度：gram 字滑窗粒度
MAX_EXCLUDED_LISTED = 60               # 报告中「非正文行（章节标题等）」最多列出条数
UNIT_MIN_CHARS = 4            # 短于此长度的句片并入上一单元（"他说。"这类不作为独立单元）
RULE_PREFIX_LEN = 12          # 规则层：单元前 12 字命中即视为已承载
JUDGE_BATCH_UNITS = 24        # 单次 LLM 判定的原文单元数
SUPPLEMENT_CHARS = 1200       # 补生成时每块聚合的遗漏原文字数上限
MAX_SUPPLEMENT_UNITS = 120    # 单轮补生成最多处理的遗漏单元数
MAX_MISSING_LISTED = 300      # 报告中「遗漏清单」明细最多列出条数

SYSTEM_COVERAGE = ("你是漫剧剧本质检员，负责逐条核对原小说片段是否已被剧本镜头承载。"
                   "只做客观核对，不改写、不创作，输出严格合法的 JSON。")


# ===================== 基础工具 =====================

def _safe_name(name, limit: int = 60) -> str:
    """与前端 / 项目键同样的安全化规则（非法字符转下划线）"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())
    return name[:limit] or "novel_project"


_PUNCT_RE = re.compile(r"[\s，。！？；：、“”‘’'\"（）()《》〈〉【】\[\]…—\-·,.!?;:~`|/\\]+")


def _norm(text) -> str:
    """去空白与标点，仅保留实义字符（用于承载判定）"""
    return _PUNCT_RE.sub("", str(text or ""))


_BLOCK_SPLIT_RE = re.compile(r"\n+")
_SENT_SPLIT_RE = re.compile(r"[^。！？!?；;…\n]+[。！？!?；;…]*")


def split_source_units(text) -> tuple:
    """把原文切成「句 / 段」单元。

    返回 (units, total_chars)；units 依次拼接后覆盖原文全部非空白字符，不丢字。
    """
    text = str(text or "").strip()
    if not text:
        return [], 0
    units = []
    for block in _BLOCK_SPLIT_RE.split(text):
        block = block.strip()
        if not block:
            continue
        for piece in _SENT_SPLIT_RE.findall(block):
            piece = piece.strip()
            if not piece:
                continue
            if units and len(piece) < UNIT_MIN_CHARS:
                units[-1] = units[-1] + piece      # 过短片段并入上一单元
                continue
            units.append(piece)
    return units, len(text)


def build_shot_corpus(shots) -> str:
    """把整集镜头拼成一段「已承载文本」（画面 / 细节 / 台词 / 提示词 / 情绪）

    ⚠️ ``visual_detail`` 必须计入：剧本阶段 description 限 200 字，超长的原文承载会被拆到
    ``visual_detail``（见 novel_to_script._norm_shots）。漏掉它会让「正文其实写进镜头了、
    覆盖率却判未承载」的假失败出现 —— 旁白取消后更多原文走这条路，风险显著上升。
    ⚠️ ``narration`` 是**旧剧本遗留字段**，保留读取只为兼容改造前生成的项目。
    """
    parts = []
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        for key in ("description", "visual_detail", "narration", "picture", "dialogue_text",
                    "audio_cues", "emotion", "camera", "location", "shot_type", "prompt_h3"):
            v = s.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v)
        dlg = s.get("dialogue")
        if isinstance(dlg, list):
            for d in dlg:
                if isinstance(d, dict):
                    parts.append(str(d.get("text") or ""))
                elif isinstance(d, str):
                    parts.append(d)
    return _norm(" ".join(parts))


def _rule_probe(unit: str) -> str:
    """规则层探针：短句用整句，长句用前 RULE_PREFIX_LEN 字"""
    n = _norm(unit)
    if not n:
        return ""
    return n if len(n) <= RULE_PREFIX_LEN else n[:RULE_PREFIX_LEN]


def _rate(covered_n: int, total_n: int) -> float:
    return round(covered_n / float(total_n), 4) if total_n else 1.0


def _pct(value: float) -> float:
    return round(float(value) * 100, 2)


# 章节标题这类「非正文行」：不计入覆盖率考核，但会在报告中逐条列明
_TITLE_RE = re.compile(
    r"^(第[〇零一二三四五六七八九十百千万两\d]+[节章回卷部篇集话]"
    r"(\s*[:：、.．\-—]\s*.*)?"
    r"|序章|序言|叙|楔子|引子|尾声|后记|尾记|番外.{0,20}"
    r"|[上中下]卷.{0,20})$")


def is_title_unit(unit: str) -> bool:
    """是否为章节标题这类非正文行（如「第二节：逆光阴五百年觉悟」「第一章 起源」）"""
    t = str(unit or "").strip().strip("　 ")
    if not t or len(t) > 30:
        return False
    if _TITLE_RE.match(t):
        return True
    # 「第一章：xxx，yyy」这类标题后带短副题的写法
    m = re.match(r"^第[〇零一二三四五六七八九十百千万两\d]+[节章回卷部篇集话]\s*[:：、.．\-—]\s*\S{1,20}$", t)
    return bool(m)


def _grams(nstr: str, gram: int = LITERAL_GRAM) -> set:
    """把（已去标点的）字符串切成 gram 字滑窗集合"""
    if not nstr:
        return set()
    if len(nstr) < gram:
        return {nstr}
    return {nstr[i:i + gram] for i in range(len(nstr) - gram + 1)}


def literal_ratio(unit: str, corpus_grams: set, gram: int = LITERAL_GRAM) -> float:
    """原文措辞保留度：单元切 gram 字滑窗后命中「镜头文本」的比例（0~1）"""
    gs = _grams(_norm(unit), gram)
    if not gs:
        return 1.0
    hit = sum(1 for g in gs if g in corpus_grams)
    return round(hit / float(len(gs)), 4)


# ===================== LLM 判定 =====================

def _json_call(client, prompt: str, label: str = "coverage", temperature: float = 0.2,
               max_tokens: int = 4000, events=None, max_attempts: int = 3,
               token_ladder=(6000, 10000, 16000)):
    """统一的 JSON 调用（优先走截断感知的 chat_json_robust）"""
    def on_event(ev):
        if events is not None and isinstance(ev, dict):
            row = dict(ev)
            row["stage"] = label
            row.setdefault("event", "coverage_retry")
            events.append(row)

    robust = getattr(client, "chat_json_robust", None)
    if robust is not None:
        return robust(prompt, system=SYSTEM_COVERAGE, temperature=temperature,
                      max_tokens=max_tokens, max_attempts=max_attempts,
                      token_ladder=token_ladder, on_event=on_event)
    return client.chat_json(prompt, system=SYSTEM_COVERAGE,
                            temperature=temperature, max_tokens=max_tokens)


def _shot_lines(shots, desc_limit: int = 40) -> list:
    lines = []
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("shot_id")
        desc = str(s.get("description") or "").replace("\n", " ").strip()[:desc_limit]
        # visual_detail：description 截断后溢出的画面细节（承载原文的那部分要能被判官看到，
        # 否则会把「已承载」误判成「未承载」）
        detail = str(s.get("visual_detail") or "").replace("\n", " ").strip()[:desc_limit]
        dlg = str(s.get("dialogue_text") or "").replace("\n", " ").strip()[:desc_limit]
        cues = str(s.get("audio_cues") or "").replace("\n", " ").strip()[:desc_limit]
        ctx = " | ".join(x for x in (desc, detail, dlg, cues) if x)
        lines.append(f"#{sid} {ctx}")
    return lines


def _judge_units(client, shots, units, ids, events=None, label: str = "coverage",
                 batch_size: int = JUDGE_BATCH_UNITS, retry_missing: bool = True) -> tuple:
    """批量 LLM 判定；返回 ({unit_id: bool}, {unit_id: bool}, failed_batches)

    第一个返回值是「被判定过」的单元（含判 false 的），第二个仅判 true 的。
    """
    judged, covered, failed = {}, {}, []
    if not ids or client is None:
        return judged, covered, failed
    shot_lines = _shot_lines(shots, desc_limit=40 if len(shots or []) <= 200 else 24)
    shot_block = "\n".join(shot_lines) if shot_lines else "（本集暂无镜头）"

    for start in range(0, len(ids), max(1, int(batch_size))):
        batch = ids[start:start + max(1, int(batch_size))]
        unit_block = "\n".join(f"{uid}｜{str(units[uid - 1])[:200]}" for uid in batch)
        prompt = (
            "【任务】逐条核对下列【原文片段】是否已被【本集剧本镜头】承载。\n"
            "【判定口径】允许体裁形式改写：心理描写→角色自语台词或画面神态、叙述→画面动作描述、"
            "环境描写→画面与音效、对话→台词；只要该片段的情节、信息、人物、情绪在镜头中被表达出来，"
            "即算「已承载」。\n"
            "【压缩提炼口径（重要）】本剧剧本是「压缩提炼」而非「逐句照搬」：纯背景补叙、纯环境描写、"
            "世界观/势力背景交代等**不推进剧情**的片段，允许被删去或仅一笔带过，这类片段**应判 "
            "covered=true（不算遗漏）**。只有当该片段承载了推动剧情的冲突/转折/关键动作/金句，"
            "却在镜头里完全找不到对应（被删除、被跳过）时，才判「未承载」。\n"
            "【注意】本系统不产出旁白：原文里的背景补叙与环境描写应当靠画面（description / "
            "visual_detail）承载，心理活动靠角色自语台词或神态动作承载。因此**不要**因为"
            "「没有旁白」而判未承载，只看画面与台词里有没有对应表达。\n\n"
            "【本集剧本镜头】（格式：#镜头号 画面描述｜画面细节｜台词｜音效）\n" + shot_block + "\n\n"
            "【原文片段】（格式：编号｜片段）\n" + unit_block + "\n\n"
            "【输出要求】严格只输出一个 JSON 对象，不要解释文字：\n"
            '{"results": [{"id": 1, "covered": true, "shot_id": 3, "reason": "≤15字"}]}\n'
            "【硬性约束】results 必须逐条覆盖上面全部 " + str(len(batch)) +
            " 条编号（原样使用给出的编号，一条不漏）；covered 为布尔值；"
            "shot_id 填最能承载该片段的镜头号，找不到填 null。"
        )
        try:
            data = _json_call(client, prompt, label=f"{label}#{start // max(1, int(batch_size)) + 1}",
                              events=events)
        except (LLMError, LLMTruncatedError) as e:
            failed.append({"batch": batch[:1], "error": str(e)[:200]})
            logger.warning(f"覆盖率判定批次失败（{label}）：{e}")
            continue
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            failed.append({"batch": batch[:1], "error": "模型未返回 results 数组"})
            continue
        got = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                uid = int(r.get("id"))
            except (TypeError, ValueError):
                continue
            if uid not in batch:
                continue
            got.add(uid)
            ok = bool(r.get("covered"))
            judged[uid] = ok
            if ok:
                covered[uid] = True
        missed = [u for u in batch if u not in got]
        if missed and retry_missing:
            # 降半批重试一次，尽量少留「未判定」
            sub_judged, sub_covered, sub_failed = _judge_units(
                client, shots, units, missed, events=events, label=label + "r",
                batch_size=max(4, int(batch_size) // 3), retry_missing=False)
            judged.update(sub_judged)
            covered.update(sub_covered)
            failed.extend(sub_failed)
            missed = [u for u in missed if u not in sub_judged]
        if missed:
            failed.append({"batch": missed[:20], "error": "模型未返回这些单元的判定"})
    return judged, covered, failed


# ===================== 覆盖率校验 =====================

def check_coverage(client, chapter_text, script, threshold=None, use_llm: bool = True,
                   events=None) -> dict:
    """对「原文章节文本 vs 单集剧本」做覆盖率校验（不做补生成）

    v2 双指标：
      - 情节级 plot_coverage：正文单元（排除章节标题等非正文行）被镜头承载的比例；
      - 细节级 detail_coverage：正文单元「字数」被承载的比例（字级加权，反映单句修饰细节）。
    """
    t0 = time.time()
    shots = (script or {}).get("shots") or []
    units, total_chars = split_source_units(chapter_text)
    corpus = build_shot_corpus(shots)
    corpus_grams = _grams(corpus)

    title_ids, body_ids = [], []
    for uid in range(1, len(units) + 1):
        (title_ids if is_title_unit(units[uid - 1]) else body_ids).append(uid)

    covered_ids, rule_hits, pending = set(), 0, []
    for uid in list(body_ids) + list(title_ids):
        probe = _rule_probe(units[uid - 1])
        if probe and probe in corpus:
            covered_ids.add(uid)
            rule_hits += 1
        elif uid in body_ids:
            pending.append(uid)

    judged, llm_covered, failed = {}, {}, []
    if pending and use_llm and client is not None:
        judged, llm_covered, failed = _judge_units(client, shots, units, pending, events=events)
        covered_ids.update(llm_covered.keys())

    body_covered = [uid for uid in body_ids if uid in covered_ids]
    missing_ids = [uid for uid in body_ids if uid not in covered_ids]
    body_chars = sum(len(units[uid - 1]) for uid in body_ids)
    covered_chars = sum(len(units[uid - 1]) for uid in body_covered)
    literal_sum = sum(len(units[uid - 1]) * literal_ratio(units[uid - 1], corpus_grams)
                      for uid in body_ids)

    missing_rows = []
    for uid in missing_ids[:MAX_MISSING_LISTED]:
        unit = units[uid - 1]
        reason = "unjudged" if (uid in pending and uid not in judged) else "not_covered"
        missing_rows.append({"id": uid, "text": unit[:300], "chars": len(unit), "reason": reason})

    excluded_rows = [{"id": uid, "text": units[uid - 1][:120], "chars": len(units[uid - 1]),
                      "carried": bool(uid in covered_ids)}
                     for uid in title_ids[:MAX_EXCLUDED_LISTED]]

    thr = float(threshold if threshold is not None else DEFAULT_THRESHOLD)
    dthr = float(DETAIL_THRESHOLD)
    coverage = _rate(len(body_covered), len(body_ids))
    detail = round(covered_chars / float(body_chars), 4) if body_chars else 1.0
    literal = round(literal_sum / float(body_chars), 4) if body_chars else 1.0
    report = {
        "version": COVERAGE_VERSION,
        "checked_at": _now(),
        "chapter_chars": total_chars,
        # —— 单元口径：unit_count 沿用旧名但语义为「正文章节单元（不含标题行）」
        "all_unit_count": len(units),
        "unit_count": len(body_ids),
        "covered_count": len(body_covered),
        "missing_count": len(missing_ids),
        "excluded_title_count": len(title_ids),
        "excluded_title_units": excluded_rows,
        "excluded_title_truncated": max(0, len(title_ids) - len(excluded_rows)),
        # —— 情节级覆盖（单元承载率，排除标题行）
        "coverage": coverage,
        "coverage_percent": _pct(coverage),
        "plot_coverage": coverage,
        "plot_coverage_percent": _pct(coverage),
        "plot_unit_count": len(body_ids),
        "plot_covered_count": len(body_covered),
        # —— 细节级覆盖（字级加权，正文口径分母）
        "body_char_count": body_chars,
        "covered_char_count": covered_chars,
        "missing_char_count": sum(len(units[uid - 1]) for uid in missing_ids),
        "char_coverage": detail,
        "char_coverage_percent": _pct(detail),
        "detail_coverage": detail,
        "detail_coverage_percent": _pct(detail),
        "detail_threshold": dthr,
        "detail_threshold_percent": _pct(dthr),
        "detail_passed": bool(detail >= dthr),
        # —— 诊断：原文措辞（gram）保留度，衡量「画面/旁白是否用原文措辞承载细节」
        "literal_ratio": literal,
        "literal_ratio_percent": _pct(literal),
        "literal_gram": LITERAL_GRAM,
        "threshold": thr,
        "threshold_percent": _pct(thr),
        "passed": bool(coverage >= thr),
        "zero_omission": bool(not missing_ids),
        "rule_hits": rule_hits,
        "llm_judged": len(judged),
        "llm_covered": len(llm_covered),
        "llm_failed_count": len(failed),
        "llm_failed_batches": failed[:10],
        "use_llm": bool(use_llm),
        "shot_count": len([s for s in shots if isinstance(s, dict)]),
        "episode_duration_sec": (script or {}).get("episode_duration_sec"),
        "missing": missing_rows,
        "missing_truncated": max(0, len(missing_ids) - len(missing_rows)),
        "missing_ids": missing_ids,
        "body_unit_ids": body_ids,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    return report


def _group_units(units, ids, chars_per_group: int = SUPPLEMENT_CHARS) -> list:
    """把遗漏单元按原文顺序聚合成若干块（每块 ≤ chars_per_group 字）"""
    groups, buf, buf_ids, size = [], [], [], 0
    for uid in ids:
        if not (1 <= int(uid) <= len(units)):
            continue
        u = units[int(uid) - 1]
        if buf and size + len(u) > chars_per_group:
            groups.append((buf, buf_ids))
            buf, buf_ids, size = [], [], 0
        buf.append(u)
        buf_ids.append(int(uid))
        size += len(u)
    if buf:
        groups.append((buf, buf_ids))
    return groups


def supplement_missing(client, chapter_text, script, missing_ids, episode_no=None,
                       events=None, max_units: int = MAX_SUPPLEMENT_UNITS,
                       main_unit_ids=None) -> dict:
    """把遗漏的原文单元补生成镜头并追加到 script["shots"]（只增不删）

    main_unit_ids 非空时，只对「该集正文单元集合」内的遗漏单元补生成，避免把邻章/非本集
    单元误补进来；为 None 时回退旧行为（不限制）。
    """
    units, _ = split_source_units(chapter_text)
    allow = [int(i) for i in main_unit_ids] if main_unit_ids else None
    ids = [int(i) for i in (missing_ids or [])
           if 1 <= int(i) <= len(units) and (allow is None or int(i) in allow)][:max_units]
    result = {"added_shots": 0, "new_shot_ids": [], "groups": 0, "errors": [],
              "only_add": True, "skipped_outside_episode": len([i for i in (missing_ids or [])
                                                               if allow is not None and int(i) not in allow]),
              "supplement_units": len(ids), "supplement_chars": sum(len(units[i - 1]) for i in ids)}
    if not ids:
        return result

    bible = {
        "title": (script or {}).get("title") or "",
        "style": (script or {}).get("style") or "",
        "characters": (script or {}).get("characters") or [],
        "items": (script or {}).get("items") or [],
        "scenes": (script or {}).get("scenes") or [],
        "production_notes": (script or {}).get("production_notes") or {},
    }
    groups = _group_units(units, ids)
    result["groups"] = len(groups)

    existing = list((script or {}).get("shots") or [])
    start_id = max([int(s.get("shot_id") or 0) for s in existing if isinstance(s, dict)] + [0]) + 1

    import novel_to_script as nts  # 惰性导入（2026-10-08 解耦）：本模块只在覆盖率补生成时用到它，
    # 模块级 import 会让 coverage → novel_to_script → coverage 形成静态环。
    raw_new = []
    for gi, (texts, g_ids) in enumerate(groups, 1):
        chunk = {"index": gi, "total": len(groups), "title": f"覆盖率补生成-{gi}",
                 "text": "\n".join(texts), "char_count": sum(len(t) for t in texts)}
        outline = {"summary": "以下原文片段尚未被任何镜头承载，请逐句补写成分镜（只补写缺失内容，不改写情节）",
                   "key_beats": [t[:40] for t in texts][:14]}
        target = max(1, nts.estimate_shots_for_chars(chunk["char_count"]))
        try:
            rows = nts.build_shots_for_chunk(client, bible, outline, chunk, target, events=events)
        except (LLMError, LLMTruncatedError) as e:
            result["errors"].append(f"补生成第 {gi} 组失败：{str(e)[:160]}")
            logger.warning(f"覆盖率补生成第 {gi} 组失败：{e}")
            continue
        for s in rows or []:
            if isinstance(s, dict):
                s = dict(s)
                s["source_unit_ids"] = list(g_ids)
                s["supplement"] = True
                raw_new.append(s)

    if raw_new:
        new_shots = nts._norm_shots(raw_new, bible, 1, start_id=start_id)
        ep = int(episode_no or (script or {}).get("episode_no")
                 or ((script or {}).get("metadata") or {}).get("episode_no") or 1)
        for sh in new_shots:
            sh["episode"] = ep
        script["shots"] = existing + new_shots
        nts.apply_episode_schema(script)
        result["added_shots"] = len(new_shots)
        result["new_shot_ids"] = [s.get("shot_id") for s in new_shots]
    return result


def coverage_root(continuity_dir: str = None, project_key: str = None) -> str:
    """覆盖率报告根目录：<continuity_dir>/<项目键>（与 continuity 的目录结构保持一致）"""
    base = continuity_dir or CONTINUITY_DIR
    return os.path.abspath(os.path.join(base, _safe_name(project_key or "novel_project")))


def coverage_episodes_dir(continuity_dir: str = None, project_key: str = None) -> str:
    """覆盖率报告目录：<continuity_dir>/<项目键>/episodes"""
    return os.path.join(coverage_root(continuity_dir, project_key), "episodes")


def coverage_report_path(continuity_dir: str, project_key: str, episode_no) -> str:
    """单集覆盖率报告绝对路径：<continuity_dir>/<项目键>/episodes/第N集_覆盖率.json"""
    return os.path.abspath(os.path.join(
        coverage_episodes_dir(continuity_dir, project_key), f"第{int(episode_no)}集_覆盖率.json"))


def load_coverage_report(continuity_dir: str, project_key: str, episode_no) -> dict:
    """读取单集覆盖率报告（不存在或损坏时返回空 dict，不抛异常）"""
    path = coverage_report_path(continuity_dir, project_key, episode_no)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"覆盖率报告读取失败（按无报告处理）{path}：{e}")
        return {}


def save_coverage_report(report: dict, continuity_dir: str = None, project_key: str = None,
                         episode_no=None) -> str:
    """覆盖率报告落盘（UTF-8 / 缩进 2），返回绝对路径。

    报告不参与剧情生成，落盘失败时由调用方决定是否降级，不影响剧本本体。
    """
    report = report or {}
    ep = int(episode_no or report.get("episode_no") or 1)
    pkey = _safe_name(project_key or report.get("project_key") or "novel_project")
    path = coverage_report_path(continuity_dir, pkey, ep)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(report)
    payload["project_key"] = pkey
    payload["episode_no"] = ep
    payload["report_path"] = path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"第{ep}集覆盖率报告已落盘：{path}")
    return path


def run_coverage_check(client, chapter_text, script, episode_no=None, threshold=None,
                       max_rounds: int = 3, use_llm: bool = True, events=None,
                       continuity_dir: str = None, project_key: str = None,
                       save: bool = True, detail_threshold=None,
                       progress_cb=None) -> dict:
    """覆盖率校验主流程：校验 → 情节级不足才补生成 → 复检 → 落盘 + 写回剧本 metadata

    :param progress_cb: fn(message)（2026-10-06 新增）。整个校验（初检 + N 轮补生成 +
        复检）此前**零进度回报**，托管侧 step_stalled_sec 与 step_elapsed_sec 恒相等，
        「卡住」与「慢」无法区分（2026-10-06 第2集 coverage 静默 34 分钟实录）。现在
        每次初检/补生成/复检前后都回调一次，调用方（continuity.report）透传给流水线
        进度条；不传则行为与旧版完全一致。

    v3 压缩提炼口径：触发补生成的条件是「情节级覆盖率低于阈值」（而非「遗漏清单非空」）。
    纯背景补叙/环境描写已由判定层判 covered=true（不算遗漏），少量漏掉的次要单元不会
    触发补生成，避免为「凑字面覆盖率」补出一堆无台词的静态空镜。
    """
    thr = float(threshold if threshold is not None else DEFAULT_THRESHOLD)
    dthr = float(detail_threshold if detail_threshold is not None else DETAIL_THRESHOLD)
    meta = (script or {}).setdefault("metadata", {})
    ep = int(episode_no or (script or {}).get("episode_no")
             or ((script or {}).get("metadata") or {}).get("episode_no") or 1)
    rounds = max(0, int(max_rounds or 0))
    # 0 = 关闭补生成：rounds_cap 保持 0，下方 while 的 `supplement_rounds < rounds_cap`
    # 恒为 False，补生成循环自然整体跳过（旧写法把 0 强转成 1，「0=关闭」从未生效）
    rounds_cap = max(int(rounds or 0), 0)

    history, supplement_rounds = [], 0
    if progress_cb:
        try:
            progress_cb("覆盖率初检：逐句核对原文是否被镜头承载…")
        except Exception:  # noqa: BLE001
            pass
    report = check_coverage(client, chapter_text, script, threshold=thr, use_llm=use_llm, events=events)
    if progress_cb:
        try:
            progress_cb(f"覆盖率初检完成：情节 {report.get('plot_coverage_percent')}% / "
                        f"细节 {report.get('detail_coverage_percent')}%，遗漏 "
                        f"{len(report.get('missing_ids') or [])} 条")
        except Exception:  # noqa: BLE001
            pass

    while (report.get("missing_ids")
           and report.get("plot_coverage", 0) < thr
           and supplement_rounds < rounds_cap):
        supplement_rounds += 1
        prev_missing = len(report.get("missing_ids") or [])
        if events:
            events.append({"label": "coverage", "attempt": supplement_rounds, "max_tokens": 0,
                           "finish_reason": "n/a", "truncated": False,
                           "note": f"第{ep}集覆盖率：情节级 {report.get('plot_coverage_percent')}% / "
                                     f"细节级 {report.get('detail_coverage_percent')}%，"
                                     f"遗漏 {prev_missing} 条，触发第 {supplement_rounds} 轮补生成"})
        if progress_cb:
            try:
                progress_cb(f"覆盖率不足，第 {supplement_rounds}/{rounds_cap} 轮补生成中"
                            f"（遗漏 {prev_missing} 条，只增不删）…")
            except Exception:  # noqa: BLE001
                pass
        sup = supplement_missing(client, chapter_text, script, report.get("missing_ids"),
                                 episode_no=ep, events=events,
                                 main_unit_ids=report.get("body_unit_ids"))
        sup["round"] = supplement_rounds
        sup["missing_before"] = prev_missing
        history.append(sup)
        if progress_cb:
            try:
                progress_cb(f"第 {supplement_rounds} 轮补生成结束：新增 {sup.get('added_shots', 0)} 镜，"
                            f"复检中…")
            except Exception:  # noqa: BLE001
                pass
        if not sup.get("added_shots"):
            logger.warning(f"第{ep}集覆盖率补生成第 {supplement_rounds} 轮未产出新镜头，停止补生成")
            break
        report = check_coverage(client, chapter_text, script, threshold=thr, use_llm=use_llm, events=events)
        report["round"] = supplement_rounds

    report["supplement_rounds"] = supplement_rounds
    report["supplement_shots"] = sum(int(h.get("added_shots") or 0) for h in history)
    report["supplement_errors"] = [e for h in history for e in (h.get("errors") or [])]
    report["supplement_history"] = [{"round": h.get("round"), "missing_before": h.get("missing_before"),
                                     "added_shots": h.get("added_shots"),
                                     "new_shot_ids": h.get("new_shot_ids"),
                                     "supplement_units": h.get("supplement_units"),
                                     "supplement_chars": h.get("supplement_chars")} for h in history]
    report["supplement_triggered"] = bool(supplement_rounds)
    report["detail_passed"] = bool(report.get("detail_coverage", 0) >= dthr)
    report["zero_omission"] = bool(not report.get("missing_ids"))
    report["passed"] = bool(report.get("plot_coverage", 0) >= thr and report["detail_passed"])
    report["episode_no"] = ep
    report["elapsed_sec_total"] = report.get("elapsed_sec")

    pkey = _safe_name(project_key or meta.get("project_key") or meta.get("project_name")
                      or (script or {}).get("title") or "novel_project")
    report["project_key"] = pkey
    report["coverage_mode"] = "full"

    path = None
    if save:
        try:
            path = save_coverage_report(report, continuity_dir=continuity_dir,
                                        project_key=pkey, episode_no=ep)
            report["report_path"] = path
        except Exception as e:  # 落盘失败不影响主流程
            report["save_error"] = str(e)[:200]
            logger.warning(f"覆盖率报告落盘失败：{e}")
    attach_to_script(script, report, path)
    return report


def summary_for_meta(report: dict, path: str = None) -> dict:
    """压缩版覆盖率信息，写回剧本 metadata，供前端面板直接展示（情节级 + 细节级）"""
    if not report:
        return {}
    r = dict(report or {})
    plot = r.get("plot_coverage", r.get("coverage") or 0.0)
    detail = r.get("detail_coverage", r.get("char_coverage") or 0.0)
    return {
        "version": r.get("version"),
        "checked_at": r.get("checked_at"),
        "episode_no": r.get("episode_no"),
        # 情节级（单元承载率，排除章节标题等非正文行）
        "plot_coverage": plot,
        "plot_coverage_percent": r.get("plot_coverage_percent", _pct(plot)),
        "plot_unit_count": r.get("plot_unit_count", r.get("unit_count")),
        "plot_covered_count": r.get("plot_covered_count", r.get("covered_count")),
        # 细节级（字级加权）
        "detail_coverage": detail,
        "detail_coverage_percent": r.get("detail_coverage_percent", _pct(detail)),
        "detail_threshold": r.get("detail_threshold", DETAIL_THRESHOLD),
        "detail_threshold_percent": r.get("detail_threshold_percent", _pct(DETAIL_THRESHOLD)),
        "detail_passed": bool(r.get("detail_passed")),
        "body_char_count": r.get("body_char_count"),
        "covered_char_count": r.get("covered_char_count"),
        "missing_char_count": r.get("missing_char_count"),
        # 兼容旧字段
        "coverage": plot,
        "coverage_percent": _pct(plot),
        "char_coverage": detail,
        "char_coverage_percent": _pct(detail),
        "unit_count": r.get("plot_unit_count", r.get("unit_count")),
        "covered_count": r.get("plot_covered_count", r.get("covered_count")),
        "threshold": r.get("threshold", DEFAULT_THRESHOLD),
        "threshold_percent": r.get("threshold_percent", _pct(DEFAULT_THRESHOLD)),
        "passed": bool(r.get("passed")),
        "zero_omission": bool(r.get("zero_omission")),
        # 遗漏与排除
        "missing_count": r.get("missing_count"),
        "missing_ids": r.get("missing_ids"),
        "missing": (r.get("missing") or [])[:20],
        "missing_preview": [m.get("text") for m in (r.get("missing") or [])[:8]],
        "excluded_title_count": r.get("excluded_title_count"),
        "excluded_title_units": r.get("excluded_title_units"),
        # 补生成
        "supplement_shots": r.get("supplement_shots"),
        "supplement_rounds": r.get("supplement_rounds"),
        "supplement_triggered": bool(r.get("supplement_triggered")),
        "supplement_errors": (r.get("supplement_errors") or [])[:5],
        # 诊断
        "literal_ratio": r.get("literal_ratio"),
        "literal_ratio_percent": r.get("literal_ratio_percent"),
        "shot_count": r.get("shot_count"),
        "episode_duration_sec": r.get("episode_duration_sec"),
        "elapsed_sec": r.get("elapsed_sec"),
        "report_path": path or r.get("report_path"),
    }


def attach_to_script(script: dict, report: dict, path: str = None) -> dict:
    """把覆盖率结果写回剧本（前端可直接从剧本读取）"""
    if not isinstance(script, dict):
        return script
    meta = script.setdefault("metadata", {})
    meta["coverage"] = summary_for_meta(report, path)
    if path or report.get("report_path"):
        meta["coverage_report_path"] = path or report.get("report_path")
    meta["coverage_threshold"] = report.get("threshold")
    return script


def script_coverage(script: dict) -> dict:
    """从剧本里取覆盖率摘要（前端用；缺失时返回空）"""
    return ((script or {}).get("metadata") or {}).get("coverage") or {}
