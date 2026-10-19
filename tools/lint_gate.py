#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""静态检查门禁：把「未定义名 / 语法错误」这类可静态检出的缺陷纳入提交前 / 发布前检查。

为什么需要
----------
2026-10-16 核验时，一处 `continuity.py` 的已知 `NameError`（函数内裸用某阈值别名，
在裸名首次被触达的那一镜才炸）正在杀生产 —— 这类缺陷**零收益、全损耗**，
而且**完全能被静态检出**。本门禁把它固化成一条命令。

检查内容与依赖策略
------------------
  · **语法检查**（始终执行）：对目标目录下所有 .py 做内存编译，
    抓 SyntaxError / IndentationError（历史教训：改多行断言少一行会留下孤立参数行）。
  · **未定义名**（有 linter 才执行）：优先 `pyflakes`（`python -m pyflakes`），
    退而求其次 `ruff check --select F821`。抓 `undefined name`。
  · 两者都没有 → 只做语法检查，并明确提示安装 pyflakes 才能启用完整检查。

用法::
    python tools/lint_gate.py                 # 检查默认目录（app/）
    python tools/lint_gate.py app tools       # 指定目录（可重复）
    python tools/lint_gate.py --all           # 列出全部 pyflakes 告警（不只 undefined name）
    python tools/lint_gate.py --json

启用完整检查（推荐，写入开发环境）::

    pip install pyflakes

退出码::
    0 = 无问题（语法 + 未定义名均干净）
    1 = 检出问题（语法错误，或未定义名）
    2 = 环境未就绪：无 pyflakes/ruff，仅做了语法检查（未定义名一类**未**覆盖）

接入门禁::
    python tools/deploy_gates.py --only static-lint
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = ["app"]
SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules",
                       "site-packages", "dist", "build", ".mypy_cache", ".pytest_cache"})
UNDEFINED_MARK = "undefined name"


def _iter_py(dirs: list) -> list:
    out = []
    for d in dirs:
        base = Path(d) if os.path.isabs(d) else (ROOT / d)
        if base.is_file() and base.suffix == ".py":
            out.append(base)
            continue
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            out.append(p)
    return sorted(set(out))


def _syntax_check(files: list) -> list:
    """内存编译每个文件，返回 [(path, message)]（不写任何 .pyc）。"""
    problems = []
    for p in files:
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
            compile(src, str(p), "exec")
        except SyntaxError as e:
            problems.append((str(p), "%s (line %s)" % (e.msg, e.lineno)))
        except OSError as e:
            problems.append((str(p), str(e)))
    return problems


def _linter() -> str:
    if importlib.util.find_spec("pyflakes") is not None:
        return "pyflakes"
    if shutil.which("ruff"):
        return "ruff"
    return ""


def _run_undefined(which: str, dirs: list) -> list:
    """返回 linter 的原始输出行。"""
    if which == "pyflakes":
        argv = [sys.executable, "-m", "pyflakes"] + [str(ROOT / d) for d in dirs]
    else:  # ruff
        argv = ["ruff", "check", "--select", "F821", "--no-cache",
                "--output-format", "concise"] + [str(ROOT / d) for d in dirs]
    r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return [ln for ln in ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines() if ln.strip()]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="静态检查门禁：语法错误 + 未定义名（pyflakes/ruff）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="*", default=None, help="要检查的目录（默认 app/）")
    ap.add_argument("--all", action="store_true",
                    help="列出全部 linter 告警（默认只看 undefined name）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    dirs = args.dirs or DEFAULT_DIRS
    files = _iter_py(dirs)
    if not files:
        print("未找到任何 .py（目录：%s）" % ", ".join(dirs), file=sys.stderr)
        return 2

    syntax = _syntax_check(files)
    which = _linter()
    undef_lines = []
    if which:
        raw = _run_undefined(which, dirs)
        if which == "pyflakes":
            undef_lines = raw if args.all else [ln for ln in raw if UNDEFINED_MARK in ln]
        else:  # ruff --select F821 输出即 undefined name
            undef_lines = raw

    report = {
        "dirs": dirs, "files": len(files), "linter": which or None,
        "syntax_errors": [{"file": f, "error": m} for f, m in syntax],
        "undefined_name": undef_lines,
        "scope": "syntax+undefined" if which else "syntax-only",
    }
    hard_fail = bool(syntax) or bool(undef_lines)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("静态检查：%d 个 .py（%s）｜linter=%s"
              % (len(files), ", ".join(dirs), which or "无"))
        if not which:
            print("  ⚠️ 未找到 pyflakes/ruff —— 仅做了语法检查，**未定义名一类未覆盖**。")
            print("     启用完整检查：pip install pyflakes")
        if syntax:
            print("  ✗ 语法错误 %d 处：" % len(syntax))
            for f, m in syntax[:40]:
                print("      %s：%s" % (f, m))
        if undef_lines:
            print("  ✗ 未定义名 %d 处：" % len(undef_lines))
            for ln in undef_lines[:40]:
                print("      %s" % ln)
        if not hard_fail:
            print("  ✓ 通过")

    if hard_fail:
        return 1
    return 0 if which else 2


if __name__ == "__main__":
    raise SystemExit(main())
