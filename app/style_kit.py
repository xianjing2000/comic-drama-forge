"""项目风格统一落地工具（风格注入链路的唯一入口）

背景（2026-09-18 修复）
----------------------
用户与 AI 总控敲定的风格串（如「国漫风格/偏写实/竖屏9:16」）此前只在剧本 bible 的提示词里
"提了一句"，随后在所有真正的生成环节全部丢失：

1. ``novel_to_script._norm_shots`` 组装的镜头行里**没有 style 字段** → 24/24 镜 ``shot["style"]``
   为 None → 视频提示词 ``shot.get("style", "3D动漫渲染")`` 永远回落到硬编码默认值；
2. ``app._generate_asset_task`` 调 ``generate_character_base/item_base/scene_base`` 时**只传
   reference_prompt_zh**，风格既没进 bible 的参考图提示词，也没在生成前追加；
3. ``comfyui_client`` 分镜提示词把「国漫3D渲染风格，竖屏 9:16 构图」**写死**在要求段里；
4. 工作流尺寸节点（``EmptySD3LatentImage``）与 H3 模板的 ``ResolutionSelector`` 都是
   模板自带的 16:9，**没有任何代码按用户设定覆写** → 成片恒为 960×544 横屏。

结论：风格设定对**所有**资产类型都是失效的（不只物品），只是物品最容易一眼看出。

本模块提供唯一入口，避免又一次"每个调用点各写一遍、又漏一处"：

- :func:`normalize_style`        —— 归一化风格串（分隔符统一为「，」、去重保序）
- :func:`style_suffix`           —— 生成可直接拼进图像提示词的风格后缀
- :func:`with_style`             —— 幂等地把风格后缀拼到任意提示词末尾
- :func:`aspect_ratio`           —— 从风格串解析画幅（"竖屏9:16" → (9, 16)）
- :func:`aspect_size`            —— 画幅 → 工作流用宽高（按 multiple 对齐，复刻 ComfyUI 公式）
- :func:`apply_latent_size`      —— 覆写 API prompt 中所有尺寸节点的宽高
- :func:`apply_resolution_widgets` —— 覆写 UI 工作流里分辨率节点的 widget 值
"""

from __future__ import annotations

import math
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 风格串归一化
# --------------------------------------------------------------------------- #

#: 用户 / 模型可能使用的各种分隔符
_SEP_RE = re.compile(r"[，,、/／|｜·・；;\s]+")

#: 视为「无风格」的占位串
_EMPTY_TOKENS = {"", "none", "null", "无", "默认", "未设置", "无风格", "custom", "自定义"}


def normalize_style(style) -> str:
    """归一化风格串：保留原词序与用词，仅统一分隔符并去重。

    ``"国漫风格/偏写实/竖屏9:16"`` → ``"国漫风格，偏写实，竖屏9:16"``
    """
    raw = str(style or "").strip()
    if not raw or raw.lower() in _EMPTY_TOKENS:
        return ""
    seen, parts = set(), []
    for p in _SEP_RE.split(raw):
        p = p.strip()
        if not p or p.lower() in _EMPTY_TOKENS or p in seen:
            continue
        seen.add(p)
        parts.append(p)
    return "，".join(parts)


def style_tokens(style) -> List[str]:
    """拆出风格词列表（供画幅解析、关键词匹配使用）"""
    norm = normalize_style(style)
    return [p for p in norm.split("，") if p] if norm else []


# --------------------------------------------------------------------------- #
# 画幅解析
# --------------------------------------------------------------------------- #

_RATIO_RE = re.compile(r"(?P<a>\d{1,2})\s*[:：]\s*(?P<b>\d{1,2})")

#: 中文/英文关键词 → 画幅（关键词比裸数字更明确地表达用户意图，优先命中）
#: ⚠️ 2026-09-23 起**显式比例（"a:b"）优先于关键词**（见 aspect_ratio）：
#: 新建项目新增 2:3/3:2/3:4/4:3/21:9 等比例后，「横屏 21:9」这类串若关键词
#: 优先会被压成 (16,9)，只有显式数字优先才能表达 UltraWide。
_KEYWORD_RATIO: Sequence[Tuple[str, Tuple[int, int]]] = (
    ("竖屏", (9, 16)),
    ("竖向", (9, 16)),
    ("竖版", (9, 16)),
    ("竖幅", (9, 16)),
    ("手机屏", (9, 16)),
    ("抖音", (9, 16)),
    ("portrait", (9, 16)),
    ("vertical", (9, 16)),
    ("横屏", (16, 9)),
    ("横向", (16, 9)),
    ("横版", (16, 9)),
    ("横幅", (16, 9)),
    ("宽屏", (16, 9)),
    ("landscape", (16, 9)),
    ("方形", (1, 1)),
    ("square", (1, 1)),
    ("超宽", (21, 9)),
    ("宽银幕", (21, 9)),
    ("ultrawide", (21, 9)),
)

#: 默认画幅：**2026-09-28 用户要求默认改横屏 16:9**（对标参考作品即 16:9），
#: 由原竖屏 9:16（漫剧短视频主形态，抖音/视频号）翻转而来。
#: ⚠️ 显式比例「a:b」与「竖屏/横屏」等关键词仍**优先于**本默认值，
#:    :func:`aspect_ratio` 的解析语义未变 —— 本值只在风格串未含任何画幅信息时兜底。
#: G19 修复（历史）：风格串未含画幅关键词时，各生成入口以本值为底，
#: 不再静默回落工作流模板里写死的 16:9 尺寸。
DEFAULT_RATIO: Tuple[int, int] = (16, 9)

#: 视频画幅的**像素预算**兜底（MP）。⚠️ 这不是审美参数，是显存预算。
#: 默认 **0.5**：16:9 → 960×544，9:16 → 544×960。
#: H3 Director 模板自带的 output 配置就是 ``megapixels=0.4``（864×480）。
#: 2026-09-27 实测（RTX 5060 Laptop 8GB）：544×960 + 2 段 146 帧的 Director
#: 任务在「模型初始化」阶段**可复现崩溃**（fresh 进程也一样）：
#:   Fatal Python error: Aborted / Windows fatal exception: code 0x80000003
#: 栈恒落在 SolAttn 的 ``_morton_h3.py:135``（该行是 ``finally:`` 里的
#: ``model._sol_morton_state = None`` —— 制造 abort 的是 comfy_aimdo 的分配
#: 失败，这一帧只是最后碰到的 Python 语句，属噪音）。
#: 8GB 卡已知**可跑通**的档位是 **0.4**（864×480，与 H3 模板自带 output 同档）；
#: 需要时覆盖：``MJSCXT_VIDEO_MEGAPIXELS=0.4``（只在视频链路消费，见
#: :func:`video_megapixels`；分镜 / 资产链路的 :func:`resolve` 行为不变）。
#: ⚠️ 2026-09-28 默认画幅由 9:16 翻转为 16:9 后，**竖屏尺寸**仅当项目/风格串
#: 显式声明 9:16 时才会出现（本注释里 544×960 / 864×480 的尺寸即属此列）。
VIDEO_MEGAPIXELS_DEFAULT: float = 0.5

