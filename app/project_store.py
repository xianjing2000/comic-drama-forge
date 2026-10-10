"""
项目级隔离（每部小说 = 一个独立项目）
=====================================

设计原则
--------
1. **物理隔离靠「项目键（dir_key）」**：既有链路已把产物按 `output/<kind>/<项目名>/`
   落盘（assets / storyboards / videos / final / upscale / dub / qc / scripts）。
   本模块把「项目键」固定沉淀在注册表里，所有下游链路一律使用该键，
   从而做到「一部小说一个项目，数据互不混用」。
2. **配置分离**：每个项目在 `output/projects/<项目ID>/` 下拥有独立配置文件
   （config.json / ai_settings.json / chat_history.json），互不覆盖。
3. **注册表可重建**：`index.json` 是项目列表的唯一事实来源，但即使丢失，
   也能通过 `migrate_legacy()` 从磁盘产物目录重新扫描恢复（不丢数据）。
4. **删除可恢复**：删除项目不物理删除，而是把项目工作区 + 各产物目录整体
   移入 `output/projects/_trash/<时间戳>_<项目键>/`，随时可手工还原。

本模块只对 `output/projects/` 及 `output/<kind>/<项目键>/` 读写；
不触碰系统目录，不修改 ComfyUI 侧文件。
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime

from config import (
    PROJECT_OUTPUT_DIR, SCRIPT_DIR, ASSETS_DIR, CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR,
    STORYBOARDS_DIR, VIDEOS_DIR, FINAL_DIR, UPSCALE_DIR, DUB_DIR, QC_DIR,
    KEYFRAMES_DIR, CONTINUITY_DIR, DUB_MIX_DIR, WATERMARK_DIR, H3_SFX_DIR,
    PROJECTS_DIR, PROJECT_INDEX_PATH, PROJECT_TRASH_DIR, PROJECT_MIGRATE_REPORT,
    PROJECT_DEFAULT_CONFIG, AI_CHAT_DIR, AI_SETTINGS_PATH, AI_CHAT_HISTORY_PATH,
    NOVELS_DIR, TASKS_DB_PATH,
    # 视频生成方式（项目级，新建项目时用户选择）：常量与归一同源，避免两处口径漂移
    VIDEO_MODES, norm_video_mode,
)

INDEX_VERSION = 1
KEY_MAX_LEN = 50

ASSET_KINDS = ("characters", "items", "scenes")

logger = logging.getLogger(__name__)


# =====================================================================
# 并发保护（审计 S8）
# ---------------------------------------------------------------------
# 本模块所有 CRUD 都是「load_index → 改内存 → save_index」的读改写，此前**零锁**
# （grep `Lock|RLock` 命中 0）。两个并发请求（例：新建项目 + 绑定剧本）各自读到同一份
# 旧快照，后写覆盖先写 —— 新建的项目凭空消失、episode_count 回退。
# 另外 `_write_json` 曾用**固定**的 `.tmp` 名：A 写了一半、B 以 "w" 截断重写，A 再
# `os.replace` → 发布出去的是**交错/截断的 JSON**；而 `load_index` 解析失败后
# **静默返回空索引** → 用户所有项目从列表里消失（数据仍在磁盘，界面却看不到，
# `get_project` 也找不到，托管 `enabled_projects()` 直接为空）。
# 用 RLock：`save_index` / `load_index` 自身也加锁，靠可重入避免同线程自锁。
# =====================================================================

_INDEX_LOCK = threading.RLock()


def _locked(fn):
    """把整个「读改写」放回同一把可重入锁的临界区里（函数级装饰，签名/文档不变）"""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _INDEX_LOCK:
            return fn(*args, **kwargs)
    return _wrapper


# =====================================================================
# 基础工具
# =====================================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_key(name: str) -> str:
    """项目名 → 项目键（与既有 _safe_project 规则一致，保证历史目录可直接命中）"""
    cleaned = re.sub(r"[《》〈〉【】「」『』]", "", str(name or "")).strip()
    key = "".join(c if c.isalnum() or c in "_-" else "_" for c in cleaned)
    key = key.strip("_") or "project"
    return key[:KEY_MAX_LEN]


def paths(key: str) -> dict:
    """项目键 → 该项目在各产物目录下的隔离路径 + 独立配置目录"""
    key = safe_key(key)
    root = os.path.join(PROJECTS_DIR, key)
    return {
        "key": key,
        "root": root,
        "config": os.path.join(root, "config.json"),
        "ai_settings": os.path.join(root, "ai_settings.json"),
        "chat_history": os.path.join(root, "chat_history.json"),
        "scripts": os.path.join(SCRIPT_DIR, key),
        "characters": os.path.join(CHARACTERS_DIR, key),
        "items": os.path.join(ITEMS_DIR, key),
        "scenes": os.path.join(SCENES_DIR, key),
        "storyboards": os.path.join(STORYBOARDS_DIR, key),
        "videos": os.path.join(VIDEOS_DIR, key),
        "final": os.path.join(FINAL_DIR, key),
        "upscale": os.path.join(UPSCALE_DIR, key),
        "dub": os.path.join(DUB_DIR, key),
        "qc": os.path.join(QC_DIR, key),
    }


def is_path_inside_output(path: str) -> bool:
    """P0-4：路径必须落在「项目输出目录（output/）」内才可信。

    口径与 app.py 的 `_project_or_400` / `_serve_safe` 一致：
    `abspath` 归一后前缀匹配 `root + os.sep`（Windows 下 `abspath` 两侧同时
    归一化大小写，直接比较即可）。空路径 / 相对路径越界 / `..` 穿越一律 False。
    index.json 的 `script_path` / `script_paths` 都该通过这个校验。
    """
    if not path or not str(path).strip():
        return False
    root = os.path.abspath(PROJECT_OUTPUT_DIR)
    p = os.path.abspath(str(path))
    return p.startswith(root + os.sep)


def _last_good_bak(path: str) -> str:
    """数据文件「上一份可解析好版本」备份路径：``path + '.bak'``。

    P1-2（A-12）：``_write_json`` 每次发布前先快照当前**可解析**的活文件到 ``.bak``，
    使 ``.bak`` 恒为「最后一次正常落盘的内容」。活文件损坏时可从它恢复。
    """
    return path + ".bak"


def _snapshot_bak(path: str) -> None:
    """发布前快照：若活文件当前可解析，复制到 ``.bak``（保留最后一次好版本）。

    只对「可解析」的活文件做快照 —— 若活文件已损坏，绝不把它当好版本盖到 ``.bak``
    上（否则连唯一可恢复的来源都丢）。
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        return  # 活文件已不可解析 → 不覆盖既有 .bak
    try:
        shutil.copy2(path, _last_good_bak(path))
    except OSError as e:
        logger.warning("项目索引 .bak 快照写入失败（既有 .bak 仍保留为最后好版本）：%s", e)


