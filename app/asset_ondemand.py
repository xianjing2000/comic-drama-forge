# -*- coding: utf-8 -*-
"""方案A：分镜链路的**角色资产按需生成**（2026-10-16）

动机（用户口径「没做方案A你跑什么测试」）：
  剧本声明了角色、但该角色资产目录里还没有 base.png 时，
  qc_helpers._allocate_storyboard_refs 原本只记录警告 + 该镜**没有身份锚点** 就 continue ——
  角色一致性从此失去保障。实测触发面很大（新登场角色尤其）。

做法：
  1. 分镜取图时若判定「资产缺失」→ 调用本模块的 ensure_character_asset；
  2. 用角色索引里的 ``appearance``（外貌锁定）当提示词，走**资产生成同一入口**
     （comfyui_client.generate_character_base，1:1 @1.5MP）；
  3. 落盘到 <资产目录>/base.png，随后**自动触发高清增强**产出 hd/front.png；
  4. **天然缓存**：图已存在直接复用；并发用 ``O_CREAT|O_EXCL`` 标记文件防重复生成。

⚠️ **物品不适用** —— 物品有独立的「固定参考图钉住」机制（asset_worker.pin_item_reference）。
⚠️ **fail-open**：任何失败都返回空串，分镜按「无该角色锚点」继续，绝不阻断出图。
⚠️ 服务端（视频/质检）不受影响：它们不调用本模块。
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

#: 生成中的标记文件后缀（防并发重复生成）
_LOCK_SUFFIX = ".ondemand.lock"

#: 标记文件超过该秒数视为陈旧（生成进程崩溃留下的），可被抢占
LOCK_STALE_SEC = 900


def _usable(path: str) -> bool:
    """文件存在且非空才算可用。"""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def pick_existing(entry: dict) -> str:
    """按优先级找 entry 里已有的可用图：hd/front → front → base → image。"""
    _dir = (entry or {}).get("_dir") or ""
    cands = []
    if _dir:
        # 高清增强产物优先（分镜用它最清晰）
        for _n in ("front.png", "side.png", "back.png"):
            cands.append(os.path.join(_dir, "hd", _n))
    for _k in ("front", "base", "image"):
        cands.append((entry or {}).get(_k) or "")
    if _dir:
        for _n in ("front.png", "base.png"):
            cands.append(os.path.join(_dir, _n))
    for c in cands:
        if _usable(c):
            return c
    return ""


def _acquire_lock(asset_dir: str) -> str:
    """抢占生成锁；返回锁文件路径，抢不到返回空串。"""
    lock = os.path.join(asset_dir, _LOCK_SUFFIX)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(int(time.time())).encode())
        os.close(fd)
        return lock
    except FileExistsError:
        # 陈旧锁（进程崩溃残留）→ 抢占
        try:
            if time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC:
                os.remove(lock)
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(int(time.time())).encode())
                os.close(fd)
                return lock
        except Exception:  # noqa: BLE001
            pass
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning("[按需角色] 抢占生成锁失败（跳过生成）：%s", e)
        return ""


def _release_lock(lock: str) -> None:
    try:
        if lock and os.path.exists(lock):
            os.remove(lock)
    except OSError:
        pass


def ensure_character_asset(entry: dict, project_name: str = "", style: str = "",
                           client=None, size=None) -> str:
    """角色资产缺失时**按需生成**并落盘，返回可用图路径（失败返回空串）。

    :param entry: 角色索引条目（需含 ``_dir`` / ``name`` / ``appearance``）
    :param size:  可选 (w, h)；默认按角色资产口径 1:1 @1.5MP
    """
    entry = entry or {}
    asset_dir = entry.get("_dir") or ""
    name = str(entry.get("name") or "").strip() or "未知角色"
    if not asset_dir:
        logger.warning("[按需角色] 「%s」没有资产目录，无法按需生成", name)
        return ""
    # 1) 已有可用图 → 直接复用（缓存命中）
    got = pick_existing(entry)
    if got:
        return got
    # 2) 抢锁（并发时只有一个线程真正生成）
    try:
        os.makedirs(asset_dir, exist_ok=True)
    except OSError as e:
        logger.warning("[按需角色] 无法创建目录 %s：%s", asset_dir, e)
        return ""
    lock = _acquire_lock(asset_dir)
    if not lock:
        logger.info("[按需角色] 「%s」已有其他线程在生成，本镜先按无锚点继续", name)
        return ""
    t0 = time.time()
    try:
        import comfyui_client
        import style_kit
        if client is None:
            client = comfyui_client.ComfyUIClient()
        if size is None:
            try:
                size = style_kit.aspect_size(style_kit.asset_aspect_ratio("character") or (1, 1),
                                            style_kit.asset_megapixels())
            except Exception:  # noqa: BLE001
                size = (1248, 1248)
        # 3) 提示词 = 外貌锁定；空则退回角色名
        appearance = str(entry.get("appearance") or "").strip()
        prompt = appearance or name
        logger.info("[按需角色] 资产缺失，开始生成：「%s」（%s，目标 %dx%d）",
                    name, prompt[:40], size[0], size[1])
        files = client.generate_character_base(prompt, style=style or "", size=size)
        src = ""
        for f in (files or []):
            if _usable(f):
                src = f
                break
        if not src:
            logger.warning("[按需角色] 「%s」生成无产物，本镜按无锚点继续", name)
            return ""
        # 4) 落盘到资产目录 base.png（与资产生成链路同约定）
        import shutil
        dst = os.path.join(asset_dir, "base.png")
        shutil.copy2(src, dst)
        logger.info("[按需角色] 「%s」已落盘：%s（耗时 %.1f 秒）",
                    name, dst, time.time() - t0)
        # 5) 顺手做高清增强（产出 hd/front.png），失败不影响返回
        try:
            import asset_enhance as _ae
            _r = _ae.enhance_character_sheet(asset_dir, client=client)
            logger.info("[按需角色] 「%s」高清增强：成功 %d / 失败 %d",
                        name, len(_r.get("produced") or []), len(_r.get("failed") or []))
        except Exception as _ee:  # noqa: BLE001
            logger.warning("[按需角色] 「%s」高清增强失败（不影响使用）：%s", name, _ee)
        # 6) 返回最清晰的一张
        return pick_existing({**entry, "base": dst}) or dst
    except Exception as e:  # noqa: BLE001
        logger.warning("[按需角色] 「%s」按需生成失败（本镜按无锚点继续）：%s: %s",
                       name, type(e).__name__, e)
        return ""
    finally:
        _release_lock(lock)