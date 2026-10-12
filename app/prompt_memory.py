"""
prompt_memory.py — AI 提示词记忆库

作用：把「质检说出的具体缺陷 + 当时用的提示词」沉淀成一条条经验，
下次构造同类提示词时自动把经验补进去，避免重复犯同样的错。

数据落盘：output/lessons/lessons.jsonl（追加式，逐条记录）
语义：以「缺陷关键词」为依据做匹配，命中则把对应的修正建议追加进
生成的提示词。

⚠️ 唯一实例契约（T01）：
    本模块**有且只有一个**内存列表与**唯一一个**写入者。
    历史缺陷：`PromptMemory`（基础）与 `PromptMemoryEnhanced`（增强）各持一份
    内存列表，却把 `_path` 定死为同一个 `lessons.jsonl`，而 `_flush()` 用
    `open(path, "w")` 整文件覆盖 → 两份列表抢写同一文件、last-writer-wins，
    增强侧刚写的 `kind="script"` 会被基础侧的下一次 flush 整体抹掉。
    现收敛为「唯一实现 `PromptMemory`」：增强能力（分类/优先级/上下文/衰减）
    整体折入，`PromptMemoryEnhanced` 退化为空壳别名；`get_enhanced_memory`
    与 `get_memory` 返回**同一对象**；`_flush()` 改为原子写（唯一临时名 +
    fsync + `os.replace` 退避重试），彻底消除双单例互抹与半截文件。

用法：
    mem = PromptMemory(root_dir)
    mem.record(proj, kind, prompt, issues, reason)     # 质检不达届时调用，记教训
    mem.suggestions(kind, prompt) -> [str]             # 生成提示词前调用，取修正建议
    mem.learned_prompt(kind, prompt, text_block)       # 拼装成最后的大提示词
    mem.hash(prompt) -> str                            # 提示词指纹
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# 修正建议侧的「规则关键词种子」：质检 defect 文案经过这些词归一化后，
# 才能稳定命中同名教训。规则按优先级排列，命中即停。
#
# 注意：material 类缺陷（发色/服饰/冠饰等具体道具）无法用统一规则表达，
# 但质检的 issue 里通常会带上原文专有名词，我们把这些专有名词也作为
# 匹配键的一部分（通过 extract_core_terms），因此「发冠样式错误」这类
# 长期会通过「专有名词 + 类别」的组合命中和复发。
_MATCH_RULES: List[str] = [
    "构图", "遮挡", "面部", "发色", "发冠", "发型", "服饰", "配色", "衣服",
    "背景", "场景", "光影", "对比", "畸变", "糊", "模糊", "噪",
    "灯笼", "水印", "字幕", "肢体", "手", "脸", "眼睛", "比例", "拥挤",
    "人物数量", "多余人", "面板", "文字", "logo", "重复", "武器", "剑",
    "风格", "画风",
]

# 修正建议为空时，按类别给一个兜底建议
_CLASS_HINTS: dict = {
    "构图": "调整构图，突出主体，避免主体被边缘裁切或过小。",
    "遮挡": "避免前景杂物遮挡主体面部与关键动作。",
    "面部": "进一步刻画面部细节，避免五官模糊或扭曲。",
    "发色": "严格保持参考图中的发色与发质，不得自由改变。",
    "发冠": "严格还原参考图中的发冠/发饰样式与位置。",
    "发型": "严格还原参考图中的发型，不得改变扎法或发髻。",
    "服饰": "严格保持参考图中的服装款式、颜色与材质。",
    "配色": "全图色调统一，避免局部色彩跳变或过饱和。",
    "背景": "丰富背景层次，避免大面积空白或单调墙面。",
    "场景": "增强场景通透感与空间透视，避免扁平。",
    "光影": "强化主光源方向与体积光影，避免平光或死黑。",
    "对比": "增强主体与背景的明暗对比，拉开层次。",
    "畸变": "修复透视与形体畸变，人物比例自然。",
    "糊": "提升清晰度，消除噪点、涂抹与模糊。",
    "模糊": "提升清晰度，消除模糊与运动拖影。",
    "噪": "降低噪点，提升画面纯净度。",
    "手": "修正手部结构，手指数量与关节自然。",
    "肢体": "修正肢体比例与关节衔接，避免僵硬。",
    "脸": "优化五官结构，避免脸型畸变。",
    "眼睛": "优化眼部细节，眼神聚焦。",
    "比例": "修正人物与环境比例关系。",
    "拥挤": "精简画面元素，避免过度拥挤。",
    "人物数量": "严格按分镜要求的人物数量绘制，不增减人。",
    "多余人": "移除画面中多余的人物或物体。",
    "文字": "画面中不得出现任何文字、字幕或水印。",
    "水印": "画面中不得出现任何文字、字幕或水印。",
    "字幕": "画面中不得出现任何文字、字幕或水印。",
    "武器": "严格还原参考图中的武器造型与细节。",
    "剑": "严格还原参考图中的剑/武器造型，不得变形或换款。",
    "风格": "严格采用目标风格的画风、渲染方式与配色，不得偏离为其他风格。",
    "画风": "严格采用目标风格的画风、渲染方式与配色，不得偏离为其他风格。",
}

# ⚠️ 历史缺陷：_MATCH_RULES / _CLASS_HINTS 曾长期是死代码（定义了没引用），
# 「按缺陷类别给通用修正建议」的泛化职责一直没人承担 —— 质检缺陷文案
# （「构图不平」「透视处理欠佳」）几乎不出现在新镜头的生成提示词里，
# 词面召回命中不了 → 同类问题换个镜头再犯防不住（教训库 197 条仅 14 条被召回过）。
# 现由 PromptMemory.category_hints 接回：按「近窗口高频缺陷类别」注入通用修正建议。

#: 允许注入类别建议的环节（kind 白名单）。类别建议全是**图像域**的修正话术
# （「调整构图」「修正手部结构」），audio / script 等文本域环节注入属于串味。
_CATEGORY_HINT_KINDS = {"storyboard", "asset", "keyframe", "video", "prompt"}

# 每条经验最多存活条数（防止无限膨胀）
_MAX_LESSONS = 2000
# 单条经验最长保留天数（避免过时教训长期生效）
_MAX_AGE_DAYS = 120


def _now() -> str:
    # t42：timespec 从 seconds 提升到 microseconds —— 同秒同 kind 同 phash 会派生出
    # 相同 lesson_id（主键冲突，delete/mark_used 按 lesson_id 会误删/误标多条）。
    # 微秒精度让 (kind,phash,ts) 主键几乎不可能碰撞；ISO 字符串仍是字典序=时间序，
    # since/until/cutoff 等字符串比较不受影响，旧库秒级 ts 派生 id 亦保持不变。
    return datetime.now().isoformat(timespec="microseconds")


def _norm(text: str) -> str:
    """归一化用于匹配：去空白、转小写，保留中英文与数字。"""
    return re.sub(r"[\s]+", "", (text or "")).lower()


def _split_kinds(kind: str = "", kinds: Optional[List[str]] = None) -> set:
    """把 ``kind``（逗号分隔）与 ``kinds``（列表）合并归一为去重集合。"""
    out: set = set()
    for k in (kinds or []):
        if k:
            out.add(str(k).strip())
    for k in str(kind or "").split(","):
        k = k.strip()
        if k:
            out.add(k)
    return out


def _day_end(s: str) -> str:
    """把「只到日期」的 ISO 串补成当天末，保证 ``until`` 是闭区间。"""
    s = str(s or "").strip()
    if len(s) == 10 and "T" not in s:
        return s + "T23:59:59"
    return s


# ===================== 提示词指纹与素材提取 =====================

def prompt_hash(prompt: str) -> str:
    """提示词指纹：去掉空白后的 sha1 前 12 位，用于发现「同提示词反复失败」。"""
    p = _norm(prompt)
    if not p:
        return ""
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


def extract_core_terms(text: str) -> List[str]:
    """从缺陷描述 / 提示词里尽量抽出「原文专有名词」作为匹配键补充。

    例如质检说「发冠样式与设定不符，应为白玉莲花冠」→ 抽出 [白玉莲花冠]。
    这些专有名词能显著提高「同素材复发」时的命中率。
    """
    terms: List[str] = []
    # 中文连续词（去标点）2~6 字的词组
    for seg in re.findall(r"[\u4e00-\u9fff]{2,6}", text or ""):
        if len(seg) >= 2:
            terms.append(seg)
    # 双引号/书名号内的专有名词优先
    for seg in re.findall(r"[《“\"「『]([^》”\"」』]{1,10})[》”\"」』]", text or ""):
        if seg.strip():
            terms.append(seg.strip())
    # 去重保序
    seen: set = set()
    out: List[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:60]


# ===================== 数据契约：分类 / 优先级 / 主键 / 惰性补齐 =====================

# 问题分类定义
ISSUE_CATEGORIES = {
    "structure": "结构问题",
    "logic": "逻辑问题",
    "style": "风格问题",
    "prompt_quality": "提示词质量问题",
    "feasibility": "可执行性问题",
    "visual": "视觉问题",
    "consistency": "一致性问题",
}

# 优先级定义
PRIORITY_LEVELS = {
    "critical": {"weight": 1.0, "decay_days": 180},  # 严重问题，长期保留
    "high": {"weight": 0.8, "decay_days": 120},      # 高优先级
    "medium": {"weight": 0.5, "decay_days": 60},     # 中等优先级
    "low": {"weight": 0.2, "decay_days": 30},        # 低优先级
}

# 问题分类关键词映射
_CATEGORY_KEYWORDS = {
    "structure": ["缺少字段", "格式错误", "JSON", "结构", "必填", "字段"],
    "logic": ["逻辑", "矛盾", "不一致", "引用", "未定义", "角色", "物品"],
    "style": ["风格", "不符", "不匹配", "不一致"],
    "prompt_quality": ["描述过短", "描述不详细", "缺少描述", "提示词"],
    "feasibility": ["时长", "过长", "过短", "镜头数量", "偏差"],
    "visual": ["畸变", "崩坏", "模糊", "糊化", "噪点", "色块", "撕裂"],
    "consistency": ["不一致", "变化", "差异", "不同"],
}


def categorize_issue(issue: str) -> str:
    """自动分类问题类型"""
    issue_lower = (issue or "").lower()

    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in issue_lower:
                return category

    return "visual"  # 默认分类


def assess_priority(issues: List[str], score: int = 100) -> str:
    """评估问题优先级 —— 收敛到 ai_qc_judge.assess_severity 单一来源（t42）。

    原实现自带一套简化关键词与「len(issues)>3」判据，与质检侧
    ai_qc_judge.assess_severity 口径不一致（第三处简化的严重度判据）。
    现构造等价 verdict 交给权威判据，返回 critical/high/medium/low（与 PRIORITY_LEVELS 对齐）。
    """
    from ai_qc_judge import assess_severity
    return assess_severity({"issues": list(issues or []),
                            "score": score, "critical_issues": []})


def _calculate_decay_weight(lesson: dict) -> float:
    """计算衰减权重"""
    ts_str = lesson.get("ts", "")
    if not ts_str:
        return 1.0

    try:
        ts = datetime.fromisoformat(ts_str)
        days_elapsed = (datetime.now() - ts).days

        priority = lesson.get("priority", "medium")
        decay_days = PRIORITY_LEVELS.get(priority, {}).get("decay_days", 60)

        # 线性衰减
        if days_elapsed >= decay_days:
            return 0.1  # 最低权重
        return 1.0 - (days_elapsed / decay_days) * 0.9
    except (ValueError, TypeError):
        return 1.0


def derive_lesson_id(kind: str, phash: str, ts: str) -> str:
    """确定性派生教训主键：``"L" + sha1(f"{kind}\\x1f{phash}\\x1f{ts}")[:16]``。

    为什么必须确定性：存量教训（如 71 条）**没有** `lesson_id` 字段，若用随机
    主键则无法在「不迁移文件」的前提下被删除 / 计数回写。用
    (kind, phash, ts) 三元组稳定派生后，加载时即可为旧记录补齐同一 id。
    唯一性依据：同 `kind+phash` 的旧记录本就被 `record()` 的合并逻辑
    （保留最新一条）收敛，故 `ts` 足以区分；「同秒同 kind 同 phash」会被去重
    合并成一行，不产生冲突。
    """
    raw = f"{kind or ''}\x1f{phash or ''}\x1f{ts or ''}"
    return "L" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_lesson(d: dict) -> dict:
    """纯函数：为一条教训补齐数据契约字段（惰性迁移，不写文件、不改入参）。

    - `lesson_id`：缺失时按 (kind, phash, ts) 确定性派生；
    - `use_count`：缺失或 None 时**一律补 0**（⚠️ 不按 `ts` 估算；
      `use_count` 的产品价值正是识别「记了却从未被召回的死教训」，伪造初值
      会摧毁该判据）；
    - `last_used`：缺失时补 None。

    已携带这些字段的记录原样保留（返回浅拷贝），因此不会覆盖新记录写入的
    真实 `use_count` / `last_used`。
    """
    if not isinstance(d, dict):
        return {}
    out = dict(d)
    if not out.get("lesson_id"):
        out["lesson_id"] = derive_lesson_id(
            out.get("kind") or "", out.get("phash") or "", out.get("ts") or "")
    if out.get("use_count") is None:
        out["use_count"] = 0
    if "last_used" not in out:
        out["last_used"] = None
    return out


# ===================== 原子落盘 =====================

def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试。

    ⚠️ Windows 上若目标文件正被另一个线程/进程打开读取（例如刚读完、
    句柄尚未释放），`os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这属于「暂时拿不到」而不是「文件坏了」：直接失败会让正常落盘偶发失败
    （实测并发读写时必现）。退避重试即可。与 `ai_chat._atomic_replace` /
    `project_store._atomic_replace` 同型（本模块内联实现，避免跨模块硬依赖）。
    """
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:        # WinError 5 / 32：目标被占用
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


