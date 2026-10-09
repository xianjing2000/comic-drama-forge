# -*- coding: utf-8 -*-
"""ComfyUI 模型 / 插件扫描 + 用户选定模型的持久化。

背景（2026-09-29）：
    H3 Director 工作流里两个 UNETLoader 原本**写死裸文件名**，而 ComfyUI 的
    UNETLoader combo 合法值取决于模型在磁盘上的**目录布局**（放进
    `diffusion_models/minimax-h3/` 后名字会带 `minimax-h3\\` 前缀），
    于是模板里的裸名不在合法值列表里 → 校验失败 → 视频输出被静默丢弃。

    根因教训：ComfyUI combo 值 = 目录布局 + 文件名，**挪动模型等于改名**。
    任何在模板里写死模型文件名的做法都是脆的。因此这里统一以
    ComfyUI `object_info` 为**唯一权威来源**去枚举可选模型，让用户手动指定。

对外 API：
    scan(force=False)            -> 扫描 ComfyUI 可用模型槽位候选 + 已装插件
    load_selection()             -> 读取用户已选定的模型覆盖表
    save_selection(dict)         -> 原子写入用户选定（None/"" 表示清空该槽位）
    resolve_selection()          -> 注入层读取（剔空 + LoRA 槽位只放 H3 系）
    overrides_for(workflow_key)  -> WORKFLOW_TEMPLATE 的 key → 节点级覆盖表
    overrides_for_template(name) -> 模板文件名 → 节点级覆盖表

    align_prompt_models(prompt)  -> 提交前把工作流里的模型名对齐到本机 ComfyUI 的
                                    真实可用值（object_info 为准，见文末「运行时模型名对齐」）

设计要点：
    - 只读为主：scan 不修改任何工作流/模板，只上报。
    - 向前兼容：未选择任何槽位时 load_selection() 返回空 dict，
      注入层据此保持模板原样，行为与改动前完全一致。
    - LoRA 双层白名单（2026-10-04）：模型槽位下拉（scan 过滤候选 + save_selection
      写入拦截 + resolve_selection 读侧剔除）只放 MiniMax H3 系 LoRA，
      禁止图片链路 LoRA 进视频生成。判据见 ``h3_segment_loras.is_h3_family_lora``。
"""

import json
import logging
import os
import time

from fs_atomic import atomic_write_json, read_json_strict
from h3_segment_loras import is_h3_family_lora

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 持久化

# 全局（与项目无关）：ComfyUI 是本机唯一一套模型，按项目分裂反而是负担。
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "comfyui_models.json",
)

