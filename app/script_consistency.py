# -*- coding: utf-8 -*-
"""P0-3 剧本 ↔ 原著一致性校验（确定性三件套 + 局部修复闭环）

背景
----
现有三套校验各管一段，但都不覆盖「单集剧本 ↔ 本章原著」这一层：

* ``coverage.py``      原文句子级 → 镜头承载率（遗漏补生成）
* ``continuity.py``    相邻集六类一致性（LLM 判定 + 局部重写）
* ``qc_client.py``     剧本结构/逻辑/风格/提示词五维质检（**不接原著**）

P0-3 补齐的缺口（实测症状）：

1. **元信息泄漏**：模型把「第01章」「本章概要」「爽点」等章节元数据写进画面描述/台词；
2. **要素缺失**：原著该章的群像/场景/道具在剧本里整体消失（场景错配、缺群雄）；
3. **章节锚定错位**：剧本内容对应不上 ``metadata.chapter_index`` 指向的章节正文。

设计原则
--------
1. 三件套全部**零 LLM 成本可判定**（正则 / 后缀锚定 / 结构比对），保证 100% 可复现、不误伤；
2. 命中后的修复走**定向最小改动**，绝不整集重生成：

   - 元信息泄漏 → 复用 :func:`continuity.rewrite_shots_for_issues` 只重写命中镜头；
   - 要素缺失   → 按原著句子定向补生成镜头（只增不删，与 coverage 同源）；
   - 章节锚定   → **不自动修**（根因在章节切分，属 P0-1 面），只告警；

3. 修复后必须**复检**，轮次上限 :data:`CONSISTENCY_MAX_ROUNDS`（默认 1）防死循环；
4. 全过程降级安全：任何异常只记 warning，**不阻断剧本落盘**。

落盘
----
``<continuity_dir>/<项目键>/episodes/第N集_剧本一致性.json``（与覆盖率报告同目录同风格）
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

# ===================== 常量 =====================

SCRIPT_CONSISTENCY_VERSION = "script_consistency_v1"

CONSISTENCY_MAX_ROUNDS = 1          # 命中问题后的自动修复轮次上限（防死循环）
CHAPTER_DEV_TOLERANCE = 0.25        # 章节锚定：字数偏差超过该比例即判「锚定错位」
ELEMENT_MIN_FREQ = 2                # 要素候选最低频次（低于此值视为噪声）
ELEMENT_COVERAGE_MIN = 0.6          # 要素覆盖率达标线
ELEMENT_SUPPLEMENT_CHARS = 1200     # 要素补生成：单块聚合的原文字数上限
ELEMENT_SUPPLEMENT_MAX = 60         # 单轮最多补生成的缺失要素数
MAX_LEAK_HITS_LISTED = 200          # 报告中列出的泄漏命中条数上限
MAX_MISSING_ELEMENTS_LISTED = 80    # 报告中列出的缺失要素条数上限
MAX_SUPPLEMENT_SENTENCES = 40       # 单块补生成引用的原句数上限


class ScriptConsistencyError(Exception):
    """剧本一致性校验的结构性错误（调用方按降级处理）"""


# ===================== 元信息泄漏：正则黑名单 =====================
# 口径：high = 一定是「章节元数据 / 创作术语」泄漏，不可能出现在画面或台词里；
#       medium = 疑似（如 N 字、书名号），需人工复核但不影响自动修复。
_META_LEAK_RULES = (
    ("chapter_marker", "high",
     re.compile(r"第\s*[0-9一二三四五六七八九十百零两]{1,6}\s*章")),
    ("chapter_span", "high",
     re.compile(r"全\s*[0-9一二三四五六七八九十百零两]{1,6}\s*章")),
    ("meta_word", "high",
     re.compile(r"本章|上章|下章|上一章|下一章|章节概要|内容概要|故事概要|剧情概要|梗概|"
                r"大纲|爽点|情绪曲线|分镜表|剧情简介|人物小传|章节字数")),
    ("speaker_label", "high",
     re.compile(r"(?:主角|男主|女主|反派|配角|人物)\s*[:：]")),
    ("author_note", "high",
     re.compile(r"原文|原著|本章字数|字数要求|改编自|小说原文|网文|爽文")),
    ("char_count", "medium",
     re.compile(r"[0-9]{2,}\s*字")),
)
#: 书名号：可能是道具书名（合法），也可能是泄漏；与小说名一致时升为 high
_BOOK_TITLE_RE = re.compile(r"《[^》]{1,40}》")

#: 扫描字段（不扫 prompt_h3：它在生成期由 h3_prompt_kit 按参考图重建，不是剧本事实源）
_LEAK_SCAN_FIELDS = ("location", "description", "visual_detail", "dialogue_text", "audio_cues", "emotion")


# ===================== 要素抽取：后缀锚定 =====================

SCENE_SUFFIXES = ("殿", "堂", "崖", "峰", "洞", "谷", "林", "城", "村", "院", "府", "宫", "阁",
                  "楼", "塔", "海", "河", "湖", "山", "街", "巷", "门", "桥", "洲", "岛", "渊",
                  "墓", "窟", "秘境", "坊", "镇", "关", "园", "庄", "寨", "坪", "漠", "荒", "原",
                  "墟", "界", "域", "境", "狱", "坛", "庙", "祠", "苑", "庭", "斋", "轩", "房",
                  "室", "厅", "营", "场", "道", "路", "梯", "滩", "岭", "川", "湾", "港")

ITEM_SUFFIXES = ("丹", "剑", "符", "玉", "珠", "环", "镯", "牌", "令", "旗", "钟", "鼎", "镜",
                 "印", "书", "卷", "册", "石", "草", "花", "果", "木", "匣", "盒", "袋", "囊",
                 "链", "甲", "衣", "袍", "扇", "笛", "琴", "弓", "刀", "枪", "棍", "杖", "壶",
                 "炉", "图", "简", "佩", "玺", "冠", "靴", "幡", "索", "珠串",
                 # 仙侠/国漫高频道具后缀（蛊真人等本作核心）
                 "蛊", "图腾", "窍", "诀", "经", "阵", "术", "录", "谱", "匣", "梭", "舟",
                 "船", "车", "灯", "伞", "绳", "弓", "箭", "盾", "锤", "叉", "鞭", "锏")

_ALL_SUFFIXES = tuple(sorted(set(SCENE_SUFFIXES) | set(ITEM_SUFFIXES), key=len, reverse=True))
_SUFFIX_SCAN_RE = re.compile("|".join(_ALL_SUFFIXES))

#: 要素候选的停用字：命中则说明该候选是被动词/虚词/代词「带出来」的噪声
_ELEMENT_STOP_CHARS = set(
    "的 了 是 在 有 和 与 及 就 都 还 也 而 但 并 且 或 被 把 从 对 向 以 为 上 下 里 外 中 前 后 时 之 其 "
    "此 这 那 些 个 们 我 你 他 她 它 您 咱 很 没 不 无 将 要 能 可 会 得 着 过 再 又 只 更 最 太 真 "
    "走 进 到 来 出 看 说 道 想 听 站 坐 飞 落 入 回 起 拉 推 打 杀 抓 拿 放 带 跟 奔 冲 退 转 抬 低 "
    "睁 闭 笑 哭 喊 叫 问 答 见 望 指 挥 升 沉 浮 扑 跃 踏 踢 拽 扯 抱 背 扛 挑 扔 掷 砸 劈 砍 刺 "
    "扫 拂 拍 敲 握 捏 捧 端 斟 饮 食 吞 咬 嚼 吐 喘 叹 哼 吼 啸 吟 唱 唤 应 跪 趴 躺 卧 爬 掉 掉 "
    "如 若 似 像 比 让 使 令 遭 受 被 由 于 至 於 呢 吗 吧 啊 呀 哦 嗯 哈 呵 嘿 喂 咦 唉 谁 何 怎 "
    # 副词/时间副词：拦掉「早已经」这类「副词 + 后缀」的伪要素
    "早 已 才 曾 正 刚 顿 忽 突 终 竟 居 幸 因 所 然 接 随 彼 当 现 初 直 始 往 常 偶 究 反 或 许 "
    "概 仿 佛 犹 宛 般 样 非 岂 各 每 另 别 渐 逐 依 仍 尚 恰 偏 唯 独 尤 越 稍 略 颇 甚 极 挺".split()
)

#: 章节标题行（章节锚定与要素抽取都要先剔除，避免把标题当正文）
_TITLE_LINE_RE = re.compile(r"^\s*(?:第\s*[0-9一二三四五六七八九十百零两]{1,6}\s*章|Chapter\s*\d+)")


# ===================== 通用小工具 =====================

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(name, limit: int = 60) -> str:
    """项目键 → 文件系统安全名（与 coverage / continuity 同口径）"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip("._")
    return (s or "novel_project")[:limit]


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iter_body_lines(text):
    """逐行产出「正文字段」，剔除空行与章节标题行"""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if _TITLE_LINE_RE.match(s) and len(s) <= 60:
            continue
        yield s


