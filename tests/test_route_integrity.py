# -*- coding: utf-8 -*-
"""路由完整性测试：每个 @app.route 装饰器必须最终挂到一个函数定义上。

事故背景（2026-10-11）：搬迁 _resolve_static_dir 时，把它下面紧邻的
    @app.route('/')
    def index():
        return send_from_directory(_STATIC_DIR, 'index.html')
整段误删，只留下悬空的 @app.route('/') 贴在下一个 @app.route 上，
Flask 于是把 / 注册到 static_assets，访问首页报
    TypeError: static_assets() missing 1 required positional argument: 'filename'

判定规则（避免误报）：
  · @app.route 后允许出现**其他装饰器**（如 @_autopilot_guard），这是合法多装饰器；
  · 但连续装饰器序列中**不能出现第二个 @app.route** —— 那说明前一个没有函数；
  · 序列最终必须到达一个 def。
"""
import io
import os
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app', 'app.py')


def _scan(src):
    lines = src.split('\n')
    bad = []
    for i, l in enumerate(lines):
        if not l.startswith('@app.route('):
            continue
        j = i + 1
        seen_route = False
        while j < len(lines):
            s = lines[j].strip()
            if not s or s.startswith('#'):
                j += 1
                continue
            if s.startswith('@'):
                if s.startswith('@app.route('):
                    seen_route = True
                j += 1
                continue
            break
        nxt = lines[j] if j < len(lines) else ''
        if seen_route:
            bad.append((i + 1, '连续 @app.route 未配对函数', nxt[:60]))
        elif not nxt.startswith('def '):
            bad.append((i + 1, '装饰器后不是 def', nxt[:60]))
    return bad


class TestRouteIntegrity(unittest.TestCase):

    def test_no_dangling_route_decorator(self):
        bad = _scan(io.open(APP, encoding='utf-8').read())
        self.assertEqual(bad, [], 'app.py 存在悬空/错位的 @app.route：%s' % bad)

    def test_index_route_serves_html(self):
        src = io.open(APP, encoding='utf-8').read()
        self.assertIn("@app.route('/')", src)
        idx = src.index("@app.route('/')")
        # 窗口给足：index 的 docstring 记录了这起事故，约 8 行
        tail = src[idx:idx + 1200]
        self.assertIn('def index(', tail,
                      "@app.route('/') 后面必须紧跟 def index()，否则 / 会注册到下一个视图")
        self.assertIn('index.html', tail, 'index 视图必须返回 index.html')

    def test_static_assets_needs_filename(self):
        # static_assets 只应挂在 /assets/<path:filename> 上
        src = io.open(APP, encoding='utf-8').read()
        i = src.index('def static_assets(filename)')
        # 往上找最近的装饰器
        head = src[:i].rstrip().split('\n')[-1]
        self.assertIn('/assets/<path:filename>', head,
                      'static_assets 必须只挂在 /assets/<path:filename>')


if __name__ == '__main__':
    unittest.main()
