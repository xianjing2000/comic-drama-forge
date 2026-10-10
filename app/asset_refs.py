# -*- coding: utf-8 -*-
'''资产索引与参考图选择（2026-10-11 从 app.py 下沉，助手域第四批）。'''

# 本批是 h3 闭包 —— 实测发现 _h3_* 的依赖闭包实际上是整个「资产索引与参考图
# 选择」子系统（25 个对象约 737 行）：_h3_*(7) + pick/resolve/match/normalize(14)
# + _build_asset_index(80 行) + 3 个常量。因此整批作为一个域迁出。
#
# 搬迁目的：_video_generate_worker_body（1222 行）依赖其中的 _h3_* 与
# _build_asset_index 等，拆出本域后该 worker 才有搬迁的可能。
#
# import 处理：本批不再猜来源 —— 先扫出函数体引用但模块未定义的名字，
# 再从 app.py 的原始 import 语句**原样复制**（含 as 重命名）。
# 教训来源：上一批自动猜 import 把 probe_audio_info 猜成 tts_client 的真实名，
# 实际它是 from tts_client import probe_audio as probe_audio_info 的别名。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
import logging

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
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, send_from_directory
from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from routes.tts import (_dub_audio_url, _dub_project_dir)  # noqa: F401
from tts_client import (
    QwenTTSClient, TTSError, check_environment as tts_env_check,
    build_dub_plan, default_voice_map, normalize_voice, save_voice_map,
    load_voice_map, list_voices as tts_list_voices, probe_audio as probe_audio_info,
    concat_audio, clean_line_text,
    # 参考音频克隆（2026-10-06）
    save_voice_bank_ref, find_voice_bank_ref, list_voice_bank, voice_bank_dir,
    clone_available as tts_clone_available, VOICE_BANK_EXTS
)
from video_postprocess import VideoPostProcessor, ensure_no_audio, ensure_audio_track
import asset_name_match
import h3_common_refs
import h3_director_builder
import json
import os
import project_store

# 2026-10-11 归位：这两个常量语义属于「资产取图判据」，原本被我误搬进
#   artifact_helpers（质检清理域），此处归位到本域。
_ASSET_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_ASSET_IMG_PRIORITY = ("front.png", "base.png", "front.jpg", "base.jpg",
                       "left.png", "right.png", "back.png",
                       "left.jpg", "right.jpg", "back.jpg")

# 2026-10-11 一并下沉：_STATIC_DIR 由它推导，留在 app.py 会让本模块反向依赖 app。
def _resolve_static_dir():
    """定位前端静态目录（app/static），兼容源码运行与 PyInstaller 单文件打包。

    2026-09-30 修复「桌面版页面出不来（GET / 返回 404）」：
    打包后模块以**裸名 app** 从 PYZ 导入（serve.py 的 app.app 回退路径），
    `__file__` = <_MEIPASS>/app.pyc → 旧写法得到 <_MEIPASS>/static，
    而 spec 的 datas 实际把前端放在 <_MEIPASS>/app/static → 找不到 index.html。
    现按「打包布局优先、源码布局兜底」依次探测。
    """
    import sys as _sys
    _cands = []
    if getattr(_sys, "frozen", False):
        _mp = getattr(_sys, "_MEIPASS", "") or ""
        if _mp:
            _cands.append(os.path.join(_mp, "app", "static"))
            _cands.append(os.path.join(_mp, "static"))
    _cands.append(os.path.join(os.path.dirname(__file__), "static"))
    for _c in _cands:
        if os.path.isdir(_c):
            return _c
    return _cands[-1]

import shot_key
_norm_shot_key = shot_key.norm_shot_key

logger = logging.getLogger(__name__)

_FRAMING_HALF_SHOT = ("大特写", "特写", "近景", "中近景", "局部", "中景")
_ASSET_DIRS = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}
_STATIC_DIR = _resolve_static_dir()


def _h3_audio_policy(path: str) -> dict:
    """按 config 的 H3 音轨策略处理刚生成的视频，返回可展示的音频处理记录。

    - `H3_STRIP_AUDIO=True`  → 剥离音轨（2026-09-17 之前的旧行为）
    - 否则 `H3_EMIT_AUDIO=True` → **保留** H3 原生音轨（环境音/打斗音效），
      并顺带保证「每镜都有音轨」：缺的补一条静音轨。
      必须补齐的理由：成片拼接用 concat demuxer + `-c copy`，
      音轨时有时无会让拼接错位甚至失败。
    """
    if H3_STRIP_AUDIO:
        strip = ensure_no_audio(path, backup=True)
        return {
            "policy": "strip",
            "has_audio_before": strip.get("has_audio_before"),
            "has_audio_after": strip.get("has_audio_after"),
            "changed": strip.get("changed"),
            "method": strip.get("method"),
            "backup": strip.get("backup"),
            "message": strip.get("message") or ("已剥离音轨" if strip.get("changed") else ""),
            "error": strip.get("error"),
        }
    pad = ensure_audio_track(path) if H3_EMIT_AUDIO else None
    pad = pad or {}
    if pad.get("error"):
        msg = f"音轨处理异常（已保留原状）：{pad.get('error')}"
    elif pad.get("changed"):
        msg = "原无音轨，已补静音轨（保证拼接一致）"
    elif H3_EMIT_AUDIO:
        msg = "音轨保留（H3 原生音效）"
    else:
        msg = "未做音轨处理"
    return {
        "policy": "keep" if H3_EMIT_AUDIO else "keep_as_is",
        "has_audio_before": pad.get("has_audio_before"),
        "has_audio_after": pad.get("has_audio_after"),
        "changed": pad.get("changed"),
        "method": pad.get("method"),
        "message": msg,
        "error": pad.get("error"),
    }


