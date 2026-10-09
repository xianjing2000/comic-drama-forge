# -*- coding: utf-8 -*-
"""一致性校验 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @consistency_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import consistency
import novel_to_script
import project_store
from config import CHARACTERS_DIR, SCRIPT_DIR, STORYBOARDS_DIR
# 2026-10-09 修复遗漏：本模块 3 处使用 _shot_num_key（L61/70/86，_shared.py:1063 = shot_key.norm_shot_key），
# 原先未导入 → 一致性校验接口一旦执行就 NameError → 500。
from routes._shared import (_app_logger, _ep_read_dir, _first_existing, _qc_load_cfg,
                            _safe_project, _shot_num_key, comfyui_client)

consistency_bp = Blueprint('consistency', __name__)

def _consistency_collect(project_name: str, episode_no: int = None) -> dict:
    """从磁盘采集一致性校验所需素材

    - character_refs：output/assets/characters/<项目>/<角色>/front.png（缺则 base.png）
    - shot_images：优先读 storyboards manifest（含每镜产出图与 QC 记录），
      其次按 shot_NN.png 命名约定扫描，再从剧本补齐 characters_in_shot
    """
    chars_dir = os.path.join(CHARACTERS_DIR, project_name)
    character_refs = {}
    asset_dirs = {}
    if os.path.isdir(chars_dir):
        for name in sorted(os.listdir(chars_dir)):
            d = os.path.join(chars_dir, name)
            if not os.path.isdir(d):
                continue
            ref = _first_existing(os.path.join(d, "front.png"), os.path.join(d, "base.png"))
            if ref:
                character_refs[name] = ref
            asset_dirs[name] = d

    # 分镜图 + 剧本角色归属（统一用数字键，保证文件名/剧本/manifest 三方对齐）
    shot_images = {}
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project_name, episode_no)

    # 剧本 → 每镜角色
    shot_chars = {}
    try:
        key = project_store.safe_key(project_name)
        script = None
        if episode_no:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        if not script:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
            if eps:
                first = eps[0]
                no = first if isinstance(first, int) else (
                    first.get("episode_no") if isinstance(first, dict) else 1)
                script = novel_to_script.load_episode_script(SCRIPT_DIR, key, no)
        for s in ((script or {}).get("shots") or []):
            if isinstance(s, dict):
                shot_chars[_shot_num_key(s.get("shot_id"))] = s.get("characters_in_shot") or []
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"剧本读取失败（一致性校验将缺少角色归属）：{e}")

    # 先按目录命名约定扫描
    if os.path.isdir(sb_dir):
        for fn in sorted(os.listdir(sb_dir)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            k = _shot_num_key(os.path.splitext(fn)[0])
            shot_images[k] = {"image": os.path.join(sb_dir, fn),
                              "characters": shot_chars.get(k, [])}

    # manifest 覆盖（可能指向非默认目录，并附带 QC 分数）
    manifest_path = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            for s in (mf.get("shots") or []):
                if not isinstance(s, dict):
                    continue
                fp = comfyui_client.resolve_local_path(s.get("file") or "")
                if not (fp and os.path.isfile(fp)):
                    continue
                k = _shot_num_key(s.get("shot_id"))
                shot_images.setdefault(k, {})
                shot_images[k]["image"] = fp
                shot_images[k]["characters"] = shot_chars.get(k, [])
                shot_images[k]["qc"] = (s.get("qc") or {})
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"分镜 manifest 读取失败：{e}")

    return {"character_refs": character_refs, "shot_images": shot_images,
            "asset_dirs": asset_dirs}
@consistency_bp.route('/api/consistency/run', methods=['POST'])
def api_consistency_run():
    """执行一致性校验（可指定 project_name / episode_no / 是否含资产多视图）"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or '')
    if not project_name:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    episode_no = data.get('episode_no')
    try:
        collect = _consistency_collect(project_name, episode_no)
        cfg = _qc_load_cfg()
        report = consistency.run(
            project_name,
            character_refs=collect["character_refs"],
            shot_images=collect["shot_images"],
            asset_dirs=collect["asset_dirs"],
            cfg=cfg,
            include_assets=bool(data.get('include_assets', True)),
            include_shots=bool(data.get('include_shots', True)),
        )
    except Exception as e:  # noqa: BLE001
        _app_logger().exception("一致性校验失败")
        return jsonify({"success": False, "error": f"一致性校验失败：{e}"}), 500
    return jsonify({"success": True, "project": project_name,
                    "summary": report.get("summary"),
                    "report": report, "report_path": report.get("report_path")})
@consistency_bp.route('/api/consistency/report/<path:project_name>', methods=['GET'])
def api_consistency_report(project_name):
    """读取已有的一致性报告（不重新校验）"""
    project_name = _safe_project(project_name)
    rep = consistency.load_report(project_name)
    if not rep:
        return jsonify({"success": False, "error": "暂无一致性报告，请先执行校验",
                        "project": project_name}), 404
    return jsonify({"success": True, "project": project_name, "report": rep,
                    "summary": rep.get("summary")})
