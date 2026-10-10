"""
漫剧生成系统 - Flask Web 应用
图片资产三类：角色（多视图）/ 物品（3D多视角）/ 场景（3D多视角）
"""
import os
import re
import json
import time
import random
import shutil
import threading
import copy
import uuid
import contextvars
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException

from config import (
    COMFYUI_URL, PROJECT_ROOT_DIR, PROJECT_DATA_DIR, PROJECT_OUTPUT_DIR, SCRIPT_DIR,
    CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR, KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR,
    PROJECT_TRASH_DIR,
    NOVELS_DIR, LLM_CONFIG_PATH,
    NOVEL_DEFAULT_SHOTS, NOVEL_PREVIEW_CHARS, NOVEL_BRIEF_CHARS, LLM_REQUEST_TIMEOUT,
    QC_CONFIG_PATH, QC_DIR,
    CLEAR_COMFYUI_HISTORY, CLEAR_COMFYUI_HISTORY_INTERVAL_SEC,
    WATERMARK_CONFIG_PATH, WATERMARK_DIR,
    AI_CONFIG_PATH, AI_MODULES, AI_CHAT_HISTORY_PATH, AI_SETTINGS_PATH,
    UPSCALE_DIR, UPSCALE_DEFAULT_PARAMS, COMFYUI_OUTPUT_DIR,
    TE_UPSCALE_DEFAULT_PARAMS, TE_UPSCALE_LOWVRAM_PARAMS, UPSCALE_ENGINE,
    DUB_DIR, TTS_DEFAULT_PARAMS, H3_STRIP_AUDIO, H3_EMIT_AUDIO, H3_SFX_ISOLATE,
    DUB_MIX_DIR, MIX_DEFAULT_PARAMS, CONTINUITY_DIR,
    TASKS_DB_PATH, TASK_QUEUE_CONCURRENCY, TASK_UNIT_MIN_BYTES,
    KEYFRAME_CHAIN_MODE, WORKFLOW_TEMPLATE,
    ASSET_VIEW_STEMS,
    SCENE_VIEW_KEYS, SCENE_VIEW_LABELS, SCENE_VIEW_ANGLE_ZH,
    SCENE_ANGLE_TO_VIEW, SCENE_VIEWS_ENABLED, SCENE_VIEW_MAX_RETRIES,
    SCENE_VIEW_DUP_PHASH_MAX,
    # 场景九宫格多视角（2026-10-05）：开关 + 9 机位键序 + 机位句/标签 + 主图文件名/derive_mode
    SCENE_GRID_MODE, SCENE_GRID_ONESHOT,
    SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH, SCENE_GRID_LABELS,
    SCENE_GRID_FILENAME, SCENE_GRID_DERIVE_MODE, SCENE_GRID_CELL_FILE_STEM,
    H3_COMMON_REFS, H3_COMMON_REFS_MAX,
    PROJECT_DEFAULT_CONFIG,
    PROMPT_ENHANCE_CONFIG_PATH, save_prompt_enhance_config, _prompt_enhance_file_flags,
)
from script_generator import ScriptGenerator
from comfyui_client import (ComfyUIClient, camera_spec as _camera_spec,
                            camera_key as _camera_key, camera_angle as _camera_angle,
                            BLOCKING_REF_MARK as _BLOCKING_REF_MARK,
                            IDENTITY_GRID_REF_MARK as _IDENTITY_GRID_REF_MARK)
import comfyui_job_store  # 崩溃免重渲检查点（2026-09-29）：种子沿用判据 + 台账查询
import preview_gate  # 两级生产（2026-09-29）：预演不可交付 + 预演批准
import novel_screenplay  # 文学剧本层（2026-10-03 两段式生产：文学剧本→改写为分镜表）
import quality_stage  # 四层质量状态（2026-09-29：预演也记 A/B 层）
# ⚠️ 注意：本文件里 `comfyui_client` 这个名字是**实例**（见下方 `comfyui_client = ComfyUIClient()`），
# 不是模块。因此**模块级函数**（camera_spec / camera_key / get_call_stats 等）必须像上面这样
# 直接 import 后用别名调用 —— 写成 `comfyui_client.camera_spec(...)` 会在运行时抛
# AttributeError（实例上没有该属性）。类方法（_build_h3_prompt / generate_storyboard 等）
# 通过实例调用没问题，已有的那种写法不用改。
from video_postprocess import VideoPostProcessor, ensure_no_audio, ensure_audio_track
from novel_parser import (
    SUPPORTED_EXTS, NovelParseError, ingest_novel, list_novels,
    get_novel, preview_novel, read_novel_text, split_chapters,
    ensure_chapter_structure, chapter_body_chars
)
from llm_client import (
    LLMClient, LLMError, LLMGatewayUnavailable,
    FailoverLLMClient,
)
import ai_config
import ai_chat
import agent_core
import novel_to_script
import analytics
import autopilot
import consistency
import continuity
import chapter_preflight
import coverage
import script_consistency
import keyframe
import nle_export
import pipeline
import audio_qc
import plugin_registry
import deps_check
import project_store
import shot_key
from fs_atomic import atomic_write_json, read_json_strict
import providers
import comfyui_models
import log_viewer
import asset_name_match
import scene_grid
import model_capabilities
import prompt_qc
import prompt_templates
import qc_client
import qc_coverage
import style_kit
import asset_prompt_kit
import sheet_split
import task_store
import gpu_task_gate
import video_watermark
import upscale_client
import tts_client
import dub_mix
import dialogue_utils
import h3_prompt_kit
import h3_common_refs
import h3_director_builder
import h3_segment_loras
import autonomous
import cancellation
import ai_memory
import prompt_memory
from ai_memory import get_memory_system
from dub_mix import (
    DubMixError, ffmpeg_available as mix_ffmpeg_check, shot_timeline,
    build_entries, mix_video_with_entries, write_mix_report, mix_out_dir,
    probe_audio_info as mix_probe_audio,
)
from tts_client import (
    QwenTTSClient, TTSError, check_environment as tts_env_check,
    build_dub_plan, default_voice_map, normalize_voice, save_voice_map,
    load_voice_map, list_voices as tts_list_voices, probe_audio as probe_audio_info,
    concat_audio, clean_line_text,
    # 参考音频克隆（2026-10-06）
    save_voice_bank_ref, find_voice_bank_ref, list_voice_bank, voice_bank_dir,
    clone_available as tts_clone_available, VOICE_BANK_EXTS
)
from upscale_client import (
    VideoUpscaler, UpscaleError, check_environment as upscale_env_check,
    probe_video as probe_video_info
)
from script_prompt_analyzer import analyze_script as analyze_script_prompts, save_script_inplace
from character_manager import CharacterManager
from relation_manager import RelationManager, RelationConflictDetector

# ---- 跨域助手已下沉到 routes/_shared.py（2026-10-08 第七批）----
from routes._shared import _ai_gate_or_400, _project_or_400, _safe_project, _shot_seq, _trash_move  # noqa: F401  再导出


app = Flask(__name__)

# ---- Blueprint 注册（2026-10-08 起按域拆分，见 app/routes/）----
from routes.system import system_bp  # noqa: E402  系统/环境信息类 API
app.register_blueprint(system_bp)
from routes.memory import memory_bp  # noqa: E402  AI 记忆类 API
app.register_blueprint(memory_bp)
from routes.prompts import prompt_bp  # noqa: E402  提示词模板/增强配置
app.register_blueprint(prompt_bp)
from routes.script_quality import script_qa_bp  # noqa: E402  覆盖率/剧本一致性
app.register_blueprint(script_qa_bp)
from routes.novels import novels_bp  # noqa: E402  小说库/章节/预检
app.register_blueprint(novels_bp)
from routes.projects import projects_bp  # noqa: E402  项目管理
app.register_blueprint(projects_bp)
from routes.ai import _sync_project_config_style, ai_bp  # noqa: E402  AI 配置/对话总控
app.register_blueprint(ai_bp)
from routes.autopilot import autopilot_bp  # noqa: E402  无人值守托管控制面
app.register_blueprint(autopilot_bp)
from routes.qc import qc_bp  # noqa: E402  质检（图片/视频/剧本/提示词）配置与执行
app.register_blueprint(qc_bp)
# ---- tts 域（2026-10-08 第九批）----
from routes.tts import tts_bp  # noqa: E402
app.register_blueprint(tts_bp)
# ---- upscale 域（2026-10-08 第十批）----
from routes.upscale import upscale_bp  # noqa: E402
app.register_blueprint(upscale_bp)
# ---- relations 域（2026-10-08 第十一批）----
from routes.relations import relations_bp
app.register_blueprint(relations_bp)
# ---- watermark 域（2026-10-08 第十二批）----
from routes.watermark import watermark_bp
app.register_blueprint(watermark_bp)
# ---- analytics 域（2026-10-08 第十二批）----
from routes.analytics import analytics_bp
app.register_blueprint(analytics_bp)
# ---- characters 域（2026-10-08 第十三批）----
from routes.characters import characters_bp
app.register_blueprint(characters_bp)
# ---- llm 域（2026-10-08 第十三批）----
from routes.llm import llm_bp
app.register_blueprint(llm_bp)
# ---- agent 域（2026-10-08 第十四批）----
from routes.agent import agent_bp
app.register_blueprint(agent_bp)
# ---- tasks 域（2026-10-08 第十四批）----
from routes.tasks import tasks_bp
app.register_blueprint(tasks_bp)
# ---- final 域（2026-10-08 第十四批）----
from routes.final import final_bp
app.register_blueprint(final_bp)
# ---- export 域（2026-10-08 第十四批）----
from routes.export import export_bp
app.register_blueprint(export_bp)
# ---- episodes 域（2026-10-08 第十四批）----
from routes.episodes import episodes_bp
app.register_blueprint(episodes_bp)
# ---- caption_verify 域（2026-10-08 第十四批）----
from routes.caption_verify import caption_verify_bp
app.register_blueprint(caption_verify_bp)
# ---- consistency 域（2026-10-08 第十四批）----
from routes.consistency import consistency_bp
app.register_blueprint(consistency_bp)
# ---- continuity 域（2026-10-08 第十四批）----
from routes.continuity import continuity_bp
app.register_blueprint(continuity_bp)
# ---- quality 域（2026-10-08 第十四批）----
from routes.quality import quality_bp
app.register_blueprint(quality_bp)
# ---- plugin 域（2026-10-08 第十四批）----
from routes.plugin import plugin_bp
app.register_blueprint(plugin_bp)
# ---- generation_status 域（2026-10-09 三步法搬迁）----
from routes.generation_status import generation_status_bp
app.register_blueprint(generation_status_bp)
from routes.config_api import config_api_bp  # noqa: E402  系统配置/引擎状态（2026-10-11 从 app.py 迁出）
app.register_blueprint(config_api_bp)
# ---- scripts 域（2026-10-09 三步法搬迁）----
from routes.scripts import scripts_bp
app.register_blueprint(scripts_bp)
# ---- scenes 域（2026-10-09 三步法搬迁）----
from routes.scenes import scenes_bp
app.register_blueprint(scenes_bp)
# ---- keyframes 域（2026-10-09 三步法搬迁）----
from routes.keyframes import keyframes_bp
app.register_blueprint(keyframes_bp)

# 2026-10-11 路由拆分：托管控制面 已迁至 routes/autonomous.py。
from routes.autonomous import autonomous_bp  # noqa: E402
app.register_blueprint(autonomous_bp)

# 2026-10-11 路由拆分：分集场景 已迁至 routes/episode_api.py。
from routes.episode_api import episode_api_bp  # noqa: E402
app.register_blueprint(episode_api_bp)

# 2026-10-11 路由拆分：系统状态 已迁至 routes/status_api.py。
from routes.status_api import status_api_bp  # noqa: E402
app.register_blueprint(status_api_bp)

# 2026-10-11 路由拆分：视频重试 已迁至 routes/video_api.py。
from routes.video_api import video_api_bp  # noqa: E402
app.register_blueprint(video_api_bp)
from routes.video_api import (
    api_video_retry_shot, api_video_retry_shots_batch, api_generate_videos,
    api_video_file, api_video_preview_approve)  # noqa: F401
from routes.status_api import (
    api_status)  # noqa: F401
