# -*- coding: utf-8 -*-
"""持久化任务队列 + 断点续跑（P0-4）

解决的问题
----------
改造前：任务状态（generation_state / upscale_tasks / dub_tasks ...）全部存在内存 dict 里，
进程重启即丢，H3 单集生成动辄数小时，中途崩一次就得从零重来。

本模块提供
----------
1. **任务落盘**：SQLite（默认 output/tasks.db）记录任务全生命周期，
   重启后仍可查询「哪些任务跑到哪了」；
2. **断点续跑**：以「最小可复用单元产物是否已落盘」为完成判据，
   重跑时自动跳过已完成单元（见 `is_unit_done` / `filter_pending_units`）；
3. **串行消费**：单 GPU 场景下任务必须排队（避免与 ComfyUI 抢显存），
   提供 `TaskQueue` 做串行调度与失败记录；
4. **崩溃恢复**：启动时把残留的 running 任务标记为 interrupted，
   便于前端提示「可继续」。

设计约束
--------
- **不侵入现有流程**：既有的内存 task_id 机制保持不动，本模块作为「旁路记录 + 续跑判据」接入；
- **单文件存储**：SQLite 免运维，不引入 Redis 等外部依赖；
- 所有异常都不得打断主流程（记录到 error 字段即可）。

任务状态机
----------
pending → running → (done | failed | interrupted | cancelled)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# 任务状态
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_INTERRUPTED = "interrupted"   # 进程重启导致的「跑到一半丢失」
ST_CANCELLED = "cancelled"

TERMINAL_STATES = (ST_DONE, ST_FAILED, ST_CANCELLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    progress    REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0,
    result_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind);

CREATE TABLE IF NOT EXISTS units (
    task_id     TEXT NOT NULL,
    unit_key    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    result_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, unit_key)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskStore:
    """SQLite 任务存储（线程安全；各方法独立开连接，避免跨线程复用）

    on_change: 可选回调 fn(task_dict, event)，在任务状态变化（finish/fail）后调用。
               用于把耗时统计等旁路逻辑解耦出去（P2-3 成本看板即由此接入），
               回调内抛异常不影响任务本身。
    """

    def __init__(self, db_path: str, on_change: Callable = None):
        self.db_path = db_path
        self.on_change = on_change
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _notify(self, task_id: str, event: str) -> None:
        """触发变更回调（绝不抛异常）"""
        if not self.on_change:
            return
        try:
            t = self.get(task_id)
            if t:
                self.on_change(t, event)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"任务变更回调失败（忽略）：{e}")

    # ---------- 底层 ----------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception as e:  # noqa: BLE001
            logger.debug("开启 SQLite WAL 失败（忽略，回落默认 journal）：%s", e)
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # ---------- 任务 CRUD ----------

    def create(self, kind: str, project: str = "", label: str = "",
               payload: dict = None, total: float = 0,
               task_id: str = None) -> str:
        tid = task_id or uuid.uuid4().hex[:16]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                # ⚠️ 绝不能用 INSERT OR REPLACE（2026-10-12 数据层审计 T23 实测的数据丢失 bug）：
                #    SQLite 的 OR REPLACE 语义是「先 DELETE 再 INSERT」，而生产库的 units 表
                #    带 FK `ON DELETE CASCADE`（schema.py:62-69 已生效）→ 同一 task_id 二次 create
                #    会**静默清空该任务的全部 units**（= 断点续跑进度），实测 units 1 → 0。
                #    触发点真实存在：routes/keyframes.py:260 用
                #    `task_id=keyframe_{project}_{秒级时间戳}` —— 同一秒内重建即命中。
                #    改为 upsert（ON CONFLICT DO UPDATE）：
                #    · id 冲突时**保留原 created_at**，只更新内容与 updated_at；
                #    · 不触发 DELETE，因此不会级联删掉 units。
                conn.execute(
                    "INSERT INTO tasks (id, project, kind, label, payload, status, "
                    "progress, total, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "project=excluded.project, kind=excluded.kind, label=excluded.label, "
                    "payload=excluded.payload, status=excluded.status, progress=excluded.progress, "
                    "total=excluded.total, updated_at=excluded.updated_at",
                    (tid, project, kind, label,
                     json.dumps(payload or {}, ensure_ascii=False),
                     ST_PENDING, 0, total, now, now))
                conn.commit()
            finally:
                conn.close()
        return tid

    def start(self, task_id: str) -> None:
        self._update(task_id, status=ST_RUNNING, started_at=_now())

    def set_progress(self, task_id: str, progress: float = None, total: float = None) -> None:
        fields = {}
        if progress is not None:
            fields["progress"] = float(progress)
        if total is not None:
            fields["total"] = float(total)
        if fields:
            self._update(task_id, **fields)

    def finish(self, task_id: str, result_path: str = "") -> None:
        # progress 置 100：否则前端会显示「已完成但 0%」，与状态矛盾
        self._update(task_id, status=ST_DONE, progress=100.0,
                     result_path=result_path, finished_at=_now(), error="")
        self._notify(task_id, ST_DONE)

    def fail(self, task_id: str, error: str) -> None:
        self._update(task_id, status=ST_FAILED, error=str(error)[:2000], finished_at=_now())
        self._notify(task_id, ST_FAILED)

    def cancel(self, task_id: str) -> None:
        self._update(task_id, status=ST_CANCELLED, finished_at=_now())

    def _update(self, task_id: str, status: str = None, progress: float = None,
                result_path: str = None, error: str = None,
                started_at: str = None, finished_at: str = None,
                total: float = None) -> None:
        sets, vals = [], []
        mapping = [("status", status), ("progress", progress), ("result_path", result_path),
                   ("error", error), ("started_at", started_at),
                   ("finished_at", finished_at), ("total", total)]
        for col, val in mapping:
            if val is not None and not (col == "progress" and isinstance(val, float) and val != val):
                sets.append(f"{col}=?")
                vals.append(val)
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(task_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
                conn.commit()
            finally:
                conn.close()

    def get(self, task_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def list(self, project: str = None, status: str = None,
             kind: str = None, limit: int = 100) -> list:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args = []
        if project:
            sql += " AND project=?"
            args.append(project)
        if status:
            sql += " AND status=?"
            args.append(status)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        conn = self._conn()
        try:
            rows = conn.execute(sql, args).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except Exception:  # noqa: BLE001
            d["payload"] = {}
        return d

    # ---------- 断点续跑：单元级 ----------

    def mark_unit(self, task_id: str, unit_key: str, status: str,
                  result_path: str = "", error: str = "") -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO units (task_id, unit_key, status, result_path, error, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (task_id, unit_key, status, result_path, error[:1000], _now()))
                conn.commit()
            finally:
                conn.close()

    def list_units(self, task_id: str) -> list:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM units WHERE task_id=?", (task_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def done_units(self, task_id: str) -> set:
        """已完成的单元键集合（用于跳过重算）"""
        return {u["unit_key"] for u in self.list_units(task_id)
                if u.get("status") == ST_DONE}

    def recycle_interrupted(self) -> int:
        """启动时调用：把残留的 running 任务标记为 interrupted，返回处理条数"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET status=?, error=?, finished_at=?, updated_at=? "
                    "WHERE status=?",
                    (ST_INTERRUPTED, "进程重启导致任务中断，可从已完成单元继续",
                     _now(), _now(), ST_RUNNING))
                conn.commit()
                n = cur.rowcount or 0
            finally:
                conn.close()
        if n:
            logger.warning(f"检测到 {n} 个中断任务（进程重启），已标记为 interrupted，可续跑")
        return n

    def purge_project(self, projects: list) -> dict:
        """项目被删除时，级联摘除该项目在 tasks / units 两表里的关联记录。

        tasks.db 是**全局单库**（所有项目共用，不能整库删），只能按 project
        别名并集摘除：``projects`` 传该项目的全部别名（dir_key + 显示名），
        用 ``IN (...)`` 覆盖「列里可能存键、也可能存名」两种落法。
        先按 project 找出关联 task_id、级联删 units（units 只有 task_id 一个关联键），
        再删 tasks 本体。幂等：没有匹配时两表 0 删、不动任何其它项目。
        返回摘除统计 {tasks, units}。
        """
        vals = [str(p).strip() for p in (projects or []) if p and str(p).strip()]
        if not vals:
            return {"tasks": 0, "units": 0}
        marks = ",".join("?" for _ in vals)
        with self._lock:
            conn = self._conn()
            try:
                ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM tasks WHERE project IN ({marks})", vals)]
                units_n = 0
                if ids:
                    umarks = ",".join("?" for _ in ids)
                    units_n = conn.execute(
                        f"DELETE FROM units WHERE task_id IN ({umarks})",
                        ids).rowcount or 0
                tasks_n = conn.execute(
                    f"DELETE FROM tasks WHERE project IN ({marks})",
                    vals).rowcount or 0
                conn.commit()
            finally:
                conn.close()
        if tasks_n or units_n:
            logger.info("任务库已随项目删除摘除 %d 个任务 / %d 个单元：%s",
                        tasks_n, units_n, vals)
        return {"tasks": tasks_n, "units": units_n}


