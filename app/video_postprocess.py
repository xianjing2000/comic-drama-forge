"""
视频后期处理 - FlashVSR 超分辨率 + FFmpeg 合并
"""
import os
import re
import subprocess
import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional, Dict
import json

from config import COMFYUI_URL, PROJECT_OUTPUT_DIR, VIDEOS_DIR, FINAL_DIR

# 探测工具已下沉到叶子模块 media_probe（2026-10-08 解耦）：
# comfyui_client / dub_mix / caption_verify 只需要 ffprobe 探测，却被本模块的超分/拼接
# 依赖拖着走（comfyui_client → video_postprocess 还是环形依赖的一条边）。此处保留同名
# 再导出，历史调用点（含 .workbuddy/test 下的守卫）零改动。
from media_probe import probe_media, has_audio_stream  # noqa: F401

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== 集号 / 片段目录（审计 S4：与 app._ep_dir 同口径） =====================
# 写入侧（app.py 的 _ep_dir）遵守「第 1 集平铺、第 2 集起 epNN/」；本模块原先完全不知道集号，
# 于是 `generate_final_video` 只 listdir 平铺目录 → 多集项目合成第 2 集时会拿到第 1 集的片段。
_SHOT_RE = re.compile(r"^shot_(\d+)")


def _episode_no_of(script: dict, fallback=None) -> int:
    """从剧本里取集号（metadata.episode_no > 顶层 episode_no > 兜底），最小 1"""
    if isinstance(script, dict):
        meta = script.get("metadata") or {}
        for v in (meta.get("episode_no"), script.get("episode_no"), fallback):
            try:
                if v is None or v == "":
                    continue
                n = int(v)
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue
    try:
        return max(1, int(fallback or 1))
    except (TypeError, ValueError):
        return 1


def _subtitle_enabled_for(project: str) -> bool:
    """项目级成片字幕开关（config.json 的 subtitle_enabled），**默认 False**。

    2026-09-24 用户要求「视频不要生成字幕」：本模块的 finalize_episode /
    generate_final_video 旧路径此前**无条件**烧硬字幕（产出 epNN_final_subtitles.mp4）。
    这里统一取值：非 true 一律不烧。
    读取失败 / 项目不存在 → 返回 False（安全侧：宁可不烧字幕，也不要产出一条带字幕的成片）。
    """
    try:
        import project_store  # 延迟导入，避免与 project_store 形成循环依赖
        cfg = project_store.read_config(project or "")
        val = (cfg or {}).get("subtitle_enabled", False)
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            return False
        return bool(val)
    except Exception as e:  # noqa: BLE001  开关读取失败按「关闭」处理
        logger.warning(f"读取项目 config.subtitle_enabled 失败（按关闭处理）：{e}")
        return False


def _caption_burn_enabled_for(project: str) -> bool:
    """项目级「字幕/转场 caption」烧制开关（config.json 的 caption_burn_enabled），**默认 True**。

    与 _subtitle_enabled_for 是**两件事**，刻意分开：
      · subtitle_enabled（默认关）：把人物开口的台词转录成硬字幕——辅助性文字，
        用户 2026-09-24 明确要求不要；
      · caption_burn_enabled（默认开）：把剧本的 caption 字幕烧进成片——它是**剧情装置**
        （时空落点、时空回溯、集尾悬念），参考稿正是靠「春秋蝉，逆转时光。」这类字幕
        让观众看懂时空跳变；不烧观众就会看到无过渡的跳切。
    不想要字幕的项目在其 config.json 里写 caption_burn_enabled: false 即可（也能被
    subtitle_enabled: false 之外单独控制）。读取失败 → 回到默认 True。
    """
    try:
        import project_store  # 延迟导入，避免与 project_store 形成循环依赖
        cfg = project_store.read_config(project or "")
        if "caption_burn_enabled" in (cfg or {}):
            val = cfg.get("caption_burn_enabled")
            if isinstance(val, str):
                s = val.strip().lower()
                return s in ("true", "1", "yes", "on")
            return bool(val)
        # 未显式配置：沿用配置中心的默认值（默认 True），与 _subtitle_enabled_for 同思路
        try:
            from config import PROJECT_DEFAULT_CONFIG
            return bool(PROJECT_DEFAULT_CONFIG.get("caption_burn_enabled", True))
        except Exception:  # noqa: BLE001
            return True
    except Exception as e:  # noqa: BLE001  开关读取失败按「默认开」处理
        logger.warning(f"读取项目 config.caption_burn_enabled 失败（按默认开启处理）：{e}")
        return True


