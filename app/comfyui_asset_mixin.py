# -*- coding: utf-8 -*-
"""ComfyUIClient 的「_AssetMixin」方法（D-3 拆类，2026-10-16）。

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
    ANNOTATED_DIR, SCENE_NO_CHARACTER_SUFFIX, SCENE_RELATION_REF_HINT, scene_view_prompt_suffix,
)

logger = logging.getLogger(__name__)


class _AssetMixin:
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
        # ⚠️ 2026-10-14 新增【否定短语剥离】——所有资产类型（item/character/scene）都要过。
        #    实测：物品「筑基丹」正向提示词含「边缘圆润无棱角」「构图稳定，无畸形」，
        #    出图在画面正中画了一个**大号汉字「无」**（三个物品资产全部中招）。
        #    扩散模型不擅长理解否定语义，会把「无」当成画面内容。
        #    放在这里是因为 `_generate_base_image` 是**所有资产基础图的唯一公共入口**
        #    （generate_item_base / generate_character_base / generate_scene_base 都走它），
        #    这样不必在三个调用方各改一遍、也不会漏掉任何一条链路。
        prompt_zh = self.strip_negation_phrases(prompt_zh)
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
        files = self.get_output_files(history, ".png", filename_prefix)
        if _fp and files:
            try:
                asset_library.store(_fp, files, meta={
                    "template": os.path.basename(str(workflow_file)),
                    "style": style, "size": list(size) if size else None,
                    "type": str(asset_type)})
            except Exception as e:                   # noqa: BLE001
                logger.warning("角色资产入库失败（不影响出图）：%s", e)
        return files


    def generate_character_base(self, prompt_zh: str, seed: int = None,
                                style: str = "", size=None,
                                filename_prefix: str = None,
                                crowd_names=None, tier: str = "") -> List[str]:
        """生成角色基础图。按**四档成本分级**决定出图形态。

        2026-10-12 用户指定（判据见 ``asset_worker._character_tier``）：
          primary    → 四区 character sheet（正/侧/背 + 面部特写 + 配色板 + 细节/比例）
          supporting → 双格设定图（正面 + 侧面）
          minor      → 单张正面全身立绘
          crowd      → 多人合影（``crowd_names`` 给出同批群像名单）
        ``tier`` 为空 / unknown 时一律走 primary 的四区版式 —— 读不到剧本时按旧行为，
        宁可多出信息，也不要用一个猜出来的档位把角色画残。
        """
        logger.info(f"生成角色基础图: {prompt_zh[:50]}...")
        _tier = str(tier or "").strip().lower()
        if crowd_names:
            # 群像路径：一张多人合影，画风天然统一（同图生成，光影色调不会各画各的）。
            # ⚠️ 与四区三视图互斥：合影里没有「正/侧/背」分区，下游也不做切分。
            logger.info("角色基础图走【群像合影】版式（%d 人）：%s",
                        len(list(crowd_names)), "、".join(list(crowd_names))[:80])
            prompt_zh = self._ensure_crowd_prompt(prompt_zh, style, names=crowd_names)
        elif _tier == "minor":
            # 次要角色：单张正面全身立绘（省掉正/侧/背多格与设定集版面）。
            logger.info("角色基础图走【单张正面立绘】版式（次要角色）")
            prompt_zh = self._ensure_solo_prompt(prompt_zh, style)
        elif _tier == "supporting":
            # 主要配角：双格（正面 + 侧面）。够他单独出镜的形体一致性，但省掉另三区。
            logger.info("角色基础图走【双格正侧设定图】版式（主要配角）")
            prompt_zh = self._ensure_pair_prompt(prompt_zh, style)
        else:
            # primary / unknown：角色基础图 = 设定集（三视图横排）。必须确定性注入
            # 「全身 + 横排三视图」版式硬约束：剧本层提示词只写外貌特征、不含构图约束
            # （风格词也由程序追加，版式同属构图维度），不注入则模型默认半身/胸像构图
            # 且三格版式不可控，多视角与分镜一致性都会崩坏
            # （2026-09-19 实测：三视图出成半身，且同图内人物身高比例不一致）。
            prompt_zh = self._ensure_fullbody_prompt(prompt_zh, style)
        # ⭐ 2026-10-06（用户指定）：角色未明说国籍/人种时，**一律默认中国人（亚洲面孔）**。
        # 扩散模型在"没写国籍"时会按自身偏置默认出欧美面孔；这里确定性补默认约束，
        # 仅当提示词未出现「外国/西方/欧美/黑人/白人/混血」等明说非华人词时才追加，避免误伤。
        prompt_zh = self._ensure_cn_default_ethnicity(prompt_zh)
        return self._generate_base_image(WORKFLOW_TEMPLATE["character_gen"], prompt_zh,
                                         asset_type="character", seed=seed, style=style, size=size,
                                         filename_prefix=filename_prefix)


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
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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
        files = self.get_output_files(history, ".png", filename_prefix)
        return files[0] if files else None


