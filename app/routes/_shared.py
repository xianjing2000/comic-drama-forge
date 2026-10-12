# -*- coding: utf-8 -*-
# ⭐ 2026-10-12 清理拆分残留死 import（t32）：
#   保留三类 —— ① 剩余未下沉函数的真依赖（prompt_memory/get_novel/video_watermark/
#   quality_stage/preview_gate 等）；② 被其它 routes 模块 `from routes._shared import X`
#   中转引用的 config 常量与业务模块（ai_config/qc_client/novel_to_script/list_novels/
#   UpscaleError/AI_CONFIG_PATH/QC_CONFIG_PATH/COMFYUI_OUTPUT_DIR/abort/threading 等）；
#   ③ shared_* 再导出（见下方）。
#   已删 25 个零调用方死 import（ai_chat/autopilot/pipeline/style_kit/task_store/
#   analytics/ComfyUIClient/shot_key/LLMClient/FailoverLLMClient 等），
#   routes._shared 不再「拉起全业务层」。
import os
import threading

import prompt_memory
from flask import abort, jsonify, request
from werkzeug.exceptions import HTTPException
import project_store
from config import (NOVELS_DIR, PROJECT_OUTPUT_DIR, VIDEOS_DIR, DUB_DIR,
                    AI_CONFIG_PATH, LLM_CONFIG_PATH, QC_CONFIG_PATH,
                    COMFYUI_OUTPUT_DIR, FINAL_DIR, UPSCALE_DIR,
                    WATERMARK_CONFIG_PATH)
from novel_parser import get_novel, list_novels

import ai_config
import novel_to_script
import qc_client
import video_watermark
import preview_gate
import quality_stage
from upscale_client import UpscaleError

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

# t105 评估「待迁」：属 HTTP 边界工具，应迁至 shared_web（与 _friendly_error/_body 同族），
# 但目标模块 app/shared_web.py 超出本任务 inScope，留待后续任务。
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
# t105 评估「待迁」：教训库统计视图（_prompt_memory_* 三函数），应迁至 prompt_memory，
# 但目标模块 app/prompt_memory.py 超出本任务 inScope，留待后续任务。
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
# t105 评估「不迁」：用 request.args（Flask 请求上下文）解析 project_id/project_name，
# 属路由层粘合逻辑；下沉需改成显式传参、破坏现有调用方，故保留在路由层。
def _resolve_continuity_key(novel_id):
    """小说 → (meta, 项目记录, 项目键)，供连贯性查询接口复用"""
    meta = get_novel(NOVELS_DIR, novel_id)
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    return meta, proj, _novel_key(meta, proj["dir_key"] if proj else None)
















# t105：_dub_audio_url/_dub_project_dir 已下沉到 dub_helpers（audio-dev 完成），此处改为再导出。
from dub_helpers import _dub_audio_url, _dub_project_dir  # noqa: F401  再导出





































# t105 评估「待迁」：水印配置统一入口，应迁至 video_watermark，
# 但目标模块 app/video_watermark.py 超出本任务 inScope，留待后续任务。
def _wm_load_cfg() -> dict:
    return video_watermark.load_config(WATERMARK_CONFIG_PATH)






















# t105 评估「不迁」：以下 _quality_* 五函数是「质检状态视图」粘合层，聚合 quality_stage（状态机）
# + preview_gate（预演判定）+ shared_project._ep_read_dir（目录）+ VIDEOS_DIR/PROJECT_OUTPUT_DIR，
# 下沉到 quality_stage 会让其反向依赖 preview_gate/shared_project 引入耦合，故保留在路由层。
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
