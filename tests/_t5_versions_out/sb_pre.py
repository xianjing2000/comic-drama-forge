# -*- coding: utf-8 -*-
'''分镜自愈助手（2026-10-11 从 app.py 下沉）。'''

# 由 tools/sink_helpers.py 自动生成：闭包展开 + 原样复制 import。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
import logging
import re

from shared_project import _first_existing, _shot_num_key, comfyui_client  # noqa: F401  再导出

logger = logging.getLogger(__name__)

_SB_STRUCTURAL_DEFECT_KEYWORDS = (
    # ⚠️ 2026-10-09 按用户决策：「面板雷同 / 面板重复」已**移出 qc 侧硬阻断词表**
    #    （降级为 warning；原因与影响见 qc_client.CRITICAL_ISSUE_KEYWORDS 处的同批注释：
    #    9 格对 7B 模型要求过高、首轮通过率仅 1/7，且它是唯一 critical，长期拖住链路）。
    #
    # ⭐ 2026-10-12（T18 P0）**修正一个此前的口径混淆**：「不硬阻断」≠「可软放行」。
    #    面板雷同仍是「九宫格拆解本身失败」的结构性缺陷 —— 放行等于把雷同废图交付给用户
    #    （T2 实测：提示词改动后仍有 4/6 镜被判面板雷同，占失败判语绝大多数）。
    #    故本表**恢复**面板雷同类词条。两者是**互补而非冲突**的两件事：
    #      · 硬阻断（是否进 critical_issues）：由 **qc 侧**词表决定 → 保持 2026-10-09 决策，不恢复；
    #      · 软放行豁免（能否借 storyboard_soft_qc 入库）：由**本表**决定 → 本次恢复否决。
    #    回退：删除 _panel_dup_hit() 的调用即恢复「面板雷同可被软放行」。
    # ⚠️ 面板雷同**不放进本表做字面匹配** —— 它由下方 _PANEL_DUP_RE 正则判定，
    #    原因见该正则的注释（VLM 会插修饰语「面板**严重**雷同」；且达标结论里会出现
    #    「未达面板雷同阈值」这类**否定式**，字面词条会把正面结论误判成缺陷）。
    #
    # ✅ 2026-10-12（T18）用生产真实产物校准（shot_42 / shot_44）：
    #    · shot_44 reason「九宫格面板严重雷同（第3、4、6、7格…）」→ 曾被软放行入库
    #      （meta.extra.qc_soft_accepted=True，label=关键缺陷阻断（独立复核））；
    #    · shot_42 reason「…动作推进连贯，未达面板雷同阈值，可判合格。」→ 是**达标**结论。
    #    两者都含「雷同」，必须靠正则 + 否定语境区分，字面词条做不到。
    "九宫格场景不一致", "格间换场景",
    "九宫格左右手镜像", "左右手互换", "左右手颠倒", "镜像翻转", "朝向翻转",
    # ⭐ 2026-10-12（T18 P0）补齐**手指畸变类**词条：
    #    此前本表一条手指类词都没有，而 qc 侧 CRITICAL_ISSUE_KEYWORDS 有 13 条
    #    → 硬闸门命中「九宫格手指畸变」判阻断，本表却不认得 → _sb_structural_defect()
    #    返回 False → 畸变图照样被软放行入库（镜头 44 实测；T5 审计 F2 得出同一结论）。
    # ⚠️ 两表是**同一套判据的两处**（qc_client.CRITICAL_ISSUE_KEYWORDS ↔ 本表），
    #    新增/删除任一侧的该类词条时**必须同时改另一侧**，否则「判官判阻断、这里软放行」两处打架。
    # ⚠️ 刻意只用**长词/特征词**：单独的「手」「手指」会与正常构图描述撞车
    #    （如「手指向远方」「双手交叠」），收短词会造成大面积误杀 —— 与 qc 侧同一取舍。
    "九宫格手指畸变", "手指异常", "手指粘连", "指头粘连", "手指融合", "多指畸形",
    "手指粗细不均", "指节模糊",
    "杵状指", "指尖膨大", "指尖钝化", "指尖球状", "六指",
    # 与 qc_client.CRITICAL_ISSUE_KEYWORDS 同步（同一套判据的两处，必须一起改）：
    # 角色与设定不符的长词变体。不同步会导致「判官判阻断、这里软放行」两处打架。
    "与设定不符", "与设定图不符", "与设定不一致", "与设定图不一致",
    "角色设定不符", "角色设定不一致",
    "发型与设定不符", "发型与设定不一致", "服装与设定不符", "服装与设定不一致",
    "外观与设定不符", "外观与设定不一致", "形象与设定不符",
    # 2026-10-12 第二轮补齐（第一轮测试发现盲区）：
    #   反例原文是「…且角色发型/服装与设定图**及上一镜**明显不符」——
    #   「与设定图」与「不符」之间插入了「及上一镜」，固定搭配词匹配不上。
    #   故补「明显不符」这类**不依赖前置词**的短语，并单列「与上一镜」的连续性缺陷。
    "明显不符", "明显不一致",
    "与上一镜不符", "与上一镜不一致", "与上一镜不连贯", "与上一镜不连续",
    "与设定图及上一镜不符",
)


# --------------------------------------------------------------------------- #
# 「面板雷同」类缺陷的**词根**判定（2026-10-12，T18 P0）
# --------------------------------------------------------------------------- #
# 为什么不写进 _SB_STRUCTURAL_DEFECT_KEYWORDS 做字面匹配 —— 两条都用生产实测校准：
#   ① 漏判：VLM 会在词间插修饰语。shot_44 原文是「九宫格面板**严重**雷同（第3、4、6、7格
#      动作构图几乎相同）」，字面词条「面板雷同」匹配不上 → 硬闸门已判关键缺陷阻断，
#      软放行闸门却不认 → 该镜被放行入库（meta.extra.qc_soft_accepted=True）。
#   ② 误判：达标结论里会出现否定式。shot_42 原文是「…动作推进连贯，**未达**面板雷同阈值，
#      可判合格。」——字面词条会把这条**正面结论**当成结构性缺陷。
# 故改用「词根正则 + 紧邻否定语境排除」，两者缺一不可。
_PANEL_DUP_RE = re.compile(
    r"(?:面板|九宫格|格间)[^\s，。；、,.;：:!！?？]{0,6}?(?:雷同|重复)")
#: 紧邻（前 3 字内）出现即视为**否定/达标结论**，不算缺陷。
#: ⚠️ 只取紧邻窗口：放宽会把真缺陷一起吞掉（如「第5格无主体，面板严重雷同」里的「无」）。
_PANEL_DUP_NEG = ("未达", "未超", "未触及", "未见", "没有", "无",
                  "不构成", "不算", "达标", "合格")


def _panel_dup_hit(txt: str) -> bool:
    """判定文本是否含**未被否定**的「面板雷同 / 面板重复」表述。"""
    for m in _PANEL_DUP_RE.finditer(txt or ""):
        if any(n in txt[max(0, m.start() - 3):m.start()] for n in _PANEL_DUP_NEG):
            continue
        return True
    return False


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
    # 面板雷同类：词根正则 + 否定语境排除（不能用字面词条，原因见 _PANEL_DUP_RE 注释）
    if _panel_dup_hit(txt):
        return True
    return any(k in txt for k in _SB_STRUCTURAL_DEFECT_KEYWORDS)

