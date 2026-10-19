# -*- coding: utf-8 -*-
"""版本化 SQLite 迁移机制（T23 §2.2，2026-10-16）。

为什么需要
----------
此前本仓库**没有**版本化迁移：只有 ``CREATE TABLE IF NOT EXISTS``（对**已存在**的表
完全无效）+ 一次性手写重建（``schema._migrate_units_fk``）。``schema.SCHEMA_VERSION``
只写不读 —— 任何「加列 / 改列」都不会在旧库上生效。T23 实测：开发根与生产根的同名库
schema 已分叉（生产 v2 有 FK / 复合索引，开发根没有），却是同一份代码在两边跑。

本模块补上真正的迁移：以 SQLite 内置 ``PRAGMA user_version`` 作版本号，迁移按
``(版本号, 说明, 函数)`` 登记，``migrate()`` 顺序、幂等、**逐条事务**地应用到最新，
改库前**先备份**。

设计约束
--------
  · **幂等**：反复调用安全（已应用版本不重跑，靠 user_version 判定）；
  · **逐条事务**：每条迁移一个事务（显式 BEGIN/COMMIT，autocommit 模式），失败即
    回滚该条并中止，之前成功的保留；
  · **先备份**：任何写入前复制 ``<db>.bak_migrate_v<from>_<ts>``；
  · **只增不删**：迁移函数只做前向变更，不动用户数据；
  · **叶子模块**：不 import 任何业务模块（可被最底层安全依赖）。

用法::
    from migrations import migrate
    migrate(config.TASKS_DB_PATH)          # 启动时调用即可（幂等、无迁移时零开销）
    python -m migrations <db_path>          # CLI：跑一次、把结果打成 JSON
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Callable, List, Tuple

#: (目标版本号, 说明, 迁移函数)。fn(conn) 在**一个事务**内执行。
#: ⚠️ 版本号严格递增、唯一；**已发布的条目不可改函数体**，只能在其后**追加**新条目。
#: 当前 schema 已由既有 ``CREATE TABLE IF NOT EXISTS`` 铺底（无版本号）→ 基线视为 0；
#: 未来任何「加列 / 改列 / 加索引」都按 (1, 2, 3, …) 追加到这里。
MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Connection], None]]] = []


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    # 手动控制事务（autocommit 模式）—— 否则 Python sqlite3 只对 DML 隐式开事务，
    # DDL（ALTER TABLE）不在事务内、失败无法回滚。迁移必须能整体回滚，故自己管。
    conn.isolation_level = None
    return conn


def current_version(db_path: str) -> int:
    """读 ``PRAGMA user_version``；库不存在 / 打不开 → 0。"""
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def pending(db_path: str) -> list:
    """未应用的迁移 [(版本号, 说明)]。"""
    cur = current_version(db_path)
    return [[int(v), note] for v, note, _fn in MIGRATIONS if int(v) > cur]


def backup_db(db_path: str, tag: str = "manual") -> str:
    """把库文件复制成 ``<db>.bak_<tag>_<ts>``；返回备份路径（失败 / 空库返回 ""）。"""
    try:
        if db_path and os.path.exists(db_path) and os.path.getsize(db_path) > 0:
            bak = "%s.bak_%s_%s" % (db_path, tag, time.strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(db_path, bak)
            return bak
    except OSError:
        pass
    return ""


def migrate(db_path: str, *, backup: bool = True) -> dict:
    """把库迁移到最新版本。返回 ``{ok, from, to, applied, failed, backup}``，**绝不抛**。"""
    out = {"ok": True, "from": 0, "to": 0, "applied": [], "failed": [], "backup": ""}
    if not db_path:
        out["ok"] = False
        out["failed"] = [[0, "db_path 为空"]]
        return out
    cur = current_version(db_path)
    out["from"] = cur
    todo = sorted((int(v), note, fn) for v, note, fn in MIGRATIONS if int(v) > cur)
    if not todo:
        out["to"] = cur
        return out
    if backup:
        bak = backup_db(db_path, tag="migrate_v%d" % cur)
        out["backup"] = bak
        # 有数据但没备成 → 安全优先，拒绝迁移
        if not bak and os.path.exists(db_path) and os.path.getsize(db_path) > 0:
            out["ok"] = False
            out["failed"] = [[cur, "备份失败，已中止迁移（安全优先）"]]
            return out
    try:
        conn = _connect(db_path)
    except sqlite3.Error as e:
        out["ok"] = False
        out["failed"] = [[cur, "打不开库：%s" % e]]
        return out
    try:
        for v, note, fn in todo:
            try:
                conn.execute("BEGIN")
                fn(conn)
                conn.execute("PRAGMA user_version = %d" % v)
                conn.execute("COMMIT")
                out["applied"].append([v, note])
            except Exception as e:  # noqa: BLE001 单条失败即中止（保留已成功的）
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                out["failed"].append([v, "%s: %s" % (type(e).__name__, e)])
                out["ok"] = False
                break
        out["to"] = current_version(db_path)
    finally:
        conn.close()
    return out


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("用法: python -m migrations <db_path>\n")
        return 2
    r = migrate(argv[0])
    sys.stdout.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
