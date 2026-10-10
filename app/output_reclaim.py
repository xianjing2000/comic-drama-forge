"""产物目录占用报告与安全清理（2026-10-10 设计审查修复）。

## 排查背景
output/ 有 3.4GB / 4037 文件，且**产物与系统数据混在一层**：
  projects      2831MB  ← 正式产物，绝不能删
  _te3d_render   454MB  ← 3D 渲染中间件；实测含 _browser_profile（Edge 的缓存/DB），
                            且**最后一次修改就在当下**（渲染进行中）→ 不能整体删
  asset_lib      105MB  ← 资产库，勿删
  qc              60MB  ← 质检抽帧，可随项目清理
  10镜待判_*      26MB  ← 调试残留，最后一次修改 47 小时前
  lessons/dub/ai_chat/.leases  ← 系统数据（不是"产物"），混在同一层

## 设计原则（保守优先）
  · **只清白名单内、且明确属于缓存/临时的东西**；正式产物一律不碰；
  · 默认 dry_run=True —— 只报告不删除，删除必须显式传 dry_run=False；
  · 绝不删除正在被使用的目录（按 mtime 判定"活跃"）；
  · 全程 fail-safe：任何异常只记日志，绝不影响生产。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: 允许清理的「纯缓存」路径（相对 output/），且必须满足 min_age_hours 才动。
#  为什么是这些：它们是浏览器/渲染器产生的可再生缓存，删掉只会让下次重建时慢一点。
SAFE_CACHE_PATHS: List[str] = [
    '_te3d_render/_browser_profile/Default/Cache',
    '_te3d_render/_browser_profile/Default/Code Cache',
    '_te3d_render/_browser_profile/Default/GPUCache',
    '_te3d_render/_browser_profile/GrShaderCache',
    '_te3d_render/_browser_profile/ShaderCache',
    '_te3d_render/_browser_profile/GPUPersistentCache',
]

#: 需要人工确认、不自动清理的（报告出来供决策）
MANUAL_REVIEW_PREFIXES: List[str] = ['10镜待判_', '_regen_backup_']


def _dirstats(path: str) -> Dict[str, Any]:
    n = 0
    sz = 0
    newest = 0.0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz += os.path.getsize(fp)
                    n += 1
                    m = os.path.getmtime(fp)
                    if m > newest:
                        newest = m
                except OSError:
                    pass
    except OSError:
        pass
    return {'files': n, 'bytes': sz, 'newest': newest,
            'age_hours': (time.time() - newest) / 3600 if newest else -1.0}


def report(output_dir: str, top: int = 15) -> Dict[str, Any]:
    """只读占用报告：各子目录的文件数/体积/最后修改时间，并标注性质。"""
    out: Dict[str, Any] = {'ok': False, 'total_bytes': 0, 'total_files': 0,
                           'items': [], 'manual_review': [], 'error': ''}
    if not output_dir or not os.path.isdir(output_dir):
        out['error'] = 'output 目录不存在'
        return out
    try:
        rows = []
        for name in sorted(os.listdir(output_dir)):
            p = os.path.join(output_dir, name)
            if not os.path.isdir(p):
                continue
            st = _dirstats(p)
            kind = '正式产物'
            if name == 'projects':
                kind = '正式产物（勿删）'
            elif name.startswith('_'):
                kind = '程序内部产物'
            elif name.startswith('10镜') or name.startswith('_regen_backup'):
                kind = '疑似调试/备份残留'
            elif name in ('ai_chat', 'lessons', 'dub', 'asset_lib', '.leases'):
                kind = '系统数据（非产物）'
            rows.append({'name': name, 'files': st['files'], 'bytes': st['bytes'],
                         'age_hours': round(st['age_hours'], 1), 'kind': kind})
            if any(name.startswith(x) for x in MANUAL_REVIEW_PREFIXES):
                out['manual_review'].append(name)
        rows.sort(key=lambda r: -r['bytes'])
        out['items'] = rows[:top]
        out['total_bytes'] = sum(r['bytes'] for r in rows)
        out['total_files'] = sum(r['files'] for r in rows)
        out['ok'] = True
    except Exception as e:  # noqa: BLE001
        out['error'] = '%s: %s' % (type(e).__name__, e)
    return out


def reclaim(output_dir: str, dry_run: bool = True, min_age_hours: float = 6.0) -> Dict[str, Any]:
    """清理白名单内的纯缓存目录。

    ⚠️ 默认 dry_run=True（只报告）。**绝不**触碰 projects / asset_lib / dub /
    qc / lessons 等正式或系统数据；**绝不**删除 mtime 在 min_age_hours 内的目录
    （正在被渲染器使用）。
    """
    res: Dict[str, Any] = {'ok': True, 'dry_run': dry_run, 'freed_bytes': 0,
                           'removed': [], 'skipped': [], 'error': ''}
    if not output_dir or not os.path.isdir(output_dir):
        res['ok'] = False
        res['error'] = 'output 目录不存在'
        return res
    for rel in SAFE_CACHE_PATHS:
        p = os.path.join(output_dir, *rel.split('/'))
        if not os.path.isdir(p):
            continue
        st = _dirstats(p)
        if st['age_hours'] >= 0 and st['age_hours'] < min_age_hours:
            res['skipped'].append({'path': rel, 'reason': '%.1f 小时前仍在写入（可能正在使用）'
                                   % st['age_hours']})
            continue
        res['removed'].append({'path': rel, 'bytes': st['bytes'], 'files': st['files']})
        res['freed_bytes'] += st['bytes']
        if not dry_run:
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception as e:  # noqa: BLE001
                logger.warning('清理 %s 失败：%s', rel, e)
    if dry_run:
        res['note'] = '这是预演（dry_run=True），未删除任何文件；确认后传 dry_run=False 执行'
    return res


def summary_line(output_dir: str) -> str:
    """一行式摘要，供接口/日志。"""
    try:
        r = report(output_dir)
        if not r.get('ok'):
            return '产物报告失败：' + str(r.get('error'))
        return ('产物目录：%.1f MB / %d 个文件 ｜ 需人工确认 %d 项 %s'
                % (r['total_bytes'] / 1024 / 1024, r['total_files'],
                   len(r['manual_review']), r['manual_review'][:3]))
    except Exception as e:  # noqa: BLE001
        return '产物报告失败：' + type(e).__name__
