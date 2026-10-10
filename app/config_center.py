"""统一配置中心（2026-10-10，第二步：持久化到数据库）。

## 为什么需要（承接 config_doctor 的诊断）
用户在多个文件里反复遇到「改了一处、另一处没改」的问题：
  · 346 个大写常量散落 12 个文件；
  · 同一语义重复定义（COVERAGE_MAX_ROUNDS 在 continuity 与 novel_to_script）；
  · 阈值链路跨 config → coverage → continuity 三段，任一段漏改就行为分叉。

## 设计（零侵入，不重写任何模块的取值代码）
把 config_doctor.REGISTRY 当作**参数的唯一登记表**，实际值按优先级解析：
    数据库(app_settings 表)  >  环境变量(MJSCXT_*)  >  代码默认值(REGISTRY.default)

启动时调 apply_to_runtime()：把最终生效值 **setattr 回各模块的同名属性**
（如 continuity.COVERAGE_MAX_ROUNDS = 3）。这样：
  · 现有业务代码**一行都不用改**（它们照旧读模块常量）；
  · 但所有可调参数的最终值由配置中心统一决定 —— 不再有「某处漏改」；
  · 数据库是唯一可写入口，改一次全局生效。

## 纪律
  · 全程 fail-open：数据库不可用/表不存在/值非法，一律退回默认值并记日志，绝不让服务起不来；
  · 只回写 REGISTRY 里登记过的键 —— 不碰实现常量（正则、枚举、表结构）；
  · 值类型按其登记 type 强转，转不过去的忽略（防止脏数据把服务搞崩）；
  · 每次写入留 updated_at 与 note，便于回溯「谁什么时候改了什么」。
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 数据库文件名（复用任务库，避免再引入一个 sqlite 文件）
_DB_FILENAME = 'tasks.db'

#: 键 → 环境变量名（沿用历史 env 口径，保持向后兼容）
ENV_MAP: Dict[str, str] = {
    'coverage_threshold': 'MJSCXT_COVERAGE_THRESHOLD',
}

#: 键别名 → 正式键（2026-10-10 用户要求「相同的配置字段要统一名称」）。
#  背景：配置 key 的生成规则统一为「常量名小写化」，据此个别历史手写 key 需要正名；
#  但旧调用方/旧文档里可能仍在用旧名，故保留别名，读与写都自动映射到正式键。
#  这比直接删除旧名安全 —— 不会让任何既有调用静默失效。
ALIASES: Dict[str, str] = {
    'target_shots': 'novel_default_shots',
    # ⭐ 2026-10-10 用户口径「相同的配置字段要统一名称」的继续：镜数此前有三个名字
    #   （NOVEL_DEFAULT_SHOTS / target_shots / shots_per_episode），其中一个还带着
    #   冲突的默认值 12。正式名统一为 novel_default_shots（常量 NOVEL_DEFAULT_SHOTS
    #   小写化）；target_shots 与 shots_per_episode 都作为别名映射过去，
    #   读写自动归一 —— 这样「改一处」即可，不会再出现改了正式键、旧键仍生效。
    'shots_per_episode': 'novel_default_shots',
    # ⚠️ 2026-10-10：qc_config.json 用的键名与代码常量名不一致（同一作用、两个名字）。
    #    统一规则是「JSON 键 == 配置中心 key == 代码常量小写化」，故正式键为
    #    disable_thinking_default；旧的 disable_thinking 作为别名保留，
    #    overlay_config 会把两者一起覆盖，避免「改了正式键、旧键仍在生效」。
    'disable_thinking': 'disable_thinking_default',
}


def canonical_key(key: str) -> str:
    """把别名解析为正式键（无别名则原样返回）。"""
    k = str(key or '').strip()
    return ALIASES.get(k, k)


def db_path() -> str:
    """配置库路径（与任务库同一个文件）。"""
    try:
        import config
        return getattr(config, 'TASKS_DB_PATH', '') or ''
    except Exception:  # noqa: BLE001
        return ''


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn




@contextlib.contextmanager
def _db(path: str):
    """连接上下文管理器：退出时**关闭连接**。

    ⚠ 不能写成 with _connect(p) as conn —— sqlite3 的 Connection
    作为上下文管理器时**只提交/回滚事务，不会关闭连接**，会留下一堆未关闭的
    sqlite3.Connection（用 -W error::ResourceWarning 跑测试时成片报出）。
    这里显式关闭，避免长期挂机场景下的句柄泄漏。
    """
    conn = _connect(path)
    try:
        with conn:
            yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def ensure_table(path: str = None) -> bool:
    """建表（幂等）。返回是否可用。"""
    p = path or db_path()
    if not p:
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _db(p) as conn:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS app_settings ('
                '  key TEXT PRIMARY KEY,'
                '  value TEXT,'
                '  value_type TEXT,'
                '  updated_at TEXT,'
                '  note TEXT'
                ')')
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning('配置库建表失败（忽略，将退回代码默认值）：%s', e)
        return False


def _coerce(raw: Any, t: str) -> Tuple[bool, Any]:
    """按登记类型强转；失败返回 (False, 原值)。"""
    try:
        if t == 'bool':
            if isinstance(raw, bool):
                return True, raw
            return True, str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
        if t == 'int':
            return True, int(float(str(raw).strip()))
        if t == 'float':
            return True, float(str(raw).strip())
        return True, str(raw)
    except Exception:  # noqa: BLE001
        return False, raw


def _in_range(val: Any, rng: Optional[Tuple[float, float]]) -> bool:
    if not rng:
        return True
    try:
        return float(rng[0]) <= float(val) <= float(rng[1])
    except Exception:  # noqa: BLE001
        return True


def resolve_all() -> Dict[str, Dict[str, Any]]:
    """解析全部登记参数的**最终生效值**及其来源。

    返回 {key: {value, source, type, group, desc, invalid}}，
    source ∈ {'db', 'env', 'default'}。
    """
    try:
        import config_doctor as CD
    except Exception as e:  # noqa: BLE001
        logger.warning('配置登记表不可用：%s', e)
        return {}
    reg = getattr(CD, 'REGISTRY', {}) or {}
    db_vals: Dict[str, str] = {}
    p = db_path()
    if p and ensure_table(p):
        try:
            with _db(p) as conn:
                for k, v in conn.execute('SELECT key, value FROM app_settings'):
                    db_vals[str(k)] = '' if v is None else str(v)
        except Exception as e:  # noqa: BLE001
            logger.warning('读取配置库失败（忽略）：%s', e)
    out: Dict[str, Dict[str, Any]] = {}
    for key, meta in reg.items():
        t = meta.get('type') or 'str'
        rng = meta.get('range')
        value, source, invalid = meta.get('default'), 'default', None
        # ① 数据库
        if key in db_vals:
            ok, v = _coerce(db_vals[key], t)
            if ok and _in_range(v, rng):
                value, source = v, 'db'
            else:
                invalid = '数据库值 %r 非法（类型 %s / 范围 %s），已退回默认值' % (
                    db_vals[key], t, rng)
                logger.warning('配置项 %s：%s', key, invalid)
        # ② 环境变量
        if source == 'default':
            env_name = meta.get('env') or ENV_MAP.get(key)
            if env_name:
                raw = os.environ.get(env_name)
                if raw not in (None, ''):
                    ok, v = _coerce(raw, t)
                    if ok and _in_range(v, rng):
                        value, source = v, 'env'
                    else:
                        invalid = '环境变量 %s=%r 非法，已退回默认值' % (env_name, raw)
                        logger.warning('配置项 %s：%s', key, invalid)
        out[key] = {'value': value, 'source': source, 'type': t,
                    'group': meta.get('group'), 'desc': meta.get('desc'),
                    'owner': meta.get('owner'), 'invalid': invalid,
                    'must_match': meta.get('must_match') or []}
    return out


def apply_to_runtime() -> int:
    """把最终生效值回写到各模块属性（启动时调用一次）。

    返回成功回写的项数。fail-open：任何一项失败只记日志。
    """
    resolved = resolve_all()
    n = 0
    for key, info in resolved.items():
        owner = str(info.get('owner') or '')
        if not owner:
            continue
        targets = [owner] + list(info.get('must_match') or [])
        for dotted in targets:
            try:
                parts = dotted.split('.')
                mod = __import__(parts[0])
                obj = mod
                for p in parts[1:-1]:
                    obj = obj[p] if isinstance(obj, dict) else getattr(obj, p)
                last = parts[-1]
                if isinstance(obj, dict):
                    obj[last] = info['value']
                else:
                    setattr(obj, last, info['value'])
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning('配置回写失败 %s=%r（%s）：%s',
                               dotted, info.get('value'), key, e)
    if n:
        from_db = sum(1 for i in resolved.values() if i.get('source') == 'db')
        from_env = sum(1 for i in resolved.values() if i.get('source') == 'env')
        logger.info('配置中心已生效：%d 项（数据库 %d / 环境变量 %d / 默认 %d）',
                    len(resolved), from_db, from_env,
                    len(resolved) - from_db - from_env)
    return n


def get_value(key: str) -> Any:
    """读取单个参数的当前生效值（不触发回写）。自动解析别名。"""
    return (resolve_all().get(canonical_key(key)) or {}).get('value')


def overlay_config(cfg: dict) -> dict:
    """把配置中心里「被显式改过」的项叠加到模块自己的配置字典上。

    ## 解决什么问题（2026-10-10 实测发现）
    qc_client / llm_client 有**自己的配置链**：load_config() 只读自己的 JSON 文件
    （qc_config.json / ai_config.json），完全不经过配置中心。实测证据：
        qc_config.json 的 min_tokens_when_thinking = 1024
        qc_client.load_config() 返回               = 1024   ← 只读 JSON
        配置中心 DB 里的值                          = 24576
    → 配置中心改的值对这两个模块**完全无效**，还造成同一参数在 JSON 与 DB 双写。
    本函数在它们的 load_config() 返回前做一次叠加，把链路接通。

    ## 关键设计：只覆盖「来源为 db/env」的项
    若某项仍是代码默认值（source == 'default'），**不覆盖** JSON ——
    这样用户没在配置中心动过的参数，JSON 里的既有配置依然生效，
    不会因为「登记了 93 项」就把 JSON 里 38 个键全部冲掉。零破坏性。

    别名同时处理：JSON 里可能用旧名（如 disable_thinking），
    ALIASES 里注册过的旧名会一并被覆盖，避免「改了正式键、旧键还在生效」。
    """
    if not isinstance(cfg, dict):
        return cfg
    try:
        resolved = resolve_all()
    except Exception as e:  # noqa: BLE001  配置中心异常绝不影响模块自己的配置
        logger.warning('配置中心叠加失败（忽略，沿用模块自身配置）：%s', e)
        return cfg
    out = dict(cfg)
    n = 0
    for key, info in resolved.items():
        if info.get('source') == 'default':
            continue                      # 未被显式改过 → 不覆盖 JSON
        # 正式键 + 所有指向它的旧别名
        targets = [key] + [a for a, c in ALIASES.items() if c == key]
        for t in targets:
            if t in out:
                out[t] = info['value']
                n += 1
    if n:
        logger.info('配置中心叠加生效：覆盖 %d 项（模块 JSON < 配置中心）', n)
    return out


def set_value(key: str, value: Any, note: str = '') -> Dict[str, Any]:
    """写入数据库（唯一可写入口）。返回 {ok, error, applied, value}。"""
    try:
        import config_doctor as CD
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': '登记表不可用：%s' % e}
    key = canonical_key(key)
    meta = (getattr(CD, 'REGISTRY', {}) or {}).get(key)
    if not meta:
        return {'ok': False, 'error': '未登记的配置键：%s（只有 REGISTRY 里的可调参数才允许修改）' % key}
    t = meta.get('type') or 'str'
    ok, v = _coerce(value, t)
    if not ok:
        return {'ok': False, 'error': '值 %r 无法转为 %s' % (value, t)}
    if not _in_range(v, meta.get('range')):
        return {'ok': False, 'error': '值 %r 超出允许范围 %s' % (v, meta.get('range'))}
    p = db_path()
    if not p or not ensure_table(p):
        return {'ok': False, 'error': '配置库不可用'}
    try:
        with _db(p) as conn:
            conn.execute('INSERT INTO app_settings(key, value, value_type, updated_at, note) '
                         'VALUES(?,?,?,?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value, '
                         'value_type=excluded.value_type, updated_at=excluded.updated_at, '
                         'note=excluded.note',
                         (key, str(v), t, time.strftime('%Y-%m-%d %H:%M:%S'), note or ''))
        applied = apply_to_runtime()
        logger.info('配置中心写入：%s=%r（重新回写 %d 项）', key, v, applied)
        return {'ok': True, 'applied': applied, 'value': v}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}


def snapshot() -> Dict[str, Any]:
    """供 API/界面：全部参数的生效值、来源、默认值与说明。"""
    resolved = resolve_all()
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for key, info in resolved.items():
        groups.setdefault(str(info.get('group') or '其它'), []).append({
            'key': key, 'value': info.get('value'), 'source': info.get('source'),
            'type': info.get('type'), 'desc': info.get('desc'),
            'owner': info.get('owner'), 'invalid': info.get('invalid'),
        })
    return {'ok': True, 'db': db_path(), 'count': len(resolved), 'groups': groups,
            'source_counts': {
                'db': sum(1 for i in resolved.values() if i.get('source') == 'db'),
                'env': sum(1 for i in resolved.values() if i.get('source') == 'env'),
                'default': sum(1 for i in resolved.values() if i.get('source') == 'default'),
            }}
