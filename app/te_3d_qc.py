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
#: ⚠️ 2026-10-13 **暂停作为否决判据**（_SLOT_ENFORCE=False）：
#:    真实 70 镜日志统计出的落位是**错乱的** —— 声明 center 实测 0.68~0.82（偏右）、
#:    声明 left 0.52、声明 right 0.50~0.58（left/right 几乎重叠）。
#:    即渲染侧没有把站位世界坐标映射到屏幕水平位置（实测把 px 从 0 移到 20 米，
#:    屏幕中心只移动 0.31 而理论应为 4.8）。
#:    此时若按 _SLOT_BAND 判决，会把**正确构图判成不合格** → 本镜不注入基准图 →
#:    反而拉低质量（与「不能影响质量」相悖）。
#:    保留档位表与 _refit_slot（自愈仍可按名义档位平移），但**不再让它否决基准图**。
#:    待渲染侧映射定位并标定后，把 _SLOT_ENFORCE 改回 True 即恢复判定。
_SLOT_ENFORCE = False
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

    # ---- 是否被画面裁掉 ----
    # ⭐ 2026-10-13 改为**优先按「人偶的屏幕包围盒」**判（render.html 输出 diag.charBoxes）：
    #    此前用「整图色度掩码触边」，而场景背板（40m 高的真实照片 billboard）会铺满
    #    整幅画面并被掩码计入 → 永远判 top 贴边、自愈把机位拉多远都无效
    #    （实测机位 1m → 10m、人偶像素占比 34.8% → 13.9%，而 h_ratio 恒为 1.00、
    #    触边恒为 top）。背板不是人偶 —— 判「人偶是否出画」必须只看人偶本身。
    #    旧图 / 渲染页未输出该字段时，退回原掩码判据（零回归）。
    _cbx = (diag or {}).get("charBoxes") if isinstance(diag, dict) else None
    # ⭐ 2026-10-13 兼容两种形状：[x1,y1,x2,y2] 或 {name, box}（带角色名，供站位判定用）
    _boxes_by_name = {}
    _box_list = []
    if isinstance(_cbx, list):
        for _b in _cbx:
            _bb2 = _b.get("box") if isinstance(_b, dict) else _b
            if not (isinstance(_bb2, (list, tuple)) and len(_bb2) == 4):
                continue
            try:
                _v = [float(x) for x in _bb2]
            except (TypeError, ValueError):
                continue
            _box_list.append(_v)
            if isinstance(_b, dict) and str(_b.get("name") or "").strip():
                _boxes_by_name[str(_b["name"]).strip()] = _v
    # ⚠️⚠️ 2026-10-14：**景别判据（h_ratio）也必须改用 charBoxes**。
    #    原因与上面「被裁」判定完全相同，但此前漏改了这里：
    #    h_ratio 来自 _char_mask_stats 的色度掩码（"有颜色即算人偶"），
    #    而场景背板（山崖/天空/地面/40m billboard 全景图）全是彩色像素 →
    #    整个画面都被算作人偶 → **h_ratio 恒为 1.00**。
    #    实测对比（shot15 中景，机位 8.532m，本地实渲）：
    #      · charBoxes 口径：角色 y 跨度 0.639 NDC / 全高 2 = **0.32**（完全符合预期）
    #      · 掩码口径      ：**1.00**（且自愈拉远到 8.5m 仍报 1.00）
    #    后果：中景这类"本来就该装下全身"的镜头被反复判「竖直占比超标 0.8」，
    #    自愈怎么拉远都不收敛，最后"质量=未交付"——**纯属白跑一轮 GPU**。
    #    charBoxes 是 three.js 按角色包围盒投影出的 NDC 坐标（y ∈ [-1, 1]），
    #    既准确又与背景无关。没有 charBoxes（旧图/未输出）时保持旧口径，零回归。
    if _box_list:
        try:
            _ys = [float(_v[1]) for _v in _box_list] + [float(_v[3]) for _v in _box_list]
            _y0, _y1n = min(_ys), max(_ys)
            if _y1n > _y0:
                _hr_box = (_y1n - _y0) / 2.0          # NDC 全高 = 2
                if _hr_box > 0:
                    stats = dict(stats)
                    stats["h_ratio_mask"] = stats.get("h_ratio")
                    stats["h_ratio"] = round(_hr_box, 4)
                    stats["h_ratio_src"] = "charBoxes"
        except (TypeError, ValueError, IndexError):
            pass
    if _box_list:
        _eps = 0.02
        # ⭐ 2026-10-13：**近景/特写允许下半身出画** —— 那是镜头语言，不是构图错误。
        #    近景的视线落点在胸部以上，本来就该裁掉下半身；此前一律要求「整个人偶
        #    在画内」，把正确的特写判成不合格（实测 shot3 近景：y2=0.52、y1=-4.11，
        #    即画面里是胸部以上，正是近景语义），自愈因此永远修不好那一类镜头。
        #    全景/远景/中景/中近景仍要求完整轮廓（那些档位本来就该装下全身）。
        # 注意传入的可能是原始机位串（如「近景固定」），必须先归一化成景别档位，
        # 否则判据永不命中（实测踩坑：cam_key='近景固定' 时 _allow_bottom 恒为 False）。
        try:
            from shot_camera import camera_key as _ckf  # noqa: PLC0415
            _ck_now = str(_ckf(str(cam_key or "")) or "").strip()
        except Exception:  # noqa: BLE001
            _ck_now = str(cam_key or "").strip()
        _allow_bottom = _ck_now in ("特写", "近景")
        # ⭐ 2026-10-14：「局部」档位**完全跳过贴边判定**。
        #    「局部」= 只拍手部/道具的插入镜（见提示词对 shot_type 的定义），
        #    镜头本来就只对准人偶的某一小块 —— 头顶/脚底/左右必然出画，
        #    这是镜头语言而非构图错误。此前未豁免，实测 shot9（局部）：
        #    h_ratio=3.27（人偶比画面高 3.27 倍）、被判 "bottom/top 贴边"，
        #    而自愈只会把机位拉远 —— 拉远就不再是局部特写了，永远收敛不了，
        #    最后 "质量=未交付" 白跑一轮 GPU。
        #    近景/特写只豁免下半身（它们仍需露出上半身），局部则四条边都豁免。
        _skip_clip = _ck_now == "局部"
        _sides = []
        for _b in _box_list:
            if _skip_clip:
                continue                      # 局部镜：人偶出画属正常，不判贴边
            _x1, _y1, _x2, _y2 = _b
            if _x1 <= -1.0 + _eps:
                _sides.append("left")
            if _x2 >= 1.0 - _eps:
                _sides.append("right")
            if _y1 <= -1.0 + _eps and not _allow_bottom:      # NDC y 向上：y1 = 底边
                _sides.append("bottom")
            if _y2 >= 1.0 - _eps:       # y2 = 顶边
                _sides.append("top")
                _sides.append("top")
        _sides = sorted(set(_sides))
        # ⚠️ 2026-10-13：把**新判据**算出的触边方向写回 stats —— 自愈端
        #    (_refit_clipped) 读的就是 stats["clipped_sides"]，若不同步，
        #    它拿到的还是旧色度掩码的结果（实测出现「新判据没触边、自愈却按
        #    旧的 bottom 去修」以及「贴边：-」这种空方向，导致空修 / 白跑一轮）。
        stats["clipped_sides"] = list(_sides)
        stats["clipped"] = bool(_sides)
        if _sides:
            add("clipped", False, "人偶贴到画面边缘（%s）—— 会被裁掉" % "/".join(_sides))
        else:
            add("clipped", True, "人偶完整在画内（未触边）")
    elif stats.get("clipped"):
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

    # ---- 站位 ----
    # ⭐ 2026-10-13：由「只对单角色镜头有意义」升级为**多角色也能判** —— 借助
    #    render.html 输出的 diag.charBoxes（每个角色各自的屏幕包围盒），取该角色的
    #    水平中心与**它自己**的声明档位比较（过去只有一个整图质心，多角色时是混合
    #    质心，无法对应到某个角色，故当时直接跳过）。
    blk = (shot or {}).get("blocking")
    _slot_judged = 0
    if isinstance(blk, list) and blk and _boxes_by_name:
        for _it in blk:
            if not isinstance(_it, dict):
                continue
            _nm = str(_it.get("name") or "").strip()
            _decl = str(_it.get("x") or _it.get("side") or "").strip().lower()
            _band = _SLOT_BAND.get(_decl)
            _bx = _boxes_by_name.get(_nm) if _nm else None
            if not _band or not _bx:
                continue
            _ncx = ((_bx[0] + _bx[2]) / 2.0 + 1.0) / 2.0     # NDC(-1..1) → 归一(0..1)
            add("slot", (_band[0] <= _ncx <= _band[1]) if _SLOT_ENFORCE else None,
                "声明站位 %s·%s（水平中心应落在 %.2f~%.2f），实测 %.2f%s"
                % (_nm, _decl, _band[0], _band[1], _ncx,
                   "" if _SLOT_ENFORCE else "【映射未标定，本项仅记录、不否决】"))
            _slot_judged += 1
    if not _slot_judged and len(chars) == 1 and isinstance(blk, list) and blk:
        decl = ""
        for it in blk:
            if isinstance(it, dict) and str(it.get("name") or "").strip() == chars[0]:
                decl = str(it.get("x") or it.get("side") or "").strip().lower()
                break
        band = _SLOT_BAND.get(decl)
        if band:
            cx = float(stats["cx"])
            add("slot", (band[0] <= cx <= band[1]) if _SLOT_ENFORCE else None,
                "声明站位 %s（质心应落在 %.2f~%.2f），实测质心 %.2f%s"
                % (decl, band[0], band[1], cx,
                   "" if _SLOT_ENFORCE else "【映射未标定，本项仅记录、不否决】"))
        else:
            add("slot", None, "本镜无显式站位声明，跳过")
    elif not _slot_judged:
        # ⚠️ 2026-10-13：必须带 _slot_judged 前置 —— 否则上面的多角色分支已经
        #    add 了 slot 结果，这里又 add 一条 ok=None，by_name["slot"] 被后者覆盖，
        #    repair_plan 的 _refit_slot 条件永不成立（实测：站位偏差明明报了，
        #    自愈却"修复=[]"整轮不动手）。
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
    return {"ok": not reasons, "checks": checks, "reasons": reasons, "stats": stats,
            # ⭐ 2026-10-13：把「每个角色的屏幕包围盒」一并带出，供 repair_plan 做
            #    多角色站位修正（此前只有一个整图质心，多角色无法定位到具体角色）。
            "charBoxes": _box_list, "charBoxesByName": _boxes_by_name}


