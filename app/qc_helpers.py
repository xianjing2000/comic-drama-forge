# -*- coding: utf-8 -*-
'''质检辅助函数（2026-10-11 从 app.py 下沉，助手域第五批）。'''

# 本批 = 全部 _qc_* 助手及其依赖闭包。_qc_* 被分镜/视频/关键帧多处复用，
# 是 _storyboard_worker 与 _video_generate_worker_body 的共同前置。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
# import 由脚本从 app.py 原始语句原样复制（不猜来源）。
import logging

from asset_refs import (  # noqa: F401, E402  助手按域下沉（第四批）
                        _ASSET_IMG_EXTS, _ASSET_IMG_PRIORITY, _STATIC_DIR, _build_asset_index,
                        _first_existing_asset_image, _framing_wants_half_shot,
                        _h3_audio_defs_for, _h3_audio_policy, _h3_common_ref_audios,
                        _h3_common_subject_lock, _h3_is_common_comp, _h3_plan_common_refs,
                        _h3_shot_ref_components, _match_scene_name, _match_shot_chars,
                        _norm_shot_key, _normalize_char_alias, _note_ref_warning,
                        _pick_char_view, _pick_scene_view, _resolve_item_names,
                        _resolve_scene_entry, _resolve_static_dir, _scene_view_for_shot)
from comfyui_client import (ComfyUIClient, camera_spec as _camera_spec,
                            camera_key as _camera_key, camera_angle as _camera_angle,
                            BLOCKING_REF_MARK as _BLOCKING_REF_MARK,
                            IDENTITY_GRID_REF_MARK as _IDENTITY_GRID_REF_MARK)
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
from routes._shared import _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _AUDIO_QC_NON_PROJECT_DIRS, _audio_qc_project_key, _ep_dir, _ep_read_dir, _qc_load_cfg  # noqa: F401  再导出
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
import asset_name_match
import autopilot
import novel_to_script
import os
import project_store
import qc_client
import re
import style_kit

logger = logging.getLogger(__name__)

_OUTFITS_DIRNAME = "outfits"
CLOSEUP_CHAR_CROP_TOP = (0.32, 0.02, 0.68, 0.28)   # 头部条带（x0, y0, x1, y1）


def _sanitize_outfit_key(raw) -> str:
    """outfit_key 安全化：剔除路径非法字符 ``\\ / : * ? " < > |`` 与首尾空白、
    截断到 40 字符，防路径穿越。

    剔完为空（或只剩 ``.`` / ``..`` —— 分别是目录自身与上级，同样算穿越）返回
    ''，由调用方按 400 拒绝。刻意**不用** _safe_project：那是项目键收敛规则，
    会把任意输入坍缩成合法键（恒非空），而 outfit_key 必须能被 400 明确拒绝。
    """
    s = str(raw or "").strip()
    for _ch in '\\/:*?"<>|':
        s = s.replace(_ch, "")
    s = s.strip()
    if not s or s in (".", ".."):
        return ""
    return s[:40]


def _normalize_scene_name(name) -> str:
    """归一化场景名（只做**标点 / 空白 / 全半角**层面，不做语义改写）。

    刻意**不剥**「（夜）」「外」「门口」这类限定词：它们是场景区分的一部分，
    剥了会把「卧室」和「卧室外」混成一个。此类差异交给
    :func:`_match_scene_name` 的「唯一子串」一级去兜（且必须唯一）。

    ⚠️ 2026-09-29：实现已移到 :mod:`asset_name_match`（三类资产共用一份），
    这里只是保持旧函数名的薄封装。
    """
    return asset_name_match.normalize(name)


def _on_screen_characters(shot: dict) -> list:
    """「本镜画面内可见角色」的**权威口径**（单一来源，2026-10-05）。

    直接委托 :func:`te_3d_director.on_screen_characters` —— 它同时是 3D 导演台
    ``_parse_characters`` 的实现（``render_blocking → build_render_plan →
    _parse_characters``）。因此「分镜该不该注入 3D 基准图」的判据与「3D 导演台渲不渲得出
    人偶」的判据**是同一个函数**，天然同源，不会再出现「A 处说没人、B 处说有人」的分叉。

    ⚠️ 只认剧本的 ``characters_in_shot``；**台词 speaker 可能是画外音，不算出场角色**。

    te_3d_director 不可导入时返回 ``[]``：此时 ``render_blocking`` 同样不可用（本镜本就不会
    注入基准图），判为「无出场角色」是安全的保守值，不会误注入人偶。
    """
    if not isinstance(shot, dict):
        return []
    try:
        import te_3d_director  # noqa: PLC0415
        return list(te_3d_director.on_screen_characters(shot))
    except Exception as e:  # noqa: BLE001 - 导演台不可用时保守判为「无出场角色」
        logger.warning("[3D导演台] 解析出场角色失败（按无出场角色处理）：%s", e)
        return []


def _shot_has_on_screen(shot) -> bool:
    """本镜是否有**画面内出场角色**（读 :func:`_on_screen_characters` 的权威口径）。

    正常路径下 ``_allocate_storyboard_refs`` 已把该口径以 ``shot['_on_screen_chars']``
    挂回；字段**缺失**时保守返回 ``True``（沿用旧行为）—— 避免某条没走分配器的调用路径被
    误判成无人物镜、丢掉构图基准图或加错提示词（宁可少禁、不可误禁）。
    """
    if not isinstance(shot, dict):
        return True
    return bool(shot.get("_on_screen_chars", True))


