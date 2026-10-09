# -*- coding: utf-8 -*-
"""景别 / 机位解析 —— 零业务依赖的叶子模块（2026-10-08 解耦）。

为什么单独拆出来：
    camera_key / camera_angle / camera_spec / shot_framing / shot_motion 原本住在
    comfyui_client（5050 行的 ComfyUI 客户端），但真正需要它们的是**纯文本解析**侧：
    app.py（生成/质检两端注入口径）与 te_3d_director.py（3D 导演台机位判定）。
    te_3d_director 为此反向 import comfyui_client，与 comfyui_client 内部的
    「3D 遮挡图」调用形成环（静态依赖分析里 8 模块大环的一条边）。

    这几支函数只依赖 config.SHOT_TYPES 与两张本地表，与网络/客户端无关，因此下沉为叶子
    模块：谁要解析景别谁就 import 它，不再经由 ComfyUI 客户端。comfyui_client 保留同名
    再导出，历史调用点与 .workbuddy/test 下的守卫（verify_camera_framing /
    verify_shot_type_registry）零改动。

⚠️ 单一事实源不变：SHOT_CAMERA_SPECS 必须与 config.SHOT_TYPES 逐字对齐，
   漂移时下方检查会打 error 日志，配套守卫会直接断言两表相等。
"""
import logging

from config import SHOT_TYPES

logger = logging.getLogger(__name__)


# 分镜图景别强约束：仅写「特写」二字时模型容易退化为中景/近景，这里给出显式构图规范。
#
# ⚠️ 键集合必须与 config.SHOT_TYPES（景别唯一权威表）**逐字对齐**：少一个景别 =
#    生成端没有构图规范（模型自由发挥）+ 质检端没有判定标准（判「不符」也没依据），
#    正是历史「景别判定把 43% 镜头误判为不合格」的土壤。下方 _MISSING_SPECS 会在
#    两表漂移时打 error 日志，配套守卫 .workbuddy/test/verify_shot_type_registry.py
#    会直接断言两表相等。
SHOT_CAMERA_SPECS = {
    "大特写": ("大特写镜头（extreme close-up）：只拍眼睛/手指/道具的一个点，该局部占据画面 85% 以上，"
               "背景完全虚化；**严禁退为特写、近景或中景**"),
    "特写": ("特写镜头（close-up）：镜头极贴近主体，人物面部（或手部、道具局部）占据画面 70% 以上面积，"
             "背景明显虚化，只呈现局部，严禁退为近景、中景或全景"),
    "近景": ("近景镜头（medium close-up）：取景自人物胸部以上至头顶，面部细节清晰，"
             "严禁退为中景或全景"),
    "中近景": ("中近景镜头（medium close-up，略松）：取景自人物腰部以上至头顶，比近景多带一点身体与手势，"
               "**严禁退为中景/全景**（不得出现腰部以下部位或大片地面）"),
    "局部": ("局部镜头（detail / insert shot）：只拍手部、道具或身体局部，**画面中不出现完整人脸**、"
             "不交代人物全身与所处环境；用于把叙事重心压到道具上（如递出的手绘纸币、电子秤、抽屉里的纸），"
             "**严禁退为近景/中景**"),
    "中景": ("中景镜头（medium shot）：取景自人物腰部或膝部以上至头顶，人物占画面一半左右，"
             "可带入部分环境；**严禁退为全景/远景**（不得出现膝盖以下部位、脚部或大片地面）"),
    "全景": "全景镜头（wide shot）：完整呈现人物全身及其所处环境，人物占画面高度的大半；**不得退为远景色块**",
    "远景": "远景镜头（long shot）：人物在画面中较小、环境为主体，强调空间感与氛围；**不得推成中景/近景**",
    "大远景": ("大远景镜头（extreme long shot）：人物在画面中极小（可为剪影或色点），"
               "环境与空间关系为主体；**不得推成中景/近景**"),
}