def _split_sentences(text):
    """按句末标点把正文切成句子（用于要素补生成时定位原文出处）"""
    out = []
    for line in _iter_body_lines(text):
        for part in re.split(r"(?<=[。！？!?…])", line):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _shot_text_pool(script) -> str:
    """剧本「画面事实源」文本池：只算镜头实际承载的文本，不算设定库名称。

    刻意**不**把 ``script["items"]`` / ``script["scenes"]`` 的设定名算进来 ——
    设定库里有、镜头里没有，正是 P0-3 要抓的「要素缺失」。
    """
    parts = []
    for s in (script or {}).get("shots") or []:
        if not isinstance(s, dict):
            continue
        for key in ("location", "description", "visual_detail", "dialogue_text", "audio_cues", "emotion"):
            val = s.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val)
        for key in ("characters_in_shot", "items_in_shot"):
            val = s.get(key)
            if isinstance(val, list):
                parts.extend([str(x) for x in val if x])
    return "\n".join(parts)


# ===================== ① 章节锚定校验 =====================

def resolve_chapter(novel_meta: dict, chapter_index, chapter: dict = None) -> dict:
    """定位 ``chapter_index`` 对应的章节（优先用调用方直传的 chapter）"""
    if isinstance(chapter, dict) and chapter.get("start") is not None:
        expected = _as_int(chapter.get("end"), 0) - _as_int(chapter.get("start"), 0)
        return {"found": True, "source": "chapter_arg",
                "index": _as_int(chapter.get("index"), _as_int(chapter_index, 0)),
                "title": chapter.get("title") or "", "start": _as_int(chapter.get("start"), 0),
                "end": _as_int(chapter.get("end"), 0), "char_count": max(0, expected)}
    idx = _as_int(chapter_index)
    chapters = (novel_meta or {}).get("chapters") or []
    if idx is None or not chapters:
        return {"found": False, "source": "none", "index": idx,
                "reason": "缺少 chapter_index 或小说章节表"}
    for c in chapters:
        if _as_int(c.get("index")) == idx:
            return {"found": True, "source": "novel_meta", "index": idx,
                    "title": c.get("title") or "", "start": _as_int(c.get("start"), 0),
                    "end": _as_int(c.get("end"), 0),
                    "char_count": max(0, _as_int(c.get("end"), 0) - _as_int(c.get("start"), 0))}
    return {"found": False, "source": "novel_meta", "index": idx,
            "reason": f"小说章节表中不存在第 {idx} 章（章节数 {len(chapters)}）",
            "chapter_count": len(chapters)}


