#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目文本规则扫描器：从真实剧本里**发现**角色名词表（A2）候选，并列出多音字（L2）候选。

为什么需要它：`app/text_rules.py` 的规则文件默认不存在 = 全链路 fail-open、行为零变化，
但也就意味着「能力就绪、生产不生效」。本工具把「该配什么」从猜变成**有证据的建议**：

  · **别名候选**：镜头里出现、但不在 bible 名单里的名字（`dialogue[].speaker` /
    `characters_in_shot` / `items_in_shot` / `items_state` 键 / `blocking[].name`），
    逐个用与生成链路**同一套**三级匹配（精确→归一化→唯一子串）比对 bible。
      - 命中非精确层 → **高置信**，`--apply` 会写进 rules 的 aliases；
      - 完全未命中 → 只列出来（附「共享姓氏的唯一 bible 名」作为参考），**不自动写**。
  · **多音字候选**：扫台词里出现的常见多音词（重创/给予/档案/角色…），给出现次数与
    例句 —— L2 的替换是**同音改写**（如 重创→崇创），必须人工确认，工具不自动写。

用法：
    python tools/project_rules_scan.py <项目>                  # 只体检（dry-run）
    python tools/project_rules_scan.py <项目> --apply          # 写高置信别名（合并，不覆盖冲突）
    python tools/project_rules_scan.py <项目> --json
    python tools/project_rules_scan.py <项目> --script-dir <剧本目录>   # 指定剧本根（默认 output/scripts）
退出码：0 = 有可写入项或全部一致；1 = 只读模式检出了待确认项（便于 CI/人工 gate）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

#: 常见多音/易读错词（只用于提示，不自动改写）。命中后由人工决定同音写法。
POLYPHONES = (
    "重创", "给予", "档案", "角色", "处理", "模样", "参差", "倔强", "说服", "血泊",
    "地壳", "勉强", "供给", "传记", "还钱", "长大", "行长", "行走", "重要", "差不多",
    "一撮", "薄荷", "湖泊", "颤动", "软骨", "咀嚼", "桎梏", "商贾", "吐蕃", "龟裂",
)