# ---------------------------------------------------------------- 槽位定义
#
# slot_key 面向**用户可读的职责**（"视频主模型"），而不是面向模板节点 id。
# targets 描述该槽位要写入**哪个模板文件**的哪些节点（Task #127 注入层据此落地）。
#
# ⚠️ 2026-10-04 改为**按模板文件名**索引（原来是按 workflow key "h3_video"）：
#    同一个 workflow key（"h3_video"）会随 env / config/workflow_mapping.json 指向
#    不同模板文件（单采 12 节点 / 二采 23 节点），两套模板的节点 id 完全不同。
#    若按 key 索引，用户切回二采模板时会往「单采的 id」上写值 —— 而
#    h3_director_builder.apply_model_overrides 对**不存在**的 id 只 warning 后
#    continue（不报错）⇒ 静默失效。按文件名索引后，覆盖永远落到实际生效模板的真实 id。
#
# node_type / field 决定从 object_info 的哪一处取候选值。
# values_filter == "h3_lora" 的槽位：scan() 过滤候选、save_selection() 拒绝非 H3 值
# （见 h3_segment_loras.is_h3_family_lora）。
SLOTS = {
    "unet_main": {
        "label": "视频主模型（UNET）",
        "hint": "采样通道的主模型（单采模板 1 处 / 二采模板 2 处），选一次即同时写入",
        "node_type": "UNETLoader",
        "field": "unet_name",
        "targets": {
            "h3_director_r2v_单采.json": [78],
            "minimax_h3_director_二采_加速.json": [1, 54],
        },
    },
    "lora_channel_a": {
        "label": "通道A 加速 LoRA",
        "hint": "ref2v 分支的加速 LoRA（单采模板 id=80 / 二采模板 id=16）",
        "node_type": "LoraLoaderModelOnly",
        "field": "lora_name",
        # ⭐ 白名单：只接受 MiniMax H3 系 LoRA，禁止图片链路 LoRA 混入视频生成
        "values_filter": "h3_lora",
        "targets": {
            "h3_director_r2v_单采.json": [80],
            "minimax_h3_director_二采_加速.json": [16],
        },
    },
    "lora_channel_b": {
        "label": "通道B 加速 LoRA（仅二采回退模板有该通道）",
        "hint": "fl2v 分支的加速 LoRA（二采回退模板 id=55）；单采模板不含此通道",
        "node_type": "LoraLoaderModelOnly",
        "field": "lora_name",
        "values_filter": "h3_lora",
        "targets": {
            # 单采模板无第二通道 → 空列表（槽位保留，前端/守卫仍可引用该 key）
            "h3_director_r2v_单采.json": [],
            "minimax_h3_director_二采_加速.json": [55],
        },
    },
    "clip": {
        "label": "文本编码器（CLIP）",
        "hint": "影响中文语义理解与提示词还原度",
        "node_type": "CLIPLoader",
        "field": "clip_name",
        "targets": {
            "h3_director_r2v_单采.json": [79],
            "minimax_h3_director_二采_加速.json": [2],
        },
    },
    "vae_audio": {
        "label": "音频 VAE",
        "hint": "⚠️ 必须接 fp32 音频专用 VAE，接视频 VAE 会因通道数不符崩溃",
        "node_type": "VAELoader",
        "field": "vae_name",
        # ⚠️ 二采回退模板的两个副本 id 不同：项目内 workflows/ 副本 23 节点 → id=67
        #    （实际生效，resolve_workflow_path 项目优先）；ComfyUI 目录副本 24 节点 → id=68。
        #    此处按**实际生效**的项目副本取 67。（原值 67 本就正确，无需改。）
        "targets": {
            "h3_director_r2v_单采.json": [86],
            "minimax_h3_director_二采_加速.json": [67],
        },
    },
    "vae_video_decoder": {
        "label": "视频 VAE 解码器（TRT 引擎）",
        "hint": "MiniMaxH3TRTVAELoader 的 decoder",
        "node_type": "MiniMaxH3TRTVAELoader",
        "field": "decoder",
        "targets": {
            "h3_director_r2v_单采.json": [83],
            "minimax_h3_director_二采_加速.json": [48],
        },
    },
    "vae_video_encoder": {
        "label": "视频 VAE 编码器（TRT 引擎）",
        "hint": "MiniMaxH3TRTVAELoader 的 encoder",
        "node_type": "MiniMaxH3TRTVAELoader",
        "field": "encoder",
        "targets": {
            "h3_director_r2v_单采.json": [83],
            "minimax_h3_director_二采_加速.json": [48],
        },
    },
}

# 槽位展示顺序（保持字典顺序稳定，避免前端乱序）
SLOT_ORDER = ["unet_main", "clip", "vae_video_decoder", "vae_video_encoder",
              "vae_audio", "lora_channel_a", "lora_channel_b"]


# ---------------------------------------------------------------- 模板名解析

