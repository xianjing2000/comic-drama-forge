# -*- coding: utf-8 -*-
"""3D 人偶构图基准图质检（生成分镜图**之前**的门禁）。

## 为什么需要
基准图是分镜生成的 <image1> —— 构图/站位/姿势的**强先验**。它一旦错了（人偶数量
不对、站位反了、景别越档、动作没摆上），后面那张分镜图必然跟着错；而现有质检要等
分镜图生成完（单镜 20+ 分钟、含 3 次重试）才发现，代价极大。这里在**生成之前**先用
确定性判据把明显不合格的基准图拦下来。

## 判据（全部确定性、零模型调用）
  1. image_ok    图像可读，且人物像素占比在合理区间（防空白/糊成一片）
  2. char_count  渲染诊断 diag.chars == 该镜出场角色数（防漏人/多人）
  3. pose_applied 剧本动作命中预设时，diag.poseApplied > 0（防姿势白写）
  4. slot        单角色镜头：人物水平质心 与 blocking 声明的 left/center/right 同档
  5. framing     人物竖直投影占比 与景别档位不冲突（防近景拍成小不点）
  缺 diag（老图/落盘失败）时，2/3 记 skipped 而非 fail —— 判据降级、不误杀。

## 失败怎么办
调用方**不注入**这张基准图，回落纯文字站位锚点（旧行为）。既不污染生成，
也不会把生产卡住（对比：硬门禁会让 46 镜大面积停摆）。
开关：env MJSCXT_TE3D_QC=0 可整体关闭。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: 整体开关（出问题时可一键关掉，不影响主链路）
def enabled() -> bool:
    return str(os.environ.get("MJSCXT_TE3D_QC") or "1").strip().lower() \
        not in ("0", "false", "off", "no")


#: 彩色像素判定阈值：背景是灰色渐变（R≈G≈B），人偶是彩色 → 通道极差即可分割
_MASK_MIN_CHROMA = 25

#: 人物像素占比下限（低于视为空白/未渲染出人偶）
_MIN_CHAR_RATIO = 0.004

#: 站位档位：声明值 -> (质心 x 下限, 上限)
_SLOT_BAND = {
    "left": (0.0, 0.42), "左": (0.0, 0.42),
    "center": (0.34, 0.66), "centre": (0.34, 0.66), "中": (0.34, 0.66),
    "right": (0.58, 1.0), "右": (0.58, 1.0),
}

#: 景别 -> (人物竖直投影占比下限, 上限)；None = 该侧不设限。
#: ⚠️ 初值是保守区间（宁可不报，也不误杀）；待用真实 46 镜标定后收紧。
_FRAMING_BAND = {
    "特写": (0.42, None), "近景": (0.42, None), "中近景": (0.28, None),
    "中景": (0.16, 0.80), "中全景": (0.10, 0.65),
    "全景": (None, 0.50), "远景": (None, 0.35),
}


def _char_mask_stats(png_path: str) -> Optional[Dict[str, float]]:
    """统计人物掩码：占比 / 水平质心 / 竖直投影占比。

    纯 Python 缩略图扫描（256 宽）—— 判据对分辨率不敏感，且不引入 numpy 依赖。
    """
    try:
        from PIL import Image
        im = Image.open(png_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    W, H = im.size
    if W <= 0 or H <= 0:
        return None
    sw = 256
    sh = max(1, int(round(H * sw / float(W))))
    im = im.resize((sw, sh))
    px = im.load()
    n = 0
    sx = sy = 0
    miny, maxy = sh, -1
    for y in range(sh):
        for x in range(sw):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) < _MASK_MIN_CHROMA:
                continue          # 灰色背景
            n += 1
            sx += x
            sy += y
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
    if not n:
        return {"ratio": 0.0, "cx": 0.5, "cy": 0.5, "h_ratio": 0.0, "pixels": 0}
    return {
        "ratio": n / float(sw * sh),
        "cx": (sx / float(n)) / sw,
        "cy": (sy / float(n)) / sh,
        "h_ratio": (maxy - miny + 1) / float(sh),
        "pixels": n,
    }


def _on_screen_chars(shot: dict) -> List[str]:
    """该镜出场角色（与 3D 导演台同一口径 _parse_characters）。"""
    try:
        import te_3d_director as D  # noqa: PLC0415
        return list(D._parse_characters(shot) or [])
    except Exception:  # noqa: BLE001
        raw = (shot or {}).get("characters_in_shot")
        return [str(c).strip() for c in raw if str(c).strip()] if isinstance(raw, list) else []


def qc_blocking_image(shot: dict, png_path: str, diag: Optional[dict] = None,
                      cam_key: Optional[str] = None) -> Dict[str, object]:
    """质检一张 3D 构图基准图；返回 {ok, checks, reasons, stats}。"""
    checks: List[Dict[str, object]] = []

    def add(name: str, ok: Optional[bool], detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    stats = _char_mask_stats(png_path)
    if stats is None:
        add("image_ok", False, "图像无法读取：%s" % png_path)
        return {"ok": False, "checks": checks, "reasons": ["图像无法读取"], "stats": {}}
    add("image_ok", stats["ratio"] >= _MIN_CHAR_RATIO,
        "人物像素占比 %.4f（下限 %.4f）" % (stats["ratio"], _MIN_CHAR_RATIO))

    # ---- 人数（要 diag）----
    chars = _on_screen_chars(shot)
    if isinstance(diag, dict) and diag.get("chars") is not None:
        try:
            got = int(diag.get("chars"))
        except (TypeError, ValueError):
            got = -1
        add("char_count", got == len(chars),
            "渲染人偶 %d 个 vs 本镜出场 %d 个%s" % (got, len(chars),
                                                    ("（%s）" % "/".join(chars)) if chars else ""))
    else:
        add("char_count", None, "无渲染诊断，跳过（降级判据）")

    # ---- 姿势是否真的应用 ----
    try:
        import te_3d_poses as P  # noqa: PLC0415
        text = " ".join(str(shot.get(k) or "") for k in ("action", "description"))
        preset, _ov, why = P.resolve_pose(text)
        want_pose = bool(why)
    except Exception:  # noqa: BLE001
        want_pose, why = False, ""
    if not want_pose:
        add("pose_applied", None, "本镜未命中动作预设，跳过")
    elif isinstance(diag, dict):
        applied = int(diag.get("poseApplied") or 0)
        add("pose_applied", applied > 0,
            "命中动作「%s」→ 关节应用 %d 个" % (why, applied))
    else:
        add("pose_applied", None, "命中动作但无渲染诊断，跳过")

    # ---- 站位（只对单角色镜头有意义）----
    blk = (shot or {}).get("blocking")
    if len(chars) == 1 and isinstance(blk, list) and blk:
        decl = ""
        for it in blk:
            if isinstance(it, dict) and str(it.get("name") or "").strip() == chars[0]:
                decl = str(it.get("x") or it.get("side") or "").strip().lower()
                break
        band = _SLOT_BAND.get(decl)
        if band:
            cx = float(stats["cx"])
            add("slot", band[0] <= cx <= band[1],
                "声明站位 %s（质心应落在 %.2f~%.2f），实测质心 %.2f" % (decl, band[0], band[1], cx))
        else:
            add("slot", None, "本镜无显式站位声明，跳过")
    else:
        add("slot", None, "多角色或无站位声明，跳过（质心口径不适用）")

    # ---- 景别 ----
    if cam_key:
        band = _FRAMING_BAND.get(str(cam_key).strip())
        if band:
            hr = float(stats["h_ratio"])
            lo_ok = band[0] is None or hr >= band[0]
            hi_ok = band[1] is None or hr <= band[1]
            add("framing", bool(lo_ok and hi_ok),
                "景别 %s（竖直占比应在 %s~%s），实测 %.2f"
                % (cam_key, band[0] if band[0] is not None else "-",
                   band[1] if band[1] is not None else "-", hr))
        else:
            add("framing", None, "景别 %s 无档位表，跳过" % cam_key)
    else:
        add("framing", None, "未声明景别，跳过")

    reasons = [c["detail"] for c in checks if c["ok"] is False]
    return {"ok": not reasons, "checks": checks, "reasons": reasons, "stats": stats}