#: 分镜图的画幅像素预算（MP）。
#: 2026-09-28 画质提升：0.5 → 0.8；**2026-10-02 用户要求：0.8 → 1.5**。
#: 依据：分镜图分辨率是**全链路画质天花板** —— 分镜模板 `分镜生成_Qwen21.json`
#: 没有尺寸节点，输出画幅完全由 _unify_ref_canvas 把参考图 cover 到这个尺寸决定。
#: 当年压到 0.5MP 是为规避 H3 竖屏崩溃，而该崩溃真因是 SolAttn morton 组合（已解决）
#: → 预算可以恢复。env MJSCXT_STORYBOARD_MEGAPIXELS 可覆盖。
#: ⚠️ 1.5MP + 16:9 → 1632×928；1:1 → 1248×1248。8GB 卡若 OOM 可回落此常量
#:    或临时 env 覆盖（无需改代码）。
STORYBOARD_MEGAPIXELS_DEFAULT: float = 1.5

#: 资产参考图（角色三视图 / 物品 / 场景原画）的画幅像素预算（MP）。
#: **2026-10-02 新增**：此前资产链路走 `aspect_size(ratio)` 的**函数默认值 0.5**，
#: 于是「角色设定图」这种**决定全链路角色一致性**的关键参考图只有 0.5MP
#: （1:1 → 736×736）—— 面部/配饰细节不足，被分镜参考图放大后就是系统性脸漂。
#: 与分镜同档取 1.5：资产图是**静态单图**（无时序、无 latent 拼接），显存压力
#: 远小于视频链路，提档收益/风险比最高。
#: env MJSCXT_ASSET_MEGAPIXELS 可覆盖。
ASSET_MEGAPIXELS_DEFAULT: float = 1.5


def storyboard_megapixels() -> float:
    """分镜链路画幅像素预算（MP）：env ``MJSCXT_STORYBOARD_MEGAPIXELS`` 优先。

    非法 / 越界一律回落 :data:`STORYBOARD_MEGAPIXELS_DEFAULT`（不抛异常：
    这是显存调优参数，读错不该让整集分镜生成挂掉）。
    """
    raw = str(os.environ.get("MJSCXT_STORYBOARD_MEGAPIXELS") or "").strip()
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return STORYBOARD_MEGAPIXELS_DEFAULT
    return val if 0.05 <= val <= 4.0 else STORYBOARD_MEGAPIXELS_DEFAULT


def video_megapixels() -> float:
    """视频链路的画幅像素预算（MP）：env ``MJSCXT_VIDEO_MEGAPIXELS`` 优先。

    越界 / 非法值一律回落 :data:`VIDEO_MEGAPIXELS_DEFAULT`（不抛异常：
    这是显存调优参数，读错不该让整集生产挂掉）。
    """
    raw = str(os.environ.get("MJSCXT_VIDEO_MEGAPIXELS") or "").strip()
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return VIDEO_MEGAPIXELS_DEFAULT
    return val if 0.05 <= val <= 4.0 else VIDEO_MEGAPIXELS_DEFAULT


def asset_megapixels() -> float:
    """资产参考图画幅像素预算（MP）：env ``MJSCXT_ASSET_MEGAPIXELS`` 优先。

    越界 / 非法值一律回落 :data:`ASSET_MEGAPIXELS_DEFAULT`（不抛异常：
    这是显存调优参数，读错不该让整批资产挂掉）。
    """
    raw = str(os.environ.get("MJSCXT_ASSET_MEGAPIXELS") or "").strip()
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return ASSET_MEGAPIXELS_DEFAULT
    return val if 0.05 <= val <= 4.0 else ASSET_MEGAPIXELS_DEFAULT

# --------------------------------------------------------------------------- #
# 资产内置画幅（2026-09-22 需求：参考资产图的画幅**写死**，不跟随视频比例）
#   角色参考图(三视图设定图) 1:1   ← 见下方 2026-09-23 修正说明
#   角色多视图(三视图)        1:1
#   道具 / 物品资产           1:1
#   场景原画（背景）          16:9
# 只有「视频」与「分镜」的比例由用户输入控制（跟随 style 串解析出的 aspect_ratio），
# 这里的参考资产图画幅与成片画幅解耦——即使用户拍 9:16 成片，角色参考图仍是 1:1。
#
# ⚠️ 2026-09-23 修正（角色 3:4 → 1:1，勿改回）：
#   角色基础图的内容**不是单人立绘，而是「正面/左侧面/背面」三张全身视图横排的三视图
#   设定图**（见 comfyui_client._ensure_fullbody_prompt，该版式约束自 2026-09-19 起强制）。
#   2026-09-22 把角色基础图写死成 3:4 竖幅时，是按「单人立绘」定的比例，与三视图横排版式
#   互相冲突 —— 三个人被硬塞进竖构图。
#   实测（prompt=蛊真人精校版/黄钟族长，QwenImage2.1，0.5MP，种子 11/22/33）：
#     3:4  640×832 → 列段检测只有 1~2 段（三人横向粘连），人物外接框左边界 x=0、右边界
#                     x=639，**左右留白 0~2px**，三人挤满画面
#     1:1  736×736 → 列段 3（三人清晰分离），左右留白 3~14px，三人等高（体量比 1.005~1.012）、
#                     脚底共线（≤1.2% 画高）、头顶齐平（0.1%）、间距均匀（≤4%），
#                     每人像素高 645~669px（各候选画幅中最高）
#     4:3  832×640 → 留白更足（25~32px）但每人像素高降到 584~594px
#     16:9 960×544 → 留白最多（52~59px）但每人只有 524px 高，且脚底余量仅 9px（切脚风险）
#   结论：1:1 同时取得「三人分离」与「每人可用像素最高」，故角色参考图取方形。
#   注：多视角阶段共用 分镜生成_Qwen21.json，该模板无尺寸节点 → 多视角图**继承参考图**，
#       所以基础图取方形后，4 张视角图也自动是方形，两者天然一致。
# --------------------------------------------------------------------------- #
ASSET_BASE_RATIO: Dict[str, Tuple[int, int]] = {
    "character": (1, 1),   # 角色三视图设定图（三人横排，必须方形，勿改 3:4）
    "item": (1, 1),        # 道具 / 物品资产
    "scene": (16, 9),      # 场景原画（背景）
}
#: 角色多视图（三视图：正面/侧面/背面横排）统一用方形，避免横排被裁切
ASSET_MULTIVIEW_RATIO: Tuple[int, int] = (1, 1)


def asset_aspect_ratio(asset_type: str, is_multiview: bool = False
                       ) -> Optional[Tuple[int, int]]:
    """资产内置画幅：按类型写死，不跟随视频比例。

    ``is_multiview=True`` 时返回多视图（三视图）画幅（恒 1:1）；
    否则按 ``asset_type``（character/item/scene）取内置基础图画幅，未知类型返回 None。
    调用方应再经 :func:`aspect_size` 转成宽高元组喂给 ComfyUI 尺寸节点。
    """
    if is_multiview:
        return ASSET_MULTIVIEW_RATIO
    return ASSET_BASE_RATIO.get((asset_type or "").strip().lower())