def _template_file_for(workflow_key):
    """把 ``WORKFLOW_TEMPLATE`` 的 key 解析成**模板文件名**（解析不到返回空串）。

    延迟 import ``config``：``config`` 是叶子模块（不 import app 内其它模块），
    但为绝对避免 import 顺序问题（本模块可能被较早导入），这里在函数体内 import，
    并每次读 ``config.WORKFLOW_TEMPLATE`` 的**实时值**（它是支持外置覆盖的可变字典）。
    """
    try:
        import config  # 延迟 import，避免任何潜在的环依赖
        tpl = getattr(config, "WORKFLOW_TEMPLATE", None) or {}
        return str(tpl.get(workflow_key) or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("解析 workflow key %r 的模板名失败：%s", workflow_key, e)
        return ""


def _filter_slot_values(meta, values):
    """对带 ``values_filter`` 的槽位过滤候选值（当前只认 ``"h3_lora"``）。"""
    if meta.get("values_filter") == "h3_lora":
        return [v for v in values if is_h3_family_lora(v)]
    return list(values)


# ---------------------------------------------------------------- 扫描

def _combo_values(object_info, node_type, field):
    """从 object_info 取某节点某字段的候选值列表。

    ComfyUI 的 combo 声明在 `input.required[field]` 的第一个元素里，
    形如 `[["a.safetensors", "b.safetensors"], {...}]`。
    """
    try:
        blob = (object_info or {}).get(node_type) or {}
        req = (blob.get("input") or {}).get("required") or {}
        spec = req.get(field)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(spec, (list, tuple)) or not spec:
        return []
    opts = spec[0]
    if isinstance(opts, list):
        return list(opts)
    return []


def _installed_packages(object_info):
    """聚合已安装的自定义节点插件包。

    两种常见做法这里都不用：

    * ``/extensions`` 会返回几百个**内置前端 JS 路径**，噪音极大，没有参考价值；
    * ComfyUI Manager 的 ``cnr_id`` 并非所有环境都装了 Manager，依赖它就等于
      把「能不能看见插件」绑定到「有没有装另一个插件」上。

    改用 ``object_info`` 里每个节点都有的 ``python_module``：

    * 内置核心节点 → ``nodes`` / ``comfy.*``；
    * 自定义节点 → ``custom_nodes.<包名>.<模块>``（实测 MiniMaxH3Director 就是
      ``custom_nodes.ComfyUI_MiniMaxH3_Director``）。

    据此即可稳定地推断出装了哪些自定义节点包，无需额外依赖。
    """
    pkgs = {}
    core = 0
    for cls, blob in (object_info or {}).items():
        mod = ""
        try:
            mod = (blob or {}).get("python_module") or ""
        except Exception:  # noqa: BLE001
            continue
        if not mod.startswith("custom_nodes."):
            core += 1
            continue
        pkg = mod[len("custom_nodes."):].split(".")[0] or "(unknown)"
        entry = pkgs.setdefault(pkg, {"id": pkg, "node_count": 0, "nodes": []})
        entry["node_count"] += 1
        if len(entry["nodes"]) < 12:
            entry["nodes"].append(cls)
    return {
        "custom_packages": sorted(pkgs.values(), key=lambda x: -x["node_count"]),
        "core_node_count": core,
    }


def scan(client, force=False):
    """扫描 ComfyUI：各模型槽位的合法候选值 + 已安装插件。

    失败不抛：返回 success=False 与错误信息，前端可据此提示「ComfyUI 离线」。
    """
    t0 = time.time()
    out = {
        "success": True,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slots": [],
        "plugins": [],
        "error": "",
    }
    try:
        object_info = client.get_object_info(force=bool(force))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ComfyUI 模型扫描失败: {e}")
        return {**out, "success": False, "error": f"ComfyUI 未就绪：{e}"}

    if not object_info:
        return {**out, "success": False, "error": "ComfyUI 在线但未返回 object_info"}

    selection = load_selection()
    for key in SLOT_ORDER:
        meta = SLOTS.get(key)
        if not meta:
            continue
        values = _combo_values(object_info, meta["node_type"], meta["field"])
        # ⭐ 白名单：带 values_filter 的槽位（LoRA 槽）只暴露 H3 系候选，
        #    图片链路 LoRA 不进下拉框（用户需求 3 第 2 层）。
        values = _filter_slot_values(meta, values)
        current = selection.get(key) or ""
        out["slots"].append({
            "key": key,
            "label": meta["label"],
            "hint": meta["hint"],
            "node_type": meta["node_type"],
            "field": meta["field"],
            "values": values,
            "selected": current,
            # 已选值不在合法值里 → 标红，避免用户以为"选了但没生效"
            # （含「存量脏数据是非 H3 LoRA」的情况：过滤后不在 values 里 → 标红）
            "selected_valid": (not current) or (current in values),
            "available": bool(values),
        })

    pkgs = _installed_packages(object_info)
    out["plugins"] = pkgs["custom_packages"]
    out["core_node_count"] = pkgs["core_node_count"]
    out["node_type_count"] = len(object_info)
    out["elapsed_sec"] = round(time.time() - t0, 2)
    return out


# ---------------------------------------------------------------- 持久化

def load_selection():
    """读取用户已选定的模型覆盖表（key -> 模型名）。文件不存在返回空 dict。"""
    try:
        data = read_json_strict(_STORE_PATH, default={})
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    # 只保留已知槽位，防脏数据污染注入层
    return {k: v for k, v in data.items() if k in SLOTS and isinstance(v, str)}


def save_selection(patch):
    """合并写入用户选定。value 为 None/"" 表示清空该槽位（回落到模板原值）。

    ⭐ 写入侧闸门（fail-closed）：带 ``values_filter="h3_lora"`` 的槽位，
    值非空且 ``not is_h3_family_lora(val)`` → **直接抛 ValueError 拒绝**，
    在写入任何内容之前校验，避免手改 ``output/comfyui_models.json`` 或绕过前端
    把图片链路 LoRA 塞进视频生成。调用方（app.py 的
    ``/api/comfyui/models`` POST）会把该异常转成 500 + 错误体 → 前端能看到拒绝理由。
    """
    if not isinstance(patch, dict):
        return load_selection()
    # ---- 先整体校验（在任何写入之前），任一非法即整体拒绝 ----
    for key, val in patch.items():
        meta = SLOTS.get(key)
        if not meta:
            continue
        if meta.get("values_filter") != "h3_lora":
            continue
        if val in (None, ""):
            continue
        if not is_h3_family_lora(str(val)):
            raise ValueError(
                f"槽位 {key} 仅接受 MiniMax H3 系 LoRA，拒绝非 H3 值：{val!r}")
    cur = load_selection()
    for key, val in patch.items():
        if key not in SLOTS:
            continue
        if val in (None, ""):
            cur.pop(key, None)
        else:
            cur[key] = str(val)
    try:
        os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
        atomic_write_json(_STORE_PATH, cur)
    except Exception as e:  # noqa: BLE001
        logger.error(f"写入 ComfyUI 模型选择失败: {e}")
        raise
    return cur


def resolve_selection():
    """给注入层用：返回 {slot_key: value}（已剔除空值 + 非 H3 脏值）。

    ⭐ 读取侧白名单（幂等）：带 ``values_filter="h3_lora"`` 的槽位，若存量值是
    非 H3 LoRA（历史脏数据），这里剔除并 warning —— 防「手改 JSON 绕过写入闸门」
    的存量脏值真的进到视频生成。
    """
    out = {}
    for k, v in load_selection().items():
        if not v:
            continue
        meta = SLOTS.get(k) or {}
        if meta.get("values_filter") == "h3_lora" and not is_h3_family_lora(v):
            logger.warning(
                "ComfyUI 模型槽位 %s 的存量值 %r 不是 MiniMax H3 系 LoRA，已剔除"
                "（仅接受 H3 系，防图片链路 LoRA 混入视频生成）", k, v)
            continue
        out[k] = v
    return out


def overrides_for_template(tpl_name):
    """把用户选定翻译成「模板节点级」覆盖表：{node_id: {field: value}}。

    ``tpl_name`` 是**模板文件名**（如 ``h3_director_r2v_单采.json``）——SLOTS 的
    ``targets`` 按文件名索引，故调用方应传实际生效的模板文件名（可用
    ``os.path.basename(resolve_workflow_path(...))``）。
    未做任何选择时返回空 dict，注入层据此保持模板原样（行为等同改动前）。
    """
    name = str(tpl_name or "")
    selection = resolve_selection()
    if not selection or not name:
        return {}
    out = {}
    for key, val in selection.items():
        meta = SLOTS.get(key)
        if not meta:
            continue
        field = meta.get("field")
        if not field:
            continue
        for nid in ((meta.get("targets") or {}).get(name) or []):
            out.setdefault(nid, {})[field] = val
    return out


def overrides_for(workflow_key="h3_video"):
    """``WORKFLOW_TEMPLATE`` 的 key → 节点级覆盖表（兼容旧调用契约）。

    与 ``overrides_for_template`` 的区别：本函数先把 key 解析成**实际生效的模板
    文件名**再查表 —— 这样 env / ``config/workflow_mapping.json`` 切换模板时，
    覆盖会自动落到对应模板的真实节点 id 上（见 SLOTS 上方注释）。
    若传入的不是已知 key（例如直接给了文件名），则按文件名处理（向后兼容）。

    未做任何选择时返回空 dict，注入层据此保持模板原样（行为等同改动前）。
    """
    tpl_name = _template_file_for(workflow_key) or str(workflow_key or "")
    return overrides_for_template(tpl_name)


# ---------------------------------------------------------------- 运行时模型名对齐
#
# 背景（2026-10-08 实测事故，两起同源）：
#   ① H3 单采模板 id=86 VAELoader 的 vae_name 写死为
#      MiniMax\minimax_h3_audio_vae_fp32.safetensors，而本机 ComfyUI 的合法列表是
#      minimax-h3\minimax_h3_audio_vae_fp32.safetensors（删掉重复模型后目录名变了）
#      → /prompt 400：「节点 86(VAELoader): Value not in list[输入 vae_name]」
#      → 整集 18 段视频全部被丢弃；
#   ② 分镜模板里 TE_MAN 增强节点的 mmproj 同理 → 该镜分镜图生成失败。
#
# 根因是同一句话：模型名 = 目录布局 + 文件名。把它写死在模板 / 代码默认值 / 用户选定里，
# 换一台机器（或同一台机器换了目录布局）就会失效。
#
# 因此提交前统一做一次「权威对齐」——以 ComfyUI 的 object_info（它自己 /prompt 校验
# 用的那份列表）为准，把工作流里所有「模型文件型」控件值与当前布局对齐：
#     值仍在列表 → 原样不动；
#     不在列表   → 按「唯一候选 → 同名文件 → 同名+目录归一化 → 归一化同名 → 关键词打分」
#                  自动改写成本机真实地址；
#     匹配不上   → 原样保留 + WARNING 指明哪个节点哪个字段（fail-open：自动对齐本身
#                  绝不阻断生成，更不会「猜」一个模型塞进去）。
#
# ⚠️ 只碰「模型文件型」控件：候选值里出现模型扩展名（.safetensors/.gguf/.engine…）才算。
#    sampler_name / scheduler / precision 这类无扩展名枚举天然不会被误改。

#: 模型文件扩展名白名单（判定「这个控件是不是模型文件型」的唯一依据，与字段名语言无关）
MODEL_FILE_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".engine",
                   ".trt", ".onnx", ".sft", ".patch", ".pkl")

