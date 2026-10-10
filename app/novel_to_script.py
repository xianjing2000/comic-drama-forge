"""
小说 → A 版剧本转换器

核心思路（长篇小说必须分块，但**不得删减原小说内容**，只做小说→剧本的体裁改写）：
1. 分块：优先按章节把全文聚合成约 CHUNK_CHARS 字的块；无章节则按段落聚合
2. 全量覆盖：分块只用于控制单次 prompt 体量，**所有块都必须产出结果并合并**，
   块与块连续无缝、不跳段、不抽样（旧的「首尾保留 + 中间均匀抽样」逻辑已取消）
3. 三段式生成：
   ① 逐块提炼（每块一次调用）→ 剧情摘要 / 人物 / 物品 / 场景 / 情节要点
   ② 汇总设定（一次调用）→ title / theme / style / characters[] / items[] / scenes[]（含参考图提示词）
   ③ 逐块写分镜（每块一次调用）→ shots[]（A 版字段，含 prompt_h3 草稿）
4. 组装为系统 A 版剧本 Schema 并落盘 output/scripts
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime

from llm_client import LLMError, LLMTruncatedError, LLMGatewayUnavailable
from dialogue_utils import dialogue_text as _dlg_text, normalize_lines as _dlg_lines
import fs_atomic
import style_kit
import h3_prompt_kit
import asset_prompt_kit
import asset_name_match
# 提示词模板中心（2026-10-07 外置改造）：REWRITE_RULES 等常量的生效值从这里加载
# （用户覆盖 > 出厂默认 app/prompts/*.txt > 代码内兜底常量）。只依赖标准库、config 延迟
# 导入，不会形成循环依赖。
import prompt_templates
# 景别唯一权威表（写入侧白名单与提示词枚举都由它派生；config 是叶子模块，无循环依赖）
from config import (
    COVERAGE_THRESHOLD,   # 唯一事实源在 config（2026-10-08 解耦）
    SHOT_TYPES, ACTION_WORDS, ACTION_CLAUSE_RE, count_action_beats,
    SHOT_DURATION_MIN, SHOT_DURATION_MAX, SHOT_DURATION_SILENT, CHARS_PER_SECOND,
    BEAT_CLIMAX_BONUS_SEC, SHOT_SPEECH_BUDGET_CHARS,
    SHOT_DURATION_DESC_SEC_MAX, SHOT_DURATION_DESC_CHARS_PER_SEC,
    SHOT_DURATION_ACTION_SEC_MAX,
    # 分镜粒度（2026-10-06 用户指定：每集 20~30 镜、每镜 5~6 秒）——见 config 里
    # SHOT_GRANULARITY_* 与本文件 CHARS_PER_SHOT / REF_INSERT_RATIO / SPLIT_ACTION_BEATS 的注释。
    SHOT_CAP_GROWTH, SHOT_GRANULARITY_TARGET_SHOTS, SHOT_GRANULARITY_MAX_SHOTS,
)

logger = logging.getLogger(__name__)

# ⚠️ 2026-09-25 实测修正：3000 字 × (1/CHARS_PER_SHOT=120) ≈ 25 镜/块，
#    **正好越过 MAX_SHOTS_PER_CHUNK=24 的单次调用响应体红线** → 预劈半几乎每块都触发
#    （实测日志 32 次「目标 N 镜超过单块上限 24，预拆为 2 个子块」），
#    把「一次调用」变成「先拆再调」，既慢又平白多一层失败面。
#    改为 2400 字 → 约 20 镜/块，首次调用即落在红线内（预劈半退回真正的兜底而非常态）。
#    原文字字不丢：只是块数变多、每块更短，总覆盖不变。
#    （⚠️ 现值注记：`CHARS_PER_SHOT` 已于 2026-10-06 由 120 改为 240 → 3000 字 ≈ 13 镜/块，
#     「预劈半是常态」的前提**已不成立**；`CHUNK_CHARS=2400` 本身保留不动。）
CHUNK_CHARS = 2400
MIN_CHUNK_CHARS = 300
MAX_CHARS_PER_CHUNK_PROMPT = 3400
COVERAGE_MAX_ROUNDS = 1          # 原文覆盖率不足时自动补生成轮次上限（与 continuity.COVERAGE_MAX_ROUNDS 对齐）

# ===================== 每集时长口径（产品需求，2026-09-26） =====================
# ⚠️ 这是**产品口径**，不是技术红线 —— 与下方两条技术红线（H3 资源红线 / LLM 响应体红线）
#    是三个独立维度，别混：
#      · EPISODE_MAX_SEC   = 用户要的「一集多长」（本块）
#      · MAX_SHOTS_PER_EPISODE = H3 连续渲染的资源红线（78 段会崩）
#      · MAX_SHOTS_PER_CHUNK   = 单次 LLM 调用的响应体红线（24 镜）
#
# 需求：**每集 1-2 分钟，最长不超过 3 分钟**。故取
#   - EPISODE_MAX_SEC = 180（3 分钟）＝ 硬上限，拆集判据用它；
#   - EPISODE_TARGET_SEC = 90（1.5 分钟）＝ 期望中位，仅用于日志/展示与质检目标值；
#   - EPISODE_MIN_SEC = 60（1 分钟）＝ 下限参考，低于它的集不被判缺陷（内容自然就这么长）。
#
# ⚠️ 为什么拆集判据用「时长」而不是「镜数」：
#   历史实现用 `ceil(预估镜数 / MAX_SHOTS_PER_EPISODE=78)`，而 78 镜 ≈ 6.5 分钟成片 ——
#   从「集」的产品定义（1-2 分钟一集）看这个阈值形同虚设：实测《逆天系统》每章 1766 字
#   ≈ 15 镜 ≈ 70 秒，**永远触发不了拆集**，一本 42 章的小说会产出 42 集、每集都超出/逼近
#   产品口径。改成按预估成片秒数判据后，集数才真正由「一章能拍几分钟」决定。
#
# 换算依据（用本仓库实测数据标定，不是拍脑袋）：
#   第 1 集实测 63 镜 / 372 秒成片 / 原文 2361 字 → **约 6.35 字原文 = 1 秒成片**，
#   即约 5.9 秒/镜、约 37 字原文/镜。为留出画面细节余量，下方按**保守的**
#   EST_SEC_PER_SHOT=6.0 秒/镜、SEC_PER_SRC_CHAR=1/6 换算：
#     180 秒 ≈ 30 镜 ≈ 1080 字原文
#   比实测密度更保守 → 算出的份数只会偏多（更碎），不会偏少（更不碎）。
#   拆碎是安全的（原文不丢、每集仍完整叙事），拆不够才危险（超出产品口径）。
def _env_pos_int(name: str, default: int, floor: int = 1) -> int:
    """读一个**正整数**环境变量，非法/缺失/≤0 一律回落到 `default`。

    ⚠️ 为什么不写成 `int(os.environ.get(name, default) or default)`：
    那样在 env 设为 **"0"** 时 `"0" or default` 走的是 `"0"`（非空字符串为真）
    → `int("0")` = 0 → 再被 `max(floor, ...)` 抬到 floor，**悄悄变成 floor 而不是默认值**
    （实测 `MJSCXT_EPISODE_TARGET_SEC=0` 会得到 10 而不是 90）。
    这里显式判「解析失败或 ≤0 → 用默认」，语义可预期、可被守卫断言。
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= floor else default


def _env_float(name: str, default: float, floor: float = 0.0, ceiling: float = 1.0) -> float:
    """读一个 [floor, ceiling] 区间的浮点环境变量，非法/缺失/越界一律回落 `default`。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not (floor <= val <= ceiling):
        return default
    return val


EPISODE_MAX_SEC = _env_pos_int("MJSCXT_EPISODE_MAX_SEC", 180, floor=30)
EPISODE_TARGET_SEC = _env_pos_int("MJSCXT_EPISODE_TARGET_SEC", 90, floor=10)
EPISODE_MIN_SEC = _env_pos_int("MJSCXT_EPISODE_MIN_SEC", 60, floor=5)

#: 单镜成片秒数的**拆集规划**用估值（实测 7.28 → 取 7.5）。
#: ⚠️ 与 `estimate_shot_duration`（按台词/画面内容逐镜精算）**不是一回事**：
#:    那个要读镜头内容、用于生成期对账；这个是**不读正文**的前置规划估值，用于算集数。
#: ⚠️ 为什么是 7.5 而不是早先的 6.0：
#:    旧口径第 1 集（2361 字 / 63 镜）平均 5.9 秒/镜，但那次有 **24 个覆盖率补生成镜头**
#:    稀释了密度（补生成镜头承载原文少、时长短）。拆集改造后每集变小、覆盖率一次通过、
#:    无补生成 —— 纯主生成实测（第 1 部分 823 字 / 29 镜）是 **7.28 秒/镜**。
#:    用 6.0 会系统性低估成片时长 → 拆集不足（实测第 1 部分估 138 秒不拆，实际 211 秒）。
try:
    EPISODE_PLAN_SEC_PER_SHOT = max(
        1.0, float(os.environ.get("MJSCXT_EPISODE_PLAN_SEC_PER_SHOT", "7.5") or 7.5))
except (TypeError, ValueError):
    EPISODE_PLAN_SEC_PER_SHOT = 7.5

#: 🔴 **规划用的「每镜承载原文」实测标定值**（2026-09-26 二次修正，本次最关键的一处）。
#:
#: 背景：`CHARS_PER_SHOT=120` 是**模型提示词侧的软引导**（「约每 120 字原文写 1 镜」），
#: 实测**模型根本不遵守** —— 真实生成密度约是它的 **4 倍**：
#:     第 1 部分实测：原文 823 字 → 实际产出 29 镜（≈ **28.4 字/镜**），
#:     而 `estimate_shots_for_chars(823)` 只给出 7 镜 —— 规划值只有实际的 1/4。
#: 后果（本次实测踩到两次）：「按预估秒数拆集」的判据因为预估值腰斩而**永远不触发**：
#:     42 章全部估算 102~162 秒（都在 180 秒上限内）→ 42 章仍是 42 集。
#:     即便第一版改到 36 字/镜，第 1 部分（823 字）仍估 138 秒 < 180 秒不拆，
#:     实际却生成 211 秒（3.5 分钟）—— 因为 36 字/镜来自**旧口径**（含 24 个补生成镜头
#:     的稀释），不是纯主生成的密度。
#:
#: 处置：拆集**规划**改用本常量（纯主生成实测密度）而不是 `CHARS_PER_SHOT`。
#: 为什么不在估计器里直接改 `CHARS_PER_SHOT`：它还被覆盖率容量校验
#: （`build_chapter_coverage_meta(capacity=n*chars_per_shot)`）消费，
#: 改它会连带改覆盖率口径 —— 那不是这次要动的东西。
#: （审计 P2-10 修正（2026-09-29）：旧注释声称 CHARS_PER_SHOT 还经
#: `REWRITE_RULES.format(chars_per_shot=...)` 喂给模型提示词 —— 实为空转：
#: REWRITE_RULES 全文没有该占位符，`.format()` 无害但不生效，调用点已移除。）
#: 因此**只把规划口径独立出来**：两个数字服务两个目的，各自有各自的依据。
#:
#: 取值：实测 823/29 = 28.4 字/镜。取 **26**（略密 = 略偏多估镜数 = 拆得更保险）。
#: 组合密度 = 26 字/镜 ÷ 7.5 秒/镜 ≈ **3.47 字/秒**，比实测 3.9 字/秒保守约 11%，
#: 确保「预估秒数只会偏高、拆集只会偏多」，绝不会拆不够（拆不够会超出 3 分钟上限）。
try:
    EPISODE_PLAN_CHARS_PER_SHOT = _env_pos_int(
        "MJSCXT_EPISODE_PLAN_CHARS_PER_SHOT", 26, floor=1)
except (TypeError, ValueError):
    EPISODE_PLAN_CHARS_PER_SHOT = 26

SHOT_FIELDS_DEFAULT = {
    "episode": 1,
    "duration": 5,
    "camera": "中景",
    "location": "",
    "scene_lighting": "",
    "description": "",
    "dialogue": [],
    "dialogue_text": "",
    "emotion": "平静",
    "edit_reason": "",
    "audio_cues": "",
    "prompt_h3": "",
    "characters_in_shot": [],
    "items_in_shot": [],
    # P0-1：分镜「首帧/末帧/运动」三段结构（借鉴 ViMax decompose_visual_description）。
    # 模型逐镜把单段画面描述拆成「运动起点→终点→运动类型」，下游 build_storyboard_prompt
    # 据此给模型显式锚点，减少动作画崩。旧剧本无此字段 → 空串，下游回落单段 description。
    # A1：景别/运镜**拆成两个独立权威字段**（借鉴 ArcReel shot_type + camera_motion）。
    # 历史坑：camera 是「景别+运镜」复合字符串（如「特写推入」），下游只能靠 camera_key()
    # 事后解析，猜错会**同时污染生成端与质检端**（曾让分镜质检通过率掉到 57%）。
    # 新剧本优先读这两个字段；旧剧本两字段为空 → 下游回退解析 camera，零回归。
    "shot_type": "",
    "camera_motion": "",
    "first_frame": "",
    "last_frame": "",
    "motion": "",
    # 2026-10-01：**显式站位 + 动作 beat** —— 解决「3D 导演台没料可控」。
    # 此前舞台只由「出场顺序 + 人数 + 景别」推导，同角色同景别的镜头恒得同一张基准图，
    # 实测 8 个镜头折叠成 2 种舞台（shot 2/3/4 计划哈希完全相同）。
    # te_3d_director 读这两个字段；blocking 缺省或名字不匹配 → 逐字回退旧的按序排布，
    # 因此老剧本零影响。取值：
    #   x     ∈ left / center / right  （画面左 / 中 / 右）
    #   depth ∈ front / mid / back     （离镜头近 / 中 / 远）
    #   facing∈ camera / left / right / back（面朝镜头 / 画面左 / 画面右 / 背对）；留空 = 向内稍转
    "blocking": [],
    "action": "",
}

#: **已废弃分镜字段登记表**（结构化，替代注释里的君子协定）。
#: 键 = 字段名；值中的 ``deprecated=True`` 供代码/回归脚本做机器可判定的守卫。
#: 口径：这些字段**不得再写入剧本**，``_norm_shots`` 的字段白名单在标准化时会显式丢弃
#: 模型越界输出的对应字段并留痕；读取侧（coverage / h3_prompt_kit / tts_client）仅为兼容
#: 旧剧本做 legacy 记账，是唯一的合法消费方。
DEPRECATED_SHOT_FIELDS = {
    "narration": {
        "deprecated": True,
        "since": "2026-09-19",
        "reason": (
            "旁白通道已关闭（2026-09-19 产品决策）：剧本阶段不写、配音链路不念、成片不产出旁白。"
            "历史缺陷：narration 曾被当成「心理活动 + 背景补叙 + 环境描写」的公共出口，"
            "实测 ep04 旁白 2231 字 ≈ 496 秒铺在 100 秒画面上（4.93x 溢出），尾部被成片 -shortest "
            "静默截断。现在 _norm_shots 不再透传模型越界输出的 narration，"
            "保证「成片无旁白」是硬不变量；旧剧本残留字段由读取侧按需兼容。"
        ),
    },
}

SYSTEM_BIBLE = ("你是资深漫剧编剧与 AI 绘画提示词工程师，精通把长篇小说改编成可拍摄的漫剧分镜脚本，"
                "并输出严格合法的 JSON。严禁在 content 中输出任何思考过程、英文推理、分析或解释文字，"
                "只允许输出一个可被 json.loads 直接解析的 JSON 对象。")

# ===================== 全量覆盖策略常量（改编 ≠ 缩写，严禁删减原文） =====================

#: 每个镜头承载的原文字数基准（镜头数随内容体量自动扩展）——
#: ⚠️ 这是**镜头数规划的唯一杠杆**，它同时喂给：分块写分镜的目标镜数
#: （`convert_chapter_to_script` 的 `per_chunk`）、`coverage` 与 `script_consistency`
#: 的「该块应写几镜」判据、以及 metadata 的 `chars_per_shot`。
#:
#: 【2026-10-06 用户口径：减镜增时长 → 120 改 240】
#:   实测（桌面版《进境》第 1 集）：文学剧本 4617 字 / 2 个子块，按 120 字/镜算出
#:   `estimated_shots = 38`，模型产出约 40 镜，再叠加「补局部插入镜 16 个」→ 落盘 56 镜 /
#:   平均 2.82 秒。用户口径要「每集 20~30 镜、每镜 5~6 秒」，故每镜承载量翻倍：
#:   4617 / 240 ≈ 19 镜/集 → 模型按此产出 ~20~25 镜，正好落进目标区间。
#:
#: ⚠️ 与 `EPISODE_PLAN_CHARS_PER_SHOT=26`（实测密度，拆集规划用）**是两回事**：
#:   那个是「已知模型会超产」时的**规划口径**；这个是**下给模型的引导口径**。
#:   历史注释：早期实测模型产出是引导值的 4 倍，故两者差一个量级；本仓库当前
#:   实测（2026-10-05）模型**基本照办**（目标 38 → 产出 40），故这里如实按目标值设定。
#: 回滚：`MJSCXT_CHARS_PER_SHOT=120`。
CHARS_PER_SHOT = _env_pos_int("MJSCXT_CHARS_PER_SHOT", 240, floor=30)
SHOTS_PER_CHUNK_MIN = 6       # 单块分镜数下限（再短的块也至少这么多镜）

# ---- 历史资源红线（2026-09-24 实测；**2026-10-09 起不再是单集上限**）----
# ⚠️ 背景：整集视频走 H3 连续工作流（84 段一个 prompt，全程约 7 小时）。实测渲染到
# **第 78 段**时 ComfyUI 崩溃：
#     aimdo: xfer_file_read_at: GetOverlappedResult failed error=1450
#     RuntimeError: HostBuffer.read_file_slice failed
# error=1450 = Windows ERROR_NO_SYSTEM_RESOURCES —— 长跑把系统资源（尤其是 ComfyUI 默认
# pin 住的 ram*0.40 ≈ 12.9GB 锁定页）耗尽，异步重叠 I/O 读不进模型权重。
# 根因是**长跑累积**（时间相关，不是段号本身），但表现为「跑到 78 段左右必崩」。
# 配套：ComfyUI 侧可加 --disable-pinned-memory 进一步降低资源压力（见项目记忆）。
#
# ⚠️ 2026-10-09 用户口径变更：**单集分镜数不再设上限**。
#   原因：生产已从「整集一次连续渲染」改为「**按场次生产**」——每场单独提交渲染、最后拼接成片，
#   上面那条「整集长跑到 78 段左右崩」的资源红线不再适用于单集口径。
#   处置：剧本阶段不再收紧每块镜头额度、也不再对最终 shots 做硬截断（见下方两处 2026-10-09 注释）。
#   本常量保留为**诊断参考线**（over_redline 告警仍在用），但**不再是任何截断依据**。
MAX_SHOTS_PER_EPISODE = 78

# ---- 单块镜头数上限（LLM 响应体红线，2026-09-25 实测）----
# ⚠️ 与 MAX_SHOTS_PER_EPISODE 是**两条独立的红线**，别混：
#   · MAX_SHOTS_PER_EPISODE = H3 连续渲染的资源红线（78 段会崩，见上）；
#   · 本常量 = **单次 LLM 调用的响应体红线**。
# ⚠️ 背景（实测）：一次调用要出 49 镜时，agnes 网关 ReadTimeout ——
#     ReadTimeout: host='api.agnes-ai.cn' read timeout=1200
#     等了整整 20 分钟读不完响应，整块失败。
# 根因：`CHARS_PER_SHOT=120` + `CHUNK_CHARS=3000` → 一块 ~25 镜；两章聚合即 49 镜。
# 而 `shots_cap = min(120, target*2+3)` 让模型**被允许**输出到 101 镜 → 响应体更大。
# 处置：单块目标镜数超过本阈值时，**先二分再送模型**（原文字字不丢，只是拆成多次调用）。
# 之所以要「预劈半」而不是「等出错再二分」：劈半原本只挂在 LLMTruncatedError 上，
# 而超时抛的是 LLMError/LLMGatewayUnavailable → **不触发劈半**，直接整块失败（本次即此坑）。
# 设为 0 = 关闭（退回旧行为，只靠截断兜底）。
try:
    MAX_SHOTS_PER_CHUNK = max(0, int(os.environ.get("MJSCXT_MAX_SHOTS_PER_CHUNK", "24") or 24))
except (TypeError, ValueError):
    MAX_SHOTS_PER_CHUNK = 24

# ---- 每章**至少**拆成的集数（默认 1 = 不强制拆，纯按内容判断）----
# ⚠️ 2026-09-25 策略变更：由「一章固定拆 2 集」改为**纯按内容自动判断**。
#
# 旧行为（=2）：MAX_SHOTS_PER_EPISODE 只是「兜底硬上限」，章节短时它根本不触发，
#   所以曾再加一条固定份数规则强行拆开。实测后果：本项目 42 章平均 1766 字、
#   预估 15 镜/章，**远低于 78 镜上限**，却在旧规则下被硬拆成 2 集 ——
#   每集只剩 7~8 镜（约 35 秒），集与集之间还在句中断开，
#   既不是「一集」该有的体量，也破坏了叙事完整性（用户实测反馈）。
#
# 新行为（=1）：集数**完全由内容体量决定** ——
#   `parts = ceil(预估镜头数 / MAX_SHOTS_PER_EPISODE)`，
#   只有预估镜头数真的超过单集硬上限时才拆，且拆出的每段都不超上限。
#   这与业界漫剧/短剧项目的通行做法一致（一集 = 一个完整叙事单元，
#   约 1.5~3 分钟；内容不够就不硬凑集数）。
#
# 本常量现在退化为**「每章至少拆 N 集」的下限**，仅供需要「无论长短都拆」的场景
# 通过 env 显式开启（MJSCXT_EPISODES_PER_CHAPTER=2）。
# 取更碎的那个：parts = max(本下限, 内容算出的份数)，因此调大它只可能更碎，不会更碎不动。
try:
    EPISODES_PER_CHAPTER = max(1, int(os.environ.get("MJSCXT_EPISODES_PER_CHAPTER", "1") or 1))
except (TypeError, ValueError):
    EPISODES_PER_CHAPTER = 1

# ---- 分镜阶段的 token 预算（必须给「思考」留预留量）----
# ⚠️ always-on reasoning 模型（agnes-3.0-flash / GLM 系）在写分镜前会先输出一大段思考，
# 实测该任务的思考量 ≈16K token。若 max_tokens 低于思考量，模型会「只吐思考、正文为空」，
# 表现为整集卡死（2026-09-23 端到端复测的核心阻塞）。此前按 shots_target*300+1200 给
# （12 镜 → 4800）远低于水位，故改为「固定思考预留 + 每镜正文额度」。
SHOTS_THINKING_RESERVE = 16384   # 思考预留（与 llm_client.REASONING_ONLY_TOKEN_FLOOR 对齐）
SHOTS_TOKENS_PER_SHOT = 300      # 单镜正文额度（description+visual_detail+dialogue+audio_cues 实测够用）

# ===================== 提炼 / 分镜阶段的断点缓存（对抗网关偶发挂起） =====================
# 背景（2026-09-24 实测）：网关上游偶发挂起 —— 连 max_tokens=3600 的小请求也会挂满
# 1200s read timeout（而同一时刻 16384 额度的大请求 258s 就返回了），说明**与请求体量无关**。
# 而本阶段单个请求体量本来就大（思考预留 16384 + 每镜 300），一集完整生成要 20+ 分钟；
# pipeline 的重试又是**整个 script 步骤重来**（`_run_step_with_retry`）——没有缓存时
# 每次重试都要把 提炼 + 设定 + 全部分镜 重新跑一遍，网关一抖就永远跑不完。
#
# 这里按「prompt 内容指纹」做**内容寻址**落盘，重跑时命中即跳过模型调用：
#   - 任意输入（原文 / 镜头额度 / 风格 / 提示词模板本身）变化 → 指纹随之变化 → 自动失效，
#     绝不会用旧结果冒充新结果（把 prompt 整体入指纹，是防止「改了提示词却命中旧缓存」的关键）；
#   - 网关抖动的净效果从「永远跑不完」变成「多跑几次总能跑完」。
# 缓存只写不读回主链路语义 —— 未命中时行为与加缓存前**完全一致**。
# 用 MJSCXT_SHOTS_CACHE=0 可关闭（离线测试 / 需要强制重新生成时）。
SHOTS_CACHE_ENV = "MJSCXT_SHOTS_CACHE"


def _shots_cache_enabled() -> bool:
    """缓存开关：默认开启，环境变量置 0/false/no/off 时关闭。"""
    return str(os.environ.get(SHOTS_CACHE_ENV, "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _cache_file(cache_dir: str, kind: str, prompt: str) -> str:
    """内容寻址缓存路径：文件名 = sha1(kind + prompt) 前 20 位。"""
    digest = hashlib.sha1(f"{kind}\x00{prompt}".encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir, f"{kind}_{digest}.json")


def _shots_cache_root(continuity_dir: str, key: str) -> str:
    """**整本路径**的断点缓存目录（``<continuity>/<项目>/shots_cache/whole``）。

    ⚠️ 与「按章分集」路径的 ``continuity.shots_cache_dir(...)``（``.../shots_cache/epNN``）
    刻意分开放：两条路径的 prompt 体量、块划分、shots_target 完全不同，
    混在一个目录里虽然因内容寻址不会串味，但排查「这次为什么没命中」时无法区分来源。

    缓存是纯加速手段：目录不可用（权限/磁盘）时返回 ""，调用方按「不落缓存」继续跑，
    **绝不让缓存问题阻断主链路**（与 `_shots_cache_enabled` 的取舍一致）。
    """
    if not continuity_dir or not key:
        return ""
    try:
        root = os.path.join(continuity_dir, safe_project_name(key), "shots_cache", "whole")
        os.makedirs(root, exist_ok=True)
        return root
    except Exception as e:  # noqa: BLE001
        logger.warning(f"整本路径缓存目录不可用（本次不落缓存）：{e}")
        return ""


def _cache_read(path: str):
    """读缓存：不存在 / 损坏 / 非 dict 一律当作**未命中**。

    缓存是「只读加速视图」，不是主链路状态 —— 因此损坏时按未命中重新生成，
    而不是 fail-loud 中断整集（与 `fs_atomic` 对主链路文件的 fail-loud 口径区分开）。
    """
    try:
        data = fs_atomic.read_json_strict(path, None)
    except Exception as e:  # noqa: BLE001 —— 缓存损坏仅降级为未命中
        logger.warning(f"提炼/分镜缓存不可用，按未命中重新生成：{os.path.basename(path)}：{e}")
        return None
    return data if isinstance(data, dict) else None


def _cache_write(path: str, payload: dict) -> None:
    """写缓存：失败只告警，绝不影响本次产出（缓存是加速手段，不是必需产物）。"""
    try:
        fs_atomic.atomic_write_json(path, payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"写提炼/分镜缓存失败（忽略，不影响本次产出）：{e}")


def _cache_get(cache_dir: str, kind: str, prompt: str, events: list, label: str):
    """命中则返回缓存 dict，否则 None（未命中不产生任何副作用）。"""
    if not cache_dir or not _shots_cache_enabled():
        return None
    path = _cache_file(cache_dir, kind, prompt)
    hit = _cache_read(path)
    if hit is None:
        return None
    logger.info(f"{label} 命中断点缓存，跳过模型调用：{os.path.basename(path)}")
    if events is not None:
        events.append({"label": label, "event": "cache_hit", "kind": kind})
    return hit


def _cache_put(cache_dir: str, kind: str, prompt: str, payload: dict) -> None:
    if not cache_dir or not _shots_cache_enabled():
        return
    _cache_write(_cache_file(cache_dir, kind, prompt), payload)


# 2026-10-07 提示词外置（app/prompts/script_rewrite_rules.txt）：常量改名保留为**代码内兜底**；
# 实际生效值在常量定义之后由 prompt_templates.load("script_rewrite_rules") 加载
# （用户覆盖 > 出厂默认 > 本常量）。已核实本常量无任何运行时改写（全文件仅此一处赋值）。
_DEFAULT_REWRITE_RULES = (
    "【改写规则（这是压缩提炼，不是逐句照搬：只保留推动剧情的信息，删掉纯背景铺陈）】\n"
    "1) 先提炼：把本段原文压缩成 3~5 句剧情梗概，只保留「冲突、转折、关键动作、金句」四类信息；"
    "世界观、势力背景、环境补叙、器物来历等**不推进剧情**的描写，一律不逐句复述、不单独成镜；\n"
    "2) 再落镜：把梗概里的关键情节改写成镜头。心理活动→可拍的表情/动作或该角色第一人称自语，"
    "叙述→画面动作，环境→画面与音效；**禁止**把第三人称背景补叙、世界观说明原样写进 description 当画面；\n"
    "3) 【一镜一动作·信息密度】每个镜头只承载 **1 个核心动作/信息**（人物动作 / 台词 / 关键环境 三选一）；"
    "一段需要连续完成 2 个以上动作时，**按动作先后拆成相邻两镜**，每镜只推进一个动作——"
    "参考片 92 镜几乎每镜只有一个动作节拍，靠相邻镜切换推进，而不是靠一镜内堆叠多个动作；"
    "一条信息用一个画面能讲清的，绝不拆成两镜；**纯环境空镜**（无人、无动作、无信息推进）禁止生成；\n"
    "4) 【描述/对白比】剧情主要由人物台词与动作推进，画面描述只做必要补充："
    "全块所有镜头 description+visual_detail 的合计字数不得超过 dialogue 合计字数的 3 倍；"
    "无台词的镜头必须有明确的人物动作或情绪变化，禁止写成静态环境铺陈；\n"
    "5) 原文对话尽量原样写进对应角色的 dialogue.text，禁止改写成概括式引述；\n"
    "6) 镜头按原文时间顺序排列，块首镜头自然衔接上一块结尾，不得跳段、不得重复；\n"
    "7) 【本系统不产出旁白】成片没有画外音解说：背景补叙、环境描写一律靠画面承载（且只保留推进剧情的部分），"
    "心理活动靠神态动作或第一人称角色自语承载；**禁止**把第三人称叙述/背景补叙硬转成角色开口的台词——"
    "dialogue 只承载两种内容：原文的对话，以及原文明确的心理活动/独白改写的**第一人称**角色自语。"
    "判断标准：这句话由该角色以第一人称自然说出才可进 dialogue；"
    "全知视角的交代句（世界观、势力背景、环境说明、「XX大陆人人习武」这类）"
    "只能写进 description / audio_cues（且仅保留推进剧情的部分），绝不进 dialogue；\n"
    "8) 自检：写完一块后回看梗概，确认每个关键情节都有对应镜头；"
    "纯背景补叙、纯环境描写若未推进剧情，应当已删去，**不要求「逐句覆盖原文」**。\n"
    "9) 【镜头语言克制·对齐参考片 92 镜实测】运镜以固定为主（约 75%），"
    "推/拉/摇/手持/跟随仅在情绪递进或空间转换时用，且用「轻推 / 轻摇 / 跟随 / 轻手持」这类克制写法"
    "（参考片非固定运镜全部是这 4 种，从不使用推镜/拉镜/摇镜/移镜/升降/环绕/变焦/定格）；"
    "景别三足鼎立：近景 / 中近景 / 局部 各约 25%（合计约 76%）——「局部」= 只拍手部/道具/身体局部、"
    "不出现完整人脸的插入镜，参考片占比 25%，用于把叙事压到道具上并规避人脸崩坏；"
    "中景 约 17%（空间与关系交代，是运镜主力）；全景与特写只做情绪锚点（各 ≤ 3%），大特写/远景/大远景不用；"
    "越近越静——特写 100% 固定、局部与中近景基本固定，运镜多长在中景；"
    "每个镜头的 edit_reason 字段写一句**具体**的剪辑动机（20~30 字，说明「为什么切到这一镜 / 承担什么叙事功能」，"
    "如「切掉环境只留下他的反应」「用空间拉开取代告别对白」「物件回环把十年压缩到一张纸上」），"
    "不解释给观众，是给构图与取舍的依据。\n"
    "10) 【视觉锚点/道具回环】识别原文中反复出现的关键道具（如印章、信物、武器、食物、书信等），"
    "把它作为跨镜头视觉锚点：每次该道具出现时，在 description 的构图描述中明确写道具"
    "在画面中的位置与状态变化（如「纸币被折叠/展开/递出/攥紧」），"
    "让道具成为观众追踪情感或信任弧线的视觉线索；"
    "同一道具全段出现 ≥ 2 次时，在首次出现的镜头 description 末尾加「（视觉锚点）」标注，"
    "后续每次出现的镜头 description 末尾加「（视觉锚点·第 N 次）」，N 从 2 起算。\n"
    "11) 【节拍识别·节奏分层】先判断本段属于哪种叙事节拍，再把节拍落成镜头切分与时长：\n"
    "    · 节拍四段：开场（建立情境/人物/悬念）→ 触发（矛盾出现/目标确立）→ 高潮（冲突爆发/反转/关键动作，"
    "      全段情绪与张力的最高点）→ 收尾（结果落地/钩子留白/接下一段）；本段不一定四段齐全，按原文实际节奏取舍。\n"
    "    · 节拍边界**强制切段**：从一个节拍切换到下一个节拍（尤其进入/离开「高潮」）处，"
    "      镜头边界必须落在节拍切换点上——绝不让「高潮爆发」与「收尾」挤在同一镜内，"
    "      也不让铺垫（开场/触发）与高潮并镜。\n"
    "    · 爆点镜时长加成：处于「高潮」节拍的镜头（冲突爆发/反转/关键动作/金句落点）"
    "      应取该档位**上限附近**的时长（节奏上需要停留，让观众看清动作与反应）；"
    "      「触发/开场/收尾」等过渡与铺垫镜则取较短时长，快切推进。"
    "      具体档位由程序按本镜内容自动估算，你只需把高潮镜的画面信息写足、把过渡镜写紧凑。\n"
    "12) 【镜头语言强化·画面锚定】（写剧本层把镜头语言克制与连续性锚点内化，零额外成本：）\n"
    "    · ①画面位置与朝向：每个镜头 description 写明主体在画面中的位置（左/中/右、前/后景）与"
    "      面向（角色面向镜头/背向/侧向；双人/多人在画面里的相对空间关系，谁近谁远、谁左谁右），"
    "      让构图有明确锚点，不把位置留给模型猜；\n"
    "    · ②聚焦身体部位：特写/近景镜头要写明**聚焦哪个身体部位或物件**（如「特写握紧的拳头」「"
    "      特写低垂的眼眸」「近景袖口滑落的手」），不写「特写某人」这种无部位的虚指；\n"
    "    · ③新场景首镜建场景：每当 location 切到一个新场景，该场景的**第一个镜头**用最宽的"
    "      适用景别（全景/远景优先）建立空间关系（谁在哪里、空间多大、主光源方向），"
    "      之后该场景内的镜头才收进中景/近景推进剧情；例外——开场即冲突爆发/特写情绪爆点的段落，"
    "      允许首镜直接用中近景或特写，不硬套最宽景别（该例外不受规则9「全景≤3%」额度约束）；\n"
    "    · ④机位克制：同一场景内的连续镜头**尽量保持机位统一**（同机位/同视角切换构图），"
    "      只在节拍切换或空间转换时才换机位，减少无意义机位跳变（承接规则9「运镜以固定为主」）；\n"
    "    · ⑤画面变化幅度自分级：写镜头时**自行**判断本镜画面内容相对上一镜的变化幅度，"
    "      并在 description/visual_detail 里**体现**这种幅度——按「起点 → 关键动作节点 → 结果」写，"
    "      动作过程写**关键动作分解**（用→连接的 2~4 步，如：走到椅前→扶椅背转身→缓缓落座），"
    "      只写关键节点、不写琐碎中间步（见「描述粒度口径」段）：大幅变化（场景切换/冲突爆发）"
    "      写「切换后空间里的新主体与结果态」、中幅变化（动作推进）写**关键动作分解**、"
    "      小幅变化（表情微变/道具细节）只写变化点。让相邻镜的画面递进**可追踪**，"
    "      变化幅度与本镜叙事节拍（规则11）节奏一致：高潮镜允许大幅、过渡镜用小幅快切。"
    "      琐碎中间步留给视频/九宫格阶段按时间切片完成，剧本层只写 2~4 个关键节点，"
    "      写满琐碎中间步反而导致九宫格雷同。\n"
    "13) 【台词长度·切镜节奏的总开关｜权重高于规则 9 的镜头语言】参考片单镜中位时长只有 2.08 秒，"
    "它的快节奏**不是靠运镜、是靠「一句短台词说完就切」**。换算成台词：一镜台词以 8~14 字为宜"
    "（按中文语速≈5 字/秒，对应 2~3 秒），**上限 16 字（与 config.SHOT_SPEECH_BUDGET_CHARS 一致）**。"
    "禁止一镜塞一句长台词（30 字以上）——"
    "本系统旧剧本实测单镜台词中位要 8 秒才念完，镜头被迫拉长到 8~12 秒，成片观感就是"
    "「一个分镜演半天不切换」。因此：\n"
    "    · 长台词必须按**停顿/语义换气点**拆成相邻多镜，一句一镜（问句一镜、答句一镜、"
    "      金句单独一镜、对方反应单独一镜）；同一句话也可在停顿处切开、后半镜把景别推近一档"
    "      （参考片「同一句话换景别」切法）；\n"
    "    · 拆出的镜头**各自都要有独立画面内容**（说话人换景别 / 对方的反应 / 手部动作），"
    "      不允许把同一画面重复两遍充数；\n"
    "    · 无台词的镜头靠动作与反应推进，同样保持短促（2~4 秒），不要写成静态长镜。\n"
)

# 2026-10-07 提示词外置：实际生效 = app/prompts/script_rewrite_rules.txt（可被
# PROJECT_DATA_DIR/prompt_overrides/script_rewrite_rules.txt 覆盖）；任一级读失败回落
# 上方 _DEFAULT_REWRITE_RULES，行为与外置前逐字一致（load 已剥离模板头注释）。
# 同时把兜底注册进 loader：默认文件丢失时 load() 仍能拿到本常量原文。
REWRITE_RULES = prompt_templates.load("script_rewrite_rules") or _DEFAULT_REWRITE_RULES
prompt_templates.register_fallback("script_rewrite_rules", _DEFAULT_REWRITE_RULES)


# ===================== 分块与全量覆盖 =====================

def build_chunks(text: str, chapters: list, chunk_chars: int = CHUNK_CHARS) -> list:
    """把全文切成若干块（优先按章节边界聚合）"""
    text = text or ""
    if not text.strip():
        return []

    chunks = []
    if chapters:
        buf_text, buf_start, buf_chars, from_ch, to_ch = [], None, 0, None, None
        _first_start = int(chapters[0].get("start") or 0) if chapters else 0
        for _ci, ch in enumerate(chapters):
            _seg_start = ch["start"]
            if _ci == 0 and _first_start > 0 and text[:_first_start].strip():
                # 审计 P2-15（2026-09-29）：第一个章节标记之前的开篇正文（无
                # 「序章/楔子」标题的简介、开篇白）不属于任何章区间 —— 旧实现直接
                # 从第一章 start 聚合，这段正文被静默丢弃（与「零丢弃」不变量相悖，
                # 仅靠覆盖率补生成部分兜回）。并入首块一起送模型。
                _seg_start = 0
            seg = text[_seg_start:ch["end"]]
            if buf_start is None:
                buf_start, from_ch = ch["start"], ch["index"]
            buf_text.append(seg)
            buf_chars += len(seg)
            to_ch = ch["index"]
            if buf_chars >= chunk_chars:
                chunks.append({
                    "text": "".join(buf_text).strip(),
                    "char_count": buf_chars,
                    "from_chapter": from_ch,
                    "to_chapter": to_ch,
                })
                buf_text, buf_start, buf_chars = [], None, 0
        if buf_text and buf_chars > 0:
            chunks.append({
                "text": "".join(buf_text).strip(),
                "char_count": buf_chars,
                "from_chapter": from_ch,
                "to_chapter": to_ch,
            })
    else:
        paras = [p for p in re.split(r"\n{1,}", text)]
        buf, buf_chars = [], 0
        for p in paras:
            buf.append(p)
            buf_chars += len(p) + 1
            if buf_chars >= chunk_chars:
                chunks.append({"text": "\n".join(buf).strip(), "char_count": buf_chars,
                               "from_chapter": None, "to_chapter": None})
                buf, buf_chars = [], 0
        if buf:
            chunks.append({"text": "\n".join(buf).strip(), "char_count": buf_chars,
                           "from_chapter": None, "to_chapter": None})

    # 零丢弃：过短的块并入相邻块（原实现用过滤丢弃，会把整章/整段正文静默舍弃）
    if len(chunks) >= 2:
        merged = []
        for c in chunks:
            if merged and len(c["text"]) < MIN_CHUNK_CHARS:
                prev = merged[-1]
                prev["text"] = (prev["text"] + "\n" + c["text"]).strip()
                prev["char_count"] += c["char_count"]
                prev["to_chapter"] = c.get("to_chapter") or prev.get("to_chapter")
            else:
                merged.append(c)
        if len(merged) >= 2 and len(merged[0]["text"]) < MIN_CHUNK_CHARS:
            # 首块过短：并入后一块，保证正文开头不被丢弃
            merged[1]["text"] = (merged[0]["text"] + "\n" + merged[1]["text"]).strip()
            merged[1]["char_count"] += merged[0]["char_count"]
            merged[1]["from_chapter"] = (merged[0].get("from_chapter")
                                         or merged[1].get("from_chapter"))
            merged.pop(0)
        chunks = merged
    for i, c in enumerate(chunks):
        c["index"] = i + 1
        c["total"] = len(chunks)
        c["title"] = _chunk_title(c)
    return chunks


def _chunk_title(c: dict) -> str:
    first = (c.get("text") or "").split("\n")[0].strip()
    if c.get("from_chapter"):
        if c["from_chapter"] == c.get("to_chapter"):
            return f"第{c['from_chapter']}章"
        return f"第{c['from_chapter']}-{c['to_chapter']}章"
    return first[:24] or f"第{c['index']}块"


# ===================== 三段式生成 =====================

def _as_dict(data) -> dict:
    """模型返回容错：兼容部分模型把 JSON 对象包在数组里返回（[{"...": ...}]）的情况。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                return it
    return {}


