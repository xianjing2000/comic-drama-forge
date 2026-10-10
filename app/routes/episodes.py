# -*- coding: utf-8 -*-
"""分集管理 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @episodes_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import autopilot
import continuity
import novel_to_script
import project_store
import re
from config import CONTINUITY_DIR, FINAL_DIR, NOVELS_DIR, PROJECT_OUTPUT_DIR, SCRIPT_DIR, STORYBOARDS_DIR, VIDEOS_DIR
from novel_parser import NovelParseError, get_novel
from routes._shared import _app_logger, _ep_read_dir, _novel_key, _safe_project

episodes_bp = Blueprint('episodes', __name__)

def _episode_progress(project_name: str, episode_no: int, shot_count: int = 0) -> dict:
    """从磁盘真实产物推导单集的进度与状态（界面回显的唯一依据）

    背景（P1 线上问题）
    ------------------
    `novel_to_script.list_episodes` 只返回镜头数/覆盖率等字段，**从不返回 status / completed_shots**。
    前端 `EpisodeInfo.status` 因此恒为 undefined，`completed_shots` 恒为 undefined，于是：
      - 状态徽标一律落到兜底分支 → 第 1 集跑完 4 小时（含 final 失败）仍显示「○ 待生产」；
      - 进度显示「0 / 24 镜头」；
      - 概览统计「已完成 0 / 生产中 0」；
    用户完全无法从界面判断任务是否结束、成功还是失败，体验等同卡死。

    修复：以磁盘产物 + 生产历史为准推导状态，前端拿到的是真实进度。
    判定顺序（先看终态，再看进行中，最后看未开始）：
      1) 成片存在                  → done
      2) 最近一次生产记录为失败      → failed（并把错误原文回显）
      3) 托管正在跑这一集 或 已有中间产物 → producing
      4) 其余                      → pending
    """
    proj = _safe_project(project_name or "")
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    def _count(d: str, pattern: str) -> int:
        try:
            return sum(1 for f in os.listdir(d) if re.match(pattern, f))
        except OSError:
            return 0

    sb_dir = _ep_read_dir(STORYBOARDS_DIR, proj, ep)
    vid_dir = _ep_read_dir(VIDEOS_DIR, proj, ep)
    storyboards = _count(sb_dir, r"^shot_\d+\.png$")
    shot_videos = _count(vid_dir, r"^shot_\d+\.mp4$")
    full_video = ""
    for cand in (f"ep{ep:02d}_full.mp4", "episode_full.mp4"):
        p = os.path.join(vid_dir, cand)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            full_video = p
            break
    final_file = os.path.join(FINAL_DIR, proj, f"ep{ep:02d}_final.mp4")
    final_ready = os.path.isfile(final_file) and os.path.getsize(final_file) > 0

    # 生产历史：取该集**最后一条**记录（不按时间窗过滤，否则老失败会被漏掉）
    last_run = None
    try:
        hp = os.path.join(PROJECT_OUTPUT_DIR, "autopilot", proj, "history.jsonl")
        if os.path.isfile(hp):
            with open(hp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(row.get("episode_no") or 0) == ep:
                        last_run = row
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"读取生产历史失败（{proj} 第{ep}集）：{e}")

    # 托管是否正在跑这一集
    running_this = False
    try:
        cur = (autopilot.status(proj) or {}).get("current") or {}
        running_this = (cur.get("project") == proj and int(cur.get("episode") or 0) == ep
                        and bool((autopilot.status(proj) or {}).get("running")))
    except Exception:  # noqa: BLE001
        running_this = False

    total = int(shot_count or 0)
    completed = storyboards
    if final_ready:
        status = "done"
    elif last_run is not None and not last_run.get("ok") and \
            str(last_run.get("status") or "") == "failed":
        status = "failed"
    elif running_this or storyboards or shot_videos or full_video:
        status = "producing"
    else:
        status = "pending"

    # 成片已出但镜头没画齐，同样视为「完成」但标注不齐（与交付物登记口径一致）
    incomplete = bool(final_ready and total and completed < total)
    return {
        "status": status,
        "completed_shots": min(completed, total) if total else completed,
        "storyboard_count": storyboards,
        "shot_video_count": shot_videos,
        "full_video": bool(full_video),
        "final_ready": final_ready,
        "final_file": final_file if final_ready else "",
        "incomplete_shots": incomplete,
        "last_run": ({
            "ok": last_run.get("ok"),
            "status": last_run.get("status"),
            "elapsed_sec": last_run.get("elapsed_sec"),
            "error": last_run.get("error") or "",
            "at": last_run.get("at"),
            "steps": last_run.get("steps") or {},
        } if last_run else None),
    }
@episodes_bp.route('/api/episodes/<novel_id>', methods=['GET'])
def api_list_episodes(novel_id):
    """某小说已生成的剧集清单（含从磁盘产物推导的真实状态与进度）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    # 支持 ?project_id= 指定项目查看（不传时自动按小说归属的项目）
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
    # 逐集补齐状态 / 进度（前端 EpisodeInfo.status、completed_shots 的数据源）。
    # ⚠️ 产物目录（storyboards/videos/final/autopilot）用的是项目 dir_key（= key），
    #    而非剧本 meta.project_name（那是「每集独立项目名」，如 极短小说_雨夜归人_第1集）。
    for row in episodes:
        try:
            row.update(_episode_progress(key, int(row.get("episode_no") or 1),
                                         int(row.get("shot_count") or 0)))
        except Exception as e:  # noqa: BLE001  单集探测失败不拖垮整表
            _app_logger().warning(f"第{row.get('episode_no')}集进度探测失败：{e}")
            row.setdefault("status", "pending")
            row.setdefault("completed_shots", 0)
    # ⭐⭐ 2026-10-10（用户实测：「前端剧本这里还是没有实时显示出来」）：
    #   剧本是**整个步骤跑完才落盘**的（pipeline 写 第N集.json）。生产刚跑到
    #   「第 1 集剧本 18% · 第 2 轮补生成复检中」时磁盘上什么都没有 ——
    #   list_episodes 返回空列表，前端「剧本概览」便一直显示
    #   「暂无剧集数据 / 请先启动自动生产」，用户以为根本没在跑。
    #   （前端其实已有 10 秒静默重拉的轮询，但重拉同样拿到空表，救不了。）
    #   这里读 autopilot 运行态，把**正在生成但尚未落盘**的那一集补成占位行：
    #   status=producing + percent/step/message。落盘后真实行出现，占位按集号去重消失。
    try:
        _cur = (autopilot.status(key) or {}).get("current") or {}
        _cep = int(_cur.get("episode") or 0)
        if _cep and not any(int(r.get("episode_no") or 0) == _cep for r in episodes):
            episodes.append({
                "episode_no": _cep,
                "file": "",
                "path": "",
                "title": "",
                "episode_title": "",
                "chapter_index": None,
                "shot_count": 0,
                "completed_shots": 0,
                "status": "producing",
                "producing": True,
                "progress": int(_cur.get("percent") or 0),
                "current_step": _cur.get("step") or "",
                "message": _cur.get("message") or "",
                "started_at": _cur.get("started_at") or "",
                #: 供前端标注「生成中（剧本尚未落盘）」——避免把空行误当已完成的集
                "pending_script": True,
            })
            episodes.sort(key=lambda r: int(r.get("episode_no") or 0))
    except Exception as e:  # noqa: BLE001 - 占位失败不该让剧本列表 500
        _app_logger().warning("补「正在生成」集占位失败（忽略）：%s", e)
    done = sum(1 for r in episodes if r.get("status") == "done")
    failed = sum(1 for r in episodes if r.get("status") == "failed")
    producing = sum(1 for r in episodes if r.get("status") == "producing")
    return jsonify({
        "success": True, "novel_id": novel_id, "name": key,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "novel_title": meta.get("title") or meta.get("name"),
        "chapter_count": meta.get("chapter_count"),
        "count": len(episodes),
        "total": len(episodes),
        "stats": {"total": len(episodes), "done": done, "failed": failed,
                  "producing": producing,
                  "pending": len(episodes) - done - failed - producing},
        "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
        "episodes": episodes,
    })
