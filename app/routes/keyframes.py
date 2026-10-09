# -*- coding: utf-8 -*-
"""关键帧 API 蓝图（三步法搬迁，2026-10-09）。

URL 规则与响应体一字不改，只把 @app.route 换成 @keyframes_bp.route。
共享状态来自叶子模块 job_state（唯一来源）；跨域助手来自 routes/_shared.py。
"""
from flask import Blueprint, jsonify, request


def index(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, 'index')(*_a, **_kw)


def _ASSET_DIRS(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_ASSET_DIRS')(*_a, **_kw)


def _ASSET_IMG_EXTS(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_ASSET_IMG_EXTS')(*_a, **_kw)


def _ASSET_IMG_PRIORITY(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_ASSET_IMG_PRIORITY')(*_a, **_kw)


def _STATIC_DIR(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_STATIC_DIR')(*_a, **_kw)


def _build_asset_index(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_build_asset_index')(*_a, **_kw)


def _ep_of_script(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_ep_of_script')(*_a, **_kw)


def _first_existing_asset_image(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_first_existing_asset_image')(*_a, **_kw)


def _keyframe_prompt_preflight(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_keyframe_prompt_preflight')(*_a, **_kw)


def _keyframe_qc_verifier(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_keyframe_qc_verifier')(*_a, **_kw)


def _keyframe_recall_cb(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_keyframe_recall_cb')(*_a, **_kw)


def _keyframe_sb_map(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_keyframe_sb_map')(*_a, **_kw)


def _match_scene_name(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_match_scene_name')(*_a, **_kw)


def _match_shot_chars(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_match_shot_chars')(*_a, **_kw)


def _note_ref_warning(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_note_ref_warning')(*_a, **_kw)


def _pick_scene_view(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_pick_scene_view')(*_a, **_kw)


def _qc_lesson_from_record(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_qc_lesson_from_record')(*_a, **_kw)


def _qc_ref_images(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_qc_ref_images')(*_a, **_kw)


def _qc_retry_hopeless(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_qc_retry_hopeless')(*_a, **_kw)


def _qc_shot_desc(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_qc_shot_desc')(*_a, **_kw)


def _qc_style_of(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_qc_style_of')(*_a, **_kw)


def _record_preflight_lesson(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_record_preflight_lesson')(*_a, **_kw)


def _record_qc_lesson(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_record_qc_lesson')(*_a, **_kw)


def _resolve_item_names(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_resolve_item_names')(*_a, **_kw)


def _resolve_scene_entry(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_resolve_scene_entry')(*_a, **_kw)


def _resolve_static_dir(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_resolve_static_dir')(*_a, **_kw)


def _scene_view_for_shot(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_scene_view_for_shot')(*_a, **_kw)


def _style_aspect_confirmed(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_style_aspect_confirmed')(*_a, **_kw)


def _style_aspect_guard(*_a, **_kw):
    """2026-10-09：该函数是 app.py 里的路由视图（路由仍注册在 app.py），
    这里只为被搬走的内部调用提供同名转发。"""
    import app as _root_app
    return getattr(_root_app, '_style_aspect_guard')(*_a, **_kw)

from routes._shared import _app_logger, _ep_dir, _ep_read_dir, _load_script_for, _project_or_400, _safe_project, _serve_safe, comfyui_client
# 2026-10-09 修复搬迁漏导入：本模块 4 处使用 generation_state（L295/321/348/361），
# 原先只导入 lock → 关键帧生成任务一旦执行就 NameError → 500。
from job_state import generation_state, lock
import gpu_task_gate
import keyframe
import os
import task_store
import threading
import time
from config import KEYFRAMES_DIR, KEYFRAME_CHAIN_MODE, TASKS_DB_PATH

keyframes_bp = Blueprint('keyframes', __name__)

def _keyframes_dir(project_name: str, episode_no=None) -> str:
    d = _ep_dir(os.path.join(KEYFRAMES_DIR, _safe_project(project_name)), episode_no)
    os.makedirs(d, exist_ok=True)
    return d

@keyframes_bp.route('/api/keyframes/plan', methods=['GET'])
def api_keyframes_plan():
    """关键帧尾帧生成预检（不调用模型）"""
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(request.args.get('project_name') or '')
    if err is not None:
        return err
    episode_no = request.args.get('episode_no')
    script = _load_script_for(project, episode_no)
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    kf_dir = _keyframes_dir(project, episode_no)
    sb_map = _keyframe_sb_map(project, script, episode_no=episode_no)
    chain_mode = keyframe.norm_chain_mode(
        request.args.get('chain_mode') or KEYFRAME_CHAIN_MODE)
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir,
                                   only_missing=(request.args.get('only_missing', '1') != '0'),
                                   chain_mode=chain_mode)
    return jsonify({"success": True, "project": project,
                    "shot_count": len(shots),
                    "keyframes_dir": kf_dir,
                    "chain_mode": chain_mode,
                    "start_frames_ready": sum(1 for p in plan if p["has_start"]),
                    "end_frames_ready": sum(1 for p in plan if p["has_end"]),
                    "chained_count": sum(1 for p in plan if p.get("chained")),
                    "to_generate": sum(1 for p in plan if p["need_gen"]),
                    "plan": plan})

@keyframes_bp.route('/api/keyframes/generate', methods=['POST'])
def api_keyframes_generate():
    """批量生成尾帧（Qwen Edit，以分镜图为首帧参考）——后台任务 + 断点续跑"""
    # ⚠️ 故意不设 AI 门禁：尾帧提示词在 shot/剧本数据里（上游产出），本步只做 Qwen Edit
    # 图生图 + 质检，不读 AI 凭证。门禁挂这里会误伤「有存量分镜、但 AI key 未配」的续跑。
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no')) if not data.get('shots') \
        else {"shots": data.get('shots') or []}
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "没有镜头数据"}), 400
    _g = _style_aspect_guard(project)
    if _g is not None:
        return _g
    _kf_ep = _ep_of_script(script, data.get('episode_no'))
    kf_dir = _keyframes_dir(project, _kf_ep)
    sb_map = _keyframe_sb_map(project, script, data.get('storyboards'), episode_no=_kf_ep)
    only_missing = bool(data.get('only_missing', True))
    seed = data.get('seed')
    timeout = int(data.get('timeout') or 900)
    chain_mode = keyframe.norm_chain_mode(
        data.get('chain_mode') or KEYFRAME_CHAIN_MODE)

    task_id = f"keyframe_{project}_{int(time.time())}"
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir, only_missing=only_missing,
                                   chain_mode=chain_mode)
    # 尾帧质检（可选）：默认跟随图片质检开关，不达标换 seed 重画，仍不通过则本镜判失败
    _kf_verify, _kf_vretries = _keyframe_qc_verifier(project, script=script)
    # 尾帧提示词预检（生成前质检）：能自愈的先自愈再出图；成批生成不阻断
    # （与资产 / 整集视频同一取舍 —— 为一条提示词打断整批代价过大）
    _kf_pre, _kf_pre_on = _keyframe_prompt_preflight(project)
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "关键帧尾帧生成",
            "total": len([p for p in plan if p["need_gen"]]), "current": 0,
            "results": [], "keyframes_dir": kf_dir,
        }
    try:
        task_store.get_store(TASKS_DB_PATH).create(
            kind="keyframe", project=project, label=f"{project} 尾帧生成",
            total=len([p for p in plan if p["need_gen"]]), task_id=task_id)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"任务库登记失败（不影响生成）：{e}")

    def _kf_worker():
        store = None
        try:
            store = task_store.get_store(TASKS_DB_PATH)
        except Exception:  # noqa: BLE001
            store = None
        if store:
            try:
                store.start(task_id)     # 登记开始时间（否则任务列表「开始」为空）
            except Exception as e:  # noqa: BLE001
                _app_logger().debug("任务库 start 登记失败（不影响执行）：%s", e)

        def _progress(done, total, item):
            with lock:
                st = generation_state.get(task_id) or {}
                st.update({"current": done, "total": total,
                           "progress": int(done / max(total, 1) * 100)})
                st.setdefault("results", []).append(item)
            if store:
                # 单元级进度：尾帧产物落盘即视为该镜完成（断点续跑判据同源）
                try:
                    store.set_progress(task_id, progress=int(done / max(total, 1) * 100))
                    store.mark_unit(task_id, f"shot_{item.get('seq') or item.get('shot_id')}",
                                    task_store.ST_DONE if item.get("ok") else task_store.ST_FAILED,
                                    result_path=item.get("path") or "",
                                    error=item.get("error") or "")
                except Exception as e:  # noqa: BLE001
                    _app_logger().debug("任务库单元进度写入失败（忽略）：%s", e)

        try:
            report = keyframe.generate_keyframes(
                shots, sb_map, kf_dir, seed=seed, timeout=timeout,
                only_missing=only_missing, progress_cb=_progress,
                chain_mode=chain_mode, verify_cb=_kf_verify,
                max_verify_retries=_kf_vretries, preflight_cb=_kf_pre,
                client=comfyui_client,  # S-04：注入全局 ComfyUIClient 实例（复用连接/共享状态）
                qc_stop_cb=_qc_retry_hopeless,  # G1：尾帧连续两次缺陷相同 → 止损
                recall_cb=_keyframe_recall_cb(project),  # T03a：尾帧质检重试召回历史教训
                project_name=project,  # A-16：尾帧达标落盘时写旁路 .meta.json 用
            )
            with lock:
                generation_state[task_id].update({
                    "status": "completed" if report.get("ok") else "failed",
                    "progress": 100, "report": report,
                    "error": "" if report.get("ok") else "全部尾帧生成失败",
                })
            if store:
                if report.get("ok"):
                    store.finish(task_id, result_path=os.path.join(kf_dir, "keyframes_manifest.json"))
                else:
                    store.fail(task_id, "全部尾帧生成失败")
        except Exception as e:  # noqa: BLE001
            _app_logger().exception("关键帧生成任务失败")
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})
            if store:
                store.fail(task_id, str(e))

    # B-01 P1-12：GPU 并发闸门（不接管 task_db 生命周期，worker 内部已写好）
    def _kf_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, "关键帧生成"):
            _kf_worker()
    th = threading.Thread(target=_kf_worker_gated, daemon=True)
    th.start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len([p for p in plan if p["need_gen"]]),
                    "keyframes_dir": kf_dir, "chain_mode": chain_mode,
                    "qc_enabled": bool(_kf_verify),
                    "prompt_qc_enabled": bool(_kf_pre_on)})

@keyframes_bp.route('/api/keyframes/file/<path:filename>')
def api_keyframes_file(filename):
    """关键帧图片访问：/api/keyframes/file/<项目>/shot_01_end.png"""
    return _serve_safe(KEYFRAMES_DIR, filename)

@keyframes_bp.route('/api/keyframes/list/<path:project_name>')
def api_keyframes_list(project_name):
    """列出项目已有首尾帧"""
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    _kl_ep = request.args.get('episode_no')
    # 审计 P2-2（2026-09-29）：episode_no 非数字时裸 int() 会 500（全库只注册了
    # BadRequest 处理器，ValueError 漏成 HTML 500）。与 _ep_read_dir 的容错口径对齐。
    try:
        _kl_no = int(_kl_ep)
    except (TypeError, ValueError):
        _kl_no = 1
    _kl_sub = f"ep{_kl_no:02d}/" if _kl_no > 1 else ""
    kf_dir = _ep_read_dir(KEYFRAMES_DIR, project, _kl_ep)
    items = []
    if os.path.isdir(kf_dir):
        for fn in sorted(os.listdir(kf_dir)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            seq = "".join(ch for ch in fn.split("_")[1] if ch.isdigit()) if "_" in fn else ""
            kind = "end" if "_end." in fn else ("start" if "_start." in fn else "other")
            items.append({"file": fn, "shot": int(seq) if seq else None, "kind": kind,
                          "url": f"/api/keyframes/file/{project}/{_kl_sub}{fn}",
                          "size": os.path.getsize(os.path.join(kf_dir, fn))})
    return jsonify({"success": True, "project": project, "dir": kf_dir,
                    "count": len(items), "items": items})
