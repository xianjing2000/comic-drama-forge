#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""非 .py 源资产副本同步器（**写**操作，默认 dry-run）。

背景（2026-10-16 发现的缺口）：`tools/sync_copies.py` 与只读门禁
`tools/check_app_sync.py` **只覆盖 `app/*.py`**（其 docstring 明确写
「templates/static 等非 .py 资产不在本不变量内」）。但 `app/prompts/*.txt`
是**人工维护的载荷源**（剧本生成 / 质检提示词模板），改它同样必须落到
Electron 打包源、安装区 backend 与运行时资源镜像，否则**运行时读到的还是旧提示词**
—— 症状是「源码改了、日志也说改了，模型行为却没变」，极难排障。

本工具把这一类**非 .py 的源码资产目录**（默认 `prompts`）按 sha1 差异同步到
与 `check_app_sync.default_targets()` **同源**的目标集合，避免两处清单漂移。

⚠️ 只同步**源码资产**目录（`app/prompts` 等）。**不**同步以下（它们各自有归属）：
  · `app/static` —— 前端构建产物（由 `npm run build` 产出，勿手改）；
  · `.py` —— 走 `sync_copies.py`。

对齐后请跑 `python tools/check_app_sync.py`（.py 门禁）确认退出码 0。

用法：
    python tools/sync_assets.py                      # dry-run（默认，只看计划）
    python tools/sync_assets.py --apply              # 真正执行（先备份再覆盖）
    python tools/sync_assets.py --dir prompts --dir catalog   # 指定多个源资产目录
    python tools/sync_assets.py --apply --target <目录>  # 只同步某一个目标根目录
    python tools/sync_assets.py --apply --include-packaged   # 连打包产物一起同步
    python tools/sync_assets.py --json               # 机器可读计划
退出码：0 = 无需变更或已成功同步；1 = dry-run 检出了待同步（调用方可据此 gate）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 复用只读门禁的目标定义与 ROOT（单一事实来源，避免清单漂移）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_app_sync as cas  # noqa: E402

ROOT = cas.ROOT

#: 默认同步的源码资产目录（相对 `app/`）。
DEFAULT_ASSET_DIRS = ("prompts",)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_backup(name: str) -> bool:
    """备份/快照文件（.bak_ / .bak-）——视为快照跳过，不参与比对也不传播
    （与 check_app_sync 对 `.bak_` 的豁免口径一致）。"""
    return ".bak_" in name or ".bak-" in name


def _rel_files(root: Path) -> dict:
    """返回 {相对路径: sha1}。root 不存在 → {}。备份快照文件不计入。"""
    out = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file() and not _is_backup(p.name):
            out[p.relative_to(root).as_posix()] = _sha1(p)
    return out


def _plan_for_dir(asset_dir: str, src_root: Path, targets: list) -> dict:
    src = src_root / asset_dir
    src_files = _rel_files(src)
    plan = {"asset_dir": asset_dir, "source": str(src), "source_files": len(src_files),
            "targets": []}
    for tgt_root, allow_stale in targets:
        tgt = tgt_root / asset_dir
        tgt_files = _rel_files(tgt)
        added = sorted(k for k in src_files if k not in tgt_files)
        updated = sorted(k for k in src_files if k in tgt_files and tgt_files[k] != src_files[k])
        extra = sorted(k for k in tgt_files if k not in src_files)
        plan["targets"].append({
            "path": str(tgt), "allow_stale": allow_stale, "exists": tgt.is_dir(),
            "added": added, "updated": updated, "extra": extra,
            "changed": len(added) + len(updated),
        })
    return plan


def _sync_one(asset_dir: str, src_root: Path, tgt_root: Path, ts: str) -> dict:
    src = src_root / asset_dir
    tgt = tgt_root / asset_dir
    src_files = _rel_files(src)
    res = {"path": str(tgt), "copied": 0, "backups": 0, "skipped": 0}
    for rel in sorted(src_files):
        s = src / rel
        d = tgt / rel
        if d.is_file() and _sha1(d) == src_files[rel]:
            res["skipped"] += 1
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_file():  # 覆盖前备份（改动可回滚）
            try:
                shutil.copy2(d, d.with_name(d.name + f".bak_{ts}"))
                res["backups"] += 1
            except OSError:
                pass
        shutil.copy2(s, d)
        res["copied"] += 1
    return res


