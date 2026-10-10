"""
统一「AI 设置」配置管理 —— 文本分析 / 质检 / 对话总控 三个相互独立的模型模块

配置文件：项目根目录 ai_config.json
{
  "version": 1,
  "modules": {
    "text": {"base_url": "", "api_key": "", "model": "", "reasoning_effort": "", "updated_at": null},
    "qc":   {...},
    "chat": {...}
  },
  "migrated_from": "llm_config.json",   # 可选，记录首次自动迁移来源
  "updated_at": "2026-09-11T09:21:03"
}

设计约束：
- 三个模块完全独立：各自的 base_url / api_key / model、独立连通性测试、独立脱敏回显、独立保存 / 清空；
  质检模块不再复用文本分析模型的配置（LLM 可能只配了纯文本模型，无法做视觉质检）。
- api_key 明文只落盘，永不回显（对外只给 api_key_masked + has_api_key）。
- 保存时 api_key 留空，或提交回的是脱敏值（含 *），一律视为「不改动原密钥」。
- reasoning_effort（思考档位）只对「思考不可关闭」的模型有意义（如 GLM-5.3-Flash）：
  留空 = 不注入该参数，由服务端取默认档；low / high / max 为合法档位；off = 显式关闭思考
  （= disable_thinking，对 Qwen/vLLM 系有效，对 always-on 模型不会烧更多 token）。
  它与 llm_client 的 disable_thinking 是**互斥的两套机制**：一旦设了档位，就不再尝试关思考。
- 兼容旧 llm_config.json：首次读取时若 text 模块为空而旧配置存在，自动迁移（不丢原有配置）。
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
from datetime import datetime

from llm_client import build_chat_url, mask_key
from llm_client import load_config as _load_legacy_config
from llm_client import REASONING_EFFORT_LEVELS, REASONING_EFFORT_OFF
import secret_store

logger = logging.getLogger(__name__)

# 数据根目录（定位加密密钥库 output/secrets.enc 与主密钥 .secret_key）。
# 走 env_loader.PROJECT_DATA_DIR（MJSCXT_DATA_DIR 守卫，默认=源根，行为不变）；
# 安装版（Program Files 只读）设 MJSCXT_DATA_DIR 后密钥库落可写目录。
try:
    from env_loader import PROJECT_DATA_DIR as _ROOT_DIR
except Exception:  # noqa: BLE001  兜底：env_loader 未就绪时退回源根
    _ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _store():
    return secret_store.get_store(_ROOT_DIR)


# =====================================================================
# 凭证单一事实源闭合（2026-09-23）
# ---------------------------------------------------------------------
# 读侧（本模块 `get_module`、`qc_client.load_config`、`ai_selfcheck`）自 P0-5 起
# **一律 DB 优先**（`output/tasks.db` · ai_credentials 表），所以**任务真正用的是 DB 里那把钥**。
# 但写侧此前是「谁调用谁负责同步」：只有 `/api/ai/module`(`/api/ai/config`) 与
# `/api/ai/config/clear` 在**路由里**顺手补了一次 `ai_credentials_db` 写入；而
# `/api/ai/settings`（兼容旧接口，`llm_api_key` 字段）与 `/api/llm/config/clear`
# 直接调本模块的 `save_module` / `clear_module` —— 这两个函数**只写 json + 旧加密库**，
# 于是「前端配的新密钥」永远进不了 DB：
#     · 任务读 DB 旧值 → 继续用旧密钥（表现：改了 key 但任务照旧 401 / 用错账号）；
#     · 加密库已是新值 → 万一 DB 被清空又「回落」出新旧两套，排查时自相矛盾。
# 这就是用户报的「前端配置的 ai 密钥与后端实际任务调用的不一致」。
#
# 修法：把镜像**下沉到写函数内部**，任何调用方（含将来新增的路由）都自动闭合。
# 失败必须**响亮降级**且**绝不抛**：json / 加密库已经落盘成功，不能因为镜像失败
# 让保存接口 500 或把用户的改动回滚（此时读侧会回落旧口径 → 必须让用户看见告警）。
# =====================================================================

def _mirror_credentials_db(module: str, base_url: str = "", model: str = "",
                           api_key: str = None, reasoning_effort: str = None,
                           clear: bool = False) -> str:
    """把「AI 设置」的改动镜像进 AI 凭证单一事实源（tasks.db · ai_credentials）。

    参数语义与 `ai_credentials_db.set_credentials` 对齐：
    - `api_key=None` = 库内原密钥不动（对应「留空 / 脱敏回显 = 不改密钥」）；`""` = 清空。
    - `reasoning_effort=None` = 不动；`""` = 清空；其余为已归一化档位。
    - `clear=True` 时忽略其余参数，直接清空该模块（`module=None` 清全部）。

    返回 "" = 成功；否则返回错误描述（供调用方/守卫判读）。**绝不抛异常**。
    """
    try:
        import ai_credentials_db
        if clear:
            ai_credentials_db.clear_credentials(module)
            return ""
        ai_credentials_db.set_credentials(
            module, base_url=base_url or "", model=model or "",
            api_key=api_key, reasoning_effort=reasoning_effort, source="ai_config")
        return ""
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        logger.error(
            "AI 凭证库（tasks.db）镜像失败：模块 %s 的改动只落了 json / 旧加密库，"
            "而任务读侧是 DB 优先 → 会出现「前端配的 ≠ 任务实际用的」漂移"
            "（任务会继续用 DB 里的旧凭证）。请检查 output/tasks.db 是否可写、磁盘是否已满。"
            "原因：%s", module, err)
        return err


# =====================================================================
# 并发保护（审计 S8）
# ---------------------------------------------------------------------
# `save_module` / `clear_module` / `load_config` 的首次迁移都是
# 「load_config → 改内存 → _write_file」的读改写，此前**零锁**。
# 典型症状：快速连续保存「文本分析」和「质检」两个模块，后者用旧快照覆盖前者 ——
# 用户看到「保存成功了但没生效」。另外 `_write_file` 曾用固定的 `.tmp` 名，
# 并发写者会互相截断对方写了一半的临时文件，`os.replace` 发布出**损坏的 JSON**
# （而 `_read_file` 解析失败只告警、返回 {}）→ 表现为「AI 设置全空、密钥丢失」。
# =====================================================================

_CONFIG_LOCK = threading.RLock()


def _locked(fn):
    """把读改写放回同一把可重入锁的临界区（函数级装饰，签名/文档不变）"""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _CONFIG_LOCK:
            return fn(*args, **kwargs)
    return _wrapper

# 三个独立模块的键（顺序即前端展示顺序）
# ⭐ 2026-10-10：原为本地重复定义（3 处同值），统一到 config.AI_MODULES ——
# 改一处即可，不再有「改了这本、漏了那本」。
from config import AI_MODULES as MODULES

MODULE_META = {
    "text": {
        "label": "文本分析模型（= LLM 引擎）",
        "desc": "就是 LLM 引擎：小说转剧本、章节转剧本、提示词分析等所有纯文本任务都用这一份配置",
        "need_vision": False,
        "placeholder_model": "例如 deepseek-chat / qwen2.5:14b",
        "used_by": ["小说转剧本", "章节转剧本", "提示词分析", "剧本生成"],
    },
    "qc": {
        "label": "质检模型",
        "desc": "分镜图片 / 视频的视觉质检（需支持图像输入的多模态模型）",
        "need_vision": True,
        "placeholder_model": "例如 gpt-4o-mini / qwen-vl-max / glm-4v",
        "used_by": ["步骤5 分镜图片质检", "步骤6 视频质检"],
    },
    "chat": {
        "label": "对话总控模型",
        "desc": "AI 对话总控：通过多轮对话确定漫剧风格、题材、画风、角色等创作设定",
        "need_vision": False,
        "placeholder_model": "例如 deepseek-chat / gpt-4o",
        "used_by": ["AI 对话（创作总控）", "创作设定落盘"],
    },
}


# ===================== 基础结构 =====================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_module() -> dict:
    return {"base_url": "", "api_key": "", "model": "",
            "reasoning_effort": "", "updated_at": None,
            "fallbacks": []}


def _empty_config() -> dict:
    return {"version": 1, "modules": {m: _empty_module() for m in MODULES}, "updated_at": None}


def normalize_reasoning_effort(value) -> str:
    """思考档位归一化：合法值原样返回（小写），非法/空值一律返回 ""（=不注入任何档位参数）

    合法值 = low / high / max（思考档位）+ off（显式关闭思考，= disable_thinking）。
    ⚠️ 必须在这里就把非法值挡掉。GLM-5.3 这类模型会把**非法档位静默解析成最高档（最贵）**，
    如果让用户的笔误原样发给服务端，就等于悄悄按 max 档烧 token。
    """
    v = str(value or "").strip().lower()
    if v == REASONING_EFFORT_OFF:
        return REASONING_EFFORT_OFF
    return v if v in REASONING_EFFORT_LEVELS else ""


def _normalize_fallbacks(raw, module: str = "") -> list:
    """备用模型列表归一化（顺序即故障转移顺序）。

    每项 = {base_url, model, reasoning_effort, label, api_key, api_key_masked,
            has_api_key, secret_ns}。
    - api_key 明文只在内存保留（首次迁移/保存时识别）；json 落盘恒为空；
    - get_module 按 secret_ns（ai.<module>.fb.<i>）从加密库补明文；
    - 对外视图一律脱敏（api_key_masked + has_api_key）。
    """
    out = []
    if not isinstance(raw, list):
        return out
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        _pk = str(item.get("api_key") or "").strip()
        entry = {
            "base_url": str(item.get("base_url") or "").strip(),
            "model": str(item.get("model") or "").strip(),
            "reasoning_effort": normalize_reasoning_effort(item.get("reasoning_effort")),
            "label": str(item.get("label") or "").strip() or f"备用{i + 1}",
            "api_key": "",
            "secret_ns": f"ai.{module}.fb.{i}" if module else "",
            "has_api_key": bool(_pk) or bool(item.get("has_api_key")),
            "api_key_masked": mask_key(_pk) if _pk else "",
        }
        if entry["base_url"] and entry["model"]:
            out.append(entry)
    return out


def _normalize_module(raw) -> dict:
    cfg = _empty_module()
    if isinstance(raw, dict):
        for k in ("base_url", "api_key", "model", "updated_at"):
            if raw.get(k) is not None:
                cfg[k] = str(raw[k])
        cfg["reasoning_effort"] = normalize_reasoning_effort(raw.get("reasoning_effort"))
    return cfg


def module_meta() -> dict:
    """模块元信息（前端渲染用，不含任何密钥）"""
    return {m: dict(MODULE_META[m], key=m) for m in MODULES}


# ===================== 读写 / 迁移 =====================

def _read_file(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    # ⚠️ Windows 上 `_atomic_replace` 换目录项的瞬间，另一线程 open() 会抛
    #    PermissionError（暂时拿不到 ≠ 文件坏了）。旧实现直接当读失败 → 返回 {}，
    #    表现为「AI 设置全空 / 密钥丢失」，刷新又回来。这里退避重试。
    err = None
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except PermissionError as e:
            err = e
            time.sleep(0.02 * (i + 1))
        except Exception as e:  # noqa: BLE001
            err = e
            break
    logger.warning(f"AI 设置读取失败（按未配置处理）：{err}")
    return {}


def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试。

    ⚠️ Windows 上若目标文件正被另一个线程/进程打开读取（例如 `load_index` 刚读完、
    句柄尚未释放），`os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这属于「暂时拿不到」而不是「文件坏了」：直接失败会让新建项目/保存设置偶发 500
    （实测并发读写时必现）。退避重试即可。
    """
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:        # WinError 5 / 32：目标被占用
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


