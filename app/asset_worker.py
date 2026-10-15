# -*- coding: utf-8 -*-
'''资产生成 worker（2026-10-11 从 app.py 下沉）。'''

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
    # 场景多视角九宫格的「按剧情判断」门槛（2026-10-10 用户要求）
    SCENE_GRID_MIN_SHOTS,
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
from system_helpers import (_COMFYUI_CLEAR_HISTORY_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_CLEAR_HISTORY_LOCK, _maybe_clear_comfyui_history)
from artifact_helpers import (_COMFYUI_RECLAIM_INTERVAL_SEC, _COMFYUI_RECLAIM_LAST_TS,  # noqa: F401, E402
                               _COMFYUI_RECLAIM_LOCK, _PURGE_REJECTED_ENV, _comfyui_official_dirs,
                               _mark_history_file_purged, _maybe_reclaim_comfyui_output,
                               _purge_prompt_records, _purge_rejected_artifacts,
                               _purge_rejected_enabled, _purge_sb_refs, _reject_artifact)
from routes.projects import _collect_project_cast_images, _cover_prompt_from_outline, _move_with_retry, _project_cover_path  # noqa: F401  再导出
from storyboard_helpers import (  # noqa: F401, E402
    _GRID_PLAN_TPL_FP, _OPT_REASONING_MARKERS, _TE3D_RENDER_LOCK,
    _blocking_spec_text, _build_identity_ref_grid, _fit_ref_to_canvas,
    _grid_panel_plan, _grid_plan_template_fingerprint, _optimize_prompt_from_qc,
    _ref_canvas_target, _sanitize_optimized_prompt, _storyboard_prompt_structurally_ok,
    _storyboard_retry_shot_impl, _storyboard_scratch_map, _storyboard_worker,
    _unify_ref_canvas, _update_storyboard_manifest_shot, _write_artifact_meta)
from keyframe_helpers import (  # noqa: F401, E402
    _ep_of_script, _keyframe_prompt_preflight, _keyframe_qc_verifier,
    _keyframe_recall_cb, _keyframe_sb_map, _prompt_preflight)
from shared_web import _serve_safe  # noqa: F401  再导出
from shared_qc import _qc_gate, _qc_record, _qc_record_verdict  # noqa: F401  再导出
from shared_tasks import _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _prune_task_registry  # noqa: F401  再导出
from shared_upscale import COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _comfy_view_url, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks  # noqa: F401  再导出
from shared_project import _apply_project_settings, _project_style  # noqa: F401  再导出
from shared_qc import _qc_load_cfg  # noqa: F401  再导出
from shared_project import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir  # noqa: F401  再导出
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
import asset_name_match
import asset_prompt_kit
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
import consistency
from job_state import (generation_state, lock)  # noqa: F401
import os
import prompt_memory
import uuid   # 服装变体任务 id 生成（outfit_<项目>_<key>_<uuid8>）
import qc_client
import random
#: ⭐⭐ 2026-10-12 用户口径：「角色是群像、后续剧情也与他无关时**不用生成三视图**，
#:    直接生成一张多人图就行了 —— 这样设计是不是好一点」。
#:    确实是更好的成本分级。判据必须来自**已落盘的剧本事实**（同 _scene_shot_count 的
#:    思路：确定性、可复现、零 LLM 成本），而不是让模型每次judge。
#:
#: ⭐⭐ 2026-10-12 实测修正：群像词必须分**强 / 弱**两档 ——
#:
#:    原先只有一档，结果「外门围观弟子」因为「有 8 镜 4 台词」被判成 supporting，
#:    去出双格立绘。但它名字本身就是一个**集合名词**（一群人），出合影才对；
#:    即便「人群里有人说了 4 句话」，那也只是群像戏里有人开口，不需要给「他」单独立绘。
#:
#:    反过来，「便利店女店员」里的「店员」却常指**单个人**，若也当群像就会漏掉一个
#:    真正需要立绘的角色。所以拆成两档，只有**强群像词**才无条件走合影。

#: 强群像词：命中即**必然是一群人**（集合名词）→ 直接判 crowd，优先于镜头/台词判定。
_CROWD_NAME_MARKERS = (
    "群演", "群像", "围观", "路人", "群众", "众人", "人群", "人群们",
    "甲乙丙丁", "围观者", "群众演员", "龙套", "学生们", "士兵们", "弟子们",
    "们）", "们)", "众弟子", "众弟子们",
)

#: 弱群像词：可能指一群人也可能指单个（店员 / 侍卫 / 士兵…）→
#: 只在**没有独立戏份**时（镜头少且无台词）才按 crowd 处理。
_CROWD_WEAK_MARKERS = (
    "弟子", "侍卫", "侍从", "随从", "护卫", "士兵",
    "乘客", "行人", "店员", "服务员", "保安", "杂役", "学徒", "员工",
)

#: ⭐⭐ 2026-10-12 用户指定「四档成本分级」（产出由全到简，成本递减）：
#:     primary    → 四区 character sheet（正/侧/背 + 面部特写 + 配色板 + 细节/比例）
#:     supporting → 双格设定图（正面 + 侧面）—— 够用且省掉另外两区
#:     minor      → 单张正面立绘
#:     crowd      → 多人合影（同批群像共用一张）
#:   阈值全部可用环境变量覆盖，便于按项目调。

#: primary 门槛：镜头数 ≥ 此值 → 贯穿全剧的主角（必须给最完整的设定图）。
CHARACTER_PRIMARY_MIN_SHOTS = int(os.getenv("MJSCXT_CHAR_PRIMARY_SHOTS", "15"))

#: supporting 门槛：镜头数 ≥ 此值，**或**台词数 ≥ 台词门槛 → 主要配角（经常单独出镜）。
CHARACTER_SUPPORT_MIN_SHOTS = int(os.getenv("MJSCXT_CHAR_SUPPORT_SHOTS", "6"))
CHARACTER_SUPPORT_MIN_LINES = int(os.getenv("MJSCXT_CHAR_SUPPORT_LINES", "4"))

#: 兼容旧名（历史代码/文档引用过）：等价于 supporting 的镜头门槛。
CHARACTER_TIER_MIN_SHOTS = CHARACTER_SUPPORT_MIN_SHOTS


def _character_tier(project_name: str, char: dict, episode_no=None) -> str:
    """判定角色级别：``primary`` / ``supporting`` / ``minor`` / ``crowd``。

    用户口径（2026-10-12 四档成本分级）：
      primary    → 四区 character sheet（贯穿全剧，一致性是生命线，给最全的）
      supporting → 双格设定图（正面 + 侧面）—— 经常单独出镜，但用不到面部特写/配色板
      minor      → 单张正面立绘 —— 只露一两次脸，够用
      crowd      → 多人合影（同批群像共用一张；画风天然统一）

    判据（全部来自剧本事实，确定性、零 LLM 成本）：
      1. 角色/bible 里**显式写死的 tier** → 直接采纳（用户手改过就永远听他的）；
      2. 读不到剧本 → ``unknown``（调用方按旧行为出四区，绝不因此卡住资产）；
      3. 镜头数 ≥ CHARACTER_PRIMARY_MIN_SHOTS → primary；
      4. 镜头数 ≥ SUPPORT_MIN_SHOTS 或 台词数 ≥ SUPPORT_MIN_LINES → supporting；
      5. **只要出镜过（≥1 镜）或有台词** → minor ——
         对应「后续哪怕只有一句台词，也不能只从合影裁脸」这条前提：
         有独立戏份就必须有属于自己的图，只是不用给最全的版面；
      6. 名字命中群像标记词，或**一镜未出** → crowd（只活在群像场里）。

    实测分层（进境_整本小说 / 60 镜）：羡进 46 镜 → primary；灰制服女人 7 镜 4 台词
    → supporting；便利店女店员 3 镜 3 台词、红工牌女人 3 镜 2 台词 → minor；
    陈默/林栖/无脸员工 0 镜 → crowd。
    """
    try:
        # 1) 显式覆盖优先
        for k in ("tier", "character_tier", "role_tier"):
            v = str((char or {}).get(k) or "").strip().lower()
            if v in ("primary", "supporting", "minor", "crowd"):
                return v
        name = str((char or {}).get("name") or "").strip()
        if not name:
            return "unknown"
        import shared_episode
        # ⚠️ 2026-10-14 修正（用户口径「角色评级要根据整本小说来」）：
        #    原实现 shared_episode._load_script_for(project_name, episode_no) 在缺集号时
        #    **取第一集**，于是评级只看第一集（详见 _all_project_shots 的注释）。
        #    现汇总该项目全部已生成集；汇总失败才退回旧的单集口径（fail-open）。
        _shots_all = _all_project_shots(project_name)
        if _shots_all is None:
            script = shared_episode._load_script_for(project_name, episode_no)
            if not isinstance(script, dict):
                return "unknown"
            shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
        else:
            shots = _shots_all
        if not shots:
            return "unknown"
        if not shots:
            return "unknown"
        # 2) ⭐ 强群像词优先：名字本身就是集合名词（外门围观弟子 / 路人甲 / 群众…）
        #    → 直接判 crowd，**不参与**后面的镜头数/台词判定。
        #    理由见 _CROWD_NAME_MARKERS 注释：这类「角色」在原文里就是一群人，
        #    给「他」单独立绘没有意义；人群里有人说话也不改变这一点。
        if any(m in name for m in _CROWD_NAME_MARKERS):
            return "crowd"
        appear = [s for s in shots if name in (s.get("characters_in_shot") or [])]
        # 台词数（同一角色在多句台词里分别计数）
        _lines = 0
        for s in appear:
            for d in (s.get("dialogue") or []):
                if isinstance(d, dict) and str(d.get("speaker") or "").strip() == name:
                    _lines += 1
        # 3) 主角
        if len(appear) >= CHARACTER_PRIMARY_MIN_SHOTS:
            return "primary"
        # 4) 主要配角
        if len(appear) >= CHARACTER_SUPPORT_MIN_SHOTS or _lines >= CHARACTER_SUPPORT_MIN_LINES:
            return "supporting"
        # 5) 有独立戏份（出镜过或有台词）→ 次要角色，仍单独出图
        if appear or _lines > 0:
            return "minor"
        # 6) 一镜未出 → 群像（只活在合影里）；弱群像词同理由此兜底。
        return "crowd"
    except Exception as e:  # noqa: BLE001 - 判定失败按旧行为（生成），不拦资产
        logger.debug("角色级别判定失败（按正常生成处理）：%s", e)
        return "unknown"


