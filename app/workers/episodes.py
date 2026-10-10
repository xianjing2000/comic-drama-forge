# -*- coding: utf-8 -*-
'''分集批量生成 worker（2026-10-11 从 app.py 下沉，第二批）。'''

# 依赖清点（实测）：
#   · novel_to_script / SCRIPT_DIR / NovelParseError  ← 叶子模块
#   · lock / generation_state                        ← job_state（唯一来源）
#   · LLMError                                       ← llm_client
#   · logger                                     ← 改为模块 logger
#   · _salvage_episode_script                        ← 同批一并下沉（只被本文件使用）

# 函数体与下沉前逐字一致（仅 logger → logger）。
import logging
import os

import novel_to_script
from config import SCRIPT_DIR
from job_state import generation_state, lock
from llm_client import LLMError
from novel_parser import NovelParseError

# 2026-10-11 补齐搬迁时遗漏的模块级名字（自动扫描发现）
from config import CONTINUITY_DIR
from config import NOVELS_DIR
from novel_parser import read_novel_text
from shared_novel import _episode_units_for_chapters
from shared_project import _novel_key
from workers.screenplay import _current_llm_client
import chapter_preflight
import continuity
import dialogue_utils
import json
import novel_screenplay
import project_store
logger = logging.getLogger(__name__)

def _salvage_episode_script(path: str, episode_no: int):
    """任务报错后检查剧本产物是否其实可用（**以产物为准**，避免误报失败）

    2026-09-17 E2E 实测：分集任务报「模型未返回有效分镜…未知错误」，
    但 `第1集.json` 其实已落盘、6 镜有效、覆盖率 100%、也已被 /api/episodes 收录 ——
    用户看到 "失败" 会以为白跑一趟。产物存在且镜头非空时，按成功回填统计字段。

    返回可直接并入 results 的统计 dict；产物缺失/不可用则返回 None。
    """
    try:
        if not (path and os.path.isfile(path) and os.path.getsize(path) > 200):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    shots = data.get("shots") or []
    if not shots:
        return None
    meta = data.get("metadata") or {}
    try:
        stats = meta.get("episode_stats") or novel_to_script.build_episode_stats(shots)
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "shots": len(shots),
        "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
        "episode_duration_sec": data.get("episode_duration_sec") or stats.get("duration_sec"),
        "characters": len(data.get("characters") or []),
        "items": len(data.get("items") or []),
        "scenes": len(data.get("scenes") or []),
        "elapsed_sec": meta.get("elapsed_sec"),
        "warnings": meta.get("warnings") or [],
    }


def _episodes_worker(task_id: str, novel_meta: dict, chapters: list, style: str,
                     target_shots: int, overwrite: bool, project_key: str = None,
                     use_screenplay: bool = False):
    key = _novel_key(novel_meta, project_key)
    # 拍摄单元：超长章按语义边界拆成多集
