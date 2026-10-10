# -*- coding: utf-8 -*-
"""助手下沉工具（v4 流程固化）。用法：

    python tools/sink_helpers.py <种子前缀> <目标模块名> ["文件头说明"]

例：
    python tools/sink_helpers.py _mix_ mix_helpers "混音助手"

安全设计（每一条都对应真实踩过的坑）：
  · 闭包只展开「下划线开头」或「全大写常量」——防止 index/static_assets 这类
    通用词被函数体里的同名局部变量命中（曾导致路由悬空、首页 500）；
  · 硬断言：待删名单里出现 index/static_assets/vite_icon/favicon_svg 直接中止；
  · import 从 app.py 原始语句原样复制，不猜来源（曾把 probe_audio_info 猜成不存在的名字）；
  · 删除后立即 ast.parse 校验，失败不落盘；
  · 再导出列表来自新模块实际顶层名，不可能导入不存在的名字。
"""
import io
import os
import ast
import re
import sys

P = 'app/app.py'
src = io.open(P, encoding='utf-8').read()
lines = src.split(chr(10))
tree = ast.parse(src)

IMPORT_LINES = []
for n in tree.body:
    if isinstance(n, ast.ImportFrom):
        IMPORT_LINES.append(([a.asname or a.name for a in n.names],
                             chr(10).join(lines[n.lineno - 1:n.end_lineno])))
    elif isinstance(n, ast.Import):
        IMPORT_LINES.append(([(a.asname or a.name.split('.')[0]) for a in n.names],
                             chr(10).join(lines[n.lineno - 1:n.end_lineno])))


def import_stmt_for(name):
    for nm, txt in IMPORT_LINES:
        if name in nm:
            return txt
    return None


def expandable(name):
    return name.startswith('_') or (name.isupper() and len(name) > 2)