def _ep_videos_dir(project: str, ep: int) -> str:
    """该集视频片段目录（第 1 集 = 平铺目录，第 2 集起 epNN/）"""
    base = os.path.join(PROJECT_OUTPUT_DIR, "videos", project)
    return os.path.join(base, f"ep{ep:02d}") if int(ep) > 1 else base


def _ordered_shot_files(videos_dir: str, ep: int) -> List[str]:
    """按**镜头号数字**顺序取逐镜片段（而非文件名字典序）。

    显式排除整集成片 `*_full.mp4`（否则会把整集和它的各镜一起拼，产出内容重复的成片，
    体积够大能骗过 100KB/2s 硬闸）；一个逐镜片段都没有时，才回退采用整集成片。
    """
    try:
        names = os.listdir(videos_dir)
    except OSError:
        return []
    media = [f for f in names if f.lower().endswith((".mp4", ".mov", ".mkv"))]
    shots = sorted((f for f in media if _SHOT_RE.match(f)
                    and not f.endswith("_full.mp4") and not f.endswith(".bak.mp4")),
                   key=lambda f: int(_SHOT_RE.match(f).group(1)))
    if shots:
        return [os.path.join(videos_dir, f) for f in shots]
    # 整集模式（video_mode=episode）磁盘上只有一支 *full*.mp4，直接采用
    for cand in (f"ep{int(ep):02d}_full.mp4", "episode_full.mp4"):
        if cand in media:
            return [os.path.join(videos_dir, cand)]
    fulls = [f for f in media if f.endswith("_full.mp4")]
    return [os.path.join(videos_dir, fulls[0])] if fulls else []


# ===================== 音轨工具（H3 出片音轨策略见 config：H3_EMIT_AUDIO / H3_STRIP_AUDIO） =====================

def _filter_existing_segments(video_files: List[str]) -> tuple:
    """A2：拼接**前置校验**（借鉴 Huobao ``services/ffmpeg-merge.ts``）——拆分可拼片段与缺失片段。

    为什么必须在拼接之前做：目录/记录里的路径可能指向已被清理、或未生成完的文件
    （重生成覆盖、清理临时文件、跨设备拷贝都会造成），直接交给 ffmpeg 只会得到
    「No such file or directory」这类**晦涩报错**；而成片链路的 ``-shortest`` 又会把
    缺失造成的时长差**静默截掉尾部**，用户完全拿不到「哪几镜缺片」的可操作信息。

    Returns:
        ``(valid, missing)`` —— ``valid`` 为存在且**非空**的文件（保持原顺序，供部分拼接），
        ``missing`` 为缺失或 0 字节的片段（调用方应显式列出其镜号）。
    """
    valid: List[str] = []
    missing: List[str] = []
    for p in list(video_files or []):
        try:
            ok = os.path.isfile(p) and os.path.getsize(p) > 0
        except OSError:
            ok = False          # 权限/路径异常一律按缺失处理，不让它炸掉整集拼接
        (valid if ok else missing).append(p)
    return valid, missing


