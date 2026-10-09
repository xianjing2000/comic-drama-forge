# -*- coding: utf-8 -*-
"""无人值守托管（autopilot）API 蓝图（Blueprint 拆分第七批，2026-10-08）。

23 条 /api/autopilot/* 路由：状态·就绪·曲线·计划（读/改/由设置生成）·启用/停用/暂停/恢复·
进度（全部/单集）·交付物（清单/取文件/审核）·异常（清单/处理）·单集日志·手动跑一次·
重置（单集/单镜头/资产）。依赖闭包不含生成状态机本身 —— 托管线程在 autopilot 模块里，
这里只是它的控制面。

跨域助手（_project_or_400 / _safe_project / _trash_move / _collect_matching / _ai_gate_or_400 /
_shot_seq）按架构约定下沉到 routes/_shared.py，本模块按需 import。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @autopilot_bp.route。
"""
import json
import os
import shutil
import time

from flask import Blueprint, current_app, jsonify, request, send_file

import autopilot
import project_store
from config import PROJECT_OUTPUT_DIR
from routes._shared import _ai_gate_or_400, _project_or_400, _safe_project, _shot_seq, _trash_move, _app_logger

autopilot_bp = Blueprint('autopilot', __name__)

import ai_chat
import pipeline
import threading
from config import AI_SETTINGS_PATH, CHARACTERS_DIR, DUB_DIR, DUB_MIX_DIR, FINAL_DIR, ITEMS_DIR, KEYFRAMES_DIR, PROJECT_TRASH_DIR, QC_DIR, SCENES_DIR, SCRIPT_DIR, STORYBOARDS_DIR, VIDEOS_DIR
from flask import abort
from fs_atomic import atomic_write_json
from routes._shared import NOVELS_DIR, _autopilot_guard, _body, _move_with_retry, list_novels, novel_to_script
def _collect_matching(base_dir, prefixes):
    """收集 base_dir 下（递归）名字以任一前缀开头的文件/目录路径（浅层优先）。

    浅层优先的原因：目录整移会连带其内容，深层残余路径在 move 时已不存在 → 静默跳过。
    """
    hits = []
    if base_dir and os.path.isdir(base_dir):
        for _root, _dirs, _files in os.walk(base_dir):
            for _name in _dirs + _files:
                if any(_name.startswith(pfx) for pfx in prefixes):
                    hits.append(os.path.join(_root, _name))
    hits.sort(key=lambda p: p.count(os.sep))
    return hits
