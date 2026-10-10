# -*- coding: utf-8 -*-
'''音效隔离（新模块）（2026-10-11 从 app.py 下沉）。'''

# 由 tools/sink_helpers.py 自动生成：闭包展开 + 原样复制 import。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
import logging

logger = logging.getLogger(__name__)

def _isolate_shot_sfx(video_path: str, project: str, episode, shot_id) -> dict:
    """对单镜音轨做人声分离，只留音效（H3 原生音效，剔掉它自带的说话声）。

    **fail-open**：任何异常都只返回 ok=False，绝不打断出片流程。
    """
    try:
        import sfx_isolate
        return sfx_isolate.isolate_sfx(
            video_path, project, f"ep{int(episode):02d}_shot{int(shot_id):02d}")
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

