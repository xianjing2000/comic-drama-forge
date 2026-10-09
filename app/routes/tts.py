# -*- coding: utf-8 -*-
"""TTS 配音（音色库 / 环境 / 试听） API 蓝图（Blueprint 拆分第九批，2026-10-08）。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @tts_bp.route。
跨域助手在 routes/_shared.py；本模块只放该域自己的东西。
"""
import json
import os
import time

from flask import Blueprint, current_app, jsonify, request, send_file

tts_bp = Blueprint('tts', __name__)

import audio_qc
import prompt_qc
import tts_client
from config import TTS_DEFAULT_PARAMS
from flask import abort
from tts_client import QwenTTSClient, TTSError, VOICE_BANK_EXTS, check_environment as tts_env_check, clean_line_text, clone_available as tts_clone_available, default_voice_map, find_voice_bank_ref, list_voice_bank, list_voices as tts_list_voices, load_voice_map, normalize_voice, probe_audio as probe_audio_info, save_voice_bank_ref, save_voice_map, voice_bank_dir
from routes._shared import DUB_DIR, UPLOAD_TMP_DIR, _body, _dub_audio_url, _dub_project_dir, _project_or_400, _qc_load_cfg, _safe_project, _safe_upload_name, qc_client, _app_logger
@tts_bp.route('/api/tts/env', methods=['GET'])
def api_tts_env():
    """配音环境自检：ComfyUI 在线 / Qwen-TTS 节点 / 模型权重 / 可用音色"""
    env = tts_env_check()
    env["voices"] = tts_list_voices()
    env["dub_dir"] = DUB_DIR
    env["out_dir"] = _dub_project_dir(_safe_project(request.args.get('project_name') or 'project'))
    return jsonify(env)
@tts_bp.route('/api/tts/voice-map', methods=['POST'])
def api_tts_voice_map_save():
    """保存角色音色配置（所有角色固定 speaker/seed，保证全剧音色一致）"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    voice_map = data.get('voice_map')
    if not isinstance(voice_map, dict):
        return jsonify({"success": False, "error": "缺少 voice_map"}), 400
    try:
        for name, v in (voice_map.get("characters") or {}).items():
            normalize_voice(v)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    path = os.path.join(_dub_project_dir(project_name), "voice_map.json")
    save_voice_map(voice_map, path)
    return jsonify({"success": True, "path": path, "voice_map": voice_map})
@tts_bp.route('/api/tts/voice-bank', methods=['GET'])
def api_tts_voice_bank_list():
    """列出本项目已绑定参考音频的角色（含时长/原文/更新时间）。"""
    project_name = _safe_project(request.args.get('project_name') or '')
    if not project_name:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    items = list_voice_bank(_dub_project_dir(project_name))
    return jsonify({"success": True, "items": items,
                    "clone_available": tts_clone_available(),
                    "supported_exts": list(VOICE_BANK_EXTS)})
@tts_bp.route('/api/tts/voice-bank/upload', methods=['POST'])
def api_tts_voice_bank_upload():
    """上传某角色的参考音频（form-data: file, project_name, character, ref_text?）。

    ref_text = 这段参考音频里**实际说出的那句话**。填了克隆相似度显著更高，
    但可留空（节点支持 x_vector_only 路径，只用说话人向量）。
    """
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False, "error": "未收到音频，请通过 file 字段上传"}), 400
    f = files[0]
    project_name, err = _project_or_400(
        (request.form.get('project_name') or request.args.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (request.form.get('character') or request.args.get('character') or '').strip()
    ref_text = str(request.form.get('ref_text') or request.args.get('ref_text') or '').strip()
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400

    raw_name = _safe_upload_name(f.filename)
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in VOICE_BANK_EXTS:
        return jsonify({"success": False,
                        "error": f"不支持的音频格式 {ext or '（无扩展名）'}；"
                                 f"支持 {'、'.join(VOICE_BANK_EXTS)}"}), 400

    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(UPLOAD_TMP_DIR,
                            f"vref_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext}")
    try:
        f.save(tmp_path)
        # 客观校验：能读出时长且落在合理区间。参考音频太短克隆不出音色、
        # 太长拖慢每次合成（每次都要解码），都要在入口拦下并给出可读原因。
        info = probe_audio_info(tmp_path)
        if not info.get("ok"):
            return jsonify({"success": False,
                            "error": f"音频无法读取：{info.get('error') or '解码失败'}"}), 400
        dur = float(info.get("duration") or 0)
        if dur < tts_client.VOICE_BANK_MIN_SEC:
            return jsonify({"success": False,
                            "error": f"参考音频过短（{dur:.1f}s）—— 建议 3–15 秒清晰人声"}), 400
        if dur > tts_client.VOICE_BANK_MAX_SEC:
            return jsonify({"success": False,
                            "error": f"参考音频过长（{dur:.1f}s > "
                                     f"{tts_client.VOICE_BANK_MAX_SEC:.0f}s）—— 请截取 3–15 秒"}), 400

        dub_dir = _dub_project_dir(project_name)
        dst = save_voice_bank_ref(dub_dir, character, tmp_path,
                                  ref_text=ref_text, original_filename=raw_name)
        _app_logger().info("[参考音频] %s/%s ← %s（%.1fs）", project_name, character, raw_name, dur)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("参考音频入库失败：%s", e)
        return jsonify({"success": False, "error": f"参考音频保存失败：{e}"}), 500
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return jsonify({"success": True, "character": character, "file": dst,
                    "ref_text": ref_text, "duration_sec": dur,
                    "clone_available": tts_clone_available(),
                    "message": f"已绑定参考音频（{dur:.1f}s）；该角色下次配音将自动使用克隆声线"})
@tts_bp.route('/api/tts/voice-bank/delete', methods=['POST'])
def api_tts_voice_bank_delete():
    """解绑某角色的参考音频（删掉整个 voice_bank/<角色>/ 目录）。

    只删 voice_bank 子目录内的内容（由 voice_bank_dir 给出路径），不递归到别处。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    if not character or '/' in character or '\\' in character or character in ('.', '..'):
        return jsonify({"success": False,
                        "error": "character（角色名）不能为空且不得含路径分隔符"}), 400
    d = voice_bank_dir(_dub_project_dir(project_name), character)
    removed = []
    if os.path.isdir(d):
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(fn)
            os.rmdir(d)
        except OSError as e:
            _app_logger().warning("解绑参考音频失败 %s：%s", d, e)
            return jsonify({"success": False, "error": f"删除失败：{e}"}), 500
    return jsonify({"success": True, "character": character, "removed": removed,
                    "message": "已解绑参考音频" if removed else "该角色未绑定参考音频"})
