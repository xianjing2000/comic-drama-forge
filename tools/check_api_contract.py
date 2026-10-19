#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端调用 ↔ 后端路由 契约核对（只读，静态分析）。

背景（2026-10-19）：用户实测「按钮没反应」，根因是 6 个 POST 不带 body 被 Flask 3
以 400/415 拒绝，而前端静默 catch。这类问题**编译/类型检查都发现不了**，必须做
「调用点 → 路由」的逐条比对。

本工具做四件事：
  1. 前端路径在后端不存在 → 404（按钮必坏）；
  2. HTTP 方法不匹配 → 405；
  3. 前端 POST/PUT/PATCH **无 body** → Flask 3 下极易 400/415（列出以人工确认）；
  4. 后端路由从未被前端调用（信息项，可能是给 AI/外部用的）。

解析口径：
  · 前端：`request<...>('/path', {...})`、`request('/path')`、直接 `fetch('/api/...')`；
    去掉 query string；`${...}` 统一成 `*`。
  · 后端：`@xxx.route('/api/...', methods=[...])`，`<converter:name>` → `*`，
    `<path:name>` → `**`（可跨段）。
退出码：0 = 无致命问题（404/405）；1 = 存在致命问题。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend" / "src"
BE_DIRS = [ROOT / "app", ROOT / "app" / "routes"]

# ---------------------------------------------------------------- 后端路由

ROUTE_RE = re.compile(
    r"@\w+\.route\(\s*[\"']([^\"']+)[\"']\s*(?:,\s*methods\s*=\s*\[([^\]]*)\])?")
CONV_RE = re.compile(r"<(?:([a-z]+)(?:\([^)]*\))?:)?(\w+)>")


def _norm_be(path: str) -> tuple:
    """后端路径 → 段列表；`<name>`/`<int:name>` → *，`<path:name>` → **。"""
    def repl(m):
        conv = (m.group(1) or "").lower()
        return "**" if conv == "path" else "*"
    p = CONV_RE.sub(repl, path)
    segs = [s for s in p.split("/") if s != ""]
    return tuple(segs)


def parse_backend():
    routes = []
    for d in BE_DIRS:
        for f in d.glob("*.py"):
            if ".bak" in f.name:
                continue
            try:
                src = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in ROUTE_RE.finditer(src):
                path = m.group(1)
                methods = m.group(2)
                if methods:
                    ms = [x.strip().strip("'\"").upper() for x in methods.split(",") if x.strip()]
                else:
                    ms = ["GET"]
                routes.append({"path": path, "segs": _norm_be(path),
                               "methods": set(ms), "file": f.name,
                               "line": src[:m.start()].count("\n") + 1})
    return routes


# ---------------------------------------------------------------- 前端调用

# request<T>('path', { method: 'POST', body: ... }) / request('path')
CALL_RE = re.compile(r"\brequest(?:<[^>]*>)?\(\s*([`'\"])", re.S)
PATH_LIT = re.compile(r"\$\{[^}]*\}")


def _extract_call(src: str, start: int):
    """从 `request(` 的引号起点开始，抽出路径字符串与调用剩余片段。"""
    quote = src[start]
    i = start + 1
    buf = []
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            buf.append(src[i:i + 2]); i += 2; continue
        if quote == "`" and src.startswith("${", i):
            depth += 1; buf.append("${"); i += 2; continue
        if quote == "`" and ch == "}" and depth:
            depth -= 1; buf.append("}"); i += 1; continue
        if ch == quote and depth == 0:
            i += 1
            break
        buf.append(ch); i += 1
    # 取到本次调用结束的 )（粗略：找到下一个 ); 或 ),）
    tail = _call_tail(src, i)
    return "".join(buf), tail


def _call_tail(src: str, pos: int, limit: int = 400) -> str:
    """取本次调用剩余的源码，**截断到调用结束**（避免把下一个调用的 method 误算进来）。"""
    tail = src[pos:pos + limit]
    cut = re.split(r"\)\s*[,;]\s*\n", tail, maxsplit=1)[0]
    return cut


def _norm_fe(path: str) -> tuple:
    """前端路径 → 段列表。`/x/${id}` → *；`/x${query}`（紧跟在字面量后）视为 query，丢弃。"""
    out, i = [], 0
    while i < len(path):
        if path.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(path) and depth:
                if path[j] == "{":
                    depth += 1
                elif path[j] == "}":
                    depth -= 1
                j += 1
            prev = path[i - 1] if i > 0 else ""
            if prev == "/":
                out.append("*")          # 路径段
            # 否则是拼接的 query 串 → 丢弃
            i = j
            continue
        out.append(path[i])
        i += 1
    p = "".join(out).split("?", 1)[0]
    return tuple(s for s in p.split("/") if s != "")