# ===================== 自愈：按质检发现的问题修正渲染计划，重渲 =====================
# 用户口径（2026-10-13）：「不通过就不注入，不应该是不通过就根据质检发现的问题
# 重新生成吗」。对 —— 基准图是我们**确定性渲染**出来的，问题可定位、参数可反推，
# 所以不通过时应当修正计划再渲一次，而不是直接放弃。
# 每类问题对应一个修正器；修正后由调用方重渲再质检（最多 max_rounds 轮）。

#: 质心偏差 → 站位位移的增益（经验值；1.0 = 质心差多少就挪多少米，先保守取 4.0）
_SLOT_GAIN = 4.0
#: 单次站位修正的上限（米），避免把角色甩出画面
_SLOT_MAX_DX = 2.0


def _cam_entity(plan: dict):
    """本镜的相机 entity —— 官方 scene_json 里相机是 scene.entities 中的一项。

    ⚠️ 2026-10-13 修复（自愈一直空转的真根因）：此前两个 refit 改的是
    plan["camera"]，而 plan 就是 scene_json 本身（te_3d_render 直接把它写成
    payload），该键**根本不存在** → 两个函数每次都直接 return ""，自愈等于没做。
    生产实证：同一镜头连渲 3 轮，竖直占比 0.85 → 0.85 纹丝不动，纯白烧三次渲染。
    渲染侧只认 entity：render.html 里 camEnt = entities.find(e => e.type === 'camera')。
    """
    for e in (plan.get("scene") or {}).get("entities") or []:
        if isinstance(e, dict) and e.get("type") == "camera":
            return e
    return None


