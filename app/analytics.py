# -*- coding: utf-8 -*-
"""成本与耗时看板（P2-3）

解决的问题
----------
漫剧生成是「长时间 + 高显存」任务，实际生产中最常被问的两个问题是：
① 这一集到底跑了多久？② 电费/机时成本大概多少？
改造前没有任何统计，只能靠人工掐表。

本模块提供
----------
1. **事件流水**：`output/analytics/events.jsonl`（追加写，一行一条，永不覆盖）
   每条记录：项目 / 环节 / 标签 / 耗时秒 / 单元数 / 附带元信息 / 时间戳
2. **聚合汇总**：`summarize()` 按项目与环节聚合耗时、次数、单元数
3. **成本估算**：按显存档案（8G/16G/server）对应 GPU 功耗估算电费
4. **ComfyUI 调用统计**：从 comfyui_client 的计数器读取提交次数

设计约束
--------
- 任何异常都不得打断生成流程（统计是可观测性，不是业务）；
- 追加写 + WAL 无需；单文件 jsonl 便于人工排查与离线分析；
- 成本是**估算值**（功耗按档案默认值），可被环境变量覆盖。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ⭐ 2026-10-10：同 script_generator —— 原先用 __file__ 上级硬算 output/analytics
# （= 代码目录），绕过数据根。统一从 config 取。
from config import ANALYTICS_DIR  # noqa: F401
EVENTS_PATH = os.path.join(ANALYTICS_DIR, "events.jsonl")

_LOCK = threading.Lock()

# ===================== 成本模型 =====================

# GPU 显存档案 → 整机满载功耗估算（瓦）。含 CPU/主板/风扇等其余部件。
GPU_PROFILES = {
    "8G": {"gpu": "RTX 4060 Laptop / 3060 12G", "watts_total": 260},
    "16G": {"gpu": "RTX 4080 16G", "watts_total": 480},
    "server": {"gpu": "A100 / 4090 24G+", "watts_total": 900},
}

# 电价（元/度），可用环境变量覆盖
ELECTRICITY_PRICE = float(os.getenv("MJSCXT_ELECTRICITY_PRICE", "0.6"))

# 各环节的中文名（前端展示用）
KIND_LABELS = {
    "script": "剧本生成",
    "assets": "图片资产生成",
    "storyboard": "分镜图生成",
    "video": "视频生成",
    "qc": "AI 质检",
    "tts": "配音合成",
    "mix": "音画合成",
    "upscale": "超分放大",
    "watermark": "水印处理",
    "final": "成片合成",
    "consistency": "一致性校验",
    "export": "导出",
    "other": "其他",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _profile_watts(profile: Optional[str] = None) -> int:
    """按部署档案取整机功耗；未指定则读环境变量/DEPLOY_PROFILE"""
    prof = (profile or os.getenv("DEPLOY_PROFILE") or "16G").strip()
    info = GPU_PROFILES.get(prof) or GPU_PROFILES["16G"]
    return int(info["watts_total"])


def estimate_cost_rmb(seconds: float, profile: Optional[str] = None,
                      watts: Optional[int] = None) -> dict:
    """按耗时估算电费（元）"""
    try:
        sec = max(0.0, float(seconds))
    except (TypeError, ValueError):
        sec = 0.0
    w = int(watts) if watts else _profile_watts(profile)
    kwh = sec * w / 3600.0 / 1000.0
    return {
        "seconds": round(sec, 1),
        "watts": w,
        "kwh": round(kwh, 4),
        "price_per_kwh": ELECTRICITY_PRICE,
        "cost_rmb": round(kwh * ELECTRICITY_PRICE, 4),
    }


# ===================== 事件记录 =====================

def record_event(kind: str, project: str = "", label: str = "",
                 duration_sec: float = 0.0, units: int = 0,
                 success: bool = True, meta: dict = None) -> bool:
    """追加一条统计事件（永不抛异常）"""
    try:
        os.makedirs(ANALYTICS_DIR, exist_ok=True)
        rec = {
            "ts": _now_iso(),
            "project": project or "",
            "kind": kind or "other",
            "label": label or "",
            "duration_sec": round(float(duration_sec or 0.0), 2),
            "units": int(units or 0),
            "success": bool(success),
            "meta": meta or {},
        }
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with open(EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as e:  # noqa: BLE001  统计失败不影响业务
        logger.warning(f"统计事件写入失败（忽略）：{e}")
        return False


def record_from_task(task: dict, analytics_kind: str = None) -> bool:
    """从任务记录（task_store 的任务 dict）生成一条统计事件

    耗时优先用 started_at/finished_at 差值，缺失则退回 updated_at。
    """
    try:
        kind = analytics_kind or task.get("kind") or "other"
        project = task.get("project") or ""
        start = task.get("started_at") or task.get("created_at") or ""
        end = task.get("finished_at") or task.get("updated_at") or ""
        dur = _parse_diff_seconds(start, end)
        units = 0
        total = task.get("total")
        if isinstance(total, (int, float)) and total:
            units = int(total)
        return record_event(
            kind=kind, project=project, label=task.get("label") or "",
            duration_sec=dur, units=units,
            success=(task.get("status") == "done"),
            meta={"task_id": task.get("id"), "status": task.get("status"),
                  "error": (task.get("error") or "")[:300]},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"任务统计生成失败（忽略）：{e}")
        return False


def _parse_diff_seconds(start: str, end: str) -> float:
    """解析两个 ISO 时间戳的秒差；任一侧不可解析时返回 0"""
    if not start or not end:
        return 0.0
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
        return max(0.0, (b - a).total_seconds())
    except Exception:  # noqa: BLE001
        return 0.0


# ===================== 读取与聚合 =====================

def read_events(project: str = None, kind: str = None, limit: int = 5000) -> list:
    """读取事件流水（倒序，最多 limit 条）"""
    if not os.path.isfile(EVENTS_PATH):
        return []
    out = []
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if project and rec.get("project") != project:
                    continue
                if kind and rec.get("kind") != kind:
                    continue
                out.append(rec)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"统计事件读取失败：{e}")
        return []
    out.reverse()
    return out[:max(1, int(limit))]


def _comfyui_stats() -> dict:
    """从 comfyui_client 读取调用计数（失败返回空）"""
    try:
        import comfyui_client
        return comfyui_client.get_call_stats()
    except Exception:  # noqa: BLE001
        return {}


def summarize(project: str = None, recent_limit: int = 100) -> dict:
    """聚合汇总：按环节统计 + 总耗时 + 成本估算 + ComfyUI 调用次数"""
    events = read_events(project=project, limit=100000)

    by_kind: dict = {}
    total_sec = 0.0
    total_units = 0
    failed = 0
    for e in events:
        k = e.get("kind") or "other"
        row = by_kind.setdefault(k, {
            "kind": k, "label": KIND_LABELS.get(k, k),
            "count": 0, "failed": 0, "seconds": 0.0, "units": 0,
        })
        row["count"] += 1
        sec = float(e.get("duration_sec") or 0)
        row["seconds"] += sec
        row["units"] += int(e.get("units") or 0)
        total_sec += sec
        total_units += int(e.get("units") or 0)
        if not e.get("success", True):
            row["failed"] += 1
            failed += 1

    rows = []
    for row in by_kind.values():
        row["seconds"] = round(row["seconds"], 1)
        row["minutes"] = round(row["seconds"] / 60.0, 1)
        row["avg_sec"] = round(row["seconds"] / row["count"], 1) if row["count"] else 0
        row["cost_rmb"] = estimate_cost_rmb(row["seconds"])["cost_rmb"]
        rows.append(row)
    rows.sort(key=lambda r: r["seconds"], reverse=True)

    cstats = _comfyui_stats()
    summary = {
        "project": project or "(全部项目)",
        "event_count": len(events),
        "failed_count": failed,
        "total_seconds": round(total_sec, 1),
        "total_hours": round(total_sec / 3600.0, 2),
        "total_units": total_units,
        "by_kind": rows,
        "comfyui": cstats,
        "cost": estimate_cost_rmb(total_sec),
        "deploy_profile": (os.getenv("DEPLOY_PROFILE") or "16G"),
        "generated_at": _now_iso(),
    }
    # 若某环节有 ComfyUI 提交但无耗时记录，补充提示
    summary["recent"] = events[:max(1, int(recent_limit))]
    return summary


def list_projects() -> list:
    """列出统计中出现过的项目（按最近活动时间倒序）"""
    seen = {}
    for e in read_events(limit=100000):
        p = e.get("project") or ""
        if not p:
            continue
        if p not in seen:
            seen[p] = e.get("ts")
    return [{"project": k, "last_active": v}
            for k, v in sorted(seen.items(), key=lambda kv: kv[1] or "", reverse=True)]


def reset(project: str = None) -> dict:
    """清空统计（project 为空则清空全部）。返回删除条数。"""
    if not os.path.isfile(EVENTS_PATH):
        return {"removed_events": 0, "remaining_events": 0}
    if not project:
        try:
            os.remove(EVENTS_PATH)
            return {"removed_events": -1, "remaining_events": 0}   # -1 表示整体清空
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    kept = [e for e in read_events(limit=100000) if e.get("project") != project]
    removed = 0
    try:
        with _LOCK:
            with open(EVENTS_PATH, "w", encoding="utf-8") as f:
                for e in kept:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        removed = len(read_events(limit=100000))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"removed_events": removed, "remaining_events": len(kept)}
