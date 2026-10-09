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
# ---- scripts 域（2026-10-09 三步法搬迁）----
from routes.scripts import scripts_bp
app.register_blueprint(scripts_bp)
# ---- scenes 域（2026-10-09 三步法搬迁）----
from routes.scenes import scenes_bp
app.register_blueprint(scenes_bp)
# ---- keyframes 域（2026-10-09 三步法搬迁）----
from routes.keyframes import keyframes_bp
app.register_blueprint(keyframes_bp)
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


def _h3_audio_policy(path: str) -> dict:
    """按 config 的 H3 音轨策略处理刚生成的视频，返回可展示的音频处理记录。

    - `H3_STRIP_AUDIO=True`  → 剥离音轨（2026-09-17 之前的旧行为）
    - 否则 `H3_EMIT_AUDIO=True` → **保留** H3 原生音轨（环境音/打斗音效），
      并顺带保证「每镜都有音轨」：缺的补一条静音轨。
      必须补齐的理由：成片拼接用 concat demuxer + `-c copy`，
      音轨时有时无会让拼接错位甚至失败。
    """
    if H3_STRIP_AUDIO:
        strip = ensure_no_audio(path, backup=True)
        return {
            "policy": "strip",
            "has_audio_before": strip.get("has_audio_before"),
            "has_audio_after": strip.get("has_audio_after"),
            "changed": strip.get("changed"),
            "method": strip.get("method"),
            "backup": strip.get("backup"),
            "message": strip.get("message") or ("已剥离音轨" if strip.get("changed") else ""),
            "error": strip.get("error"),
        }
    pad = ensure_audio_track(path) if H3_EMIT_AUDIO else None
    pad = pad or {}
    if pad.get("error"):
        msg = f"音轨处理异常（已保留原状）：{pad.get('error')}"
    elif pad.get("changed"):
        msg = "原无音轨，已补静音轨（保证拼接一致）"
    elif H3_EMIT_AUDIO:
        msg = "音轨保留（H3 原生音效）"
    else:
        msg = "未做音轨处理"
    return {
        "policy": "keep" if H3_EMIT_AUDIO else "keep_as_is",
        "has_audio_before": pad.get("has_audio_before"),
        "has_audio_after": pad.get("has_audio_after"),
        "changed": pad.get("changed"),
        "method": pad.get("method"),
        "message": msg,
        "error": pad.get("error"),
    }

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
def _cleanup_scratch_dir(dir_path: str, logger=None) -> None:
    """清空目录内容（保留目录本身），失败时只记日志不抛异常。"""
    import logging
    _log = logger or logging.getLogger(__name__)
    if not dir_path or not os.path.isdir(dir_path):
        return
    try:
        for fn in os.listdir(dir_path):
            fp = os.path.join(dir_path, fn)
            try:
                if os.path.isdir(fp):
                    import shutil
                    shutil.rmtree(fp, ignore_errors=True)
                else:
                    os.remove(fp)
            except OSError:
                _log.warning("清理中间产物失败：%s", fp)
        _log.info("已清理 scratch 目录：%s", dir_path)
    except OSError as e:
        _log.warning("清理 scratch 目录失败：%s", e)


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
_PURGE_REJECTED_ENV = "MJSCXT_PURGE_REJECTED"


def _purge_rejected_enabled() -> bool:
    """不合格产物清理开关。默认开启；设 `MJSCXT_PURGE_REJECTED=0` 时只记日志不移走。"""
    v = str(os.environ.get(_PURGE_REJECTED_ENV, "1")).strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _reject_artifact(paths, project: str = "", reason: str = "", kind: str = "") -> dict:
    """质检不合格产物 → 移入回收站（可恢复），**绝不硬删**。

    返回 ``{"moved": [...], "skipped": [...], "failed": [...]}``，三态都带
    `{"src":..., "why":...}`（moved 项另带 `dst`）。

    安全闸（任一不满足即跳过并记日志，绝不抛异常）：
      · 只接受绝对路径（相对路径无法可靠判边界，直接拒绝）；
      · `os.path.normpath(os.path.abspath(p))` 归一 —— 本项目已知坑：混合分隔符
        （`/` 与 `\\`）会让外部 API 静默匹配失败，必须先归一；
      · 必须落在 PROJECT_OUTPUT_DIR 或 COMFYUI_OUTPUT_DIR 之内（越界拒绝）；
      · 显式排除 PROJECT_TRASH_DIR（含 `_backup_*` / `_watermark_backup` 同理越界/排除）；
      · `os.path.lexists` 判存在 —— episode 成片 `move` 后 src 已消失是常态，
        不存在即跳过，**不能当异常**；
      · `os.stat().st_nlink == 1` 校验（硬链接不释放空间，见 disk_reclaim.py 的教训）；
      · 文件与目录都支持（目录走 shutil.move）。

    全程 try/except：清理是优化而非功能，**任何失败都不阻断主流程**，只 logger.warning。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    paths = [p for p in (paths or []) if p]
    if not paths:
        return res

    # 归一化的边界根（含尾分隔符，防止 /output 误匹配 /output2）
    def _root_ok(p: str) -> bool:
        cands = []
        for root in (PROJECT_OUTPUT_DIR, COMFYUI_OUTPUT_DIR):
            if root:
                try:
                    cands.append(os.path.normpath(os.path.abspath(root)) + os.sep)
                except Exception:  # noqa: BLE001
                    pass
        return any(p == c.rstrip(os.sep) or p.startswith(c) for c in cands)

    trash_abs = ""
    try:
        if PROJECT_TRASH_DIR:
            trash_abs = os.path.normpath(os.path.abspath(PROJECT_TRASH_DIR))
    except Exception:  # noqa: BLE001
        trash_abs = ""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "qc_reject",
                              f"{stamp}_{_safe_project(project or 'project')}")
    enabled = _purge_rejected_enabled()

    for raw in paths:
        try:
            if not os.path.isabs(raw):
                res["skipped"].append({"src": str(raw), "why": "非绝对路径"})
                app.logger.warning(
                    "[质检清理] 跳过：非绝对路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, raw)
                continue
            p = os.path.normpath(os.path.abspath(raw))
            if trash_abs and (p == trash_abs or p.startswith(trash_abs + os.sep)):
                res["skipped"].append({"src": p, "why": "回收站内，跳过"})
                continue
            if not _root_ok(p):
                res["skipped"].append({"src": p, "why": "越界（不在 output/ComfyUI 目录内）"})
                app.logger.warning(
                    "[质检清理] 拒绝越界路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            if not os.path.lexists(p):
                res["skipped"].append({"src": p, "why": "不存在"})
                continue
            try:
                if os.stat(p).st_nlink > 1:
                    res["skipped"].append({"src": p, "why": "硬链接（不释放空间）"})
                    app.logger.warning(
                        "[质检清理] 跳过硬链接（project=%s kind=%s reason=%s path=%s）",
                        project, kind, reason, p)
                    continue
            except OSError as se:
                res["failed"].append({"src": p, "why": f"stat 失败：{se}"})
                continue
            if not enabled:
                res["skipped"].append({"src": p, "why": f"{_PURGE_REJECTED_ENV}=0（仅记日志）"})
                app.logger.warning(
                    "[质检清理] 开关关闭，仅记日志不移走（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            os.makedirs(trash_root, exist_ok=True)
            base = os.path.basename(p.rstrip(os.sep)) or "artifact"
            dst = os.path.join(trash_root, base)
            # 同名冲突（同项目多次重试同名）→ 加序号，绝不覆盖已在回收站里的证据
            _n = 1
            while os.path.lexists(dst):
                stem, ext = os.path.splitext(base)
                dst = os.path.join(trash_root, f"{stem}__{_n}{ext}")
                _n += 1
            _move_with_retry(p, dst)
            res["moved"].append({"src": p, "dst": dst})
            app.logger.warning(
                "[质检清理] 不合格产物已移入回收站（project=%s kind=%s reason=%s）：%s → %s",
                project, kind, reason, p, dst)
        except Exception as e:  # noqa: BLE001  清理绝不能中断生产
            res["failed"].append({"src": str(raw), "why": f"{type(e).__name__}: {e}"})
            app.logger.warning("[质检清理] 移入回收站失败（已忽略，不影响主流程）：%s: %s",
                               type(e).__name__, e)
    return res


def _purge_rejected_artifacts(paths, project: str = "", reason: str = "", kind: str = "",
                              history_file: str = "") -> dict:
    """``_reject_artifact`` 的语义化包装：移走后顺手把「质检历史 file 断链」补掉。

    ⚠️ 质检历史 json 是排查依据，**只把断链的 `file` 字段置 null + 打 `purged` 标记**，
    绝不删除历史记录本身（否则事后无法查「当时为什么不合格」）。
    """
    out = _reject_artifact(paths, project=project, reason=reason, kind=kind)
    moved_srcs = {os.path.normpath(m["src"]) for m in out.get("moved") or []}
    if moved_srcs and history_file:
        try:
            _mark_history_file_purged(history_file, moved_srcs)
        except Exception as e:  # noqa: BLE001
            app.logger.warning("[质检清理] 质检历史 file 断链标记失败（忽略）：%s", e)
    return out


def _mark_history_file_purged(history_file: str, moved_srcs: set) -> None:
    """把质检历史里指向**已移走路径**的 `file` / `frames_f` 记为 null 并打 `purged=true`。

    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据，只是不再指向
    已不存在的本地文件（否则前端/排障脚本按路径取会 404）。
    """
    if not history_file or not os.path.isfile(history_file):
        return
    with open(history_file, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    hit = False
    for rec in (data.get("records") or []):
        if not isinstance(rec, dict):
            continue
        fp = rec.get("file")
        if fp and os.path.normpath(os.path.abspath(str(fp))) in moved_srcs:
            rec["file"] = None
            rec["purged"] = True
            hit = True
        # 抽帧图（整片质检）：整目录被移走 → 逐条把已消失的帧路径剔除
        frames = rec.get("frames_f")
        if isinstance(frames, list) and frames:
            kept = [x for x in frames
                    if os.path.normpath(os.path.abspath(str(x))) not in moved_srcs]
            if len(kept) != len(frames):
                rec["frames_f"] = kept
                rec["frames_purged"] = True
                hit = True
    if hit:
        atomic_write_json(history_file, data)
        app.logger.warning("[质检清理] 已标记质检历史断链：%s", history_file)


def _purge_prompt_records(project: str, shot_key, reason: str = "") -> dict:
    """提示词预检不通过 → 移走该镜**唯一落盘物** `prompt_<shot>.json`（P12）。

    用户决策 1：``prompt_qc.preflight`` 是生成前的文本合规检查，不通过直接阻断不生成，
    因此没有图片/视频可删 —— 只有这份提示词历史 json 留在了本地。
    """
    try:
        key = qc_client.safe_token(shot_key, "0")
        path = os.path.join(QC_DIR, _safe_project(project or "project"), f"prompt_{key}.json")
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检清理] 提示词落盘物路径解析失败（忽略）：%s", e)
        return {"moved": [], "skipped": [], "failed": []}
    return _reject_artifact([path], project=project, reason=reason or "提示词预检未通过",
                            kind="prompt")


def _purge_sb_refs(project: str) -> dict:
    """分镜参考图上传残留（ComfyUI output/sb_ref_*.png）按项目清理（P13）。

    ⚠️ 与「不合格产物」是**两码事**：`sb_ref_*` 是分镜**参考图输入**（上传给
    LoadImageOutput 的中间件），不是质检产物，**绝不能混进普通「不合格即删」逻辑**
    （那会在单镜重试中途删掉当前镜头正在用的参考图）。按用户决策 4：只在**本轮分镜
    批量生成循环全部结束后**调用一次，按项目前缀收口。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    try:
        root = COMFYUI_OUTPUT_DIR
        if not root or not os.path.isdir(root):
            return res
        # ⚠️ 2026-09-29 前置安全检查（实测根因，用户 09-29 日志）：
        # 「收尾调用一次」这个前提在**异常收尾**时不成立 —— 某镜 wait_for_completion
        # 超时返回、或批次被中止信号打断时，任务其实**仍在 ComfyUI 队列里跑/等待**。
        # 此时照常清理 `sb_ref_*`，那些任务执行到 LoadImage 就报
        # FileNotFoundError（实测 shot_05~11 连续 7 镜全灭，每镜 0.01s 失败）。
        # 故队列非空、或队列状态查不到（ok=False，信息不明）时**跳过本轮清理**：
        # 宁可留残留（有 _maybe_reclaim_comfyui_output 滚动兜底），
        # 也不删正在被引用的参考图。
        _q = comfyui_client.queue_state()
        if not _q.get("ok") or _q.get("running") or _q.get("pending"):
            app.logger.warning(
                "[质检清理] 跳过 sb_ref 清理（project=%s）：ComfyUI 队列 运行=%s / 等待=%s"
                "（查询ok=%s）—— 队列里可能仍有引用这些参考图的任务，"
                "删掉会让它们 LoadImage 报 FileNotFoundError",
                project, _q.get("running"), _q.get("pending"), _q.get("ok"))
            return res
        # generate_storyboard 的命名：sb_ref_<filename_prefix 的 basename>_<idx>.png，
        # 分镜链路 filename_prefix 形如 `comic_drama_sb/<项目>_shot_NN[...]`，
        # 故前缀里含 `<项目>_shot_`。
        pref = f"sb_ref_{_safe_project(project or '')}_shot_"
        targets = []
        for fn in os.listdir(root):
            if fn.startswith(pref) and fn.lower().endswith(".png"):
                targets.append(os.path.join(root, fn))
        if not targets:
            return res
        res = _reject_artifact(targets, project=project,
                               reason="分镜参考图上传残留（本轮分镜生成结束）",
                               kind="sb_ref")
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检清理] sb_ref 清理失败（忽略，不影响生产）：%s: %s",
                           type(e).__name__, e)
    return res


# B-14 P2-4：资产「取图判据」统一入口。就绪判据（_collect_asset_refs 的
# _first_nonempty_image）与取图判据（_build_asset_index 的 _first_existing）
# 此前各自维护一套「判有图」逻辑，口径漂移（一个只认 4 个扩展名、另一个只
# 认 front/base 固定名）。统一为：扩展名白名单 + 取第一张非空图片。
_ASSET_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

#: 资产目录内「主视角图」的取图优先级（2026-09-24 修复）。
#: ⚠️ 实测 bug：原来只按**文件名字典序**取第一张非空图，而角色资产目录里
#:    back.png < base.png < front.png < left.png < right.png —— 于是 `back.png`
#:    （**背面图**）被当成「主角锚点」喂给分镜/视频链路（_build_asset_index 在
#:    剧本 characters 没有 front/base 字段时就走这条兜底，autopilot 正是这种形态）。
#:    当时看不出来，是因为 4 张视角图与 base 内容完全一致（都是同一张三视图整图）；
#:    一旦视角图变成真单机位（2026-09-24 sheet_split 改造后就是如此），
#:    这个字典序兜底就会静默地把每个镜头的角色锚点换成「只有背面」。
#: 故改为显式优先级：正面 > 整图 > 左侧 > 右侧 > 背面 > 其它图片（字典序）。
_ASSET_IMG_PRIORITY = ("front.png", "base.png", "front.jpg", "base.jpg",
                       "left.png", "right.png", "back.png",
                       "left.jpg", "right.jpg", "back.jpg")


def _first_existing_asset_image(directory: str) -> str:
    """在目录内取「主视角」图片（扩展名白名单），无则返回 ''。

    统一判据：
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")；
      2) 按 :data:`_ASSET_IMG_PRIORITY` 显式优先级取（**不是**文件名字典序，
         否则 back.png 会排在 base/front 之前被取走，详见该常量注释）；
      3) 优先级名都不存在时，再按字典序取第一张非空图片；
      4) 找不到任何图片 → 返回 ''，调用方自行决策（报错/跳过）。
    """
    if not directory or not os.path.isdir(directory):
        return ""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return ""

    def _ok(fn: str) -> bool:
        return (fn.lower().endswith(_ASSET_IMG_EXTS)
                and os.path.isfile(os.path.join(directory, fn))
                and os.path.getsize(os.path.join(directory, fn)) > 0)

    for cand in _ASSET_IMG_PRIORITY:
        if cand in entries and _ok(cand):
            return os.path.join(directory, cand)
    for fn in entries:
        if _ok(fn):
            return os.path.join(directory, fn)
    return ""


#: 「物品上的主人照片」参考图取图优先级（2026-10-06 T02「物品主人形象」链路）。
#: ⚠️ **刻意排除 base.png**：角色 base 图是「正/侧/背三视图整图」，贴进物品照片区域
#:    会把 3 个人物一起搬过去（T02 明确要求排除）。正面档 front.png 才是单人全身图，
#:    half.png（半身胸像）作次选。
_ITEM_OWNER_REF_PRIORITY = ("front.png", "front.jpg", "half.png", "half.jpg")


# ---- 项目助手已下沉到 routes/projects.py（2026-10-08 第五批）----
from routes.projects import _collect_project_cast_images, _cover_prompt_from_outline, _move_with_retry, _project_cover_path  # noqa: F401  再导出


def _sb_heal_comfyui(task_id: str, project: str) -> bool:
    """分镜/资产 worker 里的 ComfyUI 自愈：连续拒连/未出图时自动重启 ComfyUI 进程。

    背景（2026-10-08）：ComfyUI 在「后端超时 → 全局 /interrupt」后可能进入半死状态
    （webserver 仍监听 8188 但执行器被打断），后续镜头 /upload/image 全部 10061 拒连。
    本函数在「该镜头 ComfyUI 未出图且 ComfyUI 不在线」时重启 ComfyUI 并等它就绪，
    让下一镜头（或本镜头重试）能继续，而非让整集镜头全挂。

    返回 True = 已重启且就绪（可继续），False = 重启失败/ComfyUI 仍离线（按离线处理）。
    全程 fail-open，不抛异常。
    """
    global _comfyui_heal_count
    try:
        _comfyui_heal_count = getattr(globals(), "_comfyui_heal_count", 0) + 1
        app.logger.warning(
            "[自愈] 镜头 ComfyUI 未出图且离线，自动重启 ComfyUI（第 %d 次，项目=%s，任务=%s）",
            _comfyui_heal_count, project, task_id)
        ok = comfyui_client.restart_comfyui(wait_sec=180, poll_sec=3.0)
        if ok:
            app.logger.info("[自愈] ComfyUI 重启成功，继续分镜生成")
        else:
            app.logger.warning("[自愈] ComfyUI 重启后仍未就绪，本镜头继续按离线处理")
        return ok
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[自愈] ComfyUI 重启异常（按离线处理）：%s", e)
        return False


def _item_owner_ref_image(project_name: str, owner) -> str:
    """解析「物品主人」在角色资产库里的**单人**参考图路径（取不到返回 ""）。

    用途（T02「物品主人形象」链路）：工牌 / 证件照 / 告示人像 / 屏幕人像上的肖像
    应当是**物品的主人本人**，故需取该主人角色设定图作为参考图做图生图（走
    ``comfyui_client.generate_item_base_with_ref`` → ``item_ref_gen`` 模板）。

    匹配规则（**禁止裸字符串相等**）：
      用 :func:`asset_name_match.match`（kind="character"，会剥 `_主角/_角色/_主/_人`
      后缀、归一全半角/标点）把 ``owner`` 解析到角色资产索引的规范键。
      未命中 → 记 INFO 日志并返回 ""（调用方回落纯 T2I，绝不静默误配）。

    取图优先级（**必须排除 base.png**）：
      1) ``front.png``（角色正面档，单人全身）→ ``half.png``（半身胸像）；
      2) 都不存在 → 该角色目录内**第一张可用图**（字典序 + 扩展名白名单，
         绕过 ``base.*``），口径复用 :data:`_ASSET_IMG_EXTS` /
         :func:`_first_existing_asset_image`（不自造一套判据）。
    """
    owner_s = str(owner or "").strip()
    if not owner_s:
        return ""
    proj_dir = os.path.join(CHARACTERS_DIR, project_name)
    if not os.path.isdir(proj_dir):
        return ""
    # 角色资产索引：目录名 → 目录绝对路径（角色资产以「目录」为单位）
    try:
        char_dirs = {d: os.path.join(proj_dir, d) for d in os.listdir(proj_dir)
                     if os.path.isdir(os.path.join(proj_dir, d))}
    except OSError:
        return ""
    key, level = asset_name_match.match(owner_s, char_dirs, "character")
    if not key:
        app.logger.info("[物品主人形象] 主人「%s」未匹配到角色资产（项目 %s），回落纯 T2I",
                        owner_s, project_name)
        return ""
    if level != asset_name_match.LEVEL_EXACT:
        app.logger.info("[物品主人形象] 主人「%s」经 %s 匹配到角色「%s」（项目 %s）",
                        owner_s, level, key, project_name)
    char_dir = char_dirs[key]
    # 优先级取图（排除 base.png）
    for cand in _ITEM_OWNER_REF_PRIORITY:
        p = os.path.join(char_dir, cand)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    # 兜底：目录内第一张可用图（**跳过 base.* **，三视图整图会带进 3 个人物）
    _base_names = ("base.png", "base.jpg", "base.jpeg", "base.webp")
    try:
        entries = sorted(os.listdir(char_dir))
    except OSError:
        return ""
    for fn in entries:
        if fn.lower() in _base_names:
            continue
        p = os.path.join(char_dir, fn)
        if (fn.lower().endswith(_ASSET_IMG_EXTS) and os.path.isfile(p)
                and os.path.getsize(p) > 0):
            return p
    app.logger.info("[物品主人形象] 角色「%s」目录内无可用单人参考图（已排除 base），"
                    "回落纯 T2I（项目 %s）", key, project_name)
    return ""


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


def _ep_of_script(script: dict, fallback=None):
    """从剧本里取集号（metadata.episode_no > 顶层 episode_no > 兜底）"""
    if not isinstance(script, dict):
        return fallback
    meta = script.get("metadata") or {}
    for v in (meta.get("episode_no"), script.get("episode_no"), fallback):
        try:
            if v is not None and str(v).strip() != "":
                return int(v)
        except (TypeError, ValueError):
            continue
    return fallback


def _episode_schema_defaults(project_name: str, shots: list) -> dict:
    """⑥ 下游链路自动引用剧本自动判定的「镜头数 / 每集时长」字段。

    - 镜头缺 duration 时按项目配置的 duration_per_shot 兜底；
    - 返回 episode_stats（shot_count / duration_sec / episode_plan）供接口回显与后续步骤使用。
    """
    cfg = {}
    try:
        cfg = project_store.read_config(project_name)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取项目配置失败（{project_name}）：{e}")
    per_shot = cfg.get("duration_per_shot") or 5
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        if not s.get("duration"):
            s["duration"] = per_shot
    return novel_to_script.build_episode_stats(shots)


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
def _resolve_static_dir():
    """定位前端静态目录（app/static），兼容源码运行与 PyInstaller 单文件打包。

    2026-09-30 修复「桌面版页面出不来（GET / 返回 404）」：
    打包后模块以**裸名 app** 从 PYZ 导入（serve.py 的 app.app 回退路径），
    `__file__` = <_MEIPASS>/app.pyc → 旧写法得到 <_MEIPASS>/static，
    而 spec 的 datas 实际把前端放在 <_MEIPASS>/app/static → 找不到 index.html。
    现按「打包布局优先、源码布局兜底」依次探测。
    """
    import sys as _sys
    _cands = []
    if getattr(_sys, "frozen", False):
        _mp = getattr(_sys, "_MEIPASS", "") or ""
        if _mp:
            _cands.append(os.path.join(_mp, "app", "static"))
            _cands.append(os.path.join(_mp, "static"))
    _cands.append(os.path.join(os.path.dirname(__file__), "static"))
    for _c in _cands:
        if os.path.isdir(_c):
            return _c
    return _cands[-1]


_STATIC_DIR = _resolve_static_dir()

@app.route('/')
def index():
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


def _chapter_text_for_script(script: dict) -> str:
    """由剧本 metadata（novel_id + chapter_index）反查该集对应的原文章节文本"""
    meta = (script or {}).get("metadata") or {}
    novel_id = meta.get("novel_id")
    if not novel_id:
        return ""
    ch_index = meta.get("chapter_index") or (script or {}).get("episode_no") or 1
    try:
        text = read_novel_text(NOVELS_DIR, str(novel_id))
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"小说正文不可读（覆盖率归属将缺失）：{e}")
        return ""
    for c in split_chapters(text):
        if int(c.get("index") or 0) == int(ch_index or 0):
            return text[c.get("start") or 0:c.get("end") or 0]
    return ""


def _shot_coverage_map(script: dict) -> dict:
    """把原文章节正文单元归属到镜头（用于分镜画布展示「该镜承载了原文哪几句」）

    规则：逐单元与各镜「描述+台词+prompt_h3」做 4-gram 字面比对，
    取命中率最高的镜头归属；命中率低于 0.3 视为未承载。
    这是**离线规则判定**，与 coverage.py 的 LLM 判定同源（同一 gram 口径），
    仅供画布展示定位用，不替代覆盖率报告结论。
    """
    text = _chapter_text_for_script(script)
    if not text:
        return {}
    try:
        units, _total = coverage.split_source_units(text)
    except Exception:  # noqa: BLE001
        return {}
    if not units:
        return {}
    shots = [s for s in ((script or {}).get("shots") or []) if isinstance(s, dict)]
    if not shots:
        return {}
    shot_grams = []
    for s in shots:
        corpus = " ".join(str(x) for x in (
            s.get("description"), s.get("dialogue_text"), s.get("prompt_h3"),
            s.get("location"), s.get("camera")) if x)
        try:
            shot_grams.append(coverage._grams(coverage._norm(corpus)))
        except Exception:  # noqa: BLE001
            shot_grams.append(set())

    out: dict = {}
    for uid, unit in enumerate(units, start=1):
        if coverage.is_title_unit(unit):
            continue
        best_i, best_r = -1, 0.0
        for i, grams in enumerate(shot_grams):
            if not grams:
                continue
            try:
                r = coverage.literal_ratio(unit, grams)
            except Exception:  # noqa: BLE001
                continue
            if r > best_r:
                best_i, best_r = i, r
        if best_i >= 0 and best_r >= 0.3:
            sid = shots[best_i].get("shot_id", best_i + 1)
            out.setdefault(str(sid), []).append(
                {"unit_id": uid, "text": unit[:200], "ratio": best_r})
    return out


def _collect_asset_refs(project: str) -> tuple:
    """从磁盘自动收集项目的角色 / 场景参考图（无需前端传入）

    返回 (character_refs, scene_refs)，元素形如 {"name":..., "front": 本地路径}，
    可直接喂给 _collect_reference_images。

    为什么需要它：单镜重跑等「带内调用的接口」如果只依赖前端传参，
    前端一旦传了结构不完整的对象（例如直接传剧本里的 characters，只有
    reference_prompt_zh 而没有 front/base 键），参考图会静默丢失、
    视频退化成无角色锚点——这类静默降级比报错更难发现。

    S6 修复：判据与 pipeline.probe_assets 对齐 ——
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")，不再只认 4 个固定文件名；
      2) 同一目录下取第一张非空图片（兼容 ComfyUI 直接输出 base_123.png 等非标名）；
      3) 找不到任何图片 → 返回空 dict，**绝不静默 take-first**（由调用方决策报错/跳过）。
    B-14 P2-4：判据统一抽到模块级 _first_existing_asset_image，取图判据
    _build_asset_index 复用同一函数，消除两处「判有图」口径漂移。
    """
    def _first_nonempty_image(d: str) -> str:
        return _first_existing_asset_image(d)

    def _scan(root: str) -> list:
        out = []
        base = os.path.join(root, _safe_project(project))
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            # S6 候选顺序：front/base 固定名优先，否则取目录内第一张非空图片
            ref = ""
            for cand in ("front.png", "base.png", "front.jpg", "base.jpg"):
                p = os.path.join(d, cand)
                if os.path.isfile(p):
                    ref = p
                    break
            if not ref:
                ref = _first_nonempty_image(d)
            if ref:
                out.append({"name": name, "front": ref, "base": ref})
        return out

    return _scan(CHARACTERS_DIR), _scan(SCENES_DIR)


# ==========================================================================
# P1-2 关键帧驱动视频模式
# ==========================================================================

def _keyframe_sb_map(project_name: str, script: dict = None, storyboards=None,
                     episode_no=None) -> dict:
    """取该项目的分镜图映射 {shot键: 本地路径}

    键同时注册「数字键」与「shot_NN 键」（如 "1" 与 "shot_01"），
    避免调用方因键风格不同而漏配。

    注意（真实缺陷修复）：**必须合并** manifest 与目录扫描，不能命中 manifest 就提前返回。
    manifest 可能只记录了部分镜头（例如某镜曾在画布上单独重跑，manifest 被写成了单条），
    此时提前返回会让其余镜头在后续 index 兜底里**错配到别的镜头的分镜图**，
    进而用错误的画面当关键帧首帧。
    """
    project = _safe_project(project_name)
    sb_map: dict = {}

    def _put(key, path):
        if not path or not os.path.isfile(path):
            return
        k = _shot_num_key(key)
        sb_map[k] = path
        # 同时注册 shot_NN 风格键（若 k 为纯数字）
        if k.isdigit():
            sb_map[f"shot_{int(k):02d}"] = path

    # ① 前端显式传入优先
    if isinstance(storyboards, dict):
        for k, v in storyboards.items():
            local = comfyui_client.resolve_local_path(v) if isinstance(v, str) else None
            if local:
                _put(k, local)
    # ② 目录扫描作为基底（覆盖所有实际存在的分镜图）
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project,
                          episode_no if episode_no is not None else _ep_of_script(script))
    if not sb_map and os.path.isdir(sb_dir):
        for fn in sorted(os.listdir(sb_dir)):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                name = os.path.splitext(fn)[0]
                seq = "".join(ch for ch in name if ch.isdigit())
                if seq:
                    _put(seq, os.path.join(sb_dir, fn))
    # ③ manifest 覆盖（含质检状态与可能位于非默认目录的产物路径）
    manifest_path = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            for s in (mf.get("shots") or []):
                if not isinstance(s, dict) or not s.get("success"):
                    continue
                fp = comfyui_client.resolve_local_path(s.get("file") or "")
                _put(s.get("shot_id"), fp)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")
    return sb_map


# ==========================================================================
# P1-3 可视化分镜画布 + 单镜重跑
# ==========================================================================

def _storyboard_scratch_map(project):
    """扫「分镜生成中」的中间产物 → {shot_seq: {"url", "mtime"}}。

    ⭐ 为什么需要它（2026-10-02）：
        分镜步骤是**整步落盘**的 —— 6 镜全部生成 + 质检通过后，才把图写进
        `STORYBOARDS_DIR/<项目>/` 与 `storyboard_manifest.json`（画布的正规数据源）。
        而生成过程本身要 2~3 分钟/镜，整步十几分钟，期间画布**一张图都取不到**，
        用户看到的是「跑着但什么都没有」，误判为卡死或前端不刷新。

    `storyboard_scratch/` 是步骤进行中每镜的落盘位置（`shot_NN_tryK.png`，见
    下方生成侧 `scratch_png`），这里把它作为**只读的进度快照**暴露给画布，
    仅用于「生成中」预览，**不改变** `storyboard.exists/url` 的既有语义
    （那仍严格代表「已落盘正式产物」）。取最新 try（文件名尾部序号最大）。

    失败一律返回空 dict：这是纯展示增强，绝不能让画布接口 500。
    """
    out = {}
    scratch_dir = os.path.join(QC_DIR, project, "storyboard_scratch")
    if not os.path.isdir(scratch_dir):
        return out
    try:
        for fn in os.listdir(scratch_dir):
            m = re.match(r"^shot_(\d+)_try(\d+)\.png$", fn)
            if not m:
                continue
            seq, attempt = int(m.group(1)), int(m.group(2))
            full = os.path.join(scratch_dir, fn)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            cur = out.get(seq)
            if cur is None or attempt > cur["_attempt"]:
                out[seq] = {
                    "_attempt": attempt,
                    "url": f"/api/storyboards/scratch/{project}/{fn}",
                    "mtime": mtime,
                }
    except OSError:
        return {}
    for v in out.values():
        v.pop("_attempt", None)
    return out


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


def _storyboard_retry_shot_impl():
    """单镜分镜图重跑的实际实现（整段在 GPU 闸门内执行）

    D1（2026-09-23）：
    - 加 @_autopilot_guard → 异常不再泄漏成裸 HTML 500（与其它托管接口一致）。
    - 整段关键区（出图 → 质检 → 入库 → manifest 回写）进入 gpu_task_gate：
        · 避免与批量分镜 worker 抢同一张 GPU（TASK_QUEUE_CONCURRENCY 默认 1）；
        · 消除两边并发 read-modify-write storyboard_manifest.json 的**丢更新**
          —— 批量 worker 的 manifest 写入在它的 gate 内（`_storyboard_worker`
          由 `run_gpu_task` 包裹），本函数的写入也在本 gate 内，两者互斥。
      代价：批量任务在跑时手动重跑会排队等待（与「单 GPU 并发度 1」的设计一致；
      排队超过 30s 由 gpu_task_gate 打 warning，不静默）。
    """
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
    char_idx = _build_asset_index(script.get("characters") or [], project, "character")
    item_idx = _build_asset_index(script.get("items") or [], project, "item")
    scene_idx = _build_asset_index(script.get("scenes") or [], project, "scene")
    refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project)
    # S6 修复：参考图匹配失败时，不再静默取首角色（旧行为会把"不存在的角色"当主角色），
    # 而是明确 400 + 具体错误。
    if not refs and shot.get("_no_reference"):
        return jsonify({"success": False, "no_reference": True,
                        "error": shot.get("_ref_error") or "该镜头角色在资产索引中无匹配",
                        "hint": "请检查剧本 characters_in_shot 与资产目录名是否一致"}), 400
    if not refs:
        return jsonify({"success": False,
                       "error": "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"}), 400
    labels = [r[1] for r in refs]
    # 风格：剧本自带 style（用户与总控敲定）优先，缺失时退回项目 plan 的 style
    _rs_style = style_kit.normalize_style(script.get("style")) or style_kit.normalize_style(
        (autopilot.get_plan(project) or {}).get("style"))
    if _rs_style:
        shot = dict(shot, style=(shot.get("style") or _rs_style))
    # G19 同款兜底：风格串无画幅关键词时以默认画幅（2026-09-28 起为 16:9）为底。本端点
    # 此前漏了这层兜底 → size=None → 完全不覆写，画幅完全沿用模板/参考图，与批量 worker
    # 口径不一致。第二个实参 = 分镜图预算 0.8MP（画质提升，旧值 0.5）。
    _rs_size = style_kit.aspect_size(
        style_kit.aspect_ratio(_rs_style) or style_kit.DEFAULT_RATIO,
        style_kit.storyboard_megapixels())
    prompt = comfyui_client.build_storyboard_prompt(
        shot, labels, has_characters=_shot_has_on_screen(shot))
    refs = _unify_ref_canvas(refs, _rs_size, project)
    # ---- 提示词预检（生成前质检）：先判 → 确定性自愈 → 再出图 ----
    # 目的：把 GPU 花在有问题的提示词上是纯浪费，且出图后质检才发现就已经晚了。
    qc_cfg = _qc_load_cfg()
    prompt, _pf, _pgate = _prompt_preflight(
        "storyboard", prompt, ctx=shot, style=(shot.get("style") or _rs_style),
        ref_count=len(refs), project_name=project)
    if not _pgate.get("accept"):
        # ★ 用户需求：质检不合格的提示词不留本地。用户决策 1：提示词预检是**生成前**的文本
        # 合规检查，不通过直接阻断不生成 → 没有图片/视频可删，只有这份提示词历史 json 落盘
        # （P12 `output/qc/<项目>/prompt_<shot>.json`），把它移回收站。
        try:
            _purge_prompt_records(project, shot_id,
                                  reason=f"提示词预检未通过（{_pgate.get('label')}）")
        except Exception as _pe:  # noqa: BLE001
            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
        return jsonify({"success": False, "prompt_qc_blocked": True,
                        "error": f"提示词预检未通过（{_pgate.get('label')}）：{_pgate.get('reason')}"
                                 + (f"；建议：{_pf.get('rebuild_hint')}" if _pf.get("rebuild_hint") else ""),
                        "prompt_qc": _pf.get("verdict")}), 200
    seed = data.get('seed')
    try:
        result = comfyui_client.generate_storyboard(
            prompt_zh=prompt, ref_images=[r[2] for r in refs],
            filename_prefix=f"comic_drama_sb/{project}_shot_{seq:02d}_retry",
            seed=seed, size=_rs_size)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"分镜图重跑失败：{e}"}), 500
    files = (result or {}).get("files") or []
    if not files:
        return jsonify({"success": False, "error": "ComfyUI 未返回分镜图"}), 500

    # 质检（若已开启）：不达标同样阻断入库（与批量链路一致）
    qc_on = qc_client.image_qc_ready(qc_cfg)
    _ep = _ep_of_script(script, data.get('episode_no'))
    dst_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project), _ep)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"shot_{seq:02d}.png")
    verdict = None
    gate = None
    scratch_dir = os.path.join(QC_DIR, project, "storyboard_scratch")
    os.makedirs(scratch_dir, exist_ok=True)
    scratch = os.path.join(scratch_dir, f"shot_{seq:02d}_retry.png")
    # G8②：消费 ComfyUI output 源（与主 worker 的 move 语义对齐），不留 output 残留
    _move_with_retry(files[0], scratch)
    if qc_on:
        verdict = qc_client.check_image(scratch, _qc_shot_desc(shot), qc_cfg,
                                        style=(shot.get("style") or _rs_style),
                                        ref_images=_qc_ref_images(
                                            shot, char_idx, item_idx, scene_idx, refs))
        gate = _qc_gate(verdict)
        _qc_record_verdict(project, "image", shot_id, "单镜重跑质检",
                           1, seed, scratch, verdict, style=(shot.get("style") or _rs_style))
    if qc_on and not (gate or {}).get("accept"):
        # ★ 用户需求：质检「判定不通过」的暂存图不留本地（含 ComfyUI 侧）。
        # ⚠️ 只删「质检成功返回（ok=True）且判定不合格」的产物；ok=False（接口故障/超时/
        # 鉴权失败）不是产物不合格，绝不能删（那会把好图删光）。
        if (verdict or {}).get("ok") is True:
            _purge_rejected_artifacts([scratch], project=project, kind="storyboard_image_retry",
                                      reason=f"单镜重跑质检不合格（{(gate or {}).get('label')}）",
                                      history_file=(_qc_history_file_for(project, "image", shot_id)))
        return jsonify({"success": False, "qc_blocked": True,
                        "error": f"分镜图质检阻断（{(gate or {}).get('label')}）："
                                 f"{(gate or {}).get('reason')}；未写入正式目录",
                        "verdict": verdict}), 200
    shutil.copy2(scratch, dst)
    # 同步更新 manifest 中该镜条目
    # B-2 收口（2026-09-22 复验）：manifest 损坏时 read_json_strict 会 fail-loud 抛错，
    # 但此刻图片**已经**重跑成功并写进正式目录（上一行的 copy2）。若让异常直接冒泡，
    # 会把「部分成功」整镜报成失败，前端还只能拿到裸 HTML 500（全库仅注册了
    # BadRequest 处理器，无 JSON 500 处理器）。
    # 这里用窄 try 做**响亮降级**（不是 fail-open）：
    #   · 记 error 级日志（数据层异常不静默）
    #   · 在响应里显式带 manifest_updated=False + 原因，调用方可感知
    #   · **绝不**把 manifest 重建为 {} —— 那才会清空其他镜头的记录
    _manifest_updated = True
    _manifest_err = ""
    try:
        _update_storyboard_manifest_shot(project, shot_id, seq, dst, prompt, refs, verdict, gate,
                                         episode_no=_ep)
    except Exception as _m_err:  # noqa: BLE001
        _manifest_updated = False
        _manifest_err = f"{type(_m_err).__name__}: {_m_err}"
        app.logger.error(
            "单镜重跑：图片已写入正式目录，但 manifest 同步失败（不影响本次出图；"
            "project=%s shot=%s dst=%s）：%s", project, shot_id, dst, _manifest_err)
    # 提示词预检结论也落质检历史（kind=prompt），便于回溯「这一镜出图前提示词是什么状态」
    if not _pf.get("skipped"):
        try:
            _qc_record_verdict(project, "prompt", shot_id, "分镜图提示词预检",
                               0, seed, None, _pf.get("verdict") or {}, extra={
                                   "prompt_kind": "storyboard",
                                   "repairs": _pf.get("repairs") or [],
                                   "mode": (_pf.get("verdict") or {}).get("mode"),
                                   "accept": bool(_pf.get("accept")),
                               }, style=(shot.get("style") or _rs_style))
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"提示词预检记录落盘失败：{e}")
    _sub = f"ep{int(_ep):02d}/" if _ep and int(_ep) > 1 else ""
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "path": dst,
                    "url": f"/api/storyboards/file/{project}/{_sub}shot_{seq:02d}.png",
                    "prompt": prompt, "ref_count": len(refs),
                    # B-2：本次出图是否已同步进 manifest。False 表示图已出好、但清单未更新
                    # （manifest 损坏等），调用方可据此提示用户「重跑成功、清单待修」。
                    "manifest_updated": _manifest_updated,
                    "manifest_error": _manifest_err,
                    "prompt_qc": _pf.get("verdict"), "prompt_qc_repairs": _pf.get("repairs") or [],
                    "qc": verdict})


def _update_storyboard_manifest_shot(project: str, shot_id, seq: int, dst: str,
                                     prompt: str, refs: list, verdict=None, gate=None,
                                     episode_no=None):
    """把单镜重跑结果写回分镜 manifest（保持既有 schema 不变）

    集级目录：第 1 集沿用平铺，第 2 集起写 epNN/ 下的 manifest 与 URL。
    """
    _flat = os.path.join(STORYBOARDS_DIR, project)
    mpath = os.path.join(_ep_dir(_flat, episode_no), "storyboard_manifest.json")
    _sub = os.path.basename(_ep_dir(_flat, episode_no)) if _ep_dir(_flat, episode_no) != _flat else ""
    _url_prefix = f"{project}/{_sub}/" if _sub else f"{project}/"
    manifest = {}
    if os.path.isfile(mpath):
        # B-2（2026-09-22 复核补漏）：本路径与 _storyboard_worker（app.py 的
        # atomic_write_json 落盘）写的是**同一个** storyboard_manifest.json。
        # 旧实现用 `except: manifest = {}` 的 fail-open 读 + 裸 open(w) 非原子写，
        # 与批量 worker 并发时会出现「读到半截 → 用残缺 manifest 覆盖回去 → 其他
        # 镜头记录整批丢失」。这里改为与 D-03/D-04 同口径：严格读（损坏→.bak 恢复或
        # fail-loud）+ 原子写。单镜重跑是用户显式操作，manifest 损坏时报错远好过静默清空。
        manifest = read_json_strict(mpath, {})
    items = [s for s in (manifest.get("shots") or []) if isinstance(s, dict)]
    target = next((s for s in items if _shot_num_key(s.get("shot_id")) == _shot_num_key(shot_id)), None)
    entry = {
        "shot_id": shot_id, "success": True,
        "file": dst, "url": f"/api/storyboards/file/{_url_prefix}shot_{seq:02d}.png",
        "prompt": prompt, "ref_count": len(refs),
        "refs": {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs},
        "regenerated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regenerated": "single_shot_retry",
    }
    if verdict:
        entry["qc"] = {"enabled": True, "status": "pass" if (gate or {}).get("accept") else "blocked",
                       "label": (gate or {}).get("label"), "attempts": 1,
                       "score": verdict.get("score"), "verdict": verdict.get("verdict"),
                       "reason": verdict.get("reason")}
    if target is not None:
        target.update(entry)
    else:
        items.append(entry)
    manifest.setdefault("project_name", project)
    manifest["shots"] = items
    manifest["total"] = len(items)
    manifest["success_count"] = sum(1 for s in items if s.get("success"))
    manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        # B-2：与 worker 侧统一走 fs_atomic（唯一临时名 + fsync + .bak 快照 + replace 重试），
        # 避免单镜重跑与批量分镜 worker 并发写同一 manifest 互相截断。
        atomic_write_json(mpath, manifest)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"分镜 manifest 更新失败：{e}")


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

def _generate_asset_task(task_id: str, assets: list, asset_type: str, project_name: str,
                         style: str = "", overwrite: bool = False, sub_dir: str = ""):
    """后台资产生成任务：基础图 + （角色）由整图本地切分派生的视角单图

    P0 修复（④⑤）：全链路接入 AI 质检——基础图必须送检；不达标自动重生成（换 seed），
    重试仍不达标 / 质检调用异常 → 阻断入库并标记 qc_blocked。

    A-2 P0 断点续跑：新增 overwrite 参数（默认 False）。已达标入库的资产
    （目录内已有非空图，判据同 pipeline.probe_assets）直接跳过，不再重复
    「生成→质检→重画」；overwrite=True 时强制全量重生成。

    服装变体（衣柜，2026-10-02）：新增 sub_dir 可选参数（默认空串 = 行为与从前
    完全一致）。非空时把该资产的落盘目录（含 base.png / 切分视角图 / meta
    sidecar / 质检历史）整体挂到「主设定目录 + sub_dir」下（如 outfits/<key>），
    用于角色服装变体 —— 生成链路（提示词预检 / 质检 / 重试 / 切分）零改动，
    仅目录多一层。

    风格落地（2026-09-18 修复）：新增 style 参数。此前该任务**完全没有风格入参**，
    资产提示词只有 bible 的 reference_prompt_zh（实测其中零风格词），于是物品/角色/场景
    出的参考图完全不体现用户与总控敲定的风格。现在：
      - 正向提示词在生成前统一追加风格后缀（幂等，二次追加不重复）；
      - 解析画幅并覆写尺寸节点，「竖屏 9:16」真正落到画布；
      - 派生视角图继承基础图的画幅（切分件贴回同尺寸画布，见 sheet_split）。

    ⚠️ 2026-09-24 视角图改造（勿回退为 GPU 多视角重渲染）：详见 app/sheet_split.py 模块头。
      角色视角图由**基础图整图本地列投影切分**得到（零 GPU、零质检），物品/场景不再产出
      视角图；`success` 也不再受视角质量影响。
    """
    try:
        base_dir = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}[asset_type]
        gen_base = {
            "character": comfyui_client.generate_character_base,
            "item": comfyui_client.generate_item_base,
            "scene": comfyui_client.generate_scene_base,
        }[asset_type]

        # 服装变体（衣柜）子目录防御：sub_dir 只应是「outfits/<安全键>」这类相对
        # 子路径（由 /api/assets/character/outfit 传入）。这里再做一次纵深防御——
        # 归一后含「..」/ 盘符 / 绝对路径前缀的一律按空串处理（fail-open，回落主
        # 设定目录，与该参数不存在时的行为完全一致）。
        if sub_dir:
            _sd = os.path.normpath(str(sub_dir)).replace("\\", "/").strip("/")
            if not _sd or _sd.startswith("..") or "/../" in f"/{_sd}/" or ":" in _sd:
                app.logger.warning("资产子目录参数非法，按主设定目录处理：%r", sub_dir)
                sub_dir = ""
            else:
                sub_dir = _sd

        def _asset_full_dir(asset_name: str) -> str:
            """该资产的落盘目录：主设定目录（base_dir/<项目>/<名称>），或
            （服装变体）主设定目录 + sub_dir 子目录。sub_dir 为空时与从前逐字节一致。"""
            d = os.path.join(base_dir, project_name, asset_name)
            return os.path.join(d, sub_dir) if sub_dir else d

        # 风格（文字部分，如画风/色调）仍从总控敲定的 style 串解析；
        # 画幅**按资产类型内置写死**（2026-09-22 需求，不跟随视频比例）：
        #   角色参考图(三视图设定图) 1:1 / 道具 item 1:1 / 场景 scene 16:9
        # 与成片画幅解耦——即使用户拍 9:16 成片，角色参考图仍是 1:1。
        # ⚠️ 角色基础图内容是「正/侧/背三张全身视图横排的三视图设定图」，不是单人立绘，
        #    2026-09-23 已从 3:4 竖幅改回 1:1（3:4 会把三人挤到贴边，实测留白 0~2px）；
        #    详见 style_kit.ASSET_BASE_RATIO 上方注释。
        # ⚠️ 2026-09-24：视角图已改为从基础图**本地切分**派生（app/sheet_split.py），
        #    不再有「多视图独立画幅」这条路径，故这里只需解析基础图画幅。
        style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO)
        gen_style = style_res["style"]
        _base_ratio = style_kit.asset_aspect_ratio(asset_type) or style_kit.DEFAULT_RATIO
        # ⚠️ 像素预算**必须显式传**：2026-10-02 前此处漏传，资产图静默吃 `aspect_size`
        #    的函数默认值 0.5MP（1:1 → 736×736），而角色设定图是**全链路角色一致性的
        #    上游锚点** —— 参考图细节不足 → 被分镜放大后表现为系统性脸漂/色漂。
        #    现取 asset_megapixels()（默认 1.5，env MJSCXT_ASSET_MEGAPIXELS 可覆盖）。
        gen_size = style_kit.aspect_size(_base_ratio, style_kit.asset_megapixels())
        app.logger.info("[资产风格] %s 资产生成风格=%s；内置画幅 %s×%s（%.2fMP）",
                        asset_type, gen_style or style, _base_ratio[0], _base_ratio[1],
                        style_kit.asset_megapixels())

        # 质检配置：任务级读取一次，本任务内所有资产共用
        qc_cfg = _qc_load_cfg()
        qc_on = qc_client.image_qc_ready(qc_cfg)          # 质检接口是否可用
        qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
        if qc_declared and not qc_on:
            # 已声明开启图片质检但接口不可用：后续逐个资产明确阻断，绝不静默放行
            app.logger.error("[资产质检] 图片质检已开启但接口未就绪（base_url/api_key/model 不完整），"
                             "本次资产生成将阻断入库；请检查 qc_config.json")
        max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
        scratch_root = os.path.join(QC_DIR, project_name, "assets_scratch")

        results = []
        total = len(assets)

        def _set_phase(phase: str, qc_phase: str = None):
            with lock:
                generation_state[task_id]["phase"] = phase
                if qc_phase:
                    generation_state[task_id]["qc_phase"] = qc_phase

        for i, asset in enumerate(assets):
            name = asset.get('name', f'{asset_type}_{i+1}')
            try:
            
                # A-2 P0 断点续跑：已达标入库的资产不重复「生成→质检→重画」。
                # 就绪判据与 pipeline.probe_assets / _first_existing_asset_image 一致
                # （目录内任意一张非空白名单图）。仅 overwrite=True 时强制重生成。
                # ⚠️ 只在「已就绪」时提前 continue；未就绪项继续走下面的重要性过滤
                #    与完整生成链路，临时道具的 skip 路径不受影响。
                if not overwrite:
                    _ready_img = _first_existing_asset_image(_asset_full_dir(name))
                    if _ready_img:
                        app.logger.info("资产已达标入库，断点续跑跳过：%s（%s）",
                                        name, _ready_img)
                        results.append({"name": name, "status": "skipped",
                                        "reason": "已达标入库，断点续跑跳过"})
                        continue

                # 物品过滤：只生成重要道具的参考图
                if asset_type == 'item':
                    importance = asset.get('importance', '')
                    if importance and importance != '重要':
                        app.logger.info(f"跳过临时道具 '{name}'（importance={importance}），不生成参考图")
                        results.append({"name": name, "status": "skipped", "reason": f"临时道具，importance={importance}"})
                        continue
            
                prompt_zh = asset.get('reference_prompt_zh', asset.get('prompt_zh', asset.get('appearance', '')))
                # ---- 物品「主人形象」+「确切文字」入参（T02，2026-10-06） ----
                # owner_photo：物品表面是否承载某角色的肖像（证件照/头像/画像/告示人像/屏幕人像）；
                # surface_text：物品表面**确切**要呈现的文字（写进提示词，治「带文字物品乱码」）。
                # 老工程零回归：缺字段 → owner_photo=False、surface_text=""。
                # owner_photo 兜底兼容 bool 与 LLM 偶发的字符串 "true"/"false"（bool("false") 为真，故显式判）。
                surface_text = str(asset.get("surface_text") or "")
                _op_raw = asset.get("owner_photo")
                owner_photo = (_op_raw if isinstance(_op_raw, bool)
                               else str(_op_raw or "").strip().lower()
                               in ("1", "true", "yes", "y", "是", "有"))
                if asset_type == 'character':
                    # 不变量（2026-09-28）：qc_desc 里说「性别：X」，出图 prompt 里就必须有 X。
                    # reference_prompt_zh 为空时 canon 条款无处可落（ensure_canon_clause 对空
                    # zh 直接返回 False）→ 不在生成入口兜底就会出现「生成侧零性别约束、质检侧
                    # 按性别判」的反复重画。放在这里（而不是 qc_desc 之后）是为了让
                    # orig_asset_prompt（重试基准 + 教训库 key）也带上性别，否则每次重试
                    # 都会把 prompt 复原成无性别版本。
                    prompt_zh = asset_prompt_kit.ensure_prompt_gender(prompt_zh, asset)
                elif asset_type == 'scene':
                    # 场景同款不变量（2026-09-29）：角色有性别、物品有白底，唯独场景缺一条
                    # —— 场景图要被多视角 / 分镜 / 视频当作**同一个可导航空间**反复引用，
                    # 图里一旦出现人物就会被一并带进下游，且换机位后无法复用。
                    # ⚠️ 与 character 一样必须放在下方 qc_desc 之前，否则会重蹈
                    # 「生成侧零约束、质检侧按另一口径判」→ 反复判不过重画的坑。
                    prompt_zh = asset_prompt_kit.ensure_scene_layout(prompt_zh, asset)

                with lock:
                    generation_state[task_id].update({
                        "current": i + 1, "progress": int((i + 1) / total * 100),
                        "current_asset": name, "phase": "基础图"
                    })

                asset_dir = _asset_full_dir(name)
                os.makedirs(asset_dir, exist_ok=True)
                # 质检暂存目录：服装变体（sub_dir 非空）带子目录标签，避免与同一角色
                # 主设定图的并发生成互相 prune 掉对方的 try 中间产物；sub_dir 为空时
                # 目录名与从前逐字节一致（零回归）。
                _scratch_tag = sub_dir.replace("/", "_") if sub_dir else ""
                scratch_dir = os.path.join(
                    scratch_root,
                    f"{asset_type}_{name}" + (f"_{_scratch_tag}" if _scratch_tag else ""))
                os.makedirs(scratch_dir, exist_ok=True)
                _qc_prune_attempts(scratch_dir)   # G8③：清理上一轮遗留的过期 try（只留最近 4）
                _g_hint = asset_prompt_kit.gender_hint(asset); qc_desc = f"资产类型：{asset_type}；资产名称：{name}；{(_g_hint + '；') if _g_hint else ''}资产设定：{str(prompt_zh)[:400]}"

                # ---------- 阶段1：基础图（生成 → 质检 → 重生成 → 阻断判定） ----------
                base_dst = os.path.join(asset_dir, "base.png")
                base_attempts = []
                base_gate = None
                base_ok = False
                base_files = []
                # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None 走模板默认常量），
                # 使 qc.history[0].seed 不再为 null，产物可复现、可追溯。
                seed = random.randint(1, 2 ** 31 - 1)
                orig_asset_prompt = prompt_zh     # 教训库稳定键（改写后的提示词不参与指纹）
                # ---- 提示词预检（生成前质检）----
                # 资产是「一对多」批量生成（一个项目几十个角色/物品/场景），与整集同理不做硬阻断：
                # 单条提示词有问题就自愈 + 记录，不让整批资产生成中断。缺失/过短这类致命缺陷
                # 由后面的生成+质检链路兜底（空提示词本就出不来可用资产）。
                prompt_zh, _pf_asset, _pgate_asset = _prompt_preflight(
                    "asset", prompt_zh, ctx=asset, style=(asset.get("style") or gen_style or ""),
                    project_name=project_name, cfg=qc_cfg)   # G13：复用 worker 级配置
                if not _pgate_asset.get("accept"):
                    app.logger.warning("资产「%s」参考图提示词预检未通过（%s）：%s",
                                       name, _pgate_asset.get("label"), _pgate_asset.get("reason"))
                # ---- 物品主人形象参考图链路状态（T02，逐资产重置；跨该资产的重试累计） ----
                # _item_ref_img：主人角色单人参考图路径（空=不启用参考图链路）
                # _item_ref_fail：参考图链路**连续**失败次数（成功清零；达 2 次即降级）
                # _item_degraded：是否已降级（仅降级时写进结果字段 degraded）
                _item_ref_img = ""
                _item_ref_fail = 0
                _item_degraded = False
                if asset_type == 'item' and owner_photo:
                    _item_ref_img = _item_owner_ref_image(project_name, asset.get("owner"))
                    if not _item_ref_img:
                        app.logger.warning("[物品主人形象] 物品「%s」声明有人像，但未取到主人"
                                           "「%s」的参考图，本次回落纯 T2I",
                                           name, asset.get("owner"))
                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        seed = random.randint(1, 2 ** 31 - 1)
                        # ① 优先：针对**上一轮这张图**的质检缺陷，用 LLM 即时改写提示词（精准）
                        # ② 回落：召回历史教训库改写；再回落：仅换种子。
                        # ⚠️ 基准用 orig_asset_prompt，避免建议块一轮轮累积
                        prompt_zh = orig_asset_prompt
                        optimized = None
                        if base_attempts:
                            optimized = _optimize_prompt_from_qc(
                                "asset", orig_asset_prompt, base_attempts[-1],
                                style=gen_style or _qc_style_of(project_name))
                        if optimized:
                            prompt_zh = optimized
                            app.logger.info("资产 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
                                            name, attempt + 1)
                        else:
                            try:
                                suggestions = prompt_memory.suggest(
                                    kind="asset",
                                    prompt=orig_asset_prompt,
                                    project=project_name,
                                    root_dir=PROJECT_OUTPUT_DIR
                                )
                                learned = prompt_memory.learned_prompt(
                                    kind="asset",
                                    prompt=orig_asset_prompt,
                                    project=project_name,
                                    root_dir=PROJECT_OUTPUT_DIR,
                                    style=_qc_style_of(project_name),
                                )
                                if learned and learned != orig_asset_prompt:
                                    prompt_zh = learned
                                    app.logger.info("资产 %s 第 %d 次重试，按历史质检教训改写提示词：%s",
                                                    name, attempt + 1, suggestions[:2])
                                else:
                                    app.logger.info("资产 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                    name, attempt + 1)
                            except Exception as mem_err:
                                app.logger.warning(f"读取记忆模块失败: {mem_err}")
                    
                        _set_phase(f"{name} 基础图质检不达标，修改提示词后重新生成（第 {attempt}/{max_retries} 次）",
                                   "regenerating")
                    # B-13 P1-14：output 基础图改按「项目/类型/资产名」分桶，不再按
                    # 「项目×类型」混放——同名资产跨项目、同项目不同集共享 asset_type 时
                    # 互串基础图。资产目录仍按 name 分桶（asset_dir 不变），仅 output 桶细化。
                    _gen_prefix = f"comic_drama/{project_name}/{asset_type}/{name}"
                    # ---------- 物品主人形象：参考图链路（图生图）+ 连续失败降级（T02） ----------
                    # 判据：仅 item、声明有人像、拿到主人参考图、且未达降级阈值（连续失败 < 2）。
                    # ⚠️ 参考图链路失败（异常 / 未产出）**绝不让资产判 failed** —— 下方已有的
                    #    `if not base_files: ... break` 会把空结果当「基础图生成失败」，
                    #    故失败时**在本 attempt 内立即回落纯 T2I** 产出图片；连续失败达 2 次
                    #    置 degraded，并自第 3 次起**不再尝试**参考图链路（纯 T2I）。
                    if asset_type == 'item' and _item_ref_img and _item_ref_fail < 2:
                        _ref_files = []
                        try:
                            _ref_files = comfyui_client.generate_item_base_with_ref(
                                prompt_zh, _item_ref_img, seed=seed, style=gen_style,
                                size=gen_size, filename_prefix=_gen_prefix,
                                surface_text=surface_text)
                        except Exception as _ref_err:  # noqa: BLE001 - 参考图链路失败不阻断出图
                            app.logger.warning(
                                "[物品主人形象] 物品「%s」参考图链路异常（连续第 %d 次）：%s: %s",
                                name, _item_ref_fail + 1, type(_ref_err).__name__, _ref_err)
                        if _ref_files:
                            base_files = _ref_files
                            _item_ref_fail = 0                 # 成功 → 连续失败清零
                        else:
                            _item_ref_fail += 1
                            app.logger.warning(
                                "[物品主人形象] 物品「%s」参考图链路未产出（连续第 %d/2 次），"
                                "本次立即回落纯 T2I", name, _item_ref_fail)
                            if _item_ref_fail >= 2:
                                _item_degraded = True
                                app.logger.warning(
                                    "[物品主人形象] 物品「%s」参考图链路连续失败 %d 次，"
                                    "自第 3 次起不再尝试参考图链路（纯 T2I）",
                                    name, _item_ref_fail)
                            # 立即回落，保证本 attempt 仍产出图片（参考图失败不得让资产判 failed）
                            base_files = comfyui_client.generate_item_base(
                                prompt_zh, seed=seed, style=gen_style, size=gen_size,
                                filename_prefix=_gen_prefix, surface_text=surface_text)
                    elif asset_type == 'item':
                        if _item_degraded:
                            app.logger.warning(
                                "[物品主人形象] 物品「%s」已降级为纯 T2I"
                                "（参考图链路连续失败 %d 次）", name, _item_ref_fail)
                        # 普通物品 / 未命中主人 / 已降级：纯 T2I（含 surface_text 确切文字硬约束）
                        base_files = comfyui_client.generate_item_base(
                            prompt_zh, seed=seed, style=gen_style, size=gen_size,
                            filename_prefix=_gen_prefix, surface_text=surface_text)
                    else:
                        # character / scene：保持既有字典派发调用签名不变
                        base_files = gen_base(prompt_zh, seed=seed, style=gen_style,
                                              size=gen_size, filename_prefix=_gen_prefix)
                    if not base_files:
                        base_attempts.append({"attempt": attempt + 1, "seed": seed, "stage": "基础图生成",
                                              "ok": False, "error": "基础图生成失败"})
                        # B-16 P2-11：基础图生成失败 → 清理本资产产生的 scratch 中间产物
                        _cleanup_scratch_dir(scratch_dir, app.logger)
                        base_gate = {"accept": False, "blocked": True, "skipped": False,
                                     "label": "生成失败", "reason": "基础图生成失败", "critical_issues": []}
                        break
                    scratch_base = os.path.join(scratch_dir, f"base_try{attempt + 1}.png")
                    # G8②：消费 ComfyUI output 源（视频链路一直用 move，图片链路此前 copy2
                    # 导致 output/comic_drama/ 只增不减）。move 后 output 目录不留残留。
                    _move_with_retry(base_files[0], scratch_base)
                    if not qc_on:
                        if qc_declared:
                            # 已声明开启质检但接口不可用：明确阻断（图仅留在暂存区），不静默放行
                            base_gate = {"accept": False, "blocked": True, "skipped": False,
                                         "label": "质检接口未就绪",
                                         "reason": "已开启图片质检但质检接口不可用"
                                                   "（qc_config.json 缺 base_url / api_key / model）",
                                         "critical_issues": []}
                            break
                        base_gate = {"accept": True, "blocked": False, "skipped": True,
                                     "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                     "critical_issues": []}
                        base_ok = True
                        break
                    _set_phase(f"{name} 基础图质检中（第 {attempt + 1} 次）", "checking")
                    # 物品参考图多给一条判据：只认本体，出现承托物/容器即缺陷。
                    # 实测漏判：筑基丹提示词自带「置于黑色玉盒中」，图照着画 → 图与提示词
                    # 完全一致 → 质检判通过。质检口径必须比「像不像提示词」更高一层。
                    _qc_desc_base = qc_desc
                    if str(asset_type) == "item":
                        _qc_desc_base = (str(qc_desc) +
                                         "\n\n【物品图专项判据】物品参考图只应呈现**物品本体**。"
                                         "若画面主体是容器/托盘/盒子/底座/支架/展示台/碗碟/绸布等承托物，"
                                         "或物品被遮挡、被放进/放在别的物体内或上面、出现手或人物，"
                                         "判为关键缺陷（extra_prop / 承托物），kinds 记「物品变形」。"
                                         "纯白背景上出现明显投影或桌面/地面也计一条缺陷。"
                                         "注意：本判据优先于「是否与提示词一致」——"
                                         "提示词本身写了容器时，仍判缺陷。")
                    # 2026-10-07 用户反馈「生成的角色皮肤是蓝色的」：项目风格串含
                    # 「色调灰蓝压抑」被图像模型当**全局调色** → 皮肤与固有色一起染蓝，
                    # 而老质检给 100 分通过 —— 因为通用判据只要求「配色与目标风格一致」，
                    # 反而**奖励**了染色。故资产参考图补一条颜色保真判据（与物品专项
                    # 判据同一挂载方式），把「色调不得改变固有色」写进判定口径。
                    if str(asset_type) in ("character", "item", "scene"):
                        _qc_desc_base = (str(_qc_desc_base) +
                                         "\n\n【参考图颜色保真专项判据】参考图是身份与固有色的基准，"
                                         "颜色必须准确。若整体色调把**肤色**（应为自然肤色）或"
                                         "**服饰/物品/环境的固有色**整体染偏（例如皮肤呈蓝/绿/灰蓝，"
                                         "白色帆布鞋变蓝灰），判为关键缺陷，kinds 记「配色异常」；"
                                         "**即使与目标风格的色调描述一致也判缺陷** ——"
                                         "色调只作用于画面氛围，不是改变固有色。")
                    verdict = qc_client.check_image(scratch_base, _qc_desc_base, qc_cfg, style=gen_style)
                    app.logger.info(f"[资产质检] base {asset_type}/{name} 第{attempt + 1}次 → "
                                    f"{verdict.get('call_url')} model={verdict.get('model')} "
                                    f"ok={verdict.get('ok')} passed={verdict.get('passed')} "
                                    f"score={verdict.get('score')} style_mismatch={verdict.get('style_mismatch')} "
                                    f"latency={verdict.get('latency_ms')}ms")
                    base_attempts.append(_qc_record_verdict(project_name, "asset_image", f"{name}_base",
                                                            "资产基础图质检", attempt + 1, seed,
                                                            scratch_base, verdict, style=gen_style))
                    base_gate = _qc_gate(verdict)
                    if base_gate["accept"]:
                        base_ok = True
                        break
                    if not verdict.get("ok"):
                        break     # 质检接口异常，重生成无意义
                    # ★ 立刻沉淀：让同一次循环的下一次重试就能召回这条缺陷
                    _record_qc_lesson(project_name, "asset", orig_asset_prompt, base_attempts[-1])
                    # ★ G1 止损：连续两次基础图缺陷完全相同 → 继续重试只是重复烧 GPU，提前停
                    _hopeless, _hopeless_detail = _qc_retry_hopeless(base_attempts)
                    if _hopeless:
                        base_attempts[-1]["retry_stopped"] = True
                        base_attempts[-1]["retry_stopped_features"] = _hopeless_detail
                        app.logger.warning(
                            f"资产基础图重试止损（{name}）：连续 {len(base_attempts)} 次缺陷完全相同，"
                            f"提前停止重试。缺陷：{_hopeless_detail}；建议改写该资产提示词后重跑")
                        break
                if not base_ok:
                    # 把「基础图哪里不对」沉淀进教训库（供下次重生成时改写提示词）
                    if base_attempts and isinstance(base_attempts[-1], dict):
                        _record_qc_lesson(project_name, "asset", prompt_zh, base_attempts[-1])
                    # ★ 用户需求：质检「判定不通过」的暂存基础图不留本地（含 ComfyUI 侧）。
                    # ⚠️ 仅当最后一次尝试是「质检成功返回且不合格」（ok=True）时才删；
                    # ok=False（接口故障）/ skipped（未开启）/ 质检接口未就绪 都不删。
                    try:
                        _last_b = base_attempts[-1] if (base_attempts and isinstance(base_attempts[-1], dict)) else {}
                        if qc_on and _last_b.get("ok") is True:
                            _purge_rejected_artifacts(
                                [_last_b.get("file") or scratch_base],
                                project=project_name,
                                reason=f"资产基础图质检不合格（{(base_gate or {}).get('label')}）",
                                kind="asset_base_image",
                                history_file=_last_b.get("history_file") or "")
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"资产不合格基础图清理失败（忽略）：{_pe}")

                    results.append({
                        "name": name, "success": False, "dir": asset_dir, "stage": "基础图",
                        "qc_blocked": bool(base_gate and base_gate.get("blocked")),
                        "error": (f"基础图未通过质检（{base_gate['label']}）：{base_gate['reason']}"
                                  if base_gate else "基础图生成失败"),
                        "qc": _qc_summary(base_attempts, qc_declared, qc_on,
                                          int(qc_cfg.get("max_retries", 0))),
                    })
                    continue
                # 质检达标 → 正式入库
                if base_attempts:
                    shutil.copy2(base_attempts[-1]["file"], base_dst)
                else:
                    # G8②：2380 处已把 ComfyUI output move 到 scratch_base（不再 copy2），
                    # 故入库源是 scratch_base（base_files[0] 此时已 move 走、不可再取）
                    shutil.copy2(scratch_base, base_dst)
                # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论），可复现可追溯
                _write_artifact_meta(
                    base_dst, kind="asset_base", project_name=project_name,
                    seed=seed, prompt=orig_asset_prompt,
                    workflow_key={"character": "character_gen", "item": "item_gen",
                                  "scene": "scene_gen"}.get(asset_type),
                    qc=base_gate, asset_name=name,
                    extra={"asset_type": asset_type, "style": gen_style or None})

                # ---------- 阶段2：视角单图（本地切分，不再走 GPU 多视角编辑） ----------
                # 2026-09-24 改造，机制与实测见 app/sheet_split.py 模块头 + config 同名注释：
                #   旧实现在这里调 `comfyui_client.generate_multiview` 逐视角**重渲染** —— 实测
                #   4 张产物与 base.png **内容一致**（参考图编辑 cfg=1.0 只复刻已见机位），
                #   净成本 = 每资产 4 次 GPU 渲染 + 4 次质检，收益 = 0（下游只取 front.png），
                #   且多视角不达标会把**整个资产判 failed**（旧 success = not blocked_views）。
                #   现在：
                #     · 角色 → 从三视图整图**本地列投影切分**出 front/left/back 单视角图
                #       （零 GPU、零质检），并清掉旧实现遗留的 right.png；
                #     · 物品 / 场景 → 基础图本身就是单主体图，不再产出任何视角图。
                #   切分是**已通过质检**的基础图的确定性派生 —— 没有可重试的自由度
                #   （重跑只会得到同一张切图），故不再需要「逐视角质检 → 整组重生成」循环。
                view_paths = {"base": base_dst}
                view_attempts = {}
                view_gate = {}
                saved_views = []
                blocked_views = []      # 保留字段：派生无「阻断」语义，恒为空
                derive_error = None
                if asset_type == "character":
                    # 2026-10-02 用户指定：角色图**不裁剪**，直接保留整图 base.png。
                    # 角色设定图已改为英文四区 character sheet（见
                    # comfyui_client._CHARACTER_SHEET_EN_LAYOUT），四区不对称布局无法再
                    # 做行列投影切分 → 不再产出 front/left/back/half 单视角。
                    # 并**清掉旧视角图**：否则 _ASSET_IMG_PRIORITY（front > base）会让
                    # 下游取到上一轮遗留的旧单视角图，与「整图」口径互斥
                    # （即旧发型图继续被用作参考的老坑，见下方原注释）。
                    _derived = {}
                    derive_error = None
                    app.logger.info("角色「%s」不裁剪：保留整图 %s（不再切分单视角）",
                                    name, os.path.basename(base_dst))
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(), known=ASSET_VIEW_STEMS, logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                elif (asset_type == "scene" and SCENE_VIEWS_ENABLED
                      and SCENE_GRID_MODE and SCENE_GRID_ONESHOT):
                    # ---------- 场景：九宫格「单次出图」直出整图（2026-10-07 用户拍板） ----------
                    # 动机：旧路「9 档逐档独立 T2I + scene_grid.stitch_grid 拼接」实测
                    #   8 分 16 秒/场景，且 9 个机位在独立出图下易收敛成同一张平视全景；
                    #   实验 B（.workbuddy/test/_out/exp_b_scene_oneshot.py）证明把 9 个机位
                    #   **逐格写死**进一句提示词（范式同 storyboard_grid_main），单次 T2I
                    #   57 秒即得机位正确的 3×3 整图 → 一次出图直出整图，省时且机位更准。
                    # 口径（用户拍板）：单次出图 @1.5MP（整图 1664×928，每格 554×309）。
                    # ⚠️ 本路径**绝不**调用 scene_grid.stitch_grid、**绝不** os.remove/unlink
                    #    任何分档 png；关闭 SCENE_GRID_ONESHOT 则回落到下一个 elif 的旧
                    #    9 档拼接路径（那段代码原样保留、一行不改）。
                    # 正面档仍复用已过质检的 base.png（语义与旧路径一致，base.png 必须保留）。
                    view_paths["front"] = base_dst
                    view_gate["front"] = {
                        "accept": True, "blocked": False, "skipped": True,
                        "label": "复用基础图（正面档）",
                        "reason": "基础图即正面机位出图，正面档直接复用，不重复出图",
                        "critical_issues": [],
                    }
                    saved_views.append("front")
                    _grid_dst = os.path.join(asset_dir, SCENE_GRID_FILENAME)
                    # 机位已写进提示词主体 → view_key=None（不再追加机位后缀）。
                    _oneshot_prompt = comfyui_client.build_scene_grid_prompt(
                        prompt_zh, style=gen_style)
                    _oneshot_gate = None
                    _oneshot_verdict = None       # 最近一次质检原始判据（丢图排障用）
                    _oneshot_scratch = None
                    _oneshot_seed = seed          # 首轮沿用 base 同 seed，便于目视对比
                    view_attempts[SCENE_GRID_FILENAME] = []
                    for _oa in range(SCENE_VIEW_MAX_RETRIES + 1):
                        if _oa > 0:
                            _oneshot_seed = random.randint(1, 2 ** 31 - 1)
                        _set_phase(f"{name} 场景九宫格整图（单次出图，第 {_oa + 1}/"
                                   f"{SCENE_VIEW_MAX_RETRIES + 1} 次）", "views")
                        # gen_size 是上游已算好的 1.5MP 尺寸（见本函数上方 gen_size 计算），
                        # 不在此另算尺寸 —— 画幅/像素预算是资产级统一口径。
                        _of = comfyui_client.generate_scene_base(
                            _oneshot_prompt, seed=_oneshot_seed, style=gen_style,
                            size=gen_size,
                            filename_prefix=(f"comic_drama/{project_name}/{asset_type}"
                                             f"/{name}/grid"),
                            view_key=None, surface_text=surface_text)
                        if not _of:
                            # 出图失败（非质检问题）→ 换 seed 重试无意义
                            app.logger.warning(
                                "场景「%s」九宫格整图单次出图失败（seed=%s）",
                                name, _oneshot_seed)
                            break
                        _oneshot_scratch = os.path.join(
                            scratch_dir, f"grid_oneshot_try{_oa + 1}.png")
                        _move_with_retry(_of[0], _oneshot_scratch)
                        if not qc_on:
                            _oneshot_gate = {"accept": True, "blocked": False, "skipped": True,
                                             "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                             "critical_issues": []}
                            break
                        # 质检口径**显式写明九宫格语义**：否则判官只核「是不是这个场景」，
                        # 出一张单图也会判过，九宫格的机位价值就白费了。
                        _oneshot_desc = (
                            f"资产类型：scene；资产名称：{name}；"
                            "本图是 3×3 九宫格，共 9 格，每格是同一场地的不同机位与景别，"
                            "须 9 格齐全且各格构图明显不同（全景/远景/中景/近景/两个特写/"
                            "左右侧视/俯视鸟瞰，同一空间结构、陈设与光影一致）；"
                            f"资产设定：{str(prompt_zh)[:400]}")
                        _set_phase(f"{name} 场景九宫格整图质检中（第 {_oa + 1} 次）", "checking")
                        _oneshot_verdict = qc_client.check_image(
                            _oneshot_scratch, _oneshot_desc, qc_cfg, style=gen_style)
                        app.logger.info(
                            "[资产质检] grid scene/%s 第%d次 → ok=%s passed=%s score=%s",
                            name, _oa + 1, _oneshot_verdict.get("ok"),
                            _oneshot_verdict.get("passed"), _oneshot_verdict.get("score"))
                        view_attempts[SCENE_GRID_FILENAME].append(
                            _qc_record_verdict(
                                project_name, "asset_image", f"{name}_grid",
                                "场景九宫格整图质检", _oa + 1, _oneshot_seed,
                                _oneshot_scratch, _oneshot_verdict, style=gen_style))
                        _oneshot_gate = _qc_gate(_oneshot_verdict)
                        # ⚠️ 质检接口故障（fault_open）**不算达标**：不得当「已判过」落盘。
                        if _oneshot_gate["accept"] and not _oneshot_gate.get("fault_open"):
                            break
                        # 非接口级的「质检调用异常」重生成无意义 → 停；接口故障继续换 seed。
                        if not _oneshot_verdict.get("ok") and not (
                                _oneshot_verdict.get("interface_fault")
                                or (_oneshot_gate or {}).get("fault_open")):
                            break
                    _oneshot_ok = bool(_oneshot_gate and _oneshot_gate.get("accept")
                                       and not _oneshot_gate.get("fault_open")
                                       and _oneshot_scratch)
                    if _oneshot_ok:
                        _move_with_retry(_oneshot_scratch, _grid_dst)
                        view_paths[SCENE_GRID_FILENAME] = _grid_dst
                        view_gate[SCENE_GRID_FILENAME] = _oneshot_gate
                        saved_views.append(SCENE_GRID_FILENAME)
                        _write_artifact_meta(
                            _grid_dst, kind="asset_grid", project_name=project_name,
                            seed=_oneshot_seed, prompt=orig_asset_prompt,
                            # workflow_key 指向**实际跑的** T2I 工作流（generate_scene_base
                            # 走 scene_gen = 场景生成_Qwen21.json），这样 meta 才能记到真实的
                            # workflow_sha256；不写自造的、WORKFLOW_TEMPLATE 里查不到的临时键
                            # （查不到 → sha 恒 null，字段白留）。旧路那条不动。
                            workflow_key="scene_gen",
                            qc=_oneshot_gate, asset_name=name,
                            extra={"asset_type": asset_type,
                                   "derive_mode": "oneshot",
                                   "cells": list(SCENE_GRID_VIEW_KEYS),
                                   "n_cells": len(SCENE_GRID_VIEW_KEYS),
                                   "style": gen_style or None})
                        app.logger.info(
                            "[场景九宫格] 场景「%s」单次出图完成（1.5MP 直出整图）：%s",
                            name, _grid_dst)
                    else:
                        # 加值项不阻断资产：整图未达标时不落盘、不判 failed，分镜回落 base.png。
                        _oneshot_gate = _oneshot_gate or {}
                        if not _oneshot_scratch:
                            app.logger.warning(
                                "场景「%s」九宫格整图单次出图失败（未产出图片）→ 不落盘、"
                                "不判 failed，分镜将回落 base.png", name)
                            view_gate[SCENE_GRID_FILENAME] = dict(
                                _oneshot_gate, dropped=True, drop_reason="出图失败（未产出图片）")
                        else:
                            app.logger.warning(
                                "场景「%s」九宫格整图**质检判定不达标**（重试 %d 次）→ 不落盘、"
                                "不判 failed，分镜将回落 base.png：%s",
                                name, SCENE_VIEW_MAX_RETRIES + 1,
                                _oneshot_gate.get("label"))
                            view_gate[SCENE_GRID_FILENAME] = dict(
                                _oneshot_gate, dropped=True, drop_reason="质检判定不达标")
                    # 清掉本档集合内**本轮不再产出**的陈旧机位图（上一轮 9 档/4 档遗留），
                    # 避免 UI 与资产索引把陈旧分档当成有效档展示；grid.png 在 keep 内不受影响。
                    _known_stems = (tuple(ASSET_VIEW_STEMS)
                                    + tuple(SCENE_GRID_VIEW_KEYS))
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=tuple(view_paths.keys()),
                            known=_known_stems,
                            logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                elif asset_type == "scene" and SCENE_VIEWS_ENABLED:
                    # ---------- 场景：按机位逐档出图（2026-09-29 新增） ----------
                    # 动机：场景此前只有一张 base.png，分镜不论什么机位都拿它当参考图 ——
                    #   俯拍 / 斜侧镜头拿到的是**正面基准图**，构图先验与镜头要求反向。
                    # 做法：在**基础图阶段**把机位写进提示词逐档**独立出图**（T2I），
                    #   而不是基础图之后用参考图编辑补机位 —— 后者在 cfg=1.0 下改不动
                    #   机位，正是 2026-09-24 被废除的那条路（机制见 config.SCENE_VIEW_KEYS
                    #   上方注释 + comfyui_client.scene_view_prompt_suffix）。
                    # 成本口径：正面档**复用刚过质检的 base.png**（不重复烧 GPU），
                    #   只多出 left45 / right45 / top 三档；三档都是**加值**而非必需，
                    #   不达标就丢弃并由下游回落正面档，绝不把资产判 failed。
                    # ⭐ 2026-10-05 场景九宫格（SCENE_GRID_MODE）：机位档从 4 档扩到 9 档，
                    #   逐档独立出图后额外用 scene_grid.stitch_grid 拼成一张 3×3 总图
                    #   grid.png（= 场景资产本体，下游 _pick_scene_view 整图直接用）。
                    #   关闭时下面全部回落到 4 档旧行为（SCENE_VIEW_*），零回归。
                    _grid = bool(SCENE_GRID_MODE)
                    _grid_keys = SCENE_GRID_VIEW_KEYS if _grid else SCENE_VIEW_KEYS
                    _grid_labels = SCENE_GRID_LABELS if _grid else SCENE_VIEW_LABELS
                    _grid_angles = SCENE_GRID_ANGLE_ZH if _grid else SCENE_VIEW_ANGLE_ZH
                    view_paths["front"] = base_dst
                    view_gate["front"] = {
                        "accept": True, "blocked": False, "skipped": True,
                        "label": "复用基础图（正面档）",
                        "reason": "基础图即正面机位出图，正面档直接复用，不重复出图",
                        "critical_issues": [],
                    }
                    saved_views.append("front")
                    for vk in _grid_keys:
                        if vk == "front":
                            continue
                        _v_label = _grid_labels.get(vk) or vk
                        _v_angle = _grid_angles.get(vk) or _v_label
                        _v_dst = os.path.join(asset_dir, f"{vk}.png")
                        _v_gate = None
                        _v_verdict = None       # 最近一次质检原始判据（丢档排障用）
                        _v_scratch = None
                        view_attempts[vk] = []   # 逐次累加（不覆盖），供结果回传与排障
                        _v_seed = seed      # 同 seed：跨档共享初始噪声，结构最相关
                        for _va in range(SCENE_VIEW_MAX_RETRIES + 1):
                            if _va > 0:
                                _v_seed = random.randint(1, 2 ** 31 - 1)
                            _set_phase(f"{name} 场景机位档「{_v_label}」"
                                       f"（第 {_va + 1}/{SCENE_VIEW_MAX_RETRIES + 1} 次）", "views")
                            _v_files = comfyui_client.generate_scene_base(
                                prompt_zh, seed=_v_seed, style=gen_style, size=gen_size,
                                filename_prefix=(f"comic_drama/{project_name}/{asset_type}"
                                                 f"/{name}/{vk}"),
                                view_key=vk,
                                # ⭐ 2026-10-07：场景「确切文字」硬约束（与物品同款）。
                                #    非空时由 _ensure_scene_text_render 把「写哪几个字」
                                #    逐字写进提示词 —— 治「场景告示/招牌文字乱码」的根因。
                                surface_text=surface_text)
                            if not _v_files:
                                # 出图失败（非质检问题）→ 换 seed 重试无意义
                                app.logger.warning("场景「%s」机位档 %s 出图失败（seed=%s）",
                                                   name, vk, _v_seed)
                                break
                            _v_scratch = os.path.join(scratch_dir, f"{vk}_try{_va + 1}.png")
                            _move_with_retry(_v_files[0], _v_scratch)
                            if not qc_on:
                                _v_gate = {"accept": True, "blocked": False, "skipped": True,
                                           "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                           "critical_issues": []}
                                break
                            # 机位档的质检口径**显式带机位要求**：否则判官只核「是不是这个场景」，
                            # 出一张正面图也会判过，机位档就白生成了。
                            _v_desc = (
                                f"资产类型：scene；资产名称：{name}；"
                                f"本图机位要求：{_v_angle}；"
                                f"资产设定：{str(prompt_zh)[:400]}")
                            _set_phase(f"{name} 场景机位档「{_v_label}」质检中"
                                       f"（第 {_va + 1} 次）", "checking")
                            _v_verdict = qc_client.check_image(
                                _v_scratch, _v_desc, qc_cfg, style=gen_style)
                            app.logger.info(
                                "[资产质检] view scene/%s/%s 第%d次 → ok=%s passed=%s "
                                "score=%s", name, vk, _va + 1, _v_verdict.get("ok"),
                                _v_verdict.get("passed"), _v_verdict.get("score"))
                            view_attempts[vk].append(
                                _qc_record_verdict(
                                    project_name, "asset_image", f"{name}_{vk}",
                                    f"场景机位档「{_v_label}」质检", _va + 1, _v_seed,
                                    _v_scratch, _v_verdict, style=gen_style))
                            _v_gate = _qc_gate(_v_verdict)
                            # ⚠️ 质检接口故障（fault_open）**不算达标**：不得当「已判过」落盘。
                            if _v_gate["accept"] and not _v_gate.get("fault_open"):
                                break
                            # 接口故障（根本没拿到判定）→ **继续循环换 seed 重试**，别当内容不合格停手；
                            # 非接口级的「质检调用异常」重生成无意义 → 停。
                            if not _v_verdict.get("ok") and not (
                                    _v_verdict.get("interface_fault")
                                    or (_v_gate or {}).get("fault_open")):
                                break     # 质检调用异常（非接口级）→ 重生成无意义
                            # ⚠️ 刻意**不**调 _record_qc_lesson：机位档的缺陷（角度不对）
                            #    不是资产提示词的缺陷，沉淀进去会让**下一轮的基础图提示词**
                            #    被按「机位」改写，把加值项的毛病传播成资产本身的毛病。
                        # 达标 = 判官放行 **且** 非接口故障放行（fault_open 不算达标）
                        _v_ok = bool(_v_gate and _v_gate.get("accept")
                                     and not _v_gate.get("fault_open") and _v_scratch)
                        # 与 base 的**相似度粗筛**（撞车即丢档，2026-10-03 B 方案）：
                        # 仅作「明显撞车」的**下限粗筛** —— 阈值见 config.SCENE_VIEW_DUP_PHASH_MAX
                        # （=95，**不能调到 80**：真换构图的 top≈81.2、几乎没变的 left45≈84.4，
                        #   80~95 不可分，调到 80 会误杀 top），只拦 >95 的近重复。
                        # ⚠️⭐ 2026-10-06：**网格模式下不做丢档**（`_v_dup` 恒 False，见下）——
                        #   九宫格的每一格都是用户指定的画面（第 1 格全景基准 / 第 5 格特写细节A…），
                        #   与 base 相像也**必须入格**；丢档会在九宫格里留空黑格。
                        #   粗筛的原始目的（旧 4 档：与正面档太像 ⇒ 该档没价值 ⇒ 丢弃让分镜回落
                        #   base）在网格模式下已由「_pick_scene_view 整图直接用」取代，不再成立。
                        #   网格模式改为**软告警**（记相似度、提示该机位可能没真换构图），不阻断。
                        _v_sim = None
                        if _v_ok:
                            try:
                                _v_sim_res = consistency.phash_similarity(base_dst, _v_scratch)
                                if _v_sim_res.get("ok"):
                                    _v_sim = float(_v_sim_res.get("score") or 0.0)
                            except Exception as _vse:  # noqa: BLE001
                                app.logger.warning(
                                    "场景「%s」机位档「%s」相似度粗筛失败（不拦截，按达标处理）：%s",
                                    name, _v_label, _vse)
                        _v_dup = bool((not _grid) and _v_sim is not None
                                      and _v_sim > SCENE_VIEW_DUP_PHASH_MAX)
                        if (_grid and _v_ok and _v_sim is not None
                                and _v_sim > SCENE_VIEW_DUP_PHASH_MAX):
                            app.logger.warning(
                                "场景「%s」机位档「%s」与 base 相似度 %.1f > %.1f："
                                "九宫格模式下**不丢档**（每一格都是用户要的画面），但该机位可能"
                                "没真的换构图 —— 建议检查 SCENE_GRID_ANGLE_ZH 里该档的措辞"
                                "（A7 范式：给结果 + 排斥旧构图）",
                                name, _v_label, _v_sim, SCENE_VIEW_DUP_PHASH_MAX)
                        if _v_ok and not _v_dup:
                            shutil.copy2(_v_scratch, _v_dst)
                            view_paths[vk] = _v_dst
                            view_gate[vk] = _v_gate
                            saved_views.append(vk)
                            # O2：机位档旁路元数据（标注「同 seed 独立出图」，可复现）
                            _write_artifact_meta(
                                _v_dst, kind="asset_view", project_name=project_name,
                                seed=_v_seed, prompt=orig_asset_prompt,
                                workflow_key="scene_gen",
                                qc=_v_gate, asset_name=name,
                                extra={"asset_type": asset_type, "view": vk,
                                       "view_label": _v_label, "angle_zh": _v_angle,
                                       "derive_mode": "angle_regen",
                                       "derived_from": os.path.basename(base_dst)})
                        else:
                            # 加值项不阻断资产：本档不提供，分镜侧逐级回落 front/base。
                            # 三种丢档原因彼此区分，并在 qc_views 里留 drop_reason 便于排障。
                            _v_gate = _v_gate or {}
                            _v_fault = bool(_v_gate.get("fault_open")
                                            or (_v_verdict or {}).get("interface_fault"))
                            if _v_dup:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」与 base 相似度 %.1f > %.1f"
                                    "（明显撞车 / 未换构图）→ 丢弃该档（不落盘、不判 failed，"
                                    "分镜将回落正面档）",
                                    name, _v_label, _v_sim, SCENE_VIEW_DUP_PHASH_MAX)
                                view_gate[vk] = dict(
                                    _v_gate, dropped=True, dup_similarity=round(_v_sim, 1),
                                    drop_reason=(f"与base相似度{_v_sim:.1f}"
                                                 f">阈值{SCENE_VIEW_DUP_PHASH_MAX:g}"))
                            elif not _v_scratch:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」出图失败（未产出图片）→ 丢弃该档"
                                    "（不落盘、不判 failed，分镜将回落正面档）", name, _v_label)
                                view_gate[vk] = dict(_v_gate, dropped=True,
                                                     drop_reason="出图失败（未产出图片）")
                            elif _v_fault:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」因**质检接口故障**重试 %d 次仍未"
                                    "取得判定 → 丢弃该档（不落盘、不判 failed，"
                                    "分镜将回落正面档）：%s",
                                    name, _v_label, SCENE_VIEW_MAX_RETRIES + 1,
                                    _v_gate.get("reason") or "质检接口不可用")
                                view_gate[vk] = dict(
                                    _v_gate, dropped=True,
                                    drop_reason="质检接口故障·重试后仍未取得判定")
                            else:
                                app.logger.warning(
                                    "场景「%s」机位档「%s」**质检判定不达标**，本次不落盘"
                                    "（分镜将回落正面档）：%s",
                                    name, _v_label, _v_gate.get("label"))
                                view_gate[vk] = dict(_v_gate, dropped=True,
                                                     drop_reason="质检判定不达标")
                    # 清掉本档集合内**本轮不再产出**的陈旧机位图（含上一轮/旧实现遗留），
                    # 避免 UI 与资产索引把陈旧机位当成有效档展示。
                    # ⭐ 2026-10-05 九宫格：known 集合随模式扩展（网格含 9 档 + grid 主图），
                    #    否则机位分档（wide/mid/near/detail_a/detail_b 等）会被 prune 当陈旧档清掉。
                    _known_stems = (tuple(ASSET_VIEW_STEMS)
                                    + (_grid_keys if _grid else tuple(SCENE_VIEW_KEYS)))
                    if _grid:
                        # ⭐ 2026-10-06 修复（九宫格缺格）：第 1 格 `front` 在磁盘上**没有**
                        #   `front.png` —— 场景不单独产出正面档，正面档就是已经过质检的
                        #   `base.png`（见 config.SCENE_GRID_CELL_FILE_STEM 与 _build_asset_index
                        #   的「front 别名到 base」）。旧写法无脑拼 f"{k}.png"，于是 9 档里
                        #   第一格恒被判「文件不存在」而跳过 → 实际只拼进 8 格 → 3×3 里
                        #   **「全景（主视角）」整个缺格**（旧 8 档时代更惨，只剩 7 格 + 2 个空黑格）。
                        #   这里按 SCENE_GRID_CELL_FILE_STEM 解析磁盘名，`front` → base.png。
                        # ⚠️⭐ 2026-10-06 第二处修复（**格序错位**）：构造的必须是**定长槽位表**
                        #   （长度恒 = len(_grid_keys)，缺档填 None），**不能**是「过滤后 append」——
                        #   stitch_grid 按槽位索引贴图，过滤会让缺格之后的机位**整体左移一格**
                        #   （第 5 格＝特写细节A 那格变成别的画面，比空黑格更难发现）。
                        _grid_slots = []
                        for _k in _grid_keys:
                            _stem = SCENE_GRID_CELL_FILE_STEM.get(_k, _k)
                            _cp = (base_dst if _stem == "base"
                                   else os.path.join(asset_dir, f"{_stem}.png"))
                            _grid_slots.append(_cp if os.path.isfile(_cp) else None)
                        _grid_missing = [_k for _k, _p in zip(_grid_keys, _grid_slots) if not _p]
                        _grid_have = len(_grid_keys) - len(_grid_missing)
                        if _grid_missing:
                            app.logger.warning(
                                "[场景九宫格] 场景「%s」应有 %d 格、实得 %d 格（缺格：%s）——"
                                "缺格在总图里留空占位（**后续格位不左移**），下游拿不到该机位视角",
                                name, len(_grid_keys), _grid_have, _grid_missing)
                        _grid_dst = os.path.join(asset_dir, SCENE_GRID_FILENAME)
                        if _grid_have >= 2:
                            try:
                                # ⚠️ 2026-10-07 修正：scene_grid.stitch_grid 的默认 cell_width=640 是随手定的整数，
                                #    3×640=1920 → 整图 1920×1068 = 2.051MP，**超过本仓「所有图片 1.5MP」的约定**
                                #    （style_kit.ASSET_MEGAPIXELS_DEFAULT=1.5，实测 1664×928）；且每格被压到 640×356
                                #    = 0.228MP（原料的 1/6.8）。改传 554 使整图 ≈1664×928（每格 554×309）。
                                #    该值只在旧路径（SCENE_GRID_ONESHOT=False 的回退分支）生效。
                                _stitched = scene_grid.stitch_grid(
                                    _grid_slots, _grid_dst, slot_count=len(_grid_keys),
                                    cell_width=554)
                                if _stitched and os.path.isfile(_stitched):
                                    view_paths[SCENE_GRID_FILENAME] = _stitched
                                    view_gate[SCENE_GRID_FILENAME] = {
                                        "accept": True, "blocked": False, "skipped": True,
                                        "label": "九宫格拼接（本地派生）",
                                        "reason": "多机位独立出图后本地拼 3×3 总图，继承各档质检",
                                        "critical_issues": [],
                                    }
                                    saved_views.append(SCENE_GRID_FILENAME)
                                    _write_artifact_meta(
                                        _stitched, kind="asset_grid", project_name=project_name,
                                        seed=seed, prompt=orig_asset_prompt,
                                        workflow_key="scene_gen_grid",
                                        qc={"accept": True, "skipped": True,
                                             "label": "拼接总图（继承各档质检）"},
                                        asset_name=name,
                                        extra={"asset_type": asset_type,
                                               "derive_mode": SCENE_GRID_DERIVE_MODE,
                                               "cells": list(_grid_keys),
                                               "n_cells": _grid_have,
                                               "cells_missing": _grid_missing,
                                               "style": gen_style or None})
                                    app.logger.info(
                                        "[场景九宫格] 场景「%s」%d 机位独立出图已拼成 3×3 总图 "
                                        "（%d 格）：%s", name, len(_grid_keys),
                                        _grid_have, _stitched)
                                    # ⭐ 2026-10-06（用户指定）：场景资产**只保留 grid.png + base.png**，
                                    #    多机位分档图（left45/right45/top/…）**降级为拼接原料**：
                                    #    拼完 3×3 总图后立即删掉分档 png，使其**不进资产索引、
                                    #    不在 UI 显示、不被下游按机位取档**（下游 _pick_scene_view
                                    #    走 grid 整图，关掉了按机位对档）。
                                    #    只删本轮**成功落盘**的分档（_grid_slots 里的非空项），
                                    #    保留 base.png 与 grid.png。分档 meta 一并清掉（避免孤儿 meta）。
                                    #    ⚠️ 必须先跳过 None（缺格槽位）——``os.remove(None)`` 抛的是
                                    #    TypeError 而非 OSError，会绕过下面的 except 直接冒泡。
                                    for _cell in _grid_slots:
                                        if not _cell or _cell == base_dst or _cell == _stitched:
                                            continue        # 跳过缺格槽位 / 保留 base 与 grid
                                        try:
                                            os.remove(_cell)
                                            _meta = _cell[:-4] + ".meta.json"
                                            if os.path.isfile(_meta):
                                                os.remove(_meta)
                                        except OSError:
                                            pass
                                    app.logger.info(
                                        "[场景九宫格] 场景「%s」分档原料图已清理（仅留 base+grid）", name)
                            except Exception as _ge:  # noqa: BLE001
                                app.logger.warning(
                                    "场景「%s」九宫格拼接失败（保留各机位档，不阻断入库）：%s",
                                    name, _ge)
                        else:
                            app.logger.warning(
                                "场景「%s」九宫格可用的机位档不足 2（实得 %d），跳过拼接，"
                                "下游回落 base.png", name, _grid_have)
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=tuple(view_paths.keys()),
                            known=_known_stems,
                            logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                else:
                    # 物品：基础图即单主体图；清掉旧实现遗留的视角图，避免 UI 把陈旧的
                    # 「重渲染整图」继续当成一个视角展示。
                    # （场景在关闭 SCENE_VIEWS_ENABLED 时也走这里 → 与改造前逐字一致）
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(),
                            known=tuple(ASSET_VIEW_STEMS) + tuple(SCENE_VIEW_KEYS),
                            logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)

                _asset_result = {
                    "name": name,
                    "success": True,
                    "dir": asset_dir,
                    "views": list(view_paths.keys()),
                    "qc_blocked": False,
                    "qc_blocked_views": blocked_views,
                    "derive_error": derive_error,
                    "error": None,
                    # 视角图多为本地派生（不单独质检），故 qc 汇总只反映基础图
                    "qc": _qc_summary(base_attempts, qc_declared, qc_on,
                                      int(qc_cfg.get("max_retries", 0))),
                    "qc_base": _qc_summary(base_attempts, qc_declared, qc_on,
                                           int(qc_cfg.get("max_retries", 0))),
                    # 逐档质检结论（角色档是「本地派生·继承基础图质检」，场景机位档是
                    # 真跑质检的结论）。前端此前未消费该字段，这里填实数据不改形状。
                    "qc_views": {k: dict(v) for k, v in view_gate.items()},
                }
                # T02：物品「主人形象」参考图链路降级标记（**仅降级时写该键**，
                # 成功走参考图链路或本就不适用时不写，保持结果形状零回归）。
                if _item_degraded:
                    _asset_result["degraded"] = True
                results.append(_asset_result)

            except Exception as _asset_err:  # noqa: BLE001
                # ⚠️ 审计 S11：旧代码这里没有 try —— `generate_multiview` 上传基础图失败会
                #    raise RuntimeError，`queue_prompt` 遇 5xx/超时也会抛。任一处抛出 →
                #    整个批次被标 failed，`results`（已成功资产的结果）全部丢弃：
                #    20 个资产在第 7 个时来一次连接抖动，前 6 个已入库的成果用户也看不见。
                app.logger.error("资产「%s」生成失败（已隔离，继续后续资产）：%s: %s",
                                 name, type(_asset_err).__name__, _asset_err)
                results.append({
                    "name": name, "success": False,
                    "dir": _asset_full_dir(name),
                    "stage": "异常中断", "qc_blocked": False,
                    "error": f"{type(_asset_err).__name__}: {_asset_err}",
                })
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / total * 100),
                        "phase": f"{name} 生成异常（已跳过）",
                    })
                continue
        blocked_count = sum(1 for r in results if r.get("qc_blocked"))
        with lock:
            generation_state[task_id].update({
                "status": "completed", "results": results,
                "success_count": sum(1 for r in results if r.get("success")),
                "qc_blocked_count": blocked_count,
            })
    except Exception as e:
        app.logger.error(f"资产生成失败: {e}")
        _partial = locals().get("results") or []   # 审计 S11：已成功的部分结果不能丢
        with lock:
            generation_state[task_id].update({
                "status": "failed", "error": str(e), "results": _partial,
                "success_count": sum(1 for r in _partial if r.get("success")),
            })
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    _maybe_clear_comfyui_history("资产批量生成收尾")


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
_OUTFITS_DIRNAME = "outfits"
#: 服装描述追加进提示词的标记（幂等判据，见 _append_outfit_prompt）
_OUTFIT_PROMPT_MARK = "；本套服装："
#: 服装档案文件名（生成发起时先落一份 outfit_key/desc 记录，查询端点回显描述用）
_OUTFIT_RECORD_FILE = "outfit.json"
#: 变体档位（与主设定目录的切分产物同名，来自 sheet_split 链路）
_OUTFIT_VIEW_STEMS = ("front", "left", "back", "half")


def _sanitize_outfit_key(raw) -> str:
    """outfit_key 安全化：剔除路径非法字符 ``\\ / : * ? " < > |`` 与首尾空白、
    截断到 40 字符，防路径穿越。

    剔完为空（或只剩 ``.`` / ``..`` —— 分别是目录自身与上级，同样算穿越）返回
    ''，由调用方按 400 拒绝。刻意**不用** _safe_project：那是项目键收敛规则，
    会把任意输入坍缩成合法键（恒非空），而 outfit_key 必须能被 400 明确拒绝。
    """
    s = str(raw or "").strip()
    for _ch in '\\/:*?"<>|':
        s = s.replace(_ch, "")
    s = s.strip()
    if not s or s in (".", ".."):
        return ""
    return s[:40]


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


def _character_outfit_dir(project_name: str, character: str, outfit_key: str = "") -> str:
    """角色服装变体目录（outfit_key 为空时是 outfits 根目录）"""
    d = os.path.join(CHARACTERS_DIR, project_name, character, _OUTFITS_DIRNAME)
    return os.path.join(d, outfit_key) if outfit_key else d


def _find_script_character(project_name: str, character: str) -> dict:
    """从项目剧本（_load_script_for）按名字（含别名归一）找角色档案；找不到返回 {}

    用途：服装变体的提示词要在「角色主设定」之上追加服装描述，主设定来自剧本
    characters[].reference_prompt_zh；appearance / gender 等字段也一并透传，
    供生成端 ensure_prompt_gender 不变量与质检描述使用。
    """
    _norm = _normalize_char_alias(character)
    try:
        for c in ((_load_script_for(project_name, None) or {}).get("characters") or []):
            if isinstance(c, dict) and \
                    _normalize_char_alias(str(c.get("name") or "")) == _norm:
                return dict(c)
    except Exception as e:  # noqa: BLE001  剧本读失败不阻断（回落主设定 meta）
        app.logger.warning("服装变体读取剧本角色档案失败（忽略）：%s", e)
    return {}


def _character_base_prompt(project_name: str, character: str) -> str:
    """角色主设定的参考提示词：剧本 characters[].reference_prompt_zh 优先，
    回落主设定目录 base.png.meta.json 的 prompt（O2 产物旁路元数据）。

    都拿不到返回 ''——此时变体提示词只含服装段，生成端的性别不变量会按剩余
    字段兜底；与「剧本缺角色描述」的既有资产生成行为同口径，不额外阻断。
    """
    p = str(_find_script_character(project_name, character).get("reference_prompt_zh")
            or "").strip()
    if p:
        return p
    try:
        meta_path = os.path.join(CHARACTERS_DIR, project_name, character,
                                 "base.png.meta.json")
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                return str((json.load(f) or {}).get("prompt") or "").strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning("服装变体读取主设定 meta 失败（忽略）：%s", e)
    return ""


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


def _bigram_overlap(a: str, b: str) -> float:
    """字符 2-gram 重叠率（|A∩B| / |B|）：服装文本与变体描述的模糊匹配打分。"""
    a = re.sub(r"\s+", "", str(a or ""))
    b = re.sub(r"\s+", "", str(b or ""))
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(ga & gb) / max(1, len(gb))


def _episode_outfit_overrides(project_name: str, episode_no: int) -> dict:
    """跨集一致性巩固（2026-10-02）：把本集各角色的服装状态解析成衣柜变体 key。

    服装文本来源（按优先级）：本集 state_in.character_states[].outfit（continuity
    按集登记的服装状态）→ bible.current_outfit。变体匹配：outfits/<key>/outfit.json
    的 desc 与服装文本做 2-gram 重叠打分，最高分且 >0 才采用 —— 分不清就不指定，
    走主设定图（宁缺毋滥，绝不因猜错服装而错挂参考图）。
    :return: {角色名: outfit_key}；无 state / 无变体 / 匹配不上 → {}（零回归）
    """
    try:
        from config import CONTINUITY_DIR as _cont_dir
        from continuity import load_state as _load_ep_state
        _st = _load_ep_state(_cont_dir, project_name, int(episode_no)) or {}
    except Exception:  # noqa: BLE001
        _st = {}
    want: dict = {}
    for cs in ((_st.get("state_in") or {}).get("character_states") or []):
        if isinstance(cs, dict) and str(cs.get("name") or "").strip():
            want[str(cs.get("name")).strip()] = str(cs.get("outfit") or "").strip()
    if not want:
        try:
            from continuity import load_bible as _load_bible
            _bible = _load_bible(_cont_dir, project_name) or {}
            for c in (_bible.get("characters") or []):
                if isinstance(c, dict) and str(c.get("name") or "").strip():
                    want[str(c.get("name")).strip()] = str(
                        c.get("current_outfit") or "").strip()
        except Exception:  # noqa: BLE001
            return {}
    want = {k: v for k, v in want.items() if v}
    if not want:
        return {}

    proj_char_root = os.path.join(CHARACTERS_DIR, project_name)
    if not os.path.isdir(proj_char_root):
        return {}
    out: dict = {}
    try:
        _char_dirs = os.listdir(proj_char_root)
    except OSError:
        return {}
    for char_name in _char_dirs:
        outfit_root = os.path.join(proj_char_root, char_name, _OUTFITS_DIRNAME)
        if not os.path.isdir(outfit_root):
            continue
        text = want.get(char_name) or ""
        # 别名容错：want 的键可能带别名，做一次包含匹配
        if not text:
            text = next((v for k, v in want.items()
                         if k in char_name or char_name in k), "")
        if not text:
            continue
        best_key, best_score = "", 0.0
        try:
            _keys = os.listdir(outfit_root)
        except OSError:
            continue
        for key in _keys:
            rec = os.path.join(outfit_root, key, _OUTFIT_RECORD_FILE)
            desc = ""
            try:
                if os.path.isfile(rec):
                    with open(rec, "r", encoding="utf-8") as _f:
                        desc = str((json.load(_f) or {}).get("desc") or "")
            except Exception:  # noqa: BLE001
                desc = ""
            if not desc:
                continue
            _s = _bigram_overlap(desc, text)
            if _s > best_score:
                best_key, best_score = key, _s
        if best_key and best_score > 0:
            out[char_name] = best_key
    if out:
        app.logger.info("[服装变体] 本集服装覆盖：%s", out)
    return out


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

_ASSET_DIRS = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}


def _build_asset_index(assets: list, project_name: str, kind: str) -> dict:
    """构建「资产名 → 本地图片绝对路径」索引

    优先使用前端传来的 HTTP 资源路径（解析回本地），其次按资产目录约定推导
    output/assets/<kind>/<项目>/<名称>/(front|base).png，最后补齐磁盘上已有但未上报的资产。
    """
    base_dir = _ASSET_DIRS[kind]
    names = []
    for a in (assets or []):
        if isinstance(a, dict) and a.get("name"):
            names.append((a.get("name"), a))
        elif isinstance(a, str) and a:
            names.append((a, {}))
    project_dir = os.path.join(base_dir, project_name)
    if os.path.isdir(project_dir):
        known = {n for n, _ in names}
        for d in sorted(os.listdir(project_dir)):
            if os.path.isdir(os.path.join(project_dir, d)) and d not in known:
                names.append((d, {}))

    index = {}
    for name, payload in names:
        if name in index:
            continue
        # B-14 P2-4：取图判据统一走 _first_existing_asset_image——
        # 不再只认 front/base 固定名，改为「扩展名白名单 + 第一张非空」。
        # front/base 的 http URL 仍由前端 resolve_local_path 传回本地路径；
        # 本地推导时按 (project_dir, name) 目录取第一张可用图。
        asset_dir_for_name = os.path.join(project_dir, name)
        local = _first_existing(
            comfyui_client.resolve_local_path(payload.get("front") or ""),
            comfyui_client.resolve_local_path(payload.get("base") or ""),
            _first_existing_asset_image(asset_dir_for_name),
        )
        entry = {"name": name, "image": local, "_dir": asset_dir_for_name,
                 "url": f"/api/assets/{kind}s/{project_name}/{name}/front.png"}
        # 2026-09-25 景别对档：把**逐视角**路径也挂进来，供 `_pick_char_view`
        # 按镜头景别取「半身档 / 全身档」。角色资产自 sheet_split 改造后是
        # front/left/right/back/half 五个独立文件（少任何一个都合法）。
        #   ⚠️ 键只在**文件真实存在**时写入（不写空串），否则 `payload.get(k) or ""`
        #      分辨不出「没这个档位」与「有这个档位但路径为空」。
        #   ⚠️ `_dir` 是 `_pick_char_view` 的兜底按需推导目录（而不是批量 stat），
        #      这样前端上报的 http URL 失效时仍能命中磁盘约定路径。
        # 2026-09-29：挂在集合里再并入**场景机位档**（front/left45/right45/top）——
        # 供 `_pick_scene_view` 按镜头机位取「同机位场景图」。两类档位名不重叠
        # （角色 front/left/right/back/half vs 场景 front/left45/right45/top），
        # 且键只在**文件真实存在**时写入，故混挂不会互相干扰、也不会给没有的档写空串。
        for _stem in tuple(ASSET_VIEW_STEMS) + tuple(SCENE_VIEW_KEYS):
            _p = _first_existing(
                comfyui_client.resolve_local_path(payload.get(_stem) or ""),
                os.path.join(asset_dir_for_name, f"{_stem}.png"),
                os.path.join(asset_dir_for_name, f"{_stem}.jpg"),
            )
            if _p:
                entry[_stem] = _p
        # ⭐ 2026-10-05 场景九宫格：把 9 机位独立档（wide/mid/near/detail_a/detail_b 等）
        #    与拼接总图 grid.png 一并挂进 entry，供 `_pick_scene_view` 的九宫格分支
        #    按 SCENE_GRID_FILENAME 直接取整图。键只在**文件真实存在**时写入（同上方
        #    逐档口径），避免给「没这个档」写空串。
        #    ⚠️ 2026-10-06 起生产链路拼完九宫格会**删掉**机位分档 png（只留 base + grid），
        #    所以这里的 9 档键实际大多不存在、不会写入 —— 保留它只为兼容「旧项目存量分档图」
        #    与关闭九宫格开关后的回滚路径，不影响新生产。
        if kind == "scene":
            for _stem in tuple(SCENE_GRID_VIEW_KEYS) + (SCENE_GRID_FILENAME,):
                if _stem in entry:
                    continue
                _p = _first_existing(
                    comfyui_client.resolve_local_path(payload.get(_stem) or ""),
                    os.path.join(asset_dir_for_name, f"{_stem}.png"),
                    os.path.join(asset_dir_for_name, f"{_stem}.jpg"),
                )
                if _p:
                    entry[_stem] = _p
        # 场景把**正面档别名到 base.png**：场景不单独产出 front.png（base 本身就是正面
        # 机位出图，见资产 worker 的 scene 分支），但下游 `_pick_scene_view` 的回退链
        # 要按档位名逐级取，别名能省掉「每个调用点各自特判 scene」的分支。
        if kind == "scene" and "front" not in entry and local:
            entry["front"] = local
        index[name] = entry
    return index


def _normalize_char_alias(name) -> str:
    """归一化角色名别名（S6）：剥离 _主角/_角色/_主/_人 后缀、去空白与《》。

    与 pipeline.probe_assets 的资产目录扫描口径对齐：资产目录名可能是
    "青玉_主角" 而剧本里写 "青玉"，或反之。这里只做「后缀剥离 + 去符号」，
    **不做模糊匹配**（避免把"阿青"误归到"青玉"）。
    """
    s = str(name or "").strip()
    s = s.replace("《", "").replace("》", "").replace(" ", "")
    for suf in ("_主角", "_角色", "_主", "_人"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


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


def _normalize_scene_name(name) -> str:
    """归一化场景名（只做**标点 / 空白 / 全半角**层面，不做语义改写）。

    刻意**不剥**「（夜）」「外」「门口」这类限定词：它们是场景区分的一部分，
    剥了会把「卧室」和「卧室外」混成一个。此类差异交给
    :func:`_match_scene_name` 的「唯一子串」一级去兜（且必须唯一）。

    ⚠️ 2026-09-29：实现已移到 :mod:`asset_name_match`（三类资产共用一份），
    这里只是保持旧函数名的薄封装。
    """
    return asset_name_match.normalize(name)


def _match_scene_name(loc, scene_idx) -> tuple:
    """把镜头写的场景名解析到 ``scene_idx`` 的键 → ``(key, level)``。

    三级降级（精确 → 归一化 → **唯一**子串），全部失败返回 ``(None, "")``。
    第 3 级必须唯一：若索引里「卧室」「卧室外」都能被子串命中，则放弃——
    **宁可不给图，也不给错图**。

    ⚠️ 2026-09-29：实现已移到 :mod:`asset_name_match`，此处为薄封装。
    """
    return asset_name_match.match(loc, scene_idx, "scene")


def _note_ref_warning(shot, msg: str) -> None:
    """把「参考图未命中」告警挂到镜头上（**累加，不覆盖**）。

    一个镜头可能同时缺场景图与物品图；直接赋值会把前一条覆盖掉，排障时只能
    看到最后一条。这里统一累加到 ``_ref_warnings``，同时保留 ``_ref_warning``
    （首条）以兼容既有读取方。
    """
    if not isinstance(shot, dict) or not msg:
        return
    lst = shot.get("_ref_warnings")
    if not isinstance(lst, list):
        lst = []
        shot["_ref_warnings"] = lst
    if msg not in lst:
        lst.append(msg)
    shot.setdefault("_ref_warning", msg)


def _resolve_scene_entry(shot: dict, scene_idx: dict,
                         where: str = "") -> tuple:
    """解析镜头所属场景 → ``(scene_name, entry)``；未命中时**显式告警**并记账。

    调用方只有在 ``entry is None`` 时才真正「没有场景锚点」。告警写进
    ``shot["_ref_warnings"]``（**累加**），能在任务结果里定位到具体哪一镜、
    写的什么名字。

    ⚠️ 只有「剧本声明了场景、且索引非空」时未命中才告警——两者皆空属于正常
    空镜脚本，不该制造噪音。
    """
    if not isinstance(shot, dict):
        return None, None
    loc = shot.get("location")
    name, level = _match_scene_name(loc, scene_idx)
    if not name:
        if loc and scene_idx:
            _msg = (f"场景名未匹配：镜头 {shot.get('shot_id')} 的 location={loc!r} "
                    f"在场景资产 {sorted(scene_idx.keys())[:8]} 中无对应项，"
                    f"该镜将不带场景参考图")
            _note_ref_warning(shot, _msg)
            app.logger.warning("[%s] %s", where or "scene-ref", _msg)
        return None, None
    if level != "exact":
        app.logger.info("[%s] 场景名模糊命中：%r → %r（%s）",
                        where or "scene-ref", loc, name, level)
        shot["_scene_match"] = {"queried": loc, "matched": name, "level": level}
    return name, (scene_idx.get(name) or {})


def _resolve_item_names(shot: dict, item_idx: dict, where: str = "") -> list:
    """解析镜头出场物品 → **规范名列表**（去重保序）；未命中显式告警。

    与 :func:`_resolve_scene_entry` / :func:`_match_shot_chars` 同口径：
    「宁可告警、不可静默」。旧实现是裸判据
    ``[n for n in items_in_shot if n in item_idx]`` —— 物品名只因全角括号、
    书名号、空格或「（断）」这类后缀差异对不上，该物品的设定图就**不注入、
    不打任何日志**，画面里道具形制只能靠模型猜（「道具走形」类质检缺陷的
    隐蔽来源）。
    """
    if not isinstance(shot, dict):
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(
        shot.get("items_in_shot"), item_idx, "item")
    for _q, _k, _lv in fuzzy:
        app.logger.info("[%s] 物品名模糊命中：%r → %r（%s）",
                        where or "item-ref", _q, _k, _lv)
    if missing:
        _msg = (f"物品名未匹配：镜头 {shot.get('shot_id')} 的 {missing} "
                f"在物品资产 {sorted(item_idx.keys())[:8]} 中无对应项，"
                f"该物品将不带参考图")
        _note_ref_warning(shot, _msg)
        app.logger.warning("[%s] %s", where or "item-ref", _msg)
    return resolved


def _match_shot_chars(shot: dict, char_idx: dict) -> list:
    """S6 修复：按镜头 characters_in_shot 匹配 char_idx，**禁止静默 take-first**。

    返回匹配到的角色名列表（保持 shot 原顺序、去重）；镜头一个角色都匹配不到 →
    返回 []，由调用方设 shot['_no_reference']=True / shot['_ref_error']=... 决定
    400 / 跳过。

    ⚠️ 2026-09-29 与物品／场景统一口径：
      · 匹配走 :mod:`asset_name_match` 三级降级（精确 → 归一化 → **唯一**子串），
        不再只做「精确 + 后缀别名」两档 —— 「方源。（少年）」这类写法以前会静默丢图；
      · **部分未命中也会告警**。旧实现只在「一个都没匹配上」时出声，而
        「镜头登记了 A、B 两人、只有 A 命中」时 B 被静默丢弃（B 的参考图不注入，
        用户与日志都看不出来），是「角色不像设定」的隐蔽来源。
    """
    if not isinstance(shot, dict):
        return []
    chars_in = [n for n in (shot.get("characters_in_shot") or []) if n]
    if not chars_in:
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(
        chars_in, char_idx, "character")
    for _q, _k, _lv in fuzzy:
        app.logger.info("[角色参考图] 角色名模糊命中：%r → %r（%s）", _q, _k, _lv)
    if missing:
        _msg = (f"角色名未匹配：镜头 {shot.get('shot_id')} 的 {missing} "
                f"在角色资产 {sorted(char_idx.keys())[:8]} 中无对应项，将不带其参考图")
        _note_ref_warning(shot, _msg)
        app.logger.warning("[角色参考图] %s", _msg)
    return resolved


def _on_screen_characters(shot: dict) -> list:
    """「本镜画面内可见角色」的**权威口径**（单一来源，2026-10-05）。

    直接委托 :func:`te_3d_director.on_screen_characters` —— 它同时是 3D 导演台
    ``_parse_characters`` 的实现（``render_blocking → build_render_plan →
    _parse_characters``）。因此「分镜该不该注入 3D 基准图」的判据与「3D 导演台渲不渲得出
    人偶」的判据**是同一个函数**，天然同源，不会再出现「A 处说没人、B 处说有人」的分叉。

    ⚠️ 只认剧本的 ``characters_in_shot``；**台词 speaker 可能是画外音，不算出场角色**。

    te_3d_director 不可导入时返回 ``[]``：此时 ``render_blocking`` 同样不可用（本镜本就不会
    注入基准图），判为「无出场角色」是安全的保守值，不会误注入人偶。
    """
    if not isinstance(shot, dict):
        return []
    try:
        import te_3d_director  # noqa: PLC0415
        return list(te_3d_director.on_screen_characters(shot))
    except Exception as e:  # noqa: BLE001 - 导演台不可用时保守判为「无出场角色」
        app.logger.warning("[3D导演台] 解析出场角色失败（按无出场角色处理）：%s", e)
        return []


def _shot_has_on_screen(shot) -> bool:
    """本镜是否有**画面内出场角色**（读 :func:`_on_screen_characters` 的权威口径）。

    正常路径下 ``_allocate_storyboard_refs`` 已把该口径以 ``shot['_on_screen_chars']``
    挂回；字段**缺失**时保守返回 ``True``（沿用旧行为）—— 避免某条没走分配器的调用路径被
    误判成无人物镜、丢掉构图基准图或加错提示词（宁可少禁、不可误禁）。
    """
    if not isinstance(shot, dict):
        return True
    return bool(shot.get("_on_screen_chars", True))


def _shot_has_char_ref(shot) -> bool:
    """本镜是否有**已命中资产的角色身份参考图**（读 ``_allocate_storyboard_refs`` 挂回的
    ``shot['_char_ref_names']`` = :func:`_match_shot_chars` 的 ``chars_in``）。

    ## 为什么渲染 3D 人偶基准图还要再过这一关（2026-10-05）

    ``COMPOSITION_BASELINE_SECTION``（comfyui_client.py）要求模型「用其他参考图里的角色
    **完全覆盖**人偶」。若本镜声明了角色、但角色**资产缺失**（``_match_shot_chars`` 返回
    ``[]``、``_no_reference=True``），refs 里就只有场景图、**没有角色图可覆盖** → 模型只能
    照抄人偶，症状与「画外音兜底」逐字相同。故渲染门槛在 ``_shot_has_on_screen``（declared）
    之外，**还须** ``_shot_has_char_ref``（declared ∩ matched）。

    ⚠️ 本判据是 ``_shot_has_on_screen`` 的**子集**，行为只会更保守（不插 ref、少调一次
    ``render_blocking``），**不会与渲染器产生矛盾输出**，因此不构成「第三个会分叉的口径」。

    字段**缺失**时保守返回 ``True``：与 :func:`_shot_has_on_screen` 同风格 —— 避免某条未走
    ``_allocate_storyboard_refs`` 的调用路径被误判成「无角色图」而丢掉基准图（宁可多渲、
    不可误禁）。注意：真走到渲染前的正常路径**一定**已挂该字段。
    """
    if not isinstance(shot, dict):
        return True
    return bool(shot.get("_char_ref_names", True))


#: 取「半身档」角色参考图的景别集合（近景类）。
#: 为什么是这些：角色立绘是**全身**，而近景类镜头要求参考图与目标取景同向 ——
#: 全身立绘会把模型往全景方向拉（2026-09-25 实测 shot_24：近景规定、出图近全身）。
#: ⚠️ 景别取值来自 config.SHOT_TYPES（唯一权威表）；新增近景类景别时要同步加进来，
#:    否则该景别会静默走全身档、重新引入「画幅对抗」。
_FRAMING_HALF_SHOT = ("大特写", "特写", "近景", "中近景", "局部", "中景")


def _framing_wants_half_shot(shot: dict) -> bool:
    """A1：按**权威景别字段**判断本镜角色参考图是否取「半身档」。

    与 :func:`_framing_wants_half` 的差别：优先读 ``shot_type``（新剧本权威字段），不再靠
    解析「特写推入」这类复合串去猜；``shot_type`` 与 ``camera`` **都为空**时返回 False
    （全身档）—— 与原实现「空值返回 False」同口径，绝不把「景别未指定」误判成中景。
    """
    if not isinstance(shot, dict):
        return False
    st = str(shot.get("shot_type") or "").strip()
    if st:
        return st in _FRAMING_HALF_SHOT
    _raw = str(shot.get("camera") or "").strip()
    if not _raw:
        return False
    return _camera_key(_raw) in _FRAMING_HALF_SHOT


def _pick_char_view(char_payload: dict, want_half: bool, outfit_dir: str = "") -> str:
    """取角色参考图：**始终返回整张设定图 base.png**（2026-10-02 起不裁剪）。

    ⚠️⚠️ **2026-10-02 用户指定「角色图不用裁剪，给整个图片就行」** —— 本函数
    从「按景别对档挑 half/front/back」退化为「**只取整图 base.png**」。动机与配套：
      · 角色设定图已改为**英文四区 character sheet**（Top 三视图 / Left 面部+配色 /
        Bottom 细节 / Right 比例参照，见 `comfyui_client._CHARACTER_SHEET_EN_LAYOUT`），
        四区不对称布局**无法再做行列投影切分** → 单视角切分（front/left/back/half）
        失去产出依据；
      · 用户要「整图给模型自己取视角」，不再由我们按景别裁单视角。
    ⚠️ 取舍（风险已向用户说明）：取消「景别对档」会**重新引入画幅对抗**（全景/近景
    参考图同用一张整图，近景/特写的构图牵引变弱）——这是 2026-09-25 那套「半身档」
    要解决的问题。用户明确选择「不裁剪」，故此处按要求退化；`want_half`/`outfit_dir`
    参数**保留签名**（调用方不破），但不再影响取图结果（仅整图）。
    ⚠️ 回滚点：如需恢复景别对档，改回 order 的 half/front 分支即可。

    服装变体（衣柜，2026-10-02）：``outfit_dir`` 非空时**仍优先**在该变体目录里取
    整图（base.png），变体目录没有 / 异常 → 回落主设定图（fail-open，零回归）。
    """
    payload = char_payload or {}
    # 只取整图：base.png →（兜底）front.png（存量资产可能只有 front）
    order = ("base", "front")
    if outfit_dir:
        try:
            _od = comfyui_client.resolve_local_path(outfit_dir) or outfit_dir
            if os.path.isdir(_od):
                _hit = _first_existing(*[os.path.join(_od, f"{k}.png") for k in order])
                if _hit:
                    return _hit
        except Exception as _oe:  # noqa: BLE001  变体取图失败绝不拖垮参考图链
            app.logger.debug("服装变体取图失败（回落主设定图）：%s", _oe)
    cands = [comfyui_client.resolve_local_path(payload.get(k) or "") for k in order]
    # 资产目录约定路径兜底（前端未上报时）
    d = payload.get("_dir")
    if d and os.path.isdir(d):
        cands += [os.path.join(d, f"{k}.png") for k in order]
    return _first_existing(*cands) or ""


def _shot_outfit_dir(shot: dict, character_name: str, char_dir: str) -> str:
    """解析「本镜服装提示」→ 该角色**已生成**的服装变体目录（outfits/<key>/）。

    服装提示来源（按优先级）：
      ① ``shot["outfit"]``（本镜服装提示，字符串）；
      ② ``shot["character_outfits"]`` 按角色名查（值可为字符串，或
         {outfit_key|key|outfit|desc: ...} 形态的字典；键走别名归一匹配）。
    解析：剥掉「本套服装：/服装：/outfit(_key)?:」前缀后，与该角色主设定目录下
    ``outfits/`` 的**已存在**子目录名做精确匹配（含书名号/引号剥离与安全化键）——
    只认磁盘上真实存在的变体，解析出的名字没建过目录就回落主设定图。

    ⚠️ fail-open：任何一步解析不出 / outfits 目录不存在 → 返回 ''，调用方按
    「无变体」走主设定图（与改造前行为完全一致）。查不到表也绝不抛异常。
    """
    if not isinstance(shot, dict) or not char_dir or not os.path.isdir(char_dir):
        return ""
    outfit_root = os.path.join(char_dir, _OUTFITS_DIRNAME)
    try:
        known = [d for d in os.listdir(outfit_root)
                 if os.path.isdir(os.path.join(outfit_root, d))]
    except OSError:
        return ""
    if not known:
        return ""
    raw = str(shot.get("outfit") or "").strip()          # ① 本镜服装提示（优先）
    if not raw:
        co = shot.get("character_outfits")               # ② 按角色名查
        if isinstance(co, dict):
            v = co.get(character_name)
            if v is None:
                _norm = _normalize_char_alias(character_name)
                for k, vv in co.items():
                    if _normalize_char_alias(str(k)) == _norm:
                        v = vv
                        break
            if isinstance(v, dict):
                v = (v.get("outfit_key") or v.get("key")
                     or v.get("outfit") or v.get("desc") or "")
            raw = str(v or "").strip()
    if not raw:
        return ""
    _m = re.match(r"^(?:本套服装|服装|outfit(?:_key)?)\s*[:：=]\s*(.+)$", raw)
    cand = (_m.group(1) if _m else raw).strip()
    for k in (cand, cand.strip("「」《》\"'“”‘’")):
        if k in known:
            return os.path.join(outfit_root, k)
    _sk = _sanitize_outfit_key(cand)
    if _sk and _sk in known:
        return os.path.join(outfit_root, _sk)
    return ""


def _scene_view_for_shot(shot) -> str:
    """本镜**机位**该取哪个场景档（``front`` / ``left45`` / ``right45`` / ``top``）。

    与景别（`shot_type` / `camera` 里的特写/近景/全景）**正交**：景别决定取景范围，
    机位决定观察方向。映射表单一来源在 :data:`config.SCENE_ANGLE_TO_VIEW`，
    未列出的机位（含未指定即空串）一律回落到正面档。

    ⚠️ 机位只能从**复合串** `camera` 解析（`shot_type` 只登记景别，无角度信息）；
    `camera_motion` 是 A1 新增的运镜权威字段，老剧本为空、新剧本可能把「俯拍缓推」
    写在那里，故作为**第二来源**（不是第一条）：先正规字段，再权威字段，
    两边都没有才判「未指定」。
    """
    if not isinstance(shot, dict):
        return "front"
    ang = _camera_angle(str(shot.get("camera") or "")) or \
        _camera_angle(str(shot.get("camera_motion") or ""))
    return SCENE_ANGLE_TO_VIEW.get(ang, "front")


def _pick_scene_view(scene_payload: dict, shot) -> str:
    """按镜头机位从场景资产里挑一张**同机位**的场景参考图（2026-09-29 新增）。

    动机：场景此前只有一张基准图，俯拍 / 斜侧镜头拿到的都是正面图，构图先验与镜头
    要求**反向**（「要求俯拍却给了平视」既没被约束、也没被检出）。场景现在按机位档
    逐档出图（`config.SCENE_VIEW_KEYS`），这里负责把镜头机位映射到档位。

    ⚠️ 必须**逐级优雅降级**，绝不返回空（与 `_pick_char_view` 同口径）：返回空会让该镜
    判成「无场景锚点」，把「机位档缺失」这种**加值项缺失**升级成「镜头不能出图」。
    回退链：
      · 目标档 → front → base（该档没生成 / 是老项目没有机位档）
      · 目标档本身就是 front → front → base
    `base` 是每个场景都有的兜底（正面机位出图），故链尾一定落得住。

    ⚠️ 刻意**不做**「俯拍档缺失就退而取斜侧档」这类跨机位借用：俯视与仰拍是反向机位，
    拿俯视图当仰拍镜头的锚点会把构图拽反 —— 「误配比丢图更糟」（丢图只是没有锚点，
    误配是把错误机位焊进画面，且日志看不出来）。宁可回落正面档。

    ⚠️ 链尾必须是 ``entry["image"]``（2026-09-29 实修）：它是**长期存在的主图契约**，
    `_build_asset_index` 与历史生产者都填它，但**不一定**同时给 `front`/`base`/`_dir`。
    少了这一档，任何「只填 image」的场景索引都会静默丢场景锚点 —— 这不是假想：
    改完当场被 ``probe_scene_match.py`` 的端到端用例抓出（fixture 就是只填 image 的形态）。

    ⭐ 2026-10-05 场景九宫格（SCENE_GRID_MODE）：**彻底关机位对档、纯整图直接用**。
       网格模式下场景资产本体是 9 机位拼成的 ``grid.png``，下游**所有镜头机位**都喂
       这张整图（不再按机位选档）——这是用户 2026-10-05 三轮确认的选择。故函数体
       第一步就先查 ``grid``；查不到（老项目 / 关网格）才走下面原有的按机位逐级回退。
    """
    payload = scene_payload or {}
    # ⭐ 九宫格模式：优先整图（grid.png），不再按镜头机位选档（关机位对档）。
    #    用既有回退链工具保证「网格图缺失时平滑回落旧行为」，绝不返回空。
    if SCENE_GRID_MODE:
        _g = comfyui_client.resolve_local_path(payload.get(SCENE_GRID_FILENAME) or "")
        _gd = payload.get("_dir")
        _g2 = os.path.join(_gd, SCENE_GRID_FILENAME) if (_gd and os.path.isdir(_gd)) else ""
        _grid_pick = _first_existing(_g, _g2)
        if _grid_pick:
            return _grid_pick
        # 网格主图缺失（老项目未重跑 / 拼接失败）→ 继续走下方按机位回退，绝不静默丢图。
    target = _scene_view_for_shot(shot)
    order = []
    for k in (target, "front", "base"):
        if k and k not in order:
            order.append(k)
    cands = [comfyui_client.resolve_local_path(payload.get(k) or "") for k in order]
    # 资产目录约定路径兜底（前端未上报 / 索引未挂到该档时）
    d = payload.get("_dir")
    if d and os.path.isdir(d):
        cands += [os.path.join(d, f"{k}.png") for k in order]
    # 最后回落主图契约（老索引 / 外部生产者只填 image 的形态）
    cands.append(comfyui_client.resolve_local_path(payload.get("image") or ""))
    return _first_existing(*cands) or ""


def _h3_shot_ref_components(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                            character_refs: list = None,
                            main_char_img: list = None,
                            outfit_map: dict = None) -> list:
    """把一个分镜解析成**有序参考图组件**（不构建提示词、不决定槽位）。

    返回 ``[{"kind": "character"|"item"|"scene", "name": str,
             "path": 本地路径, "appearance": str}, ...]``，
    顺序恒为「角色 → 物品 → 场景」——与 ``comfyui_client._h3_picture_defs`` 里
    三段的排放顺序一致（提示词的 ``<Picture N>`` 就按这个序推下去）。

    ## 为什么要抽出来（2026-09-30，H3 Director 公共参考图）

    「公共参考图」的判据是「全段都在用、且用的是同一张图」，要在 worker 循环**之前**
    先把每个镜头的资产解析一遍才能求交集；而 ``_shot_segment`` 里原来那段解析是
    内联的。两处各写一份必然漂移（公共池按 A 口径选、段级按 B 口径挂 → 公共图挂错镜）。
    故收敛到本函数：公共池规划与段级挂图**共用同一份解析结果**，
    并且解析只做一次（``_match_shot_chars`` / ``_resolve_item_names`` /
    ``_resolve_scene_entry`` 都会往 ``shot["_ref_warnings"]`` 记账，
    重复调用会把同一句告警记两遍）。

    ⚠️ 解析口径必须是**逐镜对档**后的结果，不能退化成「资产的基准图」：
      · 角色按景别取半身/全身档（``_pick_char_view``，2026-09-25「景别对档」）；
      · 场景按机位取 front/left45/right45/top 档（``_pick_scene_view``，2026-09-29）。
    公共池要的是「同一张图」，所以上图**按路径**判交集——档位不同的同一资产
    天然不满足，这正是「公共锁定」不会把对档工作废掉的原因（见 h3_common_refs）。
    """
    if not isinstance(shot, dict):
        return []
    out: list = []

    # ---- 本镜角色：每人一张（按景别对档），去重保序 ----
    # outfit_map（2026-10-02 服装变体）：「角色名 → outfits/<key>/ 目录」的可选映射，
    # 由调用方解析本镜服装提示（shot.outfit / shot.character_outfits）后传入；
    # None（默认）/ 缺该角色 → 走主设定图，与旧行为逐字一致（视频 worker 调用点
    # 位于 6200 行后的区域、本轮不可改动，故暂以默认 None 接线，见最终报告）。
    _want_half = _framing_wants_half_shot(shot)
    _seen_paths: set = set()
    for _mc in (_match_shot_chars(shot, char_idx) or []):
        _entry = char_idx.get(_mc) or {}
        _od = ""
        if outfit_map:
            _od = str(outfit_map.get(_mc)
                      or outfit_map.get(_normalize_char_alias(_mc)) or "")
        _p = _pick_char_view(_entry, _want_half, _od)
        if not _p or _p in _seen_paths:
            continue
        _seen_paths.add(_p)
        out.append({"kind": "character", "name": _mc, "path": _p,
                    "common_key": f"char:{_mc}",
                    "appearance": _entry.get("appearance")
                    or _entry.get("description") or ""})

    # ---- 本镜物品（与角色图重复的丢弃，保持旧口径）----
    for _it in (_resolve_item_names(shot, item_idx, "H3视频段") or []):
        _entry = item_idx.get(_it) or {}
        _p = _entry.get("image")
        if not _p or _p in _seen_paths:
            continue
        _seen_paths.add(_p)
        out.append({"kind": "item", "name": _it, "path": _p,
                    "appearance": _entry.get("appearance")
                    or _entry.get("description") or ""})

    # ---- 本镜场景（按机位取档）----
    _loc, _scene_entry = _resolve_scene_entry(shot, scene_idx, "H3视频段")
    _scene_img = _pick_scene_view(_scene_entry, shot) if _scene_entry else None
    if _scene_img:
        out.append({"kind": "scene", "name": _loc or "本镜场景", "path": _scene_img,
                    "appearance": ""})

    # ---- 兜底：逐镜角色一张都没解析出来 → 退回全局主角锚点（保持原行为）----
    # ⚠️ 旧实现把整个 refs 直接替换成 `[sb] + main_char_img`，于是本镜的**物品/场景锚点
    #    连同「提示词已声明」一起被丢掉**（声明在、图不在 → 编号错位）。这里改成
    #    「主角锚点排到最前、其余组件保留」，发送的图只多不少、且每一张都有声明。
    # ⚠️ 旧实现是 `refs = [sb] + main_char_img` 但提示词只声明 1 个角色 —— 图比声明的多，
    #    多出来的那张「有图无声明」（模型拿到一张没说用途的图）。这里按**实际张数**
    #    逐个声明，把编号对齐。
    if not any(c["kind"] == "character" for c in out) and main_char_img:
        _ref0 = (character_refs or [{}])[0] if character_refs else {}
        _nm = (_ref0.get("name") if isinstance(_ref0, dict) else None) or "主角"
        _ap = (_ref0.get("appearance") or _ref0.get("description") or "") \
            if isinstance(_ref0, dict) else ""
        _fb = []
        for _p in list(main_char_img):
            if not _p or _p in _seen_paths:
                continue
            _seen_paths.add(_p)
        _fb.append({"kind": "character", "name": _nm, "path": _p,
                    "common_key": f"char:{_nm}",
                    "appearance": _ap})
        out = _fb + out      # 角色在前（与 _h3_picture_defs 的段序一致，且保序）
    return out


def _h3_plan_common_refs(shots: list, char_idx: dict, item_idx: dict, scene_idx: dict,
                         character_refs: list = None, main_char_img: list = None,
                         sb_map: dict = None, use_storyboard: bool = True,
                         project_name: str = ""):
    """按「全段都在用、且同一张图」挑出公共参考图（H3 Director 公共参数）。

    返回 ``(common, comps_map)``：
      · ``common``: 公共组件列表（顺序 = 第 1 镜的出现顺序，已按上限截断）；
      · ``comps_map``: ``{shot_id: [组件, ...]}`` —— 每镜的解析结果，
        供 ``_shot_segment`` 复用（避免解析两遍造成重复告警）。

    ⚠️ 关掉开关或只有 1 个镜头时返回 ``([], {})``：**完全走原路径**，零行为变更。
    单镜头（per_shot 重跑）没有「跨段公共」可言，硬开公共池只会把段级图挪到全局，
    收益为零还多一层 ``commonEnabled`` 耦合。

    ⚠️ **要求每个镜头都有分镜图**，否则整集公共化会被整体放弃（返回 ``([], {})``）：
    公共图由客户端写进 ``global.refs``，它被 merge 进**每一段**，而提示词侧的
    ``<Picture 1..K>`` 只有在「分镜图分支」（``sb_local`` 为真）才会被生成。
    只要有一镜走了无分镜图的兜底分支，它的 ``<Picture N>`` 就从 1 起重新数，
    与槽位（公共块从 0 起）整体错位 —— 那是**静默错图**，宁可不做这个特性。
    """
    if not H3_COMMON_REFS or len(shots or []) < 2:
        return [], {}
    _shots = [s for s in shots if isinstance(s, dict)]
    # 逐镜解析结果按 shot_id 缓存复用（避免解析两遍），故 shot_id 必须唯一且非空 ——
    # 一旦重复/缺失（历史剧本偶有），缓存会互相覆盖 → 公共池按 A 镜选、B 镜挂错图。
    _ids = [str(s.get("shot_id") or "") for s in _shots]
    if "" in _ids or len(set(_ids)) != len(_ids):
        app.logger.warning(
            "[H3公共参考图] 镜号缺失或重复（%s…）→ 放弃公共化（逐镜缓存需要唯一镜号）",
            [x or "<空>" for x in _ids[:8]])
        return [], {}
    if sb_map is not None:
        _missing_sb = [s.get("shot_id") for s in _shots
                       if not (use_storyboard
                               and sb_map.get(_norm_shot_key(s.get("shot_id"))))]
        if _missing_sb:
            app.logger.info(
                "[H3公共参考图] %d 个镜头没有分镜图（%s…）→ 放弃公共化"
                "（公共图会被 merge 进每一段，缺分镜图的段编号会整体错位）",
                len(_missing_sb), _missing_sb[:5])
            return [], {}
    comps_map = {}
    per_shot_assets = []
    for _s in _shots:
        _sid = str(_s.get("shot_id") or "")
        _comps = _h3_shot_ref_components(_s, char_idx, item_idx, scene_idx,
                                         character_refs, main_char_img)
        comps_map[_sid] = _comps
        per_shot_assets.append(_comps)
    common = h3_common_refs.plan_common_refs(
        per_shot_assets, max_common=H3_COMMON_REFS_MAX,
        limit=h3_director_builder.MAX_REFERENCE_IMAGES)
    # ⭐ 全局风格参考图（2026-10-03）：项目 config.json 的 style_ref_image 作为最后一张公共图。
    if project_name:
        try:
            _proj_root = _safe_project(project_name)
            if _proj_root:
                # 修复（2026-10-05）：_safe_project 只返回项目键，config.json 必须拼在
                # 项目工作区**绝对路径**下（output/projects/<键>/config.json），
                # 否则相对路径基于 CWD、必然读不到（style_ref_image 相对写法同理）。
                _proj_dir = project_store.paths(_proj_root)["root"]
                _cfg = json.load(open(os.path.join(_proj_dir, 'config.json'), encoding='utf-8'))
                _style_ref = str(_cfg.get('style_ref_image') or '').strip()
                if _style_ref:
                    _style_ref_path = _style_ref if os.path.isabs(_style_ref) else os.path.join(_proj_dir, _style_ref)
                    if os.path.isfile(_style_ref_path) and len(common) < h3_director_builder.MAX_REFERENCE_IMAGES:
                        common = list(common) + [{'kind':'scene','name':'全局风格参考','appearance':'global style / lighting anchor','path':_style_ref_path}]
                        app.logger.info('[H3公共参考图] 追加全局风格参考图：%s', os.path.basename(_style_ref_path))
        except Exception as _e:  # noqa: BLE001
            app.logger.debug('[H3公共参考图] 风格参考图解析失败（忽略）：%s', _e)
    if common:
        app.logger.info(
            "[H3公共参考图] %d 镜中 %d 项全段共用 → 走 global.refs + commonEnabled：%s",
            len(per_shot_assets), len(common), h3_common_refs.describe(common))
    else:
        app.logger.info(
            "[H3公共参考图] %d 镜无「全段都在用且同一张图」的资产 → 维持逐段 refs",
            len(per_shot_assets))
    return common, comps_map


def _h3_is_common_comp(comp: dict, common_keys: set) -> bool:
    """该组件是否属于公共池（身份键 = 种类 + 名称 + 图片路径，与 h3_common_refs 同源）。"""
    return h3_common_refs.asset_key(comp) in common_keys


def _project_worldview(project_name: str) -> str:
    """取项目的「世界观设定」文本（2026-10-03）：供公共提示词 WORLD 行拼接用。

    来源优先级：① autopilot plan 的 brief（总控 AI 敲定的故事概述）；
    ② 项目 config.json 的 note。两者皆空则返回 ""（公共段不写 WORLD 行，零变更）。
    纯只读、永不抛。
    """
    _pw = ""
    try:
        _plan = (autopilot.get_plan(project_name) or {})
        _brief = str(_plan.get("brief") or _plan.get("worldview") or _plan.get("era_world") or "")
        if _brief and len(_brief) <= 400:
            _pw = _brief
    except Exception:  # noqa: BLE001
        pass
    if not _pw:
        try:
            _proj = _safe_project(project_name)
            if _proj:
                # 修复（2026-10-05）：项目键 ≠ 目录，config.json 按 project_store 的
                # 项目工作区绝对路径拼（同 _h3_plan_common_refs 处的口径）。
                _cfg = json.load(open(project_store.paths(_proj)["config"], encoding="utf-8"))
                _note = str(_cfg.get("note") or "")
                if _note and len(_note) <= 200:
                    _pw = _note
        except Exception:  # noqa: BLE001
            _pw = ""
    return _pw


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


def _h3_common_ref_audios(common: list, project_name: str, all_names=None) -> list:
    """从公共池角色收集 voice_bank 参考音色（2026-10-02 公共参考音色）。

    只对 ``kind == character`` 的公共项，取其在 voice_bank 里已登记的参考音频
    （``find_voice_bank_ref``，缺则跳过＝该角色无绑定音色，绝不误挂别的声音）。
    返回**有序**的 ``(角色名, 本地绝对路径)`` 列表，供 ComfyUI 侧写进
    ``global.refAudios``（index 0..M-1）**并**在公共提示词里做**逐行角色归属**
    （``<Audio N> = 角色名``）。
    无公共角色 / 无音色库 / 全部缺音色 → 返回 ``[]``（零行为变更，走原 generate 无参考）。

    ⭐ 2026-10-05：从「纯路径列表」改为 ``(角色名, 路径)`` 有序对 —— 因为本函数会
    **跳过无音色的角色**，下游靠公共池顺序反推「哪个 <Audio> 属于谁」会错位；
    携带角色名才能逐行准确归属。下游两处消费（build 侧取 [1]、subject_lock 侧取 [0]）
    需同步。
    """
    _char_names = [str(c.get("name") or "").strip()
                   for c in (common or [])
                   if isinstance(c, dict) and c.get("kind") == "character"
                   and str(c.get("name") or "").strip()]
    # ⭐ 2026-10-10（用户方案②：**音色扩到全角色，参考图维持现状**）：
    #    「公共池」的判定是「全段共用」，于是只在部分镜出现的角色（老周/船长）
    #    拿不到自己的参考音色 —— 实测 voice_bank 里有 3 个角色，只有林昭挂上了。
    #    音频槽上限 3（ref_audio_0..2），而参考图槽上限 9 本就紧张，
    #    故这里只把**音色**补到全角色，参考图仍维持「全段共用才进公共池」。
    #    公共池里已有的角色优先，其余角色按传入顺序追加（超出 3 个由 _ref_audio_items 截断）。
    if all_names:
        _extra = (list(all_names.keys()) if isinstance(all_names, dict)
                  else list(all_names))
        for _n in _extra:
            _n = str(_n or "").strip()
            if _n and _n not in _char_names:
                _char_names.append(_n)
    if not _char_names:
        return []
    try:
        _dub_dir = _dub_project_dir(project_name)
    except Exception:  # noqa: BLE001
        return []
    _out: list = []
    _seen = set()
    for _cn in _char_names:
        try:
            _ref, _ = find_voice_bank_ref(_dub_dir, _cn)
        except Exception:  # noqa: BLE001
            _ref = ""
        if not _ref or not os.path.isfile(_ref):
            app.logger.info("[H3公共参考音色] 角色 %s 未绑定参考音色，跳过（不挂声）", _cn)
            continue
        _key = os.path.normcase(os.path.normpath(os.path.abspath(_ref)))
        if _key in _seen:
            continue
        _seen.add(_key)
        _out.append((_cn, _ref))
    if _out:
        app.logger.info("[H3公共参考音色] 公共角色音色 %d 支 → global.refAudios：%s",
                        len(_out), "、".join(n for n, _p in _out))
    return _out


def _h3_audio_defs_for(common: list, project_name: str, characters=None) -> list:
    """把公共角色参考音色转成 :func:`h3_prompt_kit.build_ref2va` 的 ``audio_defs``。

    ⭐ 2026-10-09 官方 Ref2VA 对齐（MiniMax-H3 技能 references/ref-en.txt）：音色参考
    必须在六段里**显式声明**，四个位置都要出现 ``<Audio N>`` ——
      subject_definitions: ``<Audio 1> is the voice-timbre reference for <Subject 1> (S1)``
      summary:             ``[reference generation + audio reference]``
      retention_analysis:  ``<Audio 1>: reference - its vocal timbre guides ...``
      detailed_description:``... using the voice timbre referenced from <Audio 1> ...``
    此前我们只把音频文件写进 ``global.refAudios``，提示词里仅有一句非官方的
    ``<Audio 1> = 角色名``，H3 拿到音频却不知道用途 → 自己编嗓子（中英混杂 / 多说话人）。
    """
    try:
        # 与写进 global.refAudios 的那一份**必须同源同序**（用户方案②）：_
        # 否则提示词声明的 <Audio N> 与真实挂载的音频条数/顺序不一致，
        # H3 会把音色归错人。characters 在这里既是音色补齐来源，也是 voice_style 来源。
        _pairs = _h3_common_ref_audios(common, project_name, all_names=characters)
    except Exception as _e:  # noqa: BLE001
        app.logger.warning("[H3音色声明] 收集参考音色失败（按无声处理）：%s", _e)
        return []
    if not _pairs:
        return []
    _vs_map = {}
    for _c in (characters or []):
        if isinstance(_c, dict) and _c.get("name"):
            _vs_map[str(_c["name"]).strip()] = str(_c.get("voice_style") or "").strip()
    _out = []
    for _i, _item in enumerate(_pairs, start=1):
        _nm = str((_item[0] if isinstance(_item, (list, tuple)) else _item) or "").strip()
        _vs = _vs_map.get(_nm) or ""
        # ⚠️ 官方要求六段正文用英文；中文 voice_style 不直接塞进去（会诱导中文发声），
        #    非 ASCII 时留空，由 h3_prompt_kit 的性别兜底短语接管。
        _voice = _vs if (_vs and _vs.isascii()) else ""
        _out.append({"name": _nm, "label": f"<Audio {_i}>", "speaker": f"S{_i}",
                     "voice": _voice})
    app.logger.info("[H3音色声明] 生成 %d 条 <Audio N> 声明：%s", len(_out),
                    "、".join(d["name"] + d["label"] for d in _out))
    return _out


def _h3_common_subject_lock(common: list, common_ref_audios: list,
                        style: str = "", worldview: str = "", aspect: str = "") -> str:
    """公共提示词 subject lock（2026-10-02）：角色/物品/场景锁定句 + 公共音色指代。

    编号与公共槽位**逐位对齐**：公共图 index 0..K-1 → ``<Picture 1..K>``（1-based）；
    公共音色 index 0..M-1 → ``<Audio 1..M>``。插件在 commonEnabled 时把本句拼在**每段
    提示词之前**（plan.py:concat_common_segment_prompt），作为全集一致的主体锁定前缀，
    段级提示词只管本镜叙事，主体外观/音色由这句统一锁定（不再逐镜重述）。

    ⚠️ 提示词消费的是 ``<Picture N>``/``<Audio N>``（H3 模型认的标签）；``@image#N``
    只是 ComfyUI 前端 @ 选择器的 UI chip，不进模型 —— 故这里用 ``<>`` 标签。
    无公共项且无公共音色 → 返回 ``""``（零行为变更，global.prompt 留空）。
    """
    _parts: list = []
    _K = len(common or [])
    if _K:
        _lock = ["Subject lock — keep these shared references consistent across every shot:"]
        for _i, _c in enumerate(common or []):
            _num = _i + 1
            _name = str(_c.get("name") or "").strip()
            _kind = _c.get("kind")
            _app = str(_c.get("appearance") or _c.get("description") or "").strip()
            _ap = (f", {_app[:80]}" if _app else "")
            if _kind == "character":
                _lock.append(
                    f"<Picture {_num}> is {_name}{_ap} — keep this character's facial "
                    "identity, hairstyle, build, outfit and rendering style identical in all shots.")
            elif _kind == "item":
                _lock.append(
                    f"<Picture {_num}> is {_name}{_ap} — keep this prop's shape, material "
                    "and colors identical in all shots.")
            else:
                _lock.append(
                    f"<Picture {_num}> is the scene {_name} — keep its spatial layout and "
                    "lighting anchor; framing follows the per-shot camera, not this image.")
        _parts.append(" ".join(_lock))
    _M = len(common_ref_audios or [])
    if _M:
        # ⭐ 2026-10-05：音色参考音频**每个单独一行、更显眼、带角色名归属**。
        # common_ref_audios 现为 ``[(角色名, 路径)]`` 有序对（见 _h3_common_ref_audios）；
        # index 0..M-1 → <Audio 1..M>，逐行写「<Audio N> = 角色名」，模型不再猜哪支音色是谁。
        _voice_lines = []
        for _i, _item in enumerate(common_ref_audios or []):
            # 兼容：正常是 (名, 路径) 元组；万一上游仍传纯路径字符串，取不到名字就用「角色_{i+1}」。
            if isinstance(_item, (list, tuple)) and len(_item) >= 1:
                _vn = str(_item[0] or "").strip() or f"角色{_i + 1}"
            else:
                _vn = f"角色{_i + 1}"
            _voice_lines.append(f"<Audio {_i + 1}> = {_vn}")
        _parts.append(
            "VOICE TIMBRE REFERENCE — 本集角色参考音色（贯穿全片共用，逐行对应）:\n"
            + "\n".join(_voice_lines)
            + ". Each reference voice drives that character's spoken lines across every "
              "shot; keep the timbre of each character consistent with its reference "
              "audio (do not switch between voices for the same character).")
    
    # ---- 公共提示词补全：世界观 + 全局 STYLE（2026-10-03）----
    # 按「推荐放公共参数的内容」表：公共提示词 = 角色定义(上面 subject lock) +
    # 世界观设定 + 全局 STYLE（画风 / 比例 / 无字幕 / 稳定构图）。这些会被插件拼在
    # **每段提示词之前**（concat_common_segment_prompt），保证全片一致；镜头特有的
    # 动作/构图/景别/运镜/光线只写在组内，不进公共段。
    _w = str(worldview or "").strip()
    if _w:
        _parts.append(f"WORLD (world setting, holds for every shot): {_w}.")
    _st = str(style or "").strip()
    if _st:
        _aspect = ("/" + str(aspect or "").strip()) if str(aspect or "").strip() else ""
        _parts.append(
            f"GLOBAL STYLE (holds for every shot): {_st}{_aspect} vertical cinematic, "
            "no text, no subtitles, stable composition, consistent lighting and "
            "colour grading.")
    return "\n".join(_parts)


def _build_identity_ref_grid(char_paths: list, out_path: str, target_size=None) -> str:
    """把角色基准图拼成 3×3「身份基准网格」参考图（2026-10-06，用户拍板）。

    单镜九宫格的 9 个面板在**一次扩散**里生成，格间服装/发型漂移是扩散模型已知
    行为（逐格重画），纯提示词约束不住。这里把角色基准图先用 PIL 拼成与输出
    **同构**的 3×3 网格（整体尺寸=分镜目标尺寸，逐像素一致——工作流无尺寸节点，
    输出画幅继承第一张参考图），作靠前参考图传入，配合 comfyui_client 的
    IDENTITY BASELINE GRID / PANEL-WISE IDENTITY BINDING 提示词协议，给每个输出
    面板一个视觉身份锚点。同构先例：3D 导演台站位基准网格（COMPOSITION BASELINE
    GRID 协议）。

    格位填充：按传入顺序循环填满 9 格（多角色/多视图 → 依次轮转）。
    ⚠️ 2026-10-07 起 **9 格同图模式已停用**：仅 1~2 张图（单角色单视图）时
    拼出的 9 份同图拷贝会被扩散模型当成「输出范例」、直接诱导九格趋同
    （用户反馈「9 格还是有重复」）—— 调用方已改为**去重后 ≥3 张不同图才建
    网格**（<3 图不建网格，直接用原始参考图锚定身份）。全不可读时抛
    ValueError，由调用方降级（fail-open，不阻断生成）。
    """
    from PIL import Image
    paths = [p for p in (char_paths or []) if p and os.path.isfile(p)]
    if not paths:
        raise ValueError("没有可用的角色基准图")
    w, h = (int(target_size[0]), int(target_size[1])) if target_size else (1632, 928)
    # 格边界：末列/末行吸收整除余数，保证整图尺寸与 target_size 逐像素一致
    xs = [0, w // 3, (w // 3) * 2, w]
    ys = [0, h // 3, (h // 3) * 2, h]
    sheet = Image.new("RGB", (w, h), (12, 12, 16))
    for i in range(9):
        r, c = divmod(i, 3)
        im = Image.open(paths[i % len(paths)]).convert("RGB").resize(
            (xs[c + 1] - xs[c], ys[r + 1] - ys[r]))
        sheet.paste(im, (xs[c], ys[r]))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.save(out_path, "PNG")
    return out_path


def _allocate_storyboard_refs(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                               project_name: str = None) -> list:
    """为单个镜头分配参考图（Qwen-Image-2.1 reference stack，最多 9 张）

    槽位键名随编辑节点换代而变（QwenImage2.1 的 TextEncodeQwenImage21 是
    ``images.image_1..9``，老的 TextEncodeQwenImageEditPlus 是 ``image1..3``），
    由 comfyui_client._find_image_slots 统一识别；本函数只负责**按序**给出这些图。

    ## 为什么改成 9 槽位（2026-09-25）

    Qwen-Image-2.1 官方规格支持最多 10 张参考图，且官方 Prompt Rewriter 的核心是
    **Attribute Disentanglement**（属性解耦）：每张参考图解决**一个明确问题**，
    由 ``<image1>…<imageN>`` 显式编号绑定职责。官方同时强调「10 是容量不是目标」——
    塞重复图片会让模型分不清哪张优先。

    旧实现按「主角色 / 次要 / 场景」压成 3 张，导致：多角色镜头第 3 人起直接丢失、
    服装与身份混在同一张图、道具只能挤占角色槽位 —— 这些正是质检重灾区
    （角色/背景不一致 16+11 次）。

    ## 槽位分工（固定顺序，与 build_storyboard_prompt 的 <imageN> 编号一一对应）

      1. ``<image1>`` 主角色身份锚点 —— 兼作画布/构图基线（官方：image_1 是 edit target）
      2. ``<image2>`` 次角色身份（多角色镜头才有；让模型独立保身份，禁止特征串味）
      3. ``<image3>`` 第三角色身份（三人同框时才有）
      4. ``<image4>`` 主角色服装（角色资产图与服装资产不同图时才有独立槽位）
      5. ``<image5>`` 场景
      6. ``<image6>`` 道具（镜头内物品，可多个）
      7. ``<image7>`` 构图参考（特写镜头放头部特写锚点）
      8. ``<image8>`` 上一镜连续性锚点（同场景承接时才有）

    只填**实际需要**的前 N 个：未用到的尾部槽位在生成端被留空
    （见 generate_storyboard 的 slot_cleared），不会塞重复图。
    """
    chars_in = _match_shot_chars(shot, char_idx)
    # ⭐ 2026-10-05：把「本镜画面内可见角色」以**单一权威口径**挂回 shot，供调用点的
    #    3D 构图基准图注入判据复用（见工作线程里 `_shot_has_on_screen(shot)`）。
    #    ⚠️ 取的是**剧本声明**（characters_in_shot），不是 chars_in（= 声明 ∩ 资产索引命中）：
    #    `has_characters` **提示词参数**要的正是这个「本镜该不该有人」的剧本意图。
    shot["_on_screen_chars"] = _on_screen_characters(shot)
    # ⭐ 2026-10-05：另行挂回**已命中资产的角色名**（= chars_in），专供「渲不渲 3D 基准图」的
    #    门槛用（见 `_shot_has_char_ref`）。为什么渲染门槛要比提示词门槛更严：人偶基准图必须
    #    被「其他参考图里的角色」完全覆盖才有意义；角色资产缺失时没有可覆盖的角色图，渲了只会
    #    被照抄（与本次缺陷同症状）。该判据是 `_on_screen_chars` 的子集，只更保守、不会分叉。
    shot["_char_ref_names"] = list(chars_in)
    # 2026-09-29：物品与角色/场景同口径——走统一匹配器（原先 `if n in item_idx`
    # 让名字稍有差异的物品**静默不带参考图**，且不打日志）。
    items_in = _resolve_item_names(shot, item_idx, "分镜参考图")

    refs = []
    # ⭐ 2026-10-05：区分两种「一个角色都没匹配到」——
    #   (a) characters_in_shot **本就为空** → **合法的无人物镜头**（道具特写/空镜/纯画外音），
    #       **不得**置 _no_reference、不写 _ref_error（否则会产出误导文案
    #       「角色 [] 在资产索引中均无匹配」，并让该镜走 S6 报错路径）；
    #   (b) 声明了角色但 resolve 后一个都没命中 → 保持 S6 行为（置 _no_reference + _ref_error
    #       + 日志），由调用方决定 400 / 跳过。
    # ⚠️ 消费方 `if not refs:` 分支（工作线程内）只在前者为「(b) 且无任何其他参考图」时
    #    才触发；`(a)` 或「有场景图但无角色」的镜头 refs 非空 → 该分支不触发（这正是 shot#1
    #    以前被「静默放行」的原因）。
    if not chars_in and (shot.get("_on_screen_chars") or []):
        # S6：禁止静默 take-first —— 镜头声明了角色却一个都匹配不到时，标记 no_reference，
        # 由调用方决定 400（单镜）/ 跳过 + 警告日志（批量）。
        shot["_no_reference"] = True
        shot["_ref_error"] = (
            f"镜头 {shot.get('shot_id')} 的角色 {shot.get('characters_in_shot')} "
            f"在资产索引中均无匹配（别名归一化后仍无）")

    used_paths = set()
    # 本镜景别决定角色参考图取「半身档」还是「全身档」（画幅与景别同向，消除对抗）。
    _want_half = _framing_wants_half_shot(shot)

    # ---- <image1>…<image3>：角色身份锚点（逐个独立，禁止特征串味）----
    # 官方要点：多角色时每人一张图 + 明确「independently / Do not merge facial features」，
    # 否则最常见的失败就是 A 的脸跑到 B、B 的衣服跑到 C。
    for i, name in enumerate(chars_in):
        if i >= 3:
            break                       # 3 个角色身份槽位；第 4 人起并入构图说明
        payload = char_idx.get(name, {}) or {}
        # 2026-09-25 景别对档：近景/特写/中景优先取 half.png（半身胸像），
        # 全景/远景优先取 front.png（全身）；档位缺失时逐级回退（见 _pick_char_view）。
        # 2026-10-02 服装变体：本镜服装提示（shot.outfit 优先，其次
        # shot.character_outfits 按角色名查）能解析出已生成的 outfit_key →
        # 参考图优先取 outfits/<key>/ 的同档位图；解析不出/未生成回落主设定图。
        img = _pick_char_view(payload, _want_half,
                              _shot_outfit_dir(shot, name, payload.get("_dir") or ""))
        if not img or img in used_paths:
            continue
        used_paths.add(img)
        _zoom = "半身近景" if _want_half else "全身"
        if i == 0:
            refs.append(("主角色",
                         f"参考图1（<image1>）是角色「{name}」的身份锚点（{_zoom}视图）："
                         f"保持其面部身份、发型与体型不变", img))
        else:
            refs.append(("次角色",
                         f"参考图{i + 1}（<image{i + 1}>）是角色「{name}」的身份锚点（{_zoom}视图）："
                         f"独立保持其面部身份与发型，不得与其他角色特征混用", img))

    # ---- <imageN>：场景（角色之后，作为环境锚点）----
    # 2026-09-29：走统一容错匹配（精确 → 归一化 → 唯一子串）；未命中时**显式告警**，
    # 不再沿用旧的 `loc if loc in scene_idx else None`——那条路会静默丢场景锚点。
    scene_name, _sc_entry = _resolve_scene_entry(shot, scene_idx, "分镜参考图")
    # 2026-09-29：取**与本镜机位同向**的那一档场景图（俯拍→俯视档 / 斜侧→斜侧档），
    # 而不是恒取基准图 —— 机位反向的参考图会把构图先验拽反。档位缺失时逐级回落。
    _sc_view = _scene_view_for_shot(shot)
    scene_img = _pick_scene_view(_sc_entry, shot) if _sc_entry else None
    if scene_img and scene_img in used_paths:
        scene_img = None

    # ---- <imageN>：道具（镜头内物品，可多个但最多 2 张，避免挤占角色槽位）----
    item_refs = []
    for cand in items_in:
        if len(item_refs) >= 2:
            break
        img = item_idx.get(cand, {}).get("image")
        if not img or img in used_paths or img == scene_img:
            continue
        used_paths.add(img)
        item_refs.append((cand, img))

    slot_no = len(refs) + 1
    if scene_img:
        # 2026-09-29 与场景新规格同步：``novel_to_script`` 的 reference_prompt_zh
        # 已升级为「地理优先」（先定空间结构与地理关系，再定光照基调），此处槽位
        # 话术同步升级，并**显式声明不锁定机位**——场景基准图是一个固定 View，
        # 而分镜是另一个机位；不声明的话模型会照搬参考图的取景范围（实测口径与
        # 特写镜头「场景仅作基调参考」一致）。
        # ⚠️ 改这句话必须同步 comfyui_client._REF_ROLE_ZH2EN（中文职责 → 官方英文
        #    短语）与 prompt_memory._BOILERPLATE（相似度剥离用），否则职责声明会
        #    退化成中文原文、教训召回会被套话稀释。
        # 2026-09-29：参考图已按**本镜机位**选档（俯拍→俯视档…），故话术里点明档位，
        #    让模型知道这张图**就是本镜机位**的空间参考，「不得照搬机位」的豁免随之
        #    收紧为「同一机位、取景范围仍以镜头描述为准」——档位与镜头同向时再照搬
        #    机位不再有害，但**取景范围**（全景 vs 特写）仍必须听镜头。
        refs.append(("场景",
                     f"参考图{slot_no}（<image{slot_no}>）是场景「{scene_name}」"
                     f"（{SCENE_VIEW_LABELS.get(_sc_view) or _sc_view}）的空间与光照锚点："
                     f"保持空间结构、地理关系（入口 / 通道 / 固定陈设的位置关系）"
                     f"与光源方向、色温基调一致；"
                     f"取景范围以本镜镜头描述为准，不得照搬该参考图的构图范围",
                     scene_img))
        used_paths.add(scene_img)
        slot_no += 1
    for cand, img in item_refs:
        refs.append(("道具",
                     f"参考图{slot_no}（<image{slot_no}>）是物品「{cand}」的形状、材质与配色锚点",
                     img))
        slot_no += 1

    refs = _apply_closeup_ref_strategy(refs, shot, project_name)
    # P0-3（借鉴 ViMax reference_image_selector）：每镜参考图**相关性优先 + ≤8 张上限**。
    return _cap_storyboard_refs(refs, shot)


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
CLOSEUP_CHAR_CROP_TOP = (0.32, 0.02, 0.68, 0.28)   # 头部条带（x0, y0, x1, y1）


def _closeup_char_crop(img_path: str, project_name: str, shot_id) -> str:
    """把角色正视/半身图裁剪为头部特写图，作为特写镜头的构图锚点（失败则回退原图）。"""
    try:
        from PIL import Image
        out_dir = os.path.join(QC_DIR, str(project_name or "default"), "closeup_refs")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, f"char_closeup_shot{_shot_seq(shot_id, 1):02d}.png")
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            x0, y0, x1, y1 = CLOSEUP_CHAR_CROP_TOP
            band = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
            side = max(1, min(band.width, band.height))
            left = max(0, (band.width - side) // 2)
            crop = band.crop((left, 0, left + side, side))
            target_w = max(768, crop.width)
            crop = crop.resize((target_w, max(1, int(target_w * crop.height / crop.width))))
            crop.save(dst, format="PNG")
        return dst
    except Exception as e:  # 裁剪失败不影响主流程，回退原图
        app.logger.warning(f"特写参考图裁剪失败，回退原图: {e}")
        return img_path


def _apply_closeup_ref_strategy(refs: list, shot: dict, project_name: str = None) -> list:
    """特写镜头：只保留「角色头部特写」单一锚点。

    实测（4 轮 12 次生成）：混入场景/道具参考时，模型会按参考图的取景范围把画面
    铺开成中全景，并把道具画回手中；道具参考剔除后模型仍会自行"想象"出手持物。
    因此特写镜头只给头部特写锚点，构图约束最强。

    ⚠️ 2026-09-25 口径变化（Qwen-Image-2.1 9 槽位）：
      旧实现「多出的槽位自动复用同一张」在 9 槽模板下会把头部特写复制到 8 个槽位，
      而官方明确「容量不是目标，重复图反而稀释注意力」。现在特写镜头**只输出 1 张**
      （主角色头部特写，占 ``<image1>``），其余槽位由生成端留空。
      多角色特写时保留每个角色的头部特写（各占一槽），不复制。
    """
    # 审计 P2-13（2026-09-29）：特写判定改为 shot_type / camera 双查 —— camera 字段
    # 已拆分（A1），_norm_shots 允许 camera 为纯运镜串（如「推入」）而 shot_type=「特写」；
    # 只看旧 camera 会漏掉这类镜头，与生成端（build_storyboard_prompt 按 shot_framing
    # 注入特写硬约束）判定分裂。
    _is_closeup = ("特写" in str(shot.get("shot_type") or "")
                   or "特写" in str(shot.get("camera") or ""))
    if not _is_closeup:
        return refs
    out = []
    for kind, label, path in refs:
        if kind in ("主角色", "次角色"):
            # 保留 <imageN> 编号锚点，让质检端能继续核对「编号 ↔ 职责」一致性。
            # 审计 P2-19（2026-09-29）：必须完整保留「（<imageN>）」整组 —— 旧实现
            # 只留「参考图N（」，<imageN> 被削掉，下游 _ref_label_body 剥前缀不净
            # （提示词出现「参考图2（已替换为…」脏片段）且特写镜丢身份保留句。
            _mark = ""
            if label.startswith("参考图"):
                _m = re.match(r"(参考图\d+（<image\d+>）)", label)
                if _m:
                    _mark = _m.group(1)
                else:
                    _head = label.split("）", 1)[0]
                    _mark = _head.split("（", 1)[0] + "（"
            out.append((kind,
                        _mark + "已替换为该角色头部特写，画面取景范围以此为准：仅肩部以上",
                        _closeup_char_crop(path, project_name, shot.get("shot_id", 1))))
    return out or refs


def _cap_storyboard_refs(refs: list, shot: dict) -> list:
    """P0-3（借鉴 ViMax reference_image_selector）：每镜参考图**相关性优先 + ≤8 张上限**。

    官方 Qwen-Image-2.1 支持最多 10 张但明确「容量不是目标」；ViMax 实践为每帧 ≤8 张，
    且同角色多视图只取一张（已由 ``_pick_char_view`` 按景别对档处理）。这里在分配末端
    再加一道安全网：超出 8 张时按相关性优先级截断（角色身份 > 场景 > 道具），
    绝不丢弃角色身份锚点，并打日志便于排查「参考图被静默截断」。

    只做**保序截断**，不改变已分配槽位的职责语义；``build_storyboard_prompt`` 按位置
    重编号 ``<imageN>``，列表顺序即相关性顺序。
    """
    MAX_STORYBOARD_REFS = 8
    if len(refs) <= MAX_STORYBOARD_REFS:
        return refs
    _rank = {"主角色": 0, "次角色": 0, "场景": 1}   # 其余（道具等）→ 2，最先被截断
    ordered = sorted(refs, key=lambda r: _rank.get(r[0], 2))
    dropped = ordered[MAX_STORYBOARD_REFS:]
    app.logger.warning(
        "镜头 %s 参考图 %d 张 > 上限 %d，已按相关性丢弃 %d 张：%s",
        shot.get("shot_id"), len(refs), MAX_STORYBOARD_REFS, len(dropped),
        "、".join(f"{d[0]}:{str(d[1])[:24]}" for d in dropped))
    return ordered[:MAX_STORYBOARD_REFS]


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
_REF_CANVAS_CACHE: dict = {}


def _ref_canvas_target(size):
    """把目标画幅规整成 (W, H) 正整数元组；None / 非法 → None（调用方按「不处理」走）。"""
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def _fit_ref_to_canvas(im, size):
    """按 **cover** 把图缩放到恰好覆盖 size 画布并居中裁剪（内容充满、无条带）。

    ⚠️ 为什么用 cover 而不是 contain(letterbox)：2026-09-24 真图 A/B 实测
    （逆天系统 shot_02，同镜同 prompt）——
      · contain（内容缩放居中 + 自身模糊放大作底）→ 图像编辑型工作流**会模仿这个
        布局**：输出内容只占中间约 44%，上下是模型自绘的虚化带，画面利用率腰斩；
      · cover（放大到覆盖画布 + 居中裁剪）→ 内容充满整幅，构图正常（中景主体 +
        背景群像，与 camera 描述一致）。
    代价：宽幅参考图（场景资产内置 16:9）会被裁掉两侧。参考图的语义是「内容锚点」，
    中心区域通常已含代表性主体，环境细节由模型按 prompt 补全 —— 比留虚化带更划算。
    """
    from PIL import Image
    W, H = int(size[0]), int(size[1])
    if im.width == W and im.height == H:
        return im
    s = max(W / im.width, H / im.height)
    scaled = im.resize((max(W, int(round(im.width * s))),
                        max(H, int(round(im.height * s)))), Image.LANCZOS)
    left, top = (scaled.width - W) // 2, (scaled.height - H) // 2
    return scaled.crop((left, top, left + W, top + H))


def _unify_ref_canvas(refs: list, size, project_name: str = "") -> list:
    """把分镜参考图统一到目标画幅（cover 填充），返回新的 refs（结构不变）。

    只替换第 3 项（本地路径），kind / label 原样保留。失败降级：单张处理失败 → 该张
    沿用原图；缓存目录不可建 → 整批沿用原图；size 非法 → 原样返回。任何情况都不抛
    异常、不阻断分镜生成。
    """
    tgt = _ref_canvas_target(size)
    if not tgt or not refs:
        return refs
    out_dir = os.path.join(QC_DIR, str(project_name or "default"), "ref_canvas")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        app.logger.warning("参考图统一画幅：缓存目录不可建，本次沿用原图（%s）", e)
        return refs
    unified, changed = [], 0
    for item in refs:
        try:
            kind, label, path = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            unified.append(item)
            continue
        newp = path
        try:
            local = comfyui_client.resolve_local_path(path) or path
            if not local or not os.path.isfile(local):
                raise FileNotFoundError(f"参考图本地路径不可用: {path}")
            key = (os.path.normcase(os.path.abspath(local)),
                   int(os.stat(local).st_mtime_ns), tgt[0], tgt[1])
            cached = _REF_CANVAS_CACHE.get(key)
            if cached and os.path.isfile(cached):
                newp = cached
            else:
                import hashlib
                import tempfile
                from PIL import Image
                with Image.open(local) as _im:
                    _rgb = _im.convert("RGB")
                    if (_rgb.width, _rgb.height) == tgt:
                        unified.append(item)
                        continue
                    fixed = _fit_ref_to_canvas(_rgb, tgt)
                _stem = os.path.splitext(os.path.basename(local))[0]
                _h = hashlib.sha1(os.path.abspath(local).encode("utf-8")).hexdigest()[:8]
                _dst = os.path.join(out_dir, f"{_stem}_{_h}_{tgt[0]}x{tgt[1]}.png")
                # 仓库纪律：禁止「路径拼接固定 .tmp 后缀」这类**固定临时名**（并发会互相写坏，
                # 守卫 verify_asset_skip_existing A3.3 会红）。用 mkstemp 拿唯一名。
                _fd, _tmp = tempfile.mkstemp(dir=out_dir, prefix=".refcanvas_", suffix=".png")
                os.close(_fd)
                try:
                    fixed.save(_tmp, format="PNG")
                    os.replace(_tmp, _dst)
                except BaseException:
                    try:
                        os.unlink(_tmp)
                    except OSError:
                        pass
                    raise
                _REF_CANVAS_CACHE[key] = _dst
                newp = _dst
            if newp != path:
                changed += 1
        except Exception as e:  # noqa: BLE001 —— 单张失败不影响整镜
            app.logger.warning("参考图统一画幅失败，该张沿用原图（%s: %s）",
                               type(e).__name__, e)
            newp = path
        unified.append((kind, label, newp))
    app.logger.info("[分镜参考图画幅] 目标 %d×%d，%d/%d 张已统一（其余原尺寸或降级）",
                    tgt[0], tgt[1], changed, len(refs))
    return unified


# 注：本处原为 `_shot_seq(shot_id, fallback)`。已收敛为 app/shot_key.shot_seq 的
# 一行代理（见文件顶部），全项目唯一的镜号归一化实现见 app/shot_key.py。

#: 3D 站位基准图渲染的进程级串行锁（2026-10-06 分镜「滚动预取」改造引入）。
#: ⚠️ te_3d_render.render_blocking **不可并发重入**：其回传静态服务是进程级单例
#:    且只有一个 inbox 槽位（`srv.inbox = box`，后到者覆盖先到者 → 回传错投/超时），
#:    无头浏览器的 user-data-dir 也是共享的。分镜工作流预取线程与主循环都可能进入
#:    渲染，这里统一串行化；锁内调用自带「按计划哈希」的 PNG 缓存，同镜重复调用
#:    直接命中不重渲，串行化不损失流水线收益（渲染与 GPU 出图本就并行）。
_TE3D_RENDER_LOCK = threading.Lock()


# ===================== 九宫格「逐格写死」规划（2026-10-07，用户范本范式） =====================
# 两段式第一步：文本 LLM 按 storyboard_grid_plan 模板把一个镜头拆成 9 个关键帧分镜；
# 第二步（渲染）在 comfyui_client.build_shot_grid_keyframes_prompt 里走 storyboard_grid_main
# 模板产出中文逐格版九宫格提示词。规划拿不到 → 调用方传 panel_plans=None → 回落英文版路径。

_GRID_PLAN_TPL_FP = {"v": ""}


def _grid_plan_template_fingerprint() -> str:
    """规划模板的内容指纹（并入九宫格规划的缓存键）。

    为什么必须有：规划缓存是内容寻址的 —— 键只由 shot 的输入字段算出，不含模板版本。
    于是改了 storyboard_grid_plan.txt（2026-10-08 新增「场景锁死 / 方位锁死 / 反雷同」
    三条硬约束）之后，已经缓存过的镜头会继续命中旧规划 → 修复对它们完全无效，而且
    日志只显示「命中缓存」，看不出模板已变。把模板内容哈希并进键里，模板一改旧缓存
    自然失效（缓存文件保留，只是不再命中）。
    """
    if _GRID_PLAN_TPL_FP["v"]:
        return _GRID_PLAN_TPL_FP["v"]
    v = ""
    try:
        import hashlib
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "prompts", "storyboard_grid_plan.txt")
        with open(p, "r", encoding="utf-8") as f:
            v = hashlib.sha1(f.read().encode("utf-8")).hexdigest()[:8]
    except Exception as e:  # noqa: BLE001 拿不到指纹不阻断（退化为旧行为）
        app.logger.debug("[九宫格规划] 模板指纹计算失败（按空指纹处理）：%s", e)
    _GRID_PLAN_TPL_FP["v"] = v
    return v


def _grid_panel_plan(shot, chars, style, client, project_name: str = "",
                     shot_key: str = ""):
    """把一个镜头规划成九宫格的 9 个关键帧分镜（LLM 规划 → 内容寻址缓存 → 严格校验）。

    - 内容寻址缓存：``QC_DIR/<项目>/panel_plans/<shot_key>_<sha1[:10]>.json`` —— 键 =
      本镜全部规划输入（description / action / 首末帧 / motion / emotion / dialogue /
      duration / 角色清单 / 风格），同镜重复（断点续跑 / 质检重试 / 预取与内联）直接
      命中零 LLM 成本；缓存损坏按未命中处理（重规划并覆盖），读写失败只降级不阻断。
    - JSON 解析失败由 ``client.chat_json(retries=1)`` 的重试链内部「重试 1 次」处理；
      仍失败（或输出不满 9 格 / no 重复）→ 返回 ``None``，调用方
      （:func:`_storyboard_worker` 的 ``_prep_shot``）随即将 ``panel_plans=None`` 传入
      ``build_shot_grid_keyframes_prompt``，**回落现有英文版提示词**（fail-open，
      绝不因规划失败阻断分镜生成）。
    - LLM 不可用（未配置，``client is None``）→ 直接返回 None，同样走英文版回落。

    :param chars: 本镜出场角色名清单（``shot._char_ref_names`` 优先，兜底 characters_in_shot）
    :param client: 文本 LLM 客户端（worker 级 ``_optional_llm_client()`` 的产物；可为 None）
    :param project_name: 项目键（缓存目录定位）；空则落 ``default`` 目录
    :param shot_key: 镜头缓存键（如 ``shot_07``）
    :returns: 9 个 ``{"no","framing","tone","content"}`` 的列表；失败 → None
    """
    shot = shot if isinstance(shot, dict) else {}
    if client is None:
        return None

    def _s(key: str) -> str:
        return str(shot.get(key) or "").strip()

    # 台词统一成「角色：台词」行（dialogue 兼容 str / dict / list / None）
    dlg_lines = []
    for ln in dialogue_utils.normalize_lines(shot.get("dialogue")):
        dlg_lines.append(f"{ln['speaker']}：{ln['text']}" if ln.get("speaker")
                         else ln["text"])

    plan_inputs = {
        "description": _s("description"), "storyboard_prompt_zh": _s("storyboard_prompt_zh"),
        "action": _s("action"), "first_frame": _s("first_frame"),
        "last_frame": _s("last_frame"), "motion": _s("motion"),
        "emotion": _s("emotion"), "duration": _s("duration"),
        "characters": [str(c) for c in (chars or [])], "style": str(style or ""),
        "dialogue": dlg_lines,
        # 2026-10-08：模板指纹并入缓存键 —— 否则改了规划模板，已缓存镜头仍命中旧规划，
        # 修复静默失效（见 _grid_plan_template_fingerprint 的说明）。
        "template": _grid_plan_template_fingerprint(),
    }
    cache_path = ""
    try:
        import hashlib
        digest = hashlib.sha1(json.dumps(plan_inputs, ensure_ascii=False,
                                         sort_keys=True).encode("utf-8")).hexdigest()[:10]
        cache_dir = os.path.join(QC_DIR, project_name or "default", "panel_plans")
        cache_path = os.path.join(cache_dir, f"{shot_key or 'shot'}_{digest}.json")
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, list) and len(cached) == 9:
                app.logger.debug("[九宫格规划] 镜头 %s 命中内容寻址缓存（%s）",
                                 shot.get("shot_id"), os.path.basename(cache_path))
                return cached
            app.logger.warning("[九宫格规划] 缓存内容异常（%s），按未命中重新规划",
                               os.path.basename(cache_path))
    except Exception as e:  # noqa: BLE001 —— 缓存读失败只降级为「每次现算」
        app.logger.debug("[九宫格规划] 缓存读取失败（忽略，按未命中处理）：%s", e)
        cache_path = ""

    prompt = prompt_templates.render(
        "storyboard_grid_plan",
        shot_id=shot_key or str(shot.get("shot_id") or "本镜"),
        duration=_s("duration") or "5",
        style=str(style or ""),
        characters=("、".join(str(c) for c in (chars or []))
                    if (chars or []) else "（本镜无出场角色）"),
        description=_s("description") or _s("storyboard_prompt_zh") or "（未提供）",
        action=_s("action") or "（未提供）",
        first_frame=_s("first_frame") or "（未提供）",
        last_frame=_s("last_frame") or "（未提供）",
        motion=_s("motion") or "（未提供）",
        emotion=_s("emotion") or "（未提供）",
        dialogue="\n".join(dlg_lines) if dlg_lines else "（本镜无台词）",
    )
    if not prompt:
        return None
    try:
        # retries=1：chat_json 内部对「非法 JSON / 截断」自动追加修正提示重试 1 次；
        # 仍失败抛 LLMError → 捕获后返回 None（英文版回落）。
        raw = client.chat_json(prompt, temperature=0.4, max_tokens=4096, retries=1)
    except Exception as e:  # noqa: BLE001 —— LLMError / 网关异常一律 fail-open
        app.logger.warning("[九宫格规划] 镜头 %s 规划失败（回落英文版提示词）：%s: %s",
                           shot.get("shot_id"), type(e).__name__, e)
        return None
    plans = raw if isinstance(raw, list) else None
    if plans is None and isinstance(raw, dict):
        # 兜底：个别模型无视「只要数组」的指令，用对象包了一层数组
        plans = raw.get("panels") or raw.get("plan") or raw.get("data")
    clean = []
    for p in (plans or []):
        if not isinstance(p, dict):
            continue
        try:
            no = int(p.get("no") or 0)
        except (TypeError, ValueError):
            no = 0
        content = str(p.get("content") or "").strip()
        if no < 1 or no > 9 or not content:
            continue
        clean.append({"no": no,
                      "framing": str(p.get("framing") or "").strip() or "中景",
                      "tone": str(p.get("tone") or "").strip(),
                      "content": content})
    if len(clean) != 9 or len({p["no"] for p in clean}) != 9:
        app.logger.warning("[九宫格规划] 镜头 %s 规划不满 9 格（实得 %d），回落英文版提示词",
                           shot.get("shot_id"), len(clean))
        return None
    clean.sort(key=lambda p: p["no"])
    if cache_path:
        try:
            atomic_write_json(cache_path, clean)
        except Exception as e:  # noqa: BLE001 —— 缓存写失败不影响本次结果
            app.logger.debug("[九宫格规划] 缓存写入失败（忽略）：%s", e)
    app.logger.info("[九宫格规划] 镜头 %s 已产出 9 格分镜规划%s",
                    shot.get("shot_id"),
                    f"（缓存 {os.path.basename(cache_path)}）" if cache_path else "")
    return clean


#: 分镜「结构性缺陷」词表（2026-10-08）。
#: 这几类缺陷意味着**九宫格拆解本身失败** —— 同一格重复、中途换场景、左右手镜像 ——
#: 图不可用，**不参与 storyboard_soft_qc 软放行**，必须按硬阻断重跑。
#: 实测：shot_04 格3/4/6/9 四格近乎相同，质检已判「九宫格面板雷同」，却被软放行入库。
#: ⚠️ 与 qc_client.IMAGE_CRITICAL_KEYWORDS 里同名字段刻意保持一致（同一套判据）。
_SB_STRUCTURAL_DEFECT_KEYWORDS = (
    # ⚠️ 2026-10-09 按用户决策：「面板雷同 / 面板重复」已**移出**本词表（降级为 warning）。
    #    原因与影响见 qc_client.IMAGE_CRITICAL_KEYWORDS 处的同批注释：
    #    9 格对 7B 模型要求过高、首轮通过率仅 1/7，且它是唯一 critical，长期拖住链路。
    #    代价：**雷同的分镜图现在可能通过软放行入库**（这正是 2026-10-08 加它时要防的）。
    #    回退：把下面这行取消注释即可恢复「结构性缺陷 → 不参与软放行」。
    # "面板雷同", "九宫格面板雷同", "面板重复",
    "九宫格场景不一致", "格间换场景",
    "九宫格左右手镜像", "左右手互换", "左右手颠倒", "镜像翻转", "朝向翻转",
)


def _sb_structural_defect(gate) -> bool:
    """该镜的质检结论是否含**结构性缺陷**（软放行必须跳过它们）。

    判定文本 = reason + label + critical_issues（三者任一命中即算）。
    gate 非 dict / 缺字段都返回 False（退化为「可软放行」，不改变既有行为）。
    """
    if not isinstance(gate, dict):
        return False
    parts = [str(gate.get("reason") or ""), str(gate.get("label") or "")]
    for x in (gate.get("critical_issues") or []):
        parts.append(str(x))
    txt = " ".join(parts)
    return any(k in txt for k in _SB_STRUCTURAL_DEFECT_KEYWORDS)


def _storyboard_worker(task_id: str, project_name: str, shots: list,
                       char_idx: dict, item_idx: dict, scene_idx: dict,
                       episode_no=None, style: str = "", overwrite: bool = False):
    """后台分镜图生成任务：逐镜头生成并落盘 output/storyboards/<项目>[/epNN]/shot_XX.png

    style：用户与总控敲定的风格。用于 ① 补齐镜头 style 字段（老剧本无该字段时兜底）
    ② 解析画幅并覆写分镜图尺寸，保证分镜与成片同为竖屏 9:16。

    overwrite：是否「全量重做」。默认 False —— 已达标入库的 shot_XX.png 直接复用、
    只重跑缺失/被质检阻断的镜头（断点续跑语义）。这是本次修复的核心：
    修复前无论如何都从第 1 镜重跑到最后一镜，导致「补跑 21 个不达标镜」要重烧全部 83 镜。
    需要强制全部重画时（如换风格）显式传 overwrite=True。
    注意：质检不达标的镜头只写质检暂存区、不写正式目录，所以「正式目录里已有该图」
    ⟺ 「该镜上一轮已通过质检」——跳过它不会漏掉任何不达标镜。
    """
    out_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project_name), episode_no)
    os.makedirs(out_dir, exist_ok=True)
    # ---- 分镜图 URL 的集前缀（P1 修复，2026-09-25）----
    # ⚠️ 旧 bug：本 worker 落盘在 `_ep_dir(...)`（第 2 集起是 <项目>/epNN/），
    #    但 manifest 里的 url 却**硬编码**成 `/api/storyboards/file/<项目>/shot_NN.png`
    #    —— 少了 epNN 段 → 第 2 集起所有分镜图在界面上 404（文件明明存在）。
    # ⚠️ 参照物是**同文件里的 `_update_storyboard_manifest_shot`**（单镜重跑那条路）
    #    与视频 worker 的 `_vurl`：两者都按下标算前缀，所以单镜重跑后 URL 变对、
    #    整批重跑后又变错 —— 这正是「同一张图时好时坏」的根因。
    # 口径：第 1 集平铺（无前缀），第 2 集起 `epNN/`。
    # 用 `_ep_of_script` 而非裸 `int(episode_no)`：兼容历史调用传 None / "" 的情况，
    # 与 `_ep_dir` 的兜底（<=1 → 平铺）保持一致。
    _sb_ep = episode_no if episode_no not in (None, "") else 1
    _sb_sub = (f"ep{int(_sb_ep):02d}/" if int(_sb_ep) > 1 else "")
    _sb_url_base = f"/api/storyboards/file/{project_name}/{_sb_sub}"
    # 风格/画幅：整批分镜共用
    # G19：风格串未含画幅关键词时以默认画幅（2026-09-28 起为 16:9）为底，
    #      不再静默回落模板尺寸。megapixels：分镜图预算 0.8MP（画质提升，旧值 0.5）。
    _sb_style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO,
                                      megapixels=style_kit.storyboard_megapixels())
    _sb_style = _sb_style_res["style"]
    _sb_size = _sb_style_res["size"]
    # 分镜图「单镜九宫格（9 关键帧）」开关（2026-10-02 用户指定，见 config.SB_GRID_MODE）。
    # 开启时：① 提示词用 build_shot_grid_keyframes_prompt（一个镜头 = 3x3 九宫格 = 9 关键帧）；
    #         ② 景别自动裁剪禁用（九宫格不能按景别裁，否则破坏 9 格结构）；
    #         ③ 质检描述带「九宫格」口径（判官按整体一致性评，不按单帧景别判）。
    from config import SB_GRID_MODE
    _sb_grid_mode = bool(SB_GRID_MODE)
    if _sb_style:
        app.logger.info("[分镜风格] 风格=%s；画幅=%s；九宫格=%s", _sb_style,
                        _sb_style_res["label"] or "未指定（沿用模板）", _sb_grid_mode)
        shots = [dict(s, style=(s.get("style") or _sb_style)) for s in (shots or [])
                 if isinstance(s, dict)]
    manifest_shots = []
    # 旧 manifest：断点续跑时给「被跳过的镜头」回填上一轮的质检/提示词信息，避免信息丢失
    _prev_by_key = {}
    _prev_manifest = os.path.join(out_dir, "storyboard_manifest.json")
    # C4-1（2026-09-22 复验收口）：读取口径与 D-03/D-04 统一 —— 交给 read_json_strict
    # 自己负责三态（缺失→{}；活文件缺失但有 .bak→自动恢复；损坏→.bak 或 fail-loud），
    # 故不再用 os.path.isfile 预判。口径与 _update_storyboard_manifest_shot（本文件
    # L1990-1998 的 B-2 收口）一致；差异在于**本处是只读视图**：只给被跳过的镜头回填
    # 上一轮的质检/提示词信息、从不写回，所以损坏时**响亮降级**（error 日志 + 不回填），
    # 而不是 fail-loud 把整批分镜打挂。
    try:
        for _it in (read_json_strict(_prev_manifest, {}).get("shots") or []):
            if not isinstance(_it, dict):
                continue
            _sid = _it.get("shot_id")
            if _sid is not None:
                _prev_by_key[str(_sid)] = _it
            _sq = _shot_seq(_sid, 0)
            if _sq:
                _prev_by_key[f"shot_{_sq:02d}"] = _it
    except Exception as _e:  # noqa: BLE001
        app.logger.error("旧分镜清单 %s 不可读（%s: %s）：本次不回填被跳过镜头的信息，不影响生成",
                         _prev_manifest, type(_e).__name__, _e)
    app.logger.info("[分镜断点续跑] overwrite=%s；待处理 %d 镜（已存在者将跳过）",
                    overwrite, len(shots))
    # G13（P1）：质检配置 worker 级读一次，本批所有镜头共用（对齐资产 worker 2302）。
    # 旧代码逐镜 _qc_load_cfg()（每次 load JSON + Fernet 解密 secrets.enc），单集 56 镜 ≈
    # 上百次读盘；改为进循环前读一次，既省开销又避免「同批任务新旧配置混用」（审计 G13）。
    qc_cfg = _qc_load_cfg()
    qc_on = qc_client.image_qc_ready(qc_cfg)
    qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
    max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
    # ⭐⭐ 2026-10-07（用户指定「分镜重试轮数 2→1」）——分镜专用重试轮数。
    # 依据（实测）：单张图出图 = 40 步 × 0.9s ≈ 45 秒（分镜与资产**同速**），
    # 而分镜每镜最坏开销 = (1 + 重试轮数) × (一次 GPU 出图 + 一次质检)，
    # 且每集 20~30 镜**逐镜串行** —— 2 轮把最坏开销从 3 倍降到 2 倍。
    # 只作用于分镜：资产 / 视频链路仍走通用 max_retries（用户未要求改，重试性价比也不同）。
    # ⚠️ 以通用 max_retries 作**上限**（取 min）：用户把 max_retries 调成 0（全局不重试）时，
    #    分镜也随之 0 —— 尊重更严的总开关，不会被这个新键「偷偷放开」。
    if qc_on:
        try:
            _sb_retries_cap = int(qc_cfg.get("storyboard_max_retries", 1))
        except (TypeError, ValueError):
            _sb_retries_cap = 1
        _sb_retries_cap = max(0, _sb_retries_cap)
        if _sb_retries_cap != max_retries:
            app.logger.info("[分镜重试] 本批分镜重试轮数 %d（通用 max_retries=%d，"
                            "storyboard_max_retries=%d，取较小者）",
                            min(max_retries, _sb_retries_cap), max_retries, _sb_retries_cap)
        max_retries = min(max_retries, _sb_retries_cap)
    # best-of-N（2026-09-29，借 ViMax best_image_selector）：>1 时每镜固定生成 N 张候选，
    # 循环内只收集、**不立即入库**，循环后按质检分选**最佳**那张入库；==1 时完全走原
    # 「通过即停」逻辑（零回归）。质检未开（qc_on=False）无分可比 → 强制退回 1。
    best_of = max(1, int(qc_cfg.get("best_of", 1) or 1)) if qc_on else 1
    _rounds = best_of if best_of > 1 else (max_retries + 1)

    # ===================== 提示词流水线「滚动预取」（2026-10-06） =====================
    # 痛点：主循环逐镜串行「前置段（参考图分配 → 画幅统一 → 身份网格 → 3D 基准图 →
    # build prompt → 提示词预检）→ GPU 出图」，LLM 预检期间 GPU 完全空闲。
    # 改造：主循环消费完第 i 镜的前置段后，立刻派出第 i+1 / i+2 镜的**预取线程**
    # 提前做完它们的前置段（daemon 线程，领先 2 镜）；主循环走到 i+1 时若包已就绪
    # 直接取用（GPU 零等待），未就绪（LLM 慢于上一镜的 GPU 出图）则内联执行**同一段**
    # _prep_shot —— 内联与预取共用同一实现，绝无逻辑分叉。
    # ⚠️ 预取线程纪律（写死的边界，勿越）：
    #   · 只写 _pf_cache，**不碰** item / generation_state / live（主循环专属）；
    #   · 不向主循环抛异常 —— 一切异常装包（{"exc": ...}），主循环消费时按内联
    #     except 同语义落账；
    #   · 每镜至多一个线程（_pf_scheduled 幂等去重）；主循环若赶上「线程还在跑」，
    #     join 等它收尾而非自己重跑（同镜双跑会双打 LLM，且并发写同一张身份网格/
    #     基准图文件）。
    _pf_cache: dict = {}            # seq -> _prep_shot 返回的 pack（异常包见 _pf_run）
    _pf_threads: dict = {}          # seq -> 预取线程（内联兜底前 join，防同镜双跑）
    _pf_scheduled: set = set()      # 已派出的 seq（幂等）
    _pf_lock = threading.Lock()     # 保护上面三者
    # 待生成镜有序表（断点续跑已达标跳过的镜**不进表** —— 预取它们纯属白打 LLM）。
    # ⚠️ 跳过判据与主循环下方「断点续跑」分支**逐字同源**（not overwrite + 正式目录
    #    已有非空产物）。_pf_pending = [(seq, shot), ...] 与主循环同序；
    #    _pf_pending_idx[seq] = 下标（同 seq 重复镜号只登记第一处，其余走内联）。
    _pf_pending = []
    _pf_pending_idx = {}
    for _pi, _ps in enumerate(shots or []):
        if not isinstance(_ps, dict):
            continue
        _psid = _ps.get("shot_id", _pi + 1)
        _pseq = _shot_seq(_psid, _pi + 1)
        _pdst = os.path.join(out_dir, f"shot_{_pseq:02d}.png")
        if (not overwrite) and os.path.isfile(_pdst) and os.path.getsize(_pdst) > 0:
            continue
        if _pseq not in _pf_pending_idx:
            _pf_pending_idx[_pseq] = len(_pf_pending)
            _pf_pending.append((_pseq, _ps))
    _pf_shot_by_seq = {sq: s for sq, s in _pf_pending}

    # 九宫格逐格规划用的「文本分析模型」客户端（worker 级读一次，G13 同款口径）：
    # 未配置 → None → _grid_panel_plan 直接跳过 → 九宫格回落英文版提示词（fail-open）。
    try:
        _sb_llm = _optional_llm_client()
    except Exception as _sb_llm_e:  # noqa: BLE001 —— 客户端构造异常按未配置处理
        app.logger.warning("[九宫格规划] 文本 LLM 客户端获取失败（回落英文版提示词）：%s: %s",
                           type(_sb_llm_e).__name__, _sb_llm_e)
        _sb_llm = None

    def _prep_shot(seq_p, shot_p):
        """单镜前置段 —— 主循环内联与预取线程**共用的唯一实现**（2026-10-06 抽取）。

        步骤（原主循环内联段原样搬移，语义零变更）：
          参考图分配 → 统一画幅 → 空参考图分支（S6）→ 身份基准网格（九宫格模式）
          → 3D 站位基准图（进程锁串行）→ build prompt（九宫格 / 单帧两分支）→ 提示词预检。
        ⚠️ 本函数**不写 item、不碰 generation_state/live、不做 blocked 抛错**：
           item 字段落账、_purge_prompt_records + raise _PromptQCBlocked 都由主循环
           消费 pack 时执行（内联与预取两条路径同语义）。
        异常向上抛：内联路径由主循环既有 except 落账；预取路径由 _pf_run 捕获装包。
        返回 pack：
          {"seq", "refs", "labels", "prompt", "orig_prompt", "pf_item", "pgate_item",
           "blocked", "refs_empty", "no_reference", "ref_error", "error",
           "identity_grid", "blocking_ref"}
        """
        pack = {"seq": seq_p, "refs": [], "labels": [], "prompt": "", "orig_prompt": "",
                "pf_item": {}, "pgate_item": {}, "blocked": False, "refs_empty": False,
                "no_reference": False, "ref_error": "", "error": "",
                "identity_grid": "", "blocking_ref": ""}
        # ⭐ 2026-10-07 逐阶段计时（只加日志、不改行为）。
        # 动机：实测已排除「GPU 单图 45s」「3D 基准图 1.5s」「VLM 质检 5s」，
        # 剩下的分钟级开销必须能拆到「前置段」的哪个子步上，否则优化就是猜。
        # ⚠️ 计时不吞异常：_prep_shot 仍按原语义向上抛（预取线程装包 / 内联 except 落账）。
        _t0 = time.perf_counter()
        _tprev = _t0
        _ph = {}
        refs = _allocate_storyboard_refs(shot_p, char_idx, item_idx, scene_idx, project_name)
        # 统一参考图画幅：分镜工作流无尺寸节点，输出画幅继承第一张参考图，
        # 不统一会让同集画幅在 16:9 / 1:1 间跳变（详见 _unify_ref_canvas）。
        refs = _unify_ref_canvas(refs, _sb_size, project_name)
        _ph["参考图分配+归一"] = time.perf_counter() - _tprev
        _tprev = time.perf_counter()
        if not refs:
            # S6：区分"无参考图"与"角色匹配失败"（_no_reference）
            # ⚠️ 2026-10-05：本分支**只**在 refs 完全为空时进入。对「有场景/道具图但
            #    无角色」的镜头 refs 非空 → 走 else 分支、**不会**进入这里 —— 这正是
            #    shot#1（道具特写 + 场景图，characters_in_shot=[]）以前能「静默通过」
            #    并被人偶污染的原因。故这里只处理「(b) 声明了角色却匹配不到 且 无其他
            #    任何参考图」的情形；「(a) 本就无人物」的镜头由 _allocate_storyboard_refs
            #    决定**不**置 _no_reference（见那里的注释）。
            pack["refs_empty"] = True
            if shot_p.get("_no_reference"):
                pack["no_reference"] = True
                pack["ref_error"] = shot_p.get("_ref_error") or ""
                pack["error"] = f"角色匹配失败（禁止静默兜底）：{pack['ref_error']}"
                app.logger.warning(
                    f"[S6] 分镜 shot {shot_p.get('shot_id')} 角色匹配失败"
                    f"（characters_in_shot={shot_p.get('characters_in_shot')}），"
                    f"跳过该镜参考图分配：{pack['ref_error']}")
            else:
                pack["error"] = "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"
            return pack
        labels = [r[1] for r in refs]
        # ---- TE 3D 导演台：渲染 3D 站位/机位构图基准图（2026-09-30）----
        # 每镜先渲一张无面人偶站位图 → 作 <image1> 构图基准 → 提示词追加
        # COMPOSITION BASELINE 段。渲染失败静默降级为纯文字站位锚点（fail-open）。
        # ⭐ 2026-10-05（修复「无角色镜头被人偶污染」）：渲人偶基准图需**同时**
        #    满足两条（口径见 _shot_has_on_screen / _shot_has_char_ref）：
        #      ① 画面内确有出场角色（declared，characters_in_shot 非空）；
        #      ② 本镜确有**已命中资产的角色身份参考图**（chars_in 非空）。
        #    缺②时（声明了角色但资产缺失，_no_reference=True）refs 里只有场景图、
        #    没有角色图可覆盖人偶 → COMPOSITION BASELINE 要求「用其他角色图完全覆盖
        #    人偶」无法满足 → 模型照样照抄人偶，症状与本次缺陷一致。故一并挡住。
        #    注意：② 是 ① 的**子集**，只更保守（不插 ref / 少调一次渲染），不会分叉。
        #    ⚠️ `has_characters` 提示词参数仍只取 ①（declared）—— 见下方与 L2817。
        from config import ENABLE_3D_BLOCKING_IMAGE
        _has_on_screen = _shot_has_on_screen(shot_p)
        # ---- 角色身份基准网格（2026-10-06，用户拍板「先拼九宫格再送工作流」）----
        # 九宫格 9 面板一次扩散生成，格间服装/发型漂移纯文字约束不住；
        # 先把角色基准图拼成与输出同构的 3×3 参考网格作靠前参考图，
        # 配合提示词 IDENTITY BASELINE GRID / PANEL-WISE IDENTITY BINDING
        # 协议逐格绑定身份。拼接失败降级为常规参考图（fail-open）。
        # ⚠️ 2026-10-07（用户反馈「9 格还是有重复」）：触发条件收紧为**去重后
        #    ≥3 张不同图** —— 只有 1~2 张图（单角色单视图）时，9 格同图拷贝会被
        #    扩散模型当成「输出范例」而非「输入约束」，直接诱导九格趋同；此时
        #    个别参考图本身已足够锚定身份，不再拼 9 格同图网格。多角色/多视图
        #    时网格各格互异，逐格绑定仍有意义，保持原行为。
        _identity_grid_path = ""
        if _sb_grid_mode and _has_on_screen:
            _ig_paths = [r[2] for r in refs if r[0] in ("主角色", "次角色")]
            if len(set(_ig_paths)) >= 3:
                try:
                    _identity_grid_path = _build_identity_ref_grid(
                        _ig_paths,
                        os.path.join(QC_DIR, project_name, "identity_grid",
                                     f"shot_{seq_p:02d}_identity_grid.png"),
                        target_size=_sb_size)
                except Exception as _ig_e:  # noqa: BLE001
                    app.logger.warning(
                        "[身份基准网格] shot=%s 拼接失败（降级为常规参考图）：%s",
                        shot_p.get("shot_id"), _ig_e)
                    _identity_grid_path = ""
                if _identity_grid_path:
                    # 网格占一个参考图槽位：尾部参考图让位（工作流上限 9 槽；
                    # 下方 3D 构图基准图还要插 <image1>）。尾部=相关性最低
                    #（_cap_storyboard_refs 已按相关性排序）。
                    refs = refs[:7]
                    refs.insert(0, (
                        _identity_grid_path,
                        f"参考图1（<image1>）【{_IDENTITY_GRID_REF_MARK}】"
                        f"是本镜出场角色的身份基准网格（3×3 九格角色基准外观）："
                        f"输出九个面板逐格与它保持面部身份、发型与服装一致",
                        _identity_grid_path))
                    labels = [r[1] for r in refs]
        # ⭐ 2026-10-09（用户反馈）：**跨镜视觉衔接**。
        #    问题：每镜的九宫格都是独立生成的，下一镜看不到上一镜长什么样 ——
        #    服装/发型/光位/道具（用户实测：旧哨子被画反）逐镜漂移，
        #    成片里「上下两个分镜图没有关联」。
        #    做法：把**上一镜已入库的分镜图**作为本镜的一张参考图（复用
        #    _qc_prev_shot_ref 的同场景 + 已通过质检语义），让出图模型看到
        #    上一镜的角色状态、光位与道具形态，从图片层先把连续性接上。
        #    取不到（首镜 / 跨场景 / 上一镜未入库）→ 不加，行为与改动前一致。
        _prev_sb_ref = ""
        try:
            _prev_sb_ref = _qc_prev_shot_ref(shots, shots.index(shot_p), out_dir)
        except (ValueError, TypeError):
            _prev_sb_ref = ""
        # 槽位纪律：工作流上限 9 张（builder MAX_REFERENCE_IMAGES=9），
        # 本插入点在 _cap_storyboard_refs 之后，故必须自己守住上限；
        # 满槽时宁可放弃衔接，也不能挤掉身份基准/场景等更强约束的参考图。
        if _prev_sb_ref and os.path.isfile(_prev_sb_ref) and len(refs) < 9:
            refs.append((_prev_sb_ref, (
                f"参考图【上一镜衔接】是**同一场景中上一镜已通过质检的分镜图**（"
                f"{os.path.basename(_prev_sb_ref)}）：本镜九个面板必须与它在"
                f"**角色服装/发型、道具形态与朝向、光源方向、场景陈设、色调**上自然接续，"
                f"不得出现前后矛盾（如同一角色换发型、同一道具反向、光源换侧）；"
                f"但构图与景别按本镜要求走，不要照抄上一镜。"), _prev_sb_ref))
            app.logger.info("[分镜跨镜衔接] shot=%s 引入上一镜参考：%s",
                            shot_p.get("shot_id"), os.path.basename(_prev_sb_ref))
        _ph["身份基准网格"] = time.perf_counter() - _tprev
        _tprev = time.perf_counter()
        _blocking_ref_path = ""
        if ENABLE_3D_BLOCKING_IMAGE and _has_on_screen and _shot_has_char_ref(shot_p):
            try:
                import te_3d_render
                if te_3d_render.available():
                    _blk_out_dir = os.path.join(QC_DIR, project_name, "te3d_blocking")
                    # ⚠️ 并发纪律（2026-10-06 预取改造）：render_blocking 不可并发重入
                    #    （回传服务是进程级单例且只有一个 inbox 槽位、浏览器 profile
                    #    目录共享 —— 并发渲染会互相抢回传），预取线程与主循环都可能
                    #    进来 → 用 _TE3D_RENDER_LOCK 串行化。
                    #    不做「预取跳过 3D、主循环补渲」：基准图渲没渲成决定 <image1>
                    #    槽位与 COMPOSITION BASELINE 段，必须先于 build prompt 与预检
                    #    确定 —— 跳过会让预取出的提示词整体作废、预检白跑。锁内自带
                    #    计划哈希 PNG 缓存，同镜重复调用零成本。
                    with _TE3D_RENDER_LOCK:
                        _blocking_ref_path = te_3d_render.render_blocking(
                            shot_p, _blk_out_dir,
                            aspect=":".join(map(str, _sb_style_res.get("ratio") or ("16", "9"))),
                            width=_sb_size[0],
                            # ⭐ 按目标像素对齐（2026-10-02）：裸 aspect 字符串在本仓
                            #    有双语义（style_kit 的 (16,9)＝横屏，而 te_3d_director
                            #    收 "16:9" 会算成 684 高）→ 基准图 1216×684 ≠ 分镜 1216×672，
                            #    作 <image1> 定画布会把错的画幅带进分镜图。
                            #    传 _sb_size 保证逐像素一致（G2 存量失败的真因）。
                            target_size=_sb_size,
                        ) or ""
                if _blocking_ref_path:
                    app.logger.info("[3D导演台] shot=%s 站位基准图已渲染：%s",
                                     shot_p.get("shot_id"), os.path.basename(_blocking_ref_path))
                    # 插入为第一张参考图（<image1>），后续参考图序号后移
                    # ⭐ 标签必须引用常量 _BLOCKING_REF_MARK（= comfyui_client.BLOCKING_REF_MARK，
                    #    值 "3D导演台构图基准"），**禁止再写裸字面量**：comfyui_client.build_storyboard_prompt
                    #    靠 `BLOCKING_REF_MARK in label` 识别基准图并生成 COMPOSITION BASELINE 段，
                    #    字面量与常量一旦漂移（如旧 "3D构图基准" ≠ "3D导演台构图基准"）该段静默缺失、
                    #    且人偶被误当 identity anchor（2026-10-05 定位的既有缺陷）。
                    refs.insert(0, (_blocking_ref_path, _BLOCKING_REF_MARK, _blocking_ref_path))
                    # ⭐ 基准图在 _unify_ref_canvas **之后**插入，躲过了归一
                    #    （2026-10-02）：它是 <image1> 画布定义者，尺寸必须与
                    #    _sb_size 逐像素一致，否则整个分镜画幅被它带偏。
                    #    这里显式再归一一次，把「插入顺序」这个隐患彻底封死。
                    refs = _unify_ref_canvas(refs, _sb_size, project_name)
                    labels = [r[1] for r in refs]
            except Exception as _3d_e:  # noqa: BLE001
                app.logger.warning("[3D导演台] shot=%s 渲染失败（降级为纯文字站位）：%s",
                                   shot_p.get("shot_id"), _3d_e)
                _blocking_ref_path = ""
        _ph["3D站位基准图"] = time.perf_counter() - _tprev
        _tprev = time.perf_counter()
        if _sb_grid_mode:
            # ---------- 九宫格逐格规划（2026-10-07，两段式第一步）----------
            # 先用文本 LLM 把本镜拆成 9 个关键帧分镜（内容寻址缓存，同镜重复零成本）；
            # 规划成功 → build_shot_grid_keyframes_prompt 走「逐格写死」中文模板
            # （storyboard_grid_main，用户范本结构）；规划失败 / LLM 未配置（_sb_llm=None）
            # → panel_plans=None → 该函数内部**原样走英文版全路径**
            #（fail-open：默认行为只有拿到规划才变，原路径一字不动）。
            _plan_chars = shot_p.get("_char_ref_names")
            if not isinstance(_plan_chars, (list, tuple)):
                _plan_chars = shot_p.get("characters_in_shot")
            _panel_plans = _grid_panel_plan(
                shot_p, [str(c) for c in (_plan_chars or [])],
                (shot_p.get("style") or _sb_style), _sb_llm,
                project_name=project_name, shot_key=f"shot_{seq_p:02d}")
            prompt = comfyui_client.build_shot_grid_keyframes_prompt(
                shot_p, labels, style=(shot_p.get("style") or _sb_style),
                has_characters=_has_on_screen, panel_plans=_panel_plans)
        else:
            prompt = comfyui_client.build_storyboard_prompt(
                shot_p, labels,
                has_blocking_image=bool(_blocking_ref_path),
                has_characters=_has_on_screen)
        _ph["LLM九宫格规划+build提示词"] = time.perf_counter() - _tprev
        _tprev = time.perf_counter()
        orig_prompt = prompt      # 教训库的稳定键：改写后的提示词不参与指纹

        # ---------- 提示词预检（生成前质检）：先判 → 确定性自愈 → 再出图 ----------
        # ⚠️ 放在 orig_prompt 之后：教训库指纹仍以构建器输出为准（自愈不参与指纹，
        #    否则同一镜头在自愈前后会生成两条互不相认的教训）。
        prompt, _pf_item, _pgate_item = _prompt_preflight(
            "storyboard", prompt, ctx=shot_p,
            style=(shot_p.get("style") or _sb_style), ref_count=len(refs),
            project_name=project_name, cfg=qc_cfg)   # G13：复用 worker 级配置
        _ph["提示词预检"] = time.perf_counter() - _tprev
        _ph_total = time.perf_counter() - _t0
        # ⭐ 逐阶段计时（2026-10-07）：定位「分镜为什么慢」。
        #    只观测不改行为；主循环会把 phase_secs 原样带回并落进 stage_log，
        #    用于区分「前置段（LLM/3D/归一）」与「GPU 出图 + 质检 + 重试」各占多少。
        #    预取线程与内联同语义，故两条路径都能拿到同一份 phase_secs。
        app.logger.info(
            "[分镜计时] shot=%s 前置段总 %.1fs 明细 %s",
            shot_p.get("shot_id"),
            _ph_total,
            " ".join(f"{k}={v:.1f}s" for k, v in _ph.items()))
        pack.update({"refs": refs, "labels": labels, "prompt": prompt,
                     "orig_prompt": orig_prompt, "pf_item": _pf_item,
                     "pgate_item": _pgate_item,
                     "blocked": not bool(_pgate_item.get("accept")),
                     "identity_grid": _identity_grid_path,
                     "blocking_ref": _blocking_ref_path,
                     "phase_secs": dict(_ph),
                     "phase_secs_total": _ph_total})
        return pack

    def _pf_run(seq_p):
        """预取线程主体：与主循环完全同一段 _prep_shot；一切异常装包，绝不 crash 主流程。"""
        try:
            pack = _prep_shot(seq_p, _pf_shot_by_seq[seq_p])
        except Exception as _pf_e:  # noqa: BLE001
            import traceback as _tb
            app.logger.warning("[分镜预取] seq=%s 前置段异常（装包待主循环按内联同语义落账）：%s\n%s",
                               seq_p, _pf_e, _tb.format_exc())
            pack = {"seq": seq_p, "exc": str(_pf_e)}
        with _pf_lock:
            _pf_cache[seq_p] = pack

    def _pf_schedule(seq_p):
        """幂等派出 seq_p 的预取线程；已在跑 / 已跑过返回 False。"""
        with _pf_lock:
            if seq_p in _pf_scheduled:
                return False
            _pf_scheduled.add(seq_p)
        _t = threading.Thread(target=_pf_run, args=(seq_p,), daemon=True,
                              name=f"sb-prefetch-shot-{seq_p}")
        with _pf_lock:
            _pf_threads[seq_p] = _t
        _t.start()
        return True

    def _pf_fill(cur_p):
        """滚动预取调度：主循环消费完 _pf_pending 下标 cur_p 后，派出后面最多 2 个未预取镜。"""
        for _j in range(cur_p + 1, min(cur_p + 3, len(_pf_pending))):
            _pf_schedule(_pf_pending[_j][0])

    def _pf_take(seq_p):
        """取走 seq_p 的预取包（有则弹出）；没有返回 None（主循环转内联）。"""
        with _pf_lock:
            return _pf_cache.pop(seq_p, None)

    try:
        for i, shot in enumerate(shots):
            shot_id = shot.get("shot_id", i + 1)
            seq = _shot_seq(shot_id, i + 1)
            dst = os.path.join(out_dir, f"shot_{seq:02d}.png")
            # ---------- 断点续跑：已达标入库的镜头直接复用，只重跑缺失/不达标的 ----------
            # 正式目录里已有非空 shot_XX.png ⟺ 上一轮该镜已通过质检（不达标的只落暂存区）。
            # 因此这里跳过是安全的，且能把「补跑 N 个不达标镜」的代价从「全量 M 镜」降回 N 镜。
            if not overwrite and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                prev = _prev_by_key.get(str(shot_id)) or _prev_by_key.get(f"shot_{seq:02d}") or {}
                item = dict(prev) if prev else {}
                item.update({"shot_id": shot_id, "success": True, "skipped": True,
                             "file": dst, "error": "",
                             "url": f"{_sb_url_base}shot_{seq:02d}.png"})
                item.setdefault("qc", {"enabled": False, "status": "skipped",
                                       "label": "沿用已达标图", "attempts": 0, "regenerated": 0})
                item.pop("qc_blocked", None)
                manifest_shots.append(item)
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / max(len(shots), 1) * 100),
                        "current_shot": shot_id,
                    })
                    generation_state[task_id]["results"].append(item)
                continue
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int(i / max(len(shots), 1) * 100),
                    "current_shot": shot_id,
                    "phase": "分镜图生成",
                })
            item = {"shot_id": shot_id, "success": False, "refs": {}, "prompt": "",
                    # 单镜九宫格标记（2026-10-06）：随 manifest 落盘，画布接口把它投影成
                    # storyboard.grid，前端灯箱据此叠加 1-9 编号覆盖层（编号不再让模型画）。
                    "grid_layout": bool(_sb_grid_mode)}
            # ⭐⭐ 2026-10-08 修复（严重）：scratch_png 必须**逐镜重置**。
            #    它原本只在「生成成功」后赋值，生成失败/预检阻断时**保留上一镜的路径**；
            #    下方的「质检软放行」若直接读它，会把**上一镜的图**复制成本镜产物
            #    （实测：镜16/20/21/23/28/30/37/41 八镜被写成镜5 的同一张图，duplicate
            #     hash 35a8d1abd4）。这里逐镜清空 + 软放行处再做文件名归属校验，双保险。
            scratch_png = ""
            _shot_t0 = time.perf_counter()   # ⭐ 逐镜总墙钟（含前置段 + GPU + 质检 + 重试）
            try:
                # ---------- 前置段：预取包优先，未就绪则内联（同一段 _prep_shot） ----------
                # 滚动预取（2026-10-06）：上一镜消费后已派出本镜的预取线程；这里优先
                # 取现成包，LLM 预检不再阻塞 GPU。未就绪 / 线程失败 → 内联执行同一段
                # _prep_shot（两条路径共用同一实现，绝无逻辑分叉）。
                pack = _pf_take(seq)
                if pack is None:
                    with _pf_lock:
                        _pf_t = _pf_threads.get(seq)
                    if _pf_t is not None and _pf_t.is_alive():
                        # 预取线程还在跑同一段逻辑：等它收尾（与内联重跑等价但零重复 ——
                        # 同镜双跑会双打 LLM，且并发写同一张身份网格/基准图文件）。
                        # 线程内所有阻塞调用自带超时；daemon 线程不阻塞进程退出。
                        _pf_t.join()
                        pack = _pf_take(seq)
                if pack is None:
                    pack = _prep_shot(seq, shot)   # 预取未派出（重复镜号等）→ 内联
                _p_idx = _pf_pending_idx.get(seq)
                if _p_idx is not None:
                    _pf_fill(_p_idx)   # 消费完本镜 → 滚动派出后面 ≤2 镜的预取
                if pack.get("exc"):
                    # 预取线程装包的异常 → 与内联 except Exception 分支**同语义**落账
                    #（fail-closed：绝不留「success=True 但无产物」的僵尸条目）。
                    app.logger.error(f"镜头 {shot_id} 分镜图前置段失败（预取包）: {pack['exc']}")
                    item["success"] = False
                    item["file"] = ""
                    item.pop("url", None)
                    item["error"] = pack["exc"]
                elif pack["refs_empty"]:
                    # S6：区分"无参考图"与"角色匹配失败"（_no_reference）—— 语义与原
                    # 内联分支一致：只处理「声明了角色却匹配不到 且 无其他任何参考图」。
                    if pack["no_reference"]:
                        item["no_reference"] = True
                        item["ref_error"] = pack["ref_error"]
                        item["error"] = pack["error"]
                    else:
                        item["error"] = pack["error"]
                else:
                    refs = pack["refs"]
                    if pack["identity_grid"]:
                        item["_identity_grid"] = pack["identity_grid"]
                    if pack["blocking_ref"]:
                        item["_blocking_ref"] = pack["blocking_ref"]
                    prompt = pack["prompt"]
                    item["prompt"] = prompt
                    item["phase_secs"] = pack.get("phase_secs")   # ⭐ 前置段逐阶段耗时（观测用）
                    item["phase_secs_total"] = pack.get("phase_secs_total")
                    orig_prompt = pack["orig_prompt"]   # 教训库的稳定键：改写后的提示词不参与指纹
                    item["refs"] = {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs}
                    item["prompt_qc"] = pack["pf_item"].get("verdict")
                    item["prompt_qc_repairs"] = pack["pf_item"].get("repairs") or []
                    if pack["blocked"]:
                        item["prompt_qc_blocked"] = True
                        # ★ 用户需求：不合格提示词不留本地（P12）—— 该镜不生成，
                        # 把上一轮遗留的 `output/qc/<项目>/prompt_<shot>.json` 移回收站。
                        try:
                            _purge_prompt_records(
                                project_name, shot_id,
                                reason=f"提示词预检未通过（{pack['pgate_item'].get('label')}）")
                        except Exception as _pe:  # noqa: BLE001
                            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
                        raise _PromptQCBlocked(
                            f"提示词预检未通过（{pack['pgate_item'].get('label')}）："
                            f"{pack['pgate_item'].get('reason')}" +
                            (f"；建议：{pack['pf_item'].get('rebuild_hint')}"
                             if pack["pf_item"].get("rebuild_hint") else ""))
                    # ★ G10：捕获自愈后提示词作为重试基准。
                    #   旧 bug：重试召回教训库以 orig_prompt（自愈前）为键，导致重试
                    #   回落到未自愈提示词，自愈修复被静默丢弃。
                    self_healed_prompt = prompt
                    # G10b（2026-09-30）：第 1 次尝试的提示词也实时暴露（live.attempt=0）
                    with lock:
                        generation_state[task_id]["live"] = {
                            "shot": shot_id, "attempt": 0, "prompt": prompt,
                            "phase": "generating", "done": False}


                    # ---------- 图片 AI 质检（不达标自动重生成） ----------
                    # G13：qc_cfg/qc_on/qc_declared/max_retries 已在 worker 级读一次（见上方），
                    # 本批所有镜头共用，不再逐镜 _qc_load_cfg()。
                    attempts = []
                    _best_candidates = []   # best-of-N：本镜候选 [(score, png, rec, gate)]
                    # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None 走模板默认常量），
                    # 使 manifest.shots[*].qc.history[0].seed 不再为 null，产物可复现、可追溯。
                    seed = random.randint(1, 2 ** 31 - 1)
                    dst = os.path.join(out_dir, f"shot_{seq:02d}.png")

                    for attempt in range(_rounds):
                        if attempt > 0:
                            seed = random.randint(1, 2 ** 31 - 1)
                            # G10：基准改为 self_healed_prompt（自愈后提示词）。
                            # 旧 bug：基准是 orig_prompt（自愈前），重试会回落到未自愈提示词，
                            # 导致首次自愈对后续重试不再生效。
                            # 教训库召回也改用 self_healed_prompt 作键：
                            # 自愈改变了提示词 → 指纹也变了，用旧指纹的教训与自愈后提示词不匹配。
                            _retry_base = self_healed_prompt
                            # ① 优先：针对上一轮这张图的缺陷，LLM 即时改写（精准）
                            # ② 回落：历史教训库召回；再回落：仅换种子
                            prompt = _retry_base
                            _opt_prompt = None
                            if attempts:
                                _opt_prompt = _optimize_prompt_from_qc(
                                    "storyboard", _retry_base, attempts[-1],
                                    style=_qc_style_of(project_name))
                            if _opt_prompt:
                                # ★ 双保险（2026-10-06）：即时优化结果**再走一次**确定性
                                #   结构闸门。不合格就当作「没有优化」回落历史召回/换种子 ——
                                #   重试路径此前不复检提示词，那正是本次事故的入口。
                                _pf_ok, _pf_miss = _storyboard_prompt_structurally_ok(_opt_prompt, _sb_grid_mode)
                                if not _pf_ok:
                                    app.logger.warning(
                                        "镜头 %s 即时优化提示词缺协议段（%s），弃用并回落原逻辑",
                                        shot_id, "、".join(_pf_miss))
                                    _opt_prompt = None
                            if _opt_prompt:
                                prompt = _opt_prompt
                                item["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
                                                shot_id, attempt + 1)
                            else:
                                try:
                                    hints = prompt_memory.suggest(
                                        kind="storyboard",
                                        prompt=_retry_base,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR
                                    )
                                    # 2026-10-09：max_hints 从默认 3 降到 1。
                                    # 实测注入 3 条会把提示词从 1812 字撑到 2115 字，
                                    # 而教训文本是「上次失败的具体缺陷描述」—— 越注越长，与
                                    # 官方 Simple and Clear 相悖。只保留最相关的一条。
                                    learned = prompt_memory.learned_prompt(
                                        kind="storyboard",
                                        prompt=_retry_base,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR,
                                        max_hints=1,
                                        style=_qc_style_of(project_name),
                                    )
                                    if learned and learned != _retry_base:
                                        prompt = learned
                                        item["prompt"] = prompt
                                        item["prompt_hints"] = hints[:3]
                                        app.logger.info("镜头 %s 第 %d 次重试，按质检教训改写提示词：%s",
                                                        shot_id, attempt + 1, hints[:2])
                                    else:
                                        prompt = _retry_base
                                        item["prompt"] = prompt
                                        app.logger.info("镜头 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                        shot_id, attempt + 1)
                                except Exception as mem_err:
                                    app.logger.warning(f"读取记忆模块失败: {mem_err}")

                            with lock:
                                generation_state[task_id]["phase"] = \
                                    f"质检不达标，修改提示词后重新生成（第 {attempt}/{max_retries} 次）"
                                generation_state[task_id]["qc_phase"] = "regenerating"
                                # G10b（2026-09-30）：改写后的提示词**实时**暴露给前端轮询。
                                # 前端每 3s 拉 /api/generation/status/<task_id> 的 live 字段
                                generation_state[task_id]["live"] = {
                                    "shot": shot_id, "attempt": attempt + 1,
                                    "prompt": prompt, "phase": "regenerating", "done": False}
                        result = comfyui_client.generate_storyboard(
                            prompt_zh=prompt,
                            ref_images=[r[2] for r in refs],
                            filename_prefix=f"comic_drama_sb/{project_name}_shot_{seq:02d}",
                            seed=seed,
                            size=_sb_size,
                        )
                        if not result["files"]:
                            # B 自愈（2026-10-08）：ComfyUI 离线时自动重启，重启成功则本镜头下一轮重跑
                            if not comfyui_client.comfy_online(timeout=3):
                                if _sb_heal_comfyui(task_id, project_name):
                                    item["error"] = "ComfyUI 离线已自愈重启，本镜头下一轮重跑"
                                    continue
                            item["error"] = "ComfyUI 未返回分镜图（可能节点缺失或超时）"
                            break
                        # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该图）
                        sb_scratch_dir = os.path.join(QC_DIR, project_name, "storyboard_scratch")
                        os.makedirs(sb_scratch_dir, exist_ok=True)
                        _qc_prune_attempts(sb_scratch_dir)   # G8③：清本镜历史过期 try（共享目录按镜头前缀保留最近4）
                        scratch_png = os.path.join(sb_scratch_dir,
                                                   f"shot_{seq:02d}_try{attempt + 1}.png")
                        # G8②：消费 ComfyUI output 源（与视频链路的 move 语义对齐），
                        # 不再 copy2 导致 output/comic_drama_sb/ 只增不减。
                        # 每轮 attempt 都会重新 generate 出新文件，move 走旧源无副作用。
                        _move_with_retry(result["files"][0], scratch_png)
                        item.update({
                            "success": True,
                            "file": dst,
                            "url": f"{_sb_url_base}shot_{seq:02d}.png",
                            "ref_count": len(refs),
                        })
                        item.pop("error", None)
                        if not qc_on:
                            # P0-1 fail-closed：qc_declared=True 但接口未就绪（qc_on=False）→ 阻断，
                            # **不写正式目录**。旧实现此处 `copy2(scratch_png, dst)` 属 fail-open，
                            # 会把未质检产物当成品交付并破坏「正式目录有产物 ⟺ 已过质检」不变量。
                            # 资产链路早已 fail-closed（见资产生成处的同型分支），此处对齐口径。
                            if qc_declared:
                                item["error"] = ("分镜图质检阻断（质检接口未就绪）：已开启图片质检，"
                                                 "但 base_url / api_key / model 不可用；"
                                                 "未质检产物不写入正式目录（暂存图见质检历史）")
                                app.logger.warning(
                                    "[分镜质检] qc_declared=True 但 qc_on=False → fail-closed 阻断入库"
                                    "（不写正式目录）：project=%s shot=%s", project_name, shot_id)
                                break
                            shutil.copy2(scratch_png, dst)   # 质检本就未开启：按原行为直接入库
                            break
                        with lock:
                            generation_state[task_id]["phase"] = f"图片质检中（镜头 {shot_id} · 第 {attempt + 1} 次）"
                            generation_state[task_id]["qc_phase"] = "checking"
                            # G10b：质检阶段同步暴露当前提示词（与生成阶段同一份）
                            generation_state[task_id]["live"] = {
                                "shot": shot_id, "attempt": attempt + 1,
                                "prompt": prompt, "phase": "checking", "done": False}
                        # ---- 景别后处理（C 方案，2026-09-30）：送检前若主体占比超出目标景别，自动裁剪 ----
                        # ⚠️ 九宫格模式（2026-10-02）禁用：一张图是 3x3 九个关键帧，
                        #    按景别裁剪会切掉 8 格、只剩一格，破坏九宫格结构。
                        _shot_type = (shot.get("shot_type") or shot.get("camera") or "").strip()
                        if _shot_type and not _sb_grid_mode and os.path.isfile(scratch_png):
                            try:
                                _crop_res = qc_client.auto_crop_framing(scratch_png, _shot_type)
                                if _crop_res.get("cropped") and os.path.isfile(_crop_res["out_path"]):
                                    app.logger.info("[分镜质检] 景别自动裁剪 shot=%s: %s", shot_id, _crop_res["reason"])
                                    scratch_png = _crop_res["out_path"]
                            except Exception as _crop_e:  # noqa: BLE001
                                app.logger.warning("[分镜质检] 景别自动裁剪失败（不阻断）shot=%s: %s", shot_id, _crop_e)
                        # 九宫格模式（2026-10-02）：质检描述加「九宫格」口径，
                        # 判官按 9 个关键帧的整体一致性评，不按单帧景别判。
                        _qc_desc = _qc_shot_desc(shot)
                        if _sb_grid_mode:
                            _qc_desc = ("本图为一张 3x3 九宫格故事板：同一镜头的 9 个关键帧，"
                                        "随时间从左到右、从上到下推进；请评 9 格整体的角色/场景/"
                                        "光线/色调一致性、动作推进的连贯性与画面质量，"
                                        "不要按单帧的景别去判（景别可能随运镜在格间渐次变化）。"
                                        "⚠️ 面板重复判据：若 ≥4/9 格的动作与构图基本相同"
                                        "（仅微小位移），判为面板雷同，必须不合格并在 issues "
                                        "写明『九宫格面板雷同』。"
                                        + _qc_desc)
                        verdict = qc_client.check_image(
                            scratch_png, _qc_desc, qc_cfg,
                            style=(shot.get("style") or _sb_style),
                            ref_images=_qc_ref_images(
                                shot, char_idx, item_idx, scene_idx, refs),
                            blocking_ref=(item.get("_blocking_ref") or ""),
                        # 预演图不再送检（见 qc_client.check_image 的说明），改送确定性文字规格；
                        # 只有本镜确实该出基准图时才带（否则空串，等同于不加这段口径）。
                        blocking_spec=(_blocking_spec_text(shot)
                                        if (item.get("_blocking_ref") or "") else ""),
                            # ---- 跨镜连续性（P1，2026-09-25）----
                            # 只在**同场景**时给上一镜信息：跨场景切换本就该换背景换光，
                            # 拿上一镜去比会判出一堆假缺陷（与 keyframe.same_scene 同判据）。
                            prev_shot_desc=_qc_prev_shot_desc(shots, i),
                            prev_shot_ref=_qc_prev_shot_ref(shots, i, out_dir))
                        rec = _qc_record_verdict(project_name, "image", shot_id, "图片质检",
                                                 attempt + 1, seed, scratch_png, verdict,
                                                 style=(shot.get("style") or _sb_style))
                        attempts.append(rec)
                        gate = _qc_gate(verdict)
                        item["qc_gate"] = gate
                        item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                        if gate["accept"]:
                            if best_of > 1:
                                # best-of-N：本轮达标也**不立即入库** —— 先收集候选，
                                # 循环后按质检分选最佳那张入库（见循环末尾收口块）。
                                _best_candidates.append(
                                    (verdict.get("score"), scratch_png, rec, gate))
                                continue
                            shutil.copy2(scratch_png, dst)   # 质检达标 → 写入正式交付目录
                            item["file"] = dst
                            # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论）
                            _write_artifact_meta(
                                dst, kind="storyboard", project_name=project_name,
                                seed=seed, prompt=item.get("prompt"), workflow_key="storyboard_gen",
                                qc=gate, shot_id=shot_id,
                                extra={"ref_count": item.get("ref_count")})
                            break
                        if not verdict.get("ok"):
                            # 质检接口异常：保留暂存图，不盲目重生成（闸门会阻断入库）
                            break
                        # ★ 立刻把这次的缺陷沉淀进教训库 ——
                        #   这样「同一次重试循环的下一次」就能召回它（旧实现只在循环结束后记一次，
                        #   导致前 N 次重试拿不到任何信息，纯粹换种子瞎撞）
                        _record_qc_lesson(project_name, "storyboard", orig_prompt, rec)
                        # ★ 重试止损：连续两次缺陷一字不差 → 「改提示词 + 换种子」根本没带来
                        #   任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 这样白烧 6 次，
                        #   全程 GPU 十几分钟，出的图全都一样）。放在闸门与教训沉淀之后：
                        #   本镜若达标早已 break，不受影响；止损只减少无效重试，不改结论。
                        _hopeless, _hopeless_detail = _qc_retry_hopeless(attempts)
                        if _hopeless:
                            # 标记最后一条质检记录：_qc_summary 据此把 label 写成「已停止重试」
                            rec["retry_stopped"] = True
                            rec["retry_stopped_features"] = _hopeless_detail
                            item["qc_retry_stopped"] = {
                                "reason": "连续两次缺陷完全相同，判定重试无收益，已提前停止",
                                "features": _hopeless_detail, "attempts": len(attempts)}
                            app.logger.warning(
                                f"分镜重试止损（镜头 {shot_id}）：连续 {len(attempts)} 次缺陷完全相同，"
                                f"提前停止重试。缺陷：{_hopeless_detail}；"
                                f"建议改写该镜剧本字段（camera / description）后单独重跑该镜")
                            break
                    # ---------- best-of-N 收口：从候选里选**质检分最高**的一张入库 ----------
                    # 仅在 best_of>1 且收集到候选时生效；==1 时此块完全不出现（零回归）。
                    # 达标 → 入库；最佳仍未达标 → **不在此处阻断**，交下方统一阻断逻辑处理
                    #（scratch_png 已指向最佳候选，确保「不合格产物不留本地」删的是被选中那张）。
                    if best_of > 1 and _best_candidates:
                        _bi = qc_client.pick_best_candidate([c[0] for c in _best_candidates])
                        _bs, _bpng, _brec, _bgate = _best_candidates[_bi]
                        scratch_png = _bpng
                        item["qc_gate"] = _bgate
                        item["best_of"] = {
                            "candidates": len(_best_candidates), "picked": _bi + 1,
                            "score": _bs, "accepted": bool(_bgate.get("accept"))}
                        app.logger.info(
                            "镜头 %s best-of-N：%d 张候选中选第 %d 张（score=%s，达标=%s）",
                            shot_id, len(_best_candidates), _bi + 1, _bs,
                            bool(_bgate.get("accept")))
                        if _bgate.get("accept"):
                            shutil.copy2(_bpng, dst)
                            item["file"] = dst
                            item["url"] = f"{_sb_url_base}shot_{seq:02d}.png"
                            item["success"] = True
                            item.pop("error", None)
                            _write_artifact_meta(
                                dst, kind="storyboard", project_name=project_name,
                                seed=seed, prompt=item.get("prompt"),
                                workflow_key="storyboard_gen", qc=_bgate, shot_id=shot_id,
                                extra={"ref_count": item.get("ref_count"),
                                       "best_of": len(_best_candidates)})
                    if qc_declared or qc_on:
                        item["qc"] = _qc_summary(attempts, qc_declared, qc_on,
                                                 int(qc_cfg.get("max_retries", 0)))
                        gate = item.get("qc_gate")
                        # ⭐ 2026-10-08（用户拍板：A 方案）分镜质检「软放行」——
                        #    质检不达标只记录、仍写入正式目录。动机（实测）：同一项目内
                        #    已通过的镜3 九宫格雷同度 0.831，而信息量更大的镜4/28/37
                        #    （边缘密度 8.29/11.83/8.55，均高于已通过镜1 的 6.58）却被
                        #    判「主体缺失/内容错误」硬阻断 —— VLM 判官在此项上双标，把
                        #    可用图长期挡在门外，整集永远凑不齐。开关：qc_config.
                        #    storyboard_soft_qc（默认 True）；置 False 即回到旧硬阻断。
                        #    实现：把 gate 就地改成 accept，下游既有的「未达标→阻断」
                        #    分支自然不再进入（零缩进改动，风险最小）。仅分镜图生效。
                        if ((not gate or not gate.get("accept"))
                                and bool(qc_cfg.get("storyboard_soft_qc", True))
                                # ⭐ 2026-10-08：结构性缺陷（面板雷同 / 场景不一致 /
                                #    左右手镜像）**不参与软放行** —— 它们是「九宫格
                                #    拆解本身失败」，图不可用。实测 shot_04 格3/4/6/9
                                #    四格近乎相同，质检已判「九宫格面板雷同」，却被
                                #    软放行入库，用户看到的就是四格雷同的废图。
                                and not _sb_structural_defect(gate)):
                            _soft_lbl = (gate or {}).get("label") or ""
                            _soft_rsn = ((gate or {}).get("reason") or "")[:200]
                            _soft_ok = False
                            try:
                                if (scratch_png and os.path.isfile(scratch_png)
                                        and os.path.getsize(scratch_png) > 0
                                        # ⭐ 归属校验：必须是**本镜**的暂存图，防串镜
                                        and f"shot_{seq:02d}_" in os.path.basename(scratch_png)):
                                    shutil.copy2(scratch_png, dst)
                                    gate = dict(gate or {})
                                    gate.update({"accept": True, "soft_accepted": True,
                                                 "blocked": False})
                                    item["qc_gate"] = gate
                                    item["success"] = True
                                    item["qc_soft_accepted"] = True
                                    item["file"] = dst
                                    item["url"] = f"{_sb_url_base}shot_{seq:02d}.png"
                                    item.pop("qc_blocked", None)
                                    item.pop("error", None)
                                    _write_artifact_meta(
                                        dst, kind="storyboard", project_name=project_name,
                                        seed=seed, prompt=item.get("prompt"),
                                        workflow_key="storyboard_gen", qc=gate,
                                        shot_id=shot_id,
                                        extra={"ref_count": item.get("ref_count"),
                                               "qc_soft_accepted": True})
                                    _soft_ok = True
                                    app.logger.warning(
                                        "分镜质检未达标但已软放行入库（storyboard_soft_qc）："
                                        "镜头 %s｜判定=%s｜原因=%s",
                                        shot_id, _soft_lbl, _soft_rsn)
                            except Exception as _se:  # noqa: BLE001
                                app.logger.warning(
                                    "分镜软放行入库失败（回落硬阻断）：镜头 %s｜%s",
                                    shot_id, _se)
                            if not _soft_ok:
                                app.logger.warning(
                                    "分镜软放行未生效（无可入库暂存图）→ 保持硬阻断：镜头 %s",
                                    shot_id)
                        if not gate or not gate.get("accept"):
                            # 教训已在循环内逐次沉淀；这里兜底记一次终态（同键会被去重）
                            if attempts and isinstance(attempts[-1], dict):
                                _record_qc_lesson(project_name, "storyboard",
                                                  orig_prompt, attempts[-1])
                            # P0：质检不达标 / 调用异常 → 阻断入库
                            item["success"] = False
                            item["qc_blocked"] = True
                            item.pop("file", None)
                            item.pop("url", None)
                            # ★ 用户需求：质检「判定不通过」的暂存图不留本地（含 ComfyUI 侧）。
                            # ⚠️ 只删「质检成功返回且判定不合格」的产物：ok=False（接口故障/
                            # 超时/鉴权失败）、skipped（未开启）、qc_on=False（接口未就绪）都
                            # 不是产物不合格，删下去会误删好图。见 _reject_artifact 红线说明。
                            try:
                                if qc_on and attempts and attempts[-1].get("ok") is True:
                                    _purge_rejected_artifacts(
                                        [scratch_png], project=project_name,
                                        reason=f"分镜图质检不合格（{gate['label']}）" if gate else "",
                                        kind="storyboard_image",
                                        history_file=attempts[-1].get("history_file") or "")
                            except Exception as _pe:  # noqa: BLE001
                                app.logger.warning(f"分镜图不合格产物清理失败（忽略）：{_pe}")
                            item["error"] = ((f"分镜图质检阻断（{gate['label']}）：{gate['reason']}"
                                              "；未通过质检，未写入正式目录（暂存图见质检历史）")
                                             if gate else (item.get("error")
                                                           or "分镜图生成失败，未写入正式目录"))
                    elif "qc" not in item:
                        item["qc"] = {"enabled": False, "status": "disabled",
                                      "label": "质检未开启", "attempts": 0, "regenerated": 0}
            except _PromptQCBlocked as _pqb:
                # ⭐ P0（2026-10-02 实测定位）：预检阻断**必须**单独捕获。
                #   旧实现只写 item["error"]，而 `item["success"]=True` 是成功路径里
                #   **无条件先写**的（生成完图就置位，见上方 item.update），此后才做预检/
                #   质检/入库。异常一抛，success 保持 True 且 file 指向从未 copy2 的路径
                #   → manifest 谎报「6/6 成功」而正式目录一张图都没有：
                #     · api_storyboard_canvas 按 os.path.isfile 判 exists=False（界面无图）
                #     · 下游 video 拿这个不存在的 file 当 I2V 首帧
                #     · 断点续跑 skip 判据也是 os.path.isfile → 永远跳不过，每轮白重跑
                #   与下方 `item["success"]=False`（质检闸门口径）对齐：阻断即非成功。
                app.logger.warning("镜头 %s 分镜图被提示词预检阻断：%s", shot_id, _pqb)
                item["success"] = False
                item["file"] = ""
                item.pop("url", None)
                item["error"] = str(_pqb)
            except Exception as shot_err:
                import traceback as _tb
                # B 自愈（2026-10-08）：异常若是 ComfyUI 拒连/超时，重启 ComfyUI 后再让下一轮重跑
                if not comfyui_client.comfy_online(timeout=3):
                    if _sb_heal_comfyui(task_id, project_name):
                        item["error"] = "ComfyUI 异常已自愈重启，本镜头下一轮重跑"
                        continue
                app.logger.error(f"镜头 {shot_id} 分镜图生成失败: {shot_err}\n{_tb.format_exc()}")
                # ⭐ 同上：任何异常都不得留下「success=True 但无产物」的僵尸条目。
                #   成功路径先置位、后续任何一步抛错都必须在这里复位（fail-closed）。
                item["success"] = False
                item["file"] = ""
                item.pop("url", None)
                item["error"] = str(shot_err)

            _shot_elapsed = time.perf_counter() - _shot_t0
            item["elapsed_sec"] = round(_shot_elapsed, 1)
            # ⭐ 逐镜总墙钟（2026-10-07）：前置段（LLM/3D/归一/预检）+ GPU 出图 + 质检 + 重试
            #    的总和。配合 _prep_shot 的 [分镜计时] 明细，可一眼区分「前置段慢」还是
            #    「GPU/质检/重试慢」。只观测，不改任何分支行为。
            app.logger.info("[分镜总计时] shot=%s 成功=%s 墙钟 %.1fs（前置段 %.1fs）",
                            shot_id, bool(item.get("success")), _shot_elapsed,
                            float(item.get("phase_secs_total") or 0.0))
            # 2026-10-08（用户要求）：逐镜上报「正在拍第几镜」，前端显示「分镜 16/41」
            #    而不是干等一个「已运行 N 分钟未推进」；顺带刷新 step_updated_at。
            try:
                import autopilot as _ap
                _ap.report_progress(
                    f"分镜 {i + 1}/{len(shots)}"
                    + ("" if item.get("success") else "（重试中）"))
            except Exception:  # noqa: BLE001
                pass
            manifest_shots.append(item)
            with lock:
                generation_state[task_id]["results"].append(item)
                generation_state[task_id]["progress"] = int((i + 1) / max(len(shots), 1) * 100)
                # G10b（2026-09-30）：该镜收尾 → 标记 live 完成（前端据此区分「进行中/已定稿」）
                _live = generation_state[task_id].get("live")
                if isinstance(_live, dict) and _live.get("shot") == shot_id:
                    _live["done"] = True

        ok = sum(1 for r in manifest_shots if r.get("success"))
        blocked = sum(1 for r in manifest_shots if r.get("qc_blocked"))
        manifest = {
            "project_name": project_name,
            # 从模板表取，避免模型换代后 manifest 里还写着旧工作流名（口径漂移）
            "workflow": WORKFLOW_TEMPLATE.get("storyboard_gen", ""),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(manifest_shots),
            "success_count": ok,
            "qc_blocked_count": blocked,
            "shots": manifest_shots,
        }
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」。落盘失败仍回滚 status。
        _sb_mp = os.path.join(out_dir, "storyboard_manifest.json")
        try:
            atomic_write_json(_sb_mp, manifest)
        except Exception as _mp_err:
            app.logger.warning(f"分镜 manifest 原子写失败（已回滚 status）：{_mp_err}")
            with lock:
                generation_state[task_id].update({
                    "status": "failed",
                    "progress": 100,
                    "success_count": ok,
                    "qc_blocked_count": blocked,
                    "output_dir": out_dir,
                    "error": f"分镜 manifest 落盘失败：{_mp_err}",
                })
            return

        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok else "failed",
                "progress": 100,
                "success_count": ok,
                "qc_blocked_count": blocked,
                "output_dir": out_dir,
                "error": "" if ok else "所有镜头分镜图均生成失败",
            })
    except Exception as e:
        app.logger.error(f"分镜图任务失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    # ★ 用户决策 4：ComfyUI 侧分镜参考图上传残留（sb_ref_*）只在**本轮分镜批量生成全部
    # 结束后**按项目清理一次 —— 不在单镜循环里调（那会在重试中途删掉当前镜头正在用的参考图）。
    try:
        _purge_sb_refs(project_name)
    except Exception as _sb_ref_err:  # noqa: BLE001
        app.logger.warning(f"sb_ref 残留清理失败（忽略）：{_sb_ref_err}")
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    # 分镜批量是本项目重跑最密集的环节（每失败一次多一条任务历史）→ 收尾顺手清历史面板。
    _maybe_clear_comfyui_history("storyboard 批量生成收尾")


def _project_subtitle_enabled(project_name: str = "") -> bool:
    """该项目的成片「硬字幕」开关（config.json 的 subtitle_enabled），默认 False。

    2026-09-24（用户明确要求「不要生成字幕」）：
    成片阶段有两处会往视频里烧硬字幕（pipeline.step_final / video_postprocess.finalize_episode），
    此前无条件执行。现在统一从这里取值：读不到 / 非 true → 视为关闭，直接不烧字幕。
    这样「H3 提示词不诱导字幕」+「成片不烧字幕」两层都封死，
    确需硬字幕的老项目可在其 config.json 里显式写 "subtitle_enabled": true 单独放开。
    """
    proj = _safe_project(project_name or "")
    default = bool(PROJECT_DEFAULT_CONFIG.get("subtitle_enabled", False))
    try:
        rec = project_store.get_project(proj)
        if rec:
            cfg = project_store.read_config(rec["dir_key"])
            if "subtitle_enabled" not in cfg:
                return default
            val = cfg.get("subtitle_enabled")
            # 宽容解析：字符串 "false"/"0"/"no"/"off" 不能被 bool() 误判为「开」
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off", "none", "null", ""):
                    return False
                return default
            return bool(val)
    except Exception as e:  # noqa: BLE001  开关读取失败按「关闭」处理（安全侧）
        app.logger.warning(f"读取项目 config.subtitle_enabled 失败（按关闭处理）：{e}")
    return default


def _project_caption_burn_enabled(project_name: str = "") -> bool:
    """该项目的「字幕/转场 caption」烧制开关（config.json 的 caption_burn_enabled），**默认 True**。

    与 _project_subtitle_enabled 是**两件事**，刻意分开：
      · subtitle_enabled（默认关）：把人物开口的台词转录成硬字幕——辅助性文字，
        用户 2026-09-24 明确要求不要；
      · caption_burn_enabled（默认开）：把剧本 caption 烧进成片——它是**剧情装置**
        （时空落点、时空回溯、集尾悬念）。参考改编稿正是靠「春秋蝉，逆转时光。」
        让观众看懂时空跳变；不烧就会看到无过渡的跳切。
    不想要字幕的项目在其 config.json 写 "caption_burn_enabled": false 即可。
    """
    proj = _safe_project(project_name or "")
    default = bool(PROJECT_DEFAULT_CONFIG.get("caption_burn_enabled", True))
    try:
        rec = project_store.get_project(proj)
        if rec:
            cfg = project_store.read_config(rec["dir_key"])
            if "caption_burn_enabled" not in cfg:
                return default
            val = cfg.get("caption_burn_enabled")
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off", "none", "null", ""):
                    return False
                return default
            return bool(val)
    except Exception as e:  # noqa: BLE001  开关读取失败按默认处理
        app.logger.warning(f"读取项目 config.caption_burn_enabled 失败（按默认开启处理）：{e}")
    return default


def _style_aspect_confirmed(project_name: str) -> dict:
    """生成前置确认门判据：用户是否已与总控 AI 确认「风格」与「视频比例」。

    单一事实源 = 已应用的总控设定（ai_chat/project_settings.json，按项目）。
    - 风格确认：settings 的风格基调(style) 或 画风(art_style) 任一非空；
    - 比例确认：settings 的画面比例(aspect_ratio，即视频画幅，如 9:16) 非空。
    资产图的画幅已按类型内置写死（见 style_kit.asset_aspect_ratio），不依赖此比例；
    这里确认比例只为「视频 / 分镜」画幅服务。
    """
    try:
        view = ai_chat.settings_view(AI_SETTINGS_PATH, project_name or "") or {}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"确认门读取总控设定失败（按未确认处理）：{e}")
        view = {}
    s = view.get("settings") or {}
    style_confirmed = bool((s.get("style") or s.get("art_style") or "").strip())
    # 2026-09-23（建项目选风格）：用户「新建项目」时下拉/自定义的风格写进 config.json 的
    # style，也算「风格已确认」——否则用户明明选了风格，生成仍被 409 拦在「尚未确认风格」，
    # 与「建项目时就能选风格」的体验自相矛盾。总控 AI 敲定（project_settings）仍是第一优先级。
    if not style_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_style = str(project_store.read_config(rec["dir_key"]).get("style") or "").strip()
                style_confirmed = bool(cfg_style)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"确认门读取 config.style 失败（忽略）：{e}")
    aspect_confirmed = bool((s.get("aspect_ratio") or "").strip())
    # 2026-09-23（建项目选比例）：用户「新建项目」时选的画面比例写进 config.json 的
    # aspect_ratio，也算「比例已确认」，与 style 的同源兜底保持一致。总控 AI 敲定仍是第一优先级。
    if not aspect_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_ar = str(project_store.read_config(rec["dir_key"]).get("aspect_ratio") or "").strip()
                aspect_confirmed = bool(cfg_ar)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"确认门读取 config.aspect_ratio 失败（忽略）：{e}")
    missing = []
    if not style_confirmed:
        missing.append("风格")
    if not aspect_confirmed:
        missing.append("视频比例")
    return {"confirmed": style_confirmed and aspect_confirmed,
            "style_confirmed": style_confirmed, "aspect_confirmed": aspect_confirmed,
            "missing": missing, "settings": s}


def _style_aspect_guard(project_name: str, override_style: str = ""):
    """生成入口前置校验门（2026-09-22 需求）。

    用户未与总控 AI 确认「风格 / 视频比例」时拦截生成：返回 409 + 可读提醒响应；
    已确认则返回 None（放行）。各生成端点在解析出 project_name 后调用它。
    前端 client.ts readError 会自动弹出 error + guide 文案，提示去总控确认。

    ``override_style``：个别入口（如托管 /api/autonomous/start）允许调用方**显式传风格**
    （plan_overrides.style）——此时视「风格」为已确认，但「视频比例」仍须总控确认。
    """
    chk = _style_aspect_confirmed(project_name)
    if override_style and not chk["style_confirmed"]:
        chk["style_confirmed"] = True
        chk["missing"] = [m for m in chk["missing"] if m != "风格"]
        chk["confirmed"] = chk["style_confirmed"] and chk["aspect_confirmed"]
    if chk["confirmed"]:
        return None
    _names = "、".join(chk["missing"])
    return jsonify({
        "success": False,
        "requires_confirm": True,
        "error": f"尚未与总控 AI 确认{_names}，暂不开展生成。",
        "guide": ("请先在「AI 对话 · 创作总控」里与 AI 敲定" + _names
                  + "（风格：画风/基调；视频比例：画面画幅，如 9:16 / 16:9 / 1:1），"
                    "点击「应用设定」落盘后再开始生成。"),
        "missing": chk["missing"],
        "style_confirmed": chk["style_confirmed"],
        "aspect_confirmed": chk["aspect_confirmed"],
    }), 409


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

def _blocking_spec_text(shot: dict) -> str:
    """本镜的**确定性构图规格**文字（3D 导演台），用于替代「无面人偶预演图」送质检。

    背景：把预演图与成品图一起送视觉质检会严重污染判定（同一张合格图 88 → 35），
    污染来自图像本身而非措辞，所以改送这段文字规格（人数/左右顺序/景别/机位）。
    任何异常都返回空串 —— 构图规格绝不能影响质检主流程。
    """
    try:
        import te_3d_director  # noqa: PLC0415
        return te_3d_director.blocking_spec_text(shot)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检] 构图规格生成失败（不影响判定）：%s", e)
        return ""


def _collect_reference_images(character_refs: list, scene_refs: list) -> list:
    """把前端传来的参考图（可能是 /api/assets/... 的 HTTP 资源路径）解析为本地绝对路径

    修复 P0-4：原实现直接 os.path.exists(HTTP 路径) 恒为 False，参考图永远传不到 H3。
    """
    ref_imgs = []
    for ref in list(character_refs)[:2]:
        for key in ("front", "base"):
            p = ref.get(key) if isinstance(ref, dict) else None
            local = comfyui_client.resolve_local_path(p) if p else None
            if local and os.path.exists(local):
                ref_imgs.append(local)
                break
        else:
            app.logger.warning(f"角色参考图不可用: {ref.get('name') if isinstance(ref, dict) else ref}")
    for ref in list(scene_refs)[:1]:
        for key in ("front", "base"):
            p = ref.get(key) if isinstance(ref, dict) else None
            local = comfyui_client.resolve_local_path(p) if p else None
            if local and os.path.exists(local):
                ref_imgs.append(local)
                break
    return ref_imgs


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


def _qc_brief(kind: str) -> dict:
    """任务级质检摘要（随状态接口下发，供前端展示徽标与开关状态）"""
    try:
        cfg = _qc_load_cfg()
        view = qc_client.public_view(cfg)
        active = view["image_qc_active"] if kind == "image" else view["video_qc_active"]
        return {"enabled": view["enabled"], "active": active,
                "declared": bool(view["enabled"] and (cfg.get("image_enabled") if kind == "image"
                                                      else cfg.get("video_enabled"))),
                "pass_score": cfg.get("pass_score"), "max_retries": cfg.get("max_retries"),
                "video_frame_count": cfg.get("video_frame_count"),
                "model": view["effective_model"], "endpoint_source": view["endpoint_source"]}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检摘要生成失败（忽略）：{e}")
        return {"enabled": False, "active": False, "declared": False}


class _PromptQCBlocked(RuntimeError):
    """提示词预检未通过（内部信号）

    批处理循环里每个镜头是一大段嵌套代码，用异常跳出比「把生成段整体再缩进一层」
    安全得多；异常会被同一层的 ``except Exception`` 接住，该镜头照常记入 manifest
    （状态为失败），不会从产物清单里消失。
    """


def _prompt_preflight(kind: str, prompt: str, *, ctx=None, style: str = "",
                      ref_count=None, expect_refs=None, project_name: str = "",
                      cfg: dict = None) -> tuple:
    """生成前提示词预检 + 确定性自愈（统一入口）。

    返回 ``(应交给生成端的提示词, preflight 结果, 闸门结论)``。

    ``project_name``：可选；给出时会在预检**之前**先召回 ``kind="prompt"`` 的历史教训
    并叠加，把「历史上被预检判死的输入模式」变成显式补丁后再进预检（US-7）。

    ``cfg``：G13（P1）可选，传入 worker 级已读取的质检配置则直接复用，避免逐镜再
    触发一次 ``_qc_load_cfg()``（读 JSON + Fernet 解密）；不传则按需自读（向后兼容）。

    ⚠️ **一律 fail-open**：预检自身异常时按「原样放行」处理并记 warning。
    这一层是新增的保险，绝不能因为它自己出问题就把整集生产卡死。
    """
    text = str(prompt or "")
    # 「提示词质检」教训的稳定键：预检**之前**的原始提示词（不含召回叠加块、不含自愈结果）。
    # 与尾帧路径（_keyframe_prompt_preflight）同一口径 —— 键漂移会让同一条缺陷反复入库。
    _pf_orig = text
    try:
        cfg = cfg if cfg is not None else _qc_load_cfg()
        if project_name:
            # 召回叠加在原始提示词上（自愈前）；调用方已保留 orig_prompt 作稳定 phash 键。
            text = prompt_memory.learned_prompt(
                kind="prompt", prompt=text, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=style)
        pf = prompt_qc.preflight(kind, text, ctx=ctx, style=style, cfg=cfg,
                                 ref_count=ref_count, expect_refs=expect_refs)
        gate = prompt_qc.prompt_qc_gate(pf, cfg)
        if pf.get("repairs") or pf["verdict"].get("issues"):
            app.logger.info(
                "提示词预检[%s] %s｜自愈 %s 项｜issue %s 项｜accept=%s",
                kind, pf.get("label"), len(pf.get("repairs") or []),
                len(pf["verdict"].get("issues") or []), pf.get("accept"))
        # ---- 「提示词质检」教训沉淀（kind="prompt"）----
        # 缺口修复（2026-10-08）：此前全项目只有尾帧预检接了下沉（_record_preflight_lesson
        # 的唯一调用点），资产 / 分镜 / H3 视频这三条预检路径只写日志与返回体
        # （item["prompt_qc"]）→ 「AI 记忆 · 质检教训库 · 提示词质检」恒为空：
        # 用户看到的是「没做预检」，实际是「做了没留档」。在这里统一沉淀，5 个调用点一次覆盖。
        # 口径与尾帧一致：被拦下、或复检后仍有 issue 才记（无害自愈不入库，避免刷屏）。
        if project_name and not pf.get("skipped") and (
                not gate.get("accept") or (pf.get("verdict") or {}).get("issues")):
            try:
                _record_preflight_lesson(project_name, _pf_orig, pf, gate)
            except Exception as _pf_lesson_err:  # noqa: BLE001 - 沉淀失败绝不影响生成
                app.logger.warning("提示词教训沉淀失败（忽略）：%s", _pf_lesson_err)
        return pf["prompt"], pf, gate
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"提示词预检异常（{kind}），按放行处理：{e}")
        skip = {"ok": False, "skipped": True, "accept": True, "blocked": False,
                "label": "提示词预检异常", "reason": str(e), "repairs": [],
                "verdict": {"issues": [], "critical_issues": [], "reason": str(e)}}
        return text, skip, {"accept": True, "blocked": False, "skipped": True,
                            "label": "提示词预检异常", "reason": str(e),
                            "critical_issues": [], "repairs": []}


def _qc_retry_hopeless(attempts: list, streak: int = 2) -> tuple:
    """连续 ``streak`` 次重试的缺陷特征**完全相同** → 判定「改提示词 + 换种子」没有产生
    任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 连烧 6 次全败，缺陷一字不差）。

    返回 ``(True, "缺陷摘要")`` 或 ``(False, "")``。

    G1 修复：把本函数抽到 qc_client.qc_retry_hopeless（共享叶子模块），
    供 comfyui_client / keyframe 以注入回调（qc_stop_cb）方式复用，
    规避 app ↔ comfyui_client 的循环依赖。app 侧把 style_issues 合并进
    issues 再委托 —— 通用模块不认识 app 特有字段。

    ⚠️ 刻意保守（宁可多试一次，也不要误停）：
      · 特征为**空**时一律不判定 —— 质检没给出可用信息 ≠ 缺陷相同；
      · 必须最近 streak 条**逐条集合相等**（多一条少一条都不算）；
      · **不改变闸门结论**：该镜仍算未通过、仍不进正式目录，只是不再继续重试；
        用户可直接改这一镜的剧本字段（如 camera / description）后单独重跑该镜。
    """
    merged = []
    for r in (attempts or []):
        if not isinstance(r, dict):
            continue
        issues = list(r.get("issues") or [])
        style = list(r.get("style_issues") or [])
        if style:
            issues = issues + style
        merged.append({"issues": issues, "critical_issues": r.get("critical_issues") or []})
    return qc_client.qc_retry_hopeless(merged, streak)


def _qc_prune_attempts(scratch_dir: str, keep: int = 4) -> None:
    """G8：质检暂存区 try 产物滚动保留 —— 每个 shot/asset 只保留最近 keep 个尝试。

    背景（审计 G8）：图片链路每次都把产物 copy 进暂存区 `output/qc/<项目>/{assets,storyboard,
    video}_scratch/`，不达标越多残留越多，实测 qc 目录膨胀到 1.1GB。临时产物（`*_tryN`）
    只有「最近几轮」对续跑/排障有意义，更早的纯浪费。这里按 (前缀, 尝试号, 扩展名) 归组，
    每组只留最大的 keep 个 try，其余删除。

    设计取舍：
      - 只清「带 _try 后缀的临时产物」，正式交付图（base.png/shot_XX.png）绝不动；
      - 单文件失败静默跳过（清理是优化而非功能，绝不能因清错文件阻断生产）；
      - 保留策略对 `.png`/`.mp4`/`.srt`/`.list` 通用，三类暂存区都能复用。
    """
    if not os.path.isdir(scratch_dir):
        return
    try:
        import re as _re
        groups = {}   # (前缀, 扩展名) -> [尝试号]
        info = {}      # 尝试号 -> 完整路径
        for fn in os.listdir(scratch_dir):
            # ⚠️ 单镜重跑落盘名是 `shot_NN_retry.png`（**无** `_tryN` 数字后缀），
            # 旧正则 `^(.+)_try(\d+)(\.\w+)$` 匹配不到 → 该文件永不被滚动清理、只增不减。
            # 这里把 `_retry` 也纳入：归到 num=0（比任何 `_tryN` 都旧 → 优先被清）。
            m = _re.match(r"^(.+)_try(\d+)(\.\w+)$", fn)
            if not m:
                m = _re.match(r"^(.+)_retry(\.\w+)$", fn)
                if m:
                    prefix, num, ext = m.group(1), 0, m.group(2)
                else:
                    continue   # 非 try/retry 命名（正式产物/杂项）一律不动
            else:
                prefix, num, ext = m.group(1), int(m.group(2)), m.group(3)
            groups.setdefault((prefix, ext), []).append(num)
            info[(prefix, ext, num)] = os.path.join(scratch_dir, fn)
        removed = 0
        for (prefix, ext), nums in groups.items():
            nums = sorted(nums)   # P2-2（A-15）：listdir 无序，必须按尝试号排序，
                                  # 否则 `nums[-keep:]` 保留的是「最早遍历到」而非「最新尝试号」
            keep_set = set(nums[-keep:]) if len(nums) > keep else set(nums)
            for num in nums:
                if num in keep_set:
                    continue
                p = info.get((prefix, ext, num))
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass   # 文件被占用/权限问题：跳过，不阻断
        if removed:
            app.logger.info("G8 暂存区滚动清理 %s：删 %d 个过期 try（每组保留最近 %d）",
                            scratch_dir, removed, keep)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("G8 暂存区清理失败（不影响生产）：%s: %s",
                           type(e).__name__, e)


def _write_artifact_meta(artifact_path: str, *, kind: str, project_name: str,
                         seed=None, prompt=None, workflow_key=None,
                         elapsed=None, qc=None, shot_id=None, asset_name=None,
                         extra=None) -> None:
    """O2：产物旁路元数据 —— 在正式产物旁写 `<产物>.meta.json`（可追溯/可复现）。

    记录：seed / prompt / 工作流文件名 + SHA256（内容指纹，而非仅文件名）/ 耗时 /
    生效质检结论。此前 manifest 只记工作流**文件名**，无法校验"当初到底用哪版工作流
    出的这张图"；SHA256 让产物与生成时点的工作流内容一一对应。

    纯旁路（绝不阻断生产）：任何异常静默降级、只留 debug 日志 —— meta 缺失不影响主流程。
    """
    try:
        import hashlib
        import config as _cfg
        meta = {
            "artifact": os.path.basename(artifact_path),
            "kind": kind,
            "project": project_name,
            "seed": seed,
            "prompt": (str(prompt)[:8000] if prompt else None),   # 2026-10-06：2000 太短，
            # 事故复盘时判断不了「提示词是否缺协议段」，放宽到 8000
            "workflow": None,
            "workflow_sha256": None,
            "elapsed_sec": elapsed,
            "qc": qc,
            "extra": extra,
            "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if shot_id is not None:
            meta["shot_id"] = shot_id
        if asset_name is not None:
            meta["asset"] = asset_name
        # 工作流内容指纹（O2 核心：文件名 → 名 + SHA256）
        if workflow_key:
            try:
                tpl_name = _cfg.WORKFLOW_TEMPLATE.get(workflow_key)
                if tpl_name:
                    # 2026-10-09 修复「项目自包含」缺口：原实现直连 COMFYUI_WORKFLOWS_DIR，
                    # 项目内已有同名模板时仍去 ComfyUI 安装目录找 → 换机/离线时算不出指纹。
                    # 与其余调用点一致，改走解析器（项目优先，ComfyUI 回落）。
                    wf_path = _cfg.resolve_workflow_path(tpl_name)
                    if os.path.isfile(wf_path):
                        h = hashlib.sha256()
                        with open(wf_path, "rb") as _f:
                            for _chunk in iter(lambda: _f.read(65536), b""):
                                h.update(_chunk)
                        meta["workflow"] = tpl_name
                        meta["workflow_sha256"] = h.hexdigest()
            except Exception as e:  # noqa: BLE001  工作流指纹算不出不影响 meta 主体
                app.logger.warning("工作流指纹计算失败（不影响 meta 主体）：%s", e)
        out = os.path.splitext(artifact_path)[0] + ".meta.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001  旁路兜底：meta 写失败绝不阻断入库
        try:
            app.logger.debug("O2 产物元数据旁路写失败（不影响入库）：%s: %s",
                            type(e).__name__, e)
        except Exception:  # noqa: BLE001
            pass


def _qc_lesson_from_record(rec: dict) -> dict:
    """从一条质检历史记录里取出「缺陷」，供教训库沉淀。

    ⚠️ `_qc_record_verdict` 返回的记录把 score/reason/issues 放在**顶层**，
    **没有** `verdict` 子对象。此前写入教训时误读 `rec["verdict"]`（恒为 None → {}），
    于是教训库里躺着的全是 score=0 / reason="" / issues=[] 的空记录，
    召回时自然什么建议都给不出来 —— 重试就变成了「换种子瞎撞」。
    这里对两种形态都做兼容，避免再被字段形态坑一次。
    """
    if not isinstance(rec, dict):
        return {}
    inner = rec.get("verdict") if isinstance(rec.get("verdict"), dict) else {}
    score = rec.get("score")
    if score is None:
        score = inner.get("score")
    issues = list(rec.get("issues") or inner.get("issues") or [])
    issues += list(rec.get("critical_issues") or inner.get("critical_issues") or [])
    issues += list(rec.get("style_issues") or inner.get("style_issues") or [])
    issues = [str(x).strip() for x in issues if str(x).strip()]
    reason = str(rec.get("reason") or inner.get("reason") or "").strip()
    if not issues and reason:
        issues = [reason]
    return {"score": score if score is not None else 0, "issues": issues[:20], "reason": reason}


def _record_qc_lesson(project_name: str, kind: str, prompt: str, rec: dict,
                      root_dir: str = "") -> dict:
    """把一次「质检不达标」沉淀成教训（供下次重试时改写提示词）。

    风格由 ``_project_style()`` 内部取（plan 的 style > AI 设定 > config.style），
    这样 6 个调用点不用各自找 style —— 它们本来就都在同一个项目上下文里。
    """
    lesson_src = _qc_lesson_from_record(rec)
    # 记录当时的视觉风格：召回时按「同风格加权 / 异风格降权」使用。
    # 没有它就无法回答「生成相同风格的提示词时有没有参考历史教训」——
    # 旧教训一律 context={}，跨画风的缺陷会串味（用 A 画风的标准要求 B 画风的图）。
    try:
        _qc_style = _project_style(project_name) or ""
    except Exception as _se:  # noqa: BLE001
        app.logger.warning("取项目风格失败（教训按无风格记录）：%s", _se)
        _qc_style = ""
    # 风格不达标：额外注入一条「明确的风格强化指令」，确保召回时能直接指导模型修正风格，
    # 而不是只给一条「风格不符」的缺陷描述。
    # ⚠️ 风格名必须写成占位符 {style}，**不能在记录时把项目风格写死**：
    #    教训库是跨项目复用的，写死会让 A 项目（中国古风玄幻）的教训被 B 项目
    #    （国漫偏写实）召回时强行要求 B 采用 A 的风格 —— 那是主动伤害。
    #    实际替换发生在 prompt_memory.suggestions(..., style=当前项目风格)。
    if (rec or {}).get("style_mismatch"):
        style_hint = ("画面风格与目标风格不符，必须严格采用「{style}」"
                      "的视觉风格、画风、渲染方式与配色，不得偏离")
        existing = lesson_src.get("issues") or []
        lesson_src["issues"] = [style_hint] + [i for i in existing if i != style_hint]
    if not lesson_src.get("issues") and not lesson_src.get("reason"):
        return {}
    try:
        got = prompt_memory.record(project=project_name, kind=kind, prompt=prompt,
                                   issues=lesson_src["issues"], reason=lesson_src["reason"],
                                   score=lesson_src.get("score"),
                                   # 2026-10-09：新增可选 root_dir 便于**隔离测试**（默认仍是项目输出目录，
                                   # 行为不变）。此前测试只能落真实教训库、再反手清理 —— 见报告第 116 节。
                                   root_dir=root_dir or PROJECT_OUTPUT_DIR, style=_qc_style)
        if got:
            app.logger.info("[教训库] %s 记录 %d 条缺陷（kind=%s score=%s style=%s）：%s",
                            project_name, len(lesson_src["issues"]), kind,
                            lesson_src.get("score"), _qc_style or "-",
                            lesson_src["issues"][:2])
        return got or {}
    except Exception as mem_err:  # noqa: BLE001
        app.logger.warning("记录质检教训失败：%s", mem_err)
        return {}


#: 优化器「思考过程」泄漏特征（2026-10-06 分镜图事故）。
#: 推理型模型会把中文推敲过程当正文返回；命中即整条弃用，回落原提示词 ——
#: 绝不把「关于提示词的元讨论」送进图片模型。
_OPT_REASONING_MARKERS = (
    "我们需要回答用户", "作为漫剧生成系统提示词优化器", "作为提示词优化器",
    "需要输出修正后的提示词", "需要保留原提示词", "原提示词有", "需要避免新增",
    "需要确保中文输出", "需要保留英文结构", "最好不要大改",
    "we need to", "the user wants", "i need to", "let me think",
)


def _sanitize_optimized_prompt(reply: str, original: str) -> str:
    """清洗「质检后即时优化」的模型回复：剥围栏 → 剥思考前缀 → 命中思考特征则判废。

    ⚠️ 2026-10-06 实测事故（分镜图输出 = 身份基准网格本身）：
    优化器模型（推理型）把**中文思考过程**当正文返回 —— 1823 字符、无 TASK /
    SCENE AND ACTION / FRAMING 等任何协议段，被原样当成提示词送进 ComfyUI。
    图片模型收到与画面无关的元讨论（「我们需要回答用户：作为提示词优化器…」），
    无法执行任何画面指令，退化为「原样返回参考图」：<image1> 恰是身份基准网格
    → 分镜图 = 3×3 角色基准网格（九格几乎同图）。

    任何一层判废都返回空串，由调用方回落原提示词（fail-open，绝不阻断重生成）。
    """
    t = str(reply or "").strip()
    if not t:
        return ""
    # ① 剥 Markdown 代码围栏（用 chr(96) 拼，避免源码里出现三反引号本身）
    _fence = chr(96) * 3
    if t.startswith(_fence):
        t = t[len(_fence):].lstrip()
    if t.endswith(_fence):
        t = t[:-len(_fence)].rstrip()
    if not t:
        return ""
    # ② 思考前缀：正文必然复现原提示词的**首个协议段**（改写不改段序），
    #    就把它当切点。注意用 >=0 判断：合格的改写可能正好以该段开头（位置 0），
    #    此时绝不能截断 —— 否则会吃掉第一段（曾实测把 "TASK:" 整句切掉）。
    _first = ""
    for _a in ("TASK:", "PRIMARY CANVAS", "FRAMING", "SCENE AND ACTION",
               "IDENTITY", "PRESERVE", "REFERENCE ROLES", "NO TEXT"):
        if _a in original:
            _first = _a
            break
    if _first:
        _cut = t.find(_first)
        if _cut > 0:
            t = t[_cut:].strip()
    # ③ 仍含思考特征 → 判废（宁可回落原提示词，也不送元讨论进图片模型）
    low = t.lower()
    for _mk in _OPT_REASONING_MARKERS:
        if _mk.lower() in low:
            return ""
    return t


def _storyboard_prompt_structurally_ok(prompt: str, grid_mode: bool = True) -> tuple:
    """分镜提示词的**确定性结构闸门**（零模型、零误判）。

    背景（2026-10-06 事故）：重试路径把「LLM 即时优化」的结果直接交给 ComfyUI，
    **没有再走一次 prompt_qc 预检** —— 于是优化器返回的中文思考过程（无 TASK /
    FRAMING / SCENE AND ACTION 任何协议段）畅通无阻地进了图片模型，模型只好原样
    返回参考图（<image1> = 身份基准网格），分镜图就成了九格几乎同图的角色基准网格。

    返回 ``(是否合格, 缺失项列表)``。判据直接复用 prompt_qc 的协议段常量，
    与生成前预检**同一把尺子**，因此不会误伤合法提示词。
    """
    t = str(prompt or "")
    if not t.strip():
        return False, ["空提示词"]

    # ⚠️⚠️ 2026-10-09 修复：本闸门原先**只认英文官方协议段名**，而分镜实际走的是
    #   中文「逐格写死」九宫格范式（storyboard_grid_main）—— 它用中文段名，
    #   不含 TASK:/FRAMING/SCENE AND ACTION，于是**必然被判缺 3 段**。
    #   实跑证据：日志里「即时优化提示词缺协议段（任务段、景别段、画面段），弃用并回落原逻辑」
    #   出现了 **26 次** —— 即那条「LLM 针对本次缺陷即时改写」的增强链路**从未生效过一次**，
    #   每次还白跑一次 LLM 调用。（与 prompt_qc 对中文逐格版整批假红是同一类口径问题。）
    #   修法：先识别中文逐格范式，按它自己的骨架判；否则再走英文官方判据。
    if getattr(prompt_qc, "_grid_zh_protocol", None) and prompt_qc._grid_zh_protocol(t):
        _miss_zh = []
        if getattr(prompt_qc, "SB_MARK_GRID_ZH_PANEL", "分镜1（") not in t:
            _miss_zh.append("逐格清单")
        if "分镜9（" not in t:
            _miss_zh.append("9 格齐整")
        if "格间差异" not in t:
            _miss_zh.append("格间差异段")
        if not (getattr(prompt_qc, "SB_MARK_NO_TEXT", "") in t
                or getattr(prompt_qc, "SB_MARK_NO_TEXT_LEGACY", "") in t
                or "严禁出现任何台词文字或字幕" in t
                or "不出现开口说话的口型" in t):
            _miss_zh.append("无文字段")
        if "3x3" not in t.lower():
            _miss_zh.append("3x3 九宫格布局段")
        return (not _miss_zh), _miss_zh

    miss = []
    for _name, _mark in (("任务段", getattr(prompt_qc, "SB_MARK_TASK", "TASK:")),
                         ("景别段", getattr(prompt_qc, "SB_MARK_FRAMING_NEW", "FRAMING")),
                         ("画面段", getattr(prompt_qc, "SB_MARK_CONTENT", "SCENE AND ACTION"))):
        if _mark and _mark not in t:
            miss.append(_name)
    # ⚠️ 3x3 布局段只在**九宫格模式**下才是必备段：单帧分镜（SB_GRID_MODE 关）
    #    本来就不含 3x3，若无条件要求会误伤全部单帧镜头。
    if grid_mode:
        _low = t.lower()
        if "3x3" not in _low and "nine-panel" not in _low and "nine panel" not in _low:
            miss.append("3x3 九宫格布局段")
    _no_text = (getattr(prompt_qc, "SB_MARK_NO_TEXT", "NO TEXT") in t
                or getattr(prompt_qc, "SB_MARK_NO_TEXT_LEGACY", "\u753b\u9762\u4e2d\u4e0d\u5f97\u51fa\u73b0\u4efb\u4f55\u6587\u5b57") in t)
    if not _no_text:
        miss.append("无文字段")
    return (not miss), miss

def _optimize_prompt_from_qc(kind: str, prompt: str, rec: dict, style: str = "") -> str:
    """质检不达标后，用「文本分析模型」针对**本次这张图**的缺陷即时改写提示词。

    与 ``prompt_memory.learned_prompt``（召回**历史泛化**教训）的区别：
    这里把本次 verdict 的具体 issues 直接喂给 LLM，让它针对「这张图为什么没过」给出
    一条精准的提示词修正——而不是拼一条可能跨项目、可能过时的历史建议。

    返回优化后的提示词；任何失败（模型未配置 / 调用异常 / 返回空）都返回 None，
    由调用方回落原逻辑（换 seed / 召回历史教训），**绝不让优化环节阻断重生成**。
    """
    try:
        lesson = _qc_lesson_from_record(rec)
        issues = [str(x).strip() for x in (lesson.get("issues") or []) if str(x).strip()]
        reason = str(lesson.get("reason") or "").strip()
        if not issues and not reason:
            return None
        if not (prompt or "").strip():
            return None
        client = _optional_llm_client()
        if client is None:
            app.logger.info("[提示词优化] 文本分析模型未配置，跳过即时优化（回落历史召回/换种子）")
            return None
        kind_label = {"asset": "参考图", "storyboard": "分镜图",
                      "keyframe": "尾帧", "h3": "视频"}.get(kind, kind)
        issues_text = "\n".join(f"  - {i}" for i in issues[:6])
        style_text = (f"\n目标风格：{style}" if style else "")
        system = (
            "你是漫剧生成系统的提示词优化器。用户给出一段「生成图片用的提示词」和「质检判定它"
            "不达标的具体问题」，你要输出一段**修正后的提示词**，让重新生成能通过质检。\n"
            "要求：\n"
            "1. 只输出修正后的提示词正文，不要任何解释、前言、标号或 Markdown；\n"
            "2. 保留原提示词里仍然有效的描述（主体、外貌、材质、风格等），只针对列出的问题做精准修补；\n"
            "3. 用中文输出；\n"
            "4. 不要新增与问题无关的内容，不要改变原有画面主体；\n"
            "5. 修正要具体可执行（例如「去掉文字」就写「画面中不得出现任何文字/字幕/水印」）。"
        )
        # ⚠️ 2026-10-01 实测：参考图重试的优化器会写出与资产图**不变量**冲突的要求 ——
        # 实测输出「特写镜头，画面核心为黑色玉盒及其内部丹药，严禁全白纯色背景」，
        # 而守卫函数随后又追加「，纯白背景，…」→ 同一份提示词里出现矛盾指令，模型必然摇摆
        # （那一轮同时被「承托物」和「桌面/阴影」两条判据拦下就是因此）。
        # 这里把不变量写进系统提示，并要求它**不得写入与之冲突的措辞**。
        # 2026-10-09：分镜优化必须原样保留协议段骨架。
        #   实跑实录：优化器把逐格版提示词简化成一段话
        #   （「生成一张3x3的九宫格电影故事板主图，近景：林昭位于画面右侧…」），
        #   丢掉了「不得出现任何文字…」等段 -> 被 _skeleton_ok 判「丢失协议段」弃用
        #   -> 调用方回落教训改写。即这条「精准改写」链路再次失效。
        #   修法与 asset 的不变量同款：把「必须保留什么」写进系统提示。
        if kind == "storyboard":
            _must_keep = []
            try:
                import prompt_enhance as _pe_k
                for _m in getattr(_pe_k, "_STORYBOARD_SECTIONS", ()):  # noqa: SLF001
                    if _m in prompt:
                        _must_keep.append(_m)
            except Exception:  # noqa: BLE001
                _must_keep = []
            _keep_txt = ("、".join(f"「{m}」" for m in _must_keep)
                         if _must_keep else "原提示词里出现的全部段名与编号")
            system += (
                "\n\n【不可违背的分镜提示词骨架（优先级高于上面全部要求）】\n"
                f"1. 原提示词里出现的这些段**必须逐字原样保留**：{_keep_txt}；"
                "缺任何一段，结果都会被判废并白打一次调用。\n"
                "2. **逐格清单必须完整保留**（形如「*   **分镜N（景别，色调）：** …」的 9 条）。"
                "严禁把九宫格提示词压缩成一段话、严禁删格、严禁只写成「近景：…」这种单段式。\n"
                "3. 原提示词里的 <imageN> 编号必须一个不少、一个不多，保持原样。\n"
                "4. 只改被判不达标的那几处描述（如某格内容、道具状态、机位），"
                "其余文字照抄；不要重写整篇，不要改变篇幅量级。"
            )
        if kind == "asset":
            system += (
                "\n\n【不可违背的参考图不变量（优先级高于上面全部要求）】\n"
                "1. 纯白背景：画面里不得出现场景、地面、桌面、墙面、投影、背景纹理或任何环境元素；"
                "严禁写入「不要纯白背景 / 严禁全白纯色背景 / 加上场景或桌面」这类**与不变量冲突**的要求。\n"
                "2. 物品参考图只呈现**物品本体**：不得出现容器、托盘、盒子、底座、支架、展示台、"
                "碗碟、绸布等任何承托物，不得把物品放进或放在别的物体内部/上方，不得出现手或人物；"
                "严禁把「某个容器」（例如黑色玉盒）写成画面核心或主体。\n"
                "3. 角色参考图保持多视图横排、完整入画，不改变五官与服装。\n"
                "4. 只修补质检列出的问题，**不得引入任何新物体、新容器、新场景元素**。"
            )
        user = (
            f"原提示词：\n{prompt.strip()}\n\n"
            f"质检判定不达标的问题：\n{issues_text}"
            f"{'（结论：' + reason + '）' if reason else ''}{style_text}\n\n"
            f"请输出修正后的提示词："
        )
        # ⚠️ max_tokens 原为 1024：推理型优化器光思考过程就 1800+ 字符，正文被截断
        #    甚至根本没输出（见 _sanitize_optimized_prompt 事故注释）。
        # ⭐ 2026-10-08：把骨架校验的长度区间**前置**成模型硬约束。此前系统提示里
        #    零长度约束（5 条要求全是内容规则）→ 优化结果高频因「超出长度上限 /
        #    篇幅骤减 / 丢失协议段」被判废，每次都是一个白打的 LLM 调用。
        try:
            import prompt_enhance as _pe_len
            if not _pe_len.has_valid_window(prompt):
                _lo0, _hi0 = _pe_len.prompt_len_budget(prompt)
                app.logger.info(
                    "[提示词优化] %s 跳过：原文 %d 字符，接受区间 [%d,%d] 为空集"
                    "（模型无论怎么写都过不了，不再白打 LLM）",
                    kind_label, len(prompt), _lo0, _hi0)
                return None
            system += _pe_len.length_clause(prompt)
            _lo_len, _hi_len = _pe_len.prompt_len_budget(prompt)
            _mt_len = max(1500, min(4096, int(_hi_len / 1.2)))
        except Exception:  # noqa: BLE001 拿不到预算就沿用旧值，绝不阻断
            _mt_len = 3000
        reply = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=0.4, max_tokens=_mt_len,
        )
        optimized = _sanitize_optimized_prompt(reply, prompt)
        if not optimized or optimized == prompt.strip():
            app.logger.warning(
                "[提示词优化] %s 优化结果为空/被判废/无变化，回落原逻辑（回复前 60 字：%s）",
                kind_label, str(reply or "")[:60])
            return None
        # 骨架校验（复用生成前增强的同一把尺子）：段名齐全、<imageN> 集合不变、
        # H3 结构完整、不骤缩。不过就丢弃 —— 宁可回落原提示词，也不能把一段缺协议段
        # 的文本送进图片模型（那正是本次「分镜图 = 身份基准网格」事故的成因）。
        try:
            import prompt_enhance as _pe
            _ok, _why = _pe._skeleton_ok(kind, prompt, optimized)
        except Exception as _sv_e:  # noqa: BLE001
            _ok, _why = True, f"校验器异常（fail-open）：{_sv_e}"
        if not _ok:
            app.logger.warning(
                "[提示词优化] %s 结果未通过骨架校验（%s），弃用并回落原逻辑；候选前 80 字：%s",
                kind_label, _why, optimized[:80])
            return None
        app.logger.info("[提示词优化] %s 针对本次缺陷改写提示词（%d 条问题）：%s → %s",
                        kind_label, len(issues), prompt[:24], optimized[:40])
        return optimized
    except Exception as e:  # noqa: BLE001
        app.logger.warning("质检后即时优化提示词失败（回落原逻辑）：%s", e)
        return None


def _record_preflight_lesson(project_name: str, prompt_original: str, pf: dict,
                             gate: dict) -> dict:
    """把一次「提示词预检不通过 / 有缺陷」沉淀成 ``kind="prompt"`` 教训。

    ``prompt_original`` 必须是**自愈前**（也**不含召回叠加块**）的原始提示词，作为稳定
    phash 键。收敛 keyframe / asset / storyboard 三处预检的沉淀逻辑，避免复制粘贴。
    """
    pf = pf if isinstance(pf, dict) else {}
    verdict = pf.get("verdict") if isinstance(pf.get("verdict"), dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in (verdict.get("issues") or []) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(pf.get("reason") or gate.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "stage": "prompt_preflight",
        "label": str(pf.get("label") or gate.get("label") or ""),
    }
    if not rec["issues"] and rec["reason"]:
        rec["issues"] = [rec["reason"]]
    if not rec["issues"] and not rec["reason"]:
        return {}
    return _record_qc_lesson(project_name, "prompt", prompt_original or "", rec)


def _qc_style_of(project_name: str) -> str:
    """取项目**当前**风格，供教训召回替换建议里的 ``{style}`` 占位符。

    数据源优先用 autopilot 计划（用户与总控敲定的创作设定，最权威），
    其次退回项目级创作设定的 style；都取不到就返回空串
    （此时 ``prompt_memory`` 会主动丢弃带 ``{style}`` 的建议，而不是把别的项目的风格安上来）。
    """
    try:
        plan = autopilot.get_plan(project_name) or {}
        s = style_kit.normalize_style(plan.get("style"))
        if s:
            return s
    except Exception as e:  # noqa: BLE001
        app.logger.debug("读取托管计划风格失败（忽略）：%s", e)
    try:
        for ep in (novel_to_script.list_episodes(SCRIPT_DIR, project_name, project_name) or []):
            path = ep.get("path") or ""
            if path and os.path.isfile(path):
                data = project_store._read_json(path, {}) or {}
                s = style_kit.normalize_style(data.get("style"))
                if s:
                    return s
    except Exception as e:  # noqa: BLE001
        app.logger.debug("读取剧本风格失败（忽略）：%s", e)
    return ""


def _keyframe_recall_cb(project_name: str):
    """尾帧重试召回回调（注入 ``keyframe.generate_keyframes`` 的 ``recall_cb``）。

    返回闭包 ``(orig_prompt, shot, item) -> str``：用 preflight 自愈**之前**的原始尾帧
    提示词召回 ``kind="keyframe"`` 历史教训并叠加；无教训时原样返回（零行为变更）。
    """
    def _recall(orig_prompt: str, shot: dict, item: dict) -> str:
        try:
            return prompt_memory.learned_prompt(
                kind="keyframe", prompt=orig_prompt or "", project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=_qc_style_of(project_name))
        except Exception as e:  # noqa: BLE001 - 召回失败绝不影响生成
            app.logger.warning(f"尾帧教训召回失败（忽略）：{e}")
            return orig_prompt or ""
    return _recall


def _episode_frame_ratios(segs: list, max_frames: int = None) -> list:
    """D-05（P1）整集按段抽帧的占比列表 —— 实现见 ``qc_coverage.episode_frame_ratios``。

    抽成独立零依赖模块（``app/qc_coverage.py``）以便离线单测
    （``verify_episode_qc_coverage.py``）无需 Flask/requests 即可验证覆盖率。
    """
    return qc_coverage.episode_frame_ratios(segs, max_frames=max_frames)


def _episode_qc_desc(shots: list, limit: int = qc_coverage.DEFAULT_DESC_LIMIT) -> str:
    """构造整集质检用的「镜头信息」摘要 —— 实现见 ``qc_coverage.episode_qc_desc``。

    为什么不用 comfyui_client 传进来的 ``shot_desc``：整集模式下它传的是
    **所有段的 H3 提示词全文拼接**（每段都是六段式结构，几十段叠在一起），
    又长又难判读，还挤占上下文。整片质检真正需要的是「这一集有哪些镜头、
    各自什么景别和内容」，这里按镜头生成紧凑摘要。

    D-05（P1）：``limit`` 由 12 提到 60 —— 原来 40 镜的整集只把前 12 镜给模型，
    中后段镜头对模型**完全不可见**，与抽帧漏检叠加后整集质检形同虚设。
    另：一旦真的截断，必须在串里**显式声明「其余未提供」**，让模型知道信息不完整，
    而不是误以为整集只有 limit 个镜头。
    """
    return qc_coverage.episode_qc_desc(shots, limit=limit, warn=app.logger.warning)


def _qc_shot_desc(shot: dict) -> str:
    """构造交给质检模型的「镜头信息」。

    ⚠️ 景别必须带上**判定标准**，不能只给裸词。
    生成端用 ``SHOT_CAMERA_SPECS[camera]`` 的精确定义写提示词，而质检端此前只传
    「机位：中景跟拍」——模型只能凭自己的理解判「中景」，与生成端标准不一致，
    实测分镜图通过率仅 57%、失败原因几乎全是「景别不符」（把腰部以上的中景判成不合规）。
    这里改为引用 :func:`comfyui_client.camera_spec`（经模块顶部的 ``_camera_spec`` 别名调用，
    因为本文件里 ``comfyui_client`` 是实例而非模块），保证两端**同一份标准**。
    """
    parts = []
    # 景别放在最前：描述较长时 [:900] 截断会吃掉尾部，判定标准必须优先保住
    if shot.get("camera"):
        cam = str(shot["camera"]).strip()
        # ⚠️ 必须显示「解析后的景别」而不是只给裸词：camera 常是「景别+机位+运镜」的复合写法。
        # 且**景别未给时不许编**（旧实现回落中景 → 拿中景标准去判脚部俯拍图，必然判不符，
        # 该镜永远过不了；实测 ep02 shot_13 因此白烧 6 次 GPU）。
        cam_k = _camera_key(cam)
        parts.append(f"景别：{cam_k or '未指定'}（camera 原值「{cam}」；判定标准：{_camera_spec(cam)}）")
        # 机位与景别正交：只给景别不给机位的话，「要求俯拍却给了平视」没人能判出来。
        ang = _camera_angle(cam)
        if ang:
            parts.append(f"机位：{ang}（须与画面一致）")
    if shot.get("location"):
        parts.append(f"场景：{shot['location']}")
    if shot.get("description"):
        parts.append(str(shot["description"]).strip())
    if shot.get("emotion"):
        parts.append(f"情绪：{shot['emotion']}")
    if shot.get("dialogue"):
        parts.append(f"台词：{str(shot['dialogue']).strip()[:80]}")
    return "；".join(parts)[:900] or "（无镜头描述）"


def _qc_prev_shot_desc(shots: list, idx: int) -> str:
    """取「上一镜」的文字描述，供图片质检做跨镜连续性比对（P1，2026-09-25）

    ⚠️ 只在**同场景**时返回：跨场景切换本就该换背景、换光位，拿上一镜去比会判出
    一堆假缺陷（与 ``keyframe.same_scene`` 同一判据，口径保持一致）。
    首镜、或上一镜不同场景 → 返回空串，``check_image`` 便完全不加连续性口径
    （零行为变更，也不会诱发模型凭「上一镜」三个字臆造缺陷）。
    """
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    if i <= 0 or i >= len(shots or []):
        return ""
    prev = shots[i - 1] if isinstance(shots[i - 1], dict) else {}
    cur = shots[i] if isinstance(shots[i], dict) else {}
    _same = False
    for k in ("location", "scene_id", "scene", "scene_name"):
        a = str(prev.get(k) or "").strip()
        b = str(cur.get(k) or "").strip()
        if a and b:
            # 2026-09-29：与场景参考图取用共用同一套归一化（全半角 / 引号 / 空白）。
            # 旧写法 `a == b` 会把「铜铃巷」vs「铜铃巷（夜）」判成换场，
            # 于是**无谓地关掉**连续性判定与上一镜锚点，同场景承接镜失去衔接约束。
            _same = (_normalize_scene_name(a) == _normalize_scene_name(b))
            break
    else:
        # 两边都没写场景信息 → 无法判定「是否换场」。宁可**不启用**连续性判定
        # （不给描述比给错描述安全：错描述会让模型把正常换场判成缺陷）。
        _same = False
    if not _same:
        return ""
    bits = []
    if prev.get("camera"):
        bits.append(f"景别 {str(prev['camera']).strip()}")
    if prev.get("description"):
        bits.append(str(prev["description"]).strip()[:200])
    return "；".join(bits)[:300]


def _qc_prev_shot_ref(shots: list, idx: int, out_dir: str) -> str:
    """取「上一镜」已入库的分镜图路径，供图片质检真正做画面级比对（P1）

    与 :func:`_qc_prev_shot_desc` 同判据（仅同场景）。**只取正式目录里已通过的图**
    （``out_dir/shot_NN.png``）—— 不达标的图留在暂存区，拿它当基准会把缺陷传播下去。
    首镜 / 跨场景 / 上一镜尚未入库 → 返回空串。
    """
    if not _qc_prev_shot_desc(shots, idx):
        return ""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    prev = shots[i - 1] if isinstance(shots[i - 1], dict) else {}
    try:
        pseq = _shot_seq(prev.get("shot_id"), i)
    except Exception:  # noqa: BLE001
        return ""
    p = os.path.join(out_dir, f"shot_{pseq:02d}.png")
    return p if os.path.isfile(p) else ""


def _qc_ref_images(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                   fallback_refs: list = None) -> list:
    """为「图片质检」收集**本镜出现**的角色 / 物品 / 场景设定图 → [(label, path)]。

    为什么要单独收集，而不直接复用生成侧的 `_allocate_storyboard_refs`：
    生成侧只有 3 个参考图槽位（主角色 / 次要 / 场景），最多带 3 张；而质检的目的是
    **逐个核对画面里的每个角色、每件物品有没有变形、是否与设定一致**，所以按
    `shot.characters_in_shot` / `shot.items_in_shot` 全量收集（总数上限
    `qc_client.MAX_REF_IMAGES`，在 check_image 内还会按路径去重）。

    历史缺陷：分镜质检只把成品图单独送检，模型手里没有任何设定锚点，
    「这个角色长得像不像设定」「这柄剑的形制对不对」只能靠它自己猜 ——
    「角色不像设定 / 道具走形」这类问题要么被放过、要么被误判。
    """
    out, seen = [], set()

    def _add(label, path):
        if not path or path in seen:
            return
        seen.add(path)
        out.append((label, path))

    # 2026-09-29：角色 / 物品也走统一匹配。原先这里与场景**不同源** ——
    # 场景已用 _resolve_scene_entry，角色 / 物品却是裸 char_idx.get(n) /
    # item_idx.get(n)：名字只因标点或后缀差一点，质检就**拿不到该资产的设定图**，
    # 「这个角色像不像设定」只能靠模型凭记忆猜（正是本函数注释里说的历史缺陷）。
    for n in _match_shot_chars(shot, char_idx):
        _add(f"角色「{n}」的外貌、服装与发型", (char_idx.get(n) or {}).get("image"))
    for n in _resolve_item_names(shot, item_idx, "图片质检"):
        _add(f"物品「{n}」的形状、材质与配色", (item_idx.get(n) or {}).get("image"))
    loc, _scene_e = _resolve_scene_entry(shot, scene_idx, "图片质检")
    if _scene_e:
        # 质检口径与生成侧话术同步（2026-09-29）：场景锚点核对的是**空间结构与
        # 光照基调**，而非笼统的「环境与氛围」——与 <imageN> 场景槽位新规格一致。
        # ⚠️ 送检的锚点也必须**同机位**（与生成侧 _allocate_storyboard_refs 同一取图函数）：
        #    否则判官拿正面锚点去判一张俯拍分镜图，只会判「背景不一致」，
        #    把「机位档没生效」误报成「场景画错了」。
        _add(f"场景「{loc}」的空间结构与光照基调",
             _pick_scene_view(_scene_e, shot))
    if not out:
        # 兜底：本镜没登记角色/物品时，用生成侧实际用的那几张（至少保住场景锚点）
        # ⚠️ 2026-10-02 修复：必须**跳过构图基准图**。生成侧 refs 的第 1 项可能是
        #    `(_blocking_ref_path, _BLOCKING_REF_MARK, ...)`（见上方 refs.insert(0, ...)，
        #    标签现为常量 _BLOCKING_REF_MARK = "3D导演台构图基准"）——
        #    那是**无面人偶预演图**，实测把它当设定图送检会严重污染判定
        #    （同图 score 88 → 35，且诱发臆造缺陷，见 qc_client.check_image 的定论）。
        #    ⭐ 下方过滤判据 `"构图基准" in _kind/_label` 对新旧标签（"3D构图基准"/
        #    "3D导演台构图基准"）都命中，无需随标签改动。
        #    原先兜底无差别收下 refs，等于从「质检输入」这个后门把基准图放了回去。
        for r in (fallback_refs or []):
            if not (isinstance(r, (list, tuple)) and len(r) >= 3):
                continue
            _kind, _label = str(r[0] or ""), str(r[1] or "")
            if "构图基准" in _kind or "构图基准" in _label:
                continue
            _add(_label, r[2])
    return out[:qc_client.MAX_REF_IMAGES]


def _keyframe_qc_verifier(project_name: str, script: dict = None):
    """尾帧质检回调（供 keyframe.generate_keyframes 的 verify_cb 注入）

    返回 (verify_cb, max_retries)；质检未开启或不可用时返回 (None, 0)，
    此时尾帧链路与旧行为完全一致（不做任何质检）。

    背景：尾帧此前**完全不经过质检**（只有分镜图走），而链式模式下尾帧会直接
    成为下一镜的首帧，一张坏图会顺着链污染后面所有镜——必须拦在源头。
    """
    try:
        cfg = _qc_load_cfg()
    except Exception:  # noqa: BLE001
        return None, 0
    if not (cfg.get("enabled") and cfg.get("image_enabled")
            and cfg.get("keyframe_qc_enabled", True)):
        return None, 0
    if not qc_client.image_qc_ready(cfg):
        return None, 0

    # 尾帧同样要核对「角色/物品有没有变形、是否与设定一致」：尾帧在链式模式下
    # 会直接成为下一镜的首帧，一张走形的尾帧会顺着链污染后面所有镜。
    # 这里按剧本预建一次资产索引（只建一次，逐镜复用）。
    _kf_idx = None
    try:
        if script:
            _kf_idx = (
                _build_asset_index(script.get("characters") or [], project_name, "character"),
                _build_asset_index(script.get("items") or [], project_name, "item"),
                _build_asset_index(script.get("scenes") or [], project_name, "scene"),
            )
    except Exception as _e:  # noqa: BLE001
        app.logger.warning(f"尾帧质检构建资产索引失败（本轮不带设定图）：{_e}")
        _kf_idx = None

    def _verify(path: str, shot: dict, item: dict):
        desc = (_qc_shot_desc(shot)
                + f"；本图是该镜的「尾帧」（动作结束瞬间），"
                  f"须与首帧保持同一人物、同一服装、同一场景与同一画风"
                + ("，且须承接上一镜尾帧的画面" if item.get("chained") else ""))
        verdict = qc_client.check_image(path, desc, cfg, style=(shot.get("style") or ""),
                                        ref_images=(_qc_ref_images(shot, *_kf_idx) if _kf_idx else None))
        gate = _qc_gate(verdict)
        accepted = bool(gate.get("accept"))
        try:
            # A-17（P2-6）：尾帧质检结论**达标 / 不达标都落盘**一条 keyframe 质检记录 ——
            # 旧实现只在 `if not accepted` 里写记录，达标时无痕，用户无从确认「这集尾帧
            # 到底查没查」。这里两种结果都产生一条记录（record 内自带 ok/passed/accepted
            # 区分口径）；只有**不达标**才进一步沉淀教训（lesson 供下次重试改写提示词）。
            rec = _qc_record_verdict(
                project_name, "keyframe",
                f"shot_{item.get('seq') or item.get('shot_id')}", "尾帧质检",
                1, None, path, verdict,
                extra={"qc_outcome": "pass" if accepted else "fail"},
                style=(shot.get("style") or ""))
            if not accepted:
                # 尾帧质检不达标 → 沉淀 kind="keyframe" 教训，供下次重试 recall_cb 改写提示词。
                # 提示词键用 preflight 自愈**之前**的确定性串（build_end_frame_prompt），与
                # keyframe.generate_keyframes 里的 orig_prompt 同键，保证 phash 稳定。
                orig_prompt = keyframe.build_end_frame_prompt(
                    shot, chained=bool(item.get("chained")))
                _record_qc_lesson(project_name, "keyframe", orig_prompt, rec)
        except Exception as e:  # noqa: BLE001 - 落盘/沉淀失败绝不影响质检结论
            app.logger.warning(f"尾帧质检记录/教训沉淀失败（忽略）：{e}")
        # P1-18：返回 4 元组（ok, reason, unavailable, critical_issues）——
        #  · verdict.ok=False 表示质检「不可判定」（接口 5xx / ffmpeg 缺失等，与内容无关），
        #    由 keyframe 侧据此 **不重试**并标记 qc_unavailable（口径与「不达标」分开）；
        #  · critical_issues（A-18）：本镜判定为致命的缺陷清单（gate 独立复核命中的关键
        #    缺陷 ∪ 上游显式 critical_issues），keyframe 侧写入 r["qc"] 供 G1 止损
        #    （_qc_retry_hopeless）比对「连续 N 次缺陷相同」。旧 3 元组下 keyframe 侧
        #    r["qc"]["critical_issues"] 恒空 → 止损永不判无望（纯换 seed 瞎撞）。
        return (accepted, gate.get("reason") or "",
                not bool(verdict.get("ok")),
                list(gate.get("critical_issues") or []))

    return _verify, min(2, int(cfg.get("max_retries") or 0))


def _keyframe_prompt_preflight(project_name: str):
    """尾帧「生成前提示词预检」回调构建器（与 ``_keyframe_qc_verifier`` 同一注入风格）

    返回 ``(preflight_cb, enabled)``；``preflight_cb(prompt, shot, item) -> dict`` 直接返回
    ``prompt_qc.preflight`` 的结果（含自愈后的提示词），由
    ``keyframe.generate_keyframes`` 取其中的 ``prompt`` 去出图。

    ⚠️ 与尾帧质检（生成后、依赖质检接口）不同：预检是**纯确定性**的，所以只看
    ``prompt_enabled`` —— 质检接口没配好时它照样能拦住「提示词为空」「缺锚定参考图语义」
    这类必然废图的输入。这正是预检比事后质检便宜、且能兜住事后质检的原因。
    """
    try:
        cfg = _qc_load_cfg()
    except Exception:  # noqa: BLE001
        return None, False
    if not prompt_qc.prompt_qc_ready(cfg):
        return None, False

    # 项目级风格只解析一次：预检要按镜头逐个跑，不能在闭包里反复读盘
    _proj_style = ""
    try:
        _proj_style = _qc_style_of(project_name) or ""
    except Exception:  # noqa: BLE001
        _proj_style = ""

    def _pre(prompt: str, shot: dict, item: dict) -> dict:
        shot = shot or {}
        # 风格与生成端对齐：build_end_frame_prompt 只读 shot["style"]。镜头没带风格时退回
        # 项目当前风格 —— 这样预检能把「风格缺失」判出来并自愈补上，而不是直接放过。
        style = shot.get("style") or _proj_style
        ctx = dict(shot)
        ctx["chained"] = bool(item.get("chained"))
        # prompt 召回：预检前先叠加 kind="prompt" 历史教训。orig_prompt 是自愈**前**的
        # 原始串（也不含召回叠加块），作为下方沉淀的稳定 phash 键，避免指纹漂移。
        orig_prompt = prompt or ""
        try:
            prompt = prompt_memory.learned_prompt(
                kind="prompt", prompt=orig_prompt, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=style)
        except Exception as e:  # noqa: BLE001 - 召回失败不影响预检
            app.logger.warning(f"尾帧提示词召回失败（忽略）：{e}")
        pf = prompt_qc.preflight("keyframe", prompt, ctx=ctx, style=style, cfg=cfg)
        gate = prompt_qc.prompt_qc_gate(pf, cfg)
        _qc_record(project_name, "prompt",
                   f"shot_{item.get('seq') or item.get('shot_id')}",
                   {"stage": "keyframe",
                    "label": pf.get("label"),
                    "accept": bool(pf.get("accept")),
                    "repairs": list(pf.get("repairs") or []),
                    "rebuild_hint": pf.get("rebuild_hint") or "",
                    "verdict": pf.get("verdict") or {},
                    "prompt": pf.get("prompt") or ""})
        # prompt 沉淀：预检不通过或有缺陷时落 kind="prompt"（键用自愈前原始串）。
        if not gate.get("accept") or (pf.get("verdict") or {}).get("issues"):
            try:
                _record_preflight_lesson(project_name, orig_prompt, pf, gate)
            except Exception as e:  # noqa: BLE001 - 沉淀失败不影响预检
                app.logger.warning(f"尾帧提示词教训沉淀失败（忽略）：{e}")
        return pf

    return _pre, True


def _qc_history_file_for(project_name: str, kind: str, shot_id) -> str:
    """推算某条质检历史的落盘路径（与 qc_client.history_path 同口径）。

    用途：产物被移入回收站后，把该历史里指向已删路径的 `file` 记为 null（断链修正），
    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据。
    """
    try:
        return qc_client.history_path(QC_DIR, project_name, kind, shot_id)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检历史路径推算失败（忽略）：{e}")
        return ""


def _qc_summary(attempts: list, enabled: bool, ready: bool, max_retries: int) -> dict:
    """汇总一次资产生成的全部质检尝试，供前端展示徽标 / 明细 / 重试次数"""
    if not enabled:
        return {"enabled": False, "status": "disabled", "label": "质检未开启",
                "attempts": 0, "max_retries": max_retries}
    if not ready:
        return {"enabled": True, "status": "unconfigured", "label": "质检未配置（未启用）",
                "attempts": 0, "max_retries": max_retries}
    passed_rec = next((a for a in attempts if a.get("passed")), None)
    last = attempts[-1] if attempts else {}
    # P0-2：接口级故障放行——最后一次尝试是 interface_fault（鉴权/超时/网络）且无客观层
    # 致命缺陷 → 资产已被 fail-open 写入，不算「error/不达标」，单独一档 fault_open。
    fault_open = bool(last.get("interface_fault")) and not passed_rec and \
        not (last.get("critical_issues") or [])
    if fault_open:
        status = "fault_open"
        blocked = False
    else:
        status = "passed" if passed_rec else ("error" if last.get("error") else "failed")
        blocked = status in ("failed", "error")
    crit = list((passed_rec or last).get("critical_issues") or [])
    # ★ 重试止损（_qc_retry_hopeless）：未通过且已提前停止重试时，把原因写进 label，
    #   否则用户只看到「质检不达标」，不知道系统其实已经主动止损（没在继续烧 GPU）。
    retry_stopped = bool((last or {}).get("retry_stopped")) and not passed_rec
    _label = {"passed": "质检达标", "failed": "质检不达标", "error": "质检调用异常",
              "fault_open": "质检接口故障·已放行"}.get(status, status)
    if retry_stopped:
        _label = "质检不达标（已停止重试：连续两次缺陷完全相同）"
    return {
        "enabled": True,
        "status": status,
        "label": _label,
        "fault_open": fault_open,
        "retry_stopped": retry_stopped,
        "retry_stopped_detail": (last or {}).get("retry_stopped_features") or "",
        "passed": bool(passed_rec),
        "blocked": blocked,
        "entry_blocked": blocked,
        "asset_written": (not blocked),
        "critical_issues": crit,
        "score": (passed_rec or last).get("score"),
        "reason": (passed_rec or last).get("reason") or (last.get("error") or ""),
        "issues": (passed_rec or last).get("issues") or [],
        "attempts": len(attempts),
        "regenerated": max(0, len(attempts) - 1),
        "max_retries": max_retries,
        "history": attempts,
        "history_file": last.get("history_file") or "",
    }


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


def _salvage_episode_script(path: str, episode_no: int):
    """任务报错后检查剧本产物是否其实可用（**以产物为准**，避免误报失败）

    2026-09-17 E2E 实测：分集任务报「模型未返回有效分镜…未知错误」，
    但 `第1集.json` 其实已落盘、6 镜有效、覆盖率 100%、也已被 /api/episodes 收录 ——
    用户看到 "失败" 会以为白跑一趟。产物存在且镜头非空时，按成功回填统计字段。

    返回可直接并入 results 的统计 dict；产物缺失/不可用则返回 None。
    """
    try:
        if not (path and os.path.isfile(path) and os.path.getsize(path) > 200):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    shots = data.get("shots") or []
    if not shots:
        return None
    meta = data.get("metadata") or {}
    try:
        stats = meta.get("episode_stats") or novel_to_script.build_episode_stats(shots)
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "shots": len(shots),
        "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
        "episode_duration_sec": data.get("episode_duration_sec") or stats.get("duration_sec"),
        "characters": len(data.get("characters") or []),
        "items": len(data.get("items") or []),
        "scenes": len(data.get("scenes") or []),
        "elapsed_sec": meta.get("elapsed_sec"),
        "warnings": meta.get("warnings") or [],
    }


def _episodes_worker(task_id: str, novel_meta: dict, chapters: list, style: str,
                     target_shots: int, overwrite: bool, project_key: str = None,
                     use_screenplay: bool = False):
    key = _novel_key(novel_meta, project_key)
    # 拍摄单元：超长章按语义边界拆成多集
# （⚠️ 2026-10-09 起单集镜数**不再设上限**：生产改为按场次渲染，详见 novel_to_script 同名注释）
    units = _episode_units_for_chapters(novel_meta, chapters)
    total = len(units)

    def report(ep_ordinal, chapter, phase, message, inner_percent):
        overall = int(((ep_ordinal - 1) + (inner_percent or 0) / 100.0) / total * 100)
        with lock:
            generation_state[task_id].update({
                "phase": phase, "progress": min(99, max(1, overall)),
                "current": ep_ordinal, "total": total,
                "current_chapter": chapter.get("title"),
                "current_episode": chapter.get("index"),
                "message": message,
            })

    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")

        results = []
        for i, unit in enumerate(units):
            ep = int(unit["episode_no"])
            chapter = unit["chapter"]
            ch_title = chapter.get("title") or f"第{ep}集"
            out_path = novel_to_script.episode_script_path(SCRIPT_DIR, key, ep)

            if os.path.isfile(out_path) and not overwrite:
                info = None
                for e in novel_to_script.list_episodes(SCRIPT_DIR, key):
                    if int(e.get("episode_no")) == ep:
                        info = e
                        break
                results.append({"episode_no": ep, "chapter_title": ch_title,
                                "status": "skipped", "path": out_path,
                                "project_key": key,
                                "message": "该集已存在，跳过（可勾选覆盖重新生成）",
                                "shots": (info or {}).get("shots", 0),
                                "shot_count": (info or {}).get("shot_count"),
                                "episode_duration_sec": (info or {}).get("episode_duration_sec")})
                report(i + 1, chapter, "skip", f"第{ep}集已存在，跳过", 100)
                continue

            report(i + 1, chapter, "chapter",
                   f"第{ep}集《{ch_title}》：准备章节正文…", 2)

            def cb(phase, cur, tot, msg, pct, _ep=ep, _ord=i + 1, _ch=chapter, _t=ch_title):
                report(_ord, _ch, phase, f"第{_ep}集《{_t}》 {msg}", pct)

            try:
                # 前置解析（2026-10-03 补链，用户口径：前置解析必须先于改写）：
                # 该章还没有 preflight（人物档案/梗概/关键事件/情绪基线）时先自动
                # 跑一次并并入项目级设定库 —— convert_chapter_with_continuity 依赖
                # 它注入防 OOC 约束。一章一集口径下章号=集号，注入键天然对齐。
                # 失败不阻塞改写（fail-open，只告警）。
                try:
                    if not chapter_preflight.load_preflight(
                            CONTINUITY_DIR, key, int(chapter.get("index") or 0)):
                        report(ep, chapter, "preflight",
                               f"第{ep}集：前置解析（人物档案/梗概/关键事件/情绪基线）…", 2)
                        _pf_seg = text[int(chapter.get("start") or 0):
                                       int(chapter.get("end") or 0)]
                        _pf_res = chapter_preflight.preflight_analyze(
                            client,
                            novel_meta.get("title") or novel_meta.get("name") or "",
                            int(chapter.get("index") or 0),
                            chapter.get("title") or "", _pf_seg)
                        chapter_preflight.save_preflight(CONTINUITY_DIR, key, _pf_res)
                        try:
                            chapter_preflight.merge_to_bible(CONTINUITY_DIR, key, _pf_res)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as _pfe:  # noqa: BLE001
                    app.logger.warning("第%s集前置解析失败（跳过注入，不阻塞）：%s", ep, _pfe)
                # 两段式生产（2026-10-03 ②）：use_screenplay=true 时以「文学剧本」为
                # 原文走改写链路 —— 合成 chapter 使 start/end 覆盖剧本全文（continuity
                # 与覆盖率校验只消费传入的 novel_text[start:end]，零内部改动即生效）。
                # 文学剧本缺失时回退章节原文并告警（fail-open，不阻塞批量）。
                _conv_text = text
                _conv_chapter = chapter
                if use_screenplay:
                    _md = novel_screenplay.load_screenplay(
                        key, int(unit.get("episode_no") or 0))
                    if _md:
                        _conv_text = _md
                        _conv_chapter = {"index": chapter.get("index"),
                                         "title": chapter.get("title"),
                                         "start": 0, "end": len(_md)}
                    else:
                        app.logger.warning(
                            "第%s集：文学剧本不存在，回退章节原文（建议先调用 "
                            "/api/novels/<id>/screenplay/generate）",
                            unit.get("episode_no"))
                # 跨集连贯性（A/B/C/D）：项目级 bible + 上集摘要卡 + 衔接契约 + state 锚点
                # + 相邻集六类校验 + 命中高危问题时的局部重写，全部由 continuity 编排
                conv = continuity.convert_chapter_with_continuity(
                    client, novel_meta, _conv_text, _conv_chapter, key, CONTINUITY_DIR,
                    style=style, target_shots=target_shots, episode_no=ep,
                    save_dir=SCRIPT_DIR, progress_cb=cb,
                )
                script = conv["script"]
                validation = conv.get("validation") or {}
                path = (conv.get("script_path")
                        or novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep, key))
                # 项目登记：把剧本及其镜头数/每集时长统计写回项目注册表
                if project_key:
                    try:
                        project_store.bind_script(
                            project_key, path, project_store.script_stats(path))
                    except Exception as be:  # noqa: BLE001
                        app.logger.warning(f"项目剧本登记失败（{project_key}）：{be}")
                # 剧本体检：兜底镜头（dialogue=[] 且 prompt_h3=""）会在配音环节变成
                # 「一句都合不出来」，但这里看起来是「生成成功」，必须把缺口显式带出
                _audit = dialogue_utils.audit_script(script)
                _meta_warnings = list(script["metadata"].get("warnings") or [])
                if _audit["warnings"]:
                    _meta_warnings.extend(_audit["warnings"])
                    app.logger.warning(f"第{ep}集剧本存在内容缺口：{_audit['warnings']}")
                # P0-3 剧本↔原著一致性（三件套 + 定向修复）结果，随生成结果带出给前端
                _sc = script["metadata"].get("script_consistency") or {}
                results.append({
                    "episode_no": ep, "chapter_title": ch_title, "status": "success",
                    "path": path, "project_name": script["metadata"]["project_name"],
                    "project_key": key,
                    "shots": len(script.get("shots") or []),
                    "shot_count": script.get("shot_count") or len(script.get("shots") or []),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                    "characters": len(script.get("characters") or []),
                    "items": len(script.get("items") or []),
                    "scenes": len(script.get("scenes") or []),
                    "chunks_total": script["metadata"].get("chunks_total"),
                    "chunks_used": script["metadata"].get("chunks_used"),
                    "elapsed_sec": script["metadata"].get("elapsed_sec"),
                    "warnings": _meta_warnings,
                    "script_audit": _audit["stats"],
                    "script_audit_ok": _audit["ok"],
                    "continuity_score": (script["metadata"].get("continuity") or {}).get("validation_score"),
                    "continuity_issues": len(validation.get("issues") or []),
                    "continuity_issue_stats": validation.get("issue_stats") or {},
                    "continuity_rewrite": bool((conv.get("rewrite") or {}).get("triggered")),
                    "continuity_rewrite_shots": (conv.get("rewrite") or {}).get("rewritten_shot_ids") or [],
                    "continuity_dir": continuity.continuity_root(CONTINUITY_DIR, key),
                    "coverage_percent": (conv.get("coverage") or {}).get("coverage_percent"),
                    "coverage_plot_percent": (conv.get("coverage") or {}).get("plot_coverage_percent"),
                    "coverage_detail_percent": (conv.get("coverage") or {}).get("detail_coverage_percent"),
                    "coverage_detail_passed": (conv.get("coverage") or {}).get("detail_passed"),
                    "coverage_passed": (conv.get("coverage") or {}).get("passed"),
                    "coverage_threshold_percent": (conv.get("coverage") or {}).get("threshold_percent"),
                    "coverage_missing": (conv.get("coverage") or {}).get("missing_count"),
                    "coverage_zero_omission": (conv.get("coverage") or {}).get("zero_omission"),
                    "coverage_supplement_shots": (conv.get("coverage") or {}).get("supplement_shots"),
                    "coverage_supplement_rounds": (conv.get("coverage") or {}).get("supplement_rounds"),
                    "coverage_report_path": (conv.get("coverage") or {}).get("report_path"),
                    # P0-3 剧本↔原著一致性：三件套 + 定向修复闭环
                    "consistency_passed": _sc.get("passed"),
                    "consistency_chapter_index": _sc.get("chapter_index"),
                    "consistency_anchor_checked": _sc.get("anchor_checked"),
                    "consistency_anchor_ok": _sc.get("anchor_ok"),
                    "consistency_anchor_deviation": _sc.get("anchor_deviation"),
                    "consistency_anchor_reason": _sc.get("anchor_reason"),
                    "consistency_leak_count": _sc.get("leak_count"),
                    "consistency_leak_shot_ids": _sc.get("leak_shot_ids") or [],
                    "consistency_element_percent": _sc.get("element_coverage_percent"),
                    "consistency_element_missing_count": _sc.get("element_missing_count"),
                    "consistency_element_missing": [e.get("name") for e in (_sc.get("element_missing") or [])],
                    "consistency_fix_rounds": _sc.get("fix_rounds"),
                    "consistency_fixed": _sc.get("fixed"),
                    "consistency_issue_count": _sc.get("issue_count"),
                    "consistency_issue_stats": _sc.get("issue_stats") or {},
                    "consistency_report_path": _sc.get("report_path"),
                    "message": "生成完成",
                })
            except Exception as e:  # noqa: BLE001
                app.logger.error(f"第{ep}集生成失败: {e}")
                # 以产物为准：模型抖动/校验失败时报错，但剧本可能已经落盘且可用
                salvaged = _salvage_episode_script(out_path, ep)
                if salvaged:
                    app.logger.warning(
                        f"第{ep}集虽报错但剧本产物可用，已按成功回填：{out_path}")
                    results.append({
                        "episode_no": ep, "chapter_title": ch_title,
                        "status": "success", "degraded": True,
                        "path": out_path, "project_key": key, **salvaged,
                        "message": f"生成过程报错，但剧本已落盘且可用（已按产物回填为成功）：{e}",
                        "error": str(e),
                    })
                else:
                    results.append({"episode_no": ep, "chapter_title": ch_title,
                                    "status": "failed", "path": out_path,
                                    "message": str(e)})

        ok = [r for r in results if r["status"] == "success"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]
        degraded = [r for r in ok if r.get("degraded")]
        episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok or skipped else "failed",
                "progress": 100, "current": total, "total": total,
                "message": (f"批量完成：成功 {len(ok)} 集"
                            + (f"（其中 {len(degraded)} 集过程报错但产物可用）" if degraded else "")
                            + f" / 跳过 {len(skipped)} 集 / 失败 {len(failed)} 集"),
                "degraded_count": len(degraded),
                "error": "" if (ok or skipped) else (failed[0]["message"] if failed else "全部失败"),
                "results": results, "episodes": episodes,
                "novel_id": novel_meta.get("novel_id"),
                "project_key": key,
                "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
            })
    except (LLMError, NovelParseError) as e:
        app.logger.error(f"分集生成失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("分集生成异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分集生成异常：{e}"})


# ===================== 文学剧本层（两段式生产 ①：人审层，2026-10-03） =====================

def _screenplay_worker(task_id: str, novel_meta: dict, chapter: dict,
                       project_key: str, style: str, episode_no: int):
    """文学剧本生成 worker：章节正文 → LLM 场次剧本 → output/screenplays/<项目>/第N集_文学剧本.md"""
    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        seg = text[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
        with lock:
            generation_state[task_id].update({"phase": "literary", "progress": 15,
                                              "message": "正在把本章正文改写成文学剧本…"})
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")
        md = novel_screenplay.generate_screenplay(
            client, novel_meta.get("title") or novel_meta.get("name") or "",
            chapter.get("title") or f"第{episode_no}集", seg, style)
        with lock:
            generation_state[task_id].update({"progress": 80, "message": "落盘…"})
        path = novel_screenplay.save_screenplay(
            novel_screenplay.screenplay_path(project_key, episode_no), md)
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "message": "文学剧本已生成（可在前端查看，确认后再改写为分镜剧本）",
                "screenplay_path": path,
                "results": [{"success": True, "episode_no": episode_no,
                             "path": path, "chars": len(md)}]})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("文学剧本生成失败")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})


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
    target_shots = max(4, min(int(data.get('target_shots') or NOVEL_DEFAULT_SHOTS), 40))
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

dub_tasks = {}
dub_lock = threading.Lock()


def _dub_resolve_script(data: dict) -> dict:
    """解析配音所用剧本：优先 body.script，其次 script_path（限项目输出目录内），最后自动匹配"""
    script = data.get("script")
    if isinstance(script, dict) and script.get("shots"):
        return {"script": script, "script_path": (data.get("script_path") or "").strip(),
                "source": "body"}

    script_path = (data.get("script_path") or "").strip()
    if script_path:
        # P0-4：与 project_store.bind_script / /api/final/video 同一校验函数
        if not project_store.is_path_inside_output(script_path):
            raise TTSError(f"剧本路径必须在项目输出目录内：{os.path.abspath(PROJECT_OUTPUT_DIR)}")
        p = os.path.abspath(script_path)
        if not os.path.exists(p):
            raise TTSError(f"剧本文件不存在：{p}")
        with open(p, "r", encoding="utf-8") as f:
            return {"script": json.load(f), "script_path": p, "source": "path"}

    # 自动匹配 output/scripts 下的剧本（优先路径含项目名，其次 episode_no 命中，最后取最新）
    project_name = data.get("project_name") or ""
    episode = data.get("episode")
    cands = []
    for dp, _dn, fn in os.walk(SCRIPT_DIR):
        for name in fn:
            if not name.lower().endswith(".json"):
                continue
            p = os.path.join(dp, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    s = json.load(f)
            except Exception:
                continue
            if not isinstance(s, dict) or not s.get("shots"):
                continue
            cands.append({"path": p, "mtime": os.path.getmtime(p), "script": s,
                          "episode_no": s.get("episode_no") or (s.get("metadata") or {}).get("episode_no")})
    if not cands:
        raise TTSError("未找到可用剧本（output/scripts 下无含 shots 的 JSON），请先生成剧本")

    # 严格匹配：有 project_name 时必须属于该项目，禁止跨项目回退
    if project_name:
        project_cands = [c for c in cands if project_name in c["path"]]
        if not project_cands:
            raise TTSError(f"项目 '{project_name}' 暂无剧本，请先生成剧本后再使用 TTS 功能")
        hit = project_cands
    else:
        hit = cands

    if episode:
        hit2 = [c for c in hit if str(c["episode_no"]) == str(episode)]
        if hit2:
            hit = hit2
    best = max(hit, key=lambda c: c["mtime"])
    return {"script": best["script"], "script_path": best["path"], "source": "auto"}


# =====================================================================
# 音频质检接线（提示词预检 + 成品质检）
# =====================================================================
# 两层都在「生成前后」各管一段，与图片/视频质检的三层结构（预检 → 成品质检 → 重试）对齐：
#   ① 配音台词预检（零模型依赖，默认开启）：挡住会被念出来的结构化残留、空台词、错配音色；
#   ② 配音成品质检（ffmpeg 客观层 + 频谱/波形 AI 层）：挡住「合成成功但整段无声」这类
#      在旧流程里要等到成片验收才暴露的问题。
# 两者都**不阻断生成**：整集生产不能被单句质检拖死，结论如实记录、逐句可定位即可。

def _record_audio_qc_lesson(project_name: str, ln: dict, verdict: dict) -> dict:
    """把一句「配音成品质检不达标」沉淀成 ``kind="audio"`` 教训。

    提示词键用**自愈前**的台词原文（``audio_orig_text``，回退当前 ``ln["text"]``）：
    它正是 TTS 的实际输入，phash 稳定；预检已自愈过 text 时取自愈前的原文，避免指纹漂移。
    ⚠️ 沉淀的 issues **只进教训库，绝不改台词**（音频类召回是计划级纠偏，见 _apply_audio_hints）。
    """
    text_key = ln.get("audio_orig_text") or ln.get("text") or ""
    if not text_key:
        return {}
    verdict = verdict if isinstance(verdict, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in
                   (list(verdict.get("issues") or []) +
                    list(verdict.get("critical_issues") or [])) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(verdict.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "audio": True,
    }
    if not rec["issues"] and not rec["reason"]:
        return {}
    try:
        return _record_qc_lesson(project_name, "audio", text_key, rec)
    except Exception as e:  # noqa: BLE001 - 沉淀失败绝不影响配音
        app.logger.warning(f"配音教训沉淀失败（忽略）：{e}")
        return {}


def _dub_line_speaker_from_script(ln: dict, project_name: str) -> str:
    """从剧本里找该句所属镜头登记的 speaker（dialogue[].speaker / shot.speaker）。

    取不到返回空串（调用方不做回填）。纯只读，永不抛异常。
    """
    shot_id = ln.get("shot_id")
    if not project_name or shot_id is None:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
    except Exception:  # noqa: BLE001
        return ""
    sid_str = str(shot_id)
    text = str(ln.get("text") or "").strip()
    shots = []
    for sc in (script.get("scenes") or []):
        shots.extend(sc.get("shots") or [])
    if not shots:
        shots = script.get("shots") or []
    for shot in shots:
        if str(shot.get("shot_id") or "") != sid_str:
            continue
        dlg_speaker = ""
        for row in (shot.get("dialogue") or []):
            if str(row.get("text") or "").strip() == text:
                dlg_speaker = str(row.get("speaker") or "").strip()
                if dlg_speaker:
                    break
        return dlg_speaker or str(shot.get("speaker") or "").strip()
    return ""


def _dub_character_desc(character: str, project_name: str) -> str:
    """取角色音色底稿描述（供 design 模式 instruct）；取不到返回空串。"""
    if not character or not project_name:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
        for ch in (script.get("characters") or []):
            if str(ch.get("name") or "") == str(character):
                return str(ch.get("description") or ch.get("tts_voice") or "")
    except Exception as e:  # noqa: BLE001
        app.logger.debug("角色描述取值失败（忽略）：%s", e)
    return ""


def _apply_audio_hints(ln: dict, hints: list, project_name: str = "") -> None:
    """音频类召回的**计划级纠偏**（设计 D4：音频建议绝不拼进 ``ln["text"]``，会被 TTS 念出来）。

    逐条扫描 hints（缺陷描述），按特征做确定性纠偏，只动 plan 的说话人/音色模式/期望时长：
      - 含「旁白」「speaker」「角色」：若本句说话人是旁白兜底（剧本 dialogue 没登记 speaker），
        且能拿到该镜在剧本里登记的 speaker，则回填 ``ln["character"]``，避免角色台词被旁白念；
      - 含「情绪」「语气」「instruct」：``voice.mode == "preset"`` 时切到 ``design``，
        并确保 ``instruct`` 携带该句情绪（preset 的 CustomVoice 会忽略 instruct，只有
        VoiceDesign 真正按 instruct 控制语气）；
      - 含「时长」「截断」：记录 ``ln["audio_expect_sec"]``（期望时长）供后续质检比对，不阻断；
      - 其它：仅留痕（hints 由调用方写入 ``ln["audio_hints"]`` 审计），不改 plan。

    纯就地修改、永不抛异常、不改 tts_client.py（build_dub_plan 保持纯计划构建）。
    """
    hints = [str(h).strip() for h in (hints or []) if str(h).strip()]
    if not hints:
        return
    try:
        joined = " ".join(hints)
        voice = ln.get("voice") or {}
        # —— 说话人回填：旁白兜底 + hint 提示该句其实是角色台词 → 按剧本登记的 speaker 纠偏 ——
        if (("旁白" in joined or "speaker" in joined or "角色" in joined)
                and str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER):
            speaker = ""
            try:
                speaker = _dub_line_speaker_from_script(ln, project_name)
            except Exception:  # noqa: BLE001
                speaker = ""
            if speaker and speaker != tts_client.NARRATION_SPEAKER:
                ln["character"] = speaker
        # —— 情绪/语气：preset 忽略 instruct → 切 design 并携带情绪 ——
        if ("情绪" in joined or "语气" in joined or "instruct" in joined.lower()):
            emotion = str(ln.get("emotion") or "").strip()
            if emotion and not tts_client._is_neutral_emotion(emotion):
                desc = ""
                try:
                    desc = _dub_character_desc(ln.get("character"), project_name)
                except Exception:  # noqa: BLE001
                    desc = ""
                voice = dict(voice, mode="design",
                             instruct=tts_client._emotion_instruct(emotion, desc))
                ln["voice"] = voice
        # —— 时长/截断：记录期望时长供质检比对（不阻断）——
        if "时长" in joined or "截断" in joined:
            expect = _audio_line_expect_sec(ln)
            if expect > 0:
                ln["audio_expect_sec"] = round(float(expect), 2)
    except Exception as e:  # noqa: BLE001 - 纠偏失败绝不影响配音
        app.logger.warning(f"配音教训纠偏失败（忽略）：{e}")


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


def _audio_line_expect_sec(line: dict) -> float:
    """该句配音的期望时长（由台词字数推算；推算不出时退回镜头时长）

    只用于「时长偏差」这一条软判据，因此宁松勿紧：优先用字数推算（能发现「被截断」），
    推算不出（空台词）时才退回剧本给的镜头时长，避免拿 0 当期望值把一切都判成偏差。
    """
    est = audio_qc.estimate_speech_sec(line.get("text"))
    if est > 0:
        return est
    try:
        return max(0.0, float(line.get("duration_hint") or 0))
    except (TypeError, ValueError):
        return 0.0


def _dub_prompt_preflight(lines: list, project_name: str = "") -> dict:
    """配音台词生成前预检：就地自愈 ``lines[i]["text"]``，结论写入 ``lines[i]["prompt_qc"]``。

    永不抛异常（预检是保险，保险本身出问题不能耽误配音）。
    """
    stats = {"enabled": False, "checked": 0, "repaired": 0, "blocked": 0,
             "issue_lines": 0, "repaired_lines": 0, "problem_lines": []}
    if not lines:
        return stats
    try:
        cfg = _qc_load_cfg()
        if not prompt_qc.prompt_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        mode = prompt_qc.prompt_qc_mode(cfg)
        for ln in lines:
            text = ln.get("text") or ""
            ctx = {
                "project_name": project_name,
                "shot_id": ln.get("shot_id"),
                "character": ln.get("character"),
                "emotion": ln.get("emotion"),
                # preset（CustomVoice）会忽略 instruct → 情绪送不进 TTS，预检据此提示
                "voice_mode": (ln.get("voice") or {}).get("mode"),
                # 剧本没写 speaker（或写了未登记角色）时 build_dub_plan 落到「旁白」音色，
                # 角色台词会被旁白念 —— 用「最终音色是不是旁白兜底」判定，而不是旧写法
                # `source == "narration"`（旁白通道关闭后 source 恒为 dialogue，那个判据永远为假，
                # 等于这条预检规则静默失效）。
                "speaker_fallback": str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER,
            }
            pf = prompt_qc.preflight("audio", text, ctx=ctx, cfg=cfg)
            verdict = pf.get("verdict") or {}
            stats["checked"] += 1
            if pf.get("repairs"):
                stats["repaired"] += 1
            if verdict.get("issues"):
                stats["issue_lines"] += 1
            # ⚠️ 自愈结果为空时**保留原文**：把台词改成空串会让该句直接合成失败/静音，
            #    比「带一点噪音」更糟。空台词交给调用方按 rebuild_hint 从剧本重建。
            new_text = pf.get("prompt") or ""
            if new_text and new_text != text:
                ln["text"] = new_text
                stats["repaired_lines"] += 1
            ln["prompt_qc"] = {
                "mode": mode,
                "passed": bool(verdict.get("passed")),
                "blocked": bool(pf.get("blocked")),
                "score": verdict.get("score"),
                "issues": list(verdict.get("issues") or []),
                "critical_issues": list(verdict.get("critical_issues") or []),
                "repairs": list(pf.get("repairs") or []),
                "label": pf.get("label") or "",
                "reason": pf.get("reason") or "",
                "rebuild_hint": pf.get("rebuild_hint") or "",
            }
            if pf.get("blocked") or verdict.get("issues"):
                if pf.get("blocked"):
                    stats["blocked"] += 1
                if len(stats["problem_lines"]) < 20:
                    stats["problem_lines"].append({
                        "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
                        "character": ln.get("character"),
                        "blocked": bool(pf.get("blocked")),
                        "issues": list(verdict.get("issues") or [])[:3]
                                  + list(verdict.get("critical_issues") or [])[:2],
                        "repairs": list(pf.get("repairs") or []),
                        "rebuild_hint": pf.get("rebuild_hint") or "",
                    })
                # 配音台词的预检缺陷同样沉淀为「提示词质检」教训（此前只进 stats 与
                # ln["prompt_qc"]，不进教训库）；键用自愈前的原文 text。
                if project_name:
                    try:
                        _record_preflight_lesson(project_name, text, pf,
                                                 prompt_qc.prompt_qc_gate(pf, cfg))
                    except Exception as _dub_lesson_err:  # noqa: BLE001
                        app.logger.warning("配音提示词教训沉淀失败（忽略）：%s",
                                           _dub_lesson_err)
    except Exception as e:  # noqa: BLE001 - 预检失败绝不影响配音
        app.logger.warning(f"配音台词预检异常（已跳过，不影响配音）：{e}")
    return stats


def _audio_qc_lines(project_name: str, lines: list, results: list, cfg: dict,
                    retry_cb=None) -> dict:
    """配音成品逐句质检（客观层 + AI 层），结论写入 ``results[i]["audio_qc"]``。

    ``retry_cb(line, result) -> dict|None``：可选的重配合回调。**只对客观层判致命的句子
    调用**（整段无声/空文件）—— 这类失败属于「合成出了东西但不是人声」，重配一次是最有效
    的补救；软扣分项（音量偏小、时长偏差）不重配，交由用户决定。

    永不抛异常；批量口径为「记录 + 有限重配」，不阻断整集。
    """
    stats = {"enabled": False, "checked": 0, "passed": 0, "failed": 0, "blocked": 0,
             "ai_used": 0, "retried": 0, "recovered": 0, "problems": []}
    if not results:
        return stats
    try:
        if not qc_client.audio_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        by_id = {str(l.get("line_id")): l for l in (lines or [])}
        visuals_root = os.path.join(QC_DIR, "audio", _safe_project(project_name or "project"))
        for r in results:
            if not r.get("ok") or not r.get("out_path"):
                continue
            ln = by_id.get(str(r.get("line_id"))) or {}
            expect = _audio_line_expect_sec(ln)
            verdict = qc_client.check_audio(
                r["out_path"], expect_sec=expect or None,
                line_text=ln.get("text") or r.get("text") or "", cfg=cfg,
                visuals_dir=os.path.join(visuals_root,
                                         os.path.splitext(os.path.basename(r["out_path"]))[0]))
            stats["checked"] += 1
            # 致命（整段无声/空文件）→ 重配一次。⚠️ 计数必须在重配之后按**最终**结论统计：
            # 先记 blocked 再重配会出现「致命 1 句 / 未通过 0 句」这种自相矛盾的汇总，
            # 前端与任务消息都在读这两个数，口径必须一致。
            if verdict.get("blocked") and retry_cb is not None:
                try:
                    stats["retried"] += 1
                    again = retry_cb(ln, r)
                    if again:
                        verdict = again
                        if not verdict.get("blocked"):
                            stats["recovered"] += 1
                except Exception as e:  # noqa: BLE001 - 重配失败不影响已有结论
                    app.logger.warning(f"音频质检重配失败（{r.get('line_id')}）：{e}")
            if verdict.get("blocked"):
                stats["blocked"] += 1
            if verdict.get("ai_used"):
                stats["ai_used"] += 1
            if verdict.get("passed"):
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                # T03b：配音成品质检不达标 → 沉淀 kind="audio" 教训（键 = 该句 TTS 输入原文，
                # 自愈前用 audio_orig_text）。verdict.ok=false（接口异常）时 verdict 无有效缺陷，
                # 不沉淀，避免把「质检调用失败」记成「这句配音有问题」。
                if verdict.get("ok", True) and ln:
                    _record_audio_qc_lesson(project_name, ln, verdict)
                # ★ 用户需求：质检不合格的配音不留本地。⚠️ **仅在最终 failed（重配也失败）时删**：
                # `blocked`（整段无声/空文件）已由 retry_cb 重配过一次，重配若恢复则 verdict
                # 被替换、不会走到这里；能走到这里说明**最终结论仍不合格**。删除条件是
                # 「质检成功返回（ok 非 False）且最终 not passed」—— ok=False（接口故障）不删。
                # 删：该句 wav（P9）+ 其可视化目录（P10 output/qc/audio/<项目>/<stem>/）。
                if verdict.get("ok", True) and r.get("out_path"):
                    try:
                        _stem = os.path.splitext(os.path.basename(r["out_path"]))[0]
                        _purge_rejected_artifacts(
                            [r["out_path"], os.path.join(visuals_root, _stem)],
                            project=project_name,
                            reason=f"配音质检不合格（{verdict.get('reason') or ''}）"[:120],
                            kind="audio_line")
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"不合格配音清理失败（忽略）：{_pe}")
                if len(stats["problems"]) < 20:
                    stats["problems"].append({
                        "line_id": r.get("line_id"), "shot_id": r.get("shot_id"),
                        "character": r.get("character"),
                        "blocked": bool(verdict.get("blocked")),
                        "score": verdict.get("score"),
                        "reason": str(verdict.get("reason") or "")[:200],
                        "metrics": verdict.get("metrics") or {},
                        "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                                    f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                                    for p in (verdict.get("visuals") or [])],
                    })
            r["audio_qc"] = {
                "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
                "score": verdict.get("score"),
                "reason": str(verdict.get("reason") or "")[:300],
                "issues": list(verdict.get("issues") or [])[:5],
                "critical_issues": list(verdict.get("critical_issues") or [])[:3],
                "metrics": verdict.get("metrics") or {},
                "ai_used": bool(verdict.get("ai_used")),
                "ai_skipped": bool(verdict.get("ai_skipped")),
                "ai_skip_reason": verdict.get("ai_skip_reason") or "",
                "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                            f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                            for p in (verdict.get("visuals") or [])],
            }
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响配音产物
        app.logger.warning(f"配音成品质检异常（已跳过，不影响配音）：{e}")
    if stats["enabled"]:
        app.logger.info(f"配音质检（{project_name}）：检查 {stats['checked']} 句，"
                        f"通过 {stats['passed']}，未通过 {stats['failed']}，"
                        f"致命 {stats['blocked']}，重配 {stats['retried']}，"
                        f"恢复 {stats['recovered']}，AI 层 {stats['ai_used']}")
    return stats


def _mix_audio_qc(report: dict, cfg: dict) -> dict:
    """带配音成片的音频质检（整轨口径）。

    ⚠️ 必须关掉「有声占比下限」：成片天然有大段无台词留白（无台词镜头/纯环境音），
    拿单句的 50% 标准去卡它必然误报「漏句」。整轨真正要挡的是**整条音轨近乎无声**
    （amix 失败 / 全部条目静音）与**不含音频流** —— 这两条都在客观层的硬闸里。
    """
    try:
        if not qc_client.audio_qc_ready(cfg):
            return {"enabled": False, "reason": "音频质检开关未开启"}
        out = report.get("output_path") or ""
        before = report.get("video_before") or {}
        expect = 0.0
        try:
            expect = float(before.get("duration") or 0)
        except (TypeError, ValueError):
            expect = 0.0
        project_name = _safe_project(report.get("project") or "project")
        stem = os.path.splitext(os.path.basename(out))[0]
        verdict = qc_client.check_audio(
            out, expect_sec=expect or None, cfg=cfg,
            check_speech_ratio=False,
            visuals_dir=os.path.join(QC_DIR, "audio_mix", project_name, stem))
        metrics = verdict.get("metrics") or {}
        out_v = {
            "enabled": True,
            "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
            "score": verdict.get("score"),
            "reason": str(verdict.get("reason") or "")[:300],
            "issues": list(verdict.get("issues") or [])[:5],
            "critical_issues": list(verdict.get("critical_issues") or [])[:3],
            "metrics": metrics,
            "ai_used": bool(verdict.get("ai_used")),
            "ai_skipped": bool(verdict.get("ai_skipped")),
            "ai_skip_reason": verdict.get("ai_skip_reason") or "",
            "visuals": [f"/api/qc/frames/audio_mix/{project_name}/{stem}/"
                        f"{os.path.basename(p)}" for p in (verdict.get("visuals") or [])],
            "coverage_sec": report.get("coverage_sec"),
            "video_duration": expect or None,
        }
        # 配音覆盖率：逐句音频总时长 / 视频时长。**两端都要看**：
        #   偏低（<50%）→ 大量镜头没有配音落点；
        #   偏高（>115%）→ 台词总长超过画面，末尾整段被 `-shortest` **静默截掉**
        #     （成片仍「有声音」所以客观层查不出来，但台词已经丢了一大半）。
        #   ⚠️ 曾只写「偏低」这一个方向，实测把 ep04（台词 606.4s / 画面 85.2s，
        #      21 句里 17 句落在片外）这条最该拦的缺陷直接放过了 —— 覆盖率是**比值**，
        #      单向判定等于漏掉一半语义。
        try:
            cov = float(report.get("coverage_sec") or 0)
            if expect > 0:
                ratio = cov / expect
                out_v.setdefault("issues", [])
                if ratio < 0.5:
                    out_v["issues"].append(
                        f"配音覆盖偏低：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                        f"（{ratio * 100:.0f}%）")
                elif ratio > 1.15:
                    entries = report.get("entries") or []
                    dropped = 0
                    for e in entries:
                        try:
                            if float(e.get("start") or 0) >= expect:
                                dropped += 1
                        except (TypeError, ValueError):
                            continue
                    detail = (f"，其中 {dropped}/{len(entries)} 句起始点已在片长之外、放不出来"
                              if dropped else "")
                    msg = (f"配音总长超出画面：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                           f"（{ratio * 100:.0f}%）{detail} —— 超出部分会被合成命令静默截断")
                    out_v["issues"].append(msg)
                    out_v.setdefault("critical_issues", [])
                    out_v["critical_issues"].append(msg)
                    # 单向收紧：客观层/AI 层说通过也翻不回来
                    out_v["blocked"] = True
                    out_v["passed"] = False
                    try:
                        out_v["score"] = min(int(out_v.get("score") or 0), 40)
                    except (TypeError, ValueError) as e:
                        app.logger.debug("评分字段解析失败（忽略）：%s", e)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            app.logger.debug("评分归一化计算失败（忽略）：%s", e)
        # ★ 用户需求：质检不合格的混音不留本地（P10 可视化目录）。仅当最终「质检成功返回
        # 且不合格」（ok 非 False 且 passed=False）时删；ok=False（接口故障）不删。
        if verdict.get("ok") is not False and not out_v.get("passed"):
            try:
                _purge_rejected_artifacts(
                    [os.path.join(QC_DIR, "audio_mix", project_name, stem)],
                    project=project_name,
                    reason=f"混音质检不合格（{out_v.get('reason') or ''}）"[:120],
                    kind="audio_mix")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"不合格混音清理失败（忽略）：{_pe}")
        return out_v
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响合成结果
        app.logger.warning(f"成片音频质检异常（已跳过）：{e}")
        return {"enabled": True, "passed": None, "error": f"{type(e).__name__}: {e}"}


def _dub_worker(task_id: str, project_name: str, plan: dict, out_dir: str,
                fmt: str, episode: int):
    """后台配音：批量合成逐句音频 → 合并整集音轨 → 落盘清单"""
    try:
        lines = plan.get("lines") or []
        if not lines:
            raise TTSError("配音计划为空（剧本中没有可朗读台词，或所选镜头无台词）")

        lines_dir = os.path.join(out_dir, "lines")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        for ln in lines:
            ln["project_tag"] = project_name

        # ---- ① 生成前提示词预检（配音台词）----
        # 台词会被 TTS 逐字念出来：结构化残留（`(S1) 说：[Chinese] …`）、舞台指示
        # （`（转身冷笑）`）都会原样进成片；空台词/纯标点则合成出静音却显示「成功」。
        # 这一层零模型依赖、默认开启，在消耗 GPU 之前把确定性缺陷挡住/修掉。
        qc_cfg = _qc_load_cfg()
        pre = _dub_prompt_preflight(lines, project_name)
        if pre.get("blocked") or pre.get("repaired_lines"):
            with dub_lock:
                dub_tasks[task_id].update({"prompt_qc": pre})

        def _cb(done, total, last, note):
            with dub_lock:
                dub_tasks[task_id].update({
                    "current": done, "total": total,
                    "progress": int(done / max(1, total) * 90),
                    "phase": f"配音合成中（{done}/{total}）",
                    "message": f"最新：{last.get('character') or ''} {str(last.get('text') or '')[:18]}",
                })

        results = client.synthesize_lines(lines, lines_dir, progress_cb=_cb)
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
            })

        # ---- ② 成品质检（音频客观层 + 频谱/波形 AI 层）----
        # 「合成成功」不等于「念出来了」：节点正常返回、文件也落盘，但整段可以是静音
        # （漏配音 / 模型未发声）。旧流程要等到成片验收才发现整集缺一句。
        # 这里逐句实测，致命的（整段无声/空文件）当场重配一次，软扣分项只记录。
        def _retry_line(ln, rec):
            """重配单句并重新质检（只对客观层判致命的句子调用）"""
            import copy as _copy
            one = _copy.deepcopy(ln)
            one["project_tag"] = project_name
            res = client.synthesize_lines([one], lines_dir)
            if not res or not res[0].get("ok"):
                return None
            rec.update({k: v for k, v in res[0].items() if k != "audio_qc"})
            return qc_client.check_audio(
                rec["out_path"], expect_sec=_audio_line_expect_sec(ln) or None,
                line_text=ln.get("text") or "", cfg=qc_cfg,
                visuals_dir=os.path.join(QC_DIR, "audio", _safe_project(project_name),
                                         os.path.splitext(os.path.basename(rec["out_path"]))[0]))

        if qc_cfg.get("enabled") and qc_cfg.get("audio_enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"phase": "配音质检中（客观指标 + 频谱波形送检）",
                                           "progress": 92})
        aqua = _audio_qc_lines(project_name, lines, results, qc_cfg, retry_cb=_retry_line)
        if aqua.get("enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"audio_qc": aqua})
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
                "audio_qc_failed": aqua.get("failed", 0),
            })

        # 合并整集音轨（按剧本镜头顺序）
        merged = None
        merged_probe = {}
        if ok_items:
            with dub_lock:
                dub_tasks[task_id].update({"phase": "合并整集音轨", "progress": 94})
            order = {l.get("line_id"): i for i, l in enumerate(lines)}
            ok_sorted = sorted(ok_items, key=lambda r: order.get(r.get("line_id"), 9999))
            merged_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub.{fmt}")
            merged = concat_audio([r["out_path"] for r in ok_sorted], merged_path, fmt=fmt)
            merged_probe = probe_audio_info(merged)

        manifest = {
            "project": project_name, "episode": episode,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "script_path": plan.get("script_path") or "",
            "voice_map": plan.get("voice_map") or {},
            "characters": plan.get("characters") or [],
            "merged_audio": merged,
            "merged_info": merged_probe,
            # 质检结论随清单落盘：成片验收时能回溯「这句当时是怎么判的」
            "prompt_qc": pre if pre.get("enabled") else {},
            "audio_qc": aqua if aqua.get("enabled") else {},
            "lines": [dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                      for r in results],
        }
        manifest_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」
        atomic_write_json(manifest_path, manifest)

        _msg = f"成功 {len(ok_items)} 句 / 失败 {len(results) - len(ok_items)} 句"
        if aqua.get("enabled") and aqua.get("checked"):
            _msg += f"；质检通过 {aqua['passed']}/{aqua['checked']} 句"
            if aqua.get("recovered"):
                _msg += f"（重配恢复 {aqua['recovered']} 句）"
        with dub_lock:
            dub_tasks[task_id].update({
                "status": "completed" if ok_items else "failed",
                "progress": 100, "phase": "配音完成" if ok_items else "配音失败",
                "message": _msg,
                "merged_audio": merged,
                "merged_url": _dub_audio_url(project_name, merged) if merged else "",
                "merged_info": merged_probe,
                "manifest": manifest_path,
                "error": "" if ok_items else "全部句子合成失败，请查看 results 中的错误原因",
            })
        with dub_lock:
            _prune_task_registry(dub_tasks)
    except (TTSError, OSError) as e:
        app.logger.error(f"配音任务失败: {e}")
        # B-16 P2-11：配音失败 → 清理本任务产生的中间产物（lines 目录、merged 半成品）
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), app.logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": str(e), "phase": "失败"})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("配音任务异常")
        # B-16 P2-11：配音异常 → 清理中间产物
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), app.logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": f"异常：{e}", "phase": "失败"})


# ===================== 参考音频克隆角色声线（2026-10-06） =====================
# 链路：上传参考音频 → 落 <dub>/voice_bank/<角色>/ref.<ext> → 试听确认 →
#       build_dub_plan 自动把该角色切到 clone 模式（显式模式优先，不被覆盖）。
# 开关在**前端**（音色面板）：绑定/解绑 + 试听。后端只做「存/查/删/试听」。


# =====================================================================
# 音画对齐与混音合成（配音轨 × 成片视频 → 带配音成片 output/final_dub/）
# =====================================================================

mix_tasks = {}
mix_lock = threading.Lock()


def _mix_resolve_video(data: dict, project_name: str) -> str:
    """定位待合成的成片：显式 video_path/video_url 优先，否则在项目成片目录自动匹配最新 mp4"""
    if (data.get("video_path") or "").strip() or (data.get("video_url") or "").strip():
        return _upscale_resolve_video(data)
    cands = []
    for d in project_store.project_dirs(FINAL_DIR, project_name):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".mp4"):
                p = os.path.join(d, name)
                cands.append((os.path.getmtime(p), p))
    if not cands:
        raise DubMixError(
            "未找到成片视频：请先在步骤6完成成片合成，或显式提供 video_path / video_url")
    cands.sort(reverse=True)
    return cands[0][1]


def _mix_segments_dir(project_name: str, episode: int = 0) -> str:
    """定位该集（episode 给定）或该项目的镜头分段视频目录（用于按真实分段时长对齐时间轴）

    B-10 P1-6：带集号过滤。第 2 集起不再取到第 1 集素材，避免时间轴/成片源系统性错配。
    优先匹配该集专属目录（``<key>_第N集`` 或 ``epNN`` 子目录），找不到再回退到项目级目录。
    """
    ep_tag = f"ep{int(episode):02d}" if episode else ""
    best, best_key = "", (-1, 0)
    # 优先找该集专属目录（第 2 集起视频通常落在 <项目键>_第N集/ 或 epNN/ 子目录）
    ep_dir = ""
    if episode:
        for cand in (os.path.join(VIDEOS_DIR, project_name, ep_tag),
                     os.path.join(VIDEOS_DIR, f"{project_name}_第{episode}集")):
            if os.path.isdir(cand):
                ep_dir = cand
                break
    if ep_dir:
        # 该集目录直接采用
        vids = [f for f in os.listdir(ep_dir) if f.lower().endswith(".mp4")]
        if vids:
            return ep_dir
    # 回退：项目级目录（第 1 集或整集模式）
    for d in project_store.project_dirs(VIDEOS_DIR, project_name):
        if not os.path.isdir(d):
            continue
        vids = [f for f in os.listdir(d) if f.lower().endswith(".mp4")]
        if not vids:
            continue
        key = (len(vids), max(os.path.getmtime(os.path.join(d, f)) for f in vids))
        if key > best_key:
            best, best_key = d, key
    return best


def _mix_manifest(project_name: str, episode: int = 0) -> dict:
    """读取配音清单（优先指定集数，其次最新）"""
    out_dir = os.path.join(DUB_DIR, project_name)
    if not os.path.isdir(out_dir):
        raise DubMixError(f"尚未生成配音（目录不存在）：{out_dir}")
    if episode:
        p = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return {"manifest": json.load(f), "path": p}
    cands = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.endswith("_dub_manifest.json")]
    if not cands:
        raise DubMixError("未找到配音清单（*_dub_manifest.json），请先完成配音合成")
    cands.sort(key=os.path.getmtime, reverse=True)
    p = cands[0]
    with open(p, "r", encoding="utf-8") as f:
        return {"manifest": json.load(f), "path": p}


def _mix_audio_url(project_name: str, rel_path: str) -> str:
    base = os.path.abspath(mix_out_dir(project_name))
    p = os.path.abspath(rel_path)
    if not p.startswith(base + os.sep):
        return ""
    rel = os.path.relpath(p, base).replace(os.sep, "/")
    return f"/api/mix/file/{project_name}/{rel}"


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


def _mix_prepare(data: dict) -> dict:
    """公共准备：解析项目 / 视频 / 剧本 / 配音清单 / 时间轴 / 逐句条目（不合成）"""
    # P2-T2：mix 的 project 解析唯一事实源在 _mix_prepare（被 /mix/plan 与 /mix/generate 共用）。
    # 缺省/越界 project_name → 抛 DubMixError（两条路由均已 catch 并回 400），
    # 不再静默回落共享 'project' 命名空间造成串项目。前端契约必填。
    project_name, _mix_err = _project_or_400((data.get('project_name') or '').strip())
    if _mix_err is not None:
        raise DubMixError("缺少 project_name")
    video_path = _mix_resolve_video(data, project_name)

    resolved = _dub_resolve_script(dict(data, project_name=project_name))
    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 0)

    mf = _mix_manifest(project_name, episode)
    manifest = mf["manifest"]
    episode = episode or int(manifest.get("episode") or 1)

    seg_dir = _mix_segments_dir(project_name, episode)
    timeline = shot_timeline(script, videos_dir=seg_dir)

    params = dict(MIX_DEFAULT_PARAMS)
    params.update(data.get('params') or {})
    mode = (data.get('mode') or params.get("mode") or "timeline").strip()

    lines = [ln for ln in (manifest.get("lines") or []) if ln.get("ok") and ln.get("out_path")]
    if mode == "concat":
        merged = manifest.get("merged_audio") or ""
        if not merged or not os.path.exists(merged):
            raise DubMixError("concat 模式需要整集合并音轨，但配音清单中没有有效 merged_audio")
        entries = [{"line_id": "merged", "shot_id": None, "character": "",
                    "text": "", "audio_path": os.path.abspath(merged),
                    "audio_dur": float((manifest.get("merged_info") or {}).get("duration") or 0),
                    "start": 0.0, "fit_ratio": 1.0}]
        warnings = ["concat 模式：整集音轨从 0 秒顺次铺设，不做逐镜头对齐"]
    else:
        built = build_entries(lines, timeline, params, mode=mode)
        entries, warnings = built["entries"], list(built["warnings"])
    if not entries:
        raise DubMixError("没有可用的配音音频：请先完成配音合成，或检查配音文件是否存在")

    # ---- 音效轨：H3 原生音效经人声分离后垫底（2026-09-17 新增）----
    # 为什么需要：H3 是音视频联合模型，原音轨里既有打斗/雨声等音效，也有它自己生成的
    # 说话声。直接保留原音轨会让两套人声重叠；完全丢弃又会让成片没有任何音效。
    # 折中：用 sfx_isolate 分离出「纯音效」，作为独立条目按同一条时间轴垫底。
    sfx_entries = []
    if H3_SFX_ISOLATE and mode != "concat":
        try:
            import sfx_isolate
            sfx_entries = sfx_isolate.build_sfx_entries(
                project_name, episode, timeline,
                volume=float(params.get("original_audio_volume") or 0.3))
        except Exception as e:                                  # noqa: BLE001
            warnings.append(f"音效轨装配失败（本集跳过音效）：{type(e).__name__}: {e}")
    if sfx_entries:
        entries = list(entries) + sfx_entries
        # 已用「分离后的纯音效」→ 关掉视频原音轨，否则人声会回来、音效也会叠双份
        params["keep_original_audio"] = False
        warnings.append(f"已叠加 {len(sfx_entries)} 条镜头音效（H3 音轨已做人声分离，"
                        f"垫底音量 {params.get('original_audio_volume')}）")
    elif H3_SFX_ISOLATE and params.get("keep_original_audio"):
        warnings.append("未找到可用的分离音效轨，将直接使用视频原音轨垫底"
                        "（其中可能含 H3 生成的说话声）")

    vinfo = probe_video_info(video_path)
    if vinfo.get("duration") and entries[-1].get("end", 0) > float(vinfo["duration"]) + 0.5:
        warnings.append(
            f"末句结束 {entries[-1].get('end')}s 超出视频时长 {vinfo.get('duration')}s，超出部分会被截断")

    return {
        "project_name": project_name, "video_path": video_path, "video_info": vinfo,
        "script_path": resolved["script_path"], "script_source": resolved["source"],
        "episode": episode, "manifest_path": mf["path"], "manifest": manifest,
        "segments_dir": seg_dir, "timeline": timeline,
        "entries": entries, "warnings": warnings, "mode": mode, "params": params,
    }


def _mix_worker(task_id: str, prepared: dict, out_name: str):
    """后台合成：逐句对齐混音 → 落盘带配音成片 + 报告"""
    try:
        project_name = prepared["project_name"]
        out_dir = mix_out_dir(project_name)
        out_path = os.path.join(out_dir, out_name)
        with mix_lock:
            mix_tasks[task_id].update({"phase": "音画对齐混音中", "progress": 30})
        report = mix_video_with_entries(prepared["video_path"], prepared["entries"],
                                        out_path, prepared["params"])
        # ---- 成品音频质检：成片音轨是不是真的有人声 ----
        # ffmpeg 返回成功、文件也有音频流，并不代表「配音真的混进去了」：
        # 条目路径错、amix 被压成静音、源片段本身无声，都能产出一条「合法但没声音」的
        # 音轨。这里对**最终成片**实测一遍（整轨口径，不做有声占比判定）。
        report["audio_qc"] = _mix_audio_qc(report, _qc_load_cfg())
        report.update({
            "task_id": task_id, "project": project_name, "episode": prepared["episode"],
            "mode": prepared["mode"], "video_source": prepared["video_path"],
            "script_path": prepared["script_path"], "manifest_path": prepared["manifest_path"],
            "segments_dir": prepared["segments_dir"], "warnings": prepared["warnings"],
            "timeline": prepared["timeline"], "entries": prepared["entries"],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        report_path = os.path.join(out_dir, f"{os.path.splitext(out_name)[0]}_mix_report.json")
        write_mix_report(report, report_path)
        _aq = report.get("audio_qc") or {}
        _aq_msg = ""
        if _aq.get("enabled") and _aq.get("passed") is not None:
            _aq_msg = "；音频质检通过" if _aq.get("passed") else \
                f"；音频质检未通过（{str(_aq.get('reason') or '')[:60]}）"
        with mix_lock:
            mix_tasks[task_id].update({
                "status": "completed", "progress": 100, "phase": "合成完成",
                "message": (f"已合成 {report['entry_count']} 句配音，"
                            f"耗时 {report['elapsed_sec']}s{_aq_msg}"),
                "output_path": report["output_path"],
                "report_path": report_path,
                "url": _mix_audio_url(project_name, report["output_path"]),
                "audio_qc": _aq,
                "result": report,
            })
        with mix_lock:
            _prune_task_registry(mix_tasks)
        # 带配音成片＝用户真正要验收的成品：自动登记进「成品验收」队列
        reg = register_final_deliverable(
            project_name, prepared["episode"], report["output_path"],
            meta={"source": "mix", "mode": prepared["mode"],
                  "entry_count": report.get("entry_count"),
                  "video_source": os.path.basename(prepared["video_path"] or ""),
                  "report": os.path.basename(report_path)})
        with mix_lock:
            mix_tasks[task_id]["deliverable"] = {
                "registered": bool(reg.get("registered")),
                "reason": reg.get("reason") or "",
                "episode_no": int(prepared["episode"] or 1),
                "path": report["output_path"],
            }
        if not reg.get("registered"):
            app.logger.info(f"成片未登记待验收（{project_name} 第{prepared['episode']}集）："
                            f"{reg.get('reason')}")
    except DubMixError as e:
        app.logger.warning(f"混音合成失败: {e}")
        # B-16 P2-11：混音失败 → 清理本任务产生的中间产物（未完成的 report / 半成品）
        _cleanup_scratch_dir(out_dir, app.logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成失败",
                                       "message": str(e), "progress": 100})
    except Exception as e:  # pragma: no cover - 兜底
        app.logger.exception("音画合成异常")
        # B-16 P2-11：混音异常 → 清理中间产物
        _cleanup_scratch_dir(out_dir, app.logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成异常",
                                       "message": f"{type(e).__name__}: {e}", "progress": 100})


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
                               'step_max_retries')}

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


@app.route('/api/engine/state', methods=['GET'])
def api_engine_state():
    """生成引擎（ComfyUI）健康与自愈状态（2026-10-09）。

    配合 comfyui_client 的**主动心跳**：前端/运维可直接看到
    「引擎是否在线、连续失败几次、10 分钟内重启了几次、上次错误是什么」。

    只读：本接口不触发任何探测或重启，状态由后台心跳线程维护。
    """
    try:
        # ⚠️ 2026-10-09：本文件里 `comfyui_client` 这个名字是**实例**（见 L56 注释：
        #    comfyui_client = ComfyUIClient()），而 get_engine_state 是**模块级**函数 ——
        #    直接用实例取会 AttributeError（表现为接口全 None）。必须从模块取。
        import comfyui_client as _cc  # noqa: PLC0415
        st = _cc.get_engine_state()
    except Exception as e:  # noqa: BLE001  状态接口永不 5xx
        return jsonify({"success": False, "error": str(e)}), 200
    st["success"] = True
    return jsonify(st)


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
_COMFYUI_RECLAIM_LAST_TS = 0.0          # 上次真正扫描的时间戳（模块级节流状态）
_COMFYUI_RECLAIM_INTERVAL_SEC = 600.0   # 同一进程 10 分钟内只真正扫描一次
_COMFYUI_RECLAIM_LOCK = threading.Lock()

# ComfyUI「任务历史」自动清理（面板只增不减 → 易被误读成「生成了大量废图」）：
# 与上面的回收同构 —— 模块级节流 + 全容错，默认间隔取 config 值（5 分钟）。
_COMFYUI_CLEAR_HISTORY_LAST_TS = 0.0
_COMFYUI_CLEAR_HISTORY_LOCK = threading.Lock()


def _maybe_clear_comfyui_history(where: str = "") -> bool:
    """按节流清空 ComfyUI **任务历史列表**（不是磁盘产物）。

    为什么要做：ComfyUI 界面「任务历史」面板只增不减，质检每失败一次重跑就多一条
    记录，跑几轮后几百条 → 用户会以为「生成了大量废图」。实测面板 162 条时磁盘上
    真正残留的废弃分镜图 **0 张**（清之前 /history 162 条 → 清完 0 条）。

    语义边界（重要）：
      · 只调 `POST /history {"clear":true}`，**绝不删任何 output 文件**；
      · 不影响正在执行/排队中的任务（它们结束后会各自追加新记录）；
      · 只应在**任务收尾**调用 —— 有任务在飞时清掉历史，会让 `wait_for_completion`
        的轮询查不到自己那条记录而误判超时。

    开关 `MJSCXT_CLEAR_COMFYUI_HISTORY=0` 可整体关闭；节流 5 分钟（见 config）。
    永不抛异常、永不阻断生产。
    """
    global _COMFYUI_CLEAR_HISTORY_LAST_TS
    if not CLEAR_COMFYUI_HISTORY:
        return False
    try:
        now = time.time()
        with _COMFYUI_CLEAR_HISTORY_LOCK:
            if now - _COMFYUI_CLEAR_HISTORY_LAST_TS < CLEAR_COMFYUI_HISTORY_INTERVAL_SEC:
                return False
            # 先占用时间戳：真正清理失败也不要在同一分钟内反复重试刷屏。
            _COMFYUI_CLEAR_HISTORY_LAST_TS = now
        ok = comfyui_client.clear_history()
        if ok:
            app.logger.info("[任务历史] 已清空 ComfyUI 任务历史面板（收尾：%s）", where or "未知")
        return ok
    except Exception as e:  # noqa: BLE001  可观测性优化，绝不能阻断生产
        app.logger.warning("ComfyUI 任务历史清理异常（不影响生产）：%s: %s",
                           type(e).__name__, e)
        return False


def _comfyui_official_dirs() -> list:
    """正式产物目录清单（全部由 config 常量推导，不硬编码盘符路径）。"""
    return [d for d in (CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR,
                        KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR) if d]


def _maybe_reclaim_comfyui_output() -> None:
    """薄包装：带节流地回收 ``COMFYUI_OUTPUT_DIR`` 下的产物残留（D-11a）。

    只有**同时**满足下列条件的文件才会被删（判定细节与安全论证见
    ``app/disk_reclaim.py`` 模块 docstring）：

      ① 位于 ``COMFYUI_OUTPUT_DIR`` 下的 ``comic_drama*`` 产物目录内；
      ② 文件名是 ComfyUI 侧产物命名（带自动编号后缀 ``_00001_``，或含
         ``_retry``/``_try``）—— 交付件名 ``base.png`` 之类天然不匹配；
      ③ ``mtime`` 距今 > 24h；
      ④ 正式产物目录里已有 ``(size, sha256)`` **双匹配**的同内容副本；
      ⑤ ``st_nlink == 1``（硬链接删了不释放空间）；
      ⑥ 候选不在任何正式产物目录内（含大小写归一后的比较）。

    性能：内容指纹按 ``(路径, size, mtime_ns)`` 进程内缓存，稳态下只有**新增**的
    正式产物需要读盘；配合 10 分钟节流，同步调用不会给任务收尾带来可感知的延迟。
    """
    global _COMFYUI_RECLAIM_LAST_TS
    try:
        now = time.time()
        with _COMFYUI_RECLAIM_LOCK:
            if now - _COMFYUI_RECLAIM_LAST_TS < _COMFYUI_RECLAIM_INTERVAL_SEC:
                return
            _COMFYUI_RECLAIM_LAST_TS = now
        if not COMFYUI_OUTPUT_DIR or not os.path.isdir(COMFYUI_OUTPUT_DIR):
            return
        import disk_reclaim   # 延迟导入：与本文件其它叶子模块一致，避免加载期副作用
        stats = disk_reclaim.reclaim_comfyui_output(
            COMFYUI_OUTPUT_DIR, _comfyui_official_dirs(), logger=app.logger)
        if stats.get("delete"):
            app.logger.info("D-11a ComfyUI 输出回收：删 %d 个产物残留，释放 %.2f MB",
                            len(stats["delete"]),
                            (stats.get("removed_bytes") or 0) / 1048576.0)
    except Exception as e:  # noqa: BLE001  回收是优化，绝不能阻断生产
        app.logger.warning("D-11a ComfyUI 输出回收失败（不影响生产）：%s: %s",
                           type(e).__name__, e)
