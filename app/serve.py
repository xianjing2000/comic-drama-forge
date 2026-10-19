"""
生产级启动器：无人值守 24/7 场景使用真正的 WSGI 服务器，而不是 Flask 自带的开发服务器。

为什么用 waitress 而不是开发服务器：
    - Flask 自带的开发服务器官方明确「不得用于生产」，单线程/调试导向、无优雅超时、
      带自动重载器，长期挂机容易被中间层掐断或受代码变更干扰；
    - 本项目单集生产可能持续数十分钟（H3 视频生成 + 超分），需要稳定的长连接与
      可控的连接空闲超时；
    - waitress 为纯 Python、跨平台，Windows 上无需额外系统依赖，适合桌面挂机。

设计要点：
    - 默认 waitress；未安装时回退到 Flask 开发服务器并显著告警，不让服务起不来；
    - 单进程多线程：ComfyUI 侧已有全局串行队列，这里不需要多进程；
    - channel_timeout 放宽：单次生成任务动辄数十分钟，不能被中间层掐断连接。
    - 内置崩溃检测：Flask 崩溃后自动重启，无需手动介入。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import time
import signal
import types
import importlib.util

# 让脚本无论从哪个目录启动都能找到 app 包内的模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _reexec_in_utf8_mode() -> None:
    """确保进程运行在 Python UTF-8 模式下；否则带 -X utf8 重新执行自己（最多一次）。

    ⭐⭐⭐ 2026-10-12 与 _force_utf8_stdio 配套，用于彻底封闭「中文目录」这一类问题。

    为什么光有 _force_utf8_stdio 还不够：它只改 stdio 的编码，**改不了**
    `open()` 的默认编码 —— Python 的「文件系统/IO 默认编码」由 UTF-8 模式决定，
    而源码里存在 38 处未显式传 encoding 的 open()（读配置、读剧本、写 json 等）。
    实测（真建中文目录）：
        PYTHONUTF8=0 → open(中文文件) 抛 UnicodeDecodeError: gbk codec ...
        PYTHONUTF8=1 → 同一句代码自动按 UTF-8 读，内容正确。
    也就是说：只要进程进到 UTF-8 模式，那 38 处**一处都不用改**。

    本系统有多个启动入口，环境变量注入情况不一：
        electron spawn      → main.js 已注入 PYTHONUTF8/PYTHONIOENCODING ✅
        计划任务 / run_serve.bat / 手工 python serve.py → 没有 ❌
    这里做一次自举把入口差异抹平：非 UTF-8 模式就 execv 自己一次。
    防重入用环境变量标记，且重入失败直接放行（还有 _force_utf8_stdio 兜底），
    绝不允许出现启动死循环。
    """
    if getattr(sys.flags, "utf8_mode", 0):
        return
    if os.environ.get("MJSCXT_UTF8_REEXEC") == "1":
        return  # 已重入过仍不是 utf8（例如宿主禁用了）→ 放弃，避免循环
    os.environ["MJSCXT_UTF8_REEXEC"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    try:
        os.execv(sys.executable, [sys.executable, "-X", "utf8"] + list(sys.argv))
    except Exception:  # noqa: BLE001 重入不可用（受限环境）→ 继续跑，靠 stdio 兜底
        pass


def _force_utf8_stdio() -> None:
    """把 stdio 强制成 UTF-8（幂等，尽早调用）。

    ⭐⭐⭐ 2026-10-12 修复「桌面版无法在中文目录运行」：
      Windows 上 Python 的 sys.stdout/stderr 默认编码跟随**系统 locale**
      （简体中文 = cp936/GBK）。安装目录、数据目录、日志内容只要含中文，
      任何一次 print / logging 就会抛 UnicodeEncodeError，进程随即退出 ——
      表现就是「装在中文路径下就打不开 / 一闪而过 / 后端起不来」。

    为什么光靠 main.js 传 PYTHONUTF8/PYTHONIOENCODING 还不够：
      · 计划任务、run_serve.bat、手工 `python serve.py` 等入口没有那两个变量；
      · 本文件后面还会把 sys.stdout 换成 _Tee（tee 到**原始** stdio），
        如果原始流仍是 GBK，tee 照样会炸。
      所以这里自己做一层兜底：把两个流 reconfigure 成 utf-8 + errors=replace，
      「宁可显示成问号，也不要因为一个中文路径把整个后端打死」。
    幂等：重复调用无副作用（reconfigure 可重复执行）。
    """
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        try:
            if _s is not None and hasattr(_s, "reconfigure"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 某些流（已重定向/被包裹）不支持
            pass


_reexec_in_utf8_mode()   # 先确保 UTF-8 模式（会 execv 重入一次）
_force_utf8_stdio()      # 再兜底 stdio 编码（重入失败时仍保中文不崩）


def _load_flask_app():
    """按文件路径稳健加载 app/app.py 里的 Flask 实例。

    背景（对应测试缺陷 D1）：
        本项目 app/ 目录与 app.py 同名，且 app/ 下没有 __init__.py（命名空间包）。
        于是 `from app import app` 的语义随启动方式而变：
          - `python app/serve.py`  → 拿到 Flask 实例（正确）
          - `python -m app.serve`  → 拿到的是子模块 app.app（错误！）
        错误时的表现：
          waitress 报 `TypeError: 'module' object is not callable`，
          waitress 缺失时的回退分支再报 `module 'app.app' has no attribute 'run'`，
          最终所有页面与 API 全部返回 500。
        这里改为显式按文件路径加载，彻底消除同名歧义。

    打包版（frozen）补充：单文件 EXE 里 app.py 不在磁盘上（收进 PYZ 归档），
    按文件路径加载必然 FileNotFoundError。PyInstaller `collect_submodules('app')`
    已把它登记为模块 `app.app`，故 frozen 时改用 `importlib.import_module` 从
    PYZ 取模块，同样先注册 `sys.modules["app"]` 供 app.py 自引用，逻辑与源码一致。
    """
    # ⚠️ 启动故障修复（2026-09-29）：`import importlib` 原先只写在下方 frozen 分支内，
    #   Python 会把 importlib 编译成**整个函数的局部变量** —— 非 frozen 路径走到
    #   `importlib.util.spec_from_file_location`（下方 L85）时必然 UnboundLocalError，
    #   源码模式（python app/serve.py / 计划任务）启动即崩（实测 LastTaskResult=1）。
    #   在函数入口统一绑定；模块顶部本就有 import importlib.util，frozen 分支内的
    #   重复 import 变成无害的重复绑定。
    import importlib
    if getattr(sys, "frozen", False):
        # PYZ 里已登记为 `app.app`（spec 的 collect_submodules('app')）。
        # 先试 `app.app`，再试 `app`；拿到后注册 sys.modules["app"] 供自引用。
        # 诊断信息全部打到日志（frozen 下已重定向到 exe 同级 logs/），不静默吞。
        import traceback
        _candidate = None
        for _name in ("app.app", "app"):
            try:
                _mod = importlib.import_module(_name)
            except Exception:
                print(f"[frozen] import {_name} 失败：\n{traceback.format_exc()}")
                continue
            _fa = getattr(_mod, "app", None)
            print(f"[frozen] {_name} 已导入，type=<{type(_mod).__name__}>，"
                  f"app 属性 type=<{type(_fa).__name__ if _fa is not None else 'None'}>")
            if _fa is not None and not isinstance(_fa, types.ModuleType):
                sys.modules.setdefault("app", _mod)
                return _fa
            _candidate = _mod
        if _candidate is not None:
            return _candidate
        # 诊断：列出 sys.modules 里所有含 app / serve 的键，定位真实登记名
        _hits = [k for k in sys.modules if "app" in k or "serve" in k]
        print(f"[frozen] sys.modules 中含 app/serve 的模块: {_hits[:40]}")
        raise RuntimeError(
            "frozen 模式下未找到 Flask 应用实例 `app`（已试 app.app / app，详见上方日志）")

    app_py = os.path.join(_HERE, "app.py")
    if not os.path.isfile(app_py):
        raise FileNotFoundError(f"未找到应用入口文件：{app_py}")

    spec = importlib.util.spec_from_file_location("app", app_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {app_py} 构建模块规格")

    module = importlib.util.module_from_spec(spec)
    # 先注册再执行：让 app.py 内部的 `import app` 自引用拿到同一个模块对象
    sys.modules["app"] = module
    spec.loader.exec_module(module)

    flask_app = getattr(module, "app", None)
    if flask_app is None or isinstance(flask_app, types.ModuleType):
        raise RuntimeError(
            "app.py 中未找到 Flask 应用实例 `app`"
            f"（实际拿到：{type(flask_app).__name__}）"
        )
    return flask_app


# =======================================================================
# 进程日志落盘（2026-10-05，二次修正：提前到 exec app.py 之前）
# =======================================================================
# 背景：桌面版（Electron）spawn 的是**本文件**（python.exe app/serve.py，cwd=资源镜像），
#   而 main.py 的 _redirect_frozen_logs() 在打包布局下**根本不会被调用** ——
#   main.py 在 resources/ 根、serve.py 在资源镜像里，spawn 的是后者。
#   读取侧 log_viewer._resolve_log_dir() 读的是
#   <MJSCXT_DATA_DIR>/logs/serve_stdout.log，故写入侧必须落到同一路径。
#
# ⚠️ 为什么必须**在顶层、且在 `app = _load_flask_app()` 之前**执行：
#   1) `app = _load_flask_app()` 会 exec 整份 app.py，import 期就会打出一批关键日志
#      （AI 前置自检不通过、密钥库初始化、script_generator 缺 key 等）。
#      若重定向发生在其后，这批日志只会进 Electron 管道，**永远不进文件**，
#      而前端「运行日志」页只读文件 → 用户看到的仍是空白页。
#   2) 放在第三方 import 之前，logging 的 handler 尚不存在，之后任何模块
#      basicConfig 时拿到的就是本文件流 —— 无需事后修补（下面的 re-point 仅作保险）。
#   本模块没有任何其他模块 import 它（serve.py 是入口），顶层副作用可控。
#   非桌面/非打包场景（MJSCXT_DATA_DIR 未设置且非 frozen）直接 return，行为不变。
#
# 两个必须遵守的坑（main.py 同款，实测踩过）：
#   1. 不要用 os.dup2：后台方式启动时控制台句柄失效 → 退出写日志 OSError(9)
#      → CPython 'lost sys.stderr' abort。只换 sys.stdout/sys.stderr 对象。
#   2. 日志文件句柄必须模块级持有：被 GC 回收会连带关掉正在写的流。
_LOG_FH = None


class _Tee:
    """把写入同时送给「原始流」和「日志文件」。

    为什么要 tee 而不是直接替换：
      · 日志文件侧是给前端「运行日志」页读的（log_viewer 按同一路径读）；
      · 原始流侧是 Electron 主进程的管道（main.js 的环形缓冲，菜单「后端 → 查看日志」用）。
    直接 ``sys.stdout = fh`` 会让其中一侧彻底失声（2026-10-05 用户反馈：
    桌面版日志页只有一行 '[serve] log redirected to: ...'，因为 logging 的
    StreamHandler 早在模块导入期就绑定了旧管道，替换 sys.stdout 改不到它）。

    实现约束（沿用 main.py 踩过的坑）：
      · 不用 os.dup2（后台启动时控制台句柄失效 → 退出期 OSError(9) → abort）；
      · 只做 Python 层的对象替换，写文件失败绝不抛（日志系统不能反过来搞挂服务）。
    """

    def __init__(self, primary, mirror):
        self._primary = primary
        self._mirror = mirror

    def write(self, data):
        n = 0
        for stream in (self._primary, self._mirror):
            if stream is None:
                continue
            try:
                n = stream.write(data)
            except Exception:  # noqa: BLE001 任一目标失败都不影响另一侧
                # 
                from degradation import note as _dg_note
                _dg_note('serve.py:161', 'noqa: BLE001 任一目标失败都不影响另一侧')
        return n

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        for stream in (self._primary, self._mirror):
            if stream is None:
                continue
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                # 
                from degradation import note as _dg_note
                _dg_note('serve.py:175', 'noqa: BLE001')

    def isatty(self):
        return False

    def fileno(self):
        # 让需要真实 fd 的调用方拿到文件句柄（不是管道）
        try:
            return self._mirror.fileno()
        except Exception:  # noqa: BLE001
            raise OSError('no fileno')


#: 日志轮转参数（2026-10-10 设计审查修复）。
#  背景：本项目是**无人值守 24/7 挂机**（本文件开头即写明），但日志此前是
#  直接 open(path, 'a') 追加写入、**全项目 0 处轮转配置** —— 单文件会无限增长。
#  实测 serve_stdout.log 已达 3.17MB 且持续增长；长期挂机足以吃满磁盘。
#  策略：单文件超过 MAX 就整体归档为 .1（旧的 .1 → .2 …），最多保留 KEEP 份，
#  最老的一份丢弃。总占用被限制在约 MAX × (KEEP+1)。
_LOG_MAX_BYTES = 32 * 1024 * 1024
_LOG_KEEP = 5


def _rotate_log(path: str, max_bytes: int = _LOG_MAX_BYTES, keep: int = _LOG_KEEP) -> bool:
    """按大小轮转日志文件。返回是否发生了轮转（只读判断，失败只告警不抛）。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < max_bytes:
            return False
        # 逆序顺延：先删最老的，再把 .N-1 → .N，最后把主文件 → .1
        oldest = '%s.%d' % (path, keep)
        if os.path.isfile(oldest):
            try:
                os.remove(oldest)
            except OSError:
                # 
                from degradation import note as _dg_note
                _dg_note('serve.py:209', '静默降级')
        for i in range(keep - 1, 0, -1):
            src = '%s.%d' % (path, i)
            if os.path.isfile(src):
                try:
                    os.replace(src, '%s.%d' % (path, i + 1))
                except OSError:
                    # 
                    from degradation import note as _dg_note
                    _dg_note('serve.py:216', '静默降级')
        os.replace(path, path + '.1')
        return True
    except Exception as e:  # noqa: BLE001  轮转失败绝不能影响启动
        logger.warning('日志轮转失败（忽略，继续追加写）：%s', e)
        return False


