# -*- coding: utf-8 -*-
"""角色设定（增删改 / 参考图 / 提示词） API 蓝图（Blueprint 拆分第十三批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @characters_bp.route。
日志器用上下文安全的 _app_logger()。
"""
import json
import os
import shutil
import time

from flask import Blueprint, jsonify, request, send_file

from character_manager import CharacterManager
from routes._shared import PROJECT_OUTPUT_DIR, _app_logger, _autopilot_guard, _project_or_400

characters_bp = Blueprint('characters', __name__)

# ⭐ 2026-10-09：角色卡的真实数据源是**剧本顶层 characters**（实测 CharacterManager 的
#    characters.json 从未被本流水线创建）。下面三个助手统一读写剧本，并在覆盖写前备份。
_CHAR_FIELD_MAP = {
    # 对外字段（沿用 CharacterManager 口径，AI 工具与老调用方都按这套传） → 剧本字段
    "name": "name",
    "role": "identity",          # 「定位」在剧本里叫 identity
    "description": "appearance",  # 「描述」对应静态外貌 appearance
    "outfit": "current_outfit",
    # status：剧本里没有对应字段，显式忽略（不静默写脏数据）
}


def _script_json_path(project_name: str, episode_no: int = 1) -> str:
    """剧本 JSON 绝对路径（与 /asset-definitions 同一套定位逻辑）。"""
    try:
        from novel_to_script import episode_script_path
        return episode_script_path(project_name, episode_no) or ""
    except Exception:  # noqa: BLE001
        return os.path.join(PROJECT_OUTPUT_DIR, "scripts", project_name,
                            f"第{episode_no}集.json")


def _load_script(project_name: str, episode_no: int = 1):
    """读剧本；不存在返回 (None, path)。"""
    sp = _script_json_path(project_name, episode_no)
    if not sp or not os.path.isfile(sp):
        return None, sp
    try:
        with open(sp, "r", encoding="utf-8") as f:
            return json.load(f), sp
    except (OSError, ValueError) as e:
        _app_logger().warning("[characters] 剧本读取失败 %s：%s", sp, e)
        return None, sp


def _save_script(script: dict, sp: str) -> bool:
    """覆盖写剧本（**先备份**）。返回是否成功。"""
    try:
        bak = f"{sp}.bak_charupdate_{time.strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(sp, bak)
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(script, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sp)
        _app_logger().info("[characters] 剧本已更新（备份 %s）", os.path.basename(bak))
        return True
    except (OSError, ValueError) as e:
        _app_logger().error("[characters] 剧本写回失败：%s", e)
        return False


