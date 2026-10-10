"""
自定义 LLM API 客户端（OpenAI 兼容 chat/completions 协议）

职责：
- 配置持久化：base_url / api_key / model 写入项目根目录 llm_config.json
- 对外只暴露脱敏后的 api_key（明文永不回显）
- 统一 chat 调用、JSON 输出解析、连接测试
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

import cancellation
from fs_atomic import atomic_write_json

from common_util import mask_key as mask_key
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 240
# 服务端瞬时不可用（限流/过载/网关错误）：同一请求内可安全重试
HTTP_TRANSIENT_CODES = (429, 500, 502, 503, 504)
HTTP_RETRY_MAX = 3            # 单次请求最大尝试次数（含首次）
HTTP_RETRY_BACKOFF = (3, 8)   # 每次重试前的等待秒数

# ---- 网关级快速失败 / 熔断（2026-09-17 新增）----
# 背景（实测）：当上游整体没算力时，请求不会快速报错，而是**挂到超时**。
# 按原逻辑重试 3 次 × 每次 240~300s → 单块分镜 672 秒才降级；一集 6 块 ≈ 1 小时纯等待，
# 最后产出一份「无分镜无台词、配音 0 句」的兜底剧本 —— 比直接报错更糟。
# 上游没算力，等 3 秒重试是不会变好的，所以分两层拦：
#   ① 单次调用内：识别到「上游无算力」就直接放弃，不把 3 次超时都打完；
#   ② 跨调用：同一网关连续失败达阈值 → 熔断 cooldown 秒，期间直接抛错、不打网络。
# ⚠️ 本地/内网地址（ollama 等）不适用 ①：本地模型冷启动瞬间 503 是真的会自愈，仍按原逻辑重试。
GATEWAY_UNAVAILABLE_CODES = (500, 502, 503, 504)
UPSTREAM_UNAVAILABLE_MARKERS = (
    "no available workers", "all circuits open", "upstream_error",
    "no available channel", "service unavailable",
)
GATEWAY_FAILFAST_THRESHOLD = 2      # 连续 N 次「上游不可用」→ 熔断
GATEWAY_CIRCUIT_COOLDOWN_SEC = 600  # 熔断冷却时长；期间直接快速失败
GATEWAY_UNAVAILABLE_MAX_ATTEMPTS = 1  # 判定为「上游不可用」后允许的尝试次数（远端=1，不重试）
# ---- 远端**读超时**的独立重试额度（2026-09-25 新增，与上面的 fail-fast 区分开）----
# 背景（实测）：远端网关的偶发挂起表现为「连得上、但响应读到超时」，此时上游往往是好的，
# 一次就放弃会让整集白烧（第 1 集实测即此坑：仅 1 次 ReadTimeout 就整集中止）。
# 但也不能无限等 —— 单次超时本身已是 LLM_REQUEST_TIMEOUT(1200s)=20 分钟，
# 故最多额外再给 1 次，总等待上界 2×20 分钟，不至于像旧逻辑那样 3 次打满。
TIMEOUT_MAX_ATTEMPTS = 2

# 网络层异常：requests 的封装类型 + 标准库 socket 层。
# ⚠️ 只写 requests.exceptions.Timeout / ConnectionError 是不够的：`socket.timeout` 在
# Python 3.10+ 就是内置 `TimeoutError`（OSError 子类），某些传输/中间件路径会原样抛出来，
# 那种情况此前会掉进「请求异常」分支 → 不计入网关不可用，等于白等一轮超时。
NETWORK_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    TimeoutError,   # == socket.timeout
    OSError,        # ConnectionResetError / ConnectionRefusedError 等
)

# 同一网关的连续失败计数：{origin: {"fails": int, "opened_at": float, "detail": str}}
_GATEWAY_STATE: dict = {}

JSON_RETRY_HINT = "上一次输出不是合法 JSON。请只输出一个合法 JSON 对象，不要包含 markdown 代码块、注释或任何解释文字。"
TRUNCATION_RETRY_HINT = (
    "上一次输出因长度上限被截断（finish_reason=length），JSON 未闭合。"
    "请务必精简描述、严格压缩每个字段的文本长度，只输出结构完整且闭合的 JSON 对象，不要复述指令、不要输出思考过程。"
)
# 截断时的 max_tokens 提升阶梯（按顺序尝试，取第一个大于当前值的档位）
DEFAULT_TOKEN_LADDER = (2048, 4096, 8192, 16384, 24576)

# 是否默认关闭「思考模式」。
# ⚠️ 2026-09-17 起改为 **False（默认允许思考）**：用户明确要求「所有配置的模型都允许思考」。
# 关思考虽然能规避「思考吃掉 token → 正文为空」，但会让质检/剧本单位模型退化成不走推理的
# 直觉判断，漏掉明显问题（用户反馈「图片有明显不合理的地方却通过了」）。
# 现在的策略是：**允许思考，但用「token 下限 + 截断重试」来兜底**，而不是阉割模型。
# 仍可用配置项 disable_thinking 对单个模块关掉。
DISABLE_THINKING_DEFAULT = False
# 允许思考时的最小 max_tokens：思考本身就要吃掉几百 token，额度太小必然空正文
MIN_TOKENS_WHEN_THINKING = 1024
MAX_TOKENS_CEILING = 32768

# ---- 思考「档位」模型（GLM-5.3 之类思考不可关闭的模型） ----
# GLM-5.3-Flash 是 always-on reasoning：没有 enable_thinking 开关，
# 只有 reasoning_effort = low / high / max（默认 max），官方建议 max_tokens ≥ 2048。
# 所以「关思考」这条路对它根本不适用，必须换成一个额度更足的下限。
# 另注：该参数各家网关封装位置不同（顶层 / chat_template_kwargs），
# 这里按 vLLM Recipe 与 Qubrid 文档采用 chat_template_kwargs 形式，必要时用
# REASONING_EFFORT_STYLE 切换。
REASONING_EFFORT_LEVELS = ("low", "high", "max")
# 「关闭思考」的档位值：语义 = disable_thinking=True（注入 chat_template_kwargs.enable_thinking=false）。
# 这是标准 OpenAI 兼容协议里真正「关思考」的参数，对 Qwen/vLLM 系有效；对 always-on 模型
# （agnes/GLM 系）网关会忽略它，但**不会**像非法 reasoning_effort 那样被静默解析成 max 档烧 token
# （不注入 reasoning_effort，走 disable_thinking 分支）。故单独作为档位暴露给前端。
REASONING_EFFORT_OFF = "off"
MIN_TOKENS_WHEN_REASONING_EFFORT = 2048
# ⚠️ 「只吐思考」场景的额度下限：思考本身就要吃掉几千甚至上万 token，下限太小等于必空正文。
# 实测（2026-09-23 端到端复测，agnes-3.0-flash / low 档 / 5614 字符分镜 prompt）：
#   max_tokens  4800 → 只有思考，正文为空
#   max_tokens  8192 → 只有思考，正文为空
#   max_tokens 16384 → 只有思考，正文为空
#   max_tokens 24576 → ✅ finish=stop，思考 15793 字符、正文 14291 字符
#   max_tokens 32768 → ✅ finish=stop，正文 7917 字符（思考未复现，走缓存）
# 即该任务思考水位 ≈16K token。故「只吐思考」自救必须**一步跨到 ≥16K**，
# 而不是按 2K 阶梯小步试探（每步要耗 2-7 分钟，等于把用户时间烧光）。
# ⚠️ 2026-10-10 修正为 24576：上方实测表写得清清楚楚 —— 16384 仍然「只有思考、正文为空」，
#    24576 才是首个成功档（思考 15793 / 正文 14291）。原先把它设成 16384，导致
#    「只吐思考」自救时抬到这个值就**原地不动**，随后被下游误判成「模型/服务端异常」
#    并快速失败（用户实测：日志反复出现「max_tokens=19384 已达上限，停止无意义提额」）。
#    用户明确要求「模型在正常思考，要无视思考内容、不要反复重试」——
#    所以水位必须一步到位给足，而不是靠一轮轮失败去试探。
REASONING_ONLY_TOKEN_FLOOR = 24576
# "chat_template_kwargs"（默认，zai/vLLM 风格）| "top_level"（部分网关）
REASONING_EFFORT_STYLE = "chat_template_kwargs"
# 思考档位降级顺序（max → high → low）。
# 用于「模型只吐思考内容」时的自救：档位越高思考越烧 token，先把档位往下压，
# 配合提高 max_tokens 才能突破「思考吃光额度 → 正文永远为空」的死循环。
# ⚠️ 只降到 low 就停，不要降到「不注入」：对 always-on reasoning 模型（GLM/agnes 系）
# 不注入档位会被网关按默认（通常是最高档）解析，反而更烧 token；low 才是最短思考档。
REASONING_EFFORT_DOWNGRADE_ORDER = ("max", "high", "low")


class LLMError(Exception):
    """LLM 调用失败（面向用户的友好错误）"""


class LLMReasoningOnlyError(LLMError):
    """模型只吐了思考内容（reasoning_content），正文为空。

    允许思考后这是可恢复的：思考吃光了 max_tokens，加额度重来即可，
    不该当成致命错误直接失败。
    """


class LLMGatewayUnavailable(LLMError):
    """网关整体不可用（上游没算力 / 连续超时 / 熔断中）。

    刻意做成 LLMError 的子类：现有 `except LLMError` 的地方不用改就能兜住；
    但调用方**应该**单独识别它 —— 这类错误重试无用，正确处置是
    「立刻停下、把网关问题告诉用户」，而不是逐块降级成原文兜底。
    """

    def __init__(self, message: str, base_url: str = "", streak: int = 0,
                 detail: str = "", cooling: bool = False):
        super().__init__(message)
        self.base_url = base_url
        self.streak = int(streak or 0)
        self.detail = detail or ""
        self.cooling = bool(cooling)

    @property
    def hint(self) -> str:
        if self.cooling:
            return (f"该网关（{self.base_url}）刚被判定为不可用，已暂停请求 "
                    f"{GATEWAY_CIRCUIT_COOLDOWN_SEC} 秒以免空等。"
                    "请到「AI 设置」换一个可用的 base_url / 模型，或稍后重试。")
        return ("网关连续不可用（多为上游没有可用算力/配额耗尽）。"
                "重试无用，请到「AI 设置」更换 base_url 或模型后重试。"
                "可用 AI 设置里的「测试连接」先确认网关是否活着。")


def _gateway_origin(url: str) -> str:
    """熔断按「网关」粒度而非完整 URL：同网关下换模型/换路径同样受保护"""
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}" if p.netloc else (url or "")


def _is_upstream_unavailable(status: int, body: str) -> bool:
    """响应体里出现「上游没算力」的明确标记 → 重试也不会变好"""
    if status not in GATEWAY_UNAVAILABLE_CODES:
        return False
    low = (body or "").lower()
    return any(m in low for m in UPSTREAM_UNAVAILABLE_MARKERS)


def _is_timeout_error(e) -> bool:
    """区分「读超时」与「连不上」。

    - 超时（Timeout / socket.timeout）：连接已建立、上游在响应，只是慢或偶发挂起 ——
      **值得再试一次**，一次就放弃会白烧整集（第 1 集实测坑）。
    - 其他（ConnectionError / DNS / 拒绝连接 / ChunkedEncodingError）：链路本身不通，
      重试无用 → 保持 fail-fast。

    ⚠️ 必须先判 Timeout 再判 OSError：Python 3.10+ 的 `socket.timeout` 就是内置
    `TimeoutError`（OSError 子类），`requests.exceptions.Timeout` 也继承自 OSError，
    顺序反了会把超时误判成「连不上」而退回旧的 fail-fast 行为。
    """
    if isinstance(e, (requests.exceptions.Timeout, TimeoutError)):
        return True
    name = type(e).__name__
    return name in ("Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError")


def gateway_circuit_state(base_url: str) -> dict:
    """查询某网关的熔断状态（供探针/接口展示，不触发任何请求）"""
    st = _GATEWAY_STATE.get(_gateway_origin(base_url)) or {}
    opened_at = float(st.get("opened_at") or 0)
    remain = max(0, int(opened_at + GATEWAY_CIRCUIT_COOLDOWN_SEC - time.time()))
    return {"open": remain > 0, "cooldown_remain_sec": remain,
            "fails": int(st.get("fails") or 0), "detail": st.get("detail") or ""}


def _note_gateway_failure(url: str, detail: str) -> int:
    """记一次网关级失败，返回当前连续失败次数"""
    key = _gateway_origin(url)
    st = _GATEWAY_STATE.setdefault(key, {"fails": 0, "opened_at": 0.0, "detail": ""})
    st["fails"] = int(st.get("fails") or 0) + 1
    st["detail"] = (detail or "")[:200]
    if st["fails"] >= GATEWAY_FAILFAST_THRESHOLD:
        st["opened_at"] = time.time()
        logger.error(f"网关 {key} 连续 {st['fails']} 次不可用 → 熔断 "
                     f"{GATEWAY_CIRCUIT_COOLDOWN_SEC}s（期间不再发起请求）")
    return st["fails"]


def _clear_gateway_failure(url: str) -> None:
    key = _gateway_origin(url)
    st = _GATEWAY_STATE.get(key)
    if st and (st.get("fails") or st.get("opened_at")):
        logger.info(f"网关 {key} 已恢复，熔断计数清零")
    _GATEWAY_STATE.pop(key, None)


def reset_gateway_circuits() -> None:
    """手动清空全部熔断状态（供「测试连接」/诊断使用：用户换完网关要能立刻重试）"""
    _GATEWAY_STATE.clear()


class LLMTruncatedError(LLMError):
    """连续重试后仍因输出截断（finish_reason=length）而无法得到完整 JSON"""

    def __init__(self, message: str, detail: dict = None):
        super().__init__(message)
        self.detail = detail or {}
        self.truncated = True


# ===================== 配置持久化 =====================

# 数据根目录（定位加密密钥库 output/secrets.enc 与主密钥 .secret_key）。
# 走 env_loader.PROJECT_DATA_DIR（MJSCXT_DATA_DIR 守卫，默认=源根，行为不变）。
try:
    from env_loader import PROJECT_DATA_DIR as _PROJECT_ROOT
except Exception:  # noqa: BLE001  兜底：env_loader 未就绪时退回源根
    _PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _empty_config() -> dict:
    return {"base_url": "", "api_key": "", "model": "", "updated_at": None}


def load_config(config_path: str) -> dict:
    cfg = _empty_config()
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            for k in ("base_url", "api_key", "model", "updated_at"):
                if data.get(k) is not None:
                    cfg[k] = str(data[k])
            # P0-3：明文密钥自动迁移到加密库并清空 json 字段（只做一次）
            if data.get("api_key"):
                try:
                    import secret_store
                    if secret_store.get_store(_PROJECT_ROOT).set_api_key("llm", str(data["api_key"]).strip()):
                        data["api_key"] = ""
                        atomic_write_json(config_path, data)
                        cfg["api_key"] = ""
                        logger.info("LLM 配置中的明文密钥已迁移至加密库")
                except Exception as e:  # noqa: BLE001
                    # N3（2026-09-22 复验）：迁移失败意味着**明文密钥仍留在配置文件里**
                    # （安全相关），故升为 error —— 不阻断流程，但生产 root=WARNING 必须可见。
                    logger.error(f"LLM 密钥迁移失败（明文密钥仍留在配置文件，暂不阻断）：{e}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM 配置读取失败（按未配置处理）：{e}")
    # 密钥取值：环境变量 > 加密库 > json（迁移后应为空）
    try:
        import secret_store
        secure = secret_store.get_store(_PROJECT_ROOT).get_api_key("llm")
        if secure:
            cfg["api_key"] = secure
        env_base = secret_store.SecretStore.env_base_url("llm")
        env_model = secret_store.SecretStore.env_model("llm")
        if env_base:
            cfg["base_url"] = env_base
        if env_model:
            cfg["model"] = env_model
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM 密钥读取异常（回退 json）：{e}")
    # ⭐ 2026-10-10（P0 修复）：叠加统一配置中心的值（同 qc_client.load_config 的理由）。
    #   overlay_config 只覆盖已被显式改过（来源 db/env）的项，未动的键保持 JSON 原值。
    try:
        import config_center as _cc
        cfg = _cc.overlay_config(cfg)
    except Exception as _e:  # noqa: BLE001
        logger.warning(f"配置中心叠加失败（沿用 LLM 自身配置）：{_e}")
    return cfg


def save_config(config_path: str, base_url: str, api_key: str = None,
                model: str = None, keep_key_if_blank: bool = True) -> dict:
    """保存配置。P0-3：有效密钥写入加密库，json 中不落明文。"""
    cfg = load_config(config_path)
    if base_url is not None:
        cfg["base_url"] = (base_url or "").strip()
    if model is not None:
        cfg["model"] = (model or "").strip()
    if api_key is not None and api_key.strip():
        key = api_key.strip()
        if "*" not in key:
            try:
                import secret_store
                if not secret_store.get_store(_PROJECT_ROOT).set_api_key("llm", key):
                    raise RuntimeError("密钥加密存储不可用")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"LLM 密钥加密保存失败：{e}")
                raise ValueError(
                    "LLM 密钥加密存储不可用（缺少 cryptography 或主密钥），已拒绝明文落盘。"
                    "请安装 cryptography 后重试，或改用环境变量 MJSCXT_API_KEY 配置密钥。")
    elif api_key is not None and not keep_key_if_blank:
        try:
            import secret_store
            secret_store.get_store(_PROJECT_ROOT).clear_api_key("llm")
        except Exception as e:  # noqa: BLE001
            logger.error("清理加密库中的 LLM 密钥失败，加密库可能残留旧密钥（配置与密钥库不一致）：%s", e)
    cfg["api_key"] = ""
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    atomic_write_json(config_path, cfg)
    return cfg


def clear_config(config_path: str) -> dict:
    cfg = _empty_config()
    try:
        import secret_store
        secret_store.get_store(_PROJECT_ROOT).clear_api_key("llm")
    except Exception as e:  # noqa: BLE001
        logger.error("清理加密库中的 LLM 密钥失败（clear_config），加密库可能残留旧密钥：%s", e)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def public_view(cfg: dict) -> dict:
    """对外（前端）可见的配置视图，绝不包含 api_key 明文"""
    key = cfg.get("api_key") or ""
    base_url = cfg.get("base_url") or ""
    model = cfg.get("model") or ""
    return {
        "configured": bool(base_url and key and model),
        "has_api_key": bool(key),
        "base_url": base_url,
        "model": model,
        "api_key_masked": mask_key(key),
        "chat_url": build_chat_url(base_url) if base_url else "",
        "updated_at": cfg.get("updated_at"),
    }


# ===================== URL 规范化 =====================

def thinking_token_floor(max_tokens: int, reasoning_effort: str = "") -> int:
    """把 max_tokens 抬到「思考水位」之上（只抬不降）。

    各调用点按「正文长度」估额度（1600~7500），但 always-on reasoning 模型
    （agnes-3.0-flash / GLM 系）还要额外消耗上万 token 的思考。额度低于思考量时
    正文恒为空（表现为「模型只吐思考内容」并把整步卡死）。
    统一在这里兜住，避免逐调用点漏改。

    只在「配置了思考档位」时抬 —— 未配置档位的普通模型不需要这个水位，
    抬高只会浪费配额（它们的空正文另有原因，走原重试语义）。
    """
    try:
        mt = int(max_tokens or 0)
    except (TypeError, ValueError):
        return max_tokens
    re_ = str(reasoning_effort or "").strip().lower()
    # ⚠️ 2026-10-10（用户：「模型在正常思考，要无视思考内容不要反复重试」）：
    #    原实现在「未配置档位」时直接返回原值，理由是「普通模型不需要这个水位」。
    #    但本项目实测的模型（agnes / GLM 系）是 always-on reasoning：**不注入档位**
    #    恰恰是最常见的状态（日志里写「最低思考档（未注入）」），结果起始额度给不够，
    #    必然先空正文、再走一轮 2-7 分钟的失败重试才能补上——用户明确反对这种消耗。
    #    现改为：**未配置档位时同样抬到思考水位**（一步到位，避免「先失败再补」）。
    #    代价是普通非思考模型会多占一些配额上限；但上限只是天花板（模型该停就停），
    #    而反复失败重试消耗的是真实时间，两害相权取其轻。
    #    可用环境变量 MJSCXT_THINKING_FLOOR_ALWAYS=0 恢复旧行为（仅配置档位时抬）。
    if re_ and re_ not in REASONING_EFFORT_DOWNGRADE_ORDER:
        return mt   # 档位值非法（如拼写错误）→ 不抬，交给下游报错，避免掩盖配置问题
    if not re_ and str(os.environ.get("MJSCXT_THINKING_FLOOR_ALWAYS", "1")).strip().lower() \
            in ("0", "false", "off", "no"):
        return mt
    if mt >= REASONING_ONLY_TOKEN_FLOOR:
        return mt
    return min(REASONING_ONLY_TOKEN_FLOOR, MAX_TOKENS_CEILING)


def build_chat_url(base_url: str) -> str:
    """把用户填写的 base_url 规范化为 chat/completions 完整地址

    - https://api.deepseek.com                     → https://api.deepseek.com/v1/chat/completions
    - https://api.openai.com/v1                    → https://api.openai.com/v1/chat/completions
    - http://127.0.0.1:8899/v1                     → http://127.0.0.1:8899/v1/chat/completions
    - .../v1/chat/completions（已是完整地址）        → 原样返回
    """
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    path = urlparse(u).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return u
    if path in ("", "/"):
        return f"{u}/v1/chat/completions"
    return f"{u}/chat/completions"


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or host.endswith(".local")


# ===================== 客户端 =====================

class LLMClient:
    """OpenAI 兼容协议客户端（base_url + api_key + model 由用户在页面配置）"""

    def __init__(self, config_path: str, config: dict = None, timeout: int = DEFAULT_TIMEOUT):
        self.config_path = config_path
        self.config = config or load_config(config_path)
        self.timeout = timeout
        self.base_url = self.config.get("base_url") or ""
        self.api_key = self.config.get("api_key") or ""
        self.model = self.config.get("model") or ""
        self.chat_url = build_chat_url(self.base_url)
        self.last_error = ""
        # 思考模式开关（默认关闭，可由配置项 disable_thinking 覆盖）
        self.disable_thinking = bool(self.config.get("disable_thinking", DISABLE_THINKING_DEFAULT))
        # 思考档位（思考不可关闭的模型用，如 GLM-5.3）：low / high / max，留空=不注入；
        # off = 显式「关闭思考」（= disable_thinking=True，走 enable_thinking=false 分支）。
        _re = str(self.config.get("reasoning_effort") or "").strip().lower()
        if _re == REASONING_EFFORT_OFF:
            self.reasoning_effort = ""
            self.disable_thinking = True
        else:
            self.reasoning_effort = _re if _re in REASONING_EFFORT_LEVELS else ""
        if _re and not self.reasoning_effort and _re != REASONING_EFFORT_OFF:
            logger.warning(
                f"reasoning_effort={_re!r} 不是合法档位（仅 {'/'.join(REASONING_EFFORT_LEVELS)}，"
                f"或 {REASONING_EFFORT_OFF!r}=关闭思考），"
                f"已忽略。注意：部分模型会把非法值静默解析成最高档（最贵），务必用合法值。")
        # 最近一次 chat_json_robust 的诊断信息（attempts / finish_reason / max_tokens / truncated）
        self.last_json_meta = {}

    # ---- 状态 ----
    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @staticmethod
    def gateway_circuit_state(base_url: str) -> dict:
        """某网关的熔断状态（类方法入口，方便调用方统一从 LLMClient 取）"""
        return gateway_circuit_state(base_url)

    @staticmethod
    def reset_gateway_circuits() -> None:
        """清空熔断计数（用户改完 AI 设置点「测试连接」时必须先清，否则会被旧熔断直接拦掉）"""
        reset_gateway_circuits()

    def require_configured(self):
        if not self.configured:
            missing = []
            if not self.base_url:
                missing.append("base_url")
            if not self.api_key:
                missing.append("api_key")
            if not self.model:
                missing.append("model")
            raise LLMError(
                "尚未配置自定义 AI 接口（缺少：" + "、".join(missing) +
                "）。请点击右上角「AI 接口配置」填写 base_url / api_key / model 后再试。"
            )

    # ---- 底层调用 ----
    def _post(self, payload: dict, timeout: int = None, chat_url: str = None) -> dict:
        url = chat_url or self.chat_url
        local = _is_local(url)

        # ① 熔断中：直接快速失败，不打网络（否则又是几十秒起步的空等）
        state = gateway_circuit_state(url)
        if state["open"]:
            raise LLMGatewayUnavailable(
                f"网关 {_gateway_origin(url)} 处于熔断冷却中"
                f"（剩余 {state['cooldown_remain_sec']}s，连续失败 {state['fails']} 次）："
                f"{state['detail'] or '上游不可用'}",
                base_url=_gateway_origin(url), streak=state["fails"],
                detail=state["detail"], cooling=True)

        session = requests.Session()
        # 本地/内网地址不走系统代理，避免 localhost 被代理拦截
        if local:
            session.trust_env = False
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        t0 = time.time()
        attempts = max(1, int(HTTP_RETRY_MAX))
        # ② 远端网关 + 判定为「上游没算力」→ 只试 1 次。
        #    本地地址例外：本地模型冷启动的瞬时 503 是真的会自愈，保留重试。
        upstream_bad = False
        upstream_detail = ""
        resp = None
        last_err = None
        idx = 0
        while idx < attempts:
            # ⚠️ 退避休眠前先看一眼中止信号（托管暂停 / 用户停止）。
            # 这里还没发出任何请求、没产出任何文件，中止是安全的；
            # 少了这一句，「暂停」要等退避跑满 + 整个重试链结束才有反应。
            cancellation.check("LLM 请求重试前收到中止信号")
            if idx:
                cancellation.sleep(HTTP_RETRY_BACKOFF[min(idx - 1, len(HTTP_RETRY_BACKOFF) - 1)])
            idx += 1
            try:
                resp = session.post(url, headers=headers, json=payload,
                                    timeout=timeout or self.timeout)
            except NETWORK_ERRORS as e:
                last_err = e
                resp = None
                # 远端超时/连不上 = 「网关不可用」的典型形态（实测上游没算力时会挂到超时）
                if not local:
                    # ⚠️ 2026-09-25 实测修正：**超时与连接失败必须分开**。
                    # 旧逻辑把两者一律判成 upstream_bad=True 并 break，于是远端只要
                    # **一次** ReadTimeout（read timeout=1200 要等满 20 分钟）就整集中止 ——
                    # 实测第 1 集即此坑：日志「LLM 请求瞬时失败（第 1/3 次）：ReadTimeout」
                    # 之后紧接 ERROR「第1集生成失败…已尝试 1/3 次」，那个 1/3 是假的，
                    # 根本没发生第 2、3 次重试。
                    # 而「连不上」（ConnectionError / DNS / 拒绝连接）才是真的打完没用，
                    # 保持 fail-fast；「连上了但响应慢/偶尔挂死」值得**再给一次**机会 ——
                    # 上游偶发挂起是这类网关的常态，一次就放弃会把整集白白烧掉。
                    if _is_timeout_error(e):
                        # 超时：允许额外一次重试（仅一次，避免 2×20 分钟地等）
                        if idx >= TIMEOUT_MAX_ATTEMPTS:
                            upstream_bad = True
                            upstream_detail = f"{type(e).__name__}: {e}"
                        else:
                            logger.warning(
                                "远端读超时（第 %d/%d 次），再重试一次：%s", idx, attempts, e)
                    else:
                        upstream_bad = True
                        upstream_detail = f"{type(e).__name__}: {e}"
                logger.warning(f"LLM 请求瞬时失败（第 {idx}/{attempts} 次）：{e}")
                if upstream_bad:
                    break
                continue
            except Exception as e:  # noqa: BLE001
                raise LLMError(f"请求异常：{e}") from e
            if resp.status_code in HTTP_TRANSIENT_CODES:
                body = resp.text or ""
                last_err = LLMError(f"接口返回 HTTP {resp.status_code}：{body[:200]}")
                logger.warning(f"LLM 接口瞬时不可用（HTTP {resp.status_code}，"
                               f"第 {idx}/{attempts} 次），将退避后重试")
                if not local and _is_upstream_unavailable(resp.status_code, body):
                    upstream_bad = True
                    upstream_detail = f"HTTP {resp.status_code}：{body[:200]}"
                    logger.error(f"上游明确报「无可用算力」，放弃重试：{body[:200]}")
                    # ⚠️ 必须先把 resp 置空再 break：下面靠 `resp is None` 判定是否失败，
                    # 漏了这行会直接掉进 `resp.status_code >= 400` 分支，
                    # 抛成普通 LLMError，熔断/快速失败就整个失效了。
                    resp = None
                    break
                resp = None
                continue
            break
        latency = int((time.time() - t0) * 1000)
        if resp is None:
            if upstream_bad:
                streak = _note_gateway_failure(url, upstream_detail)
                raise LLMGatewayUnavailable(
                    f"网关 {_gateway_origin(url)} 不可用"
                    f"（第 {streak} 次连续失败，已尝试 {idx}/{attempts} 次）：{upstream_detail}",
                    base_url=_gateway_origin(url), streak=streak, detail=upstream_detail)
            raise LLMError(f"接口连续 {attempts} 次瞬时不可用（含超时/连接失败）：{last_err}")
        _clear_gateway_failure(url)

        if resp.status_code >= 400:
            body = (resp.text or "")[:400]
            raise LLMError(f"接口返回 HTTP {resp.status_code}：{body}")
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"接口返回非 JSON 内容：{(resp.text or '')[:300]}") from e
        data["_latency_ms"] = latency
        return data

    @staticmethod
    def _extract_choice(data: dict) -> tuple:
        """返回 (content, finish_reason)，同时保留 reasoning_content 供诊断"""
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"接口返回缺少 choices 字段：{json.dumps(data, ensure_ascii=False)[:300]}")
        ch = choices[0]
        finish_reason = ch.get("finish_reason")
        msg = ch.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # 兼容 content 为分段数组的实现
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not content:
            content = ch.get("text") or ""
        if not content:
            reasoning = str(msg.get("reasoning_content") or "")
            if reasoning:
                raise LLMReasoningOnlyError(
                    "模型只返回了思考内容（reasoning_content），正文为空 —— "
                    "思考过程把 max_tokens 用光了，加大额度重试即可。"
                    f"思考片段：{reasoning[:200]}"
                )
            raise LLMError("接口返回内容为空")
        return str(content), finish_reason

    @staticmethod
    def _extract_content(data: dict) -> str:
        return LLMClient._extract_choice(data)[0]

    @staticmethod
    def is_truncated(finish_reason) -> bool:
        """是否因长度上限被截断"""
        return str(finish_reason or "").lower() in ("length", "max_tokens", "max_output_tokens")

    def _build_payload(self, messages: list, temperature: float, max_tokens: int) -> dict:
        """构造 OpenAI 兼容请求体

        思考控制分两种模型形态（**互斥，reasoning_effort 优先**）：

        1. **可关思考**（Qwen / vLLM 系）：`disable_thinking=True` 时注入
           `chat_template_kwargs.enable_thinking=false`。
        2. **思考不可关闭**（GLM-5.3-Flash 等 always-on reasoning）：没有 enable_thinking，
           只有 `reasoning_effort = low|high|max`（默认 max）。此时「关思考」无从谈起，
           只能选档位 + 给足额度下限。

        两种情况下都抬高一档 max_tokens 下限：思考要吃 token，给太少必然只剩 reasoning_content。
        """
        if self.reasoning_effort:
            try:
                mt = int(max_tokens or 0)
            except (TypeError, ValueError):
                mt = 0
            if 0 < mt < MIN_TOKENS_WHEN_REASONING_EFFORT:
                logger.info(f"reasoning_effort={self.reasoning_effort} 模式下 max_tokens 由 {mt} "
                            f"抬到 {MIN_TOKENS_WHEN_REASONING_EFFORT}")
                max_tokens = MIN_TOKENS_WHEN_REASONING_EFFORT
        elif not getattr(self, "disable_thinking", DISABLE_THINKING_DEFAULT):
            try:
                mt = int(max_tokens or 0)
            except (TypeError, ValueError):
                mt = 0
            if 0 < mt < MIN_TOKENS_WHEN_THINKING:
                logger.info(f"允许思考模式下 max_tokens 由 {mt} 抬到 {MIN_TOKENS_WHEN_THINKING}")
                max_tokens = MIN_TOKENS_WHEN_THINKING
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.reasoning_effort:
            kwargs = {"reasoning_effort": self.reasoning_effort}
            # clear_thinking 默认 false，聊天场景官方建议显式传 true（避免上一轮思考串场）
            kwargs["clear_thinking"] = True
            if REASONING_EFFORT_STYLE == "top_level":
                payload.update(kwargs)
            else:
                payload["chat_template_kwargs"] = kwargs
        elif getattr(self, "disable_thinking", DISABLE_THINKING_DEFAULT):
            # ⚠️ 注意：这个参数只有 Qwen/vLLM 系认。GLM-5.3 这类 always-on 模型请改用
            # reasoning_effort，否则会出现「以为关了思考、其实还在按 max 档烧 token」。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def chat(self, messages: list, temperature: float = 0.7, max_tokens: int = 4096,
             timeout: int = None) -> str:
        self.require_configured()
        payload = self._build_payload(messages, temperature, max_tokens)
        data = self._post(payload, timeout=timeout)
        return self._extract_content(data)

    def chat_ex(self, messages: list, temperature: float = 0.7, max_tokens: int = 4096,
                timeout: int = None) -> dict:
        """与 chat 相同，但额外返回 finish_reason 等诊断信息

        返回：{content, finish_reason, truncated, max_tokens, latency_ms, usage}
        """
        self.require_configured()
        payload = self._build_payload(messages, temperature, max_tokens)
        data = self._post(payload, timeout=timeout)
        content, finish_reason = self._extract_choice(data)
        return {
            "content": content,
            "finish_reason": finish_reason,
            "truncated": self.is_truncated(finish_reason),
            "max_tokens": max_tokens,
            "latency_ms": data.get("_latency_ms"),
            "usage": data.get("usage") or {},
        }

    def chat_tools(self, messages: list, tools: list, temperature: float = 0.3,
                   max_tokens: int = 4096, timeout: int = None) -> dict:
        """Function-calling 调用（OpenAI 兼容 tools 协议）

        返回：{content, tool_calls:[{id, name, arguments}], finish_reason, usage}
        其中 arguments 是**未解析的 JSON 字符串**，由调用方自行 json.loads，
        这样模型吐出非法参数时调用方能拿到原文去做诊断与降级。

        若服务端不支持 tools（多数国产兼容层会返回 400 或直接忽略），
        调用方应捕获 LLMError 后走「纯文本 + JSON 动作块」降级链路。
        """
        self.require_configured()
        payload = self._build_payload(messages, temperature, max_tokens)
        payload["tools"] = tools
        data = self._post(payload, timeout=timeout)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"接口返回缺少 choices 字段：{json.dumps(data, ensure_ascii=False)[:300]}")
        msg = choices[0].get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            calls.append({
                "id": tc.get("id") or f"call_{i}",
                "name": str(fn.get("name") or "").strip(),
                "arguments": fn.get("arguments") or "{}",
            })
        return {
            "content": msg.get("content") or "",
            "tool_calls": calls,
            "finish_reason": choices[0].get("finish_reason"),
            "usage": data.get("usage") or {},
        }

    # ---- JSON 输出 ----
    @staticmethod
    def parse_json(content: str) -> dict:
        text = (content or "").strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
            if m:
                text = m.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.debug("JSON 候选 1 解析失败（试下一个候选）：%s", e)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)  # 去尾逗号
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                logger.debug("JSON 候选 2 解析失败（试下一个候选）：%s", e)
        start, end = text.find("["), text.rfind("]")
        # 顶层已是对象（以 { 开头）时不再用中括号片段兜底，否则会返回内层数组，
        # 导致 _repair_truncated_json 无从发挥、调用方拿到语义错位的片段
        if start != -1 and end > start and (text.find("{") == -1 or start < text.find("{")):
            candidate = re.sub(r",\s*([}\]])", r"\1", text[start:end + 1])
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                logger.debug("JSON 候选 3 解析失败（试下一个候选）：%s", e)
        repaired = LLMClient._repair_truncated_json(text)
        if repaired is not None:
            logger.warning("JSON 不完整，已按截断修复（补全尾部结构）后解析成功")
            return repaired
        raise LLMError(f"模型输出无法解析为 JSON：{text[:300]}")

    @staticmethod
    def _repair_truncated_json(text: str):
        """修复被截断的 JSON（模型中途停止或 max_tokens 用尽）

        从右往左回退到最近的合法结构边界，删掉尾部残缺的键值片段，
        再按未闭合的括号/引号补齐；逐个候选尝试 json.loads，全部失败返回 None。
        """
        s = text[text.find("{"):] if "{" in text else text
        if not s:
            return None
        cands = [i for i, ch in enumerate(s) if ch in "}],\""]

        def _try_fix(i: int):
            head = s[:i + 1]
            head = re.sub(r'[,{]\s*"[^"]*"\s*:\s*"?[^"]*$', "", head)  # 去掉尾部残缺键值
            head = re.sub(r"[,{]\s*$", "", head)
            stack, in_str, esc = [], False, False
            for ch in head:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch in "{[":
                    stack.append(ch)
                elif ch in "}]" and stack:
                    stack.pop()
            fixed = head + ('"' if in_str else "")
            for op in reversed(stack):
                fixed += "}" if op == "{" else "]"
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                return None

        # 两趟扫描：优先返回 dict（顶层对象语义完整，如 state_in / state_out / shots），
        # 只有找不到任何可用对象时才退化为内层 list，避免调用方拿到错位的数组片段。
        for want_dict in (True, False):
            for i in reversed(cands[-200:]):
                obj = _try_fix(i)
                if not obj:
                    continue
                if want_dict and isinstance(obj, dict):
                    return obj
                if not want_dict and isinstance(obj, list):
                    return obj
        return None

    def chat_json(self, prompt: str, system: str = None, temperature: float = 0.4,
                  max_tokens: int = 4096, retries: int = 1) -> dict:
        """要求模型输出 JSON，失败自动重试（兼容旧行为，内部委托 chat_json_robust）"""
        return self.chat_json_robust(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens,
            max_attempts=max(1, retries + 1),
        )

    def chat_json_robust(self, prompt: str, system: str = None, temperature: float = 0.4,
                         max_tokens: int = 4096, max_attempts: int = 3,
                         token_ladder=None, on_event=None) -> dict:
        """带「截断感知 → 自动提高 max_tokens → 重试」的 JSON 调用（A 项③ 核心）

        重试策略：
        - finish_reason=length（被截断）→ 追加"精简输出"提示，并把 max_tokens 提升到阶梯的下一档再试；
        - 输出无法解析为 JSON → 追加 JSON 修正提示，并把 max_tokens 提升一档再试；
        - 全部尝试失败 → 抛 LLMTruncatedError（截断导致）或 LLMError，错误信息含 finish_reason 与原因。

        诊断信息写入 self.last_json_meta。
        """
        ladder = tuple(token_ladder or DEFAULT_TOKEN_LADDER)
        # 统一抬到思考水位：各调用点按正文长度估的额度（1600~7500）没算思考开销，
        # 低于水位时正文恒为空（「模型只吐思考内容」）。集中兜住，避免逐点漏改。
        max_tokens = thinking_token_floor(max_tokens, self.reasoning_effort)
        cur = int(max_tokens or 4096)
        attempts = 0
        truncated = False
        last_err = None
        history = []
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _thinking_floor() -> int:
            """本 client 的思考额度下限：档位模式下 GLM 系建议 ≥2048，其余 ≥1024"""
            return (MIN_TOKENS_WHEN_REASONING_EFFORT if self.reasoning_effort
                    else MIN_TOKENS_WHEN_THINKING)

        def _next_tokens(value: int) -> int:
            for t in ladder:
                if t > value:
                    return min(t, MAX_TOKENS_CEILING)
            return min(value * 2, MAX_TOKENS_CEILING)

        def _raise_over_thinking_floor(value: int) -> int:
            """把额度抬到「思考水位」之上，返回提升后的值（已在上方则不降）。

            为什么单独有这一档：`_next_tokens` 走的是调用方传入的 ladder，而剧本/分镜这类
            长输出任务的起始额度可能只有 4800，阶梯第一级却是 8192 —— 一旦 `cur` 已 ≥ 阶梯
            首项，`_next_tokens` 就只会「×2」，遇到 MAX_TOKENS_CEILING 更是原地返回。
            实测就是在这里卡死：low 档 + 4800 额度 → 思考恒超额度 → 正文永远为空。
            所以「只吐思考」时**先无条件越过思考水位**，再谈降档。
            """
            floor = max(REASONING_ONLY_TOKEN_FLOOR, _thinking_floor())
            if value >= floor:
                return value
            return min(floor, MAX_TOKENS_CEILING)

        def _downgrade_reasoning_effort() -> bool:
            """把 reasoning_effort 降到下一档，返回是否还有可降空间。

            目标：always-on reasoning 模型（GLM-5.3 / agnes-3.0 等）在 high/max 档下
            思考内容可能极长（远超 max_tokens 上限），此时**只提 max_tokens 救不回来**，
            必须先降思考档位、压缩思考长度，正文才有机会露出来。
            """
            cur_re = str(getattr(self, "reasoning_effort", "") or "").strip().lower()
            order = REASONING_EFFORT_DOWNGRADE_ORDER
            idx = order.index(cur_re) if cur_re in order else -1
            nxt = order[idx + 1] if 0 <= idx < len(order) - 1 else None
            if nxt is None:
                return False
            self.reasoning_effort = nxt
            logger.warning(f"模型只吐思考内容：reasoning_effort 由 {cur_re or '(未注入)'} "
                           f"降级为 {nxt or '(不注入)'}，以压缩思考长度")
            return True

        repaired_fallback = None
        for _ in range(max(1, int(max_attempts))):
            # 提额重试是最外层循环（每次都会重新发一次完整请求），收益最大：
            # 托管暂停时立刻停下，不再为「模型只吐思考」反复加码重试。
            cancellation.check("LLM 提额重试前收到中止信号")
            attempts += 1
            try:
                r = self.chat_ex(messages, temperature=temperature, max_tokens=cur)
            except LLMReasoningOnlyError as e:
                # 思考吃光额度。⚠️ 救助顺序是「先提额、再降档」，不能反过来：
                # 空正文的根因是「思考量 > max_tokens」，而低档位并不保证思考就短到装得下
                # （实测 low 档 + 4800 额度仍 100% 空正文）。先前只降档不提额，
                # 在 ladder 首项已被越过时 `_next_tokens` 会原地返回同值 → 每次重试都空转，
                # 最后被误判成「模型/服务端故障」。
                last_err = e
                # ① 无条件越过思考水位（这是唯一能真正把正文挤出来的手段）。
                raised = _raise_over_thinking_floor(cur)
                # ② 越过水位后仍无正文，才说明思考确实过长 → 再压思考档位。
                downgraded = False
                if raised <= cur and _next_tokens(cur) <= cur and self.reasoning_effort:
                    downgraded = _downgrade_reasoning_effort()
                # ⚠️ 2026-10-10 修复「明明还能提额却判定为异常」：
                #    原表达式在 raised == cur（已在思考水位之上）且 downgraded 为假时，
                #    三元表达式走 else 分支 → nxt 回落成 cur → 下游 L923 判「nxt <= cur」
                #    → 误判「提额到顶」并快速失败。实测：cur=19384 时 _next_tokens 本可
                #    给到 24576（阶梯里有），却因为这条分支根本没被调用。
                #    现在改成无条件取三者最大值：水位、阶梯下一档、必要时还叠一次。
                _candidates = [int(raised or 0), int(_next_tokens(cur) or 0)]
                if downgraded:
                    _candidates.append(_next_tokens(max(cur, _thinking_floor())))
                nxt = max(_candidates + [cur])
                nxt = min(nxt, MAX_TOKENS_CEILING)
                # ③ 额度已顶到上限、档位也降无可降 → 才是真的模型/服务端异常，快速失败上浮。
                if nxt <= cur and not downgraded:
                    logger.error(
                        f"模型在最低思考档（{getattr(self, 'reasoning_effort', '') or '未注入'}）下"
                        f"仍只吐思考内容（第 {attempts} 次，max_tokens={cur} 已达上限），"
                        "判定为模型/服务端异常，停止无意义提额，快速失败上浮。")
                    history.append({"attempt": attempts, "max_tokens": cur,
                                    "finish_reason": "reasoning_only", "truncated": True,
                                    "content_len": 0, "latency_ms": None})
                    break
                logger.warning(f"模型只吐思考内容（第 {attempts} 次，max_tokens={cur}），"
                               f"提高到 {nxt} 重试"
                               + (f"（并把思考档位降为 {self.reasoning_effort}）" if downgraded else ""))
                history.append({"attempt": attempts, "max_tokens": cur,
                                "finish_reason": "reasoning_only", "truncated": True,
                                "content_len": 0, "latency_ms": None})
                if on_event:
                    try:
                        on_event(history[-1])
                    except Exception as e2:  # noqa: BLE001
                        logger.debug("流式事件回调异常（忽略）：%s", e2)
                cur = nxt
                continue
            truncated = r["truncated"]
            history.append({"attempt": attempts, "max_tokens": cur,
                            "finish_reason": r["finish_reason"], "truncated": truncated,
                            "content_len": len(r["content"]), "latency_ms": r["latency_ms"]})
            if on_event:
                try:
                    on_event(history[-1])
                except Exception as e:  # noqa: BLE001
                    logger.debug("流式事件回调异常（忽略，不阻断对话）：%s", e)
            data = None
            try:
                data = self.parse_json(r["content"])
            except LLMError as e:
                last_err = e
                logger.warning(f"JSON 解析失败（第 {attempts} 次，max_tokens={cur}，"
                               f"finish_reason={r['finish_reason']}）：{e}")
                messages.append({"role": "assistant", "content": r["content"][:2000]})
                messages.append({"role": "user",
                                 "content": TRUNCATION_RETRY_HINT if truncated else JSON_RETRY_HINT})
            if data is not None:
                self.last_json_meta = {
                    "attempts": attempts, "finish_reason": r["finish_reason"],
                    "max_tokens": cur, "truncated": truncated, "history": history,
                    "accepted_partial": bool(truncated),
                }
                if not truncated:
                    return data
                nxt = _next_tokens(cur)
                if nxt > cur:
                    # 被截断但结构可修复：不静默接受部分结果，先提额重试拿完整版
                    logger.warning(f"输出被截断（finish_reason=length）但已修复出部分结构，"
                                   f"提升 max_tokens {cur}→{nxt} 重试以获取完整内容")
                    repaired_fallback = data
                    messages.append({"role": "assistant", "content": r["content"][:2000]})
                    messages.append({"role": "user", "content": TRUNCATION_RETRY_HINT})
                else:
                    logger.warning("输出被截断且 max_tokens 已到上限，接受修复后的部分结果（可能不完整）")
                    return data
            cur = _next_tokens(cur)

        if repaired_fallback is not None:
            self.last_json_meta["accepted_partial"] = True
            logger.warning("连续提额后仍未取得完整 JSON，降级返回修复后的部分结果")
            return repaired_fallback

        last = history[-1] if history else {}
        detail = {"attempts": attempts, "truncated": truncated, "history": history,
                  "max_tokens": last.get("max_tokens", max_tokens),
                  "finish_reason": last.get("finish_reason")}
        self.last_json_meta = {
            "attempts": attempts, "finish_reason": last.get("finish_reason"),
            "max_tokens": last.get("max_tokens", max_tokens), "truncated": truncated,
            "history": history,
        }
        if truncated:
            raise LLMTruncatedError(
                f"连续 {attempts} 次调用均因输出被截断（finish_reason=length，最后一次 max_tokens="
                f"{detail['max_tokens']}）而未能得到完整 JSON，建议进一步缩小单次分析规模。"
                f"最后错误：{last_err}", detail=detail)
        # 全是「只吐思考」的失败：给出针对性提示，别让上层/用户误以为是「JSON 格式问题」。
        if history and all(h.get("finish_reason") == "reasoning_only" for h in history):
            raise LLMError(
                f"模型连续 {attempts} 次只返回思考内容（reasoning_content）而没有正文，"
                "即使已把思考档位降到最低、额度提到上限仍无正文。这通常是模型/服务端异常"
                "（思考无限长或参数异常），并非本任务的 JSON 格式问题。建议："
                "到「AI 设置」降低该模块的 reasoning_effort（low）、或换一个非 reasoning 模型、"
                "或稍后重试。")
        raise LLMError(f"连续 {attempts} 次调用均未返回合法 JSON。最后错误：{last_err}")

    # ---- 连接测试 ----
    def test_connection(self) -> dict:
        """连通测试：验证 base_url / api_key / model 三者可用

        ⚠️ 这里必须走 `_build_payload` 而不是自己拼 body。
        曾经自己拼 `max_tokens=32`，在「允许思考」模式下思考本身就要几百 token，
        于是正文永远为空、`reply` 里只剩模型的自我独白（「The user asks: …」），
        用户看到这句会直接判定「AI 配错了」，而实际上链路是通的。
        现在由 `_build_payload` 统一抬到 MIN_TOKENS_WHEN_THINKING，
        并额外返回 finish_reason / thinking_only 等诊断字段。
        """
        if not self.base_url:
            return {"success": False, "error": "请先填写 base_url"}
        if not self.api_key:
            return {"success": False, "error": "请先填写 api_key"}
        if not self.model:
            return {"success": False, "error": "请先填写 model"}
        messages = [
            {"role": "system", "content": "你是连接测试助手。不要解释、不要思考，直接按要求输出。"},
            {"role": "user", "content": "请只回复两个字：连通"},
        ]
        base_out = {
            "chat_url": self.chat_url,
            "model": self.model,
            "disable_thinking": bool(getattr(self, "disable_thinking",
                                             DISABLE_THINKING_DEFAULT)),
        }
        payload = self._build_payload(messages, 0, 256)
        base_out["max_tokens"] = payload.get("max_tokens")
        try:
            data = self._post(payload, timeout=45)
            content, finish_reason = self._extract_choice(data)
            content = (content or "").strip()
            out = dict(base_out, success=True, latency_ms=data.get("_latency_ms"),
                       finish_reason=finish_reason,
                       truncated=self.is_truncated(finish_reason),
                       thinking_only=False, reply=content[:200], verdict="ok")
            if self.is_truncated(finish_reason):
                out["hint"] = "链路正常，但回复被长度上限截断（不影响连通性判定）。"
            return out
        except LLMReasoningOnlyError:
            # 走到这里说明 HTTP 通了、模型也确实回答了，只是把额度全用在思考上。
            # 这**不是配置错误**，绝不能给用户报「测试失败」（这正是之前的误判来源：
            # 失败文案里还带着模型的自我独白「The user asks: …」）。
            return dict(base_out, success=True, thinking_only=True, truncated=True,
                        finish_reason="reasoning_only", reply="",
                        verdict="reachable_but_no_content",
                        hint=("链路可达，模型把 token 全用在思考上、没有输出正文。"
                              "这通常不影响正式任务；若正式任务报「正文为空」，"
                              "请提高该模块的 max_tokens，或对轻量任务关闭思考模式。"))
        except LLMError as e:
            return dict(base_out, success=False, error=str(e), verdict="failed")


# ===================== 备用模型故障转移（2026-09-30） =====================
class FailoverLLMClient:
    """主 + 备用 LLMClient 链的故障转移包装器。

    设计目标（用户要求）：主模型连续 3 次 API 报错 → 自动切到下一个备用模型，
    任务继续跑、不中断成片生产。主模型恢复后自动回切（每次调用前探活主模型）。

    对外接口 = LLMClient 常用子集：chat / chat_ex / chat_tools / chat_json /
    chat_json_robust / test_connection / 属性（model、base_url、configured、
    last_json_meta、last_error、reasoning_effort、disable_thinking、chat_url）。
    上层调用方（novel_to_script / continuity / script_generator / qc_client）
    无需任何改动 —— 同一个鸭子接口。

    切换语义：
    - 只对「API 级错误」计数（LLMError 族，含 LLMGatewayUnavailable）：
      模型输出质量问题（JSON 解析失败 / 截断 / 只吐思考）由调用方各自的
      retry 逻辑处理，不触发切换。
    - 当前 active 连续 3 次 API 报错 → 切到下一个 client（计数清零）；
      备用也挂就按序试下一个；全链耗尽才抛最后一次的错。
    - 切到备用后，每次调用前先对主模型做一次轻量探活（45s 超时）；
      主模型活了就回切，任务零感知。
    """

    _FAILOVER_THRESHOLD = 3          # 连续 N 次 API 报错 → 切备用
    _PROBE_TIMEOUT = 45             # 回切前主模型探活超时（秒）

    def __init__(self, clients: list, labels: list = None, module: str = ""):
        if not clients:
            raise LLMError("FailoverLLMClient 至少需要一个客户端")
        self.clients = list(clients)
        self.labels = list(labels) if labels and len(labels) == len(clients) \
            else [f"模型{i + 1}" for i in range(len(clients))]
        self.module = module
        self._active_idx = 0
        self._consec_fail = 0        # 当前 active 的连续 API 报错数
        self._switch_log = []        # [{from, to, reason, at}] 供诊断回显

    # ---- 鸭子接口：转发当前 active client 的属性 ----
    @property
    def active(self) -> LLMClient:
        return self.clients[self._active_idx]

    @property
    def configured(self) -> bool:
        return any(c.configured for c in self.clients)

    @property
    def model(self) -> str:
        return self.active.model

    @property
    def base_url(self) -> str:
        return self.active.base_url

    @property
    def chat_url(self) -> str:
        return self.active.chat_url

    @property
    def last_error(self) -> str:
        return getattr(self.active, "last_error", "")

    @last_error.setter
    def last_error(self, value):
        try:
            self.active.last_error = value
        except Exception:  # noqa: BLE001
            pass

    @property
    def last_json_meta(self) -> dict:
        return getattr(self.active, "last_json_meta", {}) or {}

    @last_json_meta.setter
    def last_json_meta(self, value):
        try:
            self.active.last_json_meta = value
        except Exception:  # noqa: BLE001
            pass

    @property
    def reasoning_effort(self) -> str:
        return getattr(self.active, "reasoning_effort", "")

    @reasoning_effort.setter
    def reasoning_effort(self, value):
        try:
            self.active.reasoning_effort = value
        except Exception:  # noqa: BLE001
            pass

    @property
    def disable_thinking(self):
        return getattr(self.active, "disable_thinking", False)

    @disable_thinking.setter
    def disable_thinking(self, value):
        try:
            self.active.disable_thinking = value
        except Exception:  # noqa: BLE001
            pass

    # ---- 故障转移核心 ----
    def _note_api_error(self, exc) -> None:
        """记一次 API 级报错；达到阈值时切到下一个 client。"""
        self._consec_fail += 1
        if self._consec_fail < self._FAILOVER_THRESHOLD:
            logger.warning(
                "LLM API 报错（%s 连续 %d/%d 次）：%s",
                self.module or "llm", self._consec_fail,
                self._FAILOVER_THRESHOLD, str(exc)[:200])
            return
        cur = self._active_idx
        if cur + 1 >= len(self.clients):
            logger.error("LLM 已是故障转移链尾（%s），无备用可切", self.labels[cur])
            self._consec_fail = 0
            return
        nxt = cur + 1
        self._switch_log.append({"from": self.labels[cur], "to": self.labels[nxt],
                                 "reason": str(exc)[:150],
                                 "at": datetime.now().isoformat(timespec="seconds")})
        logger.warning(
            "LLM 故障转移：%s → %s（连续 %d 次 API 报错）：%s",
            self.labels[cur], self.labels[nxt], self._consec_fail, str(exc)[:200])
        self._active_idx = nxt
        self._consec_fail = 0

    def _try_return_to_primary(self) -> None:
        """若当前不在主模型，探活主模型；活了就回切（任务零感知）。"""
        if self._active_idx == 0:
            return
        primary = self.clients[0]
        try:
            _payload = primary._build_payload(
                [{"role": "user", "content": "ping"}], 0, 16)
            primary._post(_payload, timeout=self._PROBE_TIMEOUT)
            self._switch_log.append(
                {"from": self.labels[self._active_idx], "to": self.labels[0],
                 "reason": "主模型探活成功，回切",
                 "at": datetime.now().isoformat(timespec="seconds")})
            logger.info("LLM 主模型已恢复，切回主模型（原：%s）",
                       self.labels[self._active_idx])
            self._active_idx = 0
            self._consec_fail = 0
        except LLMError:
            pass  # 主模型还挂着，继续用备用
        except Exception:  # noqa: BLE001  探活本身异常不阻断任务
            pass

    def _call_with_failover(self, method: str, *args, **kwargs):
        """调 active client 的 method；API 级报错计数 + 自动切换 + 全链兜底。

        语义：当前 active 累计 _FAILOVER_THRESHOLD 次 API 报错后，自动切到下一个
        client 并清空计数（_note_api_error 内部处理）。整条链（主 + 各备用）依次
        给足各自阈值次数；全链耗尽才抛最后一次的错。
        """
        self._try_return_to_primary()
        last_exc = None
        for _round in range(len(self.clients)):
            client = self.active
            _budget = self._FAILOVER_THRESHOLD - self._consec_fail
            _attempted = 0
            while _attempted < _budget and self.active is client:
                try:
                    result = getattr(client, method)(*args, **kwargs)
                    self._consec_fail = 0
                    return result
                except LLMError as e:
                    last_exc = e
                    self._note_api_error(e)
                    _attempted += 1
                except Exception as e:  # noqa: BLE001  网络层非 LLMError 异常也计入
                    last_exc = e
                    self._note_api_error(e)
                    _attempted += 1
        if last_exc is None:
            raise LLMError("LLM 调用失败（故障转移链耗尽）")
        raise last_exc
    # ---- 对外接口（LLMClient 子集，上层无感知） ----
    def chat(self, messages: list, temperature: float = 0.7, max_tokens: int = 4096,
             timeout: int = None) -> str:
        return self._call_with_failover("chat", messages, temperature=temperature,
                                        max_tokens=max_tokens, timeout=timeout)

    def chat_ex(self, messages: list, temperature: float = 0.7, max_tokens: int = 4096,
                timeout: int = None) -> dict:
        return self._call_with_failover("chat_ex", messages, temperature=temperature,
                                        max_tokens=max_tokens, timeout=timeout)

    def chat_tools(self, messages: list, tools: list, temperature: float = 0.3,
                   max_tokens: int = 4096, timeout: int = None) -> dict:
        return self._call_with_failover("chat_tools", messages, tools,
                                        temperature=temperature,
                                        max_tokens=max_tokens, timeout=timeout)

    def chat_json(self, prompt: str, system: str = None, temperature: float = 0.4,
                  max_tokens: int = 4096, retries: int = 1) -> dict:
        return self._call_with_failover("chat_json", prompt, system=system,
                                        temperature=temperature,
                                        max_tokens=max_tokens, retries=retries)

    def chat_json_robust(self, prompt: str, system: str = None, temperature: float = 0.4,
                         max_tokens: int = 4096, max_attempts: int = 3,
                         token_ladder=None, on_event=None) -> dict:
        return self._call_with_failover("chat_json_robust", prompt, system=system,
                                        temperature=temperature,
                                        max_tokens=max_tokens,
                                        max_attempts=max_attempts,
                                        token_ladder=token_ladder, on_event=on_event)

    def test_connection(self) -> dict:
        """当前 active 的连通测试结果 + 故障转移链状态。"""
        out = dict(self.active.test_connection())
        out["failover"] = {
            "active": self.labels[self._active_idx],
            "active_index": self._active_idx,
            "chain": self.labels,
            "switches": self._switch_log[-5:],
        }
        return out

    @staticmethod
    def gateway_circuit_state(base_url: str) -> dict:
        return LLMClient.gateway_circuit_state(base_url)

    @staticmethod
    def reset_gateway_circuits() -> None:
        LLMClient.reset_gateway_circuits()

    def require_configured(self):
        if not self.configured:
            raise LLMError("尚未配置任何可用的 LLM 客户端（主 + 备用均未配置）")


def client_from_config(config_path: str, timeout: int = DEFAULT_TIMEOUT) -> LLMClient:
    return LLMClient(config_path=config_path, timeout=timeout)
