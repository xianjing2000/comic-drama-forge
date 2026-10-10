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

_norm_shot_key = shot_key.norm_shot_key

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
_VIDEO_TASK_IS_PIPELINE = contextvars.ContextVar("video_task_is_pipeline", default=False)


try:
    # 启动时把上次残留的 running 任务标记为 interrupted（可供前端提示「可继续」）
    _interrupted = task_db.recycle_interrupted()
except Exception as _e:  # noqa: BLE001  不得因任务库异常导致启动失败
    _interrupted = 0
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


@app.route('/api/status')
def api_status():
    comfyui_status = comfyui_client.get_status()
    return jsonify({
        "comfyui": comfyui_status,
        "assets": {
            "characters": sum(
                len(files) for _, _, files in os.walk(CHARACTERS_DIR)
            ) if os.path.exists(CHARACTERS_DIR) else 0,
            "items": sum(
                len(files) for _, _, files in os.walk(ITEMS_DIR)
            ) if os.path.exists(ITEMS_DIR) else 0,
            "scenes": sum(
                len(files) for _, _, files in os.walk(SCENES_DIR)
            ) if os.path.exists(SCENES_DIR) else 0,
        },
        "task_queue": _task_queue_status(),
        "gpu_gate": gpu_task_gate.status(),
        "interrupted_tasks": _interrupted,
    })


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






@app.route('/api/video/retry-shot', methods=['POST'])
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


@app.route('/api/video/retry-shots-batch', methods=['POST'])
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
                with app.test_request_context(json=body):
                    resp = _video_retry_shot_impl()
                    payload = (resp[0].get_json() if isinstance(resp, tuple)
                               else resp.get_json())
                    status = resp[1] if isinstance(resp, tuple) else resp.status_code
                    results.append({"shot_id": sid, "http_status": status,
                                    **(payload if isinstance(payload, dict) else {})})
            except Exception as e:  # noqa: BLE001  单镜失败不断批次
                app.logger.warning("[批量重生成] 镜头 %s 失败：%s", sid, e)
                results.append({"shot_id": sid, "success": False, "error": str(e)})
    ok_n = sum(1 for r in results if r.get("success"))
    app.logger.info("[批量重生成] 完成：%d/%d 镜成功", ok_n, len(results))
    return jsonify({"success": ok_n > 0, "total": len(results), "ok_count": ok_n,
                    "results": results})


def _video_retry_shot_impl():
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    shots = script.get("shots") or []
    shot = data.get('shot') or {}
    if not shot and shots:
        want = str(data.get('shot_id'))
        shot = next((s for s in shots if str(s.get("shot_id")) == want), {})
    if not shot:
        return jsonify({"success": False, "error": "未找到目标镜头"}), 400

    shot_id = shot.get("shot_id", 1)
    seq = _shot_seq(shot_id, 1)
    mode = str(data.get('mode') or 'reference').strip().lower()
    char_refs = data.get('character_refs') or []
    scene_refs = data.get('scene_refs') or []
    ref_imgs = _collect_reference_images(char_refs, scene_refs)
    main_char_img = _collect_reference_images(char_refs[:1], [])
    # 2026-09-27「分镜 + 本镜资产」：构建角色/物品/场景索引，逐镜匹配。
    _r_char_idx = _build_asset_index(char_refs, project, "character")
    _r_item_idx = _build_asset_index(script.get("items") or [], project, "item")
    _r_scene_idx = _build_asset_index(script.get("scenes") or [], project, "scene")
    if scene_refs:
        for _k, _v in _build_asset_index(scene_refs, project, "scene").items():
            if _v.get("image"):
                _r_scene_idx[_k] = _v
    # 参考图兜底：前端未传、或传了结构不完整的对象（例如直接传剧本 characters，
    # 只有 reference_prompt_zh 而无 front/base 键）时，从磁盘资产目录自动收集，
    # 避免「无角色锚点」的静默降级。
    if not main_char_img or not ref_imgs:
        auto_chars, auto_scenes = _collect_asset_refs(project)
        if not main_char_img:
            char_refs = char_refs or auto_chars
            main_char_img = _collect_reference_images(char_refs[:1], [])
        if not ref_imgs:
            scene_refs = scene_refs or auto_scenes
            ref_imgs = _collect_reference_images(char_refs, scene_refs)
        if main_char_img or ref_imgs:
            app.logger.info(f"[retry-shot] 参考图已由磁盘资产补齐："
                            f"角色 {len(main_char_img)} / 合计 {len(ref_imgs)}")
    _rs_ep = _ep_of_script(script, data.get('episode_no'))
    _rs_sub = f"ep{int(_rs_ep):02d}/" if _rs_ep and int(_rs_ep) > 1 else ""
    sb_map = _keyframe_sb_map(project, script, episode_no=_rs_ep)
    sb_local = sb_map.get(_shot_num_key(shot_id))
    _r_end_ref = None  # keyframe 模式的尾帧声明（<Picture 2>），非 keyframe 恒 None

    if mode == 'keyframe':
        kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project), _rs_ep)
        end_p = os.path.join(kf_dir, f"shot_{seq:02d}_end.png")
        if not (sb_local and os.path.isfile(sb_local)):
            return jsonify({"success": False, "error": "缺少分镜图，无法关键帧驱动"}), 400
        if not os.path.isfile(end_p):
            return jsonify({"success": False,
                            "error": "缺少尾帧，请先执行关键帧生成（/api/keyframes/generate）"}), 400
        _seg_refs = [sb_local, end_p]
        # 审计 P1-1（2026-09-29）：与主链路 keyframe 分支同口径 —— 实际挂图只有
        # 「首帧 + 尾帧」两张，char/item/scene 引用必须置空（旧实现残留
        # char_refs/scene_refs，会声明出实际不存在的 <Picture 3..N>）；尾帧图
        # 经 end_frame_ref 声明为 <Picture 2>，让提示词真正产出尾帧锚定句。
        _r_char_refs, _r_item_refs, _r_scene_refs = [], [], []
        _r_end_ref = {"name": f"shot_{seq:02d}_end"}
    else:
        if sb_local:
            # 「分镜 + 本镜资产」：分镜图 + 本镜角色三视图 + 物品 + 场景
            _r_matched = _match_shot_chars(shot, _r_char_idx)
            _r_want_half = _framing_wants_half_shot(shot)
            _r_char_imgs, _r_char_refs = [], []
            for _mc in (_r_matched or []):
                _e = _r_char_idx.get(_mc) or {}
                # 2026-10-02 服装变体：本镜服装提示（shot.outfit / shot.character_outfits）
                # 能解析出已生成的 outfit_key → 参考图优先取 outfits/<key>/ 同档位图；
                # 解析不出 / 未生成回落主设定图（_shot_outfit_dir 返回 ''，fail-open）。
                _p = _pick_char_view(_e, _r_want_half,
                                     _shot_outfit_dir(shot, _mc, _e.get("_dir") or ""))
                if _p and _p not in _r_char_imgs:
                    _r_char_imgs.append(_p)
                    _r_char_refs.append({"name": _mc,
                                         "appearance": _e.get("appearance")
                                         or _e.get("description") or ""})
            _r_item_imgs, _r_item_refs = [], []
            for _it in _resolve_item_names(shot, _r_item_idx, "重跑切段"):
                _p = (_r_item_idx.get(_it) or {}).get("image")
                if _p and _p not in _r_item_imgs and _p not in _r_char_imgs:
                    _r_item_imgs.append(_p)
                    _e = _r_item_idx.get(_it) or {}
                    _r_item_refs.append({"name": _it,
                                         "appearance": _e.get("appearance")
                                         or _e.get("description") or ""})
            _r_loc, _r_sc_entry = _resolve_scene_entry(shot, _r_scene_idx, "重跑切段")
            # 与主链路同口径：按本镜机位取对应场景档（缺失逐级回落 front/base）
            _r_scene_img = _pick_scene_view(_r_sc_entry, shot) if _r_sc_entry else None
            _r_scene_refs = ([{"name": _r_loc, "appearance": ""}]
                             if _r_scene_img else [])
            _seg_refs = [sb_local] + _r_char_imgs + _r_item_imgs + \
                ([_r_scene_img] if _r_scene_img else [])
            if not _r_char_imgs:
                _seg_refs = [sb_local] + main_char_img
                _r_char_refs = char_refs[:1] if char_refs else []
            # 9 张上限（与 builder MAX_REFERENCE_IMAGES=9 一致）
            if len(_seg_refs) > 9:
                _seg_refs = _seg_refs[:9]
                _rk = 9 - 1
                _r_char_refs = _r_char_refs[:_rk]
                _rk -= len(_r_char_refs)
                _r_item_refs = _r_item_refs[:_rk] if _rk > 0 else []
                _rk -= len(_r_item_refs)
                _r_scene_refs = _r_scene_refs[:_rk] if _rk > 0 else []
        else:
            _seg_refs = ref_imgs
            _r_char_refs, _r_item_refs, _r_scene_refs = char_refs, [], scene_refs
    try:
        dur = float(shot.get('duration') or 5)
    except (TypeError, ValueError):
        dur = 5.0

    # ---- 长镜切段（P0-1，与 worker 内 _shot_segment 同口径）----
    # 单镜重跑接口此前硬编码「一个分镜 = 一段」，与主链路的长镜切段不一致：
    # 用户手点重跑一个 12 秒镜头时，仍会一次生成 12 秒（超出 4 秒可信窗口）。
    # 这里改为与 _shot_segment 相同的切段 + 逐子段重建提示词逻辑。
    _rs_sub_shots = h3_prompt_kit.segment_shot(
        shot, dur, max_sec=h3_prompt_kit.H3_SEGMENT_MAX_SEC)
    _rs_segs = []
    # ⭐ 单镜重跑同样走 H3 Director timeline（generate_h3_sequence → 构建器），
    #    故按与 worker 内 _shot_segment 同口径给每个子段挂场景 LoRA（同源规则表）。
    #    优先使用 LLM 智能选择，失败则回落规则表匹配。
    _rs_loras = h3_segment_loras.select_loras_for_shot_smart(shot)
    for _rsi, _rsub in enumerate(_rs_sub_shots):
        if mode == 'keyframe':
            # ⚠️ 与 worker 内 _shot_segment 同口径：尾帧锚定句只挂最后一段
            #    （每段都挂 = 每段都演完整镜，接缝倒带重启）。
            _rp = comfyui_client._build_h3_prompt(
                _rsub, _r_char_refs, _r_scene_refs,
                storyboard_ref={"name": f"shot_{seq}"}, item_refs=_r_item_refs,
                end_frame_ref=(_r_end_ref if _rsi == len(_rs_sub_shots) - 1 else None))
        elif sb_local:
            _rp = comfyui_client._build_h3_prompt(
                _rsub, _r_char_refs, _r_scene_refs,
                storyboard_ref={"name": f"shot_{seq}"}, item_refs=_r_item_refs)
        else:
            # 择优：既有 prompt_h3 结构合规才采用，否则用规范构建器重建
            # （历史缺陷：`shot.get('prompt_h3') or _build_h3_prompt(...)` 让
            #  剧本里那句无参考图标签的裸英文把结构化提示词整个顶掉）
            _rp = comfyui_client.resolve_h3_prompt(
                _rsub, _r_char_refs, _r_scene_refs, item_refs=_r_item_refs)
        _rsuf = (f"_{chr(ord('a') + _rsi)}"
                 if len(_rs_sub_shots) > 1 and _rsi < 26 else "")
        _rs_segs.append({"prompt": _rp,
                         "duration": float(_rsub.get("duration") or dur),
                         "reference_images": _seg_refs,
                         "name": f"shot_{seq:02d}{_rsuf}",
                         "loras": list(_rs_loras)})
    seg = _rs_segs[0]

    # ---- 提示词预检（生成前质检）----
    # H3 的结构缺段只有生成端能重建（必须有每张参考图的用途），所以这里做「验证 + 安全追加」，
    # 命中致命缺陷就直接拦：缺段的 H3 提示词等于出片跑偏，而一次视频生成的代价远大于一次判断。
    # ⚠️ 逐子段预检：任一子段不合格即整镜拦截（与主链路 worker 内的逐子段预检口径一致）。
    for _ri, _rseg in enumerate(_rs_segs):
        _rp2, _pf_v, _pgate_v = _prompt_preflight(
            "h3", _rseg["prompt"], ctx=shot,
            style=(shot.get("style") or style_kit.normalize_style(
                (autopilot.get_plan(project) or {}).get("style"))),
            # ⚠️ 用 seg 里的参考图数量，不要用 `refs`：关键帧分支只设 ref_images，没有 `refs`，
            #    直接引用会 NameError（该分支走不到 else，`refs` 从未绑定）。
            expect_refs=bool(_rseg.get("reference_images")),
            project_name=project)
        _rseg["prompt"] = _rp2
        if not _pgate_v.get("accept"):
            # ★ 用户需求：视频提示词预检不通过 → 提示词唯一落盘物（P12）移回收站（同决策 1）。
            try:
                _purge_prompt_records(project, shot_id,
                                      reason=f"视频提示词预检未通过（{_pgate_v.get('label')}）")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
            return jsonify({"success": False, "prompt_qc_blocked": True,
                            "error": f"视频提示词预检未通过（{_pgate_v.get('label')}）：{_pgate_v.get('reason')}"
                                     + (f"；建议：{_pf_v.get('rebuild_hint')}" if _pf_v.get("rebuild_hint") else ""),
                            "prompt_qc": _pf_v.get("verdict")}), 200

    try:
        result = comfyui_client.generate_h3_sequence(
            segments=_rs_segs,
            filename_prefix=f"comic_drama_retry/{project}_shot_{seq:02d}",
            seed=data.get('seed'), timeout_per_segment=int(data.get('timeout') or 900))
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"单镜视频重跑失败：{e}"}), 500
    files = (result or {}).get('files') or []
    if not files or not os.path.isfile(files[0]):
        return jsonify({"success": False, "error": "ComfyUI 未返回视频文件"}), 500
    vid_dir = _ep_dir(os.path.join(VIDEOS_DIR, project), _rs_ep)
    os.makedirs(vid_dir, exist_ok=True)
    dst = os.path.join(vid_dir, f"shot_{seq:02d}.mp4")
    _move_with_retry(files[0], dst)
    # 该镜已更新 → 同集的旧成片失效，打上「已过期」标记，避免用户对着旧成片点验收
    stale = {}
    try:
        stale = pipeline.mark_deliverable_stale(
            project, _rs_ep or 1, "镜头重做后成片需重新合成",
            {"shot_id": shot_id, "seq": seq, "mode": mode,
             "video": os.path.basename(dst)})
    except Exception as e:  # noqa: BLE001 - 打标失败不影响重做本身
        app.logger.warning(f"标记成片过期失败：{e}")
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "mode": mode, "path": dst,
                    "url": f"/api/videos/{project}/{_rs_sub}{os.path.basename(dst)}",
                    "ref_count": len(seg["reference_images"]), "duration": seg["duration"],
                    "deliverable_marked_stale": bool(stale)})


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




















