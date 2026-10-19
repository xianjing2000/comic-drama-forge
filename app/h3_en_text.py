# -*- coding: utf-8 -*-
"""H3 提示词「英文分镜转写」（2026-10-19，A 方案：对齐 H3信号10段测试001 模板）。

背景（格式审计结论）：模板是**全英文叙事**——中文只出现在人名与 `<d>` 台词里；
而本项目的 detailed_description / summary 把剧本中文原值（description / first_frame /
last_frame / motion / emotion）直接塞进英文句子，形成「英文骨架 + 中文原值」的杂句，
稀释 H3 的镜头控制力。

做法：构建视频提示词前，把这几个字段交给 LLM **按模板口吻译成英文**（人名/门派名
保留中文原文），结果按「源文本指纹」缓存；任何失败回落中文原文（与改动前逐字一致）。
开关：MJSCXT_H3_EN_TEXT（默认 1；=0 关闭）。

⚠️ 边界：只碰「提示词载体」，**不动剧本/字幕/配音**；`dialogue` 台词永远保持中文
（`<d>` 标签里的内容必须是中文原文）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: 需要英文化的字段（只碰这些画面描述类字段）
_FIELDS = (
    "description", "first_frame", "last_frame", "motion", "emotion",
    # ⭐ 2026-10-19（用户实测「人物说话把场景描述也说出来了」）：上面 5 个字段不够。
    #    h3_prompt_kit 还会把下面这些**中文字段**直接拼进英文句，H3 看到中文就
    #    当台词念 —— 这正是「说话时把场景描述念出来」的直接成因：
    #      · visual_detail → detailed_description（h3_prompt_kit:453）
    #      · location      → summary / detailed_description（:358、:1476、:1959）
    #      · action        → canon 硬事实块（:1663）
    #    一并纳入英文化，确保**除 <d> 台词与人名外提示词内无中文**。
    "visual_detail", "action", "location",
    # audio_cues：overall_soundscape 的原料。此前中文直拼会被 H3 念出来，
    # 于是 build_soundscape 只能写「designed diegetic sound effects…」这种空话，
    # 具体声音全丢。英文化后即可原样拼入，音景重新变得具体。
    "audio_cues",
)

_CJK_RE = re.compile(r"[一-鿿]")


def enabled() -> bool:
    return str(os.environ.get("MJSCXT_H3_EN_TEXT", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _cache_dir() -> str:
    try:
        from config import PROJECT_OUTPUT_DIR
        root = PROJECT_OUTPUT_DIR or "."
    except Exception:  # noqa: BLE001
        root = "."
    return os.path.join(root, "cache", "h3_en")


def _read_cache(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_cache(path: str, data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        logger.debug("[H3-EN] 缓存写入失败（忽略）：%s", e)


_TRANSLATE_PROMPT = (
    "你是漫剧分镜文案的英文译者。把下面 JSON 里的中文字段逐项翻译成**英文**，"
    "供 MiniMax H3 视频模型做分镜提示词。要求：\n"
    "- 只翻译画面事实，不增删信息、不解释、不改写剧情；\n"
    # ⚠️ 2026-10-19（用户实测「人物说话把场景描述也说出来了」的根因）：H3 遇到中文
    #    会当台词念出来。旧指令让「门派名、地名保留中文」→ 地名被念。故只保留人名，
    #    其余专有名词（门派/地名/建筑/环境）一律译成英文。
    "- **只有人名**保留中文原文（如 赵天霸、羡进）；门派名、地名、建筑名、环境名"
    "一律译成英文（如 青云宗→Qingyun Sect、外门广场→Outer Gate Plaza）；\n"
    "- 动作要具体可拍：谁做了什么、动作方向、做完之后画面是什么状态，用电影叙事口吻；\n"
    "- description 不超过 120 个英文单词；emotion 只译情绪词本身（如 cold and smug）；\n"
    "- audio_cues 译成英文拟声/环境音描述（如 noon cicadas buzzing, crowd murmuring）；\n"
    "- 空字符串字段原样输出空串。\n"
    "只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释：\n"
    # ⚠️ 下面这行 JSON 示例里有大括号 —— 调用方必须用 str.replace 而不是 .format()，
    #    否则 .format() 会把 {"description": ...} 当成占位符，直接 KeyError: '"description"'
    #    （这正是 2026-10-19 之前英文化 100% 失败、永远回落中文的真正原因）。
    '__SCHEMA__\n'
    "输入：\n__PAYLOAD__"
)

#: 输出骨架：由 _FIELDS 自动生成，避免示例字段与真实字段漂移
_SCHEMA_TEXT = "{" + ", ".join('"%s": "..."' % k for k in _FIELDS) + "}"


def _build_translate_prompt(payload: str) -> str:
    """组装翻译提示词（用 replace，不用 format —— 示例里有大括号）。"""
    return (_TRANSLATE_PROMPT
            .replace("__SCHEMA__", _SCHEMA_TEXT)
            .replace("__PAYLOAD__", payload))


def _translate(src: Dict[str, str]) -> Optional[Dict[str, str]]:
    """调 LLM 翻译；不可用/失败返回 None（调用方回落中文原文）。"""
    try:
        from shared_ai import _optional_llm_client
        client = _optional_llm_client()
        if client is None:
            return None
        payload = json.dumps(src, ensure_ascii=False, indent=1)
        txt = client.chat(
            [{"role": "user", "content": _build_translate_prompt(payload)}],
            temperature=0.2, max_tokens=1200)
        txt = str(txt or "").strip()
        # 容错：剥掉可能的代码块围栏，截取第一个 { 到最后一个 }
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
        a, b = txt.find("{"), txt.rfind("}")
        if a < 0 or b <= a:
            logger.warning("[H3-EN] 翻译返回非 JSON（忽略）：%s", txt[:120])
            return None
        d = json.loads(txt[a:b + 1])
        if not isinstance(d, dict):
            return None
        return {k: str(d.get(k) or "").strip() for k in _FIELDS}
    except Exception as e:  # noqa: BLE001
        logger.warning("[H3-EN] 翻译失败（用中文原文）：%s", e)
        return None


def englishize_shot(shot: dict) -> dict:
    """返回**英文化后的镜头副本**（fail-open：失败/关闭时原样返回）。

    · 只在任一字段含中文时才翻译（已英文化过的重跑直接跳过，零成本）；
    · 结果按源文本指纹缓存到 `output/cache/h3_en/<sha1>.json`；
    · 台词（dialogue）不参与翻译。
    """
    if not enabled() or not isinstance(shot, dict):
        return shot
    src = {k: str(shot.get(k) or "").strip() for k in _FIELDS}
    if not any(src.values()):
        return shot
    if not any(_CJK_RE.search(v) for v in src.values()):
        return shot                      # 全英文 → 无需翻译
    try:
        sig = hashlib.sha1(json.dumps(src, ensure_ascii=False, sort_keys=True)
                           .encode("utf-8")).hexdigest()
        path = os.path.join(_cache_dir(), sig + ".json")
        data = _read_cache(path)
        if data is None:
            data = _translate(src)
            if data is None:
                return shot
            _write_cache(path, data)
        out = dict(shot)
        for k in _FIELDS:
            v = str(data.get(k) or "").strip()
            if v:
                out[k] = v
        logger.info("[H3-EN] 分镜 %s 已英文化（描述 %d→%d 字符）",
                    shot.get("shot_id"), len(src.get("description") or ""),
                    len(str(out.get("description") or "")))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[H3-EN] 英文化异常（用中文原文）：%s", e)
        return shot

_TEXT_PROMPT = (
    # ⚠️ 2026-10-19（用户实测「人物说话把场景描述也说出来了」）：旧指令要求
    #    「地名保留中文原文」—— 但 H3 遇到中文会**当台词念出来**（实测念出
    #    「正午，青石广场…」）。故改为：**只有人名保留中文**（人名是身份标识，
    #    且与 <Subject N> 一致），门派名/地名/环境名一律译成英文。
    "把下面这段中文影视美术描述翻译成英文。规则："
    "① 人名保留中文原文（如 韩立、羡进）；"
    "② 门派名、地名、建筑名、环境名一律译成英文（如 青云宗→Qingyun Sect）；"
    "③ 不增删信息、不解释、不加评论。"
    "只输出译文本身，不要引号、不要前后缀、不要任何说明。\n"
    "原文：{text}"
    "原文：{text}"
)


def englishize_text(text: str, kind: str = "text") -> str:
    """通用短文本英文化（角色/场景外观等），按 (kind, 原文) 指纹缓存；fail-open。"""
    if not enabled():
        return text
    t = str(text or "").strip()
    if not t or not _CJK_RE.search(t):
        return t
    try:
        sig = hashlib.sha1(("%s|%s" % (kind, t)).encode("utf-8")).hexdigest()
        path = os.path.join(_cache_dir(), "%s_%s.json" % (kind, sig))
        data = _read_cache(path)
        if data is None:
            from shared_ai import _optional_llm_client
            client = _optional_llm_client()
            if client is None:
                return text
            out = str(client.chat(
                [{"role": "user", "content": _TEXT_PROMPT.format(text=t)}],
                temperature=0.2, max_tokens=400) or "").strip()
            out = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", out, flags=re.S).strip()
            if not out:
                return text
            data = {"text": out}
            _write_cache(path, data)
        return str((data or {}).get("text") or "").strip() or text
    except Exception as e:  # noqa: BLE001
        logger.debug("[H3-EN] 文本英文化失败（忽略）：%s", e)
        return text
