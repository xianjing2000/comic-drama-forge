# -*- coding: utf-8 -*-
"""依赖检测模块：核查用户机器上是否具备「模型权重」与「ComfyUI 自定义插件」

背景（见 docs/依赖清单.md）：
  本项目的工作流模板 JSON 已随项目内置，但真正跑起来还差两层：
    ① ComfyUI 侧要装对应的自定义插件包（custom_nodes/）
    ② ComfyUI 侧要下载对应的模型权重（models/ 下若干目录）

本模块做三件事，全部「只读、不阻断」：
  1. 读项目 workflows/ 下 6 个现役模板，提取它们实际用到的**节点类型**集合；
  2. 把这些节点类型映射到所属**插件包**（见 docs/依赖清单.md §1），
     再判定「已装 / 未装 / 无法判定」；
  3. 提取模板里 loader 节点写死的**模型文件名**，按 config.MODULES 的
     MODELS_DIR 等推导路径核查「存在 / 缺失」。

判定策略（双通道）：
  - 在线：ComfyUI /object_info 能访问时，以它为准（最权威——能注册的节点才算有）；
  - 离线：ComfyUI 不在线时，退化扫描 COMFYUI_ROOT/custom_nodes/ 目录名
     （插件包名通常＝目录名），给出「疑似已装 / 未装」并标注不可靠。

本模块不依赖 Flask，可单测：check_deps(comfyui_url=None) 强制离线模式。
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

import requests

from config import (
    COMFYUI_ROOT,
    COMFYUI_URL,
    MODELS_DIR,
    WORKFLOW_TEMPLATE,
    workflow_search_dirs,
)

# 节点类型 → 所属插件包（依据 docs/依赖清单.md §1，本机实测映射）。
# ⚠️ 2026-10-04：H3 视频现役模板已换成 **12 节点单采**（h3_director_r2v_单采.json），
#    故 MiniMaxH3DirectorRefine / BasicScheduler / NvidiaDLSSFrameInterpolation 这三个
#    **仅二采回退模板**（WORKFLOW_TEMPLATE["h3_video_refine"]）需要 —— 这里**不删**映射，
#    避免回退模板被误报「缺插件」。
NODE_TO_PLUGIN = {
    # H3 视频 · Director（现役 h3_director_r2v_单采.json；二采回退 h3_video_refine）
    "MiniMaxH3Director": {"plugin": "ComfyUI_MiniMaxH3_Director", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director"},
    "MiniMaxH3DirectorRefine": {"plugin": "ComfyUI_MiniMaxH3_Director", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "note": "仅二采回退模板需要"},
    "BasicScheduler": {"plugin": "ComfyUI_MiniMaxH3_Director", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "note": "仅二采回退模板需要"},
    "ResolutionSelector": {"plugin": "ComfyUI_MiniMaxH3_Director / KJNodes", "dir": "ComfyUI_MiniMaxH3_Director", "repo": "github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "note": "KJNodes 亦提供，任一即可"},
    "MiniMaxH3TRTVAELoader": {"plugin": "ComfyUI-H3VAE_TRT", "dir": "ComfyUI-H3VAE_TRT", "repo": "github.com/lihaoyun6/ComfyUI-H3VAE_TRT", "note": "视频 VAE 走 TRT；⚠ 音频 VAE 不能用它"},
    "NvidiaDLSSFrameInterpolation": {"plugin": "ComfyUI-NVIDIA-DLSS-Frame-Interpolation", "dir": "ComfyUI-NVIDIA-DLSS-Frame-Interpolation", "repo": "github.com/Comfy-Org/ComfyUI-NVIDIA-DLSS-Frame-Interpolation", "note": "仅二采回退模板需要"},
    "PathchSageAttentionKJ": {"plugin": "ComfyUI-KJNodes", "dir": "ComfyUI-KJNodes", "repo": "github.com/jjy1998/ComfyUI-KJNodes"},
    "EasyCache": {"plugin": "ComfyUI-KJNodes", "dir": "ComfyUI-KJNodes", "repo": "github.com/jjy1998/ComfyUI-KJNodes"},
    "XHImagePrecision": {"plugin": "ComfyUI-KJNodes", "dir": "ComfyUI-KJNodes", "repo": "github.com/jjy1998/ComfyUI-KJNodes"},
    "SolAttnPatch": {"plugin": "ComfyUI-SolAttn_triton", "dir": "ComfyUI-SolAttn_triton", "repo": None, "note": "⚠ morton=False 定档"},
    "TESpeedMiniMaxH3": {"plugin": "TE-Speed-MiniMaxH3", "dir": "TE-Speed-MiniMaxH3", "repo": None, "note": "加速插件"},
    # 图片 · QwenImage2.1（4 个 *_Qwen21.json）
    "TESpeedQwenImage21": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21", "note": "本机为编译 .pyd"},
    "TextEncodeQwenImage21": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21"},
    "SaveImageAdvanced": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21"},
    "QwenImage21Cache": {"plugin": "TE-Speed-QwenImage21", "dir": "TE-Speed-QwenImage21", "repo": "github.com/tl2012tl/TE-Speed-QwenImage21"},
    # 可选/增强
    "PreviewAny": {"plugin": "ComfyUI_BFSNodes（可选）", "dir": "ComfyUI_BFSNodes", "repo": "github.com/chflouis1985/ComfyUI-BFSNodes", "optional": True, "note": "缺失可回退原生"},
    # legacy 回退链
    "H3ContinuousSeamlessJoinV14": {"plugin": "Herrgotts-H3-Infinite-Continuation-Suite", "dir": "Herrgotts-H3-Infinite-Continuation-Suite", "repo": None, "legacy": True},
    "MiniMaxLowVRAMAttention": {"plugin": "KJNodes / minimax-h3-audio-T8", "dir": "ComfyUI-KJNodes", "repo": None, "legacy": True},
    "MiniMaxChunkFeedForward": {"plugin": "KJNodes / minimax-h3-audio-T8", "dir": "ComfyUI-KJNodes", "repo": None, "legacy": True},
    "MiniMaxH3SigmaShift": {"plugin": "KJNodes / minimax-h3-audio-T8", "dir": "ComfyUI-KJNodes", "repo": None, "legacy": True},
}

# ComfyUI 原生节点白名单（无需任何插件）
CORE_NODE_TYPES = {
    "UNETLoader", "CLIPLoader", "LoraLoaderModelOnly", "VAELoader",
    "KSampler", "KSamplerSelect", "VAEDecode", "VAEEncode", "EmptyLatentImage",
    "CreateVideo", "SaveVideo", "MarkdownNote", "Note", "PreviewImage",
    "LoadImage", "LoadImageOutput", "GetNode", "SetNode", "PrimitiveFloat",
    "Text Multiline", "PrimitiveString", "PrimitiveInt", "Reroute",
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def _is_real_node_type(t: str) -> bool:
    """过滤掉 ComfyUI 的 Group 容器（type 为 UUID）与空值，只留真实节点类名。"""
    if not t or not isinstance(t, str):
        return False
    if t in ("Group", "GroupNode"):
        return False
    if _UUID_RE.match(t):
        return False
    return True

# 模型清单（与 docs/依赖清单.md §2 **同源**，2026-09-29 审计对齐双向补齐；
# required=False 的条目缺失只提示、不判"缺依赖"）
MODEL_CHECKLIST = [
    {"path": "diffusion_models\\minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", "kind": "video_unet", "required": True, "note": "H3 UNET 主模型（现役，2026-09-27 起由 minimaxH3Singularity 换为 ref2va）"},
    {"path": "text_encoders\\minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "kind": "video_clip", "required": True, "note": "H3 CLIP（minimax/Qwen3-VL）；ComfyUI 实际归在 text_encoders/ 而非 clip/"},
    {"path": "loras\\minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "kind": "video_lora", "required": True, "note": "通道A 4 步加速 LoRA（单采模板 h3_director_r2v_单采.json 的 LoraLoaderModelOnly id=80，**唯一实际执行**的加速 LoRA）"},
    {"path": "loras\\minimax_h3\\minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors", "kind": "video_lora", "required": False, "note": "通道B fl2v 变体 LoRA（模板 id=55）**仅二采回退模板 h3_video_refine 使用，单采模板不加载**；H3_ENABLE_REFINE=False 时不执行，对成片零影响"},
    {"path": "vae\\minimax-h3\\minimax_h3_audio_vae_fp32.safetensors", "kind": "audio_vae", "required": True, "note": "音频 VAE"},
    {"path": "vae\\minimax_h3_vae_decoder_w4a16_awq.engine", "kind": "video_vae_trt", "required": True, "note": "TRT 视频 VAE 解码（GPU 架构专用）"},
    {"path": "vae\\minimax_h3_vae_encoder.engine", "kind": "video_vae_trt", "required": True, "note": "TRT 视频 VAE 编码（GPU 架构专用）"},
    {"path": "diffusion_models\\qwen_image_2.1_int8_convrot.safetensors", "kind": "image_unet", "required": True, "note": "QwenImage2.1 UNET"},
    {"path": "text_encoders\\qwen3vl_8b_int8_convrot.safetensors", "kind": "image_clip", "required": True, "note": "QwenImage2.1 CLIP；ComfyUI 实际归在 text_encoders/ 而非 clip/"},
    {"path": "vae\\qwen_image_2.1_vae_bf16.safetensors", "kind": "image_vae", "required": True, "note": "QwenImage2.1 VAE"},
    {"path": "diffusion_models\\minimax_h3_latent_upscaler_3d_bf16.safetensors", "kind": "video_latent_upscaler", "required": False, "note": "latent 3D 超分（可选，未进现役模板硬校验）"},
    {"path": "FlashVSR-v1.1\\diffusion_pytorch_model_streaming_dmd.safetensors", "kind": "upscale_dit", "required": False, "note": "超分 DiT（enable_upscale=true 才需要）"},
]


# ---------------- 工作流模板：节点类型 / 模型引用 提取 ----------------

def _resolve_workflow_files() -> List[Dict]:
    """把 WORKFLOW_TEMPLATE（key→文件名）解析成实际存在的文件清单。

    用 config.workflow_search_dirs() 的顺序找每个模板文件；找不到的记 missing。
    """
    dirs = [d for d in workflow_search_dirs() if d]
    items: List[Dict] = []
    seen_files = set()
    for key, fname in WORKFLOW_TEMPLATE.items():
        found = None
        for d in dirs:
            p = os.path.join(d, fname)
            if os.path.isfile(p):
                found = p
                break
        items.append({
            "key": key,
            "file": fname,
            "path": found or "",
            "found": found is not None,
        })
        if found:
            seen_files.add(found)
    # 去重：同一文件被多个 key 引用（multiview_gen/storyboard_gen 共用 分镜生成_Qwen21.json）
    dedup: List[Dict] = []
    for it in items:
        if it["path"] and any(x["path"] == it["path"] for x in dedup):
            continue
        dedup.append(it)
    return dedup


def _extract_nodes_and_models(items: List[Dict]) -> Dict:
    """读每个模板 JSON，聚合节点类型集合 + loader 写死的模型文件名。"""
    node_types: set = set()
    wf_nodes: List[Dict] = []      # 每个工作流的节点类型明细
    ref_models: set = set()        # 模板 loader 里出现的模型文件名（basename）

    for it in items:
        if not it["found"]:
            wf_nodes.append({"key": it["key"], "file": it["file"], "types": [], "error": "文件缺失"})
            continue
        try:
            with open(it["path"], encoding="utf-8") as f:
                j = json.load(f)
        except Exception as e:  # JSON 损坏
            wf_nodes.append({"key": it["key"], "file": it["file"], "types": [], "error": str(e)})
            continue
        types = sorted({
            n.get("type") for n in (j.get("nodes") or [])
            if isinstance(n, dict) and _is_real_node_type(n.get("type"))
        })
        node_types.update(types)
        wf_nodes.append({"key": it["key"], "file": it["file"], "types": types, "error": ""})

        # 提取 loader 的 widgets_values 里第一个字符串（通常是模型文件名）
        for n in j.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            t = n.get("type")
            if t in ("UNETLoader", "CLIPLoader", "LoraLoaderModelOnly", "VAELoader",
                     "MiniMaxH3TRTVAELoader"):
                wv = n.get("widgets_values") or []
                for v in wv:
                    if isinstance(v, str) and (v.endswith(".safetensors") or v.endswith(".engine")
                                                or v.endswith(".ckpt") or v.endswith(".pth")):
                        ref_models.add(v.replace("\\", "/").split("/")[-1])

    return {
        "node_types": sorted(node_types),
        "per_workflow": wf_nodes,
        "ref_models": sorted(ref_models),
    }


# ---------------- ComfyUI 在线探测 ----------------

def _comfyui_online(url: str, timeout: int = 60) -> bool:
    """ComfyUI /system_stats 可达即在线。超时/连接失败/非 200 一律 False。"""
    if not url:
        return False
    try:
        r = requests.get(f"{url}/system_stats", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _fetch_object_info(url: str, timeout: int = 60) -> Optional[Dict]:
    """拉 /object_info（已注册的节点类型全集）。失败返回 None。"""
    if not url:
        return None
    try:
        r = requests.get(f"{url}/object_info", timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


# ---------------- 离线：扫描 custom_nodes 目录 ----------------

def _custom_nodes_dirs(root: str) -> List[str]:
    """COMFYUI_ROOT 下探测 custom_nodes/（两种结构），返回目录名列表。

    ComfyUI 可能装在：
      A) COMFYUI_ROOT/ComfyUI/ComfyUI/custom_nodes（portable）
      B) COMFYUI_ROOT/custom_nodes（标准）
      C) COMFYUI_ROOT 本身即 ComfyUI 根 → COMFYUI_ROOT/custom_nodes
    """
    if not root:
        return []
    cands = [
        os.path.join(root, "custom_nodes"),
        os.path.join(root, "ComfyUI", "ComfyUI", "custom_nodes"),
        os.path.join(root, "ComfyUI", "custom_nodes"),
    ]
    for c in cands:
        if os.path.isdir(c):
            try:
                return sorted(os.listdir(c))
            except OSError:
                continue
    return []


# ---------------- 模型文件核查 ----------------

def _check_models(ref_models: List[str], models_dir: str,
                  rel_by_name: Optional[Dict[str, str]] = None) -> List[Dict]:
    """逐模型核查存在性：先按清单完整相对路径精确匹配，再递归按 basename 兜底。

    ⭐ 2026-10-09 修复（用户实测反馈「我有这个模型啊」）：
      旧实现只扫 models/ 的**一级**子目录（models/<sub>/<file>，见 `os.listdir(sp)`），
      但本机实际布局是**两级**：
          models/diffusion_models/minimax-h3/minimax_h3_ref2va_pruned_int8_convrot.safetensors
          models/text_encoders/minimax-h3/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
          models/vae/minimax-h3/minimax_h3_audio_vae_fp32.safetensors
      三个**真实存在**的模型因此被误报「缺失」，进而把它们写进 blockers ——
      用户按报告去补下载只会白费功夫。

    现在两级策略：
      ① rel_by_name 给出清单里的完整相对路径（如 diffusion_models\\minimax-h3\\x.safetensors）
         → 直接 os.path.isfile 精确命中；
      ② 未命中则用**递归**索引 os.walk 按 basename 兜底（兼容自定义布局）。

    返回每个模型 {name, found, path, subdirs}。
    """
    rel_by_name = rel_by_name or {}
    results: List[Dict] = []
    if not models_dir or not os.path.isdir(models_dir):
        for name in ref_models:
            results.append({"name": name, "found": False, "path": "", "subdirs": []})
        return results

    # 递归索引：basename → 全部命中路径（模型目录只有几百个文件，os.walk 成本可忽略）
    index: Dict[str, List[str]] = {}
    try:
        for _root, _dirs, _files in os.walk(models_dir):
            for _fn in _files:
                index.setdefault(_fn, []).append(os.path.join(_root, _fn))
    except OSError:
        pass

    for name in ref_models:
        hits: List[str] = []
        # ① 清单给的完整相对路径（\ 与 / 都兼容）
        _rel = str(rel_by_name.get(name) or "").strip()
        if _rel:
            _exact = os.path.join(models_dir, _rel.replace("\\", os.sep).replace("/", os.sep))
            if os.path.isfile(_exact):
                hits.append(_exact)
        # ② 递归 basename 兜底
        if not hits:
            hits = list(index.get(name, []))
        results.append({
            "name": name,
            "found": bool(hits),
            "path": hits[0] if hits else "",
            "subdirs": sorted({os.path.dirname(h).replace(models_dir + os.sep, "") for h in hits}),
        })
    return results


# ---------------- 主入口 ----------------

def check_deps(comfyui_url: Optional[str] = None,
               force_offline: bool = False) -> Dict:
    """依赖检测主入口。

    参数：
      comfyui_url  指定 ComfyUI 地址（默认用 config.COMFYUI_URL）。传 None 且
                   force_offline=False 时用默认 URL 尝试在线。
      force_offline  强制离线（跳过 /object_info，只扫 custom_nodes 目录）。

    返回结构化 dict：
      {
        comfyui: {url, online, source},          # source: online|offline|unconfigured
        plugins: {
            needed: [ {node_type, plugin, repo, optional, legacy, status} ],
            ready: bool,                        # 全部必需插件就位（非 legacy/非 optional）
            missing_required: [ ... ],
            missing_optional: [ ... ],
        },
        models: {
            dir, dir_exists,
            items: [ {name, found, path} ],
            ready: bool,
            missing_required: [ ... ],
        },
        workflows: [ {key, file, found, types} ],
        summary: {plugins_ok, models_ok, all_ok, blockers: [ ... ]},
      }
    """
    url = comfyui_url if comfyui_url is not None else COMFYUI_URL
    online = False if force_offline else _comfyui_online(url)
    object_info = None if force_offline else _fetch_object_info(url)
    online_nodes = set(object_info.keys()) if object_info else None

    # ---- 工作流与节点 ----
    wf_items = _resolve_workflow_files()
    extracted = _extract_nodes_and_models(wf_items)
    all_types: set = set(extracted["node_types"])

    # ---- 插件判定 ----
    needed: List[Dict] = []
    for nt in sorted(all_types):
        if nt in CORE_NODE_TYPES:
            continue
        spec = NODE_TO_PLUGIN.get(nt, {"plugin": "未知插件", "dir": nt, "repo": None})
        # 状态：
        if online_nodes is not None:
            # 在线：该节点类型在 object_info 注册即认为对应插件可用
            registered = nt in online_nodes
            # Director 包还负责 ResolutionSelector 等；只要任一节点注册成功即认为该插件就位
            status = "ok" if registered else "missing"
        else:
            # 离线：扫 custom_nodes 目录名（插件包目录≈目录名）
            cdirs = _custom_nodes_dirs(COMFYUI_ROOT)
            hit = any(
                d.replace("-", "").lower() == spec["dir"].replace("-", "").lower()
                for d in cdirs
            ) if cdirs else False
            status = "suspected" if hit else ("missing" if not cdirs else "unknown")
            # cdirs 为空（找不到 custom_nodes）→ 无法判定
            if not cdirs:
                status = "unknown"
        needed.append({
            "node_type": nt,
            "plugin": spec["plugin"],
            "repo": spec.get("repo"),
            "optional": bool(spec.get("optional")),
            "legacy": bool(spec.get("legacy")),
            "note": spec.get("note", ""),
            "status": status,
        })

    missing_required = [p for p in needed
                        if p["status"] in ("missing", "unknown")
                        and not p["optional"] and not p["legacy"]]
    missing_optional = [p for p in needed
                        if p["status"] in ("missing", "unknown") and p["optional"]]

    # ---- 模型核查 ----
    ref_models = extracted["ref_models"]
    # 若模板 loader 里没提取到模型名（例如模板被改动），回落用 MODEL_CHECKLIST 的 basename
    if not ref_models:
        ref_models = [m["path"].split("\\")[-1] for m in MODEL_CHECKLIST if m.get("required")]
    # 清单里 basename → 完整相对路径（供 _check_models 精确匹配，治两级目录误报）
    _rel_by_name = {m["path"].split("\\")[-1]: m["path"] for m in MODEL_CHECKLIST if m.get("path")}
    model_items = _check_models(ref_models, MODELS_DIR, rel_by_name=_rel_by_name)

    # 必需模型清单（来自 MODEL_CHECKLIST）
    required_names = {m["path"].split("\\")[-1] for m in MODEL_CHECKLIST if m.get("required")}
    models_missing_required = [
        m["name"] for m in model_items
        if m["name"] in required_names and not m["found"]
    ]
    models_dir_exists = bool(MODELS_DIR) and os.path.isdir(MODELS_DIR)

    # ---- 汇总 ----
    plugins_ok = len(missing_required) == 0
    models_ok = len(models_missing_required) == 0
    all_ok = plugins_ok and models_ok and online  # 在线是「可实际运行」前提
    blockers: List[str] = []
    if not online:
        blockers.append("ComfyUI 不在线（无法确认插件节点是否注册）")
    if not models_dir_exists:
        blockers.append(f"模型目录不存在：{MODELS_DIR}")
    if missing_required:
        blockers.append(f"缺失必需插件：{', '.join(p['plugin'] for p in missing_required)}")
    if models_missing_required:
        blockers.append(f"缺失必需模型：{', '.join(models_missing_required)}")

    return {
        "comfyui": {
            "url": url,
            "online": online,
            "source": "online" if online_nodes is not None
                      else ("offline_scan" if _custom_nodes_dirs(COMFYUI_ROOT) else "unconfigured"),
            "online_node_count": len(online_nodes) if online_nodes is not None else 0,
        },
        "plugins": {
            "needed": needed,
            "ready": plugins_ok,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
        },
        "models": {
            "dir": MODELS_DIR,
            "dir_exists": models_dir_exists,
            "items": model_items,
            "required_names": sorted(required_names),
            "ready": models_ok,
            "missing_required": models_missing_required,
        },
        "workflows": [
            {"key": w["key"], "file": w["file"], "found": w["found"],
             "types": [x["types"] for x in extracted["per_workflow"] if x["key"] == w["key"]][0]
             if any(x["key"] == w["key"] for x in extracted["per_workflow"]) else []}
            for w in wf_items
        ],
        "per_workflow_nodes": extracted["per_workflow"],
        "summary": {
            "plugins_ok": plugins_ok,
            "models_ok": models_ok,
            "all_ok": all_ok,
            "blockers": blockers,
        },
    }



