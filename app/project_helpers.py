# -*- coding: utf-8 -*-
'''项目级配置助手（2026-10-11 从 app.py 下沉）。'''

# 由 tools/sink_helpers.py 自动生成：闭包展开 + 原样复制 import。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
import logging

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
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
import autopilot
import json
import project_store

logger = logging.getLogger(__name__)

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
        logger.warning(f"读取项目 config.subtitle_enabled 失败（按关闭处理）：{e}")
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
        logger.warning(f"读取项目 config.caption_burn_enabled 失败（按默认开启处理）：{e}")
    return default

