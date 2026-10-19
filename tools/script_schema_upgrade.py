#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧格式剧本 → 当前 schema 的**机械迁移**（不编内容、不改剧情）。

只做「确定能推断」的升级，推断不了的**如实列出**交给人工，绝不猜：
  · `dialogue` 字符串 → `[{speaker,text}]`：
      镜头内**恰好 1 个**出场角色 → 说话人取它；0 个或多个 → 留空并列入「需人工」；
  · 补齐镜头标准字段：`caption / caption_text / items_state / motion / first_frame / last_frame`；
  · `beat` 自由文本 → 四值枚举（开场/触发/高潮/收尾，按关键词映射）；
  · 场景补 `int_ext / time_of_day`（复用 novel_to_script.normalize_scene_meta）；
  · 重算 `production_notes.total_shots / estimated_duration`；
  · `prompt_h3`：裸英文（无 `<Picture N>`）在生成期会被丢弃 → 默认**保留但告警**，
    加 `--drop-legacy-h3` 才删除。

用法：
    python tools/script_schema_upgrade.py <剧本.json>                     # 输出 <名>.upgraded.json
    python tools/script_schema_upgrade.py <剧本.json> --out 新文件.json
    python tools/script_schema_upgrade.py <剧本.json> --in-place          # 原地改（先备份 .bak_<日期>）
    python tools/script_schema_upgrade.py <剧本.json> --drop-legacy-h3
退出码：0 = 已写出（仍可能有需人工项，见输出）；1 = 失败；2 = 无需迁移。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

BEATS = ("开场", "触发", "高潮", "收尾")

#: 非角色台词（系统提示 / 画外音）的识别词：这类文本**不能**归给场上角色，
#: 否则会用人声念出来（如「【叮！逆天系统激活！】」被归给主角）。
_NON_DIEGETIC_HINTS = ("【", "】", "叮", "系统", "提示音", "画外音")


def _map_beat(raw) -> str:
    b = str(raw or "")
    if "开场" in b:
        return "开场"
    if any(k in b for k in ("收尾", "章末", "转场", "悬念", "结尾", "立威")):
        return "收尾"
    if any(k in b for k in ("高潮", "爆发", "爽", "打脸", "觉醒", "反击")):
        return "高潮"
    return "触发"


def _speaker_for(shot: dict) -> tuple:
    """返回 (speaker, reason)。只在**恰好一个**出场角色时确定，否则留空。"""
    cast = [str(x).strip() for x in (shot.get("characters_in_shot") or []) if str(x).strip()]
    if len(cast) == 1:
        return cast[0], ""
    if not cast:
        return "", "本镜没有出场角色（群嘲/画外音需先加一个群演/旁白角色）"
    return "", "本镜有 %d 个出场角色，无法确定谁在说" % len(cast)


def upgrade(script: dict, *, drop_legacy_h3: bool = False) -> tuple:
    """返回 (新剧本, 报告)。不修改入参。"""
    import copy
    out = copy.deepcopy(script)
    rep = {"auto": [], "manual": [], "notes": []}

    # ---- 场景：int_ext / time_of_day ----
    try:
        import novel_to_script as nts
        for sc in out.get("scenes") or []:
            if not isinstance(sc, dict):
                continue
            meta = nts.normalize_scene_meta(sc)
            changed = False
            if not str(sc.get("int_ext") or "").strip():
                sc["int_ext"] = meta[0]
                changed = True
            if not str(sc.get("time_of_day") or "").strip():
                sc["time_of_day"] = meta[1]
                changed = True
            if "surface_text" not in sc:
                sc["surface_text"] = ""
                changed = True
            if changed:
                rep["auto"].append("场景「%s」补 int_ext/time_of_day = %s/%s"
                                   % (sc.get("name"), sc.get("int_ext"), sc.get("time_of_day")))
    except Exception as e:  # noqa: BLE001
        rep["notes"].append("场景元数据推导不可用（%s），未补 int_ext/time_of_day" % e)

    # ---- 镜头 ----
    for sh in out.get("shots") or []:
        if not isinstance(sh, dict):
            continue
        sid = sh.get("shot_id")

        # dialogue
        dlg = sh.get("dialogue")
        if isinstance(dlg, str):
            text = dlg.strip()
            if not text:
                sh["dialogue"] = []
                rep["auto"].append("镜 %s：空字符串 dialogue → []" % sid)
            else:
                # 非角色台词（系统提示/画外音）绝不能按「场上唯一角色」归人声
                if any(h in text for h in _NON_DIEGETIC_HINTS):
                    spk, why = "", ("疑似系统提示/画外音，不能算作角色台词；"
                                    "请改写成 audio_cues 音效或在 bible 里新增专用角色")
                else:
                    spk, why = _speaker_for(sh)
                sh["dialogue"] = [{"speaker": spk, "text": text}]
                if spk:
                    rep["auto"].append("镜 %s：字符串 dialogue → 结构化（speaker=%s）" % (sid, spk))
                else:
                    rep["manual"].append("镜 %s：需补 speaker（%s）" % (sid, why))
        elif not isinstance(dlg, list):
            sh["dialogue"] = []
            rep["manual"].append("镜 %s：dialogue 类型异常（%s）→ 置空" % (sid, type(dlg).__name__))
        else:
            for d in dlg:
                if isinstance(d, dict) and not str(d.get("speaker") or "").strip():
                    rep["manual"].append("镜 %s：台词缺 speaker（%s）" % (sid, str(d.get("text") or "")[:20]))

        # 标准字段
        for k, v in (("caption", {}), ("caption_text", ""), ("items_state", {}),
                     ("motion", ""), ("first_frame", ""), ("last_frame", "")):
            if k not in sh:
                sh[k] = v
                rep["auto"].append("镜 %s：补字段 %s" % (sid, k))

        # beat
        old_beat = sh.get("beat")
        new_beat = _map_beat(old_beat)
        if old_beat != new_beat:
            sh["beat"] = new_beat
            rep["auto"].append("镜 %s：beat「%s」→「%s」" % (sid, old_beat, new_beat))

        # prompt_h3
        h3 = str(sh.get("prompt_h3") or "")
        if h3 and "<Picture" not in h3:
            if drop_legacy_h3:
                sh.pop("prompt_h3", None)
                rep["auto"].append("镜 %s：删除裸英文 prompt_h3（生成期本就会丢弃）" % sid)
            else:
                rep["notes"].append("镜 %s 的 prompt_h3 是裸英文，生成期会被丢弃（可加 --drop-legacy-h3 删除）" % sid)

    # ---- 统计 ----
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    total = 0.0
    for s in shots:
        try:
            total += float(s.get("duration") or 0)
        except (TypeError, ValueError):
            pass
    pn = out.setdefault("production_notes", {})
    if pn.get("total_shots") != len(shots):
        pn["total_shots"] = len(shots)
        rep["auto"].append("production_notes.total_shots → %d" % len(shots))
    if pn.get("estimated_duration") != round(total):
        pn["estimated_duration"] = round(total)
        rep["auto"].append("production_notes.estimated_duration → %d" % round(total))

    rep["manual"] = sorted(set(rep["manual"]))
    rep["notes"] = sorted(set(rep["notes"]))
    return out, rep


