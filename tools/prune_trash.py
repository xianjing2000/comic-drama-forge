#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回收站（output/projects/_trash）清理入口 —— 显式执行，默认预演。

为什么需要它：软删除只有入口没有出口。
  12+ 处代码把产物移入 _trash（项目删除 / 质检拒收 / 镜头重做 / 场景基图替换 /
  分镜格应用 / 视频提示词替换……），设计方向是对的（误删好图好片不可逆），
  但**没有任何代码清理它**，前端也没有入口 —— 实测两处数据根累计 6.42 GB。

为什么不在启动时自动删：与 qc_client.migrate_enabled_default 同一条纪律 ——
  模块级代码会在任何 import app（守卫脚本 / 离线探针 / python -c）时执行，
  不能在那里删用户的文件。所以 app.py 启动时只**报告**占用，清理靠本脚本。

保留策略（可用 --keep-days / --keep-per-project 覆盖）：
  · 项目快照：7 天内 或 同一项目最近 3 个；
  · 分类桶 qc_reject / reset / _novels：各自保留最近 5 / 3 / 3 个。

用法::

    python tools/prune_trash.py                     # 预演（列清单，不删）
    python tools/prune_trash.py --apply             # 真删
    python tools/prune_trash.py --only data         # 只处理运行时数据根
    python tools/prune_trash.py --only workspace    # 只处理工作区（开发环境）
    python tools/prune_trash.py --keep-days 30 --keep-per-project 5 --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'app'))

import output_reclaim as OR  # noqa: E402


def default_targets():
    """返回 [(标签, projects_dir)]：运行时数据根 + 工作区（开发环境数据根）。"""
    out = []
    appdata = os.environ.get('APPDATA')
    if appdata:
        out.append(('数据根', os.path.join(appdata, 'mjscxt-desktop', 'mjscxt-data',
                                           'output', 'projects')))
    out.append(('工作区', os.path.join(_ROOT, 'output', 'projects')))
    return out


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description='回收站清理（默认只预演）')
    ap.add_argument('--apply', action='store_true', help='真正删除（默认只列清单）')
    ap.add_argument('--only', choices=['data', 'workspace'], default=None,
                    help='只处理其中之一（默认两处都处理）')
    ap.add_argument('--keep-days', type=int, default=None, help='保留最近 N 天')
    ap.add_argument('--keep-per-project', type=int, default=None, help='每项目保留最近 N 个')
    args = ap.parse_args(argv)

    targets = default_targets()
    if args.only == 'data':
        targets = [t for t in targets if t[0] == '数据根']
    elif args.only == 'workspace':
        targets = [t for t in targets if t[0] == '工作区']

    total_drop = total_bytes = 0
    for label, pdir in targets:
        print('=' * 70)
        print('%s：%s' % (label, pdir))
        if not os.path.isdir(pdir):
            print('  目录不存在，跳过')
            continue
        plan = OR.trash_plan(pdir, keep_days=args.keep_days,
                             keep_per_project=args.keep_per_project)
        if not plan.get('ok'):
            print('  报告失败：%s' % plan.get('error'))
            continue
        print('  保留 %d 项 / %.1f MB ｜ 待清理 %d 项 / %.1f MB'
              % (len(plan['keep']), sum(x['bytes'] for x in plan['keep']) / 1048576,
                 len(plan['drop']), plan['drop_bytes'] / 1048576))
        for item in sorted(plan['drop'], key=lambda x: -x['bytes'])[:25]:
            print('    - %8.1f MB  %5.1f 天  %-46s  %s'
                  % (item['bytes'] / 1048576, item['age_days'], item['label'][:46], item['why']))
        if len(plan['drop']) > 25:
            print('    … 另有 %d 项' % (len(plan['drop']) - 25))
        total_drop += len(plan['drop'])
        total_bytes += plan['drop_bytes']

        if args.apply and plan['drop']:
            res = OR.trash_reclaim(pdir, keep_days=args.keep_days,
                                   keep_per_project=args.keep_per_project, dry_run=False)
            print('  ✓ 已清理 %d 项，释放 %.1f MB（保留 %d 项）'
                  % (len(res['removed']), res['freed_bytes'] / 1048576, res['kept']))

    print('=' * 70)
    if args.apply:
        print('合计清理 %d 项' % total_drop)
    else:
        print('合计待清理 %d 项 / %.1f MB —— 这是**预演**，未删除任何文件；'
              '确认后加 --apply' % (total_drop, total_bytes / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main())