def _look_target(plan: dict, cam_pos: list) -> list:
    """视线目标点：优先顶层 viewCamera.target；缺失时回落「场地中心」。

    本模块的角色恒以场地中心为构图中心（见 _position_for_index），故该回落与
    真实视线方向一致；纯几何量，不依赖渲染侧。
    """
    vc = plan.get("viewCamera")
    if isinstance(vc, dict):
        t = vc.get("target") or vc.get("lookTarget")
        if isinstance(t, (list, tuple)) and len(t) >= 3:
            return [float(t[0]), float(t[1]), float(t[2])]
    return [0.0, float(cam_pos[1]) * 0.5, 0.0]


def _pull_back(plan: dict, scale: float) -> bool:
    """沿视线把机位推拉 scale 倍（保持机位高度）。

    ⚠️ 必须**两条路径都写**（2026-10-13 实测踩坑，这是自愈长期空转的真根因）：
      · 渲染页**优先**用顶层 plan["camera"]（含 position + target）；
      · scene.entities 里的相机**只是 fallback**
        （render.html 的判据是：pc.position 是三元组就用它，否则才用 camEnt）。
    只改其中一处，另一处仍是旧值 → 渲染出来的图**逐像素不变**。
    实测证据：只改 entity 时相机 z 3.842→5.588，但两张 PNG 的 sha1 完全相同。
    """
    cam = plan.get("camera")
    if not isinstance(cam, dict):
        cam = None
    ent = _cam_entity(plan)

    pos = None
    tgt = None
    if cam is not None:
        _p = cam.get("position")
        if isinstance(_p, (list, tuple)) and len(_p) >= 3:
            pos = [float(v) for v in _p]
            _t = cam.get("target")
            if isinstance(_t, (list, tuple)) and len(_t) >= 3:
                tgt = [float(_t[0]), float(_t[1]), float(_t[2])]
    if pos is None:
        if ent is None:
            return False
        _p = (ent.get("transform") or {}).get("position")
        if not (isinstance(_p, (list, tuple)) and len(_p) >= 3):
            return False
        pos = [float(v) for v in _p]
    if tgt is None:
        tgt = _look_target(plan, pos)

    new_pos = [tgt[i] + (pos[i] - tgt[i]) * float(scale) for i in range(3)]
    new_pos[1] = pos[1]          # 只推拉距离，保持机位高度（免得钻地/飞天）
    new_pos = [round(float(v), 3) for v in new_pos]

    wrote = False
    if cam is not None:
        cam["position"] = list(new_pos)
        if not (isinstance(cam.get("target"), (list, tuple)) and len(cam["target"]) >= 3):
            cam["target"] = [round(float(v), 3) for v in tgt]
        plan["camera"] = cam
        wrote = True
    if ent is not None:
        ent.setdefault("transform", {})["position"] = list(new_pos)
        wrote = True
    return wrote