@autopilot_bp.route('/api/autopilot/reset-episode', methods=['POST'])
def api_autopilot_reset_episode():
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    include_script = bool(data.get('include_script'))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "reset",
                              f"{stamp}_{project_name}")
    cleared, skipped, notes = [], [], []

    def _trash_move(src, category):
        """把单个文件/目录移入回收站；不存在=无事发生，被占用=记入 skipped"""
        if not src or not os.path.exists(src):
            return
        try:
            dst = os.path.join(trash_root, category,
                               os.path.basename(src.rstrip('\\/')) or category)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _move_with_retry(src, dst)
            cleared.append({"category": category, "path": src})
        except Exception as e:  # noqa: BLE001  单项失败不阻断其余清理
            skipped.append({"path": src, "error": str(e)})

    # ---- 目录级产物：分镜图 / 尾帧 / 视频 / 成片 ----
    # 第 1 集平铺（只移文件，其它集的 epNN/ 子目录原地保留）；第 2 集起整块移 epNN/ 子目录
    for category, base_dir in (("storyboards", STORYBOARDS_DIR), ("keyframes", KEYFRAMES_DIR),
                               ("videos", VIDEOS_DIR), ("final", FINAL_DIR)):
        proj_dir = os.path.join(base_dir, project_name)
        if not os.path.isdir(proj_dir):
            continue
        if episode_no > 1:
            _trash_move(os.path.join(proj_dir, f"ep{episode_no:02d}"), category)
        else:
            for _name in os.listdir(proj_dir):
                _p = os.path.join(proj_dir, _name)
                if os.path.isfile(_p):
                    _trash_move(_p, category)

    # ---- 文件级产物：配音 / 混音 / 质检（文件名带 epNN_ 前缀的归该集）----
    for category, base_dir in (("dub", DUB_DIR), ("dub_mix", DUB_MIX_DIR), ("qc", QC_DIR)):
        proj_dir = os.path.join(base_dir, project_name)
        if not os.path.isdir(proj_dir):
            continue
        _prefix = f"ep{episode_no:02d}_"
        _hit = False
        for _root, _dirs, _files in os.walk(proj_dir):
            for _name in list(_files):
                if _name.startswith(_prefix):
                    _hit = True
                    _trash_move(os.path.join(_root, _name), category)
        if not _hit and episode_no == 1 and category == "qc":
            # 兜底：qc 里不带 ep 前缀的记录（如 storyboard_scratch、prompt_*.json）属第 1 集
            # 平铺口径 —— 整目录移入回收站（qc 是派生数据，可恢复）。
            _trash_move(proj_dir, category)

    # ---- 剧本（可选）：include_script=true 时连剧本一并移入回收站 ----
    if include_script:
        try:
            _script_path = novel_to_script.episode_script_path(SCRIPT_DIR, project_name, episode_no)
            _trash_move(_script_path, "scripts")
        except Exception as e:  # noqa: BLE001
            notes.append(f"剧本定位失败（忽略）：{e}")

    # ---- 交付记录：把该集条目从 deliverables.json 摘除（被摘条目进回收站，可恢复）----
    _dl_file = os.path.join(PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    if os.path.isfile(_dl_file):
        try:
            with open(_dl_file, "r", encoding="utf-8") as f:
                _dl = json.load(f)
            _items = _dl if isinstance(_dl, list) else (_dl.get("items") if isinstance(_dl, dict) else None) or []
            _removed = [_it for _it in _items
                        if isinstance(_it, dict) and int(_it.get("episode_no") or 0) == episode_no]
            if _removed:
                _keep = [_it for _it in _items if _it not in _removed]
                os.makedirs(os.path.join(trash_root, "autopilot"), exist_ok=True)
                with open(os.path.join(trash_root, "autopilot", "deliverables_removed.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(_removed, f, ensure_ascii=False, indent=2)
                if isinstance(_dl, list):
                    atomic_write_json(_dl_file, _keep)
                else:
                    _dl["items"] = _keep
                    atomic_write_json(_dl_file, _dl)
                notes.append(f"交付记录已摘除 {len(_removed)} 条（被摘条目在回收站，可恢复）")
        except Exception as e:  # noqa: BLE001
            notes.append(f"交付记录清理失败（忽略）：{e}")

    if not cleared and not skipped:
        notes.append("该集没有可清理的产物（本来就是空的）")
    _app_logger().info("[reset-episode] 项目=%s 集=%s 清理 %d 项、跳过 %d 项，回收站=%s",
                    project_name, episode_no, len(cleared), len(skipped), trash_root)
    return jsonify({
        "success": True, "project": project_name, "episode_no": episode_no,
        "include_script": include_script, "cleared": cleared, "skipped": skipped,
        "notes": notes, "trash": trash_root,
        "hint": "产物已移入回收站（可恢复）；接着用 produce_episode 从头重产该集",
    })
@autopilot_bp.route('/api/autopilot/reset-shot', methods=['POST'])
def api_autopilot_reset_shot():
    """清空重做单个镜头：分镜图 / 尾帧 / 视频 / 配音 / 质检记录 → 回收站（可恢复）。

    清完之后用 produce_episode 重产该集，断点续跑只会重做缺失的这一镜（其余镜不重烧）。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    shot_id = (data.get('shot_id') or '').strip()
    seq = _shot_seq(shot_id, 0)
    if seq <= 0:
        return jsonify({"success": False,
                        "error": f"无法解析镜号：{shot_id}（形如 shot_3，可从 get_shots 拿到）"})
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "reset",
                              f"{stamp}_{project_name}")
    ep_tag = "" if episode_no <= 1 else f"ep{episode_no:02d}"
    ep_prefix = f"ep{episode_no:02d}_shot{seq:02d}"
    shot_prefix = f"shot_{seq:02d}"
    cleared, skipped = [], []
    # （目录, 名字前缀列表, 类别）——前缀按各目录的实际命名口径
    for base_dir, prefixes, category in (
            (os.path.join(STORYBOARDS_DIR, project_name, ep_tag), [shot_prefix], "storyboards"),
            (os.path.join(KEYFRAMES_DIR, project_name, ep_tag), [shot_prefix], "keyframes"),
            (os.path.join(VIDEOS_DIR, project_name, ep_tag), [shot_prefix], "videos"),
            (os.path.join(DUB_DIR, project_name), [ep_prefix], "dub"),
            (os.path.join(DUB_MIX_DIR, project_name), [ep_prefix], "dub_mix"),
            (os.path.join(QC_DIR, project_name), [ep_prefix, shot_prefix,
                                                  f"prompt_shot_{seq:02d}"], "qc"),
    ):
        for _p in _collect_matching(base_dir, prefixes):
            _trash_move(_p, category, trash_root, cleared, skipped)
    if not cleared and not skipped:
        return jsonify({"success": True, "project": project_name, "episode_no": episode_no,
                        "shot_id": shot_id, "cleared": [], "skipped": [],
                        "notes": ["该镜没有已生成的产物（本来就是空的）"], "trash": trash_root})
    _app_logger().info("[reset-shot] 项目=%s 集=%s 镜=%s(seq %02d) 清理 %d 项、跳过 %d 项",
                    project_name, episode_no, shot_id, seq, len(cleared), len(skipped))
    return jsonify({"success": True, "project": project_name, "episode_no": episode_no,
                    "shot_id": shot_id, "cleared": cleared, "skipped": skipped,
                    "trash": trash_root,
                    "hint": "该镜产物已移入回收站；用 produce_episode 重产该集时只会重做这一镜"})
@autopilot_bp.route('/api/autopilot/reset-asset', methods=['POST'])
def api_autopilot_reset_asset():
    """清空重做单个资产（角色/物品/场景）：基础图 + 多视图 + 质检记录 → 回收站（可恢复）。

    资产卡描述保留（只清图）；下一轮生产的资产步骤会因「base 图缺失」自动补做该资产。
    ⚠️ 若命中跨项目资产库的形象指纹，重做可能直接复用既有图；要换形象请先改描述再重做。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    kind = (data.get('kind') or '').strip().lower()
    name = (data.get('name') or '').strip()
    _kind_dirs = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}
    base_dir = _kind_dirs.get(kind)
    if not base_dir:
        return jsonify({"success": False,
                        "error": "kind 必须是 character / item / scene 之一"})
    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        return jsonify({"success": False, "error": "name 不能为空且不得含路径分隔符"})
    asset_dir = os.path.join(base_dir, project_name, name)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "reset",
                              f"{stamp}_{project_name}")
    cleared, skipped, notes = [], [], []
    if not os.path.isdir(asset_dir):
        notes.append("该资产没有已生成的图（本来就是空的），无需清理")
    else:
        _trash_move(asset_dir, f"assets/{kind}", trash_root, cleared, skipped)
    _app_logger().info("[reset-asset] 项目=%s kind=%s 资产=%s 清理 %d 项、跳过 %d 项",
                    project_name, kind, name, len(cleared), len(skipped))
    return jsonify({"success": True, "project": project_name, "kind": kind, "name": name,
                    "cleared": cleared, "skipped": skipped, "notes": notes,
                    "trash": trash_root,
                    "hint": "资产图已移入回收站（角色卡描述保留）；下一轮生产会自动补做该资产。"
                            "若命中跨项目资产库指纹可能直接复用旧图，要换形象请先改描述"})
@autopilot_bp.route('/api/autopilot/status', methods=['GET'])
@_autopilot_guard
def api_autopilot_status():
    """托管总览：开关状态、当前在做什么、待验收数、异常数、24h 生产曲线

    brief=1：只回状态判断必要的短字段（AI 总控用；见 autopilot.status 的 brief 说明）。
    """
    project = request.args.get("project", "").strip()
    brief = str(request.args.get("brief") or "").strip().lower() in ("1", "true", "yes", "on")
    return jsonify({"success": True, **autopilot.status(project, brief=brief)})
@autopilot_bp.route('/api/autopilot/ready', methods=['GET'])
@_autopilot_guard
def api_autopilot_ready():
    """托管可行性自检：告诉用户还差什么才能真正无人值守"""
    return jsonify({"success": True, **autopilot.ready()})
@autopilot_bp.route('/api/autopilot/curve', methods=['GET'])
@_autopilot_guard
def api_autopilot_curve():
    """24 小时生产曲线（每小时完成/失败集数、平均耗时、吞吐）"""
    try:
        hours = max(1, min(int(request.args.get('hours') or 24), 168))
    except (TypeError, ValueError):
        hours = 24
    return jsonify({"success": True, **autopilot.production_curve(hours)})
@autopilot_bp.route('/api/autopilot/plans', methods=['GET'])
@_autopilot_guard
def api_autopilot_plans():
    """全部项目的托管计划"""
    plans = autopilot.list_plans()
    return jsonify({"success": True, "count": len(plans), "items": plans,
                    "defaults": autopilot.PLAN_DEFAULTS})
@autopilot_bp.route('/api/autopilot/plan/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autopilot_plan_get(project_name):
    project = _safe_project(project_name)
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.get_plan(project),
                    "defaults": autopilot.PLAN_DEFAULTS})