from routes.episode_api import (
    api_episode_scenes)  # noqa: F401
from routes.autonomous import (
    api_autonomous_start, api_autonomous_stop, api_autonomous_resume,
    api_autonomous_status, api_autonomous_chat, api_autonomous_report,
    api_autonomous_report_export, api_autonomous_projects, api_autonomous_deliverables)  # noqa: F401

# 2026-10-11 助手下沉：视频生成助手 已迁至 video_helpers.py。
from video_helpers import (  # noqa: F401, E402
    _VIDEO_TASK_IS_PIPELINE, _ensure_voice_bank_refs, _norm_shot_key,
    _video_generate_worker, _video_generate_worker_body, _video_retry_shot_impl,
    _video_should_stop)

# 2026-10-11 助手下沉：资产生成 worker 已迁至 asset_worker.py。
from asset_worker import (  # noqa: F401, E402
    _ITEM_OWNER_REF_PRIORITY, _generate_asset_task, _item_owner_ref_image)

# 2026-10-11 随分镜批次下沉：这两个名字语义属于分镜 QC，且只被 storyboard_helpers 使用。
from storyboard_helpers import _PromptQCBlocked, _REF_CANVAS_CACHE  # noqa: F401, E402

# 2026-10-11 助手下沉：分镜助手 已迁至 storyboard_helpers.py。
from storyboard_helpers import (  # noqa: F401, E402
    _GRID_PLAN_TPL_FP, _OPT_REASONING_MARKERS, _TE3D_RENDER_LOCK,
    _blocking_spec_text, _build_identity_ref_grid, _fit_ref_to_canvas,
    _grid_panel_plan, _grid_plan_template_fingerprint, _optimize_prompt_from_qc,
    _ref_canvas_target, _sanitize_optimized_prompt, _storyboard_prompt_structurally_ok,
    _storyboard_retry_shot_impl, _storyboard_scratch_map, _storyboard_worker,
    _unify_ref_canvas, _update_storyboard_manifest_shot, _write_artifact_meta)

# 2026-10-11 助手下沉：分镜自愈助手 已迁至 sb_helpers.py。
from sb_helpers import (  # noqa: F401, E402
    _SB_STRUCTURAL_DEFECT_KEYWORDS, _sb_heal_comfyui, _sb_structural_defect)

# 2026-10-11 助手下沉：角色助手 已迁至 character_helpers.py。
from character_helpers import (  # noqa: F401, E402
    _character_base_prompt, _character_outfit_dir, _find_script_character)

# 2026-10-11 助手下沉：资产收集助手 已迁至 collect_helpers.py。
from collect_helpers import (  # noqa: F401, E402
    _collect_asset_refs, _collect_reference_images)

# 2026-10-11 助手下沉：项目级配置助手 已迁至 project_helpers.py。
from project_helpers import (  # noqa: F401, E402
    _project_caption_burn_enabled, _project_subtitle_enabled, _project_worldview)

# 2026-10-11 助手下沉：集级配置助手 已迁至 episode_helpers.py。
from episode_helpers import (  # noqa: F401, E402
    _OUTFIT_RECORD_FILE, _bigram_overlap, _episode_frame_ratios,
    _episode_outfit_overrides, _episode_qc_desc, _episode_schema_defaults)

# 2026-10-11 助手下沉：镜头辅助助手 已迁至 shot_helpers.py。
from shot_helpers import (  # noqa: F401, E402
    _chapter_text_for_script, _shot_coverage_map)

# 2026-10-11 助手下沉：混音助手 已迁至 mix_helpers.py。
from mix_helpers import (  # noqa: F401, E402
    _mix_manifest, _mix_prepare, _mix_resolve_video,
    _mix_segments_dir)

# 2026-10-11 助手按域下沉（第六批）：关键帧助手已迁至 keyframe_helpers.py。
from keyframe_helpers import (  # noqa: F401, E402
    _ep_of_script, _keyframe_prompt_preflight, _keyframe_qc_verifier,
    _keyframe_recall_cb, _keyframe_sb_map, _prompt_preflight)

# 2026-10-11 助手按域下沉（第五批）：质检助手已迁至 qc_helpers.py。
from qc_helpers import (  # noqa: F401, E402
    CLOSEUP_CHAR_CROP_TOP, _OUTFITS_DIRNAME, _allocate_storyboard_refs,
    _apply_closeup_ref_strategy, _cap_storyboard_refs, _closeup_char_crop,
    _normalize_scene_name, _on_screen_characters, _qc_brief,
    _qc_history_file_for, _qc_prev_shot_desc, _qc_prev_shot_ref,
    _qc_prune_attempts, _qc_ref_images, _qc_retry_hopeless,
    _qc_shot_desc, _qc_style_of, _qc_summary,
    _sanitize_outfit_key, _shot_has_char_ref, _shot_has_on_screen,
    _shot_outfit_dir)

# 2026-10-11 助手按域下沉（第四批）：资产索引/参考图选择已迁至 asset_refs.py。
from asset_refs import (  # noqa: F401, E402  助手按域下沉（第四批）
                        _ASSET_IMG_EXTS, _ASSET_IMG_PRIORITY, _STATIC_DIR, _build_asset_index,
                        _first_existing_asset_image, _framing_wants_half_shot,
                        _h3_audio_defs_for, _h3_audio_policy, _h3_common_ref_audios,
                        _h3_common_subject_lock, _h3_is_common_comp, _h3_plan_common_refs,
                        _h3_shot_ref_components, _match_scene_name, _match_shot_chars,
                        _norm_shot_key, _normalize_char_alias, _note_ref_warning,
                        _pick_char_view, _pick_scene_view, _resolve_item_names,
                        _resolve_scene_entry, _resolve_static_dir, _scene_view_for_shot)

# 2026-10-11 助手按域下沉（第三批）：教训库/风格/系统维护各自迁出。
from lesson_helpers import (_apply_audio_hints, _qc_lesson_from_record,  # noqa: F401, E402
                             _record_audio_qc_lesson, _record_preflight_lesson, _record_qc_lesson)
from style_helpers import _style_aspect_confirmed, _style_aspect_guard  # noqa: F401, E402
from system_helpers import (_COMFYUI_CLEAR_HISTORY_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_CLEAR_HISTORY_LOCK, _maybe_clear_comfyui_history)

# 2026-10-11 助手按域下沉（第二批）：质检清理/产物回收已迁至 artifact_helpers.py。
from artifact_helpers import (_COMFYUI_RECLAIM_INTERVAL_SEC, _COMFYUI_RECLAIM_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_RECLAIM_LOCK, _PURGE_REJECTED_ENV, _comfyui_official_dirs,
                               _mark_history_file_purged, _maybe_reclaim_comfyui_output,
                               _purge_prompt_records, _purge_rejected_artifacts,
                               _purge_rejected_enabled, _purge_sb_refs, _reject_artifact)

# 2026-10-11 助手按域下沉（第一批）：配音脚本解析已迁至 dub_helpers.py。
from dub_helpers import (_dub_character_desc, _dub_line_speaker_from_script,  # noqa: F401, E402
                        _dub_resolve_script)

# 2026-10-11 worker 下沉（第三批）：配音/混音已迁至 workers/audio.py。
from workers.audio import (_audio_line_expect_sec, _audio_qc_lines,  # noqa: F401, E402
                           _cleanup_scratch_dir, _dub_prompt_preflight,
                           _dub_worker, _mix_audio_qc, _mix_audio_url, _mix_worker)

# 2026-10-11 worker 下沉（第二批）：分集批量生成已迁至 workers/episodes.py。
from workers.episodes import _episodes_worker, _salvage_episode_script  # noqa: F401, E402

# 2026-10-11 worker 下沉（第一批）：本函数已迁至 workers/screenplay.py。
#   这里保留名字再导出，既有 app._screenplay_worker 调用表面零改动。
from workers.screenplay import _screenplay_worker  # noqa: F401, E402
import job_state
from job_state import (generation_state, lock)  # noqa: F401
from routes.keyframes import (_keyframes_dir, api_keyframes_file, api_keyframes_generate, api_keyframes_list, api_keyframes_plan)  # noqa: F401
from job_state import (generation_state, lock)  # noqa: F401
from routes.scenes import (_find_scene_asset_dir, _scene_grid_prompt_for, api_scenes_grid_apply, api_scenes_grid_file, api_scenes_grid_preview)  # noqa: F401
from job_state import (generation_state, lock)  # noqa: F401
from routes.scripts import (_analyze_worker, _ensure_script_file, api_analyze_prompts)  # noqa: F401
from routes.generation_status import (api_generation_status)  # noqa: F401  （保留旧表面：app.api_generation_status 仍可用）
from routes._shared import (_quality_asset_url, _quality_find_full, _quality_find_preview, _quality_state_view, _quality_video_url)  # noqa: F401
from routes.quality import (_quality_contract_summary, _quality_ep_numbers, _quality_episode_row, _quality_refs_for_shot, _quality_storyboard_url)  # noqa: F401
from routes._shared import (_first_existing, _shot_num_key, comfyui_client)  # noqa: F401
from routes.consistency import (_consistency_collect)  # noqa: F401
from routes.episodes import (_episode_progress)  # noqa: F401
from routes.export import (_serve_attachment)  # noqa: F401
from routes._shared import (_episode_video_stats, _load_legacy_flat_script, _load_script_for, register_final_deliverable)  # noqa: F401
from routes.final import (_wm_apply_to_final, video_processor)  # noqa: F401
from routes._shared import (_task_analytics_hook, _task_queue_status, task_queue)  # noqa: F401
from routes.tasks import (task_db)  # noqa: F401
from routes.llm import (LLM_NOT_CONFIGURED_GUIDE)  # noqa: F401
from routes.watermark import (_wm_view)  # noqa: F401
# 该域被搬走的助手全部保留再导出（守卫可能直接取 app._xxx）。
from routes._shared import (COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _apply_project_settings, _comfy_view_url, _project_style, _prune_task_registry, _qc_gate, _qc_record, _qc_record_verdict, _serve_safe, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks)  # noqa: F401  该域助手已下沉到共享模块
from routes.upscale import (_upscale_worker)  # noqa: F401  本域私有助手
# 该域被搬走的助手全部保留再导出：守卫/测试可能直接取 app._xxx 做端点级验证。
from routes.tts import (_dub_audio_url, _dub_project_dir)  # noqa: F401
# ---- 质检域私有助手再导出（2026-10-08 第八批）----
# 守卫脚本会直接取 app._resolve_audio_qc_target 等做端点级测试，保持旧可达面。
from routes.qc import (_audio_qc_file_url, _audio_qc_visuals_key,  # noqa: F401
                       _qc_test_override, _resolve_audio_qc_target)
# 总控 AI 自主执行内核：注入 Flask 实例，工具调用走进程内直连（不走网络/不绑端口）
agent_core.bind_app(app)
# ⚠️ 审计 P1-6（2026-09-29）：不再启用全局 CORS。
# 旧实现 `CORS(app)` 等价于 Access-Control-Allow-Origin: * —— 任意网页都能在用户
# 浏览器里跨源 fetch 本服务（包括 GET /api/ai/config/reveal 明文回显密钥、全部写操作路由），
# 属「drive-by 偷密钥」面。而本应用三端全部同源：Web 前端由本服务直接托管（app/static）、
# Electron 桌面壳 loadURL('http://127.0.0.1:<port>/')、Vite 开发期走 server.proxy ——
# 没有任何跨源调用方，全局 CORS 纯属攻击面。如未来确需跨源，必须按路由白名单收紧。

