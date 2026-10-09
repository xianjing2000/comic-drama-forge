"""TE MAN 3D导演台 · scene_json 程序化生成器

把漫剧剧本里每镜的「人物站位 / 机位 / 景别」结构化信息，翻译成 TE_3D_Director
自定义节点认得的 ``scene_json``（version=4 协议），并派生出一条可读的
``composition_prompt``（构图提示词）。

## 背景（2026-09-26 逆向结论，勿再重查）

TE MAN 的 ``TE_3D_Director`` 节点（ComfyUI 自定义节点，``.pyd`` 二进制、无 Python 源码）
本质是**手动 3D 拖拽面板**，但它保存/回读的 ``scene_json`` 是纯 JSON 字符串。
前端 ``te_3d_director.js`` 的 ``serializeScene()`` 固定产出如下结构（version=4）：

    {
      "version": 4,
      "aspect": "9:16",              # 16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / 自由
      "viewCamera": ...,             # 导演视角相机状态（可缺省）
      "skeletonMode": false,         # 骨骼模式开关
      "activeCameraId": "cam_1",     # 当前机位 entity 的 id
      "scene": {
        "background": "#060608",     # 场景背景色（hex）
        "gridVisible": true,
        "groundVisible": true,
        "snap": true,
        "panorama": null             # 全景模式（本模块不生成全景，恒为 null）
      },
      "entities": [
        {
          "id": "char_1",
          "type": "character",       # character / camera / crowd / prop / skeleton / mannequin / custom
          "kind": "standard",        # 素体类型（standard 等）
          "name": "林风",
          "color": "#2f80ff",        # 人偶颜色（CHARACTER_COLORS 前 8 种）
          "visible": true,
          "uniformScale": 1.0,
          "heightScale": 1.0,        # 身高缩放（1.0 = 1.7m 标准身高）
          "height": 1.7,
          "girth": 1.0,              # 体型（胖瘦）
          "style": "neutral",
          "transform": {
            "position": [x, y, z],   # 站位（世界坐标，y 为离地高度）
            "rotation": [x, y, z],   # 朝向（欧拉角，弧度）
            "scale": [1, 1, 1]
          },
          "poseValues": {},          # 姿势（骨骼角，本模块置空 → 默认站姿）
          "currentPreset": "",       # 姿势预设名
          ...
        },
        { "id": "cam_1", "type": "camera", "kind": "custom", "name": "机位1",
          "transform": { "position": [...], "rotation": [...], "scale": [1,1,1] } }
      ]
    }

前端 ``looksLikeSceneJson()`` 的识别判据：``JSON.parse`` 成功且是对象，且
``Array.isArray(entities) || scene || version || aspect`` 任一命中。本模块恒输出
``version`` + ``aspect`` + ``entities``，一定被识别。

## 关键约束（本模块职责边界）

1. **两档用法**：
   * **文字软约束** —— ``block_annotation`` 把站位/机位写成一行英文，注入分镜图提示词；
   * **出图（2026-09-27 起）** —— ``build_render_plan`` 给出完整渲染计划，由
     ``app/te_3d_render.py`` 用无头浏览器把 scene_json 渲染成**站位/机位基准图**，
     作为分镜生成的 ``<image1>`` 构图基准（详见该模块；TE_3D_Director 节点自身
     仍然是「只透传两个字符串、不出图」的浏览器面板，出图是我们在服务端复刻它的
     机位数学重渲染的）。
2. **scene_json 只放前端认得的字段**：渲染专用的 FOV / 视线落点等放在
   ``build_render_plan`` 的独立字段里，不塞进 scene_json（保证导演台回读不报错）。
3. **零模型调用、确定性输出**：纯规则从 shot 字段翻译，不烧 token、可复现。
4. 复用 ``comfyui_client.camera_key / camera_angle`` 的景别/机位判定，不重复造解析。
5. **机位在 +Z 侧**（角色面朝 +Z）—— 旧实现在 -Z 侧，等于拍后脑勺，勿改回去。
6. **取景与提示词景别规范同口径**：``_FRAMING_SPAN`` 以「米」为单位写取景区间，
   再换算成机位距离 + FOV（见该表上方注释），逐条对齐 ``SHOT_CAMERA_SPECS``。
   改这些数值前先想清楚「这张基准图是分镜的主要画布，取景会被强先验复刻」。
7. **不 fits 就不出图**：``build_render_plan`` 的 ``fits`` 是渲染的唯一闸门
   （空舞台 / 未声明景别 / 人数超容量 → 调用方回退文字站位锚点）。
"""

from __future__ import annotations

import json
import math
import re
from typing import Dict, List, Optional, Tuple

# 与前端 te_3d_director.js 对齐的常量（勿改数值，改了用户在导演台里打开会对不上）
SCENE_VERSION = 4
TARGET_CHARACTER_HEIGHT = 1.7
PANORAMA_RADIUS = 60.0
PANORAMA_SUBJECT_DISTANCE = 5.0
DEFAULT_BACKGROUND = "#060608"

# CHARACTER_COLORS（前端 8 色，按出场顺序轮转）
_CHAR_COLORS = [
    "#2f80ff", "#e978a8", "#ffb84d", "#41c97a",
    "#b58cff", "#ff6f61", "#35c9d0", "#d4d85b",
]

