# -*- coding: utf-8 -*-
"""自动生产流水线（Pipeline）——「一章小说 → 一集可交付成片」的无人值守编排

为什么需要
----------
改造前所有环节都是**手动触发**：前端点一次「生成剧本」、再点「生成资产」、
再点「生成分镜」…… 中途任何一步断了都要人工补。这无法支撑「电脑 24 小时
自动生产、人只看最终成品」的需求。

本模块把整条链路编排成一条**流水线**，由 autopilot 守护进程驱动：

    script → tts_pre → assets → storyboard → video → upscale → final

（tts_pre = 为每个角色生成参考音色，供 H3 锁定角色音色并自生成对白；
  tts / mix 已下线 —— H3 视频自带原生音轨）

（assets 为项目级资产，只在首集前跑一次）

（2026-10-05 收敛为 **7 步**：keyframe「尾帧」步骤移出流水线 —— 2026-10-01 起
  视频只保留整集一次生成，尾帧仅 keyframe 逐镜模式消费，该步在序列里恒为
  disabled 空转。尾帧的**手动**生成入口（/api/keyframes/*，keyframe.py）不受影响。）

三个关键设计
------------
1. **幂等 + 断点续跑**
   每步先探测产物是否已存在且有效，存在即跳过。崩溃 / 重启 / 重跑都不会重复
   烧 GPU —— 这是 24 小时无人值守的基础。

2. **质检门禁 + 自动重试**
   复用既有质检链路（脚本原文覆盖率、图片/视频 AI 质检、跨镜角色一致性）。
   不达标**自动重试**（重试时换 seed 提高多样性），超过上限才升级为
   「需人工介入」。**绝不静默放行不达标产物**。

3. **失败隔离**
   单步失败只影响该集：该集标记失败并进入异常队列，其它集与后续集不受影响。

与 app.py 的关系
----------------
本模块不复制任何业务逻辑，全部通过**延迟取宿主模块**复用 app.py 里既有的
worker（`_storyboard_worker` / `_video_generate_worker` / `_dub_worker` /
`_mix_worker` …）。延迟访问（而非顶层 import）是为了避开循环导入：
app.py 在模块加载期 import 本模块，此时 app 尚未完成初始化。
"""
from __future__ import annotations

import json
import re  # 逐场超分：场次文件名 scene_NN.mp4 解析（2026-10-08）
import logging
import os
import shutil
import threading
import time
import traceback

import cancellation
import gpu_task_gate
import quality_stage  # 四层质量状态 + 哈希绑定人审（2026-09-29）
import task_lease  # 文件租约 + 心跳（2026-09-29：跨进程互斥 + 崩溃可回收）
import failure_codes  # 结构化失败码（2026-09-29：从既有文案归类，原文一字不改）
import preview_gate  # 两级生产（2026-09-29：预演产物永不可交付）

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

# ===================== 集级互斥（B-02 P0-5 并发/幂等） =====================
# 问题：autopilot 托管守护线程（autopilot.py:805）与手动 run-once（app.py:9417）
# 可能并发写同一集的同一批路径（正式目录 + scratch）。加「集级」锁
# （键 = 项目 + 集号）保证同一集任何时刻只有一个执行体；不同集可并行。
# 不可用全局锁（会让不同项目/不同集互相阻塞）。
_EP_LOCKS: dict = {}
_EP_LOCKS_GUARD = threading.Lock()
#: 集级**文件租约**（跨进程）：key -> task_lease.Lease
_EP_LEASES: dict = {}


def _episode_scope(project_name: str, episode_no: int) -> str:
    return f"episode:{project_name}#{int(episode_no)}"


def _acquire_episode_lock(project_name: str, episode_no: int) -> bool:
    """尝试获取集级锁；拿不到（另一执行体正在跑）返回 False，不阻塞。

    2026-09-29 增强（阶段三）：在原**进程内** threading.Lock 之外再加一层
    **文件租约**（task_lease）。原因：

    * 跨进程无效 —— 原实现是一张进程内字典。托管守护线程与「手动 run-once」若不在
      同一进程（或多个实例并行），两边各持一份字典，互斥形同虚设，同一集会并发写
      同一批路径；
    * 崩溃无痕 —— 进程没了字典也没了，没人能回答「这集是不是正被别人跑着」。

    两层都拿到才算成功；任一层失败就回滚另一层，绝不半持有。租约子系统故障
    （目录不可写等）会降级为「仅进程内锁」并 warning —— 不让产线因它停摆。
    """
    key = f"{project_name}#{int(episode_no)}"
    with _EP_LOCKS_GUARD:
        lk = _EP_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _EP_LOCKS[key] = lk
    if not lk.acquire(blocking=False):
        return False
    lease = None
    try:
        lease = task_lease.acquire(_episode_scope(project_name, episode_no),
                                   owner="pipeline",
                                   ttl_sec=task_lease.DEFAULT_TTL_SEC)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("集级租约获取异常（降级为仅进程内锁）：%s", e)
        lease = None
    if lease is None:
        try:
            lk.release()
        except RuntimeError:
            pass
        logger.warning("第%s集正被其他执行体运行（文件租约被占用），本次跳过：%s",
                       episode_no, project_name)
        return False
    lease.start_heartbeat()      # 长任务必备：心跳停了租约会被判过期并回收
    with _EP_LOCKS_GUARD:
        _EP_LEASES[key] = lease
    return True


def _release_episode_lock(project_name: str, episode_no: int) -> None:
    key = f"{project_name}#{int(episode_no)}"
    with _EP_LOCKS_GUARD:
        lk = _EP_LOCKS.get(key)
        lease = _EP_LEASES.pop(key, None)
    if lease is not None:
        try:
            lease.release()      # 内部停掉心跳线程；只有 token 仍是自己的才删
        except Exception as e:                               # noqa: BLE001
            logger.debug("释放集级租约失败（忽略）：%s", e)
    if lk is not None:
        try:
            lk.release()
        except RuntimeError as e:
            logger.debug("释放集级锁失败（忽略）：%s", e)


def is_episode_running(project_name: str, episode_no: int) -> bool:
    """该集当前是否有执行体（供前端/诊断查询）

    2026-09-29：除进程内锁外也认**文件租约** —— 别的进程（或崩溃前留下的）正在跑，
    这里必须如实回答 True，不能因为本进程没有锁就报「没人跑」。
    """
    key = f"{project_name}#{int(episode_no)}"
    with _EP_LOCKS_GUARD:
        lk = _EP_LOCKS.get(key)
    if lk is not None and lk.locked():
        return True
    try:
        st = task_lease.status(_episode_scope(project_name, episode_no))
        return bool(st.get("held"))
    except Exception as e:                                   # noqa: BLE001
        logger.debug("查询集级租约状态失败（按未运行处理）：%s", e)
        return False


# ===================== 集级操作日志（JSONL，一行一事件） =====================


def _episode_log_path(project_name: str, episode_no: int) -> str:
    """集级操作日志：output/autopilot/<项目>/episodes/ep{NN}_log.jsonl

    落在 autopilot 目录树下：删除项目的级联清理（project_store.project_kind_roots 的
    "autopilot" 项）会一并收走，不产生新残留；与 plan/history 同根便于运维定位。
    """
    import autopilot as _ap
    base = os.path.join(_ap._autopilot_dir(project_name), "episodes")
    return os.path.join(base, f"ep{int(episode_no):02d}_log.jsonl")


def _ep_log(project_name: str, episode_no: int, event: str, message: str,
            level: str = "info", **fields) -> None:
    """追加一条集级操作日志（JSONL 一行一事件）。全容错：日志失败绝不影响生产。

    同时镜像一行到 logging（[集日志] 前缀），人工在「运行日志」页也能看到同一份内容。
    """
    try:
        rec = {"ts": _now(),
               "project": project_name, "episode": int(episode_no),
               "event": event, "message": str(message or ""), "level": level}
        rec.update({k: v for k, v in fields.items() if v is not None})
        path = _episode_log_path(project_name, episode_no)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info("[集日志] %s#ep%s %s：%s", project_name, episode_no, event, message)
    except Exception as e:  # noqa: BLE001
        logger.debug("集日志写入失败（忽略）：%s", e)


# ===================== 步骤定义 =====================

#: 步骤顺序（键即步骤 id）
#:
#: ⭐ 2026-10-04 收敛：H3 视频自带**原生音轨**，配音改由「每角色参考音色 →
#:    H3 生成对白」承担，故**移除 `tts`（配音合成）与 `mix`（音画对齐混音）**。
#: ⭐ 2026-10-05 收敛为 **7 步**：`keyframe`（尾帧）移出流水线 —— 视频只保留
#:    整集一次生成后，尾帧在序列里恒为 disabled 空转；手动尾帧入口不废。
#: ``tts_pre`` 新语义：剧本后为**每个角色**生成一段参考音色音频（只做这件事，
#:    **不再逐句合成整集配音、不再回填 `shot.duration`**）——参考音色经
#:    `voice_bank` 落盘，视频生成时以 `audioMode=generate`（`global.refAudios`）
#:    锁定角色音色、对白由 H3 自生成。
#: `upscale` 紧跟 `video`：对**集级原片**（video 步产出的整集视频）超分；
#:    `final` 优先消费超分产物，故 `final` 落在最后一步。
STEP_SEQUENCE = ("script", "tts_pre", "assets", "storyboard", "video",
                 "upscale", "final")

STEP_LABELS = {
    "script": "剧本生成",
    "tts_pre": "配音先行（角色参考音色）",
    "assets": "资产（角色/物品/场景）",
    "storyboard": "分镜图",
    "video": "视频生成",
    "upscale": "超分（FlashVSR）",
    "final": "成片合成",
    # 历史回放兜底（旧任务状态里可能出现；不再在 STEP_SEQUENCE 中）
    "keyframe": "尾帧（已下线）",
}

#: 需要「质检门禁」的步骤（不达标必须重试，不允许静默通过）
GATED_STEPS = ("script", "storyboard", "video")

#: 单次占用的 GPU 重任务步骤（守护进程据此做资源互斥，避免抢显存）
GPU_STEPS = ("storyboard", "video", "upscale")

DEFAULT_CONFIG = {
    # ---- 输入 ----
    "novel_id": "",                # 小说 id（必填）
    "project_key": "",             # 项目键（缺省由 novel_id 推导）
    "style": "",                   # 总控 AI 敲定的风格描述
    "target_shots": 12,            # 每集目标镜头数
    # ---- 范围 ----
    "episodes": "all",             # "all" 或 [1,2,3]
    "overwrite_script": False,     # 是否覆盖已存在的剧本
    # ---- 各环节开关 ----
    "enable_assets": True,
    "enable_video": True,
    "enable_final": True,
    # TTS 总开关：控制 `tts_pre` 里「每角色参考音色」的实际合成。
    "enable_tts": True,
    # 混音默认关闭（2026-10-04，10→8 步）：H3 原生音轨生效后，混音会与它冲突 ——
    # 有分离音效轨时 H3 人声会被当 vocals 剔掉、无则双重人声；且 `mix` 已不在 STEP_SEQUENCE。
    "enable_mix": False,
    # 超分（FlashVSR）：默认开启。⚠️ 这一步是画质增强而非出片必需环节，
    # step_upscale 全程 fail-open——环境不可用或执行失败一律记 skipped，
    # 绝不把已经跑通的成片拖成失败。
    "enable_upscale": True,
    "upscale_scale": 2,            # 超分倍率，FlashVSR 支持 2 / 3 / 4
        # 逐场次先超分再拼接（2026-10-08）：True = 各场次独立超分→流拼接成整集；False = 直接超分整集原片
        "upscale_per_scene": True,
    # ⭐「配音先行」（2026-10-04 新语义）：默认开启。True = 剧本后为**每个角色**生成
    # 一段参考音色音频，供 H3 以 `audioMode=generate` 锁定角色音色并自生成对白。
    # （不再逐句合成整集配音、不再回填 shot.duration。）仅在调试或无需参考音色时关闭。
    "enable_tts_pre": True,
    "video_mode": "episode",       # 2026-10-01 起只保留整集一次生成（唯一合法值）
    # ---- 质量阈值 ----
    "coverage_min_percent": 95.0,      # 原文覆盖率下限
    "consistency_min_score": 80,       # 跨镜一致性分数下限
    "require_consistency": True,
    "step_max_retries": 2,             # 单步失败重试次数上限
    # ---- 运行时 ----
    "auto_repair": True,               # 失败自动补救（换 seed / 重生成）
    "seed": None,
    "timeout_per_segment": 900,        # 单段/单镜任务超时（秒；此前不在册会被静默丢弃）
}


def normalize_config(raw: dict, default_project_key: str = "") -> dict:
    """把外部传入的配置补齐为完整可用配置（并做类型收敛）"""
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (raw or {}).items():
        if k in cfg or k in ("novel_id", "project_key"):
            cfg[k] = v
    # 类型收敛（配置可能来自前端 JSON，类型不可信）
    for k in ("target_shots", "consistency_min_score", "step_max_retries"):
        try:
            cfg[k] = int(cfg.get(k))
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CONFIG[k]
    # 超分倍率：非法值一律回落 2（FlashVSR 只支持 2/3/4，传错会让该步直接跳过）
    try:
        cfg["upscale_scale"] = int(cfg.get("upscale_scale"))
    except (TypeError, ValueError):
        cfg["upscale_scale"] = DEFAULT_CONFIG["upscale_scale"]
    if cfg["upscale_scale"] not in (2, 3, 4):
        cfg["upscale_scale"] = DEFAULT_CONFIG["upscale_scale"]
    try:
        cfg["coverage_min_percent"] = float(cfg.get("coverage_min_percent"))
    except (TypeError, ValueError):
        cfg["coverage_min_percent"] = DEFAULT_CONFIG["coverage_min_percent"]
    for k in ("enable_assets", "enable_video", "enable_final",
              "enable_tts", "enable_mix", "enable_tts_pre",
              "enable_upscale", "require_consistency",
              "auto_repair", "overwrite_script",
               "upscale_per_scene"):
        cfg[k] = bool(cfg.get(k))
    # 2026-10-01：只保留整集一次生成（per_shot / keyframe 废弃，2026-10-05 起
    # keyframe 步骤也已移出 STEP_SEQUENCE）
    cfg["video_mode"] = "episode"
    if not cfg.get("project_key"):
        cfg["project_key"] = default_project_key or cfg.get("novel_id") or ""
    return cfg