def main(prefix, modname, note=''):
    own = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and expandable(n.name):
            own.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and expandable(t.id):
                    own.add(t.id)

    def span(name):
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return n.lineno, n.end_lineno
        return None, None

    def const_span(name):
        """返回常量的完整行区间。

        ⚠️ 早期版本写成 (i, i+1)（假设单行），但 _SB_STRUCTURAL_DEFECT_KEYWORDS 与
        _OPT_REASONING_MARKERS 是**跨行元组** —— 只抓第一行会得到未闭合的
        `X = (` 从而 ast.parse 报 '(' was never closed（工具因此中止）。
        现按括号/方括号/花括号平衡抓取完整定义。
        """
        for i, l in enumerate(lines):
            if l.startswith(name + ' ='):
                def bal(x):
                    return (x.count('(') + x.count('[') + x.count('{')
                            - x.count(')') - x.count(']') - x.count('}'))
                depth = bal(l)
                j = i + 1
                while depth > 0 and j < len(lines):
                    depth += bal(lines[j])
                    j += 1
                return (i, j)
        return None, None

    SEEDS = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith(prefix)]
    print('  种子 %s* = %d: %s' % (prefix, len(SEEDS), SEEDS))
    if not SEEDS:
        print('  [XX] 无种子'); return 1

    queue, seen, consts = list(SEEDS), [], []
    while queue:
        nm = queue.pop(0)
        if nm in seen or nm == 'app':
            continue
        seen.append(nm)
        a, b = span(nm)
        if a is None:
            if const_span(nm):
                consts.append(nm)
            continue
        body = chr(10).join(lines[a - 1:b])
        for x in own:
            if x != nm and re.search(r'(?<![\\w.])' + re.escape(x) + r'(?![\\w])', body):
                if x not in seen:
                    queue.append(x)

    print('  闭包函数 %d ｜ 常量 %d' % (len(seen), len(consts)))
    print('  名单: %s' % ', '.join(sorted(seen)))
    bad = [x for x in seen if x in ('index', 'static_assets', 'vite_icon', 'favicon_svg')]
    if bad:
        print('  [XX] 闭包含路由视图 %s —— 中止' % bad); return 1

    parts, spans = [], []
    for c in consts:
        sp = const_span(c)
        # 必须取完整区间：常量可能是跨行元组（如 _SB_STRUCTURAL_DEFECT_KEYWORDS），
        # 只取首行会得到未闭合的 `X = (` 导致语法错、工具中止。
        parts.append(chr(10).join(lines[sp[0]:sp[1]])); spans.append(sp)
    if consts:
        parts += ['', '']
    for nm in sorted(seen, key=lambda x: (span(x)[0] or 0)):
        a, b = span(nm)
        if a is None:
            continue
        e = b
        while e > a - 1 and not lines[e - 1].strip():
            e -= 1
        parts.append(chr(10).join(lines[a - 1:e]).replace('app.logger', 'logger'))
        parts += ['', '']
        spans.append((a - 1, e))
    body_txt = chr(10).join(parts)

    DOC = chr(39) * 3
    HEADER = [
        '# -*- coding: utf-8 -*-',
        DOC + (note or modname) + '（2026-10-11 从 app.py 下沉）。' + DOC,
        '',
        '# 由 tools/sink_helpers.py 自动生成：闭包展开 + 原样复制 import。',
        '# 函数体与下沉前逐字一致（仅 app.logger -> logger）。',
        'import logging',
        '',
        'logger = logging.getLogger(__name__)',
        '',
        '',
    ]
    mod = chr(10).join(HEADER) + body_txt

    t2 = ast.parse(mod)
    defined = set()
    for n in ast.walk(t2):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Assign):
            for x in n.targets:
                if isinstance(x, ast.Name):
                    defined.add(x.id)
        elif isinstance(n, ast.Import):
            for a in n.names:
                defined.add(a.asname or a.name.split('.')[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                defined.add(a.asname or a.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            defined.add(n.id)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
    import builtins
    defined |= set(dir(builtins)) | {'__name__', '__file__'}
    missing = sorted({n.id for n in ast.walk(t2)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                      and n.id not in defined and not n.id.startswith('__')})
    good, unresolved = [], []
    for nm in missing:
        st = import_stmt_for(nm)
        # ⚠️ 2026-10-11 修复（真实故障）：app.py 里有「从目标模块再导出」的语句，
        #    原样复制会变成该模块**import 自己**：
        #      lesson_helpers.py:36  from lesson_helpers import (_apply_audio_hints, ...)
        #    → 自导入/循环依赖，连带 keyframe/video/storyboard/routes.* 全部 ImportError。
        #    这类名字本就定义在目标模块内（同模块可见），既不需要 import，也不算未定位。
        if st and re.search(r'from\s+%s\s+import' % re.escape(modname), st):
            continue
        if st and re.search(r'^import\s+%s\s*$' % re.escape(modname), st):
            continue
        if st and st not in good:
            good.append(st)
        elif not st:
            unresolved.append(nm)
    print('  缺失名 %d ｜ 复制 %d ｜ 未定位 %d: %s' % (len(missing), len(good), len(unresolved), unresolved))
    if good:
        idx = mod.index('logger = logging.getLogger')
        mod = mod[:idx] + chr(10).join(good) + chr(10) + chr(10) + mod[idx:]

    path = 'app/%s.py' % modname
    # ⚠️ 2026-10-11 硬约束（真实事故）：**目标模块已存在且非空时一律中止**。
    #    本工具早期直接 `open(path,'w')` 覆盖 —— 对已有内容的模块会**丢掉全部原有函数**：
    #      lesson_helpers.py 196 行 -> 69 行（丢 127 行）
    #      mix_helpers.py    205 行 -> 21 行（丢 184 行）
    #    所幸内容都在 git 中可恢复。正确做法：新函数追加进已有模块，或另起新模块名；
    #    覆盖不是本工具该做的事，故直接在写盘前拦住。
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print('  [XX] 目标模块已存在且非空：%s' % path)
        print('       本工具不覆盖已有模块（会丢失原有函数）。')
        print('       请改用新的模块名，或手工把函数追加进该文件。')
        return 1
    ast.parse(mod)
    io.open(path, 'w', encoding='utf-8').write(mod)
    print('  [OK] %s（%d 行）' % (path, mod.count(chr(10)) + 1))

    kh = ast.parse(io.open(path, encoding='utf-8').read())
    have = set()
    for n in kh.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            have.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    have.add(t.id)
    guard = [x for x in have if x in ('index', 'static_assets', 'vite_icon', 'favicon_svg')]
    if guard:
        print('  [XX] 白名单含路由视图 %s —— 中止删除' % guard); return 1

    export = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in have:
            spans.append((n.lineno - 1, n.end_lineno)); export.append(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in have:
                    spans.append((n.lineno - 1, n.end_lineno)); export.append(t.id)
    export = sorted(set(export))
    print('  删除 %d 个: %s' % (len(export), ', '.join(export)))
    for s, e in sorted(set(spans), reverse=True):
        del lines[s:e]

    rex = ['', '# 2026-10-11 助手下沉：%s 已迁至 %s.py。' % (note or prefix, modname),
           'from %s import (  # noqa: F401, E402' % modname]
    PER = 3
    for i in range(0, len(export), PER):
        ch = export[i:i + PER]
        last = (i + PER >= len(export))
        rex.append('    ' + ', '.join(ch) + ('' if last else ',') + (')' if last else ''))
    ins = max(i for i, l in enumerate(lines) if 'register_blueprint' in l) + 1
    for i, l in enumerate(rex):
        lines.insert(ins + i, l)
    s2 = chr(10).join(lines)
    ast.parse(s2)
    io.open(P, 'w', encoding='utf-8').write(s2)
    print('  [OK] app.py 剩 %d 行' % len(lines))
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ''))
