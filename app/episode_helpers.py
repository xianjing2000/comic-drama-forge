# -*- coding: utf-8 -*-
'''集级配置助手（2026-10-11 从 app.py 下沉）。'''

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
import json
import novel_to_script
import os
import project_store
import qc_coverage
import re

logger = logging.getLogger(__name__)

_OUTFIT_RECORD_FILE = "outfit.json"


def _episode_schema_defaults(project_name: str, shots: list) -> dict:
    """⑥ 下游链路自动引用剧本自动判定的「镜头数 / 每集时长」字段。

    - 镜头缺 duration 时按项目配置的 duration_per_shot 兜底；
    - 返回 episode_stats（shot_count / duration_sec / episode_plan）供接口回显与后续步骤使用。
    """
    cfg = {}
    try:
        cfg = project_store.read_config(project_name)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取项目配置失败（{project_name}）：{e}")
    per_shot = cfg.get("duration_per_shot") or 5
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        if not s.get("duration"):
            s["duration"] = per_shot
    return novel_to_script.build_episode_stats(shots)


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
        logger.info("[服装变体] 本集服装覆盖：%s", out)
    return out


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
    return qc_coverage.episode_qc_desc(shots, limit=limit, warn=logger.warning)

