# -*- coding: utf-8 -*-
"""前置解析（pre-flight）：章节生成前先建「人物档案 + 故事梗概 + 关键事件 + 情绪」库。

设计目标
--------
用户要求：创建项目后输入小说章节 → 输出固定 JSON（人物档案 / 本章故事梗概 /
关键事件 / 人物情绪）→ 保存人设库 → 保证全片人物不 OOC（out of character）。

本模块提供：
- preflight_analyze  对单章原文做 LLM 解析，输出固定 JSON 结构
- save_preflight     落盘到 output/continuity/<项目键>/preflight/第N章.json
- load_preflight     读取已保存的前置解析结果（供后续集生成注入）
- merge_to_bible     将人物档案并入项目级 bible.json（锁定角色不 OOC）
- build_preflight_injection_block  把解析结果格式化为可注入 prompt 的文本块
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fs_atomic import atomic_write_json, read_json_strict
from llm_client import LLMError

logger = logging.getLogger(__name__)

PREFLIGHT_VERSION = "chapter_preflight_v1"
_PREFLIGHT_SUBDIR = "preflight"

_SYSTEM = (
    "你是漫剧编剧组的前置解析器。你的任务是在正式生成剧本之前，"
    "对单章原文做深度解析，输出固定 JSON，供后续集生成约束人物行为不 OOC。"
    "只输出严格合法的 JSON，不要解释、不要 markdown 代码块。"
)

_PROMPT_TEMPLATE = """【任务】解析小说《{novel_title}》第 {chapter_index} 章（{chapter_title}）的原文，输出固定 JSON。
【原文开始】
{chapter_text}
【原文结束】

【输出 schema（必须严格遵循，字符串字段按描述填写）】
{{
  "characters": [
    {{
      "name": "人物姓名（必须用原文中的规范称谓，不得改写或造新名）",
      "gender": "男 或 女（按原文推断，无法确定填 未知）",
      "age": "年龄描述（如 十六岁 / 青年 / 老年，无法确定填 未知）",
      "identity": "身份/职位（如 青云宗外门弟子，无法确定填空串）",
      "appearance": "静态定妆外貌（发色/瞳色/脸型/体格等不随剧情变化的特征，35字内；只写原文明确描述的，没有填 未知）",
      "personality": "核心性格（20字内，不随剧情变化，如 隐忍坚韧 / 嚣张跋扈）",
      "voice_style": "声线风格（15字内，如 清亮少年音 / 粗厉暴喝，无法确定填空串）",
      "emotions": [
        {{"beat": "触发情绪的原文情节锚点（15字内，如 被诬陷偷丹）", "emotion": "情绪词（如 屈辱/愤怒/震惊）", "intensity": 3}}
      ]
    }}
  ],
  "story_summary": "本章故事梗概（150字内，概括核心冲突与结果，不剧透后续章节）",
  "key_events": ["按原文顺序的关键情节事件，每条30字内，最多12条，只写已发生的事"],
  "character_mood_arc": "本章整体情绪走向（一句话，如 压迫→反抗→反转→震慑）"
}}

