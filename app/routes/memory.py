# -*- coding: utf-8 -*-
"""AI 记忆（教训库 / 洞察 / 趋势 / 导出）API 蓝图 —— Blueprint 拆分第二批。

15 条 /api/memory/* 路由原来住在 app.py（约 17600-18300 行段）。它们依赖的
PROJECT_OUTPUT_DIR、prompt_memory、get_memory_system 都是叶子模块，
只有 _autopilot_guard 与 _prompt_memory_* 是 app.py 的私有助手 —— 那五个已下沉到
routes/_shared.py，所以本模块不再需要 app.py 的任何东西。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @memory_bp.route。
"""
from datetime import datetime

import prompt_memory
from flask import Blueprint, current_app, jsonify, request

from ai_memory import get_memory_system
from config import PROJECT_OUTPUT_DIR
from routes._shared import (_app_logger, _autopilot_guard, _prompt_memory_dead_count, _prompt_memory_used_total, _prompt_memory_view)

memory_bp = Blueprint('memory', __name__)

@memory_bp.route('/api/memory/lessons', methods=['GET'])
@_autopilot_guard
def api_memory_lessons():
    """质检教训库（generation 链路自动学习成果）

    query: kind（逗号分隔多值）/ project / since / until(ISO，只到日期按当天末闭区间) /
           q（关键词）/ limit（默认 50）/ offset（默认 0）/ prune_empty=1（顺手清理空记录）
    → {success, pruned, total, filtered, offset, limit, by_kind, dead_lessons, lessons[]}
    """
    kind = str(request.args.get('kind') or '')
    project = str(request.args.get('project') or '').strip()
    since = str(request.args.get('since') or '').strip()
    until = str(request.args.get('until') or '').strip()
    q = str(request.args.get('q') or '').strip()
    try:
        limit = max(0, min(500, int(request.args.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    removed = 0
    if str(request.args.get('prune_empty') or '') in ('1', 'true', 'yes'):
        try:
            removed = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).prune_empty()
        except Exception as e:  # noqa: BLE001
            _app_logger().warning("清理空教训失败：%s", e)
    try:
        page = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).query(
            kind=kind, project=project, since=since, until=until, q=q,
            limit=limit, offset=offset)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("读取质检教训库失败：%s", e)
        return jsonify({"success": False, "pruned": removed, "total": 0, "filtered": 0,
                        "offset": offset, "limit": limit, "by_kind": {}, "dead_lessons": 0,
                        "lessons": [], "error": str(e)})
    return jsonify({
        "success": True, "pruned": removed,
        "total": page["total"], "filtered": page["filtered"],
        "offset": offset, "limit": limit,
        "by_kind": page["by_kind"], "dead_lessons": page["dead_lessons"],
        "lessons": page["items"],
    })

@memory_bp.route('/api/memory/lessons/<lesson_id>', methods=['DELETE'])
@_autopilot_guard
def api_memory_lesson_delete(lesson_id):
    """删除单条教训（按确定性主键 lesson_id，"L"+sha1 前 16 位）。

    → {success, deleted, lesson_id}；未命中返回 404 {"success":false,"error":"未找到该教训"}。
    """
    lid = str(lesson_id or "").strip()
    if not lid:
        return jsonify({"success": False, "error": "缺少 lesson_id"}), 400
    try:
        ok = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).delete(lid)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("删除教训失败：%s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    if not ok:
        return jsonify({"success": False, "deleted": 0, "lesson_id": lid,
                        "error": "未找到该教训"}), 404
    return jsonify({"success": True, "deleted": 1, "lesson_id": lid})

@memory_bp.route('/api/memory/lessons/clear', methods=['POST'])
@_autopilot_guard
def api_memory_lessons_clear():
    """清空某一环节的全部教训（body {kind}；kind 为空 = 清空全部）。

    → {success, cleared, kind}。
    """
    data = request.json or {}
    kind = str(data.get("kind") or "").strip()
    try:
        cleared = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).clear(kind)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("清空教训失败：%s", e)
        return jsonify({"success": False, "cleared": 0, "kind": kind,
                        "error": str(e)}), 500
    return jsonify({"success": True, "cleared": cleared, "kind": kind})

@memory_bp.route('/api/memory/lessons/search', methods=['GET'])
@_autopilot_guard
def api_memory_lessons_search():
    """按提示词召回教训建议（可视化「如果现在生成，会带上哪些历史修正」）

    query: kind（默认 storyboard）/ prompt（必填）/ project / style
    """
    kind = str(request.args.get('kind') or 'storyboard')
    prompt = str(request.args.get('prompt') or '').strip()
    project = str(request.args.get('project') or '').strip()
    style = str(request.args.get('style') or '').strip()
    if not prompt:
        return jsonify({"success": False, "error": "缺少 prompt 参数"}), 400
    try:
        hints = prompt_memory.suggest(kind=kind, prompt=prompt, project=project,
                                      root_dir=PROJECT_OUTPUT_DIR, style=style)
        learned = prompt_memory.learned_prompt(kind=kind, prompt=prompt, project=project,
                                               root_dir=PROJECT_OUTPUT_DIR, style=style)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"召回失败：{e}"}), 500
    return jsonify({"success": True, "kind": kind, "project": project, "style": style,
                    "hints": hints, "learned_prompt": learned,
                    "changed": learned != prompt})

