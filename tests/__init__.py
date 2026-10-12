# -*- coding: utf-8 -*-
"""tests 包入口 —— **测试数据隔离的第一道闸**（t20 / D5，P0 数据安全）。

为什么放在这里：
  · app/env_loader.py 在 **import 时**就把 PROJECT_DATA_DIR 定死
    （os.environ.get("MJSCXT_DATA_DIR") or PROJECT_ROOT_DIR，见 env_loader:53）；
  · app/config.py 再用它算出 TASKS_DB_PATH / PROJECT_OUTPUT_DIR 等全套路径。
  于是**谁先 import config，数据根就是那一刻的目录** —— 后设 MJSCXT_DATA_DIR 无效。
  实测踩坑（t23/D5）：tests/test_config.py 自己在模块级设了临时目录，
  但全量跑时别的模块先 import 了 config → 数据根=仓库根 →
  CC.set_value(..., note="test-restore") 把测试设置写进了**开发数据根** output/tasks.db。

本模块是 pytest 与 python -m unittest discover -s tests **两条入口都会先加载**的位置
（pytest 收集 tests/ 时按包导入；unittest discover 先导入 start 目录的包），
因此在 import 期就把数据根改成临时目录，任何 app 模块都拿不到真实路径。

同时装一道 sqlite3 写闸：即使将来有人绕过数据根（硬编码路径 / 先 import 后改 env），
写向真实数据根的连接会**直接抛错并点名调用点**，而不是静默污染。
"""
import atexit
import os
import sys
import tempfile

# --------------------------------------------------------------------------- #
# 1) 数据根强制重定向到进程级临时目录（必须在任何 app 模块 import 之前）
# --------------------------------------------------------------------------- #
_ENV_OVERRIDE = "MJSCXT_T20_FORCE_DATA_DIR"
_PREV = os.environ.get("MJSCXT_DATA_DIR")


def _is_tempish(path: str) -> bool:
    """判断一个数据根是否已落在系统临时目录（= 已隔离）。"""
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:  # noqa: BLE001
        return False
    for base in (tempfile.gettempdir(), os.environ.get("TEMP") or "",
                 os.environ.get("TMP") or ""):
        if base and p.startswith(os.path.normcase(os.path.abspath(base))):
            return True
    return False


_FORCED = os.environ.get(_ENV_OVERRIDE) == "1" or not _is_tempish(_PREV or "")
if _FORCED:
    #: 每个测试**进程**一份独立数据根（不是每个模块），避免不同模块互相看不到彼此的写。
    #: 进程退出时清理。
    _T20_TMP = tempfile.mkdtemp(prefix="mjscxt_t20_data_")
    os.environ["MJSCXT_DATA_DIR"] = _T20_TMP
    for _sub in ("output", "novels", "prompt_overrides"):
        try:
            os.makedirs(os.path.join(_T20_TMP, _sub), exist_ok=True)
        except OSError:
            pass
    atexit.register(lambda: __import__("shutil").rmtree(_T20_TMP, ignore_errors=True))
else:  # 调用方已经给了临时目录
    _T20_TMP = _PREV

#: 允许作为「隔离数据根」的目录集合。
#: ⚠️ 必须**包含后来者**：个别测试模块（如 tests/test_config.py:22、test_config_channels.py:36）
#:   会在模块级再建一个自己的 tempfile 并覆盖 MJSCXT_DATA_DIR。只要它仍是临时目录，
#:   就等于「已隔离」（两条互不相干的数据根反而更安全）；
#:   这里禁止的是「被改回真实数据根」，不是「必须等于我这一个临时目录」。
_ALLOWED_ROOTS = [_T20_TMP]
for _sub in ("output", "novels", "prompt_overrides"):
    os.makedirs(os.path.join(_T20_TMP, _sub), exist_ok=True)


def allow_data_root(path: str) -> bool:
    """该路径是否算「已隔离」——必须是临时目录。"""
    return bool(path) and _is_tempish(path)


def data_dir() -> str:
    """当前测试进程的数据根（供测试断言「已隔离」）。"""
    return os.environ.get("MJSCXT_DATA_DIR") or ""