def aspect_ratio(style) -> Optional[Tuple[int, int]]:
    """从风格串解析目标画幅，解析不到返回 None。

    **显式比例（"a:b"）优先于关键词**（2026-09-23 语义修正）：「a:b」是精确表达，
    「竖屏/横屏」是泛化表达；新建项目支持 21:9 等比例后，「横屏 21:9」若关键词
    优先会被压成 (16,9)。仅当串中无显式比例时才回落关键词（"竖屏"单独出现仍生效）。
    """
    toks = style_tokens(style)
    if not toks:
        return None
    joined = "，".join(toks).lower()
    m = _RATIO_RE.search(joined)
    if m:
        a, b = int(m.group("a")), int(m.group("b"))
        if 0 < a <= 32 and 0 < b <= 32:
            return (a, b)
    for kw, ratio in _KEYWORD_RATIO:
        if kw in joined:
            return ratio
    return None


def aspect_size(ratio: Optional[Tuple[int, int]], megapixels: float = ASSET_MEGAPIXELS_DEFAULT,
                multiple: int = 32) -> Optional[Tuple[int, int]]:
    """画幅 → 宽高像素（复刻 ComfyUI ``nodes_resolution`` 的算法）

    ``(9, 16)`` + 0.5MP + 32 → ``(544, 960)``；``(16, 9)`` → ``(960, 544)``

    ⚠️ ``megapixels`` 默认值 = :data:`ASSET_MEGAPIXELS_DEFAULT`（**2026-10-02 起 1.5**）。
       历史为字面量 ``0.5``；改用常量是为了让「资产/兜底档位」只有**一个权威来源**
       —— 此前资产链路（`app.py` 的资产生成）与 `aspect_label` 都不传此参，
       于是静默吃 0.5，改档位时极易漏改（这正是「资产图只有 0.5MP」的成因）。
       需要旧行为的调用方请显式传值（视频链路传 :func:`video_megapixels`）。
    """
    if not ratio:
        return None
    a, b = int(ratio[0]), int(ratio[1])
    if a <= 0 or b <= 0 or multiple <= 0:
        return None
    scale = math.sqrt(float(megapixels) * 1024 * 1024 / (a * b))
    width = round_to_multiple(a * scale, multiple)
    height = round_to_multiple(b * scale, multiple)
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def round_to_multiple(val, multiple: int = 32) -> int:
    """把任意数值严格对齐到 ``multiple`` 步长（潜空间分辨率默认 32）。

    这是项目里**唯一**的「分辨率取整」出口：凡是需要把动态画幅落到潜空间
    可接受像素上的地方（:func:`aspect_size`、参考图缩放、ResolutionSelector
    反解 megapixels 等）都必须走这里，避免各处各写一遍 ``round(x/m)*m``
    导致「有的对齐到 8、有的对齐到 32」的漂移。

    - ``val`` 为负或 None 时返回 0（调用方自行判空）。
    - ``multiple`` 非法（None / ≤0 / 非整数）时回落到 1（即原值取整），
      绝不抛异常阻断生成。
    """
    try:
        m = int(multiple)
        if m <= 0:
            m = 1
    except (TypeError, ValueError):
        m = 1
    if val is None:
        return 0
    try:
        return int(round(int(val) / m) * m)
    except (TypeError, ValueError):
        try:
            return int(round(float(val) / m) * m)
        except (TypeError, ValueError):
            return 0


def aspect_label(style, size: Optional[Tuple[int, int]] = None) -> str:
    """把画幅描述成人话，用于日志 / 界面回显。

    ``size``：**调用方已知的实际像素，务必传** —— 不传就按 :func:`aspect_size` 的
    默认 MP 重算，会与真实渲染尺寸不一致。2026-10-08 实录：视频按
    ``video_megapixels()=0.5`` 实际渲 960×544，日志却显示 1664×928（默认 1.5MP），
    直接把人误导到「视频分辨率过高、应该降档」的错误结论上。
    """
    r = aspect_ratio(style)
    if not r:
        return ""
    size = tuple(size) if size else aspect_size(r)
    orient = "竖屏" if r[0] < r[1] else ("横屏" if r[0] > r[1] else "方形")
    return f"{orient} {r[0]}:{r[1]}" + (f"（{size[0]}×{size[1]}）" if size else "")


# --------------------------------------------------------------------------- #
# 提示词风格注入
# --------------------------------------------------------------------------- #

#: 画幅类 token 不进「图像风格后缀」正文（已由尺寸节点表达，重复描述反而污染提示词）
_ASPECT_TOKENS = ("竖屏", "横屏", "竖向", "横向", "竖版", "横版", "竖幅", "横幅", "方形",
                  "超宽", "宽银幕", "9:16", "16:9", "1:1", "2:3", "3:2", "3:4", "4:3", "21:9",
                  "portrait", "landscape", "square", "ultrawide")

#: 风格后缀的固定收尾（保画质、压畸形，与既有工作流负向词配合）
_QUALITY_TAIL = "画面精致，光影细腻，构图稳定，无畸形"

#: 「色调 / 影调 / 光照」类 token —— 只作用于**画面氛围**，不进**资产参考图**。
#: 根因（2026-10-07 用户反馈「生成的角色皮肤是蓝色的」）：
#:   剧本 style 字段含「冷峻超现实主义，色调灰蓝压抑，光影破碎，…」，
#:   整串被拼进角色三视图 → 模型把「色调灰蓝压抑」当**全局调色** →
#:   皮肤与服饰固有色一起被染蓝；而 base.png 是身份基准图，会经身份网格
#:   把蓝皮肤传染给所有分镜/关键帧（与分镜事故同一条传播通道）。
#: ⚠️ 只收「明确指向全局调色」的子串。不要把「冷峻 / 压抑 / 配色」收进来：
#:   「冷峻」是「冷峻超现实主义」的子串，收了会把画风一起丢掉。
_TONE_TOKENS = (
    "色调", "影调", "色温", "冷色", "暖色", "冷暖", "灰调",
    "光影", "光照", "打光", "布光", "明暗", "暗部", "高光", "对比度",
)

