"""
ComfyUI API 客户端（v2 / 2026-09 修复版）
- 角色：Qwen 2512 生成基础图 → Qwen Edit 2511 生成多视图（正面/左侧/右侧/背面）
- 物品：Qwen 2512 生成基础图 → Qwen Edit 2511 生成 3D 多视角（正面/左45°/右45°/俯视）
- 场景：Qwen 2512 生成基础图 → Qwen Edit 2511 生成 3D 多视角（正面/左45°/右45°/俯视）
- 视频：MiniMax H3（Ref2VA）按分镜段数动态生成（一个分镜一段）

v2 关键修复：
1. UI(node-graph) → API 转换改为「节点自带 widgets_values_named 优先」，
   仅在缺失时按 object_info 声明的控件顺序做位置兜底（并剔除 control_after_generate
   等前端伪控件），彻底解决 KSampler 参数错位（steps='randomize'）。
2. 展开 ComfyUI 新版 subgraph（UUID 型 class_type 实例）：把子图内部真实节点
   抽取为 API 节点，并把 SetNode/GetNode/Reroute 走通为「值来源重定向」。
3. 过滤 MarkdownNote / Note / Label / Reroute / PrimitiveNode 等虚拟节点。
4. 多视角：3 个 LoadImageOutput 参考图全部替换（原实现只改第一个）。
5. 视频：前端 HTTP 资源路径解析为本地绝对路径；H3 的 Text Multiline 分镜段
   按分镜段数动态全部替换（原实现只改第一段），并按 clip 序号一一对应。
"""
import os
import re
import json
import time
import copy
import shutil
import random
import hashlib
import logging
import threading
import requests
import cancellation  # S9：远端任务取消（中止信号贯穿 ComfyUI 轮询，与 pipeline/llm_client 同一套）
import comfyui_job_store as job_store  # 崩溃免重渲检查点（2026-09-29：台账复用 + 重连，不重复提交）
import asset_library  # 跨项目角色资产库（2026-09-29：形象指纹命中即零渲染复用）
from typing import Dict, List, Optional, Any, Tuple, Sequence
# ⚠️ Sequence 曾被漏导入：类级注解 `_LIGHT_KEYWORDS: Sequence[...]` 在类创建时**不求值**，
# 所以模块照常导入、py_compile 也通过，但一旦有工具读取
# `ComfyUIClient.__annotations__` 或调用 `typing.get_type_hints()` 就会抛
# NameError: name 'Sequence' is not defined（实测）。别删这个导入。

from config import (
    COMFYUI_URL, COMFYUI_OUTPUT_DIR, COMFYUI_TEMP_DIR, MJSCXT_COMFYUI_DIR,
    resolve_workflow_path,
    PROJECT_OUTPUT_DIR, WORKFLOW_TEMPLATE, MULTIVIEW_CONFIG,
    SCENE_VIEW_ANGLE_ZH, SCENE_VIEW_KEYS, SCENE_VIEW_LABELS,
    H3_EMIT_AUDIO,
    CONFLICT_NEGATIVE_TOKENS,
    ENABLE_BLOCKING_ANNOTATION,
    H3_ENABLE_REFINE,
    H3_COMMON_NOTE_MODE,
    COMFYUI_INPUT_DIR,
    # 景别唯一权威表（分镜构图规范 / 解析顺序都必须由它派生，见 SHOT_CAMERA_SPECS 上方注释）
    SHOT_TYPES,
)
# 模板路径一律走 resolve_workflow_path()（项目内优先，回落 ComfyUI 目录）。
# COMFYUI_WORKFLOWS_DIR 在本模块内已不再直接使用，但**必须保留为模块属性**：
# .workbuddy/test 下的守卫（如 verify_style_injection.py）会通过
# `comfyui_client.COMFYUI_WORKFLOWS_DIR` 取用，删掉会让那些守卫 AttributeError。
from config import COMFYUI_WORKFLOWS_DIR  # noqa: F401
from dialogue_utils import (dialogue_text as _dlg_text, format_line as _dlg_line,
                            dialogue_speaker as _dlg_speaker)
