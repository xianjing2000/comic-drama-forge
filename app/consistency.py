# -*- coding: utf-8 -*-
"""跨镜头一致性校验闭环（P1-1）

对标开源项目的「角色一致性锚定」能力，补齐本项目原先只有「单图质检」的缺口。

三层校验
--------
① **感知哈希层**（无依赖、快速）：Pillow 实现 aHash + dHash，
   用于同一角色多视图之间的快速结构比对，可在**没有多模态模型时**作为降级参考值。
   注意：这一层是「像素结构相似度」，不是语义判定，报告里会明确标注置信度。

② **语义判定层**（多模态视觉模型，主判据）：把角色参考图 + 目标图一起送模型，
   问「是否同一角色（发型/五官/服装/配色）」，返回 0-100 分与差异点清单。
   复用质检模块已配置的视觉模型，不新增配置项。

③ **设定比对层**：读 continuity bible 里该角色的「当前服装状态」，
   在同一轮视觉调用里附带问「服装是否符合 <状态>」，避免多花一次调用。

产物
----
output/continuity/<项目>/consistency.json   —— 完整报告（按角色/镜头组织）
report 结构：
{
  "project": "...", "generated_at": "...", "asset_check": {...}, "shot_check": {...},
  "summary": {"characters": 2, "shots_checked": 12, "passed": 10, "warn": 1, "failed": 1,
              "avg_score": 86.5, "model": "...", "semantic_available": true}
}

设计约束
--------
- 任何异常都不得打断生成流程（返回 ok=False 并记录）；
- 无多模态模型时自动降级为感知哈希，并在报告里标 `semantic_available: false`；
- 不修改任何既有产物文件，只新增报告。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

# ⭐ 2026-10-10 同 nle_export 的 bug：用 __file__ 上级自算数据目录，绕过数据根。
#   设置 MJSCXT_DATA_DIR 后，一致性数据写在数据根，这里却去代码目录读 → 读不到。
#   统一从 config 取（跟随 PROJECT_DATA_DIR）。旧的 _ROOT_DIR 已无其他用途，一并移除。
from config import CONTINUITY_DIR  # noqa: F401

# 判定阈值（可被环境变量覆盖）
PASS_SCORE = int(os.getenv("MJSCXT_CONSISTENCY_PASS", "70"))   # ≥ 视为一致
WARN_SCORE = int(os.getenv("MJSCXT_CONSISTENCY_WARN", "55"))   # ≥ 视为需关注，< 视为不一致
PHASH_ALARM = int(os.getenv("MJSCXT_PHASH_ALARM", "40"))       # 感知哈希相似度警戒线（降级模式用，仅报告展示）
# A-20（P2-9）：降级（phash）模式**专用**判定阈值。无多模态模型时只能靠像素结构哈希，
# 置信度低，判定口径应整体收紧：pass 线抬高（PHASH_PASS）、warn 下界取**非 0** 的合理值
# （PHASH_WARN）。旧实现把降级模式 warn 下界写成 0（`score>=0` 恒成立）且 pass 线用
# PHASH_ALARM(40)——一张噪声图（像素结构 score≈49）>= 40 就被判「pass」，降级模式形同虚设。
PHASH_PASS = int(os.getenv("MJSCXT_PHASH_PASS", "80"))         # 降级模式「一致」线（≥ 才 pass）
PHASH_WARN = int(os.getenv("MJSCXT_PHASH_WARN", "60"))         # 降级模式 warn 下界（非 0）

CHARACTER_VIEW_FILES = ("front.png", "base.png", "left.png", "right.png", "back.png")


# ===================== ① 感知哈希层（无外部依赖） =====================

def _open_gray(path: str, size):
    """读图并转灰度缩略图；失败返回 None"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.convert("L").resize(size, Image.LANCZOS)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片读取失败（跳过哈希）：{path} - {e}")
        return None


def _pixels(img) -> list:
    try:
        return list(img.getdata())
    except Exception:  # noqa: BLE001
        return []


def ahash(path: str, hash_size: int = 8) -> Optional[int]:
    """平均哈希：像素均值二值化 → 64 位整数"""
    img = _open_gray(path, (hash_size, hash_size))
    if img is None:
        return None
    px = _pixels(img)
    if not px:
        return None
    avg = sum(px) / len(px)
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= (1 << i)
    return bits


def dhash(path: str, hash_size: int = 8) -> Optional[int]:
    """差值哈希：水平相邻像素差二值化 → 64 位整数（对亮度变化更鲁棒）"""
    img = _open_gray(path, (hash_size + 1, hash_size))   # 宽需多 1 列用于相邻比较
    if img is None:
        return None
    px = _pixels(img)
    need = (hash_size + 1) * hash_size
    if len(px) < need:
        return None
    bits = 0
    bit = 0
    stride = hash_size + 1
    for row in range(hash_size):
        base = row * stride
        for col in range(hash_size):
            left = px[base + col]
            right = px[base + col + 1]
            if left > right:
                bits |= (1 << bit)
            bit += 1
    return bits


