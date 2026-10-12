# -*- coding: utf-8 -*-
"""音频质检客观层（ffmpeg 指标 + 频谱/波形可视化）

## 为什么音频必须独立一层

图片/视频可以直接交给多模态模型「看」，音频不行 —— 模型听不到声音。如果把音频质检
整个交给 AI 层，就会出现两种失败：模型对着一张空白图瞎猜，或者干脆判定「无法判断」。
所以音频质检拆成两层：

1. **客观层（本模块）**：只用 ffmpeg 量出来的数字做硬判定，零模型依赖、零成本、
   确定性、毫秒级。它负责挡掉「必然不可用」的音频：**整段无声 / 近乎无声 / 空文件 /
   不含音频流**。这一层永远执行，没配质检接口的项目也能享受。
2. **AI 层（见 ``qc_client.check_audio``）**：把本模块渲染出的**频谱图 + 波形图**当成
   「图片」交给多模态模型，由它判读内容层面的问题（人声能量分布异常、周期爆破、断续、
   与台词时长明显不符等）。模型看不到声音，但看得到声音的形状 —— 这是当前技术条件下
   唯一可用的折中。

## 历史缺陷（本模块存在的原因）

2026-09-19 审计发现：``qc_client`` 里 ``audio_enabled`` / ``audio_prompt`` /
``audio_min_speech_ratio`` / ``audio_min_mean_db`` / ``audio_max_drift`` 五个配置键、
``DEFAULT_AUDIO_PROMPT`` 提示词、六个 ``AUDIO_*`` 阈值常量**全部零消费** ——
配置项、阈值、提示词都写好了，但没有任何代码读它们，前端「音频质检」页写着
「功能正在开发中」。也就是说用户打开音频质检开关、调阈值，什么都不会发生。
本模块与 ``qc_client.check_audio`` 一起把这半条链路接上。

## 阈值来源

硬阈值（无声/削波/空文件）是**物理判据**，与剧集无关，因此写成本模块常量；
可调阈值（有声占比下限、平均电平下限、时长偏差上限）由用户在质检配置里按音色与语速
整体调档，以参数形式传入 ``evaluate()``。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: ffmpeg 可执行文件的显式覆盖（环境变量优先，其次 PATH）
FFMPEG_ENV = "MJSCXT_FFMPEG"

# --------------------------------------------------------------------------- #
# 硬阈值：物理判据，不随剧集/音色变化
# --------------------------------------------------------------------------- #

#: 静音判定门限（低于该电平视为静音），交给 ffmpeg silencedetect 的 noise 参数
SILENCE_THRESHOLD_DB = -35.0
#: 最短静音段（秒）：避免把正常换气、句读停顿当成静音
SILENCE_MIN_DURATION = 0.35
#: 平均电平低于此值 → 视为近乎无声（硬闸）。实测纯静音轨为 -91 dB
#: ⚠️ 与 quality_stage.AUDIO_NEAR_SILENT_DB 是**同一判据的两份定义**（作用域：单段 / 整轨）。
#:   两处同值已由 config_doctor.REGISTRY 显式声明（near_silent_mean_db 是 owner，
#:   audio_near_silent_db.must_match 指回本条）；改这条请同步改那边，否则 doctor 报 duplicates 且 ok=False。
NEAR_SILENT_MEAN_DB = -50.0
#: 峰值电平高于此值 → 削波失真风险（软扣分项，不阻断）
CLIP_MAX_DB = -0.1
#: 有效音频最短时长（秒），更短视为空文件 / 合成失败（硬闸）
MIN_VALID_DURATION = 0.15
#: 有声占比低于此值 → 视为整段无声（硬闸）
#: ⚠️ 与 quality_stage.AUDIO_HARD_SILENT_RATIO 同值锁定（REGISTRY audio_hard_silent_ratio.must_match）。
HARD_SILENT_RATIO = 0.15
#: 单次 ffmpeg 指标探测超时（秒）
PROBE_TIMEOUT = 300
#: 单次可视化渲染超时（秒）
RENDER_TIMEOUT = 300

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_MEAN_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_MAX_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SIL_DUR_RE = re.compile(r"silence_duration:\s*(\d+(?:\.\d+)?)")
_CODEC_RE = re.compile(r"Audio:\s*([A-Za-z0-9_]+)")
_SR_RE = re.compile(r"(\d+)\s*Hz")
_CH_RE = re.compile(r"\b(mono|stereo|quad)\b")
#: 「没有可用的音频流」类报错（不同 ffmpeg 版本措辞不同，逐条覆盖）
_NO_AUDIO_HINTS = (
    "matches no streams", "does not contain any stream",
    "Output file #0 does not contain any stream", "could not find codec parameters",
)


def ffmpeg_exe() -> str:
    return os.environ.get(FFMPEG_ENV) or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_exe() -> str:
    return shutil.which("ffprobe") or ""


def ffmpeg_available() -> bool:
    """ffmpeg 是否可用（实测，不猜测）"""
    try:
        p = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True, timeout=20)
        return p.returncode == 0
    except Exception:  # noqa: BLE001 - 环境相关
        return False


def _decode_io(raw) -> str:
    """subprocess 原始字节 → 文本。

    Windows 下 ``text=True`` 会按 locale(cp936) 解码 ffmpeg 的 UTF-8 输出，遇到非 GBK
    字节抛 UnicodeDecodeError 被外层吞掉，导致指标静默解析为 0（``qc_client._decode_io``
    同一成因，两处各自保留是因为 qc_client 不能反向依赖本模块）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def _channel_count(token: str) -> int:
    return {"mono": 1, "stereo": 2, "quad": 4}.get(token or "", 0)


