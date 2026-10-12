#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""部署门禁（t15，2026-10-12）：把「桌面版启动 / 资源镜像播种 / 副本同步」三件事一键检查。

为什么需要
----------
本次会话多次出现**只在实际启动桌面版时才暴露**的部署态问题：
  · 工作区改了 app/*.py，但三处副本没同步 → 打包/桌面版跑的是老代码（t3 实录）；
  · 重新打包后安装区变了，但可写镜像没重播种 → 用户看不到新功能（10-07 实录）；
  · 镜像被工作区同步进来后，被安装区旧代码静默覆盖 → 生产代码回退（t7 审计）。
它们都不体现在单元测试里，所以固化成可执行门禁：退出码直接进 CI / 交付前清单。

门禁项
------
  1. main-js-syntax   node --check electron-app/main.js
                      启动脚本语法 —— 坏了桌面版直接打不开（GUI 子系统无控制台，表现是闪退）。
  2. mirror-policy    node electron-app/main.js --mirror-selftest
                      镜像策略自检 13 项：指纹覆盖面（旧实现漏掉的 120+ 文件）、
                      播种方向决策（「镜像比安装区新一律不覆盖」等）。
  3. app-sync         python tools/check_app_sync.py
                      app/*.py 是否已同步到 electron-app/_backend/app、安装区 backend、
                      运行时镜像 app（三处「必须同步」副本）。

用法
----
    python tools/deploy_gates.py               # 跑全部门禁
    python tools/deploy_gates.py --json        # 机器可读（CI 用）
    python tools/deploy_gates.py --list        # 只列门禁项
    python tools/deploy_gates.py --only app-sync
    python tools/deploy_gates.py --strict-stale   # 打包产物陈旧也算失败（透传 check_app_sync）

退出码
------
    0 = 全部门禁通过
    1 = 至少一项门禁失败（例如副本漂移 / 镜像策略自检不过）
    2 = 环境或参数错误（node 不在 PATH、工作目录不对、--only 名字不存在）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_JS = ROOT / "electron-app" / "main.js"
CHECK_SYNC = ROOT / "tools" / "check_app_sync.py"


def _run(argv: list, timeout: int = 600) -> dict:
    """跑一条命令，返回 {code, out, err, seconds}。命令不存在 → code=None。"""
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return {"code": p.returncode, "out": (p.stdout or "").strip(),
                "err": (p.stderr or "").strip(), "seconds": round(time.time() - t0, 2)}
    except FileNotFoundError:
        return {"code": None, "out": "", "err": "%s 不存在或不在 PATH" % argv[0],
                "seconds": round(time.time() - t0, 2)}
    except subprocess.TimeoutExpired:
        return {"code": None, "out": "", "err": "超时（%ds）" % timeout,
                "seconds": round(time.time() - t0, 2)}


def gate_main_js_syntax(_opts: dict) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ok": False, "env_error": True, "detail": "找不到 node（PATH 缺失）"}
    r = _run([node, "--check", str(MAIN_JS)])
    return {"ok": r["code"] == 0, "exit": r["code"], "seconds": r["seconds"],
            "detail": (r["out"] or r["err"] or "语法 OK")[-800:]}


def gate_mirror_policy(_opts: dict) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ok": False, "env_error": True, "detail": "找不到 node（PATH 缺失）"}
    r = _run([node, str(MAIN_JS), "--mirror-selftest"])
    return {"ok": r["code"] == 0, "exit": r["code"], "seconds": r["seconds"],
            "detail": (r["out"] or r["err"])[-2000:]}


def gate_app_sync(opts: dict) -> dict:
    argv = [sys.executable, str(CHECK_SYNC)]
    if opts.get("strict_stale"):
        argv.append("--strict-stale")
    r = _run(argv)
    if r["code"] == 2:
        return {"ok": False, "env_error": True, "exit": 2, "seconds": r["seconds"],
                "detail": (r["out"] or r["err"])[-800:]}
    return {"ok": r["code"] == 0, "exit": r["code"], "seconds": r["seconds"],
            "detail": (r["out"] or r["err"])[-4000:]}


#: 必须与仓库源一致的 main.js 副本（Electron 运行时的启动入口）。
#: 为什么单列一条门禁：check_app_sync.py 只比对 app/*.py，**不含 main.js** ——
#: 而「改了 main.js 但安装区那份没同步」会让桌面版继续跑旧启动逻辑（t15 修的就是
#: 这一类「改了不生效」）。打包产物（dist / 便携版）允许陈旧，只提示不判失败。
MAIN_JS_STRICT_COPIES = [
    Path("C:/mjscxt_p139/resources/app/main.js"),
]
MAIN_JS_STALE_COPIES = [
    ROOT / "electron-app" / "dist" / "win-unpacked" / "resources" / "app" / "main.js",
]


def _sha1(p: Path) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def gate_main_js_copies(_opts: dict) -> dict:
    """main.js 的安装区副本必须与仓库源一致（不一致 = 桌面版会跑旧启动逻辑）。"""
    if not MAIN_JS.is_file():
        return {"ok": False, "env_error": True, "detail": "找不到 %s" % MAIN_JS}
    src = _sha1(MAIN_JS)
    lines = ["源 %s  sha1=%s" % (MAIN_JS, src)]
    bad = []
    for p in MAIN_JS_STRICT_COPIES:
        if not p.is_file():
            lines.append("  [跳过] 不存在：%s" % p)
            continue
        got = _sha1(p)
        same = got == src
        lines.append("  [%s] %s  sha1=%s" % ("一致" if same else "不一致", p, got))
        if not same:
            bad.append(str(p))
    for p in MAIN_JS_STALE_COPIES:
        if p.is_file():
            lines.append("  [允许陈旧] %s  sha1=%s" % (p, _sha1(p)))
    if bad:
        lines.append("请同步（先备份再覆盖）：%s" % ", ".join(bad))
    return {"ok": not bad, "exit": 0 if not bad else 1, "seconds": 0,
            "detail": "\n".join(lines)}


GATES = [
    ("main-js-syntax", "node --check electron-app/main.js（桌面版启动脚本语法）", gate_main_js_syntax),
    ("main-js-copies", "electron-app/main.js 与安装区启动入口是否一致（避免改了不生效）", gate_main_js_copies),
    ("mirror-policy", "node electron-app/main.js --mirror-selftest（镜像指纹/播种策略自检）", gate_mirror_policy),
    ("app-sync", "python tools/check_app_sync.py（app/*.py 三处副本是否同步）", gate_app_sync),
]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        description="部署门禁：桌面版启动脚本语法 + 镜像播种策略 + 副本同步",
        epilog=("退出码：0=全部通过；1=至少一项失败；2=环境/参数错误。"
                "单项门禁也可单独跑（见各命令的 --help）。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="机器可读输出（CI）")
    ap.add_argument("--only", action="append", default=None,
                    help="只跑指定门禁（可重复）：%s" % ", ".join(g[0] for g in GATES))
    ap.add_argument("--list", action="store_true", help="列出全部门禁项后退出")
    ap.add_argument("--strict-stale", action="store_true",
                    help="透传给 check_app_sync：打包产物陈旧也算失败")
    args = ap.parse_args(argv)

    if args.list:
        for name, desc, _fn in GATES:
            print("%-16s %s" % (name, desc))
        return 0

    if args.only:
        unknown = [n for n in args.only if n not in [g[0] for g in GATES]]
        if unknown:
            sys.stderr.write("未知门禁：%s（可用：%s）\n"
                             % (", ".join(unknown), ", ".join(g[0] for g in GATES)))
            return 2
        chosen = [g for g in GATES if g[0] in args.only]
    else:
        chosen = list(GATES)

    if not MAIN_JS.is_file():
        sys.stderr.write("找不到 %s（请在仓库根目录运行）\n" % MAIN_JS)
        return 2

    results = []
    for name, desc, fn in chosen:
        res = fn({"strict_stale": args.strict_stale})
        res.update({"name": name, "desc": desc})
        results.append(res)
        if not args.json:
            mark = "PASS" if res.get("ok") else ("ENV " if res.get("env_error") else "FAIL")
            print("[%s] %-16s %5.2fs  %s" % (mark, name, res.get("seconds") or 0, desc))
            if not res.get("ok"):
                body = res.get("detail") or ""
                for line in body.splitlines()[-12:]:
                    print("        " + line)

    failed = [r for r in results if not r.get("ok")]
    env_err = [r for r in failed if r.get("env_error")]
    summary = {
        "root": str(ROOT),
        "passed": len(results) - len(failed),
        "total": len(results),
        "gates": [{"name": r["name"], "ok": bool(r.get("ok")), "exit": r.get("exit"),
                   "seconds": r.get("seconds"), "env_error": bool(r.get("env_error")),
                   "detail": (r.get("detail") or "")[-2000:]} for r in results],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("门禁结果：%d/%d 通过%s"
              % (summary["passed"], summary["total"],
                 "（失败：%s）" % ", ".join(r["name"] for r in failed) if failed else ""))

    if env_err:
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
