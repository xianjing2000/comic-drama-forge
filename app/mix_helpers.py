# -*- coding: utf-8 -*-
'''混音助手（2026-10-11 从 app.py 下沉）。'''

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
from dub_mix import (
    DubMixError, ffmpeg_available as mix_ffmpeg_check, shot_timeline,
    build_entries, mix_video_with_entries, write_mix_report, mix_out_dir,
    probe_audio_info as mix_probe_audio,
)
from dub_helpers import (_dub_character_desc, _dub_line_speaker_from_script,  # noqa: F401, E402
                        _dub_resolve_script)
from shared_ai import _ai_gate_or_400  # noqa: F401  再导出
from shared_base import _trash_move  # noqa: F401  再导出
from shared_project import _project_or_400, _safe_project, _shot_seq  # noqa: F401  再导出
from shared_web import _serve_safe  # noqa: F401  再导出
from shared_qc import _qc_gate, _qc_record, _qc_record_verdict  # noqa: F401  再导出
from shared_tasks import _TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES, _prune_task_registry  # noqa: F401  再导出
from shared_upscale import COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES, _comfy_view_url, _upscale_resolve_comfyview, _upscale_resolve_video, _upscale_url_for_path, upscale_lock, upscale_tasks  # noqa: F401  再导出
from routes._shared import _apply_project_settings, _project_style  # noqa: F401  再导出
import json
import os
from upscale_client import (
    VideoUpscaler, UpscaleError, check_environment as upscale_env_check,
    probe_video as probe_video_info
)
import project_store

logger = logging.getLogger(__name__)

def _mix_resolve_video(data: dict, project_name: str) -> str:
    """定位待合成的成片：显式 video_path/video_url 优先，否则在项目成片目录自动匹配最新 mp4"""
    if (data.get("video_path") or "").strip() or (data.get("video_url") or "").strip():
        return _upscale_resolve_video(data)
    cands = []
    for d in project_store.project_dirs(FINAL_DIR, project_name):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".mp4"):
                p = os.path.join(d, name)
                cands.append((os.path.getmtime(p), p))
    if not cands:
        raise DubMixError(
            "未找到成片视频：请先在步骤6完成成片合成，或显式提供 video_path / video_url")
    cands.sort(reverse=True)
    return cands[0][1]


def _mix_segments_dir(project_name: str, episode: int = 0) -> str:
    """定位该集（episode 给定）或该项目的镜头分段视频目录（用于按真实分段时长对齐时间轴）

    B-10 P1-6：带集号过滤。第 2 集起不再取到第 1 集素材，避免时间轴/成片源系统性错配。
    优先匹配该集专属目录（``<key>_第N集`` 或 ``epNN`` 子目录），找不到再回退到项目级目录。
    """
    ep_tag = f"ep{int(episode):02d}" if episode else ""
    best, best_key = "", (-1, 0)
    # 优先找该集专属目录（第 2 集起视频通常落在 <项目键>_第N集/ 或 epNN/ 子目录）
    ep_dir = ""
    if episode:
        for cand in (os.path.join(VIDEOS_DIR, project_name, ep_tag),
                     os.path.join(VIDEOS_DIR, f"{project_name}_第{episode}集")):
            if os.path.isdir(cand):
                ep_dir = cand
                break
    if ep_dir:
        # 该集目录直接采用
        vids = [f for f in os.listdir(ep_dir) if f.lower().endswith(".mp4")]
        if vids:
            return ep_dir
    # 回退：项目级目录（第 1 集或整集模式）
    for d in project_store.project_dirs(VIDEOS_DIR, project_name):
        if not os.path.isdir(d):
            continue
        vids = [f for f in os.listdir(d) if f.lower().endswith(".mp4")]
        if not vids:
            continue
        key = (len(vids), max(os.path.getmtime(os.path.join(d, f)) for f in vids))
        if key > best_key:
            best, best_key = d, key
    return best


def _mix_manifest(project_name: str, episode: int = 0) -> dict:
    """读取配音清单（优先指定集数，其次最新）"""
    out_dir = os.path.join(DUB_DIR, project_name)
    if not os.path.isdir(out_dir):
        raise DubMixError(f"尚未生成配音（目录不存在）：{out_dir}")
    if episode:
        p = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return {"manifest": json.load(f), "path": p}
    cands = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.endswith("_dub_manifest.json")]
    if not cands:
        raise DubMixError("未找到配音清单（*_dub_manifest.json），请先完成配音合成")
    cands.sort(key=os.path.getmtime, reverse=True)
    p = cands[0]
    with open(p, "r", encoding="utf-8") as f:
        return {"manifest": json.load(f), "path": p}