#: 关键词打分的「语义词」——命中权重高于普通 token（数字 / 量化标签等）
_MODEL_SEMANTIC_TOKENS = (
    "audio", "video", "vision", "encoder", "decoder", "mmproj", "vae", "unet", "clip",
    "llm", "pe", "engine", "turbo", "fl2v", "ref2v", "t2v", "i2v", "hybrid", "lora",
)

#: 反斜杠（用 chr 取，避免源码里出现转义序列造成歧义）
_BS = chr(92)
#: token 切分占位符（Windows 文件名不允许 |，不会与真实文件名冲突）
_SEP = "|"

#: object_info 的 TTL 缓存（供不经 ComfyUIClient 的提交路径复用，如 tts_client）
_OBJECT_INFO_CACHE = {"url": "", "ts": 0.0, "data": None}


def _leaf(name) -> str:
    """取「文件名」部分（同时兼容 Windows 反斜杠与 / 两种分隔符）。"""
    return str(name or "").replace("/", _BS).split(_BS)[-1]


def _dirname(name) -> str:
    """取「目录前缀」部分（无分隔符时返回空串）。"""
    s = str(name or "").replace("/", _BS)
    return s.rsplit(_BS, 1)[0] if _BS in s else ""


def _strip_ext(name) -> str:
    """去掉已知模型扩展名（不认识的后缀原样返回）。"""
    s = _leaf(name)
    low = s.lower()
    for ext in MODEL_FILE_EXTS:
        if low.endswith(ext):
            return s[: -len(ext)]
    return s


