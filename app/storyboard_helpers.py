# -*- coding: utf-8 -*-
'''分镜助手（2026-10-11 从 app.py 下沉）。'''

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
from comfyui_client import (ComfyUIClient, camera_spec as _camera_spec,
                            camera_key as _camera_key, camera_angle as _camera_angle,
                            BLOCKING_REF_MARK as _BLOCKING_REF_MARK,
                            IDENTITY_GRID_REF_MARK as _IDENTITY_GRID_REF_MARK)
from qc_helpers import (  # noqa: F401, E402
    CLOSEUP_CHAR_CROP_TOP, _OUTFITS_DIRNAME, _allocate_storyboard_refs,
    _apply_closeup_ref_strategy, _cap_storyboard_refs, _closeup_char_crop,
    _normalize_scene_name, _on_screen_characters, _qc_brief,
    _qc_history_file_for, _qc_prev_shot_desc, _qc_prev_shot_ref,
    _qc_prune_attempts, _qc_ref_images, _qc_retry_hopeless,
    _qc_shot_desc, _qc_style_of, _qc_summary,
    _sanitize_outfit_key, _shot_has_char_ref, _shot_has_on_screen,
    _shot_outfit_dir)
from asset_refs import (  # noqa: F401, E402  助手按域下沉（第四批）
                        _ASSET_IMG_EXTS, _ASSET_IMG_PRIORITY, _STATIC_DIR, _build_asset_index,
                        _first_existing_asset_image, _framing_wants_half_shot,
                        _h3_audio_defs_for, _h3_audio_policy, _h3_common_ref_audios,
                        _h3_common_subject_lock, _h3_is_common_comp, _h3_plan_common_refs,
                        _h3_shot_ref_components, _match_scene_name, _match_shot_chars,
                        _norm_shot_key, _normalize_char_alias, _note_ref_warning,
                        _pick_char_view, _pick_scene_view, _resolve_item_names,
                        _resolve_scene_entry, _resolve_static_dir, _scene_view_for_shot)
from routes._shared import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir, _qc_load_cfg  # noqa: F401  再导出
from keyframe_helpers import (  # noqa: F401, E402
    _ep_of_script, _keyframe_prompt_preflight, _keyframe_qc_verifier,
    _keyframe_recall_cb, _keyframe_sb_map, _prompt_preflight)
from routes._shared import (_episode_video_stats, _load_legacy_flat_script, _load_script_for, register_final_deliverable)  # noqa: F401
from system_helpers import (_COMFYUI_CLEAR_HISTORY_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_CLEAR_HISTORY_LOCK, _maybe_clear_comfyui_history)
from artifact_helpers import (_COMFYUI_RECLAIM_INTERVAL_SEC, _COMFYUI_RECLAIM_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_RECLAIM_LOCK, _PURGE_REJECTED_ENV, _comfyui_official_dirs,
                               _mark_history_file_purged, _maybe_reclaim_comfyui_output,
                               _purge_prompt_records, _purge_rejected_artifacts,
                               _purge_rejected_enabled, _purge_sb_refs, _reject_artifact)
from routes.projects import _collect_project_cast_images, _cover_prompt_from_outline, _move_with_retry, _project_cover_path  # noqa: F401  再导出
from shared_ai import AI_MODULE_LABEL, _ai_client_for_module, _ai_guide_response, _current_llm_client, _optional_llm_client  # noqa: F401  再导出
from shared_web import _safe_upload_name  # noqa: F401  再导出
from routes._shared import EPISODE_BATCH_LIMIT, UPLOAD_TMP_DIR, _episode_units_for_chapters, _estimate_subchunks, _novels_stats, _resolve_novel_project  # noqa: F401  再导出
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from shared_web import _serve_safe  # noqa: F401  再导出
from routes._shared import COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _apply_project_settings, _comfy_view_url, _project_style, _prune_task_registry, _qc_gate, _qc_record, _qc_record_verdict, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks  # noqa: F401  再导出
from lesson_helpers import (_apply_audio_hints, _qc_lesson_from_record,  # noqa: F401, E402
                             _record_audio_qc_lesson, _record_preflight_lesson, _record_qc_lesson)
from sb_helpers import (  # noqa: F401, E402
    _SB_STRUCTURAL_DEFECT_KEYWORDS, _sb_heal_comfyui, _sb_structural_defect)
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
from fs_atomic import atomic_write_json, read_json_strict
import autopilot
from datetime import datetime
import dialogue_utils
from job_state import (generation_state, lock)  # noqa: F401
import json
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, send_from_directory
import os
import prompt_memory
import prompt_qc
import prompt_templates
import qc_client
import random
import re
import shutil
import style_kit
import threading
import time