def main() -> int:
    ap = argparse.ArgumentParser(
        description="非 .py 源资产（默认 app/prompts）→ 各副本同步（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", action="append", dest="dirs", default=None,
                    help="要同步的源资产目录（相对 app/，可重复；默认为 prompts）")
    ap.add_argument("--apply", action="store_true", help="真正执行（缺省仅 dry-run）")
    ap.add_argument("--include-packaged", action="store_true",
                    help="连「允许陈旧」的打包产物一起同步（默认跳过，同 sync_copies）")
    ap.add_argument("--target", action="append", dest="only_targets", default=None,
                    help="只同步指定目标根目录（可重复；缺省=全部必须同步目标）")
    ap.add_argument("--json", action="store_true", help="输出机器可读计划")
    args = ap.parse_args()

    dirs = tuple(args.dirs) if args.dirs else DEFAULT_ASSET_DIRS
    src_root = ROOT / "app"

    # 防呆：源目录必须存在且非空，否则拒绝（避免指错把副本抹白）。
    valid_dirs = []
    for d in dirs:
        if (src_root / d).is_dir() and any((src_root / d).rglob("*")):
            valid_dirs.append(d)
        else:
            print("[skip] 源资产目录不存在/为空：%s" % (src_root / d))
    if not valid_dirs:
        print("[error] 无有效源资产目录，终止。")
        return 1

    targets = cas.default_targets()
    if args.only_targets:
        wanted = {str(Path(t).resolve()) for t in args.only_targets}
        targets = [(p, s) for (p, s) in targets
                   if str(Path(p).resolve()) in wanted]
        if not targets:
            print("[error] --target 未命中任何已知目标；可用目标：")
            for p, s in cas.default_targets():
                print("  · %s%s" % (p, "（打包产物）" if s else ""))
            return 1
    elif not args.include_packaged:
        # 默认不碰「允许陈旧」的打包产物（与 sync_copies 缺省语义一致）
        targets = [(p, s) for (p, s) in targets if not s]
    plans = [_plan_for_dir(d, src_root, targets) for d in valid_dirs]

    if args.json:
        print(json.dumps({"mode": "apply" if args.apply else "dry-run",
                          "plans": plans}, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("[%s] 源：%s 资产目录：%s" % (
            "APPLY" if args.apply else "dry-run", src_root, "、".join(valid_dirs)))
        for pl in plans:
            print("-" * 72)
            print("  资产目录 %s（源 %d 个文件）" % (pl["asset_dir"], pl["source_files"]))
            for t in pl["targets"]:
                tag = "打包产物(允许陈旧)" if t["allow_stale"] else "必须同步"
                state = "存在" if t["exists"] else "**缺失**"
                print("    [%s][%s] %s" % (tag, state, t["path"]))
                print("        +%d 新增  ~%d 更新  (=%d 相同)  extra=%d" % (
                    len(t["added"]), len(t["updated"]),
                    pl["source_files"] - t["changed"], len(t["extra"])))
                for k in (t["added"] + t["updated"])[:12]:
                    print("          · %s" % k)
        changed_total = sum(t["changed"] for pl in plans for t in pl["targets"])
        print("=" * 72)
        print("待同步变更合计：%d" % changed_total)

    if not args.apply:
        changed_total = sum(t["changed"] for pl in plans for t in pl["targets"])
        return 1 if changed_total else 0

    ts = _now_ts()
    applied = []
    for pl in plans:
        for t in pl["targets"]:
            r = _sync_one(pl["asset_dir"], src_root, Path(t["path"]).parent, ts)
            applied.append(r)
            print("  → %s：复制 %d、备份 %d、跳过 %d" % (
                r["path"], r["copied"], r["backups"], r["skipped"]))
    print("完成（备份后缀 .bak_%s）。请再跑 python tools/check_app_sync.py 核对 .py 门禁。" % ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
