# -*- coding: utf-8 -*-
'''镜头辅助助手（2026-10-11 从 app.py 下沉）。'''

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
import coverage
from novel_parser import (
    SUPPORTED_EXTS, NovelParseError, ingest_novel, list_novels,
    get_novel, preview_novel, read_novel_text, split_chapters,
    ensure_chapter_structure, chapter_body_chars
)

logger = logging.getLogger(__name__)

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
        logger.debug(f"小说正文不可读（覆盖率归属将缺失）：{e}")
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

