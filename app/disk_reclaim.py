# -*- coding: utf-8 -*-
"""D-11a（P2）ComfyUI 输出目录滚动回收 —— 纯判定逻辑（零第三方依赖，可离线单测）

对应缺陷
--------
全项目缺陷审计报告 **D-11**（原报告 C-5「ComfyUI 输出目录回收」）：
`COMFYUI_OUTPUT_DIR`（见 ``app/config.py:65``，由环境变量决定）下的重试产物
**只增不减** —— 历史事故：暂存区膨胀到 1.1GB、另有一次 80GB 写满 C 盘。
现有清理函数 ``app/app.py _qc_prune_attempts`` 只覆盖
``output/qc/<项目>/{assets,storyboard,video}_scratch/`` 下的 ``*_tryN``，
**作用域不含 ComfyUI 的 output 目录**。

本模块把「滚动清理」推广到 ``COMFYUI_OUTPUT_DIR`` 内、由 ``filename_prefix``
决定的产物（``comic_drama/…`` / ``comic_drama_sb/…`` / ``comic_drama_retry/…`` /
``comic_drama_kf/…``）。

⚠️ 命名判定的坑（首版踩过，务必别再踩回去）
----------------------------------------
ComfyUI 的 ``SaveImage`` 把 ``filename_prefix`` **逐字**当路径，再在**末段**
追加自动编号后缀 ``_{counter:05}_``。所以 ``filename_prefix="comic_drama_retry/proj_shot_01"``
产出的文件名是 ``proj_shot_01_00001_.png`` —— **不含 ``_retry``**！
首版把「重试产物」判定写成「stem 含 ``_retry``/``_try``」，结果六个生产 prefix 里
只有 ``app.py`` 的单镜重跑 API（``..._shot_NN_retry``）能命中，而**真实残留**
（批量分镜 / 资产 / 视频 / 整集留下的 ``{proj}_shot_01_00001_.png``）一个都收不到
—— 回收在真实场景近乎无效（独立复核实测：3 轮增量比 100%，而非 0%）。

因此判定改用「**ComfyUI 自动编号后缀**」：交付件名（``base.png`` / ``shot_01.png`` /
``ep01_final.mp4``）永不携带 ``_00001_`` 这类后缀，而 ComfyUI 侧产物**必然**携带。
``_retry`` / ``_try`` 仍作为并集条件保留（防御未来 prefix 写成别的形态）。

为什么这样判定是安全的
----------------------
这是**删文件**的代码，安全优先于回收率。只有**同时**满足以下全部条件才允许删：

1. 路径确实位于 ``COMFYUI_OUTPUT_DIR`` 之内（由调用方传入 ``comfy_root``），
   且落在 ``comic_drama*`` 产物目录内（不碰 ComfyUI 里别的工具/项目的产物）；
2. 文件名匹配「ComfyUI 产物」命名 —— 带自动编号后缀 ``_00001_``，或含
   ``_retry`` / ``_try``。手工放置的交付件名（``base.png``）天然不匹配；
3. ``mtime`` 距今 **> 24 小时**（阈值参数化 ``min_age_sec=86400``）——
   给「刚生成、正在被后续环节读取」的产物留足缓冲；
4. 在「正式产物目录」中能找到**同内容副本** —— 判定用 ``(size, sha256)``
   **双匹配**，绝不只看 size（同名不同内容的撞车会造成误删）；
5. ``os.stat().st_nlink == 1``：**硬链接直接跳过** —— 删了不释放空间，
   且说明它与正式产物共享同一 inode。本项目有过「把硬链接误判成重复副本」的
   教训（见仓库根 ``ComfyUI冗余资源扫描报告.md`` 的「⚠️ 一处修正」段：
   ``models/vae/MiniMax`` 与 ``models/vae/minimax-h3`` 是同一 inode 的硬链接，
   删任何一方都不释放空间）；
6. 候选文件**不在任何「正式产物目录」之内** —— 防御性断言，
   宁可漏删（少回收一点空间）也绝不误删正式交付物。
   ⚠️ 条件 6 的比较必须 **``normcase`` 之后再比**：Windows 路径大小写不敏感，
   若 ``COMFYUI_OUTPUT_DIR`` 与某个正式目录指向同一路径但写法大小写不同
   （``...\\assets`` vs ``...\\Assets``，misconfig 即可触发），不 normcase 会让
   「在里面」被判成「不在里面」，这道防线直接被击穿（首版实测可删正式目录内文件）。

哪些情况会被跳过
----------------
- ``skip["recent"]``    ：``mtime`` 距今 ≤ 阈值（默认 24h），太新不动；
- ``skip["no_copy"]``   ：正式产物目录里找不到 ``(size, sha256)`` 完全一致的副本；
- ``skip["hardlink"]``  ：``st_nlink != 1``（硬链接 / 别名，删了不释放空间）；
- ``skip["unreadable"]``：stat / 读取失败（被占用、权限不足、条目结构异常）。

清理绝不向上抛异常
------------------
回收是**优化**而非功能：:func:`reclaim_comfyui_output` 全程 try/except，
任何异常只经 ``logger.warning`` / ``logger.debug`` 留痕，绝不阻断生产。
（遵守 D-09 静默吞异常整治口径：不新增 ``except: pass``，异常一律绑定对象。）

运行环境
--------
仅依赖标准库（``hashlib`` / ``logging`` / ``os`` / ``time``），
测试环境无 flask / requests / PIL 也能被
``MJSCXT_AUTOPILOT=0 python verify_comfyui_reclaim.py`` 直接 import 跑通。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Optional

__all__ = [
    "DEFAULT_MIN_AGE_SEC",
    "RETRY_MARKERS",
    "ARTIFACT_DIR_PREFIX",
    "is_retry_artifact",
    "is_comfy_output_artifact",
    "is_reclaimable_candidate",
    "is_artifact_scope",
    "official_fingerprints",
    "plan_reclaim",
    "reclaim_comfyui_output",
]

#: 回收的最小「年龄」：``mtime`` 距今必须超过该秒数（默认 24 小时）。
DEFAULT_MIN_AGE_SEC = 86400

#: 重试产物的命名标记（**并集条件之一**，不再是唯一判据，理由见模块 docstring）。
RETRY_MARKERS = ("_retry", "_try")

#: ComfyUI ``SaveImage`` 追加的自动编号后缀，形如 ``..._00001_``。
#: 交付件名（``base.png`` / ``shot_01.png`` / ``ep01_final.mp4``）永不携带它，
#: 而 ComfyUI 侧产物必然携带 —— 这才是「真实残留」的可靠判据。
COMFY_COUNTER_RE = re.compile(r"_\d{5,}_$")

#: 产物目录名前缀。六个生产 ``filename_prefix`` 全部落在 ``comic_drama*`` 目录下：
#: ``comic_drama/``（资产/成片）、``comic_drama_sb/``（分镜）、
#: ``comic_drama_retry/``（整集重试）、``comic_drama_kf/``（关键帧）。
ARTIFACT_DIR_PREFIX = "comic_drama"

#: 计算内容指纹时的分块大小（1 MiB）—— 避免把大图整块读进内存。
_CHUNK = 1 << 20

#: ``plan_reclaim`` 的跳过计数键（返回值结构对调用方是稳定契约）。
_SKIP_KEYS = ("recent", "no_copy", "hardlink", "unreadable")

#: 正式产物指纹的进程内缓存，键为 ``(normcase 绝对路径, size, mtime_ns)``。
#: 为什么需要：正式产物是「交付物全集」，会随生产线性增长；每次回收都全量重算
#: sha256 等于把整个交付库重读一遍（实测 64MiB ≈ 0.15s，80GB 量级要数分钟），
#: 而回收是在任务收尾的**同步**路径上跑的。缓存让「内容未变」的文件不再重复读盘。
_FP_CACHE: dict = {}
_FP_CACHE_MAX = 200_000
#: 保护 _FP_CACHE 的锁（t43：此前无锁，并发回收时 clear/写会互相覆盖，虽只影响性能，
#: 但「交付库越大、回收越慢」的并发场景下值得加锁）。
_FP_CACHE_LOCK = threading.Lock()


def _sha256(path: str) -> str:
    """流式计算文件内容 sha256（调用方需自行捕获 ``OSError``）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def is_retry_artifact(name: str) -> bool:
    """文件名（stem）是否匹配「重试产物」命名（含 ``_retry`` 或 ``_try``）。

    ``is_retry_artifact("proj_shot_01_retry_00001_.png") -> True``
    ``is_retry_artifact("proj_shot_01_00001_.png")       -> False``
    ``is_retry_artifact("base.png")                      -> False``

    只看 stem（去掉扩展名），不看目录名 —— 目录名可能含 ``retry`` 等巧合词，
    正式交付目录里的文件不应据此被判定为临时产物。

    ⚠️ 这**不是**「ComfyUI 侧产物」的充分判据（真实残留名不含 ``_retry``），
    删除判定请用 :func:`is_reclaimable_candidate`。
    """
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    if not stem:
        return False
    return any(marker in stem for marker in RETRY_MARKERS)


