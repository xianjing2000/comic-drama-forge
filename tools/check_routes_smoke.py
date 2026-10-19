#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端 GET 路由运行时冒烟（只读）。

目的：编译通过 ≠ 运行不炸。把 Flask `url_map` 里的 GET 路由**逐个真调用**一遍，
抓 500 / 未捕获异常 / 卡死（超时）。参数占位一律填安全值，**只跑 GET**，不触碰写接口。

排除（会依赖外部服务或长连接/大文件）：
  · /api/events/stream（SSE 长连接）
  · /api/comfyui/*、/api/deps/*、/api/logs*（外部探测，可能慢）
  · 含 /file/、/scratch/、/audio、/download 的下载型路由

用法：python tools/check_routes_smoke.py [--timeout 5] [--project 项目名] [--json]
退出码：0 = 无 5xx；1 = 存在 5xx 或异常。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

SKIP = ("/api/events/stream", "/api/comfyui/", "/api/deps/", "/api/logs",
        "/file/", "/scratch/", "/download")
CONV = re.compile(r"<(?:[a-z]+(?:\([^)]*\))?:)?(\w+)>")


def build_path(rule: str, project: str) -> str:
    def repl(m):
        name = m.group(1).lower()
        if "episode" in name or name.endswith("_no"):
            return "1"
        if "project" in name or name in ("pid", "key", "novel_id", "novelid"):
            return project
        return "x"
    # path 转换器不替换成带 / 的值（避免一次命中多段）
    return CONV.sub(repl, rule)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--project", default="测试项目")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import app as A
    c = A.app.test_client()
    rules = sorted(A.app.url_map.iter_rules(), key=lambda r: str(r))

    cases = []
    for r in rules:
        if "GET" not in (r.methods or set()):
            continue
        rule = str(r)
        if any(s in rule for s in SKIP):
            continue
        cases.append((rule, build_path(rule, args.project)))

    bad, slow = [], []
    ex = ThreadPoolExecutor(max_workers=1)
    for rule, url in cases:
        try:
            fut = ex.submit(lambda u=url: c.get(u))
            resp = fut.result(timeout=args.timeout)
            code = resp.status_code
            if code >= 500:
                body = resp.data[:300].decode("utf-8", "replace")
                bad.append({"rule": rule, "url": url, "status": code, "body": body})
        except FutTimeout:
            slow.append({"rule": rule, "url": url, "error": "timeout %ss" % args.timeout})
            # 线程卡住无法取消；换一个执行器继续，避免整轮被一个坏路由拖死
            ex.shutdown(wait=False)
            ex = ThreadPoolExecutor(max_workers=1)
        except Exception as e:  # noqa: BLE001
            bad.append({"rule": rule, "url": url, "status": 0,
                        "body": "%s: %s" % (type(e).__name__, e)})
    ex.shutdown(wait=False)

    if args.json:
        print(json.dumps({"total": len(cases), "bad": bad, "slow": slow},
                         ensure_ascii=False, indent=2))
    else:
        print("=" * 74)
        print("GET 路由冒烟：%d 条（超时 %ss，项目参数=%s）" % (len(cases), args.timeout, args.project))
        if bad:
            print("✗ 5xx / 异常：%d" % len(bad))
            for b in bad[:25]:
                print("   %-46s %s" % (b["rule"], b["status"]))
                print("      %s" % b["body"][:220].replace("\n", " "))
        else:
            print("✓ 无 5xx")
        if slow:
            print("! 超时：%d" % len(slow))
            for s in slow[:15]:
                print("   %-46s %s" % (s["rule"], s["error"]))
        print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
