# -*- coding: utf-8 -*-
"""caption 烧录回读校对（本地 whisper.cpp ASR）——「成片音轨转写 ↔ 剧本应烧字幕」比对

为什么需要
----------
成片里烧录的 caption（时空落点 / 时空回溯 / 集尾悬念）是**剧情装置**
（config.caption_burn_enabled，默认开）：烧错、烧丢、烧成别的内容，观众就会看到
无过渡的跳切。烧录链路（pipeline.step_final / video_postprocess.generate_final_video）
对「该烧什么」有单一口径，但对「实际烧进去的成片」此前零校验——本模块用本机
whisper.cpp（参考 D:\\Moha\\whisper 的命令行方案）把成片音轨转写回文本，
与剧本里的应烧文本做规范化比对，给出相似度与差异片段，供人工/托管抽查。

口径来源（刻意全部复用既有实现，不自造第二套）：
  · 成片定位  → pipeline.upscale_path / pipeline.final_path 同名规则
                （UPSCALE_DIR/<项目>/ep{NN}_upscaled.mp4 优先，其次
                 FINAL_DIR/<项目>/ep{NN}_final.mp4）；
  · ffmpeg    → 与 video_postprocess 同一来源（裸 `ffmpeg` = PATH 探测，
                 这里用 shutil.which 等价实现）；
  · 字幕文本  → 与 pipeline.step_final 的 _cap_text / add_subtitles 同一来源：
                shots[].caption（兼容 {text,kind} 结构 / 旧字符串）+ caption_text 扁平兜底；
                若项目开了 subtitle_enabled（台词字幕，默认关）则把
                dialogue_utils.dialogue_text(shots[].dialogue) 一并计入；
  · 开关读取  → 直接复用 video_postprocess._caption_burn_enabled_for /
                _subtitle_enabled_for（项目 config.json，默认 caption=开 / 台词=关）；
  · 剧本定位  → novel_to_script.episode_script_path（output/scripts/<项目键>/第N集.json）；
  · 报告落盘  → fs_atomic.atomic_write_json（原子写 + .bak 快照）。

环境变量（env 优先，缺省探测本机已知安装位置）：
  MJSCXT_WHISPER_EXE               whisper.cpp 命行可执行文件（缺省探测
                                   D:\\Moha\\whisper\\whisper-cli.exe，再退该目录下的 *.exe）
  MJSCXT_WHISPER_MODEL             whisper ggml 语言模型（缺省探测 D:\\Moha\\model\\ggml-*.bin，
                                   **排除 silero / vad 文件名**——那是 VAD 模型不是转写模型；
                                   多个命中取 mtime 最新）
  MJSCXT_CAPTION_VERIFY_THRESHOLD  ok 判定相似度阈值，默认 0.60（0~1）

失败语义：verify_episode 全程 try/except 收口，任何一步失败都返回
``{ok: False, error: ...}``，绝不向上抛异常；任务线程同样只更新状态不抛。
"""
from __future__ import annotations

import difflib
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from config import PROJECT_OUTPUT_DIR, UPSCALE_DIR, FINAL_DIR, SCRIPT_DIR
from video_postprocess import (_subtitle_enabled_for, _caption_burn_enabled_for)
# 2026-10-08 解耦：ffprobe 探测走叶子模块 media_probe，不再经由后期模块中转
from media_probe import probe_media
from dialogue_utils import dialogue_text
from fs_atomic import atomic_write_json

logger = logging.getLogger(__name__)

__all__ = ["available", "verify_episode", "start_verify", "get_result",
           "report_path", "locate_episode_video", "load_episode_script"]

# ===================== 配置（env 优先，缺省探测） =====================

_WHISPER_EXE_ENV = "MJSCXT_WHISPER_EXE"
_WHISPER_MODEL_ENV = "MJSCXT_WHISPER_MODEL"
_THRESHOLD_ENV = "MJSCXT_CAPTION_VERIFY_THRESHOLD"

