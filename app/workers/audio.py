# -*- coding: utf-8 -*-
'''配音 / 混音 worker（2026-10-11 从 app.py 下沉，第三批）。'''

# 本批 8 个对象共 554 行：_dub_worker、_mix_worker 及它们独占的 6 个助手。
# 前置（已先行完成）：dub_tasks/dub_lock/mix_tasks/mix_lock 已归位到 job_state，
#   本模块直接 from job_state import，不再依赖 app 实例。
# 函数体与下沉前逐字一致（唯一替换：logger -> logger）。
import logging
import os
import shutil
import threading
import time

from job_state import dub_lock, dub_tasks, mix_lock, mix_tasks

# 2026-10-11 补齐搬迁时遗漏的模块级名字（自动扫描发现）
from artifact_helpers import _purge_rejected_artifacts
from config import DUB_DIR
from config import QC_DIR
from config import TTS_DEFAULT_PARAMS
from dub_mix import DubMixError
from dub_mix import mix_out_dir
from dub_mix import mix_video_with_entries
from dub_mix import write_mix_report
from fs_atomic import atomic_write_json
from routes._shared import _prune_task_registry
from routes._shared import _qc_load_cfg
from routes._shared import _safe_project
from routes._shared import register_final_deliverable
from routes.tts import _dub_audio_url
from tts_client import QwenTTSClient
from tts_client import TTSError
from tts_client import concat_audio
from tts_client import probe_audio as probe_audio_info
import audio_qc
import prompt_qc
import qc_client
import tts_client
logger = logging.getLogger(__name__)

def _cleanup_scratch_dir(dir_path: str, logger=None) -> None:
    """清空目录内容（保留目录本身），失败时只记日志不抛异常。"""
    import logging
    _log = logger or logging.getLogger(__name__)
    if not dir_path or not os.path.isdir(dir_path):
        return
    try:
        for fn in os.listdir(dir_path):
            fp = os.path.join(dir_path, fn)
            try:
                if os.path.isdir(fp):
                    import shutil
                    shutil.rmtree(fp, ignore_errors=True)
                else:
                    os.remove(fp)
            except OSError:
                _log.warning("清理中间产物失败：%s", fp)
        _log.info("已清理 scratch 目录：%s", dir_path)
    except OSError as e:
        _log.warning("清理 scratch 目录失败：%s", e)


def _audio_line_expect_sec(line: dict) -> float:
    """该句配音的期望时长（由台词字数推算；推算不出时退回镜头时长）

    只用于「时长偏差」这一条软判据，因此宁松勿紧：优先用字数推算（能发现「被截断」），
    推算不出（空台词）时才退回剧本给的镜头时长，避免拿 0 当期望值把一切都判成偏差。
    """
    est = audio_qc.estimate_speech_sec(line.get("text"))
    if est > 0:
        return est
    try:
        return max(0.0, float(line.get("duration_hint") or 0))
    except (TypeError, ValueError):
        return 0.0