# ---- 机位推导（2026-09-27 重写；同日二次校准：取景范围对齐 SHOT_CAMERA_SPECS）----
#
# ⚠️ 旧实现把机位放在 **z 负半轴**（``base[2] = -dist * TARGET``），而本模块的朝向约定是
#    「rotation y=0 → 面朝 **+Z**（朝镜头）」（实测 Xbot.glb 骨骼：左右髋连线与 +X 夹角
#    3.7°，确认模型原生面朝 +Z）。两者矛盾 ⇒ 旧机位算出来在角色**背后**，用于手动拖拽的
#    导演台时没人察觉（用户自己摆机位），但一旦要**程序化出图**就会拍到后脑勺。
#    这里统一改为机位在 **+Z 侧**，与朝向约定对齐。
#
# ⭐ 取景范围**以米为单位**定义，再换算成「机位距离 + 视线落点高度」：
#      可见高度 = 2·d·tan(fov/2)（three.js 的 ``PerspectiveCamera.fov`` 是**垂直** FOV），
#      d = 取景高度 / (2·tan(fov/2))，视线落点 = 取景区间的中点。
#    这样表里写的「从 X 米取到 Y 米」可以直接与 ``comfyui_client.SHOT_CAMERA_SPECS``
#    的景别规范逐条对照 —— 基准图是分镜的 ``<image1>`` **主要画布**，而参考图的取景会被
#    模型当强先验复刻（见 ``app._pick_char_view`` 的长注释：参考图构图牵引正是「景别画不准」
#    的根因）。基准图取景与提示词景别一旦不一致，等于把「景别」这个分镜质检最大的一类
#    驳回又放大一遍。
#
#     景别   取景（相对地面，米）          主体占画面高   对应 SHOT_CAMERA_SPECS
#     ────  ──────────────────────────  ────────────  ─────────────────────────────
#     大特写  1.55 → 1.74（眼/面部一点）       ~85%       单点细节占画面 85% 以上
#     特写   1.47 → 1.80（头）                ~68%       面部占画面 70% 以上
#     近景   1.22 → 1.88（胸部以上至头顶）     ~37%       取景自胸部以上至头顶
#     中近景  0.95 → 1.88（腰部以上）          ~52%       腰部以上、胸部以下
#     局部   0.75 → 1.35（手/道具所在高度）    ~60%       只拍手部/道具，不出现完整人脸
#     中景   0.50 → 2.17（膝部以上）          ~73%       腰部或膝部以上；严禁膝盖以下/脚部
#     全景  -0.35 → 2.35（全身 + 环境）        ~64%       完整全身，占画面高度大半
#     远景  -1.35 → 3.45（宽环境）            ~36%       人物较小、环境为主体
#     大远景 -3.20 → 5.60（大空间）           ~18%       人物极小，环境与空间关系为主
# ⚠️ 中景下沿取 0.50 而非更低的 0.42：膝盖实测在 ≈0.45m（0.26×身高），下沿必须**在膝以上**，
#    否则渲染图里会露出一截小腿，与规范「严禁出现膝盖以下部位」直接冲突。
#
# 角色身高口径 = ``TARGET_CHARACTER_HEIGHT``（1.7m；渲染页归一化到 1.72m，差异可忽略）。
_FRAMING_SPAN: Dict[str, Tuple[float, float, float]] = {
    # ⚠️ 键集合必须覆盖 config.SHOT_TYPES 的全部景别（唯一权威表）：漏一个则该景别
    #    静默回落到默认档 → 3D 基准图的取景与提示词景别不一致，反而放大「景别画不准」。
    #    配套守卫 verify_shot_type_registry.py 会断言这里覆盖齐全。
    "大特写": (1.55, 1.74, 26.0),
    "特写": (1.47, 1.80, 30.0),
    "近景": (1.22, 1.88, 36.0),
    "中近景": (0.95, 1.88, 34.0),
    # 局部 = 手部/道具插入镜：取景落在「手持道具」的常见高度带（胸腹前 ~0.75~1.35m），
    # 收紧 FOV 让手/纸/秤占住画面，与 SHOT_CAMERA_SPECS「只拍手部/道具」对齐。
    "局部": (0.75, 1.35, 28.0),
    "中景": (0.50, 2.17, 38.0),
    "全景": (-0.35, 2.35, 40.0),
    "远景": (-1.35, 3.45, 40.0),
    "大远景": (-3.20, 5.60, 42.0),
}


def _framing_to_shot(bottom_y: float, top_y: float, fov: float) -> Tuple[float, float, float]:
    """取景区间（米）+ 垂直 FOV → ``(机位水平距离 m, 视线落点高度 m, FOV)``。"""
    span = max(0.05, float(top_y) - float(bottom_y))
    d = span / (2.0 * math.tan(math.radians(fov) / 2.0))
    return (round(d, 4), round((float(bottom_y) + float(top_y)) / 2.0, 4), float(fov))


#: 景别 → (机位到主体的水平距离 m, 视线落点高度 m, 垂直 FOV 度)：由 ``_FRAMING_SPAN`` 换算。
_FRAMING_SHOT: Dict[str, Tuple[float, float, float]] = {
    _k: _framing_to_shot(*_v) for _k, _v in _FRAMING_SPAN.items()
}

#: 本镜**未指定景别**（camera 只有机位/运镜，如「俯拍缓推」）时的取景档。
#: ⚠️ 这一档**只用来把渲染计划补齐字段，实际不产图**（见 ``build_render_plan`` 的 ``fits``）：
#:    此时提示词里写的是「按 SCENE AND ACTION 自行决定取景，**不要**默认中景或全景」，
#:    再塞一张带固定取景的基准图就是与提示词直接互斥（历史坑：编造景别导致模型摇摆）。
_FRAMING_UNSPEC: Tuple[float, float, float] = _FRAMING_SHOT["全景"]

#: 人偶**完整轮廓宽度**（米）：Xbot 站姿双臂自然下垂时实测 ≈ 0.60m（0.348×身高，
#: 2026-09-27 用 768 宽渲染图按「画面高 = 可见高」换算像素→米量得）。
#: ⚠️ 不是「肩宽 0.45」——决定会不会被画框切掉的是**轮廓宽度**（含手臂）。
_FIGURE_WIDTH = 0.62
#: 相邻两人**头部可分辨**所需的最小中心距（米）：0.24 ≈ 1.5 个头宽。
#: 这条是「人物数量」可读的底线（构图基准核对的第一条判据），比轮廓不重叠更宽松：
#: 真实的中景双人镜本来就允许手臂交叠，但要能一眼数出是两个人。
_HEAD_GAP = 0.24
#: 取景框边缘留白比例（0.94 = 最外侧的人轮廓离画面边缘还留 6% 半画幅余量）
_EDGE_MARGIN = 0.94
#: 单张基准图最多摆几个人（导演台一般摆 1-3 人；再多构图本身就数不清了）
_MAX_BLOCKING_CHARS = 3

# 机位角度 → (水平绕主体旋转角 度, 机位相对视线落点的高度偏移 m)
#   高度偏移 + lookAt(落点) 共同产生俯仰：俯拍把机位抬高（俯视），仰拍压低（仰视）。
#   水平角让机位绕到主体侧后方，产出侧向/过肩视角。
_ANGLE_SHOT: Dict[str, Tuple[float, float]] = {
    "平视": (0.0, 0.00),
    "俯拍": (0.0, 1.10),
    "仰拍": (0.0, -0.70),
    "环绕": (28.0, 0.00),
    "过肩": (16.0, 0.15),
    "斜侧": (34.0, 0.00),
}

