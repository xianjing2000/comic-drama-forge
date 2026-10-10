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
import re
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


# ===================== 回收站（_trash）保留策略 =====================
# 背景（2026-10-10 设计审查）：软删除机制散布在 12+ 处（项目删除、质检拒收、镜头重做、
# 场景基图替换、分镜格应用、视频提示词替换……），全部设计成「移入 _trash，可恢复」——
# 方向是对的（误删好图好片不可逆）。但**只有入口没有出口**：
#   · 没有任何代码清理或还原 _trash；
#   · 前端也没有「回收站」入口；
# 实测后果：两处数据根的 _trash 累计 6.42 GB（工作区 3.65 GB + 数据根 2.76 GB），
# 其中工作区那份是开发环境遗留的孤儿（其项目注册表为 0，生产读的是数据根）。
#
# 本节的职责：给 _trash 补上「出口」，且保留策略足够保守 ——
#   · 分类目录（qc_reject / reset / _novels）按「每类保留最近 N 个」；
#   · 项目快照按「N 天内 或 该项目最近 M 个」；
#   · 默认 dry_run=True，删除必须显式开启；
#   · 全部 fail-safe：任何异常只记日志，绝不影响启动。
try:
    TRASH_KEEP_DAYS = max(0, int(os.environ.get("MJSCXT_TRASH_KEEP_DAYS", "7") or 7))
except (TypeError, ValueError):
    TRASH_KEEP_DAYS = 7
try:
    TRASH_KEEP_PER_PROJECT = max(1, int(os.environ.get("MJSCXT_TRASH_KEEP_PER_PROJECT", "3") or 3))
except (TypeError, ValueError):
    TRASH_KEEP_PER_PROJECT = 3
#: 分类目录（不是项目名）保留最近 N 个 —— 它们各自是一个桶，不按项目分组
TRASH_KEEP_PER_CATEGORY = {'qc_reject': 5, 'reset': 3, '_novels': 3}
#: 设为 0 可整体关闭启动期的自动清理
TRASH_AUTOCLEAN = str(os.environ.get("MJSCXT_TRASH_AUTOCLEAN", "1")).strip().lower() not in (
    "0", "false", "no", "off")

_STAMP_RE = re.compile(r'^(\d{8})_(\d{6})_(.+)$')


def _entry_stats(path: str):
    """(文件数, 字节, 内部最新文件的 mtime)。

    ⚠️ 用**内部最新文件**的时间，不用目录自身 mtime：目录 mtime 会因任何子项增删而变化，
    实测出现过「目录显示 0.3 天前、里面最新文件其实是 16 天前」的假新鲜度。
    """
    n = 0
    sz = 0
    newest = 0.0
    for root, _dirs, files in os.walk(path):
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
    return n, sz, newest