def _blank_metrics(error: str = "") -> Dict:
    return {"ok": False, "error": error, "duration": 0.0,
            "mean_db": None, "max_db": None, "silence_sec": 0.0,
            "speech_ratio": None, "codec": "", "sample_rate": 0, "channels": 0,
            "size_bytes": 0}


# --------------------------------------------------------------------------- #
# 客观指标
# --------------------------------------------------------------------------- #

def probe_metrics(path: str) -> dict:
    """ffmpeg 一次解码取全部客观指标（永不抛异常）。

    返回::

        {"ok": bool, "error": str,
         "duration": float,            # 秒
         "mean_db": float|None,        # 平均电平
         "max_db": float|None,         # 峰值电平
         "silence_sec": float,         # 静音总时长
         "speech_ratio": float|None,   # 有声占比 = 1 - 静音/总时长
         "codec": str, "sample_rate": int, "channels": int,
         "size_bytes": int}

    实现要点：``silencedetect`` 与 ``volumedetect`` 串在同一条 ``-af`` 链上，只解码一次；
    ⚠️ volumedetect 在滤镜图协商阶段会先打印一次 ``n_samples: 0`` 的空实例，
    因此 ``mean_volume`` / ``max_volume`` **必须取最后一次匹配**，否则拿到的是空值。
    """
    info = _blank_metrics()
    if not path or not os.path.isfile(path):
        info["error"] = f"音频文件不存在：{path}"
        return info
    info["size_bytes"] = os.path.getsize(path)
    if not ffmpeg_available():
        info["error"] = "未找到 ffmpeg（请安装并加入 PATH，或用 MJSCXT_FFMPEG 指定路径）"
        return info

    af = (f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={SILENCE_MIN_DURATION},"
          f"volumedetect")
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", path,
           "-map", "0:a:0", "-af", af, "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        info["error"] = f"ffmpeg 指标探测超时（>{PROBE_TIMEOUT}s）"
        return info
    except Exception as e:  # noqa: BLE001 - 环境相关
        info["error"] = f"ffmpeg 调用失败：{type(e).__name__}: {e}"
        return info
    txt = _decode_io(p.stderr) + "\n" + _decode_io(p.stdout)

    if any(h in txt for h in _NO_AUDIO_HINTS):
        info["error"] = "文件不含音频流（无法做音频质检）"
        return info
    if "Invalid data found" in txt or "No such file" in txt:
        info["error"] = "文件无法解码（可能损坏或不是音频/视频文件）"
        return info

    m = _DURATION_RE.search(txt)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                 + float(m.group(3)), 3)
    else:
        info["duration"] = round(_ffprobe_duration(path), 3)

    means = _MEAN_RE.findall(txt)
    maxs = _MAX_RE.findall(txt)
    if means:
        info["mean_db"] = round(float(means[-1]), 2)
    if maxs:
        info["max_db"] = round(float(maxs[-1]), 2)

    # 解析不出电平 = 没有真正解码到音频 → 判失败。
    # ⚠️ 这里曾漏判：不含音频流的 mp4 仍能解析出 Duration，于是 duration>0 就被当成
    #    「ok」放过去，speech_ratio 还会被伪造成 1.0（无声却判合格）。
    if info["mean_db"] is None:
        info["error"] = "未解析到音频电平（文件可能不含音频流，或音轨为纯数据流）"
        return info

    # 静音段：silence_start 与 silence_duration 交替出现；末尾未闭合时用总时长兜底
    dur = float(info["duration"] or 0)
    starts = [float(x) for x in _SIL_START_RE.findall(txt)]
    durs = [float(x) for x in _SIL_DUR_RE.findall(txt)]
    if len(durs) < len(starts):
        durs = durs + [max(0.0, dur - starts[-1]) if dur else 0.0]
    info["silence_sec"] = round(min(sum(durs), dur) if dur else sum(durs), 3)
    if dur > 0:
        info["speech_ratio"] = round(max(0.0, 1.0 - info["silence_sec"] / dur), 4)

    mc = _CODEC_RE.search(txt)
    if mc:
        info["codec"] = mc.group(1)
    ms = _SR_RE.search(txt)
    if ms:
        info["sample_rate"] = int(ms.group(1))
    mh = _CH_RE.search(txt)
    if mh:
        info["channels"] = _channel_count(mh.group(1))

    info["ok"] = True
    return info


