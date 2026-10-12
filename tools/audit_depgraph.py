#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""依赖图体检器（只读）：app/ 内 import 关系 → 循环依赖 / 分层倒置 / 上帝模块。

用法::
    python tools/audit_depgraph.py                 # 摘要
    python tools/audit_depgraph.py --cycles        # 只列循环依赖（含行号）
    python tools/audit_depgraph.py --inversion     # 只列分层倒置（core -> routes.*）
    python tools/audit_depgraph.py --god           # 只列 fan-in / fan-out 排行
    python tools/audit_depgraph.py --json          # 机器可读

判据（与 T12 设计审计一致）：
  · 循环依赖 = 大小 >1 的强连通分量（Tarjan）
  · 分层倒置 = 非 routes/workers 的模块 import routes.*
  · 上帝模块 = fan-out 排行（import 了过多模块）
只读：不写任何文件（--json 也只打到 stdout）。
"""
from __future__ import annotations

import argparse
import ast
import collections
import io
import json
import os
import sys

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP = os.path.join(os.path.dirname(SELF_DIR), "app")


def _mod_name(path: str, root: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    return rel[:-3].replace("/", ".")


def scan_modules(root: str) -> dict:
    out = {}
    for r, d, fs in os.walk(root):
        d[:] = [x for x in d if x not in ("__pycache__", "static", "templates", "prompts")]
        for f in fs:
            if f.endswith(".py"):
                p = os.path.join(r, f)
                out[_mod_name(p, root)] = p
    return out


def build_graph(root: str):
    mods = scan_modules(root)
    tops = {m.split(".")[0] for m in mods}

    def resolve(name: str):
        if not name:
            return None
        parts = name.split(".")
        if parts[0] not in tops:
            return None
        if name in mods:
            return name
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in mods:
                return cand
        return parts[0]

    edges = collections.defaultdict(set)
    linenos = collections.defaultdict(list)
    for m, p in mods.items():
        try:
            tree = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                base = node.module or ""
                targets = [(base + "." + a.name) if base else a.name for a in node.names]
            for t in targets:
                r = resolve(t)
                if r and r != m:
                    edges[m].add(r)
                    linenos[(m, r)].append(node.lineno)
    return mods, edges, linenos


def find_cycles(edges: dict) -> list:
    nodes = set(edges) | {t for v in edges.values() for t in v}
    index, low, onstk, stk, idx, sccs = {}, {}, {}, [], [0], []

    def strong(v):
        index[v] = low[v] = idx[0]
        idx[0] += 1
        stk.append(v)
        onstk[v] = True
        for w in edges.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif onstk.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stk.pop()
                onstk[w] = False
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    for v in sorted(nodes):
        if v not in index:
            strong(v)
    return sorted(sccs, key=len, reverse=True)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="app/ 依赖图体检（只读）")
    ap.add_argument("--app", default=DEFAULT_APP, help="app 目录")
    ap.add_argument("--cycles", action="store_true")
    ap.add_argument("--inversion", action="store_true")
    ap.add_argument("--god", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    want_all = not (args.cycles or args.inversion or args.god)

    mods, edges, ln = build_graph(args.app)
    indeg, outdeg = collections.Counter(), {}
    for s, ts in edges.items():
        outdeg[s] = len(ts)
        for t in ts:
            indeg[t] += 1
    cycles = find_cycles(edges)
    inv = sorted((m, t, ln.get((m, t), [])[:5]) for m in edges if not m.startswith(("routes.", "workers."))
                 for t in edges[m] if t.startswith("routes."))

    if args.json:
        print(json.dumps({
            "modules": len(mods), "edges": sum(len(v) for v in edges.values()),
            "cycles": cycles,
            "inversion": [{"from": m, "to": t, "lines": l} for m, t, l in inv],
            "fan_in": indeg.most_common(30),
            "fan_out": sorted(outdeg.items(), key=lambda kv: -kv[1])[:30],
        }, ensure_ascii=False, indent=2))
        return 0

    if want_all or args.cycles:
        print("=== 循环依赖（强连通分量 >1）：%d 组 ===" % len(cycles))
        for comp in cycles:
            print("  [%d] %s" % (len(comp), ", ".join(comp)))
            for a in comp:
                for b in sorted(edges.get(a, ())):
                    if b in comp:
                        print("        %s -> %s L%s" % (a, b, ln.get((a, b), [])[:4]))
        print()
    if want_all or args.inversion:
        print("=== 分层倒置（非 routes/workers 模块 import routes.*）：%d 条 ===" % len(inv))
        agg = collections.Counter(t for _m, t, _l in inv)
        for t, n in agg.most_common():
            print("  %-28s <- %d 个核心模块" % (t, n))
        for m, t, l in inv[:20]:
            print("      %s -> %s L%s" % (m, t, l))
        print()
    if want_all or args.god:
        print("=== fan-in Top 15（事实上的核心层）===")
        for m, c in indeg.most_common(15):
            print("  %4d  %s" % (c, m))
        print("=== fan-out Top 15（上帝模块候选）===")
        for m, c in sorted(outdeg.items(), key=lambda kv: -kv[1])[:15]:
            print("  %4d  %s" % (c, m))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
