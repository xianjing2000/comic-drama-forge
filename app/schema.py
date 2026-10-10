"""数据库 schema 的单一事实源（2026-10-10 设计审查修复）。

## 为什么需要
排查发现：tasks.db 的 4 张表由 **3 个模块各自建表**（5 处 CREATE TABLE）——
  ai_credentials_db.py:23,68 ｜ task_store.py:55,75 ｜ config_center.py:105
没有 schema 版本号、没有迁移记录、索引也不全。

本模块提供**幂等**的 ensure_schema()：只补建缺失的表/索引并记录版本，
不动任何既有数据，也不取代各模块自己的建表。

## ⚠️ 表 DDL 必须与各模块的权威定义逐列一致
初版我把 tasks.progress/total 写成 INTEGER —— 实际是 REAL（见 task_store._SCHEMA）。
因为用的是 CREATE TABLE IF NOT EXISTS，谁先建谁生效，既有库没暴露问题；
但**新装环境若先跑 ensure_schema，就会建成 INTEGER**（进度小数被截断）。
凡在本文件重复 DDL 的地方，都以对应模块的定义为准。

## 表状态说明（排查结论）
  tasks           —— 结构完整但**当前 0 行**：持久化任务队列（P0-4）建好了但从未接入，
                     task_store 只在 project_store 里被调用做清理。属**预留功能**，
                     已在 TABLE_NOTES 显式标注，由 report() 的 not_connected 暴露。
  units           —— 同上（tasks 的从表，0 行）。**已加 FK ... ON DELETE CASCADE**。
  ai_credentials  —— 使用中（3 行）。
  app_settings    —— 使用中（配置中心，11 行）。

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

#: 当前 schema 版本。任何结构变更都要 +1 并在下方注释记录。
#: v1 (2026-10-10): 建立本模块；补 idx_tasks_project_status / idx_tasks_created_at / idx_units_task；
#:                  units 增加 FOREIGN KEY ... ON DELETE CASCADE；_connect 开启 PRAGMA foreign_keys。
SCHEMA_VERSION = 2

#: 建表语句（IF NOT EXISTS，幂等）。
TABLES: Dict[str, str] = {
    'schema_version': (
        'CREATE TABLE IF NOT EXISTS schema_version ('
        ' version INTEGER PRIMARY KEY,'
        ' applied_at TEXT,'
        ' note TEXT)'),
    # 与 task_store._SCHEMA 逐列一致（progress/total 是 REAL，不是 INTEGER）。
    'tasks': (
        'CREATE TABLE IF NOT EXISTS tasks ('
        " id TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',"
        " payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',"
        ' progress REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0,'
        " result_path TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',"
        ' created_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT ' + "''" + ','
        " finished_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"),
    # 外键 ON DELETE CASCADE：删任务时自动清掉其 unit 行，从根上杜绝孤儿行。
    'units': (
        'CREATE TABLE IF NOT EXISTS units ('
        ' task_id TEXT NOT NULL, unit_key TEXT NOT NULL,'
        " status TEXT NOT NULL DEFAULT 'pending',"
        " result_path TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',"
        ' updated_at TEXT NOT NULL,'
        ' PRIMARY KEY (task_id, unit_key),'
        ' FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE)'),
    'ai_credentials': (
        'CREATE TABLE IF NOT EXISTS ai_credentials ('
        ' module TEXT PRIMARY KEY, base_url TEXT, model TEXT, api_key_cipher TEXT,'
        ' reasoning_effort TEXT, source TEXT, updated_at TEXT)'),
    'app_settings': (
        'CREATE TABLE IF NOT EXISTS app_settings ('
        ' key TEXT PRIMARY KEY, value TEXT, value_type TEXT, updated_at TEXT, note TEXT)'),
}

#: 索引（IF NOT EXISTS，幂等）。
#  2026-10-10 新增三个：实测 EXPLAIN 显示
#    · WHERE project=? AND status=? 只用到单列索引；
#    · ORDER BY created_at 依赖 TEMP B-TREE。
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
    # SQLite 的外键约束默认关闭，且是**连接级**设置 —— 必须每个连接都开，
    # 否则 units.task_id 的外键形同虚设（不跨连接继承）。
    try:
        conn.execute('PRAGMA foreign_keys=ON')
    except Exception:  # noqa: BLE001
        pass
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    """读已应用的 schema 版本（无表/无行返回 0）。"""
    try:
        row = conn.execute('SELECT MAX(version) FROM schema_version').fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _has_foreign_key(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(conn.execute('PRAGMA foreign_key_list(%s)' % table).fetchall())
    except Exception:  # noqa: BLE001
        return False


def _migrate_units_fk(conn: sqlite3.Connection) -> str:
    """给既有的 units 表补外键（SQLite 不支持 ALTER ADD CONSTRAINT，需重建）。

    安全检查（任一不满足就放弃迁移并返回原因，**绝不冒险**）：
      · 已有外键 → 跳过；
      · 表内存在孤儿行（task_id 指向不存在的 tasks.id）→ 拒绝，
        因为带外键建表会直接失败或丢数据；
    实际迁移：建新表 → 拷数据 → 换名 → 删旧表（在事务内）。
    """
    if _has_foreign_key(conn, 'units'):
        return '已存在外键，跳过'
    try:
        rows = conn.execute('SELECT COUNT(*) FROM units').fetchone()[0]
    except Exception:  # noqa: BLE001
        return 'units 表不存在'
    if rows:
        try:
            orphan = conn.execute(
                'SELECT COUNT(*) FROM units u LEFT JOIN tasks t ON u.task_id=t.id '
                'WHERE t.id IS NULL').fetchone()[0]
        except Exception:  # noqa: BLE001
            orphan = 0
        if orphan:
            return '存在 %d 行孤儿数据，拒绝自动迁移（请人工处理后再试）' % orphan
    cols = [x[1] for x in conn.execute('PRAGMA table_info(units)').fetchall()]
    if not cols:
        return 'units 表不存在'
    col_list = ', '.join(cols)
    try:
        with conn:
            conn.execute('ALTER TABLE units RENAME TO _units_pre_fk')
            conn.execute(TABLES['units'])
            conn.execute('INSERT INTO units (%s) SELECT %s FROM _units_pre_fk'
                         % (col_list, col_list))
            conn.execute('DROP TABLE _units_pre_fk')
        return '迁移完成（拷贝 %d 行）' % rows
    except Exception as e:  # noqa: BLE001
        logger.warning('units 外键迁移失败（保留原表）：%s', e)
        return '迁移失败：%s' % e


def ensure_schema(db_path: str) -> Dict[str, Any]:
    """幂等建表/建索引/补外键并记录版本。绝不抛异常。"""
    out: Dict[str, Any] = {'ok': False, 'version': 0, 'created_tables': [],
                           'created_indexes': [], 'fk_migration': '', 'error': ''}
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
            # ⚠️ 顺序很重要：外键迁移会 **重建 units 表**（SQLite 不支持 ADD CONSTRAINT），
            #    而重建会连带删掉该表上的索引。所以必须在**建索引之前**迁移，
            #    否则第一次跑完索引就没了，第二次才会被重新建出来（实测踩到过）。
            if 'units' in before:
                out['fk_migration'] = _migrate_units_fk(conn)
            with conn:
                idx_before = set(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'").fetchall())
                for sql in INDEXES:
                    conn.execute(sql)
                idx_after = set(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'").fetchall())
                out['created_indexes'] = sorted(idx_after - idx_before)
            with conn:
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
    """只读报告：表/索引/行数/版本/外键，供体检接口使用。"""
    rep: Dict[str, Any] = {'ok': False, 'version': 0, 'tables': {}, 'not_connected': [],
                           'foreign_keys': {}, 'error': ''}
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
                fks = []
                try:
                    for fk in conn.execute('PRAGMA foreign_key_list(%s)' % t).fetchall():
                        fks.append('%s -> %s.%s' % (fk[3], fk[2], fk[4]))
                except Exception:  # noqa: BLE001
                    pass
                rep['foreign_keys'][t] = fks
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
