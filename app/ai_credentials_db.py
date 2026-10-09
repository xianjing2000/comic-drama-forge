# -*- coding: utf-8 -*-
"""AI 凭证统一事实源（tasks.db · ai_credentials 表）

背景
----
此前「AI 设置」密钥落 加密库 `secrets.enc`（ai.qc / qc 双槽），质检任务实际读
`qc` 槽、AI 设置页读 `ai.qc` 槽，两套数据只在保存路由里被 best-effort 顺手复制一次，
任何绕过该路由的修改（只存 text 模块 / 质检页单存 / env 覆盖 / 同步异常被吞）都会造成
**双槽永久漂移 → 质检 401**。

本模块把 AI 凭证（text / qc / chat 三模块的 base_url / api_key / model / reasoning_effort）
收敛到 `output/tasks.db` 的 `ai_credentials` 表作为**唯一事实源**：

- 前端保存写这里；
- 后端 qc_client / llm_client 每次任务前**实时重读**这里（DB 单文件短连接，无缓存）；
- 密钥列存 **Fernet 密文**（复用 secret_store 的主密钥 `.secret_key`），仓库/库内零明文；
- **env 变量优先级不变**（MJSCXT_API_KEY_* > DB）—— 运维部署仍可用 env 覆盖；
- 启动时自动一次性迁移：从 secrets.enc 的 ai.qc / qc / ai.text / ai.chat 槽 + 遗留 json
  补齐 DB（仅当 DB 空且旧源有值时），迁移后旧槽不再被任务链路读取。

设计约束
--------
- 表 DDL 自包含在本模块（`CREATE TABLE IF NOT EXISTS`），不侵入 task_store 的 schema 所有权；
- 各方法独立开短连接（与 TaskStore._conn 同一约定），加模块锁串行「读改写」；
- 所有读路径异常**降级返回空值**（绝不打断质检/LLM 任务），写路径异常向上抛（保存路由
  自己兜成「响亮降级」）；
- 对外只给脱敏值（has_api_key / api_key_masked），明文 api_key 仅 `get_credentials` 内部使用。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import secret_store

logger = logging.getLogger(__name__)

# 项目根目录（定位 output/tasks.db 与加密主密钥 .secret_key）
#
# ⚠️ 支持环境变量覆盖，**默认值与生产行为完全不变**，仅供测试 / CI 做整体隔离：
#     MJSCXT_CRED_ROOT → 项目根（同时决定加密主密钥 / secrets.enc 的位置）
#     MJSCXT_CRED_DB   → DB 文件绝对路径（优先级高于 MJSCXT_CRED_ROOT）
#
# 为什么必须有：`app.py` 是**模块级初始化**（`import app` 即触发
# `migrate_from_legacy()` 写 DB、`qc_client.save_config` 也会同步写 DB）。
# 隔离测试若只重指向 `qc_client._PROJECT_ROOT` / `ai_config._ROOT_DIR` 而漏掉这里，
# 会把测试用的假凭证写进**用户真实的** output/tasks.db（2026-09-23 已实测踩到：
# qc 模块被写成 probe.example/v1 + 假钥，事后按真实旧槽值修复）。
# 默认根走 env_loader.PROJECT_DATA_DIR（MJSCXT_DATA_DIR 守卫，默认=源根，行为不变）；
# 安装版（Program Files 只读）设 MJSCXT_DATA_DIR 后 tasks.db / 密钥库落可写目录。
try:
    from env_loader import PROJECT_DATA_DIR as _DEFAULT_CRED_ROOT
except Exception:  # noqa: BLE001  兜底
    _DEFAULT_CRED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# MJSCXT_CRED_ROOT 仍为最高优先级（测试/CI 隔离），未设才落数据根。
_PROJECT_ROOT = os.path.abspath(
    os.getenv("MJSCXT_CRED_ROOT") or _DEFAULT_CRED_ROOT)
_DB_PATH = os.getenv("MJSCXT_CRED_DB") or os.path.join(_PROJECT_ROOT, "output", "tasks.db")

# 三个 AI 模块（与 ai_config.MODULES 对齐；qc 即质检视觉模型）
MODULES = ("text", "qc", "chat")

_CREDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_credentials (
    module           TEXT PRIMARY KEY,
    base_url         TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL DEFAULT '',
    api_key_cipher   TEXT NOT NULL DEFAULT '',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL
);
"""

