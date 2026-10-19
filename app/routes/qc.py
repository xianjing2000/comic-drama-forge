# -*- coding: utf-8 -*-
"""质检（图片/视频/剧本/提示词）配置与执行 API 蓝图（Blueprint 拆分第八批，2026-10-08）。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @qc_bp.route。
跨域助手在 routes/_shared.py；本模块只放质检域自己的东西。
"""
import json
import os
import time

from flask import Blueprint, current_app, jsonify, request

qc_bp = Blueprint('qc', __name__)

import audio_qc
import prompt_protocol
import prompt_qc
from config import DUB_DIR, FINAL_DIR, PROJECT_DATA_DIR, QC_DIR, STORYBOARDS_DIR
from dub_mix import mix_out_dir, probe_audio_info as mix_probe_audio
from flask import abort, send_file
from routes._shared import AI_CONFIG_PATH, LLM_CONFIG_PATH, PROJECT_OUTPUT_DIR, QC_CONFIG_PATH, _AUDIO_QC_AUDIO_EXT, _AUDIO_QC_MEDIA_EXT, _audio_qc_project_key, _body, _ep_read_dir, _qc_load_cfg, _safe_project, ai_config, project_store, qc_client, _app_logger
def _audio_qc_file_url(project: str, path: str) -> str:
    """尽量给出可直接播放的 URL（只对 tts/mix 两个既有静态路由下的产物）"""
    ap = os.path.abspath(path)
    try:
        rel_dub = os.path.relpath(ap, os.path.join(DUB_DIR, project or 'project'))
        if not rel_dub.startswith('..'):
            return f"/api/tts/file/{project or 'project'}/{rel_dub.replace(os.sep, '/')}"
        rel_mix = os.path.relpath(ap, mix_out_dir(project or 'project'))
        if not rel_mix.startswith('..'):
            return f"/api/mix/file/{project or 'project'}/{rel_mix.replace(os.sep, '/')}"
    except ValueError as e:
        _app_logger().debug("混音相对路径解析失败（忽略）：%s", e)
    return ""
def _audio_qc_visuals_key(requested_project, target: str) -> str:
    """音频质检可视化图片用的项目键。

    ⚠️ 调用方**不能**写成 ``_safe_project(x) or _audio_qc_project_key(target)``：
    ``_safe_project('')`` 返回的是**字面量 'project'**（``project_store.safe_key``
    的空值兜底），恒为真值 → 兜底永不生效。后果是所有按 ``path`` 直接检查的请求都
    挤进同一个 ``.../project/`` 目录，**不同项目的同名产物互相覆盖** —— 而 AI 读图是
    异步进行的，覆盖会变成竞态（读到了别的项目的频谱图）。
    判「调用方到底有没有传项目名」必须看**原始入参**。
    """
    if str(requested_project or '').strip():
        return _safe_project(requested_project)
    return _audio_qc_project_key(target)
def _qc_test_override(data: dict) -> dict:
    """测试用的临时接口参数（不落盘）；返回空 dict 表示完全用已保存配置"""
    ep = {k: str(data.get(k) or "").strip() for k in ("base_url", "model")}
    key = str(data.get("api_key") or "").strip()
    if "*" in key:
        key = ""          # 脱敏回显 → 用已保存密钥
    ep["api_key"] = key
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {}
    return ep
def _resolve_audio_qc_target(project: str, source: str = ''):
    """按项目推导待质检音频：final（成片音轨）> mix（带配音成片）> merged（整集合成音轨）> line（单句）

    返回 ``(路径, source, 失败原因)``。

    ⚠️ 必须按扩展名过滤：``mix_out_dir`` 里除了成片还有 ``*_mix_report.json``
    等边车文件（且它们往往最新），不过滤就会把 JSON 报告当成成片送去解码，
    结论变成「文件无法解码」——假失败。
    """
    def _newest(paths):
        cands = [p for p in paths if os.path.isfile(p) and os.path.getsize(p) > 0]
        return max(cands, key=os.path.getmtime) if cands else ''

    def _media(d, exts):
        if not os.path.isdir(d):
            return []
        return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(exts)]

    mix_dir = mix_out_dir(project)
    merged_dir = os.path.join(DUB_DIR, project)
    lines_dir = os.path.join(merged_dir, 'lines')

    # R5b：成片音轨 —— H3 成片（FINAL_DIR/<项目>/ 下的 mp4，含配音音轨）。
    # 前端「音频质检」页固定发 source='final'，故这是主用档。
    # 项目成片目录兼容 dir_key 旧命名 → 走 project_store.project_dirs 解析（同 _mix_resolve_video）。
    if source == 'final':
        cands = []
        for d in project_store.project_dirs(FINAL_DIR, project):
            cands.extend(_media(d, _AUDIO_QC_MEDIA_EXT))
        p = _newest(cands)
        if p:
            return p, 'final', ''
        return '', 'final', f"项目 '{project}' 下没有成片音轨（请先在步骤6完成成片合成）"
    if source in ('', 'mix'):
        p = _newest(_media(mix_dir, _AUDIO_QC_MEDIA_EXT))
        if p:
            return p, 'mix', ''
        if source == 'mix':
            return '', 'mix', f"项目 '{project}' 下没有带配音成片（请先做音画合成）"
    if source in ('', 'merged'):
        p = _newest(_media(merged_dir, _AUDIO_QC_AUDIO_EXT))
        if p:
            return p, 'merged', ''
        if source == 'merged':
            return '', 'merged', f"项目 '{project}' 下没有整集配音音轨"
    if source in ('', 'line'):
        p = _newest(_media(lines_dir, _AUDIO_QC_AUDIO_EXT))
        if p:
            return p, 'line', ''
        if source == 'line':
            return '', 'line', f"项目 '{project}' 下没有逐句配音文件"
    # ⭐ 2026-10-12 P0：参考音色档。现行 7 步流水线的 tts_pre 只产
    #    output/dub/<项目>/voice_bank/<角色>/ref.wav，**不产 _lines/** —— 不给
    #    这一档，面板「音频质检」在现役链路上根本点不到任何产物（t10 审计）。
    #    ⚠️ 必须放在 'line' 之后：默认链（mix > merged > line > voice_bank）的
    #    既有优先级不得改变（.workbuddy/test/verify_r5b_audio_target_qa.py 锁了
    #    「默认档解析为 mix」）。
    if source in ('', 'voice_bank'):
        vb_dir = os.path.join(merged_dir, 'voice_bank')
        cands = []
        if os.path.isdir(vb_dir):
            for _ch in sorted(os.listdir(vb_dir)):
                _d = os.path.join(vb_dir, _ch)
                if os.path.isdir(_d):
                    cands.extend(_media(_d, _AUDIO_QC_AUDIO_EXT))
        p = _newest(cands)
        if p:
            return p, 'voice_bank', ''
        if source == 'voice_bank':
            return '', 'voice_bank', f"项目 '{project}' 下没有参考音色（配音先行未跑或未生成）"
    return '', source or 'mix', f"项目 '{project}' 下没有可质检的音频产物（先做配音/合成）"
