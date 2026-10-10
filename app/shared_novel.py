# -*- coding: utf-8 -*-
"""小说与剧集规划（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 7 步，2026-10-10）

core 模块需要这些能力却只能从 routes._shared 取 —— 与 HTTP 无关。
上移到 app/ 层后，core 侧依赖的是 app 层模块。

## 铁律

不得反向依赖 routes/*。autopilot / novel_to_script / pipeline / preview_gate /
ai_chat / project_store / style_kit 均已核对不导入 routes._shared。
"""
from __future__ import annotations

import os

import autopilot
import novel_to_script
import project_store
from config import NOVELS_DIR
from novel_parser import list_novels
from shared_base import _app_logger


UPLOAD_TMP_DIR = os.path.join(NOVELS_DIR, "_uploads")


def _novels_stats(project_ref: str = None, include_unbound: bool = True):
    items = list_novels(NOVELS_DIR)
    # 标注每部小说当前归属的项目（一部小说 = 一个独立项目）
    for m in items:
        rec = project_store.find_by_novel(m.get("novel_id") or m.get("id"))
        m["project_id"] = (rec or {}).get("id", "")
        m["project_key"] = (rec or {}).get("dir_key", "")
        m["project_name"] = (rec or {}).get("name", "")
        m["bound"] = bool(rec)
    if project_ref:
        rec = project_store.get_project(project_ref)
        pid = (rec or {}).get("id") or project_ref
        filtered = [m for m in items if m["project_id"] == pid
                    or (include_unbound and not m["project_id"])]
    else:
        filtered = items
    return {
        "count": len(filtered),
        "total_chars": sum(int(m.get("char_count") or 0) for m in filtered),
        "novels": filtered,
        "all_count": len(items),
    }


def _estimate_subchunks(char_count: int) -> int:
    """不读全文的二次分块数量估算（用于章节列表）"""
    return novel_to_script.estimate_subchunks(char_count)


EPISODE_BATCH_LIMIT = 30          # 单次批量生成集数上限（保护后台任务）


def _episode_units_for_chapters(novel_meta: dict, chapters: list) -> list:
    """把「用户选中的章」展开成**拍摄单元**（超长章会拆成多集）。

    ⚠️ 单元编号必须基于**全量章节**展开（口径 = ``autopilot.episode_units``），
    不能用传入的子集 —— 否则同一章在「手动选集生成」与「托管」两条链路上会拿到
    不同的集号，产物（``第N集.json`` / 成片 / 验收记录）互相错位。
    """
    selected = [int(c.get("index") or 0) for c in (chapters or [])]
    if not selected:
        return []
    try:
        all_chapters, text = autopilot.chapters_and_text(novel_meta)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("章节列表读取失败（按一章一集处理）：%s", e)
        all_chapters, text = [], ""
    if all_chapters:
        try:
            units = autopilot.episode_units(all_chapters, {"episodes": selected}, text)
            if units:
                return units
        except Exception as e:  # noqa: BLE001
            _app_logger().warning("拆章失败（按一章一集处理）：%s", e)
    # 兜底：拿不到全量章节时退回「一章一集」（与历史行为一致）
    return [{"episode_no": int(c.get("index") or i + 1),
             "chapter_index": int(c.get("index") or i + 1),
             "part": 1, "parts": 1, "chapter": c}
            for i, c in enumerate(chapters or [])]