@tts_bp.route('/api/tts/voice-bank/preview', methods=['POST'])
def api_tts_voice_bank_preview():
    """用已绑定的参考音频试听克隆效果（合成一句样例文本）。

    body = {project_name, character, text?}
    刻意**不读** voice_map：直接以 clone 模式合成，让用户在决定保存前先听效果。
    """
    data = _body()
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    character = (data.get('character') or '').strip()
    if not character:
        return jsonify({"success": False, "error": "缺少 character"}), 400
    text = clean_line_text(data.get('text') or '', character) or \
        f"我是{character}，今日便让你见识见识。"
    if not tts_clone_available():
        return jsonify({"success": False,
                        "error": "当前 ComfyUI 未提供参考音频克隆节点"
                                 "（FB_Qwen3TTSVoiceClone / LoadAudio）"}), 503
    dub_dir = _dub_project_dir(project_name)
    ref, ref_text = find_voice_bank_ref(dub_dir, character)
    if not ref:
        return jsonify({"success": False,
                        "error": f"角色「{character}」未绑定参考音频，请先上传"}), 400
    # ⭐ 2026-10-05 修复「试听 500」：
    # ① voice 带上 character —— synthesize_one 的批项此前恒为 character=None，
    #    教训库 / 台词清洗全部丢了角色维度；
    # ② 整段包一层兜底 try：此前 normalize_voice / safe_name / makedirs 里任何
    #    未预期异常都会直接变 Flask 500（界面只剩「Internal Server Error」一句，
    #    用户完全不知道发生了什么）。现在统一返回可读的 502/500 JSON。
    try:
        voice = normalize_voice({
            "mode": "clone", "ref_audio": ref, "ref_text": ref_text,
            "speaker": "Ryan", "seed": 0, "character": character,
        })
        preview_dir = os.path.join(dub_dir, "preview")
        os.makedirs(preview_dir, exist_ok=True)
        out_path = os.path.join(preview_dir,
                                f"clone_{int(time.time())}_{tts_client.safe_name(character, 12)}.wav")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        rec = client.synthesize_one(text, voice, out_path)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 兜底：绝不让试听变裸 500
        _app_logger().exception("参考音色试听失败")
        return jsonify({"success": False, "error": f"试听失败：{e}"}), 500
    if not rec.get("ok"):
        return jsonify({"success": False, "error": rec.get("error") or "合成失败",
                        "clone_fallback": rec.get("clone_fallback")}), 502
    info = probe_audio_info(rec["out_path"])
    return jsonify({"success": True, "result": rec, "audio": info, "voice": voice,
                    "url": _dub_audio_url(project_name, rec["out_path"]),
                    "text_used": text})