def check_chapter_anchor(script, novel_meta: dict = None, chapter: dict = None) -> dict:
    """① 章节锚定：剧本 metadata.chapter_index 是否指向真实章节，且字数对得上"""
    meta = (script or {}).get("metadata") or {}
    idx = meta.get("chapter_index")
    src_count = _as_int(meta.get("chapter_char_count"))
    row = resolve_chapter(novel_meta, idx, chapter)

    result = {
        "checked": bool(row.get("found")),
        "ok": True,
        "chapter_index": _as_int(idx, row.get("index")),
        "expected_char_count": row.get("char_count"),
        "script_char_count": src_count,
        "deviation": None,
        "tolerance": CHAPTER_DEV_TOLERANCE,
        "reason": row.get("reason"),
    }
    if not row.get("found"):
        # 无法判定时不误报（novel_meta 缺失的整本路径、或调用方未传章节）
        result["checked"] = False
        result["severity"] = None
        return result

    chapters = (novel_meta or {}).get("chapters") or []
    if row.get("source") == "novel_meta" and chapters and _as_int(idx) and \
            _as_int(idx) > len(chapters):
        result.update({"ok": False, "severity": "high",
                       "reason": f"chapter_index={idx} 超出章节总数 {len(chapters)}"})
        return result

    expected = _as_int(row.get("char_count"), 0)
    if expected > 0 and src_count:
        dev = abs(src_count - expected) / float(expected)
        result["deviation"] = round(dev, 4)
        if dev > CHAPTER_DEV_TOLERANCE:
            result.update({
                "ok": False, "severity": "high",
                "reason": (f"剧本 chapter_char_count={src_count} 与第 {row.get('index')} 章正文 "
                           f"{expected} 字偏差 {dev * 100:.1f}%（阈值 "
                           f"{CHAPTER_DEV_TOLERANCE * 100:.0f}%）"),
            })
    elif not src_count:
        result.update({"severity": "low",
                       "reason": "剧本未记录 chapter_char_count，无法做字数比对（建议补写 metadata）"})
    return result


# ===================== ② 元信息泄漏扫描 =====================

def scan_meta_leak(script, novel_meta: dict = None) -> dict:
    """② 元信息泄漏：对镜头文本跑正则黑名单，返回命中清单（含 shot_id + 字段 + 片段）"""
    titles = set()
    for key in ("title", "name", "novel_title"):
        val = str((novel_meta or {}).get(key) or "").strip()
        if val:
            titles.add(val)
    meta = (script or {}).get("metadata") or {}
    for key in ("project_name", "novel_title", "title"):
        val = str(meta.get(key) or "").strip()
        if val:
            titles.add(val)
    script_title = str((script or {}).get("title") or "").strip()
    if script_title:
        titles.add(script_title)

    hits, shot_ids = [], set()
    for s in (script or {}).get("shots") or []:
        if not isinstance(s, dict):
            continue
        sid = _as_int(s.get("shot_id"), 0)
        for field in _LEAK_SCAN_FIELDS:
            text = s.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            for name, severity, pattern in _META_LEAK_RULES:
                m = pattern.search(text)
                if m:
                    hits.append({"shot_id": sid, "field": field, "rule": name,
                                 "severity": severity, "match": m.group(0)[:40],
                                 "excerpt": text[max(0, m.start() - 12):m.end() + 12][:80]})
                    shot_ids.add(sid)
            # 书名号：与小说/项目名一致 → high；否则 medium（可能是合法道具书名）
            for m in _BOOK_TITLE_RE.finditer(text):
                inner = str(m.group(0)).strip("《》")
                sev = "high" if (titles and any(inner and (inner in t or t in inner) for t in titles)) \
                    else "medium"
                hits.append({"shot_id": sid, "field": field, "rule": "book_title",
                             "severity": sev, "match": m.group(0)[:40],
                             "excerpt": text[max(0, m.start() - 8):m.end() + 8][:80]})
                shot_ids.add(sid)

    stats = {"high": 0, "medium": 0, "low": 0}
    for h in hits:
        stats[h["severity"]] = stats.get(h["severity"], 0) + 1
    return {
        "hit_count": len(hits),
        "stats": stats,
        "shot_ids": sorted(shot_ids),
        "hits": hits[:MAX_LEAK_HITS_LISTED],
        "hits_truncated": max(0, len(hits) - MAX_LEAK_HITS_LISTED),
    }


# ===================== ③ 要素覆盖校验 =====================

_HAN_ONLY_RE = re.compile(r"^[\u4e00-\u9fa5]+$")


def _is_han(ch: str) -> bool:
    """单字符是否汉字（用于中文词边界判断）。"""
    return bool(ch) and '\u4e00' <= ch <= '\u9fff'