# 服务配置（可通过环境变量覆盖：APP_HOST / APP_PORT / APP_DEBUG）
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
APP_DEBUG = os.getenv("APP_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# D-05（P1）整集视频质检抽帧上限：常量与纯逻辑见 app/qc_coverage.py
# （独立零依赖模块，便于 verify_episode_qc_coverage.py 离线单测）。



# 全局状态：**唯一定义在 app/job_state.py**（消费方都从这里导入同一对象，禁止重新赋值）
from job_state import generation_state, lock  # noqa: E402,F401


# 视频 worker「是否托管（pipeline）任务」的执行期标记（contextvar，随线程上下文传递）。
# 用于让「托管暂停」只掐断托管任务，不误杀用户手动触发的生成。见 _video_should_stop。


try:
    # 启动时把上次残留的 running 任务标记为 interrupted（可供前端提示「可继续」）
    job_state.interrupted_tasks = task_db.recycle_interrupted()
except Exception as _e:  # noqa: BLE001  不得因任务库异常导致启动失败
    job_state.interrupted_tasks = 0
    app.logger.warning(f"任务库中断恢复失败（不影响启动）：{_e}")

# ⭐ AI 凭证单一事实源（tasks.db · ai_credentials 表）：启动时一次性把旧
# secrets.enc 双槽（ai.text/ai.qc/ai.chat + qc）+ ai_config.json 非密钥字段迁入 DB。
# 幂等（DB 已有值则跳过），失败不影响启动（读侧对每个模块都有旧口径回落）。
try:
    import ai_credentials_db
    _mig = ai_credentials_db.migrate_from_legacy()
    if _mig.get("migrated"):
        app.logger.info(f"AI 凭证已从旧源迁入 {TASKS_DB_PATH}：{_mig['migrated']}"
                        + (f"（跳过 {_mig.get('skipped')}）" if _mig.get("skipped") else ""))
except Exception as _e:  # noqa: BLE001  迁移失败不得阻断启动
    app.logger.warning(f"AI 凭证库迁移失败（不影响启动，任务读侧仍回落旧口径）：{_e}")

# ⭐ AI 前置自检（P0-5）：启动即体检三个 AI 模块的配置完整性，缺 key / 缺 base_url 时
# 醒目告警（不阻断启动 —— 用户很可能正是启动后才去「AI 设置」页补配置）。
# 这里只做静态检查、不打外网，避免拖慢启动或让启动依赖外部网络；端点可达性在
# 开跑门禁（_ai_gate_or_400）与 /api/ai/selfcheck?probe=1 时才探测。
try:
    import ai_selfcheck
    AI_SELFCHECK_BOOT = ai_selfcheck.startup_report()
except Exception as _e:  # noqa: BLE001  自检失败不得阻断启动
    AI_SELFCHECK_BOOT = {}
    app.logger.warning(f"AI 前置自检执行失败（不影响启动）：{_e}")

# ⭐ 质检存量迁移（2026-10-XX）：质检总开关默认值由「关」改「开」后，把从未显式开过
# 质检的老配置**幂等**补成开启（备份 .bak.*，只改总开关，见 qc_client.migrate_enabled_default）。
# ⚠️⚠️ **严禁在模块级调用本函数** —— 模块级代码会在**任何** `import app`（守卫脚本、
#    离线探针、`python -c "import app"`）时执行，从而改写用户的**真实** qc_config.json，
#    违反本项目「测试绝不读写用户数据」的纪律（与 runtime.json 同款约束）。
#    本函数只允许由**服务真正启动时**的入口（app/serve.py 的 main()、app/app.py 的
#    __main__ 直跑分支）调用一次；另在 GET /api/qc/config 作幂等兜底（用户打开质检页时补迁）。
def boot_qc_migration() -> bool:
    """质检存量迁移（服务真正启动时显式调用一次；幂等；失败绝不阻断启动）。

    Returns:
        True 表示本次确实迁移了；False 表示无需迁移 / 已迁移 / 失败。
    """
    try:
        if qc_client.migrate_enabled_default(QC_CONFIG_PATH):
            app.logger.info("质检存量配置已迁移为「默认开启」")
            return True
    except Exception as _e:  # noqa: BLE001  迁移失败不得阻断启动
        app.logger.warning(f"质检存量迁移失败（不影响启动）：{_e}")
    return False


# 初始化组件
script_gen = ScriptGenerator()

# 无人值守托管：若存在已启用的托管计划，服务启动后自动接着生产（断点续跑）
# 用一个短延时线程延后启动，避免拖慢 Flask 首次响应；失败不影响服务可用性。
# 注意：若上次是用户「主动暂停」的，启动时尊重该状态，不擅自恢复生产。
def autopilot_boot_enabled() -> bool:
    """是否允许「随进程启动自动恢复生产」

    默认开启 —— 24/7 无人值守是本系统的主场景。
    但必须留一个逃生口：任何 `import app` 的短命脚本（单元验证、数据迁移、
    一次性批处理）都会触发 boot，从而与正在挂机的服务**抢同一块 GPU**。
    实测踩过这个坑：一条用于取路由列表的 `python -c "import app"` 直接
    启动了守护进程并开始跑图。需要这类脚本时设置 MJSCXT_AUTOPILOT=0 即可。
    """
    val = (os.getenv("MJSCXT_AUTOPILOT") or "").strip().lower()
    return val not in ("0", "false", "no", "off")


def _autopilot_boot():
    try:
        autopilot._restore_runtime()
        if autopilot.is_paused():
            app.logger.info("托管：上次为「已暂停」状态，启动后保持暂停（可在控制台恢复）")
            return
        enabled = autopilot.enabled_projects()
        if not enabled:
            app.logger.info("托管：没有已启用的项目，守护进程待命（可在控制台一键开启）")
            return
        app.logger.info("托管：检测到 %d 个启用项目，自动恢复生产", len(enabled))
        autopilot.resume()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"托管自动恢复失败（不影响服务）：{e}")


def _schedule_autopilot_boot(delay: float = 3.0):
    """按开关决定是否调度自动恢复（关闭时给出显式提示，避免误以为已托管）"""
    if not autopilot_boot_enabled():
        app.logger.info("托管：MJSCXT_AUTOPILOT=0，本次启动不自动恢复生产"
                        "（如需 24/7 托管请移除该环境变量）")
        return
    threading.Timer(delay, _autopilot_boot).start()


try:
    _schedule_autopilot_boot()
except Exception as _e:  # noqa: BLE001
    app.logger.warning(f"托管启动调度失败：{_e}")


# B-16 P2-11：失败路径清理中间产物。把「生成失败 / 质检阻断 / 异常」时的 scratch
# 目录、.tmp 文件等中间产物统一删掉，避免只增不减。


# ===================== 质检不合格产物：统一移入回收站（可恢复） =====================
# 需求（用户原话）：「质检不合格的图片、提示词或视频要删除，不要留存在本地（包括
# ComfyUI 目录下的）」。这里统一收敛为**移入回收站**而非硬删 —— 误删好图好片是不可逆
# 事故，移入 `output/projects/_trash/qc_reject/<时间戳>_<项目>/` 可人工恢复/复核。
#
# ⚠️ 红线（缺一不可）：
#   1) 调用方必须用 `verdict.get("ok") is True and not gate["accept"]` 作为删除条件。
#      `ok=False`（接口故障/超时/鉴权失败）、`skipped=True`（质检未开启）、
#      `qc_declared 但 qc_on=False`（接口未就绪）**都不是产物不合格** —— 那些删下去会
#      把好图好片删光。本工具只负责「安全地移」，判定由调用方提供，工具内不再猜。
#   2) 路径必须落在 PROJECT_OUTPUT_DIR / COMFYUI_OUTPUT_DIR 之内；
#   3) 显式排除 PROJECT_TRASH_DIR（避免把自己的回收站再搬一层）；
#   4) 硬链接（st_nlink > 1）不释放空间、且可能被他处引用 → 拒绝移动。
















#: 「物品上的主人照片」参考图取图优先级（2026-10-06 T02「物品主人形象」链路）。
#: ⚠️ **刻意排除 base.png**：角色 base 图是「正/侧/背三视图整图」，贴进物品照片区域
#:    会把 3 个人物一起搬过去（T02 明确要求排除）。正面档 front.png 才是单人全身图，
#:    half.png（半身胸像）作次选。


# ---- 项目助手已下沉到 routes/projects.py（2026-10-08 第五批）----
from routes.projects import _collect_project_cast_images, _cover_prompt_from_outline, _move_with_retry, _project_cover_path  # noqa: F401  再导出






# ---- _body / _resolve_continuity_key 已下沉到 routes/_shared.py（2026-10-08）----
from routes._shared import _body, _novel_key, _resolve_continuity_key  # noqa: F401  再导出


# ==========================================================================
# 集级产物目录（多集自动生产的必要条件）
# --------------------------------------------------------------------------
# 背景：配音文件名早已含集号（ep{NN}_shot{NN}_角色.wav），但分镜图 / 尾帧 / 视频
# 历史上按 shot_NN 平铺存放，**多集生产会互相覆盖**——第 2 集的分镜图会覆盖第 1 集的。
# 手动流程一次只做一集，问题不暴露；24 小时托管必须解决。
#
# 策略：第 1 集沿用平铺目录（现有项目、现有前端、现有数据完全不受影响），
#       第 2 集起写入 epNN/ 子目录。读取时优先集目录、回落平铺目录，兼容旧数据。
# ==========================================================================

# ---- 质检域跨域助手已下沉到 routes/_shared.py（2026-10-08 第八批）----
from routes._shared import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir, _qc_load_cfg  # noqa: F401  再导出






# ===== 项目管理（A：每部小说 = 一个独立项目） =====


# ===== 项目封面（项目中心卡片对外展示的第一张图） =====


# ===== 资产点击查看（B：角色 / 场景 / 物品 / 分镜详情） =====

_PROJECT_KIND_DIRS = {"characters": CHARACTERS_DIR, "items": ITEMS_DIR,
                      "scenes": SCENES_DIR, "storyboards": STORYBOARDS_DIR,
                      "videos": VIDEOS_DIR, "final": FINAL_DIR,
                      "upscale": UPSCALE_DIR, "dub": DUB_DIR, "qc": QC_DIR}


# ===================== 资产沉淀过程可视化（2026-10-06） =====================
# 需求：资产不是「一下就有的」，而是「抽取 → 写提示词 → 出图 → 质检（可能重画 N 次）
# → 沉淀教训 → 切分入库」这样一条流水线跑出来的。此前这些步骤散落在质检历史
# （QC_DIR/<项目>/asset_<名称>.json）、产物旁路元数据（*.meta.json）与教训库
# （output/lessons/lessons.jsonl）三处，用户在界面上只能看到「最后那张图」，
# 看不到「它被改了几次、为什么改、学到了什么」。
#
# 本接口把三处数据按资产聚合成一条**时间线**，供前端在资产详情里渲染。
# 纯只读：不触发任何生成，不写盘，读失败一律降级成空步骤（绝不 500）。


# ===== 页面（Vite SPA）=====
@app.route('/')
def index():
    """SPA 首页（Vite 构建产物）

    2026-10-11 恢复：搬迁 _resolve_static_dir 时，把它下面紧邻的这个视图整段误删，
    只留下悬空的 @app.route('/') 装饰器贴在 static_assets 上 ——
    Flask 于是把 / 注册到 static_assets，访问首页报
        TypeError: static_assets() missing 1 required positional argument: 'filename'
    现恢复原实现（与删除前逐字一致），并加 tests/test_route_integrity.py 守护。
    """
    return send_from_directory(_STATIC_DIR, 'index.html')


@app.route('/assets/<path:filename>')
def static_assets(filename):
    """服务 Vite 构建的静态资源"""
    return send_from_directory(os.path.join(_STATIC_DIR, 'assets'), filename)


@app.route('/vite.svg')
def vite_icon():
    """Vite favicon 回退（旧版本 index.html 仍可能引用，保留向后兼容）"""
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">⚡</text></svg>'


@app.route('/favicon.svg')
def favicon_svg():
    """站点图标：由 Vite 从 frontend/public/favicon.svg 复制到 static 根目录"""
    return send_from_directory(_STATIC_DIR, 'favicon.svg')


# ===== 状态 =====




# ===== P0-4 持久化任务队列查询 =====


# ===== P1-1 一致性校验（跨镜头角色一致性） =====

# 注：`_shot_num_key` 已收敛为 app/shot_key.norm_shot_key 的一行代理（见文件顶部），
# 定义不再落在此处 —— 全项目唯一的镜号归一化实现见 app/shot_key.py。


# ===== P2-3 成本与耗时看板 =====


# ==========================================================================
# 通用辅助：剧本读取 / 章节原文 / 关键帧目录
# ==========================================================================








# ==========================================================================
# P1-2 关键帧驱动视频模式
# ==========================================================================



# ==========================================================================
# P1-3 可视化分镜画布 + 单镜重跑
# ==========================================================================



