"""MiniMax H3 提示词构建 —— 单一事实源

背景（为什么要单独抽一个模块）
--------------------------------------------------------------------------
历史实现里 H3 视频提示词有**两个互相打架的来源**：

1. ``novel_to_script`` 让 LLM 直接写 ``shot["prompt_h3"]``，字段说明只是
   「英文画面描述（60 词以内）」。模型于是输出一句裸英文，例如::

       Aerial shot rising through thin mist onto a five-story pagoda courtyard,
       golden sunrise rays, symmetrical composition, cold blue sky with warm gold contrast

   它既没有 H3 规范要求的六段结构，也**完全不知道自己配了几张参考图**，
   更没有 <Picture N> 标签 —— 而 H3 走的是 Ref2VA（参考图转视频），
   没有标签的提示词等于让模型盲猜参考图用途，实测出片与设定严重不符。

2. ``comfyui_client._build_h3_prompt`` 才是懂参考图语义的构建器，但只在
   ``prompt_h3`` 为空时才被调用；而 (1) 几乎总会填上值，于是**结构化构建器
   形同虚设**。实测全项目 10 份剧本、200+ 镜头里结构化提示词数量为 **0**。

本模块把「H3 提示词长什么样」收敛成唯一实现，供以下三处复用：

- ``comfyui_client._build_h3_prompt``（生成期兜底/权威构建）
- ``app._resolve_h3_prompt``（生成期择优：合规即用，不合规则并入细节）
- ``script_prompt_analyzer``（LLM 侧的规格说明与结构校验）

规范依据：H3 官方提示词指南
- Ref2VA（有参考图）固定六段，顺序不可变：
  ``subject_definitions`` / ``summary`` / ``retention_analysis`` /
  ``detailed_description`` / ``overall_soundscape`` / ``non_diegetic_music``
- base 模式（T2VA / I2VA / FL2VA / L2VA）固定三段：
  ``integrated_multimodal_description`` / ``overall_soundscape`` / ``non_diegetic_music``
- 每个镜头行以 ``[Shot N] MM:SS.mmm`` 时间码开头（毫秒三位）
- 台词必须带语言标记，如 ``[Chinese] 台词原文``，说话人以 ``(S1)`` 标注
- 与画面无关的抽象词（cinematic / beautiful）尽量少用，改用具体视觉与听觉细节

正文语言（2026-09-24 二次对齐）
--------------------------------------------------------------------------
本地手跑模板 ``H3信号10段测试001.json`` 的**六段正文全部是英文散文**，
只有台词本体用 ``<d>[Chinese] 原文</d>`` 包裹。本模块此前把六段写成中文，
与模板不一致；实测 H3 对英文长句的理解与权重分配更稳，故正文统一改为英文产出。

中文输入（``description`` / ``emotion`` / ``audio_cues`` 等）**原样嵌进英文句子**
（模板同样把角色名、台词、符文名等中文原样保留，不做罗马字转写）；
只有「风格」走 :func:`style_kit.style_suffix_en` 给出确定性的英文风格短语。
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Ref2VA 六段（顺序即输出顺序，不可变更）
REF_SECTIONS: Tuple[str, ...] = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)

#: base 模式三段
BASE_SECTIONS: Tuple[str, ...] = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)

#: 单镜最长时长（超过则拆成多个时间码节拍，让时间轴与目标时长对齐）
BEAT_MAX_SEC = 6.0

#: 单段视频的**时长上限**（秒）。这是生成期的硬约束，与 :data:`BEAT_MAX_SEC`
#: （提示词节拍粒度）是两个不同层面的东西，不要混用。
#:
#: 为什么是 4.0：业界共识 —— AI 视频「前 4 秒可信、之后开始崩坏」（后段易出现
#: 肢体融化、面部漂移、背景跳变）。因此**单次生成**应限制在 4 秒以内，
#: 需要更长的镜头靠「同一提示词续写多段 + H3 原生无缝衔接」实现。
#: 实测本项目剧本 96~100% 的镜头超此线（第1集均 7.56s / 第2集均 6.91s），
#: 故生成期必须强制切段。
H3_SEGMENT_MAX_SEC = 4.0

#: 切段时允许的**最小段长**（秒）。切成比这更短的碎段没有意义：
#: 模型还没站稳就切下一段，反而引入更多接缝。故段数上限为
#: ``ceil(duration / H3_SEGMENT_MAX_SEC)`` 的基础上，把余数摊平而非留一个超短尾段。
H3_SEGMENT_MIN_SEC = 1.5

#: 无风格时的兜底（保持历史行为；有 style 时一律以 style 为准）
_DEFAULT_STYLE = "国漫3D渲染"

#: 提示词长度闸门（A-5）：H3 服务端对超长提示词**静默截断**，被砍掉的正是末尾的
#: ``overall_soundscape`` / ``non_diegetic_music`` 与后段动作节拍 —— 且日志里没有任何痕迹
#: （"关键词丢失"的典型形态）。这里在客户端先截断并告警，让丢失可见、可控。
MAX_PROMPT_CHARS = 6000
#: 单段"补充细节"（旧裸英文提示词 / merged detail）的长度闸门
MAX_DETAIL_CHARS = 800

#: 截断标记（计入闸门额度，保证输出严格不超过 limit）
_CLAMP_MARK = "…[截断]"

_PAREN_NOTE_RE = re.compile(r"[（(]\s*(旁白|画外音|音效|配乐|BGM|VO|OS)[^）)]*[）)]", re.IGNORECASE)


def _clamp(text: str, limit: int, label: str) -> str:
    """把 ``text`` 截到 ``limit`` 字符以内；超长时告警并以 ``…[截断]`` 收尾

    ⚠️ 标记本身也占额度（``text[:limit - len(mark)] + mark``），保证返回值**严格 ≤ limit**。
    若照字面写成 ``text[:limit] + mark``，6000 的闸门会漏出 6005 字符 —— 服务端照样再砍一刀，
    闸门就白设了。
    """
    if len(text) <= limit:
        return text
    logger.warning("提示词超长截断：%s %d→%d 字符（末尾以 …[截断] 标记）", label, len(text), limit)
    if limit <= len(_CLAMP_MARK):
        return text[:limit]
    return text[:limit - len(_CLAMP_MARK)] + _CLAMP_MARK


def clamp_prompt(text: str, label: str = "prompt_h3") -> str:
    """对外统一入口：把最终提示词截到 :data:`MAX_PROMPT_CHARS`

    任何产出最终 H3 提示词的路径都应过一道这里，避免绕过 :func:`resolve` 的裸返回
    （例如 ``comfyui_client.resolve_h3_prompt`` 里直接放行既有 ``prompt_h3`` 的分支）。

    ``label`` **只用于日志标签**（默认 ``prompt_h3``），不影响截断行为与返回值；
    所有 kind 共用本函数时传入各自标签（如 ``prompt_qc.audio``），避免超长日志里
    统一被误标成 H3（P-4，2026-09-22）。
    """
    return _clamp(str(text or ""), MAX_PROMPT_CHARS, label or "prompt_h3")


_SECTION_RE_CACHE: Optional[re.Pattern] = None


def _SECTION_RE() -> re.Pattern:
    """段名标签行正则（模块级缓存）：``^name[:：][ \\t]*$``。

    段名标签**独占一行**（``name:`` 后到行尾只有空白），这是 build_ref2va /
    build_base 的固定格式；用整行匹配避免 body 里的普通文本被误认成段名。
    """
    global _SECTION_RE_CACHE
    if _SECTION_RE_CACHE is None:
        names = "|".join(re.escape(n) for n in REF_SECTIONS + BASE_SECTIONS)
        _SECTION_RE_CACHE = re.compile(rf"(?m)^({names})[:：][ \t]*$")
    return _SECTION_RE_CACHE


def _section_spans(text: str):
    """把 H3 文本切成 ``[(段名, 标签(含换行), body起点, 下段起点), ...]``。

    识别不出 ≥2 个段标签（非结构化文本 / 结构残缺）时返回 ``None``，调用方退化。
    """
    ms = list(_SECTION_RE().finditer(text))
    if len(ms) < 2:
        return None
    spans = []
    for i, m in enumerate(ms):
        body_start = m.end() + 1 if (m.end() < len(text) and text[m.end()] == "\n") else m.end()
        next_start = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        spans.append((m.group(1), text[m.start():body_start], body_start, next_start))
    return spans


def _compress_structured(text: str, limit: int, label: str):
    """按段压缩：从 body 最长的段开始逐段压到恰好 ≤ limit，保段名与末尾段。

    只压缩各段**正文**（``body``），段名标签行原样保留 —— 这样 :func:`validate`
    仍能识别出全部段名（含末尾的 ``overall_soundscape`` / ``non_diegetic_music``），
    不会因截断把一条结构完整的提示词判成「缺段」。压缩后仍超限则返回 ``None``
    由调用方退化到旧的末尾截断。
    """
    spans = _section_spans(text)
    if not spans:
        return None
    segs = []
    for (name, lab, body_start, next_start) in spans:
        raw = text[body_start:next_start]
        body = raw.rstrip("\n")
        sep = raw[len(body):]
        segs.append([name, lab, body, sep])
    overflow = sum(len(l) + len(b) + len(s) for _, l, b, s in segs) - limit
    if overflow <= 0:
        return None
    order = sorted(range(len(segs)), key=lambda i: len(segs[i][2]), reverse=True)
    for i in order:
        if overflow <= 0:
            break
        name, lab, body, sep = segs[i]
        if not body:
            continue
        target = max(0, len(body) - overflow)
        if target == 0:
            new_b = ""
        else:
            new_b = _clamp(body, target, f"{label}.{name}")
        overflow -= len(body) - len(new_b)
        segs[i][2] = new_b
    if overflow > 0:
        return None
    return "".join(l + b + s for _, l, b, s in segs)


def clamp_h3_prompt(text: str, limit: int = MAX_PROMPT_CHARS, label: str = "h3") -> str:
    """结构感知截断（P-2，2026-09-22）

    普通 :func:`clamp_prompt` 从**末尾**截断；而 H3 规范把 ``overall_soundscape`` /
    ``non_diegetic_music`` 放在**末尾** —— 一旦超长，末尾截断会连带砍掉这两段，
    :func:`validate` 立刻报「结构不合规（缺段）」，把一条本来完整的提示词判废。

    这里改为：超长时优先压缩**贡献超长的段**（通常是塞了超长 description 的
    ``summary`` / ``detailed_description`` 等中段），**完整保留每个段的段名标签与
    末尾两段**；识别不出结构、或压缩到极致仍超限时，退化到旧的末尾截断（保底，不更糟）。
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    out = _compress_structured(text, limit, label)
    if out is not None and len(out) <= limit:
        return out
    return _clamp(text, limit, label)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def fmt_ts(sec: Any) -> str:
    """秒 → H3 时间码 ``MM:SS.mmm``（毫秒三位，与官方示例一致）"""
    try:
        s = float(sec or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s < 0:
        s = 0.0
    total_ms = int(round(s * 1000))
    minutes, rem_ms = divmod(total_ms, 60_000)
    seconds, ms = divmod(rem_ms, 1000)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def dialogue_lines(raw) -> List[Dict[str, str]]:
    """把任意形态的台词归一成 ``[{"speaker": 名, "text": 台词}]``（保持出现顺序）"""
    out: List[Dict[str, str]] = []
    if raw is None:
        return out
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            out.append({"speaker": "", "text": text})
        return out
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("line") or raw.get("dialogue")
                   or raw.get("content") or "").strip()
        speaker = str(raw.get("speaker") or raw.get("character") or raw.get("role")
                      or raw.get("name") or "").strip()
        if text:
            out.append({"speaker": speaker, "text": text})
        return out
    if isinstance(raw, (list, tuple)):
        for item in raw:
            out.extend(dialogue_lines(item))
    return out


def speaker_slots(lines: Sequence[Dict[str, str]]) -> Dict[str, str]:
    """按首次出场顺序给说话人分配 ``S1`` / ``S2`` …（同名复用同一槽位）"""
    slots: Dict[str, str] = {}
    for ln in lines:
        name = str(ln.get("speaker") or "").strip()
        if name and name not in slots:
            slots[name] = f"S{len(slots) + 1}"
    return slots