def _audio_qc_candidates(project: str) -> list:
    """体检要扫的音频产物清单（每项 {key, history_key, path, kind, whole_track, label}）。

    为什么不能只扫 <dub>/<项目>/*_lines/：现行 7 步流水线的 tts_pre 只产 voice_bank
    （每角色参考音色），**不产 _lines/** → 旧实现 audio_checked 恒为 0，面板永远显示
    「配音无问题」，而成片音轨（H3 原生，直接 copy2 交付）根本没人扫过（t10 审计）。

    覆盖五类：
      · line       逐句配音（旧布局 *_lines/，保留兼容）
      · voice_bank 参考音色（现行链路唯一的 TTS 产物）
      · merged     整集配音合并轨
      · mix        带配音成片（final_dub/，旧链路保留）
      · final      成片（FINAL_DIR/，现役交付物本体）
    整轨类一律走**整轨口径**（不设有声占比下限）：参考音色是声音样本、成片天然有
    大段无台词留白，拿单句 50% 标准卡它们必然误报「漏句」。
    """
    out: list = []
    seen: set = set()

    def _add(key, history_key, path, kind, whole_track, label):
        ap = os.path.abspath(path)
        try:
            if ap in seen or not os.path.isfile(ap) or os.path.getsize(ap) <= 0:
                return
        except OSError:
            return
        seen.add(ap)
        out.append({"key": key, "history_key": history_key, "path": ap,
                    "kind": kind, "whole_track": bool(whole_track), "label": label})

    dub_dir = os.path.join(DUB_DIR, project)
    if os.path.isdir(dub_dir):
        for scene in sorted(os.listdir(dub_dir)):
            if not scene.endswith("_lines"):
                continue
            sd = os.path.join(dub_dir, scene)
            if not os.path.isdir(sd):
                continue
            for wav in sorted(os.listdir(sd)):
                if wav.lower().endswith(_AUDIO_QC_AUDIO_EXT):
                    # 历史键沿用 basename（与 dub worker 落盘一致，别与旧记录脱钩）
                    _add(f"{scene}/{wav}", wav, os.path.join(sd, wav), "line", False,
                         f"逐句配音 {wav}")
        vb = os.path.join(dub_dir, "voice_bank")
        if os.path.isdir(vb):
            for ch in sorted(os.listdir(vb)):
                d = os.path.join(vb, ch)
                if not os.path.isdir(d):
                    continue
                for fn in sorted(os.listdir(d)):
                    if fn.lower().endswith(_AUDIO_QC_AUDIO_EXT):
                        _add(f"voice_bank/{ch}/{fn}", f"voice_bank_{ch}_{fn}",
                             os.path.join(d, fn), "voice_bank", True, f"参考音色 {ch}")
        for fn in sorted(os.listdir(dub_dir)):
            if fn.lower().endswith(_AUDIO_QC_AUDIO_EXT):
                _add(fn, fn, os.path.join(dub_dir, fn), "merged", True, f"整集配音轨 {fn}")
    md = mix_out_dir(project)
    if os.path.isdir(md):
        for fn in sorted(os.listdir(md)):
            if fn.lower().endswith(_AUDIO_QC_MEDIA_EXT):
                _add(f"mix/{fn}", f"mix_{fn}", os.path.join(md, fn), "mix", True,
                     f"带配音成片 {fn}")
    try:
        fin_dirs = project_store.project_dirs(FINAL_DIR, project)
    except Exception:                                        # noqa: BLE001
        fin_dirs = [os.path.join(FINAL_DIR, project)]
    for d in fin_dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(_AUDIO_QC_MEDIA_EXT):
                _add(f"final/{fn}", f"final_{fn}", os.path.join(d, fn), "final", True,
                     f"成片 {fn}")
    return out

@qc_bp.route('/api/qc/config', methods=['GET'])
def api_qc_config_get():
    # 质检存量迁移触发点之二（之一在启动段）：前端打开质检页即保证老配置被补成默认开启。
    # 幂等（已迁移返回 False），失败只告警、不得让接口 500。
    try:
        qc_client.migrate_enabled_default(QC_CONFIG_PATH)
    except Exception as _e:  # noqa: BLE001  迁移失败不得影响接口
        _app_logger().warning(f"质检存量迁移失败（不影响接口）：{_e}")
    cfg = _qc_load_cfg()
    view = qc_client.public_view(cfg)
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "history_dir": os.path.abspath(QC_DIR),
                    "ai_settings_path": os.path.abspath(AI_CONFIG_PATH)})
