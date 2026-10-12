# -*- coding: utf-8 -*-
"""成片生成与取文件 API 蓝图（Blueprint 拆分第十四批，2026-10-08）。

URL 规则与响应体一字不改，只把 @app.route 换成 @final_bp.route。
日志器用上下文安全的 _app_logger()；跨域助手来自 routes/_shared.py。

⭐ 2026-10-12（t17）：「生成成片」不再走 video_postprocess.generate_final_video 的
「逐镜拼接」老路 —— 整集模式（唯一模式）下那会把目录里**返工残留**的 shot_NN.mp4
当成唯一素材：t11 实测产出 2.021s/64980B 的截断成片，并**原地覆盖**正式成片路径
（并且 probe_final 之后恒判 done，流水线也不会再修）。现在：
  · 有整集原片（<tag>_full.mp4）→ 走托管流水线的 pipeline.step_final（超分产物失效判断、
    「已失效」标记清除、字幕烧录、幂等短路全部复用），来源恒为整集原片/未失效的超分产物；
  · 没有整集原片（遗留逐镜布局）→ 保留 generate_final_video 兜底，但产物**必须**过
    「长度/完整性守卫」，不通过就回滚 + 400，绝不让截断片覆盖正式成片。
"""
import json
import os
import shutil
import time

from flask import Blueprint, jsonify, request, send_file

import pipeline
import project_store
import video_watermark
from config import FINAL_DIR, VIDEOS_DIR, WATERMARK_DIR
from media_probe import probe_media
from video_postprocess import VideoPostProcessor
from routes._shared import (_app_logger, _body, _ep_dir, _project_or_400, _serve_safe,
                            _wm_load_cfg, register_final_deliverable)

final_bp = Blueprint('final', __name__)

#: 成片完整性守卫的硬下限（秒）——与 register_final_deliverable 的 2s 闸门同口径
_MIN_FINAL_SEC = 2.0
#: 相对参考时长（上一版成片 / 整集原片）的比例下限
_MIN_FINAL_RATIO = 0.6


def _wm_apply_to_final(final_path: str, project_name: str) -> dict:
    """成片后处理：水印开启时额外产出一份带水印成片；默认关闭则整体跳过"""
    try:
        cfg = _wm_load_cfg()
        if not cfg.get("enabled"):
            return {"enabled": False, "skipped": True,
                    "message": "视频水印未开启（默认关闭，成片保持无水印）"}
        out_dir = os.path.join(WATERMARK_DIR, project_name)
        res = video_watermark.apply_watermark(final_path, cfg=cfg, out_dir=out_dir)
        out_path = res.get("out_path") or ""
        res.update({
            "type": cfg.get("type"), "mode": cfg.get("mode"),
            "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode"), cfg.get("mode")),
            "url": (f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}"
                    if out_path else ""),
        })
        if not res.get("ok"):
            _app_logger().warning(f"成片水印烧写失败（不影响无水印成片）：{res.get('error')}")
        return res
    except Exception as e:  # noqa: BLE001
        _app_logger().warning(f"成片水印处理异常（忽略）：{e}")
        return {"enabled": True, "ok": False, "error": str(e)}


video_processor = VideoPostProcessor()


# ===================== t17：整集成片来源与完整性守卫 =====================