@autopilot_bp.route('/api/autopilot/plan/<project_name>', methods=['POST'])
@_autopilot_guard
def api_autopilot_plan_set(project_name):
    """设置某项目的自动生产计划（目标集数、镜头数、风格、质量阈值、重试上限…）"""
    project = _safe_project(project_name)
    data = request.json or {}
    novel_id = str(data.pop('novel_id', '') or '').strip()
    patch = {k: v for k, v in (data or {}).items() if k in autopilot.PLAN_DEFAULTS}
    if not patch and not novel_id:
        return jsonify({"success": False,
                        "error": f"没有可更新字段；可用字段：{sorted(autopilot.PLAN_DEFAULTS)}"}), 400
    plan = autopilot.set_plan(project, patch, novel_id=novel_id)
    return jsonify({"success": True, "project": project, "plan": plan})
@autopilot_bp.route('/api/autopilot/enable', methods=['POST'])
@_autopilot_guard
def api_autopilot_enable():
    """开启托管（可同时带生产配置）。novel_id 缺省时从项目注册表自动关联。"""
    # P0-5 门禁：托管是无人值守路径，配置不全时开闸只会白跑一整晚 —— 先在门口拦下
    _gate = _ai_gate_or_400("episode")
    if _gate is not None:
        return _gate
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project') or data.get('project_name') or '',
                                    field_name="project")
    if err is not None:
        return err
    novel_id = str(data.get('novel_id') or '').strip()
    if not novel_id:
        try:
            rec = project_store.get_project(project) or {}
            novel_id = (rec.get('novel_id') or '').strip()
        except Exception:  # noqa: BLE001
            novel_id = ''
    if not novel_id:
        # 退回「按项目名匹配同名小说」，尽量让用户少填一步
        try:
            for n in list_novels(NOVELS_DIR):
                if (n.get('title') or '').strip() == project:
                    novel_id = n.get('novel_id') or ''
                    break
        except Exception:  # noqa: BLE001
            novel_id = ''
    patch = {k: v for k, v in (data or {}).items()
             if k in autopilot.PLAN_DEFAULTS and k != 'enabled'}
    plan = autopilot.enable(project, patch)
    if novel_id:
        plan = autopilot.set_plan(project, {}, novel_id=novel_id)
    # 顺带校验输入是否齐备（小说正文 / 项目绑定），避免"开了但没活干"
    warn = ""
    if not plan.get('novel_id'):
        warn = "该项目未关联小说，托管不会生产；请先在「小说库」上传并建项目"
    return jsonify({"success": True, "project": project, "plan": plan,
                    "novel_id": plan.get('novel_id') or "", "warning": warn})