def _shot_has_char_ref(shot) -> bool:
    """本镜是否有**已命中资产的角色身份参考图**（读 ``_allocate_storyboard_refs`` 挂回的
    ``shot['_char_ref_names']`` = :func:`_match_shot_chars` 的 ``chars_in``）。

    ## 为什么渲染 3D 人偶基准图还要再过这一关（2026-10-05）

    ``COMPOSITION_BASELINE_SECTION``（comfyui_client.py）要求模型「用其他参考图里的角色
    **完全覆盖**人偶」。若本镜声明了角色、但角色**资产缺失**（``_match_shot_chars`` 返回
    ``[]``、``_no_reference=True``），refs 里就只有场景图、**没有角色图可覆盖** → 模型只能
    照抄人偶，症状与「画外音兜底」逐字相同。故渲染门槛在 ``_shot_has_on_screen``（declared）
    之外，**还须** ``_shot_has_char_ref``（declared ∩ matched）。

    ⚠️ 本判据是 ``_shot_has_on_screen`` 的**子集**，行为只会更保守（不插 ref、少调一次
    ``render_blocking``），**不会与渲染器产生矛盾输出**，因此不构成「第三个会分叉的口径」。

    字段**缺失**时保守返回 ``True``：与 :func:`_shot_has_on_screen` 同风格 —— 避免某条未走
    ``_allocate_storyboard_refs`` 的调用路径被误判成「无角色图」而丢掉基准图（宁可多渲、
    不可误禁）。注意：真走到渲染前的正常路径**一定**已挂该字段。
    """
    if not isinstance(shot, dict):
        return True
    return bool(shot.get("_char_ref_names", True))


def _shot_outfit_dir(shot: dict, character_name: str, char_dir: str) -> str:
    """解析「本镜服装提示」→ 该角色**已生成**的服装变体目录（outfits/<key>/）。

    服装提示来源（按优先级）：
      ① ``shot["outfit"]``（本镜服装提示，字符串）；
      ② ``shot["character_outfits"]`` 按角色名查（值可为字符串，或
         {outfit_key|key|outfit|desc: ...} 形态的字典；键走别名归一匹配）。
    解析：剥掉「本套服装：/服装：/outfit(_key)?:」前缀后，与该角色主设定目录下
    ``outfits/`` 的**已存在**子目录名做精确匹配（含书名号/引号剥离与安全化键）——
    只认磁盘上真实存在的变体，解析出的名字没建过目录就回落主设定图。

    ⚠️ fail-open：任何一步解析不出 / outfits 目录不存在 → 返回 ''，调用方按
    「无变体」走主设定图（与改造前行为完全一致）。查不到表也绝不抛异常。
    """
    if not isinstance(shot, dict) or not char_dir or not os.path.isdir(char_dir):
        return ""
    outfit_root = os.path.join(char_dir, _OUTFITS_DIRNAME)
    try:
        known = [d for d in os.listdir(outfit_root)
                 if os.path.isdir(os.path.join(outfit_root, d))]
    except OSError:
        return ""
    if not known:
        return ""
    raw = str(shot.get("outfit") or "").strip()          # ① 本镜服装提示（优先）
    if not raw:
        co = shot.get("character_outfits")               # ② 按角色名查
        if isinstance(co, dict):
            v = co.get(character_name)
            if v is None:
                _norm = _normalize_char_alias(character_name)
                for k, vv in co.items():
                    if _normalize_char_alias(str(k)) == _norm:
                        v = vv
                        break
            if isinstance(v, dict):
                v = (v.get("outfit_key") or v.get("key")
                     or v.get("outfit") or v.get("desc") or "")
            raw = str(v or "").strip()
    if not raw:
        return ""
    _m = re.match(r"^(?:本套服装|服装|outfit(?:_key)?)\s*[:：=]\s*(.+)$", raw)
    cand = (_m.group(1) if _m else raw).strip()
    for k in (cand, cand.strip("「」《》\"'“”‘’")):
        if k in known:
            return os.path.join(outfit_root, k)
    _sk = _sanitize_outfit_key(cand)
    if _sk and _sk in known:
        return os.path.join(outfit_root, _sk)
    return ""