def _media_seconds(path: str) -> float:
    """ffprobe 实测时长（秒）；探不到（或路径为空）返回 0.0，不抛异常"""
    if not path:
        return 0.0
    try:
        return float((probe_media(path) or {}).get("duration") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _episode_final_path(project_name: str, ep: int) -> str:
    """正式成片路径（与 pipeline.final_path 同口径：FINAL_DIR/<项目>/epNN_final.mp4）"""
    return os.path.join(FINAL_DIR, project_name, f"ep{int(ep):02d}_final.mp4")


def _episode_film_path(project_name: str, ep: int) -> str:
    """本集「整集原片」路径（第 1 集平铺、第 2 集起 epNN/；与 app._ep_dir 同口径）

    只认整集产物（<tag>_full.mp4 / episode_full.mp4），**不认** shot_NN.mp4 ——
    后者在整集模式下只是 retry_shot 的返工残留，拿它拼成片就是 t11 的截断事故。
    """
    d = _ep_dir(os.path.join(VIDEOS_DIR, project_name), int(ep))
    for cand in (f"ep{int(ep):02d}_full.mp4", "episode_full.mp4"):
        p = os.path.join(d, cand)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return ""


def _shot_file_count(project_name: str, ep: int) -> int:
    """该集视频目录里的 shot_*.mp4 个数（整集模式下 = 返工残留数）"""
    d = _ep_dir(os.path.join(VIDEOS_DIR, project_name), int(ep))
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    return len([n for n in names
                if n.startswith("shot_") and n.lower().endswith(".mp4")
                and not n.endswith(".bak.mp4")])


def _snapshot_final(path: str) -> str:
    """生成前备份正式成片（后缀 .pregen.bak，不带 .mp4：G8 约定，避免被 glob 当成分片/成片）

    返回备份路径；文件不存在/备份失败返回 ""（此时守卫不通过只能删残次品，无法回滚）。
    """
    try:
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            bak = path + ".pregen.bak"
            shutil.copy2(path, bak)
            return bak
    except OSError as e:
        _app_logger().warning("[成片守卫] 备份正式成片失败（守卫失败时将无法回滚）：%s", e)
    return ""


def _drop_backup(backup: str) -> None:
    """生成成功 → 丢掉备份（不留 .bak 垃圾）"""
    try:
        if backup and os.path.isfile(backup):
            os.remove(backup)
    except OSError as e:
        _app_logger().debug("[成片守卫] 清理备份失败（忽略）：%s", e)


def _restore_final(path: str, backup: str) -> None:
    """守卫不通过 / 合成异常 → 正式成片还原成上一版；没有备份就删掉这次产出的残次品。

    为什么必须回滚：正式路径是「成品验收」里用户正在看的那一份；被 2s 截断片覆盖后
    probe_final 会判 done，流水线也不会再修，用户会拿着半截片去发布。
    """
    try:
        if backup and os.path.isfile(backup):
            os.replace(backup, path)
            _app_logger().warning("[成片守卫] 已回滚正式成片到上一版：%s", path)
        elif path and os.path.isfile(path):
            os.remove(path)
            _app_logger().warning("[成片守卫] 已删除未通过守卫的成片：%s", path)
    except OSError as e:
        _app_logger().error("[成片守卫] 回滚失败（请人工检查 %s）：%s", path, e)


def _final_guard(produced: str, final_out: str, film: str, backup: str,
                 script: dict, project_name: str, ep: int) -> dict:
    """成片完整性守卫（t17）：拒绝「2s 级截断成片」落进正式路径。

    判据（任一不满足即拒收）：
      1) 产物存在且非空；
      2) 能探到时长（不可播放/没写完的容器一律拒收）；
      3) 时长 >= 2.0s（与 register_final_deliverable 的硬闸门同口径）；
      4) 有参考时长（上一版成片 / 整集原片）时，产物时长 >= 0.6 x 参考时长；
      5) 没有整集原片（遗留逐镜布局）时，目录里的 shot_*.mp4 数不得低于剧本镜头数的一半
         —— 这正是 t11 实测「1 个返工片段拼出 2.021s 成片」的场景。
    """
    info = {"ok": False, "reason": "", "produced_sec": 0.0, "reference_sec": 0.0,
            "reference": "", "shots_in_script": len((script or {}).get("shots") or []),
            "shot_files": 0, "official_path": os.path.abspath(final_out or "")}
    if not produced or not os.path.isfile(produced):
        info["reason"] = "合成未产出文件"
        return info
    info["produced"] = os.path.abspath(produced)
    produced_sec = _media_seconds(produced)
    info["produced_sec"] = round(produced_sec, 3)
    ref_sec, ref_from = 0.0, ""
    for tag, val in (("上一版成片", _media_seconds(backup)), ("整集原片", _media_seconds(film))):
        if val > ref_sec:
            ref_sec, ref_from = val, tag
    info["reference_sec"] = round(ref_sec, 3)
    info["reference"] = ref_from
    if os.path.abspath(produced) != os.path.abspath(final_out or ""):
        # 理论上不会发生（两条合成路径都写正式路径）；真发生则只做粗检，不动正式路径上
        # 既有文件
        info["ok"] = produced_sec >= _MIN_FINAL_SEC
        info["reason"] = "" if info["ok"] else f"产物不在正式路径且仅 {produced_sec:.2f}s"
        return info
    if produced_sec <= 0:
        info["reason"] = "探不到成片时长（不可播放或未写完）"
        return info
    if produced_sec < _MIN_FINAL_SEC:
        info["reason"] = f"成片仅 {produced_sec:.2f}s（< {_MIN_FINAL_SEC}s），疑似片段"
        return info
    if ref_sec > 0 and produced_sec < _MIN_FINAL_RATIO * ref_sec:
        info["reason"] = (f"成片仅 {produced_sec:.2f}s，远短于参考（{ref_from} {ref_sec:.2f}s）"
                          " → 疑似截断，拒绝覆盖正式成片")
        return info
    if not film:
        shots = info["shots_in_script"]
        n_shot = _shot_file_count(project_name, ep)
        info["shot_files"] = n_shot
        if shots >= 4 and n_shot < max(2, shots // 2):
            info["reason"] = (f"遗留逐镜布局：剧本 {shots} 镜但只找到 {n_shot} 个 shot_*.mp4"
                              " → 疑似返工残留，拒绝用它拼成片")
            return info
    info["ok"] = True
    info["reason"] = "通过完整性守卫"
    return info


def _compose_via_pipeline(project_name: str, ep: int, script: dict) -> str:
    """用托管流水线的 step_final 合成整集成片（整集模式唯一正确来源）。

    复用它的全部既有契约：超分产物优先 + **失效标记判断**、成片「已失效」标记清除、
    打回(rejected)后强制重跑、字幕烧录（含 caption）、成片已存在时的幂等短路。
    失败抛 pipeline.PipelineError（例如整集视频未就绪），由调用方决定怎么报。
    """
    ctx = {"project_name": project_name, "episode_no": int(ep),
           "episode_tag": f"ep{int(ep):02d}", "script": script or {},
           "config": {}, "progress": lambda *a, **k: None}
    res = pipeline.step_final(ctx) or {}
    return str(res.get("artifact") or "")


@final_bp.route('/api/final/video', methods=['POST'])
def api_generate_final():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（成片合成需定位项目内剧本，缺省无合理语义）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    script_path = data.get('script_path', '')

    # P0-4：剧本路径必须先落在项目输出目录内（与 project_store.bind_script 同口径），
    # 越界（如 C:/Windows/... 或项目外路径）直接拒读，避免被 index.json 里被污染的
    # 绝对路径拖出目录读走任意文件。
    if not script_path or not project_store.is_path_inside_output(script_path):
        return jsonify({"error": "剧本路径必须在项目输出目录内（output/），越界路径已拒读"}), 400
    if not os.path.exists(script_path):
        return jsonify({"error": "剧本文件不存在"}), 400

    final_out = ""
    backup = ""
    try:
        # 集号与剧本一起解析：合成必须知道是第几集（审计 S4 —— 旧代码合成完才读集号，
        # 而合成函数压根没有集号入参，于是第 2 集及以后合成的是第 1 集的片段）
        _sc = {}
        ep_no = 0
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                _sc = json.load(f) or {}
            ep_no = int(_sc.get('episode_no')
                        or (_sc.get('metadata') or {}).get('episode_no') or 0)
        except Exception:  # noqa: BLE001 - 剧本读不到就退回第 1 集
            ep_no = 0
        ep = ep_no or 1
        final_out = _episode_final_path(project_name, ep)
        # 生成前先备份正式成片：守卫不通过时能整体回滚（否则会被截断片顶掉）
        backup = _snapshot_final(final_out)
        film = _episode_film_path(project_name, ep)
        if film:
            # ⭐ t17：整集模式（唯一模式）——来源恒为整集原片（或未被标记失效的超分产物）
            output = _compose_via_pipeline(project_name, ep, _sc)
        else:
            # 遗留逐镜布局兜底（老工程仍可用）；产物必须过下面的完整性守卫
            _app_logger().warning("[成片] 未找到整集原片（项目 %s 第 %s 集），回落遗留逐镜布局：%s",
                                  project_name, ep,
                                  _ep_dir(os.path.join(VIDEOS_DIR, project_name), ep))
            output = video_processor.generate_final_video(script_path, project_name, ep_no or 1)
        guard = _final_guard(output, final_out, film, backup, _sc, project_name, ep)
        if not guard.get("ok"):
            _restore_final(final_out, backup)
            # 拒收后成片仍不可信 → 补打失效标记，让托管轮转/下一次生成再重做
            try:
                pipeline.mark_artifact_stale(
                    final_out, "生成的成片未通过完整性守卫",
                    {"guard": guard, "project": project_name, "episode_no": ep})
            except Exception as e:  # noqa: BLE001
                _app_logger().warning("[成片守卫] 补打失效标记失败：%s", e)
            _app_logger().error("[成片守卫] 拒收第%s集成片（项目 %s）：%s",
                                ep, project_name, guard.get("reason"))
            return jsonify({"error": f"成片未通过完整性守卫：{guard.get('reason')}",
                            "guard": guard}), 400
        _drop_backup(backup)
        filename = os.path.basename(output)
        # URL 按「相对 FINAL_DIR 的路径」拼，避免成片落在项目子目录时 404
        try:
            rel_path = os.path.relpath(output, FINAL_DIR).replace(os.sep, "/")
        except ValueError:
            rel_path = f"{project_name}/{filename}"
        resp = {
            "success": True,
            "output_path": output,                       # 保留原字段（本地绝对路径）
            "episode_no": ep,
            "filename": filename,
            "url": f"/api/final/{rel_path}",             # 前端可直接播放/下载的 URL
            "guard": guard,                              # t17：守卫结论回给前端/排障
        }
        # 整集成片落盘 → 自动登记进「成品验收」队列（用户只需看这里）
        reg = register_final_deliverable(
            project_name, ep, output,
            meta={"source": "final_video", "script": os.path.basename(script_path)})
        resp["deliverable"] = {"registered": bool(reg.get("registered")),
                               "reason": reg.get("reason") or "",
                               "episode_no": ep}
        # 视频水印（默认关闭；开启后额外产出一份带水印成片，不影响上面的无水印成片）
        resp["watermark"] = _wm_apply_to_final(output, project_name)
        return jsonify(resp)
    except Exception as e:
        _restore_final(final_out, backup)
        _app_logger().error("[成片] 合成失败（项目 %s）：%s", project_name, e)
        return jsonify({"error": str(e)}), 500


@final_bp.route('/api/final/<path:filename>')
def api_final_file(filename):
    """提供最终成片文件访问（新增：使成片可在页面内联播放/下载）"""
    # conditional=True 支持 Range 请求，视频可拖动进度条
    return _serve_safe(FINAL_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')