def _dub_prompt_preflight(lines: list, project_name: str = "") -> dict:
    """配音台词生成前预检：就地自愈 ``lines[i]["text"]``，结论写入 ``lines[i]["prompt_qc"]``。

    永不抛异常（预检是保险，保险本身出问题不能耽误配音）。
    """
    stats = {"enabled": False, "checked": 0, "repaired": 0, "blocked": 0,
             "issue_lines": 0, "repaired_lines": 0, "problem_lines": []}
    if not lines:
        return stats
    try:
        cfg = _qc_load_cfg()
        if not prompt_qc.prompt_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        mode = prompt_qc.prompt_qc_mode(cfg)
        for ln in lines:
            text = ln.get("text") or ""
            ctx = {
                "project_name": project_name,
                "shot_id": ln.get("shot_id"),
                "character": ln.get("character"),
                "emotion": ln.get("emotion"),
                # preset（CustomVoice）会忽略 instruct → 情绪送不进 TTS，预检据此提示
                "voice_mode": (ln.get("voice") or {}).get("mode"),
                # 剧本没写 speaker（或写了未登记角色）时 build_dub_plan 落到「旁白」音色，
                # 角色台词会被旁白念 —— 用「最终音色是不是旁白兜底」判定，而不是旧写法
                # `source == "narration"`（旁白通道关闭后 source 恒为 dialogue，那个判据永远为假，
                # 等于这条预检规则静默失效）。
                "speaker_fallback": str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER,
            }
            pf = prompt_qc.preflight("audio", text, ctx=ctx, cfg=cfg)
            verdict = pf.get("verdict") or {}
            stats["checked"] += 1
            if pf.get("repairs"):
                stats["repaired"] += 1
            if verdict.get("issues"):
                stats["issue_lines"] += 1
            # ⚠️ 自愈结果为空时**保留原文**：把台词改成空串会让该句直接合成失败/静音，
            #    比「带一点噪音」更糟。空台词交给调用方按 rebuild_hint 从剧本重建。
            new_text = pf.get("prompt") or ""
            if new_text and new_text != text:
                ln["text"] = new_text
                stats["repaired_lines"] += 1
            ln["prompt_qc"] = {
                "mode": mode,
                "passed": bool(verdict.get("passed")),
                "blocked": bool(pf.get("blocked")),
                "score": verdict.get("score"),
                "issues": list(verdict.get("issues") or []),
                "critical_issues": list(verdict.get("critical_issues") or []),
                "repairs": list(pf.get("repairs") or []),
                "label": pf.get("label") or "",
                "reason": pf.get("reason") or "",
                "rebuild_hint": pf.get("rebuild_hint") or "",
            }
            if pf.get("blocked") or verdict.get("issues"):
                if pf.get("blocked"):
                    stats["blocked"] += 1
                if len(stats["problem_lines"]) < 20:
                    stats["problem_lines"].append({
                        "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
                        "character": ln.get("character"),
                        "blocked": bool(pf.get("blocked")),
                        "issues": list(verdict.get("issues") or [])[:3]
                                  + list(verdict.get("critical_issues") or [])[:2],
                        "repairs": list(pf.get("repairs") or []),
                        "rebuild_hint": pf.get("rebuild_hint") or "",
                    })
                # 配音台词的预检缺陷同样沉淀为「提示词质检」教训（此前只进 stats 与
                # ln["prompt_qc"]，不进教训库）；键用自愈前的原文 text。
                if project_name:
                    try:
                        _record_preflight_lesson(project_name, text, pf,
                                                 prompt_qc.prompt_qc_gate(pf, cfg))
                    except Exception as _dub_lesson_err:  # noqa: BLE001
                        logger.warning("配音提示词教训沉淀失败（忽略）：%s",
                                           _dub_lesson_err)
    except Exception as e:  # noqa: BLE001 - 预检失败绝不影响配音
        logger.warning(f"配音台词预检异常（已跳过，不影响配音）：{e}")
    return stats


