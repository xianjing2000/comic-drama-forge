# -*- coding: utf-8 -*-
"""全自动生产主控（Autonomous Director）

核心目标：用户上传小说 → AI 对话敲定风格 → 一键启动 → 电脑 24h 自动生产 → 用户只看成品

与 autopilot.py 的关系
----------------------
autopilot.py 是「守护循环」——负责轮询、重试、失败隔离。
本模块是「总控层」——负责：
  1. 一键全自动生产（从小说上传到成片交付的全链路编排）
  2. AI 对话与生产联动的语义理解
  3. 生产报告生成
  4. 多项目并行管理

调用链：
  用户上传小说 → api_autonomous_start(project) → 设置 plan + enable autopilot
  用户说"继续生产" → api_autonomous_resume() → resume autopilot
  用户说"停下" → api_autonomous_stop() → pause autopilot
  前端轮询 → api_autonomous_status() → 实时状态
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import threading
from datetime import datetime

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

# ===================== 状态持久化 =====================

def _state_path(project: str = "") -> str:
    from config import PROJECT_OUTPUT_DIR
    root = os.path.join(PROJECT_OUTPUT_DIR, "autonomous")
    return os.path.join(root, project, "state.json") if project else root


def _ensure_state_dir(project: str) -> str:
    path = _state_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _read_state(project: str) -> dict:
    """严格读项目状态（A-4）：缺失→{}；损坏→从 .bak 恢复；无 .bak→抛错。

    旧实现 `except Exception: return {}` 会把损坏的 state.json 静默读成空，
    而 `stop_autonomous` / `resume_autonomous` 是「读改写」，会把空快照写回 →
    项目风格设定 / novel_id 被永久清空。
    """
    return read_json_strict(_state_path(project), {})


def _write_state(project: str, data: dict) -> None:
    """原子写项目状态（A-3）：唯一临时名 + fsync + .bak 快照 + replace 重试。"""
    atomic_write_json(_state_path(project), data)


# ===================== 一键全自动生产 =====================

def start_autonomous(project: str, novel_id: str, plan_overrides: dict = None) -> dict:
    """一键启动全自动生产：配置项目 → 同步 AI 对话设定 → 开启 autopilot

    参数:
        project: 项目名
        novel_id: 小说 ID
        plan_overrides: 覆盖 autopilot plan 的字段（如 style, target_shots 等）

    返回:
        {success, project, novel_id, episodes_total, episodes_pending, message}
    """
    import autopilot
    import project_store
    from novel_parser import get_novel
    from config import NOVELS_DIR

    # 1. 验证小说存在
    try:
        novel_meta = get_novel(NOVELS_DIR, novel_id)
    except Exception as e:
        return {"success": False, "error": f"小说不存在：{novel_id}，{e}"}

    chapters = novel_meta.get("chapters") or []
    if not chapters:
        # 兼容历史数据（缺陷 D3）：本次修复前，正文没有「第X章」标题时会被存成空 chapters。
        # 这里按正文实时重切一次（split_chapters 现已带兜底切分），避免老项目永远无法生产。
        try:
            from novel_parser import read_novel_text, split_chapters
            chapters = split_chapters(read_novel_text(NOVELS_DIR, novel_id) or "")
            if chapters:
                novel_meta["chapters"] = chapters
                logger.info("小说 %s 无章节标题，已兜底切分为 %d 节", novel_id, len(chapters))
        except Exception as e:  # noqa: BLE001
            logger.warning("章节兜底切分失败：%s", e)
            chapters = []

    if not chapters:
        return {"success": False,
                "error": "该小说正文为空或无法解析，请确认文件内容后重新上传"}

    # 2. 确保项目存在
    proj = project_store.ensure_project_for_novel(novel_id)
    if not proj:
        return {"success": False, "error": "项目创建失败"}

    project_key = proj.get("dir_key") or project
    chapters = novel_meta.get("chapters", [])

    # 3. 同步 AI 对话设定到 autopilot plan
    state_path = _ensure_state_dir(project_key)
    # A-4 边界决策：**不**在此处吞掉损坏异常。start 下面第 5 步会用一份「新建的
    # state」覆盖 state.json（只继承 style_brief），若把损坏态读成空再写回，
    # AI 对话敲定的风格设定会被永久清空。故让 read_json_strict 的抛错上抛，
    # 用户看到明确的启动失败并可按提示从 .bak 恢复。
    current_state = _read_state(project_key)
    style_brief = current_state.get("style_brief") or ""

    # 4. 写入 autopilot plan
    plan_patch = {
        "enabled": True,
        "novel_id": novel_id,
        "style": style_brief,
        "episodes": "all",
        **(plan_overrides or {})
    }
    autopilot.set_plan(project_key, plan_patch, novel_id)

    # 真实「集」数由 episode_units 展开（一章可拆多集），state.json 与播报文案都以此为准
    total_episodes = len(autopilot.episode_units(chapters, plan_patch))

    # 5. 记录启动状态
    _write_state(project_key, {
        "started_at": datetime.now().isoformat(),
        "novel_id": novel_id,
        "project_key": project_key,
        "style_brief": style_brief,
        "total_episodes": total_episodes,
        "plan": plan_patch,
    })

    # 6. 唤醒 autopilot（必须用 resume() 而非 wake()：wake 只设信号不创建线程）
    autopilot.resume()

    # 7. 统计待生产集数（缺陷 D2：区分「已全部完成」与「目标集为空」，不谎报已启动）
    progress = autopilot.project_progress(project_key)
    episodes = progress.get("episodes", [])
    done = sum(1 for ep in episodes if ep.get("state") == "done")
    pending = sum(1 for ep in episodes if ep.get("state") not in ("done", "rejected"))

    if not episodes:
        logger.warning("项目 %s 分集目标为空：novel=%s, chapters=%d",
                       project_key, novel_id, len(chapters))
        return {
            "success": False,
            "project": project_key,
            "novel_id": novel_id,
            "total_episodes": total_episodes,
            "pending_episodes": 0,
            "error": "该项目没有可生产的集（分集目标为空），请检查小说章节与项目集数配置",
        }

    if pending == 0:
        message = (
            f"该项目 {total_episodes} 集已全部生产完成，无需重复启动"
            if done else
            f"该项目当前没有待生产的集（{total_episodes} 集均为已打回/待人工处理状态），请先处理异常"
        )
    else:
        message = f"已启动全自动生产，覆盖 {len(chapters)} 章（共 {total_episodes} 集），当前待生产 {pending} 集"

    logger.info("全自动生产启动: project=%s, novel=%s, chapters=%d, episodes=%d, pending=%d, done=%d",
                project_key, novel_id, len(chapters), total_episodes, pending, done)

    return {
        "success": True,
        "project": project_key,
        "novel_id": novel_id,
        "total_episodes": total_episodes,
        "pending_episodes": pending,
        "done_episodes": done,
        "already_done": pending == 0,
        "style_brief": style_brief,
        "message": message,
    }


def stop_autonomous(project: str = "") -> dict:
    """停止全自动生产（暂停 autopilot）"""
    import autopilot
    autopilot.pause("用户主动停止")
    if project:
        # A-4 边界决策：「停止生产」必须永远可用（运维安全阀），不能因为
        # state.json 损坏就抛错挡住用户。但绝不能用空快照覆盖 —— 读不到就
        # 显式报错并**跳过** stopped_at 落盘，等人工修复后再写。
        try:
            state = _read_state(project)
        except (ValueError, OSError) as e:
            logger.error("项目 %s 的 state.json 损坏且无可用 .bak，"
                         "跳过 stopped_at 落盘以免覆盖（autopilot 已暂停）：%s",
                         project, e)
        else:
            state["stopped_at"] = datetime.now().isoformat()
            _write_state(project, state)
    return {"success": True, "message": "已停止生产"}


def resume_autonomous(project: str = "") -> dict:
    """恢复全自动生产（恢复 autopilot）"""
    import autopilot
    autopilot.resume()
    if project:
        # A-4 边界决策：同 stop_autonomous —— 恢复动作不能被状态文件损坏挡住，
        # 但也不能把损坏态读成空再写回；读不到就显式报错并跳过落盘。
        try:
            state = _read_state(project)
        except (ValueError, OSError) as e:
            logger.error("项目 %s 的 state.json 损坏且无可用 .bak，"
                         "跳过 stopped_at 清除以免覆盖（autopilot 已恢复）：%s",
                         project, e)
        else:
            state.pop("stopped_at", None)
            _write_state(project, state)
    return {"success": True, "message": "已恢复生产"}


def status(project: str = "") -> dict:
    """查询全自动生产状态"""
    import autopilot
    import project_store

    result = {
        "autopilot": {
            "running": autopilot.status().get("running", False),
            "paused": autopilot.is_paused(),
            "current": autopilot.status().get("current"),
            "totals": autopilot.status().get("totals", {}),
        },
        "projects": []
    }

    projects = project_store.list_projects() if project else []
    if project:
        projects = [p for p in projects if p.get("dir_key") == project]

    for proj in projects:
        proj_key = proj.get("dir_key", "")
        # A-4 边界决策：status 是**只读**轮询视图，不参与任何「读改写」，
        # 单个项目状态损坏不应让整个状态接口 500。故显式记 error 后降级为 {}，
        # 前端会看到该项目字段为空（响亮降级，非静默清空）。
        try:
            state = _read_state(proj_key)
        except (ValueError, OSError) as e:
            logger.error("项目 %s 的 state.json 损坏且无可用 .bak，"
                         "该项目的状态字段本次按空处理：%s", proj_key, e)
            state = {}
        progress = autopilot.project_progress(proj_key)
        plan = autopilot.get_plan(proj_key)

        result["projects"].append({
            "project": proj_key,
            "name": proj.get("name", proj_key),
            "novel_id": state.get("novel_id", plan.get("novel_id", "")),
            "style_brief": state.get("style_brief", ""),
            "total_episodes": state.get("total_episodes") or len(progress.get("episodes", [])),
            "episodes_done": int(progress.get("done") or 0),
            "episodes_pending": max(int(progress.get("total") or 0) - int(progress.get("done") or 0), 0),
            "episodes_failed": 0,  # project_progress 暂无失败计数维度
            "enabled": plan.get("enabled", False),
            "started_at": state.get("started_at", ""),
            "stopped_at": state.get("stopped_at", ""),
        })

    return result


# ===================== AI 对话联动 =====================

def interpret_chat_command(message: str, project: str = "") -> dict:
    """解析用户对话指令，转化为生产动作

    支持的指令：
    - "开始生产" / "开始" / "开始全自动" → 启动全自动
    - "继续生产" / "继续" → 恢复暂停
    - "停止" / "停下" / "暂停" → 暂停
    - "状态" / "进度" → 查询状态
    - "生成第N集" → 指定集数
    - "只生成前N集" → 限制范围
    """
    msg = message.strip().lower()
    proj_key = project or ""

    # 停止
    if any(k in msg for k in ["停止", "停下", "暂停", "pause"]):
        return stop_autonomous(proj_key)

    # 恢复/继续
    if any(k in msg for k in ["继续", "resume", "恢复", "接着生产"]):
        return resume_autonomous(proj_key)

    # 状态查询
    if any(k in msg for k in ["状态", "进度", "怎么样", "多少集", "完成"]):
        return status(proj_key)

    # 开始生产
    if any(k in msg for k in ["开始生产", "全自动", "开始全自动", "启动生产"]):
        # 尝试从当前项目获取 novel_id
        if proj_key:
            import autopilot
            plan = autopilot.get_plan(proj_key)
            novel_id = plan.get("novel_id", "")
            if novel_id:
                return start_autonomous(proj_key, novel_id)
        return {"success": False, "error": "请先通过「上传小说」接口上传小说并创建项目"}

    # 指定集数
    match = re.search(r"第\s*(\d+)\s*集", msg)
    if match:
        return {"success": False, "error": "单集生产请使用专用接口，指令暂不支持"}

    # 默认：返回状态
    return status(proj_key)


# ===================== 生产报告 =====================

def generate_report(project: str, episode_no: int = None) -> dict:
    """生成生产报告（单集或全部）"""
    import autopilot
    import project_store

    project_key = project
    history = autopilot.read_history(project_key)

    if episode_no is not None:
        history = [h for h in history if h.get("episode_no") == episode_no]

    if not history:
        return {"success": False, "error": "暂无生产记录"}

    # 统计
    total = len(history)
    ok_count = sum(1 for h in history if h.get("ok"))
    failed_count = total - ok_count
    # 注意：autopilot 的 history 行（append_history 写入）没有 retries 字段，
    # 原来的 total_retries = sum(h.get("retries", 0)) 恒为 0、失真，故直接删除该统计。
    total_time = sum(h.get("elapsed_sec", 0) for h in history)

    report = {
        "success": True,
        "project": project_key,
        "episodes_reported": total,
        "summary": {
            "total_episodes": total,
            "completed": ok_count,
            "failed": failed_count,
            "total_elapsed_sec": round(total_time, 1),
            "avg_elapsed_sec": round(total_time / max(ok_count, 1), 1),
        },
        "episodes": history[-20:],  # 最近 20 条
    }
    return report


def export_report(project: str, format: str = "json") -> str:
    """导出生产报告为文件"""
    import autopilot

    report = generate_report(project)
    if not report.get("success"):
        return ""

    from config import PROJECT_OUTPUT_DIR
    output_dir = os.path.join(PROJECT_OUTPUT_DIR, "reports", project)
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"report_{timestamp}.{format}"
    filepath = os.path.join(output_dir, filename)

    if format == "json":
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    elif format == "md":
        lines = [
            f"# 生产报告：{project}",
            f"",
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"",
            f"## 概要",
            f"- 总集数：{report['summary']['total_episodes']}",
            f"- 完成：{report['summary']['completed']}",
            f"- 失败：{report['summary']['failed']}",
            f"- 总耗时：{report['summary']['total_elapsed_sec']} 秒",
            f"",
            f"## 明细",
        ]
        for ep in report.get("episodes", []):
            lines.append(f"- 第{ep.get('episode_no')}集：{'✅' if ep.get('ok') else '❌'} "
                         f"耗时{ep.get('elapsed_sec', 0):.0f}s")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    return filepath


# ===================== 多项目并行管理 =====================

def list_all_projects() -> list:
    """列出所有有生产记录的项目"""
    import project_store
    from config import PROJECT_OUTPUT_DIR

    projects = []
    for proj in project_store.list_projects():
        proj_key = proj.get("dir_key", "")
        # A-4 边界决策：列表视图只读；状态损坏的项目显式报错并**跳过该条**，
        # 既不阻断整个列表，也不用空快照冒充「有生产记录的项目」。
        try:
            state = _read_state(proj_key)
        except (ValueError, OSError) as e:
            logger.error("项目 %s 的 state.json 损坏且无可用 .bak，"
                         "已从「有生产记录的项目」列表中跳过：%s", proj_key, e)
            continue
        if not state:
            continue
        projects.append({
            "project": proj_key,
            "name": proj.get("name", proj_key),
            "novel_id": state.get("novel_id", ""),
            "started_at": state.get("started_at", ""),
        })
    return projects


def get_deliverables(project: str) -> list:
    """获取项目的交付物列表"""
    import autopilot
    return autopilot.pipeline_list_deliverables(project)
