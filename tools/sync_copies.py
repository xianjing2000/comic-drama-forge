#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""代码副本同步器（**写**操作，默认 dry-run）—— 把源 app/*.py 同步到各副本。

为什么需要
----------
`tools/check_app_sync.py` 是**只读**漂移门禁（逐文件 sha1 比对 + 退出码），只
「发现漂移」，不「执行同步」。此前同步靠人工 cp，结果副本实际在漂移
（2026-10-16 实测：`app/pipeline.py` 3602 行 vs 打包副本 3554 行）。

本工具把「执行同步」这一步补上，并遵守 t43 基建审查对本文件提出的四条硬约束：

  · **dry-run 默认**：不加 `--apply` 只打印计划，不动任何文件。
  · **备份**：覆盖前把副本原文件另存为 `<name>.bak_sync_<UTC 时间戳>`；该名字
    含 `.bak_`、且不以 `.py` 结尾，被 check_app_sync 视为快照自动跳过，不污染比对。
  · **防误覆盖**：源目录必须存在且 `.py` 数 ≥ MIN_SOURCE_FILES；只覆盖 diff/missing，
    **从不删除副本里多出来的文件（extra）**——删 extra 需显式 `--prune-extra`
    （且跳过 IGNORED_EXTRA 里已判定的历史残留）。
  · **防并发**：进程级排他锁文件；已有未过期锁则拒绝运行（`--force` 破陈旧锁）。

同步范围 / 目标
--------------
  · 默认只同步「必须同步」目标（`check_app_sync.default_targets()` 里
    allow_stale=False 的三个：打包输入 `electron-app/_backend/app`、安装区
    `resources/backend`（若探测到）、运行时镜像 `resource-mirror/app`）。
  · `--include-packaged` 连「允许陈旧」的打包产物一起刷新（发布/打包时用）。
  · 目标集合与 `check_app_sync.py` **同源**（直接 import 复用），避免两处清单漂移。
  · 同步的是 `.py`（与门禁口径一致）。templates/static 等非 .py 资产不在本不变量内，
    由打包流程负责。

用法::
    python tools/sync_copies.py                       # dry-run（默认，只看计划）
    python tools/sync_copies.py --apply               # 真正执行（先备份再覆盖）
    python tools/sync_copies.py --apply --include-packaged
    python tools/sync_copies.py --target <dir>         # 指定目标（可重复，视为必须同步）
    python tools/sync_copies.py --apply --prune-extra   # 连副本多出来的文件也删（先备份）
    python tools/sync_copies.py --json

同步后请再跑一次只读门禁确认::

    python tools/check_app_sync.py          # 退出码 0 = 四处副本一致

退出码::
    0 = 无待同步项，或 dry-run 完成，或 --apply 全部成功
    1 = 有文件同步失败
    2 = 参数/源目录错误，或并发锁被占用
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 复用只读门禁的扫描 / 目标定义 / 豁免清单（单一事实来源）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_app_sync as cas  # noqa: E402

ROOT = cas.ROOT
#: 源目录至少要有这么多 .py 才敢同步（防「源目录指错 / 被清空」把副本一起抹白）。
MIN_SOURCE_FILES = 20
#: 陈旧锁判定阈值（秒）；超过则认为是崩溃残留，`--force` 可破。
LOCK_STALE_SEC = 3600
LOCK_PATH = ROOT / "tools" / ".sync_copies.lock"


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _acquire_lock(force: bool) -> bool:
    """进程级排他锁（O_EXCL）。返回 True=拿到；False=被占用且未过期。"""
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0
        if age > LOCK_STALE_SEC or force:
            try:
                LOCK_PATH.unlink()
            except OSError:
                return False
            return _acquire_lock(force)
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("pid=%d\nts=%s\n" % (os.getpid(), _now_ts()))
    return True


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def _backup(dst_file: Path, ts: str) -> None:
    """覆盖前把副本原文件另存为 <name>.bak_sync_<ts>（含 .bak_ → 门禁自动跳过）。"""
    if not dst_file.exists():
        return
    bak = dst_file.with_name(dst_file.name + ".bak_sync_" + ts)
    try:
        shutil.copy2(str(dst_file), str(bak))
    except OSError as e:  # noqa: BLE001 —— 备份失败必须中止覆盖（安全优先）
        raise RuntimeError("备份失败，已中止覆盖：%s (%s)" % (bak, e))


