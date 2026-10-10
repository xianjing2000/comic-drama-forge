"""
小说解析器 —— 支持任意常见格式小说文件的上传解析

覆盖格式：
- txt / text / log：自动探测编码（utf-8 / utf-8-sig / GBK / GB18030 / BIG5 / utf-16），
  中文小说常见的 GBK 系列编码优先使用 charset-normalizer 统计判定，并用「严格 utf-8 解码」作为第一判据
- md / markdown：按纯文本处理（章节支持 `# 标题`）
- docx：python-docx 抽取段落 + 表格
- pdf：pypdf 逐页抽取（扫描版 PDF 会给出 OCR 提示）
- epub：ebooklib 抽取正文（缺失时回退 zipfile 解析）
- html / htm / xhtml：标准库 HTMLParser 去标签抽取正文

统一输出：纯文本 + 字符统计 + 章节切分，并落盘到项目 novels 目录。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from html import unescape as html_unescape
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# 支持的扩展名（.doc 为旧版二进制格式，单独给出友好错误提示）
TEXT_EXTS = {".txt", ".text", ".log", ".md", ".markdown"}
DOC_EXTS = {".docx", ".pdf", ".epub", ".html", ".htm", ".xhtml"}
SUPPORTED_EXTS = TEXT_EXTS | DOC_EXTS
LEGACY_EXTS = {".doc", ".wps", ".rtf", ".mobi", ".azw", ".azw3"}

MAX_CHAPTERS_KEPT = 3000


class NovelParseError(Exception):
    """小说解析失败（面向用户的友好错误）"""


# ===================== 编码探测 =====================

def _han_ratio(text: str) -> float:
    if not text:
        return 0.0
    sample = text[:200000]
    han = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return han / max(len(sample), 1)


def _ascii_ratio(text: str) -> float:
    if not text:
        return 0.0
    sample = text[:200000]
    asc = sum(1 for ch in sample if ord(ch) < 128)
    return asc / max(len(sample), 1)


def _bad_ratio(text: str) -> float:
    if not text:
        return 1.0
    sample = text[:200000]
    bad = sample.count("\ufffd") + sum(
        1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t"
    )
    return bad / max(len(sample), 1)


def decode_bytes(blob: bytes):
    """把字节流解码为文本，返回 (text, encoding, note)。

    策略（中文小说友好）：
    1. BOM 判定（utf-8-sig / utf-16）
    2. 严格 utf-8 解码成功 → utf-8（GBK 字节几乎不可能整体通过 utf-8 严格校验）
    3. charset-normalizer 统计判定（能正确识别 GBK / GB18030 / BIG5 等）
    4. 依次回退 gb18030 → big5 → cp936，用「汉字占比 / ASCII 占比 / 坏字符占比」打分择优
    """
    if blob.startswith(b"\xef\xbb\xbf"):
        return blob.decode("utf-8-sig", errors="replace"), "utf-8-sig", "BOM 识别为 UTF-8(BOM)"
    if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return blob.decode("utf-16", errors="replace"), "utf-16", "BOM 识别为 UTF-16"

    try:
        text = blob.decode("utf-8")
        if "\ufffd" not in text and _bad_ratio(text) < 0.001:
            return text, "utf-8", "严格 UTF-8 解码成功"
    except UnicodeDecodeError as e:
        logger.debug("严格 UTF-8 解码失败（试下一个候选编码）：%s", e)

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(blob).best()
        if best is not None and best.encoding:
            enc = str(best.encoding).replace("_", "-")
            try:
                text = blob.decode(enc, errors="replace")
                if _bad_ratio(text) < 0.005:
                    return text, enc, f"charset-normalizer 统计判定为 {enc}"
            except (LookupError, UnicodeDecodeError) as e:
                logger.debug("统计判定编码解码失败（试下一个候选编码）：%s", e)
    except ImportError as e:
        logger.warning("charset-normalizer 未安装（%s），中文编码将使用回退策略", e)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"charset-normalizer 探测异常：{e}")

    best_pair = (None, "", -1.0)
    for enc in ("gb18030", "big5", "cp936"):
        try:
            text = blob.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _han_ratio(text) * 2 + _ascii_ratio(text) - _bad_ratio(text) * 10
        if score > best_pair[2]:
            best_pair = (text, enc, score)
    if best_pair[0] is not None:
        return best_pair[0], best_pair[1], f"回退解码（{best_pair[1]}），适用于 GBK/GB2312/GB18030 中文小说"

    text = blob.decode("gb18030", errors="replace")
    return text, "gb18030(errors=replace)", "强制 GB18030 解码，部分字符可能丢失"


# ===================== 各格式抽取 =====================

class _HTMLTextExtractor(HTMLParser):
    """去掉 script/style，抽取正文文本"""

    # 注意：只放"成对出现"的容器标签；meta / link / br 等空元素无闭合标签，
    # 若放进 _SKIP 会让跳过深度只增不减，从而把整篇正文全部丢弃。
    _SKIP = {"script", "style", "noscript", "head", "svg", "iframe", "template", "canvas", "form"}
    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "blockquote", "pre", "td", "th", "hr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    text = ""
    try:
        parser.feed(html)
        text = parser.text()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"HTMLParser 解析异常，改用正则清洗：{e}")
    if len(re.sub(r"\s", "", text)) < 20:
        # 兜底：异常/非常规 HTML（如残缺标签、大写标签、编码声明错乱）改用正则清洗
        cleaned = re.sub(r"(?is)<(script|style|noscript|head|svg).*?</\1>", " ", html)
        cleaned = re.sub(r"(?s)<[^>]+>", "\n", cleaned)
        cleaned = html_unescape(cleaned)
        if len(re.sub(r"\s", "", cleaned)) > len(re.sub(r"\s", "", text)):
            text = cleaned
    return text


def _read_text_file(path: str):
    with open(path, "rb") as f:
        blob = f.read()
    text, enc, note = decode_bytes(blob)
    return text, enc, note, []


def _read_docx(path: str):
    try:
        import docx  # python-docx
    except ImportError as e:  # pragma: no cover
        raise NovelParseError("缺少 python-docx 依赖，无法解析 .docx") from e
    doc = docx.Document(path)
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts), "docx(xml)", "python-docx 抽取段落与表格", []


def _read_pdf(path: str):
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise NovelParseError("缺少 pypdf 依赖，无法解析 .pdf") from e
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PDF 某页抽取失败：{e}")
            pages.append("")
    text = "\n\n".join(pages)
    warnings = []
    if len(text.strip()) < 50:
        warnings.append("PDF 抽取到的文本极少，可能是扫描版图片 PDF，需先做 OCR 才能转剧本")
    return text, "pdf", f"pypdf 抽取 {len(reader.pages)} 页", warnings


def _read_epub(path: str):
    warnings = []
    try:
        import ebooklib
        from ebooklib import epub

        book = epub.read_epub(path)
        parts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                raw = item.get_content().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            plain = html_to_text(raw).strip()
            if plain:
                parts.append(plain)
        if parts:
            return "\n\n".join(parts), "epub", f"ebooklib 抽取 {len(parts)} 个文档节点", warnings
        warnings.append("ebooklib 未抽到正文，已回退 zipfile 解析")
    except ImportError:
        warnings.append("未安装 ebooklib，已回退 zipfile 解析")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"ebooklib 解析异常（{e}），已回退 zipfile 解析")

    import zipfile

    with zipfile.ZipFile(path) as zf:
        names = [
            n for n in zf.namelist()
            if n.lower().endswith((".xhtml", ".html", ".htm")) and "META-INF" not in n
        ]
        names.sort()
        parts = []
        for n in names:
            raw = zf.read(n).decode("utf-8", errors="replace")
            plain = html_to_text(raw).strip()
            if plain:
                parts.append(plain)
    if not parts:
        raise NovelParseError("EPUB 中未找到可读正文")
    return "\n\n".join(parts), "epub(zip)", f"zipfile 抽取 {len(parts)} 个文档节点", warnings


def _read_html(path: str):
    with open(path, "rb") as f:
        blob = f.read()
    html, enc, note = decode_bytes(blob)
    text = html_to_text(html)
    return text, enc, f"HTML 去标签（{note}）", []


def normalize_text(text: str) -> str:
    text = (text or "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+$", "", text, flags=re.M)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


# ===================== 章节切分 =====================

_CN_NUM = "0-9０-９零一二三四五六七八九十百千万两〇"
CHAPTER_PATTERNS = [
    re.compile(rf"^[ \t\u3000]*第[ \t]*[{_CN_NUM}]{{1,12}}[ \t]*[章回节卷篇集部][ \t]*[：:.、·\-—－]?[ \t]*(.{{0,50}})$", re.M),
    re.compile(r"^[ \t\u3000]*(?:Chapter|CHAPTER|chapter)[ \t]+\d{1,5}[ \t]*[：:.、\-]?[ \t]*(.{0,50})$", re.M),
    # Markdown 章节（2026-09-22 P0-1 收窄）：旧规则 `#{1,4}` 把 1~4 级标题全当章节——
    # 「## 本章概要 / 爽点 / 情绪曲线」混入后 42 章书被解析成 170「章」，第 1 集取到
    # 38 字书名标题。现 md 只认「一级标题且含第X章结构」；H2~H4 与无结构的 H1（书名）
    # 不再触发切分。`^#` 锚定 + [^#] 开头 → `##` 行天然排除；「第..章」段本身作为
    # 整行匹配已隐含首条纯文本规则，无需单独给 H1 加章号正则。
    re.compile(rf"^#[^#][ \t\u3000]*第[ \t]*[{_CN_NUM}]{{1,12}}[ \t]*[章回节卷篇集部][ \t]*[：:.、·\-—－]?[ \t]*\S.{{0,60}}$", re.M),
    re.compile(r"^[ \t\u3000]*(?:序章|楔子|前言|引子|后记|尾声|终章|大结局|番外).{0,30}$", re.M),
]


# 正文没有任何章节标题时的兜底切分粒度（字符数）。
# 短篇/话本/测试文本常常没有「第X章」标记，若直接返回空章节列表，
# 会导致「未识别到章节，无法自动分集生产」（缺陷 D3）与「待生产 0 集」（缺陷 D2）。
FALLBACK_CHAPTER_CHARS = 3000


def _fallback_split(text: str, chunk_chars: int):
    """无章节标记时的兜底切分：按行边界聚合成长度约 chunk_chars 的若干节。

    短文本（<= chunk_chars）自然只会得到 1 节「全文」，正好适配极短测试小说。
    """
    total = len(text)
    if total <= 0:
        return []
    n = max(int(chunk_chars or 0), 500)
    if total <= n:
        return [{"index": 1, "title": "全文", "start": 0, "end": total, "char_count": total}]

    chapters = []
    seg_start = 0
    idx = 1
    for m in re.finditer(r"(?m)^", text):
        pos = m.start()
        if pos <= seg_start:
            continue
        if pos - seg_start >= n:
            chapters.append({
                "index": idx,
                "title": f"第{idx}节",
                "start": seg_start,
                "end": pos,
                "char_count": pos - seg_start,
            })
            seg_start = pos
            idx += 1
            if len(chapters) >= MAX_CHAPTERS_KEPT:
                break
    if seg_start < total and len(chapters) < MAX_CHAPTERS_KEPT:
        chapters.append({
            "index": idx,
            "title": f"第{idx}节",
            "start": seg_start,
            "end": total,
            "char_count": total - seg_start,
        })
    return chapters


def split_chapters(text: str, fallback_chars: int = FALLBACK_CHAPTER_CHARS,
                   extra_patterns: list = None):
    """切分章节，返回 [{'index','title','start','end','char_count'}]（不含正文，避免重复占用内存）

    找不到任何章节标题时不再返回空列表，而是走 _fallback_split 兜底切分，
    保证下游「分集生产」始终有集可产。

    extra_patterns（可选）：额外正则字符串列表（由 chapter_llm.derive_patterns
    从 LLM 返回），编译后并入 CHAPTER_PATTERNS 一起扫描；编译失败的条目静默跳过。
    """
    if not text:
        return []
    marks = []
    patterns = list(CHAPTER_PATTERNS)
    if extra_patterns:
        for ps in extra_patterns:
            try:
                patterns.append(re.compile(ps, re.M))
            except (re.error, TypeError):
                continue
    for pat in patterns:
        for m in pat.finditer(text):
            line = m.group(0).strip()
            if not line or len(line) > 70:
                continue
            marks.append((m.start(), line[:70]))
    if not marks:
        return _fallback_split(text, fallback_chars)

    marks.sort()
    dedup = []
    seen_lines = set()
    for pos, title in marks:
        line_no = text.count("\n", 0, pos)
        if line_no in seen_lines:
            continue
        seen_lines.add(line_no)
        title = re.sub(r"^#+[ \t]*", "", title).strip()
        dedup.append((pos, title or f"第{len(dedup) + 1}节"))
        if len(dedup) >= MAX_CHAPTERS_KEPT:
            break

    chapters = []
    for i, (pos, title) in enumerate(dedup):
        end = dedup[i + 1][0] if i + 1 < len(dedup) else len(text)
        chapters.append({
            "index": i + 1,
            "title": title,
            "start": pos,
            "end": end,
            "char_count": end - pos,
        })
    # 折叠「卷/部」标题壳（第[数字][卷] 与 第[数字][节] 同正则，卷标题无正文）
    return collapse_shell_chapters(chapters, text)


# 「卷/部/篇」这类结构标题与「章/节」共用同一条正则（第[数字][章回节卷篇集部]），
# 于是「第一卷：魔性不改」会被切成一个独立章节 —— 而它下面一个字正文都没有。
# 下游按 index 取章 → 取到空壳 → 模型只拿到 11 个字的标题，把整集剧本凭空编出来
#（2026-10-02 实测：《蛊真人》第 1 集 = 「第一卷：魔性不改」11 字，产出 8 个镜头全是
# 「魔性不改 / 大道无情 / 唯我独尊 / 为了长生 / 手段尽可尽」这类模型自造的四字口号，
# 原文台词一句没用，且整半章（春秋蝉逆转重生 · 青茅山古月山寨）彻底丢失，
# 而覆盖率检查仍报 100% —— 因为根本没有正文单元可核对）。
# 这类「只有标题行、正文近空」的壳章必须并入其后的真实章节，不能单独成章。
SHELL_CHAPTER_BODY_CHARS = 60


def chapter_body_chars(text: str, ch: dict) -> int:
    """章节正文（去掉标题行与全部空白后）的字符数。

    标题行不计入 —— 壳章的特征正是「去掉标题行后什么都不剩」。
    """
    try:
        seg = text[int(ch.get("start") or 0):int(ch.get("end") or 0)]
    except (TypeError, ValueError):
        return 0
    nl = seg.find("\n")
    body = seg[nl + 1:] if nl >= 0 else ""
    return len(re.sub(r"\s", "", body))


def collapse_shell_chapters(chapters: list, text: str,
                            min_body: int = SHELL_CHAPTER_BODY_CHARS) -> list:
    """把「只有标题、正文近空」的壳章并入下一章，并重排 index（1..N）。

    - 只折叠 **后面还有章节** 的短条目；末章若也是壳章则丢弃（它不是正文）。
    - 被折叠的标题（如卷名）记入下一章的 volume 字段，信息不丢。
    - 真正写得短的章节（短篇/测试文本）只要正文不空就不受影响；
      若折叠后一章不剩，原样返回 —— 绝不产出空章节表。
    """
    if not chapters:
        return []
    out = []
    pending = []
    for i, ch in enumerate(chapters):
        if not isinstance(ch, dict):
            continue
        body = chapter_body_chars(text, ch)
        is_last = (i + 1 >= len(chapters))
        if body < min_body:
            if is_last:
                continue
            pending.append(str(ch.get("title") or "").strip())
            continue
        item = dict(ch)
        if pending:
            vols = [x for x in pending if x]
            if vols:
                item["volume"] = " / ".join(vols)
            pending = []
        item["index"] = len(out) + 1
        out.append(item)
    if not out:
        keep = [dict(c) for c in chapters if isinstance(c, dict)]
        for n, c in enumerate(keep, 1):
            c["index"] = n
        return keep
    return out


def apply_chapter_structure(chapters: list, verdict: dict, text: str) -> list:
    """按 LLM 判决剔除「结构标题」条目，并重排 index。

    **LLM 只提供判断，删条必须通过规则复核**：只有同时满足
    「LLM 判为结构标题」+「去掉标题行后正文近空」两个条件才剔除，
    绝不因为模型一句话就删掉可能有正文的章节（误删正文不可逆）。
    被剔除的标题记入下一章的 volume 字段。
    """
    if not chapters or not isinstance(verdict, dict):
        return chapters
    raw = verdict.get("volume_indices") or []
    vols = set()
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            vols.add(n)
    if not vols:
        return chapters
    out, pending = [], []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        try:
            idx = int(ch.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        body = chapter_body_chars(text, ch)
        if idx in vols and body < SHELL_CHAPTER_BODY_CHARS:
            pending.append(str(ch.get("title") or "").strip())
            continue
        item = dict(ch)
        if pending:
            existing = str(item.get("volume") or "").strip()
            have = [x.strip() for x in existing.split("/") if x.strip()]
            # 规则折叠可能已经把同一个卷名写进 volume，这里去重，避免
            # 「第一卷：魔性不改 / 第一卷：魔性不改」这种重复
            extra = [x for x in (y.strip() for y in pending) if x and x not in have]
            if extra:
                joined = " / ".join(extra)
                item["volume"] = (existing + " / " + joined) if existing else joined
            pending = []
        item["index"] = len(out) + 1
        out.append(item)
    return out if out else chapters


def ensure_chapter_structure(novels_dir: str, novel_id: str, llm_client=None,
                             force: bool = False) -> dict:
    """生成剧本前的「章节目录结构体检」：让 LLM 判断哪些条目只是结构标题。

    在**生成剧本之前**调用（用户要求：先像人一样读目录判断真章节，再写剧本）。

    - 结果缓存进 meta（chapter_structure / chapter_structure_audited_at），
      同一本书只跑一次 LLM，后续调用零成本（force=True 可重跑）；
    - LLM 未配置 / 调用失败 / 结论不可采信 → 直接返回原 meta，
      沿用 collapse_shell_chapters 的规则折叠结果，绝不阻断生产；
    - 应用判决时走 apply_chapter_structure 的规则复核（不盲信模型）。
    """
    meta = get_novel(novels_dir, novel_id)
    if not isinstance(meta, dict):
        return meta or {}
    if meta.get("chapter_structure") and not force:
        return meta
    if llm_client is None:
        return meta
    chapters = meta.get("chapters") or []
    if not chapters:
        return meta
    text = read_novel_text(novels_dir, novel_id) or ""
    if not text:
        return meta
    try:
        import chapter_llm
        verdict = chapter_llm.analyze_chapter_structure(
            llm_client, chapters, text, meta.get("name") or meta.get("title") or "")
    except Exception as e:  # noqa: BLE001  体检失败不阻断生产
        logger.warning("章节目录结构体检失败（沿用规则折叠）：%s %s", novel_id, e)
        return meta
    if not verdict:
        return meta
    try:
        before = len(chapters)
        new_chapters = apply_chapter_structure(chapters, verdict, text)
        meta["chapter_structure"] = verdict
        meta["chapter_structure_audited_at"] = datetime.now().isoformat(timespec="seconds")
        if len(new_chapters) != before:
            meta["chapters"] = new_chapters
            meta["chapter_count"] = len(new_chapters)
            meta["chapter_structure_note"] = (
                "LLM 目录体检再剔除 %d 个结构标题条目" % (before - len(new_chapters)))
            logger.info("章节目录体检：%s %d → %d 章", novel_id, before, len(new_chapters))
        with open(os.path.join(novels_dir, "%s.json" % novel_id), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        index = _load_index(novels_dir)
        hit = False
        for m in index:
            if m.get("novel_id") == novel_id:
                m["chapter_count"] = meta.get("chapter_count")
                m["chapter_structure"] = verdict.get("structure") or ""
                hit = True
        if hit:
            _save_index(novels_dir, index)
    except OSError as e:  # noqa: BLE001
        logger.warning("目录体检结果回写失败（内存内已生效）：%s %s", novel_id, e)
    return meta


def guess_title(text: str, fallback: str) -> str:
    for raw in text.split("\n")[:60]:
        line = raw.strip().strip("#").strip()
        if not line:
            continue
        if any(p.match(line) for p in CHAPTER_PATTERNS):
            continue
        if 2 <= len(line) <= 30:
            return line
    return fallback


# ===================== 对外主流程 =====================

def extract_text(path: str, display_name: str = None) -> dict:
    """从任意支持格式抽取纯文本，返回 {text, ext, format, encoding, encoding_note, warnings}"""
    name = display_name or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in LEGACY_EXTS:
        raise NovelParseError(
            f"暂不支持 {ext} 格式（旧版二进制/私有格式），请先另存为 .docx / .txt / .pdf 后再上传"
        )
    if ext in TEXT_EXTS:
        text, enc, note, warns = _read_text_file(path)
        fmt = "markdown" if ext in (".md", ".markdown") else "text"
    elif ext == ".docx":
        text, enc, note, warns = _read_docx(path)
        fmt = "docx"
    elif ext == ".pdf":
        text, enc, note, warns = _read_pdf(path)
        fmt = "pdf"
    elif ext == ".epub":
        text, enc, note, warns = _read_epub(path)
        fmt = "epub"
    elif ext in (".html", ".htm", ".xhtml"):
        text, enc, note, warns = _read_html(path)
        fmt = "html"
    else:
        # 未知扩展名：按纯文本尽力解析
        text, enc, note, warns = _read_text_file(path)
        fmt = ext.lstrip(".") or "text"
        warns = list(warns) + [f"未知扩展名 {ext}，已按纯文本尽力解析"]

    return {
        "text": text,
        "ext": ext,
        "format": fmt,
        "encoding": enc,
        "encoding_note": note,
        "warnings": warns,
    }


def ingest_novel(raw_path: str, filename: str, novels_dir: str,
                 llm_client=None) -> dict:
    """解析上传的小说原始文件 → 落盘标准化文本 + 元数据，返回元数据 dict

    llm_client（可选）：前端「文本分析模型」的 LLMClient。传入时会先用 LLM 归纳
    这本书的章节标题正则（chapter_llm.derive_patterns），再并入 split_chapters 的正则
    扫描——每本小说格式各异，纯正则容易漏检；LLM 失败/未配置时自动退回纯正则，
    绝不阻断上传。
    """
    os.makedirs(novels_dir, exist_ok=True)
    info = extract_text(raw_path, filename)
    text = normalize_text(info["text"])
    if len(text.strip()) < 20:
        raise NovelParseError("未能从文件中提取到有效文本（内容过短或为扫描件）")

    extra_patterns = []
    if llm_client is not None:
        try:
            import chapter_llm
            extra_patterns = chapter_llm.derive_patterns(llm_client, text) or []
            if extra_patterns:
                logger.info(f"LLM 归纳章节正则 {len(extra_patterns)} 条：{extra_patterns}")
        except Exception as e:  # noqa: BLE001  LLM 环节失败不阻断上传，退回纯正则
            logger.warning(f"LLM 章节识别失败（退回纯正则切分）：{e}")

    chapters = split_chapters(text, extra_patterns=extra_patterns or None)
    novel_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    text_name = f"{novel_id}.txt"
    text_path = os.path.join(novels_dir, text_name)
    with open(text_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    stem = os.path.splitext(os.path.basename(filename))[0]
    meta = {
        "novel_id": novel_id,
        "name": stem or novel_id,
        "title": guess_title(text, stem or novel_id),
        "source_filename": os.path.basename(filename),
        "ext": info["ext"],
        "format": info["format"],
        "encoding": info["encoding"],
        "encoding_note": info["encoding_note"],
        "warnings": info["warnings"],
        "size_bytes": os.path.getsize(raw_path),
        "total_chars": len(text),
        "char_count": len(re.sub(r"\s", "", text)),
        "line_count": text.count("\n") + 1,
        "chapter_count": len(chapters),
        "chapter_source": "llm" if extra_patterns else "regex",
        "chapters_collapsed": True,
        "chapters": chapters[:MAX_CHAPTERS_KEPT],
        "text_file": text_name,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(os.path.join(novels_dir, f"{novel_id}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    index = _load_index(novels_dir)
    index = [m for m in index if m.get("novel_id") != novel_id]
    index.append({k: v for k, v in meta.items() if k != "chapters"})
    index.sort(key=lambda m: m.get("uploaded_at", ""), reverse=True)
    _save_index(novels_dir, index)
    logger.info(f"小说已入库：{novel_id}（{meta['char_count']} 字 / {meta['chapter_count']} 章）")
    return meta


# ===================== 索引与读取 =====================

def _index_path(novels_dir: str) -> str:
    return os.path.join(novels_dir, "index.json")


def _load_index(novels_dir: str) -> list:
    path = _index_path(novels_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"小说索引读取失败：{e}")
        return []


def _save_index(novels_dir: str, index: list):
    os.makedirs(novels_dir, exist_ok=True)
    with open(_index_path(novels_dir), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def list_novels(novels_dir: str) -> list:
    index = _load_index(novels_dir)
    # 过滤掉实体文件已丢失的条目
    alive = []
    for m in index:
        text_file = m.get("text_file") or f"{m.get('novel_id')}.txt"
        if os.path.isfile(os.path.join(novels_dir, text_file)):
            alive.append(m)
    if len(alive) != len(index):
        _save_index(novels_dir, alive)
    return alive


def _migrate_shell_chapters(novels_dir: str, novel_id: str, meta: dict) -> dict:
    """老库惰性迁移：把已存 meta 里的「卷标题壳章」折叠掉并回写（一次性）。

    只在 meta 无 chapters_collapsed 标记时执行；正文读不到就只打标记，
    迁移失败一律不阻断读取（返回原 meta）。
    """
    text_file = meta.get("text_file") or f"{novel_id}.txt"
    text_path = os.path.join(novels_dir, text_file)
    text = ""
    if os.path.isfile(text_path):
        try:
            with open(text_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
    old = meta.get("chapters") or []
    if text and old:
        chapters = collapse_shell_chapters(old, text)
        meta["chapters"] = chapters
        meta["chapter_count"] = len(chapters)
        if len(chapters) != len(old):
            meta["chapter_collapse_note"] = (
                f"已折叠 {len(old) - len(chapters)} 个空壳章节标题（卷/部标题无正文）")
            logger.info("章节壳章折叠：%s %d → %d 章", novel_id, len(old), len(chapters))
    meta["chapters_collapsed"] = True
    try:
        with open(os.path.join(novels_dir, f"{novel_id}.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        index = _load_index(novels_dir)
        hit = False
        for m in index:
            if m.get("novel_id") == novel_id:
                m["chapter_count"] = meta.get("chapter_count")
                m["chapters_collapsed"] = True
                hit = True
        if hit:
            _save_index(novels_dir, index)
    except OSError as e:  # noqa: BLE001
        logger.warning("章节折叠结果回写失败（内存内已生效）：%s %s", novel_id, e)
    return meta


def get_novel(novels_dir: str, novel_id: str) -> dict:
    path = os.path.join(novels_dir, f"{novel_id}.json")
    if not os.path.isfile(path):
        raise NovelParseError(f"小说不存在：{novel_id}")
    with open(path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    # 惰性迁移：老库（2026-10-02 之前入库）的 chapters 里可能混着「卷标题壳章」，
    # 第一次读到就折叠并回写；之后靠 chapters_collapsed 标记跳过，不再读正文。
    if isinstance(meta, dict) and meta.get("chapters") and not meta.get("chapters_collapsed"):
        try:
            meta = _migrate_shell_chapters(novels_dir, novel_id, meta)
        except Exception as e:  # noqa: BLE001  迁移失败绝不影响读取
            logger.warning("章节壳章折叠迁移失败（沿用原章节表）：%s %s", novel_id, e)
    return meta


def remove_novel(novels_dir: str, novel_id: str, trash_dir: str = None) -> dict:
    """删除小说（软删：把 <id>.json + <id>.txt 移入回收站，并从 index.json 摘除，可手工还原）。

    与项目删除同一风格——**不物理删除**。`trash_dir` 缺省为 ``<novels_dir>/_trash``，
    实际落 ``<trash_dir>/<时间戳>_<novel_id>/``。已不存在的条目返回 success=False。
    返回 {success, moved:[{kind,from,to}], skipped:[...], recoverable, trash_dir}。
    """
    novel_id = str(novel_id or "").strip()
    if not novel_id:
        return {"success": False, "error": "novel_id 为空"}
    # 实体文件是否存在（json 在才算真登记过）
    meta_path = os.path.join(novels_dir, f"{novel_id}.json")
    if not os.path.isfile(meta_path):
        return {"success": False, "error": f"小说不存在：{novel_id}"}

    trash_root = trash_dir or os.path.join(novels_dir, "_trash")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst_root = os.path.join(trash_root, f"{stamp}_{novel_id}")
    os.makedirs(dst_root, exist_ok=True)

    moved, skipped = [], []
    files = [meta_path, os.path.join(novels_dir, f"{novel_id}.txt")]
    # 若 meta 里登记了自定义 text_file（老数据），一并收走
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            tf = (json.load(f) or {}).get("text_file")
        if tf:
            files.append(os.path.join(novels_dir, tf))
    except Exception:  # noqa: BLE001
        pass
    for src in files:
        if not src or not os.path.isfile(src):
            continue
        dst = os.path.join(dst_root, os.path.basename(src))
        try:
            shutil.move(src, dst)
            moved.append({"kind": "novel", "from": src, "to": dst})
        except (OSError, shutil.Error) as e:  # noqa: BLE001
            skipped.append({"path": src, "error": str(e)})

    # 从 index.json 摘除（软删：数据在回收站可还原）
    index = _load_index(novels_dir)
    new_index = [m for m in index if (m.get("novel_id") or m.get("id")) != novel_id]
    if len(new_index) != len(index):
        _save_index(novels_dir, new_index)

    return {"success": True, "moved": moved, "skipped": skipped,
            "recoverable": True, "trash_dir": dst_root}




NOVELS_DIR = __import__('config').NOVELS_DIR


def chapters_and_text(novel_meta: dict) -> tuple:
    """返回 (章节列表, 小说全文)。

    拆章（一章拆多集）需要在**正文**上找语义切点，正文此前已在这里读过一次，
    所以直接把全文一并带出，避免调用方二次读盘。

    ⚠️ 2026-10-10 从 autopilot 搬来：pipeline 原本 import autopilot 只为用它，
    而 autopilot 又 import pipeline，形成循环。此函数只依赖本模块与 config，
    放这里最自然，搬走后 pipeline 不再需要 import autopilot。
    """
    if not novel_meta:
        return [], ''
    text = ''
    try:
        text = read_novel_text(NOVELS_DIR, novel_meta.get('novel_id')) or ''
    except Exception as e:  # noqa: BLE001
        logger.warning('小说正文读取失败：%s', e)
    if not text:
        return [], ''
    items = []
    for c in split_chapters(text):
        items.append({
            'index': int(c.get('index') or len(items) + 1),
            'title': c.get('title') or ('第%d章' % c.get('index')),
            'start': c.get('start') or 0,
            'end': c.get('end') or 0,
            'char_count': c.get('char_count') or 0,
        })
    return items, text


def read_novel_text(novels_dir: str, novel_id: str, max_chars: int = None) -> str:
    meta = get_novel(novels_dir, novel_id)
    text_path = os.path.join(novels_dir, meta.get("text_file") or f"{novel_id}.txt")
    if not os.path.isfile(text_path):
        raise NovelParseError(f"小说正文文件缺失：{text_path}")
    with open(text_path, "r", encoding="utf-8") as f:
        text = f.read()
    if max_chars and max_chars > 0:
        return text[:max_chars]
    return text


def preview_novel(novels_dir: str, novel_id: str, offset: int = 0, limit: int = 4000) -> dict:
    meta = get_novel(novels_dir, novel_id)
    text = read_novel_text(novels_dir, novel_id)
    offset = max(0, int(offset or 0))
    limit = max(200, min(int(limit or 4000), 20000))
    chunk = text[offset:offset + limit]
    return {
        "novel_id": novel_id,
        "name": meta.get("name"),
        "title": meta.get("title"),
        "offset": offset,
        "limit": limit,
        "text": chunk,
        "total_chars": len(text),
        "has_more": offset + limit < len(text),
    }