def _clean_sfx(text: str) -> str:
    """去掉音效串里的旁白/配乐标注，只留环境与动作音"""
    t = _PAREN_NOTE_RE.sub("", str(text or ""))
    t = re.sub(r"^(音效|配乐|BGM|环境音)\s*[:：]\s*", "", t.strip(), flags=re.IGNORECASE)
    for prefix in ("旁白", "画外音"):
        if t.startswith(prefix):
            t = t[len(prefix):].lstrip("：: ")
    return t.strip(" 。；;，,")


# --------------------------------------------------------------------------- #
# 声音 / 音乐段（H3 要求独立成段，不能并进画面描述）
# --------------------------------------------------------------------------- #

_DEFAULT_MUSIC_BY_EMOTION = {
    "紧张": "低频弦乐持续压迫，节奏渐紧",
    "期待": "弦乐缓慢上行，留白等待爆发",
    "庄重": "低沉鼓点与号角质感的铜管长音",
    "威严": "缓慢低音铜管铺底，重音落点明确",
    "悲伤": "单线条钢琴稀疏音符，尾音自然衰减",
    "温柔": "钢琴与弦乐弱奏，旋律舒缓下行",
    "愤怒": "短促打击乐与低音弦乐切分推进",
    "恐惧": "高频弦乐颤音与不谐和音程，渐强后骤停",
    "平静": "稀疏钢琴单音配持续低音垫，安静收尾",
}


def build_soundscape(shot: dict, scene_hint: str = "") -> str:
    """``overall_soundscape``：环境音 + 动作音 + 台词混响（画面内可闻）

    本地模板的写法是**把画面内所有可闻声音按时间顺序铺开**（英文散文），并在有台词时
    顺带描述「台词的音色与混响」（例如 ``Her spoken words reverberate cleanly
    against the obsidian platform, the echo of "娶我" lingering for a full second``）。
    这既补齐了配音的空间感，也**再次向模型确认「人物确实在说话」**——对驱动口型
    有正面作用。

    ⚠️ 历史实现结尾固定写「全程无解说、无旁白念白」，本意是防旁白，但副作用是
    在同一段里同时出现「台词」与「无念白」两种相反信号，模型可能因此把台词
    降级成画外音（口不动）。现改为只在**确实没有台词**时，用官方句式声明
    「no one speaks / no voice-over」。
    """
    loc = str(shot.get("location") or scene_hint or "").strip()
    sfx = _clean_sfx(shot.get("audio_cues") or "")
    emotion = str(shot.get("emotion") or "").strip()
    lines = dialogue_lines(shot.get("dialogue"))
    parts: List[str] = []
    if sfx:
        parts.append(sfx)
    if loc:
        parts.append(f"the ambient room tone of {loc}")
    elif emotion:
        parts.append("a quiet ambient room tone")
    if emotion:
        parts.append(f"breathing and fabric rustle matched to the 「{emotion}」 mood")
    if not parts:
        parts.append("a quiet ambient room tone, with breathing and fabric rustle clearly audible")
    body = ", ".join(parts).rstrip("。；;，,")
    if lines:
        # 2026-09-26：⚠️ 此前这里把台词前 12 字写成「The echo of "台词"」，导致台词
        # 内容在提示词里出现**两次**（detailed_description 的 <d>台词</d> + 此处 echo
        # 引用）—— H3 把两处都当台词念，实测「同一句台词被反复念」（用户反馈）。
        # 手跑模板虽然也写 echo，但只取台词末尾 2 字关键词、风险低；此处前 12 字几乎
        # 是完整台词、风险高。现彻底去掉台词引用，只保留「说话声混入环境音」的空间感
        # —— 台词本体只留在 detailed_description 的 <d> 标签里（单一事实来源）。
        where = loc or "the scene"
        tail = (f"The spoken voice carries clearly through {where}, synced to the lip "
                f"movement, and sits naturally on top of the ambient tone.")
        return f"{body}. {tail}"
    # ⚠️ 2026-10-08（用户反馈「一会中文一会英文」）：实测「No one speaks」这种温和声明挡不住
    #    H3 —— 它在无声段仍会自制语音，而提示词是英文，于是出来英文/含糊英语。
    #    改成**逐项硬性禁用**（说话 / 低语 / 气声 / 念白 / 画外音 全禁），只留环境音。
    return (f"{body}. This segment contains NO dialogue whatsoever: no speaking, no murmuring, "
            f"no whispering, no breathy vocalisation, no voice-over and no narration — "
            f"only the ambient sound described above, with no human voice at all.")


def build_music(shot: dict, style: str = "") -> str:
    """``non_diegetic_music``：画面外配乐（角色听不到）

    本地模板 10 段里 **9 段直接写 ``N/A``**（只有 1 段写了说明性 N/A）—— 即
    默认**不生成**画外配乐，配乐由后期另行处理。故本函数默认也返回 ``N/A``：

    - 显式关闭（``music: false`` / ``"false"`` / ``"无"`` 等）→ ``N/A``
    - 调用方给了**具体配乐描述** → 尊重它，并包成英文句
    - 其余情况（含情绪已知）→ 跟随模板写 ``N/A``

    ⚠️ 历史实现会按情绪**凭空生成**一段配乐描述，与模板「默认 N/A」不一致。
    ``N/A`` 段名仍在（:func:`validate` 靠段名判合规），六段结构不受影响。

    （``style`` 形参当前未参与生成，保留以免破坏调用方签名。）
    """
    raw_flag = shot.get("non_diegetic_music")
    if raw_flag is None:
        raw_flag = shot.get("music")
    # 显式关闭配乐：布尔 False / 字符串 "false"/"none"/"n/a"/"无" 一律视为 N/A
    if raw_flag is not None and not isinstance(raw_flag, str):
        if raw_flag is False:
            return "N/A"
    if isinstance(raw_flag, str) and raw_flag.strip().lower() in (
            "false", "none", "n/a", "na", "no", "off", "无", "不要", "不需要"):
        return "N/A"
    if isinstance(raw_flag, str) and raw_flag.strip() and raw_flag.strip() not in ("true", "yes", "on"):
        # 调用方直接给了配乐描述 → 尊重它（包成英文句，与模板语言一致）
        return (f"Non-diegetic score: {_strip_end(raw_flag)}; it stays strictly outside "
                f"the frame, with no sung vocals.")
    # 默认跟随模板：本段不生成音乐，配乐交由后期处理
    return "N/A"


# --------------------------------------------------------------------------- #
# 画面时间轴
# --------------------------------------------------------------------------- #

def _beats(shot: dict, duration: float) -> List[Tuple[float, float, str]]:
    """把单镜拆成时间轴节拍 ``[(起始秒, 时长, 描述)]``

    时长 ≤ ``BEAT_MAX_SEC`` 时只有一个节拍；更长时按 2~3 个节拍铺满，
    避免「描述只有一瞬、视频却要演 12 秒」的空转。

    ⚠️ 节拍描述以**英文**产出（对齐本地模板）。``description`` / ``visual_detail``
    里的中文原文按模板惯例**原样保留**（模板同样把中文台词、角色名嵌在英文句里），
    只把「衔接语」写成英文。
    """
    desc = str(shot.get("description") or "").strip()
    # narration 是**旧剧本遗留字段**（本系统自 2026-09-19 起剧本阶段不再产出旁白）。
    # 保留读取只为兼容改造前生成的项目、让它们重出视频时不至于丢掉画面里的情绪衔接；
    # 新剧本这里恒为空串，下方「画外音延续」分支不会触发。
    narration = str(shot.get("narration") or "").strip()
    detail = str(shot.get("visual_detail") or "").strip()
    # ⚠️ 模型可能把同一条细节同时写进 description 与 visual_detail（两个字段本就允许重叠），
    # 直接 join 会让同一句细节在视频提示词里出现两遍 → 重复的那份丢掉。
    if detail and detail in desc:
        detail = ""
    # ⚠️ 每段各自去掉句末标点再 join：LLM 常在 description 末尾带「。」，
    # 直接拼会产出「。，」连排（视频模型会把它当断句，浪费提示词预算）。
    body = ", ".join(x for x in (_strip_end(desc), _strip_end(detail)) if x)
    if not body:
        body = _strip_end(str(shot.get("prompt_h3") or "")) or \
            "the framing continues from the previous shot with the same composition and lighting"

    dur = max(1.0, float(duration or 4.0))
    if dur <= BEAT_MAX_SEC:
        return [(0.0, dur, body)]

    n = 2 if dur <= BEAT_MAX_SEC * 2 else 3
    span = dur / n
    beats: List[Tuple[float, float, str]] = []
    for i in range(n):
        start = i * span
        if i == 0:
            text = body
        elif i == n - 1:
            # 本地模板的收尾写法是「动作推进 → 说话 → 收束」，最后一拍必须
            # **为台词留出明确的开口时机**（「and then speaks」），否则 H3 会把
            # 台词当成背景旁白、人物嘴唇不动。
            text = ("the movement settles and the posture stabilises, with the framing "
                    "cutting cleanly from the previous moment")
            # 审计 P2-18（2026-09-29）：不再把旁白原文拼进提示词。「成片不产出旁白」是
            # 产品决策（DEPRECATED_SHOT_FIELDS 显式拒绝 narration），且这里一旦有人
            # 调大 H3_SEGMENT_MAX_SEC 越过 BEAT_MAX_SEC，旧剧本的旁白会借这条分支复活。
        else:
            text = ("the action continues to advance, keeping the characters' appearance, "
                    "wardrobe and scene lighting exactly consistent with the previous moment")
        beats.append((start, span, text))
    return beats


def _strip_end(text: str) -> str:
    """去掉句末标点，便于重新拼接（避免出现「。。」）"""
    return str(text or "").strip().rstrip("。．.；;，,！!？?")


#: 说话动作的前导语（按性别/未知各一句）。这段**必须显式描述「开口说话」的
#: 物理动作**，否则 H3 会把台词只当成音轨去配音、画面里人物嘴唇不动（实测现象）。
#: 本地手跑模板（H3信号10段测试001.json）里每句台词前都有
#: ``speaks — a clear, resonant female voice with ... at a measured declarative rate``，
#: 那正是模型据此驱动口型的依据，不能省。
_SPEAK_LEAD = {
    "male": "a clear male voice with a measured, controlled delivery",
    "female": "a clear female voice with a measured, controlled delivery",
    "": "a clear voice with a measured, controlled delivery",
}

#: 每句台词后的收口语（官方示例：``She closes her lips after the final word.``）。
#: 有了它模型才知道「这句说完了、可以闭嘴」，否则会在段内继续自造语音。
_SPEAK_CLOSE = {
    "male": "He closes his lips after the final word.",
    "female": "She closes her lips after the final word.",
    "": "The lips close after the final word.",
}


def _guess_voice(form: str) -> str:
    """从说话人署名猜音色性别键（女帝/娘… → female；男/爷… → male；判不出返回 ``""``）"""
    t = str(form or "")
    if re.search(r"女|娘|妃|后|母|妹|姐|婆|妈", t):
        return "female"
    if re.search(r"男|爷|父|兄|弟|帝|王|公|叔|伯", t):
        return "male"
    return ""


