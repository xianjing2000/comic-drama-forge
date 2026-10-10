# -*- coding: utf-8 -*-
'''小说分集/剧本 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
import logging
from flask import Blueprint, jsonify, request, send_file, abort, current_app  # noqa: F401
import os    # noqa: F401
import sys   # noqa: F401
import re    # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
import threading   # noqa: F401
import hashlib     # noqa: F401
import shutil      # noqa: F401
import random      # noqa: F401
import base64      # noqa: F401
import datetime    # noqa: F401
import traceback   # noqa: F401
import subprocess  # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from agent_core import logger
from config import NOVEL_DEFAULT_SHOTS
from config import SCRIPT_DIR
from job_state import generation_state
from job_state import lock
from novel_parser import NOVELS_DIR
from novel_parser import NovelParseError
from novel_parser import chapter_body_chars
from novel_parser import ensure_chapter_structure
from novel_parser import get_novel
from novel_parser import read_novel_text
from routes._shared import EPISODE_BATCH_LIMIT
from routes._shared import _ai_guide_response
from routes._shared import _apply_project_settings
from routes._shared import _current_llm_client
from routes._shared import _novel_key
from routes._shared import _optional_llm_client
from routes._shared import _project_style
from routes._shared import _resolve_novel_project
from workers.episodes import _episodes_worker
from workers.screenplay import _screenplay_worker
import novel_screenplay
import project_store

logger = logging.getLogger(__name__)

novels_api_bp = Blueprint('novels_api', __name__)

@novels_api_bp.route('/api/novels/<novel_id>/screenplay/generate', methods=['POST'])
def api_novel_screenplay_generate(novel_id):
    """生成某章的文学剧本（两段式生产 ①，异步）。body: {chapter, project_id/project_name?, style?, episode_no?}"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法生成文学剧本")
    data = request.json or {}
    chapters = meta.get("chapters") or []
    try:
        ch_idx = int(data.get("chapter") or 1)
    except (TypeError, ValueError):
        ch_idx = 1
    chapter = next((c for c in chapters if int(c.get("index") or 0) == ch_idx), None)
    if chapter is None:
        return jsonify({"success": False, "error": f"找不到章节 {ch_idx}"}), 404
    proj = _resolve_novel_project(data, meta)
    key = _novel_key(meta, proj["dir_key"])
    try:
        episode_no = max(1, int(data.get("episode_no") or ch_idx))
    except (TypeError, ValueError):
        episode_no = ch_idx
    style = (data.get("style") or _project_style(proj["dir_key"]) or "3D动漫渲染")
    task_id = f"screenplay_{novel_id}_{episode_no}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {"status": "running", "progress": 2,
                                     "phase": "prepare", "message": "准备章节正文…",
                                     "novel_id": novel_id, "project_key": key}
    threading.Thread(target=_screenplay_worker,
                     args=(task_id, meta, chapter, key, style, episode_no),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "project_key": key, "episode_no": episode_no})


@novels_api_bp.route('/api/novels/<novel_id>/screenplay/<int:episode_no>', methods=['GET'])
def api_novel_screenplay_get(novel_id, episode_no):
    """读取已生成的文学剧本（Markdown）。?project= 指定项目键（缺省按小说找项目）。"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project') or '').strip()
    rec = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, rec["dir_key"] if rec else None)
    path = novel_screenplay.screenplay_path(key, episode_no)
    md = novel_screenplay.load_screenplay(key, episode_no) if os.path.isfile(path) else ""
    return jsonify({"success": True, "exists": bool(md), "markdown": md,
                    "path": path, "project_key": key, "episode_no": int(episode_no)})


@novels_api_bp.route('/api/novels/<novel_id>/episodes/generate', methods=['POST'])
def api_novel_episodes_generate(novel_id):
    """按章节分集生成：单章生成 / 批量生成多集（每章一集）

    body: {chapters:[1,2,3] | start:1,end:3, style, target_shots, overwrite}
    """
    try:
        # 生成剧本前先做章节目录体检（LLM 判断真章节，结果缓存；失败退回规则折叠）
        meta = ensure_chapter_structure(NOVELS_DIR, novel_id, _optional_llm_client())
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法按章生成剧本")

    all_chapters = meta.get("chapters") or []
    if not all_chapters:
        return jsonify({"success": False,
                        "error": "该小说未识别到章节标记，无法按章分集；请改用「AI 转成剧本」整本处理"}), 400

    data = request.json or {}
    style = (data.get('style') or '3D动漫渲染').strip() or '3D动漫渲染'
    # ⭐ 2026-10-10：0 = 不预设镜数（由原文信息密度决定）；显式传值仍 clamp 到 4~40。
    try:
        _ts_raw = int(data.get('target_shots')
                      if data.get('target_shots') not in (None, '') else NOVEL_DEFAULT_SHOTS)
    except (TypeError, ValueError):
        _ts_raw = NOVEL_DEFAULT_SHOTS
    target_shots = 0 if _ts_raw <= 0 else max(4, min(_ts_raw, 40))
    overwrite = bool(data.get('overwrite'))
    style = _apply_project_settings(style, data.get('project_name') or novel_id)

    by_index = {}
    for c in all_chapters:
        by_index[int(c.get("index") or 0)] = c

    requested = data.get('chapters')
    if isinstance(requested, (str, int)):
        requested = [requested]
    picks = []
    if isinstance(requested, list) and requested:
        for x in requested:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if xi in by_index and xi not in [p.get("index") for p in picks]:
                picks.append(by_index[xi])
    else:
        start = data.get('start')
        end = data.get('end')
        try:
            s = int(start) if start is not None else None
            e = int(end) if end is not None else None
        except (TypeError, ValueError):
            s = e = None
        if s is not None or e is not None:
            lo = s if s is not None else 1
            hi = e if e is not None else max(by_index)
            picks = [c for idx, c in sorted(by_index.items()) if lo <= idx <= hi]

    if not picks:
        return jsonify({"success": False, "error": "未选择有效章节（chapters 或 start/end 至少提供一项）"}), 400

    # 空壳章节防线（2026-10-02）：去掉标题行后几乎没有正文的条目绝不是可拍摄内容。
    # 曾经拿 11 个字的「第一卷：魔性不改」跑完整条流水线：模型凭空编了 8 个镜头
    # （「魔性不改 / 大道无情 / 唯我独尊」这类自造口号），原文台词一句没用，
    # 整半章内容丢失，而覆盖率检查还报 100%（没有正文单元可核对 → 空集恒真）。
    # 宁可在这里明确挡下并说清原因，也不产出整集幻觉剧本。
    try:
        _novel_text = read_novel_text(NOVELS_DIR, novel_id) or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("空壳章节防线：正文读取失败（跳过检查）：%s", e)
        _novel_text = ""
    if _novel_text and len(_novel_text) > 5000:
        _shells = []
        for _c in picks:
            try:
                _body = chapter_body_chars(_novel_text, _c)
            except Exception:  # noqa: BLE001
                _body = 0
            if _body < 120:
                _shells.append("%s（%d 字）" % (_c.get("title") or _c.get("index"), _body))
        if _shells:
            return jsonify({
                "success": False,
                "error": ("下列章节去掉标题行后几乎没有正文，像是卷/分部标题而不是正文，"
                          "不能据此生成剧本：" + "、".join(_shells[:5]) +
                          "。请改选其后的正文章节（本书一节正文通常约 3000 字）。"),
                "shell_chapters": _shells,
            }), 400
    if len(picks) > EPISODE_BATCH_LIMIT:
        return jsonify({"success": False,
                        "error": f"单次批量最多 {EPISODE_BATCH_LIMIT} 集，本次选择了 {len(picks)} 集；请缩小范围"}), 400

    picks.sort(key=lambda c: int(c.get("index") or 0))
    # A：绑定/自动建立该项目，剧本与后续产物全部落在该项目目录
    proj = _resolve_novel_project(data, meta)
    key = _novel_key(meta, proj["dir_key"])
    ep_dir = os.path.abspath(os.path.join(SCRIPT_DIR, key))

    # ⚠️ 互斥（2026-09-17 E2E 实测教训）：同一篇小说若已有分集生成任务在跑，
    # 再派一次会让两轮并发处理同一章 —— 互相抢模型配额（实测把接口打成 503 风暴），
    # 结果「一个成功写盘 + 一个 failed」，用户看到失败但产物其实是好的（覆盖率 100%）。
    # 规则：章节有重叠 → 直接复用正在跑的那个任务；章节不重叠 → 允许并发。
    # 旧任务没记 picks（历史数据）时保守视为冲突。
    want = {int(c.get("index") or 0) for c in picks}
    with lock:
        for _tid, _st in list(generation_state.items()):
            if not (isinstance(_st, dict) and _st.get("status") == "running"
                    and str(_tid).startswith("episodes_")):
                continue
            if _st.get("novel_id") != meta.get("novel_id"):
                continue
            _running = {int(x) for x in (_st.get("picks") or [])}
            if _running and not (_running & want):
                continue                      # 章节不重叠，互不干扰
            return jsonify({
                "success": True, "reused": True, "task_id": _tid, "status": "running",
                "novel_id": meta.get("novel_id"),
                "message": (f"该小说已有分集生成任务在跑"
                            f"（{_st.get('current') or 0}/{_st.get('total') or 0} 集），"
                            "本次请求已复用它 —— 避免同一章被并发生成两次"),
                "chapters": [{"index": c.get("index"), "title": c.get("title"),
                              "char_count": c.get("char_count")} for c in picks],
                "episodes": [c.get("index") for c in picks],
                "total": len(picks),
                "project_id": proj["id"], "project_key": key,
                "episode_dir": ep_dir,
            })

    task_id = f"episodes_{meta.get('novel_id')}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": f"准备生成 {len(picks)} 集…", "current": 0, "total": len(picks),
            "novel_id": meta.get("novel_id"), "results": [],
            # 记录本任务负责的章节，供上面的互斥判断比对重叠
            "picks": sorted(int(c.get("index") or 0) for c in picks),
            "project_id": proj["id"], "project_key": key,
            "episode_dir": ep_dir,
        }
    threading.Thread(target=_episodes_worker,
                     args=(task_id, meta, picks, style, target_shots, overwrite, key,
                           # ⭐ 2026-10-06（用户指定）：默认开启「文学剧本→开拍剧本」两段式全自动生产。
                           # 用户是**自动项目**，明确"文学剧本无需人审"——故默认 True：自动出文学剧本、
                           # 自动改写成开拍剧本（分镜/台词），全程不插入人工审核。前端若显式传
                           # use_screenplay=false 仍可回退"章节原文直接开拍"（保持兼容）。
                           data.get('use_screenplay', True)),
                     daemon=True).start()
    return jsonify({
        "success": True, "task_id": task_id, "status": "started",
        "novel_id": meta.get("novel_id"), "style": style, "target_shots": target_shots,
        "use_screenplay": data.get('use_screenplay', True),
        "project_id": proj["id"], "project_key": key, "project_name": proj["name"],
        "episode_dir": ep_dir,
        "chapters": [{"index": c.get("index"), "title": c.get("title"),
                      "char_count": c.get("char_count")} for c in picks],
        "episodes": [c.get("index") for c in picks],
        "total": len(picks),
    })
