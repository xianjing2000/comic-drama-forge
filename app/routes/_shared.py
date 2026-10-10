# -*- coding: utf-8 -*-
import shot_key
import re
import prompt_memory
from flask import current_app, jsonify, request
from werkzeug.exceptions import HTTPException
import project_store
from config import NOVELS_DIR, PROJECT_OUTPUT_DIR
from novel_parser import get_novel
import ai_config
from llm_client import LLMClient, LLMError
from novel_parser import list_novels
import time
import autopilot
import novel_to_script
import logging
import os
import shutil
from config import AI_CONFIG_PATH, AI_MODULES, LLM_CONFIG_PATH, LLM_REQUEST_TIMEOUT
from llm_client import FailoverLLMClient


import qc_client
from config import QC_CONFIG_PATH

from config import DUB_DIR

import ai_chat
import style_kit
import threading
from config import AI_SETTINGS_PATH, COMFYUI_OUTPUT_DIR, FINAL_DIR, QC_DIR, UPSCALE_DIR, VIDEOS_DIR
from flask import abort, send_file
from upscale_client import UpscaleError

import video_watermark
from config import WATERMARK_CONFIG_PATH
import analytics
import task_store
from config import TASKS_DB_PATH

import json
import pipeline
import preview_gate
from config import SCRIPT_DIR
from upscale_client import probe_video as probe_video_info

from comfyui_client import ComfyUIClient

import quality_stage

# ⭐ 2026-10-10 拆分第 1 步：日志器 + 文件搬移已上移到 app/shared_base.py。
# 为什么先抽这三个：它们**零业务依赖**（只用标准库 + flask），且 _app_logger 是
# 被本文件 17 个函数依赖的地基。在这里**再导出**，55 个导入点一个都不用改，零行为变化。
from shared_base import _app_logger, _move_with_retry, _trash_move  # noqa: F401  再导出
# ⭐ 2026-10-10 拆分第 2 步：HTTP 边界工具（错误归一化 / 取 body / 上传名净化 /
# 目录穿越防护的 send_file）上移到 app/shared_web.py，同样只在这里再导出。
from shared_web import _body, _friendly_error, _safe_upload_name, _serve_safe  # noqa: F401  再导出
# ⭐ 2026-10-10 拆分第 3 步：AI 客户端构造与前置门禁上移到 app/shared_ai.py。
from shared_ai import (AI_MODULE_LABEL, _ai_client_for_module, _ai_gate_or_400,  # noqa: F401  再导出
                       _ai_guide_response, _current_llm_client, _optional_llm_client)
# ⭐ 2026-10-10 拆分第 4 步：项目/镜头基础（反向依赖最热的 6 个实体）上移到 app/shared_project.py。
from shared_project import (_first_existing, _project_or_400, _safe_project,  # noqa: F401  再导出
                            _shot_num_key, _shot_seq, comfyui_client)
# ⭐ 2026-10-10 拆分第 6 步：质检 / 超分解析 / 任务队列三组上移到 app/ 层。
from shared_qc import (_qc_gate, _qc_load_cfg, _qc_record,  # noqa: F401  再导出
                       _qc_record_verdict)
from shared_tasks import (_TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES,  # noqa: F401  再导出
                          _prune_task_registry, _task_analytics_hook,
                          _task_queue_status, task_queue)
from shared_upscale import (COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES,  # noqa: F401  再导出
                            _comfy_view_url, _upscale_resolve_comfyview,
                            _upscale_resolve_video, _upscale_url_for_path,
                            upscale_lock, upscale_tasks)
# ⭐ 2026-10-10 拆分第 7 步：剧集/小说/项目收尾三组上移到 app/ 层。
from shared_episode import (_episode_video_stats, _load_legacy_flat_script,  # noqa: F401  再导出
                            _load_script_for, register_final_deliverable)
from shared_novel import (EPISODE_BATCH_LIMIT, UPLOAD_TMP_DIR,  # noqa: F401  再导出
                          _episode_units_for_chapters, _estimate_subchunks, _novels_stats)
from shared_project import (_apply_project_settings, _audio_qc_project_key,  # noqa: F401  再导出
                            _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT,
                            _AUDIO_QC_NON_PROJECT_DIRS, _ep_dir, _ep_read_dir,
                            _novel_key, _project_style, _resolve_novel_project)

def _autopilot_guard(fn):
    """统一异常兜底：托管接口不应把 500 抛给前端，而是返回可读错误

    注意不要把客户端错误（HTTPException，例如请求体不是合法 JSON 时
    werkzeug 抛出的 400 BadRequest）误判成服务端 500——否则前端会看到
    「500 服务内部错误」，而真实原因是自己发了个畸形请求，排查方向会被带偏。
    """
    def _wrap(*a, **k):
        try:
            return fn(*a, **k)
        except KeyError as e:
            return jsonify({"success": False, "error": f"对象不存在：{e}"}), 404
        except HTTPException as e:
            # 保留 werkzeug 原本的语义状态码（400/404/405…），不要降级成 500
            return jsonify({
                "success": False,
                "error": e.description or e.name,
            }), (e.code or 400)
        except Exception as e:  # noqa: BLE001
            _app_logger().exception("托管接口异常")
            return jsonify({"success": False, "error": _friendly_error(e)}), 500
    _wrap.__name__ = fn.__name__
    return _wrap
