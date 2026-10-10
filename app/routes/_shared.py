# -*- coding: utf-8 -*-
import shot_key
import re
import prompt_memory
from flask import current_app, jsonify, request
from werkzeug.exceptions import HTTPException
import project_store
from config import NOVELS_DIR, PROJECT_OUTPUT_DIR
from novel_parser import get_novel
import ai_config
from llm_client import LLMClient, LLMError
from novel_parser import list_novels
import time
import autopilot
import novel_to_script
import logging
import os
import shutil
from config import AI_CONFIG_PATH, AI_MODULES, LLM_CONFIG_PATH, LLM_REQUEST_TIMEOUT
from llm_client import FailoverLLMClient


import qc_client
from config import QC_CONFIG_PATH

from config import DUB_DIR

import ai_chat
import style_kit
import threading
from config import AI_SETTINGS_PATH, COMFYUI_OUTPUT_DIR, FINAL_DIR, QC_DIR, UPSCALE_DIR, VIDEOS_DIR
from flask import abort, send_file
from upscale_client import UpscaleError

import video_watermark
from config import WATERMARK_CONFIG_PATH
import analytics
import task_store
from config import TASKS_DB_PATH

import json
import pipeline
import preview_gate
from config import SCRIPT_DIR
from upscale_client import probe_video as probe_video_info

from comfyui_client import ComfyUIClient

import quality_stage

# ⭐ 2026-10-10 拆分第 1 步：日志器 + 文件搬移已上移到 app/shared_base.py。
# 为什么先抽这三个：它们**零业务依赖**（只用标准库 + flask），且 _app_logger 是
# 被本文件 17 个函数依赖的地基。在这里**再导出**，55 个导入点一个都不用改，零行为变化。
from shared_base import _app_logger, _move_with_retry, _trash_move  # noqa: F401  再导出
# ⭐ 2026-10-10 拆分第 2 步：HTTP 边界工具（错误归一化 / 取 body / 上传名净化 /
# 目录穿越防护的 send_file）上移到 app/shared_web.py，同样只在这里再导出。
from shared_web import _body, _friendly_error, _safe_upload_name, _serve_safe  # noqa: F401  再导出
# ⭐ 2026-10-10 拆分第 3 步：AI 客户端构造与前置门禁上移到 app/shared_ai.py。
from shared_ai import (AI_MODULE_LABEL, _ai_client_for_module, _ai_gate_or_400,  # noqa: F401  再导出
                       _ai_guide_response, _current_llm_client, _optional_llm_client)

def _autopilot_guard(fn):
    """统一异常兜底：托管接口不应把 500 抛给前端，而是返回可读错误

    注意不要把客户端错误（HTTPException，例如请求体不是合法 JSON 时
    werkzeug 抛出的 400 BadRequest）误判成服务端 500——否则前端会看到
    「500 服务内部错误」，而真实原因是自己发了个畸形请求，排查方向会被带偏。
    """
    def _wrap(*a, **k):
        try:
            return fn(*a, **k)
        except KeyError as e:
            return jsonify({"success": False, "error": f"对象不存在：{e}"}), 404
        except HTTPException as e:
            # 保留 werkzeug 原本的语义状态码（400/404/405…），不要降级成 500
            return jsonify({
                "success": False,
                "error": e.description or e.name,
            }), (e.code or 400)
        except Exception as e:  # noqa: BLE001
            _app_logger().exception("托管接口异常")
            return jsonify({"success": False, "error": _friendly_error(e)}), 500
    _wrap.__name__ = fn.__name__
    return _wrap
def _prompt_memory_dead_count() -> int:
    """死教训数（use_count==0 的条数）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("dead_lessons") or 0)
    except Exception:  # noqa: BLE001
        return 0
def _prompt_memory_used_total() -> int:
    """累计被生成链路召回次数（所有教训 use_count 之和）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("used_total") or 0)
    except Exception:  # noqa: BLE001
        return 0
