# -*- coding: utf-8 -*-
"""ComfyUIClient 的「工作流装载/转换/校验」方法（D-3 拆类，2026-10-16）。

从 app/comfyui_client.py **原样搬来**（不做逻辑改动），以 mixin 形式由 ComfyUIClient 继承。
本模块 import config/comfyui_shared 等叶子依赖，**不 import comfyui_client**（避免成环）。
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

from comfyui_shared import (
    MEDIA_INPUT_KEYS, PSEUDO_VALUES, PSEUDO_WIDGETS, VIRTUAL_NODE_TYPES, _is_dynamic_child,
)

logger = logging.getLogger(__name__)


class _WorkflowMixin:
    @staticmethod
    def _inject_director_cache_name(wf: dict, cache_name: str) -> int:
        """给工作流里的 MiniMaxH3Director 节点写入 cache_name，返回改了几个节点。"""
        if not cache_name or not isinstance(wf, dict):
            return 0
        n = 0
        for nd in (wf.get("nodes") or []):
            if not isinstance(nd, dict):
                continue
            if nd.get("class_type") == "MiniMaxH3Director" or nd.get("type") == "MiniMaxH3Director":
                named = nd.get("widgets_values_named")
                if not isinstance(named, dict):
                    named = {}
                    nd["widgets_values_named"] = named
                if str(named.get("cache_name") or "").strip() != str(cache_name):
                    named["cache_name"] = str(cache_name)
                    n += 1
        return n


    @staticmethod
    def build_storyboard_workflow_api(ref_count: int, *,
                                      unet_name: str,
                                      clip_name: str,
                                      vae_name: str,
                                      lora_name: str = "",
                                      lora_strength: float = 1.0,
                                      steps: int = 40,
                                      cfg: float = 1.0,
                                      seed: int = 0,
                                      denoise: float = 1.0,
                                      sampler_name: str = "euler",
                                      scheduler: str = "simple",
                                      attention: str = "sageattn",
                                      megapixels: float = 1.0,
                                      aspect_ratio: str = "16:9 (Widescreen)",
                                      multiple: int = 32,
                                      filename_prefix: str = "Qwen_image_2.1",
                                      enable_cache_node: bool = True,
                                      base_image: bool = False) -> dict:
        """按参考图数量生成分镜工作流的 **API prompt**（不再读 JSON 模板）。

        :param ref_count: 参考图槽位数量（= 本次实际要喂的图片数）。
            传 4 就只有 4 个 LoadImageOutput/ImageScaleToTotalPixels —— 不存在多余槽位，
            因此也就没有「清空/删除尾部槽位」那套三步联动清理。
        :returns: 可直接 POST /prompt 的 API 字典（结构见下方节点编号表）。

        节点编号（固定语义，便于排障）：
          1 UNETLoader / 2 CLIPLoader / 3 VAELoader / 4 TESpeed / 5 Cache(可选)
          6 KSampler / 7 TextEncodeQwenImage21（prompt+negative+latent 三输出）
          100+i LoadImageOutput（第 i 张参考图，i 从 0 起）
          200+i ImageScaleToTotalPixels（与 100+i 一一对应）
          300 VAEDecode / 301 SaveImageAdvanced / 302 ResolutionSelector
        """
        n = max(1, int(ref_count or 0))
        api: dict = {}
        api["1"] = {"class_type": "UNETLoader",
                    "inputs": {"unet_name": unet_name, "weight_dtype": "default"}}
        api["2"] = {"class_type": "CLIPLoader",
                    "inputs": {"clip_name": clip_name, "type": "qwen_image",
                               "device": "default"}}
        api["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}}
        # TE-Speed 加速：attention 必须是 sageattn（kitchen_int8 等于关掉加速器，实测慢 1.5 倍）
        # LoRA（可选）：Qwen-Image-2.1-Fun-Acc-4Step 这类加速 LoRA。
        #   挂在 UNETLoader 之后、TESpeed 之前 —— 加速器要作用在「已挂 LoRA」的模型上。
        #   lora_name 为空则不生成该节点，行为与旧版**完全一致**（零变更）。
        unet_src = ["1", 0]
        if lora_name:
            api["8"] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": ["1", 0], "lora_name": lora_name,
                                   "strength_model": float(lora_strength)}}
            unet_src = ["8", 0]
        # TE-Speed 加速：attention 必须是 sageattn（kitchen_int8 等于关掉加速器，实测慢 1.5 倍）
        api["4"] = {"class_type": "TESpeedQwenImage21",
                    "inputs": {"model": unet_src, "attention": attention,
                               "step_cache": "te_predictor", "reuse_threshold": 0.06,
                               "start_percent": 0, "end_percent": 0,
                               "predictor_error_limit": 0.08, "verbose": True}}
        model_src = ["4", 0]
        if enable_cache_node:
            api["5"] = {"class_type": "QwenImage21Cache",
                        "inputs": {"model": ["4", 0], "device": "auto", "dtype": "default"}}
            model_src = ["5", 0]
        # 参考图槽位：按 ref_count 动态生成，编号 100+i / 200+i
        image_links: dict = {}
        for i in range(n):
            lid, sid = str(100 + i), str(200 + i)
            api[lid] = {"class_type": "LoadImageOutput", "inputs": {"image": ""}}
            api[sid] = {"class_type": "ImageScaleToTotalPixels",
                        "inputs": {"image": [lid, 0], "upscale_method": "nearest-exact",
                                   "megapixels": megapixels, "resolution_steps": multiple}}
            image_links["images.image_%d" % (i + 1)] = [sid, 0]
        # 正向编码节点：clip + 全部参考图槽 + 负向词 + 一段式 latent（三输出）
        te_inputs: dict = {"clip": ["2", 0]}
        te_inputs.update(image_links)
        te_inputs.update({"vae": ["3", 0], "prompt": "", "negative_prompt": "",
                          "resolution": 0})
        api["7"] = {"class_type": "TextEncodeQwenImage21", "inputs": te_inputs}
        # ---- 编辑式修复（2026-10-15 用户口径）----
        # 质检不达标时**不再从零重画**：以「上一版失败的图」为画布（VAEEncode -> latent），
        # 配低 denoise + 修复提示词做编辑式修复。
        #    实测收益：全量重生成 530s vs 编辑式修复 26~56s（快 10~20 倍）；更重要的是
        #    能保住已经正确的部分（构图/场景/其他角色/光线），只改质检指出的那一处缺陷 ——
        #    从零重画会把对的部分一起丢掉，这正是「赵天霸发型越改越偏」的放大器。
        # 节点 400 LoadImageOutput / 401 ImageScaleToTotalPixels / 402 VAEEncode，
        #    图名由调用方（generate_storyboard）上传后填入。
        if base_image:
            api["400"] = {"class_type": "LoadImageOutput", "inputs": {"image": ""}}
            api["401"] = {"class_type": "ImageScaleToTotalPixels",
                          "inputs": {"image": ["400", 0], "upscale_method": "nearest-exact",
                                     "megapixels": megapixels, "resolution_steps": multiple}}
            api["402"] = {"class_type": "VAEEncode",
                          "inputs": {"pixels": ["401", 0], "vae": ["3", 0]}}
            latent_src = ["402", 0]
        else:
            latent_src = ["7", 2]
        api["6"] = {"class_type": "KSampler",
                    "inputs": {"model": model_src, "positive": ["7", 0],
                               "negative": ["7", 1], "latent_image": latent_src,
                               "seed": int(seed), "steps": int(steps), "cfg": float(cfg),
                               "sampler_name": sampler_name, "scheduler": scheduler,
                               "denoise": float(denoise)}}
        api["300"] = {"class_type": "VAEDecode",
                      "inputs": {"samples": ["6", 0], "vae": ["3", 0]}}
        api["301"] = {"class_type": "SaveImageAdvanced",
                      "inputs": {"images": ["300", 0], "filename_prefix": filename_prefix,
                                 "format": "png", "format.bit_depth": "8-bit",
                                 "format.input_color_space": "sRGB"}}
        api["302"] = {"class_type": "ResolutionSelector",
                      "inputs": {"aspect_ratio": aspect_ratio,
                                 "megapixels": megapixels, "multiple": multiple}}
        return api


    @staticmethod
    def _find_ref_slot_nodes(api: dict) -> list:
        """列出代码生成工作流里的参考图槽位节点 id（LoadImageOutput / ImageScale）与编码键。"""
        loads = sorted([int(k) for k, v in api.items()
                        if v.get("class_type") == "LoadImageOutput"])
        scales = sorted([int(k) for k, v in api.items()
                         if v.get("class_type") == "ImageScaleToTotalPixels"])
        return loads, scales


    @staticmethod
    def _extract_storyboard_params(api: dict) -> dict:
        """从（旧模板转换来的）API prompt 里提取生成参数，供代码重建时复用。"""
        def first(ct: str):
            for v in api.values():
                if v.get("class_type") == ct:
                    return v.get("inputs") or {}
            return {}
        un = first("UNETLoader"); cl = first("CLIPLoader"); va = first("VAELoader")
        ks = first("KSampler"); rs = first("ResolutionSelector")
        sc = first("ImageScaleToTotalPixels"); te = first("TESpeedQwenImage21")
        cache = any(v.get("class_type") == "QwenImage21Cache" for v in api.values())
        lo = first("LoraLoaderModelOnly")
        return {
            "unet_name": un.get("unet_name") or "",
            "clip_name": cl.get("clip_name") or "",
            "vae_name": va.get("vae_name") or "",
            "lora_name": lo.get("lora_name") or "",
            "lora_strength": float(lo.get("strength_model") if lo.get("strength_model") is not None else 1.0),
            "steps": int(ks.get("steps") or 40),
            "cfg": float(ks.get("cfg") if ks.get("cfg") is not None else 1.0),
            "sampler_name": ks.get("sampler_name") or "euler",
            "scheduler": ks.get("scheduler") or "simple",
            "denoise": float(ks.get("denoise") if ks.get("denoise") is not None else 1.0),
            "attention": te.get("attention") or "sageattn",
            "megapixels": float(rs.get("megapixels") or sc.get("megapixels") or 1.0),
            "aspect_ratio": rs.get("aspect_ratio") or "16:9 (Widescreen)",
            "multiple": int(rs.get("multiple") or 32),
            "enable_cache_node": bool(cache),
        }


    def _rebuild_storyboard_workflow(self, api: dict, ref_count: int,
                                    base_image: bool = False) -> dict:
        """用代码重建分镜节点图（槽位 = ref_count），参数沿用传入 api 里的取值。

        为什么这样做（2026-10-15 用户口径）：原实现直接用手工维护的 JSON 模板，
        固定 9 槽 + 需要「尾部槽位三步联动清理」，历史上因此出过 KSampler 参数错位、
        节点数量与实参不符等问题。改为**参数从模板取、结构由代码生成** ——
        既保留用户可调参数的便利，又让槽位数量天然正确、不再需要清理逻辑。
        """
        try:
            params = self._extract_storyboard_params(api)
            rebuilt = self.build_storyboard_workflow_api(
                max(1, int(ref_count)), base_image=base_image, **params)
            logger.info("[分镜工作流] 代码重建：槽位=%d（原模板 %d 槽）节点 %d → %d",
                        max(1, int(ref_count)),
                        len(self._find_ref_slot_nodes(api)[0]), len(api), len(rebuilt))
            return rebuilt
        except Exception as e:  # noqa: BLE001 —— 重建失败回落原模板，绝不阻断出图
            logger.warning("[分镜工作流] 代码重建失败（回落 JSON 模板）：%s", e)
            return api


    def load_workflow(self, workflow_file: str, return_meta: bool = False,
                      cache_name: str = None):
        """加载工作流文件并转换为 API prompt 格式

        cache_name（2026-10-12 新增）：非空时给 Director 节点注入插件新增的
        cache_name 控件 → 分段缓存落到独立目录，避免多场次互相覆盖。
        """
        workflow_path = resolve_workflow_path(workflow_file)
        with open(workflow_path, "r", encoding="utf-8-sig") as f:   # 兼容 UTF-8 BOM
            wf = json.load(f)
        if cache_name:
            _hit = self._inject_director_cache_name(wf, cache_name)
            if _hit:
                logger.info("[H3-Director] 分段缓存目录名 cache_name=%r（%d 个节点）"
                            "—— 本批产物与其它场次/集次互不覆盖", cache_name, _hit)
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