def _hamming(a: int, b: int, bits: int = 64) -> int:
    return bin(a ^ b).count("1")


def phash_similarity(path_a: str, path_b: str) -> dict:
    """感知哈希综合相似度（0-100）。aHash 与 dHash 各占一半权重。

    注意：这是**像素结构**相似度，非语义判定；仅在没有多模态模型时作为降级参考。
    """
    ha, hb = ahash(path_a), ahash(path_b)
    da, db = dhash(path_a), dhash(path_b)
    parts = []
    detail = {}
    if ha is not None and hb is not None:
        s = (64 - _hamming(ha, hb)) / 64 * 100
        parts.append(s)
        detail["ahash_similarity"] = round(s, 1)
    if da is not None and db is not None:
        s = (64 - _hamming(da, db)) / 64 * 100
        parts.append(s)
        detail["dhash_similarity"] = round(s, 1)
    if not parts:
        return {"ok": False, "error": "图片无法解析（缺 Pillow 或文件损坏）", **detail}
    score = sum(parts) / len(parts)
    return {"ok": True, "mode": "phash", "semantic_available": False,
            "score": round(score, 1), "confidence": "low",
            "note": "像素结构相似度，非语义判定；配置多模态质检模型后可获得语义结论",
            **detail}


# ===================== ② 语义判定层 =====================

_SEMANTIC_PROMPT = """你是漫剧角色一致性审校员。我给你两张图：
【参考图】是该角色在项目设定中的标准形象（资产图）。
【目标图】是正片中的某个镜头画面或另一视角的资产图。

请判断目标图中的角色与参考图**是否为同一角色**，重点对比：
- 发型与发色
- 五官特征与脸型
- 服装款式、配色与纹样
- 配饰（耳饰、腰带、武器等）

另外，请忽略以下差异（这些不算不一致）：
- 镜头机位/角度/景别不同
- 光照、天气、时间（白天/夜晚）、色调氛围不同
- 表情、姿态、动作不同
- 画面上的水印、角标、字幕、"AI生成"标识

{extra}

请只输出 JSON，不要任何解释文字：
{{"same_character": true/false, "score": 0-100 的整数（越高越一致）,
 "differences": ["具体差异点，最多 4 条，无差异则为空数组"],
 "reason": "一句话结论"}}
"""

_COSTUME_EXTRA = """补充比对项：该角色在本集的服装状态应为「{costume}」。
请额外判断目标图中的服装是否符合这个状态，并把它作为差异点之一（若不符）。
输出 JSON 时额外增加字段 "costume_match": true/false。"""


def check_pair_semantic(ref_path: str, target_path: str, cfg: dict = None,
                        costume_state: str = "", override: dict = None) -> dict:
    """用多模态模型判定「目标图与参考图是否同一角色」

    返回 {ok, score, same_character, differences, reason, costume_match, model, ...}
    """
    import qc_client

    extra = ""
    if costume_state:
        extra = _COSTUME_EXTRA.format(costume=costume_state)
    prompt = _SEMANTIC_PROMPT.format(extra=extra)

    resp = qc_client.run_custom_vision(prompt, [ref_path, target_path],
                                       cfg=cfg, override=override, max_tokens=700)
    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error"), "mode": "semantic"}

    data = qc_client.parse_json_loose(resp.get("content") or "")
    if not data:
        return {"ok": False, "mode": "semantic", "error": "模型返回非 JSON，无法解析",
                "raw": (resp.get("content") or "")[:300]}

    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    diffs = data.get("differences")
    if not isinstance(diffs, list):
        diffs = []
    return {
        "ok": True, "mode": "semantic", "semantic_available": True,
        "confidence": "high",
        "score": score,
        "same_character": bool(data.get("same_character")),
        "differences": [str(d) for d in diffs][:4],
        "reason": str(data.get("reason") or ""),
        "costume_match": data.get("costume_match"),
        "model": resp.get("model"),
        "latency_ms": resp.get("latency_ms"),
    }