def _prompt_memory_dead_count() -> int:
    """死教训数（use_count==0 的条数）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("dead_lessons") or 0)
    except Exception:  # noqa: BLE001
        return 0
def _prompt_memory_used_total() -> int:
    """累计被生成链路召回次数（所有教训 use_count 之和）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("used_total") or 0)
    except Exception:  # noqa: BLE001
        return 0
def _prompt_memory_view(kind: str = "", limit: int = 50) -> dict:
    """真实质检教训库的只读视图（供记忆页展示）"""
    try:
        m = prompt_memory.get_memory(PROJECT_OUTPUT_DIR)
        st = m.stats()
        return {
            "total": st.get("total", 0),
            "by_kind": st.get("by_kind") or {},
            "path": st.get("path", ""),
            "lessons": m.list(kind=kind, limit=limit),
        }
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("读取质检教训库失败：%s", e)
        return {"total": 0, "by_kind": {}, "path": "", "lessons": [], "error": str(e)}
def _resolve_continuity_key(novel_id):
    """小说 → (meta, 项目记录, 项目键)，供连贯性查询接口复用"""
    meta = get_novel(NOVELS_DIR, novel_id)
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    return meta, proj, _novel_key(meta, proj["dir_key"] if proj else None)
















def _dub_audio_url(project_name: str, rel_path: str) -> str:
    rel = os.path.relpath(rel_path, _dub_project_dir(project_name)).replace(os.sep, "/")
    return f"/api/tts/file/{project_name}/{rel}" if not rel.startswith("..") else ""


def _dub_project_dir(project_name: str) -> str:
    d = os.path.join(DUB_DIR, project_name)
    os.makedirs(d, exist_ok=True)
    return d





































def _wm_load_cfg() -> dict:
    return video_watermark.load_config(WATERMARK_CONFIG_PATH)






















def _quality_asset_url(kind: str, project: str, name: str) -> str:
    """资产名 → 第一张可用图的 URL（front/base 优先，与生成链路取图同口径）。"""
    d = os.path.join(PROJECT_OUTPUT_DIR, "assets", kind, project, str(name))
    if not os.path.isdir(d):
        return ""
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    except OSError:
        return ""
    if not files:
        return ""
    pick = files[0]
    for prio in ("front", "base"):
        hit = next((x for x in files if x.lower().startswith(prio)), "")
        if hit:
            pick = hit
            break
    return "/api/assets/%s/%s/%s/%s" % (kind, project, name, pick)


def _quality_find_full(project: str, ep) -> str:
    """该集整集成片（播放与批准绑定都认它）；无则空串。"""
    d = _ep_read_dir(VIDEOS_DIR, project, ep)
    if not os.path.isdir(d):
        return ""
    try:
        fps = sorted(f for f in os.listdir(d)
                     if f.lower().endswith(".mp4") and "_full" in f.lower())
    except OSError:
        return ""
    if not fps:
        return ""
    try:
        tag = "ep%02d" % int(ep)
    except (TypeError, ValueError):
        tag = ""
    for f in fps:
        if tag and f.lower().startswith(tag):
            return os.path.join(d, f)
    return os.path.join(d, fps[0])


def _quality_find_preview(project: str, ep) -> dict:
    """该集预演产物（两级生产第一阶段的输出，不可交付）。"""
    d = _ep_read_dir(VIDEOS_DIR, project, ep)
    try:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if preview_gate.is_preview_path(fn):
                    p = os.path.join(d, fn)
                    return {"exists": True, "name": fn, "url": _quality_video_url(p)}
    except OSError as e:                                     # noqa: BLE001
        _app_logger().warning("扫描预演产物失败：%s", e)
    return {"exists": False, "name": "", "url": ""}


def _quality_state_view(project: str, ep) -> dict:
    """四层状态 + 发布就绪判定 + 批准是否已被重渲作废（界面的唯一口径）。"""
    state = quality_stage.load_state(project, ep)
    full = _quality_find_full(project, ep)
    stages, stale, blockers_extra = {}, {}, []
    for s in quality_stage.STAGES:
        e = (state.get("stages") or {}).get(s) or {}
        stages[s] = {"status": e.get("status") or "pending",
                     "name": e.get("name") or quality_stage.STAGE_NAMES[s],
                     "label": e.get("label") or quality_stage.STAGE_LABELS[s],
                     "at": e.get("at") or "", "note": e.get("note") or "",
                     "has_binding": bool(e.get("binding"))}
        # 批准已过期检测：C/D 通过过、但产物已不是批准时那一份
        if s in ("C", "D") and stages[s]["status"] == "passed" and e.get("binding"):
            bstatus, breason = quality_stage.check_stage_binding(state, s, full)
            if bstatus == "invalid":
                stale[s] = breason
                blockers_extra.append("%s(%s) 批准已失效：%s"
                                      % (s, quality_stage.STAGE_NAMES[s], breason))
    _ready, blockers = quality_stage.release_ready(state)
    blockers = list(blockers) + blockers_extra
    ready = not [r for r in blockers if not r.startswith("提示：")]
    return {"stages": stages, "release": {"ready": ready, "blockers": blockers},
            "stale": stale, "updated_at": state.get("updated_at") or ""}


def _quality_video_url(local_path: str) -> str:
    """本地视频路径 → /api/videos URL；出了 VIDEOS_DIR 就不给 URL（防穿越）。"""
    if not local_path:
        return ""
    try:
        rel = os.path.relpath(local_path, VIDEOS_DIR)
    except (ValueError, TypeError):
        return ""
    if rel.startswith(".."):
        return ""
    return "/api/videos/" + rel.replace(os.sep, "/")
