# -*- coding: utf-8 -*-
"""回归测试：禁止「模板可用反而抛异常」这类控制流错误（2026-10-10 真实事故）。

事故经过：删除 f-string 兜底时，把 raise ScriptTemplateError 留在了
    if _tpl:
        prompt = render(...)
        raise ScriptTemplateError(...)   # ← 错：模板正常时也抛
导致**每一集剧本生成都必然失败**，而错误信息却说「模板不可用」，误导排查方向。
生产端到端验证抓到它（静态检查、单元测试、import 检查全都没发现）。

本测试用 AST 检查该模式：某个 if 分支的**末尾**是 raise，且该分支内还有
正常逻辑（render/调用等），说明 raise 很可能是误放。
"""
import ast
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))


class TestNoRaiseAfterNormalWork(unittest.TestCase):

    def _check(self, path):
        tree = ast.parse(io.open(path, encoding='utf-8').read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            body = node.body
            if not body:
                continue
            last = body[-1]
            if not isinstance(last, ast.Raise):
                continue
            # 该分支内除 raise 外还有「实质逻辑」（赋值/调用/返回以外）
            substantive = [s for s in body[:-1]
                           if isinstance(s, (ast.Assign, ast.Expr, ast.Return))]
            if not substantive:
                continue
            # 排除「守卫式」：条件是否定式（if not X: raise）属正常
            test_node = node.test
            is_guard = isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not)
            if is_guard:
                continue
            bad.append((node.lineno, len(substantive)))
        return bad

    def test_novel_to_script_guard(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'app', 'novel_to_script.py')
        bad = self._check(p)
        self.assertEqual(
            bad, [],
            'novel_to_script.py 存在「if 分支末尾 raise、分支内还做正常逻辑」的可疑结构：%s' % bad)

    def test_template_guard_exists(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'app', 'novel_to_script.py')
        src = io.open(p, encoding='utf-8').read()
        self.assertIn('if not _tpl:', src,
                      'script_generate 的模板守卫必须是 if not _tpl: 形式（否定式守卫）')
        idx = src.index('_tpl = prompt_templates.load("script_generate")')
        tail = src[idx:idx + 400]
        self.assertIn('if not _tpl:', tail,
                      '守卫必须紧跟在 _tpl 加载之后')


if __name__ == '__main__':
    unittest.main()