def _iter_episodes(script_dir: str):
    for p in sorted(glob.glob(os.path.join(script_dir, "*.json"))):
        name = os.path.basename(p)
        if name.startswith("_") or name in ("metadata.json", "glossary.json"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (data.get("shots") or data.get("characters")):
            yield p, data


def _collect(scripts):
    """返回 (bible_chars, bible_items, used: {kind: {name: count}}, dialogues: [str])"""
    chars, items, used = set(), set(), {"character": {}, "item": {}}
    dialogues = []

    def _bump(kind, name):
        n = str(name or "").strip()
        if n:
            used[kind][n] = used[kind].get(n, 0) + 1

    for _p, sc in scripts:
        for c in sc.get("characters") or []:
            if isinstance(c, dict) and c.get("name"):
                chars.add(str(c["name"]).strip())
        for i in sc.get("items") or []:
            if isinstance(i, dict) and i.get("name"):
                items.add(str(i["name"]).strip())
        for sh in sc.get("shots") or []:
            if not isinstance(sh, dict):
                continue
            for d in sh.get("dialogue") or []:
                if isinstance(d, dict):
                    _bump("character", d.get("speaker"))
                    if str(d.get("text") or "").strip():
                        dialogues.append(str(d["text"]).strip())
            if str(sh.get("dialogue_text") or "").strip():
                dialogues.append(str(sh["dialogue_text"]).strip())
            for n in sh.get("characters_in_shot") or []:
                _bump("character", n)
            for n in sh.get("items_in_shot") or []:
                _bump("item", n)
            st = sh.get("items_state")
            if isinstance(st, dict):
                for n in st.keys():
                    _bump("item", n)
            for b in sh.get("blocking") or []:
                if isinstance(b, dict):
                    _bump("character", b.get("name"))
    return chars, items, used, dialogues


def _surname_hint(name: str, pool: set) -> list:
    """低置信参考：共享首字且长度接近的 bible 名（仅用于人工判断，不自动写）。"""
    if len(name) < 2:
        return []
    return sorted(n for n in pool if len(n) >= 2 and n[0] == name[0] and n != name)


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描剧本，生成角色名词表（A2）候选 + 多音字（L2）提示")
    ap.add_argument("project", help="项目名（dir_key）")
    ap.add_argument("--script-dir", default="", help="剧本根目录（默认 config.SCRIPT_DIR）")
    ap.add_argument("--apply", action="store_true", help="写入高置信别名（合并，冲突不覆盖）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    import asset_name_match as anm
    import text_rules

    script_dir = args.script_dir
    if not script_dir:
        from config import SCRIPT_DIR as script_dir  # noqa: PLC0415
    project_dir = os.path.join(script_dir, args.project)
    if not os.path.isdir(project_dir):
        print("[error] 剧本目录不存在：%s" % project_dir)
        return 1

    scripts = list(_iter_episodes(project_dir))
    if not scripts:
        print("[error] 该目录下没有可用剧本（%s）" % project_dir)
        return 1

    chars, items, used, dialogues = _collect(scripts)
    report = {"project": args.project, "script_dir": project_dir,
              "episodes": len(scripts), "bible_characters": sorted(chars),
              "bible_items": sorted(items),
              "high_confidence": [], "needs_review": [], "polyphones": []}

    for kind, pool in (("character", chars), ("item", items)):
        for name, cnt in sorted(used[kind].items(), key=lambda kv: -kv[1]):
            if name in pool:
                continue
            k, lv = anm.match(name, sorted(pool), kind)
            if k:
                lv_name = getattr(anm, "LEVEL_" + str(lv).upper(), str(lv)) if isinstance(lv, str) else str(lv)
                report["high_confidence"].append(
                    {"kind": kind, "alias": name, "canonical": k, "level": lv_name, "count": cnt})
            else:
                report["needs_review"].append(
                    {"kind": kind, "name": name, "count": cnt,
                     "surname_hint": _surname_hint(name, pool)})

    _all_text = "\n".join(dialogues)
    for w in POLYPHONES:
        c = _all_text.count(w)
        if c:
            idx = _all_text.find(w)
            report["polyphones"].append(
                {"word": w, "count": c, "example": _all_text[max(0, idx - 12): idx + 14].replace("\n", " ")})

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 68)
        print("项目 %s：%d 集；bible 角色 %d / 物品 %d" % (
            args.project, len(scripts), len(chars), len(items)))
        print("-" * 68)
        if report["high_confidence"]:
            print("✔ 高置信别名（可 --apply 写入）：")
            for r in report["high_confidence"]:
                print("   %s 「%s」→「%s」  出现 %d 次（匹配层级 %s）" % (
                    "角色" if r["kind"] == "character" else "物品",
                    r["alias"], r["canonical"], r["count"], r["level"]))
        else:
            print("✔ 高置信别名：无")
        if report["needs_review"]:
            print("? 需人工确认（未自动写入）：")
            for r in report["needs_review"]:
                hint = ("；同姓候选：" + "、".join(r["surname_hint"])) if r["surname_hint"] else ""
                print("   %s 「%s」出现 %d 次%s" % (
                    "角色" if r["kind"] == "character" else "物品", r["name"], r["count"], hint))
        if report["polyphones"]:
            print("◐ 多音字（L2 需人工决定同音写法，如 重创→崇创）：")
            for r in report["polyphones"]:
                print("   「%s」×%d   例：%s" % (r["word"], r["count"], r["example"]))
        print("=" * 68)

    if not args.apply:
        pending = len(report["high_confidence"]) + len(report["needs_review"])
        return 1 if pending else 0

    # ---- 写入：只写高置信，合并已有；冲突（同一别名指向不同标准名）不覆盖并告警 ----
    cur = text_rules.load(args.project)
    aliases = dict(cur.get("aliases") or {})
    written, conflicts = [], []
    for r in report["high_confidence"]:
        old = aliases.get(r["alias"])
        if old and old != r["canonical"]:
            conflicts.append((r["alias"], old, r["canonical"]))
            continue
        if old == r["canonical"]:
            continue
        aliases[r["alias"]] = r["canonical"]
        written.append((r["alias"], r["canonical"]))
    if written:
        path = text_rules.save(args.project, {"aliases": aliases,
                                              "pronounce": cur.get("pronounce") or {}})
        print("[apply] 写入 %d 条别名 → %s" % (len(written), path))
        for a, c in written:
            print("   %s → %s" % (a, c))
    else:
        print("[apply] 无需写入（已一致或无可写项）")
    if conflicts:
        print("[warn] 以下别名与现有规则冲突，**未覆盖**，请人工确认：")
        for a, old, new in conflicts:
            print("   %s：现有→%s，本次建议→%s" % (a, old, new))
    if report["polyphones"]:
        print("[note] 多音字需要人工确认后写入 pronounce 表（POST /api/projects/<pid>/text-rules 或直接改 JSON）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
