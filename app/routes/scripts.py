# -*- coding: utf-8 -*-
"""剧本文本分析 API 蓝图（三步法搬迁，2026-10-09）。

URL 规则与响应体一字不改，只把 @app.route 换成 @scripts_bp.route。
共享状态来自叶子模块 job_state（唯一来源）；跨域助手来自 routes/_shared.py。
"""
from flask import Blueprint, jsonify, request

from routes._shared import _ai_guide_response, _app_logger, _current_llm_client, _safe_project
# 2026-10-09 修复搬迁遗漏：本模块有 5 处使用 generation_state（L26/41/48/52/101），
# 而这里当初只导入了 lock → POST /api/scripts/analyze-prompts 在「目录内 script_path」时
# 必然 NameError → 500（由 verify_p0_mode_path_deadletter 的 D4 正对照暴露）。
from job_state import generation_state, lock
import novel_to_script
import os
import project_store
import threading
import time
from config import SCRIPT_DIR, STORYBOARDS_DIR
from llm_client import LLMError
from script_prompt_analyzer import analyze_script as analyze_script_prompts, save_script_inplace

scripts_bp = Blueprint('scripts', __name__)

def _analyze_worker(task_id: str, script: dict, script_path: str, mode: str,
                    shot_ids, project_name: str, extra: str):
    def cb(phase, current, total, message, percent):
        with lock:
            generation_state[task_id].update({
                "phase": phase, "current": current, "total": total,
                "message": message, "progress": percent,
            })

    try:
        client = _current_llm_client()
        result = analyze_script_prompts(
            client, script, mode=mode, shot_ids=shot_ids,
            storyboards_dir=STORYBOARDS_DIR, project_name=project_name,
            extra_instruction=extra, progress_cb=cb,
        )
        path = _ensure_script_file(script, script_path, project_name)
        save_script_inplace(script, path)
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "result": result, "script_path": path, "script": script,
            })
    except (LLMError, OSError) as e:
        _app_logger().error(f"提示词分析失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        _app_logger().exception("提示词分析异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分析异常：{e}"})

def _ensure_script_file(script: dict, script_path: str, project_name: str) -> str:
    # P0-4 纵深防御：worker（save_script_inplace 覆盖写盘）只认「项目输出目录内」的既有剧本；
    # 越界/空路径一律视为未提供，改在 SCRIPT_DIR 内另存，绝不对项目外文件落笔。
    if script_path and project_store.is_path_inside_output(script_path) \
            and os.path.isfile(script_path):
        return script_path
    # 2026-10-09 修复真实崩溃：novel_to_script 早已没有 save_generated_script（现存的按集落盘 API 是
    # save_episode_script）。此前该分支一旦走到就抛 AttributeError —— 属历史遗留死路径，
    # 由 verify_p0_mode_path_deadletter 的 D 段暴露。集号从剧本 metadata 取，缺省第 1 集。
    _ep = int((script.get("metadata") or {}).get("episode_no") or 1)
    return novel_to_script.save_episode_script(script, SCRIPT_DIR, project_name, _ep)

@scripts_bp.route('/api/scripts/analyze-prompts', methods=['POST'])
def api_analyze_prompts():
    """为剧本生成/优化 prompt_h3 与参考图提示词（后台任务）

    body: {script, script_path, mode: shots|assets|all, shot_ids: [...], project_name, extra_instruction}
    单镜头重写：mode=shots 且 shot_ids=[该镜头号]
    """
    data = request.json or {}
    script = data.get('script')
    if not isinstance(script, dict) or not script.get('shots'):
        return jsonify({"success": False, "error": "缺少剧本数据（或剧本中没有镜头）"}), 400

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法生成提示词")

    mode = data.get('mode') or 'all'
    if mode not in ('shots', 'assets', 'all'):
        return jsonify({"success": False, "error": "mode 必须是 shots / assets / all"}), 400
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]
    project_name = _safe_project(data.get('project_name')
                                 or (script.get('title') or 'project'))
    script_path = data.get('script_path') or script.get('metadata', {}).get('script_path') or ''
    # P0-4：这条链路会把分析结果**写回** script_path（_ensure_script_file → save_script_inplace
    # 覆盖式 json.dump），是「剧本路径越界」的写盘面 —— 与 /api/final/video、_dub_resolve_script、
    # bind_script 同一校验口径。越界一律 400 拒写（空值仍允许：worker 会在 SCRIPT_DIR 内另存）。
    if script_path and not project_store.is_path_inside_output(script_path):
        return jsonify({"success": False,
                        "error": "剧本路径必须在项目输出目录内（output/），越界路径已拒写"}), 400
    extra = str(data.get('extra_instruction') or '')[:500]

    task_id = f"prompts_{project_name}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": "正在准备提示词生成…", "mode": mode,
            "project_name": project_name,
            "total_shots": len(script.get('shots') or []),
        }
    threading.Thread(target=_analyze_worker,
                     args=(task_id, script, script_path, mode, shot_ids, project_name, extra),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "mode": mode, "project_name": project_name,
                    "script_path": script_path, "shot_ids": shot_ids})
