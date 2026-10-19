# -*- coding: utf-8 -*-
"""生成前提示词 LLM 增强 + 质检模型语义复审（2026-09-30 用户需求）

## 定位（与 prompt_qc 的分工）

``prompt_qc.preflight`` 是**确定性层**（零模型、毫秒级、可自愈）——它的模块文档明确
「这一层不做 LLM 审阅」。本模块补上模型侧的两步，由 preflight 在合适的时机调用，
因此**自动覆盖所有已接入预检的生成路径**（分镜图 / 资产图 / 尾帧 / H3 视频）：

  ① LLM 增强（text 模块）：出图/出片**之前**把画面描述改写得更具体（动作先后、
     空间关系、光影氛围），协议骨架（段名 / <imageN> / <Picture N> / 时间码）逐字保留；
  ② 质检模型复审（qc 模块）：对**最终**提示词做语义审阅（主体是否清晰、与镜头上下文
     是否一致、是否有会導致画面崩坏的描述），复审不通过且给出改进版时，改进版必须先
     通过确定性复检且分数不降才被采纳。

## 安全边界（逐条都是硬约束）

- **绝不阻断生成**：两步任何异常（未配置 / 超时 / 输出不合法）一律 fail-open，
  按原提示词继续——生成链路可以没有这一层，但不能因为这一层挂掉；
- **台词永不改写**：kind="audio" 不参与增强与复审（台词会被 TTS 逐字念出）；
- **骨架先于采纳**：增强/改进结果必须先过骨架校验（段名齐全、<imageN>/<Picture N>
  集合不变、H3 结构完整、长度上限、不骤缩），不过就丢弃并用原提示词；
- **同一提示词只打一次模型**：进程内缓存（生成重试/质检重试会重复预检同一条提示词）；
  失败结果缓存 10 分钟，避免上游故障时每镜都空等一次超时。
- **失败要安静且快速**（2026-10-12 P0）：单次调用有超时（PROMPT_ENHANCE_TIMEOUT_SEC，默认
  240s —— 思考模式模型长输出实测 118.5s+），外层还有本层总预算（PROMPT_ENHANCE_BUDGET_SEC，
  默认 300s，用于截断 llm_client 对本地网关的最多 3 次读超时重试）；失败按类型
  （限流/超时/连不上/其它）登记**全局冷却**（PROMPT_ENHANCE_COOLDOWN_SEC，默认 600s），
  冷却期内所有提示词直接跳过模型调用 —— 零网络、零等待。
  事故背景：timeout 曾被误调成 25s，思考模式模型必然超时 → 每镜 3×25s ≈ 75s 白等。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import ai_config
import h3_prompt_kit
from prompt_protocol import (  # 协议标记下沉（2026-10-08 解耦）：不再模块级依赖 prompt_qc
    SB_MARK_CONTENT, SB_MARK_CONTENT_LEGACY, SB_MARK_FRAMING_LEGACY, SB_MARK_FRAMING_NEW,
    SB_MARK_NO_TEXT, SB_MARK_NO_TEXT_LEGACY, SB_MARK_PRESERVE, SB_MARK_REF_USAGE,
    SB_MARK_REF_USAGE_LEGACY, SB_MARK_STYLE, SB_MARK_STYLE_LEGACY, SB_MARK_TASK)
from config import (AI_CONFIG_PATH, LLM_CONFIG_PATH,
                    PROMPT_ENHANCE_ENABLED, PROMPT_MODEL_REVIEW_ENABLED,
                    PROMPT_ENHANCE_TIMEOUT_SEC, PROMPT_ENHANCE_CACHE_SIZE,
                    PROMPT_ENHANCE_BUDGET_SEC, PROMPT_ENHANCE_COOLDOWN_SEC,
                    PROMPT_ENHANCE_SKIP_KINDS)
from llm_client import LLMClient

logger = logging.getLogger(__name__)

#: 参与增强/复审的提示词类型（audio=台词，会被 TTS 逐字念出，绝不改写）
_ENHANCEABLE_KINDS: Tuple[str, ...] = ("storyboard", "asset", "keyframe", "h3")

#: per-kind 跳过名单（来自 config.PROMPT_ENHANCE_SKIP_KINDS；默认空 = 不跳过任何类型）。
#   设成 "storyboard" 可**只关分镜**的增强/复审（省前置段 ~56s/镜）。B-4，2026-10-16。
_SKIP_KINDS = frozenset(str(k).strip().lower() for k in PROMPT_ENHANCE_SKIP_KINDS)

#: 运维兜底开关：设为 0/false/off 可在不改代码的情况下临时关闭（排障用）
_ENV_ENHANCE = "MJSCXT_PROMPT_ENHANCE"
_ENV_REVIEW = "MJSCXT_PROMPT_MODEL_REVIEW"

#: 失败缓存的 TTL（秒）：上游故障后 10 分钟内同一条提示词不再重试，避免逐镜空等超时
_FAIL_TTL_SEC = 600

#: 从镜头上下文里抽这些键作为增强/复审的语义依据（有啥用啥，缺了不勉强）
_CTX_KEYS: Tuple[str, ...] = (
    "description", "visual_detail", "storyboard_prompt_zh",
    "camera", "location", "emotion", "name", "outfit",
)

# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #

def _env_flag(name: str) -> Optional[bool]:
    v = str(os.environ.get(name, "")).strip().lower()
    if v in ("0", "false", "off", "no"):
        return False
    if v in ("1", "true", "on", "yes"):
        return True
    return None


def _file_flag(name: str):
    """文件级开关（前端「AI 配置」页写入 prompt_enhance_config.json）。None = 未设置。"""
    try:
        import config as _cfg
        return _cfg._prompt_enhance_file_flags().get(name)
    except Exception:  # noqa: BLE001  配置读取失败不阻断生成链路（fail-open）
        return None


def enhance_enabled() -> bool:
    # 优先级：env（运维最高）> 文件（前端开关）> 代码默认
    flag = _env_flag(_ENV_ENHANCE)
    if flag is None:
        flag = _file_flag("enhance_enabled")
    return PROMPT_ENHANCE_ENABLED if flag is None else flag


def review_enabled() -> bool:
    # 优先级：env（运维最高）> 文件（前端开关）> 代码默认
    flag = _env_flag(_ENV_REVIEW)
    if flag is None:
        flag = _file_flag("review_enabled")
    return PROMPT_MODEL_REVIEW_ENABLED if flag is None else flag


# --------------------------------------------------------------------------- #
# 失败分类 / 单次调用总预算 / 全局冷却（2026-10-12 P0）
# --------------------------------------------------------------------------- #
# 为什么要有这三件东西（实测回归）：增强层原先只有「单次调用超时」一个旋钮，失败记忆又是
# **按提示词**记的 —— 每镜提示词都不同，于是上游一坏就变成「每镜各付一次重试」。
# 实测代价：timeout 被误调成 25s 后，思考模式模型（长输出实测 118.5s）必然超时，
# 每镜 3 次 × 25s ≈ 75s 白等，46 镜 ≈ 57 分钟，且增强 100% 降级。
#
# 三道闸各管一件事，互不替代：
#   · 失败分类 —— 限流/无算力不该按「网络抖动」处理（重试只是重复挨打 → 立即放弃 + 长冷却）；
#   · 总预算   —— timeout 管「单次尝试」，预算管「这一层总共最多等多久」
#                 （llm_client 对本地网关的读超时最多重试 3 次 → 单旋钮必然失衡）；
#   · 全局冷却 —— 把「每镜各付一次」变成「一次失败、整段安静」，这是安静降级的核心。
_RATE_LIMIT_MARKERS: Tuple[str, ...] = (
    "model_unavailable", "cooling down", "rate limit", "rate_limit", "too many requests",
    "no available channel", "no available workers", "all circuits open",
    "无可用算力", "限流",
)
#: 「连不上」类失败的特征（requests/urllib3 的文案 + 中文系统提示）
_UNREACHABLE_MARKERS: Tuple[str, ...] = (
    "failed to establish a new connection", "max retries exceeded", "connection refused",
    "拒绝连接", "nodename nor servname", "name or service not known",
)
#: 「其它」类失败的冷却（秒）：多是单条提示词/单次输出的问题，不该把整层关太久
_OTHER_FAIL_COOLDOWN_SEC = 60
#: 整层共用的冷却键：限流/无算力/超时是**上游级**症状，一次失败就让整层安静下来。
#: （review 与 enhance 共用这一把键 —— 增强已经等满预算后，复审不该再花第二个预算。）
_COOLDOWN_LAYER_KEY = "prompt_enhance"


class EnhanceBudgetExceeded(RuntimeError):
    """单次增强/复审超出**总预算**（墙钟）—— 按超时类处理，安静降级。"""


def _is_timeout_error(exc) -> bool:
    if isinstance(exc, (TimeoutError, EnhanceBudgetExceeded)):
        return True
    return ("timeout" in type(exc).__name__.lower()
            or "timed out" in str(exc).lower() or "超时" in str(exc))


def _classify_failure(exc) -> str:
    """把 LLM 失败分类（决定冷却时长与日志措辞；每一类都**不再叠加我们自己的等待**）。

    · `ratelimit`   —— 限流 / 无算力（HTTP 400 model_unavailable、429、账号冷却、
      "no available channel"…）：上游明确说「现在没配额」，重试只是重复挨打。
    · `timeout`     —— 读超时或超出本层总预算：模型可能只是慢（思考模式），但本次已经等够。
    · `unreachable` —— 连不上（ConnectionError / 拒绝连接 / DNS 失败）。远端会 fail-fast，
      但**本地/内网**网关按 llm_client 现有逻辑仍会重试 3 次并退避（实测 11s/次），
      故同样归入长冷却：上游根本不在，没必要每镜都试一遍。
    · `other`       —— 其它（未配置 / 输出非 JSON / 未知异常）：立即放弃，短冷却。
    """
    _low = str(exc or "").lower()
    if any(_m in _low for _m in _RATE_LIMIT_MARKERS):
        return "ratelimit"
    if _is_timeout_error(exc):
        return "timeout"
    if any(_m in _low for _m in _UNREACHABLE_MARKERS):
        return "unreachable"
    if "connectionerror" in type(exc).__name__.lower():
        return "unreachable"
    return "other"


_COOLDOWN: Dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


def _cooldown_remaining(module: str) -> float:
    """本层（或本模块）冷却剩余秒数；0 = 可正常调用模型。"""
    _now = time.time()
    with _COOLDOWN_LOCK:
        _layer = float(_COOLDOWN.get(_COOLDOWN_LAYER_KEY) or 0.0)
        _own = float(_COOLDOWN.get(module) or 0.0)
    return max(0.0, max(_layer, _own) - _now)


def _note_failure(module: str, category: str) -> int:
    """登记一次失败：按类型设置冷却，返回冷却秒数（0 = 不冷却）。"""
    _sec = (int(PROMPT_ENHANCE_COOLDOWN_SEC)
            if category in ("ratelimit", "timeout", "unreachable")
            else int(_OTHER_FAIL_COOLDOWN_SEC))
    if _sec <= 0:
        return 0
    _key = (_COOLDOWN_LAYER_KEY
            if category in ("ratelimit", "timeout", "unreachable") else module)
    with _COOLDOWN_LOCK:
        _base = max(float(_COOLDOWN.get(_key) or 0.0), time.time())
        _COOLDOWN[_key] = _base + _sec
    return _sec


def reset_cooldown() -> None:
    """清掉全局冷却（人工重试 / 诊断 / 单测用）。"""
    with _COOLDOWN_LOCK:
        _COOLDOWN.clear()


def _call_with_budget(fn, what: str, budget_sec: float = None):
    """在**墙钟总预算**内跑一次模型调用；返回 `(ok, value, error)`。

    为什么需要它：llm_client 对**本地/内网**网关的读超时最多重试 3 次
    （本地冷启动 503 会自愈，故不走 fail-fast 分支）→ 单次调用最坏 3×timeout。
    只有 timeout 一个旋钮无法同时满足「思考模式要 240s」与「失败不得 75s/镜」
    （取 240s → 最坏 720s；取小 → 正常增强必然超时，这正是本次 P0 的根因）。
    超预算即放弃：被放弃的调用留在**守护线程**里自行结束（其超时由 requests 负责），
    结果丢弃 —— 主链路立刻继续，这是「安静降级」的关键部分。
    """
    _budget = float(PROMPT_ENHANCE_BUDGET_SEC if budget_sec is None else budget_sec)
    _box: Dict[str, object] = {}

    def _worker() -> None:
        try:
            _box["value"] = fn()
        except BaseException as _e:  # noqa: BLE001 —— 原样带回主线程做分类
            _box["error"] = _e

    _th = threading.Thread(target=_worker, name="prompt-enhance-llm", daemon=True)
    _t0 = time.time()
    _th.start()
    _th.join(timeout=max(1.0, _budget))
    _elapsed = time.time() - _t0
    if _th.is_alive():
        return False, None, EnhanceBudgetExceeded(
            f"{what} 超出本层总预算 {_budget:.0f}s（已放弃等待，后台调用自行结束；"
            f"实际等待 {_elapsed:.1f}s）")
    if "error" in _box:
        return False, None, _box["error"]
    if _elapsed >= 1.0:
        logger.info("%s 用时 %.1fs（预算 %.0fs）", what, _elapsed, _budget)
    return True, _box.get("value"), None


# --------------------------------------------------------------------------- #
# 客户端 / 上下文
# --------------------------------------------------------------------------- #

def _client_for(module: str) -> Optional[LLMClient]:
    """构造某个 AI 模块的客户端（text=增强 / qc=复审）；未配置返回 None"""
    cfg = ai_config.get_module(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH), module)
    if not isinstance(cfg, dict):
        return None
    client = LLMClient(AI_CONFIG_PATH, config=cfg, timeout=PROMPT_ENHANCE_TIMEOUT_SEC)
    return client if client.configured else None


def _module_model(module: str) -> str:
    """取某模块当前生效的 model 名（用于缓存 key 防串味；读失败返回空串）。

    ⭐ 2026-10-12（t50）：缓存 key 原不含 model —— 换模型后同一条 prompt 会命中
    旧增强/复审结果（进程内 FIFO 长期有效）。现把 model 并入 key。
    """
    try:
        cfg = ai_config.get_module(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH), module)
        return str((cfg or {}).get("model") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _ctx_brief(ctx) -> str:
    """把镜头上下文压成一段简短文本（给模型当语义依据）"""
    parts: List[str] = []
    if isinstance(ctx, dict):
        for k in _CTX_KEYS:
            v = str(ctx.get(k) or "").strip()
            if v:
                parts.append(f"- {k}: {v[:400]}")
    return "\n".join(parts) if parts else "（无）"


# --------------------------------------------------------------------------- #
# 骨架校验：增强/改进结果必须与原文结构等价，否则弃用
# --------------------------------------------------------------------------- #

_IMAGE_TAG_RE = re.compile(r"<image\s*(\d+)\s*>", re.IGNORECASE)
_PICTURE_TAG_RE = re.compile(r"<Picture\s*(\d+)>", re.IGNORECASE)
_TS_RE = re.compile(r"\b\d{1,2}:\d{2}\.\d{3}\b")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

#: 分镜图协议段名（旧中文协议 + 新英文协议一并检查：原文有的段，结果必须有）
_STORYBOARD_SECTIONS: Tuple[str, ...] = (
    SB_MARK_TASK, SB_MARK_PRESERVE,
    SB_MARK_CONTENT, SB_MARK_CONTENT_LEGACY,
    SB_MARK_STYLE, SB_MARK_STYLE_LEGACY,
    SB_MARK_FRAMING_NEW, SB_MARK_FRAMING_LEGACY,
    SB_MARK_REF_USAGE, SB_MARK_REF_USAGE_LEGACY,
    SB_MARK_NO_TEXT, SB_MARK_NO_TEXT_LEGACY,
)


def _strip_fence(text: str) -> str:
    """剥掉模型输出可能包裹的 Markdown 代码块围栏"""
    return _FENCE_RE.sub("", str(text or "").strip(), count=0).strip()


def _skeleton_ok(kind: str, original: str, candidate: str) -> Tuple[bool, str]:
    """校验候选提示词与原文结构等价。返回 (是否通过, 不通过原因)"""
    cand = str(candidate or "").strip()
    if not cand:
        return False, "空结果"
    if len(cand) > h3_prompt_kit.MAX_PROMPT_CHARS:
        return False, "超出长度上限"
    # 骤缩保护：增强只会更具体，篇幅骤减多半是丢段/丢内容
    if len(cand) < max(24, int(len(original) * 0.7)):
        return False, "篇幅骤减（疑似丢内容）"

    if kind == "storyboard":
        for mark in _STORYBOARD_SECTIONS:
            if mark in original and mark not in cand:
                return False, f"丢失协议段 {mark}"
        tags_o = sorted({m.group(1) for m in _IMAGE_TAG_RE.finditer(original)})
        tags_c = sorted({m.group(1) for m in _IMAGE_TAG_RE.finditer(cand)})
        if tags_o != tags_c:
            return False, "<imageN> 编号集合发生变化"
    elif kind == "h3":
        v = h3_prompt_kit.validate(cand)
        if not v.get("valid"):
            miss = "、".join(v.get("missing") or []) or "未知"
            return False, f"H3 结构缺段（{miss}）"
        pics_o = sorted({m.group(1) for m in _PICTURE_TAG_RE.finditer(original)})
        pics_c = sorted({m.group(1) for m in _PICTURE_TAG_RE.finditer(cand)})
        if pics_o != pics_c:
            return False, "<Picture N> 标签集合发生变化"
        # 时间码只能照抄不能改轴：数量明显变少说明节拍被合并/删除
        if len(_TS_RE.findall(cand)) < len(_TS_RE.findall(original)):
            return False, "时间码数量变少（节拍被删）"
    else:  # asset / keyframe：过一遍确定性检查，判废即弃
        import prompt_qc  # 惰性导入（2026-10-08 解耦）：只在需要确定性复检时才依赖质检模块
        v = prompt_qc.check_prompt(kind, cand, ctx=None, style="")
        if v.get("blocked"):
            return False, "确定性检查判废"
    return True, ""


def prompt_len_budget(original: str) -> Tuple[int, int]:
    """返回骨架校验**实际接受**的字符区间 ``(下限, 上限)``。

    ⚠️ 与 :func:`_skeleton_ok` 的判据**逐字同源**，改一处必须同步改另一处。
    存在的意义：把「校验器接受什么」提前变成「生成前就告诉模型什么」——
    2026-10-08 实录：优化器/增强器的系统提示里**完全没有长度约束**，模型自由扩写 →
    129 次结果因「超出长度上限 / 篇幅骤减 / 丢协议段」被全部弃用，每次都是一个白打的
    LLM 调用（几十秒 × 上百次）。
    """
    hi = int(h3_prompt_kit.MAX_PROMPT_CHARS)
    lo = max(24, int(len(str(original or "")) * 0.7))
    return lo, hi


def has_valid_window(original: str) -> bool:
    """原文是否**存在**一个骨架校验能接受的输出长度。

    ``lo = 原文×0.7``、``hi = MAX_PROMPT_CHARS(6000)`` —— 原文一长到约 8571 字符
    （6000/0.7），区间就成了空集：模型**无论怎么写都过不了**。此时再打一次 LLM
    纯属白费（几十秒 + 一次配额），应当直接跳过增强/优化并回落原文。
    """
    lo, hi = prompt_len_budget(original)
    return lo <= hi


def length_clause(original: str, *, keep_ratio: float = 1.2) -> str:
    """生成一段可直接拼进 system 提示的**输出长度硬约束**。

    只描述长度，不碰其它规则；调用方自行拼接（增强 / 即时优化共用同一把尺子）。
    """
    lo, hi = prompt_len_budget(original)
    n = len(str(original or ""))
    target = min(hi, max(lo, int(n * keep_ratio)))
    return (
        "\n【输出长度硬约束 —— 不满足则本次结果直接作废】\n"
        f"- 原提示词 {n} 字符；你的输出**完整字符数**必须落在 {lo}–{hi} 之间。\n"
        f"- 目标约 {target} 字符：比原文略具体即可，不要大幅扩写。\n"
        f"- 严禁超过 {hi} 字符（超出即判废）；严禁少于 {lo} 字符（骤缩即判废）。\n"
        "- 不得为压缩篇幅而删减任何段落或节拍，结构必须与原文同样齐全。"
    )


# --------------------------------------------------------------------------- #
# 进程内缓存（成功结果长期有效；失败按 TTL 短暂记忆）
# --------------------------------------------------------------------------- #

_OK_CACHE: Dict[str, str] = {}
_FAIL_CACHE: Dict[str, float] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Optional[str]:
    with _CACHE_LOCK:
        return _OK_CACHE.get(key)


def _cache_put_ok(key: str, value: str) -> None:
    with _CACHE_LOCK:
        if PROMPT_ENHANCE_CACHE_SIZE <= 0:
            return
        if len(_OK_CACHE) >= PROMPT_ENHANCE_CACHE_SIZE:
            _OK_CACHE.pop(next(iter(_OK_CACHE)), None)
        _OK_CACHE[key] = value


def _fail_recent(key: str) -> bool:
    with _CACHE_LOCK:
        t = _FAIL_CACHE.get(key)
        return bool(t and (time.time() - t) < _FAIL_TTL_SEC)


def _cache_put_fail(key: str) -> None:
    with _CACHE_LOCK:
        _FAIL_CACHE[key] = time.time()


def _cache_key(kind: str, style: str, prompt: str, salt: str = "", model: str = "") -> str:
    # ⭐ 2026-10-12（t50）：model 并入键 —— 换模型后不命中旧增强/复审结果。
    basis = f"{kind}|{style}|{salt}|{model}|{prompt}"
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()


# --------------------------------------------------------------------------- #
# ① LLM 增强（text 模块）
# --------------------------------------------------------------------------- #

_ENHANCE_INSTRUCTIONS: Dict[str, str] = {
    "storyboard": (
        "你是资深漫剧分镜提示词工程师。用户给出一条即将提交给图像生成模型"
        "（Qwen-Image，英文 <imageN> 参考图协议）的**结构化**分镜提示词，以及该镜头的中文上下文。"
        "请按 **Qwen-Image 官方规范**把它改写为**一段流畅、自然的英文描述性语言**，规则：\n"
        "1. **必须输出单个连贯段落**；**禁止保留任何分段标题、编号、列表或 Markdown 结构**"
        "（正文里不得出现 TASK: / FRAMING: / IDENTITY: / SCENE AND ACTION: 这类字样）；\n"
        "2. 必须完整保留这些**硬约束**（用自然语言融进句子）：景别与机位；每个角色的朝向与姿态"
        "（如「背影、背对镜头、面部不可见」）；「同一角色/道具在画面中只出现一次、不得分身或"
        "复制、多角色各保身份不串味」；参考图是身份来源（按 <imageN> 逐一对应，不得张冠李戴）；"
        "场景与动作；整体画风；\n"
        "3. 所有 <imageN> 编号引用**原样保留**；不得增删角色/道具；不得出现任何「文字/字幕/水印」"
        "相关描述；景别与运镜遵守参考片语法（特写/局部/近景固定机位，运镜只写在中景上）；\n"
        "4. 写成**对这一帧画面本身的描述**（谁在哪、在做什么、什么景别/机位/光影/画风），"
        "而不是对模型的命令句；\n"
        "5. 末尾加：Ultra HD, 4K, cinematic composition。直接输出改写后的那一段，不要解释、"
        "不要 Markdown 代码块。"
    ),
    "asset": (
        "你是漫剧资产设定图提示词工程师。用户给出一条资产（角色/物品/场景）参考图的"
        "生成提示词。请增强主体外观描述（材质、比例、结构、颜色、做旧与磨损等可画出来的"
        "细节），规则：\n"
        "1. 不改变主体的身份特征与数量，不新增人物、品牌、logo；\n"
        "2. 已有的风格声明与「不得出现文字/水印」约束必须原样保留且只出现一次；\n"
        "3. 语言与原文保持一致，直接输出完整提示词，不要解释、不要 Markdown 代码块。"
    ),
    "keyframe": (
        "你是漫剧首尾帧插值的提示词工程师。用户给出一条「尾帧」图生图提示词：它描述"
        "该镜头动作推进的终点画面。请增强动作终点的具体描述（身体姿态、重心与位置位移、"
        "道具状态、环境随动作的变化），规则：\n"
        "1. 「保持参考图的角色外观」「与首帧保持连贯」这类锚定/连贯语句必须逐字保留"
        "（若原文含「承接该画面继续推进」同样保留）；\n"
        "2. 已有的风格声明与「不得出现文字」约束原样保留；\n"
        "3. 语言与原文保持一致，直接输出完整提示词，不要解释、不要 Markdown 代码块。"
    ),
    "h3": (
        "你是漫剧视频提示词工程师。用户给出一条即将提交给视频生成模型 H3 的英文结构化"
        "提示词。请把 detailed_description 段内各节拍的动作描述写得更具体（人物动作、"
        "镜头运动、情绪表达），规则：\n"
        "1. 全部段名（summary / retention_analysis / detailed_description 等）、"
        "全部时间码（[Shot N] MM:SS.mmm 与 At MM:SS.mmm）、全部 <Picture N> / <Subject N> "
        "标签必须逐字保留，不新增节拍、不改动时间轴、不增删参考图标签；\n"
        "2. 只在现有节拍句内部丰富细节，与中文上下文保持一致；\n"
        "3. 直接输出完整提示词全文，不要解释、不要 Markdown 代码块。"
    ),
}


def enhance_prompt(kind: str, prompt: str, ctx=None, style: str = "") -> dict:
    """LLM 增强。返回 ``{prompt, applied, note}``；applied=False 时 prompt=原文。"""
    out = {"prompt": prompt, "applied": False, "note": ""}
    kind = str(kind or "").strip().lower()
    if kind not in _ENHANCEABLE_KINDS or not str(prompt or "").strip():
        return out
    if kind in _SKIP_KINDS:
        out["note"] = "该类型已按配置跳过增强（MJSCXT_PROMPT_ENHANCE_SKIP_KINDS）"
        return out
    if not enhance_enabled():
        out["note"] = "增强未启用"
        return out

    key = _cache_key(kind, style, prompt, salt="enhance", model=_module_model("text"))
    hit = _cache_get(key)
    if hit is not None:
        out.update(prompt=hit, applied=True, note="缓存命中")
        return out
    if _fail_recent(key):
        out["note"] = "近期增强失败（冷却中），按原文继续"
        return out
    _cd = _cooldown_remaining("text")
    if _cd > 0:
        # 安静降级：整层冷却期不写 warning（每镜一行会刷屏），只留 debug + note
        out["note"] = f"增强整层冷却中（剩余 {int(_cd)}s），按原文继续"
        logger.debug("提示词增强[%s] 跳过：整层冷却剩余 %.0fs", kind, _cd)
        return out

    try:
        client = _client_for("text")
        if client is None:
            out["note"] = "text 模块未配置，跳过增强"
            return out
        user = (f"【目标风格】{str(style or '').strip() or '（未指定）'}\n"
                f"【镜头上下文】\n{_ctx_brief(ctx)}\n"
                f"【待增强的生成提示词】\n{prompt}")
        # ⭐ 2026-10-08：把骨架校验的长度区间**前置**成模型硬约束（原提示词里零长度
        #    约束 → 模型自由扩写 → 高频因「超出长度上限」被判废，白打一次 LLM）。
        if not has_valid_window(prompt):
            _lo0, _hi0 = prompt_len_budget(prompt)
            logger.info("提示词增强[%s] 跳过：原文 %d 字符，骨架校验的接受区间 [%d,%d] 为空集"
                        "（模型无论怎么写都过不了）", kind, len(prompt), _lo0, _hi0)
            out["note"] = "原文超长，长度区间为空集，跳过增强"
            return out
        _system = _ENHANCE_INSTRUCTIONS[kind] + length_clause(prompt)
        # max_tokens 只是物理兜底（中文约 1.5 字符/token，取 1.2 保守系数并留思考余量）；
        # 真正的长度控制由 system 里的硬约束表达。
        _mt = max(1500, min(8192, int(h3_prompt_kit.MAX_PROMPT_CHARS / 1.2)))
        _ok, _resp, _err = _call_with_budget(
            lambda: client.chat(
                [{"role": "system", "content": _system},
                 {"role": "user", "content": user}],
                temperature=0.4, max_tokens=_mt, timeout=PROMPT_ENHANCE_TIMEOUT_SEC),
            what=f"提示词增强[{kind}]")
        if not _ok:
            raise _err
        cand = _strip_fence(str(_resp))
        ok, why = _skeleton_ok(kind, prompt, cand)
        if not ok:
            logger.info("提示词增强[%s] 结果未通过骨架校验（%s），保持原文", kind, why)
            out["note"] = f"增强输出未过校验（{why}），保持原文"
            _cache_put_fail(key)
            return out
        _cache_put_ok(key, cand)
        out.update(prompt=cand, applied=True, note="LLM 增强已采纳")
        logger.info("提示词增强[%s] 已采纳（%d -> %d 字符）", kind, len(prompt), len(cand))
        return out
    except Exception as e:  # noqa: BLE001  fail-open：增强失败绝不阻断生成
        _cat = _classify_failure(e)
        _sec = _note_failure("text", _cat)
        logger.warning("提示词增强[%s] 失败（按原文继续）：[%s] %s: %s%s",
                       kind, _cat, type(e).__name__, e,
                       f"；整层冷却 {_sec}s（期间所有提示词跳过增强/复审）" if _sec else "")
        out["note"] = f"增强调用失败（{_cat}），按原文继续"
        _cache_put_fail(key)
        return out


# --------------------------------------------------------------------------- #
# ② 质检模型语义复审（qc 模块）
# --------------------------------------------------------------------------- #

_REVIEW_INSTRUCTION = (
    "你是漫剧生成管线的提示词质检员。图像/视频即将严格按这条提示词生成，生成很贵，"
    "请在消耗 GPU 之前把语义层的问题挑出来。只审以下三类，不要吹毛求疵——风格措辞差异、"
    "正常可生成的描述、与上下文无关的细节都不算问题：\n"
    "1) 画面主体与动作是否清晰、可生成（不含糊、不自相矛盾）；\n"
    "2) 是否与【镜头上下文】一致（人物、场景、情绪、景别/机位）；\n"
    "3) 是否存在会导致画面崩坏的描述（多个主体的身份指代混淆、光影/景别自相矛盾、"
    "把台词当画面内容写等）。\n"
    # ⭐ 2026-10-04：补「本系统既定设计」契约，压掉质检模型的**已知假阳性**。
    #   实测（蛊真人第一集预检）：镜头上下文里出现「画外音」三个字，模型就判
    #   「提示词声明 no voice-over 与上下文矛盾」并把改写版采纳进工作流 —— 而
    #   「H3 不出声、人声后期合成」正是本系统的设计（H3_STRIP_AUDIO），
    #   no voice-over 是**必需**的防字幕约束。缺少该契约时模型的语义判断会跑偏。
    "\n【本系统的既定设计——以下**不是**缺陷，不要据此判不通过】\n"
    "- H3 只出画面、不出人声：人声由后期 QwenTTS 独立合成并按时轴合入，"
    "H3 自带音轨会被分离剔除。故提示词中的「no one speaks / no voice-over "
    "narration」是**正确且必需**的防字幕约束——即使【镜头上下文】的描述里出现"
    "「画外音」「旁白」等叙事字眼，也不算矛盾。\n"
    "- 台词原文不进提示词：口语内容只由配音层消费，提示词只描述「谁、什么语气、"
    "开口说多久」；提示词不含台词文本**不是**缺陷。\n"
    "- 参考图标签 <Picture N> / <Subject N> 由程序按槽位生成，编号与顺序不可改动。\n"
    "- 分镜图参考是**构图/景别/机位/站位基准**（staging baseline），"
    "其「composition anchor」措辞正确，不要改写成环境材质类表述。\n"
    "输出严格 JSON（不要解释、不要代码块）：\n"
    '{"pass": true/false, "issues": ["问题1", "问题2"], '
    '"improved": "仅当 pass=false 时给出修改后的完整提示词，否则留空字符串"}'
)


def _extract_json(text: str) -> dict:
    """从模型输出里提取第一个 JSON 对象（容忍代码块/前后杂讯）"""
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def model_review(kind: str, prompt: str, ctx=None, style: str = "") -> dict:
    """质检模型语义复审。返回 ``{passed, issues, improved_prompt, note}``。

    ``improved_prompt`` 只有在不通过且改进版通过了骨架校验时才非空——
    采纳与否由调用方（prompt_qc.preflight）再跑一次确定性复检决定。
    """
    out = {"passed": True, "issues": [], "improved_prompt": "", "note": ""}
    kind = str(kind or "").strip().lower()
    if kind not in _ENHANCEABLE_KINDS or not str(prompt or "").strip():
        out["note"] = "该类型不参与模型复审"
        return out
    if kind in _SKIP_KINDS:
        out["note"] = "该类型已按配置跳过复审（MJSCXT_PROMPT_ENHANCE_SKIP_KINDS）"
        return out
    if not review_enabled():
        out["note"] = "复审未启用"
        return out

    key = _cache_key(kind, style, prompt, salt="review", model=_module_model("qc"))
    hit = _cache_get(key)
    if hit is not None:
        try:
            cached = json.loads(hit)
            if isinstance(cached, dict):
                cached.setdefault("note", "缓存命中")
                return cached
        except Exception:  # noqa: BLE001
            pass
    if _fail_recent(key):
        out["note"] = "近期复审失败（冷却中），默认通过"
        return out
    _cd = _cooldown_remaining("qc")
    if _cd > 0:
        # 整层冷却（含「增强刚失败」这一情形）：复审不再花第二个预算，安静默认通过
        out["note"] = f"复审整层冷却中（剩余 {int(_cd)}s），默认通过"
        logger.debug("提示词复审[%s] 跳过：整层冷却剩余 %.0fs", kind, _cd)
        return out

    try:
        client = _client_for("qc")
        if client is None:
            out["note"] = "qc 模块未配置，跳过复审"
            return out
        import prompt_qc  # 惰性导入（2026-10-08 解耦）：仅取类型标签
        user = (f"【提示词类型】{prompt_qc.KIND_LABELS.get(kind, kind)}\n"
                f"【目标风格】{str(style or '').strip() or '（未指定）'}\n"
                f"【镜头上下文】\n{_ctx_brief(ctx)}\n"
                f"【待审提示词】\n{prompt}")
        _ok, _resp, _err = _call_with_budget(
            lambda: client.chat(
                [{"role": "system", "content": _REVIEW_INSTRUCTION},
                 {"role": "user", "content": user}],
                temperature=0.1, max_tokens=8192, timeout=PROMPT_ENHANCE_TIMEOUT_SEC),
            what=f"提示词复审[{kind}]")
        if not _ok:
            raise _err
        data = _extract_json(str(_resp))
        if not data:
            out["note"] = "复审输出不是合法 JSON，默认通过"
            _cache_put_fail(key)
            return out
        passed = bool(data.get("pass", True))
        issues = [str(x) for x in (data.get("issues") or [])][:5]
        improved = _strip_fence(str(data.get("improved") or ""))
        if improved and improved == str(prompt or "").strip():
            improved = ""
        if improved:
            ok, why = _skeleton_ok(kind, prompt, improved)
            if not ok:
                improved = ""
                out["note"] = f"改进版未过骨架校验（{why}），弃用"
        result = {"passed": passed, "issues": issues,
                  "improved_prompt": improved if not passed else "",
                  "note": "复审完成"}
        _cache_put_ok(key, json.dumps(result, ensure_ascii=False))
        return result
    except Exception as e:  # noqa: BLE001  fail-open：复审失败不影响本次生成
        _cat = _classify_failure(e)
        _sec = _note_failure("qc", _cat)
        logger.warning("提示词复审[%s] 失败（默认通过）：[%s] %s: %s%s",
                       kind, _cat, type(e).__name__, e,
                       f"；整层冷却 {_sec}s（期间所有提示词跳过增强/复审）" if _sec else "")
        out["note"] = f"复审调用失败（{_cat}），默认通过"
        _cache_put_fail(key)
        return out
