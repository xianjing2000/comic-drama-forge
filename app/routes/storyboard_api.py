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
    mpath = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                for s in ((json.load(f) or {}).get("shots") or []):
                    if isinstance(s, dict):
                        sb_manifest[_shot_num_key(s.get("shot_id"))] = s
        except Exception as e:  # noqa: BLE001
            logger.warning(f"分镜 manifest 读取失败：{e}")

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
    scratch_map = _storyboard_scratch_map(project)
    cards = []
    for i, s in enumerate(ordered):
        sid = s.get("shot_id", i + 1)
        k = _shot_num_key(sid)
        seq = _shot_seq(sid, i + 1)
        sb_file = sb_map.get(k) or sb_map.get(f"shot_{seq:02d}")
        sb_item = sb_manifest.get(k) or {}
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
                "qc": sb_item.get("qc") or {},
                "success": bool(sb_item.get("success")),
                "blocked": bool(sb_item.get("qc_blocked")),
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

    summary = {
        "shot_count": len(cards),
        "storyboard_ready": sum(1 for c in cards if c["storyboard"]["exists"]),
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
    script["metadata"]["shot_order_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep_no)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本落盘失败：{e}"}), 500
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


@storyboard_api_bp.route('/api/storyboard/grid-candidates', methods=['POST'])
def api_storyboard_grid_candidates():
    """为一镜生成九宫格候选构图（异步）。body: {project_name, episode_no, shot_id}"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    shot_key = (data.get('shot_id') or '').strip()
    script = _load_script_for(project_name, episode_no) or {}
    shots = script.get("shots") or []
    shot = next((s for s in shots if isinstance(s, dict) and
                 (str(s.get("shot_id")) == shot_key or
                  str(_shot_seq(s.get("shot_id"), 0)) == shot_key.replace("shot_", ""))), None)
    if shot is None:
        return jsonify({"success": False,
                        "error": f"剧本里找不到镜头：{shot_key}"}), 404
    char_idx = _build_asset_index(script.get("characters") or [], project_name, "character")
    item_idx = _build_asset_index(script.get("items") or [], project_name, "item")
    scene_idx = _build_asset_index(script.get("scenes") or [], project_name, "scene")
    refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project_name)
    if not refs:
        return jsonify({"success": False,
                        "error": "该镜无可用参考图（请先生成资产生成）"}), 400
    style = data.get('style') or _project_style(project_name)
    _res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO,
                             megapixels=style_kit.storyboard_megapixels())
    refs = _unify_ref_canvas(refs, _res["size"], project_name)
    refs = _cap_storyboard_refs(refs, shot)
    labels = [r[1] for r in refs]
    seq = _shot_seq(shot.get("shot_id"), 1)
    task_id = f"shot_grid_{project_name}_{int(time.time() * 1000)}"

    with lock:
        generation_state[task_id] = {
            "status": "running", "phase": "分镜九宫格候选构图",
            "project": project_name, "shot": shot.get("shot_id"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _grid_worker():
        try:
            with gpu_task_gate.run_gpu_task(task_id, "分镜九宫格候选构图"):
                # ---- 3D 导演台：九宫格「3D 构图基准网格」（2026-10-03）----
                # 逐格渲染 9 个候选构图的 3D 站位/机位基准，拼成一张与目标九宫格一一对应的
                # 3x3 网格 → 作 <image1> 构图基准 → 提示词追加 COMPOSITION BASELINE GRID 段。
                # 渲染失败静默降级为纯文字站位锚点（fail-open，绝不阻断九宫格主链路）。
                _grid_refs = [r[2] for r in refs]
                _grid_labels = list(labels)
                _grid_has_blocking = False
                try:
                    from config import ENABLE_3D_BLOCKING_IMAGE
                    if ENABLE_3D_BLOCKING_IMAGE:
                        import te_3d_render
                        if te_3d_render.available():
                            _blk_out = os.path.join(QC_DIR, project_name, "te3d_blocking")
                            _grid_sheet = te_3d_render.render_blocking_grid(
                                shot, _blk_out, target_size=_res["size"]) or ""
                            if _grid_sheet:
                                logger.info("[3D导演台] 九宫格 shot=%s 3D 构图基准网格已渲染：%s",
                                                shot.get("shot_id"), os.path.basename(_grid_sheet))
                                _grid_refs.insert(0, _grid_sheet)
                                _grid_labels.insert(0, "3D构图基准网格")
                                _grid_has_blocking = True
                except Exception as _3d_e:  # noqa: BLE001
                    logger.warning("[3D导演台] 九宫格 shot=%s 3D 基准渲染失败（降级为纯文字站位）：%s",
                                       shot.get("shot_id"), _3d_e)
                result = comfyui_client.generate_shot_grid_candidates(
                    shot, _grid_labels, _grid_refs, project_name,
                    f"shot_{seq:02d}", style=style,
                    has_blocking_image=_grid_has_blocking,
                    seed=random.randint(1, 2 ** 31 - 1), size=_res["size"])
            grid_png = (result.get("files") or [""])[0]
            if not grid_png or not os.path.isfile(grid_png):
                raise RuntimeError("九宫格候选构图生成未返回文件")
            # 集级目录（2026-10-02 修复）：分镜画布读 epNN/ 子目录，grid 产物此前
            # 落平铺目录，第 2 集起选格结果不会出现在该集画布 —— 与
            # _update_storyboard_manifest_shot 的目录/URL 口径对齐。
            _flat = os.path.join(STORYBOARDS_DIR, project_name)
            _sb_dir = _ep_dir(_flat, episode_no)
            _sub = os.path.basename(_sb_dir) if _sb_dir != _flat else ""
            dst = os.path.join(_sb_dir, f"shot_{seq:02d}_grid.png")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(grid_png, dst)
            with lock:
                generation_state[task_id].update({
                    "status": "completed", "progress": 100,
                    "grid_url": (f"/api/storyboards/file/{project_name}/"
                                 f"{_sub + '/' if _sub else ''}shot_{seq:02d}_grid.png"),
                    "result": {"grid": dst},
                })
        except cancellation.Cancelled as e:
            with lock:
                generation_state[task_id].update({"status": "cancelled", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            logger.error("分镜九宫格候选构图失败：%s", e, exc_info=True)
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_grid_worker, daemon=True, name=task_id).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started"})


@storyboard_api_bp.route('/api/storyboard/grid-apply', methods=['POST'])
def api_storyboard_grid_apply():
    """把九宫格里选中的格（1-9）裁切为该镜正式分镜图（旧图移入回收站，可恢复）。

    ⚠️ 选格应用视为**用户人工定稿**：裁切结果直接入库，不再走图片 AI 质检。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    shot_key = (data.get('shot_id') or '').strip()
    try:
        cell = max(1, int(data.get('cell') or 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "cell 必须是 1-9 的整数"}), 400
    if cell > 9:
        return jsonify({"success": False, "error": "cell 必须是 1-9"}), 400
    seq = _shot_seq(shot_key, 0)
    if seq <= 0:
        return jsonify({"success": False, "error": f"无法解析镜号：{shot_key}"}), 400
    # 集级目录（2026-10-02 修复）：与 grid-candidates / 分镜画布同口径，第 2 集
    # 起读写 epNN/ 子目录，选格裁切结果才能落到该集画布实际读取的位置。
    _flat = os.path.join(STORYBOARDS_DIR, project_name)
    _sb_dir = _ep_dir(_flat, episode_no)
    _sub = os.path.basename(_sb_dir) if _sb_dir != _flat else ""
    grid_png = os.path.join(_sb_dir, f"shot_{seq:02d}_grid.png")
    if not os.path.isfile(grid_png):
        return jsonify({"success": False,
                        "error": f"九宫格候选图不存在：{grid_png}（请先生成候选构图）"}), 404
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "shot_grid",
                              f"{stamp}_{project_name}")
    dst = os.path.join(_sb_dir, f"shot_{seq:02d}.png")
    cleared, skipped = [], []
    if os.path.isfile(dst):
        _trash_move(dst, "storyboards", trash_root, cleared, skipped)
    comfyui_client.crop_grid_cell(grid_png, cell - 1, dst)
    logger.info("[shot-grid-apply] 项目=%s 集=%s 镜=%s 第 %d 格已应用（旧图 %d 项入回收站）",
                    project_name, episode_no, shot_key, cell, len(cleared))
    return jsonify({"success": True, "project": project_name, "shot_id": shot_key,
                    "cell": cell, "applied": dst,
                    "url": (f"/api/storyboards/file/{project_name}/"
                            f"{_sub + '/' if _sub else ''}shot_{seq:02d}.png"),
                    "cleared": cleared,
                    "hint": "选中格已裁切为该镜正式分镜图（人工定稿，未走 AI 质检）"})