def _ensure_voice_bank_refs(common: list, project_name: str, all_characters=None) -> None:
    """确保每个公共角色在 voice_bank 里有参考音频（2026-10-03）：
    遍历公共池的角色，若 voice_bank 里没有参考音频，用 TTS 自动生成一段短文本
    并保存到 voice_bank，供后续 _h3_common_ref_audios 捡取。
    任何异常只记日志、绝不阻断主流程。
    """
    # ⚠️ 放宽守卫（原为 `if not common:`）：存量调用方要么传非空 common、要么传非空 all_characters（恒传 char_idx），故零行为变更；放宽只为让 `tts_pre`
    #    能以 common=[] + all_characters=<char_idx> 调用（「每角色参考音色」口径）。
    if not common and not all_characters:
        return
    _dub_dir = _dub_project_dir(project_name)
    if not _dub_dir:
        app.logger.warning("[VoiceBank] 找不到项目 dub 目录，跳过自动生成参考音色")
        return
    _char_names = []
    for _c in (common or []):
        if isinstance(_c, dict) and _c.get("kind") == "character":
            _n = str(_c.get("name") or "").strip()
            if _n and _n not in _char_names:
                _char_names.append(_n)
    # ⭐ 追加项目全量角色（2026-10-04）：用户要求「每个角色都要生成一个参考音色」。
    #    公共池角色之外的角色也要落 voice_bank（供后续按需作为参考音色使用）。
    if all_characters:
        _all_names = list(all_characters.keys()) if isinstance(all_characters, dict) else list(all_characters)
        for _n in _all_names:
            _n = str(_n or "").strip()
            if _n and _n not in _char_names:
                _char_names.append(_n)
    if not _char_names:
        return
    # 获取默认 voice_map（用于拿到每个角色的默认 speaker/seed）
    # ⭐ 2026-10-10 修复（用户反馈「人物音色要根据人物人设生成」）：
    #    原实现只传 [{"name": n}]，而 tts_client.default_voice_map 的 instruct
    #    来自 ch["voice_style"]（tts_client.py:515-522）→ 入参没有 voice_style
    #    → instruct 恒为空 → 参考音色只由 speaker 预设决定，与角色人设无关。
    #    这里把**剧本里的角色人设**读出来一起传进去（gender/age/identity/
    #    personality/voice_style），让音色底稿真正由人设派生。
    #    ⚠️ 注意与 H3 的英文约束无关：那是「六段正文」的约束；TTS 的 instruct 是
    #       QwenTTS 的中文参数，中文人设（如「清亮少年音」）正是它要的输入。
    _chars_profile = []
    try:
        _script_p = os.path.join(_ep_dir(os.path.join(SCRIPT_DIR, project_name), 1),
                                 "第1集.json")
        if not os.path.isfile(_script_p):
            _script_p = os.path.join(SCRIPT_DIR, project_name, "第1集.json")
        _sd = read_json_strict(_script_p, {}) or {}
        for _c in (_sd.get("characters") or []):
            if isinstance(_c, dict) and str(_c.get("name") or "").strip():
                _chars_profile.append(_c)
    except Exception as _e:  # noqa: BLE001
        _chars_profile = []
    if _chars_profile:
        app.logger.info("[VoiceBank] 从剧本读到 %d 个角色人设，将用于音色派生",
                        len(_chars_profile))
    try:
        import tts_client
        _vm_input = _chars_profile or [{"name": n} for n in _char_names]
        _voice_map = tts_client.default_voice_map(
            _vm_input,
            project=project_name,
            episode=1)
        _chars_vmap = _voice_map.get("characters", {})
    except Exception as _e:  # noqa: BLE001
        app.logger.warning("[VoiceBank] 无法生成默认 voice_map，跳过：%s", _e)
        return
    # 逐角色合成
    import tempfile
    for _cn in _char_names:
        try:
            _ref, _ = find_voice_bank_ref(_dub_dir, _cn)
            if _ref and os.path.isfile(_ref):
                continue  # 已有参考音频
        except Exception:
            pass
        # 需要生成：参考文本与音色指令都尽量带**角色人设**（2026-10-10 修复）。
        # ⚠️ 原实现的两个问题（用户反馈「人物音色要根据人物人设生成」）：
        #   ① instruct 被**硬编码为空** —— 而 tts_client.default_voice_map 已经按
        #      角色算好了音色底稿（voice_style → instruct，见 tts_client.py:522），
        #      算完就被这里丢掉，于是参考音色只由 speaker 预设决定、与人设无关；
        #   ② 参考文本是通用句「我是X，这是我的声音样本。」，不含任何人物信息，
        #      TTS 拿不到「这人多大、什么身份、什么气质」的语境。
        #   现在：有音色底稿就用 design 模式（instruct 生效），并在参考文本里带上
        #   角色的身份/年龄/性格要点；都没有时保持原行为（preset + 通用句）。
        _voice = _chars_vmap.get(_cn) or {}
        _speaker = str(_voice.get("speaker") or tts_client.SPEAKER_KEYS[0])
        _seed = int(_voice.get("seed") or 0)
        _instruct = str(_voice.get("instruct") or "").strip()
        # 人设要点：优先取角色索引里的 appearance/identity/age，取不到就跳过
        _ci = {}
        try:
            if isinstance(all_characters, dict):
                _ci = all_characters.get(_cn) or {}
            if not _ci:
                _ci = _chars_vmap.get(_cn) or {}
        except Exception:  # noqa: BLE001
            _ci = {}
        _bits = []
        for _k, _prefix in (("identity", ""), ("age", ""), ("personality", ""),
                            ("gender", "")):
            _v = str((_ci or {}).get(_k) or "").strip()
            if _v:
                _bits.append(f"{_prefix}{_v}")
        _desc = "，".join(_bits)
        _ref_text = (f"我是{_cn}，{_desc}。这是我的声音样本。" if _desc
                     else f"我是{_cn}，这是我的声音样本。")
        # 组装 TTS voice dict：有音色底稿走 design（instruct 生效），否则维持 preset
        _tts_voice = {
            "mode": "design" if _instruct else "preset",
            "speaker": _speaker,
            "seed": _seed,
            "instruct": _instruct,
            "model_choice": "1.7B",
        }
        app.logger.info("[VoiceBank] %s 参考音色生成：mode=%s instruct=%s 文本=%s",
                        _cn, _tts_voice["mode"], (_instruct[:40] or "(空)"), _ref_text[:50])
        # 输出路径：voice_bank 目录下的临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _tf:
            _tmp_path = _tf.name
        try:
            _dub_dir2 = _dub_project_dir(project_name)
            _client = tts_client.QwenTTSClient(out_root=_dub_dir2, params=tts_client.TTS_DEFAULT_PARAMS)
            _res = _client.synthesize_one(_ref_text, _tts_voice, _tmp_path, timeout=60)
            if not _res.get("ok"):
                app.logger.warning("[VoiceBank] %s 合成参考音频失败：%s", _cn, _res.get("error"))
                continue
            # 存入 voice_bank（按 ref.wav 落盘，save_voice_bank_ref 会覆盖旧文件）
            _saved = save_voice_bank_ref(_dub_dir2, _cn, _tmp_path, ref_text=_ref_text)
            app.logger.info("[VoiceBank] %s 自动生成参考音色：%s", _cn, _saved)
        except Exception as _e:  # noqa: BLE001
            app.logger.warning("[VoiceBank] %s 自动生成参考音色异常（忽略）：%s", _cn, _e)
        finally:
            try:
                if os.path.isfile(_tmp_path):
                    os.remove(_tmp_path)
            except Exception:
                pass












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


@app.route('/api/videos/generate', methods=['POST'])
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


def _video_generate_worker(task_id, project_name, shots, character_refs,
                          scene_refs, storyboards, use_storyboard, mode,
                          timeout_per_segment, episode_tag, episode_no=None,
                          chain_mode="auto", style="", overwrite=False,
                          build_only=False, only_scenes=None):
    """整集一次生成的视频生成（后台任务体，可被路由与流水线复用）

    2026-10-05：per_shot / keyframe 模式分支已移除，mode 恒按 episode 处理。
    overwrite：是否全量重做。默认 False —— 按场次生成时，已有成片的场次直接复用、
    只重做缺失的场次（断点续跑）；True 时强制重画。
    ``only_scenes``：只生成/重做指定场次（单场重做）。

    从 /api/videos/generate 抽出的模块级实现：原闭包变量（项目名、镜头、参考图、
    模式等）改为显式参数，业务逻辑不变。抽出的目的是让自动生产流水线
    （pipeline.py / autopilot.py）能直接复用同一条视频生成链路，避免两套实现漂移。

    episode_no：集级目录隔离（第 1 集沿用平铺，第 2 集起写 epNN/），
    避免多集自动生产时 shot_NN.mp4 互相覆盖。
    style：用户与总控敲定的风格描述。用途有二 ——
      ① 补齐镜头 style 字段（剧本未注入时的兜底），使提示词带上风格；
      ② 解析画幅并覆写 H3 分辨率（竖屏 9:16 真正落地，而非模板死板的 16:9）。
    """
    # ⚠️ 关键：本 worker 是**裸线程**（见 /api/videos/generate 的 threading.Thread），
    # 运行在 Flask 请求线程之外 —— contextvars 不会从请求线程继承过来，因此
    # `comfyui_client.wait_for_completion` 轮询里的 `cancellation.should_stop()`
    # 在过去**恒为 False**：前端点「暂停」只停了托管的下一步，**正在跑的 ComfyUI
    # 渲染任务不会被打断**（用户反馈「暂停要同步停止 comfyui 的任务」的根因）。
    # 这里显式把中止判定器注册进本线程的执行上下文：
    #   - 托管暂停（autopilot.is_paused）→ 立即中断远端
    #   - 进程退出（autopilot._STOP / 解释器与用户交互无关的关停）→ 一并中断
    # 判定器自身异常在 cancellation.should_stop 里 fail-open 处理，不影响生产。
    _cancel_token = cancellation.push(_video_should_stop)
    # 该任务是否由托管流水线发起（pipeline._run_task_worker 会置 state["pipeline"]=True）。
    # 决定「托管暂停」是否应掐断本任务：手动生成不受托管开关影响。
    _is_pipeline = False
    with lock:
        _st0 = generation_state.get(task_id)
        if isinstance(_st0, dict):
            _is_pipeline = bool(_st0.get("pipeline"))
    _pipe_token = _VIDEO_TASK_IS_PIPELINE.set(_is_pipeline)
    try:
        _video_generate_worker_body(
            task_id, project_name, shots, character_refs, scene_refs, storyboards,
            use_storyboard, mode, timeout_per_segment, episode_tag, episode_no,
            chain_mode, style, overwrite, build_only, only_scenes)
    except cancellation.Cancelled as e:
        # 协作式中止：不是失败，落到「已取消」态，前端展示为已停止而非报错
        app.logger.info("[视频] 任务因中止信号停止（task=%s）：%s", task_id, e)
        with lock:
            _st = generation_state.get(task_id)
            if isinstance(_st, dict):
                _st.update({"status": "cancelled", "phase": "已停止",
                            "error": "已收到中止信号，ComfyUI 远端任务已中断（已完成镜头保留，可续跑）"})
    finally:
        _VIDEO_TASK_IS_PIPELINE.reset(_pipe_token)
        cancellation.reset(_cancel_token)


