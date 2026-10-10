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
    # ⚠️ 2026-10-10 命名统一（用户要求「相同的配置字段要统一名称」）：
    #    配置 key 的生成规则统一为「常量名小写化」—— 唯一且可逆。
    #    此前这一项手写成 target_shots，与常量 NOVEL_DEFAULT_SHOTS 反推不一致，
    #    是全表唯一的不可逆项。现正名为 novel_default_shots；
    #    target_shots 作为**别名**保留（旧调用方仍可读写，见 config_center.ALIASES）。
    'novel_default_shots': {
        'default': 0, 'type': 'int', 'range': (0, 2000), 'group': '基础配置',
        'desc': '显式传 4~40 仍可按题材指定下限（如悬疑推理 18~30）。',
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

    # ===== 以下为 2026-10-10 从代码扫描自动登记（用户要求「所有配置字段都进数据库」）=====
    # 生成方式：扫描 app/*.py 的全部大写常量 → 过滤掉枚举/路径/集合/标注「不可调」者 →
    # 默认值直接取代码实际值（因此登记即刻与现状一致，零风险）；范围按命名语义推断。
    # 这些项现在都可以通过 POST /api/config/settings 修改，写入即回写运行时。
    'poll_interval_sec': {
        'default': 5, 'type': 'int', 'range': (0, 100), 'group': '其它',
        'desc': '这里给异步工具补上「派发 → 轮询状态接口到终态 → 如实汇报」的闭环。',
        'owner': 'agent_core.POLL_INTERVAL_SEC',
    },
    'silence_threshold_db': {
        'default': -35.0, 'type': 'float', 'range': (-100.0, 0.0), 'group': '音频',
        'desc': ': 静音判定门限（低于该电平视为静音），交给 ffmpeg silencedetect 的 noise 参数',
        'owner': 'audio_qc.SILENCE_THRESHOLD_DB',
    },
    'silence_min_duration': {
        'default': 0.35, 'type': 'float', 'range': (0.0, 1.0), 'group': '音频',
        'desc': ': 最短静音段（秒）：避免把正常换气、句读停顿当成静音',
        'owner': 'audio_qc.SILENCE_MIN_DURATION',
    },
    'clip_max_db': {
        'default': -0.1, 'type': 'float', 'range': (0.0, 1.0), 'group': '音频',
        'desc': ': 峰值电平高于此值 → 削波失真风险（软扣分项，不阻断）',
        'owner': 'audio_qc.CLIP_MAX_DB',
    },
    'min_valid_duration': {
        'default': 0.15, 'type': 'float', 'range': (0.0, 1.0), 'group': '音频',
        'desc': ': 有效音频最短时长（秒），更短视为空文件 / 合成失败（硬闸）',
        'owner': 'audio_qc.MIN_VALID_DURATION',
    },
    'hard_silent_ratio': {
        'default': 0.15, 'type': 'float', 'range': (0.0, 1.0), 'group': '音频',
        'desc': ': 有声占比低于此值 → 视为整段无声（硬闸）',
        'owner': 'audio_qc.HARD_SILENT_RATIO',
    },
    'probe_timeout': {
        'default': 300, 'type': 'int', 'range': (0, 100000), 'group': '音频',
        'desc': ': 单次 ffmpeg 指标探测超时（秒）',
        'owner': 'audio_qc.PROBE_TIMEOUT',
    },
    'render_timeout': {
        'default': 300, 'type': 'int', 'range': (0, 100000), 'group': '音频',
        'desc': ': 单次可视化渲染超时（秒）',
        'owner': 'audio_qc.RENDER_TIMEOUT',
    },
    'primary_min_chunks': {
        'default': 3, 'type': 'int', 'range': (0, 100), 'group': '小说解析',
        'desc': '出现次数相同的按「姓名总出现次数」二次排序（LLM 提炼已按戏份排序）。',
        'owner': 'book_outline.PRIMARY_MIN_CHUNKS',
    },
    'sample_chars': {
        'default': 6000, 'type': 'int', 'range': (0, 10000000), 'group': '小说解析',
        'desc': '喂给 LLM 的样本长度（取小说前段，足以观察其章节标记风格）',
        'owner': 'chapter_llm.SAMPLE_CHARS',
    },
    'structure_preview_chars': {
        'default': 160, 'type': 'int', 'range': (0, 100000), 'group': '小说解析',
        'desc': '并给出「真正的第一章」——在生成剧本之前完成这次体检（结果缓存进 meta，全书一次）。',
        'owner': 'chapter_llm.STRUCTURE_PREVIEW_CHARS',
    },
    'structure_max_rows': {
        'default': 200, 'type': 'int', 'range': (0, 100000), 'group': '小说解析',
        'desc': '并给出「真正的第一章」——在生成剧本之前完成这次体检（结果缓存进 meta，全书一次）。',
        'owner': 'chapter_llm.STRUCTURE_MAX_ROWS',
    },
    'default_max_entries': {
        'default': 2000, 'type': 'int', 'range': (0, 10000000), 'group': '任务台账',
        'desc': ': 台账最多保留多少条（超上限按时间淘汰最旧）。',
        'owner': 'comfyui_job_store.DEFAULT_MAX_ENTRIES',
    },
    'shot_duration_desc_sec_max': {
        'default': 0.5, 'type': 'float', 'range': (0.0, 1.0), 'group': '基础配置',
        'desc': ': 画面描述带来的时长加成上限（旧值 2.0s）+ 折算系数（每多少字给 1 秒）',
        'owner': 'config.SHOT_DURATION_DESC_SEC_MAX',
    },
    'shot_duration_desc_chars_per_sec': {
        'default': 150.0, 'type': 'float', 'range': (0.0, 100.0), 'group': '基础配置',
        'desc': ': 画面描述带来的时长加成上限（旧值 2.0s）+ 折算系数（每多少字给 1 秒）',
        'owner': 'config.SHOT_DURATION_DESC_CHARS_PER_SEC',
    },
    'shot_duration_action_sec_max': {
        'default': 0.5, 'type': 'float', 'range': (0.0, 1.0), 'group': '基础配置',
        'desc': ': 动作复杂度带来的时长加成上限（旧值 1.5s）',
        'owner': 'config.SHOT_DURATION_ACTION_SEC_MAX',
    },
    'shot_speech_budget_chars': {
        'default': 16, 'type': 'int', 'range': (0, 100000), 'group': '基础配置',
        'desc': ': 单镜台词字数预算（16 字 ≈ 3.6 秒配音）。配合规则 13 一起约束单镜台词长度。',
        'owner': 'config.SHOT_SPEECH_BUDGET_CHARS',
    },
    'clear_comfyui_history_interval_sec': {
        'default': 300.0, 'type': 'float', 'range': (0.0, 1000000.0), 'group': '基础配置',
        'desc': ': 两次清理之间的最小间隔（秒）。太频繁会让「刚跑完那一镜」的现场也被清掉。',
        'owner': 'config.CLEAR_COMFYUI_HISTORY_INTERVAL_SEC',
    },
    'scene_view_dup_phash_max': {
        'default': 95.0, 'type': 'float', 'range': (0.0, 10000.0), 'group': '基础配置',
        'desc': ':   （如「对峙空地 right45」≈99.2 这类近重复），80~95 一律放行。',
        'owner': 'config.SCENE_VIEW_DUP_PHASH_MAX',
    },
    'char_surprise_appear_gap': {
        'default': 3, 'type': 'int', 'range': (0, 100), 'group': '连贯性',
        'desc': ': 首次出镜序号（0-based）≥ 此值判「突兀出场」（前 3 镜内的首次出镜视为正常开场）。',
        'owner': 'continuity_contract.CHAR_SURPRISE_APPEAR_GAP',
    },
    'char_disappear_gap': {
        'default': 4, 'type': 'int', 'range': (0, 100), 'group': '连贯性',
        'desc': ': 末次出镜到集尾剩余镜头数 ≥ 此值判「中途消失」（角色应自然收尾而非断在中间）。',
        'owner': 'continuity_contract.CHAR_DISAPPEAR_GAP',
    },
    'max_contract_issues': {
        'default': 500, 'type': 'int', 'range': (0, 100000), 'group': '连贯性',
        'desc': '这些 issue 会被拼进局部重写的提示词，无限增长会把 prompt 撑爆。',
        'owner': 'continuity_contract.MAX_CONTRACT_ISSUES',
    },
    'default_min_age_sec': {
        'default': 86400, 'type': 'int', 'range': (0, 10000000), 'group': '运维',
        'desc': ': 回收的最小「年龄」：``mtime`` 距今必须超过该秒数（默认 24 小时）。',
        'owner': 'disk_reclaim.DEFAULT_MIN_AGE_SEC',
    },
    'min_segment_frames': {
        'default': 5, 'type': 'int', 'range': (0, 100), 'group': 'H3 导演台',
        'desc': ': 单段最小帧数（源 ``MIN_GEN_VIDEO_FRAMES = 4``，但官方换算下限是 5）',
        'owner': 'h3_director_builder.MIN_SEGMENT_FRAMES',
    },
    'max_segment_loras': {
        'default': 8, 'type': 'int', 'range': (0, 100), 'group': 'H3 导演台',
        'desc': '段级 LoRA 条数上限（与插件 segment_loras.normalize_lora_rows 一致，=8）。'
                '⚠️ 该常量在 h3_segment_loras 与 h3_director_builder **两处各定义一份**，'
                '故用 must_match 锁死同值 —— 改一处两处一起改。',
        'owner': 'h3_segment_loras.MAX_SEGMENT_LORAS',
        'must_match': ['h3_director_builder.MAX_SEGMENT_LORAS'],
    },
    'beat_max_sec': {
        'default': 6.0, 'type': 'float', 'range': (0.0, 60.0), 'group': 'H3 提示词',
        'desc': ': 单镜最长时长（超过则拆成多个时间码节拍，让时间轴与目标时长对齐）',
        'owner': 'h3_prompt_kit.BEAT_MAX_SEC',
    },
    'h3_segment_max_sec': {
        'default': 4.0, 'type': 'float', 'range': (0.0, 60.0), 'group': 'H3 提示词',
        'desc': ': 故生成期必须强制切段。',
        'owner': 'h3_prompt_kit.H3_SEGMENT_MAX_SEC',
    },
    'h3_segment_min_sec': {
        'default': 1.5, 'type': 'float', 'range': (0.0, 60.0), 'group': 'H3 提示词',
        'desc': ': ``ceil(duration / H3_SEGMENT_MAX_SEC)`` 的基础上，把余数摊平而非留一个超短尾段。',
        'owner': 'h3_prompt_kit.H3_SEGMENT_MIN_SEC',
    },
    'style_lora_min_strength': {
        'default': 0.6, 'type': 'float', 'range': (0.0, 1.0), 'group': 'H3 LoRA',
        'desc': '钳到该区间）；LLM 缺省或规则表兜底时取区间上限 0.8，避免默认 1.0 过冲。',
        'owner': 'h3_segment_loras.STYLE_LORA_MIN_STRENGTH',
    },
    'style_lora_max_strength': {
        'default': 0.8, 'type': 'float', 'range': (0.0, 1.0), 'group': 'H3 LoRA',
        'desc': '钳到该区间）；LLM 缺省或规则表兜底时取区间上限 0.8，避免默认 1.0 过冲。',
        'owner': 'h3_segment_loras.STYLE_LORA_MAX_STRENGTH',
    },
    'default_timeout': {
        'default': 240, 'type': 'int', 'range': (0, 100000), 'group': 'LLM',
        'desc': 'DEFAULT_TIMEOUT',
        'owner': 'llm_client.DEFAULT_TIMEOUT',
    },
    'timeout_max_attempts': {
        'default': 2, 'type': 'int', 'range': (0, 100), 'group': 'LLM',
        'desc': '故最多额外再给 1 次，总等待上界 2×20 分钟，不至于像旧逻辑那样 3 次打满。',
        'owner': 'llm_client.TIMEOUT_MAX_ATTEMPTS',
    },
    'min_tokens_when_thinking': {
        'default': 1024, 'type': 'int', 'range': (0, 200000), 'group': 'LLM',
        'desc': '允许思考时的最小 max_tokens（太小必然只剩思考、正文为空）',
        'owner': 'llm_client.MIN_TOKENS_WHEN_THINKING',
        'must_match': ['qc_client.MIN_TOKENS_WHEN_THINKING'],
    },
    'max_tail': {
        'default': 2000, 'type': 'int', 'range': (0, 10000000), 'group': '运维',
        'desc': 'MAX_TAIL',
        'owner': 'log_viewer.MAX_TAIL',
    },
    'h3_max_duration_sec': {
        'default': 15.0, 'type': 'float', 'range': (0.0, 100.0), 'group': 'LLM',
        'desc': ': H3 单次生成的硬能力（官方 README：单次最长 15 秒）',
        'owner': 'model_capabilities.H3_MAX_DURATION_SEC',
    },
    'shot_duration_floor': {
        'default': 1.0, 'type': 'float', 'range': (0.0, 1.0), 'group': 'LLM',
        'desc': ': 避免 import config 的重依赖；config 为唯一权威，此处仅作归一时的安全钳位）',
        'owner': 'model_capabilities.SHOT_DURATION_FLOOR',
    },
    'shot_duration_ceil': {
        'default': 12.0, 'type': 'float', 'range': (0.0, 100.0), 'group': 'LLM',
        'desc': ': 避免 import config 的重依赖；config 为唯一权威，此处仅作归一时的安全钳位）',
        'owner': 'model_capabilities.SHOT_DURATION_CEIL',
    },
    'max_chapters_kept': {
        'default': 3000, 'type': 'int', 'range': (0, 10000000), 'group': '小说解析',
        'desc': 'MAX_CHAPTERS_KEPT',
        'owner': 'novel_parser.MAX_CHAPTERS_KEPT',
    },
    'fallback_chapter_chars': {
        'default': 3000, 'type': 'int', 'range': (0, 10000000), 'group': '小说解析',
        'desc': '会导致「未识别到章节，无法自动分集生产」（缺陷 D3）与「待生产 0 集」（缺陷 D2）。',
        'owner': 'novel_parser.FALLBACK_CHAPTER_CHARS',
    },
    'shell_chapter_body_chars': {
        'default': 60, 'type': 'int', 'range': (0, 100000), 'group': '小说解析',
        'desc': '这类「只有标题行、正文近空」的壳章必须并入其后的真实章节，不能单独成章。',
        'owner': 'novel_parser.SHELL_CHAPTER_BODY_CHARS',
    },
    'chunk_chars': {
        'default': 2400, 'type': 'int', 'range': (0, 10000000), 'group': '剧本',
        'desc': '「预劈半是常态」的前提**已不成立**；`CHUNK_CHARS=2400` 本身保留不动。）',
        'owner': 'novel_to_script.CHUNK_CHARS',
    },
    'min_chunk_chars': {
        'default': 300, 'type': 'int', 'range': (0, 100000), 'group': '剧本',
        'desc': '「预劈半是常态」的前提**已不成立**；`CHUNK_CHARS=2400` 本身保留不动。）',
        'owner': 'novel_to_script.MIN_CHUNK_CHARS',
    },
    'max_shots_per_episode': {
        'default': 78, 'type': 'int', 'range': (0, 100000), 'group': '剧本',
        'desc': '本常量保留为**诊断参考线**（over_redline 告警仍在用），但**不再是任何截断依据**。',
        'owner': 'novel_to_script.MAX_SHOTS_PER_EPISODE',
    },
    'chapter_split_max_depth': {
        'default': 3, 'type': 'int', 'range': (0, 100), 'group': '剧本',
        'desc': '仍失败则把该块再二分（最多 CHAPTER_SPLIT_MAX_DEPTH 层）后分别生成再合并，保证不静默失败。',
        'owner': 'novel_to_script.CHAPTER_SPLIT_MAX_DEPTH',
    },
    'ref_insert_max_growth': {
        'default': 1.35, 'type': 'float', 'range': (0.0, 60.0), 'group': '剧本',
        'desc': ': 插入镜最多把镜头数放大到原来的多少倍（防镜数与总时长失控）',
        'owner': 'novel_to_script.REF_INSERT_MAX_GROWTH',
    },
    'ref_dialogue_min_part': {
        'default': 4, 'type': 'int', 'range': (0, 100), 'group': '剧本',
        'desc': ': 拆台词后每段的最小字数（低于此值不值得单独成镜，切了也是碎句）',
        'owner': 'novel_to_script.REF_DIALOGUE_MIN_PART',
    },
    'default_preview_scale': {
        'default': 0.5, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': 预演分辨率倍率（相对正式）。',
        'owner': 'preview_gate.DEFAULT_PREVIEW_SCALE',
    },
    'default_preview_segment_sec': {
        'default': 2.0, 'type': 'float', 'range': (0.0, 60.0), 'group': '质检',
        'desc': ': 预演每段时长上限（秒）。段数不变 → 每镜都看得到，总时长大幅缩短。',
        'owner': 'preview_gate.DEFAULT_PREVIEW_SEGMENT_SEC',
    },
    'min_preview_side': {
        'default': 256, 'type': 'int', 'range': (0, 100000), 'group': '质检',
        'desc': ': 预演画幅下限（避免缩到模型根本画不出结构）。',
        'owner': 'preview_gate.MIN_PREVIEW_SIDE',
    },
    'min_preview_segment_sec': {
        'default': 0.5, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': 预演每段时长的下限（与 h3_prompt_kit 的段时长下限同量级）。',
        'owner': 'preview_gate.MIN_PREVIEW_SEGMENT_SEC',
    },
    'desc_to_dialogue_ratio_max': {
        'default': 3.0, 'type': 'float', 'range': (0.0, 100.0), 'group': '质检',
        'desc': ': 与 novel_to_script.REWRITE_RULES 的「description+visual_detail ≤ dialog',
        'owner': 'qc_client.DESC_TO_DIALOGUE_RATIO_MAX',
    },
    'dialogue_shot_ratio_min': {
        'default': 0.4, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': 对白驱动率下限：有台词的镜头占比低于该值，说明剧情靠画面铺陈推进、易退化成「背景描述流水账」。',
        'owner': 'qc_client.DIALOGUE_SHOT_RATIO_MIN',
    },
    'locked_off_ratio_min': {
        'default': 0.6, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': ② 固定机位占比下限：92 镜参照 77% 固定，软告警阈值取 60%（克制口径，防运镜滥用）。',
        'owner': 'qc_client.LOCKED_OFF_RATIO_MIN',
    },
    'anchor_shot_ratio_max': {
        'default': 0.1, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': ② 特写+全景占比上限：参照 92 镜（特写 2 + 全景 3 = 5.4%），软告警阈值取 10%。',
        'owner': 'qc_client.ANCHOR_SHOT_RATIO_MAX',
    },
    'action_beats_max': {
        'default': 2, 'type': 'int', 'range': (0, 100), 'group': '质检',
        'desc': ': 1 个动作节拍；description 里可数出的动作节拍 ≥ 此值即视为「一镜多动作」。',
        'owner': 'qc_client.ACTION_BEATS_MAX',
    },
    'action_beats_shot_min': {
        'default': 3, 'type': 'int', 'range': (0, 100), 'group': '质检',
        'desc': ': ④ 触发阈值：出现「一镜多动作」的镜头数达到该值（或占比 > 20%）时提示，防单镜误伤。',
        'owner': 'qc_client.ACTION_BEATS_SHOT_MIN',
    },
    'main_shot_ratio_min': {
        'default': 0.55, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': ⑤ 主力景别（参考片 92 镜实测：近景 26% + 中近景 25% + 局部 25% ≈ 76%）。',
        'owner': 'qc_client.MAIN_SHOT_RATIO_MIN',
    },
    'local_shot_ratio_min': {
        'default': 0.1, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': ⑤「局部」插入镜下限：参考片 25%（只拍手部/道具、不出现完整人脸的插入镜）。',
        'owner': 'qc_client.LOCAL_SHOT_RATIO_MIN',
    },
    'shot_duration_tolerance': {
        'default': 0.5, 'type': 'float', 'range': (0.0, 1.0), 'group': '质检',
        'desc': ': 改 MIN/MAX 时**必须同批复核本值是否仍 < MIN**。',
        'owner': 'qc_client.SHOT_DURATION_TOLERANCE',
    },
    'episode_duration_tolerance': {
        'default': 10.0, 'type': 'float', 'range': (0.0, 100.0), 'group': '质检',
        'desc': ': 本容差因此只兜「四舍五入/台词长度微调」级别的小幅越界，不做百分比缩放。',
        'owner': 'qc_client.EPISODE_DURATION_TOLERANCE',
    },
    'episode_max_frames': {
        'default': 48, 'type': 'int', 'range': (0, 100000), 'group': '质检',
        'desc': ': 1~6 上限是**两回事**：后者只约束单镜/默认抽帧路径，整集路径不受它约束。',
        'owner': 'qc_coverage.EPISODE_MAX_FRAMES',
    },
    'default_desc_limit': {
        'default': 60, 'type': 'int', 'range': (0, 100000), 'group': '质检',
        'desc': ': 整集镜头摘要默认条数上限（原为 12，导致 40 镜整集中后段对模型完全不可见）。',
        'owner': 'qc_coverage.DEFAULT_DESC_LIMIT',
    },
    'min_occ_px': {
        'default': 3, 'type': 'int', 'range': (0, 100), 'group': '小说解析',
        'desc': ': 一列至少要有这么多「内容像素」才算被人物占用（滤掉零星噪声列）。',
        'owner': 'sheet_split.MIN_OCC_PX',
    },
    'min_gap_px': {
        'default': 5, 'type': 'int', 'range': (0, 100), 'group': '小说解析',
        'desc': ': 相邻人物之间的最小空隙列数，达到即认为可分。',
        'owner': 'sheet_split.MIN_GAP_PX',
    },
    'min_seg_w': {
        'default': 8, 'type': 'int', 'range': (0, 100), 'group': '小说解析',
        'desc': ': 切出的单段最小宽度，过窄视为误检。',
        'owner': 'sheet_split.MIN_SEG_W',
    },
    'video_megapixels_default': {
        'default': 0.5, 'type': 'float', 'range': (0.0, 1.0), 'group': '风格',
        'desc': ': 显式声明 9:16 时才会出现（本注释里 544×960 / 864×480 的尺寸即属此列）。',
        'owner': 'style_kit.VIDEO_MEGAPIXELS_DEFAULT',
    },
    'storyboard_megapixels_default': {
        'default': 1.5, 'type': 'float', 'range': (0.0, 60.0), 'group': '风格',
        'desc': ':    或临时 env 覆盖（无需改代码）。',
        'owner': 'style_kit.STORYBOARD_MEGAPIXELS_DEFAULT',
    },
    'asset_megapixels_default': {
        'default': 1.5, 'type': 'float', 'range': (0.0, 60.0), 'group': '风格',
        'desc': ': env MJSCXT_ASSET_MEGAPIXELS 可覆盖。',
        'owner': 'style_kit.ASSET_MEGAPIXELS_DEFAULT',
    },
    'default_ttl_sec': {
        'default': 900, 'type': 'int', 'range': (0, 100000), 'group': '任务台账',
        'desc': ': 默认租约存活时长（秒）。心跳停超过它就判 stale。',
        'owner': 'task_lease.DEFAULT_TTL_SEC',
    },
    'voice_bank_min_sec': {
        'default': 1.0, 'type': 'float', 'range': (0.0, 1.0), 'group': '音频',
        'desc': ': 参考音频允许的扩展名 / 时长建议区间（秒）',
        'owner': 'tts_client.VOICE_BANK_MIN_SEC',
    },
    'voice_bank_max_sec': {
        'default': 60.0, 'type': 'float', 'range': (0.0, 10000.0), 'group': '音频',
        'desc': ': 参考音频允许的扩展名 / 时长建议区间（秒）',
        'owner': 'tts_client.VOICE_BANK_MAX_SEC',
    },

    # ===== 2026-10-10 补登：孤儿检测发现的「可配置但漏登记」项 =====
    # 来源：按与生成时相同的规则重扫，找出「符合可配置特征但不在 REGISTRY」的常量。
    # 其中 DISABLE_THINKING_DEFAULT / MIN_TOKENS_WHEN_THINKING 都是**同一语义两处定义**
    # （llm_client 与 qc_client 各一份），正是用户担心的「改一处漏一处」场景，
    # 故用 must_match 显式声明，回写时会两处一起改。
    'disable_thinking_default': {
        'default': False, 'type': 'bool', 'range': None, 'group': 'LLM',
        'desc': '是否默认关闭模型思考（2026-09-17 起改为 False = 允许思考；'
                '关思考会让质检退化成直觉判断，漏掉明显问题）',
        'owner': 'llm_client.DISABLE_THINKING_DEFAULT',
        'must_match': ['qc_client.DISABLE_THINKING_DEFAULT'],
    },
    'shot_duration_silent': {
        'default': 0.6, 'type': 'float', 'range': (0.0, 60.0), 'group': '镜头',
        'desc': '无台词纯画面镜头的最短秒数',
        'owner': 'config.SHOT_DURATION_SILENT',
    },
    'chars_per_second': {
        'default': 4.5, 'type': 'float', 'range': (0.5, 30.0), 'group': '镜头',
        'desc': '中文配音语速（字/秒），用于台词时长折算',
        'owner': 'config.CHARS_PER_SECOND',
    },
    'beat_climax_bonus_sec': {
        'default': 0.7, 'type': 'float', 'range': (0.0, 30.0), 'group': '镜头',
        'desc': '「高潮」节拍镜的时长加成（秒）',
        'owner': 'config.BEAT_CLIMAX_BONUS_SEC',
    },
    'shot_granularity_target_sec': {
        'default': 5.5, 'type': 'float', 'range': (0.5, 120.0), 'group': '镜头',
        'desc': '每镜目标秒数（用户口径 5~6 秒）',
        'owner': 'config.SHOT_GRANULARITY_TARGET_SEC',
    },
    'chapter_dev_tolerance': {
        'default': 0.25, 'type': 'float', 'range': (0.0, 1.0), 'group': '连贯性',
        'desc': '章节锚定的字数偏差容差（0.25 = ±25%）',
        'owner': 'script_consistency.CHAPTER_DEV_TOLERANCE',
    },
    'element_coverage_min': {
        'default': 0.6, 'type': 'float', 'range': (0.0, 1.0), 'group': '连贯性',
        'desc': '要素覆盖率达标线（低于此值判未覆盖）',
        'owner': 'script_consistency.ELEMENT_COVERAGE_MIN',
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
    # 命名一致性（用户要求「相同字段统一名称」）：只报不改 —— 重命名常量要改所有
    # 引用点，风险大于收益；这里把混用清单显式列出来供逐步收敛。
    rep['naming'] = naming_report()
    for _f in (rep['naming'].get('families') or []):
        if _f.get('mixed'):
            rep['warnings'].append(
                '命名不统一【' + _f['family'] + '】前缀式 ' + str(_f['prefix_style']) +
                ' 个 / 后缀式 ' + str(_f['suffix_style']) + ' 个。建议：' + _f['suggested'])
    # 孤儿检测（2026-10-10 补强）：找出「该登记却没登记」的可配置常量 ——
    # 只做「登记 vs 实际值」检查发现不了它们，而未登记项被改动正是老问题重现的入口。
    rep['orphans'] = orphan_candidates()
    if rep['orphans'].get('count'):
        rep['warnings'].append(
            '发现 %s 个可配置常量未登记（改动它们不会被同步、也不会被体检发现）：%s'
            % (rep['orphans']['count'],
               ', '.join(x['module'] + '.' + x['name']
                         for x in rep['orphans']['items'][:8])))
    rep['prompts'] = prompt_fingerprints()
    for m in (rep['prompts'].get('mismatch') or []):
        rep['ok'] = False
        rep['warnings'].append('提示词 ' + m['name'] + '：外部文件(' + str(m['ext_len'])
                               + '字/' + m['ext_sha1'] + ') 与代码兜底(' + str(m['code_len'])
                               + '字/' + m['code_sha1'] + ') 不一致')
    if any(not r.get('match') for r in rep['params']):
        rep['ok'] = False
    return rep



# ===================== 同族命名一致性（2026-10-10 用户要求「相同字段统一名称」）=====================
# 背景：同一语义在不同模块可能用了不同命名风格 —— 开关既有 ENABLE_XXX 前缀，也有 XXX_ENABLED
# 后缀；MAX 既有 MAX_ 前缀（32 个）也有 _MAX 后缀（12 个）。这类不一致**无法靠值比对发现**
# （不同模块的值本来就可以不同），必须靠命名规则检查。
#
# 为什么只报不改：重命名常量要改掉所有引用点，风险远大于收益。这里把混用清单显式列出来
# （供逐步收敛），并把「建议的统一风格」写死在规则里，避免每次讨论重复判断。
FAMILY_RULES = (
    ('开关', r'^(ENABLE_|DISABLE_)|(_ENABLED$|_DISABLED$)',
     '统一为 ENABLE_<特性> / DISABLE_<特性> 前缀式；现有 <特性>_ENABLED 后缀式为历史遗留'),
    ('最大值', r'^(MAX_|MAXIMUM_)|(_MAX$)',
     '统一为 MAX_<对象> 前缀式；现有 <对象>_MAX 后缀式为历史遗留'),
    ('最小值', r'^(MIN_|MINIMUM_)|(_MIN$)',
     '统一为 MIN_<对象> 前缀式；现有 <对象>_MIN 后缀式为历史遗留'),
    ('秒数', r'_SEC$|_SECONDS$|_SEC_', '统一为 _SEC 后缀（已基本一致）'),
    ('字符数', r'_CHARS$|_CHARS_|CHARS_PER_', '统一为 _CHARS / CHARS_PER_（已基本一致）'),
    ('比率', r'_RATIO$|_RATE$|_PERCENT$', '统一为 _RATIO 后缀（已基本一致）'),
)


def naming_report() -> Dict[str, Any]:
    """扫描全部大写常量的命名一致性，返回各族的风格分布与混用清单（只读、绝不抛）。"""
    import re as _re
    base = os.path.dirname(os.path.abspath(__file__))
    pat = _re.compile(r'^([A-Z][A-Z0-9_]{2,})\s*(?::\s*[^=]+)?=\s*(.+)$')
    names: List[Dict[str, str]] = []
    try:
        for fn in sorted(os.listdir(base)):
            if not fn.endswith('.py') or fn.startswith('_'):
                continue
            try:
                with open(os.path.join(base, fn), encoding='utf-8') as _fh:
                    src = _fh.read()
            except Exception:  # noqa: BLE001
                continue
            for ln in src.split(chr(10)):
                m = pat.match(ln)
                if m and not m.group(1).startswith('_'):
                    names.append({'module': fn[:-3], 'name': m.group(1)})
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)[:200]}
    out: Dict[str, Any] = {'ok': True, 'total': len(names), 'families': [], 'mixed_families': []}
    for label, rx, style in FAMILY_RULES:
        hit = [n for n in names if _re.search(rx, n['name'])]
        if len(hit) < 2:
            continue
        pre = [n['name'] for n in hit
               if _re.match(r'^(ENABLE_|DISABLE_|MAX_|MIN_|MAXIMUM_|MINIMUM_)', n['name'])]
        post = [n['name'] for n in hit
                if _re.search(r'(_ENABLED|_DISABLED|_MAX|_MIN)$', n['name'])]
        mixed = bool(pre) and bool(post)
        out['families'].append({'family': label, 'count': len(hit), 'suggested': style,
                                'prefix_style': len(pre), 'suffix_style': len(post),
                                'mixed': mixed,
                                'examples_prefix': pre[:4], 'examples_suffix': post[:4]})
        if mixed:
            out['mixed_families'].append(label)
    return out