def _contains_word(corpus: str, name: str) -> bool:
    """判断 name 是否作为「独立词」出现在 corpus 中（中文词边界语义，t104 P2）。

    原实现 `name in corpus` 是子串匹配：候选「青云宗」会误命中「青云宗派」导致
    覆盖假阴性（该补生成的不补）。这里按中文词边界重判：候选名前后若是「停用字
    （助词/动词/虚词，见 _ELEMENT_STOP_CHARS）/ 非汉字 / 文本边界」，视为独立词命中；
    若是「实词汉字」，视为更长词的一部分（子串）不命中。
    例：『青云宗』命中「的青云宗的」「青云宗」，不命中「青云宗派」。
    """
    name = str(name or "")
    if not name:
        return False
    for m in re.finditer(re.escape(name), corpus):
        s, e = m.start(), m.end()
        prev = corpus[s - 1] if s > 0 else ""
        nxt = corpus[e] if e < len(corpus) else ""
        prev_ok = (not prev) or (not _is_han(prev)) or (prev in _ELEMENT_STOP_CHARS)
        nxt_ok = (not nxt) or (not _is_han(nxt)) or (nxt in _ELEMENT_STOP_CHARS)
        if prev_ok and nxt_ok:
            return True
    return False


def _valid_element(word: str, exclude_names: set = None, name_pool: set = None) -> bool:
    """要素候选合法性：纯中文、长度 3~5、无停用字、不是角色/设定名的片段"""
    if not word or not (3 <= len(word) <= 5):
        return False
    if not _HAN_ONLY_RE.match(word):      # 拦掉「，黄钟」这类标点混入
        return False
    if exclude_names and word in exclude_names:
        return False
    # 角色/设定名的子串一律剔除（如「黑楼兰」→「黑楼」）
    if name_pool:
        for nm in name_pool:
            if len(nm) >= 2 and (word in nm or nm in word):
                return False
    # 除末尾后缀外的部分不得含虚词/动词（过滤「走进青云殿」这类被带出来的噪声）
    return not any(ch in _ELEMENT_STOP_CHARS for ch in word[:-1])


def extract_elements(text: str, exclude_names: set = None, name_pool: set = None) -> dict:
    """从原文抽取场景/道具候选：后缀锚定 + 向前取 1~3 字组合 + 频次过滤"""
    exclude_names = exclude_names or set()
    name_pool = name_pool or set()
    counters = {"scene": {}, "item": {}}
    for line in _iter_body_lines(text):
        for m in _SUFFIX_SCAN_RE.finditer(line):
            suffix = m.group(0)
            end = m.start()
            for n in (1, 2, 3):
                start = end - n
                if start < 0:
                    continue
                head = line[start:end]
                if not head:
                    continue
                word = head + suffix
                if not _valid_element(word, exclude_names, name_pool):
                    continue
                kind = "scene" if suffix in SCENE_SUFFIXES else "item"
                counters[kind][word] = counters[kind].get(word, 0) + 1

    rows = {}
    for kind, counter in counters.items():
        picked = [{"name": w, "freq": n} for w, n in counter.items() if n >= ELEMENT_MIN_FREQ]
        rows[kind] = sorted(picked, key=lambda r: (-r["freq"], r["name"]))
    return rows


def check_element_coverage(chapter_text: str, script, novel_meta: dict = None) -> dict:
    """③ 要素覆盖：原著该章的场景/道具候选，是否在剧本中出现

    覆盖池 = 镜头文本 + ``scenes``/``items`` 设定库名称（设定库是剧本一等公民，
    进了设定库即视为已落地）；只看镜头文本会把「设定有、镜头没有」误判为缺失。
    """
    name_pool = set()
    for s in (script or {}).get("scenes") or []:
        if isinstance(s, dict) and s.get("name"):
            name_pool.add(str(s["name"]).strip())
    for i in (script or {}).get("items") or []:
        if isinstance(i, dict) and i.get("name"):
            name_pool.add(str(i["name"]).strip())
    char_names = set()
    for c in (script or {}).get("characters") or []:
        if isinstance(c, dict) and c.get("name"):
            char_names.add(str(c["name"]).strip())

    candidates = extract_elements(chapter_text, exclude_names=char_names,
                                  name_pool=name_pool | char_names)
    corpus = _shot_text_pool(script)
    for nm in name_pool:
        corpus += "\n" + nm
    covered, missing = [], []
    for kind, rows in candidates.items():
        for row in rows:
            item = {"name": row["name"], "kind": kind, "freq": row["freq"]}
            if _contains_word(corpus, row["name"]):
                covered.append(item)
            else:
                missing.append(item)
    total = len(covered) + len(missing)
    coverage = (len(covered) / float(total)) if total else 1.0
    missing.sort(key=lambda r: (-r["freq"], r["name"]))
    return {
        "candidate_count": total,
        "scenes": len(candidates.get("scene") or []),
        "items": len(candidates.get("item") or []),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "coverage": round(coverage, 4),
        "coverage_percent": round(coverage * 100, 1),
        "threshold": ELEMENT_COVERAGE_MIN,
        "passed": bool(coverage >= ELEMENT_COVERAGE_MIN),
        "missing": missing[:MAX_MISSING_ELEMENTS_LISTED],
        "missing_truncated": max(0, len(missing) - MAX_MISSING_ELEMENTS_LISTED),
        "covered": covered[:MAX_MISSING_ELEMENTS_LISTED],
    }


# ===================== 三件套总入口 =====================