# 画幅 → 多人站位的横向铺开**期望半宽**（米）。竖屏横向空间窄，横屏宽。
# ⚠️ 这只是**期望**，实际半宽由 ``_lateral_spread`` 按「不重叠 + 不出画」两条夹出来。
_SPREAD_ASPECT: Dict[str, float] = {
    "9:16": 1.05, "3:4": 1.35, "1:1": 1.70,
    "4:3": 2.10, "16:9": 2.60, "21:9": 3.00,
}
_SPREAD_DEFAULT = 1.70

# 画幅 → (宽, 高) 比例，用于渲染计划推像素尺寸
_ASPECT_RATIO: Dict[str, Tuple[int, int]] = {
    "9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1),
    "4:3": (4, 3), "3:4": (3, 4), "21:9": (21, 9),
}


def _safe_name(raw) -> str:
    """把角色/镜头名归一成安全字符串（去空白，缺省给占位）。"""
    s = str(raw or "").strip()
    return s or "未命名"


def _entity_id(prefix: str, idx: int) -> str:
    return f"{prefix}_{idx}"


def on_screen_characters(shot: dict) -> List[str]:
    """「本镜画面内可见角色」的**唯一权威解析**（= 剧本 ``characters_in_shot``）。

    ## 口径（2026-10-05 统一，勿再分叉）

    ``characters_in_shot`` 是剧本对**画面内可见角色**的权威声明。
    **台词 speaker 不是**出场角色 —— 画外音 / 旁白 / 电话另一头的声音都可以有
    speaker，但画面里一个人都没有。把 speaker 当成出场角色，会让「本该无人的道具
    特写镜」凭空多出一个（无面）人物，正是「本镜不该有人却有人」的根因。

    本函数是唯一来源：``te_3d_director`` 内部（``build_scene_json`` /
    ``build_composition_prompt`` / ``build_render_plan`` / ``block_annotation`` /
    ``blocking_spec_text``）与 ``app.py`` 的 3D 基准图注入判据**都调用它**，
    一处定义 → 多处同源。

    :param shot: 剧本镜头对象。
    :return: 归一化后的出场角色名列表（切分 → 去重保序 → 上限 6）。
    """
    chars = shot.get("characters_in_shot") or []
    if isinstance(chars, str):
        chars = [c.strip() for c in re.split(r"[，,、/]", chars) if c.strip()]
    chars = [c for c in chars if c]
    # 去重保序 + 上限（导演台一般摆 1-3 人）
    uniq: List[str] = []
    for c in chars:
        if c not in uniq:
            uniq.append(c)
    return uniq[:6]


def _parse_characters(shot: dict) -> List[str]:
    """从 shot 取出场角色名单（**只认 characters_in_shot**，与 on_screen_characters 同源）。

    ⚠️ 2026-10-05（修复「无角色镜头被 3D 人偶基准图污染」）：**删除了「characters_in_shot
    为空时回落台词 speaker」的兜底**。原兜底使本函数与 ``app.py`` 的 ``_match_shot_chars``
    （只读 characters_in_shot）口径不一致：道具特写镜（characters_in_shot=[]，仅画外音
    台词）被 3D 导演台判为「有角色」→ 渲出一张无面人偶基准图 → 作 ``<image1>`` 注入分镜
    提示词，而 ``COMPOSITION BASELINE`` 段要求「用其他参考图里的角色完全覆盖人偶」——
    本镜根本没有角色参考图可覆盖 → 模型只能照抄人偶。

    🔁 **回滚点**：若某日确实需要恢复「按台词 speaker 当出场角色」的旧行为，**不要**在本
    函数里加回兜底（那会让两处口径再次分叉、重现同一缺陷）；正确做法是让剧本侧**先**把
    画外音说话人写进 ``characters_in_shot``（或另立显式「出场角色」字段），再让本函数与
    ``app.py`` **同时**消费该字段。恢复前提：确认该 speaker 确实**在画面内**说话（非画外音）。
    """
    return on_screen_characters(shot)


def _position_for_index(idx: int, total: int,
                        spread: float = _SPREAD_DEFAULT) -> Tuple[float, float, float]:
    """按出场顺序推导站位（画面横向分布，面向镜头，z 轴前移拉开纵深）。

    约定：z 正方向 = 朝向镜头（屏幕外），所以人物站在 z=0 平面、面向 z 正。
    x 轴横向分布；多人时按序左右排开，中间者稍靠前（z 略正）。
    世界 +X 在**画面右侧**（正交右手系，机位在 +Z 侧看向 -Z），因此 idx 递增 = 从左到右，
    与 :func:`build_composition_prompt` / :func:`block_annotation` 的「最左…最右」文案同向。

    :param spread: 横向铺开**半宽**（米）。由 ``_lateral_spread`` 按「人数不重叠 + 身体不出画」
        推导（画幅只给期望值）。
    """
    if total <= 1:
        return (0.0, 0.0, 0.0)
    half = max(0.15, float(spread))
    x = (idx / (total - 1)) * 2 * half - half
    # 奇数人时「正中间」者靠前一点，形成层次；偶数人两侧对称、同平面
    mid = (total - 1) / 2
    z = 0.4 if (total % 2 == 1 and idx == mid) else 0.0
    return (round(x, 3), 0.0, round(z, 3))


def _rotation_for_index(idx: int, total: int) -> Tuple[float, float, float]:
    """朝向：默认面向镜头（y 轴旋转 0 = 面朝 z 正）。多人时两侧角色略向内转。"""
    if total <= 1:
        return (0.0, 0.0, 0.0)
    if idx < (total - 1) / 2:
        return (0.0, 0.35, 0.0)   # 左侧向右转
    if idx > (total - 1) / 2:
        return (0.0, -0.35, 0.0)  # 右侧向左转
    return (0.0, 0.0, 0.0)


def _subject_center(chars: List[str], spread: float) -> Tuple[float, float]:
    """主体水平中心（x, z）：多人时取各站位均值，单人/无人时取原点。"""
    n = len(chars)
    if n <= 0:
        return (0.0, 0.0)
    xs, zs = [], []
    for i in range(n):
        px, _py, pz = _position_for_index(i, n, spread)
        xs.append(px)
        zs.append(pz)
    return (round(sum(xs) / n, 3), round(sum(zs) / n, 3))