def _ffprobe_duration(path: str) -> float:
    exe = ffprobe_exe()
    if not exe:
        return 0.0
    try:
        p = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, timeout=60)
        raw = _decode_io(p.stdout).strip().splitlines()
        return float(raw[0]) if raw and raw[0].strip() else 0.0
    except Exception:  # noqa: BLE001 - 环境相关
        return 0.0


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

def evaluate(metrics: dict, expect_sec: Optional[float] = None,
             min_speech_ratio: Optional[float] = 0.50,
             min_mean_db: Optional[float] = -45.0,
             max_drift: Optional[float] = 0.50) -> dict:
    """客观层判定（纯函数，永不抛异常）。

    ``expect_sec``：期望时长（单句取台词推算时长，整轨取视频时长）。为空则不做时长偏差判定。

    ``min_speech_ratio=None`` 表示**关闭有声占比判定** —— 整集/成片音轨本来就有大量
    刻意留白（无台词镜头），拿单句标准去卡它必然误判，这类调用方应显式关闭。

    返回与 ``check_image`` / ``check_video`` 同构的 verdict::

        {"ok", "skipped": False, "passed", "accepted", "blocked", "score",
         "issues", "critical_issues", "reason", "metrics", "objective_only": True}
    """
    metrics = metrics or {}
    issues: List[str] = []
    fatal: List[str] = []

    if not metrics.get("ok"):
        fatal.append(metrics.get("error") or "无法读取音频指标（文件缺失或不可解码）")
        return _verdict(issues, fatal, metrics, expect_sec)

    dur = float(metrics.get("duration") or 0.0)
    mean_db = metrics.get("mean_db")
    max_db = metrics.get("max_db")
    ratio = metrics.get("speech_ratio")

    # ---- 硬闸：必然不可用 ----
    if dur < MIN_VALID_DURATION:
        fatal.append(f"音频时长仅 {dur:.3f}s（< {MIN_VALID_DURATION}s）：合成失败或文件为空")
    elif isinstance(ratio, float) and ratio < HARD_SILENT_RATIO:
        fatal.append(f"整段近乎无声（有声占比 {ratio * 100:.1f}% < "
                     f"{HARD_SILENT_RATIO * 100:.0f}%）：漏配音或 TTS 未发声")
    elif isinstance(mean_db, float) and mean_db < NEAR_SILENT_MEAN_DB:
        fatal.append(f"平均电平 {mean_db:.1f}dB 低于 {NEAR_SILENT_MEAN_DB:.0f}dB：整段近乎无声")

    # ---- 软扣分：可调阈值 ----
    if min_speech_ratio is not None and isinstance(ratio, float) \
            and ratio < float(min_speech_ratio) and not fatal:
        issues.append(f"有声占比 {ratio * 100:.1f}% 低于下限 "
                      f"{float(min_speech_ratio) * 100:.0f}%：静音过多，可能存在漏句")
    if min_mean_db is not None and isinstance(mean_db, float) \
            and mean_db < float(min_mean_db) and not fatal:
        issues.append(f"平均电平 {mean_db:.1f}dB 低于下限 {float(min_mean_db):.1f}dB：音量偏小")
    if isinstance(max_db, float) and max_db > CLIP_MAX_DB:
        issues.append(f"峰值电平 {max_db:.2f}dB 已触顶：增益过大，存在削波爆音风险")
    if max_drift is not None and expect_sec and float(expect_sec) > 0 and dur > 0:
        drift = abs(dur - float(expect_sec)) / float(expect_sec)
        if drift > float(max_drift):
            issues.append(f"时长偏差 {drift * 100:.0f}% 超过上限 "
                          f"{float(max_drift) * 100:.0f}%（实测 {dur:.2f}s / "
                          f"期望 {float(expect_sec):.2f}s）：疑似被截断或补了空白")

    return _verdict(issues, fatal, metrics, expect_sec)


