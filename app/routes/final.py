# -*- coding: utf-8 -*-
"""成片生成与取文件 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @final_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import project_store
import video_watermark
from config import FINAL_DIR, WATERMARK_DIR
from video_postprocess import VideoPostProcessor
from routes._shared import _app_logger, _body, _project_or_400, _serve_safe, _wm_load_cfg, register_final_deliverable

final_bp = Blueprint('final', __name__)

def _wm_apply_to_final(final_path: str, project_name: str) -> dict:
    """成片后处理：水印开启时额外产出一份带水印成片；默认关闭则整体跳过"""
    try:
        cfg = _wm_load_cfg()
        if not cfg.get("enabled"):
            return {"enabled": False, "skipped": True,
                    "message": "视频水印未开启（默认关闭，成片保持无水印）"}
        out_dir = os.path.join(WATERMARK_DIR, project_name)
        res = video_watermark.apply_watermark(final_path, cfg=cfg, out_dir=out_dir)
        out_path = res.get("out_path") or ""
        res.update({
            "type": cfg.get("type"), "mode": cfg.get("mode"),
            "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode"), cfg.get("mode")),
            "url": (f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}"
                    if out_path else ""),
        })
        if not res.get("ok"):
            _app_logger().warning(f"成片水印烧写失败（不影响无水印成片）：{res.get('error')}")
        return res
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"成片水印处理异常（忽略）：{e}")
        return {"enabled": True, "ok": False, "error": str(e)}
video_processor = VideoPostProcessor()
@final_bp.route('/api/final/video', methods=['POST'])
def api_generate_final():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（成片合成需定位项目内剧本，缺省无合理语义）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    script_path = data.get('script_path', '')

    # P0-4：剧本路径必须先落在项目输出目录内（与 project_store.bind_script 同口径），
    # 越界（如 C:/Windows/... 或项目外路径）直接拒读，避免被 index.json 里被污染的
    # 绝对路径拖出目录读走任意文件。
    if not script_path or not project_store.is_path_inside_output(script_path):
        return jsonify({"error": "剧本路径必须在项目输出目录内（output/），越界路径已拒读"}), 400
    if not os.path.exists(script_path):
        return jsonify({"error": "剧本文件不存在"}), 400

    try:
        # 集号与剧本一起解析：合成必须知道是第几集（审计 S4 —— 旧代码合成完才读集号，
        # 而合成函数压根没有集号入参，于是第 2 集及以后合成的是第 1 集的片段）
        ep_no = 0
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                _sc = json.load(f) or {}
            ep_no = int(_sc.get('episode_no')
                        or (_sc.get('metadata') or {}).get('episode_no') or 0)
        except Exception:  # noqa: BLE001 - 剧本读不到就退回第 1 集
            ep_no = 0
        output = video_processor.generate_final_video(script_path, project_name, ep_no or 1)
        if not output:
            return jsonify({"error": f"没有可合并的视频片段（第 {ep_no or 1} 集），"
                                     "请先完成步骤5的视频生成"}), 400
        filename = os.path.basename(output)
        # URL 按「相对 FINAL_DIR 的路径」拼，避免成片落在项目子目录时 404
        try:
            rel_path = os.path.relpath(output, FINAL_DIR).replace(os.sep, "/")
        except ValueError:
            rel_path = f"{project_name}/{filename}"
        resp = {
            "success": True,
            "output_path": output,                       # 保留原字段（本地绝对路径）
            "episode_no": ep_no or 1,
            "filename": filename,
            "url": f"/api/final/{rel_path}"              # 前端可直接播放/下载的 URL
        }
        # 整集成片落盘 → 自动登记进「成品验收」队列（用户只需看这里）
        reg = register_final_deliverable(
            project_name, ep_no or 1, output,
            meta={"source": "final_video", "script": os.path.basename(script_path)})
        resp["deliverable"] = {"registered": bool(reg.get("registered")),
                               "reason": reg.get("reason") or "",
                               "episode_no": ep_no or 1}
        # 视频水印（默认关闭；开启后额外产出一份带水印成片，不影响上面的无水印成片）
        resp["watermark"] = _wm_apply_to_final(output, project_name)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@final_bp.route('/api/final/<path:filename>')
def api_final_file(filename):
    """提供最终成片文件访问（新增：使成片可在页面内联播放/下载）"""
    # conditional=True 支持 Range 请求，视频可拖动进度条
    return _serve_safe(FINAL_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')