def visible_width(aspect: str, cam_key: str) -> float:
    """本镜**画面可见宽度**（米）：``2·d·tan(fov/2)·(w/h)``（d / fov 由景别档给出）。"""
    dist, _focus_h, fov = _FRAMING_SHOT.get(cam_key) or _FRAMING_UNSPEC
    aw, ah = _ASPECT_RATIO.get(str(aspect or "").strip(), _ASPECT_RATIO["9:16"])
    return 2.0 * dist * math.tan(math.radians(fov) / 2.0) * (aw / float(ah))


def _side_room(aspect: str, cam_key: str) -> float:
    """站位中心**最多**能离画面中线多远（米），保证整个人偶轮廓都在画内。

    = ``可见半宽 × 边缘留白 − 半轮廓宽``。中景（9:16）≈ 0.4697×0.94 − 0.31 = 0.132m。
    """
    half_frame = visible_width(aspect, cam_key) / 2.0
    return max(0.0, half_frame * _EDGE_MARGIN - _FIGURE_WIDTH / 2.0)


def framing_capacity(aspect: str, cam_key: str) -> int:
    """该「景别 + 画幅」下基准图**能同时摆下几个人**（头部可分辨、身体不出画）。

    推导：``n`` 人需要 ``2×(可见半宽×留白 − 半轮廓宽) ≥ (n−1)×最小中心距``
    ⇒ ``可见宽度×留白 ≥ 轮廓宽 + (n−1)×最小中心距``。下限 1、上限 ``_MAX_BLOCKING_CHARS``。

    下限取 1 的道理：单人特写本来就该让肩膀出画的（规范要求面部占 70% 以上），
    可见宽 0.19m 远小于轮廓宽 0.62m —— 这不是「摆不下」，而是**特写本身就只装一颗头**。
    """
    w = visible_width(aspect, cam_key)
    if w <= 0:
        return 1
    usable = w * _EDGE_MARGIN
    cap = 1
    while cap < _MAX_BLOCKING_CHARS and usable >= _FIGURE_WIDTH + cap * _HEAD_GAP:
        cap += 1
    return cap


def _lateral_spread(aspect: str, cam_key: str, n: int = 1) -> float:
    """站位横向铺开**半宽**（米）：够摆下且整个人偶轮廓不出画。

    三个约束取交（判据由 ``.workbuddy/test/verify_te_3d_render.py`` 逐条实证）：
      * **下界** ``(n−1)×最小中心距/2`` —— 相邻两人的头不能叠到数不出人数；
        ``n ≤ 1`` 返回 0（单人恒居中）。
      * **上界** ``_side_room`` —— 最外侧的人**连手臂**都要在画内。旧实现只按
        「可见宽 × 0.42」摆开，而 0.42 是**中心点**位置 ⇒ 中心落在 84% 半宽处、
        轮廓再往外伸半个身宽，实测两人中景/全景两侧**都被画框切掉**（渲染图上是半边人）。
      * 画幅期望值 ``_SPREAD_ASPECT``（竖屏窄、横屏宽）只作**期望**，被上面两条夹住。
    """
    if n <= 1:
        return 0.0
    need = (n - 1) * _HEAD_GAP / 2.0                       # 下界：头部可分辨
    limit = max(_side_room(aspect, cam_key), need)         # 上界：轮廓不出画
    base = _SPREAD_ASPECT.get(str(aspect or "").strip(), _SPREAD_DEFAULT)
    return round(min(max(base, need), limit), 4)


def build_camera(cam_key: str, cam_angle: str, center_x: float = 0.0, center_z: float = 0.0):
    """由景别 + 机位角度推导机位（世界坐标）。

    返回 ``(position, target, fov)``：

      * ``position`` = 主体中心水平位置 + 绕 Y 旋转 ``yaw`` 后的水平后退距离 ``d``，
        y = 视线落点高度 + 高度偏移；
      * ``target``   = 视线落点（主体中心，高度由景别决定）；
      * ``fov``      = 垂直视场角（度），与 ``d`` 联合决定取景范围。

    ⚠️ ``d`` 与 ``fov`` **成对定义在** ``_FRAMING_SHOT`` 里，别单独改一个 —— 改距离不改 FOV
    等于把景别整体缩放；两者一起改才能保持「特写只框住头肩」这类语义。
    """
    dist, focus_h, fov = _FRAMING_SHOT.get(cam_key) or _FRAMING_UNSPEC
    yaw_deg, v_off = _ANGLE_SHOT.get(cam_angle or "平视", _ANGLE_SHOT["平视"])
    yaw = math.radians(yaw_deg)
    # 机位在 +Z 侧（角色面朝 +Z）；yaw 让机位绕到主体侧方
    px = center_x + dist * math.sin(yaw)
    pz = center_z + dist * math.cos(yaw)
    py = focus_h + v_off
    position = (round(px, 3), round(py, 3), round(pz, 3))
    target = (round(center_x, 3), round(focus_h, 3), round(center_z, 3))
    return position, target, float(fov)


# --------------------------------------------------------------------------- #
# 显式站位（shot["blocking"]）—— 解决「8 个镜头只有 2 种舞台」
# --------------------------------------------------------------------------- #
# 背景（2026-10-01 实测）：剧本给每镜的只有 camera="中景固定" + characters_in_shot，
# 没有任何站位信息 → 同角色同景别的镜头只能推出同一张舞台 → 8 镜折叠成 2 种基准图。
# 这里让 shot 可以**显式声明**站位；缺失/不匹配时逐字回退旧的「按出场序排开」，零行为变更。
#
# shot["blocking"] 形如：
#   [{"name":"羡进","x":"left","depth":"front","facing":"right"},
#    {"name":"赵天霸","x":"right","depth":"front","facing":"left"}]
#   x ∈ left/center/right（画面左/中/右）｜depth ∈ front/mid/back（离镜头近/中/远）
#   facing ∈ camera/left/right/back（面朝镜头/画面左/画面右/背对镜头），留空 = 向内稍转
_X_SLOT = {"left": -1.0, "左": -1.0, "center": 0.0, "centre": 0.0, "middle": 0.0, "中": 0.0,
           "right": 1.0, "右": 1.0}
_DEPTH_Z = {"front": 0.45, "前": 0.45, "mid": 0.0, "middle": 0.0, "中": 0.0,
            "back": -0.7, "后": -0.7}
_FACING_Y = {"camera": 0.0, "镜头": 0.0, "front": 0.0,
             "left": -0.55, "左": -0.55, "right": 0.55, "右": 0.55,
             "back": math.pi, "背": math.pi, "away": math.pi}


