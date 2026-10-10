# -*- coding: utf-8 -*-
"""项目 / 镜头基础工具（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 4 步，2026-10-10）

前三步按「关注点」搬；本步按**反向依赖的实际热度**搬。统计 core 模块从
routes._shared 导入的 49 个名字，排在最前的是：

    _safe_project     11 个 core 模块
    comfyui_client     9 个
    _project_or_400    8 个
    _shot_seq          8 个
    _first_existing    7 个
    _shot_num_key      7 个

也就是说，core 模块「反向依赖路由层」这件事，主要就是在拿这些**与 HTTP 毫无关系**
的东西（项目键安全化、镜头序号、ComfyUI 单例）。把它们上移到 app/ 层之后，
反向依赖的实体少掉一大半。

## 内容

  · _first_existing   取第一个存在的路径
  · _safe_project     项目名 → 安全键（与项目注册表同口径）
  · _project_or_400   项目入参的统一校验入口（空 / 控制字符 / 路径穿越 → 400）
  · _shot_seq         shot_key.shot_seq 的再导出
  · _shot_num_key     shot_key.norm_shot_key 的再导出
  · comfyui_client    ComfyUIClient 的**进程级单例**

## 铁律

不得反向依赖 routes/*。project_store 与 shot_key 均不导入 routes._shared（已核）。
"""
from __future__ import annotations

import os
import re

from flask import jsonify

import project_store
import shot_key
from comfyui_client import ComfyUIClient
from config import PROJECT_OUTPUT_DIR
from shared_base import _app_logger


_shot_seq = shot_key.shot_seq


_shot_num_key = shot_key.norm_shot_key


comfyui_client = ComfyUIClient()


def _project_or_400(raw, field_name="project_name"):
    """G4 收口：路由层「项目入参 → 安全键 / 400」的统一入口。

    ⚠️ 不能写 `_safe_project(x) or 兜底`、也不能判 `_safe_project(x)` 的真值——
    `safe_key('')` 返回**字面量 'project'**（真值），守卫恒不成立（死守卫），
    漏传项目名会静默写进共享 `project` 命名空间。判空必须看**原始入参**
    （与 api_qc_project_summary 的 G3 修复同一口径）。

    A-01（F-01）加固：额外**拒绝路径穿越**入参（含 `..` / 绝对路径 / 路径分隔符）。
    仅靠 `safe_key` 收敛会把 `../../evil` 静默变成合法键 `evil`——虽不越界写盘，
    但把越界尝试当成正常项目混淆视听；此处直接 400，作到「越界即拒 + 不落盘」。
    收敛后仍做一次 abspath 前缀校验作为双保险（防御未来 safe_key 规则变更）。

    返回 (project, error)：error 为 None 表示合法（project 已 safe_key）；
    否则 error 是 (jsonify, 400) 响应，直接 return 它。
    用法::

        project, err = _project_or_400((data.get('project_name') or '').strip())
        if err is not None:
            return err
    """
    if not (isinstance(raw, str) and raw.strip()):
        return "", (jsonify({"success": False, "error": f"缺少 {field_name}"}), 400)
    raw_s = raw.strip()
    # task#7 口径补齐：含控制字符（如 NUL `\x00`）/ **无任何有效字符**（如 `.` `。` `…`）的
    # 入参 → 与空串**同口径 400**。否则 `safe_key` 会把它们坍缩成共享默认键 `project`
    # （非越界、无写盘，但会静默写进共享命名空间，且与空串口径不一致、掩盖调用方 bug）。
    # ⚠️ 判「有效字符」只看 isalnum/_/-（与 safe_key 的存活字符一致）：中文名（isalnum 为真，
    #    如「剑影孤城」「蛊真人精校版」）照常通过，绝不被误杀。
    if any(ord(_c) < 32 or ord(_c) == 0x7f for _c in raw_s):
        _app_logger().warning("[task#7] 拒绝含控制字符的项目名：%r", raw_s)
        return "", (jsonify({"success": False,
                             "error": f"非法的 {field_name}（含控制字符）"}), 400)
    _cleaned = re.sub(r"[《》〈〉【】「」『』]", "", raw_s)
    if not any((_c.isalnum() or _c in "_-") for _c in _cleaned):
        _app_logger().warning("[task#7] 拒绝无有效字符的项目名（与空串同口径）：%r", raw_s)
        return "", (jsonify({"success": False,
                             "error": f"非法的 {field_name}（无有效字符）"}), 400)
    if (raw_s.startswith(("/", "\\")) or ".." in raw_s
            or "/" in raw_s or "\\" in raw_s
            or os.path.isabs(raw_s) or os.path.splitdrive(raw_s)[0]):
        _app_logger().warning("[A-01] 拒绝越界项目名（疑似路径穿越）：%r", raw_s)
        return "", (jsonify({
            "success": False,
            "error": f"非法的 {field_name}（禁止路径分隔符 / 绝对路径 / 「..」）"}), 400)
    project = _safe_project(raw_s)
    _root = os.path.abspath(PROJECT_OUTPUT_DIR)
    _pdir = os.path.abspath(os.path.join(PROJECT_OUTPUT_DIR, project))
    if not _pdir.startswith(_root + os.sep):
        _app_logger().warning("[A-01] 项目名收敛后仍越界，拒绝：%r → %r", raw_s, project)
        return "", (jsonify({"success": False, "error": f"非法的 {field_name}"}), 400)
    return project, None


def _safe_project(name: str) -> str:
    """项目名安全化（与项目注册表的项目键规则保持一致）"""
    return project_store.safe_key(name)


def _first_existing(*candidates):
    for c in candidates:
        if isinstance(c, str) and c and os.path.exists(c):
            return c
    return None


