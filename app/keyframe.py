# -*- coding: utf-8 -*-
"""关键帧驱动视频模式（P1-2）

传统「参考图模式」把分镜图 + 主角锚点图丢给 H3，模型只能猜运动轨迹，
构图与位置可控性差、容易跑偏（人物漂移、出画、动作与剧本不符）。

关键帧驱动改为：
    1) 先用分镜图（=首帧）经 Qwen Edit 生成该镜动作结束时的「尾帧」
    2) H3 的段参考图槽位改为注入 [首帧, 尾帧]
       —— 让模型在两端之间插值运动，首尾构图都被锚死

跨镜链式（chain_mode，2026-09-17 新增）：
    参考工作流的做法是「上一镜的尾帧 = 下一镜的首帧」，镜与镜之间首尾相接，
    整集才连得起来。此前每镜都拿自己的分镜图当首帧，导致相邻两镜的画面
    各画各的（用户反馈：每个分镜的首尾帧都不一样）。
    链式模式下 shot N 的首帧 = shot N-1 的尾帧（存在时），
    尾帧仍由该首帧经 Qwen Edit 推进得到，H3 参考槽位 = [上一镜尾帧, 本镜尾帧]。
    - auto（默认）：仅在相邻两镜同场景（location/scene 一致）时串帧，跨场景不串；
    - always：无条件串帧；
    - off：保持旧行为（每镜用自己的分镜图当首帧）。

零行为变更原则：本模块只用既有 comfyui_client 接口，不新增任何模型依赖。
若 H3 工作流只有 1 个参考图槽位，则自动退化为「只锚首帧」，并在报告中明确标注。
"""
from __future__ import annotations

import logging
import os
import random
import shutil
from typing import Callable, Dict, List, Optional, Tuple

import style_kit
import shot_key

logger = logging.getLogger(__name__)

# 链式模式：auto=同场景才串 / always=无条件串 / off=关闭（旧行为）
CHAIN_MODES = ("auto", "always", "off")
DEFAULT_CHAIN_MODE = "auto"


def norm_chain_mode(mode) -> str:
    """归一化链式模式参数，非法值回落默认（永不抛异常）"""
    m = str(mode or "").strip().lower()
    return m if m in CHAIN_MODES else DEFAULT_CHAIN_MODE

# 按镜头类型给出的「运动收尾」提示，避免尾帧与首帧几乎无差别（无运动可插值）
_MOTION_BY_CAMERA = {
    "特写": "动作收尾：人物完成一个细微的表情/眼神变化（如抬眼、垂眸、嘴角微动），"
            "肩部与头部位置随动作小幅移动，取景仍保持肩部以上特写。",
    "近景": "动作收尾：人物上半身完成一次小幅位移或转身，手臂自然落位，"
            "取景仍保持胸部以上近景。",
    "中景": "动作收尾：人物完成该镜头描述的主要动作（起身/转身/抬手/迈步），"
            "身体重心发生明确位移，取景仍保持中景不变。",
    "全景": "动作收尾：人物在场景中完成一次位移或环境变化（雾散/涟漪扩散/光影移动），"
            "人物在画面中的位置相比首帧有明显变化，取景仍保持全景。",
    "远景": "动作收尾：人物或主体在广阔场景中完成位移，环境光影随时间推进，"
            "主体在画面中的位置相比首帧有明显变化。",
    "俯拍": "动作收尾：自上方俯视的视角下，人物完成该镜头的主要动作，"
            "地面投影与相对位置发生可见变化，仍保持俯拍构图。",
    "仰拍": "动作收尾：自下方仰视的视角下，人物完成该镜头的主要动作，"
            "身体姿态发生可见变化，仍保持仰拍构图。",
    "空镜": "动作收尾：环境本身发生变化（雾气流散、水波扩散、光线移动），"
            "构图与镜头位置保持不变。",
}

_DEFAULT_MOTION = ("动作收尾：人物完成该镜头描述的主要动作，身体姿态与画面内位置"
                   "相比首帧有明确变化，机位与焦段保持不变。")


def motion_hint(camera: str) -> str:
    """按镜头类型取运动收尾提示（未知类型回退通用描述）

    ⚠️ ``camera`` 实际是「景别+运镜」复合词（``中景跟拍`` / ``特写推入``），
    本函数只按**景别**选收尾提示 —— 运镜另有 :func:`movement_hint` 负责补一句
    「镜头本身怎么动」，二者合起来才是完整的首尾帧差异描述（2026-09-25 补）。
    """
    cam = (camera or "").strip()
    if not cam:
        return _DEFAULT_MOTION
    for key, val in _MOTION_BY_CAMERA.items():
        if key in cam:
            return val
    return _DEFAULT_MOTION


