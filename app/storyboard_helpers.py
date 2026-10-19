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
    #: T20（2026-10-13 用户口径）：每场戏的最后一镜（连续 run 口径）
    tail_shot_indexes,
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
    _is_detail_shot, _qc_prune_attempts, _qc_ref_images, _qc_retry_hopeless,
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
from shared_qc import _qc_load_cfg  # noqa: F401  再导出
from shared_project import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir  # noqa: F401  再导出
from keyframe_helpers import (  # noqa: F401, E402
    _ep_of_script, _keyframe_prompt_preflight, _keyframe_qc_verifier,
    _keyframe_recall_cb, _keyframe_sb_map, _prompt_preflight)
from shared_episode import _episode_video_stats, _load_legacy_flat_script, _load_script_for, register_final_deliverable  # noqa: F401  再导出
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
from shared_novel import EPISODE_BATCH_LIMIT, UPLOAD_TMP_DIR, _episode_units_for_chapters, _estimate_subchunks, _novels_stats  # noqa: F401  再导出
from shared_project import _resolve_novel_project  # noqa: F401  再导出
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from shared_web import _serve_safe  # noqa: F401  再导出
from shared_qc import _qc_gate, _qc_record, _qc_record_verdict  # noqa: F401  再导出
from shared_tasks import _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _prune_task_registry  # noqa: F401  再导出
from shared_upscale import COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _comfy_view_url, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks  # noqa: F401  再导出
from shared_project import _apply_project_settings, _project_style  # noqa: F401  再导出
from lesson_helpers import (_apply_audio_hints, _qc_lesson_from_record,  # noqa: F401, E402
                             _record_audio_qc_lesson, _record_preflight_lesson, _record_qc_lesson)
from sb_helpers import (  # noqa: F401, E402
    _SB_STRUCTURAL_DEFECT_KEYWORDS, _sb_heal_comfyui, _sb_soft_release_blocked,
    _sb_structural_defect)
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
from fs_atomic import atomic_write_json, read_json_strict
import autopilot
from datetime import datetime
import dialogue_utils
from job_state import (generation_state, lock)  # noqa: F401
import json
from flask import Flask, request, jsonify, send_file, abort, redirect, send_from_directory
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
#: 画幅统一产物 → 源图 的反查表（T3，2026-10-12）。
#: 为什么必须记：统一产物的文件名是 base_<sha1前8>_WxH.png（只带源路径哈希），
#: 从产物**加不回来**源路径；而 manifest 里要记的是「哪个道具的哪张图」这种稳定身份。
#: 只在进程内累积（生成期一次跑完就用完），不落盘 —— 落盘的证据是 manifest 的 refs 身份串。
_REF_CANVAS_ORIGIN: dict = {}
#: 身份基准网格的**内容缓存**（2026-10-13）：同一批角色 + 同尺寸 + 同布局 → 逐像素相同，
#: 复用已拼好的网格文件；key 含各源图 mtime_ns，角色设定图更新即自动失效。
_IDENTITY_GRID_CACHE: dict = {}


_TE3D_RENDER_LOCK = threading.Lock()
_GRID_PLAN_TPL_FP = {"v": ""}
_OPT_REASONING_MARKERS = (
    "我们需要回答用户", "作为漫剧生成系统提示词优化器", "作为提示词优化器",
    "需要输出修正后的提示词", "需要保留原提示词", "原提示词有", "需要避免新增",
    "需要确保中文输出", "需要保留英文结构", "最好不要大改",
    "we need to", "the user wants", "i need to", "let me think",
)


_TE3D_BODY_CACHE: dict = {}


def _te3d_body_profiles(project_name, episode_no):
    """本集角色体型 {角色名: {heightScale, girth, height}}（进程内缓存）。

    数据源 = 剧本 characters 的 appearance / age（「清瘦少年」「魁梧…体格壮硕」），
    由 te_3d_director 的确定性规则推断，零模型调用。给 3D 构图基准图消费。
    """
    key = (str(project_name or ""), str(episode_no or ""))
    if key in _TE3D_BODY_CACHE:
        return _TE3D_BODY_CACHE[key]
    out: dict = {}
    try:
        import te_3d_director as _t3d
        _script = _load_script_for(project_name, episode_no) or {}
        out = _t3d.build_char_profiles(_script.get("characters") or [])
    except Exception as _e:  # noqa: BLE001
        logger.debug("[3D导演台] 体型档案构建失败（忽略）：%s", _e)
    _TE3D_BODY_CACHE[key] = out
    return out


_TE3D_SCENE_CACHE: dict = {}


#: 场景描述里的**人物词**：命中即该分句描述的是「谁在这」，不是「这地方长什么样」。
#: 分镜的人物清单是严格由 characters_in_shot 驱动的（清单外一律不许出现），
#: 所以场景描述里的这些人一律剔除，否则「弟子可四面围观」「由内门弟子把守」
#: 会被模型当成画面内容 → 质检判「背景出现名单外人物」（镜 14 实测命中）。
_SCENE_PEOPLE_RE = re.compile(
    r"弟子|众人|人群|群众|守卫|侍卫|随从|护卫|路人|行人|人影|人们|百姓|围观|被围者|人群们")


def _strip_people_from_scene(desc: str) -> str:
    """去掉场景描述里「有谁在那里」的分句，只留「这地方长什么样」。"""
    t = str(desc or "").strip()
    if not t:
        return t
    parts = re.split(r"([，；。])", t)
    out: List[str] = []
    for seg in parts:
        if not seg:
            continue
        if seg in "，；。":
            out.append(seg)
            continue
        if _SCENE_PEOPLE_RE.search(seg):
            continue
        out.append(seg)
    cleaned = "".join(out)
    cleaned = re.sub(r"[，；]{2,}", "，", cleaned).strip("，；")
    return cleaned


def _scene_appearance_of(shot: dict, scene_idx: dict) -> str:
    """本镜场景的**视觉描述**（scenes[].appearance）—— 分镜提示词的环境来源。

    ⚠️ 2026-10-20（用户实测「场景缺失：背景为整片纯白空白」）：分镜提示词此前只会
    拿到 shot['location'] 那**六个字的场景名**（如「青云宗外门广场」），
    而剧本场景资产里写得很全的 appearance（「正午烈日下的宽阔青灰石坪，四周殿阁环立，
    光比强烈石面反光刺目…」）**从来没进过提示词** —— 模型不知道这地方长什么样，
    于是直接画成纯白背景，被质检判「场景缺失/场景不符」硬阻断（实测 5/14 镜报废）。

    scene_idx 来自 _build_asset_index(script['scenes'], project, 'scene')，
    其 entry 已带 appearance（见 asset_refs._build_asset_index 的 2026-10-15 注释）。
    """
    loc = str((shot or {}).get("location") or "").strip()
    if not loc or not isinstance(scene_idx, dict):
        return ""
    entry = scene_idx.get(loc)
    if isinstance(entry, dict):
        return str(entry.get("appearance") or "").strip()
    # 兜底：剧本场景名与镜头 location 不完全一致时按包含关系匹配（取最长命中）
    best, best_len = "", 0
    for nm, e in scene_idx.items():
        if not isinstance(e, dict):
            continue
        n = str(nm or "")
        if n and (n in loc or loc in n) and len(n) > best_len:
            best, best_len = str(e.get("appearance") or "").strip(), len(n)
    return best


def _with_scene_appearance(shot: dict, scene_idx: dict) -> dict:
    """给 shot 补上 scene_appearance（原地写，返回同一个 dict，便于链式调用）。"""
    try:
        if isinstance(shot, dict) and not str(shot.get("scene_appearance") or "").strip():
            d = _scene_appearance_of(shot, scene_idx)
            if d:
                # ⭐ 2026-10-20：剔除场景描述里的人物分句（见 _strip_people_from_scene）
                d = _strip_people_from_scene(d)
            if d:
                shot["scene_appearance"] = d
    except Exception as _e:  # noqa: BLE001 - 绝不影响出图
        logger.debug("[分镜场景] 场景描述注入失败（忽略）：%s", _e)
    return shot


def _te3d_scene_images(project_name, episode_no=None):
    """{场景名: base.png 绝对路径}（进程内缓存）—— 3D 基准图的场景背板/全景来源。"""
    key = str(project_name or "")
    if key in _TE3D_SCENE_CACHE:
        return _TE3D_SCENE_CACHE[key]
    out: dict = {}
    try:
        root = os.path.join(SCENES_DIR, _safe_project(project_name))
        if os.path.isdir(root):
            for nm in os.listdir(root):
                p = os.path.join(root, str(nm), "base.png")
                if os.path.isfile(p):
                    out[str(nm)] = p
    except Exception as _se:  # noqa: BLE001
        logger.debug("[3D导演台] 场景图索引失败（忽略）：%s", _se)
    _TE3D_SCENE_CACHE[key] = out
    return out


def _storyboard_scratch_ep(episode_no=None) -> int:
    """分镜「生成中」暂存路径的集号（兜底口径与 shared_project._ep_dir 一致）。"""
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1
    return ep if ep > 1 else 1


def _storyboard_scratch_dir(project, episode_no=None) -> str:
    """分镜「生成中」暂存目录（**按集隔离**：第 1 集平铺，第 2 集起 `epNN/`）。

    ⚠️ 2026-10-12 P0 修复（用户实测报告）：原实现固定
    `QC_DIR/<项目>/storyboard_scratch` —— **路径不带集号**，于是第 1 集生成过程中
    写进去的 `shot_NN_tryK.png` 会被切到第 2 集的画布扫到，前端把第 1 集的图
    当作「第 2 集第 N 镜 生成中」的预览（切集后图串集）。

    口径与正式产物完全一致（见 `shared_project._ep_dir`）：第 1 集平铺以兼容旧数据，
    第 2 集起 `epNN/`。

    ⭐ 写入侧与读取侧**必须**共用本函数：只改一侧会让新路径永远为空
    （写入仍在平铺、读取已指向 `epNN/`）。
    """
    return _ep_dir(os.path.join(QC_DIR, project, "storyboard_scratch"), episode_no)


def _storyboard_scratch_sub(episode_no=None) -> str:
    """暂存图 URL 的集前缀：第 1 集空串（兼容旧前端与旧数据），第 2 集起 `epNN/`。

    拼进 `/api/storyboards/scratch/<项目>/<前缀><文件名>`。该路由（routes/storyboards_api.py）
    的 filename 是 `<path:>` 转换器，天然接受 `epNN/shot_NN_tryK.png` 这类子路径，
    与 base 目录（`.../storyboard_scratch`）拼接后正好命中集级子目录。
    """
    ep = _storyboard_scratch_ep(episode_no)
    return f"ep{ep:02d}/" if ep > 1 else ""


def _storyboard_scratch_map(project, episode_no=None):
    """扫「分镜生成中」的中间产物 → {shot_seq: {"url", "mtime"}}。

    ⭐ 为什么需要它（2026-10-02）：
        分镜步骤是**整步落盘**的 —— 6 镜全部生成 + 质检通过后，才把图写进
        `STORYBOARDS_DIR/<项目>/` 与 `storyboard_manifest.json`（画布的正规数据源）。
        而生成过程本身要 2~3 分钟/镜，整步十几分钟，期间画布**一张图都取不到**，
        用户看到的是「跑着但什么都没有」，误判为卡死或前端不刷新。

    `storyboard_scratch/[epNN/]` 是步骤进行中每镜的落盘位置（`shot_NN_tryK.png`，见
    下方生成侧 `scratch_png`），这里把它作为**只读的进度快照**暴露给画布，
    仅用于「生成中」预览，**不改变** `storyboard.exists/url` 的既有语义
    （那仍严格代表「已落盘正式产物」）。取最新 try（文件名尾部序号最大）。

    ⚠️ **集隔离（2026-10-12 P0）**：只扫**本集**的暂存目录（`episode_no` 决定；
    第 1 集平铺、第 2 集起 `epNN/`）。本集目录不存在就返回空表，
    **绝不回落**到第 1 集的平铺目录 —— 那正是「第 2 集显示第 1 集分镜图」的根因。

    失败一律返回空 dict：这是纯展示增强，绝不能让画布接口 500。
    """
    out = {}
    scratch_dir = _storyboard_scratch_dir(project, episode_no)
    if not os.path.isdir(scratch_dir):
        return out
    _sub = _storyboard_scratch_sub(episode_no)
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
                    "url": f"/api/storyboards/scratch/{project}/{_sub}{fn}",
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
    data = (request.get_json(silent=True) or {})
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
    # ⭐ 2026-10-20：同 _prep_shot —— 单镜重跑也必须带场景视觉描述。
    _with_scene_appearance(shot, scene_idx)
    # ⭐ 2026-10-18（借鉴同类项目「清单驱动·缺图不出图」）：本镜声明了出场角色、但某角色
    #    没有任何可用参考图（按需生成也失败）→ 直接拒绝出图，提示先补资产。
    _miss_c = list(shot.get("_sb_missing_char_refs") or [])
    if _miss_c and _sb_require_char_refs():
        return jsonify({"success": False, "char_refs_missing": _miss_c,
                        "error": ("角色参考图缺失：%s —— 已按「缺图不出图」拒绝生成"
                                  "（请先补齐这些角色的资产，再重跑本镜）")
                                 % "、".join(_miss_c)}), 409
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
    # P0（2026-10-12）：暂存目录**按集隔离**，与画布读取侧共用同一口径，
    # 否则单镜重跑的图会落到平铺目录、被别的集的画布扫到。
    scratch_dir = _storyboard_scratch_dir(project, _ep)
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
    # ⭐ T25（2026-10-12）：单镜重跑**真通过**后必须同步刷新产物旁 `<shot>.meta.json` ——
    #   meta 是软放行事实的**耐久源**（manifest 被后续重跑重写、断点续跑只回填精简信息时，
    #   画布会回落到 meta 读软放行）。若只清 manifest 标记而不更新 meta，这一镜会因
    #   **陈旧 meta** 继续在画布上显示为「软放行」（假警示，用户以为没修好）。
    #   ⚠️ _write_artifact_meta 是**整份覆盖写**：必须带上 seed/prompt，不能只写 extra。
    try:
        _write_artifact_meta(
            dst, kind="storyboard", project_name=project, seed=seed, prompt=prompt,
            workflow_key="storyboard_gen",
            qc={**(gate or {}), "soft_accepted": False, "delivered": True, "passed": True},
            shot_id=shot_id, extra={"qc_soft_accepted": False})
    except Exception as _me:  # noqa: BLE001
        logger.warning("单镜重跑：产物 meta.json 刷新失败（忽略，不影响入库）：%s", _me)
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


# ===================== T12（2026-10-12）分镜质量口径：success / soft / blocked =====================
# 用户实测（逆天系统_全书 46 镜）：**17 镜（37%）是「软放行」** —— 质检判定不达标、
# 但按 `qc_config.storyboard_soft_qc`（默认 True）仍写入正式目录（见下方 worker 的
# 软放行分支），其中 shot_28 / shot_44 还带**非空 critical_issues**。
# 旧实现把这些镜一律写成 `item["success"]=True`，于是 storyboard_manifest.json 报
# `success_count=46 / qc_blocked_count=0`，前端据此显示**全绿** —— 用户以为整集合格，
# 实际首轮通过率只有 15/46=33%、最终 29/46=63%。
#
# 本节把「软放行」变成一等公民：**单独计数、绝不计入 success**，并把该事实同步到
# manifest item + item["qc"] + worker 任务状态（manifest / API / UI 三层同源）。

_SB_CRITICAL_KEYS = ("critical_issues", "critical_issue", "critical_defects")


def _sb_critical_issues(*sources) -> list:
    """从若干候选字典里取第一份非空 critical_issues（软放行的「严重问题清单」）。"""
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in _SB_CRITICAL_KEYS:
            v = src.get(k)
            if isinstance(v, (list, tuple)):
                out = [str(x).strip() for x in v if str(x or "").strip()]
            elif v:
                out = [str(v).strip()]
            else:
                out = []
            if out:
                return out
    return []