def _spoken_clause(lines: Sequence[Dict[str, str]], slots: Dict[str, str],
                   speak_lead: str = "",
                   subject_labels: Optional[Dict[str, str]] = None,
                   audio_labels: Optional[Dict[str, str]] = None,
                   voice_descs: Optional[Dict[str, str]] = None) -> str:
    """把「谁在说话」渲染成 I2V 提示词的**对白句**（本提示词里台词文本的唯一载体）。

    ⚠️⭐ 2026-10-08 修复（用户实测反馈「配音含糊不清」，且手跑 H3 测试声音正常）：
    **必须写台词原文**，格式按官方规范 —— ``<d>[Chinese] 原文</d>``（见本文件头部
    第 34/40 行）。这正是用户手跑 H3 时声音正常的写法。

    这是一次**自己造成的回归**，因果链：
      · 2026-09-26：``build_soundscape`` 里的台词 echo 被移除，注释写明「台词本体
        只留在 detailed_description 的 <d> 标签里（单一事实来源）」——
        「同一句被念两遍」在此时已经修好；
      · 2026-10-05：本函数又把 ``<d>`` 也删掉（沿用「两个载体」时代的理由），
        于是唯一载体也没了 → **零载体** → H3 只知道「有人在说话」，不知道说什么，
        只能含糊咕哝。

    ⚠️ 因此本处是全提示词中**台词文本的唯一出现位置**，绝不可在 ``build_soundscape``
    或任何其它段落再重复一次（那才会重新触发「同一句被念两遍」）。
    """
    spoken: List[str] = []
    for ln in lines:
        text = str(ln.get("text") or "").strip()
        if not text:
            continue
        speaker = str(ln.get("speaker") or "").strip()
        slot = slots.get(speaker, "")
        who = f"({slot}) " if slot else ""
        lead = speak_lead or _SPEAK_LEAD.get(_guess_voice(speaker), "") or _SPEAK_LEAD[""]
        # ⭐ 2026-10-09 官方格式对齐（MiniMax-H3 技能 references/ref-en.txt §5.4）：
        #   <Subject N> (Sx) says in {音色/语速}, using the voice timbre referenced from
        #   <Audio N>, <d>[Chinese] 原文</d> {收口句}
        # ⚠️ 2026-10-08 我曾把 speaks/says 这类英文动词删掉，以为它诱导 H3 念英文；
        #    对照官方指南后确认**删错了** —— 官方示例正是这种写法。真正的病因是
        #    `<Audio N>` 音色参考**从未声明进六段**（H3 不知道参考音频是干什么的）。
        _vd = (voice_descs or {}).get(speaker, "") or lead
        _al = (audio_labels or {}).get(speaker, "")
        _sl = (subject_labels or {}).get(speaker, "")
        _head = (f"{_sl} " if _sl else "") + (f"({slot}) " if slot else "")
        _mid = f"says in {_vd}"
        _mid += (f", using the voice timbre referenced from {_al}," if _al else ",")
        _close = _SPEAK_CLOSE.get(_guess_voice(speaker), _SPEAK_CLOSE[""])
        # ⭐ 2026-10-08 修复：写出台词原文（官方格式 `<d>[Chinese] 原文</d>`）。
        #    本处是整条提示词里台词文本的**唯一出现位置** —— 别处再写一次就会
        #    重新触发「同一句被念两遍」。
        # ⚠️ 2026-10-08（用户反馈「一会中文一会英文」）：去掉英文口语动词。
        #    旧写法 "speaks — a clear voice at a measured spoken rate (S1) says <d>…</d>,
        #    with the lips moving naturally in sync with the spoken words" 把英文的
        #    speaks/says/voice 直接贴在台词标签旁，H3 会把英文一并念出来 → 中英混杂。
        #    现在只保留「说话人槽位 + <d> 中文台词</d>」，唇形信号统一挪到句尾一句里。
        spoken.append(f"{_head}{_mid} <d>[Chinese] {text}</d> {_close}")
    if not spoken:
        return ""
    # 收口语已逐句写在台词后面（官方格式），这里不再追加统一尾句。
    return " ".join(spoken)


#: 景别中文 → 英文（对齐模板：``A full shot`` / ``a medium close-up`` …）。
#: 模板把景别写成**英文名词短语**，历史实现直接嵌中文「中景：」，与模板不一致。
_CAMERA_EN = {
    "大远景": "A wide establishing shot",
    "远景": "A wide shot",
    "全景": "A full shot",
    "大全景": "A very wide shot",
    "中景": "A medium shot",
    "中近景": "A medium close-up",
    "近景": "A close-up",
    "特写": "A close-up",
    "大特写": "An extreme close-up",
    "微距": "A macro close-up",
    # 局部（插入镜）：只拍手部/道具/身体局部，不出现完整人脸。参考片 92 镜拆解里
    # 「局部」是主力景别（第 8/16/19/25/28/31/35/37/38/42/50/65/67/72/76/78/88/90/92 镜），
    # 旧口径下表达不出来。取值与 config.SHOT_TYPES / SHOT_CAMERA_SPECS 对齐（本模块
    # 刻意零项目依赖、保持纯函数，故此处是**镜像表**，由 verify_shot_type_registry.py 守卫对齐）。
    "局部": "A detail shot",
    # ---- 「运镜在前」的复合词（术语表里 real 存在，如 handheld close-up）----
    # ⚠️ 必须放进来：:func:`_camera_en` 是**子串**匹配，``手持跟拍`` 里没有景别词，
    #    不加这几条就会返回空串 → 景别整段丢失（实测 ``手持跟拍`` 曾翻成 ``''``）。
    #    注意表内匹配按**长词优先**遍历，``手持特写`` 会比 ``特写`` 先命中。
    "手持特写": "A handheld close-up",
    "手持近景": "A handheld close-up",
    "手持中景": "A handheld medium shot",
    "手持全景": "A handheld full shot",
    "手持远景": "A handheld wide shot",
    "手持跟拍": "A handheld tracking shot",
    "手持": "A handheld shot",
    "环绕特写": "An orbiting close-up",
    "环绕近景": "An orbiting close-up",
    "环绕中景": "An orbiting medium shot",
    "定格特写": "A frozen close-up",
    "定格中景": "A frozen medium shot",
}


def _camera_en(camera: str) -> str:
    """景别 → 英文短语；认不出时返回空串（交由调用方退化为无景别写法，不瞎猜）"""
    t = str(camera or "").strip()
    if not t:
        return ""
    if t in _CAMERA_EN:
        return _CAMERA_EN[t]
    # 子串兜底：「中近景仰拍」这类带修饰的写法也能命中
    for zh in sorted(_CAMERA_EN, key=len, reverse=True):
        if zh in t:
            return _CAMERA_EN[zh]
    # 已经是英文（调用方直接给了英文景别）→ 原样用
    if re.match(r"^[A-Za-z]", t):
        return t
    return ""


#: 运镜中文 → 英文短语（对齐 H3 模板的运镜写法：``with a slow dolly-in`` /
#: ``the camera tracks the subject`` …）。
#:
#: ⚠️ 为什么必须单独做一张表（历史缺陷）：剧本质检第 13 条**强制要求**
#: ``camera`` 字段写成「景别+运镜」（如 ``中景跟拍`` / ``特写推入``），真实剧本里
#: 组合词占绝大多数（实测第 2 集 29 镜出现 16 种组合）。但 :func:`_camera_en`
#: 只做**景别前缀子串匹配**，运镜部分被静默吞掉：
#:
#:     '中景跟拍' → 'A medium shot'    （「跟拍」丢失）
#:     '特写推入' → 'A close-up'        （「推入」丢失）
#:     '全景升降' → 'A full shot'       （「升降」丢失）
#:
#: 结果是：剧本侧生产的运镜指令**一个都进不了提示词**，模型只能自行猜运镜，
#: 出片运镜随机。这张表把运镜补回提示词，与景别一起构成完整镜头语言。
_CAMERA_MOVE_EN = {
    "固定": "the camera stays locked off on a fixed tripod",
    "定格": "the frame freezes on a held moment",
    "推镜": "with a steady dolly-in toward the subject",
    "推入": "with a slow dolly-in toward the subject",
    "急推": "with a fast, forceful dolly-in on the subject",
    "推进": "with a gradual dolly-in toward the subject",
    "拉镜": "with a steady dolly-out away from the subject",
    "拉远": "with a slow dolly-out away from the subject",
    "拉": "with a slow dolly-out away from the subject",
    "摇镜": "with a smooth horizontal pan across the scene",
    "摇拍": "with a smooth horizontal pan across the scene",
    "移镜": "with a lateral tracking move alongside the subject",
    "平移": "with a lateral tracking move alongside the subject",
    "跟镜": "the camera tracks the subject and keeps pace with the movement",
    "跟拍": "the camera tracks the subject and keeps pace with the movement",
    "升降": "with a vertical crane move through the space",
    "升": "with a rising crane move upward",
    "降": "with a descending crane move downward",
    "环绕": "with a slow orbiting move around the subject",
    "旋转": "with a slow orbiting move around the subject",
    "变焦": "with a slow zoom that tightens the framing",
    "手持": "with a subtle handheld shake that keeps the frame alive",
    "俯冲": "with a fast downward push into the scene",
    "仰冲": "with a fast upward push into the scene",
    # ---- 强度/方式变体（参考片 92 镜拆解的真实写法：轻推/轻摇/跟随/轻手持）----
    # ⚠️ 为什么必须逐条列出：词典是「子串命中」，但命中的必须是**词表里的整颗词**。
    #    旧词表里没有「轻推」「轻摇」「跟随」—— 于是参考片里占非固定运镜绝大多数的
    #    这三个词**整段翻译失败**（返回空串），提示词里运镜一个字母都没有，模型只能
    #    自行猜运动（「出片运镜随机」的直接原因）。此处补全 + 下方 _MOVE_CORE 兜底。
    "跟随": "the camera tracks the subject and keeps pace with the movement",
    "跟移": "the camera tracks the subject and keeps pace with the movement",
    "轻推": "with a gentle dolly-in toward the subject",
    "缓推": "with a slow dolly-in toward the subject",
    "慢推": "with a slow dolly-in toward the subject",
    "推近": "with a slow dolly-in toward the subject",
    "轻摇": "with a gentle horizontal pan across the scene",
    "缓摇": "with a slow horizontal pan across the scene",
    "慢摇": "with a slow horizontal pan across the scene",
    "上摇": "with a smooth upward tilt across the scene",
    "下摇": "with a smooth downward tilt across the scene",
    "甩镜": "with a fast whip pan across the scene",
    "轻移": "with a gentle lateral tracking move alongside the subject",
    "横移": "with a lateral tracking move alongside the subject",
    "左移": "with a lateral tracking move to the left",
    "右移": "with a lateral tracking move to the right",
    "拉出": "with a slow dolly-out away from the subject",
    # ---- 「固定/定格」的口语变体与剪辑转场词（2026-09-29 按 106 份真实剧本统计补）----
    # 实测「定拍 / 静止 / 静态 / 定帧 / 快切 / 叠化」共 30+ 个镜头：不补这些词，
    # 它们与「轻推 / 跟随」一样**整段丢运镜**（返回空串）→ 模型自行猜运动。
    # 注意分工：**术语表（continuity.CAMERA_TERMS）只放规范词**给模型取词，
    # 词典在这里**宽容收编**真实写法 —— 两端职责不同，不要为了收编而放宽术语表。
    # 「快切 / 叠化 / 硬切」是**剪辑层**转场：单段生成里表达不了「切」，本段一律锁死
    # 机位并显式声明段内不动（切点由成片剪辑完成）。
    "定拍": "the camera stays locked off on a fixed tripod",
    "静止": "the camera stays locked off on a fixed tripod",
    "静态": "the camera stays locked off on a fixed tripod",
    "定帧": "the frame freezes on a held moment",
    "快切": ("the camera stays locked off on a fixed tripod, with no camera "
             "move inside this segment"),
    "叠化": ("the camera stays locked off on a fixed tripod, with no camera "
             "move inside this segment"),
    "硬切": ("the camera stays locked off on a fixed tripod, with no camera "
             "move inside this segment"),
    "切镜": ("the camera stays locked off on a fixed tripod, with no camera "
             "move inside this segment"),
    # ---- 机位/角度（与景别正交，qc_client 剧本 QC 明确要求「机位与景别正交」）----
    "俯拍": "shot from a high angle looking down on the subject",
    "俯视": "shot from a high angle looking down on the subject",
    "仰拍": "shot from a low angle looking up at the subject",
    "仰视": "shot from a low angle looking up at the subject",
    "平视": "shot at the subject's eye level",
    "斜角": "shot from a Dutch-tilted angle",
    "侧面": "shot from the side of the subject",
    "侧拍": "shot from the side of the subject",
    "背拍": "shot from behind the subject",
    "过肩": "shot over the subject's shoulder",
    "主观": "shot as the character's point of view",
}