@autopilot_bp.route('/api/autopilot/disable', methods=['POST'])
@_autopilot_guard
def api_autopilot_disable():
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project') or data.get('project_name') or '',
                                   field_name="project")
    if err is not None:
        return err
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.disable(project)})
@autopilot_bp.route('/api/autopilot/pause', methods=['POST'])
@_autopilot_guard
def api_autopilot_pause():
    """暂停托管（在当前步骤边界生效，不产生半成品）

    ⚠️ 这是**全局**开关：``autopilot.pause()`` 置的是全局 ``_STATE["paused"]``，
    **不区分项目**。此前接口既不读 ``project`` 也不提示，调用方（尤其是总控 AI）
    很容易以为「只暂停了某个项目」，实际把所有项目都停了 —— 静默越权。
    这里保留 reason 语义，并在收到 project 时**显式告知**它被忽略、给出替代做法。
    """
    data = request.json or {}
    result = autopilot.pause(str(data.get('reason') or '手动暂停'))
    ignored_project = str(data.get('project') or data.get('project_name') or '').strip()
    payload = {"success": True, **result}
    if ignored_project:
        payload["warning"] = (f"暂停托管是**全局**开关，已忽略 project='{ignored_project}'"
                              "（所有项目都会暂停）。若只想停某个项目，"
                              "请调用 /api/autopilot/disable 并带 project。")
        payload["scope"] = "global"
    return jsonify(payload)
@autopilot_bp.route('/api/autopilot/resume', methods=['POST'])
@_autopilot_guard
def api_autopilot_resume():
    """恢复托管（服务重启后也可用它手动拉起守护进程）"""
    return jsonify({"success": True, **autopilot.resume()})
@autopilot_bp.route('/api/autopilot/progress', methods=['GET'])
@_autopilot_guard
def api_autopilot_progress_all():
    """全部项目的分集进度（剧本/资产/分镜/视频/成片 逐集状态）"""
    items = autopilot.all_progress()
    return jsonify({"success": True, "count": len(items), "items": items})