# ===================== 记忆库主体 =====================

class PromptMemory:
    """AI 提示词记忆库（唯一实现）。

    增强能力（分类 / 优先级 / 上下文 / 衰减 / 学习曲线）已由原
    `PromptMemoryEnhanced` **整体折入**：全模块只有一个内存列表、一个 `_path`、
    一个写入者。
    """

    #: 风格类建议里的占位符：召回时用**当前项目**的风格替换
    #:
    #: 为什么用占位符而不是记录时就把风格名写死：教训是跨项目复用的，
    #: 而「严格采用 X 风格」里的 X 是**项目专属**的。若记录时就写死，
    #: A 项目（中国古风玄幻）的风格教训被 B 项目（国漫偏写实）召回时，
    #: 会把 A 的风格强行扣到 B 上 —— 这不是没帮助，是主动伤害。
    STYLE_PLACEHOLDER = "{style}"

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self._dir = os.path.join(root_dir, "lessons")
        # ★ 全模块唯一：所有实例共享同一落盘路径语义（此处每个 root_dir 一个实例，
        #   由 get_memory 保证同 root_dir 复用同一对象）。
        self._path = os.path.join(self._dir, "lessons.jsonl")
        self._lock = threading.RLock()
        os.makedirs(self._dir, exist_ok=True)
        self._lessons: list = self._load()
        self._dirty_usage = False          # t42：召回命中后 use_count 已改内存但未落盘

    # ---------- 落盘 ----------
    def _load(self) -> list:
        """读入全部教训，逐条经 `_normalize_lesson` 补齐数据契约字段。

        对 Windows 的**瞬时共享冲突**（`os.replace` 换目录项瞬间 `open()` 拿不到
        句柄）做退避重试；其余读取异常按既有容错语义只告警、读到的有效条数照旧。
        """
        out: list = []
        if not os.path.isfile(self._path):
            return out
        handle = None
        for attempt in range(6):
            try:
                handle = open(self._path, "r", encoding="utf-8")
                break
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"提示词记忆读取失败：{e}")
                return out
        if handle is None:
            logger.warning("提示词记忆读取反复被占用（已重试 6 次），跳过本次加载")
            return out
        try:
            with handle:
                for lineno, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception as e:  # noqa: BLE001
                        # t42：损坏行不再静默丢弃 —— 记 warning 并把原文备份到 .corrupt 旁路文件，
                        # 否则下次 _flush 全量重写会把这条损坏记录永久删掉。
                        logger.warning("提示词记忆第 %d 行损坏（已备份到 %s，本次跳过不丢原文）：%s",
                                       lineno, os.path.basename(self._path) + ".corrupt", e)
                        try:
                            with open(self._path + ".corrupt", "a", encoding="utf-8") as cf:
                                cf.write(line + "\n")
                        except OSError as ce:
                            logger.debug("备份损坏行失败（忽略）：%s", ce)
                        continue
                    if isinstance(obj, dict):
                        out.append(_normalize_lesson(obj))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"提示词记忆读取失败：{e}")
        return out

    def _flush(self):
        """原子覆盖写：唯一临时名 + flush + fsync + `os.replace` 退避重试。

        ⚠️ 必须原子：旧实现直接以写模式 open 目标路径（先截断目标文件再写），
        并发写者会互相截断，进程崩溃/断电会留下半截文件；固定 `.tmp` 名也会被
        并发写者互相截断。临时名每次唯一（pid + tid + 随机段）+ fsync 后 replace，
        保证读者要么看到旧全文、要么看到新全文。
        失败时清理临时文件并保持既有「记录失败只 warning 不抛」的容错语义。
        """
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"提示词记忆写入失败：{e}")
            return
        tmp = f"{self._path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for item in self._lessons:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())        # 先落盘再 replace，避免断电后只剩空文件
            _atomic_replace(tmp, self._path)
        except Exception as e:  # noqa: BLE001
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)          # 失败不留垃圾临时文件
            except OSError as e:
                logger.debug("清理临时文件失败（忽略）：%s", e)
            logger.warning(f"提示词记忆写入失败：{e}")

    # ---------- 内部：追加一条并去重/裁剪/落盘 ----------
    def _commit_lesson(self, lesson: dict) -> dict:
        """把一条教训追加进内存列表，做合并/去重/裁剪后原子落盘。

        `record()` 与 `record_with_context()` 共用此路径，保证两条写入路径
        「产出同形记录、行为等价」。
        """
        ph = lesson.get("phash") or ""
        kind = lesson.get("kind") or ""
        with self._lock:
            self._lessons.append(lesson)
            # 同提示词同缺陷的旧记录合并（保留最新一条），避免重复条目膨胀
            self._lessons = [x for x in self._lessons
                             if not (x.get("phash") == ph and x.get("kind") == kind
                                     and (x.get("ts") or "") < (lesson.get("ts") or ""))]
            # 去重：真实语义上已被保留的最新条目
            seen = set()
            dedup = []
            for x in reversed(self._lessons):
                key = (x.get("phash") or "", x.get("kind") or "", _norm(" ".join(x.get("issues") or [])))
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(x)
            self._lessons = list(reversed(dedup))
            # 上限裁剪
            if len(self._lessons) > _MAX_LESSONS:
                self._lessons = self._lessons[-_MAX_LESSONS:]
            self._flush()
        return lesson

    # ---------- 记录一条教训 ----------
    def record(self, project: str, kind: str, prompt: str,
               issues: List[str], reason: str = "",
               score: Optional[int] = None, style: str = "") -> dict:
        """质检不达标时调用：把「提示词 + 缺陷」沉淀为一条经验，供后续召回。

        `category` / `priority` 由 `categorize_issue` / `assess_priority` **自动推断**，
        与 `record_with_context()` 产出**同形记录**（都带 category/priority/context/
        decay_weight/use_count/last_used）。
        """
        ph = prompt_hash(prompt)
        if not ph:
            return {}
        defects = [str(x) for x in (issues or []) if str(x).strip()]
        if not defects and reason:
            defects = [reason]
        ts = _now()
        lesson = {
            "lesson_id": derive_lesson_id(kind, ph, ts),
            "ts": ts,
            "project": project or "",
            # 记录当时的视觉风格：召回时按「同风格加权 / 异风格降权」使用。
            # 没有它，「下次生成相同风格的提示词时是否参考历史教训」在数据层面
            # 就无从回答 —— 旧记录一律 {}，无法按风格区分，跨画风的教训会串味。
            "style": str(style or "").strip(),
            "kind": kind,
            "category": categorize_issue(defects[0] if defects else (reason or "")),
            "priority": assess_priority(defects, score if isinstance(score, int) else 100),
            "context": {},
            "phash": ph,
            "prompt": (prompt or "")[:3000],
            "issues": defects[:20],
            "reason": (reason or "")[:500],
            "score": score,
            "terms": extract_core_terms((reason or "") + " " + " ".join(defects))[:40],
            "decay_weight": 1.0,
            "use_count": 0,
            "last_used": None,
        }
        return self._commit_lesson(lesson)

    # ---------- 记录一条带上下文的教训（原增强版能力，折入） ----------
    def record_with_context(self, project: str, kind: str, category: str,
                            priority: str, context: dict, prompt: str,
                            issues: List[str], reason: str = "",
                            score: Optional[int] = None,
                            style: str = "") -> dict:
        """带上下文和分类的记录。

        与基础 `record()` 产出同形记录，区别仅在于 `category/priority/context`
        由调用方显式传入（而非自动推断）。
        """
        ph = prompt_hash(prompt)
        if not ph:
            return {}

        defects = [str(x) for x in (issues or []) if str(x).strip()]
        if not defects and reason:
            defects = [reason]

        ts = _now()
        lesson = {
            "lesson_id": derive_lesson_id(kind, ph, ts),
            "ts": ts,
            "project": project or "",
            # 与 record() 同形：context 里通常带 style，这里显式抽出来做召回加权维度
            "style": str(style or (context or {}).get("style") or "").strip(),
            "kind": kind,
            "category": category,
            "priority": priority,
            "context": context or {},
            "phash": ph,
            "prompt": (prompt or "")[:3000],
            "issues": defects[:20],
            "reason": (reason or "")[:500],
            "score": score,
            "terms": extract_core_terms((reason or "") + " " + " ".join(defects))[:40],
            "decay_weight": 1.0,
            "use_count": 0,
            "last_used": None,
        }
        return self._commit_lesson(lesson)

    # ---------- 召回修正建议 ----------
    def suggestions(self, kind: str, prompt: str,
                    project: str = "", max_hints: int = 3,
                    style: str = "") -> List[str]:
        """生成提示词前调用：按 kind + 提示词相似度 + 项目归属，召回修正建议。

        打分构成：
        - ``sim``：提示词指纹完全命中 1.0；否则按文本相似度近似；
        - ``kw_hit``：该教训的缺陷关键词是否出现在**当前查询提示词**里
          （⚠️ 历史缺陷：原先拿教训自己的 prompt 去匹配自己的 terms，恒等命中，
          贡献了一个与相关性无关的常数分，等于把召回变成了随机取样）；
        - ``proj_bonus``：同项目教训加权，跨项目教训降权（跨项目经验仍有参考价值，
          但同类名/同场景的教训显然更对症）。
        """
        ph = prompt_hash(prompt)
        qp = _norm(prompt or "")
        # t42：遍历前持锁取快照，避免与 _commit_lesson 的整表替换交错读到不一致列表
        with self._lock:
            lessons = list(self._lessons)
        scored: List[tuple] = []
        for l in lessons:
            if l.get("kind") and l.get("kind") != kind:
                continue
            # 空教训（无缺陷也无结论）不参与召回 —— 历史脏数据里有整批这种记录
            if not (l.get("issues") or l.get("reason")):
                continue
            # 1) 提示词指纹命中（同句提示词曾经失败）——最强信号
            sim = 0.0
            if ph and l.get("phash") == ph:
                sim = 1.0
            # 2) 提示词文本相似（编辑距离近似）
            elif l.get("prompt"):
                sim = self._similar(l.get("prompt"), prompt)
            # 3) 缺陷关键词命中（当前提示词里有「曾经出问题的词」）
            kw_hit = 0.0
            for t in (l.get("terms") or []):
                if t and qp and t in qp:
                    kw_hit += 1.0
            # 4) 项目归属加权/降权
            lp = str(l.get("project") or "").strip()
            if lp and project:
                proj_bonus = 0.15 if lp == project else -0.10
            else:
                proj_bonus = 0.0
            # 5) 风格归属加权/降权：同一视觉风格的教训才最对症。
            #    「画面偏写实」「配色偏灰」这类缺陷换了画风后并不成立，
            #    跨风格照搬等于用 A 画风的标准去要求 B 画风的图。
            ls = str(l.get("style") or "").strip()
            if ls and style:
                style_bonus = 0.15 if ls == style else -0.15
            else:
                style_bonus = 0.0
            total = sim + min(kw_hit, 3.0) * 0.25 + proj_bonus + style_bonus
            if total > 0.2:
                scored.append((total, l))
        scored.sort(key=lambda x: x[0], reverse=True)
        hints, seen, used_ids = [], set(), set()
        for _, l in scored:
            for iss in (l.get("issues") or []):
                # 景别冲突防护：给「远景」镜头注入「要求为特写」= 主动把图画错，
                # 比不给建议更糟。只对明确断言了景别的 issue 生效。
                if self._framing_conflict(iss, prompt):
                    continue
                text = self._render_hint(iss, style)
                if not text:
                    continue          # 需要风格但本次没给风格 → 丢弃这条，不要误导
                key = _norm(text)
                if key and key not in seen:
                    seen.add(key)
                    hints.append(text)
                    # 计数口径（§2.4.1）：只有「真正被渲染进 hints」的教训才算被召回。
                    # 记下该条 lesson_id，函数返回前统一回写 use_count / last_used。
                    lid = l.get("lesson_id")
                    if lid:
                        used_ids.add(lid)
                if len(hints) >= max_hints:
                    break
            if len(hints) >= max_hints:
                break
        if used_ids:
            self.mark_used(list(used_ids))
        return hints

    # ---------- 带优先级和上下文的召回（原增强版能力，折入） ----------
    def suggestions_with_priority(self, kind: str, prompt: str,
                                  context: dict = None, max_hints: int = 5) -> List[dict]:
        """带优先级和上下文的召回"""
        ph = prompt_hash(prompt)
        # t42：遍历前持锁取快照，避免与 _commit_lesson 的整表替换交错读到不一致列表
        with self._lock:
            lessons = list(self._lessons)
        scored: List[tuple] = []

        for l in lessons:
            if l.get("kind") and l.get("kind") != kind:
                continue

            # 计算衰减权重
            decay_weight = _calculate_decay_weight(l)
            if decay_weight < 0.1:
                continue  # 跳过严重衰减的记忆

            # 1) 提示词指纹命中
            sim = 0.0
            if ph and l.get("phash") == ph:
                sim = 1.0
            # 2) 提示词文本相似
            elif l.get("prompt"):
                sim = self._similar(l.get("prompt"), prompt)

            # 3) 缺陷关键词命中（必须匹配**当前查询提示词**）
            #    ⚠️ 历史缺陷：这里曾拿教训自己的 prompt 去匹配自己的 terms，恒等命中，
            #    给每条教训都加了一个与相关性无关的常数分 —— 召回退化成随机取样。
            kw_hit = 0.0
            qp = _norm(prompt or "")
            for t in (l.get("terms") or []):
                if t and qp and t in qp:
                    kw_hit += 1.0

            # 4) 上下文匹配
            context_match = 0.0
            if context and l.get("context"):
                for key, value in context.items():
                    if l["context"].get(key) == value:
                        context_match += 0.2

            # 5) 优先级权重
            priority_weight = PRIORITY_LEVELS.get(l.get("priority", "medium"), {}).get("weight", 0.5)

            total = (sim + min(kw_hit, 3.0) * 0.25 + context_match) * decay_weight * priority_weight

            if total > 0.1:
                scored.append((total, l))

        scored.sort(key=lambda x: x[0], reverse=True)

        hints = []
        seen = set()
        for _, l in scored:
            for iss in (l.get("issues") or []):
                key = _norm(iss)
                if key and key not in seen:
                    seen.add(key)
                    hints.append({
                        "issue": iss,
                        "category": l.get("category", "visual"),
                        "priority": l.get("priority", "medium"),
                        "project": l.get("project", ""),
                        "score": l.get("score"),
                    })
                if len(hints) >= max_hints:
                    break
            if len(hints) >= max_hints:
                break

        return hints

    @classmethod
    def _render_hint(cls, issue, style: str = "") -> str:
        """把一条建议渲染成可用文本：替换 ``{style}`` 占位符

        - 无占位符 → 原样返回；
        - 有占位符且本次给了 style → 替换成当前项目的风格；
        - 有占位符但没给 style → 返回空串（丢弃该建议）。宁可少给一条，
          也不能把别的项目的风格名安到当前项目上。
        """
        text = str(issue or "").strip()
        if not text:
            return ""
        if cls.STYLE_PLACEHOLDER not in text:
            return text
        s = str(style or "").strip()
        if not s:
            return ""
        return text.replace(cls.STYLE_PLACEHOLDER, s)

    # 分镜/资产提示词是**同一套模板**拼出来的，「根据参考图生成漫剧分镜画面…景别（必须
    # 严格遵守）…场景：…动作与画面内容：…」这套套话在每条提示词里都出现。直接用它算
    # 相似度 → 每条教训都能命中大量 4-gram → 全部封顶 → 相似度失去区分度。
    # 先剥离这些套话，再比较**真正有区分度的部分**（角色 / 场景 / 动作 / 景别措辞）。
    #
    # ⚠️ 2026-09-25：图片链路切到 QwenImage2.1 后，分镜提示词骨架换成了官方 <imageN>
    # 英文协议（TASK / PRIMARY CANVAS / IDENTITY / REFERENCE ROLES / PRESERVE…）。
    # 旧中文套话已不再出现，若只留旧条目 → 新提示词**一句都剥离不掉** → 相似度重新
    # 退化回「全部封顶 0.95」的历史缺陷（会给远景镜头召到要求特写的教训）。
    # 因此新旧两套套话都登记（存量教训仍按旧骨架存库，新教训按新骨架存库）。
    _BOILERPLATE = (
        # ---- 新协议（Qwen-Image-2.1 <imageN>，英文骨架）----
        "TASK:", "Generate a single storyboard frame",
        "FRAMING (must be strictly followed)", "FRAMING (not specified",
        "CAMERA ANGLE (must be strictly followed)",
        # ---- 分镜光学/材质段（2026-09-28 画质提升）----
        # ⚠️ 必须登记**正文**（不只是段标签）：同批的 COMPOSITION BASELINE 也是登记
        #    多个正文片段。只登记 "CINEMATOGRAPHY:" 时 479 字符正文原样残留 ——
        #    两条不同分镜提示词的 Dice 相似度被同向抬高 +0.1289（0.7298→0.8587），
        #    召回失去区分度。
        # ⚠️ 本组必须与 comfyui_client.CINEMATOGRAPHY_SECTION 保持同步：改一段要改两处；
        #    漂移会让相似度剥离退化（历史缺陷）。
        #    守卫 .workbuddy/test/verify_aspect_default_16x9.py 断言剥离残留 < 全长 10%。
        "CINEMATOGRAPHY:",
        "cinematic depth of field with a shallow focal plane and soft bokeh in the out-of-focus background",
        "natural skin subsurface scattering with visible fine pore and fabric-level texture",
        "individual hair strands catching the key light",
        "cloth weave and material grain clearly readable",
        "volumetric light shafts and drifting atmospheric haze",
        "physically plausible shadow falloff with soft contact shadows",
        "restrained film grain",
        "clean highlight roll-off without clipping",
        "PRIMARY CANVAS:", "Use <image1> as the primary canvas",
        "and identity anchor", "No reference image is provided for this shot",
        "Generate the frame purely from the SCENE AND ACTION and STYLE descriptions below",
        "IDENTITY:", "Preserve the exact identity from",
        "Keep the original facial structure, hairstyle and body proportions of",
        "do NOT redraw or re-describe the face",
        "REFERENCE ROLES:", "only for", "Do not merge or transfer attributes",
        "Each referenced character must keep its own individual identity",
        # ---- 3D 导演台构图基准图段（2026-09-27）----
        # ⚠️ 必须登记：否则「带基准图」与「不带基准图」的同一镜指纹不同，
        #    教训召回会互相认不出（历史同类问题见上方 2026-09-25 注释）。
        "COMPOSITION BASELINE:", "is a 3D blocking reference",
        "Copy ONLY its composition", "Replace those mannequins with the characters",
        "Do NOT copy the mannequin bodies",
        "It defines the framing and the spatial layout of the frame",
        "3D导演台构图基准",
        "SCENE AND ACTION:", "Location:", "Action and content:",
        "Speaking state:", "is quietly saying one short line",
        "shown only as natural lip movement and subtle expression changes",
        "Emotion and mood:", "LIGHTING:", "STYLE:", "PRESERVE:",
        "Keep all untargeted content unchanged",
        "Keep the framing and camera angle exactly as specified in",
        "Keep the identity of every referenced character unchanged",
        "Keep the shape, material and colour of every referenced prop unchanged",
        "Keep the environment and atmosphere consistent with the scene reference",
        "must not contain any text", "The image must not contain any text",
        # ---- 存量：旧中文分节协议 ----
        "根据参考图生成漫剧分镜画面", "参考图用途", "参考图1是角色", "参考图2是角色",
        "参考图3是场景", "的外貌、服装与发型", "的外貌与服装", "的环境与氛围",
        "镜头（必须严格遵守）", "景别（必须严格遵守）", "动作与画面内容",
        "画面要求", "人物造型一致", "无畸变", "画面清晰",
        # ---- 参考图职责套话（新旧共用的中文表述）----
        "的身份锚点", "的面部身份", "保持其面部身份、发型与体型不变",
        "独立保持其面部身份与发型，不得与其他角色特征混用",
        "的环境与氛围锚点", "的结构与氛围保持一致",
        # 2026-09-29：场景槽位话术升级（地理优先规格）后的新句式。与上面两条
        # 旧串**并存**——存量教训库里的历史提示词仍含旧串，剥离时两条都要命中。
        "的空间与光照锚点", "保持空间结构、地理关系",
        "与光源方向、色温基调一致", "不得照搬该参考图的机位与取景范围",
        # 2026-09-29（场景按机位取图）：场景槽位话术再次调整 —— 参考图**已按本镜机位
        # 选档**，故豁免从「不得照搬机位」收紧为「取景范围以镜头为准、不得照搬构图范围」。
        # 与上面那条旧串**并存**：存量教训库里的历史提示词仍含旧句式。
        "取景范围以本镜镜头描述为准", "不得照搬该参考图的构图范围",
        "的形状、材质与配色锚点",
    )

    @classmethod
    def _strip_boilerplate(cls, s: str) -> str:
        t = str(s or "")
        for b in cls._BOILERPLATE:
            t = t.replace(b, " ")
        return _norm(t) or _norm(str(s or ""))

    @classmethod
    def _similar(cls, a: str, b: str) -> float:
        """字符级相似度（4-gram Dice 系数，长度归一），阈值内近似。

        ⚠️ 历史缺陷：旧实现是「短串 4-gram 在长串里的命中数 / 8」，
        **只数命中数、不做长度归一、也没有剥离模板套话**。实测对同一个查询，
        库里 60 条 storyboard 教训的相似度**全部恒为 0.950** —— 因为查询（~150 字）
        的 4-gram 几乎都能在教训（~600 字）里找到，命中数远超 8 直接封顶。
        后果：sim 退化成一个常数，phash 精确命中（1.0）与毫不相干（0.95）只差 0.05，
        召回排序实际由 kw_hit / proj_bonus 决定 ≈ **随机取样** —— 会给「远景」镜头
        召到「要求为特写」的教训，属于主动伤害。

        现在改为：先剥离模板套话，再用 Dice 系数 ``2*hit/(len_a+len_b)`` ——
        长度差异会被惩罚，不同教训之间重新拉开区分度。
        """
        a = cls._strip_boilerplate(a)
        b = cls._strip_boilerplate(b)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if len(short) < 4:
            return 1.0 if short in long_ else 0.0
        hit = 0
        for i in range(0, len(short) - 3, 1):
            if short[i:i + 4] in long_:
                hit += 1
        return min(2.0 * hit / (len(a) + len(b)), 0.95)

    # ---------- 高频类别预防建议（类别级泛化召回） ----------
    #: 近窗口内同类别教训 >= 此数才注入该类别的预防建议。
    #: 太小（=1）会把偶发缺陷当系统性问题，每个提示词都被塞满建议；太大对冷启动不敏感。
    CATEGORY_HINT_MIN = 3
    #: 统计「高频类别」的时间窗口（天）：只看近期教训，久远问题由衰减机制自然退出。
    CATEGORY_HINT_RECENT_DAYS = 30
    #: 单次最多注入的类别建议条数（控制提示词膨胀）。
    CATEGORY_HINT_MAX = 3

    @staticmethod
    def _rule_hits(text: str) -> List[str]:
        """缺陷文本命中的规则关键词（按 ``_MATCH_RULES`` 优先级收集全部命中）。"""
        t = _norm(text)
        if not t:
            return []
        return [r for r in _MATCH_RULES if r in t]

    def category_hints(self, kind: str, max_hints: Optional[int] = None) -> List[str]:
        """按「近窗口高频缺陷类别」召回**类别级**预防建议（跨镜头泛化）。

        与 `suggestions()`（按提示词相似度召回**具体**教训）互补：具体教训只对
        同/相似提示词有效；这里统计近期教训的缺陷类别频次，命中阈值的高频类别
        直接注入 ``_CLASS_HINTS`` 的通用修正建议 —— 相当于把「高频缺陷的修正」
        **动态固化**进每次生成的提示词，且随教训库自动更新：哪类问题修好了、
        近期频次掉下去，对应建议自动消失，不需要手工改提示词骨架。

        只统计 ``kind`` 同环节的教训（跨项目累计——构图/透视这类是模型级系统性
        问题，不局限单一项目）；建议文本为通用话术，不含风格名，跨项目/跨风格
        注入无「串味」风险。
        """
        cap = self.CATEGORY_HINT_MAX if max_hints is None else max(0, int(max_hints))
        if cap <= 0:
            return []
        cutoff = (datetime.now() - timedelta(days=self.CATEGORY_HINT_RECENT_DAYS)
                  ).isoformat(timespec="seconds")
        with self._lock:
            lessons = list(self._lessons)
        counts: dict = {}
        for l in lessons:
            if l.get("kind") and l.get("kind") != kind:
                continue
            if not (l.get("issues") or l.get("reason")):
                continue      # 空教训不参与统计
            ts = str(l.get("ts") or "")
            if ts and ts < cutoff:
                continue      # 只看近期：过期类别由衰减机制负责
            text = " ".join(str(x) for x in (l.get("issues") or [])) + " " + str(l.get("reason") or "")
            for cat in set(self._rule_hits(text)):
                if cat in _CLASS_HINTS:
                    counts[cat] = counts.get(cat, 0) + 1
        hints: List[str] = []
        for cat, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            if n < self.CATEGORY_HINT_MIN:
                break         # 频次降序 → 后面只会更少
            hints.append(_CLASS_HINTS[cat])
            if len(hints) >= cap:
                break
        return hints

    # 景别词（长词在前，先匹配长词避免「远景」被「大远景」截走）。
    # ⚠️ 取值必须 ⊆ config.SHOT_TYPES（景别唯一权威表）：漏掉某个景别 → 质检教训里
    #    该景别的「要求 X 实际 Y」句式识别不出来，教训召回失效（守卫 E3 会断言 ⊆）。
    _FRAMINGS = ("大远景", "大特写", "中近景", "局部", "远景", "全景", "中景", "近景", "特写")

    # 「目标景别」引导词：质检缺陷的经典句式是「要求 X，实际画成 Y」
    # —— 必须取**目标**（X）那一侧的景别，取到「实际」那侧会把最对症的教训误杀。
    # 例：issue「镜头要求特写，实际为腰部以上中景」对查询「…特写。」是**对症**的，
    #     若按「文本里第一个景别词」取到「中景」就会判成冲突、过滤掉（历史踩坑）。
    _FRAMING_TARGET_RE = None  # 延迟编译（模块顶部未 import re 时不炸）

    @classmethod
    def _framing_of(cls, text: str) -> str:
        """抽取文本里**断言的目标景别**（用于判断教训与当前镜头是否同类型）。"""
        import re

        t = str(text or "")
        if not t:
            return ""
        if cls._FRAMING_TARGET_RE is None:
            cls._FRAMING_TARGET_RE = re.compile(
                r"(?:要求|应为|目标|必须|需要|严格)[^，。；]{0,10}?"
                r"(大远景|大特写|中近景|局部|远景|全景|中景|近景|特写)")
        m = cls._FRAMING_TARGET_RE.search(t)
        if m:
            return m.group(1)
        # 没有引导词 → 退回「第一个出现的景别词」
        for f in cls._FRAMINGS:
            if f in t:
                return f
        return ""

    @classmethod
    def _framing_conflict(cls, issue: str, prompt: str) -> bool:
        """教训里断言的景别与当前提示词要求的景别**明确冲突** → 不能采用。

        这是「主动伤害」防护：给一个远景镜头注入「要求为特写」的建议，
        会让模型把远景画成特写，比不给建议更糟。
        只对**明确提到景别**的 issue 生效；没提景别的（构图/清晰度/风格类）一律放行。
        """
        want = cls._framing_of(prompt)
        got = cls._framing_of(issue)
        if not want or not got:
            return False
        return want != got

    # ---------- 拼装带经验的提示词 ----------
    def learned_prompt(self, kind: str, prompt: str,
                       project: str = "", max_hints: int = 3,
                       style: str = "") -> str:
        """在原始提示词后追加历史修正建议；无建议则原样返回。

        style：本次生成的目标风格。仅用于替换建议里的 ``{style}`` 占位符，
        保证「严格采用 X 风格」里的 X 永远是**当前项目**的风格。

        除历史教训外，还会叠加 ``category_hints``（高频缺陷类别的预防建议，
        仅限 ``_CATEGORY_HINT_KINDS`` 图像域环节）：历史教训按提示词相似度
        召回、只对同/相似提示词有效；类别建议按频次触发、跨镜头泛化，
        两块去重后分开标注来源。
        """
        hints = self.suggestions(kind, prompt, project, max_hints, style=style)
        cat_hints: List[str] = []
        if kind in _CATEGORY_HINT_KINDS:
            try:
                cat_hints = self.category_hints(kind)
            except Exception as e:  # noqa: BLE001 - 类别建议失败不影响历史召回
                logger.warning("高频类别预防建议生成失败（忽略）：%s", e)
        # 与历史建议去重（同一句话既当过具体缺陷又命中间类别时只保留一处）
        seen_norms = {_norm(h) for h in hints if (h or "").strip()}
        cat_hints = [h for h in cat_hints
                     if (h or "").strip() and _norm(h) not in seen_norms]
        safe = [(h or "").strip() for h in hints if (h or "").strip()]
        catsafe = [(h or "").strip() for h in cat_hints if (h or "").strip()]
        if not safe and not catsafe:
            return prompt
        suffix = ""
        if safe:
            suffix += "\n【历史质检修正建议（请务必遵守）】\n" + "\n".join(f"  {i+1}. {h}" for i, h in enumerate(safe))
        if catsafe:
            suffix += "\n【高频缺陷预防建议（请务必遵守）】\n" + "\n".join(f"  {i+1}. {h}" for i, h in enumerate(catsafe))
        return prompt + suffix

    # ---------- 衰减过时记忆（原增强版能力，折入） ----------
    def decay_old_lessons(self, days_threshold: int = 30) -> int:
        """衰减过时记忆"""
        decayed_count = 0

        with self._lock:
            for lesson in self._lessons:
                old_weight = lesson.get("decay_weight", 1.0)
                new_weight = _calculate_decay_weight(lesson)

                if new_weight < old_weight:
                    lesson["decay_weight"] = new_weight
                    decayed_count += 1

            if decayed_count > 0:
                self._flush()

        return decayed_count

    # ---------- 学习曲线统计（原增强版能力，折入） ----------
    def get_learning_curve(self, project: str = None) -> dict:
        """获取学习曲线统计"""
        with self._lock:
            filtered = self._lessons
            if project:
                filtered = [l for l in filtered if l.get("project") == project]

            if not filtered:
                return {"total": 0, "by_category": {}, "by_priority": {}, "trend": []}

            # 按类别统计
            by_category = {}
            for l in filtered:
                cat = l.get("category", "unknown")
                by_category[cat] = by_category.get(cat, 0) + 1

            # 按优先级统计
            by_priority = {}
            for l in filtered:
                pri = l.get("priority", "medium")
                by_priority[pri] = by_priority.get(pri, 0) + 1

            # 按时间趋势（最近30天）
            trend = []
            now = datetime.now()
            for i in range(30):
                date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                count = sum(1 for l in filtered if l.get("ts", "").startswith(date))
                trend.append({"date": date, "count": count})

            trend.reverse()

            return {
                "total": len(filtered),
                "by_category": by_category,
                "by_priority": by_priority,
                "trend": trend,
                "avg_score": sum(l.get("score", 0) or 0 for l in filtered) / max(len(filtered), 1),
            }

    # ---------- 统计与检索 ----------
    def stats(self) -> dict:
        with self._lock:
            by_kind: dict = {}
            dead = 0
            used_total = 0
            for l in self._lessons:
                k = l.get("kind") or "?"
                by_kind[k] = by_kind.get(k, 0) + 1
                if (l.get("use_count") or 0) == 0:
                    dead += 1
                used_total += int(l.get("use_count") or 0)
            return {
                "total": len(self._lessons),
                "by_kind": by_kind,
                "dead_lessons": dead,
                "used_total": used_total,
                "path": self._path,
            }

    def dead_count(self) -> int:
        """`use_count == 0` 的教训条数（「记了却从未被召回」的死教训）。"""
        with self._lock:
            return sum(1 for l in self._lessons if (l.get("use_count") or 0) == 0)

    def list(self, kind: str = "", limit: int = 50) -> list:
        """返回最新在前的教训列表（**向后兼容**：旧调用 `list(kind, limit)` 仍返回 list）。

        多维筛选 / 分页 / 统计请用 ``query()``（返回富结构 dict）。
        """
        with self._lock:
            items = [dict(l) for l in self._lessons
                     if (not kind or l.get("kind") == kind)]
            return items[-limit:][::-1]         # 最新在前

    def query(self, kind: str = "", kinds: Optional[List[str]] = None,
              project: str = "", since: str = "", until: str = "",
              q: str = "", limit: int = 50, offset: int = 0) -> dict:
        """多维筛选 + 关键词检索 + 分页（供教训管理页 / API 使用）。

        返回富结构：:

            {"items": [...],        # 最新在前，已分页
             "total": int,          # 未过滤前条数
             "filtered": int,       # 应用全部过滤后条数（分页前）
             "by_kind": {kind: n},  # 过滤后口径
             "dead_lessons": int,   # 过滤后口径里 use_count==0 的条数}

        ``kind`` 支持逗号分隔多值（如 ``"asset,storyboard"``），``kinds`` 列表同样支持。
        ``since`` / ``until`` 按 ``ts`` 做 ISO 字符串比较；``until`` 只给到日期时按
        「当天末」闭区间处理。``q`` 命中 ``issues ∪ terms ∪ reason ∪ prompt`` 任一即命中。
        """
        want = _split_kinds(kind, kinds)
        qn = _norm(q or "")
        until_s = _day_end(until)
        with self._lock:
            items = [dict(l) for l in self._lessons]
        total = len(items)
        if want:
            items = [l for l in items if (l.get("kind") or "") in want]
        if project:
            items = [l for l in items if (l.get("project") or "") == project]
        if since:
            items = [l for l in items if str(l.get("ts") or "") >= str(since)]
        if until_s:
            items = [l for l in items if str(l.get("ts") or "") <= until_s]
        if qn:
            def _hit(l: dict) -> bool:
                for key in ("issues", "terms"):
                    if qn in _norm(" ".join(str(x) for x in (l.get(key) or []))):
                        return True
                if qn in _norm(str(l.get("reason") or "")):
                    return True
                if qn in _norm(str(l.get("prompt") or "")):
                    return True
                return False
            items = [l for l in items if _hit(l)]
        filtered = len(items)
        by_kind: dict = {}
        dead = 0
        for l in items:
            k = l.get("kind") or "?"
            by_kind[k] = by_kind.get(k, 0) + 1
            if (l.get("use_count") or 0) == 0:
                dead += 1
        # 最新在前（追加顺序：尾部最新）
        items = items[::-1]
        if limit and limit > 0:
            page = items[offset:offset + limit]
        else:
            page = items[offset:]
        return {"items": page, "total": total, "filtered": filtered,
                "by_kind": by_kind, "dead_lessons": dead}

    # ---------- 带过滤条件的列表（原增强版能力，折入） ----------
    def list_with_context(self, kind: str = "", category: str = "",
                          priority: str = "", limit: int = 50) -> list:
        """带过滤条件的列表"""
        with self._lock:
            items = self._lessons

            if kind:
                items = [l for l in items if l.get("kind") == kind]
            if category:
                items = [l for l in items if l.get("category") == category]
            if priority:
                items = [l for l in items if l.get("priority") == priority]

            return items[-limit:][::-1]  # 最新在前

    def clear(self, kind: str = "") -> int:
        with self._lock:
            before = len(self._lessons)
            if kind:
                self._lessons = [l for l in self._lessons if l.get("kind") != kind]
            else:
                self._lessons = []
            self._flush()
            return before - len(self._lessons)

    def delete_by_kind(self, kind: str) -> int:
        """按 kind 删除（复用 ``clear(kind)``，语义一致）。"""
        return self.clear(kind)

    def delete(self, lesson_id: str) -> bool:
        """按 ``lesson_id`` 删除单条并原子落盘；未命中返回 False。"""
        lid = str(lesson_id or "").strip()
        if not lid:
            return False
        with self._lock:
            before = len(self._lessons)
            self._lessons = [l for l in self._lessons
                             if (l.get("lesson_id") or "") != lid]
            removed = before - len(self._lessons)
            if removed:
                self._flush()
            return removed > 0

    def mark_used(self, lesson_ids: List[str]) -> int:
        """命中回写：对每个命中的 ``lesson_id`` 做 ``use_count += 1``、
        ``last_used = now``，全部命中后**原子落盘**。返回实际命中条数（去重后）。
        """
        ids = {str(x).strip() for x in (lesson_ids or []) if str(x).strip()}
        if not ids:
            return 0
        now = _now()
        hit = 0
        with self._lock:
            for l in self._lessons:
                if (l.get("lesson_id") or "") in ids:
                    l["use_count"] = int(l.get("use_count") or 0) + 1
                    l["last_used"] = now
                    hit += 1
            # t42：召回命中只改内存，不立即全量写盘（每次生成提示词都 flush 整库是性能
            # 问题）。use_count 的持久化顺延到下一次写操作或显式 flush()。
            if hit:
                self._dirty_usage = True
        return hit

    def flush(self) -> None:
        """显式落盘（含召回累积的 use_count / last_used 脏数据）。"""
        with self._lock:
            self._flush()

    def prune_empty(self) -> int:
        """清掉「无缺陷也无结论」的空教训，返回清理条数。

        为什么需要：历史缺陷导致某段时间写入的教训全是
        ``issues=[] / reason="" / score=0`` 的空壳（写入端曾误读 ``rec["verdict"]``）。
        这些记录既不能提供建议，又会在 stats 里冒充"学到了 36 条经验"，
        还会拖慢召回。写入端已加守卫不再产生新的空记录，这里负责清历史遗留。
        """
        with self._lock:
            before = len(self._lessons)
            self._lessons = [l for l in self._lessons
                             if (l.get("issues") or l.get("reason"))]
            removed = before - len(self._lessons)
            if removed:
                self._flush()
            return removed

    # ---------- 数据契约派生：主键 / 惰性补齐（类级入口，供 instance 调用） ----------
    #: 确定性派生教训主键（与模块级 `derive_lesson_id` 同一实现）
    lesson_id = staticmethod(derive_lesson_id)
    #: 惰性补齐数据契约字段（与模块级 `_normalize_lesson` 同一实现）
    _normalize_lesson = staticmethod(_normalize_lesson)