def _blocking_lookup(shot: dict, chars: List[str]) -> Dict[str, dict]:
    """读出 shot["blocking"] 中**与出场角色同名**的条目；没有则返回空 dict（回退按序排）。"""
    raw = (shot or {}).get("blocking")
    if not isinstance(raw, list) or not raw:
        return {}
    want = {str(c).strip() for c in chars}
    out: Dict[str, dict] = {}
    for it in raw:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name") or "").strip()
        if nm and nm in want:
            out[nm] = {"x": str(it.get("x") or it.get("side") or "").strip().lower(),
                       "depth": str(it.get("depth") or "").strip().lower(),
                       "facing": str(it.get("facing") or "").strip().lower()}
    return out


def _blocking_pose(spec: dict, spread: float, idx: int, total: int):
    """由站位条目推 (position, rotation)；字段缺失的维度沿用旧的按序推导。"""
    base_pos = _position_for_index(idx, total, spread)
    base_rot = _rotation_for_index(idx, total)
    xk = _X_SLOT.get(spec.get("x") or "")
    px = round(xk * max(0.15, float(spread)), 3) if xk is not None else base_pos[0]
    pz = base_pos[2]
    dk = _DEPTH_Z.get(spec.get("depth") or "")
    if dk is not None:
        pz = dk
    yk = _FACING_Y.get(spec.get("facing") or "")
    ry = yk if yk is not None else base_rot[1]
    return (px, 0.0, pz), (0.0, ry, 0.0)


def _scene_center(scene: dict) -> Tuple[float, float]:
    """主体中心取**场景实体**的均值（显式站位生效后，机位瞄准必须跟着走）。"""
    xs, zs = [], []
    for e in ((scene or {}).get("entities") or []):
        if not isinstance(e, dict) or e.get("type") != "character":
            continue
        try:
            p = (e.get("transform") or {}).get("position") or [0, 0, 0]
            xs.append(float(p[0]))
            zs.append(float(p[2]))
        except (TypeError, ValueError, IndexError):
            continue
    if not xs:
        return (0.0, 0.0)
    return (round(sum(xs) / len(xs), 3), round(sum(zs) / len(zs), 3))


def build_scene_json(
    shot: dict,
    aspect: str = "9:16",
    characters: Optional[List[str]] = None,
) -> dict:
    """从单镜 shot 生成 TE_3D_Director 认得的 scene_json（dict）。

    :param shot: 剧本镜头对象（含 camera / characters_in_shot / dialogue / location）
    :param aspect: 画幅比例（"16:9"/"9:16"/"1:1"/"4:3"/"3:4"/"21:9"）
    :param characters: 显式指定的角色名单（缺省时从 shot 自动提取）
    :return: 可直接 json.dumps 的 scene dict（version=4 协议）

    ⚠️ **本 dict 只放 TE_3D_Director 前端认得的字段** —— 渲染用的 FOV 等私有量由
    :func:`build_render_plan` 单独返回，不塞进这里（塞了会让导演台回读多出未知字段）。
    """
    chars = characters if characters is not None else _parse_characters(shot)
    camera = str(shot.get("camera") or "").strip()

    # 景别/机位判定：复用 comfyui_client 的权威解析（避免重复造、避免两端标准错位）
    try:
        from shot_camera import camera_key, camera_angle  # noqa: PLC0415  2026-10-08 解耦
        _cam_key = camera_key(camera)
        _cam_angle = camera_angle(camera)
    except Exception:  # noqa: BLE001 - 离线/独立测试时降级
        _cam_key = ""
        _cam_angle = ""
    if _cam_key == "中景" and not camera:
        _cam_key = ""  # camera 为空时 camera_key 默认回「中景」，这里按「未指定」处理

    spread = _lateral_spread(aspect, _cam_key, len(chars))

    entities: List[dict] = []

    # ---- 角色 entity（站位核心）----
    total = len(chars)
    # 显式站位优先（缺失/不匹配 → 逐字回退旧行为，老剧本零影响）
    _blk = _blocking_lookup(shot, chars)
    for idx, name in enumerate(chars, start=1):
        if name in _blk:
            pos, rot = _blocking_pose(_blk[name], spread, idx - 1, total)
        else:
            pos = _position_for_index(idx - 1, total, spread)
            rot = _rotation_for_index(idx - 1, total)
        entities.append({
            "id": _entity_id("char", idx),
            "type": "character",
            "kind": "standard",
            "name": name,
            "color": _CHAR_COLORS[(idx - 1) % len(_CHAR_COLORS)],
            "visible": True,
            "uniformScale": 1.0,
            "heightScale": 1.0,
            "height": TARGET_CHARACTER_HEIGHT,
            "girth": 1.0,
            "style": "neutral",
            "transform": {
                "position": list(pos),
                "rotation": list(rot),
                "scale": [1.0, 1.0, 1.0],
            },
            "poseValues": {},
            "currentPreset": "",
        })

    # ---- 机位 entity（camera）----
    cx, cz = _subject_center(chars, spread)
    cam_pos, cam_target, _cam_fov = build_camera(_cam_key, _cam_angle, cx, cz)
    cam_rot = _camera_rotation(_cam_angle)
    cam_id = _entity_id("cam", 1)
    entities.append({
        "id": cam_id,
        "type": "camera",
        "kind": "custom",
        "name": f"机位·{_cam_key or '未指定'}{('·' + _cam_angle) if _cam_angle else ''}",
        "color": "#ff8a3d",   # 前端 CAMERA_BODY_COLOR
        "visible": True,
        "transform": {
            "position": list(cam_pos),
            "rotation": list(cam_rot),
            "scale": [1.0, 1.0, 1.0],
        },
        # 视线落点：前端当前不消费，但渲染器需要它来 lookAt（见 build_render_plan）
        "target": list(cam_target),
    })

    scene = {
        "version": SCENE_VERSION,
        "aspect": aspect,
        "skeletonMode": False,
        "activeCameraId": cam_id,
        "scene": {
            "background": DEFAULT_BACKGROUND,
            "gridVisible": True,
            "groundVisible": True,
            "snap": True,
            "panorama": None,
        },
        "entities": entities,
    }
    return validate_scene_json(scene)