@autopilot_bp.route('/api/autopilot/progress/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autopilot_progress_one(project_name):
    project = _safe_project(project_name)
    return jsonify({"success": True, **autopilot.project_progress(project)})
@autopilot_bp.route('/api/autopilot/deliverables', methods=['GET'])
@_autopilot_guard
def api_autopilot_deliverables():
    """成品验收队列——用户唯一需要重点看的清单（只含最终成片）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else ''
    items = pipeline.list_deliverables(project)
    return jsonify({"success": True, "count": len(items),
                    "pending": sum(1 for x in items if x.get('review') == 'pending'),
                    "items": items})
@autopilot_bp.route('/api/autopilot/deliverables/review', methods=['POST'])
@_autopilot_guard
def api_autopilot_review():
    """验收 / 打回成片（打回 = 该集在下次轮转时自动重做）"""
    data = request.json or {}
    # 口径统一（F-01 收口）：与 run-once 一致，改走 _project_or_400 拒绝越界/空/控制字符。
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    review = str(data.get('review') or '').strip().lower()
    if review not in ('accepted', 'rejected', 'pending'):
        return jsonify({"success": False, "error": "review 仅支持 accepted / rejected / pending"}), 400
    try:
        ep = int(data.get('episode_no') or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    item = pipeline.set_deliverable_review(project, ep, review, str(data.get('note') or ''))
    if not item:
        return jsonify({"success": False, "error": f"未找到 {project} 第{ep}集的成片记录"}), 404
    return jsonify({"success": True, "item": item})
@autopilot_bp.route('/api/autopilot/deliverable/file/<project_name>/<path:filename>')
@_autopilot_guard
def api_autopilot_deliverable_file(project_name, filename):
    """播放 / 下载成片（以交付物索引为准解析真实路径，防目录穿越）"""
    project = _safe_project(project_name)
    filepath = pipeline.deliverable_path(project, filename)
    if not filepath or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')
@autopilot_bp.route('/api/autopilot/exceptions', methods=['GET'])
@_autopilot_guard
def api_autopilot_exceptions():
    """需人工介入清单（超过重试上限 / 环境性缺失导致挂起）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else ''
    projects = [project] if project else [p['project'] for p in autopilot.list_plans()]
    items = []
    for pj in projects:
        for d in pipeline.list_dead_letters(pj):
            if not d.get('resolved'):
                # P0-4：list_dead_letters 返回的是磁盘 dead_letter.json 里的原始项
                # （字段只有 episode_no/reason/detail/...，不含 project）——不传
                # ?project= 时每条无法归属项目、排序键 x.get('project') 形同虚设。
                # 用**拷贝**补 project（不就地改磁盘读出的对象，避免污染其他调用方），
                # 让 items.sort 的 project 排序键从此真正生效。
                items.append({**d, "project": pj})
    items.sort(key=lambda x: (x.get('project') or '', int(x.get('episode_no') or 0)))
    return jsonify({"success": True, "count": len(items), "items": items})
@autopilot_bp.route('/api/autopilot/exceptions/resolve', methods=['POST'])
@_autopilot_guard
def api_autopilot_exception_resolve():
    """处理异常：标记已解决 / 忽略（之后托管会重新尝试该集）"""
    data = request.json or {}
    project = _safe_project(data.get('project') or data.get('project_name') or '')
    try:
        ep = int(data.get('episode_no') or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    item = pipeline.resolve_dead_letter(project, ep, str(data.get('note') or ''))
    if not item:
        return jsonify({"success": False, "error": f"未找到 {project} 第{ep}集的异常记录"}), 404
    autopilot.wake()
    return jsonify({"success": True, "item": item})
@autopilot_bp.route('/api/autopilot/episode_log', methods=['GET'])
@_autopilot_guard
def api_autopilot_episode_log():
    """集级操作日志（总控 AI 与人工排查共用）：?project=&episode_no=&tail=200

    返回该集 ep{NN}_log.jsonl 的尾部 N 行（每行一个事件：episode_run_start /
    progress / step_start / step_skip / step_done / step_fail / episode_done /
    episode_failed，含时间戳与耗时）。文件缺失不算错误：返回 exists:false +
    note 提示，便于总控区分「没有日志」与「日志为空」。
    """
    project, err = _project_or_400(request.args.get('project') or '', field_name="project")
    if err is not None:
        return err
    try:
        episode_no = int(request.args.get('episode_no') or 0)
    except (TypeError, ValueError):
        episode_no = 0
    try:
        tail = max(1, min(int(request.args.get('tail') or 200), 1000))
    except (TypeError, ValueError):
        tail = 200
    events, exists = [], False
    if episode_no > 0:
        try:
            log_path = pipeline._episode_log_path(project, episode_no)
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"日志路径解析失败：{e}"}), 500
        if os.path.isfile(log_path):
            exists = True
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-tail:]
                for ln in lines:
                    try:
                        events.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
            except OSError as e:
                return jsonify({"success": False, "error": f"读取操作日志失败：{e}"}), 500
    resp = {"success": True, "project": project, "episode_no": episode_no,
            "exists": exists, "count": len(events), "events": events}
    if not exists:
        resp["note"] = "该集还没有操作日志"
    return jsonify(resp)