#: 运镜 → 尾帧「镜头位移」提示。为什么必须补这一句：尾帧是由首帧经 Qwen Edit 推进
#: 生成的图片，如果提示词只说「人物完成动作」而不说「镜头也移动了」，那么
#: ``中景跟拍`` 这类镜头推进出来的尾帧会与人像原地不动，H3 在首尾之间插值就没有
#: 位移可补 —— 运镜同样丢失，只是丢在尾帧这一环（与 h3_prompt_kit 的丢失点不同）。
#: 键与 :data:`h3_prompt_kit._CAMERA_MOVE_EN` 同序（长词优先），保持两处口径一致。
_MOVEMENT_BY_CAMERA = (
    ("急推", "镜头同时快速推近，主体在画面中明显放大"),
    ("推入", "镜头同时缓慢推近，主体占比略微增大、背景略微收窄"),
    ("推进", "镜头同时缓慢推近，主体占比略微增大"),
    # ---- 强度/方式变体（与 h3_prompt_kit._CAMERA_MOVE_EN 同口径，2026-09-29）----
    # ⚠️ 历史缺口：参考片 92 镜拆解里占非固定运镜绝大多数的「跟随 / 轻推 / 轻摇」
    #    在这里**一个都认不出** → movement_hint 返回空串 → 尾帧提示词不含任何镜头位移
    #    → H3 在首尾帧之间没有位移可插值，成片就是「静帧微动」。必须逐条补上。
    ("跟随", "镜头同时跟随主体平移，主体在画面中位置基本不变、背景持续后退"),
    ("跟移", "镜头同时跟随主体平移，主体在画面中位置基本不变、背景持续后退"),
    ("轻推", "镜头同时轻微推近，主体占比略微增大、背景略微收窄"),
    ("缓推", "镜头同时缓慢推近，主体占比略微增大、背景略微收窄"),
    ("慢推", "镜头同时缓慢推近，主体占比略微增大"),
    ("推近", "镜头同时缓慢推近，主体占比略微增大"),
    ("轻摇", "镜头同时轻微横向摇移，画面左右两侧内容发生可见位移"),
    ("缓摇", "镜头同时缓慢横向摇移，画面左右两侧内容发生可见位移"),
    ("慢摇", "镜头同时缓慢横向摇移，画面左右两侧内容发生可见位移"),
    ("上摇", "镜头同时向上摇移，画面下沿内容进入、上沿内容移出"),
    ("下摇", "镜头同时向下摇移，画面上沿内容进入、下沿内容移出"),
    ("甩镜", "镜头同时快速甩动，画面产生明显方向性拖影位移"),
    ("横移", "镜头同时横向平移，画面整体产生视差位移"),
    ("左移", "镜头同时向左平移，画面整体产生视差位移"),
    ("右移", "镜头同时向右平移，画面整体产生视差位移"),
    ("拉出", "镜头同时缓慢拉远，主体占比略微减小、环境交代更完整"),
    ("拉远", "镜头同时缓慢拉远，主体占比略微减小、环境交代更完整"),
    ("拉镜", "镜头同时缓慢拉远，画面露出更多环境"),
    ("摇拍", "镜头同时横向摇移，画面左右两侧内容发生可见位移"),
    ("摇镜", "镜头同时横向摇移，画面左右两侧内容发生可见位移"),
    ("跟拍", "镜头同时跟随主体平移，主体在画面中位置基本不变、背景持续后退"),
    ("跟镜", "镜头同时跟随主体平移，主体在画面中位置基本不变、背景持续后退"),
    ("平移", "镜头同时横向平移，画面整体产生视差位移"),
    ("移镜", "镜头同时横向平移，画面整体产生视差位移"),
    ("升降", "镜头同时垂直升降，地平线与取景高度发生可见变化"),
    ("环绕", "镜头同时绕主体环绕，主体的朝向与背景相对关系发生变化"),
    ("旋转", "镜头同时绕主体环绕，背景透视关系发生变化"),
    ("变焦", "镜头同时变焦收紧取景，景深变浅、背景更虚化"),
    ("手持", "镜头带轻微手持晃动，构图边缘有细微漂移"),
    ("定格", "镜头保持静止，画面定格在动作停住的瞬间"),
    ("定帧", "镜头保持静止，画面定格在动作停住的瞬间"),
    ("固定", "机位保持完全固定，只有画面内主体在动"),
    # ---- 「固定/剪辑转场」的口语变体（与 h3_prompt_kit 同口径，2026-09-29）----
    # 实测真实剧本里的「定拍 / 静止 / 静态 / 快切 / 叠化」共 30+ 镜；不补则
    # movement_hint 返回空串 → 尾帧无位移提示 → H3 插值成「静帧微动」。
    ("定拍", "机位保持完全固定，只有画面内主体在动"),
    ("静止", "机位保持完全固定，只有画面内主体在动"),
    ("静态", "机位保持完全固定，只有画面内主体在动"),
    ("快切", "机位保持完全固定，本段内不产生任何镜头位移"),
    ("叠化", "机位保持完全固定，本段内不产生任何镜头位移"),
    ("硬切", "机位保持完全固定，本段内不产生任何镜头位移"),
)

#: 运镜**核心动作**兜底（尾帧口径）：词典外的写法（「缓缓下摇」「轻微横移」…）也补上
#: 位移提示。与 h3_prompt_kit._MOVE_CORE 同一思路 —— 认得出动作就绝不丢位移。
#: ⚠️ 只兜底**运动**类核心词：纯景别（中景/近景…）绝不在此表命中，
#:    必须继续返回空串（不瞎猜运镜，见 :func:`movement_hint` 与 C2 守卫）。
_MOVEMENT_CORE = (
    ("推", "镜头同时推近，主体占比增大、背景略微收窄"),
    ("拉", "镜头同时拉远，主体占比减小、环境交代更完整"),
    ("摇", "镜头同时横向摇移，画面左右两侧内容发生可见位移"),
    ("移", "镜头同时横向平移，画面整体产生视差位移"),
    ("跟", "镜头同时跟随主体平移，主体在画面中位置基本不变、背景持续后退"),
    ("升降", "镜头同时垂直升降，地平线与取景高度发生可见变化"),
    ("环绕", "镜头同时绕主体环绕，主体的朝向与背景相对关系发生变化"),
    ("旋转", "镜头同时绕主体环绕，背景透视关系发生变化"),
    ("变焦", "镜头同时变焦收紧取景，景深变浅、背景更虚化"),
    ("手持", "镜头带轻微手持晃动，构图边缘有细微漂移"),
)


def movement_hint(camera: str) -> str:
    """运镜 → 尾帧镜头位移提示；认不出（如只给景别）返回空串（不瞎猜）

    两级匹配：① 具体运镜词表（长词优先，取值逐字不变）；② 核心动作兜底
    （见 :data:`_MOVEMENT_CORE`，词典追不上的写法也不至于丢掉位移）。
    纯景别（中景/近景/特写…）两级都不命中 → 仍返回空串。
    """
    t = str(camera or "").strip()
    if not t:
        return ""
    for zh, val in _MOVEMENT_BY_CAMERA:
        if zh in t:
            return val
    for zh, val in _MOVEMENT_CORE:
        if zh in t:
            return val
    return ""


