# -*- coding: utf-8 -*-
'''视频生成助手（2026-10-11 从 app.py 下沉）。'''

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
from workers.audio import (_audio_line_expect_sec, _audio_qc_lines,  # noqa: F401, E402
                           _cleanup_scratch_dir, _dub_prompt_preflight,
                           _dub_worker, _mix_audio_qc, _mix_audio_url, _mix_worker)
from collect_helpers import (  # noqa: F401, E402
    _collect_asset_refs, _collect_reference_images)
from routes.tts import (_dub_audio_url, _dub_project_dir)  # noqa: F401
from shared_qc import _qc_load_cfg  # noqa: F401  再导出
from routes._shared import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir  # noqa: F401  再导出
from keyframe_helpers import (  # noqa: F401, E402
    _ep_of_script, _keyframe_prompt_preflight, _keyframe_qc_verifier,
    _keyframe_recall_cb, _keyframe_sb_map, _prompt_preflight)
from episode_helpers import (  # noqa: F401, E402
    _OUTFIT_RECORD_FILE, _bigram_overlap, _episode_frame_ratios,
    _episode_outfit_overrides, _episode_qc_desc, _episode_schema_defaults)
from asset_worker import (  # noqa: F401, E402
    _ITEM_OWNER_REF_PRIORITY, _generate_asset_task, _item_owner_ref_image)
from routes._shared import (_episode_video_stats, _load_legacy_flat_script, _load_script_for, register_final_deliverable)  # noqa: F401
from routes.projects import _collect_project_cast_images, _cover_prompt_from_outline, _move_with_retry, _project_cover_path  # noqa: F401  再导出
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from shared_web import _serve_safe  # noqa: F401  再导出
from shared_qc import _qc_gate, _qc_record, _qc_record_verdict  # noqa: F401  再导出
from shared_tasks import _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _prune_task_registry  # noqa: F401  再导出
from shared_upscale import COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _comfy_view_url, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks  # noqa: F401  再导出
from routes._shared import _apply_project_settings, _project_style  # noqa: F401  再导出
from project_helpers import (  # noqa: F401, E402
    _project_caption_burn_enabled, _project_subtitle_enabled, _project_worldview)
from artifact_helpers import (_COMFYUI_RECLAIM_INTERVAL_SEC, _COMFYUI_RECLAIM_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_RECLAIM_LOCK, _PURGE_REJECTED_ENV, _comfyui_official_dirs,
                               _mark_history_file_purged, _maybe_reclaim_comfyui_output,
                               _purge_prompt_records, _purge_rejected_artifacts,
                               _purge_rejected_enabled, _purge_sb_refs, _reject_artifact)
from qc_helpers import (  # noqa: F401, E402
    CLOSEUP_CHAR_CROP_TOP, _OUTFITS_DIRNAME, _allocate_storyboard_refs,
    _apply_closeup_ref_strategy, _cap_storyboard_refs, _closeup_char_crop,
    _normalize_scene_name, _on_screen_characters, _qc_brief,
    _qc_history_file_for, _qc_prev_shot_desc, _qc_prev_shot_ref,
    _qc_prune_attempts, _qc_ref_images, _qc_retry_hopeless,
    _qc_shot_desc, _qc_style_of, _qc_summary,
    _sanitize_outfit_key, _shot_has_char_ref, _shot_has_on_screen,
    _shot_outfit_dir)
from lesson_helpers import (_apply_audio_hints, _qc_lesson_from_record,  # noqa: F401, E402
                             _record_audio_qc_lesson, _record_preflight_lesson, _record_qc_lesson)
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
from fs_atomic import atomic_write_json, read_json_strict
import cancellation
import comfyui_job_store  # 崩溃免重渲检查点（2026-09-29）：种子沿用判据 + 台账查询
import contextvars
from tts_client import (
    QwenTTSClient, TTSError, check_environment as tts_env_check,
    build_dub_plan, default_voice_map, normalize_voice, save_voice_map,
    load_voice_map, list_voices as tts_list_voices, probe_audio as probe_audio_info,
    concat_audio, clean_line_text,
    # 参考音频克隆（2026-10-06）
    save_voice_bank_ref, find_voice_bank_ref, list_voice_bank, voice_bank_dir,
    clone_available as tts_clone_available, VOICE_BANK_EXTS
)
from job_state import (generation_state, lock)  # noqa: F401
import h3_common_refs
import h3_director_builder
import h3_prompt_kit
import h3_segment_loras
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, send_from_directory
import model_capabilities
import os
import pipeline
import preview_gate  # 两级生产（2026-09-29）：预演不可交付 + 预演批准
import qc_client
import quality_stage  # 四层质量状态（2026-09-29：预演也记 A/B 层）
import random
import shot_key
import style_kit
import threading
import uuid

logger = logging.getLogger(__name__)