def _first_existing_asset_image(directory: str) -> str:
    """在目录内取「主视角」图片（扩展名白名单），无则返回 ''。

    统一判据：
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")；
      2) 按 :data:`_ASSET_IMG_PRIORITY` 显式优先级取（**不是**文件名字典序，
         否则 back.png 会排在 base/front 之前被取走，详见该常量注释）；
      3) 优先级名都不存在时，再按字典序取第一张非空图片；
      4) 找不到任何图片 → 返回 ''，调用方自行决策（报错/跳过）。
    """
    if not directory or not os.path.isdir(directory):
        return ""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return ""

    def _ok(fn: str) -> bool:
        return (fn.lower().endswith(_ASSET_IMG_EXTS)
                and os.path.isfile(os.path.join(directory, fn))
                and os.path.getsize(os.path.join(directory, fn)) > 0)

    for cand in _ASSET_IMG_PRIORITY:
        if cand in entries and _ok(cand):
            return os.path.join(directory, cand)
    for fn in entries:
        if _ok(fn):
            return os.path.join(directory, fn)
    return ""


def index():
    return send_from_directory(_STATIC_DIR, 'index.html')


def _build_asset_index(assets: list, project_name: str, kind: str) -> dict:
    """构建「资产名 → 本地图片绝对路径」索引

    优先使用前端传来的 HTTP 资源路径（解析回本地），其次按资产目录约定推导
    output/assets/<kind>/<项目>/<名称>/(front|base).png，最后补齐磁盘上已有但未上报的资产。
    """
    base_dir = _ASSET_DIRS[kind]
    names = []
    for a in (assets or []):
        if isinstance(a, dict) and a.get("name"):
            names.append((a.get("name"), a))
        elif isinstance(a, str) and a:
            names.append((a, {}))
    project_dir = os.path.join(base_dir, project_name)
    if os.path.isdir(project_dir):
        known = {n for n, _ in names}
        for d in sorted(os.listdir(project_dir)):
            if os.path.isdir(os.path.join(project_dir, d)) and d not in known:
                names.append((d, {}))

    index = {}
    for name, payload in names:
        if name in index:
            continue
        # B-14 P2-4：取图判据统一走 _first_existing_asset_image——
        # 不再只认 front/base 固定名，改为「扩展名白名单 + 第一张非空」。
        # front/base 的 http URL 仍由前端 resolve_local_path 传回本地路径；
        # 本地推导时按 (project_dir, name) 目录取第一张可用图。
        asset_dir_for_name = os.path.join(project_dir, name)
        local = _first_existing(
            comfyui_client.resolve_local_path(payload.get("front") or ""),
            comfyui_client.resolve_local_path(payload.get("base") or ""),
            _first_existing_asset_image(asset_dir_for_name),
        )
        entry = {"name": name, "image": local, "_dir": asset_dir_for_name,
                 "url": f"/api/assets/{kind}s/{project_name}/{name}/front.png"}
        # 2026-09-25 景别对档：把**逐视角**路径也挂进来，供 `_pick_char_view`
        # 按镜头景别取「半身档 / 全身档」。角色资产自 sheet_split 改造后是
        # front/left/right/back/half 五个独立文件（少任何一个都合法）。
        #   ⚠️ 键只在**文件真实存在**时写入（不写空串），否则 `payload.get(k) or ""`
        #      分辨不出「没这个档位」与「有这个档位但路径为空」。
        #   ⚠️ `_dir` 是 `_pick_char_view` 的兜底按需推导目录（而不是批量 stat），
        #      这样前端上报的 http URL 失效时仍能命中磁盘约定路径。
        # 2026-09-29：挂在集合里再并入**场景机位档**（front/left45/right45/top）——
        # 供 `_pick_scene_view` 按镜头机位取「同机位场景图」。两类档位名不重叠
        # （角色 front/left/right/back/half vs 场景 front/left45/right45/top），
        # 且键只在**文件真实存在**时写入，故混挂不会互相干扰、也不会给没有的档写空串。
        for _stem in tuple(ASSET_VIEW_STEMS) + tuple(SCENE_VIEW_KEYS):
            _p = _first_existing(
                comfyui_client.resolve_local_path(payload.get(_stem) or ""),
                os.path.join(asset_dir_for_name, f"{_stem}.png"),
                os.path.join(asset_dir_for_name, f"{_stem}.jpg"),
            )
            if _p:
                entry[_stem] = _p
        # ⭐ 2026-10-05 场景九宫格：把 9 机位独立档（wide/mid/near/detail_a/detail_b 等）
        #    与拼接总图 grid.png 一并挂进 entry，供 `_pick_scene_view` 的九宫格分支
        #    按 SCENE_GRID_FILENAME 直接取整图。键只在**文件真实存在**时写入（同上方
        #    逐档口径），避免给「没这个档」写空串。
        #    ⚠️ 2026-10-06 起生产链路拼完九宫格会**删掉**机位分档 png（只留 base + grid），
        #    所以这里的 9 档键实际大多不存在、不会写入 —— 保留它只为兼容「旧项目存量分档图」
        #    与关闭九宫格开关后的回滚路径，不影响新生产。
        if kind == "scene":
            for _stem in tuple(SCENE_GRID_VIEW_KEYS) + (SCENE_GRID_FILENAME,):
                if _stem in entry:
                    continue
                _p = _first_existing(
                    comfyui_client.resolve_local_path(payload.get(_stem) or ""),
                    os.path.join(asset_dir_for_name, f"{_stem}.png"),
                    os.path.join(asset_dir_for_name, f"{_stem}.jpg"),
                )
                if _p:
                    entry[_stem] = _p
        # 场景把**正面档别名到 base.png**：场景不单独产出 front.png（base 本身就是正面
        # 机位出图，见资产 worker 的 scene 分支），但下游 `_pick_scene_view` 的回退链
        # 要按档位名逐级取，别名能省掉「每个调用点各自特判 scene」的分支。
        if kind == "scene" and "front" not in entry and local:
            entry["front"] = local
        index[name] = entry
    return index