@qc_bp.route('/api/qc/config', methods=['POST'])
def api_qc_config_save():
    data = (request.get_json(silent=True) or {})
    data.pop("reuse_llm", None)   # 旧字段：质检不再复用文本分析 LLM，直接忽略
    cfg = qc_client.save_config(QC_CONFIG_PATH, data)
    view = qc_client.public_view(cfg)
    if view["enabled"] and not view["ready"]:
        return jsonify({"success": True, "config": view,
                        "config_path": os.path.abspath(QC_CONFIG_PATH),
                        "warning": "质检开关已开启，但质检接口信息不完整（base_url / api_key / model），"
                                   "生成流程将跳过质检且不会报错。",
                        "message": "配置已保存（接口未就绪）"})
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "质检配置已保存"})
@qc_bp.route('/api/qc/config/clear', methods=['POST'])
def api_qc_config_clear():
    cfg = qc_client.clear_config(QC_CONFIG_PATH)
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "质检配置已清除（质检总开关关闭）"})
@qc_bp.route('/api/qc/config/reset-endpoint', methods=['POST'])
def api_qc_config_reset_endpoint():
    """把质检接口恢复为「AI 设置 → 质检模型」的独立配置"""
    cfg = qc_client.reset_endpoint(QC_CONFIG_PATH)
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "已清空质检页面内的接口覆盖，将使用「AI 设置 → 质检模型」"})
@qc_bp.route('/api/qc/config/sync-from-ai', methods=['POST'])
def api_qc_config_sync_from_ai():
    """一键把「AI 设置 → 质检模型」的接口同步到质检配置（可选，便于统一维护）"""
    ai_cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(ai_cfg, "qc")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return jsonify({"success": False,
                        "error": "「AI 设置 → 质检模型」尚未配置完整，请先在那里填写并保存"}), 400
    cfg = qc_client.set_endpoint(QC_CONFIG_PATH, ep["base_url"], ep["api_key"], ep["model"])
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "endpoint": {"base_url": ep["base_url"], "model": ep["model"], "source": "ai_settings"},
                    "message": "已同步「AI 设置 → 质检模型」到质检配置"})
@qc_bp.route('/api/qc/test', methods=['POST'])
def api_qc_test():
    """测试质检接口：可用页面暂存参数直接测试，不落盘。
    传 image_path 时用真实图片走一次图片质检；传 video_path 走视频抽帧质检；
    否则做连通性测试（vision=true 时用极小图片探测视觉能力）。"""
    data = (request.get_json(silent=True) or {})
    data.pop("reuse_llm", None)
    cfg = _qc_load_cfg()
    for k in ("image_prompt", "video_prompt", "pass_score",
              "max_retries", "video_frame_count"):
        if data.get(k) not in (None, ""):
            cfg[k] = data[k]
    cfg["enabled"] = True
    cfg["image_enabled"] = True
    cfg["video_enabled"] = True
    cfg = qc_client.load_config_dict(cfg)

    override = _qc_test_override(data)
    ep = qc_client.resolve_endpoint(cfg, override or None)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return jsonify({"success": False,
                        "error": "质检接口未配置完整（质检需独立配置 base_url / api_key / model）",
                        "endpoint_source": ep["source"]}), 400

    def _endpoint_view():
        return {"base_url": ep["base_url"], "model": ep["model"], "source": ep["source"]}

    image_path = data.get("image_path") or ""
    if not image_path:
        # 自动挑一张已有分镜图作为测试样张
        probe = _ep_read_dir(STORYBOARDS_DIR,
                             _safe_project(data.get("project_name") or "project"),
                             data.get("episode_no"))
        if os.path.isdir(probe):
            pngs = sorted([f for f in os.listdir(probe) if f.lower().endswith(".png")])
            if pngs:
                image_path = os.path.join(probe, pngs[0])

    video_path = data.get("video_path") or ""
    if video_path and os.path.isfile(video_path):
        verdict = qc_client.check_video(video_path, "接口连通性测试样张", cfg, override or None,
                                        frames_dir=os.path.join(QC_DIR, "_selftest", "frames"))
        ok = bool(verdict.get("ok"))
        return jsonify({
            "success": ok,
            "mode": "video",
            "video_path": os.path.abspath(video_path),
            "frame_count": verdict.get("frame_count"),
            "duration": verdict.get("duration"),
            "frames": verdict.get("frames"),
            "verdict": verdict,
            "endpoint": _endpoint_view(),
            "error": None if ok else (verdict.get("error") or verdict.get("reason")),
        }), (200 if ok else 400)

    if image_path and os.path.isfile(image_path):
        verdict = qc_client.check_image(image_path, "接口连通性测试样张", cfg, override or None)
        ok = bool(verdict.get("ok"))
        return jsonify({
            "success": ok,
            "mode": "image",
            "image_path": os.path.abspath(image_path),
            "verdict": verdict,
            "endpoint": _endpoint_view(),
            "error": None if ok else (verdict.get("error") or verdict.get("reason")),
        }), (200 if ok else 400)

    # 无样张：连通性测试。vision=true 或未指定时用极小图片探测视觉能力
    if data.get("vision", True):
        result = qc_client.test_vision(ep, timeout=60)
        return jsonify({"success": result.get("success"), "mode": "vision",
                        "vision": result.get("vision"), "reply": result.get("reply"),
                        "latency_ms": result.get("latency_ms"), "url": result.get("url"),
                        "endpoint": _endpoint_view(), "error": result.get("error")}), (
            200 if result.get("success") else 400)
    try:
        resp = qc_client._post_chat(ep, {
            "model": ep["model"],
            "messages": [{"role": "user", "content": "回复 JSON：{\"ok\": true}"}],
            "temperature": 0, "max_tokens": 64},
            cfg.get("timeout", 180))
        return jsonify({"success": True, "mode": "text", "raw": resp["content"][:300],
                        "latency_ms": resp["latency_ms"], "endpoint": _endpoint_view()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "mode": "text", "error": str(e),
                        "endpoint": _endpoint_view()}), 400
