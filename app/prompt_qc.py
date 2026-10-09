"""提示词预检（生成前质检）

## 为什么要有这一层

提示词是一切出图/出片的**输入**。输入错了，后面的图片质检、视频质检都只是在为错误的
结果打分 —— 用户看到的是「质检通过了，但画面不是我要的」。把质检前移到提示词，才能在
**消耗 GPU 之前**把可确定的缺陷挡掉。

## 设计取舍：预检 + 自愈，而不是一律拦截

提示词缺陷里绝大多数是**确定性可判可修**的（缺风格、缺景别、台词被写进画面、结构缺段），
所以默认策略是：

1. **确定性检查**（零模型依赖、零成本、毫秒级）—— 这里是主力，覆盖真实缺陷的大头；
2. **确定性自愈** —— 能修的当场修，生成用修好的提示词，不打断流程；
3. **只对「必然废图」的缺陷拦截**（画面内容为空、H3 结构缺段且无法修复）——
   拦截会阻塞整集生产，用它换来的收益必须足够大。

因此默认模式是 ``repair``：自愈后放行，记 issue 不阻断；``block`` 为严格模式（有任何
issue 就拦），``warn`` 为诊断模式（只记录、连自愈都不做）。

**为什么这一层不做 LLM 审阅**：预检的价值在于「免费、稳定、毫秒级、零模型依赖」——
它要能跑在每一集每一个镜头上。加一次 LLM 调用就把成本、延迟、不确定性都引进来了，
而真实缺陷的大头（缺风格、缺景别、结构缺段、台词泄漏、禁词）恰好都是确定性可判的。
语义层面的判断留给生成后的图片/视频质检（那时有真图可看，判断也更准）。

（2026-09-30 更新：应「所有图片/视频生成前先做 LLM 提示词增强 + 质检模型提示词质检」
的需求，preflight 外围补了两道**可选**的模型侧闸门——LLM 增强 + 质检模型语义复审，
见 ``prompt_enhance.py``。两者 fail-open、可独立关闭、结果必须过本层的确定性检查才被
采纳；本层仍是唯一裁判，上面的设计理由不变。）

## 判据来源

全部对齐生成端的同一个单一事实源，避免「生成端一个标准、质检端另一个标准」的历史坑
（景别判定曾因两端标准不同，把 43% 的镜头误判为不合格）：

- 分镜图骨架 → ``comfyui_client.build_storyboard_prompt`` 写死的标记
- H3 结构 → ``h3_prompt_kit.REF_SECTIONS`` / ``h3_prompt_kit.validate``
- 风格 → ``style_kit.style_suffix`` / ``style_kit.with_style``
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import h3_prompt_kit
import style_kit
# 景别唯一权威表（分镜取景段判定必须与生成端同源，否则又是一次「两端口径不同」）
from config import SHOT_TYPES

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 可检的提示词类型
# --------------------------------------------------------------------------- #

PROMPT_KINDS: Tuple[str, ...] = ("storyboard", "h3", "asset", "keyframe", "audio")

KIND_LABELS = {
    "storyboard": "分镜图提示词",
    "h3": "视频提示词(H3)",
    "asset": "资产参考图提示词",
    "keyframe": "尾帧提示词",
    "audio": "配音台词",
}

#: 质检模式：warn=只记录 / repair=自愈后放行 / block=有 issue 即拦
PROMPT_QC_MODES: Tuple[str, ...] = ("warn", "repair", "block")
DEFAULT_MODE = "repair"


# --------------------------------------------------------------------------- #
# 分镜图提示词：骨架标记（与 build_storyboard_prompt 一一对应）
# --------------------------------------------------------------------------- #
# ⚠️ 标记串已下沉到叶子模块 prompt_protocol（2026-10-08 解耦）：prompt_enhance 此前只为
#    几个标记就模块级 import 本模块，与本模块内部的惰性 import 构成静态环。这里保留
#    同名再导出，app.py 等历史调用点与守卫脚本零改动；「判据与生成端同源」这条铁律不变。
from prompt_protocol import (  # noqa: F401
    SB_MARK_BLOCKING, SB_MARK_CANVAS, SB_MARK_CONTENT, SB_MARK_CONTENT_LEGACY,
    SB_MARK_FRAMING_LEGACY, SB_MARK_FRAMING_NEW, SB_MARK_FRAMING_UNSPEC, SB_MARK_IDENTITY,
    SB_MARK_IDENTITY_FROM_REF, SB_MARK_NO_REF, SB_MARK_NO_REF_LEGACY, SB_MARK_NO_TEXT,
    SB_MARK_GRID_ZH_LAYOUT, SB_MARK_GRID_ZH_NO_TEXT, SB_MARK_GRID_ZH_PANEL,
    SB_MARK_NO_TEXT_LEGACY, SB_MARK_PRESERVE, SB_MARK_PRESERVE_LEGACY, SB_MARK_REF_USAGE,
    SB_MARK_REF_USAGE_LEGACY, SB_MARK_STYLE, SB_MARK_STYLE_LEGACY, SB_MARK_TASK,
    SB_PRESERVE_BLANKET)
#: 参考图编号标记：官方协议的核心，提示词里**必须**用 <imageN> 而非自然语言指代
SB_IMAGE_TAG_RE = re.compile(r"<image\s*(\d+)\s*>", re.IGNORECASE)
#: 自然语言指代参考图（官方明确禁用，会产生歧义）
SB_VAGUE_REF_RE = re.compile(
    r"(?:第一张图|第二张图|第三张图|图\s*[ABC1-9]|左边那张|右边那张|人物参考图|"
    r"the first image|the second image|image\s+[A-C]\b)",
    re.IGNORECASE)

#: 分镜图「取景段」判定用：景别中文词（新协议里 FRAMING 行仍写中文景别词）。
#: ⚠️ 由 config.SHOT_TYPES 派生（长词优先，便于逐个匹配）：若这里漏了某个景别，
#:    生成端写了「局部」而质检端不认 —— 该镜会被判「缺景别」而**永远过不了预检**。
SB_FRAMING_KEYS: Tuple[str, ...] = tuple(sorted(SHOT_TYPES, key=len, reverse=True))

#: H3 提示词里的无文字声明（build_detailed_description 结尾固定句）
H3_NO_TEXT = "严禁出现任何文字、字幕、台词文本、水印"
H3_NO_TEXT_FULL = "画面中严禁出现任何文字、字幕、台词文本、水印、logo 或标识。"

# --------------------------------------------------------------------------- #
# 尾帧提示词：骨架标记（与 keyframe.build_end_frame_prompt 一一对应）
# --------------------------------------------------------------------------- #
#: 「锚定参考图」语义 —— 尾帧是「以首帧为参考图的图生图」，不写这句模型就会换脸换服装
KF_MARK_ANCHOR = "保持参考图的角色外观"
#: 取景连贯 —— 尾帧必须与首帧同机位同景别，否则两帧无法插值出连续动作
KF_MARK_FRAMING = "与首帧保持连贯"
#: 链式承接 —— 首帧取自上一镜尾帧时，必须写明「承接」，否则模型当成同镜两个时间点而拒绝推进动作
KF_MARK_CHAIN = "承接该画面继续推进"

#: 「字幕类指令」—— 把这些写进提示词，模型会把文字直接画进画面
_INSTRUCTION_RE = re.compile(
    r"(?:字幕|台词文本|文字|标题|水印|logo|LOGO)\s*[:：][^。；，,;！!？?]*[。；，,;！!？?]?"
)

#: 质量类空词：对图像模型无信息量，还会挤占风格/构图语义
#: 2026-09-28：``highly detailed`` 移出 fluff —— 它是分镜 STYLE 质量尾的正式组成
#: （style_kit._QUALITY_TAIL_EN），原判定会在 _repair_storyboard 把它当空词直接剥掉，
#: 导致 with_tail=True 找回的质量尾被静默删除。``ultra detailed`` 与其余项保留。
_QUALITY_FLUFF: Tuple[str, ...] = (
    "masterpiece", "best quality", "ultra detailed",
    "award winning", "8k", "4k", "杰作", "最佳画质", "超高清", "高清晰度",
)

#: 台词的可见长度下限：太短的（「嗯」「好」）在画面描述里可能是巧合，不判泄漏
_MIN_LEAK_LEN = 4

#: 资产提示词最短长度（中英混排按字符计）
_ASSET_MIN_LEN = 12

#: 时间码：与 h3_prompt_kit.fmt_ts / build_detailed_description 的写法一致。
#: 2026-09-24 对齐本地模板后，时间码有两种合法落法：
#:   ① 首镜 ``[Shot 1] ...``（本身不带时间码，以 00:00.000 为隐含起点）
#:   ② 后续镜 ``At 00:03.500, the camera cuts to ...``
#: 故这里同时接受「[Shot N] 紧跟时间码」与「裸 MM:SS.mmm」两种形态。
_TS_RE = re.compile(r"\[Shot\s+(\d+)\]\s+(\d{1,2}):(\d{2})\.(\d{3})")
#: 裸时间码（新格式的 At 00:03.500 与首码判据用）
_BARE_TS_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})\.(\d{3})")
#: 台词标记。2026-09-26 P0-1「口型与画面分离」前是 ``(S1): <d>[Chinese] 台词</d>``；
#: P0-1 起提示词**不再写台词原文**（配音走独立 QwenTTS，H3 自带人声还会被 HDEMUCS
#: 剔除），只保留「说话人槽位 (S1) + 开口说话动作 + 口型自然开合」。故本正则改为
#: 匹配**说话动作句**（speaks … voice … lips moving），同时保留对历史 ``(S1): <d>…</d>``
#: 格式的兼容（存量项目重出视频时不应被新规则误判缺陷）。
_SPOKEN_RE = re.compile(
    r"speaks\s*[—\-–][^.\n]{0,120}?voice"          # 旧：speaks — … voice
    # 2026-10-09 对齐生成侧（build_ref2va）实测句式 —— 精确匹配，**不放宽**：
    #   (S1) says in a clear voice with a measured, controlled delivery, <d>…</d> …
    # 注意只认「says in a <定语> voice」这种紧邻结构，避免把普通叙述里的 voice 也算成说话动作
    #（上一轮我用宽正则 [^.]{0,60} 导致反向探针失效，已回滚重做）。
    r"|\(S\d+\)\s*says?\s+in\s+a?\s*[A-Za-z][A-Za-z ,\-]{0,40}?voice"
    r"|\(S\d+\)\s*[^：:\n]{0,6}[：:]\s*(?:<d>\s*)?\[[A-Za-z\-]+\]"  # 旧：<d>台词</d> 兼容
    , re.IGNORECASE)
#: 「开口说话」动作描述（2026-09-24 二次对齐）：模板每句台词前都有
#: ``speaks — a clear, resonant female voice with …, at a measured declarative rate``。
#: **没有这句就没有唇部动画**（H3 只配音、不开口，实测现象），故升级为硬检查。
_SPEAK_ACTION_RE = re.compile(
    # 2026-10-09：同时接受旧「speaks — … voice」与生成侧现行的「(S1) says in a … voice」（精确匹配）。
    r"speaks\s*[—\-–][^.]{0,120}?voice"
    r"|\(S\d+\)\s*says?\s+in\s+a?\s*[A-Za-z][A-Za-z ,\-]{0,40}?voice",
    re.IGNORECASE)
#: 「无台词」显式声明（2026-09-27 起新官方句式：no one … speaks / no voice-over；
#: 保留对旧「No dialogue」的兼容，存量项目重出视频时不应被误判缺陷）。
_NO_DIALOGUE_RE = re.compile(
    r"No dialogue"                       # 旧句式（兼容存量）
    r"|no one[^.\n]{0,40}speaks"         # 新：No one in the frame speaks
    r"|no voice[- ]?over",               # 新：no voice-over / no voiceover
    re.IGNORECASE)
#: 口型自然开合（P0-1 后说话动作句的标志：不再写台词原文，用这句保留驱动嘴型的信号）
_LIP_SYNC_RE = re.compile(r"lips moving naturally", re.IGNORECASE)
#: ``detailed_description`` 首句风格声明（模板：``The target video uses a … style …``）。
#: ⚠️ 必须同时接受历史中文写法（「全片画面风格统一为…」），否则存量项目重出视频时
#: 会被新规则统一判缺陷（假红）。
_STYLE_OPENING_RE = re.compile(r"^The target video uses .{0,200}?style\b",
                               re.IGNORECASE | re.MULTILINE)
#: 历史中文风格声明（存量项目按旧格式生成）—— 有它即认为风格已声明，不再报「缺风格首句」。
_DD_CN_STYLE_RE = re.compile(r"全片画面风格统一为")
#: 中文景别写进镜头行（``[Shot 1] 中景：`` / ``cuts to a new framing of 近景：``）。
#: 模板一律用英文景别短语，故命中即报。
_CN_CAMERA_RE = re.compile(
    r"\[Shot \d+\]\s*(?:大?远景|全景|大?中景|中近景|近景|大?特写|微距)[：:]"
    r"|camera cuts to[^:：\n]{0,24}(?:大?远景|全景|大?中景|中近景|近景|大?特写|微距)[：:]")
#: 连续标点（含全角/半角混排）
_PUNCT_RUN = re.compile(r"[，、,;。；！？!?]{2,}")

# --------------------------------------------------------------------------- #
# 配音台词：缺陷标记（与 tts_client.clean_line_text / build_dub_plan 的产物对应）
# --------------------------------------------------------------------------- #
# 配音的「提示词」就是那句台词本身 —— 它会被 TTS 逐字念出来，所以判断标准与图片提示词
# 完全不同：图片提示词里「信息越多越好」，台词里**多一个字符都是噪音**。
#
# 历史缺陷（本 kind 存在的原因）：`build_dub_plan` 只做 `clean_line_text` 清洗，
# 之后台词就直奔 TTS。剧本里的结构化残留（`(S1) 说：[Chinese] …`、`[Shot 3] 00:01.200`）
# 与舞台指示（`（转身冷笑）`）会被原样念出来，成片里就能听到「左括号转身冷笑右括号」；
# 空台词或纯标点句（`……`）则会合成出一段静音，而它在流程里显示为「合成成功」——
# 要等到成片验收才发现整集缺了一句。

#: 说话人/时间码等结构化残留：会被逐字念出来的「格式化垃圾」
#: ⚠️ 顺序即优先级：「说话人前缀」必须排在「说话人标记」之前。
#:    `(S1) 说：[Chinese] 台词` 若只删 `(S1)`，会剩下 `说： 台词` ——
#:    TTS 会把这句「说：」也念出来（实测过）。必须整段前缀一起剥掉。
_AUDIO_LEAK_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("说话人前缀（如 (S1) 说：）", re.compile(r"\(\s*(?:S|SPK|N)\s*\d*\s*\)"
                                              r"\s*[^：:\n]{0,6}[：:]\s*")),
    ("语言标签（如 [Chinese]）", re.compile(r"\[(?:Chinese|English|Japanese|Korean|"
                                            r"French|German|Spanish|Portuguese|Russian|"
                                            r"Italian|Auto)\]")),
    ("镜头标签（如 [Shot 3]）", re.compile(r"\[\s*Shot\s*\d+\s*\]")),
    ("时间码（如 00:01.200）", re.compile(r"\b\d{1,2}:\d{2}\.\d{3}\b")),
    ("说话人标记（如 (S1)）", re.compile(r"\(\s*(?:S|SPK|N)\s*\d*\s*\)")),
)
#: 舞台指示关键词：括号里出现这些词说明是「给演员的提示」，不是要念的台词
_AUDIO_STAGE_HINTS: Tuple[str, ...] = (
    "转身", "回头", "叹气", "冷笑", "皱眉", "抬头", "低头", "挥手", "点头", "摇头",
    "沉默", "停", "走近", "后退", "拔剑", "落泪", "颤抖", "咬牙", "呼吸", "旁白",
    "镜头", "画面", "动作", "表情", "画外音", "内心", "os", "OS",
)
_AUDIO_BRACKET_RE = re.compile(r"[（(]([^（()）]{1,24})[)）]")
#: 零宽字符与方向控制符（肉眼看不见，但会被算进字符数、可能触发 TTS 异常）
_AUDIO_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
#: emoji / 变体选择符：TTS 念不出，还会让模型把符号读成名称
_AUDIO_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27bf\ufe0f]")
#: 有效字符（去掉空白、标点、符号后剩下的才是「会发声的内容」）
_AUDIO_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]", re.UNICODE)
#: 单句台词的舒适上限：超过后 TTS 容易在中间断句、也更容易触发超时
_AUDIO_MAX_CHARS = 60
#: 台词最短有效字数：1 个字（「嗯」）能出声但信息量过低，只提示不阻断
_AUDIO_MIN_WORDS = 2
#: 中性情绪词：这些情绪不值得切换 VoiceDesign 模式（切了反而增加音色漂移风险）
_AUDIO_NEUTRAL_EMOTIONS: Tuple[str, ...] = (
    "", "平静", "无", "常态", "自然", "如常", "normal", "neutral", "calm",
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _txt(v) -> str:
    return str(v or "").strip()


def _style_clause(style) -> str:
    """生成端写进提示词的那半句风格声明（与 build_storyboard_prompt 同源）

    返回 ``""`` 表示「这个风格没有可声明的正文」（例如只有画幅 token），
    此时任何「未声明风格」的判定都应当跳过，否则会误报。
    """
    try:
        return style_kit.style_suffix(style, head="画面风格", with_tail=False, with_aspect=False)
    except Exception:  # noqa: BLE001
        return ""


def style_declared(text: str, style) -> bool:
    """提示词里是否已承载目标风格。

    按**词**比对而不是整句比对：模型/历史数据常写「…，中国古风玄幻漫剧风格。」
    这种漏冒号的写法，整句比对会把已有的风格判成缺失，进而重复追加（双重风格）。

    ⚠️ 2026-09-25：QwenImage2.1 分镜提示词改为**英文**（``STYLE: Style: Chinese
    animated style, 3D.``），而 ``_style_clause`` 给的是中文 token（「国漫3D渲染」）。
    若仍只比中文 token，英文提示词会 100% 被判「未声明风格」→ 触发自愈重复追加中文
    风格句（中英风格双写）。故增加英文 token 比对（``style_kit.style_suffix_en``）。
    """
    clause = _style_clause(style)
    if not clause:
        return True
    toks = [t.strip() for t in clause.split("：", 1)[-1].split("，") if t.strip()]
    if not toks:
        return True
    hay = text.lower()
    if any(t.lower() in hay for t in toks):
        return True
    # 英文写法：把同一风格串的英文 token 也纳入比对
    en_toks: List[str] = []
    try:
        en_clause = style_kit.style_suffix_en(style, with_tail=False)
        if en_clause:
            body = en_clause.split(":", 1)[-1]
            en_toks = [t.strip().lower() for t in body.split(",") if t.strip()]
    except Exception:  # noqa: BLE001
        en_toks = []
    return any(t in hay for t in en_toks)


def _section_body(prompt: str, name: str) -> str:
    """取 H3 提示词里某一段的正文（下一个顶层段名之前的内容）

    ⚠️ 段名必须以**行首 ``name:`` 独占一行**识别（与 build_ref2va 的输出格式一致），
    否则正文里出现的同名英文词会被误当作段边界。找不到段名时返回空串。
    """
    lines = str(prompt or "").split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().lower().rstrip(":：") == name.lower():
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].strip().lower().rstrip(":：") in (
                h3_prompt_kit.REF_SECTIONS + h3_prompt_kit.BASE_SECTIONS):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def _dialogue_texts(ctx) -> List[str]:
    """取镜头台词原文（用于检测「台词被写进画面提示词」）"""
    out: List[str] = []
    if not isinstance(ctx, dict):
        return out
    for d in (ctx.get("dialogue") or []):
        t = _txt(d.get("text") if isinstance(d, dict) else d)
        if len(t) >= _MIN_LEAK_LEN:
            out.append(t)
    return out


def _tidy(text: str) -> str:
    """清掉自愈后留下的重复/混排标点与多余空白

    ⚠️ 必须把**混排标点**也归一：删掉一个词后常留下「，,。」这种全角+半角混排，
    只按「同一字符重复」的规则去重是抓不到的（`([。；，])\\1+` 匹配不到「，,。」）。
    """
    s = re.sub(r"[ \t]{2,}", " ", _txt(text))
    for _ in range(3):
        # 任意 ≥2 个标点组成的串 → 归一到「其中最重的那个终止符」
        s = _PUNCT_RUN.sub(lambda m: "。" if any(c in m.group(0) for c in "。！？!?；") else "，", s)
    s = re.sub(r"[，、,;]\s*([。；！？!?])", r"\1", s)
    s = re.sub(r"^\s*[，、,;。；]+", "", s)
    s = re.sub(r"[，、,;\s]+$", "", s)
    return s.strip()


def _has_shot_content(ctx) -> bool:
    """镜头是否自带画面内容（三选一有值即算）"""
    if not isinstance(ctx, dict):
        return True   # 没有镜头上下文（例如手工传提示词来预检）→ 不据此判致命
    return bool(_txt(ctx.get("description")) or _txt(ctx.get("visual_detail"))
                or _txt(ctx.get("storyboard_prompt_zh")))


# --------------------------------------------------------------------------- #
# 分镜图提示词检查
# --------------------------------------------------------------------------- #

def _has_section(text: str, name: str) -> bool:
    """提示词里是否存在**真正的**段标题（行首的 ``NAME:``）

    ⚠️ 不能用 ``name in text``：段名是另一个单词的尾串时会被误判存在
    （``PRESERVE:`` 是 ``XPRESERVE:`` 的子串；``IDENTITY:`` 也可能出现在正文里）。
    行首锚定 + 允许前置空白，与 build_storyboard_prompt 的输出格式一致。
    """
    if not text or not name:
        return False
    pat = re.compile(r"^[ \t]*" + re.escape(name), re.MULTILINE | re.IGNORECASE)
    return bool(pat.search(text))


def _has_any(text: str, *marks: str) -> bool:
    """任一标记命中（用于兼容新旧两种协议写法的段落判存在）"""
    return any(m in (text or "") for m in marks if m)


def _new_protocol(text: str) -> bool:
    """提示词是否用 Qwen-Image-2.1 官方 <imageN> 协议生成

    判据用 ``TASK:`` + ``PRESERVE:`` 两个只在官方协议里出现的段名 —— 存量项目按旧中文
    分节生成，若按新标准去要求它们，会整批假红（生成端未改、质检端先改）。
    因此本层的「结构类」判定按协议分流：新协议按新骨架判、旧协议按旧骨架判。
    """
    t = text or ""
    return SB_MARK_TASK in t or SB_MARK_PRESERVE in t


def _grid_zh_protocol(text: str) -> bool:
    """提示词是否用中文「逐格写死」九宫格范式（storyboard_grid_main，2026-10-07）。

    2026-10-09 实跑暴露：它是**第三种**骨架 —— 既不含英文官方段名（TASK: / PRESERVE:），
    也不含旧中文分节段名（【画面内容】/「景别（必须严格遵守）」）。
    原先只分「新协议 / 存量」两支，于是它被误判成存量 → 每镜假红 2 条
    （实跑实测：10 条教训全部栽在这上面）。

    ⚠️ 不能把它并进 _new_protocol：那会让质检改用**英文段名**去要求它
    （TASK / PRESERVE / SCENE AND ACTION…），issues 反而从 2 条涨到 7 条（已实测）。
    故这里单列一支，只按它自己真实存在的骨架判。
    """
    t = text or ""
    return (SB_MARK_GRID_ZH_LAYOUT in t) and (SB_MARK_GRID_ZH_PANEL in t)


def _ref_tag_indices(text: str) -> List[int]:
    """提示词里出现的 <imageN> 编号（去重升序）"""
    got = sorted({int(m.group(1)) for m in SB_IMAGE_TAG_RE.finditer(text or "")})
    return got


def _check_storyboard(prompt: str, ctx, style, ref_count: Optional[int] = None,
                      **_kw) -> Dict[str, Any]:
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空，无任何可用信息")
        return {"issues": issues, "fatal": fatal}

    if not _has_shot_content(ctx):
        fatal.append("镜头缺少画面描述（description / visual_detail / storyboard_prompt_zh 均为空）")

    new_proto = _new_protocol(prompt)
    grid_zh = _grid_zh_protocol(prompt)

    if grid_zh:
        # ---------- 中文「逐格写死」九宫格范式（storyboard_grid_main）----------
        # 2026-10-09：第三支骨架。只按它**真实存在**的结构判，不套英文段名、也不套 legacy 段名。
        if SB_MARK_GRID_ZH_PANEL not in prompt:
            issues.append("九宫格逐格版缺少「分镜N（景别，色调）：」逐格清单：9 格无从区分，易整片雷同")
        if "分镜9（" not in prompt:
            issues.append("九宫格逐格版不足 9 格：3×3 布局与左下角数字标注会断档")
        if "格间差异" not in prompt:
            issues.append("九宫格逐格版缺少「格间差异」硬约束：模型倾向复刻同一构图")
    elif new_proto:
        # ---------- Qwen-Image-2.1 官方 <imageN> 协议 ----------
        if not _has_section(prompt, SB_MARK_PRESERVE) \
                and SB_MARK_PRESERVE_LEGACY not in prompt:
            issues.append("缺少 PRESERVE 段：未声明「未指定内容保持不变」，画面易整体重画")
        elif SB_PRESERVE_BLANKET not in prompt:
            issues.append("PRESERVE 段缺少 blanket 保留子句（Keep all untargeted content "
                          "unchanged）：逐项罗列会重复触发生成")
        if not _has_section(prompt, SB_MARK_TASK):
            issues.append("缺少 TASK 段：景别/机位硬约束不受控，容易跑偏成中景")
        # ⚠️ 「本镜未指定景别」是**合法的第三种状态**（camera 只给了机位/运镜，如
        #    「俯拍缓推」）：此时提示词里不应有任何景别词，交还画面描述决定取景。
        #    若按「必须有景别硬约束」判，这类镜头会整批假红（历史：曾把 43% 误判不合格）。
        if not _has_any(prompt, SB_MARK_FRAMING_NEW, SB_MARK_FRAMING_LEGACY) \
                and SB_MARK_FRAMING_UNSPEC not in prompt:
            issues.append("缺少 FRAMING 硬约束：景别不受控，容易跑偏成中景")
        if SB_MARK_CONTENT not in prompt:
            issues.append("缺少 SCENE AND ACTION 段：模型只能自行想象画面")
        if not _has_any(prompt, SB_MARK_STYLE, SB_MARK_STYLE_LEGACY):
            issues.append("缺少 STYLE 段：画风会漂移到参考图或模型默认风格")
        elif not style_declared(prompt, style) and SB_MARK_STYLE in prompt:
            issues.append("未声明画面风格：画风会漂移到参考图或模型默认风格")

        # 参考图协议：官方要求 <imageN> 显式编号 + 每张图唯一职责
        if ref_count:
            if SB_MARK_CANVAS not in prompt:
                issues.append("缺少 PRIMARY CANVAS 段：未指定哪张参考图作主要画布")
            tags = _ref_tag_indices(prompt)
            if not tags:
                issues.append("参考图未用 <imageN> 编号引用：官方明确要求显式编号，"
                              "自然语言指代（如「第一张图」）会产生歧义")
            else:
                bad = [n for n in tags if n < 1 or n > ref_count]
                if bad:
                    issues.append(
                        f"提示词引用了不存在的参考图编号 {bad}（本镜实际只有 {ref_count} 张）："
                        f"模型找不到对应参考图，该条约束会静默失效")
                if SB_MARK_IDENTITY in prompt and SB_MARK_IDENTITY_FROM_REF not in prompt:
                    issues.append("IDENTITY 段未把身份指向参考图（缺少 "
                                  "「Preserve the exact identity from <imageN>」）："
                                  "模型会重新生成一张脸，降低 likeness")
            if not _has_any(prompt, SB_MARK_REF_USAGE, SB_MARK_REF_USAGE_LEGACY):
                issues.append("给了参考图但未声明各图职责（REFERENCE ROLES）："
                              "模型不知道每张图该参考什么")
        elif SB_MARK_NO_REF not in prompt and SB_MARK_NO_REF_LEGACY not in prompt:
            issues.append("未声明「本镜无参考图」：模型可能凭空套用模板里的参考图")

        # 3D 导演台构图基准图（<image1>）：带了图却没声明职责 → 模型会把人偶当身份基准
        if isinstance(ctx, dict) and ctx.get("_blocking_ref"):
            if not _has_section(prompt, SB_MARK_BLOCKING):
                issues.append("带了 3D 导演台构图基准图，但提示词缺少 COMPOSITION BASELINE "
                              "段：未声明「只照它的构图摆、不得复刻人偶外观」")

        # 官方禁令：禁止用文字重述五官（重述会让模型「重新画一个符合描述的人」）
        if SB_MARK_IDENTITY in prompt and re.search(
                r"(脸型|五官|鼻子|眼睛|双眼皮|颧骨|下巴)", prompt):
            issues.append("提示词用文字重述了五官细节：官方明确要求身份直接指向参考图，"
                          "重述面部会降低 likeness（应写 Preserve the exact identity from <imageN>）")

        # 自然语言指代参考图（官方禁用）
        vague = SB_VAGUE_REF_RE.search(prompt)
        if vague:
            issues.append(f"用自然语言指代参考图「{vague.group(0)}」："
                          f"官方要求改用 <imageN> 显式编号")
    else:
        # ---------- 存量：旧中文分节协议（不按新标准判，避免整批假红）----------
        if SB_MARK_CONTENT_LEGACY not in prompt:
            issues.append("缺少「动作与画面内容」段：模型只能自行想象画面")
        if SB_MARK_FRAMING_LEGACY not in prompt:
            issues.append("缺少「景别（必须严格遵守）」段：景别不受控，容易跑偏成中景")
        if ref_count and SB_MARK_REF_USAGE_LEGACY not in prompt:
            issues.append("给了参考图但未声明「参考图用途」：模型不知道每张图该参考什么")
        if SB_MARK_STYLE_LEGACY not in prompt and not style_declared(prompt, style):
            issues.append("未声明画面风格：画风会漂移到参考图或模型默认风格")

    # 2026-10-09：中文逐格版的防文字措辞有两种形态 ——
    #   · 有台词 → 注入「严禁出现任何台词文字或字幕」；
    #   · 无台词 → 注入「均不出现开口说话的口型」（此时本就不会画字，**不该**再要求防文字句）。
    # 逐格版按 grid_zh 单独判，避免对无台词镜假红。
    if grid_zh:
        if not _has_any(prompt, SB_MARK_NO_TEXT, SB_MARK_NO_TEXT_LEGACY,
                        SB_MARK_GRID_ZH_NO_TEXT, "不出现开口说话的口型"):
            issues.append("缺少「不得出现文字/字幕/水印」约束：生成图易带字幕或水印")
    elif not _has_any(prompt, SB_MARK_NO_TEXT, SB_MARK_NO_TEXT_LEGACY):
        issues.append("缺少「不得出现文字/字幕/水印」约束：生成图易带字幕或水印")

    # 台词被写进画面提示词 —— 模型会把台词当字幕画出来（硬缺陷，可自愈）
    for t in _dialogue_texts(ctx):
        if t in prompt:
            issues.append(f"台词文本被写进画面提示词（会被渲染成字幕）：{t[:20]}")
    if _INSTRUCTION_RE.search(prompt):
        issues.append("含「字幕/文字/水印」类指令：会被模型当作要绘制的画面内容")
    hits = [w for w in _QUALITY_FLUFF if w.lower() in prompt.lower()]
    if hits:
        issues.append("含质量类空词（无信息量且挤占语义）：" + "、".join(hits[:3]))
    return {"issues": issues, "fatal": fatal}


def _repair_storyboard(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    repairs: List[str] = []
    out = prompt

    for t in _dialogue_texts(ctx):
        if t in out:
            out = out.replace(t, "")
            repairs.append("移除画面提示词里的台词文本（避免被渲染成字幕）")
    if _INSTRUCTION_RE.search(out):
        out = _INSTRUCTION_RE.sub("", out)
        repairs.append("移除「字幕/文字/水印」类指令")
    for w in _QUALITY_FLUFF:
        if w.lower() in out.lower():
            out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
            repairs.append("移除质量类空词")

    # 官方协议：自然语言指代参考图 → 统一改写成 <imageN> 编号。
    # 只在「新协议」提示词上做（旧协议没编号语义，硬改反而错位）。
    if _new_protocol(out) and SB_VAGUE_REF_RE.search(out):
        out = SB_VAGUE_REF_RE.sub("<image1>", out)
        repairs.append("自然语言指代参考图统一改写为 <imageN> 编号")

    out = _tidy(out)

    if not style_declared(out, style):
        new = style_kit.with_style(out, style)
        if new and new != out:
            out = new
            repairs.append("补写画面风格声明")

    if not _has_any(out, SB_MARK_NO_TEXT, SB_MARK_NO_TEXT_LEGACY):
        out = out.rstrip()
        if _new_protocol(out):
            out = f"{out}\nThe image must not contain any text, subtitles, dialogue " \
                  f"text, watermark, logo or sign."
        else:
            sep = "" if out.endswith(("。", "！", "？", "；")) else "。"
            out = f"{out}{sep}画面中不得出现任何文字、字幕、台词文本、水印、logo 或标识。"
        repairs.append("补写「不得出现文字/字幕/水印」约束")
    return out, repairs


# --------------------------------------------------------------------------- #
# H3 视频提示词检查
# --------------------------------------------------------------------------- #

def _check_h3(prompt: str, ctx, style, expect_refs: Optional[bool] = None,
              **_kw) -> Dict[str, Any]:
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空，H3 无法生成")
        return {"issues": issues, "fatal": fatal}

    v = h3_prompt_kit.validate(prompt)
    if not v.get("valid"):
        miss = "、".join(v.get("missing") or []) or "未知"
        # 结构缺段是出片跑偏的根因（历史缺陷：裸英文顶掉结构化构建器，211 镜 0 合规）
        fatal.append(f"H3 结构不合规（{v.get('mode')} 模式缺段：{miss}）")

    marks = [(int(m.group(1)), int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0)
             for m in _TS_RE.finditer(prompt)]
    # 2026-09-24 对齐本地模板：后续镜的时间码内嵌在句子里（``At 00:03.500, ...``），
    # 不再跟在 ``[Shot N]`` 后面。故 ``marks`` 为空时退化用裸时间码判时间轴是否存在。
    bare = [(int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 1000.0)
            for m in _BARE_TS_RE.finditer(prompt)]
    # 单节拍提示词（时长 ≤ BEAT_MAX_SEC）本身**不需要**时间码：只有 ``[Shot 1]``，
    # 起点天然是 00:00.000，模板同样不写时间码。只有在出现 ≥2 个节拍时，时间码
    # 才是判断时间轴是否连续的必需信息。
    #
    # ⚠️ 2026-09-24 二次对齐：多节拍判据必须只看**节拍行**（行首 ``[Shot N]`` / ``At …``），
    # 不能用 ``prompt.count("[Shot ")`` —— retention_analysis 段里的
    # ``(appears in [Shot 1], [Shot 2])`` 归属声明也会被计入，导致**单节拍镜被
    # 误判成多节拍**，进而误报「缺少时间码」（实测踩过）。
    beat_lines = [ln.lstrip() for ln in prompt.split("\n")
                  if ln.lstrip().startswith("[Shot ") or ln.lstrip().startswith("At ")]
    multi_beat = len(beat_lines) > 1 or "camera cuts to" in prompt
    if not marks and not bare and multi_beat:
        issues.append("缺少 [Shot N] MM:SS.mmm 时间码：节拍无时间轴，长镜头会空转")
    elif marks:
        nums = [n for n, _ in marks]
        if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
            issues.append(f"时间码编号不连续（{[n for n in nums][:8]}）：节拍顺序被打乱")
        times = [t for _, t in marks]
        if times[0] > 0.001:
            issues.append("首个时间码不是 00:00.000：视频开头会缺画面")
        if any(b < a for a, b in zip(times, times[1:])):
            issues.append("时间码非递增：节拍时间轴倒流")
    else:
        # 新格式：首镜无时间码（隐含 00:00.000），后续镜为 At 时间码。
        # 断言递增即可；首码 00:00.000 由「首镜 [Shot 1] 开头」隐含保证。
        if any(b < a for a, b in zip(bare, bare[1:])):
            issues.append("时间码非递增：节拍时间轴倒流")

    if _dialogue_texts(ctx) and not _SPOKEN_RE.search(prompt):
        issues.append("镜头有台词但缺少「开口说话 + 口型自然开合」动作句：口型与配音可能对不上")

    # ⚠️ 2026-09-24 二次对齐模板：台词若只被 <d> 包住、**台词前没有「开口说话」的
    # 动作描述**，H3 会把台词当成背景配音、画面里人物嘴唇不动（实测现象）。
    # 模板每句台词前都有 ``speaks — a clear … voice … at a measured … rate``，
    # 这正是驱动口型的依据。这里把它升级为硬性检查（原先只在守卫里断言）。
    # 2026-09-26 P0-1：新格式（speaks … voice）已含在 _SPOKEN_RE 里；此分支只针对
    # **旧格式兼容**（仅命中 ``(S1): <d>台词</d>`` 而无 speaks 动作）补报。
    if (_dialogue_texts(ctx) and _SPOKEN_RE.search(prompt)
            and not _SPEAK_ACTION_RE.search(prompt)):
        issues.append("台词缺少「开口说话」动作描述（speaks — … voice …）："
                      "人物可能只出配音、嘴唇不动")

    if expect_refs and "<Picture 1>" not in prompt:
        issues.append("期望使用参考图但缺少 <Picture 1> 标签：H3 拿不到参考图语义")

    if not style_declared(prompt, style):
        issues.append("未声明画面风格：全片画风可能被参考图带偏")
    # ⚠️ 2026-09-24：**取消**「必须含无文字/字幕约束」这条检查。对齐本地模板后
    # 提示词里不再写「严禁出现文字/字幕/水印」——该措辞会把「字幕/文字」两个词
    # 引入提示词，反而诱导 H3 把台词画成字幕（实测视频出字幕）。
    # 改为检查「无台词的节拍是否显式声明 no one speaks / no voice-over（或旧 No dialogue）」，
    # 这才是防字幕/防画外音的有效手段。
    if not _SPOKEN_RE.search(prompt) and not _NO_DIALOGUE_RE.search(prompt):
        issues.append("无台词且未显式声明「no one speaks / no voice-over」："
                      "模型可能自补台词画成字幕，或把画面描述念成画外音")

    # ------------------------------------------------------------------ #
    # 2026-09-24 二次对齐本地模板：以下四项是模板 10 段**无一例外**都有的格式特征，
    # 缺失即为「与参考工作流不一致」。全部按 issue 报（不阻断），便于观察灰度。
    # ⚠️ 中文格式一律**不报**（存量项目按旧格式生成、重出视频时不应被新规则判缺陷）。
    # ------------------------------------------------------------------ #
    v_mode = (v.get("mode") or "") if isinstance(v, dict) else ""
    if v_mode == "ref":
        # ① Ref2VA 模式声明（模板段段以 [reference generation] 起头）
        if "[reference generation]" not in prompt:
            issues.append("summary 缺「[reference generation]」模式声明："
                          "未显式告知 H3 本段以参考图为基础生成")
        # ② retention_analysis 的保留句式
        if "fully_preserved" not in prompt:
            issues.append("retention_analysis 未使用模板的「fully_preserved」句式："
                          "保留项表述与参考工作流不一致")
    # ③ detailed_description 首句风格声明（模板：The target video uses … style …）。
    # ⚠️ 必须同时放行历史中文写法（「全片画面风格统一为…」/段首即 `[Shot 1]`），
    # 否则存量项目会统一假红 —— 只在「已经写了英文描述、却没有风格首句」时报。
    _dd = _section_body(prompt, "detailed_description")
    if _dd and not _STYLE_OPENING_RE.search(_dd) and not _DD_CN_STYLE_RE.search(_dd) \
            and not re.search(r"(?m)^\[Shot \d+\]\s*[^\n]{0,12}[：:]?\s*[\u4e00-\u9fff]", _dd):
        issues.append("detailed_description 未以风格句（The target video uses … style …）开头："
                      "画风锚定权重不足")
    # ④ 景别必须是英文短语；模板不出现「[Shot 1] 中景：」这类中文景别
    if _CN_CAMERA_RE.search(prompt):
        issues.append("镜头行使用中文景别（如「[Shot 1] 中景：」）："
                      "模板为英文景别短语（A medium shot / a close-up）")
    # ⑤ 风格句叠词（2026-10-04）：style_suffix_en 产出「Chinese animated style」，
    #   风格首句再拼一个 " style" → "Chinese animated style style"。实测出现在导出
    #   工作流的每一段提示词里（确定性可判，且自愈可修）。
    if re.search(r"\bstyle\s+style\b", prompt, re.IGNORECASE):
        issues.append("风格句出现重复的「style style」叠词：风格声明冗余，削弱风格权重")
    return {"issues": issues, "fatal": fatal}


def _repair_h3(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    """H3 提示词只做「安全追加」类自愈。

    结构缺段（六段不全）**不在这里重建** —— 重建需要参考图语义（每张图的用途），
    只有生成端 ``comfyui_client.resolve_h3_prompt`` 掌握。

    ⚠️ 2026-09-24：**不再补写**「严禁出现文字/字幕/水印」约束。对齐本地手跑模板后，
    该约束被视为有害（在提示词里引入「字幕/文字」两个词，反而诱导 H3 画出字幕）。
    现在只补「no one speaks / no voice-over」这类与上下文无关、且真正有利于防字幕/防旁白的固定句。
    """
    repairs: List[str] = []
    out = prompt
    # ⭐ 2026-10-04：折叠风格句叠词「… style style」→「… style」。
    #   纯确定性文本规整，不改变语义，属最安全的一类自愈。
    _dedup = re.sub(r"\bstyle(\s+)style\b", r"style", out, flags=re.IGNORECASE)
    if _dedup != out:
        out = _dedup
        repairs.append("折叠风格句重复的「style style」叠词")
    if out and not _SPOKEN_RE.search(out) and not _NO_DIALOGUE_RE.search(out):
        # 无台词镜：在 detailed_description 段内**最后一条 [Shot 节拍** 上补
        # 「no one speaks / no voice-over」。
        # ⚠️ 不能简单取「下一段名之前的最后一行」—— 那行往往是全片风格声明
        #（「全片画面风格统一为…」），补在后面既语义错位、也识别不到节拍。
        # 这里从段尾向前找最近一条真正的节拍行（以 ``[Shot`` 或 ``At `` 开头）。
        lines = out.split("\n")
        idx = None
        for i, ln in enumerate(lines):
            if ln.strip().lower().startswith("detailed_description"):
                idx = i
                break
        if idx is not None:
            end = len(lines)
            for j in range(idx + 1, len(lines)):
                if lines[j].strip().lower().rstrip(":：") in (
                        h3_prompt_kit.REF_SECTIONS + h3_prompt_kit.BASE_SECTIONS):
                    end = j
                    break
            beat = None
            for j in range(end - 1, idx, -1):
                s = lines[j].lstrip()
                if s.startswith("[Shot") or s.startswith("At "):
                    beat = j
                    break
            if beat is not None:
                lines[beat] = (lines[beat].rstrip()
                               + " No one in the frame speaks; there is no voice-over narration.")
                out = "\n".join(lines)
                repairs.append("补写「no one speaks / no voice-over」（防模型自补台词/念旁白）")
        else:
            out = out.rstrip() + " No one in the frame speaks; there is no voice-over narration."
            repairs.append("补写「no one speaks / no voice-over」（防模型自补台词/念旁白）")

    # ------------------------------------------------------------------ #
    # 2026-09-24 二次对齐：补两处「安全追加」型的模板特征。
    # ⚠️ 两者都只在**提示词本来就是英文格式**时补 —— 中文格式的存量提示词不碰
    #（在中文句里塞英文标记反而更乱，且存量数据不应在重出视频时被改写）。
    # ------------------------------------------------------------------ #
    verdict = h3_prompt_kit.validate(out)
    is_ref = verdict.get("mode") == "ref"
    looks_en = bool(_STYLE_OPENING_RE.search(_section_body(out, "detailed_description"))
                    or re.search(r"(?m)^\[Shot \d+\] [A-Z]", out))
    if is_ref and looks_en and "[reference generation]" not in out:
        # ① 在 summary 段正文前补模式声明（模板段段都有）
        lines = out.split("\n")
        for i, ln in enumerate(lines):
            if ln.strip().lower().rstrip(":：") == "summary":
                for j in range(i + 1, len(lines)):
                    if lines[j].strip():
                        lines[j] = "[reference generation] " + lines[j].lstrip()
                        repairs.append("补写「[reference generation]」模式声明")
                        break
                    if lines[j].strip().lower().rstrip(":：") in (
                            h3_prompt_kit.REF_SECTIONS + h3_prompt_kit.BASE_SECTIONS):
                        break
                break
        out = "\n".join(lines)
    if is_ref and looks_en and "fully_preserved" not in out:
        # ② retention_analysis 的保留句式：把中文「必须保留 X 中的：Y」换成模板句式。
        #    只做**段落级**保守改写，识别不出格式时不动（宁可不修，不可改坏）。
        lines = out.split("\n")
        start = end = None
        for i, ln in enumerate(lines):
            if ln.strip().lower().rstrip(":：") == "retention_analysis":
                start = i + 1
                break
        if start is not None:
            end = len(lines)
            for j in range(start, len(lines)):
                if lines[j].strip().lower().rstrip(":：") in (
                        h3_prompt_kit.REF_SECTIONS + h3_prompt_kit.BASE_SECTIONS):
                    end = j
                    break
            conv: List[str] = []
            changed = False
            for ln in lines[start:end]:
                m = re.match(r"^\s*-\s*必须保留\s*(<Picture \d+>)\s*中的[：:]\s*(.+?)\s*$", ln)
                if m:
                    conv.append(f"{m.group(1)}: fully_preserved - {m.group(2)}")
                    changed = True
                else:
                    conv.append(ln)
            if changed:
                lines[start:end] = conv
                out = "\n".join(lines)
                repairs.append("retention_analysis 改写为模板的 fully_preserved 句式")
    return out, repairs


# --------------------------------------------------------------------------- #
# 资产参考图提示词检查
# --------------------------------------------------------------------------- #

def _check_asset(prompt: str, ctx, style, **_kw) -> Dict[str, Any]:
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空：该资产会退化为无参考信息的随机外观")
        return {"issues": issues, "fatal": fatal}

    if len(prompt) < _ASSET_MIN_LEN:
        issues.append(f"提示词过短（{len(prompt)} 字符）：不足以描述主体特征")

    if not style_declared(prompt, style):
        issues.append("未声明风格：资产画风会与全片其它资产不一致")

    # 风格双写：与尾帧检查共用同一份判定（见 _style_dup_token）
    dup = _style_dup_token(prompt, style)
    if dup:
        issues.append(f"风格被重复声明（「{dup}」出现 {prompt.count(dup)} 次）：会放大该风格权重")

    hits = _fluff_hits(prompt)
    if hits:
        issues.append("含质量类空词（无信息量且挤占风格语义）：" + "、".join(hits[:3]))
    return {"issues": issues, "fatal": fatal}


def _repair_asset(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    repairs: List[str] = []
    out = prompt
    for w in _QUALITY_FLUFF:
        if w.lower() in out.lower():
            out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
            repairs.append("移除质量类空词")
    out = _tidy(out)
    # with_style 内部先 _collapse_style（自愈双写）再按标记判幂等 → 一次同时修「缺失」与「双写」
    # ⚠️ 资产参考图走 with_reference_style（2026-10-07）：剥离色调/光影 token 并补颜色保真条款，
    #    否则自愈会把「色调灰蓝压抑」重新写回角色三视图 → 皮肤染蓝。
    new = style_kit.with_reference_style(out, style)
    if new and new != out:
        out = new
        repairs.append("规范化风格声明（补齐缺失 / 合并重复）")
    return out, repairs


# --------------------------------------------------------------------------- #
# 尾帧提示词检查
# --------------------------------------------------------------------------- #

def _fluff_hits(prompt: str) -> List[str]:
    """命中质量类空词（中英混排，大小写不敏感）"""
    low = (prompt or "").lower()
    return [w for w in _QUALITY_FLUFF if w.lower() in low]


def _style_dup_token(prompt: str, style) -> str:
    """返回「被重复声明 ≥2 次」的风格词，没有则返回 ""

    典型症状是「模型自写风格词 + 程序追加风格句」叠加，同一风格写两遍会放大其权重。
    """
    clause = _style_clause(style)
    if not clause:
        return ""
    for tok in clause.split("：", 1)[-1].split("，"):
        tok = tok.strip()
        if tok and prompt.count(tok) >= 2:
            return tok
    return ""


def _check_keyframe(prompt: str, ctx, style, **_kw) -> Dict[str, Any]:
    """尾帧提示词检查

    尾帧是「以本镜分镜图为首帧参考的图生图」，链式模式下还会直接成为**下一镜的首帧**
    —— 一张坏图会顺着链污染后面所有镜头，所以这一层是源头把关。
    """
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空：尾帧会退化为随机画面，并顺着链污染后续镜头的首帧")
        return {"issues": issues, "fatal": fatal}

    if len(prompt) < _ASSET_MIN_LEN:
        issues.append(f"提示词过短（{len(prompt)} 字符）：不足以约束动作推进的终点")

    # ① 锚定参考图：不写这句，模型会换脸、换服装、换场景
    if KF_MARK_ANCHOR not in prompt:
        issues.append("未声明「保持参考图外观/服装/场景/画风不变」：尾帧极易换脸换服装")

    # ② 取景连贯：机位/景别一变，首尾帧之间就插不出连续动作
    if KF_MARK_FRAMING not in prompt:
        issues.append("未要求机位/焦段/构图与首帧连贯：尾帧可能切换视角")

    # ③ 链式承接：首帧取自上一镜尾帧时缺这句，模型会当成同镜的两个时间点而拒绝推进动作
    if isinstance(ctx, dict) and ctx.get("chained") and KF_MARK_CHAIN not in prompt:
        issues.append("链式尾帧未写明「承接上一镜」：模型会当成同镜两个时间点，动作推不动")

    if not style_declared(prompt, style):
        issues.append("未声明风格：尾帧画风会与分镜图跑偏")

    dup = _style_dup_token(prompt, style)
    if dup:
        issues.append(f"风格被重复声明（「{dup}」出现 {prompt.count(dup)} 次）：会放大该风格权重")

    if H3_NO_TEXT not in prompt:
        issues.append("未禁止画面出现文字：台词/字幕会被渲染进画面")

    hits = _fluff_hits(prompt)
    if hits:
        issues.append("含质量类空词（无信息量且挤占风格语义）：" + "、".join(hits[:3]))
    return {"issues": issues, "fatal": fatal}


def _repair_keyframe(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    """尾帧提示词自愈：只修文本层可确定的项

    ⚠️ 「锚定参考图」「取景连贯」这类**语义**缺失不在这里补：随手补一句通用措辞会掩盖
    真正的问题（该镜的动作推进根本没被描述清楚），这时应当重建而不是打补丁 —— 见
    ``_rebuild_hint`` 给出的重建器。
    """
    repairs: List[str] = []
    out = prompt
    for w in _QUALITY_FLUFF:
        if w.lower() in out.lower():
            out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
            repairs.append("移除质量类空词")
    out = _tidy(out)

    if not style_declared(out, style):
        new = style_kit.with_style(out, style)
        if new and new != out:
            out = new
            repairs.append("补写画面风格声明")

    if out and H3_NO_TEXT not in out:
        out = out.rstrip()
        sep = "" if out.endswith(("。", "！", "？", "；")) else "。"
        out = f"{out}{sep}{H3_NO_TEXT_FULL}"
        repairs.append("补写「严禁出现文字/字幕/水印」约束")
    return out, repairs


# --------------------------------------------------------------------------- #
# 配音台词检查
# --------------------------------------------------------------------------- #

def _audio_word_count(text: str) -> int:
    """有效字数（去掉空白、标点、符号后剩下「会发声」的字符数）"""
    return len(_AUDIO_WORD_RE.findall(str(text or "")))


def _audio_leak_hits(text: str) -> List[str]:
    """命中的结构化残留（返回人类可读的标签）"""
    return [label for label, rx in _AUDIO_LEAK_PATTERNS if rx.search(text or "")]


def _audio_stage_brackets(text: str) -> List[str]:
    """括号里的舞台指示片段（不是要念的台词）"""
    hits: List[str] = []
    for m in _AUDIO_BRACKET_RE.finditer(text or ""):
        inner = (m.group(1) or "").strip()
        low = inner.lower()
        if not inner:
            continue
        if any(h.lower() in low for h in _AUDIO_STAGE_HINTS):
            hits.append(inner)
    return hits


def _check_audio(prompt: str, ctx, style, **_kw) -> Dict[str, Any]:
    """配音台词检查

    ⚠️ 这一层判的不是「提示词写得好不好」，而是「这句台词拿去念会不会出问题」——
    台词会被 TTS 逐字念出来，所以多一个字符、少一个字都是实质缺陷。
    """
    issues: List[str] = []
    fatal: List[str] = []

    text = _txt(prompt)
    if not text:
        fatal.append("台词为空：该句会合成出静音片段（或直接被跳过），"
                     "成片对应镜头整段无声")
        return {"issues": issues, "fatal": fatal}

    words = _audio_word_count(text)
    if words == 0:
        fatal.append("台词只有标点/符号（无任何可发声字符）：TTS 会产出空音频或直接失败")
        return {"issues": issues, "fatal": fatal}

    if words < _AUDIO_MIN_WORDS:
        issues.append(f"有效字数仅 {words} 个：信息量过低，多数情况下是脏数据残留")

    leaks = _audio_leak_hits(text)
    if leaks:
        issues.append("含结构化残留（会被 TTS 逐字念出来）：" + "、".join(leaks))

    stage = _audio_stage_brackets(text)
    if stage:
        issues.append("含舞台指示/表演提示（会被念出来）：「" + "」「".join(stage[:3]) + "」")

    if _AUDIO_INVISIBLE_RE.search(text):
        issues.append("含零宽字符或方向控制符：肉眼不可见，但会污染文本与时长统计")

    if _AUDIO_EMOJI_RE.search(text):
        issues.append("含 emoji / 特殊符号：TTS 无法发声，可能被读成符号名称")

    if words > _AUDIO_MAX_CHARS:
        issues.append(f"单句过长（{words} 个有效字 > {_AUDIO_MAX_CHARS}）："
                      f"易被断句或触发合成超时，建议按语气拆句")

    # 情绪：剧本每镜都有 emotion，但只有 VoiceDesign 模式才真的按 instruct 控语气；
    # preset（CustomVoice）会把 instruct 当备注忽略 → 「情绪化配音」静默失效。
    if isinstance(ctx, dict):
        emotion = _txt(ctx.get("emotion"))
        mode = _txt(ctx.get("voice_mode")).lower()
        if emotion and emotion.lower() not in [e.lower() for e in _AUDIO_NEUTRAL_EMOTIONS] \
                and mode and mode != "design":
            issues.append(f"该镜情绪为「{emotion}」但音色模式是 {mode}："
                          f"instruct 会被忽略，配音听不出情绪")

        # 说话人兜底：剧本没写 speaker 时 build_dub_plan 会落到「旁白」，
        # 于是角色台词被旁白音色念出来 —— 音色错配，且前端看起来「有配音」。
        if ctx.get("speaker_fallback"):
            issues.append("该句未指定说话人，将使用「旁白」音色兜底："
                          "角色台词会被旁白念，音色错配")

    if not style_declared(text, style) and isinstance(ctx, dict) and ctx.get("require_style"):
        issues.append("未声明配音风格：多角色音色可能不统一")

    return {"issues": issues, "fatal": fatal}


def _repair_audio(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    """配音台词自愈：只剥「确定不是台词内容」的噪音，绝不改写台词本身。

    ⚠️ 语义层缺陷（说话人缺失、情绪模式不对）**不在文本层修** —— 那需要改音色配置，
    属于调用方的职责；随手补一句会掩盖真正的问题（见 ``_rebuild_hint``）。
    """
    repairs: List[str] = []
    out = str(prompt or "")

    if _AUDIO_INVISIBLE_RE.search(out):
        out = _AUDIO_INVISIBLE_RE.sub("", out)
        repairs.append("移除零宽字符/方向控制符")

    if _AUDIO_EMOJI_RE.search(out):
        out = _AUDIO_EMOJI_RE.sub("", out)
        repairs.append("移除 emoji/特殊符号")

    if _audio_leak_hits(out):
        for _label, rx in _AUDIO_LEAK_PATTERNS:
            out = rx.sub("", out)
        repairs.append("剥离结构化残留（语言标签/镜头标签/时间码/说话人标记）")

    # 舞台指示：只剥「括号内含表演提示词」的括号，正常括号内容（如「（笑）」之外的
    # 专有名词）保持原样，避免误删台词本身
    stage = _audio_stage_brackets(out)
    if stage:
        out = _AUDIO_BRACKET_RE.sub(
            lambda m: "" if any(h.lower() in (m.group(1) or "").lower()
                                for h in _AUDIO_STAGE_HINTS) else m.group(0), out)
        repairs.append("剥离舞台指示/表演提示")

    out = _PUNCT_RUN.sub(lambda m: m.group(0)[0], out)
    out = _tidy(out)
    return out, repairs


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

_CHECKS = {
    "storyboard": _check_storyboard,
    "h3": _check_h3,
    "asset": _check_asset,
    "keyframe": _check_keyframe,
    "audio": _check_audio,
}
_REPAIRS = {
    "storyboard": _repair_storyboard,
    "h3": _repair_h3,
    "asset": _repair_asset,
    "keyframe": _repair_keyframe,
    "audio": _repair_audio,
}


def _score(issues: Sequence[str], fatal: Sequence[str]) -> int:
    s = 100 - 8 * len(issues) - 40 * len(fatal)
    return max(0, min(100, s))


def prompt_qc_ready(cfg) -> bool:
    """预检是否启用。

    ⚠️ 与图片/视频质检不同，这里**不依赖质检接口**：确定性检查零成本、零模型依赖，
    没配质检接口的项目也应该享受这一层保护（默认开启）。
    """
    return bool((cfg or {}).get("prompt_enabled", True))


def prompt_qc_mode(cfg) -> str:
    mode = str((cfg or {}).get("prompt_mode") or DEFAULT_MODE).strip().lower()
    return mode if mode in PROMPT_QC_MODES else DEFAULT_MODE


def check_prompt(kind: str, prompt: str, ctx: dict = None, style: str = "",
                 cfg: dict = None, ref_count: Optional[int] = None,
                 expect_refs: Optional[bool] = None) -> dict:
    """对一条提示词做确定性预检（不做自愈，不改动 prompt）。

    返回 verdict 与图片/视频质检**同构**：``ok / kind / prompt_kind / passed /
    accepted / blocked / score / issues / critical_issues / reason`` —— 便于复用
    已有的闸门、历史落盘与前端展示。
    """
    kind = str(kind or "").strip().lower()
    if kind not in _CHECKS:
        return {"ok": False, "kind": "prompt", "prompt_kind": kind,
                "passed": False, "accepted": False, "blocked": True, "score": 0,
                "issues": [f"未知的提示词类型：{kind}（支持 {'/'.join(PROMPT_KINDS)}）"],
                "critical_issues": [], "reason": "提示词类型不受支持", "skipped": False}

    text = _txt(prompt)
    style = _txt(style) or _txt((ctx or {}).get("style") if isinstance(ctx, dict) else "")
    res = _CHECKS[kind](text, ctx, style, ref_count=ref_count, expect_refs=expect_refs)
    fatal = list(res.get("fatal") or [])
    issues = list(res.get("issues") or [])
    passed = not fatal
    reason = ""
    if fatal:
        reason = "致命缺陷：" + "；".join(fatal[:2])
    elif issues:
        reason = "存在可优化项：" + "；".join(issues[:2])
    out = {
        "ok": True,
        "skipped": False,
        "kind": "prompt",
        "prompt_kind": kind,
        "passed": passed,
        "accepted": passed,
        "blocked": bool(fatal),
        "score": _score(issues, fatal),
        "issues": issues,
        # critical_issues 供 app.py 的 `_qc_gate` 复用（它会把这里的条目强制视为阻断项）
        "critical_issues": fatal,
        "reason": reason,
        "chars": len(text),
        "style": style,
    }
    out["rebuild_hint"] = _rebuild_hint(kind, out, text)
    return out


def repair_prompt(kind: str, prompt: str, ctx: dict = None, style: str = "") -> Tuple[str, List[str]]:
    """确定性自愈（幂等：对同一条提示词重复调用，第二次不再产生修改）。

    返回 ``(自愈后的提示词, 修复项说明)``。
    """
    kind = str(kind or "").strip().lower()
    fn = _REPAIRS.get(kind)
    if fn is None:
        return _txt(prompt), []
    style = _txt(style) or _txt((ctx or {}).get("style") if isinstance(ctx, dict) else "")
    try:
        return fn(_txt(prompt), ctx, style)
    except Exception as e:  # noqa: BLE001
        # 自愈失败不能让生成链断掉：原样返回 + 记录
        logger.warning("提示词自愈失败（%s）：%s", kind, e)
        return _txt(prompt), []


def _rebuild_hint(kind: str, verdict: dict, text: str = "") -> str:
    """结构性缺陷无法在文本层自愈时，告诉调用方该用哪个构建器重建。

    这类缺陷（缺景别骨架、缺参考图语义、H3 缺段、尾帧缺锚定语义）只能由掌握镜头上下文
    与参考图信息的生成端重建 —— 预检不越权去猜，只负责指出「该重建了」。

    ``text`` 是待判定的提示词正文（可选）。按**标记**判而不是按 issue 文案判，
    避免「文案一改、判定就悄悄失效」。
    """
    joined = " ".join([str(x) for x in (verdict.get("issues") or [])]
                      + [str(x) for x in (verdict.get("critical_issues") or [])])
    # 空提示词只能重建（文本层没有任何可修补的东西）
    if not verdict.get("chars"):
        if kind == "storyboard":
            return "comfyui_client.build_storyboard_prompt（用镜头上下文重建分镜图提示词）"
        if kind == "h3":
            return "comfyui_client.resolve_h3_prompt（用参考图语义重建 H3 提示词）"
        if kind == "keyframe":
            return "keyframe.build_end_frame_prompt（用镜头上下文重建尾帧提示词）"
        if kind == "audio":
            return ("从剧本该镜头重新取台词（dialogue_utils.normalize_lines → "
                    "shot.dialogue；本系统不产出旁白，台词是唯一人声来源）")
        return ""
    if kind == "storyboard":
        if any(m in joined for m in (SB_MARK_CONTENT, SB_MARK_CONTENT_LEGACY,
                                     SB_MARK_FRAMING_LEGACY, SB_MARK_FRAMING_NEW,
                                     SB_MARK_REF_USAGE, SB_MARK_REF_USAGE_LEGACY,
                                     SB_MARK_CANVAS, SB_MARK_TASK)):
            return "comfyui_client.build_storyboard_prompt（用镜头上下文重建分镜图提示词）"
    if kind == "h3":
        if verdict.get("critical_issues") or "时间码" in joined:
            return "comfyui_client.resolve_h3_prompt（用参考图语义重建 H3 提示词）"
    if kind == "keyframe" and text:
        # 锚定参考图 / 取景连贯缺失 → 文本层补不出「这一镜的动作推进是什么」，只能重建
        if KF_MARK_ANCHOR not in text or KF_MARK_FRAMING not in text:
            return "keyframe.build_end_frame_prompt（用镜头上下文重建尾帧提示词）"
    if kind == "audio":
        # 空台词 / 只剩标点：文本层没有任何可修补的内容，只能从剧本重新取台词
        if not text or _audio_word_count(text) == 0:
            return ("从剧本该镜头重新取台词（dialogue_utils.normalize_lines → "
                    "shot.dialogue；本系统不产出旁白，台词是唯一人声来源）")
    return ""


def _crit_kind(msg) -> str:
    """致命缺陷的**类型键**：取到第一个中/英文左括号之前的部分。

    为什么不能按整串比：钳制只会让提示词**变短**，被剪掉的是末尾段落，于是同一条致命
    缺陷的**描述细节会变**（H3 的缺段列表由「non_diegetic_music」变成
    「overall_soundscape、non_diegetic_music」），整串相等就匹配不上，会把「原文本来就
    存在的致命缺陷」误判成「本次钳制新增」而降级掉 —— 真致命被洗白（2026-09-22 QA 复验
    P-1 的反例实测）。按类型键比对才能既降级「钳制新增的致命类型」，又保留「原文本就
    存在的致命类型」。
    """
    s = str(msg or "")
    for ch in ("（", "("):
        i = s.find(ch)
        if i >= 0:
            s = s[:i]
    return s.strip()


def preflight(kind: str, prompt: str, ctx: dict = None, style: str = "",
              cfg: dict = None, ref_count: Optional[int] = None,
              expect_refs: Optional[bool] = None) -> dict:
    """生成前预检 + 自愈（**生成链路的推荐入口**）。

    流程：检查 → （模式允许时）自愈 → 复检 → 按模式给放行结论。

    返回::

        {
          "prompt":   <应交给生成端的提示词，可能是自愈后的>,
          "before":   <自愈前的 verdict>,
          "verdict":  <自愈后的 verdict，语义以它为准>,
          "repairs":  [...],
          "accept":   bool,      # 是否放行生成
          "blocked":  bool,      # 是否命中致命缺陷
          "skipped":  bool,      # 预检未启用
          "label":    str,       # 便于日志/前端展示的一句话结论
          "reason":   str,
        }
    """
    cfg = cfg or {}
    text = _txt(prompt)
    mode = prompt_qc_mode(cfg)

    if not prompt_qc_ready(cfg):
        verdict = check_prompt(kind, text, ctx=ctx, style=style, cfg=cfg,
                               ref_count=ref_count, expect_refs=expect_refs)
        verdict["skipped"] = True
        return {"prompt": text, "before": verdict, "verdict": verdict, "repairs": [],
                "accept": True, "blocked": False, "skipped": True,
                "label": "提示词预检未启用（跳过）",
                "reason": "提示词预检未启用（跳过）"}

    # ---- 生成前 LLM 增强（2026-09-30 需求：所有图片/视频生成前先做提示词增强） ----
    # 放在确定性检查之前：增强结果同样要过整套确定性检查 + 自愈 + 钳制。
    # 台词（audio）绝不改写；失败/未配置/骨架不符一律按原文继续（fail-open，
    # 见 prompt_enhance 模块文档）。此层只在预检启用时执行——预检关掉即整层旁路。
    enhance_meta: dict = {}
    if kind in ("storyboard", "asset", "keyframe", "h3") and text:
        try:
            import prompt_enhance  # 函数内导入：避免模块级循环依赖
            _er = prompt_enhance.enhance_prompt(kind, text, ctx=ctx, style=style)
            if _er.get("applied") and _er.get("prompt"):
                text = _er["prompt"]
                enhance_meta = {"applied": True, "note": str(_er.get("note") or "")}
            else:
                enhance_meta = {"applied": False, "note": str(_er.get("note") or "")}
        except Exception as _enh_err:  # noqa: BLE001  增强层故障绝不阻断生成
            logger.warning("提示词增强层异常（按原文继续）：%s: %s",
                           type(_enh_err).__name__, _enh_err)
            enhance_meta = {"applied": False, "note": "增强层异常"}

    before = check_prompt(kind, text, ctx=ctx, style=style, cfg=cfg,
                          ref_count=ref_count, expect_refs=expect_refs)
    repairs: List[str] = []
    final = text
    # ⚠️ 空提示词**绝不自愈**：补风格 / 补无文字约束会把空串变成一个「只有风格句、
    #    没有任何画面内容」的提示词，于是「提示词为空」这条致命缺陷在下一次检查里
    #    消失了 —— 等于用自愈把致命缺陷洗白，然后拿一条废提示词去出图。
    #    空就是空，交给调用方重建（见 rebuild_hint）。
    if mode in ("repair", "block") and text:
        final, repairs = repair_prompt(kind, text, ctx=ctx, style=style)
    # N1（2026-09-22 复验）：自愈后的提示词可能重新超过服务端上限，使 D-06 的「≤6000」
    # 不绝对成立。6000 是**服务端硬上限**，属安全闸门而非自愈策略，故在**启用预检**的
    # 路径上无条件钳制（warn 模式的语义是「不因质检缺陷阻断」，不是「允许超出上限」）；
    # 预检关闭时上面已 early-return 原样透传，保持「关闭时不改写」契约不变。
    # P-4（2026-09-22 QA 复验）：clamp_prompt 的日志标签按 kind 区分，避免 audio/
    # storyboard 超长时也被打成 prompt_h3（该形参只影响日志，不影响截断行为）。
    _clamped = h3_prompt_kit.clamp_prompt(final, label=f"prompt_qc.{kind}")
    _prompt_clamped = _clamped != final
    if _prompt_clamped:
        repairs.append(f"提示词超长已截断（{len(final)} > "
                       f"{h3_prompt_kit.MAX_PROMPT_CHARS} 字符）")
        final = _clamped
    repairs = list(dict.fromkeys(repairs))   # 同类修复（如多次去空词）只报一次
    verdict = check_prompt(kind, final, ctx=ctx, style=style, cfg=cfg,
                           ref_count=ref_count, expect_refs=expect_refs)
    # 自愈的成效写进 verdict，便于前端/报告展示「修了什么」
    verdict["repairs"] = repairs
    # N1：暴露「本次是否因超长被钳制」，便于观测与测试（复检看到的是钳制后的文本）
    verdict["prompt_clamped"] = _prompt_clamped
    # N1 修正（2026-09-22 QA 复验 P-1）：截断是**客户端安全闸门**，绝不能自己造出一条
    # 原来不存在的致命缺陷。H3 的 validate 按顺序查段名，从末尾剪掉超长内容会连带剪掉
    # overall_soundscape / non_diegetic_music → 报「结构不合规（缺段）」→ blocked=True，
    # 把一条**结构本来完整**的提示词判废（服务端本来只是静默截断、照样出片）。
    # 判据：只降级「本次钳制**新增**」的致命缺陷；`before`（原文本）已有的致命缺陷照旧阻断。
    # ⚠️ 必须按**类型键**（见 _crit_kind）而不是整串比对 —— 钳制会改变同一条致命缺陷的描述
    #    （缺段列表变长），整串比对会把「原文本来就缺段」误判成「钳制新增」而洗白真致命。
    # 降级不等于隐藏：同时进 issues（可见）与 clamp_induced_critical（可分型统计）。
    # ⚠️ 注意 `verdict["score"]` / `verdict["passed"]` 仍是含致命惩罚的旧值：`block` 严格模式
    #    依旧会拦（这符合严格模式语义），`repair`（默认）模式不再误阻断。
    if _prompt_clamped:
        _pre_kinds = {_crit_kind(c) for c in (before.get("critical_issues") or [])}
        _after_crit = list(verdict.get("critical_issues") or [])
        _induced = [c for c in _after_crit if _crit_kind(c) not in _pre_kinds]
        _kept = [c for c in _after_crit if _crit_kind(c) in _pre_kinds]
        if _induced:
            verdict["clamp_induced_critical"] = _induced
            verdict["critical_issues"] = _kept
            verdict["issues"] = list(verdict.get("issues") or []) + [
                f"（截断致）{c}" for c in _induced]
            logger.warning("提示词预检[%s] 钳制新增致命缺陷已降级为非阻断（%d 项）：%s",
                           kind, len(_induced), "；".join(_induced)[:200])

    # ---- 质检模型语义复审（2026-09-30 需求：确定性检查通过后再过一遍质检模型） ----
    # 只在默认的 repair 模式做（warn=纯诊断不改动；block=严格模式保持纯确定性语义）。
    # 复审「不通过且给出改进版」时：改进版必须先自愈 + 复检，分数不降才采纳——
    # 模型的改写不可信，确定性检查才是最终裁判。
    verdict["llm_enhance"] = enhance_meta
    if (mode == "repair" and not verdict.get("critical_issues") and final
            and kind in ("storyboard", "asset", "keyframe", "h3")):
        try:
            import prompt_enhance as _pe  # 函数内导入：避免模块级循环依赖
            _rv = _pe.model_review(kind, final, ctx=ctx, style=style)
            verdict["model_review"] = {
                "passed": bool(_rv.get("passed", True)),
                "issues": list(_rv.get("issues") or []),
                "note": str(_rv.get("note") or ""),
            }
            _imp = str(_rv.get("improved_prompt") or "").strip()
            if (not _rv.get("passed", True)) and _imp and _imp != final:
                _imp_final, _imp_repairs = repair_prompt(kind, _imp, ctx=ctx, style=style)
                _imp_final = h3_prompt_kit.clamp_prompt(_imp_final,
                                                        label=f"prompt_qc.{kind}.review")
                _v2 = check_prompt(kind, _imp_final, ctx=ctx, style=style, cfg=cfg,
                                   ref_count=ref_count, expect_refs=expect_refs)
                if not _v2.get("critical_issues") \
                        and int(_v2.get("score") or 0) >= int(verdict.get("score") or 0):
                    _prev_issues = list(verdict.get("issues") or [])
                    # ⚠️ _v2 是 check_prompt 的新鲜产物，没有 model_review 键 ——
                    #    直接 verdict["model_review"]["adopted"]=True 会 KeyError（实测）。
                    #    把旧 verdict 上的复审结论带上 adopted 标记整体搬过来。
                    _mr = dict(verdict.get("model_review") or {})
                    _mr["adopted"] = True
                    final = _imp_final
                    repairs = list(dict.fromkeys(
                        list(repairs) + list(_imp_repairs) + ["采纳质检模型的改进版提示词"]))
                    _v2["llm_enhance"] = enhance_meta
                    _v2["model_review"] = _mr
                    verdict = _v2
                    verdict["repairs"] = repairs
                    logger.info("提示词预检[%s] 已采纳质检模型改进版（原 issues：%s）",
                                kind, "；".join(_prev_issues[:2]))
        except Exception as _rv_err:  # noqa: BLE001  复审故障不影响本次生成
            logger.warning("质检模型复审异常（跳过）：%s: %s",
                           type(_rv_err).__name__, _rv_err)

    verdict["before_issues"] = list(before.get("issues") or [])
    verdict["before_score"] = before.get("score")
    verdict["mode"] = mode
    verdict["rebuild_hint"] = _rebuild_hint(kind, verdict, final)

    blocked = bool(verdict.get("critical_issues"))
    if mode == "warn":
        accept = True
    elif mode == "block":
        accept = bool(verdict.get("passed")) and not (verdict.get("issues") or [])
    else:  # repair
        accept = not blocked

    if blocked:
        label = "提示词致命缺陷"
    elif accept:
        label = "提示词达标" + (f"（自愈 {len(repairs)} 项）" if repairs else "")
    else:
        label = "提示词不达标"
    if mode == "warn" and (blocked or verdict.get("issues")):
        label = "提示词有问题（warn 模式不阻断）"
    # reason 必须与 accept 结论一致，不能出现「已放行」却只列缺陷的割裂表述
    if blocked:
        reason = "致命缺陷：" + "；".join(verdict.get("critical_issues") or [])[:200]
    elif accept and repairs:
        reason = f"已自愈 {len(repairs)} 项：" + "；".join(repairs[:2])
        if verdict.get("issues"):
            reason += f"；另有 {len(verdict['issues'])} 项非致命提示（不阻断）"
    elif accept and verdict.get("issues"):
        rest = "；".join(verdict["issues"][:2])
        reason = f"按当前模式放行，但仍有 {len(verdict['issues'])} 项提示：{rest}"
    else:
        reason = verdict.get("reason") or "无问题"
    return {"prompt": final, "before": before, "verdict": verdict, "repairs": repairs,
            "accept": accept, "blocked": blocked, "skipped": False,
            "label": label, "reason": reason,
            "rebuild_hint": verdict.get("rebuild_hint") or ""}


def prompt_qc_gate(pf: dict, cfg: dict = None) -> dict:
    """把 preflight 结果转成统一闸门结构 ``{accept, blocked, skipped, label, reason}``

    与 ``app._qc_gate`` 字段一致，便于两条链路共用同一套编排代码。
    """
    pf = pf or {}
    if pf.get("skipped"):
        return {"accept": True, "blocked": False, "skipped": True,
                "label": pf.get("label") or "提示词预检未启用（跳过）",
                "reason": pf.get("reason") or "", "critical_issues": []}
    v = pf.get("verdict") or {}
    return {"accept": bool(pf.get("accept")), "blocked": bool(pf.get("blocked")),
            "skipped": False, "label": pf.get("label") or "",
            "reason": pf.get("reason") or "",
            "critical_issues": list(v.get("critical_issues") or []),
            "repairs": list(pf.get("repairs") or []),
            "mode": v.get("mode") or prompt_qc_mode(cfg or {})}