def check_script_consistency(script, novel_meta: dict = None, chapter_text: str = "",
                             chapter: dict = None, episode_no=None,
                             skip_anchor: bool = False, skip_elements: bool = False) -> dict:
    """跑三件套（章节锚定 / 元信息泄漏 / 要素覆盖），返回统一结构报告（零 LLM 成本）

    skip_anchor / skip_elements：整本单集路径（1 集 = 整本小说）下，锚定与要素覆盖
    失去比较基准（无单章正文），显式跳过可避免把「整本 vs 单章」的结构差异误判为问题。
    """
    t0 = time.time()
    meta = (script or {}).get("metadata") or {}
    ep = _as_int(episode_no) or _as_int(meta.get("episode_no")) or 1

    if skip_anchor:
        anchor = {"checked": False, "ok": True, "severity": None, "deviation": None,
                  "reason": "整本单集路径无单章基准，跳过章节锚定",
                  "chapter_index": _as_int(meta.get("chapter_index"))}
    else:
        anchor = check_chapter_anchor(script, novel_meta=novel_meta, chapter=chapter)
    leak = scan_meta_leak(script, novel_meta=novel_meta)
    if skip_elements:
        elements = {"checked": False, "candidate_count": 0, "covered_count": 0,
                    "missing_count": 0, "coverage": 1.0, "coverage_percent": 100.0,
                    "passed": True, "missing": [], "covered": [],
                    "reason": "整本单集路径无单章正文，跳过要素覆盖"}
    else:
        elements = check_element_coverage(chapter_text, script, novel_meta=novel_meta)

    issues = []
    if anchor.get("checked") and not anchor.get("ok"):
        issues.append({
            "severity": anchor.get("severity") or "high",
            "category": "章节锚定",
            "detail": "剧本与章节正文锚定不一致（根因多为章节切分错位）",
            "evidence": str(anchor.get("reason") or "")[:300],
            "fix": "核对章节切分边界后重新生成该集（不建议整集重写）",
            "shot_ids": [],
        })
    if leak.get("hit_count"):
        sample = "；".join(f"#{h['shot_id']}.{h['field']}：{h['excerpt']}" for h in leak["hits"][:5])
        issues.append({
            "severity": "high" if leak["stats"].get("high") else "medium",
            "category": "元信息泄漏",
            "detail": f"{leak['hit_count']} 处章节元信息/创作术语泄漏进镜头文本",
            "evidence": sample[:300],
            "fix": "重写命中镜头，删除元信息与创作术语，只保留可拍摄的画面内容",
            "shot_ids": list(leak.get("shot_ids") or []),
        })
    if elements.get("missing_count"):
        names = "、".join(e["name"] for e in elements["missing"][:12])
        issues.append({
            "severity": "high" if elements["coverage"] < 0.4 else "medium",
            "category": "要素覆盖",
            "detail": (f"原著该章 {elements['candidate_count']} 个场景/道具要素中，"
                       f"{elements['missing_count']} 个未在剧本出现（覆盖率 "
                       f"{elements['coverage_percent']}%）"),
            "evidence": names[:300],
            "fix": "按原著句子定向补生成承载镜头（只增不删）",
            "shot_ids": [],
        })

    stats = {"high": 0, "medium": 0, "low": 0}
    for it in issues:
        stats[it["severity"]] = stats.get(it["severity"], 0) + 1

    report = {
        "version": SCRIPT_CONSISTENCY_VERSION,
        "checked_at": _now(),
        "episode_no": ep,
        "chapter_index": _as_int(meta.get("chapter_index")),
        "anchor": anchor,
        "leak": leak,
        "elements": elements,
        "issues": issues,
        "issue_stats": stats,
        "issue_count": len(issues),
        "shot_count": len([s for s in (script or {}).get("shots") or [] if isinstance(s, dict)]),
        # 可自动修复项：泄漏（有明确 shot_ids）与要素缺失（可定向补生成）
        "fix_needed": bool(leak.get("hit_count") or elements.get("missing_count")),
        "fix_plan": {
            "rewrite_shot_ids": list(leak.get("shot_ids") or []),
            "missing_elements": [e["name"] for e in elements.get("missing") or []],
            "anchor_blocked": bool(anchor.get("checked") and not anchor.get("ok")),
        },
        "passed": bool(not leak.get("hit_count") and anchor.get("ok") and elements.get("passed")),
        "elapsed_sec": round(time.time() - t0, 3),
    }
    return report


# ===================== 修复器 A：元信息泄漏 → 局部重写 =====================

def _leak_issues_for_rewrite(report: dict) -> list:
    """把泄漏命中整理成 continuity.rewrite_shots_for_issues 可消费的 issues"""
    leak = (report or {}).get("leak") or {}
    by_shot = {}
    for h in leak.get("hits") or []:
        by_shot.setdefault(_as_int(h.get("shot_id"), 0), []).append(h)
    issues = []
    for sid, hits in sorted(by_shot.items()):
        fields = sorted({h.get("field") for h in hits})
        evidence = "；".join(f"{h.get('field')}「{h.get('match')}」" for h in hits[:6])
        issues.append({
            "severity": "high" if any(h.get("severity") == "high" for h in hits) else "medium",
            "category": "元信息泄漏",
            "detail": f"镜头 #{sid} 的 {'/'.join(fields)} 混入章节元信息（{len(hits)} 处）",
            "evidence": evidence[:300],
            "fix": "删除章节编号/概要/字数等元信息与创作术语，改成可拍摄的画面或台词",
            "shot_ids": [sid],
        })
    return issues