def _verdict(score: int, semantic: bool) -> str:
    """分数 → 判定标签（A-20：降级模式带 `degraded` 前缀，且用收紧的非 0 阈值）

    - 语义模式（有多模态模型）：阈值 PASS_SCORE / WARN_SCORE，标签 pass/warn/fail（不变）；
    - 降级模式（phash，无模型）：阈值 PHASH_PASS / PHASH_WARN（**非 0** 合理值），
      标签为 ``degraded_pass`` / ``degraded_warn`` / ``degraded_fail`` —— 显式标
      ``degraded``，让调用方/前端一眼看出这是低置信度像素哈希判定，不是语义结论。
    旧实现降级模式 `warn` 下界=0（`score>=0` 恒成立）且 pass 线取 PHASH_ALARM(40)，
    噪声图 score≈49 就被判「pass」——降级形同虚设；A-20 收紧后不再误判 pass。
    """
    if semantic:
        if score >= PASS_SCORE:
            return "pass"
        if score >= WARN_SCORE:
            return "warn"
        return "fail"
    # 降级（phash）模式：非 0 阈值 + degraded 标记
    if score >= PHASH_PASS:
        return "degraded_pass"
    if score >= PHASH_WARN:
        return "degraded_warn"
    return "degraded_fail"


# ===================== ③ 设定比对层 =====================

def load_character_costumes(project: str) -> dict:
    """从 continuity bible 读取角色当前服装状态 → {角色名: 服装描述}"""
    path = os.path.join(CONTINUITY_DIR, project, "bible.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            bible = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"bible.json 读取失败（跳过服装比对）：{e}")
        return {}
    chars = bible.get("characters")
    out = {}
    if isinstance(chars, dict):
        for name, info in chars.items():
            if isinstance(info, dict):
                c = (info.get("current_costume") or info.get("costume")
                     or info.get("outfit") or "")
                if c:
                    out[name] = str(c)
    elif isinstance(chars, list):
        for info in chars:
            if isinstance(info, dict) and info.get("name"):
                c = (info.get("current_costume") or info.get("costume")
                     or info.get("outfit") or "")
                if c:
                    out[str(info["name"])] = str(c)
    return out


# ===================== 资产多视图校验 =====================

def check_asset_multiview(project: str, character_dir: str, character_name: str = "",
                          cfg: dict = None, costume_state: str = "",
                          max_views: int = 4) -> dict:
    """校验单个角色的多视图资产是否形象一致

    - 有多模态模型：以 base/front 为参考，逐张语义比对其它视角；
    - 无模型：退化为感知哈希比对（明确标注低置信度）。
    """
    files = [f for f in CHARACTER_VIEW_FILES
             if os.path.isfile(os.path.join(character_dir, f))]
    # 参考图优先 front，其次 base
    ref_name = "front.png" if "front.png" in files else (files[0] if files else "")
    if not ref_name:
        return {"ok": False, "error": "角色目录下没有可用视图"}
    ref_path = os.path.join(character_dir, ref_name)

    targets = [f for f in files if f != ref_name][:max_views]
    results = []
    for f in targets:
        p = os.path.join(character_dir, f)
        rec = check_pair_semantic(ref_path, p, cfg=cfg, costume_state=costume_state)
        if not rec.get("ok"):
            rec = phash_similarity(ref_path, p)
        rec.update({"view": f, "ref_view": ref_name})
        rec["verdict"] = _verdict(int(rec.get("score") or 0),
                                  bool(rec.get("semantic_available")))
        results.append(rec)

    scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    return {
        "ok": True, "character": character_name or os.path.basename(character_dir),
        "reference_view": ref_name,
        "views_checked": len(results),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "min_score": min(scores) if scores else 0,
        "results": results,
    }


# ===================== 跨镜头校验 =====================

def check_shots(project: str, character_refs: dict, shot_images: dict,
                cfg: dict = None, costumes: dict = None,
                max_shots: int = 60) -> dict:
    """跨镜头角色一致性校验

    character_refs: {角色名: 参考图绝对路径}
    shot_images:    {shot_key: {"image": 分镜图路径, "characters": [角色名...]}}
    """
    costumes = costumes if costumes is not None else load_character_costumes(project)
    results = []
    for shot_key, info in list(shot_images.items())[:max_shots]:
        if not isinstance(info, dict):
            continue
        img = info.get("image")
        if not img or not os.path.isfile(img):
            continue
        chars = [c for c in (info.get("characters") or []) if c in character_refs]
        if not chars:
            # 未标注角色时，用第一个角色参考兜底比对
            chars = list(character_refs.keys())[:1]
        for cname in chars:
            ref = character_refs.get(cname)
            if not ref or not os.path.isfile(ref):
                continue
            rec = check_pair_semantic(ref, img, cfg=cfg, costume_state=costumes.get(cname, ""))
            if not rec.get("ok"):
                rec = phash_similarity(ref, img)
            rec.update({"shot": shot_key, "character": cname,
                        "image": img, "ref_image": ref})
            rec["verdict"] = _verdict(int(rec.get("score") or 0),
                                      bool(rec.get("semantic_available")))
            results.append(rec)
    scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    return {
        "ok": True, "shots_checked": len({r["shot"] for r in results}),
        "pairs_checked": len(results),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "min_score": min(scores) if scores else 0,
        "passed": sum(1 for r in results if r["verdict"] in ("pass", "degraded_pass")),
        "warn": sum(1 for r in results if r["verdict"] in ("warn", "degraded_warn")),
        "failed": sum(1 for r in results if r["verdict"] in ("fail", "degraded_fail")),
        # A-20：单列降级（phash）判定计数，便于前端/体检区分「低置信度」结论
        "degraded": sum(1 for r in results if str(r["verdict"]).startswith("degraded")),
        "results": results,
    }


# ===================== 报告落盘 / 读取 =====================

def report_path(project: str) -> str:
    return os.path.join(CONTINUITY_DIR, project, "consistency.json")


def save_report(project: str, report: dict) -> str:
    """原子写一致性报告（A-3）：唯一临时名 + fsync + .bak 快照 + replace 重试。

    保持原契约：落盘失败只告警、不抛出（报告可由 `run()` 重新生成）。
    """
    path = report_path(project)
    try:
        atomic_write_json(path, report)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"一致性报告落盘失败：{e}")
    return path