def _redirect_process_logs() -> None:
    """把本进程 stdout/stderr 追加写到日志文件（与 log_viewer 读侧同路径）"""
    global _LOG_FH
    # 幂等：本函数在模块导入期已先调用过一次（赶在 exec app.py 之前）。
    # __main__ 里那次调用保留作保险，故这里必须幂等，否则会开出第二个文件句柄。
    if _LOG_FH is not None:
        return
    try:
        _data_dir = (os.environ.get('MJSCXT_DATA_DIR') or '').strip()
        if _data_dir:
            log_dir = os.path.join(_data_dir, 'logs')
        elif getattr(sys, 'frozen', False):
            log_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'logs')
        else:
            return  # 源码/计划任务模式由 run_serve.bat 重定向，保持原行为
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'serve_stdout.log')
        # ⭐ 2026-10-10：写之前先按大小轮转（无人值守挂机，不能让它无限增长）。
        #   注意必须在 open(..., 'a') **之前** —— 打开后再改名会让句柄指向被移走的旧文件。
        if _rotate_log(log_path):
            logger.info('日志已轮转：%s → %s.1（单文件上限 %dMB，保留 %d 份）',
                        os.path.basename(log_path), os.path.basename(log_path),
                        _LOG_MAX_BYTES // (1024 * 1024), _LOG_KEEP)
        _orig = (sys.stdout, sys.stderr)
        fh = open(log_path, 'a', encoding='utf-8', errors='replace', buffering=1)
        try:
            fh.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except Exception:  # noqa: BLE001 某些流不支持 reconfigure
            # 
            from degradation import note as _dg_note
            _dg_note('serve.py:252', 'noqa: BLE001 某些流不支持 reconfigure')
        _LOG_FH = fh
        tee_out = _Tee(_orig[0], fh)
        tee_err = _Tee(_orig[1], fh)
        sys.stdout = tee_out
        sys.stderr = tee_err

        # ⭐ 关键一步：logging 的 StreamHandler 在本模块 import 期（被别的模块
        #   basicConfig 触发）就已经绑定了**旧的** sys.stderr（管道）。只替换
        #   sys.stdout/sys.stderr 不会改到已存在的 handler —— 那些日志会继续走管道，
        #   日志文件里一条不落（这正是「日志页只有 redirect 横幅」的真正原因）。
        #   这里把「指向旧流的 handler」改指到新的 tee 上，读/写两侧才真正对齐。
        try:
            _loggers = [logging.root]
            for _name in list(logging.root.manager.loggerDict.keys()):
                _loggers.append(logging.getLogger(_name))
            for _lg in _loggers:
                for _h in list(getattr(_lg, 'handlers', []) or []):
                    if not isinstance(_h, logging.StreamHandler):
                        continue
                    _stream = getattr(_h, 'stream', None)
                    if _stream is _orig[0]:
                        _h.setStream(tee_out)
                    elif _stream is _orig[1]:
                        _h.setStream(tee_err)
        except Exception as _e:  # noqa: BLE001 handler 重定向失败不影响服务启动
            sys.stderr.write(f'[serve] logging handler re-point failed (ignored): {_e}\n')

        def _restore():
            try:
                if _LOG_FH is not None:
                    try:
                        _LOG_FH.flush()
                    except Exception:  # noqa: BLE001
                        # 
                        from degradation import note as _dg_note
                        _dg_note('serve.py:286', 'noqa: BLE001')
                    sys.stdout, sys.stderr = _orig
                    _LOG_FH.close()
            except Exception:  # noqa: BLE001
                # 
                from degradation import note as _dg_note
                _dg_note('serve.py:290', 'noqa: BLE001')

        atexit.register(_restore)
        sys.stdout.write(f'[serve] log redirected to: {log_path}\n')
        sys.stdout.flush()
    except Exception as _e:  # noqa: BLE001 重定向失败绝不阻塞启动
        print(f'[serve] log redirect failed (ignored): {_e}')

# 导入期立即生效：必须在 `app = _load_flask_app()` 之前（见上方说明）。
_redirect_process_logs()


app = _load_flask_app()

logger = logging.getLogger("serve")

# 自动重启配置
MAX_RESTARTS = 10        # 最多连续重启次数
RESTART_COOLDOWN = 30    # 重启冷却时间（秒）
CHECK_INTERVAL = 10      # 健康检查间隔（秒）


def _env_int(key: str, default: int) -> int:
    try:
        return int((os.getenv(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


# P2-T4（F-02）：护栏实现抽到独立无副作用模块 host_guard.py —— serve.py 与
# app.py 直跑分支（`python app/app.py`）共用同一事实源。⚠️ 不能 import serve：
# serve 顶层 `app = _load_flask_app()` 会 exec 整份 app.py，若 app.py __main__
# 再 import serve 会触发顶层重跑、Flask 路由重复注册报错。
# 以下 4 个名字保留为再导出（main() 及既有 QA 探针均按 serve 命名空间引用）。
from host_guard import (        # noqa: E402
    _LOOPBACK_HOSTS, _ALLOW_NON_LOOPBACK_ENV,
    _is_loopback_host, _guard_host)



def _safe_run():
    """安全运行主服务，崩溃后返回 False"""
    try:
        # ⭐ 2026-10-10（用户要求「统一配置、别老是改一处漏一处」）：
        #    在 serve() 之前应用**统一配置中心** —— 取值优先级 数据库 > 环境变量 > 代码默认值，
        #    并把最终值 setattr 回各业务模块（现有代码零改动）。必须放在这里：
        #    此刻全部业务模块已 import，回写才能生效；放在更早的 import 期会因模块未加载而失败。
        #    同时跑一次配置体检，把「同一语义多处定义不一致」「提示词双副本不一致」直接打日志。
        try:
            import config_center as _cc
            import config_doctor as _cd
            _cc.ensure_table()
            # ⭐ 2026-10-10：统一 schema 初始化（幂等）。
            #   排查发现 tasks.db 的 4 张表由 3 个模块各自建表、没有版本号，
            #   且 tasks 缺 (project,status) 复合索引与 created_at 索引。
            #   ensure_schema 只补建缺失项并记录 schema_version，不动既有数据。
            try:
                import schema as _sc
                _sr = _sc.ensure_schema(_cc.db_path())
                if _sr.get('created_tables') or _sr.get('created_indexes'):
                    logger.info('schema 初始化：新建表 %s ｜ 新建索引 %s ｜ 版本 %s',
                                _sr.get('created_tables'), _sr.get('created_indexes'),
                                _sr.get('version'))
            except Exception as _se:  # noqa: BLE001
                logger.warning('schema 初始化失败（忽略）：%s', _se)
            _n = _cc.apply_to_runtime()
            logger.info(_cd.summary_line())
            _rep = _cd.doctor()
            if not _rep.get("ok"):
                for _w in (_rep.get("warnings") or [])[:10]:
                    logger.warning("[配置体检] %s", _w)
        except Exception as _e:  # noqa: BLE001  配置中心失败绝不让服务起不来
            logger.warning("配置中心初始化失败（忽略，退回代码默认值）：%s", _e)
        host = (os.getenv("APP_HOST") or "127.0.0.1").strip()
        port = _env_int("APP_PORT", 5210)
        threads = _env_int("APP_THREADS", 8)
        channel_timeout = _env_int("APP_CHANNEL_TIMEOUT", 1800)

        try:
            from waitress import serve
            logger.info("漫剧生成系统已启动（waitress）：http://%s:%d", host, port)
            logger.info("线程数 %d · 连接空闲超时 %ds", threads, channel_timeout)
            serve(app, host=host, port=port, threads=threads,
                  channel_timeout=channel_timeout, ident="mjscxt")
        except ImportError as e:
            logger.warning("=" * 68)
            logger.warning("未安装 waitress（%s），回退到 Flask 开发服务器。", e)
            logger.warning("开发服务器无法可靠处理文件上传，请执行：")
            logger.warning("    pip install waitress")
            logger.warning("=" * 68)
            app.run(host=host, port=port, threaded=True, use_reloader=False)
        return True
    except Exception as e:
        logger.error(f"服务崩溃: {e}", exc_info=True)
        return False


def _install_shutdown_hooks() -> None:
    """O16：优雅停机钩子。

    背景：`autopilot.stop()` 原本**全库无调用方**，`serve.py` 里 `import signal` 也未使用。
    于是 Ctrl+C 是「硬杀」——托管循环线程（daemon）被进程直接带死，可能留下：
      - SQLite 里的 `interrupted` 记录（当前步骤没被正常收尾）；
      - 正在写一半的 JSON / manifest（非原子写）。
    本钩子让进程退出**之前**先优雅停掉托管循环（步骤边界生效），再正常终止。

    实现（Python 官方推荐的「优雅停机 + 二次信号强制」模式）：
      - 首次收到 SIGINT/SIGTERM/SIGBREAK → 调 `autopilot.stop(timeout=15)` 给在飞步骤
        一个到边界停下的窗口，然后**恢复默认处理并重新抛出该信号**，让进程走常规终止
        流程（触发 atexit 兜底）；
      - `atexit.register(_atexit_stop)` 作为最后兜底：即使信号 handler 没跑到，
        进程正常/异常退出前也保证 `autopilot.stop()` 被执行一次（幂等，重复调用安全）。

    ⚠️ 与 S9 的「cancellation 检查点刻意不进 ComfyUI 渲染循环」不冲突：这里只停托管
    循环线程本身，不强行打断已在 GPU 上渲染的段（避免留半成品），交由各自的超时兜底。
    """
    import atexit
    import signal

    def _do_stop() -> None:
        try:
            import autopilot
        except Exception:  # noqa: BLE001  导入失败（如 app 尚未就绪）不应阻塞停机
            return
        try:
            autopilot.stop(timeout=15)
            logger.info("托管循环已优雅停止（步骤边界）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"优雅停止托管循环失败：{e}")

    def _atexit_stop() -> None:
        # 进程退出兜底；_do_stop 内部幂等（autopilot.stop 重复调用安全）
        _do_stop()

    def _sig_handler(signum, frame) -> None:
        logger.info(f"收到停机信号（{signum}），优雅停止托管循环后终止进程...")
        _do_stop()
        # 恢复默认处理，重新抛出同一信号 → 走常规终止（会再触发 atexit，幂等）
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    # 注册信号（Windows 上 SIGTERM/SIGBREAK 语义有限，能注册哪个就注册哪个）
    for _sig, _name in ((signal.SIGINT, "SIGINT"),
                        (getattr(signal, "SIGBREAK", signal.SIGINT), "SIGBREAK"),
                        (getattr(signal, "SIGTERM", None), "SIGTERM")):
        if _sig is None or _sig == signal.SIG_DFL:
            continue
        try:
            signal.signal(_sig, _sig_handler)
            logger.info(f"已注册优雅停机 handler：{_name}")
        except (ValueError, OSError, RuntimeError) as e:
            logger.debug(f"注册 {_name} handler 失败（{e}），跳过")

    atexit.register(_atexit_stop)
    logger.info("已注册 atexit 优雅停机兜底")


def _boot_qc_migration() -> None:
    """服务**真正启动时**执行一次「质检存量迁移」（幂等，见 app.boot_qc_migration）。

    ⚠️ 为什么放这里、而不是 app.py 模块级：app.py 模块级代码会在**任何** `import app`
    （守卫脚本 / 离线探针 / `python -c "import app"`）时执行，从而改写用户的真实
    qc_config.json —— 违反本项目「测试绝不读写用户数据」的纪律。serve.main() 是本服务
    的唯一启动入口（main.py 亦经 `import serve; serve.main()`），且**只在真正干活的进程
    里调用一次**（下面 main() 在重启循环之外调用，不随 _safe_run 重复触发）。

    通过 `sys.modules["app"]`（由 _load_flask_app 注册的 app.py 模块）取函数，避免
    `import app` 的命名歧义；取不到就静默跳过（如旧版模块）。失败只告警、不得阻断启动。
    """
    # D-1（2026-10-16）：直接 import 下沉后的实现，去掉 `sys.modules["app"]` 服务定位。
    try:
        from qc_helpers import boot_qc_migration
        boot_qc_migration()
    except Exception as e:  # noqa: BLE001  迁移失败不得阻断启动
        logger.warning(f"质检存量迁移调用失败（不影响启动）：{e}")


def _boot_comfyui_heartbeat() -> None:
    """服务**真正启动时**开启 ComfyUI 主动心跳（2026-10-09）。

    与 _boot_qc_migration 同一纪律：**只在真正干活的进程里调用一次**，
    绝不放 app.py 模块级 —— app.py 模块级代码会在任何 `import app`
    （守卫脚本 / 离线探针）时执行，那样会让守卫也去真探/真重启 ComfyUI。

    心跳本身 fail-open、daemon 线程、可用 MJSCXT_COMFYUI_HEARTBEAT=0 关闭。
    """
    try:
        # ⚠️ 2026-10-09 修正：**必须直接 import 模块**。
        #    app.py 里有条明确注释（app.py:56）：「本文件里 comfyui_client 这个名字是**实例**
        #    （comfyui_client = ComfyUIClient()）」。我第一版取 app 模块的 comfyui_client 属性
        #    → 拿到 ComfyUIClient **实例** → 它没有 start_comfyui_heartbeat（该函数是模块级的）
        #    → 启动日志出现「ComfyUI 心跳未启动」，且 /api/engine/state 全是 None。
        import comfyui_client as _hb  # noqa: PLC0415
        _fn = getattr(_hb, "start_comfyui_heartbeat", None)
        if callable(_fn):
            _fn()
        else:
            logger.warning("ComfyUI 心跳未启动：模块里没有 start_comfyui_heartbeat")
    except Exception as e:  # noqa: BLE001  心跳失败不得阻断启动
        logger.warning("ComfyUI 心跳启动失败（不影响启动）：%s", e)


#: 单实例锁的文件句柄 —— 必须一直持有到进程结束，OS 会在进程退出时自动释放锁。
#: 绝不能在拿到锁后关闭它（关掉就等于主动释放，第二个进程立刻就能进来）。
_LOCK_FH = None


def _acquire_single_backend_lock() -> bool:
    """同一数据根只允许**一个**后端进程存活。取不到锁就返回 False（调用方应退出）。

    ⭐⭐⭐ 2026-10-12 事故复盘（这是必须根治的那一个）：
      桌面版后端（5211）与我手工起的后端（5299）**同时存活**、共用同一个
      `MJSCXT_DATA_DIR`。两者都跑 pipeline，`task_lease` 会把对方的租约视为
      「过期」并回收，然后**自动把那集重跑一遍** —— 实测「逆天系统_全书#ep1」与
      「进境_整本小说#ep1」被反复重跑，两个后端还都往**同一个 serve_stdout.log**
      写日志，于是日志交织在一起，看起来就像「项目串了 / 又启动了别的项目」，
      实际是纯浪费 GPU（项目数据本身是隔离的，已核验）。

    为什么用 OS 文件锁而不是 PID 文件：
      · PID 文件在进程被强杀（任务管理器 / taskkill）时会残留，形成"僵尸锁"，
        下次启动会误判"已有后端在跑"而拒绝启动；
      · msvcrt/flock 的锁由**操作系统**在其进程终止时释放，不存在残留问题。

    逃生口：设 MJSCXT_ALLOW_MULTI_BACKEND=1 可显式允许多开（调试/数据迁移用）。
    fail-open：数据根未配置、或锁机制本身出错时一律放行，绝不因为锁把服务挡住。
    """
    global _LOCK_FH
    if (os.getenv("MJSCXT_ALLOW_MULTI_BACKEND") or "").strip() == "1":
        logger.info("MJSCXT_ALLOW_MULTI_BACKEND=1：已显式允许多个后端并行，跳过单实例锁")
        return True
    d = (os.getenv("MJSCXT_DATA_DIR") or "").strip()
    if not d:
        return True  # 无数据根概念（开发态）→ 不拦
    try:
        os.makedirs(d, exist_ok=True)
        lp = os.path.join(d, ".backend.lock")
        fh = open(lp, "a+", encoding="utf-8")
        if os.name == "nt":
            import msvcrt
            try:
                # 先保证文件至少 1 字节：Windows 的 locking 不能锁零长度区域。
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(" ")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
        _LOCK_FH = fh  # 持有到进程结束（勿手动 close）
        # 身份信息写**另一个文件**：2026-10-12 实测发现，在持有 msvcrt 锁的同一
        # 文件上再 seek/truncate/write 会被"锁区间"干扰，写进去的内容读出来是空的
        # → 提示里说不清"已有后端在哪个地址"。锁只管锁，身份单独落盘，互不干扰。
        try:
            with open(os.path.join(d, ".backend.owner.json"), "w", encoding="utf-8") as of:
                of.write(json.dumps({
                    "pid": os.getpid(),
                    "port": _env_int("APP_PORT", 5210),
                    "host": os.getenv("APP_HOST") or "127.0.0.1",
                    "data_dir": d,
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, ensure_ascii=False))
        except Exception:  # noqa: BLE001 身份信息写不进去不影响持锁
            pass
        return True
    except Exception as e:  # noqa: BLE001 锁机制自身故障 → fail-open
        logger.warning("单实例锁初始化失败（按放行处理）：%s", e)
        return True


def _describe_existing_backend() -> str:
    """读 .backend.lock，给出"已有后端在哪"，便于用户直接打开它而不是干等。"""
    d = (os.getenv("MJSCXT_DATA_DIR") or "").strip()
    if not d:
        return ""
    try:
        with open(os.path.join(d, ".backend.owner.json"), "r", encoding="utf-8") as fh:
            info = json.loads(fh.read().strip() or "{}")
        port = info.get("port")
        host = info.get("host") or "127.0.0.1"
        pid = info.get("pid")
        if port:
            return f"http://{host}:{port}（pid={pid}，启动于 {info.get('started_at') or '?'}）"
    except Exception:  # noqa: BLE001
        pass
    return ""


def main() -> int:
    restart_count = 0

    logger.info("漫剧生成系统启动器开始运行（含自动重启保护）")

    # F-02 护栏：非回环 APP_HOST 启动校验（默认拒绝，显式放行才继续）。放在重启循环
    # **之外**，避免被当作「崩溃」触发最多 MAX_RESTARTS 次无意义重启。
    try:
        _guard_host((os.getenv("APP_HOST") or "127.0.0.1").strip())
    except RuntimeError as e:
        logger.error("启动被安全护栏拦截（F-02）：%s", e)
        return 2

    # ⭐⭐⭐ 单实例锁（2026-10-12）：同一数据根只允许一个后端进程。
    # 必须在重启循环**之外**：锁是按进程持有的，循环里重复获取没有意义；
    # 也必须在 _boot_qc_migration / autopilot 之前 —— 那些动作会动生产数据/占 GPU，
    # 一旦已经确认"有后端在跑"，就不该再往前走一步。
    if not _acquire_single_backend_lock():
        _where = _describe_existing_backend()
        logger.error(
            "检测到同一数据根已有后端在运行%s。"
            "本进程退出 —— 两个后端共存会互相回收任务租约、把同一集反复重跑，纯浪费 GPU。"
            "（如需并行，请显式设置 MJSCXT_ALLOW_MULTI_BACKEND=1）",
            ("：" + _where) if _where else "")
        return 3

    # 质检存量迁移：**服务真正启动时**执行一次（幂等）。放在重启循环之外 —— 只在
    # 真正干活的进程里触发一次，不随 _safe_run 反复执行。⚠️ 绝不放模块级（见函数 docstring）。
    _boot_qc_migration()

    # ComfyUI 主动心跳（2026-10-09）：连续探测失败即主动重启引擎，
    # 不再等业务请求的长超时（见 comfyui_client.start_comfyui_heartbeat 的说明）。
    _boot_comfyui_heartbeat()

    _install_shutdown_hooks()

    while restart_count <= MAX_RESTARTS:
        logger.info("启动 Flask 服务... (尝试 #%d)", restart_count + 1)
        success = _safe_run()

        if success:
            # 正常退出（比如用户按下 Ctrl+C）
            logger.info("服务已正常停止")
            return 0

        # 发生崩溃，检查是否需要继续重启
        restart_count += 1
        if restart_count > MAX_RESTARTS:
            logger.error(f"已崩溃 {MAX_RESTARTS} 次，停止自动重启")
            return 1

        # 冷却期
        wait_time = min(RESTART_COOLDOWN * restart_count, 300)  # 最多等5分钟
        logger.warning(f"服务崩溃，{wait_time} 秒后自动重启...")
        time.sleep(wait_time)

    return 0


if __name__ == "__main__":
    _redirect_process_logs()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())

