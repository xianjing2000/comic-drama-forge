"""数据库 schema 的单一事实源（2026-10-10 设计审查修复）。

## 为什么需要
排查发现：tasks.db 的 4 张表由 **3 个模块各自建表**（5 处 CREATE TABLE）——
  ai_credentials_db.py:23,68 ｜ task_store.py:55,75 ｜ config_center.py:105
没有 schema 版本号、没有迁移记录、索引也不全（tasks 的 project+status 组合查询与
ORDER BY created_at 都只能扫或走临时 B 树）。改表结构只能手写 ALTER，无法回滚。

本模块提供**幂等**的 ensure_schema()：只补建缺失的表/索引并记录版本，
不动任何既有数据，也不取代各模块自己的建表（避免一次性大改带来的风险）。

## 表状态说明（排查结论）
  tasks           —— 结构完整但**当前 0 行**：持久化任务队列（P0-4）建好了但从未接入，
                     task_store 只在 project_store 里被调用做清理。属**预留功能**，
                     保留结构、标注状态，待真正接入或显式废弃。
  units           —— 同上（tasks 的从表，0 行）。
  ai_credentials  —— 使用中（3 行）。
  app_settings    —— 使用中（配置中心，10 行）。

## 纪律
只依赖标准库；不 import 任何业务模块（避免制造新的循环依赖）。
所有操作 fail-open：建表/建索引失败只告警，绝不让服务起不来。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: 当前 schema 版本。任何结构变更都要 +1 并在 MIGRATIONS 里追加说明。
SCHEMA_VERSION = 1

#: 建表语句（IF NOT EXISTS，幂等）。与各模块自身建表保持一致，此处作为权威登记。
TABLES: Dict[str, str] = {
    'schema_version': (
        'CREATE TABLE IF NOT EXISTS schema_version ('
        ' version INTEGER PRIMARY KEY,'
        ' applied_at TEXT,'
        ' note TEXT)'),
    'tasks': (
        'CREATE TABLE IF NOT EXISTS tasks ('
        ' id TEXT PRIMARY KEY, project TEXT, kind TEXT, label TEXT, payload TEXT,'
        ' status TEXT, progress INTEGER, total INTEGER, result_path TEXT, error TEXT,'
        ' created_at TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT)'),
    'units': (
        'CREATE TABLE IF NOT EXISTS units ('
        ' task_id TEXT, unit_key TEXT, status TEXT, result_path TEXT, error TEXT,'
        ' updated_at TEXT, PRIMARY KEY (task_id, unit_key))'),
    'ai_credentials': (
        'CREATE TABLE IF NOT EXISTS ai_credentials ('
        ' module TEXT PRIMARY KEY, base_url TEXT, model TEXT, api_key_cipher TEXT,'
        ' reasoning_effort TEXT, source TEXT, updated_at TEXT)'),
    'app_settings': (
        'CREATE TABLE IF NOT EXISTS app_settings ('
        ' key TEXT PRIMARY KEY, value TEXT, value_type TEXT, updated_at TEXT, note TEXT)'),
}

#: 索引（IF NOT EXISTS，幂等）。
#  2026-10-10 新增两个：实测 EXPLAIN 显示
#    · WHERE project=? AND status=? 只用到单列索引；
#    · ORDER BY created_at DESC 依赖 TEMP B-TREE（无索引）。
#  当前 tasks 为 0 行故无性能影响，但结构上应当补齐。
INDEXES: List[str] = [
    'CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project)',
    'CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)',
    'CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind)',
    'CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status)',
    'CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)',
    'CREATE INDEX IF NOT EXISTS idx_units_task ON units(task_id)',
]

#: 预留/未接入的表的说明（供 /api/config/doctor 与运维参考）
TABLE_NOTES: Dict[str, str] = {
    'tasks': '预留：持久化任务队列（P0-4）结构已建但从未接入写入，当前 0 行',
    'units': '预留：tasks 的从表，随 tasks 一起未接入，当前 0 行',
}


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    """读已应用的 schema 版本（无表/无行返回 0）。"""
    try:
        row = conn.execute('SELECT MAX(version) FROM schema_version').fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def ensure_schema(db_path: str) -> Dict[str, Any]:
    """幂等建表/建索引并记录版本。返回 {ok, version, created_tables, created_indexes, error}。

    绝不抛异常：库不可用/权限问题都只记日志并返回 ok=False。
    """
    out: Dict[str, Any] = {'ok': False, 'version': 0, 'created_tables': [],
                           'created_indexes': [], 'error': ''}
    if not db_path:
        out['error'] = '未提供数据库路径'
        return out
    try:
        d = os.path.dirname(os.path.abspath(db_path))
        if d:
            os.makedirs(d, exist_ok=True)
        conn = _connect(db_path)
        try:
            before = set(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            with conn:
                for name, sql in TABLES.items():
                    conn.execute(sql)
                    if name not in before:
                        out['created_tables'].append(name)
                idx_before = set(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'").fetchall())
                for sql in INDEXES:
                    conn.execute(sql)
                idx_after = set(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'").fetchall())
                out['created_indexes'] = sorted(idx_after - idx_before)
                ver = current_version(conn)
                if ver < SCHEMA_VERSION:
                    conn.execute(
                        'INSERT OR REPLACE INTO schema_version(version, applied_at, note) '
                        'VALUES(?,?,?)',
                        (SCHEMA_VERSION, time.strftime('%Y-%m-%d %H:%M:%S'),
                         'ensure_schema 自动应用'))
                    ver = SCHEMA_VERSION
                out['version'] = ver
            out['ok'] = True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out['error'] = '%s: %s' % (type(e).__name__, e)
        logger.warning('schema 初始化失败（忽略）：%s', e)
    return out


def report(db_path: str) -> Dict[str, Any]:
    """只读报告：表/索引/行数/版本，供体检接口使用。"""
    rep: Dict[str, Any] = {'ok': False, 'version': 0, 'tables': {}, 'not_connected': [],
                           'error': ''}
    if not db_path or not os.path.isfile(db_path):
        rep['error'] = '数据库文件不存在'
        return rep
    try:
        conn = _connect(db_path)
        try:
            rep['version'] = current_version(conn)
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()]
            idx = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()]
            for t in sorted(names):
                try:
                    n = conn.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
                except Exception:  # noqa: BLE001
                    n = -1
                rep['tables'][t] = {
                    'rows': n,
                    'note': TABLE_NOTES.get(t, ''),
                    'indexes': [i for i in idx if t in i],
                }
            rep['not_connected'] = [t for t, note in TABLE_NOTES.items()
                                    if note.startswith('预留')]
            rep['ok'] = True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        rep['error'] = '%s: %s' % (type(e).__name__, e)
    return rep