@qc_bp.route('/api/qc/history/<path:project_name>/<kind>/<path:shot_key>')
def api_qc_history(project_name, kind, shot_key):
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    data = qc_client.read_history(QC_DIR, project, kind, shot_key)
    path = qc_client.history_path(QC_DIR, project, kind, shot_key)
    return jsonify({"success": bool(data), "project_name": project, "kind": kind,
                    "shot_id": shot_key, "exists": bool(data), "history": data,
                    "history_file": os.path.abspath(path)})
@qc_bp.route('/api/qc/prompt', methods=['POST'])
def api_qc_prompt():
    """提示词预检（生成前质检）：按需检查一条提示词，并按配置自愈。

    body::

        {kind: "storyboard"|"h3"|"asset", prompt: "...", style?: "...",
         context?: <镜头/资产 dict>, ref_count?: int, expect_refs?: bool,
         repair?: true}   # repair=false 时只检查、不改写

    与图片/视频质检不同，这一层**不依赖质检接口**（纯确定性检查），因此未配置质检
    接口也能用；返回的 verdict 与 check_image/check_video 同构，便于前端统一展示。
    """
    data = _body()
    kind = str(data.get('kind') or '').strip().lower()
    if not kind:
        return jsonify({"success": False,
                        "error": f"缺少 kind（可选 {' / '.join(prompt_qc.PROMPT_KINDS)}）"}), 400
    if kind not in prompt_qc.PROMPT_KINDS:
        return jsonify({"success": False,
                        "error": f"不支持的 kind：{kind}（可选 {'/'.join(prompt_qc.PROMPT_KINDS)}）"}), 400
    prompt = str(data.get('prompt') or '')
    if not prompt.strip():
        return jsonify({"success": False, "error": "缺少 prompt"}), 400

    cfg = _qc_load_cfg()
    style = str(data.get('style') or '').strip()
    ctx = data.get('context') if isinstance(data.get('context'), dict) else None
    rc = data.get('ref_count')
    try:
        rc = int(rc) if rc is not None else None
    except (TypeError, ValueError):
        rc = None
    expect_refs = data.get('expect_refs')
    expect_refs = bool(expect_refs) if isinstance(expect_refs, (bool, int)) else None

    if bool(data.get('repair', True)):
        pf = prompt_qc.preflight(kind, prompt, ctx=ctx, style=style, cfg=cfg,
                                 ref_count=rc, expect_refs=expect_refs)
    else:
        v = prompt_qc.check_prompt(kind, prompt, ctx=ctx, style=style, cfg=cfg,
                                   ref_count=rc, expect_refs=expect_refs)
        blocked = bool(v.get("critical_issues"))
        pf = {"prompt": prompt, "verdict": v, "repairs": [], "accept": not blocked,
              "blocked": blocked, "skipped": not prompt_qc.prompt_qc_ready(cfg),
              "label": "提示词达标" if v.get("passed") and not v.get("issues") else "提示词不达标",
              "reason": v.get("reason") or "", "rebuild_hint": v.get("rebuild_hint") or ""}
    return jsonify({"success": True, "kind": kind,
                    "prompt": pf.get("prompt"), "verdict": pf.get("verdict"),
                    "repairs": pf.get("repairs") or [],
                    "gate": prompt_qc.prompt_qc_gate(pf, cfg),
                    "mode": prompt_qc.prompt_qc_mode(cfg),
                    "accept": bool(pf.get("accept")),
                    "label": pf.get("label"), "reason": pf.get("reason"),
                    "rebuild_hint": pf.get("rebuild_hint") or ""})
