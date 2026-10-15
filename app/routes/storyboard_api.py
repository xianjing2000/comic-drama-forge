# -*- coding: utf-8 -*-
'''分镜画布/重跑 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
import logging
from flask import Blueprint, jsonify, request, send_file, abort, current_app  # noqa: F401
import os    # noqa: F401
import sys   # noqa: F401
import re    # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
import threading   # noqa: F401
import hashlib     # noqa: F401
import shutil      # noqa: F401
import random      # noqa: F401
import base64      # noqa: F401
import datetime    # noqa: F401
import traceback   # noqa: F401
import subprocess  # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from agent_core import logger
from asset_refs import _build_asset_index
from config import ENABLE_3D_BLOCKING_IMAGE
from config import KEYFRAMES_DIR
from config import PROJECT_TRASH_DIR
from config import QC_DIR
from config import SCRIPT_DIR
from consistency import CONTINUITY_DIR
from job_state import generation_state
from job_state import lock
from keyframe_helpers import _ep_of_script
from keyframe_helpers import _keyframe_sb_map
from nle_export import STORYBOARDS_DIR
from nle_export import VIDEOS_DIR
from qc_helpers import _allocate_storyboard_refs
from qc_helpers import _cap_storyboard_refs
from routes._shared import _autopilot_guard
from routes._shared import _body
from routes._shared import _ep_dir
from routes._shared import _ep_read_dir
from routes._shared import _load_script_for
from routes._shared import _project_or_400
from routes._shared import _project_style
from routes._shared import _safe_project
from routes._shared import _shot_num_key
from routes._shared import _shot_seq
from routes._shared import _trash_move
from routes._shared import comfyui_client
from shot_helpers import _shot_coverage_map
from storyboard_helpers import _sb_canvas_qc
from storyboard_helpers import _sb_quality_tally
from storyboard_helpers import _storyboard_retry_shot_impl
from storyboard_helpers import _storyboard_scratch_map
from storyboard_helpers import _unify_ref_canvas
import cancellation
import consistency
import coverage
import gpu_task_gate
import novel_to_script
import project_store
import style_kit
import te_3d_render

logger = logging.getLogger(__name__)

storyboard_api_bp = Blueprint('storyboard_api', __name__)

@storyboard_api_bp.route('/api/storyboard/canvas/<path:project_name>', methods=['GET'])
def api_storyboard_canvas(project_name):
    """分镜画布数据：每镜一张卡片（分镜图/视频 + 质检分 + 一致性分 + 承载原文 + 台词）

    卡片按剧本 shots 顺序排列；手动排序（order）保存在剧本 metadata.shot_order，
    因此画布顺序与后续视频生成顺序始终一致。

    ⚠️ 2026-10-02 起每张卡片的 `storyboard` 额外带**生成中**信息（`generating` /
    `scratch_url`），让前端在整步落盘之前就能显示「第 N 镜生成中」的预览快照；
    每镜 `storyboard.exists` 的语义不变（仍只代表正式产物已落盘）。
    """
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    script = _load_script_for(project, request.args.get('episode_no'))
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    meta = script.get("metadata") or {}

    # 分镜图 + 质检（集级目录：第 1 集平铺，第 2 集起含 epNN）
    _cv_ep = _ep_of_script(script, request.args.get('episode_no'))
    _cv_sub = f"ep{int(_cv_ep):02d}/" if _cv_ep and int(_cv_ep) > 1 else ""
    sb_map = _keyframe_sb_map(project, script, episode_no=_cv_ep)
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project, _cv_ep)
    sb_manifest = {}
    # ⭐ T25（2026-10-12）：同时保留 manifest 的**原始条目列表** —— 卡片的质量字段
    #   （soft_accepted / critical_issues / delivered）与 summary 的三类计数必须与
    #   manifest 顶层字段**同源同值**，不能再走「投影时丢字段」的老路。
    sb_manifest_items = []
    mpath = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                sb_manifest_items = [s for s in ((json.load(f) or {}).get("shots") or [])
                                     if isinstance(s, dict)]
            for s in sb_manifest_items:
                sb_manifest[_shot_num_key(s.get("shot_id"))] = s
        except Exception as e:  # noqa: BLE001
            logger.warning(f"分镜 manifest 读取失败：{e}")
    # 与 manifest 顶层字段同源的三类质量计数（软放行**不计入** success，T12 口径）
    sb_quality = _sb_quality_tally(sb_manifest_items, base_dir=sb_dir,
                                   use_artifact_meta=True)

    # 视频
    vid_dir = _ep_dir(os.path.join(VIDEOS_DIR, project), _cv_ep)
    vid_map = {}
    if os.path.isdir(vid_dir):
        for fn in sorted(os.listdir(vid_dir)):
            if fn.lower().endswith((".mp4", ".mov", ".webm")):
                vid_map[_shot_num_key(os.path.splitext(fn)[0])] = os.path.join(vid_dir, fn)

    # 一致性报告（按镜头取最低分）
    consistency_by_shot = {}
    try:
        rep = consistency.load_report(project) or {}
        for r in ((rep.get("shot_check") or {}).get("results") or []):
            k = _shot_num_key(r.get("shot"))
            cur = consistency_by_shot.get(k)
            if cur is None or (r.get("score") or 0) < (cur.get("score") or 0):
                consistency_by_shot[k] = {"score": r.get("score"),
                                          "verdict": r.get("verdict"),
                                          "character": r.get("character"),
                                          "mode": r.get("mode")}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"一致性报告读取失败（画布将不含一致性分）：{e}")

    # 原文承载归属
    try:
        cover_map = _shot_coverage_map(script)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"覆盖率归属计算失败：{e}")
        cover_map = {}
    cov_report = {}
    try:
        cov_report = coverage.load_coverage_report(CONTINUITY_DIR, project,
                                                   meta.get("episode_no") or script.get("episode_no") or 1)
    except Exception:  # noqa: BLE001
        cov_report = {}

    order = meta.get("shot_order") or []
    ordered = list(shots)
    if isinstance(order, list) and order:
        ordered = sorted(shots, key=lambda s: (order.index(str(s.get("shot_id")))
                                                if str(s.get("shot_id")) in order else 10 ** 6))
    kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project), _cv_ep)
    # 生成中快照（整步落盘之前也能预览）；纯展示增强，失败即空表
    # P0（2026-10-12）：必须把**本集号**传给 scratch_map，否则第 2 集会扫到
    # 第 1 集平铺目录里的 `shot_NN_tryK.png`，把第 1 集的图当成本集的「生成中」预览。
    scratch_map = _storyboard_scratch_map(project, _cv_ep)
    cards = []
    for i, s in enumerate(ordered):
        sid = s.get("shot_id", i + 1)
        k = _shot_num_key(sid)
        seq = _shot_seq(sid, i + 1)
        sb_file = sb_map.get(k) or sb_map.get(f"shot_{seq:02d}")
        sb_item = sb_manifest.get(k) or {}
        # ⭐ T25：规范化 qc 投影 —— 保证**非空**且携带 T12 质量字段
        #   （soft_accepted / critical_issues / delivered / passed）。旧实现直接
        #   `sb_item.get("qc") or {}`：manifest 缺该镜时给 {}，历史精简条目又只有
        #   status/label —— 前端据此什么都判断不出来。
        card_qc = _sb_canvas_qc(sb_item, sb_file or "", sb_dir, seq)
        card_soft = bool(card_qc.get("soft_accepted"))
        vid = vid_map.get(k) or vid_map.get(f"shot_{seq:02d}")
        kf_end = os.path.join(kf_dir, f"shot_{seq:02d}_end.png")
        cards.append({
            "order": i,
            "shot_id": sid,
            "seq": seq,
            "camera": s.get("camera"),
            "duration": s.get("duration"),
            "location": s.get("location"),
            "emotion": s.get("emotion"),
            "description": s.get("description"),
            "dialogue": s.get("dialogue") or [],
            "dialogue_text": s.get("dialogue_text"),
            "characters_in_shot": s.get("characters_in_shot") or [],
            "items_in_shot": s.get("items_in_shot") or [],
            "storyboard": {
                "exists": bool(sb_file),
                "url": (f"/api/storyboards/file/{project}/{_cv_sub}shot_{seq:02d}.png"
                        if sb_file else ""),
                "path": sb_file or "",
                "qc": card_qc,
                # 软放行**不计入** success/blocked（T12/T25 统一口径）
                "success": bool(sb_item.get("success")) and not card_soft,
                "blocked": bool(sb_item.get("qc_blocked")) and not card_soft,
                # ⭐ T25：把软放行/交付事实**显式**放到卡片顶层 —— 用户实测反馈
                #   「manifest 有 17 条软放行、canvas 软放行 0」，正是因为旧投影只把
                #   item["qc"] 原样带出，item 顶层的 qc_soft_accepted 完全丢失。
                "qc_soft_accepted": card_soft,
                "soft_accepted": card_soft,
                "delivered": bool(card_qc.get("delivered")),
                "critical_issues": card_qc.get("critical_issues") or [],
                "error": sb_item.get("error") or "",
                # ⭐ 生成中快照（2026-10-02）：正式产物未落盘、但 scratch 里已有该镜
                #    的中间图时给出预览。exists/url 语义不变，前端据此显示「生成中」。
                "generating": bool(not sb_file and scratch_map.get(seq)),
                "scratch_url": (scratch_map.get(seq) or {}).get("url", ""),
                # 单镜九宫格标记（2026-10-06）：manifest item 的 grid_layout 投影。
                # 前端灯箱据此叠加 1-9 编号覆盖层（编号不再由模型画进图，见 B 方案）。
                "grid": bool(sb_item.get("grid_layout")),
            },
            "video": {
                "exists": bool(vid),
                "url": (f"/api/videos/{project}/{_cv_sub}{os.path.basename(vid)}"
                        if vid else ""),
                "path": vid or "",
            },
            "keyframe": {
                "start": bool(sb_file),
                "end_exists": os.path.isfile(kf_end),
                "end_url": (f"/api/keyframes/file/{project}/{_cv_sub}shot_{seq:02d}_end.png"
                            if os.path.isfile(kf_end) else ""),
            },
            "consistency": consistency_by_shot.get(k) or {},
            "coverage": {"units": cover_map.get(str(sid)) or [],
                         "unit_count": len(cover_map.get(str(sid)) or [])},
        })

    # ⭐ T25 回归断言（运行时、响亮不静默）：卡片可见的软放行计数必须等于 manifest
    #   顶层 soft_accepted_count —— 不等即说明投影又丢了 qc 字段（历史 bug 复发）。
    _vis_soft = sum(1 for c in cards if c["storyboard"].get("soft_accepted"))
    if _vis_soft != (sb_quality.get("soft_accepted_count") or 0):
        logger.warning(
            "画布软放行计数与 manifest 不一致（canvas=%s manifest=%s，project=%s ep=%s）："
            "卡片投影可能丢失 qc 字段", _vis_soft, sb_quality.get("soft_accepted_count"),
            project, _cv_ep)

    summary = {
        "shot_count": len(cards),
        "storyboard_ready": sum(1 for c in cards if c["storyboard"]["exists"]),
        # ⭐ T25：三类质量计数（真通过 / 软放行 / 阻断）—— 与 manifest 顶层字段同源同值。
        #   前端据此显示「真通过 29 / 软放行 17（37%）/ 其中 6 镜带严重问题」。
        "success_count": sb_quality["success_count"],
        "soft_accepted_count": sb_quality["soft_accepted_count"],
        "critical_issue_count": sb_quality["critical_issue_count"],
        "qc_blocked_count": sb_quality["qc_blocked_count"],
        "delivered_count": sb_quality["delivered_count"],
        "video_ready": sum(1 for c in cards if c["video"]["exists"]),
        "keyframe_end_ready": sum(1 for c in cards if c["keyframe"]["end_exists"]),
        "qc_blocked": sum(1 for c in cards if c["storyboard"]["blocked"]),
        # ⭐ 生成中快照统计（2026-10-02）：正式产物未落盘、但 scratch 已有中间图的镜数。
        #    前端据此显示「生成中 3/6」进度条，不必等整步完成。
        "storyboard_generating": sum(1 for c in cards if c["storyboard"].get("generating")),
        "coverage": {
            "plot_coverage_percent": cov_report.get("plot_coverage_percent"),
            "detail_coverage_percent": cov_report.get("detail_coverage_percent"),
            "missing_count": cov_report.get("missing_count"),
            "passed": cov_report.get("passed"),
            "checked_at": cov_report.get("checked_at"),
        } if cov_report else {},
    }
    return jsonify({"success": True, "project": project,
                    "episode_no": meta.get("episode_no") or script.get("episode_no"),
                    "episode_title": meta.get("episode_title") or script.get("episode_title"),
                    "title": script.get("title"),
                    "summary": summary, "cards": cards,
                    "shot_order": order or [str(s.get("shot_id")) for s in shots]})


@storyboard_api_bp.route('/api/storyboard/shot/reorder', methods=['POST'])
def api_storyboard_shot_reorder():
    """分镜拖拽排序：写回剧本 shots 顺序 + metadata.shot_order

    body: {project_name, episode_no, order: [shot_id, ...]}
    副作用：shot_id 保持原值不变（避免打断既有产物文件名映射），
    仅调整 shots 数组顺序与 shot_order 记录。
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    order = data.get('order') or []
    if err is not None:
        return err
    if not order:
        return jsonify({"success": False, "error": "缺少 project_name / order"}), 400
    key = project_store.safe_key(project)
    episode_no = data.get('episode_no')
    script = _load_script_for(project, episode_no)
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    shots = script.get("shots") or []
    idx = {str(s.get("shot_id")): s for s in shots}
    new_shots = [idx[str(sid)] for sid in order if str(sid) in idx]
    if len(new_shots) != len(shots):
        missing = [str(s.get("shot_id")) for s in shots if str(s.get("shot_id")) not in
                   {str(x) for x in order}]
        return jsonify({"success": False,
                        "error": f"排序清单与镜头不匹配（缺少：{missing[:5]}）"}), 400
    script["shots"] = new_shots
    ep_no = script.get("episode_no") or episode_no or 1
    script.setdefault("metadata", {})["shot_order"] = [str(x) for x in order]
    # ⚠️ 2026-10-12：本文件顶部是 `import datetime`（**模块**导入，见 :18）。这里曾写
    #    `datetime.now()` → AttributeError → 该接口**恒 500**；且抛错点在下面的
    #    save_episode_script 之前，shots 顺序与 shot_order 双双不落盘（排序 0 生效）。
    #    必须走 datetime.datetime.now()（同仓 artifact_helpers.py / keyframe.py 同写法）。
    script["metadata"]["shot_order_updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep_no)
    except Exception:  # noqa: BLE001
        # 异常原文可能带本机绝对路径 → 只进服务端日志，响应体给稳定可读文案
        logger.error("分镜排序落盘失败：项目=%s 集=%s", project, ep_no, exc_info=True)
        return jsonify({"success": False,
                        "error": "剧本落盘失败，镜头顺序未保存（详见服务端日志）"}), 500
    return jsonify({"success": True, "project": project, "episode_no": ep_no,
                    "shot_order": [str(x) for x in order]})


@storyboard_api_bp.route('/api/storyboard/retry-shot', methods=['POST'])
@_autopilot_guard
def api_storyboard_retry_shot():
    """单镜分镜图重跑（同步返回；只影响该镜，不触碰其它镜头产物）

    body: {project_name, shot: {...}, seed?, episode_no?}
    未传 shot 时按 shot_id 从剧本取。

    D1（2026-09-23）：见 `_storyboard_retry_shot_impl` 上方说明。
    """
    with gpu_task_gate.run_gpu_task(
            f"sb_retry_{uuid.uuid4().hex[:8]}", "分镜图单镜重跑"):
        return _storyboard_retry_shot_impl()