def _norm(s) -> str:
    """归一化：小写 + 去掉 - _ . 与空格，用于「同一模型的不同写法」比较。"""
    out = str(s or "").lower()
    for ch in ("-", "_", ".", " ", "　"):
        out = out.replace(ch, "")
    return out


def _tokens(name):
    """把文件名切成 token（丢掉扩展名、纯数字与单字符）。"""
    raw = _strip_ext(name).lower()
    for ch in ("-", "_", ".", " ", "　"):
        raw = raw.replace(ch, _SEP)
    return [t for t in raw.split(_SEP) if t and not t.isdigit() and len(t) > 1]


def _has_model_ext(v) -> bool:
    """值是否长得像模型文件（只看扩展名，与语言/字段名无关）。"""
    s = str(v or "").lower()
    return any(s.endswith(e) for e in MODEL_FILE_EXTS)


def _score_tokens(cur_leaf, cand_leaf) -> int:
    """关键词打分：语义词命中 3 分、普通 token 命中 1 分。"""
    a, b = set(_tokens(cur_leaf)), set(_tokens(cand_leaf))
    common = a & b
    return len(common) + 3 * len([t for t in common if t in _MODEL_SEMANTIC_TOKENS])


def resolve_model_value(current, candidates):
    """把 current 对齐到 candidates 里的真实可用值，返回 (值, 说明)。

    说明为空串 = 无需改动或**无法可靠改动**（调用方据此决定是否告警）。
    匹配优先级（越前越可信）：唯一候选 → 同名文件 → 同名+目录名归一化 → 归一化同名
    → 关键词打分（唯一最优且明显领先）。

    ⚠️ 宁可不改也不猜：同名多命中且无法用目录名区分、或打分不够领先时一律返回空，
    交回原值让 ComfyUI 自己报错 —— 猜错模型（例如给音频 VAE 塞一个视频 VAE）比失败更糟。
    """
    cur = str(current or "")
    cands = [str(c) for c in (candidates or []) if str(c).strip()]
    if not cur.strip() or not cands:
        return "", ""
    if cur in cands:
        return "", ""
    if len(cands) == 1:
        return cands[0], "候选唯一"
    leaf = _leaf(cur).lower()
    # ① 同名文件（只差目录前缀）—— 「模型被挪了目录」场景命中率最高，证据也最强
    same = [c for c in cands if _leaf(c).lower() == leaf]
    if len(same) == 1:
        return same[0], "同名文件（仅目录前缀不同）"
    if len(same) > 1:
        cur_dir = _norm(_dirname(cur))
        narrowed = [c for c in same if _norm(_dirname(c)) == cur_dir] if cur_dir else []
        if len(narrowed) == 1:
            return narrowed[0], "同名文件 + 目录名归一化一致"
        return "", ""       # 同名多份且分不清 → 不猜
    # ② 归一化同名（大小写 / 分隔符 / 扩展名写法不同）
    key = _norm(_strip_ext(cur))
    near = [c for c in cands if _norm(_strip_ext(c)) == key]
    if len(near) == 1:
        return near[0], "归一化同名"
    if len(near) > 1:
        return "", ""
    # ③ 关键词打分：必须「唯一最优且明显领先」，否则一律不动
    scored = sorted(((_score_tokens(cur, c), c) for c in cands), key=lambda x: -x[0])
    best, best_v = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0
    if best >= 3 and (best - second) >= 2:
        return best_v, "关键词匹配（%d 分，次优 %d 分）" % (best, second)
    return "", ""


