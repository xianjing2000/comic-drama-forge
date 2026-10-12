# -*- coding: utf-8 -*-
"""全书结构化大纲 —— 读接口（2026-10-02 引入，2026-10-12 生成主流程移除）。

背景（2026-10-12 t40 死代码处置）：
本模块最初的定位是「整本转剧本的强制前置」—— build_outline 通读全文、抽角色、
拆段落、落成 output/continuity/<项目键>/book_outline.json 供后续环节引用。
但「整本转剧本」路径已于 2026-10-05 随「一章一集」拍板移除（见 novel_to_script
末尾注释），build_outline / save_outline 及整条分段流水线因此零调用方、成为
死代码，本次一并删除。

保留的是读接口：routes/projects.py 的封面主题与主角/反派识别仍会
load_outline 读取历史已生成的 book_outline.json（向后兼容）。若产品后续恢复
「全书大纲」或「整本转剧本」，需在相应入口重新接线生成逻辑。

对外接口：
- outline_path：book_outline.json 的落盘/读取路径（单一来源）。
- load_outline：读档（按内容指纹校验，不一致返回 None）。
"""
from __future__ import annotations

import hashlib
import logging
import os

from config import CONTINUITY_DIR
from fs_atomic import read_json_strict

logger = logging.getLogger(__name__)

OUTLINE_VERSION = "book_outline_v1"

# 主角团判定阈值（角色在提炼结果中出现的块数 ≥ 该值 → primary）。
# 保留供 config_doctor 的 owner 标注引用（字符串），以及未来接线时复用。
PRIMARY_MIN_CHUNKS = 3


def outline_path(project_key: str, continuity_dir: str = None) -> str:
    """全书大纲落盘路径：output/continuity/<项目键>/book_outline.json"""
    root = os.path.join(continuity_dir or CONTINUITY_DIR,
                        project_key or "_default")
    return os.path.join(root, "book_outline.json")


def _content_fingerprint(text: str, chunk_count: int) -> str:
    """内容指纹：同一本书重跑时命中缓存/复用文档的依据（正文变更则失效）"""
    h = hashlib.sha1()
    h.update(str(chunk_count).encode("utf-8"))
    h.update(str(len(text)).encode("utf-8"))
    h.update(text[:800].encode("utf-8"))
    h.update(text[-800:].encode("utf-8"))
    return h.hexdigest()[:16]


def load_outline(project_key: str, text: str, chunk_count: int,
                 continuity_dir: str = None) -> dict | None:
    """读取已落盘大纲；指纹不一致（正文/分块变了）返回 None 触发重算"""
    path = outline_path(project_key, continuity_dir)
    if not os.path.isfile(path):
        return None
    try:
        data = read_json_strict(path, None)
    except Exception:  # noqa: BLE001 - 读档失败等同未生成，走重算
        data = None
    if not isinstance(data, dict) or not data.get("characters"):
        return None
    if data.get("fingerprint") and data.get("fingerprint") != _content_fingerprint(text, chunk_count):
        logger.info("book_outline 指纹不一致（正文或分块变化），重新生成：%s", path)
        return None
    return data