def _all_project_shots(project_name: str):
    """汇总该项目**全部已生成集**的镜头（角色评级用）；读不到时返回 None。

    ⚠️ 2026-10-14 新增（用户口径「角色评级要根据整本小说来」）：
    评级原先走 shared_episode._load_script_for(project_name, None)，而它
    「缺集号时取该项目第一集」—— 于是评级只统计第一集：
      · 第 2 集之后才登场的主角，第 1 集「一镜未出」→ 被判 crowd（多人合影）；
      · 「第 1 集只 3 镜、之后是主要角色」的配角 → 被判 minor（单张立绘）。
    实测本项目：林清雪第 1 集 3 镜 2 台词 → minor，资产只出单张立绘、缺全部细节区。

    返回 None 表示「汇总失败」（调用方退回旧的单集口径，fail-open）；
    返回 [] 表示「该项目一集剧本都没有」（调用方按 unknown 处理）。
    """
    try:
        import novel_to_script  # noqa: PLC0415
        import project_store  # noqa: PLC0415
        from config import SCRIPT_DIR  # noqa: PLC0415
        key = project_store.safe_key(project_name)
        eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
        if not eps:
            return []
        out = []
        for e in eps:
            epno = e if isinstance(e, int) else (e.get("episode_no") or 0)
            if not epno:
                continue
            try:
                sc = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(epno))
            except Exception:  # noqa: BLE001  单集读失败不影响其它集
                continue
            if isinstance(sc, dict):
                out.extend(s for s in (sc.get("shots") or []) if isinstance(s, dict))
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("汇总项目全部剧本失败（退回单集口径）：%s", e)
        return None


def _crowd_names_for(project_name: str, episode_no=None) -> list:
    """列出本集所有「群像」角色名 —— 合影要按这份名单把人都画进去。

    为什么必须给全名单：合影是这群人的「定妆照」，下游群像镜头直接拿它当参考图。
    只写「一群人」会让模型自由发挥，画面里可能根本没有剧本真正要的那几个角色，
    参考图就失去意义（这一点与用户提的「合影要带上主角/已知角色，否则会跑偏」同理）。

    兜底：读不到剧本 / 判定异常 → 返回空列表，调用方回落「非群像」路径（照常出四区
    三视图），绝不因为读不到剧本把资产卡住。
    """
    try:
        # ⚠️ 2026-10-12 实测踩坑：**不能用剧本的 characters** —— 那里只有「本集出场的
        #    少数几人」（实测某个 7 角色的项目，剧本里只列了 4 个），拿它当合影名单会
        #    漏掉同集其他群像角色，合影里就少了人。角色全集在 **bible**（跨集累积）。
        chars = []
        try:
            from config import CONTINUITY_DIR as _cd
            import continuity as _cont
            _bible = _cont.load_bible(_cd, project_name) or {}
            chars = [c for c in (_bible.get("characters") or []) if isinstance(c, dict)]
        except Exception as _be:  # noqa: BLE001
            logger.debug("读取 bible 失败，回落剧本角色表：%s", _be)
        if not chars:
            import shared_episode
            script = shared_episode._load_script_for(project_name, episode_no)
            chars = [c for c in ((script or {}).get("characters") or [])
                     if isinstance(c, dict)]
        if not chars:
            return []
        out = []
        for c in chars:
            if not isinstance(c, dict):
                continue
            nm = str(c.get("name") or "").strip()
            if not nm or nm in out:
                continue
            if _character_tier(project_name, c, episode_no) == "crowd":
                out.append(nm)
        return out
    except Exception as e:  # noqa: BLE001 - 判定失败不拦资产
        logger.debug("群像名单收集失败（按非群像处理）：%s", e)
        return []


def _scene_shot_count(project_name: str, scene_name: str, episode_no=None) -> int:
    """该场景在本集剧本里**承载的镜头数** —— 判断是否需要多视角九宫格的客观信号。

    ⭐ 2026-10-10 用户要求：「场景要根据剧情判断是否相应生成多视角9宫格」。
    为什么用「镜头数」而不是让模型判断：镜头数是**已落盘的剧本事实**（确定性、
    可复现、零 LLM 成本），而「这个场景会不会被多角度拍」在剧本阶段就已经定下来了
    —— 出现 3 镜的场景必然要换机位，只出现 1 镜的背景场景则不必浪费 9 格算力。

    返回 -1 表示**读不到剧本**（尚未生成 / 读取异常）→ 调用方按"不拦"处理，
    保持旧行为（宁可想生成就生成，也不要因为读不到剧本把资产卡住）。
    """
    try:
        import shared_episode
        script = shared_episode._load_script_for(project_name, episode_no)
        if not isinstance(script, dict):
            return -1
        want = str(scene_name or "").strip()
        if not want:
            return -1
        n = 0
        for sh in (script.get("shots") or []):
            if not isinstance(sh, dict):
                continue
            if str(sh.get("location") or "").strip() == want:
                n += 1
        return n
    except Exception as e:  # noqa: BLE001 - 判定失败不拦（保持旧行为）
        logger.debug("场景镜头数统计失败（按需要多视角处理）：%s", e)
        return -1


def _scene_needs_multiview(project_name: str, scene_name: str, episode_no=None) -> bool:
    """按剧情判断该场景是否需要多视角九宫格。

    · 镜头数 >= SCENE_GRID_MIN_SHOTS（默认 3）→ 需要（多机位复用，必须有机位一致性）；
    · 镜头数 <  该值 → 不需要（如只作背景交代的单镜场景，单图足够）；
    · 读不到剧本（-1）→ 返回 True（不拦，保持旧行为）。
    """
    n = _scene_shot_count(project_name, scene_name, episode_no)
    if n < 0:
        return True
    need = n >= int(SCENE_GRID_MIN_SHOTS or 3)
    if not need:
        logger.info("场景「%s」本集仅 %d 镜（< %d），按剧情判断**不需要**多视角九宫格，"
                    "只出基础图（省算力）", scene_name, n, SCENE_GRID_MIN_SHOTS)
    return need


from character_helpers import (  # noqa: E402
    _character_base_prompt, _find_script_character,
)
# ⭐ 2026-10-11：这两个都由 character_helpers 定义，app.py:326 是三个一起导入的，
#    asset_worker 此前一个都没导入 —— 用了却未导入的只有「服装变体」分支会走到，
#    所以主链路一直没暴露。本次按静态分析（AST 扫"模块内无定义/无导入的名字"）
#    一次性补齐，避免修完一个又冒一个。
# ⭐ 2026-10-11 修复：[服装变体] 出图失败 NameError: name '_find_script_character' is not defined
#    该函数定义在 character_helpers，asset_worker 使用了它却没导入 ——
#    与 shared_episode 缺 import os 同类（模块拆分时漏了依赖）。
#    只在「服装变体」分支触发，故此前未被主链路暴露。


import scene_grid
import sheet_split
import shutil
import style_kit
import threading

logger = logging.getLogger(__name__)