def _normalize_char_alias(name) -> str:
    """归一化角色名别名（S6）：剥离 _主角/_角色/_主/_人 后缀、去空白与《》。

    与 pipeline.probe_assets 的资产目录扫描口径对齐：资产目录名可能是
    "青玉_主角" 而剧本里写 "青玉"，或反之。这里只做「后缀剥离 + 去符号」，
    **不做模糊匹配**（避免把"阿青"误归到"青玉"）。
    """
    s = str(name or "").strip()
    s = s.replace("《", "").replace("》", "").replace(" ", "")
    for suf in ("_主角", "_角色", "_主", "_人"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


def _match_scene_name(loc, scene_idx) -> tuple:
    """把镜头写的场景名解析到 ``scene_idx`` 的键 → ``(key, level)``。

    三级降级（精确 → 归一化 → **唯一**子串），全部失败返回 ``(None, "")``。
    第 3 级必须唯一：若索引里「卧室」「卧室外」都能被子串命中，则放弃——
    **宁可不给图，也不给错图**。

    ⚠️ 2026-09-29：实现已移到 :mod:`asset_name_match`，此处为薄封装。
    """
    return asset_name_match.match(loc, scene_idx, "scene")


def _note_ref_warning(shot, msg: str) -> None:
    """把「参考图未命中」告警挂到镜头上（**累加，不覆盖**）。

    一个镜头可能同时缺场景图与物品图；直接赋值会把前一条覆盖掉，排障时只能
    看到最后一条。这里统一累加到 ``_ref_warnings``，同时保留 ``_ref_warning``
    （首条）以兼容既有读取方。
    """
    if not isinstance(shot, dict) or not msg:
        return
    lst = shot.get("_ref_warnings")
    if not isinstance(lst, list):
        lst = []
        shot["_ref_warnings"] = lst
    if msg not in lst:
        lst.append(msg)
    shot.setdefault("_ref_warning", msg)


def _resolve_scene_entry(shot: dict, scene_idx: dict,
                         where: str = "") -> tuple:
    """解析镜头所属场景 → ``(scene_name, entry)``；未命中时**显式告警**并记账。

    调用方只有在 ``entry is None`` 时才真正「没有场景锚点」。告警写进
    ``shot["_ref_warnings"]``（**累加**），能在任务结果里定位到具体哪一镜、
    写的什么名字。

    ⚠️ 只有「剧本声明了场景、且索引非空」时未命中才告警——两者皆空属于正常
    空镜脚本，不该制造噪音。
    """
    if not isinstance(shot, dict):
        return None, None
    loc = shot.get("location")
    name, level = _match_scene_name(loc, scene_idx)
    if not name:
        if loc and scene_idx:
            _msg = (f"场景名未匹配：镜头 {shot.get('shot_id')} 的 location={loc!r} "
                    f"在场景资产 {sorted(scene_idx.keys())[:8]} 中无对应项，"
                    f"该镜将不带场景参考图")
            _note_ref_warning(shot, _msg)
            logger.warning("[%s] %s", where or "scene-ref", _msg)
        return None, None
    if level != "exact":
        logger.info("[%s] 场景名模糊命中：%r → %r（%s）",
                        where or "scene-ref", loc, name, level)
        shot["_scene_match"] = {"queried": loc, "matched": name, "level": level}
    return name, (scene_idx.get(name) or {})


def _resolve_item_names(shot: dict, item_idx: dict, where: str = "") -> list:
    """解析镜头出场物品 → **规范名列表**（去重保序）；未命中显式告警。

    与 :func:`_resolve_scene_entry` / :func:`_match_shot_chars` 同口径：
    「宁可告警、不可静默」。旧实现是裸判据
    ``[n for n in items_in_shot if n in item_idx]`` —— 物品名只因全角括号、
    书名号、空格或「（断）」这类后缀差异对不上，该物品的设定图就**不注入、
    不打任何日志**，画面里道具形制只能靠模型猜（「道具走形」类质检缺陷的
    隐蔽来源）。
    """
    if not isinstance(shot, dict):
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(
        shot.get("items_in_shot"), item_idx, "item")
    for _q, _k, _lv in fuzzy:
        logger.info("[%s] 物品名模糊命中：%r → %r（%s）",
                        where or "item-ref", _q, _k, _lv)
    if missing:
        _msg = (f"物品名未匹配：镜头 {shot.get('shot_id')} 的 {missing} "
                f"在物品资产 {sorted(item_idx.keys())[:8]} 中无对应项，"
                f"该物品将不带参考图")
        _note_ref_warning(shot, _msg)
        logger.warning("[%s] %s", where or "item-ref", _msg)
    return resolved


def _match_shot_chars(shot: dict, char_idx: dict) -> list:
    """S6 修复：按镜头 characters_in_shot 匹配 char_idx，**禁止静默 take-first**。

    返回匹配到的角色名列表（保持 shot 原顺序、去重）；镜头一个角色都匹配不到 →
    返回 []，由调用方设 shot['_no_reference']=True / shot['_ref_error']=... 决定
    400 / 跳过。

    ⚠️ 2026-09-29 与物品／场景统一口径：
      · 匹配走 :mod:`asset_name_match` 三级降级（精确 → 归一化 → **唯一**子串），
        不再只做「精确 + 后缀别名」两档 —— 「方源。（少年）」这类写法以前会静默丢图；
      · **部分未命中也会告警**。旧实现只在「一个都没匹配上」时出声，而
        「镜头登记了 A、B 两人、只有 A 命中」时 B 被静默丢弃（B 的参考图不注入，
        用户与日志都看不出来），是「角色不像设定」的隐蔽来源。
    """
    if not isinstance(shot, dict):
        return []
    chars_in = [n for n in (shot.get("characters_in_shot") or []) if n]
    if not chars_in:
        return []
    resolved, missing, fuzzy = asset_name_match.resolve_names(
        chars_in, char_idx, "character")
    for _q, _k, _lv in fuzzy:
        logger.info("[角色参考图] 角色名模糊命中：%r → %r（%s）", _q, _k, _lv)
    if missing:
        _msg = (f"角色名未匹配：镜头 {shot.get('shot_id')} 的 {missing} "
                f"在角色资产 {sorted(char_idx.keys())[:8]} 中无对应项，将不带其参考图")
        _note_ref_warning(shot, _msg)
        logger.warning("[角色参考图] %s", _msg)
    return resolved


def _framing_wants_half_shot(shot: dict) -> bool:
    """A1：按**权威景别字段**判断本镜角色参考图是否取「半身档」。

    与 :func:`_framing_wants_half` 的差别：优先读 ``shot_type``（新剧本权威字段），不再靠
    解析「特写推入」这类复合串去猜；``shot_type`` 与 ``camera`` **都为空**时返回 False
    （全身档）—— 与原实现「空值返回 False」同口径，绝不把「景别未指定」误判成中景。
    """
    if not isinstance(shot, dict):
        return False
    st = str(shot.get("shot_type") or "").strip()
    if st:
        return st in _FRAMING_HALF_SHOT
    _raw = str(shot.get("camera") or "").strip()
    if not _raw:
        return False
    return _camera_key(_raw) in _FRAMING_HALF_SHOT


def _pick_char_view(char_payload: dict, want_half: bool, outfit_dir: str = "") -> str:
    """取角色参考图：**始终返回整张设定图 base.png**（2026-10-02 起不裁剪）。

    ⚠️⚠️ **2026-10-02 用户指定「角色图不用裁剪，给整个图片就行」** —— 本函数
    从「按景别对档挑 half/front/back」退化为「**只取整图 base.png**」。动机与配套：
      · 角色设定图已改为**英文四区 character sheet**（Top 三视图 / Left 面部+配色 /
        Bottom 细节 / Right 比例参照，见 `comfyui_client._CHARACTER_SHEET_EN_LAYOUT`），
        四区不对称布局**无法再做行列投影切分** → 单视角切分（front/left/back/half）
        失去产出依据；
      · 用户要「整图给模型自己取视角」，不再由我们按景别裁单视角。
    ⚠️ 取舍（风险已向用户说明）：取消「景别对档」会**重新引入画幅对抗**（全景/近景
    参考图同用一张整图，近景/特写的构图牵引变弱）——这是 2026-09-25 那套「半身档」
    要解决的问题。用户明确选择「不裁剪」，故此处按要求退化；`want_half`/`outfit_dir`
    参数**保留签名**（调用方不破），但不再影响取图结果（仅整图）。
    ⚠️ 回滚点：如需恢复景别对档，改回 order 的 half/front 分支即可。

    服装变体（衣柜，2026-10-02）：``outfit_dir`` 非空时**仍优先**在该变体目录里取
    整图（base.png），变体目录没有 / 异常 → 回落主设定图（fail-open，零回归）。
    """
    payload = char_payload or {}
    # 只取整图：base.png →（兜底）front.png（存量资产可能只有 front）
    order = ("base", "front")
    if outfit_dir:
        try:
            _od = comfyui_client.resolve_local_path(outfit_dir) or outfit_dir
            if os.path.isdir(_od):
                _hit = _first_existing(*[os.path.join(_od, f"{k}.png") for k in order])
                if _hit:
                    return _hit
        except Exception as _oe:  # noqa: BLE001  变体取图失败绝不拖垮参考图链
            logger.debug("服装变体取图失败（回落主设定图）：%s", _oe)
    cands = [comfyui_client.resolve_local_path(payload.get(k) or "") for k in order]
    # 资产目录约定路径兜底（前端未上报时）
    d = payload.get("_dir")
    if d and os.path.isdir(d):
        cands += [os.path.join(d, f"{k}.png") for k in order]
    return _first_existing(*cands) or ""


def _scene_view_for_shot(shot) -> str:
    """本镜**机位**该取哪个场景档（``front`` / ``left45`` / ``right45`` / ``top``）。

    与景别（`shot_type` / `camera` 里的特写/近景/全景）**正交**：景别决定取景范围，
    机位决定观察方向。映射表单一来源在 :data:`config.SCENE_ANGLE_TO_VIEW`，
    未列出的机位（含未指定即空串）一律回落到正面档。

    ⚠️ 机位只能从**复合串** `camera` 解析（`shot_type` 只登记景别，无角度信息）；
    `camera_motion` 是 A1 新增的运镜权威字段，老剧本为空、新剧本可能把「俯拍缓推」
    写在那里，故作为**第二来源**（不是第一条）：先正规字段，再权威字段，
    两边都没有才判「未指定」。
    """
    if not isinstance(shot, dict):
        return "front"
    ang = _camera_angle(str(shot.get("camera") or "")) or \
        _camera_angle(str(shot.get("camera_motion") or ""))
    return SCENE_ANGLE_TO_VIEW.get(ang, "front")


def _pick_scene_view(scene_payload: dict, shot) -> str:
    """按镜头机位从场景资产里挑一张**同机位**的场景参考图（2026-09-29 新增）。

    动机：场景此前只有一张基准图，俯拍 / 斜侧镜头拿到的都是正面图，构图先验与镜头
    要求**反向**（「要求俯拍却给了平视」既没被约束、也没被检出）。场景现在按机位档
    逐档出图（`config.SCENE_VIEW_KEYS`），这里负责把镜头机位映射到档位。

    ⚠️ 必须**逐级优雅降级**，绝不返回空（与 `_pick_char_view` 同口径）：返回空会让该镜
    判成「无场景锚点」，把「机位档缺失」这种**加值项缺失**升级成「镜头不能出图」。
    回退链：
      · 目标档 → front → base（该档没生成 / 是老项目没有机位档）
      · 目标档本身就是 front → front → base
    `base` 是每个场景都有的兜底（正面机位出图），故链尾一定落得住。

    ⚠️ 刻意**不做**「俯拍档缺失就退而取斜侧档」这类跨机位借用：俯视与仰拍是反向机位，
    拿俯视图当仰拍镜头的锚点会把构图拽反 —— 「误配比丢图更糟」（丢图只是没有锚点，
    误配是把错误机位焊进画面，且日志看不出来）。宁可回落正面档。

    ⚠️ 链尾必须是 ``entry["image"]``（2026-09-29 实修）：它是**长期存在的主图契约**，
    `_build_asset_index` 与历史生产者都填它，但**不一定**同时给 `front`/`base`/`_dir`。
    少了这一档，任何「只填 image」的场景索引都会静默丢场景锚点 —— 这不是假想：
    改完当场被 ``probe_scene_match.py`` 的端到端用例抓出（fixture 就是只填 image 的形态）。

    ⭐ 2026-10-05 场景九宫格（SCENE_GRID_MODE）：**彻底关机位对档、纯整图直接用**。
       网格模式下场景资产本体是 9 机位拼成的 ``grid.png``，下游**所有镜头机位**都喂
       这张整图（不再按机位选档）——这是用户 2026-10-05 三轮确认的选择。故函数体
       第一步就先查 ``grid``；查不到（老项目 / 关网格）才走下面原有的按机位逐级回退。
    """
    payload = scene_payload or {}
    # ⭐ 九宫格模式：优先整图（grid.png），不再按镜头机位选档（关机位对档）。
    #    用既有回退链工具保证「网格图缺失时平滑回落旧行为」，绝不返回空。
    if SCENE_GRID_MODE:
        _g = comfyui_client.resolve_local_path(payload.get(SCENE_GRID_FILENAME) or "")
        _gd = payload.get("_dir")
        _g2 = os.path.join(_gd, SCENE_GRID_FILENAME) if (_gd and os.path.isdir(_gd)) else ""
        _grid_pick = _first_existing(_g, _g2)
        if _grid_pick:
            return _grid_pick
        # 网格主图缺失（老项目未重跑 / 拼接失败）→ 继续走下方按机位回退，绝不静默丢图。
    target = _scene_view_for_shot(shot)
    order = []
    for k in (target, "front", "base"):
        if k and k not in order:
            order.append(k)
    cands = [comfyui_client.resolve_local_path(payload.get(k) or "") for k in order]
    # 资产目录约定路径兜底（前端未上报 / 索引未挂到该档时）
    d = payload.get("_dir")
    if d and os.path.isdir(d):
        cands += [os.path.join(d, f"{k}.png") for k in order]
    # 最后回落主图契约（老索引 / 外部生产者只填 image 的形态）
    cands.append(comfyui_client.resolve_local_path(payload.get("image") or ""))
    return _first_existing(*cands) or ""


def _h3_shot_ref_components(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                            character_refs: list = None,
                            main_char_img: list = None,
                            outfit_map: dict = None) -> list:
    """把一个分镜解析成**有序参考图组件**（不构建提示词、不决定槽位）。

    返回 ``[{"kind": "character"|"item"|"scene", "name": str,
             "path": 本地路径, "appearance": str}, ...]``，
    顺序恒为「角色 → 物品 → 场景」——与 ``comfyui_client._h3_picture_defs`` 里
    三段的排放顺序一致（提示词的 ``<Picture N>`` 就按这个序推下去）。

    ## 为什么要抽出来（2026-09-30，H3 Director 公共参考图）

    「公共参考图」的判据是「全段都在用、且用的是同一张图」，要在 worker 循环**之前**
    先把每个镜头的资产解析一遍才能求交集；而 ``_shot_segment`` 里原来那段解析是
    内联的。两处各写一份必然漂移（公共池按 A 口径选、段级按 B 口径挂 → 公共图挂错镜）。
    故收敛到本函数：公共池规划与段级挂图**共用同一份解析结果**，
    并且解析只做一次（``_match_shot_chars`` / ``_resolve_item_names`` /
    ``_resolve_scene_entry`` 都会往 ``shot["_ref_warnings"]`` 记账，
    重复调用会把同一句告警记两遍）。

    ⚠️ 解析口径必须是**逐镜对档**后的结果，不能退化成「资产的基准图」：
      · 角色按景别取半身/全身档（``_pick_char_view``，2026-09-25「景别对档」）；
      · 场景按机位取 front/left45/right45/top 档（``_pick_scene_view``，2026-09-29）。
    公共池要的是「同一张图」，所以上图**按路径**判交集——档位不同的同一资产
    天然不满足，这正是「公共锁定」不会把对档工作废掉的原因（见 h3_common_refs）。
    """
    if not isinstance(shot, dict):
        return []
    out: list = []

    # ---- 本镜角色：每人一张（按景别对档），去重保序 ----
    # outfit_map（2026-10-02 服装变体）：「角色名 → outfits/<key>/ 目录」的可选映射，
    # 由调用方解析本镜服装提示（shot.outfit / shot.character_outfits）后传入；
    # None（默认）/ 缺该角色 → 走主设定图，与旧行为逐字一致（视频 worker 调用点
    # 位于 6200 行后的区域、本轮不可改动，故暂以默认 None 接线，见最终报告）。
    _want_half = _framing_wants_half_shot(shot)
    _seen_paths: set = set()
    for _mc in (_match_shot_chars(shot, char_idx) or []):
        _entry = char_idx.get(_mc) or {}
        _od = ""
        if outfit_map:
            _od = str(outfit_map.get(_mc)
                      or outfit_map.get(_normalize_char_alias(_mc)) or "")
        _p = _pick_char_view(_entry, _want_half, _od)
        if not _p or _p in _seen_paths:
            continue
        _seen_paths.add(_p)
        out.append({"kind": "character", "name": _mc, "path": _p,
                    "common_key": f"char:{_mc}",
                    "appearance": _entry.get("appearance")
                    or _entry.get("description") or ""})

    # ---- 本镜物品（与角色图重复的丢弃，保持旧口径）----
    for _it in (_resolve_item_names(shot, item_idx, "H3视频段") or []):
        _entry = item_idx.get(_it) or {}
        _p = _entry.get("image")
        if not _p or _p in _seen_paths:
            continue
        _seen_paths.add(_p)
        out.append({"kind": "item", "name": _it, "path": _p,
                    "appearance": _entry.get("appearance")
                    or _entry.get("description") or ""})

    # ---- 本镜场景（按机位取档）----
    _loc, _scene_entry = _resolve_scene_entry(shot, scene_idx, "H3视频段")
    _scene_img = _pick_scene_view(_scene_entry, shot) if _scene_entry else None
    if _scene_img:
        out.append({"kind": "scene", "name": _loc or "本镜场景", "path": _scene_img,
                    "appearance": ""})

    # ---- 兜底：逐镜角色一张都没解析出来 → 退回全局主角锚点（保持原行为）----
    # ⚠️ 旧实现把整个 refs 直接替换成 `[sb] + main_char_img`，于是本镜的**物品/场景锚点
    #    连同「提示词已声明」一起被丢掉**（声明在、图不在 → 编号错位）。这里改成
    #    「主角锚点排到最前、其余组件保留」，发送的图只多不少、且每一张都有声明。
    # ⚠️ 旧实现是 `refs = [sb] + main_char_img` 但提示词只声明 1 个角色 —— 图比声明的多，
    #    多出来的那张「有图无声明」（模型拿到一张没说用途的图）。这里按**实际张数**
    #    逐个声明，把编号对齐。
    if not any(c["kind"] == "character" for c in out) and main_char_img:
        _ref0 = (character_refs or [{}])[0] if character_refs else {}
        _nm = (_ref0.get("name") if isinstance(_ref0, dict) else None) or "主角"
        _ap = (_ref0.get("appearance") or _ref0.get("description") or "") \
            if isinstance(_ref0, dict) else ""
        _fb = []
        for _p in list(main_char_img):
            if not _p or _p in _seen_paths:
                continue
            _seen_paths.add(_p)
        _fb.append({"kind": "character", "name": _nm, "path": _p,
                    "common_key": f"char:{_nm}",
                    "appearance": _ap})
        out = _fb + out      # 角色在前（与 _h3_picture_defs 的段序一致，且保序）
    return out


def _h3_plan_common_refs(shots: list, char_idx: dict, item_idx: dict, scene_idx: dict,
                         character_refs: list = None, main_char_img: list = None,
                         sb_map: dict = None, use_storyboard: bool = True,
                         project_name: str = ""):
    """按「全段都在用、且同一张图」挑出公共参考图（H3 Director 公共参数）。

    返回 ``(common, comps_map)``：
      · ``common``: 公共组件列表（顺序 = 第 1 镜的出现顺序，已按上限截断）；
      · ``comps_map``: ``{shot_id: [组件, ...]}`` —— 每镜的解析结果，
        供 ``_shot_segment`` 复用（避免解析两遍造成重复告警）。

    ⚠️ 关掉开关或只有 1 个镜头时返回 ``([], {})``：**完全走原路径**，零行为变更。
    单镜头（per_shot 重跑）没有「跨段公共」可言，硬开公共池只会把段级图挪到全局，
    收益为零还多一层 ``commonEnabled`` 耦合。

    ⚠️ **要求每个镜头都有分镜图**，否则整集公共化会被整体放弃（返回 ``([], {})``）：
    公共图由客户端写进 ``global.refs``，它被 merge 进**每一段**，而提示词侧的
    ``<Picture 1..K>`` 只有在「分镜图分支」（``sb_local`` 为真）才会被生成。
    只要有一镜走了无分镜图的兜底分支，它的 ``<Picture N>`` 就从 1 起重新数，
    与槽位（公共块从 0 起）整体错位 —— 那是**静默错图**，宁可不做这个特性。
    """
    if not H3_COMMON_REFS or len(shots or []) < 2:
        return [], {}
    _shots = [s for s in shots if isinstance(s, dict)]
    # 逐镜解析结果按 shot_id 缓存复用（避免解析两遍），故 shot_id 必须唯一且非空 ——
    # 一旦重复/缺失（历史剧本偶有），缓存会互相覆盖 → 公共池按 A 镜选、B 镜挂错图。
    _ids = [str(s.get("shot_id") or "") for s in _shots]
    if "" in _ids or len(set(_ids)) != len(_ids):
        logger.warning(
            "[H3公共参考图] 镜号缺失或重复（%s…）→ 放弃公共化（逐镜缓存需要唯一镜号）",
            [x or "<空>" for x in _ids[:8]])
        return [], {}
    if sb_map is not None:
        _missing_sb = [s.get("shot_id") for s in _shots
                       if not (use_storyboard
                               and sb_map.get(_norm_shot_key(s.get("shot_id"))))]
        if _missing_sb:
            logger.info(
                "[H3公共参考图] %d 个镜头没有分镜图（%s…）→ 放弃公共化"
                "（公共图会被 merge 进每一段，缺分镜图的段编号会整体错位）",
                len(_missing_sb), _missing_sb[:5])
            return [], {}
    comps_map = {}
    per_shot_assets = []
    for _s in _shots:
        _sid = str(_s.get("shot_id") or "")
        _comps = _h3_shot_ref_components(_s, char_idx, item_idx, scene_idx,
                                         character_refs, main_char_img)
        comps_map[_sid] = _comps
        per_shot_assets.append(_comps)
    common = h3_common_refs.plan_common_refs(
        per_shot_assets, max_common=H3_COMMON_REFS_MAX,
        limit=h3_director_builder.MAX_REFERENCE_IMAGES)
    # ⭐ 全局风格参考图（2026-10-03）：项目 config.json 的 style_ref_image 作为最后一张公共图。
    if project_name:
        try:
            _proj_root = _safe_project(project_name)
            if _proj_root:
                # 修复（2026-10-05）：_safe_project 只返回项目键，config.json 必须拼在
                # 项目工作区**绝对路径**下（output/projects/<键>/config.json），
                # 否则相对路径基于 CWD、必然读不到（style_ref_image 相对写法同理）。
                _proj_dir = project_store.paths(_proj_root)["root"]
                _cfg = json.load(open(os.path.join(_proj_dir, 'config.json'), encoding='utf-8'))
                _style_ref = str(_cfg.get('style_ref_image') or '').strip()
                if _style_ref:
                    _style_ref_path = _style_ref if os.path.isabs(_style_ref) else os.path.join(_proj_dir, _style_ref)
                    if os.path.isfile(_style_ref_path) and len(common) < h3_director_builder.MAX_REFERENCE_IMAGES:
                        common = list(common) + [{'kind':'scene','name':'全局风格参考','appearance':'global style / lighting anchor','path':_style_ref_path}]
                        logger.info('[H3公共参考图] 追加全局风格参考图：%s', os.path.basename(_style_ref_path))
        except Exception as _e:  # noqa: BLE001
            logger.debug('[H3公共参考图] 风格参考图解析失败（忽略）：%s', _e)
    if common:
        logger.info(
            "[H3公共参考图] %d 镜中 %d 项全段共用 → 走 global.refs + commonEnabled：%s",
            len(per_shot_assets), len(common), h3_common_refs.describe(common))
    else:
        logger.info(
            "[H3公共参考图] %d 镜无「全段都在用且同一张图」的资产 → 维持逐段 refs",
            len(per_shot_assets))
    return common, comps_map


def _h3_is_common_comp(comp: dict, common_keys: set) -> bool:
    """该组件是否属于公共池（身份键 = 种类 + 名称 + 图片路径，与 h3_common_refs 同源）。"""
    return h3_common_refs.asset_key(comp) in common_keys


def _h3_common_ref_audios(common: list, project_name: str, all_names=None) -> list:
    """从公共池角色收集 voice_bank 参考音色（2026-10-02 公共参考音色）。

    只对 ``kind == character`` 的公共项，取其在 voice_bank 里已登记的参考音频
    （``find_voice_bank_ref``，缺则跳过＝该角色无绑定音色，绝不误挂别的声音）。
    返回**有序**的 ``(角色名, 本地绝对路径)`` 列表，供 ComfyUI 侧写进
    ``global.refAudios``（index 0..M-1）**并**在公共提示词里做**逐行角色归属**
    （``<Audio N> = 角色名``）。
    无公共角色 / 无音色库 / 全部缺音色 → 返回 ``[]``（零行为变更，走原 generate 无参考）。

    ⭐ 2026-10-05：从「纯路径列表」改为 ``(角色名, 路径)`` 有序对 —— 因为本函数会
    **跳过无音色的角色**，下游靠公共池顺序反推「哪个 <Audio> 属于谁」会错位；
    携带角色名才能逐行准确归属。下游两处消费（build 侧取 [1]、subject_lock 侧取 [0]）
    需同步。
    """
    _char_names = [str(c.get("name") or "").strip()
                   for c in (common or [])
                   if isinstance(c, dict) and c.get("kind") == "character"
                   and str(c.get("name") or "").strip()]
    # ⭐ 2026-10-10（用户方案②：**音色扩到全角色，参考图维持现状**）：
    #    「公共池」的判定是「全段共用」，于是只在部分镜出现的角色（老周/船长）
    #    拿不到自己的参考音色 —— 实测 voice_bank 里有 3 个角色，只有林昭挂上了。
    #    音频槽上限 3（ref_audio_0..2），而参考图槽上限 9 本就紧张，
    #    故这里只把**音色**补到全角色，参考图仍维持「全段共用才进公共池」。
    #    公共池里已有的角色优先，其余角色按传入顺序追加（超出 3 个由 _ref_audio_items 截断）。
    if all_names:
        _extra = (list(all_names.keys()) if isinstance(all_names, dict)
                  else list(all_names))
        for _n in _extra:
            _n = str(_n or "").strip()
            if _n and _n not in _char_names:
                _char_names.append(_n)
    if not _char_names:
        return []
    try:
        _dub_dir = _dub_project_dir(project_name)
    except Exception:  # noqa: BLE001
        return []
    _out: list = []
    _seen = set()
    for _cn in _char_names:
        try:
            _ref, _ = find_voice_bank_ref(_dub_dir, _cn)
        except Exception:  # noqa: BLE001
            _ref = ""
        if not _ref or not os.path.isfile(_ref):
            logger.info("[H3公共参考音色] 角色 %s 未绑定参考音色，跳过（不挂声）", _cn)
            continue
        _key = os.path.normcase(os.path.normpath(os.path.abspath(_ref)))
        if _key in _seen:
            continue
        _seen.add(_key)
        _out.append((_cn, _ref))
    if _out:
        logger.info("[H3公共参考音色] 公共角色音色 %d 支 → global.refAudios：%s",
                        len(_out), "、".join(n for n, _p in _out))
    return _out


def _h3_audio_defs_for(common: list, project_name: str, characters=None) -> list:
    """把公共角色参考音色转成 :func:`h3_prompt_kit.build_ref2va` 的 ``audio_defs``。

    ⭐ 2026-10-09 官方 Ref2VA 对齐（MiniMax-H3 技能 references/ref-en.txt）：音色参考
    必须在六段里**显式声明**，四个位置都要出现 ``<Audio N>`` ——
      subject_definitions: ``<Audio 1> is the voice-timbre reference for <Subject 1> (S1)``
      summary:             ``[reference generation + audio reference]``
      retention_analysis:  ``<Audio 1>: reference - its vocal timbre guides ...``
      detailed_description:``... using the voice timbre referenced from <Audio 1> ...``
    此前我们只把音频文件写进 ``global.refAudios``，提示词里仅有一句非官方的
    ``<Audio 1> = 角色名``，H3 拿到音频却不知道用途 → 自己编嗓子（中英混杂 / 多说话人）。
    """
    try:
        # 与写进 global.refAudios 的那一份**必须同源同序**（用户方案②）：_
        # 否则提示词声明的 <Audio N> 与真实挂载的音频条数/顺序不一致，
        # H3 会把音色归错人。characters 在这里既是音色补齐来源，也是 voice_style 来源。
        _pairs = _h3_common_ref_audios(common, project_name, all_names=characters)
    except Exception as _e:  # noqa: BLE001
        logger.warning("[H3音色声明] 收集参考音色失败（按无声处理）：%s", _e)
        return []
    if not _pairs:
        return []
    _vs_map = {}
    for _c in (characters or []):
        if isinstance(_c, dict) and _c.get("name"):
            _vs_map[str(_c["name"]).strip()] = str(_c.get("voice_style") or "").strip()
    _out = []
    for _i, _item in enumerate(_pairs, start=1):
        _nm = str((_item[0] if isinstance(_item, (list, tuple)) else _item) or "").strip()
        _vs = _vs_map.get(_nm) or ""
        # ⚠️ 官方要求六段正文用英文；中文 voice_style 不直接塞进去（会诱导中文发声），
        #    非 ASCII 时留空，由 h3_prompt_kit 的性别兜底短语接管。
        _voice = _vs if (_vs and _vs.isascii()) else ""
        _out.append({"name": _nm, "label": f"<Audio {_i}>", "speaker": f"S{_i}",
                     "voice": _voice})
    logger.info("[H3音色声明] 生成 %d 条 <Audio N> 声明：%s", len(_out),
                    "、".join(d["name"] + d["label"] for d in _out))
    return _out


def _h3_common_subject_lock(common: list, common_ref_audios: list,
                        style: str = "", worldview: str = "", aspect: str = "") -> str:
    """公共提示词 subject lock（2026-10-02）：角色/物品/场景锁定句 + 公共音色指代。

    编号与公共槽位**逐位对齐**：公共图 index 0..K-1 → ``<Picture 1..K>``（1-based）；
    公共音色 index 0..M-1 → ``<Audio 1..M>``。插件在 commonEnabled 时把本句拼在**每段
    提示词之前**（plan.py:concat_common_segment_prompt），作为全集一致的主体锁定前缀，
    段级提示词只管本镜叙事，主体外观/音色由这句统一锁定（不再逐镜重述）。

    ⚠️ 提示词消费的是 ``<Picture N>``/``<Audio N>``（H3 模型认的标签）；``@image#N``
    只是 ComfyUI 前端 @ 选择器的 UI chip，不进模型 —— 故这里用 ``<>`` 标签。
    无公共项且无公共音色 → 返回 ``""``（零行为变更，global.prompt 留空）。
    """
    _parts: list = []
    _K = len(common or [])
    if _K:
        _lock = ["Subject lock — keep these shared references consistent across every shot:"]
        for _i, _c in enumerate(common or []):
            _num = _i + 1
            _name = str(_c.get("name") or "").strip()
            _kind = _c.get("kind")
            _app = str(_c.get("appearance") or _c.get("description") or "").strip()
            _ap = (f", {_app[:80]}" if _app else "")
            if _kind == "character":
                _lock.append(
                    f"<Picture {_num}> is {_name}{_ap} — keep this character's facial "
                    "identity, hairstyle, build, outfit and rendering style identical in all shots.")
            elif _kind == "item":
                _lock.append(
                    f"<Picture {_num}> is {_name}{_ap} — keep this prop's shape, material "
                    "and colors identical in all shots.")
            else:
                _lock.append(
                    f"<Picture {_num}> is the scene {_name} — keep its spatial layout and "
                    "lighting anchor; framing follows the per-shot camera, not this image.")
        _parts.append(" ".join(_lock))
    _M = len(common_ref_audios or [])
    if _M:
        # ⭐ 2026-10-05：音色参考音频**每个单独一行、更显眼、带角色名归属**。
        # common_ref_audios 现为 ``[(角色名, 路径)]`` 有序对（见 _h3_common_ref_audios）；
        # index 0..M-1 → <Audio 1..M>，逐行写「<Audio N> = 角色名」，模型不再猜哪支音色是谁。
        _voice_lines = []
        for _i, _item in enumerate(common_ref_audios or []):
            # 兼容：正常是 (名, 路径) 元组；万一上游仍传纯路径字符串，取不到名字就用「角色_{i+1}」。
            if isinstance(_item, (list, tuple)) and len(_item) >= 1:
                _vn = str(_item[0] or "").strip() or f"角色{_i + 1}"
            else:
                _vn = f"角色{_i + 1}"
            _voice_lines.append(f"<Audio {_i + 1}> = {_vn}")
        _parts.append(
            "VOICE TIMBRE REFERENCE — 本集角色参考音色（贯穿全片共用，逐行对应）:\n"
            + "\n".join(_voice_lines)
            + ". Each reference voice drives that character's spoken lines across every "
              "shot; keep the timbre of each character consistent with its reference "
              "audio (do not switch between voices for the same character).")
    
    # ---- 公共提示词补全：世界观 + 全局 STYLE（2026-10-03）----
    # 按「推荐放公共参数的内容」表：公共提示词 = 角色定义(上面 subject lock) +
    # 世界观设定 + 全局 STYLE（画风 / 比例 / 无字幕 / 稳定构图）。这些会被插件拼在
    # **每段提示词之前**（concat_common_segment_prompt），保证全片一致；镜头特有的
    # 动作/构图/景别/运镜/光线只写在组内，不进公共段。
    _w = str(worldview or "").strip()
    if _w:
        _parts.append(f"WORLD (world setting, holds for every shot): {_w}.")
    _st = str(style or "").strip()
    if _st:
        _aspect = ("/" + str(aspect or "").strip()) if str(aspect or "").strip() else ""
        _parts.append(
            f"GLOBAL STYLE (holds for every shot): {_st}{_aspect} vertical cinematic, "
            "no text, no subtitles, stable composition, consistent lighting and "
            "colour grading.")
    return "\n".join(_parts)