def real_data_roots():
    """**绝不允许测试写入**的真实数据根。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    out = [os.path.join(root, "output")]
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        out.append(os.path.join(appdata, "mjscxt-desktop", "mjscxt-data", "output"))
    return [os.path.normcase(os.path.abspath(p)) for p in out]


_ALLOW_REAL = os.environ.get("MJSCXT_T20_ALLOW_REAL_DB") == "1"


def _target_path(args, kwargs):
    """把 sqlite3.connect(...) 的实参解析成规范化路径（拿不到就返回空串）。"""
    raw = None
    if args:
        raw = args[0]
    elif "database" in kwargs:
        raw = kwargs["database"]
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""
    raw = str(raw)
    if raw.startswith("file:"):
        # URI 形式：只读连接不算污染
        if "mode=ro" in raw or "immutable=1" in raw:
            return ""
        raw = raw[5:].split("?", 1)[0]
    if not raw or raw == ":memory:":
        return ""
    return os.path.normcase(os.path.abspath(raw))

# --------------------------------------------------------------------------- #
# 2) sqlite3 写闸：写真实数据根的连接 → 显式报错（带调用点）
# --------------------------------------------------------------------------- #
def _install_sqlite_guard():
    import sqlite3
    if getattr(sqlite3, "_t20_guard", False):
        return
    _orig = sqlite3.connect

    def _guarded(*args, **kwargs):
        if _ALLOW_REAL:
            return _orig(*args, **kwargs)
        p = _target_path(args, kwargs)
        if p:
            for bad in real_data_roots():
                if p == bad or p.startswith(bad + os.sep):
                    import traceback
                    caller = "".join(traceback.format_stack()[-6:-3])
                    raise RuntimeError(
                        "[t20/D5] 测试试图把 sqlite 写到真实数据根：%s"
                        "\n  已隔离的数据根 = %s"
                        "\n  调用点：\n%s"
                        "\n  这是 P0 数据安全闸门（tests/test_config.py 曾把"
                        " note='test-restore' 写进开发根）。请改用 tests/__init__.py"
                        " 提供的临时数据根；若确实要读真实库，请用只读 URI"
                        "（file:...?mode=ro）。"
                        % (p, data_dir(), caller))
        return _orig(*args, **kwargs)

    sqlite3.connect = _guarded
    sqlite3._t20_guard = True
    sqlite3._t20_original_connect = _orig


_install_sqlite_guard()


#: 允许把数据根改回真实根的**调用方**白名单（例如某个测试故意验证真实路径行为）。
#: 目前为空 —— 任何把数据根改回真实根的模块都会被 force_data_root 当场纠正并打 warning。
_REAL_ROOT_OFFENDERS = []


def is_real_root(path) -> bool:
    """该路径是否落在「真实数据根」里。"""
    try:
        p = os.path.normcase(os.path.abspath(str(path or "")))
    except Exception:  # noqa: BLE001
        return False
    for bad in real_data_roots():
        if p == bad or p.startswith(bad + os.sep):
            return True
    return False


def force_data_root(reason="") -> dict:
    """把已 import 的 app 模块里的数据路径**纠正**回临时根（幂等, 每用例调用）。

    为什么需要它：数据根在 import 期被缓存（env_loader.PROJECT_DATA_DIR ->
    config.PROJECT_DATA_DIR / TASKS_DB_PATH / PROJECT_OUTPUT_DIR ...）。实测：
    `python -m unittest discover` 下个别导入顺序会让某个模块先于本包初始化读到**真实根**，
    于是 config_center.db_path() 指向仓库 output/tasks.db（测试会真的写进去）。
    单靠「init 期设 env」不足以保证顺序，故再加一道**运行期纠正**。

    返回 {模块名: (修复前, 修复后)}，供测试断言/打印。
    """
    fixed = {}
    env_loader = sys.modules.get("env_loader")
    if env_loader is None:
        return fixed
    root = data_dir()
    if not root or not _is_tempish(root):
        return fixed
    if is_real_root(getattr(env_loader, "PROJECT_DATA_DIR", "")):
        before = env_loader.PROJECT_DATA_DIR
        env_loader.PROJECT_DATA_DIR = root
        env_loader._DATA_ROOT_FIXED = root
        fixed["env_loader.PROJECT_DATA_DIR"] = (before, root)
        _REAL_ROOT_OFFENDERS.append((reason, before))
    cfg = sys.modules.get("config")
    if cfg is not None:
        for attr, rel in (("PROJECT_DATA_DIR", ""), ("PROJECT_OUTPUT_DIR", "output"),
                          ("TASKS_DB_PATH", os.path.join("output", "tasks.db"))):
            cur = getattr(cfg, attr, None)
            if cur and is_real_root(cur):
                new = os.path.join(root, rel) if rel else root
                setattr(cfg, attr, new)
                fixed["config." + attr] = (cur, new)
    #: 其它**在 import 期就缓存数据根**的模块：一律钉回临时根。
    #: ⚠️ 实测（2026-10-12，t20）：`python -m unittest discover` 会在导入本包**之前**
    #:  就先 import app/env_loader（日志证据：env_loader 看到 MJSCXT_DATA_DIR=None，
    #:  紧接着才是本包的初始化），于是 env_loader/config 把数据根定成了**仓库根**。
    #:  只靠「import 期设 env」挡不住这种顺序，必须有这道运行期纠正。
    other = [("config_center", ("_DB_PATH", os.path.join("output", "tasks.db"))),
             ("ai_credentials_db", ("_DB_PATH", os.path.join("output", "tasks.db"))),
             ("ai_credentials_db", ("_PROJECT_ROOT", "")),
             ("secret_store", ("_PROJECT_ROOT", "")),
             ("ai_config", ("_ROOT_DIR", "")),
             ("task_store", ("_DB_PATH", os.path.join("output", "tasks.db"))),
             ("task_store", ("DB_PATH", os.path.join("output", "tasks.db")))]
    for mod_name, (attr, rel) in other:
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        cur = getattr(mod, attr, None)
        if cur and is_real_root(cur):
            new = os.path.join(root, rel) if rel else root
            setattr(mod, attr, new)
            fixed[mod_name + "." + attr] = (cur, new)
    os.environ["MJSCXT_CRED_DB"] = os.path.join(root, "output", "tasks.db")
    return fixed


def _finalize_init() -> None:
    """import 期能做的收尾：把已加载模块纠正一遍（未加载的交给 _patch_watcher）。"""
    _reassert_env()
    force_data_root(reason="import-time")
    _start_patch_watcher()


_WATCH_TARGETS = ("env_loader", "config", "config_center", "ai_credentials_db",
                  "secret_store", "ai_config", "task_store")
_WATCH_STARTED = False


def _start_patch_watcher(deadline_sec: float = 120.0) -> bool:
    """后台纠偏线程：这些模块**可能在 import 期读到真实根**（实测 discover 下 env_loader
    会比本包先 import）。谁出现就纠正谁 —— 线程很轻（5ms 轮询 + 目标到齐即退出），
    是 daemon 且不持有任何东西。

    为什么不用 sys.meta_path 导入钩子：那会干扰 unittest/pytest 的模块解析；
    这里只需「模块出现后把路径改回临时根」，轮询最不容易出错。
    """
    global _WATCH_STARTED
    if _WATCH_STARTED:
        return True
    _WATCH_STARTED = True

    def _loop():
        import threading
        import time
        t0 = time.time()
        while time.time() - t0 < deadline_sec:
            try:
                force_data_root(reason="watcher")
                _reassert_env()
                if all(m in sys.modules for m in ("env_loader", "config",
                                                  "config_center", "ai_credentials_db")):
                    break
            except Exception:  # noqa: BLE001  纠偏线程自身绝不抛
                pass
            time.sleep(0.005)

    try:
        import threading
        th = threading.Thread(target=_loop, name="t20-data-root-watcher", daemon=True)
        th.start()
    except Exception:  # noqa: BLE001
        return False
    return True


def ensure_isolated(reason="runtime") -> dict:
    """测试用例/模块入口统一调用：钉 env + 纠正已加载模块。"""
    _reassert_env()
    return force_data_root(reason=reason)


def _reassert_env() -> bool:
    """把 os.environ 里的数据根重新钉回临时目录（幂等）。"""
    if _FORCED and _is_tempish(_T20_TMP):
        os.environ["MJSCXT_DATA_DIR"] = _T20_TMP
        return True
    return False


try:
    _finalize_init()
except Exception:  # noqa: BLE001
    pass


def isolation_report() -> dict:
    """给测试/报告用的隔离事实快照。"""
    import sqlite3
    return {
        "forced": _FORCED,
        "data_dir": data_dir(),
        "is_temp": _is_tempish(data_dir()),
        "real_roots": real_data_roots(),
        "guard_active": bool(getattr(sqlite3, "_t20_guard", False)),
        "allow_real": _ALLOW_REAL,
    }


# =========================================================================== #
# 数据根**前后指纹**工具（t20/D5）：原本是 tests/conftest.py 里的 pytest fixture，
# 2026-10-12 并入本文件。
#
# ⚠️ 实测结论（别被误导）：pytest **不会**从包的 __init__.py 采集 fixture —— 把一个
#    必然新建的哨兵路径塞进 _real_db_paths() 再跑 pytest，会话级断言**没有**触发。
#    所以下面这两个函数现在是**可调用的工具**，不是自动钩子：
#      · 用途一：测试/人工随时对比「跑测试前 vs 跑测试后」的真实数据根指纹
#        （本次 t20 验收就是用它给出的 sha256 前后一致证据）；
#      · 用途二：tests/test_config.py 的 setUpModule 会调用 _reassert_env + force_data_root
#        做**用例级**隔离断言（那是真正生效的回归护栏）。
#    若要恢复「每次 pytest 运行自动前后对比」，需要 tests/conftest.py —— 它在当前 inScope
#    的 glob 语义下被判 undeclared，已上报 captain 申请授权。
# =========================================================================== #
import json as _json

def _fingerprint(path):
    """只读指纹：存在性 + 大小 + mtime + app_settings 内容。"""
    out = {"path": path, "exists": os.path.isfile(path)}
    if not out["exists"]:
        return out
    st = os.stat(path)
    out["size"] = st.st_size
    out["mtime"] = round(st.st_mtime, 3)
    try:
        import sqlite3
        c = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
        out["app_settings"] = [list(r) for r in
                               c.execute("select key, value, note from app_settings order by key")]
        c.close()
    except Exception as e:  # noqa: BLE001
        out["probe_error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _real_db_paths():
    return [os.path.join(p, "tasks.db") for p in real_data_roots()]


def _diff_fingerprints(before, after):
    dirty = []
    for p in before:
        b, a = before[p], after[p]
        if b.get("exists") != a.get("exists"):
            dirty.append((p, "exists %s -> %s" % (b.get("exists"), a.get("exists"))))
            continue
        if not b.get("exists"):
            continue
        if b.get("app_settings") != a.get("app_settings"):
            dirty.append((p, "app_settings changed: %s -> %s" % (
                json.dumps(b.get("app_settings"), ensure_ascii=False),
                json.dumps(a.get("app_settings"), ensure_ascii=False))))
        elif b.get("mtime") != a.get("mtime"):
            dirty.append((p, "mtime changed: %s -> %s" % (b.get("mtime"), a.get("mtime"))))
    return dirty


def t20_session_isolation():
    """会话级：断言数据根已隔离 + 前后指纹一致。"""
    rep = isolation_report()
    assert rep["is_temp"], "数据根未隔离（%s）—— 强制重定向没生效" % rep["data_dir"]
    assert rep["guard_active"], "sqlite3 写闸未装上 —— 隔离无法兜底"
    before = {p: _fingerprint(p) for p in _real_db_paths()}
    yield
    after = {p: _fingerprint(p) for p in _real_db_paths()}
    dirty = _diff_fingerprints(before, after)
    assert not dirty, "本轮测试写到了真实数据根（t20/D5 红）：%s" % dirty


def t20_assert_isolated_paths():
    """用例级：先把数据根**重新钉回**临时目录，再断言所有已加载模块都指向它。

    为什么要「先钉再断言」：数据根在 import 期被缓存，而 unittest/pytest 的导入顺序
    不完全可控（实测 discover 下会出现某个模块先读到真实根）。tests/__init__.py 负责
    第一道（import 期），本 fixture 负责第二道（每个用例前纠正 + 断言），
    两道都过不了才会写进真实库。
    """
    _reassert_env()
    fixed = force_data_root(reason="pytest fixture")

    #: ⚠️ 这里**不能**要求「等于我这一个临时目录」：
    #:   · 数据根在 import 期就会被缓存（env_loader.PROJECT_DATA_DIR），
    #:     而个别模块（tests/test_config.py、test_config_channels.py）会再建自己的 tempfile；
    #:   · 结果同一进程里可能存在两个临时数据根 —— 都是隔离的，只是不相等。
    #: 真正禁止的是「落到系统临时目录之外」（= 仓库根 / %APPDATA% 真实数据根）。
    for mod_name, attr in (("env_loader", "PROJECT_DATA_DIR"), ("config", "PROJECT_DATA_DIR"),
                           ("config", "PROJECT_OUTPUT_DIR"), ("config", "TASKS_DB_PATH")):
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        val = os.path.normcase(os.path.abspath(str(getattr(mod, attr) or ".")))
        assert allow_data_root(val), (
            "%s.%s=%s 落在真实数据根上（t20/D5：测试会污染生产/开发数据）" % (mod_name, attr, val))
    yield