def _tilt_target(plan: dict, dy: float = 0.0, dx: float = 0.0) -> bool:
    """平移**视线目标**（改变取景中心），把出画的一侧拉回画面内。

    ⚠️ 为什么必须有它（2026-10-13 实测）：_pull_back 沿视线推拉且**保持机位高度**，
    当角色贴近相机（近景档位 dist≈1.0m）时，人偶的脚就在相机下方很近处 ——
    进退相机对它几乎无作用（实测 charBoxes 底边 y=-4.11，连拉 4 轮都不收敛）。
    这种情形必须改变**视线方向**而不是距离：target 往出画那一侧挪，
    画面内容就整体朝反方向移动（下移 target → 内容上移 → 底部进画）。

    只写 plan["camera"]["target"]（渲染页优先读它；entity 无 target 字段）。
    """
    if not dy and not dx:
        return False
    cam = plan.get("camera")
    if not isinstance(cam, dict):
        return False
    _t = cam.get("target")
    if isinstance(_t, (list, tuple)) and len(_t) >= 3:
        tgt = [float(_t[0]), float(_t[1]), float(_t[2])]
    else:
        _p = cam.get("position")
        if not (isinstance(_p, (list, tuple)) and len(_p) >= 3):
            return False
        tgt = _look_target(plan, [float(v) for v in _p])
    cam["target"] = [round(tgt[0] + float(dx), 3), round(tgt[1] + float(dy), 3),
                     round(tgt[2], 3)]
    plan["camera"] = cam
    return True


