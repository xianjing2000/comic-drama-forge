# -*- coding: utf-8 -*-
'''视频重试 API 蓝图（2026-10-11 从 app.py 迁出）。'''

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
from config import KEYFRAME_CHAIN_MODE
from episode_helpers import _episode_schema_defaults
from job_state import generation_state
from job_state import lock
from nle_export import VIDEOS_DIR
from qc_helpers import _qc_brief
from routes._shared import _autopilot_guard
from routes._shared import _body
from routes._shared import _ep_dir
from routes._shared import _load_script_for
from routes._shared import _project_or_400
from routes._shared import _project_style
from routes._shared import _prune_task_registry
from routes._shared import _serve_safe
from style_helpers import _style_aspect_guard
from video_helpers import _video_generate_worker
from video_helpers import _video_retry_shot_impl
import gpu_task_gate
import keyframe
import preview_gate

logger = logging.getLogger(__name__)

video_api_bp = Blueprint('video_api', __name__)

@video_api_bp.route('/api/video/retry-shot', methods=['POST'])
@_autopilot_guard
def api_video_retry_shot():
    """单镜视频重跑（同步；只重生成该镜的 mp4）

    支持 mode：reference（默认，分镜图+主角锚点）/ keyframe（首尾帧插值）

    D1（2026-09-23）：与分镜重跑同口径——加 @_autopilot_guard（异常不再泄漏成
    裸 HTML 500）+ 整段关键区进入 gpu_task_gate（与批量视频 worker 互斥，
    避免两个 ComfyUI 任务抢同一张 GPU）。
    """
    with gpu_task_gate.run_gpu_task(
            f"video_retry_{uuid.uuid4().hex[:8]}", "单镜视频重跑"):
        return _video_retry_shot_impl()


@video_api_bp.route('/api/video/retry-shots-batch', methods=['POST'])
@_autopilot_guard
def api_video_retry_shots_batch():
    """批量单镜重生成（2026-10-02）：body = {project_name, episode_no?, shot_ids: [...]}

    逐镜**串行**复用 `_video_retry_shot_impl` 的完整链路（切段 / 提示词预检 /
    生成 / 质检 / 落盘 / manifest 回写），GPU 闸门包住**整个批次**（批内不再嵌套
    加锁 —— impl 本身无闸门，闸门在单镜路由壳上）。单镜失败不中断批次；
    上限 12 镜防误触全量重跑。同步返回逐镜结果（前端逐条展示）。
    """
    data = request.json or {}
    ids = data.get('shot_ids')
    if not isinstance(ids, list) or not [s for s in ids if str(s).strip()]:
        return jsonify({"success": False,
                        "error": "shot_ids 必须是非空数组（如 [\"shot_03\", \"shot_07\"]）"}), 400
    ids = [str(s).strip() for s in ids if str(s).strip()][:12]
    base_body = {k: v for k, v in data.items() if k != 'shot_ids'}
    results = []
    with gpu_task_gate.run_gpu_task(
            f"video_retry_batch_{uuid.uuid4().hex[:8]}", "批量单镜重生成"):
        for sid in ids:
            body = dict(base_body)
            body['shot_id'] = sid
            try:
                with current_app.test_request_context(json=body):
                    resp = _video_retry_shot_impl()
                    payload = (resp[0].get_json() if isinstance(resp, tuple)
                               else resp.get_json())
                    status = resp[1] if isinstance(resp, tuple) else resp.status_code
                    results.append({"shot_id": sid, "http_status": status,
                                    **(payload if isinstance(payload, dict) else {})})
            except Exception as e:  # noqa: BLE001  单镜失败不断批次
                logger.warning("[批量重生成] 镜头 %s 失败：%s", sid, e)
                results.append({"shot_id": sid, "success": False, "error": str(e)})
    ok_n = sum(1 for r in results if r.get("success"))
    logger.info("[批量重生成] 完成：%d/%d 镜成功", ok_n, len(results))
    return jsonify({"success": ok_n > 0, "total": len(results), "ok_count": ok_n,
                    "results": results})