#: 机位/角度类词条（与「运镜」正交，二者可同时出现）。
#:
#: ⚠️ 必须与运动类词条**分开检索**：旧实现把两类词放在同一张表里做「最长词优先」
#: 子串匹配，「俯拍缓推」会先命中「俯拍」（机位）→ 真正的运镜「缓推」被整段吞掉，
#: 提示词里只剩「高角度俯视」，镜头**怎么动一个字都没有**。
_CAMERA_ANGLE_KEYS = ("俯拍", "俯视", "仰拍", "仰视", "平视", "斜角", "侧面", "侧拍",
                      "背拍", "过肩", "主观")

#: 运镜**强度/方式**修饰词 → 英文副词（长词优先）。
#: 真实剧本与参考片里「轻推 / 轻摇 / 缓推 / 慢摇 / 急推 / 轻手持」这类带程度的写法
#: 占非固定运镜的多数，而旧词典只认不带修饰的规范词（推入 / 摇镜 / 跟拍）。
_MOVE_INTENSITY = (
    ("轻轻", "very gently"),
    ("缓缓", "slowly"),
    ("徐徐", "slowly"),
    ("轻", "gently"),
    ("缓", "slowly"),
    ("慢", "slowly"),
    ("急", "quickly"),
    ("猛", "forcefully"),
    ("快", "quickly"),
)

#: 运镜**核心动作**兜底表 (核心词, 默认副词, 英文模板)；模板里的 {how} 由
#: :data:`_MOVE_INTENSITY` 命中值或默认副词填充。
#:
#: 为什么要有这一层：词典永远追不上真实写法（「缓缓下摇」「轻微横移」「推近」…）。
#: **认得出核心动作就绝不丢运镜**；只有连核心动作都认不出才告警 ——
#: 「静默丢运镜 → 模型自行猜 → 出片运镜随机」是历史根因，丢失必须可见。
_MOVE_CORE = (
    ("推", "steady", "with a {how} dolly-in toward the subject"),
    ("拉", "steady", "with a {how} dolly-out away from the subject"),
    ("摇", "smooth", "with a {how} horizontal pan across the scene"),
    ("移", "steady", "with a {how} lateral tracking move alongside the subject"),
    ("跟", "", "the camera tracks the subject and keeps pace with the movement"),
    ("升降", "", "with a vertical crane move through the space"),
    ("环绕", "", "with a slow orbiting move around the subject"),
    ("旋转", "", "with a slow orbiting move around the subject"),
    ("变焦", "", "with a slow zoom that tightens the framing"),
    ("手持", "", "with a subtle handheld shake that keeps the frame alive"),
    ("升", "", "with a rising crane move upward"),
    ("降", "", "with a descending crane move downward"),
)

#: 只表示「取景 / 机位 / 空镜 / 转场」而**不含运动含义**的词。
#: 仅用于回答「认不出运镜时要不要告警」：整串都是这类词 → 本镜就是没有运镜，
#: 静默返回空串（不瞎猜、也不刷日志）；还有剩余成分才说明是**没认出来的运镜写法**，
#: 那才是必须可见的丢失。
_FRAMING_ONLY_WORDS = tuple(_CAMERA_EN) + ("中近景", "局部", "空镜", "全黑", "转场",
                                           "黑场", "微距", "大特写",
                                           "广角", "广景", "超广角", "大全景")

#: 段间运镜延续声明。同一镜头被切成多段时，只有**首段**重新声明运镜起手；
#: 其余段必须显式写成「同一运镜继续」，否则 H3 会在每一段重新起步 ——
#: 观感就是「推一半跳回起点再推」「跟一半跳回起点再跟」。
_MOVE_CONTINUE_EN = ("continuing the same camera move from the previous moment — "
                     "carry it forward from where it left off; do not restart, reset "
                     "or cut the camera move")


def _move_core_en(camera: str) -> str:
    """核心动作兜底：强度副词 + 核心动作 拼出运镜从句；认不出返回空串。"""
    t = str(camera or "").strip()
    if not t:
        return ""
    how = ""
    for zh, adv in sorted(_MOVE_INTENSITY, key=lambda x: len(x[0]), reverse=True):
        if zh in t:
            how = adv
            break
    for zh, default_how, tpl in sorted(_MOVE_CORE, key=lambda x: len(x[0]), reverse=True):
        if zh in t:
            return tpl.format(how=how or default_how)
    return ""


def _camera_move_en(camera: str, continuation: bool = False) -> str:
    """运镜 → 英文从句；认不出返回空串（宁可不说，也不瞎猜运镜）

    三层匹配，逐层兜底：

    ① **运动词表**（最长词优先，**排除机位词**）—— 历史取值逐字不变；
    ② **核心动作兜底**（推/拉/摇/移/跟/升降/环绕/变焦/手持 + 强度副词）——
       「缓缓下摇」「轻微横移」「推近」这类词典追不上的写法也能落到运镜；
    ③ **机位词并行追加**（俯拍/仰拍/过肩…）—— 与运动**正交**，不再互相吞掉。

    continuation=True：本段不是镜头的第一段 → 追加「同一运镜继续、不得重启」
    声明（见 :data:`_MOVE_CONTINUE_EN`），避免每 4 秒把运镜从头再来一遍。

    ⚠️ 匹配顺序：**长词优先**。「升降」必须比「升」/「降」先命中，否则
    「全景升降」会被「升」抢先翻成「rising crane move」而丢掉「降」。
    同样「推入」/「推进」要先于「推镜」之外的单字「推」判定。

    ⚠️ 三层全落空时**必须打 warning**：静默丢弃运镜会让模型自行猜运动 ——
    这是「出片运镜随机」的历史根因，丢失必须在日志里可见。
    """
    t = str(camera or "").strip()
    if not t:
        return ""
    _angles = set(_CAMERA_ANGLE_KEYS)
    # ① 运动词表（最长词优先；机位词留给第 ③ 层，二者互不吞并）
    move = ""
    for zh in sorted((k for k in _CAMERA_MOVE_EN if k not in _angles),
                     key=len, reverse=True):
        if zh in t:
            move = _CAMERA_MOVE_EN[zh]
            break
    # ② 核心动作兜底（「轻推近」「缓缓下摇」这类词典外的写法）
    if not move:
        move = _move_core_en(t)
    # ③ 机位并行追加（与运动正交：俯拍缓推 = 高角度 + 缓推，两条都留下）
    angle = ""
    for zh in sorted(_angles, key=len, reverse=True):
        if zh in t:
            angle = _CAMERA_MOVE_EN[zh]
            break
    parts = [p for p in (move, angle) if p]
    if not parts:
        # 纯景别/机位（中景、近景、俯拍、空镜…）本来就没有运镜 → 静默返回空串。
        # 只有「剥掉景别与机位词后仍有剩余」才说明这是一条**没认出来的运镜写法**，
        # 那种丢失必须告警（模型会自行猜运动）。
        _residual = t
        for _zh in sorted(_FRAMING_ONLY_WORDS, key=len, reverse=True):
            _residual = _residual.replace(_zh, "")
        for _zh in sorted(_angles, key=len, reverse=True):
            _residual = _residual.replace(_zh, "")
        if _residual.strip(" /·、,，。+&"):
            logger.warning(
                "运镜无法翻译（模型只能自行猜运动，出片运镜会随机）：%r —— "
                "请在 h3_prompt_kit._CAMERA_MOVE_EN / _MOVE_CORE 补该写法，"
                "或让剧本改用术语表内的运镜词", t)
        return ""
    clause = ", ".join(parts)
    if continuation:
        clause = f"{clause} ({_MOVE_CONTINUE_EN})"
    return clause


def _mid_sentence(clause: str) -> str:
    """把句首大写压成小写（用于「At 00:06.000, the camera cuts to a medium shot」这种句中位置）

    ⚠️ 不处理 ``An extreme close-up`` 这类**首字母即元音 A** 的写法：直接小写会得到
    「a extreme」，需要用 ``an``。这里一并纠正冠词。
    """
    s = str(clause or "").strip()
    if not s:
        return ""
    if s.startswith("An "):
        return "an " + s[3:]
    if s.startswith("A "):
        return "a " + s[2:]
    return s[0].lower() + s[1:] if s[:1].isupper() else s


def _style_opening(style: str, has_dialogue: bool = False) -> str:
    """``detailed_description`` 的首句风格声明（对齐模板 ``The target video uses …``）

    模板首句是 ``The target video uses a Chinese xianxia cultivation drama style with
    cool moonlit silver-and-teal palette, soft frontal moonlight and a shallow depth of
    field …``。这里用 :func:`style_kit.style_suffix_en` 把中文风格串翻成确定性的
    英文短语，再套上模板句式。

    ⚠️ 风格已在此处（段**首**）声明，故 :func:`build_detailed_description` 不再于
    段尾追加中文「全片画面风格统一为…」—— 同一风格声明两遍会放大其权重。
    """
    from style_kit import style_suffix_en  # 延迟导入：避免模块级循环依赖

    suffix = style_suffix_en(style, with_tail=False) if style else ""
    body = suffix[len("Style: "):].strip() if suffix.startswith("Style: ") else suffix
    if not body:
        return ("The target video keeps a consistent visual style across the whole "
                "segment, with delicate lighting and stable composition.")
    # ⭐ 无台词镜头不再说「speaking faces」（2026-10-04）：旧实现无论有无台词都写
    #   「keeps the speaking faces as the sharp focal plane」——对无台词镜头是无效且
    #   误导的约束（提示词凭空出现 speaking / faces）。
    # ⭐ 去重尾部 "style"（2026-10-04）：style_suffix_en("国漫3D渲染") 产出
    #   「Chinese animated style」，旧句再拼一个 " style" → "Chinese animated style style"
    #   （实测出现在导出工作流的每一段提示词里，属确定性可判缺陷）。
    _style_word = re.sub(r"[\s,]*style\s*$", "", body, flags=re.IGNORECASE).strip(" ,") or body
    _focal = ("keeps the speaking faces as the sharp focal plane"
              if has_dialogue else
              "keeps the on-screen subjects as the sharp focal plane")
    return (f"The target video uses a {_style_word} style, with a shallow depth of field "
            f"that {_focal} while the background falls into soft bokeh.")