def assert_mode_contract(cfg: dict) -> None:
    """P0-6：「模式 × 后续步骤产物期望」一致性前置断言（纯读配置，不写盘、不发请求）。

    video_mode 已收敛为唯一合法值 `episode`（2026-10-01），这里保留断言作为
    兜底：拦截绕过 normalize_config、手工拼配置的调用路径。

    - episode 模式成片 = 整集视频（step_final 直接 copy2 整集片）：
      启用成片合成（final）时必须同时启用视频生成（video），否则没有整集片可采。

    断言失败抛 PipelineError（进入步骤循环前调用，属配置错误而非步骤运行时故障，
    不触发步骤级重试）。
    """
    mode = cfg.get("video_mode") or "episode"
    if mode != "episode":
        raise PipelineError(
            f"视频生成模式「{mode}」已废弃（2026-10-01 起仅支持 episode 整集一次生成），"
            "请检查托管计划配置")
    video_on = bool(cfg.get("enable_video"))
    final_on = bool(cfg.get("enable_final"))
    if final_on and not video_on:
        raise PipelineError("整集模式的成片需要整集视频，但视频生成步骤被禁用；请启用视频生成，或关闭成片合成")


# ===================== 异常 =====================


class PipelineError(RuntimeError):
    """流水线一般性错误（会触发重试）"""


class NeedsHumanError(PipelineError):
    """重试已耗尽 / 环境性故障，需要人工介入（不再重试）"""


# ===================== 宿主访问 =====================


def _A():
    """取宿主模块 app（延迟访问，避开循环导入）"""
    import sys as _sys
    mod = _sys.modules.get("app")
    if mod is None:
        raise PipelineError("宿主模块 app 尚未加载，无法运行流水线")
    return mod


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ===================== 通用工具 =====================


def _run_task_worker(worker, args, registry_name: str, lock_name: str,
                     init: dict = None, prefix: str = "pipe") -> dict:
    """在线程内直接执行一个「写任务字典」的既有 worker，返回其最终状态

    既有 worker（`_storyboard_worker` / `_dub_worker` / `_mix_worker` …）都以
    `task_id` 作为第一个参数，并把进度与结果写进各自的全局任务字典。流水线
    为每次调用生成一个内部 task_id，预置初始状态，调用后读取最终状态并把该
    条目清掉（避免污染前端可见的任务列表）。
    """
    A = _A()
    registry = getattr(A, registry_name)
    lock = getattr(A, lock_name)
    tid = f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"
    state = {"status": "running", "progress": 0, "phase": "准备中",
             "results": [], "pipeline": True, "created_at": _now()}
    if init:
        state.update(init)
    with lock:
        registry[tid] = state
    err = ""
    try:
        worker(tid, *args)
    except cancellation.Cancelled:
        # ⚠️ 中止信号必须穿透：否则会被下面归一化成 status=failed，
        # 再被 _run_step_with_retry 当成「步骤失败」重试 —— 用户点暂停后
        # 反而触发整步（含 84 段 H3 整片）重新提交。这里清掉内部任务条目后原样上抛。
        with lock:
            registry.pop(tid, None)
        raise
    except Exception as e:  # noqa: BLE001  单步异常向上抛，由重试层决定
        err = f"{type(e).__name__}: {e}"
        logger.error("流水线 worker 异常（%s）：%s\n%s", prefix, err, traceback.format_exc())
    with lock:
        final = dict(registry.get(tid) or {})
        registry.pop(tid, None)      # 内部任务不留在 UI 列表
    if err and not final.get("error"):
        final["error"] = err
        final.setdefault("status", "failed")
    return final


def _outcome_from_task(final: dict, what: str) -> dict:
    """把既有 worker 的任务状态归一化成流水线可判定的结果"""
    status = (final or {}).get("status") or "failed"
    results = (final or {}).get("results") or []
    ok_cnt = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
    # 质检阻断计数：资产/分镜/视频 worker 都会打 qc_blocked
    blocked = sum(1 for r in results if isinstance(r, dict) and r.get("qc_blocked"))
    # 「全部被合法跳过」不是失败。
    # 最典型的场景：物品卡里只有临时道具 —— _generate_asset_task 对
    # importance=临时 的物体**有意不生成参考图**，results 里只留 status=skipped，
    # 于是 ok_cnt=0。历史实现按「results 非空且无成功」直接判失败，导致整集在
    # assets 步骤反复失败并挂起等人工介入（实测：雨夜归人 第2集只有「深色长柄伞」
    # 一个临时道具，连续 2 次失败被标记需人工处理）。
    skipped_cnt = sum(1 for r in results if isinstance(r, dict)
                      and (str(r.get("status") or "") == "skipped" or r.get("skipped")))
    all_skipped = bool(results) and skipped_cnt == len(results)
    if status == "failed" or (results and not ok_cnt and not all_skipped):
        return {"ok": False, "blocked": bool(blocked), "count": ok_cnt,
                "error": (final or {}).get("error") or f"{what}失败",
                "detail": final}
    return {"ok": True, "blocked": bool(blocked), "count": ok_cnt, "error": "", "detail": final}


# ===================== 产物探测（幂等 / 断点续跑判据） =====================