#: 资产参考图的「颜色保真条款」。
#: ⚠️ 措辞必须避开 comfyui_client 的 CHARACTER_WORDS（人物/角色/人影…），
#:   否则场景链路 sanitize_scene_prompt 会把整句当「人物描述句」丢弃
#:   （该类事故见 comfyui_client.scene_view_prompt_suffix 的注释）。
#: ⚠️ 条款正文**绝不能出现「风格」二字**：style_kit._collapse_style 用
#:   raw.count("风格") >= 2 判定「风格双写」并会把 `风格：…` 子句整段剥掉 ——
#:   实测条款里写「上述风格中的色调…」会让计数变 2，于是每次调用都把风格声明
#:   剥掉再重挂，幂等彻底失效（提示词里出现两份质量尾缀）。
REFERENCE_COLOR_FIDELITY_ZH = (
    "参考图颜色须准确：肤色为自然肤色、服饰与物品呈固有色；"
    "色调与光影描述只作用于画面氛围，不得把肤色与固有色整体染偏。"
)
#: 幂等用的**短固定标记**：场景链路 sanitize_scene_prompt 会按标点重新分句、
#: 把「；」并成「，」并去掉句号 → 用整句比对会失配而重复追加，故用短串判定。
REFERENCE_COLOR_FIDELITY_MARK_ZH = "参考图颜色须准确"
REFERENCE_COLOR_FIDELITY_EN = (
    "colors must stay accurate: natural skin tone and true material colors; "
    "any tone or lighting wording affects atmosphere only and must not tint skin or objects."
)
REFERENCE_COLOR_FIDELITY_MARK_EN = "colors must stay accurate"


def style_suffix(style, *, with_tail: bool = True, with_aspect: bool = True,
                 head: str = "风格") -> str:
    """把风格串变成可拼进图像提示词的后缀。

    画幅类 token 默认不进正文（改由工作流尺寸节点落实），但会以「竖屏 9:16 构图」的
    形式补一句构图声明 —— 因为图像模型对画面比例的语义提示仍敏感。
    """
    toks = style_tokens(style)
    if not toks:
        return ""
    body = [t for t in toks if not any(a in t for a in _ASPECT_TOKENS)]
    bits: List[str] = []
    if body:
        bits.append(f"{head}：{'，'.join(body)}")
    if with_aspect:
        r = aspect_ratio(style)
        if r:
            orient = "竖屏" if r[0] < r[1] else ("横屏" if r[0] > r[1] else "方形")
            bits.append(f"{orient} {r[0]}:{r[1]} 构图")
    if with_tail and bits:
        bits.append(_QUALITY_TAIL)
    return "，".join(bits)


def with_style(prompt: str, style, *, with_tail: bool = True) -> str:
    """幂等地把风格后缀拼到提示词末尾（已带过同一风格则不重复追加）"""
    text = str(prompt or "").strip()
    suffix = style_suffix(style, with_tail=with_tail)
    if not suffix:
        return text
    # 先规整历史脏数据（风格双写），让旧剧本在下次生成时自动自愈
    text = _collapse_style(text, style)
    if suffix in text:
        return text
    # 只判「风格：」标记，避免不同风格串互相判定为已注入
    if "风格：" in text and _style_marker(style) in text:
        return text
    # 模型常见的「漏冒号」写法：写成「…，中国古风玄幻漫剧风格。」而不是「风格：中国古风玄幻漫剧」。
    # 历史缺陷：这里只认带冒号的标记，于是模型自己写的那半句不算数，程序又追加一遍，
    # 实测产出「中国古风玄幻漫剧风格。风格：中国古风玄幻漫剧，画面精致…」风格出现两遍。
    if _style_already_present(text, style):
        return text
    if not text:
        return suffix
    sep = "" if text.endswith(("。", "，", "；", ".", "!", "！", "?", "？")) else "。"
    return f"{text}{sep}{suffix}。"


def _drop_tone_tokens(toks) -> List[str]:
    """剔除「色调/影调/光照」类 token（参考图专用，见 _TONE_TOKENS）"""
    return [t for t in toks if not any(a in t for a in _TONE_TOKENS)]


def reference_style(style) -> str:
    """参考图专用风格串：剥离「色调/影调/光照」token

    参考图（角色三视图 / 物品 / 场景原画）是**身份与固有色的基准**，
    色调/光影类词只该作用于成片氛围，不该写进基准图 —— 否则整图被调色，
    肤色与固有色一起染偏（2026-10-07 蓝皮肤事故）。
    """
    norm = normalize_style(style)
    if not norm:
        return ""
    return "，".join(_drop_tone_tokens(style_tokens(norm)))


def _append_clause(text: str, clause: str, marker: str = "") -> str:
    """拼一句附加条款（幂等：marker 已存在则不重复拼）

    marker 传**短固定串**而非整句：场景链路 sanitize_scene_prompt 会按标点重新分句
    并规范化标点，整句比对会失配 → 条款被重复追加。
    """
    text = str(text or "").strip()
    if not clause:
        return text
    if (marker or clause) in text:
        return text
    if not text:
        return clause
    sep = "" if text.endswith(("。", "，", "；", ".", "!", "！", "?", "？")) else "。"
    return f"{text}{sep}{clause}"


def strip_tone_tokens_text(text: str, style) -> str:
    """删除文本里**已存在**的「色调/影调/光照」token（含相邻分隔符）

    为什么必须有这一步（2026-10-07 实测踩到）：
      资产提示词常常在**剧本阶段**就被 style_kit.apply_asset_style_all 拼上了当时的
      完整风格串（含色调词）。此时 with_style 的幂等判定认为「风格已存在」
      （色调词之外的所有 token 都在文本里）→ **原样返回**，于是只做「注入时过滤」
      根本清不掉上游写进去的色调词：重跑资产依旧是蓝皮肤（实测确认）。
      故这里做一次与注入路径无关的确定性清除。

    ⚠️ 只删当前风格串里属于 _TONE_TOKENS 的 token，不动画风/氛围类 token。
    """
    out = str(text or "")
    if not out:
        return out
    for t in [x for x in style_tokens(style) if any(a in x for a in _TONE_TOKENS)]:
        t = t.strip(" 。，；,;")
        if not t or t not in out:
            continue
        # 连同相邻分隔符一起删，避免留下「，，」或悬空标点
        out = out.replace("，" + t, "").replace("," + t, "")
        out = out.replace(t + "，", "，").replace(t + ",", ",")
        out = out.replace(t, "")
    out = re.sub(r"[，,、]{2,}", "，", out)
    out = re.sub(r"。\s*，", "。", out)
    out = re.sub(r"[，,、]\s*。", "。", out)
    return out.strip()


