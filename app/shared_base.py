# -*- coding: utf-8 -*-
"""路由层与核心模块共用的**最底层**工具（自 routes/_shared.py 抽出）。

## 为什么要抽（2026-10-10 设计审查）

routes/_shared.py 1143 行、53 个函数，混了 6 个关注点（HTTP 辅助 / 业务算法 /
项目解析 / AI 客户端 / 提示词记忆 / 文件搬移），却被 **16 个 core 模块反向依赖**
（asset_worker、storyboard_helpers、keyframe_helpers、video_helpers …）——
核心逻辑依赖 HTTP 路由层的私有模块，是明确的分层倒置。

拆分第 1 步（本文件）：把**零业务依赖**的地基先提到 app/ 层。
routes/_shared.py 改为从本模块再导出，55 个导入点一个都不用改 —— 零行为变化。
后续再逐个关注点上移，最后把 _shared 收薄成纯再导出。

## 本模块的铁律

**不得依赖任何业务模块**（config / project_store / comfyui_client / …），
只允许标准库与 flask。否则只是把分层倒置换了个地方。
"""
from __future__ import annotations

import logging
import os
import shutil
import time

from flask import current_app


def _app_logger():
    """上下文安全的日志器：请求内用 Flask 的 app.logger（保留其 handler/格式），
    请求外（后台线程、离线守卫、单测）回落到标准 logging —— 直接写 current_app.logger
    会在没有应用上下文时抛 RuntimeError（2026-10-08 verify_qc_fault_open 实测）。

    ⚠️ 2026-10-10 修复：此处原写作「return _app_logger()」——**递归调用自己**。
    它会一路递归到 RecursionError，而 RecursionError 是 RuntimeError 的子类，
    于是每次都被 except 接住、返回标准 logging：**Flask 的 logger 从未生效过**，
    注释里写的意图（保留 app.logger 的 handler/格式）与实际行为完全相反；
    每次调用还要付一次约 1000 层栈展开 + 异常构造的代价。
    """
    try:
        return current_app.logger
    except RuntimeError:                 # 没有应用上下文（后台线程 / 离线守卫 / 单测）
        return logging.getLogger("app")


def _move_with_retry(src, dst, attempts: int = 5, delay: float = 0.4):
    """把产物从 ComfyUI output 搬进项目目录，**容忍 Windows 文件占用**（幂等）。

    背景（2026-10-07 实测真缺陷）：资产链路出现过
    「PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问。」
    —— ComfyUI 刚写完 PNG，句柄（或缩略图 / 杀软 / 另一条链路）尚未释放，
    shutil.move 当场抛错 → 该资产被判「生成失败」→ **白烧一次 45 秒渲染**，
    且断点续跑还要再烧。渲染本身没问题，纯粹是搬移时机问题。

    做法：
      · 按 delay * (i + 1) 递增退避重试 attempts 次；
      · 期间若 src 已消失而 dst 已就位（另一条链路已完成同一搬移）→
        **视为成功**（幂等，不把并发搬移误判成失败）；
      · src 与 dst 都不存在 → 直接抛 FileNotFoundError（真丢产物，别空等）。

    返回 dst；重试耗尽仍失败时抛出最后一次异常（与旧行为同样 fail-loud）。
    """
    last = None
    for i in range(max(1, int(attempts))):
        if os.path.exists(dst) and not os.path.exists(src):
            return dst                      # 已被别处搬走：幂等成功
        try:
            shutil.move(src, dst)
            return dst
        except OSError as e:                # PermissionError 是 OSError 子类
            last = e
            if not os.path.exists(src):
                break                       # 源已没了、目标又没成 → 再等无意义
            time.sleep(delay * (i + 1))
    if last is None:
        last = FileNotFoundError(f"产物不存在，无法搬移：{src}")
    _app_logger().warning("产物搬移失败（已重试 %d 次）：%s -> %s：%s",
                          attempts, src, dst, last)
    raise last


def _trash_move(src, category, trash_root, cleared, skipped):
    """把单个文件/目录移入回收站；不存在=无事发生，被占用=记入 skipped。

    模块级版本（reset-shot / reset-asset 共用）；reset-episode 端点内另有闭包版本，语义一致。
    """
    if not src or not os.path.exists(src):
        return
    try:
        dst = os.path.join(trash_root, category,
                           os.path.basename(src.rstrip('\\/')) or category)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _move_with_retry(src, dst)
        cleared.append({"category": category, "path": src})
    except Exception as e:  # noqa: BLE001  单项失败不阻断其余清理
        skipped.append({"path": src, "error": str(e)})