def normalize_prompt_models(api_prompt, object_info):
    """提交前把工作流里所有模型文件型控件值与 object_info 对齐（**就地修改**）。

    返回 {"model_fields": 扫描到的模型控件数, "changed": [...], "unresolved": [...]}。
    object_info 为空（ComfyUI 离线 / 未取到）时直接返回空报告 → 调用方 fail-open。
    """
    report = {"model_fields": 0, "changed": [], "unresolved": []}
    if not isinstance(api_prompt, dict) or not api_prompt:
        return report
    if not isinstance(object_info, dict) or not object_info:
        return report
    for nid, node in list(api_prompt.items()):
        if not isinstance(node, dict):
            continue
        cls = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not cls or not isinstance(inputs, dict):
            continue
        blob = object_info.get(cls)
        if not isinstance(blob, dict):
            continue        # 未知节点类型：交给 ComfyUI 自己报错，本层不动它
        raw = blob.get("input") or {}
        decl = {}
        for sect in ("required", "optional"):
            part = raw.get(sect)
            if isinstance(part, dict):
                decl.update(part)
        for field, val in list(inputs.items()):
            if not isinstance(val, str) or not val.strip():
                continue
            spec = decl.get(str(field))
            cands = spec[0] if (isinstance(spec, (list, tuple)) and spec
                                and isinstance(spec[0], list)) else None
            if not cands or not all(isinstance(c, str) for c in cands):
                continue
            # 只认「模型文件型」控件：当前值或候选值里出现模型扩展名
            if not (_has_model_ext(val) or any(_has_model_ext(c) for c in cands)):
                continue
            report["model_fields"] += 1
            if val in cands:
                continue
            new, why = resolve_model_value(val, cands)
            entry = {"node": str(nid), "class_type": cls, "field": str(field),
                     "old": val, "candidates": len(cands)}
            if new:
                inputs[field] = new
                entry["new"] = new
                entry["why"] = why
                report["changed"].append(entry)
            else:
                report["unresolved"].append(entry)
    return report