def is_comfy_output_artifact(name: str) -> bool:
    """文件名是否长得像 **ComfyUI 侧产物**（带自动编号后缀 ``_00001_``）。

    ``is_comfy_output_artifact("proj_shot_01_00001_.png")        -> True``
    ``is_comfy_output_artifact("proj_shot_01_retry_00001_.png") -> True``
    ``is_comfy_output_artifact("base.png")                      -> False``
    ``is_comfy_output_artifact("shot_01.png")                   -> False``

    交付件名（``base.png`` / ``shot_NN.png`` / ``ep01_final.mp4``）都是**我们**
    起的固定名，永不带编号后缀；只有 ComfyUI 生成的产物才带 —— 这个判据覆盖
    全部六个生产 prefix，而 ``_retry`` 只能覆盖其中一个。
    """
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    if not stem:
        return False
    return bool(COMFY_COUNTER_RE.search(stem))


def is_reclaimable_candidate(name: str) -> bool:
    """文件名是否够格当回收候选（ComfyUI 自动编号产物 **或** 带重试标记）。"""
    return is_comfy_output_artifact(name) or is_retry_artifact(name)


def is_artifact_scope(path: str, comfy_root: str) -> bool:
    """``path`` 是否落在 ``comfy_root`` 下的 ``comic_drama*`` 产物目录内。

    作用域限定有两个理由：① 不碰 ComfyUI output 里别的工具/项目留下的东西；
    ② 把候选限制在「本项目产物」这一明确集合内，降低误删面。
    """
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(comfy_root))
    except (OSError, ValueError):
        return False
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if len(parts) < 2:                       # 必须是 comic_drama*/<文件>，散在根上的不算
        return False
    if any(p == ".." for p in parts):        # 在 comfy_root 之外
        return False
    return any(p.lower().startswith(ARTIFACT_DIR_PREFIX) for p in parts[:-1])


