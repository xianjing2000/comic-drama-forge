# -*- coding: utf-8 -*-
"""视频超分放大（环境探测 / 候选源 / 任务提交与预览） API 蓝图（Blueprint 拆分第十批，2026-10-08）。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @upscale_bp.route。
跨域助手在 routes/_shared.py；本模块只放该域自己的东西。
"""
import json
import os
import time

from flask import Blueprint, current_app, jsonify, request, send_file

import gpu_task_gate
from config import COMFYUI_URL, TE_UPSCALE_DEFAULT_PARAMS, TE_UPSCALE_LOWVRAM_PARAMS, UPSCALE_DEFAULT_PARAMS, UPSCALE_ENGINE
from flask import redirect
from upscale_client import VideoUpscaler, check_environment as upscale_env_check, probe_video as probe_video_info
from routes._shared import COMFYUI_OUTPUT_DIR, FINAL_DIR, UPSCALE_DIR, UPSCALE_URL_PREFIXES, UpscaleError, VIDEOS_DIR, _comfy_view_url, _project_or_400, _project_style, _prune_task_registry, _qc_gate, _qc_load_cfg, _qc_record_verdict, _safe_project, _serve_safe, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, abort, qc_client, threading, upscale_lock, upscale_tasks, _app_logger

upscale_bp = Blueprint('upscale', __name__)
def _upscale_worker(task_id: str, video_path: str, project_name: str, params: dict):
    """后台线程：执行真实超分链路，进度/错误全部写入 upscale_tasks"""
    def progress(msg, pct=None):
        with upscale_lock:
            t = upscale_tasks.get(task_id)
            if not t:
                return
            if pct is not None:
                t["progress"] = max(int(t.get("progress") or 0), int(pct))
            t["message"] = msg
            t["updated_at"] = time.time()

    with upscale_lock:
        upscale_tasks[task_id].update({"status": "running", "progress": 2,
                                       "message": "正在准备超分…"})
    try:
        result = VideoUpscaler().upscale(video_path, project_name=project_name,
                                         progress_cb=progress, **params)
        result["output_url"] = _upscale_url_for_path(result.get("output_path") or "")
        result["input_url"] = _upscale_url_for_path(video_path)

        # —— 超分成品质检（2026-09-24 补上，此前超分是唯一「产出后零质检」的环节）——
        # 超分是成品链路的最后一环（4K 输出），若不质检，超分导致的闪烁/撕裂/糊化会直接
        # 进成片无人拦。这里对「超分后的成品」跑一次视频质检（抽帧 + 多模态判定）。
        # 口径与整集视频质检一致，但**不阻断**（fail-open）：超分是增值环节，质检接口
        # 未就绪 / 成品不达标时只标记 qc_passed=False 并留痕，不把任务判 error ——
        # 用户仍能拿到超分成品，同时能看到质检结论。
        _qc_result = {"checked": False, "passed": None, "reason": ""}
        try:
            _qc_cfg = _qc_load_cfg()
            if qc_client.video_qc_ready(_qc_cfg):
                _out_path = result.get("output_path") or ""
                _before = probe_video_info(video_path) or {}
                _dur = _before.get("duration")
                _style = _project_style(project_name)
                _verdict = qc_client.check_video(
                    _out_path, "超分成品质检（对超分后的成品抽帧，检查是否引入闪烁/撕裂/糊化/色块）",
                    _qc_cfg, style=_style,
                    expected_duration=float(_dur) if _dur else None)
                _gate = _qc_gate(_verdict)
                _qc_result["checked"] = bool(_verdict.get("ok"))
                _qc_result["passed"] = bool(_gate.get("accept", False))
                _qc_result["reason"] = (_gate.get("reason") or _verdict.get("reason")
                                        or _verdict.get("error") or "")
                _qc_result["score"] = _verdict.get("score")
                if _verdict.get("ok"):
                    try:
                        _qc_record_verdict(
                            project_name, "video", "upscale", "超分成品质检",
                            1, None, _out_path, _verdict, style=_style)
                    except Exception as _re:  # noqa: BLE001 质检记录失败不影响超分交付
                        _app_logger().warning("超分质检落盘失败（不影响交付）：%s", _re)
                if _qc_result["passed"] is False:
                    _app_logger().warning("[超分质检] 成品未通过质检：%s", _qc_result["reason"])
                else:
                    _app_logger().info("[超分质检] 成品质检%s：%s",
                                    "通过" if _qc_result["passed"] else "未执行/不可判定",
                                    _qc_result["reason"])
            else:
                _app_logger().info("[超分质检] 视频质检未就绪（开关/接口），跳过（fail-open）")
        except Exception as _qe:  # noqa: BLE001 质检自身异常绝不拖垮超分交付
            _qc_result["reason"] = f"质检异常：{_qe}"
            _app_logger().warning("[超分质检] 质检异常（不影响交付）：%s", _qe)
        result["qc"] = _qc_result

        with upscale_lock:
            upscale_tasks[task_id].update({
                "status": "done", "progress": 100, "message": "超分完成",
                "result": result, "finished_at": time.time(),
            })
        with upscale_lock:
            _prune_task_registry(upscale_tasks)
    except Exception as e:
        import traceback
        traceback.print_exc()
        with upscale_lock:
            upscale_tasks[task_id].update({
                "status": "error", "message": str(e), "error": str(e),
                "finished_at": time.time(),
            })