_ITEM_OWNER_REF_PRIORITY = ("front.png", "front.jpg", "half.png", "half.jpg")


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
        logger.info("[物品主人形象] 主人「%s」未匹配到角色资产（项目 %s），回落纯 T2I",
                        owner_s, project_name)
        return ""
    if level != asset_name_match.LEVEL_EXACT:
        logger.info("[物品主人形象] 主人「%s」经 %s 匹配到角色「%s」（项目 %s）",
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
    logger.info("[物品主人形象] 角色「%s」目录内无可用单人参考图（已排除 base），"
                    "回落纯 T2I（项目 %s）", key, project_name)
    return ""


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
                logger.warning("资产子目录参数非法，按主设定目录处理：%r", sub_dir)
                sub_dir = ""
            else:
                sub_dir = _sd

        # ⭐ 2026-10-10 角色换装出图（用户要求）：剧本阶段已登记 outfits/<key>/outfit.json
        #    （pending），这里在主设定图生成完成后把它们画出来。
        #    为什么必须出图：消费侧 _pick_char_view 找的是 outfits/<key>/base.png，
        #    只有 json 没有图 → 仍回落主设定图 → 换装不生效（这正是该功能此前
        #    「机制齐全却从未生效」的原因：登记侧此前根本没人调用）。
        #    串行执行，避免同项目多变体抢 GPU；fail-open，绝不影响主资产任务。
        def _pending_outfit_variants(chars: list) -> list:
            """扫描这些角色下「有 outfit.json 但无 base.png」的变体，返回资产描述列表。"""
            out = []
            try:
                import json as _json
                from config import CHARACTERS_DIR as _CH
                for _nm in (chars or []):
                    _nm = str(_nm or "").strip()
                    if not _nm:
                        continue
                    _od = os.path.join(_CH, project_name, _nm, "outfits")
                    if not os.path.isdir(_od):
                        continue
                    for _key in sorted(os.listdir(_od)):
                        _vd = os.path.join(_od, _key)
                        if not os.path.isdir(_vd):
                            continue
                        _base = os.path.join(_vd, "base.png")
                        if os.path.isfile(_base) and os.path.getsize(_base) > 0:
                            continue                      # 已出图
                        _rec = os.path.join(_vd, "outfit.json")
                        if not os.path.isfile(_rec):
                            continue
                        try:
                            with open(_rec, "r", encoding="utf-8") as _f:
                                _info = _json.load(_f) or {}
                        except Exception:  # noqa: BLE001
                            continue
                        _desc = str(_info.get("desc") or "").strip()
                        if not _desc:
                            continue
                        out.append((_nm, _key, _desc))
            except Exception as _e:  # noqa: BLE001
                logger.debug("扫描待出图换装变体失败（跳过）：%s", _e)
            return out

        def _spawn_outfit_variant_jobs(chars: list) -> None:
            """为待出图的换装变体各起一个后台任务（串行在同一线程里，避免抢 GPU）。

            复用 _generate_asset_task 本身（sub_dir=outfits/<key>），因此生成→质检→
            重试→切分全链路与主设定图完全一致，零新增生成代码。
            """
            _pend = _pending_outfit_variants(chars)
            if not _pend:
                return
            logger.info("[服装变体] 本批资产完成后待出图换装 %d 个：%s", len(_pend),
                        ", ".join("%s/%s" % (x[0], x[1]) for x in _pend))

            def _run() -> None:
                for _nm, _key, _desc in _pend:
                    try:
                        _ast = _find_script_character(project_name, _nm) or {}
                        _bp = _character_base_prompt(project_name, _nm) or ""
                        _merged = (_bp + "；本集服装：" + _desc) if _bp else ("角色设定图；本集服装：" + _desc)
                        _ast.update({"name": _nm, "reference_prompt_zh": _merged,
                                     "prompt_zh": _merged})
                        if not str(_ast.get("appearance") or "").strip():
                            _ast["appearance"] = _merged
                        _tid = "outfit_%s_%s_%s" % (project_name, _key,
                                                    uuid.uuid4().hex[:8])
                        with lock:
                            generation_state[_tid] = {
                                "status": "running", "asset_type": "character",
                                "progress": 0, "total": 1, "current": 0,
                                "phase": "服装变体", "results": [],
                                "overwrite": False, "project_name": project_name,
                                "step": "asset_outfit", "character": _nm,
                                "outfit_key": _key,
                            }
                        _generate_asset_task(_tid, [_ast], "character", project_name,
                                             _ast.get("style") or "",
                                             False, os.path.join("outfits", _key))
                    except Exception as _e:  # noqa: BLE001  变体失败不影响主资产
                        logger.warning("[服装变体] %s/%s 出图失败：%s: %s",
                                       _nm, _key, type(_e).__name__, _e)

            _t = threading.Thread(target=_run, daemon=True)
            _t.start()

        # ⭐ 2026-10-10 场景空间关联（用户需求①②③）：资产生成前确保关联已算。
        #    为什么放在这里：assets 由**前端**传入（不含 refs / fixed_structure），
        #    服务端必须先补齐 —— 否则 generate_scene_base 拿不到关联参考图，
        #    ensure_scene_layout 也拿不到 fixed_structure，门框仍会对不上。
        #    幂等：bible 里已有该字段的场景跳过，只有缺失时才调一次模型；
        #    fail-open：关联分析失败按「无关联」继续，绝不阻断资产生产。
        def _ensure_scene_relations(asset_items: list, proj: str) -> None:
            try:
                if not any(isinstance(x, dict) for x in (asset_items or [])):
                    return
                missing = [x for x in (asset_items or [])
                           if isinstance(x, dict)
                           and (not x.get("refs") or not x.get("fixed_structure"))]
                if not missing:
                    return
                from config import CONTINUITY_DIR as _cd
                import continuity as _cont
                import scene_relations as _sr
                bible = _cont.load_bible(_cd, proj) or {}
                rows = [s for s in (bible.get("scenes") or []) if isinstance(s, dict)]
                if not rows:
                    return
                need = [s for s in rows
                        if not s.get("refs") and not s.get("fixed_structure")]
                if need:
                    from shared_ai import _optional_llm_client
                    _cli = _optional_llm_client()
                    if _cli is not None:
                        _rel = _sr.relate_scenes(_cli, rows)
                        _stat = _sr.apply_scene_relations(bible, _rel)
                        if _stat.get("updated"):
                            _cont.save_bible(_cd, proj, bible)
                            logger.info("[场景关联] 已写入 %d 个场景（分组/参考/固定结构）",
                                        _stat["updated"])
                # 把 bible 的值回填进本次要生成的 asset（内存对象；前端传的不含这些字段）
                by_name = {str(s.get("name") or "").strip(): s for s in rows}
                for x in (asset_items or []):
                    if not isinstance(x, dict):
                        continue
                    src = by_name.get(str(x.get("name") or "").strip())
                    if not src:
                        continue
                    for k in ("refs", "fixed_structure", "space_group", "is_anchor"):
                        if not x.get(k) and src.get(k):
                            x[k] = src[k]
            except Exception as _e:  # noqa: BLE001  关联是增强，绝不阻断资产生产
                logger.warning("[场景关联] 资产生成前的关联补齐失败（按无关联继续）：%s: %s",
                               type(_e).__name__, _e)

        def _scene_relation_refs(asset: dict, project_name: str) -> list:
            """取该场景的**关联参考图**（已存在的同组场景 base.png，最多 2 张）。

            依据 asset["refs"]（由 scene_relations.relate_scenes 写回 bible.scenes[]）。
            取不到 / 文件缺失 / 任何异常 → 空列表（调用方回落纯 T2I，零回归）。
            """
            try:
                import scene_relations as _sr
                return _sr.scene_ref_images(asset or {}, SCENES_DIR, project_name)
            except Exception as _e:  # noqa: BLE001
                logger.debug("场景关联参考图解析失败（按无参考处理）：%s", _e)
                return []

        if asset_type == "scene" and not sub_dir:
            _ensure_scene_relations(assets, project_name)

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
        logger.info("[资产风格] %s 资产生成风格=%s；内置画幅 %s×%s（%.2fMP）",
                        asset_type, gen_style or style, _base_ratio[0], _base_ratio[1],
                        style_kit.asset_megapixels())

        # 质检配置：任务级读取一次，本任务内所有资产共用
        qc_cfg = _qc_load_cfg()
        qc_on = qc_client.image_qc_ready(qc_cfg)          # 质检接口是否可用
        qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
        if qc_declared and not qc_on:
            # 已声明开启图片质检但接口不可用：后续逐个资产明确阻断，绝不静默放行
            logger.error("[资产质检] 图片质检已开启但接口未就绪（base_url/api_key/model 不完整），"
                             "本次资产生成将阻断入库；请检查 qc_config.json")
        max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
        scratch_root = os.path.join(QC_DIR, project_name, "assets_scratch")

        results = []
        # ===================== 资产提示词预热（2026-10-10 用户指定）=====================
        # 用户要求：「要不止预热一个资产」—— 即在本批资产**出图期间**，把**本批全部**
        # 资产的增强提示词先算好，而不是每轮到某个资产才开始拼/增强。
        #
        # 原理与安全性：
        #   · 只调 prompt_enhance.enhance_prompt()，它命中/写入的是 process 内线程安全
        #     缓存（_OK_CACHE + _CACHE_LOCK）→ 预热与正式跑用同一个 key（kind|style|salt|prompt）
        #     → 正式跑时秒过，**零重复 LLM 调用**；
        #   · 纯 LLM，零 GPU，不与 ComfyUI 抢卡（8G 显存跑不了并发出图，但 LLM 可以并行）；
        #   · 只填缓存，**不落任何产物、不写教训库、不改 results** —— 预热失败绝不影响主线；
        #   · 限流 max_workers=3：LLM 网关本会话已多次出现「只返回思考内容」（实测 105 次），
        #     并发过高会加剧；3 是「够用又不压垮网关」的经验值。
        # 开关：plan/pipeline 配置 prewarm_asset_prompt（界面可勾选，默认开）。
        def _prewarm_asset_prompts(_assets, _atype, _gstyle, _pname):
            """后台预热本批资产的增强提示词（只填 prompt_enhance 缓存）"""
            try:
                if str(os.environ.get('MJSCXT_PREWARM_ASSET_PROMPT') or '1').strip().lower() \
                        in ('0', 'false', 'off', 'no'):
                    return
                import prompt_enhance as _pe
                if not _pe.enhance_enabled():
                    logger.info('[资产提示词预热] 增强层未启用，跳过')
                    return
                import prompt_memory as _pm
                import comfyui_client as _cc_mod
                from concurrent.futures import ThreadPoolExecutor
                _done = {'n': 0, 'skip': 0}

                def _one(_a):
                    try:
                        _nm = str(_a.get('name') or '').strip()
                        if not _nm:
                            return
                        _p = str(_a.get('reference_prompt_zh')
                                 or _a.get('prompt_zh') or _a.get('appearance') or '')
                        if not _p:
                            return
                        # 与主路径 L2187/2194 **同序同参数**：顺序不一致会让缓存 key 漂移
                        if _atype == 'character':
                            _p = asset_prompt_kit.ensure_prompt_gender(_p, _a)
                        elif _atype == 'scene':
                            _p = asset_prompt_kit.ensure_scene_layout(_p, _a)
                        if _atype == 'item':
                            _imp = _a.get('importance', '')
                            if _imp and _imp != '重要':
                                _done['skip'] += 1
                                return
                        _style = str(_a.get('style') or _gstyle or '')
                        # 与 _prompt_preflight 一致：先叠加该项目历史召回
                        try:
                            _t = _pm.learned_prompt(kind='prompt', prompt=_p, project=_pname,
                                                    root_dir=PROJECT_OUTPUT_DIR, style=_style)
                        except Exception:  # noqa: BLE001  召回失败按原文
                            _t = _p
                        _pe.enhance_prompt('asset', _t, ctx=_a, style=_style)
                        _done['n'] += 1
                    except Exception:  # noqa: BLE001  单个失败不影响其余
                        pass

                with ThreadPoolExecutor(max_workers=3,
                                        thread_name_prefix='prewarm-asset') as _ex:
                    list(_ex.map(_one, _assets))
                logger.info('[资产提示词预热] %s 批：已预热 %d 条（跳过 %d 条），'
                                '正式生成时命中缓存', _atype, _done['n'], _done['skip'])
            except Exception as _pw_e:  # noqa: BLE001  预热整体失败不影响生产
                logger.warning('[资产提示词预热] 调度失败（忽略，不影响生产）：%s', _pw_e)

        # ⭐⭐ 2026-10-11 用户口径：「**本集预热要一直开启，不管什么情况**」。
        # ---------------------------------------------------------------------
        # 原实现有两条关闭通道：env MJSCXT_PREWARM_ASSET_PROMPT=0（调试用）
        # 与 plan.prewarm_asset_prompt=False（界面勾选）。按用户口径**两条都去掉** ——
        # 本集预热不存在"需要关掉"的场景：
        #   · 它只往 prompt_enhance 缓存里填结果，**不落产物、不写教训库、不碰 results**；
        #   · 它是**纯 LLM 调用**，不抢 GPU，与出图**并行**反而缩短本集总耗时；
        #   · 关掉它毫无收益，只会让正式出图时干等 LLM（批越大越明显）。
        # 因此这里恒为 True，不做任何条件判断 —— 保留变量是为了下面的 if 可读性。
        _pw_on = True
        if _pw_on and assets:
            try:
                threading.Thread(target=_prewarm_asset_prompts,
                                 args=(list(assets), asset_type, gen_style, project_name),
                                 daemon=True, name=f'prewarm-asset-{asset_type}').start()
                logger.info('[资产提示词预热] 已在后台预热 %s 批共 %d 个资产（与出图并行）',
                                asset_type, len(assets))
            except Exception as _pw_t_e:  # noqa: BLE001
                logger.warning('[资产提示词预热] 线程启动失败（忽略）：%s', _pw_t_e)
        # ===================== 预热结束 =====================

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
                        logger.info("资产已达标入库，断点续跑跳过：%s（%s）",
                                        name, _ready_img)
                        results.append({"name": name, "status": "skipped",
                                        "reason": "已达标入库，断点续跑跳过"})
                        continue

                # 物品过滤：只生成重要道具的参考图
                if asset_type == 'item':
                    importance = asset.get('importance', '')
                    if importance and importance != '重要':
                        logger.info(f"跳过临时道具 '{name}'（importance={importance}），不生成参考图")
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
                    logger.warning("资产「%s」参考图提示词预检未通过（%s）：%s",
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
                        logger.warning("[物品主人形象] 物品「%s」声明有人像，但未取到主人"
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
                            logger.info("资产 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
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
                                    logger.info("资产 %s 第 %d 次重试，按历史质检教训改写提示词：%s",
                                                    name, attempt + 1, suggestions[:2])
                                else:
                                    logger.info("资产 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                    name, attempt + 1)
                            except Exception as mem_err:
                                logger.warning(f"读取记忆模块失败: {mem_err}")
                    
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
                    # ⚠️ 2026-10-14 修复 UnboundLocalError（实测第2集 item 资产失败、整集中断）：
                    #    `_base_kw` 原先只在最后的 `else:`（character/scene）分支里定义，
                    #    而下方通用的分级/群像 QC 取值处写的是 `(_base_kw or {}).get(...)`。
                    #    作者以为 `or {}` 能兜住"未定义"，但 **UnboundLocalError 是编译期
                    #    判定的** —— 只要函数体里存在对该名字的赋值，其它路径上引用它就必崩。
                    #    item 资产走上面两个分支 → 崩在 QC 取值处 → 该资产永远生不出来
                    #    → probe 永远缺 1 个 → **整集在生产里反复失败并卡死**（实测停止在 ep2）。
                    #    修法：在三分支之前**统一初始化**，所有路径都安全。
                    _base_kw = {}
                    if asset_type == 'item' and _item_ref_img and _item_ref_fail < 2:
                        _ref_files = []
                        try:
                            _ref_files = comfyui_client.generate_item_base_with_ref(
                                prompt_zh, _item_ref_img, seed=seed, style=gen_style,
                                size=gen_size, filename_prefix=_gen_prefix,
                                surface_text=surface_text)
                        except Exception as _ref_err:  # noqa: BLE001 - 参考图链路失败不阻断出图
                            logger.warning(
                                "[物品主人形象] 物品「%s」参考图链路异常（连续第 %d 次）：%s: %s",
                                name, _item_ref_fail + 1, type(_ref_err).__name__, _ref_err)
                        if _ref_files:
                            base_files = _ref_files
                            _item_ref_fail = 0                 # 成功 → 连续失败清零
                        else:
                            _item_ref_fail += 1
                            logger.warning(
                                "[物品主人形象] 物品「%s」参考图链路未产出（连续第 %d/2 次），"
                                "本次立即回落纯 T2I", name, _item_ref_fail)
                            if _item_ref_fail >= 2:
                                _item_degraded = True
                                logger.warning(
                                    "[物品主人形象] 物品「%s」参考图链路连续失败 %d 次，"
                                    "自第 3 次起不再尝试参考图链路（纯 T2I）",
                                    name, _item_ref_fail)
                            # 立即回落，保证本 attempt 仍产出图片（参考图失败不得让资产判 failed）
                            base_files = comfyui_client.generate_item_base(
                                prompt_zh, seed=seed, style=gen_style, size=gen_size,
                                filename_prefix=_gen_prefix, surface_text=surface_text)
                    elif asset_type == 'item':
                        if _item_degraded:
                            logger.warning(
                                "[物品主人形象] 物品「%s」已降级为纯 T2I"
                                "（参考图链路连续失败 %d 次）", name, _item_ref_fail)
                        # 普通物品 / 未命中主人 / 已降级：纯 T2I（含 surface_text 确切文字硬约束）
                        base_files = comfyui_client.generate_item_base(
                            prompt_zh, seed=seed, style=gen_style, size=gen_size,
                            filename_prefix=_gen_prefix, surface_text=surface_text)
                    else:
                        # character / scene：保持既有字典派发调用签名不变
                        # ⭐ 2026-10-10 场景空间关联：场景带 refs 时传关联参考图，
                        #    让门框 / 地面 / 墙面材质与关联场景接上（见
                        #    ComfyUIClient.generate_scene_base 的 ref_images 说明）。
                        #    只取**已存在**的同组场景图；取不到就不传（回落纯 T2I，
                        #    与从前逐字一致）。参考图编辑失败由下层 fail-open 兜底。
                        _base_kw = {}
                        if asset_type == "scene":
                            _refs = _scene_relation_refs(asset, project_name)
                            if _refs:
                                _base_kw["ref_images"] = _refs
                        elif asset_type == "character":
                            # ⭐⭐ 2026-10-12 用户口径（群像分级出图）：
                            #   「角色是群像、后续剧情也与他无关时不用生成三视图，
                            #    直接生成一张多人图就行了」。
                            #   判定口径见 _character_tier（全部来自已落盘剧本事实）。
                            #   命中群像 → 出「多人合影」，并把本集**所有**群像角色名给进去，
                            #   保证画面里真的是剧本要的那几个人（而非模型自由发挥）。
                            _tier = _character_tier(project_name, asset)
                            if _tier == "crowd":
                                _crowd = _crowd_names_for(project_name)
                                if _crowd:
                                    _base_kw["crowd_names"] = _crowd
                                # 名单为空（读不到 bible/剧本）→ 不传，回落四区，不误伤
                            elif _tier in ("supporting", "minor"):
                                # 主要配角 → 双格正侧；次要角色 → 单张正面立绘。
                                # primary 与 unknown **不传 tier** —— 一律走四区设定集，
                                # 保证「读不到剧本时按旧行为」这条底线不被破坏。
                                _base_kw["tier"] = _tier
                        base_files = gen_base(prompt_zh, seed=seed, style=gen_style,
                                              size=gen_size, filename_prefix=_gen_prefix,
                                              **_base_kw)
                    if not base_files:
                        base_attempts.append({"attempt": attempt + 1, "seed": seed, "stage": "基础图生成",
                                              "ok": False, "error": "基础图生成失败"})
                        # B-16 P2-11：基础图生成失败 → 清理本资产产生的 scratch 中间产物
                        _cleanup_scratch_dir(scratch_dir, logger)
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
                        # ⭐ 2026-10-12 修「必然失败」矛盾（实测：筑基丹 6 次质检全部 45 分卡死，
                        #    整集被判 needs_human）。矛盾结构是：
                        #      · 生成守卫 _ensure_item_prompt() 强制追加「纯白背景，无任何场景、地面、桌面」；
                        #      · 本段判据说「出现绸布等承托物 → 关键缺陷」；
                        #      · 而通用判据又要求「与资产设定一致」，资产设定里常写「置于深色布面/玉盒」。
                        #    于是：画纯白 → 判「与设定不符」；画深色布 → 判「出现绸布承托物」。
                        #    两条路都是缺陷，模型怎么画都不合格，重试 3 次分数纹丝不动（45/45/45）。
                        # 修法：在判据里**显式声明背景口径**，把「资产设定中的承托物措辞」
                        #    从「背景比对」中排除，只保留「不得真出现承托物实体」这一条硬要求。
                        _qc_desc_base = (str(qc_desc) +
                                         "\n\n【物品图专项判据】物品参考图只应呈现**物品本体**。"
                                         "若画面主体是容器/托盘/盒子/底座/支架/展示台/碗碟/绸布等承托物，"
                                         "或物品被遮挡、被放进/放在别的物体内或上面、出现手或人物，"
                                         "判为关键缺陷（extra_prop / 承托物），kinds 记「物品变形」。"
                                         "纯白背景上出现明显投影或桌面/地面也计一条缺陷。"
                                         "注意：本判据优先于「是否与提示词一致」——"
                                         "提示词本身写了容器时，仍判缺陷。"
                                         "\n\n【背景口径（务必遵守，避免误判）】物品参考图是**资产素材**，"
                                         "其背景**一律为纯白、且不含任何承托物**，这是物品参考图的既定规范。"
                                         "因此：**资产设定/镜头描述里出现的「置于深色布面 / 放在玉盒中 / 摆在托盘上」等措辞，"
                                         "只表示该物品在剧情中的用途或出场方式，不作为本图的背景比对依据**。"
                                         "**严禁**因为「背景是纯白而非深色布面」而判「与设定不符」「场景与镜头信息不符」；"
                                         "反过来，若画面**真的画出了**布面/盒子/托盘等承托物实体，仍按上一段判关键缺陷。")
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
                    # ⚠️ 2026-10-14 新增（用户报「群像也只生成了一个人，而且群像没必要出三视图」）：
                    #    四档分级的版式是**硬约束**，不能只靠 prompt 里的「严禁」——
                    #    实测群像版式的 prompt 已写明「严禁把任何一个人画成单独的三视图」，
                    #    但成图仍可能画成单人立绘。这里把档位对应的形态要求写进质检判据，
                    #    不满足就驳回重画（与上面物品/颜色两条专项判据同一挂载方式）。
                    _tier_qc = str((_base_kw or {}).get("tier") or "").strip().lower()
                    _crowd_qc = list((_base_kw or {}).get("crowd_names") or [])
                    if _crowd_qc or _tier_qc == "crowd":
                        _qc_desc_base = (str(_qc_desc_base) +
                                         "\n\n【群像合影专项判据】本图是**多人合影**（群像角色的资产图）："
                                         "必须在一张画面内并列出现**多名不同人物**（至少 3 名），"
                                         "他们是同一群体，且处于同一光线、同一色调、同一画风下。"
                                         "若画面里**只有一个人物**，或把群体画成了**单人的三视图/**"
                                         "**四区设定表**（正/侧/背分格、或带面部特写+配色板+局部细节的版面），"
                                         "判为关键缺陷，kinds 记「群像人数不符」。")
                    elif _tier_qc == "minor":
                        _qc_desc_base = (str(_qc_desc_base) +
                                         "\n\n【单人立绘专项判据】本图是**次要角色的单张正面全身立绘**："
                                         "画面里只应有**一个**人物，正面朝向镜头、全身完整入画。"
                                         "若出现多个人物、或画成了三视图/四区设定表（正/侧/背分格、"
                                         "或带面部特写+配色板+局部细节的版面），"
                                         "判为关键缺陷，kinds 记「版式不符」。")
                    verdict = qc_client.check_image(scratch_base, _qc_desc_base, qc_cfg, style=gen_style)
                    logger.info(f"[资产质检] base {asset_type}/{name} 第{attempt + 1}次 → "
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
                        logger.warning(
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
                        logger.warning(f"资产不合格基础图清理失败（忽略）：{_pe}")

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
                    logger.info("角色「%s」不裁剪：保留整图 %s（不再切分单视角）",
                                    name, os.path.basename(base_dst))
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(), known=ASSET_VIEW_STEMS, logger=logger)
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                elif (asset_type == "scene" and SCENE_VIEWS_ENABLED
                      and SCENE_GRID_MODE and SCENE_GRID_ONESHOT
                      # ⭐ 2026-10-10 用户要求：按剧情判断是否**需要**多视角九宫格 ——
                      #   只出现 1 镜的背景场景不生成（单图足够），反复出现的才生成。
                      #   读不到剧本时返回 True（不拦），保持旧行为。
                      and _scene_needs_multiview(project_name, name)):
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
                            logger.warning(
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
                        logger.info(
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
                        logger.info(
                            "[场景九宫格] 场景「%s」单次出图完成（1.5MP 直出整图）：%s",
                            name, _grid_dst)
                    else:
                        # 加值项不阻断资产：整图未达标时不落盘、不判 failed，分镜回落 base.png。
                        _oneshot_gate = _oneshot_gate or {}
                        if not _oneshot_scratch:
                            logger.warning(
                                "场景「%s」九宫格整图单次出图失败（未产出图片）→ 不落盘、"
                                "不判 failed，分镜将回落 base.png", name)
                            view_gate[SCENE_GRID_FILENAME] = dict(
                                _oneshot_gate, dropped=True, drop_reason="出图失败（未产出图片）")
                        else:
                            logger.warning(
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
                            logger=logger)
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                elif (asset_type == "scene" and SCENE_VIEWS_ENABLED
                      # ⭐ 2026-10-11 补漏：这条「逐档独立出图 + 拼接」路径**同样**要过
                      #   「按剧情判断」门槛。此前只改了上面 L924 的「单次出图」分支，
                      #   判定拦下后控制流落到这里，于是只出现 2 镜的「老楼楼梯间」
                      #   照样花 16 分钟逐档出 9 张再拼接 —— 白烧算力，且九宫格必然
                      #   偏稀（部分档不达标被丢弃，7/9 格）。两条路径必须同口径。
                      and _scene_needs_multiview(project_name, name)):
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
                                logger.warning("场景「%s」机位档 %s 出图失败（seed=%s）",
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
                            logger.info(
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
                                logger.warning(
                                    "场景「%s」机位档「%s」相似度粗筛失败（不拦截，按达标处理）：%s",
                                    name, _v_label, _vse)
                        _v_dup = bool((not _grid) and _v_sim is not None
                                      and _v_sim > SCENE_VIEW_DUP_PHASH_MAX)
                        if (_grid and _v_ok and _v_sim is not None
                                and _v_sim > SCENE_VIEW_DUP_PHASH_MAX):
                            logger.warning(
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
                                logger.warning(
                                    "场景「%s」机位档「%s」与 base 相似度 %.1f > %.1f"
                                    "（明显撞车 / 未换构图）→ 丢弃该档（不落盘、不判 failed，"
                                    "分镜将回落正面档）",
                                    name, _v_label, _v_sim, SCENE_VIEW_DUP_PHASH_MAX)
                                view_gate[vk] = dict(
                                    _v_gate, dropped=True, dup_similarity=round(_v_sim, 1),
                                    drop_reason=(f"与base相似度{_v_sim:.1f}"
                                                 f">阈值{SCENE_VIEW_DUP_PHASH_MAX:g}"))
                            elif not _v_scratch:
                                logger.warning(
                                    "场景「%s」机位档「%s」出图失败（未产出图片）→ 丢弃该档"
                                    "（不落盘、不判 failed，分镜将回落正面档）", name, _v_label)
                                view_gate[vk] = dict(_v_gate, dropped=True,
                                                     drop_reason="出图失败（未产出图片）")
                            elif _v_fault:
                                logger.warning(
                                    "场景「%s」机位档「%s」因**质检接口故障**重试 %d 次仍未"
                                    "取得判定 → 丢弃该档（不落盘、不判 failed，"
                                    "分镜将回落正面档）：%s",
                                    name, _v_label, SCENE_VIEW_MAX_RETRIES + 1,
                                    _v_gate.get("reason") or "质检接口不可用")
                                view_gate[vk] = dict(
                                    _v_gate, dropped=True,
                                    drop_reason="质检接口故障·重试后仍未取得判定")
                            else:
                                logger.warning(
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
                            logger.warning(
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
                                    logger.info(
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
                                    logger.info(
                                        "[场景九宫格] 场景「%s」分档原料图已清理（仅留 base+grid）", name)
                            except Exception as _ge:  # noqa: BLE001
                                logger.warning(
                                    "场景「%s」九宫格拼接失败（保留各机位档，不阻断入库）：%s",
                                    name, _ge)
                        else:
                            logger.warning(
                                "场景「%s」九宫格可用的机位档不足 2（实得 %d），跳过拼接，"
                                "下游回落 base.png", name, _grid_have)
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=tuple(view_paths.keys()),
                            known=_known_stems,
                            logger=logger)
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                else:
                    # 物品：基础图即单主体图；清掉旧实现遗留的视角图，避免 UI 把陈旧的
                    # 「重渲染整图」继续当成一个视角展示。
                    # （场景在关闭 SCENE_VIEWS_ENABLED 时也走这里 → 与改造前逐字一致）
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(),
                            known=tuple(ASSET_VIEW_STEMS) + tuple(SCENE_VIEW_KEYS),
                            logger=logger)
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)

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
                # T3（2026-10-12）：物品资产入库即**钉住固定参考图** —— 把 base.png 的文件号
                # 与 sha256 写进 base.meta.json 的 ref_* 字段（只写元数据、不重生成任何图）。
                # 分镜侧 resolve_item_reference 据此在任何镜头都取到同一张图。
                if str(asset_type) == "item":
                    try:
                        _pin = pin_item_reference(project_name, name)
                        if _pin.get("sha256"):
                            _asset_result["fixed_ref"] = {
                                "file": _pin.get("file"),
                                "sha256": _pin.get("sha256"),
                                "identity": item_ref_identity(
                                    os.path.join(asset_dir, str(_pin.get("file") or "")), project_name),
                                "changed": bool(_pin.get("changed")),
                            }
                        elif _pin.get("error"):
                            _asset_result["fixed_ref"] = {"error": _pin["error"]}
                    except Exception as _pin_e:  # noqa: BLE001 —— 钉住失败不影响入库
                        logger.warning("[道具固定参考图] %s/%s 钉住失败（不影响入库）：%s: %s",
                                       project_name, name, type(_pin_e).__name__, _pin_e)
                results.append(_asset_result)

            except Exception as _asset_err:  # noqa: BLE001
                # ⚠️ 审计 S11：旧代码这里没有 try —— `generate_multiview` 上传基础图失败会
                #    raise RuntimeError，`queue_prompt` 遇 5xx/超时也会抛。任一处抛出 →
                #    整个批次被标 failed，`results`（已成功资产的结果）全部丢弃：
                #    20 个资产在第 7 个时来一次连接抖动，前 6 个已入库的成果用户也看不见。
                logger.error("资产「%s」生成失败（已隔离，继续后续资产）：%s: %s",
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
        # ⭐ 2026-10-10 角色换装出图（用户要求）：主设定图这一批已完成 → 触发本批
        #    角色的换装变体出图（剧本阶段已登记 outfit.json，这里把图补齐）。
        #    只对**主设定**触发（sub_dir 为空），避免变体再触发变体造成递归。
        #    fail-open：触发失败只记日志，主资产任务已经完成，不受影响。
        if asset_type == "character" and not sub_dir:
            try:
                _spawn_outfit_variant_jobs(
                    [str(x.get("name") or "").strip() for x in (assets or [])
                     if isinstance(x, dict)])
            except Exception as _ole:  # noqa: BLE001
                logger.warning("[服装变体] 触发换装出图失败（不影响主资产）：%s: %s",
                               type(_ole).__name__, _ole)
    except Exception as e:
        logger.error(f"资产生成失败: {e}")
        _partial = locals().get("results") or []   # 审计 S11：已成功的部分结果不能丢
        with lock:
            generation_state[task_id].update({
                "status": "failed", "error": str(e), "results": _partial,
                "success_count": sum(1 for r in _partial if r.get("success")),
            })
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    _maybe_clear_comfyui_history("资产批量生成收尾")


# ===================== 道具固定参考图复用（T3，2026-10-12） =====================
#
# 背景（用户 2026-10-12 成片质检报告）：「文字类道具（员工卡 / 楼层安全守则）在不同镜头里
# 长得不一样」——同一个道具在剧本里反复出现，分镜侧却各镜各取一张图。
#
# 实测证据（2026-10-12，进境_整本小说 第1集真实产物）：
#   · storyboard_manifest.json（60 镜）里，23 个带道具参考图的镜头，refs["道具"] 的取值
#     **全是 "ref_canvas"** —— 那是「画幅统一」产物的目录名，既看不出用的是哪张图，
#     也无法从产物反推「第 12 镜与第 26 镜用的是不是同一张员工卡」。
#   · output/qc/<项目>/ref_canvas/ 下恰好 7 张 base_<sha1前8>_1664x928.png，按 sha1
#     反查源码路径：4 张角色 base.png + 3 张道具 base.png（天合科技员工卡 / 楼层安全守则 /
#     贴27号的无标签水瓶）——**每个道具只有一份统一产物**，说明这一批没有串图；
#     但「没有串图」这件事此前无法从产物自证，只能靠反查哈希。
#
# 本节的职责（写侧 / 解算侧，与 storyboard_helpers 的读侧配合）：
#   ① item_ref_identity：把资产路径归一成**稳定的身份串** items/<项目>/<道具>/base.png；
#   ② pin_item_reference：为道具钉住一张固定参考图（默认 base.png），把文件号 + sha256 +
#      字节数写进 base.meta.json 的 ref_* 字段。**只写元数据，不重生成任何图**（非目标）；
#      幂等：同一张图重复 pin，内容一字不变（changed=False）。
#   ③ resolve_item_reference：按「目录名精确 → 别名/近名 → 目录内首图」三级解析，
#      保证**同一个道具名在任何镜头都解析到同一个文件**（脚本写「员工证」而资产目录叫
#      「员工卡」这类情况也能落到同一张图）。
#   ④ item_reference_report：一次汇总全项目的道具固定参考图（给日志/质检/排查用）。
#
# 为什么钉「文件」而不是钉「提示词」：分镜侧要的是**同一张输入图**（对齐参考图链路），
# 提示词再一致也压不住两张不同的参考图带来的外观漂移。

#: 写进道具 meta 的固定参考图字段（前缀 ref_ 统一，便于一次读取/一次核对）
ITEM_REF_FIELDS = ("ref_file", "ref_sha256", "ref_bytes", "ref_pinned_at", "ref_project")

#: 默认固定参考图文件名（与资产生成产出一致；改这里等于改全项目的道具锚点口径）
ITEM_REF_DEFAULT_FILE = "base.png"

#: 道具 meta 旁路文件的两种历史命名（新写入统一用 base.meta.json —— 与
#: storyboard_helpers._write_artifact_meta 的 splitext(artifact)[0] + ".meta.json" 同口径）
ITEM_REF_META_NAMES = ("base.meta.json", "base.png.meta.json")


def item_asset_dir(project_name: str, item_name: str) -> str:
    """道具资产目录绝对路径：<ITEMS_DIR>/<项目>/<道具>（不做存在性判断）。"""
    return os.path.join(ITEMS_DIR, str(project_name or "").strip(), str(item_name or "").strip())


def item_ref_identity(path, project_name: str = "") -> str:
    """把任意资产路径归一成**稳定的资产身份串**：<kind>/<项目>/<名称>[/<文件>]。

    - 只认三类资产根（items / characters / scenes）；不在资产根下 → 返回空串
      （调用方自行回落旧口径，如「目录名」）。
    - project_name 给定时只接受该项目下的路径（防跨项目串图）。
    - 身份串**与绝对路径无关**：换机器、换输出根（桌面版 vs 便携版）都是同一个身份 ——
      这正是「同一道具在跨镜头分镜中引用的参考图路径稳定一致」可比对的前提。
    """
    p = os.path.abspath(str(path or ""))
    if not p:
        return ""
    want = str(project_name or "").strip()
    for kind, root in (("items", ITEMS_DIR), ("characters", CHARACTERS_DIR), ("scenes", SCENES_DIR)):
        try:
            root_abs = os.path.abspath(root)
            if os.path.normcase(p) == os.path.normcase(root_abs):
                continue
            rel = os.path.relpath(p, root_abs)
        except (TypeError, ValueError):
            continue
        if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
            continue
        parts = [x for x in rel.replace("\\", "/").split("/") if x]
        if not parts:
            continue
        if want and parts[0] != want:
            continue
        return "/".join([kind] + parts)
    return ""


def _item_meta_path(asset_dir: str) -> str:
    """道具旁路元数据路径：优先已存在的历史命名，否则用 base.meta.json（新写入口径）。"""
    for name in ITEM_REF_META_NAMES:
        p = os.path.join(asset_dir, name)
        if os.path.isfile(p):
            return p
    return os.path.join(asset_dir, ITEM_REF_META_NAMES[0])


def read_item_meta(asset_dir: str) -> dict:
    """读道具旁路元数据（fail-open：缺失/损坏一律返回 {}，绝不抛）。"""
    try:
        p = _item_meta_path(asset_dir)
        if not os.path.isfile(p):
            return {}
        import json as _json_meta
        with open(p, encoding="utf-8") as f:
            data = _json_meta.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("道具元数据读取失败（按无元数据处理）：%s: %s", type(e).__name__, e)
        return {}


# ===================== 项目级道具别名表（t16，2026-10-12） =====================
#
# 【为什么是别名表，而不是放宽近名匹配】
#   t3 定的原则是「找不到就报错，绝不猜」——猜错＝给道具挂错图，比不挂更糟。实测里
#   剧本把同一个物件写成两种名字（进境_整本小说 第1集.json：员工证 26 次 / 员工卡 38 次），
#   而资产目录只有一个名（天合科技员工卡）。这不是「近名」，是**同名异写**：
#   卡片正面印的字就是「员工证」（items[].surface_text），叙述与资产目录却叫「员工卡」。
#   这类差异必须由人**显式登记**（含来源与理由），不能交给相似度算法自动判定 ——
#   所以这里落地成配置表，算法侧一字不改（仍然「查不到就返回空串」）。
#
# 【登记规则（改动本表前必读）】
#   ① 只在**有剧本/原文证据**证明是同一物件时登记（见每条 source）；
#   ② target 必须是**已存在的资产目录名** —— 表里有、目录没有，仍然报错（不猜）；
#   ③ 宁可不登记：未登记的别名走原有近名匹配，匹配不上就报错（见 test 用例）；
#   ④ 项目级：不同项目的同名道具互不干扰（表按项目分包）。

#: 内嵌别名表（打包 exe 的 datas 不含 app/*.json，故代码内必须有一份；与文件覆盖合并）
#: 结构：{项目: {别名: {"target", "source", "reason"}}}
ITEM_ALIAS_TABLE = {
    "进境_整本小说": {
        "员工证": {
            "target": "天合科技员工卡",
            "source": "output/scripts/进境_整本小说/第1集.json：全文 26 处；items[0].surface_text="
                      "「天合科技 姓名 羡进 员工证，请勿转借」（卡片正面印的字就是「员工证」）",
            "reason": "同一物件的两种写法：卡面印刷文字用「员工证」，叙述与资产目录名用「员工卡」。"
                      "用户反馈「工牌前后不一致」的机制性根源之一。",
        },
        "天合科技员工证": {
            "target": "天合科技员工卡",
            "source": "同上（带公司前缀的写法；资产目录名 = 天合科技员工卡）",
            "reason": "带前缀的同一物件写法，与资产目录名仅差「证/卡」一字。",
        },
        "员工卡": {
            "target": "天合科技员工卡",
            "source": "output/scripts/进境_整本小说/第1集.json：全文 38 处（叙述口径）",
            "reason": "叙述主用名。此前靠 asset_name_match 近名匹配命中，改为显式登记（不再依赖相似度）。",
        },
        "蓝色员工卡": {
            "target": "天合科技员工卡",
            "source": "output/scripts/进境_整本小说/第1集.json shot#56 description「蓝色员工卡正面向外」，"
                      "该镜 items_in_shot=['天合科技员工卡']",
            "reason": "带颜色限定词的同一物件；剧本里该镜已把它挂在天合科技员工卡槽位。",
        },
        "工牌": {
            "target": "天合科技员工卡",
            "source": "output/scripts/进境_整本小说/第1集.json：全文 57 处；shot#55 items_in_shot="
                      "['天合科技员工卡'] 而 description 写「她左胸工牌已由蓝色变为红色」；"
                      "t1 canon 已判定「员工卡」与「工牌」同义（不作冲突）",
            "reason": "剧本把「工牌」与「天合科技员工卡」挂在同一道具槽位（shot#55/56/57），"
                      "登记为同一物件的别名。",
        },
        "守则纸": {
            "target": "楼层安全守则",
            "source": "output/scripts/进境_整本小说/第1集.json shot#27 description「羡进的手把守则纸翻面」，"
                      "该镜 items_in_shot=['楼层安全守则']",
            "reason": "剧本叙述里把守则称作「守则纸」，与资产目录名「楼层安全守则」同一物件。",
        },
        "守则": {
            "target": "楼层安全守则",
            "source": "同上（视觉锚点里大量写作「守则第N次」）",
            "reason": "简称，与资产目录名同一物件。",
        },
    },
}

#: 别名表**不登记**的词（含不登记的理由）—— 与上面的规则③呼应：宁可不登记也不误挂。
ITEM_ALIAS_REJECTS = {
    "红色工牌": "指**红工牌女人左胸**那张牌（另一角色持有的物件，且颜色不同），"
                "与羡进的蓝色员工卡不是同一份资产；挂同一张图会造成「工牌前后不一致」。"
                "剧本 shot#55 虽把它写进同一槽位，但那是剧本层的归槽，资产层不据此建别名。",
    "红工牌": "同上。",
    "红底工牌": "同上。",
}

#: 项目级别名覆盖文件（可选）：<ITEMS_DIR>/<项目>/item_aliases.json
#: 形如 {"别名": {"target": "...", "source": "...", "reason": "..."}}
#: 存在则与内嵌表合并（**文件优先**，便于不改代码补登记）；缺失/损坏一律 fail-open。
ITEM_ALIAS_FILE_NAME = "item_aliases.json"


def _item_alias_file(project_name: str) -> str:
    return os.path.join(ITEMS_DIR, str(project_name or "").strip(), ITEM_ALIAS_FILE_NAME)


def load_item_aliases(project_name: str) -> dict:
    """项目道具别名表（内嵌 + 项目文件覆盖，文件优先；fail-open）。"""
    project = str(project_name or "").strip()
    merged = {}
    for alias, spec in (ITEM_ALIAS_TABLE.get(project) or {}).items():
        if isinstance(spec, dict) and spec.get("target"):
            merged[str(alias)] = dict(spec)
    p = _item_alias_file(project)
    if project and os.path.isfile(p):
        try:
            import json as _json_alias
            with open(p, encoding="utf-8") as f:
                data = _json_alias.load(f)
            for alias, spec in (data or {}).items():
                if isinstance(spec, dict) and spec.get("target"):
                    merged[str(alias)] = dict(spec)
                elif isinstance(spec, str) and spec.strip():
                    merged[str(alias)] = {"target": spec.strip(), "source": p,
                                          "reason": "项目文件登记（简写形式）"}
        except Exception as e:  # noqa: BLE001 —— 覆盖文件坏了不该让解析失败
            logger.warning("道具别名覆盖文件读取失败（只用内嵌表）：%s: %s", type(e).__name__, e)
    return merged


def resolve_item_alias(project_name: str, item_name: str) -> dict:
    """查**显式登记**的道具别名（只查表，不算相似度）。

    返回 {"alias", "target", "source", "reason", "exists"}；未登记 → target=""。
    exists 表示该 target 目录是否真的存在于本项目资产里（不存在时不算命中，仍报错不猜）。
    """
    project = str(project_name or "").strip()
    want = str(item_name or "").strip()
    out = {"alias": want, "target": "", "source": "", "reason": "", "exists": False}
    if not project or not want:
        return out
    spec = load_item_aliases(project).get(want)
    if not spec:
        return out
    target = str(spec.get("target") or "").strip()
    if not target:
        return out
    out.update({"target": target, "source": str(spec.get("source") or ""),
                "reason": str(spec.get("reason") or "")})
    out["exists"] = os.path.isdir(os.path.join(ITEMS_DIR, project, target))
    return out


def item_alias_report(project_name: str) -> dict:
    """项目道具别名总览（只读；给日志 / 质检 / 排查用）。"""
    project = str(project_name or "").strip()
    table = load_item_aliases(project)
    rows = []
    for alias in sorted(table):
        spec = table[alias]
        target = str(spec.get("target") or "")
        rows.append({"alias": alias, "target": target,
                     "exists": os.path.isdir(os.path.join(ITEMS_DIR, project, target)) if target else False,
                     "source": str(spec.get("source") or ""), "reason": str(spec.get("reason") or "")})
    return {"project": project, "aliases": rows,
            "rejects": dict(ITEM_ALIAS_REJECTS), "file": _item_alias_file(project)}


def match_item_asset_name(project_name: str, item_name: str) -> str:
    """把剧本里的道具名解析成**资产目录名**（精确 → 显式别名表 → 归一化近名 → 空串）。

    例：脚本写「员工证」而资产目录叫「天合科技员工卡」时，显式别名表（t16）命中；
    别名表也没登记、近名也匹配不上时返回空串，由调用方决定回落策略（绝不猜一个不存在的目录）。
    """
    base = os.path.join(ITEMS_DIR, str(project_name or "").strip())
    want = str(item_name or "").strip()
    if not want or not os.path.isdir(base):
        return ""
    try:
        names = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return ""
    for d in names:
        if d == want:
            return d
    # 显式别名表（t16）：登记了且 target 目录真实存在才算命中 —— 登记了但资产没生成时，
    # 仍然走「找不到」（不猜、不回落到一个不存在的目录）。
    alias = resolve_item_alias(str(project_name or "").strip(), want)
    if alias.get("target") and alias.get("exists"):
        logger.info("[道具别名] %s：%s → %s（显式登记）", project_name, want, alias["target"])
        return alias["target"]
    if alias.get("target") and not alias.get("exists"):
        logger.warning("[道具别名] %s：别名 %s → %s 已登记，但资产目录不存在（按找不到处理）",
                       project_name, want, alias["target"])
    try:
        index = {asset_name_match.normalize(d): d for d in names}
        hit, _score = asset_name_match.match(want, index, kind="item")
        if hit:
            return index.get(asset_name_match.normalize(hit)) or hit
    except Exception as e:  # noqa: BLE001 —— 近名匹配失败不阻断，回落「找不到」
        logger.warning("道具近名匹配失败（%s vs %s）：%s: %s", project_name, want, type(e).__name__, e)
    return ""


def resolve_item_reference(project_name: str, item_name: str) -> dict:
    """解析道具的**固定参考图**（读侧，任何镜头都用这一个入口）。

    返回 {"requested", "name", "dir", "path", "identity", "file", "pinned", "source", "error"}：
      · source="meta"  → 用了 meta 里 pin 住的 ref_file（换过图也仍指向同一文件）；
      · source="base"  → 无 pin，回落到约定的 base.png；
      · source="first" → 连 base.png 都没有，回落「目录内首图」（_ASSET_IMG_PRIORITY 顺序）；
      · path=""  → 该项目里找不到这个道具（调用方按「无参考图」处理，不要自己编路径）。
    """
    project = str(project_name or "").strip()
    want = str(item_name or "").strip()
    out = {"requested": want, "name": "", "dir": "", "path": "", "identity": "",
           "file": "", "pinned": False, "source": "", "error": "", "via_alias": "",
           "alias_source": "", "alias_reason": ""}
    if not project or not want:
        out["error"] = "项目名或道具名为空"
        return out
    base = os.path.join(ITEMS_DIR, project)
    if not os.path.isdir(base):
        out["error"] = "项目道具目录不存在"
        return out
    # 别名命中留痕（验收可核对：请求名 ≠ 资产目录名时 via_alias 非空）
    _alias = resolve_item_alias(project, want)
    if _alias.get("target") and _alias.get("exists"):
        out.update({"via_alias": _alias["target"], "alias_source": _alias.get("source") or "",
                    "alias_reason": _alias.get("reason") or ""})
    name = match_item_asset_name(project, want)
    if not name:
        out["error"] = "道具在项目资产里找不到（不做模糊猜测）"
        return out
    asset_dir = os.path.join(base, name)
    out["name"] = name
    out["dir"] = os.path.abspath(asset_dir)
    meta = read_item_meta(asset_dir)
    ref_file = str(meta.get("ref_file") or "").strip()
    if ref_file:
        p = os.path.join(asset_dir, ref_file)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            out.update({"path": os.path.abspath(p), "file": ref_file, "pinned": True, "source": "meta"})
            out["identity"] = item_ref_identity(out["path"], project)
            return out
        out["error"] = "meta 里 pin 的参考图不存在（回落 base.png）"
    p = os.path.join(asset_dir, ITEM_REF_DEFAULT_FILE)
    if os.path.isfile(p) and os.path.getsize(p) > 0:
        out.update({"path": os.path.abspath(p), "file": ITEM_REF_DEFAULT_FILE, "source": "base"})
        out["identity"] = item_ref_identity(out["path"], project)
        return out
    first = ""
    try:
        first = _first_existing_asset_image(asset_dir) or ""
    except Exception as e:  # noqa: BLE001
        out["error"] = "目录内首图解析失败：%s: %s" % (type(e).__name__, e)
    if first:
        out.update({"path": os.path.abspath(first), "file": os.path.basename(first), "source": "first"})
        out["identity"] = item_ref_identity(out["path"], project)
        return out
    if not out["error"]:
        out["error"] = "道具目录内没有可用参考图"
    return out


def _now_stamp() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def pin_item_reference(project_name: str, item_name: str, image_path: str = None) -> dict:
    """把某个道具的参考图**钉住**（幂等；只写元数据，不生成/不修改任何图）。

    image_path 为空时钉 base.png（即资产产出的那张）；显式传路径时钉该文件 —— 调用方
    可用它把「历史上被替换掉的那张图」重新钉回去。
    """
    out = {"project": str(project_name or "").strip(), "item": str(item_name or "").strip(),
           "file": "", "sha256": "", "bytes": 0, "meta": "", "changed": False, "pinned": False,
           "error": ""}
    resolved = resolve_item_reference(project_name, item_name)
    if not resolved.get("path") and not image_path:
        out["error"] = resolved.get("error") or "道具参考图不存在"
        return out
    asset_dir = resolved.get("dir") or item_asset_dir(project_name, resolved.get("name") or item_name)
    path = os.path.abspath(str(image_path or resolved.get("path") or ""))
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        out["error"] = "参考图文件不可用（缺失或空文件）"
        return out
    try:
        import hashlib
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
    except OSError as e:
        out["error"] = "参考图哈希失败：%s: %s" % (type(e).__name__, e)
        return out
    meta_path = _item_meta_path(asset_dir)
    meta = read_item_meta(asset_dir)
    wanted = {"ref_file": os.path.basename(path), "ref_sha256": sha,
              "ref_bytes": int(os.path.getsize(path)), "ref_project": out["project"]}
    changed = any(str(meta.get(k) or "") != str(v) for k, v in wanted.items())
    out.update({"file": wanted["ref_file"], "sha256": sha, "bytes": wanted["ref_bytes"],
                "meta": os.path.abspath(meta_path), "pinned": True})
    if not changed:
        return out
    meta.update(wanted)
    meta["ref_pinned_at"] = _now_stamp()
    try:
        from fs_atomic import atomic_write_json
        atomic_write_json(meta_path, meta)
    except Exception as e:  # noqa: BLE001 —— 原子写失败时回落普通写，仍失败则只报错
        try:
            import json as _json_pin
            os.makedirs(asset_dir, exist_ok=True)
            with open(meta_path, "w", encoding="utf-8") as f:
                _json_pin.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e2:  # noqa: BLE001
            out["pinned"] = False
            out["error"] = "元数据写入失败：%s: %s / %s: %s" % (type(e).__name__, e, type(e2).__name__, e2)
            return out
    out["changed"] = True
    logger.info("[道具固定参考图] %s/%s 已钉住 %s（sha256 %s…，%d bytes）",
                out["project"], resolved.get("name") or out["item"], out["file"], sha[:12], out["bytes"])
    return out


def pin_project_item_references(project_name: str) -> dict:
    """把整个项目的道具参考图钉一遍（幂等；已有 pin 且哈希一致时零写入）。"""
    project = str(project_name or "").strip()
    base = os.path.join(ITEMS_DIR, project)
    out = {"project": project, "total": 0, "pinned": 0, "changed": 0, "items": [], "error": ""}
    if not os.path.isdir(base):
        out["error"] = "项目道具目录不存在"
        return out
    try:
        names = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except OSError as e:
        out["error"] = "目录列举失败：%s: %s" % (type(e).__name__, e)
        return out
    for name in names:
        info = pin_item_reference(project, name)
        out["total"] += 1
        out["items"].append({"item": name, "file": info.get("file"), "sha256": info.get("sha256"),
                             "changed": bool(info.get("changed")), "error": info.get("error") or ""})
        if info.get("pinned"):
            out["pinned"] += 1
        if info.get("changed"):
            out["changed"] += 1
    logger.info("[道具固定参考图] 项目 %s：%d 个道具，%d 个已钉住（本次写入 %d 个）",
                project, out["total"], out["pinned"], out["changed"])
    return out


def item_reference_report(project_name: str) -> dict:
    """项目道具固定参考图总览（只读；给日志 / 质检 / 排查用）。"""
    project = str(project_name or "").strip()
    base = os.path.join(ITEMS_DIR, project)
    out = {"project": project, "items": [], "error": ""}
    if not os.path.isdir(base):
        out["error"] = "项目道具目录不存在"
        return out
    try:
        names = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except OSError as e:
        out["error"] = "目录列举失败：%s: %s" % (type(e).__name__, e)
        return out
    for name in names:
        r = resolve_item_reference(project, name)
        out["items"].append({"item": r.get("name") or name, "file": r.get("file"),
                             "identity": r.get("identity"), "source": r.get("source"),
                             "pinned": r.get("pinned"), "path": r.get("path"),
                             "error": r.get("error") or ""})
    return out