def same_scene(prev: dict, cur: dict) -> bool:
    """相邻两镜是否同场景（链式串帧的判据）

    依次比较 location → scene_id → scene/scene_name；任一层两边都有值时以该层为准。
    全都缺失时返回 True（宁可串起来保持连贯，也不要每镜各画各的）。
    """
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        return False
    for k in ("location", "scene_id", "scene", "scene_name"):
        a = str(prev.get(k) or "").strip()
        b = str(cur.get(k) or "").strip()
        if a and b:
            return a == b
    return True


def build_end_frame_prompt(shot: dict, char_refs: Optional[List[dict]] = None,
                           scene_refs: Optional[List[dict]] = None,
                           chained: bool = False) -> str:
    """构造「尾帧」生成提示词（中文，供 Qwen Edit 以首帧为参考图做编辑）

    要点：
    - 明确「仅改变动作/时间点，外观与场景必须与参考图完全一致」，避免模型改脸改服装
    - 显式禁止画面内出现文字（与 H3 链路同一约束，防止台词被渲染成字幕）

    chained=True 表示本镜首帧沿用了上一镜的尾帧（跨镜链式），
    参考图已经不是本镜自己的分镜图，因此首句措辞要改成「承接上一镜」，
    否则模型会以为是同一镜的两个时间点而拒绝推进动作。
    """
    shot = shot or {}
    camera = (shot.get("camera") or "中景").strip()
    desc = (shot.get("description") or "").strip()
    loc = (shot.get("location") or "").strip()
    emotion = (shot.get("emotion") or "").strip()
    chars = [c for c in (shot.get("characters_in_shot") or []) if c]
    items = [i for i in (shot.get("items_in_shot") or []) if i]

    parts: List[str] = []
    # 风格落地：尾帧也要显式带上目标风格（图生图虽靠参考图延续画风，但显式注入
    # 能在模型弱化参考时兜底，避免「分镜图有风格、尾帧又跑偏」）
    style_norm = style_kit.normalize_style(shot.get("style") or "")
    if style_norm:
        parts.append(style_kit.style_emphasis(style_norm))
    if chained:
        parts.append(f"参考图是**上一镜结束时的画面**。本镜（「{camera}」）承接该画面继续推进："
                     f"保持参考图的角色外观、服装、发型、配色、场景环境、光照与整体画风完全不变，"
                     f"只把时间与动作推进到本镜结束的瞬间。")
    else:
        parts.append(f"这是同一镜头「{camera}」的尾帧画面：保持参考图的角色外观、服装、发型、"
                     f"配色、场景环境、光照与整体画风完全不变，只把画面推进到该镜头动作结束的瞬间。")
    if loc:
        parts.append(f"场景：{loc}。")
    if desc:
        parts.append(f"镜头内容：{desc}")
    parts.append(motion_hint(camera))
    # 运镜位移提示：尾帧必须体现「镜头也动了」，否则 H3 在首尾帧之间插值时
    # 无位移可补 → camera 里的运镜（跟拍/推入/升降…）在这一环被丢掉。
    # ⚠️ 优先读**权威运镜字段** camera_motion（A1 拆分后的取值源），与
    #    h3_prompt_kit.build_detailed_description 同口径：旧剧本该字段为空 →
    #    回退解析 camera 复合串，行为与改动前逐字一致（零回归）。
    _mv = movement_hint(str(shot.get("camera_motion") or "").strip() or camera)
    if _mv:
        parts.append(f"{_mv}。")
    if emotion:
        parts.append(f"人物情绪：{emotion}。")
    if chars:
        parts.append(f"出场人物：{'、'.join(chars)}（外貌与服装必须与参考图一致，不得替换人物）。")
    if items:
        parts.append(f"关键道具：{'、'.join(items)}（形制与材质与参考图一致）。")
    parts.append(f"取景要求：仍为{camera}，机位、焦段与构图框架与首帧保持连贯，"
                 f"不得切换视角或大幅改变景别。")
    parts.append("画面中严禁出现任何文字、字幕、台词文本、水印或标识。")
    return "".join(parts)


def end_frame_path(keyframes_dir: str, shot_id) -> str:
    """尾帧落盘路径：output/keyframes/<项目>/shot_<NN>_end.png（与分镜图命名对齐）

    镜号归一化收敛后用全项目唯一实现 ``shot_key.shot_seq``（P1-19 / A-10）：
    写侧（本函数）与读侧（app.py 的 kf_end_map 查找）必须共用同一函数，
    否则会出现「写 shot_102_end.png、读 shot_01_end.png」的静默错位。
    """
    seq = shot_key.shot_seq(shot_id, 0)
    return os.path.join(keyframes_dir, f"shot_{seq:02d}_end.png")


def start_frame_link_path(keyframes_dir: str, shot_id) -> str:
    """首帧在本目录的镜像路径（便于画布/导出统一按目录取图，不依赖分镜图目录）"""
    seq = shot_key.shot_seq(shot_id, 0)
    return os.path.join(keyframes_dir, f"shot_{seq:02d}_start.png")


