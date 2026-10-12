# -*- coding: utf-8 -*-
"""LLM 归纳「章节标题规律」→ 出正则 → 供 split_chapters 扫全文定位章节。

背景
----
每本小说的章节标记格式都可能不同（``第N章`` / ``Chapter N`` / ``一、`` /
``【场景三】`` / 空行分节 / 无标题……），``novel_parser.split_chapters`` 的纯正则
只认得几种固定格式，遇到其它就漏检、最后退化成按字数硬切。本模块让**前端「文本分析
模型」（OpenAI 兼容，任意厂商）** 看一眼小说样本，归纳出「这本书的章节标题长什么样」，
返回若干条正则；这些正则被并入 ``split_chapters`` 的标记扫描，从而对格式任意的小
说也能正确分章。

设计要点
--------
- **LLM 只看样本、出规则、再扫全文**（一次 LLM 调用，快、可复用于整本）：
  ``derive_patterns`` 取文本前段样本 → 让模型产出正则 → 校验/清洗 → 返回。
- **零第三方依赖、可离线单测**：``make_sample`` / ``build_prompt`` / ``parse_patterns``
  均为纯函数（不碰 LLMClient），只有 ``derive_patterns`` 才调用 client。
- **失败一律降级、绝不阻断上传**：任何异常（LLM 未配置 / 调用失败 / 解析不出）都
  返回 ``[]``，让调用方退回纯正则结果，行为与改造前完全一致。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

__all__ = [
    "make_sample", "build_prompt", "parse_patterns", "derive_patterns",
    "build_structure_prompt", "structure_rows", "parse_structure",
    "analyze_chapter_structure",
    "MAX_PATTERNS", "MAX_PATTERN_LEN", "SAMPLE_CHARS",
    "STRUCTURE_PREVIEW_CHARS", "STRUCTURE_MAX_ROWS",
]

# LLM 最多返回几条章节标题正则（防止模型失控生成一长串）
MAX_PATTERNS = 8
# 单条正则长度上限（过长的「正则」通常是模型把整段标题原样塞进来，无泛化价值）
MAX_PATTERN_LEN = 120
# 喂给 LLM 的样本长度（取小说前段，足以观察其章节标记风格）
SAMPLE_CHARS = 6000

_SYSTEM_PROMPT = (
    "你是「小说章节切分」规则归纳器。我会给你一段小说正文样本，"
    "请你判断这本书的**章节标题行**长什么样，并给出若干条能匹配这些标题行"
    "的**Python 正则表达式**（按行匹配，用于 re.MULTILINE 下的 finditer）。"
)


def make_sample(text: str, sample_chars: int = SAMPLE_CHARS) -> str:
    """取正文前段样本（去多余空行、控制长度），供 LLM 观察章节标记风格。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 规整空白：把 3 个及以上连续换行压成 2 个，避免样本被大段空行稀释
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[: int(sample_chars or SAMPLE_CHARS)]


def build_prompt(sample: str, hint: str = "") -> str:
    """构造让 LLM 产出章节标题正则的提示词（要求严格 JSON）。"""
    sample_block = (sample or "").strip() or "（样本为空）"
    hint_block = f"\n补充说明：{hint.strip()}" if (hint or "").strip() else ""
    return (
        "请观察下面这段小说正文样本，判断其中「章节/小节标题行」的格式"
        "（例如 第N章 / 第N回 / Chapter N / 一、 / 【场景】 / 纯空行分节 等），"
        "归纳出能匹配这些标题行的 Python 正则表达式。"
        + hint_block
        + "\n\n要求：\n"
        "1. 每条正则都按**整行**匹配（内部已含行首行尾锚定），供 re.MULTILINE 使用；"
        "2. 优先给出**能泛化到全书**的少数几条，而不是逐条枚举看到的标题原文；"
        f"3. 最多 {MAX_PATTERNS} 条；若无明显章节标记（如纯空行分节），可返回空数组；"
        "4. 只输出如下 JSON，不要任何解释或 markdown 代码块：\n"
        '{"patterns": ["正则1", "正则2"], "note": "一句话说明你观察到的章节格式"}\n'
        "\n=== 正文样本 ===\n"
        f"{sample_block}"
    )


def _sanitize_one(pat: str) -> str:
    """清洗单条正则：去空白首尾、限长；无法编译则返回空串。"""
    p = (pat or "").strip()
    if not p or len(p) > MAX_PATTERN_LEN:
        return ""
    # 编译校验用「调用方等价口径」：临时加 (?m) 模拟 split_chapters 的 re.M，确保在
    # MULTILINE 下也能编译；返回值仍用原 p —— 调用方 split_chapters 会以 re.M 重新编译，
    # 无需在返回的正则里内联 (?m)。
    body = p if p.startswith("(?m)") else "(?m)" + p
    try:
        re.compile(body)
    except re.error:
        return ""
    return p