def build_detailed_description(shot: dict, duration: float, style: str = "",
                               picture_refs: Optional[Dict[str, str]] = None,
                               end_frame_ref: str = "",
                               storyboard_ref_label: str = "",
                               subject_labels: Optional[Dict[str, str]] = None,
                               audio_labels: Optional[Dict[str, str]] = None,
                               voice_descs: Optional[Dict[str, str]] = None,
                               slots_override: Optional[Dict[str, str]] = None) -> str:
    """``detailed_description``：按 ``[Shot N]`` 逐节拍写画面（本地模板同格式）

    与本地手跑模板（``H3信号10段测试001.json``）对齐的要点：

    - **首句写全片风格**（``The target video uses … style … bokeh.``），景别用英文短语。
    - 首镜用 ``[Shot 1]``，**后续镜用 ``At MM:SS.mmm, the camera cuts to ...``** 起头，
      时间码内嵌在句子里（不再是「[Shot N] 00:03.500 中景：」）。
    - 无台词的镜头必须**显式写「no one speaks / no voice-over」** —— 留空会让模型
      自行「补台词」或把画面描述念成画外音；模板里每个无台词节拍都明确标注。
    - 结尾**不再追加**「画面中严禁出现任何文字、字幕…」这类中文禁令：模板里没有，
      而且它本身就在提示词里引入了「字幕/文字」这两个词，反而更容易诱发字幕。
    - **运镜必须显式写进画面句**（2026-09-25 补）：``camera`` 字段是「景别+运镜」复合词
      （如 ``中景跟拍`` / ``特写推入`` / ``全景升降``），旧实现只翻景别前缀、把运镜整段丢掉，
      导致模型自行猜运镜、出片运镜随机。现在运镜**写进句首的镜头声明**（与景别同处），
      既补回了信息，也符合模板「镜号后先写景别/机位、再写画面内容」的写法。
    - **P0-1 首帧/运动/末帧三段锚点**（借鉴 ViMax / CineGen，2026-09-28）：``first_frame``
      作 ``Opening frame`` 前置声明、``motion`` 显式区分「摄影机运动 vs 画面内运动」、
      ``last_frame`` 作文字末态兜底（**仅在无 ``end_frame_ref`` 尾帧图时写入**，有图时图已锚定
      末态、不叠文字避免图文打架）。旧剧本三字段缺失 → 各段整块不出现，零回归。
    - **构图基准改用 ``storyboard_ref_label`` 显式指定**（2026-09-30）：旧实现硬判
      「``<Picture 1>`` 在不在 picture_refs 里」，等于把「分镜图 = 第 1 张参考图」
      这个**隐含同序约定**写死在提示词侧 —— 一旦参考图序列前面多出公共参考图
      （H3 Director 公共参数，分镜图会落到 ``<Picture K+1>``），构图基准就会指错图。
      现在由调用方（``comfyui_client``，它知道 picture_defs 的实际顺序）把分镜图的
      真实标签传进来；**不传时回退旧判据**，零回归。
    """
    camera = str(shot.get("camera") or "中景").strip()
    # A1：景别/运镜优先读权威字段（新剧本 shot_type / camera_motion）；旧剧本两字段为空 →
    # 回退整个 camera 复合串，行为与改动前逐字一致（零回归）。
    _st = str(shot.get("shot_type") or "").strip()
    _mo = str(shot.get("camera_motion") or "").strip()
    # 段间运镜延续（segment_shot 置位）：非首段必须声明「同一运镜继续」，
    # 否则 H3 会在每 4 秒的子段重新起步 —— 观感是「推一半跳回起点再推」。
    _seg_continue = bool(shot.get("_seg_continue"))
    camera_en = _camera_en(_st or camera)
    camera_move = _camera_move_en(_mo or camera, continuation=_seg_continue)
    lines = dialogue_lines(shot.get("dialogue"))
    slots = speaker_slots(lines)
    # ⭐ 2026-10-09 官方一致性：`(Sx)` 必须与 subject_definitions / retention_analysis
    #    里的 `<Audio N> ... (Sx)` 声明一致。本函数默认按「台词出现顺序」分配 Sx，
    #    而 <Audio N> 按音色库顺序分配 —— 两者会打架。有 audio_defs 时以它为准
    #    （只覆盖本段真正出场的说话人，未声明的仍走原顺序）。
    if slots_override:
        for _sk, _sv in slots_override.items():
            if _sk in slots and _sv:
                slots[_sk] = str(_sv)
    picture_refs = picture_refs or {}
    end_frame_ref = str(end_frame_ref or "").strip()

    # 构图基准声明只挂在首节拍，后续节拍不必重复。
    # ⚠️ 标签必须**由调用方显式给出**（分镜图在 picture_defs 里的真实位置），
    #    不能再靠「<Picture 1> 在不在」硬判 —— 公共参考图会让分镜图不再排第 1
    #    （见本函数 docstring）。未传标签时才走旧判据（零回归）。
    _sb_label = str(storyboard_ref_label or "").strip()
    if _sb_label:
        first_ref = _sb_label if _sb_label in picture_refs else ""
    else:
        first_ref = "<Picture 1>" if "<Picture 1>" in picture_refs else ""

    beats = _beats(shot, duration)
    spoken = _spoken_clause(lines, slots, subject_labels=subject_labels,
                            audio_labels=audio_labels, voice_descs=voice_descs)

    # P0-1 首帧 / 运动三段锚点（借鉴 ViMax / CineGen）：
    # 读到三字段（旧剧本/模型未输出 → 空串，下面各段整块不出现，零回归）。
    _first_frame = str(shot.get("first_frame") or "").strip()
    _motion = str(shot.get("motion") or "").strip()

    out: List[str] = [_style_opening(style, has_dialogue=bool(lines))]
    # 2026-10-09 官方对齐（ref-en.txt §5.2）：删掉 `Opening frame:` 独立元信息行 ——
    # 官方与本地手跑模板的 detailed_description **只有 [Shot N] 时间线**；
    # 首帧信息改为内联进 [Shot 1] 的画面描述（见下方）。
    for idx, (start, _span, text) in enumerate(beats, start=1):
        clause = _strip_end(text)
        # A-5：单节拍的画面细节（description + visual_detail）也设闸门，
        # 否则历史超长描述会把 detailed_description 整段撑爆。
        clause = _clamp(clause, MAX_DETAIL_CHARS, "build_detailed_description.detail")
        if idx == 1 and first_ref:
            clause += f"; the composition, framing and character placement follow {first_ref}"
        if idx == 1 and _first_frame:
            # 首帧快照内联（官方不写独立 Opening frame 元行）
            clause += f"; the shot opens from the static snapshot where {_strip_end(_first_frame)}"
        # 统一补句号收口（clause 已 strip 掉原句末标点，不会出现「。。」）
        clause += "."
        # 模板格式：首镜 [Shot 1]，后续镜「At 时间码, the camera cuts to」。
        # 景别与运镜只在首镜点明，后续由画面内容承接（模板同样不逐镜重复景别词）。
        # ⚠️ 运镜**只在句首声明一次**，绝不在这里于句尾再补一遍 —— 同一运镜写两遍
        #    会让提示词自相矛盾（实测会把「推入」重复两次），且浪费提示词预算。
        if idx == 1:
            # ``[Shot 1] A medium shot, the camera tracks the subject: …``
            # 景别缺但运镜在（如 camera='手持跟拍'）时只写运镜，不出现空景别。
            head_parts = [p for p in (camera_en, camera_move) if p]
            # 2026-10-09 官方对齐：本镜整体运动**内嵌进 [Shot 1] 句**（官方与本地模板
            # 都把运镜写在镜头句里，没有独立的 `Motion:` 元行）。
            if _motion:
                head_parts.append(f"the guiding motion is {_strip_end(_motion)}")
            head = (f"[Shot 1] {', '.join(head_parts)}: " if head_parts
                    else "[Shot 1] ")
        else:
            # ⚠️ 句中位置必须压小写：``cuts to A medium shot`` 是错的。
            # 2026-10-09 官方格式（ref-en.txt §5.2）：后续镜头**必须带 [Shot N] 段标**
            cam_mid = _mid_sentence(camera_en)
            head = (f"[Shot {idx}] At {fmt_ts(start)}, the camera cuts to {cam_mid}: " if cam_mid
                    else f"[Shot {idx}] At {fmt_ts(start)}, the camera cuts to a new framing: ")
        # 台词落在最后一个节拍，符合「动作推进→开口说话」的时序直觉
        if idx == len(beats) and spoken:
            line = head + clause + " " + spoken
        else:
            # 无台词节拍显式标注「无人说话、无画外音」，防止模型把画面描述念成旁白。
            # ⚠️ 2026-09-27 对齐官方：旧「No dialogue.」太弱（模型会理解成「这幕没对话」，
            #    于是改用画外音复述画面/背景），改为官方「no one speaks / no voice-over」。
            # 2026-10-09 官方/本地模板口径：无声拍写 `No dialogue.`，后续拍
            # `No further dialogue.`（旧的解释性长句反而容易诱导模型自造语音）。
            _nod = "No dialogue." if idx == 1 else "No further dialogue."
            line = head + clause + " " + _nod
        out.append(line)

    # ⭐ 尾帧软锚定（Ref2VA 首尾一致的关键，2026-09-26）：
    # Ref2VA 对参考图是软约束，没有「最后一帧=尾帧图」的硬保证。为逼近首尾一致，
    # 在末拍之后显式声明「本段最后一帧必须落在尾帧参考图的构图与姿态上」——
    # 给时间轴末端一个明确落点，配合 FL2V Turbo LoRA 拉首尾一致性。
    # ⚠️ 只挂在尾帧参考图确实存在的镜头；无尾帧时绝不凭空写（否则会诱发模型
    #    自行脑补一个"结束帧"，反而引入漂移）。
    # 2026-10-09 官方对齐（ref-en.txt §5.2）：末态**内联进末拍句子**，不再单起
    # `End state:` 元行 —— 官方与本地模板的 detailed_description 只有 [Shot N] 时间线。
    # 尾帧参考图与 last_frame 二选一（避免「图说一个终态、文说另一个」打架）。
    if end_frame_ref and out:
        out[-1] = out[-1] + (f" The clip closes exactly on the composition, framing, "
                             f"character pose and expression shown in {end_frame_ref},"
                             f" with the camera movement and action settling into that "
                             f"exact end image.")
    elif out:
        _last_frame = str(shot.get("last_frame") or "").strip()
        if _last_frame:
            out[-1] = out[-1] + (f" By the close the frame settles into — "
                                 f"{_strip_end(_last_frame)}.")
    # P0-1 运动声明：严格区分「摄影机运动（推拉摇移跟升降）」与「画面内运动（人物/物体
    # 自身动作）」。写进末段（时间轴锚点之后、作为独立一句），给模型显式运动类型锚点。
    # ⚠️ 与句首 camera_move（景别+运镜复合词）不同处：这里是**本镜整体运动定性**，
    # 句首是逐拍镜头声明；两者互补不重复（句首不写本镜无运镜时的画面内动作）。
    # 2026-10-09 官方对齐：原 `Motion:` 独立元行已删除（运镜已内嵌进 [Shot 1]）。
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 主体定义 / 保留分析
# --------------------------------------------------------------------------- #

def _subject_definitions(picture_defs: Sequence[Tuple[str, str]],
                         subjects: Sequence[Dict[str, str]],
                         style: str = "",
                         storyboard_ref_label: str = "",
                         end_frame_ref: str = "",
                         audio_defs: Sequence[Dict[str, str]] = ()) -> str:
    """``subject_definitions``：逐张参考图声明用途 + 逐主体描述外观（英文句式）

    模板写法::

        <Picture 1> is the reference image defining the appearance, costume and style
        of the Male Lead 韩立, and serves as the composition anchor for his on-screen shots.
        <Subject 1> is 韩立 in <Picture 1> — a young Chinese male with ...

    ⚠️ 中文外观描述按模板惯例**原样嵌进英文句子**（模板同样保留中文人名/术语）。
    """
    lines: List[str] = []
    # 主体 → 其参考图标签（用于 ``<Subject N> ... in <Picture M>`` 的归属声明）
    subj_pics: Dict[str, str] = {}
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or "").strip()
        pic = str(sub.get("picture") or "").strip()
        if not pic:
            pic = f"<Picture {min(i, max(1, len(picture_defs)))}>"
        if name:
            subj_pics[name] = pic

    _sb_label = str(storyboard_ref_label or "").strip()
    _ef_label = str(end_frame_ref or "").strip()
    for label, desc in picture_defs:
        # 找出该参考图对应的主体名（有则写进句子，便于模型建立图-人绑定）
        owner = ""
        for name, pic in subj_pics.items():
            if pic == label:
                owner = name
                break
        if _sb_label and label == _sb_label:
            # ⭐ 分镜图 = 构图/机位/站位基准（2026-10-04 修正）：旧实现落到下方 else，
            #   把分镜图说成「environment, materials and lighting mood of the scene」——
            #   语义错位会稀释构图锚点权重（用户反馈「视频提示词质量不高」）。
            lines.append(
                f"{label} is the storyboard reference for this shot, defining the "
                f"composition, framing, camera angle and character placement (the "
                f"staging baseline that must be followed): {desc}.")
        elif _ef_label and label == _ef_label:
            lines.append(
                f"{label} is the end-frame reference for this shot, defining the final "
                f"composition and pose the clip must settle into: {desc}.")
        elif owner:
            lines.append(
                f"{label} is the reference image defining the appearance, costume and "
                f"style of {owner}, and serves as the composition anchor for their "
                f"on-screen shots: {desc}.")
        else:
            lines.append(
                f"{label} is the reference image defining the environment, materials and "
                f"lighting mood of the scene: {desc}.")
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or f"Subject {i}").strip()
        appearance = str(sub.get("appearance") or "").strip()
        pic = subj_pics.get(name, f"<Picture {min(i, max(1, len(picture_defs)))}>")
        lines.append(
            f"<Subject {i}> is {name} in {pic} — {appearance}; the on-screen appearance "
            f"and costume must stay consistent with this reference image.")
    # ⭐ 2026-10-09 官方格式（ref-en.txt §2.4）：音色参考必须在 subject_definitions 里
    #    声明为 `<Audio N> is the voice-timbre reference for <Subject M> (Sx)`。
    #    此前只在「公共提示词」块里写 `<Audio N> = 角色名`（非官方格式、且在六段之外），
    #    H3 拿到了音频却不知道用途 → 自己编嗓子（中英混杂 / 多说话人）。
    _subj_idx = {}
    for _si, _ss in enumerate(subjects, start=1):
        _sn = str(_ss.get("name") or "").strip()
        if _sn:
            _subj_idx[_sn] = _si
    for _d in (audio_defs or []):
        _lbl = str(_d.get("label") or "").strip()
        _i = _subj_idx.get(str(_d.get("name") or "").strip())
        if not _lbl or not _i:
            continue
        _sx = str(_d.get("speaker") or "").strip() or f"S{_i}"
        lines.append(f"{_lbl} is the voice-timbre reference for <Subject {_i}> ({_sx}), "
                     f"containing a spoken Chinese vocal layer.")
    return "\n".join(lines) if lines else \
        "<Picture 1> is the reference image defining the appearance and composition of this shot."


