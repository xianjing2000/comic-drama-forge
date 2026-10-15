# -*- coding: utf-8 -*-
"""TE MAN 3D导演台 · 服务端站位图渲染器（分镜生成的 ``<image1>`` 构图基准图）

## 它解决什么问题

分镜图提示词里「谁站在画面哪一侧、机位怎么摆、取景到哪」过去只是一行**文字**
（``te_3d_director.block_annotation``），模型听不听全看运气 —— 实测景别偏差是分镜
质检最大的一类驳回。本模块把 ``te_3d_director.build_render_plan`` 的结构化机位
**渲染成一张真图**，作为分镜生成的 ``<image1>`` 构图基准，让模型「照着摆」而不是
「照文字猜」。

## 为什么必须自己渲染

``TE_3D_Director`` 是 ComfyUI 的自定义节点（``.pyd``，无源码），它的截图能力
**只在浏览器里**：前端 ``te_3d_director.js`` 用 three.js 画 WebGL → ``canvas.toDataURL()``
→ POST 到 ``/te_3d_director/capture``。服务端没有渲染能力，官方工作流示例里
``TE_3D_Director`` 节点也全是零连线。所以这里**复刻它的机位数学**，用自己的渲染页
把同一份 ``scene_json`` 渲出来 —— 数学单一事实源仍是 ``te_3d_director.py``。

## 实现要点（踩过的坑，别绕回去）

1. **回传而不是截图**：无头浏览器 ``--screenshot`` 与 WebGL 合成存在时序竞态，
   实测五张里会截到空白。改为页面渲染完 ``fetch('/__te3d_capture')`` 把
   dataURL 回传给本模块起的本地静态服务 —— 时序确定、不受视口尺寸干扰。
2. **资产自举**：three.js（r160）+ ``Xbot.glb`` 从本机 ComfyUI 的 TE MAN 插件目录
   复制到 ``output/_te3d_render/`` 缓存（不入库，``output/`` 已 gitignore）。
   可用 ``MJSCXT_TE_MAN_JS_DIR`` 覆盖源目录（版本升级/换机器时用）。
3. **失败必须静默降级**：渲染不可用（没浏览器 / 没资产 / 超时 / 全黑）时一律返回
   ``None``，调用方回退到「只注入文字站位锚点」的旧行为 —— **绝不阻断分镜主链路**。
4. **按计划哈希缓存**：同一 shot 的计划确定性 → PNG 可直接复用，重跑不重渲。

对外接口：``available()`` / ``render_blocking(shot, ...)`` / ``render_blocking_from_plan(plan, ...)``。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 路径 / 常量
# --------------------------------------------------------------------------- #

_APP_DIR = os.path.dirname(os.path.abspath(__file__))

# 渲染页（我们手写维护的代码，由 app/static 提供）。
# ⚠️ 这里踩过两个坑，都属于「静默降级、没人发现」那一类：
#  1) 冻结态 __file__ = <_MEIPASS>/te_3d_render.pyc，_APP_DIR 就等于 _MEIPASS，
#     而静态资源按 spec 落在 <_MEIPASS>/app/static —— 原来只拼 "static/…"，
#     exe 里恒「渲染页缺失」，站位图悄悄降级成文字锚点；
#  2) 这个文件原本住在 app/static 下，而 vite.config.ts 是 outDir=../app/static
#     + emptyOutDir=true：**每次 vite build 都会清空该目录**，它不在构建图里、
#     也不是 public/ 的产物，于是被删过（见提交 bc6ab37：322 deletions）。
#     现已复制一份到 frontend/public/te_3d_render/，构建会把它带回来。
_PAGE_SRC_CANDIDATES = tuple(dict.fromkeys([
    os.path.join(_APP_DIR, "app", "static", "te_3d_render", "render.html"),  # 冻结态（spec: app/static → app/static）
    os.path.join(_APP_DIR, "static", "te_3d_render", "render.html"),        # 源码态（app/static/…）
    os.path.join(os.path.dirname(_APP_DIR), "frontend", "public", "te_3d_render", "render.html"),
]))


def _page_src() -> str:
    """渲染页路径：按候选顺序取第一个存在的（每次调用都重探，便于热修复）。"""
    for _c in _PAGE_SRC_CANDIDATES:
        if os.path.isfile(_c):
            return _c
    return _PAGE_SRC_CANDIDATES[0]

#: 本机 ComfyUI 的 TE MAN 插件 web 目录（按顺序探测，第一个存在的胜出）
_TE_MAN_JS_CANDIDATES = (
    r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\custom_nodes\TE_MAN\web\js",
    r"D:\ComfyUI_portable_TE_v260619\ComfyUI\custom_nodes\TE_MAN\web\js",
    os.path.join(os.environ.get("COMFYUI_DIR", ""), "custom_nodes", "TE_MAN", "web", "js"),
)

#: 无头浏览器候选（Edge 是 Windows 预置的，优先用它可以省掉 Playwright 下载）
_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: 需要从 TE MAN 插件复制过来的渲染资产：(相对 js 目录, 目标相对路径)
_ASSET_FILES = (
    ("vendor/three/build/three.module.js", "three/build/three.module.js"),
    ("vendor/three/loaders/GLTFLoader.js", "three/loaders/GLTFLoader.js"),
    ("vendor/three/utils/BufferGeometryUtils.js", "three/utils/BufferGeometryUtils.js"),
    ("assets/te_3d_director/Xbot.glb", "assets/Xbot.glb"),
)

CAPTURE_PATH = "/__te3d_capture"
# ⭐ 2026-10-12 性能实测后收紧（T3 性能画像）：原来 120/60 且失败还会重试一次，
#   实测「3D站位 mean 108.2s、p90 427.8s」，单镜最坏 240s → 46 镜最坏约 3h/集，
#   是本链路第三大瓶颈。收紧到 45/20：
#   · 首次 45s 仍覆盖「下 3MB GLB + 编译着色器」的冷启动（实测命中时通常 20-40s）；
#   · 后续 20s 对应已热身的渲染（实测 p50 在 10s 级）。
#   fail-open 保证安全：超时只是 `return None` → 本镜没有 COMPOSITION BASELINE 基准图，
#   build prompt 与预检会按「无基准图」分支继续，**不阻断生成**；
#   即最坏情况是「这一镜少一张构图基准图」，而不是「整集白等几分钟」。
#   需要退回旧值：改回 120.0 / 60.0 即可。
_FIRST_RENDER_TIMEOUT = 45.0      # 首次渲染（要下 3MB GLB + 编译着色器）
_RENDER_TIMEOUT = 20.0            # 后续渲染
_MIN_INK_RATIO = 0.004            # 非背景像素占比下限：低于此值判「空白渲染」

_lock = threading.Lock()
_assets_dir: Optional[str] = None          # 资产目录（None = 未初始化）
_assets_ready = False
_server: Optional["_AssetServer"] = None
_browser_path: Optional[str] = None
_browser_checked = False
_browser_disabled = False                  # 启动失败后不再反复重试（进程级熔断）
_render_done_once = False       # 首次渲染要额外等 GLB 下载 + 着色器编译
_profile_dir: str = ""              # 本次渲染的临时浏览器 profile（每次 _attempt 新建）                  # 首次渲染要额外等 GLB 下载 + 着色器编译


def default_root_dir() -> str:
    """渲染资产 / 浏览器的落点根目录：默认 ``output/``（已 gitignore，不入库）。

    为什么不用 `output/_te3d_render/` 之外的目录：three.js + Xbot.glb 约 4MB，
    属于**可再生的第三方资产**，放进仓库不合适；放 output 下可随时删、启动自动重建。
    """
    try:
        import config  # noqa: PLC0415
        d = getattr(config, "PROJECT_OUTPUT_DIR", None)
        if d:
            return str(d)
    except Exception:  # noqa: BLE001 - 独立测试时无 config
        pass
    return os.path.join(os.path.dirname(_APP_DIR), "output")


# --------------------------------------------------------------------------- #
# 资产自举
# --------------------------------------------------------------------------- #

def _te_man_js_dir() -> Optional[str]:
    env = (os.environ.get("MJSCXT_TE_MAN_JS_DIR") or "").strip()
    if env and os.path.isdir(env):
        return env
    for cand in _TE_MAN_JS_CANDIDATES:
        if cand and os.path.isdir(cand):
            return cand
    return None


def _browser_exe() -> Optional[str]:
    env = (os.environ.get("MJSCXT_HEADLESS_BROWSER") or "").strip()
    if env and os.path.isfile(env):
        return env
    for cand in _BROWSER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def ensure_assets(root_dir: str) -> Optional[str]:
    """把渲染页与 three.js / Xbot.glb 备齐到 ``<root_dir>/_te3d_render``，返回该目录。

    返回 ``None`` 表示资产不可用（调用方走降级）。幂等：文件已存在且大小一致就跳过。
    """
    global _assets_dir, _assets_ready
    with _lock:
        if _assets_ready and _assets_dir:
            return _assets_dir
        dst = os.path.join(root_dir, "_te3d_render")
        try:
            os.makedirs(os.path.join(dst, "three", "build"), exist_ok=True)
            os.makedirs(os.path.join(dst, "three", "loaders"), exist_ok=True)
            os.makedirs(os.path.join(dst, "three", "utils"), exist_ok=True)
            os.makedirs(os.path.join(dst, "assets"), exist_ok=True)
            # 渲染页永远以 app/static 里的版本为准（我们自己维护的代码）
            _src_page = _page_src()
            if os.path.isfile(_src_page):
                _copy_if_newer(_src_page, os.path.join(dst, "render.html"))
            else:
                logger.warning("[3D站位图] 渲染页缺失（已尝试）：%s", " | ".join(_PAGE_SRC_CANDIDATES))
                return None
            src = _te_man_js_dir()
            if not src:
                logger.warning("[3D站位图] 未找到 TE MAN 插件 web 目录（可用 "
                               "MJSCXT_TE_MAN_JS_DIR 指定），跳过渲染资产复制")
                return None
            for rel, target in _ASSET_FILES:
                s = os.path.join(src, *rel.split("/"))
                d = os.path.join(dst, *target.split("/"))
                if not os.path.isfile(s):
                    logger.warning("[3D站位图] 渲染资产缺失：%s", s)
                    return None
                _copy_if_newer(s, d)
        except OSError as e:
            logger.warning("[3D站位图] 渲染资产准备失败：%s", e)
            return None
        _assets_dir = dst
        _assets_ready = True
        return dst


def _copy_if_newer(src: str, dst: str) -> None:
    """按 (大小, mtime) 判断是否需要复制，避免每次启动都搬 3MB。"""
    try:
        ss = os.stat(src)
        if os.path.isfile(dst):
            ds = os.stat(dst)
            if ds.st_size == ss.st_size and int(ds.st_mtime) >= int(ss.st_mtime):
                return
        shutil.copy2(src, dst)
    except OSError as e:
        logger.warning("[3D站位图] 复制资产失败 %s → %s：%s", src, dst, e)
        raise


def available(root_dir: str = None) -> bool:
    """渲染能力是否可用（资产齐全 + 有浏览器）。只做探测，不渲染。"""
    global _browser_path, _browser_checked
    if _browser_disabled:
        return False
    if not _browser_checked:
        _browser_path = _browser_exe()
        _browser_checked = True
        if not _browser_path:
            logger.warning("[3D站位图] 未找到无头浏览器（Edge/Chrome），"
                           "站位基准图不可用，回退文字站位锚点")
    if not _browser_path:
        return False
    if root_dir is None:
        return True
    return ensure_assets(root_dir) is not None


# --------------------------------------------------------------------------- #
# 本地静态服务（服务渲染页 + 接收回传）
# --------------------------------------------------------------------------- #

class _CaptureInbox:
    """一次渲染的回传信箱：等 POST 到达或超时。"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.png: Optional[bytes] = None
        self.diag: Optional[dict] = None
        self.error: str = ""

    def post(self, body: bytes) -> None:
        try:
            obj = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.error = "回传体不是合法 JSON"
            self.event.set()
            return
        if not isinstance(obj, dict):
            self.error = "回传体不是对象"
            self.event.set()
            return
        err = obj.get("error")
        if err:
            self.error = str(err)[:400]
            self.event.set()
            return
        png = obj.get("png")
        if not isinstance(png, str):
            self.error = "回传体缺少 png 字段"
            self.event.set()
            return
        try:
            raw = base64.b64decode(png.split(",", 1)[-1])
        except (binascii.Error, ValueError) as e:
            self.error = "PNG base64 解码失败：%s" % e
            self.event.set()
            return
        self.png = raw
        self.diag = obj.get("diag") if isinstance(obj.get("diag"), dict) else None
        self.event.set()


