#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片自动验收门（L3）命令行：台词乱码率 + 角色参考图覆盖。

判据：
  · **乱码率** = 1 − 相似度(whisper 转写成片音轨, 剧本台词)，默认阈值 20%
    （env ``MJSCXT_GARBLE_MAX`` 可改）。whisper 环境不全 / 该集无台词 → **未判定**，
    退出码 2（不判缺陷，避免把「没装 whisper」当成「配音烂」）。
  · **角色覆盖** = 应出镜镜头里「生成期就没拿到角色参考图」的镜头数
    （数据源是镜头的 ``_ref_warnings`` / ``_no_reference``），零成本、不调模型。

用法：
    python tools/acceptance_check.py <项目> --episode 3
    python tools/acceptance_check.py <项目> --episode 3 --video D:\\...\\ep03_final.mp4
    python tools/acceptance_check.py <项目> --episode 3 --no-cast      # 只算乱码率
    python tools/acceptance_check.py <项目> --episode 3 --json
退出码：0 = 通过；1 = 不通过（乱码率超阈值 / 角色缺参考图）；2 = 未判定（环境或数据不足）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))


def _doctor(cv, project: str) -> int:
    """环境体检：whisper 三件套 / 文本规则文件 / （可选）成片。"""
    import text_rules
    env = cv.available()
    print("=" * 64)
    print("whisper 三件套：%s" % ("齐全" if env.get("available") else "**不齐（乱码率将判「未判定」）**"))
    print("  exe    : %s" % (env.get("exe") or "<未找到>"))
    print("  model  : %s" % (env.get("model") or "<未找到>"))
    print("  ffmpeg : %s" % (env.get("ffmpeg") or "<未找到>"))
    for r in env.get("reasons") or []:
        print("  - %s" % r)
    if project:
        info = text_rules.describe(project)
        print("文本规则：%s" % info["path"])
        print("  存在=%s；别名 %d 条 / 发音 %d 条" % (
            os.path.isfile(info["path"]), info["alias_count"], info["pronounce_count"]))
        if not os.path.isfile(info["path"]):
            print("  提示：python tools/project_rules_scan.py %s 可扫描剧本给出候选" % project)
    print("=" * 64)
    return 0 if env.get("available") else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="成片自动验收门（乱码率 + 角色参考图覆盖）")
    ap.add_argument("project", nargs="?", default="", help="项目名（dir_key）；--doctor 时可省略")
    ap.add_argument("--episode", "-e", default="", help="集号（1 起）")
    ap.add_argument("--video", default="", help="显式指定成片路径（缺省按超分/成片目录自动定位）")
    ap.add_argument("--timeout", type=int, default=0, help="whisper 转写超时秒数（0=用默认）")
    ap.add_argument("--no-cast", action="store_true", help="跳过角色参考图覆盖核对")
    ap.add_argument("--save", action="store_true",
                    help="把结论写入 output/qc/<项目>/<集>/acceptance.json（与流水线同一落点）")
    ap.add_argument("--doctor", action="store_true",
                    help="只体检环境（whisper 三件套 / 规则文件 / 成片），不判定")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    args = ap.parse_args()

    import caption_verify as cv
    import quality_stage as qs

    if args.doctor:
        return _doctor(cv, args.project)
    if not args.project or not args.episode:
        ap.error("需要 project 与 --episode（或使用 --doctor）")

    video = args.video
    if not video:
        video, src, err = cv.locate_episode_video(args.project, args.episode)
        if not video:
            print("[error] 未找到成片：%s" % err)
            return 2
        if not args.json:
            print("[info] 成片（%s）：%s" % (src, video))

    script, spath = cv.load_episode_script(args.project, args.episode)
    if not isinstance(script, dict):
        print("[error] 未找到该集剧本（%s）" % (spath or "<未定位>"))
        return 2

    speech = qs.speech_garble_verdict(
        video, project=args.project, episode=args.episode,
        timeout=(args.timeout or None))
    cast = ({"skipped": True} if args.no_cast
            else qs.cast_coverage_verdict(script, project=args.project,
                                          episode=args.episode))

    ok_speech = speech.get("ok")
    cast_gap = int(cast.get("shots_missing_cast_ref") or 0) + int(cast.get("shots_no_reference") or 0)
    # 规则是否真的配了（空表 = A2/L2 本轮空跑，报告里必须可见）
    try:
        import text_rules as _trm
        _ri = _trm.describe(args.project)
        _tr_info = {"path": _ri.get("path"), "aliases": _ri.get("alias_count", 0),
                    "pronounce": _ri.get("pronounce_count", 0)}
    except Exception as _e:                             # noqa: BLE001
        _tr_info = {"error": "%s: %s" % (type(_e).__name__, _e)}
    report = {"project": args.project, "episode": args.episode, "video": video,
              "speech": speech, "cast": cast, "text_rules": _tr_info,
              "ok": bool(ok_speech is not False and (args.no_cast or not cast_gap))}

    if args.save:
        _sp = qs.save_acceptance(args.project, args.episode, report)
        print("[save] %s" % (_sp or "<落盘失败>"))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 64)
        _tr = report.get("text_rules") or {}
        if _tr:
            if _tr.get("error"):
                print("文本规则：读取失败（%s）" % _tr["error"])
            else:
                print("文本规则：别名 %s 条 / 发音 %s 条%s" % (
                    _tr.get("aliases"), _tr.get("pronounce"),
                    "" if (_tr.get("aliases") or _tr.get("pronounce"))
                    else "（空表 = A2/L2 未生效）"))
        if speech.get("available"):
            print("乱码率：%.1f%%（阈值 %.0f%%）→ %s" % (
                float(speech.get("garble_rate") or 0) * 100,
                float(speech.get("threshold") or 0) * 100,
                "通过" if ok_speech else "**超过阈值，建议重配**"))
            print("  转写 %s 字 / 剧本 %s 字" % (
                speech.get("transcript_chars"), speech.get("expected_chars")))
        else:
            print("乱码率：未判定 —— %s" % (speech.get("error") or "环境不可用"))
        if args.no_cast:
            print("角色覆盖：已跳过（--no-cast）")
        else:
            print("角色覆盖：应出镜 %s 镜，缺角色参考图 %s 镜，无参考 %s 镜 → %s" % (
                cast.get("shots_with_cast"), cast.get("shots_missing_cast_ref"),
                cast.get("shots_no_reference"), "通过" if not cast_gap else "**有缺口**"))
            if cast_gap:
                ids = (cast.get("missing_cast_ref_shots") or []) + (cast.get("no_reference_shots") or [])
                print("  镜号：%s" % "、".join(str(x) for x in ids[:20]))
        print("=" * 64)
        if report["ok"] and ok_speech is None:
            print("总结论：通过（台词乱码率**未判定** —— 装好 whisper 后复跑才有这一项）")
        else:
            print("总结论：%s" % ("通过" if report["ok"] else "不通过"))

    if ok_speech is None and (args.no_cast or not cast_gap):
        return 2                      # 未判定（不判缺陷）
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
