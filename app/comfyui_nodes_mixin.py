# -*- coding: utf-8 -*-
"""ComfyUIClient 的「_NodesMixin」方法（D-3 拆类，2026-10-16）。

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
    COMBINED_PROMPT_NODE_TYPES, PROMPT_ENHANCER_CLASS, PROMPT_ENHANCER_IMAGE_FIELDS, PROMPT_ENHANCER_LOCAL_DEFAULTS, PROMPT_ENHANCER_MODE_I2I, PROMPT_ENHANCER_MODE_T2I, PROMPT_ENHANCER_PROMPT_FIELD, PROMPT_NODE_TYPES, PROMPT_TEXT_FIELDS, SCENE_EXACT_TEXT_MARKER, _IMAGE_SLOT_RE, _enhancer_text_credentials, _is_dynamic_child, _iter_prompt_slots, _node_sort_key, _prompt_enhancer_node_enabled, _prompt_slot_polarity, _slot_index,
)

logger = logging.getLogger(__name__)


class _NodesMixin:
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
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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