def _parse_entry(name: str, path: str):
    """解析回收站条目 -> (项目键, 时间戳秒)。非 <时间戳>_<键> 形态则回落目录 mtime。"""
    m = _STAMP_RE.match(name)
    if m:
        try:
            return m.group(3), time.mktime(time.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S'))
        except Exception:  # noqa: BLE001
            pass
    return name, os.path.getmtime(path)


def trash_plan(projects_dir: str, keep_days: int = None, keep_per_project: int = None):
    """算出回收站里「保留哪些、清理哪些」，不改动任何文件。

    返回 dict：ok / trash_dir / keep / drop / total_bytes / drop_bytes / error
    """
    kd = TRASH_KEEP_DAYS if keep_days is None else keep_days
    kp = TRASH_KEEP_PER_PROJECT if keep_per_project is None else keep_per_project
    out = {'ok': False, 'trash_dir': '', 'keep': [], 'drop': [],
           'total_bytes': 0, 'drop_bytes': 0, 'error': ''}
    if not projects_dir:
        out['error'] = 'projects_dir 为空'
        return out
    trash = os.path.join(projects_dir, '_trash')
    out['trash_dir'] = trash
    if not os.path.isdir(trash):
        out['ok'] = True
        return out
    now = time.time()
    try:
        for name in sorted(os.listdir(trash)):
            top = os.path.join(trash, name)
            if not os.path.isdir(top):
                continue
            if name in TRASH_KEEP_PER_CATEGORY:
                # 分类桶：下钻一层，每类保留最近 N 个
                limit = TRASH_KEEP_PER_CATEGORY[name]
                subs = []
                for s in os.listdir(top):
                    sp = os.path.join(top, s)
                    if os.path.isdir(sp):
                        n, sz, newest = _entry_stats(sp)
                        subs.append({'path': sp, 'label': '%s/%s' % (name, s), 'files': n,
                                     'bytes': sz, 'mtime': newest,
                                     'age_days': (now - newest) / 86400})
                subs.sort(key=lambda x: x['mtime'], reverse=True)
                for i, item in enumerate(subs):
                    out['total_bytes'] += item['bytes']
                    if i < limit:
                        item['why'] = '分类桶保留最近 %d 个' % limit
                        out['keep'].append(item)
                    else:
                        item['why'] = '超出分类桶保留数 %d' % limit
                        out['drop'].append(item)
            else:
                n, sz, newest = _entry_stats(top)
                key, ts = _parse_entry(name, top)
                age = (now - ts) / 86400
                item = {'path': top, 'label': name, 'key': key, 'files': n, 'bytes': sz,
                        'mtime': newest, 'age_days': age, '_ts': ts}
                out['total_bytes'] += sz
                out['keep'].append(item)      # 先全部当保留，下面按项目组重排
        # 项目快照：每个项目键保留最近 kp 个，或 kd 天内
        snapshots = [x for x in out['keep'] if '_ts' in x]
        out['keep'] = [x for x in out['keep'] if '_ts' not in x]
        bykey = {}
        for item in snapshots:
            bykey.setdefault(item['key'], []).append(item)
        for _key, items in bykey.items():
            items.sort(key=lambda x: x['_ts'], reverse=True)
            for i, item in enumerate(items):
                fresh = item['age_days'] <= kd
                if fresh or i < kp:
                    item['why'] = ('%d 天内' % kd) if fresh else ('该项目最近 %d 个' % kp)
                    out['keep'].append(item)
                else:
                    item['why'] = '超过 %d 天且非最近 %d 个' % (kd, kp)
                    out['drop'].append(item)
        for item in out['keep'] + out['drop']:
            item.pop('_ts', None)
        out['drop_bytes'] = sum(x['bytes'] for x in out['drop'])
        out['ok'] = True
    except Exception as e:  # noqa: BLE001
        out['error'] = '%s: %s' % (type(e).__name__, e)
    return out


def trash_reclaim(projects_dir: str, keep_days: int = None, keep_per_project: int = None,
                  dry_run: bool = True):
    """按保留策略清理回收站。默认 dry_run=True，只报告不删除。

    ⚠️ 安全闸：只删 projects_dir/_trash 直接子项里的目录，且路径必须真的落在
    _trash 之下 —— 绝不触碰 _trash 目录本身，也绝不越界到 projects/ 其它位置。
    """
    plan = trash_plan(projects_dir, keep_days=keep_days, keep_per_project=keep_per_project)
    res = {'ok': plan.get('ok', False), 'dry_run': dry_run, 'freed_bytes': 0,
           'removed': [], 'kept': len(plan.get('keep') or []), 'error': plan.get('error', '')}
    if not res['ok']:
        return res
    trash = os.path.normpath(plan.get('trash_dir') or '')
    if not trash:
        res['ok'] = False
        res['error'] = '未解析出 _trash 路径'
        return res
    for item in plan.get('drop') or []:
        p = os.path.normpath(item['path'])
        if os.path.normpath(os.path.dirname(p)) != trash:
            logger.warning('跳过越界路径：%s', p)
            continue
        res['removed'].append({'label': item['label'], 'bytes': item['bytes']})
        res['freed_bytes'] += item['bytes']
        if not dry_run:
            try:
                shutil.rmtree(p, ignore_errors=False)
            except Exception as e:  # noqa: BLE001
                logger.warning('回收站清理失败 %s：%s', p, e)
    if dry_run:
        res['note'] = '预演（dry_run=True），未删除任何文件'
    return res


def trash_startup_report(projects_dir: str, apply: bool = True) -> str:
    """启动期一次调用：报告回收站占用，并按策略清理。返回一行摘要（永不抛）。"""
    try:
        plan = trash_plan(projects_dir)
        if not plan.get('ok'):
            return '回收站报告失败：%s' % plan.get('error')
        if not plan.get('drop'):
            return ('回收站：保留 %d 项 / %.1f MB，无需清理'
                    % (len(plan['keep']), plan['total_bytes'] / 1048576))
        if not (apply and TRASH_AUTOCLEAN):
            return ('回收站：%d 项待清理 / %.1f MB（自动清理已关闭）'
                    % (len(plan['drop']), plan['drop_bytes'] / 1048576))
        res = trash_reclaim(projects_dir, dry_run=False)
        return ('回收站：清理 %d 项 / 释放 %.1f MB（保留 %d 项）'
                % (len(res['removed']), res['freed_bytes'] / 1048576, res['kept']))
    except Exception as e:  # noqa: BLE001
        return '回收站清理异常（不影响启动）：%s' % e


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
