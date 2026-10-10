# -*- coding: utf-8 -*-
"""按风险分级列出「静默 except」（吞掉异常且不留任何痕迹）。

为什么需要它：
  本仓库有大量 fail-open 设计（可选清理、降级路径），这是**有意**的；
  但「有意降级」与「关键路径静默吞错」外观相同 —— 后者是真实故障的温床。
  实例：ai_credentials_db._decipher 解密失败时静默返回密文，导致拿密文当 api_key
        去调 AI，认证失败且日志无任何提示（2026-10-11 已修）。

分级依据（按模块名/路径推断这处 except 是否处在关键路径）：
  CRITICAL : 密钥/凭证/持久化/状态/并发 —— secret·credential·cred·db·store·state·
             lease·lock·atomic·job_state·schema
  HIGH     : 生产主链路 —— pipeline·autopilot·worker·task·queue·gate
  NORMAL   : 其余（多为可选清理/降级，容忍度高）

退出码：默认 0（只报告）；加 --strict 时若 CRITICAL 非空则返回 1（可用于 CI）。
"""
import ast
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')

_CRITICAL = re.compile(r'secret|credential|cred|_db\b|_store|store\b|state|lease|lock|atomic|schema', re.I)
_HIGH = re.compile(r'pipeline|autopilot|worker|task|queue|gate', re.I)


def is_silent(node, lines):
    body = node.body or []
    if not body:
        return False
    parts = []
    for b in body:
        a = getattr(b, 'lineno', None)
        e = getattr(b, 'end_lineno', None) or a
        if a:
            parts.append('\n'.join(lines[a - 1:e]))
    txt = '\n'.join(parts)
    if ('logger.' in txt) or ('log.' in txt) or ('print(' in txt):
        return False
    if any(isinstance(x, ast.Raise) for b in body for x in ast.walk(b)):
        return False
    trivial = all(isinstance(b, (ast.Pass, ast.Break, ast.Continue)) for b in body)
    ret_const = bool(body) and all(isinstance(b, ast.Return) for b in body)
    return trivial or ret_const


def scan() -> dict:
    buckets = {'CRITICAL': [], 'HIGH': [], 'NORMAL': []}
    for root, _d, files in os.walk(_APP):
        if '__pycache__' in root:
            continue
        for f in sorted(files):
            if not f.endswith('.py'):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, _APP).replace(os.sep, '/')
            try:
                src = io.open(p, encoding='utf-8').read()
                tree = ast.parse(src)
            except Exception:
                continue
            lines = src.split('\n')
            for n in ast.walk(tree):
                if not isinstance(n, ast.ExceptHandler) or not is_silent(n, lines):
                    continue
                scope = rel + ':' + str(n.lineno)
                if _CRITICAL.search(rel):
                    buckets['CRITICAL'].append(scope)
                elif _HIGH.search(rel):
                    buckets['HIGH'].append(scope)
                else:
                    buckets['NORMAL'].append(scope)
    return buckets


def main() -> int:
    strict = '--strict' in sys.argv
    b = scan()
    total = sum(len(v) for v in b.values())
    print('  静默 except 共 %d 处（CRITICAL %d ｜ HIGH %d ｜ NORMAL %d）'
          % (total, len(b['CRITICAL']), len(b['HIGH']), len(b['NORMAL'])))
    if b['CRITICAL']:
        print()
        print('  [!!] CRITICAL —— 密钥/持久化/状态/并发 路径上的静默吞错（建议逐个补日志或显式失败）：')
        for s in b['CRITICAL']:
            print('       %s' % s)
    if b['HIGH']:
        print()
        print('  [ !] HIGH —— 生产主链路上的静默吞错（建议抽空补日志）：')
        for s in b['HIGH']:
            print('       %s' % s)
    if strict and b['CRITICAL']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())