def load_report(project: str) -> Optional[dict]:
    """严格读一致性报告（A-4）：缺失→None；损坏→从 .bak 恢复；无 .bak→降级 None。

    A-4 边界决策：报告是**只读派生数据**（随时可重新执行校验生成），且被
    app.py 的两个**只读**接口直接调用（`/api/consistency/report` 与镜头列表视图，
    app.py 不在本次改动范围内）。故损坏且无 .bak 时在这里显式记 error 并返回 None，
    让接口照旧给出「暂无一致性报告」，而不是 500 —— 响亮降级，不静默清空。
    """
    path = report_path(project)
    try:
        data = read_json_strict(path, None)
    except (ValueError, OSError) as e:
        logger.error("一致性报告 %s 损坏且无可用 .bak，本次按「暂无报告」处理：%s",
                     project, e)
        return None
    return data if isinstance(data, dict) else None


def run(project: str, character_refs: dict = None, shot_images: dict = None,
        cfg: dict = None, asset_dirs: dict = None,
        include_assets: bool = True, include_shots: bool = True) -> dict:
    """完整一致性校验（对外主入口）

    cfg 为质检配置（复用其中的视觉模型）；不传则自动读取全局 qc_config.json。
    返回报告 dict（同时落盘 consistency.json）。
    """
    import qc_client
    if cfg is None:
        try:
            from config import QC_CONFIG_PATH
            cfg = qc_client.load_config(QC_CONFIG_PATH)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"质检配置读取失败，一致性校验将降级为感知哈希：{e}")
            cfg = {}

    semantic_ready = False
    try:
        semantic_ready = bool(qc_client.qc_endpoint_ready(cfg))
    except Exception:  # noqa: BLE001
        semantic_ready = False

    costumes = load_character_costumes(project)
    report = {
        "project": project,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "semantic_available": semantic_ready,
        "mode": "semantic" if semantic_ready else "phash",
        "thresholds": {"pass": PASS_SCORE, "warn": WARN_SCORE, "phash_alarm": PHASH_ALARM},
        "costumes": costumes,
        "asset_check": None,
        "shot_check": None,
        "summary": {},
    }

    if include_assets and asset_dirs:
        assets = []
        for name, d in (asset_dirs or {}).items():
            if os.path.isdir(d):
                assets.append(check_asset_multiview(project, d, name, cfg=cfg,
                                                    costume_state=costumes.get(name, "")))
        report["asset_check"] = {"characters": assets, "count": len(assets)}

    if include_shots and character_refs and shot_images:
        report["shot_check"] = check_shots(project, character_refs, shot_images,
                                           cfg=cfg, costumes=costumes)

    # 汇总
    shot = report.get("shot_check") or {}
    asset = report.get("asset_check") or {}
    all_scores = []
    for r in (shot.get("results") or []):
        if isinstance(r.get("score"), (int, float)):
            all_scores.append(r["score"])
    for c in (asset.get("characters") or []):
        if isinstance(c.get("avg_score"), (int, float)):
            all_scores.append(c["avg_score"])
    report["summary"] = {
        "mode": report["mode"],
        "semantic_available": semantic_ready,
        "characters": len(asset.get("characters") or []),
        "shots_checked": shot.get("shots_checked", 0),
        "pairs_checked": shot.get("pairs_checked", 0),
        "passed": shot.get("passed", 0),
        "warn": shot.get("warn", 0),
        "failed": shot.get("failed", 0),
        "avg_score": round(sum(all_scores) / len(all_scores), 1) if all_scores else 0,
        "min_score": min(all_scores) if all_scores else 0,
        "model": (cfg or {}).get("model") or "",
    }
    report["report_path"] = save_report(project, report)
    return report