def _video_should_stop() -> bool:
    """视频 worker 的中止判定器：**仅对托管（pipeline）任务**生效。

    刻意与 `autopilot._halt_requested` 同口径，但**不 import autopilot**（app.py 与
    autopilot 相互 import 会成环）。用惰性 import 规避循环依赖，失败时 fail-open。

    ⚠️ 2026-09-24 修正：早期实现「只要 autopilot 处于 paused 就停」，会把**用户手工
    触发**的生成一起掐掉 —— 实测：托管暂停期间点「生成视频（手动）」，第一次轮询就
    命中中止信号，报「ComfyUI 远端等待期间收到中止信号」（manual 任务被 pause 误杀）。
    暂停的语义应只覆盖「托管自动生产」，不该阻断用户当前手动操作。
    因此这里判定的前提是 `_VIDEO_TASK_IS_PIPELINE`（由 worker 外壳按任务态设置）：
      - 托管任务（generation_state[task]["pipeline"] is True）：托管暂停 → 停；
      - 进程退出（autopilot._STOP）：任何任务都停（关服就该全停）。
    """
    try:
        import autopilot
        # 进程退出：无论手动还是托管，都应立刻停
        stop_ev = getattr(autopilot, "_STOP", None)
        if stop_ev is not None and stop_ev.is_set():
            return True
        # 托管暂停：只对 pipeline 任务生效（手动任务不受托管开关影响）
        if _VIDEO_TASK_IS_PIPELINE.get() and autopilot.is_paused():
            return True
    except Exception as e:  # noqa: BLE001  判定器故障不得影响生产
        app.logger.debug("视频中止判定器读取失败（按不中止处理）：%s", e)
    return False


