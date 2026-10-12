"""H3 Director 「公共参考图」跨段交集规划（2026-09-30）

作用
----
整集（或一次提交的多段）里，把**每一段都在用、且用的是同一张图**的资产挑出来，
交给 H3 Director 的公共参数（``timeline.global.refs`` + ``commonEnabled=true``）。
插件在 ``commonEnabled`` 时按**槽位 index** 把公共图 merge 进每一段
（``director/plan.py:merge_indexed_refs``，同 index 段级优先），公共图占 index
``0..K-1``、段级私有图从 index ``K`` 起继续 —— 这正是插件前端
``batch.r2v.slotContinueHint``（「公共参数已占用图片1–{common}；本组从图片{from}
继续编号」）描述的官方用法。这样同一个资产在全集的 ``<Picture N>`` 编号**恒定**，
模型看到的「Picture 2 = 林昭」从头到尾一致，而不是随每镜声明顺序漂移。

为什么判据必须是**交集**（而不是「出场多」）
----------------------------------------
公共图会被 merge 进**每一段**，"放进公共池" 等于 "每一镜都带上它"。一个只在
5/30 镜出场的配角一旦进池，其余 25 镜的 conditioning 里都会多出它的三视图锚点 ——
模型会把锚点里的人画进画面（与本项目已实测的「模板示例图污染角色外观」同类缺陷）。
故：**只有全段都在用的资产才配进公共池**；「多数在为公共」是错的。

为什么身份键要带**图片路径**（而不只是资产名）
------------------------------------------
同一资产的参考图会随镜头变化：角色按景别取半身/全身档（``_pick_char_view``，
2026-09-25「景别对档」），场景按机位取 front/left45/right45/top 档
（``_pick_scene_view``，2026-09-29「按机位出图」）。若只按资产名判交集，公共池会把
**某一镜的档位焊死**给全段 —— 等于把这两轮专门做的「对档」废掉（近景镜头拿到全身
立绘、俯拍镜头拿到正面图）。带路径 = 「同一资产的同一张图」才是真正的公共项。

这也是本模块存在的意义：把「谁该进公共池」这个判据收在一处，可单测、可解释，
而不是散在 worker 的循环里靠 if 拼。

设计约束
--------
* **纯函数、零副作用**：不读盘、不调模型、不碰 app 状态；输入是「每段一个有序资产
  描述列表」，输出是「全段共有的前 N 项」。这样能在离线探针里直接断言。
* **顺序确定**：按第 1 段里的出现顺序 —— 剧本第 1 镜的声明顺序天然把主角/主场景排在
  前面，比「按名字排序」更符合「谁是主要资产」的直觉，且可复现。
* **留位**：公共块 + 1 张分镜图 ≤ ``limit``，且必须给段级私有项留至少 1 格
  （``max_common <= limit - 2``）—— 否则某镜自带的物品/场景锚点会被公共块挤掉。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

# 单一来源：官方 Reference to Video 的参考图槽位上限（``ref_image_0..8``）在
# h3_director_builder.MAX_REFERENCE_IMAGES（=9）。原注释「独立声明以免模块间循环依赖」
# 经核实不成立（h3_director_builder 只依赖 stdlib + h3_segment_loras，均不 import 本模块），
# 故直接 import，彻底消除两处各定义一份的漂移风险（t70 收口）。
from h3_director_builder import MAX_REFERENCE_IMAGES

#: 兼容别名：与 h3_director_builder.MAX_REFERENCE_IMAGES 同义（=9），供 plan_common_refs 默认 limit 用。
LIMIT_REFERENCE_IMAGES = MAX_REFERENCE_IMAGES

#: 资产种类标记（决定公共项在提示词里的「用途句」与是否进 ``<Subject N>``）。
KIND_CHARACTER = "character"
KIND_ITEM = "item"
KIND_SCENE = "scene"
VALID_KINDS = (KIND_CHARACTER, KIND_ITEM, KIND_SCENE)


def _path_key(path: Any) -> str:
    """图片路径 → 身份键片段（大小写/分隔符/相对写法归一）。

    刻意**不**要求文件存在：本模块是纯函数，路径有效性由上游
    （``_first_existing`` 等）保证；这里只负责「同一个文件的不同写法」能对上。
    """
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.normpath(raw))
    except (OSError, ValueError):  # pragma: no cover - 极端非法路径
        return raw


def asset_key(asset: Dict[str, Any]) -> Optional[tuple]:
    """资产描述 → 身份键 ``(kind, name, 图片路径)``；缺任一要素返回 ``None``（不入池）。

    ``None`` 的语义是「这条资产不能参与公共池判定」：
    * 没有路径（资产缺图）→ 放进去只会占格不产效果；
    * 没有名字（匿名资产）→ 提示词里写不出「谁的图」，``<Subject N>`` 也归不了属。
    """
    if not isinstance(asset, dict):
        return None
    kind = str(asset.get("kind") or "").strip()
    name = str(asset.get("name") or "").strip()
    pkey = _path_key(asset.get("path"))
    # 2026-10-02：角色按「角色级」判交集（common_key），不因服装变体（衣柜 outfits/
    # <key>/base.png）把同一角色路径劈成多张而漏出公共池；场景/物品不设 common_key，
    # 仍按「同一张图」判交集（档位不同不应焊死）。common_key 缺失回落路径键。
    key_id = str(asset.get("common_key") or "").strip()
    ident = key_id if key_id else pkey
    if kind not in VALID_KINDS or not name or not (key_id or pkey):
        return None
    return (kind, name, ident)


def _segment_keys(assets: Iterable[Dict[str, Any]]) -> List[tuple]:
    """一段的资产键列表（去重保序）。"""
    out: List[tuple] = []
    seen = set()
    for a in assets or []:
        k = asset_key(a)
        if k is None or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def plan_common_refs(seg_assets: Sequence[Iterable[Dict[str, Any]]], *,
                     max_common: int = 6,
                     limit: int = LIMIT_REFERENCE_IMAGES) -> List[Dict[str, Any]]:
    """挑出**每一段都在用**的资产 → 公共参考图列表（有序、已设上限）。

    seg_assets: 每段一个**有序**资产描述列表（``segment[0]`` 是第 1 段）。
                资产描述：``{"kind": character|item|scene, "name": str,
                            "path": 本地路径或 URL, "appearance": str(可选)}``

    返回：公共资产描述列表（**保留原始 dict**，只按交集与顺序筛），可以直接当
    ``global.refs`` 的载荷 —— 调用方负责把 ``path`` 换成上传后的相对名。

    ⚠️ 少于 2 段 → 返回 ``[]``：单段（单镜重跑 / 单镜内多子段）没有「跨段公共」可言，
    强行开公共池只会把段级图挪到全局而**不产生任何收益**，还会多一层
    ``commonEnabled`` 耦合。返回空 = 完全走原路径（零行为变更）。
    """
    segs = [list(s or []) for s in (seg_assets or [])]
    if len(segs) < 2:
        return []

    per_seg = [_segment_keys(s) for s in segs]
    if any(not ks for ks in per_seg):
        # 有段一张图都没有 → 交集必空（也说明上游参考图不全，应由上游告警处理）
        return []

    common_keys = set(per_seg[0])
    for ks in per_seg[1:]:
        common_keys &= set(ks)
    if not common_keys:
        return []

    # 上限：公共块 + 1 张分镜图 必须仍在 limit 内，且给段级私有项留 ≥1 格
    cap = max(0, min(int(max_common or 0), int(limit) - 2))
    if cap <= 0:
        return []

    # 顺序 = 第 1 段的出现顺序（剧本第 1 镜的声明顺序把主要资产排在前面）
    picked: List[Dict[str, Any]] = []
    seen = set()
    for a in segs[0]:
        k = asset_key(a)
        if k is None or k not in common_keys or k in seen:
            continue
        seen.add(k)
        picked.append(a)
        if len(picked) >= cap:
            break
    return picked


def common_names(common: Sequence[Dict[str, Any]]) -> List[str]:
    """公共项名字列表（日志/报表用）。"""
    return [str((a or {}).get("name") or "").strip() for a in (common or [])]


def describe(common: Sequence[Dict[str, Any]]) -> str:
    """一行摘要，便于日志定位「公共池里到底是哪些资产」。"""
    parts = [f"{(a or {}).get('kind')}:{(a or {}).get('name')}"
             for a in (common or [])]
    return "、".join(parts) if parts else "（空）"
