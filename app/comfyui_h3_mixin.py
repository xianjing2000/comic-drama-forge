# -*- coding: utf-8 -*-
"""ComfyUIClient 的「_H3Mixin」方法（D-3 拆类，2026-10-16）。

从 app/comfyui_client.py **原样搬来**（零逻辑改动），以 mixin 形式被 ComfyUIClient 继承；
本模块 import config/comfyui_shared（叶子），**不 import comfyui_client**（避免成环）。
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
    PROMPT_ENHANCER_CLASS, PROMPT_ENHANCER_IMAGE_FIELDS, _env_float, _env_int, _h3_record_seg_cost, _h3_seg_cost_seconds, _h3_timeout_seconds,
)

logger = logging.getLogger(__name__)


class _H3Mixin:
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
                            size=None, base_image=None, denoise=None) -> dict:
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
        # ⭐ 2026-10-15（用户口径）：「直接用代码生成 json，控制拼接成工作流」——
        #    从模板取出模型/步数/加速等参数，**用代码重建节点图**，槽位数量 = 实际参考图数。
        #    这样不再依赖固定 9 槽的模板结构，也就不需要尾部槽位清理那套逻辑。
        #    重建失败会在内部回落原模板（fail-open），不阻断出图。
        api_prompt = self._rebuild_storyboard_workflow(
            api_prompt, len(ref_images or []), base_image=bool(base_image))
        seed_changed = self._inject_seed(api_prompt, seed)
        if seed_changed:
            logger.info(f"分镜采样种子已注入: {seed_changed}")

        # 1) 正向提示词：只写正向字段（正负同体节点的 negative_prompt 必须原样保留）
        node_id = self._find_positive_text_node(api_prompt)
        pos_field = self._positive_field(api_prompt, node_id) if node_id else None
        if node_id is None or not pos_field:
            raise RuntimeError(f"{wf_name} 未定位到正向提示词字段（节点={node_id}）")
        api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        # ⭐ 2026-10-17（官方口径）：Qwen-Image 官方 Edit / 参考图示例的 negative 一律是
        #    **一个空格 " "**（"Recommended if you don't use a negative prompt"），而非空串，
        #    也不是 T2I 的那条长中文 negative。本分镜走 Edit 型参考图节点 → 按官方设 " "。
        try:
            _ni = api_prompt[node_id]["inputs"]
            if "negative_prompt" in _ni and not str(_ni.get("negative_prompt") or "").strip():
                _ni["negative_prompt"] = " "
        except Exception as _ne:  # noqa: BLE001
            logger.debug("设置官方 negative（空格）失败（忽略）：%s", _ne)
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
        # ---- 编辑式修复：把「上一版失败的图」上传为画布 #400（2026-10-15 用户口径）----
        #    质检不达标时不再从零重画，而是以失败图为底 + 低 denoise + 修复提示词做编辑。
        #    与参考图走同一上传通道（image_type="output"），保证 LoadImageOutput 读得到。
        #    任何失败都回落全量重生成（fail-open），绝不阻断出图。
        if base_image:
            try:
                _bl = self.resolve_local_path(base_image) if isinstance(base_image, str) else None
                if _bl and os.path.exists(_bl) and api_prompt.get("400"):
                    _bn = "sb_fix_%s.png" % os.path.splitext(os.path.basename(filename_prefix))[0]
                    _up = self.upload_image(_bl, _bn, image_type="output")
                    # P0-5：LoadImageOutput 读 output 目录，**必须**带 " [output]" 标注，
                    #    否则 ComfyUI 直接 prompt_outputs_failed_validation（节点 400 验证失败）——
                    #    实测踩过：漏标注时编辑式修复整条链路 400 失败。用既有工具保证幂等。
                    _image_ref = self.annotate_file_ref(_up, "LoadImageOutput")
                    api_prompt["400"]["inputs"]["image"] = _image_ref
                    logger.info("[编辑式修复] 画布已注入 #400=%r（源图 %s）",
                                _image_ref, os.path.basename(_bl))
                else:
                    logger.warning("[编辑式修复] 失败图不可用，回落全量重生成：%s", base_image)
            except Exception as _be:  # noqa: BLE001
                logger.warning("[编辑式修复] 画布上传失败（回落全量重生成）：%s", _be)
        if denoise is not None:
            _dn = max(0.0, min(1.0, float(denoise)))
            for _v in api_prompt.values():
                if isinstance(_v, dict) and _v.get("class_type") == "KSampler":
                    _v["inputs"]["denoise"] = _dn
            logger.info("[编辑式修复] denoise=%.2f（<1 才只改缺陷、不全量重画）", _dn)

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
            # ⚠️ 措辞务必写「删除节点」而不是「留空」（2026-10-12 修）：
            # 旧文案是「空槽位已留空」，读起来像「空槽仍占着、节点还在跑」，
            # 实测会误导排查方向（连开发者也据此以为 9 组节点每次都执行）。
            # 实际行为见上方 4477-4495：**连同 LoadImageOutput 与中间缩放层一起
            # 从 API prompt 里 pop 掉**，不提交给 ComfyUI 执行。
            # 实测裁剪幅度：模板 9+9=18 个槽位节点 -> 实际提交 4+4=8 或 3+3=6。
            logger.info(f"分镜参考图空槽位已删除节点 {len(slot_cleared)} 处"
                        f"（LoadImageOutput + 中间缩放层，不提交执行；"
                        f"Qwen-Image-2.1「容量非目标」，不塞重复图）: {slot_cleared}")
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
        cache_name: str = None,
        on_qc_retry=None,   # 可选：on_qc_retry(attempt, segs, verdict) -> 新 segs | None（整片重试前调用）
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
                common_prompt=common_prompt, cache_name=cache_name)
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
        # 2026-10-12: retry reason classes. Seed means different things for each.
        #   "quality"   - QC failed (content) -> changing seed is correct (new dice).
        #   "transport" - link failure (ComfyUI returned no file / missing file /
        #                 generation exception) -> unrelated to seed; changing it only
        #                 invalidates the ComfyUI segment cache, so every already-
        #                 finished segment is regenerated from scratch.
        #   Measured waste before the fix: scene 1 (28 shots / 146s / 47% of the
        #   episode) had completed at 19:15, then the 1st QC failure changed the seed
        #   and re-ran the whole episode; at 23:07 the 2nd attempt failed with
        #   "ComfyUI returned no video file" (pure transport) and changed the seed
        #   AGAIN, wasting scene 1 a second time. ~6h GPU burned across retries.
        retry_kind = "quality"
        # ⭐ 2026-10-13：上一轮质检 verdict（供 on_qc_retry 回调按本次缺陷即时改写段提示词）。
        _last_verdict: dict = {}

        for attempt in range(max_retries + 1):
            attempts_used = attempt + 1
            # ⭐ 2026-10-13：整片重试前，给调用方一次「按上一轮质检缺陷**即时改写**段提示词」的机会。
            #   此前重试只换种子、提示词一字不变 —— 同一份提示词换骰子重投，QC 大概率仍不通过
            #   （自述注释里的「瞎撞」）。分镜/资产链路的做法是「用本次 issues 让 LLM 即时改写
            #   提示词再重试」，视频此前缺这一步；这里补上回调挂点，改写策略由调用方决定。
            #   fail-open：回调异常 / 返回非法值 → 用原 segments，绝不阻断重试。
            if attempt > 0 and on_qc_retry is not None and _last_verdict:
                try:
                    _ns = on_qc_retry(attempt, segs, _last_verdict)
                    if isinstance(_ns, list) and _ns:
                        segs = _ns
                        logger.info(
                            "[H3-episode] 第 %d 次重试前已按本轮质检缺陷即时改写段提示词"
                            "（%d 段）", attempt + 1, len(segs))
                except Exception as _oe:  # noqa: BLE001
                    logger.warning(
                        "[H3-episode] 重试前改写段提示词失败（忽略，用原提示词）：%s", _oe)
            if attempt > 0 and retry_kind == "transport":
                logger.info("[H3-episode] attempt %d retry (transport failure, KEEPING seed %s "
                            "so the already-finished segments stay cached)",
                            attempt + 1, cur_seed)
            elif attempt > 0:
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
                    cache_name=cache_name,
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
                # 生成异常 = 链路层失败：保持种子，让段缓存可复用
                retry_kind = "transport"
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次生成异常: {e}")
                continue

            files = result.get("files") or []
            last_prompt_id = result.get("prompt_id") or last_prompt_id
            last_seg_report = result.get("segments") or []
            if not files:
                # 未返回文件 = 链路层失败（与种子无关）：保持种子重试
                retry_kind = "transport"
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次：ComfyUI 未返回视频文件")
                continue
            if not os.path.isfile(files[0]):
                retry_kind = "transport"
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
                # QC 不通过 = 质量层失败：换种子才是正确的（换骰子）
                retry_kind = "quality"
                _last_verdict = _v   # 供下一次 attempt 的 on_qc_retry 使用
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
                             common_prompt: str = None,
                             cache_name: str = None) -> dict:
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
                common_prompt=common_prompt,
                cache_name=cache_name)

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


        timeout = timeout or _h3_timeout_seconds(n, timeout_per_segment)
        logger.info(f"H3 提交：{n} 段，总超时 {timeout}s"
                    f"（单段估算 {_h3_seg_cost_seconds():.0f}s × {n} 段 × 系数"
                    f" {_env_float('MJSCXT_H3_TIMEOUT_MULT', 1.5)} + 基线"
                    f" {_env_int('MJSCXT_H3_TIMEOUT_BASE', 1800)}s；"
                    f"可用 MJSCXT_H3_TIMEOUT_* / MJSCXT_H3_SEGMENT_SECONDS 覆盖）")
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
        common_prompt: Optional[str] = None,
        cache_name: str = None) -> dict:
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
                # ⚠️ 2026-10-18（8G 显存应急）：按比例降分辨率。960×544 + 243 帧的 VAE
                #    解码在 8G 卡上从第 2 段起必死锁（GPU 100% / 功耗 32W / 30+ 分钟零输出）。
                #    MJSCXT_H3_RES_SCALE=0.66 → 640×352，VAE 显存需求砍半；默认 1.0 不生效。
                try:
                    _rs = float(os.environ.get("MJSCXT_H3_RES_SCALE", "1.0") or 1.0)
                except ValueError:
                    _rs = 1.0
                if 0.2 <= _rs < 1.0:
                    _w2 = max(64, int(w * _rs) // 32 * 32)
                    _h2 = max(64, int(h * _rs) // 32 * 32)
                    logger.info("[H3-Director] 分辨率按 MJSCXT_H3_RES_SCALE=%.2f 降为 %d×%d（原 %d×%d）",
                                _rs, _w2, _h2, w, h)
                    w, h = _w2, _h2
            except (TypeError, ValueError, IndexError):
                logger.warning(f"[H3-Director] 画幅入参非法，改用模板值: {size!r}")
                w = h = None
        if continuity_overlap is None:
            ov_env = str(os.environ.get("MJSCXT_H3_CONTINUITY_OVERLAP") or "").strip()
            continuity_overlap = (int(ov_env) if ov_env.isdigit()
                                  else h3_director_builder.CONTINUITY_OVERLAP_DEFAULT)
        # ⭐ 2026-10-12 分段导出（用户需求：「只重生成有问题的分镜，再拼回去」）。
        # ⭐ 2026-10-13 用户口径（随「超分不切片」同批确认）：「导出方式改成**全部导出**吧，
        #    我电脑超分 5 段分镜还是带的动的」→ **常规出片链一律 all（全部导出）**。
        #    segments 只作为显式排障 / 单镜返工开关保留（函数参数或 MJSCXT_H3_EXPORT_MODE），
        #    **不得**成为默认，也不要接到前端常规入口上。
        #
        # ⚠️ t13/B6 校正：**当前默认 export_mode = "all"**（见下方那行的 or "all"；t24 已把
        #    它钉为既定边界：tests/test_t24_verification_boundaries.py::test_default_is_all）。
        #    曾短暂试过默认 segments，**已回退 all**，回退原因：
        #      · generate_h3_sequence_sequential 只回传 files[0]：默认 segments 会让
        #        「多段导出 → 只取第一段」静默截断成片（整片 QC 也只抽到第一段）；
        #      · all 由插件内部把 N 段拼成 1 个 mp4，判据链（QC 抽帧 / 成片可播 /
        #        probe_video / final）与历史行为逐位一致，零回归风险。
        #    历史留档（当时试改的取舍，以免重复踩坑）——曾短暂把默认改为 segments，
        #    两种模式的差别如下：
        #      all      -> 插件内部把 N 段拼成 1 个 mp4；某一段坏了只能**整集重跑**
        #                  （实测代价：28 段 / 数小时，且 Retry 会连带换种子重跑全部段）。
        #      segments -> 插件按段导出 N 个 mp4；调用方（video_helpers）把 N 段落盘为
        #                  seg_0000.mp4..seg_NNNN.mp4 后**自己 concat** 成 <集>_full.mp4，
        #                  单段返工只需替换那一段再重新 concat（成本从「整集」降到「一段」）。
        #    需要按段导出时**显式开启**（不改默认值）：函数参数 export_mode="segments"
        #    或环境变量 MJSCXT_H3_EXPORT_MODE=segments。
        #    覆盖优先级：函数参数 > 环境变量 MJSCXT_H3_EXPORT_MODE > 默认 all。
        #
        # 向后兼容（务必保持）：video_helpers 始终产出 <tag>_full.mp4，
        #   probe_video / final / 下游一切按老路径取片，行为不变；
        #   segments 模式下**额外**多出的只有 seg_*.mp4 这一份「可返工素材」。
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
        # audio_mode 优先级：逐镜 TTS 驱动音轨（seg audios）> 公共参考音色 → "generate" >
        #   emit_audio → "generate" / "mute"。
        #   ⭐ 2026-10-19（用户 P0「配音语义破碎」）：**逐镜 TTS 驱动音轨最优先** ——
        #   generate 模式由 H3"演"台词（伪中文乱码/人名漂移/把画面描述念出来，
        #   见 shot_dub 模块头），source 用 TTS 音轨驱动口型 ⇒ 台词逐字准确、
        #   音色=角色克隆音色。旧的「公共音色优先」正是乱码的入口。
        _has_dub_tracks = bool(any(seg_audio_names))
        _eff_audio_mode = audio_mode
        if _eff_audio_mode is None:
            if _has_dub_tracks:
                _eff_audio_mode = "source"
            elif common_audio_names:
                # 无逐镜配音时退回旧行为：公共音色作条件、台词自生成（音质不可靠，
                # 但比无声强；要台词准确请开 MJSCXT_H3_DUB_DRIVEN）。
                _eff_audio_mode = "generate"
            else:
                _eff_audio_mode = "generate" if emit_audio else "mute"
        if _has_dub_tracks and common_audio_names:
            # source 模式下驱动音频来自逐段 dub；公共参考音色若同时挂进 global.refAudios
            # 会与逐段音频抢槽位（官方上限 3 支），这里整体让位。
            logger.info("[H3-Director] 逐镜 TTS 驱动音轨生效 → 公共参考音色不再挂载"
                        "（%d 支让位，source 模式）", len(common_audio_names))
            common_audio_names = []
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

        # ⭐ 2026-10-12：给 Director 节点写入分段缓存目录名 —— 插件旧版缓存目录是
        #   minimax_seg_cache/<node_id>（节点 id 恒为 12），导致第 2 场的 seg_0000
        #   覆盖第 1 场的 seg_0000。插件本次更新后支持 cache_name → <name>_<node_id>，
        #   填上它即按「项目+集+场」分目录，多场次互不覆盖（详见 load_workflow）。
        if cache_name:
            _cn_hit = self._inject_director_cache_name(wf, cache_name)
            if _cn_hit:
                logger.info("[H3-Director] 分段缓存目录名 cache_name=%r（%d 个节点）",
                            cache_name, _cn_hit)

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

        # ⭐ t18：总超时按「实测单段耗时 × 段数 × 安全系数 + 基线」估算（全部 env 可配）。
        #  旧公式 1200 + timeout_per_segment(900) × 段数 对 10 段只有 10200s，而实测
        #  单段 ≈ 1020s（17 分钟）→ 恰好卡在临界点，每次都在最后一刻超时。
        _t18_t0 = time.time()
        timeout = timeout or _h3_timeout_seconds(n, timeout_per_segment)
        logger.info(f"H3(Director) 提交：{n} 段，总超时 {timeout}s"
                    f"（单段估算 {_h3_seg_cost_seconds():.0f}s × {n} 段 × 系数"
                    f" {_env_float('MJSCXT_H3_TIMEOUT_MULT', 1.5)} + 基线"
                    f" {_env_int('MJSCXT_H3_TIMEOUT_BASE', 1800)}s；"
                    f"到点后仍会先复查续等 {_env_int('MJSCXT_WAIT_EXTENSIONS', 3)} 次），"
                    f"段间引导 {'开' if layout.get('continuity') else '关'}"
                    f"（{layout.get('continuity_overlap_frames')} 帧）")

        # 崩溃免重渲（2026-09-29）：整集一次提交要跑几十分钟，崩溃/重启后先查台账与
        # 远端 history —— 能复用就复用、还在跑就重连，绝不重复提交白烧 GPU。
        history, prompt_id, resumed = self.submit_resumable(
            api_prompt, job_key=f"h3|{filename_prefix}",
            timeout=timeout, file_ext=".mp4", label=f"H3(Director) {n} 段")
        if resumed:
            logger.info("H3(Director) 本次为**免重渲复用**（未消耗 GPU）")
        files = self.get_output_files(history, ".mp4")
        # ⭐ t18：本轮成功产出 → 把「实测单段耗时」写进标定文件，下次超时估算直接用实测值
        #   （resumed=True 是台账复用、没真正渲染，不计入标定）。
        if files and not resumed:
            _h3_record_seg_cost(n, time.time() - _t18_t0)

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
            _raw = str(ref.get("appearance") or ref.get("description")
                       or ref.get("reference_prompt_zh") or "").strip()[:120]
            # ⭐ 2026-10-19（A 方案）：外观设定同走英文化（10段模板的 <Subject N>
            #    是整句英文外观）；缓存 + fail-open，原样回落。
            try:
                import h3_en_text as _h3en
                _raw = _h3en.englishize_text(_raw, kind="appearance")
            except Exception:  # noqa: BLE001
                return _raw
            return _raw

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
                # ⭐ 2026-10-19：场景图可自带用途声明（ref["desc"]），有则逐字采用。
                #    首个使用者 =「上一场末镜衔接」（跨场视觉锚点）：通用「环境参考，
                #    定义场景结构/材质/光照」的措辞会诱导 H3 把上一场的空间结构搬进
                #    新场景；带 desc 时按调用方口径声明「只接续服装/道具/光位」。
                _scene_desc = str(ref.get("desc") or "").strip()
                picture_defs.append((
                    _next_label(),
                    _scene_desc or f"{name} 的环境参考，定义场景结构、材质氛围与光照基调"))
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
            _scene_desc = str(ref.get("desc") or "").strip()
            picture_defs.append((
                _next_label(),
                _scene_desc or f"{name} 的环境参考，定义场景结构、材质氛围与光照基调"))
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
        # ⭐ 2026-10-19（A 方案，对齐 H3信号10段测试001 模板）：剧本中文字段
        #    （description / first_frame / last_frame / motion / emotion）→ 模板式
        #    英文叙事（LLM 翻译 + 指纹缓存；失败回落中文原文）。10段模板是全英文，
        #    旧实现把中文原值直塞英文句，稀释 H3 的镜头控制力。
        try:
            import h3_en_text as _h3en
            shot = _h3en.englishize_shot(shot)
        except Exception as _te:  # noqa: BLE001 - 英文化失败绝不影响出片
            logger.debug("[H3-EN] 英文化失败（用中文原文）：%s", _te)
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
        # ⭐ 2026-10-19（A 方案，对齐 H3信号10段测试001 模板）：剧本中文字段
        #    （description / first_frame / last_frame / motion / emotion）→ 模板式
        #    英文叙事（LLM 翻译 + 指纹缓存；失败回落中文原文）。10段模板是全英文，
        #    旧实现把中文原值直塞英文句，稀释 H3 的镜头控制力。
        try:
            import h3_en_text as _h3en
            shot = _h3en.englishize_shot(shot)
        except Exception as _te:  # noqa: BLE001 - 英文化失败绝不影响出片
            logger.debug("[H3-EN] 英文化失败（用中文原文）：%s", _te)
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