# ===================== 已废弃：增强版别名 =====================

# t56：原为空壳子类（isinstance 对 get_enhanced_memory 的返回值恒 False，因后者返回
# 父类 PromptMemory 实例 —— 「isinstance 兼容」形似实则无效）。改为类别名，
# isinstance(x, PromptMemoryEnhanced) 与 isinstance(x, PromptMemory) 完全等价，兼容真实。
PromptMemoryEnhanced = PromptMemory


# ===================== 模块级单例 =====================

_INSTANCE: Optional[PromptMemory] = None
_INST_LOCK = threading.Lock()


def get_memory(root_dir: str) -> PromptMemory:
    """返回 `root_dir` 对应的唯一 `PromptMemory` 实例（全模块唯一写入者）。"""
    global _INSTANCE
    with _INST_LOCK:
        if _INSTANCE is None or os.path.abspath(_INSTANCE.root_dir) != os.path.abspath(root_dir):
            _INSTANCE = PromptMemory(root_dir)
        return _INSTANCE


def get_enhanced_memory(root_dir: str) -> PromptMemory:
    """**同一对象**：增强版单例已删除，直接返回 `get_memory(root_dir)`。

    保留该函数名只为向后兼容既有调用点；返回值与 `get_memory` 完全一致
    （`get_memory(root) is get_enhanced_memory(root)` 恒成立）。
    """
    return get_memory(root_dir)