logger = logging.getLogger(__name__)

# 2026-10-11 随本批一并下沉：这两个名字原先定义在 app.py，
# 但只被本模块使用（全项目引用核对：app.py 1 处定义 + 本模块 5 处使用，无第三方）。
# 留在 app.py 会让本模块反向依赖路由文件，故迁入。
class _PromptQCBlocked(RuntimeError):
    """提示词预检未通过（内部信号）

    批处理循环里每个镜头是一大段嵌套代码，用异常跳出比「把生成段整体再缩进一层」
    安全得多；异常会被同一层的 ``except Exception`` 接住，该镜头照常记入 manifest
    （状态为失败），不会从产物清单里消失。
    """

_REF_CANVAS_CACHE: dict = {}


_TE3D_RENDER_LOCK = threading.Lock()
_GRID_PLAN_TPL_FP = {"v": ""}
_OPT_REASONING_MARKERS = (
    "我们需要回答用户", "作为漫剧生成系统提示词优化器", "作为提示词优化器",
    "需要输出修正后的提示词", "需要保留原提示词", "原提示词有", "需要避免新增",
    "需要确保中文输出", "需要保留英文结构", "最好不要大改",
    "we need to", "the user wants", "i need to", "let me think",
)


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
            logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
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
        logger.error(
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
            logger.warning(f"提示词预检记录落盘失败：{e}")
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
        logger.warning(f"分镜 manifest 更新失败：{e}")


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
        logger.warning("参考图统一画幅：缓存目录不可建，本次沿用原图（%s）", e)
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
            logger.warning("参考图统一画幅失败，该张沿用原图（%s: %s）",
                               type(e).__name__, e)
            newp = path
        unified.append((kind, label, newp))
    logger.info("[分镜参考图画幅] 目标 %d×%d，%d/%d 张已统一（其余原尺寸或降级）",
                    tgt[0], tgt[1], changed, len(refs))
    return unified


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
        logger.debug("[九宫格规划] 模板指纹计算失败（按空指纹处理）：%s", e)
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
                logger.debug("[九宫格规划] 镜头 %s 命中内容寻址缓存（%s）",
                                 shot.get("shot_id"), os.path.basename(cache_path))
                return cached
            logger.warning("[九宫格规划] 缓存内容异常（%s），按未命中重新规划",
                               os.path.basename(cache_path))
    except Exception as e:  # noqa: BLE001 —— 缓存读失败只降级为「每次现算」
        logger.debug("[九宫格规划] 缓存读取失败（忽略，按未命中处理）：%s", e)
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
        logger.warning("[九宫格规划] 镜头 %s 规划失败（回落英文版提示词）：%s: %s",
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
        logger.warning("[九宫格规划] 镜头 %s 规划不满 9 格（实得 %d），回落英文版提示词",
                           shot.get("shot_id"), len(clean))
        return None
    clean.sort(key=lambda p: p["no"])
    if cache_path:
        try:
            atomic_write_json(cache_path, clean)
        except Exception as e:  # noqa: BLE001 —— 缓存写失败不影响本次结果
            logger.debug("[九宫格规划] 缓存写入失败（忽略）：%s", e)
    logger.info("[九宫格规划] 镜头 %s 已产出 9 格分镜规划%s",
                    shot.get("shot_id"),
                    f"（缓存 {os.path.basename(cache_path)}）" if cache_path else "")
    return clean


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
        logger.info("[分镜风格] 风格=%s；画幅=%s；九宫格=%s", _sb_style,
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
        logger.error("旧分镜清单 %s 不可读（%s: %s）：本次不回填被跳过镜头的信息，不影响生成",
                         _prev_manifest, type(_e).__name__, _e)
    logger.info("[分镜断点续跑] overwrite=%s；待处理 %d 镜（已存在者将跳过）",
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
            logger.info("[分镜重试] 本批分镜重试轮数 %d（通用 max_retries=%d，"
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
        logger.warning("[九宫格规划] 文本 LLM 客户端获取失败（回落英文版提示词）：%s: %s",
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
                logger.warning(
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
                    logger.warning(
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
            logger.info("[分镜跨镜衔接] shot=%s 引入上一镜参考：%s",
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
                    logger.info("[3D导演台] shot=%s 站位基准图已渲染：%s",
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
                logger.warning("[3D导演台] shot=%s 渲染失败（降级为纯文字站位）：%s",
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
        logger.info(
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
            logger.warning("[分镜预取] seq=%s 前置段异常（装包待主循环按内联同语义落账）：%s\n%s",
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
                    logger.error(f"镜头 {shot_id} 分镜图前置段失败（预取包）: {pack['exc']}")
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
                            logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
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
                                    logger.warning(
                                        "镜头 %s 即时优化提示词缺协议段（%s），弃用并回落原逻辑",
                                        shot_id, "、".join(_pf_miss))
                                    _opt_prompt = None
                            if _opt_prompt:
                                prompt = _opt_prompt
                                item["prompt"] = prompt
                                logger.info("镜头 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
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
                                        logger.info("镜头 %s 第 %d 次重试，按质检教训改写提示词：%s",
                                                        shot_id, attempt + 1, hints[:2])
                                    else:
                                        prompt = _retry_base
                                        item["prompt"] = prompt
                                        logger.info("镜头 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                        shot_id, attempt + 1)
                                except Exception as mem_err:
                                    logger.warning(f"读取记忆模块失败: {mem_err}")

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
                                logger.warning(
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
                                    logger.info("[分镜质检] 景别自动裁剪 shot=%s: %s", shot_id, _crop_res["reason"])
                                    scratch_png = _crop_res["out_path"]
                            except Exception as _crop_e:  # noqa: BLE001
                                logger.warning("[分镜质检] 景别自动裁剪失败（不阻断）shot=%s: %s", shot_id, _crop_e)
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
                            logger.warning(
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
                        logger.info(
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
                                    logger.warning(
                                        "分镜质检未达标但已软放行入库（storyboard_soft_qc）："
                                        "镜头 %s｜判定=%s｜原因=%s",
                                        shot_id, _soft_lbl, _soft_rsn)
                            except Exception as _se:  # noqa: BLE001
                                logger.warning(
                                    "分镜软放行入库失败（回落硬阻断）：镜头 %s｜%s",
                                    shot_id, _se)
                            if not _soft_ok:
                                logger.warning(
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
                                logger.warning(f"分镜图不合格产物清理失败（忽略）：{_pe}")
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
                logger.warning("镜头 %s 分镜图被提示词预检阻断：%s", shot_id, _pqb)
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
                logger.error(f"镜头 {shot_id} 分镜图生成失败: {shot_err}\n{_tb.format_exc()}")
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
            logger.info("[分镜总计时] shot=%s 成功=%s 墙钟 %.1fs（前置段 %.1fs）",
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
            logger.warning(f"分镜 manifest 原子写失败（已回滚 status）：{_mp_err}")
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
        logger.error(f"分镜图任务失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    # ★ 用户决策 4：ComfyUI 侧分镜参考图上传残留（sb_ref_*）只在**本轮分镜批量生成全部
    # 结束后**按项目清理一次 —— 不在单镜循环里调（那会在重试中途删掉当前镜头正在用的参考图）。
    try:
        _purge_sb_refs(project_name)
    except Exception as _sb_ref_err:  # noqa: BLE001
        logger.warning(f"sb_ref 残留清理失败（忽略）：{_sb_ref_err}")
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    # 分镜批量是本项目重跑最密集的环节（每失败一次多一条任务历史）→ 收尾顺手清历史面板。
    _maybe_clear_comfyui_history("storyboard 批量生成收尾")


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
        logger.warning("[质检] 构图规格生成失败（不影响判定）：%s", e)
        return ""


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
                logger.warning("工作流指纹计算失败（不影响 meta 主体）：%s", e)
        out = os.path.splitext(artifact_path)[0] + ".meta.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001  旁路兜底：meta 写失败绝不阻断入库
        try:
            logger.debug("O2 产物元数据旁路写失败（不影响入库）：%s: %s",
                            type(e).__name__, e)
        except Exception:  # noqa: BLE001
            pass


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
            logger.info("[提示词优化] 文本分析模型未配置，跳过即时优化（回落历史召回/换种子）")
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
                logger.info(
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
            logger.warning(
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
            logger.warning(
                "[提示词优化] %s 结果未通过骨架校验（%s），弃用并回落原逻辑；候选前 80 字：%s",
                kind_label, _why, optimized[:80])
            return None
        logger.info("[提示词优化] %s 针对本次缺陷改写提示词（%d 条问题）：%s → %s",
                        kind_label, len(issues), prompt[:24], optimized[:40])
        return optimized
    except Exception as e:  # noqa: BLE001
        logger.warning("质检后即时优化提示词失败（回落原逻辑）：%s", e)
        return None

