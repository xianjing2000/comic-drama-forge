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

import copy
import logging
import os
from typing import Dict, List, Optional, Tuple

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
    minx, maxx = sw, -1
    miny, maxy = sh, -1
    edge = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    for y in range(sh):
        for x in range(sw):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) < _MASK_MIN_CHROMA:
                continue          # 灰色背景
            n += 1
            sx += x
            sy += y
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
            if x <= 1:
                edge["left"] += 1
            if x >= sw - 2:
                edge["right"] += 1
            if y <= 1:
                edge["top"] += 1
            if y >= sh - 2:
                edge["bottom"] += 1
    if not n:
        return {"ratio": 0.0, "cx": 0.5, "cy": 0.5, "h_ratio": 0.0, "pixels": 0,
                "edge": {}, "clipped": False}
    # 「被裁」判定：贴边像素占人偶总像素的一定比例（抗单像素噪点）。
    # 2026-10-13 实测触发场景：体型大的角色（赵天霸）在近景里头顶直接出画。
    _clipped_sides = [k for k, v in edge.items() if v >= max(12, n * 0.01)]
    return {
        "ratio": n / float(sw * sh),
        "cx": (sx / float(n)) / sw,
        "cy": (sy / float(n)) / sh,
        "h_ratio": (maxy - miny + 1) / float(sh),
        "pixels": n,
        "edge": edge,
        "clipped": bool(_clipped_sides),
        "clipped_sides": _clipped_sides,
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
        # 群演角色会被展开成多个人偶（scene.crowdMap 登记），期望人数要按展开后算，
        # 否则「1 个围观弟子 → 5 个人偶」会被误判成人数不符。
        _cr = int(diag.get("crowdRoles") or 0)
        _cm = int(diag.get("crowdMembers") or 0)
        exp = (len(chars) - _cr + _cm) if (_cr and _cm) else len(chars)
        add("char_count", got == exp,
            "渲染人偶 %d 个 vs 期望 %d 个（出场 %d 人%s）"
            % (got, exp, len(chars),
               "，群演展开 %d 角色→%d 人" % (_cr, _cm) if (_cr and _cm) else ""))
    else:
        add("char_count", None, "无渲染诊断，跳过（降级判据）")

    # ---- 是否被画面裁掉（2026-10-13 实测触发：体型大的角色近景里头顶出画）----
    if stats.get("clipped"):
        add("clipped", False,
            "人偶贴到画面边缘（%s）—— 会被裁掉" % "/".join(stats.get("clipped_sides") or []))
    else:
        add("clipped", True, "人偶完整在画内（未触边）")

    # ---- 道具数量（剧本声明了几个道具，图里就该有几个）----
    _items = [str(x).strip() for x in ((shot or {}).get("items_in_shot") or []) if str(x).strip()]
    if not _items:
        add("prop_count", None, "本镜无道具声明，跳过")
    elif isinstance(diag, dict) and diag.get("props") is not None:
        add("prop_count", int(diag.get("props") or 0) == len(_items),
            "渲染道具 %d 个 vs 剧本声明 %d 个（%s）"
            % (int(diag.get("props") or 0), len(_items), "/".join(_items)))
    else:
        add("prop_count", None, "无渲染诊断，跳过（降级判据）")

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


# ===================== 自愈：按质检发现的问题修正渲染计划，重渲 =====================
# 用户口径（2026-10-13）：「不通过就不注入，不应该是不通过就根据质检发现的问题
# 重新生成吗」。对 —— 基准图是我们**确定性渲染**出来的，问题可定位、参数可反推，
# 所以不通过时应当修正计划再渲一次，而不是直接放弃。
# 每类问题对应一个修正器；修正后由调用方重渲再质检（最多 max_rounds 轮）。

#: 质心偏差 → 站位位移的增益（经验值；1.0 = 质心差多少就挪多少米，先保守取 4.0）
_SLOT_GAIN = 4.0
#: 单次站位修正的上限（米），避免把角色甩出画面
_SLOT_MAX_DX = 2.0


def _refit_clipped(plan: dict, stats: dict) -> str:
    """人偶被画面裁掉 → 把机位拉远（保高度），让它整个进画。"""
    cam = plan.get("camera") or {}
    pos = list(cam.get("position") or [])
    tgt = list(cam.get("target") or [])
    if len(pos) < 3 or len(tgt) < 3:
        return ""
    scale = 1.28
    new_pos = [tgt[i] + (pos[i] - tgt[i]) * scale for i in range(3)]
    new_pos[1] = pos[1]
    cam["position"] = [round(float(v), 3) for v in new_pos]
    plan["camera"] = cam
    return "机位拉远 x%.2f（人偶贴边：%s）" % (scale, "/".join(stats.get("clipped_sides") or []))


def _refit_camera(plan: dict, cam_key: str, h_ratio: float) -> str:
    """景别越档 → 沿视线缩放机位距离（小孔成像：投影高度与距离成反比）。"""
    band = _FRAMING_BAND.get(str(cam_key or "").strip())
    if not band or h_ratio <= 0:
        return ""
    lo, hi = band
    if lo is not None and h_ratio < lo:
        target = lo * 1.12
    elif hi is not None and h_ratio > hi:
        target = hi * 0.88
    else:
        return ""
    # ⚠️ 方向别搞反（2026-10-13 实测纠错）：投影高度 ∝ 1/距离 —— 想让占比**变大**
    #    就必须把机位**拉近**，所以倍率 = 实测占比 / 目标占比（小于 1 = 拉近）。
    #    单轮限幅 [0.4, 2.5]，靠多轮逐步逼近，避免一次甩到人脸前或飞到天边。
    scale = max(0.4, min(2.5, h_ratio / max(target, 1e-3)))
    cam = plan.get("camera") or {}
    pos = list(cam.get("position") or [])
    tgt = list(cam.get("target") or [])
    if len(pos) < 3 or len(tgt) < 3:
        return ""
    new_pos = [tgt[i] + (pos[i] - tgt[i]) * scale for i in range(3)]
    new_pos[1] = pos[1]                      # 只拉近/推远，保持机位高度（免得钻地/飞天）
    cam["position"] = [round(float(v), 3) for v in new_pos]
    plan["camera"] = cam
    return "机位缩放 x%.2f（竖直占比 %.2f → 目标 %.2f）" % (scale, h_ratio, target)


def _refit_slot(plan: dict, shot: dict, cx: float) -> str:
    """站位越档 → 按声明档位中心平移该角色（仅单角色镜头，质心口径才成立）。"""
    chars = _on_screen_chars(shot)
    blk = (shot or {}).get("blocking")
    if len(chars) != 1 or not isinstance(blk, list):
        return ""
    decl = ""
    for it in blk:
        if isinstance(it, dict) and str(it.get("name") or "").strip() == chars[0]:
            decl = str(it.get("x") or it.get("side") or "").strip().lower()
            break
    band = _SLOT_BAND.get(decl)
    if not band:
        return ""
    target_cx = (band[0] + band[1]) / 2.0
    dx = (target_cx - cx) * _SLOT_GAIN
    dx = max(-_SLOT_MAX_DX, min(_SLOT_MAX_DX, dx))
    if abs(dx) < 0.05:
        return ""
    for e in (plan.get("scene") or {}).get("entities") or []:
        if isinstance(e, dict) and e.get("type") == "character":
            tf = e.setdefault("transform", {})
            p = list(tf.get("position") or [0, 0, 0])
            p[0] = round(float(p[0]) + dx, 3)
            tf["position"] = p
            break
    return "站位平移 x%+.2f（质心 %.2f → 档位中心 %.2f）" % (dx, cx, target_cx)


def _refit_pose(plan: dict, shot: dict) -> str:
    """姿势未应用 → 按剧本动作重新写入 scene 的 poseValues/currentPreset。"""
    try:
        import te_3d_poses as P  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(str(shot.get(_k) or "") for _k in ("action", "description"))
    built = P.build_pose(text)
    pv = dict(built.get("poseValues") or {})
    if not pv:
        return ""
    hit = False
    for e in (plan.get("scene") or {}).get("entities") or []:
        if isinstance(e, dict) and e.get("type") == "character":
            e["poseValues"] = dict(pv)
            e["currentPreset"] = str(built.get("currentPreset") or "")
            hit = True
    if not hit:
        return ""
    return "重写姿势（%d 个关节，preset=%s）" % (len(pv), built.get("currentPreset") or "-")


def repair_plan(shot: dict, plan: dict, qc: dict) -> Tuple[Optional[dict], List[str]]:
    """按质检结论修正渲染计划；返回 (新计划, 修正说明)；无可修项返回 (None, [])。"""
    by_name = {str(c.get("name")): c for c in (qc.get("checks") or [])}
    stats = qc.get("stats") or {}
    cam_key = ""
    try:
        from shot_camera import camera_key as _ck  # noqa: PLC0415
        cam_key = _ck(str((shot or {}).get("camera") or ""))
    except Exception:  # noqa: BLE001
        cam_key = ""
    new = copy.deepcopy(plan or {})
    notes: List[str] = []
    if by_name.get("clipped", {}).get("ok") is False:
        n = _refit_clipped(new, stats)
        if n:
            notes.append(n)
    if by_name.get("framing", {}).get("ok") is False:
        n = _refit_camera(new, cam_key, float(stats.get("h_ratio") or 0.0))
        if n:
            notes.append(n)
    if by_name.get("slot", {}).get("ok") is False:
        n = _refit_slot(new, shot, float(stats.get("cx") or 0.5))
        if n:
            notes.append(n)
    if by_name.get("pose_applied", {}).get("ok") is False:
        n = _refit_pose(new, shot)
        if n:
            notes.append(n)
    return (new, notes) if notes else (None, [])


def qc_and_repair(shot: dict, png_path: str, cam_key: Optional[str] = None,
                  aspect: str = "16:9", target_size=None, width: int = 768,
                  out_dir: Optional[str] = None, render_fn=None,
                  max_rounds: int = 2) -> Dict[str, object]:
    """渲染后调用：质检 → 不通过就修计划重渲 → 再质检（最多 max_rounds 轮修复）。

    :param render_fn: 重渲函数（签名 (plan, out_dir) -> 新 PNG 路径）。为 None 时只质检。
    :return: {ok, path, rounds:[{round, ok, reasons, checks, repair?}], checks, reasons}
    """
    path = str(png_path or "")
    rounds: List[Dict[str, object]] = []
    if not path:
        return {"ok": False, "path": "", "rounds": rounds, "checks": [],
                "reasons": ["无基准图"]}
    try:
        import te_3d_render as R  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        R = None
    diag = R.read_render_diag(path) if R is not None else None
    last_qc: Dict[str, object] = {}
    for rnd in range(int(max_rounds) + 1):
        last_qc = qc_blocking_image(shot, path, diag=diag, cam_key=cam_key)
        rounds.append({"round": rnd, "ok": last_qc["ok"],
                       "reasons": list(last_qc.get("reasons") or []),
                       "checks": last_qc.get("checks"), "path": path})
        if last_qc["ok"] or rnd >= int(max_rounds) or render_fn is None or not out_dir:
            break
        try:
            import te_3d_director as D  # noqa: PLC0415
            plan = D.build_render_plan(shot, aspect=aspect, width=width,
                                       target_size=target_size)
        except Exception as _pe:  # noqa: BLE001
            rounds[-1]["repair"] = ["重建渲染计划失败：%s" % _pe]
            break
        fixed, notes = repair_plan(shot, plan, last_qc)
        rounds[-1]["repair"] = notes
        if not fixed:
            break
        try:
            new_path = render_fn(fixed, out_dir)
        except Exception as _re:  # noqa: BLE001
            rounds[-1]["repair"] = list(notes) + ["重渲异常：%s" % _re]
            break
        if not new_path:
            break
        path = str(new_path)
        diag = R.read_render_diag(path) if R is not None else None
    return {"ok": bool(last_qc.get("ok")), "path": path, "rounds": rounds,
            "checks": last_qc.get("checks") or [],
            "reasons": list(last_qc.get("reasons") or [])}


#: 备选机位俯仰角（**只改俯仰、不改景别**）。为什么只改俯仰：景别是提示词里的硬约束，
#: 为「凑合格」把景别放宽等于篡改约束（见 build_render_plan 的既有纪律）。
_ALT_CAM_ANGLES = ("仰拍", "俯拍")


def _qc_score(qc: dict) -> int:
    """质检得分 = 通过的判据条数（用于在候选里择优；None/skipped 不计）。"""
    return len([c for c in (qc.get("checks") or []) if c.get("ok") is True])


def qc_repair_and_alt_cameras(shot: dict, png_path: str, cam_key: Optional[str] = None,
                              aspect: str = "16:9", target_size=None, width: int = 768,
                              out_dir: Optional[str] = None, render_fn=None,
                              max_rounds: int = 2,
                              cam_angles=_ALT_CAM_ANGLES) -> Dict[str, object]:
    """渲染后**完整闭环**：质检 → 修计划重渲（≤max_rounds）→ 仍不过则换机位俯仰角重渲择优。

    多机位的正确用法在这里：不是机械多渲几张，而是「主板自愈无效时换视角」。
    """
    res = qc_and_repair(shot, png_path, cam_key=cam_key, aspect=aspect,
                        target_size=target_size, width=width, out_dir=out_dir,
                        render_fn=render_fn, max_rounds=max_rounds)
    if res.get("ok") or not out_dir or render_fn is None or not cam_angles:
        return res
    try:
        import te_3d_director as D  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return res
    _ck = cam_key or ""
    if not _ck:
        try:
            from shot_camera import camera_key as _ckf  # noqa: PLC0415
            _ck = _ckf(str((shot or {}).get("camera") or ""))
        except Exception:  # noqa: BLE001
            _ck = ""
    best = res
    best_score = _qc_score(res)
    for ang in cam_angles:
        try:
            plan = D.build_render_plan(shot, aspect=aspect, width=width,
                                       target_size=target_size,
                                       cam_key=_ck or None, cam_angle=ang)
            p = render_fn(plan, out_dir)
        except Exception:  # noqa: BLE001
            p = None
        if not p:
            continue
        r2 = qc_and_repair(shot, p, cam_key=cam_key, aspect=aspect,
                           target_size=target_size, width=width, out_dir=out_dir,
                           render_fn=None, max_rounds=0)
        r2["cameraAngle"] = ang
        if r2.get("ok"):
            return r2
        sc = _qc_score(r2)
        if sc > best_score:
            best, best_score = r2, sc
    return best