def strip_audio(video_path: str, output_path: Optional[str] = None,
                backup: bool = True) -> Dict:
    """剥离视频音轨（-an，视频流直接复制不重编码；失败时降级 libx264 重编码）

    - 不传 output_path 时原地替换：先写临时文件，校验无音轨后再替换，原文件可按 backup 留存
    - backup=True 且原文件确有音轨时，备份为 <同名>.withaudio.bak（同目录；刻意不带 .mp4
      后缀，避免被 *.mp4 glob 当成分片混进拼接清单）
    """
    t0 = time.time()
    report = {"ok": False, "input": os.path.abspath(video_path) if video_path else "",
              "changed": False, "backup": None, "method": None,
              "has_audio_before": None, "has_audio_after": None}
    if not video_path or not os.path.exists(video_path):
        report["error"] = "输入视频不存在"
        return report
    before = probe_media(video_path)
    report["has_audio_before"] = before.get("has_audio")
    report["before"] = {k: before.get(k) for k in ("width", "height", "duration", "size_mb", "video_codec")}
    if not before.get("has_audio"):
        report.update({"ok": True, "changed": False, "has_audio_after": False,
                       "output": os.path.abspath(video_path), "message": "输入本身无音轨，无需处理"})
        report["elapsed_sec"] = round(time.time() - t0, 2)
        return report

    in_place = not output_path
    target = os.path.abspath(output_path or video_path)
    # G8：临时文件用 .clean（非 .mp4 后缀），避免被 *.mp4 glob（_ordered_shot_files /
    # get_output_status）命中当成"分片/成片"；容器格式改由 -f mp4 显式指定。
    tmp_out = target + ".noaudio.clean" if in_place else target
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

    if backup and in_place:
        # 备份不带 .mp4 后缀（双保险）：.bak.mp4 会被 *.mp4 glob（_ordered_shot_files /
        # get_output_status）当成正片混进拼接；读侧/清理侧均按返回的 report["backup"] 取路径
        bak = os.path.splitext(video_path)[0] + ".withaudio.bak"
        try:
            shutil.copy2(video_path, bak)
            report["backup"] = bak
        except OSError as e:
            report["error"] = f"备份失败，已中止（不做无备份剥离）：{e}"
            return report

    # in_place 时输出到 .noaudio.clean（非 .mp4），必须显式 -f mp4 指定容器；
    # 非 in_place 时输出到调用方给的目标（容器按其自身扩展名推断），不强加 -f mp4
    fmt = ["-f", "mp4"] if in_place else []
    attempts = [
        ("copy", ["ffmpeg", "-y", "-i", video_path, "-map", "0:v", "-c", "copy", "-an"]
         + fmt + [tmp_out]),
        ("reencode_h264", ["ffmpeg", "-y", "-i", video_path, "-map", "0:v", "-an",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
         + fmt + [tmp_out]),
    ]
    last_err = ""
    for method, cmd in attempts:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        if r.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            after = probe_media(tmp_out)
            if after.get("has_audio"):
                last_err = f"{method} 后仍检测到音轨"
                continue
            if in_place:
                try:
                    os.replace(tmp_out, target)
                except OSError as e:
                    # S-02：Windows 文件锁（多任务场景：autopilot 跑图 + 前端查看同一视频）
                    # os.replace 目标被占用会抛 OSError。保留 tmp_out 供下次清理，
                    # 原文件音轨未剥离（保留原状），但标记 error 让调用方知晓。
                    report["error"] = (f"剥离成功但 os.replace 失败（原文件保留音轨未变，"
                                       f"tmp 残留 {os.path.basename(tmp_out)}）：{e}")
                    report["ok"] = False
                    report["changed"] = False
                    report["method"] = method
                    report["elapsed_sec"] = round(time.time() - t0, 2)
                    logger.error(report["error"])
                    return report
            report.update({"ok": True, "changed": True, "method": method,
                           "output": os.path.abspath(target),
                           "has_audio_after": False,
                           "after": {k: after.get(k) for k in ("width", "height", "duration", "size_mb", "video_codec")}})
            report["elapsed_sec"] = round(time.time() - t0, 2)
            return report
        last_err = (r.stderr or "").strip()[-300:]
        logger.warning(f"剥离音轨失败（{method}）：{last_err[:160]}")

    try:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    except OSError as e:
        logger.debug("清理临时输出失败（忽略）：%s", e)
    report["error"] = f"剥离音轨失败：{last_err}"
    report["elapsed_sec"] = round(time.time() - t0, 2)
    return report


def ensure_no_audio(video_path: str, backup: bool = True) -> Dict:
    """确保视频无音轨：ffprobe 校验 → 若无音轨直接返回；若有则剥离并复核"""
    report = strip_audio(video_path, output_path=None, backup=backup)
    if report.get("ok") and not report.get("changed"):
        report["message"] = "已确认无音轨（未改动文件）"
    return report


def ensure_audio_track(video_path: str, sample_rate: int = 48000) -> Dict:
    """确保视频**含有**音轨：已有音轨直接返回；没有则补一条等长静音 AAC 轨。

    为什么要补：成片拼接用的是 concat demuxer + `-c copy`。如果部分镜头有音轨、
    部分没有（H3 未必每镜都出声），拼出来的音轨会错位甚至拼接失败。
    统一补齐后拼接行为可预测。

    与本项目其它后处理一致：失败不抛异常，只把原因写进返回的 error。
    """
    t0 = time.time()
    report: Dict = {"ok": False, "target": os.path.abspath(video_path) if video_path else "",
                    "kind": "audio_pad", "changed": False, "method": None}
    if not video_path or not os.path.exists(video_path):
        report["error"] = "输入视频不存在"
        return report
    before = probe_media(video_path)
    report["has_audio_before"] = bool(before.get("has_audio"))
    report["before"] = {k: before.get(k) for k in ("width", "height", "duration", "size_mb")}
    if before.get("has_audio"):
        report.update({"ok": True, "changed": False, "has_audio_after": True,
                       "message": "已有音轨（未改动文件）"})
        report["elapsed_sec"] = round(time.time() - t0, 2)
        return report
    dur = float(before.get("duration") or 0)
    if dur <= 0:
        report["error"] = "无法读取视频时长，跳过补音轨"
        return report
    # G8：临时文件用 .clean（非 .mp4 后缀），避免被 *.mp4 glob 命中；容器由 -f mp4 显式指定
    tmp_out = os.path.splitext(video_path)[0] + ".audioadded.clean"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "128k", "-shortest",
           "-f", "mp4", "-movflags", "+faststart", tmp_out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    except Exception as e:  # pragma: no cover - 环境相关
        report["error"] = f"{type(e).__name__}: {e}"
        return report
    if r.returncode != 0 or not os.path.exists(tmp_out):
        # B-16 P2-11：补音轨失败 → 清理中间产物（.clean 半成品）
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError as e:
            logger.debug("清理临时输出失败（忽略）：%s", e)
        report["error"] = (r.stderr or "ffmpeg 补音轨失败").strip()[-300:]
        return report
    after = probe_media(tmp_out)
    if not after.get("has_audio"):
        report["error"] = "补音轨后仍未检测到音频流"
        try:
            os.remove(tmp_out)
        except OSError as e:
            logger.debug("清理临时输出失败（忽略）：%s", e)
        return report
    os.replace(tmp_out, video_path)
    report.update({"ok": True, "changed": True, "has_audio_after": True,
                   "method": "silent_aac_pad",
                   "after": {k: after.get(k) for k in ("width", "height", "duration",
                                                       "size_mb", "audio_codec")}})
    report["elapsed_sec"] = round(time.time() - t0, 2)
    return report


class VideoPostProcessor:
    """视频后期处理器"""

    def __init__(self, comfyui_url: str = COMFYUI_URL):
        self.comfyui_url = comfyui_url

    def _concat_fail(self, caller: str, reason: str) -> str:
        """拼接失败的**唯一出口**：按契约返回 ``""``，但必须先把原因留痕。

        D-10（P2，契约脆弱）：``concat_videos`` 用空串同时表达「失败」与「无输出」，
        且不抛异常 —— 返回值**无法携带失败理由**。调用方一旦漏判空，就会把 ``""``
        当成合法路径继续喂给 ffmpeg，报出与真实原因无关的错误，排障时被彻底带偏。

        保持返回契约不变（改动风险大于收益），改为在返回前把「谁调的 + 为什么失败」
        写进 error 日志：``reason`` 必须带上 ffmpeg 的真实输出（stderr / 返回码 / 异常类型），
        而不是「合并失败」这种无信息量的套话。
        """
        logger.error("concat_videos 失败[调用方=%s]：%s"
                     "（按契约返回空串，调用方必须判空后才能继续）",
                     (caller or "未标注"), reason)
        return ""

    def concat_videos(self, video_paths: List[str], output_path: str,
                     audio_paths: Optional[List[str]] = None,
                     caller: str = "") -> str:
        """使用 FFmpeg 合并视频

        S-01 修复：拼接前探测每个片段的音轨参数（codec/sample_rate/channels）。
        - 全部一致 → 走 concat demuxer + -c copy（快，不重编码）
        - 不一致（或部分有音轨/部分无）→ 走 concat filter 重编码（统一采样率/声道/编码器）

        返回值契约（M3/A-23）：成功返回 output_path；**任何失败/异常/空输入一律返回 ""**
        （不抛异常，保持既有调用方语义）。⚠️ 所有调用点必须对返回值判空（`if not ret` /
        `_nonempty(path)`）；已核实全库仅 pipeline.py:876（concat 后 `_nonempty(tmp)`）与
        本类（`if not self.concat_videos(...)`）两处，均判空。外部新增调用务必照此判空，
        并在 `verify_project_audit.py` 的守卫里登记（脚本会断言「所有调用点都判空」）。

        D-10：因为返回值带不了失败理由，特新增 `caller` 参数（调用方自报标识）—— 每条
        返回 `""` 的路径都会经 :meth:`_concat_fail` 打一条带调用方标识 + ffmpeg 真实原因的
        error 日志。**新调用点请务必传 `caller`**（如 `caller="pipeline.step_final"`）。
        """
        if not video_paths:
            return self._concat_fail(caller, "输入片段为空（video_paths 为空列表）")

        # 探测音轨参数一致性（probe_media 自身不抛异常，任何异常落到 info.error）
        probes = [probe_media(p) for p in video_paths]
        audio_params = set()
        has_any_audio = False
        for p in probes:
            if p.get("has_audio"):
                has_any_audio = True
                audio_params.add((
                    p.get("audio_codec"), p.get("sample_rate"),
                    p.get("channels"),
                ))
        need_reencode = has_any_audio and len(audio_params) > 1

        if need_reencode:
            logger.info(f"音轨参数不一致（{len(audio_params)} 种），走 concat filter 重编码路径")
            return self._concat_reencode(video_paths, output_path, probes, caller=caller)

        # 参数一致 → 走 concat demuxer（快，不重编码）
        list_file = output_path + ".list"

        try:
            # 建目录也放进 try：docstring 承诺「任何失败/异常一律返回 ""」，建目录失败
            # （盘不存在/无权限）同样属于失败路径，不能把异常抛给调用方。
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(list_file, 'w', encoding='utf-8') as f:
                for v in video_paths:
                    # 路径含单引号时必须转义，否则引号会把路径截断（No such file or
                    # directory）。concat 清单的引号按成对解析，转义写法是「' → '\''」
                    # （关引号 + \' 字面引号 + 重开引号）；与 add_subtitles 对 SRT 路径
                    # 的单引号处理同一动机，但清单语法不能直接写 \'（引号内 \ 原样保留）。
                    _p = os.path.abspath(v).replace("'", "'\\''")
                    f.write(f"file '{_p}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                output_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            if result.returncode == 0:
                logger.info(f"视频合并成功（demuxer -c copy）: {output_path}")
                os.remove(list_file)
                return output_path
            # -c copy 失败（常见原因：音视频参数实际不一致但探测未捕获，
            # 如 H265 vs H264 混拼、关键帧对齐失败）→ 降级重编码兜底
            logger.warning(f"demuxer 拼接失败，降级 concat filter 重编码：{(result.stderr or '')[-300:]}")
            if os.path.exists(list_file):
                os.remove(list_file)
            return self._concat_reencode(video_paths, output_path, probes, caller=caller)
        except Exception as e:
            # G8：异常路径也要清掉 concat list 临时文件，不留 .list 残片
            try:
                if os.path.exists(list_file):
                    os.remove(list_file)
            except OSError as _ce:
                logger.debug("清理 concat 清单文件失败（忽略）：%s", _ce)
            return self._concat_fail(
                caller, f"concat demuxer 执行异常：{type(e).__name__}: {e}")

    def _concat_reencode(self, video_paths: List[str], output_path: str,
                         probes: List[Dict], caller: str = "") -> str:
        """concat filter 重编码拼接（分辨率/帧率由源片实测推导，不硬编码）

        B-05 P1-4：原实现硬编码 scale=1280:720 + fps=30 → 竖屏 9:16 项目被悄悄
        改成横屏加黑边且不可逆。改为以「分辨率/帧率出现最多的片段」为基准，
        竖屏保持竖屏、帧率与源片一致。

        filter_complex 结构：
          [i:v:0]scale/fps/format → [vi]
          [i:a?]aresample/aformat → [ai]
          [v0][v1]...concat=n:1:0 → [outv]
          [a0][a1]...concat=n:0:1 → [outa]

        D-10：失败同样经 `_concat_fail` 出口，`caller` 由 `concat_videos` 透传。
        """
        if not video_paths:
            return self._concat_fail(caller, "重编码路径输入片段为空（video_paths 为空列表）")
        n = len(video_paths)

        # B-05：从源片实测推导目标分辨率/帧率（以出现最多的宽/高/帧率为准）
        target_w, target_h = 1280, 720  # 兜底值；B-05 已改为源片实测投票，仅 ffprobe 全失败时触达
        target_fps = 30  # 兜底值；B-05 已改为源片实测投票，仅 ffprobe 全失败时触达
        w_votes: Dict[int, int] = {}
        h_votes: Dict[int, int] = {}
        fps_votes: Dict[int, int] = {}
        for i, p in enumerate(video_paths):
            pro = probes[i] if i < len(probes) else {}
            w = int(pro.get("width") or 0)
            h = int(pro.get("height") or 0)
            fps = int(round(float(pro.get("fps") or 0)))
            if w and h:
                w_votes[w] = w_votes.get(w, 0) + 1
                h_votes[h] = h_votes.get(h, 0) + 1
            if fps:
                fps_votes[fps] = fps_votes.get(fps, 0) + 1
        if w_votes:
            target_w = max(w_votes, key=w_votes.get)
        if h_votes:
            target_h = max(h_votes, key=h_votes.get)
        if fps_votes:
            target_fps = max(fps_votes, key=fps_votes.get)
        # 宽高对齐到偶数（libx264 要求），防止 721 之类奇数
        target_w = target_w // 2 * 2 or 1280
        target_h = target_h // 2 * 2 or 720
        logger.info(f"降级重编码基准：{target_w}x{target_h}@{target_fps}fps（源片实测）")

        inputs: List[str] = []
        for p in video_paths:
            inputs += ["-i", os.path.abspath(p)]

        # 逐片段：视频归一化（scale 到源片基准 + fps + pix_fmt + sar）→ [vi]；音频归一化 → [ai]
        per_stream: List[str] = []
        for i in range(n):
            per_stream.append(
                f"[{i}:v:0]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={target_fps},format=yuv420p,setsar=1[v{i}]"
            )
            per_stream.append(
                f"[{i}:a?]aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]"
            )
        # 拼接
        v_concat_in = "".join(f"[v{i}]" for i in range(n))
        a_concat_in = "".join(f"[a{i}]" for i in range(n))
        per_stream.append(f"{v_concat_in}concat=n={n}:v=1:a=0[outv]")
        per_stream.append(f"{a_concat_in}concat=n={n}:v=0:a=1[outa]")
        filter_complex = ";".join(per_stream)

        cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + [
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]
        logger.info(f"concat filter 重编码拼接 {n} 个片段 → {output_path}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
            if result.returncode == 0 and os.path.exists(output_path):
                logger.info(f"视频合并成功（filter 重编码）: {output_path}")
                return output_path
            if result.returncode == 0:
                # 返回码 0 却没产出文件：不是 ffmpeg 报错，而是输出被吞（磁盘满/被杀）
                return self._concat_fail(
                    caller, f"ffmpeg 返回码 0 但输出文件不存在：{output_path}"
                            f"（疑磁盘写满或进程被回收；stderr={(result.stderr or '')[-300:]!r}）")
            return self._concat_fail(
                caller, f"ffmpeg 重编码返回码 {result.returncode}，"
                        f"stderr={(result.stderr or '')[-500:]!r}")
        except Exception as e:
            return self._concat_fail(
                caller, f"ffmpeg 重编码调用异常：{type(e).__name__}: {e}")

    def add_subtitles(self, video_path: str, subtitles: List[dict],
                     output_path: str) -> str:
        """添加字幕到视频（烧录）

        Windows 路径坑（实测踩过）：ffmpeg 的 `subtitles=` 是**滤镜参数**，其解析器
        会把反斜杠当转义符吃掉，`C:\\a\\b.srt` 会变成 `Cab.srt`，报
        "Unable to parse original_size option value ..."。
        因此这里改为：把 ffmpeg 的工作目录切到 SRT 所在目录，滤镜只传**纯文件名**。
        这样既不出现反斜杠也不用转义盘符冒号，且路径里的中文/空格也一并规避。
        """
        # G8：SRT 临时文件统一 finally 清理（成功则置 None 跳过；失败/异常路径不留残片）
        srt_file = None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

            # 生成 SRT 文件
            srt_file = output_path + ".srt"
            with open(srt_file, 'w', encoding='utf-8') as f:
                for i, sub in enumerate(subtitles, 1):
                    start = sub.get("start", 0)
                    end = sub.get("end", start + 3)
                    text = sub.get("text", "")
                    f.write(f"{i}\n")
                    f.write(f"{self._format_time(start)} --> {self._format_time(end)}\n")
                    f.write(f"{text}\n\n")

            srt_dir = os.path.dirname(os.path.abspath(srt_file))
            srt_name = os.path.basename(srt_file)
            # 文件名内可能含单引号（项目名极端情况），按 ffmpeg 滤镜语法转义
            filter_arg = "subtitles=filename='" + srt_name.replace("'", r"\'") + "'"
            # -map 0:a? 保证输入有音轨时不丢（?=可选，无音轨不报错）。
            # 不显式 map 时 ffmpeg 的 -vf 可能只选视频流，导致 H3 原生音效丢失。
            cmd = [
                "ffmpeg", "-y",
                "-i", os.path.abspath(video_path),
                "-vf", filter_arg,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-map", "0:v:0", "-map", "0:a?",
                "-c:a", "copy",
                os.path.abspath(output_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=1800, cwd=srt_dir)
            if result.returncode == 0 and os.path.isfile(output_path) \
                    and os.path.getsize(output_path) > 0:
                logger.info(f"字幕添加成功: {output_path}")
                srt_file = None
                return output_path
            logger.error(f"添加字幕失败（返回码 {result.returncode}）："
                         f"{(result.stderr or '')[-800:]}")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.error(f"添加字幕失败: {e}")
            return ""
        finally:
            if srt_file and os.path.exists(srt_file):
                try:
                    os.remove(srt_file)
                except OSError as e:
                    logger.debug("清理临时字幕文件失败（忽略）：%s", e)

    def _format_time(self, seconds: float) -> str:
        """格式化时间为 SRT 格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    def upscale_with_flashvsr(self, video_path: str, output_path: str,
                               scale: int = 4, project_name: Optional[str] = None,
                               mode: Optional[str] = None, **kwargs) -> Dict:
        """使用 FlashVSR 进行**真实**超分辨率（委托 upscale_client.VideoUpscaler）

        原实现为占位（只打日志并直接 return True，静默成功）；
        现改为调用已实跑验证的 FlashVSR Ultra-Fast 链路：
        VHS_LoadVideoPath → FlashVSRInitPipe → FlashVSRNodeAdv → VHS_VideoCombine。

        返回 VideoUpscaler 的结构化结果（before / after / elapsed_sec / output_path 等）；
        失败时抛出 upscale_client.UpscaleError，绝不静默返回成功。
        """
        import shutil
        from upscale_client import VideoUpscaler   # 延迟导入，避免与应用初始化互相依赖

        project = (project_name
                   or os.path.basename(os.path.dirname(os.path.abspath(output_path)))
                   or "project")
        logger.info(f"FlashVSR 超分: {video_path} -> {output_path}（{scale}x, mode={mode or 'default'}）")
        res = VideoUpscaler(self.comfyui_url).upscale(
            video_path, project_name=project, scale=int(scale), mode=mode, **kwargs)

        # 兼容旧签名：调用方指定了 output_path 时，再拷贝一份到该路径
        if output_path and os.path.abspath(output_path) != os.path.abspath(res["output_path"]):
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            shutil.copy2(res["output_path"], output_path)
            res["output_path"] = os.path.abspath(output_path)
        return res

    def generate_final_video(self, script_path: str, project_name: str,
                             episode_no=None) -> str:
        """（A2 拼接前置校验见模块级 :func:`_filter_existing_segments`）"""
        """生成最终视频（**按集**合成：第 1 集平铺、第 2 集起 epNN/）

        审计 S4 修复：
        1. 旧签名没有 `episode_no`，只 `listdir` 平铺目录 —— 多集项目点「生成成片」时，
           第 2 集及以后**完全漏掉**（拿到的是第 1 集的片段），而接口照样返回 success:true
           并把它登记进「成品验收」队列。现在集号取「入参 > 剧本 episode_no > 1」，
           目录口径与写入侧 `app._ep_dir` 一致；
        2. 文件名带上集号（`epNN_final.mp4`），与 `pipeline.final_path` / 集进度推导
           （`app.py` 的 `ep{ep:02d}_final.mp4`）统一 —— 此前手合成的成片叫 `<项目>.mp4`，
           托管侧的 `probe_final` 永远看不见；
        3. 只取逐镜 `shot_NN.*` 且按镜头号数字排序（排除 `*_full.mp4`，避免整集与各镜
           一起拼出内容重复的成片）；
        4. `concat_videos` 失败会返回 `""`，旧代码**忽略返回值**照样返回一个不存在的路径
           → 这里把失败上抛为 `""`。
        """
        with open(script_path, 'r', encoding='utf-8') as f:
            script = json.load(f)

        ep = _episode_no_of(script, episode_no)
        videos_dir = _ep_videos_dir(project_name, ep)
        final_dir = os.path.join(PROJECT_OUTPUT_DIR, "final", project_name)
        os.makedirs(videos_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)

        # 获取该集的视频片段（按镜头号排序；无逐镜片段时回退整集成片）
        video_files = _ordered_shot_files(videos_dir, ep)
        if not video_files:
            logger.warning(f"没有找到第 {ep} 集的视频片段：{videos_dir}")
            return ""

        # ---- A2 拼接前置校验（借鉴 Huobao ``ffmpeg-merge.ts``）----
        # 历史痛点：目录里的路径可能指向已被清理/未生成完的文件，直接交给 ffmpeg 只会得到
        # 「No such file or directory」这类晦涩报错；而成片链路的 ``-shortest`` 又会把
        # 缺失造成的时长差**静默截掉尾部**，用户拿不到「哪几镜缺片」的可操作信息。
        # 这里先把缺失/空片段挑出来**显式列出镜号**，再**允许部分拼接**（跳过缺失段、
        # 按镜号顺序拼已生成的），既不整集失败、也不静默截尾。
        video_files, _missing = _filter_existing_segments(video_files)
        if _missing:
            logger.warning(
                "第 %s 集成片：%d 个镜头片段缺失或为空 → 已跳过（部分拼接）。"
                "缺失片段：%s。请重新生成这些镜头，或接受当前部分成片。",
                ep, len(_missing),
                "、".join(os.path.basename(m) for m in _missing[:20]))
        if not video_files:
            logger.error("第 %s 集所有镜头片段均缺失或为空，无法拼接：%s", ep, videos_dir)
            return ""

        # 合并视频
        output_path = os.path.join(final_dir, f"ep{ep:02d}_final.mp4")
        if not self.concat_videos(video_files, output_path,
                                  caller=f"ep{ep:02d}_final（{project_name}）"):
            # ⚠️ 必须判返回值：否则拼失败时对外抛出一个**不存在**的 URL，还被登记成交付物
            # （具体失败原因已由 concat_videos 内部按 D-10 打了 error 日志，此处只补调用点）
            logger.error(f"第 {ep} 集成片拼接失败（未产出有效文件）：{output_path}")
            return ""

        # 添加字幕（dialogue 兼容结构化 [{speaker,text}] 与旧字符串）
        # 2026-09-24 用户明确要求「视频不要生成字幕」：台词硬字幕默认不再烧。
        # 2026-10-02：区分两种文字，各有独立开关——
        #   · 台词字幕（人物开口的转录）→ subtitle_enabled，默认 false；
        #   · 字幕/转场 caption（时空落点/回溯/集尾悬念）→ caption_burn_enabled，默认 true，
        #     它是剧情装置：不烧观众就会看到无过渡的跳切（参考稿靠「春秋蝉，逆转时光。」交代）。
        # 注意：本函数在旧接口路径（非 pipeline 托管）下调用，无法依赖宿主模块 app，
        # 故这里直接读配置文件，保证「界面点生成成片」这条路与 pipeline 行为一致。
        _want_dlg = _subtitle_enabled_for(project_name)
        _want_cap = _caption_burn_enabled_for(project_name)
        subtitles = []
        if _want_dlg or _want_cap:
            from dialogue_utils import dialogue_text
            current_time = 0
            for shot in script.get("shots", []):
                duration = shot.get("duration", 5)
                if _want_dlg:
                    text = dialogue_text(shot.get("dialogue"))
                    if text:
                        subtitles.append({
                            "start": current_time,
                            "end": current_time + duration,
                            "text": text
                        })
                if _want_cap:
                    # caption 兼容结构化 {text, kind} / 旧字符串 / 扁平 caption_text 三种形状
                    _cap = shot.get("caption")
                    if isinstance(_cap, dict):
                        _cap = _cap.get("text")
                    _cap = str(_cap or shot.get("caption_text") or "").strip()
                    if _cap:
                        subtitles.append({"start": current_time,
                                          "end": current_time + duration,
                                          "text": _cap})
                current_time += duration
        else:
            logger.info(f"字幕全部关闭（subtitle_enabled=caption_burn_enabled=false），跳过烧制：{output_path}")

        if subtitles:
            self.add_subtitles(output_path, subtitles,
                             os.path.join(final_dir, f"ep{ep:02d}_final_subtitles.mp4"))

        logger.info(f"第 {ep} 集最终视频: {output_path}")
        return output_path