@app.route('/api/storyboard/canvas/<path:project_name>', methods=['GET'])
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
            app.logger.warning(f"分镜 manifest 读取失败：{e}")

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
        app.logger.debug(f"一致性报告读取失败（画布将不含一致性分）：{e}")

    # 原文承载归属
    try:
        cover_map = _shot_coverage_map(script)
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"覆盖率归属计算失败：{e}")
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


@app.route('/api/storyboard/shot/reorder', methods=['POST'])
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


# ---- 蓝图共享助手已下沉到 routes/_shared.py（2026-10-08 解耦）----
from routes._shared import (  # noqa: F401  再导出：既有装饰器/调用零改动
    _autopilot_guard, _friendly_error, _prompt_memory_dead_count,
    _prompt_memory_used_total, _prompt_memory_view)


@app.route('/api/storyboard/retry-shot', methods=['POST'])
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












# ==========================================================================
# P2-1 / P2-2  NLE 导出（剪映草稿 / FCPXML / SRT / 帧序列）
# ==========================================================================


# ==========================================================================
# P1-4 / P2-4  引擎 Provider 与插件注册表（透明化，只读为主）
# ==========================================================================


# ==========================================================================
# ComfyUI 模型 / 插件扫描 + 手选模型
#
#  为什么要"扫描"而不是直接读模板：ComfyUI 某节点 combo 的合法值取决于模型在
#  磁盘上的**目录布局**（放进 diffusion_models/minimax-h3/ 后名字会带
#  `minimax-h3\` 前缀），而工作流模板里通常写死的是裸文件名。两边一旦不符，
#  ComfyUI 校验失败 → 该节点产出被丢弃（H3 视频就曾因此整段静默失败）。
#  所以这里以 ComfyUI 的 object_info 为**唯一权威来源**给出候选，由用户手选。
# ==========================================================================


# ==========================================================================
# P2-5 国际化
# ==========================================================================


# ===== 自动流水线第 1/7 步：剧本生成 =====

@app.route('/api/script/generate', methods=['POST'])
def api_generate_script():
    # P0-5 门禁：文本分析模型未配置 / 端点不可达 → 直接阻断，不给「静默跑进执行中」的机会
    _gate = _ai_gate_or_400("script")
    if _gate is not None:
        return _gate
    data = _body()
    theme = data.get('theme', '')
    episodes = data.get('episodes', 1)
    duration = data.get('duration', 60)
    style = data.get('style', '古风仙侠')
    style = _apply_project_settings(style, _safe_project(data.get('project_name') or (theme or '')[:20] or 'project'))

    if not theme:
        return jsonify({"error": "请提供主题"}), 400

    try:
        # 统一走前端「AI 设置 → 文本分析模型」的密钥（OpenAI 兼容，任意厂商），
        # 不再读环境变量 ANTHROPIC_API_KEY / 硬编码 CLAUDE_MODEL。
        client = _current_llm_client()
        script = script_gen.generate_script_with_client(
            client, theme=theme, episodes=episodes,
            duration_per_episode=duration, style=style
        )
        project_name = _safe_project(theme[:20])
        script_path = script_gen.save_script(script, project_name,
                                             provider="openai-compatible")
        script["metadata"]["script_path"] = script_path

        # S1：剧本质检接线（原为死代码——生产路径只调无质检的 generate_script，
        # 结构缺陷直接流入分镜/视频后才暴露）。这里在剧本落盘后跑一次 check_script，
        # 按 script_qc_ready 门控（与 image/video qc 同模式，开关关时 no-op、不报错），
        # 结果透出给前端如实回显「剧本是否已质检」。单次检测（非 generate_script_with_qc
        # 的 3 次重试+AI 判断重链路），止血优先、行为保守。
        qc_cfg = _qc_load_cfg()
        qc_result = {"skipped": True, "reason": "剧本质检未启用"}
        if qc_client.script_qc_ready(qc_cfg):
            qc_result = qc_client.check_script(
                script_data=script, style=style, target_duration=duration, cfg=qc_cfg)
            app.logger.info("剧本质检：passed=%s score=%s reason=%s",
                            qc_result.get("passed"), qc_result.get("score"),
                            qc_result.get("reason"))

        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "script_qc_active": qc_client.script_qc_ready(qc_cfg),
            "qc_result": qc_result,
        })
    except LLMError as e:
        # 未配置「文本分析模型」或密钥错误：给引导（400）而非裸 500，
        # 引导用户去 AI 设置配置 base_url / api_key / model。
        # _ai_guide_response 已返回 (jsonify, code) 元组，直接透传。
        app.logger.error(f"剧本生成 LLM 调用失败: {e}")
        return _ai_guide_response(str(e))
    except Exception as e:
        app.logger.error(f"生成剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


# ===== 第 3/7 步：资产生成（角色/物品/场景 + 多视角） =====



# ===================== 清空重做单个镜头 / 单个资产（总控 AI 工具后端） =====================
# 与 reset-episode 同一套安全原则：移入回收站（可恢复）、逐项容错、按镜号/资产名精确圈定。


# ===================== 场景九宫格机位预览（2026-10-01，对标 BigBanana 候选构图） =====================
# 场景 base 图 → 9 个机位各一张同场景变体 + 3x3 拼接预览（scene_grid.py）。
# 「应用」= 选中机位图升级为该场景新 base（旧 base 移入回收站），分镜参考图与
# 按机位出图自动沿用新视角。GPU 任务走统一闸门（与生产串行，不抢卡）。


# ===================== 分镜九宫格候选构图（2026-10-01，对标 BigBanana） =====================
# 一镜一次生成「3x3 九候选构图联系表」→ 选格裁切为正式分镜图。与 best-of-N 相比：
# 一次生成出 9 个机位变体，省时省卡；选格可由 QC 模型打分或用户手动指定。

@app.route('/api/storyboard/grid-candidates', methods=['POST'])
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
                                app.logger.info("[3D导演台] 九宫格 shot=%s 3D 构图基准网格已渲染：%s",
                                                shot.get("shot_id"), os.path.basename(_grid_sheet))
                                _grid_refs.insert(0, _grid_sheet)
                                _grid_labels.insert(0, "3D构图基准网格")
                                _grid_has_blocking = True
                except Exception as _3d_e:  # noqa: BLE001
                    app.logger.warning("[3D导演台] 九宫格 shot=%s 3D 基准渲染失败（降级为纯文字站位）：%s",
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
            app.logger.error("分镜九宫格候选构图失败：%s", e, exc_info=True)
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_grid_worker, daemon=True, name=task_id).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started"})


@app.route('/api/storyboard/grid-apply', methods=['POST'])
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
    app.logger.info("[shot-grid-apply] 项目=%s 集=%s 镜=%s 第 %d 格已应用（旧图 %d 项入回收站）",
                    project_name, episode_no, shot_key, cell, len(cleared))
    return jsonify({"success": True, "project": project_name, "shot_id": shot_key,
                    "cell": cell, "applied": dst,
                    "url": (f"/api/storyboards/file/{project_name}/"
                            f"{_sub + '/' if _sub else ''}shot_{seq:02d}.png"),
                    "cleared": cleared,
                    "hint": "选中格已裁切为该镜正式分镜图（人工定稿，未走 AI 质检）"})


# ===================== P2-2：RefMod 节点探测（Fizgig/MiniMaxH3Mod） =====================


@app.route('/api/assets/generate', methods=['POST'])
def api_generate_assets():
    """生成资产（角色/物品/场景，含多视角）"""
    # ⚠️ 这里**故意不设** AI 前置门禁：资产生成是「消费已产出的提示词 + ComfyUI 出图 +
    # 质检」的链路，全程不读 text/qc/chat 凭证（提示词由上游剧本步骤产出、随 assets 传入）。
    # 早前一版把门禁挂在这里，后果是「AI key 没配 → 连本来能出的图也一起被拦」，
    # 属于护栏误伤业务。凡是光跑 ComfyUI 就能完成的入口都不挂门禁。
    data = _body()
    asset_type = data.get('asset_type', '')  # character / item / scene
    # P2-T2：写盘路由统一走 _project_or_400（契约必填 project_name）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    assets = data.get('assets', [])
    # A-2 P0：断点续跑开关。默认 False → 已达标入库的资产跳过；显式传 true 强制重生成
    overwrite = bool(data.get('overwrite'))

    if asset_type not in ("character", "item", "scene"):
        return jsonify({"error": "asset_type 必须是 character/item/scene"}), 400
    if not assets:
        return jsonify({"error": "没有资产数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # B-12 P1-15：资产任务 ID 改用 uuid（G5 只改了分镜/视频/配音/混音，资产漏改），
    # 同秒并发请求不再互撞。
    task_id = f"{asset_type}_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": asset_type,
            "progress": 0, "total": len(assets), "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
        }

    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, assets, asset_type, project_name,
              data.get('style') or _project_style(project_name), overwrite)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "started"})


# ===================== 角色服装变体（衣柜，2026-10-02） =====================
# 目录约定：output/assets/characters/<项目>/<角色名>/outfits/<outfit_key>/，
# 内部文件布局与主设定目录相同（base.png + front/left/back/half.png，由整图本地
# 切分派生 —— 复用 _generate_asset_task 的「生成→质检→重试→切分」全链路，见其
# sub_dir 参数）。端点加在 api_generate_assets 旁边：同为「消费已产出提示词 +
# ComfyUI 出图 + 质检」链路，不读 AI 凭证，故同样不挂 AI 前置门禁。
# ⚠️ 零回归约束：所有新行为都以「服装变体目录存在」为前提 —— 目录不存在时查询
# 返回空数组、取图回落主设定图（fail-open，任何异常只 log 不抛）。

#: 服装变体子目录名（挂在角色主设定目录下）
#: 服装描述追加进提示词的标记（幂等判据，见 _append_outfit_prompt）
_OUTFIT_PROMPT_MARK = "；本套服装："
#: 服装档案文件名（生成发起时先落一份 outfit_key/desc 记录，查询端点回显描述用）
#: 变体档位（与主设定目录的切分产物同名，来自 sheet_split 链路）
_OUTFIT_VIEW_STEMS = ("front", "left", "back", "half")




def _append_outfit_prompt(base_prompt: str, outfit_desc: str) -> str:
    """把「；本套服装：{outfit_desc}」追加到角色提示词末尾（幂等）。

    幂等实现：服装段**只追加在串尾**，故追加前把已有的尾段剥掉再接新段 ——
    同一 desc 重复追加结果不变（不重复），desc 改动时旧描述被替换而非叠两段。
    """
    base = str(base_prompt or "").strip()
    desc = str(outfit_desc or "").strip()
    if not desc:
        return base
    base = re.sub(r"；本套服装：.*$", "", base).strip()
    if base:
        return f"{base}{_OUTFIT_PROMPT_MARK}{desc}"
    return f"本套服装：{desc}"








def _outfit_desc_of(outfit_dir: str) -> str:
    """该服装变体的描述：outfit.json 档案优先，回落 base.png.meta.json
    提示词里的「；本套服装：…」尾段。任何失败返回 ''（查询列表不因此报错）。"""
    try:
        rec_path = os.path.join(outfit_dir, _OUTFIT_RECORD_FILE)
        if os.path.isfile(rec_path):
            with open(rec_path, "r", encoding="utf-8") as f:
                desc = str((json.load(f) or {}).get("desc") or "").strip()
            if desc:
                return desc
    except Exception as e:  # noqa: BLE001
        app.logger.debug("服装档案读取失败（回落 meta sidecar）：%s", e)
    try:
        meta_path = os.path.join(outfit_dir, "base.png.meta.json")
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                prompt = str((json.load(f) or {}).get("prompt") or "")
            _m = re.search(r"；本套服装：(.+)$", prompt)
            if _m:
                return _m.group(1).strip()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("服装 meta 回读失败（忽略）：%s", e)
    return ""


