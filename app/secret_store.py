# -*- coding: utf-8 -*-
"""密钥保管中心（P0-3 安全加固）

目标
----
1. **仓库零明文密钥**：api_key 不再以明文写入 ai_config.json / llm_config.json / qc_config.json；
2. **多层取值优先级**（高 → 低）：
   ① 环境变量（.env / 系统环境）—— 推荐的生产与团队协作方式
   ② 本地加密文件（output/.secrets.enc）—— 兜底，供 Web UI 保存密钥时使用
   ③ 遗留明文 json（读取时自动迁移，迁移后清除明文）
3. **对外只回显脱敏值**：mask_key 逻辑保持与既有实现一致，前端体验不变。

加密方案
--------
- Fernet 对称加密（cryptography 库）；
- 主密钥（SECRET_KEY）来源：环境变量 MJSCXT_SECRET_KEY > 本地 .secret_key 文件（自动生成，已 gitignore）；
- 加密文件格式：{"version": 1, "secrets": {"<namespace>": {"api_key": "<ciphertext>"}}}
- 若 cryptography 未安装，降级为「仅环境变量」模式并给出明确日志告警（不阻断启动）。

命名空间约定
------------
- "llm"                → 旧版单模型配置（llm_config.json）
- "ai.text" / "ai.qc" / "ai.chat" → AI 设置三模块
- "qc"                 → 质检配置（qc_client）
"""
from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import shutil
import threading
from typing import Optional

# 关键：必须经由 env_loader 保证 .env 已加载，否则 MJSCXT_SECRET_KEY 读不到，
# 会导致已加密的密钥全部解密失败（实际踩过的坑）。
from env_loader import PROJECT_ROOT_DIR as _ENV_ROOT  # noqa: F401
from fs_atomic import atomic_write_json

logger = logging.getLogger(__name__)

# 进程内读写锁：SecretStore 的 CRUD 是「_read_store → 改内存 → _write_store」的读改写。
# P1-1（A-11）：此前全类**零锁** + `_write_store` 用**固定** ``.tmp`` 名，两个并发线程
# （例：保存 AI 密钥 + 保存质检配置）A 写一半、B 以 "w" 截断重写同一 ``.tmp``，A 再
# ``os.replace`` → 发布出去的是**交错/截断的 JSON**；而 ``_read_store`` 解析失败后
# **静默返回空库** → 下次读改写把**整个密钥库清空**。加 RLock + 唯一临时名 + 损坏 .bak
# 恢复 / fail-loud 根治（与 project_store 的 P1-2 同一口径）。
_STORE_LOCK = threading.RLock()

# ===================== 主密钥解析 =====================

_CRYPTO_AVAILABLE = True
try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # noqa: BLE001  cryptography 未安装
    _CRYPTO_AVAILABLE = False
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

SECRET_KEY_ENV = "MJSCXT_SECRET_KEY"
SECRET_KEY_FILE = ".secret_key"
SECRETS_FILE = "secrets.enc"

# 环境变量兜底映射：namespace.key → 环境变量名
# 例：MJSCXT_API_KEY_TEXT / MJSCXT_API_KEY_QC / MJSCXT_API_KEY_CHAT / MJSCXT_API_KEY
ENV_KEY_MAP = {
    "ai.text": "MJSCXT_API_KEY_TEXT",
    "ai.qc": "MJSCXT_API_KEY_QC",
    "ai.chat": "MJSCXT_API_KEY_CHAT",
    "llm": "MJSCXT_API_KEY",
    "qc": "MJSCXT_API_KEY_QC",
}

_ENV_BASE_URL_MAP = {
    "ai.text": "MJSCXT_BASE_URL_TEXT",
    "ai.qc": "MJSCXT_BASE_URL_QC",
    "ai.chat": "MJSCXT_BASE_URL_CHAT",
    "llm": "MJSCXT_BASE_URL",
    "qc": "MJSCXT_BASE_URL_QC",
}