def _mix_prepare(data: dict) -> dict:
    """公共准备：解析项目 / 视频 / 剧本 / 配音清单 / 时间轴 / 逐句条目（不合成）"""
    # P2-T2：mix 的 project 解析唯一事实源在 _mix_prepare（被 /mix/plan 与 /mix/generate 共用）。
    # 缺省/越界 project_name → 抛 DubMixError（两条路由均已 catch 并回 400），
    # 不再静默回落共享 'project' 命名空间造成串项目。前端契约必填。
    project_name, _mix_err = _project_or_400((data.get('project_name') or '').strip())
    if _mix_err is not None:
        raise DubMixError("缺少 project_name")
    video_path = _mix_resolve_video(data, project_name)

    resolved = _dub_resolve_script(dict(data, project_name=project_name))
    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 0)

    mf = _mix_manifest(project_name, episode)
    manifest = mf["manifest"]
    episode = episode or int(manifest.get("episode") or 1)

    seg_dir = _mix_segments_dir(project_name, episode)
    timeline = shot_timeline(script, videos_dir=seg_dir)

    params = dict(MIX_DEFAULT_PARAMS)
    params.update(data.get('params') or {})
    mode = (data.get('mode') or params.get("mode") or "timeline").strip()

    lines = [ln for ln in (manifest.get("lines") or []) if ln.get("ok") and ln.get("out_path")]
    if mode == "concat":
        merged = manifest.get("merged_audio") or ""
        if not merged or not os.path.exists(merged):
            raise DubMixError("concat 模式需要整集合并音轨，但配音清单中没有有效 merged_audio")
        entries = [{"line_id": "merged", "shot_id": None, "character": "",
                    "text": "", "audio_path": os.path.abspath(merged),
                    "audio_dur": float((manifest.get("merged_info") or {}).get("duration") or 0),
                    "start": 0.0, "fit_ratio": 1.0}]
        warnings = ["concat 模式：整集音轨从 0 秒顺次铺设，不做逐镜头对齐"]
    else:
        built = build_entries(lines, timeline, params, mode=mode)
        entries, warnings = built["entries"], list(built["warnings"])
    if not entries:
        raise DubMixError("没有可用的配音音频：请先完成配音合成，或检查配音文件是否存在")

    # ---- 音效轨：H3 原生音效经人声分离后垫底（2026-09-17 新增）----
    # 为什么需要：H3 是音视频联合模型，原音轨里既有打斗/雨声等音效，也有它自己生成的
    # 说话声。直接保留原音轨会让两套人声重叠；完全丢弃又会让成片没有任何音效。
    # 折中：用 sfx_isolate 分离出「纯音效」，作为独立条目按同一条时间轴垫底。
    sfx_entries = []
    if H3_SFX_ISOLATE and mode != "concat":
        try:
            import sfx_isolate
            sfx_entries = sfx_isolate.build_sfx_entries(
                project_name, episode, timeline,
                volume=float(params.get("original_audio_volume") or 0.3))
        except Exception as e:                                  # noqa: BLE001
            warnings.append(f"音效轨装配失败（本集跳过音效）：{type(e).__name__}: {e}")
    if sfx_entries:
        entries = list(entries) + sfx_entries
        # 已用「分离后的纯音效」→ 关掉视频原音轨，否则人声会回来、音效也会叠双份
        params["keep_original_audio"] = False
        warnings.append(f"已叠加 {len(sfx_entries)} 条镜头音效（H3 音轨已做人声分离，"
                        f"垫底音量 {params.get('original_audio_volume')}）")
    elif H3_SFX_ISOLATE and params.get("keep_original_audio"):
        warnings.append("未找到可用的分离音效轨，将直接使用视频原音轨垫底"
                        "（其中可能含 H3 生成的说话声）")

    vinfo = probe_video_info(video_path)
    if vinfo.get("duration") and entries[-1].get("end", 0) > float(vinfo["duration"]) + 0.5:
        warnings.append(
            f"末句结束 {entries[-1].get('end')}s 超出视频时长 {vinfo.get('duration')}s，超出部分会被截断")

    return {
        "project_name": project_name, "video_path": video_path, "video_info": vinfo,
        "script_path": resolved["script_path"], "script_source": resolved["source"],
        "episode": episode, "manifest_path": mf["path"], "manifest": manifest,
        "segments_dir": seg_dir, "timeline": timeline,
        "entries": entries, "warnings": warnings, "mode": mode, "params": params,
    }

