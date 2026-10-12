# -*- coding: utf-8 -*-
"""四层质量状态机 + 哈希绑定人审（借鉴 ai-manga-factory 的 A/B/C/D 与哈希晋级）

为什么需要「四层」而不是「一个 passed」
------------------------------------
此前只有二值的质检结论，出问题时无法回答「卡在哪一层」。四层把责任分开：

======  ==================  ====================================================
层      名称                 谁负责 / 判据
======  ==================  ====================================================
A       technical_render    **确定性**：ffprobe 时长/分辨率/fps/codec/音轨是否存在
B       content_qa          **自动**：复用现有 qc_client 的逐镜与整片结论
C       editorial_review    **人工**：并排复核（合同目标 / 参考资产 / 抽帧 / QA 指标）
D       release_approval    **人工**：整集可发布（联系表、节奏、平台安全区…）
======  ==================  ====================================================

⚠️ A 层通过只代表「**技术生成完成**」，**绝不等于内容合格** ——
   这是竞品文档里反复强调的一条，也是本模块刻意用 label 而不是 passed 来措辞的原因。

为什么批准必须绑定哈希（本模块最值钱的部分）
-----------------------------------------
原实现只在**路径变化**时把 review 复位。但重做一集时产物路径是同一个
（ep01_full.mp4 会被原地覆盖）→ 路径没变 → **旧的「已验收」结论继续生效**。
用户看到的是「已验收」，实际批的是上一版内容 —— 静默事故，且无法察觉。

绑定三元组后，任一变化即判定批准失效：

* artifact_sha256：产物内容（快路径用 size+mtime 先比，避免每次哈希大文件）
* contract_hash  ：合同（该集应有的镜头数 / 时长 / 来源）
* qa_report_hash ：当次质检报告

一切失败 **fail-open**：绑定算不出/读不到时只判 unbound（不误伤），
状态机读写失败只降级为内存态，绝不阻断出片。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

__all__ = [
    "STAGES", "STAGE_NAMES", "STAGE_LABELS", "STATUSES",
    "technical_checks", "evaluate_technical", "evaluate_content",
    "blank_state", "set_stage", "release_ready",
    "state_path", "load_state", "save_state", "record_stage",
    "sha256_file", "hash_obj", "file_signature",
    "make_binding", "check_binding", "stage_binding", "check_stage_binding",
    # 音轨电平 + 成片整轨音频客观层（2026-10-12 P0：静音被静音轨掩盖）
    "probe_audio_metrics", "audio_level_check", "silence_intervals",
    "audio_quality_verdict", "final_audio_qc_path", "save_final_audio_qc",
    "load_final_audio_qc",
]

#: 四层，顺序即晋级顺序。
STAGES: Tuple[str, ...] = ("A", "B", "C", "D")
STAGE_NAMES = {"A": "technical_render", "B": "content_qa",
               "C": "editorial_review", "D": "release_approval"}
#: 给界面/日志用的中文措辞。A 刻意不写「通过」，避免被误读成质量结论。
STAGE_LABELS = {"A": "技术生成完成", "B": "内容质检", "C": "编辑复核", "D": "发布批准"}
STATUSES = ("pending", "passed", "failed", "blocked", "skipped")

_UNSET = object()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _norm_ep(episode: Any) -> str:
    """把集号规整成安全的目录名（支持 3 / "3" / "整集"）。"""
    s = str(episode if episode is not None else "all").strip() or "all"
    if s.isdigit():
        return "ep%02d" % int(s)
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in s)


# =====================================================================
# A 层：技术渲染（确定性，不掺任何主观判断）
# =====================================================================

def technical_checks(path: str, *, probe=None, min_duration: float = 2.0,
                     audio_probe=None) -> List[Dict[str, Any]]:
    """A 层的确定性检查项。返回 [{check, ok, detail}]，全部可单独定位。

    audio_probe：音轨电平探针（默认走 audio_qc.probe_metrics）。测试可注入桩；
    探针不可用时该项判「未判定」（ok=True），不冒充缺陷。
    """
    checks: List[Dict[str, Any]] = []

    exists = bool(path) and os.path.isfile(path)
    checks.append({"check": "file_exists", "ok": exists,
                   "detail": os.path.basename(str(path or "")) or "(空路径)"})
    if not exists:
        return checks

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    checks.append({"check": "non_empty", "ok": size > 0, "detail": "%d bytes" % size})

    if probe is None:
        from video_probe import probe_video as probe
    try:
        info = probe(path) or {}
    except Exception as e:                                  # noqa: BLE001
        info = {"ok": False, "error": "探测异常: %s" % e}

    if not info.get("ok"):
        checks.append({"check": "probe", "ok": False,
                       "detail": str(info.get("error") or "探测失败")[:200]})
        return checks

    checks.append({"check": "probe", "ok": True,
                   "detail": "%sx%s @%sfps" % (info.get("width"), info.get("height"),
                                               info.get("fps"))})
    dur = float(info.get("duration") or 0)
    checks.append({"check": "duration", "ok": dur >= min_duration,
                   "detail": "%.2fs (下限 %.1fs)" % (dur, min_duration)})
    checks.append({"check": "video_codec", "ok": bool(info.get("video_codec")),
                   "detail": str(info.get("video_codec"))})
    checks.append({"check": "fps", "ok": float(info.get("fps") or 0) > 0,
                   "detail": str(info.get("fps"))})
    # 音轨「存在性」只记录、不判失败：H3 视频自带原生音轨（成片即带配音）；无音轨的
    # 硬拦截在 qc_client 视频客观层（B 层）。
    # ⚠️ 2026-10-12 P0（t10 审计）：只查「有没有音轨流」会被**补出来的静音轨**骗过 ——
    #    asset_refs._h3_audio_policy → video_postprocess.ensure_audio_track 会给
    #    「H3 本来没出声」的镜头补一条 anullsrc 静音 AAC 轨（保证拼接一致）。补完之后
    #    ffprobe 报 has_audio=True，qc_client 的 no_audio 硬闸与本项**都判正常** →
    #    整镜无声被静默放行。故这里必须再查一次**电平**（见 audio_level_check）。
    checks.append({"check": "has_audio", "ok": True,
                   "detail": "有音轨" if info.get("has_audio") else "无音轨（生成阶段静音或待配音）"})
    # ⚠️ 电平检查必须 fail-open：音频探针不可用（无 ffmpeg / 文件不可解码）时判
    #    ok=True（「未判定」）—— 环境问题不得冒充内容缺陷，否则本层会整片假失败。
    checks.append(audio_level_check(path, probe=audio_probe))
    return checks


def evaluate_technical(path: str, *, probe=None,
                       min_duration: float = 2.0,
                       audio_probe=None) -> Dict[str, Any]:
    """跑 A 层并给出该层状态。

    ⚠️ status=passed 的语义是「**技术生成完成**」，不是「内容合格」。

    ⚠️ 2026-10-12 P0：本层现在**会**因「音轨近乎无声」判 failed（见
    audio_level_check）—— 这是为了堵住「anullsrc 静音轨冒充正常」。
    探针不可用时仍判未判定（ok=True），环境问题不冒充缺陷。
    """
    checks = technical_checks(path, probe=probe, min_duration=min_duration,
                              audio_probe=audio_probe)
    failed = [c for c in checks if not c.get("ok")]
    return {
        "stage": "A", "name": STAGE_NAMES["A"], "label": STAGE_LABELS["A"],
        "status": "passed" if checks and not failed else "failed",
        "meaning": "技术生成完成（不代表内容合格）",
        "checks": checks,
        "failed_checks": [c["check"] for c in failed],
        "at": _now(),
    }


# =====================================================================
# 音轨电平 + 成片整轨音频客观层（2026-10-12 P0：静音被静音轨掩盖）
# =====================================================================
# 为什么单独立这一层：现行 7 步流水线（pipeline.STEP_SEQUENCE）里没有 tts、没有 mix，
# 成片就是 H3 原生音轨**直接 copy2**（pipeline.step_final）—— 全程没有任何音频门禁。
# 唯一那条整轨音频质检在**已停用的** mix 链路里（workers.audio._mix_audio_qc）。
# t10 审计实测的三条后果：
#   ① 成片整段静音 / H3 该集没出声 → 判「正常」，一路交付到用户耳朵；
#   ② 被 video_postprocess.ensure_audio_track 补过 anullsrc 静音轨的镜头 → has_audio=True，
#      qc_client 的 no_audio 硬闸与本模块 A 层的 has_audio 都判正常；
#   ③ 面板音频体检只扫 <dub>/<项目>/*_lines/，而 tts_pre 产的是 voice_bank → audio_checked 恒 0。
# 本段提供三件东西：电平探针（A 层判据用）、整轨结论（final 门禁用）、
# 静音区间 → 镜级定位（交付清单里按镜号追责）。
AUDIO_NEAR_SILENT_DB = -50.0      # 与 audio_qc.NEAR_SILENT_MEAN_DB 同口径
AUDIO_HARD_SILENT_RATIO = 0.15    # 与 audio_qc.HARD_SILENT_RATIO 同口径


def _audio_qc_module():
    """惰性取 audio_qc 模块（缺失/异常 → None，调用方据此 fail-open）。"""
    try:
        import audio_qc
        return audio_qc
    except Exception as e:                                   # noqa: BLE001
        logger.warning("audio_qc 不可用（音频判据降级为「未判定」）：%s", e)
        return None


def probe_audio_metrics(path: str, *, probe=None) -> Dict[str, Any]:
    """音频客观指标（复用 audio_qc，**永不抛异常**）。

    关键区分（本函数的全部价值）：
      · available=False —— **没取到电平**（ffmpeg 缺失 / 文件不可解码）→ 环境问题，
        调用方必须 fail-open，不得判成内容缺陷；
      · available=True 且 mean_db / speech_ratio 极低 —— 拿到了电平，音频**确实**无声
        （anullsrc 静音轨实测 mean -91dB）→ 这才是要拦的缺陷。
    """
    out: Dict[str, Any] = {
        "available": False, "has_level": False, "ok": False, "error": "",
        "duration": 0.0, "mean_db": None, "max_db": None, "silence_sec": 0.0,
        "speech_ratio": None, "codec": "", "sample_rate": 0, "channels": 0,
        "engine": "audio_qc",
    }
    if not path or not os.path.isfile(path):
        out["error"] = "文件不存在"
        return out
    fn = probe
    if fn is None:
        aq = _audio_qc_module()
        if aq is None:
            out["error"] = "audio_qc 模块不可用（未判定）"
            return out
        fn = getattr(aq, "probe_metrics", None)
        if fn is None:
            out["error"] = "audio_qc 未提供 probe_metrics（未判定）"
            return out
    try:
        m = fn(path) or {}
    except Exception as e:                                   # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    for k in ("ok", "error", "duration", "mean_db", "max_db", "silence_sec",
              "speech_ratio", "codec", "sample_rate", "channels"):
        if k in m:
            out[k] = m.get(k)
    out["has_level"] = (out.get("mean_db") is not None) or (out.get("speech_ratio") is not None)
    out["available"] = bool(out["has_level"])
    if not out["available"] and not out.get("error"):
        out["error"] = "未取到音频电平（文件可能不含音频流，或无法解码）"
    return out


def _fmt_pct(v) -> str:
    return ("%.1f%%" % (float(v) * 100)) if isinstance(v, (int, float)) else "n/a"


def audio_level_check(path: str, *, probe=None) -> Dict[str, Any]:
    """A 层的音轨**电平**检查项（未取到电平 → ok=True「未判定」，绝不冒充缺陷）。

    判据与 audio_qc 硬闸同口径：平均电平 < -50dB 或 有声占比 < 15% → 近乎无声。
    这正是 anullsrc 补出来的静音轨的特征（实测 mean -91dB / 有声占比 0%），
    也是「只看音轨流存在」永远发现不了的那一类。
    """
    lvl = probe_audio_metrics(path, probe=probe)
    if not lvl.get("available"):
        return {"check": "audio_level", "ok": True,
                "detail": "未判定（%s）" % str(lvl.get("error") or "音频探针不可用")[:120],
                "metrics": lvl}
    mean_db = lvl.get("mean_db")
    ratio = lvl.get("speech_ratio")
    near = ((isinstance(mean_db, (int, float)) and float(mean_db) < AUDIO_NEAR_SILENT_DB)
            or (isinstance(ratio, (int, float)) and float(ratio) < AUDIO_HARD_SILENT_RATIO))
    if near:
        detail = ("音轨近乎无声：平均电平 %s dB（阈值 %.0f）、有声占比 %s —— "
                  "常见成因是 anullsrc 补出来的静音轨，或 H3 该段根本没出声"
                  % (mean_db, AUDIO_NEAR_SILENT_DB, _fmt_pct(ratio)))
    else:
        detail = ("音轨电平正常：平均 %s dB / 有声占比 %s / 静音合计 %.2fs"
                  % (mean_db, _fmt_pct(ratio), float(lvl.get("silence_sec") or 0)))
    return {"check": "audio_level", "ok": not near, "detail": detail, "metrics": lvl}


_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_DUR_RE = re.compile(r"silence_duration:\s*(\d+(?:\.\d+)?)")


def _ffmpeg_bin() -> str:
    """ffmpeg 可执行（与 audio_qc 同口径：MJSCXT_FFMPEG → PATH），取不到就回 "ffmpeg"。"""
    aq = _audio_qc_module()
    fn = getattr(aq, "ffmpeg_exe", None) if aq is not None else None
    if callable(fn):
        try:
            return str(fn() or "ffmpeg")
        except Exception:                                    # noqa: BLE001
            pass
    return "ffmpeg"


def silence_intervals(path: str, *, timeout: int = 300, noise_db: float = -35.0,
                      min_dur: float = 0.35) -> Optional[List[Tuple[float, float]]]:
    """一次 ffmpeg silencedetect → [(start, dur), ...]；不可用/失败 → None。

    只做**一次**额外解码，失败一律 None（调用方据此跳过镜级定位，整轨结论不受影响）。
    返回 [] 表示「探测成功且整段没有静音段」。
    """
    if not path or not os.path.isfile(path):
        return None
    cmd = [_ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path,
           "-map", "0:a:0",
           "-af", "silencedetect=noise=%sdB:d=%s" % (noise_db, min_dur),
           "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception as e:                                   # noqa: BLE001
        logger.debug("静音区间探测失败（跳过镜级定位）：%s", e)
        return None
    txt = (p.stderr or b"").decode("utf-8", errors="replace") if isinstance(p.stderr, bytes) \
        else str(p.stderr or "")
    starts = [float(x) for x in _SILENCE_START_RE.findall(txt)]
    durs = [float(x) for x in _SILENCE_DUR_RE.findall(txt)]
    if not durs:
        return [] if p.returncode == 0 else None
    pairs = list(zip(starts, durs)) if len(starts) >= len(durs) else list(zip(durs, durs))
    return [(float(a), float(d)) for (a, d) in pairs]


def _overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def _shot_slots(shots, total_sec: float) -> List[Dict[str, Any]]:
    """剧本镜头 → 实测成片上的时间槽（与 step_final 字幕同一套按比例缩放的口径）。"""
    durs: List[float] = []
    for s in (shots or []):
        try:
            d = float((s or {}).get("duration") or 0)
        except (TypeError, ValueError):
            d = 0.0
        durs.append(d if d > 0 else 5.0)
    tot = sum(durs) or 1.0
    rows: List[Dict[str, Any]] = []
    cur = 0.0
    for s, d in zip((shots or []), durs):
        seg = d / tot * float(total_sec)
        rows.append({"shot_id": (s or {}).get("shot_id"), "start": round(cur, 3),
                     "end": round(cur + seg, 3), "duration": round(seg, 3)})
        cur += seg
    return rows


def audio_quality_verdict(path: str, *, shots=None, total_sec=None, expect_sec=None,
                          min_mean_db=None, max_drift=None, metrics=None,
                          intervals=None, probe=None) -> Dict[str, Any]:
    """整轨音频客观层（final 成片门禁 / 面板体检共用；**整轨口径**）。

    整轨口径 = 不设「有声占比下限」（check_speech_ratio=False）：成片天然有大段无台词
    留白（无台词镜头 / 纯环境音），拿单句的 50% 标准去卡它必然误报「漏句」。
    真正要挡的是**有音轨但整段近乎无声**、以及**逐镜无声**（H3 该镜没出声或补了静音轨）。

    metrics 可注入（测试用）；intervals 可注入静音区间（省一次解码）。返回的 silent_shots
    就是「可定位到镜号」的那份结论（pipeline.step_final 会落盘并挂进交付清单 meta）。
    """
    out: Dict[str, Any] = {
        "enabled": True, "available": False, "passed": None, "blocked": False,
        "score": None, "reason": "", "issues": [], "critical_issues": [],
        "metrics": {}, "check_speech_ratio": False, "silent_shots": [],
        "shot_count": 0, "silence_intervals": [], "error": "",
        "path": os.path.abspath(path) if path else "",
    }
    m = metrics if metrics is not None else probe_audio_metrics(path, probe=probe)
    out["metrics"] = {k: (m or {}).get(k) for k in (
        "duration", "mean_db", "max_db", "silence_sec", "speech_ratio",
        "codec", "sample_rate", "channels")}
    if not (m or {}).get("available"):
        out["error"] = str((m or {}).get("error") or "未取到音频电平")
        return out
    out["available"] = True
    aq = _audio_qc_module()
    if aq is not None and hasattr(aq, "evaluate"):
        try:
            v = aq.evaluate(m, expect_sec=expect_sec, min_speech_ratio=None,
                            min_mean_db=min_mean_db, max_drift=max_drift) or {}
        except Exception as e:                               # noqa: BLE001
            v = {}
            out["error"] = "%s: %s" % (type(e).__name__, e)
        out["passed"] = v.get("passed")
        out["blocked"] = bool(v.get("blocked"))
        out["score"] = v.get("score")
        out["reason"] = str(v.get("reason") or "")[:300]
        out["issues"] = list(v.get("issues") or [])
        out["critical_issues"] = list(v.get("critical_issues") or [])
    else:
        out["reason"] = "audio_qc.evaluate 不可用（仅给出电平摘要，未判合格）"
    total = float(total_sec or out["metrics"].get("duration") or 0)
    rows = _shot_slots(shots, total) if (shots and total > 0) else []
    ivs = intervals if intervals is not None else (silence_intervals(path) if rows else None)
    out["silence_intervals"] = [[round(a, 3), round(a + d, 3)] for (a, d) in (ivs or [])][:200]
    silent: List[Dict[str, Any]] = []
    for r in rows:
        sil = sum(_overlap(r["start"], r["end"], a, a + d) for (a, d) in (ivs or []))
        sil = min(sil, r["duration"])
        ratio = max(0.0, 1.0 - (sil / r["duration"])) if r["duration"] > 0 else 1.0
        if ratio < AUDIO_HARD_SILENT_RATIO:
            silent.append({"shot_id": r["shot_id"], "start": r["start"], "end": r["end"],
                           "speech_ratio": round(ratio, 4), "silence_sec": round(sil, 3)})
    out["silent_shots"] = silent
    out["shot_count"] = len(rows)
    out["shot_level_defects"] = bool(silent)
    out["generated_at"] = _now()
    if silent:
        out["issues"] = list(out.get("issues") or []) + [
            "有 %d 个镜头整段无声（镜号 %s）：H3 该镜未出声，或音轨被补成了静音轨"
            % (len(silent), "、".join(str(s["shot_id"]) for s in silent[:12]))]
    return out


def final_audio_qc_path(project: str, episode: Any, *, root: Optional[str] = None) -> str:
    """成片音频客观层结论落点：<output>/qc/<项目>/<集>/final_audio_qc.json"""
    if root is None:
        from config import PROJECT_OUTPUT_DIR as root
    return os.path.join(root, "qc", str(project or ""), _norm_ep(episode),
                        "final_audio_qc.json")


def save_final_audio_qc(project: str, episode: Any, payload: Dict[str, Any],
                        *, root: Optional[str] = None) -> str:
    """落盘成片音频结论（交付清单据此定位到镜号）；失败只 warning 并返回空串。"""
    try:
        p = final_audio_qc_path(project, episode, root=root)
        atomic_write_json(p, dict(payload or {}))
        return p
    except Exception as e:                                   # noqa: BLE001
        logger.warning("成片音频结论落盘失败（不影响成片）：%s", e)
        return ""


def load_final_audio_qc(project: str, episode: Any, *,
                        root: Optional[str] = None) -> Dict[str, Any]:
    """读回成片音频结论（缺失/损坏 → {}，fail-open）。"""
    try:
        data = read_json_strict(final_audio_qc_path(project, episode, root=root), {}) or {}
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError) as e:
        logger.warning("成片音频结论读取失败（按无结论处理）：%s", e)
        return {}

# =====================================================================
# B 层：内容质检（复用既有 qc_client 结论，不重复造判据）
# =====================================================================

def evaluate_content(qc_result: Optional[dict]) -> Dict[str, Any]:
    """把既有质检结论映射为 B 层状态。

    * 未开启/未送检 → skipped（**不是 passed**：没检过不能说合格）
    * 接口不可用     → blocked（已声明要检但检不了，必须阻断而不是放行）
    * passed=True   → passed
    * passed=False  → failed
    """
    if not qc_result:
        return {"stage": "B", "name": STAGE_NAMES["B"], "label": STAGE_LABELS["B"],
                "status": "skipped", "note": "未送检（质检未开启），不得视为合格",
                "at": _now()}
    ok = qc_result.get("ok", _UNSET)
    passed = qc_result.get("passed", _UNSET)
    if ok is False and passed is _UNSET:
        status, note = "blocked", "质检接口不可用"
    elif passed is True:
        status, note = "passed", ""
    elif passed is False:
        status, note = "failed", str(qc_result.get("reason") or "")[:300]
    else:
        status, note = "pending", "质检结论不完整"
    return {"stage": "B", "name": STAGE_NAMES["B"], "label": STAGE_LABELS["B"],
            "status": status, "note": note,
            "evidence": {"score": qc_result.get("score"),
                         "passed": qc_result.get("passed"),
                         "ok": qc_result.get("ok")},
            "at": _now()}


# =====================================================================
# 状态机
# =====================================================================

def blank_state(project: str, episode: Any) -> Dict[str, Any]:
    """全新状态：四层全部 pending。"""
    return {
        "v": 1, "project": str(project or ""), "episode": str(episode),
        "stages": {s: {"stage": s, "name": STAGE_NAMES[s], "label": STAGE_LABELS[s],
                       "status": "pending"} for s in STAGES},
        "updated_at": _now(),
    }


def set_stage(state: Dict[str, Any], stage: str, status: str, *,
              evidence: Optional[dict] = None, note: str = "",
              binding: Optional[dict] = None) -> Dict[str, Any]:
    """把某层置为某状态，返回**新** state（纯函数，不修改入参）。

    状态集合之外的取值会被拒绝（保持状态机封闭，避免写入拼错的字符串）。
    """
    if stage not in STAGES:
        raise ValueError("未知阶段: %r（合法值 %s）" % (stage, list(STAGES)))
    if status not in STATUSES:
        raise ValueError("未知状态: %r（合法值 %s）" % (status, list(STATUSES)))
    out = copy.deepcopy(state or {})
    stages = out.setdefault("stages", {})
    entry = dict(stages.get(stage) or {"stage": stage, "name": STAGE_NAMES[stage],
                                       "label": STAGE_LABELS[stage]})
    entry["status"] = status
    entry["at"] = _now()
    if note:
        entry["note"] = note
    if evidence is not None:
        entry["evidence"] = evidence
    if binding is not None:
        entry["binding"] = binding
    stages[stage] = entry
    out["updated_at"] = _now()
    return out


def stage_status(state: Dict[str, Any], stage: str) -> str:
    return str(((state or {}).get("stages") or {}).get(stage, {}).get("status") or "pending")


def release_ready(state: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """能否发布：**D 必须 passed**，且任一层不得处于 failed/blocked。

    B 层 skipped 允许放行（用户没开质检），但会把提示带出来 —— 不假装检过了。
    """
    reasons: List[str] = []
    for s in STAGES:
        st = stage_status(state, s)
        if st in ("failed", "blocked"):
            reasons.append("%s(%s) 状态为 %s" % (s, STAGE_NAMES[s], st))
    if stage_status(state, "D") != "passed":
        reasons.append("D(release_approval) 尚未批准")
    if stage_status(state, "B") == "skipped":
        reasons.append("提示：B 层未送检，本次发布未经内容质检")
    return (not [r for r in reasons if not r.startswith("提示：")], reasons)


def state_path(project: str, episode: Any, *, root: Optional[str] = None) -> str:
    """状态文件：<PROJECT_OUTPUT_DIR>/qc/<项目>/<集>/quality_state.json。"""
    if root is None:
        from config import PROJECT_OUTPUT_DIR as root
    return os.path.join(root, "qc", str(project or ""), _norm_ep(episode),
                        "quality_state.json")


def load_state(project: str, episode: Any, *, root: Optional[str] = None) -> Dict[str, Any]:
    """读状态；不存在/损坏一律返回 blank（fail-open，绝不阻断出片）。"""
    try:
        data = read_json_strict(state_path(project, episode, root=root), {}) or {}
        if isinstance(data, dict) and data.get("stages"):
            return data
    except Exception as e:                                   # noqa: BLE001
        logger.warning("质量状态读取失败（按全新状态处理）：%s", e)
    return blank_state(project, episode)


def save_state(project: str, episode: Any, state: Dict[str, Any], *,
               root: Optional[str] = None) -> bool:
    """原子落盘；失败只 warning 并返回 False（状态是观测面，不是生产依赖）。"""
    try:
        atomic_write_json(state_path(project, episode, root=root), state)
        return True
    except Exception as e:                                   # noqa: BLE001
        logger.warning("质量状态落盘失败（不影响出片）：%s", e)
        return False


def record_stage(project: str, episode: Any, stage: str, status: str, *,
                 evidence: Optional[dict] = None, note: str = "",
                 root: Optional[str] = None,
                 binding: Optional[dict] = None) -> Dict[str, Any]:
    """便捷入口：读 → 置层 → 写，返回新状态。"""
    st = set_stage(load_state(project, episode, root=root), stage, status,
                   evidence=evidence, note=note, binding=binding)
    save_state(project, episode, st, root=root)
    return st


def stage_binding(state: Dict[str, Any], stage: str) -> Optional[dict]:
    """取某层批准时钉下的产物绑定；无则 None。"""
    return ((state or {}).get("stages") or {}).get(stage, {}).get("binding")


def check_stage_binding(state: Dict[str, Any], stage: str, path: str, *,
                        contract: Any = None,
                        qa_report: Any = None) -> Tuple[str, str]:
    """校验某层批准绑定是否仍成立，返回 (status, reason)。

    无绑定（老数据 / 未绑定）→ unbound，**不误判为失效**；调用方据此可在界面上
    区分「尚未批准」与「批准已失效（产物被重做）」两种状态。
    """
    b = stage_binding(state, stage)
    return check_binding(b, path, contract=contract, qa_report=qa_report)


# =====================================================================
# 哈希绑定（批准必须钉在具体内容上）
# =====================================================================

def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """流式 sha256（不整文件读进内存）。失败返回空串（调用方按 unbound 处理）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except (OSError, TypeError, ValueError) as e:
        logger.warning("产物哈希计算失败（按无法绑定处理）：%s", e)
        return ""


