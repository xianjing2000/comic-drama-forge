# -*- coding: utf-8 -*-
"""ComfyUI 产物 → 超分链路的 URL 与路径解析（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 6 步，2026-10-10）

core 模块（asset_worker / keyframe_helpers / mix_helpers / video_helpers …）需要这组能力，
却只能从 routes/_shared 取 —— 它们与 HTTP 毫无关系。上移到 app/ 层后，
core 侧依赖的是 app 层模块，而不是路由层私有模块。

## 铁律

不得反向依赖 routes/*。所依赖的业务模块均已核对不导入 routes._shared。
"""
from __future__ import annotations

import os
import threading

from config import (COMFYUI_OUTPUT_DIR, FINAL_DIR, PROJECT_OUTPUT_DIR,
                    UPSCALE_DIR, VIDEOS_DIR)
from upscale_client import UpscaleError


COMFY_VIDEO_DIRS = [("ComfyUI片段", os.path.join(COMFYUI_OUTPUT_DIR, "video")),
                    ("ComfyUI超分", os.path.join(COMFYUI_OUTPUT_DIR, "upscale"))]


UPSCALE_URL_PREFIXES = [("/api/final/", FINAL_DIR),
                        ("/api/videos/", VIDEOS_DIR),
                        ("/api/upscale/", UPSCALE_DIR)]


def _comfy_view_url(filename: str, subfolder: str = "") -> str:
    """生成后端代理 URL（/api/upscale/comfyview），实际播放时 302 到 ComfyUI /view"""
    from urllib.parse import quote
    return (f"/api/upscale/comfyview?filename={quote(filename)}"
            f"&subfolder={quote(subfolder)}")


def _upscale_resolve_comfyview(query: dict) -> str:
    """解析 /api/upscale/comfyview?... 形式的 ComfyUI 产出视频为本地绝对路径"""
    filename = (query.get("filename") or "").replace("\\", "/").lstrip("/")
    subfolder = (query.get("subfolder") or "").replace("\\", "/").strip("/")
    if not filename or ".." in filename.split("/") or ".." in subfolder.split("/"):
        raise UpscaleError("非法的 ComfyUI 文件参数")
    candidate = os.path.abspath(os.path.join(COMFYUI_OUTPUT_DIR, subfolder, filename))
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if not candidate.startswith(root + os.sep):
        raise UpscaleError("非法路径：不允许跳出 ComfyUI 输出目录")
    if not os.path.exists(candidate):
        raise UpscaleError(f"ComfyUI 产出文件不存在: {candidate}")
    return candidate


def _upscale_resolve_video(data: dict) -> str:
    """解析待超分视频的真实本地路径：支持 video_path（绝对路径）或 video_url（/api/... 前缀）"""
    video_path = (data.get("video_path") or "").strip()
    if video_path:
        video_path = os.path.abspath(video_path)
        if not os.path.exists(video_path):
            raise UpscaleError(f"视频文件不存在: {video_path}")
        # 审计 P2-4（2026-09-29）：绝对路径输入限定在 output/ 与 ComfyUI 输出目录内。
        # URL 分支本就有目录边界，绝对路径分支此前没有 —— 零鉴权部署下等于
        # 「任意磁盘视频文件间接读取」（送 ComfyUI 渲染、产物可回看）。normcase
        # 对齐大小写不敏感文件系统的路径比较。
        _vp_norm = os.path.normcase(video_path)
        _allowed_roots = (os.path.abspath(PROJECT_OUTPUT_DIR),
                          os.path.abspath(COMFYUI_OUTPUT_DIR))
        if not any(_vp_norm.startswith(os.path.normcase(r + os.sep))
                   for r in _allowed_roots):
            raise UpscaleError("非法路径：video_path 仅允许 output/ 或 ComfyUI 输出目录内的文件")
        return video_path

    url = (data.get("video_url") or "").strip()
    if url.startswith("/api/upscale/comfyview"):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        return _upscale_resolve_comfyview({k: v[0] for k, v in qs.items()})
    url = url.split("?")[0]
    if url:
        for prefix, base in UPSCALE_URL_PREFIXES:
            if url.startswith(prefix):
                rel = url[len(prefix):]
                candidate = os.path.abspath(os.path.join(base, rel))
                if not candidate.startswith(os.path.abspath(base)):
                    raise UpscaleError("非法路径：不允许跳出输出目录")
                if not os.path.exists(candidate):
                    raise UpscaleError(f"URL 对应文件不存在: {candidate}")
                return candidate
        raise UpscaleError(f"不支持的视频 URL 前缀: {url}")

    raise UpscaleError("请提供 video_path（绝对路径）或 video_url（如 /api/final/<项目>/<文件>）")


def _upscale_url_for_path(path: str) -> str:
    """把输出目录下的绝对路径反查为可播放 URL（用于前端对比预览）"""
    try:
        p = os.path.abspath(path)
    except Exception:
        return ""
    for prefix, base in UPSCALE_URL_PREFIXES:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            return prefix + rel
    for _label, base in COMFY_VIDEO_DIRS:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            sub = os.path.relpath(b, os.path.abspath(COMFYUI_OUTPUT_DIR)).replace(os.sep, "/")
            return _comfy_view_url(rel, "" if sub == "." else sub)
    # ComfyUI 侧任意子目录产出（video / v5video / upscale / 自定义工作流目录等）：
    # 统一走 comfyview 代理，保证「超分前」对比预览有可播放地址
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if p.startswith(root + os.sep):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        return _comfy_view_url(os.path.basename(rel), os.path.dirname(rel).replace("\\", "/"))
    return ""


upscale_lock = threading.Lock()


upscale_tasks = {}


