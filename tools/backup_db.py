#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""tasks.db 备份与轮转（T23 #7，2026-10-16）。

背景：T23 实测**生产 tasks.db 没有任何备份**（无 .bak、无定时快照）—— 单点损坏即丢
全部任务台账 / AI 凭证 / 应用设置。本工具补上：把候选数据根下的 tasks.db 各复制一份
带时间戳的备份，并按保留数轮转。**默认 dry-run**，加 --apply 才真写。

候选（存在才处理）：
  · 工作区      <项目>/output/tasks.db
  · 生产数据根   %APPDATA%\mjscxt-desktop\mjscxt-data\output\tasks.db
  · 旧数据根     %APPDATA%\mjscxt-desktop\output\tasks.db

用法::
    python tools/backup_db.py                  # 预览（不写）
    python tools/backup_db.py --apply          # 真备份（默认每库保留 10 份）
    python tools/backup_db.py --db <path> --keep 5 --apply

建议做成计划任务（本机每天一次）：`schtasks /create /tn MJSCXT_DBBackup /sc daily /st 03:00 ^
  /tr "python <项目>\tools\backup_db.py --apply"`。备份是 SQLite 单文件，冷拷贝安全
（WAL 下建议在无长写事务时跑；本工具只 copy 主库文件，不动 -wal/-shm）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BAK_MARK = ".bak_bk_"


def candidates() -> list:
    out = [_ROOT / "output" / "tasks.db"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "mjscxt-desktop" / "mjscxt-data" / "output" / "tasks.db")
        out.append(Path(appdata) / "mjscxt-desktop" / "output" / "tasks.db")
    return [p for p in out if p.is_file()]


def _existing_backups(db: Path) -> list:
    try:
        return sorted((p for p in db.parent.iterdir() if p.name.startswith(db.name + _BAK_MARK)),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def backup_one(db: Path, keep: int, apply: bool) -> dict:
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = db.parent / ("%s%s%s" % (db.name, _BAK_MARK, ts))
    res = {"db": str(db), "size_mb": round(db.stat().st_size / 1048576, 3),
           "dst": str(dst), "rotated": [], "copied": False}
    if apply:
        shutil.copy2(db, dst)
        res["copied"] = True
    # 轮转：保留最近 keep 份（apply 后含本次新建的）
    listed = _existing_backups(db) if apply else ([dst] + _existing_backups(db))
    if keep > 0:
        res["rotated"] = [str(p) for p in listed[keep:]]
        if apply:
            for p in listed[keep:]:
                try:
                    p.unlink()
                except OSError:
                    pass
    return res


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="tasks.db 备份与轮转（默认 dry-run）")
    ap.add_argument("--db", action="append", default=None, help="指定库文件（可重复；默认按候选）")
    ap.add_argument("--keep", type=int, default=10, help="每库保留最近几份（默认 10；0=不轮转）")
    ap.add_argument("--apply", action="store_true", help="真正备份（默认只预览）")
    args = ap.parse_args(argv)

    dbs = [Path(p) for p in args.db] if args.db else candidates()
    dbs = [p for p in dbs if p.is_file()]
    if not dbs:
        print("未找到任何 tasks.db（候选：工作区 output/、%APPDATA%\\mjscxt-desktop\\...）")
        return 0

    for db in dbs:
        r = backup_one(db, args.keep, args.apply)
        mark = "已备份" if r["copied"] else "将备份"
        print("[%s] %s（%.3f MB）-> %s" % (mark, r["db"], r["size_mb"], r["dst"]))
        for old in r["rotated"]:
            print("    轮转删除：%s" % old)
    if not args.apply:
        print("（dry-run，未写任何文件；加 --apply 生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