def _prompt_memory_view(kind: str = "", limit: int = 50) -> dict:
    """真实质检教训库的只读视图（供记忆页展示）"""
    try:
        m = prompt_memory.get_memory(PROJECT_OUTPUT_DIR)
        st = m.stats()
        return {
            "total": st.get("total", 0),
            "by_kind": st.get("by_kind") or {},
            "path": st.get("path", ""),
            "lessons": m.list(kind=kind, limit=limit),
        }
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("读取质检教训库失败：%s", e)
        return {"total": 0, "by_kind": {}, "path": "", "lessons": [], "error": str(e)}
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
def _resolve_continuity_key(novel_id):
    """小说 → (meta, 项目记录, 项目键)，供连贯性查询接口复用"""
    meta = get_novel(NOVELS_DIR, novel_id)
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    return meta, proj, _novel_key(meta, proj["dir_key"] if proj else None)
UPLOAD_TMP_DIR = os.path.join(NOVELS_DIR, "_uploads")
def _novels_stats(project_ref: str = None, include_unbound: bool = True):
    items = list_novels(NOVELS_DIR)
    # 标注每部小说当前归属的项目（一部小说 = 一个独立项目）
    for m in items:
        rec = project_store.find_by_novel(m.get("novel_id") or m.get("id"))
        m["project_id"] = (rec or {}).get("id", "")
        m["project_key"] = (rec or {}).get("dir_key", "")
        m["project_name"] = (rec or {}).get("name", "")
        m["bound"] = bool(rec)
    if project_ref:
        rec = project_store.get_project(project_ref)
        pid = (rec or {}).get("id") or project_ref
        filtered = [m for m in items if m["project_id"] == pid
                    or (include_unbound and not m["project_id"])]
    else:
        filtered = items
    return {
        "count": len(filtered),
        "total_chars": sum(int(m.get("char_count") or 0) for m in filtered),
        "novels": filtered,
        "all_count": len(items),
    }
def _estimate_subchunks(char_count: int) -> int:
    """不读全文的二次分块数量估算（用于章节列表）"""
    return novel_to_script.estimate_subchunks(char_count)
EPISODE_BATCH_LIMIT = 30          # 单次批量生成集数上限（保护后台任务）
def _episode_units_for_chapters(novel_meta: dict, chapters: list) -> list:
    """把「用户选中的章」展开成**拍摄单元**（超长章会拆成多集）。

    ⚠️ 单元编号必须基于**全量章节**展开（口径 = ``autopilot.episode_units``），
    不能用传入的子集 —— 否则同一章在「手动选集生成」与「托管」两条链路上会拿到
    不同的集号，产物（``第N集.json`` / 成片 / 验收记录）互相错位。
    """
    selected = [int(c.get("index") or 0) for c in (chapters or [])]
    if not selected:
        return []
    try:
        all_chapters, text = autopilot.chapters_and_text(novel_meta)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("章节列表读取失败（按一章一集处理）：%s", e)
        all_chapters, text = [], ""
    if all_chapters:
        try:
            units = autopilot.episode_units(all_chapters, {"episodes": selected}, text)
            if units:
                return units
        except Exception as e:  # noqa: BLE001
            _app_logger().warning("拆章失败（按一章一集处理）：%s", e)
    # 兜底：拿不到全量章节时退回「一章一集」（与历史行为一致）
    return [{"episode_no": int(c.get("index") or i + 1),
             "chapter_index": int(c.get("index") or i + 1),
             "part": 1, "parts": 1, "chapter": c}
            for i, c in enumerate(chapters or [])]
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
_shot_seq = shot_key.shot_seq


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


def _qc_load_cfg() -> dict:
    return qc_client.load_config(QC_CONFIG_PATH)


def _dub_audio_url(project_name: str, rel_path: str) -> str:
    rel = os.path.relpath(rel_path, _dub_project_dir(project_name)).replace(os.sep, "/")
    return f"/api/tts/file/{project_name}/{rel}" if not rel.startswith("..") else ""


def _dub_project_dir(project_name: str) -> str:
    d = os.path.join(DUB_DIR, project_name)
    os.makedirs(d, exist_ok=True)
    return d


COMFY_VIDEO_DIRS = [("ComfyUI片段", os.path.join(COMFYUI_OUTPUT_DIR, "video")),
                    ("ComfyUI超分", os.path.join(COMFYUI_OUTPUT_DIR, "upscale"))]