def orphan_candidates() -> Dict[str, Any]:
    """找出「符合可配置特征、但没在 REGISTRY 登记」的常量（2026-10-10 补强）。

    为什么需要：此前只有「登记 vs 实际值」检查，能发现「登记了但值不对」，
    却**发现不了「该登记却没登记」** —— 而未登记的常量被改动时，正是
    「改一处漏一处」重新出现的入口（实测查出 10 个，其中 DISABLE_THINKING_DEFAULT、
    MIN_TOKENS_WHEN_THINKING 都是同一语义两处定义）。规则与当初生成 REGISTRY 时一致。
    只报不改。
    """
    import re as _re
    base = os.path.dirname(os.path.abspath(__file__))
    pat = _re.compile(r'^([A-Z][A-Z0-9_]{2,})\s*(?::\s*[^=]+)?=\s*(.+)$')
    cfg = ('THRESHOLD', 'RATIO', 'RATE', 'ROUNDS', 'RETRIES', 'RETRY', 'MAX_', 'MIN_',
           'LIMIT', 'BUDGET', 'ENABLED', 'ENABLE_', 'DISABLE', 'TIMEOUT', 'SECONDS', '_SEC',
           'CHARS', 'SHOTS', 'TOKENS', 'SCORE', 'PASS_', 'WEIGHT', 'SIZE', 'COUNT',
           'DURATION', '_MAX', '_MIN', 'TOLERANCE', 'GAP', 'SCALE', 'FACTOR', 'MODE',
           'LEVEL', 'PERCENT')
    impl = ('_RE', '_PATTERN', 'REGEX', '_REGEX', 'URL', 'PATH', '_DIR', 'DIR_', 'EXT',
            'EXTENSION', '_SUFFIX', '_PREFIX', 'PROMPT', 'TEMPLATE', '_TXT', 'MARKER',
            'SENTINEL', 'KEY', 'HEADER', 'MIME', '_VERSION_', 'SCHEMA', 'TABLE', 'COLUMN',
            'ENUM', '_NAMES', 'ALIASES', 'MAP', '_TYPES', '_VALUES', '_LIST', '_SET')
    owners = set()
    for _k, _v in REGISTRY.items():
        owners.add(str(_v.get('owner') or ''))
        for _m in (_v.get('must_match') or []):
            owners.add(str(_m))
    out = []
    try:
        for fn in sorted(os.listdir(base)):
            if not fn.endswith('.py') or fn.startswith('_'):
                continue
            try:
                with open(os.path.join(base, fn), encoding='utf-8') as _fh:
                    src = _fh.read()
            except Exception:  # noqa: BLE001
                continue
            for ln in src.split(chr(10)):
                m = pat.match(ln)
                if not m:
                    continue
                name, val = m.group(1), m.group(2).strip()
                if name.startswith('_') or len(name) < 4:
                    continue
                t = 'str'
                if _re.match(r'^(True|False)$', val):
                    t = 'bool'
                elif _re.match(r'^-?\d+$', val):
                    t = 'int'
                elif _re.match(r'^-?\d+\.\d+', val):
                    t = 'float'
                if t not in ('int', 'float', 'bool'):
                    continue
                if any(k in name.upper() for k in impl):
                    continue
                if not any(k in name.upper() for k in cfg):
                    continue
                dotted = fn[:-3] + '.' + name
                if dotted in owners:
                    continue
                out.append({'module': fn[:-3], 'name': name, 'type': t, 'value': val[:30]})
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)[:200]}
    return {'ok': True, 'count': len(out), 'items': out}


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