_ENV_MODEL_MAP = {
    "ai.text": "MJSCXT_MODEL_TEXT",
    "ai.qc": "MJSCXT_MODEL_QC",
    "ai.chat": "MJSCXT_MODEL_CHAT",
    "llm": "MJSCXT_MODEL",
    "qc": "MJSCXT_MODEL_QC",
}


def _secret_key_path(root_dir: str) -> str:
    return os.path.join(root_dir, SECRET_KEY_FILE)


def _secrets_path(root_dir: str) -> str:
    return os.path.join(root_dir, "output", SECRETS_FILE)


def get_or_create_secret_key(root_dir: str) -> Optional[bytes]:
    """取主密钥：环境变量优先，其次本地文件（不存在则生成）。返回 Fernet 可用的 key bytes。

    ⚠️ 2026-10-11 补诊断（真实故障）：本函数原先在「环境变量 / 本地文件」之间**静默**二选一，
    日志里看不出到底用了哪一把。而两者值不同时，加密数据只能用其中一个解开，
    表现就是反复出现「密钥解密失败（主密钥可能已更换）」且极难定位。
    现在：① 明确打印主密钥来源；② 两者同时存在且不一致时**响亮告警**。
    """
    if not _CRYPTO_AVAILABLE:
        return None
    env_key = (os.getenv(SECRET_KEY_ENV) or "").strip()
    path = _secret_key_path(root_dir)
    file_key = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                file_key = f.read().strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"主密钥文件读取失败，将重新生成：{e}")
    if env_key and file_key and env_key != file_key:
        logger.warning(
            "⚠️ 主密钥来源不一致：环境变量 %s 与文件 %s 的值不同。"
            "加密数据只能用其中一把解开 —— 若日志出现「密钥解密失败」，"
            "请确认二者同源（建议：把 %s 放进数据根 .env，或保证内容一致）。",
            SECRET_KEY_ENV, path, SECRET_KEY_ENV)
    if env_key:
        logger.info("主密钥来源：环境变量 %s", SECRET_KEY_ENV)
        return env_key.encode("utf-8") if isinstance(env_key, str) else env_key
    if file_key:
        logger.info("主密钥来源：文件 %s", path)
        return file_key.encode("utf-8")
    # 生成新密钥并落盘（文件本身已 gitignore）
    try:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except Exception as e:  # noqa: BLE001  Windows 上可能不支持
            logger.error("密钥库文件权限收紧（0o600）失败，密钥文件可能对同机其他用户可读：%s", e)
        logger.info(f"已生成新的本地主密钥：{path}（请勿提交到版本库）")
        return key
    except Exception as e:  # noqa: BLE001
        logger.warning(f"主密钥生成失败，密钥将仅从环境变量读取：{e}")
        return None