UPSCALE_URL_PREFIXES = [("/api/final/", FINAL_DIR),
                        ("/api/videos/", VIDEOS_DIR),
                        ("/api/upscale/", UPSCALE_DIR)]


_TASK_STATE_KEEP_DONE = 40


_TASK_TERMINAL_STATUSES = ("completed", "done", "failed", "error", "cancelled")


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


def _comfy_view_url(filename: str, subfolder: str = "") -> str:
    """生成后端代理 URL（/api/upscale/comfyview），实际播放时 302 到 ComfyUI /view"""
    from urllib.parse import quote
    return (f"/api/upscale/comfyview?filename={quote(filename)}"
            f"&subfolder={quote(subfolder)}")


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


def _prune_task_registry(registry: dict) -> int:
    """清理一个任务注册表里超量的终态条目，返回清理条数（须持有该注册表的锁）"""
    if not isinstance(registry, dict):
        return 0
    _done = sum(1 for v in registry.values()
                if isinstance(v, dict)
                and str(v.get("status") or "") in _TASK_TERMINAL_STATUSES)
    _excess = _done - _TASK_STATE_KEEP_DONE
    if _excess <= 0:
        return 0
    _pruned = 0
    for _k in list(registry):
        if _excess <= 0:
            break
        _v = registry.get(_k)
        if isinstance(_v, dict) and \
                str(_v.get("status") or "") in _TASK_TERMINAL_STATUSES:
            registry.pop(_k, None)
            _excess -= 1
            _pruned += 1
    return _pruned


def _qc_gate(verdict: dict) -> dict:
    """统一质检入库闸门（P0）：ok=false 或 不达标 一律不得静默入库。
    - skipped=True   → 质检未执行（总开关/类型开关关闭、接口未配置），按「放行」处理并明确标注；
    - ok=False       → 质检调用异常，结果不可判定，一律阻断（不得静默入库）；
    - accepted=False → 不通过；命中关键缺陷时 blocked=True（关键缺陷阻断）。

    P0 加固（不盲信 verdict.accepted）：无论上游 verdict 的 accepted / passed / blocked
    给什么值，本函数都会用本地关键缺陷词表对 issues（并集上游显式 critical_issues）做一次
    独立复核；一旦命中关键缺陷（画面崩坏 / 拼接 / 人物重复 等），**强制阻断**，不得因为
    模型自评 accepted=True 而放行。放行条件是 accepted 与 passed **同时**为真（任一为假即阻断）。
    """
    verdict = verdict or {}
    if verdict.get("skipped"):
        return {"accept": True, "blocked": False, "skipped": True, "label": "质检未执行",
                "reason": verdict.get("reason") or "质检未执行（跳过）", "critical_issues": []}
    if not verdict.get("ok"):
        # P0-2：区分「接口级故障」与「内容不合格」。
        # interface_fault=True（鉴权 401/403、超时、网络抖动、未配置 key）= 根本没拿到
        # 模型判定，**不等于**产物不合格——ComfyUI 已出好的图/片不能因质检 key 失效被丢弃。
        # 此时 fail-open：放行已产出资产 + 响亮告警；但若客观层已命中致命缺陷
        # （全黑/无音轨等确定性闸门，见 critical_issues）仍强制阻断，不放行真坏帧。
        if verdict.get("interface_fault"):
            crit = [str(x) for x in (verdict.get("critical_issues") or [])]
            if crit:
                return {"accept": False, "blocked": True, "skipped": False,
                        "fault_open": False, "label": "客观层致命缺陷（AI 质检接口不可用）",
                        "reason": "；".join(crit[:3]), "critical_issues": crit}
            _app_logger().warning(
                "质检接口故障，已 fail-open 放行本资产（结果不可判定）：%s",
                verdict.get("error") or "质检接口不可用")
            return {"accept": True, "blocked": False, "skipped": False,
                    "fault_open": True, "label": "质检接口故障·已放行",
                    "reason": (verdict.get("error") or "质检接口不可用")
                              + "（接口级故障，未做内容判定，已放行）",
                    "critical_issues": []}
        return {"accept": False, "blocked": True, "skipped": False, "label": "质检调用异常",
                "reason": verdict.get("error") or "质检调用失败，结果不可判定", "critical_issues": []}

    # ---- 独立复核：本地词表命中 ∪ 上游显式 critical_issues（不依赖模型自评结论） ----
    local_hits = qc_client.find_critical_issues(verdict.get("issues") or [])
    declared = [str(x) for x in (verdict.get("critical_issues") or [])]
    crit = list(dict.fromkeys(list(local_hits) + declared))
    if crit:
        _app_logger().warning(f"质检闸门独立复核命中关键缺陷，强制阻断：{crit[:3]}")
        return {"accept": False, "blocked": True, "skipped": False,
                "label": "关键缺陷阻断（独立复核）" if local_hits else "关键缺陷阻断",
                "reason": verdict.get("reason") or ("命中关键缺陷：" + "；".join(crit[:3])),
                "critical_issues": crit}

    accepted = verdict.get("accepted")
    passed = verdict.get("passed")
    if accepted is None:
        accepted = bool(passed)
    if passed is None:
        passed = bool(accepted)
    if not (bool(accepted) and bool(passed)):
        if verdict.get("style_mismatch"):
            return {"accept": False, "blocked": bool(verdict.get("blocked")),
                    "skipped": False, "style_blocked": True, "label": "风格不达标",
                    "reason": verdict.get("reason") or "画面风格与目标风格不符",
                    "critical_issues": [],
                    "style_issues": verdict.get("style_issues") or []}
        return {"accept": False, "blocked": bool(verdict.get("blocked")), "skipped": False,
                "label": "质检不达标", "reason": verdict.get("reason") or "质检未达标",
                "critical_issues": []}
    return {"accept": True, "blocked": False, "skipped": False, "label": "质检达标",
            "reason": verdict.get("reason") or "", "critical_issues": []}