class _AssetServer(ThreadingHTTPServer):
    """127.0.0.1 上的临时静态服务，额外接受渲染结果回传。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, root: str):
        super().__init__(addr, handler)
        self.root = root
        self.inbox: Optional[_CaptureInbox] = None

    def handle_error(self, request, client_address):
        """静音客户端中断（浏览器被 terminate 时必然触发 ConnectionResetError）。

        默认实现会把整段 traceback 打到 stderr，在 24 小时托管里会淹没真正的告警。
        """
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
                            TimeoutError)):
            return
        logger.debug("[3D站位图] 本地服务请求异常：%r", exc)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # 静音：aiohttp 风格的一行日志会淹没应用日志
        return

    def _send(self, code: int, body: bytes, ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):        # noqa: N802 - BaseHTTPRequestHandler 契约
        path = self.path.split("?", 1)[0]
        if path in ("/", ""):
            path = "/render.html"
        # 防目录穿越：只允许 root 下的普通文件
        rel = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        if rel.startswith("..") or os.path.isabs(rel):
            self._send(403, b"forbidden")
            return
        full = os.path.join(self.server.root, *rel.split("/"))   # type: ignore[attr-defined]
        if not os.path.isfile(full):
            self._send(404, b"not found")
            return
        ctype = "application/octet-stream"
        low = rel.lower()
        if low.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif low.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif low.endswith(".json"):
            ctype = "application/json; charset=utf-8"
        elif low.endswith(".glb"):
            ctype = "model/gltf-binary"
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send(500, ("read failed: %s" % e).encode("utf-8"))
            return
        self._send(200, data, ctype)

    def do_POST(self):       # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
        except (ValueError, OSError):
            self._send(400, b"bad request")
            return
        if path != CAPTURE_PATH:
            self._send(404, b"not found")
            return
        inbox = getattr(self.server, "inbox", None)
        if inbox is None:
            self._send(409, b"no pending render")
            return
        inbox.post(body)
        self._send(200, b'{"ok":true}', "application/json")


def _ensure_server(root: str) -> "_AssetServer":
    """起（或复用）本地静态服务。端口由系统分配，避免撞端口。"""
    global _server
    with _lock:
        if _server is not None and _server.root == root:
            return _server
        srv = _AssetServer(("127.0.0.1", 0), _Handler, root)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, name="te3d-render-http", daemon=True)
        t.start()
        logger.info("[3D站位图] 本地渲染服务已启动：http://127.0.0.1:%d（root=%s）", port, root)
        _server = srv
        return srv


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

def plan_cache_key(plan: dict) -> str:
    """计划 → 稳定哈希（决定 PNG 缓存文件名）。"""
    blob = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def _ink_ratio(png: bytes) -> Optional[float]:
    """非背景像素占比（判断「渲染出来是不是一张全黑图」）。

    返回 ``None`` 表示无法判定（PIL 不可用 / 图损坏）—— 此时**放行**，宁可留一张
    可疑图也不要因为校验工具本身缺失而让整条链路失去构图基准。
    """
    try:
        import io

        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(io.BytesIO(png)) as im:
            im = im.convert("L")
            hist = im.histogram()
        total = max(1, sum(hist))
        # 背景是 #060608（灰度≈6）：把 ≤12 的档位都算作背景
        bg = sum(hist[:13])
        return max(0.0, (total - bg) / total)
    except Exception as e:  # noqa: BLE001
        logger.debug("[3D站位图] 非空校验失败（忽略）：%s", e)
        return None


def render_blocking_from_plan(plan: dict, out_dir: str, timeout: float = None,
                              root_dir: str = None) -> Optional[str]:
    """把渲染计划渲染成 PNG，返回文件路径；任何失败返回 ``None``（绝不抛）。"""
    if _browser_disabled:
        return None
    if root_dir is None:
        root_dir = default_root_dir()
    assets = ensure_assets(root_dir)
    if not assets:
        return None
    if not available():
        return None

    key = plan_cache_key(plan)
    dst = os.path.join(out_dir, "te3d_blocking", f"{key}.png")
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        srv = _ensure_server(assets)
    except OSError as e:
        logger.warning("[3D站位图] 准备渲染环境失败（降级）：%s", e)
        return None

    payload_name = f"payload_{key}.json"
    payload_path = os.path.join(assets, payload_name)
    try:
        from fs_atomic import atomic_write_bytes, atomic_write_json  # noqa: PLC0415
    except ImportError:  # 独立测试（app 不在 sys.path）时降级
        atomic_write_bytes = None
        atomic_write_json = None

    inbox = _CaptureInbox()
    srv.inbox = inbox
    port = srv.server_address[1]
    url = (f"http://127.0.0.1:{port}/render.html"
           f"?p={payload_name}&post={CAPTURE_PATH}")
    env = dict(os.environ)
    env.setdefault("MJSCXT_TE3D_RENDER", "1")
    _cmd_base = [
        _browser_path,                      # type: ignore[list-item]
        "--headless=new",
        "--disable-gpu",
        "--enable-unsafe-swiftshader",      # SwiftShader 软件 WebGL（无独显/无头必需）
        "--no-sandbox",
        "--no-first-run",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--hide-scrollbars",
        # ⭐⭐ 2026-10-11 修复「3D 站位图必然超时、整条链路被迫降级」的根因：
        #   渲染页要**播 idle 动画到固定时刻**才拿自然站姿（见本页注释「坑3」：
        #   Xbot.glb 是 T-pose 绑定姿势，T-pose 当构图参考会误导生图模型）。
        #   而 --headless 的**虚拟时钟默认不推进**，页面就永远停在「等动画到点」
        #   那一步 → 既不渲染也不回传 → 服务端只能超时（实测 90s/180s/600s 全部超时）。
        #   加上虚拟时间预算后，页面在**6.7 秒**内完成并回传 292KB 有效 PNG
        #   （实测 diag：chars=1 tris=28276 camPos=[0,1.55,1.016] fov=36）。
        #   25000ms 是实测值：足够跑完 idle 动画 + 着色器编译，又不会拖长失败兜底。
        "--virtual-time-budget=25000",
        # ⭐ 2026-10-11：**这条才是真正的驱动器**。只加 virtual-time-budget 仍然超时
        #   （实测 60s/90s/180s/600s 全超时）；加上 --dump-dom 后浏览器会在页面
        #   跑完后主动 dump 并退出，POST 回传随即到达 —— 实测 6.7 秒拿到 292KB PNG。
        #   不加它，无头浏览器只保持打开空等，页面事件循环不被驱动。
        "--dump-dom",
        "--window-size=%d,%d" % (int(plan.get("width") or 768),
                                 int(plan.get("height") or 1365)),
        # ⭐⭐ 2026-10-11 修复「3D 站位图必然超时」（真正根因）：
        #   原来所有渲染共用 assets/_browser_profile。该目录一旦残留 Edge 的
        #   单实例锁（SingletonLock / SingletonCookie），新启动的 Edge 会认为
        #   「已有实例在跑」，把请求转交给那个**并不存在的实例**后自己瞬间退出 ——
        #   实测表现：进程 0.1 秒退出、服务器**一个 HTTP 请求都收不到**，
        #   于是每镜白等 90s×2 后降级（整条 3D 构图基准链路长期失效）。
        #   改为**每次渲染一个独立临时 profile**：实测 1.2 秒完成、回传 390KB，
        #   比复用还稳（临时目录不保存任何状态，不存在锁残留）。
    ]
    # ⚠️ --user-data-dir 与 url 不在此拼接（2026-10-12 t25 修复）：--user-data-dir 必须
    #    指向 _attempt 内每次新建的临时 profile（mkdtemp 之后）。若在此拼接，_profile_dir
    #    此刻仍是空串 / 上一轮已删路径 → 临时 profile 隔离失效、单实例锁残留回归。

    def _attempt(tag: str) -> Optional[bytes]:
        """跑一次浏览器渲染，返回 PNG 字节；失败返回 None（原因写日志）。"""
        global _browser_disabled, _profile_dir
        box = _CaptureInbox()
        srv.inbox = box
        # 每次独立临时 profile（复用固定 profile 会因单实例锁残留而静默失败，见上方 _cmd_base 注释）
        _profile_dir = tempfile.mkdtemp(prefix="te3d_prof_")
        # --user-data-dir 必须在 mkdtemp 之后拼接，指向本次临时目录（否则隔离失效）
        _cmd = list(_cmd_base) + ["--user-data-dir=" + _profile_dir, url]
        try:
            proc = subprocess.Popen(_cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env)
        except OSError as e:
            logger.warning("[3D站位图] 浏览器启动失败（%s）：%s", tag, e)
            if isinstance(e, FileNotFoundError):
                _browser_disabled = True       # 浏览器没了 → 熔断，别每镜都撞
            return None
        try:
            wait = float(timeout) if timeout else (
                _FIRST_RENDER_TIMEOUT if not _render_done_once else _RENDER_TIMEOUT)
            if not box.event.wait(wait):
                logger.warning("[3D站位图] 渲染超时（%s，%.0fs）：%s", tag, wait, url)
                return None
            if box.error:
                logger.warning("[3D站位图] 渲染页报错（%s）：%s", tag, box.error)
                return None
            png = box.png or b""
            if not png:
                logger.warning("[3D站位图] 渲染页未回传图像（%s）", tag)
                return None
            ratio = _ink_ratio(png)
            _skel = bool(((plan.get("scene") or {}).get("skeletonMode")))
            if ratio is not None and ratio < _MIN_INK_RATIO and not _skel:
                # ⚠️ 骨骼模式的图是「黑底 + 细彩线」，非背景占比天然很低，
                #    不能套用人偶图的空白判据（否则骨架图会被误判全黑而丢弃）。
                logger.warning("[3D站位图] 渲染结果近乎全黑（%s，非背景占比 %.4f）",
                               tag, ratio)
                return None
            # ⭐ 2026-10-13：连渲染诊断一起返回（人数 / 骨骼盒 / 机位 / 姿势应用数…）——
            #    基准图质检（app/te_3d_qc.py）靠它做确定性判据，见 read_render_diag。
            return png, box.diag
        finally:
            _terminate(proc)
            try:
                shutil.rmtree(_profile_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
            _profile_dir = ""
    try:
        # 全景/场景背板：把本地图内联成 dataUrl（渲染页只经 HTTP 取 payload，
        # 不再为场景图单开一条文件服务）
        try:
            _pano = ((plan.get("scene") or {}).get("scene") or {}).get("panorama")
            if isinstance(_pano, dict):
                _lp = str(_pano.get("localPath") or "")
                if _lp and os.path.isfile(_lp):
                    with open(_lp, "rb") as _pf:
                        _b64 = base64.b64encode(_pf.read()).decode("ascii")
                    _ext = os.path.splitext(_lp)[1].lower().lstrip(".") or "png"
                    _mime = "jpeg" if _ext in ("jpg", "jpeg") else _ext
                    _pano["dataUrl"] = "data:image/%s;base64,%s" % (_mime, _b64)
        except Exception as _pe2:  # noqa: BLE001
            logger.debug("[3D站位图] 全景图内联失败（忽略）：%s", _pe2)
        if atomic_write_json is not None:
            atomic_write_json(payload_path, plan)
        else:
            with open(payload_path, "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False)

        png, diag = _attempt("第1次") or (None, None)
        if png is None:
            # 冷启动偶发失败（首建浏览器 profile / 着色器编译超时）→ 重试一次再降级
            time.sleep(0.6)
            png, diag = _attempt("重试") or (None, None)
        if png is None:
            return None

        if atomic_write_bytes is not None:
            atomic_write_bytes(dst, png)
        else:
            with open(dst, "wb") as f:
                f.write(png)
        # 渲染诊断落 sidecar（同目录同 key）：质检与排障都靠它，缓存命中时也读得回
        try:
            _dp = _diag_path(dst)
            if diag is not None:
                if atomic_write_json is not None:
                    atomic_write_json(_dp, diag)
                else:
                    with open(_dp, "w", encoding="utf-8") as _f:
                        json.dump(diag, _f, ensure_ascii=False)
        except Exception as _de:  # noqa: BLE001
            logger.debug("[3D站位图] 诊断落盘失败（忽略）：%s", _de)
        _mark_render_done()
        logger.info("[3D站位图] 渲染完成：%s（%.0f KB）", dst, len(png) / 1024.0)
        return dst
    except OSError as e:
        logger.warning("[3D站位图] 渲染失败（降级）：%s", e)
        return None
    except Exception as e:  # noqa: BLE001 - 渲染是增强项，任何异常都不许冒泡
        logger.warning("[3D站位图] 渲染异常（降级）：%s: %s", type(e).__name__, e)
        return None
    finally:
        srv.inbox = None
        try:
            if os.path.isfile(payload_path):
                os.remove(payload_path)
        except OSError:
            pass


_render_done_once = False


def _mark_render_done() -> None:
    global _render_done_once
    _render_done_once = True


def _diag_path(png_path: str) -> str:
    """渲染诊断 sidecar 路径（与 PNG 同 key 同目录）。"""
    return os.path.splitext(str(png_path or ""))[0] + ".diag.json"


def read_render_diag(png_path: str) -> Optional[dict]:
    """读回某张基准图的渲染诊断（人数 / 骨骼盒 / 机位 / 朝向 / 姿势应用数）。

    没有 sidecar（旧图 / 落盘失败）返回 None —— 调用方必须容忍 None（判据降级为
    只看像素统计）。
    """
    try:
        p = _diag_path(png_path)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _terminate(proc: subprocess.Popen) -> None:
    """结束浏览器进程**及其全部子进程**。

    ⚠️ 2026-10-14（两次修正，均由实测驱动）：
    1) 真根因：3D 渲染服务用的是 **Edge**（msedge.exe，见 _browser_exe()），
       它启动后会 fork 出 renderer / GPU / utility / crashpad 等多个子进程；
       而 terminate() / kill() **只结束主进程**，子进程会变成孤儿继续存活并占用
       GPU 与内存 —— 实测一晚累积出 9~14 个残留进程，让基准图渲染从 1.4s 退化到 47~89s。
    2) **顺序很关键**（实测踩坑）：若先 terminate() 让父进程退出，子进程会被系统
       重新挂载，此时 taskkill /T 已经**追溯不到**它们（实测：仍残留 9 个）。
       因此必须**在父进程还活着时先按进程树杀**，再回退到 terminate/kill。
    3) taskkill 只按 **PID** 精确指定（/T 作用于该进程树），绝不按进程名匹配，
       因此不会影响用户自己打开的浏览器。
    """
    if proc is None:
        return
    _pid = getattr(proc, "pid", None)
    # ① 先杀进程树（父进程还在，/T 才能追溯全部子进程）
    if _pid and os.name == "nt" and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(_pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.debug("[3D站位图] 清理浏览器进程树失败（回退 terminate）：%s", e)
    # ② 回退：进程树清理未成功时，逐级 terminate -> kill
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("[3D站位图] 结束浏览器主进程失败（忽略）：%s", e)


def render_blocking(shot: dict, out_dir: str, aspect: str = "9:16", width: int = 768,
                    root_dir: str = None, timeout: float = None,
                    target_size=None) -> Optional[str]:
    """按镜头渲染 3D 站位基准图，返回 PNG 路径；不可用/失败返回 ``None``。

    调用方必须容忍 ``None``（回退文字站位锚点），本函数不抛任何异常。

    ``target_size``：期望的**输出像素画幅** ``(W, H)``（如分镜的 ``1216×672``）。
    ⭐ 传它就不用再传 ``aspect`` 字符串 —— 裸字符串在本仓有**双语义**
    （``style_kit`` 的 ``(16,9)``＝横屏，而本模块 ``"16:9"`` 会算成 684 高），
    按像素对齐才能保证基准图与分镜目标画幅**逐像素一致**（见 ``fit_pixel_size``）。
    """
    try:
        import te_3d_director  # noqa: PLC0415
    except ImportError as e:
        logger.warning("[3D站位图] 无法导入 te_3d_director（降级）：%s", e)
        return None
    try:
        plan = te_3d_director.build_render_plan(shot, aspect=aspect, width=width,
                                               target_size=target_size)
    except Exception as e:  # noqa: BLE001
        logger.warning("[3D站位图] 生成渲染计划失败（降级）：%s: %s", type(e).__name__, e)
        return None
    # 该不该出这张图，由 build_render_plan 的 fits 判据给结论（三条理由见其 docstring：
    # 空舞台 / 本镜未声明景别 / 人数超过该景别横向容量）。不 fits 就**直接跳过**，
    # 回退到文字站位锚点 —— 硬凑出来的一张图（空黑底 / 篡改景别 / 人数数不清）
    # 比没有基准图更坏，而且还要白烧 2 次浏览器启动。
    if not plan.get("fits", True) or not _has_character(plan):
        logger.info("[3D站位图] 本镜不适合出构图基准图（跳过，回退文字站位锚点）："
                    "shot=%s 景别=%s 出场 %s 人 / 容量 %s",
                    shot.get("shot_id"), plan.get("cam_key") or "未指定",
                    plan.get("char_count"), plan.get("capacity"))
        return None
    return render_blocking_from_plan(plan, out_dir, timeout=timeout, root_dir=root_dir)


#: 九宫格「9 个候选构图」→ 逐格渲染 3D 站位基准图的景别/机位对照（2026-10-03）。
#: 键序即九宫格格序（1-9，与 build_shot_grid_candidates_prompt 的 GRID LAYOUT 变体一致）：
#:   (1) wide establishing  (2) medium front  (3) close-up face  (4) low angle
#:   (5) high angle        (6) over-shoulder (7) side profile  (8) extreme close-up
#:   (9) dutch-tilt dramatic
#: cam_key 取 config.SHOT_TYPES 景别（_FRAMING_SPAN 键），cam_angle 取 te_3d_director._ANGLE_SHOT 键。

#: 九宫格「单镜 9 关键帧」→ 逐格渲染用的机位角度序列（2026-10-15 用户口径）。
#: 与 GRID_BLOCKING_CELLS（9 个**不同景别**的候选构图）不同：本表**景别一律取本镜自身**，
#: 只用机位角度区分 9 格 —— 因为输出九宫格是「同一镜头随时间的 9 个关键帧」，不是 9 个景别；
#: 锁定本镜景别还能保证 fits 判据必然通过（本镜人数必然适配本镜景别），
#: 否则特写/大特写会因「人数超横向容量」被跳过，9 格只剩 1 格（实测）。


def render_blocking_variant(shot, cam_key, cam_angle,
                            out_dir, target_size=None,
                            root_dir=None, timeout=None, ignore_fit=False):
    """按**指定景别/机位**渲染某格候选构图的 3D 站位基准图（九宫格逐格构图基准用）。

    与 :func:`render_blocking` 的区别：`render_blocking` 的取景从 `shot.camera`
    推导（单镜用）；本函数用调用方给的 `cam_key`/`cam_angle` **覆盖**取景，
    渲出「这个候选构图该有的站位/机位」。不可用/失败一律返回 `None`（fail-open），
    绝不阻断九宫格主链路。
    """
    if not (cam_key and cam_angle):
        return None
    try:
        import te_3d_director  # noqa: PLC0415
        plan = te_3d_director.build_render_plan(
            shot, aspect="9:16", width=768, target_size=target_size,
            cam_key=cam_key, cam_angle=cam_angle)
    except Exception as e:  # noqa: BLE001
        logger.warning("[3D站位图] 逐格渲染计划失败（降级）：%s: %s", type(e).__name__, e)
        return None
    # 同 render_blocking 的 fits 判据（空舞台/未指定取景/人数超容量 → 跳过）。
    # ⭐ ignore_fit=True（2026-10-15）：关键帧九宫格场景用 —— 那是**同一镜头的机位预演**，
    #    9 格共用本镜景别，不需要按「该景别能容几个人」逐格卡（否则 3 人镜头的
    #    中近景/近景/特写会被全数跳过，实测 shot=2 中近景 9 格全灭）。
    if not ignore_fit and (not plan.get("fits", True) or not _has_character(plan)):
        return None
    if not _has_character(plan):
        return None
    return render_blocking_from_plan(plan, out_dir, timeout=timeout, root_dir=root_dir)









def _has_character(plan: dict) -> bool:
    """渲染计划里是否真有 character entity（``fits`` 之外的兜底：没有实体就是全黑底）。"""
    scene = (plan or {}).get("scene") or {}
    for e in (scene.get("entities") or []):
        if isinstance(e, dict) and e.get("type") == "character":
            return True
    return False


def shutdown() -> None:
    """关闭本地渲染服务（测试/退出时用）。"""
    global _server
    with _lock:
        srv, _server = _server, None
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception as e:  # noqa: BLE001
            logger.debug("[3D站位图] 关闭渲染服务失败（忽略）：%s", e)


def _cli(argv: List[str]) -> int:      # pragma: no cover - 手工诊断入口
    """``python te_3d_render.py <shot_json_file> <out_dir> [aspect]``：单张渲染诊断。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(argv) < 3:
        print(__doc__)
        return 2
    with open(argv[1], "r", encoding="utf-8") as f:
        shot = json.load(f)
    out_dir = argv[2]
    aspect = argv[3] if len(argv) > 3 else "9:16"
    p = render_blocking(shot, out_dir, aspect=aspect, root_dir=tempfile.gettempdir())
    print("RESULT:", p)
    return 0 if p else 1


if __name__ == "__main__":             # pragma: no cover
    import sys

    sys.exit(_cli(sys.argv))