def with_reference_style(prompt: str, style, *, with_tail: bool = True) -> str:
    """资产参考图（角色三视图 / 物品 / 场景原画）专用风格注入。

    与 with_style 的两点差别：
      1) 用 reference_style 过滤后的风格串 —— 剥离色调/影调/光照 token；
      2) 追加「颜色保真条款」（REFERENCE_COLOR_FIDELITY_ZH），
         保证即使残留风格词也不会把肤色与固有色整体染偏。

    幂等性由 with_style 自身保证（它按传入的风格串重算 marker），
    条款用 _append_clause 去重。成片（分镜/关键帧）链路**不**走本函数，
    风格照旧完整注入。
    """
    # ⓪ 2026-10-09 修复真实缺陷：本函数此前**不是幂等的**。
    #    实测（STYLE='国漫风格'，自由风格文本）：第二次调用会把「风格段」与
    #    「颜色保真条款」**整体对调**（第一次 …风格…条款，第二次 …条款…风格…），
    #    原因是第①步 strip_tone_tokens_text 改动文本后，with_style 认为风格未落地 → 再拼一次。
    #    注意：用**库内已注册**的风格串（如 '国漫3D渲染'）时不会暴露，只有自由风格文本会。
    #    这里做入口短路：风格与条款都已落地 → 原样返回。
    _raw = str(prompt or "").strip()
    if (_raw and REFERENCE_COLOR_FIDELITY_MARK_ZH in _raw
            and _style_already_present(_raw, reference_style(style) or style)):
        return _raw
    # ① 先删掉文本里**已存在**的色调词（上游剧本阶段就写进来的，注入时的过滤清不掉）
    text = strip_tone_tokens_text(_raw, style) if _raw else _raw
    # ② 再按「参考图口径」补写过滤后的风格后缀（幂等）
    ref = reference_style(style)
    merged = with_style(text, ref, with_tail=with_tail) if ref else text
    # ③ 追加颜色保真条款
    return _append_clause(merged, REFERENCE_COLOR_FIDELITY_ZH,
                          REFERENCE_COLOR_FIDELITY_MARK_ZH)


def _style_already_present(text: str, style) -> bool:
    """判断文本里是否已经承载了该风格（多种写法都算数）

    命中任一条即认为风格已落地：
    1. 完整风格串（``_style_marker``）已出现；
    2. 所有长度 ≥3 的风格 token 都各自出现（模型可能换成别的措辞但语义已覆盖）。
    """
    if not text:
        return False
    marker = _style_marker(style)
    if marker and marker in text:
        return True
    toks = [t for t in style_tokens(style)
            if len(t) >= 3 and not any(a in t for a in _ASPECT_TOKENS)]
    return bool(toks) and all(t in text for t in toks)


#: 模型把中文概念硬音译成英文的常见错译（实测「国漫」被写成 xuanxuan）
#: ⚠️ 必须先匹配「带上下文的整块」，再匹配裸词，否则 ``Chinese xuanxuan comic style``
#:    会被替换成 ``Chinese Chinese animated style comic style``（实测踩过）。
_BAD_ROMANIZATION: Sequence[Tuple[str, str]] = (
    (r"Chinese\s+xuan\s*xuan\s+comic\s+style", "Chinese animated style"),
    (r"\bxuan\s*xuan\s+comic\s+style\b", "Chinese animated style"),
    (r"\bxuan\s*xuan\b", "Chinese animated style"),
    (r"\bguo\s*man\b", "Chinese animated style"),
    (r"\bguoman\b", "Chinese animated style"),
    (r"\bdonghua\b", "Chinese animated style"),
    (r"\bmanhua\b", "Chinese comic style"),
    (r"\bxian\s*xia\b", "Chinese high-fantasy"),
    (r"\bwu\s*xia\b", "Chinese martial arts"),
    (r"\bgufeng\b", "ancient Chinese style"),
    (r"\bxiuzhen\b", "Chinese cultivation fantasy"),
)

#: 英文提示词里不该由模型写、也不该重复的质量词（程序统一收尾）
_QUALITY_WORDS_EN = (
    "masterpiece", "best quality", "ultra detailed", "highly detailed",
    "8k", "4k", "high resolution", "sharp focus", "award winning",
)

#: 出现这些词的英文片段一律视为「风格声明」，由程序统一收尾（避免与后缀打架）
_STYLE_HINT_EN = re.compile(
    r"style|comic|anime|cartoon|rendering|rendered|cinematic|realis|painting|"
    r"\bink\b|cel\s*shad",
    re.IGNORECASE,
)


def style_emphasis(style) -> str:
    """生成「强风格指令头」，用于需要强调风格的场景（如尾帧、风格不达标重试）。

    与 :func:`style_suffix` 的区别：后者只是拼在提示词末尾的一句话，容易被模型弱化；
    本函数把风格要求放到提示词**最前**并声明为最高优先级，用于在风格已跑偏时强力纠正。
    """
    norm = normalize_style(style)
    if not norm:
        return ""
    return (f"【风格铁律·最高优先级】本画面必须严格采用「{norm}」的视觉风格、"
            f"画风、渲染方式与配色，任何环节都不得偏离该风格。")


def _style_marker(style) -> str:
    toks = [t for t in style_tokens(style) if not any(a in t for a in _ASPECT_TOKENS)]
    return "，".join(toks)


def negative_for_style(style) -> List[str]:
    """按风格给出需要压制的负向词（避免风格互相打架）"""
    toks = "，".join(style_tokens(style))
    neg: List[str] = []
    if any(k in toks for k in ("国漫", "国风", "水墨", "日式", "赛璐璐", "动漫", "卡通")):
        neg += ["写实照片", "真人摄影", "3D 写实渲染"]
    if "写实" in toks and "偏写实" not in toks:
        neg += ["卡通描边", "平涂色块"]
    return neg


# --------------------------------------------------------------------------- #
# 工作流尺寸覆写
# --------------------------------------------------------------------------- #

#: 直接带 width / height 输入的潜空间尺寸节点（ComfyUI 各版本命名差异）
_LATENT_SIZE_TYPES = (
    "EmptySD3LatentImage",
    "EmptyLatentImage",
    "EmptyLatentImagePresets",
    "SDXL Empty Latent Image",
    "EmptyLatentSDXL",
    "EmptyHunyuanLatentVideo",
)

#: 带 aspect_ratio / megapixels 的分辨率选择器节点
_RESOLUTION_SELECTOR_TYPES = ("ResolutionSelector", "Resolution Selector")

#: 参考图归一化节点（ImageScaleToTotalPixels / 缩放图像（像素））
#: 与 ResolutionSelector 同步改写：保证参考图归一化像素与输出画幅同档。
_REF_CANVAS_SCALE_TYPES = (
    "ImageScaleToTotalPixels",
    "ImageScaleToTotalPixels ",
    "Upscale Image (Pixels)",
    "ImageScaleByMegapixels",
)

#: 画幅 → ResolutionSelector 的下拉文本（不同版本用词不同，这里给出规范值）
_SELECTOR_ASPECT_TEXT = {
    (1, 1): "1:1 (Square)",
    (2, 3): "2:3 (Portrait Photo)",
    (3, 2): "3:2 (Photo)",
    (3, 4): "3:4 (Portrait Standard)",
    (4, 3): "4:3 (Standard)",
    (9, 16): "9:16 (Portrait Widescreen)",
    (16, 9): "16:9 (Widescreen)",
    # ⚠️ "UltraWide" 大小写与 ComfyUI ResolutionSelector 节点真实选项一致
    #（combo widget 按字面量匹配，写错大小写会被拒收 value not in list）。
    (21, 9): "21:9 (UltraWide)",
}