def _audio_qc_lines(project_name: str, lines: list, results: list, cfg: dict,
                    retry_cb=None) -> dict:
    """配音成品逐句质检（客观层 + AI 层），结论写入 ``results[i]["audio_qc"]``。

    ``retry_cb(line, result) -> dict|None``：可选的重配合回调。**只对客观层判致命的句子
    调用**（整段无声/空文件）—— 这类失败属于「合成出了东西但不是人声」，重配一次是最有效
    的补救；软扣分项（音量偏小、时长偏差）不重配，交由用户决定。

    永不抛异常；批量口径为「记录 + 有限重配」，不阻断整集。
    """
    from lesson_helpers import _record_audio_qc_lesson, _record_preflight_lesson
    stats = {"enabled": False, "checked": 0, "passed": 0, "failed": 0, "blocked": 0,
             "ai_used": 0, "retried": 0, "recovered": 0, "problems": []}
    if not results:
        return stats
    try:
        if not qc_client.audio_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        by_id = {str(l.get("line_id")): l for l in (lines or [])}
        visuals_root = os.path.join(QC_DIR, "audio", _safe_project(project_name or "project"))
        for r in results:
            if not r.get("ok") or not r.get("out_path"):
                continue
            ln = by_id.get(str(r.get("line_id"))) or {}
            expect = _audio_line_expect_sec(ln)
            verdict = qc_client.check_audio(
                r["out_path"], expect_sec=expect or None,
                line_text=ln.get("text") or r.get("text") or "", cfg=cfg,
                visuals_dir=os.path.join(visuals_root,
                                         os.path.splitext(os.path.basename(r["out_path"]))[0]))
            stats["checked"] += 1
            # 致命（整段无声/空文件）→ 重配一次。⚠️ 计数必须在重配之后按**最终**结论统计：
            # 先记 blocked 再重配会出现「致命 1 句 / 未通过 0 句」这种自相矛盾的汇总，
            # 前端与任务消息都在读这两个数，口径必须一致。
            if verdict.get("blocked") and retry_cb is not None:
                try:
                    stats["retried"] += 1
                    again = retry_cb(ln, r)
                    if again:
                        verdict = again
                        if not verdict.get("blocked"):
                            stats["recovered"] += 1
                except Exception as e:  # noqa: BLE001 - 重配失败不影响已有结论
                    logger.warning(f"音频质检重配失败（{r.get('line_id')}）：{e}")
            if verdict.get("blocked"):
                stats["blocked"] += 1
            if verdict.get("ai_used"):
                stats["ai_used"] += 1
            if verdict.get("passed"):
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                # T03b：配音成品质检不达标 → 沉淀 kind="audio" 教训（键 = 该句 TTS 输入原文，
                # 自愈前用 audio_orig_text）。verdict.ok=false（接口异常）时 verdict 无有效缺陷，
                # 不沉淀，避免把「质检调用失败」记成「这句配音有问题」。
                if verdict.get("ok", True) and ln:
                    _record_audio_qc_lesson(project_name, ln, verdict)
                # ★ 用户需求：质检不合格的配音不留本地。⚠️ **仅在最终 failed（重配也失败）时删**：
                # `blocked`（整段无声/空文件）已由 retry_cb 重配过一次，重配若恢复则 verdict
                # 被替换、不会走到这里；能走到这里说明**最终结论仍不合格**。删除条件是
                # 「质检成功返回（ok 非 False）且最终 not passed」—— ok=False（接口故障）不删。
                # 删：该句 wav（P9）+ 其可视化目录（P10 output/qc/audio/<项目>/<stem>/）。
                if verdict.get("ok", True) and r.get("out_path"):
                    try:
                        _stem = os.path.splitext(os.path.basename(r["out_path"]))[0]
                        _purge_rejected_artifacts(
                            [r["out_path"], os.path.join(visuals_root, _stem)],
                            project=project_name,
                            reason=f"配音质检不合格（{verdict.get('reason') or ''}）"[:120],
                            kind="audio_line")
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning(f"不合格配音清理失败（忽略）：{_pe}")
                if len(stats["problems"]) < 20:
                    stats["problems"].append({
                        "line_id": r.get("line_id"), "shot_id": r.get("shot_id"),
                        "character": r.get("character"),
                        "blocked": bool(verdict.get("blocked")),
                        "score": verdict.get("score"),
                        "reason": str(verdict.get("reason") or "")[:200],
                        "metrics": verdict.get("metrics") or {},
                        "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                                    f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                                    for p in (verdict.get("visuals") or [])],
                    })
            r["audio_qc"] = {
                "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
                "score": verdict.get("score"),
                "reason": str(verdict.get("reason") or "")[:300],
                "issues": list(verdict.get("issues") or [])[:5],
                "critical_issues": list(verdict.get("critical_issues") or [])[:3],
                "metrics": verdict.get("metrics") or {},
                "ai_used": bool(verdict.get("ai_used")),
                "ai_skipped": bool(verdict.get("ai_skipped")),
                "ai_skip_reason": verdict.get("ai_skip_reason") or "",
                "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                            f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                            for p in (verdict.get("visuals") or [])],
            }
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响配音产物
        logger.warning(f"配音成品质检异常（已跳过，不影响配音）：{e}")
    if stats["enabled"]:
        logger.info(f"配音质检（{project_name}）：检查 {stats['checked']} 句，"
                        f"通过 {stats['passed']}，未通过 {stats['failed']}，"
                        f"致命 {stats['blocked']}，重配 {stats['retried']}，"
                        f"恢复 {stats['recovered']}，AI 层 {stats['ai_used']}")
    return stats