def hash_obj(obj: Any) -> str:
    """任意 JSON-able 对象的稳定哈希；不可序列化时返回空串。"""
    if obj is None:
        return ""
    try:
        blob = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:
        logger.warning("对象哈希计算失败：%s", e)
        return ""
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:32]


def file_signature(path: str, *, with_hash: bool = True) -> Dict[str, Any]:
    """产物签名：size+mtime 永远有（快路径），sha256 可选。

    size+mtime 是为了**避免每次校验都哈希大文件**：成片动辄几百 MB，
    在列表接口里逐个哈希会直接把界面拖死。只有 size/mtime 变了才需要真哈希。
    """
    sig: Dict[str, Any] = {"size": 0, "mtime": 0.0, "sha256": ""}
    try:
        stt = os.stat(path)
        sig["size"] = int(stt.st_size)
        sig["mtime"] = round(float(stt.st_mtime), 3)
    except (OSError, TypeError):
        return sig
    if with_hash:
        sig["sha256"] = sha256_file(path)
    return sig


def make_binding(path: str, *, contract: Any = None, qa_report: Any = None,
                 with_hash: bool = True) -> Dict[str, Any]:
    """构造批准绑定：产物签名 + 合同哈希 + 质检报告哈希。

    三者在**批准的那一刻**取快照。之后任一变一动，批准即失效。
    """
    sig = file_signature(path, with_hash=with_hash)
    return {
        "artifact_sha256": sig.get("sha256") or "",
        "artifact_size": sig.get("size"),
        "artifact_mtime": sig.get("mtime"),
        "contract_hash": hash_obj(contract),
        "qa_report_hash": hash_obj(qa_report),
        "bound_at": _now(),
    }


