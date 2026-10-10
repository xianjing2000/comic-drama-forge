# -*- coding: utf-8 -*-
"""外部 NLE 导出（P2-1 / P2-2）

为什么需要
----------
开源项目（ArcReel / CineGen）普遍把「能导进剪映 / Premiere 继续精修」当成核心卖点：
AI 成片是起点而不是终点，专业创作者需要在自己的时间线里微调节奏、换配乐、加特效。
本模块补齐这一出口。

支持的导出格式
--------------
① **剪映草稿**（P2-1）：生成 draft_content.json + draft_meta_info.json，
   把视频轨（逐镜片段，素材自带原生音轨）、字幕轨（台词）一次性铺好，
   音轨仅在显式传入 audio 时挂载（tts 整集配音已下线），
   导入剪映后可直接在轨道上继续编辑。
   ⚠ 剪映的草稿格式为私有格式且随版本演进，本实现按通用字段构造；
     若某版本无法识别，主要需调整 draft_content.json 的 `version` 字段。

② **FCPXML**（P2-2）：Premiere Pro / Final Cut Pro 可直接导入的 XML，
   逐镜作为独立 clip 放在视频轨（视频自带音轨随 clip 走；
   仅显式传入 audio 时另挂音频轨）。

③ **SRT 字幕**：通用字幕文件，任何播放器/剪辑软件都能挂载。

④ **帧序列清单**（P2-2）：列出每镜已产出的关键帧图片路径与时长，
   便于导入 AE/PR 做二次合成。

设计约束
--------
- 只读既有产物（剧本 + 视频片段 + 显式传入的音频），不修改任何原始文件；
- 导出结果统一落在 output/export/<项目>/；
- 所有时间单位内部用秒，输出时按各格式要求换算（剪映用微秒）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import xml.sax.saxutils as saxutils
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ⭐⭐ 2026-10-10 用户指出「一个值散在多处、改一处漏四处」后查出的**真 bug**：
#   本模块原先用 __file__ 上级自算 _ROOT_DIR，再拼 output/{export,videos,dub,storyboards}。
#   而 _ROOT_DIR 是**代码所在目录**，不是数据根 —— 一旦设置 MJSCXT_DATA_DIR（桌面版
#   就会设，指向 %APPDATA%/mjscxt-desktop/mjscxt-data），生产产物落在数据根，
#   这里却仍去代码目录找 → NLE 导出**永远找不到视频/分镜/配音**。
#   现统一从 config 取（跟随 PROJECT_DATA_DIR），与其余产物目录同一事实源。
#   同时删除 _ROOT_DIR —— 它只服务于这几个路径，留着就是第二个事实源。
from config import DUB_DIR, EXPORT_DIR, STORYBOARDS_DIR, VIDEOS_DIR  # noqa: F401

# 剪映工程 FPS / 画布（竖屏漫剧默认；H3 成片 24fps）
JY_FPS = 24
JY_CANVAS = {"width": 1080, "height": 1920}
# 剪映草稿版本号：不同剪映版本识别的版本号不同，无法识别时优先调这里
JY_VERSION = 360000


# ===================== 通用工具 =====================

def _out_dir(project: str, sub: str = "") -> str:
    d = os.path.join(EXPORT_DIR, project, sub) if sub else os.path.join(EXPORT_DIR, project)
    os.makedirs(d, exist_ok=True)
    return d


def _sec_to_us(sec: float) -> int:
    """秒 → 微秒（剪映时间单位）"""
    return int(round(max(0.0, float(sec)) * 1_000_000))


def _safe_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "_", str(name or "")).strip() or "unnamed"


def build_timeline(script: dict, project: str, videos: list = None,
                   use_real_duration: bool = True, episode: int = None) -> dict:
    """构建逐镜时间轴

    返回 {"shots":[{shot_id,index,start,duration,end,video,text,speaker}], "total_sec": float}
    时长优先取视频真实时长（ffprobe），缺失时退回剧本 duration。
    B-17 P2-13：episode 参数可选；提供时按集号过滤视频目录，避免跨集混用素材。
    """
    shots = (script or {}).get("shots") or []
    vids = videos if videos is not None else _scan_videos(project, script, episode=episode)

    rows, t = [], 0.0
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        vid = vids[i] if i < len(vids) else ""
        dur = None
        if use_real_duration and vid and os.path.isfile(vid):
            dur = _probe_duration(vid)
        if not dur:
            try:
                dur = float(shot.get("duration") or 0) or 5.0
            except (TypeError, ValueError):
                dur = 5.0
        dur = round(float(dur), 3)

        dlg = shot.get("dialogue")
        text, speaker = "", ""
        if isinstance(dlg, list) and dlg:
            first = dlg[0]
            if isinstance(first, dict):
                text = str(first.get("text") or "")
                speaker = str(first.get("speaker") or "")
            elif isinstance(first, str):
                text = first
        elif isinstance(dlg, str):
            text = dlg
        if not text:
            text = str(shot.get("dialogue_text") or "")

        rows.append({
            "shot_id": shot.get("shot_id", i + 1),
            "index": i,
            "start": round(t, 3),
            "duration": dur,
            "end": round(t + dur, 3),
            "video": vid,
            "text": text,
            "speaker": speaker,
            "camera": shot.get("camera") or "",
            "description": shot.get("description") or "",
        })
        t += dur
    return {"shots": rows, "total_sec": round(t, 3)}


def _scan_videos(project: str, script: dict = None, episode: int = None) -> list:
    """扫描项目视频片段，优先按剧本镜头数顺序匹配 shot_NN.mp4
    B-17 P2-13：episode 参数可选；提供时优先匹配该集专属目录
    （``<key>_第N集`` 或 ``epNN`` 子目录），找不到再回退到项目级目录。
    """
    d = os.path.join(VIDEOS_DIR, project)
    if not os.path.isdir(d):
        return []
    # 集号专属目录优先
    if episode:
        for ep_tag in (f"ep{int(episode):02d}", f"第{int(episode)}集"):
            ep_dir = os.path.join(VIDEOS_DIR, project, ep_tag)
            if os.path.isdir(ep_dir):
                files = sorted(f for f in os.listdir(ep_dir)
                               if f.lower().endswith((".mp4", ".mov", ".mkv")))
                if files:
                    ordered = []
                    numbered = sorted((f for f in files if re.match(r"^shot_(\d+)", f)),
                                      key=lambda f: int(re.search(r"\d+", f).group()))
                    ordered.extend(numbered)
                    ordered.extend(f for f in files if f not in ordered and not f.endswith("_full.mp4"))
                    return [os.path.join(ep_dir, f) for f in ordered]
    # 回退到项目级目录（兼容旧布局）
    files = sorted(f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv")))
    fulls = [f for f in files if f.endswith("_full.mp4")]
    if fulls and not [f for f in files if re.match(r"^shot_\d+", f)]:
        return [os.path.join(d, fulls[0])]
    ordered = []
    numbered = sorted((f for f in files if re.match(r"^shot_(\d+)", f)),
                      key=lambda f: int(re.search(r"\d+", f).group()))
    ordered.extend(numbered)
    ordered.extend(f for f in files if f not in ordered and not f.endswith("_full.mp4"))
    return [os.path.join(d, f) for f in ordered]


def _probe_duration(path: str) -> Optional[float]:
    """ffprobe 取视频时长（失败返回 None）"""
    try:
        import video_probe
        return float((video_probe.probe_video(path) or {}).get("duration") or 0) or None
    except Exception:  # noqa: BLE001
        try:
            import subprocess
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            return float(out.stdout.strip()) or None
        except Exception:  # noqa: BLE001
            return None


# ===================== ① 剪映草稿（P2-1） =====================

def _jy_id(prefix: str, n: int, salt: str = "") -> str:
    """剪映内部 ID：字母+数字+短哈希，稳定且唯一"""
    return f"{prefix}-{n:04d}-{abs(hash((prefix, n, salt))) % 0xFFFFFF:06X}"


def export_jianying(project: str, script: dict, videos: list = None,
                    audio: str = None, timeline: dict = None,
                    draft_name: str = None, episode: int = None) -> dict:
    """导出剪映草稿（draft_content.json + draft_meta_info.json + 素材引用）

    轨道布局：
      视频轨：逐镜片段顺序排列（素材自带原生音轨）
      音频轨：仅显式传入 audio 参数时挂载（旧 output/dub 整集配音轨已停止产出）
      字幕轨：每镜台词（若存在）
    B-17 P2-13：episode 参数可选；提供时按集号过滤视频目录。
    """
    tl = timeline or build_timeline(script, project, videos, episode=episode)
    rows = tl.get("shots") or []
    if not rows:
        return {"ok": False, "error": "没有可导出的镜头（剧本为空或无视频片段）"}

    name = _safe_filename(draft_name or f"{project}_draft")
    draft_dir = _out_dir(project, f"jianying/{name}")
    total_us = _sec_to_us(tl.get("total_sec") or 0)

    materials_videos, video_segments = [], []
    for i, r in enumerate(rows):
        mid = _jy_id("mat-v", i, project)
        mats = {
            "id": mid, "type": "video",
            "path": os.path.abspath(r["video"]) if r["video"] else "",
            "material_name": os.path.basename(r["video"]) if r["video"] else f"shot_{i+1}",
            "duration": _sec_to_us(r["duration"]),
            "width": JY_CANVAS["width"], "height": JY_CANVAS["height"],
            # 2026-10-05 口径：H3 成片自带原生音轨（成片即带配音），素材按有音轨声明
            "has_audio": True,
        }
        materials_videos.append(mats)
        video_segments.append({
            "id": _jy_id("seg-v", i, project),
            "material_id": mid,
            "target_timerange": {"start": _sec_to_us(r["start"]),
                                 "duration": _sec_to_us(r["duration"])},
            "source_timerange": {"start": 0, "duration": _sec_to_us(r["duration"])},
            "speed": 1.0, "volume": 1.0,
            "visible": True,
            "clip": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
                     "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
                     "transform": {"x": 0.0, "y": 0.0}},
        })

    # 音频轨（仅显式传入 audio 时挂载；旧 output/dub 整集配音轨已停止产出）
    materials_audios, audio_segments = [], []
    audio_path = audio or ""
    if audio_path and os.path.isfile(audio_path):
        aid = _jy_id("mat-a", 0, project)
        aud_dur = _probe_duration(audio_path) or tl.get("total_sec") or 0
        materials_audios.append({
            "id": aid, "type": "audio",
            "path": os.path.abspath(audio_path),
            "name": os.path.basename(audio_path),
            "duration": _sec_to_us(aud_dur),
        })
        audio_segments.append({
            "id": _jy_id("seg-a", 0, project),
            "material_id": aid,
            "target_timerange": {"start": 0, "duration": _sec_to_us(min(aud_dur, tl.get("total_sec") or aud_dur))},
            "source_timerange": {"start": 0, "duration": _sec_to_us(min(aud_dur, tl.get("total_sec") or aud_dur))},
            "volume": 1.0,
        })

    # 字幕轨
    materials_texts, text_segments = [], []
    ti = 0
    for r in rows:
        if not r["text"]:
            continue
        label = f"{r['speaker']}：{r['text']}" if r["speaker"] else r["text"]
        tid = _jy_id("mat-t", ti, project)
        materials_texts.append({
            "id": tid, "type": "text", "content": json.dumps(
                {"text": label}, ensure_ascii=False),
        })
        text_segments.append({
            "id": _jy_id("seg-t", ti, project),
            "material_id": tid,
            "target_timerange": {"start": _sec_to_us(r["start"]),
                                 "duration": _sec_to_us(r["duration"])},
            "clip": {"alpha": 1.0, "scale": {"x": 1.0, "y": 1.0},
                     "transform": {"x": 0.0, "y": -0.7}},
        })
        ti += 1

    tracks = [{"type": "video", "segments": video_segments}]
    if audio_segments:
        tracks.append({"type": "audio", "segments": audio_segments})
    if text_segments:
        tracks.append({"type": "text", "segments": text_segments})

    draft_content = {
        "canvas_config": {"width": JY_CANVAS["width"], "height": JY_CANVAS["height"],
                          "ratio": "9:16"},
        "duration": total_us,
        "fps": JY_FPS,
        "version": JY_VERSION,
        "id": _jy_id("draft", 0, project),
        "create_time": int(datetime.now().timestamp()),
        "update_time": int(datetime.now().timestamp()),
        "materials": {
            "videos": materials_videos,
            "audios": materials_audios,
            "texts": materials_texts,
        },
        "tracks": tracks,
    }
    draft_meta = {
        "draft_id": draft_content["id"],
        "draft_name": name,
        "draft_fold_path": draft_dir.replace("\\", "/"),
        "draft_root_path": _out_dir(project, "jianying").replace("\\", "/"),
        "draft_removable_storage_device": "",
        "draft_duration": total_us,
        "draft_fps": JY_FPS,
        "draft_canvas_config": draft_content["canvas_config"],
        "draft_cover": "",
        "draft_create_time": draft_content["create_time"],
        "draft_update_time": draft_content["update_time"],
        "draft_materials": [
            {"type": 0, "value": [{"file_Path": m["path"], "metetype": "video",
                                   "duration": m["duration"]} for m in materials_videos]},
            {"type": 1, "value": [{"file_Path": m["path"], "metetype": "audio",
                                   "duration": m["duration"]} for m in materials_audios]},
        ],
    }

    try:
        with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(draft_content, f, ensure_ascii=False, indent=2)
        with open(os.path.join(draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump(draft_meta, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"草稿写入失败：{e}"}

    manifest = {
        "format": "jianying",
        "project": project,
        "draft_dir": draft_dir,
        "draft_name": name,
        "shot_count": len(rows),
        "total_sec": tl.get("total_sec"),
        "video_track_segments": len(video_segments),
        "audio_track_segments": len(audio_segments),
        "text_track_segments": len(text_segments),
        "audio_source": audio_path or "",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "note": ("剪映草稿为私有格式，若导入后识别异常，"
                 "优先调整 draft_content.json 的 version 字段（JY_VERSION）"),
    }
    _write_manifest(project, "jianying_manifest.json", manifest)
    return {"ok": True, "draft_dir": draft_dir,
            "draft_content": os.path.join(draft_dir, "draft_content.json"),
            "draft_meta": os.path.join(draft_dir, "draft_meta_info.json"),
            **manifest}


# ===================== ② FCPXML（P2-2） =====================

def export_fcpxml(project: str, script: dict, videos: list = None,
                  audio: str = None, timeline: dict = None,
                  fps: int = 24, episode: int = None) -> dict:
    """导出 FCPXML（Premiere Pro / Final Cut Pro 可导入）
    B-17 P2-13：episode 参数可选；提供时按集号过滤视频目录。
    """
    tl = timeline or build_timeline(script, project, videos, episode=episode)
    rows = tl.get("shots") or []
    if not rows:
        return {"ok": False, "error": "没有可导出的镜头"}

    out_dir = _out_dir(project, "fcpxml")
    out_path = os.path.join(out_dir, f"{_safe_filename(project)}.fcpxml")
    # 仅显式传入 audio 时挂音频轨；旧 output/dub 整集配音轨已停止产出
    audio_path = audio or ""

    def _e(s) -> str:
        return saxutils.escape(str(s or ""))

    assets, clips = [], []
    for i, r in enumerate(rows):
        aid = f"r{i + 2}"
        dur = f"{int(round(r['duration'] * fps))}/{fps}s"
        if r["video"] and os.path.isfile(r["video"]):
            assets.append(
                f'    <asset id="{aid}" name="{_e(os.path.basename(r["video"]))}" '
                f'start="0s" duration="{dur}" hasVideo="1" format="r1">\n'
                f'      <media-rep kind="original-media" '
                f'src="file:///{_e(os.path.abspath(r["video"]).replace(os.sep, "/"))}"/>\n'
                f'    </asset>')
            clips.append(
                f'        <asset-clip ref="{aid}" offset="{int(round(r["start"] * fps))}/{fps}s" '
                f'name="{_e(os.path.basename(r["video"]))}" duration="{dur}" '
                f'audioRole="dialogue"/>')
        else:
            # 无视频素材的镜头用占位间隙，保持时间轴长度与剧本一致
            clips.append(
                f'        <gap name="shot_{i + 1}" offset="{int(round(r["start"] * fps))}/{fps}s" '
                f'duration="{dur}"/>')

    audio_asset = ""
    if audio_path and os.path.isfile(audio_path):
        ad = _probe_duration(audio_path) or tl.get("total_sec") or 0
        audio_asset = (
            f'    <asset id="rA1" name="{_e(os.path.basename(audio_path))}" start="0s" '
            f'duration="{int(round(ad * fps))}/{fps}s" hasAudio="1" audioSources="1" '
            f'audioChannels="2" audioRate="48000">\n'
            f'      <media-rep kind="original-media" '
            f'src="file:///{_e(os.path.abspath(audio_path).replace(os.sep, "/"))}"/>\n'
            f'    </asset>')
        clips.append(
            f'        <audio ref="rA1" lane="-1" offset="0s" '
            f'name="{_e(os.path.basename(audio_path))}" duration="{int(round(ad * fps))}/{fps}s" '
            f'role="dialogue"/>')

    total = f"{int(round((tl.get('total_sec') or 0) * fps))}/{fps}s"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.9">\n'
        '  <resources>\n'
        f'    <format id="r1" name="FFVideoFormat1080x1920p{fps}" frameDuration="1/{fps}s" '
        f'width="{JY_CANVAS["width"]}" height="{JY_CANVAS["height"]}" '
        f'colorSpace="1-1-1 (Rec. 709)"/>\n'
        + ("\n".join(assets) + "\n" if assets else "")
        + (audio_asset + "\n" if audio_asset else "")
        + '  </resources>\n'
        '  <library>\n'
        f'    <event name="{_e(project)}">\n'
        f'      <project name="{_e(project)}">\n'
        f'        <sequence format="r1" duration="{total}" tcStart="0s" tcFormat="NDF">\n'
        '          <spine>\n'
        + "\n".join(clips) + "\n"
        '          </spine>\n'
        '        </sequence>\n'
        '      </project>\n'
        '    </event>\n'
        '  </library>\n'
        '</fcpxml>\n'
    )
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"FCPXML 写入失败：{e}"}

    manifest = {"format": "fcpxml", "project": project, "path": out_path,
                "shot_count": len(rows), "total_sec": tl.get("total_sec"),
                "fps": fps, "audio_source": audio_path or "",
                "exported_at": datetime.now().isoformat(timespec="seconds")}
    _write_manifest(project, "fcpxml_manifest.json", manifest)
    return {"ok": True, **manifest}


# ===================== ③ SRT 字幕 =====================

def _fmt_srt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def export_srt(project: str, script: dict, timeline: dict = None,
               videos: list = None, episode: int = None) -> dict:
    """导出 SRT 字幕（逐镜台词，按镜头时间轴对齐）
    B-17 P2-13：episode 参数可选；提供时按集号过滤视频目录。
    """
    tl = timeline or build_timeline(script, project, videos, episode=episode)
    rows = [r for r in (tl.get("shots") or []) if r.get("text")]
    if not rows:
        return {"ok": False, "error": "剧本中没有台词，无法生成字幕"}

    lines, idx = [], 0
    for r in rows:
        idx += 1
        label = f"{r['speaker']}：{r['text']}" if r["speaker"] else r["text"]
        lines.append(f"{idx}\n{_fmt_srt_time(r['start'])} --> {_fmt_srt_time(r['end'])}\n{label}\n")

    out_dir = _out_dir(project, "subtitle")
    out_path = os.path.join(out_dir, f"{_safe_filename(project)}.srt")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"字幕写入失败：{e}"}
    manifest = {"format": "srt", "project": project, "path": out_path,
                "entries": len(rows), "exported_at": datetime.now().isoformat(timespec="seconds")}
    _write_manifest(project, "srt_manifest.json", manifest)
    return {"ok": True, **manifest}


# ===================== ④ 帧序列清单（P2-2） =====================

def export_frames(project: str, script: dict, timeline: dict = None,
                  videos: list = None, copy_files: bool = False,
                  episode: int = None) -> dict:
    """导出关键帧清单：列出每镜的分镜图与视频片段路径 + 时间轴信息
    B-17 P2-13：episode 参数可选；提供时按集号过滤视频目录。
    """
    tl = timeline or build_timeline(script, project, videos, episode=episode)
    rows = tl.get("shots") or []

    sb_dir = os.path.join(STORYBOARDS_DIR, project)
    sb_map = {}
    if os.path.isdir(sb_dir):
        for fn in os.listdir(sb_dir):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                m = re.search(r"\d+", fn)
                if m:
                    sb_map[str(int(m.group()))] = os.path.join(sb_dir, fn)

    out_dir = _out_dir(project, "frames")
    items = []
    for r in rows:
        m = re.search(r"\d+", str(r["shot_id"]))
        k = str(int(m.group())) if m else ""
        items.append({
            "shot_id": r["shot_id"],
            "index": r["index"],
            "start_sec": r["start"], "duration_sec": r["duration"], "end_sec": r["end"],
            "video": r["video"], "storyboard": sb_map.get(k, ""),
            "camera": r["camera"], "description": r["description"],
        })

    manifest = {
        "format": "frames", "project": project, "output_dir": out_dir,
        "shot_count": len(items), "total_sec": tl.get("total_sec"),
        "fps": JY_FPS, "canvas": JY_CANVAS,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
        "note": "storyboard 为镜头构图参考图，video 为已生成的视频片段；可直接导入 AE/PR",
    }
    path = os.path.join(out_dir, f"{_safe_filename(project)}_frames.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"帧清单写入失败：{e}"}
    manifest["path"] = path
    _write_manifest(project, "frames_manifest.json", manifest)
    return {"ok": True, **manifest}


# ===================== 清单与列表 =====================

def _write_manifest(project: str, name: str, data: dict) -> None:
    try:
        path = os.path.join(_out_dir(project), name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"导出清单写入失败：{e}")


def list_exports(project: str = None) -> list:
    """列出已有导出产物"""
    roots = []
    if project:
        roots = [os.path.join(EXPORT_DIR, project)]
    elif os.path.isdir(EXPORT_DIR):
        roots = [os.path.join(EXPORT_DIR, d) for d in sorted(os.listdir(EXPORT_DIR))]
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.endswith("_manifest.json"):
                    p = os.path.join(dirpath, fn)
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:  # noqa: BLE001
                        data = {}
                    out.append({"project": os.path.basename(root),
                                "format": data.get("format") or fn.replace("_manifest.json", ""),
                                "path": p, "dir": dirpath,
                                "exported_at": data.get("exported_at"),
                                "shot_count": data.get("shot_count"),
                                "total_sec": data.get("total_sec"),
                                "size_mb": round(os.path.getsize(p) / 1048576, 3)})
    out.sort(key=lambda x: x.get("exported_at") or "", reverse=True)
    return out


def export_all(project: str, script: dict, videos: list = None,
               audio: str = None, formats: list = None,
               episode: int = None) -> dict:
    """一键导出（默认全部格式；formats 可指定子集）

    formats 可选值：jianying / fcpxml / srt / frames
    B-17 P2-13：episode 参数可选；提供时按集号过滤视频目录。
    """
    tl = build_timeline(script, project, videos, episode=episode)
    results = {"timeline": {"shot_count": len(tl.get("shots") or []),
                            "total_sec": tl.get("total_sec")}}
    want = None
    if isinstance(formats, list) and formats:
        want = {str(f).strip().lower() for f in formats if str(f).strip()}
    if want is None or "jianying" in want:
        results["jianying"] = export_jianying(project, script, videos, audio, timeline=tl, episode=episode)
    if want is None or "fcpxml" in want:
        results["fcpxml"] = export_fcpxml(project, script, videos, audio, timeline=tl, episode=episode)
    if want is None or "srt" in want:
        results["srt"] = export_srt(project, script, timeline=tl, episode=episode)
    if want is None or "frames" in want:
        results["frames"] = export_frames(project, script, timeline=tl, episode=episode)
    return {"ok": any(v.get("ok") for v in results.values() if isinstance(v, dict)),
            "project": project, "output_dir": _out_dir(project), "results": results}