def parse_patterns(resp, max_patterns: int = MAX_PATTERNS) -> list:
    """从 LLM 的 JSON 响应里提取并校验正则列表（纯函数，可离线测）。

    返回清洗后、可编译的**去重**正则字符串列表；任何不合法项都被丢弃。
    ``resp`` 可以是 LLMClient.chat_json_robust 解析出的 dict，或含 JSON 的原始字符串。
    """
    data = resp
    if isinstance(resp, str):
        try:
            data = json.loads(resp)
        except (ValueError, TypeError):
            data = {}
    if not isinstance(data, dict):
        return []
    raw = data.get("patterns")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            continue
        p = _sanitize_one(item)
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= int(max_patterns or MAX_PATTERNS):
            break
    return out


def derive_patterns(client, text: str, hint: str = "") -> list:
    """调用 LLM 从小说文本归纳章节标题正则（失败返回 []，绝不抛给调用方）。

    client 为 llm_client.LLMClient（由 app._current_llm_client() 构造，密钥取自
    前端「文本分析模型」）。LLM 未配置 / 调用 / 解析任何失败 → 记日志并返回空列表，
    调用方据此退回纯正则切分，行为与改造前一致。
    """
    if client is None:
        return []
    sample = make_sample(text)
    if not sample:
        return []
    prompt = build_prompt(sample, hint)
    chat = getattr(client, "chat_json_robust", None)
    if not callable(chat):
        logger.warning("chapter_llm：客户端无 chat_json_robust，退回纯正则切分")
        return []
    try:
        resp = chat(prompt, system=_SYSTEM_PROMPT, max_tokens=1500)
    except Exception as e:  # noqa: BLE001  任何 LLM 侧失败都降级，不阻断上传
        logger.warning(f"chapter_llm：LLM 归纳章节规则失败，退回纯正则切分：{e}")
        return []
    pats = parse_patterns(resp)
    if pats:
        logger.info(f"chapter_llm：LLM 归纳出 {len(pats)} 条章节标题正则")
    else:
        logger.info("chapter_llm：LLM 未给出有效正则（或样本无章节标记），用纯正则切分")
    return pats


# ===================== 章节目录结构体检（2026-10-02） =====================
# 背景：CHAPTER_PATTERNS 用同一条正则匹配「第[数字][章回节卷篇集部]」，于是
# 「第一卷：魔性不改」这种**卷标题**也会被切成一个独立章节 —— 它的正文为 0，
# 下游按集取章就取到这个空壳，模型只能凭 11 个字的标题凭空编剧本
#（实测：《蛊真人》第 1 集产出 8 个镜头全是自造的四字口号，整半章内容丢失，
# 覆盖率却报 100% —— 因为根本没有正文单元可核对）。
# novel_parser.collapse_shell_chapters 用「去掉标题行后正文近空」这一硬信号解决了
# 标准情形；但真实书籍的目录千奇百怪（分卷里套章、卷末有番外、短章本来就短、
# 标题格式不规则……），纯规则判断不了「这个短条目到底是不是真章节」。
# 所以这里让 LLM 像人一样读目录 + 抽读开头，判断哪些条目只是结构标题、哪些是正文，
# 并给出「真正的第一章」——在生成剧本之前完成这次体检（结果缓存进 meta，全书一次）。
STRUCTURE_PREVIEW_CHARS = 160
STRUCTURE_MAX_ROWS = 200

_STRUCTURE_SYSTEM_PROMPT = (
    "你是「小说章节目录结构分析器」。用户会给你一本书的章节目录，每项含序号、标题、"
    "字数，以及该项开头的正文抽读。请判断这本书的真实结构，指出哪些条目只是"
    "结构标题（如「第一卷 X」「第一部 X」这类分卷/分部名，本身没有正文），"
    "哪些才是真正的正文章节，并给出第一个真正的正文章节的序号。"
)


def structure_rows(chapters, text, preview_chars=STRUCTURE_PREVIEW_CHARS,
                   max_rows=STRUCTURE_MAX_ROWS):
    """把章节表压成给 LLM 看的目录行（序号 / 标题 / 字数 / 开头正文抽读）。

    超长目录只取前段 + 末尾 5 条：分卷问题几乎都出在开头，末尾用来让模型确认
    「后面的条目都是正常章节」，避免它只看前若干行就误判全书结构。
    """
    if not chapters:
        return []
    lim = max(20, int(max_rows or STRUCTURE_MAX_ROWS))
    picked = list(chapters[:lim])
    if len(chapters) > lim:
        # 去重：末尾 5 条可能与前段重叠（len(chapters) 略大于 lim 时），按对象身份剔除
        _seen = {id(ch) for ch in picked}
        picked = picked + [ch for ch in chapters[-5:] if id(ch) not in _seen]
    out = []
    for ch in picked:
        if not isinstance(ch, dict):
            continue
        try:
            seg = text[int(ch.get("start") or 0):int(ch.get("end") or 0)]
        except (TypeError, ValueError):
            seg = ""
        nl = seg.find("\n")
        body = seg[nl + 1:] if nl >= 0 else ""
        body = re.sub(r"\s+", " ", body).strip()
        out.append({
            "index": ch.get("index"),
            "title": str(ch.get("title") or "").strip(),
            "char_count": ch.get("char_count"),
            "preview": body[: int(preview_chars or STRUCTURE_PREVIEW_CHARS)],
        })
    return out

