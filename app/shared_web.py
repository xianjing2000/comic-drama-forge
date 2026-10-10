# -*- coding: utf-8 -*-
"""请求/响应与文本安全的通用工具（自 routes/_shared.py 抽出）。

## 为什么抽（_shared 拆分第 2 步，2026-10-10）

延续第 1 步（app/shared_base.py）的做法：把**零业务依赖**的关注点从
routes/_shared.py 上移到 app/ 层，_shared.py 只做再导出，导入点一个不改。

本模块收拢的是一组「HTTP 边界」工具：

  · _friendly_error   —— 异常文本归一化（不让 traceback / 路径 / 模块名外泄到前端）
  · _body             —— 永不抛的取 body（避免 415/400 把接口带偏）
  · _safe_upload_name —— 上传文件名净化（保留中文、剥离路径与非法字符）
  · _serve_safe       —— 目录穿越防护的 send_file 统一入口

## 铁律

不得依赖任何业务模块（config / project_store / comfyui_client / …），
只允许标准库与 flask —— 与 shared_base 同一条规矩。
"""
from __future__ import annotations

import os
import re
import time

from flask import abort, request, send_file


def _friendly_error(msg, fallback: str = "服务内部错误，请稍后重试（详情见后端日志）") -> str:
    """把后端异常整理成可安全展示给前端的文案（对应测试缺陷 D5）。

    前端错误框不应出现 traceback、文件路径、模块名等实现细节。
    这里做一次归一化：截掉 traceback 段、去掉 File/line 与模块来源、
    压缩空白并限长；若仍残留实现细节特征，则整体降级为通用文案。
    业务类友好错误（中文短句）会原样保留。
    """
    text = str(msg or "").strip()
    if not text:
        return fallback
    idx = text.find("Traceback (most recent call last)")
    if idx != -1:
        text = text[:idx].strip()
    text = text.splitlines()[-1].strip() if text else ""
    text = re.sub(r'File\s+"[^"]*",\s*line\s*\d+', "", text)
    text = re.sub(r"\s*from\s+'[^']*'", "", text)          # cannot import name 'X' from 'mod'
    text = re.sub(r"\s*\([^()]*\.py[^()]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 仍残留实现细节（模块/导入语句/文件路径）→ 一律降级，避免外泄内部结构
    if re.search(r"\.py\b|\bimport\b|\bmodule\b|site-packages|[\\/]", text):
        return fallback
    if len(text) > 160:
        text = text[:160] + "…"
    return text or fallback


def _body() -> dict:
    """统一取请求 body，**永不抛异常**（返回空 dict 兜底）

    为什么不用裸 request.json：Flask 在 Content-Type 不是 application/json 时抛
    UnsupportedMediaType（415），body 非法 JSON 时抛 BadRequest（400）。
    这类错误会让接口以 4xx 结束，而不是「按缺参数处理并给出可读错误」——
    用 curl -d '{}'（默认表单 Content-Type）调就会直接 415，排查成本很高。
    项目约定：所有取 body 的地方统一走这里。
    """
    return request.get_json(silent=True) or {}


def _safe_upload_name(filename: str) -> str:
    """保留中文文件名，仅剥离路径与非法字符"""
    name = os.path.basename(str(filename or '').replace('\\', '/').split('/')[-1])
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name).strip().strip('.')
    return name or f"novel_{int(time.time())}.txt"


def _serve_safe(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的 send_file 统一入口。

    返回 send_file(...) 或 403/404 的 Flask 响应；永不抛异常。
    用法：return _serve_safe(KEYFRAMES_DIR, filename)。

    防护原理（S-06）：
    - 旧写法用 os.path.normpath(filename).startswith('..') 拦截穿越，
      但 os.path.join(base, safe_path) 遇**绝对路径**（如 C 盘路径、/etc/passwd）
      会丢弃 base 前缀直接返回绝对路径 → 穿越成功。
    - 这里用 os.path.abspath 归一后校验目标路径必须落在 base_dir 之内
      （前缀匹配 base_dir + 分隔符），从根上杜绝穿越。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    # 必须严格落在 base 之内：base 本身（目录）或 base + 分隔符 开头
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    return send_file(target, **send_kw)