def _nonempty(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _playable(path: str) -> bool:
    """G7：视频产物「可播放性」判据（取代「文件存在 + size>0」）。

    一次超时/被杀留下的半截 mp4 往往远超 1KB，旧判据 `_nonempty` 会把半截文件
    当「已完成」→ 后续步骤永久跳过、坏成片进验收。现在要求 ffprobe 能读出
    **视频流且 duration>0** 才算就绪。

    降级策略：ffprobe 二进制缺失时退回「存在 + 非空」（不能让工具链不齐导致
    整条流水线误判全部未就绪而重烧全部镜头）；文件存在但 ffprobe 解析失败
    / 无视频流 / 时长为 0 → 判未就绪（会重跑）。
    仅用于 .mp4 等视频产物；图片类产物（分镜/尾帧 .png）仍用 `_nonempty`。
    """
    if not _nonempty(path):
        return False
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("ffprobe"):
        return True  # ffprobe 不可用：降级为「存在 + 非空」，避免误伤全流水线
    try:
        r = _sp.run(["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_type:format=duration", "-of", "json", path],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if r.returncode != 0:
            return False
        d = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001 - 解析失败 = 半截/损坏，判未就绪
        return False
    streams = d.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    try:
        dur = float((d.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return bool(has_video and dur > 0)


def probe_script(ctx) -> bool:
    A = _A()
    p = A.novel_to_script.episode_script_path(A.SCRIPT_DIR, ctx["project_key"], ctx["episode_no"])
    return _nonempty(p)


def probe_assets(ctx) -> dict:
    """资产就绪判据：剧本里的每个角色/物品/场景都有非空基础图"""
    A = _A()
    script = ctx.get("script") or {}
    out = {}
    for kind, dir_key, script_key in (
        ("character", "CHARACTERS_DIR", "characters"),
        ("item", "ITEMS_DIR", "items"),
        ("scene", "SCENES_DIR", "scenes"),
    ):
        base = os.path.join(getattr(A, dir_key), ctx["project_name"])
        names = [str(a.get("name") or "").strip()
                 for a in (script.get(script_key) or []) if isinstance(a, dict)]
        names = [n for n in names if n]
        ready, missing = 0, []
        for n in names:
            d = os.path.join(base, n)
            hit = None
            for cand in ("front.png", "base.png"):
                if _nonempty(os.path.join(d, cand)):
                    hit = cand
                    break
            if not hit:
                # 目录扫描兜底：任何一张非空图都算就绪
                if os.path.isdir(d):
                    for fn in os.listdir(d):
                        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) \
                                and _nonempty(os.path.join(d, fn)):
                            hit = fn
                            break
            if hit:
                ready += 1
            else:
                missing.append(n)
        out[kind] = {"total": len(names), "ready": ready, "missing": missing,
                     "done": bool(names) and not missing}
    # 顶层 done：三类资产都齐全才算就绪（step_assets 据此整步跳过，避免每天重跑都
    # 全量重生成资产 —— 这是「断点续跑」在资产步骤上的落点）
    out["done"] = all(bool(v.get("done")) for v in out.values() if isinstance(v, dict))
    out["kinds_ready"] = sum(1 for v in out.values()
                             if isinstance(v, dict) and v.get("done"))
    out["kinds_total"] = 3
    return out


def probe_storyboard(ctx) -> dict:
    A = _A()
    d = A._ep_dir(os.path.join(A.STORYBOARDS_DIR, ctx["project_name"]), ctx["episode_no"])
    shots = (ctx.get("script") or {}).get("shots") or []
    missing = []
    for i, s in enumerate(shots):
        seq = A._shot_seq(s.get("shot_id", i + 1), i + 1)
        if not _nonempty(os.path.join(d, f"shot_{seq:02d}.png")):
            missing.append(s.get("shot_id", i + 1))
    return {"total": len(shots), "ready": len(shots) - len(missing),
            "missing": missing, "done": bool(shots) and not missing, "dir": d}


# probe_keyframe 已随 keyframe 步骤移出流水线删除（2026-10-05）；手动尾帧链路的
# 就绪判定在 keyframe.py / app.py 的 /api/keyframes/* 内，与此处无关。


def probe_video(ctx) -> dict:
    A = _A()
    d = A._ep_dir(os.path.join(A.VIDEOS_DIR, ctx["project_name"]), ctx["episode_no"])
    # 2026-10-01 起只有整集模式（episode）：整集视频 = <tag>_full.mp4（历史命名兼容
    # episode_full.mp4）。逐镜 shot_NN.mp4 探测分支已随 per_shot/keyframe 模式废弃删除。
    tag = ctx.get("episode_tag") or f"ep{ctx['episode_no']:02d}"
    hit = None
    for cand in (f"{tag}_full.mp4", "episode_full.mp4"):
        if _playable(os.path.join(d, cand)):
            hit = cand
            break
    return {"total": 1, "ready": 1 if hit else 0, "missing": [] if hit else ["整集"],
            "done": bool(hit), "dir": d, "file": os.path.join(d, hit) if hit else ""}


def final_path(ctx) -> str:
    A = _A()
    return os.path.join(A.FINAL_DIR, ctx["project_name"], f"ep{ctx['episode_no']:02d}_final.mp4")


def _deliverable_review(ctx) -> dict:
    """读取该集成片在 deliverables.json 里登记的 review 状态（找不到返回 {}）。

    A3（2026-09-23 收口）：原实现是裸 ``open + json.load`` + ``except: return {}``
    （fail-open）。deliverables.json 一旦损坏就被判成「没有 review 记录」→ 下面
    :func:`probe_final` 的 ``review == "rejected"`` 判定失效 → **被打回的成片会被当成
    已完成、不再重做**（同函数族的 `_reset_deliverable_review` 早已迁 `read_json_strict`，
    只有这个只读口漏了）。

    现改走 :func:`read_json_strict`（缺失→{}；损坏→`.bak` 或抛错）；抛错时**响亮降级**
    （记 error 后返回 {}）—— 本函数是只读视图、从不写回，不能把整集生产打挂；
    这里刻意不 fail-loud 上抛：调用点在断点续跑判定链上，抛错会中断整集生产，
    而「不知道 review」的后果仅是「可能少重做一集」，两害相权取其轻。
    """
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot",
                            ctx["project_name"], "deliverables.json")
    try:
        data = read_json_strict(idx_path, {}) or {}
    except (ValueError, OSError) as e:
        logger.error("读取成片验收记录失败（deliverables.json 损坏且无可用 .bak，项目 %s）：%s"
                     "—— 本次按「无 review 记录」处理，被打回的成片可能被跳过重做",
                     ctx["project_name"], e)
        return {}
    return (data.get("items") or {}).get(str(int(ctx["episode_no"]))) or {}


def probe_final(ctx) -> dict:
    p = final_path(ctx)
    done = _playable(p)
    # B-03 P0-4：成片被打回（review= rejected）时，即便磁盘上成片仍可播放，
    # 也不能走幂等短路（原 done=done 会让 step_final 直接跳过、review 永不复位、
    # 最终误报「状态同步异常」）。打回 = 该集需要重做 → 让 step_final 真正重跑。
    review = _deliverable_review(ctx).get("review")
    if done and review == "rejected":
        done = False
    return {"total": 1, "ready": 1 if done else 0, "done": done, "file": p,
            "review": review}


def _reset_deliverable_review(ctx, review: str, note: str = "") -> None:
    """B-03 P0-4：成片重做后把 deliverables.json 里该集的 review 复位。

    失败不阻断流水线（review 复位是「体验」问题，不影响成片本身）。
    """
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot",
                            ctx["project_name"], "deliverables.json")
    # A-4：损坏文件不再静默当作空索引（否则下面「读改写」会用空 items 覆盖整个交付物索引）
    try:
        data = read_json_strict(idx_path, {}) or {}
    except (ValueError, OSError) as e:
        # 本函数「失败不阻断流水线」是既有契约（review 复位只是体验问题），
        # 故在**此处**显式降级：记 error 后放弃本次复位，绝不写回任何内容。
        logger.error("复位 review 失败（deliverables.json 损坏且无可用 .bak，项目 %s）：%s",
                     ctx["project_name"], e)
        return
    item = (data.get("items") or {}).get(str(int(ctx["episode_no"])))
    if not isinstance(item, dict):
        return
    item["review"] = review
    item["review_note"] = note
    item["reviewed_at"] = _now()
    try:
        atomic_write_json(idx_path, data)
        logger.info("第%s集 成片重做后 review 复位为 %s（项目 %s）",
                    int(ctx["episode_no"]), review, ctx["project_name"])
    except Exception as e:  # noqa: BLE001
        logger.warning("复位 review 失败（写 deliverables.json 异常，项目 %s）：%s",
                       ctx["project_name"], e)


def dub_manifest_path(ctx) -> str:
    A = _A()
    return os.path.join(A.DUB_DIR, ctx["project_name"],
                        f"ep{ctx['episode_no']:02d}_dub_manifest.json")


def probe_tts(ctx) -> dict:
    p = dub_manifest_path(ctx)
    if not _nonempty(p):
        return {"total": 0, "ready": 0, "done": False, "file": p}

_SCENE_FILE_RE = re.compile(r"^scene_(\d+)\.mp4$")


def _scene_segments(ctx) -> list:
    """列出该集按场次生成、且**可播放**的视频片段（按场号数字排序）。

    与 app._ep_dir 同口径（第 1 集平铺、第 2 集起 epNN/）；过滤掉已拼接的整集
    *_full.mp4 与半截文件（_playable 判 ffprobe 可读到视频流且时长>0，ffprobe 缺失时
    降级为存在+非空），确保「全部超分成功」的拼接输入都合法。

    返回 [] = 该集没有逐场片段（非按场生成 / 尚未生成）→ 调用方回退整集原片路径。
    """
    A = _A()
    d = A._ep_dir(os.path.join(A.VIDEOS_DIR, ctx["project_name"]), ctx["episode_no"])
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        m = _SCENE_FILE_RE.match(n)
        if not m:
            continue
        p = os.path.join(d, n)
        if not _playable(p):
            logger.warning("逐场超分：场次片段不可播放（半截/损坏），跳过：%s", p)
            continue
        out.append((int(m.group(1)), p))
    return [p for _, p in sorted(out)]


def _upscale_scene_once(upscaler, src_path, dst_path, project_name, scale, progress_cb) -> dict:
    """把单场超分产物（带时间戳原文件名）归档到确定性路径 dst_path，返回 {output_path}。

    与整集路径同样的归档契约：源文件（UPSCALE_DIR/<project>/ 下带时间戳）拷贝到确定性
    路径；dst 已存在且非空时直接复用（断点续跑，幂等）；拷贝失败返回 {}。
    """
    res = upscaler.upscale(
        src_path, project_name=project_name, scale=scale,
        # 与整集路径一致：源片自带 H3 原生音轨，显式挂音轨，避免超分产物无声
        attach_audio=True,
        progress_cb=progress_cb,
    )
    produced = (res or {}).get("output_path") or ""
    if not _nonempty(produced):
        return {}
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    if os.path.abspath(produced) != os.path.abspath(dst_path):
        shutil.copy2(produced, dst_path)
    if not _nonempty(dst_path):
        return {}
    return {"output_path": dst_path}


def _try_per_scene_upscale(ctx, scene_srcs, out_path) -> bool:
    """逐场次独立超分 → 流拼接成整集超分产物（out_path = 确定性 epNN_upscaled.mp4）。

    全部成功且拼接可播放才返回 True（调用方采用）；任一场失败 / 拼接失败返回 False
    （调用方回落整集原片超分）。已超分场次直接复用（断点续跑）。
    """
    A = _A()
    scale = int(ctx["config"].get("upscale_scale") or 2)
    if scale not in (2, 3, 4):
        scale = 2
    total = len(scene_srcs)
    ep_no = ctx["episode_no"]
    project = ctx["project_name"]
    try:
        import upscale_client
    except Exception as e:  # noqa: BLE001
        logger.warning("逐场超分：超分模块不可用，回落整集路径：%s", e)
        return False

    _upscaler = upscale_client.VideoUpscaler()
    _up_tid = f"pipe_up_ps_{int(time.time() * 1000)}"
    _upscaler.current_task_id = _up_tid
    up_scene_files = []
    try:
        with gpu_task_gate.run_gpu_task(_up_tid, "托管·逐场超分"):
            for i, src_s in enumerate(scene_srcs):
                sn = int(re.match(_SCENE_FILE_RE, os.path.basename(src_s)).group(1))
                dst_s = os.path.join(os.path.dirname(out_path),
                                     f"ep{ep_no:02d}_scene{sn:02d}_upscaled.mp4")
                if _playable(dst_s):
                    logger.info("逐场超分：第 %d/%d 场（scene_%02d）已有产物，复用",
                                i + 1, total, sn)
                else:
                    ctx["progress"](f"超分 场次 {i + 1}/{total}（FlashVSR {scale}x）…",
                                    84, phase="upscale")
                    # ⚠️ 2026-10-10 修复（日志显示「超分 45：…」的根因）：
                    #    upscale_client 的进度回调签名是 progress_cb(msg, pct) —— **两个参数**；
                    #    原 lambda 写成 (m, _tag=f"scene{sn:02d}")，于是 pct 落进了 _tag，
                    #    日志变成「超分 45：超分执行中…」（45 是百分比，不是场次标签），
                    #    场次标识被吞掉，排查时完全看不出在超分哪一场。
                    #    现在显式接收 pct 并拼进消息；进度值仍用 84（整集步骤级进度，
                    #    不把场内百分比写进整集进度条以免回退）。
                    def _scene_up_prog(m, pct=None, _tag=f"scene{sn:02d}"):
                        try:
                            _p = int(float(pct))
                            _head = f"超分 {_tag}（{_p}%）"
                        except (TypeError, ValueError):
                            _head = f"超分 {_tag}"
                        ctx["progress"](f"{_head}：{m}", 84, phase="upscale")

                    _r = _upscale_scene_once(
                        _upscaler, src_s, dst_s, project, scale, _scene_up_prog)
                    if not _r:
                        logger.warning("逐场超分：第 %d 场（%s）失败，回落整集超分",
                                       sn, os.path.basename(src_s))
                        return False
                up_scene_files.append(dst_s)
    except Exception as e:  # noqa: BLE001
        logger.warning("逐场超分：异常，回落整集超分：%s", e, exc_info=True)
        return False

    # 全部场次超分完成 → 流拼接成整集超分
    tmp_concat = os.path.join(os.path.dirname(out_path),
                              f"_per_scene_concat_ep{ep_no:02d}.mp4")
    cv = A.video_processor.concat_videos(up_scene_files, tmp_concat,
                                         caller="pipeline.step_upscale.per_scene")
    if not cv or not _playable(cv):
        logger.warning("逐场超分：拼接失败或拼接产物不可播放，回落整集超分")
        return False
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.abspath(cv) != os.path.abspath(out_path):
        try:
            shutil.copy2(cv, out_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("逐场超分：归档失败：%s", e)
            return False
    if not _playable(out_path):
        logger.warning("逐场超分：归档后不可播放，回落整集超分")
        return False
    logger.info("逐场超分→拼接完成：%s（%d 场，%d KB）",
                out_path, len(up_scene_files), os.path.getsize(out_path) // 1024)
    return True

    try:
        with open(p, "r", encoding="utf-8") as f:
            mf = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        # B-08 P1-10：manifest 损坏/解析失败 → fail-loud（不再静默 done:False），
        # 否则 TTS 步骤会误判「未就绪」而重烧整集配音。
        logger.warning("配音 manifest 解析失败（项目 %s 第 %s 集）：%s",
                       ctx["project_name"], int(ctx["episode_no"]), e)
        return {"total": 0, "ready": 0, "done": False, "file": p,
                "error": f"manifest 损坏或不可解析：{e}"}
    lines = [l for l in (mf.get("lines") or []) if isinstance(l, dict)]
    ok = [l for l in lines if l.get("ok") and _nonempty(l.get("out_path") or "")]
    return {"total": len(lines), "ready": len(ok),
            "done": bool(lines) and len(ok) == len(lines), "file": p,
            "missing": [l.get("line_id") for l in lines if l not in ok]}


def mix_output_path(ctx) -> str:
    """混音产物路径（流水线固定命名，便于幂等探测）"""
    A = _A()
    return os.path.join(A.mix_out_dir(ctx["project_name"]),
                        f"ep{ctx['episode_no']:02d}_dubbed.mp4")


def probe_mix(ctx) -> dict:
    p = mix_output_path(ctx)
    return {"total": 1, "ready": 1 if _playable(p) else 0, "done": _playable(p), "file": p}


def upscale_path(ctx) -> str:
    """超分产物路径（流水线固定命名，便于幂等探测与「成品验收」直接引用）

    VideoUpscaler 自身产出的文件名带时间戳（不可幂等），因此流水线会把结果
    归档到该确定性路径，重跑时 probe 命中即跳过。
    """
    A = _A()
    return os.path.join(A.UPSCALE_DIR, ctx["project_name"],
                        f"ep{ctx['episode_no']:02d}_upscaled.mp4")


def probe_upscale(ctx) -> dict:
    p = upscale_path(ctx)
    return {"total": 1, "ready": 1 if _playable(p) else 0, "done": _playable(p), "file": p}


PROBES = {
    "script": probe_script,
    "assets": probe_assets,
    "storyboard": probe_storyboard,
    "video": probe_video,
    "final": probe_final,
    "tts": probe_tts,
    "mix": probe_mix,
    "upscale": probe_upscale,
}


# ===================== 步骤实现 =====================
# 每个步骤函数返回 dict：{"ok": bool, "skipped": bool, "blocked": bool,
#                          "error": str, "detail": {...}, "artifact": str}


def step_script(ctx) -> dict:
    """章节正文 → 结构化剧本（含原文覆盖率守门 + 跨集连贯性编排）"""
    A = _A()
    if probe_script(ctx) and not ctx["config"].get("overwrite_script"):
        return {"ok": True, "skipped": True, "artifact": _script_path(ctx),
                "detail": {"note": "剧本已存在，跳过（断点续跑）"}}

    client = A._current_llm_client()
    if not client or not getattr(client, "configured", False):
        raise NeedsHumanError("尚未配置文本模型接口（AI 设置 → 文本分析），无法生成剧本")

    text = A.read_novel_text(A.NOVELS_DIR, ctx["novel_meta"]["novel_id"])
    if not text:
        raise NeedsHumanError("小说正文为空，无法生成剧本")

    chapter = ctx["chapter"]
    cfg = ctx["config"]

    # ⚠️ phase 契约（2026-10-06）：ctx["progress"] 的 phase 只允许「当前步骤名」或
    # 「当前步骤名:子阶段」。continuity / novel_to_script 的内部阶段名必须加
    # "script:" 前缀后才能透传 —— 裸传与流水线步骤名（STEP_SEQUENCE）撞车的名字，
    # 会被托管 autopilot._cb 的 base 归因当成「该步骤已完成」。
    # continuity.convert_chapter_with_continuity 内 report(...) 的全部 phase 取值：
    #   assets / context / state / validate / rewrite / coverage / consistency / done
    #   （"assets"＝加载项目级设定库，与流水线步骤 assets 撞车）；
    # novel_to_script.convert_chapter_to_script（continuity 裸转发同一 progress_cb）：
    #   prepare / outline / bible / shots / done。
    # 2026-10-06 实录：此前裸传 phase，剧本步骤 4% 时托管按 base="assets" 归因，把
    # script/tts_pre/assets 三步提前标成已完成（前端显示「资产已完成 3角色/3物品/8场景」
    # 而资产面板为 0、ComfyUI 零任务 —— 那是剧本里声明的数量）。
    def _cb(phase, cur, tot, msg, pct):
        ctx["progress"](f"第{ctx['episode_no']}集剧本：{msg}", 4 + int((pct or 0) * 0.14),
                        phase=f"script:{phase}")

    # 文学剧本改写稿：用作 coverage 与 script_consistency 比对基准（若存在）
    screenplay_text = cfg.get("screenplay_text")
    # ⚠️ 生成输入必须同步替换为文学剧本（2026-10-06 实录 bug）：continuity 是从
    # novel_text 里按 chapter.start/end 切片的（novel_text[start:end]）。run_episode
    # 已把 chapter 覆盖成 {start:0, end:len(剧本)}，但 novel_text 若还是**小说全文**，
    # 切出来的就是「小说开头 len(剧本) 字」——第 1 集碰巧近似第 1 章正文，第 2 集起
    # 就是完全错误的文本，且与 coverage 基准（文学剧本）系统性错位 → 补生成/修复
    # 回路高频触发、耗时翻倍。手动路由（app._episodes_worker 的 _conv_text）一直是
    # 「文本+区间成对替换」的正确口径，这里对齐它。
    if screenplay_text:
        text = screenplay_text

    conv = A.continuity.convert_chapter_with_continuity(
        client, ctx["novel_meta"], text, chapter, ctx["project_key"], A.CONTINUITY_DIR,
        style=cfg.get("style") or "", target_shots=int(cfg.get("target_shots") or 12),
        episode_no=ctx["episode_no"], save_dir=A.SCRIPT_DIR, progress_cb=_cb,
        screenplay_text=screenplay_text,
    )
    script = conv.get("script") or {}
    cov = conv.get("coverage") or {}
    val = conv.get("validation") or {}
    path = (conv.get("script_path")
            or A.novel_to_script.save_episode_script(
                script, A.SCRIPT_DIR, ctx["project_key"], ctx["episode_no"], ctx["project_key"]))

    detail = {
        "script_path": path,
        "shots": len(script.get("shots") or []),
        "characters": len(script.get("characters") or []),
        "items": len(script.get("items") or []),
        "scenes": len(script.get("scenes") or []),
        "coverage_percent": cov.get("coverage_percent"),
        "coverage_passed": cov.get("passed"),
        "continuity_score": (script.get("metadata") or {}).get("continuity", {}).get("validation_score"),
        "continuity_issues": len(val.get("issues") or []),
        "continuity_rewrite": bool((conv.get("rewrite") or {}).get("triggered")),
    }
    # 覆盖率门禁：不达标即判失败，交由重试层处理（绝不静默放行）
    floor = float(cfg.get("coverage_min_percent") or 0)
    pct = cov.get("coverage_percent")
    if floor > 0 and isinstance(pct, (int, float)) and pct < floor:
        return {"ok": False, "artifact": path, "detail": detail,
                "error": f"原文覆盖率 {pct}% 低于阈值 {floor}%（{len(cov.get('missing') or [])} 处遗漏）"}
    ctx["script"] = script
    return {"ok": True, "artifact": path, "detail": detail}


def _script_path(ctx) -> str:
    A = _A()
    return A.novel_to_script.episode_script_path(A.SCRIPT_DIR, ctx["project_key"], ctx["episode_no"])


def step_tts_pre(ctx) -> dict:
    """配音先行：剧本后为**每个角色**生成一段参考音色音频（2026-10-04 新语义）

    为什么是「每角色参考音色」而不是「逐句配音」
    --------------------------------------------
    H3 视频**自带原生音轨**：视频生成时以 `audioMode=generate` + 公共参考音色
    （`global.refAudios`）锁定角色音色，对白由 H3 按画面/提示词**自生成**。
    因此「剧本后先逐句 TTS 整集配音」不再需要 —— 那是旧「画面先、配音后」时序的产物。
    本步只需把**每个角色**的参考音色备好，供后续视频生成捡取。

    产物落点
    --------
    参考音色由 `app._ensure_voice_bank_refs` 经 `save_voice_bank_ref` 落到
    ``<DUB_DIR>/<项目>/voice_bank/<角色>/ref.wav``；视频生成时由
    `app._h3_common_ref_audios` 捡取为 H3 的公共参考音频。

    幂等与回退
    ----------
    - ``enable_tts_pre=False`` 整步跳过；``enable_tts=False`` 跳过（不合成参考音色）。
    - 剧本无角色且无镜头 → 跳过。
    - 本步是**纯增强**：任何异常（TTS / 磁盘 / 网络）只告警并返回 ``skipped``，
      绝不把整集拖失败（沿用旧的 fail-open 精神）。
    """
    cfg = ctx["config"]
    if not cfg.get("enable_tts_pre"):
        return {"ok": True, "skipped": True,
                "detail": {"note": "配音先行已关闭（enable_tts_pre=False）"}}
    if not cfg.get("enable_tts"):
        return {"ok": True, "skipped": True,
                "detail": {"note": "TTS 已关闭（enable_tts=False），跳过参考音色生成"}}

    # 该集是否有可处理的内容：无角色且无镜头则整个前置步骤无意义，直接放行
    script = ctx.get("script") or {}
    if not script:
        script = _load_script_into_ctx(ctx)
        ctx["script"] = script
    if not (script.get("characters") or []) and not (script.get("shots") or []):
        return {"ok": True, "skipped": True,
                "detail": {"note": "剧本无角色与镜头，跳过参考音色生成"}}

    A = _A()
    try:
        # 项目全量角色（角色资产索引的键即角色名）→ 交给既有实现逐角色补参考音色。
        # 传 common=[] 是因为「已确认的公共池」与本步无关：本步要覆盖**全部**角色。
        char_idx = A._build_asset_index(script.get("characters") or [],
                                        ctx["project_name"], "character")
        # 2026-10-05：TTS 环境自检 —— 不可用时直接返回精确跳过原因，
        # 不再静默跑一遍全部合成失败（用户会以为「配音完成了」其实什么都没有）。
        try:
            _tts_env = A.tts_env_check()
        except AttributeError:
            _tts_env = {"available": True, "reasons": []}
        if not _tts_env.get("available"):
            return {"ok": True, "skipped": True,
                    "detail": {"note": "TTS 环境不可用，跳过参考音色生成：" + "；".join(_tts_env.get("reasons") or []),
                               "env_available": False}}
        A._ensure_voice_bank_refs([], ctx["project_name"], all_characters=char_idx)
        _dub_dir = A._dub_project_dir(ctx["project_name"])
        _rows = A.tts_client.list_voice_bank(_dub_dir) or []
        _names = [str(r.get("character")) for r in _rows
                  if isinstance(r, dict) and r.get("character")]
        return {"ok": True, "skipped": False,
                "detail": {"note": f"配音先行：{len(_names)} 个角色参考音色就绪",
                           "characters": _names},
                "artifact": _dub_dir if os.path.isdir(_dub_dir) else ""}
    except Exception as e:  # noqa: BLE001  纯增强环节：绝不因 TTS/磁盘问题拖垮整集
        logger.warning("第%s集 配音先行（参考音色）失败，已跳过（不阻断）：%s",
                       ctx["episode_no"], e)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"配音先行失败，已跳过（不阻断）：{e}"}}


def step_assets(ctx) -> dict:
    """角色 / 物品 / 场景资产（项目级；三种类型各自带 AI 质检与重生成）"""
    A = _A()
    script = ctx.get("script") or {}
    pd = probe_assets(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd},
                "artifact": os.path.join(A.CHARACTERS_DIR, ctx["project_name"])}

    results, errs = {}, []
    for kind, script_key in (("character", "characters"), ("item", "items"), ("scene", "scenes")):
        if pd.get(kind, {}).get("done"):
            results[kind] = {"skipped": True, "total": pd[kind]["total"]}
            continue
        assets = [a for a in (script.get(script_key) or []) if isinstance(a, dict)]
        if not assets:
            results[kind] = {"skipped": True, "total": 0, "note": "剧本中无此类资产"}
            continue
        # S13 断点续跑：probe_assets 已算出本类 missing（缺 base 图）名单；只把缺失项传给
        # worker，已就绪资产不重烧 —— 30 个角色缺 1 个时不再重跑 30 次基础图 + 多视图 + 质检
        missing_names = {n for n in (pd.get(kind, {}).get("missing") or [])}
        todo = [a for a in assets
                if str((a.get("name") or "").strip()) in missing_names]
        if not todo:
            # 本类顶层未 done（顶层 done 需三类全齐）但本类无缺失 → 视为已就绪
            results[kind] = {"skipped": True, "total": len(assets), "note": "本类资产已就绪"}
            continue
        ctx["progress"](f"生成{kind}资产（缺 {len(todo)}/{len(assets)} 个）", 20,
                        phase=f"assets:{kind}")
        final = _run_task_worker(
            A._generate_asset_task, (todo, kind, ctx["project_name"], ctx["config"].get("style") or ""),
            "generation_state", "lock",
            init={"total": len(todo), "asset_type": kind, "phase": f"{kind} 资产"},
            prefix=f"pipe_asset_{kind}")
        out = _outcome_from_task(final, f"{kind} 资产生成")
        results[kind] = {"total": len(assets), "generated": len(todo),
                         "ok": out["ok"], "count": out["count"],
                         "blocked": out["blocked"], "error": out["error"]}
        if not out["ok"]:
            errs.append(f"{kind}: {out['error']}")
    if errs:
        return {"ok": False, "detail": {"results": results}, "error": "；".join(errs)}
    return {"ok": True, "detail": {"results": results}}


