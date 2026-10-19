# -*- coding: utf-8 -*-
"""comfyui_client 的**模块级工具**（D-3 分层下沉，2026-10-16）。

从 `app/comfyui_client.py` 原样搬来（不做任何逻辑改动），含四类：
  · 提示词槽位极性 / 增强器节点（PROMPT_NODE_TYPES、_iter_prompt_slots …）
  · 参考图标签 → 官方句式 / 构图基准段 / 场景视角后缀（_ref_label_body、scene_view_prompt_suffix …）
  · RefMod 节点探测（probe_refmod_nodes）
  · H3 超时/耗时标定 + 缓存名 + 群像描述（_env_int、_h3_timeout_seconds、h3_cache_name …）

为什么单独成模块：comfyui_client 要拆成 mixin（提交/轮询/解析/提示词），mixin 模块
需要一个**叶子**依赖来源 —— 否则 mixin import comfyui_client、comfyui_client 又 import mixin 成环。
comfyui_client 再把本模块的名字**全量再导出**，故对外 `comfyui_client.X` 逐字不变。
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
from typing import Any, Dict, List, Optional, Tuple, Sequence

from config import (
    MULTIVIEW_CONFIG, SCENE_VIEW_ANGLE_ZH, SCENE_VIEW_KEYS, SCENE_VIEW_LABELS,
    SHOT_TYPES, CONFLICT_NEGATIVE_TOKENS, ENABLE_BLOCKING_ANNOTATION,
    PROJECT_OUTPUT_DIR, COMFYUI_WORKFLOWS_DIR, COMFYUI_URL,
)
from dialogue_utils import (dialogue_text as _dlg_text, format_line as _dlg_line,
                            dialogue_speaker as _dlg_speaker)
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

#: 标签里「参考图N（<imageN>）」前缀的匹配（兼容生成端三种写法）
#: ⚠️ 2026-10-12（R6 修复）：尖括号**可选** —— 文字道具特写标签是
#:    「参考图1（image1）是本镜画面主体的道具「X」的设定图」（qc_helpers 的 _hero_item
#:    分支，见 _allocate_storyboard_refs）。旧正则吃不到它，正文会残留
#:    「（image1）是…」脏前缀，职责短语转写因此带上中文脏串。
_REF_LABEL_PREFIX_RE = re.compile(
    r"^参考图\s*\d+\s*[（(]\s*<?\s*image\s*\d+\s*>?\s*[)）]\s*")
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
    # ⭐⭐ 2026-10-12（R6 修复，核心）：连接词「是 / 为」必须一并剥掉。
    #   生成端角色/场景槽位是「参考图N（<imageN>）**是**角色「X」的身份锚点（…视图）：…」
    #   （qc_helpers._allocate_storyboard_refs 的主/次角色与场景两类写法），
    #   前缀正则只吃到「）」，剩下的正文**以连接词开头**；而下游 _GRID_CHAR_LABEL_RE
    #   是 "^角色「…" 的**锚定**匹配（当年按无连接词的旧标签写死）→ 正文
    #   「是角色「X」的身份锚点（…）」一条都匹配不上 → 九宫格逐格版的「角色设定」段
    #   **整段**回落到兜底句「…（本镜未注入角色身份参考图）…」= 有图无声明
    #   （生产实测 45/46 镜，R6）。归一化后正文恒以职责主体（角色/场景/物品）开头，
    #   中文逐格版与英文官方句式（build_storyboard_prompt）共用同一份干净正文。
    text = re.sub(r"^[是为]\s*", "", text).strip()
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


def identity_baseline_grid_section(pos, cols: int = 3, rows: int = 3, cells=None) -> str:
    """身份基准网格提示词段（2026-10-13：支持动态布局）。

    九宫格输出沿用 3×3（与输出同构 → 返回原常量、逐字不变，避免提示词指纹漂移）；
    收尾镜单帧输出按实际网格形状给出 1×3 / 2×2 / 2×3 / 3×3 等描述 —— 单帧不需要
    九格同构，网格只承担「外观基准」职责（2026-10-13 用户口径）。
    """
    try:
        cols = max(1, int(cols))
        rows = max(1, int(rows))
    except (TypeError, ValueError):
        cols, rows = 3, 3
    if (cols, rows) == (3, 3):
        return IDENTITY_BASELINE_GRID_SECTION.format(pos=pos)
    try:
        n = int(cells) if cells else cols * rows
    except (TypeError, ValueError):
        n = cols * rows
    return (
        f"IDENTITY BASELINE GRID (critical): <image{pos}> is a {cols}x{rows} grid "
        f"of {n} cells showing the canonical appearance (facial identity, hairstyle, "
        f"outfit and colour palette) of the character(s) of this shot, taken from "
        f"their character baseline assets. It is the appearance ground truth: every "
        f"character rendered in the output must replicate EXACTLY the face, hairstyle "
        f"and outfit shown in the cell(s) of that same character in <image{pos}>. "
        f"Rows group one character's views together: all cells in the same row show "
        f"the SAME character (different views / outfits of that character), so match "
        f"each output character against its own row only. "
        f"Do not add, remove or recolor any clothing element relative to the "
        f"baseline grid.")


#: 从身份基准网格 label 解析动态布局（2026-10-13）：形状串形如「1×3 共 3 格」。
_IDENTITY_GRID_SHAPE_RE = re.compile(r"(\d+)\s*[x×X]\s*(\d+)\s*共\s*(\d+)\s*格")
_IDENTITY_GRID_SHAPE_LOOSE_RE = re.compile(r"(\d+)\s*[x×X]\s*(\d+)")


def _parse_identity_grid_layout(label):
    """从网格参考图 label 解析 (cols, rows, cells)；解析不到返回 None（调用方按 3×3）。"""
    try:
        # ⚠️ label 按用户语言书写「行×列」（1×3 = 1 行 3 列），本函数对外返回
        #    (cols, rows, cells)（与 _identity_grid_layout / 构建函数同口径）→ 此处交换。
        m = _IDENTITY_GRID_SHAPE_RE.search(str(label or ""))
        if m:
            r, c = int(m.group(1)), int(m.group(2))
            return (c, r, int(m.group(3)))
        m = _IDENTITY_GRID_SHAPE_LOOSE_RE.search(str(label or ""))
        if m:
            r, c = int(m.group(1)), int(m.group(2))
            return (c, r, c * r)
    except Exception:  # noqa: BLE001
        pass
    return None


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


#: TeaCache 节点的类名匹配（B-7，2026-10-16）。
_TEACACHE_NODE_RE = re.compile(r"(?i)teacache")


def probe_teacache_node(timeout: int = 5) -> dict:
    """GET {COMFYUI_URL}/object_info 探测 **TeaCache** 节点是否可用（B-7，2026-10-16）。

    TeaCache 是**训练无关的时间步缓存**提速节点（约 1.5–2×）。本项目出图/出视频走
    H3 Director 模板 —— 若 ComfyUI 侧装了 TeaCache 节点，可评估把它插进采样链
    （见 `docs/B-7_FP8_TeaCache_PoC_20261016.md`）。本探针把「确认 ComfyUI 侧是否
    支持」从人工翻目录变成一次自动检查（与 :func:`probe_refmod_nodes` 同款）。

    返回 ``{"reachable": bool, "available": bool, "classes": [str], "error": str?}`` ——
    fail-open：连不上 / 异常一律 ``available=False``，绝不抛错。
    """
    result: dict = {"reachable": False, "available": False, "classes": []}
    try:
        resp = requests.get(f"{COMFYUI_URL}/object_info", timeout=(min(3, timeout), timeout))
        resp.raise_for_status()
        info = resp.json()
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    try:
        if isinstance(info, dict):
            result["classes"] = sorted(str(c) for c in info if _TEACACHE_NODE_RE.search(str(c)))
            result["reachable"] = True
            result["available"] = bool(result["classes"])
    except Exception as e:  # noqa: BLE001
        result["error"] = f"解析 object_info 失败: {type(e).__name__}: {e}"
    return result


# ===================================================================== #
# t18：H3 总超时可配置 + 实测标定（直接修「成片永远跑不出」的根因）
# ===================================================================== #
# 背景（2026-10-12 生产实测）：单段 H3 ≈ 17 分钟（1020s+），而旧公式
#     timeout = 1200 + timeout_per_segment(900) × 段数
# 对 10 段给出 10200s —— 与「10 段 × 17 分钟 ≈ 10200s」**恰好重合**：
# 每次都在最后一刻判超时 → interrupt + 重投（段缓存虽复用，但最后一段白跑、
# 还要重新排队），用户看到的现象就是「永远出不来 mp4」。
#
# 现口径：timeout = base + 单段实测耗时 × 段数 × 安全系数，全部可 env 覆盖：
#   MJSCXT_H3_SEGMENT_SECONDS   单段实测耗时（秒；默认读标定文件，兜底 1020 = 实测 17min）
#   MJSCXT_H3_TIMEOUT_BASE      固定基数（秒，默认 1800：排队/加载模型/导出开销）
#   MJSCXT_H3_TIMEOUT_MULT      安全系数（默认 1.5）
#   MJSCXT_H3_TIMEOUT_MIN       下限（秒，默认 3600）
#   MJSCXT_H3_TIMEOUT_CAP       上限（秒，默认 0 = 不设上限）
#   MJSCXT_WAIT_EXTENSIONS      软超时续等次数（默认 3；见 wait_for_completion）
#   MJSCXT_WAIT_EXTEND_SECONDS  每次续等时长（秒，默认 1800）
# 例：10 段 → 1800 + 1020×10×1.5 = 17100s（≈4.75h），相对旧值 10200s 富余 ≈ 68%。
_H3_SEG_COST_DEFAULT_SEC = 1020.0
_H3_TIMING_FILE = os.path.join(PROJECT_OUTPUT_DIR, "_h3_timing.json")


def _env_int(name: str, default: int) -> int:
    """整数环境变量（非法/空 → default，绝不抛）。"""
    try:
        raw = str(os.environ.get(name) or "").strip()
        return int(float(raw)) if raw else int(default)
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    """浮点环境变量（非法/空 → default，绝不抛）。"""
    try:
        raw = str(os.environ.get(name) or "").strip()
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


def _h3_seg_cost_seconds(fallback: float = 0.0) -> float:
    """单段 H3 实测耗时（秒）：env > 标定文件 > 兜底（默认 1020s = 实测 17 分钟）。

    标定文件由每次成功生成后自动写入（见 _h3_record_seg_cost），跨进程有效。
    """
    env = _env_float("MJSCXT_H3_SEGMENT_SECONDS", 0.0)
    if env > 0:
        return env
    try:
        with open(_H3_TIMING_FILE, "r", encoding="utf-8") as f:
            _d = json.load(f) or {}
        _v = float(_d.get("seg_seconds") or 0)
        if _v > 0:
            return _v
    except (OSError, ValueError, TypeError) as _e:
        logger.debug("H3 耗时标定读取失败（用兜底值）：%s", _e)
    return float(fallback or _H3_SEG_COST_DEFAULT_SEC)


def _h3_record_seg_cost(n_segments: int, elapsed_sec: float) -> None:
    """把本轮「整体耗时 / 段数」写进标定文件（EMA 平滑，抗单次抖动）；失败只 debug。"""
    try:
        n = max(1, int(n_segments))
        per = float(elapsed_sec) / n
        if per <= 0:
            return
        old = 0.0
        try:
            with open(_H3_TIMING_FILE, "r", encoding="utf-8") as f:
                old = float((json.load(f) or {}).get("seg_seconds") or 0)
        except (OSError, ValueError, TypeError):
            old = 0.0
        new = per if old <= 0 else (old * 0.6 + per * 0.4)
        os.makedirs(os.path.dirname(_H3_TIMING_FILE), exist_ok=True)
        with open(_H3_TIMING_FILE, "w", encoding="utf-8") as f:
            json.dump({"seg_seconds": round(new, 1), "last_per": round(per, 1),
                       "segments": n,
                       "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
        logger.info("[H3 标定] 单段实测 %.1fs（本轮 %d 段 / %.0fs）→ 下次总超时按此估算",
                    new, n, elapsed_sec)
    except Exception as _e:  # noqa: BLE001
        logger.debug("H3 耗时标定写入失败（忽略）：%s", _e)


def _h3_timeout_seconds(n_segments: int, timeout_per_segment: int = None) -> int:
    """H3 单次提交的总超时（秒）：base + 实测单段耗时 × 段数 × 安全系数。"""
    base = _env_int("MJSCXT_H3_TIMEOUT_BASE", 1800)
    mult = _env_float("MJSCXT_H3_TIMEOUT_MULT", 1.5)
    floor = _env_int("MJSCXT_H3_TIMEOUT_MIN", 3600)
    cap = _env_int("MJSCXT_H3_TIMEOUT_CAP", 0)
    seg = _h3_seg_cost_seconds()
    try:
        tps = float(timeout_per_segment or 0)
    except (TypeError, ValueError):
        tps = 0.0
    if tps > seg:          # 调用方显式给了更大的单段预估 → 尊重它（宁大不小）
        seg = tps
    total = int(base + seg * max(1, int(n_segments)) * mult)
    if cap > 0:
        # ⭐ 上限（运维显式设的）优先于下限：否则 CAP=3000 会被 MIN=3600 顶掉。
        floor = min(floor, cap)
        total = min(total, cap)
    return max(floor, total)

#: 插件侧 normalize_cache_name 对 cache_name 的截断长度（2026-10-12 t72 复核）。
#: 插件实现（director/segment_cache.py）把清洗后的名字截断到 64 字符。
#: 本仓 h3_cache_name 必须保证「截断到该长度后，不同场次/批次仍不撞名」。
H3_CACHE_NAME_MAX_LEN = 64


def h3_cache_name(project_name, ep, scene, batch=None) -> str:
    """H3 分段缓存目录名（t72 加固：区分性字段前置，消除对 safe_key(KEY_MAX_LEN) 的隐性依赖）。

    格式：s{scene}__ep{ep}[__b{batch}]__{proj_fp}__{project_name}

    - 区分性字段（场次 s / 集号 ep / 批次 b）前置：插件 normalize_cache_name 截断到
      H3_CACHE_NAME_MAX_LEN=64 时先丢项目名尾部，不同场次/批次天然不撞名；
    - proj_fp = sha1(project_name)[:8]：项目指纹，超长截断后仍能凭前缀区分不同项目；
    - project_name 放最后（可读性兜底，可被截断）—— 排障仍能从完整名看出项目/集/场。
    """
    proj_fp = hashlib.sha1(str(project_name or "").encode("utf-8")).hexdigest()[:8]
    parts = [f"s{int(scene):02d}", f"ep{int(ep):02d}"]
    if batch is not None:
        parts.append(f"b{int(batch):02d}")
    parts.append(proj_fp)
    parts.append(str(project_name or ""))
    return "__".join(parts)






#: 群像成员的**差异维度**（确定性展开）——保证画面里是「多个不同的人」，
#: 而不是同一个人的复制。每条 = (体型描述, 姿态, 神情)。
#: ⚠️ 2026-10-14（用户报「群像还是一个人」，并给出期望模板）：
#:    「群像合影，{N}个人物并排站立，从左到右依次是：{角色1描述}、{角色2描述}…」
#:    此前只把名单长度当人数，而名单里往往只有**一个集合名词**（外门弟子群像）——
#:    模型只拿到一份描述，于是把同一个人复制 N 遍（实测三张脸完全一致）。
#:    这里为每个位置给一组互不相同的体型/姿态/神情，让第 N 位是「另一个人」。
_CROWD_MEMBER_VARIANTS = (
    ("身材高挑、体型偏瘦", "抱臂而立、下巴微抬", "神情轻蔑"),
    ("中等个头、体形敦实", "双手叉腰", "嘴角下撇、面露讥笑"),
    ("身高偏矮、肩膀厚实", "一手搭在同伴肩上", "咧嘴起哄"),
    ("身形瘦削、脖子略长", "双手拢在袖中", "眼神游移、与人交头接耳"),
    ("体格壮硕、肩背宽厚", "抱胸站着", "眉头拧起、满脸不耐"),
    ("中等偏瘦、背略驼", "一手扶着腰", "低声嘀咕"),
    ("高个宽肩、动作外放", "双臂张开比划", "高声附和"),
    ("矮个精瘦、圆脸", "缩着脖子", "偷偷张望"),
)


def _crowd_member_descriptions(n: int) -> str:
    """按用户给的模板，为群像合影生成 n 位**互不相同**成员的描述。

    ⚠️ 关键：必须逐位给出差异（体型/姿态/神情），否则模型会把同一个人复制 N 遍。
    返回形如：「从左到右依次是：第1位：…；第2位：…」。
    """
    n = max(1, int(n or 1))
    _items = []
    for i in range(n):
        body, pose, mood = _CROWD_MEMBER_VARIANTS[i % len(_CROWD_MEMBER_VARIANTS)]
        _items.append("第%d位（%s，%s，%s）" % (i + 1, body, pose, mood))
    return ("从左到右依次是：%s。" % "；".join(_items) +
            "**这几位必须是长相、体型、神情各不相同的人**（同一门派弟子的不同个体），"
            "绝不能是同一个人物的复制或多角度视图。")


def _strip_sheet_phrases(text: str) -> str:
    """剥离基础提示词里的「三视图横排」类**版面要求**（档位版式会另给版面）。

    ⚠️ 2026-10-14（用户报「群像只生成了一个人」「林清雪没有细节三视图」的深层原因）：
    reference_prompt_zh 模板统一写了「正面、侧面、背面三张全身视图横排，从头到脚完整入画，
    身高比例一致」，而 minor（单张立绘）/ crowd（多人合影）档要的是**别种版面**。
    实测只在后面**追加**新版式不起作用 —— 模型听前面那句：
      · minor 档 prompt =「…三视图横排…。单人全身立绘，正面朝向…」→ 仍画三视图；
      · crowd 档 prompt =「…三视图横排…。群像合影，5 位人物…」→ 画成「同一个人的多角度」。
    故这里先按分句剥离含「视图」的版面描述，再由调用方追加档位版式。

    只剥离**版面句**（含三视图/视图横排等词的分句），不碰角色外观描述 ——
    「黑色短发」这类特征句里不含这些词，不会被误删。
    """
    s = str(text or "").strip()
    if not s:
        return s
    _parts = re.split(r"(?<=[。；;！!？?])", s)
    _kept = [p for p in _parts
             if not any(w in p for w in ("三视图", "视图横排", "三张全身视图",
                                          "正面、侧面", "正面,侧面", "正、侧、背"))]
    out = "".join(_kept).strip()
    return out or s