@video_api_bp.route('/api/videos/generate', methods=['POST'])
def api_generate_videos():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（同 api_generate_storyboards 口径）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    shots = data.get('shots', [])
    character_refs = data.get('character_refs', [])
    scene_refs = data.get('scene_refs', [])
    storyboards = data.get('storyboards', {}) or {}   # {shot_id: /api/storyboards/file/... 或本地路径}
    use_storyboard = data.get('use_storyboard', True)
    # 2026-10-01 起只保留整集一次生成（per_shot/keyframe 废弃，2026-10-05 移除分支）：
    # 请求传 mode 不再生效，恒按 episode 处理（旧的项目级 video_mode 设定同样只归一为 episode）。
    mode = 'episode'
    timeout_per_segment = int(data.get('timeout_per_segment') or 900)
    episode_tag = str(data.get('episode_tag') or '').strip()
    # 跨镜链式：上一镜尾帧 = 下一镜首帧（auto / always / off，默认取 KEYFRAME_CHAIN_MODE）
    chain_mode = keyframe.norm_chain_mode(
        data.get('chain_mode') or KEYFRAME_CHAIN_MODE)

    # 集号：作为入口幂等键的一部分（见下），也写进 generation_state 供状态回显。
    # 裸 int(episode_no) 会抛 —— 历史前端可能传 "" / null / "2"，统一走 _ep_of_script 同口径的容错。
    # ⚠️ 提前到这里解析：空 shots 时要用它读剧本兜底，后面幂等键 / 状态 / worker 全部复用同一个值。
    _vid_ep = data.get('episode_no')
    try:
        _vid_ep = int(_vid_ep) if str(_vid_ep or "").strip() else 1
    except (TypeError, ValueError):
        _vid_ep = 1

    # ⚠️ 2026-09-28 修复：前端「整集生成视频」按钮（工程台 handleGenerateEpisode）只发
    #    {project_name, episode_no}，从不带 shots —— 旧代码在此直接 400「没有镜头数据」，
    #    按钮永久失败（client.ts 注释承诺的「后端按剧本兜底」从未实现）。
    #    现在：shots 为空时按本集剧本兜底（剧本里的 shots 就是生成视频所需的镜头表）。
    #    ⚠️ 兜底只发生在空 shots 时，有 shots 的调用路径（流水线 / 单镜重跑）行为**完全不变**。
    if not shots:
        _scr_fb = _load_script_for(project_name, _vid_ep)
        if not isinstance(_scr_fb, dict):
            _scr_fb = {}
        shots = _scr_fb.get("shots") or []
        # 参考图同理兜底：只在调用方没显式传时补剧本里已判定的角色 / 场景。
        # （物品的权威来源是剧本、由 _video_generate_worker_body 自行兜底；此处不覆盖显式传参）
        if not character_refs:
            character_refs = _scr_fb.get("characters") or []
        if not scene_refs:
            scene_refs = _scr_fb.get("scenes") or []
    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # ⑥ 视频链路自动引用剧本自动判定的镜头时长（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    task_id = f"video_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        _prune_task_registry(generation_state)
        # G5 + B-11 P1-8：同项目**同集**已有 running 的视频任务 → 复用。
        # ⚠️ 修复（2026-09-25）：旧键只匹配 project_name + step=="video"，**不含集号** ——
        #    用户在第 2 集点「生成视频」，若第 1 集的视频任务还在跑，会被直接吞掉：
        #    返回 reused=True 且 task_id 指向第 1 集的任务，第 2 集永远不生成，
        #    而界面显示「已开始」。分镜侧早已加 episode_no（见 api_generate_storyboards），
        #    视频侧漏了 —— 两条链路口径不一致。
        # 用 `==` 精确比集号（而非 `!=` 排除），历史任务无 episode_no 字段时按 1 处理，
        # 与 `_ep_dir` 的「第 1 集平铺」口径一致。
        def _st_ep(st):
            try:
                return int(st.get("episode_no") or 1)
            except (TypeError, ValueError):
                return 1

        _existing_vid = next((tid for tid, st in generation_state.items()
                              if st.get("status") == "running"
                              and st.get("project_name") == project_name
                              and st.get("step") == "video"
                              and _st_ep(st) == _vid_ep), None)
        if _existing_vid:
            return jsonify({"success": True, "task_id": _existing_vid, "status": "started",
                            "reused": True, "total": len(shots),
                            "mode": mode, "project_name": project_name,
                            "episode_no": _vid_ep})
        generation_state[task_id] = {
            "status": "running", "progress": 0,
            "total": len(shots), "current": 0, "results": [],
            "phase": "视频生成", "qc": _qc_brief("video"),
            "project_name": project_name, "step": "video",
            "episode_no": _vid_ep,
            "episode_stats": episode_stats,
        }

    # 抽取 worker 时这里被截断了：既没启动线程也没有 return，
    # 导致 POST /api/videos/generate 抛 "did not return a valid response" (500)。
    # 现在把「启动后台线程 + 返回 task_id」补回路由本身（worker 只负责干活）。
    # B-01 P1-12：GPU 并发闸门
    def _video_generate_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, f"视频生成({mode})"):
            _video_generate_worker(
                task_id, project_name, shots, character_refs, scene_refs,
                storyboards, use_storyboard, mode, timeout_per_segment,
                # 传规范化后的 _vid_ep（与上面幂等键 / 状态里的集号同源），
                # 而不是原始 data['episode_no'] —— 否则 "" / None 会让落盘目录与状态不一致。
                episode_tag, _vid_ep,
                chain_mode=chain_mode,
                style=(data.get('style') or _project_style(project_name)),
                overwrite=bool(data.get('overwrite')),
                build_only=bool(data.get('build_only')),
                only_scenes=(data.get('only_scenes')
                             if isinstance(data.get('only_scenes'), list) else None))
    thread = threading.Thread(target=_video_generate_worker_gated, daemon=True)
    thread.daemon = True
    thread.start()

    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len(shots), "mode": mode})


