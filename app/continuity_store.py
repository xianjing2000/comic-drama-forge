# -*- coding: utf-8 -*-
"""continuity 目录的路径 / JSON 小工具 + bible 读写（叶子模块，2026-10-08 解耦）。

为什么单独拆出来：
    continuity.py 有 2300+ 行，但 chapter_preflight 只想要 load_bible / save_bible，
    为此不得不 import 整个 continuity —— 那是「5 模块剧本环」的一条环边。
    这几个函数真正依赖的只有「目录内路径拼接 + JSON 读写 + 版本号」，与 LLM/剧本逻辑无关，
    因此下沉为叶子模块：谁要读设定库谁就 import 它，不再经过 continuity。

    continuity 保留同名再导出（app.py 的 continuity.continuity_root / load_bible、
    .workbuddy/test 下的守卫都照旧可用）。
"""
import os
import re
from datetime import datetime

from fs_atomic import atomic_write_json, read_json_strict

CONTINUITY_VERSION = "continuity_v1"



def load_json(path: str, default=None):
    """严格读 JSON（A-4）：缺失→default；损坏→从 .bak 恢复；无 .bak→抛错。

    旧实现 `except Exception: logger.warning(...); return default` 会把「文件损坏」
    降级成「没有内容」，而 bible / style_guide / quotes / voice_dict / camera_terms
    这几个读取点都是「读改写」（读出来改一改再 save_json 写回）→ 损坏态被读成空后
    写回，项目设定库被永久清空。现在改为 fail-loud，由调用方按需在边界处显式降级。
    """
    return read_json_strict(path, default)


def save_json(path: str, data) -> str:
    """原子写 JSON（A-3）：唯一临时名 + fsync + .bak 快照 + replace 重试。"""
    atomic_write_json(path, data)
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe(name: str, limit: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())
    return s[:limit] or "novel"


def continuity_root(continuity_dir: str, project_key: str) -> str:
    return os.path.abspath(os.path.join(continuity_dir, _safe(project_key)))


def _path(continuity_dir: str, project_key: str, filename: str) -> str:
    return os.path.join(continuity_root(continuity_dir, project_key), filename)


def bible_path(continuity_dir: str, project_key: str) -> str:
    return _path(continuity_dir, project_key, "bible.json")


def load_bible(continuity_dir: str, project_key: str) -> dict:
    data = load_json(bible_path(continuity_dir, project_key), None)
    if not isinstance(data, dict):
        return {}
    for k in ("characters", "items", "scenes"):
        if not isinstance(data.get(k), list):
            data[k] = []
    return data


def save_bible(continuity_dir: str, project_key: str, bible: dict) -> str:
    bible = dict(bible or {})
    bible["version"] = CONTINUITY_VERSION
    bible["updated_at"] = _now()
    return save_json(bible_path(continuity_dir, project_key), bible)