def _norm(path: str) -> str:
    """绝对路径 + ``normcase``（Windows 大小写不敏感，比较前必须归一）。"""
    return os.path.normcase(os.path.abspath(path))


def _is_within(path: str, roots) -> bool:
    """``path`` 是否位于 ``roots`` 中任一目录之内（防御性断言用）。

    ⚠️ 两侧都 ``normcase`` 后再比较：``os.path.commonpath`` 不做大小写归一，
    且返回值的大小写取自 ``paths[0]``；不归一的话
    ``...\\assets`` 与 ``...\\Assets`` 会被判成「不在里面」—— 防线直接失守。
    """
    try:
        target = _norm(path)
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            if os.path.commonpath([target, _norm(root)]) == _norm(root):
                return True
        except (OSError, ValueError):
            continue
    return False


def official_fingerprints(root_dirs) -> set:
    """扫描「正式产物目录」，返回 ``(size, sha256)`` 指纹集合。

    ``root_dirs`` 由调用方从 config 常量推导（如 ``CHARACTERS_DIR`` /
    ``ITEMS_DIR`` / ``SCENES_DIR`` / ``STORYBOARDS_DIR`` / ``KEYFRAMES_DIR`` /
    ``VIDEOS_DIR`` / ``FINAL_DIR``），**不硬编码任何盘符路径**。

    读不了 / stat 不了的文件直接跳过（不影响其它文件），
    因此本函数不会因个别坏文件而抛异常。

    性能：内容指纹按 ``(路径, size, mtime_ns)`` 缓存在进程内 —— 交付库随生产增长，
    没有缓存的话每次回收都要把整个交付库重读一遍（回收跑在任务收尾的同步路径上）。
    """
    fps: set = set()
    log = logging.getLogger(__name__)
    for root in (root_dirs or []):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                    size = int(st.st_size)
                    digest = _fingerprint_cached(p, size, st)
                    if digest is None:
                        continue
                    fps.add((size, digest))
                except OSError:
                    continue
                except Exception as e:  # noqa: BLE001 指纹扫描是旁路，坏文件不该中断整体
                    log.debug("D-11a 指纹扫描跳过异常文件 %s：%s: %s",
                              p, type(e).__name__, e)
                    continue
    return fps