def record(project: str, kind: str, prompt: str, issues: List[str],
           reason: str = "", score: Optional[int] = None, root_dir: str = "",
           style: str = "") -> dict:
    """关键便捷入口：由生成链路内部直接调用，记录一条经验。

    ``style``：记录当时的视觉风格，供召回时「同风格加权 / 异风格降权」。
    """
    if not root_dir:
        return {}
    return get_memory(root_dir).record(project, kind, prompt, issues, reason, score,
                                       style=style)


def record_with_context(project: str, kind: str, category: str,
                        priority: str, context: dict, prompt: str,
                        issues: List[str], reason: str = "",
                        score: Optional[int] = None, root_dir: str = "",
                        style: str = "") -> dict:
    """增强版记录入口（统一后落到底层唯一实例）。"""
    if not root_dir:
        return {}
    return get_enhanced_memory(root_dir).record_with_context(
        project, kind, category, priority, context, prompt, issues, reason, score,
        style=style
    )


def suggest(kind: str, prompt: str, project: str = "", root_dir: str = "",
            style: str = "") -> List[str]:
    if not root_dir:
        return []
    return get_memory(root_dir).suggestions(kind, prompt, project, style=style)


def suggest_with_priority(kind: str, prompt: str, context: dict = None,
                          max_hints: int = 5, root_dir: str = "") -> List[dict]:
    """增强版召回入口（统一后落到底层唯一实例）。"""
    if not root_dir:
        return []
    return get_enhanced_memory(root_dir).suggestions_with_priority(
        kind, prompt, context, max_hints
    )


