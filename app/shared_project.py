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
import ai_chat
import autopilot
import style_kit
from config import AI_SETTINGS_PATH


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


def _novel_key(novel_meta: dict, project_ref: str = None) -> str:
    """剧集目录/项目名前缀所用的稳定键：优先取项目注册表分配的项目键。

    一部小说 = 一个独立项目 → 剧本落盘 output/scripts/<项目键>/，与其它小说彻底隔离。
    """
    rec = project_store.get_project(project_ref) if project_ref else None
    if not rec:
        rec = project_store.find_by_novel(novel_meta.get("novel_id") or novel_meta.get("id"))
    if rec:
        return rec["dir_key"]
    raw = (novel_meta.get("name") or novel_meta.get("title")
           or novel_meta.get("novel_id") or "novel")
    cleaned = re.sub(r"[《》〈〉【】「」『』\s]+", "", str(raw)).strip()
    return cleaned or str(novel_meta.get("novel_id") or "novel")


def _resolve_novel_project(data: dict, novel_meta: dict) -> dict:
    """把当前操作绑定到项目：显式指定优先，否则按小说自动建立/复用独立项目。"""
    data = data or {}
    ref = (data.get('project_id') or data.get('project_name') or '').strip()
    rec = project_store.get_project(ref) if ref else None
    if rec is None:
        rec = project_store.ensure_project_for_novel(
            novel_meta.get("novel_id") or novel_meta.get("id") or "",
            novel_meta.get("name") or novel_meta.get("title") or "")
    return rec


_AUDIO_QC_AUDIO_EXT = ('.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg')


_AUDIO_QC_MEDIA_EXT = ('.mp4', '.mkv', '.mov', '.webm', '.m4v', '.avi')


_AUDIO_QC_NON_PROJECT_DIRS = ('lines', 'frames', 'output', 'temp', 'qc', 'audio', 'audio_mix')


def _audio_qc_project_key(target: str) -> str:
    """按产物路径反推项目名，用于可视化图片的落盘目录。

    ⚠️ 不能退化成字面量（如 ``project``）：按 ``path`` 直接检查时拿不到项目名，
    所有项目就会挤进同一个目录，**不同项目的同名文件互相覆盖** —— 而 AI 读图是
    子进程/网络异步进行的，覆盖会变成竞态（读数项目的图）。
    布局：成片 ``output/final_dub/<项目>/x.mp4``、逐句 ``output/dub/<项目>/lines/x.wav``。
    """
    d = os.path.dirname(os.path.abspath(target))
    for _ in range(4):
        name = os.path.basename(d)
        if name and name.lower() not in _AUDIO_QC_NON_PROJECT_DIRS:
            return name
        parent = os.path.dirname(d)
        if parent == d:                       # 已到根，别再往上
            break
        d = parent
    return 'adhoc'


def _ep_dir(base_dir: str, episode_no=None) -> str:
    """写入用的集级产物目录（第 1 集 = 平铺目录）"""
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1
    if ep <= 1:
        return base_dir
    return os.path.join(base_dir, f"ep{ep:02d}")


def _ep_read_dir(base_dir: str, project: str, episode_no=None) -> str:
    """读取用的集级产物目录：优先集目录；**仅第 1 集**才回落到平铺目录。

    ⚠️ 2026-10-09 修复（实测暴露）：原实现「不存在就回落平铺」对**第 2 集及以后**
    是错的 —— 当 epNN/ 还没产出时，它会返回**第 1 集的平铺目录**，
    于是前端/接口把第 1 集的分镜图当成第 2 集的产物展示（用户看到「两集分镜重复」）。
    真正的兼容目标只是**第 1 集平铺时代的旧数据**（见 _ep_dir 的 docstring），
    因此这里把回落严格限定在 ep <= 1。第 2+ 集目录不存在时返回该集应有的空目录路径，
    让调用方得到「本集暂无产物」而不是「别人的产物」。
    """
    try:
        _ep = int(episode_no or 1)
    except (TypeError, ValueError):
        _ep = 1
    flat = os.path.join(base_dir, project)
    d = _ep_dir(flat, episode_no)
    if os.path.isdir(d) and d != flat:
        return d
    if _ep <= 1 and os.path.isdir(flat):
        return flat
    return d