def _try_restore_bak(path: str):
    """活文件损坏时，尝试从 ``.bak``（最后一份好版本）恢复并返回其内容。

    恢复成功会把 ``.bak`` 内容写回活文件（原子替换），返回解析后的 dict；
    ``.bak`` 不存在/也不可解析时返回 ``None``（调用方据此 fail-loud）。
    """
    bak = _last_good_bak(path)
    if not os.path.isfile(bak):
        return None
    try:
        with open(bak, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    # 把 .bak 内容原子写回活文件，恢复数据
    tmp = f"{path}.restore.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
    return data


def _read_json(path: str, default, strict: bool = False):
    """读 JSON：对 Windows 的**瞬时共享冲突**做退避重试。

    ⚠️ 与 `_atomic_replace` 配对：Windows 上 `os.replace` 换目录项的那一瞬间，
    另一个线程 `open()` 同一路径会抛 `PermissionError`。旧实现把 OSError 一律
    当「读失败」→ 返回默认值（空索引）→ 用户看到「所有项目突然消失」，刷新又回来。
    这不是文件坏了，是暂时拿不到；退避重试后仍失败才降到默认值。

    P1-2（A-12）``strict=True``（仅关键数据文件，如项目索引）：解析失败时
    **不再静默返默认值**（那会被下游「读改写」当成空索引写回 → 清空全部项目）。
    改为：先尝试从 ``.bak``（最后一份好版本）恢复；恢复不了才**抛错 fail-loud**，
    由调用方决定上抛或降级 —— 绝不把损坏态读成空再写回。
    """
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:
            time.sleep(0.02 * (i + 1))
        except ValueError:
            # JSON 解析失败 = 文件真损坏（非瞬时占用）
            if strict:
                restored = _try_restore_bak(path)
                if restored is not None:
                    logger.warning(
                        "读取 %s 解析失败（文件损坏），已从 .bak 恢复最后一份好版本",
                        os.path.basename(path))
                    return restored
                logger.error(
                    "读取 %s 解析失败（文件损坏）且无可用 .bak，fail-loud，"
                    "避免静默清空数据", os.path.basename(path))
                raise
            logger.warning("读取 %s 解析失败，按默认值处理", os.path.basename(path))
            return default
        except OSError as e:
            # P1-2 补修（回归修复）：区分「文件不存在」与「存在但不可读」。
            # 文件不存在 = **正常初始态**（首次运行/全新部署）：
            if not os.path.exists(path):
                if strict:
                    # 活文件不存在但 .bak 还在（活文件被误删场景）→ 恢复最后一份好版本，
                    # 而不是静默建空索引（下次 save 会基于空索引写回、清空项目注册表）
                    restored = _try_restore_bak(path)
                    if restored is not None:
                        logger.warning(
                            "读取 %s：活文件不存在但存在 .bak，已恢复最后一份好版本",
                            os.path.basename(path))
                        return restored
                return default
            # 活文件存在但读取失败（非瞬时占用类 OSError）：
            if strict:
                logger.error(
                    "读取 %s 失败（文件存在但不可读：%s），fail-loud，"
                    "避免下游「读改写」基于空索引写回、清空数据",
                    os.path.basename(path), e if isinstance(e, Exception) else "OSError")
                raise
            return default
    # 瞬时占用（PermissionError）重试 6 轮仍未读到的兜底出口：
    if strict:
        logger.error(
            "读取 %s 反复被占用（已重试 6 次）且文件存在，fail-loud（strict），"
            "绝不把「读不到」当成「空」写回", os.path.basename(path))
        raise
    logger.warning("读取 %s 反复被占用（已重试 6 次），按未配置处理", os.path.basename(path))
    return default


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


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # ⚠️ 临时名必须**每次唯一**：固定 `.tmp` 会被并发写者互相截断（见文件头并发注释）
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())        # 先落盘再 replace，避免断电后只剩空文件
        _atomic_replace(tmp, path)      # 同分区 replace 原子 + Windows 占用重试
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)          # 失败不留垃圾临时文件
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
        raise


def new_project_id() -> str:
    return "p" + datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(2).hex()


# =====================================================================
# 注册表读写
# =====================================================================

@_locked
def load_index() -> dict:
    try:
        data = _read_json(PROJECT_INDEX_PATH, None, strict=True)
    except (ValueError, OSError) as e:
        # P1-2（A-12）：索引损坏且无可用 .bak → fail-loud，**绝不**静默建空索引再写回
        # （旧实现会把全部项目从注册表永久清空）。交由上层决定上抛或走 migrate_legacy 重建。
        logger.error("项目索引不可读且无可用备份：%s", e)
        raise
    if not isinstance(data, dict):
        data = {"version": INDEX_VERSION, "projects": [], "updated_at": now_str()}
    data.setdefault("version", INDEX_VERSION)
    projects = data.get("projects")
    if not isinstance(projects, list):
        data["projects"] = []
    # P0-4：越界剧本路径的只读标记（仅内存旁路字段，不写回 index.json、不清空原值）
    # —— 下游消费方（如成片合成）按 `script_path_untrusted` 判断是否可信。
    for rec in data.get("projects") or []:
        if not isinstance(rec, dict):
            continue
        sp = rec.get("script_path")
        if sp:
            rec["script_path_untrusted"] = not is_path_inside_output(sp)
        spls = rec.get("script_paths")
        if isinstance(spls, list):
            rec["script_paths_untrusted"] = [p for p in spls
                                             if not is_path_inside_output(p)]
    return data


@_locked
def save_index(index: dict) -> dict:
    index["version"] = INDEX_VERSION
    index["updated_at"] = now_str()
    _snapshot_bak(PROJECT_INDEX_PATH)   # P1-2：发布前快照「当前可解析好版本」，损坏时可从 .bak 恢复
    _write_json(PROJECT_INDEX_PATH, index)
    return index


def _unique_key(index: dict, base: str) -> str:
    used = {p.get("dir_key") for p in index.get("projects", [])}
    key = base
    n = 2
    while key in used:
        suffix = f"_{n}"
        key = base[: KEY_MAX_LEN - len(suffix)] + suffix
        n += 1
    return key


