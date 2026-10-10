# -*- coding: utf-8 -*-
"""接口健康扫描（**默认只读**）。

⚠️ 这个工具的存在是因为一次真实事故（2026-10-11）：
   为了验证「删除再导出是否安全」，我写了个脚本遍历 243 条路由并对**无参数接口**
   逐一发真实请求，其中 POST 用了空 JSON body —— 结果误调了「保存 AI 配置」类接口，
   把 ai_config.json 的三个模型（text/qc/chat）写成了空，打断了正在跑的生产。

   教训：要验证「只读性质」的事情，绝不能碰写接口。

因此本工具的默认行为是**只发 GET**：
  · 写方法（POST/PUT/PATCH/DELETE）默认只**列出**，不发请求；
  · 必须同时给出 --write 与 --allow=<前缀> 才会真的发写请求（且逐个打印）；
  · 带路径参数（<...>）的路由总是跳过。

用法：
    python tools/scan_endpoints.py                     # 只读扫描（推荐）
    python tools/scan_endpoints.py --list-write         # 只列出写接口
    python tools/scan_endpoints.py --write --allow=/api/foo   # 显式授权写请求
"""
import os
import sys
import argparse

WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
# 这些前缀即使 -write --allow 也拒绝（配置/凭据类，误调会直接破坏现场）
FORBIDDEN_PREFIXES = ('/api/ai/', '/api/config/', '/api/settings/', '/api/autonomous/')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='允许发写请求（需同时给 --allow）')
    ap.add_argument('--allow', action='append', default=[], help='允许发写请求的 URL 前缀')
    ap.add_argument('--list-write', action='store_true', help='只列出写接口')
    args = ap.parse_args()

    os.environ.setdefault('MJSCXT_DATA_DIR',
                          os.path.join(os.environ.get('APPDATA', ''), 'mjscxt-desktop', 'mjscxt-data'))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))
    import importlib
    m = importlib.import_module('app')
    rules = list(m.app.url_map.iter_rules())
    print('  路由总数 = %d' % len(rules))

    writes = []
    for r in rules:
        methods = sorted(r.methods - {'HEAD', 'OPTIONS'})
        if any(x in WRITE_METHODS for x in methods) and '<' not in str(r):
            writes.append((str(r), methods))
    print('  可扫描的写接口 = %d 个（默认**不请求**）' % len(writes))
    if args.list_write:
        for u, ms in writes:
            print('     [%s] %s' % (','.join(ms), u))
        return 0

    ok, bad, skipped = 0, [], 0
    with m.app.test_client() as c:
        for r in rules:
            u = str(r)
            methods = sorted(r.methods - {'HEAD', 'OPTIONS'})
            if '<' in u or r.endpoint in ('index', 'static_assets', 'vite_icon', 'favicon_svg', 'spa_fallback'):
                skipped += 1
                continue
            for meth in methods:
                if meth == 'GET':
                    try:
                        resp = c.get(u)
                        (bad.append((meth, u, resp.status_code)) if resp.status_code >= 500 else None)
                        ok += 1 if resp.status_code < 500 else 0
                    except Exception as e:
                        bad.append((meth, u, 'EXC:' + type(e).__name__))
                    continue
                if meth in WRITE_METHODS:
                    allowed = args.write and any(u.startswith(p) for p in args.allow)
                    forbidden = u.startswith(FORBIDDEN_PREFIXES)
                    if not allowed or forbidden:
                        skipped += 1
                        continue
                    print('     ⚠️ 发写请求 [%s] %s' % (meth, u))
                    try:
                        resp = c.post(u, json={})
                        ok += 1 if resp.status_code < 500 else 0
                        if resp.status_code >= 500:
                            bad.append((meth, u, resp.status_code))
                    except Exception as e:
                        bad.append((meth, u, 'EXC:' + type(e).__name__))

    print()
    print('  只读请求正常 = %d ｜ 500/异常 = %d ｜ 跳过（写接口/带参数/静态）= %d'
          % (ok, len(bad), skipped))
    for meth, u, code in bad:
        print('     [%s] %-52s %s' % (meth, u, code))
    print()
    print('  提示：写接口默认不请求。确需验证时用 --write --allow=<前缀>，'
          '且 /api/ai /api/config /api/settings /api/autonomous 永远被拒绝。')
    return 0


if __name__ == '__main__':
    sys.exit(main())