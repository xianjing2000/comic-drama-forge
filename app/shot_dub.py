# -*- coding: utf-8 -*-
"""逐镜 TTS 驱动音轨（2026-10-19，用户 P0「配音语义破碎」的修复）。

背景（用户成片实测 P0）：
  · H3 `audioMode=generate` 由模型"演"台词 → **伪中文乱码**（可读台词不足一半）、
    人名漂移（赵天监/赵安/赵晓天）、把**画面描述当旁白念**出来（「静态构图，尘土…」）、
    群像/反派共用一个嗓。这不是提示词问题 —— ref_audio 只驱动口型、不锁音色，
    H3 的自生成语音本来就不可靠（见 memory/reference_h3_skill_audio）。

修法（与 10-06 手跑模板同一思路的"后驱"）：**台词交给 TTS 逐字念**，H3 以
``audioMode=source`` 用这支音轨**驱动口型/节奏**（音轨原样进入成片）：
  · 台词逐字准确、姓名不再被"演"歪；
  · 音色 = 角色 voice_bank 克隆音色（与 H3 公共参考音色同源，前后一致）；
  · 画面描述不再有机会被念出来（音轨里只有台词）。

产物（每镜一支，含无台词镜的静音轨）：
  ``<DUB_DIR>/<项目>/shots/epNN/shot_XX.wav``          驱动音轨
  ``<DUB_DIR>/<项目>/shots/epNN/shot_XX.json``         指纹（文本/音色/镜长），命中复用
  ``<DUB_DIR>/<项目>/shots/epNN/_silence/silence_*.wav`` 静音（按秒缓存）
  ``<DUB_DIR>/<项目>/shots/epNN/_fit/…``                裁剪到镜长的缓存

开关：``MJSCXT_H3_DUB_DRIVEN``（默认 1；=0 回退 H3 自生成音频）。
fail-open 铁律：TTS 不可用 / 任一句合成失败 / ffmpeg 异常 → 返回 ``{}``，
调用方整体回退 generate 模式（绝不出现"半集 TTS 半集静音"的残缺音轨）。
⚠️ 代价（已知）：source 模式下成片音轨就是驱动音轨 —— **没有 H3 原生环境音/音效**，
   后续如需 BGM/音效需另加混音层（与 dub_mix 的既有能力衔接）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: 句首静音（给画面一个起势，也避免第一字被切）
_LEAD_SEC = 0.30
#: 句间停顿
_GAP_SEC = 0.30
#: 输出统一采样规格（与 tts_client.concat_audio 一致）
_RATE = 24000


def dub_driven_enabled() -> bool:
    return str(os.environ.get("MJSCXT_H3_DUB_DRIVEN", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _project_dub_dir(project: str) -> str:
    from dub_helpers import _dub_project_dir
    return _dub_project_dir(str(project or "project"))


def _shots_dir(project: str, episode_no) -> str:
    try:
        ep = int(episode_no)
    except (TypeError, ValueError):
        ep = 1
    return os.path.join(_project_dub_dir(project), "shots", "ep%02d" % ep)


def _run_ffmpeg(cmd: List[str], timeout: int = 300) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            logger.warning("[逐镜配音] ffmpeg 失败：%s", (r.stderr or "").strip()[:300])
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[逐镜配音] ffmpeg 异常：%s", e)
        return False


def silence_for(duration: float) -> str:
    """生成（并缓存）指定秒数的静音轨；失败返回空串。缓存跨项目共享。"""
    try:
        d = max(0.2, round(float(duration), 2))
    except (TypeError, ValueError):
        d = 5.0
    from config import DUB_DIR
    out_dir = os.path.join(DUB_DIR, "_silence")
    out = os.path.join(out_dir, "silence_%05.2fs.wav" % d)
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except OSError as e:  # noqa: BLE001
        logger.debug("[逐镜配音] 静音缓存探测失败（忽略）：%s", e)
    os.makedirs(out_dir, exist_ok=True)
    ok = _run_ffmpeg(["ffmpeg", "-y", "-f", "lavfi", "-i",
                      "anullsrc=r=%d:cl=mono" % _RATE, "-t", "%0.2f" % d,
                      "-c:a", "pcm_s16le", out], timeout=120)
    return out if ok and os.path.isfile(out) else ""


def _atempo_filters(ratio: float) -> List[str]:
    r = float(ratio or 1.0)
    out: List[str] = []
    while r > 2.0:
        out.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        out.append("atempo=0.5")
        r /= 0.5
    if abs(r - 1.0) > 0.02:
        out.append("atempo=%.4f" % r)
    return out


def fit_track(src: str, duration: float, out_dir: str) -> str:
    """把音轨裁/补到恰好 duration 秒（超长整体提速，尾部补静音），带缓存。"""
    try:
        d = max(0.2, round(float(duration), 2))
    except (TypeError, ValueError):
        d = 5.0
    if not src or not os.path.isfile(src):
        return ""
    try:
        import tts_client as _tts
        sdur = float((_tts.probe_audio(src) or {}).get("duration") or 0)
    except Exception:  # noqa: BLE001
        sdur = 0.0
    filt = []
    if sdur > d * 1.02:
        filt += _atempo_filters(sdur / d)
    filt.append("apad")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, "%s_fit%05.2f.wav" % (stem, d))
    try:
        if (os.path.isfile(out) and os.path.getsize(out) > 0
                and os.path.getmtime(out) >= os.path.getmtime(src)):
            return out
    except OSError as e:  # noqa: BLE001
        logger.debug("[逐镜配音] fit 缓存探测失败（忽略）：%s", e)
    ok = _run_ffmpeg(["ffmpeg", "-y", "-i", src, "-af", ",".join(filt),
                      "-t", "%0.2f" % d, "-ar", str(_RATE), "-ac", "1",
                      "-c:a", "pcm_s16le", out])
    return out if ok and os.path.isfile(out) else ""


def _plan_lines(project: str, episode_no, script: dict,
                shot_ids: Optional[List] = None) -> List[dict]:
    """由剧本生成配音行（含角色克隆音色）；失败抛异常，由调用方兜。"""
    from tts_client import (build_dub_plan, default_voice_map, load_voice_map,
                            find_voice_bank_ref)
    out_dir = _project_dub_dir(project)
    vm_path = os.path.join(out_dir, "voice_map.json")
    try:
        voice_map = load_voice_map(vm_path)
    except Exception:  # noqa: BLE001
        voice_map = None
    voice_map = voice_map or default_voice_map(
        script.get("characters") or [], project, episode_no)
    plan = build_dub_plan(script, voice_map, project, episode_no,
                          shot_ids=shot_ids, out_dir_wav="")
    # 参考音色 → clone（与 H3 公共参考音色同源：voice_bank 里每个角色一支）。
    # 用户显式选过 design/clone 的保留；preset/auto 一律换成角色克隆音色。
    for ln in plan.get("lines") or []:
        v = dict(ln.get("voice") or {})
        if str(v.get("mode") or "").lower() in ("design", "clone") and v.get("ref_audio"):
            continue
        ref, rt = find_voice_bank_ref(out_dir, str(ln.get("character") or ""))
        if not ref:
            continue
        v["mode"] = "clone"
        v["ref_audio"] = ref
        if rt and not v.get("ref_text"):
            v["ref_text"] = rt
        ln["voice"] = v
    return plan.get("lines") or []


def _fingerprint(lines: List[dict], duration: float) -> str:
    rows = []
    for ln in lines:
        v = ln.get("voice") or {}
        ref = str(v.get("ref_audio") or "")
        try:
            ref_m = int(os.path.getmtime(ref)) if ref and os.path.isfile(ref) else 0
        except OSError:
            ref_m = 0
        rows.append([str(ln.get("character") or ""), str(ln.get("text") or ""),
                     str(v.get("mode") or ""), ref, ref_m, str(ln.get("emotion") or "")])
    blob = json.dumps({"lines": rows, "dur": round(float(duration), 2)},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _build_shot_track(project: str, episode_no, seq: int, lines: List[dict],
                      duration: float, sdir: str) -> str:
    """合成一支「该镜台词」音轨（含缓存）；失败返回空串。"""
    sig = _fingerprint(lines, duration)
    wav = os.path.join(sdir, "shot_%02d.wav" % seq)
    meta = os.path.join(sdir, "shot_%02d.json" % seq)
    try:
        if os.path.isfile(wav) and os.path.getsize(wav) > 0 and os.path.isfile(meta):
            with open(meta, "r", encoding="utf-8") as f:
                if str((json.load(f) or {}).get("sig") or "") == sig:
                    return wav
    except Exception as e:  # noqa: BLE001
        logger.debug("[逐镜配音] 缓存指纹读取失败（按未命中处理）：%s", e)

    # 1) 逐句合成（写入 work 目录；synthesize_lines 自带分批与逐句降级）
    work = os.path.join(sdir, "_lines")
    os.makedirs(work, exist_ok=True)
    prepared = []
    for i, ln in enumerate(lines):
        ln = dict(ln)
        ln["out_path"] = os.path.join(work, "shot%02d_l%02d.wav" % (seq, i + 1))
        prepared.append(ln)
    from tts_client import QwenTTSClient
    client = QwenTTSClient()
    results = client.synthesize_lines(prepared, work)
    by_path = {os.path.abspath(r.get("out_path") or ""): r for r in (results or [])}
    wavs: List[str] = []
    for ln in prepared:
        r = by_path.get(os.path.abspath(ln["out_path"])) or {}
        p = str(ln["out_path"])
        if not r.get("ok") or not os.path.isfile(p) or os.path.getsize(p) <= 0:
            logger.warning("[逐镜配音] shot=%02d 第 %s 句合成失败（整集回退 generate）：%s",
                           seq, ln.get("line_id"), (r.get("error") or "")[:160])
            return ""
        wavs.append(p)

    # 2) 测量各句时长 → 拼 lead/gap/tail 静音
    try:
        import tts_client as _tts
        durs = [float((_tts.probe_audio(p) or {}).get("duration") or 0) for p in wavs]
    except Exception:  # noqa: BLE001
        return ""
    total = _LEAD_SEC + sum(durs) + _GAP_SEC * max(0, len(wavs) - 1)
    tail = max(0.0, float(duration) - total)
    parts: List[str] = [silence_for(round(_LEAD_SEC, 2))]
    for i, p in enumerate(wavs):
        parts.append(p)
        if i < len(wavs) - 1:
            parts.append(silence_for(round(_GAP_SEC, 2)))
    parts.append(silence_for(round(tail, 2)))
    parts = [p for p in parts if p]
    try:
        import tts_client as _tts2
        tmp = os.path.join(sdir, "_tmp_%02d.wav" % seq)
        _tts2.concat_audio(parts, tmp, fmt="wav")
    except Exception as e:  # noqa: BLE001
        logger.warning("[逐镜配音] shot=%02d 拼接失败（整集回退 generate）：%s", seq, e)
        return ""
    # 3) 裁/补到镜长
    fit = fit_track(tmp, duration, os.path.join(sdir, "_fit"))
    if not fit:
        return ""
    try:
        os.replace(fit, wav)
        if os.path.isfile(tmp):
            os.remove(tmp)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"sig": sig, "duration": round(float(duration), 2),
                       "lines": len(wavs), "built_at": __import__("time").strftime(
                           "%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("[逐镜配音] 落盘失败（忽略）：%s", e)
        return fit
    return wav


def build_episode_shot_dubs(project_name: str, episode_no, script: dict,
                            shots_hint: Optional[List[dict]] = None,
                            only_shot_ids: Optional[List] = None) -> Dict[str, str]:
    """为整集每个镜头准备驱动音轨，返回 ``{shot_id(str): wav 路径}``。

    · 有台词的镜 → TTS 台词音轨；无台词的镜 → 与镜长相同的静音轨；
    · 任一「有台词镜」合成失败 → **整集返回 {}**（调用方整体回退 generate 模式，
      避免"半集 TTS 半集静音"造成台词缺失）；
    · 整体 fail-open：任何异常只记日志。
    """
    if not dub_driven_enabled():
        return {}
    script = script or {}
    shots = list(shots_hint or script.get("shots") or [])
    if not shots:
        return {}
    try:
        from tts_client import check_environment
        env = check_environment() or {}
        if not env.get("available"):
            logger.warning("[逐镜配音] TTS 环境不可用 → 回退 H3 自生成音频：%s",
                           env.get("reason") or env.get("error") or "unknown")
            return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[逐镜配音] TTS 自检失败（回退 H3 自生成音频）：%s", e)
        return {}

    try:
        lines = _plan_lines(project_name, episode_no, script,
                            shot_ids=only_shot_ids)
    except Exception as e:  # noqa: BLE001
        logger.warning("[逐镜配音] 配音计划生成失败（回退 H3 自生成音频）：%s", e)
        return {}

    by_shot: Dict[str, List[dict]] = {}
    for ln in lines:
        by_shot.setdefault(str(ln.get("shot_id")), []).append(ln)

    sdir = _shots_dir(project_name, episode_no)
    try:
        os.makedirs(sdir, exist_ok=True)
    except OSError as e:
        logger.warning("[逐镜配音] 目录不可建（回退 H3 自生成音频）：%s", e)
        return {}

    out: Dict[str, str] = {}
    for shot in shots:
        sid = str(shot.get("shot_id"))
        if only_shot_ids and sid not in [str(x) for x in only_shot_ids]:
            continue
        try:
            seq = int(sid)
        except (TypeError, ValueError):
            seq = abs(hash(sid)) % 1000
        try:
            dur = float(shot.get("duration") or 10)
        except (TypeError, ValueError):
            dur = 10.0
        shot_lines = by_shot.get(sid) or []
        if not shot_lines:
            p = silence_for(round(dur, 2))
        else:
            p = _build_shot_track(project_name, episode_no, seq, shot_lines, dur, sdir)
            if not p:
                logger.warning("[逐镜配音] 存在未成功的台词镜（shot=%s）→ "
                               "整集回退 H3 自生成音频", sid)
                return {}
        if p:
            out[sid] = p
    logger.info("[逐镜配音] 已完成 %d 镜驱动音轨（TTS，source 模式）→ %s",
                len(out), os.path.relpath(sdir, _project_dub_dir(project_name)))
    return out


def build_shot_track(project_name: str, episode_no, script: dict, shot: dict) -> str:
    """单镜（重跑接口用）：返回该镜驱动音轨；无台词返回静音；失败返回空串。"""
    if not dub_driven_enabled():
        return ""
    sid = str((shot or {}).get("shot_id"))
    try:
        seq = int(sid)
    except (TypeError, ValueError):
        return ""
    try:
        dur = float((shot or {}).get("duration") or 10)
    except (TypeError, ValueError):
        dur = 10.0
    try:
        from tts_client import check_environment
        if not (check_environment() or {}).get("available"):
            return ""
        lines = _plan_lines(project_name, episode_no, script or {}, shot_ids=[shot.get("shot_id")])
        lines = [ln for ln in lines if str(ln.get("shot_id")) == sid]
    except Exception as e:  # noqa: BLE001
        logger.warning("[逐镜配音] 单镜计划失败（回退 generate）：%s", e)
        return ""
    sdir = _shots_dir(project_name, episode_no)
    try:
        os.makedirs(sdir, exist_ok=True)
    except OSError:
        return ""
    if not lines:
        return silence_for(round(dur, 2))
    return _build_shot_track(project_name, episode_no, seq, lines, dur, sdir)
