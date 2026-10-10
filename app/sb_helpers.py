# -*- coding: utf-8 -*-
'''分镜自愈助手（2026-10-11 从 app.py 下沉）。'''

# 由 tools/sink_helpers.py 自动生成：闭包展开 + 原样复制 import。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
import logging

from routes._shared import (_first_existing, _shot_num_key, comfyui_client)  # noqa: F401

logger = logging.getLogger(__name__)

_SB_STRUCTURAL_DEFECT_KEYWORDS = (
    # ⚠️ 2026-10-09 按用户决策：「面板雷同 / 面板重复」已**移出**本词表（降级为 warning）。
    #    原因与影响见 qc_client.IMAGE_CRITICAL_KEYWORDS 处的同批注释：
    #    9 格对 7B 模型要求过高、首轮通过率仅 1/7，且它是唯一 critical，长期拖住链路。
    #    代价：**雷同的分镜图现在可能通过软放行入库**（这正是 2026-10-08 加它时要防的）。
    #    回退：把下面这行取消注释即可恢复「结构性缺陷 → 不参与软放行」。
    # "面板雷同", "九宫格面板雷同", "面板重复",
    "九宫格场景不一致", "格间换场景",
    "九宫格左右手镜像", "左右手互换", "左右手颠倒", "镜像翻转", "朝向翻转",
)


def _sb_heal_comfyui(task_id: str, project: str) -> bool:
    """分镜/资产 worker 里的 ComfyUI 自愈：连续拒连/未出图时自动重启 ComfyUI 进程。

    背景（2026-10-08）：ComfyUI 在「后端超时 → 全局 /interrupt」后可能进入半死状态
    （webserver 仍监听 8188 但执行器被打断），后续镜头 /upload/image 全部 10061 拒连。
    本函数在「该镜头 ComfyUI 未出图且 ComfyUI 不在线」时重启 ComfyUI 并等它就绪，
    让下一镜头（或本镜头重试）能继续，而非让整集镜头全挂。

    返回 True = 已重启且就绪（可继续），False = 重启失败/ComfyUI 仍离线（按离线处理）。
    全程 fail-open，不抛异常。
    """
    global _comfyui_heal_count
    try:
        _comfyui_heal_count = getattr(globals(), "_comfyui_heal_count", 0) + 1
        logger.warning(
            "[自愈] 镜头 ComfyUI 未出图且离线，自动重启 ComfyUI（第 %d 次，项目=%s，任务=%s）",
            _comfyui_heal_count, project, task_id)
        ok = comfyui_client.restart_comfyui(wait_sec=180, poll_sec=3.0)
        if ok:
            logger.info("[自愈] ComfyUI 重启成功，继续分镜生成")
        else:
            logger.warning("[自愈] ComfyUI 重启后仍未就绪，本镜头继续按离线处理")
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning("[自愈] ComfyUI 重启异常（按离线处理）：%s", e)
        return False


def _sb_structural_defect(gate) -> bool:
    """该镜的质检结论是否含**结构性缺陷**（软放行必须跳过它们）。

    判定文本 = reason + label + critical_issues（三者任一命中即算）。
    gate 非 dict / 缺字段都返回 False（退化为「可软放行」，不改变既有行为）。
    """
    if not isinstance(gate, dict):
        return False
    parts = [str(gate.get("reason") or ""), str(gate.get("label") or "")]
    for x in (gate.get("critical_issues") or []):
        parts.append(str(x))
    txt = " ".join(parts)
    return any(k in txt for k in _SB_STRUCTURAL_DEFECT_KEYWORDS)