def _allocate_storyboard_refs(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                               project_name: str = None) -> list:
    """为单个镜头分配参考图（Qwen-Image-2.1 reference stack，最多 9 张）

    槽位键名随编辑节点换代而变（QwenImage2.1 的 TextEncodeQwenImage21 是
    ``images.image_1..9``，老的 TextEncodeQwenImageEditPlus 是 ``image1..3``），
    由 comfyui_client._find_image_slots 统一识别；本函数只负责**按序**给出这些图。

    ## 为什么改成 9 槽位（2026-09-25）

    Qwen-Image-2.1 官方规格支持最多 10 张参考图，且官方 Prompt Rewriter 的核心是
    **Attribute Disentanglement**（属性解耦）：每张参考图解决**一个明确问题**，
    由 ``<image1>…<imageN>`` 显式编号绑定职责。官方同时强调「10 是容量不是目标」——
    塞重复图片会让模型分不清哪张优先。

    旧实现按「主角色 / 次要 / 场景」压成 3 张，导致：多角色镜头第 3 人起直接丢失、
    服装与身份混在同一张图、道具只能挤占角色槽位 —— 这些正是质检重灾区
    （角色/背景不一致 16+11 次）。

    ## 槽位分工（固定顺序，与 build_storyboard_prompt 的 <imageN> 编号一一对应）

      1. ``<image1>`` 主角色身份锚点 —— 兼作画布/构图基线（官方：image_1 是 edit target）
      2. ``<image2>`` 次角色身份（多角色镜头才有；让模型独立保身份，禁止特征串味）
      3. ``<image3>`` 第三角色身份（三人同框时才有）
      4. ``<image4>`` 主角色服装（角色资产图与服装资产不同图时才有独立槽位）
      5. ``<image5>`` 场景
      6. ``<image6>`` 道具（镜头内物品，可多个）
      7. ``<image7>`` 构图参考（特写镜头放头部特写锚点）
      8. ``<image8>`` 上一镜连续性锚点（同场景承接时才有）

    只填**实际需要**的前 N 个：未用到的尾部槽位在生成端被留空
    （见 generate_storyboard 的 slot_cleared），不会塞重复图。
    """
    chars_in = _match_shot_chars(shot, char_idx)
    # ⭐ 2026-10-05：把「本镜画面内可见角色」以**单一权威口径**挂回 shot，供调用点的
    #    3D 构图基准图注入判据复用（见工作线程里 `_shot_has_on_screen(shot)`）。
    #    ⚠️ 取的是**剧本声明**（characters_in_shot），不是 chars_in（= 声明 ∩ 资产索引命中）：
    #    `has_characters` **提示词参数**要的正是这个「本镜该不该有人」的剧本意图。
    shot["_on_screen_chars"] = _on_screen_characters(shot)
    # ⭐ 2026-10-05：另行挂回**已命中资产的角色名**（= chars_in），专供「渲不渲 3D 基准图」的
    #    门槛用（见 `_shot_has_char_ref`）。为什么渲染门槛要比提示词门槛更严：人偶基准图必须
    #    被「其他参考图里的角色」完全覆盖才有意义；角色资产缺失时没有可覆盖的角色图，渲了只会
    #    被照抄（与本次缺陷同症状）。该判据是 `_on_screen_chars` 的子集，只更保守、不会分叉。
    shot["_char_ref_names"] = list(chars_in)
    # 2026-09-29：物品与角色/场景同口径——走统一匹配器（原先 `if n in item_idx`
    # 让名字稍有差异的物品**静默不带参考图**，且不打日志）。
    items_in = _resolve_item_names(shot, item_idx, "分镜参考图")

    refs = []
    # ⭐ 2026-10-05：区分两种「一个角色都没匹配到」——
    #   (a) characters_in_shot **本就为空** → **合法的无人物镜头**（道具特写/空镜/纯画外音），
    #       **不得**置 _no_reference、不写 _ref_error（否则会产出误导文案
    #       「角色 [] 在资产索引中均无匹配」，并让该镜走 S6 报错路径）；
    #   (b) 声明了角色但 resolve 后一个都没命中 → 保持 S6 行为（置 _no_reference + _ref_error
    #       + 日志），由调用方决定 400 / 跳过。
    # ⚠️ 消费方 `if not refs:` 分支（工作线程内）只在前者为「(b) 且无任何其他参考图」时
    #    才触发；`(a)` 或「有场景图但无角色」的镜头 refs 非空 → 该分支不触发（这正是 shot#1
    #    以前被「静默放行」的原因）。
    if not chars_in and (shot.get("_on_screen_chars") or []):
        # S6：禁止静默 take-first —— 镜头声明了角色却一个都匹配不到时，标记 no_reference，
        # 由调用方决定 400（单镜）/ 跳过 + 警告日志（批量）。
        shot["_no_reference"] = True
        shot["_ref_error"] = (
            f"镜头 {shot.get('shot_id')} 的角色 {shot.get('characters_in_shot')} "
            f"在资产索引中均无匹配（别名归一化后仍无）")

    used_paths = set()
    # 本镜景别决定角色参考图取「半身档」还是「全身档」（画幅与景别同向，消除对抗）。
    _want_half = _framing_wants_half_shot(shot)

    # ---- <image1>…<image3>：角色身份锚点（逐个独立，禁止特征串味）----
    # 官方要点：多角色时每人一张图 + 明确「independently / Do not merge facial features」，
    # 否则最常见的失败就是 A 的脸跑到 B、B 的衣服跑到 C。
    for i, name in enumerate(chars_in):
        if i >= 3:
            break                       # 3 个角色身份槽位；第 4 人起并入构图说明
        payload = char_idx.get(name, {}) or {}
        # 2026-09-25 景别对档：近景/特写/中景优先取 half.png（半身胸像），
        # 全景/远景优先取 front.png（全身）；档位缺失时逐级回退（见 _pick_char_view）。
        # 2026-10-02 服装变体：本镜服装提示（shot.outfit 优先，其次
        # shot.character_outfits 按角色名查）能解析出已生成的 outfit_key →
        # 参考图优先取 outfits/<key>/ 的同档位图；解析不出/未生成回落主设定图。
        img = _pick_char_view(payload, _want_half,
                              _shot_outfit_dir(shot, name, payload.get("_dir") or ""))
        if not img or img in used_paths:
            continue
        used_paths.add(img)
        _zoom = "半身近景" if _want_half else "全身"
        if i == 0:
            refs.append(("主角色",
                         f"参考图1（<image1>）是角色「{name}」的身份锚点（{_zoom}视图）："
                         f"保持其面部身份、发型与体型不变", img))
        else:
            refs.append(("次角色",
                         f"参考图{i + 1}（<image{i + 1}>）是角色「{name}」的身份锚点（{_zoom}视图）："
                         f"独立保持其面部身份与发型，不得与其他角色特征混用", img))

    # ---- <imageN>：场景（角色之后，作为环境锚点）----
    # 2026-09-29：走统一容错匹配（精确 → 归一化 → 唯一子串）；未命中时**显式告警**，
    # 不再沿用旧的 `loc if loc in scene_idx else None`——那条路会静默丢场景锚点。
    scene_name, _sc_entry = _resolve_scene_entry(shot, scene_idx, "分镜参考图")
    # 2026-09-29：取**与本镜机位同向**的那一档场景图（俯拍→俯视档 / 斜侧→斜侧档），
    # 而不是恒取基准图 —— 机位反向的参考图会把构图先验拽反。档位缺失时逐级回落。
    _sc_view = _scene_view_for_shot(shot)
    scene_img = _pick_scene_view(_sc_entry, shot) if _sc_entry else None
    if scene_img and scene_img in used_paths:
        scene_img = None

    # ---- <imageN>：道具（镜头内物品，可多个但最多 2 张，避免挤占角色槽位）----
    item_refs = []
    for cand in items_in:
        if len(item_refs) >= 2:
            break
        img = item_idx.get(cand, {}).get("image")
        if not img or img in used_paths or img == scene_img:
            continue
        used_paths.add(img)
        item_refs.append((cand, img))

    slot_no = len(refs) + 1
    if scene_img:
        # 2026-09-29 与场景新规格同步：``novel_to_script`` 的 reference_prompt_zh
        # 已升级为「地理优先」（先定空间结构与地理关系，再定光照基调），此处槽位
        # 话术同步升级，并**显式声明不锁定机位**——场景基准图是一个固定 View，
        # 而分镜是另一个机位；不声明的话模型会照搬参考图的取景范围（实测口径与
        # 特写镜头「场景仅作基调参考」一致）。
        # ⚠️ 改这句话必须同步 comfyui_client._REF_ROLE_ZH2EN（中文职责 → 官方英文
        #    短语）与 prompt_memory._BOILERPLATE（相似度剥离用），否则职责声明会
        #    退化成中文原文、教训召回会被套话稀释。
        # 2026-09-29：参考图已按**本镜机位**选档（俯拍→俯视档…），故话术里点明档位，
        #    让模型知道这张图**就是本镜机位**的空间参考，「不得照搬机位」的豁免随之
        #    收紧为「同一机位、取景范围仍以镜头描述为准」——档位与镜头同向时再照搬
        #    机位不再有害，但**取景范围**（全景 vs 特写）仍必须听镜头。
        refs.append(("场景",
                     f"参考图{slot_no}（<image{slot_no}>）是场景「{scene_name}」"
                     f"（{SCENE_VIEW_LABELS.get(_sc_view) or _sc_view}）的空间与光照锚点："
                     f"保持空间结构、地理关系（入口 / 通道 / 固定陈设的位置关系）"
                     f"与光源方向、色温基调一致；"
                     f"取景范围以本镜镜头描述为准，不得照搬该参考图的构图范围",
                     scene_img))
        used_paths.add(scene_img)
        slot_no += 1
    for cand, img in item_refs:
        refs.append(("道具",
                     f"参考图{slot_no}（<image{slot_no}>）是物品「{cand}」的形状、材质与配色锚点",
                     img))
        slot_no += 1

    refs = _apply_closeup_ref_strategy(refs, shot, project_name)
    # P0-3（借鉴 ViMax reference_image_selector）：每镜参考图**相关性优先 + ≤8 张上限**。
    return _cap_storyboard_refs(refs, shot)