def generate_end_frame(start_frame: str, prompt: str, out_path: str,
                       seed: Optional[int] = None, timeout: int = 900,
                       client=None) -> dict:
    """以首帧为参考图，生成该镜头尾帧（Qwen Edit 2511 图像编辑链路）

    S-04：client 参数支持注入全局 ComfyUIClient（避免每镜新建实例、连接不复用）。
    未传时退回新建（向后兼容）。
    """
    try:
        import comfyui_client
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"comfyui_client 不可用：{e}"}
    if client is None:
        client = comfyui_client.ComfyUIClient()
    prefix = os.path.splitext(os.path.basename(out_path))[0]
    try:
        result = client.generate_storyboard(
            prompt_zh=prompt, ref_images=[start_frame],
            filename_prefix=f"comic_drama_kf/{prefix}", seed=seed, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"尾帧生成失败：{e}"}
    files = (result or {}).get("files") or []
    if not files:
        return {"ok": False, "error": "ComfyUI 未返回尾帧图片"}
    try:
        import shutil
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        if os.path.abspath(files[0]) != os.path.abspath(out_path):
            shutil.copy2(files[0], out_path)
            # ★ 用户需求：尾帧源图（ComfyUI output/comic_drama_kf/<prefix>_00001_.png）
            # 是本链路**唯一还在用 copy2** 的图片落盘（其余链路已改 move），不清理就是
            # 每镜尾帧在 ComfyUI output 留一份、只增不减。copy2 已成功 → 删源无副作用。
            try:
                _src = os.path.normpath(os.path.abspath(files[0]))
                import config as _cfg
                _comfy = os.path.normpath(os.path.abspath(_cfg.COMFYUI_OUTPUT_DIR or ""))
                # 只删落在 ComfyUI output 目录内的源（越界一律不动）
                if _comfy and os.path.isfile(_src) and _src.startswith(_comfy + os.sep):
                    if os.stat(_src).st_nlink == 1:
                        os.remove(_src)
                        logger.warning("[尾帧清理] 已删除 ComfyUI 侧尾帧源图：%s", _src)
                    else:
                        logger.warning("[尾帧清理] 尾帧源为硬链接，跳过：%s", _src)
            except Exception as _re:  # noqa: BLE001  清理失败绝不影响尾帧交付
                logger.warning("尾帧源图清理失败（忽略，不影响交付）：%s: %s",
                               type(_re).__name__, _re)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"尾帧落盘失败：{e}"}
    return {"ok": True, "path": out_path, "raw": files[0],
            "prompt_id": (result or {}).get("prompt_id")}


def _own_start(sb_map: Dict[str, str], key: str, seq: int) -> str:
    """取本镜自己的分镜图作为「原生首帧」

    只按「镜头键」精确取图：数字键 / shot_NN 键。
    绝不按数组下标兜底——那会在 manifest 缺项时把别的镜头的分镜图当成首帧，
    生成出与剧本不符的尾帧（真实缺陷，已在新版修正）。
    """
    return (sb_map.get(key)
            or sb_map.get(f"shot_{seq:02d}")
            or sb_map.get(str(seq))
            or "")


def prev_episode_end_frame(keyframes_dir: str) -> str:
    """**上一集最后一镜的尾帧**路径（跨集串帧用，2026-10-10 用户要求）。

    用户口径：「第一集的分镜最后一帧要传到第二集的视频生成第一个分镜里面去」。
    即把既有的**集内**跨镜链式（上一镜尾帧 = 下一镜首帧）延伸到**集与集之间**，
    让第 N+1 集的开场画面从第 N 集的收尾画面长出来，而不是各画各的。

    目录约定（shared_project._ep_dir）：**第 1 集平铺**在 <项目>/，第 2 集起为 epNN/。
    因此：
      · 当前是平铺目录（即第 1 集）→ 没有上一集，返回 ""；
      · 当前是 ep(N) → 上一集目录 = ep(N-1)，但 N-1==1 时是**平铺的项目根**。

    末帧取「上一集里 shot_NN_end.png 序号最大的那个」（= 该集最后一个镜头）。
    找不到任何尾帧（该集还没生成过关键帧）→ 返回 ""，调用方自然回退到本镜分镜图。

    只读、无副作用；任何异常一律返回 ""（绝不因它让视频生成失败）。
    """
    try:
        import re as _re
        d = os.path.abspath(str(keyframes_dir or ""))
        if not d or not os.path.isdir(d):
            return ""
        base = os.path.basename(d)
        m = _re.match(r"^ep(\d+)$", base)
        cur = int(m.group(1)) if m else 1
        if cur <= 1:
            return ""                      # 第 1 集之上没有上一集
        parent = os.path.dirname(d)
        prev_no = cur - 1
        prev_dir = parent if prev_no <= 1 else os.path.join(parent, f"ep{prev_no:02d}")
        if not os.path.isdir(prev_dir):
            return ""
        best, best_no = "", 0
        for fn in os.listdir(prev_dir):
            mm = _re.match(r"^shot_(\d+)_end\.png$", fn)
            if not mm:
                continue
            n = int(mm.group(1))
            p = os.path.join(prev_dir, fn)
            try:
                if n > best_no and os.path.isfile(p) and os.path.getsize(p) > 0:
                    best, best_no = p, n
            except OSError:
                continue
        return best
    except Exception as e:  # noqa: BLE001 - 跨集串帧是增强，失败必须无感降级
        logger.debug("查找上一集末帧失败（按无跨集串帧处理）：%s", e)
        return ""