def segment_durations(duration, max_sec: float = None,
                      min_sec: float = None) -> List[float]:
    """把一个镜头的总时长切成若干**每段 ≤ max_sec** 的时长列表（秒）

    为什么需要它（2026-09-25 需求 J）：AI 视频的可信窗口只有约 4 秒，超过后段
    容易崩坏。本项目剧本单镜普遍 4.5~12 秒（实测第1集 27/27 超线），所以生成期
    要把长镜拆成多段、用 H3 原生段间衔接拼成连续视频，既保住镜头语义，
    又把**每一次生成**都压在 4 秒以内。

    切法：``n = ceil(duration / max_sec)``，再把总时长**均摊**到 n 段
    （``duration / n``），而不是「前 n-1 段满 4 秒 + 尾段塞余数」——
    均摊避免出现「前几段 4s、最后一段 0.3s」的残缺尾段（模型演不出东西，
    且接缝突兀）。均摊后每段必然 ≤ max_sec（因 n = ceil 保证 duration/n ≤ max_sec）。

    ⚠️ 段数上限由 ``min_sec`` 兜底：若均摊后某段短于 ``min_sec``，减少段数
    （宁可某段略超 max_sec，也不要碎段）—— 但 duration ≤ max_sec 时**恒返回单段**，
    即「本来就不超线的镜头行为完全不变」，这是本函数最重要的向后兼容保证。

    返回：``[3.75, 3.75, 3.75, 3.75]`` 这类等长列表；``duration <= max_sec`` 时返回
    ``[duration]`` 单元素列表（**不做任何取整**，保持原时长逐位一致）。
    """
    try:
        dur = float(duration or 0)
    except (TypeError, ValueError):
        dur = 0.0
    cap = float(H3_SEGMENT_MAX_SEC if max_sec is None else max_sec)
    floor = float(H3_SEGMENT_MIN_SEC if min_sec is None else min_sec)

    # 非有限值 / 非正时长 → 原样单段（让下游按自己的兜底逻辑处理，不在这里造数）
    if dur != dur or dur in (float("inf"), float("-inf")) or dur <= 0:
        return [dur]
    # 未超上限 → 单段，**逐位返回原值**（不做 round，避免 5.0 被改成 4.999）
    if cap <= 0 or dur <= cap:
        return [dur]

    n = int(math.ceil(dur / cap))
    # min_sec 兜底：段太少会碎 → 逐步减段，直到每段都不短于 floor（n 至少为 1）
    if floor > 0:
        while n > 1 and (dur / n) < floor:
            n -= 1
    return [dur / n] * n


def segment_shot(shot: dict, duration, max_sec: float = None) -> List[dict]:
    """把一个镜头按 :func:`segment_durations` 切成多个「生成段」

    每个子段是一个**独立的 H3 段**（自带提示词与时长），但它们同属一个镜头：
    - ``name`` 为 ``shot_07_a`` / ``shot_07_b`` … 仅作日志与画布标识；
    - **落盘仍是一个 ``shot_07.mp4``**（H3 段间原生无缝衔接，一次提交产出连续视频），
      所以不需要改任何文件命名契约（``probe_video`` / ``dub_mix`` / ``_SHOT_RE`` 全不受影响）；
    - 提示词的 ``[Shot N]`` 计数与节拍由 :func:`build_detailed_description` 按**子段时长**
      重新生成，因此每段的节拍自然变少（4 秒 → 1 个节拍），动作不会挤在一段里；
    - **参考图槽位形状保持一致**（同一批图片），第 2 段起不再重复「构图基准」声明
      （``build_detailed_description`` 只在首节拍挂 ``first_ref``）。
    - ⭐ **台词只落在最后一段**（关键）：H3 的每段都会被独立生成并出声，若每段都带台词，
      同一句会被念 N 遍。因此首段起清空 ``dialogue``，只保留最后一段的台词 ——
      这也符合「动作推进 → 最后开口说话」的时序直觉（与 :func:`_beats` 的收尾约定一致）。
      同时清掉 ``narration``，避免旧剧本走「画外音延续」分支重复念白。
    - ⭐ **段间锚点按段位分配**（2026-09-29 续修）：``first_frame`` / ``last_frame`` /
      ``motion`` 是**整镜级**锚点，历史实现 ``sub = dict(shot)`` 把它们逐字复制给
      每一个子段 —— 于是前段被要求提前演到整镜末态、后段被要求回到整镜开场
      （画面倒带），段与段之间必然跳变。现在：
        首段 = 唯一持有 ``first_frame``（开场快照）的段；
        末段 = 唯一持有 ``last_frame``（收尾终态）的段；中间段两者皆无。
    - ⭐ **运镜只由首段重新起手**：非首段置 ``_seg_continue``，提示词写成
      「同一运镜继续、不得重启/不得切」（见 :data:`_MOVE_CONTINUE_EN`），
      否则每 4 秒的接缝处运镜都会从头再来一遍。

    返回的每个 dict 供 :func:`segment_to_dicts` 转成 comfyui_client 的 segments 元素。
    """
    try:
        dur = float(duration or 0)
    except (TypeError, ValueError):
        dur = 0.0
    durs = segment_durations(dur, max_sec=max_sec)
    last = len(durs) - 1
    multi = len(durs) > 1
    out: List[dict] = []
    for i, d in enumerate(durs):
        sub = dict(shot or {})
        # ⚠️ 子段的 duration 必须是**本段时长**，否则 build_detailed_description 会按
        #    总时长铺节拍，4 秒的段被塞进 12 秒的节拍 → 段内动作空转。
        sub["duration"] = d
        sub["_seg_index"] = i
        sub["_seg_count"] = len(durs)
        if multi:
            # ---- 段间锚点按段位分配（见函数 docstring）----
            # 历史缺陷：dict(shot) 把整镜的 first_frame / last_frame / motion 逐字复制给
            # 每个子段 → 每段都同时声明「开场快照」与「收尾终态」：前段被迫提前演到末态、
            # 后段被迫回到开场（倒带），接缝必然跳变，运镜也被迫从头再来一遍。
            sub["_seg_continue"] = i > 0
            sub["first_frame"] = str(shot.get("first_frame") or "") if i == 0 else ""
            sub["last_frame"] = str(shot.get("last_frame") or "") if i == last else ""
            if i > 0:
                # 非首段：运动定性由句首的「运镜延续」声明承担，不再逐字重复整镜运动
                sub["motion"] = ""
        if multi and i != last:
            # 非末段：不带台词（否则同一句会被 H3 在每段各念一遍）。
            # 清空而非删除键，保持下游 ``shot.get("dialogue")`` 的类型稳定。
            sub["dialogue"] = []
            sub["dialogue_text"] = ""
            sub["narration"] = ""
            # 画面内容降调为「进程推进」：段内不需要再复述完整动作起手，
            # 用一句承接语把动作往下一段推（与 _beats 的中间节拍同一写法）。
            # ⚠️ 首段没有「上一段」：写成「from the start of this shot」，否则模型会
            #    去承接**上一个镜头**的动作（首段是镜头的起手，不是承接）。
            sub["description"] = (
                ("the action continues to advance from the start of this shot, keeping the "
                 if i == 0 else
                 "the action continues to advance from the previous moment, keeping the ")
                + "characters' appearance, wardrobe and scene lighting exactly consistent")
            sub["visual_detail"] = ""
        out.append(sub)
    return out