#: 景别权威表与生成端规范的一致性检查（漂移必须**可见**：error 日志 + 守卫断言）
_MISSING_SPECS = [k for k in SHOT_TYPES if k not in SHOT_CAMERA_SPECS]
_EXTRA_SPECS = [k for k in SHOT_CAMERA_SPECS if k not in SHOT_TYPES]
if _MISSING_SPECS or _EXTRA_SPECS:
    logger.error("景别权威表 config.SHOT_TYPES 与 SHOT_CAMERA_SPECS 不一致：缺规范=%s 多出=%s",
                 _MISSING_SPECS, _EXTRA_SPECS)

#: 景别关键字的解析顺序（**长词优先**）：剧本里 camera 字段常是「景别+运镜」的复合写法
#: （如「特写推入」「全景升降」「中景跟拍」「中近景轻推」），必须按关键字解析，不能只做精确匹配。
#: ⚠️ 必须长词优先：否则「中近景」会被「近景」抢先命中、「大特写」会被「特写」抢先命中。
#: 由权威表派生，杜绝手写顺序漏词（历史缺陷：手写顺序里没有新景别 → 永远解析不出来）。
_CAMERA_KEY_ORDER = tuple(sorted(SHOT_TYPES, key=len, reverse=True))

#: 机位/视角关键字。与景别**正交**：剧本 camera 字段里既有景别（中景/特写）也有机位（俯拍/仰拍）。
#: ⚠️ 历史缺陷：机位以前完全没人解析，等于白写在剧本里 —— 生成端不知道要俯拍，
#: 质检端也没有依据判机位，于是「要求俯拍却给了平视」既没被约束也没被检出。
_CAMERA_ANGLE_SPECS = {
    "俯拍": "俯拍（高角度）：镜头高于主体自上向下俯视，画面能看到主体顶部/脚前的地面",
    "仰拍": "仰拍（低角度）：镜头低于主体自下向上仰视，主体显得高大压迫",
    "平视": "平视：镜头与主体视线同高",
    "环绕": "环绕：镜头绕主体转动（在静帧里体现为明显的侧向机位）",
    "过肩": "过肩：越过前景人物肩部拍向主体",
    "斜侧": "斜侧机位：镜头相对主体明显偏斜（非正面）",
}
_CAMERA_ANGLE_ORDER = ("俯拍", "仰拍", "平视", "环绕", "过肩", "斜侧")

#: 机位**同义词**：剧本写法很自由，只认「俯拍」不认「俯视」等于漏掉一半机位标注
#: （漏掉的后果与「机位没人解析」一样：生成端不约束、质检端不判定）。
#: 解析顺序：先用 _CAMERA_ANGLE_ORDER 的正式词（具体优先），再回落到本表。
_CAMERA_ANGLE_ALIASES = {"俯视": "俯拍", "高角度": "俯拍", "高机位": "俯拍",
                         "仰视": "仰拍", "低角度": "仰拍", "低机位": "仰拍",
                         "平角": "平视", "水平视角": "平视",
                         "环摇": "环绕", "绕拍": "环绕"}

def shot_framing(shot) -> str:
    """本镜**景别**的权威取值（A1）：优先读 ``shot_type``，缺失回退解析 ``camera`` 复合串。

    为什么要有这个统一入口：``camera`` 是「景别+运镜」复合字符串（「特写推入」），十余处
    消费点各自调 ``camera_key()`` 解析，一旦口径漂移就会**同时**污染生成端与质检端
    （历史坑：猜错景别曾让分镜质检通过率掉到 57%）。新剧本写入 ``shot_type`` 作单一权威，
    这里优先读它；旧剧本该字段为空 → 回退 ``camera_key(camera)``，与改动前**逐字一致**。
    """
    if not isinstance(shot, dict):
        return ""
    st = str(shot.get("shot_type") or "").strip()
    if st:
        return st if st in SHOT_CAMERA_SPECS else camera_key(st)
    return camera_key(str(shot.get("camera") or "").strip())