def _qc_record(project_name: str, kind: str, shot_id, payload: dict) -> str:
    """写一条质检/重试历史（失败也不影响主流程）"""
    try:
        return qc_client.append_history(QC_DIR, project_name, kind, shot_id, payload)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"质检历史写入失败（忽略）：{e}")
        return ""


def _qc_record_verdict(project_name: str, kind: str, shot_key, stage: str,
                       attempt: int, seed, file_path: str, verdict: dict,
                       extra: dict = None, style: str = "") -> dict:
    """把一次质检结论整理成历史记录并落盘，返回该记录（含 history_file）

    style：本次质检所用的目标风格串。连同 style_mismatch / style_issues 一并落盘，
    供教训库识别「风格不达标」并触发改写提示词重生成。
    """
    rec = {"attempt": attempt, "seed": seed, "file": file_path, "stage": stage,
           "ok": bool(verdict.get("ok")), "passed": bool(verdict.get("passed")),
           "accepted": bool(verdict.get("accepted") if verdict.get("accepted") is not None
                            else verdict.get("passed")),
           "blocked": bool(verdict.get("blocked")),
           "score": verdict.get("score"), "reason": verdict.get("reason"),
           "issues": verdict.get("issues") or [],
           "critical_issues": verdict.get("critical_issues") or [],
           "style_mismatch": bool(verdict.get("style_mismatch")),
           "style_issues": verdict.get("style_issues") or [],
           "style": style_kit.normalize_style(style),
           "error": verdict.get("error"), "latency_ms": verdict.get("latency_ms"),
           # P0-2：接口级故障（鉴权/超时/网络）标记，供 _qc_summary 区分「故障放行」与「内容不合格」
           "interface_fault": bool(verdict.get("interface_fault")),
           # ★ 二次复核留档：首次判不过时用同一张图再判一次（判官抖动实测极大）。
           #   落盘后可直接统计「多少重跑是被复核拦下来的」，用于评估该机制收益。
           "recheck": verdict.get("recheck") or None,
           "recheck_first": verdict.get("recheck_first") or None}
    if extra:
        rec.update(extra)
    rec["history_file"] = _qc_record(project_name, kind, shot_key, rec)
    return rec