def _selector_aspect_text(width: int, height: int) -> Optional[str]:
    """由目标宽高选出 ResolutionSelector 里**最接近**的标准画幅下拉项。

    与字面量逐字匹配（combo widget 按字面量校验，写错大小写会被拒收）；
    找不到任何候选时返回 ``None``（调用方应保持原值不动）。
    """
    if width <= 0 or height <= 0:
        return None
    best_text: Optional[str] = None
    best_dist = float("inf")
    w, h = float(width), float(height)
    for (a, b), text in _SELECTOR_ASPECT_TEXT.items():
        if a <= 0 or b <= 0:
            continue
        dist = abs(w / h - a / b)
        if dist < best_dist:
            best_dist, best_text = dist, text
    return best_text


def apply_latent_size(api_prompt: dict, size: Optional[Tuple[int, int]]) -> List[str]:
    """把 API prompt 的分辨率落地到画布可见的分辨率节点上，返回被改写的节点描述。

    **画布可见优先（2026-10-07）**：先扫一遍 ``api_prompt`` 里的节点——

    - 若存在 ``class_type`` 在 :data:`_RESOLUTION_SELECTOR_TYPES` 的分辨率选择器节点，
      **不改** ``EmptyLatentImage``，而是改选择器节点自身的 ``aspect_ratio`` /
      ``megapixels`` / ``multiple`` 控件（即「代码跟随画布节点」，而不是强行覆盖
      选择器输出线到 EmptyLatentImage 的字面量）。用户因此在画布上能直接看到参数。
    - 只有当工作流中**完全没有**分辨率选择器节点时，才回落到直接覆写
      ``EmptyLatentImage``（及同型潜空间尺寸节点）的 ``width``/``height`` 整数值。

    命中即算成功；未命中返回空列表，由调用方决定记 warning 还是静默放行。
    """
    if not api_prompt or not size:
        return []
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        return []

    # ---- 1) 先找分辨率选择器节点（画布可见，优先改它）---- #
    selector_ids = [
        node_id for node_id, node in (api_prompt or {}).items()
        if isinstance(node, dict)
        and str(node.get("class_type") or "") in _RESOLUTION_SELECTOR_TYPES
    ]
    # ---- 1b) 参考图归一化节点（与选择器同档，避免 latent 与参考图尺寸不匹配）---- #
    scale_ids = [
        node_id for node_id, node in (api_prompt or {}).items()
        if isinstance(node, dict)
        and str(node.get("class_type") or "") in _REF_CANVAS_SCALE_TYPES
    ]
    if selector_ids or scale_ids:
        text = _selector_aspect_text(width, height)
        # megapixels 反解：让 aspect_size(ratio, megapixels) 尽量复现目标像素预算。
        # ComfyUI 公式 width*height ≈ megapixels * 1024 * 1024（32 取整前后近似）。
        megapixels = round(width * height / (1024 * 1024), 4)
        changed: List[str] = []
        for node_id in selector_ids:
            node = api_prompt[node_id]
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                inputs = {}
                node["inputs"] = inputs
            if text is not None:
                inputs["aspect_ratio"] = text
            inputs["megapixels"] = megapixels
            inputs["multiple"] = 32
            changed.append(f"{node_id}(ResolutionSelector→aspect={text},mp={megapixels},x32)")
        # 参考图归一化节点同步改写：与选择器同档，确保 cover 到相同像素预算 + 32 倍数。
        # 若该节点未声明 megapixels / resolution_steps 输入则静默跳过（不报错）。
        for node_id in scale_ids:
            node = api_prompt[node_id]
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                inputs = {}
                node["inputs"] = inputs
            if "megapixels" in inputs or True:
                inputs["megapixels"] = megapixels
            if "resolution_steps" in inputs or True:
                inputs["resolution_steps"] = 32
            changed.append(f"{node_id}(RefScale→mp={megapixels},x32)")
        # 显式「代码跟随画布」：不动 EmptyLatentImage，它的宽高由选择器输出线决定。
        return changed

    # ---- 2) 无选择器节点时，回落到直接改潜空间尺寸节点的 width/height 字面量 ---- #
    changed = []
    for node_id, node in (api_prompt or {}).items():
        if not isinstance(node, dict):
            continue
        ctype = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if ctype in _LATENT_SIZE_TYPES and "width" in inputs and "height" in inputs:
            inputs["width"] = width
            inputs["height"] = height
            changed.append(f"{node_id}({ctype})")
            continue
        # 兜底：任何同时声明 width/height 的潜空间/尺寸节点
        if "width" in inputs and "height" in inputs and ("Latent" in ctype or "Size" in ctype):
            inputs["width"] = width
            inputs["height"] = height
            changed.append(f"{node_id}({ctype})")
    return changed


def apply_resolution_widgets(nodes: List[dict],
                             ratio: Optional[Tuple[int, int]]) -> List[str]:
    """覆写 UI 工作流里 ResolutionSelector 节点的 widget 值，返回被改写节点描述。

    仅改 ``widgets_values`` 与 ``widgets_values_named``，不动连线结构。
    """
    if not nodes or not ratio:
        return []
    text = _SELECTOR_ASPECT_TEXT.get(tuple(ratio))
    if not text:
        # 非标准画幅（如 5:4）没有对应下拉项，保持原样让调用方走显式宽高覆写路径
        return []
    changed: List[str] = []
    for n in nodes:
        if str(n.get("type") or "") not in _RESOLUTION_SELECTOR_TYPES:
            continue
        named = dict(n.get("widgets_values_named") or {})
        named["aspect_ratio"] = text
        n["widgets_values_named"] = named
        vals = list(n.get("widgets_values") or [])
        if vals:
            vals[0] = text
        else:
            vals = [text]
        n["widgets_values"] = vals
        changed.append(f"{n.get('id')}(ResolutionSelector→{text})")
    return changed