def _refit_clipped(plan: dict, stats: dict) -> str:
    # ⚠️ 2026-10-13 力度上调 1.28 → 1.45：实测「人偶下半身大幅出画」时，
    #    1.28 倍在三轮累积后（1.28^3≈2.1）仍修不回来；1.45 配合上面的**累积修复**
    #    （每轮在上一轮结果上继续）能在 1~2 轮内把整个人偶拉回画面内。
    #    判据一字未放宽 —— 只是让机位真的退到装得下。
    # ⚠️ 2026-10-13 力度 1.28 → 1.45 → 1.7（实测收敛数据驱动）：
    #    shot4 实测「脚下触底边像素」随累积拉远为 176 → 149 → 65，
    #    判定阈值是 max(12, n×1%)（该图 n=3256 → 33）。1.45 三轮收不到 33 以下，
    #    1.7 配合累积（最多 4 轮）才有足够余量把脚拉离底边。
    #    判据一字未放宽 —— 只是让机位退到人偶整个进画。
    _sides = [str(s) for s in (stats.get("clipped_sides") or [])]
    _moved = _pull_back(plan, 1.7)
    # ⚠️ 2026-10-13：拉远之外**再调整视线目标** —— 贴着相机的人偶（近景 dist≈1m）
    #    靠推拉距离解决不了，必须改变视线方向（见 _tilt_target 的说明）。
    #    方向：bottom 触边 → 视线下移（内容上移）；top 触边 → 视线上移；左右同理。
    # ⚠️ 2026-10-13：同一轴**两侧都触边**（top+bottom）说明人偶比画面还高 ——
    #    这时调视线只会顾此失彼（底边修好又冒出顶边，实测 shot4 第1轮
    #    "bottom/top" 反复），此时只能靠拉远，故该轴不调视线。
    _dy = 0.0
    if not ("bottom" in _sides and "top" in _sides):
        if "bottom" in _sides:
            _dy = -0.35
        elif "top" in _sides:
            _dy = 0.35
    _dx = 0.0
    if not ("left" in _sides and "right" in _sides):
        if "left" in _sides:
            _dx = -0.35
        elif "right" in _sides:
            _dx = 0.35
    _tilted = _tilt_target(plan, dy=_dy, dx=_dx)
    if not _moved and not _tilted:
        return ""
    _how = []
    if _moved:
        _how.append("拉远 x1.7")
    if _tilted:
        _how.append("视线调整 dy%+.2f/dx%+.2f" % (_dy, _dx))
    return "机位%s（人偶贴边：%s）" % ("+".join(_how), "/".join(_sides) or "-")


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
    if not _pull_back(plan, scale):
        return ""
    return "机位缩放 x%.2f（竖直占比 %.2f → 目标 %.2f）" % (scale, h_ratio, target)