def _upscale_resolve_comfyview(query: dict) -> str:
    """解析 /api/upscale/comfyview?... 形式的 ComfyUI 产出视频为本地绝对路径"""
    filename = (query.get("filename") or "").replace("\\", "/").lstrip("/")
    subfolder = (query.get("subfolder") or "").replace("\\", "/").strip("/")
    if not filename or ".." in filename.split("/") or ".." in subfolder.split("/"):
        raise UpscaleError("非法的 ComfyUI 文件参数")
    candidate = os.path.abspath(os.path.join(COMFYUI_OUTPUT_DIR, subfolder, filename))
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if not candidate.startswith(root + os.sep):
        raise UpscaleError("非法路径：不允许跳出 ComfyUI 输出目录")
    if not os.path.exists(candidate):
        raise UpscaleError(f"ComfyUI 产出文件不存在: {candidate}")
    return candidate


def _upscale_resolve_video(data: dict) -> str:
    """解析待超分视频的真实本地路径：支持 video_path（绝对路径）或 video_url（/api/... 前缀）"""
    video_path = (data.get("video_path") or "").strip()
    if video_path:
        video_path = os.path.abspath(video_path)
        if not os.path.exists(video_path):
            raise UpscaleError(f"视频文件不存在: {video_path}")
        # 审计 P2-4（2026-09-29）：绝对路径输入限定在 output/ 与 ComfyUI 输出目录内。
        # URL 分支本就有目录边界，绝对路径分支此前没有 —— 零鉴权部署下等于
        # 「任意磁盘视频文件间接读取」（送 ComfyUI 渲染、产物可回看）。normcase
        # 对齐大小写不敏感文件系统的路径比较。
        _vp_norm = os.path.normcase(video_path)
        _allowed_roots = (os.path.abspath(PROJECT_OUTPUT_DIR),
                          os.path.abspath(COMFYUI_OUTPUT_DIR))
        if not any(_vp_norm.startswith(os.path.normcase(r + os.sep))
                   for r in _allowed_roots):
            raise UpscaleError("非法路径：video_path 仅允许 output/ 或 ComfyUI 输出目录内的文件")
        return video_path

    url = (data.get("video_url") or "").strip()
    if url.startswith("/api/upscale/comfyview"):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        return _upscale_resolve_comfyview({k: v[0] for k, v in qs.items()})
    url = url.split("?")[0]
    if url:
        for prefix, base in UPSCALE_URL_PREFIXES:
            if url.startswith(prefix):
                rel = url[len(prefix):]
                candidate = os.path.abspath(os.path.join(base, rel))
                if not candidate.startswith(os.path.abspath(base)):
                    raise UpscaleError("非法路径：不允许跳出输出目录")
                if not os.path.exists(candidate):
                    raise UpscaleError(f"URL 对应文件不存在: {candidate}")
                return candidate
        raise UpscaleError(f"不支持的视频 URL 前缀: {url}")

    raise UpscaleError("请提供 video_path（绝对路径）或 video_url（如 /api/final/<项目>/<文件>）")


def _upscale_url_for_path(path: str) -> str:
    """把输出目录下的绝对路径反查为可播放 URL（用于前端对比预览）"""
    try:
        p = os.path.abspath(path)
    except Exception:
        return ""
    for prefix, base in UPSCALE_URL_PREFIXES:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            return prefix + rel
    for _label, base in COMFY_VIDEO_DIRS:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            sub = os.path.relpath(b, os.path.abspath(COMFYUI_OUTPUT_DIR)).replace(os.sep, "/")
            return _comfy_view_url(rel, "" if sub == "." else sub)
    # ComfyUI 侧任意子目录产出（video / v5video / upscale / 自定义工作流目录等）：
    # 统一走 comfyview 代理，保证「超分前」对比预览有可播放地址
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if p.startswith(root + os.sep):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        return _comfy_view_url(os.path.basename(rel), os.path.dirname(rel).replace("\\", "/"))
    return ""


upscale_lock = threading.Lock()


upscale_tasks = {}



def _wm_load_cfg() -> dict:
    return video_watermark.load_config(WATERMARK_CONFIG_PATH)


def _task_analytics_hook(task: dict, event: str) -> None:
    try:
        analytics.record_from_task(task, analytics_kind=task.get("kind"))
    except Exception as e:  # noqa: BLE001  统计失败不得影响任务
        _app_logger().warning(f"任务统计写入失败（忽略）：{e}")