def _fix_meta_leak(client, script, report, episode_no, events=None, continuity_ctx=None) -> dict:
    """定向重写命中元信息泄漏的镜头（复用 D⑨ 局部重写器，保留其余镜头）"""
    issues = _leak_issues_for_rewrite(report)
    if not issues:
        return {"changed": False, "reason": "无可重写镜头", "rewritten_shot_ids": []}
    try:
        import continuity as continuity_mod
    except Exception as e:  # noqa: BLE001
        return {"changed": False, "reason": f"重写器不可用：{type(e).__name__}", "rewritten_shot_ids": []}

    shot_ids = (report.get("fix_plan") or {}).get("rewrite_shot_ids") or []
    rw = continuity_mod.rewrite_shots_for_issues(
        client, script, issues, int(episode_no),
        continuity_ctx=continuity_ctx or {}, events=events, shot_ids=shot_ids)
    rewritten = list(rw.get("rewritten_shot_ids") or [])
    return {"changed": bool(rewritten), "rewritten_shot_ids": rewritten,
            "notes": rw.get("notes") or [], "error": rw.get("error")}


# ===================== 修复器 B：要素缺失 → 定向补生成 =====================

def supplement_missing_elements(client, chapter_text: str, script, missing: list,
                                episode_no=None, events=None, continuity_ctx=None,
                                max_elements: int = ELEMENT_SUPPLEMENT_MAX) -> dict:
    """按原著句子定向补生成承载缺失要素的镜头（只增不删）

    与 ``coverage.supplement_missing`` 同源思路，但定位依据是**要素**而非句子单元：
    先挑出含缺失要素的原文句子，聚合成块，再交给 ``build_shots_for_chunk`` 编写分镜。
    """
    result = {"added_shots": 0, "new_shot_ids": [], "groups": 0, "errors": [],
              "only_add": True, "supplement_elements": [], "supplement_chars": 0}
    names = [str(e.get("name") if isinstance(e, dict) else e).strip()
             for e in (missing or [])][:max_elements]
    names = [n for n in names if n]
    if not names:
        return result
    if client is None:
        result["errors"].append("未配置 LLM 客户端，跳过要素补生成")
        return result
    try:
        import novel_to_script as nts
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"分镜生成器不可用：{type(e).__name__}")
        return result

    sentences = _split_sentences(chapter_text)
    picked, hit_names = [], set()
    for s in sentences:
        hit = [n for n in names if n in s]
        if hit:
            picked.append(s)
            hit_names.update(hit)
    result["supplement_elements"] = sorted(hit_names)
    if not picked:
        result["errors"].append("原文章节中未找到含缺失要素的句子")
        return result

    groups, buf, size = [], [], 0
    for s in picked[:MAX_SUPPLEMENT_SENTENCES * 2]:
        if buf and size + len(s) > ELEMENT_SUPPLEMENT_CHARS:
            groups.append(buf)
            buf, size = [], 0
        buf.append(s)
        size += len(s)
    if buf:
        groups.append(buf)
    if not groups:
        return result

    bible = {
        "title": (script or {}).get("title") or "",
        "style": (script or {}).get("style") or "",
        "characters": (script or {}).get("characters") or [],
        "items": (script or {}).get("items") or [],
        "scenes": (script or {}).get("scenes") or [],
        "production_notes": (script or {}).get("production_notes") or {},
    }
    existing = list((script or {}).get("shots") or [])
    start_id = max([_as_int(s.get("shot_id"), 0) for s in existing if isinstance(s, dict)] + [0]) + 1
    ep = _as_int(episode_no) or 1

    raw_new = []
    for gi, texts in enumerate(groups, 1):
        chunk = {"index": gi, "total": len(groups), "title": f"要素补生成-{gi}",
                 "text": "\n".join(texts), "char_count": sum(len(t) for t in texts)}
        outline = {"summary": "以下原文片段含剧本遗漏的场景/道具要素，请逐句补写成分镜（只补写这些内容，不改写情节）",
                   "key_beats": [t[:40] for t in texts][:14]}
        target = max(1, nts.estimate_shots_for_chars(chunk["char_count"]))
        try:
            rows = nts.build_shots_for_chunk(client, bible, outline, chunk, target,
                                             events=events, continuity_ctx=continuity_ctx)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"要素补生成第 {gi} 组失败：{type(e).__name__}: {str(e)[:120]}")
            logger.warning(f"要素补生成第 {gi} 组失败：{e}")
            continue
        for s in rows or []:
            if isinstance(s, dict):
                raw_new.append(dict(s))

    if raw_new:
        new_shots = nts._norm_shots(raw_new, bible, 1, start_id=start_id)
        for sh in new_shots:
            sh["episode"] = ep
            sh["element_supplement"] = True     # 标记来源，便于前端与人工复核回溯
        script["shots"] = existing + new_shots
        nts.apply_episode_schema(script)
        result["added_shots"] = len(new_shots)
        result["new_shot_ids"] = [s.get("shot_id") for s in new_shots]
    result["groups"] = len(groups)
    result["supplement_chars"] = sum(len(t) for t in picked)
    return result


# ===================== 闭环主流程 =====================

def _issue_brief(report: dict) -> dict:
    r = report or {}
    return {
        "issues": r.get("issue_count"),
        "leak_hits": (r.get("leak") or {}).get("hit_count"),
        "missing_elements": (r.get("elements") or {}).get("missing_count"),
        "element_coverage_percent": (r.get("elements") or {}).get("coverage_percent"),
        "anchor_ok": (r.get("anchor") or {}).get("ok"),
    }


def _fix_once(client, script, report, episode_no, events=None, continuity_ctx=None) -> dict:
    """执行一轮修复：先清元信息泄漏（文本清理），再补缺失要素（只增不删）"""
    step = {"changed": False, "leak": {}, "elements": {}}
    if (report.get("leak") or {}).get("hit_count"):
        leak_fix = _fix_meta_leak(client, script, report, episode_no,
                                  events=events, continuity_ctx=continuity_ctx)
        step["leak"] = leak_fix
        step["changed"] = step["changed"] or bool(leak_fix.get("changed"))

    if (report.get("elements") or {}).get("missing_count"):
        sup = supplement_missing_elements(client, (report.get("chapter_text") or ""), script,
                                          (report.get("elements") or {}).get("missing") or [],
                                          episode_no=episode_no, events=events,
                                          continuity_ctx=continuity_ctx)
        step["elements"] = sup
        step["changed"] = step["changed"] or bool(sup.get("added_shots"))
    return step