@characters_bp.route('/api/characters', methods=['GET'])
@_autopilot_guard
def api_list_characters():
    """列出项目所有角色"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    # ⭐ 2026-10-09 修复：原先只读 CharacterManager 的 characters.json，
    #    但该注册表**从未被本流水线创建**（实测 output 下 0 个 characters.json）→
    #    永远返回空字典：前端 AudioTab 的音色面板看不到任何角色，
    #    AI 总控的 list_characters 工具也拿不到数据。
    #    现改为读**剧本顶层 characters**（与 /asset-definitions 同源），
    #    并把注册表里多出来的角色合并进来（老项目不丢数据）。
    #    ⚠️ 响应形状保持 `{success, characters: {<名称>: {...}}}`（**字典**，前端按字典消费）。
    try:
        _ep = int(request.args.get('episode_no') or 1)
    except (TypeError, ValueError):
        _ep = 1
    characters = {}
    try:
        from novel_to_script import episode_script_path
        _sp = episode_script_path(project_name, _ep)
    except Exception:  # noqa: BLE001
        _sp = os.path.join(PROJECT_OUTPUT_DIR, "scripts", project_name, f"第{_ep}集.json")
    if _sp and os.path.isfile(_sp):
        try:
            with open(_sp, "r", encoding="utf-8") as _f:
                for _c in (json.load(_f).get("characters") or []):
                    _nm = str(_c.get("name") or "").strip()
                    if _nm:
                        characters[_nm] = _c
        except (OSError, ValueError) as _e:
            _app_logger().warning("[characters] 剧本读取失败：%s", _e)
    try:
        _mgr = CharacterManager(project_name,
                                os.path.join(PROJECT_OUTPUT_DIR, project_name))
        for _cid, _cc in (_mgr.get_all_characters() or {}).items():
            characters.setdefault(_cid, _cc)
    except Exception:  # noqa: BLE001  注册表缺失/损坏不影响剧本来源
        pass
    return jsonify({"success": True, "characters": characters})
# ⚠️ 2026-10-09 已删除：POST /api/characters（新增角色）
#    原因：该端点只写 CharacterManager 的 characters.json —— 实测 output 下 0 个该文件，
#    注册表从未被本流水线创建。新增角色由剧本生成链路负责；上传角色图走
#    POST /api/assets/character/upload-sheet（前端「上传设定图」已在用）。
#    删除前已核对：AI 总控工具与前端均无调用方。
@characters_bp.route('/api/characters/<char_id>', methods=['PUT'])
@_autopilot_guard
def api_update_character(char_id):
    """更新角色信息"""
    data = request.get_json(silent=True) or {}
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(data.get('project'), field_name="project")
    if err is not None:
        return err

    # ⭐ 2026-10-09：改为读写**剧本顶层 characters**（CharacterManager 注册表已废弃，
    #    它找不到角色会抛 ValueError → 500，AI 总控的 update_character 工具必然失败）。
    #    字段映射见 _CHAR_FIELD_MAP：role→identity / description→appearance /
    #    outfit→current_outfit；status 剧本无对应字段，忽略。
    script, sp = _load_script(project_name, int(data.get('episode_no') or 1))
    if script is None:
        return jsonify({"success": False, "error": "找不到剧本，无法更新角色卡"}), 404
    chars = script.get("characters") or []
    target = next((c for c in chars if str(c.get("name") or "").strip() == char_id), None)
    if target is None:
        return jsonify({"success": False,
                        "error": f"剧本里没有角色：{char_id}"}), 404
    _changed = []
    for _api_key, _script_key in _CHAR_FIELD_MAP.items():
        _val = data.get(_api_key)
        if _val is None or str(_val).strip() == "":
            continue
        target[_script_key] = str(_val).strip()
        _changed.append(f"{_api_key}→{_script_key}")
    if not _changed:
        return jsonify({"success": False, "error": "没有可更新的字段"}), 400
    if not _save_script(script, sp):
        return jsonify({"success": False, "error": "剧本写回失败（详见日志）"}), 500
    return jsonify({"success": True, "character_id": char_id,
                    "updated": _changed, "character": target})
# ⚠️ 2026-10-09 已删除：POST /<id>/reference（参考图）
#    原因：该端点只写 CharacterManager 的 characters.json —— 实测 output 下 0 个该文件，
#    注册表从未被本流水线创建。新增角色由剧本生成链路负责；上传角色图走
#    POST /api/assets/character/upload-sheet（前端「上传设定图」已在用）。
#    删除前已核对：AI 总控工具与前端均无调用方。
@characters_bp.route('/api/characters/<char_id>/prompt', methods=['GET'])
@_autopilot_guard
def api_get_character_prompt(char_id):
    """获取角色生成提示词"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err
    # ⭐ 2026-10-09：同 update —— 提示词就在剧本 characters[].reference_prompt_zh，
    #    不再走已废弃的 CharacterManager（那条路必然抛「角色不存在」）。
    script, _sp = _load_script(project_name, int(request.args.get('episode_no') or 1))
    if script is None:
        return jsonify({"success": False, "error": "找不到剧本"}), 404
    target = next((c for c in (script.get("characters") or [])
                   if str(c.get("name") or "").strip() == char_id), None)
    if target is None:
        return jsonify({"success": False, "error": f"剧本里没有角色：{char_id}"}), 404
    return jsonify({"success": True, "character_id": char_id,
                    "prompt": target.get("reference_prompt_zh") or "",
                    "prompt_en": target.get("reference_prompt_en") or ""})

@characters_bp.route('/api/characters', methods=['POST'])
@_autopilot_guard
def api_add_character():
    """添加新角色"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    name = data.get('name', '')
    role = data.get('role', '配角')
    description = data.get('description', '')
    outfit = data.get('outfit', '')

    if not name:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    char_id = mgr.add_character(name, role, description, outfit)

    return jsonify({"success": True, "character_id": char_id, "name": name})


@characters_bp.route('/api/characters/<char_id>/reference', methods=['POST'])
@_autopilot_guard
def api_upload_character_reference(char_id):
    """上传角色参考图"""
    project_name = request.form.get('project')
    view_type = request.form.get('view_type', 'front')
    file = request.files.get('image')

    if not file:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)

    # 保存上传的文件
    upload_dir = os.path.join(project_dir, "characters", "references")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1]
    filename = f"{char_id}_{view_type}{ext}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    mgr.set_reference(char_id, view_type, filepath)
    return jsonify({"success": True, "path": filepath})