from h3_episode_builder import H3EpisodeBuilder
import h3_director_builder
import comfyui_models
import style_kit
import h3_prompt_kit
# 提示词模板中心（2026-10-07）：九宫格「逐格写死」中文版主模板（storyboard_grid_main）从
# 这里加载（该模块只依赖标准库 + 延迟 import config，与本模块无循环依赖）。
import prompt_templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# D-3（2026-10-16）：模块级工具下沉到 comfyui_shared（叶子），此处**逐名再导出**。
#   不用 `import *`：那会让 pyflakes 无法检查本模块的未定义名（守卫会瞎）。
from comfyui_shared import (
    ANNOTATED_DIR, BLOCKING_REF_MARK, CAMERA_UNSPECIFIED_SPEC, CHARACTER_QTY_RE, CHARACTER_WORDS, CINEMATOGRAPHY_SECTION, COMBINED_NEGATIVE_FIELD, COMBINED_POSITIVE_FIELD, COMBINED_PROMPT_NODE_TYPES, COMPOSITION_BASELINE_SECTION, CONFLICT_NEGATIVE_SORTED, DYNAMIC_INPUT_TYPES, H3_CACHE_NAME_MAX_LEN, IDENTITY_BASELINE_GRID_SECTION, IDENTITY_GRID_REF_MARK, INPUT_LOAD_CLASSES, MEDIA_INPUT_KEYS, NEGATIVE_HINTS, OUTPUT_LOAD_CLASSES, PROMPT_ENHANCER_CLASS, PROMPT_ENHANCER_ENV, PROMPT_ENHANCER_IMAGE_FIELDS, PROMPT_ENHANCER_LOCAL_DEFAULTS, PROMPT_ENHANCER_MODE_I2I, PROMPT_ENHANCER_MODE_T2I, PROMPT_ENHANCER_PROMPT_FIELD, PROMPT_NODE_TYPES, PROMPT_TEXT_FIELDS, PSEUDO_VALUES, PSEUDO_WIDGETS, SCENE_EXACT_TEXT_MARKER, SCENE_NO_CHARACTER_SUFFIX, SCENE_RELATION_REF_HINT, SHOT_ACTION_SUFFIX, SHOT_CAMERA_SPECS, SUFFIX_RE, TEXT_HINT_WORDS, TEXT_SECTION_ZH, VIRTUAL_NODE_TYPES, _CALL_STATS, _CALL_STATS_LOCK, _CAMERA_ANGLE_ALIASES, _CAMERA_ANGLE_ORDER, _CAMERA_ANGLE_SPECS, _CAMERA_KEY_ORDER, _CROWD_MEMBER_VARIANTS, _H3_SEG_COST_DEFAULT_SEC, _H3_TIMING_FILE, _IDENTITY_GRID_SHAPE_LOOSE_RE, _IDENTITY_GRID_SHAPE_RE, _IMAGE_SLOT_RE, _NEG_CONSTRAINT_RE, _PROMPT_SPLIT_RE, _REFMOD_NODE_RE, _REF_IMAGE_TAG_RE, _REF_LABEL_PREFIX_RE, _REF_ROLE_ZH2EN, _SCENE_GRID_MAIN_FALLBACK, _bump, _compact_refmod_spec, _crowd_member_descriptions, _dlg_line, _dlg_speaker, _dlg_text, _enhancer_text_credentials, _env_float, _env_int, _h3_record_seg_cost, _h3_seg_cost_seconds, _h3_timeout_seconds, _is_dynamic_child, _is_negative_slot, _iter_prompt_slots, _linked_enhancer_text, _node_sort_key, _parse_identity_grid_layout, _prompt_enhancer_node_enabled, _prompt_slot_polarity, _ref_label_body, _ref_label_purpose, _slot_index, _strip_sheet_phrases, camera_angle, camera_key, camera_spec, get_call_stats, h3_cache_name, identity_baseline_grid_section, logger, probe_refmod_nodes, reset_call_stats, scene_view_prompt_suffix, shot_framing, shot_motion,
)  # noqa: F401

