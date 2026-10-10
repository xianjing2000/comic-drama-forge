# -*- coding: utf-8 -*-
'''文学剧本生成 worker（2026-10-11 从 app.py 下沉，搬迁示范）。

依赖清点（搬迁时实测，非估计）：
  · read_novel_text / NOVELS_DIR  ← config + novel_parser（叶子模块，直接 import）
  · lock / generation_state       ← job_state（共享状态唯一来源）
  · novel_screenplay / LLMError   ← 各自模块
  · _current_llm_client           ← 延迟 import routes._shared 的实现
      （那里是唯一口径；workers 不该另建一套，否则又会出现
        「两处定义、改一处漏一处」）
  · app.logger                    ← 改为模块 logger（本次唯一的行为调整：
      日志归类名从 app 变为 workers.screenplay，级别与内容不变）

业务逻辑与下沉前一字不差：章节正文 → LLM 场次剧本 →
  output/screenplays/<项目>/第N集_文学剧本.md
'''
import logging

import novel_screenplay
from config import NOVELS_DIR
from job_state import generation_state, lock
from llm_client import LLMError
from novel_parser import read_novel_text

logger = logging.getLogger(__name__)


def _current_llm_client():
    '''延迟取当前 LLM 客户端。

    走 routes._shared 的唯一实现（那里已处理 ai_config 的按模块路由、可选客户端等
    细节）。延迟 import 是为了避免 workers 与 routes 的模块级循环依赖。
    '''
    from routes._shared import _current_llm_client as _impl
    return _impl()


def _screenplay_worker(task_id: str, novel_meta: dict, chapter: dict,
                       project_key: str, style: str, episode_no: int):
    '''文学剧本生成 worker：章节正文 → LLM 场次剧本 → output/screenplays/<项目>/第N集_文学剧本.md'''
    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        seg = text[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
        with lock:
            generation_state[task_id].update({"phase": "literary", "progress": 15,
                                              "message": "正在把本章正文改写成文学剧本…"})
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")
        md = novel_screenplay.generate_screenplay(
            client, novel_meta.get("title") or novel_meta.get("name") or "",
            chapter.get("title") or f"第{episode_no}集", seg, style)
        with lock:
            generation_state[task_id].update({"progress": 80, "message": "落盘…"})
        path = novel_screenplay.save_screenplay(
            novel_screenplay.screenplay_path(project_key, episode_no), md)
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "message": "文学剧本已生成（可在前端查看，确认后再改写为分镜剧本）",
                "screenplay_path": path,
                "results": [{"success": True, "episode_no": episode_no,
                             "path": path, "chars": len(md)}]})
    except Exception as e:  # noqa: BLE001
        logger.exception("文学剧本生成失败")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