# ⭐ 2026-10-09 项目自带（用户要求：不再依赖 D:\Moha 等其他项目）：
#   随应用分发的 whisper.cpp 放在 <resources>/whisper/ 下，**优先**探测；
#   D:\Moha\... 仅作历史回退（老安装目录里可能已有可用副本）。
#   目录布局：<resources>/whisper/bin/Release/whisper-cli.exe（含 ggml-*.dll）
#            <resources>/whisper/models/ggml-base.bin
_APP_DIR = os.path.dirname(os.path.abspath(__file__))     # .../resources/backend
_RES_DIR = os.path.dirname(_APP_DIR)                      # .../resources
_LOCAL_WHISPER_DIR = os.path.join(_RES_DIR, "whisper")
_LOCAL_EXE_DIR = os.path.join(_LOCAL_WHISPER_DIR, "bin", "Release")
_LOCAL_MODEL_DIRS = (os.path.join(_LOCAL_WHISPER_DIR, "models"), _LOCAL_EXE_DIR)

#: whisper.cpp 命令行：项目内优先，其次历史安装位置
_DEFAULT_EXE = os.path.join(_LOCAL_EXE_DIR, "whisper-cli.exe")
_DEFAULT_EXE_DIRS = (_LOCAL_EXE_DIR, r"D:\Moha\whisper")
_DEFAULT_EXE_DIR = _DEFAULT_EXE_DIRS[0]      # 兼容旧引用（只指项目内）
#: ggml 语言模型缺省探测目录（项目内优先）。⚠️ silero/vad 是 VAD 模型不是转写模型，
#: 探测时按文件名排除（见 whisper_model）。
_DEFAULT_MODEL_DIRS = _LOCAL_MODEL_DIRS + (r"D:\Moha\model", r"D:\Moha\whisper")

_AUDIO_SAMPLE_RATE = 16000      # whisper.cpp 期望 16kHz 单声道 PCM wav
_FFMPEG_TIMEOUT_SEC = 600       # 抽音轨超时（整集片一般几十 MB~几百 MB，600s 很宽裕）
_WHISPER_TIMEOUT_SEC = 900      # 转写超时（任务书指定值）
_SIMILARITY_OK_DEFAULT = 0.60   # ok 粗判阈值（可被 MJSCXT_CAPTION_VERIFY_THRESHOLD 覆盖）

_SEGMENTS_MAX = 10              # missing/extra 片段最多各列 10 条
_SEG_MAX_CHARS = 80             # 单条差异片段展示截断长度
_PREVIEW_CHARS = 500            # transcript_preview 取转写原文前 500 字

#: whisper 常见的「训练残留」套话（zh 模型偶尔会输出「字幕由…/谢谢观看」等），
#: 不在剧本里 → 会虚增 missing_segments，规范化前先剥掉。
_ASR_NOISE_RE = re.compile(
    r"(字幕由.{0,24}提供|字幕[由制]作[:：]?|amara\.?org|谢谢观看|请订阅|"
    r"订阅频道|点赞关注|by\s*amara)",
    re.IGNORECASE)


# ===================== 三件套探测 =====================

def whisper_exe() -> str:
    """whisper.cpp 可执行文件：env 指定优先；否则探测已知安装目录。找不到返回 ""。"""
    env = (os.environ.get(_WHISPER_EXE_ENV) or "").strip()
    if env:
        return env                       # env 指定即采用；存在性由 available() 校验并给 reason
    if os.path.isfile(_DEFAULT_EXE):
        return _DEFAULT_EXE
    # 逐目录探测：项目内 → 历史安装位置（glob 顺序即优先级）。
    for _d in _DEFAULT_EXE_DIRS:
        for _pat in ("whisper-cli*.exe", "*.exe"):
            try:
                _hits = sorted(glob.glob(os.path.join(_d, _pat)))
            except Exception:            # noqa: BLE001  探测失败视为未找到
                _hits = []
            if _hits:
                return _hits[0]
    return ""


def whisper_model() -> str:
    """whisper ggml 语言模型：env 指定优先；否则探测 ggml-*.bin（排除 VAD），取最新。"""
    env = (os.environ.get(_WHISPER_MODEL_ENV) or "").strip()
    if env:
        return env
    best, best_mtime = "", -1.0
    for d in _DEFAULT_MODEL_DIRS:
        try:
            hits = glob.glob(os.path.join(d, "ggml-*.bin"))
        except Exception:                # noqa: BLE001
            hits = []
        for p in hits:
            base = os.path.basename(p).lower()
            # ⚠️ silero / vad 是**语音活动检测**模型（whisper-cli -vm 用），不是语言模型：
            #    拿去当 -m 会被 whisper.cpp 拒绝或产出空文本，必须按文件名排除。
            if "silero" in base or "vad" in base:
                continue
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = p, m
    return best