def _closeup_char_crop(img_path: str, project_name: str, shot_id) -> str:
    """把角色正视/半身图裁剪为头部特写图，作为特写镜头的构图锚点（失败则回退原图）。"""
    try:
        from PIL import Image
        out_dir = os.path.join(QC_DIR, str(project_name or "default"), "closeup_refs")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, f"char_closeup_shot{_shot_seq(shot_id, 1):02d}.png")
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            x0, y0, x1, y1 = CLOSEUP_CHAR_CROP_TOP
            band = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
            side = max(1, min(band.width, band.height))
            left = max(0, (band.width - side) // 2)
            crop = band.crop((left, 0, left + side, side))
            target_w = max(768, crop.width)
            crop = crop.resize((target_w, max(1, int(target_w * crop.height / crop.width))))
            crop.save(dst, format="PNG")
        return dst
    except Exception as e:  # 裁剪失败不影响主流程，回退原图
        logger.warning(f"特写参考图裁剪失败，回退原图: {e}")
        return img_path


def _apply_closeup_ref_strategy(refs: list, shot: dict, project_name: str = None) -> list:
    """特写镜头：只保留「角色头部特写」单一锚点。

    实测（4 轮 12 次生成）：混入场景/道具参考时，模型会按参考图的取景范围把画面
    铺开成中全景，并把道具画回手中；道具参考剔除后模型仍会自行"想象"出手持物。
    因此特写镜头只给头部特写锚点，构图约束最强。

    ⚠️ 2026-09-25 口径变化（Qwen-Image-2.1 9 槽位）：
      旧实现「多出的槽位自动复用同一张」在 9 槽模板下会把头部特写复制到 8 个槽位，
      而官方明确「容量不是目标，重复图反而稀释注意力」。现在特写镜头**只输出 1 张**
      （主角色头部特写，占 ``<image1>``），其余槽位由生成端留空。
      多角色特写时保留每个角色的头部特写（各占一槽），不复制。
    """
    # 审计 P2-13（2026-09-29）：特写判定改为 shot_type / camera 双查 —— camera 字段
    # 已拆分（A1），_norm_shots 允许 camera 为纯运镜串（如「推入」）而 shot_type=「特写」；
    # 只看旧 camera 会漏掉这类镜头，与生成端（build_storyboard_prompt 按 shot_framing
    # 注入特写硬约束）判定分裂。
    _is_closeup = ("特写" in str(shot.get("shot_type") or "")
                   or "特写" in str(shot.get("camera") or ""))
    if not _is_closeup:
        return refs
    out = []
    for kind, label, path in refs:
        if kind in ("主角色", "次角色"):
            # 保留 <imageN> 编号锚点，让质检端能继续核对「编号 ↔ 职责」一致性。
            # 审计 P2-19（2026-09-29）：必须完整保留「（<imageN>）」整组 —— 旧实现
            # 只留「参考图N（」，<imageN> 被削掉，下游 _ref_label_body 剥前缀不净
            # （提示词出现「参考图2（已替换为…」脏片段）且特写镜丢身份保留句。
            _mark = ""
            if label.startswith("参考图"):
                _m = re.match(r"(参考图\d+（<image\d+>）)", label)
                if _m:
                    _mark = _m.group(1)
                else:
                    _head = label.split("）", 1)[0]
                    _mark = _head.split("（", 1)[0] + "（"
            out.append((kind,
                        _mark + "已替换为该角色头部特写，画面取景范围以此为准：仅肩部以上",
                        _closeup_char_crop(path, project_name, shot.get("shot_id", 1))))
    return out or refs


def _cap_storyboard_refs(refs: list, shot: dict) -> list:
    """P0-3（借鉴 ViMax reference_image_selector）：每镜参考图**相关性优先 + ≤8 张上限**。

    官方 Qwen-Image-2.1 支持最多 10 张但明确「容量不是目标」；ViMax 实践为每帧 ≤8 张，
    且同角色多视图只取一张（已由 ``_pick_char_view`` 按景别对档处理）。这里在分配末端
    再加一道安全网：超出 8 张时按相关性优先级截断（角色身份 > 场景 > 道具），
    绝不丢弃角色身份锚点，并打日志便于排查「参考图被静默截断」。

    只做**保序截断**，不改变已分配槽位的职责语义；``build_storyboard_prompt`` 按位置
    重编号 ``<imageN>``，列表顺序即相关性顺序。
    """
    MAX_STORYBOARD_REFS = 8
    if len(refs) <= MAX_STORYBOARD_REFS:
        return refs
    _rank = {"主角色": 0, "次角色": 0, "场景": 1}   # 其余（道具等）→ 2，最先被截断
    ordered = sorted(refs, key=lambda r: _rank.get(r[0], 2))
    dropped = ordered[MAX_STORYBOARD_REFS:]
    logger.warning(
        "镜头 %s 参考图 %d 张 > 上限 %d，已按相关性丢弃 %d 张：%s",
        shot.get("shot_id"), len(refs), MAX_STORYBOARD_REFS, len(dropped),
        "、".join(f"{d[0]}:{str(d[1])[:24]}" for d in dropped))
    return ordered[:MAX_STORYBOARD_REFS]


def _qc_brief(kind: str) -> dict:
    """任务级质检摘要（随状态接口下发，供前端展示徽标与开关状态）"""
    try:
        cfg = _qc_load_cfg()
        view = qc_client.public_view(cfg)
        active = view["image_qc_active"] if kind == "image" else view["video_qc_active"]
        return {"enabled": view["enabled"], "active": active,
                "declared": bool(view["enabled"] and (cfg.get("image_enabled") if kind == "image"
                                                      else cfg.get("video_enabled"))),
                "pass_score": cfg.get("pass_score"), "max_retries": cfg.get("max_retries"),
                "video_frame_count": cfg.get("video_frame_count"),
                "model": view["effective_model"], "endpoint_source": view["endpoint_source"]}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"质检摘要生成失败（忽略）：{e}")
        return {"enabled": False, "active": False, "declared": False}