# ===================== 上传角色形象图 → 三视图（2026-10-06） =====================
# 需求：用户手里已有角色画稿（自己画的 / 外包出的 / 别处满意的一张），希望直接
# 用这张图当角色的「设定图基准」，而不是让模型按提示词重新抽一次外形。
#
# 关键取舍（**本地切分，不是参考图重绘**）：
#   · 上传图直接落 `base.png`（= 角色设定整图），再走 `sheet_split` 现有链路切出
#     front/left/back/half —— 零 GPU、零质检、秒出，且**人物外形 100% 保留**
#     （模型重绘一定会改写脸/发型，这正是用户上传画稿要避免的）。
#   · 因此上传图必须**本身就已按分档版式排好**（上排正面/左侧/背面三全身，
#     下排一格正面半身）。版式不符 → `SheetSplitError` → 明确报错并回滚，
#     绝不落一张切不动的 base.png 污染下游（下游会拿它当参考图）。
#   · 上传即视为**已定稿**，不做图片质检（`view_gate` 记 skipped）：用户上传
#     自己的画稿，质检判「不合格」既无意义又会把用户的东西删掉。
#
# 与服装变体（outfits/<key>）的关系：同一张上传图可以挂到主设定，也可以挂到
# 某个服装变体档；由 `outfit_key` 决定落位（空 = 主设定）。
# 幂等：默认已存在 base.png 即跳过；overwrite=true 强制覆盖。

#: 上传形象图允许的扩展名（与 project_store._views_of 的图片白名单同口径）
_UPLOAD_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