def ffmpeg_exe() -> str:
    """ffmpeg 探测：与 video_postprocess 同一来源（那里裸调 `ffmpeg` = PATH 查找，
    shutil.which 是同一语义的显式形式）。不可用返回 ""。"""
    return shutil.which("ffmpeg") or ""


def available() -> dict:
    """校对环境三件套（whisper-cli / ggml 模型 / ffmpeg）是否齐全。

    Returns:
        {available: bool, reasons: [中文原因], exe, model, ffmpeg}；
        三件齐才 available=True。**缺模型是最常见形态**：D:\\Moha\\model\\ 目前只有
        VAD 模型时，这里如实报「未找到语言模型」并给出 env 指定指引。
    """
    exe, model, ffmpeg = whisper_exe(), whisper_model(), ffmpeg_exe()
    reasons = []
    if not exe:
        reasons.append(f"未找到 whisper-cli 可执行文件（缺省探测 {_DEFAULT_EXE}；"
                       f"可用环境变量 {_WHISPER_EXE_ENV} 指定）")
    elif not os.path.isfile(exe):
        reasons.append(f"{_WHISPER_EXE_ENV} 指定的可执行文件不存在：{exe}")
    if not model:
        reasons.append(
            f"未找到 whisper ggml 语言模型（已扫描 "
            + "、".join(_DEFAULT_MODEL_DIRS) +
            r" 下的 ggml-*.bin；该目录里的 silero/vad 文件是 VAD 模型，不能用作转写模型；"
            f"可用环境变量 {_WHISPER_MODEL_ENV} 指定）")
    elif not os.path.isfile(model):
        reasons.append(f"{_WHISPER_MODEL_ENV} 指定的模型不存在：{model}")
    if not ffmpeg:
        reasons.append("ffmpeg 不可用（PATH 中未找到；音轨抽取依赖它，"
                       "与视频后期模块同一依赖）")
    return {"available": not reasons, "reasons": reasons,
            "exe": exe, "model": model, "ffmpeg": ffmpeg}


def _ok_threshold() -> float:
    raw = (os.environ.get(_THRESHOLD_ENV) or "").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _SIMILARITY_OK_DEFAULT
    return min(1.0, max(0.0, v))


# ===================== 小工具 =====================

def _norm_ep(episode_no) -> int:
    try:
        ep = int(episode_no)
    except (TypeError, ValueError):
        ep = 1
    return max(1, ep)


def _safe_key(project: str) -> str:
    """项目名安全化：与 app._safe_project / project_store.safe_key 同一事实源；
    project_store 不可用时退化为本地同规则清洗（绝不让校对因为键清洗挂掉）。"""
    p = str(project or "").strip()
    if not p:
        return ""
    try:
        import project_store            # 延迟导入，避免与 app 的导入顺序耦合
        return str(project_store.safe_key(p) or "")
    except Exception:                   # noqa: BLE001
        k = re.sub(r'[\\/:*?"<>|\s]+', "_", p)
        return k[:40] or ""


def report_path(project: str, episode_no) -> str:
    """校对报告落盘路径：PROJECT_OUTPUT_DIR/caption_verify/<项目>/ep{NN}.json"""
    return os.path.join(PROJECT_OUTPUT_DIR, "caption_verify", _safe_key(project),
                        f"ep{_norm_ep(episode_no):02d}.json")


# ===================== 成片 / 剧本定位（与生产链路同口径） =====================

def _video_ok(path: str) -> tuple:
    """成片可用判据：存在 + 非空（+ 能探测到视频流；ffprobe 缺失时降级为存在性判定，
    与 pipeline._playable 的降级策略同一精神——不能让工具链不齐误判全部未就绪）。"""
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False, "文件不存在或为空"
    except OSError as e:
        return False, f"stat 失败：{e}"
    info = probe_media(path)
    if info.get("ok") and not info.get("has_video"):
        return False, "探测不到视频流"
    return True, ""