# ===================== 数值 schema 校验（Toonflow 吸收点 #3，2026-10-02） =====================
# 对齐 Toonflow director3dNode 的 zod schema 思路：站位/机位的每个数值字段都有
# [min, max] 物理合法域，出界/NaN/inf 一律**夹回边界**（fail-open，不抛错、不拒产）。
# 为什么只在这里做：scene_json 是导演台回读与无头渲染的唯一数据源，基准图又是分镜的
# 主要画布 —— 一个异常值混进去就是黑图/相机飞出舞台；在唯一出口 clamp 一次即可覆盖
# 全部消费方（build_scene_json / build_render_plan 的返回都会过校验）。
import logging as _t3d_logging

_t3d_logger = _t3d_logging.getLogger(__name__)

#: 字段 → (min, max)。注意区分：entity.position.y 是「离地高度」（≥0），
#: 视线落点 target.y 允许为负（远景取景下沿到 -3.2m，见 _FRAMING_SPAN）。
_T3D_NUM_RANGE = {
    "pos_xz": (-50.0, 50.0),     # 舞台水平范围（全景半径 60 的内圈）
    "pos_y": (0.0, 10.0),        # 人偶离地高度（米）
    "look_y": (-6.0, 8.0),       # 视线落点高度（米，允许取景到地面以下）
    "rot": (-2.0 * math.pi, 2.0 * math.pi),
    "scale": (0.05, 12.0),
    "height": (0.3, 3.0),        # 人偶身高（米；heightScale=1.0 → 1.7m 标准）
    "girth": (0.2, 3.0),         # 体型（胖瘦）
    "fov_deg": (10.0, 120.0),    # 垂直 FOV（度）
}


def _t3d_clamp(value, rng, default=0.0):
    """数值 clamp：非法（NaN/inf/非数）回落 default，越界夹回 [min, max]。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    lo, hi = rng
    return min(hi, max(lo, v))


def _t3d_clamp_vec3(vec, key_xz: str, key_y: str, default=0.0) -> list:
    """[x, y, z] 向量 clamp：x/z 走水平域，y 走指定高度域；缺位补 default。"""
    out = list(vec) if isinstance(vec, (list, tuple)) else []
    out = [v for v in out][:3] + [default] * (3 - min(3, len(out)))
    return [
        _t3d_clamp(out[0], _T3D_NUM_RANGE[key_xz], default),
        _t3d_clamp(out[1], _T3D_NUM_RANGE[key_y], default),
        _t3d_clamp(out[2], _T3D_NUM_RANGE[key_xz], default),
    ]


def validate_scene_json(scene: dict) -> dict:
    """站位/机位数值 schema 校验（clamp 式，Toonflow #3）。

    对 entities 里每个 transform（position / rotation / scale）与体型字段做
    越界夹取、非法值回落安全默认；原 dict 就地修正并返回（调用方零感知）。
    """
    if not isinstance(scene, dict):
        return scene
    clamped = 0
    for ent in (scene.get("entities") or []):
        if not isinstance(ent, dict):
            continue
        tf = ent.get("transform")
        if isinstance(tf, dict):
            _old = (tf.get("position"), tf.get("rotation"), tf.get("scale"))
            new_pos = _t3d_clamp_vec3(tf.get("position"), "pos_xz", "pos_y")
            _raw_rot = (list(tf.get("rotation")) if isinstance(tf.get("rotation"), (list, tuple))
                        else [])
            _raw_rot = _raw_rot[:3] + [0.0] * (3 - min(3, len(_raw_rot)))
            new_rot = [_t3d_clamp(v, _T3D_NUM_RANGE["rot"]) for v in _raw_rot]
            new_scale = _t3d_clamp_vec3(tf.get("scale"), "scale", "scale", default=1.0)
            if (new_pos, new_rot, new_scale) != _old:
                clamped += 1
            tf["position"], tf["rotation"], tf["scale"] = new_pos, new_rot, new_scale
        for _k in ("uniformScale", "heightScale"):
            if _k in ent:
                ent[_k] = _t3d_clamp(ent.get(_k), _T3D_NUM_RANGE["scale"], default=1.0)
        if "height" in ent:
            ent["height"] = _t3d_clamp(ent.get("height"), _T3D_NUM_RANGE["height"],
                                       default=TARGET_CHARACTER_HEIGHT)
        if "girth" in ent:
            ent["girth"] = _t3d_clamp(ent.get("girth"), _T3D_NUM_RANGE["girth"], default=1.0)
        if ent.get("type") == "camera" and isinstance(ent.get("target"), (list, tuple)):
            ent["target"] = _t3d_clamp_vec3(ent.get("target"), "pos_xz", "look_y")
    if clamped:
        _t3d_logger.warning("[3D导演台] scene_json 数值越界已夹回合法域：%d 个实体", clamped)
    return scene


def validate_render_plan(plan: dict) -> dict:
    """渲染计划的数值校验：FOV / 机位坐标 / 视线落点 clamp（Toonflow #3）。"""
    if not isinstance(plan, dict):
        return plan
    plan["fov"] = _t3d_clamp(plan.get("fov"), _T3D_NUM_RANGE["fov_deg"], default=36.0)
    cam = plan.get("camera")
    if isinstance(cam, dict):
        cam["position"] = _t3d_clamp_vec3(cam.get("position"), "pos_xz", "pos_y")
        cam["target"] = _t3d_clamp_vec3(cam.get("target"), "pos_xz", "look_y")
    return plan


def _camera_rotation(cam_angle: str) -> Tuple[float, float, float]:
    """机位朝向：默认平视主体（y 轴 0）。俯拍略低头、仰拍略抬头。"""
    pitch = {"俯拍": -0.5, "仰拍": 0.5}.get(cam_angle, 0.0)
    return (round(pitch, 3), 0.0, 0.0)