def get_project(ref: str) -> dict | None:
    """按项目 ID 或项目键查找项目记录"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    for p in load_index().get("projects", []):
        if p.get("id") == ref or p.get("dir_key") == ref or p.get("name") == ref:
            return p
    return None


@_locked
def ensure_project_for_novel(novel_id: str, novel_name: str = "",
                             config: dict = None) -> dict:
    """小说 → 项目 一一对应：已有则复用，没有则按小说名新建独立项目。

    保证「一部小说 = 一个独立项目」，其剧本/资产/视频全部落在该项目自己的目录下，
    不会与其它小说混在全局目录。
    """
    rec = find_by_novel(novel_id)
    if rec:
        return rec
    display = (novel_name or novel_id or "").strip() or "未命名项目"
    return create_project(display, novel_id=novel_id, novel_name=display,
                          config=config, note="由小说转换自动建立的项目")


def find_by_novel(novel_id: str) -> dict | None:
    novel_id = str(novel_id or "").strip()
    if not novel_id:
        return None
    for p in load_index().get("projects", []):
        if p.get("novel_id") == novel_id:
            return p
    return None


# =====================================================================
# 项目 CRUD
# =====================================================================

@_locked
def create_project(name: str, novel_id: str = "", novel_name: str = "",
                   config: dict = None, key: str = None, pid: str = None,
                   note: str = "") -> dict:
    """新建项目：注册表登记 + 建立独立配置目录与各产物隔离目录（均为空目录，不动任何已有文件）"""
    index = load_index()
    display = (name or "").strip() or "未命名项目"
    base_key = safe_key(key or display)
    dir_key = _unique_key(index, base_key)
    pid = pid or new_project_id()
    p = paths(dir_key)

    for d in (p["root"], p["scripts"], p["characters"], p["items"], p["scenes"],
              p["storyboards"], p["videos"], p["final"], p["upscale"], p["dub"], p["qc"]):
        os.makedirs(d, exist_ok=True)

    cfg = dict(PROJECT_DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if v is not None})
    # 视频生成方式：新建项目时即归一（前端选的值直接落盘；脏值/缺失回落 episode），
    # 后续「整集生成视频」与托管生产都从这里取，不再各自写死默认值。
    cfg["video_mode"] = norm_video_mode(cfg.get("video_mode"))
    _write_json(p["config"], cfg)
    if not os.path.exists(p["ai_settings"]):
        _write_json(p["ai_settings"], {"version": 1, "projects": {}, "active_project": dir_key})
    if not os.path.exists(p["chat_history"]):
        _write_json(p["chat_history"], {"version": 1, "messages": [],
                                        "drafts": {}, "active_project": dir_key})

    rec = {
        "id": pid,
        "name": display,
        "dir_key": dir_key,
        "novel_id": str(novel_id or ""),
        "novel_name": str(novel_name or ""),
        "created_at": now_str(),
        "updated_at": now_str(),
        "script_path": "",
        "script_paths": [],
        "episode_count": 0,
        "shot_count": 0,
        "episode_duration_sec": int(cfg.get("episode_duration_sec") or 0),
        # ⭐ 2026-10-10 统一：镜数正式名是 target_shots；历史键 shots_per_episode 兼容读取。
        "shots_per_episode": int(cfg.get("target_shots")
                                  if cfg.get("target_shots") is not None
                                  else (cfg.get("shots_per_episode") or 0)),
        "from_migration": False,
        "note": note,
    }
    index.setdefault("projects", []).append(rec)
    save_index(index)
    return rec


@_locked
def update_project(ref: str, **fields) -> dict | None:
    index = load_index()
    for rec in index.get("projects", []):
        if rec.get("id") == ref or rec.get("dir_key") == ref:
            for k, v in fields.items():
                if k in ("id", "dir_key"):
                    continue
                rec[k] = v
            rec["updated_at"] = now_str()
            save_index(index)
            return rec
    return None


def rename_project(ref: str, new_name: str) -> dict | None:
    """重命名项目（只改显示名，项目键与目录保持不变 → 已有产物无需搬动，绝不会丢数据）"""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("新项目名不能为空")
    rec = get_project(ref)
    if not rec:
        return None
    return update_project(rec["id"], name=new_name[:60], renamed_at=now_str())


def read_config(ref: str) -> dict:
    rec = get_project(ref)
    if not rec:
        return dict(PROJECT_DEFAULT_CONFIG)
    cfg = _read_json(paths(rec["dir_key"])["config"], None)
    merged = dict(PROJECT_DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def video_mode(ref: str) -> str:
    """项目级视频生成方式（新建项目时用户选择）——**全链路唯一权威取值**。

    消费方：/api/videos/generate（mode 缺省时的回落）、autopilot.get_plan（把项目选择
    派生进托管计划）、UI 展示。旧项目 config.json 没有该字段 → read_config 用
    PROJECT_DEFAULT_CONFIG 补齐并归一，零迁移成本。
    """
    try:
        return norm_video_mode((read_config(ref) or {}).get("video_mode"))
    except Exception as e:                     # noqa: BLE001 - 读配置失败不该让出片入口 500
        logger.warning("读取项目视频生成方式失败（回落 episode）：%s", e)
        return "episode"


@_locked
def update_config(ref: str, patch: dict) -> dict:
    rec = get_project(ref)
    if not rec:
        raise KeyError("项目不存在")
    cfg = read_config(rec["dir_key"])
    _patch = {k: v for k, v in (patch or {}).items() if v is not None}
    # 视频生成方式走归一（同 create_project）：入口收敛，脏值绝不进生成链路。
    if "video_mode" in _patch:
        _patch["video_mode"] = norm_video_mode(_patch["video_mode"])
    cfg.update(_patch)
    _write_json(paths(rec["dir_key"])["config"], cfg)
    fields = {}
    # ⭐ 2026-10-10 统一：镜数写入接受两个名字，落到同一个历史字段（前端契约不变）。
    for src, dst in (("target_shots", "shots_per_episode"),
                     ("shots_per_episode", "shots_per_episode"),
                     ("episode_duration_sec", "episode_duration_sec")):
        if src in patch and patch[src] is not None:
            try:
                fields[dst] = int(patch[src])
            except (TypeError, ValueError) as e:
                logger.debug("字段整型转换失败（忽略该字段）：%s", e)
    if fields:
        update_project(rec["id"], **fields)
    return cfg


#: 项目专属产物根目录（kind → 绝对根目录）。
#:
#: ⚠️ 2026-09-19 实测教训：这里曾**只列 11 类**，漏掉 final_dub（配音成片）、keyframes（尾帧）、
#: continuity（跨镜连续性）、autopilot（托管计划/历史/交付物索引）、autonomous（自主模式）、
#: comic_drama（漫画剧资产）、exports（导出件）、watermark（水印成片）。
#: 后果是「删掉项目」后 output/ 里仍残留 76MB 的 final_dub，用户以为已经删干净了。
#: ⚠️ 2026-10-06 实测教训（同名重建串历史）：这里又漏了 screenplays（文学剧本，
#: novel_screenplay.screenplays_root()）与 reports（生产报告，autonomous.export_report）。
#: 最恶性的一个：删项目→同名重建→托管续跑时，残留的 output/screenplays/<键>/第1集_文学剧本.md
#: 会被「文件存在即改写」的文学剧本链路（autopilot._produce → pipeline.run_episode）当成
#: **新项目第 1 集的原文**用上——旧项目的历史直接污染新项目的生产。
#: 新增产物目录时**必须**同步登记到这里，否则删除会再次漏。
def project_kind_roots() -> list:
    """(kind, 根目录) 列表 —— 删除/清理项目时需要覆盖的**全部**产物目录。

    注意：其中若干目录（screenplays/reports/autopilot/autonomous/exports/export/comic_drama）没有独立
    config 常量，按名字从 PROJECT_OUTPUT_DIR 拼出，避免为了一个字符串去动 config 接口。
    """
    out = PROJECT_OUTPUT_DIR
    return [
        ("scripts", SCRIPT_DIR),
        # 文学剧本（两段式生产的第一段，Markdown）与生产报告（autonomous.export_report）：
        # 都按 output/<kind>/<项目键>/ 落盘，此前漏登记 → 同名重建时残留（见上方 2026-10-06 教训）。
        ("screenplays", os.path.join(out, "screenplays")),
        ("reports", os.path.join(out, "reports")),
        ("characters", CHARACTERS_DIR),
        ("items", ITEMS_DIR),
        ("scenes", SCENES_DIR),
        ("storyboards", STORYBOARDS_DIR),
        ("videos", VIDEOS_DIR),
        ("final", FINAL_DIR),
        ("upscale", UPSCALE_DIR),
        ("dub", DUB_DIR),
        ("qc", QC_DIR),
        ("keyframes", KEYFRAMES_DIR),
        ("continuity", CONTINUITY_DIR),
        ("final_dub", DUB_MIX_DIR),
        ("watermark", WATERMARK_DIR),
        # 分离音效产物 output/sfx/<项目>/（H3_SFX_DIR）此前漏登记 → 实测删项目后
        # output/sfx/<项目>/ 会残留（其它 22 类都收走了，只有它留下）。
        ("sfx", H3_SFX_DIR),
        # 下面几个是「两层」结构：comic_drama/{characters,scenes}/<项目>
        ("comic_characters", os.path.join(out, "comic_drama", "characters")),
        ("comic_scenes", os.path.join(out, "comic_drama", "scenes")),
        ("autopilot", os.path.join(out, "autopilot")),
        ("autonomous", os.path.join(out, "autonomous")),
        ("exports", os.path.join(out, "exports")),
        ("export", os.path.join(out, "export")),
        # G8：两层结构 output/qc/audio/<项目> 此前只扫一层 QC_DIR 永远匹配不到；
        # _watermark_backup/<项目>（水印回滚源，按项目分子目录）补进删除覆盖，
        # 单项目删除时按项目名精确收走该项目自己的备份子目录（进可还原回收站）。
        # ⭐ 2026-10-10 自动扫描补登记：字幕校对报告落
        #   output/caption_verify/<项目>/ep{NN}.json（caption_verify.report_path），
        #   按项目分目录，此前既不在 kind_roots 也不在保留名里 → 删项目会残留，
        #   同名重建后旧校对报告被继承。
        ("caption_verify", os.path.join(out, "caption_verify")),
        ("qc_audio", os.path.join(QC_DIR, "audio")),
        ("watermark_backup", os.path.join(QC_DIR, "_watermark_backup")),
    ]


def _project_alias_names(rec: dict) -> list:
    """该项目在磁盘上可能出现过的目录名（项目键 + 显示名）"""
    names = []
    for v in (rec.get("dir_key"), rec.get("name")):
        v = str(v or "").strip()
        if v and v not in names:
            names.append(v)
    # 2026-10-06 补充（同名重建串历史）：screenplays 侧的目录键走的是
    # novel_to_script.safe_project_name（替换 \\/:*?\"<>| 与空白 + **截 40 字符**），
    # 与本模块 safe_key（**截 50 字符**）在超长项目键上会推导出两个不同目录名。
    # 把该形态并入别名并集，保证 _match_project_entries 对 screenplays/ 等产物根
    # 也能命中「按 safe_project_name 落盘」的目录；推导失败只降级（不阻断删除）。
    try:
        from novel_to_script import safe_project_name as _spn
        for v in list(names):
            _alt = str(_spn(v) or "").strip()
            if _alt and _alt not in names:
                names.append(_alt)
    except Exception as e:  # noqa: BLE001
        logger.debug("推导 screenplays 侧目录别名失败（忽略）：%s", e)
    return names


def _match_project_entries(root: str, names: list) -> list:
    """在 root 下找出属于该项目的全部条目（子目录**和**文件）。

    一部小说 = 一个项目，但产物在磁盘上有多种形态：
    - ``<项目名>/``                —— 项目级产物目录
    - ``<项目名>_第N集/``          —— 分集时每集一个子目录，互不覆盖
    - ``<项目名>_<时间戳>.json``    —— 整本转换的单文件剧本（落在 output/scripts 下）
    - ``<项目名>_..._wm.mp4``      —— 水印成片（落在 output/watermark 下，是文件不是目录）
    统一按「精确名 / 前缀」聚合，保证既不串项目、也不漏分集目录与单文件产物。
    """
    if not root or not os.path.isdir(root):
        return []
    hit = []
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if not (os.path.isdir(full) or os.path.isfile(full)):
            continue
        for n in names:
            if entry == n or entry.startswith(n + "_") or entry.startswith(n + "-"):
                hit.append(full)
                break
    return hit


@_locked
def delete_project(ref: str, confirm: bool = False) -> dict:
    """删除项目 = 移入回收站（output/projects/_trash/…），非物理删除，可手工还原。

    需要显式 confirm=True（前端会弹出二次确认框），未确认一律拒绝。

    覆盖范围：项目工作区 + :func:`project_kind_roots` 里的**全部**产物目录
    （含分集变体 `<项目名>_第N集`）。漏登记的后果见 project_kind_roots 的注释。
    """
    rec = get_project(ref)
    if not rec:
        raise KeyError("项目不存在")
    if not confirm:
        raise PermissionError("删除项目需要明确确认（confirm=true）")

    key = rec["dir_key"]
    p = paths(key)
    names = _project_alias_names(rec)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, f"{stamp}_{key}")
    os.makedirs(trash_root, exist_ok=True)
    # B-23 F-07：trash 目录 ACL 收紧——只允许属主读写，其他用户 0 权限。
    # Windows 上 chmod 是 no-op（NTFS 不读 POSIX 权限位），但在 POSIX 系统
    # 上阻止其他用户读取/还原已删除项目的残留产物。
    try:
        import stat
        os.chmod(trash_root, stat.S_IRWXU)
        # 向上追溯 PROJECT_TRASH_DIR 也收紧（若存在）
        _trash_base = os.path.abspath(PROJECT_TRASH_DIR)
        if os.path.isdir(_trash_base):
            os.chmod(_trash_base, stat.S_IRWXU)
    except OSError as e:
        logger.error("回收站目录权限收紧（ACL）失败，软删产物可能对同机其他用户可读：%s", e)

    moved, skipped = [], []
    targets = [("projects_workspace", p["root"])]
    for kind, root in project_kind_roots():
        targets.extend((kind, d) for d in _match_project_entries(root, names))

    for kind, src in targets:
        if not os.path.exists(src):
            continue
        dst = os.path.join(trash_root, kind, os.path.basename(src))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved.append({"kind": kind, "from": src, "to": dst})
        except (OSError, shutil.Error) as e:  # 单个目录失败不影响其余
            skipped.append({"kind": kind, "path": src, "error": str(e)})

    index = load_index()
    index["projects"] = [x for x in index.get("projects", []) if x.get("id") != rec["id"]]
    save_index(index)

    # 关联清理：ai_chat 的「按项目键分桶/分子目录」数据不在 22 类产物目录覆盖内
    # （全局单文件 chat_history.json / project_settings.json + archive/<键>/ 子目录），
    # 软删产物目录时收不走，会残留。这里摘除该项目的会话桶/草稿/创作设定/全量归档。
    # 关联簿记性质：失败不阻断主删除（项目已软删成功），只响亮降级并在结果里标记。
    ai_purge = {"purged": False, "report": None, "error": None}
    try:
        import ai_chat  # 延迟导入：ai_chat 不依赖 project_store，单向依赖；避免任何循环
        ai_purge["report"] = ai_chat.purge_project(
            history_path=AI_CHAT_HISTORY_PATH,
            settings_path=AI_SETTINGS_PATH,
            names=_project_alias_names(rec),
            archive_root=AI_CHAT_DIR,
        )
        ai_purge["purged"] = True
    except Exception as e:
        logger.error("删除项目 %s 时关联清理 ai_chat 数据失败（主删除已成功，不影响）：%s",
                     key, e)
        ai_purge["error"] = str(e)

    # 关联清理（数据库）：tasks.db 是全局单库（所有项目共用，不能整库删），
    # 该项目登记的任务 / 断点续跑单元残留在 tasks / units 两表。按项目别名并集
    # （dir_key + 显示名，覆盖「列里存键或存名」两种落法）级联摘除。
    # 同样是关联簿记：失败不阻断主删除（项目已软删成功），只响亮降级并标记。
    db_purge = {"purged": False, "report": None, "error": None}
    try:
        import task_store
        db_purge["report"] = task_store.get_store(TASKS_DB_PATH).purge_project(
            _project_alias_names(rec))
        db_purge["purged"] = True
    except Exception as e:
        logger.error("删除项目 %s 时关联清理任务库数据失败（主删除已成功，不影响）：%s",
                     key, e)
        db_purge["error"] = str(e)

    # 关联清理（托管）：项目没了，其生产状态/死信/尝试计数一并清掉 ——
    # 防止「删后重建同名项目」时，旧项目的生产进度被挂到新项目头上（用户实测缺陷）。
    autopilot_purge = {"purged": False, "error": None}
    try:
        import autopilot
        autopilot_purge.update(autopilot.purge_project(key))
        autopilot_purge["purged"] = True
    except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
        logger.error("删除项目 %s 时清理 autopilot 状态失败（主删除已成功，不影响）：%s", key, e)
        autopilot_purge["error"] = str(e)

    # 关联清理（剧本连续性缓存）：output/continuity/<项目名>/ 存着设定集/镜头拆分/风格/配音词表，
    # 不在 22 类产物目录清单内。不清的实测后果：删项目→重建同名→剧本「秒出」（命中旧缓存）、
    # 视频步骤带着旧镜头结构开跑，而角色/物品/场景图已删光 —— 用户看到「资产 0 但直接生成视频」。
    continuity_purge = {"purged": False, "path": "", "error": None}
    try:
        import shutil as _shutil
        _cont_root = os.path.join(PROJECT_OUTPUT_DIR, "continuity", key)
        if os.path.isdir(_cont_root):
            _cont_trash = os.path.join(PROJECT_TRASH_DIR, f"{stamp}_{key}", "continuity")
            os.makedirs(os.path.dirname(_cont_trash), exist_ok=True)
            _shutil.move(_cont_root, _cont_trash)
            continuity_purge["purged"] = True
            continuity_purge["path"] = _cont_trash
    except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
        logger.error("删除项目 %s 时清理 continuity 缓存失败（主删除已成功，不影响）：%s", key, e)
        continuity_purge["error"] = str(e)

    # 关联清理（直挂 output/ 根的散目录）：CharacterManager / RelationManager 以
    # `PROJECT_OUTPUT_DIR/<项目键>/characters/…`（characters.json、references/、
    # relations/relations.json）为工作区（app.py 的关系图谱/角色管理路由传
    # base_dir=PROJECT_OUTPUT_DIR/<project>），**不在任何产物根（project_kind_roots）
    # 覆盖内**，不清会在同名重建时被新项目继承。只按别名**精确名**匹配，且显式跳过
    # 系统保留名（projects/novels/qc/scripts/…）——防止项目键恰好撞上系统目录名时
    # 把整个产物根误搬进回收站。
    loose_purge = {"purged": [], "error": None}
    try:
        # ⭐ 2026-10-10 排查修复：保留名此前是**手工枚举**的 11 个，而 output/ 根下
        #   实际有 25+ 个系统目录 —— 漏掉的（asset_lib / analytics / caption_verify /
        #   video / workflows_export）一旦与项目名撞名，本项目删除会把**跨项目共享**
        #   的资源当产物移进回收站（asset_lib 是跨项目角色资产库，误删会毁掉所有
        #   项目的复用资产）。改为**动态推导 + 兜底清单**，新增系统目录自动受保护：
        #     ① project_kind_roots() 里每一类的根目录名；
        #     ② config 中所有位于 PROJECT_OUTPUT_DIR 之下的目录常量名；
        #     ③ 少量不在上述两处、但确实直挂 output 根的名字。
        _reserved = {os.path.basename(os.path.normpath(d)) for _, d in project_kind_roots()}
        try:
            _out_root = os.path.abspath(PROJECT_OUTPUT_DIR)
            for _cn in dir(config):
                if _cn.startswith('_'):
                    continue
                _cv = getattr(config, _cn, None)
                if not isinstance(_cv, str) or not _cv:
                    continue
                _ap = os.path.abspath(_cv)
                if _ap.startswith(_out_root + os.sep):
                    _reserved.add(os.path.relpath(_ap, _out_root).split(os.sep)[0])
        except Exception as _re:  # noqa: BLE001 - 推导失败退回下方兜底清单
            logger.warning("推导 output 保留目录名失败（回落兜底清单）：%s", _re)
        # ⚠️ asset_lib 必须留在这里：它是**跨项目共享**的角色资产库
        # （asset_library.library_root 用字面量拼 output/asset_lib/characters/<指纹>，
        #  不按项目分，config 里也没有对应常量）→ 上面两条自动推导都覆盖不到。
        # 删项目时它必须**保留**，否则会毁掉所有项目的形象复用资产。
        _reserved.update({
            "projects", "novels", "ai_chat", "lessons", "memory", "comic_drama",
            "assets", "analytics", "workflows_export",
            "asset_lib",                      # ← 跨项目共享，务必保留
            "video", "temp", ".leases", "_te3d_render", "_trash",
        })
        _seen_loose = set()
        for _n in _project_alias_names(rec):
            if not _n or _n in _reserved or _n in _seen_loose:
                continue
            _seen_loose.add(_n)
            _loose = os.path.join(PROJECT_OUTPUT_DIR, _n)
            if not os.path.isdir(_loose):
                continue
            _dst = os.path.join(trash_root, "output_root_loose", _n)
            os.makedirs(os.path.dirname(_dst), exist_ok=True)
            shutil.move(_loose, _dst)
            loose_purge["purged"].append(_loose)
    except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
        logger.error("删除项目 %s 时清理直挂 output/ 根的散目录失败（主删除已成功，不影响）：%s",
                     key, e)
        loose_purge["error"] = str(e)

    # 关联清理（AI 记忆）：output/memory/memories.json 是全局单文件，条目按
    # context.project 归属（ai_memory.record_lesson / record_success 落的 project 字段）。
    # 软删产物目录收不走「文件内嵌条目」，不摘除的话同名重建会把旧项目的教训/成功
    # 经验挂到新项目头上。
    ai_memory_purge = {"purged": False, "removed": 0, "error": None}
    try:
        import ai_memory
        ai_memory_purge["removed"] = int(
            ai_memory.get_memory_system().purge_project(_project_alias_names(rec)) or 0)
        ai_memory_purge["purged"] = True
    except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
        logger.error("删除项目 %s 时清理 AI 记忆失败（主删除已成功，不影响）：%s", key, e)
        ai_memory_purge["error"] = str(e)

    # 关联清理（文件租约）：集级租约 scope = episode:<项目>#<集号>（pipeline._episode_scope），
    # 项目名是租约文件名的成分。不清的话，同名重建后的一个 TTL 窗口（默认 900 秒）内
    # pipeline.is_episode_running 会把该集误判成「正在运行」而拒绝开跑。
    task_lease_purge = {"purged": False, "removed": 0, "error": None}
    try:
        import task_lease
        task_lease_purge["removed"] = int(
            task_lease.purge_project(_project_alias_names(rec)) or 0)
        task_lease_purge["purged"] = True
    except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
        logger.error("删除项目 %s 时清理文件租约失败（主删除已成功，不影响）：%s", key, e)
        task_lease_purge["error"] = str(e)

    # 关联清理（总控内核运行态）：总控对话本体都在 ai_chat 的项目桶/归档里
    # （上方 ai_chat.purge_project 已摘），但 agent_core 还有三份**进程内**运行态
    # 按项目键组织：任务表 _JOBS（含最终回复，TTL 1 小时）、单项目互斥占位 _BUSY、
    # 同参数冷却缓存 _COOLDOWN（键 md5 内嵌项目键）。同名重建时不清会让新项目
    # 继承旧运行态：/api/agent/job 还能读到旧任务回复、互斥占位报「已有任务在执行」、
    # 冷却窗口内同参数工具直接复用旧项目的执行结果。审计 audit-*.jsonl 是跨项目
    # 流水，保留不删（agent_core.purge_project 自身也不碰它）。
    agent_purge = {"purged": False, "report": None, "error": None}
    try:
        import agent_core  # 延迟导入：agent_core 顶层不依赖 project_store，单向
        agent_purge["report"] = agent_core.purge_project(_project_alias_names(rec))
        agent_purge["purged"] = True
    except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
        logger.error("删除项目 %s 时清理总控内核运行态失败（主删除已成功，不影响）：%s", key, e)
        agent_purge["error"] = str(e)

    # 关联清理（小说库）：全局小说库与项目是「素材 vs 生产工作区」的解耦关系，删项目
    # 默认**不**删小说（同一素材可复用到别的项目）。但当且仅当「该小说不再被任何其它
    # 项目绑定」时，说明它已无归属 → 一并软删进回收站（可恢复），避免「删完项目，
    # 素材库里还挂着一堆再也用不上的小说」的用户实测困惑。
    novel_purge = {"purged": False, "novel_id": "", "report": None, "error": None}
    _novel_id = str(rec.get("novel_id") or "").strip()
    if _novel_id:
        _still_bound = bool([p for p in load_index().get("projects", [])
                             if p.get("novel_id") == _novel_id])
        if not _still_bound:
            try:
                import novel_parser
                novel_purge["report"] = novel_parser.remove_novel(
                    NOVELS_DIR, _novel_id, trash_dir=os.path.join(PROJECT_TRASH_DIR, "_novels"))
                novel_purge["purged"] = True
                novel_purge["novel_id"] = _novel_id
            except Exception as e:  # noqa: BLE001 - 关联簿记：失败不阻断主删除，只响亮降级
                logger.error("删除项目 %s 时清理孤儿小说 %s 失败（主删除已成功，不影响）：%s",
                             key, _novel_id, e)
                novel_purge["error"] = str(e)

    # 清理结果汇总（2026-10-06）：删除接口返回值新增 cleaned 字段——这次删除到底
    # 收走了什么（移入回收站的目录逐条列出 + 簿记类清理按「条目 × 数量」列出）。
    # 前端不消费该字段（不报错），供运维/日志核对「同名重建前是否真的清干净」。
    cleaned = [{"kind": m["kind"], "path": m["from"]} for m in moved]
    _r = ai_purge.get("report") or {}
    if ai_purge.get("purged") and isinstance(_r, dict):
        for _k, _label in (("history_projects", "ai_chat/chat_history 项目桶"),
                           ("history_drafts", "ai_chat/drafts 草稿"),
                           ("settings", "ai_chat/project_settings 条目"),
                           ("archived_dirs", "ai_chat/archive 归档目录")):
            if int(_r.get(_k) or 0):
                cleaned.append({"kind": "entry",
                                "target": f"{_label} ×{_r[_k]}"})
    _r = (db_purge.get("report") or {}) if db_purge.get("purged") else {}
    if int(_r.get("tasks") or 0) or int(_r.get("units") or 0):
        cleaned.append({"kind": "entry",
                        "target": "tasks.db tasks ×%s / units ×%s"
                                  % (_r.get("tasks", 0), _r.get("units", 0))})
    if autopilot_purge.get("purged"):
        cleaned.append({"kind": "entry",
                        "target": "autopilot 运行态（current/last_error/last_run/attempts/死信）"})
    if continuity_purge.get("purged"):
        cleaned.append({"kind": "entry", "target": f"continuity/{key}"})
    for _p in (loose_purge.get("purged") or []):
        cleaned.append({"kind": "dir", "target": _p})
    _n = int(ai_memory_purge.get("removed") or 0)
    if _n:
        cleaned.append({"kind": "entry",
                        "target": f"ai_memory/memories.json 条目 ×{_n}"})
    _n = int(task_lease_purge.get("removed") or 0)
    if _n:
        cleaned.append({"kind": "entry", "target": f".leases 租约文件 ×{_n}"})
    _r = (agent_purge.get("report") or {}) if agent_purge.get("purged") else {}
    if int(_r.get("jobs_removed") or 0) or int(_r.get("busy_cleared") or 0) \
            or int(_r.get("cooldown_cleared") or 0):
        cleaned.append({"kind": "entry",
                        "target": "agent_core 运行态（jobs ×%s / busy ×%s / cooldown ×%s）"
                                  % (_r.get("jobs_removed", 0), _r.get("busy_cleared", 0),
                                     _r.get("cooldown_cleared", 0))})
    if novel_purge.get("purged"):
        cleaned.append({"kind": "entry",
                        "target": f"novels/{novel_purge.get('novel_id') or ''}"})

    return {"project": rec, "trash_dir": trash_root, "moved": moved,
            "skipped": skipped, "recoverable": True, "cleaned": cleaned,
            "ai_chat_purge": ai_purge, "tasks_db_purge": db_purge,
            "autopilot_purge": autopilot_purge,
            "continuity_purge": continuity_purge,
            "loose_dirs_purge": loose_purge,
            "ai_memory_purge": ai_memory_purge,
            "task_lease_purge": task_lease_purge,
            "agent_purge": agent_purge,
            "novel_purge": novel_purge}


# =====================================================================
# 剧本绑定与统计
# =====================================================================

def project_scripts(ref: str) -> list:
    """项目内全部剧本文件（分集目录 + 单文件），按集号排序"""
    rec = get_project(ref)
    if not rec:
        return []
    p = paths(rec["dir_key"])
    out = []
    if os.path.isdir(p["scripts"]):
        for fn in sorted(os.listdir(p["scripts"])):
            if fn.lower().endswith(".json"):
                out.append(os.path.join(p["scripts"], fn))
    # 兼容历史：整本转换的剧本落在 output/scripts/<项目名>_<时间戳>.json
    if os.path.isdir(SCRIPT_DIR):
        for fn in sorted(os.listdir(SCRIPT_DIR)):
            if fn.lower().endswith(".json") and rec["dir_key"] in fn:
                out.append(os.path.join(SCRIPT_DIR, fn))
    return out


def script_stats(script_path: str) -> dict:
    """读取剧本文件，提取「镜头数 / 每集时长（秒）」等统计（优先用 Schema 字段，缺失时回算）"""
    data = _read_json(script_path, None)
    if not isinstance(data, dict):
        return {}
    shots = data.get("shots") or []
    ep = data.get("episode_stats") or data.get("metadata", {}).get("episode_stats") or {}
    shot_count = int(data.get("shot_count") or ep.get("shot_count") or len(shots) or 0)
    duration = data.get("episode_duration_sec") or ep.get("duration_sec")
    if not duration:
        try:
            duration = sum(float(s.get("duration") or 0) for s in shots) or shot_count * 5
        except (TypeError, ValueError):
            duration = shot_count * 5
    return {
        "episode_no": data.get("episode_no") or (data.get("metadata") or {}).get("episode_no"),
        "title": (data.get("metadata") or {}).get("title") or data.get("title") or "",
        "shot_count": shot_count,
        "episode_duration_sec": round(float(duration or 0), 2),
        "characters": len(data.get("characters") or []),
        "items": len(data.get("items") or []),
        "scenes": len(data.get("scenes") or []),
    }


@_locked
def bind_script(ref: str, script_path: str, stats: dict = None) -> dict | None | bool:
    """把剧本归属到项目：登记 script_path / 分集清单 / 镜头数与总时长（不移动文件）

    P0-4：`script_path` 必须落在项目输出目录内（`is_path_inside_output`），
    越界（如系统目录 / 项目外路径）**拒写**并 `logger.warning`，返回 `False`
    让调用方知道被拒（不静默成功）；正常写入仍返回登记后的项目记录。
    """
    rec = get_project(ref)
    if not rec or not script_path:
        return None
    if not is_path_inside_output(script_path):
        logger.warning("拒绝登记越界剧本路径（%s 不在项目输出目录内，项目 %s）：%r",
                       "P0-4", rec.get("dir_key") or ref, script_path)
        return False
    existing = list(rec.get("script_paths") or [])
    if script_path not in existing:
        existing.append(script_path)
    stats = stats or script_stats(script_path)
    fields = {"script_paths": existing, "script_path": script_path}
    if stats:
        fields["shot_count"] = int(stats.get("shot_count") or rec.get("shot_count") or 0)
        if stats.get("episode_duration_sec"):
            fields["episode_duration_sec"] = float(stats["episode_duration_sec"])
    if len(existing) > 1:
        fields["episode_count"] = len(existing)
    return update_project(rec["id"], **fields)


def project_stats(ref: str) -> dict:
    """项目磁盘现状统计（只读扫描，用于列表页展示隔离成果）"""
    rec = get_project(ref)
    if not rec:
        return {}
    p = paths(rec["dir_key"])

    def count_files(d, exts=None):
        if not os.path.isdir(d):
            return 0
        n = 0
        for _, _, files in os.walk(d):
            for f in files:
                if not exts or os.path.splitext(f)[1].lower() in exts:
                    n += 1
        return n

    scripts = project_scripts(rec["dir_key"])
    return {
        "scripts": len(scripts),
        "characters": len(os.listdir(p["characters"])) if os.path.isdir(p["characters"]) else 0,
        "items": len(os.listdir(p["items"])) if os.path.isdir(p["items"]) else 0,
        "scenes": len(os.listdir(p["scenes"])) if os.path.isdir(p["scenes"]) else 0,
        "asset_images": count_files(p["characters"], {".png", ".jpg", ".jpeg", ".webp"})
                        + count_files(p["items"], {".png", ".jpg", ".jpeg", ".webp"})
                        + count_files(p["scenes"], {".png", ".jpg", ".jpeg", ".webp"}),
        "storyboards": count_files(p["storyboards"], {".png", ".jpg", ".jpeg", ".webp"}),
        "videos": count_files(p["videos"], {".mp4", ".mov", ".webm"}),
        "final_videos": count_files(p["final"], {".mp4", ".mov", ".webm"}),
        "upscaled": count_files(p["upscale"], {".mp4", ".mov", ".webm"}),
        "dub_audio": count_files(p["dub"], {".wav", ".flac", ".mp3"}),
        "qc_records": count_files(p["qc"], {".json"}),
        "disk_paths": {k: p[k] for k in ("root", "scripts", "characters", "items", "scenes",
                                         "storyboards", "videos", "final", "upscale", "dub", "qc")},
    }


def summarize(ref: str, with_stats: bool = True) -> dict | None:
    rec = get_project(ref)
    if not rec:
        return None
    out = dict(rec)
    out["config"] = read_config(rec["dir_key"])
    out["paths"] = paths(rec["dir_key"])
    # 封面标记：项目根目录 cover.png 是否存在（供前端卡片决定出图还是占位图标）
    out["has_cover"] = os.path.isfile(os.path.join(out["paths"]["root"], "cover.png"))
    out["stats"] = project_stats(rec["dir_key"]) if with_stats else {}
    return out


def list_projects(with_stats: bool = True) -> list:
    return [summarize(p["id"], with_stats) for p in load_index().get("projects", [])]


# =====================================================================
# 资产画廊（角色 / 场景 / 物品 / 分镜）
# =====================================================================

def _views_of(dir_path: str) -> list:
    views = []
    if not os.path.isdir(dir_path):
        return views
    for fn in sorted(os.listdir(dir_path)):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        full = os.path.join(dir_path, fn)
        views.append({
            "view": os.path.splitext(fn)[0],
            "file": full,
            "size": os.path.getsize(full),
            # ⭐ 2026-10-05 资产自动刷新：mtime 作为「版本令牌」下发 —— 前端拼成
            #   ?v=<mtime> 加到图片 URL 上。重生成后 URL 变化 → React 必重挂 <img>、
            #   浏览器必不命中旧缓存（此前 URL 恒定，轮询虽换回新 JSON，但 <img src>
            #   没变，用户看到的仍是旧图，误以为「必须手动刷新」）。
            "mtime": int(os.path.getmtime(full)),
            "url": "/api/assets/" + os.path.relpath(full, ASSETS_DIR).replace(os.sep, "/"),
        })
    return views


def project_dirs(root: str, ref: str) -> list:
    """项目键在某个产物根目录下的全部隔离子目录。

    一部小说 = 一个项目，项目内的产物目录有两级形态：
    - ``output/<kind>/<项目键>/``            （整本转换 / 项目级产物）
    - ``output/<kind>/<项目键>_第N集/``      （按章节分集时每集独立子目录，互不覆盖）
    两者都属于同一个项目，统一按「项目键」或「项目键_*」前缀聚合，保证不串项目。
    """
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    out = []
    if not os.path.isdir(root):
        return out
    if os.path.basename(os.path.normpath(root)) == key:
        # 传入的已经是项目专属目录（如 paths(key)["characters"]），直接返回自身
        return [root]
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if name == key or name.startswith(key + "_") or name.startswith(key + "-"):
            out.append(full)
    return out


def find_kind_dir(ref: str, kind: str, name: str) -> dict:
    """在项目范围内定位某个资产的目录。

    返回 {"asset_dir": 绝对路径, "project_dir": 所属项目产物子目录名}

    ``kind`` 取 characters / items / scenes / storyboards。
    """
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    root = paths(key)[kind]
    for d in project_dirs(root, key):
        if not os.path.isdir(d):
            continue
        target = os.path.join(d, name)
        if os.path.isdir(target):
            return {"asset_dir": target, "project_dir": os.path.basename(d),
                    "kind_root": root}
    return {"asset_dir": "", "project_dir": "", "kind_root": root}


def find_asset_file(ref: str, kind: str, filename: str) -> str:
    """在项目范围内按文件名定位文件（分镜图等），返回绝对路径或空串。"""
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    root = paths(key)[kind]
    for d in project_dirs(root, key):
        cand = os.path.join(d, filename)
        if os.path.isfile(cand):
            return cand
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn == filename:
                    return os.path.join(d, fn)
    return ""


def asset_gallery(ref: str) -> dict:
    """项目资产索引：角色 / 物品 / 场景 各自的视图列表（含可直接访问的 URL）

    聚合该项目下所有产物子目录（项目键本身 + 全部分集目录），实现项目级隔离下的完整画廊。
    """
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    p = paths(key)
    out = {"characters": [], "items": [], "scenes": []}
    for kind in ASSET_KINDS:
        base = p[kind]
        items = []
        for d in project_dirs(base, key):
            pdir = os.path.basename(d)
            for name in sorted(os.listdir(d)):
                sub = os.path.join(d, name)
                if not os.path.isdir(sub):
                    continue
                views = _views_of(sub)
                base_view = next((v for v in views if v["view"] == "base"), None)
                items.append({
                    "kind": kind[:-1] if kind.endswith("s") else kind,  # characters → character
                    "name": name,
                    "dir": sub,
                    "project_dir": pdir,
                    "view_count": len(views),
                    "views": views,
                    "thumb": (base_view or (views[0] if views else None)),
                })
        out[kind] = items
    return out


def gallery_summary(ref: str) -> dict:
    """画廊汇总：角色/物品/场景/分镜/视频/成片/配音/超分的条目数（按项目聚合）"""
    g = asset_gallery(ref)
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    p = paths(key)
    out = {k: len(v) for k, v in g.items()}
    out["storyboards"] = sum(len(files) for _, _, files in
                             [(0, 0, [f for f in os.listdir(d) if f.lower().endswith(".png")])
                              for d in project_dirs(p["storyboards"], key)])
    out["videos"] = sum(len(files) for _, _, files in
                        [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv"))])
                         for d in project_dirs(p["videos"], key)])
    out["final"] = sum(len(files) for _, _, files in
                       [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv"))])
                        for d in project_dirs(p["final"], key)])
    out["upscale"] = sum(len(files) for _, _, files in
                         [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv"))])
                          for d in project_dirs(p["upscale"], key)])
    out["dub"] = sum(len(files) for _, _, files in
                     [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".wav", ".flac", ".mp3"))])
                      for d in project_dirs(p["dub"], key)])
    return out


# =====================================================================
# 历史数据归属迁移（非破坏性：只登记/复制配置，绝不移动或删除原文件）
# =====================================================================

def _novel_lookup() -> dict:
    """小说名/标题 → novel_id"""
    idx = _read_json(os.path.join(NOVELS_DIR, "index.json"), {})
    novels = idx.get("novels") if isinstance(idx, dict) else idx
    out = {}
    for n in (novels or []):
        if not isinstance(n, dict):
            continue
        for field in ("name", "title"):
            v = (n.get(field) or "").strip()
            if v:
                out[v] = n.get("novel_id") or n.get("id") or ""
    return out


def _strip_ts(name: str) -> str:
    """剑心初醒_20260910_151231.json → 剑心初醒"""
    stem = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r"_\d{8}_\d{6}$", "", stem)


@_locked
def migrate_legacy(force: bool = False) -> dict:
    """扫描磁盘上的历史产物/剧本/配置，为尚未登记的数据补齐项目归属（不搬动任何文件）。"""
    index = load_index()
    known_keys = {p.get("dir_key") for p in index.get("projects", [])}
    created, existing, binds, sources = [], [], [], {}

    def ensure(key: str, name: str = "", novel_id: str = "", novel_name: str = "",
               from_migration: bool = True):
        nonlocal index
        key = safe_key(key)
        if not key:
            return None
        if key in known_keys:
            rec = next((p for p in index["projects"] if p.get("dir_key") == key), None)
            existing.append(key)
            return rec
        display = (name or key).strip()
        rec = create_project(display, novel_id=novel_id, novel_name=novel_name,
                             key=key, note="历史数据迁移")
        update_project(rec["id"], from_migration=bool(from_migration))
        index = load_index()
        known_keys.add(rec["dir_key"])
        created.append({"key": rec["dir_key"], "name": display,
                        "novel_id": novel_id, "novel_name": novel_name})
        return rec

    novels = _novel_lookup()

    # 1) 分集剧本目录 output/scripts/<小说名>/第N集.json
    if os.path.isdir(SCRIPT_DIR):
        for entry in sorted(os.listdir(SCRIPT_DIR)):
            full = os.path.join(SCRIPT_DIR, entry)
            if os.path.isdir(full):
                hits = [f for f in os.listdir(full) if f.lower().endswith(".json")]
                if not hits:
                    continue
                rec = ensure(entry, name=entry)
                sources.setdefault(entry, []).append(full)
                if rec:
                    for fn in sorted(hits):
                        binds.append((rec["dir_key"], os.path.join(full, fn)))
            elif entry.lower().endswith(".json"):
                # 2) 整本转换的单文件剧本 output/scripts/<小说名>_<时间戳>.json
                data = _read_json(full, {})
                meta = (data or {}).get("metadata") or {}
                key = safe_key(meta.get("project_name") or _strip_ts(entry))
                if not key:
                    continue
                rec = ensure(key, name=key)
                if rec:
                    binds.append((rec["dir_key"], full))

    # 3) 各产物目录 output/<kind>/<项目键>/…
    kind_dirs = [("characters", CHARACTERS_DIR), ("items", ITEMS_DIR), ("scenes", SCENES_DIR),
                 ("storyboards", STORYBOARDS_DIR), ("videos", VIDEOS_DIR), ("final", FINAL_DIR),
                 ("upscale", UPSCALE_DIR), ("dub", DUB_DIR), ("qc", QC_DIR)]
    for kind, d in kind_dirs:
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            full = os.path.join(d, entry)
            if not os.path.isdir(full):
                continue
            if entry.startswith("_"):
                continue
            ensure(entry, name=entry)
            sources.setdefault(entry, []).append(full)

    # 4) 全局 AI 设定 / 会话里出现过的项目键 → 补齐项目并复制其配置（不改动全局文件）
    for src_path, target_name in ((AI_SETTINGS_PATH, "ai_settings.json"),
                                  (AI_CHAT_HISTORY_PATH, "chat_history.json")):
        data = _read_json(src_path, None)
        if not isinstance(data, dict):
            continue
        keys = set((data.get("projects") or {}).keys()) | set((data.get("drafts") or {}).keys())
        if data.get("active_project"):
            keys.add(data["active_project"])
        for k in sorted(keys):
            ai_key = str(k).strip()
            k = safe_key(ai_key)
            if not k or k == "default":
                continue
            rec = ensure(k, name=k)
            if not rec:
                continue
            dst = (paths(rec["dir_key"])["chat_history"] if target_name == "chat_history.json"
                   else paths(rec["dir_key"])["ai_settings"])
            if os.path.exists(dst):
                blob = _read_json(dst, {})
                if blob.get("projects", {}).get(ai_key) or blob.get("drafts", {}).get(ai_key):
                    continue
            copy = {"version": 1, "projects": {}, "drafts": {}, "messages": []}
            if target_name == "ai_settings.json":
                s = (data.get("projects") or {}).get(ai_key)
                if s:
                    copy["projects"][k] = s
            else:
                dd = (data.get("drafts") or {}).get(ai_key)
                if dd:
                    copy["drafts"][k] = dd
            if copy.get("projects") or copy.get("drafts"):
                copy["active_project"] = k
                _write_json(dst, copy)

    # 5) 用小说索引反查 novel_id，补全小说绑定
    for rec in list(index["projects"]):
        if rec.get("novel_id"):
            continue
        for nm, nid in novels.items():
            if not nid:
                continue
            if nm and (nm in rec["dir_key"] or rec["dir_key"] in nm):
                update_project(rec["id"], novel_id=nid, novel_name=nm)
                break

    # 6) 剧本绑定 + 统计
    bound = []
    for key, sp in binds:
        rec = get_project(key)
        if not rec:
            continue
        st = script_stats(sp)
        if bind_script(rec["id"], sp, st) is not False:
            bound.append({"key": key, "script": sp,
                      "shot_count": st.get("shot_count"),
                      "episode_duration_sec": st.get("episode_duration_sec")})

    report = {
        "generated_at": now_str(),
        "projects_total": len(load_index().get("projects", [])),
        "created": created,
        "already_existed": sorted(set(existing)),
        "scripts_bound": bound,
        "source_dirs": sources,
        "policy": "仅登记与复制配置；不移动、不删除任何历史产物，历史数据零丢失",
    }
    _write_json(PROJECT_MIGRATE_REPORT, report)
    return report