def _sb_require_char_refs() -> bool:
    """缺角色参考图时是否禁止出图（2026-10-18 借鉴同类项目；默认开）。

    无身份锚点硬画必出「画错人/错服」（实测反复），停该镜让人先补资产，
    比烧一遍 GPU 再被质检打回便宜。MJSCXT_SB_REQUIRE_CHAR_REFS=0 回到旧的降级出图。
    """
    return str(os.environ.get("MJSCXT_SB_REQUIRE_CHAR_REFS", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _sb_soft_accepted(item) -> bool:
    """该镜是否为**软放行**（质检未达标但仍写入正式目录）。

    证据优先级（兼容三代数据形态）：
      ① item["qc_soft_accepted"] —— 2026-10-08 起批量 worker 软放行分支直写；
      ② item["qc_gate"]["soft_accepted"] —— 同轮把 gate 就地改成 accept 的标记；
      ③ item["qc"]["soft_accepted"] —— 产物 meta 回读后的同步口径（回填/重统计）。
    """
    if not isinstance(item, dict):
        return False
    if item.get("qc_soft_accepted"):
        return True
    _g = item.get("qc_gate")
    if isinstance(_g, dict) and _g.get("soft_accepted"):
        return True
    _q = item.get("qc")
    if isinstance(_q, dict) and (_q.get("soft_accepted") or _q.get("soft_released")):
        return True
    return False


def _sb_artifact_meta_soft(dst, seq=None, base_dir: str = "") -> dict:
    """读产物旁的 `<shot>.meta.json`，取回「软放行 / 严重问题」事实。

    ⭐ 为什么必须读它（T12 验收「对生产数据重新统计得 17」的关键）：
      manifest 会被**后续重跑**重写（断点续跑只给被跳过的镜头回填上一轮的精简信息），
      历史软放行标记在 manifest 里会丢。而 `_write_artifact_meta` 把
      `extra.qc_soft_accepted` 与 `qc.soft_accepted / qc.critical_issues` 随图一起
      落盘，是**最耐久**的那份证据 —— 实测：逆天系统_全书的 manifest 里只剩 1 条软放行
      标记，而 meta.json 里有 17 条。

    返回 {"soft": bool, "critical_issues": [...]}；任何异常都返回空结论
    （fail-open：统计增强绝不能反过来影响主流程）。
    """
    out = {"soft": False, "critical_issues": []}
    cands = []
    if dst:
        cands.append(str(dst) + ".meta.json")
    if seq is not None and base_dir:
        try:
            cands.append(os.path.join(base_dir, f"shot_{int(seq):02d}.meta.json"))
        except (TypeError, ValueError):
            pass
    for p in cands:
        try:
            if not p or not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
            if not isinstance(meta, dict):
                continue
        except Exception:  # noqa: BLE001
            continue
        extra = meta.get("extra") if isinstance(meta.get("extra"), dict) else {}
        qc = meta.get("qc") if isinstance(meta.get("qc"), dict) else {}
        gate = meta.get("qc_gate") if isinstance(meta.get("qc_gate"), dict) else {}
        soft = bool(extra.get("qc_soft_accepted") or qc.get("soft_accepted")
                    or gate.get("soft_accepted"))
        ci = _sb_critical_issues(qc, gate, extra)
        if soft or ci:
            return {"soft": soft, "critical_issues": ci}
    return out


def _sb_reuse_label(facts) -> str:
    """断点续跑**复用产物**时给 UI 的 label（T13：按事实证明，不再一律说「已达标」）。

    旧文案无条件写「沿用已达标图」；而软放行（2026-10-08 起）的图同样写正式目录、
    同样会被复用 —— UI 于是出现「严重问题 1」＋「质检未达标（软放行）：沿用已达标图」
    这种自相矛盾（用户实测截图，逆天系统_全书 分镜 #11）。
    """
    if isinstance(facts, dict) and (facts.get("soft") or _sb_critical_issues(facts)):
        return "沿用上一轮未达标图（未重生成，带严重问题）"
    return "沿用已达标图"


def _sb_fix_reuse_label(item) -> bool:
    """就地修正条目上**误导的复用文案**（T13，历史 manifest 兼容）。

    只改「沿用已达标图」这一种取值：若该条目其实是软放行 / 带 critical_issues，
    改写为「沿用上一轮未达标图（未重生成，带严重问题）」。返回是否发生改写。
    """
    if not isinstance(item, dict):
        return False
    qc = item.get("qc")
    if not isinstance(qc, dict) or str(qc.get("label") or "") != "沿用已达标图":
        return False
    facts = {"soft": _sb_soft_accepted(item),
             "critical_issues": _sb_critical_issues(item.get("qc_gate") or {}, qc, item)}
    new = _sb_reuse_label(facts)
    if new == qc.get("label"):
        return False
    qc["label"] = new
    return True


def _sb_dst_soft_facts(dst, seq=None, base_dir: str = "", prev=None) -> dict:
    """正式目录里**已存在的这张图**是否「并非真达标」（软放行 / 带严重问题）。

    ⭐ T13（2026-10-13，用户指令「严重问题不要放行」）：断点续跑旧判据是
    「正式目录里有非空 shot_XX.png ⟺ 上一轮该镜已通过质检」。该等式在软放行引入
    （2026-10-08）之后**不再成立** —— 软放行的图同样写进正式目录（见下方软放行分支），
    于是「质检不达标但被放行」的图会被下一轮当成**已达标**跳过、**永不重画**
    （实测：逆天系统_全书 shot_11 带 critical_issues，shot_11.png mtime 停在
    2026-10-12 05:49:53，此后每一轮都 skip，日志「质量=未交付 墙钟 1.2s」）。

    证据优先级（fail-open：任何异常都返回「非软放行」，不改变既有的跳过行为）：
      ① 产物旁 <shot>.meta.json（_sb_artifact_meta_soft —— 最耐久的那份证据）；
      ② 上一轮 manifest 条目 prev（meta 缺失时的兜底）。

    返回 {"soft": bool, "critical_issues": [...]}。
    """
    try:
        facts = _sb_artifact_meta_soft(dst, seq, base_dir) or {}
    except Exception:  # noqa: BLE001
        facts = {}
    soft = bool(facts.get("soft"))
    ci = _sb_critical_issues(facts)
    if not soft and not ci and isinstance(prev, dict):
        prev_qc = prev.get("qc") if isinstance(prev.get("qc"), dict) else {}
        prev_gate = prev.get("qc_gate") if isinstance(prev.get("qc_gate"), dict) else {}
        soft = _sb_soft_accepted(prev)
        ci = _sb_critical_issues(prev_gate, prev_qc, prev)
    return {"soft": soft, "critical_issues": ci}


def _sb_item_seq(item, fallback=None):
    """条目 → 镜号（优先显式 seq，其次从 shot_id/file 里抽数字）。"""
    if isinstance(item, dict):
        for k in ("seq", "shot_seq", "index"):
            try:
                v = int(item.get(k))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
        for k in ("shot_id", "file"):
            s = str(item.get(k) or "")
            digits = "".join(ch for ch in s.split(".")[0] if ch.isdigit())
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    pass
    return fallback


def _sb_quality_tally(items, base_dir: str = "", use_artifact_meta: bool = True) -> dict:
    """分镜条目 → 三类质量计数（**T12 统一口径**，manifest / API / UI 共用）。

    口径（互斥且完备）：
      · soft_accepted —— 质检未达标但已放行入库（**不计入 success**）
      · qc_blocked    —— 质检阻断、未入库
      · success       —— 真正通过质检并入库
    另给 critical_issue_count（软放行中带非空 critical_issues 的镜数）与
    delivered_count（success + soft，即「正式目录里确实有图」的镜数）。

    `use_artifact_meta`：item 自身没有软放行标记时，是否回落到产物旁 meta.json。
    worker 当轮统计用 False（每条都带标记，免多余 IO）；历史 manifest 重统计用 True。
    """
    total = success = soft = blocked = crit = 0
    soft_ids, crit_ids = [], []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        total += 1
        _seq = _sb_item_seq(it)
        _is_soft = _sb_soft_accepted(it)
        _ci = _sb_critical_issues(it.get("qc_gate") or {}, it.get("qc") or {},
                                  it.get("qc_extra") or {})
        if use_artifact_meta and not _is_soft:
            _fact = _sb_artifact_meta_soft(it.get("file") or "", _seq, base_dir)
            _is_soft = bool(_fact.get("soft"))
            if not _ci:
                _ci = list(_fact.get("critical_issues") or [])
        if not _ci:
            _ci = _sb_critical_issues(it.get("qc") or {})
        if _is_soft:
            soft += 1
            soft_ids.append(it.get("shot_id"))
            if _ci:
                crit += 1
                crit_ids.append(it.get("shot_id"))
        elif it.get("qc_blocked"):
            blocked += 1
        elif it.get("success"):
            success += 1
    return {"total": total,
            "success_count": success,
            "soft_accepted_count": soft,
            "qc_blocked_count": blocked,
            "critical_issue_count": crit,
            "delivered_count": success + soft,
            "soft_shot_ids": soft_ids,
            "critical_shot_ids": crit_ids}


def _sb_record_stage_analytics(project_name: str, episode_no, shots: list) -> None:
    """把分镜逐镜 phase_secs / 墙钟聚合成一条 analytics 事件（Phase 0.4，fail-open）。

    phase_secs 是 _prep_shot 落进每个 item 的「前置段逐阶段耗时」字典（增强/预检/
    九宫格规划/3D 基准图…）。此前这些数字只写在日志里；这里按阶段求和并给出每镜
    均值与「GPU 段≈墙钟−前置段」，写进 output/analytics/events.jsonl 的 meta，
    供成本看板与后续提速分析（2026-10-13 减时分析）用。
    统计是可观测性、不是业务：任何异常都不得影响分镜结果（调用点另加 try/except）。
    """
    import analytics   # 延迟导入：analytics→config，避免与加载期依赖顺序耦合
    phase_sum: dict = {}
    wall_secs, prep_secs = [], []
    for it in shots or []:
        ps = it.get("phase_secs")
        if isinstance(ps, dict):
            for k, v in ps.items():
                try:
                    phase_sum[k] = phase_sum.get(k, 0.0) + float(v or 0.0)
                except (TypeError, ValueError):
                    continue
        for key, acc in (("elapsed_sec", wall_secs), ("phase_secs_total", prep_secs)):
            try:
                acc.append(float(it.get(key) or 0.0))
            except (TypeError, ValueError):
                continue
    n = len(shots or [])
    wall_total = round(sum(wall_secs), 1)
    prep_total = round(sum(prep_secs), 1)
    meta = {
        "shots": n,
        "phase_secs_sum": {k: round(v, 1) for k, v in sorted(phase_sum.items())},
        "prep_secs_sum": prep_total,
        "wall_secs_sum": wall_total,
        "wall_secs_mean": round(wall_total / n, 1) if n else 0.0,
        "gpu_secs_sum": round(max(wall_total - prep_total, 0.0), 1),
    }
    analytics.record_event("storyboard", project_name,
                           f"第{int(episode_no or 1)}集分镜（阶段耗时）",
                           duration_sec=wall_total, units=n, meta=meta)


def _sb_manifest_quality_fields(tally: dict) -> dict:
    """把三类计数摊成 manifest / 任务状态的字段（三层同名同值）。"""
    return {
        "success_count": tally["success_count"],
        "soft_accepted_count": tally["soft_accepted_count"],
        "qc_blocked_count": tally["qc_blocked_count"],
        "critical_issue_count": tally["critical_issue_count"],
        "delivered_count": tally["delivered_count"],
        "soft_accepted_shot_ids": tally["soft_shot_ids"],
        "critical_issue_shot_ids": tally["critical_shot_ids"],
    }


def _sb_normalize_soft_marks(items) -> int:
    """兜底规范化：任何「软放行却带 success=True / 缺 delivered」的条目就地纠正。

    为什么需要兜底（T12）：软放行标记可能通过**断点续跑复用旧条目信息**、
    单镜重跑合并、历史 manifest 回填等多条路径进入同一条目；只要有一处漏掉，
    界面就会重新把「放行了但有问题的镜」显示成成功。这里按软放行口径统一收口
    （success=False / delivered=True / 去掉 qc_blocked），返回被纠正的条目数。
    """
    fixed = 0
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        if _sb_soft_accepted(it) and (it.get("success") or not it.get("delivered")):
            it["success"] = False
            it["delivered"] = True
            it.pop("qc_blocked", None)
            fixed += 1
    return fixed


def _sb_clear_soft_marks(item) -> bool:
    """把条目上的软放行痕迹清干净（本次已**真通过**时调用）。

    为什么必须清（T12）：单镜重跑走 `target.update(entry)`，只覆盖 entry 里有的键 ——
    历史 item 上的 `qc_soft_accepted` / `qc_gate.soft_accepted` 会残留，于是
    「重跑已通过」的镜仍被判成软放行（界面永远带着警示，用户以为没修好）。
    """
    if not isinstance(item, dict):
        return False
    changed = False
    for k in ("qc_soft_accepted", "soft_accepted", "soft_reason", "soft_label"):
        if k in item:
            item.pop(k, None)
            changed = True
    for key in ("qc_gate", "qc"):
        _d = item.get(key)
        if isinstance(_d, dict) and (_d.get("soft_accepted") or _d.get("soft_released")):
            _d = dict(_d)
            _d.update({"soft_accepted": False, "soft_released": False})
            if key == "qc":
                _d.update({"delivered": True})
            item[key] = _d
            changed = True
    if item.get("delivered") and not _sb_soft_accepted(item):
        item["delivered"] = True
    return changed


def restat_storyboard_manifest(project: str, episode_no=None, write: bool = False) -> dict:
    """按 T12 口径**重新统计**某集分镜 manifest 的三类质量分（幂等、可重复执行）。

    · write=False（默认）：纯读统计，绝不改盘；
    · write=True：用统一口径回写 manifest 顶层计数，并把从产物 meta.json 回填到的
      软放行事实同步进对应 item（`qc_soft_accepted` / `qc.soft_accepted` /
      `qc.critical_issues`，且 `success` 置 False、`delivered` 置 True）——
      否则旧数据的软放行在界面/接口上不可见（实测 manifest 只剩 1 条标记、meta 有 17 条）。

    验收用法（生产数据）：
        set MJSCXT_DATA_DIR=<数据根> && python -c "import sys; sys.path.insert(0,'app');
        import json, storyboard_helpers as H;
        print(json.dumps(H.restat_storyboard_manifest('逆天系统_全书'), ensure_ascii=False))"
    """
    out = {"ok": False, "project": project, "episode_no": episode_no, "written": False}
    try:
        _d = _ep_read_dir(STORYBOARDS_DIR, project, episode_no)
        mpath = os.path.join(_d, "storyboard_manifest.json")
        out["episode_dir"] = _d
        out["manifest_path"] = mpath
        if not os.path.isfile(mpath):
            out["error"] = "分镜 manifest 不存在"
            return out
        manifest = read_json_strict(mpath, {})
        if not isinstance(manifest, dict):
            manifest = {}
        items = [s for s in (manifest.get("shots") or []) if isinstance(s, dict)]
        tally = _sb_quality_tally(items, base_dir=os.path.dirname(mpath),
                                  use_artifact_meta=True)
        out.update(_sb_manifest_quality_fields(tally))
        out["total"] = tally["total"]
        out["ok"] = True
        if not write:
            return out
        # ---- 回填 + 原子回写 ----
        for it in items:
            _seq = _sb_item_seq(it)
            _is_soft = _sb_soft_accepted(it)
            # ⭐ T13（必修3）：历史 manifest 里「软放行 + 沿用已达标图」的误导文案
            #    就地修正为事实表述（用户实测截图的直接来源）。
            _sb_fix_reuse_label(it)
            _fact = ({"soft": False, "critical_issues": []} if _is_soft
                     else _sb_artifact_meta_soft(it.get("file") or "", _seq,
                                                 os.path.dirname(mpath)))
            _ci = _sb_critical_issues(it.get("qc_gate") or {}, it.get("qc") or {})
            if not _ci:
                _ci = list(_fact.get("critical_issues") or [])
            if _is_soft or _fact.get("soft"):
                it["qc_soft_accepted"] = True
                it["delivered"] = True
                it["success"] = False
                it.pop("qc_blocked", None)
                _q = it.get("qc") if isinstance(it.get("qc"), dict) else {}
                _q.update({"soft_accepted": True, "delivered": True,
                           "accept": True, "blocked": False,
                           "entry_blocked": False, "asset_written": True})
                if _ci:
                    _q["critical_issues"] = _ci
                it["qc"] = _q
        manifest["shots"] = items
        manifest["total"] = tally["total"]
        manifest.update(_sb_manifest_quality_fields(tally))
        manifest["quality_restat_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(mpath, manifest)
        out["written"] = True
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
        logger.warning("分镜 manifest 重统计失败（%s 第%s集）：%s", project, episode_no, e)
    return out


def _sb_canvas_qc(sb_item, sb_file="", base_dir="", seq=None) -> dict:
    """canvas 卡片 storyboard.qc 的**规范化投影**（T25，2026-10-12）。

    用户实测（生产 API）：manifest 里 shots[].qc_soft_accepted 有 17 条、顶层
    soft_accepted_count=17，但 canvas 卡片的 storyboard 里**根本没有**这些字段
    （键集合只有 exists/url/path/qc/success/...），真通过卡的 qc 又只剩
    `{status:"skipped",...}` 这类旧字段；manifest 缺该镜时更是空对象 `{}` ——
    前端拿不到任何质量信号，「界面不再全绿」在 API 层没真正达成。

    本函数把 `item["qc"]` 原样保留后**补齐 T12 质量字段**，并保证任何情况下都
    不是空对象：soft_accepted / critical_issues / critical_issue_count /
    delivered / passed / status。键名与 manifest 顶层字段同义，前端可直接消费。
    """
    item = sb_item if isinstance(sb_item, dict) else {}
    qc = dict(item.get("qc")) if isinstance(item.get("qc"), dict) else {}
    _ci_own = _sb_critical_issues(item.get("qc_gate") or {}, qc, item)
    _soft = _sb_soft_accepted(item)
    _fact = {"soft": False, "critical_issues": []}
    # 历史条目（断点续跑精简形态）可能既无软放行标记也无 critical → 回落到产物旁 meta.json。
    #
    # ⚠️ 取舍（T25 明确）：meta 的软放行事实**可以把 item 的「通过」翻回软放行**。
    #   两个方向都出现过，必须选一个：
    #     · 漏报（真实软放行被显示成通过）—— 本任务要修的就是「界面骗人」，代价最大；
    #     · 误报（重跑已通过、但 meta 还是旧的软放行记录）—— 只多一个警示徽标。
    #   故选**保守方向**：meta 说软放行就算软放行。为了让误报能自愈，
    #   单镜重跑真通过时会**同步重写 meta**（见 _storyboard_retry_shot_impl，
    #   extra.qc_soft_accepted=False），因此该组合只会出现在历史数据里、且下次重跑即消失。
    if not _soft or not _ci_own:
        _fact = _sb_artifact_meta_soft(sb_file, seq, base_dir)
    _soft = bool(_soft or _fact.get("soft"))
    _ci = _ci_own or list(_fact.get("critical_issues") or [])
    _exists = bool(sb_file) and os.path.isfile(str(sb_file))
    _delivered = bool(item.get("delivered") or item.get("success") or _soft or _exists)
    qc["soft_accepted"] = _soft
    qc["delivered"] = _delivered
    qc["critical_issues"] = _ci
    qc["critical_issue_count"] = len(_ci)
    if not qc.get("status"):
        qc["status"] = ("soft_accepted" if _soft else
                        "blocked" if item.get("qc_blocked") else
                        "passed" if item.get("success") else
                        "skipped" if item.get("skipped") else "unknown")
    qc.setdefault("passed", bool(item.get("success")) and not _soft)
    if _soft:
        # 软放行：产物已入库但质检未达标 —— 与 worker / 回填口径一致（accept 但不算 passed）
        qc["accept"] = True
        qc["blocked"] = False
        qc["entry_blocked"] = False
        qc["asset_written"] = True
        qc["passed"] = False
    qc.setdefault("enabled", False)
    return qc


def sb_quality_parity(project: str, episode_no=None) -> dict:
    """回归断言（T25）：**canvas 卡片可见的计数 == manifest 顶层计数**。

    与 canvas 路由同源：读同一份 manifest，用同一 `_sb_quality_tally` 与
    `_sb_canvas_qc` 算出「前端实际能从卡片读到的」三类计数，再与 manifest 顶层的
    success_count / soft_accepted_count / qc_blocked_count / critical_issue_count 比对。
    任何一项不等 → `ok=False` 并给出两侧明细（供自检脚本 / 回归用例断言）。幂等只读。
    """
    out = {"ok": False, "project": project, "episode_no": episode_no}
    try:
        _d = _ep_read_dir(STORYBOARDS_DIR, project, episode_no)
        mpath = os.path.join(_d, "storyboard_manifest.json")
        out["episode_dir"] = _d
        out["manifest_path"] = mpath
        if not os.path.isfile(mpath):
            out["error"] = "分镜 manifest 不存在"
            return out
        manifest = read_json_strict(mpath, {}) or {}
        if not isinstance(manifest, dict):
            manifest = {}
        items = [s for s in (manifest.get("shots") or []) if isinstance(s, dict)]
        tally = _sb_quality_tally(items, base_dir=_d, use_artifact_meta=True)
        visible = {"success": 0, "soft": 0, "blocked": 0, "critical": 0}
        for it in items:
            qc = _sb_canvas_qc(it, it.get("file") or "", _d, _sb_item_seq(it))
            if qc.get("soft_accepted"):
                visible["soft"] += 1
                if qc.get("critical_issues"):
                    visible["critical"] += 1
            elif it.get("qc_blocked") or qc.get("blocked") is True:
                visible["blocked"] += 1
            elif qc.get("passed"):
                visible["success"] += 1
        man = {"success": manifest.get("success_count"),
               "soft": manifest.get("soft_accepted_count"),
               "blocked": manifest.get("qc_blocked_count"),
               "critical": manifest.get("critical_issue_count")}
        # ⭐ T25：同时给出**产物 meta.json 侧**的软放行计数（软放行事实的耐久源）——
        #   不变式是「canvas 可见 soft == meta.json 计数」，而不只是与 manifest 顶层相等
        #   （manifest 会被重跑重写、历史标记会丢，meta 才是随图落盘的那份）。
        meta_soft = meta_crit = 0
        for it in items:
            _f = _sb_artifact_meta_soft(it.get("file") or "", _sb_item_seq(it), _d)
            if _f.get("soft"):
                meta_soft += 1
                if _f.get("critical_issues"):
                    meta_crit += 1
        out.update({"manifest": man, "canvas_visible": visible,
                    "meta_json": {"soft": meta_soft, "critical": meta_crit},
                    "total": tally["total"]})
        _mism = [k for k in ("success", "soft", "blocked", "critical")
                 if man.get(k) != visible.get(k)]
        if meta_soft != visible["soft"]:
            _mism.append("meta_soft")
        if meta_crit != visible["critical"]:
            _mism.append("meta_critical")
        out["mismatches"] = _mism
        out["ok"] = not _mism
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out


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
    _ref_ids, _ref_paths = manifest_ref_maps(refs, project)
    # ⭐ T12（2026-10-12）：软放行的 gate 带 soft_accepted → 本条**不得**记 success。
    _entry_soft = bool(isinstance(gate, dict) and gate.get("soft_accepted"))
    entry = {
        "shot_id": shot_id, "success": not _entry_soft,
        "file": dst, "url": f"/api/storyboards/file/{_url_prefix}shot_{seq:02d}.png",
        "prompt": prompt, "ref_count": len(refs),
        # T3：与批量 worker 同口径 —— 记**资产身份串**（items/<项目>/<道具>/base.png）而不是
        # 统一画幅目录名。单镜重跑与批量重跑写的是同一个 manifest，两处口径必须一致，
        # 否则「这条镜头到底用了哪张道具图」会因为走哪条路径而不同。
        "refs": _ref_ids,
        "ref_paths": _ref_paths,
        "regenerated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regenerated": "single_shot_retry",
    }
    if verdict:
        entry["qc"] = {"enabled": True, "status": "pass" if (gate or {}).get("accept") else "blocked",
                       "label": (gate or {}).get("label"), "attempts": 1,
                       "score": verdict.get("score"), "verdict": verdict.get("verdict"),
                       "reason": verdict.get("reason")}
    if _entry_soft:
        # ⭐ T12：软放行事实同步进 item + item["qc"] —— canvas 直接透出 qc，
        #    前端据此显示「软放行」徽标并显著提示 critical_issues。
        entry["delivered"] = True
        entry["qc_soft_accepted"] = True
        _entry_qc = entry.get("qc") if isinstance(entry.get("qc"), dict) else {}
        _entry_qc.update({"soft_accepted": True, "delivered": True, "accept": True,
                          "blocked": False, "entry_blocked": False,
                          "asset_written": True,
                          "critical_issues": _sb_critical_issues(gate or {}, _entry_qc)})
        entry["qc"] = _entry_qc
    if target is not None:
        target.update(entry)
        # ⭐ T12：本次是**真通过**（非软放行）→ 清掉历史软放行痕迹，否则 update 只覆盖
        #   entry 里有的键，旧的 qc_soft_accepted / qc_gate.soft_accepted 会残留，
        #   重跑已通过的镜仍被判成软放行。
        if not _entry_soft:
            _sb_clear_soft_marks(target)
    else:
        items.append(entry)
    manifest.setdefault("project_name", project)
    manifest["shots"] = items
    manifest["total"] = len(items)
    # ⭐ T12：三类分开统计（软放行不计入 success）。条目自身没有标记时回落到产物旁的
    #    <shot>.meta.json —— 否则历史软放行会被当成「真通过」，界面重新变全绿。
    _sb_normalize_soft_marks(items)
    _tally = _sb_quality_tally(items, base_dir=os.path.dirname(mpath),
                               use_artifact_meta=True)
    manifest.update(_sb_manifest_quality_fields(_tally))
    manifest["quality_schema"] = 2
    manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        # B-2：与 worker 侧统一走 fs_atomic（唯一临时名 + fsync + .bak 快照 + replace 重试），
        # 避免单镜重跑与批量分镜 worker 并发写同一 manifest 互相截断。
        atomic_write_json(mpath, manifest)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"分镜 manifest 更新失败：{e}")


def _identity_views_for(char_img: str, outfit_override: str = "") -> list:
    """给定「本镜命中的那张角色图」+「本集服装变体 key」，返回该角色可用视图（去重保序）。

    ⭐ 2026-10-13 用户口径：网格一格 = 一张视图/服装变体，同一角色占同一行 ——
    所以这里要的不是「一张」，而是该角色**这一集实际可用的全部视图**。
    实测资产结构（逆天系统_全书）每角色恰有 2 张：
      characters/<proj>/<角色>/base.png                      ← 基础设定图（四区 character sheet）
      characters/<proj>/<角色>/outfits/ep01_<hash>/base.png  ← 本集服装变体（t16 服装覆盖）
    outfit_override 来自 episode_helpers._episode_outfit_overrides（t16「本集服装覆盖」表）。
      · 有 override → 返回 [本集变体, 主设定图]（本集服装优先，剧情相关性更强）；
      · 命中变体但无 override → 至少补回主设定图；
      · 两者都没有 → 只有命中那一张（调用方按门槛判断建不建网格）。
    ⚠️ 不能用 qc_helpers._shot_outfit_dir 代替：分镜侧它恒返回空
       （实测第 1/2/3 集 0/46、0/39、0/39，见 L1859 注释）。
    """
    out = []
    p = str(char_img or "")
    if p and os.path.isfile(p):
        out.append(p)
    try:
        _parts = os.path.normpath(p).split(os.sep) if p else []
        _leaf = os.path.basename(p) or "base.png"
        _in_outfit = "outfits" in _parts
        _char_dir = (os.sep.join(_parts[:_parts.index("outfits")]) if _in_outfit
                     else os.path.dirname(p))
        _variant = ""
        if _char_dir and outfit_override:
            _vdir = os.path.join(_char_dir, "outfits", str(outfit_override))
            if os.path.isdir(_vdir):
                for _cand in (_leaf, "base.png", "front.png", "half.png"):
                    _cp = os.path.join(_vdir, _cand)
                    if os.path.isfile(_cp):
                        _variant = _cp
                        break
        if _variant and _variant not in out:
            out = [_variant] + out          # 本集服装优先
        elif _in_outfit:
            _base_img = os.path.join(_char_dir, _leaf)
            if os.path.isfile(_base_img) and _base_img not in out:
                out.append(_base_img)
    except Exception:  # noqa: BLE001
        pass
    return out


def _identity_views_for_dir(char_dir: str, outfit_override: str = "",
                            episode_no=None) -> list:
    """从**角色资产目录**收集视图：本集服装变体（优先）+ 主设定图。

    与 _identity_views_for（走「命中图路径」）互补：覆盖表为空、或命中图不带 outfits/
    时，直接从资产目录拿得到两张图 —— 实测 shot=13 正是这情况（覆盖表空 → 只 1 视图
    → 门槛不满足 → 不建网格）。变体定位顺序：覆盖表 key → 本集前缀 epNN_ 取最新一个。
    """
    out = []
    if not char_dir or not os.path.isdir(char_dir):
        return out
    _vdir = ""
    try:
        if outfit_override:
            _d = os.path.join(char_dir, "outfits", str(outfit_override))
            if os.path.isdir(_d):
                _vdir = _d
        if not _vdir:
            try:
                _ep = int(episode_no or 0)
            except (TypeError, ValueError):
                _ep = 0
            _oroot = os.path.join(char_dir, "outfits")
            if _ep > 0 and os.path.isdir(_oroot):
                _pref = "ep%02d_" % _ep
                for _d in sorted(os.listdir(_oroot)):
                    _p = os.path.join(_oroot, _d)
                    if _d.startswith(_pref) and os.path.isdir(_p):
                        _vdir = _p          # 同集多个时取排序最后一个（最新）
    except Exception:  # noqa: BLE001
        pass
    for _d in ([_vdir] if _vdir else []) + [char_dir]:
        for _cand in ("base.png", "front.png", "half.png"):
            _p = os.path.join(_d, _cand)
            if os.path.isfile(_p) and _p not in out:
                out.append(_p)
                break
    return out


def _identity_grid_layout(n: int) -> tuple:
    """按「不同角色基准图张数」选身份基准网格的行列（2026-10-13 用户口径）。

    用户原话：「收尾镜可以动态调整成 2x3、1x3、2x2 等等」。单帧输出不需要与输出
    同构的 3×3 九格，网格只承担「外观基准」职责，因此形状随素材张数动态取：
      n ≤ 3 → n×1（1×1 / 1×2 / 1×3）、n = 4 → 2×2、n ≤ 6 → 3×2（2 行 3 列）、
      n ≥ 7 → 3×3（上限 9 格，超出由调用方/填充逻辑截断）。
    九宫格输出仍固定 3×3（与输出同构、逐格绑定），见 _build_identity_ref_grid 的
    repeat 参数语义。
    """
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 1
    if n <= 3:
        return (n, 1)
    if n == 4:
        return (2, 2)
    if n <= 6:
        return (3, 2)
    return (3, 3)


def _build_identity_ref_grid(char_paths: list, out_path: str, target_size=None,
                             cols: int = 3, rows: int = 3, repeat: bool = True,
                             rows_paths: list = None) -> str:
    """把角色基准图拼成「身份基准网格」参考图（2026-10-06 起；2026-10-13 支持动态布局）。

    ⭐ 2026-10-13（用户口径「具体比例要根据具体剧情来」）：**新路径 rows_paths** =
      [[角色A的视图…], [角色B的视图…]]，**同一行 = 同一角色**：行数 = 出场角色数、
      列数 = 出场角色里最多的视图数。形状因此由**剧情**（这一镜出场谁、各自有几张
      视图/服装变体）决定，而不是按「总张数」套固定比例表。
      例：1 角色 × 2 视图（基础设定图 + 本集服装变体）→ 1×2；2 角色 × 2 视图 → 2×2；
      3 角色 × 2 视图 → 3×2。格内走 _fit_ref_to_canvas（cover，保比例居中裁剪），
      避免把角色设定图拉伸变形。

    cols/rows（旧路径）：网格行列，仍服务九宫格输出（3×3 与输出同构、逐格绑定）。
    repeat：是否按顺序循环填满全部格位。
      · True（九宫格路径）：多角色/多视图依次轮转填满（旧行为逐字不变）；
      · False：不重复，空格留背景色 —— 同图拷贝会被扩散模型当成「输出范例」诱导趋同
        （2026-10-07 事故结论）。

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

    # ⭐ 2026-10-13 按「内容」缓存（用户口径：身份基准网格按角色缓存）：
    #    out_path 按 shot 序号命名，但网格内容只取决于「角色视图 + 尺寸 + 布局」——
    #    同一批角色的镜头内容逐像素相同，此前每镜都重拼（实测 0.5s/镜）。
    #    key 含各源图 mtime：角色设定图一旦更新，缓存自动失效。
    _cache_key = None
    try:
        _srcs = [p for _g in (rows_paths or []) for p in (_g or [])]
        if not _srcs:
            _srcs = list(char_paths or [])
        _cache_key = (
            tuple((os.path.normcase(os.path.abspath(p)), int(os.stat(p).st_mtime_ns))
                  for p in _srcs if p and os.path.isfile(p)),
            tuple(int(_x) for _x in (target_size or (0, 0))),
            int(cols), int(rows), bool(repeat),
        )
        _hit = _IDENTITY_GRID_CACHE.get(_cache_key)
        if _hit and os.path.isfile(_hit):
            return _hit
    except Exception:  # noqa: BLE001  缓存是加速手段，任何异常都退回正常拼接
        _cache_key = None

    # ---- 新路径（2026-10-13）：按「角色分行 × 视图分列」铺网格（剧情驱动）----
    if rows_paths:
        groups = []
        for _row in rows_paths:
            _g = [p for p in (_row or []) if p and os.path.isfile(p)]
            if _g:
                groups.append(_g)
        if not groups:
            raise ValueError("没有可用的角色基准图")
        cols = max(len(g) for g in groups)
        rows = len(groups)
        w, h = (int(target_size[0]), int(target_size[1])) if target_size else (1632, 928)
        xs = [int(round(w * i / cols)) for i in range(cols + 1)]
        ys = [int(round(h * i / rows)) for i in range(rows + 1)]
        xs[0], ys[0], xs[-1], ys[-1] = 0, 0, w, h
        sheet = Image.new("RGB", (w, h), (12, 12, 16))
        for _r, _g in enumerate(groups):
            for _c, _p in enumerate(_g[:cols]):
                try:
                    _im = Image.open(_p).convert("RGB")
                except Exception:  # noqa: BLE001  单张读失败 → 该格留背景，不阻断
                    continue
                sheet.paste(
                    _fit_ref_to_canvas(_im, (xs[_c + 1] - xs[_c], ys[_r + 1] - ys[_r])),
                    (xs[_c], ys[_r]))
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        sheet.save(out_path, "PNG")
        if _cache_key is not None:
            _IDENTITY_GRID_CACHE[_cache_key] = out_path
        return out_path

    # ---- 旧路径：按去重后的图循环/截断填充（九宫格输出，行为逐字不变）----
    paths = [p for p in (char_paths or []) if p and os.path.isfile(p)]
    if not paths:
        raise ValueError("没有可用的角色基准图")
    try:
        cols = max(1, int(cols))
        rows = max(1, int(rows))
    except (TypeError, ValueError):
        cols, rows = 3, 3
    w, h = (int(target_size[0]), int(target_size[1])) if target_size else (1632, 928)
    # 格边界：末列/末行吸收整除余数，保证整图尺寸与 target_size 逐像素一致
    xs = [int(round(w * i / cols)) for i in range(cols + 1)]
    ys = [int(round(h * i / rows)) for i in range(rows + 1)]
    xs[0], ys[0], xs[-1], ys[-1] = 0, 0, w, h
    sheet = Image.new("RGB", (w, h), (12, 12, 16))
    for i in range(cols * rows):
        if not repeat and i >= len(paths):
            break          # 单帧动态网格：不重复填充，空格留背景色
        r, c = divmod(i, cols)
        im = Image.open(paths[i % len(paths)]).convert("RGB").resize(
            (xs[c + 1] - xs[c], ys[r + 1] - ys[r]))
        sheet.paste(im, (xs[c], ys[r]))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.save(out_path, "PNG")
    if _cache_key is not None:
        _IDENTITY_GRID_CACHE[_cache_key] = out_path
    return out_path


def _ref_canvas_target(size):
    """把目标画幅规整成 (W, H) 正整数元组；None / 非法 → None（调用方按「不处理」走）。"""
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def _edge_bg_color(im):
    """取图像边缘的背景主色（左右各 4% 宽竖条的中位 RGB；失败返回纯白）。"""
    try:
        import numpy as _np
        _arr = _np.asarray(im.convert("RGB"))
        _w = _arr.shape[1]
        _e = max(2, int(_w * 0.04))
        _strip = _np.concatenate(
            [_arr[:, :_e].reshape(-1, 3), _arr[:, -_e:].reshape(-1, 3)], axis=0)
        _med = _np.median(_strip, axis=0).astype(int)
        return (int(_med[0]), int(_med[1]), int(_med[2]))
    except Exception:  # noqa: BLE001
        return (255, 255, 255)


def _fit_ref_to_canvas(im, size, kind=""):
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
    left = (scaled.width - W) // 2
    # ⭐ 2026-10-16（t17）**竖构图人物图保头脸** —— 修复「参考图只剩腰」：
    #   原实现一律居中裁剪。对 1:3 竖版角色立绘（例：资产增强产物 hd/front.png
    #   736×2144）cover 到 1664×928 时垂直只保留 19%（2144→4848 后取中间 928），
    #   **头和脸整个被裁掉**，只剩衣服下摆 —— 角色身份锚点直接失效。
    #   实测证据：output/qc/<项目>/ref_canvas/front_*_1664x928.png 只有腰部。
    #   改为「从头顶下方起裁」，保住头脸 + 上半身（分镜判定身份靠的正是这两处）。
    #   仅对**人物类**且**明显竖构图**（高 > 宽×1.35）生效，场景/道具/横图行为不变。
    _kind = str(kind or "")
    _is_char = ("角色" in _kind) or _kind.lower().startswith("char")
    # ⭐ 2026-10-18 **人物类竖图改为「按高度贴合 + 两侧自身模糊填充」**：
    #   修复「参考图只剩一张脸」。实测：350×450 的半身立绘 cover 到 1664×928 时
    #   放大 4.75×（高度 2138），再从头顶裁 928 → 只留头部；模型拿不到服装/体态，
    #   于是把两个角色都画成同一张脸（QC 反复报「两个完全相同的羡进」）。
    #   旧顾虑「模型模仿居中布局」用「自身模糊放大作底」规避：底是同一张图的模糊
    #   版本，不是纯色带，模型不会把它当作分栏虚化带。
    if _is_char and im.height > im.width * 1.15:
        _s = H / im.height
        _fg = im.resize((max(1, int(round(im.width * _s))), H), Image.LANCZOS)
        if _fg.width >= W:
            _l = (_fg.width - W) // 2
            return _fg.crop((_l, 0, _l + W, H))
        # 两侧背景（2026-10-18 二次修正）：**绝不用「自身模糊放大」当底** ——
        #   实测那张模糊底在参考图里又出现了 1~2 个同款人物轮廓，模型照着画出了
        #   「两个赵天霸 / 两个羡进」（质检反复判「人物重复」，尝试 3 次全挂）。
        #   改为用**图像自身边缘的背景色**（角色设定图多为纯色/浅色底）填充：
        #   不引入任何人物轮廓，同时保留「按高度贴合、不切头脸」的修复。
        _bg = Image.new("RGB", (W, H), _edge_bg_color(im))
        _bg.paste(_fg, ((W - _fg.width) // 2, 0))
        return _bg
    if _is_char and scaled.height > H * 1.35:
        top = int((scaled.height - H) * 0.06)   # 头顶留 6% 余量，不切到发顶
    else:
        top = (scaled.height - H) // 2
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
                _REF_CANVAS_ORIGIN[os.path.normcase(os.path.abspath(cached))] = os.path.abspath(local)
            else:
                import hashlib
                import tempfile
                from PIL import Image
                with Image.open(local) as _im:
                    _rgb = _im.convert("RGB")
                    if (_rgb.width, _rgb.height) == tgt:
                        unified.append(item)
                        continue
                    fixed = _fit_ref_to_canvas(_rgb, tgt, kind)
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
                # T3：记下「产物 → 源图」，供 manifest 身份串与道具固定参考图核对使用
                _REF_CANVAS_ORIGIN[os.path.normcase(os.path.abspath(_dst))] = os.path.abspath(local)
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


# ===================== 道具固定参考图（读侧） + 异常物件过渡镜（T3，2026-10-12） =====================
#
# 两件事都在「分镜出图之前」这一步收口：
#   ① 道具固定参考图：把本镜道具槽位的图**重指到 asset_worker 钉住的那张固定参考图**
#      （resolve_item_reference），并把「资产身份串」写进 manifest —— 于是「第 12 镜与
#      第 26 镜用的是同一张员工卡」这件事可以被**直接核对**，不必再反查哈希。
#      写侧（pin）在 asset_worker；这里只做读侧对齐，不改 H3 工作流槽位协议（非目标）。
#   ② 异常物件过渡镜：用户反馈「渗血洞缺过渡镜头」——异常物件（血洞/渗血这类）
#      在某镜里**凭空出现**。这里给出一套确定性判定：在异常首次出现的镜头序列里
#      指明**由哪一个 shot_id 承担过渡作用**（前镜铺垫 / 本镜自身运镜 / 需要显式过渡），
#      需要显式过渡时把「过渡」写进本镜提示词与 manifest 记录。
#
# ⚠️ 为什么**不**新增一个镜头：分镜序号 _shot_seq 是**位置下标**，落盘名 shot_NN.png
#    也由它决定（本文件 _storyboard_worker）。在镜表中段插一镜会让其后所有截图号整体
#    位移 —— 已入库的 shot_NN.png 会与新镜号错配，正在跑的集断点续跑会静默拿错图。
#    所以这里只做「指明 + 写进提示词/manifest」，把结构性插入留给剧本层
#    （plan_transition_shot 返回的 insert_shot 结构可直接交给剧本层使用）。

#: 异常物件关键词（首现即需要过渡）。
#:
#: ⚠️ t3 的原表只有「渗血洞/血洞/渗血/血孔/血痕/渗血孔/血洞孔」—— 那是**用户口述的视觉
#: 描述**，实测在两个真实项目剧本里命中 **0 处**，所以「渗血洞缺过渡」这个能力一直是空
#: 操作。剧本里的真实用词是「小洞」（用户看到的画面＝渗血的洞，剧本写的是纸面上的小洞）。
#: t16 按真实用词补表，并把「哪些词被拒绝、为什么」一并钉在这里（见 REJECTS）。
TRANSITION_ANOMALY_KEYWORDS = (
    # —— 视觉族（用户反馈口径；语义强、误伤面窄，保留）
    "渗血洞", "血洞", "渗血", "血孔", "血痕", "渗血孔", "血洞孔",
    # —— 剧本真实用词族（t16 补：实测命中数见 NOTES）
    "小洞", "破洞", "孔洞",
)

#: 每个关键词的**来源与登记理由**（与道具别名表同构：显式登记，禁止自动收词）。
#: hits = 在两项目真实剧本（进境_整本小说 第1集 / 逆天系统_全书 第1、2集）里的实测命中数。
TRANSITION_ANOMALY_KEYWORD_NOTES = {
    "渗血洞": {"hits": 0, "source": "用户 2026-10-12 成片质检报告原文用词",
               "reason": "用户口述的视觉描述；剧本/小说原文 0 命中，保留以便日后续写命中。"},
    "血洞": {"hits": 0, "source": "同上（视觉族）", "reason": "视觉族同义扩展。"},
    "渗血": {"hits": 0, "source": "同上（视觉族）", "reason": "视觉族同义扩展。"},
    "血孔": {"hits": 0, "source": "同上（视觉族）", "reason": "视觉族同义扩展（「孔」与「洞」同族）。"},
    "血痕": {"hits": 0, "source": "同上（视觉族）", "reason": "视觉族同义扩展。"},
    "渗血孔": {"hits": 0, "source": "同上（视觉族）", "reason": "视觉族同义扩展。"},
    "血洞孔": {"hits": 0, "source": "同上（视觉族）", "reason": "视觉族同义扩展。"},
    "小洞": {"hits": 7,
             "source": "output/scripts/进境_整本小说/第1集.json 全文 7 处；原著 novels/20261010_184008_8d5152.txt「最后一笔把纸戳出了一个小洞」",
             "reason": "剧本实际用词。命中镜 shot#25/26（守则纸面被红笔戳出的小洞）—— 即用户所说的「渗血洞」。"},
    "破洞": {"hits": 0, "source": "同族补词",
             "reason": "与「小洞」同族的明确开口用词；两项目现行剧本 0 命中，不产生误命中。"},
    "孔洞": {"hits": 0, "source": "同族补词",
             "reason": "与「小洞」同族的明确开口用词；两项目现行剧本 0 命中，不产生误命中。"},
}

#: **明确不收**的词（含实测误命中证据）—— 收了就会误伤，改词表前先看这里。
TRANSITION_ANOMALY_KEYWORD_REJECTS = {
    "洞": "单字太泛：真实剧本里会命中 shot#19「猫眼黑洞洞」、shot#53「门洞」，"
          "以及逆天系统_全书的「洞穴」（修炼场所）/「洞察力」/「漏洞」（抽象词）。",
    "黑洞": "会被 shot#19「猫眼黑洞洞」命中 —— 猫眼不是异常开口。",
    "门洞": "shot#53「画面右侧原本漆黑的门洞已经不见」：门洞是建筑构件，且该异常属于「门消失」，"
            "不是渗血洞类首现，混进来会让过渡要求挂错镜。",
    "洞口": "会与「门洞」等正常开口相邻命中，语义不纯。",
    "洞穴": "逆天系统_全书里「洞穴」＝修炼/传承场所（正常场景），命中即误判。",
    "漏洞": "抽象词（守则漏洞 / 系统漏洞），与画面异常无关。",
}


def audit_transition_keywords(shots, keywords=None):
    """关键词命中**反向检查**（给验收/排查用：列出哪些镜头会被哪些词命中）。

    返回 {"keywords", "hits", "rejects", "reject_hits"}：
      \u00b7 hits         —— 用当前（或给定）关键词表命中的镜号 + 命中词 + 上下文摘录；
      \u00b7 reject_hits  —— 用**被拒绝的词**试跑，证明它们确实会误命中（所以不收）。
    只读，不判定、不改镜头。
    """
    kws = tuple(keywords or TRANSITION_ANOMALY_KEYWORDS)
    rej = tuple(TRANSITION_ANOMALY_KEYWORD_REJECTS)
    hits, reject_hits = [], []
    for shot in (shots or []):
        if not isinstance(shot, dict):
            continue
        text = _shot_text(shot)
        if not text:
            continue
        for kw in kws:
            if kw in text:
                pos = text.find(kw)
                hits.append({"shot_id": shot.get("shot_id"), "keyword": kw,
                             "excerpt": text[max(0, pos - 24):pos + len(kw) + 24]})
                break
        for kw in rej:
            if kw in text:
                pos = text.find(kw)
                reject_hits.append({"shot_id": shot.get("shot_id"), "keyword": kw,
                                    "excerpt": text[max(0, pos - 24):pos + len(kw) + 24]})
                break
    return {"keywords": list(kws), "hits": hits,
            "rejects": dict((k, TRANSITION_ANOMALY_KEYWORD_REJECTS[k]) for k in rej),
            "reject_hits": reject_hits}

#: 过渡/铺垫措辞：命中即说明该镜**本身**承担了「把观众引到异常出现处」的作用
TRANSITION_SETUP_MARKERS = (
    "推", "推入", "推进", "推近", "拉", "拉远", "摇", "移", "跟随", "跟拍", "环绕",
    "升降", "俯", "仰", "扫过", "环视", "转场", "切换", "淡入", "渐显", "渐入",
    "定格", "特写", "逼近", "靠近", "由远及近", "拉近",
)

#: ⚠️【有意设计 · 不要「修」】role="predecessor_setup"（以及 self_setup）返回
#: needed=False / 不补镜，是**刻意如此**，不是漏做。改前请先读完：
#:   ① 判为 predecessor_setup 意味着该异常**已有过渡承担者**（真实样本：shot#25 纸面
#:      「小洞」首现，由 shot#24 的「镜头下推」铺垫承担过渡）。用户反馈的「渗血洞无过渡」，
#:      其机制是 t3 词表在剧本里 0 命中导致**从未触发**——t16 补词后已能识别且有承担者，
#:      问题已解决，不是「还缺一镜」。
#:   ② 把 predecessor_setup 改成需补镜属**策略变更**，且会牵动分镜序号：_shot_seq 是
#:      **位置下标**、落盘名 shot_NN.png 由它决定，插镜会让其后所有截图号整体位移，
#:      已入库的 shot_NN.png 与新镜号错配、断点续跑静默拿错图（与本节开头 t3 的取舍同源）。
#:   ③ 将来真要显式补镜：**开剧本层任务在 shots 数组里插**，不要在分镜层动序号。
#:      build_transition_shot() 返回的 insert_shot 结构就是给剧本层用的。

#: 策略表（dict 而非散落的模块常量：阈值必须成套看，也不会被 config_doctor 当成
#: 「未登记的可配置常量」—— 它们是算法标定值，不是用户可调项）
TRANSITION_SHOT_POLICY = {
    "enabled": True,
    "min_shot_chars": 6,        # 镜头文本太短不作判定（避免误判）
    "max_targets_per_episode": 4,  # 一集最多处理 4 处异常首现（防提示词膨胀）
}


#: 统一产物 → 源图 的反查结果缓存（同名产物重复出现时零磁盘扫描）
_REF_CANVAS_REVERSE_CACHE: dict = {}

#: 统一产物命名：<stem>_<sha1(源图绝对路径)前8>_<W>x<H>.png（见 _unify_ref_canvas）
_REF_CANVAS_NAME_RE = re.compile(r"__(?P<h>[0-9a-f]{8})__")

def _ref_canvas_origin_by_scan(path: str, project_name: str) -> str:
    """进程内映射缺失时的兜底：按产物名里的 sha1(源图绝对路径)[:8] 在项目资产里反查源图。

    为什么需要：_REF_CANVAS_ORIGIN 是**进程内**的。重跑分镜（新进程）时旧产物还在
    output/qc/<项目>/ref_canvas/ 下，只凭文件名（没有源路径，只有 8 位哈希）原本无法
    还原身份；有了这个反查，跨进程/跨批次也能把 manifest 的资产身份写对。
    代价可控：只对**未被进程内映射覆盖**的路径调用一次，结果进缓存；扫描范围限于本项目
    的 items/characters/scenes 三个目录。
    """
    base = os.path.basename(str(path or ""))
    m = _REF_CANVAS_NAME_RE.search(base)
    if not m:
        # 兼容 _unify_ref_canvas 的真实命名：base_<8位hex>_1664x928.png
        m = re.search(r"_(?P<h>[0-9a-f]{8})_[0-9]+x[0-9]+[.]png$", base)
    if not m:
        return ""
    want = m.group("h")
    ck = (str(project_name or ""), want)
    if ck in _REF_CANVAS_REVERSE_CACHE:
        return _REF_CANVAS_REVERSE_CACHE[ck]
    import hashlib
    found = ""
    for root in (ITEMS_DIR, CHARACTERS_DIR, SCENES_DIR):
        proj_dir = os.path.join(root, str(project_name or ""))
        if not os.path.isdir(proj_dir):
            continue
        for dirpath, _dn, fns in os.walk(proj_dir):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() not in _ASSET_IMG_EXTS:
                    continue
                cand = os.path.join(dirpath, fn)
                try:
                    h = hashlib.sha1(os.path.abspath(cand).encode("utf-8")).hexdigest()[:8]
                except Exception:  # noqa: BLE001
                    continue
                if h == want:
                    found = os.path.abspath(cand)
                    break
            if found:
                break
        if found:
            break
    _REF_CANVAS_REVERSE_CACHE[ck] = found
    return found


def ref_source_path(path: str, project_name: str = "") -> str:
    """参考图路径 → **源图**路径（画幅统一产物会反查回它的源资产图；未知则原样返回）。

    反查顺序：① 本进程 _unify_ref_canvas 记下的映射（首选，零成本）；
              ② 按产物名里的哈希在项目资产目录反查（跨进程/历史批次，带缓存）。
    """
    p = os.path.abspath(str(path or ""))
    if not p:
        return ""
    hit = _REF_CANVAS_ORIGIN.get(os.path.normcase(p))
    if hit:
        return hit
    return _ref_canvas_origin_by_scan(p, project_name) or p


def asset_ref_identity(path: str, project_name: str = "") -> str:
    """参考图路径 → **稳定资产身份串**（items/<项目>/<道具>/base.png）。

    统一产物先反查源图再算身份；不是资产路径（身份网格 / 上一镜衔接 / 3D 基准图等）
    回落旧口径「所属目录名」—— 旧口径对那几类本来就有意义，且保持 manifest 兼容。
    """
    src = ref_source_path(path, project_name)
    try:
        from asset_worker import item_ref_identity   # 延迟导入：asset_worker 顶层 import 本模块
        ident = item_ref_identity(src, project_name)
        if ident:
            return ident
    except Exception:  # noqa: BLE001 —— 身份解析失败绝不阻断分镜
        pass
    return os.path.basename(os.path.dirname(src)) if src else ""


def pin_item_refs_in_refs(refs: list, project_name: str) -> tuple:
    """把本镜**道具槽位**的参考图对齐到道具的固定参考图（读侧，fail-open）。

    返回 (新 refs, 记录列表)。记录形如
    {"label", "from", "to", "identity", "changed", "error"}，供 manifest / 日志核对。
    为什么只动道具：角色有衣柜（换装是合法变化）、场景有九宫格多机位（同一场景多张图
    是设计），道具才是「同一物件反复出现必须一模一样」的那一类。
    """
    out, notes = [], []
    proj = str(project_name or "").strip()
    for entry in (refs or []):
        try:
            kind, label, path = entry[0], entry[1], entry[2]
        except (TypeError, IndexError, KeyError):
            out.append(entry)
            continue
        ident = asset_ref_identity(path, proj)
        if not ident.startswith("items/"):
            out.append(entry)
            continue
        item_name = ident.split("/")[2] if len(ident.split("/")) > 2 else ""
        note = {"label": label, "item": item_name, "from": path, "to": path,
                "identity": ident, "changed": False, "error": ""}
        try:
            from asset_worker import resolve_item_reference   # 延迟导入（见上）
            got = resolve_item_reference(proj, item_name)
            newp = got.get("path") or ""
            if newp and os.path.isfile(newp):
                note["identity"] = got.get("identity") or ident
                note["pinned"] = bool(got.get("pinned"))
                note["source"] = got.get("source") or ""
                if os.path.normcase(os.path.abspath(newp)) != os.path.normcase(os.path.abspath(str(path))):
                    note["changed"] = True
                    note["to"] = newp
                out.append((kind, label, note["to"]))
                notes.append(note)
                continue
            note["error"] = got.get("error") or "固定参考图不可用"
        except Exception as e:  # noqa: BLE001
            note["error"] = "%s: %s" % (type(e).__name__, e)
        out.append(entry)
        notes.append(note)
    return out, notes


def manifest_ref_maps(refs: list, project_name: str) -> tuple:
    """refs → (身份映射 {标签: 资产身份串}, 路径映射 {标签: 源图绝对路径})。

    身份映射写进 manifest 的 refs（**可比对、与绝对路径无关**）；路径映射写进
    ref_paths（本地排查用）。两者都由**源图**算出 —— 统一画幅产物不会污染身份。
    """
    ids, paths = {}, {}
    for entry in (refs or []):
        try:
            label, path = entry[1], entry[2]
        except (TypeError, IndexError, KeyError):
            continue
        key = str(label)
        paths[key] = ref_source_path(path, project_name)
        ids[key] = asset_ref_identity(path, project_name)
    return ids, paths


def _shot_text(shot: dict) -> str:
    """镜头可判定文本（描述 + 细节 + 音效 + 画面文字 + 字幕 + 台词）。"""
    if not isinstance(shot, dict):
        return ""
    parts = []
    for k in ("description", "visual_detail", "audio_cues", "surface_text", "caption_text",
              "dialogue_text", "dialogue", "prompt_h3"):
        v = shot.get(k)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    for k in ("items_in_shot", "characters_in_shot"):
        v = shot.get(k)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
    return " ".join(parts)


def _shot_subjects(shot: dict) -> set:
    """镜头主体集合（道具名 + 出场角色名），用于判断相邻镜是否同一物件序列。"""
    if not isinstance(shot, dict):
        return set()
    subs = set()
    for k in ("items_in_shot", "characters_in_shot"):
        v = shot.get(k)
        if isinstance(v, (list, tuple)):
            subs.update(str(x).strip() for x in v if str(x).strip())
    return subs


def _has_setup_marker(text: str) -> bool:
    t = str(text or "")
    return any(mk in t for mk in TRANSITION_SETUP_MARKERS)


def build_transition_shot(target: dict, all_shots: list, keyword: str = "") -> dict:
    """按异常首现镜派生一个**过渡镜**（结构性插入用；只构造，不写盘、不改序号）。

    交给剧本层使用（本文件不做结构性插入，理由见本节开头）。字段与剧本 shot 同构，
    且 shot_id 取 max+1 保证不与既有镜号冲突；dialogue 留空（不引入新配音）。
    """
    shots = [s for s in (all_shots or []) if isinstance(s, dict)]
    ids = [int(s.get("shot_id")) for s in shots
           if str(s.get("shot_id")).strip().lstrip("-").isdigit()]
    new_id = (max(ids) + 1) if ids else 1
    loc = str(target.get("location") or "")
    subj = "、".join(sorted(_shot_subjects(target))[:2]) or "异常物件"
    return {
        "shot_id": new_id,
        "duration": 2.5,
        "camera": str(target.get("shot_type") or "近景") + "缓推",
        "shot_type": target.get("shot_type") or "近景",
        "camera_motion": "轻推",
        "location": loc,
        "scene_no": target.get("scene_no"),
        "scene_heading": target.get("scene_heading") or "",
        "description": (
            "过渡镜（程序按异常首次出现自动补）：镜头从%s的环境起幅→缓慢推近到%s即将出现的位置，"
            "画面里此刻**还没有**该异常物件（%s），光位与陈设交代清楚"
            % (loc or "本场环境", subj, keyword or "异常物件")),
        "visual_detail": "起幅留出环境信息，推近过程平稳，禁止提前出现异常物件本体。",
        "dialogue": [],
        "emotion": "压抑/不安",
        "audio_cues": "环境底噪渐入。",
        "beat": "过渡",
        "transition_for": target.get("shot_id"),
        "transition_role": "inserted_transition",
        "transition_reason": "异常物件（关键词「%s」/ 主体：%s）首现前没有任何铺垫镜"
                             % (keyword or "未定", subj),
        "episode": target.get("episode"),
    }


def plan_transition_shot(shots: list, *, keywords=None, policy: dict = None) -> dict:
    """判定「异常物件首现」是否需要过渡镜，并**指明承担过渡作用的 shot_id**。

    返回 {"needed", "shot_id", "for_shot_id", "role", "reason", "directive", "targets"}：
      · role="predecessor_setup" → 前一个镜头的运镜/铺垫已承担过渡作用 → 指它；
      · role="self_setup"        → 异常首现镜**自身**的运镜承担过渡作用 → 指它；
      · role="needs_explicit_transition" → 序列里没有任何过渡承担者：
            该镜提示词会被注入显式过渡要求，manifest 记录该 shot_id 承担过渡
            （结构性补镜交给剧本层，见 build_transition_shot）。
    targets：全部异常首现镜的判定明细（便于一次看全）。

    ⚠️ predecessor_setup / self_setup 时 needed=False **不是漏做**：该异常已有过渡承担者，
    无需补镜（真实样本：shot#25 纸面「小洞」首现 → 由 shot#24 下推铺垫承担）。
    分镜序号是位置下标、落盘名由它决定，在分镜层插镜会让已入库产物错配；
    将来要显式补镜请走**剧本层**在 shots 数组里插（详见 TRANSITION_SHOT_POLICY 上方注释）。
    """
    pol = dict(TRANSITION_SHOT_POLICY)
    pol.update(policy or {})
    kws = tuple(keywords or pol.get("keywords") or TRANSITION_ANOMALY_KEYWORDS)
    shot_list = [s for s in (shots or []) if isinstance(s, dict)]
    out = {"needed": False, "shot_id": None, "for_shot_id": None, "role": "",
           "reason": "", "directive": "", "targets": [], "keywords": list(kws),
           "insert_shot": None, "enabled": bool(pol.get("enabled", True))}
    if not out["enabled"] or not kws or not shot_list:
        return out
    max_targets = int(pol.get("max_targets_per_episode") or 4)
    min_chars = int(pol.get("min_shot_chars") or 6)
    hits = []
    for idx, shot in enumerate(shot_list):
        text = _shot_text(shot)
        if len(text) < min_chars:
            continue
        hit = next((kw for kw in kws if kw in text), "")
        if not hit:
            continue
        if hits and idx - hits[-1]["index"] <= 1:
            continue          # 与上一次命中相邻 → 同一段序列，只认首现
        hits.append({"index": idx, "shot": shot, "keyword": hit})
    for h in hits[:max_targets]:
        idx, shot = h["index"], h["shot"]
        sid = shot.get("shot_id")
        prev = shot_list[idx - 1] if idx > 0 else None
        detail = {"shot_id": sid, "keyword": h["keyword"], "role": "", "reason": "",
                  "predecessor": prev.get("shot_id") if isinstance(prev, dict) else None,
                  "directive": ""}
        _prev_loc = str(prev.get("location") or "").strip() if isinstance(prev, dict) else ""
        _cur_loc = str(shot.get("location") or "").strip()
        _same_subject = bool(isinstance(prev, dict) and (_shot_subjects(prev) & _shot_subjects(shot)))
        # 「同一序列」判据：主体（道具/角色）有交集，或**双方都写明**且地点相同。
        # ⚠️ 两侧地点都为空**不算**同地点 —— 那是「没写」而不是「相同」，据此认定
        #    前镜为过渡承担者会把不相干的镜头算进同一序列（本地旧版剧本常见）。
        _same_place = bool(_prev_loc and _cur_loc and _prev_loc == _cur_loc)
        if isinstance(prev, dict) and _has_setup_marker(_shot_text(prev)) \
                and (_same_subject or _same_place):
            detail.update({"role": "predecessor_setup",
                           "reason": "前镜 #%s 的运镜/铺垫已把观众引到异常出现处" % prev.get("shot_id"),
                           "transition_shot_id": prev.get("shot_id")})
        elif _has_setup_marker(_shot_text(shot)):
            detail.update({"role": "self_setup",
                           "reason": "异常首现镜 #%s 自身运镜承担了过渡（%s）"
                                     % (sid, h["keyword"]),
                           "transition_shot_id": sid})
        else:
            detail.update({"role": "needs_explicit_transition",
                           "reason": "异常物件（%s）首现镜 #%s 前后都没有铺垫/运镜过渡"
                                     % (h["keyword"], sid),
                           "transition_shot_id": sid,
                           "directive": (
                               "【过渡要求·程序按异常首现自动追加】本镜是「%s」在成片中首次出现："
                               "首格必须是过渡态（该物件尚未成形/尚未入画），由环境或前一个动作引入，"
                               "严禁在首格直接跳到异常本体；末格才呈现它。"
                               % h["keyword"]),
                           "insert_shot": build_transition_shot(shot, shot_list, h["keyword"])})
            out["needed"] = True
        out["targets"].append(detail)
        if out["shot_id"] is None or detail["role"] == "needs_explicit_transition":
            out.update({"shot_id": detail.get("transition_shot_id"), "for_shot_id": sid,
                        "role": detail["role"], "reason": detail["reason"],
                        "directive": detail.get("directive", ""),
                        "insert_shot": detail.get("insert_shot")})
    return out


def apply_transition_directive(shots: list, plan: dict) -> dict:
    """把过渡要求写进**异常首现镜**（内存态；本函数不写盘、不新增镜头）。

    写入字段（都只在需要时出现，便于下游/质检识别）：
      shot["transition_note"]   当前镜承担的角色（predecessor_setup/self_setup/needs_explicit_transition）
      shot["transition_for"]    为哪一镜的异常首现服务
      shot["transition_shot_id"]承担过渡作用的 shot_id
      shot["transition_directive"] 需要显式过渡时的提示词追加段
    返回 {"annotated": [shot_id...]}。
    """
    out = {"annotated": []}
    if not isinstance(plan, dict) or not plan.get("targets"):
        return out
    by_id = {}
    for s in (shots or []):
        if isinstance(s, dict):
            by_id[str(s.get("shot_id"))] = s
    for t in plan["targets"]:
        carrier = by_id.get(str(t.get("transition_shot_id")))
        if carrier is None:
            continue
        carrier["transition_shot_id"] = t.get("transition_shot_id")
        carrier["transition_for"] = t.get("shot_id")
        carrier["transition_note"] = t.get("role")
        if t.get("directive"):
            target = by_id.get(str(t.get("shot_id")))
            if target is not None:
                target["transition_directive"] = t["directive"]
        out["annotated"].append(t.get("transition_shot_id"))
    return out





def _storyboard_worker(task_id: str, project_name: str, shots: list,
                       char_idx: dict, item_idx: dict, scene_idx: dict,
                       episode_no=None, style: str = "", overwrite: bool = False,
                       prewarm_only: bool = False):
    """后台分镜图生成任务：逐镜头生成并落盘 output/storyboards/<项目>[/epNN]/shot_XX.png

    style：用户与总控敲定的风格。用于 ① 补齐镜头 style 字段（老剧本无该字段时兜底）
    ② 解析画幅并覆写分镜图尺寸，保证分镜与成片同为竖屏 9:16。

    overwrite：是否「全量重做」。默认 False —— 已达标入库的 shot_XX.png 直接复用、
    只重跑缺失/被质检阻断的镜头（断点续跑语义）。这是本次修复的核心：
    修复前无论如何都从第 1 镜重跑到最后一镜，导致「补跑 21 个不达标镜」要重烧全部 83 镜。
    需要强制全部重画时（如换风格）显式传 overwrite=True。
    注意：质检不达标的镜头只写质检暂存区、不写正式目录，所以「正式目录里已有该图」
    ⟺ 「该镜上一轮已通过质检」——跳过它不会漏掉任何不达标镜。

    episode_no：本集集号。除决定落盘目录（第 1 集平铺 / 第 2 集起 epNN/）外，
    **还决定本集角色的服装变体覆盖**（2026-10-13 t16 P0-②，见下方「集级服装覆盖」段）。
    """
    out_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project_name), episode_no)
    os.makedirs(out_dir, exist_ok=True)
    # ===================== 集级服装覆盖（2026-10-13 t16 P0-②）=====================
    # 用户实测缺陷：「同一集里分镜图的服装和视频的服装不一样」。
    #
    # 根因：服装变体（衣柜）的**消费端只接了视频一侧** ——
    #   · 视频：video_helpers 调 episode_helpers._episode_outfit_overrides(项目, 集号)，
    #     把 {角色名: outfit_key} 写进 shot["character_outfits"]，
    #     再由 qc_helpers._shot_outfit_dir 解析成 outfits/<key>/ 取变体图；
    #   · 分镜：**没有任何调用点**，而剧本里 shot["outfit"] / shot["character_outfits"]
    #     实测全为空（第1/2/3集 0/46、0/39、0/39）→ _shot_outfit_dir 恒返回 '' →
    #     分镜恒取主设定图 base.png，视频取 outfits/<key>/base.png ⇒ 两段产物服装不一致。
    #
    # 修法（与 video_helpers:1455-1462 **逐字同口径**，setdefault 不覆盖剧本显式声明）：
    #   在 worker 入口**一次性**把本集覆盖写进每镜（对所有下游路径等效：主循环内联、
    #   提示词预取线程、单镜重跑入口都各自调用本 worker 或走同一分配器）。
    # fail-open：state 缺失 / 变体解析不出 → 空 dict → 行为与改造前完全一致（零回归）。
    # 集号解析与 `_ep_dir` 同口径（<=1 → 第 1 集），不依赖 worker 入参一定是 int。
    try:
        _ovr_ep = int(episode_no)
    except (TypeError, ValueError):
        _ovr_ep = 1
    if _ovr_ep <= 0:
        _ovr_ep = 1
    try:
        from episode_helpers import _episode_outfit_overrides as _ep_outfit_ovr
        _ep_outfit_override = _ep_outfit_ovr(project_name, _ovr_ep) or {}
    except Exception as _oo_e:  # noqa: BLE001 —— 覆盖解析失败绝不阻断分镜生成
        logger.warning("[服装变体] 本集服装覆盖解析失败（分镜按主设定图出图）：%s", _oo_e)
        _ep_outfit_override = {}
    if _ep_outfit_override:
        _applied_ovr = 0
        for _s in (shots or []):
            if not isinstance(_s, dict):
                continue
            _co = dict(_s.get("character_outfits") or {})
            for _cn, _ok in _ep_outfit_override.items():
                _co.setdefault(_cn, _ok)
            _s["character_outfits"] = _co
            _applied_ovr += 1
        logger.info("[服装变体] 分镜侧已应用本集服装覆盖（t16 P0-②）：%s → %d 镜",
                    _ep_outfit_override, _applied_ovr)
    else:
        logger.info("[服装变体] 本集无可应用的服装覆盖（分镜按主设定图出图，与改造前一致）")
    # ===================== 集级服装覆盖 结束 =====================
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
    if _sb_style:
        logger.info("[分镜风格] 风格=%s；画幅=%s", _sb_style,
                        _sb_style_res["label"] or "未指定（沿用模板）")
        shots = [dict(s, style=(s.get("style") or _sb_style)) for s in (shots or [])
                 if isinstance(s, dict)]
    # ---- T3（2026-10-12）：异常物件（用户口径里的「渗血洞」）过渡判定 ----
    # 用户反馈「渗血洞缺过渡镜头」：异常物件在某镜里凭空出现。这里逐个「异常首现镜」
    # 判定承接过渡的 shot_id（前镜铺垫 / 本镜运镜 / 需要显式过渡），需要显式过渡的
    # 在本镜提示词里追加过渡要求，并把结论写进 manifest（可核对、可追溯）。
    # fail-open：判定异常一律跳过（分镜生成不受影响）。
    transition_plan = {"enabled": False, "targets": [], "needed": False, "error": ""}
    try:
        transition_plan = plan_transition_shot(shots)
        _tx_ann = apply_transition_directive(shots, transition_plan)
        if transition_plan.get("targets"):
            for _t in transition_plan["targets"]:
                logger.info("[过渡镜] 异常首现 shot=%s（关键词「%s」）→ 承担过渡的 shot=%s（%s）：%s",
                                _t.get("shot_id"), _t.get("keyword"),
                                _t.get("transition_shot_id"), _t.get("role"), _t.get("reason"))
            transition_plan["annotated"] = _tx_ann.get("annotated") or []
        else:
            logger.info("[过渡镜] 本批未命中异常物件关键词（%s）→ 不做过渡判定",
                            "、".join(transition_plan.get("keywords") or []))
    except Exception as _tx_e:  # noqa: BLE001
        transition_plan = {"enabled": False, "targets": [], "needed": False,
                           "error": "%s: %s" % (type(_tx_e).__name__, _tx_e)}
        logger.warning("[过渡镜] 过渡判定失败（跳过，不影响分镜生成）：%s", _tx_e)
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

    # ⭐ T20（2026-10-13 用户口径）：收尾镜 = **每场戏的最后一镜**（连续 run 口径）。
    #   ⚠️ 标记必须打在**上方 shots 重建（dict(s, style=…)）之后**，否则重建会丢标记；
    #      也必须早于 _prep_shot —— 它是内联与预取**共用的唯一提示词构建实现**，
    #      两边口径打架（2026-10-13 实测 shot_13 卡住：提示词仍写「9格是同一条动作链」）。
    _tail_idx = tail_shot_indexes(shots)
    for _ti in _tail_idx:
        if 0 <= _ti < len(shots or []) and isinstance(shots[_ti], dict):
            shots[_ti]["_tail_shot"] = True
    if _tail_idx:
        logger.info("[T20] 本集收尾镜（每场戏最后一镜）共 %d 个：%s",
                        len(_tail_idx),
                        [shots[_t].get("shot_id") for _t in sorted(_tail_idx) if isinstance(shots[_t], dict)])

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
                "pf_item": {}, "pgate_item": {}, "item_ref_notes": [],
                "blocked": False, "refs_empty": False,
                "no_reference": False, "ref_error": "", "error": "",
                "char_refs_missing": [],
                "identity_grid": "", "blocking_ref": ""}
        # ⭐ 2026-10-07 逐阶段计时（只加日志、不改行为）。
        # 动机：实测已排除「GPU 单图 45s」「3D 基准图 1.5s」「VLM 质检 5s」，
        # 剩下的分钟级开销必须能拆到「前置段」的哪个子步上，否则优化就是猜。
        # ⚠️ 计时不吞异常：_prep_shot 仍按原语义向上抛（预取线程装包 / 内联 except 落账）。
        _t0 = time.perf_counter()
        _tprev = _t0
        _ph = {}
        refs = _allocate_storyboard_refs(shot_p, char_idx, item_idx, scene_idx, project_name)
        # ⭐ 2026-10-20：把场景的视觉描述塞进 shot（见 _scene_appearance_of 的 docstring）——
        #    否则分镜提示词只拿到「青云宗外门广场」六个字，模型画成纯白背景。
        _with_scene_appearance(shot_p, scene_idx)
        # ⭐ 2026-10-18（借鉴同类项目「清单驱动·缺图不出图」）：本镜声明了出场角色、但某角色
        #    没有任何可用参考图（资产缺失且按需生成失败）→ 停该镜、给出可读原因。
        _miss_chars = list(shot_p.get("_sb_missing_char_refs") or [])
        if _miss_chars and _sb_require_char_refs():
            logger.warning("[分镜参考图] shot=%s 缺角色参考图 → 按「缺图不出图」停止本镜：%s",
                           shot_p.get("shot_id"), "、".join(_miss_chars))
            pack["char_refs_missing"] = _miss_chars
            pack["error"] = ("角色参考图缺失：%s —— 已按「缺图不出图」停止本镜"
                             "（请先补齐这些角色的资产，再重跑本镜）" % "、".join(_miss_chars))
            return pack
        # T3（2026-10-12）：道具槽位对齐到**固定参考图**（同一道具在任何镜头同一张图）。
        # 放在画幅统一之前：先定「用哪张图」，再统一它的画幅 —— 顺序反了会让身份串
        # 落在统一产物上（路径带哈希，换机器就变），manifest 无法比对。
        refs, _ir_notes = pin_item_refs_in_refs(refs, project_name)
        for _irn in _ir_notes:
            if _irn.get("changed"):
                logger.info("[道具固定参考图] shot=%s 标签=%s 重指：%s → %s（身份 %s）",
                                shot_p.get("shot_id"), _irn.get("label"),
                                os.path.basename(str(_irn.get("from"))),
                                os.path.basename(str(_irn.get("to"))), _irn.get("identity"))
            elif _irn.get("error"):
                logger.warning("[道具固定参考图] shot=%s 标签=%s 对齐失败（沿用本镜原图）：%s",
                                   shot_p.get("shot_id"), _irn.get("label"), _irn.get("error"))
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
        from config import ENABLE_STORYBOARD_IDENTITY_GRID as _SB_IDENTITY_GRID
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
        # ⚠️ 2026-10-13：**不再要求 _has_on_screen** —— 那个口径判的是「画面内是否有出场
        #    角色」（服务于 3D 人偶基准图与无人物镜禁令），而局部特写镜（shot=13 只拍手/
        #    膝头、无完整人脸）会被判 False，却照样需要角色外观锚点：实测这正是收尾镜拿不到
        #    身份基准网格的真因（日志「身份基准网格=0.0s」）。门槛改由「视图数 ≥2」把关。
        # ⭐ 2026-10-15（用户口径）：身份基准网格默认关闭 —— 见 config.ENABLE_STORYBOARD_IDENTITY_GRID。
        #    用户实测认定：把多角色拼成一张参考图会让特征串味（每角色仅占 1/2~1/3 面积、
        #    面部像素被摊薄），权威性反而盖过逐角色单张三视图，是角色不一致的诱因。
        #    设 True 可完整回退旧行为。
        if _SB_IDENTITY_GRID:
            # ⭐ T20b（2026-10-13 用户口径）：
            #   ② 形状**按剧情**来，不套「总张数 → 比例」的固定表：一行 = 一个出场角色、
            #      列 = 该角色的视图/服装变体（基础设定图 + 本集服装变体）。
            #      所以 1 角色 × 2 视图 → 1×2、2 角色 × 2 视图 → 2×2、3 角色 × 2 视图 → 3×2。
            #      用户原话：「我只是举例子比例，具体比例要根据具体剧情来啊」。
            _ig_groups = []           # [(角色名, [视图路径…]), …]
            # 本集服装覆盖表（t16）：{角色名: outfit_key} —— 给每个角色补「本集服装视图」
            # ⚠️ 不要用 _shot_outfit_dir 代替：分镜侧它恒为空（实测 0/46，见 L1859）。
            _ig_outfits = {}
            try:
                _ig_outfits = dict(_ep_outfit_override or {})   # worker 已解析过一次（见上）
            except Exception:  # noqa: BLE001
                _ig_outfits = {}
            if not _ig_outfits:
                try:
                    from episode_helpers import _episode_outfit_overrides as _ep_ovr
                    _ig_outfits = _ep_ovr(project_name,
                                          episode_no if episode_no not in (None, "") else 1) or {}
                except Exception as _ig_ov_e:  # noqa: BLE001
                    logger.debug("[身份基准网格] 本集服装覆盖表读取失败（忽略）：%s", _ig_ov_e)
                    _ig_outfits = {}
            for _r in refs:
                if _r[0] not in ("主角色", "次角色"):
                    continue
                _lab = str(_r[1] or "")
                _nm = ""
                _a = _lab.find("角色「")
                if _a >= 0:
                    _b = _lab.find("」", _a + 3)
                    if _b > _a:
                        _nm = _lab[_a + 3:_b]
                _ovr_key = _ig_outfits.get(_nm, "")
                _views = _identity_views_for(_r[2], _ovr_key)
                if _nm:
                    # 兜底：直接从角色资产目录取（本集服装变体 + 主设定图）—— 覆盖表为空、
                    # 或命中图不带 outfits/ 时靠它凑齐「一个角色的多张视图」（实测 shot=13）。
                    try:
                        _cdir = os.path.join(CHARACTERS_DIR, _safe_project(project_name),
                                             str(_nm))
                        _dir_views = _identity_views_for_dir(_cdir, _ovr_key, episode_no)
                        if len(_dir_views) >= 2:
                            _views = _dir_views        # 资产目录更权威（本集变体优先）
                        elif _dir_views:
                            for _p in _dir_views:
                                if _p not in _views:
                                    _views.append(_p)
                    except Exception as _ig_d_e:  # noqa: BLE001
                        logger.debug("[身份基准网格] 资产目录兜底失败（忽略）：%s", _ig_d_e)
                if _views:
                    _ig_groups.append((_nm or ("角色%d" % (len(_ig_groups) + 1)), _views))
            _ig_total = sum(len(_v) for _n, _v in _ig_groups)
            if _ig_groups and _ig_total < 2:
                # 只在「有角色槽位但视图不足」时留痕（罕见）—— 便于以后一眼看出是素材不够
                logger.warning("[身份基准网格] shot=%s 视图不足 → 本镜不建网格：%s",
                               shot_p.get("shot_id"),
                               "/".join("%s(%d视图)" % (_n, len(_v)) for _n, _v in _ig_groups))
            if _ig_groups and _ig_total >= 2:      # 2026-10-13 用户拍板：门槛放宽到 ≥2
                # 单帧布局（2026-10-15：九宫格已移除，只保留这一条路径）：
                # 一行一角色，列数 = 出场角色里最多的视图数。
                # 单帧（含收尾镜）：一行一角色，列数 = 出场角色里最多的视图数
                _ig_cols = max(len(_vs) for _n, _vs in _ig_groups)
                _ig_rows = len(_ig_groups)
                _ig_cells = _ig_cols * _ig_rows
                _ig_flat = None
                _ig_build_kw = {"rows_paths": [[_p for _p in _vs[:_ig_cols]]
                                               for _n, _vs in _ig_groups]}
                _ig_brief = "/".join("%s(%d视图)" % (_n, len(_vs)) for _n, _vs in _ig_groups)
                try:
                    _identity_grid_path = _build_identity_ref_grid(
                        _ig_flat,
                        os.path.join(QC_DIR, project_name, "identity_grid",
                                     f"shot_{seq_p:02d}_identity_grid.png"),
                        target_size=_sb_size, **_ig_build_kw)
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
                        f"是本镜出场角色的身份基准网格（{_ig_rows}×{_ig_cols} 共 {_ig_cells} 格，"
                        f"同一行 = 同一角色：{_ig_brief}）："
                        + "输出画面中的角色必须与它保持面部身份、发型与服装一致",
                        _identity_grid_path))
                    labels = [r[1] for r in refs]
                    logger.info("[身份基准网格] shot=%s 布局=%d×%d 共%d格（%s，输出=%s）",
                                shot_p.get("shot_id"), _ig_rows, _ig_cols, _ig_cells,
                                _ig_brief, "单帧")
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
        # ⭐ 2026-10-16：「局部」插入镜**不引入上一镜整图** —— 上一镜通常是**带脸的中近景**，
        #    把它当参考图会把局部特写再次往「带脸构图」拽（与角色脸部锚点同理）。
        #    局部镜只靠文字连续性口径保证道具/材质接续即可。
        if (_prev_sb_ref and not _is_detail_shot(shot_p)
                and os.path.isfile(_prev_sb_ref) and len(refs) < 9):
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
                    # ⭐ 体型差异化（2026-10-13）：登记本集角色体型（清瘦/魁梧），供
                    #    build_scene_json 写 heightScale/girth。此前所有人偶恒 1.7m/1.0，
                    #    而角色档案白纸黑字写着「清瘦少年」「体格壮硕」—— 同高同胖等于
                    #    把「谁是什么体量」这条构图信息整个丢掉。
                    try:
                        import te_3d_director as _t3d_body
                        _t3d_body.set_char_profiles(_te3d_body_profiles(project_name, episode_no))
                        # 场景背板/全景（官方 scene.panorama 口径）：登记本集场景图，
                        # 命中 location 时基准图带真实场景背景（非 2:1 走远景背板，
                        # 避免贴球变形）
                        _t3d_body.set_scene_images(_te3d_scene_images(project_name, episode_no))
                    except Exception as _bp_e:  # noqa: BLE001
                        logger.debug("[3D导演台] 体型登记失败（忽略）：%s", _bp_e)
                    # ⚠️ 只传「项目 QC 目录」—— te_3d_render 内部会再拼 te3d_blocking 子目录。
                    #    此前这里又拼了一次，基准图会落到 <项目>/te3d_blocking/te3d_blocking/
                    #    （双层，2026-10-13 从 qc 目录结构里发现）。
                    _blk_out_dir = os.path.join(QC_DIR, project_name)
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
                            target_size=_sb_size,
                        ) or ""
                if _blocking_ref_path:
                    # ⭐ 2026-10-13（用户要求）：**生成前先质检这张基准图**，不通过就不注入。
                    #    基准图是分镜的 <image1> 强先验：人偶人数/站位/景别/姿势错了，分镜图
                    #    必然跟着错，而分镜侧质检要等 20+ 分钟（含 3 次重试）才发现。判据全部
                    #    确定性、零模型调用（见 app/te_3d_qc.py）。
                    #    不通过 → 本镜不注入基准图、回落文字站位锚点：既不污染生成，也不会
                    #    把生产卡住（硬门禁会让 46 镜大面积停摆）。
                    try:
                        import te_3d_qc
                        if te_3d_qc.enabled():
                            _bq_camk = ""
                            try:
                                from shot_camera import camera_key as _bq_ck
                                _bq_camk = _bq_ck(str(shot_p.get("camera") or ""))
                            except Exception:  # noqa: BLE001
                                _bq_camk = ""
                            # ⭐ 用户口径（2026-10-13）：「不通过就不注入，不应该是不通过就根据
                            #    质检发现的问题重新生成吗」—— 基准图由我们**确定性渲染**，问题可
                            #    定位、参数可反推，所以走「质检 → 修计划重渲 → 再质检」闭环
                            #    （最多 2 轮修复）。仍不过才回落文字站位锚点。
                            # ⚠️ 2026-10-16（v2 §5 红线 #1）：修复重渲必须**同一把
                            #    _TE3D_RENDER_LOCK** 串行化 —— 此前 render_fn 直连
                            #    render_blocking_from_plan，在锁**外**重渲，会与预取线程
                            #    并发抢「进程级单例 + 单 inbox 槽位 + 共享 profile」的回传
                            #    服务（正是该锁要防的竞态）。只锁「真正渲染」这一下，
                            #    不把中间的 QC 判定也锁进来。
                            def _bq_render_locked(*_a, **_k):
                                with _TE3D_RENDER_LOCK:
                                    return te_3d_render.render_blocking_from_plan(*_a, **_k)

                            _bq = te_3d_qc.qc_repair_and_alt_cameras(
                                shot_p, _blocking_ref_path,
                                cam_key=_bq_camk,
                                aspect=":".join(map(str, _sb_style_res.get("ratio") or ("16", "9"))),
                                target_size=_sb_size, width=_sb_size[0],
                                out_dir=_blk_out_dir,
                                render_fn=_bq_render_locked,
                                max_rounds=4)
                            _bq_hist = "；".join(
                                "第%d轮 %s" % (int(r.get("round") or 0) + 1,
                                              "通过" if r.get("ok")
                                              else ("/".join(r.get("reasons") or []) or "不通过"))
                                for r in (_bq.get("rounds") or []))
                            if _bq.get("ok"):
                                _blocking_ref_path = str(_bq.get("path") or _blocking_ref_path)
                                logger.info("[3D基准图质检] shot=%s 通过（%s）",
                                            shot_p.get("shot_id"), _bq_hist)
                            else:
                                _bq_why = "；".join(_bq.get("reasons") or []) or "未知"
                                logger.warning(
                                    "[3D基准图质检] shot=%s 自愈重渲后仍不通过 → 本镜不注入基准图"
                                    "（回落文字站位锚点）：%s｜%s",
                                    shot_p.get("shot_id"), _bq_why, _bq_hist)
                                try:
                                    _note_ref_warning(
                                        shot_p, "3D 构图基准图质检不通过（已自愈重渲）：%s" % _bq_why)
                                except Exception:  # noqa: BLE001
                                    pass
                                _blocking_ref_path = ""
                    except Exception as _bq_e:  # noqa: BLE001  质检自身失败不阻断注入
                        logger.debug("[3D基准图质检] 异常（忽略，照常注入）：%s", _bq_e)
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
                    # [REFDEBUG 2026-10-15 临时诊断] 打印真实 labels，定位编号偏移
                    logger.warning("[REFDEBUG] shot=%s refs=%d | %s",
                                   shot_p.get("shot_id"), len(refs),
                                   " || ".join("%d:%s" % (i + 1, str(x)[:46]) for i, x in enumerate(labels)))
            except Exception as _3d_e:  # noqa: BLE001
                logger.warning("[3D导演台] shot=%s 渲染失败（降级为纯文字站位）：%s",
                                   shot_p.get("shot_id"), _3d_e)
                _blocking_ref_path = ""
        _ph["3D站位基准图"] = time.perf_counter() - _tprev
        _tprev = time.perf_counter()
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
        # ---------- C-2（2026-10-16）：首建召回 kind="storyboard" 历史教训 ----------
        # 背景：此前分镜**首建**只经 _prompt_preflight 召回 kind="prompt"（预检教训）；
        #   而 kind="storyboard"（质检缺陷教训：面板雷同/景别/发型…）只在 attempt>0 的
        #   重试轮（下方 _retry_base）与软放行后记账时召回 → 每个新镜头首建必然不复用
        #   已沉淀的缺陷教训（结构性断裂，见 docs/优化方案_外部借鉴v2与落地_20261016.md G3）。
        # 口径与重试轮一致：max_hints=1（教训是「上次失败的具体缺陷描述」，越注越长的
        #   提示词与 H3 官方 Simple-and-Clear 相悖），style 用本集风格替换 {style} 占位。
        # 位置：放在预检**之后**（不改预检语义、不改 orig_prompt 指纹键）；读的是
        #   预检自愈后的提示词，与重试轮的 _retry_base 同口径。fail-open：记忆异常一律
        #   按原提示词继续，绝不影响首建。
        try:
            _sb_learned = prompt_memory.learned_prompt(
                kind="storyboard", prompt=prompt, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, max_hints=1,
                style=_qc_style_of(project_name))
            if _sb_learned and _sb_learned != prompt:
                prompt = _sb_learned
                logger.info("分镜首建召回 storyboard 教训：shot=%s", shot_p.get("shot_id"))
        except Exception as _lm_e:  # noqa: BLE001 —— 记忆读取失败绝不影响首建
            logger.warning("分镜首建 storyboard 教训召回失败（忽略）：%s", _lm_e)
        # T3（2026-10-12）：异常物件首现镜的**过渡要求**注入（程序追加，不是模型自由文本）。
        # 放在预检之后：预检判的是构建器产出的提示词（口径不变）；追加后再走一次
        # 结构闸门 _storyboard_prompt_structurally_ok —— 缺协议段就撤回追加，
        # 绝不为了加一句话而送出结构不合格的提示词。
        _td = str(shot_p.get("transition_directive") or "").strip()
        if _td:
            _td_cand = prompt.rstrip() + "\n" + _td
            try:
                _td_ok, _td_miss = _storyboard_prompt_structurally_ok(_td_cand, False)
            except Exception:  # noqa: BLE001 —— 闸门自身异常按放行处理（与预检 fail-open 同口径）
                _td_ok, _td_miss = True, []
            if _td_ok:
                prompt = _td_cand
                logger.info("[过渡镜] shot=%s 已注入过渡要求（承担过渡的 shot=%s，异常首现镜自身）",
                                shot_p.get("shot_id"), shot_p.get("transition_shot_id"))
            else:
                logger.warning("[过渡镜] shot=%s 过渡要求注入后缺协议段（%s）：撤回追加，保持原提示词",
                                   shot_p.get("shot_id"), "、".join(_td_miss))
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
                     "item_ref_notes": _ir_notes,
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
        _pf_filled = []
        for _j in range(cur_p + 1, min(cur_p + 3, len(_pf_pending))):
            _pf_sq = _pf_pending[_j][0]
            if _pf_schedule(_pf_sq):
                _pf_filled.append(_pf_sq)
        if _pf_filled:
            logger.info("[预取诊断] 派出预取 seq=%s（待处理共 %d 镜）",
                        _pf_filled, len(_pf_pending))

    def _pf_take(seq_p):
        """取走 seq_p 的预取包（有则弹出）；没有返回 None（主循环转内联）。"""
        with _pf_lock:
            return _pf_cache.pop(seq_p, None)

    if prewarm_only:
        # ===================== 分镜提示词预热（依赖驱动，2026-10-12）=====================
        # 用户早期要求「分镜提示词也要预热」：某一镜所需的角色/物品/场景资产**全部就绪后**，
        # 立即后台把该镜前置段（参考图分配→画幅统一→身份网格→3D基准→build prompt→预检）
        # 提前跑一遍，**只填缓存**（prompt_qc / prompt_enhance / 九宫格规划 / 3D基准图），
        # 正式跑 _prep_shot 时全部命中秒过，GPU 不再等 CPU/LLM。
        # 安全纪律（与 asset_worker._prewarm_asset_prompts 同范式）：
        #   · 纯 LLM/CPU：3D 基准图走 SwiftShader 软渲染（--disable-gpu），不抢 ComfyUI；
        #   · 只填缓存，不落分镜产物、不写 manifest、不碰 generation_state / results；
        #   · 资产未齐的镜在 _prep_shot 内 refs_empty 提前返回（天然跳过，零 LLM）；
        #   · 异常逐镜抓取，预热失败绝不影响主线（本函数由调用方 daemon 线程兜底）。
        from concurrent.futures import ThreadPoolExecutor
        _pw_total = len(_pf_pending)
        _pw_done = {"n": 0, "fail": 0, "skip": 0}
        _pw_lock = threading.Lock()
        logger.info("[分镜提示词预热] 开始：待预热 %d 镜（只填缓存，不碰 GPU）", _pw_total)

        def _pw_one(_item):
            _seq, _shot = _item
            try:
                _pack = _prep_shot(_seq, _shot)
                with _pw_lock:
                    _pw_done["n"] += 1
                    # 资产未齐的镜 _prep_shot 内 refs_empty 提前返回、无 prompt → 计跳过
                    if _pack.get("refs_empty") and not _pack.get("prompt"):
                        _pw_done["skip"] += 1
                    _cur = _pw_done["n"] + _pw_done["fail"]
                    logger.info("[分镜提示词预热] 已预热 %d/%d（失败 %d，资产未齐跳过 %d）",
                                _cur, _pw_total, _pw_done["fail"], _pw_done["skip"])
            except Exception:  # noqa: BLE001  单个失败不影响其余
                with _pw_lock:
                    _pw_done["fail"] += 1
                    _cur = _pw_done["n"] + _pw_done["fail"]
                    logger.info("[分镜提示词预热] 已预热 %d/%d（失败 %d）",
                                _cur, _pw_total, _pw_done["fail"])

        try:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix='prewarm-sb') as _ex:
                list(_ex.map(_pw_one, _pf_pending))
        except Exception as _pw_ex:  # noqa: BLE001  线程池调度失败不影响生产
            logger.warning("[分镜提示词预热] 调度失败（忽略，不影响生产）：%s", _pw_ex)
        logger.info("[分镜提示词预热] 完成：预热 %d/%d 镜（失败 %d，资产未齐跳过 %d），"
                    "正式生成时命中缓存",
                    _pw_done["n"], _pw_total, _pw_done["fail"], _pw_done["skip"])
        return

    # ===================== T19：分镜「逐场次」→ 视频提示词预热 =====================
    # 用户指令（2026-10-13）：「不是分镜齐备后生成提示词啊，不应该是分镜在生成时
    # 同步进行预热提示词吗」。原实现只在 step_storyboard **全部**完成后触发一次整集
    # 预热（video_helpers.prewarm_video_prompts 的 docstring 亦写着「用户早期明确要求
    # （一直未落地）：视频场次需要 1-20 分镜在这些分镜生成后就可以开始预热了」），
    # 且那个调用点还落在 step_storyboard 的提前 return 之后 —— 分镜齐备时根本不执行。
    #
    # 现改为**依赖驱动**（与 _trigger_storyboard_prewarm 的「资产→分镜提示词预热」同范式）：
    # 某场次（shot["scene_no"]）的 1..N 个分镜**全部落盘**后，立即在后台预热该场的 h3
    # 视频提示词（切段 / LoRA / 增强 / 预检，只填缓存）：不碰 GPU、不占 ComfyUI 队列、
    # 与出图并行；预热失败绝不影响主线（daemon + 全捕获）。
    # 关闭开关：env MJSCXT_PREWARM_VIDEO_PROMPT=0（与 video_helpers 同一开关）。
    #: ⭐ T20：_tail_idx 已在 _prep_shot 之前算好（并已给 shots 打上 _tail_shot 标记）
    _vw_done_seqs = set()
    _vw_fired: set = set()
    _vw_state = {"script": None}
    _vw_lock = threading.Lock()
    _vw_scene_of: dict = {}
    _vw_scene_seqs: dict = {}
    for _vi, _vs in enumerate(shots or []):
        if not isinstance(_vs, dict):
            continue
        _vk = _vs.get("scene_no")
        if _vk in (None, ""):
            continue
        _vkey = _vk if isinstance(_vk, int) else str(_vk).strip()
        _vw_scene_of.setdefault(_vkey, []).append(_vs)
        _vw_scene_seqs.setdefault(_vkey, set()).add(
            _shot_seq(_vs.get("shot_id", _vi + 1), _vi + 1))

    def _vw_fire(_key):
        """后台预热单个场次的 h3 视频提示词（daemon 线程；只填缓存，不碰 GPU）。"""
        try:
            import video_helpers as _vh
            with _vw_lock:
                _sc = _vw_state["script"]
            if _sc is None:
                try:
                    _sc = _load_script_for(project_name, episode_no) or {}
                except Exception:  # noqa: BLE001
                    _sc = {}
                with _vw_lock:
                    _vw_state["script"] = _sc
            _sc_shots = list(_vw_scene_of.get(_key) or [])
            if not _sc_shots:
                return
            logger.info("[视频提示词预热] 场次 %s 的 %d 镜已全部落盘 → 后台预热该场"
                        " h3 提示词（T19）", _key, len(_sc_shots))
            # ⭐ T19b（2026-10-13）：**不要**把剧本结构当参考图传下去 —— 剧本场次的
            #   characters/scenes 只有 name / reference_prompt_zh，**没有 front/base 键**，
            #   而 _collect_reference_images 只认 front/base → 取图恒为 0。实测（12:45 起每场
            #   触发预热）：18 次「角色参考图不可用: 羡进/赵天霸」→ 随即「磁盘无参考图，跳过」，
            #   T19 逐场次预热全程空转、零收益。传 None 由 prewarm_video_prompts 内部从磁盘
            #   资产目录自动收集（与正式视频链路同源，collect_helpers 注释早已警告此坑）。
            _vh.prewarm_video_prompts(
                project_name, _sc_shots,
                character_refs=None,
                scene_refs=None,
                episode_no=(episode_no if episode_no not in (None, "") else 1),
                style=style)
        except Exception as _vw_f_e:  # noqa: BLE001
            logger.warning("[视频提示词预热] 场次 %s 触发失败（忽略，不影响生产）：%s",
                           _key, _vw_f_e)

    def _vw_after_shot(_seq):
        """本镜落盘后调用：把已齐备的场次交给后台预热（幂等，逐镜仅集合运算）。"""
        try:
            with _vw_lock:
                _vw_done_seqs.add(_seq)
                _ready = [k for k, need in _vw_scene_seqs.items()
                          if k not in _vw_fired and need and need <= _vw_done_seqs]
                for _k in _ready:
                    _vw_fired.add(_k)
            for _k in _ready:
                _vw_fire(_k)
        except Exception as _vw_e:  # noqa: BLE001
            logger.warning("[视频提示词预热] 场次齐备判定失败（忽略）：%s", _vw_e)

    try:
        _processed_ids = []
        for i, shot in enumerate(shots):
            shot_id = shot.get("shot_id", i + 1)
            seq = _shot_seq(shot_id, i + 1)
            # ⭐ T20（2026-10-13 用户口径）：**逐镜**判定是否用九宫格 ——
            #   收尾镜 = **每场戏的最后一镜**（连续 run 口径，见 config.tail_shot_indexes）——
            #   常是静态定格或悬念定格，缺少「9 个随时间推进的关键帧」所需的内容量；
            #   硬套 3×3 会逼模型重复构图或编造设定里没有的元素
            #   （实测 shot_13 连打 3 轮质检不过）。这类镜降级为**单帧**：质检口径同步切到
            #   单帧（见下方 _qc_desc），景别自动裁剪也恢复启用。
            dst = os.path.join(out_dir, f"shot_{seq:02d}.png")
            # ---------- 断点续跑：**真达标**的镜头才直接复用，只重跑缺失/不达标的 ----------
            # 正式目录里已有非空 shot_XX.png **且该图不是软放行/带严重问题产物** → 跳过。
            # ⭐ 2026-10-13 T13：旧判据只有「有文件就跳过」（注释自称「有文件 ⟺ 上一轮
            #    已通过质检，不达标的只落暂存区」）—— 该等式在软放行（2026-10-08 起）
            #    之后**不再成立**：软放行的图**同样写正式目录**（见下方软放行分支）。
            #    后果：质检不达标但被放行的图被后续每一轮当成已达标跳过、**永不重画**
            #    （实测 shot_11.png 停在 2026-10-12 05:49:53，最新一轮日志
            #    「shot=11 质量=未交付 墙钟 1.2s」= 只做了 skip；用户质问「为什么放行了、
            #    没有重新生成」）。现在叠加第二判据（产物 meta.json / 上一轮 manifest 的
            #    软放行事实，见 _sb_dst_soft_facts）：非达标产物**不跳过**，交给下面的
            #    生成+质检链路真正重画。
            _prev_item = (_prev_by_key.get(str(shot_id))
                          or _prev_by_key.get(f"shot_{seq:02d}") or {})
            _dst_exists = bool(not overwrite and os.path.isfile(dst)
                               and os.path.getsize(dst) > 0)
            _dst_facts = (_sb_dst_soft_facts(dst, seq=seq, base_dir=out_dir,
                                             prev=_prev_item) if _dst_exists else {})
            # 口径（2026-10-13 用户定稿）：**只有「严重问题」禁止复用** ——
            #   critical_issues 非空 → 不跳过、重新生成；仅软放行（无严重问题）的图仍复用。
            #   收紧范围是有意的：2026-10-08 用户拍板「评分不达标但无 critical_issues 的
            #   仍走软放行」（防 VLM 判官双标把可用图挡在门外、白烧 GPU）。
            #   与 pipeline.probe_storyboard 的进入判据同口径，避免两侧漂移。
            _dst_unqualified = bool(_sb_critical_issues(_dst_facts))
            if _dst_exists and _dst_unqualified:
                logger.warning(
                    "镜头 %s 正式目录里的图**并非达标产物**（软放行/严重问题 %s）→ "
                    "本轮不跳过、重新生成（T13）",
                    shot_id, (_sb_critical_issues(_dst_facts) or ["（软放行）"])[:2])
            if _dst_exists and not _dst_unqualified:
                prev = _prev_item
                item = dict(prev) if prev else {}
                item.update({"shot_id": shot_id, "success": True, "skipped": True,
                             "file": dst, "error": "",
                             "url": f"{_sb_url_base}shot_{seq:02d}.png"})
                # ⭐ T13（必修3）：复用文案按**事实证明**取词，不再无条件写「沿用已达标图」
                #    （该文案在软放行条目上会与「严重问题 N」同框、自相矛盾）。
                item.setdefault("qc", {"enabled": False, "status": "skipped",
                                       "label": _sb_reuse_label(_dst_facts),
                                       "attempts": 0, "regenerated": 0})
                # 历史 manifest 残留的旧文案就地修正。
                _sb_fix_reuse_label(item)
                item.pop("qc_blocked", None)
                manifest_shots.append(item)
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / max(len(shots), 1) * 100),
                        "current_shot": shot_id,
                    })
                    generation_state[task_id]["results"].append(item)
                # T19：断点续跑复用的镜同样算「本镜已就绪」→ 推进场次齐备判定
                _vw_after_shot(seq)
                continue
            # ⭐ 2026-10-19（L3 角色覆盖）：本镜将真正重新生成 → 记为「本轮已处理」。
            #    断点续跑跳过的镜**不在此列**：它们上一轮的缺口必须保留在落盘记账里，
            #    否则「补跑 3 镜」会把其余镜的旧缺口一笔勾销（验收门误报通过）。
            _processed_ids.append(shot_id)
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int(i / max(len(shots), 1) * 100),
                    "current_shot": shot_id,
                    "phase": "分镜图生成",
                })
            item = {"shot_id": shot_id, "success": False, "refs": {}, "prompt": "",
                    "grid_layout": False}
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
                # ⭐ 2026-10-12 预取命中诊断（只加日志，不改行为）：
                # 实测前置段恒 84~93s（= 预检耗时），与「预取命中应为 ~0s」矛盾，
                # 故在此显式记录「命中 / 等线程 / 内联」三条路径与各自耗时，
                # 用真实数据判定预取到底有没有生效，而不是继续猜。
                _pf_t0 = time.perf_counter()
                pack = _pf_take(seq)
                _pf_path = "命中预取包"
                if pack is None:
                    with _pf_lock:
                        _pf_t = _pf_threads.get(seq)
                    if _pf_t is not None and _pf_t.is_alive():
                        # 预取线程还在跑同一段逻辑：等它收尾（与内联重跑等价但零重复 ——
                        # 同镜双跑会双打 LLM，且并发写同一张身份网格/基准图文件）。
                        # 线程内所有阻塞调用自带超时；daemon 线程不阻塞进程退出。
                        _pf_w0 = time.perf_counter()
                        _pf_t.join()
                        _pf_wait = time.perf_counter() - _pf_w0
                        _pf_path = "等预取线程(%.1fs)" % _pf_wait
                        pack = _pf_take(seq)
                        if pack is not None:
                            _pf_path += "→获包"
                    elif _pf_t is not None:
                        _pf_path = "线程已结束但无包"
                    else:
                        _pf_path = "未派出预取"
                if pack is None:
                    _pf_path += "→内联"
                    pack = _prep_shot(seq, shot)   # 预取未派出（重复镜号等）→ 内联
                logger.info("[预取诊断] shot=%s seq=%s 路径=%s 前置段总 %.1fs",
                            shot_id, seq, _pf_path, time.perf_counter() - _pf_t0)
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
                elif pack.get("char_refs_missing"):
                    # 2026-10-18：缺角色参考图 → 不出图（借鉴「清单驱动·缺图不出图」）。
                    item["success"] = False
                    item["file"] = ""
                    item.pop("url", None)
                    item["char_refs_missing"] = pack["char_refs_missing"]
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
                    # T3（2026-10-12）：manifest 的 refs 从「所属目录名」改成**资产身份串**
                    # （items/进境_整本小说/楼层安全守则/base.png）——旧口径下道具统一画幅后
                    # 一律记成 "ref_canvas"，23 个道具镜全一个值，看不出用的是哪张图；
                    # 身份串与绝对路径无关，可直接比对「#12 与 #26 是否同一张员工卡」。
                    # ref_paths 另存源图绝对路径（本地排查用；统一产物已反查回源图）。
                    _ref_ids, _ref_paths = manifest_ref_maps(refs, project_name)
                    item["refs"] = _ref_ids
                    item["ref_paths"] = _ref_paths
                    item["item_ref_notes"] = pack.get("item_ref_notes") or []
                    for _lab, _ident in _ref_ids.items():
                        if str(_ident).startswith("items/"):
                            logger.info("[道具参考图] shot=%s 标签=%s 身份=%s 源图=%s",
                                            shot_id, _lab, _ident, _ref_paths.get(_lab))
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

                    # ⭐ 编辑式修复的画布（2026-10-15 用户口径）：attempt>0 时指向上一版失败的图，
                    #    让重试走「以图改图」而不是「从零重画」。attempt==0 时为 None（正常生成）。
                    _fix_base = None
                    for attempt in range(_rounds):
                        if attempt > 0:
                            seed = random.randint(1, 2 ** 31 - 1)
                            # ⭐ 编辑式修复（2026-10-15 用户口径）：**不再从零重画**。
                            #    画布 = 上一次尝试的图（scratch 目录按 shot_XX_try{N}.png
                            #    命名，可直接推算上一轮），配低 denoise + 下面的修复
                            #    提示词，只改质检指出的缺陷。
                            #    实测：全量重生成 530s vs 编辑式修复 26~56s（快 10~20 倍）；
                            #    更重要的是保住已正确的构图/场景/其他角色/光线 —— 从零
                            #    重画会把对的部分一起丢掉，这正是「赵天霸发型越改越偏」的放大器。
                            _fix_base = os.path.join(
                                _storyboard_scratch_dir(project_name, _sb_ep),
                                f"shot_{seq:02d}_try{attempt}.png")
                            if not os.path.isfile(_fix_base):
                                logger.warning("[编辑式修复] 上一版图不存在（%s），回落全量重生成",
                                               os.path.basename(_fix_base))
                                _fix_base = None
                            # ⭐ 2026-10-18（借鉴同类项目「重roll 决策表」）：**身份/人数类
                            #    缺陷不做定点修复** —— 低 denoise 的局部编辑改不动「画的是
                            #    谁」，实测对「画错人 / 漏画 / 多画 / 克隆」不收敛，还可能
                            #    把已正确的构图/光线改坏。这类缺陷直接全量重生成（提示词
                            #    照常先做缺陷定向优化）。
                            if _fix_base and attempts:
                                _iss_txt = " ".join(
                                    str(x) for x in (attempts[-1].get("issues") or []))
                                _ID_DEFECTS = ("漏画", "多画", "出场角色", "角色不符",
                                               "画错", "克隆", "重复", "分身",
                                               "缺少角色", "人物缺失")
                                if any(t in _iss_txt for t in _ID_DEFECTS):
                                    logger.info(
                                        "[编辑式修复] 上轮缺陷属身份/人数类 → 跳过定点修复，"
                                        "全量重生成：%s", _iss_txt[:80])
                                    _fix_base = None
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
                                _pf_ok, _pf_miss = _storyboard_prompt_structurally_ok(_opt_prompt, False)
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
                                        # ⭐ 2026-10-17：LLM 即时改写 + 教训库都没给出改写时，把**上一轮
                                        #    缺陷当「修正指令」确定性追加** —— 保证「重写提示词→重出图」
                                        #    一定发生（用户口径），而不是只换种子白烧 GPU。
                                        _rec_fb = attempts[-1] if attempts else {}
                                        if not isinstance(_rec_fb, dict):
                                            _rec_fb = {}
                                        _iss_fb = "；".join(
                                            str(x) for x in (
                                                [_rec_fb.get("reason")]
                                                + list(_rec_fb.get("critical_issues") or [])
                                                + list(_rec_fb.get("issues") or [])
                                            ) if str(x or "").strip())[:600]
                                        if _iss_fb:
                                            prompt = _retry_base + \
                                                "\n\n【本轮质检缺陷·必须针对性修正，其余保持一致】" + _iss_fb
                                            item["prompt"] = prompt
                                            logger.info("镜头 %s 第 %d 次重试：LLM/教训库均无改写 → "
                                                        "追加确定性修正指令后重出图", shot_id, attempt + 1)
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
                            # 编辑式修复：有画布就只改缺陷（denoise<1），没有就全量生成
                            base_image=_fix_base,
                            denoise=(0.55 if _fix_base else None),
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
                        # P0（2026-10-12）：暂存目录**按集隔离**（第 1 集平铺 / 第 2 集起 epNN/），
                        # 与画布读取侧共用 _storyboard_scratch_dir，杜绝跨集串图。
                        sb_scratch_dir = _storyboard_scratch_dir(project_name, _sb_ep)
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
                        _shot_type = (shot.get("shot_type") or shot.get("camera") or "").strip()
                        if _shot_type and os.path.isfile(scratch_png):
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
                                # ⭐ 2026-10-13 T13（用户指令「严重问题不要放行」）：再叠加**严重问题**门槛 ——
                                #    gate.critical_issues 非空一律落硬阻断（走下方「未达标 → 阻断」分支），
                                #    **不再依赖词表命中措辞**。两道理并列：旧闸门靠措辞（结构类缺陷），
                                #    这道靠事实（shared_qc 独立复核写进 gate 的 critical_issues）。
                                #    实测：shot_11 的 critical_issues=「第5格手部握包带的指头结构模糊粘连，
                                #    指头分离度不清」——旧词表不含手部词 → 被软放行入库（用户质问
                                #    「为什么这个严重问题被放行了，没有重新生成」）。
                                #    ⚠️ 只收紧这一档：「评分不达标但无严重问题」的仍软放行（2026-10-08
                                #    用户拍板的初衷，防 VLM 判官双标把可用图挡在门外、白烧 GPU）。
                                and not _sb_soft_release_blocked(gate)):
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
                                    # ⭐ T12（2026-10-12）P0：软放行**绝不置 success**。
                                    #   旧实现同时写 item["success"]=True → manifest 报
                                    #   success_count=46/46、前端显示全绿，而实测 17/46 镜
                                    #   质检不达标（shot_28/44 还带 critical_issues）。
                                    #   现在拆成两件事：
                                    #     · delivered=True        —— 产物确已进正式目录
                                    #       （断点续跑/下游仍按文件存在判定，语义不变）
                                    #     · qc_soft_accepted=True —— 质量事实：放行了但有问题
                                    #     · success 保持 False     —— 不再冒充「质检通过」
                                    item["success"] = False
                                    item["delivered"] = True
                                    item["qc_soft_accepted"] = True
                                    # 同步进 item["qc"]：canvas 直接透出该字典，前端据此
                                    # 显示「软放行」徽标并显著提示 critical_issues。
                                    _qc_now = (item.get("qc")
                                               if isinstance(item.get("qc"), dict) else {})
                                    _qc_now.update({
                                        "soft_accepted": True, "delivered": True,
                                        "accept": True, "blocked": False,
                                        "entry_blocked": False, "asset_written": True,
                                        "soft_label": _soft_lbl, "soft_reason": _soft_rsn,
                                        "critical_issues": _sb_critical_issues(gate, _qc_now),
                                    })
                                    item["qc"] = _qc_now
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
                            # ⭐ 2026-10-12（T18 P0）：软放行会**终结本镜的重试循环**，
                            #    而教训库召回只发生在 attempt>0 的重试轮（见上方 _retry_base
                            #    分支的 prompt_memory.suggest/learned_prompt）—— 于是软放行
                            #    镜头「沉淀了教训却从不召回它」：T6 实测 storyboard 教训 84 条
                            #    仅 7 条被采纳、本集新增 32 条仅 1 条，闭环断裂即源于此。
                            #    这里补一次**定向召回**：用与 _record_qc_lesson 相同的稳定键
                            #    （orig_prompt）召回本镜缺陷对应的教训（分镜层高频缺陷就是
                            #    「面板雷同 / 景别」两类），使 use_count 计数闭环。
                            #    ⚠️ 纯记账 + 可观测字段：**不改本镜产物、不改放行结论**，
                            #    任何异常都被吞掉（召回失败绝不影响已完成的放行）。
                            if _soft_ok:
                                try:
                                    _soft_hints = prompt_memory.suggest(
                                        kind="storyboard", prompt=orig_prompt,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR,
                                        style=_qc_style_of(project_name))
                                    if _soft_hints:
                                        item["soft_release_recall"] = _soft_hints[:3]
                                        logger.info(
                                            "镜头 %s 软放行后定向召回教训 %d 条：%s",
                                            shot_id, len(_soft_hints), _soft_hints[:2])
                                    else:
                                        logger.info(
                                            "镜头 %s 软放行后定向召回：暂无可用教训",
                                            shot_id)
                                except Exception as _re:  # noqa: BLE001
                                    logger.warning(
                                        "镜头 %s 软放行后教训召回失败（忽略，不影响放行）：%s",
                                        shot_id, _re)
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
            logger.info("[分镜总计时] shot=%s 质量=%s 墙钟 %.1fs（前置段 %.1fs）",
                            shot_id,
                            ("通过" if item.get("success") else
                             ("软放行" if item.get("qc_soft_accepted")
                              else ("未交付" if not item.get("delivered") else "已交付"))),
                            _shot_elapsed, float(item.get("phase_secs_total") or 0.0))
            # 2026-10-08（用户要求）：逐镜上报「正在拍第几镜」，前端显示「分镜 16/41」
            #    而不是干等一个「已运行 N 分钟未推进」；顺带刷新 step_updated_at。
            try:
                import autopilot as _ap
                _ap.report_progress(
                    f"分镜 {i + 1}/{len(shots)}"
                    + ("" if (item.get("success") or item.get("qc_soft_accepted"))
                       else "（重试中）"))
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
            # T19：本镜**处理完毕即登记**（无论成功 / 软放行 / 质检硬阻断 / 生成失败）。
            #   预热只是「提前构建缓存」，绝不能因个别镜反复过不了质检（实测 shot_13
            #   手部大特写连续被打回）而把整条预热链永久卡死；某场次 1..N 镜都处理完
            #   即后台预热该场，缺图的镜由 video_helpers 内部按实际情况处理（fail-open）。
            _vw_after_shot(seq)

        # ⭐ T12（2026-10-12）：三类质量**分开统计** —— 软放行不计入 success。
        #   旧口径 ok=46/blocked=0 让界面全绿，掩盖了 37% 的不合格镜。
        # T12 收口：任何分支（含断点续跑复用旧条目信息）都不得出现
        # 「qc_soft_accepted=true 却计入 success」——统一规范化后再统计/落盘。
        _sb_normalize_soft_marks(manifest_shots)
        _tally = _sb_quality_tally(manifest_shots, base_dir=out_dir,
                                   use_artifact_meta=False)
        ok = _tally["success_count"]
        soft = _tally["soft_accepted_count"]
        blocked = _tally["qc_blocked_count"]
        delivered = _tally["delivered_count"]
        # ⭐ Phase 0.4（2026-10-16）：分镜阶段耗时入库（fail-open，只观测不改行为）。
        try:
            _sb_record_stage_analytics(project_name, episode_no, manifest_shots)
        except Exception as _an_err:  # noqa: BLE001
            logger.debug("分镜阶段耗时入库失败（忽略）：%s", _an_err)
        manifest = {
            "project_name": project_name,
            # 从模板表取，避免模型换代后 manifest 里还写着旧工作流名（口径漂移）
            "workflow": WORKFLOW_TEMPLATE.get("storyboard_gen", ""),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(manifest_shots),
            "success_count": ok,
            "soft_accepted_count": soft,
            "qc_blocked_count": blocked,
            "critical_issue_count": _tally["critical_issue_count"],
            "delivered_count": delivered,
            "soft_accepted_shot_ids": _tally["soft_shot_ids"],
            "critical_issue_shot_ids": _tally["critical_shot_ids"],
            # 质量口径版本：前端/接口可据此判断该 manifest 是否已按 T12 口径统计
            "quality_schema": 2,
            # T3：异常物件过渡判定结论（承担过渡的 shot_id + 每个异常首现镜的判定明细）
            "transition_plan": transition_plan,
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
                    "soft_accepted_count": soft,
                    "qc_blocked_count": blocked,
                    "critical_issue_count": _tally["critical_issue_count"],
                    "delivered_count": delivered,
                    "output_dir": out_dir,
                    "error": f"分镜 manifest 落盘失败：{_mp_err}",
                })
            return

        with lock:
            generation_state[task_id].update({
                # ⭐ T12：状态判据用 **delivered**（真通过 + 软放行 = 正式目录里真有图），
                #   不是 success —— 否则「整集都软放行」会被判 failed，托管流水线在
                #   图已齐的情况下反复重烧。质量事实由 soft_accepted_count /
                #   critical_issue_count 如实上报，不再靠 status 掩盖。
                "status": "completed" if delivered else "failed",
                "progress": 100,
                "success_count": ok,
                "soft_accepted_count": soft,
                "qc_blocked_count": blocked,
                "critical_issue_count": _tally["critical_issue_count"],
                "delivered_count": delivered,
                "output_dir": out_dir,
                "error": "" if delivered else "所有镜头分镜图均生成失败",
            })
    except Exception as e:
        logger.error(f"分镜图任务失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    # ⭐ 2026-10-19（L3 角色覆盖）：把「角色参考图缺失 / no_reference」记账落盘。
    #    · 必须在这里做：本 worker 对 shots 做了浅拷贝，告警回不到 pipeline 的 ctx；
    #    · 只合并**本轮真正生成过**的镜号（跳过的镜保留上一轮缺口），
    #      否则补跑几镜会把其余镜的旧缺口一笔勾销 → 验收门误报通过。
    try:
        import quality_stage as _qs_ref
        _qs_ref.save_ref_warnings(project_name, episode_no, shots,
                                  processed_ids=_processed_ids)
    except Exception as _ref_err:  # noqa: BLE001
        logger.warning("角色参考图记账落盘失败（忽略）：%s", _ref_err)
    # ★ 用户决策 4：ComfyUI 侧分镜参考图上传残留（sb_ref_*）只在**本轮分镜批量生成全部
    # 结束后**按项目清理一次 —— 不在单镜循环里调（那会在重试中途删掉当前镜头正在用的参考图）。
    try:
        _purge_sb_refs(project_name)
    except Exception as _sb_ref_err:  # noqa: BLE001
        logger.warning(f"sb_ref 残留清理失败（忽略）：{_sb_ref_err}")
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    # 分镜批量是本项目重跑最密集的环节（每失败一次多一条任务历史）→ 收尾顺手清历史面板。
    _maybe_clear_comfyui_history("storyboard 批量生成收尾")


def prewarm_storyboard_prompts(project_name: str, shots: list, char_idx: dict,
                               item_idx: dict, scene_idx: dict, episode_no=None,
                               style: str = ""):
    """依赖驱动预热：资产就绪后，后台预热各镜分镜提示词（只填缓存，不碰 GPU）。

    用户早期明确要求（一直未落地）：「分镜和视频提示词也要预热啊」「比如这个分镜需要
    角色1、物品1、场景1，在这个生成后就可以开始预热了」。本函数补上『资产完成 → 预热
    分镜提示词』这半边：某一镜所需的角色/物品/场景资产全部就绪后，立即在后台把该镜
    前置段（增强 + 预检）提前跑一遍，只填 prompt_qc / prompt_enhance / 九宫格规划 /
    3D基准图缓存，不落分镜产物。

    安全纪律（与 asset_worker._prewarm_asset_prompts 同范式）：
      · daemon 线程 + 抓异常：预热失败绝不影响主线；
      · 只填缓存，不碰 ComfyUI、不抢 GPU、不写教训库、不改 results；
      · 复用 _storyboard_worker(prewarm_only=True)，与正式跑共用同一段 _prep_shot，
        缓存 key 不漂移（这正是 prompt_qc 预检缓存作为命中层的意义）。

    开关：env MJSCXT_PREWARM_STORYBOARD_PROMPT=0 可强制关闭（默认恒开 —— 与资产
    预热同口径：只填缓存、纯 LLM、与出图并行，关掉毫无收益）。
    """
    try:
        if str(os.environ.get("MJSCXT_PREWARM_STORYBOARD_PROMPT") or "1").strip().lower() \
                in ("0", "false", "off", "no"):
            return
        if not shots:
            return
        _tid = f"prewarm_sb_{int(time.time() * 1000)}_{os.getpid()}"

        def _run():
            try:
                _storyboard_worker(
                    _tid, project_name, list(shots), char_idx, item_idx, scene_idx,
                    episode_no, style, overwrite=False, prewarm_only=True)
            except Exception as _pw_e:  # noqa: BLE001  预热整体失败不影响生产
                logger.warning("[分镜提示词预热] 调度失败（忽略，不影响生产）：%s", _pw_e)

        threading.Thread(target=_run, daemon=True,
                         name=f"prewarm-sb-{project_name}").start()
    except Exception as _pw_e:  # noqa: BLE001
        logger.warning("[分镜提示词预热] 启动失败（忽略）：%s", _pw_e)


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
                "4. 只修补质检列出的问题、**其余描述逐字保留**（不要重写整篇、不要改变篇幅量级）；"
                "不得引入任何新物体、新容器、新场景元素。"
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

