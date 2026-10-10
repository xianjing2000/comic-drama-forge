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
from routes._shared import (COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _apply_project_settings, _comfy_view_url, _project_style, _prune_task_registry, _qc_gate, _qc_record, _qc_record_verdict, _serve_safe, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks)  # noqa: F401  该域助手已下沉到共享模块
from routes._shared import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir, _qc_load_cfg  # noqa: F401  再导出
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
from routes._shared import (_first_existing, _shot_num_key, comfyui_client)  # noqa: F401
import consistency
from job_state import (generation_state, lock)  # noqa: F401
import os
import prompt_memory
import qc_client
import random
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

        # 开关（与 prewarm_next_script 同口径：env 可强制关，其次读配置）
        _pw_on = True
        try:
            if str(os.environ.get('MJSCXT_PREWARM_ASSET_PROMPT') or '1').strip().lower() \
                    in ('0', 'false', 'off', 'no'):
                _pw_on = False
            else:
                _pw_cfg = None
                try:
                    _pw_cfg = (ctx or {}).get('config') if isinstance(ctx, dict) else None
                except Exception:  # noqa: BLE001
                    _pw_cfg = None
                if isinstance(_pw_cfg, dict) and _pw_cfg.get('prewarm_asset_prompt') is False:
                    _pw_on = False
        except Exception:  # noqa: BLE001
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
                        base_files = gen_base(prompt_zh, seed=seed, style=gen_style,
                                              size=gen_size, filename_prefix=_gen_prefix)
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
                        _qc_desc_base = (str(qc_desc) +
                                         "\n\n【物品图专项判据】物品参考图只应呈现**物品本体**。"
                                         "若画面主体是容器/托盘/盒子/底座/支架/展示台/碗碟/绸布等承托物，"
                                         "或物品被遮挡、被放进/放在别的物体内或上面、出现手或人物，"
                                         "判为关键缺陷（extra_prop / 承托物），kinds 记「物品变形」。"
                                         "纯白背景上出现明显投影或桌面/地面也计一条缺陷。"
                                         "注意：本判据优先于「是否与提示词一致」——"
                                         "提示词本身写了容器时，仍判缺陷。")
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
                      and SCENE_GRID_MODE and SCENE_GRID_ONESHOT):
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
                elif asset_type == "scene" and SCENE_VIEWS_ENABLED:
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