def run_script_consistency_check(client, script, novel_meta: dict = None, chapter_text: str = "",
                                 chapter: dict = None, episode_no=None,
                                 max_rounds: int = CONSISTENCY_MAX_ROUNDS,
                                 auto_fix: bool = True, events: list = None,
                                 continuity_dir: str = None, project_key: str = None,
                                 save: bool = True, continuity_ctx: dict = None,
                                 progress_cb=None,
                                 skip_anchor: bool = False, skip_elements: bool = False) -> dict:
    """P0-3 主流程：三件套校验 → 定向修复 → 复检 → 落盘 + 写回剧本 metadata

    只做「最小改动」：泄漏走局部重写、要素缺失走定向补生成，**绝不整集重生成**。
    章节锚定偏差不自动修（根因在切分，属 P0-1 面），仅作为 issue 记录。
    """
    t0 = time.time()
    meta = (script or {}).setdefault("metadata", {})
    ep = _as_int(episode_no) or _as_int(meta.get("episode_no")) or 1
    rounds_cap = max(0, _as_int(max_rounds, CONSISTENCY_MAX_ROUNDS) or 0)

    def report(phase, msg, pct):
        if progress_cb:
            try:
                progress_cb(phase, ep, ep, msg, pct)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    report("consistency", f"第{ep}集：剧本↔原著一致性校验（章节锚定/元信息泄漏/要素覆盖）…", 98.5)
    result = check_script_consistency(script, novel_meta=novel_meta, chapter_text=chapter_text,
                                      chapter=chapter, episode_no=ep,
                                      skip_anchor=skip_anchor, skip_elements=skip_elements)
    result["chapter_text"] = chapter_text

    history, rounds, fix_errors = [], 0, []
    while (auto_fix and client is not None and result.get("fix_needed")
           and rounds < rounds_cap):
        rounds += 1
        plan = result.get("fix_plan") or {}
        report("consistency",
               f"第{ep}集：命中 {result.get('leak', {}).get('hit_count')} 处元信息泄漏 / "
               f"{result.get('elements', {}).get('missing_count')} 个缺失要素，"
               f"触发定向修复（第 {rounds} 轮）…", 99)
        try:
            step = _fix_once(client, script, result, ep, events=events,
                             continuity_ctx=continuity_ctx)
        except Exception as e:  # noqa: BLE001
            note = f"第 {rounds} 轮修复异常：{type(e).__name__}: {str(e)[:200]}"
            logger.warning(f"第{ep}集剧本一致性修复：{note}")
            fix_errors.append(note)
            break
        step["round"] = rounds
        step["before"] = _issue_brief(result)
        # 复检（用真实结果，不信「已修复」的自我声明）
        result = check_script_consistency(script, novel_meta=novel_meta, chapter_text=chapter_text,
                                          chapter=chapter, episode_no=ep,
                                          skip_anchor=skip_anchor, skip_elements=skip_elements)
        result["chapter_text"] = chapter_text
        step["after"] = _issue_brief(result)
        history.append(step)
        if not step.get("changed"):
            break
        if rounds >= rounds_cap and result.get("fix_needed"):
            step["note"] = f"已达修复轮次上限 {rounds_cap}，剩余问题保留为告警"

    result["auto_fix"] = bool(auto_fix)
    result["fix_rounds"] = rounds
    result["fix_history"] = history
    result["fix_rounds_cap"] = rounds_cap
    result["fix_errors"] = fix_errors
    result["fixed"] = bool(rounds and not result.get("fix_needed"))

    # 落盘 + 写回剧本 metadata（落盘失败不影响剧本本体）
    path = None
    if save:
        try:
            path = save_consistency_report(result, continuity_dir=continuity_dir,
                                           project_key=project_key or meta.get("project_key")
                                           or meta.get("project_name") or (script or {}).get("title"),
                                           episode_no=ep)
            result["report_path"] = path
        except Exception as e:  # noqa: BLE001
            result["save_error"] = str(e)[:200]
            logger.warning(f"剧本一致性报告落盘失败：{e}")
    attach_to_script(script, result, path)
    result["elapsed_sec_total"] = round(time.time() - t0, 2)

    if rounds:
        report("consistency",
               f"第{ep}集：定向修复 {rounds} 轮后，元信息泄漏 "
               f"{result.get('leak', {}).get('hit_count')} 处、要素覆盖率 "
               f"{result.get('elements', {}).get('coverage_percent')}%"
               + (f"（已修复）" if result["fixed"] else "（仍有残留，已记入告警）"), 99.5)
    return result


# ===================== 落盘 / 读回 / 写回剧本 =====================

def consistency_root(continuity_dir: str = None, project_key: str = None) -> str:
    """一致性报告根目录：<continuity_dir>/<项目键>（与 coverage / continuity 同构）"""
    base = continuity_dir or "output/continuity"
    return os.path.abspath(os.path.join(base, _safe_name(project_key or "novel_project")))


def consistency_report_path(continuity_dir: str, project_key: str, episode_no) -> str:
    """单集一致性报告绝对路径：<continuity_dir>/<项目键>/episodes/第N集_剧本一致性.json"""
    ep = _as_int(episode_no, 1) or 1
    return os.path.abspath(os.path.join(
        consistency_root(continuity_dir, project_key), "episodes", f"第{ep}集_剧本一致性.json"))


