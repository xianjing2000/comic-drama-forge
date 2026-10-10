# -*- coding: utf-8 -*-
'''资产收集助手（2026-10-11 从 app.py 下沉）。'''

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
from asset_refs import (  # noqa: F401, E402  助手按域下沉（第四批）
                        _ASSET_IMG_EXTS, _ASSET_IMG_PRIORITY, _STATIC_DIR, _build_asset_index,
                        _first_existing_asset_image, _framing_wants_half_shot,
                        _h3_audio_defs_for, _h3_audio_policy, _h3_common_ref_audios,
                        _h3_common_subject_lock, _h3_is_common_comp, _h3_plan_common_refs,
                        _h3_shot_ref_components, _match_scene_name, _match_shot_chars,
                        _norm_shot_key, _normalize_char_alias, _note_ref_warning,
                        _pick_char_view, _pick_scene_view, _resolve_item_names,
                        _resolve_scene_entry, _resolve_static_dir, _scene_view_for_shot)
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
import os

logger = logging.getLogger(__name__)

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
            logger.warning(f"角色参考图不可用: {ref.get('name') if isinstance(ref, dict) else ref}")
    for ref in list(scene_refs)[:1]:
        for key in ("front", "base"):
            p = ref.get(key) if isinstance(ref, dict) else None
            local = comfyui_client.resolve_local_path(p) if p else None
            if local and os.path.exists(local):
                ref_imgs.append(local)
                break
    return ref_imgs