@episodes_bp.route('/api/episodes/<novel_id>/<int:episode_no>', methods=['PUT'])
def api_update_episode(novel_id, episode_no):
    """修改单集剧本（shot.description / motion / emotion / camera 等字段）。

    payload: {"shots": [{"shot_id": 2, "description": "...", "motion": "...", ...}, ...]}
    按 shot_id 匹配后合并字段（只更新 payload 里出现的 key，不整镜替换）。
    落盘前备份原始剧本到 .bak；任何失败 fail-closed（不写盘）。
    """
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    script_path = novel_to_script.episode_script_path(SCRIPT_DIR, key, episode_no)
    if not os.path.isfile(script_path):
        return jsonify({"success": False, "error": f"剧本文件不存在：{script_path}"}), 404
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            script = json.load(f)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 500
    data = request.json or {}
    updates = data.get("shots") or []
    if not updates:
        return jsonify({"success": False, "error": "没有要修改的镜头"}), 400
    _ALLOWED = ("description", "visual_detail", "motion", "emotion", "edit_reason",
                "beat", "camera", "camera_motion", "shot_type", "duration", "audio_cues")
    shots = script.get("shots") or []
    by_id = {str(s.get("shot_id")): s for s in shots}
    changed = 0
    for up in updates:
        sid = str((up or {}).get("shot_id", ""))
        target = by_id.get(sid)
        if target is None:
            continue
        for fld in _ALLOWED:
            if fld in up and up[fld] is not None:
                target[fld] = up[fld]
                changed += 1
    if changed == 0:
        return jsonify({"success": False, "error": "没有匹配到任何镜头（shot_id 对不上）"}), 400
    bak = script_path + ".bak"
    if not os.path.isfile(bak):
        import shutil as _shutil
        _shutil.copy2(script_path, bak)
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    _app_logger().info("[剧本编辑] novel=%s ep=%s 更新了 %d 个字段（shot_ids=%s）",
                   novel_id, episode_no, changed,
                   [str((u or {}).get("shot_id")) for u in updates])
    return jsonify({"success": True, "changed": changed, "script_path": script_path})