@autopilot_bp.route('/api/autopilot/run-once', methods=['POST'])
@_autopilot_guard
def api_autopilot_run_once():
    """立即生产指定一集（**异步**：校验通过即返回 task_id，后台执行）

    审计 P2-6（2026-09-29）：旧实现同步跑完整集流水线（可数小时），占用 waitress
    工作线程（默认 8）—— 几次并发就把线程池占满，连 /api/status 轮询一并饿死
    （表现为「全站卡死」）。现在校验通过即返回 task_id，生产在后台线程执行：
    进度经 _cb 写进 autopilot.current，用 /api/autopilot/status 轮询；
    完成态看交付清单（/api/autopilot/deliverables）。
    """
    # P0-5 门禁：整集生产依赖文本分析模型，未配置直接阻断（不再「跑一半才 401」）
    _gate = _ai_gate_or_400("episode")
    if _gate is not None:
        return _gate
    data = request.json or {}
    # 口径统一（F-01 收口）：run-once / review 此前用 _safe_project，会把越界/空/控制字符
    # 项目名静默收敛成合法键，与全仓 46 处 _project_or_400 不一致；现改走同一入口。
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    try:
        ep = int(data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    plan = autopilot.get_plan(project)
    meta = autopilot._novel_meta(project, plan)
    if not meta:
        return jsonify({"success": False,
                        "error": "该项目未关联小说，无法生产（请先在小说库上传建项目）"}), 400
    chapters, novel_text = autopilot.chapters_and_text(meta)
    # 集号 ≠ 章号（超长章会拆成多集）→ 必须走单元表反查，不能按章号找
    unit = autopilot.find_episode_unit(chapters, plan, ep, novel_text)
    chapter = (unit or {}).get("chapter")
    if not chapter:
        _n_units = len(autopilot.episode_units(chapters, plan, novel_text))
        return jsonify({"success": False,
                        "error": f"该小说没有第{ep}集（共 {len(chapters)} 章 / {_n_units} 集）"}), 400
    cfg = pipeline.normalize_config({**plan, 'novel_id': meta.get('novel_id')},
                                    default_project_key=project)
    # 文学剧本改写接入（run-once）：文件存在即使用，无需人工审阅
    import novel_screenplay
    _md = novel_screenplay.load_screenplay(project, ep)
    if _md:
        _app_logger().info("run-once 集 %s 使用文学剧本改写（文件存在）", ep)
        cfg.setdefault("screenplay_text", _md)
    # 2026-09-30：已被判定「需人工介入」的集，在**入口**就挡住并把原因回给调用方。
    # 旧行为是放它进后台、由 run_episode 立刻返回 needs_human，总控只拿到 200 started，
    # 用户这边什么都看不到（第1集 401 那次就是这样静默掉的）。现在总控能当场拿到 409+原因。
    _dead = pipeline._is_dead_letter(cfg, project, ep)
    if _dead:
        return jsonify({"success": False, "episode_no": ep,
                        "error": f"第{ep}集此前已判定需人工介入：{_dead.get('reason') or '未知原因'}",
                        "hint": "请先在「异常 / 需人工介入」里处理或标记忽略，然后重跑本集"}), 409
    # P1-5：run-once 的 progress_cb 把进度实时写进 autopilot 的 current 状态，
    # `/api/autopilot/status` 轮询即可拿到实时进度（当前在哪一步 / 百分之几 / 卡在重试）。
    _seen: list = []

    def _cb(message, percent, phase=None):
        try:
            import pipeline as _pl
            base = str(phase or "").split(":")[0]
            if base in _pl.STEP_SEQUENCE and base not in _seen:
                idx = _pl.STEP_SEQUENCE.index(base)
                _seen.extend(_pl.STEP_SEQUENCE[:idx])
                _seen.append(base)
            autopilot._set_current(project=project, episode=ep,
                                   title=chapter.get('title') or f"第{ep}章",
                                   step=base or "running", message=message,
                                   percent=int(percent or 0),
                                   steps_done=list(dict.fromkeys(_seen)),
                                   phase=phase or "start", started_at=autopilot._now())
        except Exception as e:  # noqa: BLE001  进度上报失败不得阻断生产
            _app_logger().warning("run-once 进度上报失败：%s", e)

    # 审计 P2-6：先做一次非阻塞忙检（保留旧「busy → 409」语义），再转后台执行
    if pipeline.is_episode_running(project, ep):
        # B-02 P0-5：该集正被另一执行体（托管轮转）生产，集级锁拒绝双跑
        return jsonify({"success": False,
                        "error": "该集正在生产中",
                        "retry_after_sec": 30}), 409

    # 总控「停止 → 再生产」流程修复（2026-09-30 实测）：stop_production 置的全局
    # 暂停**持久化到磁盘**（重启也恢复），而本集视频 worker 的中止判定器
    # （_video_should_stop）对托管任务「is_paused → 停」——于是总控停止后再
    # 「生产一集」，每次 ComfyUI 提交都被秒级掐断，3 次重试瞬间烧完、整集失败。
    # 显式 run-once = 用户要求「现在就生产」，先解除暂停再开跑。
    try:
        autopilot.resume()
    except Exception as e:  # noqa: BLE001  解除暂停失败不得阻断本次生产
        _app_logger().warning("run-once 前解除托管暂停失败（忽略）：%s", e)

    def _run_once_worker():
        result = {}
        try:
            result = pipeline.run_episode(cfg, project, ep, meta, chapter, progress_cb=_cb)
            if result.get('status') == 'busy':
                # 罕见竞态：预检后另一执行体抢跑 —— 集级锁拒绝双跑，留痕即可
                _app_logger().warning("run-once %s 第%s集被集级锁拒绝（另一执行体正在生产）",
                                   project, ep)
                return
            if result.get('ok') and result.get('deliverable'):
                pipeline.record_deliverable(project, ep, result['deliverable'], meta={
                    'title': chapter.get('title') or '', 'chapter_index': chapter.get('index'),
                    'elapsed_sec': result.get('elapsed_sec')})
        except Exception as e:  # noqa: BLE001  后台任务异常不得带崩进程/线程静默死亡
            _app_logger().error("run-once 后台生产失败（%s 第%s集）：%s",
                             project, ep, e, exc_info=True)
            result = {"ok": False, "status": "failed", "episode_no": ep,
                      "project": project, "error": f"{type(e).__name__}: {e}",
                      "elapsed_sec": 0, "deliverable": "", "steps": {}}
        finally:
            # 2026-09-30 修复「失败了总控不知道」：run-once 此前只 logger.error 再清 current，
            # 既不落死信也不写 last_run ⇒ /api/autopilot/status 回报 current=null /
            # exceptions=0 / last_error=""，总控 get_status 看到的是「空闲且零异常」，
            # 于是第1集的 401 失败永远汇报不出来。现在成功与失败都登记 last_run
            # （带项目归属、落盘）；硬失败同时落死信，让「异常」列表和总控都看得见。
            try:
                _st = str(result.get('status') or '')
                _ok = bool(result.get('ok'))
                if _st != 'busy':
                    autopilot.record_run_result(
                        project, ep, _ok, status=_st or ('done' if _ok else 'failed'),
                        error=str(result.get('error') or ''),
                        deliverable=str(result.get('deliverable') or ''),
                        elapsed_sec=result.get('elapsed_sec') or 0,
                        title=chapter.get('title') or '', source='run-once')
                    if not _ok and _st != 'cancelled':
                        # 取消＝用户/托管主动停，不算「需人工介入」，不落死信
                        autopilot._mark_dead(project, ep,
                                             str(result.get('error') or '未知错误'),
                                             {"source": "run-once",
                                              "status": _st or "failed"})
            except Exception as e:  # noqa: BLE001  登记失败不得影响 current 清理
                _app_logger().warning("run-once 结果登记失败：%s", e)
            try:
                autopilot._clear_current()
            except Exception as e:  # noqa: BLE001
                _app_logger().warning("run-once 清理 current 失败：%s", e)

    threading.Thread(target=_run_once_worker, daemon=True,
                     name=f"runonce_{project}_{ep}").start()
    return jsonify({"success": True, "task_id": f"runonce_{project}_{ep}",
                    "project": project, "episode_no": ep, "status": "started",
                    "message": "已开始生产；进度见 /api/autopilot/status，完成态见交付清单"})
@autopilot_bp.route('/api/autopilot/plan-from-settings/<project_name>', methods=['POST'])
@_autopilot_guard
def api_autopilot_plan_from_settings(project_name):
    """总控 AI 一键设定：把对话敲定的创作设定（风格/画风/镜头数/节奏…）落成托管计划

    这是「对话 → 自动生产」的接缝：用户在总控 AI 里谈好风格后点一下，
    风格纲要写进流水线配置，之后每集剧本生成都会带上它，无需再手工填表。
    """
    project = _safe_project(project_name)
    view = ai_chat.settings_view(AI_SETTINGS_PATH, project)
    brief = (view.get('style_brief') or '').strip()
    s = view.get('settings') or {}
    patch = {}
    if brief:
        patch['style'] = brief
    # 单集镜头数：从设定里解析数字（如 "12 个" → 12）
    shots = str(s.get('shots_per_episode') or '')
    digits = ''.join(ch for ch in shots if ch.isdigit())
    if digits:
        try:
            patch['target_shots'] = max(4, min(int(digits), 40))
        except ValueError as e:
            _app_logger().debug("target_shots 字段解析失败（忽略）：%s", e)
    if not patch:
        return jsonify({"success": False,
                        "error": "总控 AI 还没有生效设定；请先在「总控 AI 对话」里谈好风格再一键设定"}), 400
    plan = autopilot.set_plan(project, patch)
    return jsonify({"success": True, "project": project, "plan": plan,
                    "applied": patch, "settings_filled": view.get('filled'),
                    "settings_missing": view.get('missing'),
                    "style_brief": brief})

# =========================================================================== #
# 总控 AI · 实时执行流（2026-10-09）
# --------------------------------------------------------------------------- #
# 用户诉求：「无法从总控 AI 看到具体在执行什么 —— 想要能清晰看到正在执行什么、
# 在思考什么」。原生态只有三类零散数据：
#   ① /api/autopilot/episode_log   —— 集级事件流（step_start/step_done/step_fail…）
#   ② generation_state[task].live  —— 当前资产/镜头/阶段/尝试次数/提示词
#   ③ /api/autopilot/progress      —— 逐集五阶段进度
# 本端点把三者归一成一条按时间排序的执行流，供前端直接渲染：
#   now      —— 「此刻在做什么」（步骤/阶段/第几个/共几个/已耗时）
#   stream   —— 「做过什么 + 为什么这么做」（动作 / 思考 / 结果 / 告警）
#   counters —— 本轮计数（完成 / 失败 / 跳过）
# 「思考」类事件全部来自真实数据：提示词预检结论与自愈项、质检判据与缺陷、
# 重试原因（换种子 / 教训库召回 / 即时改写）。绝不编造推理文字。
# =========================================================================== #

#: 步骤名 → 中文标签（与 pipeline.STEP_SEQUENCE 同序；未知步骤原样回显）
_STEP_LABELS = {
    "script": "剧本生成",
    "tts_pre": "参考音色",
    "assets": "资产图生成",
    "storyboard": "分镜图生成",
    "video": "视频生成",
    "upscale": "超分",
    "final": "成片合成",
}

#: 集级日志事件 → 展示元信息（kind 决定前端配色：action/thinking/result/warn）
_EVT_META = {
    "episode_run_start": ("action", "开始本集生产"),
    "progress": ("action", "进度"),
    "step_start": ("action", "开始步骤"),
    "step_skip": ("warn", "跳过步骤"),
    "step_done": ("result", "步骤完成"),
    "step_fail": ("warn", "步骤失败"),
    "episode_done": ("result", "本集完成"),
    "episode_failed": ("warn", "本集失败"),
}


def _hhmmss(ts) -> str:
    """把 epoch 秒 / ISO 串统一成 HH:MM:SS（无法解析时原样返回）。"""
    try:
        _f = float(ts)
        return time.strftime("%H:%M:%S", time.localtime(_f))
    except (TypeError, ValueError, OSError):
        _s = str(ts or "")
        return _s[11:19] if len(_s) >= 19 and _s[10] in "T " else _s


def _live_snapshot(project: str) -> dict:
    """挑出与 project 相关、且正在跑的生成任务快照（取有 live 的那条）。

    generation_state 是 app.py 的模块级全局；用 sys.modules 取而不 import app，
    避免与「app 导入 routes → routes 再 import app」形成循环导入。
    取不到（单测 / 未启动）时返回空 dict —— 端点仍可只靠日志工作，不报错。
    """
    import sys as _sys
    _app = _sys.modules.get("app")
    _state = getattr(_app, "generation_state", None) if _app else None
    if not isinstance(_state, dict):
        return {}
    _best = {}
    for _tid, _st in list(_state.items()):
        if not isinstance(_st, dict):
            continue
        if str(_st.get("project") or "").strip() not in ("", project):
            continue
        if str(_st.get("status") or "") not in ("running", "queued", ""):
            continue
        _live = _st.get("live") if isinstance(_st.get("live"), dict) else {}
        _score = (2 if _live else 1)
        if _score >= _best.get("_score", 0):
            _best = {"_score": _score, "task_id": _tid, "state": _st, "live": _live}
    if not _best:
        return {}
    _st, _live = _best["state"], _best["live"]
    return {
        "task_id": _best["task_id"],
        "label": str(_st.get("label") or _st.get("kind") or ""),
        "status": str(_st.get("status") or ""),
        "phase": str(_live.get("phase") or _st.get("phase") or ""),
        "current": _live.get("current", _st.get("current")),
        "total": _live.get("total", _st.get("total")),
        "progress": _st.get("progress"),
        "asset": str(_st.get("current_asset") or _live.get("asset") or ""),
        "shot": _live.get("shot"),
        "attempt": _live.get("attempt"),
        "prompt": str(_live.get("prompt") or "")[:1200],
        "done": bool(_live.get("done")),
    }


@autopilot_bp.route('/api/autopilot/live_feed', methods=['GET'])
@_autopilot_guard
def api_autopilot_live_feed():
    """总控 AI 实时执行流：?project=&episode_no=&tail=120

    返回 {now, steps, stream, counters, sources}。stream 按时间倒序（最新在前），
    每条含 {t, kind, title, detail, meta}；kind ∈ action|thinking|result|warn。
    """
    project, err = _project_or_400(request.args.get('project') or '', field_name="project")
    if err is not None:
        return err
    try:
        episode_no = int(request.args.get('episode_no') or 0)
    except (TypeError, ValueError):
        episode_no = 0
    try:
        tail = max(1, min(int(request.args.get('tail') or 120), 500))
    except (TypeError, ValueError):
        tail = 120

    # ---------- ① 集级事件流 → stream ----------
    stream, counters = [], {"done": 0, "failed": 0, "skipped": 0}
    log_exists = False
    if episode_no > 0:
        try:
            log_path = pipeline._episode_log_path(project, episode_no)
        except Exception as _e:  # noqa: BLE001
            log_path = ""
            _app_logger().warning("[live_feed] 日志路径解析失败：%s", _e)
        if log_path and os.path.isfile(log_path):
            log_exists = True
            try:
                with open(log_path, "r", encoding="utf-8") as _f:
                    _lines = _f.readlines()[-tail:]
            except OSError:
                _lines = []
            for _ln in _lines:
                try:
                    _ev = json.loads(_ln)
                except json.JSONDecodeError:
                    continue
                _name = str(_ev.get("event") or _ev.get("type") or "")
                _kind, _title = _EVT_META.get(_name, ("action", _name or "事件"))
                _step = str(_ev.get("step") or "")
                if _step:
                    _title = f"{_title}：{_STEP_LABELS.get(_step, _step)}"
                _detail = str(_ev.get("error") or _ev.get("note") or _ev.get("detail")
                              or _ev.get("reason") or "")
                _meta = {k: _ev[k] for k in
                         ("step", "episode_no", "shot", "asset", "attempt", "elapsed",
                          "duration", "seq", "pct", "progress", "ok")
                         if k in _ev}
                if _name == "episode_done":
                    counters["done"] += 1
                elif _name in ("episode_failed", "step_fail"):
                    counters["failed"] += 1
                elif _name == "step_skip":
                    counters["skipped"] += 1
                stream.append({"t": _hhmmss(_ev.get("ts") or _ev.get("time")),
                               "kind": _kind, "title": _title,
                               "detail": _detail[:600], "meta": _meta})

    # ---------- ② 当前任务快照 → now（含「正在思考什么」= 模型实际收到的提示词）----------
    now = _live_snapshot(project)
    if now.get("prompt"):
        stream.append({"t": _hhmmss(time.time()), "kind": "thinking",
                       "title": "当前提示词（模型实际收到的输入）",
                       "detail": now["prompt"], "meta": {"task_id": now.get("task_id", "")}})

    # ---------- ③ 五阶段进度 → 步骤清单 ----------
    steps = []
    try:
        _prog = autopilot.project_progress(project) or {}
        _ep = next((e for e in (_prog.get("episodes") or [])
                    if int(e.get("episode_no") or 0) == episode_no), None)
        if _ep:
            for _k, _v in (_ep.get("steps") or {}).items():
                steps.append({"step": _k, "label": _STEP_LABELS.get(_k, _k), "state": _v})
    except Exception as _e:  # noqa: BLE001
        _app_logger().warning("[live_feed] 读取阶段进度失败：%s", _e)

    stream.sort(key=lambda x: str(x.get("t") or ""), reverse=True)
    return jsonify({
        "success": True, "project": project, "episode_no": episode_no,
        "now": now, "steps": steps, "stream": stream[:tail],
        "counters": counters,
        "sources": {"episode_log": log_exists, "live_task": bool(now.get("task_id"))},
    })