def main() -> int:
    ap = argparse.ArgumentParser(description="旧格式剧本 → 当前 schema 的机械迁移")
    ap.add_argument("src", help="输入剧本 JSON")
    ap.add_argument("--out", default="", help="输出路径（默认 <名>.upgraded.json）")
    ap.add_argument("--in-place", action="store_true", help="原地覆盖（先备份 .bak_<时间>）")
    ap.add_argument("--drop-legacy-h3", action="store_true", help="删除裸英文 prompt_h3")
    ap.add_argument("--json", action="store_true", help="输出迁移报告 JSON")
    args = ap.parse_args()

    try:
        with open(args.src, "r", encoding="utf-8") as f:
            script = json.load(f)
    except (OSError, ValueError) as e:
        print("[error] 读取失败：%s" % e)
        return 1
    if not isinstance(script, dict) or not isinstance(script.get("shots"), list):
        print("[error] 不是剧本 JSON（缺 shots 数组）")
        return 1

    out, rep = upgrade(script, drop_legacy_h3=args.drop_legacy_h3)

    # 迁移后体检（复用流水线同一套规则）
    try:
        import script_structure as ss
        chk = ss.check(out)
        rep["check"] = {"ok": chk["ok"],
                        "p0": [{"code": x["code"], "message": x["message"]} for x in chk["p0"]],
                        "p1": [{"code": x["code"], "message": x["message"]} for x in chk["p1"]]}
    except Exception as e:  # noqa: BLE001
        rep["check"] = {"error": str(e)}

    if args.in_place:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(args.src, args.src + ".bak_" + stamp)
        dst = args.src
    else:
        dst = args.out or (os.path.splitext(args.src)[0] + ".upgraded.json")
    try:
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("[error] 写出失败：%s" % e)
        return 1

    if args.json:
        print(json.dumps({"output": dst, **rep}, ensure_ascii=False, indent=2))
        return 0

    print("=" * 68)
    print("已写出：%s" % dst)
    print("自动修复 %d 项；需人工 %d 项" % (len(rep["auto"]), len(rep["manual"])))
    for x in rep["auto"][:20]:
        print("  ✔ %s" % x)
    if len(rep["auto"]) > 20:
        print("  … 另有 %d 项自动修复（--json 可看全量）" % (len(rep["auto"]) - 20))
    for x in rep["manual"][:20]:
        print("  ! %s" % x)
    for x in rep["notes"][:8]:
        print("  · %s" % x)
    chk = rep.get("check") or {}
    if chk.get("error"):
        print("  （迁移后体检不可用：%s）" % chk["error"])
    else:
        print("迁移后体检：%s（P0 %d / P1 %d）" % (
            "通过" if chk.get("ok") else "未通过",
            len(chk.get("p0") or []), len(chk.get("p1") or [])))
        for x in (chk.get("p0") or [])[:6]:
            print("  [P0:%s] %s" % (x["code"], x["message"][:120]))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