_LOCK = threading.RLock()
_SCHEMA_READY = False


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception as e:  # noqa: BLE001
        logger.debug("ai_credentials 开启 WAL 失败（忽略）：%s", e)
    return conn


def _ensure_schema() -> None:
    global _SCHEMA_READY
    with _LOCK:
        if _SCHEMA_READY:
            return
        conn = _conn()
        try:
            conn.executescript(_CREDS_SCHEMA)
            conn.commit()
            _SCHEMA_READY = True
        finally:
            conn.close()


# ===================== 加解密（复用 secret_store 主密钥） =====================

def _cipher(plain: str) -> str:
    """加密明文密钥 → Fernet 密文。加密不可用时返回空串（调用方需降级）。"""
    if not plain:
        return ""
    try:
        store = secret_store.get_store(_PROJECT_ROOT)
        # 借用 SecretStore 实例的 Fernet 实例加密（与 secrets.enc 同一主密钥）
        if store._fernet:
            return store._fernet.encrypt(plain.encode("utf-8")).decode("ascii")
        # 主密钥不可用：降级存明文（响亮告警，不阻断）
        logger.error("ai_credentials 加密不可用（缺 cryptography 或主密钥），"
                     "本次以明文落库（请配置 MJSCXT_SECRET_KEY 后重新保存）")
        return plain
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_credentials 加密失败，降级明文落库：%s", e)
        return plain


def _decipher(cipher_text: str) -> str:
    """解密 Fernet 密文 → 明文。旧明文行 / 解密失败返回原值或空串（不抛异常）。"""
    if not cipher_text:
        return ""
    try:
        store = secret_store.get_store(_PROJECT_ROOT)
        if store._fernet:
            try:
                return store._fernet.decrypt(cipher_text.encode("ascii")).decode("utf-8")
            except Exception:
                # 非 Fernet 密文（历史明文行）→ 原样返回
                return cipher_text
        return cipher_text
    except Exception:  # noqa: BLE001
        return ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ===================== 对外 API =====================

def set_credentials(module: str, base_url: str = "", model: str = "",
                    api_key: Optional[str] = None, reasoning_effort: Optional[str] = None,
                    source: str = "") -> dict:
    """写入 / 更新某模块的 AI 凭证（api_key 为 None = 不改密钥；空串 = 清空密钥）。

    base_url / model 直接覆盖；reasoning_effort None = 不改、"" = 清空。
    返回该模块最新的公开视图（含脱敏密钥）。
    """
    if module not in MODULES:
        raise ValueError(f"未知的 AI 凭证模块：{module}")
    _ensure_schema()
    with _LOCK:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT api_key_cipher, reasoning_effort FROM ai_credentials WHERE module=?",
                (module,)).fetchone()
            existing_key = _decipher(row["api_key_cipher"]) if row else ""
            existing_re = row["reasoning_effort"] if row else ""

            if api_key is None:
                final_key = existing_key
                cipher_col = row["api_key_cipher"] if row else ""
            elif api_key == "":
                final_key = ""
                cipher_col = ""
            else:
                final_key = str(api_key).strip()
                cipher_col = _cipher(final_key) if final_key else ""

            final_re = existing_re if reasoning_effort is None else \
                (reasoning_effort or "").strip()

            conn.execute(
                "INSERT INTO ai_credentials (module, base_url, model, api_key_cipher, "
                "reasoning_effort, source, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(module) DO UPDATE SET base_url=excluded.base_url, "
                "model=excluded.model, api_key_cipher=excluded.api_key_cipher, "
                "reasoning_effort=excluded.reasoning_effort, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (module, (base_url or "").strip(), (model or "").strip(),
                 cipher_col, final_re, source or "", _now()))
            conn.commit()
        finally:
            conn.close()
    return public_view(module)