def shot_motion(shot) -> str:
    """本镜**运镜**的权威取值（A1）：优先读 ``camera_motion``；缺失返回空串（宁可不说）。

    ⚠️ 不回退解析 camera：运镜表在 ``h3_prompt_kit._CAMERA_MOVE_EN``，这里不跨模块反向
    依赖；且下游 ``_camera_move_en(camera)`` 本来就是吃整个 camera 串，旧剧本走原路径即可。
    """
    if not isinstance(shot, dict):
        return ""
    return str(shot.get("camera_motion") or "").strip()


#: 景别未指定时的判定标准（camera 只给了机位/运镜）。
#: 这段文字会被同时注入**生成端**与**质检端**，因此措辞必须两边都说得通。
CAMERA_UNSPECIFIED_SPEC = (
    "本镜未指定景别（camera 字段只给了机位/运镜，如「俯拍缓推」「环绕慢摇」）："
    "取景范围以「动作与画面内容」的描述为准，**不要按某个固定景别去套**，"
    "也**不得据此判定景别不符**；只判「机位/构图/主体清晰度/是否崩坏」"
)


def camera_key(camera) -> str:
    """从复合写法里解析出景别关键字（``特写推入`` → ``特写``；``全景升降`` → ``全景``）

    ⚠️ 这修的是一个**双向错位**的根因：
    剧本的 camera 字段是「景别+运镜」（特写推入 / 中景跟拍 / 全景升降），而
    ``SHOT_CAMERA_SPECS`` 只有精确键。原实现 ``SPECS.get(camera) or SPECS["中景"]``
    对任何复合写法都回落到**中景规格** —— 于是生成端给「特写推入」的镜头写的是
    「中景：腰部以上至头顶」，而质检端读的是字面「特写」，两端同时错位，
    实测分镜图质检通过率仅 57%、失败原因几乎全是「景别不符」。

    ⚠️ **2026-09-20 再修：只给机位/运镜、没有景别词时不再猜「中景」，改返回空串（未指定）。**
    实测《蛊真人》ep02 shot_13：``camera = "俯拍缓推"``，description 是
    「镜头自方源脚面俯拍：灰白山石上积了一大滩血水…他清瘦的靴底半浸其中」——
    本质是**脚部俯拍特写**。猜成「中景」会同时污染两端：
      · 生成端 → 注入「中景：取景自腰部或膝部以上」与 description 的脚部俯拍
        **直接互斥**，模型在两条矛盾指令间摇摆，6 次重试出的全是「全景 + 平视」；
      · 质检端 → 拿「中景（腰部或膝部以上）」去判一张脚部俯拍图，必然判「景别不符」，
        该镜**永远不可能通过**，白烧 6 次 GPU（max_retries=5）。
    返回空串后：生成端不注入景别硬约束、质检端不做景别判定 ——
    「宁可不说，也不要拿一个猜错的标准去判」。
    """
    s = str(camera or "").strip()
    if not s:
        return "中景"
    if s in SHOT_CAMERA_SPECS:
        return s
    for k in _CAMERA_KEY_ORDER:
        if k in s:
            return k
    # 只有机位/运镜词（俯拍缓推 / 环绕慢摇 / 拉远）→ **不猜**，交回上层按「未指定」处理
    return ""


def camera_angle(camera) -> str:
    """解析机位/视角（``俯拍缓推`` → ``俯拍``）；没有机位词时返回空串。

    与 :func:`camera_key`（景别）正交：两者都要各自注入生成端与质检端，
    否则「要求俯拍却给了平视」这类偏差既没人约束也没人检出。
    """
    s = str(camera or "").strip()
    for k in _CAMERA_ANGLE_ORDER:
        if k in s:
            return k
    for alias, key in _CAMERA_ANGLE_ALIASES.items():
        if alias in s:
            return key
    return ""


def camera_spec(camera) -> str:
    """取景别（镜头类型）的**权威判定标准**（生成端与质检端共用同一份）

    见 :func:`camera_key` 说明：必须能解析复合写法，否则两端标准会错位；
    景别确实未给时返回 :data:`CAMERA_UNSPECIFIED_SPEC`（而不是编一个中景）。
    """
    k = camera_key(camera)
    return SHOT_CAMERA_SPECS[k] if k else CAMERA_UNSPECIFIED_SPEC