def _video_generate_worker_body(task_id, project_name, shots, character_refs,
                                scene_refs, storyboards, use_storyboard, mode,
                                timeout_per_segment, episode_tag, episode_no=None,
                                chain_mode="auto", style="", overwrite=False,
                                build_only=False, only_scenes=None):
    """视频生成的实际业务体（外壳见 :func:`_video_generate_worker`，负责注册中止信号）"""
    try:
        # 风格/画幅：整集共用一次解析
        # G19：风格串未含画幅关键词时以默认画幅（2026-09-28 起为 16:9）为底，
        #      不再静默回落模板尺寸。megapixels：视频预算走 video_megapixels()
        #      —— 让 MJSCXT_VIDEO_MEGAPIXELS 真正生效（8GB 卡的 0.4 应急档）。
        #      默认 0.5 与 aspect_size 的默认值**完全等价**（16:9→960×544、
        #      9:16→544×960），故本行不改变既有行为。
        _style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO,
                                       megapixels=style_kit.video_megapixels())
        _size = _style_res.get("size")
        if style:
            app.logger.info("[视频] 风格=%s；画幅=%s", _style_res.get("style") or style,
                            _style_res.get("label") or "未指定（沿用模板）")
        # 镜头 style 兜底：老剧本没有该字段时（历史产物），用本次传入的风格补齐
        if _style_res.get("style"):
            shots = [dict(s, style=(s.get("style") or _style_res["style"]))
                     for s in (shots or []) if isinstance(s, dict)]
        # 按模型能力归一化镜头参数（Toonflow 借鉴吸收点 #1，2026-10-01）：
        # duration 钳位、引用列表补齐、shot_id 补齐——fail-open，归一失败按原样继续。
        try:
            shots, _norm_notes = model_capabilities.normalize_shots_for_h3(shots)
            if _norm_notes:
                app.logger.info("[视频] 镜头参数归一化：%s", "；".join(_norm_notes[:3]))
        except Exception as _norm_err:  # noqa: BLE001
            app.logger.warning("镜头参数归一化失败（按原 shots 继续）：%s", _norm_err)
        videos_dir = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
        os.makedirs(videos_dir, exist_ok=True)
        try:
            _epn = int(episode_no) if str(episode_no or "").strip() else 1
        except (TypeError, ValueError):
            _epn = 1
        # 视频 URL 前缀：第 2 集起带 epNN 段（与 _ep_dir 的落盘位置一致）
        _vurl = (f"/api/videos/{project_name}/ep{_epn:02d}" if _epn > 1
                 else f"/api/videos/{project_name}")

        ref_imgs = _collect_reference_images(character_refs, scene_refs)
        main_char_img = _collect_reference_images(character_refs[:1], [])
        # 参考图兜底（2026-10-01 实测补）：前端未传、或传了结构不完整的对象
        # （例如直接传剧本 characters，只有 reference_prompt_zh 而无 front/base 键）时，
        # 从磁盘资产目录自动收集 —— 与单镜重跑路径 (_video_retry_shot_impl) 同一口径。
        # ⚠️ 主链路此前漏了这一步，后果是整集视频**一个角色锚点都拿不到**：
        # 日志里那句「角色参考图不可用: 羡进 / 赵天霸」就是它，人物完全靠模型自由发挥，
        # 正是「全片人物 OOC / 服装款式对不上设定图」的根因。这类静默降级不报错，
        # 只能靠跑一遍全流程看日志才能发现。
        if not main_char_img or not ref_imgs:
            _auto_chars, _auto_scenes = _collect_asset_refs(project_name)
            if not main_char_img:
                character_refs = character_refs or _auto_chars
                main_char_img = _collect_reference_images(character_refs[:1], [])
            if not ref_imgs:
                scene_refs = scene_refs or _auto_scenes
                ref_imgs = _collect_reference_images(character_refs, scene_refs)
            if main_char_img or ref_imgs:
                app.logger.info("[视频] 参考图已由磁盘资产补齐：角色 %d / 合计 %d",
                                len(main_char_img), len(ref_imgs))
            else:
                # 优化#1（2026-10-01）：连磁盘兜底都拿不到参考图 → 角色资产从未生成或
                # 目录被清空。此前只 warning 后照跑（人物全靠模型自由发挥，成片必 OOC）。
                # 现在自动补做：从剧本取角色清单，同步触发生成（上限 4 个、总等待 30 分钟），
                # 完成后重新收集参考图再继续；补做失败/超时不阻塞出片（fail-open）。
                app.logger.warning("[视频] 磁盘资产目录里也没有可用参考图 → 自动补做角色资产…")
                try:
                    _scr0 = _load_script_for(project_name, episode_no) or {}
                    _need = [c for c in (_scr0.get("characters") or [])
                             if isinstance(c, dict) and str(c.get("name") or "").strip()][:4]
                    if _need:
                        _task_id = f"character_{project_name}_{uuid.uuid4().hex[:12]}"
                        with lock:
                            generation_state[_task_id] = {
                                "status": "running", "asset_type": "character",
                                "progress": 0, "total": len(_need), "current": 0,
                                "phase": "基础图", "results": [],
                                "overwrite": False, "auto_repair": True}
                        _t0 = threading.Thread(
                            target=_generate_asset_task,
                            args=(_task_id, _need, "character", project_name,
                                  _project_style(project_name), False))
                        _t0.daemon = True
                        _t0.start()
                        _t0.join(timeout=1800)     # 上限 30 分钟，超时不阻塞出片
                        if _t0.is_alive():
                            app.logger.warning("[视频] 资产补做超时（30 分钟）→ 按无锚点继续")
                        _re_chars, _re_scenes = _collect_asset_refs(project_name)
                        if _re_chars:
                            character_refs = _re_chars
                            main_char_img = _collect_reference_images(character_refs[:1], [])
                            ref_imgs = _collect_reference_images(character_refs, scene_refs)
                            app.logger.info("[视频] 资产补做完成：角色参考图已重新挂载"
                                            "（主角锚点 %d / 合计 %d）",
                                            len(main_char_img), len(ref_imgs))
                except Exception as _e:  # noqa: BLE001
                    app.logger.warning("[视频] 资产自动补做失败（继续生成）：%s", _e)
                if not (main_char_img or ref_imgs):
                    app.logger.warning("[视频] 补做后仍无参考图 —— 本集将无角色锚点生成，"
                                       "人物一致性无法保证（检查 output/assets/characters/%s）",
                                       project_name)
        app.logger.info(f"视频参考图解析结果: {ref_imgs}；主角锚点: {main_char_img}")

        # B-18 P1-7：构建角色索引，供 _shot_segment 逐镜匹配参考图（与分镜链路口径对齐）
        char_idx = _build_asset_index(character_refs, project_name, "character")
        # 2026-09-27 扩展：物品/场景索引也逐镜匹配（「分镜+本镜资产」参考图策略）。
        # 前端 video 接口未传 items/scenes 时，从剧本兜底读取（含 characters/items/scenes）。
        _scr = _load_script_for(project_name, episode_no) or {}
        item_idx = _build_asset_index((_scr.get("items") or []), project_name, "item")
        scene_idx = _build_asset_index((_scr.get("scenes") or []), project_name, "scene")
        # 前端显式传来的 scene_refs 优先（可能带 URL/本地路径，比剧本兜底更准）
        if scene_refs:
            _scene_idx_explicit = _build_asset_index(scene_refs, project_name, "scene")
            for _k, _v in _scene_idx_explicit.items():
                if _v.get("image"):
                    scene_idx[_k] = _v

        # 分镜图映射（步骤5产物）→ 作为 H3 的 <Picture 1> 构图基准
        # 修复：改用合并式映射（目录扫描 + manifest + 前端传入）。
        # 原实现只认前端传入的 storyboards，前端漏传某镜时该镜会静默退化为
        # 「无分镜图参考」，与用户所见不符。
        sb_map = _keyframe_sb_map(project_name, None, storyboards, episode_no=episode_no)
        app.logger.info(f"分镜图参考映射: {sorted(sb_map.keys())}")

        # H3 Director **公共参考图**（2026-09-30）：episode 模式在循环前填这里
        # （见 _h3_plan_common_refs）。
        # ``_comps_map`` 让「公共池规划」与「段级挂图」共用同一份逐镜解析结果
        # （重复解析会把 _ref_warnings 同一句告警记两遍，且两边口径可能漂移）。
        _comps_map: dict = {}

        def _shot_segment(shot, seq, qc_cfg=None, common=None, common_keys=None,
                          extra_ref=None):
            """把一个分镜转成 H3 工作流的一个「段」（提示词 + 时长 + 参考图）

            ``common`` / ``common_keys``：本次提交的**公共参考图**（H3 Director 公共
            参数，2026-09-30）。公共项由客户端写进 ``global.refs``（index
            ``0..K-1``）+ ``commonEnabled=true``，因此：
              · 提示词按 ``<Picture 1..K>`` **先声明公共项**（``common_refs=`` 传给
                ``comfyui_client._h3_picture_defs``）；
              · 本段 ``reference_images`` **不含**公共项 —— 同一张图挂两处会被插件
                按 index 当成两张（槽位白白翻倍，还可能挤掉本镜自己的锚点）；
              · 9 槽预算先扣掉 K，再留给「分镜图 + 本镜资产」。
            不传（默认）＝完全走原路径，零行为变更。
            """
            sid = shot.get('shot_id')
            sb_local = sb_map.get(_norm_shot_key(sid)) if use_storyboard else None
            _c_keys = common_keys if common_keys is not None else set()
            _c_refs = []          # 公共项的「提示词载荷」（kind/name/appearance）
            if common:
                _c_refs = [dict(c) for c in common]
            # 按镜匹配的参考图 refs（供提示词构建），非 sb_local 分支默认走全局 refs
            _shot_char_refs = character_refs
            _shot_item_refs = []
            _shot_scene_refs = scene_refs
            _seg_comps = []       # 本段**私有**组件（不含公共项）
            if sb_local:
                # 2026-09-27「分镜 + 本镜资产」参考图策略：分镜图(构图基准) +
                # 本镜出场角色三视图(每人一张) + 本镜物品 + 场景图。
                # H3 Director 每段最多 9 张（ref_image_0..8），去掉旧的「上限 2 张」保守限制。
                # 2026-09-30：解析收敛到 _h3_shot_ref_components（公共池规划与段级挂图
                # 共用同一份结果），并在这里把**公共项摘掉**（它们由客户端走 global.refs）。
                _comps = (_comps_map.get(str(sid))
                          if isinstance(_comps_map, dict) else None)
                if _comps is None:
                    # 优化#2 接线（2026-10-01）：本镜角色声明了服装（shot.outfit /
                    # shot.character_outfits）且对应变体资产已生成 → 用变体参考图；
                    # 无声明或变体不存在时逐字走旧逻辑（_shot_outfit_dir 返回空）。
                    _outfit_map = {}
                    for _cn in (char_idx or {}).keys():
                        try:
                            _od = _shot_outfit_dir(
                                shot, str(_cn),
                                os.path.join(CHARACTERS_DIR, project_name, str(_cn)))
                        except Exception as _oe:  # noqa: BLE001
                            _od = ""
                            app.logger.debug("[服装变体] 解析失败（回落主设定图）：%s", _oe)
                        if _od:
                            _outfit_map[str(_cn)] = _od
                    _comps = _h3_shot_ref_components(shot, char_idx, item_idx, scene_idx,
                                                     character_refs, main_char_img,
                                                     outfit_map=_outfit_map or None)
                _seg_comps = [c for c in _comps if not _h3_is_common_comp(c, _c_keys)]
                _shot_char_refs = [c for c in _seg_comps if c.get("kind") == "character"]
                _shot_item_refs = [c for c in _seg_comps if c.get("kind") == "item"]
                _shot_scene_refs = [c for c in _seg_comps if c.get("kind") == "scene"]
                # ⭐ 槽位预算：9 格总量里先扣公共块（K 张），再扣分镜图 1 张，
                #    剩下的才是本镜资产的额度。超出按「角色→物品→场景」截断
                #    （分镜图恒保留），并同步截断提示词的 char/item/scene refs，
                #    避免「声明的 <Picture N> > 实际传入的图」错位。
                _own_room = max(0, h3_director_builder.MAX_REFERENCE_IMAGES
                                - len(_c_refs) - 1)
                # ⭐ 2026-10-09（用户反馈「场次之间没有关联」）②-A 跨场视觉衔接：
                #    非首场的**首镜**额外挂一张「上一场最后一镜的分镜图」，给 H3 一个
                #    真实的跨场视觉锚点（服装/道具/光位/陈设的接续），而不是只靠
                #    transition_clause 的文字描述（那正是本次缺陷：有字无形）。
                #    ⚠️ 必须在槽位截断**之前**插入，否则会被 _own_room 截掉而静默失效。
                if extra_ref and len(_seg_comps) < _own_room:
                    _seg_comps.append(dict(extra_ref))
                if len(_seg_comps) > _own_room:
                    _dropped = len(_seg_comps) - _own_room
                    _seg_comps = _seg_comps[:_own_room]
                    _shot_char_refs = [_c for _c in _seg_comps
                                       if _c.get("kind") == "character"]
                    _shot_item_refs = [_c for _c in _seg_comps
                                       if _c.get("kind") == "item"]
                    _shot_scene_refs = [_c for _c in _seg_comps
                                        if _c.get("kind") == "scene"]
                    app.logger.warning(
                        "[H3公共参考图] 镜头 %s：公共 %d 张 + 分镜图占位后，本镜资产"
                        "可挂 %d 张，已按「角色→物品→场景」丢弃 %d 项",
                        sid, len(_c_refs), _own_room, _dropped)
                refs = [sb_local] + [c["path"] for c in _seg_comps]
            else:
                # 无分镜图的兜底分支：编号从 1 起重新数，公共块不参与
                # （_h3_plan_common_refs 已要求全镜有分镜图，故开启公共时走不到这里）。
                refs = ref_imgs
                _c_refs = []
            try:
                dur = float(shot.get('duration') or 5)
            except (TypeError, ValueError):
                dur = 5.0
            # ---- 长镜切段（需求 J / P0-1，2026-09-25）----
            # 业界共识：AI 视频可信窗口约 4 秒，超过后段易崩坏。本项目剧本单镜普遍
            # 4.5~12 秒（实测第1集 27/27 超线、第2集 28/29 超线），故在**生成期**把长镜
            # 拆成多个 ≤H3_SEGMENT_MAX_SEC 的子段，一次提交让 H3 原生段间衔接出**一条**
            # 连续视频 —— 落盘仍是单个 shot_XX.mp4，命名契约（probe_video / dub_mix /
            # _SHOT_RE）全部不受影响；总时长严格守恒（segment_durations 均摊且 sum 不变），
            # 故配音时间轴与成片长度也不变。
            # ⚠️ 每个子段要用**子段时长**重建提示词：否则 4 秒的段会被塞进 12 秒的节拍，
            #    段内动作空转、台词位置也会整体后移。
            _seg_shots = h3_prompt_kit.segment_shot(
                shot, dur, max_sec=h3_prompt_kit.H3_SEGMENT_MAX_SEC)
            _multi_seg = len(_seg_shots) > 1
            _segs = []
            # ⭐ 逐分镜场景 LoRA（LLM 智能选择 + 规则表兜底，见 app/h3_segment_loras.py）：
            #    优先调用用户配置的 AI 模型分析分镜内容，智能判断应使用哪个风格 LoRA；
            #    若 AI 模型未配置 / 调用失败，则回落规则表匹配（按场景文本关键词）。
            #    同一分镜的所有长镜子段继承同一套（一场戏一种画风/质感）。
            #    editMode=segment → 插件按段采纳 segments[i].loras（模块 docstring 有机制说明）。
            #    无命中 → []；构建器只有在非空时才把 loras 写进 timeline。
            _shot_loras = h3_segment_loras.select_loras_for_shot_smart(shot)
            for _si, _sub in enumerate(_seg_shots):
                _sub_dur = float(_sub.get("duration") or dur)
                # 提示词按子段重建（reference 构图基准分支与无分镜图兜底分支共用同一构建入口）
                # ``common_refs=_c_refs``：公共参考图排在 ``<Picture 1..K>``，与客户端写进
                # ``global.refs`` 的槽位 0..K-1 同序同编号（H3 Director 公共参数）。
                # ⭐ 2026-10-09 官方音色声明：见 _h3_audio_defs_for
                # 2026-10-09 修复（视频阶段整集失败的根因）：
                #    此处原写 all_characters —— 本函数内根本没有这个名字
                #    （它定义在 L6911 为 char_idx），于是每次调用必抛
                #    NameError: name 'all_characters' is not defined，
                #    表现为「视频缺失 1 镜：['整集']」并连续重试 3 次全部失败、
                #    第 1 集流水线中止（stop_on_failure 等人工）。
                #    同函数 L7155 用的是正确写法 all_characters=char_idx，可对照。
                _h3_audio_defs = _h3_audio_defs_for(_c_refs, project_name, char_idx)
                if sb_local:
                    _sub_prompt = comfyui_client._build_h3_prompt(
                        _sub, _shot_char_refs, _shot_scene_refs,
                        storyboard_ref={"name": f"shot_{sid}"},
                        item_refs=_shot_item_refs,
                        common_refs=_c_refs,
                        audio_defs=_h3_audio_defs)
                else:
                    _sub_prompt = comfyui_client.resolve_h3_prompt(
                        _sub, _shot_char_refs, _shot_scene_refs,
                        item_refs=_shot_item_refs,
                        common_refs=_c_refs,
                        audio_defs=_h3_audio_defs)
                # ---- 提示词预检（生成前质检）----
                # ⚠️ 这里**只自愈 + 记录，不阻断**：整集模式一次提交 N 段，为一条提示词的问题把
                #    整集生成打断，代价远大于收益；且 H3 提示词由构建器产出、结构必然齐全，
                #    出现 fatal 只可能是构建器自身有 bug —— 那更该留下证据继续跑，
                #    而不是让整集静默失败。单镜重跑接口（用户显式只跑一镜）才做硬阻断。
                _sub_prompt, _pf_seg, _pgate_seg = _prompt_preflight(
                    "h3", _sub_prompt, ctx=_sub,
                    style=(shot.get("style") or _style_res.get("style") or ""),
                    expect_refs=bool(refs), project_name=project_name,
                    cfg=qc_cfg)   # G13：复用 worker 级质检配置，避免逐镜再读盘+解密
                # 单段时名字保持旧样式 shot_07（与历史日志/画布标识一致）；
                # 多段时标 shot_07_a/_b/_c 仅供日志辨识，**不参与落盘命名**。
                _suffix = (f"_{chr(ord('a') + _si)}"
                           if _multi_seg and _si < 26 else "")
                _segs.append({"prompt": _sub_prompt, "duration": _sub_dur,
                              "reference_images": refs,
                              "name": f"shot_{seq:02d}{_suffix}",
                              "loras": list(_shot_loras)})
                if _pf_seg.get("repairs") or (_pf_seg.get("verdict") or {}).get("issues"):
                    _segs[-1]["prompt_qc"] = _pf_seg.get("verdict")
                    _segs[-1]["prompt_qc_repairs"] = _pf_seg.get("repairs") or []
                if not _pgate_seg.get("accept"):
                    _segs[-1]["prompt_qc_blocked"] = True
                    app.logger.warning("镜头 %s 视频提示词预检未通过（%s）：%s",
                                       shot.get("shot_id"), _pgate_seg.get("label"),
                                       _pgate_seg.get("reason"))
            if _multi_seg:
                app.logger.info(
                    "[长镜切段] project=%s shot=%s %ss → %d 段 %s（每段 ≤%ss，总时长守恒）",
                    project_name, shot.get("shot_id"), dur, len(_segs),
                    "/".join(f"{s['duration']:.2f}" for s in _segs),
                    h3_prompt_kit.H3_SEGMENT_MAX_SEC)
            elif not _pgate_seg.get("accept"):
                # ⚠️ 用户需求：不合格提示词不留本地（P12）。整集模式该段不生成，
                #    把可能存在的上一轮 `prompt_<shot>.json` 移回收站。
                #    多段时（_multi_seg）暂不按段清理：一条 prompt 记录对应一个 shot_id，
                #    按子段清理会把同一镜的记录反复移动，留到质检汇总里处理。
                try:
                    _purge_prompt_records(project_name, shot.get("shot_id") or seq,
                                          reason=f"视频提示词预检未通过（{_pgate_seg.get('label')}）")
                except Exception as _pe:  # noqa: BLE001
                    app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
            # ⚠️ 必须返回**列表**：长镜切段后一个分镜可能产出 N 个子段（见上方 _segs）。
            #    两个调用方（episode / per_shot）都用 isinstance(seg, list) 兼容单段，
            #    故单段场景零行为变更。旧写法 `return _segs[0]` 会把切段结果砍成 1 段，
            #    导致整集只生成每镜的第 1 个子段（实测第 2 集 7 段 562 帧，应为 18 段 1450 帧）。
            return _segs, sb_local

        # ---------- 模式 episode：整集 N 段一次生成（H3 原生衔接）+ 整片 QC 门控 ----------
        if mode == 'episode':
            # G13：质检配置 worker 级读一次，本集所有段共用（上提到循环前，供 _shot_segment
            # 内的提示词预检复用，避免逐段再 _qc_load_cfg() 读盘+解密）。
            qc_cfg = _qc_load_cfg()
            qc_on = qc_client.video_qc_ready(qc_cfg)
            qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("video_enabled"))
            max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
            segs, shot_meta_map = [], []
            # ⭐ H3 Director 公共参考图（2026-09-30，用户拍板）：整集里「每一段都在用、
            #    且用的是同一张图」的资产走插件公共参数（global.refs + commonEnabled），
            #    使同一资产的 <Picture N> 在全集恒定（不再随每镜声明顺序漂移），
            #    也让「公共用哪些资产」在工作流 JSON 里显式可查。
            # 跨集一致性巩固（2026-10-02）：① 上集 state_out 供首镜跨集衔接；
            # ② 本集服装状态 → 衣柜变体 key 覆盖表，供逐镜参考图取变体。
            # 两者加载失败都按「无上集/无覆盖」处理（fail-open，零回归）。
            _prev_ep_state = {}
            _outfit_ovr = {}
            try:
                if _epn and int(_epn) > 1:
                    from config import CONTINUITY_DIR as _cont_dir
                    from continuity import load_state as _load_ep_state
                    # ⚠️ 2026-10-02 修复（off-by-one）：load_state(dir, key, ep) 读的是
                    #    **指定那一集**的 state，取「上集」必须减 1 —— canonical 见
                    #    continuity.py:1343 / :1888 的 `int(episode_no) - 1`。
                    #    原先传 int(_epn)（本集）→ 本集 state 尚不存在时 `or {}` 静默
                    #    退回「无上集」（fail-open 不崩，但锚点是错的）；而本集 state
                    #    已存在（重跑/续跑）时会把**本集**当上集做首镜跨集衔接。
                    _prev_ep_state = _load_ep_state(_cont_dir, project_name,
                                                    int(_epn) - 1) or {}
            except Exception as _ce:  # noqa: BLE001
                app.logger.debug("[跨集衔接] 上集 state 加载失败（按无上集处理）：%s", _ce)
            try:
                _outfit_ovr = _episode_outfit_overrides(
                    project_name, int(_epn or 1)) or {}
            except Exception as _oe:  # noqa: BLE001
                app.logger.debug("[服装变体] 本集覆盖解析失败（不指定变体）：%s", _oe)

            _common, _comps_map = _h3_plan_common_refs(
                shots, char_idx, item_idx, scene_idx,
                character_refs=character_refs, main_char_img=main_char_img,
                sb_map=sb_map, use_storyboard=use_storyboard,
                project_name=project_name)
            _common_keys = {h3_common_refs.asset_key(c) for c in _common}
            _common_keys.discard(None)
            # ⭐ 公共参考音色 + 公共提示词 subject lock（2026-10-02）：统一派生，
            #    预演/正式两个调用点共用同一份，避免两处口径漂移。
            _ensure_voice_bank_refs(_common, project_name, all_characters=char_idx)
            # ⭐ 2026-10-05：_h3_common_ref_audios 现返回 [(角色名, 路径)] 有序对。
            # subject_lock 用完整对（逐行「<Audio N> = 角色名」归属）；build 侧要纯路径列表。
            # ⭐ 2026-10-10（用户方案②）：音色扩到**全角色**（all_characters=char_idx）——
            #    与 L7263 的 _ensure_voice_bank_refs 同源，保证「先确保参考音色存在，
            #    再把它挂进 global.refAudios」这一对动作覆盖同一批角色。
            #    参考图仍维持「全段共用才进公共池」（见 _h3_plan_common_refs）。
            _common_audios = _h3_common_ref_audios(_common, project_name,
                                                   all_names=char_idx)
            _common_audio_paths = [p for (_n, p) in _common_audios]
            # ⭐ 公共提示词补全（2026-10-03）：世界观 + 全局 STYLE 也进公共段（与角色锁定一致
            #    拼在每段提示词前）。世界观优先取 AI 设定面板的 era_world，缺则取项目 brief。
            _cw_style = str(_style_res.get("style") or style or "").strip()
            _cw_aspect = str(_style_res.get("aspect") or _style_res.get("ratio") or "").strip()
            _cw_worldview = _project_worldview(project_name)
            _common_prompt = _h3_common_subject_lock(
                _common, _common_audios,
                style=_cw_style, worldview=_cw_worldview, aspect=_cw_aspect)
            def _build_shots_for(_idxs):
                """构建指定镜头下标集合的 H3 段（长镜切段 / LoRA / 提示词增强 / 预检）。

                ⭐ 2026-10-08（用户要求）：从「一次性建完全部 82 段」抽成**按镜头子集**
                可调，以支持场次级流水线 —— 首场建完即开渲、其余场后台构建，GPU 不再
                空等那约 60 分钟的提示词准备。返回 (segs, metas)，**不直接写**外层
                segs / shot_meta_map（合并由调用方负责，便于后台线程发布）。
                """
                _b_segs, _b_metas = [], []
                for i in _idxs:
                    shot = shots[i]
                    shot_id = shot.get('shot_id', i + 1)
                    seq = _shot_seq(shot_id, i + 1)
                    # 跨集一致性巩固：本镜角色未显式声明服装时，套用本集 state 推出的
                    # 衣柜变体覆盖（_shot_outfit_dir 消费 shot["character_outfits"]）
                    if _outfit_ovr:
                        _co = dict(shot.get("character_outfits") or {})
                        for _cn, _ok in _outfit_ovr.items():
                            _co.setdefault(_cn, _ok)
                        if _co:
                            shot["character_outfits"] = _co
                    # ⭐ ②-A 跨场视觉衔接（2026-10-09，用户反馈「场次之间没有关联」）：
                    #    判定「换场首镜」—— 本镜是所在场的第一镜，且不是全片首场 ——
                    #    就找**上一场最后一镜已入库的分镜图**，作为额外参考图挂进本段。
                    #    这样 H3 在这一段有一个真实的跨场视觉起点（服装/道具/光位接续），
                    #    补上 transition_clause 只有文字、没有画面的缺口。
                    #    取不到（首场 / 上一场末镜未入库）→ 不挂，行为与改动前一致。
                    _x_ref = None
                    try:
                        _scn_now = int(shot.get("scene_no") or 1)
                        _scn_prev = (int(shots[i - 1].get("scene_no") or 1)
                                     if i > 0 else None)
                        if i > 0 and _scn_prev is not None and _scn_prev != _scn_now:
                            _prev_last = None
                            for _k in range(i - 1, -1, -1):
                                if int(shots[_k].get("scene_no") or 1) != _scn_now:
                                    _prev_last = shots[_k]
                                    break
                            if _prev_last is not None:
                                _psq = _shot_seq(_prev_last.get("shot_id"), 1)
                                _sb_root = _ep_dir(
                                    os.path.join(STORYBOARDS_DIR, project_name),
                                    episode_no)
                                _pp = os.path.join(_sb_root, f"shot_{_psq:02d}.png")
                                if os.path.isfile(_pp):
                                    _x_ref = {
                                        "kind": "scene", "path": _pp,
                                        "name": "上一场末镜衔接",
                                        "appearance": "同一场景群内上一场的最后一个分镜图",
                                    }
                                    app.logger.info(
                                        "[视频跨场衔接] shot=%s 换场首镜 → 引入上一场末镜 %s",
                                        shot_id, os.path.basename(_pp))
                    except (TypeError, ValueError, IndexError):
                        _x_ref = None
                    seg, sb_local = _shot_segment(shot, seq, qc_cfg,
                                                  common=_common, common_keys=_common_keys,
                                                  extra_ref=_x_ref)
                    # ⚠️ 整集模式**每个分镜可能产出多个段**（长镜切段，见 _shot_segment）。
                    # 必须 extend 而非 append：H3 工作流段数 = len(segments)，少一段就等于
                    # 该镜只生成了一半时长；且段顺序即时间轴顺序，extend 保持镜头内子段连续。
                    _shot_segs = seg if isinstance(seg, list) else [seg]
                    # 按场次生成（2026-10-03）：段落继承所属镜头的场次号（scene_no），
                    # 视频阶段按场次分组逐场提交，最后拼接成整集。
                    for _ss in _shot_segs:
                        try:
                            _ss.setdefault("scene_no", int(shot.get("scene_no") or 1))
                        except (TypeError, ValueError):
                            _ss.setdefault("scene_no", 1)
                    # 优化#4 段间衔接（2026-10-02 细化版）：逻辑提纯到
                    # h3_prompt_kit.transition_clause —— 同场延续 / 换场 / 机位切换 /
                    # 跨集首镜（用上集 state_out 承接）四种情形各自措辞；只加在本镜
                    # **首段**（镜内子段本就是同镜延续，写「上一镜」反而误导）。
                    if _shot_segs:
                        _link = ""
                        if i > 0:
                            _link = h3_prompt_kit.transition_clause(
                                shots[i - 1] if i - 1 < len(shots) else {}, shot)
                        elif _epn and int(_epn) > 1:
                            _link = h3_prompt_kit.transition_clause(
                                None, shot, _prev_ep_state or {})
                        if _link:
                            _shot_segs[0]["prompt"] = str(
                                _shot_segs[0].get("prompt") or "") + "\n" + _link
                    _b_segs.extend(_shot_segs)
                    # 2026-10-08（用户要求）：视频前置段（切段/LoRA/提示词增强/预检）
                    #    逐镜上报，前端显示「视频提示词 12/41」。
                    try:
                        import autopilot as _ap
                        _ap.report_progress(f"视频提示词 {i + 1}/{len(shots)}")
                    except Exception:  # noqa: BLE001
                        pass
                    # shot_meta_map 是**按镜头**的报表（每镜一条），时长取该镜各子段之和 ——
                    # 与切段前的 seg["duration"] 口径一致，前端/报表不会因切段而变。
                    _b_metas.append({
                        "shot_id": shot_id, "seq": seq,
                        "duration": round(sum(float(s.get("duration") or 0)
                                              for s in _shot_segs), 3),
                        "segment_count": len(_shot_segs),
                        "used_storyboard": bool(sb_local)})
                return _b_segs, _b_metas

            # ⭐ 2026-10-08（用户要求）：场次级流水线 —— 首场提示词建完**立刻开渲**，
            #    其余场的提示词在后台线程继续构建，与 GPU 渲染重叠。
            #    此前「全部 82 段建完才开渲」，那 60 分钟里 GPU 全程空闲。
            #    开关 MJSCXT_VIDEO_STREAM（默认 1）；置 0 回到旧的「先全建后渲」。
            _vstream = (
                str(os.environ.get("MJSCXT_VIDEO_STREAM") or "1").strip().lower()
                not in ("0", "false", "off", "no")
                # ⚠️ 只在「按场次 + 非导出」时启用：导出模式（build_only）与非按场次
                #    退路都直接吃**全量 segs**，而流水线模式下 segs 要等全部场渲完才
                #    聚合（会拿到空段）。两个守卫在此**就地求值** —— _build_only /
                #    _per_scene 的赋值在这段之后，不能引用它们。
                and not bool(build_only)
                and str(os.environ.get("MJSCXT_VIDEO_PER_SCENE") or "1").strip().lower()
                not in ("0", "false", "off", "no")
            )
            _scene_plan = []          # [(scene_no, [shot_idx, ...]), ...]
            for _si2, _sshot in enumerate(shots):
                try:
                    _sn2 = int(_sshot.get("scene_no") or 1)
                except (TypeError, ValueError):
                    _sn2 = 1
                if _scene_plan and _scene_plan[-1][0] == _sn2:
                    _scene_plan[-1][1].append(_si2)
                else:
                    _scene_plan.append((_sn2, [_si2]))
            _vbuilt = {}              # scene_no -> {"ev": Event, "segs": [...], "metas": [...]}
            _vberr = {"err": None}
            if _vstream and len(_scene_plan) > 1:
                for _sn2, _ in _scene_plan:
                    _vbuilt[_sn2] = {"ev": threading.Event(), "segs": [], "metas": []}
                _s0, _i0 = _scene_plan[0]
                _ls0, _lm0 = _build_shots_for(_i0)
                _vbuilt[_s0]["segs"] = _ls0
                _vbuilt[_s0]["metas"] = _lm0
                _vbuilt[_s0]["ev"].set()

                def _vbg_build():
                    """后台构建其余场提示词（纯 LLM / 文件准备，不碰 GPU）。"""
                    try:
                        for _sn3, _idxs3 in _scene_plan[1:]:
                            _ls3, _lm3 = _build_shots_for(_idxs3)
                            _vbuilt[_sn3]["segs"] = _ls3
                            _vbuilt[_sn3]["metas"] = _lm3
                            _vbuilt[_sn3]["ev"].set()
                    except Exception as _be:  # noqa: BLE001
                        _vberr["err"] = _be
                        app.logger.exception("[视频流水线] 后台构建提示词失败：%s", _be)
                    finally:
                        for _sn3, _ in _scene_plan[1:]:
                            _vbuilt[_sn3]["ev"].set()   # 唤醒等待方，绝不让人死等

                threading.Thread(target=_vbg_build, name="h3-prefetch",
                                 daemon=True).start()
                app.logger.info(
                    "[视频流水线] 首场 scene_%02d（%d 镜）建完即开渲；其余 %d 场"
                    "提示词后台并行构建", _s0, len(_i0), len(_scene_plan) - 1)
            else:
                segs, shot_meta_map = _build_shots_for(list(range(len(shots))))

            # ⚠️ 构建循环已抽进 _build_shots_for，外层不再有循环变量 shot ——
            #    这里显式取末镜，避免 NameError（原实现靠循环残留变量，很脆）。
            _last_shot = shots[-1] if shots else None
            # 质检开关结论已在上方 worker 级算好
            eff_style = (_last_shot.get("style") if _last_shot else None) or _style_res.get("style") or ""
            # [教训][video] 诊断（§2.3.5）：qc_off = 质检总开关/类型开关/接口任一未就绪
            # → 整片 QC 门控不会注入（qc_fn=None），本模式**根本不写教训库**，如实打点。
            if not qc_on:
                app.logger.info("[教训][video] project=%s mode=episode qc_off=true "
                                "enabled=%s video_enabled=%s → 无质检门控，本模式不沉淀教训",
                                project_name, qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))

            # 整片 QC 门控回调：对 N 段一次生成出的单个连续整集视频抽帧质检
            _ep_qc_attempt = {"n": 0}

            # ⭐ 2026-10-08：质检抽帧范围 = **当前场**的段（此前误用全 82 段占比去抽
            #    单场视频，抽帧位置必然算错、中段漏检）。渲染循环每场会覆写它。
            _qc_scope = {"segs": []}

            def _seg_qc_fn(video_path, shot_desc, cfg, style):
                """QC 门控：对整片抽帧 → 多模态判定 → 返回 {"passed": bool, "verdict": {...}, "gate": {...}}

                ⚠️ 两点与「单镜模式」必须对齐，否则整集模式（默认）会缺半边能力：
                1) **镜头信息**：comfyui_client 传进来的是所有段 H3 提示词全文拼接
                   （每段六段式，几十段叠一起）——又长又难判读。改用本集镜头摘要。
                2) **教训沉淀**：此前整集模式只做 QC+门控、**从不写教训库**，
                   于是「质检不达标 → 改提示词重生成」的闭环在最常用模式下完全断裂，
                   重试只会换随机种子瞎撞。这里补齐 `_record_qc_lesson`。
                """
                _ep_qc_attempt["n"] += 1
                fr_dir = os.path.join(QC_DIR, project_name, "frames",
                                      f"episode_full_{os.path.basename(video_path)}")
                # D-05（P1）：按「每段中点」抽帧，取代原先「全片均布 3 帧（配置上限 6）」。
                # 原实现下一次产出 20~44 段的整集只抽 3~6 帧，中段几十段零采样 →
                # 崩坏镜必然漏检、整集质检形同虚设。这里把各段时长折算成全片占比传下去，
                # 抽帧数随段数增长（>6），使每一段至少被采到一次。
                _qc_segs = _qc_scope.get("segs") or segs
                _qc_ratios = _episode_frame_ratios(_qc_segs)
                if _qc_ratios:
                    app.logger.info("[整集质检] 按段抽帧：%d 段 → 请求 %d 帧（每段中点占比）",
                                    len(_qc_segs), len(_qc_ratios))
                else:
                    app.logger.warning(
                        "[整集质检] 段时长不可用（segs=%d）→ 退回配置抽帧数（可能漏检中段）",
                        len(_qc_segs))
                # P2-3 逐段主体一致性门禁（2026-10-01 接线）：段时长累加成绝对时间区间，
                # 角色索引里的设定图作为外观锚点，一并交 check_video 按段分组逐段判定。
                # ⚠️ 2026-10-09 修复：期望时长必须与**本次被检产物**同源。
                #    渲染循环每场覆写 _qc_scope（见上方 2026-10-08 注释「抽帧范围 = 当前场」），
                #    被检视频是**当前场**的产物；而此处原先恒用「全集各镜 duration 之和」。
                #    实测后果：第2集第1场实测 13.67s vs 全集期望 42.50s → 偏差 68% 超阈值 60%
                #    → 命中 critical「H3 可能截断/补白」→ 触发换种子**整片重渲**（4 段 H3，单段预估 900s）。
                #    即：一个纯口径错误在烧 GPU 时间，且会掩盖真实质检结论。
                #    与 L7316 同一口径 —— 那处 2026-10-08 已修，时长这处当时漏了。
                _expected_dur = 0.0
                for _es in (_qc_scope.get("segs") or []):
                    try:
                        _expected_dur += float(_es.get("duration") or 0.0)
                    except (TypeError, ValueError):
                        pass
                if _expected_dur <= 0:
                    # 拿不到场段时长才退回全集（例如单镜模式 / 段时长缺失）
                    _expected_dur = sum(float(s.get("duration") or 0.0) for s in shots)

                _seg_ranges = []
                _t_acc = 0.0
                for _s in _qc_segs:
                    try:
                        _d = float(_s.get("duration") or 0.0)
                    except (TypeError, ValueError):
                        _d = 0.0
                    if _d > 0:
                        _seg_ranges.append({"name": str(_s.get("name") or ""),
                                            "start": _t_acc, "end": _t_acc + _d})
                        _t_acc += _d
                _qc_refs = []
                for _v in (char_idx or {}).values():
                    _p = str((_v or {}).get("image") or "")
                    if _p and _p not in _qc_refs:
                        _qc_refs.append(_p)
                _qc_refs = _qc_refs[:4]
                if _seg_ranges:
                    app.logger.info("[整集质检] 逐段一致性门禁：%d 段时间区间 + %d 张角色锚点图",
                                    len(_seg_ranges), len(_qc_refs))
                verdict = qc_client.check_video(video_path, _episode_qc_desc(shots), cfg,
                                               frames_dir=fr_dir,
                                               style=style,
                                               frame_ratio=_qc_ratios or None,
                                               segment_ranges=_seg_ranges or None,
                                               ref_images=_qc_refs or None,
                                               expected_duration=_expected_dur)
                gate = _qc_gate(verdict)
                passed = bool(gate.get("accept", False))
                if not verdict.get("ok"):
                    # P1-18：质检「不可判定」（ffmpeg 缺失 / 接口 5xx 等，与内容无关）——
                    # 不得当作「不达标」触发换种子整片重跑（会白烧 20~44 段 H3；见报告 P1-18）。
                    # 标记 qc_unavailable，交由 _ep_qc_stop_cb 停止重试；口径与「不达标」分开。
                    _ep_qc_attempt["unavailable"] = True
                    app.logger.warning(
                        "[整集质检] 质检不可判定 → qc_unavailable（不重试）："
                        "project=%s attempt=%d error=%s",
                        project_name, _ep_qc_attempt["n"],
                        (verdict.get("error") or gate.get("reason") or ""))
                if not passed and verdict.get("ok"):
                    try:
                        # 抽帧图路径写进历史 extra.frames_f，供产物被移走后做「断链修正」
                        # （见下方 _mark_history_file_purged）。
                        rec = _qc_record_verdict(
                            project_name, "video", episode_tag or "episode", "整片质检",
                            _ep_qc_attempt["n"], None, video_path, verdict, style=style,
                            extra={"frames_f": list(verdict.get("frames") or []),
                                   "frames_dir": fr_dir})
                        # 挂上本集的段提示词集合，供下次重试时按相似度召回
                        # A-5 P1：整片模式段数可达 20~44 段，全量拼接可达数十 KB —— 全量入
                        # 教训库会撑爆/稀释检索。截断到 2000 字符（保留段边界换行，人可读）。
                        _seg_blob = "\n".join((sg.get("prompt") or "") for sg in _qc_segs)
                        _record_qc_lesson(project_name, "video", _seg_blob[:2000], rec)
                        app.logger.info(
                            "[教训][video] project=%s mode=episode attempt=%d ok=True "
                            "passed=False → 已沉淀",
                            project_name, _ep_qc_attempt["n"])
                    except Exception as le:  # noqa: BLE001
                        app.logger.warning("整片质检教训沉淀失败：%s", le)
                elif not passed and not verdict.get("ok"):
                    # [教训][video] 诊断（§2.3.5）：质检调用异常/超时（ok=false）时静默跳过、
                    # 不记教训——视频质检需 ffmpeg 抽帧 + 多模态，失败率高，如实打点便于排障。
                    app.logger.info(
                        "[教训][video] project=%s mode=episode attempt=%d ok=False "
                        "passed=False verdict.ok=false → 质检异常/超时，不沉淀教训",
                        project_name, _ep_qc_attempt["n"])
                # ★ 用户需求：整片质检「判定不通过」的抽帧图不留本地。⚠️ 仅当质检成功返回
                # 且不合格（ok=True、passed=False）时删；ok=False（接口故障/ffmpeg 缺失）时
                # 抽帧图保留供排障。整片成片本身按用户决策 2 保留（在调用方处理）。
                if not passed and verdict.get("ok") and not verdict.get("unavailable"):
                    # 抽帧图整目录移入回收站，并把质检历史里指向它的帧路径一并标记为断链
                    _hist_f = _qc_history_file_for(project_name, "video", episode_tag or "episode")
                    try:
                        _purge_rejected_artifacts(
                            [fr_dir], project=project_name,
                            reason=f"整片质检不合格（{gate.get('label')}）抽帧图",
                            kind="episode_frames", history_file=_hist_f)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"整片抽帧图清理失败（忽略）：{_pe}")
                return {"passed": passed, "verdict": verdict, "gate": gate}

            def _ep_qc_stop_cb(qc_results):
                """G1 止损 + P1-18：质检「不可判定」优先于缺陷重复判定 —— 不可判定不重试。

                与分镜/逐镜/资产的既有口径一致：`not verdict.get("ok")` 时 break（不重画）。
                这里通过止损回调把该语义传达给 comfyui_client 的整片重试循环，避免在质检
                接口/ffmpeg 不可用时换种子白烧整集。
                """
                if _ep_qc_attempt.get("unavailable"):
                    return True, "质检不可判定（接口 / ffmpeg 不可用，与内容无关），不重试"
                return _qc_retry_hopeless(qc_results)

            # P0-1 fail-closed：qc_declared=True 但质检接口未就绪（qc_on=False）→ 整集
            # **不生成、不写正式目录**，直接阻断并如实告警。旧实现在 qc_fn=None 下仍把
            # 未质检成片 move 进正式目录（fail-open），与资产链路口径不一致。
            if qc_declared and not qc_on:
                app.logger.warning(
                    "[整集质检] qc_declared=True 但 qc_on=False → fail-closed 阻断："
                    "整集视频不生成、不写正式目录（project=%s，enabled=%s video_enabled=%s）",
                    project_name, qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": 0,
                        "qc_blocked": True,
                        "qc_unavailable": True,
                        "error": ("整集视频质检阻断（质检接口未就绪）：已开启视频质检，"
                                  "但 base_url / api_key / model 不可用；"
                                  "未质检产物不写入正式目录"),
                    })
                    generation_state[task_id].update({
                        "status": "failed",
                        "success_count": 0,
                        "error": "整集视频质检阻断（质检接口未就绪）",
                    })
                return

            with lock:
                generation_state[task_id].update({
                    "current": 0, "progress": 5,
                    "phase": (f"整集 {len(segs)} 段一次生成（H3 原生衔接，整片 QC 门控）"
                              if not (_vstream and _vbuilt)
                              else f"整集按场次流水线生成（{len(shots)} 镜，首场就绪即开渲）"),
                    "segment_count": len(segs),
                })
            # ---------------- 两级生产（2026-09-29）：预演 → 批准 → 正式 ----------------
            # 开关默认**关** → 整段跳过，行为与改造前一致。
            # 打开后：本集还没有「被批准过的预演」时，只出一版低成本预演就返回，等人工
            # 批准；批准后再跑一次才是正式生产。坏片在廉价档就被拦下，不必等几十分钟。
            # ⚠️ 流水线模式（_vstream）下 segs 要等全部场渲完才聚合，预演会拿到空段 →
            #    就地跳过（预演开关默认关，常规配置不受影响）。
            _pv_gate = preview_gate.enabled()
            if _pv_gate and _vstream:
                app.logger.warning("[预演] 已开启预演，但流水线模式下 segs 未聚合 → 跳过预演")
            if _pv_gate and not _vstream:
                _pv_prefix = preview_gate.preview_prefix(
                    f"comic_drama/{project_name}_{episode_tag or 'episode'}")
                _pv_need, _pv_why = preview_gate.needs_preview(project_name, episode_tag)
                if _pv_need:
                    app.logger.info("[预演] 第%s集先出低成本预演（原因：%s）", episode_tag, _pv_why)
                    with lock:
                        generation_state[task_id].update({
                            "phase": f"整集预演生成中（{len(segs)} 段 · 低分辨率 + 短时长）",
                            "preview": True, "progress": 5})
                    _pv_size = preview_gate.preview_size(_size)
                    _pv_segs = preview_gate.preview_segments(segs)
                    app.logger.info("[预演] 画幅 %s → %s；段数 %d（不变，每镜都看得到）",
                                    _size, _pv_size, len(_pv_segs))
                    _pv_res = None
                    try:
                        _pv_res = comfyui_client.generate_h3_sequence_sequential(
                            segments=_pv_segs,
                            filename_prefix=_pv_prefix,
                            seed=comfyui_job_store.get_or_create_seed(
                                f"preview|{project_name}|{episode_tag or 'episode'}",
                                lambda: random.randint(1, 2 ** 31 - 1),
                                live_key=f"h3|{_pv_prefix}"),
                            timeout_per_segment=timeout_per_segment,
                            size=_pv_size,
                            qc_fn=_seg_qc_fn if qc_on else None,
                            qc_cfg=qc_cfg,
                            qc_style=eff_style,
                            max_retries=0,      # 预演不重试：要改就重出一版预演
                            common_refs=[c["path"] for c in _common if c.get("path")],
                            common_ref_audios=_common_audio_paths,
                            common_prompt=_common_prompt,
                        )
                    except Exception as _pv_err:                       # noqa: BLE001
                        app.logger.error("[预演] 生成失败：%s", _pv_err, exc_info=True)
                    _pv_files = (_pv_res or {}).get("files") or []
                    if not _pv_files:
                        with lock:
                            generation_state[task_id].update({
                                "status": "failed", "success_count": 0,
                                "error": "预演生成失败（未产出可用文件）",
                                "results": [{"success": False, "mode": "episode",
                                             "preview": True, "segment_count": 0,
                                             "error": "预演生成失败"}]})
                        return
                    os.makedirs(videos_dir, exist_ok=True)
                    _pv_dst = os.path.join(
                        videos_dir,
                        f"{episode_tag or 'episode'}{preview_gate.PREVIEW_MARK}1.mp4")
                    try:
                        _move_with_retry(_pv_files[0], _pv_dst)
                    except Exception as _pv_mv:                        # noqa: BLE001
                        app.logger.warning("[预演] 产物迁移失败（沿用原路径）：%s", _pv_mv)
                        _pv_dst = _pv_files[0]
                    # 预演也记四层状态：A 层判技术完成，B 层取本次质检结论（若送检）
                    try:
                        quality_stage.record_stage(
                            project_name, episode_tag, "A",
                            quality_stage.evaluate_technical(_pv_dst).get("status") or "pending",
                            evidence={"preview": True, "path": _pv_dst})
                        _pv_qc = (_pv_res or {}).get("qc_results") or []
                        if _pv_qc:
                            quality_stage.record_stage(
                                project_name, episode_tag, "B",
                                quality_stage.evaluate_content(_pv_qc[-1]).get("status")
                                or "pending", evidence={"preview": True})
                    except Exception as _pv_qs:                        # noqa: BLE001
                        app.logger.warning("[预演] 质量状态记录失败（忽略）：%s", _pv_qs)
                    with lock:
                        generation_state[task_id].update({
                            "status": "awaiting_preview_approval", "progress": 100,
                            "phase": "预演已生成，等待人工批准后再生产正式成片",
                            "results": [{"success": True, "mode": "episode",
                                         "preview": True, "deliverable": False,
                                         "file": _pv_dst, "segment_count": len(_pv_segs),
                                         "approve_hint": "批准预演后重新生成本集，即产出正式成片"}]})
                    app.logger.info("[预演] 已产出预演（不可交付）：%s", _pv_dst)
                    return

            app.logger.info(f"[episode] 整集生成开始：{len(segs)} 段，qc_on={qc_on}，max_retries={max_retries}")

            # 工作流导出模式（2026-10-03）：body 带 build_only=true 时只构建整集 UI
            # 工作流并落盘到 output/workflows_export/<项目>/epNN_h3_director_ui.json，
            # **不提交 ComfyUI、不烧 GPU** —— UI 格式可直接导入 ComfyUI 检查/运行。
            _build_only = bool(build_only)
            _wf_export_path = ""
            if _build_only and str(os.environ.get("MJSCXT_VIDEO_PER_SCENE") or "1").strip().lower() \
                    in ("0", "false", "off", "no"):
                # 仅旧「整集一次提交」模式的导出路径；按场次模式的导出在下方分支内
                _wf_export_path = os.path.join(
                    PROJECT_OUTPUT_DIR, "workflows_export", project_name,
                    f"ep{int(episode_no or 1):02d}_h3_director_ui.json")
                app.logger.info("[episode][build_only] 工作流将导出到：%s", _wf_export_path)

            # ===== 按场次生成（2026-10-03 用户决策，默认开）=====
            # 把整集段落按 scene_no 分组，每场一次 H3 提交（场内保留段间引导与衔接
            # 提示词，场间是自然剪切点）→ scene_XX.mp4；全部场次完成后 ffmpeg concat
            # 拼接成整集。逐场质检重试（比整集重试便宜一个量级）；某场失败只重做该场
            # （已完成场次落盘复用，天然断点续跑）。env MJSCXT_VIDEO_PER_SCENE=0 切回
            # 旧的「整集一次提交」。
            _per_scene = str(os.environ.get("MJSCXT_VIDEO_PER_SCENE") or "1").strip().lower() \
                not in ("0", "false", "off", "no")
            _scene_groups = []
            if _vstream and _vbuilt:
                # 流水线模式：场次表来自**镜头**（提示词还没全建完），段列表在渲染前
                # 按场从 _vbuilt 取（见下方渲染循环）。
                _scene_groups = [(sn, None) for sn, _ in _scene_plan]
            # 串行模式照旧分组；流水线模式传空列表直接跳过本循环（body 缩进不变）。
            for _s in ([] if (_vstream and _vbuilt) else segs):
                try:
                    _sn = int(_s.get("scene_no") or 1)
                except (TypeError, ValueError):
                    _sn = 1
                if _scene_groups and _scene_groups[-1][0] == _sn:
                    _scene_groups[-1][1].append(_s)
                else:
                    _scene_groups.append((_sn, [_s]))

            if _per_scene:
                _ep_name_ps = f"{episode_tag or 'episode'}_full.mp4"
                # 单场重做（2026-10-03）：body only_scenes=[N] 时只生成/重做指定场次；
                # 配合 overwrite=true 可强制重画已有成片的场（断点续跑语义保持）。
                _ovw_ps = bool(overwrite)
                _only_scenes = only_scenes
                if isinstance(_only_scenes, list) and _only_scenes:
                    try:
                        _only_set = {int(x) for x in _only_scenes}
                        _scene_groups = [g for g in _scene_groups if g[0] in _only_set]
                    except (TypeError, ValueError):
                        pass
                app.logger.info("[episode] 按场次生成：%d 场 / %d 段（build_only=%s）",
                                len(_scene_groups), len(segs), _build_only)

                if _build_only:
                    # 导出模式：逐场构建 UI 工作流落盘（零 GPU），供人工在 ComfyUI 检查
                    _scene_reports = []
                    for _sn, _ssegs in _scene_groups:
                        _sp = os.path.join(
                            PROJECT_OUTPUT_DIR, "workflows_export", project_name,
                            f"ep{int(episode_no or 1):02d}_scene{_sn:02d}_ui.json")
                        _r = comfyui_client.generate_h3_sequence_sequential(
                            segments=_ssegs,
                            filename_prefix=(f"comic_drama/{project_name}_"
                                             f"{episode_tag or 'episode'}_s{_sn:02d}"),
                            timeout_per_segment=timeout_per_segment, size=_size,
                            common_refs=[c["path"] for c in _common if c.get("path")],
                            common_ref_audios=_common_audio_paths,
                            common_prompt=_common_prompt,
                            build_only=True, save_build_to=_sp)
                        _wfo = (_r or {}).get("workflow")
                        if _wfo and _sp:
                            os.makedirs(os.path.dirname(_sp), exist_ok=True)
                            atomic_write_json(_sp, _wfo)
                        _scene_reports.append({
                            "scene_no": _sn, "segments": len(_ssegs),
                            "workflow": _sp if (_wfo and os.path.isfile(_sp)) else "",
                            "layout": (_r or {}).get("layout") or {}})
                    _ok_n = sum(1 for _r in _scene_reports if _r.get("workflow"))
                    with lock:
                        generation_state[task_id].update({
                            "status": "completed" if _ok_n == len(_scene_groups) else "failed",
                            "progress": 100,
                            "phase": "工作流已按场次导出（未提交 GPU）",
                            "workflow_dir": (os.path.dirname(_scene_reports[0]["workflow"])
                                             if _ok_n else ""),
                            "results": [{"success": _ok_n == len(_scene_groups),
                                         "build_only": True,
                                         "scenes": _scene_reports}]})
                    app.logger.info("[episode][build_only] 按场次导出完成：%d/%d 场",
                                    _ok_n, len(_scene_groups))
                    return

                _scene_files = []
                _scene_reports = []
                import secrets as _secrets
                for _gi, (_sn, _ssegs) in enumerate(_scene_groups):
                    # ⭐ 2026-10-08：流水线模式 —— 等本场提示词构建完成（后台线程发布），
                    #    并把它设为**本场**质检的段范围。
                    if _vstream and _sn in _vbuilt:
                        _vbuilt[_sn]["ev"].wait(timeout=7200)
                        if _vberr.get("err") is not None:
                            raise RuntimeError(
                                f"视频提示词后台构建失败（scene_{_sn:02d}）："
                                f"{_vberr['err']}")
                        _ssegs = _vbuilt[_sn]["segs"]
                        if not _ssegs:
                            raise RuntimeError(f"视频提示词为空（scene_{_sn:02d}）")
                    _qc_scope["segs"] = _ssegs

                    with lock:
                        generation_state[task_id].update({
                            "phase": (f"按场次生成：第 {_gi + 1}/{len(_scene_groups)} 场"
                                      f"（scene_{_sn:02d}，{len(_ssegs)} 段）"),
                            "progress": int(_gi / max(1, len(_scene_groups)) * 100)})
                    # 2026-10-08（用户要求）：逐场上报「渲染第 N/M 场」，
                    #    前端一眼看出视频在推进（而不是只有「50%」）。
                    try:
                        import autopilot as _ap
                        _ap.report_progress(
                            f"渲染第 {_gi + 1}/{len(_scene_groups)} 场"
                            f"（scene_{_sn:02d}，{len(_ssegs)} 段）")
                    except Exception:  # noqa: BLE001
                        pass
                    _sdst = os.path.join(videos_dir, f"scene_{_sn:02d}.mp4")
                    if (not _ovw_ps) and os.path.isfile(_sdst) and os.path.getsize(_sdst) > 0:
                        # 断点续跑：该场已有成片直接复用（overwrite=true 时强制重画）
                        app.logger.info("[episode][per-scene] 第 %d 场已有成片，复用：%s",
                                        _sn, _sdst)
                        _scene_files.append(_sdst)
                        _scene_reports.append({"scene_no": _sn, "success": True,
                                               "skipped": True, "path": _sdst})
                        continue
                    _sres = comfyui_client.generate_h3_sequence_sequential(
                        segments=_ssegs,
                        filename_prefix=(f"comic_drama/{project_name}_"
                                         f"{episode_tag or 'episode'}_s{_sn:02d}"),
                        seed=comfyui_job_store.get_or_create_seed(
                            f"video|{project_name}|{episode_tag or 'episode'}|scene{_sn}",
                            lambda: _secrets.randbelow(2 ** 31 - 2) + 1,
                            live_key=(f"h3|comic_drama/{project_name}_"
                                      f"{episode_tag or 'episode'}|scene{_sn}")),
                        timeout_per_segment=timeout_per_segment, size=_size,
                        qc_fn=_seg_qc_fn if qc_on else None, qc_cfg=qc_cfg,
                        qc_style=eff_style, max_retries=max_retries,
                        qc_stop_cb=(_ep_qc_stop_cb if qc_on else None),
                        common_refs=[c["path"] for c in _common if c.get("path")],
                        common_ref_audios=_common_audio_paths,
                        common_prompt=_common_prompt)
                    _sf = (_sres or {}).get("files") or []
                    if not _sf or not os.path.isfile(_sf[0]):
                        _scene_reports.append({
                            "scene_no": _sn, "success": False,
                            "error": ((_sres or {}).get("error")
                                      or "ComfyUI 未返回该场成片")})
                        with lock:
                            generation_state[task_id].update({
                                "status": "failed", "success_count": 0,
                                "error": (f"第 {_sn} 场生成失败"
                                          f"（已完成 {_gi}/{len(_scene_groups)} 场；"
                                          "重跑将自动跳过已完成场次）"),
                                "results": [{"success": False,
                                             "mode": "episode_per_scene",
                                             "scenes": _scene_reports}]})
                        return
                    os.makedirs(videos_dir, exist_ok=True)
                    _move_with_retry(_sf[0], _sdst)
                    _scene_files.append(_sdst)
                    _scene_reports.append({"scene_no": _sn, "success": True,
                                           "path": _sdst,
                                           "qc_results": (_sres or {}).get("qc_results") or []})

                # 全部场次完成 → ffmpeg concat 拼接成整集（流复制，无重编码）
                import subprocess as _sp_sub
                from pathlib import Path as _P
                dst = os.path.join(videos_dir, _ep_name_ps)
                # 拼接清单带唯一后缀（2026-10-05）：固定名在同项目并发/重跑时会互踩
                # （A 刚写完清单、B 覆盖成自己的场次列表 → A 拼出错位整集）；
                # finally 只删自己生成的那一个文件（名字存变量）。
                _concat_list = os.path.join(
                    videos_dir,
                    f"_concat_{episode_tag or 'ep'}_{uuid.uuid4().hex[:8]}.txt")
                try:
                    _lines = "".join(
                        "file '" + _f.replace("\\", "/").replace("'", "'\\''") + "'\n"
                        for _f in _scene_files)
                    _P(_concat_list).write_text(_lines, encoding="utf-8")
                    _rc = _sp_sub.run(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                         "-i", _concat_list, "-c", "copy", dst],
                        capture_output=True, text=True, timeout=3600)
                    if (_rc.returncode != 0 or not os.path.isfile(dst)
                            or os.path.getsize(dst) == 0):
                        raise RuntimeError(
                            f"ffmpeg concat 失败 rc={_rc.returncode}: "
                            f"{(_rc.stderr or '')[-300:]}")
                except Exception as _ce:  # noqa: BLE001
                    app.logger.exception("[episode][per-scene] 拼接失败")
                    with lock:
                        generation_state[task_id].update({
                            "status": "failed", "success_count": 0,
                            "error": f"场次已全部生成但拼接失败：{_ce}",
                            "results": [{"success": False, "mode": "episode_per_scene",
                                         "scenes": _scene_reports}]})
                    return
                finally:
                    try:
                        os.remove(_concat_list)
                    except OSError:
                        pass

                # ⭐ 流水线模式：全部场次渲完后聚合段/报表（供 item 与后续统计使用）
                if _vstream and _vbuilt:
                    segs = [s for _sn4, _ in _scene_plan for s in _vbuilt[_sn4]["segs"]]
                    shot_meta_map = [m for _sn4, _ in _scene_plan
                                     for m in _vbuilt[_sn4]["metas"]]

                item = {"success": True, "mode": "episode_per_scene",
                        "scene_count": len(_scene_groups),
                        "segment_count": len(segs),
                        "scenes": _scene_reports,
                        "qc_passed": True, "attempts_used": 1,
                        "path": dst, "url": f"{_vurl}/{_ep_name_ps}",
                        "shots": shot_meta_map,
                        "common_refs": [c.get("name") for c in _common],
                        "qc": _qc_summary([], qc_declared, qc_on, max_retries)}
                item["audio"] = _h3_audio_policy(dst)
                with lock:
                    generation_state[task_id]["results"].append(item)
                    generation_state[task_id]["progress"] = 100
                    generation_state[task_id]["phase"] = (
                        f"按场次生成完成（{len(_scene_groups)} 场已拼接为整集）")
                app.logger.info("[episode][per-scene] 整集拼接完成：%s（%d 场）",
                                dst, len(_scene_groups))
                with lock:
                    results = generation_state[task_id]["results"]
                    ok = sum(1 for r in results if r.get("success"))
                    generation_state[task_id].update({
                        "status": "completed", "success_count": ok, "error": ""})
                return

            result = comfyui_client.generate_h3_sequence_sequential(
                segments=segs,
                filename_prefix=f"comic_drama/{project_name}_{episode_tag or 'episode'}",
                # 崩溃免重渲（2026-09-29）：整集一次提交要跑几十分钟，若中途崩溃/重启，
                # 种子必须还能复原 —— 否则重建出的工作流哈希变了、检查点直接失效。
                # 种子只在「该任务仍在飞」时沿用（见 comfyui_job_store.get_or_create_seed）；
                # 任务一旦完成就换新种子，保证用户主动「重新生成」不会秒回旧片。
                seed=comfyui_job_store.get_or_create_seed(
                    f"video|{project_name}|{episode_tag or 'episode'}|episode",
                    lambda: random.randint(1, 2 ** 31 - 1),
                    live_key=f"h3|comic_drama/{project_name}_{episode_tag or 'episode'}"),
                timeout_per_segment=timeout_per_segment,
                size=_size,
                qc_fn=_seg_qc_fn if qc_on else None,
                qc_cfg=qc_cfg,
                qc_style=eff_style,
                max_retries=max_retries,
                qc_stop_cb=(_ep_qc_stop_cb if qc_on else None),
                # ⭐ 公共参考图（有序本地路径）：客户端先上传 → 写 global.refs
                #   （index 0..K-1）+ commonEnabled=true → 各段的 reference_images
                #   从 index K 起编号。顺序必须与提示词里的 <Picture 1..K> 一致。
                common_refs=[c["path"] for c in _common if c.get("path")],
                # ⭐ 公共参考音色（2026-10-02 取代逐段配音）：公共角色的 voice_bank 参考音
                #   → global.refAudios（index 0..M-1）；无公共角色/无音色 → 空（零行为变更）。
                common_ref_audios=_common_audio_paths,
                # ⭐ 公共提示词 subject lock（2026-10-02）：角色/物品/场景锁定 + 公共音色
                #   指代，编号与 global.refs/refAudios 逐位对齐（<Picture 1..K>/<Audio 1..M>）。
                common_prompt=_common_prompt,
                build_only=_build_only,
                save_build_to=(_wf_export_path or None),
            )
            # 导出模式：工作流落盘即任务完成（跳过成片搬运 / QC / 历史清理全流程）
            if _build_only:
                _wf_obj = (result or {}).get("workflow")
                if _wf_obj and _wf_export_path:
                    os.makedirs(os.path.dirname(_wf_export_path), exist_ok=True)
                    atomic_write_json(_wf_export_path, _wf_obj)
                if _wf_obj and os.path.isfile(_wf_export_path):
                    with lock:
                        generation_state[task_id].update({
                            "status": "completed", "progress": 100,
                            "phase": "工作流已导出（未提交 GPU）",
                            "workflow_path": _wf_export_path,
                            "results": [{"success": True, "build_only": True,
                                         "workflow": _wf_export_path,
                                         "layout": (result or {}).get("layout") or {}}]})
                    app.logger.info("[episode][build_only] 已导出：%s", _wf_export_path)
                else:
                    with lock:
                        generation_state[task_id].update({
                            "status": "failed", "success_count": 0,
                            "error": "build_only 未产出工作流（看后端 [H3-*][build_only] 日志）"})
                return
            files = result.get("files") or []
            episode_failed = bool(result.get("failed"))
            attempts_used = result.get("attempts_used", 1)
            qc_results = result.get("qc_results") or []
            ep_name = f"{episode_tag or 'episode'}_full.mp4"

            if not files:
                _ep_err = result.get("error") or "整片生成失败（ComfyUI 未返回视频文件或整片 QC 全部不通过）"
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": len(segs),
                        "qc_results": qc_results,
                        "error": _ep_err,
                    })
                with lock:
                    generation_state[task_id].update({
                        "status": "failed",
                        "success_count": 0,
                        "error": "整片生成失败或整片 QC 未通过",
                    })
                return

            src = files[0]
            dst = os.path.join(videos_dir, ep_name)
            if os.path.abspath(src) != os.path.abspath(dst):
                _move_with_retry(src, dst)
            qc_passed = not episode_failed
            # ★ 用户决策 2：整集成片**保留现行为** —— 不通过仍写入正式目录（dst）供人工复核，
            # 故 app.py 这里的 fail-open **不动**。但**必须清理 ComfyUI 侧历次重试的整集 mp4**
            # （每轮 attempt 都会在 COMFYUI_OUTPUT_DIR/comic_drama/ 生成一个 `<项目>_<集>_0000N_.mp4`，
            # 不清理就是每次重试堆一个几十分钟的成片）。qc_results[].file 是 comfyui_client
            # 回传的**原始**产物路径（dst 已 move 走，不在其中）。
            try:
                if qc_on and qc_results:
                    _comfy_retries = []
                    for _r in qc_results:
                        if not isinstance(_r, dict):
                            continue
                        _f = _r.get("file")
                        # 只清「质检成功返回且判定不合格」的轮次。comfyui_client 写入的
                        # qc_results 条目里：接口故障轮 unavailable=True 且 passed=None；
                        # 正常不合格轮 passed=False（`is False` 严格判等，None 不命中）。
                        _ok_true = _r.get("passed") is False and _r.get("unavailable") is not True
                        if _f and _ok_true:
                            _comfy_retries.append(_f)
                    if _comfy_retries:
                        _purge_rejected_artifacts(
                            _comfy_retries, project=project_name,
                            reason="整集视频历次质检不合格重试残留",
                            kind="episode_video_retry")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"整集重试残留清理失败（忽略）：{_pe}")
            # A-1 P1：整片 QC 调用异常（comfyui_client 已改「break + 追加 unavailable 条目」，
            # 不再触碰 app.py 的 _ep_qc_attempt 闭包）时，仅看闭包会漏判 → 结果/UI 会误报
            # 「QC 不通过」。这里同时看 qc_results 里是否存在 unavailable 条目，口径与闭包对齐。
            qc_unavailable = bool(_ep_qc_attempt.get("unavailable")) or \
                any(isinstance(r, dict) and r.get("unavailable") for r in qc_results)
            item = {"success": True, "mode": "episode",
                    "segment_count": len(segs),
                    "qc_passed": qc_passed,
                    "qc_unavailable": qc_unavailable,
                    "attempts_used": attempts_used,
                    "path": dst, "url": f"{_vurl}/{ep_name}",
                    "shots": shot_meta_map,
                    # 公共参考图（H3 Director 公共参数）：名字列表，便于前端/排查时
                    # 一眼看到「本集把哪几项锁成公共底图」（2026-09-30）。
                    "common_refs": [c.get("name") for c in _common],
                    "common_enabled": bool(_common) and not result.get("common_inline"),
                    "common_inline": bool(result.get("common_inline")),
                    "qc": _qc_summary([], qc_declared, qc_on, max_retries),
                    "qc_results": qc_results,
                    "prompt_id": result.get("prompt_id")}
            # H3 音轨策略
            item["audio"] = _h3_audio_policy(dst)
            with lock:
                generation_state[task_id]["results"].append(item)
                generation_state[task_id]["progress"] = 100
                generation_state[task_id]["phase"] = (
                    f"整集 {len(segs)} 段视频生成完成（QC 通过）" if qc_passed
                    else (f"整集 {len(segs)} 段视频生成（QC 不可判定，已停止重试，保留成片供人工复核）"
                          if qc_unavailable else
                          f"整集 {len(segs)} 段视频生成（QC 未通过，保留最后生成成片供人工复核）"))
            app.logger.info(f"[episode] 产物落盘: {dst}（qc_passed={qc_passed}，"
                            f"qc_unavailable={qc_unavailable}，attempts={attempts_used}）")
            with lock:
                results = generation_state[task_id]["results"]
                ok = sum(1 for r in results if r.get("success"))
                generation_state[task_id].update({
                    "status": "completed" if ok and qc_passed else "failed",
                    "success_count": ok,
                    "error": ("" if qc_passed else
                              ("整片质检不可判定（qc_unavailable，已停止重试；已保留成片供人工复核）"
                               if qc_unavailable else "整片 QC 未通过（已保留最后生成成片）")),
                })
            return

    except cancellation.Cancelled:
        # 审计 P1-2：与上面逐镜循环同理 —— Cancelled 不是失败，必须穿透到
        # _video_generate_worker 外壳的 cancelled 分支（pipeline._run_task_worker
        # 的「中止信号不重试」保护也依赖它原样上抛）。
        raise
    except Exception as e:
        app.logger.error(f"视频生成失败: {e}")
        # B-16 P2-11：视频任务异常 → 清理本任务产生的视频 scratch 中间产物
        _cleanup_scratch_dir(os.path.join(QC_DIR, project_name, "video_scratch"), app.logger)
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})


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