def _structure_example():
    return ('{"structure": "卷-节", "volume_indices": [1], "content_count": 2348, '
            '"first_content_index": 2, "first_content_title": "第一节：xxx", '
            '"notes": "一句话说明"}')


def build_structure_prompt(rows, novel_title=""):
    """构造「章节目录结构分析」提示词（严格 JSON 输出）。纯函数，可离线单测。"""
    lines = []
    for r in (rows or []):
        prev = (r.get("preview") or "").strip() or "（无正文，仅标题行）"
        lines.append("[%s] %s | %s字 | 开头：%s" % (
            r.get("index"), r.get("title"), r.get("char_count"), prev))
    body = "\n".join(lines) or "（目录为空）"
    title_block = ("书名：%s\n" % novel_title) if (novel_title or "").strip() else ""
    return (
        title_block
        + "下面是这本书的章节目录（按出现顺序）。请判断：\n"
        "1. 哪些序号的条目**只是结构标题**（分卷/分部名，本身没有正文内容）；\n"
        "2. 哪些序号是**真正的正文章节**；\n"
        "3. 第一个真正的正文章节的序号；\n"
        "4. 用一句话概括这本书的层级结构（如「卷-节」「卷-章」「章」）。\n\n"
        "判断原则：\n"
        "- 标题是「第N卷/第N部/第N篇」且开头**没有成段正文**的 → 结构标题，不是正文章节；\n"
        "- 标题是「第N章/第N节/第N回」且有成段正文的 → 正文章节；\n"
        "- 不要因为标题里出现「卷」字就一律判为结构标题 —— 必须结合**有没有正文**；\n"
        "- 短但确有正文的条目仍是正文章节，不要误判；\n"
        "- 若整本目录都正常，volume_indices 返回空数组，first_content_index 返回 1。\n\n"
        "只输出如下 JSON，不要解释、不要 markdown 代码块：\n"
        + _structure_example() + "\n\n"
        "=== 章节目录 ===\n"
        + body
    )

def parse_structure(resp):
    """从 LLM 响应里解析并校验「目录结构」判决（纯函数，可离线测）。

    返回 {} 表示无法采信（调用方沿用规则结果）。
    """
    data = resp
    if isinstance(resp, str):
        try:
            data = json.loads(resp)
        except (ValueError, TypeError):
            data = {}
    if not isinstance(data, dict):
        return {}

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    raw_vols = data.get("volume_indices")
    if isinstance(raw_vols, str):
        raw_vols = re.findall(r"\d+", raw_vols)
    vols = []
    if isinstance(raw_vols, list):
        for v in raw_vols:
            n = _int(v)
            if n is not None and n > 0 and n not in vols:
                vols.append(n)
    vols.sort()
    first = _int(data.get("first_content_index"))
    if first is not None and first <= 0:
        first = None
    out = {
        "structure": str(data.get("structure") or "").strip(),
        "volume_indices": vols,
        "content_count": _int(data.get("content_count")),
        "first_content_index": first,
        "first_content_title": str(data.get("first_content_title") or "").strip(),
        "notes": str(data.get("notes") or "").strip(),
    }
    if not vols and first is None and not out["structure"]:
        return {}
    return out


def analyze_chapter_structure(client, chapters, text, novel_title=""):
    """让 LLM 体检章节目录结构（失败返回 {}，绝不抛给调用方）。

    返回见 parse_structure。LLM 未配置 / 调用失败 / 结论不可采信一律返回 {}，
    调用方据此沿用 novel_parser.collapse_shell_chapters 的规则结果。
    """
    if client is None or not chapters or not text:
        return {}
    rows = structure_rows(chapters, text)
    if not rows:
        return {}
    prompt = build_structure_prompt(rows, novel_title)
    chat = getattr(client, "chat_json_robust", None)
    if not callable(chat):
        logger.warning("chapter_llm：客户端无 chat_json_robust，跳过目录结构体检")
        return {}
    try:
        resp = chat(prompt, system=_STRUCTURE_SYSTEM_PROMPT, max_tokens=1200)
    except Exception as e:  # noqa: BLE001  任何 LLM 侧失败都降级
        logger.warning("chapter_llm：目录结构体检失败，沿用规则切分：%s", e)
        return {}
    verdict = parse_structure(resp)
    if verdict:
        logger.info("chapter_llm：目录结构体检 → structure=%s 结构标题=%s 首个正文章=%s",
                    verdict.get("structure") or "?", verdict.get("volume_indices"),
                    verdict.get("first_content_index"))
    else:
        logger.info("chapter_llm：目录结构体检未给出可采信结论，沿用规则切分")
    return verdict
