# -*- coding: utf-8 -*-
"""剧本读取 / 剧集统计 / 成片交付登记。

## 为什么抽（_shared 拆分第 7 步，2026-10-10）

core 模块需要这些能力却只能从 routes._shared 取 —— 与 HTTP 无关。
上移到 app/ 层后，core 侧依赖的是 app 层模块。

## 铁律

不得反向依赖 routes/*。autopilot / novel_to_script / pipeline / preview_gate /
ai_chat / project_store / style_kit 均已核对不导入 routes._shared。
"""
from __future__ import annotations

import json

import novel_to_script
import pipeline
import preview_gate
import project_store
from config import SCRIPT_DIR, VIDEOS_DIR
from upscale_client import probe_video as probe_video_info
from shared_base import _app_logger
from shared_project import _ep_dir


def _episode_video_stats(project_name: str, episode_no) -> dict:
    """该集镜头视频就绪度：剧本镜头数 vs 已落盘视频数（>1KB 才算数）"""
    script = _load_script_for(project_name, episode_no)
    shots = [s for s in (script.get('shots') or []) if isinstance(s, dict)]
    d = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
    ready = 0
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if not fn.lower().endswith('.mp4'):
                continue
            try:
                if os.path.getsize(os.path.join(d, fn)) > 1024:
                    ready += 1
            except OSError:
                continue
    return {"total": len(shots), "ready": ready, "dir": d}


def _load_legacy_flat_script(project_name: str) -> dict:
    """回退：读取旧版扁平命名的剧本（SCRIPT_DIR/<name>_<时间戳>.json）。

    仅在现行目录布局读不到剧本时调用，因此不会遮蔽正常的第N集.json。
    按修改时间倒序取第一份「含 shots」的文件，避免命中空壳/中间态产物。
    """
    try:
        names = os.listdir(SCRIPT_DIR)
    except OSError:
        return {}
    cands = []
    for fn in names:
        if not fn.lower().endswith(".json"):
            continue
        stem = fn[:-5]
        # 允许 <name>_<时间戳> 与 <name> 本身（例如「剑心初醒_兼容版」）
        if stem != project_name and not stem.startswith(project_name + "_"):
            continue
        path = os.path.join(SCRIPT_DIR, fn)
        try:
            cands.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _, path in sorted(cands, reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"遗留剧本读取失败 {path}：{e}")
            continue
        if isinstance(data, dict) and (data.get("shots") or data.get("episode_no")):
            _app_logger().info("剧本回退：%s 使用遗留扁平剧本 %s", project_name, os.path.basename(path))
            return data
    return {}


def _load_script_for(project_name: str, episode_no=None) -> dict:
    """按项目名（+可选集号）读取剧本；缺集号时取该项目第一集

    兼容两种历史布局（否则「迁移项目」会永远读不到剧本）：
      A. 现行：SCRIPT_DIR/<project_key>/第N集.json
      B. 迁移遗留：SCRIPT_DIR/<name>_<时间戳>.json（扁平，无子目录）
    遗留项目在项目索引里登记着 episode_count（例如 10），但按 A 找不到任何一集，
    于是分镜画布 / 导出 / 质检等全部读到空数据，界面显示「10 集 · 0 分镜」。
    这里在 A 落空时回退到 B，并优先取时间戳最新的一份。
    """
    key = project_store.safe_key(project_name)
    script = None
    if episode_no:
        try:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"剧本读取失败（第{episode_no}集）：{e}")
    if not script:
        try:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
        except Exception:  # noqa: BLE001
            eps = []
        if eps:
            first = eps[0]
            epno = first if isinstance(first, int) else (first.get("episode_no") or 1)
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, epno)
    if not script:
        script = _load_legacy_flat_script(project_name)
    return script or {}


