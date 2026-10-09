# -*- coding: utf-8 -*-
"""无人值守托管（Autopilot）——让电脑 24 小时自己生产漫剧

目标形态
--------
用户只做两件事：① 上传小说、建项目；② 用总控 AI 对话敲定风格与要求。
之后交给本守护进程：它按章节顺序把每一集从头跑到尾（剧本→配音先行→资产→分镜→
视频→超分→成片，7 步），中途遇到质检不达标自动重试，只有超过阈值才挂起等人工。
用户最终在「成品验收」里点通过 / 打回（或开 auto_accept 自动验收）。

六个设计要点
------------
1. **单线程串行 + 资源互斥**
   只有一块 GPU，多个重任务并行只会互相抢显存导致全部变慢甚至 OOM。因此托管
   固定「同一时刻只跑一集的流水线」；多项目之间按 `priority`（大者先）+
   项目名字序**深度优先推进**——高优先级项目的待产集全部跑完才轮到下一个项目
   （并非 round-robin 轮转）。

2. **失败隔离**
   一集失败只影响这一集：记入死信并继续处理下一集 / 下一个项目。绝不因为某集
   剧本质量差就停掉整条生产线。

3. **幂等 + 可续跑**
   每集开始前由 pipeline 逐步骤探测产物，已完成的步骤直接跳过；服务重启后
   （`resume_on_start`）自动接着干，不会重复烧 GPU。

4. **死信上限**
   同一集连续失败达 `max_episode_attempts` 次 → 标记「需人工介入」并跳过，
   直到用户在处理里点「重试」或「忽略」。

5. **可观测**
   实时状态（当前项目 / 集号 / 步骤 / 进度 / 已跑时长 / 累计重试数 / 异常清单）
   全部暴露给前端；另有 24 小时生产曲线（每集完成时间与耗时）。

6. **可暂停（且秒级生效）**
   中止检查点分两类：① 步骤边界；② 步骤内部的「发起 LLM 调用前 / 重试退避前」
   （见 `cancellation.py`）。两类都落在**尚未产出文件**的位置 —— 因此既不会打断
   正在跑的 GPU 渲染、不留半成品，又不会出现「点了暂停却要等十几分钟才停」。
   实测病根：单步内嵌套着两层重试（步骤级 × 提额级 × HTTP 级）却从不检查中止信号。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

# ===================== 状态与持久化 =====================

_LOCK = threading.RLock()          # 保护 _STATE（可重入：内部函数会互相调用）
_THREAD = None                     # 守护线程
_WAKE = threading.Event()          # 立即唤醒（新任务 / 启停时）
_STOP = threading.Event()          # 进程退出信号
_RUNTIME_RESTORED = False          # 运行时状态（暂停标记）是否已从磁盘恢复

#: 托管运行态（内存；供前端高频读取）
_STATE = {
    "running": False,              # 守护线程是否在跑
    "paused": False,               # 是否已暂停
    "current": None,               # {"project","episode","step","message","percent","started_at"}
    "last_error": "",
    #: 项目名 → 上一次「真正跑完的一集」的结果（成功/失败都记，带项目归属、落盘可跨重启）。
    #: 2026-09-30 新增：current 会被 _clear_current() 清空，之后总控/前端就再也无从
    #: 知道上一集成没成 —— last_run 就是补这个洞的口径。
    "last_runs": {},
    "cycle": 0,                    # 已完成多少轮轮转
    "started_at": "",
    "checked_at": "",
    "totals": {"episodes_done": 0, "episodes_failed": 0, "retries": 0},
    #: 2026-10-08：单集失败「自动暂停」标记 —— stop_on_failure=True 时，一集生产
    #: 失败会置 paused=True 并在此记下失败的项目/集/原因，前端与总控据此醒目提醒
    #: 「第N集失败已暂停，请处理」；resume 时清除。未失败为空 dict。
    "failure_pause": {},
}

#: 每个项目连续失败计数（项目名 → {集号: 次数}）
#:
#: ⭐ 2026-10-02：**必须落盘**（原为纯内存 → 重启即清零）。
#: 为什么（实测教训）：`max_episode_attempts=2` 的判据在 `_pick_next`，
#: 它读的就是这里。曾经出现「分镜 worker 报成功但文件缺失 → probe 复核判失败 →
#: 每轮全量重跑 6 镜」的循环，指望 2 次后挂起；但守护进程一重启（改代码/手动重启）
#: 计数就归零，于是**死循环白烧 GPU（实测 12:59→13:36 烧 4 轮约 40 分钟）**。
#: 落盘后该机制才真正具备「跨重启」语义。
#: 读写仍以内存为准（热路径不加 IO），只在**变更时**落盘、**启动时**按需载入。
_ATTEMPTS: dict = {}


def _attempts_path(project: str) -> str:
    return os.path.join(_autopilot_dir(project), "attempts.json")


def _save_attempts(project: str) -> None:
    """把某项目的失败计数落盘（失败只告警，绝不阻断生产主流程）。"""
    try:
        data = dict(_ATTEMPTS.get(project) or {})
        atomic_write_json(_attempts_path(project), data)
    except Exception as e:  # noqa: BLE001
        logger.warning("保存 attempts 失败（%s）：%s", project, e)


def _load_attempts(project: str) -> dict:
    """从磁盘载入某项目的失败计数；没有就返回空。

    注意：**键统一转为 int**（JSON 的键会全部变成字符串，不转会与
    `_ATTEMPTS.setdefault(project, {})[no]` 的 int 键对不上 → 计数永远读不到 0 以外）。
    """
    try:
        path = _attempts_path(project)
        if not os.path.isfile(path):
            return {}
        data = read_json_strict(path, {})
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:  # noqa: BLE001  读不到就当没有，绝不阻断
        logger.warning("读取 attempts 失败（%s）：%s", project, e)
        return {}


def _attempts_get(project: str, episode_no: int) -> int:
    """读失败计数；内存没有该项目时**先从盘载入**（重启后仍能读到历史计数）。

    ⭐ 懒加载是关键：若只在进程启动时全量载入，项目是运行时新建/发现的话仍会漏。
    判据「内存里根本没有这个 project 的键」= 尚未接触过 → 载一次。
    """
    with _LOCK:
        d = _ATTEMPTS.get(project)
    if d is None:
        loaded = _load_attempts(project)
        with _LOCK:
            d = _ATTEMPTS.setdefault(project, loaded)
    return int(d.get(episode_no, 0))


def _bump_attempt(project: str, episode_no: int, delta: int = 1) -> int:
    """失败计数 +delta 并落盘，返回新值。"""
    with _LOCK:
        d = _ATTEMPTS.setdefault(project, {})
        d[episode_no] = int(d.get(episode_no, 0)) + int(delta)
        val = d[episode_no]
    _save_attempts(project)
    return val


def _reset_attempt(project: str, episode_no: int) -> None:
    """成功（或超限挂起后）清零并落盘。"""
    with _LOCK:
        _ATTEMPTS.setdefault(project, {})[episode_no] = 0
    _save_attempts(project)

#: 上一轮实际生产的 (项目, 集号)，以及同一集连续被生产的次数
#: 用于兜住「状态判定异常导致无限重跑同一集」这类问题（实测出现过 1 秒 121 次），
#: 对 24/7 进程这是必须的保命机制。
_LAST_RUN: dict = {"key": None, "repeat": 0, "at": 0.0}

#: 同一集两次生产之间的最小间隔（秒）：被打回/需重做时避免紧凑空转
MIN_RERUN_INTERVAL = 45

#: 托管轮询间隔（秒）：没有可做的活时休眠多久再扫一次
IDLE_SLEEP = 20

#: 单步骤停滞告警阈值（秒）：当前步骤的进度（message/percent）超过该时长无推进，
#: 就在 status() 的 current 里上浮 stall_warning，让前端/总控能提示「卡在这一步多久了」。
#: 这是报告 P1-5「生产无超时反馈」的补口：LLM 重试等场景会长时间停在同一 step，
#: 用户此前只能看到「执行中」干等。阈值取 15 分钟（与报告建议的「单步骤超 N 分钟」一致）。
STEP_STALL_WARN_SEC = 15 * 60

#: 默认托管计划
PLAN_DEFAULTS = {
    "enabled": False,              # 是否纳入托管
    "episodes": "all",             # "all" 或 [1,2,3]
    "priority": 0,                 # 数字越大越先生产
    "max_episode_attempts": 2,     # 同一集连续失败上限，超过即挂起等人工
    # ---- 传给 pipeline 的生产配置 ----
    "style": "",
    "target_shots": 12,
    # video_mode 已废弃（2026-10-01 起视频只有整集模式，pipeline 会强制归一为 episode）
    "enable_assets": True,
    "enable_video": True,
    "enable_final": True,
    # 「每角色参考音色」生成开关（tts_pre 步）：True = 剧本后为每个角色补一段参考音色，
    # 供 H3 以 audioMode=generate 锁定角色音色并自生成对白。
    # （enable_tts / enable_mix / enable_keyframe 已随 tts/mix/keyframe 步骤下线移除：
    #   计划里再设置这些键会被接口静默忽略。）
    "enable_tts_pre": True,
    # 超分（FlashVSR）：默认开启。必须列进 PLAN_DEFAULTS ——
    # api_autopilot_plan_set 会按 `k in PLAN_DEFAULTS` 过滤入参，
    # 不在此处的字段无法通过接口关闭，等于没有关掉的入口。
    "enable_upscale": True,
    "upscale_scale": 2,
    "coverage_min_percent": 95.0,
    "consistency_min_score": 80,
    "require_consistency": True,
    "step_max_retries": 2,
    "auto_repair": True,
    "overwrite_script": False,
    # ---- 无人值守（24h 托管）相关 ----
    # 挂起等人工的集，超过 N 小时自动复活重试。0 = 永不复活、必须人工处理。
    # 没有这个开关时，24h 跑一夜会攒下一堆「等人工」的集，第二天全堵在那儿。
    "auto_revive_hours": 6.0,
    # 成片产出后自动验收（不再堆在「待验收」里等人点）。
    # 注意：待验收**并不阻塞**后续集生产，这个开关只是消除人工动作、让看板干净。
    "auto_accept": False,
    # 单集失败是否「自动暂停等人工」：True = 一集生产失败就全局暂停并落异常
    #（前端/总控会醒目提醒，用户处理后 resume 继续）；False = 旧的「失败隔离」
    # 行为（单集失败记一次并跳到下一集，不打断整批）。
    # ⭐ 2026-10-08 修复：此前无此开关，单集失败被静默跳过、用户不知道 ——
    #   「第1集分镜图缺10镜 → 直接跳第2集 → 用户点开才发现」就是这个坑。
    "stop_on_failure": True,
}


def _A():
    """延迟取宿主模块 app（避开循环导入：app 在加载期就会 import 本模块）"""
    import sys as _sys
    mod = _sys.modules.get("app")
    if mod is None:
        raise RuntimeError("宿主模块 app 尚未加载")
    return mod


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _autopilot_dir(project: str = "") -> str:
    root = os.path.join(_A().PROJECT_OUTPUT_DIR, "autopilot")
    return os.path.join(root, project) if project else root


def _nonempty(p: str) -> bool:
    try:
        return bool(p) and os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def _write_json(path: str, data) -> None:
    """原子写 JSON（A-3）：唯一临时名 + fsync + .bak 快照 + replace 重试。"""
    atomic_write_json(path, data)


def _read_json(path: str, default):
    """严格读 JSON（A-4）：缺失→default；损坏→从 .bak 恢复；无 .bak→抛错。

    旧实现 `except Exception: return default` 会把「文件损坏」静默读成「空」，
    下游 `set_plan` 这类「读改写」再基于空快照写回 → 计划被永久清空。
    """
    return read_json_strict(path, default)


# ===================== 运行时状态持久化（暂停状态跨重启保持） =====================
# 为什么需要：托管会在服务启动时自动恢复生产（24/7 的核心诉求）。但如果用户是
# 主动「暂停」的（例如要检修 ComfyUI、腾出显存做别的事），重启后若无条件恢复，
# 就会在用户没预期的情况下立刻启动重任务。因此把「是否暂停」落盘，启动时尊重它。

def runtime_path() -> str:
    return os.path.join(_autopilot_dir(), "runtime.json")


def _persist_runtime() -> None:
    with _LOCK:
        data = {"paused": bool(_STATE.get("paused")),
                "pause_reason": _STATE.get("pause_reason") or "",
                "updated_at": _now()}
    try:
        _write_json(runtime_path(), data)
    except Exception as e:  # noqa: BLE001  落盘失败不得影响主流程
        logger.warning("托管运行时状态落盘失败（不影响本次运行）：%s", e)


def _restore_runtime() -> dict:
    """把上次的暂停状态读回内存；无记录则保持默认（未暂停）"""
    global _RUNTIME_RESTORED
    data = _read_json(runtime_path(), {}) or {}
    paused = bool(data.get("paused"))
    with _LOCK:
        _STATE["paused"] = paused
        _STATE["pause_reason"] = str(data.get("pause_reason") or "") if paused else ""
    _RUNTIME_RESTORED = True
    if paused:
        logger.info("托管：恢复上次的暂停状态（%s）", _STATE.get("pause_reason") or "无原因")
    return {"paused": paused, "pause_reason": _STATE.get("pause_reason") or ""}


def _restore_once() -> None:
    """懒加载式恢复（只做一次）：让 status()/is_paused() 在重启后立即反映上次的暂停状态"""
    if _RUNTIME_RESTORED:
        return
    try:
        _restore_runtime()
    except Exception as e:  # noqa: BLE001  读取失败不得影响服务
        # A-4：runtime.json 损坏且无 .bak 时 fail-loud，但「恢复暂停状态」失败
        # 不能阻断服务启动，故在此边界降级为「未暂停」——必须是显式 error 级别，
        # 不能再是 debug（旧实现等于静默）。
        logger.error("托管运行时状态恢复失败（按未暂停处理）：%s", e)
        globals()["_RUNTIME_RESTORED"] = True


# ===================== 托管计划（每个项目一份） =====================


def plan_path(project_name: str) -> str:
    return os.path.join(_autopilot_dir(project_name), "plan.json")


def get_plan(project_name: str) -> dict:
    """读托管计划（缺字段补默认值）。

    A-4 边界决策：`_read_json` 对「损坏且无 .bak」会 fail-loud 抛错，但
    `get_plan` 被 app.py 多个**只读**接口直接调用（无法在本文件内为其加保护），
    且 plan.json 属于「缺了可用 PLAN_DEFAULTS 兜底」的配置类文件（非不可再生数据）。
    因此在这里显式记 error 后降级为默认计划 —— 是「响亮的降级」而非静默清空。
    ⚠️ 注意 `set_plan` 会基于 `get_plan` 的结果回写；降级后回写的是一份「默认计划」，
    但它比「损坏内容」更有意义，且此时 .bak 恢复已在 read_json_strict 内尝试过。
    """
    plan = dict(PLAN_DEFAULTS)
    try:
        data = _read_json(plan_path(project_name), {}) or {}
    except (ValueError, OSError) as e:
        logger.error("项目 %s 的 plan.json 损坏且无可用 .bak，本次按默认计划处理：%s",
                     project_name, e)
        return plan
    plan.update(data)
    # ⭐ 视频生成方式以**项目级设定**为唯一权威（新建项目时用户选择，2026-09-29）。
    #    为什么必须在这里派生：plan.json 一旦被写过，就永久带着 PLAN_DEFAULTS 的
    #    video_mode='episode'；若直接采信它，用户在「新建项目」里选的「逐镜生成」
    #    在托管生产里会被静默顶成整集 —— 表现为「我选了但没生效」。
    #    set_plan 会把显式传入的 video_mode 写回项目配置（见下），因此 AI 总控 /
    #    一键启动对它的覆盖仍然生效，且全项目只有一个事实来源。
    try:
        import project_store as _ps          # 函数内导入：autopilot 由 app 反向导入，
        _vm = _ps.video_mode(project_name)   # 模块级导入链越短越不容易踩到顺序问题
        if _vm:
            plan["video_mode"] = _vm
    except Exception as e:                     # noqa: BLE001 - 派生失败沿用计划值，不阻断
        logger.debug("读取项目视频生成方式失败（沿用计划值）：%s", e)
    return plan


def set_plan(project_name: str, patch: dict, novel_id: str = "") -> dict:
    """写入托管计划（只接受已知字段，避免脏数据污染）"""
    plan = get_plan(project_name)
    for k, v in (patch or {}).items():
        if k in PLAN_DEFAULTS:
            plan[k] = v
    # ⭐ video_mode 是**项目级**设定：显式改动时同步写回项目配置（单一事实来源）。
    #    不写回的话，下一次 get_plan 会按项目配置把这次修改顶掉 —— 「设置不生效」
    #    且没有任何报错，是最难查的一类漂移。
    if "video_mode" in (patch or {}):
        try:
            import project_store as _ps
            _ps.update_config(project_name, {"video_mode": plan.get("video_mode")})
        except Exception as e:                 # noqa: BLE001 - 写回失败不影响计划本身
            logger.warning("写回项目视频生成方式失败（计划内仍生效）：%s", e)
    if novel_id:
        plan["novel_id"] = novel_id
    plan["updated_at"] = _now()
    _write_json(plan_path(project_name), plan)
    wake()
    return plan


def list_plans() -> list:
    """列出所有项目的托管计划（含未启用的，供前端展示全貌）"""
    A = _A()
    out = []
    seen = set()
    root = _autopilot_dir()
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if not os.path.isdir(os.path.join(root, name)):
                continue
            seen.add(name)
            out.append({"project": name, **get_plan(name)})
    # 项目注册表里存在但还没有托管计划的，也一并列出（默认为未启用）
    try:
        for rec in A.project_store.list_projects(with_stats=False):
            key = rec.get("dir_key") or rec.get("name")
            if key and key not in seen:
                out.append({"project": key, "name": rec.get("name"),
                            **get_plan(key)})
                seen.add(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("项目注册表读取失败（仅展示已有托管计划）：%s", e)
    out.sort(key=lambda x: (-int(x.get("priority") or 0), x.get("project") or ""))
    return out


def enable(project_name: str, patch: dict = None) -> dict:
    p = set_plan(project_name, {**(patch or {}), "enabled": True})
    _ensure_thread()
    return p


def disable(project_name: str) -> dict:
    return set_plan(project_name, {"enabled": False})


def enabled_projects() -> list:
    return [p for p in list_plans() if p.get("enabled")]


# ===================== 集列表与进度 =====================


def _novel_meta(project_name: str, plan: dict) -> dict:
    """取该项目关联的小说元信息（优先计划里记录的 novel_id）"""
    A = _A()
    novel_id = (plan.get("novel_id") or "").strip()
    if not novel_id:
        try:
            rec = A.project_store.get_project(project_name) or {}
            novel_id = (rec.get("novel_id") or "").strip()
        except Exception:  # noqa: BLE001
            novel_id = ""
    if not novel_id:
        return {}
    try:
        return A.get_novel(A.NOVELS_DIR, novel_id) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("小说元信息读取失败（%s）：%s", novel_id, e)
        return {}


def chapters_and_text(novel_meta: dict) -> tuple:
    """返回 (章节列表, 小说全文)。

    拆章（一章拆多集）需要在**正文**上找语义切点，正文此前已在这里读过一次，
    所以直接把全文一并带出，避免调用方二次读盘。
    """
    A = _A()
    if not novel_meta:
        return [], ""
    text = ""
    try:
        text = A.read_novel_text(A.NOVELS_DIR, novel_meta.get("novel_id")) or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("小说正文读取失败：%s", e)
    if not text:
        return [], ""
    items = []
    for c in A.split_chapters(text):
        items.append({
            "index": int(c.get("index") or len(items) + 1),
            "title": c.get("title") or f"第{c.get('index')}章",
            "start": c.get("start") or 0,
            "end": c.get("end") or 0,
            "char_count": c.get("char_count") or 0,
        })
    return items, text


def chapters_of(novel_meta: dict) -> list:
    """把小说切成章节列表（含正文切片），供逐集生产"""
    return chapters_and_text(novel_meta)[0]


def episode_units(chapters: list, plan: dict = None, text: str = "",
                  fixed_parts: int = None) -> list:
    """把选中章节展开成**拍摄单元**（一个单元 = 一集；一章可拆成多集）。

    为什么需要这一层：一集的产品定义是「**1-2 分钟，最长 3 分钟**」
    （``novel_to_script.EPISODE_MAX_SEC``=180）。一章通常写不下这么多内容，
    所以「一章 = 一集」不再是硬约束 —— 字数/内容量过大的章会被
    :func:`novel_to_script.split_chapter_for_episodes` 按语义边界切成多段，
    每段独立成一集，各自预估成片时长都落在上限内。

    ⚠️ 集数由**内容体量**决定（``novel_to_script.EPISODES_PER_CHAPTER`` 当前 =1，
    即不强制拆）——「一章 = 一集」在**预估成片时长不超上限时成立**，超长章才拆。
    短章不硬凑集数（旧行为曾强制「一章 2 集」，实测把 1766 字/15 镜的章拆成
    每集 7~8 镜、约 35 秒，既非一集体量又破坏叙事，已废弃）。

    ⚠️ 2026-09-26 口径变更：拆集判据由「预估**镜数** vs 78」改为
    「预估**成片秒数** vs 180」。前者从产品口径看等于不拆（78 镜 ≈ 6.5 分钟），
    实测《逆天系统》1766 字/章只估 15 镜 → 42 章变成 42 集、每集约 1 分钟出头，
    与「每集 1-2 分钟」的目标集数量级不符；改成时长判据后每章自然拆 2~3 集。

    两条关键约定
    ------------
    1. **单元编号基于「全量章节」依次展开**，与 ``plan.episodes`` 选了哪几章**无关**。
       否则改一次选择范围就会让同一章的集号漂移，已完成产物（``第N集.json`` /
       成片 / 验收记录）全部对不上号。
    2. ``plan.episodes`` 的语义**仍然是章号**（历史计划照旧可用）——
       这里先用 :func:`target_episodes` 解出选中的章号集合，再按单元过滤。

    返回 ``[{"episode_no", "chapter_index", "part", "parts", "chapter"}]``；
    其中 ``chapter`` 是按 part **收窄了 start/end** 的副本（未拆章时原样不变），
    可直接交给 ``pipeline.run_episode(..., chapter=...)`` —— 它也按
    ``novel_text[start:end]`` 切片，因此下游无需任何改动。
    """
    if not chapters:
        return []
    A = _A()
    nts = getattr(A, "novel_to_script", None)
    if nts is None:  # 理论上不会发生（app 必然导入它）；降级为「一章一集」
        selected = set(target_episodes(chapters, plan or {}))
        return [{"episode_no": int(c.get("index") or 0),
                 "chapter_index": int(c.get("index") or 0),
                 "part": 1, "parts": 1, "chapter": c}
                for c in chapters if int(c.get("index") or 0) in selected]

    selected = set(target_episodes(chapters, plan or {}))
    units_all = []
    no = 0
    for ch in chapters:
        idx = int(ch.get("index") or 0)
        try:
            # 2026-10-03 用户决策：改回「一章 = 一集」（不再按内容体量拆分）。
            # 传超大 max_sec 顶掉分集上限判据，estimate_episode_parts 恒为 1；
            # 如需恢复按内容拆分，设 env MJSCXT_EPISODE_SPLIT=1。
            _no_split = str(os.environ.get("MJSCXT_EPISODE_SPLIT") or "").strip().lower() \
                not in ("1", "true", "yes", "on")
            segs = nts.split_chapter_for_episodes(
                ch, text or "", fixed_parts=fixed_parts,
                max_sec=(10 ** 9) if _no_split else None)
        except Exception as e:  # noqa: BLE001
            logger.warning("第%s章拆章失败（按不拆处理）：%s", idx, e)
            segs = [{"part": 1, "parts": 1, "start": ch.get("start") or 0,
                     "end": ch.get("end") or 0, "char_count": ch.get("char_count") or 0}]
        for seg in segs:
            no += 1
            narrow = dict(ch)
            narrow["start"] = seg["start"]
            narrow["end"] = seg["end"]
            narrow["char_count"] = seg["char_count"]
            if int(seg.get("parts") or 1) > 1:
                base = str(ch.get("title") or f"第{idx}章")
                narrow["title"] = f"{base}·第{seg['part']}部分"
            units_all.append({
                "episode_no": no,
                "chapter_index": idx,
                "part": int(seg.get("part") or 1),
                "parts": int(seg.get("parts") or 1),
                "chapter": narrow,
            })
    return [u for u in units_all if u["chapter_index"] in selected]


def find_episode_unit(chapters: list, plan: dict, episode_no: int,
                      text: str = "", fixed_parts: int = None) -> dict:
    """按集号取回该集的拍摄单元（找不到返回 {}）。

    替代历史写法 ``next((c for c in chapters if c["index"] == ep), {})`` ——
    拆章后「集号」不再等于「章号」，必须走单元表反查。
    """
    try:
        want = int(episode_no)
    except (TypeError, ValueError):
        return {}
    for u in episode_units(chapters, plan, text, fixed_parts=fixed_parts):
        if int(u.get("episode_no") or 0) == want:
            return u
    return {}


def target_episodes(chapters: list, plan: dict) -> list:
    """按计划里的 episodes 配置挑出要生产的集号"""
    all_no = [c["index"] for c in chapters]
    sel = plan.get("episodes", "all")
    if sel == "all" or sel is None:
        return all_no
    if isinstance(sel, (int, float)):
        return [int(sel)] if int(sel) in all_no else []
    if isinstance(sel, str):
        parts = [p.strip() for p in sel.replace("，", ",").split(",") if p.strip()]
        try:
            want = {int(p) for p in parts}
        except ValueError:
            return all_no
        return [n for n in all_no if n in want]
    if isinstance(sel, (list, tuple)):
        want = {int(x) for x in sel}
        return [n for n in all_no if n in want]
    return all_no


def deliverables_map(project_name: str) -> dict:
    """该项目的验收索引 {集号: item}

    注意：必须走 pipeline.list_deliverables（它会实时补 `exists` 字段），
    不能直接读 JSON —— 落盘的条目**没有** exists 键，直接读会把所有已产出
    的集误判为「未完成」，进而让守护进程无限重跑同一集（实测踩过：1 秒内
    重复生产 121 次）。
    """
    import pipeline
    out = {}
    for v in pipeline.list_deliverables(project_name):
        try:
            out[int(v.get("episode_no"))] = v
        except (TypeError, ValueError):
            continue
    return out


def _episode_state(project_name: str, episode_no: int, plan: dict, chapters: list) -> dict:
    """判断某集当前状态：done / rejected / pending_human / todo"""
    A = _A()
    try:
        import pipeline
    except ImportError:
        return {"state": "todo", "note": "流水线模块不可用"}

    dl = deliverables_map(project_name).get(int(episode_no))
    if dl:
        if dl.get("review") == "rejected":
            return {"state": "rejected", "note": "成片被打回，待重做"}
        if dl.get("exists"):
            return {"state": "done", "note": "已产出成片待验收",
                    "deliverable": dl.get("path"), "review": dl.get("review")}
        return {"state": "todo", "note": "成片文件已丢失，需重做"}
    dead = (pipeline.list_dead_letters(project_name) or [])
    for d in dead:
        if int(d.get("episode_no") or 0) == int(episode_no) and not d.get("resolved"):
            return {"state": "pending_human", "note": d.get("reason") or "需人工介入"}
    return {"state": "todo", "note": ""}


def project_progress(project_name: str, plan: dict = None) -> dict:
    """单个项目的逐集进度（供前端「分集进度」视图）

    行以**拍摄单元**为单位（一章拆多集时会出现多行、集号连续、
    ``chapter_index`` 相同而 ``part`` 不同）。
    """
    plan = plan or get_plan(project_name)
    meta = _novel_meta(project_name, plan)
    chapters, text = chapters_and_text(meta)
    units = episode_units(chapters, plan, text)
    rows = []
    done = 0
    for u in units:
        no = int(u["episode_no"])
        ch = u["chapter"]
        st = _episode_state(project_name, no, plan, chapters)
        if st["state"] == "done":
            done += 1
        rows.append({"episode_no": no,
                     "chapter_index": u["chapter_index"],
                     "part": u["part"], "parts": u["parts"],
                     "title": ch.get("title") or f"第{no}章",
                     "char_count": ch.get("char_count") or 0, **st})
    return {
        "project": project_name,
        "novel_id": meta.get("novel_id") or "",
        "novel_title": meta.get("title") or "",
        "enabled": bool(plan.get("enabled")),
        "total": len(units),
        "done": done,
        "percent": round(done / len(units) * 100, 1) if units else 0.0,
        "chapters_found": len(chapters),
        "episodes": rows,
    }


def all_progress() -> list:
    out = []
    for plan in list_plans():
        try:
            out.append(project_progress(plan["project"], plan))
        except Exception as e:  # noqa: BLE001
            logger.warning("项目 %s 进度计算失败：%s", plan.get("project"), e)
    return out


# ===================== 生产历史（24 小时曲线） =====================


def _history_path(project_name: str) -> str:
    return os.path.join(_autopilot_dir(project_name), "history.jsonl")


def append_history(project_name: str, episode_no: int, result: dict) -> None:
    p = _history_path(project_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    row = {
        "project": project_name, "episode_no": int(episode_no),
        "ok": bool(result.get("ok")), "status": result.get("status"),
        "elapsed_sec": result.get("elapsed_sec"),
        "steps": {k: (v or {}).get("status") for k, v in (result.get("steps") or {}).items()},
        "error": (result.get("error") or "")[:300],
        "at": _now(), "ts": time.time(),
    }
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("生产历史写入失败：%s", e)


def read_history(project_name: str = "", hours: int = 24) -> list:
    A = _A()
    root = _autopilot_dir()
    projects = [project_name] if project_name else (
        sorted(os.listdir(root)) if os.path.isdir(root) else [])
    cutoff = time.time() - hours * 3600
    rows = []
    for pj in projects:
        p = _history_path(pj)
        if not _nonempty(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get("ts") or 0) >= cutoff:
                        rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda x: x.get("ts") or 0)
    return rows


def production_curve(hours: int = 24, buckets: int = 24, project: str = "") -> dict:
    """把生产历史聚合成「每小时完成集数」曲线，并给出吞吐与平均耗时

    传入 project 时只统计该项目的生产历史 —— 否则一个从没跑过的新项目
    也会显示别的项目的产量，让人误以为「我的项目已经出片了」。
    """
    rows = read_history(project, hours=hours)
    if not rows:
        return {"hours": hours, "buckets": [], "episodes_done": 0,
                "episodes_failed": 0, "avg_elapsed_sec": 0, "throughput_per_hour": 0.0}
    now = time.time()
    span = max(hours * 3600 / max(buckets, 1), 1)
    series = [{"label": "", "done": 0, "failed": 0} for _ in range(buckets)]
    for i in range(buckets):
        start = now - (buckets - i) * span
        series[i]["label"] = time.strftime("%H:%M", time.localtime(start + span / 2))
    for row in rows:
        idx = int((buckets - 1) - ((now - (row.get("ts") or now)) // span))
        idx = max(0, min(buckets - 1, idx))
        series[idx]["done" if row.get("ok") else "failed"] += 1
    done = sum(1 for r in rows if r.get("ok"))
    failed = len(rows) - done
    secs = [r.get("elapsed_sec") or 0 for r in rows if r.get("ok")]
    avg = round(sum(secs) / len(secs), 1) if secs else 0
    return {
        "hours": hours, "buckets": series,
        "episodes_done": done, "episodes_failed": failed,
        "avg_elapsed_sec": avg,
        "throughput_per_hour": round(done / max(hours, 1), 2),
        "last_at": rows[-1].get("at"),
    }


# ===================== 托管主循环 =====================


def pause(reason: str = "") -> dict:
    with _LOCK:
        _STATE["paused"] = True
        _STATE["pause_reason"] = reason
    _persist_runtime()
    logger.info("托管已暂停：%s", reason or "手动暂停")
    return status()


def resume() -> dict:
    """解除全局暂停并确保守护线程在跑（是否有活由循环自己判断）"""
    with _LOCK:
        _STATE["paused"] = False
        _STATE["pause_reason"] = ""
        _STATE["failure_pause"] = {}
    _persist_runtime()
    _ensure_thread()
    wake()
    return status()


def is_paused() -> bool:
    _restore_once()
    with _LOCK:
        return bool(_STATE["paused"])


def _halt_requested() -> bool:
    """是否应立即停止**当前这一集**：托管暂停，或进程正在退出。

    作为中止判定器传给 `pipeline.run_episode`，会被注册进执行上下文，
    使集内所有 LLM 调用与重试退避都能秒级感知（见 `cancellation.py`）。
    """
    return _STOP.is_set() or is_paused()


def wake() -> None:
    """唤醒守护线程立即扫一轮（新任务入队 / 配置变更时调用）"""
    _WAKE.set()


def _ensure_thread() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="autopilot", daemon=True)
        _THREAD.start()
        _STATE["running"] = True
        _STATE["started_at"] = _STATE.get("started_at") or _now()
        logger.info("托管守护线程已启动")


def stop(timeout: float = 5.0) -> None:
    """进程退出时优雅停止（步骤边界生效）"""
    _STOP.set()
    wake()
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    with _LOCK:
        _STATE["running"] = False


#: 生产阶段的中文人话名。`pipeline.STEP_LABELS` 是「环节」级（剧本/资产/分镜…），
#: 这里是「环节内子阶段」级 —— phase 形如 `coverage`、`storyboard:2`（冒号后是子序号）。
#: ⚠️ 只用于**显示**（前端进度条 / 总控汇报），**不参与任何判据**，所以缺项时
#: 回退到环节名即可，不必穷举。
PHASE_LABELS_ZH = {
    "start": "准备中",
    "outline": "提炼大纲",
    "bible": "构建设定集",
    "shots": "拆分镜头",
    "coverage": "原文覆盖率校验",
    "script": "剧本生成",
    "tts_pre": "配音先行（角色参考音色）",
    "assets": "生成资产（角色/物品/场景）",
    "storyboard": "生成分镜图",
    "video": "生成视频",
    "final": "合成成片",
    "upscale": "超分放大",
    "qc": "质量质检",
    "retry": "重试中",
    "done": "已完成",
    # 旧任务状态回放兜底：tts / mix / keyframe 均已下线（2026-10-04/05），
    # 历史状态文件里若出现这些 phase，显示为「（已下线）」而不是旧环节名，
    # 避免回放时再向用户播报「配音/混音」口径。
    "tts": "配音（已下线）",
    "mix": "混音（已下线）",
    "keyframe": "尾帧（已下线）",
}


def describe_current(cur: dict) -> str:
    """把 current 状态压成**一句话人话**，让用户一眼知道「现在在生成什么」。

    背景：`current` 里的 raw 字段（`step` / `phase` / `percent`）是给机器看的 ——
    用户在总控面板只能看到「总控执行中 · 已完成 N 步」，完全不知道后台在拍哪一集、
    走到哪个环节。这里统一产出一句可直接展示的描述。

    ⚠️ 纯展示用途，**任何判据都不得依赖本函数的输出文本**（措辞会随需求改）。
    """
    if not isinstance(cur, dict) or not cur:
        return ""
    parts = []
    ep = cur.get("episode")
    title = str(cur.get("title") or "").strip()
    if ep not in (None, ""):
        try:
            parts.append(f"第 {int(ep)} 集")
        except (TypeError, ValueError):
            pass
    if title:
        parts.append(title)
    # 阶段名：优先更细的 phase（去掉 `:子序号` 后缀），回退到 step
    phase = str(cur.get("phase") or "").split(":")[0].strip()
    step = str(cur.get("step") or "").split(":")[0].strip()
    label = PHASE_LABELS_ZH.get(phase) or ""
    if not label:
        try:
            import pipeline as _pl
            label = _pl.STEP_LABELS.get(step) or ""
        except Exception:  # noqa: BLE001  展示降级：拿不到名字也不影响主链路
            label = ""
    if not label:
        label = step or "生产中"
    parts.append(label)
    # 2026-10-08（用户要求）：把步骤内的**细粒度进度**（worker 逐镜上报，如
    #    「分镜 16/41」「渲染第 5/18 场」）也拼进描述。此前只有「分镜图 · 34%」，
    #    用户看不出在拍第几镜，只能看到「已运行 N 分钟未推进」的停滞提示。
    _msg = str(cur.get("message") or "").strip()
    if _msg and _msg not in parts:
        parts.append(_msg)
    try:
        pct = int(cur.get("percent") or 0)
    except (TypeError, ValueError):
        pct = 0
    return f"{' · '.join(parts)} · {pct}%"


def report_progress(message: str = "", percent=None) -> None:
    """步骤内**细粒度**进度上报（供 storyboard / video worker 逐镜调用）。

    动机（2026-10-08 用户反馈）：前端此前只能看到「视频生成 · 50%」配一句
    「已运行 N 分钟未推进」—— 分不清是正常渲染还是真卡住，也不知道在拍第几镜。
    worker 逐镜上报后，前端直接显示「第 1 集 · 视频生成 · 渲染第 5/18 场 · 50%」。

    与 pipeline.progress_cb 的分工（**不要混用**）：
      · progress_cb 负责**跨步骤**迁移 —— 维护 steps_done / retries / step；
      · 本函数只负责**步骤内**刷新 message / percent，**绝不改** step、steps_done、
        retries（改了会把「跑到哪一步」的进度链搅乱）。

    副作用（有意）：每次上报都会刷新 step_updated_at，因此正常推进的步骤不再
    触发「已运行 N 分钟未推进」的误报停滞告警。

    线程安全：走 _set_current（内部持 _LOCK）。无 current（未在生产）时静默返回。
    """
    try:
        with _LOCK:
            if not _STATE.get("current"):
                return
        kw = {"message": str(message or "")}
        if percent is not None:
            try:
                kw["percent"] = max(0, min(100, int(percent)))
            except (TypeError, ValueError):
                pass
        _set_current(**kw)
    except Exception as e:  # noqa: BLE001  纯展示用途，绝不能影响生产主链路
        logger.debug("步骤内进度上报失败（忽略）：%s", e)


def _set_current(**kw) -> None:
    with _LOCK:
        cur = dict(_STATE.get("current") or {})
        cur.update(kw)
        # 每次进度更新都刷新「最近推进时刻」（epoch 秒），供 status() 算停滞时长。
        # 用 epoch 而非格式化字符串：status() 要做 now - 该值 的减法。
        cur["step_updated_at"] = time.time()
        # 冗余一份人话描述，前端 / 总控直接取用，不必各自维护阶段名映射表。
        cur["describe"] = describe_current(cur)
        _STATE["current"] = cur


def _clear_current() -> None:
    with _LOCK:
        _STATE["current"] = None


def _last_run_path(project: str) -> str:
    return os.path.join(_autopilot_dir(project), "last_run.json")


def read_last_run(project: str) -> dict:
    """读某项目「上一次真正跑完的一集」的结果（成功/失败都记）。

    为什么要它（2026-09-30 实测）：总控 produce_episode 走 run-once，worker 失败时
    只 logger.error 再 _clear_current()，既不落死信也不写 last_error ⇒ status() 返回
    current=null / exceptions=0 / last_error=""，总控 get_status 看到的是「空闲且零异常」，
    于是第1集的 401 失败永远汇报不出来。last_run 带项目归属并落盘，补上这个口径。
    """
    with _LOCK:
        rec = (_STATE.get("last_runs") or {}).get(project)
    if isinstance(rec, dict) and rec:
        return rec
    try:
        path = _last_run_path(project)
        if os.path.isfile(path):
            data = read_json_strict(path, {})
            return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001  读不到就当没有，绝不阻断状态查询
        logger.warning("读取 last_run 失败（%s）：%s", project, e)
    return {}


def latest_last_run() -> dict:
    """全项目里最近一次跑完的结果（不带 project 查询 status 时用）。"""
    with _LOCK:
        runs = [r for r in (_STATE.get("last_runs") or {}).values()
                if isinstance(r, dict) and r]
    if not runs:
        return {}
    return max(runs, key=lambda r: str(r.get("finished_at") or ""))


def record_run_result(project: str, episode_no: int, ok: bool, status: str = "",
                      error: str = "", deliverable: str = "", elapsed_sec=0,
                      title: str = "", source: str = "run-once") -> dict:
    """登记「一次生产跑完了」的事实 —— 成功与失败都登记，落盘可跨重启。

    与 _STATE["last_error"] 的区别：last_error 是**全局**的、无项目归属，
    status(project) 只能以「可能是别的项目的报错」为由把它藏起来；
    last_run 按项目存盘，因此总控在 current 为空时依然能如实播报上一集的结果。
    """
    rec = {
        "project": project, "episode_no": int(episode_no), "ok": bool(ok),
        "status": str(status or ("done" if ok else "failed")),
        "error": "" if ok else str(error or "未知错误"),
        "deliverable": str(deliverable or ""),
        "elapsed_sec": round(float(elapsed_sec or 0), 1),
        "title": str(title or ""), "source": str(source or ""),
        "finished_at": _now(),
    }
    with _LOCK:
        _STATE.setdefault("last_runs", {})[project] = rec
        _STATE["last_error"] = "" if ok else rec["error"]
    try:
        atomic_write_json(_last_run_path(project), rec)
    except Exception as e:  # noqa: BLE001  落盘失败不影响内存态与生产
        logger.warning("last_run 落盘失败（%s）：%s", project, e)
    if ok:
        logger.info("生产结果登记：%s 第%s集 成功（%s）", project, episode_no,
                    rec["deliverable"] or "无交付物")
    else:
        logger.warning("生产结果登记：%s 第%s集 失败（%s）", project, episode_no,
                       rec["error"][:200])
    return rec


def _loop() -> None:
    """守护主循环：轮转推进各项目的下一待生产集"""
    logger.info("托管循环开始")
    while not _STOP.is_set():
        try:
            did = _one_round()
        except Exception as e:  # noqa: BLE001  循环绝不能因单次异常退出
            logger.error("托管轮转异常：%s\n%s", e, traceback.format_exc())
            with _LOCK:
                _STATE["last_error"] = f"{type(e).__name__}: {e}"
            did = False
        with _LOCK:
            _STATE["checked_at"] = _now()
            _STATE["cycle"] = int(_STATE.get("cycle") or 0) + 1
            _STATE["running"] = True
        if did:
            continue                      # 有活干 → 立刻找下一件
        _WAKE.wait(timeout=IDLE_SLEEP)
        _WAKE.clear()
    with _LOCK:
        _STATE["running"] = False
    logger.info("托管循环已退出")


def _one_round() -> bool:
    """扫描一轮：找到第一个可生产的（项目, 集）并跑完它。返回是否有活可做"""
    if is_paused():
        return False
    for plan in enabled_projects():
        project = plan["project"]
        try:
            _auto_revive(project, plan)      # 先解挂超期的失败集，再挑下一集
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 自动复活扫描失败：%s", project, e)
        try:
            pick = _pick_episode(project, plan)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 集列表计算失败，跳过：%s", project, e)
            continue
        if not pick:
            continue
        _produce(project, plan, pick)
        return True
    return False


def _auto_revive(project: str, plan: dict) -> int:
    """自动复活：把「挂起等人工」超过 auto_revive_hours 的集重新放回队列。

    24h 无人值守下，失败集如果只能靠人工点掉，一夜下来就全堵死了。
    这里按时间自动解挂（默认 6 小时），并留痕说明是系统自动复活的。
    返回复活的集数。
    """
    hours = plan.get("auto_revive_hours")
    try:
        hours = float(hours if hours is not None else PLAN_DEFAULTS["auto_revive_hours"])
    except (TypeError, ValueError):
        hours = float(PLAN_DEFAULTS["auto_revive_hours"])
    if hours <= 0:
        return 0
    try:
        import pipeline
    except ImportError:
        return 0

    revived = 0
    for d in (pipeline.list_dead_letters(project) or []):
        if d.get("resolved"):
            continue
        marked = str(d.get("marked_at") or "").strip()
        try:
            t = time.mktime(time.strptime(marked, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue                       # 时间格式异常 → 不动它，交给人工
        if (time.time() - t) < hours * 3600:
            continue
        try:
            ep = int(d.get("episode_no"))
            pipeline.resolve_dead_letter(project, ep, note=f"自动复活（挂起已超过 {hours:g} 小时）")
            # 复活后要把尝试计数清零，否则下一次失败又会立刻被挂起
            _reset_attempt(project, ep)
            revived += 1
            logger.info("%s 第%s集自动复活（挂起于 %s，已超过 %g 小时）",
                        project, ep, marked, hours)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 自动复活失败：%s", project, e)
    return revived


def _pick_episode(project: str, plan: dict):
    """挑出该项目下一个要生产的集：按顺序找第一个「未完成且不需人工」的集"""
    meta = _novel_meta(project, plan)
    if not meta:
        logger.debug("%s 未关联小说，跳过", project)
        return None
    chapters, text = chapters_and_text(meta)
    if not chapters:
        logger.debug("%s 小说正文为空或无章节，跳过", project)
        return None

    # 拍摄单元表：一章可能拆成多集（超长章），因此循环单位是「单元」而不是「章」
    units = episode_units(chapters, plan, text)
    if not units:
        return None

    # 先看是否已有产出但被打回的集（优先重做，用户明确要求了）
    rows = {r["episode_no"]: r for r in
            (project_progress(project, plan).get("episodes") or [])}
    for u in units:
        no = int(u["episode_no"])
        r = rows.get(no) or {}
        if r.get("state") == "rejected" and _rerun_allowed(project, no):
            return {"episode_no": no, "chapter": u["chapter"],
                    "chapter_index": u["chapter_index"],
                    "part": u["part"], "parts": u["parts"],
                    "reason": "成片被打回，重新生产"}

    # 再按顺序推进第一个未完成的集
    for u in units:
        no = int(u["episode_no"])
        r = rows.get(no) or {}
        st = r.get("state")
        if st == "done":
            continue
        if st == "pending_human":
            continue
        chapter = u["chapter"]
        if not chapter:
            continue
        # 连续失败超限 → 挂起等人工
        tries = _attempts_get(project, no)
        if tries >= int(plan.get("max_episode_attempts") or 2):
            _mark_dead(project, no,
                       f"连续 {tries} 次生产失败，已挂起等人工处理")
            _reset_attempt(project, no)
            continue
        # 防紧凑空转：同一集刚跑过就等一个间隔（打回重做也一样，不必贴着重跑）
        if not _rerun_allowed(project, no):
            continue
        return {"episode_no": no, "chapter": chapter,
                "chapter_index": u["chapter_index"],
                "part": u["part"], "parts": u["parts"],
                "reason": "按章节顺序推进"}
    return None


def _rerun_allowed(project: str, episode_no: int) -> bool:
    """该集是否已过最小重跑间隔（防状态异常导致的无限重跑）"""
    with _LOCK:
        if _LAST_RUN.get("key") != f"{project}#{episode_no}":
            return True
        return (time.time() - float(_LAST_RUN.get("at") or 0)) >= MIN_RERUN_INTERVAL


def _note_run(project: str, episode_no: int, ok: bool) -> None:
    """记录本次生产；同一集连续成功但状态未转为 done → 判定状态同步异常并挂起"""
    key = f"{project}#{episode_no}"
    with _LOCK:
        if _LAST_RUN.get("key") == key:
            _LAST_RUN["repeat"] = int(_LAST_RUN.get("repeat") or 0) + 1
        else:
            _LAST_RUN.update({"key": key, "repeat": 1})
        _LAST_RUN["at"] = time.time()
        repeat = _LAST_RUN["repeat"]
    if ok and repeat >= 3:
        # 产出物登记成功、却仍被判定为「未完成」→ 状态同步出了问题，别再烧 GPU
        _mark_dead(project, episode_no,
                   "已连续生成成功但进度仍未更新，疑似状态同步异常；"
                   "请检查成片文件是否存在、交付物索引是否可写",
                   {"repeat": repeat})
        with _LOCK:
            _LAST_RUN["repeat"] = 0
        return
    if ok:
        with _LOCK:
            _LAST_RUN["repeat"] = 0


def _mark_dead(project: str, episode_no: int, reason: str, detail: dict = None) -> None:
    try:
        import pipeline
        pipeline.mark_dead_letter(project, episode_no, reason, detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("死信标记失败：%s", e)


# ===================== 集间流水线：下一集剧本预热（2026-10-08 用户需求） =====================
# 动机：LLM 与 GPU 是两类**完全不同**的资源，此前整条流水线严格串行 —— 本集「资产生成」
# 在烧 GPU 时 LLM 全程空闲，等资产跑完才轮到剧本。用户要求拆成两条线：资产在跑的同时，
# 把**后续的剧本**（纯 LLM）先生成掉，把这段 LLM 时间从关键路径上摘除。
#
# 为什么选「下一集剧本」这个切入点（最干净的重叠点）：
#   · 纯 LLM，零 GPU，不与本集的资产/分镜/视频渲染抢卡；
#   · 只依赖小说正文 + 设定库，**不依赖本集任何产物** → 零数据依赖、无竞态；
#   · pipeline.step_script 本身幂等（产物存在即 skipped）→ 下一集正式开跑时探针命中、
#     秒过；预热线程随时被打断也不留半成品（写失败=没有文件=下次照常重跑）。
_PREWARM_LOCK = threading.Lock()
_PREWARM_RUNNING: set = set()


def prewarm_next_script(project: str, plan: dict, cur_episode: int) -> bool:
    """在本集生产期间，后台把**下一集剧本**先生成好（与 GPU 步骤并行）。

    调用点：``_produce`` 开跑最初派发一次 —— 覆盖本集全流程（资产/分镜/视频）的 GPU
    空档。整个过程 fail-open：任何异常只记日志，绝不影响本集生产。

    开关：``MJSCXT_PREWARM_NEXT_SCRIPT``（默认 1；置 0 关闭）。

    :returns: 是否**新派发**了预热线程（False = 无需 / 已在跑 / 已关）
    """
    if str(os.environ.get("MJSCXT_PREWARM_NEXT_SCRIPT") or "1").strip().lower() \
            in ("0", "false", "off", "no"):
        return False
    try:
        import pipeline
        meta = _novel_meta(project, plan)
        if not meta:
            return False
        chapters, text = chapters_and_text(meta)
        if not chapters:
            return False
        units = episode_units(chapters, plan, text)
        if not units:
            return False
        nxt = None
        for u in units:
            try:
                no = int(u.get("episode_no"))
            except (TypeError, ValueError):
                continue
            if no > int(cur_episode):
                nxt = (no, u)
                break
        if nxt is None:
            return False
        nxt_no, unit = nxt
        chapter = unit.get("chapter") or {}
        if not chapter:
            return False
        config = pipeline.normalize_config({**plan, "novel_id": meta.get("novel_id") or ""},
                                           default_project_key=project)
        key = (project, int(nxt_no))
        with _PREWARM_LOCK:
            if key in _PREWARM_RUNNING:
                return False            # 单飞：同一集只允许一个预热线程
            _PREWARM_RUNNING.add(key)

        def _pw_run():
            try:
                # 文学剧本改写：与 _produce 同口径（文件存在即用）
                try:
                    import novel_screenplay
                    _md = novel_screenplay.load_screenplay(project, int(nxt_no))
                    if _md:
                        config.setdefault("screenplay_text", _md)
                except Exception:  # noqa: BLE001 设定缺失不影响主流程
                    pass
                ctx = {
                    "config": config, "project_name": project,
                    "project_key": config.get("project_key") or project,
                    "episode_no": int(nxt_no), "episode_tag": f"ep{int(nxt_no):02d}",
                    "novel_meta": meta, "chapter": chapter,
                    "timeout_per_segment": int(config.get("timeout_per_segment") or 900),
                    "script": {}, "logs": [], "steps": {},
                    "progress": lambda *a, **k: None,
                }
                res = pipeline.step_script(ctx) or {}
                logger.info("[集间流水线] 下一集剧本预热完成：第%s集 ok=%s shots=%s（%s）",
                            nxt_no, res.get("ok"),
                            (res.get("detail") or {}).get("shots"),
                            res.get("artifact") or res.get("error") or "")
            except Exception as e:  # noqa: BLE001 预热失败绝不影响本集生产
                logger.warning("[集间流水线] 下一集剧本预热失败（不影响本集）：%s: %s",
                               type(e).__name__, e)
            finally:
                with _PREWARM_LOCK:
                    _PREWARM_RUNNING.discard(key)

        threading.Thread(target=_pw_run, name=f"prewarm-script-ep{nxt_no}",
                         daemon=True).start()
        logger.info("[集间流水线] 已在后台预热第%s集剧本（与本集 GPU 生产并行）", nxt_no)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[集间流水线] 预热派发失败（忽略）：%s", e)
        return False


def _produce(project: str, plan: dict, pick: dict) -> None:
    """跑完一集的流水线，并把结果登记到交付物 / 历史 / 死信"""
    import pipeline

    episode_no = pick["episode_no"]
    chapter = pick["chapter"]
    meta = _novel_meta(project, plan)
    A = _A()
    t0 = time.time()

    # P0-5 门禁（无人值守分支）：托管最怕「静默失败」—— AI 配置不全时若照跑，整晚生产
    # 会全是 401 且只留在日志里。这里直接落死信 + 更新当前态，让问题出现在前端
    # 「异常」列表（可一键处理重跑），而不是被埋掉。
    try:
        import ai_selfcheck
        _gate = ai_selfcheck.gate("episode")
    except Exception as e:  # noqa: BLE001  门禁自身故障不得阻断生产
        logger.error("AI 前置门禁执行异常（按放行处理）：%s", e)
        _gate = {"ok": True}
    if not _gate.get("ok"):
        _reason = _gate.get("message") or "AI 前置自检未通过"
        logger.error("第%s集托管生产被 AI 门禁阻断：%s", episode_no, _reason)
        try:
            pipeline.mark_dead_letter(project, episode_no, _reason,
                                      detail=(_gate.get("hint") or ""))
        except Exception as e:  # noqa: BLE001
            logger.error("落死信失败：%s", e)
        _set_current(project=project, episode=episode_no,
                     title=chapter.get("title") or f"第{episode_no}章",
                     step="blocked", message=_reason, percent=0,
                     started_at=_now(), retries=0, phase="blocked", steps_done=[])
        # 审计 P2-5（2026-09-29）：阻断信息已落死信（上方 mark_dead_letter，前端
        # 「异常」列表可见可处理）—— current 若不清，会永久停在 blocked 卡片，
        # 直到下一次生产覆盖（正常路径末尾有 _clear_current，这条分支此前漏了）。
        _clear_current()
        return

    _set_current(project=project, episode=episode_no,
                 title=chapter.get("title") or f"第{episode_no}章",
                 step="script", message=f"开始生产：{pick.get('reason')}",
                 percent=0, started_at=_now(), retries=0, phase="start",
                 steps_done=[])

    # ⭐ 2026-10-08（用户需求）：LLM 与 GPU 拆成两条线 —— 本集在烧 GPU（资产/分镜/视频）
    #    的时候，后台把**下一集剧本**（纯 LLM）先生成掉，把这段 LLM 时间从关键路径摘除。
    #    在开跑最初派发，覆盖本集全流程的 GPU 空档；任何失败只记日志、不影响本集。
    try:
        prewarm_next_script(project, plan, episode_no)
    except Exception as _pw_e:  # noqa: BLE001
        logger.debug("下一集剧本预热派发异常（忽略）：%s", _pw_e)

    # 步骤链用于前端「跑到哪一步」可视化：pipeline 每进入一个新步骤就回调一次，
    # 这里把"上一个步骤"记为已完成，从而得到实时进度链。
    _seen: list = []
    _retries = [0]

    def _cb(message, percent, phase=None):
        import pipeline as _pl
        # 契约：progress_cb 的 phase 约定 = <步骤名> 或 <步骤名>:<子阶段>。下面的 base
        # 归因只认 STEP_SEQUENCE 里的裸步骤名 —— 步骤内部子阶段若裸传与 STEP_SEQUENCE
        # 撞名的名字，会把未跑的步骤提前标成已完成（2026-10-06 实录：step_script 裸传
        # continuity 的内部阶段 "assets"＝加载设定库，剧本步骤 4% 时 script/tts_pre/assets
        # 三步即被标成已完成）。产出方约定见 pipeline.step_script 的 _cb（continuity /
        # novel_to_script 内部阶段一律加 "script:" 前缀）。
        base = str(phase or "").split(":")[0]
        if ":retry" in str(phase or ""):
            _retries[0] += 1
        if base in _pl.STEP_SEQUENCE and base not in _seen:
            idx = _pl.STEP_SEQUENCE.index(base)
            _seen.extend(_pl.STEP_SEQUENCE[:idx])   # 此前的步骤都已完成
            _seen.append(base)
        _set_current(step=base or "running", message=message, percent=int(percent or 0),
                     steps_done=list(dict.fromkeys(_seen)), retries=_retries[0])

    try:
        config = pipeline.normalize_config({**plan, "novel_id": meta.get("novel_id") or ""},
                                           default_project_key=project)
        # 文学剧本改写接入：文件存在即改写，无需人工审阅
        # ⚠️ 本模块不能引用 app（app 顶层 import 本模块，反向导入是循环；且本模块
        #    作用域里没有 app 这个名字，写了就是 NameError——2026-10-06 第1集整批
        #    秒败实录）。模块内日志一律走 logger。
        import novel_screenplay
        _md = novel_screenplay.load_screenplay(project, episode_no)
        if _md:
            logger.info("集 %s 使用文学剧本改写（文件存在）", episode_no)
            config.setdefault("screenplay_text", _md)
        result = pipeline.run_episode(
            config, project, episode_no, meta, chapter,
            progress_cb=_cb, should_stop=_halt_requested)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "status": "failed", "episode_no": episode_no,
                  "project": project, "error": f"{type(e).__name__}: {e}",
                  "elapsed_sec": round(time.time() - t0, 1), "steps": {}}
        logger.error("第%s集托管执行异常：%s\n%s", episode_no, e, traceback.format_exc())

    # ---- 结果登记 ----
    retries = sum(max(0, int((s or {}).get("attempts") or 1) - 1)
                  for s in (result.get("steps") or {}).values())
    with _LOCK:
        _STATE["totals"]["retries"] = int(_STATE["totals"].get("retries") or 0) + retries
    append_history(project, episode_no, result)

    produced_ok = bool(result.get("ok") and result.get("deliverable"))
    _note_run(project, episode_no, produced_ok)

    if produced_ok:
        try:
            pipeline.record_deliverable(project, episode_no, result["deliverable"], meta={
                "title": chapter.get("title") or "",
                "chapter_index": chapter.get("index"),
                "elapsed_sec": result.get("elapsed_sec"),
                "retries": retries,
                "steps": {k: (v or {}).get("status")
                          for k, v in (result.get("steps") or {}).items()},
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("交付物登记失败：%s", e)
        # 无人值守：产出即自动验收（待验收不阻塞生产，这里只是替人点掉那一下）
        if plan.get("auto_accept"):
            try:
                pipeline.set_deliverable_review(project, episode_no, "accepted",
                                                note="自动验收（托管 auto_accept）")
            except Exception as e:  # noqa: BLE001
                logger.warning("自动验收失败：%s", e)
        with _LOCK:
            _STATE["totals"]["episodes_done"] = int(_STATE["totals"].get("episodes_done") or 0) + 1
        _reset_attempt(project, episode_no)
        logger.info("第%s集生产完成：%s", episode_no, result.get("deliverable"))
    elif result.get("status") == "cancelled":
        logger.info("第%s集因托管暂停中止（已完成步骤已保留，可续跑）", episode_no)
    else:
        with _LOCK:
            _STATE["totals"]["episodes_failed"] = int(_STATE["totals"].get("episodes_failed") or 0) + 1
            _STATE["last_error"] = result.get("error") or "未知错误"
        if result.get("status") == "needs_human":
            # 环境性问题（模型未配置 / ffmpeg 缺失等）：直接挂起等人工，不浪费尝试次数
            _mark_dead(project, episode_no, result.get("error") or "需人工介入",
                       {"status": result.get("status")})
        else:
            _n = _bump_attempt(project, episode_no)
            logger.warning("第%s集生产失败（第 %d 次）：%s", episode_no,
                           _n, result.get("error"))
        # ⭐ 2026-10-08 修复「静默跳过」：stop_on_failure=True 时，单集失败不再
        # 悄悄跳下一集 —— 而是全局暂停 + 记 failure_pause，前端/总控醒目提醒
        # 「第N集失败已暂停，请处理」，用户处理后 resume 继续。False 保持旧行为。
        _stop_fail = bool(plan.get("stop_on_failure", True))
        if _stop_fail:
            _fail_reason = f"第{episode_no}集生产失败：{result.get('error') or '未知错误'}"
            _fail_err = str(result.get("error") or "")
            with _LOCK:
                _STATE["failure_pause"] = {
                    "project": project, "episode": episode_no,
                    "reason": _fail_reason, "error": _fail_err,
                    "at": _now(),
                }
                _STATE["paused"] = True
                _STATE["pause_reason"] = _fail_reason
            _mark_dead(project, episode_no, _fail_reason,
                       {"status": result.get("status") or "failed",
                        "stop_on_failure": True, "error": _fail_err})
            _persist_runtime()
            logger.warning("%s —— 已自动暂停等人工（stop_on_failure）", _fail_reason)


    # 2026-09-30：托管路径同样登记 last_run（成功/失败都记），与 run-once 口径一致。
    try:
        record_run_result(project, episode_no, produced_ok,
                          status=str(result.get("status") or ""),
                          error=str(result.get("error") or ""),
                          deliverable=str(result.get("deliverable") or ""),
                          elapsed_sec=result.get("elapsed_sec") or 0,
                          title=chapter.get("title") or "", source="autopilot")
    except Exception as e:  # noqa: BLE001
        logger.warning("托管 last_run 登记失败：%s", e)

    _clear_current()


# ===================== 对外状态 =====================


def purge_project(project_name: str) -> dict:
    """删除项目时由 project_store 调用：清掉该项目的托管运行态与死信。

    ⭐ 为什么必须做：删项目 → 重建**同名**项目时，若不清：
      ① current（正在生产的集/步骤/百分比）仍指向已删项目，同名重建后
         status(project) 按名匹配会把**旧生产进度**挂在新项目头上（用户实测
         「重新建项目上面进度条不对」的病根）；
      ② last_error / 死信 / 尝试计数残留，会被误读为新项目的状态；
      ③ 守护线程可能继续生产已删除的项目。
    计划文件（plan.json）与历史流水（history.jsonl）保留——重建后仍可查看，
    且计划默认 enabled=false，不会自动开闸生产。

    线程安全：先撤任务再清状态，避免「撤任务期间 current 又被写回」。
    """
    # 线程安全：先撤任务再清状态，避免「撤任务期间 current 又被写回」。
    # ⚠️ 2026-10-05 修复（用户报「删旧项目重建后为什么从第2集续跑」）：
    #    旧写法 `_A().schedule_cancel(project_name)` 是**写死的坏引用** —— app.py 里
    #    从未定义 `schedule_cancel`，此行必抛 AttributeError，导致**整个 purge_project
    #    第一行就中断**，后面清 current/attempts/last_run/死信 的逻辑**一条都没跑成**，
    #    于是重建同名项目时 autopilot 读旧台账（current 指向第2集、attempts 记第1集
    #    已完成）→ 自动从第2集续跑。改成安全调用：方法不存在则跳过（不影响后续清理）。
    try:
        _sched_cancel = getattr(_A(), "schedule_cancel", None)
        if callable(_sched_cancel):
            _sched_cancel(project_name)
    except Exception:  # noqa: BLE001
        # 撤任务失败不阻断台账清理（主流程下方继续清 current/attempts/last_run/死信）
        logger.warning("删除项目 %s 时撤销托管任务失败（忽略，继续清台账）", project_name)
    try:
        import pipeline
        n_dead = 0
        for d in (pipeline.list_dead_letters(project_name) or []):
            try:
                pipeline.resolve_dead_letter(project_name, int(d.get("episode_no") or 0),
                                             note="项目已删除（自动清理）")
                n_dead += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("删除项目 %s 时清理死信失败（不影响删除）：%s", project_name, e)
    with _LOCK:
        cur = dict(_STATE.get("current") or {})
        if str(cur.get("project") or "") == project_name:
            _clear_current()
        _STATE["last_error"] = ""
        (_STATE.get("last_runs") or {}).pop(project_name, None)
        _ATTEMPTS.pop(project_name, None)
        _LAST_RUN["key"] = None
        _LAST_RUN["repeat"] = 0
    # last_run 也要落盘清理：否则同名项目重建后会读到上一个项目的上一集结果
    try:
        _lp = _last_run_path(project_name)
        if os.path.isfile(_lp):
            os.remove(_lp)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 last_run 失败（%s）：%s", project_name, e)
    # ⭐ attempts 同样要落盘清理（2026-10-02 落盘后新增）：否则同名重建会带着
    # 上个项目的失败计数起步 → 一失败就立刻被「连续 N 次失败」挂起。
    try:
        _ap = _attempts_path(project_name)
        if os.path.isfile(_ap):
            os.remove(_ap)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理 attempts 失败（%s）：%s", project_name, e)
    wake()
    return {"project": project_name, "dead_letters_cleared": True}


def status(project: str = "", brief: bool = False) -> dict:
    """托管总览（前端主视图据此渲染）
    若传入 project，则只返回该项目状态；否则聚合所有项目。

    brief=True：只回「状态判断真正需要」的字段（AI 总控走这条）。
    原因：agent_core._trim 只把 JSON **前 1600 字符**喂给模型，且 Flask jsonify 会按
    字母序重排键 —— 键序不受我们控制，curve/totals 一旦变大就会把 last_run 挤出窗口。
    所以与其赌位置，不如直接给总控一份短的。前端不受影响（默认 brief=False）。
    """
    _restore_once()
    with _LOCK:
        st = json.loads(json.dumps(_STATE, ensure_ascii=False, default=str))
    plans = list_plans()
    enabled = [p for p in plans if p.get("enabled")]
    deliveries = []
    exceptions = []
    for p in plans:
        if project and p["project"] != project:
            continue
        try:
            deliveries.extend(pipeline_list_deliverables(p["project"]))
            exceptions.extend(pipeline_list_dead(p["project"]))
        except Exception as e:  # noqa: BLE001
            logger.warning("状态聚合失败（%s）：%s", p.get("project"), e)
    # 传入 project 时，开关/计划数也要收敛到该项目 ——
    # 否则前端按项目查询时，会看到别的项目开启托管而误判自己已开启。
    if project:
        mine = [p for p in plans if p.get("project") == project]
        enabled_count = sum(1 for p in mine if p.get("enabled"))
        plan_count = len(mine)
    else:
        enabled_count = len(enabled)
        plan_count = len(plans)
    # ⚠️ 2026-09-19 用户旅程实测教训：新建项目后与 AI 总控沟通风格，总控却按
    # **另一个项目的小说**给建议（拿《蛊真人》的方案回答《铜铃巷》的项目）。
    # 根因：status(project=X) 虽然把计数 / 交付物 / 曲线收敛到了 X，但 current 与
    # last_error 直接来自全局 _STATE，仍是「当时正在跑的那个项目」的内容，而这两段
    # 都落在 agent 工具结果的 1600 字符窗口内 —— 模型看到别的项目名，就当成本项目。
    # 这里：只有当正在生产的项目就是本项目时才回显 current，否则只给中性标志。
    if project:
        cur = st.get("current") or {}
        cur_proj = str(cur.get("project") or "")
        if cur_proj and cur_proj != project:
            st["current"] = None
            st["other_project_running"] = True
            # last_error 无项目归属，同样可能是别的项目的报错，一并藏起来
            st["last_error"] = ""
    # 上一次真正跑完的一集（成功/失败都记，带项目归属）：current 被清空之后，
    # 这是总控/前端唯一能知道「上一集到底成没成」的口径，别省。
    st.pop("last_runs", None)          # 只回单个项目的 last_run，避免 payload 膨胀
    st["last_run"] = read_last_run(project) if project else latest_last_run()
    # P1-5 补口：基于「最近进度推进时刻」算停滞时长，超阈值上浮 stall_warning。
    # LLM 重试/ComfyUI 排队等场景会长时间停在同一 step，用户此前只能看到「执行中」干等；
    # 现在前端/总控能据此提示「当前步骤已运行多久、是否疑似卡住」。
    _cur = st.get("current")
    if isinstance(_cur, dict) and _cur:
        try:
            _upd = float(_cur.get("step_updated_at") or 0)
        except (TypeError, ValueError):
            _upd = 0.0
        if _upd > 0:
            _stall = max(0, int(time.time() - _upd))
            _cur["step_stalled_sec"] = _stall
            _cur["step_elapsed_sec"] = _stall  # 兼容旧字段语义（当前步骤已运行时长）
            if _stall >= STEP_STALL_WARN_SEC:
                _cur["stall_warning"] = (
                    f"当前步骤（{_cur.get('step') or 'running'}）已运行 {_stall // 60} 分 "
                    f"{_stall % 60} 秒仍未推进，可能卡在重试/排队，建议关注。")
            else:
                _cur["stall_warning"] = ""
        # 人话描述在这里**重算一次**：_set_current 写入时算过一次，但那份是在进程内
        # 记账时刻算的；status() 可能在不同时刻被调用，且上面的 stall 分支刚改过字段。
        # 统一以「读取时刻」为准，保证前端/总控看到的是同一句话。
        try:
            _cur["describe"] = describe_current(_cur)
        except Exception as e:  # noqa: BLE001  展示降级不得影响状态查询接口
            logger.debug("生成人话进度描述失败（忽略）：%s", e)
        st["current"] = _cur
    st.update({
        # 回显作用域：调用方（含 AI 总控）必须能一眼看出这份数字属于哪个项目，
        # 否则模型会拿历史对话里的项目名去「对号入座」，把 A 的数据说成 B 的。
        "project": project,
        "scoped": bool(project),
        "enabled_count": enabled_count,
        "plan_count": plan_count,
        "pending_review": sum(1 for d in deliveries if d.get("review") == "pending"
                              and d.get("exists")),
        "delivered_total": len(deliveries),
        "exceptions": len([e for e in exceptions if not e.get("resolved")]),
        # 2026-10-08：单集失败「自动暂停」提醒（stop_on_failure）——
        # failure_pause 非空 = 因失败被自动暂停（前端醒目横幅 + 总控提醒用），
        # needs_user = failure_pause 或 未处理异常 > 0（供前端一键「查看/重跑」）。
        "curve": production_curve(24, project=project),
        # failure_pause：单集失败自动暂停的标记（含 project/episode/reason/error/at）
        "failure_pause": st.get("failure_pause") or {},
        # needs_user：需要人工介入（failure_pause 非空 或 未处理异常 > 0）
        "needs_user": bool(st.get("failure_pause")) or len([e for e in exceptions if not e.get("resolved")]) > 0,
    })
    # 2026-09-30：AI 总控读的是本接口 JSON 的**前 1600 字符**（agent_core._trim 是
    # 头部截断，不是省略中间）。所以「当前在跑什么 / 上一集成没成 / 有没有异常」
    # 必须排在最前面 —— 否则 last_run 落在 payload 尾部会被整段切掉，总控就又回到
    # 「只知道空闲、不知道上一集失败了」的老毛病。
    _front = ("running", "paused", "failure_pause", "needs_user", "current",
              "last_run", "last_error",
              "other_project_running", "exceptions", "pending_review",
              "project", "scoped", "enabled_count", "plan_count")
    _ordered = {k: st[k] for k in _front if k in st}
    _ordered.update({k: v for k, v in st.items() if k not in _front})
    if brief:
        _brief_keys = ("running", "paused", "current", "last_run", "last_error",
                       "other_project_running", "exceptions", "pending_review",
                       "enabled_count", "plan_count", "project", "scoped", "pause_reason",
                       "failure_pause", "needs_user")
        return {k: _ordered[k] for k in _brief_keys if k in _ordered}
    return _ordered


def pipeline_list_deliverables(project: str) -> list:
    import pipeline
    return pipeline.list_deliverables(project)


def pipeline_list_dead(project: str) -> list:
    import pipeline
    return pipeline.list_dead_letters(project)


def ready(plan: dict = None) -> dict:
    """托管可行性自检（告诉用户还差什么才能真正无人值守）"""
    A = _A()
    checks = []
    try:
        client = A._current_llm_client()
        ok = bool(client and getattr(client, "configured", False))
    except Exception:  # noqa: BLE001
        ok = False
    checks.append({"key": "text_model", "label": "文本模型（剧本生成）", "ok": ok,
                   "hint": "" if ok else "请到「AI 设置 → 文本分析」配置接口"})
    try:
        cfg = A._qc_load_cfg()
        qc_img = A.qc_client.image_qc_ready(cfg)
        qc_vid = A.qc_client.video_qc_ready(cfg)
    except Exception:  # noqa: BLE001
        qc_img = qc_vid = False
    # P1-1：接口「配置就绪」≠「能真正调用」。key 存在但鉴权 401 / 模型不可用时，
    # 上面仍会报全通、误导归因。这里在已配置时真实探测一次连通性（401/超时/网络
    # 都会失败），并把「401 / key 未配置」显式写进 hint，避免总控再猜错方向。
    qc_probe_hint = ""
    if qc_img or qc_vid:
        try:
            probe = A.qc_client.test_vision(A.qc_client.resolve_endpoint(cfg), timeout=20)
            if not probe.get("success"):
                err = str(probe.get("error") or "")
                if "401" in err or "无效" in err or "令牌" in err or "unauthorized" in err.lower():
                    qc_probe_hint = ("质检接口鉴权失败（401 / key 无效或未配置），"
                                     "产物将被 fail-open 放行但不会做 AI 判定。"
                                     "请到「AI 设置 → 质检」配置有效的 api_key。")
                else:
                    qc_probe_hint = ("质检接口连通性探测失败：%s。"
                                     "生成期间质检将 fail-open 放行资产（不做内容判定）。" % err[:120])
            elif probe.get("uncertain"):
                qc_probe_hint = ("质检接口可达但本次未确认视觉能力（正文为空，可能被思考吃掉额度），"
                                 "建议提高质检模块 max_tokens ≥1024 后重测。")
        except Exception as pe:  # noqa: BLE001
            qc_probe_hint = "质检接口探测异常：%s" % pe
    qc_ok = bool(qc_img and qc_vid) and not qc_probe_hint
    qc_hint = qc_probe_hint if qc_probe_hint else (
        "" if (qc_img and qc_vid) else "质检未开启时产物将不做 AI 判定，建议在「AI 设置 → 质检」开启")
    checks.append({"key": "qc", "label": "AI 质检（图片/视频）", "ok": qc_ok,
                   "hint": qc_hint, "qc_probe": "fail" if qc_probe_hint else "ok"})
    try:
        env = A.mix_ffmpeg_check()
        ff = bool(env.get("available"))
        ff_hint = "" if ff else "；".join(env.get("reasons") or [])
    except Exception as e:  # noqa: BLE001
        ff, ff_hint = False, str(e)
    checks.append({"key": "ffmpeg", "label": "FFmpeg（成片合成/探测）", "ok": ff, "hint": ff_hint})
    try:
        st = A.comfyui_client.get_status()
        cok = st.get("status") == "online"
    except Exception:  # noqa: BLE001
        cok = False
    checks.append({"key": "comfyui", "label": "ComfyUI 生成引擎", "ok": cok,
                   "hint": "" if cok else "ComfyUI 未启动或不可达；无人值守期间需保持运行"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
