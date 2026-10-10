# -*- coding: utf-8 -*-
'''关键帧助手（2026-10-11 从 app.py 下沉，助手域第六批）。'''

# 本批 = _keyframe_* 助手及其依赖闭包。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
# import 由脚本从 app.py 原始语句原样复制（不猜来源）。
#
# 收尾修复记录：首版用纯字符串匹配展开闭包，把通用词 index / static_assets
# 误当依赖搬走并删除，导致根路径与静态资源路由悬空、首页报 TypeError。
# 现已把闭包展开限制为「下划线开头或全大写常量」，并从 git 恢复 app.py 后重做。
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
from routes._shared import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir, _qc_load_cfg  # noqa: F401  再导出
from shared_web import _serve_safe  # noqa: F401  再导出
from routes._shared import COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _apply_project_settings, _comfy_view_url, _project_style, _prune_task_registry, _qc_gate, _qc_record, _qc_record_verdict, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks  # noqa: F401  再导出
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
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
import json
import keyframe
import os
import prompt_memory
import prompt_qc
import qc_client

logger = logging.getLogger(__name__)

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
            logger.warning(f"分镜 manifest 读取失败：{e}")
    return sb_map


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
            logger.info(
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
                logger.warning("提示词教训沉淀失败（忽略）：%s", _pf_lesson_err)
        return pf["prompt"], pf, gate
    except Exception as e:  # noqa: BLE001
        logger.warning(f"提示词预检异常（{kind}），按放行处理：{e}")
        skip = {"ok": False, "skipped": True, "accept": True, "blocked": False,
                "label": "提示词预检异常", "reason": str(e), "repairs": [],
                "verdict": {"issues": [], "critical_issues": [], "reason": str(e)}}
        return text, skip, {"accept": True, "blocked": False, "skipped": True,
                            "label": "提示词预检异常", "reason": str(e),
                            "critical_issues": [], "repairs": []}


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
            logger.warning(f"尾帧教训召回失败（忽略）：{e}")
            return orig_prompt or ""
    return _recall


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
        logger.warning(f"尾帧质检构建资产索引失败（本轮不带设定图）：{_e}")
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
            logger.warning(f"尾帧质检记录/教训沉淀失败（忽略）：{e}")
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
            logger.warning(f"尾帧提示词召回失败（忽略）：{e}")
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
                logger.warning(f"尾帧提示词教训沉淀失败（忽略）：{e}")
        return pf

    return _pre, True