@tts_bp.route('/api/tts/preview', methods=['POST'])
def api_tts_preview():
    """单句试听合成：指定 text + 音色（或角色），返回可播放音频与时长"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    text = clean_line_text(data.get('text') or '', str(data.get('character') or ''))
    if not text:
        return jsonify({"success": False, "error": "缺少待合成文本 text"}), 400

    # 试听前先跑一遍台词预检：用户在这里就能看到「这句会被念成什么样」以及为什么，
    # 不必等到整集配完才发现结构残留被念了出来。
    _pf = None
    try:
        _pcfg = _qc_load_cfg()
        _pf = prompt_qc.preflight(
            "audio", text,
            ctx={"character": str(data.get('character') or '') or None,
                 "emotion": data.get('emotion'),
                 "voice_mode": (data.get('voice') or {}).get('mode')
                               if isinstance(data.get('voice'), dict) else None},
            cfg=_pcfg)
        if _pf.get("prompt"):
            text = _pf["prompt"]
    except Exception as e:  # noqa: BLE001 - 预检失败不影响试听
        _app_logger().debug(f"试听台词预检跳过：{e}")

    env = tts_env_check()
    if not env.get("available"):
        return jsonify({"success": False, "error": "TTS 环境不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = load_voice_map(vm_path) or {}
    char_name = str(data.get('character') or '').strip()
    try:
        base = (voice_map.get("characters") or {}).get(char_name) if char_name else None
        if base is None and char_name:
            base = default_voice_map([{"name": char_name}], project_name, 1)["characters"].get(char_name)
        voice = normalize_voice(data.get('voice'), base)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    # 2026-10-05：voice 补上 character 维度（教训库记账）+ 整段兜底，不再裸 500
    voice = dict(voice)
    if char_name:
        voice["character"] = char_name
    try:
        preview_dir = os.path.join(out_dir, "preview")
        os.makedirs(preview_dir, exist_ok=True)
        out_path = os.path.join(preview_dir, f"preview_{int(time.time())}_"
                                          f"{tts_client.safe_name(char_name or 'line', 12)}.wav")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        rec = client.synthesize_one(text, voice, out_path)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 兜底：绝不让试听变裸 500
        _app_logger().exception("单句试听失败")
        return jsonify({"success": False, "error": f"试听失败：{e}"}), 500
    if not rec.get("ok"):
        return jsonify({"success": False, "error": rec.get("error") or "合成失败", "result": rec}), 502

    info = probe_audio_info(rec["out_path"])
    # 试听只跑**客观层**音频质检：纯 ffmpeg、毫秒级，不花模型调用，保证试听依然「点一下就响」。
    # 需要含 AI 层（频谱/波形送检）的完整结论时走 POST /api/qc/audio。
    _objs = None
    try:
        _ocfg = _qc_load_cfg()
        if qc_client.audio_qc_ready(_ocfg):
            # ⚠️ quick_check 的 verdict 里已经带了 metrics，不要再单独 probe 一次 ——
            #    那会重复解码一遍音频（白等一次 ffmpeg）。
            _objs = audio_qc.quick_check(
                rec["out_path"], expect_sec=audio_qc.estimate_speech_sec(text) or None,
                min_speech_ratio=_ocfg.get("audio_min_speech_ratio", 0.50),
                min_mean_db=_ocfg.get("audio_min_mean_db", -45.0),
                max_drift=_ocfg.get("audio_max_drift", 0.50))
    except Exception as e:  # noqa: BLE001
        _app_logger().debug(f"试听音频客观质检跳过：{e}")
    return jsonify({"success": True, "result": dict(rec, url=_dub_audio_url(project_name, rec["out_path"])),
                    "audio": info, "voice": voice, "url": _dub_audio_url(project_name, rec["out_path"]),
                    "prompt_qc": (_pf or {}).get("verdict"),
                    "prompt_qc_label": (_pf or {}).get("label"),
                    "prompt_qc_repairs": (_pf or {}).get("repairs") or [],
                    "text_used": text,
                    "audio_qc": _objs})
@tts_bp.route('/api/tts/file/<project_name>/<path:filename>')
def api_tts_file(project_name, filename):
    """播放 / 下载配音产物（支持 Range 拖动试听，download=1 触发下载）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(DUB_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, filename.replace("\\", "/").lstrip("/")))
    if not filepath.startswith(base + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')