def build_composition_prompt(shot: dict, characters: Optional[List[str]] = None) -> str:
    """从 shot 生成 composition_prompt（中文构图提示词，喂给导演台 composition_prompt）。

    这条提示词描述「谁站在哪、机位怎么摆」，既可作为导演台面板的参考文案，
    也可拼进下游分镜提示词作为空间锚点（软约束）。
    """
    chars = characters if characters is not None else _parse_characters(shot)
    camera = str(shot.get("camera") or "").strip()
    location = _safe_name(shot.get("location"))
    desc = str(shot.get("description") or "").strip()

    try:
        from shot_camera import camera_key, camera_angle  # noqa: PLC0415  2026-10-08 解耦
        cam_key = camera_key(camera)
        cam_angle = camera_angle(camera)
    except Exception:  # noqa: BLE001
        cam_key, cam_angle = "", ""
    if cam_key == "中景" and not camera:
        cam_key = ""

    parts: List[str] = []
    if location:
        parts.append(f"场景：{location}")
    if chars:
        # 站位描述：按出场顺序左右排布
        n = len(chars)
        if n == 1:
            parts.append(f"人物站位：{chars[0]} 居中，面向镜头")
        else:
            mapping = []
            for i, c in enumerate(chars):
                if n == 2:
                    w = "左侧" if i == 0 else "右侧"
                elif i == 0:
                    w = "最左"
                elif i == n - 1:
                    w = "最右"
                elif i == (n - 1) / 2:
                    w = "居中"
                else:
                    w = "偏左" if i < (n - 1) / 2 else "偏右"
                mapping.append(f"{c} 在{w}")
            parts.append("人物站位：" + "，".join(mapping) + "，均面向镜头")
    if cam_key:
        parts.append(f"景别：{cam_key}")
    if cam_angle:
        parts.append(f"机位：{cam_angle}")
    if desc:
        parts.append(f"画面要点：{desc}")

    return "；".join(parts) + "。"


def dumps_scene_json(scene: dict, indent: int = 2) -> str:
    """把 scene dict 序列化为 scene_json 字符串（与前端同步写入的格式一致）。"""
    return json.dumps(scene, ensure_ascii=False, indent=indent)


def plan_pixel_size(aspect: str, width: int = 768) -> Tuple[int, int]:
    """画幅 → 渲染像素尺寸（宽固定，高按比例；未知画幅按 9:16）。

    ⚠️ 本模块 ``_ASPECT_RATIO`` 的键沿用行业叫法，值的语义是 **(宽比, 高比)**：
      * ``"9:16"``（竖屏）→ ``(9, 16)`` → 高 = 宽 × 16/9 → 又高又窄 ✔
      * ``"16:9"``（横屏）→ ``(16, 9)`` → 高 = 宽 × 9/16 → 又宽又扁 ✔
    故 ``h = w * bh / bw`` 本身正确（守卫 verify_te_3d_render 断言
    ``9:16 → 高/宽 ≈ 16/9`` 一直通过）。

    ⭐ 真正的坑是**跨模块语义冲突**（2026-10-02 定位，G2 存量失败的真因）：
      ``style_kit.resolve()["ratio"]`` 返回 ``(16, 9)``＝「16:9 横屏」，
      ``style_kit.aspect_size((16,9), 0.8MP)`` = ``1216×672``；
      而调用方 ``app.py`` 把它手工拼成 ``"16:9"`` 传进来，本模块解出 ``1216×684``
      —— **同一画幅、两个像素**。该基准图作 ``<image1>`` 定画布，于是分镜图
      继承 ``684`` 高，与全局 ``672`` 打架。
    解法：调用方改用 :func:`fit_pixel_size`（按**目标像素**反推渲染尺寸，
    彻底绕开比例字符串的双语义），不要再手工拼 ``":".join(map(str, ratio))``。
    """
    bw, bh = _ASPECT_RATIO.get(str(aspect or "").strip(), _ASPECT_RATIO["9:16"])
    w = max(64, int(width))
    h = max(64, int(round(w * bh / bw)))
    return w, h


