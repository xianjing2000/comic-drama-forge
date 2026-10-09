# -*- coding: utf-8 -*-
"""质量审阅 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @quality_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。
"""
import json
import os
import time

from flask import Blueprint, jsonify, request, send_file

import novel_to_script
import pipeline
import project_store
import qc_client
import quality_stage
import re
import shot_key
from config import QC_DIR, SCRIPT_DIR, STORYBOARDS_DIR, VIDEOS_DIR
from routes._shared import _app_logger, _body, _ep_read_dir, _load_script_for, _project_or_400, _quality_asset_url, _quality_find_full, _quality_find_preview, _quality_state_view, _quality_video_url, _safe_project

quality_bp = Blueprint('quality', __name__)

def _quality_contract_summary(project: str, ep) -> dict:
    """合同的稳定摘要（批准时进哈希；镜头数/时长被改 → 批准失效）。"""
    scr = _load_script_for(project, ep) or {}
    shots = [s for s in (scr.get("shots") or []) if isinstance(s, dict)]
    return {"shots_total": int(scr.get("shot_count") or len(shots)),
            "duration_sec": float(scr.get("episode_duration_sec") or 0),
            "shots": [[shot_key.shot_seq(s.get("shot_id"), i + 1), s.get("duration")]
                      for i, s in enumerate(shots)]}
def _quality_ep_numbers(project: str) -> list:
    """项目集号 = 剧本集 ∪ 视频产物集（任一侧有就列出，旧项目缺剧本也能审）。"""
    nums = set()
    try:
        key = project_store.safe_key(project)
        for ep in (novel_to_script.list_episodes(SCRIPT_DIR, key) or []):
            try:
                nums.add(int(ep.get("episode_no")))
            except (TypeError, ValueError):
                pass
    except Exception as e:                                   # noqa: BLE001
        _app_logger().debug("剧本集列表读取失败：%s", e)
    vroot = os.path.join(VIDEOS_DIR, project)
    try:
        entries = os.listdir(vroot) if os.path.isdir(vroot) else []
    except OSError:
        entries = []
    if any(f.lower().endswith((".mp4", ".mov", ".mkv")) for f in entries):
        nums.add(1)
    for d in entries:
        m = re.match(r"^ep(\d+)$", d)
        if m and os.path.isdir(os.path.join(vroot, d)):
            nums.add(int(m.group(1)))
    return sorted(nums)
def _quality_episode_row(project: str, ep) -> dict:
    """单集摘要行（审片左栏）。"""
    scr = _load_script_for(project, ep) or {}
    full = _quality_find_full(project, ep)
    sv = _quality_state_view(project, ep)
    review = {}
    try:
        for d in pipeline.list_deliverables(project):
            try:
                if int(d.get("episode_no") or 0) == int(ep):
                    review = {"status": d.get("review") or "pending",
                              "stale": bool(d.get("approval_stale")),
                              "exists": bool(d.get("exists"))}
                    break
            except (TypeError, ValueError):
                continue
    except Exception as e:                                   # noqa: BLE001
        _app_logger().debug("交付物状态读取失败：%s", e)
    return {"episode_no": int(ep),
            "title": scr.get("episode_title") or scr.get("title") or "",
            "shots_total": int(scr.get("shot_count") or len(scr.get("shots") or [])),
            "duration_sec": float(scr.get("episode_duration_sec") or 0),
            "state": sv["stages"], "release": sv["release"], "stale": sv["stale"],
            "artifact": {"exists": bool(full),
                         "name": os.path.basename(full) if full else "",
                         "url": _quality_video_url(full)},
            "review": review, "preview": _quality_find_preview(project, ep)}
def _quality_refs_for_shot(project: str, shot: dict) -> list:
    """本镜参考资产（角色/物品/场景）→ 可显示的图列表。"""
    refs = []

    def _add(kind, names):
        for n in (names or []):
            if not n:
                continue
            u = _quality_asset_url(kind, project, n)
            if u:
                refs.append({"kind": kind, "name": str(n), "url": u})

    _add("characters", shot.get("characters_in_shot"))
    _add("items", shot.get("items_in_shot"))
    loc = shot.get("location") or shot.get("scene")
    if loc:
        u = _quality_asset_url("scenes", project, loc)
        if u:
            refs.append({"kind": "scenes", "name": str(loc), "url": u})
    return refs
def _quality_storyboard_url(project: str, seq: int) -> str:
    """分镜图 URL（并排审片「合同侧」的预期画面）；无图给空串。"""
    fn = "shot_%02d.png" % seq
    if os.path.isfile(os.path.join(STORYBOARDS_DIR, project, fn)):
        return "/api/storyboards/file/%s/%s" % (project, fn)
    return ""
@quality_bp.route('/api/quality/episodes', methods=['GET'])
def api_quality_episodes():
    """集列表 + 每集四层状态（审片界面左栏）。"""
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)
    rows = [_quality_episode_row(project, e) for e in _quality_ep_numbers(project)]
    return jsonify({"success": True, "project": project, "episodes": rows,
                    "count": len(rows),
                    "summary": {"total": len(rows),
                                "ready": sum(1 for r in rows if r["release"]["ready"]),
                                "awaiting_review": sum(
                                    1 for r in rows
                                    if r["state"]["C"]["status"] == "pending"
                                    and r["artifact"]["exists"])}})