def _qc_retry_hopeless(attempts: list, streak: int = 2) -> tuple:
    """连续 ``streak`` 次重试的缺陷特征**完全相同** → 判定「改提示词 + 换种子」没有产生
    任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 连烧 6 次全败，缺陷一字不差）。

    返回 ``(True, "缺陷摘要")`` 或 ``(False, "")``。

    G1 修复：把本函数抽到 qc_client.qc_retry_hopeless（共享叶子模块），
    供 comfyui_client / keyframe 以注入回调（qc_stop_cb）方式复用，
    规避 app ↔ comfyui_client 的循环依赖。app 侧把 style_issues 合并进
    issues 再委托 —— 通用模块不认识 app 特有字段。

    ⚠️ 刻意保守（宁可多试一次，也不要误停）：
      · 特征为**空**时一律不判定 —— 质检没给出可用信息 ≠ 缺陷相同；
      · 必须最近 streak 条**逐条集合相等**（多一条少一条都不算）；
      · **不改变闸门结论**：该镜仍算未通过、仍不进正式目录，只是不再继续重试；
        用户可直接改这一镜的剧本字段（如 camera / description）后单独重跑该镜。
    """
    merged = []
    for r in (attempts or []):
        if not isinstance(r, dict):
            continue
        issues = list(r.get("issues") or [])
        style = list(r.get("style_issues") or [])
        if style:
            issues = issues + style
        merged.append({"issues": issues, "critical_issues": r.get("critical_issues") or []})
    return qc_client.qc_retry_hopeless(merged, streak)


