# -*- coding: utf-8 -*-
"""媒体探测（ffprobe）—— 零业务依赖的叶子模块。

为什么单独拆出来（2026-10-08 解耦）：
    probe_media / has_audio_stream 原本住在 video_postprocess（超分 + 拼接 + FlashVSR +
    upscale_client 依赖），但真正需要它们的模块（comfyui_client / dub_mix /
    caption_verify）只想问一句「这个文件有没有音轨、多长」，却被拖着依赖整个后期模块 ——
    这也正是 comfyui_client → video_postprocess 这条环形依赖边的来源。

    ffprobe 探测只依赖标准库（subprocess / json / os / logging），因此下沉到这里：
    谁要用谁 import，不再经由后期模块中转。video_postprocess 保留同名再导出，
    历史调用点与守卫脚本零改动（行为逐字不变）。
"""
import json
import logging
import os
import subprocess
from typing import Dict

logger = logging.getLogger(__name__)


def probe_media(path: str) -> Dict:
    """ffprobe 读取媒体信息（含视频/音频流清单），任何异常都落到 info.error，不抛出"""
    info = {"path": os.path.abspath(path) if path else "", "ok": False,
            "has_video": False, "has_audio": False,
            "video_streams": 0, "audio_streams": 0}
    if not path or not os.path.exists(path):
        info["error"] = "文件不存在"
        return info
    info["size_bytes"] = os.path.getsize(path)
    info["size_mb"] = round(info["size_bytes"] / 1048576, 3)
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=index,codec_type,codec_name,width,height,r_frame_rate,"
           "sample_rate,channels:format=duration,format_name",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if r.returncode != 0:
            info["error"] = (r.stderr or "ffprobe 失败").strip()[:200]
            return info
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams") or []
        info["streams"] = [{"index": s.get("index"), "type": s.get("codec_type"),
                            "codec": s.get("codec_name"), "width": s.get("width"),
                            "height": s.get("height")} for s in streams]
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        info["video_streams"] = len(videos)
        info["audio_streams"] = len(audios)
        info["has_video"] = bool(videos)
        info["has_audio"] = bool(audios)
        info["video_codec"] = videos[0].get("codec_name") if videos else None
        info["audio_codec"] = audios[0].get("codec_name") if audios else None
        if videos:
            info["width"] = videos[0].get("width")
            info["height"] = videos[0].get("height")
            # B-05 P1-4：实测帧率（ffprobe r_frame_rate 形如 "30000/1001"），供降级重编码参考
            _fr = str(videos[0].get("r_frame_rate") or "")
            try:
                num, _, den = _fr.partition("/")
                if den:
                    info["fps"] = round(int(num) / int(den), 3)
                elif num:
                    info["fps"] = float(num)
            except (ValueError, ZeroDivisionError) as e:
                logger.debug("fps 字段解析失败（忽略）：%s", e)
        if audios:
            # B-04 P1-3（已修复 S-01）：补 audio 采样率/声道探测，供音轨一致性判定
            info["sample_rate"] = audios[0].get("sample_rate")
            info["channels"] = audios[0].get("channels")
        info["duration"] = round(float((d.get("format") or {}).get("duration") or 0), 3)
        info["format_name"] = (d.get("format") or {}).get("format_name")
        info["ok"] = True
    except Exception as e:  # pragma: no cover - 环境相关
        info["error"] = f"{type(e).__name__}: {e}"
    return info

def has_audio_stream(path: str) -> bool:
    """视频是否含音频流（ffprobe 实测，不猜测）"""
    return bool(probe_media(path).get("has_audio"))