from comfyui_workflow_mixin import _WorkflowMixin  # D-3：工作流层 mixin
from comfyui_transport_mixin import _TransportMixin  # D-3：运输/提交/轮询层
from comfyui_nodes_mixin import _NodesMixin          # D-3：节点/增强器操作层
from comfyui_prompt_mixin import _PromptMixin        # D-3：提示词构建层
from comfyui_asset_mixin import _AssetMixin          # D-3：资产出图层
from comfyui_h3_mixin import _H3Mixin                # D-3：H3 视频生成层
class ComfyUIClient(_TransportMixin, _NodesMixin, _PromptMixin, _AssetMixin, _H3Mixin, _WorkflowMixin):
    """ComfyUI API 客户端"""

    def __init__(self, base_url: str = COMFYUI_URL):
        self.base_url = base_url.rstrip("/")
        self.client_id = self._generate_client_id()
        self._object_info = None
        self._object_info_ts = 0.0
        self.last_convert_meta: Dict[str, Any] = {}

    # ===================== 自愈重启（2026-10-08：分镜跑着 ComfyUI 被全局中断后 8188 连接被拒）=====================
    # ComfyUI 在「后端超时→全局 /interrupt」后可能进入半死状态（webserver 仍监听但执行器被打断），
    # 后续镜头 /upload/image 全部 10061 拒连。提供「重启 ComfyUI 进程并等它重新就绪」的能力，
    # 让上层（分镜 worker / 资产 worker）在连续拒连时能自愈，而非让整集镜头全挂。






    # ===================== 基础 HTTP =====================







    # ===================== 工作流加载与转换 =====================

    # ⭐⭐ 2026-10-12：接入插件新增的 cache_name（MiniMaxH3Director 面板「性能」组）。
    #
    # ## 为什么需要
    # 插件旧版的分段缓存目录是 minimax_seg_cache / <node_id> —— **按节点 id**。
    # 我们的工作流里 Director 节点 id 恒为同一个（实测 12），于是：
    #   · 第 1 场 28 段写进 12/seg_0000..seg_0027；
    #   · 第 2 场又从 seg_0000 开始写 → **覆盖第 1 场的段**。
    # 表现为「段号莫名回到 0」「后台预构建结果互相覆盖」，与那次 OOM 后的
    # 「Segment cache pruned 224 stale file(s) for node 12」是同一条链路。
    #
    # 插件 2026-10-12 更新后提供 resolve_segment_cache_key(cache_name, node_id)：
    #   有 cache_name → <name>_<node_id>；为空 → 退回 <node_id>（旧行为）。
    # 所以**只要填 cache_name 就能分目录**，不必等插件改默认值。
    #
    # ## 命名口径
    # <项目>__ep<集号>__s<场次>：同一场次重跑复用同一目录（断点续跑的收益保住），
    # 不同场次/不同集彼此隔离（不再互相覆盖）。非法字符由插件侧
    # normalize_cache_name 统一清洗，这里只管取一个人类可读的名字。

    # ===================== 分镜工作流「代码生成」（2026-10-15 用户口径） =====================
    # 背景：分镜工作流原先依赖手工维护的 JSON 模板（分镜生成_Qwen21.json，从早期参考
    # 工作流演进而来），**固定 9 个参考图槽位**，每次改结构都要手工编辑 JSON ——
    # 实测因此踩过两次坑（KSampler 参数因 widgets_values 顺序错位、节点数量与实参不符）。
    # 用户明确要求：「你就不能直接用代码生成 json，控制拼接成工作流吗」。
    # 本函数即为按需生成：**槽位数量由 ref_count 决定**，没有固定 9 槽、也没有尾部清理。








    # ---------- 核心转换 ----------



    # ---------- 转换结果自检 ----------


    # ===================== 资源与队列 =====================


    # ===================== TE_MAN 提示词增强节点运行时注入（2026-10-06） =====================
    #
    # 给全部「图片生成」提交路径统一挂 TE_MAN 的 ``TE_Qwen_Image_2_1_Prompt_Enhancer``：
    # 正向编码节点（TextEncodeQwenImage21）的 prompt 改接增强节点输出 0，原始提示词
    # 全文交给它先用「AI 设置 · 文本分析模型」增强一遍再编码。**只改本次提交的
    # API prompt（内存对象），不修改磁盘上的工作流文件。**
    #
    # 为什么挂在 queue_prompt：本类所有图片提交（generate_storyboard /
    # _generate_base_image / _run_multiview_workflow）都在这里收口，且此刻
    # prompt_qc.preflight、风格拼接、clean_conflict_negative_tokens 等文本工序
    # 已全部完成 —— 注入的必然是**最终文本**。H3 视频走 submit_resumable →
    # queue_prompt，但其工作流不含 TextEncodeQwenImage21，被适用判定天然排除。
    #
    # fail-open 纪律（与 prompt_enhance 同口径）：开关关闭 / 不含编码节点 / 已注入 /
    # 文本模块凭据不全 / object_info 查不到该节点 / 必填输入对不上 / 任何异常 ——
    # 一律跳过注入，按原提示词原样提交，只打日志，绝不阻断生成。

















    # ---------- 路径工具（P0-4：HTTP 资源路径 → 本地绝对路径） ----------


    # ---------- 目录标注工具（P0-5：按节点类型区分 [output] / 无标注） ----------


    # ===================== 第一阶段：基础图生成 =====================

    # ---------- P0 修复辅助：场景去人 / 冲突负向词清理 ----------


    #: ⚠️ 2026-10-14 新增：提示词里的「无X / 没有X」类**否定短语**——图像模型会把它画成字。
    #:    实测：物品「筑基丹」的正向提示词含「边缘圆润**无**棱角」「构图稳定，**无**畸形」，
    #:    出图在画面正中画了一个**大号汉字「无」**（三个物品资产全部中招，用户报障）。
    #:    根因：扩散模型不擅长理解否定语义 —— 提示词**只能写「要什么」，不能写「不要什么」**。
    #: ⚠️ 二次修正（2026-10-14）：否定词可以**紧跟在汉字之后**（「边缘圆润**无**棱角」），
    #:    所以不能要求前面必须是标点 —— 初版要求 `^` 或标点前缀，实测漏掉了最主要的那个
    #:    「无棱角」。现改为**只吸收否定词本身 + 其后 1~6 字**，不吞前面的正向描述。
    _NEG_PHRASE_RE = re.compile(
        r"(?:无|没有|不带|不含|不得)[\u4e00-\u9fa5A-Za-z0-9]{1,6}")








    #: 角色设定图的**中文标注四区 character sheet 版式**（2026-10-03 用户指定模板）。
    #:
    #: 四区布局（Top / Left / Bottom / Right）：
    #:   · 顶部主视觉区：正面/侧面/背面三视图
    #:   · 左侧补充信息区：面部特写 + 配色板（标注毛发色、服饰色）
    #:   · 底部局部细节区：配饰/点缀/关键身份识别元素拆解
    #:   · 右侧全身比例区：身高比例参考
    #:
    #: ⚠️ 与 2026-10-02 英文版式的两点关键差异：
    #:   ① **画面必须渲染明确中文说明文字**（"三视图设定/正面/侧面/…" + 角色特征
    #:      标签）—— 旧英文版式结尾的 "no text" 已删除，那是反向约束；
    #:   ② 特征标签**不写死**：`_extract_trait_labels` 从角色设定文本自动提取
    #:      （如「冰蓝色眼眸，黑发微乱，哑光黑短外套…」→ 逐条标签），换角色不用改模板。
    #:
    #: ⚠️ 版式仍是**四区不对称布局**，无法做行列投影切分 → 沿用「角色图不裁剪」
    #:    配套（`_pick_char_view` 取整图 base.png、`_generate_asset_task` 角色分支
    #:    不切分）。改版式时两者必须同批考虑。
    #:
    #: ⚠️ 幂等：marker 子串 = "人物设定表"（本串字面子串，见 2026-09-25 教训：
    #:    marker 必须是 suffix 字面子串）。质检对「画面内容文字」的口径（qc_client
    #:    WATERMARK_EXEMPT_NOTE 文字段）已明确「内容需要的文字不算违规」，设定图
    #:    带中文标注不会被质检误杀。
    _CHARACTER_SHEET_CN_LAYOUT = (
        "角色设定三视图，人物设定表，白色背景，清晰中文文字排版，"
        "画面上必须出现明确中文说明文字，文字干净可读。"
        "顶部主视觉区：正面、侧面、背面三视图，展示角色整体身形、服装搭配和标志性特征。"
        "左侧补充信息区：面部特写、配色板，标注毛发色、服饰色。"
        "底部局部细节区：配饰、点缀、关键身份识别元素拆解展示。"
        "右侧全身比例区：人物身高比例参考图。"
        "画面文字内容必须清晰显示：“三视图设定”“正面”“侧面”“背面”“面部特写”"
        "“配色板”“局部细节”“身高比例”{labels_part}。"
        "同一角色在所有视图与特写中必须完全一致（五官/发型/服装/配色/体型）。"
        "布料褶皱自然，自然光照，高清纹理，细节丰富，纯白背景，无场景无道具。"
    )




    #: ⭐⭐ 2026-10-12 用户口径：「角色是群像、后续剧情也与他无关时**不用生成三视图**，
    #:    直接生成一张多人图就行了」。
    #:
    #:    为什么这样更好（不只是省成本）：
    #:      · 省一次完整的三视图设定图生成（四区版式比单人立绘更贵）；
    #:      · **画风天然统一** —— 一张图里所有人一起生成，光影/色调/画风不会各画各的，
    #:        正好满足「所有角色与主角画风一致」的要求；
    #:      · 这张合影本身就是这个群像的「定妆照」，以后群像镜头直接拿它当参考图。
    #:
    #:    ⚠️ 使用前提（对应 asset_worker._character_tier 的判定口径）：
    #:      只有**确认后续不会有单人特写 / 台词**的角色才走这条路径。
    #:      判据来自已落盘剧本：有台词、或镜头数达标的，一律仍走四区三视图。
    #:
    #:    ⚠️ 幂等：marker = "群像合影"（suffix 字面子串）。
    # ⚠️ 必须写成**隐式拼接的单个字符串**（相邻字面量之间不能有逗号）——
    #    2026-10-12 实测踩坑：写成带逗号的括号序列会变成 tuple，
    #    后续 .replace() 直接 AttributeError: tuple has no attribute replace。
    _CROWD_SHEET_CN_LAYOUT = (
        "群像合影，人物群像设定图，多人同框，同一画面内并列展示{count}位人物："
        "{names_part}，白色背景，清晰中文文字排版标注每位人物的姓名，"
        "所有人物处于**同一光线、同一色调、同一画风**下（这是本图的核心要求），"
        "全身可见，人物之间保持自然间距、不互相遮挡面部，"
        "严禁把任何一个人画成单独的三视图或四区设定表（本图只要一张多人合影）"
    )

    #: ⭐⭐ 2026-10-12 四档成本分级里「主要配角」的版式：**双格设定图**（正面 + 侧面）。
    #:    比四区 sheet 省掉「面部特写+配色板」「局部细节」「身高比例」三区 ——
    #:    主要配角经常单独出镜、需要正侧两面的形体一致性，但用不到设定集级别的信息量。
    #:    marker = "角色设定双视图"。
    _PAIR_SHEET_CN_LAYOUT = (
        "角色设定双视图，人物设定表，白色背景，清晰中文文字排版，"
        "画面左右分两格：左格「正面」全身立绘，右格「侧面」全身立绘。"
        "两格必须是同一角色（五官/发型/服装/配色/体型完全一致）。"
        "画面文字内容必须清晰显示：「正面」「侧面」{labels_part}。"
        "构图左右对称、人物等高等大，纯白背景，无场景无道具，自然光照，高清纹理。"
    )

    #: ⭐⭐ 同分级里「次要角色」的版式：**单张正面全身立绘**（只露一两次脸，够用）。
    #:    marker = "单人全身立绘"。
    _SOLO_CN_LAYOUT = (
        "单人全身立绘，人物设定图，白色背景，清晰中文文字排版，"
        "正面朝向镜头，自然站姿，全身完整可见（头顶到脚底都不出画），"
        "画面文字内容必须清晰显示姓名与身份{labels_part}。"
        "纯白背景，无场景无道具，自然光照，高清纹理，细节丰富。"
    )




    #: 特征标签提取的**停用词**：命中即不作为设定图上的标注文字
    #: （这些是版式/质检/管线层的元词，不是角色特征；混进去会变成图上的乱注记）。
    _TRAIT_LABEL_STOPWORDS = (
        "设定", "锁定", "严格", "三视图", "参考图", "提示词", "全身", "半身",
        "构图", "背景", "高清", "细节", "一致", "动画", "渲染", "风格", "画质",
        "三维", "多视角", "正交", "比例尺", "标注", "文字", "品质", "质量",
        "cinematic", "character", "sheet", "view", "texture", "lighting",
    )


    #: 物品参考图里**不该出现**的承托物/位置描述。
    #: 实测（2026-10-01）：筑基丹的 reference_prompt_zh 被剧本 LLM 写成
    #: 「…，置于黑色玉盒中，玉盒有磨损痕迹。」→ 出图照着画成「黑碗盛丹药」，
    #: 而质检拿「图 vs 提示词」比对，图与提示词完全一致 → **判通过**。
    #: 物品参考图的职责只是「本体长什么样」，容器/承托物/摆放位置属于分镜内容，
    #: 一旦画进参考图，就会被当成该物品的规范外观带进后面每一镜。
    _ITEM_PLACEMENT_RE = re.compile(
        r"[，,；;]?\s*(?:被)?(?:置于|放在|摆放于|装于|盛于|收纳于|存放于|陈列于|托在|捧在)"
        r"[^，。；;]*")







    # ===================== 第二阶段：多视角生成 =====================




    # ===================== 第三阶段：分镜图生成（Qwen Edit 2511） =====================



    # 画面光源/时间要素 → 分镜图光影引导（从镜头描述/风格里抽取）
    # 历史缺陷：描述里的「黄昏/阴雨/烛光/月光」等光影要素没有独立成句，模型容易忽略，
    # 导致分镜图与视频在光源上不一致（视频有日落、分镜图却是正午平光）。
    _LIGHT_KEYWORDS: Sequence[Tuple[str, str]] = (
        ("黄昏", "黄昏暖调逆光，长影拉长，天空带橙色到紫色的渐变"),
        ("傍晚", "傍晚蓝调过渡光，暖色点光源，整体氛围静谧"),
        ("清晨", "清晨低角度柔光，冷色空气透视，露珠微光"),
        ("黎明", "黎明前冷蓝色调，地平线微光，薄雾弥漫"),
        ("正午", "正午顶光，高对比度，阴影短而清晰"),
        ("午夜", "午夜深蓝冷调，月光为主光源，高反差明暗"),
        ("夜晚", "夜晚冷蓝月光，点光源（灯笼/烛火/灯光）与大面积暗部对比"),
        ("深夜", "深夜冷色调，微弱月光，暗部深沉"),
        ("雨夜", "雨夜冷蓝调，湿润反光，光源被雨幕柔化"),
        ("雨天", "阴雨天漫射光，低对比度，天空阴沉，地面反光"),
        ("阴天", "阴天漫射柔光，低对比度，色调偏冷"),
        ("雪天", "雪天高亮漫射光，蓝白冷调，雪地反光强烈"),
        ("雪夜", "雪夜冷蓝调，雪面反光与暗部高反差"),
        ("雾天", "雾天漫射光，空间透视感强，远景模糊"),
        ("云雾", "云雾缭绕的漫射光，空气透视，光柱穿透"),
        ("烛光", "烛火暖光为主光源，暖黄光晕，暗部偏冷形成对比"),
        ("烛火", "烛火暖光为主光源，暖黄光晕，暗部偏冷形成对比"),
        ("火光", "火光照明的暖橙色调，明暗对比强烈，火苗跳动"),
        ("灯笼", "灯笼暖光点缀，暖红与冷夜蓝形成色彩对比"),
        ("月光", "清冷月光为主光源，银蓝色调，轮廓光清晰"),
        ("油灯", "油灯暖光，低照度，柔和暖黄与深褐暗部"),
        ("晨光", "晨光低角度暖调，长影与空气透视"),
        ("夕照", "夕照暖红金调，逆光剪影，天空燃烧感"),
        ("窗光", "窗户透入的定向侧光，光斑与明暗分割线清晰"),
        ("闪电", "闪电冷白瞬间光，高反差，明暗交替"),
    )
    # 镜头描述里没出现光源词时，按情绪给一个中性光影基线（避免模型自由发挥）
    _LIGHT_EMOTION_FALLBACK: Sequence[Tuple[str, str]] = (
        ("阴郁", "冷调低饱和光，阴影浓重"),
        ("悲伤", "冷灰漫射光，低反差，情绪压抑"),
        ("恐惧", "冷蓝硬光，明暗撕裂，光源方向明确"),
        ("紧张", "高对比硬光，光源方向清晰"),
        ("温暖", "暖色柔光，低反差，光线柔和"),
        ("平静", "自然柔光，明暗过渡自然，光线均匀"),
    )






    #: ⭐⭐ 2026-10-11：画面描述里的「文字元描述词」→ 换成不指向具体字形的说法。
    #:   用户实测：「手机通知文字有错字（约 32s，f_005）：『发件人乱乱码加陌生公司域名』
    #:   —— 多了一个『乱』字」。
    #:   根因：剧本 description 写的是「发件人乱码加陌生公司域名」，这是在**描述
    #:   「这里有一串不可读的字」这件事**，而扩散模型会把「乱码」当**要写的字**画出来。
    #:   与「原文/文字/字样/某某/XX/TBD」是同一类坑（那条黑名单此前只覆盖了资产侧）。
    #:   替换成「不可辨认的字符」：语义不变，但不含任何可被逐字绘制的字形。
    #: ⭐ 2026-10-11 系统排查补全：除「乱码」外，还有 5 类同性质的元描述词在剧本里，
    #:   它们描述的都是「这里有没有字/字能不能读」，而非「要写哪几个字」，
    #:   同一批坑（模型会把它们当字画出来）。命中统计：
    #:     符号 x3 [49,50,58]  字符 x2 [49,58]  数字 x2 [49,58]
    #:     不可读 x2 [2,4]     小字 x2 [13,23]  字迹 x2 [23,35]  无法辨认 x1 [50]
    #:   ⚠️ 只替换**描述用的**说法，替换后语义不变、且不含可被逐字绘制的字形；
    #:     顺序敏感：长词在前，避免「乱乱码」被「乱码」先命中而留下残余。
    _TEXT_META_DESC = (
        ("乱乱码", "不可辨认的字符"),
        ("字迹潦草无法辨认", "笔迹潦草难辨"),
        ("无法辨认", "难以辨清"),
        ("不可读", "读不出内容"),
        ("乱码", "不可辨认的字符"),
        ("字样", "字面样式"),
        ("字迹", "笔迹"),
        ("小字", "细小的印刷体"),
        ("符号", "标记"),
        ("字符", "字形"),
        ("数字", "数码"),
    )

    #: ⭐⭐ 2026-10-11：色差澄清表。用户实测「上一格纸上还没洞，下一格突然出现**带血洞**」
    #:   —— 剧本写的其实是「红色圆珠笔批注…末笔戳破纸面留小洞」，全片 0 镜提到「血」；
    #:   是 H3 把「红色圆珠笔」的红色理解成了血迹。
    #:   这类「颜色 + 载体」的歧义（红笔/红墨水 vs 血、红漆 vs 血）在扩散模型上反复出现，
    #:   所以在提示词层直接跟一句「是墨迹不是血」把它钉死；只在该词确实出现时才加，
    #:   不给无关镜头引入新概念。
    _INK_NOT_BLOOD = (
        ("红色圆珠笔", "红色圆珠笔（普通墨水，绝非血迹）"),
        ("红笔", "红笔（普通墨水，绝非血迹）"),
        ("红色墨水", "红色墨水（普通墨水，绝非血迹）"),
    )




    # ===================== H3 音轨控制（生成阶段不出声，配音统一交给 QwenTTS） =====================

    # 末端封装/保存节点：只有这些节点决定最终落盘文件是否带音轨。
    # 说明：H3 工作流内部还有 H3ContinuousStitchOutputV14 等带 audio 输入的中间节点（audio 为
    # optional），它们承担 10 段之间的音频接力；这里**不切断中间节点**，避免破坏连续段链路，
    # 也避免产出"静音但仍有音频流"的伪无声文件。真正不产生音轨靠切断末端 mux 节点实现。
    H3_AUDIO_MUX_TYPES = ("CreateVideo", "CreateVideoWithAudio", "SaveVideo",
                          "SaveWEBM", "VHS_VideoCombine", "SaveAudio", "CreateAudio")
    # 兼容旧命名（早期版本使用的属性名）
    H3_AUDIO_SINK_TYPES = H3_AUDIO_MUX_TYPES



    # ===================== 随机种子注入（质检重试时用于产出不同结果） =====================



    # ===================== 视频生成（MiniMax H3） =====================
    # D-08（P2）：原先此处有一个 [LEGACY · 已停用] 的固定「10 段模板单次生成」方法，
    # 函数名却在语义上像是「生成一个视频」——新同学按名字调用会让每个分镜都跑 10 段。
    # 经全仓（含 frontend/）核实**零调用者**，已删除；H3 视频生成统一走下方
    # generate_h3_sequence / generate_h3_sequence_sequential（段数 = 传入分镜数）。
    # 防回潮守卫见 verify_legacy_generate_video.py。




    # ================= H3 · Director 路径（2026-09-27 起为默认） =================

    #: 参考图上传到 ComfyUI input 下的子目录（带内容哈希前缀，杜绝跨集同名覆盖）
    H3_DIRECTOR_REF_SUBDIR = "mscxt_h3_refs"












