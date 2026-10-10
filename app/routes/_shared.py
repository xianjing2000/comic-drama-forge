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
# ⭐ 2026-10-10 拆分第 4 步：项目/镜头基础（反向依赖最热的 6 个实体）上移到 app/shared_project.py。
from shared_project import (_first_existing, _project_or_400, _safe_project,  # noqa: F401  再导出
                            _shot_num_key, _shot_seq, comfyui_client)
# ⭐ 2026-10-10 拆分第 6 步：质检 / 超分解析 / 任务队列三组上移到 app/ 层。
from shared_qc import (_qc_gate, _qc_load_cfg, _qc_record,  # noqa: F401  再导出
                       _qc_record_verdict)
from shared_tasks import (_TASK_STATE_KEEP_DONE, _TASK_TERMINAL_STATUSES,  # noqa: F401  再导出
                          _prune_task_registry, _task_analytics_hook,
                          _task_queue_status, task_queue)
from shared_upscale import (COMFY_VIDEO_DIRS, UPSCALE_URL_PREFIXES,  # noqa: F401  再导出
                            _comfy_view_url, _upscale_resolve_comfyview,
                            _upscale_resolve_video, _upscale_url_for_path,
                            upscale_lock, upscale_tasks)

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




def _dub_audio_url(project_name: str, rel_path: str) -> str:
    rel = os.path.relpath(rel_path, _dub_project_dir(project_name)).replace(os.sep, "/")
    return f"/api/tts/file/{project_name}/{rel}" if not rel.startswith("..") else ""


def _dub_project_dir(project_name: str) -> str:
    d = os.path.join(DUB_DIR, project_name)
    os.makedirs(d, exist_ok=True)
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























def _wm_load_cfg() -> dict:
    return video_watermark.load_config(WATERMARK_CONFIG_PATH)








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