def plan_keyframes(shots: List[dict], sb_map: Dict[str, str],
                   keyframes_dir: str, only_missing: bool = True,
                   chain_mode: str = DEFAULT_CHAIN_MODE) -> List[dict]:
    """规划尾帧生成清单（不调用模型，供前端预检与断点续跑）

    链式（chain_mode != off）时，第 N 镜的首帧优先取第 N-1 镜的尾帧：
      - 已落盘的 `shot_NN_start.png` 镜像（上一轮生成时写下的）；
      - 否则直接取上一镜尾帧 `shot_(N-1)_end.png`；
      - 再否则，若上一镜本次会生成，则登记为「首帧待上一镜产出」。

    返回每项：{shot_id, seq, start_frame, own_start, chain_from, chain_src,
              chained, has_start, has_end, need_gen, reason}
    """
    cm = norm_chain_mode(chain_mode)
    plan: List[dict] = []
    prev_shot: Optional[dict] = None
    prev_seq = 0
    prev_sid = None
    prev_end_ready = False

    # ⭐ 跨集串帧（2026-10-10 用户要求）：本集**第一镜**的链式来源不是「上一镜」
    #   而是「上一集的最后一镜」。集内链式对第一镜天然没有 prev_shot（下面 i==0
    #   时 prev_shot is None），所以这里单独取一次上一集末帧作为它的"前序画面"。
    #   用户口径是「要传过去」，故不受 same_scene 限制（跨集场景通常是变的，
    #   若按同场景判据就永远不串 —— 这正是需要显式放开的地方）。
    _xp_end = prev_episode_end_frame(keyframes_dir) if cm != "off" else ""

    for i, shot in enumerate(shots or []):
        sid = shot.get("shot_id", i + 1)
        seq = shot_key.shot_seq(sid, 0) or (i + 1)
        key = str(sid).strip()
        own = _own_start(sb_map, key, seq)
        own_ok = bool(own) and os.path.isfile(own)

        # ---- 链式首帧：上一镜尾帧（集内）；第一镜则接**上一集末帧**（跨集）----
        chain_src, chain_from = "", None
        if cm != "off" and prev_shot is not None and prev_seq:
            if cm == "always" or same_scene(prev_shot, shot):
                chain_src = end_frame_path(keyframes_dir, prev_seq)
                chain_from = prev_sid
        elif cm != "off" and prev_shot is None and _xp_end:
            # 本集第一镜：以上一集的收尾画面作为本集开场（跨集串帧）
            chain_src = _xp_end
            chain_from = "prev_episode"

        start, chained = own, False
        if chain_src:
            mirror = start_frame_link_path(keyframes_dir, seq)
            if os.path.isfile(mirror):
                start, chained = mirror, True
            elif os.path.isfile(chain_src):
                start, chained = chain_src, True
            elif prev_end_ready:
                start, chained = chain_src, True      # 上一镜本次会产出，届时可用

        has_start = bool(start) and os.path.isfile(start)
        start_pending = bool(chained and not has_start and prev_end_ready)
        end_p = end_frame_path(keyframes_dir, seq)
        has_end = os.path.isfile(end_p) and os.path.getsize(end_p) > 0

        available = has_start or start_pending or own_ok
        if has_end and only_missing:
            need, reason = False, "尾帧已存在，跳过"
        elif not available:
            need = False
            reason = ("缺少首帧：本镜分镜图与上一镜尾帧均不可用"
                      "（请先在步骤5生成分镜图片）")
        else:
            need = True
            reason = "待生成尾帧" + ("（首帧接续上一镜尾帧）" if chained else "")

        plan.append({"shot_id": sid, "seq": seq, "start_frame": start,
                     "own_start": own, "chain_from": chain_from,
                     "chain_src": chain_src, "chained": bool(chained),
                     "start_pending": start_pending,
                     "end_path": end_p, "has_start": has_start,
                     "has_end": has_end, "need_gen": bool(need), "reason": reason})

        prev_shot, prev_seq, prev_sid = shot, seq, sid
        prev_end_ready = bool(has_end or need)
    return plan


def resolve_start_map(shots: List[dict], sb_map: Dict[str, str], keyframes_dir: str,
                      chain_mode: str = DEFAULT_CHAIN_MODE) -> Dict[str, str]:
    """每镜「实际首帧」映射（供视频生成取 H3 参考图槽位 1）

    键同时注册数字键与 shot_NN 键。链式优先；链式来源不存在时回退本镜分镜图。
    """
    out: Dict[str, str] = {}
    for p in plan_keyframes(shots, sb_map, keyframes_dir,
                            only_missing=True, chain_mode=chain_mode):
        cur = p.get("start_frame") or ""
        if not (cur and os.path.isfile(cur)):
            cur = p.get("own_start") or ""
        if cur and os.path.isfile(cur):
            out[str(p["shot_id"])] = cur
            out[f"shot_{p['seq']:02d}"] = cur
    return out