def get_credentials(module: str) -> dict:
    """读某模块凭证（**每次纯重读 DB，无缓存** → 前端改了立即生效）。

    返回含**明文** api_key（仅后端内部用）；env 变量若配置则**覆盖** DB 值
    （与 secret_store 既有优先级一致：env > 库内）。
    """
    if module not in MODULES:
        raise ValueError(f"未知的 AI 凭证模块：{module}")
    _ensure_schema()
    with _LOCK:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT * FROM ai_credentials WHERE module=?", (module,)).fetchone()
        finally:
            conn.close()
    if not row:
        return {"module": module, "base_url": "", "model": "", "api_key": "",
                "reasoning_effort": "", "configured": False}
    out = {
        "module": module,
        "base_url": row["base_url"] or "",
        "model": row["model"] or "",
        "api_key": _decipher(row["api_key_cipher"]),
        "reasoning_effort": row["reasoning_effort"] or "",
        "source": row["source"] or "",
        "updated_at": row["updated_at"] or "",
    }
    # env 覆盖（优先级最高，与 secret_store 既有口径一致：env > DB 内值）
    # ⚠️ 只覆盖密钥这一列（走 env 变量），base_url/model 亦沿用 secret_store 的 env 映射；
    # 刻意**不**回读 secrets.enc（那是要退役的双槽），env 仍是唯一的库外来源。
    ns = f"ai.{module}"
    _env_key = _env_api_key(ns)
    if _env_key:
        out["api_key"] = _env_key
    env_base = secret_store.SecretStore.env_base_url(ns)
    env_model = secret_store.SecretStore.env_model(ns)
    if env_base:
        out["base_url"] = env_base
    if env_model:
        out["model"] = env_model
    out["configured"] = bool(out["base_url"] and out["api_key"] and out["model"])
    return out


def _env_api_key(ns: str) -> str:
    env_name = secret_store.ENV_KEY_MAP.get(ns)
    if env_name:
        return (os.getenv(env_name) or "").strip()
    return ""


def public_view(module: str) -> dict:
    """单模块脱敏视图（绝不含明文 api_key，仅 has_api_key + mask）。"""
    ep = get_credentials(module)
    key = ep.get("api_key") or ""
    return {
        "module": module,
        "configured": ep.get("configured", False),
        "has_api_key": bool(key),
        "api_key_masked": secret_store.mask_key(key),
        "base_url": ep.get("base_url", ""),
        "model": ep.get("model", ""),
        "reasoning_effort": ep.get("reasoning_effort", ""),
        "source": ep.get("source", ""),
        "updated_at": ep.get("updated_at", ""),
    }


def clear_credentials(module: str = None) -> dict:
    """清空单模块或全部（None = 三模块全清）。"""
    _ensure_schema()
    with _LOCK:
        conn = _conn()
        try:
            if module:
                if module not in MODULES:
                    raise ValueError(f"未知的 AI 凭证模块：{module}")
                conn.execute("DELETE FROM ai_credentials WHERE module=?", (module,))
            else:
                conn.execute("DELETE FROM ai_credentials")
            conn.commit()
        finally:
            conn.close()
    return {"cleared": module or list(MODULES)}


def modules_with_key() -> set:
    """返回 DB 中「已配置密钥」的模块集合（``api_key_cipher`` 非空）。

    A1（2026-09-23 收口）：迁移必须按**模块**判空，而不是「DB 里有没有任何一行」。
    旧口径下 DB 只要存在任意一行（例如只有 `text`），`qc` / `chat` 的旧槽密钥就
    永远补不进来，迁移后新写入旧槽的密钥同样进不了 DB。

    ⚠️ 判据刻意取「密钥」而非「任一字段非空」：`base_url` / `model` 可能已被本模块
    的其它写点（如 qc 端点同步）填上，若据此判「已有值」就会把**密钥**永久挡在门外。
    """
    _ensure_schema()
    with _LOCK:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT module FROM ai_credentials WHERE api_key_cipher<>''").fetchall()
        finally:
            conn.close()
    return {r["module"] for r in rows}


# ===================== 一次性迁移（旧 secrets.enc 双槽 → DB） =====================