# ==================================================================== #
# ComfyUI 主动心跳（2026-10-09）
# -------------------------------------------------------------------- #
# 背景：ComfyUI 可能因为 aimdo/CUDA 故障整进程退出（本机 2026-10-09 实测：
#   cuMemSetAccess 失败 600 → VRAM Allocation failed (non OOM) → Fault failed: 2）。
# 已有的自愈 _sb_heal_comfyui 只在「这一镜已经失败」之后才触发，
# 而该镜的出图请求会一直挂到自己的长超时才返回 —— 期间界面只能显示「重试中」。
# 本模块增加**只读心跳**：连续探测失败即主动重启，不必等业务超时。
#
# 安全边界：
#   · 只探 /system_stats，不提交任何任务；
#   · 连续 N 次失败才动作（默认 3 次）；
#   · 限流：10 分钟内最多重启 2 次，避免「崩溃—重启—再崩」风暴；
#   · 串行：同一时刻只允许一个重启在跑；
#   · 可用 MJSCXT_COMFYUI_HEARTBEAT=0 整体关闭。
# ==================================================================== #
_ENGINE_STATE = {
    "online": None,            # None=未知 / True / False
    "consecutive_fails": 0,
    "last_ok_ts": 0.0,
    "last_check_ts": 0.0,
    "last_error": "",
    "restarts": [],            # [(ts, ok), …]
    "restarting": False,
    "heartbeat_started": False,
}
_ENGINE_LOCK = threading.Lock()
_HEARTBEAT_THREAD = None


