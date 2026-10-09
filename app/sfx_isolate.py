# -*- coding: utf-8 -*-
"""音效提取（人声分离）—— 把 H3 原生音轨里的「说话声」去掉，只留音效/环境声
（仅手动混音链路使用；mix 步骤已下线，自动流水线不再调用本模块）

背景（2026-09-17）
------------------
H3 是**音视频联合生成**模型（主模型 ``minimax_h3_ref2va`` ＋ 专属音频 VAE
``minimax_h3_audio_vae_fp32``），出片自带音轨：打斗、雨声、环境底噪都在里面。
但这条音轨也会混入 H3 自己生成的说话声，与本项目「全剧统一 QwenTTS 音色」的配音
策略冲突 —— 这也正是当初把整条音轨丢掉（``H3_EMIT_AUDIO=False``）的原因，代价是
成片彻底没有音效。

本模块给出折中方案：**保留音效，剔掉人声**。
用 ComfyUI 的 ``AudioSeparation`` 节点（torchaudio ``HDEMUCS_HIGH_MUSDB_PLUS``，
权重首次运行自动下载）把音轨分成 Bass / Drums / Other / Vocals 四轨，
按 ``config.H3_SFX_STEMS`` 取需要的轨（默认 Drums + Other）相加后落盘。

设计约束
--------
- **fail-open**：任何一步失败（ComfyUI 不可用、节点缺失、超时、产物为空）都只记录
  原因并返回 ``ok=False``，由调用方决定退回「用原始音轨」还是「不铺音效」，**绝不抛异常**。
- **不改动输入文件**，只产出新 wav 到 ``config.H3_SFX_DIR/<项目>/``。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import urllib.parse
from typing import Dict, List, Optional, Sequence, Tuple

from config import COMFYUI_URL, COMFYUI_INPUT_DIR, H3_SFX_DIR, H3_SFX_STEMS

logger = logging.getLogger(__name__)

FFMPEG_BIN = os.getenv("FFMPEG_BIN") or "ffmpeg"

# HDEMUCS 四轨输出下标（与 ComfyUI AudioSeparation 的 RETURN_NAMES 一致）
STEM_INDEX = {"Bass": 0, "Drums": 1, "Other": 2, "Vocals": 3}
AUDIO_EXTS = (".flac", ".wav", ".mp3", ".opus", ".m4a", ".ogg")


def resolve_stems(stems: Optional[Sequence] = None) -> Tuple[int, ...]:
    """把配置（下标或名称）归一化成一组合法下标，默认(config) Drums+Other。"""
    raw = list(stems if stems is not None else (H3_SFX_STEMS or (1, 2)))
    out: List[int] = []
    for s in raw:
        if isinstance(s, str):
            idx = STEM_INDEX.get(s.strip().capitalize())
            if idx is None:
                continue
        else:
            try:
                idx = int(s)
            except (TypeError, ValueError):
                continue
        if 0 <= idx <= 3 and idx != 3 and idx not in out:      # 3=Vocals，人声永远不要
            out.append(idx)
    return tuple(out) or (2,)


def _client(client=None):
    if client is not None:
        return client
    from comfyui_client import ComfyUIClient
    return ComfyUIClient(COMFYUI_URL)


_AVAIL_CACHE: Dict = {"ts": 0.0, "info": None}
_AVAIL_TTL = 300.0


def available(client=None, use_cache: bool = True) -> Dict:
    """探测分离链路是否可用（ComfyUI 在线 ＋ 三个必需节点存在）

    ⚠️ 必须带缓存：`/object_info` 有 4000+ 节点、约几 MB，逐镜调用会非常慢。
    缓存 5 分钟，失败结果同样缓存（避免 ComfyUI 挂掉时每镜都干等超时）。
    """
    import time as _t
    if use_cache and _AVAIL_CACHE["info"] is not None \
            and (_t.time() - _AVAIL_CACHE["ts"]) < _AVAIL_TTL:
        return _AVAIL_CACHE["info"]
    info: Dict = {"ok": False, "reason": "", "nodes": {}}
    try:
        c = _client(client)
        st = c.get_status(timeout=5)                  # 返回 {"status": "online"/"offline"}
        if st.get("status") != "online":
            info["reason"] = f"ComfyUI 不在线：{st.get('error') or st.get('status')}"
        else:
            oi = c.get_object_info() or {}            # 走缓存，别 force
            for need in ("LoadAudio", "AudioSeparation", "SaveAudio"):
                info["nodes"][need] = need in oi
            missing = [k for k, v in info["nodes"].items() if not v]
            if missing:
                info["reason"] = f"ComfyUI 缺少节点：{', '.join(missing)}"
            else:
                info["ok"] = True
    except Exception as e:                             # noqa: BLE001
        info["reason"] = f"{type(e).__name__}: {e}"
    _AVAIL_CACHE.update({"ts": _t.time(), "info": info})
    return info


def extract_audio(media_path: str, out_wav: str, sample_rate: int = 44100) -> Dict:
    """用 ffmpeg 从视频/音频里抽出一条双声道 PCM wav（HDEMUCS 需要立体声上下文）"""
    rec: Dict = {"ok": False, "out": out_wav}
    if not media_path or not os.path.exists(media_path):
        rec["error"] = "输入文件不存在"
        return rec
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    cmd = [FFMPEG_BIN, "-y", "-v", "error", "-i", media_path, "-vn",
           "-ac", "2", "-ar", str(sample_rate), "-c:a", "pcm_s16le", out_wav]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except Exception as e:                                     # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec
    if r.returncode != 0 or not os.path.exists(out_wav) or os.path.getsize(out_wav) <= 44:
        rec["error"] = (r.stderr or "ffmpeg 抽音轨失败").strip()[-300:]
        return rec
    rec.update({"ok": True, "size_bytes": os.path.getsize(out_wav)})
    return rec


def _to_wav(src: str, dst: str, sample_rate: int = 44100) -> bool:
    cmd = [FFMPEG_BIN, "-y", "-v", "error", "-i", src, "-ac", "2", "-ar", str(sample_rate),
           "-c:a", "pcm_s16le", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 44
    except Exception:                                          # noqa: BLE001
        return False


def _is_local_comfy() -> bool:
    """ComfyUI 与后端是否在同一台机器（同一台才能直接往 input 目录拷文件）"""
    host = urllib.parse.urlparse(COMFYUI_URL or "").hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _stage_audio(client, wav_path: str, name: str) -> Tuple[Optional[str], str]:
    """把 wav 送进 ComfyUI 的 input 目录，返回 (在 LoadAudio 里要填的文件名, 方式)。"""
    if _is_local_comfy() and COMFYUI_INPUT_DIR and os.path.isdir(COMFYUI_INPUT_DIR):
        try:
            shutil.copy2(wav_path, os.path.join(COMFYUI_INPUT_DIR, name))
            return name, "copy"
        except OSError as e:
            logger.warning(f"拷贝到 ComfyUI input 目录失败，改走 HTTP 上传：{e}")
    # 远端 ComfyUI：走 /upload/image（该接口不限制扩展名，文件名带 .wav 即可）
    try:
        import requests
        url = f"{client.base_url}/upload/image"
        with open(wav_path, "rb") as f:
            files = {"image": (name, f, "audio/wav")}
            resp = requests.post(url, files=files,
                                 data={"overwrite": "true", "type": "input"}, timeout=180)
        resp.raise_for_status()
        res = resp.json()
        sub = res.get("subfolder") or ""
        fn = res.get("name") or name
        return (f"{sub}/{fn}" if sub else fn), "upload"
    except Exception as e:                                     # noqa: BLE001
        logger.warning(f"上传音频到 ComfyUI 失败：{e}")
        return None, ""


def build_separation_prompt(uploaded: str, filename_prefix: str,
                            stems: Sequence[int] = (1, 2)) -> Dict:
    """构造人声分离工作流（API 格式）。

    LoadAudio → AudioSeparation → (按需 AudioCombine 相加) → SaveAudio
    只保存需要的轨，Vocals 一律不取。
    """
    idx = list(stems)
    prompt: Dict = {
        "1": {"class_type": "LoadAudio", "inputs": {"audio": uploaded}},
        "2": {"class_type": "AudioSeparation",
              "inputs": {"audio": ["1", 0], "chunk_fade_shape": "linear",
                         "chunk_length": 10.0, "chunk_overlap": 0.1}},
    }
    if len(idx) == 1:
        src = ["2", idx[0]]
    else:
        cur = ["2", idx[0]]
        for n, i in enumerate(idx[1:], start=3):
            prompt[str(n)] = {"class_type": "AudioCombine",
                              "inputs": {"audio_1": cur, "audio_2": ["2", i],
                                         "method": "add"}}
            cur = [str(n), 0]
        src = cur
    prompt["90"] = {"class_type": "SaveAudio",
                    "inputs": {"audio": src, "filename_prefix": filename_prefix}}
    return prompt


def isolate_sfx(media_path: str, project: str, tag: str,
                stems: Optional[Sequence] = None,
                out_dir: str = "", client=None, timeout: int = 900) -> Dict:
    """对一条镜头（视频或音频）做人声分离，产出「只有音效」的 wav。

    返回 dict：``{ok, out_path, stems, staged, produced, error, elapsed_sec, ...}``。
    **任何失败都 fail-open**（不抛异常），调用方据此决定退回原音轨。
    """
    import time
    t0 = time.time()
    stems = resolve_stems(stems)
    out_dir = out_dir or os.path.join(H3_SFX_DIR, project)
    out_path = os.path.join(out_dir, f"{tag}_sfx.wav")
    rec: Dict = {"ok": False, "input": media_path, "out_path": out_path,
                 "stems": list(stems), "kind": "sfx_isolate"}
    if not media_path or not os.path.exists(media_path):
        rec["error"] = "输入文件不存在"
        return rec
    os.makedirs(out_dir, exist_ok=True)

    # 1) 抽音轨（HDEMUCS 要立体声）
    tmp_wav = os.path.join(out_dir, f"_{tag}_src.wav")
    ex = extract_audio(media_path, tmp_wav)
    if not ex.get("ok"):
        rec["error"] = f"抽音轨失败：{ex.get('error')}"
        return rec

    # 2) 送到 ComfyUI 并跑分离
    try:
        c = _client(client)
        avail = available(c)
        if not avail.get("ok"):
            rec["error"] = f"分离链路不可用：{avail.get('reason')}"
            return rec
        staged_name = f"mjscxt_sfx_{project}_{tag}.wav"
        uploaded, how = _stage_audio(c, tmp_wav, staged_name)
        if not uploaded:
            rec["error"] = "音频送入 ComfyUI 失败（拷贝与上传都失败）"
            return rec
        rec["staged"] = how
        prefix = f"mjscxt_sfx/{project}/{tag}"
        prompt = build_separation_prompt(uploaded, prefix, stems)
        pid = c.queue_prompt(prompt)
        history = c.wait_for_completion(pid, timeout=timeout)
        files = [f for f in c.get_output_files(history, "")
                 if str(f).lower().endswith(AUDIO_EXTS)]
        rec["produced"] = files
        if not files:
            rec["error"] = "分离未产出音频文件"
            return rec
        # 3) 归一成 wav 落盘（下游用 ffmpeg amix，格式统一更稳）
        if not _to_wav(files[0], out_path):
            rec["error"] = f"分离产物转 wav 失败：{files[0]}"
            return rec
    except Exception as e:                                     # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec
    finally:
        try:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)
        except OSError as e:
            logger.debug("清理人声分离临时文件失败（忽略）：%s", e)

    rec.update({"ok": True, "size_bytes": os.path.getsize(out_path),
                "elapsed_sec": round(time.time() - t0, 2)})
    return rec


def sfx_track_for_shot(project: str, episode: int, shot_id, out_dir: str = "") -> str:
    """按约定返回某镜音效文件路径（只拼路径，不保证存在）"""
    tag = f"ep{int(episode):02d}_shot{int(shot_id):02d}"
    return os.path.join(out_dir or os.path.join(H3_SFX_DIR, project), f"{tag}_sfx.wav")


def build_sfx_entries(project: str, episode: int, timeline: Dict,
                      volume: float = 0.3, out_dir: str = "") -> List[Dict]:
    """把「已分离好的逐镜音效」变成混音条目（与 TTS 条目同一条时间轴）。

    产出的条目带 ``kind="sfx"`` 与 ``volume``，由 `dub_mix.mix_video_with_entries`
    以垫底方式混入。不存在的文件直接跳过（该镜没有音效不影响其它镜）。
    """
    entries: List[Dict] = []
    for slot in (timeline or {}).get("shots") or []:
        sid = slot.get("shot_id")
        if sid is None:
            continue
        path = sfx_track_for_shot(project, episode, sid, out_dir)
        if not os.path.exists(path) or os.path.getsize(path) <= 44:
            continue
        entries.append({
            "line_id": f"sfx_ep{int(episode):02d}_shot{int(sid):02d}",
            "shot_id": sid, "kind": "sfx",
            "character": "", "text": "[音效]",
            "audio_path": path,
            "start": float(slot.get("start") or 0),
            "audio_dur": float(slot.get("duration") or 0),
            "volume": float(volume),
        })
    return entries