def parse_frontend():
    calls = []
    files = list(FE.rglob("*.ts")) + list(FE.rglob("*.tsx"))
    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
        except OSError:
            continue
        line_of = lambda pos: src[:pos].count("\n") + 1
        for m in CALL_RE.finditer(src):
            try:
                path, tail = _extract_call(src, m.end() - 1)
            except Exception:  # noqa: BLE001
                continue
            if not path.startswith("/"):
                continue
            # request() 的路径是相对 API_BASE='/api' 的；直接 fetch 的已含 /api
            full = path if path.startswith("/api/") else "/api" + path
            method = "GET"
            mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", tail[:200])
            if mm:
                method = mm.group(1).upper()
            has_body = bool(re.search(r"\bbody\s*:", tail[:300]))
            calls.append({"path": full, "segs": _norm_fe(full), "method": method,
                          "has_body": has_body, "file": str(f.relative_to(ROOT)),
                          "line": line_of(m.start())})
        # 直接 fetch('/api/...') —— 用同一套模板串扫描，避免路径里的引号把路径截断
        for m in re.finditer(r"fetch\(\s*([`'\"])/api/", src):
            try:
                path, tail = _extract_call(src, m.start(1))
            except Exception:  # noqa: BLE001
                continue
            if not path.startswith("/api/"):
                continue
            method = "GET"
            mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", tail[:200])
            if mm:
                method = mm.group(1).upper()
            calls.append({"path": path, "segs": _norm_fe(path), "method": method,
                          "has_body": bool(re.search(r"\bbody\s*:", tail[:300])),
                          "file": str(f.relative_to(ROOT)),
                          "line": line_of(m.start()), "direct_fetch": True})
    return calls


def _score(cand_segs: tuple, fe_segs: tuple) -> int:
    """匹配精度：字面段越多越优先；* 次之；** 最低。"""
    s = 0
    for a, b in zip(fe_segs, cand_segs):
        if b == "**":
            s += 0
        elif b == "*":
            s += 1
        else:
            s += 10
    return s


def match(fe_segs: tuple, routes: list, method: str):
    cands = []
    for r in routes:
        bs = r["segs"]
        if len(bs) != len(fe_segs):
            continue
        ok = True
        for a, b in zip(fe_segs, bs):
            if b in ("*", "**"):
                continue
            if a != b:
                ok = False
                break
        if ok:
            cands.append(r)
    if not cands:
        return None
    # 先按「支持该方法」筛，再按路径精度排序 —— 避免 /api/assets/<path:f> 这种
    # 通配 GET 路由把 /api/assets/generate(POST) 误判成「方法不匹配」。
    ok_method = [r for r in cands if method in r["methods"]]
    if ok_method:
        return max(ok_method, key=lambda r: _score(r["segs"], fe_segs))
    return max(cands, key=lambda r: _score(r["segs"], fe_segs))


def main() -> int:
    routes = parse_backend()
    calls = parse_frontend()
    missing, method_bad, no_body = [], [], []
    hit_routes = set()
    for c in calls:
        r = match(c["segs"], routes, c["method"])
        if r is None:
            missing.append(c)
            continue
        hit_routes.add((r["path"], tuple(sorted(r["methods"]))))
        if c["method"] not in r["methods"]:
            method_bad.append((c, r))
        if c["method"] in ("POST", "PUT", "PATCH") and not c["has_body"]:
            no_body.append((c, r))

    print("=" * 78)
    print("前端调用 %d 处 · 后端路由 %d 条" % (len(calls), len(routes)))
    print("-" * 78)
    if missing:
        print("✗ 前端调用后端不存在的路由：%d" % len(missing))
        for c in missing[:30]:
            print("   %s:%s  %s %s" % (c["file"], c["line"], c["method"], c["path"]))
    else:
        print("✓ 所有前端调用都能匹配到后端路由")
    if method_bad:
        print("✗ 方法不匹配：%d" % len(method_bad))
        for c, r in method_bad[:20]:
            print("   %s:%s  %s %s（后端只支持 %s）" % (
                c["file"], c["line"], c["method"], c["path"], ",".join(sorted(r["methods"]))))
    else:
        print("✓ 方法全部匹配")
    if no_body:
        print("! 无 body 的写请求（Flask 3 下易 400/415，需确认后端用 get_json(silent=True)）：%d" % len(no_body))
        for c, r in no_body:
            print("   %s:%s  %s %s → %s" % (c["file"], c["line"], c["method"], c["path"], r["file"]))
    # 后端未被调用（排除明显给外部/AI 的）
    unused = [r for r in routes
              if (r["path"], tuple(sorted(r["methods"]))) not in hit_routes
              and r["methods"] - {"GET"}]
    print("-" * 78)
    print("后端写路由未被前端直接调用（可能给 AI/外部）：%d 条（信息项）" % len(unused))
    for r in unused[:15]:
        print("   %-58s %s:%s" % (r["path"], r["file"], r["line"]))
    print("=" * 78)
    return 1 if (missing or method_bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