def load_consistency_report(continuity_dir: str, project_key: str, episode_no) -> dict:
    """读取单集一致性报告（A-4：缺失→空 dict；损坏→.bak 恢复；无 .bak→降级空 dict）。

    用 read_json_strict 三态读取对齐项目契约：缺失返 default(None)、正常返内容、
    损坏先 .bak 恢复；损坏且无 .bak 时 read_json_strict 抛错，这里捕获并降级为空 dict
    （fail-open，不静默清空——read_json_strict 内部已 logger.error 留痕）。
    """
    path = consistency_report_path(continuity_dir, project_key, episode_no)
    try:
        data = read_json_strict(path, None)
    except (ValueError, OSError) as e:
        logger.warning(f"剧本一致性报告读取失败（损坏且无可用 .bak，按无报告处理）{path}：{e}")
        return {}
    return data if isinstance(data, dict) else {}


def save_consistency_report(report: dict, continuity_dir: str = None, project_key: str = None,
                            episode_no=None) -> str:
    """一致性报告落盘（UTF-8 / 缩进 2），返回绝对路径"""
    payload = dict(report or {})
    payload.pop("chapter_text", None)          # 报告不冗余存正文（体积大且可从小说还原）
    ep = _as_int(episode_no or payload.get("episode_no"), 1) or 1
    pkey = _safe_name(project_key or payload.get("project_key") or "novel_project")
    path = consistency_report_path(continuity_dir, pkey, ep)
    payload["project_key"] = pkey
    payload["episode_no"] = ep
    payload["report_path"] = path
    atomic_write_json(path, payload)
    logger.info(f"第{ep}集剧本一致性报告已落盘：{path}")
    return path


def summary_for_meta(report: dict, path: str = None) -> dict:
    """压缩版一致性信息，写回剧本 metadata，供前端面板直接展示"""
    r = dict(report or {})
    leak = r.get("leak") or {}
    elements = r.get("elements") or {}
    anchor = r.get("anchor") or {}
    return {
        "version": r.get("version"),
        "checked_at": r.get("checked_at"),
        "episode_no": r.get("episode_no"),
        "chapter_index": r.get("chapter_index"),
        "passed": bool(r.get("passed")),
        # 章节锚定
        "anchor_checked": bool(anchor.get("checked")),
        "anchor_ok": anchor.get("ok"),
        "anchor_deviation": anchor.get("deviation"),
        "anchor_expected_char_count": anchor.get("expected_char_count"),
        "anchor_script_char_count": anchor.get("script_char_count"),
        "anchor_reason": anchor.get("reason"),
        # 元信息泄漏
        "leak_count": leak.get("hit_count"),
        "leak_stats": leak.get("stats"),
        "leak_shot_ids": leak.get("shot_ids"),
        "leak_samples": (leak.get("hits") or [])[:6],
        # 要素覆盖
        "element_candidate_count": elements.get("candidate_count"),
        "element_covered_count": elements.get("covered_count"),
        "element_missing_count": elements.get("missing_count"),
        "element_coverage": elements.get("coverage"),
        "element_coverage_percent": elements.get("coverage_percent"),
        "element_passed": elements.get("passed"),
        "element_missing": (elements.get("missing") or [])[:20],
        # 修复闭环
        "fix_needed": bool(r.get("fix_needed")),
        "fix_rounds": r.get("fix_rounds"),
        "fix_rounds_cap": r.get("fix_rounds_cap"),
        "fixed": bool(r.get("fixed")),
        "fix_history": r.get("fix_history"),
        # 统一 issue 视图
        "issue_count": r.get("issue_count"),
        "issue_stats": r.get("issue_stats"),
        "issues": (r.get("issues") or [])[:10],
        "report_path": path or r.get("report_path"),
    }


def attach_to_script(script, report: dict, path: str = None) -> dict:
    """把一致性结果写回剧本（前端可直接从剧本读取）"""
    if not isinstance(script, dict):
        return script
    meta = script.setdefault("metadata", {})
    meta["script_consistency"] = summary_for_meta(report, path)
    if path or report.get("report_path"):
        meta["script_consistency_report_path"] = path or report.get("report_path")
    return script


def script_consistency(script: dict) -> dict:
    """从剧本里取一致性摘要（前端用；缺失时返回空）"""
    return ((script or {}).get("metadata") or {}).get("script_consistency") or {}


# ===================== 前端视图（只读；供 /api/consistency 路由使用） =====================

def episode_consistency_view(continuity_dir: str, project_key: str,
                             episode_no) -> dict:
    """单集剧本↔原著一致性视图（P0-3：三件套判定 + 问题清单 + 定向修复轨迹）"""
    ep = int(episode_no)
    report = load_consistency_report(continuity_dir, project_key, ep)
    path = os.path.abspath(consistency_report_path(continuity_dir, project_key, ep))
    if not report:
        return {"project_key": project_key, "episode_no": ep, "available": False,
                "consistency_path": path,
                "note": "该集尚无一致性校验报告（未按新流程重跑，或报告文件缺失）"}
    return {
        "project_key": project_key,
        "episode_no": ep,
        "available": True,
        "consistency_path": path,
        "summary": summary_for_meta(report, path),
        "anchor": report.get("anchor") or {},
        "leak": report.get("leak") or {},
        "elements": report.get("elements") or {},
        "issues": report.get("issues") or [],
        "fix_history": report.get("fix_history") or [],
        "checked_at": report.get("checked_at"),
    }
