# -*- coding: utf-8 -*-
'''资产生成 API 蓝图（2026-10-11 从 app.py 迁出）。'''

# 由 tools/sink_routes.py 生成：URL 与响应体一字不改，只把 @app.route 换成蓝图路由；
# 共享助手从 routes/_shared.py 取（沿用既有蓝图模式）。
import logging
from flask import Blueprint, jsonify, request, send_file, abort, current_app  # noqa: F401
import os    # noqa: F401
from PIL import Image  # noqa: F401
import sys   # noqa: F401
import re    # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
import threading   # noqa: F401
import hashlib     # noqa: F401
import shutil      # noqa: F401
import random      # noqa: F401
import base64      # noqa: F401
import datetime    # noqa: F401
import traceback   # noqa: F401
import subprocess  # noqa: F401
import json  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from agent_core import logger
from asset_worker import _generate_asset_task
from character_helpers import _character_base_prompt
from character_helpers import _character_outfit_dir
from character_helpers import _find_script_character
from config import ASSET_VIEW_STEMS
from config import CHARACTERS_DIR
from config import PROJECT_OUTPUT_DIR
from episode_helpers import _OUTFIT_RECORD_FILE
from fs_atomic import atomic_write_json
from job_state import generation_state
from job_state import lock
from qc_helpers import _OUTFITS_DIRNAME
from qc_helpers import _sanitize_outfit_key
from routes._shared import UPLOAD_TMP_DIR
from routes._shared import _body
from routes._shared import _project_or_400
from routes._shared import _project_style
from routes._shared import _safe_project
from routes._shared import _safe_upload_name
from routes._shared import _serve_safe
from storyboard_helpers import _write_artifact_meta
from style_helpers import _style_aspect_guard
import sheet_split

_OUTFIT_VIEW_STEMS = ("front", "left", "back", "half")
_UPLOAD_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
def _append_outfit_prompt(base_prompt: str, outfit_desc: str) -> str:
    """把「；本套服装：{outfit_desc}」追加到角色提示词末尾（幂等）。

    幂等实现：服装段**只追加在串尾**，故追加前把已有的尾段剥掉再接新段 ——
    同一 desc 重复追加结果不变（不重复），desc 改动时旧描述被替换而非叠两段。
    """
    base = str(base_prompt or "").strip()
    desc = str(outfit_desc or "").strip()
    if not desc:
        return base
    base = re.sub(r"；本套服装：.*$", "", base).strip()
    if base:
        return f"{base}{_OUTFIT_PROMPT_MARK}{desc}"
    return f"本套服装：{desc}"
def _outfit_desc_of(outfit_dir: str) -> str:
    """该服装变体的描述：outfit.json 档案优先，回落 base.png.meta.json
    提示词里的「；本套服装：…」尾段。任何失败返回 ''（查询列表不因此报错）。"""
    try:
        rec_path = os.path.join(outfit_dir, _OUTFIT_RECORD_FILE)
        if os.path.isfile(rec_path):
            with open(rec_path, "r", encoding="utf-8") as f:
                desc = str((json.load(f) or {}).get("desc") or "").strip()
            if desc:
                return desc
    except Exception as e:  # noqa: BLE001
        logger.debug("服装档案读取失败（回落 meta sidecar）：%s", e)
    try:
        meta_path = os.path.join(outfit_dir, "base.png.meta.json")
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                prompt = str((json.load(f) or {}).get("prompt") or "")
            _m = re.search(r"；本套服装：(.+)$", prompt)
            if _m:
                return _m.group(1).strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("服装 meta 回读失败（忽略）：%s", e)
    return ""

_OUTFIT_PROMPT_MARK = "；本套服装："

logger = logging.getLogger(__name__)

assets_api_bp = Blueprint('assets_api', __name__)

@assets_api_bp.route('/api/assets/generate', methods=['POST'])
def api_generate_assets():
    """生成资产（角色/物品/场景，含多视角）"""
    # ⚠️ 这里**故意不设** AI 前置门禁：资产生成是「消费已产出的提示词 + ComfyUI 出图 +
    # 质检」的链路，全程不读 text/qc/chat 凭证（提示词由上游剧本步骤产出、随 assets 传入）。
    # 早前一版把门禁挂在这里，后果是「AI key 没配 → 连本来能出的图也一起被拦」，
    # 属于护栏误伤业务。凡是光跑 ComfyUI 就能完成的入口都不挂门禁。
    data = _body()
    asset_type = data.get('asset_type', '')  # character / item / scene
    # P2-T2：写盘路由统一走 _project_or_400（契约必填 project_name）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    assets = data.get('assets', [])
    # A-2 P0：断点续跑开关。默认 False → 已达标入库的资产跳过；显式传 true 强制重生成
    overwrite = bool(data.get('overwrite'))

    if asset_type not in ("character", "item", "scene"):
        return jsonify({"error": "asset_type 必须是 character/item/scene"}), 400
    if not assets:
        return jsonify({"error": "没有资产数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # B-12 P1-15：资产任务 ID 改用 uuid（G5 只改了分镜/视频/配音/混音，资产漏改），
    # 同秒并发请求不再互撞。
    task_id = f"{asset_type}_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": asset_type,
            "progress": 0, "total": len(assets), "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
        }

    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, assets, asset_type, project_name,
              data.get('style') or _project_style(project_name), overwrite)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "started"})