def _mix_audio_qc(report: dict, cfg: dict) -> dict:
    """带配音成片的音频质检（整轨口径）。

    ⚠️ 必须关掉「有声占比下限」：成片天然有大段无台词留白（无台词镜头/纯环境音），
    拿单句的 50% 标准去卡它必然误报「漏句」。整轨真正要挡的是**整条音轨近乎无声**
    （amix 失败 / 全部条目静音）与**不含音频流** —— 这两条都在客观层的硬闸里。
    """
    try:
        if not qc_client.audio_qc_ready(cfg):
            return {"enabled": False, "reason": "音频质检开关未开启"}
        out = report.get("output_path") or ""
        before = report.get("video_before") or {}
        expect = 0.0
        try:
            expect = float(before.get("duration") or 0)
        except (TypeError, ValueError):
            expect = 0.0
        project_name = _safe_project(report.get("project") or "project")
        stem = os.path.splitext(os.path.basename(out))[0]
        verdict = qc_client.check_audio(
            out, expect_sec=expect or None, cfg=cfg,
            check_speech_ratio=False,
            visuals_dir=os.path.join(QC_DIR, "audio_mix", project_name, stem))
        metrics = verdict.get("metrics") or {}
        out_v = {
            "enabled": True,
            "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
            "score": verdict.get("score"),
            "reason": str(verdict.get("reason") or "")[:300],
            "issues": list(verdict.get("issues") or [])[:5],
            "critical_issues": list(verdict.get("critical_issues") or [])[:3],
            "metrics": metrics,
            "ai_used": bool(verdict.get("ai_used")),
            "ai_skipped": bool(verdict.get("ai_skipped")),
            "ai_skip_reason": verdict.get("ai_skip_reason") or "",
            "visuals": [f"/api/qc/frames/audio_mix/{project_name}/{stem}/"
                        f"{os.path.basename(p)}" for p in (verdict.get("visuals") or [])],
            "coverage_sec": report.get("coverage_sec"),
            "video_duration": expect or None,
        }
        # 配音覆盖率：逐句音频总时长 / 视频时长。**两端都要看**：
        #   偏低（<50%）→ 大量镜头没有配音落点；
        #   偏高（>115%）→ 台词总长超过画面，末尾整段被 `-shortest` **静默截掉**
        #     （成片仍「有声音」所以客观层查不出来，但台词已经丢了一大半）。
        #   ⚠️ 曾只写「偏低」这一个方向，实测把 ep04（台词 606.4s / 画面 85.2s，
        #      21 句里 17 句落在片外）这条最该拦的缺陷直接放过了 —— 覆盖率是**比值**，
        #      单向判定等于漏掉一半语义。
        try:
            cov = float(report.get("coverage_sec") or 0)
            if expect > 0:
                ratio = cov / expect
                out_v.setdefault("issues", [])
                if ratio < 0.5:
                    out_v["issues"].append(
                        f"配音覆盖偏低：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                        f"（{ratio * 100:.0f}%）")
                elif ratio > 1.15:
                    entries = report.get("entries") or []
                    dropped = 0
                    for e in entries:
                        try:
                            if float(e.get("start") or 0) >= expect:
                                dropped += 1
                        except (TypeError, ValueError):
                            continue
                    detail = (f"，其中 {dropped}/{len(entries)} 句起始点已在片长之外、放不出来"
                              if dropped else "")
                    msg = (f"配音总长超出画面：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                           f"（{ratio * 100:.0f}%）{detail} —— 超出部分会被合成命令静默截断")
                    out_v["issues"].append(msg)
                    out_v.setdefault("critical_issues", [])
                    out_v["critical_issues"].append(msg)
                    # 单向收紧：客观层/AI 层说通过也翻不回来
                    out_v["blocked"] = True
                    out_v["passed"] = False
                    try:
                        out_v["score"] = min(int(out_v.get("score") or 0), 40)
                    except (TypeError, ValueError) as e:
                        logger.debug("评分字段解析失败（忽略）：%s", e)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            logger.debug("评分归一化计算失败（忽略）：%s", e)
        # ★ 用户需求：质检不合格的混音不留本地（P10 可视化目录）。仅当最终「质检成功返回
        # 且不合格」（ok 非 False 且 passed=False）时删；ok=False（接口故障）不删。
        if verdict.get("ok") is not False and not out_v.get("passed"):
            try:
                _purge_rejected_artifacts(
                    [os.path.join(QC_DIR, "audio_mix", project_name, stem)],
                    project=project_name,
                    reason=f"混音质检不合格（{out_v.get('reason') or ''}）"[:120],
                    kind="audio_mix")
            except Exception as _pe:  # noqa: BLE001
                logger.warning(f"不合格混音清理失败（忽略）：{_pe}")
        return out_v
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响合成结果
        logger.warning(f"成片音频质检异常（已跳过）：{e}")
        return {"enabled": True, "passed": None, "error": f"{type(e).__name__}: {e}"}


def _mix_audio_url(project_name: str, rel_path: str) -> str:
    base = os.path.abspath(mix_out_dir(project_name))
    p = os.path.abspath(rel_path)
    if not p.startswith(base + os.sep):
        return ""
    rel = os.path.relpath(p, base).replace(os.sep, "/")
    return f"/api/mix/file/{project_name}/{rel}"


def _dub_worker(task_id: str, project_name: str, plan: dict, out_dir: str,
                fmt: str, episode: int):
    """后台配音：批量合成逐句音频 → 合并整集音轨 → 落盘清单"""
    try:
        lines = plan.get("lines") or []
        if not lines:
            raise TTSError("配音计划为空（剧本中没有可朗读台词，或所选镜头无台词）")

        lines_dir = os.path.join(out_dir, "lines")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        for ln in lines:
            ln["project_tag"] = project_name

        # ---- ① 生成前提示词预检（配音台词）----
        # 台词会被 TTS 逐字念出来：结构化残留（`(S1) 说：[Chinese] …`）、舞台指示
        # （`（转身冷笑）`）都会原样进成片；空台词/纯标点则合成出静音却显示「成功」。
        # 这一层零模型依赖、默认开启，在消耗 GPU 之前把确定性缺陷挡住/修掉。
        qc_cfg = _qc_load_cfg()
        pre = _dub_prompt_preflight(lines, project_name)
        if pre.get("blocked") or pre.get("repaired_lines"):
            with dub_lock:
                dub_tasks[task_id].update({"prompt_qc": pre})

        def _cb(done, total, last, note):
            with dub_lock:
                dub_tasks[task_id].update({
                    "current": done, "total": total,
                    "progress": int(done / max(1, total) * 90),
                    "phase": f"配音合成中（{done}/{total}）",
                    "message": f"最新：{last.get('character') or ''} {str(last.get('text') or '')[:18]}",
                })

        results = client.synthesize_lines(lines, lines_dir, progress_cb=_cb)
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
            })

        # ---- ② 成品质检（音频客观层 + 频谱/波形 AI 层）----
        # 「合成成功」不等于「念出来了」：节点正常返回、文件也落盘，但整段可以是静音
        # （漏配音 / 模型未发声）。旧流程要等到成片验收才发现整集缺一句。
        # 这里逐句实测，致命的（整段无声/空文件）当场重配一次，软扣分项只记录。
        def _retry_line(ln, rec):
            """重配单句并重新质检（只对客观层判致命的句子调用）"""
            import copy as _copy
            one = _copy.deepcopy(ln)
            one["project_tag"] = project_name
            res = client.synthesize_lines([one], lines_dir)
            if not res or not res[0].get("ok"):
                return None
            rec.update({k: v for k, v in res[0].items() if k != "audio_qc"})
            return qc_client.check_audio(
                rec["out_path"], expect_sec=_audio_line_expect_sec(ln) or None,
                line_text=ln.get("text") or "", cfg=qc_cfg,
                visuals_dir=os.path.join(QC_DIR, "audio", _safe_project(project_name),
                                         os.path.splitext(os.path.basename(rec["out_path"]))[0]))

        if qc_cfg.get("enabled") and qc_cfg.get("audio_enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"phase": "配音质检中（客观指标 + 频谱波形送检）",
                                           "progress": 92})
        aqua = _audio_qc_lines(project_name, lines, results, qc_cfg, retry_cb=_retry_line)
        if aqua.get("enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"audio_qc": aqua})
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
                "audio_qc_failed": aqua.get("failed", 0),
            })

        # 合并整集音轨（按剧本镜头顺序）
        merged = None
        merged_probe = {}
        if ok_items:
            with dub_lock:
                dub_tasks[task_id].update({"phase": "合并整集音轨", "progress": 94})
            order = {l.get("line_id"): i for i, l in enumerate(lines)}
            ok_sorted = sorted(ok_items, key=lambda r: order.get(r.get("line_id"), 9999))
            merged_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub.{fmt}")
            merged = concat_audio([r["out_path"] for r in ok_sorted], merged_path, fmt=fmt)
            merged_probe = probe_audio_info(merged)

        manifest = {
            "project": project_name, "episode": episode,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "script_path": plan.get("script_path") or "",
            "voice_map": plan.get("voice_map") or {},
            "characters": plan.get("characters") or [],
            "merged_audio": merged,
            "merged_info": merged_probe,
            # 质检结论随清单落盘：成片验收时能回溯「这句当时是怎么判的」
            "prompt_qc": pre if pre.get("enabled") else {},
            "audio_qc": aqua if aqua.get("enabled") else {},
            "lines": [dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                      for r in results],
        }
        manifest_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」
        atomic_write_json(manifest_path, manifest)

        _msg = f"成功 {len(ok_items)} 句 / 失败 {len(results) - len(ok_items)} 句"
        if aqua.get("enabled") and aqua.get("checked"):
            _msg += f"；质检通过 {aqua['passed']}/{aqua['checked']} 句"
            if aqua.get("recovered"):
                _msg += f"（重配恢复 {aqua['recovered']} 句）"
        with dub_lock:
            dub_tasks[task_id].update({
                "status": "completed" if ok_items else "failed",
                "progress": 100, "phase": "配音完成" if ok_items else "配音失败",
                "message": _msg,
                "merged_audio": merged,
                "merged_url": _dub_audio_url(project_name, merged) if merged else "",
                "merged_info": merged_probe,
                "manifest": manifest_path,
                "error": "" if ok_items else "全部句子合成失败，请查看 results 中的错误原因",
            })
        with dub_lock:
            _prune_task_registry(dub_tasks)
    except (TTSError, OSError) as e:
        logger.error(f"配音任务失败: {e}")
        # B-16 P2-11：配音失败 → 清理本任务产生的中间产物（lines 目录、merged 半成品）
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": str(e), "phase": "失败"})
    except Exception as e:  # noqa: BLE001
        logger.exception("配音任务异常")
        # B-16 P2-11：配音异常 → 清理中间产物
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": f"异常：{e}", "phase": "失败"})