def migrate_from_legacy(force: bool = False) -> dict:
    """把旧 secret_store 的 ai.text / ai.qc / ai.chat / qc 槽 + ai_config.json 非密钥字段
    迁入 DB。**按模块**判空：只补齐 DB 中尚无值的模块，幂等。

    返回 {migrated: [模块], skipped: [原因]}。任何单模块迁移失败不影响其它模块。
    """
    migrated, skipped = [], []
    # A1（2026-09-23 收口）：判据从「DB 里有没有**任何**一行」改为「**该模块**是否已有值」。
    # 旧写法 `if has_credentials() and not force: return` 会在 DB 只要有任何一行（如仅 text）
    # 时整体早退 —— qc / chat 的旧槽密钥永远补不进来；而迁移之后新写入旧槽的密钥同样进不了
    # DB，「DB 单点」名存实亡（09-23 实测：secrets.enc 里 ai.qc 是真钥、tasks.db 只有 text 一行）。
    # force=True 仍为全量强制覆盖。
    existing = set() if force else modules_with_key()

    store = secret_store.get_store(_PROJECT_ROOT)
    # 模块 → 旧槽优先级：`ai.*` 槽是前端「AI 设置」保存的完整值；裸槽（`qc` 等）
    # 可能残留 `sk-test` 之类占位值。若盲目「裸槽优先」，会把占位 key 固化成
    # 唯一事实源 —— 质检 401 的老毛病反而被迁移写死。故改为「可信度优先」。
    slot_map = {
        "text": ["ai.text", "text"],
        "qc": ["ai.qc", "qc"],
        "chat": ["ai.chat", "chat"],
    }
    _KEY_TRUST_MIN_LEN = 16   # 正常 key（sk-…）远长于此；短于此判为占位/无效

    def _trusted_key(candidates: list) -> tuple:
        """按候选槽顺序取第一个「可信」key → (key, slot)。

        可信 = 非空 且 len >= _KEY_TRUST_MIN_LEN。若所有非空候选都偏短，
        兜底返回第一个非空值（宁可用可疑值，也不静默丢 key）。
        """
        fallback = ("", "")
        for slot in candidates:
            try:
                v = (store.get_api_key(slot) or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.debug("旧槽 %s 读取失败：%s", slot, e)
                continue
            if not v:
                continue
            if len(v) >= _KEY_TRUST_MIN_LEN:
                return v, slot
            if not fallback[0]:
                fallback = (v, slot)
        return fallback

    for m in MODULES:
        if m in existing:
            skipped.append(f"{m}: DB 已有值，跳过（force=True 可强制覆盖）")
            continue
        try:
            legacy_key, legacy_slot = _trusted_key(slot_map[m])
            if legacy_slot and len(legacy_key) < _KEY_TRUST_MIN_LEN:
                logger.warning("ai_credentials 迁移 %s：旧槽均为短值，采用 %s（len=%d），"
                               "疑似占位 key，请在「AI 设置」页重新保存", m, legacy_slot, len(legacy_key))
            legacy_base = legacy_model = ""
            legacy_re = ""
            # 非密钥字段从 ai_config.json 读（json 里 key 恒空、base_url/model 在明文）
            try:
                import ai_config
                cfg = ai_config.load_config(
                    os.path.join(_PROJECT_ROOT, "ai_config.json"),
                    os.path.join(_PROJECT_ROOT, "llm_config.json"))
                ep = ai_config.get_module(cfg, m)
                legacy_base = (ep.get("base_url") or "").strip()
                legacy_model = (ep.get("model") or "").strip()
                legacy_re = (ep.get("reasoning_effort") or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.debug("迁移 %s：读取旧 ai_config 非密钥字段失败（忽略）：%s", m, e)
            if not (legacy_key or legacy_base or legacy_model):
                skipped.append(f"{m}: 旧源无值")
                continue
            set_credentials(m, base_url=legacy_base, model=legacy_model,
                            api_key=legacy_key or None, reasoning_effort=legacy_re or None,
                            source="migrate_legacy")
            migrated.append(m)
            logger.info("ai_credentials 迁移 %s 模块：%s", m,
                        "key" if legacy_key else "仅 base/model")
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{m}: {e}")
    if migrated:
        logger.info("ai_credentials 迁移完成：%s", migrated)
    return {"migrated": migrated, "skipped": skipped}