def _qc_prune_attempts(scratch_dir: str, keep: int = 4) -> None:
    """G8：质检暂存区 try 产物滚动保留 —— 每个 shot/asset 只保留最近 keep 个尝试。

    背景（审计 G8）：图片链路每次都把产物 copy 进暂存区 `output/qc/<项目>/{assets,storyboard,
    video}_scratch/`，不达标越多残留越多，实测 qc 目录膨胀到 1.1GB。临时产物（`*_tryN`）
    只有「最近几轮」对续跑/排障有意义，更早的纯浪费。这里按 (前缀, 尝试号, 扩展名) 归组，
    每组只留最大的 keep 个 try，其余删除。

    设计取舍：
      - 只清「带 _try 后缀的临时产物」，正式交付图（base.png/shot_XX.png）绝不动；
      - 单文件失败静默跳过（清理是优化而非功能，绝不能因清错文件阻断生产）；
      - 保留策略对 `.png`/`.mp4`/`.srt`/`.list` 通用，三类暂存区都能复用。
    """
    if not os.path.isdir(scratch_dir):
        return
    try:
        import re as _re
        groups = {}   # (前缀, 扩展名) -> [尝试号]
        info = {}      # 尝试号 -> 完整路径
        for fn in os.listdir(scratch_dir):
            # ⚠️ 单镜重跑落盘名是 `shot_NN_retry.png`（**无** `_tryN` 数字后缀），
            # 旧正则 `^(.+)_try(\d+)(\.\w+)$` 匹配不到 → 该文件永不被滚动清理、只增不减。
            # 这里把 `_retry` 也纳入：归到 num=0（比任何 `_tryN` 都旧 → 优先被清）。
            m = _re.match(r"^(.+)_try(\d+)(\.\w+)$", fn)
            if not m:
                m = _re.match(r"^(.+)_retry(\.\w+)$", fn)
                if m:
                    prefix, num, ext = m.group(1), 0, m.group(2)
                else:
                    continue   # 非 try/retry 命名（正式产物/杂项）一律不动
            else:
                prefix, num, ext = m.group(1), int(m.group(2)), m.group(3)
            groups.setdefault((prefix, ext), []).append(num)
            info[(prefix, ext, num)] = os.path.join(scratch_dir, fn)
        removed = 0
        for (prefix, ext), nums in groups.items():
            nums = sorted(nums)   # P2-2（A-15）：listdir 无序，必须按尝试号排序，
                                  # 否则 `nums[-keep:]` 保留的是「最早遍历到」而非「最新尝试号」
            keep_set = set(nums[-keep:]) if len(nums) > keep else set(nums)
            for num in nums:
                if num in keep_set:
                    continue
                p = info.get((prefix, ext, num))
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass   # 文件被占用/权限问题：跳过，不阻断
        if removed:
            logger.info("G8 暂存区滚动清理 %s：删 %d 个过期 try（每组保留最近 %d）",
                            scratch_dir, removed, keep)
    except Exception as e:  # noqa: BLE001
        logger.warning("G8 暂存区清理失败（不影响生产）：%s: %s",
                           type(e).__name__, e)


def _qc_style_of(project_name: str) -> str:
    """取项目**当前**风格，供教训召回替换建议里的 ``{style}`` 占位符。

    数据源优先用 autopilot 计划（用户与总控敲定的创作设定，最权威），
    其次退回项目级创作设定的 style；都取不到就返回空串
    （此时 ``prompt_memory`` 会主动丢弃带 ``{style}`` 的建议，而不是把别的项目的风格安上来）。
    """
    try:
        plan = autopilot.get_plan(project_name) or {}
        s = style_kit.normalize_style(plan.get("style"))
        if s:
            return s
    except Exception as e:  # noqa: BLE001
        logger.debug("读取托管计划风格失败（忽略）：%s", e)
    try:
        for ep in (novel_to_script.list_episodes(SCRIPT_DIR, project_name, project_name) or []):
            path = ep.get("path") or ""
            if path and os.path.isfile(path):
                data = project_store._read_json(path, {}) or {}
                s = style_kit.normalize_style(data.get("style"))
                if s:
                    return s
    except Exception as e:  # noqa: BLE001
        logger.debug("读取剧本风格失败（忽略）：%s", e)
    return ""


