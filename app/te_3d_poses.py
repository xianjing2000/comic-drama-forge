# -*- coding: utf-8 -*-
"""TE MAN 3D导演台 · 姿势层（关节表 / 预设 / 剧本到姿势的自动映射）。

数据来源：逆向官方 te_3d_director.js（2026-10-13），与导演台**同键名同数值**，
scene_json 回读到导演台里显示一致。逆向结论（勿再重查）：

* JOINTS 数组 27 条：每条 = 关节键 + Mixamo 骨骼名 + 单轴 + [min, max]（度）。
  应用方式见官方 applyEntityPose：bone.rotation[axis] += degToRad(value)，
  且 *Abd（外展）在左右两侧符号相反（r 侧取负）。
* 姿势预设 10 条 + STAND 基准 {lArmAbd:-80, rArmAbd:80, lFore:-8, rFore:8}
  （双臂自然下垂）。官方预设只有这 10 个；sit/wave/fight/akimbo 只是 JS 里
  别处的字符串，不在预设表里。
* Xbot.glb 共 66 根骨骼，含 20 根手骨（Thumb/Index/Middle/Ring/Pinky × 1-4 × 左右）。
  导演台 UI 只暴露到手腕，但数据层可驱动 —— 本模块补上「手型」，这是治手部畸变
  （杵状指/指头粘连）最直接的一环。

为什么需要它：分镜的 <image1> 构图基准图此前 poseValues 恒为空 →
人偶永远是 idle 站姿，剧本里写的「跪坐压膝」「抱拳行礼」「双手撑地起身」全部白写。
本模块把这些动作**自动**翻译成关节角，全程无人工。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: 官方 STAND 基准：双臂自然下垂（其余关节 0）
STAND: Dict[str, float] = {"lArmAbd": -80, "rArmAbd": 80, "lFore": -8, "rFore": 8}

#: 关节表：键 -> (Mixamo 骨骼名, 轴, min, max, 中文label)
#: 与官方 JOINTS 27 条逐条对齐（键名一致 → scene_json 可被导演台回读）
JOINTS: Dict[str, Tuple[str, str, float, float, str]] = {
    "bodyX":     ("mixamorig:Hips",          "x", -100, 100, "前倾"),
    "bodyY":     ("mixamorig:Hips",          "y",  -90,  90, "转身"),
    "bodyZ":     ("mixamorig:Hips",          "z",  -30,  30, "侧倾"),
    "spineX":    ("mixamorig:Spine1",        "x",  -35,  35, "前倾(弯腰)"),
    "spineY":    ("mixamorig:Spine1",        "y",  -40,  40, "扭转"),
    "spineZ":    ("mixamorig:Spine1",        "z",  -30,  30, "侧倾"),
    "headX":     ("mixamorig:Head",          "x",  -45,  45, "点头"),
    "headY":     ("mixamorig:Head",          "y",  -60,  60, "转头"),
    "headZ":     ("mixamorig:Head",          "z",  -35,  35, "歪头"),
    "lArmFwd":   ("mixamorig:LeftArm",       "x",  -90, 180, "前举"),
    "lArmAbd":   ("mixamorig:LeftArm",       "z",  -95,  35, "外展"),
    "lArmTwist": ("mixamorig:LeftArm",       "y",  -90,  90, "扭转"),
    "rArmFwd":   ("mixamorig:RightArm",      "x",  -90, 180, "前举"),
    "rArmAbd":   ("mixamorig:RightArm",      "z",  -35,  95, "外展"),
    "rArmTwist": ("mixamorig:RightArm",      "y",  -90,  90, "扭转"),
    "lFore":     ("mixamorig:LeftForeArm",   "y", -110,  10, "弯曲"),
    "rFore":     ("mixamorig:RightForeArm",  "y",  -10, 110, "弯曲"),
    "lHand":     ("mixamorig:LeftHand",      "x",  -60,  60, "弯曲"),
    "rHand":     ("mixamorig:RightHand",     "x",  -60,  60, "弯曲"),
    "lLegFwd":   ("mixamorig:LeftUpLeg",     "x",  -90,  50, "抬腿"),
    "lLegAbd":   ("mixamorig:LeftUpLeg",     "z",  -30,  45, "外展"),
    "rLegFwd":   ("mixamorig:RightUpLeg",    "x",  -90,  50, "抬腿"),
    "rLegAbd":   ("mixamorig:RightUpLeg",    "z",  -45,  30, "外展"),
    "lKnee":     ("mixamorig:LeftLeg",       "x",    0, 130, "弯曲"),
    "rKnee":     ("mixamorig:RightLeg",      "x",    0, 130, "弯曲"),
    "lFoot":     ("mixamorig:LeftFoot",      "x",  -40,  40, "勾绷"),
    "rFoot":     ("mixamorig:RightFoot",     "x",  -40,  40, "勾绷"),
}

#: 官方姿势预设 10 条（STAND 已展开为显式数值，便于直接比对与序列化）
PRESETS: Dict[str, Dict[str, object]] = {
    "stand":    {"label": "站立",     "values": dict(STAND)},
    "tpose":    {"label": "T型",      "values": {}},
    "walk":     {"label": "行走",     "values": {**STAND, "lLegFwd": -25, "rLegFwd": 20,
                                                 "lKnee": 20, "rKnee": 10, "lArmFwd": -25,
                                                 "rArmFwd": 30, "lFore": -25, "rFore": 25}},
    "run":      {"label": "跑步",     "values": {**STAND, "spineX": 15, "lLegFwd": -40,
                                                 "rLegFwd": 30, "lKnee": 30, "rKnee": 85,
                                                 "lArmFwd": 7, "rArmFwd": -5, "lFore": -95,
                                                 "rFore": 95, "lArmTwist": 21, "rArmTwist": 20}},
    "bow":      {"label": "鞠躬",     "values": {"bodyX": 0, "spineX": 35, "headX": 12,
                                                 "lArmAbd": -82, "rArmAbd": 82,
                                                 "lArmTwist": -40, "rArmTwist": 30}},
    "kick":     {"label": "踢球",     "values": {"spineX": -10, "rLegFwd": -70, "rKnee": 20,
                                                 "lKnee": 12, "lArmAbd": -45, "rArmAbd": 60,
                                                 "lArmFwd": -20, "rArmFwd": -20}},
    "reach":    {"label": "伸手",     "values": {"lArmAbd": -80, "lFore": -8, "spineX": 8,
                                                 "rArmFwd": 85, "rArmAbd": 55, "rFore": 0}},
    "kneelTwo": {"label": "双膝跪地", "values": {**STAND, "spineX": 6, "lLegFwd": -7,
                                                 "rLegFwd": -7, "lKnee": 97, "rKnee": 97,
                                                 "lFoot": 22, "rFoot": 22}},
    "prone":    {"label": "匍匐",     "values": {"bodyX": 88, "spineX": 8, "headX": -12,
                                                 "lArmAbd": -28, "rArmAbd": 28, "lArmFwd": -72,
                                                 "rArmFwd": -72, "lFore": -25, "rFore": 25,
                                                 "lLegFwd": 6, "rLegFwd": -8, "lKnee": 18,
                                                 "rKnee": 26, "lFoot": -12, "rFoot": -12}},
    "lieDown":  {"label": "躺下",     "values": {"bodyX": -88, "spineX": -4, "headX": 8,
                                                 "lArmAbd": -45, "rArmAbd": 45, "lArmFwd": -10,
                                                 "rArmFwd": -10, "lFore": -8, "rFore": 8,
                                                 "lLegFwd": 4, "rLegFwd": 4, "lKnee": 5,
                                                 "rKnee": 5, "lFoot": 0, "rFoot": 0}},
}

#: **我们自建**的预设（官方 10 个里没有，但剧本高频出现）。
#: ⚠️ 官方表只有 stand/tpose/walk/run/bow/kick/reach/kneelTwo/prone/lieDown ——
#: 「坐 / 挥手 / 指 / 蹲」这些只能自己用关节角拼。数值是工程初值，**逐张实拍校准**
#: （2026-10-13 渲染图确认姿态正确后才接进关键词表）。
#: 关节语义参照官方 JOINTS：l/rLegFwd 负值 = 大腿前抬；l/rKnee 正值 = 小腿后折；
#: l/rArmFwd 正值 = 前举；lFore 负值 / rFore 正值 = 该侧屈肘。
CUSTOM_PRESETS: Dict[str, Dict[str, object]] = {
    "sit": {"label": "坐（椅凳）", "values": {
        "lLegFwd": -85, "rLegFwd": -85, "lKnee": 85, "rKnee": 85,
        "lFoot": 5, "rFoot": 5, "spineX": 6,
        "lArmAbd": -70, "rArmAbd": 70, "lArmFwd": -10, "rArmFwd": -10,
        "lFore": -35, "rFore": 35}},
    "crouch": {"label": "蹲下", "values": {
        "lLegFwd": -70, "rLegFwd": -70, "lKnee": 100, "rKnee": 100,
        "lFoot": 18, "rFoot": 18, "spineX": 18, "headX": -8,
        "lArmAbd": -72, "rArmAbd": 72, "lArmFwd": -18, "rArmFwd": -18,
        "lFore": -55, "rFore": 55}},
    "wave": {"label": "挥手", "values": {
        "rArmFwd": 148, "rArmAbd": 22, "rArmTwist": -18, "rFore": 58,
        "lArmAbd": -80, "lFore": -8, "headX": -5, "spineY": -6}},
    "point": {"label": "指向", "values": {
        "rArmFwd": 92, "rArmAbd": 12, "rFore": 8,
        "lArmAbd": -80, "lFore": -8, "spineY": -8}},
}
PRESETS.update(CUSTOM_PRESETS)

#: 剧本动作关键词 -> 预设（从具体到通用，命中即停）。
#: 只覆盖官方 10 个预设；命中不了回落 stand（= 旧行为，零回归）。
_POSE_KEYWORDS = (
    ("prone",    ("匍匐", "趴在地上", "趴在", "俯卧", "趴着", "伏在地上", "趴伏")),
    ("lieDown",  ("躺下", "躺倒", "仰卧", "平躺", "倒在床上", "瘫在", "卧倒", "横躺")),
    ("kneelTwo", ("双膝跪", "跪坐", "跪下", "跪地", "长跪", "跪拜", "跪伏", "伏跪", "跪在")),
    ("crouch",   ("蹲下", "蹲在", "蹲身", "蹲伏", "半蹲", "屈膝蹲")),
    ("sit",      ("坐下", "落座", "端坐", "就座", "坐回", "坐进", "坐在", "席地而坐", "盘腿坐",
                  "盘坐", "对坐", "正坐", "坐定")),
    ("kick",     ("踢", "踹", "飞踢")),
    ("run",      ("奔跑", "狂奔", "疾冲", "冲向", "飞跑", "跑了", "奔来", "急奔", "飞奔", "逃跑", "逃走")),
    ("walk",     ("行走", "走过", "走远", "踱步", "迈步", "离开", "上前", "走向", "走来", "走进",
                  "走到", "退开", "后退", "退出", "跨过", "踏入", "步出", "起身")),
    ("bow",      ("鞠躬", "行礼", "作揖", "抱拳", "躬身", "拱手", "弯腰行礼")),
    ("wave",     ("挥手", "招手", "举手示意", "摆摆手")),
    ("point",    ("指向", "指着", "抬手一指", "直指", "虚点", "一指", "戳向")),
    ("reach",    ("伸手", "探手", "举起手", "抬手", "抬手去", "伸手去", "伸手抓", "递出", "接过")),
)

#: 手型（我们自建：官方 UI 未暴露手指，但 GLB 含 20 根手骨）。
#: 值为「手骨名 -> 弯曲角度(度)」，取每指 1..3 节（第 4 节为指尖，通常跟随）。
#: ⚠️ 轴与符号需在渲染实测里校准（Mixamo 手骨惯用 x 轴）；先给工程初值。
_FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def _finger_curve(shape: str) -> Dict[str, float]:
    """按手型生成手骨弯曲角（左右手同名骨骼各一份）。"""
    out: Dict[str, float] = {}
    prof = {
        "fist":  (70, 80, 70),
        "grip":  (55, 65, 55),
        "press": (35, 45, 40),
        "open":  (0, 0, 0),
        "point": (0, 0, 0),
    }.get(shape)
    if not prof:
        return out
    for side in ("Left", "Right"):
        for finger in _FINGERS:
            for seg in range(1, 4):
                if finger == "Thumb":
                    if shape in ("fist", "grip"):
                        deg = prof[seg - 1] * 0.5
                    else:
                        deg = prof[seg - 1] * 0.3
                elif shape == "point" and finger != "Index":
                    deg = 65.0
                else:
                    deg = prof[seg - 1]
                if deg:
                    out["mixamorig:%sHand%s%d" % (side, finger, seg)] = round(float(deg), 1)
    return out


#: 手型注册表（名称 -> {手骨: 角度}）；open = 空 map（全零 = 张开）
HAND_SHAPES: Dict[str, Dict[str, float]] = {
    "fist":  _finger_curve("fist"),    # 握拳
    "grip":  _finger_curve("grip"),    # 抓握（握剑/按压）
    "press": _finger_curve("press"),   # 按压/撑地
    "open":  _finger_curve("open"),    # 张开
    "point": _finger_curve("point"),   # 食指指向
}

#: 剧本里出现这些词时判定手型（顺序敏感：具体在前）
_HAND_KEYWORDS = (
    ("fist",  ("握拳", "攥拳", "双拳", "拳头", "捏拳", "紧握")),
    ("grip",  ("抓握", "握住", "握剑", "握刀", "握紧", "抓在手里", "捧着", "抱着", "捂")),
    ("press", ("撑地", "按住", "压在", "扶着", "撑住", "拍在", "贴在")),
    ("point", ("指向", "指着")),
    ("open",  ("张开手", "摊手", "手掌摊", "十指张开")),
)


def preset_values(name: str) -> Dict[str, float]:
    """取预设的关节角（未知预设 -> 空 dict）。"""
    item = PRESETS.get(str(name or "").strip())
    if not item:
        return {}
    return dict(item.get("values") or {})


def clamp_joints(values: Dict[str, float]) -> Dict[str, float]:
    """按官方 JOINTS 的 [min, max] 夹回合法域（未知键原样保留，便于手骨扩展）。"""
    out: Dict[str, float] = {}
    for k, v in (values or {}).items():
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        spec = JOINTS.get(k)
        if spec:
            _bone, _axis, lo, hi, _label = spec
            val = max(lo, min(hi, val))
        out[k] = round(val, 2)
    return out


def resolve_pose(text: str, default: str = "stand") -> Tuple[str, Dict[str, float], str]:
    """剧本动作文本 -> (预设名, 关节覆盖, 命中的关键词)。

    纯规则、零模型调用、确定性（与 te_3d_director 的既有纪律一致）。
    命中不了返回 (default, {}, "")，即旧行为（站立），零回归。
    """
    s = str(text or "")
    if not s.strip():
        return default, {}, ""
    for name, words in _POSE_KEYWORDS:
        for w in words:
            if w in s:
                return name, {}, w
    return default, {}, ""


def resolve_hand_shape(text: str) -> str:
    """剧本文本 -> 手型名（命中不了返回空串 = 不改手型）。"""
    s = str(text or "")
    for name, words in _HAND_KEYWORDS:
        for w in words:
            if w in s:
                return name
    return ""


def build_pose(text: str, extra: Optional[Dict[str, float]] = None,
               pose_hint: str = "") -> Dict[str, object]:
    """一步到位：文本 -> 可直接写进 scene_json 的姿势块。

    返回 {currentPreset, poseValues, handShape, reason}
    poseValues = 预设展开 + 显式覆盖 + 手型骨骼角，合并后夹回合法域。
    """
    # ⭐ 剧本显式声明的姿势优先（2026-10-13）：关键词表是在**猜**语义 —— 实测旧剧本
    #    46 镜只命中 2%，因为 description 的用词（"盘坐""虚点""压着膝头"…）远比词表丰富。
    #    而剧本 LLM 本来就知道这一镜人物在做什么，让它从固定清单里选一个，命中率接近 100%。
    #    字段缺失/非法 → 回落关键词（老剧本零回归）。
    _hint = str(pose_hint or "").strip()
    if _hint in PRESETS:
        text = ""
    else:
        _hint = ""
    preset, over, why = resolve_pose(text)
    if _hint:
        preset, why = _hint, ("pose字段:" + _hint)
    values: Dict[str, float] = {}
    if why or extra:
        # 只有**命中动作关键词**（或调用方显式给角度）才产出预设姿势。
        # 未命中 → 空：保持旧行为（渲染页继续播 idle 自然站姿），避免把全部
        # 分镜一次性切到 T-pose+STAND 基准 —— 那会让 46 镜的基准图同时变化，
        # 风险面太大且无从归因。这是刻意的零回归设计。
        values = dict(preset_values(preset))
        values.update(over or {})
        values.update(extra or {})
    shape = resolve_hand_shape(text)
    if shape and HAND_SHAPES.get(shape):
        values.update(HAND_SHAPES[shape])     # 手型可单独生效（叠加在 idle 之上）
    if not values:
        return {"currentPreset": "", "poseValues": {}, "handShape": shape,
                "reason": "no-match"}
    return {
        "currentPreset": preset if why else "",
        "poseValues": clamp_joints(values),
        "handShape": shape,
        "reason": why or ("preset:" + preset),
    }