def learned_prompt(kind: str, prompt: str, project: str = "",
                   root_dir: str = "", max_hints: int = 3,
                   style: str = "") -> str:
    """关键便捷入口：返回叠加了「历史质检修正建议」的提示词；无建议则原样返回。

    ⚠️ 必须放在模块级：生成链路是以 `prompt_memory.learned_prompt(...)` 调用的。
    此前只有类方法、没有模块级函数，调用处抛 AttributeError 又被 except 静默吞掉，
    导致「质检不通过 → 改提示词重生成」这条链路**从来没有真正生效**过。
    """
    if not root_dir:
        return prompt
    return get_memory(root_dir).learned_prompt(kind, prompt, project, max_hints, style=style)


def get_learning_curve(project: str = None, root_dir: str = "") -> dict:
    """获取学习曲线（统一后落到底层唯一实例）。"""
    if not root_dir:
        return {"total": 0, "by_category": {}, "by_priority": {}, "trend": []}
    return get_enhanced_memory(root_dir).get_learning_curve(project)


def decay_lessons(root_dir: str = "") -> int:
    """衰减过时记忆（统一后落到底层唯一实例）。"""
    if not root_dir:
        return 0
    return get_enhanced_memory(root_dir).decay_old_lessons()


def mark_used(lesson_ids: List[str], root_dir: str = "") -> int:
    """模块级：命中回写 use_count / last_used（供生成链路或外部调用）。"""
    if not root_dir:
        return 0
    return get_memory(root_dir).mark_used(lesson_ids)


def delete_lesson(lesson_id: str, root_dir: str = "") -> bool:
    """模块级：按 lesson_id 删除单条教训。"""
    if not root_dir:
        return False
    return get_memory(root_dir).delete(lesson_id)


def clear_lessons(kind: str = "", root_dir: str = "") -> int:
    """模块级：清空某一环节（kind 为空则清空全部）。"""
    if not root_dir:
        return 0
    return get_memory(root_dir).clear(kind)


def query_lessons(kind: str = "", kinds: Optional[List[str]] = None,
                  project: str = "", since: str = "", until: str = "",
                  q: str = "", limit: int = 50, offset: int = 0,
                  root_dir: str = "") -> dict:
    """模块级：多维筛选 + 关键词检索 + 分页（返回富结构 dict）。"""
    if not root_dir:
        return {"items": [], "total": 0, "filtered": 0, "by_kind": {}, "dead_lessons": 0}
    return get_memory(root_dir).query(kind=kind, kinds=kinds, project=project,
                                      since=since, until=until, q=q,
                                      limit=limit, offset=offset)