def _qc_shot_desc(shot: dict) -> str:
    """构造交给质检模型的「镜头信息」。

    ⚠️ 景别必须带上**判定标准**，不能只给裸词。
    生成端用 ``SHOT_CAMERA_SPECS[camera]`` 的精确定义写提示词，而质检端此前只传
    「机位：中景跟拍」——模型只能凭自己的理解判「中景」，与生成端标准不一致，
    实测分镜图通过率仅 57%、失败原因几乎全是「景别不符」（把腰部以上的中景判成不合规）。
    这里改为引用 :func:`comfyui_client.camera_spec`（经模块顶部的 ``_camera_spec`` 别名调用，
    因为本文件里 ``comfyui_client`` 是实例而非模块），保证两端**同一份标准**。
    """
    parts = []
    # 景别放在最前：描述较长时 [:900] 截断会吃掉尾部，判定标准必须优先保住
    if shot.get("camera"):
        cam = str(shot["camera"]).strip()
        # ⚠️ 必须显示「解析后的景别」而不是只给裸词：camera 常是「景别+机位+运镜」的复合写法。
        # 且**景别未给时不许编**（旧实现回落中景 → 拿中景标准去判脚部俯拍图，必然判不符，
        # 该镜永远过不了；实测 ep02 shot_13 因此白烧 6 次 GPU）。
        cam_k = _camera_key(cam)
        parts.append(f"景别：{cam_k or '未指定'}（camera 原值「{cam}」；判定标准：{_camera_spec(cam)}）")
        # 机位与景别正交：只给景别不给机位的话，「要求俯拍却给了平视」没人能判出来。
        ang = _camera_angle(cam)
        if ang:
            parts.append(f"机位：{ang}（须与画面一致）")
    if shot.get("location"):
        parts.append(f"场景：{shot['location']}")
    if shot.get("description"):
        parts.append(str(shot["description"]).strip())
    if shot.get("emotion"):
        parts.append(f"情绪：{shot['emotion']}")
    if shot.get("dialogue"):
        parts.append(f"台词：{str(shot['dialogue']).strip()[:80]}")
    return "；".join(parts)[:900] or "（无镜头描述）"


def _qc_prev_shot_desc(shots: list, idx: int) -> str:
    """取「上一镜」的文字描述，供图片质检做跨镜连续性比对（P1，2026-09-25）

    ⚠️ 只在**同场景**时返回：跨场景切换本就该换背景、换光位，拿上一镜去比会判出
    一堆假缺陷（与 ``keyframe.same_scene`` 同一判据，口径保持一致）。
    首镜、或上一镜不同场景 → 返回空串，``check_image`` 便完全不加连续性口径
    （零行为变更，也不会诱发模型凭「上一镜」三个字臆造缺陷）。
    """
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    if i <= 0 or i >= len(shots or []):
        return ""
    prev = shots[i - 1] if isinstance(shots[i - 1], dict) else {}
    cur = shots[i] if isinstance(shots[i], dict) else {}
    _same = False
    for k in ("location", "scene_id", "scene", "scene_name"):
        a = str(prev.get(k) or "").strip()
        b = str(cur.get(k) or "").strip()
        if a and b:
            # 2026-09-29：与场景参考图取用共用同一套归一化（全半角 / 引号 / 空白）。
            # 旧写法 `a == b` 会把「铜铃巷」vs「铜铃巷（夜）」判成换场，
            # 于是**无谓地关掉**连续性判定与上一镜锚点，同场景承接镜失去衔接约束。
            _same = (_normalize_scene_name(a) == _normalize_scene_name(b))
            break
    else:
        # 两边都没写场景信息 → 无法判定「是否换场」。宁可**不启用**连续性判定
        # （不给描述比给错描述安全：错描述会让模型把正常换场判成缺陷）。
        _same = False
    if not _same:
        return ""
    bits = []
    if prev.get("camera"):
        bits.append(f"景别 {str(prev['camera']).strip()}")
    if prev.get("description"):
        bits.append(str(prev["description"]).strip()[:200])
    return "；".join(bits)[:300]


def _qc_prev_shot_ref(shots: list, idx: int, out_dir: str) -> str:
    """取「上一镜」已入库的分镜图路径，供图片质检真正做画面级比对（P1）

    与 :func:`_qc_prev_shot_desc` 同判据（仅同场景）。**只取正式目录里已通过的图**
    （``out_dir/shot_NN.png``）—— 不达标的图留在暂存区，拿它当基准会把缺陷传播下去。
    首镜 / 跨场景 / 上一镜尚未入库 → 返回空串。
    """
    if not _qc_prev_shot_desc(shots, idx):
        return ""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    prev = shots[i - 1] if isinstance(shots[i - 1], dict) else {}
    try:
        pseq = _shot_seq(prev.get("shot_id"), i)
    except Exception:  # noqa: BLE001
        return ""
    p = os.path.join(out_dir, f"shot_{pseq:02d}.png")
    return p if os.path.isfile(p) else ""


