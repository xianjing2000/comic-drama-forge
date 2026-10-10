"""配置自检与统一登记（2026-10-10，用户要求「别老是改了一个地方另一个地方没改导致冲突」）。

## 要解决的问题（实测）
1. **同一份提示词三处副本**：app/prompts/*.txt（外部，优先）+ prompt_templates.py 的
   _DEFAULT_*（代码兜底）+ 部分模块内的 f-string 兜底。代码注释自己写着
   「改任何一处都要同步另外两处」—— 这是文档化的脆弱点，实际就是漏改的来源。
2. **同名常量多处定义**：如 COVERAGE_MAX_ROUNDS 同时在 continuity.py 与
   novel_to_script.py；今天已因此产生过真实缺陷（两处值不一致时行为取决于导入顺序）。
3. **可调参数散落**：346 个大写常量分布在 12 个文件，其中真正需要用户可调的只有几十个。

## 本模块的定位（第一步：只读自检，零行为变更）
**不改变任何取值路径**，只做两件事：
  · doctor()：扫描上述三类冲突，返回结构化报告（供 API / 界面 / 日志）；
  · REGISTRY：把「用户可调参数」显式登记在一处（键/类型/默认/范围/分组/说明），
    作为后续迁移到统一配置中心（数据库持久化）的唯一事实源。

为什么先做只读自检：统一配置中心要动核心取值路径，风险高；而冲突检测本身立刻可用、
零风险，且能立刻曝出「改了一处漏了另一处」的全部现存问题，为后续迁移提供依据。
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ============================ 可调参数登记表 ============================
# 只登记**用户可能想调**的参数（阈值 / 轮数 / 开关 / 上限）。
# 实现常量（正则、枚举、表结构、路径模板）不登记 —— 它们本就不该可调。
#
# 字段说明：
#   key        : 唯一键（也是未来数据库表 app_settings 的主键）
#   default    : 期望的代码默认值（doctor 会与实际值比对，不一致即告警）
#   type       : int / float / bool / str
#   range      : (min, max) 供未来界面校验；None 表示不限
#   group      : 分组，便于界面归类
#   desc       : 一句话说明
#   owner      : **唯一事实源**所在模块（doctor 检查该模块的实际值）
#   must_match : 必须与 owner 保持同值的其它位置（跨模块重复定义的显式声明）
REGISTRY: Dict[str, Dict[str, Any]] = {
    # ---- 原文覆盖率与重写轮数（2026-10-10 用户要求「还原所有细节」后调整）----
    'coverage_threshold': {
        'default': 0.98, 'type': 'float', 'range': (0.0, 1.0), 'group': '覆盖率',
        'desc': '原文覆盖率阈值：低于该值触发补生成（原 0.70，96.5% 都从不触发）',
        'owner': 'config.COVERAGE_THRESHOLD',
    },
    'coverage_max_rounds': {
        'default': 3, 'type': 'int', 'range': (0, 10), 'group': '覆盖率',
        'desc': '覆盖率不足时自动补生成的最大轮数（原 1）',
        'owner': 'continuity.COVERAGE_MAX_ROUNDS',
        'must_match': ['novel_to_script.COVERAGE_MAX_ROUNDS'],
    },
    'max_rewrite_rounds': {
        'default': 2, 'type': 'int', 'range': (0, 10), 'group': '覆盖率',
        'desc': '跨集连贯性问题触发局部重写的最大轮数（原 1）',
        'owner': 'continuity.MAX_REWRITE_ROUNDS',
    },
    'max_qc_script_rewrite_rounds': {
        'default': 2, 'type': 'int', 'range': (0, 10), 'group': '质检闭环',
        'desc': '剧本质检不通过时，按缺陷回传重写的最大轮数（2026-10-10 新增闭环）',
        'owner': 'pipeline.MAX_QC_SCRIPT_REWRITE_ROUNDS',
    },
    # ---- 剧本生成 ----
    'target_shots': {
        'default': 0, 'type': 'int', 'range': (0, 500), 'group': '剧本',
        'desc': '每集目标镜数；0 = 不预设（用户口径：上下限都不限制）',
        'owner': 'config.NOVEL_DEFAULT_SHOTS',
    },
    'auto_screenplay': {
        'default': True, 'type': 'bool', 'range': None, 'group': '剧本',
        'desc': '文学剧本自动生成（用户指定「无需人审」）',
        'owner': 'pipeline.DEFAULT_CONFIG.auto_screenplay',
    },
    'prewarm_next_script': {
        'default': True, 'type': 'bool', 'range': None, 'group': '预热',
        'desc': '生成本集期间后台预热下一集剧本',
        'owner': 'pipeline.DEFAULT_CONFIG.prewarm_next_script',
    },
    'prewarm_asset_prompt': {
        'default': True, 'type': 'bool', 'range': None, 'group': '预热',
        'desc': '资产批次生成期间后台预热全部资产的增强提示词',
        'owner': 'pipeline.DEFAULT_CONFIG.prewarm_asset_prompt',
    },
    # ---- 提示词长度（用户明确要求「不设上限」）----
    'max_prompt_chars': {
        'default': 32000, 'type': 'int', 'range': (1000, 200000), 'group': '提示词长度',
        'desc': '视频提示词字符上限（原 6000；用户要求不限制长度后提到 32000）',
        'owner': 'h3_prompt_kit.MAX_PROMPT_CHARS',
    },
    'max_detail_chars': {
        'default': 4000, 'type': 'int', 'range': (100, 40000), 'group': '提示词长度',
        'desc': '画面细节字段字符上限（原 800）',
        'owner': 'h3_prompt_kit.MAX_DETAIL_CHARS',
    },
    'caption_max_chars': {
        'default': 200, 'type': 'int', 'range': (10, 5000), 'group': '提示词长度',
        'desc': '字幕「偏长」告警阈值（仅告警不截断；原 24 且会硬截断）',
        'owner': 'novel_to_script.CAPTION_MAX_CHARS',
    },
    # ---- LLM 思考额度 ----
    'reasoning_only_token_floor': {
        'default': 24576, 'type': 'int', 'range': (1024, 200000), 'group': 'LLM',
        'desc': '「只吐思考」自救的 token 水位（实测 16384 仍空正文，24576 才成功）',
        'owner': 'llm_client.REASONING_ONLY_TOKEN_FLOOR',
    },
    'max_tokens_ceiling': {
        'default': 32768, 'type': 'int', 'range': (1024, 200000), 'group': 'LLM',
        'desc': 'max_tokens 硬上限',
        'owner': 'llm_client.MAX_TOKENS_CEILING',
    },
    # ---- 镜头时长 ----
    'shot_duration_min': {
        'default': 5.0, 'type': 'float', 'range': (0.5, 60.0), 'group': '镜头',
        'desc': '单镜最短秒数（用户口径：每镜 5~6 秒）',
        'owner': 'config.SHOT_DURATION_MIN',
    },
    'shot_duration_max': {
        'default': 8.0, 'type': 'float', 'range': (0.5, 120.0), 'group': '镜头',
        'desc': '单镜最长秒数。⚠️ 用户口径是「每镜 5~6 秒」，但代码刻意留到 8.0 —— '
                '注释写明「留余量给长台词；>6 秒的镜属长尾」。两者不是冲突，是留了余量。',
        'owner': 'config.SHOT_DURATION_MAX',
    },
}


def _resolve(dotted: str):
    """解析 模块.属性 或 模块.字典属性.键，取不到返回 (None, 错误说明)。"""
    try:
        parts = dotted.split('.')
        mod = importlib.import_module(parts[0])
        cur = mod
        for p in parts[1:]:
            cur = cur[p] if isinstance(cur, dict) else getattr(cur, p)
        return cur, ''
    except Exception as e:  # noqa: BLE001
        return None, f'{type(e).__name__}: {e}'


def prompt_fingerprints(prompts_dir: str = None) -> Dict[str, Any]:
    """比对「外部提示词文件」与「代码内兜底副本」是否一致。

    当前 script_generate 存在真正的双副本（外部 txt + _DEFAULT_SCRIPT_GENERATE）；
    其余模板在 prompt_templates 里也有 _DEFAULT_*，同样比对。返回差异清单。
    """
    out = {'checked': 0, 'mismatch': [], 'missing': [], 'ok': []}
    try:
        import prompt_templates as PT
    except Exception as e:  # noqa: BLE001
        out['error'] = str(e)[:200]
        return out
    base = prompts_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
    reg = getattr(PT, 'REGISTRY', {}) or {}
    for name, meta in reg.items():
        fn = (meta or {}).get('file')
        if not fn:
            continue
        dflt = getattr(PT, '_DEFAULT_' + name.upper(), None)
        if dflt is None:
            continue
        out['checked'] += 1
        path = os.path.join(base, fn)
        if not os.path.isfile(path):
            out['missing'].append(name)
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = f.read()
            # ⚠️ 必须与 prompt_templates.load() 用同一口径：它返回的是
            #    _strip_header() 之后的**生效正文**。直接拿含头注释的原文去比，
            #    会把「注释不同、正文相同」误报成不一致（本模块第一版就踩了这个坑，
            #    报出 script_generate 差 2186 字，实际正文逐字相同）。
            strip = getattr(PT, '_strip_header', None)
            raw = strip(raw) if callable(strip) else raw
        except Exception as e:  # noqa: BLE001
            out['missing'].append(name + '（读取失败 ' + type(e).__name__ + '）')
            continue
        h1 = hashlib.sha1(raw.strip().encode('utf-8')).hexdigest()[:12]
        h2 = hashlib.sha1(str(dflt).strip().encode('utf-8')).hexdigest()[:12]
        if h1 == h2:
            out['ok'].append(name)
        else:
            out['mismatch'].append({'name': name, 'file': fn, 'ext_sha1': h1, 'code_sha1': h2,
                                    'ext_len': len(raw), 'code_len': len(str(dflt))})
    return out


def doctor() -> Dict[str, Any]:
    """配置体检：返回冲突的结构化报告（只读，绝不抛异常）。"""
    rep: Dict[str, Any] = {'ok': True, 'params': [], 'duplicates': [], 'prompts': {},
                           'warnings': []}
    for key, meta in REGISTRY.items():
        actual, err = _resolve(meta.get('owner') or '')
        row = {'key': key, 'owner': meta.get('owner'), 'expect': meta.get('default'),
               'actual': actual, 'group': meta.get('group'), 'match': None, 'error': err}
        if err:
            row['match'] = False
            rep['warnings'].append(key + ': 无法解析 ' + str(meta.get('owner')) + '（' + err + '）')
        else:
            row['match'] = (actual == meta.get('default'))
            if not row['match']:
                rep['warnings'].append(key + ': 登记默认 ' + str(meta.get('default'))
                                       + ' 但实际 ' + str(actual)
                                       + '（' + str(meta.get('owner')) + '）')
        rep['params'].append(row)
        for other in (meta.get('must_match') or []):
            o_actual, o_err = _resolve(other)
            if o_err or o_actual != actual:
                rep['ok'] = False
                rep['duplicates'].append({'key': key, 'primary': meta.get('owner'),
                                          'primary_value': actual, 'other': other,
                                          'other_value': None if o_err else o_actual,
                                          'error': o_err})
    rep['prompts'] = prompt_fingerprints()
    for m in (rep['prompts'].get('mismatch') or []):
        rep['ok'] = False
        rep['warnings'].append('提示词 ' + m['name'] + '：外部文件(' + str(m['ext_len'])
                               + '字/' + m['ext_sha1'] + ') 与代码兜底(' + str(m['code_len'])
                               + '字/' + m['code_sha1'] + ') 不一致')
    if any(not r.get('match') for r in rep['params']):
        rep['ok'] = False
    return rep


def summary_line() -> str:
    """一行式摘要，供启动日志使用。"""
    try:
        r = doctor()
        p = r.get('prompts') or {}
        return ('配置体检：参数 ' + str(len(r.get('params') or [])) + ' 项（'
                + str(sum(1 for x in (r.get('params') or []) if x.get('match') is False))
                + ' 项与登记不符）｜提示词 ' + str(p.get('checked', 0)) + ' 份（'
                + str(len(p.get('mismatch') or [])) + ' 份双副本不一致）｜需要关注 '
                + str(len(r.get('warnings') or [])) + ' 条')
    except Exception as e:  # noqa: BLE001
        return '配置体检失败：' + type(e).__name__ + ': ' + str(e)