def _refit_slot(plan: dict, shot: dict, cx: float, boxes_by_name: dict = None) -> str:
    """站位越档 → 按声明档位平移对应角色。

    ⭐ 2026-10-13：支持**多角色** —— 有 diag.charBoxes（每个角色各自的屏幕包围盒）时，
    按「该角色自己的水平中心」与它自己的声明档位比较，逐个平移；此前只有一个整图
    质心，多角色时是混合质心，无法对应到具体角色，所以那时只对单角色生效。
    """
    blk = (shot or {}).get("blocking")
    if not isinstance(blk, list) or not blk:
        return ""
    ents = [e for e in (plan.get("scene") or {}).get("entities") or []
            if isinstance(e, dict) and e.get("type") == "character"]
    if not ents:
        return ""

    def _shift(name: str, dx: float) -> bool:
        """把名字匹配的角色沿 x 平移 dx（群演展开成 name#1/name#2，一并平移）。"""
        hit = False
        for e in ents:
            en = str(e.get("name") or "")
            if en == name or en.startswith(name + "#"):
                tf = e.setdefault("transform", {})
                p = list(tf.get("position") or [0, 0, 0])
                p[0] = round(float(p[0]) + dx, 3)
                tf["position"] = p
                hit = True
        return hit

    # ---- 多角色路径：按角色名逐个体检、逐个平移 ----
    if boxes_by_name:
        notes = []
        for it in blk:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("name") or "").strip()
            decl = str(it.get("x") or it.get("side") or "").strip().lower()
            band = _SLOT_BAND.get(decl)
            bx = boxes_by_name.get(nm) if nm else None
            if not band or not bx:
                continue
            ncx = ((bx[0] + bx[2]) / 2.0 + 1.0) / 2.0      # NDC(-1..1) → 归一(0..1)
            target_cx = (band[0] + band[1]) / 2.0
            dx = (target_cx - ncx) * _SLOT_GAIN
            dx = max(-_SLOT_MAX_DX, min(_SLOT_MAX_DX, dx))
            if abs(dx) < 0.05:
                continue
            if _shift(nm, dx):
                notes.append("%s x%+.2f" % (nm, dx))
        if notes:
            return "站位平移（%s）" % "、".join(notes)
        return ""

    # ---- 单角色退回路径（旧行为，逐字不变）----
    chars = _on_screen_chars(shot)
    if len(chars) != 1:
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
    _shift(chars[0], dx)
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
    # ⚠️ 2026-10-13 修正收敛性：clipped / framing / slot 三者都直接改**机位或角色位置**，
    #    同一轮里一起改会互相抵消 —— 实测：拉远使人偶变小、质心回中，紧接着站位平移
    #    又把它推回去甚至推出画外，于是 5 轮都收敛不了（日志里「bottom + 站位」
    #    反复同时出现，正是这个症状）。
    #    改为**按优先级短路**：先把「人偶是否完整在画内」解决掉，它过了才谈景别，再谈站位。
    #    姿势（改关节，与上面三者不同维度）不受影响，照常一起修。
    _geo_fixed = False
    if by_name.get("clipped", {}).get("ok") is False:
        n = _refit_clipped(new, stats)
        if n:
            notes.append(n)
            _geo_fixed = True
    if not _geo_fixed and by_name.get("framing", {}).get("ok") is False:
        n = _refit_camera(new, cam_key, float(stats.get("h_ratio") or 0.0))
        if n:
            notes.append(n)
            _geo_fixed = True
    if not _geo_fixed and by_name.get("slot", {}).get("ok") is False:
        n = _refit_slot(new, shot, float(stats.get("cx") or 0.5),
                        qc.get("charBoxesByName") or None)
        if n:
            notes.append(n)
            _geo_fixed = True
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
    #: 累积修复计划：每轮在**上一轮的结果**上继续修，而不是每次从原始计划重来。
    #: ⚠️ 2026-10-13 修复（自愈三轮原地打转的真根因）：此前每轮都 build_render_plan
    #: 重建**原始**计划、再各修一次 1.28 倍 —— 三轮得到的计划逐字节相同，渲染命中
    #: 同一张图，实测值永远不动（生产实证：「人偶贴到画面边缘」连报三轮、数值一模一样）。
    _cum_plan = None
    for rnd in range(int(max_rounds) + 1):
        last_qc = qc_blocking_image(shot, path, diag=diag, cam_key=cam_key)
        rounds.append({"round": rnd, "ok": last_qc["ok"],
                       "reasons": list(last_qc.get("reasons") or []),
                       "checks": last_qc.get("checks"), "path": path})
        if last_qc["ok"] or rnd >= int(max_rounds) or render_fn is None or not out_dir:
            break
        try:
            import te_3d_director as D  # noqa: PLC0415
            if _cum_plan is not None:
                plan = _cum_plan          # 在上一轮修复结果上继续修（累积收敛）
            else:
                plan = D.build_render_plan(shot, aspect=aspect, width=width,
                                           target_size=target_size)
        except Exception as _pe:  # noqa: BLE001
            rounds[-1]["repair"] = ["重建渲染计划失败：%s" % _pe]
            break
        fixed, notes = repair_plan(shot, plan, last_qc)
        _cum_plan = fixed if fixed is not None else plan
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