@qc_bp.route('/api/qc/audio', methods=['POST'])
def api_qc_audio():
    """音频质检（成品质检）：客观层（ffmpeg 指标）+ AI 层（频谱图/波形图送多模态）。

    body::

        {project_name?: "...", 
         path?: "output/dub/<项目>/lines/xxx.wav",   # 显式指定文件（必须位于 output/ 内）
         source?: "mix" | "merged" | "line",         # 未给 path 时按此推导（默认 mix > merged）
         expect_sec?: 3.2,          # 期望时长；不给则只做无声/削波判定，不做时长偏差
         line_text?: "三年了，我回来了。",
         check_speech_ratio?: true, # 「有声占比下限」判定。单句传 true；整轨必须 false
         with_ai?: true}            # false 时只跑客观层（毫秒级、零模型调用）

    与 ``/api/qc/prompt``（生成前预检）配套：那个管「台词写对没有」，这个管
    「录出来是不是真的有人声」。
    """
    data = _body()
    project = _safe_project(data.get('project_name') or '')
    cfg = _qc_load_cfg()
    if not qc_client.audio_qc_ready(cfg):
        return jsonify({"success": False,
                        "error": "音频质检未启用（请检查质检总开关与音频质检开关）",
                        "audio_qc_active": False}), 400

    # ---- 定位待检文件：显式 path 优先，否则按 source 推导 ----
    raw_path = str(data.get('path') or '').strip()
    source = str(data.get('source') or '').strip().lower()
    target, why = '', ''
    if raw_path:
        # 相对路径挂到**数据根**（output/ 就在它下面）：frozen 时 PROJECT_ROOT_DIR 是
        # _MEIPASS（只读资源区），拿它当基址会让下面 output/ 的包含性校验恒不通过。
        cand = os.path.abspath(os.path.join(PROJECT_DATA_DIR, raw_path)) \
            if not os.path.isabs(raw_path) else os.path.abspath(raw_path)
        root = os.path.abspath(PROJECT_OUTPUT_DIR)
        # 只允许检查 output/ 内的产物：这是「回显用户自己的成品」，不是任意文件读取接口
        if not cand.startswith(root + os.sep):
            return jsonify({"success": False,
                            "error": f"只允许检查 output/ 目录内的文件：{raw_path}"}), 400
        if not os.path.isfile(cand):
            return jsonify({"success": False, "error": f"文件不存在：{cand}"}), 404
        target, source = cand, (source or 'path')
    elif project:
        target, source, why = _resolve_audio_qc_target(project, source)
        if not target:
            return jsonify({"success": False, "error": why or "未找到可质检的音频产物",
                            "project_name": project}), 404
    else:
        return jsonify({"success": False, "error": "需要 project_name 或 path"}), 400

    # ---- 期望时长 / 有声占比口径 ----
    expect = data.get('expect_sec')
    try:
        expect = float(expect) if expect not in (None, '') else None
    except (TypeError, ValueError):
        expect = None
    if expect is None and source == 'mix':
        try:
            expect = float(mix_probe_audio(target).get('duration') or 0) or None
        except Exception:  # noqa: BLE001
            expect = None
    # ⚠️ 整轨（成片 mp4 / 整集合成音轨）默认**关闭**有声占比判定：
    #    成片天然有大段无台词留白，拿单句的 50% 标准卡它必然误报「漏句」。
    #    判据是「整轨口径」而不是「调用方有没有传 source」—— 前端直接拖一个 mp4 过来
    #    检查（source 会是 path）时同样必须关掉，否则一进来就是满屏「静音过多」。
    # ⭐ 2026-10-12：voice_bank 也走整轨口径 —— 参考音色是「声音样本」，首尾静音多、
    #    字数少，拿单句 50% 下限卡它必然误报（t10 实测 12 个 ref 中有 2 个
    #    有声占比 0.34 / 0.49，会被判「静音过多」）。
    is_whole_track = (source in ('final', 'mix', 'merged', 'voice_bank')
                      or target.lower().endswith(('.mp4', '.mkv', '.mov')))
    default_ratio = not is_whole_track
    check_ratio = data.get('check_speech_ratio')
    check_ratio = default_ratio if not isinstance(check_ratio, bool) else check_ratio

    stem = os.path.splitext(os.path.basename(target))[0]
    bucket = 'audio_mix' if (source == 'mix' or target.lower().endswith('.mp4')) else 'audio'
    visuals_dir = os.path.join(
        QC_DIR, bucket,
        _audio_qc_visuals_key(data.get('project_name'), target), stem)

    with_ai = bool(data.get('with_ai', True))
    if not with_ai:
        verdict = audio_qc.quick_check(
            target, expect_sec=expect,
            min_speech_ratio=(cfg.get("audio_min_speech_ratio", 0.50) if check_ratio else None),
            min_mean_db=cfg.get("audio_min_mean_db", -45.0),
            max_drift=cfg.get("audio_max_drift", 0.50))
        verdict["ai_skipped"] = True
        verdict["ai_skip_reason"] = "请求显式要求只做客观层（with_ai=false）"
    else:
        verdict = qc_client.check_audio(
            target, expect_sec=expect, line_text=str(data.get('line_text') or ''),
            cfg=cfg, visuals_dir=visuals_dir, check_speech_ratio=check_ratio)

    vis_urls = []
    for p in (verdict.get("visuals") or []):
        try:
            rel = os.path.relpath(p, QC_DIR).replace(os.sep, '/')
        except ValueError:
            continue
        if not rel.startswith('..'):
            vis_urls.append(f"/api/qc/frames/{rel}")
    return jsonify({
        "success": True,
        "project_name": project, "source": source, "path": os.path.abspath(target),
        "expect_sec": expect, "check_speech_ratio": check_ratio,
        "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
        "score": verdict.get("score"), "reason": verdict.get("reason"),
        "issues": verdict.get("issues") or [],
        "critical_issues": verdict.get("critical_issues") or [],
        "metrics": verdict.get("metrics") or {},
        "ai_used": bool(verdict.get("ai_used")),
        "ai_skipped": bool(verdict.get("ai_skipped")),
        "ai_skip_reason": verdict.get("ai_skip_reason") or "",
        "objective_only": bool(verdict.get("objective_only")),
        "visuals": vis_urls,
        "verdict": verdict,
        "audio_qc_active": True,
        "audio_ai_active": qc_client.audio_ai_ready(cfg),
        "file_url": _audio_qc_file_url(project, target),
    })
