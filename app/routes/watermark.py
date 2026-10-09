# -*- coding: utf-8 -*-
"""水印配置与应用 API 蓝图（Blueprint 拆分第十二批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @watermark_bp.route。
日志器用上下文安全的 _app_logger()。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import video_watermark
from config import WATERMARK_CONFIG_PATH, WATERMARK_DIR
from routes._shared import _safe_project, _serve_safe, project_store

from routes._shared import _wm_load_cfg

watermark_bp = Blueprint('watermark', __name__)

def _wm_view(cfg: dict) -> dict:
    view = video_watermark.public_view(cfg)
    view["config_path"] = os.path.abspath(WATERMARK_CONFIG_PATH)
    view["output_dir"] = os.path.abspath(WATERMARK_DIR)
    return view
@watermark_bp.route('/api/watermark/config', methods=['GET'])
def api_watermark_config_get():
    cfg = _wm_load_cfg()
    return jsonify({"success": True, "config": cfg, "view": _wm_view(cfg),
                    "config_path": os.path.abspath(WATERMARK_CONFIG_PATH),
                    "output_dir": os.path.abspath(WATERMARK_DIR),
                    "message": "视频水印默认关闭；开启并保存后，成片会自动追加一份带水印版本"})
@watermark_bp.route('/api/watermark/config', methods=['POST'])
def api_watermark_config_save():
    data = request.json or {}
    cfg = video_watermark.save_config(WATERMARK_CONFIG_PATH, data)
    view = _wm_view(cfg)
    warning = ""
    if cfg.get("enabled") and not view.get("ready"):
        warning = {
            "disabled": "禁用",
            "no_ffmpeg": "未找到 ffmpeg，无法烧写水印",
            "no_font": "未找到可用字体，文字水印无法生效",
            "no_image": "图片水印文件不存在",
        }.get(view.get("ready_state"), "")
    return jsonify({"success": True, "config": cfg, "view": view,
                    "config_path": os.path.abspath(WATERMARK_CONFIG_PATH),
                    "warning": warning,
                    "message": ("视频水印已开启（模式：%s）" % view["mode_label"]) if cfg.get("enabled")
                               else "视频水印已关闭（成片不会带水印）"})
@watermark_bp.route('/api/watermark/apply', methods=['POST'])
def api_watermark_apply():
    """对指定视频烧写水印（手动单段验证用）。body: video_path / project_name / config(临时覆盖)"""
    data = request.json or {}
    src = str(data.get("video_path") or "").strip()
    project_name = _safe_project(data.get("project_name") or "watermark")
    cfg = video_watermark.load_config(WATERMARK_CONFIG_PATH)
    if isinstance(data.get("config"), dict):
        cfg = video_watermark.normalize(data["config"], base=cfg)
    if not src:
        return jsonify({"success": False, "error": "缺少 video_path"}), 400
    if not os.path.isfile(src):
        return jsonify({"success": False, "error": f"视频不存在：{src}"}), 400
    if data.get("enabled") is not None:
        cfg["enabled"] = bool(data.get("enabled"))
    res = video_watermark.apply_watermark(src, cfg=cfg,
                                          out_dir=os.path.join(WATERMARK_DIR, project_name))
    out_path = res.get("out_path") or ""
    if res.get("skipped"):
        return jsonify({"success": True, "skipped": True, "config": cfg,
                        "message": "水印未开启，未产出带水印文件"})
    if not res.get("ok"):
        return jsonify({"success": False, "config": cfg, "error": res.get("error")}), 500
    return jsonify({"success": True, "config": cfg, "output_path": out_path,
                    "url": f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}",
                    "mode": cfg.get("mode"),
                    "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode")),
                    "elapsed": res.get("elapsed"),
                    "message": "水印已烧写"})
@watermark_bp.route('/api/watermark/list/<path:project_name>', methods=['GET'])
def api_watermark_list(project_name):
    folder = project_store.project_dirs(WATERMARK_DIR, project_name)
    files = []
    for d in folder:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".mp4", ".mov", ".mkv")):
                    files.append(os.path.join(d, f))
    return jsonify({"success": True, "files": files, "count": len(files),
                    "output_dir": os.path.abspath(WATERMARK_DIR)})
@watermark_bp.route('/api/watermark/file/<path:filename>', methods=['GET'])
def api_watermark_file(filename):
    return _serve_safe(WATERMARK_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')