def _bare_list_to_bible(rows) -> dict:
    """模型只返回了某个数组（未包成完整对象）时的兜底归位。

    按首元素的字段特征判断该数组属于 characters / items / scenes 中的哪一类，
    避免整链路因为“少了一层对象”而中断。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {}
    sample = rows[0]
    if any(k in sample for k in ("category", "owner")):
        return {"items": rows}
    if "location" in sample and "appearance" in sample:
        return {"scenes": rows}
    return {"characters": rows}


def _normalize_bible(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return _bare_list_to_bible(raw)
    return {}


# ===================== 截断自适应（A 项③） =====================
# 单块送模型时若输出被截断（finish_reason=length），除了自动提高 max_tokens 重试外，
# 仍失败则把该块再二分（最多 CHAPTER_SPLIT_MAX_DEPTH 层）后分别生成再合并，保证不静默失败。
CHAPTER_SPLIT_MAX_DEPTH = 3
CHAPTER_SPLIT_MIN_CHARS = 400     # 低于该字数的子块不再继续二分（避免无意义碎片）


def _robust_json(client, prompt: str, system: str, temperature: float, max_tokens: int,
                 events: list = None, label: str = "",
                 max_attempts: int = 3, token_ladder=None) -> dict:
    """统一 JSON 调用入口：走 chat_json_robust（截断自动提额重试）并记录重试事件

    max_attempts / token_ladder 可按阶段覆盖默认重试策略（如 bible 汇总阶段输出更长，
    需要更高的提额上限与更多尝试次数）。
    """
    robust = getattr(client, "chat_json_robust", None)
    if robust is None:                      # 兼容旧客户端
        return client.chat_json(prompt, system=system, temperature=temperature,
                                max_tokens=max_tokens)
    kw = {}
    if token_ladder:
        kw["token_ladder"] = tuple(token_ladder)
    if max_attempts:
        kw["max_attempts"] = int(max_attempts)

    def _on_event(h):
        if events is not None and int(h.get("attempt") or 1) > 1:
            events.append({"label": label, "attempt": h.get("attempt"),
                           "max_tokens": h.get("max_tokens"),
                           "finish_reason": h.get("finish_reason"),
                           "truncated": bool(h.get("truncated"))})

    try:
        return robust(prompt, system=system, temperature=temperature,
                      max_tokens=max_tokens, on_event=_on_event, **kw)
    finally:
        meta = getattr(client, "last_json_meta", None)
        if events is not None and isinstance(meta, dict) and int(meta.get("attempts") or 0) > 1:
            seen = {(e.get("label"), e.get("attempt")) for e in events}
            if (label, meta.get("attempts")) not in seen:
                events.append({"label": label, "attempt": meta.get("attempts"),
                               "max_tokens": meta.get("max_tokens"),
                               "finish_reason": meta.get("finish_reason"),
                               "truncated": bool(meta.get("truncated"))})


def _gateway_down_fallback(e, chunk: dict, per_chunk: int, bible: dict,
                           produced: list) -> list:
    """「LLM 网关不可用」时的统一处置（不是普通的单块失败）

    实测教训：网关上游没算力时，每一块都会失败 → 每块都按原文兜底 →
    整集 6/6 兜底，脚本却仍记成「生成成功」。这种剧本没有分镜设计、没有台词，
    配音链路读不到 dialogue 就只出 1 句，用户根本无从判断哪一镜有问题。
    所以分两种情况：
    - **还没产出任何真实镜头** → 直接失败。产出兜底剧本比报错更糟。
    - **已有真实产出** → 剩余块兜底，保住已完成部分，并在 warnings 里如实标注降级。
    """
    if not produced:
        hint = getattr(e, "hint", "") or "请到「AI 设置」检查 base_url / model 是否可用"
        raise LLMError(
            "LLM 网关不可用，已中止生成：继续下去只会得到一份「无分镜、无台词」的"
            f"原文兜底剧本（该剧本配音只能出 0 句）。原因：{e}　处置建议：{hint}"
        ) from e
    return _fallback_shots_for_chunk(chunk, per_chunk, bible)


def _gateway_down_warning(chunk: dict, n_fb: int) -> str:
    return (f"⚠ LLM 网关不可用：第 {chunk['index']} 块已按原文兜底生成 {n_fb} 镜。"
            "本集为**降级产出**（该块无分镜设计、无台词），网关恢复后建议重跑本集。")


def _split_chunk_in_half(chunk: dict) -> list:
    """把子块按中点附近的句末标点一分为二（保证不丢字），用于截断后的进一步切分"""
    text = str(chunk.get("text") or "")
    if len(text) < CHAPTER_SPLIT_MIN_CHARS * 2:
        return []
    mid = len(text) // 2
    cut = mid
    for m in re.finditer(r"[。！？；!?;\n]", text):
        if m.end() >= mid:
            cut = m.end()
            break
    parts = [text[:cut].strip(), text[cut:].strip()]
    out = []
    for i, p in enumerate(parts):
        if len(p) < CHAPTER_SPLIT_MIN_CHARS // 2:
            continue
        out.append({**chunk, "text": p, "char_count": len(p),
                    "title": f"{chunk.get('title') or '块'}·{i + 1}",
                    "_split_from": chunk.get("title")})
    return out if len(out) == 2 else []


def _merge_outlines(outlines: list, chunk: dict) -> dict:
    """把同一原始子块的多个子-提炼结果合并为一个 outline（人物/物品/场景按名去重）"""
    merged = {"summary": "", "characters": [], "items": [], "scenes": [], "key_beats": []}
    summaries = []
    seen = {k: set() for k in ("characters", "items", "scenes")}
    for o in outlines:
        if not isinstance(o, dict):
            continue
        if o.get("summary"):
            summaries.append(str(o["summary"]))
        for key in ("characters", "items", "scenes"):
            for row in (o.get(key) or []):
                if not isinstance(row, dict):
                    continue
                nm = str(row.get("name") or "").strip()
                if not nm or nm in seen[key]:
                    continue
                seen[key].add(nm)
                merged[key].append(row)
        for b in (o.get("key_beats") or []):
            if str(b).strip():
                merged["key_beats"].append(str(b))
    merged["summary"] = "；".join(summaries)[:200]
    # 全量覆盖：情节要点不再压到 5 条（避免把长块内容压缩丢失），容纳整段全部节点
    merged["key_beats"] = merged["key_beats"][:14]
    merged["_chunk"] = {"index": chunk.get("index"), "title": chunk.get("title"),
                        "char_count": len(str(chunk.get("text") or "")), "sub_split": True}
    return merged


def extract_chunk_outline(client, chunk: dict, novel_title: str,
                          events: list = None, depth: int = 0,
                          cache_dir: str = "") -> dict:
    """① 单块提炼（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    cache_dir 非空时启用断点缓存：命中即跳过模型调用（见文件上方缓存说明）。
    """
    body = chunk["text"][:MAX_CHARS_PER_CHUNK_PROMPT]
    prompt = f"""【任务】下面是长篇小说《{novel_title}》的第 {chunk['index']}/{chunk['total']} 段原文（{chunk.get('title')}，约 {len(body)} 字），请提炼改编漫剧所需的辅助信息（人物 / 物品 / 场景 / 剧情摘要 / 关键情节节点）。本提炼只作分镜阶段的辅助索引：分镜阶段会拿到本段完整原文，因此这里**不需要逐句复述原文**，但也不得删改原意。
【原文开始】
{body}
【原文结束】
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字，结构如下：
{{
  "summary": "本段剧情摘要，120 字以内",
  "characters": [{{"name": "人物名", "role": "主角/配角/反派", "gender": "性别，只填「男」或「女」（必须按原文称谓/代词推断给出）", "appearance": "静态外貌（发色/瞳色/脸型/体格等**不随剧情变化的定妆特征**，**不含会随集变化的服饰/配饰**），35 字以内", "personality": "性格，20 字以内"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观＋**结构三要素**（见下方硬约束），60 字以内", "structure": "**结构三要素**（每项 8 字以内）：①功能端＝哪一端是使用端（吹嘴/刃口/接口/开口）②尾端＝哪一端是尾（系绳/握把/底座）③使用朝向＝用起来时功能端朝哪儿（如「吹嘴贴唇、腔体朝外」）", "importance": "重要/临时。判定三问（任一答案为「是」即判临时、宁缺勿滥）：①删掉它剧情还成立吗？②它只是随手用的日常物品吗？③它只是场景陈设吗？只有推动剧情且后续会反复出现/被反复指认的关键道具才判「重要」"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型", "appearance": "环境特征，30 字以内"}}],
  "key_beats": ["按原文顺序列出本段关键情节节点，每条 30 字以内，最多 12 条（分镜阶段会读取完整原文，这里只做索引，不要逐句复述、不要写成英文）"]
}}
【登记口径·不设数量上限】characters / items / scenes **不设数量上限**：原文登记了多少就报多少（characters 按戏份从大到小排）；items 仍按三问过滤（见上方 importance 判定），临时道具一律不进 items；纯背景路人（无台词、无独立动作、不推进情节，如老人甲、村民若干）不登记；但共同推进情节的一方阵营／敌群／首领或群像（有共同称谓、各自开腔或行动）必须登记——有具名首领就登首领，只有共同称谓的群体就登记为一个群像角色（name 用群体称谓如「正道群敌」，role 标敌方，appearance 写可辨识代表形象）。"""
    label = f"outline#{chunk.get('index')}"
    hit = _cache_get(cache_dir, "outline", prompt, events, label)
    if hit is not None and hit.get("_chunk"):
        return hit
    try:
        data = _as_dict(_robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.35,
                                     max_tokens=3000, events=events, label=label,
                                     max_attempts=3, token_ladder=(4096, 8192, 16384)))
    except LLMTruncatedError:
        subs = _split_chunk_in_half(chunk) if depth < CHAPTER_SPLIT_MAX_DEPTH else []
        if not subs:
            raise LLMTruncatedError(
                f"{label}（{chunk.get('title')}，{len(chunk.get('text') or '')} 字）"
                f"在自动提高 max_tokens 后仍被截断，且已无法继续二分（depth={depth}）") from None
        logger.warning(f"{label} 提炼输出被截断，自动二分为 {len(subs)} 个子块重试（depth={depth}）")
        if events is not None:
            events.append({"label": label, "event": "sub_split", "depth": depth,
                           "parts": [len(s.get("text") or "") for s in subs]})
        return _merge_outlines(
            [extract_chunk_outline(client, s, novel_title, events, depth + 1, cache_dir)
             for s in subs], chunk)
    data["_chunk"] = {
        "index": chunk["index"],
        "title": chunk.get("title"),
        "char_count": len(body),
    }
    _cache_put(cache_dir, "outline", prompt, data)
    return data


def _ctx_block(ctx, key: str) -> str:
    """取跨集连贯性上下文块（A/B/C 方案的 prompt 注入片段），无则返回空串"""
    if not isinstance(ctx, dict):
        return ""
    val = ctx.get(key)
    return str(val).strip() if val else ""


def _ctx_line(ctx, key: str) -> str:
    v = _ctx_block(ctx, key)
    return (v + "\n") if v else ""


def _ctx_asset_names_block(ctx) -> str:
    """P3 名称归一化去重：把「已登记资产名单」投影进 ②汇总 prompt。

    取 ``continuity_ctx['bible']``（项目级设定库，A① 持久化）里已锁定的角色/物品/场景规范名，
    让 LLM 在汇总时**复用这些规范名**、不要另造近名（如已有「古月方源」就别再写「方源」，
    否则下游会重复生成参考图）。纯 prompt-only：不新增 import（``continuity.py`` 已
    ``import novel_to_script``，反向会循环导入），也不动 ``align_script_assets`` 的确定性
    兜底（它仍是最后一道归一闸，本块只是让 LLM 在前置阶段就收敛到规范名）。

    ``ctx`` 为 None / 无 ``bible`` / 名单全空时返回空串 —— 首集（无跨集上下文）行为零变化。
    """
    if not isinstance(ctx, dict):
        return ""
    bible = ctx.get("bible")
    if not isinstance(bible, dict):
        return ""

    def _names(key):
        rows = []
        for r in (bible.get(key) or []):
            if isinstance(r, dict):
                nm = str(r.get("name") or "").strip()
                if nm:
                    rows.append(nm)
        # 2026-09-29 资产不设上限：不再 [:12] 截断 —— 锁定名单截掉的规范名，
        # 模型在汇总阶段就会另造近名 → 下游重复生成参考图
        return rows

    chars, items, scenes = _names("characters"), _names("items"), _names("scenes")
    if not (chars or items or scenes):
        return ""
    lines = ["【已登记资产名单（跨集锁定，必须复用规范名、禁止另造近名）】"]
    if chars:
        lines.append("角色：" + "、".join(chars))
    if items:
        lines.append("物品：" + "、".join(items))
    if scenes:
        lines.append("场景：" + "、".join(scenes))
    lines.append("硬约束：characters[].name / items[].name / scenes[].name 若与上方名单指向同一对象，"
                 "必须逐字复制该规范名（含姓氏/全称，不得写简称或去姓别名）；"
                 "只有原文出现名单之外的确凿新对象时才新增条目，且不得与已有近名重复。")
    return "\n".join(lines)


def _prev_tail_block(prev_chunk: dict, prev_outline: dict) -> str:
    """P1-2 长文「接缝重叠」注入（借鉴 ViMax novel_compressor 的 overlap 思路，
    按本系统架构落地为「集内相邻块接缝上下文」）。

    写第 i 块分镜时，注入**上一块（i-1）的结尾**：已 LLM 提炼好的剧情摘要 + 末 2 条
    情节要点 + 末场景。让 LLM 知道「上段发生到哪了」，使本块开头能**承接**而非
    凭空重启——治「章节衔接硬 / 长文丢主线」（相邻块各自独立写、互不知道对方结尾）。

    纯 prompt-only、零新增 LLM 调用：上块 outline 在 ① 提炼阶段已生成，此处只取用。
    向后兼容：``prev_chunk`` / ``prev_outline`` 为 None（首块 i=0、或递归子块不传）时
    返回空串 → prompt 不出现该段、行为与旧版完全一致。

    关键措辞：这是**衔接锚点，不是重演指令**——显式要求「承接上段结尾，勿重演上段
    已发生的事件」（与「上一集已发生事件禁止重演」同口径，避免 LLM 把上段再写一遍）。
    """
    if not isinstance(prev_chunk, dict) and not isinstance(prev_outline, dict):
        return ""
    lines = []
    summary = str((prev_outline or {}).get("summary") or "").strip()
    beats = list((prev_outline or {}).get("key_beats") or [])
    tail_beats = beats[-2:] if beats else []
    prev_title = str((prev_chunk or {}).get("title") or "上一段")
    if summary or tail_beats:
        lines.append(f"【承接上段（{prev_title}结尾）——只用于让本段开头衔接自然，"
                     "切勿重演上段已发生的事件】")
        if summary:
            lines.append("上段剧情摘要：" + summary)
        if tail_beats:
            lines.append("上段最后情节要点：" + "；".join(
                str(b).strip() for b in tail_beats if str(b).strip()))
        prev_loc = str((prev_chunk or {}).get("to_chapter") or "").strip()
        if prev_loc:
            lines.append("上段所在章节：第" + prev_loc + "章")
    return "\n".join(lines)


def build_bible(client, outlines: list, novel_title: str, style: str, episodes: int,
                target_shots: int, events: list = None, continuity_ctx: dict = None,
                cache_dir: str = "") -> dict:
    """② 汇总全剧设定，产出 A 版 characters/items/scenes

    continuity_ctx 非空时（跨集连贯性方案 A①②③）：注入项目级设定库（角色外观锁定）、
    上集摘要卡与衔接契约，使本集设定与既有跨集设定保持一致。
    """
    digest = []
    for o in outlines:
        if not isinstance(o, dict):
            continue
        # 审计修正（2026-09-29，用户决策：资产不设数量上限）：digest 不再截断
        # characters[:6] / items[:3] / scenes[:6] —— 这里截掉的条目 ②汇总阶段就
        # 看不见，「上游丢的名字下游救不回来」。条目数量由模型侧登记口径约束。
        digest.append({
            "段": o.get("_chunk", {}).get("index"),
            "摘要": o.get("summary", ""),
            "人物": [{"name": c.get("name"), "role": c.get("role"), "gender": c.get("gender"), "appearance": c.get("appearance")}
                     for c in (o.get("characters") or []) if isinstance(c, dict)],
            "物品": [{"name": i.get("name"), "category": i.get("category"), "appearance": i.get("appearance"),
                     "importance": i.get("importance", "")}
                    for i in (o.get("items") or []) if isinstance(i, dict)],
            "场景": [{"name": s.get("name"), "appearance": s.get("appearance")}
                     for s in (o.get("scenes") or []) if isinstance(s, dict)],
            "情节要点": (o.get("key_beats") or [])[:5],
        })
    prompt = f"""【任务】以下是长篇小说《{novel_title}》各段落的提炼结果（JSON）。请把它们整合成一份可直接用于漫剧生产的「全剧设定集」。