def _task_queue_status() -> dict:
    """TaskQueue 状态 + 「未接线」显式标注（N2，2026-09-22 复验）。

    ``TaskQueue.submit`` 在本项目**没有生产调用方**：单 GPU 并发由 ``gpu_task_gate``
    的进程级 Semaphore 承担（见 ``gpu_task_gate.py`` 模块头「不做什么」）。D-07 给
    submit 加的背压/去重是该模块**自身契约**的加固，供嵌入使用与测试。这里显式标注
    ``wired=False``，避免 ``/api/status`` 的 ``task_queue`` 字段让调用方误以为它是
    生产并发闸门（即「已修但不可达」的假象）。

    既有字段（running/concurrency/queued/max_queue/pending/current/current_elapsed_sec）
    原样保留，``wired`` / ``note`` 均为**新增**字段。
    """
    try:
        st = dict(task_queue.status())
    except Exception as e:  # noqa: BLE001  可观测性接口自身绝不能把 /api/status 打挂
        return {"wired": False, "error": f"{type(e).__name__}: {e}"}
    st["wired"] = False
    st["note"] = ("本进程 GPU 并发由 gpu_gate 承担；TaskQueue.submit 未接线"
                  "（D-07 加固属模块自身契约，非生产路径）")
    return st


task_queue = task_store.get_queue(TASKS_DB_PATH)


def _episode_video_stats(project_name: str, episode_no) -> dict:
    """该集镜头视频就绪度：剧本镜头数 vs 已落盘视频数（>1KB 才算数）"""
    script = _load_script_for(project_name, episode_no)
    shots = [s for s in (script.get('shots') or []) if isinstance(s, dict)]
    d = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
    ready = 0
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if not fn.lower().endswith('.mp4'):
                continue
            try:
                if os.path.getsize(os.path.join(d, fn)) > 1024:
                    ready += 1
            except OSError:
                continue
    return {"total": len(shots), "ready": ready, "dir": d}


def _load_legacy_flat_script(project_name: str) -> dict:
    """回退：读取旧版扁平命名的剧本（SCRIPT_DIR/<name>_<时间戳>.json）。

    仅在现行目录布局读不到剧本时调用，因此不会遮蔽正常的第N集.json。
    按修改时间倒序取第一份「含 shots」的文件，避免命中空壳/中间态产物。
    """
    try:
        names = os.listdir(SCRIPT_DIR)
    except OSError:
        return {}
    cands = []
    for fn in names:
        if not fn.lower().endswith(".json"):
            continue
        stem = fn[:-5]
        # 允许 <name>_<时间戳> 与 <name> 本身（例如「剑心初醒_兼容版」）
        if stem != project_name and not stem.startswith(project_name + "_"):
            continue
        path = os.path.join(SCRIPT_DIR, fn)
        try:
            cands.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _, path in sorted(cands, reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"遗留剧本读取失败 {path}：{e}")
            continue
        if isinstance(data, dict) and (data.get("shots") or data.get("episode_no")):
            _app_logger().info("剧本回退：%s 使用遗留扁平剧本 %s", project_name, os.path.basename(path))
            return data
    return {}


def _load_script_for(project_name: str, episode_no=None) -> dict:
    """按项目名（+可选集号）读取剧本；缺集号时取该项目第一集

    兼容两种历史布局（否则「迁移项目」会永远读不到剧本）：
      A. 现行：SCRIPT_DIR/<project_key>/第N集.json
      B. 迁移遗留：SCRIPT_DIR/<name>_<时间戳>.json（扁平，无子目录）
    遗留项目在项目索引里登记着 episode_count（例如 10），但按 A 找不到任何一集，
    于是分镜画布 / 导出 / 质检等全部读到空数据，界面显示「10 集 · 0 分镜」。
    这里在 A 落空时回退到 B，并优先取时间戳最新的一份。
    """
    key = project_store.safe_key(project_name)
    script = None
    if episode_no:
        try:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        except Exception as e:  # noqa: BLE001
            _app_logger().warning(f"剧本读取失败（第{episode_no}集）：{e}")
    if not script:
        try:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
        except Exception:  # noqa: BLE001
            eps = []
        if eps:
            first = eps[0]
            epno = first if isinstance(first, int) else (first.get("episode_no") or 1)
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, epno)
    if not script:
        script = _load_legacy_flat_script(project_name)
    return script or {}