def get_engine_state() -> dict:
    """引擎状态快照（给 API / 前端读，只读、永不抛）。"""
    with _ENGINE_LOCK:
        s = dict(_ENGINE_STATE)
    s["restarts"] = list(s.get("restarts") or [])
    s["restarts_recent"] = sum(1 for ts, _ok in s["restarts"] if time.time() - ts < 600)
    s["stale_sec"] = (time.time() - s["last_check_ts"]) if s.get("last_check_ts") else None
    return s


#: 探测 /system_stats 的超时（秒）。
#  ⚠️ 2026-10-10 实测缺陷：原值 3 秒**过短** —— ComfyUI 在 GPU 满载出图时，
#  HTTP 服务线程会因 GIL/IO 竞争而响应迟缓，/system_stats 经常 >3 秒才回。
#  心跳因此把它判成「离线」，连续 3 次（60 秒）就**误触发重启**；而旧进程其实还活着
#  （重启日志里 "Port 8188 is already in use" 即铁证），于是两个 ComfyUI 同时抢 8G 显存
#  → 真崩溃（comfyui.prev.log 在 10:03:12 戛然而止，无 Traceback、无 OOM 报错＝被强杀）。
#  放宽到 15 秒：既能让「真的挂了」在合理时间内被发现，又不会把「正忙」误判成「死」。
_HEARTBEAT_PROBE_TIMEOUT = 15.0