【风格要求】{style}
【集数】{episodes} 集  【预计总镜头数】{target_shots}
【分段提炼结果】
{json.dumps(digest, ensure_ascii=False)}
{_ctx_asset_names_block(continuity_ctx)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{
  "title": "剧名（4-12 字）",
  "theme": "一句话主题/卖点（30 字以内）",
  "style": "{style}",
  "characters": [{{"name": "姓名", "gender": "性别，只允许「男」或「女」两个值；必须按原文的人物称谓/代词/姓名线索推断后明确给出，禁止留空或写「未知」", "age": "年龄", "identity": "身份/阵营（15 字以内）", "appearance": "静态外貌定妆（含发色/瞳色/脸型/体格/标志特征等**不随剧情变化的特征**，**必须包含性别（如「女性」「男子」）**，**不含会逐集变化的服饰/配饰——那些写进 outfit**，60 字以内；若上方设定库已锁定则该字段必须与锁定值逐字一致）", "outfit": "本集服装状态（**动态特征：逐集可变的服饰/配饰，与静态 appearance 解耦**——appearance 是定妆照约束的静态外貌，outfit 是分镜画面约束的本集服装），20 字以内，与上集结尾一致；若本集确有换装必须体现原因", "personality": "性格（30 字以内）", "voice_style": "配音风格（15 字以内）", "reference_prompt_zh": "中文参考图提示词：角色三视图设定图，60 字以内，**必须写明角色性别（如开头写「女性角色，」「男性角色，」）**，只写画面可见的具体特征——发色发型、瞳色、脸型、服装款式与材质配色、标志配饰、三视图版式（**必须写明「正面、侧面、背面三张全身视图横排，从头到脚完整入画、同一角色身高比例一致」**，不要写成半身/胸像）；**严禁写任何风格词/画风词/质量词**（如「国漫」「3D渲染」「电影级」「高清」「精致」）", "reference_prompt_en": "English prompt for a character reference sheet with three full-body views (front, side, back laid out horizontally, head-to-toe, consistent body proportions), under 45 words, must explicitly state the character's gender (e.g. 'a woman,' / 'a man,'), comma-separated CONCRETE visual keywords (hair color and style, eye color, face shape, outfit material and colors, signature accessories, view layout). It MUST be an accurate translation of reference_prompt_zh. Never romanize Chinese concepts into invented pinyin (「国漫」 must become 'Chinese animated style', NOT 'xuanxuan'); never write style or quality words — the program appends them"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观（50 字以内）＋**结构三要素**（功能端/尾端/使用朝向，见硬约束）", "structure": "**结构三要素**（每项 8 字以内）：①功能端＝哪一端是使用端（吹嘴/刃口/接口/开口）②尾端＝哪一端是尾（系绳/握把/底座）③使用朝向＝用起来时功能端朝哪儿（如「吹嘴贴唇、腔体朝外」）——**同一致命细节**：道具在多帧/多角度运动中必须保持该朝向，否则会画反（实测：旧哨子被画成反吹）", "reference_prompt_zh（只写物品本体：造型/材质/颜色/纹样/尺寸感 + **必须写明功能端与尾端各在哪一侧、系绳系在哪一端**）", "owner": "持有人", "owner_photo": "布尔值 true/false：该物品表面是否承载某人的肖像（证件照/头像/画像/悬赏告示上的人像/屏幕里的人像）；物品有归属主人但本体不含人像时填 false。⭐ 填 true 时，reference_prompt_zh **必须**写明「印有 {{owner}} 本人的正面免冠证件照／头像，五官与 {{owner}} 一致」，**严禁**自行描写表情或五官（如「异常睁眼笑脸」「狰狞笑容」「诡异微笑」）——那会让出图模型画出一张与角色无关的陌生脸（用户已反馈：证件照上是个陌生怪笑脸）；该物品的主人须与 owner 字段一致", "surface_text": "该物品表面**确切**要呈现的文字，**必须把画面上出现的每一个字都逐字给出**（如工牌抬头「安保部」、告示标题「注意安全」）。⭐ **只要该物品表面存在任何可读文字（书名/告示/牌匾/卡片/标签/证件/屏幕/横幅/刻字/正文/条目/编号），本字段就必须完整填写、不得留空**——留空会让出图模型自行编造汉字，实测必出乱码（形似汉字的错字）。⭐⭐⭐ **正文与条目也必须写全，不能只写标题**：若 appearance / reference_prompt_zh 里写了「编号列 N 条规则」「正文若干行」「分条说明」「表格」「清单」这类**需要逐字呈现**的内容，本字段就必须给出这 N 条的**逐条文字**（紧凑写法，条间用空格隔开，如「1禁止奔跑 2保持安静 3随手关门」），**严禁**只给标题而把正文留给模型想象——那必然乱码。⭐ **反向自洽（同等重要）**：本字段**没有**覆盖到的文字，**不得**在 appearance / reference_prompt_zh 里要求「可读」；若某段文字确实不该被阅读（背景虚化文本、远距离标识），请在 appearance 里明确写「文字虚化不可辨」，而不是让模型去编。⭐ 长度：**总计不超过 80 个字**（含标点）。超过时**必须精简为可渲染的短条目**（每条 2-8 字），而不是删掉内容——中文文字渲染能力有限，条目越短越清晰。确实过长的（如整页文章）请只写**开头的几个关键词**，并在 appearance 注明「其余文字虚化」。⭐ 本字段的值会被**逐字放进出图提示词的引号内**做文字渲染，因此**必须填可直接印刷的原文本身**（如「楼层安全守则」、「1禁止奔跑 2保持安静」），**严禁**填「标题」「小字」「告示文字」「手写内容」这类**描述性词语**"", "importance": "重要/临时。判定三问（任一答案为「是」即判临时）：①删掉它剧情还成立吗？②它只是随手用的日常物品吗？③它只是场景陈设吗？", "reference_prompt_zh": "中文参考图提示词，50 字以内，只写形制、材质、颜色、纹样与磨损状态；**严禁写风格词/画风词/质量词**", "reference_prompt_en": "English prompt for an item prop sheet, under 40 words, comma-separated concrete visual keywords (shape, material, color, pattern, wear). Accurate translation of reference_prompt_zh; no invented pinyin, no style or quality words"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型（**同一个场所的不同机位、朝向或景别一律视作同一个 location**，不要重复建场景；只有当边界、入口、功能区或固定结构真的不同时才算新地点）", "appearance": "环境与氛围（70 字以内）：把「压抑/肃杀/温暖」翻译成**已有依据的空间选择**——通道宽窄、光比强弱、材质反射、空气状态（雾/尘/雪/烟）、色温关系；不得为造气氛而新增剧情事故、封锁出口或搬动固定结构。并点明此空间能承载的戏：谁掌控入口、座位与视线高点，谁会被阻拦、围观或逼入死角，哪件道具可被交接、摔碎或藏匿（撑不起对抗、羞辱、救援、揭露与反转的空间，不要写成核心场景）", "scene_lighting": "该场景的**统一光影基调**（30 字以内）：整个场景所有镜头共享的主光源/时间/色温（如「黄昏暖调逆光」「冷蓝月光」「正午顶光」），用于消除同场景内逐镜光影漂移；无明确光源倾向时写「自然漫射光」。此字段只写光影，不写风格词/画风词/质量词，也不要出现人物", "int_ext": "本场景内外景（只填「内景」「外景」「内外景」三值之一：封闭空间＝内景、露天＝外景、跨内外＝内外景）", "time_of_day": "本场景时间（8 字以内，如「黄昏」「夜」「清晨」「雨夜」「正午」；必须与 scene_lighting 的时间口径一致）", "surface_text": "该场景中**确切**要呈现的文字，**必须把画面上出现的每一个字都逐字给出**（如告示/招牌/标识/门牌/守则/铭牌/横幅/屏幕上的字）。⭐ **只要场景里存在任何可读文字（告示、招牌、门牌、安全守则、楼层号、标语、屏幕文字等），本字段就必须完整填写、不得留空**——留空会让出图模型自行编造汉字，实测必出乱码（形似汉字的错字）。⭐⭐⭐ **正文与条目也必须写全，不能只写标题**：若写了「安全守则若干条」「疏散指引」「注意须知」这类**需要逐字呈现**的内容，本字段就必须给出**逐条文字**（紧凑写法，条间空格隔开，如「1禁止吸烟 2禁止奔跑 3保持安静」）；**严禁**只给标题把正文留给模型想象。⭐ **反向自洽**：本字段没覆盖的文字，**不得**在 appearance / reference_prompt_zh 里要求「可读」；背景虚化的文字请注明「文字虚化不可辨」。⭐ 长度：**总计不超过 80 个字**（含标点）。超过时精简为短条目（每条 2-8 字），而不是删内容。⭐ 会被逐字放进提示词引号内做渲染，**必须填可直接印刷的原文本身**，**严禁**填「标题」「小字」「告示文字」这类描述性词语"", "reference_prompt_zh": "中文参考图提示词，90 字以内，**地理优先**：用「从哪个入口看向哪个方向、前中后景分别是什么、锚点在彼此哪一侧」的两两关系来写，不堆装饰清单。必写：①空间身份与功能；②观察方向与可见边界；③入口与通道如何连通；④1-3 个固定锚点及其左右前后相对关系；⑤前景/中景/背景层次与一处尺度参照；⑥墙地顶材质与主次色。最后写时间天气与光源方向。**严禁写风格词/画风词/质量词，且不要出现人物**（保持空场，才能作为地理参考被后续镜头反复复用）", "reference_prompt_en": "English prompt for an environment concept art sheet, under 60 words, comma-separated concrete visual keywords. Geography-first: state the viewpoint and what falls in foreground, midground and background, then entrance and circulation, then one to three fixed anchors as pairwise relations (what sits left or right of what), then materials and palette, then time, weather and light direction. Spatial layout, architecture, no decoration inventory, no people. Accurate translation of reference_prompt_zh; no invented pinyin, no style or quality words"}}],
  "production_notes": {{"style_guide": "画面与叙事风格说明（60 字以内）"}}
}}
【硬性约束】characters / items / scenes **不设数量上限**——原文有多少就登记多少，不得为省篇幅合并或丢弃条目；characters 按戏份从大到小排序；纯背景路人（无台词、无独立动作、不推进情节，如老人甲、村民若干）不登记，但共同推进情节的一方阵营／敌群／首领或群像（有共同称谓、各自开腔或行动）必须登记——有具名首领就登首领，只有共同称谓的群体就登记为一个群像角色（name 用群体称谓如「正道群敌」，role 标敌方，appearance 写可辨识代表形象）；items 仍按三问过滤（①删掉它剧情还成立吗？②只是随手用的日常物品吗？③只是场景陈设吗？任一答案为「是」即剔除，临时道具不得进 items）；不要输出示例里的占位文字。若上方提供了「项目级设定库」，则已登记角色的 name / gender / appearance / personality 必须与该库完全一致（禁止改名、禁止改性别、禁止改外观），只允许更新 outfit（当前服装状态）。
【风格红线·重要变更】风格词由**程序在生成前统一追加**（幂等，不会重复），不再由你写。因此 characters / items / scenes 三个数组里每一条 reference_prompt_zh 与 reference_prompt_en **都不得自行写风格词、画风词或质量词**——自己写了会导致风格在提示词里出现两遍（实测就是「中国古风玄幻漫剧风格。风格：中国古风玄幻漫剧，画面精致…」这种重复），属于不合格输出。你只需专注描述画面里看得见的具体特征，把风格判断交给程序。
【格式红线】直接以 {{ 作为输出的第一个字符；严禁输出任何推理过程、思考草稿、英文说明、markdown 代码块标记或前后缀解释文字；各条目字段描述尽量精炼，但**不得为控制篇幅而丢弃或合并角色/物品/场景条目**（数量上限已取消，超长由程序自动提高额度重试）。"""
    # 断点缓存：命中则跳过模型汇总（未命中时行为与加缓存前完全一致）。
    # 只缓存**模型成功产出**的结果；下面的确定性兜底不缓存，好让下次仍有机会走模型。
    hit = _cache_get(cache_dir, "bible", prompt, events, "bible")
    if hit is not None and hit.get("characters"):
        return hit
    bible_retry_kw = {"max_attempts": 4, "token_ladder": (6000, 8192, 16384, 24576)}
    data = {}
    for tag, p in (("bible", prompt),
                   ("bible-repair", prompt + "\n\n【重要·格式修复】上一次调用未产出完整合规 JSON。"
                    "请重新输出**一个完整、紧凑的 JSON 对象**，必须同时包含 characters、items、scenes、"
                    "production_notes 四个键，不要只输出其中某个数组，不要输出任何解释文字。"),
                   ("bible-slim", prompt + "\n\n【重要·精简模式】模型输出连续被截断。请只保留最核心信息："
                    "characters 最多 4 个（name / gender / identity / appearance / outfit 五个字段，每个不超过 20 字），"
                    "items 最多 3 个（name / category / appearance），scenes 最多 3 个（name / appearance），"
                    "其余字段全部省略。直接输出 JSON，不要解释。")):
        try:
            raw = _robust_json(client, p, system=SYSTEM_BIBLE, temperature=0.3, max_tokens=6000,
                               events=events, label=tag, **bible_retry_kw)
        except LLMError as e:
            logger.warning(f"bible 阶段 {tag} 调用失败：{e}")
            continue
        data = _normalize_bible(raw)
        if data.get("characters"):
            _cache_put(cache_dir, "bible", prompt, data)
            return data
        logger.warning(f"bible {tag} 返回缺少 characters（原类型 {type(raw).__name__}）：{str(raw)[:200]}")

    # 兜底：模型连续失败时用分块提炼结果做确定性聚合，保证整集生成不中断
    logger.warning("bible 汇总失败，降级为分块设定确定性聚合（未使用模型汇总）")
    data = _fallback_bible(outlines, novel_title, style)
    if events is not None:
        events.append({"label": "bible-fallback", "attempt": 0, "max_tokens": 0,
                       "finish_reason": "n/a", "truncated": False,
                       "note": "模型汇总连续失败，已降级为分块设定聚合"})
    return data


def _fallback_bible(outlines: list, novel_title: str, style: str) -> dict:
    """bible 兜底：从各分块提炼结果确定性聚合角色 / 物品 / 场景（不调用模型）"""
    chars, items, scenes = {}, {}, {}

    def _pick(src: dict, key: str, fields: list) -> None:
        name = str(src.get("name") or "").strip()
        if not name or name in key:
            return
        key[name] = {"name": name, **{f: str(src.get(f) or "") for f in fields}}

    for o in outlines or []:
        if not isinstance(o, dict):
            continue
        # 2026-09-29 资产不设上限：兜底聚合同样不再 [:6]/[:3] 截断
        for c in (o.get("characters") or []):
            if isinstance(c, dict):
                _pick(c, chars, ["identity", "gender", "appearance", "outfit", "personality", "voice_style"])
        for i in (o.get("items") or []):
            if isinstance(i, dict):
                _pick(i, items, ["category", "appearance", "owner"])
        for s in (o.get("scenes") or []):
            if isinstance(s, dict):
                _pick(s, scenes, ["location", "appearance"])
    out_chars, out_items, out_scenes = (list(chars.values()), list(items.values()),
                                        list(scenes.values()))
    # 兜底路径同样要带风格：否则一旦 bible 汇总失败，资产提示词又回到「零风格词」老样子
    eff = style_kit.normalize_style(style)
    if eff:
        for group in (out_chars, out_items, out_scenes):
            style_kit.apply_asset_style_all(group, eff)
    return {
        "title": (novel_title or "")[:20],
        "theme": "",
        "style": eff,
        "characters": out_chars,
        "items": out_items,
        "scenes": out_scenes,
        "production_notes": {"style_guide": eff},
        "_degraded": True,
    }


def build_shots_for_chunk(client, bible: dict, outline: dict, chunk: dict, shots_target: int,
                          events: list = None, depth: int = 0,
                          continuity_ctx: dict = None, cache_dir: str = "",
                          shots_hard_cap: int = 0, prev_tail: str = "") -> list:
    """③ 单块写分镜（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    prev_tail（P1-2 接缝重叠）：上一块的「结尾上下文」预渲染文本（由调用方经
    :func:`_prev_tail_block` 构造），非空时注入 prompt 让本块开头**承接**上段而非
    凭空重启。默认 ""（首块 / 递归子块 / 旧调用方）→ prompt 不出现该段，零变化。

    continuity_ctx 非空时（跨集连贯性方案 A②③ / C⑦⑧）：注入上集摘要卡、衔接契约、
    项目级风格指南、人物口吻词典、金句保留清单与运镜术语表。
    cache_dir 非空时启用断点缓存：命中即跳过模型调用（见文件上方缓存说明）。
    shots_hard_cap > 0 时收紧本块镜头数上限（**显式额度**，只压上限、不动下限，
    避免与 SHOTS_PER_CHUNK_MIN 打架）。2026-10-09 起主流程**不再传单集上限**
    （按场次生产后单集镜数不设上限），该参数仅用于调用方显式收紧。

    ⚠️ 单块镜数超过 MAX_SHOTS_PER_CHUNK 时**先二分再送模型**（见该常量注释）：
    一次性要几十镜会让响应体大到读超时（实测 49 镜 ReadTimeout 1200s）。
    这里必须在**调用前**劈半 —— 劈半原本只挂在 LLMTruncatedError 上，而超时抛的是
    LLMError/LLMGatewayUnavailable，**不会**触发劈半，直接整块失败。
    """
    # ---- 预劈半：单块镜数过大 → 先拆成子块，避免单次响应体过大导致读超时 ----
    if (MAX_SHOTS_PER_CHUNK and int(shots_target) > MAX_SHOTS_PER_CHUNK
            and depth < CHAPTER_SPLIT_MAX_DEPTH):
        _subs = _split_chunk_in_half(chunk)
        if _subs:
            logger.warning(
                "块 %s 目标 %d 镜超过单块上限 %d，预拆为 %d 个子块（原文不丢，改为多次调用）",
                chunk.get("title"), int(shots_target), MAX_SHOTS_PER_CHUNK, len(_subs))
            if events is not None:
                events.append({"label": f"shots#{chunk.get('index')}",
                               "event": "pre_split", "depth": depth,
                               "target": int(shots_target),
                               "parts": [len(s.get("text") or "") for s in _subs]})
            # 镜数按原文字数比例分配（不是均分）—— 短的那半不该拿同样的镜数
            _lens = [max(1, len(s.get("text") or "")) for s in _subs]
            _tot = float(sum(_lens))
            _out = []
            _beats = list(outline.get("key_beats") or [])
            for _i, _s in enumerate(_subs):
                _target_i = max(1, int(round(int(shots_target) * _lens[_i] / _tot)))
                _sub_outline = dict(outline)
                if _beats:
                    _n = len(_beats)
                    _a = _i * _n // len(_subs)
                    _b = max(_a + 1, (_i + 1) * _n // len(_subs))
                    _sub_outline["key_beats"] = _beats[_a:_b]
                _out.extend(build_shots_for_chunk(
                    client, bible, _sub_outline, _s, _target_i,
                    events=events, depth=depth + 1,
                    continuity_ctx=continuity_ctx, cache_dir=cache_dir,
                    shots_hard_cap=shots_hard_cap))
            return _out

    # 2026-09-29 资产不设上限：可用资产清单不再 [:6]/[:3] 截断 —— 清单里没有的名字
    # 分镜就不敢引用，_match_known_names 会在上游把未登记名静默滤掉（丢锚点不可逆）。
    char_brief = [
        {"name": c.get("name"), "gender": c.get("gender") or "", "appearance": (c.get("appearance") or "")[:40]}
        for c in (bible.get("characters") or []) if isinstance(c, dict)
    ]
    item_brief = [{"name": i.get("name"), "appearance": (i.get("appearance") or "")[:30]}
                  for i in (bible.get("items") or []) if isinstance(i, dict)]
    scene_brief = [{"name": s.get("name"), "appearance": (s.get("appearance") or "")[:40]}
                   for s in (bible.get("scenes") or []) if isinstance(s, dict)]
    _hard = int(shots_hard_cap or 0)
    shots_target = int(shots_target)
    if _hard > 0:
        # 调用方显式给了额度时，它是**权威**：目标也不得超过它，否则 prompt 自相矛盾
        #（「至少 25 个、上限 6 个」）。⚠️ 2026-10-09 起主流程不再传单集上限（按场次生产）。
        shots_target = max(1, min(shots_target, _hard))
    # ⭐ 2026-10-10（用户指定「不默认每集镜头数」）：
    #    shots_target <= 0 = **不预设下限**，镜数交给模型按原文信息密度判定；
    #    > 0 时保持旧行为（下限 + cap）。
    #    为什么要开放：固定的默认 12 会被写进【硬性约束】当**下限**，而同一份模板里
    #    又写着「本片目标是每集 20~30 个镜头」—— 自相矛盾，且会把内容摊薄/灌水。
    # ⭐ 2026-10-10（用户追加指定「每集下限和上限都不限制」）：
    #    提示词里**不再出现任何镜数上下限** —— 唯一准绳是原文的信息密度与剧情完整度。
    #    shots_cap 仍计算（模板变量位与下游 token 预算需要它），但它只是**技术护栏**，
    #    **不写进提示词**，模型看不到。
    if shots_target > 0:
        shots_cap = max(shots_target, min(120, int(math.ceil(shots_target * SHOT_CAP_GROWTH))))
        _shots_min_text = f"至少 {shots_target} 个（**不设上限**）"
        _shots_range_text = (f"shots 数组元素个数**不设上限**（显式要求至少 {shots_target} 个）；"
                             f"不得为凑数把同一件事拆成多镜")
        _shots_targeting_text = (f"本块镜数**下限 {shots_target} 个、不设上限**"
                                 f"（原文信息量更大时可远多于它）。")
    else:
        # 无下限：cap 仅作技术护栏（防单次响应体过大），不写进提示词。
        shots_cap = max(12, min(120, int(math.ceil(SHOT_GRANULARITY_MAX_SHOTS * SHOT_CAP_GROWTH))))
        _shots_min_text = "数量由你按本块原文的信息密度判定（**不设上下限**）"
        _shots_range_text = ("shots 数组元素个数**不设上下限**"
                             "（既无「至少 N 镜」，也无「不得超过 N 镜」）")
        _shots_targeting_text = (
            "**镜数不预设、无上下限**——由你按本块原文的**信息密度**判定：冲突/转折/关键动作/"
            "金句密集就多切镜，情节单薄就少切镜；既**不要为凑数灌水**（同一件事补插入镜、"
            "把一个动作拆成几镜），也**不要为省事把原文情节合并丢掉**。")
    if _hard > 0:
        shots_cap = min(shots_cap, max(shots_target, _hard))
    speech_budget = SHOT_SPEECH_BUDGET_CHARS
    # ---- 2026-10-07 提示词外置（app/prompts/script_generate.txt）：原内联 f-string 的
    # 静态骨架抽到提示词模板中心，全部内插值改为 render() 的命名参数（逐字同值，清单见
    # prompt_templates.REGISTRY["script_generate"].variables）。模板读取失败/为空时回落
    # 下方原 f-string 兜底分支 —— 两条路径产出的提示词必须逐字一致（改模板正文时需同步
    # prompt_templates._DEFAULT_SCRIPT_GENERATE 与此处 f-string）。
    # ⚠️ render() 对值为 None 的变量不替换占位符，而 f-string 会把 None 渲染成 "None"：
    # 故可能为 None 的取值（chunk_title / outline_summary）先按 f-string 语义 str() 化，
    # 保证极端输入下两条路径仍逐字一致；其余取值均带 or ''/默认值兜底或恒为 str/int。
    _tpl = prompt_templates.load("script_generate")
    if _tpl:
        prompt = prompt_templates.render(
            "script_generate",
            rules=REWRITE_RULES,
            drama_title=bible.get('title') or '',
            chunk_title=str(chunk.get('title')),
            chunk_index=chunk['index'],
            chunk_total=chunk['total'],
            target_shots=shots_target,
            shots_cap=shots_cap,
            shots_min_text=_shots_min_text,
            shots_range_text=_shots_range_text,
            shots_targeting_text=_shots_targeting_text,
            style=bible.get('style') or '',
            style_guide=(_ctx_block(continuity_ctx, 'style_guide_text')
                         or (bible.get('production_notes') or {}).get('style_guide') or ''),
            prev_block=_ctx_line(continuity_ctx, 'prev_block'),
            bible_block=_ctx_line(continuity_ctx, 'bible_block'),
            contract_block=_ctx_line(continuity_ctx, 'contract_block'),
            style_block=_ctx_line(continuity_ctx, 'style_block'),
            camera_block=_ctx_line(continuity_ctx, 'camera_block'),
            preflight_block=_ctx_line(continuity_ctx, 'preflight_block'),
            prev_tail=prev_tail,
            char_brief_json=json.dumps(char_brief, ensure_ascii=False),
            item_brief_json=json.dumps(item_brief, ensure_ascii=False),
            scene_brief_json=json.dumps(scene_brief, ensure_ascii=False),
            chunk_text=chunk.get('text') or '',
            outline_summary=str(outline.get('summary', '')),
            key_beats_json=json.dumps(outline.get('key_beats') or [], ensure_ascii=False),
            shot_type_count=len(SHOT_TYPES),
            shot_type_enum=_SHOT_TYPE_ENUM_ZH,
            speech_budget=speech_budget,
        )
    else:
        # 兜底：用户覆盖 / 出厂模板 / 代码内注册兜底三级都不可用时走原 f-string（正文与
        # app/prompts/script_generate.txt 的骨架、prompt_templates._DEFAULT_SCRIPT_GENERATE
        # 逐字一致，仅占位符由运行时值填充）。
        prompt = f"""【任务】为漫剧《{bible.get('title') or ''}》的「{chunk.get('title')}」（第 {chunk['index']}/{chunk['total']} 段）编写分镜：{_shots_min_text}。把下方原文**压缩提炼**成可拍摄的镜头，只保留推动剧情的关键情节（冲突/转折/关键动作/金句），纯背景铺陈直接删去、勿逐句照搬。
【粒度口径（**务必先读**）】{_shots_targeting_text}每镜 5~6 秒（不是每镜 2 秒的快切）。
⚠️ **镜数不设上下限**：既没有「至少 N 镜」的下限，也没有「不得超过 N 镜」的上限。唯一的准绳是
**原文的信息密度**与**剧情完整度** —— 原文里的冲突/转折/关键动作/金句必须全部落到镜头里
（覆盖率有硬校验），但不得为凑数灌水、也不得为省事合并丢掉情节。
  · **镜数少了不等于少写情节**：目标镜数变少时，请把相邻的连续情节**合并进同一个镜头**（一个镜头里可以容纳一个完整的动作过程、以及前后两段关键情节），而**不是**把原文情节丢掉。原文里的冲突/转折/关键动作/金句仍必须**全部**落到镜头里 —— 本系统对原文覆盖率有硬校验，漏情节会导致整集重跑。
  · **不要把一个完整动作拆成几个镜头**：「抬手→握拳→挥出」是**一个**镜头里的连续动作，不是三个镜头。只有当**空间/时间/视角真的发生跳跃**（换了地点、跳了时间、要强调另一个主体）时才切镜。
  · **不要为同一件事再补一个镜头**：已经拍过的道具/手部，不要为了「规避人脸」再单独切一个几乎同画面的插入镜。

【描述粒度口径（2026-10-06 用户指定：写关键动作分解、只写关键节点，**务必先读**）】
  · description 写「**起点 → 关键动作节点 → 结果**」：动作过程写**关键动作分解**（用→连接的 2~4 步，如：走到椅前→扶椅背转身→缓缓落座），只写关键节点、不写琐碎中间步（「拉开窗帘」写「走到窗前→拉开窗帘」即可，不写手指逐片推开的过程）。
  · 琐碎中间步与逐格推进**由视频/九宫格生成阶段负责**：5~6 秒镜头的 9 宫格就是把关键节点之间的时间自然切片，剧本层只写 2~4 个关键节点即可——把琐碎中间步写满反而让 9 个格全画同一个中间态（九宫格雷同，用户已纠正）。
  · 每镜 description **100~120 字（下限 100，上限 120）**，写：主体、关键动作分解、关键构图位置、情绪落点。光线/氛围/背景**只在推动剧情或首次出场时写一句**，不逐句铺陈。
{REWRITE_RULES}
【全剧风格】{bible.get('style') or ''}　【画面风格指南】{_ctx_block(continuity_ctx, 'style_guide_text') or (bible.get('production_notes') or {}).get('style_guide') or ''}
{_ctx_line(continuity_ctx, 'prev_block')}{_ctx_line(continuity_ctx, 'bible_block')}{_ctx_line(continuity_ctx, 'contract_block')}{_ctx_line(continuity_ctx, 'style_block')}{_ctx_line(continuity_ctx, 'camera_block')}{_ctx_line(continuity_ctx, 'preflight_block')}{prev_tail}【可用角色】{json.dumps(char_brief, ensure_ascii=False)}
【可用物品】{json.dumps(item_brief, ensure_ascii=False)}
【可用场景】{json.dumps(scene_brief, ensure_ascii=False)}
【本段原文（先压缩提炼：只保留冲突/转折/关键动作/金句，纯背景铺陈直接删去，勿逐句照搬）】
{chunk.get('text') or ''}
【本段剧情摘要】{outline.get('summary', '')}
【本段情节要点】{json.dumps(outline.get('key_beats') or [], ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{"shots": [{{"camera": "景别+运镜（必须取自上方运镜术语表，如 中景跟随/近景轻推，10 字以内）", "shot_type": "景别（只填以下 {len(SHOT_TYPES)} 值之一：{_SHOT_TYPE_ENUM_ZH}；局部=只拍手部/道具的插入镜；与 camera 里的景别词保持一致；本镜确实无法确定景别时写空字符串）", "camera_motion": "运镜（只填运镜词，优先用上方【克制运镜·推荐】的 固定/轻推/轻摇/跟随/轻手持；无运镜的静止镜头填「固定」）", "location": "所属场景名（必须来自可用场景）", "description": "画面内容描述（100~120字，下限100上限120，按「起点→关键动作节点→结果」写：谁做了什么、**动作过程写关键动作分解（用→连接的 2~4 步，如：走到椅前→扶椅背转身→缓缓落座）**、**做完后画面是什么状态**、在画面什么位置；只写关键节点不写琐碎中间步（琐碎中间步交给九宫格/视频阶段切片）；外貌衣着/环境光线只在推动剧情或首次出场时写一句，不逐句铺陈，禁止写背景陈述/世界观/来历评述）", "visual_detail": "画面补充细节（可选；当 description 之外还有本镜**结果态**里必须交代的关键环境/道具状态时写在这里，≤120字；不要重复动作过程；没有多余细节时写空字符串）", "dialogue": [{{"speaker": "说话角色名（必须与可用角色完全一致）", "text": "该角色台词（≤30 字；原文对话尽量原样保留；角色的自语/心声写成该角色本人的台词）"}}], "emotion": "情绪（8 字以内）", "edit_reason": "剪辑动机（20~30字，具体说明这一镜为什么切/承担什么叙事功能，禁止写可套用的空话，如：切掉环境只留下他的反应/用空间拉开取代告别对白/物件回环把十年压缩到一张纸上）", "beat": "叙事节拍（本镜所处节拍，只填「开场」「触发」「高潮」「收尾」四值之一；拿不准填「触发」）", "audio_cues": "音效/配乐提示（60 字以内，只写环境音/音效/配乐，不写人声）", "characters_in_shot": ["出场角色名"], "first_frame": "首帧画面（运动开始前那一刻的静态快照：画面主体与构图，40字以内；无明显运动变化写空字符串）", "last_frame": "末帧画面（运动结束后的终态，40字以内；与首帧相同或无运动时写空字符串）", "motion": "运动描述（严格区分【摄影机运动】推拉摇移跟升降 与【画面内运动】人物/物体自身动作；30字以内；静止镜头写空字符串）", "caption": "字幕（**默认写空对象 {{}}**；仅当本镜承担时空落点交代或集尾悬念时才写，形如 {{"text": "字幕文字（≤20字）", "kind": "时间地点/回溯/悬念 三值之一"}}。字幕是后期叠加的文字，不进画面描述、不产生人声）", "items_in_shot": ["出场物品名"]}}]}}
【禁止输出 prompt_h3 字段】视频提示词由程序在生成阶段按 H3 规范自动构建（它会结合当次实际传入的参考图，生成 subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 六段）。你在剧本阶段并不知道最终配几张参考图，写出来的英文提示词缺少 <Picture N> 标签，反而会覆盖规范提示词导致出片偏离设定。因此**不要写 prompt_h3、不要写英文提示词**；把画面信息全部写进 description 即可。
【站位与动作（3D 导演台依赖，逐镜必填）】每一镜都要写：\n
  ① blocking：本镜出场角色的**站位**，每个出场角色一条，形如 
{{"name": 角色名, "x": left/center/right, "depth": front/mid/back, "facing": camera/left/right/back}}。\n
     · x = 画面左右（left=画面左）；depth = 离镜头远近（front=更靠近镜头）；facing = 朝向，留空表示朝内。\n
     · **左右顺序与前后层次必须与本镜情节一致**（谁在左、谁更靠近镜头），
禁止所有镜头套用同一套站位；角色互换攻守、走近/退开时，站位要跟着变。\n
  ② action：本镜的**动作 beat**——谁做了什么、动作从哪到哪（例：「羡进抬头直视赵天霸，右手缓缓握拳」）。
**不得留空**，纯对话镜也要写神态与小动作。\n
【台词要求】dialogue 必须是数组，数组元素为 {{"speaker": 角色名, "text": 台词}}；speaker 必须精确等于「可用角色」中的名字，禁止写“旁白/众人”等未登记角色；无台词的镜头 dialogue 写 []（空数组），禁止写成字符串或 null。角色的心理活动改写成该角色**本人**的自语台词时，speaker 仍写角色名（不要写成「旁白」，本系统没有旁白角色）。dialogue **只承载**：原文对话、以及原文明确心理活动/独白改写的第一人称自语——第三人称叙述与背景补叙**禁止**写成任何角色开口的台词（改写规则 8）。
【台词预算（防成片截断）】单个镜头的 dialogue **合计不超过 {speech_budget} 字**（≈3.6 秒配音，按 config.CHARS_PER_SECOND=4.5 字/秒）。台词过多时**先精简冗余语气词与重复表述**，仍超预算才拆成相邻镜头——配音是按镜头时间轴铺的，单镜台词超出镜头时长会被成片尾部静默截掉。
【音轨说明（本系统不产出旁白）】成片没有画外音解说，配音链路**只读 dialogue**：audio_cues 里写「雨声」「风声」这类音效**不会产生人声**。因此：① 有对话或自语的镜头必须写 dialogue，禁止把台词塞进 description / visual_detail / audio_cues；② 纯画面/纯动作镜头允许没有台词（该镜成片留白，由音效与配乐铺底），但**必须**在 audio_cues 写明音效/配乐提示；③ **严禁**凭空编造原文里没有的台词来「凑人声」——宁可留白，也不要无中生有。
【字幕/转场（caption）】本系统不产出旁白，**时空跳跃靠字幕点明落点**。只在三种情况写 caption：
①上下集之间时间/地点发生跳变（kind=时间地点，如「一年后」「青茅山·古月山寨」）；
②本镜是时空回溯的落点（kind=回溯，如「春秋蝉，逆转时光。」）；
③本集结尾仍有未回收伏笔、需要留住悬念（kind=悬念）。
**其余镜头一律写空对象**——字幕滥用会打断观感。caption.text ≤20 字，只写交代时空或悬念的短句；**禁止**复述台词、禁止写画面描述、禁止把台词搬进字幕。
【硬性约束】{_shots_range_text}。只把原文里**推动剧情的冲突/转折/关键动作/金句**落到镜头里，纯背景补叙、纯环境描写（不推进剧情）**直接删去、不单独成镜**；name 字段必须与上面「可用角色/物品/场景」中的名字完全一致，不要新造名字。若上方给出「本集必须出现的原文金句」，必须把每句**原样**写进对应角色的 dialogue.text（不得改写、不得拆分、不得省略）。上一集已发生的事件禁止在本集重演。
【关键情节自检】写完回看上方「剧情摘要/情节要点」，确认每个关键情节都有对应镜头；纯背景补叙、纯环境描写若未推进剧情应当已删去，**不要求逐句覆盖原文**。记住：本系统没有旁白，背景补叙与环境描写靠画面承载、绝不写成台词，心理活动靠神态动作或第一人称角色自语承载。"""
    label = f"shots#{chunk.get('index')}"
    hit = _cache_get(cache_dir, "shots", prompt, events, label)
    if hit is not None and isinstance(hit.get("shots"), list) and hit["shots"]:
        return [s for s in hit["shots"] if isinstance(s, dict)]
    try:
        # ⚠️ 起始额度必须已包含「思考水位」：agnes-3.0-flash 这类 always-on reasoning 模型
        # 在本任务的思考量实测 ≈16K token（见 llm_client.REASONING_ONLY_TOKEN_FLOOR 注释）。
        # 此前按 shots_target*300+1200 给（12 镜 → 4800），低于思考水位 → 正文恒为空，
        # 表现为「模型只吐思考内容」并把整集卡死。这里按「思考预留 + 每镜正文」给足。
        _budget = SHOTS_THINKING_RESERVE + int(shots_target) * SHOTS_TOKENS_PER_SHOT
        data = _robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.6,
                            max_tokens=max(8192, min(24000, _budget)),
                            events=events, label=label,
                            max_attempts=4,
                            token_ladder=(16384, 24576, 32768))
    except (LLMTruncatedError, LLMError) as _e:
        # ⚠️ 不只截断要二分：**超时/网关故障也要**。
        # 实测坑：一次要 49 镜 → 网关 ReadTimeout 1200s，抛的是 LLMError（非截断）
        #  → 旧代码不二分 → 整块失败、整集中断。既然「镜数太多」正是病因，
        #  二分后每块镜数减半、响应体也减半，大概率能过。
        # 但 LLMGatewayUnavailable（上游没算力/熔断）要**原样上抛** ——
        # 那是网关整体挂了，二分多少次都没用，正确处置是立刻停下告诉用户。
        if isinstance(_e, LLMGatewayUnavailable):
            raise
        _tag = "输出被截断" if isinstance(_e, LLMTruncatedError) else "调用失败"
        subs = _split_chunk_in_half(chunk) if depth < CHAPTER_SPLIT_MAX_DEPTH else []
        if not subs or int(shots_target) <= 1:
            if isinstance(_e, LLMTruncatedError):
                raise LLMTruncatedError(
                    f"{label}（{chunk.get('title')}，{len(chunk.get('text') or '')} 字，目标 {shots_target} 镜）"
                    f"在自动提高 max_tokens 后仍被截断，且已无法继续二分（depth={depth}）") from None
            # 非截断且无法再二分 → 保留原异常类型与原错误信息（含网关诊断），别吞成 Unknown
            raise
        logger.warning(
            "%s 分镜%s，自动二分为 %d 个子块重试（depth=%d，原错误：%s）",
            label, _tag, len(subs), depth, str(_e)[:160])
        if events is not None:
            events.append({"label": label, "event": "sub_split", "depth": depth,
                           "reason": _tag,
                           "parts": [len(s.get("text") or "") for s in subs]})
        per = max(1, int(round(int(shots_target) / float(len(subs)))))
        beats = list(outline.get("key_beats") or [])
        merged = []
        for i, s in enumerate(subs):
            sub_outline = dict(outline)
            if beats:
                n = len(beats)
                a = i * n // len(subs)
                b = max(a + 1, (i + 1) * n // len(subs))
                sub_outline["key_beats"] = beats[a:b]
            merged.extend(build_shots_for_chunk(client, bible, sub_outline, s, per,
                                                events=events, depth=depth + 1,
                                                continuity_ctx=continuity_ctx,
                                                cache_dir=cache_dir,
                                                shots_hard_cap=shots_hard_cap))
        return merged
    if isinstance(data, dict):
        shots = data.get("shots")
    elif isinstance(data, list):
        # 兜底：模型直接把 shots 数组作为顶层返回
        shots = [x for x in data if isinstance(x, dict)
                 and any(k in x for k in ("description", "camera", "prompt_h3"))] or None
    else:
        shots = None
    if not isinstance(shots, list):
        return []
    out = [s for s in shots if isinstance(s, dict)]
    if out:
        _cache_put(cache_dir, "shots", prompt, {"shots": out})
    # P2-1：扫描本块 LLM 原始产出的字段漂移（report-only，不阻断）。
    # 漂移会被下方 _norm_shots 静默修正/截断——这里显式记账，让「模型又越界了」可见，
    # 并把汇总写进 events（随 metadata.truncation_events 落盘可查）。空产出则跳过。
    drift = audit_shots_structure(out, bible, tag=label)
    if drift:
        _kinds = {}
        for _f in drift:
            _kinds[_f["kind"]] = _kinds.get(_f["kind"], 0) + 1
        logger.warning("%s 分镜字段漂移 %d 处：%s", label, len(drift), _kinds)
        if events is not None:
            events.append({"label": label, "event": "field_drift",
                           "count": len(drift), "kinds": _kinds,
                           "findings": drift[:12]})
    return out


def _fallback_shots_for_chunk(chunk: dict, shots_target: int = 1, bible: dict = None) -> list:
    """分镜阶段模型失败时的兜底：按原文逐句生成「原文承载镜头」，保证该段内容不丢。

    与覆盖率补生成同源（只增不删）：原文措辞写进 description（超长部分由 _norm_shots
    拆进 visual_detail，故正文不会因为 200 字截断而丢失），标记 fallback=True 供前端提示需人工润色。
    这类镜头没有台词（本系统不产出旁白），成片该段留白 —— dialogue_utils.audit_script 会显式告警。
    """
    text = str((chunk or {}).get("text") or "")
    if not text.strip():
        return []
    bible = bible or {}
    chars = [c.get("name") for c in (bible.get("characters") or [])
             if isinstance(c, dict) and c.get("name")]
    scenes = [s.get("name") for s in (bible.get("scenes") or [])
              if isinstance(s, dict) and s.get("name")]
    total = int(chunk.get("char_count") or len(text))
    per = max(CHARS_PER_SHOT, total // max(1, int(shots_target)))
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?…；;])\s*|\n+", text) if s.strip()]
    if not sentences:
        sentences = [text.strip()]
    groups, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > per:
            groups.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        groups.append(buf)
    out = []
    for g in groups:
        out.append({
            "camera": "中景", "location": (scenes[0] if scenes else ""),
            "description": g[:200], "dialogue": [], "emotion": "平静", "audio_cues": "",
            "characters_in_shot": chars[:1], "items_in_shot": [], "prompt_h3": "",
            "fallback": True,
            "fallback_reason": f"第 {chunk.get('index')} 段模型分镜失败，按原文逐句承载",
        })
    return out


# ===================== 组装 =====================

def _norm_list(items, limit, keys):
    out = []
    for it in (items or [])[:limit]:
        if not isinstance(it, dict):
            continue
        row = {}
        for k in keys:
            v = it.get(k)
            row[k] = "" if v is None else (v if isinstance(v, (int, float)) else str(v))
        if any(str(row.get(k, "")).strip() for k in keys):
            out.append(row)
    return out


# ===================== 剧本 Schema：镜头数 / 每集时长（自动判定） =====================
# AI 转剧本阶段自动判定并写入，后续分镜 / 视频 / 配音链路直接引用，无需人工配置。
# ⚠️ 时长模型常量已提到 config.py 做**唯一权威**（生成期与质检期共用同一套口径），
# 并由 config 顶部 import 引入本模块命名空间（下方 from config import 一行）。
# 历史缺陷：本文件与 qc_client 各写一份、口径不同，约束互相打架。

# 动作镜识别词：description 命中任意一个即给画面停留时间加成（动作戏观感不仓促）
_ACTION_MARKERS = (
    "冲", "扑", "挥", "劈", "斩", "刺", "砍", "击", "踢", "打", "斗", "抓", "抛", "掷",
    "跳", "跃", "翻", "滚", "追", "逃", "跑", "奔", "飞", "坠", "落", "闪", "避", "挡",
    "拔", "抽", "掷", "掐", "捏", "撕", "扯", "推", "撞", "擒", "锁", "绞", "轰", "炸",
    "施法", "结印", "御剑", "掐诀", "催动", "爆发", "猛冲", "疾驰",
)

# ---- 叙事节拍（改写规则 11，P1 节拍识别切段）----
#: 节拍四段白名单。模型输出越界/未识别值一律归一到「触发」（最通用的中性节拍）。
_BEAT_WHITELIST = ("开场", "触发", "高潮", "收尾")
_DEFAULT_BEAT = "触发"
#: 「高潮」节拍镜的画面停留加成见 config.BEAT_CLIMAX_BONUS_SEC（唯一权威）。


def _norm_beat(raw) -> str:
    """把模型给的节拍值归一到四值白名单；空/越界值取默认「触发」。"""
    v = str(raw or "").strip()
    return v if v in _BEAT_WHITELIST else _DEFAULT_BEAT


def required_shot_duration(shot: dict) -> float:
    """该镜头「装得下内容」所需的时长（秒），**不封顶**。

    与 estimate_shot_duration 同源，只是不做 [MIN, MAX] 夹取：返回值 > SHOT_DURATION_MAX
    就说明这个镜头的台词/画面塞不进一个镜头，配音铺到时间轴上会溢出到下一镜、尾部被
    成片 `-shortest` 静默截掉（历史缺陷：ep04 旁白 496 秒铺在 100 秒画面上）。

    生成期用它做对账：超限即拆镜（见 _norm_shots），而不是事后告警。
    """
    # 台词合计时长：_dlg_text 会把同镜多条台词拼起来，正是配音链路的实际喂入量。
    spoken = _dlg_text(shot.get("dialogue"))
    spoken = re.sub(r"^[^：:]{1,12}[：:]", "", spoken)               # 去掉“角色名：”前缀
    if not spoken:
        spoken = str(shot.get("dialogue_text") or "").strip()
    speak_sec = len(spoken) / CHARS_PER_SECOND if spoken else 0.0

    desc = " ".join(x for x in (
        str(shot.get("description") or ""),
        str(shot.get("visual_detail") or ""),
    ) if x).strip()
    desc_sec = min(SHOT_DURATION_DESC_SEC_MAX, len(desc) / SHOT_DURATION_DESC_CHARS_PER_SEC)
    # 动作复杂度加成：description 里动作过程词/动词越多，画面越需要停留时间。
    # 历史缺陷：动作镜与静景镜一律 3 秒基准，动作戏（打斗/追逐/施法）观感仓促。
    action_sec = 0.0
    if desc:
        action_hits = sum(1 for kw in _ACTION_MARKERS if kw in desc)
        if action_hits:
            action_sec = min(SHOT_DURATION_ACTION_SEC_MAX, 0.15 + action_hits * 0.08)
    # 节拍加成（P1）：高潮/爆点镜额外停留，让节奏分层落地；不影响过渡镜的快切。
    beat_bonus = BEAT_CLIMAX_BONUS_SEC if str(shot.get("beat") or "").strip() == "高潮" else 0.0
    return SHOT_DURATION_SILENT + speak_sec + desc_sec + action_sec + beat_bonus


def estimate_shot_duration(shot: dict) -> float:
    """按画面 + 台词长度自动推算单镜头时长（秒），保证同一剧本多次运行结果稳定。

    台词兼容两种写法：结构化 [{"speaker","text"}] / 旧字符串（含 "角色名：台词" 前缀）。

    2026-09-19 优化：纯动作/无台词镜头不再一律给 3 秒 ——
    - description 里动作要素越多（动词/动作过程词），画面停留越久（动作镜需要呈现完整过程）；
    - 动作描写长的镜头（描述超 60 字）给更高基准，避免「动作才做一半就切走」。

    ⚠️ 值被夹在 [SHOT_DURATION_MIN, SHOT_DURATION_MAX]：返回值**无法**表达「装不下」。
    需要判断是否溢出请用 required_shot_duration()。
    """
    return round(max(SHOT_DURATION_MIN,
                     min(SHOT_DURATION_MAX, required_shot_duration(shot))) * 2) / 2.0


def build_episode_stats(shots: list) -> dict:
    """由分镜列表生成「镜头数 / 每集时长（秒）」统计（含分集明细 episode_plan）。"""
    per_ep: dict = {}
    for sh in shots or []:
        if not isinstance(sh, dict):
            continue
        ep = int(sh.get("episode") or 1)
        per_ep.setdefault(ep, []).append(sh)

    plan = []
    for ep in sorted(per_ep):
        rows = per_ep[ep]
        total = round(sum(float(s.get("duration") or SHOT_DURATION_SILENT) for s in rows), 2)
        plan.append({
            "episode_no": ep,
            "shot_count": len(rows),
            "duration_sec": total,
            "duration_per_shot_sec": round(total / len(rows), 2) if rows else 0.0,
            "shot_ids": [s.get("shot_id") for s in rows],
        })

    total_shots = sum(r["shot_count"] for r in plan)
    total_sec = round(sum(r["duration_sec"] for r in plan), 2)
    primary = plan[0] if len(plan) == 1 else None
    return {
        "shot_count": primary["shot_count"] if primary else total_shots,
        "duration_sec": primary["duration_sec"] if primary else total_sec,
        "duration_per_shot_sec": (primary["duration_per_shot_sec"] if primary
                                  else (round(total_sec / total_shots, 2) if total_shots else 0.0)),
        "episode_count": len(plan),
        "total_shot_count": total_shots,
        "total_duration_sec": total_sec,
        "episode_plan": plan,
    }


def apply_episode_schema(script: dict, duration_per_shot: float = None) -> dict:
    """把自动判定的镜头数 / 每集时长写入剧本 Schema（顶层 + production_notes + metadata）。

    - 顶层：shot_count / episode_duration_sec / duration_per_shot_sec / episode_plan
    - production_notes：total_shots / estimated_duration（兼容旧字段）+ 新字段
    - metadata：episode_stats（供前端与下游链路直接读取）
    """
    shots = script.get("shots") or []
    if duration_per_shot:
        for sh in shots:
            if isinstance(sh, dict):
                sh["duration"] = round(float(duration_per_shot) * 2) / 2.0
    stats = build_episode_stats(shots)
    script["shot_count"] = stats["shot_count"]
    script["episode_duration_sec"] = stats["duration_sec"]
    script["duration_per_shot_sec"] = stats["duration_per_shot_sec"]
    script["episode_plan"] = stats["episode_plan"]

    notes = script.setdefault("production_notes", {})
    notes["shot_count"] = stats["shot_count"]
    notes["episode_duration_sec"] = stats["duration_sec"]
    notes["total_shots"] = stats["total_shot_count"]
    notes["estimated_duration"] = stats["total_duration_sec"]
    notes["episode_plan"] = stats["episode_plan"]

    meta = script.setdefault("metadata", {})
    meta["episode_stats"] = stats
    meta["shot_count"] = stats["shot_count"]
    meta["episode_duration_sec"] = stats["duration_sec"]
    return script


def build_chapter_coverage_meta(shots: list, chars_per_shot: int = CHARS_PER_SHOT) -> dict:
    """按「镜头数 × 每镜承载字数」估算内容承载量（覆盖率校验前的预估值）

    仅用于覆盖率校验未执行时给出「预计承载字数 / 是否可能遗漏」的提示；
    真实覆盖率以 coverage.py 的逐句校验结果为准。
    """
    n = len([s for s in (shots or []) if isinstance(s, dict)])
    capacity = n * int(chars_per_shot or CHARS_PER_SHOT)
    return {"shots": n, "chars_per_shot": int(chars_per_shot or CHARS_PER_SHOT),
            "capacity_chars": capacity, "verified": False}


def _keep_valid_h3(raw) -> str:
    """只保留结构合规的 H3 提示词，其余丢弃

    合规 = 六段式（Ref2VA）或三段式（base）齐全，见 :mod:`h3_prompt_kit`。
    剧本阶段模型写的裸英文描述必然不合规，会被丢弃；提示词分析器产出的
    规范文本会被保留，用户的「重新生成提示词」成果不会被下一次 script
    归一化抹掉。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return text if h3_prompt_kit.validate(text)["valid"] else ""
    except Exception:  # noqa: BLE001 —— 校验失败不应影响剧本生成主流程
        return ""


def _overflow_detail(raw, limit: int) -> str:
    """把超长描述的「超出部分」拆出来（不丢画面细节，供 visual_detail 使用）。

    - 空串 / 未超长 → 返回空串（visual_detail 不重复 description）；
    - 超长 → 从第 limit 个字符开始截取，去头尾空白，限 400 字。
    """
    s = str(raw or "").strip()
    if len(s) <= int(limit):
        return ""
    return s[int(limit):].strip()[:400]


def _match_known_names(raw_names, known: list, field: str,
                       kind: str = "character") -> list:
    """把镜头声明的角色/物品名收敛到 bible 名单内（**禁止静默 take-first**）。

    P1-16 修复：旧实现用 `[...] if c in chars] or chars[:1]` —— 名字与 bible 对不上时
    静默填入首个角色（通常是主角），使整集以**错误角色**为外观锚点（日志/界面看不出来），
    并把下游 S6「禁止静默 take-first」的 `_no_reference` 分支彻底架空（列表恒非空）。
    现在只保留**命中 bible** 的名字（去重保序）；一个都不命中即返回空列表，交由下游
    `_allocate_storyboard_refs` 的 no_reference 分支显式告警/跳过，并在「有输入但全未
    命中」时记 warning，保证排障可见。

    ⚠️ 2026-09-29 统一匹配（三类资产同口径）：判据从「裸字符串相等」升级为
    :mod:`asset_name_match` 三级降级（精确 → 归一化 → **唯一**子串）。
    旧实现只做精确相等，模型写的名字差一个全角括号 / 书名号 / 空格就被**静默丢弃**
    —— 注意**上游丢掉的名字，下游再怎么容错也救不回来**（字段已经空了），
    所以统一必须从这一层开始。``kind="item"`` 用于物品（不做角色后缀剥离）。

    ``known`` 可以是名字列表（bible 名单），``resolve_names`` 内部会转成索引。
    """
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    names = [str(n) for n in (raw_names or []) if n]
    if not names:
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(names, known or [], kind)
    for _q, _k, _lv in fuzzy:
        logger.info("[%s] 名字模糊命中 bible：%r → %r（%s）", field, _q, _k, _lv)
    if missing:
        if not resolved:
            logger.warning(
                "镜头 %s 声明的名字 %s 均未命中 bible（可用：%s）→ 保留空列表，"
                "交由下游 no_reference 显式告警/跳过（禁止静默兜底取错资产）",
                field, "、".join(missing)[:80],
                "、".join(x for x in (known or []) if x)[:120])
        else:
            # 部分命中：旧实现对此**完全静默**，未命中的那个资产全程没有设定图。
            logger.warning(
                "镜头 %s 的 %s 部分未命中 bible（已保留 %s，可用：%s）→ "
                "未命中者不带设定图，请检查剧本用词",
                field, "、".join(missing)[:60], "、".join(resolved)[:60],
                "、".join(x for x in (known or []) if x)[:120])
    return resolved

#: 画面文本字段（补全 characters_in_shot 时的扫描面）——这些字段都会被写进
#: 生成端提示词，模型看得见里面的人名，就有概率把那个人画出来。
_CAST_SCAN_FIELDS = ("description", "visual_detail", "first_frame", "last_frame", "motion")


def augment_cast_from_text(row: dict, chars: list, shot_id=None) -> list:
    """从画面文本补全 characters_in_shot —— 文本点名了谁，就必须带谁的参考图。

    ## 为什么必须补（2026-09-30，实测分镜 2 反复卡死）

    characters_in_shot 的唯一来源是 LLM 自己写的字段，而 _match_known_names
    只做「把不在 bible 里的名字过滤掉」——漏登记不会被发现。于是出现：

      · description 写「赵天霸右手食指伸出指向右前方羡进胸口方向」
      · characters_in_shot 只写 ["赵天霸"]

    下游 _allocate_storyboard_refs（生成）与 _qc_ref_images（质检）都只认
    characters_in_shot，两端都走 _match_shot_chars → 羡进全程没有参考图。
    生成端提示词里却出现了「羡进」三个字，模型要么不画（动作落空），要么凭想象
    画一张脸（身份完全不对）——两条路质检都必挂 → 重试循环 → 该镜永远出不了图。

    ## 判据与边界

    · 只增不减：已登记的角色原序保留，命中者按 bible 顺序追加；
    · 名字长度 >= 2 才扫描（单字名会在任何句子里误命中）；
    · 命中即补 + warning 日志，让「模型漏登记」这件事可见（不静默）；
    · 不做任何网络/模型调用（纯字符串包含判定），零成本、可重放。

    返回补全后的角色名列表。
    """
    declared = [str(n) for n in (row.get("characters_in_shot") or []) if n]
    text = " ".join(str(row.get(_f) or "") for _f in _CAST_SCAN_FIELDS)
    if not text:
        return declared
    added = []
    for _n in (chars or []):
        _nm = str(_n or "").strip()
        if len(_nm) < 2 or _nm in declared:
            continue
        if _nm in text:
            declared.append(_nm)
            added.append(_nm)
    if added:
        logger.warning(
            "镜头 %s 的画面文本点名了未登记角色 %s → 已自动补进 characters_in_shot"
            "（否则生成端/质检端都拿不到其参考图，模型会凭想象画脸）。原文：%s",
            shot_id if shot_id is not None else "?", "、".join(added), text[:120])
    return declared


# 实质台词文本判据（P0-2 补修 / task#9）：
# 一条台词只有当它含「实质字符」（CJK / 字母 / 数字）才算"有台词内容"。
# 纯标点（"。！？"）、空 text（[{"text":""}]）、结构化空壳（[{"text":""}]）
# 一律视为**无实质台词**——与下游 prompt_qc「缺少画面描述」的硬不变量对齐。
_DLGM_SUBSTANTIVE_RE = re.compile(r"[\u4e00-\u9fff\w]")


def _has_meaningful_dlg(raw, chars: list) -> bool:
    """归一化后是否含**实质**台词文本（单一判据，供「丢弃闸门」与「画面补齐」同源使用）。

    task#9 修复的根因：旧实现里丢弃闸门用**原始值真值**（``bool([{"text":""}])`` /
    ``bool("。！？".strip())`` 都为 True），而画面补齐条件用**归一化后真值**（这两类
    归一化后 dialogue 为 []）→ 两处口径不一致 → 「结构化空壳 / 纯标点」镜头**既不被丢弃
    也不被补齐** → 空壳镜在分镜步永久卡死。本函数以「归一化后是否含实质字符」为唯一
    判据，让两处共用，消除分歧。
    """
    for line in _dlg_lines(raw, chars, chars):
        if _DLGM_SUBSTANTIVE_RE.search(str(line.get("text") or "")):
            return True
    return False


# ===================== P2-1 分镜结构审计（report-only，借鉴 CineGen response_schema 思路，按本架构落地）=====================
# 服务 venv 无 jsonschema 且不在 requirements.txt，不能引新依赖；_norm_shots 已对漂移做**容错修补**
#（截断超长按 / 回落默认 / 结构修正），真正缺口是「漂移不可见」——字段超长按 [:200] 静默截断丢内容、
# dialogue 结构错了被悄悄修掉、说话人名对不上 bible 会被下游 tts 落到 NARRATION，用户拿不到任何信号。
# 故此处做**纯标准库、只读、不阻断**的结构审计：扫一遍 LLM 原始产出，把会被 _norm_shots 静默修正/
# 截断的点显式记成 finding（供 metadata 追踪 + 日志），口径与 P0-2 软告警一致。
_AUDIT_MAX_FINDINGS = 40   # 单块最多记多少条，防某块字段全飘刷爆 metadata
_AUDIT_LEN_LIMITS = {      # 与 _norm_shots 的截断上限**同口径**：超过即「内容会被静默截断」
    "description": 200,
    "visual_detail": 400,
    "edit_reason": 50,
    "audio_cues": 60,
    "first_frame": 40,
    "last_frame": 40,
    "motion": 30,
}


def audit_shots_structure(raw_shots, bible: dict, tag: str = "") -> list:
    """扫描 LLM 原始分镜产出，返回**会被 _norm_shots 静默修正/截断**的漂移 finding 列表。

    - 纯标准库、只读、无 LLM、无 I/O；不改动 raw_shots，不阻断生成（report-only）。
    - finding 形状：{"kind": …, "detail": …, "shot_idx": int|None}。
      常见 kind：
        · beat_off_whitelist：beat 越出 _BEAT_WHITELIST（会被 _norm_beat 归一到「触发」，模型意图丢失）
        · unknown_speaker：dialogue.speaker 不在 bible 角色表（下游 tts 会落到 NARRATION 兜底）
        · unknown_cast：characters_in_shot / items_in_shot 引用了 bible 里没有的名字（会被过滤丢弃）
        · field_overflow：某字符串字段超过 _norm_shots 截断上限（内容会被静默截断）
        · field_type_drift：某字段类型不符（description/dialogue 等），会被 str()/normalize 强制修
    """
    shots = [s for s in (raw_shots or []) if isinstance(s, dict)]
    if not shots:
        return []
    bible = bible or {}
    char_names = {str(c.get("name") or "").strip()
                  for c in (bible.get("characters") or []) if isinstance(c, dict)}
    item_names = {str(i.get("name") or "").strip()
                  for i in (bible.get("items") or []) if isinstance(i, dict)}
    findings: list = []

    def _add(kind: str, detail: str, idx):
        if len(findings) < _AUDIT_MAX_FINDINGS:
            findings.append({"kind": kind, "detail": detail, "shot_idx": idx, "tag": tag})

    for idx, s in enumerate(shots):
        # 1) beat 越出白名单（会被 _norm_beat 归一到默认「触发」）
        beat_raw = str(s.get("beat") or "").strip()
        if beat_raw and beat_raw not in _BEAT_WHITELIST:
            _add("beat_off_whitelist", f"镜#{idx + 1} beat=「{beat_raw[:12]}」越出白名单，"
                                       f"将被归一为「{_DEFAULT_BEAT}」", idx)
        # 1b) caption（字幕/转场）的形状 / 长度 / 取值 / 放置（2026-10-02）
        # 与 _norm_caption 的实际行为同源：越界会被截断/归一。场景中途的字幕**不会**
        # 被丢弃（保留模型意图），但字幕只该用于时空落点与集尾悬念，故 report-only 提示。
        _cap = s.get("caption")
        if _cap not in (None, "", {}, []):
            if isinstance(_cap, str):
                _cap_text, _cap_kind = _cap.strip(), ""
            elif isinstance(_cap, dict):
                _cap_text = str(_cap.get("text") or "").strip()
                _cap_kind = str(_cap.get("kind") or "").strip()
            else:
                _cap_text, _cap_kind = "", ""
                _add("caption_type_drift",
                     "镜#%d caption 类型为 %s（应为字符串或对象），将被丢弃"
                     % (idx + 1, type(_cap).__name__), idx)
            if _cap_text:
                if len(_cap_text) > CAPTION_MAX_CHARS:
                    _add("caption_overflow",
                         "镜#%d 字幕 %d 字超出上限 %d，将被截断"
                         % (idx + 1, len(_cap_text), CAPTION_MAX_CHARS), idx)
                if _cap_kind and _cap_kind not in CAPTION_KINDS:
                    _add("caption_kind_off_whitelist",
                         "镜#%d 字幕 kind=「%s」越出白名单，将被归一为「%s」"
                         % (idx + 1, _cap_kind[:8], CAPTION_KINDS[0]), idx)
                _prev_loc = str(shots[idx - 1].get("location") or "").strip() if idx else ""
                if idx and _prev_loc == str(s.get("location") or "").strip():
                    _add("caption_misplaced",
                         "镜#%d 的字幕出现在场景中途（上一镜同场景）：字幕只用于时空落点"
                         "或集尾悬念，建议前移到本场首镜" % (idx + 1), idx)
        # 2) dialogue.speaker 不在角色表（下游 tts 落到 NARRATION 兜底）
        dlg = s.get("dialogue")
        if isinstance(dlg, list):
            for d in dlg:
                sp = str((d.get("speaker") if isinstance(d, dict) else "") or "").strip()
                if sp and char_names and sp not in char_names:
                    _add("unknown_speaker", f"镜#{idx + 1} 台词说话人「{sp[:12]}」不在角色表，"
                                             f"将被 tts 落到旁白兜底", idx)
                    break
        elif isinstance(dlg, (str, dict)) and str(dlg or "").strip():
            _add("field_type_drift", f"镜#{idx + 1} dialogue 类型为 {type(dlg).__name__}"
                                     f"（应为数组），将被结构修正", idx)
        # 3) characters_in_shot / items_in_shot 引用 bible 之外的名字（会被过滤丢弃）
        # ⚠️ 2026-09-29：审计口径必须与 _match_known_names 的**实际**判据同源
        # （三级容错匹配），否则会把「其实能模糊命中、不会被过滤」的名字报成
        # 「将被过滤」——报告与行为不一致比不报告更误导。
        cast = s.get("characters_in_shot")
        if isinstance(cast, list) and char_names:
            unknown = [str(x).strip() for x in cast
                       if str(x).strip()
                       and not asset_name_match.match(x, char_names, "character")[0]]
            if unknown:
                _add("unknown_cast", f"镜#{idx + 1} 出场角色 {unknown[:4]} 不在角色表，将被过滤", idx)
        itms = s.get("items_in_shot")
        if isinstance(itms, list) and item_names:
            unknown = [str(x).strip() for x in itms
                       if str(x).strip()
                       and not asset_name_match.match(x, item_names, "item")[0]]
            if unknown:
                _add("unknown_cast", f"镜#{idx + 1} 物品 {unknown[:4]} 不在物品表，将被过滤", idx)
        # 4) 字符串字段超 _norm_shots 截断上限（内容会被静默截断）
        for field, limit in _AUDIT_LEN_LIMITS.items():
            val = s.get(field)
            if isinstance(val, str) and len(val) > limit:
                _add("field_overflow", f"镜#{idx + 1} {field} 长 {len(val)} 字 > 上限 {limit}，"
                                       f"将被截断", idx)
            elif val is not None and not isinstance(val, str) and field in (
                    "description", "edit_reason", "audio_cues"):
                # description 等应为字符串；模型偶尔给 dict/list → 会被 str() 强转
                _add("field_type_drift", f"镜#{idx + 1} {field} 类型为 {type(val).__name__}"
                                           f"（应为字符串）", idx)
    return findings


#: A1 景别白名单（与 config.SHOT_TYPES / comfyui_client.SHOT_CAMERA_SPECS 的键逐字对齐）。
#:
#: ⚠️ 2026-09-29 统一：此前这里是硬编码 5 值，而上游 REWRITE_RULES 明确要求
#: 「景别以中景/中近景/近景为主，局部近景用于情感道具回环」—— 模型照规则写出的
#: 「中近景」会被本白名单判成**枚举漂移丢弃**（生成端 H3 表里本就支持中近景，
#: 等于规则与白名单互相打架）。现改为由权威表派生，并**长词优先**排序：
#: 否则「中近景」会被「近景」抢先命中、「大特写」会被「特写」抢先命中。
_SHOT_TYPE_WHITELIST = tuple(sorted(SHOT_TYPES, key=len, reverse=True))

#: 剧本提示词里的景别枚举文案（由权威表派生，杜绝「提示词写五值、白名单认九值」）
_SHOT_TYPE_ENUM_ZH = "/".join(SHOT_TYPES)


def _norm_shot_type(value) -> str:
    """景别归一（借鉴 ArcReel ``_normalize_shot_type``）：命中白名单即用；模型给了复合写法
    （如「特写推入」）则按白名单词包含匹配取一个；都不命中 → 返回空串**并告警**（漂移可见），
    由下游回退解析 ``camera`` 复合串。

    ⚠️ 绝不静默编一个景别：编错的景别会同时污染生成端与质检端（历史坑：曾把脚部俯拍
    猜成「中景」，导致该镜 6 次重试全废、质检永远判不符）。宁可留空让下游按「未指定」处理。
    """
    t = str(value or "").strip()
    if not t:
        return ""
    for k in _SHOT_TYPE_WHITELIST:
        if t == k:
            return k
    for k in _SHOT_TYPE_WHITELIST:
        if k in t:
            return k
    logger.warning("shot_type 未命中景别白名单（枚举漂移）→ 置空，下游回退解析 camera：%r", t)
    return ""


def _norm_camera_motion(value) -> str:
    """运镜归一：限长去空白；若模型把**景别词写进了运镜**（如填「特写推入」）→ 告警并剥掉
    景别词，只留运镜部分。

    为什么要剥：下游 ``h3_prompt_kit._camera_move_en`` 是按运镜表匹配整串的，串里混进
    「特写」会让匹配失准（「特写推入」可能被判成别的运镜）。字段语义必须干净。
    """
    t = str(value or "").strip()[:20]
    if not t:
        return ""
    _stripped = False
    for k in _SHOT_TYPE_WHITELIST:
        if k in t:
            t = t.replace(k, "")
            _stripped = True
    t = t.strip()
    if _stripped:
        logger.warning("camera_motion 混入景别词 → 已剥离，仅保留运镜部分：%r → %r", value, t)
    return t


#: 一镜一动作的**拆镜阈值**：动作节拍 ≥ 此值的镜头会被拆成相邻两镜。
#:
#: ⚠️ 2026-10-06：**默认关闭**（阈值抬到 99 = 任何镜头都拆不动）。
#:   用户口径改为「每集 20~30 镜、每镜 5~6 秒」后，这一刀与目标**直接冲突**：
#:   它把一个镜头劈成两个更短的镜头，正是「分镜切得太细」的来源之一
#:   （实测第 1 集日志：镜14/17/21/40/53/61 共 6 处被拆）。
#:   而且 5~6 秒的镜头本来就**装得下** 2~3 个动作节拍 —— 一个完整动作过程
#:   （抬手→握拳→挥出）本来就该在同一镜里演完，而不是切成三镜各 2 秒。
#:   回滚：`MJSCXT_SPLIT_ACTION_BEATS=3`。
#:
#: 历史依据（保留，说明当初为什么要这刀）：同输入 A/B 实测（素材《剑冢》第一章、
#: shots_target=8）—— 旧规则 10 镜、节拍均值 1.60、≥3 节拍的镜 2/10；新规则 8 镜、
#: 节拍均值 1.12、≥3 节拍的镜 0/8。提示词把「一镜多动作」压到 0，但它仍是软约束，
#: 故当时补了这一刀确定性兜底。**该兜底在 2 秒短镜口径下成立，在 5~6 秒长镜口径下不成立。**
SPLIT_ACTION_BEATS = _env_pos_int("MJSCXT_SPLIT_ACTION_BEATS", 99, floor=1)


# ===================== 参考片风格对齐（2026-09-30）=====================
#
# 目标：让成片观感对齐参考片 92 镜实测口径 —— 运镜只用「固定/轻推/轻摇/跟随/轻手持」，
# 不用大特写/远景/大远景，且约 1/4 的镜头是「只拍手部/道具、不出现完整人脸」的局部插入镜。
#
# 为什么必须是**确定性后处理**而不是只写进提示词：实测提示词能把「一镜多动作」压到 0，
# 但「局部」占比始终为 0 —— 模型嘴上答应、产出不照做（同一模型、同一素材，两轮都是 0/8）。
# 用户要的是成片效果而不是提示词措辞，所以这里直接改镜头表。

#: 参考片运镜白名单：非固定运镜 100% 落在这些词上。
REF_MOTIONS = ("固定", "轻推", "轻摇", "跟随", "轻手持")
#: 非白名单运镜 → 白名单的确定性映射（按关键字匹配，长词优先）
_MOTION_MAP = (("手持", "轻手持"), ("轻推", "轻推"), ("缓推", "轻推"), ("推", "轻推"),
               ("跟随", "跟随"), ("跟", "跟随"), ("轻摇", "轻摇"), ("缓摇", "轻摇"),
               ("摇", "轻摇"), ("移", "固定"), ("升降", "固定"), ("环绕", "固定"),
               ("变焦", "固定"), ("定格", "固定"), ("拉", "固定"))
#: 参考片「越近越静」：特写 100% 固定 → 强制锁定机位（大特写已先收窄为特写）
_FORCE_LOCKED_TYPES = ("特写",)
#: 极端景别收窄：参考片大特写/远景/大远景 0 使用。只收窄到**同距离家族**，
#: 避免「描述写环境为主体、景别却给近景」这种取景与描述互相矛盾的改法。
_TYPE_NARROW = {"大特写": "特写", "远景": "全景", "大远景": "全景"}
#: 「局部」插入镜目标占比（参考片 25%，取略低值留余量）。
#:
#: ⚠️ 2026-10-06：**默认关闭（0.0）**。用户反馈「分镜分得太细 + 九宫格有重复」，
#:   而这一条本身就是**定义级的重复来源**：插入镜由源镜头派生，**同一个道具、同一取景家族**，
#:   只是把叙事压到手上（`_derive_insert_shot` 的 `edit_reason` 原话）——它在成片里就是
#:   「同一个画面再来一次」。实测第 1 集 **56 镜里有 16 镜是插入镜（28.6%）**，
#:   是「镜数虚高 + 画面重复」的单一最大来源。
#:   回滚：`MJSCXT_REF_INSERT_RATIO=0.22`。
REF_INSERT_RATIO = _env_float("MJSCXT_REF_INSERT_RATIO", 0.0, floor=0.0, ceiling=0.5)
#: 插入镜最多把镜头数放大到原来的多少倍（防镜数与总时长失控）
REF_INSERT_MAX_GROWTH = 1.35
#: 长台词拆镜的触发阈值 —— 镜头「装得下内容」所需秒数超过本值即按停顿拆成相邻两镜。
#:
#: ⚠️ 2026-10-06：**6.5 抬到 8.0（= SHOT_DURATION_MAX，即只在真溢出时拆）**。
#:   旧值 6.5 是为「2 秒短镜」口径服务的：把 8 秒的镜头劈成两个 4 秒，节奏更碎。
#:   但在新的 5~6 秒口径下，这一刀**方向反了** —— 劈出来的一半只有 3.3 秒，
#:   会被 `SHOT_DURATION_MIN=5.0` 的下限重新抬到 5 秒，等于用一个 6.6 秒的镜头
#:   换出 10 秒的成片时长（时长无谓变长，镜数无谓变多）。
#:   抬到 8.0 后，只有「台词/动作真的塞不进单镜上限」的镜头才会被拆，其余原样保留。
#:   回滚：`MJSCXT_REF_SHOT_DURATION_MAX=6.5`。
REF_SHOT_DURATION_MAX = _env_float(
    "MJSCXT_REF_SHOT_DURATION_MAX", float(SHOT_DURATION_MAX), floor=1.0,
    ceiling=float(SHOT_DURATION_MAX))


def _shot_type_of(row: dict) -> str:
    """本镜景别：优先 shot_type 字段，其次从 camera 复合串取，**长词优先**。"""
    st = str(row.get("shot_type") or "").strip()
    if st in SHOT_TYPES:
        return st
    cam = str(row.get("camera") or "")
    for name in ("大特写", "特写", "中近景", "近景", "局部", "中景", "全景", "大远景", "远景"):
        if name in cam:
            return name
    return st


def _ref_motion(motion: str) -> str:
    """把任意运镜写法归一到参考片白名单里的词。"""
    m = str(motion or "").strip()
    if not m:
        return "固定"
    if m in REF_MOTIONS:
        return m
    for k, v in _MOTION_MAP:
        if k in m:
            return v
    return "固定"


def _derive_insert_shot(src: dict, prop: str) -> dict:
    """由含道具的源镜头派生一个「局部」插入镜（不调模型、不新增剧情）。

    动作依据取源镜头 description 里**含该道具的短句**；没有就取该镜头里任意动作短句，
    再没有才退化为道具名本身 —— 保证画面内容来自原文，不是凭空编造。
    """
    segs = [x.strip() for x in ACTION_CLAUSE_RE.split(str(src.get("description") or "")) if x.strip()]
    clause = next((x for x in segs if prop in x), "")
    if not clause:
        clause = next((x for x in segs if any(w in x for w in ACTION_WORDS)), "")
    body = clause or prop
    row = dict(src)
    row["shot_type"] = "局部"
    row["camera_motion"] = "固定"
    row["camera"] = "局部固定"
    row["description"] = (body + "；画面只拍手部与" + prop + "，不出现完整人脸")[:200]
    row["visual_detail"] = ""
    row["dialogue"] = []
    row["dialogue_text"] = ""
    row["first_frame"] = ""
    row["last_frame"] = ""
    row["motion"] = ""
    row["items_in_shot"] = [prop]
    row["edit_reason"] = "局部插入镜：把叙事压到道具上，规避人脸崩坏"
    # insert_of 先挂源镜头本身，等 align 重排完 shot_id 再回填 —— 直接写死当前 shot_id
    # 的话，插入镜导致的重排会让这个编号指到别的镜头上（实测踩过）。
    row["_insert_src"] = src
    row.pop("source_unit_ids", None)          # 不重复计入原文覆盖率
    row.pop("duration_overflow_sec", None)
    row["duration"] = estimate_shot_duration(row)
    return row


#: 拆台词后每段的最小字数（低于此值不值得单独成镜，切了也是碎句）
REF_DIALOGUE_MIN_PART = 4
#: 台词拆镜时后半镜「景别推近一档」的映射：同一句话换景别，是参考片最常见的切法之一。
_CLOSER_TYPE = {"全景": "中景", "中景": "中近景", "中近景": "近景", "近景": "特写", "局部": "局部"}


def _split_long_dialogue_shot(row: dict) -> list:
    """把「台词太长、把镜头顶到 REF_SHOT_DURATION_MAX 以上」的镜头按停顿拆成相邻两镜。

    参考片单镜中位 2.08s —— 它的快节奏来自「一句短台词说完就切」，而不是靠运镜。
    本系统旧剧本单镜台词中位需要 8.0s 才念完（实测），镜头被迫拉到 8~12s，
    成片观感就是「一个分镜演半天不切换」。

    拆法：台词在停顿处（，。；！？）一分为二，前半留原镜、后半另起一镜并把景别推近一档 ——
    台词一字不丢、总时长不变，但成片多出一次真实切换。
    """
    if required_shot_duration(row) <= REF_SHOT_DURATION_MAX:
        return [row]
    lines = _dlg_lines(row.get("dialogue"))
    if not lines:
        return [row]
    idx = max(range(len(lines)), key=lambda i: len(str(lines[i].get("text") or "")))
    text = str(lines[idx].get("text") or "")
    # 在标点后切开（保留标点），再找最均衡的切点
    parts = [p for p in re.split(r"(?<=[，。；！？!?])", text) if p.strip()]
    if len(parts) < 2:
        return [row]
    best, best_score = None, None
    for k in range(1, len(parts)):
        pa = "".join(parts[:k]).strip()
        pb = "".join(parts[k:]).strip()
        if len(pa) < REF_DIALOGUE_MIN_PART or len(pb) < REF_DIALOGUE_MIN_PART:
            continue
        score = abs(len(pa) - len(pb))
        if best_score is None or score < best_score:
            best, best_score = (pa, pb), score
    if best is None:
        return [row]
    pa, pb = best
    a, b = dict(row), dict(row)
    la = [dict(x) for x in lines]
    lb = [dict(x) for x in lines]
    la[idx]["text"] = pa
    lb[idx]["text"] = pb
    a["dialogue"] = la
    b["dialogue"] = lb
    a["dialogue_text"] = _dlg_text(la)
    b["dialogue_text"] = _dlg_text(lb)
    # 后半：景别推近一档，形成真正的「切换」而不是同画面重播
    st = _shot_type_of(b)
    closer = _CLOSER_TYPE.get(st, st)
    b["shot_type"] = closer
    b["camera_motion"] = "固定"
    b["camera"] = closer + "固定"
    b["dialogue_split_from"] = int(row.get("shot_id") or 0)
    b.pop("source_unit_ids", None)          # 不重复计入原文覆盖率
    for r in (a, b):
        r["duration"] = estimate_shot_duration(r)
        _need = required_shot_duration(r)
        if _need > SHOT_DURATION_MAX:
            r["duration_overflow_sec"] = round(_need - SHOT_DURATION_MAX, 2)
        else:
            r.pop("duration_overflow_sec", None)
    return [a, b]

def align_shots_to_reference(shots: list, start_id: int = 1) -> list:
    """把镜头表对齐到参考片口径：运镜归一 / 极端景别收窄 / 补足局部插入镜。

    ⚠️ 幂等：已对齐过的镜头表再跑一次不会继续变形（局部占比达标即不再补）。
    ⚠️ 会重排 shot_id（补插入镜后必须连续），故放在集数分配之前调用。
    """
    if not shots:
        return shots
    # ---- 1) 逐镜归一：景别收窄 → 运镜归一 → 特写强制固定 ----
    for s in shots:
        st = _TYPE_NARROW.get(_shot_type_of(s)) or _shot_type_of(s)
        if st:
            s["shot_type"] = st
        mo = _ref_motion(s.get("camera_motion") or s.get("camera") or "")
        if st in _FORCE_LOCKED_TYPES:
            mo = "固定"
        s["camera_motion"] = mo
        s["camera"] = (st + mo) if st else mo
    # ---- 2) 补足「局部」插入镜 ----
    n = len(shots)
    cur = sum(1 for s in shots if _shot_type_of(s) == "局部")
    if n and cur / float(n) < REF_INSERT_RATIO:
        want = int(round((REF_INSERT_RATIO * n - cur) / (1 - REF_INSERT_RATIO)))
        want = max(0, min(want, int(n * REF_INSERT_MAX_GROWTH) - n))
        pri = {"中景": 0, "中近景": 1, "近景": 2, "全景": 3, "特写": 4}
        # ⚠️ 排除 `insert_of`：否则插入镜自己（它也有 items_in_shot）会再次成为来源，
        # 级联插出同道具的重复镜（实测：筑基丹被连插 2 次、画面几乎一样）。
        cands = [s for s in shots
                 if (s.get("items_in_shot") or [])
                 and _shot_type_of(s) != "局部"
                 and s.get("split_part") != 2
                 and not s.get("insert_of")
                 and s.get("description")]
        cands.sort(key=lambda s: pri.get(_shot_type_of(s), 5))
        cand_ids = {id(s) for s in cands}
        # 同一道具最多插 2 次：参考片里局部插入镜本来就是同一道具在不同时刻复现，
        # 但连续两次几乎同画面的插入（实测产物）观感是重复，故设上限。
        prop_used = {}
        out, added = [], 0
        for s in shots:
            out.append(s)
            if added < want and id(s) in cand_ids:
                prop = str((s.get("items_in_shot") or [""])[0])
                if prop and prop_used.get(prop, 0) < 2:
                    prop_used[prop] = prop_used.get(prop, 0) + 1
                    out.append(_derive_insert_shot(s, prop))
                    added += 1
        shots = out
        if added:
            logger.info("参考片风格对齐：补足 %d 个「局部」插入镜（%d → %d 镜）",
                        added, n, len(shots))
    # ---- 2b) 特写/全景 数量上限 ----
    # 参考片 92 镜里特写只 2 个、全景只 3 个（各 2~3%），是「情绪锚点」而非主奏景别。
    # 短集按比例算会归零，故下限取 max(1, ...) —— 每集至少留 1 个锚点镜。
    # 只做「同距离家族」内收窄：特写→近景（取景变化最轻），不够再全景→中景。
    limit = max(1, int(round(0.10 * len(shots))))
    anchors = [s for s in shots if _shot_type_of(s) in ("特写", "全景")]
    if len(anchors) > limit:
        anchors.sort(key=lambda s: 0 if _shot_type_of(s) == "特写" else 1)
        for s in anchors[:len(anchors) - limit]:
            old_t = _shot_type_of(s)
            new_t = "近景" if old_t == "特写" else "中景"
            s["shot_type"] = new_t
            s["camera"] = new_t + str(s.get("camera_motion") or "固定")
            logger.info("参考片风格对齐：锚点景别超限，%s → %s（镜%s）", old_t, new_t, s.get("shot_id"))
    # ---- 2b2) 补情绪锚点特写 ----
    # 参考片 92 镜里 2 个特写（2.2%）=「把观众钉在表情上」的情绪锚点。实测模型常一个特写都
    # 不写（新生成 0/14），全片平铺直叙没有「钉住」的一刻。若整片仍无特写/全景锚点，把
    # 节拍=高潮 的那一镜转成特写（爆点镜画面本就聚焦表情/关键动作，取景收窄不矛盾；
    # 特写 100% 固定在第 1 步已锁死，不会带出运镜）。
    anchors_now = sum(1 for s in shots if _shot_type_of(s) in ("特写", "全景"))
    if not anchors_now:
        climax = next((s for s in shots if str(s.get("beat") or "").strip() == "高潮"), None)
        if climax is not None and _shot_type_of(climax) != "局部":
            old_t = _shot_type_of(climax)
            climax["shot_type"] = "特写"
            climax["camera_motion"] = "固定"
            climax["camera"] = "特写固定"
            logger.info("参考片风格对齐：补情绪锚点特写（原镜%s %s → 特写）", climax.get("shot_id"), old_t)
    # ---- 2c) 主力景别配平 ----
    # 参考片里近景(~26%)与中近景(25%)数量几乎相等，是叙事双主力。模型常把两者写得很偏
    # （实测 40% vs 20%），这里把多的一方挪一部分到少的一方（同属腰部以上取景，变化最轻）。
    nj = sum(1 for s in shots if _shot_type_of(s) == "近景")
    nz = sum(1 for s in shots if _shot_type_of(s) == "中近景")
    if abs(nj - nz) > 1:
        big, small = ("近景", "中近景") if nj > nz else ("中近景", "近景")
        move = (max(nj, nz) - min(nj, nz)) // 2
        for s in [x for x in shots if _shot_type_of(x) == big][-move:]:
            s["shot_type"] = small
            s["camera"] = small + str(s.get("camera_motion") or "固定")
        logger.info("参考片风格对齐：主力景别配平 %s → %s 共 %d 镜", big, small, move)
    # ---- 2c3) 补运镜纹理 ----
    # 参考片 25% 的镜头有运镜（轻推 11% / 跟随 4% / 轻摇 3% / 手持 4%），且全部长在中景。
    # 实测模型会把全片写成 100% 固定（克制过头、画面发死）——把 ~15% 的镜头（只挑中景）
    # 均匀散布标成轻推，补回纹理；近景/局部/中近景/特写不动（参考片「越近越静」）。
    n_mv = len(shots)
    want_moving = int(round(0.15 * n_mv))
    moving = sum(1 for s in shots if str(s.get("camera_motion") or "固定") != "固定")
    if n_mv and moving < want_moving:
        need = want_moving - moving
        med = [i for i, s in enumerate(shots)
               if _shot_type_of(s) == "中景" and str(s.get("camera_motion") or "固定") == "固定"]
        if med:
            for k in range(min(need, len(med))):
                i = med[k * len(med) // max(1, need)]
                shots[i]["camera_motion"] = "轻推"
                shots[i]["camera"] = "中景轻推"
            logger.info("参考片风格对齐：中景补轻推 %d 处（运镜占比 → 约 15%%）", min(need, len(med)))
    # ---- 2c2) 长台词拆镜（对齐参考片切镜节奏）----
    # 参考片单镜中位 2.08s：节奏来自「一句短台词说完就切」。本系统旧剧本单镜台词中位需要
    # 8.0s 才念完（实测），镜头被迫拉到 8~12s —— 这才是「看不出切换」的真正根因。
    # 拆完再走下面的时长收窄，两半才都能落到参考片区间。
    _out, _splits = [], 0
    for s in shots:
        _parts = _split_long_dialogue_shot(s)
        _out.extend(_parts)
        _splits += len(_parts) - 1
    if _splits:
        logger.info("参考片风格对齐：长台词拆镜 %d 处（%d → %d 镜）", _splits, len(shots), len(_out))
    shots = _out
    # ---- 2d) 时长长尾收窄（对齐参考片节奏）----
    # 这才是「看不出切换」的真正来源：参考片中位镜长 2.08s，而本系统旧剧本单镜能拖到 11~12s，
    # 一镜 10 秒不切，观感就是一镜演完好几件事。这里把上限收到参考片最长镜（6.5s）。
    # ⚠️ 只砍长尾，绝不压到台词所需时长以下 —— 压下去配音会溢出（duration_overflow_sec 同理重算）。
    for s in shots:
        need = required_shot_duration(s)
        d = float(s.get("duration") or SHOT_DURATION_MIN)
        target = max(min(d, REF_SHOT_DURATION_MAX), min(need, SHOT_DURATION_MAX))
        s["duration"] = round(max(SHOT_DURATION_MIN, target), 2)
        if need > SHOT_DURATION_MAX:
            s["duration_overflow_sec"] = round(need - SHOT_DURATION_MAX, 2)
        else:
            s.pop("duration_overflow_sec", None)
    # ---- 3) 重排 shot_id（补插入镜后必然错号），并回填插入镜的真实来源镜号 ----
    for i, s in enumerate(shots, start_id):
        s["shot_id"] = i
    for s in shots:
        _src = s.pop("_insert_src", None)
        if _src is not None:
            s["insert_of"] = int(_src.get("shot_id") or 0)
    return shots

def _split_multi_action_row(row: dict) -> list:
    """把一个「一镜多动作」的镜头确定性拆成相邻两镜（不调模型）。

    做法：把 description 按动作短句边界切成两半，让每半只承载一半动作，从而在成片里
    真正产生「切换」。元数据全部保留；切点在「左右两侧动作短句数最接近」处，
    且要求两侧各至少 1 个动作短句、各自文本 ≥4 字，否则原样返回（不硬拆）。

    台词只留在前半：拆镜后两半是同一段表演的先后两个动作，同句台词若复制到后半会被
    配音播两遍。后半的时长由 estimate_shot_duration 重算，不会因为少了台词而失真。

    source_unit_ids 只留在前半：覆盖率校验按它统计原文承载量，两半都带会**重复计数**。

    返回 [row]（未拆）或 [row_a, row_b]（已拆）。
    """
    desc = str(row.get("description") or "").strip()
    if count_action_beats(desc) < SPLIT_ACTION_BEATS:
        return [row]
    segs = [x.strip() for x in ACTION_CLAUSE_RE.split(desc) if x.strip()]
    if len(segs) < 2:
        return [row]
    has_action = [any(w in x for w in ACTION_WORDS) for x in segs]
    total = sum(1 for h in has_action if h)
    best, best_score = None, None
    for k in range(1, len(segs)):
        left = sum(1 for h in has_action[:k] if h)
        right = total - left
        if left < 1 or right < 1:
            continue
        score = abs(left - right)
        if best_score is None or score < best_score:
            best, best_score = k, score
    if best is None:
        return [row]
    a_txt = "，".join(segs[:best])
    b_txt = "，".join(segs[best:])
    # 守卫：两侧都要有实际画面内容。阈值取 4 字而非 6 字 —— 「老人抬起手」（5 字）已是
    # 完整的动作短句，卡在 6 字会让「1 个动作 + 2 个动作」这种最常见的三节拍切法永远拆不开
    # （实测踩过：3 个节拍的镜头一个都没拆掉）。
    if len(a_txt) < 4 or len(b_txt) < 4:
        return [row]

    a, b = dict(row), dict(row)
    a["description"] = a_txt[:200]
    b["description"] = b_txt[:200]
    b["dialogue"] = []
    b["dialogue_text"] = ""
    a["last_frame"] = ""
    b["first_frame"] = ""
    b.pop("source_unit_ids", None)     # 覆盖率只算一次（留在前半）
    a["split_from"] = int(row.get("shot_id") or 0)
    b["split_from"] = int(row.get("shot_id") or 0)
    a["split_part"] = 1
    b["split_part"] = 2
    for r in (a, b):
        r["duration"] = estimate_shot_duration(r)
        need = required_shot_duration(r)
        if need > SHOT_DURATION_MAX:
            r["duration_overflow_sec"] = round(need - SHOT_DURATION_MAX, 2)
        else:
            r.pop("duration_overflow_sec", None)
    logger.info("一镜多动作已拆镜：镜%s → 「%s」/「%s」",
                row.get("shot_id"), a["description"][:30], b["description"][:30])
    return [a, b]

# ---- 场次表头 + 字幕/转场（2026-10-02，对齐标准剧本格式）----
# 参考改编稿用【第一场】外景·无名山巅·黄昏 的场记头组织镜头，并用「字幕」交代
# 时空跳跃（如「春秋蝉，逆转时光。」）。本模块原先只有 location + scene_lighting，
# 没有内外景/时间/场次号，也没有一等字幕字段 —— 这里补齐。
INT_EXT_VALUES = ("内景", "外景", "内外景")
CAPTION_MAX_CHARS = 24
CAPTION_KINDS = ("时间地点", "回溯", "悬念")

_INT_EXT_HINTS = (
    ("内景", ("殿", "阁", "堂", "室", "房", "厅", "廊", "洞", "牢", "狱", "塔",
              "祠堂", "居所", "屋", "密室", "帐", "轿")),
    ("外景", ("山巅", "山顶", "山道", "广场", "野外", "街", "城门", "河", "湖", "海",
              "天空", "悬崖", "荒野", "庭院", "屋顶", "林", "田野", "门外", "坡")),
)

_TIME_HINTS = (
    ("黄昏", ("黄昏", "夕阳", "落日", "残阳", "暮色", "傍晚", "日暮")),
    ("夜", ("夜", "月", "星空", "烛", "灯", "漆黑", "黑暗", "凌晨", "深更")),
    ("清晨", ("清晨", "朝阳", "拂晓", "黎明", "日出", "晨光", "早上")),
    ("日", ("正午", "白日", "日光", "午后", "上午", "晴空")),
)


def derive_int_ext(scene: dict) -> str:
    """从场景名/光影/参考提示词推断内外景（确定性兜底）。

    供**没有 int_ext 字段的旧 bible**（已落盘的存量项目）使用，保证新字段对
    老数据也有值；同时作为模型输出越界时的归一目标。只看空间身份词，不猜剧情。
    """
    blob = " ".join(str((scene or {}).get(k) or "") for k in
                    ("name", "location", "scene_lighting", "appearance",
                     "reference_prompt_zh"))
    for label, hints in _INT_EXT_HINTS:
        if any(h in blob for h in hints):
            return label
    return "外景"


def derive_time_of_day(scene: dict) -> str:
    """从光影基调推断时间（确定性兜底，口径与 scene_lighting 保持一致）。"""
    blob = " ".join(str((scene or {}).get(k) or "") for k in
                    ("scene_lighting", "name", "appearance", "reference_prompt_zh"))
    for label, hints in _TIME_HINTS:
        if any(h in blob for h in hints):
            return label
    return "日"


def normalize_scene_meta(scene: dict):
    """场景的内外景 / 时间：模型给了就用（白名单归一），没给则确定性推导。"""
    scene = scene if isinstance(scene, dict) else {}
    ie = str(scene.get("int_ext") or "").strip()
    if ie not in INT_EXT_VALUES:
        ie = derive_int_ext(scene)
    tod = " ".join(str(scene.get("time_of_day") or "").split())[:8] or derive_time_of_day(scene)
    return ie, tod


def normalize_scenes_meta(scenes: list) -> list:
    """就地补齐所有场景的 int_ext / time_of_day，返回同一列表（便于链式调用）。"""
    for s in (scenes or []):
        if isinstance(s, dict):
            ie, tod = normalize_scene_meta(s)
            s["int_ext"] = ie
            s["time_of_day"] = tod
    return scenes


def scene_heading(scene_no: int, int_ext: str, location: str, time_of_day: str) -> str:
    """标准场记头：【第N场】外景·无名山巅·黄昏（缺项自动回落，不留空占位符）"""
    return "【第%d场】%s·%s·%s" % (int(scene_no or 0), (int_ext or "外景").strip(),
                                   (location or "未标场景").strip(),
                                   (time_of_day or "日").strip())


def assign_scene_numbers(shots: list) -> list:
    """按「连续同一场景」给镜头编场次号，返回场次表（就地写回 shot）。

    标准编剧口径：**一场戏 = 同一地点的一段连续戏**。地点再次出现（闪回、时空
    回溯回到此处）算新的一场 —— 这正是观众需要被点明落点的地方。幂等：重复调用
    只会得到同一结果，因此可在补镜前后各跑一次。
    """
    flow = []
    last_loc = None
    cur = None
    for sh in (shots or []):
        if not isinstance(sh, dict):
            continue
        loc = str(sh.get("location") or "").strip()
        if cur is None or loc != last_loc:
            cur = {
                "scene_no": len(flow) + 1,
                "location": loc,
                "int_ext": str(sh.get("int_ext") or "").strip() or "外景",
                "time_of_day": str(sh.get("time_of_day") or "").strip() or "日",
                "shot_ids": [],
            }
            cur["heading"] = scene_heading(cur["scene_no"], cur["int_ext"], loc,
                                           cur["time_of_day"])
            flow.append(cur)
            last_loc = loc
        sh["scene_no"] = cur["scene_no"]
        sh["scene_heading"] = cur["heading"]
        cur["shot_ids"].append(sh.get("shot_id"))
    for s in flow:
        s["shot_count"] = len(s["shot_ids"])
        s["start_shot_id"] = s["shot_ids"][0] if s["shot_ids"] else None
        s["end_shot_id"] = s["shot_ids"][-1] if s["shot_ids"] else None
    return flow


def collect_scene_flow(shots: list) -> list:
    """只读汇总：按 shot 上已写好的 scene_no 收起场次表（不改动镜头）。

    与 assign_scene_numbers 的分工：后者负责编号并写回镜头；本函数在剧本落盘前
    （覆盖率补镜之后）重新收表，保证补生成的镜头也被计入场次。
    """
    flow = []
    for sh in (shots or []):
        if not isinstance(sh, dict):
            continue
        try:
            no = int(sh.get("scene_no") or 0)
        except (TypeError, ValueError):
            no = 0
        if no <= 0:
            continue
        if not flow or flow[-1]["scene_no"] != no:
            flow.append({
                "scene_no": no,
                "location": str(sh.get("location") or ""),
                "int_ext": str(sh.get("int_ext") or ""),
                "time_of_day": str(sh.get("time_of_day") or ""),
                "heading": str(sh.get("scene_heading") or ""),
                "shot_ids": [],
            })
        flow[-1]["shot_ids"].append(sh.get("shot_id"))
    for s in flow:
        s["shot_count"] = len(s["shot_ids"])
        s["start_shot_id"] = s["shot_ids"][0] if s["shot_ids"] else None
        s["end_shot_id"] = s["shot_ids"][-1] if s["shot_ids"] else None
    return flow


def _norm_caption(raw) -> dict:
    """字幕/转场文本规范化：{text, kind}；空/无效返回 {}（形状恒定，前端好判）。

    ⚠️ 字幕是**后期叠加**的叙事装置（时空落点、回溯、集尾悬念）：
    ① 绝不进画面提示词（prompt_qc 硬原则：画面内不得出现文字）；
    ② 不产生人声、不占配音预算（配音链路只读 dialogue）；
    ③ 是否真的烧进成片，由项目既有开关 subtitle_enabled 决定（默认关闭）。
    """
    if isinstance(raw, str):
        text, kind = raw, ""
    elif isinstance(raw, dict):
        text = raw.get("text") or ""
        kind = str(raw.get("kind") or "").strip()
    else:
        return {}
    text = " ".join(str(text).split())[:CAPTION_MAX_CHARS]
    if not text:
        return {}
    if kind not in CAPTION_KINDS:
        kind = CAPTION_KINDS[0]
    return {"text": text, "kind": kind}


def _norm_shots(raw_shots: list, bible: dict, episodes: int, start_id: int = 1) -> list:
    scenes = [s.get("name") for s in (bible.get("scenes") or []) if isinstance(s, dict)]
    chars = [c.get("name") for c in (bible.get("characters") or []) if isinstance(c, dict)]
    items = [i.get("name") for i in (bible.get("items") or []) if isinstance(i, dict)]
    # P4-A 场景共享光影：把「场景名 → 该场景统一光影基调」建成映射，落到每个镜头上。
    # 同场景所有镜头共享同一 base，下游 comfyui_client 光影推断时优先用它，消除场景内逐镜漂移。
    # 旧剧本 / bible 无 scene_lighting 字段 → 映射值为空串，下游走原有逐镜+情绪兜底（零变化）。
    scene_light_map = {
        str(s.get("name") or "").strip(): str(s.get("scene_lighting") or "").strip()
        for s in (bible.get("scenes") or [])
        if isinstance(s, dict) and str(s.get("name") or "").strip()
    }
    # 场次表头（2026-10-02）：场景的内外景/时间同样按场景名落表，镜头直接取用。
    # 旧 bible 没有这两个字段 → normalize_scene_meta 从场景名/光影确定性推导，老项目也有值。
    scene_meta_map = {
        str(s.get("name") or "").strip(): normalize_scene_meta(s)
        for s in (bible.get("scenes") or [])
        if isinstance(s, dict) and str(s.get("name") or "").strip()
    }
    # 风格：镜头级落一次 style，下游（分镜图 / 视频提示词）才有值可用。
    # 历史缺陷：这里不写 style，导致 comfyui_client 里 shot.get("style", "3D动漫渲染")
    # 永远回落硬编码默认值 —— 用户与总控敲定的风格一个镜头都传不到。
    shot_style = style_kit.normalize_style(bible.get("style"))
    shots = []
    sid = start_id
    dropped_empty = []
    dropped_deprecated = []   # 已废弃字段（DEPRECATED_SHOT_FIELDS）被模型越界输出的次数
    for s in raw_shots:
        if not isinstance(s, dict):
            continue
        # 已废弃字段可见化（见 DEPRECATED_SHOT_FIELDS）：模型若仍吐出 narration 等已废弃字段，
        # 这里显式记账并在本函数末尾打一条 warning —— 让「重新写旁白」被**可见地拒绝/告警**，
        # 而不是靠注释里的君子协定蒙混过关。字段本身仍照旧丢弃，不改变任何业务行为。
        for _dep_field in DEPRECATED_SHOT_FIELDS:
            if str(s.get(_dep_field) or "").strip():
                dropped_deprecated.append(_dep_field)
        # 空壳镜头（画面描述 / 补充细节 / 台词 三者皆空）**必须在这里丢掉**。
        # 历史缺陷：模型偶尔会吐出一条只有 camera/location/emotion 的幽灵镜头
        #（实测《蛊真人》ep02 shot_02：description/visual_detail/dialogue/audio_cues 全空），
        # 本函数原样收下 → 生成期 prompt_qc 判「镜头缺少画面描述」**致命缺陷且不可自愈**
        # → 该镜永远出不了图 → probe_storyboard 永远缺 1 镜 → 整集在分镜步永久卡死，
        # 且用户在界面上拿不到任何可操作的补救入口。
        # 这类镜头不承载任何原文内容（原文覆盖率校验不会因此丢句），丢掉是零损失；
        # 若确实有原文没被承载，后续 coverage 补生成会按原文补回一条**有内容**的镜头。
        # 注意：audio_cues 不参与判定 —— 只有音效没有画面的镜头同样出不了图。
        _has_vis = bool(str(s.get("description") or "").strip()
                        or str(s.get("visual_detail") or "").strip()
                        or str(s.get("storyboard_prompt_zh") or "").strip())
        # task#9 补修：台词判据与下方「画面补齐」同源（归一化后是否含实质文本），
        # 不再用原始值真值——否则 `[{"text":""}]` / `"。！？"` 既骗过闸门又不被补齐。
        _has_dlg = _has_meaningful_dlg(s.get("dialogue"), chars)
        if not (_has_vis or _has_dlg):
            dropped_empty.append(s.get("camera") or s.get("location") or "?")
            continue
        loc = str(s.get("location") or "").strip()
        if scenes and loc:
            # P1-16 修复（场景侧）：匹配不到时**保留原 loc** 并记 warning，绝不静默回落
            # `scenes[0]`。旧行为会把「破败的大殿」这类不在 bible 里的场景名换成「后山」
            # 这类首个场景 —— 环境锚点整集级错位，且日志/界面看不出来。
            #
            # ⚠️ 2026-09-29 统一匹配：旧的 `next((n for n in scenes if n in loc), None)`
            # 是**单向子串 + 取第一个命中** —— scenes 里有「卧室」和「卧室外」而 loc 是
            # 「卧室外面的走廊」时，会**误配**成「卧室」（环境锚点被悄悄换成另一个场景，
            # 比丢图更难发现）。现改为与下游 app.py 完全同一套三级匹配
            # （精确 → 归一化 → **唯一**子串），多候选一律放弃并保留原文 + 告警。
            _k, _lv = asset_name_match.match(loc, scenes, "scene")
            if _k:
                if _lv != asset_name_match.LEVEL_EXACT:
                    logger.info("镜头场景名模糊命中 scenery bible：%r → %r（%s）",
                                loc, _k, _lv)
                loc = _k
            else:
                logger.warning(
                    "镜头场景名未命中 scenery bible：%r（可用：%s）→ 保留原文，"
                    "不静默回落首个场景", loc, "、".join(x for x in scenes if x)[:120])
        if not loc and scenes:
            loc = scenes[0]
        _smeta = scene_meta_map.get(loc) or ("外景", "日")
        row = {
            "shot_id": sid,
            "duration": 5,
            "camera": str(s.get("camera") or "中景").strip()[:20] or "中景",
            # A1 景别/运镜双字段（权威）：新剧本直接给值、下游不再猜；旧剧本两字段为空 →
            # 下游回退解析 camera 复合串，行为与改动前逐字一致（零回归）。
            "shot_type": _norm_shot_type(s.get("shot_type")),
            "camera_motion": _norm_camera_motion(s.get("camera_motion")),
            "location": loc,
            # P4-A 场景共享光影：按解析后的 loc 取该场景统一光影基调（同场景镜头同源，
            # 消除逐镜漂移）。旧剧本 loc 未命中或场景无该字段 → 空串，下游走逐镜/情绪兜底。
            "scene_lighting": scene_light_map.get(loc, ""),
            # 内外景 / 时间（2026-10-02）：供【第N场】场记头与字幕落点提示取用。
            # 旧 bible 无这两字段 → _smeta 已确定性推导，不留空。
            "int_ext": _smeta[0],
            "time_of_day": _smeta[1],
            # description：限长 200 字（前端展示与 prompt 体量控制用）。
            # 但画面细节不丢：原始描述若超长，把超出的部分拆进 visual_detail（分镜图/视频
            # 提示词会把它并回画面主体）。历史缺陷：description 截断 200 字后剩余细节
            # 直接丢失，导致「动作完整、光影明确」的要求只能靠模型猜。
            "description": str(s.get("description") or "").strip()[:200],
            # visual_detail：优先用模型直接输出的字段（分镜 schema 已要求模型把超出
            # description 的更细画面细节写这里）；模型没给时用 _overflow_detail 兜底
            #（description 截断 200 字后的剩余部分）。供 build_storyboard_prompt /
            # h3_prompt_kit 等下游取用。
            "visual_detail": (str(s.get("visual_detail") or "").strip()[:400]
                              or _overflow_detail(s.get("description"), 200)),
            # narration：**已废弃字段（登记于 DEPRECATED_SHOT_FIELDS），此处显式丢弃**；
            # 模型若越界输出会在本函数末尾打一条 warning（可见地拒绝，不是注释君子协定）。
            # 历史缺陷：narration 被当成「心理活动 + 背景补叙 + 环境描写」的公共出口，
            # 再叠加当时的「每镜必须有人声」约束，导致原著所有叙述性文字都变成画外音解说
            #（实测 ep04 旁白 2231 字 ≈ 496 秒，铺在 100 秒画面上 → 4.93x 溢出，尾部被
            # 成片 -shortest 静默截断）。现在剧本阶段不再产出旁白，这里也不再透传模型的
            # 越界输出，保证「成片无旁白」是硬不变量而不是提示词君子协定。
            # 旧剧本文件里残留的 narration 由读取侧（coverage / h3_prompt_kit / tts_client）
            # 按需兼容，这是 DEPRECATED_SHOT_FIELDS 里 narration 唯一的合法消费方。
            # 台词：结构化 [{"speaker","text"}]（分镜阶段直接写明说话人，配音链路直接读取）
            "dialogue": _dlg_lines(s.get("dialogue"), chars, chars),
            "emotion": str(s.get("emotion") or "平静").strip()[:20],
            # edit_reason：剪辑动机（「为什么切到这一镜/承担什么叙事功能」，改写规则 9 新增字段）。
            # 它不解释给观众，是给构图与取舍的依据。限长 50 字（防模型越界输出塞一长段）。
            "edit_reason": str(s.get("edit_reason") or "").strip()[:50],
            # beat：叙事节拍（开场/触发/高潮/收尾），改写规则 11 新增。
            # 只保留四值白名单，越界/未识别值归一到「触发」；供下游节奏分层与时长加成取用。
            "beat": _norm_beat(s.get("beat")),
            "audio_cues": str(s.get("audio_cues") or "").strip()[:60],
            # 字幕/转场（2026-10-02）：后期叠加的时空落点/悬念文字。
            # **不进画面提示词**（画面内不得出现文字的硬原则），也不产生人声；
            # 是否烧进成片由项目既有开关 subtitle_enabled 决定（默认关闭）。
            # 形状恒为 dict，无字幕时为 {}，前端可直接判真假值。
            "caption": _norm_caption(s.get("caption")),
            # 视频提示词：**剧本阶段不再信任模型自写的文本**。
            # 历史缺陷：这里原样保留模型写的「英文画面描述（60 词以内）」，一句无
            # <Picture N> 标签的裸英文，会在生成期把结构化 H3 构建器整个顶掉
            #（app.py 原写法 `shot.get('prompt_h3') or _build_h3_prompt(...)`），
            # 实测全项目 200+ 镜头的结构化提示词数量为 0。
            # 现在只保留「本身已合规」的提示词（例如提示词分析器产出的六段式），
            # 其余一律丢弃，交由生成期 h3_prompt_kit 按当次参考图规范重建。
            "prompt_h3": _keep_valid_h3(s.get("prompt_h3")),
            "style": shot_style,          # ← 风格注入：分镜图/视频提示词的风格来源
            # P1-16 修复（角色侧）：**去掉 `or chars[:1]` 兜底**。旧行为在「镜头角色名与
            # bible 对不上」时静默填入首个角色（通常是主角），使整集以错误角色为外观锚点，
            # 且日志/界面看不出来 —— 同时把下游 S6「禁止静默 take-first」的 `_no_reference`
            # 分支彻底架空（列表恒非空）。现在只保留**精确命中 bible** 的角色，匹配不到即留空，
            # 由下游 `_allocate_storyboard_refs` 的 no_reference 分支显式告警/跳过。
            "characters_in_shot": _match_known_names(
                s.get("characters_in_shot"), chars, "characters_in_shot"),
            # 2026-09-29：与 characters_in_shot 同口径（此前是裸 `i in items` 判据，
            # 模型写的物品名差一个标点就静默丢弃，该物品全程没有设定图且日志无痕）。
            "items_in_shot": _match_known_names(
                s.get("items_in_shot"), items, "items_in_shot", kind="item"),
            # P0-1：首帧/末帧/运动三段（借鉴 ViMax）。限长避免模型越界输出塞一长段；
            # 旧剧本/模型未输出 → 空串，下游 build_storyboard_prompt 回落单段 description，零变化。
            "first_frame": str(s.get("first_frame") or "").strip()[:40],
            "last_frame": str(s.get("last_frame") or "").strip()[:40],
            "motion": str(s.get("motion") or "").strip()[:30],
        }
        # ---- 角色补全（2026-09-30）：画面文本点名了谁，就必须带谁的参考图 ----
        # 模型偶尔漏登记 characters_in_shot（实测分镜 2：description 写「指向右前方羡进
        # 胸口方向」却只登记了赵天霸）→ 生成端/质检端都拿不到羡进的设定图，模型只能凭
        # 想象画脸 → 质检必挂 → 重试循环。这里按画面文本自动补全（只增不减 + 告警）。
        row["characters_in_shot"] = augment_cast_from_text(row, chars, row.get("shot_id"))
        # 覆盖率补生成镜头：保留其承载的原文单元编号，便于覆盖率校验与前端回溯
        src_ids = s.get("source_unit_ids")
        if isinstance(src_ids, (list, tuple)) and src_ids:
            row["source_unit_ids"] = [int(x) for x in src_ids
                                      if isinstance(x, (int, float))][:80]
        if s.get("supplement"):
            row["supplement"] = True
        # 模型失败后的原文兜底镜头：保留标记，前端可提示需人工润色
        if s.get("fallback"):
            row["fallback"] = True
            row["fallback_reason"] = str(s.get("fallback_reason") or "")[:120]
        # 兼容展示字段：台词纯文本（有说话人时 "角色：台词"），前端/提示词按需读取
        row["dialogue_text"] = " ".join(
            (f"{d['speaker']}：{d['text']}" if d.get("speaker") else d.get("text") or "")
            for d in row["dialogue"]
        ).strip()
        # 字幕纯文本：前端与成片字幕渲染直接读它（无字幕时空串）
        row["caption_text"] = str((row.get("caption") or {}).get("text") or "")
        # P0-2 修复（上游补齐）：只有台词、没有画面描述的镜头，在生成期提示词预检里会命中
        #   「镜头缺少画面描述（description / visual_detail / storyboard_prompt_zh 均为空）」
        # 这条**致命且不可自愈**的缺陷（prompt_qc._check_storyboard → fatal）→ 该镜永远
        # 出不了图 → probe_storyboard 永远缺 1 镜 → **整集在分镜步永久卡死**。
        # 这里在上游用**台词上下文**补齐一条画面描述，使该镜带「画面内容」进入生成。
        # ⚠️ 刻意**不写入台词原文**：台词进画面提示词会被模型渲染成字幕（prompt_qc 的硬原则），
        #    且会被 description 复用方（H3/尾帧/图片质检）当成「镜头内容」——那是以台词冒充
        #    画面，属于新缺陷。故只描述「说话人物的表演」，不含任何台词文本。
        # 真·四字段全空（含 task#9 的「结构化空壳 / 纯标点」形状）的幽灵镜头已在上方丢弃。
        # 补齐判据与上方丢弃闸门**同源**（都走 _has_meaningful_dlg），避免两处口径再次漂移：
        # 只有「归一化后确有实质台词文本」才补画面描述；纯标点/空壳既已被丢弃，此处恒假。
        if not (row["description"] or row["visual_detail"]) \
                and _has_meaningful_dlg(s.get("dialogue"), chars):
            _spk = []
            for _d in row["dialogue"]:
                _nm = (_d.get("speaker") or "").strip() if isinstance(_d, dict) else ""
                if _nm and _nm not in _spk:
                    _spk.append(_nm)
            _who = "、".join(_spk) or "人物"
            row["description"] = (
                f"{_who}开口说话（本镜以人物台词表演为主，画面聚焦说话人物的口型与神情）"
            )[:200]
        # 单镜头时长：取「模型给的时长」与「内容实际需要的时长」的**较大值**。
        # 历史缺陷：原实现只要模型给了合法值就直接采用（4~5 秒），完全不看这镜有多少台词
        #   → 长台词硬贴在短画面上，配音沿时间轴溢出到后面几镜，成片尾部被 `-shortest` 静默截掉。
        #   （实测《蛊真人》ep04：21/21 镜都直接采用模型值，与 estimate_shot_duration 的返回值全部不一致）
        auto_dur = estimate_shot_duration(row)
        model_dur = None
        try:
            model_dur = float(s.get("duration"))
        except (TypeError, ValueError):
            model_dur = None
        if model_dur and SHOT_DURATION_MIN <= model_dur <= SHOT_DURATION_MAX:
            row["duration"] = round(max(model_dur, auto_dur) * 2) / 2.0
        else:
            row["duration"] = auto_dur
        # 生成期对账：内容确实塞不进单镜上限时**显式记账**，不静默吞咽。
        # 不在这里私自抬高 SHOT_DURATION_MAX —— QC 侧的 SHOT_DURATION_MAX_OK 是同一个口径，
        # 单方面拉高会让成片被剧本质检判「时长过长」。溢出部分由 dub_mix 的 max_line_sec 变速兜底，
        # 该字段供 dialogue_utils.audit_script 提示用户「这一镜台词写多了，建议拆镜」。
        need = required_shot_duration(row)
        if need > SHOT_DURATION_MAX:
            row["duration_overflow_sec"] = round(need - SHOT_DURATION_MAX, 2)
        # 一镜一动作硬兜底（2026-09-30）：动作节拍 ≥ SPLIT_ACTION_BEATS 的镜头拆成相邻两镜。
        # 提示词在样本内已能把多动作镜压到 0，但它是软约束（换素材/长度就可能反弹），
        # 这里补上确定性的一刀，保证成片真的产生「切换」。
        # 拆出的两半 shot_id 连续占号（前半=原号，后半=下一个），不跳号。
        for _part in _split_multi_action_row(row):
            _part["shot_id"] = sid
            shots.append(_part)
            sid += 1
    if dropped_empty:
        logger.warning("已丢弃 %d 条空壳镜头（无画面描述/细节/台词，出不了图且会卡死整集）：%s",
                       len(dropped_empty), dropped_empty[:12])
    if dropped_deprecated:
        logger.warning(
            "分镜标准化丢弃了模型越界输出的已废弃字段 %s（共 %d 处，见 DEPRECATED_SHOT_FIELDS）："
            "旁白通道已关闭，剧本阶段不再产出 narration；如有叙述性内容，请改写为角色自语台词"
            "（dialogue）或画面描述（description）。",
            "、".join(sorted(set(dropped_deprecated))), len(dropped_deprecated))
    # 参考片风格对齐（2026-09-30）：运镜归一 / 极端景别收窄 / 补足「局部」插入镜。
    # 必须放在集数分配之前 —— 对齐会改变镜数与 shot_id，集数分配要看到最终镜头表。
    shots = align_shots_to_reference(shots, start_id)
    # 场次编号（2026-10-02）：按「连续同一场景」编【第N场】场记头。
    # 必须放在 align_shots_to_reference 之后 —— 对齐会改变镜数与 shot_id，
    # 场次号要看到最终镜头表；幂等，落盘前还会再收一次（含覆盖率补镜）。
    assign_scene_numbers(shots)
    # 分配集数
    n = len(shots)
    if n:
        per = max(1, math.ceil(n / max(1, episodes)))
        for i, sh in enumerate(shots):
            sh["episode"] = min(episodes, i // per + 1)
    return shots


def _run_full_coverage_check(client, novel_text: str, script: dict, reports=None,
                             warnings: list = None, continuity_dir: str = None,
                             project_key: str = None, max_rounds: int = COVERAGE_MAX_ROUNDS,
                             total_steps: int = 0) -> dict:
    """整本剧本的原文覆盖率校验：逐句核对原文是否被镜头承载 → 遗漏补生成 → 复检。

    只增不删：补生成只向 shots 追加镜头，不改写、不替换既有镜头。
    校验失败时降级（记 warning 并沿用原覆盖率摘要），不阻断剧本落盘。
    """
    warnings = warnings if warnings is not None else []
    try:
        import coverage as coverage_mod
        if reports:
            reports("coverage", total_steps, total_steps,
                    f"原文覆盖率校验（逐句核对 {len(novel_text or '')} 字是否被镜头承载）…", 98)
        rep = coverage_mod.run_coverage_check(
            client, novel_text, script, episode_no=1, threshold=None,
            max_rounds=max_rounds, events=None,
            continuity_dir=continuity_dir, project_key=project_key, save=True)
        if rep.get("supplement_shots"):
            apply_episode_schema(script)   # 补生成镜头后刷新镜头数 / 时长
        if reports:
            reports("coverage", total_steps, total_steps,
                    f"原文覆盖率：情节级 {rep.get('plot_coverage_percent')}%、"
                    f"细节级 {rep.get('detail_coverage_percent')}%，遗漏 "
                    f"{rep.get('missing_count')} 条、补生成 {rep.get('supplement_shots')} 镜", 99)
        # ---- P0-3 剧本↔原著一致性：整本单集路径（1 集 = 整本小说）无单章比较基准，
        #      锚定与要素覆盖显式跳过，只做元信息泄漏扫描 + 命中镜头定向重写。
        try:
            import script_consistency as sc_mod
            sc_rep = sc_mod.run_script_consistency_check(
                client, script, novel_meta=None, chapter_text="", chapter=None,
                episode_no=1, auto_fix=True, events=None,
                continuity_dir=continuity_dir, project_key=project_key, save=True,
                skip_anchor=True, skip_elements=True)
            if reports:
                reports("consistency", total_steps, total_steps,
                        f"剧本一致性：元信息泄漏 "
                        f"{(sc_rep.get('leak') or {}).get('hit_count')} 处，"
                        f"定向修复 {sc_rep.get('fix_rounds')} 轮"
                        f"（{'已修复' if sc_rep.get('fixed') else '留告警'}）", 99.5)
        except Exception as e:  # noqa: BLE001
            note = (f"剧本一致性校验失败（已跳过，剧本仍按原结果落盘）："
                    f"{type(e).__name__}: {str(e)[:200]}")
            warnings.append(note)
            logger.warning(note)
        return coverage_mod.summary_for_meta(rep, rep.get("report_path")) \
            or (script.get("metadata") or {}).get("coverage") or {}
    except Exception as e:  # noqa: BLE001
        note = (f"原文覆盖率校验失败（已跳过，剧本仍按全量分块生成）："
                f"{type(e).__name__}: {str(e)[:200]}")
        warnings.append(note)
        logger.warning(note)
        return (script.get("metadata") or {}).get("coverage") or {}


# 整本转剧本路径已随「一章一集」拍板移除（2026-10-05）；集级入口见 convert_chapter_with_continuity


# ===================== 按章节分集生成（每章一集） =====================

CHAPTER_MIN_CHARS = 300          # 过短章节阈值（低于此值仅提示，不做合并）
# ⚠️ 2026-09-25 实测修正（与上方 CHUNK_CHARS 同因，两条路径必须同口径）：
#    3000 字 / CHARS_PER_SHOT(120) ≈ 25 镜/块 > MAX_SHOTS_PER_CHUNK(24) 的单次调用
#    响应体红线 → 预劈半变成常态。改为 2400 字 ≈ 20 镜/块，首次调用即合规。
#    （⚠️ 现值注记：`CHARS_PER_SHOT` 已于 2026-10-06 改为 240，故 2400 字 ≈ 10 镜/块；
#     本条的「120」是当时值，技术依据不变、只是现已更宽裕。）
CHAPTER_CHUNK_CHARS = 2400       # 单章二次分块粒度（字符）
CHAPTER_MAX_SUBCHUNKS = 12       # [兼容保留] 旧「单章最多子块数」上限；全量覆盖后不再抽样，仅前端旧字段展示
CHAPTER_MIN_SUBCHUNK_CHARS = 200 # 章内子块最小字数（过短的尾块并入前一块，保证不丢正文）


class EpisodeNotFoundError(Exception):
    """指定剧集尚未生成"""


def _split_oversized(unit: str, limit: int) -> list:
    """把超长子单元按句末标点/换行再切，仍超限则按固定长度硬切（保证不丢字）"""
    parts, buf = [], ""
    for piece in re.split(r"(?<=[。！？；!?;])|\n", unit or ""):
        if not piece:
            continue
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(piece) > limit:
            parts.append(piece[:limit])
            piece = piece[limit:]
        buf = piece
    if buf:
        parts.append(buf)
    return [p for p in parts if p.strip()] or [unit]


def build_chapter_chunks(chapter_text: str, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                         max_subchunks: int = CHAPTER_MAX_SUBCHUNKS):
    """单章过长时做二次分块（按段落聚合 → 超长段落按句硬切 → 尾部并块 → 全量覆盖）。

    与整本分块（build_chunks）不同：本函数不做 MIN_CHUNK_CHARS 过滤，
    保证章节正文首尾不被丢弃。

    **全量覆盖**：分块只用于控制单次 prompt 体量，块与块连续无缝，
    所有子块都会送模型并合并，不做首尾保留式抽样（max_subchunks 仅作兼容保留）。

    返回 (全部子块, 送模型的子块, 送模型子块下标 1-based)：后两者在语义上等于全部子块。
    """
    text = (chapter_text or "").strip()
    if not text:
        return [], [], []

    units = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        units.extend([para] if len(para) <= chunk_chars else _split_oversized(para, chunk_chars))
    if not units:
        units = _split_oversized(text, chunk_chars)

    chunks, buf = [], ""
    for u in units:
        if buf and len(buf) + len(u) + 1 > chunk_chars:
            chunks.append(buf)
            buf = u
        else:
            buf = f"{buf}\n{u}" if buf else u
    if buf:
        chunks.append(buf)
    # 尾部过短则并入前一块：避免产生无意义碎片，同时不丢正文
    if len(chunks) >= 2 and len(chunks[-1]) < max(CHAPTER_MIN_SUBCHUNK_CHARS, chunk_chars // 3):
        chunks[-2] = chunks[-2] + "\n" + chunks[-1]
        chunks.pop()

    out = [{"text": c.strip(), "char_count": len(c.strip()),
            "from_chapter": None, "to_chapter": None} for c in chunks if c.strip()]
    for i, c in enumerate(out):
        c["index"] = i + 1
        c["total"] = len(out)
        c["title"] = f"第{i + 1}子块"
    # 全量覆盖：所有子块连续无缝地送模型并合并，不做任何抽样
    all_idx = [c["index"] for c in out]
    return out, list(out), all_idx


def estimate_subchunks(char_count, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                       max_subchunks: int = CHAPTER_MAX_SUBCHUNKS) -> int:
    """不读正文即可估算的单章二次分块数量（与 build_chapter_chunks 的尾部并块行为对齐）

    全量覆盖改造后不再设子块数上限：返回值即实际需要送模型的子块数。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    est = int(math.ceil(n / float(chunk_chars)))
    if est >= 2 and (n - (est - 1) * chunk_chars) < max(CHAPTER_MIN_SUBCHUNK_CHARS, chunk_chars // 3):
        est -= 1
    return max(1, est)


def estimate_shots_for_chars(char_count) -> int:
    """按内容体量估算镜头数（全量覆盖：约每 CHARS_PER_SHOT 字 1 个镜头，不为凑时长砍内容）"""
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    return max(SHOTS_PER_CHUNK_MIN, int(math.ceil(n / float(CHARS_PER_SHOT))))


def propose_chapter_split(chapter: dict, text: str = "",
                          max_sec: int = None, fixed_parts: int = None,
                          max_shots: int = None) -> dict:
    """P2-2 渐进式分集 + 人会确认——把 :func:`split_chapter_for_episodes` 包装成
    一份「分集断点提议」payload，供用户**在触发整集渲染前逐条核对**，而非盲信任自动
    切章（整集 H3 连续渲染 7 小时资源红线，误切代价极高）。

    纯确定性、无 LLM、无 I/O；与底层 :func:`split_chapter_for_episodes` 完全同源
    （切点吸附语义边界、时长口径走 EPISODE_PLAN_SEC_PER_SHOT 实测密度），
    只是把「切点落在原文哪里 + 每集约几镜几秒 + 是否超红线」摊平给人看。

    Args 与 :func:`split_chapter_for_episodes` 一致，另加：
      - text：完整正文（仅用于取每个单元切点前后的原文预览；为空时 preview=""，不影响切分）。
      - preview_chars：每单元边界前后取的原文预览字符数（默认 30，足够看清切点语义）。

    返回 dict（含 4 个核心键，前端可整块消费）：
      {
        "needs_confirm": True/False,    # parts>1 或 fixed_parts>1 即需人确认（默认 1 集不触发）
        "message": str,                  # 一句话人类可读建议（含每集时长/上限红线）
        "units": [                       # 每个单元的切点细节，按 part 升序
          {
            "part": int, "start": int, "end": int, "char_count": int,
            "preview": str,             # 切点前后原文（前后各 preview_chars，越界自动截短）
            "est_shots": int,           # 按 EPISODE_PLAN_CHARS_PER_SHOT 实测密度预估
            "est_sec": float,           # 预估成片秒数（EPISODE_PLAN_SEC_PER_SHOT 口径）
            "over_redline": bool,       # est_shots > MAX_SHOTS_PER_EPISODE（单集资源红线）
          }, …
        ],
      }
    """
    seg = text or ""
    raw_units = split_chapter_for_episodes(chapter, text=seg, max_shots=max_shots,
                                            fixed_parts=fixed_parts, max_sec=max_sec)
    # 每个单元：切点前后原文预览（越界自动截短）+ 预估镜数/秒数 + 红线标记
    preview_chars = 30
    units: list = []
    for u in raw_units:
        s, e = int(u["start"]), int(u["end"])
        pv_lo = seg[max(0, s - preview_chars): s]
        pv_hi = seg[e: e + preview_chars] if seg else ""
        est_shots = plan_shots_for_chars(int(u.get("char_count") or 0))
        est_sec = estimate_episode_sec(est_shots)
        units.append({
            "part": int(u["part"]), "start": s, "end": e,
            "char_count": int(u.get("char_count") or 0),
            "preview": pv_lo + pv_hi,
            "est_shots": est_shots, "est_sec": round(est_sec, 1),
            "over_redline": est_shots > MAX_SHOTS_PER_EPISODE,
        })
    total_parts = int(raw_units[0]["parts"]) if raw_units else 1
    over = [u for u in units if u["over_redline"]]
    over_total = (len(over) / len(units)) if units else 0.0
    total_est_sec = round(sum(u["est_sec"] for u in units), 1)
    if total_parts > 1:
        # 拆集了 → 人需逐条确认「切点是否合理、每集时长是否可接受」
        msg = (f"本章 {len(seg) if seg else int(chapter.get('char_count') or 0)} 字，"
               f"按默认口径拆成 {total_parts} 集（每集预计 "
               f"{(total_est_sec / total_parts) / 60.0:.1f} 分钟，单集上限 "
               f"{EPISODE_MAX_SEC / 60.0:.0f} 分钟）；"
               f"共 {len(over)} 集预估超 {MAX_SHOTS_PER_EPISODE} 镜红线。"
               f"请逐条核对每个单元切点（单元间原文无重叠、无丢失）后再生成。")
    else:
        msg = f"本章预估 1 集（约 {total_est_sec / 60.0:.1f} 分钟）" \
              + ("；超过单集时长上限，建议手动拆 2 集" if over else "")
    return {
        "needs_confirm": total_parts > 1,
        "total_parts": total_parts,
        "units": units,
        "message": msg,
    }


def estimate_episode_shots(char_count) -> int:
    """按**章字数**预估整集镜头数（不读正文；口径必须与 convert_chapter_to_script 对齐）。

    用于「这一章要不要拆成多集」的前置规划。口径不对齐就会出问题：
    规划说「不超」，真生成却超 → 白拆；规划说「要拆」，真生成远不到 → 无谓拆碎。

    真实生成的口径是：先按 CHAPTER_CHUNK_CHARS 把本章切成子块，再对**每块**取
    `estimate_shots_for_chars(块字数)`，最后各块求和。这里用 `estimate_subchunks`
    直接拿到子块数（它刻意与 build_chapter_chunks 的尾部并块行为对齐），
    再按平均块长估算每块镜头数。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    n_chunks = estimate_subchunks(n) or 1
    return n_chunks * estimate_shots_for_chars(max(1, n // n_chunks))


def estimate_episode_sec(shot_count) -> float:
    """按预估镜头数换算**预估成片秒数**（不读正文，规划用）。

    `shots * EPISODE_PLAN_SEC_PER_SHOT`。与 `build_episode_stats` 的 duration 口径
    刻意分开：后者汇总的是**逐镜精算**后的真实秒数（要读镜头内容），
    这个只是「还没生成正文时」的用量级估值，够用来算集数即可。
    """
    return round(max(0, int(shot_count or 0)) * EPISODE_PLAN_SEC_PER_SHOT, 2)


def plan_shots_for_chars(char_count) -> int:
    """**拆集规划专用**的镜头数预估（按实测密度 `EPISODE_PLAN_CHARS_PER_SHOT`）。

    ⚠️ 与 `estimate_shots_for_chars` 的区别是本函数存在的全部理由，别混用：
      · `estimate_shots_for_chars` = 按 `CHARS_PER_SHOT`（**提示词引导值**；2026-10-06 起为
        240，沿革见该常量定义处）。它喂给模型写提示词、也用作覆盖率容量基准 ——
        旧口径（120）下**偏乐观**，实测只有实际的 1/3。
      · 本函数 = 按 `EPISODE_PLAN_CHARS_PER_SHOT=26`（**实测密度**，2026-09-26 起）。
        只用于「这一章要拆几集」的前置规划 —— **必须贴近实际**，否则拆集判据失效。

    实测依据见 `EPISODE_PLAN_CHARS_PER_SHOT` 的注释（2361 字 → 实测 63 镜）。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    return max(SHOTS_PER_CHUNK_MIN, int(math.ceil(n / float(EPISODE_PLAN_CHARS_PER_SHOT))))


def estimate_episode_plan_sec(char_count) -> float:
    """按章字数给出的**预估成片秒数**（规划口径，实测密度）。

    这是「按章节内容动态调整集数」的判据来源：`estimate_episode_parts` 用它对比
    `EPISODE_MAX_SEC` 决定拆几集。
    """
    return estimate_episode_sec(plan_shots_for_chars(char_count))


def estimate_episode_parts(char_count, max_sec: int = None) -> int:
    """一章需要拆成几集（**按预估成片时长**判断，2026-09-26 起）。

    规则：`parts = ceil(预估秒数 / max_sec)`，`max_sec` 默认 `EPISODE_MAX_SEC`（180 秒）。
    预估不超上限就是 1 集 —— 集数完全由「这一章能拍几分钟」决定。

    ⚠️ 预估秒数走 `estimate_episode_plan_sec`（**实测密度 26 字/镜**），
    而不是 `estimate_episode_shots`（提示词口径 120 字/镜）。用错口径会让判据失效 ——
    本次实测踩到：按 120 字/镜估算，42 章全部落在 180 秒内 → 集数永远拆不动，
    而实际每集约 6 分钟。详见 `EPISODE_PLAN_CHARS_PER_SHOT` 注释。

    ⚠️ 与旧的「按镜数 / MAX_SHOTS_PER_EPISODE(78)」判据的区别见 EPISODE_MAX_SEC 上方注释。
    `MAX_SHOTS_PER_EPISODE` 现在退化为**另一条独立的技术红线**（H3 资源），
    不再承担「拆集」职责；两者取更碎的那个（在 `convert_chapter_to_script` 里有兜底）。

    ⚠️ 短章不拆：字数低于 `CHAPTER_MIN_CHARS` 时退回 1 集 —— 拆开只会得到
    「每集几十字、几秒成片」的碎片，破坏叙事完整性（历史缺陷，见 EPISODES_PER_CHAPTER 注释）。
    """
    n = max(0, int(char_count or 0))
    if not n or n < CHAPTER_MIN_CHARS:
        return 1
    cap_sec = int(max_sec or EPISODE_MAX_SEC)
    if cap_sec <= 0:
        return 1
    est_sec = estimate_episode_plan_sec(n)
    if est_sec <= cap_sec:
        return 1
    return max(2, int(math.ceil(est_sec / float(cap_sec))))


#: 拆章时切点吸附的候选边界（按优先级从高到低：段落空行 → 句末 → 换行）
_CUT_MARKERS = ("\n\n", "\n", "。", "！", "？", "；", "…", "”", "』", "」")


def _snap_cut(text: str, ideal: int, lo: int, hi: int, prev: int) -> int:
    """把理想切点吸附到最近的语义边界（段落空行 > 换行 > 句末标点）。

    约束：返回值必须严格落在 `(prev, hi)` 内，否则跨段重叠或丢字。
    找不到合适边界时原样返回 `ideal`（同样被夹进合法区间）。
    """
    ideal = int(ideal)
    ideal = max(int(prev) + 1, min(ideal, int(hi) - 1))
    if not text:
        return ideal
    span = max(1, int(hi) - int(lo))
    window = max(80, span // 12)
    w_lo = max(int(prev) + 1, ideal - window)
    w_hi = min(int(hi) - 1, ideal + window)
    if w_hi <= w_lo:
        return ideal
    chunk = text[w_lo:w_hi]
    for marker in _CUT_MARKERS:
        best = None
        pos = chunk.find(marker)
        while pos != -1:
            cut = w_lo + pos + len(marker)      # 切在标记**之后**：标记留在前一段
            if prev < cut < hi:
                if best is None or abs(cut - ideal) < abs(best - ideal):
                    best = cut
            pos = chunk.find(marker, pos + 1)
        if best is not None:
            return best
    return ideal


def split_chapter_for_episodes(chapter: dict, text: str = "",
                               max_shots: int = None,
                               fixed_parts: int = None,
                               max_sec: int = None) -> list:
    """把一章拆成 1..N 个「拍摄单元」（一个单元 = 一集）。

    拆分规则：**按预估成片时长自动判断**（2026-09-26 起）
    ---------------------------------------------------
    1. **内容份数（主规则）**：按 `estimate_episode_parts` —— 先由章字数预估镜头数，
       再乘 `EPISODE_PLAN_SEC_PER_SHOT` 得到预估成片秒数，除以 `max_sec`
       （默认 ``EPISODE_MAX_SEC``=180 秒 = 3 分钟）向上取整。
       预估不超上限就是 1 集，超限才拆。集数因此**完全由「这一章能拍几分钟」决定**。
    2. **固定下限（可选）**：``fixed_parts``（默认取全局 ``EPISODES_PER_CHAPTER``，
       当前 =1 即关闭）作为「每章至少拆 N 集」的兜底下限。设为 2 可强制短章也拆；
       取 ``max(本下限, 内容份数)``，因此调大它只可能更碎，绝不会更碎不动。

    ⚠️ 为什么不再按镜数判据（历史实现）：
        旧实现是 `ceil(预估镜数 / max_shots)`，`max_shots` 默认 `MAX_SHOTS_PER_EPISODE`=78。
        78 镜 ≈ 6.5 分钟成片，而产品口径是「一集 1-2 分钟、最长 3 分钟」——
        实测 1766 字/章的《逆天系统》只估 15 镜，**永远触发不了拆集**，
        于是「按章节内容动态调整集数」形同虚设（42 章 → 42 集，每集 6 分钟）。
        改按时长判据后同一本书变成每章 2~3 集。
        `max_shots` 参数**保留但仅作为兜底**（时长口径算不出时回退），旧调用方不致报错。

    设计要点
    --------
    - **不拆时零影响**：预估不超限且下限为 1 → 返回单段且 `start/end` 原样不变，
      老项目（一章一集）行为完全不变。
    - **过短章不拆**：章字数 < ``CHAPTER_MIN_CHARS``（300）时拆开没有意义（每段
      只剩一两百字），直接退回 1 集。
    - **不丢字、不重叠**：各段首尾相接，`[start, end)` 逐段连续覆盖原章区间。
    - **切点吸附语义边界**（段落空行 > 换行 > 句末标点，在理想切点 ±span/12 内找），
      避免把一句话 / 一段动作劈到两集（跨集是独立生成，劈开会导致语义断裂）。
    - `text` 为空时退化为按字数等分硬切 —— 仍然不丢字。

    返回 `[{"part": 1..N, "parts": N, "start": int, "end": int, "char_count": int}]`。
    """
    # 2026-10-03 用户决策（收口）：默认「一章 = 一集」，不按内容体量拆分。
    # ⚠️ 开关放在**本函数入口**而非各调用方 —— 托管/手动批量/断点提议预览
    # （split-plan）/run-once 全部经由这里，口径必然一致（此前只改 autopilot
    # 调用方时，「前置解析」卡片仍按旧口径提「拆 N 集/需人工确认」，口径打架）。
    # 设 env MJSCXT_EPISODE_SPLIT=1 可恢复按内容体量拆分。
    if str(os.environ.get("MJSCXT_EPISODE_SPLIT") or "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return [{"part": 1, "parts": 1,
                 "start": int(chapter.get("start") or 0),
                 "end": int(chapter.get("end") or 0),
                 "char_count": int(chapter.get("char_count") or 0)}]
    seg_start = int(chapter.get("start") or 0)
    seg_end = int(chapter.get("end") or 0)
    if seg_end < seg_start:
        seg_end = seg_start
    n_chars = int(chapter.get("char_count") or (seg_end - seg_start) or 0)

    # 主规则：按预估成片时长（见上方注释）。max_sec 未显式给时用全局口径。
    if max_sec:
        parts = estimate_episode_parts(n_chars, max_sec=max_sec)
    else:
        parts = estimate_episode_parts(n_chars)
    # 兜底：时长口径因常量异常算不出时，退回旧的镜数判据（保证「总会拆」不会变成「永不拆」）。
    if parts <= 1 and max_shots:
        cap = int(max_shots or 0)
        if cap > 0:
            est = estimate_episode_shots(n_chars)
            if est > cap:
                parts = max(2, int(math.ceil(est / float(cap))))
    # 可选下限（默认 1 = 关闭）：仅当显式要求「每章至少 N 集」时才抬高。
    # 取更碎的那个，保证每段仍 ≤ 上限。
    n_fixed = EPISODES_PER_CHAPTER if fixed_parts is None else int(fixed_parts or 0)
    if n_fixed > 1 and n_chars >= CHAPTER_MIN_CHARS:
        parts = max(parts, n_fixed)
    total = seg_end - seg_start
    if parts <= 1 or total <= 0:
        return [{"part": 1, "parts": 1, "start": seg_start, "end": seg_end,
                 "char_count": n_chars}]

    # ⚠️ 等分算出的 parts 是「理想份数」，但切点要**吸附到语义边界**（段落/换行/句末），
    #    吸附会让各段长度不均匀 —— 实测出现过 14269 字拆 23 段时，有 8 段被顶到
    #    210 秒（超过 180 秒上限），即**理想份数不够**。
    #    处置：切完**实测**每段秒数，**按越限段数**加份重切（一轮就大致补齐），
    #    循环到没有越限段为止（设安全上限防死循环）。
    #    为什么不做「事后修剪越限段」：修剪会在句中劈开句子，破坏「跨集不劈句」不变量。
    #    加份数重切则保持所有不变量（首尾相接、不劈句、不丢字），只是集数更碎 ——
    #    更碎是安全的（原文不丢、每集仍是完整叙事单元），不拆够才危险。
    cap_sec = int(max_sec or EPISODE_MAX_SEC)
    #: 段长低于该字数时语义吸附会**无法收敛**（切点挤到同一句末、加份指数爆炸）——
    #:   实测 max_sec=10 的极端探针下，400 字理想段长 33 字 < 40，吸附窗口(80 字)远大于
    #:   段长，每轮加份后 ideal 间隔更小但句号只有 18 个，切点无法细分 → parts 涨到 5889
    #:   仍越限。此时放弃语义吸附、改**按字数硬切**（保证每段字数均匀、不越界）。
    _SNAP_MIN_CHARS = 40
    _max_parts = max(parts, 4096)  # 安全上限（正常远达不到，防死循环）
    while parts < _max_parts:
        if total / float(parts) < _SNAP_MIN_CHARS:
            break                   # 段长太小，语义吸附无法收敛，交给下面的硬切兜底
        _b = [seg_start]
        for i in range(1, parts):
            ideal = seg_start + int(round(total * i / float(parts)))
            _b.append(_snap_cut(text, ideal, seg_start, seg_end, _b[-1]))
        _b.append(seg_end)
        if cap_sec <= 0:
            break
        _over = sum(
            1 for i in range(parts)
            if estimate_episode_plan_sec(max(0, _b[i + 1] - _b[i])) > cap_sec)
        if _over == 0:
            break
        parts += max(1, _over)     # 越限几段就加几份，快速收敛（而不是每次只 +1）

    # ⚠️ 用**最终确定的 parts** 重新生成一次 bounds：上面的循环可能在「段长 < 阈值」或
    #    「parts 超上限」时直接退出，此时局部 `_b` 对应旧 parts（比最终 parts 少切点）
    #    → 直接切片会 IndexError（实测踩到）。这里重算保证两者严格同步。
    #    段长 >= 阈值走语义吸附（正常场景）；< 阈值走硬切（极端场景，保不崩溃）。
    _final_seg = total / float(parts)
    bounds = [seg_start]
    for i in range(1, parts):
        ideal = seg_start + int(round(total * i / float(parts)))
        if _final_seg >= _SNAP_MIN_CHARS:
            bounds.append(_snap_cut(text, ideal, seg_start, seg_end, bounds[-1]))
        else:
            bounds.append(ideal)   # 硬切：不吸附语义边界，按字数均分
    bounds.append(seg_end)

    out = []
    for i in range(parts):
        s, e = bounds[i], bounds[i + 1]
        out.append({"part": i + 1, "parts": parts, "start": s, "end": e,
                    "char_count": max(0, e - s)})
    return out


def chapter_advice(char_count: int, subchunk_count: int = 0) -> dict:
    """给单章的规模提示（过短只提示不合并；全量覆盖：内容体量决定镜头数）

    2026-09-26：提示语改为按**产品时长口径**给出——直接告诉用户「这一章会拆成几集、
    每集大约几分钟」，而不是只说镜数（用户关心的是集数与时长，不是镜数）。
    """
    n = int(char_count or 0)
    too_short = n < CHAPTER_MIN_CHARS
    too_long = n > CHAPTER_CHUNK_CHARS
    est_shots = estimate_shots_for_chars(n)
    # ⚠️ 展示给用户的集数/时长必须用**实测密度**（plan_shots_for_chars），
    #    否则界面会告诉用户「这一章 2 分钟」而实际出 6 分钟（本次实测踩到的坑）。
    plan_shots = plan_shots_for_chars(n)
    est_sec = estimate_episode_sec(plan_shots)
    est_parts = estimate_episode_parts(n)
    _minutes = f"{est_sec / 60.0:.1f} 分钟"
    if too_short:
        msg = (f"本章仅 {n} 字，内容偏短，成片约 {est_sec:g} 秒 / 约 {plan_shots} 个镜头"
               f"（按需求不做自动合并）")
    elif est_parts > 1:
        msg = (f"本章 {n} 字，预估成片约 {_minutes}（{est_sec:g} 秒 / 约 {plan_shots} 镜），"
               f"超过单集上限 {EPISODE_MAX_SEC / 60.0:g} 分钟 → 将自动拆成 {est_parts} 集"
               f"（每集约 {est_sec / est_parts / 60.0:.1f} 分钟）")
    elif too_long and subchunk_count >= 2:
        msg = (f"本章 {n} 字，超过 {CHAPTER_CHUNK_CHARS} 字，将自动二次分块为 "
               f"{subchunk_count} 个子块全量覆盖送模型（不抽样、不丢内容），"
               f"预计约 {plan_shots} 个镜头 / 约 {est_sec:g} 秒成片")
    elif too_long:
        msg = (f"本章 {n} 字，略超 {CHAPTER_CHUNK_CHARS} 字，按单块全量处理，"
               f"预计约 {plan_shots} 个镜头 / 约 {est_sec:g} 秒成片")
    else:
        msg = (f"本章 {n} 字，单块即可完成，预计约 {plan_shots} 个镜头 / "
               f"约 {est_sec:g} 秒成片（镜头数随内容体量自动扩展）")
    return {"char_count": n, "too_short": too_short, "too_long": too_long,
            "subchunks": subchunk_count, "estimated_shots": est_shots,
            "estimated_plan_shots": plan_shots,
            "estimated_sec": est_sec, "estimated_parts": est_parts,
            "message": msg}


def episode_project_name(novel_name: str, episode_no) -> str:
    """每集独立项目名：后续资产/分镜/视频按「某一集」继续走通，互不覆盖"""
    return f"{safe_project_name(novel_name)}_第{int(episode_no)}集"


def convert_chapter_to_script(client, novel_meta: dict, novel_text: str, chapter: dict,
                              style: str = "3D动漫渲染", target_shots: int = 12,
                              episode_no: int = 1, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                              max_subchunks: int = CHAPTER_MAX_SUBCHUNKS,
                              progress_cb=None, continuity_ctx: dict = None,
                              cache_dir: str = "") -> dict:
    """把「一章」转成一集 A 版剧本（每章一集，独立落盘）

    continuity_ctx（跨集连贯性方案 A/B/C）：由 continuity.build_continuity_context 组装，
    包含项目级设定库、上集摘要卡、衔接契约、项目级风格指南、金句清单、口吻词典与运镜术语表；
    传入后本集生成将带着跨集上下文，且 style_guide 改用项目级唯一配置。

    cache_dir 非空时，提炼 / 分镜两步启用断点缓存：重跑时命中已完成的块即跳过模型调用，
    使「网关偶发挂起 → pipeline 整步重试」不再每次都从头烧 20+ 分钟（见文件上方缓存说明）。
    """
    def report(phase, current, total, message, percent):
        if progress_cb:
            try:
                progress_cb(phase, current, total, message, percent)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    t0 = time.time()
    novel_title = novel_meta.get("title") or novel_meta.get("name") or "未命名小说"
    chapter_title = (chapter.get("title") or f"第{chapter.get('index')}章").strip()
    seg = (novel_text or "")[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
    if not seg.strip():
        raise LLMError(f"章节正文为空：{chapter_title}")
    if len(seg) < CHAPTER_MIN_CHARS:
        logger.warning(f"章节偏短（{len(seg)} 字）：{chapter_title}")

    all_chunks, sampled, sampled_idx = build_chapter_chunks(seg, chunk_chars, max_subchunks)
    if not sampled:
        raise LLMError(f"章节无法分块：{chapter_title}")
    advice = chapter_advice(len(seg), len(all_chunks))

    warnings = []
    trunc_events = []          # 截断自动提额 / 二次二分事件（写入 metadata，便于排查）
    if advice["too_short"]:
        warnings.append(advice["message"])
    # 全量覆盖：镜头数随内容体量自动扩展（约每 CHARS_PER_SHOT 字 1 镜），不再为凑固定时长砍内容
    est_shots_total = sum(estimate_shots_for_chars(c.get("char_count") or 0) for c in all_chunks)

    # ---- 单集镜头数：**不再设上限**（2026-10-09 用户口径）----
    # 生产已从「整集一次连续渲染」改为「**按场次生产**」（每场单独提交、最后拼接成片），
    # 原先的 78 镜资源红线（整集长跑把 ComfyUI 系统资源耗尽、跑到约 78 段必崩）不再适用于单集口径，
    # 因此这里不再收紧每块镜头额度、也不再对最终 shots 做截断：
    # 镜头数完全由内容体量决定（约每 CHARS_PER_SHOT 字 1 镜），原文与画面细节都不丢。
    per_chunk_cap = 0                      # 0 = 不限制（保留变量以兼容下游签名）

    total_steps = len(sampled) + 2 + len(sampled)
    report("prepare", 0, total_steps,
           f"第{episode_no}集《{chapter_title}》：{len(seg)} 字，二次分块 {len(all_chunks)} 块，"
           f"全量送模型 {len(sampled)} 块（不抽样），预计 {est_shots_total} 镜", 3)

    # ① 逐块提炼（按 chunk.index 建立映射，避免与 sampled 错位）
    outlines, outline_by_chunk = [], {}
    for i, chunk in enumerate(sampled):
        report("outline", i + 1, total_steps,
               f"第{episode_no}集 提炼子块 {chunk['index']}/{chunk['total']}…",
               int(5 + (i + 1) / total_steps * 55))
        try:
            ol = extract_chunk_outline(client, chunk, novel_title, events=trunc_events,
                                       cache_dir=cache_dir)
        except LLMTruncatedError as e:
            warnings.append(f"第 {chunk['index']} 子块提炼被截断失败（已自动提额并尝试二次切分）：{e}")
            logger.warning(f"第 {chunk['index']} 子块提炼被截断失败：{e}")
            continue
        except LLMError as e:
            warnings.append(f"第 {chunk['index']} 子块提炼失败：{e}")
            logger.warning(f"第 {chunk['index']} 子块提炼失败：{e}")
            continue
        outlines.append(ol)
        outline_by_chunk[chunk["index"]] = ol
    if not outlines:
        raise LLMError("本章所有子块提炼均失败（每块均已自动提高 max_tokens，必要时二次切分）："
                       + ("；".join(warnings) or "未知错误"))

    # ② 汇总本集设定
    report("bible", len(sampled) + 1, total_steps, f"第{episode_no}集 汇总人物 / 物品 / 场景设定…", 65)
    bible = _as_dict(build_bible(client, outlines, f"{novel_title}·{chapter_title}", style, 1,
                                 target_shots, events=trunc_events,
                                 continuity_ctx=continuity_ctx, cache_dir=cache_dir))

    characters = _norm_list(bible.get("characters"), 8,
                            ["name", "age", "gender", "identity", "appearance", "outfit", "personality",
                             "voice_style", "reference_prompt_zh", "reference_prompt_en"])
    items = _norm_list(bible.get("items"), 3,
                       ["name", "category", "appearance", "owner", "owner_photo",
                        "surface_text", "importance",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = normalize_scenes_meta(_norm_list(
        bible.get("scenes"), 8,
        ["name", "location", "appearance", "scene_lighting", "int_ext", "time_of_day",
         # ⭐ 2026-10-07：场景「确切文字」字段（治场景告示/招牌乱码）。
         #    必须与其他字段同批登记 —— _norm_list 只保留白名单字段，漏登记会静默丢弃。
         "surface_text",
         "reference_prompt_zh", "reference_prompt_en"]))
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    # 风格：以调用方传入的 style 为准（模型的返回值可能是自我发挥，用户意图优先）
    eff_style = style_kit.normalize_style(style) or style_kit.normalize_style(bible.get("style"))
    # 确定性补写：模型不得自写风格词（写了会重复），统一在这里收尾，中英双语都补
    style_filled = style_kit.apply_asset_style_all(characters, eff_style) \
        + style_kit.apply_asset_style_all(items, eff_style) \
        + style_kit.apply_asset_style_all(scenes, eff_style)
    if eff_style and style_filled:
        logger.info("第%s集：已为 %d 条资产参考提示词补写风格「%s」", episode_no, style_filled, eff_style)
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": eff_style[:60],
             "characters": characters, "items": items, "scenes": scenes,
             "production_notes": bible.get("production_notes") or {}}

    # ③ 逐块写分镜（每块镜头数按该块原文体量自动扩展，不再按 target_shots 摊薄砍内容）
    all_shots = []
    for i, chunk in enumerate(sampled):
        chunk_chars_i = chunk.get("char_count") or len(chunk.get("text") or "")
        per_chunk = estimate_shots_for_chars(chunk_chars_i)
        if per_chunk_cap:
            # 显式额度生效：本块额度不得超预算（原文不丢，只是每镜承载更多）。
            # ⚠️ 2026-10-09 起主流程不传额度（单集镜数不设上限），per_chunk_cap 恒为 0。
            per_chunk = max(1, min(int(per_chunk), int(per_chunk_cap)))
        report("shots", len(sampled) + 2 + i, total_steps,
               f"第{episode_no}集 编写分镜（子块 {chunk['index']}/{chunk['total']}，"
               f"{chunk_chars_i} 字 → {per_chunk} 镜）…",
               int(70 + (i + 1) / total_steps * 25))
        ol = outline_by_chunk.get(chunk["index"])
        if not ol:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块缺少提炼结果，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）")
            logger.warning(f"第 {chunk['index']} 子块缺少提炼结果，已兜底 {len(fb)} 镜")
            continue
        # P1-2 接缝重叠（借 ViMax overlap 思路、按本架构落地）：i>0 时把「上一子块结尾」
        # 注入本块，让开头承接上段。上一子块 = sampled[i-1]，其 outline 按 index 取；
        # i=0 → 上子块 None → prev_tail="" → prompt 不出现该段（零变化）。
        _prev_chunk = sampled[i - 1] if i > 0 else None
        _prev_outline = outline_by_chunk.get(_prev_chunk["index"]) if _prev_chunk else None
        _prev_tail = _prev_tail_block(_prev_chunk, _prev_outline)
        try:
            all_shots.extend(build_shots_for_chunk(client, bible, ol, chunk, per_chunk,
                                                   events=trunc_events,
                                                   continuity_ctx=continuity_ctx,
                                                   cache_dir=cache_dir,
                                                   shots_hard_cap=per_chunk_cap,
                                                   prev_tail=_prev_tail))
        except LLMTruncatedError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜被截断失败（已自动提额并尝试二次切分），"
                            f"已按原文兜底生成 {len(fb)} 镜（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜被截断失败，已兜底 {len(fb)} 镜：{e}")
        except LLMGatewayUnavailable as e:
            fb = _gateway_down_fallback(e, chunk, per_chunk, bible, all_shots)
            all_shots.extend(fb)
            warnings.append(_gateway_down_warning(chunk, len(fb)))
            logger.error(f"第 {chunk['index']} 子块因网关不可用降级兜底 {len(fb)} 镜：{e}")
        except LLMError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜失败，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜失败，已兜底 {len(fb)} 镜：{e}")

    shots = _norm_shots(all_shots, bible, 1)

    # ---- 单集镜头数：不再截断（2026-10-09 用户口径）----
    # 曾经这里会把超过 MAX_SHOTS_PER_EPISODE(78) 的镜头**硬截断**（丢尾部情节）。
    # 现按场次生产，单集镜数不再设上限，故整段截断逻辑已删除：
    # 模型超产多少就保留多少，尾部情节永不被吞（镜数只作粒度诊断，见下方诊断段）。

    # ---- 分镜粒度诊断（2026-10-06 用户口径：每集 20~30 镜、每镜 5~6 秒）----
    # **只诊断、不截断**（截断会丢情节，见上方注释）。镜数明显超出粒度目标 = 「又切细了」，
    # 它的可见症状就是分镜九宫格（单镜 9 关键帧·时间推进）里大量重复格：
    # 镜头越短，9 帧里能塞进的互异画面越少。这里响亮记一笔，便于在日志里第一时间
    # 看出「参数被 env 改回旧值」或「模型又超产」，而不是等用户看图才发现。
    # ⭐ 2026-10-10：用户已取消镜数上下限（「每集下限和上限都不限制」），
    #    故不再以 SHOT_GRANULARITY_MAX_SHOTS 判「切太细」——那会与新口径冲突、
    #    并在合法的长集上刷无意义告警。仅在**异常多**（>120 镜，已远超任何合理单集）
    #    时提示一次，用于发现「参数被 env 改回旧值」这类真异常。
    _gran_limit = 120
    if len(shots) > _gran_limit:
        _avg_sec = sum(float(s.get("duration") or 0) for s in shots) / max(1, len(shots))
        logger.warning(
            "第%s集：本集 %d 镜 / 平均 %.2f 秒 —— 镜数异常多（>%d），已远超任何合理单集。"
            "镜数本身不再设限（用户口径：上下限都不限），此提示仅用于发现"
            "「CHARS_PER_SHOT / REF_INSERT_RATIO / SPLIT_ACTION_BEATS / SHOT_DURATION_MIN "
            "被 env 改回旧值」这类真异常。",
            episode_no, len(shots), _avg_sec, _gran_limit)

    for sh in shots:
        sh["episode"] = int(episode_no)
    if not shots:
        raise LLMError("模型未返回有效分镜（每块均已自动提高 max_tokens，必要时二次切分）："
                       + ("；".join(warnings) or "未知错误"))

    # 外形一致性收敛（2026-09-24）：资产**出图依据**是 reference_prompt_zh，而分镜
    # 提示词 / 质检口径取 appearance —— 两者漂移会让「文字说黑寸头、参考图却是黑发高冠」
    # 两条互斥指令同时生效（分镜是 cfg=1.0 的参考图编辑型 → 参考图压过文字），模型每次
    # 随机倒向一边、质检必然抓到另一边 → 无限重试、整集跑不过。落盘前按 appearance 收敛。
    asset_prompt_kit.reconcile_script({"characters": characters, "shots": shots})

    project_name = episode_project_name(novel_meta.get("name") or novel_title, episode_no)
    # 场次表（2026-10-02）：落盘前重新编号并收表 —— 覆盖率补生成/一致性修复新增的
    # 镜头也要落在正确的【第N场】里，不能出现没场次号的孤儿镜头。
    assign_scene_numbers(shots)
    scene_flow = collect_scene_flow(shots)
    script = {
        "title": bible["title"],
        "episode_no": int(episode_no),
        "episode_title": chapter_title,
        "theme": bible["theme"],
        "style": bible["style"],
        "characters": characters,
        "items": items,
        "scenes": scenes,
        # 场次表：按出场顺序的【第N场】内外景·地点·时间，供前端场记板与成片字幕定位
        "scene_flow": scene_flow,
        "shots": shots,
        "production_notes": {
            "total_shots": len(shots),
            "scene_count": len(scene_flow),
            # C⑥：style_guide 提升为项目级唯一配置（continuity 提供时优先，不再每集各写一套）
            "style_guide": str(_ctx_block(continuity_ctx, "style_guide_text")
                               or (bible.get("production_notes") or {}).get("style_guide") or "")[:300],
            "style_guide_source": "project" if _ctx_block(continuity_ctx, "style_guide_text") else "episode",
            "bible_locked": bool(continuity_ctx),
            "continuity_version": str((continuity_ctx or {}).get("version") or "") or None,
        },
        "metadata": {
            "source": "novel_chapter_to_script",
            "source_novel": {
                "novel_id": novel_meta.get("novel_id"),
                "name": novel_meta.get("name"),
                "title": novel_meta.get("title"),
                "char_count": novel_meta.get("char_count"),
                "chapter_count": novel_meta.get("chapter_count"),
            },
            "episode_no": int(episode_no),
            "episode_title": chapter_title,
            "chapter_index": chapter.get("index"),
            "chapter_title": chapter_title,
            "chapter_char_count": len(seg),
            "chunks_total": len(all_chunks),
            "chunks_used": len(sampled),
            # 全量覆盖：sampled_chunks 语义 = 全部子块下标（保留旧字段名兼容前端）
            "sampled_chunks": sampled_idx,
            "coverage_mode": "full",
            "chars_per_shot": CHARS_PER_SHOT,
            "estimated_shots": est_shots_total,
            "shots_per_chunk": [estimate_shots_for_chars(c.get("char_count") or 0) for c in all_chunks],
            "subchunk_chars": chunk_chars,
            "style": style,
            "target_shots": target_shots,
            "project_name": project_name,
            "model": client.model,
            "base_url": client.base_url,
            "elapsed_sec": round(time.time() - t0, 1),
            "warnings": warnings,
            "truncation_events": trunc_events,
            "truncation_retries": len([e for e in trunc_events if e.get("attempt")]),
            "auto_sub_splits": len([e for e in trunc_events if e.get("event") == "sub_split"]),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    # ⑤ AI 转剧本阶段自动判定「本集镜头数 / 本集时长（秒）」并写入 Schema（下游链路直接引用）
    apply_episode_schema(script)
    report("done", total_steps, total_steps,
           f"第{episode_no}集完成：{len(characters)} 角色 / {len(items)} 物品 / "
           f"{len(scenes)} 场景 / {script['shot_count']} 镜头 / 本集约 {script['episode_duration_sec']}s", 100)
    return script


def episode_script_path(script_dir: str, novel_name: str, episode_no, project_key: str = None) -> str:
    """按集组织落盘路径：output/scripts/<项目键>/第N集.json

    project_key 由项目注册表（project_store）统一分配，保证「一部小说一个独立目录」；
    未提供时退回按小说名安全化，兼容旧行为。
    """
    folder = project_key or safe_project_name(novel_name)
    return os.path.abspath(os.path.join(
        script_dir, folder, f"第{int(episode_no)}集.json"))


def save_episode_script(script: dict, script_dir: str, novel_name: str, episode_no,
                        project_key: str = None) -> str:
    """按集落盘（覆盖同名集号），返回绝对路径"""
    path = episode_script_path(script_dir, novel_name, episode_no, project_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = script.setdefault("metadata", {})
    meta["script_path"] = path
    meta["project_name"] = meta.get("project_name") or episode_project_name(novel_name, episode_no)
    meta["project_key"] = project_key or safe_project_name(novel_name)
    meta["episode_no"] = int(episode_no)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    logger.info(f"第{episode_no}集剧本已落盘：{path}")
    return path


def list_episodes(script_dir: str, novel_name: str, project_key: str = None) -> list:
    """列出某小说已生成的剧集（按集号排序）"""
    folder = os.path.join(script_dir, project_key or safe_project_name(novel_name))
    if not os.path.isdir(folder):
        return []
    out = []
    for fn in os.listdir(folder):
        m = re.match(r"^第(\d+)集\.json$", fn)
        if not m:
            continue
        path = os.path.abspath(os.path.join(folder, fn))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"剧集读取失败 {path}：{e}")
            continue
        meta = data.get("metadata") or {}
        shots = data.get("shots") or []
        stats = meta.get("episode_stats") or build_episode_stats(shots)
        cov = meta.get("coverage") or {}
        out.append({
            "episode_no": int(m.group(1)),
            "file": fn,
            "path": path,
            "title": data.get("title"),
            "episode_title": data.get("episode_title") or meta.get("chapter_title"),
            "chapter_index": meta.get("chapter_index"),
            "chapter_char_count": meta.get("chapter_char_count"),
            "characters": len(data.get("characters") or []),
            "items": len(data.get("items") or []),
            "scenes": len(data.get("scenes") or []),
            "shots": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
            "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
            "episode_duration_sec": float(data.get("episode_duration_sec") or stats.get("duration_sec") or 0),
            "duration_per_shot_sec": float(data.get("duration_per_shot_sec")
                                           or stats.get("duration_per_shot_sec") or 0),
            "episode_stats": stats,
            "prompt_ready": sum(1 for s in shots if isinstance(s, dict) and s.get("prompt_h3")),
            "project_name": meta.get("project_name") or episode_project_name(novel_name, m.group(1)),
            "generated_at": meta.get("generated_at"),
            "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            "warnings": meta.get("warnings") or [],
            # ④⑤ 原文覆盖率（落盘可查；未跑过新流程的旧剧集为 None）
            "coverage_percent": cov.get("coverage_percent"),
            "coverage_plot_percent": cov.get("plot_coverage_percent", cov.get("coverage_percent")),
            "coverage_detail_percent": cov.get("detail_coverage_percent", cov.get("char_coverage_percent")),
            "coverage_detail_passed": cov.get("detail_passed"),
            "coverage_zero_omission": cov.get("zero_omission"),
            "coverage_passed": cov.get("passed"),
            "coverage_threshold_percent": cov.get("threshold_percent"),
            "coverage_missing_count": cov.get("missing_count"),
            "coverage_supplement_shots": cov.get("supplement_shots"),
            "coverage_supplement_rounds": cov.get("supplement_rounds"),
            "coverage_report_path": meta.get("coverage_report_path"),
            "coverage_verified": bool(cov.get("checked_at")),
        })
    out.sort(key=lambda r: r["episode_no"])
    return out


def load_episode_script(script_dir: str, novel_name: str, episode_no, project_key: str = None) -> dict:
    """读取已落盘的单集剧本（供前端切集预览 / 载入后续步骤）"""
    path = episode_script_path(script_dir, novel_name, episode_no, project_key)
    if not os.path.isfile(path):
        raise EpisodeNotFoundError(f"第{int(episode_no)}集尚未生成：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_project_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())
    return name[:40] or "novel_project"


# 整本转剧本路径已随「一章一集」拍板移除（2026-10-05）；集级入口见 convert_chapter_with_continuity
