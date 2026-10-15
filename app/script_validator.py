# -*- coding: utf-8 -*-
"""剧本事后校验与修复（2026-10-15，参考 KT-AI-Studio 的 Post Validation 口径）

## 为什么需要这个模块

剧本由 LLM 生成，提示词层反复强调的四类约束**无法保证真的被遵守**。
KT-AI-Studio 的做法是「生成规则 + 事后校验」成对：system prompt 里写规则，
LLM 返回后再跑一遍确定性校验。本项目此前只有提示词端、没有校验端，
于是规则写了也白写 —— 本模块补上校验端。

四类缺陷：

① **角色名漂移** —— speaker / characters_in_shot / blocking.name 用了简称、
   别名或代称（「他」「对方」「那少年」）。这是**确定性可修复**的：
   下游「按名字取设定图」时名字对不上会**静默丢弃**，画面里角色外观只能靠模型
   自己想象 —— 这是「角色长相漂移」最隐蔽也最致命的来源。
   修复方式 = 复用 asset_name_match 的三级降级（exact → normalized → 唯一子串）。

② **出场人数过多** —— >2 人同框。AI 生成多人同框极易把角色画成克隆体或互相粘连
   （见 script_generate.txt【可拍性红线】⑧）。

③ **相对空间描述** —— 「旁边 / 附近 / 同上 / 环境同前」。每一镜由**独立的生成调用**
   产出，相对表达会让不同镜头各自脑补出不同空间，造成「同一场景换来换去」。

④ **系统 / 虚拟 UI 场景** —— 描述了模型画不准的 UI 细节（文字、边框、数据流），
   画出来是乱码与噪点，且干扰后期合成。

## 设计原则（与 asset_name_match 一致）

**宁可告警，不可静默。**

- ① 属确定性修复：改名是安全的（语义不变），直接改并记账。
- ②③④ 属**内容质量**问题：只记账、**不改写**（改写就是篡改剧情），
  交给质检（qc_script_check.txt 第 28~31 条）与人工评审处理。
- 任何异常一律 fail-open：剧本阶段抛异常会让整集重跑，代价远大于一条告警。
"""

from __future__ import annotations

import logging

import asset_name_match

logger = logging.getLogger(__name__)

__all__ = ["MAX_CHARS_IN_SHOT", "validate_and_repair_shots"]

#: 单镜出场人数上限。>2 人同框时 AI 极易把角色画成克隆体或互相粘连。
#: 与 script_generate.txt【可拍性红线】⑧、qc_script_check.txt 第 28 条同一口径。
MAX_CHARS_IN_SHOT = 2

#: 相对位置 / 指代性表达。每一镜独立生成，这类表达会让各镜脑补出不同空间。
_RELATIVE_SPACE_PATTERNS = (
    "旁边", "附近", "不远处", "远处可见", "路旁", "屋外", "里屋",
    "同上", "同前", "延续前镜", "环境同前", "和刚才一样", "同一间",
)

#: 系统 / 虚拟 UI 场景识别词。
_UI_SCENE_PATTERNS = (
    "系统界面", "系统空间", "查看属性", "系统提示", "虚拟面板", "属性面板",
)

#: 纯黑底的合格写法（命中即视为已合规，不再告警）。
_BLACK_BG_PATTERNS = ("纯黑背景", "纯黑底", "全黑背景")


def _char_index(bible):
    """从 bible 建「规范角色名 -> 规范角色名」索引（asset_name_match 的索引形态）。"""
    idx = {}
    for c in ((bible or {}).get("characters") or []):
        if isinstance(c, dict):
            name = str(c.get("name") or "").strip()
        elif isinstance(c, str):
            name = c.strip()
        else:
            name = ""
        if name:
            idx[name] = name
    return idx


def _canon(name, index):
    """把角色名归一到规范名，返回 (规范名 | None, 匹配级别)。

    三级降级见 asset_name_match：exact / normalized / substring（唯一命中）。
    匹配不到返回 (None, "")，由调用方记账而**不**静默改名 ——
    乱猜一个名字比不改更糟。
    """
    if not isinstance(name, str) or not name.strip():
        return None, ""
    try:
        return asset_name_match.match(name.strip(), index, kind="char")
    except Exception:  # noqa: BLE001 - 归一失败绝不能阻断剧本生成
        return None, ""


def _shot_text(shot):
    """取出用于空间 / UI 检查的文本（画面描述类字段）。"""
    parts = []
    for key in ("description", "visual_detail"):
        val = shot.get(key)
        if isinstance(val, str):
            parts.append(val)
    return " ".join(parts)


def _hit(text, patterns):
    return [p for p in patterns if p in text]