【硬性要求】
1. characters 按戏份从大到小排序：纯背景路人（无台词、无独立动作、不推进情节，如老人甲、村民若干）不登记，但共同推进情节的一方阵营／敌群／首领或群像（有共同称谓、各自开腔或行动）必须登记——有具名首领就登首领，只有共同称谓的群体就登记为一个群像角色（name 用原文群体称谓如「正道群敌」，appearance 写可辨识代表形象）；
2. appearance / personality 是定妆字段：只写原文中不随剧情变化的固有特征；
3. emotions 的 intensity 填 1-5 整数（1=微弱 5=极度）；
4. key_events 只写本章已发生的事，每条不超过30字；
5. 无内容时填 未知 或 空数组；
6. 只输出 JSON，不要任何解释文字。"""


def preflight_dir(continuity_dir: str, project_key: str) -> str:
    """前置解析结果目录：output/continuity/<项目键>/preflight/"""
    return os.path.join(continuity_dir, project_key, _PREFLIGHT_SUBDIR)


def preflight_path(continuity_dir: str, project_key: str, chapter_index: int) -> str:
    return os.path.join(preflight_dir(continuity_dir, project_key), f"第{int(chapter_index)}章.json")


def load_preflight(continuity_dir: str, project_key: str, chapter_index: int) -> dict:
    """读取某章的前置解析结果；不存在返回 {}"""
    path = preflight_path(continuity_dir, project_key, chapter_index)
    data = read_json_strict(path, None)
    if not isinstance(data, dict):
        return {}
    return data


def save_preflight(continuity_dir: str, project_key: str, result: dict) -> str:
    """把前置解析结果落盘，返回路径"""
    os.makedirs(preflight_dir(continuity_dir, project_key), exist_ok=True)
    path = preflight_path(continuity_dir, project_key, int(result.get("chapter_index") or 0))
    atomic_write_json(path, result)
    return path

def _build_prompt(novel_title: str, chapter_index: int, chapter_title: str,
                  chapter_text: str) -> str:
    text = (chapter_text or "").strip()
    if len(text) > 12000:
        text = text[:6000] + "\n……（中略）……\n" + text[-6000:]
    return _PROMPT_TEMPLATE.format(
        novel_title=novel_title,
        chapter_index=chapter_index,
        chapter_title=chapter_title or f"第{chapter_index}章",
        chapter_text=text,
    )


def _sanitize_preflight(raw: dict, chapter_index: int, chapter_title: str) -> dict:
    """把 LLM 原始输出清洗成固定 schema（缺字段补默认值，非法值收敛）"""
    out_chars = []
    for c in (raw.get("characters") or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        emotions = []
        for e in (c.get("emotions") or []):
            if not isinstance(e, dict):
                continue
            try:
                intensity = int(e.get("intensity") or 3)
            except (TypeError, ValueError):
                intensity = 3
            intensity = max(1, min(5, intensity))
            emotions.append({
                "beat": str(e.get("beat") or "").strip()[:30],
                "emotion": str(e.get("emotion") or "").strip()[:15],
                "intensity": intensity,
            })
        out_chars.append({
            "name": name[:20],
            "gender": str(c.get("gender") or "未知").strip()[:4],
            "age": str(c.get("age") or "未知").strip()[:10],
            "identity": str(c.get("identity") or "").strip()[:30],
            "appearance": str(c.get("appearance") or "未知").strip()[:60],
            "personality": str(c.get("personality") or "").strip()[:40],
            "voice_style": str(c.get("voice_style") or "").strip()[:30],
            "emotions": emotions[:8],
        })

    key_events = [str(x).strip()[:40] for x in (raw.get("key_events") or []) if str(x).strip()][:12]
    return {
        "version": PREFLIGHT_VERSION,
        "chapter_index": int(chapter_index),
        "chapter_title": str(chapter_title or f"第{int(chapter_index)}章")[:60],
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
        "characters": out_chars,
        "story_summary": str(raw.get("story_summary") or "").strip()[:300],
        "key_events": key_events,
        "character_mood_arc": str(raw.get("character_mood_arc") or "").strip()[:100],
    }


def preflight_analyze(client, novel_title: str, chapter_index: int,
                      chapter_title: str, chapter_text: str,
                      events: Optional[list] = None) -> dict:
    """调用 LLM 做单章前置解析，返回固定 schema dict（不落盘，由调用方 save_preflight）。

    失败时抛出 LLMError，由调用方决定是否降级。
    """
    prompt = _build_prompt(novel_title, chapter_index, chapter_title, chapter_text)
    if events is not None:
        events.append({"label": f"preflight#{chapter_index}", "event": "llm_call",
                      "prompt_chars": len(prompt)})
    raw = client.chat_json(
        prompt,
        system=_SYSTEM,
        temperature=0.3,
        max_tokens=4096,
        retries=2,
    )
    if not isinstance(raw, dict):
        raise LLMError(f"前置解析返回非 JSON：{type(raw).__name__}")
    result = _sanitize_preflight(raw, chapter_index, chapter_title)
    if events is not None:
        events.append({"label": f"preflight#{chapter_index}", "event": "done",
                       "characters": len(result["characters"]),
                       "key_events": len(result["key_events"])})
    return result

def merge_to_bible(continuity_dir: str, project_key: str, preflight: dict) -> dict:
    """把前置解析的人物档案并入项目级 bible.json（人物库，防 OOC 的核心）。

    规则：
    - 按 name 去重（归一化后比较，与 continuity._norm_name 口径一致）；
    - 新角色：首次出现即锁定（locked=True，外观/性格/身份/性别/声线全部锁定）；
    - 已有角色：不覆盖已锁定字段，只补充空缺字段 + 追加情绪记录（按章节）。

    返回 {"added": [...names], "updated": [...names], "bible": bible}
    """
    import continuity_store as cont_mod  # 2026-10-08 解耦：只需要 bible 读写，不再 import 整个 continuity（切断 chapter_preflight → continuity 环边）

    ch_idx = int(preflight.get("chapter_index") or 0)
    bible = cont_mod.load_bible(continuity_dir, project_key)

    name_map = {re.sub(r"[\s\u00b7\u2022\u3001,\uff0c\u3002.]+", "", str(c.get("name") or "")): c
                for c in (bible.get("characters") or []) if isinstance(c, dict)}

    added, updated = [], []
    for pf_char in (preflight.get("characters") or []):
        nm = re.sub(r"[\s\u00b7\u2022\u3001,\uff0c\u3002.]+", "", str(pf_char.get("name") or ""))
        if not nm:
            continue
        emotions = pf_char.get("emotions") or []
        if nm not in name_map:
            new_char = {
                "name": str(pf_char.get("name") or "").strip(),
                "gender": str(pf_char.get("gender") or "").strip(),
                "age": str(pf_char.get("age") or "").strip(),
                "identity": str(pf_char.get("identity") or "").strip(),
                "appearance": str(pf_char.get("appearance") or "").strip(),
                "personality": str(pf_char.get("personality") or "").strip(),
                "voice_style": str(pf_char.get("voice_style") or "").strip(),
                "titles": "",
                "locked": True,
                "appearance_locked": True,
                "first_episode": ch_idx,
                "current_outfit": "",
                "outfit_by_episode": {},
                "last_seen_episode": ch_idx,
                "emotions_by_chapter": {str(ch_idx): emotions},
            }
            bible.setdefault("characters", []).append(new_char)
            name_map[nm] = new_char
            added.append(new_char["name"])
        else:
            row = name_map[nm]
            # 补充空缺字段（不覆盖已锁定值）
            for field in ("gender", "age", "identity", "appearance", "personality", "voice_style"):
                if not str(row.get(field) or "").strip() and str(pf_char.get(field) or "").strip():
                    row[field] = str(pf_char.get(field) or "").strip()
            row["last_seen_episode"] = max(int(row.get("last_seen_episode") or 0), ch_idx)
            # 情绪按章节记录（不覆盖历史）
            emo = row.setdefault("emotions_by_chapter", {})
            emo[str(ch_idx)] = emotions
            updated.append(row["name"])

    cont_mod.save_bible(continuity_dir, project_key, bible)
    return {"added": added, "updated": updated, "bible": bible}


def build_preflight_injection_block(preflight: dict) -> str:
    """把前置解析结果格式化为可注入 LLM prompt 的文本块（防 OOC 约束）。

    如果某章没有前置解析（preflight 为空 dict 或无 characters），返回空串
    （调用方自行决定是否跳过注入）。
    """
    if not preflight or not preflight.get("characters"):
        return ""
    lines = ["【本章前置解析 · 人物约束（必须严格遵守，防止 OOC）】"]
    for c in preflight.get("characters") or []:
        nm = c.get("name") or ""
        lines.append(f"· {nm}：性格={c.get('personality') or '未知'}，"
                     f"外观={c.get('appearance') or '未知'}")
        emos = c.get("emotions") or []
        if emos:
            emo_str = "；".join(f"{e.get('emotion') or '' }({e.get('intensity') or 3})" for e in emos[:4])
            lines.append(f"  本章情绪基线：{emo_str}")
    if preflight.get("key_events"):
        lines.append("本章关键事件（台词/画面不得与这些事件冲突）："
                     + "；".join(preflight.get("key_events") or [])[:500])
    if preflight.get("character_mood_arc"):
        lines.append(f"本章情绪走向：{preflight.get('character_mood_arc')}")
    return "\n".join(lines)


def list_preflight(continuity_dir: str, project_key: str) -> list:
    """列出某项目所有已解析章节的索引（供前端判断哪些章已有前置解析）"""
    d = preflight_dir(continuity_dir, project_key)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        m = re.match(r"^第(\d+)章\.json$", fn)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)