@app.route('/api/assets/character/upload-sheet', methods=['POST'])
def api_character_upload_sheet():
    """上传角色形象图 → 落 base.png → 本地切分三视图（零 GPU）。

    form-data: file（图片，必填）, project_name, character, outfit_key?（空=主设定）,
               overwrite?（"1"/"true" 强制覆盖）

    返回：{success, character, base, views:{front,left,back,half}, derive_error?}
      · derive_error 非空 = 图已落 base.png 但**版式不符未能切分**（下游会回落
        整图，属可用状态），此时 success 仍为 True 但带 `derive_ok: false` 提示。
      · 只有「图本身不可读 / 版式不符且 overwrite 已覆盖旧图」才 4xx/回滚。
    """
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False,
                        "error": "未收到图片，请通过 file 字段上传"}), 400
    f = files[0]
    project_name, err = _project_or_400(
        (request.form.get('project_name') or request.args.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (request.form.get('character') or request.args.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(
        request.form.get('outfit_key') or request.args.get('outfit_key'))
    _ow_raw = (request.form.get('overwrite') or request.args.get('overwrite') or '').strip()
    # 宽容布尔（与 qc_client._as_bool 同口径）：表单/query 传 "1"/"true"/"on"/"yes" 都算真
    overwrite = _ow_raw.lower() not in ("", "0", "false", "no", "off")

    # 角色名是路径段，与 api_character_outfit_generate 同一守卫口径
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    # outfit_key 传了但非法（非空且清洗后为空）→ 明确拒绝，不静默当主设定
    _ow_in = (request.form.get('outfit_key') or request.args.get('outfit_key') or '').strip()
    if _ow_in and not outfit_key:
        return jsonify({"success": False,
                        "error": "outfit_key 非法（≤40 字符，剔除 \\ / : * ? \" < > |）"}), 400

    raw_name = _safe_upload_name(f.filename)
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in _UPLOAD_IMAGE_EXTS:
        return jsonify({"success": False,
                        "error": f"不支持的图片格式 {ext or '（无扩展名）'}；"
                                 f"支持 {'、'.join(_UPLOAD_IMAGE_EXTS)}"}), 400

    asset_dir = (_character_outfit_dir(project_name, character, outfit_key) if outfit_key
                 else os.path.join(CHARACTERS_DIR, project_name, character))
    base_dst = os.path.join(asset_dir, "base.png")
    if not overwrite and os.path.isfile(base_dst) and os.path.getsize(base_dst) > 0:
        return jsonify({"success": True, "skipped": True, "character": character,
                        "outfit_key": outfit_key, "base": base_dst,
                        "message": "该角色已有设定图（base.png 已就绪）；"
                                   "如需替换请带 overwrite=true"})

    # 暂存上传件 → 校验可解码 → 统一转 PNG 落 base.png
    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(
        UPLOAD_TMP_DIR,
        f"sheet_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}")
    _old_backup = ""
    try:
        f.save(tmp_path)
        try:
            from PIL import Image
            with Image.open(tmp_path) as _im:
                _w, _h = _im.size
                _im.convert("RGB")
        except Exception as _ie:  # noqa: BLE001 不可解码的图绝不能入库
            return jsonify({"success": False,
                            "error": f"图片无法读取或已损坏：{type(_ie).__name__} {_ie}"}), 400

        os.makedirs(asset_dir, exist_ok=True)
        # overwrite 时先把旧 base 挪走做回滚点：切分失败要能把旧状复原
        if os.path.isfile(base_dst):
            _old_backup = base_dst + ".preupload.bak"
            try:
                shutil.copy2(base_dst, _old_backup)
            except OSError as _be:
                app.logger.warning("上传形象图：旧 base.png 备份失败（忽略）：%s", _be)
                _old_backup = ""
        with Image.open(tmp_path) as _im:
            _im.convert("RGB").save(base_dst, format="PNG")
        app.logger.info("[上传形象图] %s/%s%s ← %s（%dx%d）",
                        project_name, character, f" ({outfit_key})" if outfit_key else "",
                        raw_name, _w, _h)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ---- 2026-10-02 用户指定：角色图**不裁剪** → 不再切分三视图 ----
    # 角色设定图已改为英文四区 character sheet（见 comfyui_client._CHARACTER_SHEET_EN_LAYOUT），
    # 四区不对称布局无法切分 → 上传图直接落 base.png 整图，下游取整图。
    # 并清掉旧视角图（front/left/back/half），避免 _ASSET_IMG_PRIORITY 取到旧单视角。
    views: dict = {}
    try:
        sheet_split.prune_stale_views(
            asset_dir, keep=(), known=ASSET_VIEW_STEMS, logger=app.logger)
    except Exception as _pe:  # noqa: BLE001
        app.logger.warning("上传形象图：清理陈旧视角失败（忽略）：%s", _pe)

    if _old_backup:
        try:
            os.remove(_old_backup)
        except OSError:
            pass

    # 旁路元数据：显式标注「用户上传」，与模型生成的资产可区分、可追溯
    _view_gate = {"accept": True, "blocked": False, "skipped": True,
                  "label": "用户上传（未质检）",
                  "reason": "用户上传的定稿形象图，不做图片质检", "critical_issues": []}
    try:
        _write_artifact_meta(
            base_dst, kind="asset_base", project_name=project_name,
            seed=None, prompt="", workflow_key=None, qc=_view_gate,
            asset_name=character,
            extra={"asset_type": "character", "source": "user_upload",
                   "original_filename": raw_name, "outfit_key": outfit_key or None})
    except Exception as _me:  # noqa: BLE001
        app.logger.warning("上传形象图：元数据写入失败（忽略）：%s", _me)

    return jsonify({"success": True, "derive_ok": True, "skipped": False,
                    "character": character, "outfit_key": outfit_key,
                    "base": base_dst,
                    "views": {},
                    "view_files": {},
                    "layout_hint": "不裁剪：保留整图（character sheet 四区）",
                    "message": "已上传（不裁剪：直接使用整张 character sheet 设定图）"})


@app.route('/api/assets/character/outfit', methods=['POST'])
def api_character_outfit_generate():
    """生成角色服装变体（衣柜）。

    body = {project_name, character(角色名), outfit_key, outfit_desc, style?, overwrite?}
    返回 {task_id, status:"started"}（与 api_generate_assets 同构，进度轮询
    /api/generation/status/<task_id>）。已存在同 outfit_key 且 base.png 非空 →
    默认跳过（A-2 overwrite 语义，overwrite=true 强制重画）。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(data.get('outfit_key'))
    outfit_desc = str(data.get('outfit_desc') or '').strip()
    overwrite = bool(data.get('overwrite'))
    # 角色名是路径段，与 api_autopilot_reset_asset 同一守卫口径
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    if not outfit_key:
        return jsonify({"success": False,
                        "error": "outfit_key 不能为空（≤40 字符，剔除 \\ / : * ? \" < > |）"}), 400
    if not outfit_desc:
        return jsonify({"success": False, "error": "outfit_desc（服装描述）不能为空"}), 400

    outfit_dir = _character_outfit_dir(project_name, character, outfit_key)
    # A-2 断点续跑语义：已有同 key 且 base.png 非空 → 默认跳过（幂等入口）
    _base_png = os.path.join(outfit_dir, "base.png")
    if not overwrite and os.path.isfile(_base_png) and os.path.getsize(_base_png) > 0:
        return jsonify({"success": True, "skipped": True, "task_id": "",
                        "outfit_key": outfit_key, "character": character,
                        "message": "该服装变体已存在（base.png 已就绪）；如需重画请带 overwrite=true"})

    # 角色基础设定（剧本优先，回落主设定 meta）→ 追加服装描述（幂等）
    _base_prompt = _character_base_prompt(project_name, character)
    _merged_prompt = _append_outfit_prompt(_base_prompt, outfit_desc)
    asset = _find_script_character(project_name, character)
    asset.update({
        "name": character,
        "reference_prompt_zh": _merged_prompt,
        "prompt_zh": _merged_prompt,
    })
    if not str(asset.get("appearance") or "").strip():
        # 剧本里没有 appearance 时把合并提示词兜进去，保证 qc_desc / 性别判据有料可用
        asset["appearance"] = _merged_prompt

    # 服装档案：先落 outfit.json（查询端点回显 desc 用；写失败不影响生成 ——
    # ready 判据看 base.png，desc 还有 meta sidecar 兜底）
    try:
        os.makedirs(outfit_dir, exist_ok=True)
        atomic_write_json(os.path.join(outfit_dir, _OUTFIT_RECORD_FILE), {
            "outfit_key": outfit_key,
            "desc": outfit_desc,
            "character": character,
            "project": project_name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as _oe:  # noqa: BLE001
        app.logger.warning("服装变体档案写入失败（不影响生成）：%s", _oe)

    task_id = f"outfit_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": "character",
            "progress": 0, "total": 1, "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
            "project_name": project_name, "step": "asset_outfit",
            "character": character, "outfit_key": outfit_key,
        }
    # 线程 + 状态登记照抄 api_generate_assets（裸线程，资产链路不进 GPU 闸门）；
    # 生成→质检→重试→切分全链路由 _generate_asset_task 承担，变体经 sub_dir 落位
    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, [asset], "character", project_name,
              data.get('style') or _project_style(project_name), overwrite,
              os.path.join(_OUTFITS_DIRNAME, outfit_key))
    )
    thread.daemon = True
    thread.start()
    app.logger.info("[服装变体] 项目=%s 角色=%s 服装=%s（overwrite=%s）任务=%s 已启动",
                    project_name, character, outfit_key, overwrite, task_id)
    return jsonify({"task_id": task_id, "status": "started",
                    "outfit_key": outfit_key, "character": character})






@app.route('/api/assets/character/outfits', methods=['GET'])
def api_character_outfits_list():
    """列出角色服装变体（衣柜）：query = project_name, character。

    返回 [{outfit_key, desc, ready(base.png 存在且>0字节),
    views:{front/left/back/half 是否存在}}]。outfits 目录不存在 / 任何读取异常
    → 空数组（fail-open，绝不抛错）。
    """
    project_name = _safe_project((request.args.get('project_name') or '').strip())
    character = (request.args.get('character') or '').strip()
    if not project_name or not character or '/' in character or '\\' in character \
            or character in ('.', '..'):
        return jsonify({"success": False, "error": "project_name 与 character 必填",
                        "outfits": []}), 400
    outfits_root = _character_outfit_dir(project_name, character)
    out = []
    if not os.path.isdir(outfits_root):
        # 目录不存在 = 该角色还没做过服装变体（正常态，不是错误）
        return jsonify({"success": True, "outfits": out})
    try:
        # 纵深防御（安全复查 2026-10-02）：listdir 条目本不可能携带路径分隔符
        # （listdir 不返回 ./..，文件名也无法含 \ /），此处仍显式校验 realpath
        # 未越出 outfits_root，阻断符号链接等非常规文件系统状态造成的目录逃逸。
        _root_real = os.path.realpath(outfits_root)
        for _dir_name in sorted(os.listdir(outfits_root)):
            _od = os.path.join(outfits_root, _dir_name)
            if not os.path.isdir(_od):
                continue
            if not os.path.realpath(_od).startswith(_root_real + os.sep):
                app.logger.warning("[服装变体] 异常目录项已跳过（越界防护）：%s", _dir_name)
                continue
            _base_png = os.path.join(_od, "base.png")
            _ready = os.path.isfile(_base_png) and os.path.getsize(_base_png) > 0
            _views = {}
            for _stem in _OUTFIT_VIEW_STEMS:
                _vp = os.path.join(_od, f"{_stem}.png")
                _views[_stem] = os.path.isfile(_vp) and os.path.getsize(_vp) > 0
            out.append({
                "outfit_key": _dir_name,
                "desc": _outfit_desc_of(_od),
                "ready": _ready,
                "views": _views,
            })
    except Exception as e:  # noqa: BLE001  目录枚举失败按空数组处理（fail-open）
        app.logger.warning("服装变体列表读取失败（返回空数组）：%s", e)
        out = []
    return jsonify({"success": True, "outfits": out})


# ===== 第 4/7 步：分镜图生成 =====







# ---- 场景名容错匹配（2026-09-29）------------------------------------------- #
# 背景：场景资产名（``scenes[].name``）与镜头引用的场景名（``shot.location``）
#   是**两次独立的 LLM 生成**，用词不保证逐字一致（全角/半角括号、书名号、
#   空格、「铜铃巷」vs「铜铃巷（夜）」）。旧实现统一写成::
#
#       scene_name = loc if loc in scene_idx else None
#
#   —— **不匹配就静默置 None**：场景参考图直接不注入、不打任何日志。画面里
#   的建筑形制、光位只能靠模型自行想象，是「背景不一致」类质检缺陷的隐蔽
#   来源；而且这段判据在 5 处取用点各写了一遍（分镜参考图 / 重跑切段 /
#   H3 视频段 / 图片质检锚点 / 连续性判定），改一处漏四处的风险很高。
#   这里收敛为**一个匹配器**，并确立「宁可告警、不可静默」。
#
# ⚠️ 降级方向**越靠后越保守**：误配（把 A 场景的图给 B）比丢图更糟——
#   丢图只是「没有锚点」，误配会主动把**错误背景**焊进画面。故最后一级
#   强制要求**唯一命中**，多候选一律放弃。

#: 全角 ASCII（！-～）→ 半角；另加全角空格。只动标点/空白，不动汉字。
#: ⚠️ 2026-09-29：归一化与匹配逻辑已收敛到 ``asset_name_match``（三类资产共用），
#: 下面保留同名函数作为**薄封装**——调用点与既有探针无需改动，实现只有一份。
_SCENE_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_SCENE_FULLWIDTH_MAP[0x3000] = 0x20
#: 装饰性符号（引号/书名号/间隔号）：本身无语义，剥离后仍指向同一场景。
_SCENE_DECOR_CHARS = ("《", "》", "「", "」", "『", "』", "\"", "'",
                      "“", "”", "‘", "’", "·", "•")




















#: 取「半身档」角色参考图的景别集合（近景类）。
#: 为什么是这些：角色立绘是**全身**，而近景类镜头要求参考图与目标取景同向 ——
#: 全身立绘会把模型往全景方向拉（2026-09-25 实测 shot_24：近景规定、出图近全身）。
#: ⚠️ 景别取值来自 config.SHOT_TYPES（唯一权威表）；新增近景类景别时要同步加进来，
#:    否则该景别会静默走全身档、重新引入「画幅对抗」。
































# 特写镜头参考图策略 ---------------------------------------------------------
# 根因：分镜生成用的 Qwen Edit 以参考图构图为强先验。全身角色图 + 道具图作参考时，
# 模型倾向输出中全景，并把道具明确画在人物手中，导致「特写 + 道具已收起」类镜头反复不达标。
# 对策（仅对 camera 含「特写」的镜头生效）：
#   1) 角色参考图换为该角色全身视图的「头部特写裁剪图」（现裁现用，不改动原始资产）；
#   2) 剔除道具参考图（特写中道具应已入袖、不应出镜）；
#   3) 场景参考图**整条剔除**（见下方实现：只保留「主角色 / 次角色」两类）。
#      文件头注释曾写「保留但降级为仅色调参考」，与实现不符 —— 实测口径是剔除；
#      2026-09-29 修正注释与实现对齐。特写是「只拍局部」，而场景基准图是全景
#      View，留作基调参考仍会把模型拉回中全景（见上方实测记录）。
# 角色资产 5 个视角均为「右手握笛」形象（道具已被画进人物），直接作参考会让模型把笛子
# 画进画面，与「道具已收起」类动作冲突；此处只取角色图顶部「头部条带」作锚点，
# 从参考层面切断手部/道具先验。
#
# ⚠️ 2026-09-24 口径变化：角色参考图（front.png）已从「三视图整图」改为
#    「从整图切分出的**单人格**并居中贴回同尺寸方底」（见 app/sheet_split.py）。
#    切分后人物恰好落在画面**水平中部约 31%** 宽（x≈0.34~0.66），
#    故下面的 x 区间 (0.32, 0.68) 现在正好框住这张单人格的头部 —— 语义与常量取值一致，
#    **不需要再改**。反过来，若哪天把 front.png 换回三视图整图，这个区间会落到
#    **中格（左侧面）** 的头上，取到侧脸锚点，需同步调整为最左格。








# --------------------------------------------------------------------------- #
# 分镜参考图「统一画幅」（2026-09-24）
#   ⚠️ 为什么必须做：分镜模板 `分镜生成_Qwen21.json` 是「参考图编辑」型，**没有尺寸
#      节点** → style_kit.apply_latent_size 返回空 → 输出画幅**继承第一张参考图**。
#      而参考图随镜头而变：建立镜（characters_in_shot 为空）只有场景图（资产内置
#      16:9 → 960×544 横屏）；有角色的镜头第一张是角色图（1:1 → 736×736 方形）；
#      特写镜头第一张是头部裁剪条带（更小）→ **同一集分镜画幅在两三种尺寸间跳变**，
#      与项目画幅（9:16 竖屏 544×960）不符，成片拼接会出现黑边 / 拉伸。
#      这里在送进工作流前把每张参考图 cover 到目标画幅，使输出画幅恒定。
# --------------------------------------------------------------------------- #

#: 统一画幅结果的进程内缓存：键 = (绝对路径, mtime_ns, 目标宽, 目标高) → 处理后路径。
#: 同一批分镜里同一张参考图会出现多次（84 镜共用寥寥数张），缓存避免逐镜重复裁剪。








# 注：本处原为 `_shot_seq(shot_id, fallback)`。已收敛为 app/shot_key.shot_seq 的
# 一行代理（见文件顶部），全项目唯一的镜号归一化实现见 app/shot_key.py。

#: 3D 站位基准图渲染的进程级串行锁（2026-10-06 分镜「滚动预取」改造引入）。
#: ⚠️ te_3d_render.render_blocking **不可并发重入**：其回传静态服务是进程级单例
#:    且只有一个 inbox 槽位（`srv.inbox = box`，后到者覆盖先到者 → 回传错投/超时），
#:    无头浏览器的 user-data-dir 也是共享的。分镜工作流预取线程与主循环都可能进入
#:    渲染，这里统一串行化；锁内调用自带「按计划哈希」的 PNG 缓存，同镜重复调用
#:    直接命中不重渲，串行化不损失流水线收益（渲染与 GPU 出图本就并行）。


# ===================== 九宫格「逐格写死」规划（2026-10-07，用户范本范式） =====================
# 两段式第一步：文本 LLM 按 storyboard_grid_plan 模板把一个镜头拆成 9 个关键帧分镜；
# 第二步（渲染）在 comfyui_client.build_shot_grid_keyframes_prompt 里走 storyboard_grid_main
# 模板产出中文逐格版九宫格提示词。规划拿不到 → 调用方传 panel_plans=None → 回落英文版路径。







#: 分镜「结构性缺陷」词表（2026-10-08）。
#: 这几类缺陷意味着**九宫格拆解本身失败** —— 同一格重复、中途换场景、左右手镜像 ——
#: 图不可用，**不参与 storyboard_soft_qc 软放行**，必须按硬阻断重跑。
#: 实测：shot_04 格3/4/6/9 四格近乎相同，质检已判「九宫格面板雷同」，却被软放行入库。
#: ⚠️ 与 qc_client.IMAGE_CRITICAL_KEYWORDS 里同名字段刻意保持一致（同一套判据）。














@app.route('/api/storyboards/generate', methods=['POST'])
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


@app.route('/api/storyboards/manifest/<path:project_name>')
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


@app.route('/api/storyboards/file/<path:filename>')
def api_storyboard_file(filename):
    """提供分镜图文件访问"""
    return _serve_safe(STORYBOARDS_DIR, filename)


@app.route('/api/storyboards/scratch/<path:project_name>/<path:filename>')
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


# ===== 第 5/7 步：视频生成（整集） =====





# 注：本处原为 `_norm_shot_key(key)`。已收敛为 app/shot_key.norm_shot_key 的一行代理
# （见文件顶部），全项目唯一的镜号归一化实现见 app/shot_key.py。










# ===== 第 7/7 步：成片合成 =====


# ===== 静态资产访问 =====

@app.route('/api/assets/<path:filename>')
def api_asset_file(filename):
    """提供资产文件访问

    ⭐ 2026-10-05 资产自动刷新：资产图会被**原地覆盖重生成**（路径不变），必须让浏览器
    每次回源校验，否则轮询拿到新 JSON 后 <img> 仍显示旧缓存图，用户以为「必须手动刷新」。
    与前端 ?v=<mtime> 双重保险：前端换 URL 触发重挂（主），本处 no-cache 兜底回源。
    """
    resp = _serve_safe(os.path.join(PROJECT_OUTPUT_DIR, "assets"), filename)
    try:
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    except Exception:  # noqa: BLE001  abort 响应(403/404) 无 headers，忽略
        pass
    return resp




# ===== 视频水印（C 项：可配置、默认关闭、支持「全视频移动」） =====
# 说明：图片生成链路不添加任何水印（C 项⑧）；本区块只处理视频后处理，不改 ComfyUI 工作流。

# ---- AI 助手已下沉到 routes/ai.py（2026-10-08 第六批）----
from routes.ai import LLM_NOT_CONFIGURED_GUIDE_MAP, _ai_config_view, _ai_credentials_verify, _chat_project, _chat_state, _save_ai_module, _wm_load_cfg  # noqa: F401  再导出


# ===== 视频超分（FlashVSR 真实实现，成片/片段 → 高分辨率） =====


@app.route('/api/script/fallback', methods=['POST'])
def api_fallback_script():
    """一键加载本地兜底剧本（免 API Key 演示）"""
    try:
        script = script_gen.load_fallback_script()
        if not script:
            return jsonify({"error": "未找到本地兜底剧本（output/scripts/剑心初醒_兼容版.json）"}), 404
        data = request.json or {}
        project_name = _safe_project(data.get('project_name') or script.get("title", "fallback"))
        script_path = script_gen.save_script(script, project_name)
        script.setdefault("metadata", {})["script_path"] = script_path
        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "fallback": True
        })
    except Exception as e:
        app.logger.error(f"加载兜底剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


# =====================================================================
# 新增模块 A：自定义 LLM API 配置
# =====================================================================

app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 单次上传上限 200MB


# ---- 小说库/预检助手已下沉到 routes/_shared.py（2026-10-08 第四批）----
from routes._shared import (  # noqa: F401  再导出
    AI_MODULE_LABEL, EPISODE_BATCH_LIMIT, UPLOAD_TMP_DIR, _ai_client_for_module,
    _ai_guide_response,
    _current_llm_client, _episode_units_for_chapters, _estimate_subchunks,
    _novels_stats, _optional_llm_client, _resolve_novel_project, _safe_upload_name)


# =====================================================================
# AI 设置：文本分析 / 质检 / 对话总控 三个独立模块
# =====================================================================


# （历史说明）保存 qc 模块曾靠 qc_client.set_endpoint 把密钥从 ai.qc 槽 best-effort 复制到
# qc 槽，任何绕过该路由的修改都会双槽漂移 → 401。现已由 ai_credentials_db（tasks.db）
# 单一事实源取代：qc_client.load_config 直接读 DB 的 qc 模块，旧桥接退役。


# ---- 兼容旧接口：/api/llm/config 系列（等价于「文本分析模型」模块） ----


# =====================================================================
# 提示词模板管理（2026-10-07，借鉴 Moha 的 prompts/ 设计）
# 出厂模板在 app/prompts/*.txt；用户覆盖在 PROJECT_DATA_DIR/prompt_overrides/。
# 全局配置、不挂项目（与 /api/ai/config 同级）。注册表与加载逻辑见 app/prompt_templates.py。
# =====================================================================


# =====================================================================
# 字幕烧录回读校对（2026-10-06）：caption 烧进成片后，用本地 whisper.cpp 把
# 成片音轨转写回来，与剧本应烧字幕做相似度比对（环境三件套：whisper-cli /
# ggml 语言模型 / ffmpeg，缺一不可、缺哪件 status 如实报哪件）。
# 实现见 app/caption_verify.py；env 覆盖：MJSCXT_WHISPER_EXE / MJSCXT_WHISPER_MODEL。
# =====================================================================

import caption_verify  # noqa: E402  路由区就近导入（与上方提示词模板区同级的功能区，便于整体回滚）


# =====================================================================
# AI 对话（创作总控）：多轮对话敲定创作设定 →「应用设定」落盘
# =====================================================================

# 归档根目录注入：ai_chat.load_all_messages 未显式传 root 时用它（见 ai_chat.set_archive_root）
ai_chat.set_archive_root(os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH)))


# =====================================================================
# 新增模块 C：图片 / 视频 AI 质检（可开关、不达标自动重生成）
# =====================================================================


















#: 优化器「思考过程」泄漏特征（2026-10-06 分镜图事故）。
#: 推理型模型会把中文推敲过程当正文返回；命中即整条弃用，回落原提示词 ——
#: 绝不把「关于提示词的元讨论」送进图片模型。

































# =====================================================================
# 新增模块 B：小说上传与解析
# =====================================================================


# =====================================================================
# 新增模块 C2：按章节分集生成（每章一集，独立落盘 output/scripts/<小说名>/第N集.json）
# =====================================================================


# 注：`_episode_map(novel_meta, project_ref)`（章节序号 → 已生成剧集）已移除。
# 它的键是 `chapter_index`，一章拆多集时只能保留最后一集（静默丢数据），
# 且与 `_episode_units_for_chapters` 的「集号」口径冲突。新代码一律用
# 「集号 → 产物」索引（见 api_novel_chapters 里的 `_gen_by_ep`）。


# ===================== 文学剧本层（两段式生产 ①：人审层，2026-10-03） =====================



@app.route('/api/novels/<novel_id>/screenplay/generate', methods=['POST'])
def api_novel_screenplay_generate(novel_id):
    """生成某章的文学剧本（两段式生产 ①，异步）。body: {chapter, project_id/project_name?, style?, episode_no?}"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法生成文学剧本")
    data = request.json or {}
    chapters = meta.get("chapters") or []
    try:
        ch_idx = int(data.get("chapter") or 1)
    except (TypeError, ValueError):
        ch_idx = 1
    chapter = next((c for c in chapters if int(c.get("index") or 0) == ch_idx), None)
    if chapter is None:
        return jsonify({"success": False, "error": f"找不到章节 {ch_idx}"}), 404
    proj = _resolve_novel_project(data, meta)
    key = _novel_key(meta, proj["dir_key"])
    try:
        episode_no = max(1, int(data.get("episode_no") or ch_idx))
    except (TypeError, ValueError):
        episode_no = ch_idx
    style = (data.get("style") or _project_style(proj["dir_key"]) or "3D动漫渲染")
    task_id = f"screenplay_{novel_id}_{episode_no}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {"status": "running", "progress": 2,
                                     "phase": "prepare", "message": "准备章节正文…",
                                     "novel_id": novel_id, "project_key": key}
    threading.Thread(target=_screenplay_worker,
                     args=(task_id, meta, chapter, key, style, episode_no),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "project_key": key, "episode_no": episode_no})