def _mix_worker(task_id: str, prepared: dict, out_name: str):
    """后台合成：逐句对齐混音 → 落盘带配音成片 + 报告"""
    try:
        project_name = prepared["project_name"]
        out_dir = mix_out_dir(project_name)
        out_path = os.path.join(out_dir, out_name)
        with mix_lock:
            mix_tasks[task_id].update({"phase": "音画对齐混音中", "progress": 30})
        report = mix_video_with_entries(prepared["video_path"], prepared["entries"],
                                        out_path, prepared["params"])
        # ---- 成品音频质检：成片音轨是不是真的有人声 ----
        # ffmpeg 返回成功、文件也有音频流，并不代表「配音真的混进去了」：
        # 条目路径错、amix 被压成静音、源片段本身无声，都能产出一条「合法但没声音」的
        # 音轨。这里对**最终成片**实测一遍（整轨口径，不做有声占比判定）。
        report["audio_qc"] = _mix_audio_qc(report, _qc_load_cfg())
        report.update({
            "task_id": task_id, "project": project_name, "episode": prepared["episode"],
            "mode": prepared["mode"], "video_source": prepared["video_path"],
            "script_path": prepared["script_path"], "manifest_path": prepared["manifest_path"],
            "segments_dir": prepared["segments_dir"], "warnings": prepared["warnings"],
            "timeline": prepared["timeline"], "entries": prepared["entries"],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        report_path = os.path.join(out_dir, f"{os.path.splitext(out_name)[0]}_mix_report.json")
        write_mix_report(report, report_path)
        _aq = report.get("audio_qc") or {}
        _aq_msg = ""
        if _aq.get("enabled") and _aq.get("passed") is not None:
            _aq_msg = "；音频质检通过" if _aq.get("passed") else \
                f"；音频质检未通过（{str(_aq.get('reason') or '')[:60]}）"
        with mix_lock:
            mix_tasks[task_id].update({
                "status": "completed", "progress": 100, "phase": "合成完成",
                "message": (f"已合成 {report['entry_count']} 句配音，"
                            f"耗时 {report['elapsed_sec']}s{_aq_msg}"),
                "output_path": report["output_path"],
                "report_path": report_path,
                "url": _mix_audio_url(project_name, report["output_path"]),
                "audio_qc": _aq,
                "result": report,
            })
        with mix_lock:
            _prune_task_registry(mix_tasks)
        # 带配音成片＝用户真正要验收的成品：自动登记进「成品验收」队列
        reg = register_final_deliverable(
            project_name, prepared["episode"], report["output_path"],
            meta={"source": "mix", "mode": prepared["mode"],
                  "entry_count": report.get("entry_count"),
                  "video_source": os.path.basename(prepared["video_path"] or ""),
                  "report": os.path.basename(report_path)})
        with mix_lock:
            mix_tasks[task_id]["deliverable"] = {
                "registered": bool(reg.get("registered")),
                "reason": reg.get("reason") or "",
                "episode_no": int(prepared["episode"] or 1),
                "path": report["output_path"],
            }
        if not reg.get("registered"):
            logger.info(f"成片未登记待验收（{project_name} 第{prepared['episode']}集）："
                            f"{reg.get('reason')}")
    except DubMixError as e:
        logger.warning(f"混音合成失败: {e}")
        # B-16 P2-11：混音失败 → 清理本任务产生的中间产物（未完成的 report / 半成品）
        _cleanup_scratch_dir(out_dir, logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成失败",
                                       "message": str(e), "progress": 100})
    except Exception as e:  # pragma: no cover - 兜底
        logger.exception("音画合成异常")
        # B-16 P2-11：混音异常 → 清理中间产物
        _cleanup_scratch_dir(out_dir, logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成异常",
                                       "message": f"{type(e).__name__}: {e}", "progress": 100})