class SecretStore:
    """密钥保管器（按 root_dir 实例化；加密文件位于 output/secrets.enc）"""

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self._fernet = None
        if _CRYPTO_AVAILABLE:
            key = get_or_create_secret_key(root_dir)
            if key:
                try:
                    self._fernet = Fernet(key)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"主密钥格式非法，加密功能降级：{e}")

    # ---------- 加密文件读写 ----------

    @staticmethod
    def _bak_path(root_dir: str) -> str:
        return _secrets_path(root_dir) + ".bak"

    def _restore_corrupt(self, path: str) -> dict:
        """活文件损坏时从 ``.bak``（上一份可解析好版本）恢复，返回其 dict。

        P1-1（A-11）：恢复成功会原子写回活文件；``.bak`` 不存在/也不可解析时
        返回 ``None``（调用方据此 fail-loud，绝不静默返空库 → 防止下次读改写清空整个密钥库）。
        """
        bak = self._bak_path(self.root_dir)
        if not os.path.isfile(bak):
            return None
        try:
            with open(bak, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return None
        if not isinstance(data.get("secrets"), dict):
            data["secrets"] = {}
        tmp = f"{path}.restore.{os.getpid()}.{threading.get_ident()}.tmp"
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
            raise
        return data

    def _read_store(self) -> dict:
        path = _secrets_path(self.root_dir)
        if not os.path.isfile(path):
            return {"version": 1, "secrets": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if not isinstance(data.get("secrets"), dict):
                data["secrets"] = {}
            return data
        except Exception as e:  # noqa: BLE001
            # P1-1（A-11）：解析失败 = 文件损坏（非瞬时占用）。**不再静默返空库**——
            # 旧实现会把整个密钥库当「空」，下次 set/clear 读改写即把全部密钥写没了。
            # 先尝试从 .bak（最后一份好版本）恢复；恢复不了才 fail-loud。
            restored = self._restore_corrupt(path)
            if restored is not None:
                logger.warning(
                    "密钥库 %s 解析失败（文件损坏：%s），已从 .bak 恢复", os.path.basename(path), e)
                return restored
            logger.error(
                "密钥库 %s 解析失败（文件损坏：%s）且无可用 .bak，fail-loud 阻止基于空库写回",
                os.path.basename(path), e)
            raise ValueError(f"密钥库文件损坏且无可用备份：{path}（{e}）")

    def _write_store(self, data: dict) -> None:
        path = _secrets_path(self.root_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # P1-1（A-11）：发布前快照「当前可解析好版本」到 .bak（损坏时可恢复）
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
                shutil.copy2(path, self._bak_path(self.root_dir))
            except (OSError, ValueError):
                pass  # 活文件已损坏 → 不把它当好版本盖到 .bak
        # P1-1（A-11）：临时名**每次唯一**（旧实现固定 `.tmp`，并发写者互相截断）
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception as e:  # noqa: BLE001
            logger.error("密钥库文件权限收紧（0o600）失败，密钥文件可能对同机其他用户可读：%s", e)

    def _encrypt(self, plain: str) -> Optional[str]:
        if not self._fernet:
            return None
        try:
            return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥加密失败：{e}")
            return None

    def _decrypt(self, token: str) -> Optional[str]:
        if not self._fernet:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            logger.warning("密钥解密失败（主密钥可能已更换），该密钥需重新配置：%s", e)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥解密异常：{e}")
            return None

    # ---------- 对外 API ----------

    @property
    def encryption_available(self) -> bool:
        return self._fernet is not None

    def get_api_key(self, namespace: str) -> str:
        """按 环境变量 → 加密文件 的顺序取明文密钥；都没有则返回空串。"""
        env_name = ENV_KEY_MAP.get(namespace)
        if env_name:
            val = (os.getenv(env_name) or "").strip()
            if val:
                return val
        with _STORE_LOCK:
            try:
                data = self._read_store()
            except (ValueError, OSError):
                # 只读路径优雅降级（不写回）；损坏已在 _read_store 记 error + fail-loud
                return ""
        entry = (data.get("secrets") or {}).get(namespace) or {}
        token = entry.get("api_key")
        if token:
            plain = self._decrypt(token)
            if plain:
                return plain
        return ""

    def set_api_key(self, namespace: str, plain: str) -> bool:
        """加密写入密钥。返回是否成功（加密不可用时返回 False，调用方应提示改用环境变量）。"""
        if not plain:
            return self.clear_api_key(namespace)
        token = self._encrypt(plain)
        if not token:
            return False
        with _STORE_LOCK:
            try:
                data = self._read_store()
            except (ValueError, OSError) as e:
                # P1-1（A-11）：密钥库损坏且无 .bak → **绝不基于空库写回**（那会清空其它
                # 命名空间的所有密钥）。中止本次写入并告警。
                logger.error("密钥库不可读，中止 set_api_key（保护既有密钥）：%s", e)
                return False
            data.setdefault("secrets", {}).setdefault(namespace, {})["api_key"] = token
            try:
                self._write_store(data)
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"密钥落盘失败：{e}")
                return False

    def clear_api_key(self, namespace: str) -> bool:
        with _STORE_LOCK:
            try:
                data = self._read_store()
            except (ValueError, OSError) as e:
                # P1-1（A-11）：同上，损坏且无备份时不基于空库写回
                logger.error("密钥库不可读，中止 clear_api_key（保护既有密钥）：%s", e)
                return False
            entry = (data.get("secrets") or {}).get(namespace)
            if not entry:
                return True
            entry.pop("api_key", None)
            try:
                self._write_store(data)
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"密钥清除失败：{e}")
                return False

    # ---------- 非密钥字段的环境变量覆盖 ----------

    @staticmethod
    def env_base_url(namespace: str) -> str:
        name = _ENV_BASE_URL_MAP.get(namespace)
        return (os.getenv(name) or "").strip() if name else ""

    @staticmethod
    def env_model(namespace: str) -> str:
        name = _ENV_MODEL_MAP.get(namespace)
        return (os.getenv(name) or "").strip() if name else ""


# ===================== 全局单例 =====================

_STORE: Optional[SecretStore] = None
_STORE_ROOT: Optional[str] = None


def get_store(root_dir: str) -> SecretStore:
    """按根目录取（缓存）密钥保管器"""
    global _STORE, _STORE_ROOT
    root_dir = os.path.abspath(root_dir)
    if _STORE is None or _STORE_ROOT != root_dir:
        _STORE = SecretStore(root_dir)
        _STORE_ROOT = root_dir
    return _STORE


def mask_key(key: str) -> str:
    """api_key 脱敏：仅保留首尾少量字符（与 llm_client.mask_key 保持一致）"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def scrub_plaintext_key(config_path: str, namespace: str, root_dir: str) -> bool:
    """遗留明文迁移：把 json 里的 api_key 搬进加密库，并把 json 中该字段清空。

    覆盖三种结构（qc_config.json 的 endpoint_override 是实际踩到过的漏网之鱼）：
      ① 顶层 {"api_key": "..."}                      → namespace 参数
      ② 嵌套 {"endpoint_override": {"api_key": ...}}  → namespace 参数
      ③ 多模块 {"modules": {"text": {"api_key": ...}}} → 每模块各自的 ai.<name>

    返回是否发生了迁移（True = 本次把明文换成了加密存储）。
    """
    if not config_path or not os.path.isfile(config_path):
        return False
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"遗留配置读取失败，跳过迁移：{e}")
        return False

    migrated = False
    store = get_store(root_dir)

    # ② 嵌套 endpoint_override
    override = data.get("endpoint_override")
    if isinstance(override, dict):
        key = override.get("api_key")
        if isinstance(key, str) and key.strip():
            if store.set_api_key(namespace, key.strip()):
                override["api_key"] = ""
                migrated = True
            else:
                logger.warning(f"{os.path.basename(config_path)} 的 endpoint_override.api_key "
                               f"无法加密存储，保留明文（请配置 MJSCXT_SECRET_KEY）")

    # ① 顶层 api_key
    top = data.get("api_key")
    if isinstance(top, str) and top.strip():
        if store.set_api_key(namespace, top.strip()):
            data["api_key"] = ""
            migrated = True
        else:
            logger.warning(f"{os.path.basename(config_path)} 的 api_key "
                           f"无法加密存储，保留明文（请配置 MJSCXT_SECRET_KEY）")

    # ③ 多模块
    modules = data.get("modules")
    if isinstance(modules, dict):
        for m, cfg in modules.items():
            if not isinstance(cfg, dict):
                continue
            key = cfg.get("api_key")
            if isinstance(key, str) and key.strip():
                ns = f"ai.{m}" if m in ("text", "qc", "chat") else m
                if store.set_api_key(ns, key.strip()):
                    cfg["api_key"] = ""
                    migrated = True
                else:
                    logger.warning(f"模块 {m} 的密钥无法加密存储，保留明文"
                                   f"（请安装 cryptography 或配置 MJSCXT_SECRET_KEY）")

    if not migrated:
        return False

    try:
        atomic_write_json(config_path, data)
        logger.info(f"已迁移 {os.path.basename(config_path)} 中的明文密钥到加密库")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"遗留配置回写失败：{e}")
        return False