def generate_keyframes(shots: List[dict], sb_map: Dict[str, str], keyframes_dir: str,
                       seed: Optional[int] = None, timeout: int = 900,
                       only_missing: bool = True,
                       progress_cb: Optional[Callable[[int, int, dict], None]] = None,
                       chain_mode: str = DEFAULT_CHAIN_MODE,
                       verify_cb: Optional[Callable[[str, dict, dict], Tuple[bool, str]]] = None,
                       max_verify_retries: int = 0,
                       preflight_cb: Optional[Callable[[str, dict, dict], dict]] = None,
                       recall_cb: Optional[Callable[[str, dict, dict], str]] = None,
                       qc_stop_cb: Optional[Callable[[list], Tuple[bool, str]]] = None,
                       client=None,
                       project_name: str = None,
                       ) -> dict:
    """批量生成尾帧（串行；单镜失败不影响其它镜）

    project_name：项目名（A-16 用于尾帧 meta 落盘）；可为 None（旧调用方不传则 meta 记 None，
        不影响任何生成逻辑）。

    S-04：client 参数透传给 generate_end_frame（默认 None 时退回新建实例，向后兼容）。
    app.py 注入全局 comfyui_client 实例以复用连接与共享队列/熔断状态。

    chain_mode：跨镜链式（上一镜尾帧 = 下一镜首帧），见模块顶部说明。
    verify_cb(path, shot, item) -> (ok, reason[, unavailable[, critical_issues]])：
        可选的尾帧质检回调（由 app.py 注入 QC 实现）。2/3/4 元组均可（本函数向下取位）：
          · 2 元组 (ok, reason)；
          · 3 元组 (ok, reason, unavailable)：unavailable=True 表示「不可判定」（P1-18）；
          · 4 元组 (ok, reason, unavailable, critical_issues)（A-18）：第 4 位是本镜判为
            致命的缺陷清单，写入 r["qc"]["critical_issues"]，供 G1 止损（qc_stop_cb）比对
            「连续 N 次缺陷相同」——旧 3 元组下该字段恒空 → 止损永不判无望。
        返回 False 时会换 seed 重画，最多 max_verify_retries 次；
        仍不通过则本镜判失败（链式会让后续镜自动回退到自己的分镜图，不会连环污染）。
        ⚠️ G18（P1）统一口径：质检回调**抛异常**时按 **fail-closed**（阻断）处理——
        本镜尾帧不交付（r["ok"]=False，r["qc"]["qc_skipped_due_to_error"]=True），清掉
        暂存残片并 break。与图片（storyboard）/视频 QC 对"质检接口异常"的口径一致
        （二者同样是"保留暂存、不交付、不重试"）；尾帧在链式模式下是下一镜首帧，
        一张未被质检的图会顺链污染后面所有镜，故绝不 fail-open 静默放过。
    preflight_cb(prompt, shot, item) -> dict：**生成前**提示词预检回调（由 app.py 注入
        ``prompt_qc.preflight``，与 verify_cb 同一注入风格）。返回
        ``{"prompt": <自愈后的提示词>, "verdict": …, "repairs": [...]}``；本函数取其中的
        ``prompt`` 作为实际生成用提示词，并把精简结论记进结果。**不阻断**：尾帧是成批
        生成的，为一条提示词打断整批代价过大 —— 与资产/整集链路同一取舍。
    qc_stop_cb(attempts) -> (bool, detail)：**重试止损**回调（G1，由 app.py 注入
        ``qc_client.qc_retry_hopeless``）。连续两次缺陷特征完全相同即提前停止本镜
        尾帧重画（尾帧在链式模式下是下一镜首帧，最贵的一处，换 seed 只是换骰子）。
    progress_cb(done, total, item) —— 每个镜头完成后回调一次
    recall_cb(orig_prompt, shot, item) -> str：质检不达标**重试时**改写的提示词回调
        （由 app.py 注入 prompt_memory.learned_prompt，召回 kind="keyframe" 的历史教训）。
        仅 attempt>0 时调用；返回空串/None 时保持原 prompt，默认 None → 整段跳过、
        零行为变更。`orig_prompt` 是 preflight 自愈**之前**的确定性串（稳定 phash 键）。
    """
    cm = norm_chain_mode(chain_mode)
    plan = plan_keyframes(shots, sb_map, keyframes_dir,
                          only_missing=only_missing, chain_mode=cm)
    todo = [p for p in plan if p["need_gen"]]
    total = len(todo)
    results: List[dict] = []
    # 已存在的直接登记为已完成（断点续跑语义）
    for p in plan:
        if p["has_end"] and only_missing:
            results.append({"shot_id": p["shot_id"], "seq": p["seq"], "ok": True,
                            "path": p["end_path"], "skipped": True,
                            "chained": p.get("chained", False),
                            "reason": "尾帧已存在（断点续跑跳过）"})
    ok_count = len(results)

    by_shot = {str(s.get("shot_id", i + 1)): s for i, s in enumerate(shots or [])}
    plan_by_seq = {p["seq"]: p for p in plan}
    os.makedirs(keyframes_dir, exist_ok=True)

    # S7 修复：尾帧先写暂存区 keyframe_scratch/<basename>，QC 通过才 move 到正式 keyframes/，
    # 失败者不落地（避免坏图被下一轮"已存在"复用）。暂存目录与正式目录同级、不同名。
    scratch_root = os.path.join(os.path.dirname(os.path.abspath(keyframes_dir)),
                                "keyframe_scratch")
    os.makedirs(scratch_root, exist_ok=True)

    def _scratch_end(seq: int) -> str:
        return os.path.join(scratch_root, f"shot_{seq:02d}_end.png")

    def _mirror(src: str, dst: str):
        """把实际使用的首帧镜像到 shot_NN_start.png（画布/导出/续跑统一取图）"""
        try:
            if src and os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"首帧镜像落盘失败（忽略）：{e}")

    def _write_kf_meta(end_p: str, qc: dict, seed, project_name: str, sid, seq) -> None:
        """A-16（P2-5）：尾帧质检达标落盘后，在正式产物旁写 `<shot_NN_end>.meta.json`。

        与分镜/资产链路 `_write_artifact_meta`（app.py）同口径的**纯旁路**元数据：
        记录 seed / 尾帧质检结论 qc / kind="keyframe" / shot_id / 工作流内容指纹（SHA256），
        让「这张尾帧当初用哪版工作流 + 哪个 seed + 过没过质检」可追溯。
        ⚠️ 绝不阻断生产：任何异常静默降级（只留 debug 日志），meta 缺失不影响尾帧交付。
        本模块是叶子模块（不能 import app，避免循环依赖），故此处自持实现；工作流指纹
        取尾帧出图用的 Qwen Edit 工作流（storyboard_gen，见 generate_end_frame）。
        """
        try:
            import hashlib
            import json
            import datetime
            meta = {
                "artifact": os.path.basename(end_p),
                "kind": "keyframe",
                "project": project_name,
                "shot_id": sid,
                "seq": seq,
                "seed": seed,
                "qc": qc,
                "workflow": None,
                "workflow_sha256": None,
                "written_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                import config as _cfg
                tpl = _cfg.WORKFLOW_TEMPLATE.get("storyboard_gen")
                if tpl:
                    wf_path = _cfg.resolve_workflow_path(tpl)
                    if os.path.isfile(wf_path):
                        h = hashlib.sha256()
                        with open(wf_path, "rb") as _f:
                            for _chunk in iter(lambda: _f.read(65536), b""):
                                h.update(_chunk)
                        meta["workflow"] = tpl
                        meta["workflow_sha256"] = h.hexdigest()
            except Exception as e:  # noqa: BLE001  工作流指纹算不出不影响 meta 主体
                logger.warning("工作流指纹计算失败（不影响 meta 主体）：%s", e)
            out = os.path.splitext(end_p)[0] + ".meta.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001  旁路兜底：meta 写失败绝不阻断尾帧交付
            logger.debug("A-16 尾帧元数据旁路写失败（不影响交付）：%s: %s",
                        type(e).__name__, e)

    for n, item in enumerate(todo, start=1):
        sid, seq = item["shot_id"], item["seq"]
        shot = by_shot.get(str(sid)) or {}
        # 链式首帧可能在本轮才由上一镜产出 → 到生成时才解析实际路径
        start = item.get("start_frame") or ""
        chained = bool(item.get("chained"))
        if not (start and os.path.isfile(start)):
            own = item.get("own_start") or ""
            if own and os.path.isfile(own):
                start, chained = own, False
                item["chained"] = False
        if not (start and os.path.isfile(start)):
            r = {"ok": False, "shot_id": sid, "seq": seq,
                 "error": "缺少首帧：本镜分镜图与上一镜尾帧均不可用"}
            results.append(r)
            logger.warning(f"尾帧 {n}/{total} shot {sid} -> 缺少首帧，跳过")
            if progress_cb:
                try:
                    progress_cb(n, total, r)
                except Exception as e:  # noqa: BLE001
                    logger.debug("进度回调异常（忽略，不阻断尾帧生成）：%s", e)
            continue

        _mirror(start, start_frame_link_path(keyframes_dir, seq))
        prompt = build_end_frame_prompt(shot, chained=chained)
        # 原始提示词：必须在 preflight 自愈**之前**捕获（作为教训库稳定 phash 键）。
        # recall_cb 重写的是「下一次生成」用的 prompt，而不是覆盖 orig_prompt。
        orig_prompt = prompt

        # ---- 生成前预检（与资产 / 分镜 / 视频同一条链路）----
        # 尾帧是「以首帧为参考图的图生图」，提示词缺锚定语义就会换脸换服装；链式模式下
        # 坏尾帧还会成为下一镜的首帧并顺着链污染。这里能自愈的先自愈，不能自愈的只记录
        # （成批生成不阻断），真正的拦截交给生成后的尾帧质检 verify_cb。
        pf_info: Optional[dict] = None
        if preflight_cb is not None:
            try:
                pf = preflight_cb(prompt, shot, item) or {}
                if isinstance(pf, dict):
                    cand = pf.get("prompt")
                    if cand:
                        if cand != prompt:
                            logger.info(f"尾帧 shot {sid} 提示词预检自愈："
                                        f"{'；'.join((pf.get('repairs') or [])[:3]) or '已规范化'}")
                        prompt = cand
                    pf_info = {
                        "label": pf.get("label") or "",
                        "accept": bool(pf.get("accept", True)),
                        "repairs": list(pf.get("repairs") or []),
                        "verdict": pf.get("verdict") or {},
                        "rebuild_hint": pf.get("rebuild_hint") or "",
                    }
                    if not pf_info["accept"]:
                        logger.warning(f"尾帧 shot {sid} 提示词预检未通过"
                                       f"（{pf_info['label']}）：{pf.get('reason') or ''}")
            except Exception as e:  # noqa: BLE001
                # fail-open：预检自身出问题绝不能拖垮尾帧生成
                logger.warning(f"尾帧提示词预检回调异常（按原样生成）：{e}")

        attempts = max(0, int(max_verify_retries or 0)) if verify_cb else 0
        # S7 修复：尾帧先写暂存区，质检通过才 move 到正式 keyframes/，失败者不落地
        scratch_end = _scratch_end(seq)
        end_p = item["end_path"]
        r: dict = {}
        kf_attempts: List[dict] = []   # G1：本镜尾帧质检记录，供 _qc_retry_hopeless 止损判定
        for attempt in range(attempts + 1):
            _seed = seed if attempt == 0 else random.randint(1, 2 ** 31 - 1)
            # 质检不达标后的重试（attempt>0）：先召回历史教训改写提示词，再换 seed 重画。
            # recall_cb 返回空串/None 时保持原 prompt（行为不变）；默认 None → 整段跳过。
            if attempt > 0 and recall_cb is not None:
                _recalled = recall_cb(orig_prompt, shot, item)
                if _recalled:
                    prompt = _recalled
            # S7：尾帧先写暂存区 scratch_end，质检通过才 move 到正式 keyframes/
            r = generate_end_frame(start, prompt, scratch_end,
                                   seed=_seed, timeout=timeout, client=client)
            if not r.get("ok") or verify_cb is None:
                # verify_cb 未注入时无质检门（旧行为），保持"生成成功即落地"以兼容
                # 无质检部署；但仍落正式目录（scratch → end_p 一致路径时直接生成）。
                if verify_cb is None and r.get("ok") and os.path.abspath(scratch_end) != os.path.abspath(end_p):
                    try:
                        shutil.move(scratch_end, end_p)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("尾帧 scratch → 正式目录 promote 失败，本镜尾帧未交付：%s", e)
                break
            try:
                _vres = verify_cb(scratch_end, shot, item)
            except Exception as e:  # noqa: BLE001
                # G18（P1）：与图片/视频质检统一口径 —— 质检回调异常 **fail-closed**（阻断），
                # 不再 fail-open（按通过处理）。尾帧在链式模式下会成为下一镜首帧，一张"没被
                # 质检过"的坏图顺着链污染后面所有镜；且图片/视频链路对质检异常都是
                # 「保留暂存、不交付、不重试」，尾帧若仍按通过则是三套口径并存（审计 G18）。
                # 回调抛异常是质检侧自身故障（非"不达标"结论），重试只会再抛，直接止损并
                # 如实标记 qc_skipped_due_to_error=True，供 UI 高亮，绝不静默放过。
                logger.error(f"尾帧质检回调异常（G18 fail-closed 阻断，本镜尾帧不交付）：{e}")
                r["qc"] = {"ok": False, "reason": f"质检回调异常（fail-closed）：{e}",
                           "attempt": attempt + 1, "qc_skipped_due_to_error": True}
                r.update({"ok": False,
                          "error": f"尾帧质检回调异常，按 fail-closed 阻断本镜尾帧交付：{e}"})
                try:
                    if os.path.isfile(scratch_end):
                        os.remove(scratch_end)
                except Exception as e:  # noqa: BLE001
                    logger.debug("清理 scratch 尾帧失败（忽略）：%s", e)
                break
            # verify_cb 允许返回 2 元组 (ok, reason)、3 元组 (ok, reason, unavailable)、
            # 或 4 元组 (ok, reason, unavailable, critical_issues)。
            # unavailable=True 表示「质检不可判定」（接口 5xx / ffmpeg 缺失等**与内容无关**
            # 的失败）——与「不达标」分开：不重试（重试只会再失败），标记 qc_unavailable，
            # 本镜尾帧不交付（P1-18，口径与分镜/逐镜/资产的 `not verdict.ok → break` 一致）。
            # critical_issues（第 4 位，A-18）：本镜命中/被判为致命的具体缺陷清单，供 G1
            # 止损（qc_stop_cb → qc_client.qc_retry_hopeless）比对「连续 N 次缺陷相同」。
            #   旧实现只回传 3 位，keyframe 侧 r["qc"]["critical_issues"] 恒为空 → 止损恒
            #   判「无缺陷特征」而永不判无望（_qc_retry_hopeless 返回 (False,'')）——纯
            #   换 seed 瞎撞。这里补齐第 4 位并写入 r["qc"]，止损才真正生效。
            _v_critic: list = []
            if isinstance(_vres, (tuple, list)) and len(_vres) >= 4:
                v_ok, v_reason, v_unavailable = bool(_vres[0]), _vres[1], bool(_vres[2])
                _v_critic = list(_vres[3] or [])
            elif isinstance(_vres, (tuple, list)) and len(_vres) >= 3:
                v_ok, v_reason, v_unavailable = bool(_vres[0]), _vres[1], bool(_vres[2])
            elif isinstance(_vres, (tuple, list)) and len(_vres) == 2:
                v_ok, v_reason, v_unavailable = bool(_vres[0]), _vres[1], False
            else:
                v_ok, v_reason, v_unavailable = bool(_vres), "", False
            r["qc"] = {"ok": bool(v_ok), "reason": v_reason or "",
                       "attempt": attempt + 1,
                       "critical_issues": _v_critic}
            if v_unavailable:
                # P1-18：质检不可判定 → 不重试、标记 qc_unavailable、本镜尾帧不交付
                r["qc"]["qc_unavailable"] = True
                r.update({"ok": False, "qc_unavailable": True,
                          "error": f"尾帧质检不可判定（qc_unavailable，与内容无关，不重试）："
                                   f"{v_reason or ''}"})
                logger.warning(
                    f"尾帧 shot {sid} 质检不可判定（qc_unavailable，不重试）：{v_reason or ''}")
                try:
                    if os.path.isfile(scratch_end):
                        os.remove(scratch_end)
                except Exception as e:  # noqa: BLE001
                    logger.debug("清理 scratch 尾帧失败（忽略）：%s", e)
                break
            # G1 止损：记录本镜质检特征（critical_issues/issues），连续相同则提前停
            if not v_ok:
                _qc_rec = {"attempt": attempt + 1, "reason": v_reason or "",
                           "critical_issues": (r.get("qc") or {}).get("critical_issues") or [],
                           "issues": (r.get("qc") or {}).get("issues") or []}
                kf_attempts.append(_qc_rec)
            if v_ok:
                # S7：质检通过 → 把暂存产物移入正式 keyframes/，并清理暂存
                try:
                    shutil.move(scratch_end, end_p)
                except Exception as e:  # noqa: BLE001
                    r.update({"ok": False, "error": f"尾帧移入正式目录失败：{e}"})
                    break
                # A-16（P2-5）：尾帧达标落盘后写旁路 .meta.json（seed/qc/kind/shot_id/工作流指纹），
                # 纯旁路（内部已吞异常），绝不阻断交付。放在 move 成功之后、break 之前。
                _write_kf_meta(end_p, r.get("qc"), _seed, project_name, sid, seq)
                break
            # G1 止损铺开：尾帧连续 N 次缺陷一字不差 → 提前停止本镜尾帧重画（尾帧在
            # 链式模式下是下一镜首帧，最贵的一处），换 seed 只是换骰子
            if qc_stop_cb is not None and not v_ok:
                _kf_stop, _kf_detail = qc_stop_cb(kf_attempts)
                if _kf_stop:
                    logger.warning(
                        f"尾帧 shot {sid} 重试止损：连续 {len(kf_attempts)} 次缺陷相同"
                        f"（{_kf_detail}），提前停止；建议改首帧/剧本字段后单独重跑")
                    r["qc_retry_stopped"] = {"reason": "连续缺陷完全相同，已提前停止",
                                              "features": _kf_detail,
                                              "attempts": len(kf_attempts)}
                    break
            if attempt >= attempts:
                r.update({"ok": False, "error": f"尾帧未通过质检：{v_reason or '不符合要求'}"})
                # S7：失败者不落地，清掉暂存残片，避免坏图被下一轮当"已存在"复用
                try:
                    if os.path.isfile(scratch_end):
                        os.remove(scratch_end)
                except Exception as e:  # noqa: BLE001
                    logger.debug("清理 scratch 尾帧失败（忽略）：%s", e)
            else:
                logger.info(f"尾帧 shot {sid} 质检不通过（{v_reason}），换 seed 重画")

        r.update({"shot_id": sid, "seq": seq, "chained": chained,
                  "chain_from": item.get("chain_from"),
                  "start_frame": start,
                  "prompt_qc": pf_info})
        results.append(r)
        if r.get("ok"):
            ok_count += 1
            # 把本镜尾帧写成下一镜的首帧镜像，让链式关系在磁盘上可见、可续跑
            nxt = plan_by_seq.get(seq + 1)
            if nxt and nxt.get("chained") and nxt.get("chain_src"):
                if os.path.abspath(nxt["chain_src"]) == os.path.abspath(item["end_path"]):
                    _mirror(item["end_path"], start_frame_link_path(keyframes_dir, nxt["seq"]))
        logger.info(f"尾帧 {n}/{total} shot {sid} -> "
                    f"{'OK' if r.get('ok') else 'FAIL ' + str(r.get('error'))}")
        if progress_cb:
            try:
                progress_cb(n, total, r)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"关键帧进度回调异常（忽略）：{e}")

    return {"ok": ok_count > 0 or not todo, "total": len(plan), "generated": total,
            "succeeded": ok_count, "failed": max(0, len(results) - ok_count),
            "chain_mode": cm,
            "chained_count": sum(1 for p in plan if p.get("chained")),
            "results": results}


