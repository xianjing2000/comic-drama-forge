# -*- coding: utf-8 -*-
'''分集场景 API 蓝图（2026-10-11 从 app.py 迁出）。'''

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
from fs_atomic import read_json_strict
from nle_export import STORYBOARDS_DIR
from nle_export import VIDEOS_DIR
from routes._shared import _ep_dir
from routes._shared import _load_script_for
from routes._shared import _safe_project

logger = logging.getLogger(__name__)

episode_api_bp = Blueprint('episode_api', __name__)

@episode_api_bp.route('/api/episode/scenes', methods=['GET'])
def api_episode_scenes():
    """场次级状态（层级展示用，2026-10-03）：第N集 → 第1场/第2场…

    query: project=<项目键>&episode_no=N。返回每场的场次号/标题/镜数、分镜图完成数、
    场次视频（scene_XX.mp4）是否就绪 —— 前端按「集 → 场」两级树渲染。
    """
    project = _safe_project(request.args.get('project') or '')
    try:
        episode_no = max(1, int(request.args.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    if not project:
        return jsonify({"success": False, "error": "project 必填"}), 400
    script = _load_script_for(project, episode_no) or {}
    flow = [s for s in (script.get("scene_flow") or []) if isinstance(s, dict)]
    # 分镜 manifest（按镜 success 统计每场完成数）
    _flat = os.path.join(STORYBOARDS_DIR, project)
    _sb_dir = _ep_dir(_flat, episode_no)
    _sub = os.path.basename(_sb_dir) if _sb_dir != _flat else ""
    mpath = os.path.join(_sb_dir, "storyboard_manifest.json")
    _mshots = {}
    if os.path.isfile(mpath):
        try:
            m = read_json_strict(mpath, {})
            _mshots = {str(s.get("shot_id")): s
                       for s in ((m or {}).get("shots") or []) if isinstance(s, dict)}
        except Exception:  # noqa: BLE001
            _mshots = {}
    vdir = _ep_dir(os.path.join(VIDEOS_DIR, project), episode_no)
    scenes = []
    for sc in flow:
        sids = [str(x) for x in (sc.get("shot_ids") or [])]
        sb_ok = sum(1 for sid in sids
                    if (_mshots.get(sid) or {}).get("success"))
        _sn = int(sc.get("scene_no") or len(scenes) + 1)
        vfile = os.path.join(vdir, f"scene_{_sn:02d}.mp4")
        scenes.append({
            "scene_no": _sn,
            "heading": sc.get("heading") or f"第{_sn}场",
            "location": sc.get("location"),
            "int_ext": sc.get("int_ext"),
            "time_of_day": sc.get("time_of_day"),
            "shot_ids": sids,
            "shot_count": len(sids),
            "storyboard_ok": sb_ok,
            "video_ready": os.path.isfile(vfile) and os.path.getsize(vfile) > 0,
            "video_url": (f"/api/videos/{project}/{_sub + '/' if _sub else ''}"
                          f"scene_{_sn:02d}.mp4"),
        })
    full = os.path.join(vdir, f"ep{episode_no:02d}_full.mp4")
    if not os.path.isfile(full):
        full = os.path.join(vdir, "episode_full.mp4")
    return jsonify({
        "success": True, "project": project, "episode_no": episode_no,
        "scene_count": len(scenes), "scenes": scenes,
        "full_video_ready": os.path.isfile(full) and os.path.getsize(full) > 0,
    })