def locate_episode_video(project: str, episode_no):
    """定位该集「应校对的成片」：优先超分产物，其次成片目录（与 pipeline 同名规则）。

    Returns:
        (video_path, source, error)：source ∈ {"upscale", "final"}；找不到返回
        ("", "", 明确错误)，错误里列出已检查的路径，绝不静默猜一个。
    """
    ep = _norm_ep(episode_no)
    p = _safe_key(project)
    if not p:
        return "", "", "项目名为空，无法定位成片"
    checked = []
    for path, kind in (
            (os.path.join(UPSCALE_DIR, p, f"ep{ep:02d}_upscaled.mp4"), "upscale"),
            (os.path.join(FINAL_DIR, p, f"ep{ep:02d}_final.mp4"), "final"),
    ):
        ok, why = _video_ok(path)
        if ok:
            return path, kind, ""
        checked.append(f"{kind}[{path}]：{why}")
    # 回退：成片目录里该集的其它命名变体（如历史遗留 ep{NN}_final_subtitles.mp4），取最新
    final_dir = os.path.join(FINAL_DIR, p)
    try:
        if os.path.isdir(final_dir):
            hits = [os.path.join(final_dir, fn) for fn in os.listdir(final_dir)
                    if fn.lower().startswith(f"ep{ep:02d}") and fn.lower().endswith(".mp4")]
            hits = [h for h in hits if _video_ok(h)[0]]
            if hits:
                hits.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                return hits[0], "final_variant", ""
    except OSError as e:
        checked.append(f"final 目录扫描失败：{e}")
    return "", "", ("未找到该集可校对的成片（已检查：" + "；".join(checked) +
                    "）。请先完成视频生成 / 超分 / 成片合成")