def _fingerprint_cached(path: str, size: int, st) -> Optional[str]:
    """带缓存的 sha256：内容未变（size + mtime_ns 一致）就直接复用。

    两者同时相同却内容不同的概率可忽略（且这类文件的「同内容副本」判据只会
    更保守），换来的是「交付库越大、回收越慢」这条增长曲线被压平。
    """
    try:
        key = (_norm(path), int(size), int(getattr(st, "st_mtime_ns", 0)))
    except (OSError, ValueError, TypeError):
        return None
    with _FP_CACHE_LOCK:
        hit = _FP_CACHE.get(key)
        if hit is not None:
            return hit
    try:
        digest = _sha256(path)   # 耗时哈希在锁外执行，锁只护缓存读写
    except OSError:
        return None
    with _FP_CACHE_LOCK:
        if len(_FP_CACHE) >= _FP_CACHE_MAX:
            _FP_CACHE.clear()          # 简单的容量兜底：不做 LRU，直接整体失效
        _FP_CACHE[key] = digest
    return digest


def plan_reclaim(candidates, official_fps, now, min_age_sec: int = DEFAULT_MIN_AGE_SEC) -> dict:
    """**纯判定**：给定候选文件 stat 与正式产物指纹，决定删哪些、跳过哪些（不落盘）。

    ``candidates``：``[(path, st_size, st_mtime, st_nlink), ...]``
    —— 由调用方 stat 后传入，便于单测打桩（无需真实文件即可测判定分支）。

    ``official_fps``：:func:`official_fingerprints` 的结果（``(size, sha256)`` 集合）。

    ``now``：当前时间戳（秒，由调用方注入，测试可自由指定）。

    返回 ``{"delete": [path, ...],
            "skip": {"recent": n, "no_copy": n, "hardlink": n, "unreadable": n}}``
    —— ``delete`` 为「已确认可删」的路径列表（真正删除由调用方执行）。
    """
    skip = {k: 0 for k in _SKIP_KEYS}
    delete: list = []
    fps = official_fps or set()
    # 阈值在循环外求值一次；`None` / 非法值一律退回默认 24h ——
    # 首版 `float(None)` 抛 TypeError 会被外层 except 吞成「整批静默放弃」，
    # 表现为「回收一声不响地什么都不做」，极难排查。
    if min_age_sec is None:
        min_age_sec = DEFAULT_MIN_AGE_SEC
    try:
        threshold = float(min_age_sec)
    except (TypeError, ValueError):
        threshold = float(DEFAULT_MIN_AGE_SEC)
    for item in (candidates or []):
        try:
            path, size, mtime, nlink = item
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue

        # 条件 5：硬链接直接跳过 —— 删了不释放空间，且与正式产物共享同一 inode
        try:
            if int(nlink) != 1:
                skip["hardlink"] += 1
                continue
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue

        # 条件 3：mtime 距今必须 > 阈值（太新不动）
        try:
            age = float(now) - float(mtime)
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue
        if age <= threshold:
            skip["recent"] += 1
            continue

        # 空文件不做回收：0 字节文件彼此 sha256 相同，会互相成为「同内容副本」，
        # 删除毫无收益（不释放空间）却扩大了动作面 —— 直接跳过。
        try:
            if int(size) <= 0:
                skip["no_copy"] += 1
                continue
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue

        # 条件 4：正式产物目录里必须能找到 (size, sha256) 双匹配的同内容副本
        try:
            digest = _sha256(path)
        except OSError:
            skip["unreadable"] += 1
            continue
        except Exception as e:  # noqa: BLE001 读取异常一律按「不可读」跳过，保守不删
            logging.getLogger(__name__).debug(
                "D-11a 读候选失败，保守跳过 %s：%s: %s", path, type(e).__name__, e)
            skip["unreadable"] += 1
            continue
        if (int(size), digest) not in fps:
            skip["no_copy"] += 1
            continue

        delete.append(path)
    return {"delete": delete, "skip": skip}