def register_final_deliverable(project_name: str, episode_no, video_path: str,
                               meta: dict = None) -> dict:
    """把整集成片登记进「待验收」队列（幂等）。

    硬闸门（不满足就完全不登记，避免验收页被垃圾塞满）：
      0) 成片不存在 / < 100KB；1) 探不到时长或 < 2s。

    软闸门（镜头覆盖）：剧本镜头数 vs 已落盘镜头视频数。
    这里刻意不做「时长 >= 镜头数 x N 秒」的硬判定 —— 漫剧单镜常常不到 1s
    （实测 6 镜合并成片只有 4.46s），按时长否决会把真成片误判成半成品。
    镜头不齐时仍然登记，但在 meta 里打 `incomplete_shots` + `warning`，
    让「成品验收」页能显示「可能不完整」提醒，用户可据此打回。

    返回 {"registered": bool, "reason": str, "stats": {...}, "item": {...}}
    """
    # 铁律（2026-09-29）：预演产物**永不可交付**。这里给一个**友好拒绝**（不抛异常），
    # 让调用方能直接把原因显示给用户；同一不变量在 pipeline.record_deliverable 上还有
    # 一道硬闸门（防绕过）。
    _ok, _why = preview_gate.deliverable_ok(video_path)
    if not _ok:
        _app_logger().error("[预演拦截] 拒绝把非正式产物登记为成片（%s）：%s",
                         os.path.basename(str(video_path or "")), _why)
        return {"registered": False, "reason": _why, "stats": {}, "preview_blocked": True}
    if not video_path or not os.path.exists(video_path):
        return {"registered": False, "reason": "成片文件不存在", "stats": {}}
    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        return {"registered": False, "reason": f"成片不可读：{e}", "stats": {}}
    if size < 100 * 1024:
        return {"registered": False, "reason": f"成片过小（{size} 字节），疑似半成品",
                "stats": {"size": size}}

    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    try:
        vinfo = probe_video_info(video_path) or {}
    except Exception:  # noqa: BLE001 - 探测失败不代表成片不可用，走宽松分支
        vinfo = {}
    duration = float(vinfo.get("duration") or 0)
    if duration and duration < 2.0:
        return {"registered": False, "reason": f"成片仅 {duration:.1f}s，疑似片段",
                "stats": {"duration": duration, "size": size}}

    stats = _episode_video_stats(project_name, ep)
    stats["size"], stats["duration"] = size, duration
    total, ready = int(stats.get("total") or 0), int(stats.get("ready") or 0)
    incomplete = bool(total > 0 and ready < total)

    item_meta = dict(meta or {})
    item_meta.setdefault("source", "final")
    item_meta.setdefault("duration_sec", round(duration, 2) if duration else None)
    item_meta.setdefault("size_bytes", size)
    item_meta["shots_total"] = total
    item_meta["shots_ready"] = ready
    if incomplete:
        item_meta["incomplete_shots"] = True
        item_meta["warning"] = (f"该集剧本 {total} 镜，仅发现 {ready} 个镜头视频，"
                                f"成片可能不完整，建议核对后再验收")
    try:
        item = pipeline.record_deliverable(project_name, ep, video_path, meta=item_meta)
    except Exception as e:  # noqa: BLE001 - 登记失败不能影响出片主流程
        _app_logger().warning(f"成片登记交付物失败（{project_name} 第{ep}集）：{e}")
        return {"registered": False, "reason": f"登记失败：{e}", "stats": stats}
    if incomplete:
        _app_logger().warning(f"成片已登记但镜头疑似不全：{project_name} 第{ep}集 "
                           f"({ready}/{total}) -> {os.path.basename(video_path)}")
        return {"registered": True,
                "reason": f"已登记（镜头覆盖 {ready}/{total}，可能不完整）",
                "stats": stats, "item": item, "incomplete": True}
    _app_logger().info(f"成片已登记待验收：{project_name} 第{ep}集 -> {os.path.basename(video_path)}")
    return {"registered": True, "reason": "已登记", "stats": stats, "item": item}