def _retention_analysis(picture_defs: Sequence[Tuple[str, str]],
                        subjects: Sequence[Dict[str, str]], style: str = "",
                        shots: str = "", end_frame_ref: str = "",
                        storyboard_ref_label: str = "",
                        audio_defs: Sequence[Dict[str, str]] = (),
                        item_labels: Sequence[str] = (),
                        speaking_names: Sequence[str] = ()) -> str:
    """``retention_analysis``：逐主体声明必须保留的外观项（英文句式）

    模板写法::

        <Subject 2> (appears in [Shot 1], [Shot 2]): fully_preserved - her long black
        hair, pale teal-blue silk veil, ... are all retained without change.
        <Picture 2> ([Shot 2] composition anchor): fully_preserved - ...

    历史实现写成中文「必须保留 …」bullet 列表，与模板的
    ``fully_preserved`` 句式不一致（P-3 对齐项）。
    """
    lines: List[str] = []
    appear = f" (appears in {shots})" if shots else ""
    #: 本镜「道具类」参考图的名称集合（用于把道具从角色/服装语义里分离）
    _item_names = tuple(str(x).strip() for x in (item_labels or ()) if str(x).strip())
    #: 本镜说话人名称集合（用于给非说话人显式标注「沉默」）
    _speaking_names = tuple(str(x).strip() for x in (speaking_names or ()) if str(x).strip())
    #: 是否已知本镜说话人 —— 空集合表示「本镜无台词」，此时**不能**把所有人都标成沉默
    #: （无台词镜应由 overall_soundscape 的 NO dialogue 硬禁句式统一处理）。
    _speaking_names_defined = bool(_speaking_names)
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or f"Subject {i}").strip()
        appearance = str(sub.get("appearance") or "").strip()
        item = appearance or "their facial features, hairstyle and costume"
        # 2026-10-09 官方对齐：锁脸要求**并入 <Subject N> 这一行**（官方 retention_analysis
        # 只有逐标签的 fully_preserved 行，没有独立元行）；面部几何写在保留清单里。
        # ⭐ 2026-10-09：道具绝不能套用「锁脸」句式 —— 它没有脸。
        #    实测缺陷：旧哨子被写进 <Subject N> 行并要求保留
        #    「face shape / hairline / eyebrow / nose / lip」，而真正该保留的
        #    「形态与朝向」一个字都没有 → H3 多帧运动中把它画反（用户实观）。
        _is_item = str(sub.get("name") or "").strip() in set(_item_names or ())
        if _is_item:
            _face = ("; the object's exact shape, structure and proportions stay identical to "
                     f"{sub.get('picture') or '<Picture 1>'} — copy them from the reference "
                     "image, never re-draw from text; and its orientation is kept unchanged "
                     "for the whole shot (the functional end — mouthpiece / blade / opening — "
                     "points the same way in every frame, never flipped or reversed)")
        else:
            _face = ("; the face shape, hairline, eyebrow shape, eye shape, nose shape, "
                     "lip shape and overall facial geometry stay identical to "
                     f"{sub.get('picture') or '<Picture 1>'} — copy them from the "
                     "reference image, never re-draw them from text")
        # ⭐ 2026-10-10 官方模板对齐（用户提供的 H3信号10段测试001.json）：
        #    官方 retention_analysis 会**显式标注沉默者**：
        #      <Subject 2> (appears in [Shot 1]): fully_preserved - ...; she is silent
        #      in this segment.
        #    这条声明直接抑制「旁听者乱动嘴」—— 用户反馈的「语音在说话但人物没张嘴」
        #    往往伴随「没说话的人嘴却在动」，属于口型归属错误，而非单纯的同步误差。
        #    判据：本镜出场（在 subjects 里）但**不是**本镜说话人 → 标注沉默。
        _is_speaking = str(sub.get("name") or "").strip() in set(
            str(x).strip() for x in (speaking_names or ()) if str(x).strip())
        _silent = ("; this subject is silent in this segment — the lips stay closed "
                   "and the mouth does not form words") if (
            _speaking_names_defined and not _is_speaking and not _is_item) else ""
        lines.append(f"<Subject {i}> {name}{appear}: fully_preserved - {item}{_face}, "
                     f"all retained without change{_silent}.")
        # ⭐ P1「锁脸」（2026-09-26）：显式列举**面部身份关键点**，把「脸」锁定到参考图。
        # 历史只写 appearance（角色描述，常偏服装/气质），脸部的保留被一句「all retained」
        # 含糊带过 —— 长序列里脸型/五官会缓慢漂移（用户反馈「角色变形」）。
        # 业界锁脸（IP-Adapter / InstantID 的思路）核心就是「身份指向参考图、不复述」，
        # 这里在不引入新模型的前提下，把「五官几何必须锁定参考图」说死，并**禁止**
        # 用文字重述五官（一旦重述，模型会用文字去"重新画"一张脸，反而漂移）。
        # 2026-10-09 官方对齐：锁脸要求已并入上方 <Subject N> 行；此处不再单列元行。
    # ⚠️ 参考图要按**用途**分开写保留项：主体参考图管「costume / palette / hairstyle」，
    # 场景参考图管「scene structure / materials / lighting」。若不分流，场景图也会被
    # 写成「the costume … follow the reference image exactly」——语义错位的假声明。
    subj_pics = {str(s.get("picture") or "").strip() for s in subjects}
    subj_pics.discard("")
    _sb_label = str(storyboard_ref_label or "").strip()
    for label, desc in picture_defs:
        # ⭐ 分镜图 = 构图基准（2026-10-04 修正）：旧实现按「是否主体图」二分，
        #   分镜图被写成「scene structure / costume … follow the reference image」——
        #   它真正要保留的是**构图、景别、机位、人物站位**。
        if _sb_label and label == _sb_label:
            lines.append(f"{label}{appear} (composition anchor): fully_preserved - the "
                         f"shot's composition, framing, camera angle and character "
                         f"placement follow this storyboard reference exactly.")
            continue
        # ⭐ 2026-10-09 修复（用户实测：旧哨子被画反）：
        #    道具参考图此前**掉进了环境分支** —— 它既不在 subjects（角色）里，
        #    也没有类型信息可用，于是被判 is_env=True，被声明成
        #    「the scene structure, materials and lighting … follow the reference image」。
        #    结果：整段提示词里**没有任何一句**说「道具的形态/结构/朝向要保留」，
        #    H3 在多帧运动中就把它画反了（分镜阶段有分镜图兜底所以没暴露）。
        #    官方 retention_analysis 的口径是「逐标签声明 fully_preserved」，
        #    道具理应有自己的一行，且必须把**朝向**写死（这是模型最容易漂的维度）。
        # 注意：item_labels 传进来的是**道具名称**（如「旧哨子」），而这里循环的是
        # **参考图标签**（如 <Picture 3>）。必须经 subjects[].picture 反查映射，
        # 否则道具行永远走不进这个分支（第一版就踩了这个坑）。
        if label in {str(_s.get("picture") or "").strip()
                     for _s in subjects
                     if str(_s.get("name") or "").strip() in set(_item_names or ())}:
            lines.append(f"{label}{appear}: fully_preserved - {desc}; the object's exact "
                         f"shape, structure and proportions follow the reference image, "
                         f"and its **orientation is kept unchanged for the whole shot** — "
                         f"the functional end (mouthpiece / blade / opening) points the same "
                         f"way in every frame as in the reference, never flipped or reversed.")
            continue
        is_env = label not in subj_pics if subj_pics else False
        if is_env:
            lines.append(f"{label}{appear}: fully_preserved - {desc}; the scene "
                         f"structure, materials and lighting follow the reference image exactly.")
        else:
            lines.append(f"{label}{appear}: fully_preserved - {desc}; the costume, palette "
                         f"and hairstyle follow the reference image exactly.")
    # 2026-10-09 官方对齐：原「光照方向 / 风格锁定」两行元指令已删除
    # （官方 retention_analysis 只有逐标签的 fully_preserved 行）。
    # ⚠️ 本地模板的 retention_analysis **不含任何「禁止文字/字幕」的否定指令**，
    # 只描述「哪些内容必须保留」。历史实现在这里写「不得添加文字/字幕/水印/logo」，
    # 副作用是：提示词里凭空出现「字幕」「文字」两个词，H3 反而更容易把它们画进画面
    # （实测视频生成出了字幕）。故删掉该行，改用「必须保留」的正向表述。
    # 2026-10-09 官方对齐：原「跨镜构图一致」元行已删除。
    # ⭐ 尾帧保留声明（2026-09-26）：尾帧参考图是「结束帧」而非「外观锚点」，
    # 其保留项语义与主体/场景图不同 —— 要保留的是「末帧构图与姿态」，
    # 而不是「服装/材质」。故单独写一条，避免落入上方 is_env 分流被误写成场景材质。
    # ⭐ 2026-10-09 官方格式（ref-en.txt §4.2）：音频用**独立的一套**保留标记，
    #    音色参考固定写 `reference`（不复制原信号，只借音色与节奏）。
    _ri = {}
    for _si, _ss in enumerate(subjects, start=1):
        _sn = str(_ss.get("name") or "").strip()
        if _sn:
            _ri[_sn] = _si
    for _d in (audio_defs or []):
        _lbl = str(_d.get("label") or "").strip()
        _i = _ri.get(str(_d.get("name") or "").strip())
        if not _lbl or not _i:
            continue
        lines.append(f"{_lbl}: reference - its vocal timbre guides the dialogue delivery "
                     f"of <Subject {_i}> without copying the original signal.")
    if end_frame_ref:
        lines.append(
            f"{end_frame_ref} is the end-frame reference for this shot: fully_preserved - "
            f"the final frame of the generated clip must match its composition, framing, "
            f"character pose and expression exactly, with no drift.")
    return "\n".join(lines)


def build_summary(shot: dict, duration: float, subjects: Sequence[Dict[str, str]] = (),
                  audio_defs: Sequence[Dict[str, str]] = ()) -> str:
    """``summary``：2~4 句目标视频概述

    模板 10 段**无一例外**都以 ``[reference generation]`` 开头 —— 这是 Ref2VA
    模式的显式声明，告诉模型「本段以参考图为基础生成」（P-1 对齐项）。
    历史实现缺此前缀，与模板不一致。
    """
    desc = str(shot.get("description") or "").strip()
    emotion = str(shot.get("emotion") or "").strip()
    loc = str(shot.get("location") or "").strip()
    names = ", ".join(str(s.get("name") or "").strip() for s in subjects if s.get("name"))
    bits: List[str] = []
    bits.append(f"The target video is a comic-drama shot of about "
                f"{float(duration or 4):.0f} seconds" + (f", set in {loc}" if loc else "") + ".")
    if names:
        bits.append(f"The on-screen subjects are {names}.")
    if desc:
        # 2026-10-09 官方对齐（ref-en.txt §3）：summary 是英文散文；把剧本原文用英文
        # 框架包起来并**显式声明「这是画面内容、不是台词」** —— 原先直接贴中文原文，
        # 模型容易把这段叙事当成要念的对白。同时把角色名替换成已定义的 <Subject N>。
        _d = _strip_end(desc).replace("\n", " ")
        for _i2, _s2 in enumerate(subjects or [], start=1):
            _n2 = str(_s2.get("name") or "").strip()
            if _n2:
                _d = _d.replace(_n2, f"<Subject {_i2}>")
        bits.append(f"The action shown on screen (visual content only, not spoken): {_d}.")
    # ⭐ 2026-10-10 官方模板对齐（用户提供的 H3信号10段测试001.json）：
    #    官方 summary **显式写出「谁在说话、用什么语气」**，例如
    #      "...she turns to camera and delivers a proud declaration..."
    #      "...refuses her declaration in a cold, level voice..."
    #      "...his refusal spoken while caught in the force"
    #    而我们的旧实现只写 "visual content only, not spoken" —— 等于告诉模型
    #    「本段没有讲话」，与「人物确实在说台词」相矛盾，是**口型不同步**的高概率成因。
    #    这里补一句权威的「谁在说」，让 H3 把口型对准正确的角色。
    #    ⚠️ 只描述「谁在说 / 什么语气」，**不写台词原文**（原文由 detailed_description
    #       的 <d>[Chinese]…</d> 承载），避免 summary 被当成朗读稿。
    _dlg = dialogue_lines(shot.get("dialogue"))
    if _dlg:
        _spk_seen: List[str] = []
        for _ln in _dlg:
            _sp = str(_ln.get("speaker") or "").strip()
            if _sp and _sp not in _spk_seen:
                _spk_seen.append(_sp)
        # 说话人名 → <Subject N>（与 subjects 顺序一致）
        _spk_tags = []
        for _sp in _spk_seen:
            _tag = ""
            for _i3, _s3 in enumerate(subjects or [], start=1):
                if str(_s3.get("name") or "").strip() == _sp:
                    _tag = f"<Subject {_i3}>"
                    break
            _spk_tags.append(_tag or _sp)
        _tone = emotion or "a measured, natural speaking tone"
        if _spk_tags:
            _who = " and ".join(_spk_tags)
            bits.append(f"{_who} speaks in this segment, delivering the dialogue in "
                        f"「{_tone}」; the speaker's mouth opens and moves with the "
                        f"spoken words.")
    if emotion:
        bits.append(f"The overall emotional tone is 「{emotion}」.")
    if len(bits) < 2:
        bits.append("The shot keeps a single continuous action with a clear start and end.")
    # ⚠️ 句间必须补空格：bits 各自已带句末「.」，直接 "".join 会产出
    # 「…12 seconds, set in X.The on-screen subjects…」这种粘连句（模型会当断句错误）。
    # ⭐ 2026-10-09 官方任务类型前缀（ref-en.txt §3）：有音色参考音频时必须并列
    #    `audio reference` —— 声明「只参考音色/节奏，不复制原信号」。
    _pfx = ("[reference generation + audio reference]" if audio_defs
            else "[reference generation]")
    return _pfx + " " + " ".join(bits)


# --------------------------------------------------------------------------- #
# 对外构建入口
# --------------------------------------------------------------------------- #