@app.route('/api/novels/<novel_id>/screenplay/<int:episode_no>', methods=['GET'])
def api_novel_screenplay_get(novel_id, episode_no):
    """读取已生成的文学剧本（Markdown）。?project= 指定项目键（缺省按小说找项目）。"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project') or '').strip()
    rec = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, rec["dir_key"] if rec else None)
    path = novel_screenplay.screenplay_path(key, episode_no)
    md = novel_screenplay.load_screenplay(key, episode_no) if os.path.isfile(path) else ""
    return jsonify({"success": True, "exists": bool(md), "markdown": md,
                    "path": path, "project_key": key, "episode_no": int(episode_no)})




@app.route('/api/novels/<novel_id>/episodes/generate', methods=['POST'])
def api_novel_episodes_generate(novel_id):
    """按章节分集生成：单章生成 / 批量生成多集（每章一集）

    body: {chapters:[1,2,3] | start:1,end:3, style, target_shots, overwrite}
    """
    try:
        # 生成剧本前先做章节目录体检（LLM 判断真章节，结果缓存；失败退回规则折叠）
        meta = ensure_chapter_structure(NOVELS_DIR, novel_id, _optional_llm_client())
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法按章生成剧本")

    all_chapters = meta.get("chapters") or []
    if not all_chapters:
        return jsonify({"success": False,
                        "error": "该小说未识别到章节标记，无法按章分集；请改用「AI 转成剧本」整本处理"}), 400

    data = request.json or {}
    style = (data.get('style') or '3D动漫渲染').strip() or '3D动漫渲染'
    # ⭐ 2026-10-10：0 = 不预设镜数（由原文信息密度决定）；显式传值仍 clamp 到 4~40。
    try:
        _ts_raw = int(data.get('target_shots')
                      if data.get('target_shots') not in (None, '') else NOVEL_DEFAULT_SHOTS)
    except (TypeError, ValueError):
        _ts_raw = NOVEL_DEFAULT_SHOTS
    target_shots = 0 if _ts_raw <= 0 else max(4, min(_ts_raw, 40))
    overwrite = bool(data.get('overwrite'))
    style = _apply_project_settings(style, data.get('project_name') or novel_id)

    by_index = {}
    for c in all_chapters:
        by_index[int(c.get("index") or 0)] = c

    requested = data.get('chapters')
    if isinstance(requested, (str, int)):
        requested = [requested]
    picks = []
    if isinstance(requested, list) and requested:
        for x in requested:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if xi in by_index and xi not in [p.get("index") for p in picks]:
                picks.append(by_index[xi])
    else:
        start = data.get('start')
        end = data.get('end')
        try:
            s = int(start) if start is not None else None
            e = int(end) if end is not None else None
        except (TypeError, ValueError):
            s = e = None
        if s is not None or e is not None:
            lo = s if s is not None else 1
            hi = e if e is not None else max(by_index)
            picks = [c for idx, c in sorted(by_index.items()) if lo <= idx <= hi]

    if not picks:
        return jsonify({"success": False, "error": "未选择有效章节（chapters 或 start/end 至少提供一项）"}), 400

    # 空壳章节防线（2026-10-02）：去掉标题行后几乎没有正文的条目绝不是可拍摄内容。
    # 曾经拿 11 个字的「第一卷：魔性不改」跑完整条流水线：模型凭空编了 8 个镜头
    # （「魔性不改 / 大道无情 / 唯我独尊」这类自造口号），原文台词一句没用，
    # 整半章内容丢失，而覆盖率检查还报 100%（没有正文单元可核对 → 空集恒真）。
    # 宁可在这里明确挡下并说清原因，也不产出整集幻觉剧本。
    try:
        _novel_text = read_novel_text(NOVELS_DIR, novel_id) or ""
    except Exception as e:  # noqa: BLE001
        app.logger.warning("空壳章节防线：正文读取失败（跳过检查）：%s", e)
        _novel_text = ""
    if _novel_text and len(_novel_text) > 5000:
        _shells = []
        for _c in picks:
            try:
                _body = chapter_body_chars(_novel_text, _c)
            except Exception:  # noqa: BLE001
                _body = 0
            if _body < 120:
                _shells.append("%s（%d 字）" % (_c.get("title") or _c.get("index"), _body))
        if _shells:
            return jsonify({
                "success": False,
                "error": ("下列章节去掉标题行后几乎没有正文，像是卷/分部标题而不是正文，"
                          "不能据此生成剧本：" + "、".join(_shells[:5]) +
                          "。请改选其后的正文章节（本书一节正文通常约 3000 字）。"),
                "shell_chapters": _shells,
            }), 400
    if len(picks) > EPISODE_BATCH_LIMIT:
        return jsonify({"success": False,
                        "error": f"单次批量最多 {EPISODE_BATCH_LIMIT} 集，本次选择了 {len(picks)} 集；请缩小范围"}), 400

    picks.sort(key=lambda c: int(c.get("index") or 0))
    # A：绑定/自动建立该项目，剧本与后续产物全部落在该项目目录
    proj = _resolve_novel_project(data, meta)
    key = _novel_key(meta, proj["dir_key"])
    ep_dir = os.path.abspath(os.path.join(SCRIPT_DIR, key))

    # ⚠️ 互斥（2026-09-17 E2E 实测教训）：同一篇小说若已有分集生成任务在跑，
    # 再派一次会让两轮并发处理同一章 —— 互相抢模型配额（实测把接口打成 503 风暴），
    # 结果「一个成功写盘 + 一个 failed」，用户看到失败但产物其实是好的（覆盖率 100%）。
    # 规则：章节有重叠 → 直接复用正在跑的那个任务；章节不重叠 → 允许并发。
    # 旧任务没记 picks（历史数据）时保守视为冲突。
    want = {int(c.get("index") or 0) for c in picks}
    with lock:
        for _tid, _st in list(generation_state.items()):
            if not (isinstance(_st, dict) and _st.get("status") == "running"
                    and str(_tid).startswith("episodes_")):
                continue
            if _st.get("novel_id") != meta.get("novel_id"):
                continue
            _running = {int(x) for x in (_st.get("picks") or [])}
            if _running and not (_running & want):
                continue                      # 章节不重叠，互不干扰
            return jsonify({
                "success": True, "reused": True, "task_id": _tid, "status": "running",
                "novel_id": meta.get("novel_id"),
                "message": (f"该小说已有分集生成任务在跑"
                            f"（{_st.get('current') or 0}/{_st.get('total') or 0} 集），"
                            "本次请求已复用它 —— 避免同一章被并发生成两次"),
                "chapters": [{"index": c.get("index"), "title": c.get("title"),
                              "char_count": c.get("char_count")} for c in picks],
                "episodes": [c.get("index") for c in picks],
                "total": len(picks),
                "project_id": proj["id"], "project_key": key,
                "episode_dir": ep_dir,
            })

    task_id = f"episodes_{meta.get('novel_id')}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": f"准备生成 {len(picks)} 集…", "current": 0, "total": len(picks),
            "novel_id": meta.get("novel_id"), "results": [],
            # 记录本任务负责的章节，供上面的互斥判断比对重叠
            "picks": sorted(int(c.get("index") or 0) for c in picks),
            "project_id": proj["id"], "project_key": key,
            "episode_dir": ep_dir,
        }
    threading.Thread(target=_episodes_worker,
                     args=(task_id, meta, picks, style, target_shots, overwrite, key,
                           # ⭐ 2026-10-06（用户指定）：默认开启「文学剧本→开拍剧本」两段式全自动生产。
                           # 用户是**自动项目**，明确"文学剧本无需人审"——故默认 True：自动出文学剧本、
                           # 自动改写成开拍剧本（分镜/台词），全程不插入人工审核。前端若显式传
                           # use_screenplay=false 仍可回退"章节原文直接开拍"（保持兼容）。
                           data.get('use_screenplay', True)),
                     daemon=True).start()
    return jsonify({
        "success": True, "task_id": task_id, "status": "started",
        "novel_id": meta.get("novel_id"), "style": style, "target_shots": target_shots,
        "use_screenplay": data.get('use_screenplay', True),
        "project_id": proj["id"], "project_key": key, "project_name": proj["name"],
        "episode_dir": ep_dir,
        "chapters": [{"index": c.get("index"), "title": c.get("title"),
                      "char_count": c.get("char_count")} for c in picks],
        "episodes": [c.get("index") for c in picks],
        "total": len(picks),
    })


# ===================== 前置解析（chapter pre-flight）=====================


# =====================================================================
# 跨集连贯性（相邻两章转剧本改进 A/B/C/D）：项目级设定库 / 摘要卡 / state / 校验 查询
# =====================================================================


# =====================================================================
# 新增模块 D：剧本提示词分析（prompt_h3 + 参考图提示词）
# =====================================================================


# =====================================================================
# 视频配音（QwenTTS 真实链路：剧本台词 → 逐角色语音 → output/dub/<项目>/）
# =====================================================================

from job_state import dub_tasks  # noqa: F401  2026-10-11 归位到 job_state
from job_state import dub_lock  # noqa: F401  2026-10-11 归位到 job_state




# =====================================================================
# 音频质检接线（提示词预检 + 成品质检）
# =====================================================================
# 两层都在「生成前后」各管一段，与图片/视频质检的三层结构（预检 → 成品质检 → 重试）对齐：
#   ① 配音台词预检（零模型依赖，默认开启）：挡住会被念出来的结构化残留、空台词、错配音色；
#   ② 配音成品质检（ffmpeg 客观层 + 频谱/波形 AI 层）：挡住「合成成功但整段无声」这类
#      在旧流程里要等到成片验收才暴露的问题。
# 两者都**不阻断生成**：整集生产不能被单句质检拖死，结论如实记录、逐句可定位即可。









def _apply_audio_lessons(plan_lines: list, project_name: str) -> int:
    """配音计划构建后、逐句合成前的音频教训召回（设计 §2.3.2）。

    对每句按 ``kind="audio"`` 召回历史教训（键 = 该句 text）：
      - ``ln["audio_hints"]`` 只存审计，**绝不进台词**；
      - 调 ``_apply_audio_hints`` 做计划级纠偏（说话人回填 / preset→design / 期望时长）。
    无教训时零行为变更；召回失败静默忽略（保险不影响配音）。返回产生 hints 的句数。
    """
    if not project_name or not plan_lines:
        return 0
    hit_lines = 0
    for ln in plan_lines:
        text = str(ln.get("text") or "")
        # 键用 build_dub_plan 刚构建、**尚未被预检自愈过**的台词原文（TTS 实际输入），
        # 保证 _record_audio_qc_lesson 里 phash 稳定、与自愈后的 text 不漂移。
        ln.setdefault("audio_orig_text", text)
        if not text:
            continue
        try:
            hints = prompt_memory.suggest(kind="audio", prompt=text, project=project_name,
                                          root_dir=PROJECT_OUTPUT_DIR)
        except Exception:  # noqa: BLE001 - 召回失败绝不影响配音
            hints = []
        if not hints:
            continue
        ln["audio_hints"] = list(hints)
        _apply_audio_hints(ln, hints, project_name)
        hit_lines += 1
    return hit_lines












# ===================== 参考音频克隆角色声线（2026-10-06） =====================
# 链路：上传参考音频 → 落 <dub>/voice_bank/<角色>/ref.<ext> → 试听确认 →
#       build_dub_plan 自动把该角色切到 clone 模式（显式模式优先，不被覆盖）。
# 开关在**前端**（音色面板）：绑定/解绑 + 试听。后端只做「存/查/删/试听」。


# =====================================================================
# 音画对齐与混音合成（配音轨 × 成片视频 → 带配音成片 output/final_dub/）
# =====================================================================

from job_state import mix_tasks  # noqa: F401  2026-10-11 归位到 job_state
from job_state import mix_lock  # noqa: F401  2026-10-11 归位到 job_state










# ===== 成片自动登记交付物 =====
# 用户视角的痛点：手工点击 / AI 总控跑出来的成片不会出现在「成品验收」页，页面永远是空的
# （只有 /api/autopilot/run-once 与托管轮转会登记）。这里在成片真正落盘处统一登记。


def _isolate_shot_sfx(video_path: str, project: str, episode, shot_id) -> dict:
    """对单镜音轨做人声分离，只留音效（H3 原生音效，剔掉它自带的说话声）。

    **fail-open**：任何异常都只返回 ok=False，绝不打断出片流程。
    """
    try:
        import sfx_isolate
        return sfx_isolate.isolate_sfx(
            video_path, project, f"ep{int(episode):02d}_shot{int(shot_id):02d}")
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}






# ============================================================================
# 无人值守托管（Autopilot）——「上传小说 → 敲定风格 → 电脑自己生产 → 人只验成品」
# ============================================================================
# 接口分工：
#   总览/自检 → status / ready / curve
#   托管开关 → plans / plan / enable / disable / pause / resume
#   进度视图 → progress / progress/<project>
#   成品验收 → deliverables / review / deliverable/file
#   人工介入 → exceptions / exceptions resolve
#   手动触发 → run-once（用于验证与补跑单集）


# 注：`_autopilot_guard` 已**前移**到本文件前段（首个使用点之前，见
# 「托管接口统一异常兜底」段）—— 原先定义在这里（文件后段），只能装饰其后
# 注册的路由，早注册的路由全部拿不到兜底（D1，2026-09-23）。


@app.errorhandler(BadRequest)
def _handle_bad_request(e):
    """请求体无法解析（非合法 JSON / Content-Type 不匹配）→ 400。

    没有这个兜底时，未被 _autopilot_guard 包裹的路由会直接把 werkzeug 的
    400 渲染成 HTML 错误页，前端拿到一坨 HTML 而无法解析成 JSON。
    """
    return jsonify({
        "success": False,
        "error": "请求体格式不正确：需要合法的 JSON（并带上 Content-Type: application/json）",
    }), 400




# =====================================================================
# 四层质量状态 · 审片接口（2026-09-29，C/D 层的人工并排复核）
# =====================================================================
# 数据口径与生成链路完全一致（零新管道）：
#   逐镜视频   /api/videos/<项目>/epNN/shot_XX.mp4          （VIDEOS_DIR）
#   分镜图     /api/storyboards/file/<项目>/shot_XX.png      （合同侧的「预期画面」）
#   抽帧       /api/qc/frames/...                            （送检时已抽好）
#   逐镜质检   qc_client.read_history(QC_DIR, 项目, "video", 镜号)
#   镜头契约   _load_script_for（shot_id/duration/description/...）
#   参考资产   /api/assets/<characters|items|scenes>/<项目>/<名称>/<图>
#
# C/D 的「通过」必须绑定当刻产物 + 合同（哈希）：重渲一集或改剧本后批准自动失效，
# 由 GET 接口的 stale 字段带回界面 —— 否则界面会显示「已批准」，实际批的是上一版。


# ==================== 全自动生产主控路由 ====================





















# ===================== 角色管理API =====================


# ===================== 导出API =====================


# ===================== 角色关系图谱API =====================


# ==================== AI Memory API ====================

#: 质检教训库（prompt_memory）与 AI 记忆（ai_memory）是**两套数据**：
#:   - ai_memory：手动登记的经验/模式，供记忆页展示（本轮之前只有 3 条 test，是死壳）
#:   - prompt_memory：生成链路**自动沉淀**的质检教训（真正在驱动「不达标→改提示词」）
#: 记忆页此前只读前者，因此「看不到任何自动化学习成果」。下面这组接口把两者都暴露出来。


if __name__ == '__main__':
    # P2-T4（F-02）：直跑分支也走同一套回环护栏（与 serve.main 共用 host_guard）。
    # 此前 `python app/app.py` 直跑完全绕过 serve.py 护栏 —— APP_HOST=0.0.0.0 时
    # 零鉴权的 debug 模式对同网段全裸暴露。现：非回环且未设 MJSCXT_ALLOW_NON_LOOPBACK
    # 时拒绝启动（fail-closed），与 serve.py 口径一致。
    from host_guard import _guard_host
    try:
        _guard_host(APP_HOST)
    except RuntimeError as e:
        app.logger.error("启动被安全护栏拦截（F-02 直跑分支）：%s", e)
        raise SystemExit(2)
    # 质检存量迁移：**服务真正启动时**调用一次（幂等）。⚠️ 只在此处 / serve.main() 调用，
    # 绝不放模块级（否则 import app 即改写用户配置）。
    boot_qc_migration()
    app.logger.info(f"漫剧生成系统启动: http://{APP_HOST}:{APP_PORT} (debug={APP_DEBUG})")
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG, threaded=True)


# ===================== 总控 AI 自主执行（function-calling agent） =====================
#
# 与 /api/ai/chat 的区别：
#   /api/ai/chat      —— 只聊天 + 抽取创作设定，不执行任何生产动作
#   /api/agent/chat   —— 由模型自己决定调用哪些工具，直接把活干完（无人确认）
#
# 安全不靠弹窗，靠 agent_core 里的自动护栏：工具白名单 / 昂贵动作配额 /
# 步数上限 / 同参数冷却 / 全局急停 / 单项目互斥 / 审计日志。


# ===== SPA 路由（必须在所有 API 路由之后，Flask 默认静态路由之前）=====
# 显式注册每个 SPA 页面，避免与 Flask 默认 /static 路由冲突
_SPA_PAGES = ['projects', 'upload', 'auto', 'memory', 'characters', 'deliver', 'export', 'settings']

for _page in _SPA_PAGES:
    _endpoint = f'spa_{_page}'
    def _make_spa_page(_p=_page, _ep=_endpoint):
        @app.route(f'/{_p}', endpoint=_ep)
        def _spa_page():
            return send_from_directory(_STATIC_DIR, 'index.html')
        return _spa_page
    _make_spa_page()

# 通用回退：任意未匹配路径返回 index.html（用于 SPA 客户端路由）
@app.route('/<path:path>')
def spa_fallback(path):
    """SPA 路由回退：非 API、非静态文件请求返回 Vite index.html"""
    # 放行 API 前缀
    if path.startswith('api/'):
        abort(404)
    # 若 static 目录下确实存在该文件（favicon.svg / robots.txt 等），直接返回真实文件，
    # 避免被下面的「带扩展名一律 404」误伤。
    # 用 normpath + 前缀校验防目录穿越；send_from_directory 自身也会做安全校验。
    safe = os.path.normpath(os.path.join(_STATIC_DIR, path))
    if safe.startswith(os.path.abspath(_STATIC_DIR)) and os.path.isfile(safe):
        return send_from_directory(_STATIC_DIR, path)
    # 放行带扩展名的静态资源
    if '.' in path.split('/')[-1]:
        abort(404)
    return send_from_directory(_STATIC_DIR, 'index.html')


# ==========================================================================
# D-11a（P2）：ComfyUI 输出目录滚动回收 —— 任务收尾接线
# --------------------------------------------------------------------------
# 判定与删除**全部委托**零第三方依赖的 disk_reclaim 模块（可用
# `MJSCXT_AUTOPILOT=0 python verify_comfyui_reclaim.py` 离线单测）；
# 这里只做三件事：
#   ① 从 config 常量推导「正式产物目录」清单（不硬编码任何盘符路径）；
#   ② 进程内 10 分钟节流（同一进程最多每 10 分钟真正扫描一次）；
#   ③ 全容错 —— 回收是**优化**不是功能，任何异常只留 warning，绝不阻断生产。
#
# 为什么把包装函数集中在文件末尾追加：本文件已逾万行，把新逻辑集中放在末尾
# 便于审阅与回滚，也避免与既有函数体交错。**注意不要再以「绝对行号」锚定任何
# 守卫** —— 本仓库吃过亏：`verify_silent_except.py` 的白名单原本写成
# `("app.py", 5691)`，D-11a 在上面插了两行就漂到 5693、守卫静默失效，被迫手工
# 同步。该白名单现已改为**内容锚点**（`app/verify_project_audit.py` G5 同口径），
# 行号扰动不再影响它。
#
# 调用点：`_generate_asset_task`（资产生成任务收尾）、`_storyboard_worker`
# （分镜生成任务收尾）；成片步骤收尾见 `app/pipeline.py step_final`。
# ==========================================================================

# ComfyUI「任务历史」自动清理（面板只增不减 → 易被误读成「生成了大量废图」）：
# 与上面的回收同构 —— 模块级节流 + 全容错，默认间隔取 config 值（5 分钟）。