def step_storyboard(ctx) -> dict:
    """分镜图（逐镜生成，图片 AI 质检不达标自动换 seed 重生成并可阻断入库）"""
    A = _A()
    pd = probe_storyboard(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["dir"]}
    script = ctx.get("script") or {}
    shots = script.get("shots") or []
    if not shots:
        raise NeedsHumanError("剧本没有镜头数据，无法生成分镜图")

    char_idx = A._build_asset_index(script.get("characters") or [], ctx["project_name"], "character")
    item_idx = A._build_asset_index(script.get("items") or [], ctx["project_name"], "item")
    scene_idx = A._build_asset_index(script.get("scenes") or [], ctx["project_name"], "scene")
    ctx["progress"](f"生成分镜图（{len(shots)} 镜）", 34, phase="storyboard")
    # 审计 P1-3（2026-09-29）：托管 GPU 步骤也必须过并发闸门 —— 旧实现只闸手动链路，
    # 托管跑批期间手动任务可同时打 ComfyUI；且 has_other_running_gpu_tasks() 在托管
    # 期间返回 False，upscale/tts 会发 /free 卸掉托管任务正在用的模型。
    with gpu_task_gate.run_gpu_task(
            f"pipe_sb_{int(time.time() * 1000)}", "托管·分镜图"):
        final = _run_task_worker(
            A._storyboard_worker, (ctx["project_name"], shots, char_idx, item_idx, scene_idx,
                                   ctx["episode_no"], ctx["config"].get("style") or ""),
            "generation_state", "lock",
            init={"total": len(shots), "phase": "分镜图生成"},
            prefix="pipe_sb")
    out = _outcome_from_task(final, "分镜图生成")
    detail = {"count": out["count"], "total": len(shots), "blocked": out["blocked"],
              "qc_blocked": (final or {}).get("qc_blocked_count")}
    if not out["ok"]:
        return {"ok": False, "blocked": out["blocked"], "detail": detail, "error": out["error"]}
    # 复核产物：worker 报成功但文件缺失 / 为空 → 仍然判失败（不能凭状态字段放行）
    recheck = probe_storyboard(ctx)
    detail["probe"] = recheck
    if not recheck.get("done"):
        return {"ok": False, "detail": detail,
                "error": f"分镜图缺失 {len(recheck.get('missing') or [])} 镜："
                         f"{recheck.get('missing')[:8]}"}
    return {"ok": True, "detail": detail, "artifact": recheck["dir"]}


def step_video(ctx) -> dict:
    """整集视频生成（2026-10-01 起唯一模式：H3 一次生成整集连续视频；视频 AI
    质检门禁照常生效，不达标自动重生成，阻断不入库）"""
    A = _A()
    pd = probe_video(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["dir"]}
    script = ctx.get("script") or {}
    shots = script.get("shots") or []
    if not shots:
        raise NeedsHumanError("剧本没有镜头数据，无法生成视频")
    cfg = ctx["config"]
    mode = "episode"          # 2026-10-05：per_shot / keyframe 模式已随步骤收敛删除

    # 参考图：优先剧本自带，缺失时由 worker 内部从磁盘资产兜底
    char_refs = script.get("characters") or []
    scene_refs = script.get("scenes") or []
    ctx["progress"](f"生成视频（{len(shots)} 镜 · {mode}）", 50, phase="video")
    # 审计 P1-3：托管视频是闸门最重要的覆盖点（整集提交，独占 GPU 时间最长）
    with gpu_task_gate.run_gpu_task(
            f"pipe_video_{int(time.time() * 1000)}", "托管·视频"):
        final = _run_task_worker(
            A._video_generate_worker,
            (ctx["project_name"], shots, char_refs, scene_refs, {}, True, mode,
             int(ctx.get("timeout_per_segment") or 900),
             ctx.get("episode_tag") or f"ep{ctx['episode_no']:02d}",
             ctx["episode_no"],
             "auto",   # keyframe_chain_mode：仅逐镜 keyframe 模式消费，整集模式恒 auto
             cfg.get("style") or ""),
            "generation_state", "lock",
            init={"total": len(shots), "phase": "视频生成", "qc": A._qc_brief("video")},
            prefix="pipe_video")
    out = _outcome_from_task(final, "视频生成")
    recheck = probe_video(ctx)
    detail = {"mode": mode, "count": out["count"], "total": len(shots),
              "blocked": out["blocked"],
              "qc_blocked": (final or {}).get("qc_blocked_count"), "probe": recheck,
              "success_count": (final or {}).get("success_count")}
    if not recheck.get("done"):
        return {"ok": False, "blocked": out["blocked"], "detail": detail,
                "error": f"视频缺失 {len(recheck.get('missing') or [])} 镜："
                         f"{recheck.get('missing')[:8]}"}
    return {"ok": True, "detail": detail, "artifact": recheck["dir"]}


