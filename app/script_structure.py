# -*- coding: utf-8 -*-
"""剧本结构校验（**确定性**，不调模型）—— 治「剧本产出后才发现硬伤」。

定位：接在 `_norm_shots` 之后、分镜生成之前，把**能机器判定**的问题一次性列出来；
语义类问题（爽点铺垫够不够、人物动机深不深）仍交给提示词与人工。

设计纪律：
* **不误报优先**：每条规则都必须在「按当前 config 正确生成的剧本」上不触发。
  典型例子是时长节奏 —— `SHOT_DURATION_MIN == SHOT_DURATION_MAX == 10.0`
  （用户 2026-10-14 定稿：每镜固定 10 秒，收尾镜可更短）时**跳过该项**，
  否则每个合规剧本都会被判「节奏平板」。
* **只读**：不修改剧本，只返回结论（`p0` 必须修 / `p1` 建议修 / `info`）。
* 不 import 重量级模块；config 延迟导入。

结论形状::

    {"ok": bool,                       # 无 p0 即 ok
     "p0": [{"code","message","shot_id"}...],
     "p1": [...], "info": [...],
     "stats": {"shots","duration","dialogue_shots",...},
     "bible": {...}}                   # 同 bible_coverage_verdict()
"""
from __future__ import annotations

import re

#: 允许的节拍四值（与 novel_to_script._BEAT_WHITELIST 同口径）
BEATS = ("开场", "触发", "高潮", "收尾")

#: 旧格式里必须补齐的镜头字段（缺 = schema 漂移，下游会按默认值兜底但会丢信息）
SCHEMA_KEYS = ("caption", "items_state", "motion", "first_frame", "last_frame")

#: 外观特征词**同义词组**（出现在镜头文本、却在该角色 bible 外貌里**整组都找不到** → 漂移风险）。
#: 用组而不是单词：bible 写「伤痕」、镜头写「浅疤」是同一件事，不该报漂移。
FEATURE_GROUPS = (
    ("伤痕", "伤疤", "疤痕", "疤"),
    ("白发", "银发", "白头发"),
    ("长发",),
    ("短发",),
    ("绷带",),
    ("血迹", "血渍"),
    ("断臂",),
    ("断指",),
    ("面具",),
    ("纹身",),
    ("眼罩",),
    ("耳环",),
    ("项链",),
    ("戒指",),
    ("斗篷",),
    ("披风",),
    ("斗笠",),
    ("锁链",),
    ("枷锁",),
)

#: 「本镜在躲/挡」类反应动词
REACTION_WORDS = ("避开", "闪开", "躲开", "侧身避", "后仰", "格挡", "架住", "挡下", "接住")

#: 「有人在进攻」的可见证据（用于动作链前因判定）
ATTACK_WORDS = ("巴掌", "扇", "打", "踢", "踹", "劈", "砍", "抡", "挥拳", "出拳", "抓", "推", "扑", "袭来", "攻来", "刺")


def _txt(*vals) -> str:
    return " ".join(str(v or "") for v in vals)


def _dialogues(shot: dict) -> list:
    d = shot.get("dialogue")
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    return []


def _dialogue_text(shot: dict) -> str:
    d = shot.get("dialogue")
    if isinstance(d, str):                      # 旧格式：整段字符串
        return d.strip()
    parts = [str(x.get("text") or "") for x in _dialogues(shot)]
    if not parts:
        dt = shot.get("dialogue_text")
        if isinstance(dt, str) and dt.strip():
            parts = [dt]
    return "".join(parts)


def _issue(code: str, message: str, shot_id=None) -> dict:
    return {"code": code, "message": message, "shot_id": shot_id}