def check_binding(binding: Optional[dict], path: str, *,
                  contract: Any = None, qa_report: Any = None) -> Tuple[str, str]:
    """校验绑定，返回 (status, reason)。

    status ∈ {valid, invalid, unbound}
      * unbound —— 没有绑定信息（老数据/未绑定）：**不误判为失效**
      * valid   —— 三者都对得上
      * invalid —— 任一变了，附上具体是哪一项变了

    快路径：size 与 mtime 都没变 → 直接判 valid，不做 sha256（大文件友好）。
    """
    if not isinstance(binding, dict) or not binding:
        return "unbound", "无绑定信息（未绑定或旧数据）"

    try:
        stt = os.stat(path)
    except (OSError, TypeError):
        return "invalid", "产物已不存在或不可读"

    if binding.get("artifact_size") is not None and int(binding["artifact_size"]) != int(stt.st_size):
        return "invalid", ("产物内容已变（大小 %s → %s）"
                           % (binding.get("artifact_size"), stt.st_size))
    if binding.get("artifact_mtime") is not None and \
            abs(float(binding["artifact_mtime"]) - float(stt.st_mtime)) > 0.001:
        return "invalid", "产物已被重新写入（mtime 变化）"

    want = binding.get("artifact_sha256") or ""
    if want:
        got = sha256_file(path)
        if not got:
            return "unbound", "产物哈希本次算不出，无法确认（不误判失效）"
        if got != want:
            return "invalid", "产物内容指纹不匹配（内容已被替换）"

    bound_c = binding.get("contract_hash") or ""
    if bound_c and contract is not None:
        if hash_obj(contract) != bound_c:
            return "invalid", "合同已变（镜头数/时长等约定被修改）"
    bound_q = binding.get("qa_report_hash") or ""
    if bound_q and qa_report is not None:
        if hash_obj(qa_report) != bound_q:
            return "invalid", "质检报告已变（批准依据的结论已不是当次那份）"

    return "valid", "绑定一致"