def build_ref2va(shot: dict, picture_defs: Sequence[Tuple[str, str]],
                 subjects: Sequence[Dict[str, str]] = (), duration: Any = None,
                 style: str = "", end_frame_ref: str = "",
                 storyboard_ref_label: str = "",
                 audio_defs: Sequence[Dict[str, str]] = (),
                 item_labels: Sequence[str] = ()) -> str:
    """构建 Ref2VA 六段式提示词（有参考图时使用）

    end_frame_ref：可选，尾帧参考图的标签（如 ``<Picture 2>``）。提供时：
    - ``detailed_description`` 末拍追加 end-state 锚定句（最后一帧必须落在尾帧）；
    - ``retention_analysis`` 追加尾帧保留声明。
    用于 keyframe 模式把「首帧 + 尾帧」两张图喂进 Ref2VA 后，用提示词把
    首尾一致性拉回来（Ref2VA 对参考图是软约束，需显式声明，2026-09-26）。

    storyboard_ref_label：可选，**分镜图的真实标签**（如 ``<Picture 4>``）。
    由调用方从 ``picture_defs`` 的实序里取，供 ``detailed_description`` 把
    「构图/景别/人物站位以某图为基准」指向正确的图；不传时回退旧的
    「``<Picture 1>``」硬判（零回归）。见 ``build_detailed_description``。
    """
    shot = shot or {}
    dur = duration if duration is not None else (shot.get("duration") or 4)
    try:
        dur_f = float(dur)
    except (TypeError, ValueError):
        dur_f = 4.0
    pic_map = {label: desc for label, desc in picture_defs}
    end_frame_ref = str(end_frame_ref or "").strip()

    # ⭐ 2026-10-09 官方 Ref2VA 对齐：把「角色参考音色」变成六段内的 <Audio N> 声明。
    #    audio_defs = [{"name": 角色名, "label": "<Audio 1>", "speaker": "S1",
    #                   "voice": "a low controlled male voice ..."}, ...]
    _audio_defs = [d for d in (audio_defs or []) if isinstance(d, dict)]
    _subj_labels, _audio_labels, _voice_descs = {}, {}, {}
    for _i, _s in enumerate(subjects or [], start=1):
        _n = str(_s.get("name") or "").strip()
        if _n:
            _subj_labels[_n] = f"<Subject {_i}>"
    for _d in _audio_defs:
        _n = str(_d.get("name") or "").strip()
        if not _n:
            continue
        if _d.get("label"):
            _audio_labels[_n] = str(_d["label"])
        if _d.get("voice"):
            _voice_descs[_n] = str(_d["voice"])

    # retention_analysis 里声明「本段出现在哪些镜头」，与模板 ``(appears in [Shot 1]…)``
    # 同构。单次调用只知道这一个 shot，故按节拍数折算（单节拍即 [Shot 1]）。
    n_beats = len(_beats(shot, dur_f))
    shots = ", ".join(f"[Shot {i}]" for i in range(1, n_beats + 1))

    sections = [
        ("subject_definitions", _subject_definitions(
            list(picture_defs), list(subjects), style,
            storyboard_ref_label=storyboard_ref_label, end_frame_ref=end_frame_ref,
            audio_defs=_audio_defs)),
        ("summary", build_summary(shot, dur_f, subjects, audio_defs=_audio_defs)),
        # 本镜说话人（用于给非说话人标注「沉默」）—— 取自 shot.dialogue 的 speaker 字段，
        # 与 summary 的发言人声明同源，保证六段内部一致。
        ("retention_analysis", _retention_analysis(list(picture_defs), list(subjects),
                                                    style, shots, end_frame_ref,
                                                    storyboard_ref_label=storyboard_ref_label,
                                                    audio_defs=_audio_defs,
                                                    item_labels=item_labels,
                                                    speaking_names=[
                                                        str(_l.get("speaker") or "").strip()
                                                        for _l in dialogue_lines(
                                                            shot.get("dialogue"))
                                                        if str(_l.get("speaker") or "").strip()])),
        ("detailed_description", build_detailed_description(
            shot, dur_f, style, pic_map, end_frame_ref, storyboard_ref_label,
            subject_labels=_subj_labels, audio_labels=_audio_labels,
            voice_descs=_voice_descs,
            slots_override={str(_d.get("name") or "").strip():
                            str(_d.get("speaker") or "").strip()
                            for _d in _audio_defs if _d.get("speaker")})),
        ("overall_soundscape", build_soundscape(shot)),
        ("non_diegetic_music", build_music(shot, style)),
    ]
    return "\n\n".join(f"{name}:\n{body}" for name, body in sections)


def build_base(shot: dict, mode: str = "T2VA", duration: Any = None, style: str = "") -> str:
    """构建 base 模式三段式提示词（无参考图时使用）"""
    shot = shot or {}
    dur = duration if duration is not None else (shot.get("duration") or 4)
    try:
        dur_f = float(dur)
    except (TypeError, ValueError):
        dur_f = 4.0
    mode = str(mode or "T2VA").upper()
    body = build_detailed_description(shot, dur_f, style)
    head = f"[{mode}] " if mode else ""
    sections = [
        ("integrated_multimodal_description", head + body),
        ("overall_soundscape", build_soundscape(shot)),
        ("non_diegetic_music", build_music(shot, style)),
    ]
    return "\n\n".join(f"{name}:\n{text}" for name, text in sections)


# --------------------------------------------------------------------------- #
# 校验 / 合并
# --------------------------------------------------------------------------- #

def _present_sections(text: str) -> List[str]:
    found = []
    low = str(text or "").lower()
    for name in REF_SECTIONS:
        if f"{name}:" in low or f"{name}：" in low:
            found.append(name)
    return found


def validate(prompt: str) -> Dict[str, Any]:
    """校验提示词是否符合 H3 规范

    返回 ``{"valid", "mode", "missing", "found"}``。
    ``mode`` 为 ``"ref"``（六段齐全）/ ``"base"``（三段齐全）/ ``"invalid"``。
    """
    text = str(prompt or "").strip()
    if not text:
        return {"valid": False, "mode": "invalid", "missing": list(REF_SECTIONS), "found": []}

    found = _present_sections(text)
    missing_ref = [s for s in REF_SECTIONS if s not in found]
    low = text.lower()
    found_base = [s for s in BASE_SECTIONS if f"{s}:" in low or f"{s}：" in low]
    missing_base = [s for s in BASE_SECTIONS if s not in found_base]

    if not missing_ref:
        return {"valid": True, "mode": "ref", "missing": [], "found": found}
    if not missing_base:
        return {"valid": True, "mode": "base", "missing": [], "found": found_base}
    # 认为更接近 ref 语义（含参考图标签）时按 ref 报缺
    if "<picture" in low or "subject_definitions" in found:
        return {"valid": False, "mode": "ref", "missing": missing_ref, "found": found}
    return {"valid": False, "mode": "base", "missing": missing_base, "found": found_base}


def merge_detail(prompt: str, detail: str) -> str:
    """把「历史薄描述 / 额外画面细节」并进 ``detailed_description`` 末尾

    用于兼容旧剧本：老提示词往往只是一句裸英文，直接整段采用会让 H3
    失去参考图语义；丢弃又浪费了模型写出的画面信息。折中做法是把它作为
    补充细节粘到详细描述之后，既保住结构化语义，又不丢信息。
    """
    detail = _clamp(str(detail or "").strip(), MAX_DETAIL_CHARS, "merge_detail.detail")
    if not detail:
        return prompt
    lines = str(prompt or "").split("\n")
    # 找到 detailed_description 段落的结束位置（下一个顶层段落名之前）
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip().lower().startswith("detailed_description"))
    except StopIteration:
        return f"{prompt}\n\n补充画面细节：{detail}"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().lower().rstrip(":：") in REF_SECTIONS + BASE_SECTIONS:
            end = i
            break
    extra = f"补充画面细节：{detail.rstrip('。')}。"
    merged = lines[:end] + [extra] + lines[end:]
    return "\n".join(merged)


def resolve(shot: dict, picture_defs: Sequence[Tuple[str, str]] = (),
            subjects: Sequence[Dict[str, str]] = (), duration: Any = None,
            style: str = "", end_frame_ref: str = "",
            storyboard_ref_label: str = "",
            audio_defs: Sequence[Dict[str, str]] = ()) -> str:
    """生成期择优：合规的既有 ``prompt_h3`` 直接用，否则用构建器重建

    这是修「薄英文提示词把结构化构建器整个顶掉」的落点：
    - 既有提示词通过 :func:`validate`（六段/三段齐全）→ 尊重它（LLM 写的散文往往更生动）
    - 不合规（历史裸英文句、缺段）→ 用构建器产出规范提示词，并把旧文本并入细节

    ``storyboard_ref_label``：分镜图的真实标签（见 :func:`build_ref2va`）。
    """
    shot = shot or {}
    style = str(style or shot.get("style") or "").strip()
    existing = str(shot.get("prompt_h3") or "").strip()

    if picture_defs:
        built = build_ref2va(shot, picture_defs, subjects, duration=duration, style=style,
                             end_frame_ref=end_frame_ref,
                             storyboard_ref_label=storyboard_ref_label,
                             audio_defs=audio_defs)
    else:
        built = build_base(shot, "T2VA", duration=duration, style=style)

    if not existing:
        return clamp_h3_prompt(built)
    verdict = validate(existing)
    if verdict["valid"]:
        return clamp_h3_prompt(existing)
    logger.info("prompt_h3 结构不合规（缺 %s），改用规范构建器并并入原描述",
                ",".join(verdict["missing"]) or "未知")
    return clamp_h3_prompt(merge_detail(built, existing))


def style_of(shot: dict, fallback: str = "") -> str:
    """取镜头风格，缺省回退到调用方传入的风格 / 内置默认值"""
    return str(shot.get("style") or fallback or _DEFAULT_STYLE).strip()


def transition_clause(prev_shot: dict, shot: dict,
                      prev_episode_state: dict = None) -> str:
    """段间衔接提示词（优化#4 细化版，2026-10-02）。

    生成一条「上一镜末态 → 本镜开场」的中文衔接约束，与 H3 Director 插件原生
    段间引导（上一段末帧回喂）互补 —— 末帧只给画面，文字才说得清**承接方式**：

      · 同场（location 相同）：强调人物位置/朝向/持物/机位关系连续，禁止跳切瞬移；
      · 换场（location 变化）：明确允许换景，只要求开场先交代新环境，**不再**要求
        位置连续 —— 硬要求反而诱导模型在两个场景间硬拗连续性（实测教训）；
      · 机位：两镜机位不同时写明「机位已切换」，避免上一镜的运镜惯性带进本镜；
      · 跨集首镜：prev_episode_state 给出上集 state_out（结尾地点/承接建议）时，
        写「承接上集结尾」，让集与集之间也有文字级衔接（跨集一致性巩固）。

    :return: 衔接句（可直接拼在段提示词末尾）；无衔接信息时返回 ""。
    """
    shot = shot or {}
    cur_loc = str(shot.get("location") or "").strip()
    cur_cam = str(shot.get("camera") or "").strip()
    cur_action = str(shot.get("action") or shot.get("motion") or "").strip()

    prev_state_out = {}
    if isinstance(prev_episode_state, dict):
        prev_state_out = prev_episode_state.get("state_out") or {}
    prev_loc = str((prev_shot or {}).get("location") or "").strip()
    prev_cam = str((prev_shot or {}).get("camera") or "").strip()
    prev_tail = str((prev_shot or {}).get("action")
                    or (prev_shot or {}).get("motion")
                    or (prev_shot or {}).get("description") or "").strip()

    lines: list = []
    same_episode = bool(prev_shot)
    if same_episode and prev_tail:
        # 同场 or 换场的分野：location 都有值且不同 → 换场；其余按同场处理
        is_scene_change = bool(cur_loc and prev_loc and cur_loc != prev_loc)
        if is_scene_change:
            lines.append(f"上一镜结束于：{prev_tail[:50]}。本镜为**换场**（{prev_loc or '原场景'}"
                         f"→{cur_loc}）：开场先交代新环境与人物入场，位置连续性不作要求")
        else:
            lines.append(f"上一镜结束于：{prev_tail[:50]}。本镜开场必须从该状态自然承接："
                         "人物位置/朝向/持物保持连续，不得凭空跳切、瞬移或无故换装")
    elif prev_state_out:
        # 跨集首镜：用上集 state_out 承接（跨集一致性巩固）
        _ep_loc = str(prev_state_out.get("location") or "").strip()
        _ep_tr = str(prev_state_out.get("transition") or "").strip()
        if _ep_loc or _ep_tr:
            seg = "【跨集衔接】承接上集结尾"
            if _ep_loc:
                seg += f"（上集结束于：{_ep_loc[:40]}"
                if _ep_tr:
                    seg += f"；建议承接：{_ep_tr[:50]}"
                seg += "）"
            lines.append(seg + "：本集开场须与前情自然衔接，人物外观/服装延续上集结尾状态")

    if same_episode and prev_cam and cur_cam and prev_cam != cur_cam:
        lines.append(f"机位已切换（{prev_cam[:20]} → {cur_cam[:20]}）："
                     "按新机位重新起幅，不要延续上一镜的运镜惯性")

    if not lines:
        return ""
    head = f"【段间衔接】" if same_episode else ""
    return head + "；".join(lines) + "。"


__all__ = [
    "REF_SECTIONS", "BASE_SECTIONS",
    "MAX_PROMPT_CHARS", "MAX_DETAIL_CHARS", "clamp_prompt", "clamp_h3_prompt",
    "BEAT_MAX_SEC", "H3_SEGMENT_MAX_SEC", "H3_SEGMENT_MIN_SEC",
    "segment_durations", "segment_shot",
    "fmt_ts", "dialogue_lines", "speaker_slots",
    "build_soundscape", "build_music", "build_summary",
    "build_detailed_description", "build_ref2va", "build_base",
    "validate", "merge_detail", "resolve", "style_of", "transition_clause",
]
