# -*- coding: utf-8 -*-
'''分镜产物 API 蓝图（2026-10-11 从 app.py 迁出）。'''

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
from asset_refs import _build_asset_index
from config import QC_DIR
from episode_helpers import _episode_schema_defaults
from job_state import generation_state
from job_state import lock
from nle_export import STORYBOARDS_DIR
from qc_helpers import _qc_brief
from routes._shared import _ep_read_dir
from routes._shared import _project_or_400
from routes._shared import _project_style
from routes._shared import _prune_task_registry
from routes._shared import _safe_project
from routes._shared import _serve_safe
from storyboard_helpers import _storyboard_worker
from style_helpers import _style_aspect_guard
import gpu_task_gate

logger = logging.getLogger(__name__)

storyboards_api_bp = Blueprint('storyboards_api', __name__)

@storyboards_api_bp.route('/api/storyboards/generate', methods=['POST'])
def api_generate_storyboards():
    """为剧本的每个 shot 生成一张分镜图（参考角色/物品/场景资产图）"""
    # ⚠️ 故意不设 AI 门禁：分镜图 = 消费剧本里已产出的 shot.prompt + ComfyUI 出图 + 质检，
    # 不读任何 AI 凭证。挂在 LLM 门禁上会把「没配 key 但有存量剧本」的用户一起拦死。
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    shots = data.get('shots', [])
    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400

    limit = data.get('limit')
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g
    if isinstance(limit, int) and limit > 0:
        shots = shots[:limit]

    # ⑥ 自动引用剧本中已判定的「镜头数 / 每集时长」字段（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    char_idx = _build_asset_index(data.get('characters', []), project_name, "character")
    item_idx = _build_asset_index(data.get('items', []), project_name, "item")
    scene_idx = _build_asset_index(data.get('scenes', []), project_name, "scene")

    # G5：任务 ID 用 uuid（秒级时间戳同秒双 POST 会覆盖 generation_state 且双线程并发抢同一目标路径）；
    # 入口幂等：同项目+同集已有 running 的分镜任务 → 复用其 task_id（reused=True），不重复开线程。
    # 匹配用稳定的 step 字段（worker 运行中 phase 会变化，不能用 phase 判）。
    # B-11 P1-8：守卫键加 episode_no —— 第 2 集请求不再被第 1 集运行中任务吞掉。
    _ep_no = data.get('episode_no')
    with lock:
        _prune_task_registry(generation_state)
        _existing_sb = next((tid for tid, st in generation_state.items()
                             if st.get("status") == "running"
                             and st.get("project_name") == project_name
                             and st.get("step") == "storyboard"
                             and st.get("episode_no") == _ep_no), None)
        if _existing_sb:
            return jsonify({"task_id": _existing_sb, "status": "started", "reused": True,
                            "total": len(shots), "overwrite": bool(data.get('overwrite')),
                            "episode_stats": episode_stats})
        task_id = f"storyboard_{project_name}_{uuid.uuid4().hex[:12]}"
        generation_state[task_id] = {
            "status": "running", "progress": 0, "total": len(shots),
            "current": 0, "phase": "分镜图生成", "results": [],
            "qc": _qc_brief("image"),
            "project_name": project_name, "step": "storyboard",
            "episode_no": _ep_no,
            "refs_available": {
                "characters": {k: bool(v["image"]) for k, v in char_idx.items()},
                "items": {k: bool(v["image"]) for k, v in item_idx.items()},
                "scenes": {k: bool(v["image"]) for k, v in scene_idx.items()},
            },
        }

    # B-01 P1-12：GPU 并发闸门
    def _storyboard_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, "分镜图生成"):
            _storyboard_worker(task_id, project_name, shots, char_idx, item_idx,
                               scene_idx, data.get('episode_no'),
                               _project_style(project_name), bool(data.get('overwrite')))
    thread = threading.Thread(target=_storyboard_worker_gated, daemon=True)
    thread.daemon = True
    thread.start()
    return jsonify({"task_id": task_id, "status": "started", "total": len(shots),
                    "overwrite": bool(data.get('overwrite')),
                    "episode_stats": episode_stats})


@storyboards_api_bp.route('/api/storyboards/manifest/<path:project_name>')
def api_storyboard_manifest(project_name):
    """读取已生成的分镜图清单（用于页面回看）"""
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    _mf_ep = request.args.get('episode_no')
    out_dir = _ep_read_dir(STORYBOARDS_DIR, project, _mf_ep)
    manifest_path = os.path.join(out_dir, "storyboard_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        return jsonify({"success": True, "exists": True, "project_name": project,
                        "manifest": manifest})

    # 无清单时按磁盘文件兜底（项目可能由其他会话生成）
    # ⚠️ URL 必须带集前缀：out_dir 是集级目录（第 2 集起 <项目>/epNN/），
    #    漏掉 epNN 段会让第 2 集起的所有图 404（同 _storyboard_worker 的旧 bug）。
    #    审计 P2-2：episode_no 非数字时裸 int() 会 500，改容错解析。
    try:
        _mf_no = int(_mf_ep)
    except (TypeError, ValueError):
        _mf_no = 1
    _mf_sub = f"ep{_mf_no:02d}/" if _mf_no > 1 else ""
    shots = []
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            if fn.lower().endswith(".png"):
                sid = fn.replace("shot_", "").replace(".png", "")
                shots.append({
                    "shot_id": int(sid) if sid.isdigit() else sid,
                    "success": True,
                    "file": os.path.join(out_dir, fn),
                    "url": f"/api/storyboards/file/{project}/{_mf_sub}{fn}",
                })
    return jsonify({"success": True, "exists": bool(shots), "project_name": project,
                    "manifest": {"project_name": project, "shots": shots,
                                 "total": len(shots),
                                 "success_count": sum(1 for s in shots if s.get("success"))}})


@storyboards_api_bp.route('/api/storyboards/file/<path:filename>')
def api_storyboard_file(filename):
    """提供分镜图文件访问"""
    return _serve_safe(STORYBOARDS_DIR, filename)


@storyboards_api_bp.route('/api/storyboards/scratch/<path:project_name>/<path:filename>')
def api_storyboard_scratch_file(project_name, filename):
    """提供「分镜生成中」中间产物（storyboard_scratch）访问。

    ⭐ 2026-10-02：分镜步骤是整步落盘，正式产物要等 6 镜全跑完才写进
    STORYBOARDS_DIR；期间画布靠本路由读 scratch 目录显示「生成中」预览。
    与 api_storyboard_file 同用 `_serve_safe` 做目录穿越防护
    （base 按项目隔离在 QC_DIR/<项目>/storyboard_scratch 之内）。
    """
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    base = os.path.join(QC_DIR, project, "storyboard_scratch")
    return _serve_safe(base, filename)