# （⚠️ 2026-10-09 起单集镜数**不再设上限**：生产改为按场次渲染，详见 novel_to_script 同名注释）
    units = _episode_units_for_chapters(novel_meta, chapters)
    total = len(units)

    def report(ep_ordinal, chapter, phase, message, inner_percent):
        overall = int(((ep_ordinal - 1) + (inner_percent or 0) / 100.0) / total * 100)
        with lock:
            generation_state[task_id].update({
                "phase": phase, "progress": min(99, max(1, overall)),
                "current": ep_ordinal, "total": total,
                "current_chapter": chapter.get("title"),
                "current_episode": chapter.get("index"),
                "message": message,
            })

    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")

        results = []
        for i, unit in enumerate(units):
            ep = int(unit["episode_no"])
            chapter = unit["chapter"]
            ch_title = chapter.get("title") or f"第{ep}集"
            out_path = novel_to_script.episode_script_path(SCRIPT_DIR, key, ep)

            if os.path.isfile(out_path) and not overwrite:
                info = None
                for e in novel_to_script.list_episodes(SCRIPT_DIR, key):
                    if int(e.get("episode_no")) == ep:
                        info = e
                        break
                results.append({"episode_no": ep, "chapter_title": ch_title,
                                "status": "skipped", "path": out_path,
                                "project_key": key,
                                "message": "该集已存在，跳过（可勾选覆盖重新生成）",
                                "shots": (info or {}).get("shots", 0),
                                "shot_count": (info or {}).get("shot_count"),
                                "episode_duration_sec": (info or {}).get("episode_duration_sec")})
                report(i + 1, chapter, "skip", f"第{ep}集已存在，跳过", 100)
                continue

            report(i + 1, chapter, "chapter",
                   f"第{ep}集《{ch_title}》：准备章节正文…", 2)

            def cb(phase, cur, tot, msg, pct, _ep=ep, _ord=i + 1, _ch=chapter, _t=ch_title):
                report(_ord, _ch, phase, f"第{_ep}集《{_t}》 {msg}", pct)

            try:
                # 前置解析（2026-10-03 补链，用户口径：前置解析必须先于改写）：
                # 该章还没有 preflight（人物档案/梗概/关键事件/情绪基线）时先自动
                # 跑一次并并入项目级设定库 —— convert_chapter_with_continuity 依赖
                # 它注入防 OOC 约束。一章一集口径下章号=集号，注入键天然对齐。
                # 失败不阻塞改写（fail-open，只告警）。
                try:
                    if not chapter_preflight.load_preflight(
                            CONTINUITY_DIR, key, int(chapter.get("index") or 0)):
                        report(ep, chapter, "preflight",
                               f"第{ep}集：前置解析（人物档案/梗概/关键事件/情绪基线）…", 2)
                        _pf_seg = text[int(chapter.get("start") or 0):
                                       int(chapter.get("end") or 0)]
                        _pf_res = chapter_preflight.preflight_analyze(
                            client,
                            novel_meta.get("title") or novel_meta.get("name") or "",
                            int(chapter.get("index") or 0),
                            chapter.get("title") or "", _pf_seg)
                        chapter_preflight.save_preflight(CONTINUITY_DIR, key, _pf_res)
                        try:
                            chapter_preflight.merge_to_bible(CONTINUITY_DIR, key, _pf_res)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as _pfe:  # noqa: BLE001
                    logger.warning("第%s集前置解析失败（跳过注入，不阻塞）：%s", ep, _pfe)
                # 两段式生产（2026-10-03 ②）：use_screenplay=true 时以「文学剧本」为
                # 原文走改写链路 —— 合成 chapter 使 start/end 覆盖剧本全文（continuity
                # 与覆盖率校验只消费传入的 novel_text[start:end]，零内部改动即生效）。
                # 文学剧本缺失时回退章节原文并告警（fail-open，不阻塞批量）。
                _conv_text = text
                _conv_chapter = chapter
                if use_screenplay:
                    _md = novel_screenplay.load_screenplay(
                        key, int(unit.get("episode_no") or 0))
                    if _md:
                        _conv_text = _md
                        _conv_chapter = {"index": chapter.get("index"),
                                         "title": chapter.get("title"),
                                         "start": 0, "end": len(_md)}
                    else:
                        logger.warning(
                            "第%s集：文学剧本不存在，回退章节原文（建议先调用 "
                            "/api/novels/<id>/screenplay/generate）",
                            unit.get("episode_no"))
                # 跨集连贯性（A/B/C/D）：项目级 bible + 上集摘要卡 + 衔接契约 + state 锚点
                # + 相邻集六类校验 + 命中高危问题时的局部重写，全部由 continuity 编排
                conv = continuity.convert_chapter_with_continuity(
                    client, novel_meta, _conv_text, _conv_chapter, key, CONTINUITY_DIR,
                    style=style, target_shots=target_shots, episode_no=ep,
                    save_dir=SCRIPT_DIR, progress_cb=cb,
                )
                script = conv["script"]
                validation = conv.get("validation") or {}
                path = (conv.get("script_path")
                        or novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep, key))
                # 项目登记：把剧本及其镜头数/每集时长统计写回项目注册表
                if project_key:
                    try:
                        project_store.bind_script(
                            project_key, path, project_store.script_stats(path))
                    except Exception as be:  # noqa: BLE001
                        logger.warning(f"项目剧本登记失败（{project_key}）：{be}")
                # 剧本体检：兜底镜头（dialogue=[] 且 prompt_h3=""）会在配音环节变成
                # 「一句都合不出来」，但这里看起来是「生成成功」，必须把缺口显式带出
                _audit = dialogue_utils.audit_script(script)
                _meta_warnings = list(script["metadata"].get("warnings") or [])
                if _audit["warnings"]:
                    _meta_warnings.extend(_audit["warnings"])
                    logger.warning(f"第{ep}集剧本存在内容缺口：{_audit['warnings']}")
                # P0-3 剧本↔原著一致性（三件套 + 定向修复）结果，随生成结果带出给前端
                _sc = script["metadata"].get("script_consistency") or {}
                results.append({
                    "episode_no": ep, "chapter_title": ch_title, "status": "success",
                    "path": path, "project_name": script["metadata"]["project_name"],
                    "project_key": key,
                    "shots": len(script.get("shots") or []),
                    "shot_count": script.get("shot_count") or len(script.get("shots") or []),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                    "characters": len(script.get("characters") or []),
                    "items": len(script.get("items") or []),
                    "scenes": len(script.get("scenes") or []),
                    "chunks_total": script["metadata"].get("chunks_total"),
                    "chunks_used": script["metadata"].get("chunks_used"),
                    "elapsed_sec": script["metadata"].get("elapsed_sec"),
                    "warnings": _meta_warnings,
                    "script_audit": _audit["stats"],
                    "script_audit_ok": _audit["ok"],
                    "continuity_score": (script["metadata"].get("continuity") or {}).get("validation_score"),
                    "continuity_issues": len(validation.get("issues") or []),
                    "continuity_issue_stats": validation.get("issue_stats") or {},
                    "continuity_rewrite": bool((conv.get("rewrite") or {}).get("triggered")),
                    "continuity_rewrite_shots": (conv.get("rewrite") or {}).get("rewritten_shot_ids") or [],
                    "continuity_dir": continuity.continuity_root(CONTINUITY_DIR, key),
                    "coverage_percent": (conv.get("coverage") or {}).get("coverage_percent"),
                    "coverage_plot_percent": (conv.get("coverage") or {}).get("plot_coverage_percent"),
                    "coverage_detail_percent": (conv.get("coverage") or {}).get("detail_coverage_percent"),
                    "coverage_detail_passed": (conv.get("coverage") or {}).get("detail_passed"),
                    "coverage_passed": (conv.get("coverage") or {}).get("passed"),
                    "coverage_threshold_percent": (conv.get("coverage") or {}).get("threshold_percent"),
                    "coverage_missing": (conv.get("coverage") or {}).get("missing_count"),
                    "coverage_zero_omission": (conv.get("coverage") or {}).get("zero_omission"),
                    "coverage_supplement_shots": (conv.get("coverage") or {}).get("supplement_shots"),
                    "coverage_supplement_rounds": (conv.get("coverage") or {}).get("supplement_rounds"),
                    "coverage_report_path": (conv.get("coverage") or {}).get("report_path"),
                    # P0-3 剧本↔原著一致性：三件套 + 定向修复闭环
                    "consistency_passed": _sc.get("passed"),
                    "consistency_chapter_index": _sc.get("chapter_index"),
                    "consistency_anchor_checked": _sc.get("anchor_checked"),
                    "consistency_anchor_ok": _sc.get("anchor_ok"),
                    "consistency_anchor_deviation": _sc.get("anchor_deviation"),
                    "consistency_anchor_reason": _sc.get("anchor_reason"),
                    "consistency_leak_count": _sc.get("leak_count"),
                    "consistency_leak_shot_ids": _sc.get("leak_shot_ids") or [],
                    "consistency_element_percent": _sc.get("element_coverage_percent"),
                    "consistency_element_missing_count": _sc.get("element_missing_count"),
                    "consistency_element_missing": [e.get("name") for e in (_sc.get("element_missing") or [])],
                    "consistency_fix_rounds": _sc.get("fix_rounds"),
                    "consistency_fixed": _sc.get("fixed"),
                    "consistency_issue_count": _sc.get("issue_count"),
                    "consistency_issue_stats": _sc.get("issue_stats") or {},
                    "consistency_report_path": _sc.get("report_path"),
                    "message": "生成完成",
                })
            except Exception as e:  # noqa: BLE001
                logger.error(f"第{ep}集生成失败: {e}")
                # 以产物为准：模型抖动/校验失败时报错，但剧本可能已经落盘且可用
                salvaged = _salvage_episode_script(out_path, ep)
                if salvaged:
                    logger.warning(
                        f"第{ep}集虽报错但剧本产物可用，已按成功回填：{out_path}")
                    results.append({
                        "episode_no": ep, "chapter_title": ch_title,
                        "status": "success", "degraded": True,
                        "path": out_path, "project_key": key, **salvaged,
                        "message": f"生成过程报错，但剧本已落盘且可用（已按产物回填为成功）：{e}",
                        "error": str(e),
                    })
                else:
                    results.append({"episode_no": ep, "chapter_title": ch_title,
                                    "status": "failed", "path": out_path,
                                    "message": str(e)})

        ok = [r for r in results if r["status"] == "success"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]
        degraded = [r for r in ok if r.get("degraded")]
        episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok or skipped else "failed",
                "progress": 100, "current": total, "total": total,
                "message": (f"批量完成：成功 {len(ok)} 集"
                            + (f"（其中 {len(degraded)} 集过程报错但产物可用）" if degraded else "")
                            + f" / 跳过 {len(skipped)} 集 / 失败 {len(failed)} 集"),
                "degraded_count": len(degraded),
                "error": "" if (ok or skipped) else (failed[0]["message"] if failed else "全部失败"),
                "results": results, "episodes": episodes,
                "novel_id": novel_meta.get("novel_id"),
                "project_key": key,
                "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
            })
    except (LLMError, NovelParseError) as e:
        logger.error(f"分集生成失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        logger.exception("分集生成异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分集生成异常：{e}"})