@app.route('/api/videos/<path:filename>')
def api_video_file(filename):
    """提供视频文件访问"""
    return _serve_safe(VIDEOS_DIR, filename, conditional=True)


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


@app.route('/api/episode/scenes', methods=['GET'])
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


@app.route('/api/videos/preview/approve', methods=['POST'])
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
            app.logger.warning("查找预演产物失败：%s", e)
    if not path or not os.path.isfile(path):
        return jsonify({"success": False,
                        "error": "未找到该集的预演产物（请先生成预演）"}), 404
    state = preview_gate.approve(project, ep, path, note=str(data.get('note') or ''))
    app.logger.info("[预演] 已批准：%s 第%s集 → %s", project, ep, os.path.basename(path))
    return jsonify({"success": True, "preview": state, "path": path})


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

@app.route('/api/autonomous/start', methods=['POST'])
@_autopilot_guard
def api_autonomous_start():
    """一键启动全自动生产：上传小说后，AI对话定风格，然后一键启动24h自动生产"""
    data = request.json or {}
    # A-01（F-01）：统一走 _project_or_400（缺失/越界 → 400，不落共享 'project' 命名空间）
    project_name, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    novel_id = str(data.get('novel_id') or '').strip()
    plan_overrides = {k: v for k, v in data.items()
                      if k in ('style', 'target_shots', 'video_mode', 'enable_assets',
                               'enable_keyframe', 'enable_video', 'enable_final',
                               'enable_tts', 'enable_tts_pre', 'enable_mix',
                               'step_max_retries',
                               # ⭐ 2026-10-10：集间流水线开关（界面可勾选，默认开）
                               #    True = 本集烧 GPU 时后台预热下一集剧本（纯 LLM，不抢卡）
                               'prewarm_next_script',
                               # ⭐ 2026-10-10：资产提示词预热（界面可勾选，默认开）。
                               #    True = 本批资产生成期间后台预热**本批全部**资产的增强
                               #    提示词（只填缓存，零 GPU、不落产物）。
                               'prewarm_asset_prompt',
                               # 文学剧本自动生成（界面可勾选，默认开）
                               'auto_screenplay')}

    if not novel_id and project_name:
        # 尝试从现有计划获取 novel_id
        import autopilot as _ap
        plan = _ap.get_plan(project_name)
        novel_id = plan.get('novel_id', '')

    if not novel_id:
        return jsonify({"success": False, "error": "缺少 novel_id，请先上传小说"}), 400

    _g = _style_aspect_guard(project_name, override_style=str(plan_overrides.get('style') or ''))
    if _g is not None:
        return _g

    result = autonomous.start_autonomous(project_name or novel_id, novel_id, plan_overrides)
    # D5：返回给前端的错误统一脱敏，避免把异常栈/模块名直接显示在错误框里
    if isinstance(result, dict) and result.get('error'):
        result['error'] = _friendly_error(result['error'])
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code