def bible_coverage_verdict(script: dict) -> dict:
    """bible ↔ 镜头的一致性摘要（供验收报告引用；不调模型）。"""
    chars = [c for c in (script.get("characters") or []) if isinstance(c, dict)]
    items = [i for i in (script.get("items") or []) if isinstance(i, dict)]
    c_names = {str(c.get("name") or "").strip() for c in chars if c.get("name")}
    i_names = {str(i.get("name") or "").strip() for i in items if i.get("name")}
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]

    used_c, used_i = set(), set()
    unknown = []
    for s in shots:
        for n in s.get("characters_in_shot") or []:
            n = str(n or "").strip()
            if n:
                used_c.add(n)
                if c_names and n not in c_names:
                    unknown.append({"kind": "character", "name": n, "shot_id": s.get("shot_id")})
        for n in s.get("items_in_shot") or []:
            n = str(n or "").strip()
            if n:
                used_i.add(n)
                if i_names and n not in i_names:
                    unknown.append({"kind": "item", "name": n, "shot_id": s.get("shot_id")})
        st = s.get("items_state")
        if isinstance(st, dict):
            for n in st.keys():
                n = str(n or "").strip()
                if n:
                    used_i.add(n)
                    if i_names and n not in i_names:
                        unknown.append({"kind": "item", "name": n, "shot_id": s.get("shot_id")})

    by_name = {str(c.get("name") or "").strip(): c for c in chars}
    drift = []
    for s in shots:
        blob = _txt(s.get("description"), s.get("visual_detail"), s.get("action"))
        for n in s.get("characters_in_shot") or []:
            n = str(n or "").strip()
            app = str((by_name.get(n) or {}).get("appearance") or "")
            if not app:
                continue
            for grp in FEATURE_GROUPS:
                if any(w in blob for w in grp) and not any(w in app for w in grp):
                    drift.append({"character": n, "feature": grp[0], "shot_id": s.get("shot_id")})

    return {
        "characters_defined": len(c_names),
        "characters_used": len(used_c & c_names) if c_names else len(used_c),
        "items_defined": len(i_names),
        "items_used": len(used_i & i_names) if i_names else len(used_i),
        "unused_characters": sorted(c_names - used_c) if c_names else [],
        "unused_items": sorted(i_names - used_i) if i_names else [],
        "unknown_names": unknown[:30],
        "feature_drift": drift[:30],
        "ok": not unknown and not drift,
    }


