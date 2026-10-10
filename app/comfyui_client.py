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
    H3_DISABLE_DLSS,
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

#: 场景九宫格「单次出图」提示词的**代码内兜底正文**（模板文件缺失/渲染失败时用）。
#: ⚠️ 与 app/prompts/scene_grid_main.txt 剥离头注释后的正文**逐字一致**，改任一处都要同步
#:    另一处（渲染差异会改变 prompt 内容指纹）。占位符与 REGISTRY["scene_grid_main"] 一致。
#: 九宫格逐格版的「画面内文字」段（2026-10-09 起**按需注入**）。
#: 官方原话：「若不需要可读文字，一句都不要提『文字』（提了会诱发凭空画字）」——
#: 原先该段无条件注入（197 字），等于每镜都在提「文字」，既撑长提示词又与该建议相悖。
TEXT_SECTION_ZH = (
    "**画面内文字（关键 · 依 Qwen-Image 官方用法）：**\n"
    "需要可读文字时，**把确切原文放进引号**并写明载体与字体颜色，例：\n"
    "「守则顶部居中为黑色粗体字 \"楼层安全守则\"」。引号内必须是确切原文、逐字正确，\n"
    "**不得**只说「小字 / 文字 / 标题」而不给原文（那必然乱码）。文字尽量不超过 8 个字。\n"
)
#: 「本镜需要可读文字」的判定词（与 comfyui_client._ensure_item_white_bg 的 _text_markers 同源口径）
TEXT_HINT_WORDS = ("文字", "汉字", "书名", "告示", "卡片", "标签", "手写",
                   "守则", "屏幕", "字样", "字迹", "招牌", "标语", "牌匾")

_SCENE_GRID_MAIN_FALLBACK = """生成一张3x3的九宫格场景设定图，呈现同一个场地的9个不同机位与景别。

{style}

**场景设定：**
{scene_prompt}

**画面布局与内容：**
单张图像内均匀排列9个画面（3行3列），每个画面的左上角分别标注白色数字"1"至"9"。
9个画面是**同一个场地**在不同机位与景别下的取景：空间结构、建筑布局、材质纹理、家具陈设、光影色调必须完全一致，只有相机的位置、朝向与取景范围不同。

{panel_plans}

**视觉要求：**
*   **风格：** 整张图（含全部9个画面）必须是统一的风格化 CG 渲染，不是真人实拍照片；画面之间不画分隔边框，以画面内容自然分区。
*   **一致性：** 9格的墙面、地面、门窗、陈设位置必须对得上，是同一空间的不同视角，不要变成9个互不相关的场地。
*   **禁忌：** 除左上角的数字1-9外，画面中严禁出现任何文字、水印或标题；画面内不要有人。
"""
prompt_templates.register_fallback("scene_grid_main", _SCENE_GRID_MAIN_FALLBACK)


# ===================== 调用统计（P2-3 成本看板数据源） =====================
# 只做进程内累计计数，不落盘、不影响业务；analytics 模块按需读取。
_CALL_STATS = {
    "prompt_submitted": 0,      # 提交到 /prompt 的次数
    "prompt_failed": 0,         # 提交失败次数
    "completed": 0,             # 等待完成的次数（成功）
    "waited_seconds": 0.0,      # 累计等待时长（GPU 在跑的时间近似值）
    "segments_generated": 0,    # 累计生成段数（视频）
}
_CALL_STATS_LOCK = threading.Lock()


def get_call_stats() -> dict:
    """读取调用统计快照"""
    with _CALL_STATS_LOCK:
        return dict(_CALL_STATS)


def reset_call_stats() -> dict:
    """重置调用统计"""
    with _CALL_STATS_LOCK:
        for k in list(_CALL_STATS.keys()):
            _CALL_STATS[k] = 0 if not isinstance(_CALL_STATS[k], float) else 0.0
        return dict(_CALL_STATS)


def _bump(key: str, delta=1) -> None:
    """安全累加统计项（统计失败绝不影响业务）
    B-19 H1：读改写包锁，避免多线程并发竞争导致计数丢失。
    """
    try:
        with _CALL_STATS_LOCK:
            _CALL_STATS[key] = _CALL_STATS.get(key, 0) + delta
    except Exception as e:  # noqa: BLE001
        logger.warning("调用统计累加失败（计数可能失真）：%s", e)

# 前端伪控件 / 虚拟节点（不应提交给后端）
PSEUDO_WIDGETS = {
    "upload", "refresh", "Constant",
    "Auto-refresh after generation", "control_after_generate_widget",
    # 2026-10-09 修复真实遗漏：名单里只有 ..._widget 变体，而 ComfyUI 前端真名是
    # control_after_generate（不带头/尾缀）→ LoadImageOutput 等节点上的它没被剥掉，
    # 提交时被校验器报成 unexpected_inputs（verify_qwen21_migration 的 B14 长期红）。
    "control_after_generate",
}
# 位置兜底时需要剔除的「执行后动作」取值（跟随在 seed 之后）
PSEUDO_VALUES = {"fixed", "increment", "decrement", "randomize"}
# 需要跳过的虚拟节点类型
VIRTUAL_NODE_TYPES = {
    "MarkdownNote", "Note", "Label (rgthree)", "Reroute", "PrimitiveNode", "Bookmark",
}
# 正向提示词判定：出现这些词视为负向提示词
NEGATIVE_HINTS = ("模糊", "水印", "blurry", "watermark", "low quality", "worst quality", "低质量", "噪点")

# ⚠️ 「关键词命中」不足以判定负向槽位：正向提示词**主动**写「画面中不得出现任何文字、
#    字幕、水印、logo」这类否定式约束是常态（见 build_storyboard_prompt 的收尾段），
#    其中的「水印」二字是正向语义。若按裸关键词把它判成负向槽位，后果是
#    clean_conflict_negative_tokens 会在**正向**提示词里删掉 CONFLICT_NEGATIVE_TOKENS，
#    甚至把「水印/logo/文字/AI生成」当负向词追加到正向提示词（历史去水印强化踩过的坑）。
#    判据：命中 NEGATIVE_HINTS，**且不是**否定式正向前缀。
_NEG_CONSTRAINT_RE = re.compile(
    r"(不得|严禁|禁止|不要|避免|杜绝|切勿|不含|不包含|没有|无)"
    r"[^，。；;、]{0,16}"
    r"(水印|文字|字幕|logo|标识|签名|日期戳|模糊|噪点|low quality|blurry|watermark)",
    re.IGNORECASE,
)


def _is_negative_slot(value: str) -> bool:
    """该提示词槽位是否为「负向槽位」（本模块 4 处提示词节点判定共用同一判据）。

    ⚠️ 不要退回成裸关键词命中 —— 那会把正向的「不得出现…水印」判成负向槽位
    （实测导致 77% 的正向提示词被追加裸负面词）。
    """
    if not value:
        return False
    if _NEG_CONSTRAINT_RE.search(value):
        return False        # 「不得出现 X」= 正向约束，不是负向槽位
    low = value.lower()
    return any(h.lower() in low for h in NEGATIVE_HINTS)


# ===================== 提示词槽位极性（正负同体节点） =====================
#
# QwenImage2.1 起，``TextEncodeQwenImage21`` 把正向(prompt)与负向(negative_prompt)
# **装在同一个节点**里，并输出 positive / negative / latent 三路。这打破了本模块此前
# 「一个提示词节点一个极性，靠 NEGATIVE_HINTS 内容启发式判极性」的隐含前提：
#
#   把该节点的所有字符串输入拼起来判极性 → 必然命中「模糊/水印」等负向词
#   → 整个节点被判成负向槽位。后果有两个方向，且**都不会报错**：
#     ① ``_generate_base_image`` 落正向提示词时按「非 text 字段就把所有字符串输入都写成
#        正向词」的老逻辑走 → 把 negative_prompt 覆盖成正向提示词，负向词彻底失效；
#     ② ``clean_conflict_negative_tokens`` 会因为字段名 (``negative_prompt``)
#        不在 PROMPT_TEXT_FIELDS 里而**完全跳过负向槽位**。
#
# 所以极性判定改为「节点类型优先、内容启发式兜底」：
#   - 正负同体节点 → 按**字段名**定极性，不做任何内容启发式；
#   - 老模板（CLIPTextEncode / TextEncodeQwenImageEditPlus）→ 沿用 _is_negative_slot。
#: 「正负同体」提示词节点类型
COMBINED_PROMPT_NODE_TYPES = ("TextEncodeQwenImage21",)
#: 正负同体节点里承载正向 / 负向文本的字段名
COMBINED_POSITIVE_FIELD = "prompt"
COMBINED_NEGATIVE_FIELD = "negative_prompt"
#: 图片链路的提示词编码节点白名单（集中一处，避免 4 处手抄漂移）
PROMPT_NODE_TYPES = ("CLIPTextEncode", "TextEncodeQwenImageEditPlus",
                     "TextEncodeQwenImage21")


# ===================== TE_MAN 提示词增强节点（运行时注入，2026-10-06） =====================
# 给全部「图片生成」工作流在提交前挂上 TE_MAN 插件的提示词增强节点：
# TextEncodeQwenImage21 的正向 prompt 改接增强节点输出 0，原始提示词全文交给它
# 用「AI 设置 · 文本分析模型」先增强一遍再编码。**运行时注入，不改模板 JSON**；
# 注入点在 ComfyUIClient.queue_prompt（提交唯一收口），判定与回落见该方法。
# 开关：env MJSCXT_PROMPT_ENHANCER（默认开；0/false/off/no 关，config._env_bool 口径）。
# ⚠️ 与 config 层的 MJSCXT_PROMPT_ENHANCE（prompt_qc.preflight 的 LLM 文本增强）
#    是两道不同的工序，名字相近勿混：那道改的是「提示词文本」，本节点改的是
#    「提交给 ComfyUI 的工作流图」。
PROMPT_ENHANCER_CLASS = "TE_Qwen_Image_2_1_Prompt_Enhancer"
#: 增强节点承载原始提示词全文的输入名（字段名即中文串，与插件声明逐字一致）
PROMPT_ENHANCER_PROMPT_FIELD = "输入提示词"
#: 任务模式取值：有参考图 → 图生图（走 i2i PE），无参考图 → 文生图（走 t2i PE）
PROMPT_ENHANCER_MODE_I2I = "图生图"
PROMPT_ENHANCER_MODE_T2I = "文生图"
#: 8 路可选参考图输入名（第 1 路无序号后缀）；只接前 8 路，多余参考图不接
PROMPT_ENHANCER_IMAGE_FIELDS = ("图片", "图片2", "图片3", "图片4",
                                "图片5", "图片6", "图片7", "图片8")
#: 本地推理参数（增强方式=API 时不参与推理；按用户实测工作流的取值填死，便于人工对照）
PROMPT_ENHANCER_LOCAL_DEFAULTS = {
    "输出语言": "中文",
    "增强方式": "API",
    "文生图PE模型": "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
    "图生图PE模型": "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
    "主模型": "Qwen3.5-4B-UD-Q4_K_XL.gguf",
    "mmproj": "qwen3.5mmproj-BF16.gguf",
    "最大生成token": 4096,
    "上下文长度": 8192,
    # ⭐ 2026-10-07：改为 False（模型常驻）。
    # 此前硬编码 True：每次生成都让增强节点把 9B PE 模型卸回磁盘，下一镜再
    # 从磁盘重读（实测每次 30~60 秒）——与系统级「KEEP_MODEL_LOADED=True
    # 不向 ComfyUI 发 /free」的常驻策略自相矛盾。显存由 ComfyUI 自己的
    # offload 管理兜底（下一个任务需要显存时自动把暂不用的模型挪到 RAM），
    # 不卸载不会导致 OOM；34GB 系统内存 + 33GB RAM 余量充足。
    "生成后自动卸载模型": False,
    "启用思考": False,
}
#: 增强节点注入开关的 env 名（读法走 config._env_bool 白名单口径）
PROMPT_ENHANCER_ENV = "MJSCXT_PROMPT_ENHANCER"

#: 「确切文字」硬约束的**幂等标记**（单一来源，勿另造一份）。
#: 与 :meth:`_ensure_scene_text_render` / :meth:`_ensure_item_white_bg` 追加的「确切文字」
#: 硬约束段用同一串做幂等判据；也是「确切文字硬约束保护」（2026-10-07）的检出串。
#: 该串是 :meth:`_ensure_scene_text_render` 幂等检测的字面子串（先拼串、再回读，见其注释）。
# ⚠️ 2026-10-09：marker 必须与下方追加串**字面一致**（旧值含 markdown `**确切**`，
# 已在官方写法改写中移除 → 必须同步更新，否则幂等判定永假、提示词无限膨胀）。
SCENE_EXACT_TEXT_MARKER = "必须呈现文字：居中印有"

#: 场景**空间关联**参考图的使用声明（2026-10-10）。
#: 为什么需要它：实测「以关联场景为参考」时参考图引力较强，会把提示词里的陈设细节
#: 一并覆盖（木桌→金属桌）。此句把参考图的**作用域**限定在材质 / 建筑结构 / 光线，
#: 陈设仍以本场景描述为准，兼顾「空间连贯」与「细节可控」。
SCENE_RELATION_REF_HINT = (
    "；参考图仅用于对齐建筑结构、墙面与地面材质、门框形制与光线色温，"
    "本场景的家具与陈设以以上描述为准"
)


def _prompt_enhancer_node_enabled() -> bool:
    """增强节点注入的运行时开关（默认**关**；1/true/yes/on 显式开启）。

    ⭐ 2026-10-09 用户决策：提示词已按官方文档产出（H3 六段 + Qwen-Image 官方文字用法），
    **不再需要增强节点改写** —— 改写会破坏官方写法（尤其引号内的确切文字与硬约束段），
    且与「生成前提示词质检」职责重叠。生成前质检保留（见 h3_prompt_kit/prompt_qc 预检）。

    读法对齐 config._env_bool 白名单口径（1/true/yes/on 为真，其余显式取值为假，
    未设置/空串取默认）。每次提交即时读取，改 env 不用改代码。
    """
    try:
        import config as _cfg
        return bool(_cfg._env_bool(PROMPT_ENHANCER_ENV, False))
    except Exception:  # noqa: BLE001  配置模块异常不阻断生成（fail-closed 于"增强"，
        # 2026-10-09 用户决策：增强已停用 → 异常时也保持停用（返回 False），不再 fail-open 到开。
        return False


def _enhancer_text_credentials() -> Tuple[str, str, str]:
    """读本系统「AI 设置 · 文本分析模型」模块的 base_url / api_key / model。

    与 prompt_enhance._client_for 同一读取口径：ai_config 单一事实源
    （tasks.db 凭证表优先，回落 env > 加密库 > json）。任一项缺失返回空串，
    由调用方整体跳过注入（fail-open，走原提示词提交）。
    """
    import ai_config
    from config import AI_CONFIG_PATH, LLM_CONFIG_PATH
    cfg = ai_config.get_module(
        ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH), "text")
    if not isinstance(cfg, dict):
        return "", "", ""
    return (str(cfg.get("base_url") or "").strip(),
            str(cfg.get("api_key") or "").strip(),
            str(cfg.get("model") or "").strip())


def _linked_enhancer_text(api_prompt: dict, link) -> Optional[str]:
    """连线若指向「提示词增强节点」，返回它保存的原始提示词全文（否则 None）。

    注入后编码节点的 prompt 输入从「文本控件」变成连线 [enhancer_id, 0]，
    原文全文保存在增强节点的「输入提示词」里 —— 注入之后任何想读「正向提示词
    文本」的代码（极性判定 / 审计 / 校验器）都经这里穿透取原文，读侧看到的
    仍是文本，与注入前同形。
    """
    if not (isinstance(link, (list, tuple)) and len(link) == 2):
        return None
    src = (api_prompt or {}).get(str(link[0])) or {}
    if str(src.get("class_type") or "") != PROMPT_ENHANCER_CLASS:
        return None
    text = (src.get("inputs") or {}).get(PROMPT_ENHANCER_PROMPT_FIELD)
    return text if isinstance(text, str) else None


def _node_sort_key(nid) -> int:
    """节点 id 排序键（数字 id 按数值，非数字 id 视作 0 —— 与历史行为一致）"""
    return int(nid) if str(nid).isdigit() else 0


#: 「动态展开」输入类型：object_info 里只登记**父名**，子项在提交时以点号键出现
#:   COMFY_AUTOGROW_V3     → images.image_1 …
#:   COMFY_DYNAMICCOMBO_V3 → format.bit_depth / format.input_color_space …
DYNAMIC_INPUT_TYPES = ("COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3")


def _is_dynamic_child(declared: dict, key: str) -> bool:
    """``key`` 是否是某个动态展开输入（autogrow / dynamic-combo）的子项？

    服务端 `_expand_schema_for_dynamic` / `DynamicCombo` 就是这样生成子项名的，
    校验器若只比对父名，会把合法的 `images.image_1`、`format.bit_depth` 全报成
    「未知输入」——纯误报，会把真异常淹掉。
    """
    if "." not in str(key):
        return False
    parent = str(key).rsplit(".", 1)[0]
    spec = declared.get(parent)
    return bool(spec) and isinstance(spec, (list, tuple)) and bool(spec) \
        and spec[0] in DYNAMIC_INPUT_TYPES


def _prompt_slot_polarity(class_type: str, field: str, value) -> str:
    """提示词槽位极性：``"pos"`` / ``"neg"`` / ``""``（不是提示词槽位）。

    - 正负同体节点：按**字段名**判（prompt=正向、negative_prompt=负向），
      空串也算（负向槽位默认就是空的，必须能被加固写入）；
      ⚠️ 字段名判定**不依赖值类型**：注入增强节点后 prompt 会从文本控件变成
      连线 [enhancer_id, 0]，正向槽位极性必须仍然成立（文本经 `_linked_enhancer_text`
      穿透读取），否则已注入的 prompt 再次被处理时会漏判正向槽位；
    - 老模板：空槽位/连线返回 ""（无可判内容，跳过），否则走 `_is_negative_slot` 启发式。
    """
    if field not in PROMPT_TEXT_FIELDS:
        return ""
    if class_type in COMBINED_PROMPT_NODE_TYPES:
        if field == COMBINED_POSITIVE_FIELD:
            return "pos"
        if field == COMBINED_NEGATIVE_FIELD:
            return "neg"
        return ""
    if not isinstance(value, str) or not value.strip():
        return ""
    return "neg" if _is_negative_slot(value) else "pos"


def _iter_prompt_slots(api_prompt: dict, class_types=PROMPT_NODE_TYPES):
    """遍历提示词槽位，产出 ``(节点id, 字段名, 取值, 极性)``。

    判断单一来源：极性一律经 `_prompt_slot_polarity`，调用方不再各写一套启发式。
    取值契约：**只产出字符串**。注入后正向 prompt 是连线 → 穿透到增强节点的
    「输入提示词」取原文（读侧与注入前同形）；穿透不到的连线（如老工作流把
    negative_prompt 接到文本节点）→ 视为无可判文本跳过 —— 与旧行为一致，
    避免把连线对象当文本消费（clean_conflict_negative_tokens 会对取值 .strip()）。
    """
    for nid, node in (api_prompt or {}).items():
        if not isinstance(node, dict):
            continue
        ctype = str(node.get("class_type") or "")
        if ctype not in class_types:
            continue
        inputs = node.get("inputs") or {}
        for field in PROMPT_TEXT_FIELDS:
            value = inputs.get(field)
            polarity = _prompt_slot_polarity(ctype, field, value)
            if not polarity:
                continue
            if not isinstance(value, str):
                text = _linked_enhancer_text(api_prompt, value)
                if text is None:
                    continue
                value = text
            yield nid, field, value, polarity


# ===================== 参考图槽位键名 =====================
# 两代编辑节点两种键名，必须都认（只认老键名 → 新模板参考图静默不注入）：
#   Qwen-Edit 2511 / TextEncodeQwenImageEditPlus :  image1 / image2 / image3
#   QwenImage2.1 / TextEncodeQwenImage21         :  images.image_1 … (autogrow 点号键)
_IMAGE_SLOT_RE = re.compile(r"^(?:images\.)?image_?(\d*)$")


def _slot_index(key) -> int:
    """参考图槽位序号（``images.image_3`` / ``image3`` → 3；无语尾数字 → 0）"""
    m = _IMAGE_SLOT_RE.match(str(key))
    return int(m.group(1)) if (m and m.group(1)) else 0


# ---- 景别/机位解析已下沉到叶子模块 shot_camera（2026-10-08 解耦）----
# 这里保留同名再导出：app.py / te_3d_director.py / 守卫脚本的既有 import 全部照旧可用，
# 而新的解析点应当直接 from shot_camera import ...（不再经过 ComfyUI 客户端）。
from shot_camera import (  # noqa: F401
    CAMERA_UNSPECIFIED_SPEC, SHOT_CAMERA_SPECS, _CAMERA_ANGLE_ALIASES, _CAMERA_ANGLE_ORDER,
    _CAMERA_ANGLE_SPECS, _CAMERA_KEY_ORDER, camera_angle, camera_key, camera_spec,
    shot_framing, shot_motion)


SHOT_ACTION_SUFFIX = ("；上述动作必须完整、明确地表现出来（动作结果一眼可辨，如道具已收起、已离开手部），"
                      "不得省略、弱化或只做出起始姿态")


# ===================== 参考图标签 → Qwen-Image-2.1 官方句式 =====================
# 生成端（app._allocate_storyboard_refs）产出的 label 形如：
#   「参考图1（<image1>）是角色「方源」的身份锚点：保持其面部身份、发型与体型不变」
#   「参考图4（<image4>）是物品「青霜剑」的形状、材质与配色锚点」
# 官方协议要求把「谁提供什么」写成 <imageN> 显式编号 + 单一职责，故这里做一次
# 确定性的句式转换（不调用模型）。转换失败时返回空串，由调用方跳过该条 —— 宁可少一条
# 职责声明，也不要写一句模型读不懂的模糊指代。

#: 标签里「参考图N（<imageN>）」前缀的匹配（兼容生成端两种写法）
_REF_LABEL_PREFIX_RE = re.compile(r"^参考图\s*\d+\s*[（(]\s*<image\s*\d+>\s*[)）]\s*")
#: 「<imageN>」标记本身
_REF_IMAGE_TAG_RE = re.compile(r"<image\s*(\d+)>", re.IGNORECASE)
#: 标签里的中文职责词 → 官方英文职责短语
_REF_ROLE_ZH2EN = (
    ("身份锚点", "character identity"),
    # 2026-09-29：场景槽位话术升级为「空间与光照锚点」（地理优先规格）。
    # 必须放在「环境与氛围锚点」之前（更具体优先）；旧词条保留以兼容存量 label
    # 与历史剧本，否则存量分镜会退化成「中文职责原文混进英文提示词」。
    ("空间与光照锚点", "the spatial layout and lighting mood"),
    ("环境与氛围锚点", "the environment and atmosphere"),
    ("形状、材质与配色锚点", "the shape, material and colour"),
    ("形状、材质与配色", "the shape, material and colour"),
    ("外貌与服装", "appearance and costume"),
    ("外貌、服装与发型", "appearance, costume and hairstyle"),
    ("头部特写", "a head close-up as the framing anchor"),
)


