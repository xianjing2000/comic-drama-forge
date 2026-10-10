# -*- coding: utf-8 -*-
"""场景空间关联分析（2026-10-10，用户需求①②③）。

## 为什么需要

同一个空间的不同位置此前各自独立出图 —— 实测「2704房间」的门框是深色的，
而「2704门口」是斑驳木框、地面是水磨石，**同一个地方被画成两处**。

根因有两条，本模块处理第①条：
  ① 场景提示词里**没有固定结构（门框/地面/墙面）的约束**，模型自由发挥；
  ② 场景之间**没有任何关联信息**，生成时互不参考。

## 做法

  · relate_scenes()         让模型分析全部场景的空间关联，产出
                            space_group / refs / anchor / fixed_structure；
  · apply_scene_relations() 写回 bible.scenes[]（幂等：已有值不覆盖）；
  · 出图时 asset_worker 依据 refs 传关联参考图
    （ComfyUIClient.generate_scene_base(ref_images=...)），
    fixed_structure 由调用方拼进提示词。

## 设计取舍

  · **宁可漏判，不可错判**：模型判断不了空间关系时就让它独立成组 —— 错把
    两个无关场景挂在一起，比不挂更糟（参考图引力强，会污染另一个场景）。
  · **refs 只指向可用于参考的场景**：参考图必须已存在，否则生成时会回落。
  · fixed_structure 要求**具体到材质与颜色**，禁止「现代风格」这类抽象词 ——
    抽象词对出图模型没有约束力，正是门框对不上的直接原因。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: 关联分析的系统提示（防 OOC / 防编造）
_SYSTEM = (
    "你是影视美术指导，擅长判断场景之间的空间关系与固定结构。"
    "你只依据给出的场景描述做判断，绝不编造描述里没有的信息。"
    "严格输出 JSON，不要 markdown 代码块，不要解释文字。"
)


def build_relate_prompt(scenes: list) -> str:
    """构造关联分析提示词。scenes 为 bible.scenes[]（只读 name/appearance/location）。"""
    lines = []
    for s in (scenes or []):
        if not isinstance(s, dict):
            continue
        nm = str(s.get("name") or "").strip()
        if not nm:
            continue
        desc = str(s.get("appearance") or "").strip()
        loc = str(s.get("location") or "").strip()
        extra = str(s.get("fixed_structure") or "").strip()
        parts = [f"· {nm}"]
        if loc:
            parts.append(f"地点类型={loc}")
        if desc:
            parts.append(f"环境={desc}")
        if extra:
            parts.append(f"已知固定结构={extra}")
        lines.append("：".join([parts[0], "；".join(parts[1:])]) if len(parts) > 1 else parts[0])
    body = "\n".join(lines) or "（无场景）"
    return (
        "下面是同一部剧的全部场景。请分析它们之间的**空间关联** ——\n"
        "同一个物理空间的不同位置（例如同一房间的内与外、同一楼层的走廊与房间）、\n"
        "以及由同一条动线连接的连续空间，都算有关联。\n\n"
        f"【场景清单】\n{body}\n\n"
        "【输出要求】严格只输出一个 JSON 对象，结构如下：\n"
        '{"scenes": [{"name": "场景名", "space_group": "空间组标识", '
        '"refs": ["应参考的场景名"], "anchor": true, "fixed_structure": "固定结构"}]}\n\n'
        "【字段规则】\n"
        "· space_group：同一空间/同一动线用**同一个**标识（如 \"27F-2704\"）；"
        "确实独立的空间就用它自己的场景名。\n"
        "· refs：出图时应参考的**同组**场景名，最多 2 个，按参考优先级排序。"
        "只列同组内确实能提供结构参考的；没有就给空数组 []。\n"
        "· anchor：true 表示它是该组的**结构基准**（通常是最外层的通道/入口），"
        "组内其他场景参考它。每组至多一个 anchor。\n"
        "· fixed_structure：该场景**跨镜头不变**的固定结构，40 字以内，"
        "**必须具体到材质与颜色**（如「斑驳木质门框、水磨石地面、米黄色涂料墙面」），"
        "禁止「现代风格」「老旧」这类抽象词 —— 抽象词对出图模型没有约束力。\n\n"
        "【重要】只依据上面的场景名与环境描述判断；描述里没有的结构不要编造；"
        "拿不准是否关联时，让它独立成组（宁缺毋滥）。"
    )


def relate_scenes(client, scenes: list, temperature: float = 0.3) -> dict:
    """分析场景空间关联，返回 {场景名: {space_group, refs, anchor, fixed_structure}}。

    client：LLMClient（需有 chat_json）。scenes 为空 / 调用失败 / 解析失败
    → 返回 {}（fail-open，调用方按「无关联」处理，零回归）。
    """
    items = [s for s in (scenes or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
    if not items or client is None:
        return {}
    known = {str(s.get("name") or "").strip() for s in items}
    try:
        raw = client.chat_json(build_relate_prompt(items), system=_SYSTEM,
                               temperature=temperature)
    except Exception as e:  # noqa: BLE001  关联分析失败绝不阻断生产
        logger.warning("[场景关联] 分析失败（按无关联处理）：%s: %s", type(e).__name__, e)
        return {}
    rows = []
    if isinstance(raw, dict):
        rows = raw.get("scenes") or raw.get("data") or []
    elif isinstance(raw, list):
        rows = raw
    if not isinstance(rows, list):
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        nm = str(r.get("name") or "").strip()
        if not nm or nm not in known:
            continue                      # 模型编造的场景名一律丢弃
        refs = [str(x).strip() for x in (r.get("refs") or []) if str(x).strip()]
        refs = [x for x in refs if x in known and x != nm][:2]
        out[nm] = {
            "space_group": str(r.get("space_group") or "").strip(),
            "refs": refs,
            "anchor": bool(r.get("anchor")),
            "fixed_structure": str(r.get("fixed_structure") or "").strip()[:80],
        }
    if out:
        logger.info("[场景关联] 已解析 %d 个场景的关联（组数 %d）",
                    len(out), len({v["space_group"] for v in out.values() if v["space_group"]}))
    return out


def apply_scene_relations(bible: dict, relations: dict, overwrite: bool = False) -> dict:
    """把关联写回 bible.scenes[]，返回统计 {"updated": n, "skipped": n}。

    幂等：默认**不覆盖已有值**（overwrite=False），重复调用不会漂移；
    已被人工修正过的值（用户手动改过 refs）也不会被模型重跑冲掉。
    """
    stat = {"updated": 0, "skipped": 0}
    if not isinstance(bible, dict) or not relations:
        return stat
    for s in (bible.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        nm = str(s.get("name") or "").strip()
        rel = relations.get(nm)
        if not rel:
            continue
        touched = False
        for k in ("space_group", "refs", "is_anchor", "fixed_structure"):
            src = "anchor" if k == "is_anchor" else k
            val = rel.get(src)
            if k == "refs":
                val = list(val or [])
            if not val and val != []:
                continue
            if k == "refs" and not val:
                continue                  # 空 refs 不写，避免给每个场景塞空数组
            cur = s.get(k)
            if cur and not overwrite:
                stat["skipped"] += 1
                continue
            s[k] = val
            touched = True
        if touched:
            stat["updated"] += 1
    return stat


def scene_ref_images(scene: dict, scenes_dir: str, project_name: str,
                     os_module=None) -> list:
    """取该场景的**关联参考图**（已存在的同组场景图）。

    scene：bible.scenes[] 中的一条（读 refs）。
    scenes_dir：场景资产根目录；返回存在的 base.png 路径列表（最多 2 张）。
    任何缺失 / 异常 → 空列表（调用方回落无参考生成，零回归）。
    """
    import os as _os
    _os = os_module or _os
    refs = [str(x).strip() for x in (scene.get("refs") or []) if str(x).strip()]
    if not refs:
        return []
    out = []
    for nm in refs[:2]:
        d = _os.path.join(scenes_dir, project_name, nm)
        for stem in ("base", "front"):
            p = _os.path.join(d, stem + ".png")
            if _os.path.isfile(p) and _os.path.getsize(p) > 0:
                out.append(p)
                break
    return out