def check(script: dict, *, strict_duration: bool = None) -> dict:
    """全量结构校验。`strict_duration=None` 时按 config 决定是否检查时长节奏。"""
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    p0, p1, info = [], [], []

    dmin = dmax = None
    if strict_duration is None:
        try:
            from config import SHOT_DURATION_MIN, SHOT_DURATION_MAX
            dmin, dmax = float(SHOT_DURATION_MIN), float(SHOT_DURATION_MAX)
        except Exception:  # noqa: BLE001
            dmin = dmax = None
        # 固定时长口径（MIN==MAX）下，10 秒/镜 = 合规，不是缺陷 → 跳过节奏检查
        strict_duration = bool(dmin is not None and dmax is not None and dmin != dmax)

    if not shots:
        return {"ok": False, "p0": [_issue("no_shots", "剧本没有任何镜头")],
                "p1": [], "info": [], "stats": {"shots": 0}, "bible": {}}

    # ---------- schema：dialogue 必须是数组 ----------
    str_dlg = [s.get("shot_id") for s in shots if isinstance(s.get("dialogue"), str)
               and str(s.get("dialogue") or "").strip()]
    if str_dlg:
        p0.append(_issue("dialogue_string",
                         "有 %d 镜的 dialogue 是**字符串**（旧格式）：配音链路按结构化数组取说话人，"
                         "字符串会退化成无角色台词 → 逐角色音色失效。镜号：%s"
                         % (len(str_dlg), "、".join(str(x) for x in str_dlg[:12]))))

    # ---------- schema：缺标准字段 ----------
    missing = {k: [s.get("shot_id") for s in shots if k not in s] for k in SCHEMA_KEYS}
    miss_summary = {k: v for k, v in missing.items() if v}
    if miss_summary:
        p1.append(_issue("schema_keys_missing",
                         "旧格式字段缺失：%s（下游按默认值兜底，但会丢掉 caption/运动/首末帧信息）"
                         % "；".join("%s 缺 %d 镜" % (k, len(v)) for k, v in miss_summary.items())))

    # ---------- 台词说话人 ----------
    no_spk = []
    for s in shots:
        for d in _dialogues(s):
            if not str(d.get("speaker") or "").strip() and str(d.get("text") or "").strip():
                no_spk.append(s.get("shot_id"))
                break
    if no_spk:
        p0.append(_issue("dialogue_no_speaker",
                         "有 %d 镜的台词没有 speaker（无法分配音色 / 逐角色配音失效）。镜号：%s"
                         % (len(no_spk), "、".join(str(x) for x in no_spk[:12]))))

    # ---------- 开场：空镜 / 钩子太晚 ----------
    first = shots[0]
    first_has_actor = bool(first.get("characters_in_shot")) or bool(_dialogue_text(first).strip())
    try:
        first_dur = float(first.get("duration") or 0)
    except (TypeError, ValueError):
        first_dur = 0.0
    if not first_has_actor and first_dur >= 6:
        p0.append(_issue("opening_empty",
                         "首镜是纯空镜（无角色、无台词、%.0f 秒）——「先建立场景」属于平铺直叙开场，"
                         "观众前 10 秒拿不到冲突，钩子失效" % first_dur, first.get("shot_id")))
    cum = 0.0
    for s in shots:
        if bool(s.get("characters_in_shot")) or _dialogue_text(s).strip():
            break
        try:
            cum += float(s.get("duration") or 0)
        except (TypeError, ValueError):
            pass
    if cum > 6:
        p1.append(_issue("hook_too_late",
                         "开场累计 %.0f 秒才出现第一个人物/台词（建议 ≤6 秒）" % cum))

    # ---------- 节拍枚举 ----------
    bad_beat = [s.get("shot_id") for s in shots
                if str(s.get("beat") or "").strip() and s.get("beat") not in BEATS]
    if bad_beat:
        p1.append(_issue("beat_invalid",
                         "有 %d 镜的 beat 不在四值枚举（开场/触发/高潮/收尾），会被归一成「触发」，"
                         "丢失节奏意图。镜号：%s" % (len(bad_beat), "、".join(str(x) for x in bad_beat[:12]))))

    # ---------- 时长节奏（仅在项目允许弹性时长时检查） ----------
    if strict_duration:
        durs = []
        for s in shots:
            try:
                durs.append(round(float(s.get("duration") or 0), 1))
            except (TypeError, ValueError):
                pass
        if len(durs) >= 5:
            from collections import Counter
            top_n = Counter(durs).most_common(1)[0][1]
            if top_n / len(durs) >= 0.8:
                p1.append(_issue("duration_flat",
                                 "%d/%d 镜共用同一时长（%.1fs），节奏平板；建议对峙/打脸 3~5s、"
                                 "情绪/悬念 8~12s" % (top_n, len(durs), durs[0])))

    # ---------- bible 一致性 ----------
    bib = bible_coverage_verdict(script)
    for u in bib["unknown_names"][:8]:
        p1.append(_issue("bible_unknown_name",
                         "%s「%s」不在 bible 名单里（生成时会被过滤/告警，参考图绑不上）"
                         % ("角色" if u["kind"] == "character" else "物品", u["name"]),
                         u.get("shot_id")))
    if bib["unused_items"]:
        p1.append(_issue("bible_item_unused",
                         "bible 定义了但**从未入镜**的物品：%s（若它是剧情核心道具，必须出现在 "
                         "items_in_shot，否则出图没有锚点、观众也看不到）"
                         % "、".join(bib["unused_items"][:8])))
    for d in bib["feature_drift"][:8]:
        p1.append(_issue("feature_drift",
                         "镜 %s 写了角色「%s」的「%s」，但 bible 外貌里没有这一特征 → 跨镜漂移风险"
                         "（要么补进 bible，要么从镜头删掉）"
                         % (d["shot_id"], d["character"], d["feature"]), d.get("shot_id")))

    # ---------- 道具状态键必须在 items_in_shot ----------
    for s in shots:
        st = s.get("items_state")
        if isinstance(st, dict) and st:
            in_shot = set(str(x) for x in (s.get("items_in_shot") or []))
            orphan = [k for k in st.keys() if str(k) not in in_shot]
            if orphan:
                p1.append(_issue("items_state_orphan",
                                 "items_state 里的「%s」不在本镜 items_in_shot，状态变体不会被取用"
                                 % "、".join(orphan[:5]), s.get("shot_id")))

    # ---------- 动作链：反应必须有前因 ----------
    for i in range(1, len(shots)):
        cur, prev = shots[i], shots[i - 1]
        cur_act = _txt(cur.get("action"), cur.get("motion"))
        if not any(w in cur_act for w in REACTION_WORDS):
            continue
        prev_blob = _txt(prev.get("action"), prev.get("description"), prev.get("motion"))
        cur_desc = _txt(cur.get("description"))
        if not any(w in prev_blob for w in ATTACK_WORDS) and not any(w in cur_desc for w in ATTACK_WORDS):
            p1.append(_issue("action_chain_break",
                             "本镜有「躲/挡」类反应，但上一镜没有交代对应的进攻动作 → 观众看到"
                             "「凭空躲了一巴掌」。要给进攻方一个可见的起手镜头", cur.get("shot_id")))

    # ---------- 旧 prompt_h3 ----------
    legacy_h3 = [s.get("shot_id") for s in shots
                 if str(s.get("prompt_h3") or "").strip() and "<Picture" not in str(s.get("prompt_h3"))]
    if legacy_h3:
        p1.append(_issue("legacy_prompt_h3",
                         "有 %d 镜带**裸英文** prompt_h3（无 <Picture N> 标签）：生成期会被流水线丢弃并按"
                         "规范重建，留着只会误导人工审稿。建议删掉该字段。镜号：%s"
                         % (len(legacy_h3), "、".join(str(x) for x in legacy_h3[:12]))))

    # ---------- 场景元数据 ----------
    scenes = [sc for sc in (script.get("scenes") or []) if isinstance(sc, dict)]
    miss_meta = [str(sc.get("name") or "?") for sc in scenes
                 if not str(sc.get("int_ext") or "").strip() or not str(sc.get("time_of_day") or "").strip()]
    if miss_meta:
        info.append(_issue("scene_meta_missing",
                           "场景缺少 int_ext / time_of_day（可由场景名与光影推导）：%s"
                           % "、".join(miss_meta[:6])))

    total = 0.0
    for s in shots:
        try:
            total += float(s.get("duration") or 0)
        except (TypeError, ValueError):
            pass

    # ---------- v3 技能口径字段（2026-10-19 接入 doubao-creative-drama；P1 建议，不阻断） ----------
    # 旧剧本/旧链路无这些字段不算缺陷（enrich_v3 会兜底补齐）；只提示未增强的剧本。
    # ① 三层台词：dialogue.text 有实质内容但缺 spoken_text
    v3_no_spoken = [s.get("shot_id") for s in shots
                    for d in _dialogues(s)
                    if str(d.get("text") or "").strip()
                    and not str(d.get("spoken_text") or "").strip()]
    if v3_no_spoken:
        p1.append(_issue("v3_spoken_text_missing",
                         "有 %d 镜的台词缺 spoken_text（TTS 归一层）——配音链路仍会运行时归一，"
                         "但三层文本不可追溯；建议用 script_v3.enrich_v3 补齐。镜号：%s"
                         % (len(v3_no_spoken), "、".join(str(x) for x in v3_no_spoken[:12]))))
    # ② 单集创作卡
    if not script.get("episode_card"):
        p1.append(_issue("v3_episode_card_missing",
                         "顶层缺 episode_card（单集创作卡：核心戏剧问题/目标/失败代价/紧迫感/"
                         "信息差/金句）——这是技能口径的场级创作依据，建议 enrich_v3 补齐"))
    # ③ 场次攻防契约
    if not script.get("scene_contracts"):
        p1.append(_issue("v3_scene_contracts_missing",
                         "顶层缺 scene_contracts（场次攻防契约：每场目标→策略→反制→转折→场末变化）"
                         "——没有场次契约时镜头因果靠单镜自证，建议 enrich_v3 补齐"))
    # ④ 证据链台账
    if not script.get("evidence_ledger"):
        p1.append(_issue("v3_evidence_ledger_missing",
                         "顶层缺 evidence_ledger（证据链台账：指控/身份/能力类台词必须有"
                         " 声明→来源→证据→揭示场次→后果）——建议 enrich_v3 补齐"))

    return {
        "ok": not p0,
        "p0": p0, "p1": p1, "info": info,
        "stats": {"shots": len(shots), "duration": round(total, 1),
                  "dialogue_shots": sum(1 for s in shots if _dialogue_text(s).strip()),
                  "duration_strict_checked": bool(strict_duration)},
        "bible": bib,
    }


def summarize(result: dict, limit: int = 6) -> str:
    """一行摘要（写日志/步骤 detail 用）。"""
    if not result:
        return "剧本结构校验：无结论"
    p0, p1 = result.get("p0") or [], result.get("p1") or []
    if not p0 and not p1:
        return "剧本结构校验：通过（%d 镜）" % (result.get("stats") or {}).get("shots", 0)
    lines = ["剧本结构校验：P0 %d 项 / P1 %d 项" % (len(p0), len(p1))]
    for it in (p0 + p1)[:limit]:
        lines.append("  [%s] %s" % (it.get("code"), it.get("message")))
    return "\n".join(lines)