def fetch_object_info(base_url, ttl=300, timeout=20):
    """带 TTL 缓存的 /object_info 拉取（供不经 ComfyUIClient 的提交路径复用）。

    离线 / 异常一律返回 {}（调用方据此跳过对齐，按原样提交）——只 warning，不抛。
    """
    import urllib.request
    url = str(base_url or "").rstrip("/")
    if not url:
        return {}
    now = time.time()
    cache = _OBJECT_INFO_CACHE
    if (cache.get("data") is not None and cache.get("url") == url
            and (now - float(cache.get("ts") or 0)) < max(1, int(ttl))):
        return cache["data"]
    try:
        with urllib.request.urlopen(url + "/object_info", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, dict):
            data = {}
    except Exception as e:  # noqa: BLE001
        logger.warning("拉取 ComfyUI object_info 失败（本次跳过模型名对齐）：%s", e)
        return {}
    cache.update({"url": url, "ts": now, "data": data})
    return data


def align_prompt_models(api_prompt, object_info=None, base_url="", tag=""):
    """提交前「模型名对齐」统一收口：取 object_info → 对齐 → 打日志（永不抛）。

    object_info 缺省时按 base_url 拉取（带 TTL 缓存）。改动与无法解析的项都用 WARNING
    打出**节点 / 字段 / 原值 / 新值**，运行日志里可直接追溯。
    """
    empty = {"model_fields": 0, "changed": [], "unresolved": []}
    try:
        if not isinstance(object_info, dict) or not object_info:
            if not base_url:
                return empty
            object_info = fetch_object_info(base_url)
        rep = normalize_prompt_models(api_prompt, object_info)
        prefix = ("[模型名对齐] " + tag).rstrip()
        for ch in rep.get("changed") or []:
            logger.warning("%s 节点 %s(%s).%s：%r → %r（%s）", prefix, ch["node"],
                           ch["class_type"], ch["field"], ch["old"], ch["new"], ch["why"])
        for ur in rep.get("unresolved") or []:
            logger.warning("%s 节点 %s(%s).%s=%r 不在 ComfyUI 合法列表里且无法自动匹配"
                           "（该输出大概率会被 ComfyUI 拒绝，请到「模型管理」重选或把模型"
                           "放回原目录）", prefix, ur["node"], ur["class_type"], ur["field"],
                           ur["old"])
        return rep
    except Exception as e:  # noqa: BLE001
        logger.warning("模型名对齐失败（按原样提交）：%s", e)
        return empty