@upscale_bp.route('/api/upscale/env', methods=['GET'])
def api_upscale_env():
    """超分环境自检：ComfyUI 在线、节点、FlashVSR 模型文件、TE-Speed 加速模板是否就位"""
    env = upscale_env_check()
    env["defaults"] = dict(TE_UPSCALE_DEFAULT_PARAMS) if env.get("te_ready") \
        else dict(UPSCALE_DEFAULT_PARAMS)
    env["legacy_defaults"] = dict(UPSCALE_DEFAULT_PARAMS)
    env["te_defaults"] = dict(TE_UPSCALE_DEFAULT_PARAMS)
    env["te_lowvram"] = dict(TE_UPSCALE_LOWVRAM_PARAMS)
    env["default_engine"] = UPSCALE_ENGINE
    return jsonify(env)
@upscale_bp.route('/api/upscale/sources', methods=['GET'])
def api_upscale_sources():
    """列出可作为超分输入的候选视频（成片 final / 片段 videos / 已有超分产物 / ComfyUI 侧产出）"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    dirs = [("成片", FINAL_DIR), ("视频片段", VIDEOS_DIR), ("超分产物", UPSCALE_DIR)]
    items = []
    prefix_by_base = {os.path.abspath(b): p for p, b in UPSCALE_URL_PREFIXES}
    for label, base in dirs:
        d = os.path.join(base, project_name)
        if not os.path.isdir(d):
            continue
        url_prefix = prefix_by_base.get(os.path.abspath(base), "")
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            items.append({
                "kind": label,
                "name": name,
                "path": os.path.abspath(p),
                "url": f"{url_prefix}{project_name}/{name}",
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    # ComfyUI 侧产出（项目真实生成的镜头视频默认落在 ComfyUI/output 各子目录，
    # 如 video / v5video / 自定义工作流目录；成片前也能直接选中超分）
    comfy_root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    comfy_dirs = [(comfy_root, "ComfyUI")]
    if os.path.isdir(comfy_root):
        try:
            for name in sorted(os.listdir(comfy_root)):
                sub = os.path.join(comfy_root, name)
                if os.path.isdir(sub):
                    comfy_dirs.append((sub, f"ComfyUI/{name}"))
        except OSError as e:
            _app_logger().debug("扫描 ComfyUI 子目录失败（忽略）：%s", e)
    for base, label in comfy_dirs:
        if not os.path.isdir(base):
            continue
        subfolder = os.path.relpath(base, comfy_root).replace(os.sep, "/")
        if subfolder == ".":
            subfolder = ""
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                continue
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            items.append({
                "kind": label,
                "name": name,
                "path": os.path.abspath(p),
                "url": _comfy_view_url(name, subfolder),
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    items.sort(key=lambda it: (it["kind"] not in ("成片", "视频片段"), it["kind"], it["name"]))
    return jsonify({"success": True, "project_name": project_name, "items": items[:300]})
@upscale_bp.route('/api/upscale/comfyview')
def api_upscale_comfyview():
    """代理播放 ComfyUI 侧产出视频：302 重定向到 ComfyUI /view（仅允许 output 目录内文件）"""
    filename = request.args.get('filename') or ''
    subfolder = request.args.get('subfolder') or ''
    try:
        _upscale_resolve_comfyview({'filename': filename, 'subfolder': subfolder})
    except UpscaleError as e:
        _app_logger().warning(f"comfyview 拒绝访问: {e}")
        abort(404)
    from urllib.parse import urlencode
    q = urlencode({'filename': filename, 'subfolder': subfolder, 'type': 'output'})
    return redirect(f"{COMFYUI_URL.rstrip('/')}/view?{q}")
@upscale_bp.route('/api/upscale/list', methods=['GET'])
def api_upscale_list():
    """列出某项目已生成的超分产物"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    d = os.path.join(UPSCALE_DIR, project_name)
    items = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d), reverse=True):
            if not name.lower().endswith(".mp4"):
                continue
            p = os.path.join(d, name)
            items.append({
                "name": name, "path": p,
                "url": f"/api/upscale/{project_name}/{name}",
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    return jsonify({"success": True, "project_name": project_name, "items": items})
@upscale_bp.route('/api/upscale/video', methods=['POST'])
def api_upscale_video():
    """发起视频超分（异步任务）：body 支持 project_name / video_path|video_url / scale / mode 等"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（前端契约必填 project_name，
    # 缺省只会静默写进共享 'project' 命名空间造成串项目）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err

    try:
        video_path = _upscale_resolve_video(data)
    except UpscaleError as e:
        return jsonify({"error": str(e)}), 400

    before = probe_video_info(video_path)
    if not before.get("ok"):
        return jsonify({"error": f"输入视频无法解析: {before.get('error')}"}), 400

    env = upscale_env_check()
    if not env.get("available"):
        return jsonify({"error": "超分环境不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    try:
        scale = int(data.get('scale') or TE_UPSCALE_DEFAULT_PARAMS.get('scale', 2))
    except (TypeError, ValueError):
        return jsonify({"error": "scale 参数非法，仅支持 2 / 3 / 4"}), 400
    if scale not in (2, 3, 4):
        return jsonify({"error": "倍率仅支持 2 / 3 / 4（FlashVSR 支持范围）"}), 400

    # 引擎：默认 TE-Speed-flashVSR 加速链路，可显式指定 legacy-flashvsr
    engine = str(data.get('engine') or UPSCALE_ENGINE or "te-speed-flashvsr").strip().lower()
    if engine not in ("te-speed-flashvsr", "legacy-flashvsr"):
        return jsonify({"error": "engine 仅支持 te-speed-flashvsr / legacy-flashvsr"}), 400
    if engine == "te-speed-flashvsr" and not env.get("te_ready"):
        return jsonify({"error": "TE-Speed 加速链路不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    # 参数：TE-Speed 加速链路（sparse_sage2 + 分块）与旧链路字段都接受，按引擎生效
    params = {k: data.get(k) for k in (
        # TE-Speed-flashVSR 加速参数
        "mode", "precision", "device", "quality_profile", "intensity",
        "spatial_strategy", "memory_policy", "attention_backend",
        "attention_budget", "kv_retention", "local_radius",
        "max_tile_edge", "blend_overlap", "preprocess_batch",
        "quality_value", "color_fix", "frame_load_cap", "skip_first_frames", "free_vram",
        "seed", "timeout",
        # ⚠️ 音轨旁路开关：TE-Speed 链路默认 attach_audio=False，对「成片」超分时
        # 不显式打开会把已合成的配音丢掉，产出无声视频。
        "attach_audio",
        # 旧 FlashVSR 链路参数（回退时生效）
        "tile_size", "tile_overlap", "tiled_vae", "tiled_dit", "unload_dit",
        "sparse_ratio", "kv_ratio", "local_range", "attention_mode", "force_offload",
    ) if data.get(k) is not None}
    params["scale"] = scale
    params["engine"] = engine

    task_id = f"upscale_{int(time.time() * 1000)}"
    with upscale_lock:
        upscale_tasks[task_id] = {
            "task_id": task_id, "status": "pending", "progress": 0,
            "message": "任务已创建", "project_name": project_name,
            "input_path": video_path, "input_probe": before,
            "scale": scale, "engine": engine, "params": params, "created_at": time.time(),
        }
    # B-01 P1-12：GPU 并发闸门
    def _upscale_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, f"超分({engine} {scale}x)"):
            _upscale_worker(task_id, video_path, project_name, params)
    threading.Thread(target=_upscale_worker_gated, daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "input_path": video_path,
                    "input_probe": before, "scale": scale, "engine": engine,
                    "params": params})
@upscale_bp.route('/api/upscale/status/<task_id>', methods=['GET'])
def api_upscale_status(task_id):
    """查询超分任务进度/结果"""
    with upscale_lock:
        task = upscale_tasks.get(task_id)
        task = dict(task) if task else None
    if not task:
        return jsonify({"error": f"未找到超分任务 {task_id}"}), 404
    return jsonify({"success": True, **task})
@upscale_bp.route('/api/upscale/tasks', methods=['GET'])
def api_upscale_tasks():
    """列出全部超分任务（按创建时间倒序）"""
    with upscale_lock:
        items = [dict(t) for t in upscale_tasks.values()]
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return jsonify({"success": True, "items": items[:50]})
@upscale_bp.route('/api/upscale/<path:filename>')
def api_upscale_file(filename):
    """提供超分产物访问（支持 Range 拖动进度条与下载）"""
    return _serve_safe(UPSCALE_DIR, filename, conditional=True,
                      as_attachment=request.args.get('download') == '1')