@assets_api_bp.route('/api/assets/character/upload-sheet', methods=['POST'])
def api_character_upload_sheet():
    """上传角色形象图 → 落 base.png → 本地切分三视图（零 GPU）。

    form-data: file（图片，必填）, project_name, character, outfit_key?（空=主设定）,
               overwrite?（"1"/"true" 强制覆盖）

    返回：{success, character, base, views:{front,left,back,half}, derive_error?}
      · derive_error 非空 = 图已落 base.png 但**版式不符未能切分**（下游会回落
        整图，属可用状态），此时 success 仍为 True 但带 `derive_ok: false` 提示。
      · 只有「图本身不可读 / 版式不符且 overwrite 已覆盖旧图」才 4xx/回滚。
    """
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False,
                        "error": "未收到图片，请通过 file 字段上传"}), 400
    f = files[0]
    project_name, err = _project_or_400(
        (request.form.get('project_name') or request.args.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (request.form.get('character') or request.args.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(
        request.form.get('outfit_key') or request.args.get('outfit_key'))
    _ow_raw = (request.form.get('overwrite') or request.args.get('overwrite') or '').strip()
    # 宽容布尔（与 qc_client._as_bool 同口径）：表单/query 传 "1"/"true"/"on"/"yes" 都算真
    overwrite = _ow_raw.lower() not in ("", "0", "false", "no", "off")

    # 角色名是路径段，与 api_character_outfit_generate 同一守卫口径
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    # outfit_key 传了但非法（非空且清洗后为空）→ 明确拒绝，不静默当主设定
    _ow_in = (request.form.get('outfit_key') or request.args.get('outfit_key') or '').strip()
    if _ow_in and not outfit_key:
        return jsonify({"success": False,
                        "error": "outfit_key 非法（≤40 字符，剔除 \\ / : * ? \" < > |）"}), 400

    raw_name = _safe_upload_name(f.filename)
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in _UPLOAD_IMAGE_EXTS:
        return jsonify({"success": False,
                        "error": f"不支持的图片格式 {ext or '（无扩展名）'}；"
                                 f"支持 {'、'.join(_UPLOAD_IMAGE_EXTS)}"}), 400

    asset_dir = (_character_outfit_dir(project_name, character, outfit_key) if outfit_key
                 else os.path.join(CHARACTERS_DIR, project_name, character))
    base_dst = os.path.join(asset_dir, "base.png")
    if not overwrite and os.path.isfile(base_dst) and os.path.getsize(base_dst) > 0:
        return jsonify({"success": True, "skipped": True, "character": character,
                        "outfit_key": outfit_key, "base": base_dst,
                        "message": "该角色已有设定图（base.png 已就绪）；"
                                   "如需替换请带 overwrite=true"})

    # 暂存上传件 → 校验可解码 → 统一转 PNG 落 base.png
    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(
        UPLOAD_TMP_DIR,
        f"sheet_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}")
    _old_backup = ""
    try:
        f.save(tmp_path)
        try:
            from PIL import Image
            with Image.open(tmp_path) as _im:
                _w, _h = _im.size
                _im.convert("RGB")
        except Exception as _ie:  # noqa: BLE001 不可解码的图绝不能入库
            return jsonify({"success": False,
                            "error": f"图片无法读取或已损坏：{type(_ie).__name__} {_ie}"}), 400

        os.makedirs(asset_dir, exist_ok=True)
        # overwrite 时先把旧 base 挪走做回滚点：切分失败要能把旧状复原
        if os.path.isfile(base_dst):
            _old_backup = base_dst + ".preupload.bak"
            try:
                shutil.copy2(base_dst, _old_backup)
            except OSError as _be:
                logger.warning("上传形象图：旧 base.png 备份失败（忽略）：%s", _be)
                _old_backup = ""
        with Image.open(tmp_path) as _im:
            _im.convert("RGB").save(base_dst, format="PNG")
        logger.info("[上传形象图] %s/%s%s ← %s（%dx%d）",
                        project_name, character, f" ({outfit_key})" if outfit_key else "",
                        raw_name, _w, _h)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ---- 2026-10-02 用户指定：角色图**不裁剪** → 不再切分三视图 ----
    # 角色设定图已改为英文四区 character sheet（见 comfyui_client._CHARACTER_SHEET_EN_LAYOUT），
    # 四区不对称布局无法切分 → 上传图直接落 base.png 整图，下游取整图。
    # 并清掉旧视角图（front/left/back/half），避免 _ASSET_IMG_PRIORITY 取到旧单视角。
    views: dict = {}
    try:
        sheet_split.prune_stale_views(
            asset_dir, keep=(), known=ASSET_VIEW_STEMS, logger=logger)
    except Exception as _pe:  # noqa: BLE001
        logger.warning("上传形象图：清理陈旧视角失败（忽略）：%s", _pe)

    if _old_backup:
        try:
            os.remove(_old_backup)
        except OSError:
            pass

    # 旁路元数据：显式标注「用户上传」，与模型生成的资产可区分、可追溯
    _view_gate = {"accept": True, "blocked": False, "skipped": True,
                  "label": "用户上传（未质检）",
                  "reason": "用户上传的定稿形象图，不做图片质检", "critical_issues": []}
    try:
        _write_artifact_meta(
            base_dst, kind="asset_base", project_name=project_name,
            seed=None, prompt="", workflow_key=None, qc=_view_gate,
            asset_name=character,
            extra={"asset_type": "character", "source": "user_upload",
                   "original_filename": raw_name, "outfit_key": outfit_key or None})
    except Exception as _me:  # noqa: BLE001
        logger.warning("上传形象图：元数据写入失败（忽略）：%s", _me)

    return jsonify({"success": True, "derive_ok": True, "skipped": False,
                    "character": character, "outfit_key": outfit_key,
                    "base": base_dst,
                    "views": {},
                    "view_files": {},
                    "layout_hint": "不裁剪：保留整图（character sheet 四区）",
                    "message": "已上传（不裁剪：直接使用整张 character sheet 设定图）"})


@assets_api_bp.route('/api/assets/character/outfit', methods=['POST'])
def api_character_outfit_generate():
    """生成角色服装变体（衣柜）。

    body = {project_name, character(角色名), outfit_key, outfit_desc, style?, overwrite?}
    返回 {task_id, status:"started"}（与 api_generate_assets 同构，进度轮询
    /api/generation/status/<task_id>）。已存在同 outfit_key 且 base.png 非空 →
    默认跳过（A-2 overwrite 语义，overwrite=true 强制重画）。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    outfit_key = _sanitize_outfit_key(data.get('outfit_key'))
    outfit_desc = str(data.get('outfit_desc') or '').strip()
    overwrite = bool(data.get('overwrite'))
    # 角色名是路径段，与 api_autopilot_reset_asset 同一守卫口径
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    if not outfit_key:
        return jsonify({"success": False,
                        "error": "outfit_key 不能为空（≤40 字符，剔除 \\ / : * ? \" < > |）"}), 400
    if not outfit_desc:
        return jsonify({"success": False, "error": "outfit_desc（服装描述）不能为空"}), 400

    outfit_dir = _character_outfit_dir(project_name, character, outfit_key)
    # A-2 断点续跑语义：已有同 key 且 base.png 非空 → 默认跳过（幂等入口）
    _base_png = os.path.join(outfit_dir, "base.png")
    if not overwrite and os.path.isfile(_base_png) and os.path.getsize(_base_png) > 0:
        return jsonify({"success": True, "skipped": True, "task_id": "",
                        "outfit_key": outfit_key, "character": character,
                        "message": "该服装变体已存在（base.png 已就绪）；如需重画请带 overwrite=true"})

    # 角色基础设定（剧本优先，回落主设定 meta）→ 追加服装描述（幂等）
    _base_prompt = _character_base_prompt(project_name, character)
    _merged_prompt = _append_outfit_prompt(_base_prompt, outfit_desc)
    asset = _find_script_character(project_name, character)
    asset.update({
        "name": character,
        "reference_prompt_zh": _merged_prompt,
        "prompt_zh": _merged_prompt,
    })
    if not str(asset.get("appearance") or "").strip():
        # 剧本里没有 appearance 时把合并提示词兜进去，保证 qc_desc / 性别判据有料可用
        asset["appearance"] = _merged_prompt

    # 服装档案：先落 outfit.json（查询端点回显 desc 用；写失败不影响生成 ——
    # ready 判据看 base.png，desc 还有 meta sidecar 兜底）
    try:
        os.makedirs(outfit_dir, exist_ok=True)
        atomic_write_json(os.path.join(outfit_dir, _OUTFIT_RECORD_FILE), {
            "outfit_key": outfit_key,
            "desc": outfit_desc,
            "character": character,
            "project": project_name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as _oe:  # noqa: BLE001
        logger.warning("服装变体档案写入失败（不影响生成）：%s", _oe)

    task_id = f"outfit_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": "character",
            "progress": 0, "total": 1, "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
            "project_name": project_name, "step": "asset_outfit",
            "character": character, "outfit_key": outfit_key,
        }
    # 线程 + 状态登记照抄 api_generate_assets（裸线程，资产链路不进 GPU 闸门）；
    # 生成→质检→重试→切分全链路由 _generate_asset_task 承担，变体经 sub_dir 落位
    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, [asset], "character", project_name,
              data.get('style') or _project_style(project_name), overwrite,
              os.path.join(_OUTFITS_DIRNAME, outfit_key))
    )
    thread.daemon = True
    thread.start()
    logger.info("[服装变体] 项目=%s 角色=%s 服装=%s（overwrite=%s）任务=%s 已启动",
                    project_name, character, outfit_key, overwrite, task_id)
    return jsonify({"task_id": task_id, "status": "started",
                    "outfit_key": outfit_key, "character": character})


@assets_api_bp.route('/api/assets/character/outfits', methods=['GET'])
def api_character_outfits_list():
    """列出角色服装变体（衣柜）：query = project_name, character。

    返回 [{outfit_key, desc, ready(base.png 存在且>0字节),
    views:{front/left/back/half 是否存在}}]。outfits 目录不存在 / 任何读取异常
    → 空数组（fail-open，绝不抛错）。
    """
    project_name = _safe_project((request.args.get('project_name') or '').strip())
    character = (request.args.get('character') or '').strip()
    if not project_name or not character or '/' in character or '\\' in character \
            or character in ('.', '..'):
        return jsonify({"success": False, "error": "project_name 与 character 必填",
                        "outfits": []}), 400
    outfits_root = _character_outfit_dir(project_name, character)
    out = []
    if not os.path.isdir(outfits_root):
        # 目录不存在 = 该角色还没做过服装变体（正常态，不是错误）
        return jsonify({"success": True, "outfits": out})
    try:
        # 纵深防御（安全复查 2026-10-02）：listdir 条目本不可能携带路径分隔符
        # （listdir 不返回 ./..，文件名也无法含 \ /），此处仍显式校验 realpath
        # 未越出 outfits_root，阻断符号链接等非常规文件系统状态造成的目录逃逸。
        _root_real = os.path.realpath(outfits_root)
        for _dir_name in sorted(os.listdir(outfits_root)):
            _od = os.path.join(outfits_root, _dir_name)
            if not os.path.isdir(_od):
                continue
            if not os.path.realpath(_od).startswith(_root_real + os.sep):
                logger.warning("[服装变体] 异常目录项已跳过（越界防护）：%s", _dir_name)
                continue
            _base_png = os.path.join(_od, "base.png")
            _ready = os.path.isfile(_base_png) and os.path.getsize(_base_png) > 0
            _views = {}
            for _stem in _OUTFIT_VIEW_STEMS:
                _vp = os.path.join(_od, f"{_stem}.png")
                _views[_stem] = os.path.isfile(_vp) and os.path.getsize(_vp) > 0
            out.append({
                "outfit_key": _dir_name,
                "desc": _outfit_desc_of(_od),
                "ready": _ready,
                "views": _views,
            })
    except Exception as e:  # noqa: BLE001  目录枚举失败按空数组处理（fail-open）
        logger.warning("服装变体列表读取失败（返回空数组）：%s", e)
        out = []
    return jsonify({"success": True, "outfits": out})


@assets_api_bp.route('/api/assets/<path:filename>')
def api_asset_file(filename):
    """提供资产文件访问

    ⭐ 2026-10-05 资产自动刷新：资产图会被**原地覆盖重生成**（路径不变），必须让浏览器
    每次回源校验，否则轮询拿到新 JSON 后 <img> 仍显示旧缓存图，用户以为「必须手动刷新」。
    与前端 ?v=<mtime> 双重保险：前端换 URL 触发重挂（主），本处 no-cache 兜底回源。
    """
    resp = _serve_safe(os.path.join(PROJECT_OUTPUT_DIR, "assets"), filename)
    try:
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    except Exception:  # noqa: BLE001  abort 响应(403/404) 无 headers，忽略
        pass
    return resp
