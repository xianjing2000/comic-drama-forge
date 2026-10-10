#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""清理运行时镜像里的「代码回溯快照」残留。

背景（2026-10-10）：
  开发时在改代码前习惯性复制一份 <文件>.bak_<主题>_<日期> /
  <文件>.rollback_bpNN_<日期>，这些快照**不是代码**，但会被一起同步进
  Electron 的运行时镜像 %APPDATA%\mjscxt-desktop\resource-mirror\app，
  越攒越多。electron-app/pack_backend.js 打包时已经用 SNAPSHOT_RE 过滤，
  但镜像目录本身没人管，于是历史上混入过 app.py.rollback_bp16/bp17、
  comfyui_client.py.bak_heartbeat 等。

本工具把这一步补上：按「基础文件名」分组，每组保留最近 N 个、其余删除；
另外可设一个绝对天数上限。默认 dry-run，只有显式 --apply 才真删。

用法::

    python tools/prune_mirror_backups.py                  # 预览（默认镜像目录）
    python tools/prune_mirror_backups.py --apply          # 真删
    python tools/prune_mirror_backups.py --dir <path> --keep 1 --days 30 --apply
    python tools/prune_mirror_backups.py --check          # 有残留则退出码 1（给巡检用）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# <基础名>.bak_<主题>... / <基础名>.rollback_bpNN_...
SNAPSHOT_RE = re.compile(r"^(?P<base>.+?)\.(?:bak_|rollback_)(?P<tag>.+)$", re.IGNORECASE)


def default_mirror_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "mjscxt-desktop" / "resource-mirror" / "app"


def parse_snapshot_name(name: str):
    """返回 (base, tag)；不是快照则返回 None。"""
    m = SNAPSHOT_RE.match(name)
    if not m:
        return None
    return m.group("base"), m.group("tag")


def collect_snapshots(root: Path):
    """递归收集快照文件，返回 [(path, base, tag, mtime)]。"""
    found = []
    if not root.is_dir():
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            parsed = parse_snapshot_name(name)
            if not parsed:
                continue
            p = Path(dirpath) / name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            found.append((p, parsed[0], parsed[1], mtime))
    return found


def plan_deletions(entries, keep: int = 3, days: int = 0, now=None):
    """决定删哪些：每组保留最新 keep 个；mtime 早于 days 天的一律删。

    days <= 0 表示不做天数限制。
    """
    now = time.time() if now is None else now
    cutoff = now - days * 86400 if days and days > 0 else None
    by_base = {}
    for item in entries:
        by_base.setdefault(item[1], []).append(item)

    keep_set, delete = set(), []
    for base, group in by_base.items():
        group = sorted(group, key=lambda x: x[3], reverse=True)
        for idx, item in enumerate(group):
            if idx < keep:
                keep_set.add(item[0])
            else:
                delete.append((item, "超出每组保留数 %d" % keep))
    if cutoff is not None:
        kept_ids = {x[0] for x in delete}
        for item in entries:
            if item[0] in keep_set and item[3] < cutoff and item[0] not in kept_ids:
                delete.append((item, "早于 %d 天前" % days))
    return delete


def main(argv=None):
    ap = argparse.ArgumentParser(description="清理运行时镜像里的代码回溯快照（默认 dry-run）")
    ap.add_argument("--dir", default=None, help="镜像目录（默认 %APPDATA%\\mjscxt-desktop\\resource-mirror\\app）")
    ap.add_argument("--keep", type=int, default=3, help="每个基础文件保留最近几个快照（默认 3）")
    ap.add_argument("--days", type=int, default=0, help="超过该天数的快照一律删（0=不限）")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    ap.add_argument("--check", action="store_true", help="只报告是否有残留；有则退出码 1")
    args = ap.parse_args(argv)

    # Windows 控制台默认 GBK，中文 + 全角符号会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    root = Path(args.dir).expanduser() if args.dir else default_mirror_dir()
    print("镜像目录：%s" % root)

    entries = collect_snapshots(root)
    if not entries:
        print("未发现 .bak_* / .rollback_* 快照文件 —— 干净。")
        return 0

    total_bytes = sum(p.stat().st_size for p, _b, _t, _m in entries if p.exists())
    print("发现快照文件 %d 个，合计 %.2f MB" % (len(entries), total_bytes / 1048576))

    groups = {}
    for p, base, tag, mtime in entries:
        groups.setdefault(base, []).append((p, tag, mtime))
    for base in sorted(groups):
        print("  %s：%d 个" % (base, len(groups[base])))

    if args.check:
        print("巡检结论：存在 %d 个快照残留，建议执行 --apply 清理。" % len(entries))
        return 1

    delete = plan_deletions(entries, keep=args.keep, days=args.days)
    if not delete:
        print("按当前策略无需删除（keep=%d, days=%d）。" % (args.keep, args.days))
        return 0

    freed = 0
    for item, reason in delete:
        p, base, tag, mtime = item
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        freed += size
        print("  [%s] %s  (%.1f KB)" % (reason, p, size / 1024))

    print("待删除 %d 个，可释放 %.2f MB" % (len(delete), freed / 1048576))
    if not args.apply:
        print("（dry-run，未删除任何文件；加 --apply 生效）")
        return 0

    removed = 0
    for item, _reason in delete:
        p = item[0]
        try:
            p.unlink()
            removed += 1
        except OSError as exc:
            print("  删除失败：%s（%s）" % (p, exc))
    print("已删除 %d 个，释放 %.2f MB" % (removed, freed / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