#: 中文风格 token → 英文（供英文提示词复用同一风格）。
#: 支持**子串匹配**：风格串「中国古风玄幻漫剧」不是一个词表键，但含「古风」「玄幻」，
#: 会按最长优先逐项翻译；未覆盖的部分保留中文原词（模型能理解，总比丢风格好）。
#:
#: ⚠️ 子串匹配是双刃剑：键越短越容易「吃掉」不该吃的词。历史缺陷：「国漫」是短键，
#: 会把「美国**漫**画动画插画风格」误译成 Chinese animated style（"漫画" 含 "国漫"?
#: 实为「美漫」的「漫」被邻词影响）。凡新增短键（≤3 字），必须跑 test_style_en_lexicon.py
#: 用全部 61 个风格名回归，确认无残留中文、无误译。
_EN_STYLE_LEXICON = {
    "中国古风": "ancient Chinese style",
    "古风国漫": "ancient Chinese donghua style",
    "国漫": "Chinese animated style",
    "国风": "Chinese traditional style",
    "古风": "ancient Chinese style",
    "玄幻": "high fantasy",
    "武侠": "wuxia martial arts",
    "仙侠": "xianxia cultivation fantasy",
    "修真": "cultivation fantasy",
    "漫剧": "comic drama",
    "悬疑": "suspense",
    "暗黑": "dark tone",
    "黑暗": "dark",
    "奇幻": "fantasy",
    "偏写实": "semi-realistic",
    "写实": "realistic",
    "水墨": "ink-wash painting",
    "日式": "Japanese anime style",
    "赛璐璐": "cel shading",
    "动漫": "anime",
    "卡通": "cartoon",
    "电影级": "cinematic",
    "精致": "refined",
    # ---- 新增：61 风格库覆盖（2026-09-23 对标 pavo 全量） ----
    # 2D
    "现代都市": "modern urban",
    "都市": "urban",
    "韩漫": "Korean webtoon",
    "美漫": "American comic",
    "复古美漫": "retro American comic",
    "漫画动画": "comic animation",
    "二维": "2D",
    "赛博朋克": "cyberpunk",
    "数字插画": "digital illustration",
    "年代": "vintage era",
    "大友克洋": "Katsuhiro Otomo",
    "像素": "pixel art",
    "手冢治虫": "Osamu Tezuka",
    "二次元": "anime",
    "宫崎骏": "Hayao Miyazaki",
    "上美厂": "Shanghai Animation Film Studio",
    "老动画": "classic animation",
    "简笔画": "stick figure",
    "少女漫": "shojo manga",
    "松弛轮廓": "loose contour sketch",
    "轮廓": "contour",
    "手绘": "hand-drawn",
    "新国潮": "neo Chinese chic",
    "神话": "mythology",
    "蜡笔": "crayon",
    "皮影戏": "shadow play",
    # 3D
    "写实CG": "photorealistic CG",
    "CG": "CG",
    "次世代": "next-gen",
    "怪谈": "urban legend",
    "游戏概念艺术": "game concept art",
    "概念艺术": "concept art",
    "游戏渲染": "game rendering",
    "迪士尼": "Disney animation style",
    "3A": "AAA",
    "水果人": "fruit person",
    "粘土": "claymation",
    "定格动画": "stop-motion",
    "黏土": "clay",
    # 真人
    "真人": "live-action",
    "末世": "post-apocalyptic",
    "年代剧": "period drama",
    "古装": "ancient costume",
    "港风": "Hong Kong cinema",
    "韩剧": "Korean drama",
    "美式": "American",
    "经济上行": "economic boom",
    "复古影视": "retro film and television",
    "胶片": "film grain",
    "摄影": "photography",
    "好莱坞": "Hollywood",
    "色调": "color grading",
    "蓝橙": "blue-orange",
    "荒诞": "absurd",
    "高调": "high-key",
    "电影": "cinematic film",
    "影视": "film and television",
    "昆汀": "Quentin Tarantino",
    "未来主义": "futurism",
    "俄罗斯": "Russian",
    "忧郁": "melancholic",
    "是枝裕和": "Hirokazu Kore-eda",
    "纪实": "documentary",
    "冷淡": "cold minimalist",
    "战争": "war",
    "恐怖": "horror",
    "宫斗权谋": "palace intrigue",
    "权谋": "intrigue",
    "冷峻": "stern",
    "荒野": "wilderness",
    "科幻": "sci-fi",
    # ---- 第二批：61 风格残余词补齐（按回归报告逐条补） ----
    # 注意：键越长越先匹配，故「动画/插画」等通用词放在具名风格键之后
    "动画": "animation",
    "插画": "illustration",
    "黑白": "black and white",
    "白色": "white",
    "复古": "retro",
    "儿童": "children's",
    "中国": "Chinese",
    "美国": "American",
    "时代": "era",
    "90": "1990s",
    "60": "1960s",
}

#: 英文风格后缀固定收尾（与中文 _QUALITY_TAIL 语义对齐）
_QUALITY_TAIL_EN = "highly detailed, delicate lighting, stable composition, no distortion"

#: 中文风格名的**构词后缀**，翻译时直接剥离（英文里不需要「风格/风/画风」这类词）。
#: 注意顺序：长后缀在前，避免「画风」被「风」先吃掉。
#: ⚠️ 不能无条件剥：若剥离后与词表**匹配度变差**（如「水墨国风」剥成「水墨国」
#: 破坏「国风」键），则保留原串。见 _strip_style_suffix 的择优逻辑。
_STYLE_SUFFIX_ZH = ("风格", "画风", "风")


def _lexicon_score(s: str) -> int:
    """词表对字符串的覆盖程度（命中的词表键总字数，越大越好）"""
    score = 0
    remaining = s
    for zh in sorted(_EN_STYLE_LEXICON, key=len, reverse=True):
        if zh and zh in remaining:
            score += len(zh)
            remaining = remaining.replace(zh, "")
    return score


def _strip_style_suffix(s: str) -> str:
    """剥离构词后缀，但仅在「不损害词表覆盖」时才剥。

    「2D现代都市风」→ 剥「风」后「2D现代都市」覆盖更好 → 剥；
    「2D水墨国风」  → 剥「风」后「2D水墨国」丢掉「国风」键 → 不剥。
    """
    for suf in _STYLE_SUFFIX_ZH:
        if s.endswith(suf) and len(s) > len(suf):
            cand = s[:-len(suf)]
            if _lexicon_score(cand) >= _lexicon_score(s):
                return cand
            break
    return s


def _translate_token_en(tok: str) -> List[str]:
    """把单个中文风格 token 翻成英文短语列表（最长键优先，避免「古风」吃掉「中国古风」）

    先择优剥离构词后缀（「风格 / 画风 / 风」），再做子串翻译；
    最后把**仍未译出的尾部中文后缀**丢掉（英文提示词里不能留中文）。
    历史缺陷：「2D现代都市风」翻完只剩「2D风」，英文提示词里混进中文，模型表达不稳。
    """
    out: List[str] = []
    remaining = _strip_style_suffix(str(tok or ""))
    for zh in sorted(_EN_STYLE_LEXICON, key=len, reverse=True):
        if zh and zh in remaining:
            en = _EN_STYLE_LEXICON[zh]
            if en not in out:
                out.append(en)
            remaining = remaining.replace(zh, "")
    rest = remaining.strip(" ，,、")
    # 兜底：纯中文残渣（如单独一个「风」）不写进英文提示词
    if rest and not any('\u4e00' <= c <= '\u9fff' for c in rest):
        out.append(rest)
    # 去冗余：短语已含语义时不再重复通用词（如 "Disney animation style" + "animation"）
    words = [w.strip() for w in out if w and w.strip()]
    pruned: List[str] = []
    for i, w in enumerate(words):
        if w == "animation" and any("animation" in o.lower() for o in words if o != w):
            continue
        if w == "illustration" and any("illustration" in o.lower() for o in words if o != w):
            continue
        pruned.append(w)
    return pruned


def style_suffix_en(style, *, with_tail: bool = True) -> str:
    """英文版风格后缀（同一风格串的英文表达，供 reference_prompt_en 使用）"""
    toks = [t for t in style_tokens(style)
            if not any(a in t for a in _ASPECT_TOKENS)]
    if not toks:
        return ""
    words: List[str] = []
    for t in toks:
        for en in _translate_token_en(t):
            if en and en not in words:
                words.append(en)
    if not words:
        return ""
    body = "Style: " + ", ".join(words)
    return f"{body}, {_QUALITY_TAIL_EN}" if with_tail else body


