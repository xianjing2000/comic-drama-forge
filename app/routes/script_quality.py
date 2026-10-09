# -*- coding: utf-8 -*-
"""原文覆盖率 / 跨集剧本一致性 API 蓝图（Blueprint 拆分第三批）。

4 条路由：/api/coverage/{overview,episode} + /api/script-consistency/{overview,episode}。
依赖：config（CONTINUITY_DIR / SCRIPT_DIR）、novel_parser.NovelParseError、
coverage / script_consistency / novel_to_script 三个叶子模块 + routes/_shared._resolve_continuity_key。

⚠️ URL 规则与响应体一字不改，只把 @script_qa_bp.route 换成 @script_qa_bp.route。
"""
import os

from flask import Blueprint, jsonify, request

import continuity
import coverage
import novel_to_script
import script_consistency
from config import CONTINUITY_DIR, SCRIPT_DIR
from novel_parser import NovelParseError
from routes._shared import _resolve_continuity_key

script_qa_bp = Blueprint('script_qa', __name__)

@script_qa_bp.route('/api/coverage/<novel_id>', methods=['GET'])
def api_coverage_overview(novel_id):
    """项目级原文覆盖率总览（④⑤）：逐集覆盖率百分比 / 阈值 / 是否达标 / 遗漏数 / 补生成镜数"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
    rows = []
    for e in eps:
        ep = int(e.get("episode_no") or 0)
        rep = coverage.load_coverage_report(CONTINUITY_DIR, key, ep)
        if rep:
            row = coverage.summary_for_meta(rep)
            row.update({"episode_no": ep, "available": True,
                        "chapter_title": e.get("chapter_title"),
                        "script_path": e.get("path")})
        else:
            row = {"episode_no": ep, "available": False,
                   "coverage_percent": None, "passed": None,
                   "chapter_title": e.get("chapter_title"),
                   "script_path": e.get("path"),
                   "note": "该集尚无覆盖率报告（未按新流程重跑）"}
        rows.append(row)
    return jsonify({
        "success": True, "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "novel_title": meta.get("title") or meta.get("name"),
        "project_key": key,
        "threshold_percent": round(float(getattr(novel_to_script, "COVERAGE_THRESHOLD", 0.95)) * 100, 2),
        "coverage_dir": os.path.abspath(os.path.join(CONTINUITY_DIR, key, "episodes")),
        "count": len(rows),
        "episodes": rows,
    })
@script_qa_bp.route('/api/coverage/<novel_id>/<int:episode_no>', methods=['GET'])
def api_coverage_episode(novel_id, episode_no):
    """单集原文覆盖率详情（⑤）：覆盖率 / 阈值 / 遗漏清单 / 补生成记录 / 逐轮复检轨迹"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.episode_coverage_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)
@script_qa_bp.route('/api/script-consistency/<novel_id>', methods=['GET'])
def api_script_consistency_overview(novel_id):
    """P0-3 剧本↔原著一致性总览：逐集三件套结论 / 泄漏数 / 要素覆盖率 / 是否通过 / 报告路径
    （注意与 /api/consistency/*「资产多视图一致性」区分：本组专指剧本 ↔ 本章原著）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
    rows = []
    for e in eps:
        ep = int(e.get("episode_no") or 0)
        rep = script_consistency.load_consistency_report(CONTINUITY_DIR, key, ep)
        if rep:
            row = script_consistency.summary_for_meta(
                rep, script_consistency.consistency_report_path(CONTINUITY_DIR, key, ep))
            row.update({"episode_no": ep, "available": True,
                        "chapter_title": e.get("chapter_title"),
                        "script_path": e.get("path")})
        else:
            row = {"episode_no": ep, "available": False, "passed": None,
                   "chapter_title": e.get("chapter_title"),
                   "script_path": e.get("path"),
                   "note": "该集尚无一致性校验报告（未按新流程重跑）"}
        rows.append(row)
    return jsonify({
        "success": True, "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "novel_title": meta.get("title") or meta.get("name"),
        "project_key": key,
        "algorithm": script_consistency.SCRIPT_CONSISTENCY_VERSION,
        "consistency_dir": os.path.abspath(os.path.join(CONTINUITY_DIR, key, "episodes")),
        "count": len(rows),
        "episodes": rows,
    })
@script_qa_bp.route('/api/script-consistency/<novel_id>/<int:episode_no>', methods=['GET'])
def api_script_consistency_episode(novel_id, episode_no):
    """单集 P0-3 一致性详情：章节锚定 / 元信息泄漏 / 要素覆盖 / 问题清单 / 定向修复轨迹"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = script_consistency.episode_consistency_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)