def _verdict(issues: List[str], fatal: List[str], metrics: dict,
             expect_sec: Optional[float]) -> dict:
    score = max(0, min(100, 100 - 15 * len(issues) - 45 * len(fatal)))
    blocked = bool(fatal)
    passed = (not fatal) and (not issues)
    if fatal:
        reason = "致命缺陷：" + "；".join(fatal[:2])
    elif issues:
        reason = "存在可优化项：" + "；".join(issues[:2])
    else:
        parts = []
        if metrics.get("duration"):
            parts.append(f"时长 {float(metrics['duration']):.2f}s")
        if isinstance(metrics.get("speech_ratio"), float):
            parts.append(f"有声占比 {float(metrics['speech_ratio']) * 100:.1f}%")
        if isinstance(metrics.get("mean_db"), float):
            parts.append(f"平均电平 {float(metrics['mean_db']):.1f}dB")
        reason = ("客观指标正常（" + "，".join(parts) + "）") if parts else "客观指标正常"
    return {
        "ok": bool(metrics.get("ok")),
        "skipped": False,
        "objective_only": True,
        "passed": passed,
        # 软扣分项不阻断（与图片质检口径一致：只有关键缺陷才 blocked）
        "accepted": not fatal,
        "blocked": blocked,
        "score": score,
        "issues": issues,
        "critical_issues": fatal,
        "reason": reason[:500],
        "metrics": metrics,
        "expect_sec": float(expect_sec) if expect_sec else None,
        "silence_threshold_db": SILENCE_THRESHOLD_DB,
        "silence_min_duration": SILENCE_MIN_DURATION,
    }