@app.route('/api/autonomous/stop', methods=['POST'])
@_autopilot_guard
def api_autonomous_stop():
    """停止全自动生产"""
    data = request.json or {}
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    result = autonomous.stop_autonomous(project)
    return jsonify(result)


@app.route('/api/autonomous/resume', methods=['POST'])
@_autopilot_guard
def api_autonomous_resume():
    """恢复全自动生产"""
    data = request.json or {}
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    result = autonomous.resume_autonomous(project)
    return jsonify(result)


@app.route('/api/autonomous/status', methods=['GET'])
@_autopilot_guard
def api_autonomous_status():
    """查询全自动生产状态"""
    project, err = _project_or_400(request.args.get('project_name', ''))
    if err is not None:
        return err
    result = autonomous.status(project)
    return jsonify({"success": True, **result})


@app.route('/api/autonomous/chat', methods=['POST'])
@_autopilot_guard
def api_autonomous_chat():
    """AI 对话指令解析：将用户的自然语言指令转化为生产动作"""
    data = request.json or {}
    message = str(data.get('message') or '').strip()
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err

    if not message:
        return jsonify({"success": False, "error": "消息不能为空"}), 400

    result = autonomous.interpret_chat_command(message, project)
    return jsonify(result)


@app.route('/api/autonomous/report/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report(project_name):
    """获取生产报告"""
    project, err = _project_or_400(project_name)
    if err is not None:
        return err
    episode_no = request.args.get('episode_no', type=int)
    result = autonomous.generate_report(project, episode_no)
    return jsonify(result)


@app.route('/api/autonomous/report/export/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report_export(project_name):
    """导出生产报告"""
    project, err = _project_or_400(project_name)
    if err is not None:
        return err
    fmt = request.args.get('format', 'json')
    filepath = autonomous.export_report(project, fmt)
    if not filepath:
        return jsonify({"success": False, "error": "暂无生产记录"}), 404
    return send_file(filepath, as_attachment=True,
                     download_name=os.path.basename(filepath))


@app.route('/api/autonomous/projects', methods=['GET'])
@_autopilot_guard
def api_autonomous_projects():
    """列出所有有生产记录的项目"""
    projects = autonomous.list_all_projects()
    return jsonify({"success": True, "projects": projects})


@app.route('/api/autonomous/deliverables/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_deliverables(project_name):
    """获取项目的交付物列表"""
    project, err = _project_or_400(project_name)
    if err is not None:
        return err
    deliverables = autonomous.get_deliverables(project)
    return jsonify({"success": True, "project": project, "deliverables": deliverables})


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