def _ref_label_body(raw, index: int) -> str:
    """剥掉「参考图N（<imageN>）」前缀，返回职责正文（无有效正文 → 返回 ""）"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _REF_LABEL_PREFIX_RE.sub("", text).strip()
    # 兼容不带括号的历史写法：「参考图1是角色「X」的外貌…」
    text = re.sub(r"^参考图\s*\d+\s*是?", "", text).strip()
    return text


# ===================== 3D 导演台「构图基准图」协议（2026-09-27） =====================
# 分镜生成前先用服务端 3D 渲染器出一张站位/机位基准图（app/te_3d_render.py），
# 作为 <image1> 喂给 Qwen-Image-2.1。这是 TE MAN 官方推荐用法：3D 站位定构图、
# 设定图定身份。
#
# ⚠️ 与「设定图」的语义差别必须写清楚，否则模型会把人偶的外观当成身份基准：
#    · <image1> 只负责**构图**（人数 / 左右 / 前后 / 朝向 / 景别 / 机位角度）；
#    · 人偶的灰彩色身体、无面头部、体块比例**一律不得**被复刻；
#    · 角色/道具是否像设定，只看后面的设定图。
BLOCKING_REF_MARK = "3D导演台构图基准"
#: 带构图基准图时追加的协议段（官方 Attribute Disentanglement：每张图职责单一）
#: 2026-10-03 强化：旧版提示词不够强，模型仍把人偶画进 Panel 1。
#: 新增：明确说人偶图是「pose/composition template」，必须被角色图「完全覆盖」。
COMPOSITION_BASELINE_SECTION = (
    "COMPOSITION BASELINE: <image{pos}> is a 3D blocking reference — a pose and "
    "composition template made of plain coloured, faceless mannequins on a dark "
    "background. It shows ONLY the poses, positions, and camera framing. "
    "CRITICAL: This mannequin image is NOT part of the final output. You MUST "
    "completely REPLACE every mannequin figure with the actual characters from the "
    "other reference images, keeping ONLY the pose and position from this template. "
    "The mannequin's grey/blue body, faceless head, flat colours, and geometric "
    "shapes must NOT appear anywhere in the final image — not in any panel, not "
    "partially, not as an underlayer. Every panel must show fully rendered characters "
    "with faces, hair, costumes, and details from the character reference images. "
    "Copy ONLY the composition from <image{pos}>: how many figures there are, who "
    "stands on the left / right, their relative depth order and facing direction, "
    "the shot size (framing) and the camera angle. "
    # ⚠️ 实测（2026-09-27）：基准图是近黑背景 + 有限地面网格，远景时上下各留出
    # 一大片平坦深色区；不说清楚模型会把它当**信箱黑边**照抄（shot_01 上下各一条
    # 黑带、画面只剩中间约一半高度）。基准图里没有真实环境，必须显式声明留白不算画框。
    "Its large flat empty areas (the dark sky and the bare ground beyond the grid) are "
    "standing-room padding, NOT part of the frame: repaint them as this shot's real "
    "environment, and never reproduce them as black bars, letterbox borders, frames or "
    "flat colour blocks."
)

#: 「角色身份基准网格」参考图标记（2026-10-06）：app._storyboard_worker 把角色基准图
#: PIL 拼成 3×3 参考网格后插进 refs，其 label 必须含本标记 —— build_storyboard_prompt
#: 靠它在 REFERENCE ROLES 里豁免该图并追加 IDENTITY BASELINE GRID 段。
#: ⚠️ 与 BLOCKING_REF_MARK 同一纪律：禁止在调用方写裸字面量，统一引用本常量。
IDENTITY_GRID_REF_MARK = "角色身份基准网格"

#: 身份基准网格的提示词段（{pos} 由 build_storyboard_prompt 按实际槽位号填充）
IDENTITY_BASELINE_GRID_SECTION = (
    "IDENTITY BASELINE GRID (critical): <image{pos}> is a 3x3 grid of nine cells "
    "showing the canonical appearance (facial identity, hairstyle, outfit and "
    "colour palette) of the character(s) of this shot, taken from their character "
    "baseline assets. It is the appearance ground truth: every character rendered "
    "in the output must replicate EXACTLY the face, hairstyle and outfit shown in "
    "the cell(s) of that same character in <image{pos}>. Do not add, remove or "
    "recolor any clothing element relative to the baseline grid.")

#: 分镜图光学/材质段（2026-09-28 画质提升）。
#: 背景：分镜提示词此前**系统性缺画质/光学/材质词** —— 无景深、无次表面散射、
#: 无皮肤/毛发/布料材质描述，输出图扁平、塑料感、细节量低。
#: ⚠️ 措辞必须是**可执行的具体光学/材质描述**；masterpiece / 8k / 超高清
#: 类空词会被 prompt_qc._QUALITY_FLUFF 判为无信息量并剥掉，写了也白写。
CINEMATOGRAPHY_SECTION = (
    "CINEMATOGRAPHY: cinematic depth of field with a shallow focal plane and "
    "soft bokeh in the out-of-focus background; natural skin subsurface "
    "scattering with visible fine pore and fabric-level texture; individual "
    "hair strands catching the key light; cloth weave and material grain "
    "clearly readable; volumetric light shafts and drifting atmospheric "
    "haze; physically plausible shadow falloff with soft contact shadows; "
    "restrained film grain; clean highlight roll-off without clipping."
)


def _ref_label_purpose(body: str) -> str:
    """把中文职责正文转成官方英文职责短语（识别不到就原样保留，由模型自行理解）"""
    text = str(body or "").strip()
    if not text:
        return ""
    for zh, en in _REF_ROLE_ZH2EN:
        if zh in text:
            # 角色/物品/场景名从「「…」」里取出，拼成可读的英文职责
            m = re.search(r"「([^」]+)」", text)
            who = m.group(1) if m else ""
            if who and en in ("character identity", "appearance and costume",
                              "appearance, costume and hairstyle"):
                return f"the identity of \"{who}\" ({en})"
            if who:
                return f"{en} of \"{who}\""
            return en
    return text

# 提示词所在字段：
#   CLIPTextEncode.text / TextEncodeQwenImageEditPlus.prompt
#   TextEncodeQwenImage21.prompt(正向) + TextEncodeQwenImage21.negative_prompt(负向)
# ⚠️ negative_prompt 必须在内：漏掉它会让「冲突负向词清理」在
#    QwenImage2.1 上**整段静默跳过**（字段名对不上 → 一句都没改，也不报错）。
PROMPT_TEXT_FIELDS = ("text", "prompt", "negative_prompt")
# 需要剥离的 LoadImage* 前端显示后缀
SUFFIX_RE = re.compile(r"\s*\[(output|input|temp)\]\s*$")

# ===================== P0 修复：正负提示词冲突 / 场景资产带人 / 画幅统一 =====================

# 1) 正负提示词冲突：按长度降序匹配，保证「3D渲染」先于「3D」被命中
CONFLICT_NEGATIVE_SORTED = tuple(sorted(CONFLICT_NEGATIVE_TOKENS, key=len, reverse=True))

# 2) 场景资产禁止出现人物：
#    场景定义里常带「村民三两结伴」这类人物描述，会直接渲染出人（实测"山间小径"资产图 4 人）。
#    正向追加空场景声明 + 生成前清洗掉人物描述短语。
SCENE_NO_CHARACTER_SUFFIX = (
    "。空场景：画面中不得出现任何人物、人影、人群、士兵或生物，"
    "只呈现环境本身（建筑、地形、植被、道具与光影）"
)
# 人物类词汇（两字及以上，避免误伤"人间仙境/人迹罕至"等场景词）
CHARACTER_WORDS = (
    "人物", "角色", "主角", "村民", "人群", "人们", "众人", "行人", "路人", "群众", "游客", "游人",
    "士兵", "侍卫", "侍从", "仆人", "仆役", "孩童", "孩子", "小孩", "老者", "老人", "青年", "少女",
    "少年", "男子", "女子", "人影", "身影", "侠客", "武者", "修士", "商贩", "摊贩", "渔夫", "农夫",
    "僧人", "道士", "骑士", "守卫", "弟子", "随从", "观众", "看客", "陌生人", "男女老少",
    "人山人海", "三三两两", "结伴", "成群结队", "熙熙攘攘", "往来穿梭", "人头攒动",
)
# 数量+人（如"4人""四个人""几人群"）
CHARACTER_QTY_RE = re.compile(
    r"[0-9０-９一二三四五六七八九十两几数多]+\s*(?:个|名|位|群|队|对)?\s*(?:人|人物|人影|身影)"
)
# 提示词分句符（按句清洗，人物句整句丢弃）
_PROMPT_SPLIT_RE = re.compile(r"[，,；;。\n]")


def scene_view_prompt_suffix(view_key: str) -> str:
    """场景**机位档** → 追加进正向提示词的机位句（``None`` / 空 / 未知档 → 空串）。

    背景（2026-09-29）：场景资产此前只有一张 base.png，分镜不论机位都拿它当参考图 ——
    俯拍 / 斜侧镜头拿到的是正面基准图，构图先验与镜头要求**反向**。
    实测结论（见 ``MULTIVIEW_CONFIG`` 上方注释）：参考图编辑改不动机位，
    **必须在基础图阶段的提示词里带入角度**。本函数就是那个角度句的来源。

    ⚠️ 两条硬约束：
      1. 本函数是**纯查表**：`SCENE_VIEW_KEYS` 里的每一档（含 ``front``）都返回机位句，
         只有 ``None`` / 空串 / 未知档才返回空串。**不留「front 特殊返回空」的隐式分支** ——
         「基础图不追加机位句」这件事由调用方传 ``None`` 表达（`generate_scene_base`
         的默认值就是 ``None``），语义在调用点、不在查表函数里。
         生产链路**不会**用 ``view_key="front"`` 出图：正面档直接复用已过质检的
         base.png（见 app.py 资产 worker 的 scene 分支），故存量行为零变化。
      2. 句子里**不得出现** :data:`CHARACTER_WORDS` / ``CHARACTER_QTY_RE`` 能命中的词，
         否则会在 ``sanitize_scene_prompt`` 里被当成「人物描述句」整句丢弃
         （机位静默失效，日志只显示「丢弃人物描述句」）。改措辞前先跑
         ``.workbuddy/test/_out/probe_scene_views.py``。
    """
    key = str(view_key or "").strip()
    # ⭐ 2026-10-05 场景九宫格：机位句/标签查表支持 4 档（旧逐档）与 9 档（九宫格）两套口径，
    #   单一来源都在 config（SCENE_VIEW_* 与 SCENE_GRID_*）。
    #   ⚠️ SCENE_GRID_* 在 config 里**晚于**本文件 import 的 SCENE_VIEW_* 定义，故这里**函数内**
    #   延迟 import（避免 import 顺序依赖 / 循环），只取四张表。
    # ⭐ 2026-10-06 修正查表优先级：`front/left45/right45/top` 四档在两张表里**都有**，
    #   旧写法 `SCENE_VIEW_*.get(key) or SCENE_GRID_*.get(key)` 让**旧表恒胜** —— 于是
    #   九宫格模式下的标签一直显示旧的「正面全景 / 左前 45° / 顶部鸟瞰」，用户新定义的
    #   「全景（主视角）/ 左45°侧视全景 / 俯视鸟瞰」永远出不来。现改为：**九宫格模式打开
    #   时，凡在 SCENE_GRID_* 里有定义的键一律以九宫格表为准**（机位句对那 4 档是逐字复用
    #   旧表的，故本改动只影响标签口径，不动机位句）。
    try:
        from config import (SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH,
                            SCENE_GRID_LABELS, SCENE_GRID_MODE)
    except ImportError:
        SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH, SCENE_GRID_LABELS = (), {}, {}
        SCENE_GRID_MODE = False
    valid_keys = tuple(SCENE_VIEW_KEYS) + tuple(SCENE_GRID_VIEW_KEYS)
    if not key or key not in valid_keys:
        return ""
    _prefer_grid = bool(SCENE_GRID_MODE) and (
        key in SCENE_GRID_ANGLE_ZH or key in SCENE_GRID_LABELS)
    if _prefer_grid:
        angle = SCENE_GRID_ANGLE_ZH.get(key) or SCENE_VIEW_ANGLE_ZH.get(key)
        label = SCENE_GRID_LABELS.get(key) or SCENE_VIEW_LABELS.get(key) or key
    else:
        angle = SCENE_VIEW_ANGLE_ZH.get(key) or SCENE_GRID_ANGLE_ZH.get(key)
        label = SCENE_VIEW_LABELS.get(key) or SCENE_GRID_LABELS.get(key) or key
    if not angle:
        return ""
    # 与 base 同空间、只换机位：显式声明「同一场地」是防止模型把多档理解成多个场地
    # （一旦理解错，分镜换机位就等于换场景，比没有机位档更糟）。
    # ⚠️ 2026-10-03（B 方案）：旧句尾「建筑形制、空间关系、陈设与光照方向保持不变」里的
    #   「**空间关系**…保持不变」是**反向指令** —— 等于叫模型别动构图，正是 left45/right45
    #   与 base 取景雷同的帮凶。改为把「内容不变」与「机位必须变」**拆开说**：先声明同一场地、
    #   建筑形制/陈设/光照不变（防换场地），再明确「相机站位与朝向已改、必须呈现不同取景透视」。
    return (f"。本图机位（{label}）：{angle}；"
            f"这是同一个场地，建筑形制、陈设与光照方向保持不变，"
            f"但相机站位与朝向已经改变，必须呈现与正面机位不同的取景与透视")

# 目录标注（P0-5 修复）：
#   LoadImageOutput / LoadAudioOutput / LoadVideoOutput 等「Output 系列」读取 ComfyUI
#   output 目录，写入的 image/audio/video 值必须带 " [output]" 标注，否则 ComfyUI 的
#   folder_paths.exists_annotated_filepath() 会回退到 input 目录查找并报
#   400 "Invalid image file"。
#   LoadImage / LoadAudio / LoadVideo 等读取 input 目录，禁止带标注。
ANNOTATED_DIR = "output"
OUTPUT_LOAD_CLASSES = {"LoadImageOutput", "LoadAudioOutput", "LoadVideoOutput",
                       "LoadLatentOutput", "LoadImageMaskOutput"}
INPUT_LOAD_CLASSES = {"LoadImage", "LoadAudio", "LoadVideo", "LoadImageMask",
                      "LoadAnimatedImage", "LoadLatent"}
# 文件名型（媒体）输入键：转换时需按节点类型规范化目录标注
MEDIA_INPUT_KEYS = {"image", "images", "audio", "video", "file", "filename", "path",
                    "image_path", "audio_path", "video_path"}


# ===================== RefMod 节点探测（运维探针，供 /api/comfyui/refmod-status） =====================
# 背景：RefMod（ComfyUI-MiniMaxH3Mod）把角色参考图打包成 .safetensors，像 LoRA 一样
# 直接喂 H3。probe_refmod_nodes 探测节点是否在 ComfyUI 侧生效及其输入声明，
# 供 /api/comfyui/refmod-status 消费。早期的 UI→API 兼容自检 PoC
# （refmod_ui_to_api_poc / _refmod_widget_default）已退役删除（2026-10-05）。
# 口径与 get_status / get_object_info 一致：**fail-open**，
# ComfyUI 不可达 / 节点不存在 / 任何异常都返回结构化结果，绝不抛错、绝不影响生成链路。

#: RefMod / MiniMaxH3Mod 节点类名匹配（大小写不敏感；``.?`` 兼容 MiniMax-H3 等连字符变体）
_REFMOD_NODE_RE = re.compile(r"(refmod|minimax.?h3)", re.IGNORECASE)


def _compact_refmod_spec(raw) -> dict:
    """把 object_info 的一组输入声明压成状态端点需要的紧凑形态（输入名 → [类型, options]）。

    combo 候选只留前 8 个（避免数百个模型文件名把状态端点响应撑爆）；
    options 只留 default / forceInput（后者决定该输入是「控件」还是「连线槽位」）。
    """
    out: dict = {}
    for name, spec in (raw or {}).items():
        if not isinstance(spec, (list, tuple)) or not spec:
            out[str(name)] = [spec] if spec is not None else []
            continue
        t = spec[0]
        opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        kept: dict = {}
        if "default" in opts:
            kept["default"] = opts.get("default")
        if opts.get("forceInput"):
            kept["forceInput"] = True
        out[str(name)] = [list(t)[:8] if isinstance(t, (list, tuple)) else t, kept]
    return out


def probe_refmod_nodes(timeout: int = 5) -> dict:
    """GET {COMFYUI_URL}/object_info 探测 RefMod / MiniMaxH3Mod 自定义节点（P2-2）。

    返回 ``{"reachable": bool, "nodes": [{"class": str, "input": {...}}], "error": str?}``：
    - 遍历 object_info 的节点类名，筛出命中 ``(?i)(refmod|minimax.?h3)`` 的类，
      每个类记录其 required / optional 输入声明（紧凑形态见 :func:`_compact_refmod_spec`）；
    - 请求异常 / 非法响应 → ``reachable=False + error``（fail-open，绝不抛错）。
    """
    result: dict = {"reachable": False, "nodes": []}
    try:
        # 跟随 get_status 的 (connect, read) 元组超时：主机不可达时快速失败，不干等满程超时
        resp = requests.get(f"{COMFYUI_URL}/object_info", timeout=(min(3, timeout), timeout))
        resp.raise_for_status()
        info = resp.json()
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    try:
        if not isinstance(info, dict):
            result["error"] = f"object_info 返回类型异常：{type(info).__name__}"
            return result
        for cls_name, obj in info.items():
            if not _REFMOD_NODE_RE.search(str(cls_name or "")):
                continue
            spec = ((obj or {}).get("input") or {}) if isinstance(obj, dict) else {}
            result["nodes"].append({
                "class": cls_name,
                "input": {"required": _compact_refmod_spec(spec.get("required")),
                          "optional": _compact_refmod_spec(spec.get("optional"))},
            })
        result["reachable"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = f"解析 object_info 失败: {type(e).__name__}: {e}"
    return result


class ComfyUIClient:
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

    def comfy_online(self, timeout: int = 3) -> bool:
        """快速探测 ComfyUI 是否在线（/system/status，短超时）。"""
        try:
            self.get_status(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def restart_comfyui(self, wait_sec: int = 120, poll_sec: float = 2.0) -> bool:
        """重启 ComfyUI（杀现有 python 进程 + 重新拉起 run_nvidia_gpu_fixed.bat + 等 /system/status 就绪）。

        全程 fail-open：任何环节失败都返回 False，不抛异常。
        wait_sec：等 ComfyUI 就绪的总超时（默认 120s，便携版冷启动约 60-90s）。

        ⚠️ 重启会清掉 ComfyUI 侧未完成的队列任务——调用方应在「确认当前任务已超时/失败」后才调用，
        不要在正常出图中途调（会杀掉正在跑的图）。
        """
        import subprocess
        comfy_dir = MJSCXT_COMFYUI_DIR
        bat = os.path.join(comfy_dir, "run_nvidia_gpu_fixed.bat")
        if not os.path.isfile(bat):
            logger.warning("ComfyUI 重启失败：找不到启动脚本 %s（请设 MJSCXT_COMFYUI_DIR）", bat)
            return False
        # ① 杀掉现有 ComfyUI python 进程
        # ⚠️⚠️ 2026-10-09 严重缺陷修复：原实现是
        #       subprocess.run(["taskkill", "/F", "/IM", "python.exe"], …)
        #     —— 它会杀掉**机器上全部 python.exe**，包括**本服务自己**
        #    （后端的 serve.py 也是 python.exe）。实跑复现：心跳探测到 ComfyUI 掉线
        #    → 调本函数 → 后端被自己杀死 → 5211 与 8188 同时消失、生产中断。
        #    现在改为**只杀命令行里含 ComfyUI main.py 的进程**，并在日志里记录杀掉了谁；
        #    匹配不到时**不杀任何进程**（宁可重启失败，也不能误杀后端）。
        killed = []
        try:
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*main.py*' -and "
                "$_.CommandLine -like '*ComfyUI*' } | "
                "ForEach-Object { Write-Output $_.ProcessId; "
                "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                                  capture_output=True, timeout=20, check=False,
                                  text=True, encoding="utf-8", errors="replace")
            killed = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip().isdigit()]
            if killed:
                logger.info("ComfyUI 重启：已终止 ComfyUI 进程 %s", killed)
            else:
                logger.info("ComfyUI 重启：未发现运行中的 ComfyUI 进程（不杀任何进程）")
        except Exception as e:  # noqa: BLE001
            logger.debug("终止 ComfyUI 进程时忽略：%s", e)
        # ①.5 等旧进程**真正退出**再拉新进程
        #   ⚠️ 2026-10-10 实测缺陷：此前 ① 杀掉后**立即** Popen —— 而 Stop-Process 是
        #   异步的，旧进程可能还要几百毫秒~数秒才释放 8188，新进程于是报
        #   "Port 8188 is already in use on address 127.0.0.1"（stderr_crash.log:311）
        #   然后**自行退出** → 表面「重启成功」实际一个 ComfyUI 都没有；
        #   更糟的是若旧进程只是被判定「忙/慢」而未真死，就会出现两个实例同时抢 8G 显存。
        if killed:
            _wait_t0 = time.time()
            _deadline = _wait_t0 + 25
            while time.time() < _deadline:
                try:
                    _chk = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                         "Where-Object { $_.CommandLine -like '*main.py*' -and "
                         "$_.CommandLine -like '*ComfyUI*' } | Measure-Object | "
                         "-ExpandProperty Count"],
                        capture_output=True, timeout=15, check=False,
                        text=True, encoding="utf-8", errors="replace")
                    _left = int((_chk.stdout or "0").strip() or 0)
                except Exception:  # noqa: BLE001
                    _left = 0
                if _left == 0:
                    logger.info("ComfyUI 重启：旧进程已全部退出（等待 %.1fs）",
                                time.time() - _wait_t0)
                    break
                time.sleep(1.0)
            else:
                logger.warning("ComfyUI 重启：等待 25s 后仍检测到未退出的 ComfyUI 进程，"
                               "仍尝试拉起（可能出现端口冲突，请关注日志）")
        # ② 重新拉起（后台，不阻塞）
        try:
            subprocess.Popen(["cmd.exe", "/c", bat], cwd=comfy_dir,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            logger.info("ComfyUI 重启：已拉起 %s", bat)
        except Exception as e:  # noqa: BLE001
            logger.warning("ComfyUI 重启：拉起失败：%s", e)
            return False
        # ③ 等 /system/status 就绪
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            time.sleep(poll_sec)
            if self.comfy_online(timeout=3):
                logger.info("ComfyUI 重启：已就绪（耗时 %.0fs）", time.time() - t0)
                return True
        logger.warning("ComfyUI 重启：%ds 内未就绪（仍离线）", wait_sec)
        return False

    def wait_comfy_online(self, wait_sec: int = 120, poll_sec: float = 2.0) -> bool:
        """只等 ComfyUI 就绪（不重启）。ComfyUI 自愈/冷启动后等待可用。fail-open。"""
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            time.sleep(poll_sec)
            if self.comfy_online(timeout=3):
                return True
        return False


    def _generate_client_id(self) -> str:
        import uuid
        return str(uuid.uuid4())

    # ===================== 基础 HTTP =====================

    def _get(self, path: str, timeout: int = 30) -> Any:
        resp = requests.get(f"{self.base_url}{path}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _http_error_detail(resp) -> str:
        """把 ComfyUI 的错误响应体压成一行可读文本。

        ⚠️ 2026-10-01 全流程实测补：`/prompt` 的 400 **不是网络抖动，而是工作流校验失败** ——
        响应体里带着 `error.type / error.message` 和 `node_errors`（哪个节点、哪个输入不合法）。
        原实现直接 `raise_for_status()`，异常里只剩
        "400 Client Error: Bad Request for url: …"，**真正的原因被丢掉**：
        整集视频挂掉时日志里完全看不出为什么，上层还在无意义地「换种子重试 6 次」
        （校验错误换种子永远不可能成功）。所以这里把响应体解析出来带上。
        """
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            try:
                return (resp.text or "").strip()[:600]
            except Exception:  # noqa: BLE001
                return ""
        if not isinstance(body, dict):
            return str(body)[:600]
        bits = []
        err = body.get("error")
        if isinstance(err, dict):
            bits.append(f"{err.get('type') or 'error'}: {err.get('message') or ''}".strip(": "))
        elif err:
            bits.append(str(err))
        node_errors = body.get("node_errors")
        if isinstance(node_errors, dict):
            for nid, info in list(node_errors.items())[:6]:
                if not isinstance(info, dict):
                    bits.append(f"节点 {nid}: {info}")
                    continue
                msgs = []
                for e in (info.get("errors") or []):
                    if not isinstance(e, dict):
                        continue
                    extra = e.get("extra_info") if isinstance(e.get("extra_info"), dict) else {}
                    where = extra.get("input_name") or extra.get("node_id") or ""
                    msgs.append(f"{e.get('message') or e.get('type') or '不合法'}"
                                + (f"[输入 {where}]" if where else ""))
                bits.append(f"节点 {nid}({info.get('class_type') or '?'}): "
                            + ("; ".join(msgs) or str(info)[:120]))
        return " | ".join(x for x in bits if x)[:900]

    def _post(self, path: str, data: dict = None) -> Any:
        resp = requests.post(f"{self.base_url}{path}", json=data or {}, timeout=300)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"ComfyUI POST {path} 失败：HTTP {resp.status_code}"
                + (f" —— {self._http_error_detail(resp)}" if self._http_error_detail(resp) else ""))
        return resp.json()

    def get_status(self, timeout: int = 3) -> dict:
        """探测 ComfyUI 在线状态。

        注意：这是高频调用的状态接口（前端每次刷新都会打），
        因此**必须用短超时**——默认 3 秒。用 requests 的 (connect, read) 元组形式，
        保证「主机不可达时快速失败」而不是干等 30 秒（Windows 上防火墙丢包会更久）。
        """
        try:
            resp = requests.get(f"{self.base_url}/system_stats",
                                timeout=(min(2, timeout), timeout))
            resp.raise_for_status()
            return {"status": "online", "stats": resp.json()}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def get_object_info(self, force: bool = False, ttl: int = 300) -> dict:
        """获取 /object_info（带缓存，离线时返回空 dict，不影响主流程）"""
        now = time.time()
        if not force and self._object_info is not None and (now - self._object_info_ts) < ttl:
            return self._object_info
        try:
            self._object_info = self._get("/object_info", timeout=60)
            self._object_info_ts = now
        except Exception as e:
            logger.warning(f"获取 object_info 失败（将跳过严格校验）: {e}")
            if self._object_info is None:
                self._object_info = {}
        return self._object_info

    def declared_inputs(self, class_type: str) -> List[str]:
        """按 object_info 声明顺序返回节点可声明的输入名（required 在前，optional 在后）"""
        oi = self.get_object_info().get(class_type) or {}
        spec = oi.get("input") or {}
        names = list((spec.get("required") or {}).keys()) + list((spec.get("optional") or {}).keys())
        return names

    # ===================== 工作流加载与转换 =====================

    def load_workflow(self, workflow_file: str, return_meta: bool = False):
        """加载工作流文件并转换为 API prompt 格式"""
        workflow_path = resolve_workflow_path(workflow_file)
        with open(workflow_path, "r", encoding="utf-8-sig") as f:   # 兼容 UTF-8 BOM
            wf = json.load(f)
        return self.to_api(wf, return_meta=return_meta)

    @staticmethod
    def _is_api_format(wf: dict) -> bool:
        if not isinstance(wf, dict) or not wf:
            return False
        if "nodes" in wf or "links" in wf:
            return False
        return all(isinstance(v, dict) and "class_type" in v for v in wf.values())

    def to_api(self, wf: dict, return_meta: bool = False):
        """UI(node-graph) → API prompt"""
        if self._is_api_format(wf):
            meta = {"already_api": True, "node_count": len(wf)}
            return (wf, meta) if return_meta else wf
        api, meta = self._ui_to_api(wf)
        self.last_convert_meta = meta
        return (api, meta) if return_meta else api

    # ---------- 核心转换 ----------

    @staticmethod
    def _link_specs(links, prefix: str) -> Dict[Any, Tuple[str, int]]:
        """link 列表 → {link_id: ('node', 扁平节点id, 输出槽位)}"""
        out: Dict[Any, Tuple[str, int]] = {}
        for l in links or []:
            if isinstance(l, dict):
                lid, oid, oslot = l.get("id"), l.get("origin_id"), l.get("origin_slot", 0)
            elif isinstance(l, (list, tuple)) and len(l) >= 3:
                lid, oid, oslot = l[0], l[1], l[2]
            else:
                continue
            if lid is None or oid is None:
                continue
            out[lid] = ("node", f"{prefix}{oid}", oslot)
        return out

    def _ui_to_api(self, ui_workflow: dict) -> Tuple[dict, dict]:
        subgraphs: Dict[str, dict] = {}
        for sg in ((ui_workflow.get("definitions") or {}).get("subgraphs") or []):
            if sg.get("id"):
                subgraphs[sg["id"]] = sg

        flat: Dict[str, dict] = {}            # 扁平真实节点: id -> {type, named, seq}
        pending: List[tuple] = []             # (节点id, 输入名, 取值 spec)
        redirect: Dict[tuple, tuple] = {}      # (节点id, 槽位) -> spec（含 GetNode/Reroute/子图输出）
        set_map: Dict[str, tuple] = {}         # SetNode 名 -> spec
        inst_records: List[tuple] = []         # (顺序, clip号, 实例id, prompt 的 spec)
        warnings: List[str] = []
        seq = [0]

        def walk(nodes, links, prefix, depth, in_bindings, in_link_names, in_slot_names,
                 inode, onode):
            link_spec = self._link_specs(links, prefix)
            out_alias: Dict[int, tuple] = {}

            for raw in nodes or []:
                nid = raw.get("id")
                ntype = raw.get("type") or ""
                fid = f"{prefix}{nid}"
                named = dict(raw.get("widgets_values_named") or {})

                # 1) 子图实例（UUID 型 class_type）→ 递归展开
                sg = subgraphs.get(ntype)
                if sg and depth < 6:
                    inst_in: Dict[str, tuple] = {}
                    for inp in raw.get("inputs") or []:
                        nm, lid = inp.get("name"), inp.get("link")
                        if lid is not None and lid in link_spec:
                            inst_in[nm] = link_spec[lid]
                        elif nm in named:
                            inst_in[nm] = ("lit", named[nm])
                    pin_names: Dict[Any, str] = {}
                    slot_names: List[str] = []
                    for pin in sg.get("inputs") or []:
                        slot_names.append(pin.get("name"))
                        for lid in (pin.get("linkIds") or []):
                            pin_names[lid] = pin.get("name")
                    iid = (sg.get("inputNode") or {}).get("id")
                    oid = (sg.get("outputNode") or {}).get("id")
                    inner_alias = walk(sg.get("nodes"), sg.get("links") or [], fid + ":", depth + 1,
                                       inst_in, pin_names, slot_names, iid, oid)
                    for slot, spec in inner_alias.items():
                        redirect[(fid, slot)] = spec
                    # 记录该 clip 的提示词节点（供 10 段提示词精确替换）
                    if "prompt" in inst_in:
                        clip_no = None
                        m = re.search(r"(\d+)", str(named.get("prompt", "")))
                        if m:
                            clip_no = int(m.group(1))
                        inst_records.append((len(inst_records), clip_no, fid, inst_in["prompt"]))
                    continue

                # 2) 子图内部虚拟输入/输出节点
                if (inode is not None and nid == inode) or (onode is not None and nid == onode):
                    continue

                # 3) Set / Get / Reroute / PrimitiveNode → 重定向
                if ntype == "SetNode":
                    nm = named.get("Constant")
                    src = None
                    for inp in raw.get("inputs") or []:
                        if inp.get("link") in link_spec:
                            src = link_spec[inp["link"]]
                            break
                    if nm is not None and src is not None:
                        set_map[nm] = src
                    continue
                if ntype == "GetNode":
                    nm = named.get("Constant")
                    if nm in set_map:
                        redirect[(fid, 0)] = set_map[nm]
                    else:
                        warnings.append(f"GetNode {fid} 引用了未定义变量 {nm!r}")
                        redirect[(fid, 0)] = ("lit", None)
                    continue
                if ntype == "Reroute":
                    for inp in raw.get("inputs") or []:
                        if inp.get("link") in link_spec:
                            redirect[(fid, 0)] = link_spec[inp["link"]]
                            break
                    continue
                if ntype == "PrimitiveNode":
                    vals = [v for v in (raw.get("widgets_values") or []) if v is not None]
                    redirect[(fid, 0)] = ("lit", vals[0] if vals else None)
                    continue
                if ntype in VIRTUAL_NODE_TYPES:
                    continue

                # 4) 真实节点
                flat[fid] = {"type": ntype, "named": named, "seq": seq[0]}
                seq[0] += 1
                for inp in raw.get("inputs") or []:
                    nm, lid = inp.get("name"), inp.get("link")
                    if nm is None or lid is None or lid not in link_spec:
                        continue
                    spec = link_spec[lid]
                    # 子图内部：该输入来自子图外部绑定
                    if inode is not None and spec[1] == f"{prefix}{inode}":
                        bind_name = in_link_names.get(lid)
                        if bind_name is None and in_slot_names and spec[2] < len(in_slot_names):
                            bind_name = in_slot_names[spec[2]]
                        spec = (in_bindings or {}).get(bind_name, ("lit", None))
                    pending.append((fid, nm, spec))

            # 子图输出槽位 → 本层真实来源
            if onode is not None:
                for l in links or []:
                    if isinstance(l, dict) and l.get("target_id") == onode:
                        out_alias[l.get("target_slot", 0)] = (
                            "node", f"{prefix}{l.get('origin_id')}", l.get("origin_slot", 0))
                    elif isinstance(l, (list, tuple)) and len(l) >= 5 and l[3] == onode:
                        out_alias[l[4]] = ("node", f"{prefix}{l[1]}", l[2])
            return out_alias

        walk(ui_workflow.get("nodes") or [], ui_workflow.get("links") or [], "", 0,
             {}, {}, [], None, None)

        # ---------- 取值解析（跟随重定向链） ----------

        def resolve(spec, depth=0):
            if spec is None or depth > 16:
                return None
            if spec[0] == "lit":
                return spec
            if (spec[1], spec[2]) in redirect:
                return resolve(redirect[(spec[1], spec[2])], depth + 1)
            if spec[1] not in flat:
                return None
            return spec

        # 位置兜底所需的控件名（object_info 不可用时退化为 inputs 里的 widget 项）
        def fallback_widgets(raw):
            named = raw.get("widgets_values_named") or {}
            if named:
                return dict(named)
            widget_names = [i.get("name") for i in (raw.get("inputs") or []) if "widget" in i]
            values = list(raw.get("widgets_values") or [])
            if len(values) > len(widget_names):
                # 剔除伪控件取值（如 seed 后的 'randomize'）
                values = [v for idx, v in enumerate(values)
                          if not (idx > 0 and isinstance(v, str) and v in PSEUDO_VALUES)]
            while len(values) > len(widget_names):
                values.pop()
            return dict(zip(widget_names, values))

        # 重新遍历原始节点树取得「未使用 named 的节点」的兜底控件值
        raw_nodes_by_id: Dict[str, dict] = {}

        def collect(nodes, prefix):
            for raw in nodes or []:
                fid = f"{prefix}{raw.get('id')}"
                raw_nodes_by_id[fid] = raw
                sg = subgraphs.get(raw.get("type") or "")
                if sg:
                    collect(sg.get("nodes"), fid + ":")

        collect(ui_workflow.get("nodes") or [], "")

        # ---------- 组装 API prompt ----------

        pending_by_node: Dict[str, List[tuple]] = {}
        for pid, nm, spec in pending:
            pending_by_node.setdefault(pid, []).append((nm, spec))

        api: Dict[str, dict] = {}
        unresolved: List[str] = []
        for fid, info in sorted(flat.items(), key=lambda kv: kv[1]["seq"]):
            named = info["named"]
            if not named and fid in raw_nodes_by_id:
                named = fallback_widgets(raw_nodes_by_id[fid])
            inputs: Dict[str, Any] = {}
            ctype = info["type"]
            for k, v in named.items():
                if k in PSEUDO_WIDGETS:
                    continue
                if isinstance(v, str) and ctype.startswith("Load") and k in MEDIA_INPUT_KEYS:
                    # P0-5：按节点「读取目录」规范化标注——Load*Output 必须带 " [output]"，
                    # LoadImage 等 input 目录节点写裸文件名（不得带标注）
                    v = self.annotate_file_ref(v, ctype)
                inputs[k] = v
            for nm, spec in pending_by_node.get(fid, []):
                r = resolve(spec)
                if r is None:
                    unresolved.append(f"{fid}.{nm}")
                    continue
                if r[0] == "lit":
                    inputs[nm] = r[1]
                else:
                    inputs[nm] = [r[1], r[2]]
            api[fid] = {"class_type": info["type"], "inputs": inputs}

        # clip → 提示词节点映射（解析后回填）
        clip_prompt_nodes: List[tuple] = []
        for order, clip_no, fid, spec in inst_records:
            r = resolve(spec)
            if r and r[0] == "node":
                clip_prompt_nodes.append((clip_no if clip_no is not None else order + 1, r[1]))
        clip_prompt_nodes.sort(key=lambda x: x[0])

        meta = {
            "flat_nodes": len(api),
            "subgraph_count": len(subgraphs),
            "clip_prompt_nodes": clip_prompt_nodes,
            "unresolved_inputs": unresolved,
            "warnings": warnings,
        }
        if unresolved:
            logger.warning(f"UI→API 转换存在未解析输入 {len(unresolved)} 个: {unresolved[:5]}")
        logger.info(f"UI→API 完成: 真实节点 {len(api)} 个, 子图 {len(subgraphs)} 个, "
                    f"clip→提示词节点 {clip_prompt_nodes}")
        return api, meta

    # ---------- 转换结果自检 ----------

    def validate_api_prompt(self, api_prompt: dict, verbose: bool = False) -> dict:
        """用 object_info 校验转换结果：未知节点类型 / 缺失必填输入 / 悬空连线"""
        oi = self.get_object_info()
        ids = set(api_prompt.keys())
        unknown_types, missing_required, dangling, unexpected = [], [], [], []
        for nid, node in api_prompt.items():
            ct = node.get("class_type")
            spec = (oi.get(ct) or {}).get("input") if oi else None
            if oi and ct not in oi:
                unknown_types.append(f"{nid}:{ct}")
            for k, v in (node.get("inputs") or {}).items():
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                    if v[0] not in ids:
                        dangling.append(f"{nid}.{k} -> {v[0]}")
                elif spec is not None:
                    declared = (spec.get("required") or {}) | (spec.get("optional") or {})
                    if k not in declared and not _is_dynamic_child(declared, k):
                        unexpected.append(f"{nid}.{k}")
            if spec:
                for req in (spec.get("required") or {}).keys():
                    if req in (node.get("inputs") or {}):
                        continue
                    # autogrow 输入（COMFY_AUTOGROW_V3，如 TextEncodeQwenImage21.images）：
                    # object_info 里它挂在 required 下，但服务端 `_expand_schema_for_dynamic`
                    # 会把模板名展开成**点号键**（images.image_1 …）并全部登记为 optional，
                    # `template.min=0` 时一个都不传也完全合法（execute 收到 {}）。
                    # 所以：min=0 → 永不算缺失；min>0 → 有任一 `req.xxx` 键即满足。
                    req_spec = (spec.get("required") or {}).get(req) or []
                    if req_spec and req_spec[0] == "COMFY_AUTOGROW_V3":
                        extra = req_spec[1] if len(req_spec) > 1 and isinstance(req_spec[1], dict) else {}
                        if (extra.get("template") or {}).get("min", 0) == 0:
                            continue
                        if any(str(k).startswith(f"{req}.") for k in (node.get("inputs") or {})):
                            continue
                    missing_required.append(f"{nid}({ct}).{req}")
        report = {
            "node_count": len(api_prompt),
            "unknown_types": unknown_types,
            "missing_required": missing_required,
            "dangling_links": dangling,
            "unexpected_inputs": unexpected,
        }
        if verbose or unknown_types or missing_required or dangling:
            logger.info(f"转换自检: {json.dumps(report, ensure_ascii=False)[:600]}")
        return report

    # ===================== 资源与队列 =====================

    def upload_image(self, image_path: str, name: str = None,
                     subfolder: str = "", image_type: str = "input") -> str:
        """上传图片到 ComfyUI（input / output / temp 目录）

        ⚠️ ComfyUI 的 /upload/image 只接受**裸文件名**：
        `name` 里带 "/" 而 subfolder 为空时，服务端会拿这个相对路径去 join 一个
        不存在的目录 → 直接 **500 "Server got itself in trouble"**（实测复现）。
        而调用方习惯把「项目/类型/资产」目录一起塞进 name（见 generate_multiview
        的 filename_prefix），于是「资产基础图上传」100% 500，多视角链路整条断掉。
        这里统一做归一化：name 里的目录部分挪到 subfolder 参数，name 只留文件名。
        返回格式不变（"子目录/文件名"），下游 LoadImageOutput 的 [output] 标注照旧。
        """
        url = f"{self.base_url}/upload/image"
        if name is None:
            name = os.path.basename(image_path)
        # 归一化：反斜杠统一；按 "/" 切段（丢弃空段，兼容首/尾/重复斜杠）；
        # 末段是文件名，前面所有段是目录 → 目录进 subfolder，name 只留裸文件名。
        # 末尾带 "/" 视为「只给了目录」→ 文件名回落本地图片的 basename
        # （否则 "d/" 会被当成名为 "d" 的无扩展名文件静默传上去）。
        raw = str(name).replace("\\", "/")
        parts = [p for p in raw.split("/") if p]
        if parts and raw.endswith("/"):
            head, tail = "/".join(parts), os.path.basename(image_path)
        elif len(parts) > 1:
            head, tail = "/".join(parts[:-1]), parts[-1]
        elif parts:
            head, tail = "", parts[0]
        else:
            head, tail = "", os.path.basename(image_path)
        name = tail
        if head:
            subfolder = "/".join(p for p in (str(subfolder).strip("/"), head) if p)
        with open(image_path, "rb") as f:
            files = {"image": (name, f, "image/png")}
            data = {"overwrite": "true", "type": image_type}
            if subfolder:
                data["subfolder"] = subfolder
            resp = requests.post(url, files=files, data=data, timeout=120)
            resp.raise_for_status()
            result = resp.json()
            uploaded = result.get("name", name)
            sub = result.get("subfolder") or ""
            return f"{sub}/{uploaded}" if sub else uploaded

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

    @staticmethod
    def _next_node_id(api_prompt: dict) -> str:
        """取一个未占用的数字节点 id（API prompt 的键是字符串 id）。

        子图展开可能产生 ``"5:3"`` 这类复合 id，求 max 时只认纯数字 id；
        结果仍与现有键冲突则继续自增（防御性兜底，理论到不了）。
        """
        nums = [int(k) for k in api_prompt if str(k).isdigit()]
        nid = (max(nums) + 1) if nums else 1
        while str(nid) in api_prompt:
            nid += 1
        return str(nid)

    def _collect_enhancer_ref_sources(self, api_prompt: dict,
                                      encoder_id: str) -> List[str]:
        """收集正向编码节点已连接的参考图源节点 id（按槽位序、去重、≤8 路）。

        只认「该路真的持有图片」的 LoadImage* 节点：沿连线回溯（兼容中间隔一层
        缩放节点的老接法），且其 image 值非空（generate_storyboard 会把多余槽位
        整节点摘除，这里再兜一道空文件名）。8 路满后多余的参考图不接 ——
        增强节点只见前 8 张，提示词照常增强。
        """
        node = api_prompt.get(encoder_id) or {}
        inputs = node.get("inputs") or {}
        keys = sorted((k for k in inputs if _IMAGE_SLOT_RE.match(str(k))),
                      key=lambda k: _slot_index(k))
        sources: List[str] = []
        seen: set = set()
        for key in keys:
            value = inputs.get(key)
            if not (isinstance(value, list) and len(value) == 2):
                continue
            load_id = self._trace_load_image(api_prompt, value, depth=2)
            if not load_id or load_id in seen:
                continue
            src = api_prompt.get(load_id) or {}
            if not str((src.get("inputs") or {}).get("image") or "").strip():
                continue        # 空文件名：该路不产出 IMAGE（「槽位留空」语义）
            seen.add(load_id)
            sources.append(load_id)
            if len(sources) >= len(PROMPT_ENHANCER_IMAGE_FIELDS):
                break
        return sources

    def _detach_prompt_enhancer(self, api_prompt: dict, encoder_id: str = None,
                                pos_field: str = None,
                                fallback_text: str = None) -> dict:
        """把模板里**固化**的增强节点从本次提交中摘掉，恢复正向提示词为纯文本。

        背景（2026-10-07）：增强节点已写进 5 个 QwenImage2.1 图片链路模板，并且
        **硬连线**到编码节点的 prompt 输入。因此当运行时判定「本次不增强」时
        （开关关闭 / 凭据缺失 / 「确切文字」硬约束保护 / 插件不可用），绝不能原样
        提交——那样 ComfyUI 会拿**空的**「输入提示词」和空 api_key 去跑增强节点，
        轻则空提示词出图，重则整单失败。fail-open 必须连带摘除节点：

          1. 编码节点 prompt 若仍指向增强节点输出 → 改回 fallback_text 纯文本；
          2. 从 API 图里删掉增强节点本身；
          3. 删节点后自然不再有它的参考图输入（无悬空连线）。

        返回就地修改后的 api_prompt（永不抛异常，摘除失败也返回原图）。
        """
        try:
            if not isinstance(api_prompt, dict) or not api_prompt:
                return api_prompt
            enh_id = next((nid for nid, n in api_prompt.items()
                           if isinstance(n, dict)
                           and str(n.get("class_type") or "") == PROMPT_ENHANCER_CLASS), None)
            if enh_id is None:
                return api_prompt
            if not encoder_id or encoder_id not in api_prompt:
                encoder_id = self._find_positive_text_node(api_prompt)
            if encoder_id and not pos_field:
                pos_field = self._positive_field(api_prompt, encoder_id)
            # 恢复纯文本：优先用调用方给的 fallback_text；否则用编码节点已有的字面量；
            # 再否则用增强节点「输入提示词」里的值（模板默认值）。
            text = fallback_text
            if (not isinstance(text, str) or not text.strip()) and encoder_id and pos_field:
                cur = (api_prompt[encoder_id].get("inputs") or {}).get(pos_field)
                if isinstance(cur, str) and cur.strip():
                    text = cur
            if not isinstance(text, str) or not text.strip():
                enh_in = (api_prompt.get(enh_id) or {}).get("inputs") or {}
                tv = enh_in.get(PROMPT_ENHANCER_PROMPT_FIELD)
                if isinstance(tv, str) and tv.strip():
                    text = tv
            if encoder_id and pos_field:
                if isinstance(text, str) and text.strip():
                    api_prompt[encoder_id]["inputs"][pos_field] = text
                else:
                    # 无可用原文（调用方未填提示词）→ 置空，绝不能留悬空连线
                    logger.warning("[提示词增强] 摘除固化节点时未取到原文提示词，"
                                   "正向字段已置空（该次生成本就缺提示词）")
                    api_prompt[encoder_id]["inputs"][pos_field] = ""
            api_prompt.pop(enh_id, None)
            logger.info("[提示词增强] 本次不增强，已摘除模板固化节点 %s，"
                        "正向提示词按原文提交（%d 字）",
                        enh_id, len(text) if isinstance(text, str) else 0)
            return api_prompt
        except Exception as e:  # noqa: BLE001
            logger.warning("[提示词增强] 摘除固化节点失败（原样提交）：%s", e)
            return api_prompt

    def _inject_prompt_enhancer(self, api_prompt: dict) -> dict:
        """按需把「提示词增强节点」插进本次提交的 API prompt（就地修改并返回）。

        设计演进（2026-10-07 用户反馈：增强节点只在运行时 API 图里、模板画布
        看不到，用户误以为没开增强）→ 增强节点已**固化**进 5 个 QwenImage2.1
        图片链路模板（角色/物品/场景/物品_参考图/分镜生成_Qwen21.json），
        本方法改为「检测模板里已有增强节点 → 只填输入」：

        - 模板已有增强节点：填「输入提示词」（原提示词全文）+「任务模式」
          （有参考图→图生图 / 无→文生图）+ api_key/api_base_url/model + 本地
          PE/主模型/mmproj + seed（按提示词 md5 派生），**不**新增节点 id；
          参考图连线保持模板原样（编码 images.image_N 的 Load* 源已在 graph 里
          接好），仅把本次「实际有图」的参考图路径写进 Load* 节点 inputs。
        - 模板无增强节点（外部 / 旧模板 / H3 视频 / 超分 / TTS）：保持原动态
          注入路径（new_id = max+1），与历史行为兼容。

        判定链（任一不满足即原样返回，零侵入）：
          1. 运行时开关 MJSCXT_PROMPT_ENHANCER（默认开）；
          2. 工作流含 TextEncodeQwenImage21 或已有 TE_Qwen_Image_2_1_Prompt_Enhancer；
          3. 幂等：本次提交尚未填充（增强节点「输入提示词」为空）；
          4. 凭据齐全；
          5. object_info 核对（插件已装、必填无缺口）。
        """
        try:
            if not isinstance(api_prompt, dict) or not api_prompt:
                return api_prompt
            if not _prompt_enhancer_node_enabled():
                # 模板固化节点：开关关闭时也必须摘除，否则 ComfyUI 会拿空的
                # 「输入提示词」+ 空 api_key 跑增强节点（空提示词出图/整单失败）
                _enc0 = self._find_positive_text_node(api_prompt)
                _pos0 = self._positive_field(api_prompt, _enc0) if _enc0 else None
                _txt0 = None
                if _enc0 and _pos0:
                    _cur0 = (api_prompt[_enc0].get("inputs") or {}).get(_pos0)
                    if isinstance(_cur0, str):
                        _txt0 = _cur0
                return self._detach_prompt_enhancer(api_prompt, _enc0, _pos0, _txt0)

            existing_enh = next(
                (nid for nid, n in api_prompt.items()
                 if isinstance(n, dict)
                 and str(n.get("class_type") or "") == PROMPT_ENHANCER_CLASS),
                None)

            # ② 适用判定
            has_encoder = any(
                isinstance(n, dict)
                and str(n.get("class_type") or "") in COMBINED_PROMPT_NODE_TYPES
                for n in api_prompt.values())
            if not (has_encoder or existing_enh):
                return api_prompt

            # ③ 幂等：模板里已有增强节点 → 只填参数；否则走动态注入
            encoder_id = self._find_positive_text_node(api_prompt)
            pos_field = self._positive_field(api_prompt, encoder_id) if encoder_id else None
            original_text = None
            if encoder_id and pos_field:
                original_text = ((api_prompt[encoder_id].get("inputs") or {})
                                 .get(pos_field))

            if existing_enh:
                # ===== 模板已有增强节点：只填输入，不新增节点 =====
                enh = api_prompt[existing_enh]
                enh_inputs = enh.get("inputs") or {}
                # 原提示词可能在编码节点（模板 prompt 是 widget 控件，_ui_to_api
                # 会把它写进编码节点 inputs）——若编码节点 prompt 已是增强节点
                # 连线（[enh_id, 0]），原提示词在增强节点「输入提示词」控件里
                src_text = None
                if isinstance(original_text, str) and original_text.strip():
                    src_text = original_text
                elif isinstance(enh_inputs.get(PROMPT_ENHANCER_PROMPT_FIELD), str) \
                        and enh_inputs[PROMPT_ENHANCER_PROMPT_FIELD].strip():
                    src_text = enh_inputs[PROMPT_ENHANCER_PROMPT_FIELD]
                if not src_text:
                    return self._detach_prompt_enhancer(
                        api_prompt, encoder_id, pos_field, None)
                if SCENE_EXACT_TEXT_MARKER in src_text:
                    logger.info("[提示词增强] 检测到「确切文字」硬约束（surface_text），"
                                "跳过增强注入以保证硬约束逐字保留，按原提示词提交")
                    return self._detach_prompt_enhancer(
                        api_prompt, encoder_id, pos_field, src_text)
                # 任务模式判定：优先看模板固化增强节点自身连了几路参考图（graph 里
                # 接的是 LoadImage* 源 → API 里表现为输入值是 2 元素 list）；
                # 增强节点没连任何参考图时，再退回看编码节点 image_N 槽位的
                # Load* 源是否真有图（兼容老 3 槽紧凑模板）。
                enh_conn_refs = [
                    k for k, v in (enh_inputs.items()
                                   if isinstance(enh_inputs, dict) else [])
                    if k in PROMPT_ENHANCER_IMAGE_FIELDS
                    and isinstance(v, list) and len(v) == 2]
                has_ref = bool(enh_conn_refs)
                if not has_ref and encoder_id and encoder_id in api_prompt:
                    for k, v in (api_prompt[encoder_id].get("inputs") or {}).items():
                        if _IMAGE_SLOT_RE.match(str(k)) and isinstance(v, list) and len(v) == 2:
                            load = api_prompt.get(str(v[0]))
                            if isinstance(load, dict):
                                ctype = str(load.get("class_type") or "")
                                im = (load.get("inputs") or {}).get("image", "")
                                if ctype.startswith("LoadImage"):
                                    if im and str(im).strip():
                                        has_ref = True
                                        break
                                elif ctype.startswith("ImageScale") or ctype.startswith("FluxKontext"):
                                    inner = load.get("inputs") or {}
                                    for iv in inner.values():
                                        if isinstance(iv, list) and len(iv) == 2:
                                            load2 = api_prompt.get(str(iv[0]))
                                            if isinstance(load2, dict) and str(load2.get("class_type") or "").startswith("LoadImage"):
                                                im2 = (load2.get("inputs") or {}).get("image", "")
                                                if im2 and str(im2).strip():
                                                    has_ref = True
                                                    break
                                        if has_ref:
                                            break
                                    break
                if has_ref:
                    logger.info("[提示词增强] 模板含参考图连线（%d 路），任务模式=图生图",
                                len(enh_conn_refs))
                base_url, api_key, model = _enhancer_text_credentials()
                if not (base_url and api_key and model):
                    logger.warning("[提示词增强] AI 设置·文本分析模型未配置齐全"
                                   "（base_url/api_key/model），本次按原提示词提交")
                    return self._detach_prompt_enhancer(
                        api_prompt, encoder_id, pos_field, src_text)
                # 填增强节点输入（保留模板里已连的参考图连线 —— 参考图是 graph 层
                # 固化连线，运行时**不重建**，只覆盖本次要改的 widget 值）
                enh_inputs[PROMPT_ENHANCER_PROMPT_FIELD] = src_text
                enh_inputs["任务模式"] = (PROMPT_ENHANCER_MODE_I2I if has_ref
                                       else PROMPT_ENHANCER_MODE_T2I)
                enh_inputs["api_key"] = api_key
                enh_inputs["api_base_url"] = base_url
                enh_inputs["model"] = model
                enh_inputs.update(dict(PROMPT_ENHANCER_LOCAL_DEFAULTS))
                # seed：按提示词内容派生（同一条提示词增强结果稳定 —— 质检重试/换种子
                # 重试时增强文本不变，便于对照；不同镜头互不相同）
                try:
                    enh_inputs["seed"] = int(
                        hashlib.md5(src_text.encode("utf-8", "ignore")).hexdigest()[:8], 16)
                except Exception:  # noqa: BLE001
                    enh_inputs["seed"] = 0
                enh["inputs"] = enh_inputs
                # 确认编码节点 prompt 已指向增强节点（模板固化连线）
                if encoder_id and pos_field and encoder_id in api_prompt:
                    cur = (api_prompt[encoder_id].get("inputs") or {}).get(pos_field)
                    if not (isinstance(cur, list) and len(cur) == 2 and str(cur[0]) == existing_enh):
                        api_prompt[encoder_id]["inputs"][pos_field] = [existing_enh, 0]
                logger.info("[提示词增强] 使用模板固化节点 %s：任务模式=%s，"
                            "原提示词 %d 字先增强后编码",
                            existing_enh,
                            "图生图" if has_ref else "文生图",
                            len(src_text))
                return api_prompt

            # ===== 模板无增强节点：原动态注入路径（兼容旧/外部模板）=====
            # ③ 幂等（旧版口径）：正向字段仍是文本控件
            if not encoder_id or not pos_field:
                return api_prompt
            if not isinstance(original_text, str) or not original_text.strip():
                return api_prompt
            if SCENE_EXACT_TEXT_MARKER in original_text:
                logger.info("[提示词增强] 检测到「确切文字」硬约束（surface_text），"
                            "跳过增强注入以保证硬约束逐字保留，按原提示词提交")
                return api_prompt
            base_url, api_key, model = _enhancer_text_credentials()
            if not (base_url and api_key and model):
                logger.warning("[提示词增强] AI 设置·文本分析模型未配置齐全"
                               "（base_url/api_key/model），本次按原提示词提交")
                return api_prompt
            oi = self.get_object_info() or {}
            spec = oi.get(PROMPT_ENHANCER_CLASS)
            if not isinstance(spec, dict) or not spec:
                logger.warning("[提示词增强] object_info 中无 %s（插件未安装或 "
                               "object_info 不可用），本次按原提示词提交",
                               PROMPT_ENHANCER_CLASS)
                return api_prompt
            decl = spec.get("input") or {}
            declared_spec = {**(decl.get("required") or {}),
                             **(decl.get("optional") or {})}
            ref_sources = self._collect_enhancer_ref_sources(api_prompt, encoder_id)
            mode = PROMPT_ENHANCER_MODE_I2I if ref_sources else PROMPT_ENHANCER_MODE_T2I
            enh_inputs: Dict[str, Any] = {
                PROMPT_ENHANCER_PROMPT_FIELD: original_text,
                "任务模式": mode,
            }
            for field, src_id in zip(PROMPT_ENHANCER_IMAGE_FIELDS, ref_sources):
                enh_inputs[field] = [src_id, 0]
            enh_inputs["api_key"] = api_key
            enh_inputs["api_base_url"] = base_url
            enh_inputs["model"] = model
            enh_inputs.update(dict(PROMPT_ENHANCER_LOCAL_DEFAULTS))
            try:
                enh_inputs["seed"] = int(
                    hashlib.md5(original_text.encode("utf-8", "ignore")).hexdigest()[:8], 16)
            except Exception:  # noqa: BLE001
                enh_inputs["seed"] = 0
            dropped_ref: List[str] = []
            dropped_core: List[str] = []
            for k in list(enh_inputs):
                if k in declared_spec or _is_dynamic_child(declared_spec, k):
                    continue
                (dropped_ref if k in PROMPT_ENHANCER_IMAGE_FIELDS
                 else dropped_core).append(k)
                enh_inputs.pop(k, None)
            if dropped_core:
                logger.warning("[提示词增强] 增强节点未声明以下输入 %s（插件版本可能与"
                               "预期不符），本次按原提示词提交", dropped_core)
                return api_prompt
            if dropped_ref:
                logger.warning("[提示词增强] 增强节点未声明以下参考图槽位，已丢弃这些路"
                               "（其余参考图与提示词增强不受影响）：%s", dropped_ref)
            missing = [k for k, v in (decl.get("required") or {}).items()
                       if k not in enh_inputs
                       and not (isinstance(v, (list, tuple)) and v
                                and v[0] == "COMFY_AUTOGROW_V3")]
            if missing:
                logger.warning("[提示词增强] 增强节点缺少必填输入 %s"
                               "（插件版本可能与预期不符），本次按原提示词提交", missing)
                return api_prompt
            new_id = self._next_node_id(api_prompt)
            api_prompt[new_id] = {"class_type": PROMPT_ENHANCER_CLASS, "inputs": enh_inputs}
            api_prompt[encoder_id]["inputs"][pos_field] = [new_id, 0]
            logger.info("[提示词增强] 已注入 %s（节点 %s）：任务模式=%s，参考图 %d 路，"
                        "文本模型=%s，原提示词 %d 字先增强后编码",
                        PROMPT_ENHANCER_CLASS, new_id, mode, len(ref_sources),
                        model, len(original_text))
            return api_prompt
        except Exception as e:  # noqa: BLE001  fail-open：注入失败绝不阻断生成
            logger.warning("[提示词增强] 注入失败（按原提示词提交）：%s", e)
            # ⭐ 2026-10-08 兜底加固：模板已把编码 prompt **硬连线**到增强节点，
            #   上面任何一步中途抛错（半填充状态）时若原样提交，ComfyUI 会拿
            #   空的「输入提示词」+ 空 api_key 去跑增强节点 —— 正是本次要消灭的
            #   「空提示词出图 / 整单失败」形态。这里再兜一层：编码 prompt 仍指向
            #   增强节点时，还原为文本并摘除节点（本兜底自身异常也忽略）。
            try:
                _enhs = [k for k, n in (api_prompt or {}).items()
                         if isinstance(n, dict)
                         and str(n.get("class_type") or "") == PROMPT_ENHANCER_CLASS]
                if _enhs:
                    _enc = self._find_positive_text_node(api_prompt)
                    _pos = self._positive_field(api_prompt, _enc) if _enc else None
                    _cur = (((api_prompt.get(_enc) or {}).get("inputs") or {}).get(_pos)
                            if (_enc and _pos) else None)
                    _wired = (isinstance(_cur, list) and len(_cur) == 2
                              and str(_cur[0]) in {str(x) for x in _enhs})
                    if _wired:
                        _txt = next(
                            (v for v in (
                                ((api_prompt.get(x) or {}).get("inputs") or {})
                                .get(PROMPT_ENHANCER_PROMPT_FIELD) for x in _enhs)
                             if isinstance(v, str) and v.strip()), None)
                        api_prompt = self._detach_prompt_enhancer(
                            api_prompt, _enc, _pos, _txt)
                        logger.warning("[提示词增强] 已兜底摘除增强节点（编码 prompt 仍指向它），"
                                       "避免空提示词/空 api_key 提交")
                    else:
                        # 编码 prompt 已是字面量（异常发生在上一步）→ 增强节点是**孤儿**，
                        # 虽然 ComfyUI 从输出节点遍历不会执行它，但仍摘掉，避免把
                        # 「空输入提示词 + 空 api_key」的半成品节点带进提交图。
                        for _x in _enhs:
                            api_prompt.pop(_x, None)
                        logger.warning("[提示词增强] 已兜底摘除未被引用的增强节点 %s（异常中断残留）",
                                       ",".join(str(x) for x in _enhs))
            except Exception as e2:  # noqa: BLE001
                logger.warning("[提示词增强] 兜底摘除失败（忽略，按原图提交）：%s", e2)
            return api_prompt

    def queue_prompt(self, api_prompt: dict) -> str:
        # TE_MAN 提示词增强节点运行时注入（2026-10-06）：所有图片提交路径在此收口，
        # 预检/风格清理等文本工序都已完成，注入的是最终文本；条件不满足时原样返回
        # （判定链与 fail-open 口径见 _inject_prompt_enhancer）。
        api_prompt = self._inject_prompt_enhancer(api_prompt)
        # 提交前「模型名对齐」（2026-10-08）：模板 / 代码默认值 / 用户选定里写死的模型名
        # 会因目录改名而失效（实测 VAELoader 86 的 vae_name、增强节点的 mmproj 各炸一次：
        # /prompt 400 → 整集视频被丢弃）。这里用 object_info（ComfyUI 自己校验用的权威
        # 列表）把模型名改写成当前布局下的真实地址；匹配不上只告警、不阻断（fail-open）。
        try:
            import comfyui_models  # 函数内导入：与本模块无环依赖，也规避导入顺序问题
            comfyui_models.align_prompt_models(
                api_prompt, object_info=self.get_object_info(), tag="提交前")
        except Exception as _ma_err:  # noqa: BLE001
            logger.warning("模型名对齐调用失败（按原样提交）：%s", _ma_err)
        payload = {"prompt": api_prompt, "client_id": self.client_id}
        try:
            result = self._post("/prompt", payload)
        except Exception:
            _bump("prompt_failed")
            raise
        if "error" in result:
            _bump("prompt_failed")
            raise RuntimeError(f"ComfyUI 队列错误: {result['error']}")
        _bump("prompt_submitted")
        return result.get("prompt_id", "")

    def get_history(self, prompt_id: str) -> dict:
        return self._get(f"/history/{prompt_id}")

    def clear_history(self) -> bool:
        """清空 ComfyUI **任务历史列表**（``POST /history {"clear":true}``）。

        为什么要清：ComfyUI 界面的「任务历史」面板**只增不减**，而质检每失败一次
        重跑就多一条记录 —— 跑几轮下来面板里几百条，容易被误读成「生成了大量废图」。
        实测某项目面板 162 条时，磁盘上真正残留的废弃分镜图 **0 张**。

        ⚠️ 与磁盘产物完全无关：只清内存/磁盘上的 `history` 记录，不删任何 output 文件，
        也不影响**正在执行或排队中**的任务（它们结束后会各自追加新记录）。

        永不抛异常：失败只 warning（清历史是「可观测性优化」，不能拖垮生产）。
        """
        try:
            self._post("/history", {"clear": True})
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI 任务历史清理失败（不影响生产）: {e}")
            return False

    def interrupt(self, prompt_id: str = None) -> None:
        """S9：向 ComfyUI 发 /interrupt，打断当前正在出队的任务。

        - `prompt_id` 为 None → 打断队列中**正在执行**的那个（ComfyUI 官方语义）；
        - 为具体 prompt_id → 仅当它仍在队列/执行中时才有效（配合 `delete_queued` 精准清理）。
        失败静默（网络抖了也不应让取消路径本身抛错拖垮上层）。
        """
        try:
            if prompt_id is None:
                self._post("/interrupt")
            else:
                self._post("/interrupt")
                self.delete_queued(prompt_id)
            logger.info(f"已请求 ComfyUI 打断远端任务: {prompt_id or '(当前出队)'}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI /interrupt 失败（不影响取消流程）: {e}")

    def delete_queued(self, prompt_id: str) -> None:
        """S9：把指定 prompt 从队列中删除（ComfyUI `POST /queue {"delete":[id]}`）。"""
        try:
            self._post("/queue", {"delete": [prompt_id]})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI 删除队列项失败（不影响取消流程）: {prompt_id}: {e}")

    def queue_state(self) -> dict:
        """查询 ComfyUI 队列状态 → ``{"running": n, "pending": n, "ok": bool}``。

        用于**删除类操作前的安全检查**。`sb_ref_*` 参考图是本轮分镜上传到
        ComfyUI output 目录的**临时引用文件**，收尾清理时若队列里还压着任务，
        删文件会让那些任务执行到 ``LoadImage`` 时直接报 ``FileNotFoundError``。

        实测根因（用户 2026-09-29 日志）：某镜 ``wait_for_completion`` **超时返回**
        或批次被中止后，任务其实仍留在 ComfyUI 队列里；收尾照常清理 `sb_ref` →
        那些任务开始执行时文件已被移入回收站 → **连续 7 个镜头 0.01s 失败**，
        分镜图整批全灭。故清理前先问一句「还有人在用吗」。

        ``ok=False`` 表示查询本身失败（ComfyUI 不可达）——调用方应当**保守处理**：
        宁可跳过清理（残留有滚动回收兜底），也不要在信息不明时删掉可能正被引用的文件。
        """
        try:
            d = self._get("/queue") or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("查询 ComfyUI 队列失败: %s", e)
            return {"running": 0, "pending": 0, "ok": False}
        return {"running": len(d.get("queue_running") or []),
                "pending": len(d.get("queue_pending") or []),
                "ok": True}

    def wait_for_completion(self, prompt_id: str, timeout: int = 3600) -> dict:
        """轮询远端任务直到完成。

        S9 增强（不改变返回契约——超时仍返回 `{}`，避免 ripple 到 6 处调用方）：
          ① 轮询内检查 `cancellation.should_stop()`（contextvar，无注册时恒 False，
             不影响普通 API 调用路径）；一旦收到「暂停/停止」→ 立即 `interrupt` +
             抛 `cancellation.Cancelled`（穿透到 pipeline 归一为 cancelled），不再白烧 GPU；
          ② 轮询用 `cancellation.sleep`（可被打断的短休眠），点了暂停最多 0.25s 就有反应，
             而不是等满 3s；
          ③ 超时（非中止）后也 `interrupt` 一次，避免「本地判超时、远端继续跑」的双重浪费。
        ⚠️ 注意（审计 S9 备注）：cancellation 检查点**刻意不放进 ComfyUI 渲染循环**
        （会留半成品）——这里加的是「超时/取消后的远端清理」，两者不冲突。
        B-21 P1-13：三态分离（completed / error / timeout）+ interrupt 定向到指定 prompt_id。
        超时和 error 的 interrupt 都传 prompt_id（不再打断队列中正在执行的其他任务）。
        """
        start = time.time()
        # ⭐ 2026-10-08：存活检测计数器。旧实现只轮询 history —— 一旦 ComfyUI 重启、
        #    历史被清、或提交被丢，这个 prompt_id 就**永远不会出现在 history 里**，
        #    于是死等满 timeout（默认 3600s = 1 小时！实测整集卡死 6~25 分钟无日志、
        #    队列为空、GPU 空闲）。连续 _MISS_LIMIT 次（≈15s）既不在 history 也不在
        #    队列 → 判定远端已丢失，立即返回 {}（调用方按「没拿到结果」重试）。
        _miss = 0
        _MISS_LIMIT = 5
        while time.time() - start < timeout:
            # ⓪ 轮询顺手确保 temp 目录存在（exist_ok 幂等、零开销）——ComfyUI 侧节点
            # 仍可能在 temp 下建工作目录，此为通用防御。
            try:
                os.makedirs(COMFYUI_TEMP_DIR, exist_ok=True)
            except OSError:
                pass
            # ① 中止信号：点「暂停」后立刻打断远端并抛出，让上层转 cancelled 而非干等
            if cancellation.should_stop():
                logger.warning(f"等待期间收到中止信号，打断远端任务 {prompt_id}")
                self.interrupt(prompt_id)
                raise cancellation.Cancelled(f"ComfyUI 远端等待期间收到中止信号：{prompt_id}")
            try:
                history = self.get_history(prompt_id)
                if prompt_id in history:
                    _miss = 0
                    entry = history[prompt_id]
                    status = entry.get("status", {}) or {}
                    if status.get("completed") or status.get("status_str") == "success":
                        _bump("completed")
                        _bump("waited_seconds", round(time.time() - start, 2))
                        return entry
                    if status.get("status_str") == "error":
                        logger.error(f"生成出错: {status}")
                        _bump("waited_seconds", round(time.time() - start, 2))
                        # B-21 P1-13：error 态也定向 interrupt（清理本 prompt 的残留队列项）
                        self.interrupt(prompt_id)
                        return entry
                else:
                    # ⭐ 2026-10-08 存活检测：history 里没有 → 再看队列里在不在。
                    try:
                        _q = self._get("/queue") or {}
                        _qids = [x[1] for x in (_q.get("queue_running") or [])
                                 if isinstance(x, (list, tuple)) and len(x) > 1]
                        _qids += [x[1] for x in (_q.get("queue_pending") or [])
                                  if isinstance(x, (list, tuple)) and len(x) > 1]
                    except Exception:  # noqa: BLE001
                        _qids = [prompt_id]   # 查询失败 → 保守当作还在跑，绝不误杀
                    if prompt_id in _qids:
                        _miss = 0
                    else:
                        _miss += 1
                        if _miss >= _MISS_LIMIT:
                            logger.warning(
                                "等待中的任务在 ComfyUI 已不存在（history/queue 均无 %s，"
                                "连续 %d 次）→ 判定远端丢失，放弃等待交调用方重试",
                                prompt_id, _miss)
                            _bump("lost", 1)
                            _bump("waited_seconds", round(time.time() - start, 2))
                            return {}
            except cancellation.Cancelled:
                raise  # 中止信号必须穿透，不能被轮询的通用 except 吞掉
            except Exception as e:
                logger.debug(f"轮询历史失败: {e}")
            # ② 可被打断的短休眠（3s 轮询间隔），暂停时最多 0.25s 即有反应。
            # ⚠️ 关键：`cancellation.sleep` 在收到中止信号时**直接抛 Cancelled**，
            # 会绕过循环顶部的 `should_stop()` 分支 —— 也就是说「点暂停」时
            # **不会执行 self.interrupt()，ComfyUI 上的任务会继续白跑**
            # （用户反馈「暂停要同步停止 comfyui 的任务」的直接原因）。
            # 因此这里自己捕获并补上远端中断，再原样抛出。
            try:
                cancellation.sleep(3)
            except cancellation.Cancelled:
                logger.warning(f"退避期间收到中止信号，主动打断远端任务 {prompt_id}")
                self.interrupt(prompt_id)
                raise
        # ③ 超时（非中止）：仍清理远端，避免本地判超时而远端白跑
        # B-21 P1-13：超时 interrupt 定向到指定 prompt_id（不再误伤队列中其他任务）
        logger.warning(f"等待超时: {prompt_id}（清理远端队列）")
        self.interrupt(prompt_id)
        _bump("waited_seconds", round(time.time() - start, 2))
        return {}

    def _stage_from_library(self, lib_files: List[str],
                            filename_prefix: str = None) -> List[str]:
        """把库里的产物**复制**到 ComfyUI output 目录，返回调用方期望的绝对路径。

        为什么要复制、而不是直接返回库路径：调用方拿到产物后会 shutil.move 走
        （见 app.py 资产链路：move 到 scratch 暂存区再质检）。若直接返回库路径，
        **第一次复用就会把库条目移走**、库当场失效 —— 复制一份出去，库保持只读语义。
        """
        subdir, stem = "comic_drama", "asset"
        if filename_prefix:
            norm = str(filename_prefix).replace(os.sep, "/").replace("\\", "/").strip("/")
            parts = [p for p in norm.split("/") if p]
            if len(parts) > 1:
                subdir, stem = "/".join(parts[:-1]), parts[-1]
            elif parts:
                stem = parts[0]
        out_dir = os.path.join(COMFYUI_OUTPUT_DIR, *subdir.split("/"))
        os.makedirs(out_dir, exist_ok=True)
        staged: List[str] = []
        for i, src in enumerate(lib_files):
            ext = os.path.splitext(src)[1] or ".png"
            dst = os.path.join(out_dir, "%s_reuse_%02d%s" % (stem, i + 1, ext))
            shutil.copy2(src, dst)
            staged.append(dst)
        return staged

    def _resume_history(self, outputs: List[str]) -> dict:
        """把台账里存的**产物绝对路径**还原成 get_output_files 认得的 history 结构。

        为什么要还原而不是另开一条返回通道：get_output_files 是按
        COMFYUI_OUTPUT_DIR + subfolder + filename 解析的，只要把绝对路径反推回
        «子目录 + 文件名»，复用路径就能**零改动**地穿过现有 6 处调用方 ——
        这也是本功能刻意选择「返回 history」而不是「返回文件表」的原因。

        不在 ComfyUI output 目录下的产物（人为挪过位置）无法用 history 语义表达，
        直接跳过 → 该文件不计入复用集合，调用方按「没拿到文件」正常处理。
        """
        outs: Dict[str, Any] = {}
        for i, p in enumerate(outputs or []):
            try:
                rel = os.path.relpath(str(p), COMFYUI_OUTPUT_DIR)
            except (ValueError, TypeError):
                continue
            if rel.startswith(".."):
                logger.warning("免重渲：产物不在 ComfyUI output 目录下，跳过复用：%s", p)
                continue
            sub = os.path.dirname(rel).replace("\\", "/")
            outs["_resume%d" % i] = {"files": [{"filename": os.path.basename(rel),
                                                "subfolder": sub}]}
        return outs

    def submit_resumable(self, api_prompt: dict, *, job_key: str = "",
                         timeout: int = None, file_ext: str = "",
                         label: str = "") -> Tuple[dict, str, bool]:
        """提交并等待，**带崩溃免重渲检查点**。返回 (history, prompt_id, resumed)。

        三种分流（见 comfyui_job_store 模块文档）：

        * **已完成**：台账里的产物仍存在 → 构造 history 直接返回，resumed=True，零渲染；
        * **在跑中**：远端仍有该 prompt → wait_for_completion **重连**，不重复提交；
        * **不可复用**：无台账 / 哈希不符 / 产物已删 / 远端报错 → 正常提交（现状行为）。

        job_key 为空 → 完全等价于旧的 queue_prompt + wait_for_completion，
        这样未接入任务键的路径行为零变化。

        fail-open 口径：台账或远端 history 查询任何异常都只降级为「本次不复用」，
        绝不阻断生产 —— 缓存是加速器，不是依赖。
        """
        timeout = timeout or 3600
        if not job_key:
            pid = self.queue_prompt(api_prompt)
            return self.wait_for_completion(pid, timeout=timeout), pid, False

        wf_hash = job_store.workflow_hash(api_prompt)
        rec = job_store.find(job_key)
        if rec and wf_hash and rec.get("workflow_hash") == wf_hash and rec.get("prompt_id"):
            old_pid = rec["prompt_id"]
            # ① 台账产物仍在磁盘上 → 零渲染复用
            outs = [p for p in (rec.get("outputs") or []) if p]
            if outs and all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in outs):
                logger.info("[免重渲] %s 命中台账，复用 %d 个已完成产物（prompt=%s）",
                            label or job_key, len(outs), old_pid)
                return ({"outputs": self._resume_history(outs),
                         "status": {"completed": True, "status_str": "success"}},
                        old_pid, True)
            # ② 台账产物被清理 → 问远端 history：完成则复用，仍在跑则重连
            entry = None
            try:
                hist = self.get_history(old_pid) or {}
                entry = hist.get(old_pid) if isinstance(hist, dict) else None
            except Exception as e:                       # noqa: BLE001
                logger.debug("免重渲：远端 history 查询失败，按重渲处理：%s", e)
            if isinstance(entry, dict):
                st = entry.get("status") or {}
                if st.get("completed") or st.get("status_str") == "success":
                    got = self.get_output_files(entry, file_ext)
                    if got and all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in got):
                        job_store.mark_done(job_key, got)
                        logger.info("[免重渲] %s 远端已完成且产物有效，复用 %d 个（prompt=%s）",
                                    label or job_key, len(got), old_pid)
                        return entry, old_pid, True
                elif st.get("status_str") == "error":
                    job_store.mark_failed(job_key, "remote_error")
                    logger.info("[免重渲] %s 远端该任务已失败，重新提交", label or job_key)
                else:
                    logger.info("[免重渲] %s 远端仍在队列/执行中，**重连等待**（不重复提交，prompt=%s）",
                                label or job_key, old_pid)
                    hist2 = self.wait_for_completion(old_pid, timeout=timeout)
                    got2 = self.get_output_files(hist2, file_ext)
                    if got2:
                        job_store.mark_done(job_key, got2)
                    return hist2, old_pid, False

        pid = self.queue_prompt(api_prompt)
        try:
            job_store.remember(job_key, wf_hash, pid, label=label or job_key)
        except Exception as e:                           # noqa: BLE001
            logger.debug("免重渲：台账登记失败（不影响生成）：%s", e)
        history = self.wait_for_completion(pid, timeout=timeout)
        try:
            got = self.get_output_files(history, file_ext)
            if got:
                job_store.mark_done(job_key, got)
            else:
                job_store.mark_failed(job_key, "no_output")
        except Exception as e:                           # noqa: BLE001
            logger.debug("免重渲：台账收尾记录失败（不影响生成）：%s", e)
        return history, pid, False

    def get_output_files(self, history: dict, file_ext: str = "") -> List[str]:
        files = []
        for _node_id, node_output in (history.get("outputs") or {}).items():
            for _key, value in (node_output or {}).items():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and "filename" in item:
                        filename = item["filename"]
                        subfolder = item.get("subfolder", "")
                        base = os.path.join(COMFYUI_OUTPUT_DIR, subfolder) if subfolder else COMFYUI_OUTPUT_DIR
                        full = os.path.join(base, filename)
                        if not file_ext or filename.endswith(file_ext):
                            files.append(full)
        return files

    # ---------- 路径工具（P0-4：HTTP 资源路径 → 本地绝对路径） ----------

    @staticmethod
    def resolve_local_path(path_or_url: str) -> Optional[str]:
        """把前端传来的 HTTP 资源路径（/api/assets/...、/api/videos/...）解析为本地绝对路径"""
        if not path_or_url or not isinstance(path_or_url, str):
            return None
        p = path_or_url.strip()
        if p.startswith("http://") or p.startswith("https://"):
            # 去掉协议与主机，仅保留路径部分
            rest = p.split("//", 1)[-1]
            p = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        p = p.replace("\\", "/")
        if p.startswith("/api/assets/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "assets", p[len("/api/assets/"):]))
        if p.startswith("/api/videos/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "videos", p[len("/api/videos/"):]))
        if p.startswith("/api/final/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "final", p[len("/api/final/"):]))
        if p.startswith("/api/storyboards/file/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "storyboards",
                                                 p[len("/api/storyboards/file/"):]))
        if os.path.exists(p):
            return os.path.normpath(p)
        return None

    # ---------- 目录标注工具（P0-5：按节点类型区分 [output] / 无标注） ----------

    @staticmethod
    def annotate_file_ref(filename: str, class_type: str, default_dir: str = ANNOTATED_DIR) -> str:
        """按「读取目录」语义为文件名型资源输入补/去目录标注。

        - Load*Output 系列（LoadImageOutput / LoadAudioOutput / ...）读 ComfyUI output 目录
          → 返回 "名字 [output]"
        - LoadImage / LoadAudio / LoadVideo 等读 input 目录 → 返回 "名字"（不带标注）
        - 其它类型 → 原样返回，不做任何加工（避免引入新错误）
        """
        if not filename or not isinstance(filename, str):
            return filename
        name = SUFFIX_RE.sub("", filename).strip()   # 先剥离可能已存在的标注，保证幂等
        if class_type in OUTPUT_LOAD_CLASSES or \
                (class_type.startswith("Load") and class_type.endswith("Output")):
            return f"{name} [{default_dir}]"
        if class_type in INPUT_LOAD_CLASSES or class_type.startswith("Load"):
            return name
        return filename

    # ===================== 第一阶段：基础图生成 =====================

    # ---------- P0 修复辅助：场景去人 / 冲突负向词清理 ----------

    @classmethod
    def clean_conflict_negative_tokens(cls, api_prompt: dict) -> dict:
        """从负向提示词槽位剔除与正向 3D 风格冲突的词（P0：风格冲突）

        只处理**极性为负向**的槽位，不改正向提示词。极性经 `_prompt_slot_polarity`
        判定（正负同体节点按字段名，老模板走内容启发式），因此 QwenImage2.1 的
        ``TextEncodeQwenImage21.negative_prompt`` 也能被清理（此前字段名不在白名单 → 整段跳过）。
        返回 {节点id.字段: 变更说明} 便于审计。
        """
        changed: dict = {}
        for nid, field, value, polarity in _iter_prompt_slots(api_prompt):
            if polarity != "neg" or not (value or "").strip():
                continue
            new = value
            removed: List[str] = []
            for tok in CONFLICT_NEGATIVE_SORTED:
                if tok in new:
                    new = new.replace(tok, "")
                    removed.append(tok)
            if not removed:
                continue
            new = re.sub(r"[，,、]\s*(?=[，,、])", "", new)
            new = re.sub(r"^\s*[，,、]+\s*", "", new)
            new = re.sub(r"[，,、\s]+$", "", new)
            (api_prompt[nid]["inputs"])[field] = new
            changed[f"{nid}.{field}"] = f"移除冲突负向词 {removed}"
        if changed:
            logger.info(f"[风格冲突清理] 负向提示词已修正 {len(changed)} 处: {changed}")
        return changed

    @classmethod
    def sanitize_scene_prompt(cls, prompt_zh: str) -> str:
        """场景提示词去人物（P0：场景资产带人）**且幂等**（2026-09-29 修正）

        场景定义常写「村民三两结伴」→ 资产图实测渲染出 4 人。这里按分句粒度丢弃含人物
        描述的短句，并追加空场景声明；若清洗后为空则回退原文（避免把场景描述清空）。

        ⚠️ 2026-09-29 修的是一个**真实的二次清洗缺陷**（勿删这段剥离逻辑）：
            本函数此前**不幂等** —— 末尾追加的 `SCENE_NO_CHARACTER_SUFFIX` 里带
            「人物 / 人影 / 人群 / 士兵」这些词，第二次调用时它自己会被当成
            「人物描述句」**整句丢弃**，然后再追加一遍。而**二次调用是常态**：
              · `generate_scene_base` 先清洗一次 → 再交给 `_generate_base_image`
                （内部按 asset_type=="scene" 又清洗一次）；
              · 新增的「按机位出图」（`view_key`）同样走这条路。
            后果是提示词被无谓改写（首段声明被吃掉、文本重复），
            进而出图内容随调用次数漂移 —— 且**只在场景链路出现**，非常隐蔽。
            修法：进入清洗前先剥掉**已有的**空场景声明，再统一清洗+追加一次。
            首次调用的输出与修正前**逐字一致**（存量行为不变）。
        """
        text = (prompt_zh or "").strip()
        if not text:
            return text
        if SCENE_NO_CHARACTER_SUFFIX in text:
            # 先摘掉旧声明再清洗，保证「清洗几次结果都一样」
            _stripped = text.replace(SCENE_NO_CHARACTER_SUFFIX, "").strip().rstrip("。;； ")
            if not _stripped:
                return text     # 输入只有声明本身 → 已是终态，直接返回（否则会再追加一遍）
            text = _stripped
        parts = [p.strip() for p in _PROMPT_SPLIT_RE.split(text)]
        kept: List[str] = []
        dropped: List[str] = []
        for p in parts:
            if not p:
                continue
            hit = any(w in p for w in CHARACTER_WORDS) or bool(CHARACTER_QTY_RE.search(p))
            if hit:
                dropped.append(p)
            else:
                kept.append(p)
        cleaned = "，".join(kept).strip()
        if not cleaned:
            cleaned = text      # 全句含人物时不至于清空，仅靠正向声明压制
        if dropped:
            logger.info(f"[场景去人] 丢弃人物描述句 {dropped}；清洗后场景描述: {cleaned[:80]}")
        return cleaned.rstrip("。;； ") + SCENE_NO_CHARACTER_SUFFIX

    def _find_positive_text_node(self, api_prompt: dict,
                                 class_types=PROMPT_NODE_TYPES) -> Optional[str]:
        """定位**承载正向文本的节点**（返回节点 id）。

        极性来源唯一：`_prompt_slot_polarity`。正负同体节点（TextEncodeQwenImage21）
        永远有正向字段，因此必然命中；老模板靠内容启发式区分正/负节点。
        """
        pos = [nid for nid, _f, _v, pol in _iter_prompt_slots(api_prompt, class_types)
               if pol == "pos"]
        if pos:
            return sorted(pos, key=_node_sort_key)[-1]
        # 退化：模板里只有负向槽位（极端情况），取 id 最大的提示词节点，避免直接失败
        pool = [nid for nid, n in (api_prompt or {}).items()
                if isinstance(n, dict) and n.get("class_type") in class_types]
        return sorted(pool, key=_node_sort_key)[-1] if pool else None

    def _positive_field(self, api_prompt: dict, node_id: str) -> Optional[str]:
        """返回该正向节点真正承载正向文本的**字段名**。

        必须定位字段而不是「把所有字符串输入都写成正向词」：
        ``TextEncodeQwenImage21`` 同节点里还有 ``negative_prompt``，后者被覆盖会
        让负向词整段失效（且不报错）。
        """
        node = (api_prompt or {}).get(node_id) or {}
        ctype = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        for field in PROMPT_TEXT_FIELDS:
            if _prompt_slot_polarity(ctype, field, inputs.get(field)) == "pos":
                return field
        return None

    def _find_negative_slot(self, api_prompt: dict,
                            class_types=PROMPT_NODE_TYPES) -> Optional[Tuple[str, str]]:
        """定位负向槽位，返回 ``(节点id, 字段名)``；找不到返回 None。

        返回字段名是必须的：正负同体节点的负向文本在 ``negative_prompt`` 上，
        老模板在 ``text`` / ``prompt`` 上——只回节点 id 会让调用方写错字段。
        """
        neg = [(nid, f) for nid, f, _v, pol in _iter_prompt_slots(api_prompt, class_types)
               if pol == "neg"]
        if not neg:
            return None
        return sorted(neg, key=lambda t: _node_sort_key(t[0]))[-1]

    def _generate_base_image(self, workflow_file: str, prompt_zh: str,
                             asset_type: str = None, seed: int = None,
                             style: str = "", size=None,
                             filename_prefix: str = None,
                             prompt_extra: str = "") -> List[str]:
        """通用基础图生成：更新正向提示词节点

        P0 修复（同轮补充）：
        - asset_type="scene" → 清洗人物描述并追加"空场景"声明（场景资产不得带人）；
        - clean_conflict_negative_tokens 剔除与 3D 正向风格冲突的负向词；
        - seed 用于质检不达标时的重生成（保证与上一版结果不同）。

        风格落地（2026-09-18 修复）：
        - style 非空 → 把风格后缀拼进正向提示词（此前完全没有这一步，用户敲定的风格
          一个资产都没落到提示词里）；
        - size 非空 → 覆写尺寸节点，让「竖屏 9:16」真正体现在画布上（此前尺寸来自
          模板硬编码的 1664×928 横向）。

        G8（资源清理）：filename_prefix 非空 → 覆写 SaveImageAdvanced/SaveImage 的
        filename_prefix，让资产基础图落在 `comic_drama/<项目>_asset_<类型>` 这种项目专属
        子目录，而非全部堆在 ComfyUI output 默认目录（此前删项目/滚动清理都够不着）。

        prompt_extra（2026-09-29 新增）：场景「按机位出图」用的**追加句**，插在
        「风格尾缀之后、场景去人之前」。位置是刻意的：
          · 在风格之后 —— 风格尾缀是全局收尾语，插在它前面会破坏「风格恒在末尾」的既有口径；
          · 在 sanitize 之前 —— 这样机位句也过一遍去人清洗（机位句本身不含人物词，
            只是让清洗成为**唯一入口**，避免出现「绕过清洗的提示词片段」）。
        为空时整条路径与旧实现逐字一致。
        """
        api_prompt, meta = self.load_workflow(workflow_file, return_meta=True)
        node_id = self._find_positive_text_node(api_prompt)
        if node_id is None:
            logger.error(f"{workflow_file} 中未找到正向提示词节点，转换元信息: {meta}")
            return []
        # 风格注入：必须在场景去人之前拼好，保证风格词不被 sanitize 丢掉
        # 参考图口径（2026-10-07）：剥离「色调/光影」token + 颜色保真条款，
        # 否则「色调灰蓝压抑」会把角色三视图的皮肤整体染蓝（用户实测反馈）。
        prompt_zh = style_kit.with_reference_style(prompt_zh, style) if style else prompt_zh
        if prompt_extra:
            prompt_zh = f"{str(prompt_zh).rstrip('。;； ')}{prompt_extra}"
        # 场景资产去人
        if asset_type == "scene":
            prompt_zh = self.sanitize_scene_prompt(prompt_zh)
        # 只写「承载正向文本的那一个字段」：
        #   老模板 CLIPTextEncode → text ；TextEncodeQwenImageEditPlus → prompt ；
        #   QwenImage2.1 TextEncodeQwenImage21 → prompt（同节点另有 negative_prompt）。
        # ⚠️ 旧实现是「非 text 字段就把 node 里所有字符串输入都写成正向提示词」——
        #    遇到正负同体的 TextEncodeQwenImage21 会把 negative_prompt 覆盖掉，负向词静默失效。
        pos_field = self._positive_field(api_prompt, node_id)
        if pos_field is None:
            logger.error(f"{workflow_file} 正向节点 {node_id} 未找到可写入的正向字段，"
                         f"输入字段={sorted((api_prompt[node_id].get('inputs') or {}).keys())}")
            return []
        api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        logger.info(f"[{workflow_file}] 正向提示词节点 {node_id}.{pos_field} 已更新"
                    f"（共 {len(api_prompt)} 节点）")
        self.clean_conflict_negative_tokens(api_prompt)                 # P0：清理风格冲突负向词
        # 负向词：按风格再压一批「画风打架」的词（如国漫风不该出现写实照片）
        if style:
            self._append_style_negative(api_prompt, style)
        # 画幅落地：尺寸节点覆写
        if size:
            hit = style_kit.apply_latent_size(api_prompt, size)
            if hit:
                logger.info("[%s] 画幅已按风格覆写为 %s×%s：%s",
                            workflow_file, size[0], size[1], ",".join(hit))
            else:
                logger.warning("[%s] 未找到尺寸节点，画幅 %s×%s 未能落地（沿用模板尺寸）",
                               workflow_file, size[0], size[1])
        if seed is not None:
            logger.info(f"资产采样种子已注入: {self._inject_seed(api_prompt, seed)}")

        # G8：覆写输出文件名前缀（项目专属子目录），避免资产基础图堆在 ComfyUI output 默认目录
        if filename_prefix:
            for _nid, _n in api_prompt.items():
                if _n.get("class_type") in ("SaveImageAdvanced", "SaveImage") \
                        and "filename_prefix" in _n["inputs"]:
                    _n["inputs"]["filename_prefix"] = filename_prefix

        # ---- 跨项目角色资产库（2026-09-29）：角色设定图是确定性产物 → 命中指纹即零渲染 ----
        # 只对 character 生效：物品/场景的「像不像」主观性更强、且各自携带机位与角度语义，
        # 复用风险明显更高（口径见 asset_library 模块文档）。
        _fp = ""
        if asset_type == "character" and asset_library.enabled():
            try:
                _fp = asset_library.fingerprint(
                    template=workflow_file, prompt=prompt_zh, style=style,
                    size=size, kind=str(asset_type))
                _hit = asset_library.lookup(_fp)
                if _hit:
                    _staged = self._stage_from_library(_hit, filename_prefix)
                    if _staged:
                        logger.info("[资产库] 角色基础图命中指纹 %s，复用 %d 张（零渲染）：%s",
                                    _fp, len(_staged),
                                    [os.path.basename(p) for p in _staged])
                        return _staged
            except Exception as e:                   # noqa: BLE001
                logger.warning("资产库查询失败（按未命中处理，正常出图）：%s", e)
                _fp = ""

        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id)
        files = self.get_output_files(history, ".png")
        if _fp and files:
            try:
                asset_library.store(_fp, files, meta={
                    "template": os.path.basename(str(workflow_file)),
                    "style": style, "size": list(size) if size else None,
                    "type": str(asset_type)})
            except Exception as e:                   # noqa: BLE001
                logger.warning("角色资产入库失败（不影响出图）：%s", e)
        return files

    def _append_style_negative(self, api_prompt: dict, style: str) -> None:
        """把「与目标风格冲突」的词追加到负向提示词**槽位**（找不到负向槽位则跳过）

        写入字段由 `_find_negative_slot` 给出：正负同体节点是 ``negative_prompt``，
        老模板是 ``text`` / ``prompt``——按字段写，不能靠猜。
        """
        negs = style_kit.negative_for_style(style)
        if not negs:
            return
        try:
            hit = self._find_negative_slot(api_prompt)
        except Exception:  # noqa: BLE001
            hit = None
        if not hit:
            return
        node_id, field = hit
        node = api_prompt[node_id]
        cur = node.get("inputs", {}).get(field)
        if not isinstance(cur, str):
            return
        node["inputs"][field] = (cur.rstrip("，,。") + "，" + "，".join(negs)) if cur.strip() else "，".join(negs)

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

    def generate_character_base(self, prompt_zh: str, seed: int = None,
                                style: str = "", size=None,
                                filename_prefix: str = None) -> List[str]:
        logger.info(f"生成角色基础图: {prompt_zh[:50]}...")
        # 角色基础图 = 设定集（三视图横排）。必须确定性注入「全身 + 横排三视图」版式硬约束：
        # 剧本层提示词只写外貌特征、不含构图约束（风格词也由程序追加，版式同属构图维度），
        # 不注入则模型默认半身/胸像构图且三格版式不可控，多视角与分镜一致性都会崩坏
        # （2026-09-19 实测：三视图出成半身，且同图内人物身高比例不一致）。
        prompt_zh = self._ensure_fullbody_prompt(prompt_zh, style)
        # ⭐ 2026-10-06（用户指定）：角色未明说国籍/人种时，**一律默认中国人（亚洲面孔）**。
        # 扩散模型在"没写国籍"时会按自身偏置默认出欧美面孔；这里确定性补默认约束，
        # 仅当提示词未出现「外国/西方/欧美/黑人/白人/混血」等明说非华人词时才追加，避免误伤。
        prompt_zh = self._ensure_cn_default_ethnicity(prompt_zh)
        return self._generate_base_image(WORKFLOW_TEMPLATE["character_gen"], prompt_zh,
                                         asset_type="character", seed=seed, style=style, size=size,
                                         filename_prefix=filename_prefix)

    @staticmethod
    def _ensure_cn_default_ethnicity(prompt_zh: str) -> str:
        """角色提示词补「未明说国籍则默认中国人（亚洲面孔）」约束（幂等）。

        扩散模型在角色提示词**未写国籍/人种**时，会按自身偏置默认出欧美面孔。
        用户 2026-10-06 明确：角色没有明说是外国人的情况下，都默认是中国人。
        故在此确定性追加默认约束，且**仅在提示词未明说非华人种族时**才追加（避免误伤
        「欧洲贵族」「黑人」「混血儿」等已显式指定人种的角色）。
        """
        text = (prompt_zh or "").strip()
        if not text:
            return text
        # 已明说非华人种族 / 外国 → 不追加，尊重显式指定
        _foreign_markers = ("外国", "外籍", "西方", "欧美", "欧式", "欧洲", "西洋",
                            "白人", "黑人", "金发碧眼", "混血", "高加索", "拉丁",
                            "阿拉伯", "印度", "日本", "韩国", "高句丽", "棒子",
                            "欧美面孔", "白人面孔", "外国人", "洋人", "高鼻深目")
        if any(m in text for m in _foreign_markers):
            return text
        # 已显式写了中国人/亚洲面孔 → 不追加（幂等）
        if any(m in text for m in ("中国人", "中华", "东亚面孔", "亚洲面孔", "黄皮肤", "汉族",
                                    "中国面孔", "东方面孔")):
            return text
        return (text.rstrip("。，,.;； ")
                + "；该角色为中国人，东亚面孔、亚洲五官特征、中国人种肤色，"
                  "除非另有说明否则默认中国文化背景")

    @staticmethod
    def _ensure_fullbody_prompt(prompt_zh: str, style: str = "") -> str:
        """给角色参考图提示词确定性地补**中文标注四区 character sheet 版式**约束（幂等）

        ⚠️ 2026-10-03（用户指定）：版式改为**中文文字排版**四区设定图 ——
        顶部三视图 / 左侧面部特写+配色板 / 底部局部细节 / 右侧身高比例，
        且画面必须**清晰渲染中文说明文字**（"三视图设定/正面/侧面/…" 结构标签 +
        从设定文本自动提取的**角色特征标签**，如「冰蓝眼眸」「哑光黑短外套」）。
        2026-10-02 的英文版式（结尾 "no text"）与该需求相反，已整段替换。

        ⚠️ 配套改动（勿只改这一半）：「四区」版式无法再被 `sheet_split` 的
        行列投影切分 → `_pick_char_view` 只取整图 base.png、`_generate_asset_task`
        角色分支不再切分（见 app.py 同批改动）。

        幂等：marker = "人物设定表"（suffix 字面子串，见 2026-09-25 的教训——
        marker 必须能被二次调用识别，否则无限追加）。
        """
        text = str(prompt_zh or "").strip()
        marker = "人物设定表"
        # 幂等判断在 with_style 之前做（同 style_kit._style_suffix 的坑）。
        # 旧英文版式 marker（"character sheet"）也视为已补 —— 避免历史已拼版式的
        # 字符串被再叠一层（宁可保持原样，也不出现双层版式）。
        if marker in text or "character sheet" in text:
            return text
        # 角色设定表属参考图 → 参考图口径（剥离色调/光影 token，防肤色染偏）
        base = style_kit.with_reference_style(text, style) if style else text
        if marker in base or "character sheet" in base:
            return base
        labels = ComfyUIClient._extract_trait_labels(text)
        labels_part = ("，以及该角色特征标签：" + "、".join(labels)) if labels else ""
        suffix = (ComfyUIClient._CHARACTER_SHEET_CN_LAYOUT
                  .replace("{labels_part}", labels_part))
        # 剧本层提示词常以「三视图。」收尾，直接拼会得到「三视图。，…」的脏标点
        base = base.rstrip("。，,.;； ")
        sep = "" if base.endswith((".", "！", "?", "？")) else "。"
        return (base + sep + suffix) if base else suffix

    #: 特征标签提取的**停用词**：命中即不作为设定图上的标注文字
    #: （这些是版式/质检/管线层的元词，不是角色特征；混进去会变成图上的乱注记）。
    _TRAIT_LABEL_STOPWORDS = (
        "设定", "锁定", "严格", "三视图", "参考图", "提示词", "全身", "半身",
        "构图", "背景", "高清", "细节", "一致", "动画", "渲染", "风格", "画质",
        "三维", "多视角", "正交", "比例尺", "标注", "文字", "品质", "质量",
        "cinematic", "character", "sheet", "view", "texture", "lighting",
    )

    @staticmethod
    def _extract_trait_labels(prompt_zh: str, limit: int = 12) -> List[str]:
        """从角色设定文本提取**特征标签**（设定图上要渲染的中文说明词）。

        口径：按中英文标点切短句 → 剥掉「穿着/佩戴」等动词前缀 → 丢弃空段/
        超长短句/停用词命中 → 去重保序。
        例：「男性，冰蓝色眼眸，黑发微乱，穿着哑光黑色短款外套，低帮战术靴」→
        ［男性, 冰蓝色眼眸, 黑发微乱, 哑光黑色短款外套, 低帮战术靴］
        标签**只来自设定原文**（不臆造），上限 limit 条防版式过挤。
        """
        out: List[str] = []
        seen = set()
        for seg in re.split(r"[，,、；;。：:\n\r\t]+", str(prompt_zh or "")):
            s = seg.strip().strip("“”\"'『』「」()（）【】 ")
            s = re.sub(r"^(?:穿着|佩着|佩戴|身着|戴着|穿上|穿|戴)", "", s).strip()
            if not s or len(s) < 2 or len(s) > 12:
                continue
            if any(w in s for w in ComfyUIClient._TRAIT_LABEL_STOPWORDS):
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
        return out

    #: 物品参考图里**不该出现**的承托物/位置描述。
    #: 实测（2026-10-01）：筑基丹的 reference_prompt_zh 被剧本 LLM 写成
    #: 「…，置于黑色玉盒中，玉盒有磨损痕迹。」→ 出图照着画成「黑碗盛丹药」，
    #: 而质检拿「图 vs 提示词」比对，图与提示词完全一致 → **判通过**。
    #: 物品参考图的职责只是「本体长什么样」，容器/承托物/摆放位置属于分镜内容，
    #: 一旦画进参考图，就会被当成该物品的规范外观带进后面每一镜。
    _ITEM_PLACEMENT_RE = re.compile(
        r"[，,；;]?\s*(?:被)?(?:置于|放在|摆放于|装于|盛于|收纳于|存放于|陈列于|托在|捧在)"
        r"[^，。；;]*")

    @staticmethod
    def _strip_item_placement(prompt_zh: str) -> str:
        """剥掉物品提示词里「置于/放在 X 中」这类**位置与承托物**描述（幂等）。

        只删「放在哪儿」这半句，物品本体（造型/材质/颜色/纹样/尺寸）一字不动。
        """
        text = str(prompt_zh or "")
        cleaned = ComfyUIClient._ITEM_PLACEMENT_RE.sub("", text)
        # 第二轮：按标点切子句，丢掉**以承托物开头**的子句。
        # 只删「置于…中」是不够的：实测原文是「…置于黑色玉盒中，玉盒有磨损痕迹。」
        # 删掉前半句后仍留着「玉盒有磨损痕迹」，模型照样会画出那个盒子。
        parts = re.split(r"([，,；;。])", cleaned)
        kept = []
        for seg in parts:
            s = seg.strip()
            if re.match(r"^[^，,；;。]{0,4}(?:盒|匣|托盘|盘|底座|支架|展示台|台座|碗|碟|绸布|锦垫|垫)", s):
                continue
            # 与「纯白背景」不变量直接矛盾的子句（优化器实测写出过「严禁全白纯色背景」）：
            # 留着会与守卫函数追加的「，纯白背景…」形成指令冲突，模型必然摇摆。
            if re.search(r"(?:严禁|禁止|不要|避免|无需|去掉)[^，,；;。]{0,14}(?:白|纯色)[^，,；;。]{0,8}背景", s):
                continue
            # 承托物被写成画面核心/主体（实测：「画面核心为黑色玉盒及其内部丹药」）
            if re.search(r"(?:盒|匣|托盘|底座|支架|展示台|台座|碗|碟|绸布|锦垫)", s) and \
                    re.search(r"(核心|主体|中心|主要)", s):
                continue
            kept.append(seg)
        cleaned = "".join(kept)
        cleaned = cleaned.replace("。。", "。").replace("，。", "。").replace("；。", "。")
        cleaned = cleaned.replace("，,", "，").replace("，，", "，")
        cleaned = cleaned.replace("，；", "；").replace("。，", "。")
        return cleaned

    @staticmethod
    def _ensure_item_white_bg(prompt_zh: str, style: str = "", surface_text: str = "") -> str:
        """给物品参考图提示词确定性地补「只呈现本体 + 纯白背景」约束（幂等）。

        物品/道具参考图与角色设定图同理：后续要拿来做参考图编辑（多视角/分镜），
        背景越干净越利于一致性；带场景/贴图的物品图会把背景一起带进分镜。

        2026-10-01 补「本体之外一律不要」：此前只约束了**背景**，没约束**承托物**，
        于是「丹药置于玉盒中」这种提示词照画不误，还会被质检判通过（图符提示词）。

        2026-10-06 补「带文字物品的文字渲染规范」：扩散模型**无法写出正确可读的汉字**
        （实测出「形似汉字的乱码」，笔画对但字错、不可读）。若物品提示词**含可读
        文字**（书名/告示/卡片/标签/守则/手机屏幕等），追加约束让模型把文字画得
        规整、横排、清晰、占版面一致，**降低乱码概率**（治标——真正根治是后处理
        贴图，见 config ITEM_TEXT_POSTPROCESS；此处只改提示词、不做合成，属零风险
        确定性改进）。

        surface_text（2026-10-06 新增，带默认值以**兼容所有既有调用**）：
          · 非空 → 追加**确切文字**硬约束（**替代**下方 text_render_rule 的软约束）：
            把「要写什么字」逐字写进提示词。这是「带文字物品乱码」的根因修法——
            旧软约束只要求「写 2-6 个常见词」，从没告诉模型**写哪几个字**，
            模型只能瞎编 → 形似汉字的乱码。幂等标记「物品表面必须呈现」。
          · 空（默认）→ 与加参数前**逐字一致**（沿用 text_render_rule 软约束，
            「未明确指出的文字一律留白不写」）。
        """
        text = ComfyUIClient._strip_item_placement(str(prompt_zh or "").strip())
        marker = "纯白背景"
        # 物品参考图同样走参考图口径（固有色不得被整体色调污染）
        base = text if marker in text else style_kit.with_reference_style(text, style) if style else text
        body_rule = ("，画面中只呈现该物品本体，不要任何容器、托盘、底座、支架、展示台、盒子、"
                     "碗碟、绸布等承托物，不要手或人形生物，不得把物品放进另一个物体内部或上面")
        # ⭐ 2026-10-06 强约束版「带文字物品」的文字渲染规范（参考角色 sheet 的成功范式）。
        # 旧版只写"写清可读简体字"（软约束）——扩散模型对**小字**仍会乱码（实测告示/手写
        # 卡片出形似汉字的错字）。角色 sheet 能写清是因为用了「固定枚举的大字标签」。
        # 故此处升级：① 文字内容限定为**简短、通用、高频**的中文（避免剧情长句）；
        # ② 字号**放大、笔画粗、高对比**（小字→中/大字才写得清）；③ 版面**简洁、少行**，
        # 未指定的文字一律**留白不写**（不要塞满字）。仍仅当提示词含文字语义时追加（幂等），
        # 且措辞避开 CHARACTER_WORDS（"人物/人影…"），否则被场景清洗整句丢弃。
        # ⭐ 2026-10-09 对齐 Qwen-Image 官方用法：原文放引号 + 载体 + 字体；
        #    去掉 markdown `**`、书名号「」与「不要乱码」负向句（负向句会诱发乱码）。
        text_render_rule = ("，若物品表面需呈现文字（书名/告示/卡片/标签/手写/打印/屏幕等）："
                            "把确切原文写进描述，格式为：物品表面居中印有黑色粗体字 “原文”；"
                            "文字简短（不超过 8 个字），字体清晰、笔画完整、高对比、横排，"
                            "排版规整，字形与常用印刷体一致；未指出的文字一律留白不写")
        if marker in base:
            out = base if "只呈现该物品本体" in base else base + body_rule
        else:
            out = base.rstrip("。，,.;； ")
            out = out + body_rule + "，纯白背景，无任何场景、地面、桌面、阴影与背景纹理，" \
                        "物品完整孤立居中、边缘清晰" if out else body_rule.lstrip("，") + "，纯白背景，" \
                        "无任何场景、地面、桌面、阴影与背景纹理，物品完整孤立居中、边缘清晰"
        # ⭐ 2026-10-06（T02）：给出了**确切文字** → 用硬约束替代软约束（幂等）。
        #    根因：旧软约束从未告诉模型"写哪几个字"，汉字只能瞎编 → 乱码。
        #    固定文字（工牌/告示/证件的抬头等）写进提示词后，模型照抄即可大幅降低乱码。
        _surface = str(surface_text or "").strip()
        if _surface:
            if "物品表面必须呈现" not in out:
                out = out.rstrip("。，,.;； ") + (
                    f"，物品表面必须呈现文字：物品正面居中印有黑色粗体字 \"{_surface}\","
                    "字体清晰、笔画完整、高对比、横排"
                    "排版规整，字形与常用印刷体一致")
            return out
        # 未给出确切文字 → 沿用既有软约束（与加参数前逐字一致）
        _text_markers = ("文字", "汉字", "书名", "告示", "卡片", "标签", "手写", "守则", "屏幕", "字样", "字迹")
        if any(m in out for m in _text_markers) and "字形与常用印刷体一致" not in out:
            out = out.rstrip("。，,.;； ") + text_render_rule
        return out
    def generate_item_base(self, prompt_zh: str, seed: int = None,
                           style: str = "", size=None,
                           filename_prefix: str = None,
                           surface_text: str = "") -> List[str]:
        """生成物品基础图（纯 T2I）。

        surface_text（2026-10-06 新增，**带默认值以兼容所有既有调用**）：
          物品表面**确切**要呈现的文字（如工牌抬头「安保部」）。非空时由
          :meth:`_ensure_item_white_bg` 把它作为硬约束写进提示词（替代原来的
          「写 2-6 个常见词」软约束），从根因上降低汉字乱码；空字符串时与
          加参数前**逐字一致**。
        """
        logger.info(f"生成物品基础图: {prompt_zh[:50]}...")
        # ⭐ 2026-10-06：_ensure_item_white_bg 内含「带文字物品的中文文字渲染规范」
        #    （降低扩散模型写乱码汉字的概率，治标；根治需后处理贴图）。
        prompt_zh = self._ensure_item_white_bg(prompt_zh, style, surface_text)
        return self._generate_base_image(WORKFLOW_TEMPLATE["item_gen"], prompt_zh,
                                         asset_type="item", seed=seed, style=style, size=size,
                                         filename_prefix=filename_prefix)

    def generate_item_base_with_ref(self, prompt_zh: str, ref_image_path: str,
                                    seed: int = None, style: str = "",
                                    size=None, filename_prefix: str = None,
                                    surface_text: str = "") -> List[str]:
        """生成物品基础图（**主人形象参考图**链路，2026-10-06 新增）。

        与 :meth:`generate_item_base` 行为一致（同样走 :meth:`_ensure_item_white_bg`
        的纯白底 + 本体约束、同样保持物品 **1:1 画幅不变量**、同样按 filename_prefix
        落项目专属输出桶），**差别只在于**：把 ``ref_image_path`` 作为**参考图**送进
        ``item_ref_gen`` 工作流做图生图。

        用途：「工牌 / 证件照 / 告示人像 / 屏幕人像」这类物品表面承载某角色肖像、
        且该肖像应是**物品主人本人**的场景（app 层用 ``_item_owner_ref_image``
        解析主人角色参考图后调用本方法）。

        ⚠️ 走**专用模板** ``item_ref_gen``（= 物品纯 T2I 全节点 + 恰好 1 个参考图槽），
           而非分镜模板：分镜模板无尺寸节点、出图画幅会**继承参考图**，会破坏
           「物品图 1:1」不变量。本方法把 ``size``（物品 1:1）传给
           :meth:`_run_multiview_workflow`，专用模板的 EmptyLatentImage 宽高会被
           ``style_kit.apply_latent_size`` 覆写为字面量 —— 参考图只提供**形象**，
           不改变画幅。

        返回：单元素路径列表（与 :meth:`generate_item_base` 返回类型一致）；
              未产出文件时返回 ``[]``（由 app 层判失败并执行降级）。
        """
        logger.info(f"生成物品基础图（主人形象参考图）: {prompt_zh[:50]}...")
        prompt_zh = self._ensure_item_white_bg(prompt_zh, style, surface_text)
        local = self.resolve_local_path(ref_image_path) if isinstance(ref_image_path, str) else None
        if not local or not os.path.exists(local):
            raise RuntimeError(f"物品主人参考图不可用: {ref_image_path}")
        # 参考图上传到 ComfyUI output 目录：LoadImageOutput 只认 output 目录 + "名字 [output]" 标注
        # （_run_multiview_workflow 内部按节点类型补标注）。文件名做安全化处理，避免超长/非法字符。
        _pfpart = os.path.splitext(os.path.basename(str(filename_prefix or "item")))[0]
        _safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]", "_",
                       os.path.basename(local) or "ref")[:48]
        ref_name = f"item_ref_{_pfpart[:32]}_{_safe}.png"
        uploaded = self.upload_image(local, ref_name, image_type="output")
        img_path = self._run_multiview_workflow(
            uploaded, prompt_zh, seed=seed, size=size, filename_prefix=filename_prefix,
            wf_name=WORKFLOW_TEMPLATE["item_ref_gen"])
        return [img_path] if img_path else []

    def generate_scene_base(self, prompt_zh: str, seed: int = None,
                            style: str = "", size=None,
                            filename_prefix: str = None,
                            view_key: str = None,
                            surface_text: str = "",
                            ref_images: List[str] = None) -> List[str]:
        """生成场景图（T2I，或带关联参考的参考图编辑）

        ref_images（2026-10-10 新增，**带默认值以兼容所有既有调用**）：
          **场景空间关联**参考图（如「2704房间」以「2704门口」为参考）。
          · 空（默认）→ 走纯 T2I（scene_gen 工作流），与加参数前**逐字一致**；
          · 非空 → 改走**参考图编辑**链路（storyboard_gen 工作流，9 槽位），
            让门框 / 地面 / 墙面材质 / 光比与关联场景真正接上。

          ⚠️ 为什么不能继续用 scene_gen：该工作流是**纯 T2I、没有参考图槽位**
            （见本函数下方 2026-09-29 的注释与 8 组对照实验）。实测结论：以
            「2704门口」为参考生成「2704房间」，门框与门外走廊地面材质与参考图
            一致；而无参考时门框颜色完全对不上——同一个空间被画成两处。

          ⚠️ 已知副作用（实验实测，调用方需知情）：参考图**引力较强**，会部分
            覆盖提示词里的陈设细节（木桌→金属桌）。故提示词应显式声明「只沿用
            参考图的材质 / 建筑结构 / 光线，陈设以本场景描述为准」（见
            SCENE_RELATION_REF_HINT）。关联越紧密，细节自由度越低。

        view_key（2026-09-29 新增）：场景**机位档**（``config.SCENE_VIEW_KEYS``）。
          · ``None``（默认）→ 与旧实现**逐字一致**（不追加任何机位句），
            这就是「基础图 / base.png」的生成路径；
          · 任一档位 → 把该机位的出图指令（见 :func:`scene_view_prompt_suffix`）
            追加进正向提示词后**独立出图**。

        surface_text（2026-10-07 新增，**带默认值以兼容所有既有调用**）：
          场景里**确切**要呈现的文字（如「楼层安全守则」「禁止通行」）。非空时由
          :meth:`_ensure_scene_text_render` 把它作为硬约束写进提示词（替代原来的
          「文字务必简短高频」软约束），从根因上降低场景汉字乱码；空字符串时与
          加参数前**逐字一致**。所有机位档共用同一份提示词，故各档文字一致。
        ⚠️ 生产链路只对 ``left45`` / ``right45`` / ``top`` 三档传非空 view_key；
        正面档（``front``）**不重新出图**，由 app 层直接复用 base.png
        （base 本就是无角度声明的正拍基准图），故 ``front`` 虽可传、但没人传。

        ⚠️ 为什么这里是「独立出图」而不是「拿 base.png 做参考图编辑」：
            参考图编辑（``generate_multiview``）在 cfg=1.0 下 uncond 不评估、文字无引导，
            只会复刻参考图里已可见的机位，**改不动角度**（8 组对照实验结论，
            见 ``MULTIVIEW_CONFIG`` 上方注释）。而本函数跑的 ``scene_gen`` 是纯 T2I、
            没有参考图槽位，提示词里的机位句是**唯一**的构图来源，因此真正生效。

        跨档内容一致性由调用方保证（app 层用**同一颗 seed**逐档出图）：
        同 seed 下各档共享同一初始噪声，只有机位句不同 → 结构高度相关，
        细节差异被限制在陈设层面，不会让分镜「换个机位就换了场地」。
        """
        logger.info(f"生成场景图（机位档={view_key or 'base'}）: {prompt_zh[:50]}...")
        # 基础图同样必须去人（2026-09-29 补齐原先的不对称）：
        # 此前只有 generate_multiview 做了 sanitize，而**基础图本身就是多视角的输入参考图**
        # —— 基础图一旦渲染出人物，人物会被后续 4 个视角、分镜与视频全部继承
        # （既有教训：资产实测 "山间小径" 基础图渲染出 4 人）。
        # 幂等：sanitize 现已**真正幂等**（2026-09-29 修正，见其 docstring），
        # 这里的判存在只是省一次无用调用；_generate_base_image 内部会再走一遍。
        if SCENE_NO_CHARACTER_SUFFIX not in (prompt_zh or ""):
            prompt_zh = self.sanitize_scene_prompt(prompt_zh)
        # ⭐ 2026-10-06：带文字场景（告示/标识/招牌/守则/铭牌等）→ 补「中文文字渲染规范」。
        #    扩散模型写不出可读汉字（实测出乱码）；此句降低乱码概率（治标，根治需后处理贴图）。
        #    幂等 + 措辞避开 CHARACTER_WORDS（"人物/人影…"），不被 sanitize_scene_prompt 丢弃。
        #    ⭐ 2026-10-07：传入 surface_text（非空）时改为**确切文字**硬约束（根因修法）。
        prompt_zh = ComfyUIClient._ensure_scene_text_render(prompt_zh, surface_text)
        # ⭐ 2026-10-10 场景空间关联：有关联参考图时改走参考图编辑链路。
        #    只在 base 图（view_key 为空）上做 —— 机位档是「同一场景换机位」，
        #    与「关联场景」正交；混在一起会互相干扰（机位档本就靠同 seed 保证一致）。
        if ref_images and not view_key:
            return self._generate_scene_with_refs(
                prompt_zh, list(ref_images), seed=seed, style=style, size=size,
                filename_prefix=filename_prefix)
        return self._generate_base_image(
            WORKFLOW_TEMPLATE["scene_gen"], prompt_zh,
            asset_type="scene", seed=seed, style=style, size=size,
            filename_prefix=filename_prefix,
            prompt_extra=scene_view_prompt_suffix(view_key))

    def _generate_scene_with_refs(self, prompt_zh: str, ref_images: List[str],
                                  seed: int = None, style: str = "",
                                  size=None, filename_prefix: str = None) -> List[str]:
        """以**关联场景图**为条件生成场景图（参考图编辑）。

        复用 generate_storyboard 的参考图链路（上传到 ComfyUI output → 逐个槽位
        注入 images.image_N，见其 docstring）：本仓只有 storyboard_gen 工作流带
        参考图槽位，而实测它出场景图**效果可用**（1664×928，与资产同尺寸）。

        ⚠️ generate_storyboard **没有 style 参数**，故风格后缀必须在这里自行拼进
        提示词（与 _generate_base_image 内的处理同口径）。

        失败语义（fail-open，2026-10-10 定稿）：场景关联是**增强**而非依赖 ——
        参考图不可用 / 参考图编辑链路报错时，自动回落**无参考的纯 T2I**，
        绝不因为「关联」这一步把整集资产生产拖垮。回落是安静降级但有日志。
        """
        text = str(prompt_zh or "").strip()
        if style:
            text = style_kit.with_reference_style(text, style)
        if SCENE_RELATION_REF_HINT not in text:
            text = text.rstrip("。，,.;； ") + SCENE_RELATION_REF_HINT
        _pfx = filename_prefix or "comic_drama_scene/scene"
        try:
            res = self.generate_storyboard(text, ref_images,
                                           filename_prefix=_pfx, seed=seed, size=size)
            files = list(res.get("files") or []) if isinstance(res, dict) else list(res or [])
            if files:
                logger.info("[场景关联] 已按关联参考图出图：%d 张参考图 → %s",
                            len(ref_images), os.path.basename(files[0]))
                return files
            logger.warning("[场景关联] 参考图编辑未产出文件，回落无参考生成")
        except Exception as e:  # noqa: BLE001  关联失败绝不阻断资产生产
            logger.warning("[场景关联] 参考图编辑失败（回落无参考生成）：%s: %s",
                           type(e).__name__, e)
        return self._generate_base_image(
            WORKFLOW_TEMPLATE["scene_gen"], prompt_zh,
            asset_type="scene", seed=seed, style=style, size=size,
            filename_prefix=filename_prefix, prompt_extra="")

    @staticmethod
    def _ensure_scene_text_render(prompt_zh: str, surface_text: str = "") -> str:
        """场景提示词补「带文字的中文渲染规范」（幂等）。

        场景里若出现可读文字（告示 / 标识 / 招牌 / 守则 / 铭牌 / 屏幕等），扩散模型
        无法写出正确汉字（实测出形似汉字的乱码）。本函数仅在提示词**本身含文字语义**
        时追加约束，让模型尽量画得规整清晰（**治标**——根治需后处理贴图，不在本仓做）。

        ⭐ surface_text（2026-10-07 新增，带默认值以**兼容所有既有调用**）：
          · 非空 → 追加**确切文字**硬约束（**替代**下方软约束），把「写哪几个字」逐字
            写进提示词。这是「带文字场景乱码」的根因修法——场景此前**根本没有**这条
            通道（bible 的 scenes schema 里没有对应字段），只有「文字务必简短高频…」
            的软约束，从没告诉模型写哪几个字，模型只能瞎编 → 形似汉字的乱码。
            与物品 ``_ensure_item_white_bg(surface_text=...)`` / 角色 sheet 的固定标签
            同一范式（角色 sheet 文字正常正因为它是硬编码枚举的确切字串）。
            幂等标记「画面文字必须呈现」。
          · 空（默认）→ 与加参数前**逐字一致**（沿用软约束）。

        ⚠️ 必须避开 :data:`CHARACTER_WORDS`：``sanitize_scene_prompt`` 会把含
        「人物/人影/人群/士兵」等词的短句整句丢弃，措辞一律用「无生物/无人形」这类
        不命中词表的写法。
        """
        text = (prompt_zh or "").strip()
        if not text:
            return text
        # ⭐ 给了确切文字 → 硬约束（不看 _text_markers：字段非空即代表确有文字要写）
        _surface = str(surface_text or "").strip()
        if _surface:
            # ⚠️ 幂等标记必须是下方追加串的**字面子串**（2026-09-25 教训）：
            #    这里用「必须呈现以下**确切**文字」（SCENE_EXACT_TEXT_MARKER，单一来源），
            #    不能用「画面文字必须呈现」——
            #    实际串是「画面**中出现的**文字（…）必须呈现以下**确切**文字」，
            #    后者不是子串 → 判定永假 → 反复调用反复追加（提示词无限膨胀）。
            if SCENE_EXACT_TEXT_MARKER not in text:
                text = text.rstrip("。，,.;； ") + (
                    f"，画面中出现的文字（告示/标识/招牌/守则/铭牌/标语/屏幕等）必须呈现"
                    # ⚠️ 下句必须与 SCENE_EXACT_TEXT_MARKER 字面一致（幂等）
                    f"文字：居中印有黑色粗体字 \"{_surface}\","
                    "字体清晰、笔画完整、高对比、横排，排版规整，字形与常用印刷体一致；"
                    "未指出的文字一律留白不写")
            return text
        _text_markers = ("文字", "汉字", "告示", "标识", "招牌", "守则", "铭牌", "标语",
                         "字样", "字迹", "书法", "标题", "说明", "屏幕")
        if not any(m in text for m in _text_markers):
            return text                       # 无文字语义的场景不加，避免误伤
        if "字形与常用印刷体一致" in text:
            return text                       # 幂等（新强约束标记）
        # ⭐ 2026-10-06 强约束版（与物品同款，参考角色 sheet 范式）：把"写清可读简体字"
        # 升级为「文字简短高频 + 字号放大笔画粗 + 行少留白多 + 未指出的字留白不写」，
        # 压扩散模型对小字乱码的概率。措辞避开 CHARACTER_WORDS。
        # ⭐ 2026-10-09 对齐 Qwen-Image 官方用法（同物品口径）。
        rule = ("，若画面中出现文字（告示/标识/招牌/守则/铭牌/标语/屏幕等）："
                "把确切原文写进描述，格式为：该处居中印有黑色粗体字 “原文”；"
                "文字简短（不超过 8 个字），字体清晰、笔画完整、高对比、横排，"
                "排版规整，字形与常用印刷体一致；未指出的文字一律留白不写")
        return text.rstrip("。，,.;； ") + rule

    # ===================== 第二阶段：多视角生成 =====================

    def generate_multiview(self, base_image_path: str, asset_type: str,
                           asset_name: str, base_prompt_zh: str,
                           seed: int = None, style: str = "", size=None,
                           filename_prefix: str = None) -> Dict[str, str]:
        """基于基础图生成多视角图（Qwen Edit 2511）

        character → 4 视图（正/左/右/背）；item / scene → 4 视角（正/左45/右45/俯视）

        风格落地（2026-09-18 修复）：原 docstring 声称"统一补竖屏 9:16 画幅声明"，
        但代码里并没有这一步 —— 风格与画幅都是模板自带的，用户的设定到不了多视角图。
        现在 style / size 由调用方传入并真正生效。
        seed 供 app 层质检不达标时重生成。

        G8：filename_prefix 非空 → 多视角产物落项目专属子目录（每视角再加 key 后缀
        避免同前缀互相覆盖）。
        """
        views = MULTIVIEW_CONFIG["character_views"] if asset_type == "character" \
            else MULTIVIEW_CONFIG["item_scene_views"]

        # B-13 P1-14：基础图上传到 ComfyUI output 根目录时文件名带项目名，避免跨项目同名资产互相覆盖
        base_name = f"comic_drama_{asset_type}_{asset_name}_base.png"
        if filename_prefix:
            # filename_prefix 形如 comic_drama/<项目>_asset_<类型>_epNN，
            # 基础图沿用同一前缀子目录，与多视角产物同目录，不产生孤儿
            base_name = f"{filename_prefix.rstrip('/')}/{base_name}"
        try:
            output_name = self.upload_image(base_image_path, base_name, image_type="output")
        except Exception as e:
            # 不做「回退到 input 目录」：LoadImageOutput 只认 output 目录，回退必然 400，
            # 与其提交注定失败的 prompt，不如快速失败并给出明确原因（P0-5）
            raise RuntimeError(
                f"基础图上传到 ComfyUI output 目录失败，LoadImageOutput 无法引用: {e}"
            ) from e
        logger.info(f"基础图已上传到 ComfyUI output 目录: {output_name}")

        # 风格后缀：拼在 base_desc 之后，保证每个视角都带风格
        # 资产多视角（参考图）→ 参考图口径
        styled_desc = style_kit.with_reference_style(base_prompt_zh, style, with_tail=False) if style \
            else base_prompt_zh
        # P0：场景多视角同样必须去人（基础图与多视角一致，避免视角转换时"带出"人物）
        if asset_type == "scene":
            styled_desc = self.sanitize_scene_prompt(styled_desc)

        results = {}
        for view in views:
            logger.info(f"生成 {asset_name} {view['label']}...")
            view_prompt = self._build_multiview_prompt(asset_type, styled_desc, view)
            if asset_type == "scene" and SCENE_NO_CHARACTER_SUFFIX not in view_prompt:
                view_prompt = view_prompt.rstrip("。;； ") + SCENE_NO_CHARACTER_SUFFIX
            # G8：每视角在公共前缀后加 key，避免同前缀下不同视角产物互相覆盖
            view_prefix = f"{filename_prefix}_{view['key']}" if filename_prefix else None
            img_path = self._run_multiview_workflow(output_name, view_prompt, seed=seed,
                                                     size=size, filename_prefix=view_prefix)
            if img_path:
                results[view["key"]] = img_path
            else:
                logger.warning(f"{asset_name} {view['label']} 生成失败")
        return results

    def _build_multiview_prompt(self, asset_type: str, base_desc: str, view: dict) -> str:
        """构造多视角编辑提示词：**机位前置**，中英同口径，一致性要求降为从属。

        ⚠️ 关于「多视角图机位不变」（2026-09-23 实测，勿重复踩坑）：
            本函数**改不动模型输出的机位**。参考图编辑只复刻参考图里已可见的角度，
            不会凭空补全没见过的面 —— 用正面基础图 + 明确「背面/俯视」指令，
            输出仍是正面。已用 8 组对照实验排除措辞因素（换语言/语序/命令式 vs 编辑式、
            断开 vae、覆写 cfg、跨对象对照），结论与机制见 config.MULTIVIEW_CONFIG 注释。
            真要拿到多视角，得在**基础图**阶段带入角度。
            本函数的作用是把机位说清楚、不再自相矛盾、不让「保持一致」压过机位要求，
            对更强的模型保留正确口径。

        角色多视图每张都要求「全身」构图（2026-09-19 实测：基础图出成半身/胸像，
        视角图继承半身构图导致"三视图不是全身"。distance=full-body shot 不够，
        必须显式写"从头到脚完整入画"，并排除半身/胸像/大头）。
        """
        # 中文机位：优先用配置里的 zh；缺省回落到 label + 英文方位（旧调用方可继续工作）
        angle_zh = (view.get("zh") or "").strip() or \
            f"本图是{view['label']}，相机方位：{view['azimuth']}，{view['elevation']}"
        camera_terms = f"{view['azimuth']}, {view['elevation']}, {view['distance']}"
        if asset_type == "character":
            return (
                f"【机位】{angle_zh}。"
                f"画面完整呈现人物从头到脚，头顶与脚部不留裁切，"
                f"不要半身像、不要胸像、不要大头特写、不要截断脚部。"
                f"【一致性】只改变观察角度：脸型、发型、服装、配饰与参考图保持一致，"
                f"人物身高比例不变。{camera_terms}。{base_desc}"
            )
        asset_word = "物品" if asset_type == "item" else "场景"
        return (
            f"【机位】{angle_zh}。"
            f"相机绕{asset_word}变换位置后重新取景，{asset_word}在画面中的朝向随视角改变。"
            f"【一致性】{asset_word}的形状、材质、颜色、花纹与参考图一致。"
            f"{camera_terms}。{base_desc}"
        )

    def _run_multiview_workflow(self, uploaded_image_name: str, prompt_zh: str,
                                image_dir: str = ANNOTATED_DIR,
                                seed: int = None, size=None,
                                filename_prefix: str = None,
                                wf_name: str = None) -> Optional[str]:
        """运行参考图编辑工作流（默认 分镜生成_Qwen21.json：QwenImage2.1 参考图编辑）

        wf_name（2026-10-06 新增，带默认值以**不改变既有调用方行为**）：
          显式指定要跑的工作流文件名；缺省（None）时沿用 ``multiview_gen``。
          「物品主人形象」链路传 ``item_ref_gen``（专用模板：物品 T2I 全节点 +
          **恰好 1 个** 参考图槽，被下方「填充所有参考图节点」的逻辑命中）。
          ⚠️ 专用模板保留了尺寸节点（EmptyLatentImage），故 ``size`` 非空时画幅会
             被覆写为物品 1:1 字面量，**不继承参考图画幅** —— 这是能保证物品画幅
             不变量的关键（分镜模板无尺寸节点，画幅继承参考图，故不可复用）。
        """
        wf_name = wf_name or WORKFLOW_TEMPLATE["multiview_gen"]
        api_prompt, meta = self.load_workflow(wf_name, return_meta=True)
        if seed is not None:
            logger.info(f"多视角采样种子已注入: {self._inject_seed(api_prompt, seed)}")

        # 1) 正向提示词：只写正向字段（正负同体节点是 prompt，老模板是 prompt/text）
        node_id = self._find_positive_text_node(api_prompt)
        pos_field = self._positive_field(api_prompt, node_id) if node_id else None
        if node_id is not None and pos_field:
            api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        else:
            raise RuntimeError(f"{wf_name} 未定位到正向提示词字段（节点={node_id}）")
        self.clean_conflict_negative_tokens(api_prompt)   # P0：清理风格冲突负向词
        # 画幅落地（与基础图一致，否则多视角会把竖屏 base 图改成模板的横屏尺寸）
        if size:
            hit = style_kit.apply_latent_size(api_prompt, size)
            if hit:
                logger.info("多视角画幅已覆写为 %s×%s：%s", size[0], size[1], ",".join(hit))
            else:
                logger.info("多视角工作流无尺寸节点，画幅继承参考图（基础图 %s×%s）",
                            size[0], size[1])

        # 2) 参考图：3 个 LoadImageOutput 全部替换（P0-3），且按节点类型补目录标注（P0-5）
        #    LoadImageOutput 读 output 目录 → 必须写 "名字 [output]"
        #    LoadImage（若模板中出现）读 input 目录 → 必须写裸文件名，不能带标注
        ref_nodes = [(nid, n.get("class_type")) for nid, n in api_prompt.items()
                     if n.get("class_type") in ("LoadImageOutput", "LoadImage")]
        ref_values = []
        for nid, ctype in ref_nodes:
            value = self.annotate_file_ref(uploaded_image_name, ctype, default_dir=image_dir)
            api_prompt[nid]["inputs"]["image"] = value
            ref_values.append(f"{nid}({ctype})={value}")
        if not ref_nodes:
            logger.warning(f"{wf_name} 中未找到参考图节点（LoadImageOutput/LoadImage）")
        else:
            logger.info(f"多视角参考图已替换 {len(ref_nodes)} 个节点: {ref_values}")

        # G8：覆写输出文件名前缀（项目专属子目录），避免多视角图堆在 ComfyUI output 默认目录
        if filename_prefix:
            for _nid, _n in api_prompt.items():
                if _n.get("class_type") in ("SaveImageAdvanced", "SaveImage") \
                        and "filename_prefix" in _n["inputs"]:
                    _n["inputs"]["filename_prefix"] = filename_prefix

        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id, timeout=900)
        files = self.get_output_files(history, ".png")
        return files[0] if files else None

    # ===================== 第三阶段：分镜图生成（Qwen Edit 2511） =====================

    @staticmethod
    def _find_image_slots(api_prompt: dict, prompt_node_id: str) -> List[Tuple[str, Optional[str]]]:
        """解析正向编辑节点的参考图槽位 → 真正持有文件名的 LoadImage* 节点 id

        两种模板两种键名，**必须都认**：
        - 老模板（Qwen-Edit 2511 / TextEncodeQwenImageEditPlus）：``image1/image2/image3``；
        - QwenImage2.1（TextEncodeQwenImage21）：autogrow 点号键 ``images.image_1`` …
          （服务端 `_expand_schema_for_dynamic` 用 finalize_prefix 生成）。
        只认前者会让参考图**静默不注入**——分镜失去角色/场景一致性，且不报错。

        链路可能是「LoadImageOutput → imageN」直连，也可能中间隔一层
        （老的 ``FluxKontextImageScale`` / 新的 ``ImageScaleToTotalPixels``），
        因此向上游最多回溯两层找 LoadImage*。
        """
        result: List[Tuple[str, Optional[str]]] = []
        node = api_prompt.get(prompt_node_id) or {}
        keys = sorted((k for k in (node.get("inputs") or {}) if _IMAGE_SLOT_RE.match(str(k))),
                      key=lambda k: _slot_index(k))
        for key in keys:
            value = node["inputs"][key]
            result.append((key, ComfyUIClient._trace_load_image(api_prompt, value, depth=2)))
        return result

    @staticmethod
    def _trace_load_image(api_prompt: dict, value, depth: int) -> Optional[str]:
        """沿连线向上游回溯，找到持有文件名的 LoadImage* 节点 id（找不到返回 None）"""
        if depth <= 0 or not (isinstance(value, list) and len(value) == 2):
            return None
        src_id = str(value[0])
        src = api_prompt.get(src_id) or {}
        if str(src.get("class_type", "")).startswith("LoadImage"):
            return src_id
        for v in (src.get("inputs") or {}).values():
            found = ComfyUIClient._trace_load_image(api_prompt, v, depth - 1)
            if found:
                return found
        return None

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

    @staticmethod
    def _merge_visual_detail(desc: str, detail: str) -> str:
        """把 visual_detail 并入画面主体，**已出现在 desc 里的分句不再重复追加**。

        不能简单 `f"{desc}。{detail}"`：提示词分析器写 storyboard_prompt_zh 时也会消费
        visual_detail（见 script_prompt_analyzer.build_shot_prompt），两条来源叠加会把同一句
        细节写两遍 —— 分镜图提示词里重复描述会放大该要素、干扰构图。
        """
        desc = str(desc or "").strip()
        detail = str(detail or "").strip()
        if not detail:
            return desc
        if not desc:
            return detail
        if detail in desc:
            return desc
        segs = [s.strip() for s in detail.replace("。", "，").split("，")]
        add = [s for s in segs if s and s not in desc]
        if not add:
            return desc
        # desc 已以句末标点收尾时不再补「。」，否则会出现「。。」
        sep = "" if desc.endswith(("。", "！", "？", "…")) else "。"
        return f"{desc}{sep}{'，'.join(add)}"

    @staticmethod
    def _light_hint_covered(hint: str, text: str) -> bool:
        """判断光影提示语是否已被文本覆盖（幂等，避免同一束光写两遍）。

        只比对**完整提示语是不是子串**是不够的：剧本阶段写进 visual_detail 的往往是
        「黄昏暖调逆光」这种**首段短语**，而不是整条「黄昏暖调逆光，长影拉长，天空带
        橙色到紫色的渐变」。此时整条比对不命中 → 又追加一次 → 分镜图提示词里同一束光
        出现两遍（实测 shot2：黄昏暖调逆光 出现 2 次）。
        因此这里同时比对首段（第一个「，」之前），任一命中即视为已覆盖。
        """
        if not hint or not text:
            return False
        if hint in text:
            return True
        head = hint.split("，", 1)[0].strip()
        return bool(head) and head in text

    @staticmethod
    def _extract_light_hint(shot: dict) -> str:
        """从镜头描述/情绪里抽取光影引导；无命中返回空串（不强行加光影）。

        三级优先（2026-09-28 P4-A 场景共享光影）：
        1. 本镜光源词（``_LIGHT_KEYWORDS``，最具体）——画面明写「闪电/烛光/黄昏」等；
        2. 场景共享光影（``shot["scene_lighting"]``，治场景内逐镜漂移）——同场景所有
           无具体光源词的镜头统一吃这一档基线，避免忽冷忽热；
        3. 情绪兜底（``_LIGHT_EMOTION_FALLBACK``，最弱）。

        第 1 档命中即返回（最具体优先：画面明写的光源压过场景基线，允许单镜特写例外）；
        场景光影与情绪兜底都做幂等（提示语首段已在画面文本里则不重复追加）。
        旧剧本 / 无 ``scene_lighting`` 字段的镜头 → 第 2 档为空，行为与旧版完全一致。

        幂等：若命中的光影提示语（或它的首段短语）已经出现在文本里，说明画面细节
        已承载光源，不再重复追加。
        """
        text = " ".join([
            str(shot.get("description") or ""),
            # storyboard_prompt_zh 会顶替 description 成为画面主体（见 build_storyboard_prompt），
            # 必须一起扫描：否则分析器已写明「黄昏逆光」时识别不到 → 再叠一条光影氛围句。
            str(shot.get("storyboard_prompt_zh") or ""),
            str(shot.get("visual_detail") or ""),
            str(shot.get("audio_cues") or ""),
            str(shot.get("emotion") or ""),
        ])
        if not text.strip():
            return ""
        for kw, hint in ComfyUIClient._LIGHT_KEYWORDS:
            if kw in text:
                return "" if ComfyUIClient._light_hint_covered(hint, text) else hint
        # P4-A 场景共享光影（第二档）：无具体光源词的镜头统一落到场景基线，消除场景内漂移。
        # 命中场景基线即返回（不再走情绪兜底）——同场景光线一致性优先于单镜情绪泛化。
        scene_light = str(shot.get("scene_lighting") or "").strip()
        if scene_light:
            return "" if ComfyUIClient._light_hint_covered(scene_light, text) else scene_light
        emotion = str(shot.get("emotion") or "").strip()
        for kw, hint in ComfyUIClient._LIGHT_EMOTION_FALLBACK:
            if kw in emotion:
                return "" if ComfyUIClient._light_hint_covered(hint, text) else hint
        return ""

    @staticmethod
    def build_shot_grid_candidates_prompt(shot: dict, ref_labels: List[str] = None,
                                          has_blocking_image: bool = False,
                                          candidates: int = 9) -> str:
        """分镜「九宫格候选构图」提示词（2026-10-01，对标 BigBanana 的 Shot Workbench）。

        在常规分镜提示词的基础上做两处改写：
        1. TASK 从「单帧」改为「3x3 联系表（contact sheet），九个备选构图」；
        2. 末尾追加 GRID LAYOUT 段：严格 3 行 3 列、逐格列出机位/景别变体清单，
           同场景同角色同风格，**只有取景与角度在格间变化**。
        生成后由调用方/QC 选出最佳格，裁切为该镜正式分镜图（一图九候选，比
        best-of-N 的九次独立生成省时省卡）。
        """
        prompt = ComfyUIClient.build_storyboard_prompt(
            shot, ref_labels, has_blocking_image=has_blocking_image)
        prompt = prompt.replace(
            "TASK: Generate a single storyboard frame.",
            f"TASK: Generate ONE image laid out as a 3x3 contact sheet showing "
            f"{candidates} alternative compositions of the exact same moment, "
            "changing ONLY the camera framing and angle between cells.", 1)
        rows = (candidates + 2) // 3
        variants = ("(1) wide establishing shot, (2) medium shot from the front, "
                    "(3) close-up on the main subject's face, (4) low angle looking up, "
                    "(5) high angle looking down, (6) over-the-shoulder depth shot, "
                    "(7) side profile view, (8) extreme close-up on a key prop or detail, "
                    "(9) dutch-tilted dramatic angle")
        prompt += (f"\n\nGRID LAYOUT: exactly {rows} rows by 3 columns, reading order "
                   f"left-to-right top-to-bottom, {candidates} cells in total. Every cell "
                   "shows the SAME scene with the SAME characters (identical identity, "
                   "costume and style); cells vary ONLY in camera framing and angle as "
                   f"follows: {variants}. Do not add text, numbers, borders or labels "
                   "between cells.")
        # ⭐ 九宫格「3D 构图基准网格」（2026-10-03）：带 3D 站位基准网格图时，声明
        #    网格每一格必须照搬基准网格对应格的机位/站位/动作（<image1> 就是那张 3x3
        #    基准网格，由 te_3d_render.render_blocking_grid 渲染）。
        if has_blocking_image:
            prompt += (
                "\nCOMPOSITION BASELINE GRID (critical): <image1> is a 3x3 sheet of "
                "3D blocking thumbnails (facialess mannequins) whose 9 cells correspond "
                "1-to-1, in reading order, to the 9 cells of the output contact sheet. "
                "For EACH output cell, mirror the camera framing, camera angle, character "
                "placement and body action of the matching 3D blocking thumbnail in "
                "<image1>. Use the 3D blocking thumbnails ONLY as composition, camera and "
                "blocking references \u2014 never as identity or appearance references.")
        return prompt

    def generate_shot_grid_candidates(self, shot: dict, ref_labels: List[str],
                                      ref_images: List[str], project: str,
                                      shot_key: str, style: str = "",
                                      has_blocking_image: bool = False,
                                      seed: int = None, size=None,
                                      timeout: int = None) -> Dict[str, Any]:
        """生成一镜的九宫格候选构图，返回与 generate_storyboard 同构的结果 dict。"""
        grid_prompt = self.build_shot_grid_candidates_prompt(
            shot, ref_labels, has_blocking_image=has_blocking_image)
        return self.generate_storyboard(
            prompt_zh=grid_prompt,
            ref_images=ref_images,
            filename_prefix=f"comic_drama_shotgrid/{project}_{shot_key}",
            seed=seed,
            size=size,
            timeout=timeout,
        )

    @staticmethod
    def build_shot_grid_keyframes_prompt(shot: dict, ref_labels: List[str] = None,
                                         style: str = "", has_characters: bool = True,
                                         panel_plans: Optional[list] = None,
                                         characters_section: str = None,
                                         shot_summary: str = None) -> str:
        """分镜图「**单镜九宫格 · 9 关键帧**」提示词（2026-10-02 用户指定）。

        :param has_characters: 本镜画面内是否有出场角色（默认 True = 旧行为逐字不变）。
            为 False 时（道具特写/空镜/纯画外音）转发给 :meth:`build_storyboard_prompt`
            并**改写**九宫格追加段里的「CHARACTER CONSISTENCY」句 —— 否则
            「the characters … must be IDENTICAL in every one of the nine panels」
            会暗示画面里有角色，与无人物镜自相矛盾（2026-10-05）。
        :param panel_plans: 可选，单镜 9 关键帧的**逐格规划**（app._grid_panel_plan 的
            LLM 产出：9 个 ``{"no","framing","tone","content"}``）。非空时改走
            **中文逐格版**模板 ``storyboard_grid_main``（用户范本「逐格写死」范式，
            2026-10-07）——不再基于 :meth:`build_storyboard_prompt` 打底；为 None / 空 /
            格数不足 9 → **回落下方英文版全路径**（一字不动，fail-open：默认行为只有
            拿到规划才变）。
        :param characters_section: 可选，「角色设定」段覆盖文本（不传则由
            :meth:`_grid_characters_section` 按参考图标签自动生成）。
        :param shot_summary: 可选，「本镜内容一句话」覆盖文本（不传则取 description /
            storyboard_prompt_zh）。

        ⚠️⚠️ **粒度（用户明确纠正，勿再回退）**：「一个 5 秒的分镜就是用的 9 宫格」
          —— 九宫格的 9 个格是**这一个镜头（shot）内随时间推进的 9 个关键帧**：
            ✗ 不是 9 个不同镜头（旧的「连贯分镜」九宫格实现因粒度错误已退役删除）；
            ✗ 不是 9 个机位候选（那是 `build_shot_grid_candidates_prompt`，选一格裁切）。
        用途：**九宫格整图 = 分镜图本体**，H3 视频直接拿整图当构图参考
        （用户原话「别人的 9 宫格图片就是一整张啊」——不裁切、不选格）。

        与候选构图九宫格的对照：
        ┌─────────────────┬──────────────────────┬──────────────────────┐
        │                 │ 候选构图（已有）       │ 单镜 9 关键帧（本函数）│
        ├─────────────────┼──────────────────────┼──────────────────────┤
        │ 9 格的含义       │ 同一时刻 9 机位        │ **一个镜头内 9 关键帧** │
        │ 格间变化         │ 只有取景/角度          │ **时间推进（动作/表情/构图）** │
        │ 时间维度         │ 冻结                  │ **同一镜头内推进**     │
        │ 用途             │ 选一格裁单图           │ **九宫格整图=分镜图**  │
        └─────────────────┴──────────────────────┴──────────────────────┘

        来源：用户提供的业界九宫格分镜范式（「单张图像内完整呈现 9 个关键帧，
        保持人物一致性、画面无文字、左下角标 1-9」）+ 用户补充「一个 5 秒分镜用九宫格」。
        (2026-10-06 起 1-9 编号改由前端叠加——扩散模型画数字实测乱码，见 TEXT RULE。)
        ⚠️ 分辨率：3x3 后每格仅整图 1/9 面积 → **必须放大整图边长**（调用方负责 size）。
        """
        # ---------- 中文逐格版分支（2026-10-07，用户范本「逐格写死」范式）----------
        # 拿到 9 格规划（app._grid_panel_plan 的文本 LLM 产出）→ 走 storyboard_grid_main
        # 模板渲染中文逐格提示词；规划为空 / 不足 9 格 / 模板不可用 → **原样走下方英文版
        # 全路径**（一字不动，fail-open：默认行为只有拿到规划才变）。
        if panel_plans:
            zh = ComfyUIClient._build_grid_keyframes_prompt_zh(
                shot, ref_labels, style=style, has_characters=has_characters,
                panel_plans=panel_plans, characters_section=characters_section,
                shot_summary=shot_summary)
            if zh:
                return zh
            logger.warning("九宫格中文逐格提示词未产出（规划不足 9 格或模板不可用），"
                           "回落英文版九宫格提示词")
        prompt = ComfyUIClient.build_storyboard_prompt(
            shot, ref_labels, has_blocking_image=False,
            has_characters=has_characters)
        prompt = prompt.replace(
            "TASK: Generate a single storyboard frame.",
            "TASK: Generate ONE image laid out as a 3x3 storyboard sheet (a nine-panel "
            "contact sheet) showing NINE KEYFRAMES of this SINGLE shot, in reading "
            "order left to right, top to bottom.", 1)

        # 镜头级的景别/机位/动作摘要（提示词里已含 FRAMING / SCENE AND ACTION，
        # 这里只补「9 关键帧的时间语义」，让模型知道这是时间切片而非空间变体）。
        shot_desc = str(shot.get("description") or "").strip()
        # 一致性句随 has_characters 切换（2026-10-05）：无角色镜头若照抄「the characters …
        # must be IDENTICAL in every panel」会暗示画里有角色 → 与 no-humans 禁令矛盾。
        _consistency = (
            "CHARACTER CONSISTENCY (critical): the characters, their facial "
            "identity, hairstyle, costume and props, together with the scene, "
            "lighting direction and colour grading, must be IDENTICAL in every one "
            "of the nine panels — only the progression of the action and framing "
            "changes.\n"
            if has_characters else
            "SUBJECT CONSISTENCY (critical): this shot has NO on-screen characters, "
            "so NO human figure may appear in ANY of the nine panels. The scene, the "
            "props and their placement, the lighting direction and colour grading "
            "must be IDENTICAL in every one of the nine panels — only the "
            "progression of the action and framing changes.\n"
        )
        prompt += (
            "\n\nGRID LAYOUT (single-shot keyframes): exactly 3 rows by 3 columns, "
            "nine panels in total, reading order left-to-right then top-to-bottom.\n"
            "All nine panels are NINE KEYFRAMES OF THE SAME SINGLE SHOT — they "
            "advance IN TIME from the start of this shot (panel 1) to the end "
            "(panel 9), showing how the action, the characters' poses and "
            "expressions, and the framing evolve continuously over the shot's "
            "duration. They are NOT nine different shots and NOT nine camera-angle "
            "variants of one frozen moment.\n"
            "Distribute the progression naturally across the nine panels: early "
            "panels show the start of the action, middle panels the development, "
            "and late panels the completion or reaction. The framing may shift "
            "gradually with the camera movement described for this shot, but every "
            "panel still belongs to this one continuous take.\n"
            "The TASK framing above defines the sheet's DOMINANT shot size; "
            "individual panels SHOULD still vary shot size and camera angle "
            "inside this shot (wide establishing panel -> closer emphasis "
            "panels), and this panel-to-panel variation does NOT violate the "
            "TASK framing.\n"
            + (f"Shot content to advance through (result, not step-by-step): {shot_desc}\n"
               if shot_desc else "")
            + _consistency
            + "VARIETY RULE (critical, equally important as identity): the nine "
            "panels MUST show a CONTINUOUS, VISIBLE progression — no three "
            "consecutive panels may look essentially the same. Each panel must "
            "differ from its neighbours in at least one of: character pose or "
            "body orientation, facial expression, camera framing (tighter vs "
            "wider) or angle, or the state of the key object/prop. If the shot "
            "content above is a single frozen moment with little to advance, "
            "vary the FRAMING and ANGLE between panels (wide to close-up, side "
            "to front) rather than repeating one identical composition. "
            "Uniform near-duplicate panels are a generation failure. Nine "
            "near-identical panels (four or more panels sharing the same pose "
            "AND the same framing) are a generation failure, exactly as severe "
            "as wardrobe drift.\n"
            + "TEXT RULE (important): NO text anywhere in the artwork — no dialogue, "
            "no subtitles, no captions, no signage, no watermark, and NO panel "
            "index numerals either. (Panel index numbers 1-9 are overlaid on the "
            "image by the frontend after generation; model-painted numerals come "
            "out illegible, so the model must not draw any.)")
        if any(IDENTITY_GRID_REF_MARK in (lab or "") for lab in (ref_labels or [])):
            prompt += (
                "\nPANEL-WISE IDENTITY BINDING (critical): the identity baseline "
                "grid reference shows the character('s) canonical appearance in a "
                "3x3 layout that corresponds to the 3x3 layout of the output. "
                "EVERY one of the nine output panels must show the character(s) "
                "with exactly the same facial identity, hairstyle and outfit as "
                "the baseline grid — wardrobe drift between panels (garments "
                "changing, appearing or disappearing from panel to panel) is a "
                "generation failure. The baseline grid constrains APPEARANCE "
                "only: poses, expressions, props in hand and framing must still "
                "follow the time progression described above. Identity binding "
                "constrains WHO the characters are and what they wear only — it "
                "never requires identical poses, expressions or framing between "
                "panels; the time progression described above is mandatory.")
        if style:
            prompt += f"\nOverall visual base (must hold across all nine panels): {style}."
        return prompt

    # ===================== 九宫格中文逐格版内部件（2026-10-07，用户范本「逐格写死」范式） =====================

    #: 参考图标签「角色「X」的身份锚点（…视图）：<要点>」的匹配
    #: （生成端 app._allocate_storyboard_refs 的两类角色槽位写法：主角色/次角色）。
    _GRID_CHAR_LABEL_RE = re.compile(
        r"^角色「([^」]+)」的身份锚点（([^）]+?)视图）[:：]\s*(.+)$")
    #: 特写镜头策略把角色标签改写为「已替换为该角色头部特写，…」（app._apply_closeup_ref_strategy），
    #: 此时标签里已没有角色名，需从 shot 的角色名单按位回补。
    _GRID_CHAR_CLOSEUP_MARK = "已替换为该角色头部特写"

    @staticmethod
    def _grid_characters_section(shot: dict, ref_labels: List[str] = None,
                                 has_characters: bool = True) -> str:
        """按参考图标签生成九宫格主模板的「角色设定」段（用户范本第 2 段）。

        - 每个出场角色一条：**身份与外观完全参考`<imageN>`** + 服装/气质要点
          （要点取自标签既有文案的保留句，**不重新臆造外观词** —— 官方协议明确禁止
          用文字重述五官，重述 =「重新画一个人」，反而降低 likeness）；
        - ``<imageN>`` 按**最终位置**重新编号（identity grid / 3D 基准图插入 <image1>
          之后标签里的旧编号会整体错位，位置即真相 —— 与 :meth:`build_storyboard_prompt`
          的官方协议同口径）；
        - 无角色镜头（has_characters=False）→ 显式「无出场角色 + 禁人」声明；
        - 有角色但标签解析不出（特写替换等）→ 用 shot 的角色名单（_char_ref_names →
          characters_in_shot）回补角色名，仍取不到时给出保守兜底行。永不返回空串。
        """
        shot = shot if isinstance(shot, dict) else {}
        _declared = shot.get("_char_ref_names")
        if not isinstance(_declared, (list, tuple)):
            _declared = shot.get("characters_in_shot")
        queue = [str(n).strip() for n in (_declared or [])
                 if str(n or "").strip()]
        used = set()
        lines = []
        closeup_no = 0
        for pos, raw in enumerate(ref_labels or [], start=1):
            body = _ref_label_body(raw, pos)
            if not body:
                continue
            m = ComfyUIClient._GRID_CHAR_LABEL_RE.match(body)
            if m:
                name = m.group(1).strip()
                used.add(name)
                lines.append((name,
                              f"身份与外观完全参考`<image{pos}>`，{m.group(3).strip()}"
                              f"（{m.group(2).strip()}视图）"))
                continue
            if ComfyUIClient._GRID_CHAR_CLOSEUP_MARK in body:
                closeup_no += 1
                name = next((n for n in queue if n not in used), "")
                if name:
                    used.add(name)
                lines.append((name or f"出场角色{closeup_no}",
                              f"身份与外观完全参考`<image{pos}>`；画面取景范围以该角色的"
                              "头部特写参考为准（仅肩部以上），服装发型与角色设定完全一致"))
        if not lines:
            if not has_characters:
                return ("1. **本镜无出场角色：** 九个分镜画面只呈现场景与物品，"
                        "任何一格都不得出现人物、面部或人形剪影。")
            declared = "、".join(dict.fromkeys(queue)) if queue else "画面主体"
            return (f"1. **{declared}：** 身份外观按画面内容描述呈现"
                    "（本镜未注入角色身份参考图），九个分镜之间保持完全一致。")
        return "\n".join(f"{i}. **{name}：** {points}。"
                         for i, (name, points) in enumerate(lines, start=1))

    @staticmethod
    def _build_grid_keyframes_prompt_zh(shot: dict, ref_labels: List[str] = None,
                                        style: str = "", has_characters: bool = True,
                                        panel_plans: list = None,
                                        characters_section: str = None,
                                        shot_summary: str = None) -> str:
        """渲染九宫格主模板（storyboard_grid_main）为「逐格写死」中文提示词。

        返回空串 = 模板不可用 / 规划不足 9 格，调用方（build_shot_grid_keyframes_prompt）
        应回落英文版全路径。画幅不写进正文（继承参考图，与英文版同一口径）。
        """
        shot = shot if isinstance(shot, dict) else {}
        # ---- 规划清洗：丢弃缺 content 的格，按 no 排序后**重排 1..9** ----
        # （LLM 偶发给出 10+ 格或 no 乱序；逐格版必须 9 格齐整，否则 3x3 布局与
        #   「左下角数字 1-9」标注断档。不足 9 格 → 返回空串走英文版回落。）
        plans = []
        for p in (panel_plans or []):
            if not isinstance(p, dict):
                continue
            content = str(p.get("content") or "").strip()
            if not content:
                continue
            framing = str(p.get("framing") or "").strip() or "中景"
            tone = str(p.get("tone") or "").strip()
            try:
                no = int(p.get("no") or 0)
            except (TypeError, ValueError):
                no = 0
            plans.append((no, framing, tone, content))
        if len(plans) < 9:
            return ""
        plans.sort(key=lambda t: t[0] if t[0] > 0 else 99)
        plans = plans[:9]
        plan_lines = []
        for i, (_no, framing, tone, content) in enumerate(plans, start=1):
            if not content.endswith(("。", "！", "？", "…")):
                content += "。"
            plan_lines.append(f"*   **分镜{i}（{framing}" + (f"，{tone}" if tone else "")
                              + f"）：** {content}")

        # ---- 色彩演进：由规划结果的格序色调确定性拼出（不再调模型）----
        tones = [t for (_n, _f, t, _c) in plans if t]
        uniq = list(dict.fromkeys(tones))
        if not tones:
            color_arc = ("九个分镜保持统一色调，随本镜情绪自然演化；"
                         "相邻格过渡平滑，不得突兀跳变。")
        elif len(uniq) == 1:
            color_arc = (f"九个分镜统一为「{uniq[0]}」基调，格间保持一致，"
                         "仅随光影层次产生细腻的明暗变化。")
        else:
            color_arc = ("色调自第 1 格至第 9 格按「" + " → ".join(tones)
                         + "」随情绪平滑演进，相邻格之间不得突兀跳变。")

        # ---- 台词口型提示（与英文版同语义：只给说话状态，严禁台词上屏）----
        dlg_text = _dlg_text(shot.get("dialogue"))
        if dlg_text and not has_characters:
            # 无人物镜 + 台词 = 画外音/旁白（与 build_storyboard_prompt 的 Audio only 口径一致）
            speech_note = ("本镜无出场角色，台词为画外音：九个分镜画面不画任何人、任何口型，"
                           "只以环境与物品承载画面。")
        elif dlg_text:
            speaker = _dlg_speaker(shot.get("dialogue")) or "说话角色"
            speech_note = (f"本镜台词由{speaker}说出：在对应分镜中只以自然的开口口型与"
                           "神态变化体现，九个分镜画面里严禁出现任何台词文字或字幕。")
        else:
            speech_note = ("本镜无台词：九个分镜画面均不出现开口说话的口型，"
                           "情绪靠表情与肢体动作承载。")

        # ---- 风格锚定：剔除画幅/比例词（画幅是 generation parameter，继承参考图）----
        style_clean = style_kit.normalize_style(style)
        if style_clean:
            _toks = [t for t in style_clean.split("，")
                     if not re.search(r"(比例|画幅|分辨率|竖屏|横屏|竖版|横版|竖向|横向"
                                      r"|宽屏|方形|超宽|\d{1,2}\s*[:：]\s*\d{1,2})", t)]
            style_clean = "，".join(_toks) or style_clean
        style_clean = style_clean or "与参考图一致的画风"

        if not shot_summary:
            shot_summary = (str(shot.get("description") or "").strip()
                            or str(shot.get("storyboard_prompt_zh") or "").strip()
                            or "本镜的画面内容")
        if not str(characters_section or "").strip():
            characters_section = ComfyUIClient._grid_characters_section(
                shot, ref_labels, has_characters=has_characters)

        # ---- 画面内文字段：**按需注入**（2026-10-09）----
        # 官方：不需要可读文字时「一句都不要提文字」（提了会诱发凭空画字）。
        # 故仅当本镜确有文字需求（surface_text 有值，或描述含文字语义词）才注入。
        _surface = str(shot.get("surface_text") or "").strip()
        _probe = " ".join(str(shot.get(k) or "") for k in
                          ("description", "storyboard_prompt_zh", "visual_detail"))
        text_section = (TEXT_SECTION_ZH
                        if (_surface or any(w in _probe for w in TEXT_HINT_WORDS))
                        else "")

        try:
            zh = prompt_templates.render(
                "storyboard_grid_main",
                style=style_clean,
                shot_summary=shot_summary,
                characters_section=str(characters_section),
                panel_plans="\n".join(plan_lines),
                color_arc=color_arc,
                speech_note=speech_note,
                text_section=text_section,
            )
        except Exception as e:  # noqa: BLE001 —— 模板渲染失败绝不阻断分镜生成
            logger.warning("九宫格中文逐格模板渲染异常（回落英文版）：%s", e)
            return ""
        return str(zh or "").strip()

    @classmethod
    def build_scene_grid_prompt(cls, scene_prompt, style=""):
        """场景九宫格「单次出图」版提示词：9 个机位逐格写死（1 次 T2I 出整图）。

        ⚠️ 格号与标签解耦：每行只写「第N格」+ 机位描述，**不得写 SCENE_GRID_LABELS**
        （其「特写细节A/B」里的 A/B 会让模型把格号标成 A/B —— 实验 B 实测到这个缺陷）。

        范式同 :meth:`_build_grid_keyframes_prompt_zh`（storyboard_grid_main）：
        把 9 个机位**逐格写死**进一句提示词，一次出图即得 3×3 整图，取代
        「9 档逐档独立出图 + ``scene_grid.stitch_grid`` 拼接」（实测 57s vs 8m16s）。

        :param scene_prompt: 场景设定串（app 层传 ``prompt_zh``）。
        :param style: 画面风格锚定串；写进正文前剔除画幅/比例词（画幅是 generation
            parameter，由调用方 ``size`` 决定，不写进提示词正文——与分镜同口径）。
        :return: 非空 ``str``；模板不可用/渲染失败时回落内置兜底串。
        """
        # 机位表单一来源在 config；函数内延迟 import（同 scene_view_prompt_suffix，
        # 避免 import 顺序依赖）。SCENE_GRID_* 在 config 里晚于本模块 import 的常量定义。
        try:
            from config import SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH
        except ImportError:
            SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH = (), {}

        # 逐格写死：格号 i 从 1 起，只写「第N格」+ 机位句（**不写标签**——见 docstring）。
        rows = [f"*   **第{i}格：** {SCENE_GRID_ANGLE_ZH.get(k, '')}"
                for i, k in enumerate(SCENE_GRID_VIEW_KEYS, start=1)]
        panel_plans = "\n".join(rows)

        # 风格锚定：剔除画幅/比例词（与 _build_grid_keyframes_prompt_zh 同口径）。
        style_clean = style_kit.normalize_style(style)
        if style_clean:
            _toks = [t for t in style_clean.split("，")
                     if not re.search(r"(比例|画幅|分辨率|竖屏|横屏|竖版|横版|竖向|横向"
                                      r"|宽屏|方形|超宽|\d{1,2}\s*[:：]\s*\d{1,2})", t)]
            style_clean = "，".join(_toks) or style_clean
        scene_text = str(scene_prompt or "").strip()

        try:
            text = prompt_templates.render(
                "scene_grid_main",
                style=style_clean,
                scene_prompt=scene_text,
                panel_plans=panel_plans,
            )
        except Exception as e:  # noqa: BLE001 —— 模板渲染失败绝不阻断场景生成
            logger.warning("场景九宫格单次出图模板渲染异常（回落内置兜底）：%s", e)
            text = ""
        text = str(text or "").strip()
        if not text:
            logger.warning("场景九宫格单次出图提示词为空（模板缺失/被注释占满），"
                           "回落内置兜底串")
            text = (_SCENE_GRID_MAIN_FALLBACK
                    .replace("{style}", style_clean)
                    .replace("{scene_prompt}", scene_text)
                    .replace("{panel_plans}", panel_plans))
        return text

    @staticmethod
    def crop_grid_cell(grid_path: str, cell_index: int, out_path: str,
                       cols: int = 3, rows: int = 3) -> str:
        """从九宫格图里裁出第 cell_index（0 起，行优先）格，保存并返回路径。"""
        from PIL import Image
        img = Image.open(grid_path).convert("RGB")
        w, h = img.size
        cw, ch = w / cols, h / rows
        r, c = divmod(cell_index, cols)
        box = (int(c * cw), int(r * ch), int((c + 1) * cw), int((r + 1) * ch))
        cell = img.crop(box)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        cell.save(out_path, "PNG")
        return out_path

    @staticmethod
    def build_storyboard_prompt(shot: dict, ref_labels: List[str] = None,
                                has_blocking_image: bool = False,
                                has_characters: bool = True) -> str:
        """按镜头剧情描述构建分镜图（Qwen-Image-2.1 多参考图编辑）提示词。

        :param has_blocking_image: 本次参考图里**已含** 3D 导演台构图基准图
            （``app/te_3d_render.py`` 渲染的 ``<image1>``）。为 True 时**不再**注入文字
            「Blocking — …」空间锚点行：构图已由基准图逐像素给定，再叠一条文字版
            只会在「文字说左、图说右」时制造矛盾（文字是软约束，模型可能二选一）。
            基准图缺失/渲染失败时该参数为 False，行为与旧版完全一致。
        :param has_characters: 本镜**画面内是否有出场角色**（默认 True = 旧行为逐字不变）。
            为 False（道具特写/空镜/纯画外音）时：① 在 SCENE AND ACTION 段追加一条
            「NO HUMANS IN FRAME」硬禁令；② 把台词行从「角色在说话（口型）」改写为
            「画外音/旁白，说话人不在画面里」—— 否则这两处会诱导模型为了让「说话」可见
            而凭空画一个人，与本镜「无人物」相矛盾。仅对无人物镜生效（2026-10-05）。
            ⚠️ **不要**复用/耦合 ``has_blocking_image``：基准图缺失时它也为 False，
            但缺失 ≠ 无人物镜，混用会把有角色镜误当无人物镜加错禁令。

        ## 为什么改成官方 <imageN> 协议（2026-09-25）

        2026-09-23 起图片链路整体切到 QwenImage2.1（TE-Speed 加速链），而 Qwen-Image-2.1
        官方的多图提示词协议与旧 Qwen-Edit 有**本质差别**（依据官方 Prompt Rewriter /
        PE-I2I system prompt / ComfyUI 官方 Image Edit workflow）：

        1. **必须用 ``<image1>…<imageN>`` 显式编号**引用参考图。官方明确禁用
           「第一张图 / 图 A / 左边那张」这类自然语言指代 —— 容易产生歧义。
           编号即输入顺序：``<image1>`` 通常作主要画布 / 编辑目标（edit target），
           输出画幅也跟随它。
        2. **属性解耦（Attribute Disentanglement）**：把「谁是画布 / 谁提供身份 /
           这次唯一要改什么 / 其他哪些必须保持不变」拆成各自独立的句子。官方给出的
           结构是 ``IDENTITY → CHANGE → SOURCE → PRESERVE``。
        3. ⚠️ **身份一律指向参考图，禁止用文字重述五官**。官方明确指出：一旦重新描述
           脸型/眼睛/鼻子，任务就从「保持这个人」变成「重新画一张符合这些描述的人」，
           反而**降低 likeness**。这是与旧版最大的行为差异 —— 旧的【禁令】段写的是
           「人物的脸型、发型、服装、配饰与角色参考图完全一致」，把属性又列了一遍。
        4. **Preservation Clause 用 blanket 写法**：``Keep all untargeted content
           unchanged.`` 而不是逐项罗列（每重新描述一次都可能重新触发生成）。
        5. **Resolution / aspect ratio 不写进提示词正文** —— 它属于 generation
           parameter（官方 Rewriter 单独返回 ``wh_ratio`` / ``ratio_follow``）。
           画幅由工作流的尺寸节点与参考图画幅决定（见 generate_storyboard 的 size）。

        ## 分节顺序（固定）

          TASK            → 这一镜要生成的画面（含景别/机位硬约束，最靠前）
          VISUAL BASE     → 画风实体前置锚定 + 反实拍声明（2026-10-02；无 style 时整段不出现）
          PRIMARY CANVAS  → <image1> 作主要画布
          COMPOSITION BASELINE → 带 3D 导演台站位基准图时，声明「只照它的构图摆」
                           （2026-09-27；不带基准图时整段不出现）
          IDENTITY        → 身份锚点（指向 <imageN>，不复述五官）
          REFERENCE ROLES → 每张参考图各自的职责（<image2> 只提供 X）
          SCENE / ACTION  → 场景、动作、说话状态、情绪
          LIGHTING        → 光影氛围（可选，幂等）
          STYLE           → 画面风格声明（与 VISUAL BASE **双写**，含质量尾）
          PRESERVE        → 保留子句 + 无文字硬禁令（兜底）

        历史教训：分镜图是质检重跑重灾区（教训库 68/73 条），其中「景别」占 45 条——
        扁平长提示词里景别约束被画面内容稀释。景别/机位是**取景级硬约束**，
        必须最靠前、独立成句。
        """
        camera = str(shot.get("camera") or "中景").strip()
        # A1：景别优先读权威字段 shot_type，缺失回退解析 camera 复合串（旧剧本零回归）
        cam_key = shot_framing(shot)        # "" = 本镜没给景别（camera 只有机位/运镜）
        cam_angle = camera_angle(camera)    # 俯拍 / 仰拍 / 平视 / 环绕 …（与景别正交）
        cam_spec = camera_spec(cam_key or camera)

        sections = []

        # ---------- TASK：景别/机位硬约束（最靠前）----------
        # **没给景别时绝不能编一个**硬塞进去。实测《蛊真人》ep02 shot_13
        # camera="俯拍缓推"（description 是脚部俯拍），旧实现猜成「中景：取景自腰部或
        # 膝部以上」→ 与画面描述的脚部俯拍**互斥**，模型在两条矛盾指令间摇摆，6 次重试
        # 出的全是「全景 + 平视」，而质检端又按中景判它「景别不符」→ 该镜永远过不了。
        # 改为把取景交还给画面描述。
        framing_lines = []
        if cam_key:
            # ⚠️ 必须带上 cam_spec（`camera_spec` 的显式构图规范），别只写景别两个字：
            #    只写「特写」时模型容易退化为中景/近景（实测），且 cam_spec 是**生成端
            #    与质检端共用的同一份标准**（历史坑：两端标准不同曾把 43% 判为不合格）。
            framing_lines.append(f"FRAMING (must be strictly followed): {cam_key} — {cam_spec}.")
        else:
            framing_lines.append(
                "FRAMING (not specified for this shot): follow the framing "
                "implied by SCENE AND ACTION below; do NOT default to a medium "
                "shot or wide shot.")
        if cam_angle:
            framing_lines.append(
                f"CAMERA ANGLE (must be strictly followed): {cam_angle} — "
                f"{_CAMERA_ANGLE_SPECS[cam_angle]}.")
        sections.append("TASK: Generate a single storyboard frame.\n"
                        + "\n".join(framing_lines))

        # ---------- VISUAL BASE（2026-10-02 新增：风格前置锚定）----------
        # 来源：用户提供的业界分镜提示词范式，其结构是
        #   「整体风格：视觉基底：3D国漫风格…生成九宫格…保持人物一致性」
        # —— **风格锚在提示词开头**，而不是末尾。
        #
        # 动机（对应本项目实测缺陷）：末位 STYLE 节对**全景/远景镜**牵引力不足 ——
        #   人物在画面中占比小、环境占比大时，模型更容易漂向「真人电影实拍感」。
        #   实测第1集 shot_05（全景）首轮即被判 `style_mismatch=true`
        #   （判官原话「真人电影级写实，非 3D CG」），score 85 仍被闸门拦下；
        #   补一句「3D CG 游戏过场动画质感…数字渲染特征」后即通过。
        #   → 把风格**前置**到 TASK 紧邻处，让它在注意力分配上不被画面内容稀释。
        #
        # ⚠️ **不移除末尾 STYLE**：末尾 STYLE 是**质检端同一份口径**（`camera_spec`
        #    式的生成/质检共用标准），且 `prompt_memory` 依赖它做相似度剥离。
        #    这里是**双写**（前置声明 + 末尾重申），不是搬移。
        # ⚠️ **前置节只放「画风实体」不含质量词尾**：质量尾（highly detailed 等）
        #    归 STYLE 管；两处都放会让 `prompt_qc._QUALITY_FLUFF` 剥离口径复杂化。
        shot_style_probe = style_kit.normalize_style(shot.get("style"))
        if shot_style_probe:
            _base_style_en = ""
            if hasattr(style_kit, "style_suffix_en"):
                # with_tail=False：只取风格本体，不带质量尾
                _base_style_en = style_kit.style_suffix_en(
                    shot_style_probe, with_tail=False) or ""
            if _base_style_en:
                sections.append(
                    f"VISUAL BASE (art style, must hold for the WHOLE frame): "
                    f"{_base_style_en}. This is a stylized CG render, NOT live-action "
                    f"photography and NOT a photograph of real people — every surface "
                    f"(skin, cloth, metal, stone) must show the characteristic "
                    f"digital-render look of this art style. This style holds equally "
                    f"for wide and establishing shots, where characters occupy only a "
                    f"small part of the frame; the environment must be rendered in the "
                    f"same art style, never as photographic realism.")

        # ---------- PRIMARY CANVAS + IDENTITY + REFERENCE ROLES ----------
        # 官方要点：每张参考图都必须被赋予**明确且唯一**的职责，并把身份/修改目标/
        # 保留内容分开写。这里把生成端传入的 ref_labels（形如
        # 「参考图1（<image1>）是角色「X」的身份锚点：…」）直接落成官方句式。
        if ref_labels:
            identity_lines = []
            role_lines = []
            blocking_pos = 0      # <imageN> 里「3D 导演台构图基准图」的位置（0 = 没带）
            identity_grid_pos = 0  # <imageN> 里「角色身份基准网格」参考图的位置（0 = 没带）
            # ⚠️ **必须按位置重新编号**：官方协议里 ``<imageN>`` 的 N 就是「输入顺序」
            # （``images.image_N`` 槽位序号），不是 label 里写的那个数字。label 由
            # app._allocate_storyboard_refs 生成时可能带「预留槽位号」（例如角色占 1-3、
            # 场景本应排第 4 位，但该镜只有 1 个角色 → 实际落在第 2 槽），若照抄 label 的
            # 数字，提示词会引用一个**根本没连图**的槽位 → 模型找不到对应参考图，
            # 身份/场景约束全部失效，且不报错。
            for pos, raw in enumerate(ref_labels, start=1):
                lab = _ref_label_body(raw, pos)
                if not lab:
                    continue
                if BLOCKING_REF_MARK in lab:
                    # 构图基准图单独走 COMPOSITION BASELINE 段（见下）：它的职责是
                    # 「照这个构图摆」，用 "use only for X" 的常规句式表达力度不够。
                    blocking_pos = pos
                    continue
                if IDENTITY_GRID_REF_MARK in lab:
                    # 身份基准网格单独走 IDENTITY BASELINE GRID 段（见下）：
                    # 它的职责是「九个面板的外观基准」，不进常规 IDENTITY/ROLES 句式。
                    identity_grid_pos = pos
                    continue
                # 身份锚点：官方句式 “Preserve the exact identity from <imageN>.”
                if "身份锚点" in lab:
                    identity_lines.append(
                        f"Preserve the exact identity from <image{pos}>: "
                        f"{_ref_label_purpose(lab)}. "
                        f"Keep the original facial structure, hairstyle and body "
                        f"proportions of <image{pos}> — do NOT redraw or re-describe "
                        f"the face.")
                else:
                    role_lines.append(
                        f"Use <image{pos}> only for {_ref_label_purpose(lab)}.")
            # ⚠️ 2026-10-06 事故修复：身份基准网格会被插到 <image1>（见 app._storyboard_worker），
            #    而 Qwen-Image-Edit 的 image_1 是**编辑目标/画布**。旧代码无条件写
            #    "Use <image1> as the primary canvas（and identity anchor）"，于是模型把
            #    「3×3 身份基准网格」当成要保留的画布；一旦提示词失效（优化器把思考过程
            #    当正文返回），模型就直接原样返回它 → 分镜图 = 九格几乎同图的角色基准网格。
            #    这里显式区分三种画布，并在网格当画布时给出「禁止复制参考图」的硬约束。
            _ig_is_canvas = bool(identity_grid_pos == 1 and not blocking_pos)
            body = [f"PRIMARY CANVAS: Use <image1> as the primary canvas"
                    f"{' and identity anchor' if (identity_lines and not blocking_pos and not _ig_is_canvas) else ''}."]
            if _ig_is_canvas:
                body[0] += (
                    " ⚠️ <image1> here is the APPEARANCE/ASPECT reference only — it is a "
                    "contact sheet of the character's canonical baseline views, NOT the "
                    "picture to produce. Do NOT return, copy, trace, re-tile or lightly edit "
                    "<image1>: the output must be a NEWLY RENDERED image of the SCENE AND "
                    "ACTION described below, laid out as the same 3x3 grid, where every panel "
                    "is a DIFFERENT keyframe of this shot. Reproducing the reference sheet as "
                    "the output is a total generation failure.")
            if blocking_pos:
                # 构图基准在最前时，<image1> 是**构图基准图**、不是身份锚点 —— 必须显式
                # 声明，否则模型会把人偶当成「要保留身份的人」。
                body[0] += (" It defines the framing and the spatial layout of the frame, "
                            "not any character's appearance.")
            if identity_lines:
                body.append("IDENTITY: " + " ".join(identity_lines))
            if role_lines:
                body.append("REFERENCE ROLES: " + " ".join(role_lines)
                            + " Do not merge or transfer attributes between the reference "
                              "images: each reference is responsible only for its own role.")
            if len(identity_lines) > 1:
                body.append(
                    "Each referenced character must keep its own individual identity; "
                    "do not merge facial features, hairstyles or costumes between them.")
            sections.append("\n".join(body))
            if blocking_pos:
                sections.append(COMPOSITION_BASELINE_SECTION.format(pos=blocking_pos))
            if identity_grid_pos:
                sections.append(IDENTITY_BASELINE_GRID_SECTION.format(pos=identity_grid_pos))
        else:
            sections.append(
                "PRIMARY CANVAS: No reference image is provided for this shot. "
                "Generate the frame purely from the SCENE AND ACTION and STYLE "
                "descriptions below.")

        # ---------- SCENE AND ACTION ----------
        # 画面内容优先级：
        # 1) storyboard_prompt_zh —— 提示词分析器**专门为该镜分镜图**写的中文提示词。
        #    历史缺陷：这个字段只写不读，用户花了 token 生成却从未生效（白花钱）。
        # 2) description —— 剧本自带的画面描述（默认路径）
        # 3) visual_detail —— 剧本阶段保留的扩展画面细节（时间/天气/光源方向/动作过程补全）
        desc = str(shot.get("storyboard_prompt_zh") or "").strip() \
            or (shot.get("description") or "").strip()
        detail = str(shot.get("visual_detail") or "").strip()
        if detail and detail != desc:
            # visual_detail 是描述被截断后的剩余细节，合并成完整画面主体。
            # ⚠️ 用类名调用本类 staticmethod（裸名会去模块作用域找 → NameError）。
            desc = ComfyUIClient._merge_visual_detail(desc, detail)
        location = shot.get("location", "")
        content_lines = []
        if location:
            content_lines.append(f"Location: {location}.")
        if desc:
            content_lines.append(f"Action and content: {desc}{SHOT_ACTION_SUFFIX}")
        # ---------- 2026-09-30：消费剧本的 action 字段（一镜一动作的权威产物） ----------
        # script_generator 的 schema 已让模型输出「本镜动作beat」，此前下游零消费。
        # 分镜图是单帧，把动作 beat 显式交给模型，让它抓拍这一拍最有表现力的瞬间，
        # 而不是从长描述里自己猜该画哪一刻。
        _act = str(shot.get("action") or "").strip()
        if _act and _act != desc:
            content_lines.append(
                "KEY ACTION BEAT (the single action this frame must capture — "
                f"depict its most telling moment): {_act}.")
        # ---------- P0-1：首帧/末帧/运动 三段结构（借鉴 ViMax / CineGen）----------
        # 给模型「运动起点 → 终点 → 运动类型」的显式锚点，减少动作画崩。
        # 字段缺失（旧剧本/模型未输出）时整段不出现，回落单段 description，零变化。
        _ff = str(shot.get("first_frame") or "").strip()
        _lf = str(shot.get("last_frame") or "").strip()
        _mo = str(shot.get("motion") or "").strip()
        if _ff:
            content_lines.append(
                f"STARTING FRAME (static snapshot before motion): {_ff}.")
        if _lf:
            content_lines.append(
                f"ENDING FRAME (state after motion): {_lf}.")
        if _mo:
            content_lines.append(
                "MOTION (strictly separate camera movement — push-in / pull-out / "
                "pan / track / follow / tilt — from movement within the frame — "
                f"character or object action): {_mo}.")
        if _dlg_text(shot.get("dialogue")):
            if has_characters:
                # 只给说话状态与口型提示，严禁把台词文本写进提示词（模型会把台词当画面字幕画出来）
                content_lines.append(
                    f"Speaking state: {_dlg_speaker(shot.get('dialogue')) or 'the character'} "
                    f"is quietly saying one short line, shown only as natural lip movement "
                    f"and subtle expression changes.")
            else:
                # 2026-10-05：本镜画面内无角色 → 该台词只可能是画外音/旁白。若照抄
                # 「角色在说话 / 口型」，模型会为了让「说话」可见而凭空画一个人 ——
                # 正是「无角色镜被人偶污染」的成因之一。改写为「只出声、不画人」。
                content_lines.append(
                    "Audio only: this shot carries a line of off-screen voice-over / "
                    "narration; the speaker is NOT visible in the frame. Represent it as "
                    "sound only — do NOT draw any person, face, body or lip movement to "
                    "stand in for the speaker.")
        if shot.get("emotion"):
            content_lines.append(f"Emotion and mood: {shot['emotion']}.")
        # ---------- 无人物镜头：显式禁人（2026-10-05）----------
        # 动机：道具特写/空镜（characters_in_shot=[]）历史上会因「台词兜底」被塞进一张 3D
        # 人偶基准图、或被「角色在说话」措辞误导 → 画面凭空出现人物。此处对这一类镜头显式
        # 声明画面内不得出现任何人形。⚠️ **仅 has_characters=False 时追加**；有角色镜头
        # 提示词逐字不变（防回归）。落点在本段（SCENE AND ACTION）内，**不新增协议节**，
        # 节序契约 TASK→VISUAL BASE→PRIMARY CANVAS→(COMPOSITION BASELINE)→IDENTITY→
        # REFERENCE ROLES→SCENE/ACTION→LIGHTING→STYLE→PRESERVE 保持不变。
        if not has_characters:
            content_lines.append(
                "NO HUMANS IN FRAME (strict — this shot has no on-screen characters): "
                "the frame must contain NO human figures, NO faces, NO bodies, NO human "
                "silhouettes, and NO people reflected or partially entering the frame — "
                "show only the environment, objects and props described above.")
        # ---------- 程序化站位（TE MAN 3D导演台，软约束）----------
        # 把 shot 的角色站位/机位/景别结构化翻译成一条空间锚点行（Blocking — …），
        # 增强分镜图构图稳定性。这是**软约束**：不新增协议段、不改变 TASK/PRESERVE
        # 硬约束，只追加一条 content 行；关闭开关时行为与旧版完全一致。
        # ⚠️ has_blocking_image=True 时**跳过**：构图基准图已把站位/机位逐像素给定，
        #    再叠一条文字版反而会在「文字说左、图说右」时与基准图打架（本段是软约束，
        #    模型可能二选一 → 基准图白渲）。
        if ENABLE_BLOCKING_ANNOTATION and not has_blocking_image:
            try:
                import te_3d_director  # noqa: PLC0415
                _blocking = te_3d_director.block_annotation(shot)
                if _blocking:
                    content_lines.append(_blocking)
            except Exception as _be:  # noqa: BLE001 - 站位注入失败绝不影响分镜生成
                logger.warning("镜头 %s 站位锚点注入失败（忽略）：%s",
                               shot.get("shot_id"), _be)
        if content_lines:
            sections.append("SCENE AND ACTION:\n" + "\n".join(content_lines))

        # ---------- LIGHTING（可选）----------
        # ⚠️ `_extract_light_hint` 是本类的 @staticmethod，在另一个 staticmethod 里
        # **必须用类名调用**；写成裸名 `_extract_light_hint(shot)` 会去模块作用域找，
        # 直接 NameError → 整集分镜图 100% 生成失败（实测雨夜归人 ep2 连续失败 2 次）。
        light_hint = ComfyUIClient._extract_light_hint(shot)
        if light_hint:
            sections.append(f"LIGHTING: {light_hint}.")

        # ---------- CINEMATOGRAPHY（光学/材质，2026-09-28 画质提升）----------
        # 只写具体光学与材质描述；空词会被 prompt_qc 剥掉（见常量处注释）。
        sections.append(CINEMATOGRAPHY_SECTION)

        # ---------- STYLE ----------
        # 风格与画幅：一律以镜头自带 style 为准（不硬编码国漫）。
        # ⚠️ 画幅（aspect ratio）**不进正文** —— 属于 generation parameter，由工作流
        #    尺寸节点与参考图画幅落实（官方 Rewriter 也把 wh_ratio 单独返回）。
        # ⚠️ 2026-09-28 画质提升：with_tail 由 False 翻回 **True** —— 分镜提示词此前
        #    系统性丢了 STYLE 质量尾（style_kit._QUALITY_TAIL_EN = "highly detailed,
        #    delicate lighting, stable composition, no distortion"），输出图细节量低。
        #    注意：prompt_qc._QUALITY_FLUFF 原先含 "highly detailed" 会把它剥掉，
        #    已同步把该词移出 fluff（见 prompt_qc._QUALITY_FLUFF 处注释），两处必须同改。
        shot_style = style_kit.normalize_style(shot.get("style"))
        style_clause = ""
        if shot_style:
            style_clause = style_kit.style_suffix_en(
                shot_style, with_tail=True) if hasattr(style_kit, "style_suffix_en") else ""
        if style_clause:
            sections.append(f"STYLE: {style_clause}.")
        else:
            sections.append("STYLE: Follow the visual style of the reference images; "
                            "do not change the art style on your own.")
            logger.warning("镜头 %s 缺少 style（分镜图提示词将不声明风格，建议补齐剧本 style）",
                           shot.get("shot_id"))

        # ---------- PRESERVE（兜底，最强措辞）----------
        preserve = [
            "PRESERVE:",
            "Keep all untargeted content unchanged.",
            "Keep the framing and camera angle exactly as specified in TASK.",
            "Keep the identity of every referenced character unchanged "
            "(facial identity, hairstyle, body proportions).",
            "Keep the shape, material and colour of every referenced prop unchanged.",
            "Keep the environment and atmosphere consistent with the scene reference.",
        ]
        preserve.append(
            "The image must not contain any text, subtitles, dialogue text, watermark, "
            "logo or sign (in particular no \"AI generated\" mark in the bottom-right "
            "corner).")
        sections.append("\n".join(preserve))

        return "\n\n".join(sections)

    # ===================== H3 音轨控制（生成阶段不出声，配音统一交给 QwenTTS） =====================

    # 末端封装/保存节点：只有这些节点决定最终落盘文件是否带音轨。
    # 说明：H3 工作流内部还有 H3ContinuousStitchOutputV14 等带 audio 输入的中间节点（audio 为
    # optional），它们承担 10 段之间的音频接力；这里**不切断中间节点**，避免破坏连续段链路，
    # 也避免产出"静音但仍有音频流"的伪无声文件。真正不产生音轨靠切断末端 mux 节点实现。
    H3_AUDIO_MUX_TYPES = ("CreateVideo", "CreateVideoWithAudio", "SaveVideo",
                          "SaveWEBM", "VHS_VideoCombine", "SaveAudio", "CreateAudio")
    # 兼容旧命名（早期版本使用的属性名）
    H3_AUDIO_SINK_TYPES = H3_AUDIO_MUX_TYPES

    @classmethod
    def strip_h3_audio_inputs(cls, api_prompt: dict) -> List[str]:
        """断开 H3 工作流末端封装节点的音频输入，使输出 mp4 不含音轨

        仅改动本次提交的 API prompt（内存对象），不修改磁盘上的工作流文件。
        返回被改写的节点描述列表，便于日志核对。
        """
        changed: List[str] = []
        for nid, node in (api_prompt or {}).items():
            if not isinstance(node, dict):
                continue
            ctype = str(node.get("class_type") or "")
            if ctype not in cls.H3_AUDIO_SINK_TYPES:
                continue
            inputs = node.get("inputs") or {}
            for key in ("audio", "audio1", "audio2", "audio_path", "audio_input"):
                if key in inputs and inputs.get(key) is not None:
                    old = inputs[key]
                    inputs[key] = None
                    changed.append(f"{nid}({ctype}).{key}=None(原={old})")
        return changed

    # ===================== DLSS 补帧旁路（2026-10-01） =====================

    @classmethod
    def bypass_dlss_node(cls, api_prompt: dict) -> List[str]:
        """把工作流里的 DLSS 补帧节点设为 bypass（mode=4）并重连输出直连。

        NvidiaDLSSFrameInterpolation 在 ComfyUI temp 下建工作目录，该目录被外部
        （TE 启动器/磁盘清理）周期性删除 → mkdtemp 报 WinError 3 → 收尾文件不存在
        → 整集成片丢失（实测连续 18 次提交全死于此）。本系统的 FlashVSR 超分已
        独立承担画质提升，DLSS 补帧属可选增强，旁路后少一个单点故障。

        做法：找到 DLSS 节点 → 记录它的 video 来源 → 把下游 SaveVideo 的 video
        输入改为直连 DLSS 的上游（CreateVideo），DLSS 节点设 mode=4。
        """
        changed: List[str] = []
        dlss_id = None
        for nid, node in (api_prompt or {}).items():
            if not isinstance(node, dict):
                continue
            ctype = str(node.get("class_type") or "")
            if "dlss" in ctype.lower() or "DLSS" in ctype:
                dlss_id = nid
                break
        if dlss_id is None:
            return changed
        # 找 DLSS 的 video 来源（上游 CreateVideo 的输出）
        dlss_inputs = api_prompt[dlss_id].get("inputs") or {}
        video_src = dlss_inputs.get("video")
        if not video_src:
            return changed
        # 把下游 SaveVideo 的 video 输入改直连上游
        for nid, node in api_prompt.items():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") not in ("SaveVideo", "SaveImage", "VHS_VideoCombine"):
                continue
            inputs = node.get("inputs") or {}
            vid = inputs.get("video")
            if vid and isinstance(vid, list) and str(vid[0]) == str(dlss_id):
                inputs["video"] = video_src
                changed.append(f"{nid}(SaveVideo).video: {dlss_id} → {video_src[0]}")
        # DLSS 节点标记 bypass
        api_prompt[dlss_id]["mode"] = 4
        changed.append(f"{dlss_id} mode=bypass")
        return changed

    # ===================== 随机种子注入（质检重试时用于产出不同结果） =====================

    @staticmethod
    def _inject_seed(api_prompt: dict, seed) -> List[str]:
        """把 seed 写入工作流中所有采样类节点的 seed / noise_seed 输入

        返回被改写的节点描述列表（便于日志核对）。未传 seed 或未找到采样节点时不做任何改动。
        """
        if seed is None:
            return []
        changed: List[str] = []
        for nid, node in (api_prompt or {}).items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") or {}
            ctype = str(node.get("class_type") or "")
            for key in ("seed", "noise_seed"):
                if key in inputs and not isinstance(inputs.get(key), list):
                    inputs[key] = int(seed)
                    changed.append(f"{nid}({ctype}).{key}={int(seed)}")
                    break
        return changed

    def generate_storyboard(self, prompt_zh: str, ref_images: List[str],
                            filename_prefix: str = "comic_drama_sb/shot",
                            seed: int = None, timeout: int = 3600,
                            size=None) -> dict:
        """使用 分镜生成_Qwen21.json（QwenImage2.1 参考图编辑）生成单张分镜图

        ref_images: 参考图列表（本地绝对路径或 /api/... HTTP 资源路径）。
                    2026-09-25 起模板扩到 **9 个槽位**（Qwen-Image-2.1 reference
                    stack，官方容量上限 10），按顺序对应 ``images.image_1..9``
                    （老模板 TextEncodeQwenImageEditPlus / Qwen-Edit 2511 是
                    ``image1..3``，槽位数由模板决定，本方法不假设固定值）。
                    ⚠️ 只填**实际需要**的前 N 个槽位：官方明确「10 是容量不是目标」，
                    多余槽位必须留空，塞无关图片会让模型分不清哪张该优先。
        seed:       可选随机种子（质检不达标重生成时传入，保证产出与上一次不同）。
        size:       可选 (宽, 高)，按用户敲定的画幅覆写尺寸节点（竖屏 9:16 落地）。
        """
        wf_name = WORKFLOW_TEMPLATE["storyboard_gen"]
        api_prompt, _meta = self.load_workflow(wf_name, return_meta=True)
        seed_changed = self._inject_seed(api_prompt, seed)
        if seed_changed:
            logger.info(f"分镜采样种子已注入: {seed_changed}")

        # 1) 正向提示词：只写正向字段（正负同体节点的 negative_prompt 必须原样保留）
        node_id = self._find_positive_text_node(api_prompt)
        pos_field = self._positive_field(api_prompt, node_id) if node_id else None
        if node_id is None or not pos_field:
            raise RuntimeError(f"{wf_name} 未定位到正向提示词字段（节点={node_id}）")
        api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        self.clean_conflict_negative_tokens(api_prompt)                 # P0：清理风格冲突负向词
        if size:
            hit = style_kit.apply_latent_size(api_prompt, size)
            if hit:
                logger.info("分镜图幅已覆写为 %s×%s：%s", size[0], size[1], ",".join(hit))
            else:
                # 「参考图编辑」型模板没有横向尺寸节点：
                #   老模板 FluxKontextImageScale 与 QwenImage2.1 的 TextEncodeQwenImage21.latent
                #   都按参考图尺寸出 latent → 输出画幅继承第一张参考图，
                #   由基础资产图的尺寸决定，这里空转属正常。
                logger.info("分镜工作流无尺寸节点，画幅继承参考图（%s×%s）"
                            "—— 由基础资产图尺寸决定", size[0], size[1])

        # 2) 参考图上传到 ComfyUI output 目录（LoadImageOutput 只认 output 目录 + [output] 标注）
        #    ⚠️ 槽位数从模板读，不写死：老模板 3 槽、Qwen21 模板 9 槽，同一份代码都要能用。
        slots = self._find_image_slots(api_prompt, node_id)
        slot_cap = len(slots) or 3
        uploaded: List[str] = []
        for idx, p in enumerate((ref_images or [])[:slot_cap]):
            local = self.resolve_local_path(p) if isinstance(p, str) else None
            if not local or not os.path.exists(local):
                logger.warning(f"分镜参考图不可用，已跳过: {p}")
                continue
            name = f"sb_ref_{os.path.splitext(os.path.basename(filename_prefix))[0]}_{idx + 1}.png"
            uploaded.append(self.upload_image(local, name, image_type="output"))
        if not uploaded:
            raise RuntimeError("分镜参考图全部不可用（请先完成步骤2/3/4的资产生成）")

        # 3) 逐个槽位替换参考图（不替换会残留模板里的他人图片名 → 400 Invalid image file）
        #    P0 修复（原 comfyui_client.py:870）：原实现 `uploaded[min(idx, len(uploaded) - 1)]`
        #    在参考图少于槽位时会把**最后一张**复制到所有多余槽位 —— ref_count=2（主角+场景）
        #    时 image3 变成第二张场景图，等于告诉模型"第三主体是场景"，造成主体错位/画面错乱。
        #    现改为：参考图与槽位按序一一对应；槽位多于参考图时，多余槽位复用**第一张
        #    （主角锚点）**，并在 slot_duplicates 中显式登记 + 日志告警，杜绝静默重复场景图。
        #
        #    ⚠️ 2026-09-25 补充：Qwen-Image-2.1 官方明确「参考图 10 张是容量不是目标，多余
        #    槽位塞重复图反而稀释注意力」。因此**超过参考图数量的尾部槽位不再复用锚点图**，
        #    改为**清空**（image=""，该路 LoadImageOutput 不产出 IMAGE，服务端按缺失处理）
        #    —— 只有「老模板 3 槽 / 参考图 2 张」这类**槽位本就很少**的场景才保留复用行为
        #    （否则老模板会因空槽位报缺图）。判据：槽位数 <= 4 视为紧凑模板，沿用复用。
        _COMPACT_SLOT_CAP = 4
        ref_values = []
        slot_duplicates: List[str] = []
        slot_cleared: List[str] = []
        for idx, (key, load_id) in enumerate(slots):
            if load_id is None:
                continue
            if idx < len(uploaded):
                src = uploaded[idx]
            elif slot_cap <= _COMPACT_SLOT_CAP:
                src = uploaded[0]
                slot_duplicates.append(f"{key}(slot{idx + 1})={src}")
                logger.warning(
                    f"分镜参考图槽位多于参考图（{len(slots)} 槽 / {len(uploaded)} 张）："
                    f"{key} 复用主角锚点图 {src}（已登记 slot_duplicates）")
            else:
                # 大槽位模板（Qwen-Image-2.1 9 槽）：尾部空槽位**删除**，不复用、不塞图。
                # ⚠️ 不能 `image=""`（旧实现）：LoadImageOutput 的 image 是 **required**
                #    输入，空串会走 `VideoFromFile('')` → `av.open('')`，PyAV 把空路径
                #    解析成 ComfyUI input 目录 → `av.error.PermissionError`
                #    （实测 38 个 execution_error，分镜图全灭）。正确做法是**删节点 +
                #    断连线**：从工作流里拿掉该 LoadImageOutput 节点，并从正向编辑节点
                #    删除对应的 `images.image_N` 输入键（该键是 autogrow optional，
                #    删掉不会触发 missing_required）。
                # ⭐ 2026-10-07（增强节点固化后新增）三步联动清理，缺一会把
                # **悬空连线**带进执行路径（ComfyUI 从输出节点遍历校验，引用不存在的
                # 节点 id 直接 400）：
                #   ① 摘掉编码节点的 images.image_N 键；
                #   ② 摘掉模板固化增强节点里**同序**的参考图输入（图片N）——否则
                #      增强节点仍引用中间缩放层，而缩放层引用了被删的 Load；
                #   ③ 中间缩放层（ImageScale* / FluxKontext*）若已无任何消费者，
                #      一并删掉（它的 image 输入指向刚被删的 Load，留它就是悬空）。
                #   注：增强节点固化前 ②③ 不清理也无害（它们是孤儿节点，ComfyUI
                #   只从输出节点遍历校验）；固化后增强节点在图上，必须同步清理。
                _mid_id = None
                if node_id:
                    _pos_node = api_prompt.get(node_id) or {}
                    _pos_inputs = _pos_node.get("inputs") or {}
                    _mid_val = _pos_inputs.get(key)
                    if isinstance(_mid_val, list) and len(_mid_val) == 2:
                        _mid_id = str(_mid_val[0])
                    _pos_inputs.pop(key, None)
                _enh_id = next(
                    (_k for _k, _n in api_prompt.items()
                     if isinstance(_n, dict)
                     and str(_n.get("class_type") or "") == PROMPT_ENHANCER_CLASS),
                    None)
                if _enh_id is not None and idx < len(PROMPT_ENHANCER_IMAGE_FIELDS):
                    _enh_in = (api_prompt.get(_enh_id) or {}).get("inputs") or {}
                    _enh_in.pop(PROMPT_ENHANCER_IMAGE_FIELDS[idx], None)
                if _mid_id and _mid_id != str(load_id):
                    _still_used = any(
                        isinstance(_v, list) and len(_v) == 2
                        and str(_v[0]) == _mid_id
                        for _n2 in api_prompt.values()
                        if isinstance(_n2, dict)
                        for _v in (_n2.get("inputs") or {}).values())
                    if not _still_used:
                        api_prompt.pop(_mid_id, None)
                api_prompt.pop(load_id, None)
                slot_cleared.append(key)
                continue
            ctype = api_prompt[load_id].get("class_type")
            value = self.annotate_file_ref(src, ctype)
            api_prompt[load_id]["inputs"]["image"] = value
            ref_values.append(f"{key}->{load_id}({ctype})={value}")
        if slot_duplicates:
            logger.warning(f"分镜参考图存在槽位复用 {len(slot_duplicates)} 处: {slot_duplicates}")
        if slot_cleared:
            logger.info(f"分镜参考图空槽位已留空 {len(slot_cleared)} 处（Qwen-Image-2.1 "
                        f"「容量非目标」，不塞重复图）: {slot_cleared}")
        logger.info(f"分镜参考图已注入 {len(ref_values)} 个槽位（共 {len(slots)} 槽）: {ref_values}")

        # 4) 输出文件名前缀
        for _nid, n in api_prompt.items():
            if n.get("class_type") in ("SaveImageAdvanced", "SaveImage") and "filename_prefix" in n["inputs"]:
                n["inputs"]["filename_prefix"] = filename_prefix

        report = self.validate_api_prompt(api_prompt)
        if report["unknown_types"] or report["missing_required"] or report["dangling_links"]:
            logger.warning(f"分镜提交前自检异常: {json.dumps(report, ensure_ascii=False)[:400]}")

        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id, timeout=timeout)
        files = self.get_output_files(history, ".png")
        return {"prompt_id": prompt_id, "files": files, "history": history,
                "refs_used": uploaded, "prompt_node": node_id, "seed": seed,
                "slot_duplicates": slot_duplicates, "slot_cleared": slot_cleared,
                "slot_count": len(slots)}

    # ===================== 视频生成（MiniMax H3） =====================
    # D-08（P2）：原先此处有一个 [LEGACY · 已停用] 的固定「10 段模板单次生成」方法，
    # 函数名却在语义上像是「生成一个视频」——新同学按名字调用会让每个分镜都跑 10 段。
    # 经全仓（含 frontend/）核实**零调用者**，已删除；H3 视频生成统一走下方
    # generate_h3_sequence / generate_h3_sequence_sequential（段数 = 传入分镜数）。
    # 防回潮守卫见 verify_legacy_generate_video.py。


    def generate_h3_sequence_sequential(
        self,
        segments: List[dict],
        filename_prefix: str = "comic_drama/episode",
        seed: int = None,
        timeout_per_segment: int = 900,
        template_file: str = None,
        size=None,
        qc_fn=None,
        qc_cfg: dict = None,
        qc_style: str = "",
        max_retries: int = 2,
        qc_stop_cb=None,
        seg_audios: List[List[str]] = None,
        audio_mode: str = None,
        common_refs: List[str] = None,
        common_ref_audios: List[str] = None,
        common_prompt: str = None,
        build_only: bool = False,
        save_build_to: str = None,
    ) -> dict:
        """H3 整集视频生成（N 段一个工作流，原生 H3ContinuousSeamlessJoinV14 衔接）
        + 整片 QC 门控。

        设计说明：H3 多段无缝衔接依赖同一工作流内 previous_latent / handover
        连线（段实例间张量传递），无法拆成多次独立 ComfyUI 任务再跨任务喂隐变量。
        因此「逐段提交」在 H3 层级不成立；本方法保持 N 段一次提交，产出 **单个
        连续整集视频**，生成后对整片做抽帧质检，不通过则整片重试（换随机种子）。

        - segments: 同 generate_h3_sequence
        - qc_fn: 可选；qc_fn(video_path, shot_desc, qc_cfg, style) 返回
          {"passed": bool, "verdict": {...}, ...}；通过才保留成片，不通过则整片重试
        - qc_stop_cb: 可选；G1 止损回调 qc_stop_cb(qc_results) -> (stop, detail)。
          连续两次缺陷完全相同时提前停止整片重试，避免白烧 GPU（最贵的一处）。
        - max_retries: 整片 QC 不通过时最多重试次数（换随机种子）
        返回 dict：
          {"files": [video_path], "segments": [...], "qc_results": [...],
           "failed": bool, "attempts_used": int, "prompt_id": str}
        """
        segs = [dict(s or {}) for s in (segments or [])]
        if not segs:
            raise ValueError("generate_h3_sequence_sequential: segments 不能为空")
        n = len(segs)
        logger.info(f"[H3-episode] 整集生成：{n} 段一次提交，qc_fn={bool(qc_fn)}, "
                    f"max_retries={max_retries}")

        if build_only:
            # 工作流导出模式（2026-10-03）：只构建一次 UI 工作流（内存返回），
            # 跳过整个「提交 → 等待成片 → 整片 QC 重试」循环 —— 零 GPU 消耗。
            _r = self.generate_h3_sequence(
                segments=segs, filename_prefix=filename_prefix, seed=seed,
                timeout_per_segment=timeout_per_segment, template_file=template_file,
                save_build_to=save_build_to, build_only=True, size=size,
                seg_audios=seg_audios, audio_mode=audio_mode,
                common_refs=common_refs, common_ref_audios=common_ref_audios,
                common_prompt=common_prompt)
            return {"files": [], "build_only": True,
                    "workflow": _r.get("workflow"),
                    "layout": _r.get("layout") or {},
                    "saved": _r.get("saved") or save_build_to or "",
                    "segments": [], "qc_results": [], "failed": False,
                    "attempts_used": 0, "prompt_id": "", "segment_count": n}

        qc_results: List[dict] = []
        attempts_used = 0
        cur_seed = seed
        best_file = None       # 最后一次生成成功的文件（供 QC 全不通过时兜底）
        last_prompt_id = ""
        passed = False
        last_seg_report: List[dict] = []
        last_error: str = ""

        for attempt in range(max_retries + 1):
            attempts_used = attempt + 1
            if attempt > 0:
                # ⚠️ 必须是真随机数，不能写 None：_inject_seed 对 None 直接 `return []`
                #    （= 不注入），于是沿用工作流 JSON 里的**字面量 seed**；而前端的
                #    `control_after_generate: randomize` 属于 widgets_values，API 模式不提交。
                #    实测（2026-09-20）：这让三次"重试"提交完全相同的 prompt+参考图+seed，
                #    产物逐字节相同 —— 22~44 段 H3 一次跑几十分钟，属纯白烧 GPU。
                cur_seed = random.randint(1, 2 ** 31 - 1)
                logger.info(f"[H3-episode] 第 {attempt + 1} 次整片重试（换种子 {cur_seed}）")

            # ---------- 生成（N 段一个工作流 → 单个连续成片） ----------
            result = None
            try:
                result = self.generate_h3_sequence(
                    segments=segs,
                    filename_prefix=filename_prefix,
                    seed=cur_seed,
                    timeout_per_segment=timeout_per_segment,
                    template_file=template_file,
                    size=size,
                    seg_audios=seg_audios,
                    audio_mode=audio_mode,
                    common_refs=common_refs,
                    common_ref_audios=common_ref_audios,
                    common_prompt=common_prompt,
                )
            except RuntimeError as e:
                # S12：确定性输入错误（如某段无可用参考图被拒绝提交）——
                # 重试必然同败，立即停止并保留原因，不再空转换种子。
                last_error = str(e)
                logger.warning(f"[H3-episode] 生成被拒绝（不再重试）: {e}")
                break
            except cancellation.Cancelled:
                # ⚠️ 中止信号（用户点暂停 / 进程退出）必须穿透整个重试循环 ——
                # 否则「暂停」只会打断**当前这一次**提交，紧接着 attempt+1 换种子
                # 把 84 段整片重新提交一遍（实测：暂停后日志立刻出现
                # 「第 3 次整片重试（换种子 …）」+「段1/84 注入完成」），
                # ComfyUI 队列又被灌满，用户看到的现象就是「暂停没停住」。
                # 这里直接上抛，由上层（app._video_generate_worker / pipeline）落
                # cancelled 态；已完成镜头保留，可续跑。
                logger.warning("[H3-episode] 收到中止信号，终止整片重试循环（不再换种子重投）")
                raise
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次生成异常: {e}")
                continue

            files = result.get("files") or []
            last_prompt_id = result.get("prompt_id") or last_prompt_id
            last_seg_report = result.get("segments") or []
            if not files:
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次：ComfyUI 未返回视频文件")
                continue
            if not os.path.isfile(files[0]):
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次：文件不存在 {files[0]}")
                continue

            best_file = files[0]

            # ---------- 整片 QC 门控 ----------
            if qc_fn:
                # 用所有段提示词拼接代表整片
                shot_desc = "\n".join((s.get("prompt") or "") for s in segs)
                try:
                    qc_result = qc_fn(best_file, shot_desc, qc_cfg, qc_style)
                except Exception as qc_err:
                    # A-1：QC 回调抛异常 = **不可判定**（接口/ffmpeg 不可用，与内容无关），
                    # 不是"内容不达标"。旧实现 `continue` 会换种子重跑整集，且绕过 qc_stop_cb
                    # 止损 —— 每次整片 20~44 段 H3、几十分钟 GPU，纯白烧。这里 break：
                    # best_file 已就位，成片照常返回给调用方人工复核。
                    logger.warning(
                        f"[H3-episode] 第 {attempt + 1} 次 QC 调用异常"
                        f"（不可判定，不重试）: {qc_err}")
                    qc_results.append({"attempt": attempt + 1, "passed": None,
                                       "unavailable": True,
                                       "reason": f"QC 异常: {qc_err}"})
                    break
                qc_passed = qc_result.get("passed", False)
                _v = qc_result.get("verdict") or {}
                qc_results.append({
                    "attempt": attempt + 1, "passed": qc_passed,
                    "reason": _v.get("reason") or "",
                    "critical_issues": _v.get("critical_issues") or [],
                    "issues": _v.get("issues") or [],
                    "file": best_file,
                })
                if qc_passed:
                    logger.info(f"[H3-episode] 第 {attempt + 1} 次整片 QC 通过")
                    passed = True
                    break
                # G1 止损铺开：整片重试最贵，连续两次缺陷一字不差 → 提前停（换 seed 只是换骰子）
                if qc_stop_cb is not None:
                    _ep_stop, _ep_detail = qc_stop_cb(qc_results)
                    if _ep_stop:
                        logger.warning(
                            f"[H3-episode] 整片重试止损：连续 {len(qc_results)} 次缺陷完全相同"
                            f"（{_ep_detail}），提前停止；建议改段提示词/剧本后单独重跑")
                        qc_results[-1]["retry_stopped"] = True
                        qc_results[-1]["retry_stopped_features"] = _ep_detail
                        break
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次整片 QC 不通过"
                               f"（reason={(_v.get('reason') or '-')})，"
                               f"{'重试' if attempt < max_retries else '放弃'}")
            else:
                logger.info("[H3-episode] 整片生成成功（无 QC 门控）")
                passed = True
                break

        if not best_file:
            logger.error(f"[H3-episode] {n} 段整集生成失败（无成片）{('：' + last_error) if last_error else ''}")
            return {
                "files": [], "segments": last_seg_report,
                "qc_results": qc_results, "failed": True,
                "error": last_error,
                "attempts_used": attempts_used, "prompt_id": last_prompt_id,
                "segment_count": n,
            }

        logger.info(f"[H3-episode] 完成：产物 {best_file}，通过={passed}，"
                    f"attempts={attempts_used}")
        return {
            "files": [best_file],
            "segments": last_seg_report,
            "qc_results": qc_results,
            "failed": not passed,
            "attempts_used": attempts_used,
            "prompt_id": last_prompt_id,
            "segment_count": n,
        }

    def generate_h3_sequence(self, segments: List[dict],
                             filename_prefix: str = "comic_drama/episode",
                             emit_audio: bool = None,
                             seed: int = None,
                             timeout: int = None,
                             timeout_per_segment: int = 900,
                             template_file: str = None,
                             save_build_to: str = None,
                             build_only: bool = False,
                             size=None,
                             seg_audios: List[List[str]] = None,
                             audio_mode: str = None,
                             common_refs: List[str] = None,
                             common_ref_audios: List[str] = None,
                             common_prompt: str = None) -> dict:
        """H3 多段一次生成：**工作流段数 = len(segments)**，一个分镜对应一段。

        两条实现路径（按**模板结构**自动分流，见 `_use_director_builder`）：

        * **Director 路径**（模板含 ``MiniMaxH3Director``，2026-09-27 起为默认）：
          一个``MiniMaxH3Director`` 节点吃整条 ``timeline_data``，段间衔接由插件原生
          「段间引导」承担（上一段尾部 22 帧钉进下一段 conditioning 后裁掉前缀），
          外接 ``MiniMaxH3DirectorRefine`` 做二采。参考图走 ``segment.refs``。
        * **连续拼接路径**（旧模板）：按段数**重建整张图**，段间由
          ``H3ContinuousSeamlessJoinV14`` 做 latent 级无缝续接。

        segments: [{"prompt": str, "duration": float, "reference_images": [本地绝对路径, ...],
                    "name": str}]  —— 元素顺序 = 工作流段顺序
        seg_audios: 可选，逐段**本地音频绝对路径**列表（``seg_audios[i]`` 服务 ``segments[i]``）。
                    非空时 audio_mode 默认切 ``source``（H3 用参考音频驱动口型/节奏），
                    音频写进 ``segment.refAudios``。
        audio_mode: 可选显式指定 ``generate|mute|source``；None 时自动推导。
        common_refs: 可选，**公共参考图**本地绝对路径列表（有序，H3 Director 公共参数，
                    2026-09-30）。仅 Director 路径支持；旧连续拼接路径忽略该参数并告警。
                    调用方必须已按同一顺序把它们排进每段提示词的 ``<Picture 1..K>``。
                    见 :meth:`_generate_h3_sequence_director`。
        timeout:  总超时（秒）；None 时按 1200 + timeout_per_segment × 段数 估算
        timeout_per_segment: 单段预估耗时（默认 900s，用于总超时兜底）
        save_build_to: 可选，把重建后的 UI 工作流落盘（便于复现/排障）
        size: 可选 (宽, 高)，按用户敲定的画幅覆写模板分辨率（竖屏 9:16 → (544, 960)）
        """
        segs = [dict(s or {}) for s in (segments or [])]
        if not segs:
            raise ValueError("generate_h3_sequence: segments 不能为空")
        n = len(segs)
        tpl_name = template_file or self._default_h3_template()
        tpl_path = resolve_workflow_path(tpl_name)

        # ---- 分流：Director 模板 / 旧连续拼接模板 ----
        if self._use_director_builder(tpl_path):
            return self._generate_h3_sequence_director(
                segs, tpl_path=tpl_path, tpl_name=tpl_name,
                filename_prefix=filename_prefix, emit_audio=emit_audio, seed=seed,
                timeout=timeout, timeout_per_segment=timeout_per_segment,
                save_build_to=save_build_to, build_only=build_only, size=size,
                seg_audios=seg_audios, audio_mode=audio_mode,
                common_refs=common_refs,
                common_ref_audios=common_ref_audios,
                common_prompt=common_prompt)

        if common_refs:
            # 旧连续拼接路径按段重建子图、没有「公共参数」概念（global.refs 零引用）。
            # 静默丢弃会把「公共资产锁定」悄悄变成「没锁定」，故显式告警。
            logger.warning(
                "[H3] 旧连续拼接路径不支持公共参考图（MJSCXT_H3_BUILDER=%s），"
                "已忽略 %d 张公共图；如需公共参数请用 Director 模板",
                os.environ.get("MJSCXT_H3_BUILDER") or "auto", len(common_refs))
        if common_ref_audios:
            logger.warning(
                "[H3] 旧连续拼接路径不支持公共参考音色（MJSCXT_H3_BUILDER=%s），"
                "已忽略 %d 支公共音色；如需公共音色请用 Director 模板",
                os.environ.get("MJSCXT_H3_BUILDER") or "auto", len(common_ref_audios))
        if common_prompt:
            logger.warning(
                "[H3] 旧连续拼接路径不支持公共提示词 subject lock，已忽略；"
                "如需公共锁定请用 Director 模板")

        builder = H3EpisodeBuilder(tpl_path)
        default_duration = float((segs[0] or {}).get("duration") or 5.0)
        wf, layout = builder.build(n, duration=default_duration,
                                   resolution_override=tuple(size) if size else None)
        if build_only:
            # 只构建不提交（2026-10-03 工作流导出模式）：UI 工作流**随结果内存返回**，
            # 由调用方（app.py，用其既有 atomic_write_json）落盘 —— 本层不再直接写文件，
            # 也不进 to_api / GPU 提交。供人工在 ComfyUI 里打开检查或离线复现。
            logger.info("[H3][build_only] 工作流已构建（不落盘不提交），UI 节点 %s / 连线 %s",
                        layout.get("node_total"), layout.get("link_total"))
            return {"build_only": True, "files": [], "saved": "", "workflow": wf,
                    "layout": {"node_total": layout.get("node_total"),
                               "link_total": layout.get("link_total"),
                               "segments": len(segs)}}
        if save_build_to:
            os.makedirs(os.path.dirname(save_build_to), exist_ok=True)
            with open(save_build_to, "w", encoding="utf-8") as f:
                json.dump(wf, f, ensure_ascii=False)
            layout["build_path"] = save_build_to
        logger.info(f"H3 动态工作流已就绪：{n} 段（模板 {tpl_name}），"
                    f"UI 节点 {layout['node_total']} / 连线 {layout['link_total']}")

        api_prompt, meta = self.to_api(wf, return_meta=True)
        seed_changed = self._inject_seed(api_prompt, seed)
        if seed_changed:
            logger.info(f"视频采样种子已注入: {seed_changed}")

        # ---------------- 逐段注入（提示词 / 时长 / 参考图） ----------------
        seg_report: List[dict] = []
        for i, seg in enumerate(segs):
            lay = layout["segments"][i]
            prompt_text = seg.get("prompt") or ""
            api_prompt[str(lay["prompt_node"])]["inputs"]["text"] = prompt_text
            dur = float(seg.get("duration") or default_duration)
            api_prompt[str(lay["duration_node"])]["inputs"]["value"] = dur

            local_refs: List[str] = []
            for img in (seg.get("reference_images") or []):
                local = self.resolve_local_path(img)
                if local and os.path.exists(local):
                    local_refs.append(local)
                elif img:
                    logger.warning(f"段{i + 1} 参考图不可用，已跳过: {img}")
            # S12（P0）：参考图为空时不再静默沿用模板自带的 LoadImage 示例图——
            # 示例图里的人物会污染角色外观，成片出现与剧本无关的人物。
            # fail-fast：直接抛错让整次 H3 提交失败，上层 worker 感知并重试/跳过，
            # 而不是烧完 GPU 才拿到一个含无关人物的成片。
            if not local_refs:
                raise RuntimeError(
                    f"段{i + 1}（{seg.get('name') or f'seg_{i + 1}'}）无任何可用参考图，"
                    f"已拒绝提交（S12：模板示例图会污染角色外观）。请补齐该段 "
                    f"reference_images 后重试。"
                )
            loaded: List[dict] = []
            slots = lay["ref_nodes"]
            # 参考图不足时用本段第一张参考图补齐空槽（纯单参考即重复同一张）。
            fill_src = local_refs[0]
            up_cache: dict = {}
            for j in range(len(slots)):
                is_fill = j >= len(local_refs)
                rp = local_refs[j] if not is_fill else fill_src
                try:
                    if rp not in up_cache:
                        # 参考图上传到 ComfyUI input 目录（段实例用 LoadImage 读取）
                        up_cache[rp] = self.annotate_file_ref(
                            self.upload_image(rp, image_type="input"), "LoadImage")
                    val = up_cache[rp]
                    api_prompt[str(slots[j])]["inputs"]["image"] = val
                    loaded.append({"slot": slots[j], "src": rp, "value": val})
                except Exception as e:
                    logger.warning(f"段{i + 1} 参考图上传失败 {rp}: {e}")
            seg_report.append({"index": i, "name": seg.get("name") or f"seg_{i + 1}",
                               "inst": lay["inst"], "prompt_len": len(prompt_text),
                               "prompt_head": prompt_text[:60], "duration": dur,
                               "refs": loaded})
            logger.info(f"段{i + 1}/{n} 注入完成：时长 {dur}s，参考图 {len(loaded)} 张")

        # ---------------- 保存文件名前缀 ----------------
        for nid, node in api_prompt.items():
            if node.get("class_type") in ("SaveVideo", "SaveImage", "SaveImageAdvanced", "VHS_VideoCombine"):
                if "filename_prefix" in node["inputs"]:
                    node["inputs"]["filename_prefix"] = filename_prefix

        # ---------------- 音轨策略（与 generate_video 一致：默认静音，后续统一配音） ----------------
        emit_audio = H3_EMIT_AUDIO if emit_audio is None else bool(emit_audio)
        audio_changed: List[str] = []
        if not emit_audio:
            audio_changed = self.strip_h3_audio_inputs(api_prompt)
            if audio_changed:
                logger.info(f"H3 音轨已断开（生成阶段不出声）: {audio_changed}")
            else:
                logger.warning("H3 音轨断开未命中任何节点，请检查工作流是否变更（成片可能仍带音轨）")

        self.clean_conflict_negative_tokens(api_prompt)   # P0：清理与 3D 正向风格冲突的负向词

        report = self.validate_api_prompt(api_prompt)
        if report["unknown_types"] or report["missing_required"] or report["dangling_links"]:
            logger.warning(f"H3 提交前自检异常: {json.dumps(report, ensure_ascii=False)[:400]}")

        # DLSS 补帧旁路（2026-10-01）：NvidiaDLSSFrameInterpolation 的 temp 工作目录
        # 被外部周期性删除 → mkdtemp WinError 3 → 收尾文件不存在 → 整集成片丢失。
        # 本系统 FlashVSR 超分已独立承担画质，DLSS 插帧属可选增强，跳过不失衡。
        if H3_DISABLE_DLSS:
            dlss_bypassed = self.bypass_dlss_node(api_prompt)
            if dlss_bypassed:
                logger.info(f"H3 DLSS 补帧已旁路（temp 不稳定）: {dlss_bypassed}")

        timeout = timeout or int(1200 + timeout_per_segment * n)
        logger.info(f"H3 提交：{n} 段，总超时 {timeout}s（单段预估 {timeout_per_segment}s）")
        # 崩溃免重渲（2026-09-29）：同 Director 路径，见 submit_resumable 文档。
        history, prompt_id, resumed = self.submit_resumable(
            api_prompt, job_key=f"h3|{filename_prefix}",
            timeout=timeout, file_ext=".mp4", label=f"H3 {n} 段")
        if resumed:
            logger.info("H3(旧连续拼接路径) 本次为**免重渲复用**（未消耗 GPU）")
        files = self.get_output_files(history, ".mp4")

        audio_check = []
        if files:
            from media_probe import probe_media  # 2026-10-08 解耦：探测走叶子模块，不再依赖后期模块
            for f in files:
                m = probe_media(f)
                audio_check.append({"file": f, "has_audio": m.get("has_audio"),
                                    "audio_streams": m.get("audio_streams"),
                                    "video_streams": m.get("video_streams"),
                                    "error": m.get("error")})

        return {"prompt_id": prompt_id, "files": files, "history": history,
                "segment_count": n, "segments": seg_report, "layout": layout,
                "meta": meta, "seed": seed, "emit_audio": bool(emit_audio),
                "audio_disconnected": audio_changed, "audio_check": audio_check,
                "timeout": timeout, "template": tpl_name,
                "validate_report": report}

    # ================= H3 · Director 路径（2026-09-27 起为默认） =================

    #: 参考图上传到 ComfyUI input 下的子目录（带内容哈希前缀，杜绝跨集同名覆盖）
    H3_DIRECTOR_REF_SUBDIR = "mscxt_h3_refs"

    @staticmethod
    def _default_h3_template() -> str:
        """未显式指定 template_file 时的 H3 模板名（跟随 ``MJSCXT_H3_BUILDER`` 回退）。

        ⚠️ 必须与 ``_use_director_builder`` 同源：若只让分流函数回退而模板名不回退，
        ``H3EpisodeBuilder`` 会拿到 Director 模板 → 抛「模板中未找到 H3 段实例」，
        回退开关就是个假的。
        """
        override = str(os.environ.get("MJSCXT_H3_BUILDER") or "").strip().lower()
        if override in ("legacy", "old", "episode", "continuous", "join"):
            return (WORKFLOW_TEMPLATE.get("h3_video_legacy")
                    or WORKFLOW_TEMPLATE["h3_video"])
        return WORKFLOW_TEMPLATE["h3_video"]

    @staticmethod
    def _use_director_builder(tpl_path: str) -> bool:
        """模板是否走 Director 插件路径？

        判定顺序：环境变量 ``MJSCXT_H3_BUILDER``（``director`` / ``legacy``）显式覆盖
        → 否则看模板**结构**（``h3_director_builder.is_director_template``，绝不看文件名）。

        为什么要留开关：Director 工作流与旧连续拼接工作流的**段间衔接机制完全不同**
        （插件原生「段间引导」vs ``H3ContinuousSeamlessJoinV14`` latent 交接）。
        出问题时需要能一键退回旧路径做对照，而不必改代码。
        """
        override = str(os.environ.get("MJSCXT_H3_BUILDER") or "").strip().lower()
        if override in ("legacy", "old", "episode", "continuous", "join"):
            logger.info("H3 构建路径被 MJSCXT_H3_BUILDER=%s 强制为「旧连续拼接」", override)
            return False
        if override in ("director", "new"):
            logger.info("H3 构建路径被 MJSCXT_H3_BUILDER=%s 强制为「Director」", override)
            return True
        return h3_director_builder.is_director_template(tpl_path)

    def _upload_h3_director_ref(self, local_path: str,
                                cache: Dict[str, Optional[str]] = None) -> Optional[str]:
        """上传一张参考图到 ComfyUI ``input/mscxt_h3_refs/``，返回可写进 ``imageFile`` 的相对名。

        ⚠️ 必须带**路径哈希前缀**：``upload_image`` 默认用 basename 且 ``overwrite=true``，
        不同集的 ``shot_01.png`` 会互相覆盖 → 后面所有镜头参考图全串成同一张
        （跨集连跑时是静默错，比报错难查得多）。
        """
        # 审计 P2-20（2026-09-29）：cache key 统一 normcase(normpath(abspath)) ——
        # 调用方的公共图/段级去重键都先 normcase，这里不 normcase 时同一路径
        # 大小写不同的两次引用会 cache miss → 同一文件以两个哈希名重复上传。
        key = os.path.normcase(os.path.normpath(os.path.abspath(local_path)))
        if cache is not None and key in cache:
            return cache[key]
        base = os.path.basename(key) or "ref.png"
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".png"
        tag = hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:12]
        name = f"{self.H3_DIRECTOR_REF_SUBDIR}/{stem}_{tag}{ext}"
        try:
            val = self.upload_image(key, name=name, image_type="input")
        except Exception as e:  # noqa: BLE001 - 单张失败不该毁掉整次提交
            logger.warning(f"[H3-Director] 参考图上传失败 {key}: {e}")
            val = None
        if cache is not None:
            cache[key] = val
        return val

    def _upload_h3_director_audio(self, local_path: str,
                                  cache: Dict[str, Optional[str]] = None) -> Optional[str]:
        """上传一段参考音频到 ComfyUI ``input/mscxt_h3_refs/``，返回写进 ``audioFile`` 的相对名。

        与 ``_upload_h3_director_ref`` 对称：同样带路径哈希前缀防跨镜同名覆盖。
        音频走 ``/upload/image`` 端点（ComfyUI 的通用 input 文件上传，不校验 MIME），
        但 content-type 用音频类型避免误导。
        """
        # 审计 P2-20（2026-09-29）：cache key 统一 normcase(normpath(abspath)) ——
        # 调用方的公共图/段级去重键都先 normcase，这里不 normcase 时同一路径
        # 大小写不同的两次引用会 cache miss → 同一文件以两个哈希名重复上传。
        key = os.path.normcase(os.path.normpath(os.path.abspath(local_path)))
        if cache is not None and key in cache:
            return cache[key]
        base = os.path.basename(key) or "audio.wav"
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".wav"
        tag = hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:12]
        name = f"{self.H3_DIRECTOR_REF_SUBDIR}/{stem}_{tag}{ext}"
        ctype = "audio/wav" if ext.lower() in (".wav",) else (
            "audio/mpeg" if ext.lower() == ".mp3" else "audio/flac")
        try:
            url = f"{self.base_url}/upload/image"
            with open(key, "rb") as f:
                files = {"image": (name.split("/")[-1], f, ctype)}
                data = {"overwrite": "true", "type": "input",
                        "subfolder": self.H3_DIRECTOR_REF_SUBDIR}
                resp = requests.post(url, files=files, data=data, timeout=120)
                resp.raise_for_status()
                result = resp.json()
                uploaded = result.get("name", name.split("/")[-1])
                sub = result.get("subfolder") or self.H3_DIRECTOR_REF_SUBDIR
                val = f"{sub}/{uploaded}" if sub else uploaded
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[H3-Director] 参考音频上传失败 {key}: {e}")
            val = None
        if cache is not None:
            cache[key] = val
        return val

    def _drop_missing_director_refs(self, wf: dict, seg_ref_names, seg_audio_names,
                                    seg_count: int, common_names=None) -> None:
        """提交前校验 Director timeline 声明的参考图/音频是否已落到 ComfyUI ``input/``。

        根因竞态：``_upload_h3_director_ref`` 走 HTTP 上传（``upload_image``），返回的是
        成功后的相对名；但插件 ``_load_refs`` 在**采样那一刻**才按 ``imageFile`` 去
        ``input/`` 读文件。若个别文件上传成功响应已返回、磁盘落盘仍有极小窗口，
        插件首采会读不到 tensor → 误报「gen segment #N ... has no reference media」。
        该 warning 不致命（下一段/重采图已就位即恢复），但会误导排查。

        本方法在 ``queue_prompt`` **之前**做就地校验：逐段核对 ``timeline_data`` 里
        声明的每段 ``imageFile`` / ``audioFile`` 是否真实存在于
        ``COMFYUI_INPUT_DIR`` 下；缺失的从该段摘掉并记日志。插件读到的是
        「实际在位的图」，不再有「声明了却没图」的段 → 彻底消除该竞态告警。

        ``common_names``：``global.refs``（公共参考图）的预期内容。公共块缺一张会
        **整体左移后续所有 ``<Picture N>``**（比段级少一张严重得多），故这里用
        ``logger.error`` 明确喊出来（编号已与提示词不一致，人工复核时优先看这条）。

        安全性：
        - 整次提交的 ref 全缺 → 上游已有 S12 红线（``not any(seg_ref_names)`` 直接抛
          RuntimeError 拒提），不会走到这里；本方法只处理「个别段个别文件缺」，摘掉后
          仍保留有图的段，非首段靠段间引导钉尾帧，影响有限（与上游 empty_idx 口径一致）。
        - 正常情况（全部就位）本方法是 no-op，零行为变更。
        """
        director = next((nd for nd in wf.get("nodes", []) if nd.get("class_type") == "MiniMaxH3Director"
                         or nd.get("type") == "MiniMaxH3Director"), None)
        if not director:
            return
        named = director.get("widgets_values_named") or {}
        raw = named.get("timeline_data")
        if not raw:
            return
        try:
            tl = json.loads(raw)
        except Exception:
            return
        segs_tl = tl.get("segments") or []
        base = COMFYUI_INPUT_DIR
        if not base or not os.path.isdir(base):
            return  # 拿不到 input 目录（远端 ComfyUI 场景）则不校验，保持原行为
        changed = False

        def _on_disk(item: dict, key_name: str) -> str:
            rel = str(item.get(key_name) or item.get("fileName") or "") \
                .replace("\\", "/").strip()
            if not rel:
                return ""
            return rel if os.path.isfile(os.path.join(base, rel.replace("/", os.sep))) else ""

        # ---- 公共参考图（global.refs）：缺一张即整体错位，必须显式报警 ----
        _gbl = tl.get("global") or {}
        if common_names is not None:
            keep_common = []
            _missing_common = []
            for r in (_gbl.get("refs") or []):
                if not isinstance(r, dict):
                    keep_common.append(r)
                    continue
                rel = str(r.get("imageFile") or r.get("fileName") or "").replace("\\", "/").strip()
                if not rel:
                    continue
                if _on_disk(r, "imageFile"):
                    keep_common.append(r)
                else:
                    _missing_common.append(rel)
            if _missing_common:
                changed = True
                _gbl["refs"] = keep_common
                tl["global"] = _gbl
                logger.error(
                    "[H3-Director] 公共参考图 %s 尚未就位于 %s，已摘除 —— "
                    "⚠️ 公共块少图会让后续所有 <Picture N> 编号整体错位（提示词与实际图不一致），"
                    "本次产物请人工复核编号，必要时重跑该集", _missing_common, base)

        for i, seg in enumerate(segs_tl):
            keep_refs = []
            for r in (seg.get("refs") or []):
                if not isinstance(r, dict):
                    keep_refs.append(r)
                    continue
                rel = str(r.get("imageFile") or r.get("fileName") or "").replace("\\", "/").strip()
                if not rel:
                    keep_refs.append(r)
                    continue
                if os.path.isfile(os.path.join(base, rel.replace("/", os.sep))):
                    keep_refs.append(r)
                else:
                    changed = True
                    if i < len(seg_ref_names):
                        seg_ref_names[i] = [k for k in seg_ref_names[i]
                                             if k not in (r.get("imageFile"), r.get("fileName"))] or seg_ref_names[i]
                    logger.warning(
                        "[H3-Director] 段%d 参考图 %s 尚未就位于 %s，已摘除（避免误报无参考媒体）",
                        i + 1, rel, base)
            seg["refs"] = keep_refs
            keep_aud = []
            for a in (seg.get("refAudios") or []):
                if not isinstance(a, dict):
                    keep_aud.append(a)
                    continue
                rel = str(a.get("audioFile") or a.get("fileName") or "").replace("\\", "/").strip()
                if not rel:
                    keep_aud.append(a)
                    continue
                if os.path.isfile(os.path.join(base, rel.replace("/", os.sep))):
                    keep_aud.append(a)
                else:
                    changed = True
                    logger.warning(
                        "[H3-Director] 段%d 参考音频 %s 尚未就位于 %s，已摘除", i + 1, rel, base)
            seg["refAudios"] = keep_aud
        if changed:
            director["widgets_values_named"]["timeline_data"] = json.dumps(tl, ensure_ascii=False)

    def _generate_h3_sequence_director(
        self, segs: List[dict], *, tpl_path: str, tpl_name: str,
        filename_prefix: str = "comic_drama/episode",
        emit_audio: bool = None, seed: int = None,
        timeout: int = None, timeout_per_segment: int = 900,
        save_build_to: str = None, build_only: bool = False, size=None,
        continuity: bool = True, continuity_overlap: int = None,
        export_mode: str = None, ref_max_size: int = None,
        seg_audios: Optional[List[List[str]]] = None,
        audio_mode: str = None,
        common_refs: Optional[List[str]] = None,
        common_ref_audios: Optional[List[str]] = None,
        common_prompt: Optional[str] = None) -> dict:
        """Director 路径实现：一条 ``timeline_data`` 承载 N 段，返回结构对齐旧路径。

        与旧连续拼接路径的**语义差异（务必知道）**：

        * 段间衔接由插件原生「段间引导」完成（上一段尾 ``continuityOverlapFrames``
          帧钉进下一段 conditioning 后裁掉前缀），**必须串行**。
        * 参考图是**逐段**的（``segment.refs``）。``prompt_batch`` 时插件强制
          ``editMode=segment``，逐段 refs 才生效（``gen_timeline.py:392/529``）——
          这正是「每个镜头用自己的分镜图」得以成立的地方；``global.refs`` 只作为
          可选公共底图（``commonEnabled=true`` 时才 merge）。
        * 帧数用插件同一套换算（``max(5, round(sec*fps))`` → 17k+5 网格），
          因此 ``timeline.totalFrames`` 诚实等于各段实际帧数之和。

        ⭐ ``common_refs``（2026-09-30 用户拍板）：**公共参考图**本地路径列表（有序）。
        这些图**先于段级图上传**，成功者写进 ``timeline.global.refs``（index
        ``0..K-1``）+ ``commonEnabled=true``；各段自带的 refs 由 builder 从 index
        ``K`` 起编号 —— 与插件前端 ``batch.r2v.slotContinueHint``（「公共参数已占用
        图片1–K；本组从图片 K+1 继续」）同一套约定。调用方（``app.py``）必须先按
        同一顺序把公共项排进每段提示词的 ``<Picture 1..K>``（见
        ``_h3_picture_defs``）。

        ⚠️ 上传失败时的**退化路径**：公共图少上传成功一张，K 就会小于提示词里声明的
        公共编号数，后面所有 ``<Picture N>`` 整体错位（**静默错图**，最坏那类）。
        故此处一旦发现 ``K_eff != len(common_refs)``，就把公共图**内联**进每段 refs
        最前（index 从 0 起）、并关掉 ``commonEnabled``：段内顺序
        「公共 → 分镜 → 私有」与提示词编号**逐位一致**，只是不走 global 通道，
        编号语义零变化；同时 warning 说明已退化。宁可少一次「显式声明」，
        不可错位。
        """
        n = len(segs)
        emit_audio = H3_EMIT_AUDIO if emit_audio is None else bool(emit_audio)
        fps_env = str(os.environ.get("MJSCXT_H3_FPS") or "").strip()
        try:
            fps = float(fps_env) if fps_env else float(h3_director_builder.FPS_DEFAULT)
        except (TypeError, ValueError):
            fps = float(h3_director_builder.FPS_DEFAULT)
        if fps <= 0:
            fps = float(h3_director_builder.FPS_DEFAULT)

        # ---------------- 公共参考图：先上传（H3 Director 公共参数，2026-09-30） ----------------
        # ⚠️ 必须先于段级图：公共项的 index 是 0..K-1，段级项的 index 由 K 起 ——
        #    只有先把 K 落实，段级编号才能算准（见 builder.build 的 seg_index_base）。
        #    同一个本地路径跨镜共用很常见，故与段级共用一个 up_cache（跨段不重复上传）。
        up_cache: Dict[str, Optional[str]] = {}
        common_want: List[str] = []
        for _cp in (common_refs or []):
            _local = self.resolve_local_path(_cp)
            if not _local or not os.path.exists(_local):
                if _cp:
                    logger.warning("[H3-Director] 公共参考图不可用，已跳过: %s", _cp)
                continue
            _dkey = os.path.normcase(os.path.normpath(os.path.abspath(_local)))
            if _dkey in common_want:
                continue
            common_want.append(_dkey)
        common_names: List[str] = []
        for _key in common_want:
            _val = self._upload_h3_director_ref(_key, up_cache)
            if _val:
                common_names.append(_val)
        # K_eff < 声明数 → 公共块会整体少一位，后面所有 <Picture N> 静默错位。
        # 退化为「内联公共图」：公共图搬进每段 refs 最前、index 从 0 起，编号语义不变。
        inline_common = bool(common_want) and len(common_names) != len(common_want)
        if common_want:
            logger.info(
                "[H3-Director] 公共参考图 %d 张（声明 %d）→ %s",
                len(common_names), len(common_want),
                "已内联进各段 refs（上传不全，退化为逐段携带）" if inline_common
                else "走 global.refs + commonEnabled")

        # ---------------- 公共参考音频：解析 + 上传（取代逐段配音，2026-10-02） ----------------
        # 公共音色写进 global.refAudios（index 0..M-1），commonEnabled 时逐段共用同一套
        # 参考音色驱动口型/节奏。取代逐段 QwenTTS：当 common_ref_audios 生效时，段级
        # refAudios 不再需要（build() 侧仍保留段级管线，但 app.py 组装时传空段级配音）。
        common_audio_names: List[str] = []
        if common_ref_audios:
            _ca_cache: Dict[str, Optional[str]] = {}
            _ca_want: List[str] = []
            for _ca in common_ref_audios:
                _cal = self.resolve_local_path(_ca)
                if not _cal or not os.path.exists(_cal):
                    if _ca:
                        logger.warning("[H3-Director] 公共参考音频不可用，已跳过: %s", _ca)
                    continue
                _ckey = os.path.normcase(os.path.normpath(os.path.abspath(_cal)))
                if _ckey not in _ca_want:
                    _ca_want.append(_ckey)
            for _ck in _ca_want:
                _cv = self._upload_h3_director_audio(_ck, _ca_cache)
                if _cv:
                    common_audio_names.append(_cv)
            logger.info(
                "[H3-Director] 公共参考音频 %d 张（声明 %d）→ global.refAudios（index 0..M-1）",
                len(common_audio_names), len(common_ref_audios))

        # ---------------- 参考图：逐段解析 + 上传 ----------------
        # 同一个本地路径（跨镜共用同一张角色锚点图很常见）只上传一次。
        seg_ref_names: List[List[str]] = []
        empty_idx: List[int] = []
        for i, seg in enumerate(segs):
            names: List[str] = []
            seen: set = set()
            for img in (seg.get("reference_images") or []):
                local = self.resolve_local_path(img)
                if not local or not os.path.exists(local):
                    if img:
                        logger.warning(f"[H3-Director] 段{i + 1} 参考图不可用，已跳过: {img}")
                    continue
                dkey = os.path.normcase(os.path.normpath(os.path.abspath(local)))
                if dkey in seen:      # 同段内重复引用同一张图只留一份
                    continue
                seen.add(dkey)
                val = self._upload_h3_director_ref(local, up_cache)
                if val:
                    names.append(val)
            if not names:
                empty_idx.append(i)
            seg_ref_names.append(names)

        # S12 同源红线：整次提交一张参考图都没有 → 必然退化成纯文本生成（外观不可控），
        # 或被迫沿用模板示例图（人物污染）。两种结果都不可接受，直接拒绝提交。
        if not any(seg_ref_names):
            raise RuntimeError(
                f"H3(Director) 整次提交（{n} 段）没有任何可用参考图，已拒绝提交"
                f"（S12：无参考图=角色外观不可控，模板示例图会污染角色）。"
                f"请补齐 reference_images 后重试。"
            )
        for i in empty_idx:
            logger.warning(
                "[H3-Director] 段%d(%s) 无可用参考图 → 该段退化为纯文本生成"
                "（段间引导仍会钉住上一段尾帧，非首段影响有限）",
                i + 1, segs[i].get("name") or f"seg{i + 1}")

        # ---------------- 参考音频：逐段上传（audioMode=source 时驱动口型/节奏） ----------------
        # 音频来源优先级：seg["dub_audios"]（段自带配音路径）> seg_audios 参数（逐段列表）。
        # 同 seg_refs 一样带路径哈希前缀，跨镜共用同一句配音时不重复上传。
        audio_cache: Dict[str, Optional[str]] = {}
        seg_audio_names: List[List[str]] = []
        for i in range(n):
            _src = list((segs[i].get("dub_audios") or []) or [])
            if not _src and seg_audios is not None and i < len(seg_audios):
                _src = list(seg_audios[i] or [])
            names: List[str] = []
            seen_audio: set = set()
            for ap in _src:
                local = self.resolve_local_path(ap)
                if not local or not os.path.exists(local):
                    if ap:
                        logger.warning(f"[H3-Director] 段{i + 1} 参考音频不可用，已跳过: {ap}")
                    continue
                akey = os.path.normcase(os.path.normpath(os.path.abspath(local)))
                if akey in seen_audio:
                    continue
                seen_audio.add(akey)
                val = self._upload_h3_director_audio(local, audio_cache)
                if val:
                    names.append(val)
            seg_audio_names.append(names)

        # ---------------- 尺寸 / 连续引导 / 导出 ----------------
        w = h = None
        if size:
            try:
                w, h = int(size[0]), int(size[1])
            except (TypeError, ValueError, IndexError):
                logger.warning(f"[H3-Director] 画幅入参非法，改用模板值: {size!r}")
                w = h = None
        if continuity_overlap is None:
            ov_env = str(os.environ.get("MJSCXT_H3_CONTINUITY_OVERLAP") or "").strip()
            continuity_overlap = (int(ov_env) if ov_env.isdigit()
                                  else h3_director_builder.CONTINUITY_OVERLAP_DEFAULT)
        export_mode = (export_mode or os.environ.get("MJSCXT_H3_EXPORT_MODE")
                       or "all").strip().lower()
        if export_mode not in ("all", "segments"):
            logger.warning(f"[H3-Director] 未知 exportMode={export_mode}，回落 all")
            export_mode = "all"

        builder = h3_director_builder.H3DirectorBuilder(tpl_path)
        # ---- 用户手选模型覆盖（见 app/comfyui_models.py）----
        #    模板里写死的模型文件名会因磁盘目录布局变化而失效：模型被放进
        #    diffusion_models/minimax-h3/ 后，ComfyUI 报出的合法值会带 `minimax-h3\`
        #    前缀，裸文件名不再合法 → 节点校验失败 → H3 产出被整段静默丢弃。
        #    这里优先套用用户在前端从 object_info 扫到并手选的合法值。
        #    ⚠️ 必须 fail-safe：任何异常都只告警并沿用模板原值，绝不中断主流程。
        try:
            # ⚠️ 按**实际生效的模板文件名**查槽位映射：SLOTS.targets 按模板文件名索引
            #    （单采 12 节点 / 二采 23 节点节点 id 不同）。传 "h3_video" 这个 key
            #    只是向后兼容写法；这里直接用 resolve 后的文件名更不易错。
            _model_ov = comfyui_models.overrides_for_template(
                os.path.basename(tpl_path))
            if _model_ov:
                _applied = builder.apply_model_overrides(_model_ov)
                if _applied:
                    logger.info(
                        "[H3-Director] 应用手选模型 %s",
                        "; ".join(f"#{a['node']}.{a['field']}: {a['from']} -> {a['to']}"
                                  for a in _applied))
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[H3-Director] 手选模型覆盖失败，沿用模板原值: {_e}")
        # audio_mode 优先级：显式传入 > 有段级参考音频（seg_audios 非空）→ "source" >
        #   emit_audio → "generate" / "mute"。
        #   "source" = H3 用 refAudios 里的参考音频驱动口型/节奏（逐镜 QwenTTS 配音）。
        _eff_audio_mode = audio_mode
        if _eff_audio_mode is None:
            if common_audio_names:
                # 公共参考音色取代逐段 QwenTTS 配音（2026-10-02）：用 **generate** 模式
                # —— 公共音色（global.refAudios）作条件**锁定角色音色**，语音按各段
                # 台词自生成。⚠️ 不用 source：source＝参考音频原样 mux，会把同一句
                # 参考音频文本灌进每一段（台词全错配，docs/r2v-source-audio.md 明确
                # source 要求「台词须与音频一致」）。generate = 音色统一 + 台词正确。
                _eff_audio_mode = "generate"
            elif any(seg_audio_names):
                _eff_audio_mode = "source"
            else:
                _eff_audio_mode = "generate" if emit_audio else "mute"
        # ⭐ 公共参考图走 global.refs（commonEnabled=true）时，段级 refs 的 index 必须
        #    从 K 起（K = 公共张数）—— 否则 merge_indexed_refs 会按同 index 逐槽覆盖，
        #    公共图一张不生效且**不报任何错**（见 h3_director_builder.build 的注释）。
        #    退化路径（上传不全）：公共图内联进每段最前，index 从 0 起、不开 commonEnabled。
        _use_global_common = bool(common_names) and not inline_common
        _seg_refs_for_build = (
            [list(common_names) + list(x or []) for x in seg_ref_names]
            if inline_common else seg_ref_names)
        wf, layout = builder.build(
            segs, seg_refs=_seg_refs_for_build, seg_audios=seg_audio_names,
            common_ref_audios=common_audio_names,
            common_prompt=common_prompt,
            refs=list(common_names) if _use_global_common else (),
            common_enabled=_use_global_common,
            width=w, height=h, frame_rate=fps, seed=seed,
            filename_prefix=filename_prefix,
            continuity=bool(continuity), continuity_overlap=continuity_overlap,
            ref_max_size=ref_max_size,
            # emit_audio=False → mute：插件直接跳过音频 VAE 解码（8GB 显存下省一趟解码），
            # 仍返回**静音** AUDIO 对象（插件 executor 明说 "silent AUDIO output"），
            # 所以 CreateVideo 的 audio 输入不会悬空。
            # "source" = 用段级 refAudios 参考音频（见上）。
            audio_mode=_eff_audio_mode,
            export_mode=export_mode,
            # 二采开关（2026-09-28 用户定档默认关）：关 = 裁二采专属模型链 +
            # 断 Director.refine 输入，Director.images 落一采帧；画质由 FlashVSR 超分补。
            enable_refine=H3_ENABLE_REFINE)

        # 提交前校验参考图/音频是否已落到 ComfyUI input/：消除「图未落地首采误报
        # no reference media」的竞态（详见 _drop_missing_director_refs）。正常全就位时 no-op。
        # ⚠️ 公共参考图也一起校验：global.refs 缺一张同样会让后续 <Picture N> 错位。
        self._drop_missing_director_refs(wf, seg_ref_names, seg_audio_names, n,
                                         common_names=(common_names
                                                       if _use_global_common else None))

        if build_only:
            # 只构建不提交（Director 路径，2026-10-03）：UI 工作流**随结果内存返回**，
            # 由调用方落盘（app.py 既有原子写工具）；本层不写文件、不 to_api、
            # 不传参考图、不提交 GPU。layout 摘要随返回值带回便于核对段数/帧数。
            logger.info("[H3-Director][build_only] 工作流已构建（不落盘不提交）：%d 段 / "
                        "%s 帧 / UI 节点 %s 连线 %s", n, layout.get("total_frames"),
                        layout.get("node_total"), layout.get("link_total"))
            return {"build_only": True, "files": [], "saved": "", "workflow": wf,
                    "layout": {"node_total": layout.get("node_total"),
                               "link_total": layout.get("link_total"),
                               "total_frames": layout.get("total_frames"),
                               "duration_sec": layout.get("duration_sec"),
                               "segments": len(segs),
                               "common_refs": len(common_names or [])}}

        if save_build_to:
            os.makedirs(os.path.dirname(save_build_to), exist_ok=True)
            with open(save_build_to, "w", encoding="utf-8") as f:
                json.dump(wf, f, ensure_ascii=False)
            layout["build_path"] = save_build_to
        layout["common_refs"] = list(common_names)
        layout["common_ref_audios"] = list(common_audio_names)
        layout["audio_mode"] = _eff_audio_mode
        layout["common_inline"] = bool(inline_common)
        logger.info(
            f"H3(Director) 工作流已就绪：{n} 段 / {layout['total_frames']} 帧"
            f"（{layout['duration_sec']}s）/ 二采 {'开' if layout.get('refine_node') else '关'} / "
            f"公共参考图 {len(common_names)} 张"
            f"{'（内联退化）' if inline_common else ''} / "
            f"节点 {layout['node_total']} 连线 {layout['link_total']}")

        # ---------------- 落 API 并自检 ----------------
        api_prompt, meta = self.to_api(wf, return_meta=True)

        # 音频策略：与旧路径同一套「断开末端封装节点的音频输入」实现。
        # Director 的成片链路是 CreateVideo ← MiniMaxH3Director.audio，同属 mux 类型集合，
        # 因此这一个函数两条路径通用（mute 已让插件产出静音轨，这里再断开是双保险）。
        # ⚠️ audio_mode=source 时**不**断开：参考音频要进 H3 驱动口型，断开会丢音轨。
        audio_changed: List[str] = []
        if _eff_audio_mode != "source" and not emit_audio:
            audio_changed = self.strip_h3_audio_inputs(api_prompt)
            if audio_changed:
                logger.info(f"H3(Director) 音轨已断开（生成阶段不出声）: {audio_changed}")
            else:
                logger.warning("H3(Director) 音轨断开未命中任何节点（成片可能仍带音轨），"
                               "请检查工作流是否变更")

        report = self.validate_api_prompt(api_prompt)
        if report["unknown_types"] or report["missing_required"] or report["dangling_links"]:
            logger.warning(f"H3(Director) 提交前自检异常: "
                           f"{json.dumps(report, ensure_ascii=False)[:400]}")
        if report.get("unexpected_inputs"):
            # 伪控件没剥干净时会落到这里（含 control_after_generate / minimax_director_ui）
            logger.warning(f"H3(Director) 存在未声明输入（可能含前端伪控件）: "
                           f"{report['unexpected_inputs'][:8]}")

        seg_report: List[dict] = []
        for i, info in enumerate(layout.get("segments") or []):
            seg_report.append({
                **info,
                "inst": info.get("index"),
                "duration": info.get("duration_sec"),
                "prompt_head": (segs[i].get("prompt") or "")[:60],
                "refs": [{"slot": f"ref_image_{j}", "src": src, "value": name}
                         for j, (src, name) in enumerate(
                             zip(segs[i].get("reference_images") or [], seg_ref_names[i]))],
                "ref_count_used": len(seg_ref_names[i]),
                "audios": seg_audio_names[i],
                "audio_count_used": len(seg_audio_names[i]),
            })

        timeout = timeout or int(1200 + timeout_per_segment * n)
        logger.info(f"H3(Director) 提交：{n} 段，总超时 {timeout}s（单段预估 {timeout_per_segment}s），"
                    f"段间引导 {'开' if layout.get('continuity') else '关'}"
                    f"（{layout.get('continuity_overlap_frames')} 帧）")
        # DLSS 补帧旁路（2026-10-01）：NvidiaDLSSFrameInterpolation 的 temp 工作目录
        # 被外部周期性删除 → mkdtemp WinError 3 → 收尾文件不存在 → 整集成片丢失。
        # 本系统 FlashVSR 超分已独立承担画质，DLSS 插帧属可选增强，跳过不失衡。
        if H3_DISABLE_DLSS:
            dlss_bypassed = self.bypass_dlss_node(api_prompt)
            if dlss_bypassed:
                logger.info(f"H3(Director) DLSS 补帧已旁路: {dlss_bypassed}")
        # 崩溃免重渲（2026-09-29）：整集一次提交要跑几十分钟，崩溃/重启后先查台账与
        # 远端 history —— 能复用就复用、还在跑就重连，绝不重复提交白烧 GPU。
        history, prompt_id, resumed = self.submit_resumable(
            api_prompt, job_key=f"h3|{filename_prefix}",
            timeout=timeout, file_ext=".mp4", label=f"H3(Director) {n} 段")
        if resumed:
            logger.info("H3(Director) 本次为**免重渲复用**（未消耗 GPU）")
        files = self.get_output_files(history, ".mp4")

        audio_check = []
        if files:
            from media_probe import probe_media  # 2026-10-08 解耦：探测走叶子模块，不再依赖后期模块
            for f in files:
                m = probe_media(f)
                audio_check.append({"file": f, "has_audio": m.get("has_audio"),
                                    "audio_streams": m.get("audio_streams"),
                                    "video_streams": m.get("video_streams"),
                                    "error": m.get("error")})

        return {"prompt_id": prompt_id, "files": files, "history": history,
                "segment_count": n, "segments": seg_report, "layout": layout,
                "meta": meta, "seed": seed, "emit_audio": bool(emit_audio),
                "audio_disconnected": audio_changed, "audio_check": audio_check,
                "timeout": timeout, "template": tpl_name,
                "validate_report": report,
                # ---- Director 专属字段（报表 / 守卫用）----
                "builder": "director",
                "timeline_total_frames": layout.get("total_frames"),
                "timeline_duration_sec": layout.get("duration_sec"),
                "continuity": layout.get("continuity"),
                "continuity_overlap_frames": layout.get("continuity_overlap_frames"),
                "export_mode": layout.get("export_mode"),
                "audio_mode": layout.get("audio_mode"),
                "refs": layout.get("refs"),
                "segment_refs": layout.get("segment_refs"),
                "segment_audios": layout.get("segment_audios"),
                # ---- 公共参考图（H3 Director 公共参数，2026-09-30）----
                # common_refs = 实际生效的公共图（相对名，index 0..K-1）；
                # common_inline=True 表示上传不全已退化为「逐段内联携带」，编号语义不变。
                "common_refs": layout.get("common_refs") or [],
                "common_inline": bool(layout.get("common_inline")),
                "common_enabled": bool(layout.get("common_enabled"))}

    @staticmethod
    def _h3_picture_defs(char_refs: List[dict], scene_refs: List[dict],
                         storyboard_ref: dict = None, end_frame_ref: dict = None,
                         item_refs: List[dict] = None,
                         common_refs: List[dict] = None):
        """把参考图列表映射成 H3 的 ``(<Picture N>, 用途说明)`` 与 ``<Subject N>`` 定义

        返回 ``(picture_defs, subjects, storyboard_label)``。

        语义约定（顺序即 ``<Picture N>`` 编号）：
            common_refs 非空     → 先排**公共参考图**（H3 Director 公共参数）：``<Picture 1..K>``
            storyboard_ref 非空 → 紧随其后 = 分镜图（构图/景别/机位/人物姿态基准）
                                  其后 = 本镜出场角色三视图（每人一张）+ 物品 + 场景
            否则                 → ``<Picture 1..n>`` = 角色外观锚点，其后为场景环境参考
            end_frame_ref 非空   → 追加 ``<Picture K>`` = 结束帧（尾帧），keyframe 模式用，
                                  让 Ref2VA 在首帧与尾帧之间插值（FL2V 首尾一致的软手段）

        2026-09-27 扩展：``char_refs`` 不再截断到 ``[:2]``，本镜**所有**出场角色
        每人一张三视图独立锚点；``item_refs`` 新增物品锚点（形状/材质/配色）。
        分镜图（仅构图/机位/姿态基准）仍是构图基准，但角色身份/外观改由各自的三视图
        锚点独立锁定（分镜图不再当「唯一外观锚点」）—— 解决「配角外观缺失/串味」与
        「物品走样」的质检重灾区。

        ⭐ 2026-09-30 ``common_refs``（H3 Director 公共参数，用户拍板）：
        全段都在用、且用的是同一张图的资产（角色/物品/场景）。插件在
        ``commonEnabled=true`` 时按槽位 index 把 ``global.refs`` merge 进每一段
        （``director/plan.py:merge_indexed_refs``，同 index 段级优先）：公共项占
        index ``0..K-1``、段级私有项从 index ``K`` 起。提示词侧**必须与槽位同序** ——
        公共项排最前、编号 ``1..K`` 在全集恒定，分镜图与私有项排在后面。
        「同一角色在整集里 Picture 编号不漂移」靠的就是这一处排序。

        ⚠️ 第三个返回值 ``storyboard_label``：分镜图的标签**不再等于** ``<Picture 1>``
        （公共块会占掉前面的编号），调用方必须把真实标签交给 ``h3_prompt_kit``，
        否则「构图/景别以某图为基准」那句会指错图。旧实现靠「``<Picture 1>`` 在不在
        picture_refs 里」硬判 —— 那是一条**隐含同序约定**，公共块一加入就静默失效，
        正是本次把标签显式化的原因。无分镜图时返回空串。

        ⚠️ ``subjects`` 里的 ``picture`` 字段是给 ``h3_prompt_kit`` 用的**归属声明**：
        ``<Subject N> is X in <Picture M>`` 与 retention_analysis 的保留项措辞
        （主体图写 costume/palette、场景图写 scene structure/lighting）都靠它分流。
        历史实现不写该字段 → 场景参考图会被误写成「the costume … follow the reference
        image exactly」（语义错位的假声明，2026-09-24 二次对齐时发现）。
        """
        picture_defs: List[tuple] = []
        subjects: List[Dict[str, str]] = []
        storyboard_label = ""

        def _appearance(ref: dict) -> str:
            return str(ref.get("appearance") or ref.get("description")
                       or ref.get("reference_prompt_zh") or "").strip()[:120]

        def _next_label() -> str:
            return f"<Picture {len(picture_defs) + 1}>"

        def _char_desc(name: str) -> str:
            # 有分镜图时角色是「三视图独立锚点」（2026-09-27 策略）；无分镜图时它同时
            # 承担构图锚点，措辞不同（两句都与历史逐字一致，零回归）。
            if storyboard_ref:
                return (f"{name} 的三视图设定图，定义其面部身份、发型、体型、服装与画风，"
                        f"并作为其出场镜头的身份锚点")
            return (f"{name} 的外观参考，定义其五官、发型、服装与画风，"
                    f"并作为其出场镜头的构图锚点")

        # ---- 0) 公共参考图（H3 Director 公共参数）：必须排在段级图之前 ----
        # 顺序即槽位：global.refs 的 index 0..K-1 → <Picture 1..K>，全段一致。
        for _i, ref in enumerate(common_refs or []):
            ref = ref or {}
            name = str(ref.get("name") or "").strip() or f"公共资产{_i + 1}"
            kind = str(ref.get("kind") or "").strip()
            label = _next_label()
            # ⭐ 2026-10-05 公共参数「相关说明」（H3_COMMON_NOTE_MODE）：把本集专属的
            #    appearance 设定文案并入说明（此前只进 subjects、公共区看不到）；场景
            #    说明改九宫格多视角口径。开关关闭时回落旧固定模板（零回归）。
            _note = H3_COMMON_NOTE_MODE
            _ap = _appearance(ref)
            if kind == "scene":
                if _note:
                    _sc_txt = (f"{name} 的环境参考（9 机位九宫格总览：同一场地的各观察角度"
                               f"都在这张图里，含空间结构、材质氛围与光照基调）")
                else:
                    _sc_txt = f"{name} 的环境参考，定义场景结构、材质氛围与光照基调"
                if _note and _ap:
                    _sc_txt += f"；设定：{_ap}"
                picture_defs.append((label, _sc_txt))
                continue
            if kind == "item":
                _it_txt = f"物品「{name}」的设定图，定义其形状、材质与配色"
                if _note and _ap:
                    _it_txt += f"；设定：{_ap}"
                picture_defs.append((label, _it_txt))
            else:
                # character（kind 缺省也走这里：公共池主体就是角色锚点）
                _ch_txt = _char_desc(name)
                if _note and _ap:
                    _ch_txt += f"；本集设定：{_ap}"
                picture_defs.append((label, _ch_txt))
            subjects.append({"name": name, "appearance": _appearance(ref),
                             "picture": label})

        if storyboard_ref:
            sb_name = storyboard_ref.get("name") or "本镜头分镜图"
            # 分镜图 = 构图/机位/姿态基准（角色外观由各自三视图锚点锁定）。
            storyboard_label = _next_label()
            picture_defs.append((
                storyboard_label,
                f"该镜头的分镜图（{sb_name}），定义本镜的构图、景别、机位、环境与人物姿态基准"))
            # 本镜出场角色：每人一张三视图锚点（不再截断 [:2]），Subject 归属各自 <Picture M>。
            for ref in (char_refs or []):
                name = ref.get("name", f"角色{len(subjects) + 1}")
                label = _next_label()
                picture_defs.append((label, _char_desc(name)))
                subjects.append({"name": name, "appearance": _appearance(ref),
                                 "picture": label})
            # 本镜物品：形状/材质/配色锚点（Subject 归属，走「材质/配色」保留语义）。
            for ref in (item_refs or []):
                name = ref.get("name", f"物品{len(subjects) + 1}")
                label = _next_label()
                picture_defs.append((
                    label,
                    f"物品「{name}」的设定图，定义其形状、材质与配色"))
                subjects.append({"name": name, "appearance": _appearance(ref),
                                 "picture": label})
            # 场景：环境/氛围锚点（放在主体之后）。
            for ref in (scene_refs or []):
                name = ref.get("name", f"场景{len(picture_defs) + 1}")
                picture_defs.append((
                    _next_label(),
                    f"{name} 的环境参考，定义场景结构、材质氛围与光照基调"))
            if end_frame_ref:
                # ⭐ 尾帧参考图（keyframe 模式）：追加 <Picture K> = 结束帧，
                # 用于 Ref2VA 在首帧与尾帧之间插值（FL2V 首尾一致的软手段，2026-09-26）。
                picture_defs.append((
                    _next_label(),
                    end_frame_ref.get("desc") or
                    "该镜头的尾帧（结束画面），定义本镜结束时的构图、人物姿态与表情，"
                    "最后一帧必须落在本图上"))
            return picture_defs, subjects, storyboard_label

        for ref in (char_refs or []):
            name = ref.get("name", f"角色{len(subjects) + 1}")
            label = _next_label()
            picture_defs.append((label, _char_desc(name)))
            subjects.append({"name": name, "appearance": _appearance(ref),
                             "picture": label})
        for ref in (item_refs or []):
            name = ref.get("name", f"物品{len(subjects) + 1}")
            label = _next_label()
            picture_defs.append((
                label,
                f"物品「{name}」的设定图，定义其形状、材质与配色"))
            subjects.append({"name": name, "appearance": _appearance(ref),
                             "picture": label})
        for ref in (scene_refs or [])[:1]:
            name = ref.get("name", f"场景{len(picture_defs) + 1}")
            picture_defs.append((
                _next_label(),
                f"{name} 的环境参考，定义场景结构、材质氛围与光照基调"))
        if end_frame_ref:
            picture_defs.append((
                _next_label(),
                end_frame_ref.get("desc") or
                "该镜头的尾帧（结束画面），定义本镜结束时的构图、人物姿态与表情，"
                "最后一帧必须落在本图上"))
        return picture_defs, subjects, storyboard_label


    def resolve_h3_prompt(self, shot: dict, char_refs: List[dict],
                          scene_refs: List[dict], storyboard_ref: dict = None,
                          end_frame_ref: dict = None,
                          item_refs: List[dict] = None,
                          common_refs: List[dict] = None,
                          audio_defs: List[dict] = None) -> str:
        """生成期**权威**的 H3 提示词入口（修「薄英文顶掉结构化构建器」）

        择优规则：
        - 剧本里已有 ``prompt_h3`` 且通过 :func:`h3_prompt_kit.validate`
          （六段/三段齐全）→ 直接采用（LLM 写的散文通常更生动）
        - 不合规（历史裸英文句、缺段）→ 用规范构建器重建，并把旧文本并入
          ``detailed_description`` 作补充细节，信息不丢

        为什么不能让旧的 ``prompt_h3`` 直接生效：H3 走 Ref2VA，提示词必须带
        ``<Picture N>`` 标签告诉模型每张参考图的用途；而剧本阶段的 LLM 根本
        不知道最终配了几张图，只能写出一句无标签的裸英文 —— 实测全项目 200+
        镜头的结构化提示词数量为 0，出片与设定严重不符。

        end_frame_ref：可选，尾帧参考图信息（keyframe 模式），含 ``desc`` 用途说明；
        传入时把尾帧声明为 <Picture K> 并在提示词末拍锚定结束帧（FL2V 首尾一致）。
        item_refs：可选，本镜物品参考图（形状/材质/配色锚点，2026-09-27 扩展）。
        common_refs：可选，**公共参考图**（H3 Director 公共参数，2026-09-30）：
        全段都在用、且同一张图的资产，排在 ``<Picture 1..K>``；分镜图与私有项顺延。
        见 :meth:`_h3_picture_defs`。
        """
        shot = shot or {}
        picture_defs, subjects, sb_label = self._h3_picture_defs(
            char_refs, scene_refs, storyboard_ref, end_frame_ref, item_refs,
            common_refs)
        style = h3_prompt_kit.style_of(shot)
        # 尾帧标签：end_frame_ref 存在时，它是 picture_defs 里最后一张图
        end_label = ""
        if end_frame_ref and picture_defs:
            end_label = picture_defs[-1][0]
        if not picture_defs:
            built = h3_prompt_kit.build_base(shot, "T2VA", style=style)
            existing = str(shot.get("prompt_h3") or "").strip()
            if not existing:
                # A-5：无参考图分支不经过 resolve()，必须自己过一道长度闸门
                #（build_base 在超长 description 下同样可能越界）
                return h3_prompt_kit.clamp_h3_prompt(built)
            verdict = h3_prompt_kit.validate(existing)
            # A-5：这条路径**完全绕过 resolve()**（既有 prompt_h3 直接生效），
            # 能把裸 >6000 字符提示词原样送进 H3 —— 必须显式截断。
            return h3_prompt_kit.clamp_h3_prompt(
                existing if verdict["valid"] else h3_prompt_kit.merge_detail(built, existing))
        return h3_prompt_kit.clamp_h3_prompt(
            h3_prompt_kit.resolve(shot, picture_defs, subjects, style=style,
                                  end_frame_ref=end_label,
                                  storyboard_ref_label=sb_label,
                                  audio_defs=list(audio_defs or ())))

    def _build_h3_prompt(self, shot: dict, char_refs: List[dict], scene_refs: List[dict],
                         storyboard_ref: dict = None, end_frame_ref: dict = None,
                         item_refs: List[dict] = None,
                         common_refs: List[dict] = None,
                         audio_defs: List[dict] = None) -> str:
        """构建规范 H3 Ref2VA 提示词（无条件重建，忽略剧本里的既有 prompt_h3）

        需要一个「干净重建」的调用点时用它（例如风格纠偏重试）；日常生成请用
        :meth:`resolve_h3_prompt`，后者会优先尊重已合规的既有提示词。

        end_frame_ref：可选，尾帧参考图信息（keyframe 模式），含 ``desc`` 用途说明。
        item_refs：可选，本镜物品参考图（形状/材质/配色锚点，2026-09-27 扩展）。
        common_refs：可选，公共参考图（H3 Director 公共参数，2026-09-30），
        排在 ``<Picture 1..K>``，全段同序同编号；见 :meth:`_h3_picture_defs`。
        """
        shot = shot or {}
        picture_defs, subjects, sb_label = self._h3_picture_defs(
            char_refs, scene_refs, storyboard_ref, end_frame_ref, item_refs,
            common_refs)
        style = h3_prompt_kit.style_of(shot)
        end_label = ""
        if end_frame_ref and picture_defs:
            end_label = picture_defs[-1][0]
        if not picture_defs:
            # A-5 加固：本方法同样不经过 resolve()，且被 app.py 4 处直接调用
            #（2057/2068/3715/3733），同一类"超长提示词被服务端静默截断"的口子。
            return h3_prompt_kit.clamp_h3_prompt(
                h3_prompt_kit.build_base(shot, "T2VA", style=style))
        # ⭐ 2026-10-09：把「本镜道具参考图的名称」透传给构建器 ——
        #    retention_analysis 需要据此把**道具**从角色/服装语义里分离出来
        #    （否则道具会被要求保留「脸型/眉形/鼻形」，而真正的「形态与朝向」无人声明，
        #     实跑后果：旧哨子被 H3 画反）。
        _item_labels = [str(r.get("name")) for r in (item_refs or []) if r.get("name")]
        return h3_prompt_kit.clamp_h3_prompt(
            h3_prompt_kit.build_ref2va(shot, picture_defs, subjects, style=style,
                                       end_frame_ref=end_label,
                                       storyboard_ref_label=sb_label,
                                       audio_defs=list(audio_defs or ()),
                                       item_labels=_item_labels))


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

