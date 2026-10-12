#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""代码副本漂移检查器（只读，T7 建议①的最小实现；t9 补递归 + 补 ② 号目录）

用途：把「app/*.py 必须同步到 backend + resource-mirror 两处」从人工 cp
变成可验证的不变式：逐文件比对，输出三类差异，退出码非 0 = 不一致。

用法::
    python tools/check_app_sync.py                 # 检查默认副本集合
    python tools/check_app_sync.py --source app    # 指定源目录
    python tools/check_app_sync.py --target <dir>  # 只检查一个目标（可重复）
    python tools/check_app_sync.py --json          # 机器可读输出（CI 用）
    python tools/check_app_sync.py --strict-stale  # 允许陈旧目标不一致也算失败

默认副本集合（不存在则跳过，不报错）：
    electron-app\_backend\app                        （Electron 打包源，【必须同步】）
    C:\mjscxt_p139\resources\backend                  （生产/开发部署镜像，【必须同步】）
    %APPDATA%\mjscxt-desktop\resource-mirror\app       （Electron 运行时镜像，后端实际读这里，【必须同步】）
    electron-app\dist\win-unpacked\resources\backend   （打包产物，【允许陈旧】）
    packaging\_dist\漫剧工坊_便携版\app                  （便携包，【允许陈旧】）

⭐ t9 修的两处覆盖缺口（2026-10-12，此前导致 9 个文件长期漂移未被发现）：
  1. scan() 原来只用 os.listdir(顶层)，**不递归子目录** —— app 下的
     routes/ workers/ providers/ plugins/ 子目录整个不在比对范围内，
     routes/agent.py、routes/autopilot.py、routes/consistency.py 因此
     长期漂移却报「一致」。现在改为 os.walk 递归，键用 **POSIX 相对路径**
     （如 routes/agent.py），子目录结构与文件一并纳入。
  2. default_targets() 原来**不含** electron-app/_backend/app —— 上面那 9 个
     文件所在的目录根本不在监控范围内。现在已加入，且标记为必须同步。

「允许陈旧」的语义（不要误报成失败）：
    dist / 便携版 是**打包产物**，只有在执行打包动作时才刷新；日常改代码
    不重新打包，所以它们必然落后。这类目标单独统计为 stale，默认**不计入
    失败**（退出码仍 0），只在输出里标注 [允许陈旧·落后 N 处]。
    需要把它们当硬门禁时用 --strict-stale。

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
#: ⭐ 已判定「副本特有、不应随 app 分发」的历史残留（t9-C 判定，2026-10-12）。
#:
#: 这些文件在源 app/ 下【不存在】，只存在于三个副本里。它们不是「源丢了」也不是
#: 「没同步」，而是**本来就不属于 app 包** —— 判定依据见每项注释。
#: 登记在这里的目的：让「副本比源多出来的文件」这一信号保持**干净**，
#: 否则一条已知的常量噪音会长期掩盖真正的新漂移（狼来了）。
#: ⚠️ 新增条目必须写明依据与判定日期；**不允许**用它来掩盖「忘记同步」。
IGNORED_EXTRA = {
    # 前端旧构建脚本（v3 整合版）。源 app/templates/ 早在 9a0d01f「删除交付面 4 个死
    # 文件」就被清空；git 5e5a4ae 把它搬去 archive/frontend_legacy_templates_20261012/
    # app_templates/（当前 HEAD 只跟踪 archive 下那份）。三个副本里的这份是打包期遗留，
    # 无任何 app 代码 import（git grep "build_v2" 在 app/ 下 0 命中）。
    # → 结论：**副本特有历史残留**，不随 app 分发；保留不删（t9 不在本任务删除）。
    "templates/build_v2.py",
}
#: 递归时跳过的目录（缓存/版本控制，不是源码）
SKIP_DIRS = frozenset({"__pycache__", ".git", ".ipynb_checkpoints", ".mypy_cache",
                       ".pytest_cache", ".venv", "venv"})
SKIP_SUFFIX = (".pyc", ".pyo", ".pyd")


def sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_snapshot(name: str) -> bool:
    return any(m in name for m in SNAPSHOT_MARKERS)


def scan(d: Path) -> dict:
    """递归扫描 d 下所有 .py，键 = 相对 d 的 POSIX 路径（含子目录）。

    t9 之前只 listdir 顶层，routes/ 等子目录整个漏掉；这里改成 os.walk。
    """
    out = {}
    d = Path(d)
    if not d.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(str(d)):
        dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
        base = Path(dirpath)
        for fn in filenames:
            if not fn.endswith(".py") or fn.endswith(SKIP_SUFFIX):
                continue
            if is_snapshot(fn):
                continue
            p = base / fn
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(d).as_posix()
            except ValueError:  # pragma: no cover - 理论上不会发生
                continue
            try:
                out[rel] = (p.stat().st_size, sha1(p))
            except OSError:
                continue
    return out


def default_targets() -> list:
    """返回 [(path, allow_stale)]；allow_stale=True 的是打包产物。"""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return [
        # —— 必须与源一致（改代码就要同步）——
        (ROOT / "electron-app" / "_backend" / "app", False),
        (Path(r"C:\mjscxt_p139\resources\backend"), False),
        (Path(appdata) / "mjscxt-desktop" / "resource-mirror" / "app", False),
        # —— 打包产物：允许陈旧，落后不算失败 ——
        (ROOT / "electron-app" / "dist" / "win-unpacked" / "resources" / "backend", True),
        (ROOT / "packaging" / "_dist" / "漫剧工坊_便携版" / "app", True),
    ]


def compare(src: dict, dst: dict, ignored_extra=frozenset()) -> dict:
    """比对；ignored_extra 里的键不计入 extra（见 IGNORED_EXTRA 的说明）。

    只豁免 extra（副本多出来的文件），**diff / missing 一律不豁免** ——
    豁免名单绝不能用来掩盖「改了源没同步」。
    """
    diff, missing, extra = [], [], []
    for name, sig in src.items():
        if name not in dst:
            missing.append(name)
        elif dst[name] != sig:
            diff.append(name)
    for name in dst:
        if name not in src and name not in ignored_extra:
            extra.append(name)
    ignored_hit = sorted(n for n in dst if n not in src and n in ignored_extra)
    return {"diff": sorted(diff), "missing": sorted(missing), "extra": sorted(extra),
            "ignored_extra": ignored_hit}


def _fmt(kind: str, names: list) -> str:
    shown = ", ".join(names[:20])
    return "%-8s %s%s" % (kind + ":", shown, "  ..." if len(names) > 20 else "")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="代码副本漂移检查（只读，递归含子目录）")
    ap.add_argument("--source", default=str(ROOT / "app"), help="源目录（默认 app/）")
    ap.add_argument("--target", action="append", default=None,
                    help="目标目录，可重复（默认集合见模块 docstring）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--quiet-ok", action="store_true", help="一致的目标不打印明细")
    ap.add_argument("--strict-stale", action="store_true",
                    help="「允许陈旧」的打包产物若落后也算失败（默认不算）")
    ap.add_argument("--no-ignore-extra", action="store_true",
                    help="连 IGNORED_EXTRA 里已判定的历史残留也报出来（默认豁免）")
    args = ap.parse_args(argv)

    src_dir = Path(args.source).expanduser()
    if not src_dir.is_dir():
        print("源目录不存在：%s" % src_dir)
        return 2
    src = scan(src_dir)
    if args.target:
        # 命令行显式指定的目标一律按「必须同步」处理
        targets = [(Path(t).expanduser(), False) for t in args.target]
    else:
        targets = [(p, a) for p, a in default_targets()]

    report = {"source": str(src_dir), "source_files": len(src), "targets": []}
    bad = 0
    stale_only = 0
    for t, allow_stale in targets:
        if not t.is_dir():
            report["targets"].append({"path": str(t), "skipped": "not found",
                                      "allow_stale": allow_stale})
            continue
        res = compare(src, scan(t),
                      frozenset() if args.no_ignore_extra else IGNORED_EXTRA)
        n_bad = len(res["diff"]) + len(res["missing"]) + len(res["extra"])
        ok = n_bad == 0
        if not ok:
            if allow_stale and not args.strict_stale:
                stale_only += 1
            else:
                bad += 1
        report["targets"].append({"path": str(t), "ok": ok, "allow_stale": allow_stale,
                                  "stale_tolerated": (not ok) and allow_stale
                                  and not args.strict_stale,
                                  "ignored_extra": res.pop("ignored_extra", []), **res})

    if args.json:
        report["failed"] = bad
        report["stale_tolerated"] = stale_only
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("源：%s（递归 %d 个 .py，含子目录）" % (src_dir, len(src)))
        for t in report["targets"]:
            if "skipped" in t:
                print("  [跳过] %s（%s）" % (t["path"], t["skipped"]))
                continue
            if t["ok"]:
                flag = "一致"
            elif t.get("stale_tolerated"):
                flag = "允许陈旧·落后%d处" % (len(t["diff"]) + len(t["missing"]) + len(t["extra"]))
            else:
                flag = "不一致"
            print("  [%s]%s %s  diff=%d missing=%d extra=%d"
                  % (flag, "（允许陈旧）" if t.get("allow_stale") else "",
                     t["path"], len(t["diff"]), len(t["missing"]), len(t["extra"])))
            if t["ok"] and args.quiet_ok:
                continue
            if t.get("stale_tolerated") and args.quiet_ok:
                continue
            for k in ("diff", "missing", "extra"):
                if t[k]:
                    print("      " + _fmt(k, t[k]))
            if t.get("ignored_extra"):
                print("      %-8s %s（已判定副本特有，不随 app 分发）"
                      % ("ignored:", ", ".join(t["ignored_extra"])))
        print("结论：%d 个目标不一致（失败），%d 个打包产物落后（允许陈旧）" % (bad, stale_only))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