@episodes_bp.route('/api/episodes/<novel_id>/<int:episode_no>', methods=['GET'])
def api_get_episode(novel_id, episode_no):
    """读取单集剧本（供切集预览 / 载入后续步骤）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    try:
        script = novel_to_script.load_episode_script(SCRIPT_DIR, key, episode_no)
    except novel_to_script.EpisodeNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 500

    meta_i = script.get("metadata") or {}
    dir_info = os.path.abspath(os.path.join(SCRIPT_DIR, key))
    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "episode_no": episode_no,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "episode_title": script.get("episode_title") or meta_i.get("chapter_title"),
        "chapter_index": meta_i.get("chapter_index"),
        "project_name": meta_i.get("project_name") or novel_to_script.episode_project_name(key, episode_no),
        "script_path": novel_to_script.episode_script_path(SCRIPT_DIR, key, episode_no),
        "episode_dir": dir_info,
        "script": script,
        "stats": {
            "characters": len(script.get("characters") or []),
            "items": len(script.get("items") or []),
            "scenes": len(script.get("scenes") or []),
            "shots": len(script.get("shots") or []),
            "shot_count": script.get("shot_count") or len(script.get("shots") or []),
            "episode_duration_sec": script.get("episode_duration_sec"),
            "duration_per_shot_sec": script.get("duration_per_shot_sec"),
        },
        "episode_plan": script.get("episode_plan"),
        "warnings": meta_i.get("warnings") or [],
        "chunks_total": meta_i.get("chunks_total"),
        "chunks_used": meta_i.get("chunks_used"),
        "chapter_char_count": meta_i.get("chapter_char_count"),
        "generated_at": meta_i.get("generated_at"),
        "continuity_meta": meta_i.get("continuity") or None,
        "coverage_meta": meta_i.get("coverage") or None,
        "coverage_report_path": meta_i.get("coverage_report_path"),
        "state_in": script.get("state_in"),
        "state_out": script.get("state_out"),
        "continuity": continuity.episode_continuity_view(CONTINUITY_DIR, key, episode_no),
    })