def sanitize_prompt_en(text: str, style=None) -> str:
    """清理英文资产提示词：修错译、去重复风格/质量词、规范标点

    实测两类问题：
    1. 把「国漫」硬译成 ``xuanxuan`` 这类不存在的罗马字（模型看不懂）；
    2. 自己写 ``masterpiece, best quality`` 或 ``Chinese animated style`` 之类
       风格/质量声明，与程序统一收尾的后缀重复。

    这里做确定性纠正，不依赖模型自觉。风格与质量声明一律**剥掉**，
    由 :func:`with_style_en` 在末尾统一给，保证风格只出现一次。
    """
    out = str(text or "").strip()
    if not out:
        return ""
    for pat, repl in _BAD_ROMANIZATION:
        out = re.sub(pat, repl, out, flags=re.IGNORECASE)
    out = re.sub(r"\bChinese\s+Chinese\b", "Chinese", out)   # 折叠替换产生的重复
    parts = [p.strip() for p in re.split(r"[,，]\s*", out) if p.strip()]
    kept: List[str] = []
    for p in parts:
        low = p.lower()
        if low in _QUALITY_WORDS_EN:
            continue
        if _STYLE_HINT_EN.search(p):
            continue
        kept.append(p)
    return ", ".join(kept).strip(" ,，")


def with_style_en(prompt: str, style, *, with_tail: bool = True) -> str:
    """英文提示词的幂等风格追加（已带同一风格则不重复）

    ⚠️ 幂等判断必须**在清洗之前**做：:func:`sanitize_prompt_en` 会剥掉自己上一步
    追加进去的 `Style: …` 与质量词，先清洗会让二次调用误判为「还没加过风格」，
    于是又追加一遍，永不幂等（实测踩过）。
    """
    raw = str(prompt or "").strip()
    suffix = style_suffix_en(style, with_tail=with_tail)
    if suffix and suffix in raw:
        return raw
    text = sanitize_prompt_en(raw, style)
    if not suffix:
        return text
    if not text:
        return suffix
    return f"{text}, {suffix}"


#: 「，XX风格。」这种子句级风格声明的尾巴（要求 风格 处于子句末尾，避免误吃
#: 「服装有中国风格的元素」这类把 风格 当普通名词的用法）
_STYLE_TAIL_RE = re.compile(
    r"[，,、\s]*[^，,。；;：:]{1,24}风格(?=\s*[。.]?\s*(?:$|[，,]))")


def _collapse_style(text: str, style) -> str:
    """把重复/过期的风格表述收敛掉，只留一份由调用方补写的规范后缀（自愈历史脏数据）

    两类脏数据都会命中：

    1. **同一风格写两遍**（实测最常见）::

           清瘦少年三视图，…，中国古风玄幻漫剧风格。风格：中国古风玄幻漫剧，画面精致…

    2. **风格换过、旧风格残留**（用户与总控反复调风格时的必然产物）::

           清瘦少年三视图，…，国漫3D渲染风格。风格：国漫3D渲染，画面精致…
           （此时项目风格已改为「中国古风玄幻漫剧」）

    ⚠️ 触发条件不能只看「当前风格串出现 ≥2 次」：换风格场景下新 marker 出现 0 次、
    `风格：` 子句也可能只有 1 处，但**风格表述有 2 处**（一处是「XX风格。」尾巴）。
    因此按 `风格` 总出现次数判定（≥2 才动手，单份风格绝不误伤），命中后把所有
    `风格：…` 子句与「，XX风格。」尾巴一并清掉，再由调用方补当前风格的规范后缀。
    """
    raw = str(text or "").strip()
    if not raw:
        return raw
    if raw.count("风格") <= 1:
        return raw
    out = re.sub(r"[。，；,;\s]*风格[：:][^。；;]*", "", raw)
    out = _STYLE_TAIL_RE.sub("", out)
    return out.strip(" 。，；,;")


# --------------------------------------------------------------------------- #
# 便捷组合
# --------------------------------------------------------------------------- #

def resolve(style, *, default_ratio: Optional[Tuple[int, int]] = None,
            megapixels: Optional[float] = None) -> Dict[str, object]:
    """一次拿到风格落地所需的全部派生值（供各生成入口统一调用）

    ``megapixels``：画幅像素预算（MP）。``None`` 时沿用 :func:`aspect_size` 的
    默认值（**2026-10-02 起 = :data:`ASSET_MEGAPIXELS_DEFAULT` = 1.5**，旧为 0.5）；
    分镜链路显式传 :func:`storyboard_megapixels`、
    视频链路显式传 :func:`video_megapixels`，以便各自独立按机器显存调档。
    """
    norm = normalize_style(style)
    ratio = aspect_ratio(norm) or default_ratio
    size = (aspect_size(ratio, megapixels) if megapixels is not None
            else aspect_size(ratio))
    return {
        "style": norm,
        "tokens": style_tokens(norm),
        "ratio": ratio,
        "size": size,
        "suffix": style_suffix(norm),
        "label": aspect_label(norm, size=size),
    }


def apply_asset_style(assets: Optional[List[dict]], style,
                      *, key: str = "reference_prompt_zh") -> int:
    """把风格后缀确定性地补进资产卡参考提示词，返回被改写的条目数。

    风格补写不再依赖模型自觉：bible 提示词已明确要求模型**不要**写风格词
    （写了会与这里追加的重复，实测出现过风格出现两遍），统一由这里收尾。

    ``key="reference_prompt_zh"``（默认）走中文后缀；``key`` 以 ``_en`` 结尾时
    走英文后缀并先做一次 :func:`sanitize_prompt_en` 纠错。
    """
    norm = normalize_style(style)
    if not norm or not assets:
        return 0
    is_en = str(key).endswith("_en")
    changed = 0
    for a in assets:
        if not isinstance(a, dict):
            continue
        base = a.get(key) or ""
        if is_en:
            # 参考图口径：英文同样剥离色调/影调 token 并补颜色保真条款
            ref_en = reference_style(norm)
            merged = with_style_en(base, ref_en) if ref_en else str(base or "").strip()
            merged = _append_clause(merged, REFERENCE_COLOR_FIDELITY_EN,
                                    REFERENCE_COLOR_FIDELITY_MARK_EN)
        else:
            base = base or a.get("appearance") or ""
            # ⚠️ 资产参考图必须走 with_reference_style（见其 docstring）：
            #    旧实现用 with_style 把「色调灰蓝压抑」整串拼进角色三视图 →
            #    皮肤被染蓝（2026-10-07 用户反馈）。
            merged = with_reference_style(base, norm)
        if merged and merged != str(base or "").strip():
            a[key] = merged
            changed += 1
    return changed


def apply_asset_style_all(assets: Optional[List[dict]], style) -> int:
    """中英双语一起补写风格（资产参考提示词的统一收尾入口）"""
    return (apply_asset_style(assets, style, key="reference_prompt_zh")
            + apply_asset_style(assets, style, key="reference_prompt_en"))
