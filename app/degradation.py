"""静默降级的可观测化（2026-10-10 设计审查修复）。

## 问题
排查统计：全项目 1222 处 except，其中 **74 处是 `except Exception: pass`** ——
完全静默。分布：app.py 19 ｜ serve.py 7 ｜ llm_client.py 6 ｜ autopilot.py 4 ｜
coverage.py 4 ｜ task_lease.py 4 ｜ …

fail-open 本身是**合理设计**（配置/日志/清理失败不该让生产挂掉），
我在本轮也大量使用了它。但**没有任何计数**意味着「什么在降级」完全不可知：
长期挂机下某个依赖坏了、所有相关功能都在静默兜底，而日志里一条都没有 ——
排查时只能靠猜。

## 做法
不做「批量给 pass 加日志」（会产生噪声，且很多处确实应当安静），
而是提供一个**极轻量的降级计数器**：在原本静默的位置调用 note()，
不产生日志噪声，但可被 /api/config/doctor 等接口汇总看到。

## 用法
    from degradation import note
    try:
        ...
    except Exception:  # noqa: BLE001
        note('模块.位置', '简短原因')   # 取代裸 pass

## 纪律
本模块只依赖标准库、不 import 任何业务模块（避免制造循环依赖）；
note() 本身绝不抛异常（它自己失败就真的什么都不做）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Tuple

_LOCK = threading.Lock()
_COUNTS: Dict[Tuple[str, str], int] = {}
_FIRST: Dict[Tuple[str, str], float] = {}
_LAST: Dict[Tuple[str, str], float] = {}

#: 单项明细最多保留多少个不同 (where, reason) 组合，防止异常海量来源把内存吃光
_MAX_DISTINCT = 500


def note(where: str, reason: str = '') -> None:
    """记一次静默降级。绝不抛异常（自身失败即静默放弃）。"""
    try:
        k = (str(where)[:80], str(reason)[:120])
        now = time.time()
        with _LOCK:
            if k not in _COUNTS:
                if len(_COUNTS) >= _MAX_DISTINCT:
                    return          # 已达上限：丢弃新来源，只累加已知的
                _FIRST[k] = now
            _COUNTS[k] = _COUNTS.get(k, 0) + 1
            _LAST[k] = now
    except Exception:  # noqa: BLE001  计数器自身绝不能影响业务
        pass


def stats(top: int = 30) -> Dict[str, Any]:
    """汇总降级情况：总数、来源数、按次数排序的明细。"""
    try:
        with _LOCK:
            total = sum(_COUNTS.values())
            distinct = len(_COUNTS)
            items: List[Dict[str, Any]] = []
            for (where, reason), cnt in sorted(_COUNTS.items(), key=lambda kv: -kv[1])[:top]:
                items.append({
                    'where': where, 'reason': reason, 'count': cnt,
                    'first_seen': time.strftime('%Y-%m-%d %H:%M:%S',
                                                time.localtime(_FIRST.get((where, reason), 0))),
                    'last_seen': time.strftime('%Y-%m-%d %H:%M:%S',
                                               time.localtime(_LAST.get((where, reason), 0))),
                })
        return {'total': total, 'distinct': distinct, 'items': items}
    except Exception:  # noqa: BLE001
        return {'total': 0, 'distinct': 0, 'items': [], 'error': 'stats 失败'}


def reset() -> None:
    """清空计数（供测试使用）。"""
    try:
        with _LOCK:
            _COUNTS.clear()
            _FIRST.clear()
            _LAST.clear()
    except Exception:  # noqa: BLE001
        pass


def summary_line() -> str:
    """一行式摘要，供启动日志/体检接口。"""
    try:
        s = stats(top=1)
        if not s['total']:
            return '静默降级：0 次（本进程内没有发生被计数的降级）'
        top = s['items'][0] if s['items'] else {}
        return ('静默降级：%d 次 / %d 个来源 ｜ 最多的是 %s（%d 次）'
                % (s['total'], s['distinct'], top.get('where', '?'), top.get('count', 0)))
    except Exception:  # noqa: BLE001
        return '静默降级：统计失败'
