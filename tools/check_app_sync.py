#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""代码副本漂移检查器（只读，T7 建议①的最小实现）

用途：把「app/*.py 必须同步到 backend + resource-mirror 两处」从人工 cp
变成可验证的不变式：逐文件比对，输出三类差异，退出码非 0 = 不一致。

用法::
    python tools/check_app_sync.py                 # 检查默认副本集合
    python tools/check_app_sync.py --source app    # 指定源目录
    python tools/check_app_sync.py --target <dir>  # 只检查一个目标（可重复）
    python tools/check_app_sync.py --json          # 机器可读输出（CI 用）

默认副本集合（不存在则跳过，不报错）：
    C:\mjscxt_p139\resources\backend                  （生产/开发部署镜像）
    %APPDATA%\mjscxt-desktop\resource-mirror\app       （Electron 运行时镜像，后端实际读这里）
    electron-app\dist\win-unpacked\resources\backend   （打包产物，允许陈旧）
    packaging\_dist\漫剧工坊_便携版\app                  （便携包，允许陈旧）

⚠️ 只读：本工具不复制、不删除任何文件。同步动作请用 tools/sync_copies.py（未实现时仍走人工）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_MARKERS = (".bak_", ".bak.", ".bak-", ".rollback_")


def sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_snapshot(name: str) -> bool:
    return any(m in name for m in SNAPSHOT_MARKERS)


def scan(d: Path) -> dict:
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(os.listdir(d)):
        if not f.endswith(".py") or is_snapshot(f):
            continue
        p = d / f
        if p.is_file():
            out[f] = (p.stat().st_size, sha1(p))
    return out


def default_targets() -> list:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return [
        Path(r"C:\mjscxt_p139\resources\backend"),
        Path(appdata) / "mjscxt-desktop" / "resource-mirror" / "app",
        ROOT / "electron-app" / "dist" / "win-unpacked" / "resources" / "backend",
        ROOT / "packaging" / "_dist" / "漫剧工坊_便携版" / "app",
    ]


def compare(src: dict, dst: dict) -> dict:
    diff, missing, extra = [], [], []
    for name, sig in src.items():
        if name not in dst:
            missing.append(name)
        elif dst[name] != sig:
            diff.append(name)
    for name in dst:
        if name not in src:
            extra.append(name)
    return {"diff": sorted(diff), "missing": sorted(missing), "extra": sorted(extra)}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="代码副本漂移检查（只读）")
    ap.add_argument("--source", default=str(ROOT / "app"), help="源目录（默认 app/）")
    ap.add_argument("--target", action="append", default=None, help="目标目录，可重复")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--quiet-ok", action="store_true", help="一致的目标不打印明细")
    args = ap.parse_args(argv)

    src_dir = Path(args.source).expanduser()
    if not src_dir.is_dir():
        print("源目录不存在：%s" % src_dir)
        return 2
    src = scan(src_dir)
    targets = [Path(t).expanduser() for t in (args.target or default_targets())]

    report = {"source": str(src_dir), "source_files": len(src), "targets": []}
    bad = 0
    for t in targets:
        if not t.is_dir():
            report["targets"].append({"path": str(t), "skipped": "not found"})
            continue
        res = compare(src, scan(t))
        ok = not (res["diff"] or res["missing"] or res["extra"])
        if not ok:
            bad += 1
        report["targets"].append({"path": str(t), "ok": ok, **res})

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("源：%s（%d 个 .py）" % (src_dir, len(src)))
        for t in report["targets"]:
            if "skipped" in t:
                print("  [跳过] %s（%s）" % (t["path"], t["skipped"]))
                continue
            flag = "一致" if t["ok"] else "不一致"
            print("  [%s] %s  diff=%d missing=%d extra=%d"
                  % (flag, t["path"], len(t["diff"]), len(t["missing"]), len(t["extra"])))
            if t["ok"] and args.quiet_ok:
                continue
            for k in ("diff", "missing", "extra"):
                if t[k]:
                    print("      %-8s %s" % (k + ":", ", ".join(t[k][:20])
                                             + ("  ..." if len(t[k]) > 20 else "")))
        print("结论：%d/%d 个目标与源不一致" % (bad, len(report["targets"])))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