def _write_atomic(dst_file: Path, data: bytes) -> None:
    """原子写：先写同目录临时文件再 os.replace，避免半份文件。"""
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_file.with_name(dst_file.name + ".synctmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(str(tmp), str(dst_file))


def _sync_target(src_dir: Path, src: dict, dst_dir: Path, allow_stale: bool,
                 apply: bool, prune_extra: bool, ts: str) -> dict:
    """把 src（相对路径→签名）同步到 dst_dir。返回本目标的处置结果。"""
    res = {"path": str(dst_dir), "allow_stale": allow_stale,
           "copied": [], "pruned": [], "failed": [], "skipped_missing_dir": False}
    if not dst_dir.is_dir():
        res["skipped_missing_dir"] = True
        return res
    dst = cas.scan(dst_dir)
    cmp = cas.compare(src, dst, cas.IGNORED_EXTRA)
    to_copy = sorted(set(cmp["diff"]) | set(cmp["missing"]))
    res["copied"] = to_copy
    res["pruned"] = sorted(cmp["extra"]) if prune_extra else []
    if not apply:
        return res
    for rel in to_copy:
        s = src_dir / Path(rel)
        d = dst_dir / Path(rel)
        try:
            _backup(d, ts)
            _write_atomic(d, s.read_bytes())
        except Exception as e:  # noqa: BLE001
            res["failed"].append("%s: %s" % (rel, e))
    for rel in res["pruned"]:
        d = dst_dir / Path(rel)
        try:
            _backup(d, ts)
            d.unlink()
        except Exception as e:  # noqa: BLE001
            res["failed"].append("prune %s: %s" % (rel, e))
    return res


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="代码副本同步（写操作，默认 dry-run；与 check_app_sync 目标同源）",
        epilog="同步后请跑 python tools/check_app_sync.py 确认退出码 0。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(ROOT / "app"), help="源目录（默认 app/）")
    ap.add_argument("--target", action="append", default=None,
                    help="目标目录，可重复（默认用 check_app_sync 的「必须同步」集合）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只 dry-run）")
    ap.add_argument("--include-packaged", action="store_true",
                    help="连「允许陈旧」的打包产物也刷新（默认否）")
    ap.add_argument("--prune-extra", action="store_true",
                    help="删除副本里多出来、且不在 IGNORED_EXTRA 的文件（默认保留）")
    ap.add_argument("--force", action="store_true", help="破陈旧并发锁")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    src_dir = Path(args.source).expanduser()
    if not src_dir.is_dir():
        print("源目录不存在：%s" % src_dir, file=sys.stderr)
        return 2
    src = cas.scan(src_dir)
    if len(src) < MIN_SOURCE_FILES:
        print("源目录 %s 只有 %d 个 .py（< %d），疑似指错/被清空 —— 拒绝同步。"
              % (src_dir, len(src), MIN_SOURCE_FILES), file=sys.stderr)
        return 2

    if args.target:
        targets = [(Path(t).expanduser(), False) for t in args.target]
    else:
        targets = [(p, a) for p, a in cas.default_targets()
                   if args.include_packaged or not a]

    if not _acquire_lock(args.force):
        print("已有同步在进行（锁 %s）；如确认是崩溃残留可加 --force。" % LOCK_PATH,
              file=sys.stderr)
        return 2

    ts = _now_ts()
    report = {"source": str(src_dir), "source_files": len(src), "apply": args.apply,
              "mode": "APPLY" if args.apply else "DRY-RUN", "targets": []}
    failed = 0
    try:
        for dst_dir, allow_stale in targets:
            res = _sync_target(src_dir, src, dst_dir, allow_stale,
                               args.apply, args.prune_extra, ts)
            report["targets"].append(res)
            failed += len(res["failed"])
    finally:
        _release_lock()

    if args.json:
        report["failed"] = failed
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("[%s] 源：%s（%d 个 .py）" % (report["mode"], src_dir, len(src)))
        for t in report["targets"]:
            if t["skipped_missing_dir"]:
                print("  [跳过] %s（目录不存在）" % t["path"])
                continue
            extra = "" if not t["allow_stale"] else "（允许陈旧）"
            if not t["copied"] and not t["pruned"]:
                print("  [一致]%s %s" % (extra, t["path"]))
                continue
            print("  [%s]%s %s  待覆盖 %d%s"
                  % ("已同步" if args.apply else "将同步", extra, t["path"],
                     len(t["copied"]),
                     ("，将删除 %d" % len(t["pruned"])) if t["pruned"] else ""))
            head = ", ".join(t["copied"][:12]) + ("  ..." if len(t["copied"]) > 12 else "")
            if head:
                print("      → %s" % head)
            for f in t["failed"]:
                print("      ✗ %s" % f)
        if not args.apply:
            print("（dry-run：未改动任何文件；加 --apply 执行）")
        print("结论：%d 个目标，%d 个文件失败" % (len(report["targets"]), failed))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