def reclaim_comfyui_output(comfy_root: str, official_dirs, min_age_sec: int = DEFAULT_MIN_AGE_SEC,
                           logger=None) -> dict:
    """落地执行：扫描 ``comfy_root`` 下的重试残留并按 :func:`plan_reclaim` 删除。

    返回与 :func:`plan_reclaim` 同构的统计，另加：

    - ``removed_bytes``：实际删除的字节数（失败的不计）；
    - ``failed``       ：删除失败的个数（被占用 / 权限不足，逐个跳过不中断）；
    - ``scanned``      ：命中的候选文件数（重试命名且不在正式目录内）。

    **绝不向上抛异常**：任何异常只经 ``logger`` 留痕，函数始终返回统计字典。
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    stats = {
        "delete": [],
        "skip": {k: 0 for k in _SKIP_KEYS},
        "removed_bytes": 0,
        "failed": 0,
        "scanned": 0,
    }
    try:
        if not comfy_root or not os.path.isdir(comfy_root):
            return stats

        official_dirs = [d for d in (official_dirs or []) if d]
        official_abs = [os.path.abspath(d) for d in official_dirs]
        official_fps = official_fingerprints(official_dirs)
        now = time.time()

        candidates = []
        for dirpath, _dirnames, filenames in os.walk(comfy_root):
            # 条件 6：候选绝不允许落在任何「正式产物目录」之内（防御性断言）
            if _is_within(dirpath, official_abs):
                continue
            for fn in filenames:
                # 条件 2：必须是 ComfyUI 侧产物命名（自动编号后缀 / 重试标记）
                if not is_reclaimable_candidate(fn):
                    continue
                p = os.path.join(dirpath, fn)
                # 条件 1：限定在 comic_drama* 产物目录内（作用域收敛）
                if not is_artifact_scope(p, comfy_root):
                    continue
                if _is_within(p, official_abs):
                    continue
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                candidates.append(
                    (p, int(st.st_size), float(st.st_mtime), int(st.st_nlink)))

        stats["scanned"] = len(candidates)
        plan = plan_reclaim(candidates, official_fps, now, min_age_sec)
        stats["skip"] = plan["skip"]

        for p in plan["delete"]:
            try:
                size = os.path.getsize(p)
                os.remove(p)
                stats["delete"].append(p)
                stats["removed_bytes"] += size
            except Exception as e:  # noqa: BLE001 单文件失败绝不中断整批清理
                stats["failed"] += 1
                log.debug("D-11a 删除残留失败（跳过）：%s: %s: %s",
                          p, type(e).__name__, e)

        if stats["delete"]:
            log.info("D-11a ComfyUI 输出回收：删 %d/%d 个产物残留，释放 %.2f MB",
                     len(stats["delete"]), stats["scanned"],
                     stats["removed_bytes"] / 1048576.0)
    except Exception as e:  # noqa: BLE001 回收是优化，绝不能阻断生产
        log.warning("D-11a ComfyUI 输出回收失败（不影响生产）：%s: %s",
                    type(e).__name__, e)
    return stats