def _write_file(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # ⚠️ 临时名必须每次唯一（固定 `.tmp` 会被并发写者互相截断，见文件头并发注释）
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
        raise


def _legacy_migratable(legacy_path: str) -> dict:
    """旧 llm_config.json 若已配置完整，则作为迁移来源"""
    if not legacy_path or not os.path.isfile(legacy_path):
        return {}
    try:
        old = _load_legacy_config(legacy_path) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"旧 llm_config.json 读取失败（跳过迁移）：{e}")
        return {}
    if old.get("base_url") and old.get("api_key") and old.get("model"):
        return old
    return {}


@_locked
def load_config(config_path: str, legacy_path: str = None) -> dict:
    """读取统一 AI 设置；首次读取且 text 模块为空时，自动从旧 llm_config.json 迁移并落盘。

    注意：迁移只做一次（迁移后 ai_config.json 中的 text 模块已有值），不会覆盖用户后续修改。
    """
    raw = _read_file(config_path)
    cfg = _empty_config()
    modules = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
    for m in MODULES:
        _m = _normalize_module(modules.get(m))
        _m["fallbacks"] = _normalize_fallbacks((modules.get(m) or {}).get("fallbacks") if isinstance(modules.get(m), dict) else [], m)
        cfg["modules"][m] = _m
    if raw.get("migrated_from"):
        cfg["migrated_from"] = str(raw["migrated_from"])
    cfg["version"] = int(raw.get("version") or 1)
    cfg["updated_at"] = raw.get("updated_at")

    # P0-3：若 json 中仍残留明文密钥，自动迁移到加密库并清空 json 字段（只做一次）
    if _has_plaintext_key(raw):
        try:
            secret_store.scrub_plaintext_key(config_path, "llm", _ROOT_DIR)
            raw2 = _read_file(config_path)
            modules2 = raw2.get("modules") if isinstance(raw2.get("modules"), dict) else {}
            for m in MODULES:
                cfg["modules"][m] = _normalize_module(modules2.get(m))
            cfg["updated_at"] = raw2.get("updated_at") or cfg["updated_at"]
            logger.info(f"{os.path.basename(config_path)} 中的明文密钥已迁移至加密库")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"明文密钥迁移失败（暂不阻断）：{e}")

    text = cfg["modules"]["text"]
    if not (text.get("base_url") and text.get("model")):
        legacy = _legacy_migratable(legacy_path)
        if legacy:
            text["base_url"] = (legacy.get("base_url") or "").strip()
            text["api_key"] = (legacy.get("api_key") or "").strip()
            text["model"] = (legacy.get("model") or "").strip()
            text["updated_at"] = legacy.get("updated_at") or _now()
            cfg["migrated_from"] = os.path.basename(legacy_path) if legacy_path else "llm_config.json"
            cfg["updated_at"] = _now()
            try:
                _write_file(config_path, cfg)
                logger.info(f"已从 {cfg['migrated_from']} 迁移文本分析模型配置到 {config_path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"AI 设置迁移落盘失败（内存中已生效）：{e}")
    return cfg


def get_module(cfg: dict, module: str) -> dict:
    """取某个模块的配置（含明文 api_key，仅供后端调用使用，切勿直接回传前端）

    密钥取值优先级：环境变量（MJSCXT_API_KEY_TEXT/QC/CHAT）> 本地加密库 > json 明文（遗留）。
    base_url / model 同样支持环境变量覆盖（MJSCXT_BASE_URL_* / MJSCXT_MODEL_*）。
    """
    if module not in MODULES:
        raise ValueError(f"未知的 AI 模块：{module}")
    modules = (cfg or {}).get("modules") or {}
    _raw_m = modules.get(module)
    ep = _normalize_module(_raw_m)
    ep["fallbacks"] = _normalize_fallbacks((_raw_m or {}).get("fallbacks") if isinstance(_raw_m, dict) else [], module)
    ns = f"ai.{module}"
    # ⭐ 单一事实源：DB（ai_credentials 表）优先。get_credentials 内部已按
    # env > DB 解析，直接信任其结果；DB 有非空字段就覆盖 json 值。
    db_key = ""
    try:
        import ai_credentials_db
        db_ep = ai_credentials_db.get_credentials(module)
        if db_ep.get("base_url"):
            ep["base_url"] = db_ep["base_url"]
        if db_ep.get("model"):
            ep["model"] = db_ep["model"]
        if db_ep.get("api_key"):
            db_key = db_ep["api_key"]
            ep["api_key"] = db_key
        if db_ep.get("reasoning_effort"):
            ep["reasoning_effort"] = db_ep["reasoning_effort"]
    except Exception as e:  # noqa: BLE001  DB 不可用回落旧口径（json + 加密库槽 + env）
        logger.warning(f"AI 凭证 DB 读取失败，回落加密库/env：{e}")
    # 备用模型密钥：从加密库槽 ai.<module>.fb.<i> 补明文（与主模块同口径）
    for _i, _fb in enumerate(ep.get("fallbacks") or []):
        if _fb.get("secret_ns"):
            _fb_key = _store().get_api_key(_fb["secret_ns"])
            if _fb_key:
                _fb["api_key"] = _fb_key
    if db_key:
        return ep
    # ⚠️「DB 没有该模块的密钥」≠「没配过密钥」：本侧密钥写入点（save_module）落的是
    #    旧加密库槽 `ai.<module>`，DB 只在「启动一次性迁移 / UI 显式写库」时才有行。
    #    原实现在 DB 返回「无该模块行」时同样直接 return → 旧槽里的密钥永远读不到
    #    （DB 无行 + 加密库有钥）→ 该模块被判「未配置」（chat 总控 / qc 质检实测命中）。
    #    现改为：DB 未给出密钥就继续走旧口径回落（env > 加密库槽 > json 明文）。
    # 回落：非密钥字段环境变量优先
    env_base = secret_store.SecretStore.env_base_url(ns)
    env_model = secret_store.SecretStore.env_model(ns)
    if env_base:
        ep["base_url"] = env_base
    if env_model:
        ep["model"] = env_model
    # 密钥：环境变量 > 加密库 > json 明文
    secure = _store().get_api_key(ns)
    if secure:
        ep["api_key"] = secure
    return ep


@_locked
def save_module(config_path: str, module: str, base_url: str = None, model: str = None,
                api_key: str = None, legacy_path: str = None,
                reasoning_effort: str = None, fallbacks: list = None) -> dict:
    """保存单个模块。

    密钥处理（P0-3 加固）：
    - api_key 为 None / 空 / 含 * 的脱敏值时，视为「不改动原密钥」；
    - 有效新密钥写入**加密库**（output/secrets.enc），json 中该字段恒为空字符串；
    - 并**镜像到凭证单一事实源 tasks.db**（任务实际读的就是它，见
      `_mirror_credentials_db`）—— 镜像内置于本函数，任何调用方都自动闭合，
      不再依赖各路由自己补一次；
    - 加密不可用时（未装 cryptography），拒绝保存明文并抛 ValueError，
      提示改用环境变量 MJSCXT_API_KEY_TEXT/QC/CHAT。

    reasoning_effort（思考档位，思考不可关闭的模型如 GLM-5.3-Flash 用）：
    - None = 不改动；"" = 清空（回到「不注入档位、由服务端默认」）；low/high/max = 设定档位。
    - 非法值**不报错但会被归一化成 ""**，避免把笔误原样发给服务端（部分模型会把非法值
      静默解析成最高档，即最贵的那档）。
    """
    if module not in MODULES:
        raise ValueError(f"未知的 AI 模块：{module}")
    cfg = load_config(config_path, legacy_path)
    ep = cfg["modules"][module]
    if base_url is not None:
        ep["base_url"] = (base_url or "").strip()
    if model is not None:
        ep["model"] = (model or "").strip()
    if reasoning_effort is not None:
        ep["reasoning_effort"] = normalize_reasoning_effort(reasoning_effort)
    key = "" if api_key is None else str(api_key).strip()
    # 备用模型：密钥逐个落加密库槽 ai.<module>.fb.<i>，json 里只留脱敏值
    if fallbacks is not None:
        _norm_fb = []
        # 旧条目：按 (base_url, model) 索引 —— 用户「编辑」时前端回显的是脱敏值
        # （含 *），必须能从旧条目把密钥续上；按签名而不是按位置匹配，
        # 这样删掉中间一条、或调整顺序也不会把 A 的密钥接到 B 头上。
        _old_fbs = list(ep.get("fallbacks") or [])
        _old_by_sig = {}
        for _o in _old_fbs:
            if isinstance(_o, dict):
                _old_by_sig[(str(_o.get("base_url") or "").strip(),
                             str(_o.get("model") or "").strip())] = _o
        for _i, _fb in enumerate(fallbacks or []):
            if not isinstance(_fb, dict):
                continue
            _fk = str(_fb.get("api_key") or "").strip()
            _masked_echo = "*" in _fk          # 脱敏回显 = 用户没改密钥
            _entry = {
                "base_url": str(_fb.get("base_url") or "").strip(),
                "model": str(_fb.get("model") or "").strip(),
                "reasoning_effort": normalize_reasoning_effort(_fb.get("reasoning_effort")),
                "label": str(_fb.get("label") or "").strip() or f"备用{_i + 1}",
                "api_key": "",
                "has_api_key": bool(_fk) or bool(_fb.get("has_api_key")),
                "api_key_masked": "" if _masked_echo else (mask_key(_fk) if _fk else ""),
            }
            if _entry["base_url"] and _entry["model"]:
                if _fk and not _masked_echo:
                    # 用户填了新的明文密钥 → 写加密库槽 + 刷新脱敏值
                    if not _store().set_api_key(f"ai.{module}.fb.{_i}", _fk):
                        logger.warning("备用模型密钥加密不可用（索引 %d），跳过该条", _i)
                    else:
                        _entry["has_api_key"] = True
                        _entry["api_key_masked"] = mask_key(_fk)
                else:
                    # 脱敏回显 / 留空 → **不改动密钥**：从旧条目（按签名）或原索引槽续上，
                    # 并按新索引把槽位重写一遍，保证「顺序变了槽位也跟着对」。
                    _prev = _old_by_sig.get((_entry["base_url"], _entry["model"])) or {}
                    _prev_key = str(_prev.get("api_key") or "").strip()
                    if not _prev_key and module:
                        try:
                            _prev_key = str(_store().get_api_key(f"ai.{module}.fb.{_i}") or "").strip()
                        except Exception:  # noqa: BLE001
                            _prev_key = ""
                    if _prev_key and module:
                        try:
                            if _store().set_api_key(f"ai.{module}.fb.{_i}", _prev_key):
                                _entry["has_api_key"] = True
                                _entry["api_key_masked"] = mask_key(_prev_key)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("备用模型密钥续写失败（索引 %d）：%s", _i, e)
                    else:
                        _entry["has_api_key"] = bool(_prev.get("has_api_key"))
                        _entry["api_key_masked"] = str(_prev.get("api_key_masked") or "")
                _norm_fb.append(_entry)
        ep["fallbacks"] = _norm_fb
    if key and "*" not in key:
        if not _store().set_api_key(f"ai.{module}", key):
            raise ValueError(
                "密钥加密存储不可用（缺少 cryptography 或主密钥），已拒绝明文落盘。"
                "请安装 cryptography 后重试，或改用环境变量 "
                f"{secret_store.ENV_KEY_MAP.get(f'ai.{module}')} 配置密钥。")
    # json 中恒不保存明文密钥
    ep["api_key"] = ""
    ep["updated_at"] = _now()
    cfg["updated_at"] = _now()
    _write_file(config_path, cfg)
    # ⭐ 凭证单一事实源闭合：DB（tasks.db）才是任务实际读的那份（读侧 DB 优先）。
    #    只写 json / 加密库 = 「前端配了但任务不用」，故镜像下沉到本函数（见上方长注释）。
    #    keep 语义：留空或含 "*" 的脱敏回显 → 传 None，DB 内原密钥不动。
    #    档位只在本次**显式传参**时镜像（None = 不改动），避免把「未提交档位」误当「清空档位」。
    _mirror_credentials_db(
        module, base_url=ep["base_url"], model=ep["model"],
        api_key=None if (not key or "*" in key) else key,
        reasoning_effort=(ep.get("reasoning_effort") if reasoning_effort is not None else None))
    return cfg


def _has_plaintext_key(raw: dict) -> bool:
    """判断配置里是否残留明文密钥（用于触发一次性迁移）"""
    if not isinstance(raw, dict):
        return False
    if isinstance(raw.get("api_key"), str) and raw["api_key"].strip():
        return True
    modules = raw.get("modules")
    if isinstance(modules, dict):
        for cfg in modules.values():
            if isinstance(cfg, dict) and isinstance(cfg.get("api_key"), str) and cfg["api_key"].strip():
                return True
    return False


@_locked
def clear_module(config_path: str, module: str = None, legacy_path: str = None) -> dict:
    """清空单个模块；module 为空则清空全部三个模块（整体重置）。

    同步清除三处：json（`api_key` 恒空）+ 旧加密库槽 `ai.<module>` + **凭证单一事实源
    tasks.db 的 ai_credentials 行**（后者才是任务实际读的那份，见 `_mirror_credentials_db`）。
    """
    cfg = load_config(config_path, legacy_path)
    if module:
        if module not in MODULES:
            raise ValueError(f"未知的 AI 模块：{module}")
        cfg["modules"][module] = _empty_module()
        _store().clear_api_key(f"ai.{module}")
        # 备用模型密钥槽（ai.<module>.fb.<i>）一并清（加密库是扁平的，
        # 没有前缀扫描 API，按已知上限 10 个逐条清）
        for _i in range(10):
            _store().clear_api_key(f"ai.{module}.fb.{_i}")
    else:
        cfg = _empty_config()
        for m in MODULES:
            _store().clear_api_key(f"ai.{m}")
    cfg["updated_at"] = _now()
    _write_file(config_path, cfg)
    # ⭐ 与保存对称：DB 是任务实际读的那份，只清 json / 加密库 = 「AI 设置显示已清空、
    #    任务却仍拿 DB 旧凭证跑」。清空同样下沉到本函数，任何调用方都自动闭合
    #    （`/api/llm/config/clear` 此前就漏了这一步）。module=None → 清全部三模块。
    _mirror_credentials_db(module, clear=True)
    return cfg


# ===================== 对外视图 =====================

def _fallbacks_public_view(fallbacks) -> list:
    """备用模型的对外脱敏视图（base_url / model / label / has_api_key / api_key_masked）"""
    out = []
    for i, fb in enumerate(fallbacks or []):
        if not isinstance(fb, dict):
            continue
        out.append({
            "base_url": fb.get("base_url") or "",
            "model": fb.get("model") or "",
            "reasoning_effort": normalize_reasoning_effort(fb.get("reasoning_effort")),
            "label": fb.get("label") or f"备用{i + 1}",
            "has_api_key": bool(fb.get("has_api_key")) or bool(fb.get("api_key")),
            "api_key_masked": fb.get("api_key_masked") or mask_key(fb.get("api_key") or ""),
            "index": i,
        })
    return out


def module_public_view(ep: dict) -> dict:
    """单个模块的对外视图（绝不含 api_key 明文）"""
    key = (ep or {}).get("api_key") or ""
    base_url = (ep or {}).get("base_url") or ""
    model = (ep or {}).get("model") or ""
    return {
        "configured": bool(base_url and key and model),
        "ready": bool(base_url and key and model),
        "has_api_key": bool(key),
        "base_url": base_url,
        "model": model,
        "reasoning_effort": normalize_reasoning_effort((ep or {}).get("reasoning_effort")),
        "api_key_masked": mask_key(key),
        "chat_url": build_chat_url(base_url) if base_url else "",
        "updated_at": (ep or {}).get("updated_at"),
        # 备用模型（脱敏视图：api_key 永不回显明文）
        "fallbacks": _fallbacks_public_view(ep.get("fallbacks")),
    }


def public_view(cfg: dict) -> dict:
    """整体视图：三个模块各自的脱敏配置 + 元信息"""
    modules = {}
    for m in MODULES:
        view = module_public_view(get_module(cfg, m))
        view.update({"key": m, "label": MODULE_META[m]["label"], "desc": MODULE_META[m]["desc"],
                     "need_vision": MODULE_META[m]["need_vision"],
                     "placeholder_model": MODULE_META[m]["placeholder_model"],
                     "used_by": MODULE_META[m]["used_by"]})
        modules[m] = view
    return {
        "modules": modules,
        "module_order": list(MODULES),
        "migrated_from": (cfg or {}).get("migrated_from"),
        "updated_at": (cfg or {}).get("updated_at"),
        # 思考档位可选项（前端下拉）："" = 不注入（服务端默认）、off = 关闭思考、其余为档位
        "reasoning_effort_options": ["", REASONING_EFFORT_OFF, *REASONING_EFFORT_LEVELS],
    }
