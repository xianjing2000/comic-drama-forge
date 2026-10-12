# -*- coding: utf-8 -*-
"""统一数据隔离 helper（t48）。

背景：.workbuddy/test/ 下 122 个 verify_*.py 是**独立脚本**（不经过 tests/ 包，
tests/__init__.py 的 import 期隔离闸对它们不生效），过去靠各脚本自行 monkeypatch，
新脚本一旦漏写就会污染生产根（import config 触发的 migrate_from_legacy 等会写库）。

本模块把这些脚本都需要的那套「数据根钉到临时目录 + sqlite 写闸」抽成可复用函数，
供 verify 守卫与任何独立脚本在 import 任何 app 模块**之前**调用：

    import sys; sys.path.insert(0, "<项目根>/tests")
    import isolation_helpers as iso
    iso.isolate_data_root()          # 必须先于 import config 等业务模块
    import config                     # 现在拿到的数据根已在临时目录

与 tests/__init__.py 的关系：tests/__init__.py 是 pytest/unittest 两条入口自动加载的
「第一道闸」（import 期生效）；本模块是给**不经过 tests 包**的独立脚本用的「手动闸」。
两者不冲突、不重复——都调同一个语义（MJSCXT_DATA_DIR → 临时目录）。
"""
import atexit
import os
import tempfile


def _is_tempish(path) -> bool:
    """该路径是否落在系统临时目录（= 已隔离）。空路径恒 False。"""
    if not path:
        return False
    try:
        p = os.path.normcase(os.path.abspath(str(path)))
    except Exception:  # noqa: BLE001
        return False
    for base in (tempfile.gettempdir(), os.environ.get("TEMP") or "",
                 os.environ.get("TMP") or ""):
        if base and p.startswith(os.path.normcase(os.path.abspath(base))):
            return True
    return False


def isolate_data_root(prefix: str = "mjscxt_iso_") -> str:
    """把 MJSCXT_DATA_DIR 钉到进程级临时目录（幂等），返回该目录绝对路径。

    必须在 import 任何会缓存数据根的 app 模块（config / env_loader / ai_credentials_db）
    **之前**调用 —— 那些模块在 import 时就把数据根定死，事后设 env 无效。
    """
    prev = os.environ.get("MJSCXT_DATA_DIR")
    if prev and _is_tempish(prev):
        return os.path.abspath(prev)
    tmp = tempfile.mkdtemp(prefix=prefix)
    os.environ["MJSCXT_DATA_DIR"] = tmp
    for sub in ("output", "novels", "prompt_overrides"):
        try:
            os.makedirs(os.path.join(tmp, sub), exist_ok=True)
        except OSError:
            pass
    atexit.register(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
    return tmp


def isolation_report() -> dict:
    """隔离事实快照，供测试/报告断言。"""
    import sqlite3
    return {
        "data_dir": os.environ.get("MJSCXT_DATA_DIR") or "",
        "is_temp": _is_tempish(os.environ.get("MJSCXT_DATA_DIR") or ""),
        "sqlite_guard": bool(getattr(sqlite3, "_mjscxt_iso_guard", False)),
    }