@quality_bp.route('/api/quality/review', methods=['GET'])
def api_quality_review():
    """单集完整审片载荷：契约 + 逐镜（分镜/参考/视频/质检）+ 四层状态。"""
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)
    try:
        ep = int(request.args.get('episode') or 1)
    except (TypeError, ValueError):
        ep = 1

    scr = _load_script_for(project, ep) or {}
    shots = [s for s in (scr.get("shots") or []) if isinstance(s, dict)]
    ep_dir = _ep_read_dir(VIDEOS_DIR, project, ep)

    out_shots = []
    for i, s in enumerate(shots):
        seq = shot_key.shot_seq(s.get("shot_id"), i + 1) or (i + 1)
        vpath = os.path.join(ep_dir, "shot_%02d.mp4" % seq)
        vexists = os.path.isfile(vpath)
        sb_url = _quality_storyboard_url(project, seq)
        qc = {"found": False, "attempts": 0, "last_passed": None, "latest": {}}
        try:
            hist = qc_client.read_history(QC_DIR, project, "video",
                                          s.get("shot_id", seq)) or {}
            recs = hist.get("records") or []
            if recs:
                last = recs[-1] if isinstance(recs[-1], dict) else {}
                qc = {"found": True, "attempts": len(recs),
                      "last_passed": hist.get("last_passed"),
                      "latest": {"attempt": last.get("attempt"),
                                 "ok": last.get("ok"), "passed": last.get("passed"),
                                 "score": last.get("score"),
                                 "reason": last.get("reason") or "",
                                 "issues": last.get("issues") or [],
                                 "critical_issues": last.get("critical_issues") or [],
                                 "style_mismatch": bool(last.get("style_mismatch")),
                                 "duration": last.get("duration"),
                                 "time": last.get("time") or "",
                                 "frames": last.get("frames") or []}}
        except Exception as e:                               # noqa: BLE001
            _app_logger().warning("读逐镜质检历史失败（shot %s）：%s", seq, e)
        out_shots.append({
            "seq": seq, "shot_id": s.get("shot_id", seq),
            "duration": s.get("duration"), "camera": s.get("camera") or "",
            "location": s.get("location") or "",
            "description": s.get("description") or "",
            "dialogue_text": s.get("dialogue_text") or "",
            "first_frame": s.get("first_frame") or "",
            "last_frame": s.get("last_frame") or "",
            "motion": s.get("motion") or "", "emotion": s.get("emotion") or "",
            "beat": s.get("beat") or "",
            "video": {"exists": vexists, "url": _quality_video_url(vpath) if vexists else ""},
            "storyboard": {"exists": bool(sb_url), "url": sb_url},
            "refs": _quality_refs_for_shot(project, s), "qc": qc})

    full = _quality_find_full(project, ep)
    sv = _quality_state_view(project, ep)
    return jsonify({"success": True, "project": project, "episode": ep,
                    "contract": {"title": scr.get("episode_title") or scr.get("title") or "",
                                 "style": scr.get("style") or "",
                                 "shots_total": int(scr.get("shot_count") or len(shots)),
                                 "duration_sec": float(scr.get("episode_duration_sec") or 0)},
                    "shots": out_shots,
                    "artifact": {"exists": bool(full),
                                 "name": os.path.basename(full) if full else "",
                                 "url": _quality_video_url(full)},
                    "state": sv["stages"], "release": sv["release"],
                    "stale": sv["stale"], "preview": _quality_find_preview(project, ep)})
@quality_bp.route('/api/quality/stage', methods=['POST'])
def api_quality_stage():
    """人工层（C 编辑复核 / D 发布批准）状态写入。

    「passed」必须绑定当刻产物 + 合同哈希：重渲或改剧本后批准自动失效。
    A/B 由系统判定，本接口不接受 —— 防止人工结论覆盖机器证据。
    """
    data = _body()
    project, err = _project_or_400(
        (data.get('project') or data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        ep = int(data.get('episode') or data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode 必须是整数"}), 400
    stage = str(data.get('stage') or '').strip().upper()
    status = str(data.get('status') or '').strip().lower()
    note = str(data.get('note') or '')
    if stage not in ('C', 'D'):
        return jsonify({"success": False,
                        "error": "stage 仅允许 C(编辑复核)/D(发布批准)，A/B 由系统判定"}), 400
    if status not in quality_stage.STATUSES:
        return jsonify({"success": False,
                        "error": "status 非法（允许 %s）" % (quality_stage.STATUSES,)}), 400

    full = _quality_find_full(project, ep)
    binding = None
    if status == 'passed':
        if not full:
            return jsonify({"success": False,
                            "error": "该集尚无整集成片，无法复核/批准（请先完成整集生成）"}), 400
        binding = quality_stage.make_binding(
            full, contract=_quality_contract_summary(project, ep))

    state = quality_stage.record_stage(
        project, ep, stage, status, note=note,
        evidence={"by": "review_ui",
                  "artifact": os.path.basename(full) if full else ""},
        binding=binding)
    _app_logger().info("[审片] %s 第%s集 %s → %s%s", project, ep,
                    quality_stage.STAGE_LABELS[stage], status,
                    ("（%s）" % note) if note else "")
    sv = _quality_state_view(project, ep)
    return jsonify({"success": True, "stage": stage, "status": status,
                    "state": sv["stages"], "release": sv["release"],
                    "stale": sv["stale"]})