def _qc_ref_images(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                   fallback_refs: list = None) -> list:
    """为「图片质检」收集**本镜出现**的角色 / 物品 / 场景设定图 → [(label, path)]。

    为什么要单独收集，而不直接复用生成侧的 `_allocate_storyboard_refs`：
    生成侧只有 3 个参考图槽位（主角色 / 次要 / 场景），最多带 3 张；而质检的目的是
    **逐个核对画面里的每个角色、每件物品有没有变形、是否与设定一致**，所以按
    `shot.characters_in_shot` / `shot.items_in_shot` 全量收集（总数上限
    `qc_client.MAX_REF_IMAGES`，在 check_image 内还会按路径去重）。

    历史缺陷：分镜质检只把成品图单独送检，模型手里没有任何设定锚点，
    「这个角色长得像不像设定」「这柄剑的形制对不对」只能靠它自己猜 ——
    「角色不像设定 / 道具走形」这类问题要么被放过、要么被误判。
    """
    out, seen = [], set()

    def _add(label, path):
        if not path or path in seen:
            return
        seen.add(path)
        out.append((label, path))

    # 2026-09-29：角色 / 物品也走统一匹配。原先这里与场景**不同源** ——
    # 场景已用 _resolve_scene_entry，角色 / 物品却是裸 char_idx.get(n) /
    # item_idx.get(n)：名字只因标点或后缀差一点，质检就**拿不到该资产的设定图**，
    # 「这个角色像不像设定」只能靠模型凭记忆猜（正是本函数注释里说的历史缺陷）。
    for n in _match_shot_chars(shot, char_idx):
        _add(f"角色「{n}」的外貌、服装与发型", (char_idx.get(n) or {}).get("image"))
    for n in _resolve_item_names(shot, item_idx, "图片质检"):
        _add(f"物品「{n}」的形状、材质与配色", (item_idx.get(n) or {}).get("image"))
    loc, _scene_e = _resolve_scene_entry(shot, scene_idx, "图片质检")
    if _scene_e:
        # 质检口径与生成侧话术同步（2026-09-29）：场景锚点核对的是**空间结构与
        # 光照基调**，而非笼统的「环境与氛围」——与 <imageN> 场景槽位新规格一致。
        # ⚠️ 送检的锚点也必须**同机位**（与生成侧 _allocate_storyboard_refs 同一取图函数）：
        #    否则判官拿正面锚点去判一张俯拍分镜图，只会判「背景不一致」，
        #    把「机位档没生效」误报成「场景画错了」。
        _add(f"场景「{loc}」的空间结构与光照基调",
             _pick_scene_view(_scene_e, shot))
    if not out:
        # 兜底：本镜没登记角色/物品时，用生成侧实际用的那几张（至少保住场景锚点）
        # ⚠️ 2026-10-02 修复：必须**跳过构图基准图**。生成侧 refs 的第 1 项可能是
        #    `(_blocking_ref_path, _BLOCKING_REF_MARK, ...)`（见上方 refs.insert(0, ...)，
        #    标签现为常量 _BLOCKING_REF_MARK = "3D导演台构图基准"）——
        #    那是**无面人偶预演图**，实测把它当设定图送检会严重污染判定
        #    （同图 score 88 → 35，且诱发臆造缺陷，见 qc_client.check_image 的定论）。
        #    ⭐ 下方过滤判据 `"构图基准" in _kind/_label` 对新旧标签（"3D构图基准"/
        #    "3D导演台构图基准"）都命中，无需随标签改动。
        #    原先兜底无差别收下 refs，等于从「质检输入」这个后门把基准图放了回去。
        for r in (fallback_refs or []):
            if not (isinstance(r, (list, tuple)) and len(r) >= 3):
                continue
            _kind, _label = str(r[0] or ""), str(r[1] or "")
            if "构图基准" in _kind or "构图基准" in _label:
                continue
            _add(_label, r[2])
    return out[:qc_client.MAX_REF_IMAGES]


def _qc_history_file_for(project_name: str, kind: str, shot_id) -> str:
    """推算某条质检历史的落盘路径（与 qc_client.history_path 同口径）。

    用途：产物被移入回收站后，把该历史里指向已删路径的 `file` 记为 null（断链修正），
    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据。
    """
    try:
        return qc_client.history_path(QC_DIR, project_name, kind, shot_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"质检历史路径推算失败（忽略）：{e}")
        return ""


def _qc_summary(attempts: list, enabled: bool, ready: bool, max_retries: int) -> dict:
    """汇总一次资产生成的全部质检尝试，供前端展示徽标 / 明细 / 重试次数"""
    if not enabled:
        return {"enabled": False, "status": "disabled", "label": "质检未开启",
                "attempts": 0, "max_retries": max_retries}
    if not ready:
        return {"enabled": True, "status": "unconfigured", "label": "质检未配置（未启用）",
                "attempts": 0, "max_retries": max_retries}
    passed_rec = next((a for a in attempts if a.get("passed")), None)
    last = attempts[-1] if attempts else {}
    # P0-2：接口级故障放行——最后一次尝试是 interface_fault（鉴权/超时/网络）且无客观层
    # 致命缺陷 → 资产已被 fail-open 写入，不算「error/不达标」，单独一档 fault_open。
    fault_open = bool(last.get("interface_fault")) and not passed_rec and \
        not (last.get("critical_issues") or [])
    if fault_open:
        status = "fault_open"
        blocked = False
    else:
        status = "passed" if passed_rec else ("error" if last.get("error") else "failed")
        blocked = status in ("failed", "error")
    crit = list((passed_rec or last).get("critical_issues") or [])
    # ★ 重试止损（_qc_retry_hopeless）：未通过且已提前停止重试时，把原因写进 label，
    #   否则用户只看到「质检不达标」，不知道系统其实已经主动止损（没在继续烧 GPU）。
    retry_stopped = bool((last or {}).get("retry_stopped")) and not passed_rec
    _label = {"passed": "质检达标", "failed": "质检不达标", "error": "质检调用异常",
              "fault_open": "质检接口故障·已放行"}.get(status, status)
    if retry_stopped:
        _label = "质检不达标（已停止重试：连续两次缺陷完全相同）"
    return {
        "enabled": True,
        "status": status,
        "label": _label,
        "fault_open": fault_open,
        "retry_stopped": retry_stopped,
        "retry_stopped_detail": (last or {}).get("retry_stopped_features") or "",
        "passed": bool(passed_rec),
        "blocked": blocked,
        "entry_blocked": blocked,
        "asset_written": (not blocked),
        "critical_issues": crit,
        "score": (passed_rec or last).get("score"),
        "reason": (passed_rec or last).get("reason") or (last.get("error") or ""),
        "issues": (passed_rec or last).get("issues") or [],
        "attempts": len(attempts),
        "regenerated": max(0, len(attempts) - 1),
        "max_retries": max_retries,
        "history": attempts,
        "history_file": last.get("history_file") or "",
    }