def _probe_concat_duration(concat_video: str, segments: list) -> float:
    """B-07 P1-5：ffprobe 实测拼接后视频总时长（秒）。

    字幕时间轴基准必须与成片实测时长一致，否则字幕整体漂移。
    拼接后若存在则优先用 ffprobe 实测；无 ffprobe 时退化用各段实测时长累加。
    """
    import subprocess as _sp
    import json as _json
    # 1) 若已拼好成片（concat_video 非空且存在），直接 ffprobe 取实测时长
    if concat_video and _nonempty(concat_video):
        try:
            r = _sp.run(["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "json",
                         os.path.abspath(concat_video)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            if r.returncode == 0:
                d = _json.loads(r.stdout or "{}")
                dur = float((d.get("format") or {}).get("duration") or 0)
                if dur > 0:
                    return dur
        except Exception as e:  # noqa: BLE001
            logger.debug("时长探测解析失败（忽略）：%s", e)
    # 2) 退化：各段 ffprobe 实测时长累加（比剧本 duration 累加更可靠）
    total = 0.0
    for seg in segments:
        try:
            r = _sp.run(["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "json",
                         os.path.abspath(seg)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            if r.returncode == 0:
                d = _json.loads(r.stdout or "{}")
                total += float((d.get("format") or {}).get("duration") or 0)
        except Exception:  # noqa: BLE001
            continue
    return total


def step_final(ctx) -> dict:
    """成片合成（2026-10-05 起仅整集模式）：优先采用超分产物为成片，无超分时
    回退整集原片（copy2），不再有逐镜拼接分支"""
    A = _A()
    pd = probe_final(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}
    vd = probe_video(ctx)
    if not vd.get("done"):
        raise PipelineError("视频未就绪，无法合成成片（请先完成视频生成）")

    out = final_path(ctx)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    # 整集模式（video_mode=episode，唯一模式）：step_video 由 H3 一次生成「整集视频」，
    # 磁盘上没有逐镜 shot_XX.mp4，直接把整集视频采用为成片。
    # 优先采用**超分产物**（upscale 步在 video 之后、final 之前）：超分成功则成片
    # 即超分版；超分被跳过 / fail-open 落空时回退整集原片。
    src = upscale_path(ctx)
    if not _playable(src):
        src = vd.get("file") or ""
    if not _nonempty(src):
        raise PipelineError("整集模式未找到整集视频文件，无法合成成片")
    ctx["progress"]("整集模式：采用整集视频作为成片", 94, phase="final")
    tmp = out + ".episode.mp4"
    if os.path.exists(tmp):
        os.remove(tmp)
    shutil.copy2(src, tmp)
    if not _nonempty(tmp):
        raise PipelineError("整集视频未能落盘为成片（文件为空）")

    # 字幕：分成两种文字、两个开关（2026-10-02）——
    #   · 台词字幕（人物开口的转录）→ subtitle_enabled，默认 false（2026-09-24 用户要求）；
    #   · 字幕/转场 caption（时空落点/回溯/集尾悬念）→ caption_burn_enabled，默认 true，
    #     它是**剧情装置**：不烧观众就会看到无过渡的跳切（参考稿靠「春秋蝉，逆转时光。」交代）。
    # 两者都不开时直接沿用拼接产物 tmp，行为与改动前一致。
    subbed = ""
    if (A._project_subtitle_enabled(ctx["project_name"])
            or A._project_caption_burn_enabled(ctx["project_name"])):
        try:
            from dialogue_utils import dialogue_text
            _want_dlg = A._project_subtitle_enabled(ctx["project_name"])
            _want_cap = A._project_caption_burn_enabled(ctx["project_name"])

            def _cap_text(_s):
                """镜头字幕文本：兼容 {text,kind} / 旧字符串 / 扁平 caption_text 三种形状。"""
                _c = _s.get("caption")
                if isinstance(_c, dict):
                    _c = _c.get("text")
                return str(_c or _s.get("caption_text") or "").strip()

            subs, cur = [], 0.0
            # B-07 P1-5：时间轴基准用成片实测总时长 + 各镜剧本时长占比分配
            #（单条整集源片无法逐段 ffprobe），避免字幕整体漂移。
            # （逐镜模式分支已随 per_shot/keyframe 模式删除，2026-10-05）
            script = ctx.get("script") or {}
            shots = script.get("shots") or []
            _ep_total = _probe_concat_duration(tmp, [tmp])
            for s in shots:
                dur = float(s.get("duration") or 5)
                _script_total = sum(float(x.get("duration") or 5) for x in shots) or 1.0
                if _ep_total > 0:
                    dur = dur / _script_total * _ep_total
                if _want_dlg:
                    text = dialogue_text(s.get("dialogue"))
                    if text:
                        subs.append({"start": cur, "end": cur + dur, "text": text})
                if _want_cap:
                    _ct = _cap_text(s)
                    if _ct:
                        subs.append({"start": cur, "end": cur + dur, "text": _ct})
                cur += dur
            if subs:
                subbed = A.video_processor.add_subtitles(tmp, subs, out)
        except Exception as e:  # noqa: BLE001  字幕失败不阻断成片
            logger.warning("成片字幕生成跳过（不影响成片）：%s", e)

    produced = subbed if _nonempty(subbed) else ""
    if not produced:
        os.replace(tmp, out)
        produced = out
    elif os.path.abspath(produced) != os.path.abspath(out):
        os.replace(produced, out)
        produced = out
    if _nonempty(tmp):
        try:
            os.remove(tmp)
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
    if not _nonempty(out):
        raise PipelineError("成片合成失败（目标文件为空）")
    # B-03 P0-4：成片重做后，把 deliverables.json 里该集的 review 复位到 pending，
    # 避免「打回 → 重跑 → 仍判 rejected → 又短路」的循环。
    old_review = _deliverable_review(ctx).get("review")
    if old_review == "rejected":
        _reset_deliverable_review(ctx, "pending", "成片已重做，请重新验收")
    # D-11a：成片步骤收尾 —— 滚动回收 ComfyUI 输出目录里的产物残留
    # （带进程内节流、全容错；回收是优化，任何异常都不影响成片）
    try:
        A._maybe_reclaim_comfyui_output()
    except Exception as e:  # noqa: BLE001  回收是优化，绝不能阻断成片
        logger.debug("D-11a 成片收尾回收跳过：%s: %s", type(e).__name__, e)
    # 成片收尾 —— 顺手清空 ComfyUI 任务历史面板（只清记录，不碰磁盘产物）。
    # 一次成片要经过分镜/视频/配音等多次重跑，面板会累积几百条，容易被误读成废图堆积。
    try:
        A._maybe_clear_comfyui_history("成片步骤收尾")
    except Exception as e:  # noqa: BLE001  可观测性优化，绝不能阻断成片
        logger.debug("ComfyUI 任务历史清理跳过：%s: %s", type(e).__name__, e)
    return {"ok": True, "artifact": out,
            "detail": {"mode": "episode", "subtitles": bool(subbed), "size": os.path.getsize(out)}}


def step_tts(ctx) -> dict:
    """配音：逐句合成 → 合并该集音轨 → 落配音清单"""
    A = _A()
    pd = probe_tts(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}
    script = ctx.get("script") or {}
    project = ctx["project_name"]
    out_dir = A._dub_project_dir(project)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = A.load_voice_map(vm_path) or A.default_voice_map(
        script.get("characters") or [], project, ctx["episode_no"])
    plan = A.build_dub_plan(script, voice_map, project, ctx["episode_no"],
                            shot_ids=None, only_missing=False,
                            out_dir_wav=os.path.join(out_dir, "lines"))
    if not plan.get("lines"):
        return {"ok": True, "skipped": True,
                "detail": {"note": "该集没有可朗读台词，跳过配音"}}

    # 2026-10-09 接线：_apply_audio_lessons 此前是**无调用者的孤岛函数**（守卫为此长期红）。
    # 按 audio 教训纠偏配音计划：纯就地修改、fail-open（失败只告警，不影响配音主链路）。
    try:
        A._apply_audio_lessons(plan["lines"], project)
    except Exception as _e:                       # noqa: BLE001
        A._app_logger().warning("audio 教训回流失败（忽略）：%s", _e)

    try:
        A.save_voice_map(plan["voice_map"], vm_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("音色映射保存失败（不影响本次合成）：%s", e)

    ctx["progress"](f"配音合成（{plan['line_count']} 句）", 82, phase="tts")
    final = _run_task_worker(
        A._dub_worker, (project, plan, out_dir, "wav", ctx["episode_no"]),
        "dub_tasks", "dub_lock",
        init={"total": plan["line_count"], "phase": "配音合成",
              "project_name": project, "out_dir": out_dir},
        prefix="pipe_dub")
    recheck = probe_tts(ctx)
    if not recheck.get("done"):
        return {"ok": False, "detail": {"task": final, "probe": recheck},
                "error": (final or {}).get("error") or "配音未完成（存在未成功台词）"}
    return {"ok": True, "detail": {"probe": recheck, "lines": plan["line_count"]},
            "artifact": recheck["file"]}


def step_mix(ctx) -> dict:
    """音画对齐混音：把该集配音按时间轴铺到成片上"""
    A = _A()
    pd = probe_mix(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}
    tts = probe_tts(ctx)
    if not tts.get("done"):
        return {"ok": True, "skipped": True,
                "detail": {"note": "该集无有效配音，跳过混音（成片保持无配音版本）"}}
    fv = final_path(ctx)
    if not _nonempty(fv):
        raise PipelineError("成片未就绪，无法混音")

    env = A.mix_ffmpeg_check()
    if not env.get("available"):
        raise NeedsHumanError("ffmpeg/ffprobe 不可用：" + "；".join(env.get("reasons") or []))

    out_name = os.path.basename(mix_output_path(ctx))
    prepared = A._mix_prepare({"project_name": ctx["project_name"],
                               "episode": ctx["episode_no"],
                               "video_path": fv})
    ctx["progress"](f"音画对齐混音（{len(prepared['entries'])} 句）", 92, phase="mix")
    final = _run_task_worker(
        A._mix_worker, (prepared, out_name),
        "mix_tasks", "mix_lock",
        init={"phase": "音画对齐", "project_name": ctx["project_name"],
              "video_path": fv, "out_name": out_name,
              "line_count": len(prepared["entries"])},
        prefix="pipe_mix")
    recheck = probe_mix(ctx)
    if not recheck.get("done"):
        return {"ok": False, "detail": {"task": final, "probe": recheck},
                "error": (final or {}).get("error") or "混音未产出有效文件"}
    return {"ok": True, "detail": {"probe": recheck, "lines": len(prepared["entries"])},
            "artifact": recheck["file"]}


def step_upscale(ctx) -> dict:
    """超分（FlashVSR）：对**集级原片**做超分，并归档到确定性路径

    设计要点（重要）
    ----------------
    这一步是**画质增强**，不是出片的必要环节，因此全程 fail-open：
    环境不可用（ComfyUI 离线 / FlashVSR 模型缺失 / 节点未安装）或执行失败时，
    一律返回 ``skipped`` 而非 ``failed``。

    原因：本步骤默认开启，若按普通步骤「失败即 raise」，会把一整集已经跑完的
    成片拖成失败态，用户既拿不到交付、又要为「锦上添花」的环节买单重跑。
    """
    A = _A()
    pd = probe_upscale(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}

    # ⭐ 2026-10-08 逐场次先超分再拼接（upscale_per_scene 开关，默认开）：
    # 有 ≥2 个可播放场次片段时走该路径；任一场失败 / 拼接失败 / 单场 → 落到下方
    # 整集原片超分（旧行为，保证不劣化）。全程 fail-open：只跳过，不阻断成片。
    per_scene_srcs = _scene_segments(ctx) if bool(ctx["config"].get("upscale_per_scene", True)) else []
    if len(per_scene_srcs) >= 2:
        out = upscale_path(ctx)
        if _try_per_scene_upscale(ctx, per_scene_srcs, out):
            return {"ok": True, "artifact": out,
                    "detail": {"mode": "per_scene", "scenes": len(per_scene_srcs),
                               "size": os.path.getsize(out)}}
        # 逐场失败 → 继续走下方整集原片路径（不 return）

    # 超分作用对象＝**集级原片**（video 步产出的整集视频）。旧顺序里超分在混音之后，
    # 此处保留 mix/final 两个回退分支，便于「老工程续跑」时仍能命中既有产物。
    src = ""
    try:
        src = (probe_video(ctx).get("file") or "")
    except Exception:  # noqa: BLE001  探测失败按「无源片」处理，走下方回退
        src = ""
    if not _nonempty(src):
        src = mix_output_path(ctx)
    if not _nonempty(src):
        src = final_path(ctx)
    if not _nonempty(src):
        return {"ok": True, "skipped": True,
                "detail": {"note": "该集无成片可超分，跳过"}}

    try:
        import upscale_client
    except Exception as e:  # noqa: BLE001
        logger.warning("第%s集超分跳过（模块不可用）：%s", ctx["episode_no"], e)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分模块不可用，已跳过：{e}"}}

    # 环境自检：不可用直接跳过，不进入重试循环
    try:
        env = A.upscale_env_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("第%s集超分跳过（环境自检失败）：%s", ctx["episode_no"], e)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分环境自检失败，已跳过：{e}"}}
    if not env.get("available"):
        reason = "；".join(env.get("reasons") or []) or "超分环境不可用"
        logger.warning("第%s集超分跳过（环境不可用）：%s", ctx["episode_no"], reason)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分环境不可用，已跳过：{reason}",
                           "env": {"comfy_online": env.get("comfy_online"),
                                   "model_ready": env.get("model_ready"),
                                   "te_ready": env.get("te_ready"),
                                   "reasons": env.get("reasons") or []}}}

    scale = int(ctx["config"].get("upscale_scale") or 2)
    if scale not in (2, 3, 4):      # 兜底：配置未经 normalize_config 直连时
        scale = 2
    ctx["progress"](f"超分（FlashVSR {scale}x）…", 84, phase="upscale")
    try:
        # 审计 P1-3：托管超分同样过闸门；并把闸门 id 同步给 upscaler 的
        # current_task_id —— /free 守卫（has_other_running_gpu_tasks）据此排除自身，
        # 语义与手动超分路由一致（只挡「他人」的 /free，不挡自己的）。
        _upscaler = upscale_client.VideoUpscaler()
        _up_tid = f"pipe_up_{int(time.time() * 1000)}"
        _upscaler.current_task_id = _up_tid
        with gpu_task_gate.run_gpu_task(_up_tid, "托管·超分"):
            res = _upscaler.upscale(
                src, project_name=ctx["project_name"], scale=scale,
                # ⚠️ 源片自带 H3 原生音轨，而 TE-Speed 链路默认 attach_audio=False ——
                # 不显式开启会把音轨丢掉，超分产物变成无声视频。
                attach_audio=True,
                progress_cb=lambda msg, pct=None: ctx["progress"](
                    f"超分：{msg}", 84, phase="upscale"),
            )
    except Exception as e:  # noqa: BLE001  超分失败不阻断出片
        logger.warning("第%s集超分失败（已跳过，不影响成片交付）：%s",
                       ctx["episode_no"], e, exc_info=True)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分执行失败，已跳过：{type(e).__name__}: {e}",
                           "source": src}}

    produced = (res or {}).get("output_path") or ""
    if not _nonempty(produced):
        return {"ok": True, "skipped": True,
                "detail": {"note": "超分未产出有效文件，已跳过", "source": src}}

    # 归档到确定性路径（带时间戳的原文件名无法用于幂等探测）
    out = upscale_path(ctx)
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.abspath(produced) != os.path.abspath(out):
            shutil.copy2(produced, out)
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分产物归档失败，已跳过：{e}", "produced": produced}}
    if not _nonempty(out):
        return {"ok": True, "skipped": True,
                "detail": {"note": "超分产物归档后为空，已跳过", "produced": produced}}

    return {"ok": True, "artifact": out,
            "detail": {"source": src, "engine": (res or {}).get("engine"),
                       "scale": scale, "before": (res or {}).get("before"),
                       "after": (res or {}).get("after"),
                       "elapsed_sec": (res or {}).get("elapsed_sec"),
                       "size": os.path.getsize(out)}}


STEP_RUNNERS = {
    "script": step_script,
    "tts_pre": step_tts_pre,
    "assets": step_assets,
    "storyboard": step_storyboard,
    "video": step_video,
    "upscale": step_upscale,
    "final": step_final,
    # tts / mix / keyframe 已不在 STEP_SEQUENCE（2026-10-05 收敛为 7 步），函数/探测
    # 的保留策略：tts / mix 函数体保留（手工端点与「老工程续跑」路径不废，回滚只需
    # 把它俩加回 STEP_SEQUENCE）；keyframe 的 runner 与 probe 已删（手动尾帧链路在
    # keyframe.py / app.py，与此处无关）。
    "tts": step_tts,
    "mix": step_mix,
}


def step_enabled(step: str, ctx) -> bool:
    """步骤是否对该集启用（由配置与集号决定）"""
    cfg = ctx["config"]
    if step == "script":
        return True
    if step == "tts_pre":
        # 只受 enable_tts_pre 控制：原先还要求 enable_tts，会让「关掉 TTS」连带
        # 静默关掉整步（含参考音色）。enable_tts 改为在 step_tts_pre 内部生效。
        return bool(cfg.get("enable_tts_pre"))
    if step == "assets":
        return bool(cfg.get("enable_assets"))
    if step == "storyboard":
        return True                      # 分镜图是视频的必要输入，恒开
    if step == "video":
        return bool(cfg.get("enable_video"))
    if step == "final":
        return bool(cfg.get("enable_final"))
    if step == "tts":
        return bool(cfg.get("enable_tts"))
    if step == "mix":
        return bool(cfg.get("enable_mix"))
    if step == "upscale":
        return bool(cfg.get("enable_upscale"))
    return False


# ===================== 一致性复检（跨镜角色） =====================


def check_consistency(ctx) -> dict:
    """成片前的跨镜一致性复检（不达标即判失败，交由重试层处理）"""
    A = _A()
    cfg = ctx["config"]
    if not cfg.get("require_consistency"):
        return {"ok": True, "skipped": True, "note": "未开启一致性门禁"}
    try:
        collect = A._consistency_collect(ctx["project_name"], ctx["episode_no"])
        if not collect.get("shot_images"):
            return {"ok": True, "skipped": True, "note": "尚无分镜图可比对"}
        report = A.consistency.run(
            ctx["project_name"],
            character_refs=collect["character_refs"],
            shot_images=collect["shot_images"],
            asset_dirs=collect["asset_dirs"],
            cfg=A._qc_load_cfg(),
            include_assets=False, include_shots=True)
    except Exception as e:  # noqa: BLE001  一致性校验失败不阻断生产（仅记录）
        logger.warning("一致性复检异常（不阻断）：%s", e)
        return {"ok": True, "skipped": True, "note": f"复检异常：{e}"}
    summary = report.get("summary") or {}
    floor = float(cfg.get("consistency_min_score") or 0)
    mins = summary.get("min_score")
    out = {"summary": summary, "report_path": report.get("report_path")}
    if floor > 0 and isinstance(mins, (int, float)) and mins < floor:
        return {"ok": False, "detail": out,
                "error": f"跨镜一致性最低分 {mins} 低于阈值 {floor}"
                         f"（{summary.get('failed')} 处不达标）"}
    return {"ok": True, "detail": out}


# ===================== 单集流水线 =====================


def _load_script_into_ctx(ctx) -> dict:
    A = _A()
    p = _script_path(ctx)
    if not _nonempty(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("剧本读取失败 %s：%s", p, e)
        return {}


def _deliverable_of(ctx, steps: dict) -> str:
    """该集的最终交付物：优先超分成品，其次成片"""
    for key in ("upscale", "final"):
        art = (steps.get(key) or {}).get("artifact") or ""
        if _nonempty(art):
            return art
    return ""


def _prefetch_next_scripts(ctx: dict, limit: int = 1) -> None:
    """后台预热：当前集进入 GPU 任务前，先生成下一集剧本，避免图片/视频排队等 LLM。

    滚动预取（2026-10-06）：每集剧本步骤完成后都会触发（不再限第 1 集），
    恒只领先 1 集 —— 等效于「每一集在产图/产视频的同时，下一集剧本都在后台转」，
    且不会一口气把后续全部章节跑掉。
    特点：
    - 非阻塞：起一个后台线程执行，主流程不等待；
    - 只落剧本 JSON，不碰 ComfyUI，不与当前 GPU 任务抢显存；
    - 幂等：若目标集剧本已存在直接跳过（probe_script）。
    ⚠️ 章节映射按 chapters[N-1]（一章一集默认口径）；开 MJSCXT_EPISODE_SPLIT 拆章
    时集号≠章号，此预取的章节定位会偏——与既有行为一致，如需精确应走 episode_units。
    """
    A = _A()
    try:
        if not ctx.get("novel_meta") or not ctx.get("chapter"):
            return
        meta = ctx["novel_meta"]
        key = ctx["project_key"]
        style = ctx["config"].get("style") or ""
        target_shots = int(ctx["config"].get("target_shots") or 12)
        # 只预取下一集，防止一口气把后续全部章节都跑掉
        next_no = int(ctx["episode_no"] or 0) + 1
        import autopilot as _autopilot
        chapters, text = _autopilot.chapters_and_text(meta)
        if not chapters or not text:
            logger.debug("剧本预热：无可用章节/正文，跳过 project=%s ep=%s", key, next_no)
            return
        # 定位下一集对应章节
        ch_next = None
        for idx in range(next_no - 1, len(chapters)):
            if idx < 0:
                break
            c = chapters[idx]
            if isinstance(c, dict) and c.get("text"):
                ch_next = c
                break
        if not ch_next:
            logger.debug("剧本预热：未找到第 %s 集对应章节", next_no)
            return
        # 已存在直接跳过
        script_path = A.novel_to_script.episode_script_path(A.SCRIPT_DIR, key, next_no, key)
        if _nonempty(script_path):
            logger.info("剧本预热：第 %s 集剧本已存在，跳过 %s", next_no, script_path)
            return

        def _worker():
            try:
                client = A._current_llm_client()
                if not client or not getattr(client, "configured", False):
                    logger.warning("剧本预热：文本模型未配置，跳过 %s", key)
                    return
                conv = A.continuity.convert_chapter_with_continuity(
                    client, meta, text, ch_next, key, A.CONTINUITY_DIR,
                    style=style, target_shots=target_shots, episode_no=next_no,
                    save_dir=A.SCRIPT_DIR,
                )
                logger.info("剧本预热：第 %s 集剧本已生成 %s", next_no, conv.get("script_path") or "")
            except Exception as e:  # noqa: BLE001
                logger.warning("剧本预热：第 %s 集生成失败（不影响当前集）：%s", next_no, e)

        t = threading.Thread(target=_worker, daemon=True, name=f"prefetch-script-{key}-{next_no}")
        t.start()
        logger.info("剧本预热：已启动后台线程生成第 %s 集剧本（project=%s）", next_no, key)
    except Exception as e:  # noqa: BLE001
        logger.warning("剧本预热调度失败（忽略，不影响主流程）：%s", e)


def backfill_style_from_ai_settings(config: dict, project_name: str,
                                    episode_no: int = 0) -> str:
    """风格回填（2026-09-22 P-2 风格未生效）：config.style 为空时，从 AI 总控落盘的
    创作设定（ai_chat/project_settings.json 的 style_brief）回填。

    根因：总控 apply 设定的风格只写 project_settings.json，而托管 plan.json 缺 style
    字段 → run-once / 托管轮转 / 总控 produce_episode 三条生产入口一律带空 style 进
    流水线，剧本与提示词全落回硬编码默认「3D动漫渲染」。此处单点回填覆盖全部入口。

    返回实际回填的风格串（未回填/读取失败返回 ""）。就地更新 config。
    """
    if (config.get("style") or "").strip():
        return config["style"]
    try:
        A = _A()
        brief = (A.ai_chat.settings_view(A.AI_SETTINGS_PATH, project_name)
                 .get("style_brief") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 总控风格设定读取失败（本集沿用默认风格）：%s", e)
        return ""
    if not brief:
        return ""
    norm = _A().style_kit.normalize_style(brief) or brief
    config["style"] = norm
    logger.info("第%s集：风格回填自 AI 总控设定（托管计划未带 style）：%s",
                episode_no, norm)
    return norm


def run_episode(config: dict, project_name: str, episode_no: int, novel_meta: dict,
                chapter: dict, progress_cb=None, should_stop=None) -> dict:
    """跑完一集的完整流水线，返回可直接落库的结果字典

    参数
    ----
    config        : normalize_config() 后的配置
    project_name  : 项目名（产物目录键）
    episode_no    : 集号
    novel_meta    : novel_parser.get_novel() 的元信息
    chapter       : 该集对应章节 {"index","title","text"...}
    progress_cb   : fn(message, percent, phase=None)
    should_stop   : fn() -> bool，返回 True 时中止本集（用于暂停 / 停止托管）。
                    检查点有两类：① 步骤边界；② 步骤内部「发起 LLM 调用前 /
                    重试退避前」—— 后者让暂停**秒级生效**，不必等重试链跑完。
                    两类检查点都落在**尚未产出文件**的位置，因此不会留下半成品；
                    已完成的步骤全部保留，续跑时会被逐步骤探测跳过。
    """
    A = _A()
    started = time.time()
    backfill_style_from_ai_settings(config, project_name, episode_no)
    # 进度单调化：各步骤内部回报的百分比（如剧本步骤的 4%~18%）与整体锚点百分比
    # 来源不同，直接透传会让进度条回退（实测出现 18% → 16% → 17%）。无人值守界面里
    # 「进度倒退」非常误导，这里统一钳住只增不减。
    _cb_user = progress_cb or (lambda *a, **k: None)
    _pct_seen = {"v": 0}

    def _progress(message, percent=None, phase=None):
        try:
            p = int(percent or 0)
        except (TypeError, ValueError):
            p = 0
        if p < _pct_seen["v"]:
            p = _pct_seen["v"]
        else:
            _pct_seen["v"] = p
        _cb_user(message, p, phase=phase)
        # 进度实时镜像到集级操作日志：记录的是单调化后的百分比（与进度条所见一致），
        # 人工/总控可据此回放「卡在哪一步、几成、停了多久」，区分「慢」与「挂」。
        _ep_log(project_name, int(episode_no), "progress", message,
                percent=p, phase=phase)

    # 文学剧本改写接入：config.screenplay_text 存在时覆盖 chapter 文本
    _screenplay_text = config.get("screenplay_text")
    if _screenplay_text:
        # 用文学剧本文本覆盖 chapter 文本
        chapter_text = _screenplay_text
        chapter = {**chapter, "text": _screenplay_text, "start": 0, "end": len(_screenplay_text)}
    ctx = {
        "config": config, "project_name": project_name,
        "project_key": config.get("project_key") or project_name,
        "episode_no": int(episode_no), "episode_tag": f"ep{int(episode_no):02d}",
        "novel_meta": novel_meta or {}, "chapter": chapter or {},
        "timeout_per_segment": int(config.get("timeout_per_segment") or 900),
        "script": {}, "logs": [], "steps": {},
        "progress": _progress,
    }
    dead = _is_dead_letter(config, project_name, int(episode_no))
    if dead:
        return {"ok": False, "status": "needs_human", "episode_no": int(episode_no),
                "project": project_name, "deliverable": "", "steps": {},
                "error": f"该集此前已判定需人工介入（{dead.get('reason') or '未知原因'}）",
                "note": "请先在「需人工介入」中处理或标记忽略后才会重新尝试"}

    # B-02 P0-5：集级互斥。拿到锁才能继续，拿不到说明另一执行体（run-once / 托管轮转）
    # 正在跑同一集 → 直接返回 busy，不并发写同一批路径。不同集/项目互不影响。
    if not _acquire_episode_lock(project_name, int(episode_no)):
        return {"ok": False, "status": "busy", "episode_no": int(episode_no),
                "project": project_name, "deliverable": "", "steps": {},
                "error": f"{project_name} 第{int(episode_no)}集 正在被另一个执行体生产，"
                         f"请稍后再试（避免并发写同一批路径）",
                "note": "同一集任何时刻只有一个执行体；不同集可并行"}

    # 集级操作日志第一条事件：拿到锁 = 本次执行体真正开跑。
    # ⚠️ busy 早退（上面拿不到锁）不写日志 —— 避免与正在跑的执行体交叉写同一份 JSONL。
    # grid_mode：normalize_config 没有 sb_grid_mode 键（不造新配置），取宿主模块的
    # 场景九宫格开关（SCENE_GRID_MODE）作为最接近的可用口径。
    _ep_log(project_name, int(episode_no), "episode_run_start",
            f"开始生产第{int(episode_no)}集",
            chapter_title=(chapter or {}).get("title"),
            style=(config.get("style") or ""),
            screenplay_used=bool(config.get("screenplay_text")),
            grid_mode=bool(getattr(A, "SCENE_GRID_MODE", False)))

    result = {
        "ok": False, "status": "failed", "episode_no": int(episode_no),
        "project": project_name, "project_key": ctx["project_key"],
        "chapter_title": (chapter or {}).get("title") or f"第{int(episode_no)}集",
        "started_at": _now(), "deliverable": "", "steps": {}, "error": "",
    }

    # 把「是否该停」注册进当前执行上下文（contextvars）：本集内部所有 LLM 调用与
    # 重试退避都能感知到它，从而实现「暂停秒级生效」。作用域仅限本调用链 ——
    # 用户手动触发的生产（should_stop=None）不受任何影响。
    # ⚠️ push 前必须 clear()：线程池复用线程时 contextvars 随副本继承，先前请求
    #    泄漏的判定器（实测：总控「停止生产」的全局暂停）会让新生产每次提交都被
    #    秒级取消（3 次重试瞬间烧完）。先清场再注册自己的判定器，见 cancellation.clear()。
    cancellation.clear()
    _cancel_token = cancellation.push(should_stop)
    try:
        # P0-6：进入步骤循环前先做「模式 × 产物期望」一致性断言（配置矛盾时在这里
        # fail-loud，不要拖到视频/成片步骤运行中途才暴露）。放在 try 内很关键：
        # 断言抛 PipelineError 时 finally 仍会释放集级锁（见 _release_episode_lock），
        # 否则「拿到锁后、配置矛盾」会让该集永久 busy；且异常被下方 except 接住
        # 转成结构化 result，两个调用方（run-once / 托管）都拿到干净结果而非 500。
        assert_mode_contract(config)
        for step in STEP_SEQUENCE:
            if should_stop and should_stop():
                result.update({"status": "cancelled",
                               "error": "托管已暂停，在步骤边界安全中止（已完成步骤保留，可续跑）"})
                # 该 return 直接跳出、不经过下方统一收尾，这里补落最后一条集级事件
                _ep_log(project_name, int(episode_no), "episode_failed",
                        "托管暂停中止，已完成步骤已保留", level="info",
                        status="cancelled", error=result.get("error") or "",
                        elapsed_sec=round(time.time() - started, 1))
                result.setdefault("episode_log",
                                  _episode_log_path(project_name, int(episode_no)))
                return result
            if not step_enabled(step, ctx):
                result["steps"][step] = {"status": "disabled"}
                continue

            # 已存在的剧本要先读进来，后续步骤（资产/分镜/视频）都依赖它
            if step != "script" and not ctx.get("script"):
                ctx["script"] = _load_script_into_ctx(ctx)

            # 后台预热：第一集完成剧本后，进入 GPU 任务前预取后续集剧本（非阻塞）
            if step == "script":
                _prefetch_next_scripts(ctx)

            ctx["progress"](f"{STEP_LABELS[step]}…", result_pct(result, step), phase=step)
            # 集级操作日志：步骤边界事件（start / skip / done / fail）。
            # 上一行的 progress 回调已是该步的锚点进度回报（进入步骤即推进百分比），
            # 进度侧不重复回报。
            _ep_log(project_name, int(episode_no), "step_start",
                    f"开始步骤 {step}", step=step)
            _step_t0 = time.time()
            out, attempts = _run_step_with_retry(step, ctx)
            result["steps"][step] = {
                "status": "done" if out.get("ok") else "failed",
                "skipped": bool(out.get("skipped")),
                "blocked": bool(out.get("blocked")),
                "attempts": attempts,
                "error": out.get("error") or "",
                "artifact": out.get("artifact") or "",
                "detail": out.get("detail") or {},
                "finished_at": _now(),
            }
            ctx["steps"][step] = out
            # 角色资产自动补做（2026-10-01）：assets 步骤完成后验证角色图是否真实落盘，
            # 缺失时自动重跑一次（修复「角色参考图不可用」→ 视频无锚点的反复故障）
            if step == "assets" and out.get("ok"):
                try:
                    from config import CHARACTERS_DIR as _CD
                    _cb = os.path.join(_CD, ctx["project_name"])
                    _has = any(
                        os.path.isfile(os.path.join(_cb, d, "base.png"))
                        for d in (os.listdir(_cb) if os.path.isdir(_cb) else [])
                        if os.path.isdir(os.path.join(_cb, d))
                    )
                    if not _has:
                        logger.warning("角色资产图缺失（base.png 均不存在），自动重跑资产生成")
                        out, attempts = _run_step_with_retry("assets", ctx)
                        result["steps"][step] = {
                            "status": "done" if out.get("ok") else "failed",
                            "attempts": attempts,
                            "error": out.get("error") or "",
                            "artifact": out.get("artifact") or "",
                            "detail": out.get("detail") or {},
                        }
                        ctx["steps"][step] = out
                except Exception as _ace:
                    logger.warning("角色资产自动补做检查失败（忽略）：%s", _ace)
            _step_elapsed = round(time.time() - _step_t0, 1)
            if not out.get("ok"):
                result["steps_status"] = "blocked"
                _ep_log(project_name, int(episode_no), "step_fail",
                        f"{STEP_LABELS[step]}失败：{str(out.get('error') or '未知原因')[:120]}",
                        level="error", step=step,
                        error=str(out.get("error") or "")[:300], attempts=attempts)
                raise PipelineError(f"{STEP_LABELS[step]}未通过：{out.get('error')}"
                                    f"（已尝试 {attempts} 次）")
            if out.get("skipped"):
                # probe 命中 / 开关性跳过（skipped 由各 step runner 归一返回）
                _ep_log(project_name, int(episode_no), "step_skip",
                        f"步骤 {step} 产物已存在，跳过", step=step,
                        elapsed_sec=_step_elapsed)
            else:
                _ep_log(project_name, int(episode_no), "step_done",
                        f"{STEP_LABELS[step]}完成", step=step,
                        elapsed_sec=_step_elapsed, artifact=out.get("artifact") or "")
            # 每完成一步立即记录产物，便于崩溃后从日志判断进度
            ctx["logs"].append(f"[{_now()}] {STEP_LABELS[step]} 完成"
                               f"{'（跳过）' if out.get('skipped') else ''}"
                               f"{' · 重试 %d 次' % (attempts - 1) if attempts > 1 else ''}")

        result["deliverable"] = _deliverable_of(ctx, ctx["steps"])
        result["ok"] = True
        result["status"] = "done"
        if not result["deliverable"]:
            result["ok"] = False
            result["status"] = "failed"
            result["error"] = "流水线跑完但未产出可交付文件（请检查各环节开关）"
    except NeedsHumanError as e:
        result["status"] = "needs_human"
        result["error"] = str(e)
    except cancellation.Cancelled as e:
        # 协作式中止（托管暂停 / 用户停止）：停在**尚未产出文件**的安全点，
        # 已完成步骤全部保留，续跑时会被逐步骤探测跳过。
        result["status"] = "cancelled"
        result["error"] = f"收到中止信号，已在安全点停下（已完成步骤保留，可续跑）：{e}"
        logger.info("第%s集因中止信号停止：%s", episode_no, e)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        logger.error("第%s集流水线失败：%s\n%s", episode_no, result["error"], traceback.format_exc())
    finally:
        cancellation.reset(_cancel_token)
        _release_episode_lock(project_name, int(episode_no))

    result["elapsed_sec"] = round(time.time() - started, 1)
    result["finished_at"] = _now()
    # 集级操作日志收尾（统一出口）：成功 / 失败 / 需人工(needs_human, ok=False) /
    # 中止(cancelled) 各按状态落最后一条事件，并把日志路径带回给调用方。
    if result.get("status") == "cancelled":
        _ep_log(project_name, int(episode_no), "episode_failed",
                "托管暂停中止，已完成步骤已保留", level="info",
                status="cancelled", error=result.get("error") or "",
                elapsed_sec=result.get("elapsed_sec"))
    elif result.get("ok"):
        _ep_log(project_name, int(episode_no), "episode_done",
                f"第{int(episode_no)}集生产完成",
                ok=True, status=result.get("status"), error=result.get("error") or "",
                deliverable=result.get("deliverable") or "",
                elapsed_sec=result.get("elapsed_sec"))
    else:
        _ep_log(project_name, int(episode_no), "episode_failed",
                f"第{int(episode_no)}集生产失败："
                f"{str(result.get('error') or '未知原因')[:200]}",
                level="error", ok=bool(result.get("ok")),
                status=result.get("status"), error=str(result.get("error") or "")[:300],
                elapsed_sec=result.get("elapsed_sec"))
    result.setdefault("episode_log", _episode_log_path(project_name, int(episode_no)))
    # 记账（时长 + 成功与否），供成本看板统计
    try:
        A.analytics.record_event("pipeline", project_name,
                                 f"第{episode_no}集自动生产",
                                 int(result["elapsed_sec"]),
                                 units=len(result["steps"]),
                                 success=bool(result["ok"]))
    except Exception as e:  # noqa: BLE001
        logger.debug("旁路统计写入失败（忽略）：%s", e)
    return result


#: 步骤在整体进度里的百分比锚点（2026-10-05：keyframe 移除后重排）
_STEP_PCT = {"script": 2, "tts_pre": 10, "assets": 18, "storyboard": 32,
             "video": 48, "upscale": 82, "final": 92}


def result_pct(result: dict, step: str) -> int:
    """该步骤开始时的整体进度（用步骤锚点，避免各 worker 自己写的百分比互相打架）"""
    return int(_STEP_PCT.get(step, 5))


def _run_step_with_retry(step: str, ctx: dict) -> tuple:
    """执行一个步骤，失败按配置自动重试；返回 (outcome, 尝试次数)"""
    cfg = ctx["config"]
    cfg_max = int(cfg.get("step_max_retries") or 0) if cfg.get("auto_repair") else 0
    # 质检门禁类步骤失败通常因为生成质量，多给一次机会
    max_tries = max(cfg_max, 1 if step in GATED_STEPS else cfg_max) + 1
    last = {}
    for attempt in range(1, max_tries + 1):
        # ⚠️ 中止信号优先于重试：托管暂停时立刻停，不再空转剩余重试次数。
        # 这个检查点在「还没产出任何文件」的位置，因此不会留下半成品。
        cancellation.check(f"{STEP_LABELS[step]} 重试前收到中止信号")
        if attempt > 1:
            ctx["progress"](f"{STEP_LABELS[step]} 第 {attempt}/{max_tries} 次重试…",
                            result_pct(ctx_step_ctx(ctx), step), phase=f"{step}:retry")
            logger.warning("第%s集 %s 第 %d 次重试（上次：%s）",
                           ctx["episode_no"], step, attempt, last.get("error"))
            # 可被打断的退避：避免长退避期间「暂停」长时间无响应
            cancellation.sleep(min(3 * attempt, 10))
        try:
            out = STEP_RUNNERS[step](ctx)
        except NeedsHumanError:
            raise
        except cancellation.Cancelled:
            raise                      # 中止信号必须穿透，不能被当成一次「步骤失败」
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            logger.error("第%s集 %s 异常：%s\n%s", ctx["episode_no"], step,
                         out["error"], traceback.format_exc())
        last = out
        if out.get("ok"):
            return out, attempt
        # 明确的环境性故障不做无意义重试
        if out.get("fatal") or out.get("needs_human"):
            break
    return last, max_tries


def ctx_step_ctx(ctx):
    return ctx


# ===================== 死信（需人工介入）记录 =====================


def dead_letter_path(project_name: str) -> str:
    A = _A()
    return os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "dead_letter.json")


# P1-11（A-13）：死信文件损坏防护 —— 「.bak 快照 + 唯一临时名 + 损坏先恢复」
# 背景：`_read_dead_letters` 曾「解析失败 → 静默 {}」，死信记录因此丢失，
# 该集会被无限重烧并反复重新标记。对齐 project_store / secret_store 口径：
# 每次发布前把「当前可解析好版本」快照到 .bak，损坏时先从 .bak 恢复；
# 恢复不了才降级为空（并 error 日志留痕，绝不静默）。


def _dead_letter_bak(path: str) -> str:
    """死信文件「上一份可解析好版本」备份路径：``path + '.bak'``."""
    return path + ".bak"


def _dead_letter_snapshot_bak(path: str) -> None:
    """发布前快照：若活文件当前可解析，复制到 .bak（保留最后一次好版本）."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        return  # 活文件已不可解析 → 不覆盖既有 .bak（它仍是最后一份好版本）
    try:
        shutil.copy2(path, _dead_letter_bak(path))
    except OSError as e:
        logger.warning("死信文件 .bak 快照写入失败（既有 .bak 仍保留为最后好版本）：%s", e)


def _dead_letter_try_restore_bak(path: str):
    """活文件损坏时，尝试从 .bak（最后一份好版本）恢复并返回其 dict；无备份返回 None."""
    bak = _dead_letter_bak(path)
    if not os.path.isfile(bak):
        return None
    try:
        with open(bak, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("episodes", {})
    tmp = f"{path}.restore.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
        return None
    return data


def _read_dead_letters(project_name: str) -> dict:
    p = dead_letter_path(project_name)
    if not _nonempty(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        # P1-11（A-13）：解析失败 = 文件损坏。先尝试从 .bak 恢复，
        # 恢复不了才降级为空（留 error 日志，绝不静默吞掉）。
        logger.error("死信文件 %s 解析失败（文件损坏：%s），尝试从 .bak 恢复", p, e)
        restored = _dead_letter_try_restore_bak(p)
        if restored is not None:
            logger.warning("死信文件 %s 已从 .bak 恢复（保留既有死信记录）", p)
            return restored
        logger.error("死信文件 %s 损坏且无可用 .bak，按无死信处理（既有记录已丢失）", p)
        return {}
    if not isinstance(data, dict):
        data = {}
    eps = data.get("episodes")
    if eps is not None and not isinstance(eps, dict):
        logger.warning("死信文件 %s 的 episodes 结构异常，按空结构处理", p)
        data["episodes"] = {}
    elif eps is None:
        data["episodes"] = {}
    return data


def _is_dead_letter(config: dict, project_name: str, episode_no: int) -> dict:
    """该集是否已被判定「需人工介入」且尚未处理"""
    d = _read_dead_letters(project_name)
    item = (d.get("episodes") or {}).get(str(episode_no))
    return item if isinstance(item, dict) and not item.get("resolved") else {}


def mark_dead_letter(project_name: str, episode_no: int, reason: str,
                     detail: dict = None) -> dict:
    """把某集标记为「需人工介入」（用户处理或忽略后才会重新尝试）"""
    p = dead_letter_path(project_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = _read_dead_letters(project_name)
    eps = data.setdefault("episodes", {})
    eps[str(int(episode_no))] = {
        "episode_no": int(episode_no), "reason": reason,
        # 结构化失败码（2026-09-29）：从**已有文案**归类，原文一字不改，
        # 只为让死信队列与看板能按原因分组统计（failure_codes.summarize）。
        "code": failure_codes.classify(reason),
        "code_label": failure_codes.explain(reason),
        "detail": detail or {}, "marked_at": _now(), "resolved": False,
    }
    _dead_letter_snapshot_bak(p)  # P1-11（A-13）：发布前快照最后一份好版本
    tmp = f"{p}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    logger.warning("第%s集已标记需人工介入：%s", episode_no, reason)
    return eps[str(int(episode_no))]


def resolve_dead_letter(project_name: str, episode_no: int, note: str = "") -> dict:
    """人工处理完成 / 标记忽略（之后流水线会重新尝试该集）"""
    p = dead_letter_path(project_name)
    data = _read_dead_letters(project_name)
    item = (data.get("episodes") or {}).get(str(int(episode_no)))
    if not item:
        return {}
    item.update({"resolved": True, "resolved_at": _now(), "resolve_note": note})
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _dead_letter_snapshot_bak(p)  # P1-11（A-13）：发布前快照最后一份好版本
    tmp = f"{p}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return item


def list_dead_letters(project_name: str) -> list:
    d = _read_dead_letters(project_name)
    items = [v for v in (d.get("episodes") or {}).values() if isinstance(v, dict)]
    items.sort(key=lambda x: int(x.get("episode_no") or 0))
    return items


# ===================== 交付物登记 =====================


def record_deliverable(project_name: str, episode_no: int, path: str,
                       meta: dict = None) -> dict:
    """把该集成片登记进「待验收」队列（用户只需看这里）"""
    # 铁律（2026-09-29）：预演产物**永不可交付**。闸门放在**最深的这个漏斗**上，
    # 这样任何调用方（register_final_deliverable / 托管轮转 / 手动登记）都绕不过去。
    # 预演长得像成片，一旦进索引，用户就可能拿一版低分辨率糊图去发布。
    _ok, _why = preview_gate.deliverable_ok(path)
    if not _ok:
        logger.error("[预演拦截] 拒绝把非正式产物登记为成片（%s）：%s",
                     os.path.basename(str(path or "")), _why)
        raise PipelineError(f"不可交付的产物：{_why}")
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    os.makedirs(os.path.dirname(idx_path), exist_ok=True)
    # A-4：这是一次「读改写」，损坏态若被静默读成空索引，会把**整个交付物索引**
    # 覆盖成只含本集的一条。故改为 fail-loud（read_json_strict 会先尝试从 .bak
    # 恢复，恢复不了才抛错），由流水线的步骤级错误处理记录该集失败。
    data = read_json_strict(idx_path, {}) or {}
    items = data.setdefault("items", {})
    key = str(int(episode_no))
    prev = items.get(key) or {}
    # 哈希绑定（2026-09-29）：老实现只看**路径**是否变化，但重做一集时产物是
    # **原地覆盖**（同名 ep01_full.mp4），路径没变 → 旧的「已验收」会静默延续到
    # 新内容上，用户以为批的是当前这版、其实是上一版。这里补一层内容指纹。
    sig = quality_stage.file_signature(path, with_hash=True) if _nonempty(path) else {}
    prev_sig = prev.get("artifact") or {}
    same_path = prev.get("path") == path
    if sig.get("sha256"):
        same_content = (bool(prev_sig.get("sha256"))
                        and prev_sig.get("sha256") == sig["sha256"])
    else:
        # 哈希算不出（文件被占用 / IO 异常）→ 退回旧的纯路径判据，
        # 不制造无谓的「验收反复失效」抖动（宁可保守沿用原行为）。
        same_content = same_path
    review_keep = bool(same_path and same_content)
    items[key] = {
        "episode_no": int(episode_no),
        "project": project_name,
        "path": path,
        "filename": os.path.basename(path),
        "size": os.path.getsize(path) if _nonempty(path) else 0,
        "meta": meta or {},
        "artifact": sig,
        "created_at": prev.get("created_at") or _now(),
        "updated_at": _now(),
        # 重新生产会重置验收状态（内容已变，旧结论失效）
        "review": (prev.get("review") or "pending") if review_keep else "pending",
    }
    if review_keep:
        if prev.get("approval"):
            items[key]["approval"] = prev["approval"]
    else:
        # 内容变了 → 明确留痕，别让用户对着「已验收」察觉不到换了片
        if same_path and prev_sig and not same_content:
            items[key]["review_note"] = ("产物内容已变（同路径覆盖），"
                                        "原验收结论已自动失效")
            logger.warning("交付物内容变化但路径未变，验收结论已复位：项目 %s 第 %s 集",
                           project_name, episode_no)
    atomic_write_json(idx_path, data)
    return items[key]


def list_deliverables(project_name: str = "") -> list:
    A = _A()
    root = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot")
    projects = [project_name] if project_name else (
        sorted(os.listdir(root)) if os.path.isdir(root) else [])
    out = []
    for pj in projects:
        p = os.path.join(root, pj, "deliverables.json")
        # A-4 补充：索引损坏时不再「静默跳过该项目的全部成片」，改为显式记 error
        # 后跳过（保持「只读列表视图不 500」的原契约）。
        try:
            data = read_json_strict(p, {}) or {}
        except (ValueError, OSError) as e:
            logger.error("交付物索引损坏且无可用 .bak，项目 %s 的成片本次不列出：%s", pj, e)
            continue
        for v in (data.get("items") or {}).values():
            if isinstance(v, dict):
                v = dict(v)
                v["exists"] = _nonempty(v.get("path") or "")
                v["url"] = f"/api/autopilot/deliverable/file/{pj}/{v.get('filename')}"
                # 验收有效性快检（**只比 size/mtime，不做 sha256**）：成片动辄几百 MB，
                # 列表接口逐个哈希会把界面拖死。真哈希交给 validate_deliverable_review。
                _ap, _sig = v.get("approval") or {}, v.get("artifact") or {}
                if _ap and _sig.get("size") is not None:
                    try:
                        _st = os.stat(v.get("path") or "")
                        if (int(_sig["size"]) != _st.st_size
                                or abs(float(_sig.get("mtime") or 0)
                                       - _st.st_mtime) > 0.001):
                            v["approval_stale"] = True
                    except OSError:
                        v["approval_stale"] = True
                out.append(v)
    out.sort(key=lambda x: (x.get("project") or "", int(x.get("episode_no") or 0)))
    return out


def set_deliverable_review(project_name: str, episode_no: int, review: str,
                           note: str = "", qa_report: dict = None) -> dict:
    """验收 / 打回（打回会在下次托管轮转时重跑该集）

    review == "accepted" 时把「产物哈希 + 合同哈希 + 质检报告哈希」三者绑定到该集
    （见 quality_stage.make_binding）。之后任一变化，批准即自动失效 ——
    这是防「批完又重渲、结论静默延续」的关键。
    """
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    # A-4：统一走严格读；「文件不存在→{}」的既有契约不变，损坏时先试 .bak，
    # 无 .bak 才抛错（app.py:2117 的调用点已有 try/except 兜底，不会 500）。
    data = read_json_strict(idx_path, {}) or {}
    items = data.setdefault("items", {})
    item = items.get(str(int(episode_no)))
    if not item:
        return {}
    item.update({"review": review, "review_note": note, "reviewed_at": _now()})
    if review == "accepted":
        binding = quality_stage.make_binding(
            item.get("path") or "",
            contract=(item.get("meta") or {}),
            qa_report=qa_report)
        item["approval"] = binding
        # 同步刷新 artifact 指纹：artifact 的语义是「**当前 review 所针对的那份内容**」。
        # 不刷新的话它会停在上一版，后续 record_deliverable 拿**过期基线**比对，
        # 把刚给出的合法验收误判成「内容已变」而复位
        # （verify_quality_stage.py 的 F7 抓住的正是这个 bug）。
        item["artifact"] = {"size": binding.get("artifact_size"),
                            "mtime": binding.get("artifact_mtime"),
                            "sha256": binding.get("artifact_sha256") or ""}
        logger.info("交付物验收已绑定哈希：项目 %s 第 %s 集 artifact=%s",
                    project_name, episode_no,
                    str(binding.get("artifact_sha256") or "")[:12])
    else:
        item.pop("approval", None)
    atomic_write_json(idx_path, data)
    if review == "rejected":
        # 打回 = 该集需要重做：清掉死信状态让流水线重新尝试
        resolve_dead_letter(project_name, episode_no, note="成片被打回，重新生产")
    return item


def validate_deliverable_review(project_name: str, episode_no: int,
                                *, persist: bool = True) -> dict:
    """校验该集的「已验收」是否仍然成立；失效则复位 pending 并留因。

    为什么必须有它：重做一集时产物路径不变（原地覆盖），只看路径的话「已验收」
    会静默延续到新内容上。这里用 quality_stage.check_binding 做**内容级**校验，
    快路径先比 size/mtime（避免为几百 MB 的成片反复算哈希）。

    返回 {"status": valid|invalid|unbound|none, "reason": ..., "item": {...}}。
    一切异常 fail-open：校验不出结果时**不误伤**，保持原 review 不动。
    """
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name,
                            "deliverables.json")
    try:
        data = read_json_strict(idx_path, {}) or {}
    except (ValueError, OSError) as e:
        logger.error("交付物索引损坏，无法校验验收有效性（项目 %s）：%s", project_name, e)
        return {"status": "none", "reason": "索引不可读", "item": {}}
    item = (data.get("items") or {}).get(str(int(episode_no)))
    if not isinstance(item, dict):
        return {"status": "none", "reason": "无该集交付物", "item": {}}
    if item.get("review") != "accepted":
        return {"status": "unbound", "reason": "该集尚未验收", "item": item}
    status, reason = quality_stage.check_binding(
        item.get("approval"), item.get("path") or "",
        contract=(item.get("meta") or {}))
    if status == "invalid" and persist:
        logger.warning("交付物验收已失效（项目 %s 第 %s 集）：%s",
                       project_name, episode_no, reason)
        item.update({"review": "pending",
                     "review_note": "原验收结论已失效：%s，请重新验收" % reason,
                     "approval_invalidated_at": _now(),
                     "approval_invalid_reason": reason})
        item.pop("approval", None)
        atomic_write_json(idx_path, data)
    return {"status": status, "reason": reason, "item": item}

def mark_deliverable_stale(project_name: str, episode_no: int, reason: str,
                           detail: dict = None) -> dict:
    """把该集已登记的成片标记为「已过期」（镜头被重做，成片需要重新合成）

    用户闭环里很关键的一步：对某镜不满意 → 重做该镜 → 但成片还是旧的。
    这里给交付物打标，验收页就能提示「镜头有更新，请重新合成后再验收」，
    而不是让用户对着过期成片点验收。
    """
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    # A-4：损坏不再静默 `return {}`（那会让「成片已过期」的提示彻底消失）。
    # 记录不存在时仍返回 {}（原契约）；损坏时先试 .bak，无 .bak 才抛错。
    data = read_json_strict(idx_path, {}) or {}
    item = (data.get("items") or {}).get(str(int(episode_no)))
    if not isinstance(item, dict):
        return {}
    meta = item.setdefault("meta", {})
    stale = meta.setdefault("stale", {})
    stale.update({"reason": reason, "detail": detail or {}, "marked_at": _now()})
    item["updated_at"] = _now()
    atomic_write_json(idx_path, data)
    return item


def deliverable_path(project_name: str, filename: str) -> str:
    """按「交付物索引」解析真实文件路径（不假设成片被拷贝到 autopilot 目录）

    成片可能来自 output/dub_mix/（混音）或 output/final/（无配音），因此必须以
    索引里登记的路径为准；仅允许索引内的文件被访问，避免目录穿越。
    """
    idx_path = os.path.join(_A().PROJECT_OUTPUT_DIR, "autopilot", project_name,
                            "deliverables.json")
    # A-4 补充：损坏时不再静默返回 ""（等于「文件不存在」，会掩盖真实故障）。
    # 这里保持「解析不出路径 → ""」的原契约（调用方据此 404），但显式记 error；
    # 若存在 .bak，read_json_strict 会先自动恢复，故障可自愈。
    try:
        data = read_json_strict(idx_path, {}) or {}
    except (ValueError, OSError) as e:
        logger.error("交付物索引损坏且无可用 .bak，无法解析成片路径（项目 %s）：%s",
                     project_name, e)
        return ""
    want = os.path.basename(filename)
    for v in (data.get("items") or {}).values():
        if not isinstance(v, dict):
            continue
        if os.path.basename(v.get("path") or "") == want and _nonempty(v.get("path")):
            return v["path"]
    return ""