def register_final_deliverable(project_name: str, episode_no, video_path: str,
                               meta: dict = None) -> dict:
    """把整集成片登记进「待验收」队列（幂等）。

    硬闸门（不满足就完全不登记，避免验收页被垃圾塞满）：
      0) 成片不存在 / < 100KB；1) 探不到时长或 < 2s。

    软闸门（镜头覆盖）：剧本镜头数 vs 已落盘镜头视频数。
    这里刻意不做「时长 >= 镜头数 x N 秒」的硬判定 —— 漫剧单镜常常不到 1s
    （实测 6 镜合并成片只有 4.46s），按时长否决会把真成片误判成半成品。
    镜头不齐时仍然登记，但在 meta 里打 `incomplete_shots` + `warning`，
    让「成品验收」页能显示「可能不完整」提醒，用户可据此打回。

    返回 {"registered": bool, "reason": str, "stats": {...}, "item": {...}}
    """
    # 铁律（2026-09-29）：预演产物**永不可交付**。这里给一个**友好拒绝**（不抛异常），
    # 让调用方能直接把原因显示给用户；同一不变量在 pipeline.record_deliverable 上还有
    # 一道硬闸门（防绕过）。
    _ok, _why = preview_gate.deliverable_ok(video_path)
    if not _ok:
        _app_logger().error("[预演拦截] 拒绝把非正式产物登记为成片（%s）：%s",
                         os.path.basename(str(video_path or "")), _why)
        return {"registered": False, "reason": _why, "stats": {}, "preview_blocked": True}
    if not video_path or not os.path.exists(video_path):
        return {"registered": False, "reason": "成片文件不存在", "stats": {}}
    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        return {"registered": False, "reason": f"成片不可读：{e}", "stats": {}}
    if size < 100 * 1024:
        return {"registered": False, "reason": f"成片过小（{size} 字节），疑似半成品",
                "stats": {"size": size}}

    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    try:
        vinfo = probe_video_info(video_path) or {}
    except Exception:  # noqa: BLE001 - 探测失败不代表成片不可用，走宽松分支
        vinfo = {}
    duration = float(vinfo.get("duration") or 0)
    if duration and duration < 2.0:
        return {"registered": False, "reason": f"成片仅 {duration:.1f}s，疑似片段",
                "stats": {"duration": duration, "size": size}}

    stats = _episode_video_stats(project_name, ep)
    stats["size"], stats["duration"] = size, duration
    total, ready = int(stats.get("total") or 0), int(stats.get("ready") or 0)
    incomplete = bool(total > 0 and ready < total)

    item_meta = dict(meta or {})
    item_meta.setdefault("source", "final")
    item_meta.setdefault("duration_sec", round(duration, 2) if duration else None)
    item_meta.setdefault("size_bytes", size)
    item_meta["shots_total"] = total
    item_meta["shots_ready"] = ready
    if incomplete:
        item_meta["incomplete_shots"] = True
        item_meta["warning"] = (f"该集剧本 {total} 镜，仅发现 {ready} 个镜头视频，"
                                f"成片可能不完整，建议核对后再验收")
    try:
        item = pipeline.record_deliverable(project_name, ep, video_path, meta=item_meta)
    except Exception as e:  # noqa: BLE001 - 登记失败不能影响出片主流程
        _app_logger().warning(f"成片登记交付物失败（{project_name} 第{ep}集）：{e}")
        return {"registered": False, "reason": f"登记失败：{e}", "stats": stats}
    if incomplete:
        _app_logger().warning(f"成片已登记但镜头疑似不全：{project_name} 第{ep}集 "
                           f"({ready}/{total}) -> {os.path.basename(video_path)}")
        return {"registered": True,
                "reason": f"已登记（镜头覆盖 {ready}/{total}，可能不完整）",
                "stats": stats, "item": item, "incomplete": True}
    _app_logger().info(f"成片已登记待验收：{project_name} 第{ep}集 -> {os.path.basename(video_path)}")
    return {"registered": True, "reason": "已登记", "stats": stats, "item": item}


