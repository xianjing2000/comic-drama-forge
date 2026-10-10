"""公共工具函数（2026-10-10 设计审查修复）。

## 为什么需要
设计审查扫描发现「同一工具函数在多个模块各写一遍」：
  · 逐字重复 3 组：pipeline/autopilot 的 _now、
    qc_client/llm_client 的 mask_key、novel_to_script/continuity 的 _as_dict；
  · 同名函数 18 个，其中 qc_client ↔ llm_client 有 7 对
    （load_config/save_config/clear_config/mask_key/public_view/_empty_config/_is_local）
    —— 两套 LLM 客户端各自实现了一整套配置管理。

这与用户反馈的「改一处漏一处」是**同构问题**：不是配置值不同步，而是**实现被复制**。
复制出来的实现会各自演化，最终连"有几个地方要改"都数不清
（例如 DISABLE_THINKING_DEFAULT 与 MIN_TOKENS_WHEN_THINKING 在 llm_client
与 qc_client 各有一份，就是这两套配置管理并存的结果）。

## 用法（关键：别名导入，业务调用点零改动）
各模块保留原有函数名，只把实现指向这里：
    from common_util import now as _now, nonempty as _nonempty
这样调用点一行都不用改，但**实现唯一** —— 以后只改这一处。

## 纪律
  · 这里只放**无业务语义**的纯工具（时间/JSON/空值/掩码），不放任何业务逻辑；
  · 不 import 任何业务模块，从根上避免引入新的循环依赖；
  · 全部函数 fail-safe：读文件这类操作出错时返回调用方给的默认值，不抛异常。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any


def now() -> str:
    """本地时间戳字符串（秒精度）。

    收敛自 pipeline._now / autopilot._now / coverage._now 三处逐字重复实现。
    """
    return time.strftime('%Y-%m-%d %H:%M:%S')


def nonempty(value: Any) -> bool:
    """判断「非空」：None / 空串 / 纯空白 视为空。收敛自 pipeline / autopilot。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def read_json(path: str, default: Any = None) -> Any:
    """读 JSON；文件不存在/损坏/权限问题一律返回 default（绝不抛）。

    收敛自 autopilot._read_json / project_store._read_json 等重复实现。
    """
    try:
        if not path or not os.path.isfile(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001  读配置失败不应中断业务
        return default


def write_json(path: str, data: Any, indent: int = 2) -> bool:
    """原子写 JSON（先写临时文件再替换，避免中途崩溃留下半个文件）。

    收敛自 autopilot._write_json / project_store._write_json。
    返回是否成功；失败不抛。
    """
    try:
        if not path:
            return False
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False


def as_dict(value: Any) -> dict:
    """把模型返回值规整成 dict（容错优先）。

    收敛自 novel_to_script._as_dict / continuity._as_dict 的逐字重复实现。
    ⚠️ 三个分支缺一不可 —— 第一版我只写了 dict + JSON 字符串两个分支，
    漏掉了 **list 包裹**，而原实现明确为「兼容部分模型把 JSON 对象包在数组里返回
    ([{"...": ...}]) 的情况」而存在。漏掉会让那种模型的输出直接被丢弃。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for it in value:
            if isinstance(it, dict):
                return it
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                for it in parsed:
                    if isinstance(it, dict):
                        return it
        except Exception:  # noqa: BLE001
            return {}
    return {}


def mask_key(key: Any, keep: int = 6) -> str:
    """把 API Key 打码成「前 keep 位 + ****」。收敛自 llm_client / qc_client 的重复实现。

    空值返回空串（不要返回 'None' 之类的字符串，前端会误以为配置了密钥）。
    """
    s = '' if key is None else str(key).strip()
    if not s:
        return ''
    if len(s) <= keep:
        return '*' * len(s)
    return s[:keep] + '****'