def load_episode_script(project: str, episode_no):
    """读取该集剧本（与 pipeline._script_path 同口径：episode_script_path 优先，
    目录内按集号回退；读失败返回 (None, 已尝试路径)，不抛异常）。"""
    import novel_to_script            # 延迟导入（app 顶层已 import，这里只是避免加载期耦合）
    ep = _norm_ep(episode_no)
    p = _safe_key(project)
    if not p:
        return None, ""
    path = novel_to_script.episode_script_path(SCRIPT_DIR, p, ep, p)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data, path
        except Exception as e:        # noqa: BLE001
            logger.warning("读取集剧本失败（%s）：%s", path, e)
    # 回退：项目剧本目录内按「第N集.json」/ metadata.episode_no 匹配，取最新
    folder = os.path.join(SCRIPT_DIR, p)
    cands = []
    try:
        if os.path.isdir(folder):
            for fn in os.listdir(folder):
                if not fn.lower().endswith(".json"):
                    continue
                fp = os.path.join(folder, fn)
                m = re.match(r"^第(\d+)集\.json$", fn)
                if m:
                    if int(m.group(1)) == ep:
                        cands.append((os.path.getmtime(fp), fp))
                    continue
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    meta = (data or {}).get("metadata") or {}
                    if int(meta.get("episode_no") or data.get("episode_no") or -1) == ep:
                        cands.append((os.path.getmtime(fp), fp))
                except Exception:     # noqa: BLE001  单文件解析失败不拖垮回退
                    continue
    except OSError as e:
        logger.debug("扫描项目剧本目录失败（忽略）：%s", e)
    if cands:
        cands.sort(reverse=True)
        try:
            with open(cands[0][1], "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data, cands[0][1]
        except Exception as e:        # noqa: BLE001
            logger.warning("回退读取集剧本失败（%s）：%s", cands[0][1], e)
    return None, path


def collect_burn_text(script: dict, project: str):
    """取该集「应烧字幕文本」（与烧录实现同一来源，按镜头顺序拼接）。

    来源确认（与 pipeline.step_final / video_postprocess.generate_final_video 一致）：
      · caption（剧情装置，caption_burn_enabled 默认开）→ shot["caption"]
        （兼容 {text, kind} 结构 / 旧字符串）+ shot["caption_text"] 扁平兜底；
      · 台词字幕（subtitle_enabled 默认关）→ dialogue_text(shot["dialogue"])。

    Returns:
        (text, meta)：text 为拼接结果（无则空串）；meta 记录两个开关与镜头数，
        供报告留痕（「为什么台词没算进来」一目了然）。
    """
    shots = [s for s in ((script or {}).get("shots") or []) if isinstance(s, dict)]
    want_cap = _caption_burn_enabled_for(project)
    want_dlg = _subtitle_enabled_for(project)
    parts = []
    for s in shots:
        if want_dlg:
            t = dialogue_text(s.get("dialogue"))
            if t:
                parts.append(t)
        if want_cap:
            cap = s.get("caption")
            if isinstance(cap, dict):
                cap = cap.get("text")
            cap = str(cap or s.get("caption_text") or "").strip()
            if cap:
                parts.append(cap)
    meta = {"caption_burn_enabled": want_cap, "subtitle_enabled": want_dlg,
            "shot_count": len(shots)}
    return "\n".join(parts), meta


# ===================== 规范化与比对 =====================

def normalize_text(text: str) -> str:
    """规范化：剥 ASR 训练残留套话 → 小写 → 去全部空白/标点/引号（只留文字与数字）。

    两边（转写 / 剧本文本）都过同一函数 —— 标点与空白差异不该影响相似度
    （whisper 输出常无标点，剧本 caption 常带标点）。"""
    t = _ASR_NOISE_RE.sub("", str(text or ""))
    t = t.lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[\W_]+", "", t, flags=re.UNICODE)   # \W 含全角标点/引号；下划线一并去掉
    return t


def _diff_segments(transcript_norm: str, caption_norm: str):
    """difflib 对齐后取差异片段：
      missing = 转写有而字幕没有（音轨里说了、字幕里找不到的内容）；
      extra   = 字幕有而转写没有（字幕里写了、音轨里没说出口的内容 —— caption 常是
                时空说明文字，H3 未必念出来，这一栏天然可能非空，属「供人工判断」而非硬伤）。
    各最多 10 条、单条截断 80 字、去重保序。"""
    sm = difflib.SequenceMatcher(None, transcript_norm, caption_norm, autojunk=False)
    missing, extra = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            seg = transcript_norm[i1:i2]
            if len(seg) >= 2:
                missing.append(seg)
        if tag in ("insert", "replace"):
            seg = caption_norm[j1:j2]
            if len(seg) >= 2:
                extra.append(seg)

    def _clean(items):
        out, seen = [], set()
        for s in items:
            s = s[:_SEG_MAX_CHARS]
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= _SEGMENTS_MAX:
                break
        return out

    return _clean(missing), _clean(extra)


# ===================== 核心校对 =====================

def verify_episode(project: str, episode_no, progress_cb=None) -> dict:
    """校对一集：定位成片 → ffmpeg 抽音轨 → whisper 转写 → 与应烧字幕比对。

    全程容错：任何一步失败都返回 ``{ok: False, error: <中文原因>}``，绝不抛出；
    成功/失败结论都会尽力落盘到 report_path（落盘失败只附 report_error，不改结论）。

    Args:
        project: 项目名（路由层已 safe_key；这里再兜底一次）。
        episode_no: 集号（非法值按 1 处理）。
        progress_cb: 可选 ``cb(message, percent)``，供后台任务把阶段写进任务字典。
    """
    t0 = time.time()
    ep = _norm_ep(episode_no)
    result = {
        "project": _safe_key(project), "episode_no": ep,
        "available": True, "ok": False,
        "similarity": None, "ok_threshold": _ok_threshold(),
        "transcript_chars": 0, "caption_chars": 0,
        "missing_segments": [], "extra_segments": [],
        "transcript_preview": "",
        "elapsed_sec": 0.0,
    }
    tmpdir = ""

    def _pct(msg, pct):
        if progress_cb is None:
            return
        try:
            progress_cb(msg, pct)
        except Exception:               # noqa: BLE001  进度回调异常绝不影响校对
            logger.debug("caption 校对 progress_cb 异常（忽略）", exc_info=True)

    try:
        env = available()
        result["env"] = {k: env.get(k) for k in ("exe", "model", "ffmpeg")}
        if not env.get("available"):
            result["available"] = False
            result["reasons"] = env.get("reasons") or []
            result["error"] = ("校对环境不可用：" + "；".join(env.get("reasons") or []))
            return result

        _pct("定位成片…", 5)
        video, source, verr = locate_episode_video(project, ep)
        result["video"] = video
        result["video_source"] = source
        if not video:
            result["error"] = verr
            return result

        _pct("读取剧本与应烧字幕…", 15)
        script, script_path = load_episode_script(project, ep)
        result["script_path"] = script_path
        if not isinstance(script, dict):
            result["error"] = f"未找到该集剧本（已尝试：{script_path or '<未定位>'}）；请先生成剧本"
            return result
        burn, meta = collect_burn_text(script, result["project"] or project)
        result.update(meta)
        result["caption_chars"] = len(burn)
        if not burn.strip():
            result["error"] = ("该集没有可校对的烧录字幕文本（剧本 shots 里没有 caption / "
                               "caption_text" + ("，且台词字幕（subtitle_enabled）未开启" if not meta.get("subtitle_enabled") else "") +
                               "）；若本集本来就不烧字幕，无需校对")
            return result
        info = probe_media(video)
        if info.get("ok") and not info.get("has_audio"):
            result["error"] = "成片没有音轨，无法回读校对（该集成片是无声版本）"
            return result

        _pct("ffmpeg 抽取音轨（16kHz 单声道）…", 25)
        tmpdir = tempfile.mkdtemp(prefix="caption_verify_")
        wav = os.path.join(tmpdir, "audio.wav")
        cmd = [env["ffmpeg"], "-y", "-v", "error", "-i", video, "-vn",
               "-ar", str(_AUDIO_SAMPLE_RATE), "-ac", "1", wav]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=_FFMPEG_TIMEOUT_SEC)
        if r.returncode != 0 or not os.path.isfile(wav) or os.path.getsize(wav) <= 0:
            result["error"] = ("音轨抽取失败：" + ((r.stderr or "").strip()[-300:]
                              or f"ffmpeg 返回码 {r.returncode}"))
            return result

        _pct(f"whisper 转写中（zh，最长 {_WHISPER_TIMEOUT_SEC}s）…", 40)
        base = os.path.join(tmpdir, "transcript")
        wcmd = [env["exe"], "-m", env["model"], "-f", wav, "-l", "zh",
                "-otxt", "-of", base]
        wcode, werr = -1, ""
        try:
            wr = subprocess.run(wcmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=_WHISPER_TIMEOUT_SEC)
            wcode, werr = wr.returncode, (wr.stderr or "")
        except subprocess.TimeoutExpired:
            result["error"] = f"whisper 转写超时（超过 {_WHISPER_TIMEOUT_SEC}s），已中止"
            return result
        txt_path = base + ".txt"
        transcript = ""
        if os.path.isfile(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
                    transcript = f.read()
            except OSError as e:
                logger.warning("读取转写 txt 失败（%s）：%s", txt_path, e)
        if not transcript.strip():
            result["error"] = (f"whisper 未产出转写文本（返回码 {wcode}）："
                               + (werr.strip()[-300:] or "无输出"))
            return result
        result["transcript_chars"] = len(transcript)
        result["transcript_preview"] = transcript.strip()[:_PREVIEW_CHARS]

        _pct("与应烧字幕比对…", 90)
        norm_t = normalize_text(transcript)
        norm_c = normalize_text(burn)
        if not norm_c:
            result["error"] = "应烧字幕规范化后为空（全是标点/空白），无法比对"
            return result
        sim = difflib.SequenceMatcher(None, norm_t, norm_c, autojunk=False).ratio()
        result["similarity"] = round(sim, 4)
        missing, extra = _diff_segments(norm_t, norm_c)
        result["missing_segments"] = missing
        result["extra_segments"] = extra
        result["ok"] = sim >= result["ok_threshold"]
        if result["ok"]:
            result["message"] = (f"相似度 {result['similarity']:.2%}，达到阈值 "
                                 f"{result['ok_threshold']:.0%}，字幕烧录与音轨基本一致")
        else:
            result["message"] = (f"相似度 {result['similarity']:.2%}，低于阈值 "
                                 f"{result['ok_threshold']:.0%}；请对照 missing/extra 片段"
                                 "人工复核（caption 是剧情装置，音轨未必逐字念出，"
                                 "extra 非空不必然是缺陷）")
        _pct("校对完成", 100)
    except Exception as e:              # noqa: BLE001  对外契约：绝不向上抛
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        logger.error("caption 校对异常（project=%s ep=%s）：%s",
                     project, episode_no, result["error"], exc_info=True)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        result["elapsed_sec"] = round(time.time() - t0, 2)

    # 报告落盘（尽力而为；失败只附 report_error，不改写校对结论）
    try:
        rp = report_path(project, ep)
        atomic_write_json(rp, result)
        result["report_path"] = rp
    except Exception as e:              # noqa: BLE001
        result["report_error"] = f"校对报告落盘失败：{type(e).__name__}: {e}"
        logger.warning("校对报告落盘失败（project=%s ep=%s）：%s", project, ep, e)
    return result


# ===================== 简单后台任务（无队列、无租约，单任务语义） =====================

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOBS_KEEP_DONE = 20                # 终态条目上限（超出按最旧丢弃，防内存无界增长）


def _prune_jobs_locked() -> None:
    terminal = [k for k, v in _JOBS.items() if v.get("status") in ("done", "error")]
    excess = len(terminal) - _JOBS_KEEP_DONE
    for k in terminal[:max(0, excess)]:
        _JOBS.pop(k, None)


def start_verify(project: str, episode_no) -> dict:
    """起一个后台校对任务。同一项目同一集已在跑时拒绝重复启动（幂等保护）。

    Returns:
        {started: bool, task_key?, error?} —— started=False 时不抛错，由路由层转 4xx。
    """
    ep = _norm_ep(episode_no)
    p = _safe_key(project)
    if not p:
        return {"started": False, "error": "项目名为空"}
    key = f"{p}#ep{ep:02d}"
    with _JOBS_LOCK:
        cur = _JOBS.get(key)
        if cur and cur.get("status") == "running":
            return {"started": False, "task_key": key,
                    "error": "该集校对已在进行中，请稍后用 status 接口查询"}
        _JOBS[key] = {
            "task_key": key, "project": p, "episode_no": ep,
            "status": "running", "progress": 0, "phase": "排队中",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "result": None,
        }
        _prune_jobs_locked()
    threading.Thread(target=_job_worker, args=(key, p, ep), daemon=True).start()
    logger.info("caption 校对任务已启动：%s", key)
    return {"started": True, "task_key": key}


def _job_worker(key: str, project: str, ep: int) -> None:
    def cb(msg, pct=None):
        try:
            with _JOBS_LOCK:
                job = _JOBS.get(key)
                if job is not None:
                    job["phase"] = str(msg or "")
                    if pct is not None:
                        job["progress"] = max(0, min(100, int(pct)))
        except Exception:               # noqa: BLE001  进度更新失败不影响校对本体
            logger.debug("校对任务进度更新失败（忽略）：%s", key, exc_info=True)

    res = verify_episode(project, ep, progress_cb=cb)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job is not None:
            job["status"] = "done" if res.get("ok") else "error"
            job["progress"] = 100
            job["phase"] = "校对完成" if res.get("ok") else "校对失败"
            job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            job["result"] = res
    logger.info("caption 校对任务结束：%s → %s", key, job_status_of(res))


def job_status_of(res: dict) -> str:
    return "done" if (res or {}).get("ok") else "error"


def get_result(project: str, episode_no):
    """查某集校对任务的状态/结果（running/done/error）。

    进程内任务优先；进程重启后内存任务丢失时，回落读落盘报告
    （status 按报告 ok 标 done/error，并带 persisted=True 供前端区分来源）。
    查不到任何记录返回 None。
    """
    ep = _norm_ep(episode_no)
    p = _safe_key(project)
    if not p:
        return None
    key = f"{p}#ep{ep:02d}"
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job is not None:
            return dict(job)
    rp = report_path(p, ep)
    try:
        if os.path.isfile(rp):
            with open(rp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {"task_key": key, "project": p, "episode_no": ep,
                        "status": "done" if data.get("ok") else "error",
                        "result": data, "persisted": True}
    except Exception as e:              # noqa: BLE001  报告损坏按「无记录」处理
        logger.debug("读取校对报告失败（忽略）：%s：%s", rp, e)
    return None