def _first_existing(*candidates):
    for c in candidates:
        if isinstance(c, str) and c and os.path.exists(c):
            return c
    return None


_shot_num_key = shot_key.norm_shot_key


comfyui_client = ComfyUIClient()


def _quality_asset_url(kind: str, project: str, name: str) -> str:
    """资产名 → 第一张可用图的 URL（front/base 优先，与生成链路取图同口径）。"""
    d = os.path.join(PROJECT_OUTPUT_DIR, "assets", kind, project, str(name))
    if not os.path.isdir(d):
        return ""
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    except OSError:
        return ""
    if not files:
        return ""
    pick = files[0]
    for prio in ("front", "base"):
        hit = next((x for x in files if x.lower().startswith(prio)), "")
        if hit:
            pick = hit
            break
    return "/api/assets/%s/%s/%s/%s" % (kind, project, name, pick)


def _quality_find_full(project: str, ep) -> str:
    """该集整集成片（播放与批准绑定都认它）；无则空串。"""
    d = _ep_read_dir(VIDEOS_DIR, project, ep)
    if not os.path.isdir(d):
        return ""
    try:
        fps = sorted(f for f in os.listdir(d)
                     if f.lower().endswith(".mp4") and "_full" in f.lower())
    except OSError:
        return ""
    if not fps:
        return ""
    try:
        tag = "ep%02d" % int(ep)
    except (TypeError, ValueError):
        tag = ""
    for f in fps:
        if tag and f.lower().startswith(tag):
            return os.path.join(d, f)
    return os.path.join(d, fps[0])


def _quality_find_preview(project: str, ep) -> dict:
    """该集预演产物（两级生产第一阶段的输出，不可交付）。"""
    d = _ep_read_dir(VIDEOS_DIR, project, ep)
    try:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if preview_gate.is_preview_path(fn):
                    p = os.path.join(d, fn)
                    return {"exists": True, "name": fn, "url": _quality_video_url(p)}
    except OSError as e:                                     # noqa: BLE001
        _app_logger().warning("扫描预演产物失败：%s", e)
    return {"exists": False, "name": "", "url": ""}


def _quality_state_view(project: str, ep) -> dict:
    """四层状态 + 发布就绪判定 + 批准是否已被重渲作废（界面的唯一口径）。"""
    state = quality_stage.load_state(project, ep)
    full = _quality_find_full(project, ep)
    stages, stale, blockers_extra = {}, {}, []
    for s in quality_stage.STAGES:
        e = (state.get("stages") or {}).get(s) or {}
        stages[s] = {"status": e.get("status") or "pending",
                     "name": e.get("name") or quality_stage.STAGE_NAMES[s],
                     "label": e.get("label") or quality_stage.STAGE_LABELS[s],
                     "at": e.get("at") or "", "note": e.get("note") or "",
                     "has_binding": bool(e.get("binding"))}
        # 批准已过期检测：C/D 通过过、但产物已不是批准时那一份
        if s in ("C", "D") and stages[s]["status"] == "passed" and e.get("binding"):
            bstatus, breason = quality_stage.check_stage_binding(state, s, full)
            if bstatus == "invalid":
                stale[s] = breason
                blockers_extra.append("%s(%s) 批准已失效：%s"
                                      % (s, quality_stage.STAGE_NAMES[s], breason))
    _ready, blockers = quality_stage.release_ready(state)
    blockers = list(blockers) + blockers_extra
    ready = not [r for r in blockers if not r.startswith("提示：")]
    return {"stages": stages, "release": {"ready": ready, "blockers": blockers},
            "stale": stale, "updated_at": state.get("updated_at") or ""}


def _quality_video_url(local_path: str) -> str:
    """本地视频路径 → /api/videos URL；出了 VIDEOS_DIR 就不给 URL（防穿越）。"""
    if not local_path:
        return ""
    try:
        rel = os.path.relpath(local_path, VIDEOS_DIR)
    except (ValueError, TypeError):
        return ""
    if rel.startswith(".."):
        return ""
    return "/api/videos/" + rel.replace(os.sep, "/")
