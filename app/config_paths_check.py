# -*- coding: utf-8 -*-
"""路径类配置的启动自检 —— 补齐「配置解析链」这一环。

为什么需要（2026-10-11 真实事故，代价惨重）：

    config.py:49   MJSCXT_COMFYUI_DIR = _env("MJSCXT_COMFYUI_DIR", "") or _auto_comfyui_dir()  # 检测成功
    config.py:52   COMFYUI_ROOT       = _env("COMFYUI_ROOT", "")      # ★ 没有回退 → 空
    config.py:85   _DERIVED = _derive_comfyui_paths(COMFYUI_ROOT)      # 全空
    config.py:89   COMFYUI_OUTPUT_DIR = ...                            # ''

后果：产物搬移的 src 退化成**相对路径**，相对服务进程 CWD 解析必然失败 ——
ComfyUI 出图完全正常，却**逐个资产入库失败并被隔离**；
而当时 116 个测试、58 个模块导入、243 条路由全部通过，**没有任何检查发现它**。

教训：静态检查（能否 import、路由是否齐）无法覆盖「配置解析出的**值**是否有意义」。
所以这里补一类**运行时值自检**：空值 / 相对路径 / 关键目录不存在。

设计原则：
  · **只报告、不阻断启动**（自检失败不该让整个服务起不来）；
  · 分级：EMPTY 与 RELATIVE 是「必然运行期失败」→ error；目录缺失多为首次运行 → warning；
  · 白名单化「允许不存在的路径」（首次运行才会创建的文件/目录），避免刷屏。
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

#: 路径类常量的名字特征
_PATH_NAME_RE = re.compile(r'(DIR$|_DIR$|PATH$|ROOT$|_ROOT$)')

#: 允许暂时不存在（首次运行 / 由流程创建）的常量 —— 只做「非空 + 绝对」检查，不要求存在
ALLOW_MISSING = frozenset({
    'AI_CONFIG_PATH', 'AI_CHAT_HISTORY_PATH', 'AI_SETTINGS_PATH', 'LLM_CONFIG_PATH',
    'QC_CONFIG_PATH', 'PROMPT_ENHANCE_CONFIG_PATH', 'TASKS_DB_PATH', 'PROJECT_INDEX_PATH',
    'WATERMARK_CONFIG_PATH', 'WATERMARK_DIR', 'MIX_CALIBRATION_PATH', 'UPSCALE_CALIBRATION_PATH',
    'WORKFLOW_MAPPING_PATH',
})


def collect(config_module) -> list:
    """收集 (名字, 值) 形式的路径类常量。"""
    out = []
    for k, v in sorted(vars(config_module).items()):
        if not k.isupper() or not isinstance(v, str):
            continue
        if _PATH_NAME_RE.search(k):
            out.append((k, v))
    return out


def check(config_module) -> dict:
    """校验所有路径类常量，返回分级结果。

    :return: {"empty": [...], "relative": [...], "missing": [...], "ok": n}
    """
    empty, relative, missing = [], [], []
    rows = collect(config_module)
    for k, v in rows:
        if not v:
            empty.append(k)
            continue
        if not os.path.isabs(v):
            relative.append((k, v))
            continue
        if k in ALLOW_MISSING:
            continue
        # 「目录类」才要求存在；文件类允许首启后创建
        if k.endswith('DIR') or k.endswith('_ROOT') or k == 'PROJECT_ROOT_DIR' or k == 'PROJECT_DATA_DIR':
            if not os.path.isdir(v):
                missing.append((k, v))
    return {'empty': empty, 'relative': relative, 'missing': missing, 'total': len(rows)}


def startup_report(config_module=None) -> dict:
    """启动时调用：打印分级报告，永不抛异常（自检不得阻断服务启动）。"""
    try:
        if config_module is None:
            import config as config_module  # type: ignore
        r = check(config_module)
    except Exception as e:  # noqa: BLE001
        logger.warning('路径自检执行失败（不影响启动）：%s', e)
        return {'ok': False, 'error': str(e)}
    n = r['total']
    if r['empty'] or r['relative']:
        logger.error('【路径自检】不通过 —— 这些路径会在运行期必然失败：')
        for k in r['empty']:
            logger.error('    %s = <空>  （空路径会退化成相对路径，按进程 CWD 解析）', k)
        for k, v in r['relative']:
            logger.error('    %s = %r  （相对路径依赖进程 CWD，服务从别处启动即失效）', k, v)
    if r['missing']:
        logger.warning('【路径自检】以下目录尚不存在（首次运行常见，若持续如此需检查）：')
        for k, v in r['missing'][:12]:
            logger.warning('    %s = %s', k, v)
    if not (r['empty'] or r['relative'] or r['missing']):
        logger.info('路径自检：%d 个路径常量全部有效（非空 / 绝对 / 目录存在）', n)
    r['ok'] = not (r['empty'] or r['relative'])
    return r


if __name__ == '__main__':
    import json
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    print(json.dumps(startup_report(), ensure_ascii=False, indent=2))