# ===================== 断点续跑判据 =====================

def is_unit_done(result_path: str, min_bytes: int = 1) -> bool:
    """单元完成判据：产物文件存在且非空。

    这是断点续跑的核心约定——**产物落盘即视为该单元已完成**，
    因此重跑时无需依赖任务状态表，磁盘本身就是最可靠的状态源。
    """
    if not result_path:
        return False
    try:
        return os.path.isfile(result_path) and os.path.getsize(result_path) >= min_bytes
    except OSError:
        return False


def filter_pending_units(units: Iterable[dict], key: str = "unit_key",
                         path: str = "result_path") -> list:
    """从单元列表中筛出「尚未完成」的单元（产物不存在或为空）

    units 形如 [{"unit_key": "shot_01", "result_path": "D:/.../shot_01.mp4"}, ...]
    """
    pending = []
    for u in units or []:
        if not isinstance(u, dict):
            continue
        p = u.get(path) or ""
        # 有产物路径且产物已存在 → 已完成，跳过
        if p and is_unit_done(p):
            continue
        pending.append(u)
    return pending


# ===================== 串行任务队列 =====================

class TaskQueue:
    """串行任务队列（单 GPU 场景：同一时刻只跑一个重任务，避免抢显存）

    用法：
        q = TaskQueue(store)
        ok = q.submit(task_id, fn=lambda: do_work(), on_error=lambda e: ...)
        q.start()          # 启动后台消费线程（幂等）
        q.join(timeout=...)  # 等待队列排空（可选）

    D-07（P2）：① 加**背压** ``max_queue``（默认 64），队列满时拒绝提交而非无限堆积；
    ② 加 ``task_id`` **去重**（``_pending_ids``）—— 同一 task_id 在队列中/执行中时
    重复提交被忽略并告警，避免 ``concurrency > 1`` 时同一任务被并发执行两次
    （两遍 GPU 生成、同一输出目录互写）。

    N2（2026-09-22 复验）**接线状态声明**：本项目中 ``submit`` / ``start`` / ``join``
    **刻意未接线到生产链路** —— 单 GPU 并发由 ``gpu_task_gate`` 的进程级
    ``Semaphore(TASK_QUEUE_CONCURRENCY)`` 承担（见 ``gpu_task_gate.py`` 模块头
    「不做什么」第 2 条：「不真正接线 submit —— 改用 Semaphore 方案」）。
    本类保留是为 ``.status()`` 可观测性（``/api/status`` 的 ``task_queue`` 字段）
    与嵌入使用 / 测试。因此 D-07 的背压与去重是该模块**自身契约**的加固，
    **不构成「已在生产生效」的宣称**；调用方勿据此认为生产路径已有并发闸门。
    """

    def __init__(self, store: TaskStore, concurrency: int = 1,
                 max_queue: int = 64):
        self.store = store
        self.concurrency = max(1, int(concurrency))
        # 背压上限：<=0 表示不限制（保留旧行为，但显式传 0 才生效）
        try:
            self._max_queue = int(max_queue)
        except (TypeError, ValueError):
            self._max_queue = 64
        self._q: list = []
        # 队列中 + 执行中的 task_id 集合（去重判据；worker 的 finally 里 discard）
        self._pending_ids: set = set()
        self._cv = threading.Condition()
        self._threads: list = []
        self._running = False
        self._current: Optional[str] = None
        self._current_started: float = 0.0

    # ---------- 提交 ----------

    def submit(self, task_id: str, fn: Callable[[], object],
               on_error: Callable[[Exception], None] = None) -> bool:
        """提交任务；返回是否被接受（False = 重复提交 或 队列已满）。

        D-07：不发散 —— 两种拒绝都留 warning/error 日志，便于排障。
        """
        with self._cv:
            if task_id in self._pending_ids:
                logger.warning("任务已在队列/执行中，忽略重复提交：%s", task_id)
                return False
            if self._max_queue > 0 and len(self._q) >= self._max_queue:
                logger.error("任务队列已满（上限 %d），拒绝提交：%s",
                             self._max_queue, task_id)
                return False
            self._pending_ids.add(task_id)
            self._q.append((task_id, fn, on_error))
            self._cv.notify()
            return True

    # ---------- 生命周期 ----------

    def start(self, thread_name: str = "mjscxt-taskq") -> None:
        if self._running:
            return
        self._running = True
        for i in range(self.concurrency):
            t = threading.Thread(target=self._worker, name=f"{thread_name}-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info(f"任务队列已启动（并发 {self.concurrency}）")

    def stop(self) -> None:
        with self._cv:
            self._running = False
            self._cv.notify_all()

    def join(self, timeout: float = None) -> bool:
        """等待队列排空 + 当前任务结束；返回是否已排空"""
        deadline = time.time() + timeout if timeout else None
        while True:
            with self._cv:
                empty = not self._q and self._current is None
            if empty:
                return True
            if deadline and time.time() > deadline:
                return False
            time.sleep(0.5)

    def status(self) -> dict:
        with self._cv:
            return {
                "running": self._running,
                "concurrency": self.concurrency,
                "queued": len(self._q),
                # D-07：暴露背压上限与去重占位数，修复「队列字段失真」的可观测性
                "max_queue": self._max_queue,
                "pending": len(self._pending_ids),
                "current": self._current,
                "current_elapsed_sec": round(time.time() - self._current_started, 1)
                if self._current else 0,
            }

    # ---------- 消费 ----------

    def _worker(self) -> None:
        while True:
            with self._cv:
                while self._running and not self._q:
                    self._cv.wait(timeout=1.0)
                if not self._running and not self._q:
                    return
                if not self._q:
                    continue
                task_id, fn, on_error = self._q.pop(0)
                self._current = task_id
                self._current_started = time.time()

            try:
                self.store.start(task_id)
                result = fn()
                path = result if isinstance(result, str) else ""
                self.store.finish(task_id, result_path=path)
                logger.info(f"任务完成：{task_id}")
            except BaseException as e:  # noqa: BLE001  含环境守卫注入的 SystemExit（继承 BaseException）
                # 审计 P2-7（2026-09-29）：只捕 Exception 会让消费线程被 BaseException
                # 静默杀死 —— 队列停摆且 status() 仍显示 running。这里记录失败态后
                # 继续消费循环（守护线程里上抛无人接，只会让线程死亡、队列停摆）。
                logger.exception(f"任务异常终止：{task_id} - {e}")
                try:
                    self.store.fail(task_id, f"{type(e).__name__}: {e}")
                except Exception as e:  # noqa: BLE001
                    logger.debug("任务失败态落库失败（主异常已记录）：%s", e)
                if on_error:
                    try:
                        on_error(e)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_error 回调自身抛错")
            finally:
                with self._cv:
                    self._current = None
                    # D-07：任务已结束（成功/失败/取消）→ 释放去重占位
                    self._pending_ids.discard(task_id)
                    self._cv.notify_all()


# ===================== 全局单例 =====================

_STORE: Optional[TaskStore] = None
_QUEUE: Optional[TaskQueue] = None
# 用 RLock（可重入）：get_queue 内部会调 get_store，两者共用同一把锁，
# 普通 Lock 会导致自锁死（已踩过坑，勿改回 Lock）。
_LOCK = threading.RLock()


def get_store(db_path: str, on_change: Callable = None) -> TaskStore:
    global _STORE
    with _LOCK:
        if _STORE is None or os.path.abspath(_STORE.db_path) != os.path.abspath(db_path):
            _STORE = TaskStore(db_path, on_change=on_change)
        elif on_change and _STORE.on_change is None:
            _STORE.on_change = on_change
        return _STORE


def get_queue(db_path: str) -> TaskQueue:
    """取全局任务队列（注意：不能在 _LOCK 内调用 get_store，否则与 _LOCK 互锁）"""
    global _QUEUE
    with _LOCK:
        if _QUEUE is not None:
            return _QUEUE
    # 先在锁外取 store（get_store 自身需要 _LOCK），再回锁内写入 _QUEUE
    store = get_store(db_path)
    with _LOCK:
        if _QUEUE is None:
            _QUEUE = TaskQueue(store)
        return _QUEUE
