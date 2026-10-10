'''系统维护助手（2026-10-11 从 app.py 下沉，助手域第三批）。'''

# 依赖闭包（实测 3 个对象 34 行）：_maybe_clear_comfyui_history + 两个节流常量。
# 与 artifact_helpers 的 _maybe_reclaim_comfyui_output 同类（都是 ComfyUI 侧定期维护），
# 后续可合并；本批先单独成文件，避免改动已稳定的模块。
import logging
import threading
import time

from config import CLEAR_COMFYUI_HISTORY, CLEAR_COMFYUI_HISTORY_INTERVAL_SEC
from routes._shared import comfyui_client

logger = logging.getLogger(__name__)


_COMFYUI_CLEAR_HISTORY_LAST_TS = 0.0
_COMFYUI_CLEAR_HISTORY_LOCK = threading.Lock()


def _maybe_clear_comfyui_history(where: str = "") -> bool:
    """按节流清空 ComfyUI **任务历史列表**（不是磁盘产物）。

    为什么要做：ComfyUI 界面「任务历史」面板只增不减，质检每失败一次重跑就多一条
    记录，跑几轮后几百条 → 用户会以为「生成了大量废图」。实测面板 162 条时磁盘上
    真正残留的废弃分镜图 **0 张**（清之前 /history 162 条 → 清完 0 条）。

    语义边界（重要）：
      · 只调 `POST /history {"clear":true}`，**绝不删任何 output 文件**；
      · 不影响正在执行/排队中的任务（它们结束后会各自追加新记录）；
      · 只应在**任务收尾**调用 —— 有任务在飞时清掉历史，会让 `wait_for_completion`
        的轮询查不到自己那条记录而误判超时。

    开关 `MJSCXT_CLEAR_COMFYUI_HISTORY=0` 可整体关闭；节流 5 分钟（见 config）。
    永不抛异常、永不阻断生产。
    """
    global _COMFYUI_CLEAR_HISTORY_LAST_TS
    if not CLEAR_COMFYUI_HISTORY:
        return False
    try:
        now = time.time()
        with _COMFYUI_CLEAR_HISTORY_LOCK:
            if now - _COMFYUI_CLEAR_HISTORY_LAST_TS < CLEAR_COMFYUI_HISTORY_INTERVAL_SEC:
                return False
            # 先占用时间戳：真正清理失败也不要在同一分钟内反复重试刷屏。
            _COMFYUI_CLEAR_HISTORY_LAST_TS = now
        ok = comfyui_client.clear_history()
        if ok:
            logger.info("[任务历史] 已清空 ComfyUI 任务历史面板（收尾：%s）", where or "未知")
        return ok
    except Exception as e:  # noqa: BLE001  可观测性优化，绝不能阻断生产
        logger.warning("ComfyUI 任务历史清理异常（不影响生产）：%s: %s",
                           type(e).__name__, e)
        return False