@memory_bp.route('/api/memory/stats', methods=['GET'])
@_autopilot_guard
def api_memory_stats():
    """获取 AI 记忆统计（含**真实质检教训库**的条数，不再只报手动登记的几条）"""
    mem = get_memory_system()
    stats = mem.get_stats()
    trends = mem.evolution.analyze_trends()
    insights = mem.evolution.generate_insights(limit=5)
    lessons = _prompt_memory_view(limit=0)
    # 让前端「经验条数」反映真实学习成果，而不是 3 条测试数据
    stats = dict(stats or {})
    stats["prompt_lessons"] = lessons["total"]
    stats["prompt_lessons_by_kind"] = lessons["by_kind"]
    return jsonify({
        "success": True,
        "stats": stats,
        "trends": trends,
        "insights": insights,
        "lessons": {
            "total": lessons["total"],
            "by_kind": lessons["by_kind"],
            "dead_lessons": _prompt_memory_dead_count(),
            "used_total": _prompt_memory_used_total(),
        },
    })

@memory_bp.route('/api/memory/list', methods=['GET'])
@_autopilot_guard
def api_memory_list():
    """列出记忆"""
    mem_type = request.args.get('type')
    limit = int(request.args.get('limit', 50))
    mem = get_memory_system()
    entries = mem.list_all(mem_type=mem_type, limit=limit)
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in entries],
        "total": len(entries),
    })

@memory_bp.route('/api/memory/search', methods=['GET'])
@_autopilot_guard
def api_memory_search():
    """搜索记忆"""
    query = request.args.get('query', '')
    mem_type = request.args.get('type')
    limit = int(request.args.get('limit', 10))
    if not query:
        return jsonify({"success": False, "error": "缺少 query 参数"}), 400
    mem = get_memory_system()
    results = mem.search_by_pattern(query, mem_type=mem_type, limit=limit)
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in results],
        "total": len(results),
    })

@memory_bp.route('/api/memory/record', methods=['POST'])
@_autopilot_guard
def api_memory_record():
    """记录记忆"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    mem_type = data.get('type', 'insight')
    content = data.get('content', '')
    context = data.get('context', {})
    confidence = data.get('confidence', 1.0)
    tags = data.get('tags', [])
    source = data.get('source', 'manual')

    if not content:
        return jsonify({"success": False, "error": "缺少 content"}), 400

    mem = get_memory_system()
    entry = mem.add_memory(
        mem_type=mem_type,
        content=content,
        context=context,
        confidence=confidence,
        tags=tags,
        source=source,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})

@memory_bp.route('/api/memory/record-lesson', methods=['POST'])
@_autopilot_guard
def api_memory_record_lesson():
    """记录质检教训"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    project = data.get('project', '')
    episode = data.get('episode', 0)
    prompt = data.get('prompt', '')
    issues = data.get('issues', [])
    category = data.get('category', 'quality')

    if not project or not issues:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400

    mem = get_memory_system()
    entry = mem.record_lesson(
        project=project,
        episode=episode,
        prompt=prompt,
        issues=issues,
        category=category,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})

@memory_bp.route('/api/memory/record-success', methods=['POST'])
@_autopilot_guard
def api_memory_record_success():
    """记录成功经验"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    project = data.get('project', '')
    episode = data.get('episode', 0)
    prompt = data.get('prompt', '')
    highlights = data.get('highlights', [])
    category = data.get('category', 'quality')

    if not project or not highlights:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400

    mem = get_memory_system()
    entry = mem.record_success(
        project=project,
        episode=episode,
        prompt=prompt,
        highlights=highlights,
        category=category,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})

@memory_bp.route('/api/memory/optimize-prompt', methods=['POST'])
@_autopilot_guard
def api_memory_optimize_prompt():
    """基于记忆优化提示词"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    base_prompt = data.get('prompt', '')
    issues = data.get('issues', [])

    if not base_prompt:
        return jsonify({"success": False, "error": "缺少 prompt"}), 400

    mem = get_memory_system()
    optimized = mem.evolution.auto_optimize_prompt(base_prompt, issues)
    return jsonify({
        "success": True,
        "original": base_prompt,
        "optimized": optimized,
        "improvements": len(issues),
    })

@memory_bp.route('/api/memory/insights', methods=['GET'])
@_autopilot_guard
def api_memory_insights():
    """获取 AI 洞察和建议"""
    limit = int(request.args.get('limit', 5))
    mem = get_memory_system()
    insights = mem.evolution.generate_insights(limit=limit)
    return jsonify({
        "success": True,
        "insights": insights,
        "total": len(insights),
    })

@memory_bp.route('/api/memory/clear-old', methods=['POST'])
@_autopilot_guard
def api_memory_clear_old():
    """清理过期记忆"""
    data = request.get_json(silent=True) or {}
    days = int(data.get('days', 90))
    mem = get_memory_system()
    cleared = mem.clear_old(days=days)
    return jsonify({
        "success": True,
        "cleared": cleared,
        "days": days,
    })

@memory_bp.route('/api/memory/export', methods=['GET'])
@_autopilot_guard
def api_memory_export():
    """导出记忆数据"""
    mem_type = request.args.get('type')
    mem = get_memory_system()
    entries = mem.list_all(mem_type=mem_type) if mem_type else mem.list_all()
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in entries],
        "total": len(entries),
        "exported_at": datetime.now().isoformat(),
    })

@memory_bp.route('/api/memory/trends', methods=['GET'])
@_autopilot_guard
def api_memory_trends():
    """获取趋势分析"""
    mem = get_memory_system()
    trends = mem.evolution.analyze_trends()
    return jsonify({
        "success": True,
        "trends": trends,
    })
