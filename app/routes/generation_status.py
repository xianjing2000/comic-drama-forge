# -*- coding: utf-8 -*-
"""生成状态查询 API 蓝图（三步法搬迁，2026-10-09）。

URL 规则与响应体一字不改，只把 @app.route 换成 @generation_status_bp.route。
共享状态来自叶子模块 job_state（唯一来源，禁止在此重新定义/重新赋值）。
"""
from flask import Blueprint, jsonify, request

from job_state import generation_state, lock
import copy

generation_status_bp = Blueprint('generation_status', __name__)

@generation_status_bp.route('/api/generation/status/<task_id>', methods=['GET'])
def api_generation_status(task_id):
    with lock:
        # P2-14（A-21）：锁内深拷贝快照再出锁。旧实现在锁外 jsonify 会读到
        # 「progress 已 100 但 results 只有 3 条」这类半更新快照（与写端点
        # 在锁内 .append/.update 竞态）。深拷贝把一致性窗口收敛到持锁段内。
        state = copy.deepcopy(generation_state.get(task_id, {}))
    return jsonify(state)
