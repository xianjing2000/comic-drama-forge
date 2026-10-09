# -*- coding: utf-8 -*-
"""任务租约 + 心跳 + 超时回收（借鉴 ai-manga-factory 的夜间自动化 lease/heartbeat）

被修掉的真问题
------------
原先的「集级互斥」只是一张**进程内**的 threading.Lock 字典（pipeline._EP_LOCKS）。
于是：

1. **跨进程无效** —— 托管守护线程与「手动 run-once」如果不在同一个 Flask 进程里
   （或多个实例并行），两边各自持有一份字典，互斥形同虚设，同一集会并发写同一批路径；
2. **崩溃后无痕迹** —— 进程没了，字典也没了，没有任何地方能回答
   「这一集是不是正被别人跑着？」；前端只能靠内存态，重启即失忆。

本模块用一个**文件租约**同时解决两点：

* 文件是跨进程的可见事实（O_EXCL 原子创建 → 同一时刻只有一个持有者）；
* 持有者定期刷新 heartbeat_at；**心跳停超过 TTL 即判定 stale，可被安全回收**，
  所以进程崩溃不会留下永久死锁 —— 回收动作会记 warning 留痕，不静默抢锁。

用法
----
    lk = task_lease.acquire("项目甲#3", owner="autopilot")
    if lk is None:
        ...  # 别处正在跑
    try:
        lk.start_heartbeat()      # 长任务必备：H3 整集要跑几十分钟
        ...
    finally:
        lk.release()              # 会顺带停掉心跳线程

也可用上下文管理器：`with task_lease.acquire(...) as lk:`（acquire 失败时
拿到的不是 None 而是带 ok=False 的租约，故上下文里要判 lk.ok）。

一切异常 **fail-open 偏向「不阻塞生产」**：租约目录不可写时 acquire 直接放行
（返回一个 ok=True 但不落盘的内存租约）并记 warning —— 宁可少一层并发保护，
也不能因为租约子系统故障让整条产线停摆。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_TTL_SEC", "acquire", "status", "reclaim_stale",
           "lease_dir", "list_leases", "is_stale", "purge_project"]

#: 默认租约存活时长（秒）。心跳停超过它就判 stale。
DEFAULT_TTL_SEC = 900
#: 心跳间隔默认取 TTL 的 1/3（三次机会，容忍两次抖动）。
def _default_heartbeat(ttl_sec: int) -> float:
    return max(5.0, float(ttl_sec) / 3.0)


def lease_dir() -> str:
    """租约目录：<PROJECT_OUTPUT_DIR>/.leases。惰性读 config 便于测试注入。"""
    from config import PROJECT_OUTPUT_DIR
    return os.path.join(PROJECT_OUTPUT_DIR, ".leases")


def _safe(scope: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(scope or ""))
    return (s[:60] or "scope")


def _lease_path(scope: str) -> str:
    digest = hashlib.sha1(str(scope or "").encode("utf-8", "ignore")).hexdigest()[:10]
    return os.path.join(lease_dir(), "%s.%s.json" % (_safe(scope), digest))


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _now() -> float:
    return time.time()


def _payload(scope: str, token: str, owner: str, ttl_sec: int) -> Dict[str, Any]:
    now = _now()
    return {"scope": str(scope), "token": token, "owner": str(owner or ""),
            "pid": os.getpid(), "host": _host(),
            "acquired_at": now, "heartbeat_at": now,
            "ttl_sec": int(ttl_sec), "acquired_at_str": time.strftime("%Y-%m-%d %H:%M:%S")}


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_atomic(path: str, data: dict) -> bool:
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning("租约写入失败：%s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _try_create(path: str, data: dict) -> bool:
    """O_EXCL 原子独占创建 —— 这是「同一时刻只有一个持有者」的根本保证。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError as e:
        logger.warning("租约创建失败（%s）：%s", path, e)
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as e:
        logger.warning("租约创建后写入失败：%s", e)
        try:
            os.remove(path)
        except OSError:
            pass
        return False


