# -*- coding: utf-8 -*-
"""场景九宫格 API 蓝图（三步法搬迁，2026-10-09）。

URL 规则与响应体一字不改，只把 @app.route 换成 @scenes_bp.route。
共享状态来自叶子模块 job_state（唯一来源）；跨域助手来自 routes/_shared.py。
"""
from flask import Blueprint, jsonify, request

from routes._shared import _app_logger, _body, _load_script_for, _project_or_400, _project_style, _safe_project, _trash_move, comfyui_client
# 2026-10-09 修复搬迁漏导入：本模块 5 处使用 generation_state（L67/77/89/100/104），
# 原先只导入 lock → 场次九宫格生成任务一旦执行就 NameError → 500。
from job_state import generation_state, lock
import cancellation
import gpu_task_gate
import os
import random
import scene_grid
import threading
import time
from config import PROJECT_TRASH_DIR, SCENES_DIR
from flask import abort, send_file

scenes_bp = Blueprint('scenes', __name__)

def _find_scene_asset_dir(project_name: str, name: str) -> str:
    """按名称定位场景资产目录（scenes/<项目>/<名称>）"""
    d = os.path.join(SCENES_DIR, project_name, name)
    return d if os.path.isdir(d) else ""

def _scene_grid_prompt_for(project_name: str, name: str) -> str:
    """场景内容描述：优先读剧本 scenes[].reference_prompt_zh / appearance"""
    try:
        script = _load_script_for(project_name, 1) or {}
        for sc in (script.get("scenes") or []):
            if not isinstance(sc, dict):
                continue
            if str(sc.get("name") or "").strip() == name or \
                    str(sc.get("location") or "").strip() == name:
                return str(sc.get("reference_prompt_zh") or sc.get("appearance") or "").strip()
    except Exception as e:  # noqa: BLE001
        _app_logger().debug("场景九宫格读取剧本场景描述失败：%s", e)
    return ""

@scenes_bp.route('/api/scenes/grid-preview', methods=['POST'])
def api_scenes_grid_preview():
    """异步发起场景九宫格机位预览（9 个机位逐张生成 + 3x3 拼接），返回 task_id"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    name = (data.get('name') or '').strip()
    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        return jsonify({"success": False,
                        "error": "场景名不能为空且不得含路径分隔符"}), 400
    asset_dir = _find_scene_asset_dir(project_name, name)
    if not asset_dir:
        return jsonify({"success": False,
                        "error": f"未找到场景资产目录（{name}），请先生成场景资产"}), 404
    base_png = os.path.join(asset_dir, "base.png")
    if not os.path.isfile(base_png):
        return jsonify({"success": False,
                        "error": "该场景还没有 base 图，请先生成场景资产"}), 400
    task_id = f"scene_grid_{project_name}_{int(time.time() * 1000)}"
    style = _project_style(project_name)
    scene_prompt = _scene_grid_prompt_for(project_name, name)
    seed = random.randint(1, 2 ** 31 - 1)

    with lock:
        generation_state[task_id] = {
            "status": "running", "phase": "场景九宫格机位预览",
            "total": len(scene_grid.SCENE_GRID_ANGLES), "current": 0, "progress": 0,
            "project": project_name, "scene": name,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _grid_worker():
        def _cb(done, total, item):
            with lock:
                generation_state[task_id].update({
                    "current": done, "total": total,
                    "progress": int(done / max(total, 1) * 100),
                    "phase": f"机位 {done + 1}/{total}：{(item or {}).get('label', '')}",
                })
        try:
            with gpu_task_gate.run_gpu_task(task_id, "场景九宫格机位预览"):
                out = scene_grid.generate_scene_grid(
                    comfyui_client, project_name, name, base_png,
                    scene_prompt, style, asset_dir, seed=seed, progress_cb=_cb)
            _dir_name = os.path.basename(asset_dir)
            with lock:
                generation_state[task_id].update({
                    "status": "completed", "progress": 100, "result": out,
                    "grid_url": (f"/api/scenes/grid/file/{project_name}/{_dir_name}"
                                 "/grid/grid_preview.png") if out.get("grid") else "",
                    "angle_urls": [
                        {"key": a["key"], "label": a["label"],
                         "url": f"/api/scenes/grid/file/{project_name}/{_dir_name}/{a['key']}.png"}
                        for a in (out.get("angles") or [])],
                })
        except cancellation.Cancelled as e:
            with lock:
                generation_state[task_id].update({"status": "cancelled", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            _app_logger().error("场景九宫格生成失败：%s", e, exc_info=True)
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_grid_worker, daemon=True, name=task_id).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len(scene_grid.SCENE_GRID_ANGLES)})

@scenes_bp.route('/api/scenes/grid/file/<project_name>/<path:relpath>')
def api_scenes_grid_file(project_name, relpath):
    """九宫格产物文件服务（限 scenes/<项目>/<场景>/grid/ 内，防目录穿越）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(SCENES_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, relpath.replace("\\", "/").lstrip("/")))
    grid_root = os.path.abspath(os.path.join(base, "grid"))
    if not filepath.startswith(grid_root + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True)

@scenes_bp.route('/api/scenes/grid-apply', methods=['POST'])
def api_scenes_grid_apply():
    """把选中的机位预览图升级为场景新 base（旧 base 移入回收站，可恢复）"""
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    name = (data.get('name') or '').strip()
    angle_key = (data.get('angle') or '').strip()
    if not name or not angle_key:
        return jsonify({"success": False, "error": "name 与 angle 必填"}), 400
    asset_dir = _find_scene_asset_dir(project_name, name)
    if not asset_dir:
        return jsonify({"success": False, "error": f"未找到场景资产目录（{name}）"}), 404
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "scene_grid",
                              f"{stamp}_{project_name}")
    cleared, skipped = [], []
    try:
        res = scene_grid.apply_grid_angle(
            asset_dir, angle_key,
            lambda src: _trash_move(src, "scene_grid_base", trash_root, cleared, skipped))
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    _app_logger().info("[scene-grid-apply] 项目=%s 场景=%s 机位=%s（清理 %d 项）",
                    project_name, name, angle_key, len(cleared))
    return jsonify({"success": True, "project": project_name, "name": name,
                    "angle": angle_key, **res, "cleared": cleared, "skipped": skipped,
                    "hint": "选中机位图已升级为场景 base；下次分镜参考图生成立即使用新视角；"
                            "各机位视角图可在资产重生时刷新"})