@qc_bp.route('/api/qc/triage', methods=['POST'])
def api_qc_triage():
    """诊断与补救建议（**只读**：只诊断 + 给建议，绝不自动执行补救）。

    body::  {"project": "...", "episode_no": 1, "deep": false}

    决策逻辑（用户 2026-10-09 拍板）：
      ① 先看**画面**（分镜图 + 视频，两类都算）—— 读 QC_DIR 历史，最新一条 passed=false 即失败；
      ② 再看**配音**（逐句）—— deep=false 时只读历史（快）；deep=true 时逐句实跑 check_audio；
      ③ 最后看**字幕校对**（ASR 比对，missing/extra）；
      ④ 判定：
           只有配音有问题        → remedy="tts"          （TTS 重合成 / 重绑音色，便宜）
           只有画面有问题        → remedy="visual_regen"  （重出该镜，中等）
           音画都有问题          → remedy="prompt_regen"  （⚠️ 改提示词重出片，贵 —— TTS 救不了画面）
           都没问题              → remedy="none"

    ⚠️ TTS 只能修「声音」，修不了「画面」。所以音画同时坏时，重配音是白费功夫，
       必须回到提示词层重出 —— 这正是本端点存在的意义：避免用错补救手段。
    """
    data = _body()
    raw = str(data.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project"}), 400
    project = _safe_project(raw)
    try:
        episode_no = max(1, int(data.get('episode_no') or 1))
    except (TypeError, ValueError):
        episode_no = 1
    deep = bool(data.get('deep'))

    # ---- ① 画面：分镜图 + 视频（两类都算）----
    visual_failed: list = []
    qc_dir = os.path.join(QC_DIR, project)
    if os.path.isdir(qc_dir):
        for fn in sorted(os.listdir(qc_dir)):
            if not fn.lower().endswith('.json'):
                continue
            kind, sep, key = fn[:-5].partition('_')
            if not sep or kind not in ('image', 'video'):
                continue
            try:
                hist = qc_client.read_history(QC_DIR, project, kind, key) or {}
            except Exception:  # noqa: BLE001
                continue
            recs = hist.get('records') or []
            if not recs:
                continue
            last = recs[-1] if isinstance(recs[-1], dict) else {}
            if not last.get('passed'):
                visual_failed.append({
                    "key": key, "kind": kind,
                    "time": last.get('time') or '',
                    "attempts": len(recs),
                    "issues": (last.get('issues') or [])[:3],
                })

    # ---- ② 配音/音轨：逐句 + 参考音色 + 整轨（成片）----
    # ⚠️ 2026-10-12 P0（t10 审计）：旧实现只扫 <dub>/<项目>/*_lines/，而现行流水线的
    #    tts_pre 产的是 voice_bank（**不产 _lines/**）→ audio_checked 恒为 0，面板永远
    #    显示「配音无问题」，而成片音轨（H3 原生、直接交付）从没被体检过。
    #    现在按 _audio_qc_candidates 扫五类产物；整轨类走整轨口径（不设有声占比下限）。
    audio_failed: list = []
    audio_checked = 0
    audio_scanned: dict = {}
    for _c in _audio_qc_candidates(project):
        _kind = _c["kind"]
        audio_checked += 1
        audio_scanned[_kind] = int(audio_scanned.get(_kind) or 0) + 1
        if not deep:
            # 只读历史（快）；新类目用带类目前缀的历史键，避免跨目录同名互相覆盖
            try:
                h = qc_client.read_history(QC_DIR, project, 'audio', _c["history_key"]) or {}
            except Exception:  # noqa: BLE001
                h = {}
            recs = h.get('records') or []
            if recs and isinstance(recs[-1], dict) and not recs[-1].get('passed'):
                audio_failed.append({"line": _c["key"], "kind": _kind,
                                     "whole_track": _c["whole_track"],
                                     "reason": (recs[-1].get('issues') or ['历史记录判失败'])[:2],
                                     "source": "history"})
            continue
        # deep：实跑（慢，ffmpeg 客观层 + 可选 AI 层）
        try:
            # ⚠️ 旧实现没传 cfg → check_audio 走 _empty_config 后直接返回
            #    {"skipped": True}，而 skipped 没有 passed 字段 → 每一项都被记成
            #    「失败」（deep 模式满屏假失败）。这里传真实配置，并显式跳过 skipped。
            _cfg = _qc_load_cfg()
            r = qc_client.check_audio(_c["path"], line_text='', cfg=_cfg,
                                      check_speech_ratio=not _c["whole_track"]) or {}
        except Exception as e:  # noqa: BLE001
            audio_failed.append({"line": _c["key"], "kind": _kind,
                                 "whole_track": _c["whole_track"],
                                 "reason": [f"检查异常：{e}"], "source": "error"})
            continue
        if r.get('skipped'):
            continue          # 质检开关未开/接口未配 → 不算「失败」，不冒充问题
        if not r.get('passed'):
            _m = r.get('metrics') or {}
            audio_failed.append({
                "line": _c["key"], "kind": _kind, "whole_track": _c["whole_track"],
                "reason": (r.get('issues') or [])[:2],
                "critical": (r.get('critical_issues') or [])[:2],
                "metrics": {k: _m.get(k) for k in ("duration", "mean_db", "max_db",
                                                   "silence_sec", "speech_ratio")},
                "source": "live"})

    # ---- ③ 字幕校对 ----
    caption = {"available": False, "ok": None, "missing": [], "extra": [], "similarity": None,
               "note": "尚未校对"}
    caption_verify_module = None
    try:
        import caption_verify as caption_verify_module  # noqa: PLC0415
        caption["available"] = bool((caption_verify_module.available() or {}).get('available'))
    except Exception:  # noqa: BLE001
        pass
    try:
        # ⚠️ 正确入口是 caption_verify.get_result(project, episode_no)（见
        #    routes/caption_verify.py 的 status 端点）。此前我误写了一个不存在的
        #    _cv_status_for，被 try 兜住 → caption.ok 恒为 None（静默失效）。
        # ⭐ get_result() 返回的是**任务字典**（get_result 源码 caption_verify.py:636），
        #    真正的校对结论在它的 result 字段里：
        #      {task_key, status, phase, progress, result: {ok, missing_segments, ...}}
        #    回落分支（进程重启后读落盘报告）同样把报告放在 result 下。
        #    ⚠️ 我前两版分别写错了字段名与层级，这里逐层解包。
        _job = caption_verify_module.get_result(project, episode_no) or {}
        caption["task_status"] = _job.get('status') or ''
        caption["phase"] = _job.get('phase') or ''
        res = _job.get('result') if isinstance(_job, dict) else None
        res = res if isinstance(res, dict) else {}
        if res:
            # ⚠️⚠️ 字段名必须与 verify_episode 的返回**逐字一致**（caption_verify.py:429）：
            #    它是 missing_segments / extra_segments / ok / error，
            #    **不是** missing / extra —— 我第一版按后者写，取到的恒为空数组，
            #    表现为「字幕校对没报任何问题」的静默失效（与 _cv_status_for 同一类错）。
            caption["missing"] = res.get('missing_segments') or []
            caption["extra"] = res.get('extra_segments') or []
            caption["similarity"] = res.get('similarity')
            caption["ok"] = bool(res.get('ok'))
            caption["error"] = res.get('error') or ''
            caption["transcript_chars"] = res.get('transcript_chars')
            caption["note"] = ('校对失败：' + str(res.get('error'))[:80]) if res.get('error') else '已校对'
    except Exception:  # noqa: BLE001
        pass

    # ---- ④ 判定 ----
    visual_ok = not visual_failed
    audio_ok = not audio_failed
    # ⭐ 字幕是否算「问题」必须区分两种情况（否则假阳性会把 verdict 顶成 both）：
    #    ① 校对**跑完了**且 ok=false  → 真·内容不符（该说没说/多说）→ 算问题
    #    ② 校对**没跑起来**（如没有成片）→ 是「缺输入」，不是质量问题 → 不算问题
    #    我第一版直接把 caption.ok 当判据，而 ok=false 在「未找到成片」时同样成立，
    #    结果报出 both / prompt_regen —— 会让用户白白重跑十几分钟 GPU。
    _cap_done = caption.get('task_status') == 'done'
    _cap_seg = bool(caption.get('missing') or caption.get('extra'))
    cap_problem = bool((_cap_done and caption.get('ok') is False) or _cap_seg)
    if visual_ok and audio_ok and not cap_problem:
        verdict, remedy = 'ok', 'none', 
        reason = '画面与配音均无失败记录'
    elif not visual_ok and (not audio_ok or cap_problem):
        verdict, remedy = 'both', 'prompt_regen'
        reason = (f'画面有 {len(visual_failed)} 处失败、配音有 {len(audio_failed)} 处问题 → '
                  'TTS 只能重配音、修不了画面，必须改提示词重出该镜')
    elif not visual_ok:
        verdict, remedy = 'visual_only', 'visual_regen'
        reason = f'仅画面有 {len(visual_failed)} 处失败 → 重出该镜即可，配音无需动'
    elif not audio_ok or cap_problem:
        verdict, remedy = 'audio_only', 'tts'
        reason = (f'仅配音/字幕有问题（配音 {len(audio_failed)} 处、字幕缺失 {len(caption["missing"])} 处）'
                  ' → 用 TTS 重合成该句即可，不必重出画面')
    else:
        verdict, remedy = 'ok', 'none'
        reason = '无问题'

    # 建议动作（**不执行**，前端点了才发）
    actions = []
    if remedy == 'tts':
        for a in audio_failed[:5]:
            actions.append({"label": f"重合成配音：{a['line']}", "method": "POST",
                            "endpoint": "/api/tts/preview",
                            "payload": {"project_name": project}, "cost": "low"})
        actions.append({"label": "检查音色绑定（voice-map）", "method": "POST",
                        "endpoint": "/api/tts/voice-map", "payload": {"project": project}, "cost": "low"})
    elif remedy == 'visual_regen':
        for v in visual_failed[:8]:
            ep = "/api/storyboard/retry-shot" if v["kind"] == "image" else "/api/video/retry-shot"
            actions.append({"label": f"重出{'分镜图' if v['kind'] == 'image' else '视频'}：{v['key']}",
                            "method": "POST", "endpoint": ep,
                            "payload": {"project": project, "shot_id": v["key"]}, "cost": "mid"})
    elif remedy == 'prompt_regen':
        for v in visual_failed[:8]:
            actions.append({"label": f"查看并修改提示词：{v['key']}", "method": "POST",
                            "endpoint": "/api/qc/prompt",
                            "payload": {"project_name": project, "shot_key": v["key"]}, "cost": "none"})
        for v in visual_failed[:8]:
            actions.append({"label": f"改完后重出该镜：{v['key']}", "method": "POST",
                            "endpoint": "/api/video/retry-shot",
                            "payload": {"project": project, "shot_id": v["key"]}, "cost": "high"})

    return jsonify({
        "success": True, "project": project, "episode_no": episode_no, "deep": deep,
        "visual": {"ok": visual_ok, "failed_count": len(visual_failed), "failed": visual_failed[:20]},
        "audio": {"ok": audio_ok, "checked": audio_checked, "failed_count": len(audio_failed),
                  "failed": audio_failed[:20], "mode": 'live' if deep else 'history',
                  # 2026-10-12 P0：按类目报扫描数，面板据此证明「真的扫到了产物」
                  # （旧实现只扫 *_lines/，现役链路恒 0）。
                  "scanned": audio_scanned,
                  "legend": {"line": "逐句配音", "voice_bank": "参考音色",
                             "merged": "整集配音轨", "mix": "带配音成片",
                             "final": "成片"}},
        "caption": caption,
        "verdict": verdict, "remedy": remedy, "reason": reason,
        "actions": actions,
    })


@qc_bp.route('/api/qc/project-summary', methods=['GET'])
def api_qc_project_summary():
    """项目级 QC 聚合：列出所有质检项的最新结论，供前端总览页使用

    ⚠️ 审计 G3：本接口此前**恒返回空统计**（线上 149 个质检历史文件一个都统计不到），
    根因有三处，缺一不可：
      ① `project = _safe_project(request.args.get('project', ''))` 后面接
         `if not project:` —— `_safe_project('')` 返回**字面量 'project'**（真值），
         守卫恒不成立（死守卫）。漏传项目名不会报错，而是聚合到共享 `project` 命名空间。
         判空必须看**原始入参**。
      ② 它枚举的是 `QC_DIR` 下以 `shot_` 开头的**目录**，而真实落盘路径是
         `QC_DIR/<项目>/<kind>_<键>.json` —— 一个都匹配不到，于是总览页永远
         「0 通过 / 0 失败」，用户以为质检从未运行过。
      ③ `read_history` 返回 `{"records": [...]}` 字典、记录里的字段是
         `passed` / `time`，**没有** `verdict` / `timestamp`。旧代码把 dict 当 list 用
         （`hist[-1]`）并按 `verdict` 判通过 —— 即使目录判对了也统计不出来。
    """
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)

    qc_cfg = _qc_load_cfg()
    qc_dir = os.path.join(QC_DIR, project)
    shots: list = []
    passed = failed = retry_count = 0

    if os.path.isdir(qc_dir):
        for fn in sorted(os.listdir(qc_dir)):
            if not fn.lower().endswith('.json'):
                continue
            # 文件名形如 <kind>_<键>.json（image_1.json / asset_image_七转蛊仙_base.json）
            kind, sep, shot_key = fn[:-5].partition('_')
            if not sep or not shot_key:
                continue
            try:
                with open(os.path.join(qc_dir, fn), 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
            except Exception as e:  # noqa: BLE001 - 单条坏文件不该拖垮总览
                _app_logger().warning("质检历史读取失败（已跳过）：%s：%s", fn, e)
                continue
            records = data.get('records') or []
            latest = records[-1] if (records and isinstance(records[-1], dict)) else {}
            # 通过与否以记录里的 `passed` 为准；接口异常（ok=False）单独归入「待重试」
            if latest.get('passed') is True or data.get('last_passed') is True:
                verdict = 'pass'
            elif latest.get('ok') is False:
                verdict = 'error'
            elif latest.get('passed') is False or data.get('last_passed') is False:
                verdict = 'fail'
            else:
                verdict = 'unknown'
            shots.append({
                'shot_id': shot_key,
                'kind': kind,
                'stage': latest.get('stage') or '',
                'score': latest.get('score'),
                'verdict': verdict,
                'timestamp': latest.get('time') or data.get('updated_at') or '',
                'attempts': int(data.get('total_attempts') or len(records) or 0),
                'error': latest.get('error') or '',
                'reason': latest.get('reason') or '',
                'file': latest.get('file') or '',
            })
            if verdict == 'pass':
                passed += 1
            elif verdict == 'fail':
                failed += 1
            else:
                retry_count += 1

    view = qc_client.public_view(qc_cfg)
    return jsonify({
        "success": True,
        "project": project,
        "config": view,
        "stats": {"total": len(shots), "passed": passed, "failed": failed,
                  "retry_count": retry_count},
        "history": shots,
        "qc_dir": qc_dir,
    })
@qc_bp.route('/api/qc/prompt-cache/stats', methods=['GET'])
def api_qc_prompt_cache_stats():
    """提示词预检缓存的统计 / 诊断（只读；`force=1` 强取）。

    为什么要有这个端点：预检缓存此前「看不见」—— 是否生效、命中率多少、跨模式有没有
    串味，日志与接口里都查不到（2026-10-12 t11 登记 prompt_qc_cache_stats 零调用点）。
    本函数是最薄的一层：统计口径全部来自 qc_client（它再复用 prompt_protocol 的字段表 /
    命中率 / 措辞），本文件不另立一套数字。

    `force=1`：**强取** —— 先无条件做一次预检缓存隔离（旧配置下的结论一律作废），
    再忽略 qc_client 的周期限流取数。返回的一定是**当前配置**下的数字，而不是另一套
    模式留下的旧结论（2026-10-12 t24 的根治点：缓存键已含 prompt_mode / prompt_enabled）。

    失败一律不 500：观测接口不该影响质检；取不到就 `success=false` + 空 stats。
    """
    force = str(request.args.get('force') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    try:
        cfg = _qc_load_cfg()
    except Exception as _e:  # noqa: BLE001 —— 读配置失败不影响读缓存统计
        _app_logger().debug(f"读取质检配置失败（预检缓存统计按无 cfg 处理）：{_e}")
        cfg = None
    if force:
        try:
            # 强取语义：无条件失效一次，保证下面这行数字来自「当前配置」而不是上次的缓存
            qc_client.prompt_qc_cache_isolation_check(cfg, force=True)
        except Exception as _e:  # noqa: BLE001
            _app_logger().debug(f"预检缓存强制失效失败（忽略）：{_e}")
    stats = {}
    try:
        # 传 cfg：顺带跑「配置切换 → 清空预检缓存」的隔离检查，再取数字
        stats = qc_client.log_prompt_qc_cache_stats(
            source="接口 /api/qc/prompt-cache/stats", force=force, cfg=cfg)
    except Exception as _e:  # noqa: BLE001
        _app_logger().debug(f"预检缓存统计输出失败（忽略）：{_e}")
    if not stats:
        stats = qc_client.prompt_qc_cache_stats()
    if not stats:
        return jsonify({"success": False, "stats": {},
                        "error": "预检缓存统计不可用（prompt_qc 未加载或被裁剪）"})
    return jsonify({"success": True, "force": bool(force), "stats": stats,
                    "line": prompt_protocol.format_prompt_qc_cache_diag(
                        stats, source="接口 /api/qc/prompt-cache/stats")})


@qc_bp.route('/api/qc/frames/<path:filename>')
def api_qc_frame_file(filename):
    """回显视频质检抽帧图（只读）"""
    safe = filename.replace('\\', '/')
    target = os.path.abspath(os.path.join(QC_DIR, safe))
    root = os.path.abspath(QC_DIR)
    # 审计 P2（2026-09-29）：前缀必须带路径分隔符 —— 否则 output/qc_backup 等
    # 「qc 开头」的兄弟目录也能通过前缀判断（与 _serve_safe 的 base+os.sep 口径对齐）
    if not target.startswith(root + os.sep):
        abort(403)
    if not os.path.exists(target):
        abort(404)
    return send_file(target, conditional=True)