_VIDEO_TASK_IS_PIPELINE = contextvars.ContextVar("video_task_is_pipeline", default=False)
_norm_shot_key = shot_key.norm_shot_key


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
            logger.info(f"[retry-shot] 参考图已由磁盘资产补齐："
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
                logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
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
        logger.warning(f"标记成片过期失败：{e}")
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "mode": mode, "path": dst,
                    "url": f"/api/videos/{project}/{_rs_sub}{os.path.basename(dst)}",
                    "ref_count": len(seg["reference_images"]), "duration": seg["duration"],
                    "deliverable_marked_stale": bool(stale)})


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
        logger.warning("[VoiceBank] 找不到项目 dub 目录，跳过自动生成参考音色")
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
        logger.info("[VoiceBank] 从剧本读到 %d 个角色人设，将用于音色派生",
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
        logger.warning("[VoiceBank] 无法生成默认 voice_map，跳过：%s", _e)
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
        logger.info("[VoiceBank] %s 参考音色生成：mode=%s instruct=%s 文本=%s",
                        _cn, _tts_voice["mode"], (_instruct[:40] or "(空)"), _ref_text[:50])
        # 输出路径：voice_bank 目录下的临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _tf:
            _tmp_path = _tf.name
        try:
            _dub_dir2 = _dub_project_dir(project_name)
            _client = tts_client.QwenTTSClient(out_root=_dub_dir2, params=tts_client.TTS_DEFAULT_PARAMS)
            _res = _client.synthesize_one(_ref_text, _tts_voice, _tmp_path, timeout=60)
            if not _res.get("ok"):
                logger.warning("[VoiceBank] %s 合成参考音频失败：%s", _cn, _res.get("error"))
                continue
            # 存入 voice_bank（按 ref.wav 落盘，save_voice_bank_ref 会覆盖旧文件）
            _saved = save_voice_bank_ref(_dub_dir2, _cn, _tmp_path, ref_text=_ref_text)
            logger.info("[VoiceBank] %s 自动生成参考音色：%s", _cn, _saved)
        except Exception as _e:  # noqa: BLE001
            logger.warning("[VoiceBank] %s 自动生成参考音色异常（忽略）：%s", _cn, _e)
        finally:
            try:
                if os.path.isfile(_tmp_path):
                    os.remove(_tmp_path)
            except Exception:
                pass


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
        logger.info("[视频] 任务因中止信号停止（task=%s）：%s", task_id, e)
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
        logger.debug("视频中止判定器读取失败（按不中止处理）：%s", e)
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
            logger.info("[视频] 风格=%s；画幅=%s", _style_res.get("style") or style,
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
                logger.info("[视频] 镜头参数归一化：%s", "；".join(_norm_notes[:3]))
        except Exception as _norm_err:  # noqa: BLE001
            logger.warning("镜头参数归一化失败（按原 shots 继续）：%s", _norm_err)
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
                logger.info("[视频] 参考图已由磁盘资产补齐：角色 %d / 合计 %d",
                                len(main_char_img), len(ref_imgs))
            else:
                # 优化#1（2026-10-01）：连磁盘兜底都拿不到参考图 → 角色资产从未生成或
                # 目录被清空。此前只 warning 后照跑（人物全靠模型自由发挥，成片必 OOC）。
                # 现在自动补做：从剧本取角色清单，同步触发生成（上限 4 个、总等待 30 分钟），
                # 完成后重新收集参考图再继续；补做失败/超时不阻塞出片（fail-open）。
                logger.warning("[视频] 磁盘资产目录里也没有可用参考图 → 自动补做角色资产…")
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
                            logger.warning("[视频] 资产补做超时（30 分钟）→ 按无锚点继续")
                        _re_chars, _re_scenes = _collect_asset_refs(project_name)
                        if _re_chars:
                            character_refs = _re_chars
                            main_char_img = _collect_reference_images(character_refs[:1], [])
                            ref_imgs = _collect_reference_images(character_refs, scene_refs)
                            logger.info("[视频] 资产补做完成：角色参考图已重新挂载"
                                            "（主角锚点 %d / 合计 %d）",
                                            len(main_char_img), len(ref_imgs))
                except Exception as _e:  # noqa: BLE001
                    logger.warning("[视频] 资产自动补做失败（继续生成）：%s", _e)
                if not (main_char_img or ref_imgs):
                    logger.warning("[视频] 补做后仍无参考图 —— 本集将无角色锚点生成，"
                                       "人物一致性无法保证（检查 output/assets/characters/%s）",
                                       project_name)
        logger.info(f"视频参考图解析结果: {ref_imgs}；主角锚点: {main_char_img}")

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
        logger.info(f"分镜图参考映射: {sorted(sb_map.keys())}")

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
                            logger.debug("[服装变体] 解析失败（回落主设定图）：%s", _oe)
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
                    logger.warning(
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
                    logger.warning("镜头 %s 视频提示词预检未通过（%s）：%s",
                                       shot.get("shot_id"), _pgate_seg.get("label"),
                                       _pgate_seg.get("reason"))
            if _multi_seg:
                logger.info(
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
                    logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
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
                logger.debug("[跨集衔接] 上集 state 加载失败（按无上集处理）：%s", _ce)
            try:
                _outfit_ovr = _episode_outfit_overrides(
                    project_name, int(_epn or 1)) or {}
            except Exception as _oe:  # noqa: BLE001
                logger.debug("[服装变体] 本集覆盖解析失败（不指定变体）：%s", _oe)

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
                                    logger.info(
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
                        logger.exception("[视频流水线] 后台构建提示词失败：%s", _be)
                    finally:
                        for _sn3, _ in _scene_plan[1:]:
                            _vbuilt[_sn3]["ev"].set()   # 唤醒等待方，绝不让人死等

                threading.Thread(target=_vbg_build, name="h3-prefetch",
                                 daemon=True).start()
                logger.info(
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
                logger.info("[教训][video] project=%s mode=episode qc_off=true "
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
                    logger.info("[整集质检] 按段抽帧：%d 段 → 请求 %d 帧（每段中点占比）",
                                    len(_qc_segs), len(_qc_ratios))
                else:
                    logger.warning(
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
                    logger.info("[整集质检] 逐段一致性门禁：%d 段时间区间 + %d 张角色锚点图",
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
                    logger.warning(
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
                        logger.info(
                            "[教训][video] project=%s mode=episode attempt=%d ok=True "
                            "passed=False → 已沉淀",
                            project_name, _ep_qc_attempt["n"])
                    except Exception as le:  # noqa: BLE001
                        logger.warning("整片质检教训沉淀失败：%s", le)
                elif not passed and not verdict.get("ok"):
                    # [教训][video] 诊断（§2.3.5）：质检调用异常/超时（ok=false）时静默跳过、
                    # 不记教训——视频质检需 ffmpeg 抽帧 + 多模态，失败率高，如实打点便于排障。
                    logger.info(
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
                        logger.warning(f"整片抽帧图清理失败（忽略）：{_pe}")
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
                logger.warning(
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
                logger.warning("[预演] 已开启预演，但流水线模式下 segs 未聚合 → 跳过预演")
            if _pv_gate and not _vstream:
                _pv_prefix = preview_gate.preview_prefix(
                    f"comic_drama/{project_name}_{episode_tag or 'episode'}")
                _pv_need, _pv_why = preview_gate.needs_preview(project_name, episode_tag)
                if _pv_need:
                    logger.info("[预演] 第%s集先出低成本预演（原因：%s）", episode_tag, _pv_why)
                    with lock:
                        generation_state[task_id].update({
                            "phase": f"整集预演生成中（{len(segs)} 段 · 低分辨率 + 短时长）",
                            "preview": True, "progress": 5})
                    _pv_size = preview_gate.preview_size(_size)
                    _pv_segs = preview_gate.preview_segments(segs)
                    logger.info("[预演] 画幅 %s → %s；段数 %d（不变，每镜都看得到）",
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
                        logger.error("[预演] 生成失败：%s", _pv_err, exc_info=True)
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
                        logger.warning("[预演] 产物迁移失败（沿用原路径）：%s", _pv_mv)
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
                        logger.warning("[预演] 质量状态记录失败（忽略）：%s", _pv_qs)
                    with lock:
                        generation_state[task_id].update({
                            "status": "awaiting_preview_approval", "progress": 100,
                            "phase": "预演已生成，等待人工批准后再生产正式成片",
                            "results": [{"success": True, "mode": "episode",
                                         "preview": True, "deliverable": False,
                                         "file": _pv_dst, "segment_count": len(_pv_segs),
                                         "approve_hint": "批准预演后重新生成本集，即产出正式成片"}]})
                    logger.info("[预演] 已产出预演（不可交付）：%s", _pv_dst)
                    return

            logger.info(f"[episode] 整集生成开始：{len(segs)} 段，qc_on={qc_on}，max_retries={max_retries}")

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
                logger.info("[episode][build_only] 工作流将导出到：%s", _wf_export_path)

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
                logger.info("[episode] 按场次生成：%d 场 / %d 段（build_only=%s）",
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
                    logger.info("[episode][build_only] 按场次导出完成：%d/%d 场",
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
                        logger.info("[episode][per-scene] 第 %d 场已有成片，复用：%s",
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
                    logger.exception("[episode][per-scene] 拼接失败")
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
                logger.info("[episode][per-scene] 整集拼接完成：%s（%d 场）",
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
                    logger.info("[episode][build_only] 已导出：%s", _wf_export_path)
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
                logger.warning(f"整集重试残留清理失败（忽略）：{_pe}")
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
            logger.info(f"[episode] 产物落盘: {dst}（qc_passed={qc_passed}，"
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
        logger.error(f"视频生成失败: {e}")
        # B-16 P2-11：视频任务异常 → 清理本任务产生的视频 scratch 中间产物
        _cleanup_scratch_dir(os.path.join(QC_DIR, project_name, "video_scratch"), logger)
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})