def validate_and_repair_shots(shots, bible, label: str = ""):
    """对一集 / 一块的分镜做事后校验与修复。

    :param shots:  build_shots_for_chunk 产出的镜头列表（角色名会被就地修正）
    :param bible:  项目设定库（需含 characters 列表，元素为 dict 或字符串）
    :param label:  日志标签（如「第3集/第1块」）
    :returns: (shots, summary)，summary 形如::

        {
          "renamed": [{"shot": 2, "field": "characters_in_shot",
                       "from": "赵天霸", "to": "赵天霸（中年）", "level": "normalized"}],
          "findings": [{"kind": "char_count"|"relative_space"|"ui_scene",
                        "shot": 3, "detail": "..."}],
        }

    本函数**不抛异常**：任何内部错误都降级为「原样返回 + 已收集的 summary」。
    """
    summary = {"renamed": [], "findings": []}
    if not isinstance(shots, list) or not shots:
        return shots, summary

    try:
        index = _char_index(bible)
        if not index:
            # 没有角色名单就无从归一；人数 / 空间 / UI 检查仍可做。
            logger.debug("[剧本校验] %s：bible 无 characters，跳过角色名归一", label)

        for pos, shot in enumerate(shots, start=1):
            if not isinstance(shot, dict):
                continue
            sid = shot.get("shot_id") or pos

            # ---- ① 角色名归一：characters_in_shot / dialogue.speaker / blocking.name
            if index:
                raw_list = shot.get("characters_in_shot")
                if isinstance(raw_list, list):
                    resolved, missing, fuzzy = asset_name_match.resolve_names(
                        raw_list, index, kind="char")
                    for _orig, _to, _level in fuzzy:
                        summary["renamed"].append({
                            "shot": sid, "field": "characters_in_shot",
                            "from": _orig, "to": _to, "level": _level})
                    for _nm in missing:
                        summary["findings"].append({
                            "kind": "unknown_char", "shot": sid,
                            "detail": "角色名不在设定库：" + str(_nm)})
                    shot["characters_in_shot"] = resolved or raw_list

                dlg = shot.get("dialogue")
                if isinstance(dlg, list):
                    for line in dlg:
                        if not isinstance(line, dict):
                            continue
                        canon, level = _canon(line.get("speaker"), index)
                        if canon and canon != line.get("speaker"):
                            summary["renamed"].append({
                                "shot": sid, "field": "dialogue.speaker",
                                "from": line.get("speaker"), "to": canon, "level": level})
                            line["speaker"] = canon

                blocking = shot.get("blocking")
                if isinstance(blocking, list):
                    for blk in blocking:
                        if not isinstance(blk, dict):
                            continue
                        canon, level = _canon(blk.get("name"), index)
                        if canon and canon != blk.get("name"):
                            summary["renamed"].append({
                                "shot": sid, "field": "blocking.name",
                                "from": blk.get("name"), "to": canon, "level": level})
                            blk["name"] = canon

            # ---- ② 出场人数
            in_shot = shot.get("characters_in_shot")
            n_chars = len(in_shot) if isinstance(in_shot, list) else 0
            if n_chars > MAX_CHARS_IN_SHOT:
                summary["findings"].append({
                    "kind": "char_count", "shot": sid,
                    "detail": "出场 %d 人（上限 %d）：%s" % (
                        n_chars, MAX_CHARS_IN_SHOT,
                        "、".join(str(x) for x in in_shot))})

            # ---- ③ 相对空间描述
            text = _shot_text(shot)
            if text:
                hits = _hit(text, _RELATIVE_SPACE_PATTERNS)
                if hits:
                    summary["findings"].append({
                        "kind": "relative_space", "shot": sid,
                        "detail": "含相对位置/指代表达：" + "、".join(hits)})

            # ---- ④ 系统 UI 场景未写纯黑底
            if text:
                ui_hits = _hit(text, _UI_SCENE_PATTERNS)
                if ui_hits and not _hit(text, _BLACK_BG_PATTERNS):
                    summary["findings"].append({
                        "kind": "ui_scene", "shot": sid,
                        "detail": "系统/虚拟 UI 场景未写「纯黑背景」：" + "、".join(ui_hits)})

    except Exception as exc:  # noqa: BLE001 - fail-open，绝不阻断剧本生成
        logger.warning("[剧本校验] %s 校验过程异常（原样返回）：%s: %s",
                       label, type(exc).__name__, exc)
        return shots, summary

    if summary["renamed"]:
        logger.info("[剧本校验] %s 角色名归一 %d 处（例：%s）", label,
                    len(summary["renamed"]), summary["renamed"][0])
    if summary["findings"]:
        kinds = {}
        for f in summary["findings"]:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
        logger.warning("[剧本校验] %s 内容质量问题 %d 处：%s", label,
                       len(summary["findings"]), kinds)

    return shots, summary
