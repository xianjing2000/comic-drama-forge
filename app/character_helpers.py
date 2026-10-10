# -*- coding: utf-8 -*-
'''角色助手（2026-10-11 从 app.py 下沉）。'''

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
from qc_helpers import (  # noqa: F401, E402
    CLOSEUP_CHAR_CROP_TOP, _OUTFITS_DIRNAME, _allocate_storyboard_refs,
    _apply_closeup_ref_strategy, _cap_storyboard_refs, _closeup_char_crop,
    _normalize_scene_name, _on_screen_characters, _qc_brief,
    _qc_history_file_for, _qc_prev_shot_desc, _qc_prev_shot_ref,
    _qc_prune_attempts, _qc_ref_images, _qc_retry_hopeless,
    _qc_shot_desc, _qc_style_of, _qc_summary,
    _sanitize_outfit_key, _shot_has_char_ref, _shot_has_on_screen,
    _shot_outfit_dir)
from shared_episode import _episode_video_stats, _load_legacy_flat_script, _load_script_for, register_final_deliverable  # noqa: F401  再导出
from asset_refs import (  # noqa: F401, E402  助手按域下沉（第四批）
                        _ASSET_IMG_EXTS, _ASSET_IMG_PRIORITY, _STATIC_DIR, _build_asset_index,
                        _first_existing_asset_image, _framing_wants_half_shot,
                        _h3_audio_defs_for, _h3_audio_policy, _h3_common_ref_audios,
                        _h3_common_subject_lock, _h3_is_common_comp, _h3_plan_common_refs,
                        _h3_shot_ref_components, _match_scene_name, _match_shot_chars,
                        _norm_shot_key, _normalize_char_alias, _note_ref_warning,
                        _pick_char_view, _pick_scene_view, _resolve_item_names,
                        _resolve_scene_entry, _resolve_static_dir, _scene_view_for_shot)
import json
import os

logger = logging.getLogger(__name__)

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
        logger.warning("服装变体读取剧本角色档案失败（忽略）：%s", e)
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
        logger.warning("服装变体读取主设定 meta 失败（忽略）：%s", e)
    return ""