def _comfyui_busy(timeout: float = 5.0) -> bool:
    """ComfyUI 是否**正在出图**（队列里有运行中的任务）。

    这是「忙」与「死」的分界：只要 /queue 的 queue_running 非空，说明进程活着、
    只是在干活 —— 此时**绝不能**计入「探测失败」，更不能重启。
    任何异常都返回 False（拿不准就按原逻辑走，绝不因本函数自身出错而改变行为）。
    """
    try:
        import requests as _rq
        r = _rq.get("http://127.0.0.1:8188/queue", timeout=timeout)
        if r.status_code != 200:
            return False
        q = r.json() or {}
        return bool(q.get("queue_running"))
    except Exception:  # noqa: BLE001
        return False


def heartbeat_once(fail_threshold: int = 5, max_per_10min: int = 2) -> dict:
    """探一次并按需重启。返回本次动作摘要，全程 fail-open。

    ⚠️ 2026-10-10 修复「误判忙为死 → 双实例抢 GPU → 真崩」：
      · 探测超时 3 → 15 秒（见 _HEARTBEAT_PROBE_TIMEOUT 注释）；
      · **探测失败时先查 /queue**：若有任务在跑，说明只是「忙」不是「死」，
        直接视为在线（清零失败计数），不重启；
      · 连续失败阈值 3 → 5（配合 20 秒间隔 = 100 秒），给真故障留出确认窗口。
    """
    cli = ComfyUIClient()
    online, err = False, ""
    try:
        st = cli.get_status(timeout=_HEARTBEAT_PROBE_TIMEOUT)
        online = st.get("status") == "online"
        if not online:
            err = str(st.get("error") or "")[:200]
    except Exception as e:  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, e)
    # ---- 「忙」不算「死」：GPU 满载时 /system_stats 慢是正常现象 ----
    if not online:
        try:
            if _comfyui_busy():
                online = True
                err = ""
                logger.debug("[心跳] 探测超时但队列有任务在跑 → 判定为「忙」，不计失败")
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    action = "none"
    with _ENGINE_LOCK:
        _ENGINE_STATE["last_check_ts"] = now
        _ENGINE_STATE["online"] = online
        _ENGINE_STATE["last_error"] = "" if online else err
        if online:
            _ENGINE_STATE["consecutive_fails"] = 0
            _ENGINE_STATE["last_ok_ts"] = now
        else:
            _ENGINE_STATE["consecutive_fails"] += 1
        fails = _ENGINE_STATE["consecutive_fails"]
        recent = sum(1 for ts, _ok in _ENGINE_STATE["restarts"] if now - ts < 600)
        if (not online) and fails >= fail_threshold and recent < max_per_10min \
                and not _ENGINE_STATE["restarting"]:
            _ENGINE_STATE["restarting"] = True
            action = "restart"
    if action == "restart":
        logger.warning("[心跳] ComfyUI 连续 %d 次探测失败（%s），主动重启…", fails, err[:120])
        ok = False
        try:
            ok = cli.restart_comfyui(wait_sec=180, poll_sec=3.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("[心跳] 重启异常：%s", e)
        with _ENGINE_LOCK:
            _ENGINE_STATE["restarting"] = False
            _ENGINE_STATE["restarts"].append((now, bool(ok)))
            _ENGINE_STATE["restarts"] = _ENGINE_STATE["restarts"][-40:]
            if ok:
                _ENGINE_STATE["consecutive_fails"] = 0
                _ENGINE_STATE["online"] = True
                _ENGINE_STATE["last_ok_ts"] = time.time()
                _ENGINE_STATE["last_error"] = ""
        logger.info("[心跳] ComfyUI 主动重启%s", "成功" if ok else "失败（仍离线，已计入限流）")
    return {"online": online, "action": action, "fails": fails, "error": err[:200]}


def start_comfyui_heartbeat(interval: float = 20.0, fail_threshold: int = 5,
                            max_per_10min: int = 2) -> bool:
    """启动只读心跳守护线程（幂等）。MJSCXT_COMFYUI_HEARTBEAT=0 可整体关闭。

    ⚠️ fail_threshold 默认由 3 提到 5（2026-10-10）：配合 20 秒间隔 = 100 秒确认窗口。
    原值 3（60 秒）在 GPU 满载时太容易被「忙」凑满，会误触发重启并造成双实例抢卡。
    """
    global _HEARTBEAT_THREAD
    if os.environ.get("MJSCXT_COMFYUI_HEARTBEAT", "1").strip().lower() in ("0", "false", "no"):
        logger.info("[心跳] 已由 MJSCXT_COMFYUI_HEARTBEAT 关闭")
        return False
    with _ENGINE_LOCK:
        if _ENGINE_STATE.get("heartbeat_started"):
            return True
        _ENGINE_STATE["heartbeat_started"] = True

    def _loop():
        while True:
            try:
                heartbeat_once(fail_threshold=fail_threshold, max_per_10min=max_per_10min)
            except Exception as e:  # noqa: BLE001
                logger.debug("[心跳] 循环异常忽略：%s", e)
            time.sleep(max(5.0, float(interval)))

    _HEARTBEAT_THREAD = threading.Thread(target=_loop, name="comfyui-heartbeat", daemon=True)
    _HEARTBEAT_THREAD.start()
    logger.info("[心跳] 已启动（每 %.0fs 探一次；连续 %d 次失败自动重启；10 分钟最多 %d 次）",
                interval, fail_threshold, max_per_10min)
    return True

