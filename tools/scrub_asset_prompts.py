# -*- coding: utf-8 -*-
"""清理剧本资产提示词里的「参考图不适用段落」（一次性迁移，可重复执行）。

背景（2026-10-20 用户两次实测）：style_guide.json 的 style 把 8 个语义维度挤在
一个字符串里，剧本生成阶段会把完整风格串写进 characters/items/scenes 的
reference_prompt_zh，于是每张资产图的提示词都带着：
  · 情绪基调 / 分镜节奏 —— 成片叙事与剪辑规则，对静态资产图无意义；
  · 规避项 —— 负面清单，混进正向提示词反而更容易画出「霓虹/都市」；
  · 补充要求 —— 角色专属造型（道袍/发冠束发/反派赵天霸为内门弟子华服），
    物品与场景照抄会稀释主体，甚至真把人物画进物品图。

用法：
    python tools/scrub_asset_prompts.py            # dry-run，只报告
    python tools/scrub_asset_prompts.py --apply    # 落盘（自动备份）
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import style_kit  # noqa: E402

_KINDS = (("characters", "character"), ("items", "item"), ("scenes", "scene"))


def _data_dir() -> str:
    d = os.environ.get("MJSCXT_DATA_DIR") or ""
    if not d:
        home = os.path.expanduser("~")
        d = os.path.join(home, "AppData", "Roaming", "mjscxt-desktop", "mjscxt-data")
    return d


def scrub_file(path: str, apply: bool) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    changed = []
    for key, kind in _KINDS:
        for row in (doc.get(key) or []):
            if not isinstance(row, dict):
                continue
            old = str(row.get("reference_prompt_zh") or "")
            if not old:
                continue
            new = style_kit.strip_ref_sections_text(old, kind=kind)
            if new != old:
                changed.append({"kind": kind, "name": row.get("name"),
                                "before": len(old), "after": len(new)})
                row["reference_prompt_zh"] = new
    if changed and apply:
        bak = path + ".bak_scrub_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, bak)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    return {"path": path, "changed": changed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落盘（默认 dry-run）")
    ap.add_argument("--data-dir", default="", help="数据根（默认读 MJSCXT_DATA_DIR）")
    args = ap.parse_args()
    root = args.data_dir or _data_dir()
    scripts = os.path.join(root, "output", "scripts")
    if not os.path.isdir(scripts):
        print("找不到剧本目录：%s" % scripts)
        return 1
    files = []
    for proj in sorted(os.listdir(scripts)):
        pd = os.path.join(scripts, proj)
        if not os.path.isdir(pd):
            continue
        for fn in sorted(os.listdir(pd)):
            if fn.endswith(".json") and not fn.startswith("."):
                files.append(os.path.join(pd, fn))
    print("[%s] 数据根：%s" % ("APPLY" if args.apply else "DRY-RUN", root))
    total = 0
    for path in files:
        try:
            r = scrub_file(path, args.apply)
        except Exception as e:  # noqa: BLE001
            print("  [跳过] %s：%s" % (path, e))
            continue
        if not r["changed"]:
            continue
        print("  %s" % os.path.relpath(path, root))
        for c in r["changed"]:
            print("    %-10s %-10s %4d → %4d 字" % (c["kind"], c["name"], c["before"], c["after"]))
        total += len(r["changed"])
    print("结论：%d 个资产提示词%s" % (total, "已清理" if args.apply else "待清理（加 --apply 落盘）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())