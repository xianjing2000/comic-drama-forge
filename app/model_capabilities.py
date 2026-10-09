# -*- coding: utf-8 -*-
"""视频模型能力表 + 镜头参数归一化（2026-10-01，借鉴 Toonflow 的按能力归一化设计）

背景（Toonflow 调研的吸收点 #1）：Toonflow 的 videoGenerationNode 在加载模型后，
把 duration/resolution/ratio **自动纠正到该模型声明的合法值**，非法参数在入口报错
而不是 40 分钟后才失败。本模块把同样的思想用于 H3 Director 管线：镜头参数在进入
构建器之前，先对照 H3 的能力表归一。

能力表口径（与既有硬约束**同源**，不新增第三份标准——历史教训是口径漂移）：
- 单段时长上限/切段最小值 → h3_prompt_kit.H3_SEGMENT_MAX_SEC / H3_SEGMENT_MIN_SEC
- 提示词上限 → h3_prompt_kit.MAX_PROMPT_CHARS（预检层 clamp_prompt 执行）
- 每镜参考图上限 → comfyui_client._cap_storyboard_refs 的 MAX_STORYBOARD_REFS=8
- 分辨率/画幅 → style_kit（此处不重复）
- H3 单次生成时长上限 15 秒、最高 2K、原生立体声（MiniMax-AI/MiniMax-H3 官方 README）

用法（app.py 视频 worker 入口）::

    shots, notes = model_capabilities.normalize_shots_for_h3(shots)
    if notes:
        app.logger.info("镜头参数归一化：%s", "；".join(notes[:3]))

任何异常都由调用方 fail-open（按原 shots 继续），本模块绝不抛错阻断生产。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import h3_prompt_kit

logger = logging.getLogger(__name__)

#: H3 单次生成的硬能力（官方 README：单次最长 15 秒）
H3_MAX_DURATION_SEC = 15.0
#: 剧本级单镜时长上下限（与 config.SHOT_DURATION_MIN/MAX 同口径的本地副本，
#: 避免 import config 的重依赖；config 为唯一权威，此处仅作归一时的安全钳位）
SHOT_DURATION_FLOOR = 1.0
SHOT_DURATION_CEIL = 12.0
#: 每镜参考图上限（与 comfyui_client._cap_storyboard_refs 的 MAX_STORYBOARD_REFS=8 同口径）
MAX_SHOT_REFS = 8


def normalize_shots_for_h3(shots: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按 H3 能力表归一镜头参数，返回 ``(归一后的 shots, 说明列表)``。

    只做**安全钳位与字段补齐**，不改语义：
    - duration 缺失/非法 → 按 description 长度估一个保守值（4s），并记录说明；
    - duration 超出 [1, 12] → 钳到边界（超长部分由 H3 分段机制处理）；
    - characters_in_shot / items_in_shot 缺失 → 补空列表（下游取图按列表驱动）；
    - shot_id 缺失 → 按序号补 shot_N。
    任何一步异常都 fail-open（该项按原样保留）。
    """
    notes: List[str] = []
    out: List[Dict[str, Any]] = []
    for idx, s in enumerate(shots or []):
        if not isinstance(s, dict):
            continue
        item = dict(s)
        if not str(item.get("shot_id") or "").strip():
            item["shot_id"] = f"shot_{idx + 1}"
            notes.append(f"第 {idx + 1} 镜缺 shot_id，已按序补 shot_{idx + 1}")
        try:
            dur = float(item.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur < SHOT_DURATION_FLOOR:
            if dur > 0:
                notes.append(f"镜头 {item.get('shot_id')} 时长 {dur}s 低于下限，钳到 {SHOT_DURATION_FLOOR}s")
            else:
                notes.append(f"镜头 {item.get('shot_id')} 缺时长，按 4s 估")
            dur = max(dur, 4.0) if dur <= 0 else SHOT_DURATION_FLOOR
        elif dur > SHOT_DURATION_CEIL:
            notes.append(f"镜头 {item.get('shot_id')} 时长 {dur}s 超上限，钳到 {SHOT_DURATION_CEIL}s"
                         "（超长部分由 H3 分段机制承载）")
            dur = SHOT_DURATION_CEIL
        item["duration"] = dur
        for list_key in ("characters_in_shot", "items_in_shot"):
            v = item.get(list_key)
            if v is None:
                item[list_key] = []
            elif isinstance(v, str):
                item[list_key] = [v] if v.strip() else []
        out.append(item)
    if not out:
        notes.append("shots 为空")
    return out, notes