def _apply_project_settings(style: str, project_name: str = "") -> str:
    """把「AI 对话 → 应用设定」落盘的创作设定并入风格描述，供剧本 / 提示词 / 分镜链路引用。

    - 命中项目则用该项目的生效设定；未命中则退回最近一次应用的设定；
    - 未应用过任何设定时原样返回 style，行为与改造前一致。
    """
    base = (style or "").strip()
    try:
        brief = (ai_chat.settings_view(AI_SETTINGS_PATH, project_name).get("style_brief") or "").strip()
        if not brief and (project_name or "").strip():
            brief = (ai_chat.settings_view(AI_SETTINGS_PATH, "").get("style_brief") or "").strip()
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"创作设定读取失败（忽略，沿用原风格）：{e}")
        return base
    if not brief:
        return base
    return f"{base}；{brief}" if base else brief


def _project_style(project_name: str = "") -> str:
    """取项目的生效风格：plan.json 的 style（总控 AI 敲定）> AI 设定面板 > config.json 的 style。

    前端手工触发的生成路由（分镜 / 视频 / 资产）此前**完全不传风格**，
    导致「界面按钮点出来的图」和「托管跑出来的图」风格行为不一致。
    统一从这里取，保证两条链路同源。

    2026-09-23（建项目选风格）：新增第三级兜底 config.json 的 style —— 用户「新建项目」时
    下拉/自定义的风格写进 config.style，但此前这里完全不读它，选了什么都不会生效
    （总控没敲定风格时 style 恒为空 → 生成被 409 拦截或回落默认）。现在总控没敲定时
    退回 config.style，让「建项目时选风格」这条路径真正闭环。
    """
    proj = _safe_project(project_name or "")
    try:
        brief = (autopilot.get_plan(proj) or {}).get("style") or ""
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"读取项目风格失败（忽略）：{e}")
        brief = ""
    if not brief:
        try:
            brief = _apply_project_settings("", proj)
        except Exception:  # noqa: BLE001
            brief = ""
    if not brief:
        # 建项目时选的风格 + 画面比例（config.json 的 style / aspect_ratio 字段）作为最后兜底。
        # 画面比例拼进风格串，让 style_kit.aspect_ratio 能解析，从而真正落到视频/分镜画布
        # （与总控 style_brief 里的「画面比例：9:16 竖屏」同一种表达，解析口径一致）。
        try:
            rec = project_store.get_project(proj)
            if rec:
                cfg = project_store.read_config(rec["dir_key"])
                brief = str(cfg.get("style") or "").strip()
                ar = str(cfg.get("aspect_ratio") or "").strip()
                if ar:
                    brief = f"{brief}，画面比例：{ar}" if brief else f"画面比例：{ar}"
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"读取项目 config.style/aspect_ratio 失败（忽略）：{e}")
            brief = ""
    # 2026-09-28 修复（「建项目时选的画面比例」必须真正生效）：
    # 三级兜底顺序**完全不变**，但「拼画幅」不再只发生在第三级 —— 只要最终 brief 里
    # **还没有任何画幅信息**（style_kit.aspect_ratio(brief) is None），且项目
    # config.aspect_ratio 非空，就统一补一句「画面比例：<ar>」。这样 plan.json / AI
    # 设定面板有 style（正常情况恒成立）时，用户在「新建项目」里手选的比例也能落到
    # 视频 / 分镜画布（两链路的 style_kit.resolve 都能解析这句）。
    # ⚠️ 硬约束：brief **已带**画幅（关键词或显式 a:b）时绝不覆盖 —— 显式意图优先。
    # 读 config 失败 fail-open：沿用原 brief、只告警不抛。
    if not style_kit.aspect_ratio(brief):
        try:
            _rec = project_store.get_project(proj)
            if _rec:
                _ar = str(project_store.read_config(_rec["dir_key"]).get("aspect_ratio")
                          or "").strip()
                if _ar:
                    brief = f"{brief}，画面比例：{_ar}" if brief else f"画面比例：{_ar}"
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"补齐项目画面比例失败（忽略）：{e}")
    return style_kit.normalize_style(brief)