def _pid_alive(pid: int) -> bool:
    """判断**本机**某 pid 是否存活。

    ⚠️ Windows 上绝不能用 os.kill(pid, 0)：那在 Windows 是 TerminateProcess 的语义，
    会把目标进程真杀掉。必须走 OpenProcess 查询句柄。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True          # 权限不足等 → 当作活着（保守）
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    except Exception:                                    # noqa: BLE001
        return True              # 查不出来就当活着，绝不误抢


def is_stale(rec: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """判定租约是否可回收。

    两个判据（满足任一即可回收）：

    1. **同机 + 持有者进程已死** → 立刻可回收。这是 24/7 场景的关键：进程崩了之后
       不该把该集堵满 TTL（默认 15 分钟）才放行，否则「崩溃恢复」等于没有。
    2. 心跳停超过 TTL → 可回收（覆盖异机/进程还活着但卡死的情形）。

    字段缺失/损坏一律当 stale（可回收）—— 否则一个写坏的租约文件会永久锁死该集。
    """
    if not isinstance(rec, dict) or not rec:
        return True
    try:
        hb = float(rec.get("heartbeat_at") or 0)
        ttl = float(rec.get("ttl_sec") or DEFAULT_TTL_SEC)
    except (TypeError, ValueError):
        return True
    if not hb:
        return True
    try:
        if str(rec.get("host") or "") == _host():
            pid = int(rec.get("pid") or 0)
            if pid and pid != os.getpid() and not _pid_alive(pid):
                return True
    except (TypeError, ValueError):
        pass
    return ((now if now is not None else _now()) - hb) > max(1.0, ttl)


class Lease:
    """一次租约持有。心跳由守护线程自动刷新，release() 会停掉它。"""

    def __init__(self, scope: str, token: str, path: str, ttl_sec: int,
                 heartbeat_sec: float, *, ok: bool, degraded: bool = False,
                 owner: str = ""):
        self.scope, self.token, self.path = str(scope), token, path
        self.ttl_sec, self.heartbeat_sec = int(ttl_sec), float(heartbeat_sec)
        self.ok, self.degraded, self.owner = bool(ok), bool(degraded), str(owner or "")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lost = False

    # ---------------- 心跳 ----------------
    def heartbeat(self) -> bool:
        """刷新心跳。返回 False 表示租约已不在自己手里（被回收/被抢）。"""
        if self.degraded:
            return True
        cur = _read(self.path)
        if cur.get("token") != self.token:
            if not self.lost:
                self.lost = True
                logger.warning("租约已易主或被回收，停止心跳：%s（本 token %s…）",
                               self.scope, self.token[:6])
            return False
        cur["heartbeat_at"] = _now()
        return _write_atomic(self.path, cur)

    def start_heartbeat(self) -> "Lease":
        """启动守护线程定时刷新心跳（长任务必备）。重复调用是幂等的。"""
        if self.degraded or self._thread is not None:
            return self
        def _loop():
            while not self._stop.wait(self.heartbeat_sec):
                if not self.heartbeat():
                    return
        self._thread = threading.Thread(target=_loop, name="lease-hb-%s" % _safe(self.scope),
                                        daemon=True)
        self._thread.start()
        return self

    def stop_heartbeat(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    # ---------------- 释放 ----------------
    def release(self) -> bool:
        """释放租约（幂等）。**只有 token 还是自己的才删** —— 避免误删新持有者的租约。"""
        self.stop_heartbeat()
        if self.degraded:
            return True
        cur = _read(self.path)
        if cur.get("token") != self.token:
            return False
        try:
            os.remove(self.path)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            logger.warning("租约释放失败：%s", e)
            return False

    def info(self) -> Dict[str, Any]:
        return _read(self.path) or {"scope": self.scope, "degraded": self.degraded}

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(scope: str, *, owner: str = "", ttl_sec: int = DEFAULT_TTL_SEC,
            heartbeat_sec: Optional[float] = None) -> Optional[Lease]:
    """尝试获取租约；**拿不到返回 None**（不阻塞）。

    * 拿到 → 返回 Lease（心跳默认**不自动启动**，长任务请显式 start_heartbeat()）
    * 已被别人持有且心跳新鲜 → None
    * 已有但心跳过期 → 回收（改名归档 + warning 留痕）后重新抢占
    * 租约目录不可写 → 降级为「内存租约」放行并 warning（不因租约故障停摆产线）
    """
    scope = str(scope)
    ttl = max(1, int(ttl_sec))
    hb = _default_heartbeat(ttl) if heartbeat_sec is None else max(1.0, float(heartbeat_sec))
    try:
        path = _lease_path(scope)
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        logger.warning("租约目录不可用，本次放行（降级为无租约）：%s", e)
        return Lease(scope, "degraded", "", ttl, hb, ok=True, degraded=True, owner=owner)

    token = uuid.uuid4().hex
    me = _payload(scope, token, owner, ttl)

    try:
        if _try_create(path, me):
            return Lease(scope, token, path, ttl, hb, ok=True, owner=owner)
    except OSError:
        logger.warning("租约创建异常，本次放行（降级为无租约）：%s", scope)
        return Lease(scope, "degraded", "", ttl, hb, ok=True, degraded=True, owner=owner)

    cur = _read(path)
    if not is_stale(cur):
        logger.info("租约被占用（%s）：owner=%s pid=%s host=%s 心跳 %s",
                    scope, cur.get("owner"), cur.get("pid"), cur.get("host"),
                    time.strftime("%H:%M:%S", time.localtime(float(cur.get("heartbeat_at") or 0))))
        return None

    # 过期回收：**改名归档**是原子的 CAS —— 并发抢占时只有一个进程能把旧文件搬走
    stamp = "%s.stale.%d.%s" % (path, int(_now()), token[:6])
    try:
        os.replace(path, stamp)
    except OSError:
        return None          # 别人先抢走了
    logger.warning("回收过期租约：%s（原 owner=%s pid=%s host=%s，心跳停在 %s）",
                   scope, cur.get("owner"), cur.get("pid"), cur.get("host"),
                   time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(float(cur.get("heartbeat_at") or 0))))
    try:
        if _try_create(path, me):
            lk = Lease(scope, token, path, ttl, hb, ok=True, owner=owner)
            lk.reclaimed_from = cur       # 供调用方/诊断追溯
            return lk
    except OSError:
        pass
    return None


def status(scope: str) -> Dict[str, Any]:
    """查某租约当前状态（诊断/前端用）：{held, stale, info}。"""
    try:
        path = _lease_path(scope)
    except OSError:
        return {"held": False, "stale": True, "info": {}}
    rec = _read(path)
    if not rec:
        return {"held": False, "stale": True, "info": {}}
    stale = is_stale(rec)
    return {"held": not stale, "stale": stale, "info": rec,
            "age_sec": round(_now() - float(rec.get("heartbeat_at") or 0), 1)}


def reclaim_stale(scope: str, *, reason: str = "人工回收") -> Dict[str, Any]:
    """显式回收一个过期租约（管理员入口）。心跳新鲜时**拒绝**回收。"""
    path = _lease_path(scope)
    rec = _read(path)
    if not rec:
        return {"reclaimed": False, "reason": "无租约"}
    if not is_stale(rec):
        return {"reclaimed": False, "reason": "心跳新鲜，拒绝回收",
                "info": rec, "age_sec": round(_now() - float(rec.get("heartbeat_at") or 0), 1)}
    stamp = "%s.reclaimed.%d" % (path, int(_now()))
    try:
        os.replace(path, stamp)
    except OSError as e:
        return {"reclaimed": False, "reason": "抢占失败：%s" % e}
    logger.warning("人工回收租约：%s（%s），原 owner=%s pid=%s",
                   scope, reason, rec.get("owner"), rec.get("pid"))
    return {"reclaimed": True, "reason": reason, "info": rec, "archive": stamp}


def list_leases() -> list:
    """列出当前所有租约（诊断用，含是否过期）。"""
    out = []
    d = lease_dir()
    try:
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        rec = _read(os.path.join(d, n))
        if rec:
            out.append({**rec, "stale": is_stale(rec)})
    return out


def purge_project(projects: list) -> int:
    """项目被删除时，摘除该项目名下的全部租约文件（关联清理，由 project_store 调用）。

    为什么必须做：集级租约 scope 形如 ``episode:<项目>#<集号>``（见
    pipeline._episode_scope），**项目名是 scope（也是租约文件名）的成分之一**。
    删项目后若不清：同名重建的头一个 TTL 窗口（默认 900 秒）内，旧租约文件还在，
    ``pipeline.is_episode_running`` 会把该集误判成「正在运行」而拒绝开跑。

    判据：读租约内容里的 ``scope``，按前缀 ``episode:<别名>#`` **精确匹配**
    （别名并集 = dir_key + 显示名，覆盖「列里存键或存名」两种落法）；
    不按文件名猜（``_safe`` 会把特殊字符归一成 ``_``，反向推导不可靠）。
    归档态的 ``*.json.stale.*`` / ``*.json.reclaimed.*`` 一并扫描（它们已无互斥作用，
    纯属残留）。删除失败只 warning —— 租约本就 fail-open，绝不让清理阻断删除主流程。
    返回删除的文件数。
    """
    vals = [str(p).strip() for p in (projects or []) if p and str(p).strip()]
    if not vals:
        return 0
    prefixes = tuple("episode:%s#" % v for v in vals)
    d = lease_dir()
    try:
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
    except OSError:
        return 0
    removed = 0
    for n in names:
        # 活跃租约 <scope>_<hash>.json + 归档态 <scope>_<hash>.json.stale.<ts> / .reclaimed.<ts>
        if not (n.endswith(".json") or ".json." in n):
            continue
        path = os.path.join(d, n)
        rec = _read(path)
        scope = str((rec or {}).get("scope") or "")
        if not scope.startswith(prefixes):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            logger.warning("清理项目租约失败（%s）：%s", path, e)
    if removed:
        logger.info("已随项目删除摘除 %d 个租约文件（scope 前缀匹配）：%s", removed, vals)
    return removed