def fit_pixel_size(target_size, max_width: int) -> Tuple[int, int]:
    """把目标像素画幅 ``(W, H)`` 按 ``max_width`` 等比缩放成渲染尺寸。

    ⭐ 为什么不传 ``"W:H"`` 字符串：本模块 ``_ASPECT_RATIO`` 的键语义与
    ``style_kit`` 的 ``ratio`` 语义**相反**（见 :func:`plan_pixel_size`），
    字符串往返必然引入歧义。直接吃像素尺寸是**唯一不会误解**的口径：
    只要 ``(1216, 672)`` 进、``(1216, 672)`` 出，两侧就永远一致。

    返回 ``(w, h)``，已对齐到偶数（视频编码要求），且不低于 64。
    """
    try:
        tw, th = int(target_size[0]), int(target_size[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return (max(64, int(max_width)), max(64, int(round(max_width * 16 / 9.0))))
    if tw <= 0 or th <= 0:
        return (max(64, int(max_width)), max(64, int(round(max_width * 16 / 9.0))))
    w = max(64, int(max_width))
    h = max(64, int(round(w * th / float(tw))))
    # 偶数对齐：H.264/H.265 要求宽高为偶数，奇数会被编码器裁掉一行
    if w % 2:
        w -= 1
    if h % 2:
        h -= 1
    return (w, h)


def build_render_plan(shot: dict, aspect: str = "9:16", width: int = 768,
                      characters: Optional[List[str]] = None,
                      target_size=None, cam_key: Optional[str] = None,
                      cam_angle: Optional[str] = None) -> dict:
    """给「服务端 3D 站位图渲染器」的完整计划（``app/te_3d_render.py`` 消费）。

    与 :func:`build_scene_json` 的分工：
      * ``scene``  —— 纯 TE_3D_Director 协议，可原样喂给导演台回读；
      * ``camera`` —— 渲染器实际使用的机位（position / target / fov），
        position 与 scene_json 里 camera entity 的坐标**同源**，不会两边打架。

    返回字段：``{width, height, aspect, fov, camera:{position,target}, grid, scene,
    fits, char_count, capacity}``。纯规则、零模型调用、确定性输出（同一 shot 恒得同一
    计划，便于按哈希缓存 PNG）。

    ⭐ ``fits`` = 这张基准图**该不该产**。三连判据（任一不满足 → 调用方必须放弃出图，
    回退到「只注入文字站位锚点」的旧行为）：
      ① 本镜没有出场角色 —— 空舞台，渲出来只有 ``#060608`` 黑底（构图基准图的全部价值
         就是「谁站在哪」，没有主体就没有可照搬的构图）；
      ② 本镜**没声明景别**（camera 只有机位/运镜）—— 基准图会强加一个固定取景，而提示词
         此时写的是「按 SCENE AND ACTION 自行决定取景、不要默认中景或全景」，两者互斥；
      ③ 人数超过该景别 + 画幅的**横向容量** —— 人会互相重叠或被裁到数不清人数，
         而「人物数量」正是构图基准核对的第一条判据，数不清就等于给了错信息。
    ⚠️ 这三条都**不能**靠「换一个更宽的景别」来凑：参考图的取景是强先验，改宽等于篡改
       提示词的景别约束（那正是本仓「景别画不准」的根因）。
    """
    chars = characters if characters is not None else _parse_characters(shot)
    scene = build_scene_json(shot, aspect=aspect, characters=chars)

    camera_str = str(shot.get("camera") or "").strip()
    # ⭐ 九宫格「逐格构图基准」扩展（2026-10-03）：调用方可强制指定某格的
    #    景别/机位（cam_key / cam_angle），渲染出该候选构图的 3D 站位基准图。
    #    传入 None 时沿用旧口径（从 shot.camera 推导）。这一档专供九宫格逐格
    #    构图基准用；单镜/旧调用方不传 → 行为与历史完全一致。
    _override = bool(cam_key or cam_angle)
    if not _override:
        try:
            from shot_camera import camera_key, camera_angle  # noqa: PLC0415  2026-10-08 解耦
            cam_key = camera_key(camera_str)
            cam_angle = camera_angle(camera_str)
        except Exception:  # noqa: BLE001
            cam_key, cam_angle = "", ""
        if cam_key == "中景" and not camera_str:
            cam_key = ""

    # ⚠️ 主体中心必须用**与 build_scene_json 相同的 spread** 计算（同画幅 + 同景别 + 同人数），
    #    否则机位会偏离主体。
    spread = _lateral_spread(aspect, cam_key, len(chars))
    # 瞄准点取**场景实体**均值：显式站位生效后位置会变，用旧的按序均值会瞄偏
    cx, cz = _scene_center(scene)
    position, target, fov = build_camera(cam_key, cam_angle, cx, cz)
    # ⭐ 优先按**目标像素**定尺寸（唯一无歧义口径，见 fit_pixel_size）；
    #    未给 target_size 时才回落到比例字符串（兼容既有调用方/守卫）。
    if target_size:
        w, h = fit_pixel_size(target_size, width)
    else:
        w, h = plan_pixel_size(aspect, width)
    capacity = framing_capacity(aspect, cam_key)
    _plan = {
        "width": w,
        "height": h,
        "aspect": aspect,
        "fov": fov,
        "camera": {"position": list(position), "target": list(target)},
        "grid": False,
        "scene": scene,
        # 该不该真的出图（判据见 docstring；渲染器据此决定是否启动浏览器）
        "fits": bool(chars) and bool(cam_key) and len(chars) <= capacity,
        "char_count": len(chars),
        "capacity": capacity,
        "cam_key": cam_key,
    }
    return validate_render_plan(_plan)


def blocking_spec_text(shot: dict, aspect: str = "9:16") -> str:
    """把本镜的**确定性构图规格**压成一段文字，供质检核对构图。

    为什么用它替代「无面人偶预演图」（2026-10-01）：
      把 3D 基准图（灰/蓝无面人偶）与成品图一起送视觉质检会**严重污染**判定 ——
      实测同一张合格图：不送基准图 score=88 通过；送了 score=35 拒绝。
      反复加强提示词口径也压不住（build_blocking_note 已写明「人偶外观不得作为判定依据」），
      因为污染来自**图像本身**，不是措辞问题。
    而 3D 导演台本来就是**零模型、确定性**的：人数 / 左右顺序 / 景别 / 机位都能精确转文字。
    于是改成送「文字规格」：保住「有没有照构图出图」的核对能力，同时零视觉污染。

    返回空串表示本镜不该出基准图（``fits`` 为假），调用方据此不加这段口径。
    """
    try:
        plan = build_render_plan(shot, aspect=aspect)
    except Exception:  # noqa: BLE001 - 规格生成失败绝不影响质检主流程
        return ""
    if not plan.get("fits"):
        return ""
    ents = [e for e in ((plan.get("scene") or {}).get("entities") or [])
            if isinstance(e, dict) and e.get("type") == "character" and e.get("visible", True)]

    def _x(e):
        try:
            return float((e.get("transform") or {}).get("position", [0])[0])
        except (TypeError, ValueError, IndexError):
            return 0.0

    names = [str(e.get("name") or "?") for e in sorted(ents, key=_x)]
    cam_key = str(plan.get("cam_key") or "未指定")
    order = " → ".join(names) if names else "（无）"
    return (
        "\n【本镜构图规格（由 3D 导演台**确定性**生成，请按此核对成品图的构图）】\n"
        f"  · 景别：{cam_key}｜人物数：{len(names)}\n"
        f"  · 站位（画面从左到右）：{order}\n"
        "  · 核对口径：只比**人物数量 / 左右顺序 / 前后层次 / 景别 / 机位角度**；\n"
        "    人物的姿态、表情、动作过程、光影、道具细节、外观与配色允许不同，**不得**据此判缺陷；\n"
        "  · 仅当出现**明显**构图偏差（人数不符、左右颠倒、前后层次错乱、景别差两档以上、机位方向相反）时，\n"
        "    才在 issues 里写明「构图不符：<具体项>」并扣分。\n"
    )


def block_annotation(shot: dict, characters: Optional[List[str]] = None) -> str:
    """派生出「空间锚点」文本（英文，直接拼进分镜/视频提示词作为软约束）。

    与 composition_prompt 的区别：这条是给**生图/生视频模型**读的构图约束，
    用英文 + 明确的左右/前后/景别措辞，命中现有分镜提示词的 FRAMING/SCENE 语义。
    """
    chars = characters if characters is not None else _parse_characters(shot)
    camera = str(shot.get("camera") or "").strip()

    try:
        from shot_camera import camera_key, camera_angle  # noqa: PLC0415  2026-10-08 解耦
        cam_key = camera_key(camera)
        cam_angle = camera_angle(camera)
    except Exception:  # noqa: BLE001
        cam_key, cam_angle = "", ""
    if cam_key == "中景" and not camera:
        cam_key = ""

    lines: List[str] = []
    n = len(chars)
    if n:
        if n == 1:
            lines.append(f"{chars[0]} positioned center-frame, facing camera")
        else:
            for i, c in enumerate(chars):
                if n == 2:
                    side = "left" if i == 0 else "right"
                elif i == (n - 1) / 2:
                    side = "center"
                else:
                    side = "left" if i < (n - 1) / 2 else "right"
                lines.append(f"{c} on the {side}, facing camera")
    if cam_key:
        lines.append(f"framing: {cam_key}")
    if cam_angle:
        lines.append(f"camera angle: {cam_angle}")
    if not lines:
        return ""
    return "Blocking — " + "; ".join(lines) + "."
