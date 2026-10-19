# -*- coding: utf-8 -*-
"""导出 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @export_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import nle_export
from flask import abort
from routes._shared import _app_logger, _load_script_for, _project_or_400, _safe_project

export_bp = Blueprint('export', __name__)

def _serve_attachment(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的附件下载统一入口（as_attachment + download_name）。

    与 _serve_safe 防护逻辑相同，但额外指定 as_attachment=True 与 download_name。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    send_kw.setdefault("as_attachment", True)
    send_kw.setdefault("download_name", os.path.basename(target))
    return send_file(target, **send_kw)
@export_bp.route('/api/export/run', methods=['POST'])
def api_export_run():
    """一键导出：剪映草稿 + FCPML(FCPXML 风格) + SRT + 帧序列清单

    body: {project_name, episode_no?, formats?: ["jianying","fcpxml","srt","frames"]}
    """
    data = (request.get_json(silent=True) or {})
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    formats = data.get('formats')
    # B-17 P2-13：传集号给 nle_export，按集号过滤视频目录，避免跨集混用素材
    ep_no = data.get('episode_no')
    try:
        results = nle_export.export_all(project, script,
                                        formats=formats if isinstance(formats, list) else None,
                                        episode=ep_no)
    except Exception as e:  # noqa: BLE001
        _app_logger().exception("NLE 导出失败")
        return jsonify({"success": False, "error": f"导出失败：{e}"}), 500
    return jsonify({"success": bool(results.get("ok")), "project": project, **results})
@export_bp.route('/api/export/list', methods=['GET'])
def api_export_list():
    """导出记录列表（可按项目过滤）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else None
    try:
        items = nle_export.list_exports(project)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 500
    # 前端（ExportPage / 工作台 ExportTab）统一消费 `files` 字段：
    # 这里在保留 `items` 原始结构的同时，补一份前端可直接渲染的规范化列表。
    files = []
    for it in items:
        p = it.get("path") or ""
        name = os.path.basename(p) if p else ""
        files.append({
            "format": it.get("format"),
            "filename": name,
            "exists": bool(p and os.path.isfile(p)),
            "path": p,
            "dir": it.get("dir"),
            "project": it.get("project"),
            "exported_at": it.get("exported_at"),
            "shot_count": it.get("shot_count"),
            "total_sec": it.get("total_sec"),
            "size_mb": it.get("size_mb"),
        })

    return jsonify({"success": True, "count": len(files), "items": items, "files": files})
@export_bp.route('/api/export/download/<path:filename>')
def api_export_download(filename):
    """导出产物下载（限导出根目录内）"""
    return _serve_attachment(nle_export.EXPORT_DIR, filename)
@export_bp.route('/api/export/batch', methods=['POST'])
def api_export_batch():
    """跨集批量导出（E-3，2026-10-16）：对多个集各跑一次 ``nle_export.export_all``，
    再把本批产物打包成一个 zip 供下载。

    body: ``{project_name, episodes: [int, ...], formats?: [...], zip_name?}``
    - ``episodes`` **必填**（非空数组）——不猜、不扫全量，避免把无关产物一起打包。
    - 取代此前的死端点 ``POST /api/export/current``（只查文件是否存在、从不生成；全仓无调用方）。

    返回：``{success, project, episodes, per_episode, zip, filename, url}``
    """
    import zipfile
    from datetime import datetime as _dt

    data = (request.get_json(silent=True) or {})
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    eps_raw = data.get('episodes')
    if not isinstance(eps_raw, list) or not eps_raw:
        return jsonify({"success": False, "error": "episodes 必须是非空数组"}), 400
    try:
        eps = sorted({int(e) for e in eps_raw if str(e).strip() != ""})
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episodes 含非法集号"}), 400
    if not eps:
        return jsonify({"success": False, "error": "episodes 解析后为空"}), 400

    formats = data.get('formats')
    fmt = formats if isinstance(formats, list) and formats else None

    per_ep = {}
    for ep in eps:
        script = _load_script_for(project, ep)
        if not script:
            per_ep[ep] = {"ok": False, "error": "剧本不存在"}
            continue
        try:
            per_ep[ep] = nle_export.export_all(project, script, formats=fmt, episode=ep)
        except Exception as e:  # noqa: BLE001
            _app_logger().exception("NLE 批量导出失败 ep=%s", ep)
            per_ep[ep] = {"ok": False, "error": str(e)}

    out_dir = os.path.join(nle_export.EXPORT_DIR, project)
    os.makedirs(out_dir, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    zip_name = str(data.get('zip_name') or f"{project}_{len(eps)}集_{ts}.zip")
    if not zip_name.lower().endswith('.zip'):
        zip_name += '.zip'
    zip_path = os.path.join(out_dir, zip_name)
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for _ep, _res in per_ep.items():
                for _v in ((_res.get("results") or {}).values()):
                    if not isinstance(_v, dict) or not _v.get("ok"):
                        continue
                    _p = _v.get("path")
                    if _p and os.path.isfile(str(_p)):
                        zf.write(str(_p), arcname=f"ep{_ep:02d}/{os.path.basename(str(_p))}")
                    elif _v.get("draft_dir") and os.path.isdir(str(_v["draft_dir"])):
                        _dd = str(_v["draft_dir"])
                        for _root, _dirs, _files in os.walk(_dd):
                            for _f in _files:
                                _fp = os.path.join(_root, _f)
                                zf.write(_fp, arcname=f"ep{_ep:02d}/{os.path.relpath(_fp, _dd)}")
    except Exception as e:  # noqa: BLE001
        _app_logger().exception("批量导出打包失败")
        return jsonify({"success": False, "error": f"打包失败：{e}"}), 500

    return jsonify({"success": True, "project": project, "episodes": eps,
                    "per_episode": per_ep, "zip": zip_path, "filename": zip_name,
                    "url": f"/api/export/download/{project}/{zip_name}"})