def quick_check(path: str, expect_sec: Optional[float] = None,
                min_speech_ratio: Optional[float] = 0.50,
                min_mean_db: Optional[float] = -45.0,
                max_drift: Optional[float] = 0.50) -> dict:
    """探测 + 判定（客观层一条龙，永不抛异常）"""
    return evaluate(probe_metrics(path), expect_sec=expect_sec,
                    min_speech_ratio=min_speech_ratio, min_mean_db=min_mean_db,
                    max_drift=max_drift)


# --------------------------------------------------------------------------- #
# 可视化（喂给多模态模型当「眼睛」）
# --------------------------------------------------------------------------- #

def render_visuals(path: str, out_dir: str, prefix: str = "audio") -> dict:
    """渲染频谱图与波形图（PNG），供 AI 层送检。

    返回 ``{"ok": bool, "images": [频谱图, 波形图], "error": str}``。
    顺序固定为「先频谱、后波形」，与 ``DEFAULT_AUDIO_PROMPT`` 里对第 1/2 张图的描述
    一一对应 —— 顺序错了模型会把波形当频谱读。
    """
    result = {"ok": False, "images": [], "error": ""}
    if not path or not os.path.isfile(path):
        result["error"] = f"音频文件不存在：{path}"
        return result
    if not ffmpeg_available():
        result["error"] = "未找到 ffmpeg，无法渲染频谱/波形图"
        return result
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"可视化目录创建失败：{e}"
        return result

    safe = re.sub(r"[^0-9A-Za-z_\-]", "_", str(prefix or "audio"))[:48] or "audio"
    plans = [
        # 频谱图：横轴时间、纵轴频率、亮度=能量
        (f"{safe}_spectrum.png", "showspectrumpic=s=640x360:legend=1:scale=log"),
        # 波形图：横轴时间、纵轴振幅（亮色便于看清削波与零线）
        (f"{safe}_waveform.png", "showwavespic=s=640x240:colors=0x2f7cf6"),
    ]
    images: List[str] = []
    for name, lavfi in plans:
        out = os.path.join(out_dir, name)
        # ⚠️ 必须用 filter_complex + 显式 [0:a:0] 取音频流：
        #    `-map 0:a:0 -lavfi X` 会同时产出「原始音频流」与「滤镜输出流」两个输出，
        #    音频流没有指定编码器 → "Automatic encoder selection failed / codec none"。
        #    显式 [0:a:0] → [o] → -map [o] 则输入是纯音频或带音轨的 mp4 都成立。
        # ⚠️ `-update 1` 不能省：写单张 .png（文件名非 %03d 序列）时 ffmpeg 会报
        #    「does not contain an image sequence pattern」并写入失败。
        cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-v", "error",
               "-i", path,
               "-filter_complex", f"[0:a:0]{lavfi}[o]", "-map", "[o]",
               "-frames:v", "1", "-update", "1", out]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=RENDER_TIMEOUT)
            err = _decode_io(p.stderr).strip()[:200]
        except Exception as e:  # noqa: BLE001 - 环境相关
            result["error"] = f"{name} 渲染失败：{type(e).__name__}: {e}"
            return result
        if p.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) <= 0:
            result["error"] = f"{name} 渲染失败：{err or '未产出图片'}"
            return result
        images.append(os.path.abspath(out))

    result["ok"] = True
    result["images"] = images
    return result


def estimate_speech_sec(text: str, chars_per_sec: float = 4.5) -> float:
    """由台词文本推算期望配音时长（秒）。

    中文配音自然语速约 4~5 字/秒，取 4.5 作为默认；标点与空白不计入字数。
    这是**估算**，只用于「时长偏差过大」这一条软判据，因此宁松勿紧：
    返回下限 0.6s，避免短句被算出极端小的期望值而误判；无有效字符时返回 0.0
    （调用方据此可判「这句根本没内容」）。
    """
    s = re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE)
    n = len(s)
    if n <= 0:
        return 0.0
    rate = float(chars_per_sec) if chars_per_sec and chars_per_sec > 0 else 4.5
    return round(max(0.6, n / rate), 3)