@video_api_bp.route('/api/videos/<path:filename>')
def api_video_file(filename):
    """提供视频文件访问"""
    return _serve_safe(VIDEOS_DIR, filename, conditional=True)


@video_api_bp.route('/api/videos/preview/approve', methods=['POST'])
def api_video_preview_approve():
    """批准某集预演 → 之后重新生成本集即走**正式**生产（两级生产第二阶段）。

    批准会绑定该预演产物的哈希（见 preview_gate.approve）：预演被重出一版，
    旧批准自动失效，避免「批的是上一版预演」。
    """
    data = request.json or {}
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    ep = data.get('episode_no') or 1
    path = str(data.get('path') or '')
    if not path:
        # 没传路径 → 在该集视频目录里找预演产物（文件名带 PREVIEW_MARK）
        try:
            _d = _ep_dir(os.path.join(VIDEOS_DIR, project), ep)
            _cand = [os.path.join(_d, f) for f in sorted(os.listdir(_d))
                     if preview_gate.is_preview_path(f)] if os.path.isdir(_d) else []
            path = _cand[-1] if _cand else ''
        except Exception as e:                                       # noqa: BLE001
            logger.warning("查找预演产物失败：%s", e)
    if not path or not os.path.isfile(path):
        return jsonify({"success": False,
                        "error": "未找到该集的预演产物（请先生成预演）"}), 404
    state = preview_gate.approve(project, ep, path, note=str(data.get('note') or ''))
    logger.info("[预演] 已批准：%s 第%s集 → %s", project, ep, os.path.basename(path))
    return jsonify({"success": True, "preview": state, "path": path})
