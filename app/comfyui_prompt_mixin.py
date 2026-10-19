# -*- coding: utf-8 -*-
"""ComfyUIClient 的「_PromptMixin」方法（D-3 拆类，2026-10-16）。

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
    BLOCKING_REF_MARK, CHARACTER_QTY_RE, CHARACTER_WORDS, CINEMATOGRAPHY_SECTION, COMPOSITION_BASELINE_SECTION, CONFLICT_NEGATIVE_SORTED, IDENTITY_GRID_REF_MARK, SCENE_EXACT_TEXT_MARKER, SCENE_NO_CHARACTER_SUFFIX, SHOT_ACTION_SUFFIX, _CAMERA_ANGLE_SPECS, _PROMPT_SPLIT_RE, _SCENE_GRID_MAIN_FALLBACK, _crowd_member_descriptions, _dlg_speaker, _dlg_text, _iter_prompt_slots, _parse_identity_grid_layout, _ref_label_body, _ref_label_purpose, _strip_sheet_phrases, camera_angle, camera_spec, identity_baseline_grid_section, shot_framing,
)

logger = logging.getLogger(__name__)


class _PromptMixin:
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
    def strip_negation_phrases(cls, prompt_zh: str) -> str:
        """删掉提示词里的「无X / 没有X」否定短语（幂等），避免图像模型把否定词画成字。

        只删**否定短语本身**，保留该分句的正向内容：
            「边缘圆润无棱角」    → 「边缘圆润」      （信息未丢：正向描述已说清）
            「构图稳定，无畸形」  → 「构图稳定」

        ⚠️ 为什么整段删而不改写：图像模型看的是「画面里有什么」；把「不要什么」改写成
        「要什么」需要图像语义知识、容易改错。而留下的正向描述本来就已完整表达了该特征
        （说了「边缘圆润」就不必再说「无棱角」）。
        """
        text = (prompt_zh or "").strip()
        if not text or not cls._NEG_PHRASE_RE.search(text):
            return text
        out = cls._NEG_PHRASE_RE.sub("", text)
        out = re.sub(r"[，,]{2,}", "，", out)
        out = re.sub(r"[，,、]\s*([。;；])", r"\1", out)
        out = re.sub(r"\s{2,}", " ", out).strip().strip("，,、").strip()
        # ⚠️ 过度删除保护：若净化后**不足原文 40% 且少于 12 字**，说明原文几乎全是否定句
        #    （如「无文字、无水印、无logo的画面」），净化会把有效信息也带走 —— 此时回退原文。
        #    宁可让模型画出「无」，也不能把资产描述清空（后者会直接毁掉出图）。
        if len(out) < 12 and len(out) < len(text) * 0.4:
            return text
        if out != text:
            logger.info("已剥离提示词里的否定短语（防模型画出「无」字）：%s → %s",
                        text[:60], out[:60])
        return out or text          # 全被删空则回退原文（绝不返回空）


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
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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


    @staticmethod
    def _ensure_pair_prompt(prompt_zh: str, style: str = "") -> str:
        """给**主要配角**补「双格设定图（正面 + 侧面）」版式（幂等）。

        四档分级见 asset_worker._character_tier 的 docstring：
          primary → 四区 sheet / supporting → **本函数据此出双格** / minor → 单图 / crowd → 合影。
        """
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        text = str(prompt_zh or "").strip()
        marker = "角色设定双视图"
        if marker in text or "人物设定表" in text or "character sheet" in text:
            return text
        base = style_kit.with_reference_style(text, style) if style else text
        if marker in base or "人物设定表" in base or "character sheet" in base:
            return base
        labels = ComfyUIClient._extract_trait_labels(text)
        labels_part = ("，以及该角色特征标签：" + "、".join(labels)) if labels else ""
        suffix = ComfyUIClient._PAIR_SHEET_CN_LAYOUT.replace("{labels_part}", labels_part)
        base = base.rstrip("。，,.;； ")
        sep = "" if base.endswith((".", "！", "?", "？")) else "。"
        return (base + sep + suffix) if base else suffix


    @staticmethod
    def _ensure_solo_prompt(prompt_zh: str, style: str = "") -> str:
        """给**次要角色**补「单张正面全身立绘」版式（幂等）。

        ⚠️ 为什么次要角色也**不**从合影里裁：用户提的前提 2 —— 只要他有哪怕一次独立
        出镜，从合影裁出来的脸在单独镜头里就会走样。所以给他一张自己的单图，
        只是不给「正面/侧面/面部特写/配色板」那么全的版面。
        """
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        text = str(prompt_zh or "").strip()
        marker = "单人全身立绘"
        if marker in text or "人物设定表" in text or "character sheet" in text:
            return text
        # ⚠️ 2026-10-14：**先剥离**基础提示词里的「三视图横排」版面句，
        #    否则它与下面的「单人全身立绘」自相矛盾，模型会听前面那句画成三视图。
        base = _strip_sheet_phrases(style_kit.with_reference_style(text, style) if style else text)
        if marker in base or "人物设定表" in base or "character sheet" in base:
            return base
        labels = ComfyUIClient._extract_trait_labels(text)
        labels_part = ("（" + "、".join(labels) + "）") if labels else ""
        suffix = ComfyUIClient._SOLO_CN_LAYOUT.replace("{labels_part}", labels_part)
        base = base.rstrip("。，,.;； ")
        sep = "" if base.endswith((".", "！", "?", "？")) else "。"
        return (base + sep + suffix) if base else suffix


    @staticmethod
    def _ensure_crowd_prompt(prompt_zh: str, style: str = "", names=None) -> str:
        """给**群像角色**的参考图提示词补「多人合影」版式约束（幂等）。

        用户口径见上方 _CROWD_SHEET_CN_LAYOUT 注释。与 _ensure_fullbody_prompt 的区别：
          四区三视图 → 单人、正/侧/背、供后续单人特写复用；
          群像合影   → 多人同框、画风锁定、供群像镜头复用。

        为什么要显式列出名字：画面里要有中文姓名标注，且下游靠这张图做群像镜头的
        参考 —— 只写「一群人」会让模型自由发挥，无法保证包含剧本里那些角色。

        幂等 marker = "群像合影"。
        """
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        text = str(prompt_zh or "").strip()
        marker = "群像合影"
        if marker in text or "人物群像设定图" in text:
            return text
        # ⚠️ 2026-10-14：**先剥离**基础提示词里的「三视图横排」版面句，
        #    否则它与下面的「群像合影」自相矛盾 —— 实测模型会画成
        #    「同一个人的多角度」（而不是多个不同的人并排）。
        base = _strip_sheet_phrases(style_kit.with_reference_style(text, style) if style else text)
        if marker in base or "人物群像设定图" in base:
            return base
        _names = [str(n).strip() for n in (names or []) if str(n).strip()]
        # ⚠️ 2026-10-14 修正（用户报「群像也只生成了一个人，而且群像没必要出三视图」）：
        #    名单里的名字可能是**集合名词**（外门弟子群像 / 外门围观弟子 / 众门人…），
        #    它本身代表一群人、而不是一个人。旧实现直接拿 len(_names) 当人数 ——
        #    名单只有 1 个集合名词时 count=1，版式便要求「并列展示 1 位人物」，
        #    模型照做画出一个人（实测日志：「走【群像合影】版式（1 人）」）。
        #    现在：集合名词按语义折算成多人，并把「这是一个群体」明写进版式，
        #    免得模型把集合名词当成人名去画单个角色。
        _GROUP_WORDS = ("群像", "群演", "众", "一群", "众人", "群众", "人群",
                        "们）", "弟子群", "围观")
        _is_group = bool(_names) and all(any(w in n for w in _GROUP_WORDS) for n in _names)
        if _is_group:
            # 集合名词：给合理的中等人数（看得出是「一群人」，又不至于糊成一片）
            _count = max(4, min(8, 3 + 2 * len(_names)))
            # ⚠️ 2026-10-14（用户报「还是一个人」并给出期望模板）：
            #    只给人数不够 —— 名单里往往只有一个集合名词，模型只拿到一份描述，
            #    于是把同一个人复制 N 遍。这里按用户模板**逐位给出不同的成员描述**：
            #    「群像合影，{N}个人物并排站立，从左到右依次是：{角色1描述}、{角色2描述}…」
            names_part = ("、".join(_names) +
                          "（下面的名字是**群体**，请画出该群体里的多名不同成员）。" +
                          _crowd_member_descriptions(_count))
        else:
            _count = max(1, len(_names))
            names_part = "、".join(_names) if _names else "剧本中出场的诸位群像人物"
        suffix = (ComfyUIClient._CROWD_SHEET_CN_LAYOUT
                  .replace("{count}", str(_count))
                  .replace("{names_part}", names_part))
        base = base.rstrip("。，,.;； ")
        sep = "" if base.endswith((".", "！", "?", "？")) else "。"
        return (base + sep + suffix) if base else suffix


    @staticmethod
    def _extract_trait_labels(prompt_zh: str, limit: int = 12) -> List[str]:
        """从角色设定文本提取**特征标签**（设定图上要渲染的中文说明词）。

        口径：按中英文标点切短句 → 剥掉「穿着/佩戴」等动词前缀 → 丢弃空段/
        超长短句/停用词命中 → 去重保序。
        例：「男性，冰蓝色眼眸，黑发微乱，穿着哑光黑色短款外套，低帮战术靴」→
        ［男性, 冰蓝色眼眸, 黑发微乱, 哑光黑色短款外套, 低帮战术靴］
        标签**只来自设定原文**（不臆造），上限 limit 条防版式过挤。
        """
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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


    @staticmethod
    def _strip_item_placement(prompt_zh: str) -> str:
        """剥掉物品提示词里「置于/放在 X 中」这类**位置与承托物**描述（幂等）。

        只删「放在哪儿」这半句，物品本体（造型/材质/颜色/纹样/尺寸）一字不动。
        """
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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
    def _dehand_item_prompt(prompt_zh: str) -> str:
        """中和物品提示词里**诱导画出手**的尺寸/持握表述（幂等，2026-10-16 补）。

        实测（真实死循环，用户报「一直在重新生成过不了」）：`监控灵石` 的
        ``reference_prompt_zh`` = 「**掌心大小**青色灵石…下端为暗色石质**握座**…」——
        「掌心 / 握座」让扩散模型照字面画出一只**托着灵石的手**；而物品参考图的
        铁律是「只呈现本体，出现手/人形生物/承托物即关键缺陷」（见
        :meth:`_ensure_item_white_bg` 的 body_rule 与 asset_worker 的物品专项判据）
        → 判关键缺陷 → 重画 → 还是手 → 连续 17 次不过（换种子救不回）。

        ⚠️ 只靠 body_rule 的负向句「不要手」拦不住：尺寸词「掌心大小」写在**最前**、
        权重最高，模型优先照字面出图。故必须**确定性改写**这些词 —— 它们对本体外观
        无信息量（只是尺寸/持握类比），换等价说法不影响设定：
          · 掌心大小 / 手掌大小 → 小巧
          · 握座 → 底座
          · 「握于掌中 / 握在手中 / 执于手中 / 托在掌心」等持握从句 → 删除（保留本体）
        幂等：已改写的文本再次调用不再变化。
        """
        t = str(prompt_zh or "")
        for bad, good in (("掌心大小", "小巧"), ("手掌大小", "小巧"), ("掌心般大", "小巧"),
                          ("握座", "底座"),
                          ("握于掌中", ""), ("握于手中", ""), ("握在手中", ""),
                          ("执于手中", ""), ("托在掌心", ""), ("托于掌心", "")):
            t = t.replace(bad, good)
        # 收尾：清理删除/替换后可能产生的重复或悬空标点
        t = (t.replace("，，", "，").replace("的，", "，").replace("座，", "座，")
              .replace("，。", "。").replace("；。", "。").replace("；，", "，"))
        return t

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
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        text = ComfyUIClient._dehand_item_prompt(
            ComfyUIClient._strip_item_placement(str(prompt_zh or "").strip()))
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
        # ⭐⭐ 2026-10-10 与场景侧同一缺陷（用户实测「不要直接写原文啊」）：
        #   原句字面写着「物品表面居中印有黑色粗体字 “原文”」—— 模型把「原文」当
        #   要写的字照抄，物品表面就会印出占位词。修法与场景侧完全一致：
        #   ① 删除字面占位；② 有确切字词才逐字照抄；③ 没给就写合理的常见字词或留白；
        #   ④ 显式禁止占位词被画进画面。
        text_render_rule = ("，若物品表面需呈现文字（书名/告示/卡片/标签/手写/打印/屏幕等）："
                            "若上文已给出该处的确切字词，必须逐字照抄（不得改写、不得增加）；"
                            "若上文没有给出，则按该物品的实际用途写一个**合理的常见字词**"
                            "（例如员工证/姓名/编号/请勿转借），或直接不写文字；"
                            "文字简短（不超过 8 个字）、字体清晰、笔画完整、高对比、横排，"
                            "排版规整，字形与常用印刷体一致；"
                            "**严禁把「原文」「文字」「字样」「某某」「XX」「待填」「TBD」"
                            "这类占位词当成要写的字画进画面**；未指出的文字一律留白不写")
        if marker in base:
            out = base if "只呈现该物品本体" in base else base + body_rule
        else:
            out = base.rstrip("。，,.;； ")
            out = out + body_rule + "，纯白背景，无任何场景、地面、桌面、阴影与背景纹理，" \
                        "物品完整孤立居中、边缘清晰" if out else body_rule.lstrip("，") + "，纯白背景，" \
                        "无任何场景、地面、桌面、阴影与背景纹理，物品完整孤立居中、边缘清晰"
        # ⭐ 2026-10-19（用户 P1：「筑基丹画成带底座青绿晶石，像玉玺不像丹药」）：
        #    丹/丸/药类物品必须画成**圆形药丸** —— 扩散模型会把「丹」画成晶体/玉石/
        #    印章摆件。幂等标记「丹药类物品」；丹炉/丹房/药柜等**容器/场所类**词排除。
        _is_pill = any(t in out for t in ("丹", "药丸", "丹药", "灵药", "丸药")) and \
            not any(t in out for t in ("丹炉", "丹房", "药柜", "丹药瓶", "药匣"))
        if _is_pill and "丹药类物品" not in out:
            out = out.rstrip("。，,.;； ") + (
                "，丹药类物品：画面主体是一颗圆形或椭圆形的药丸"
                "（温润药体质感、表面有细密药纹或微微光晕），"
                "不得画成晶石、矿石、玉石、翡翠摆件或印章/玉玺，不得带底座、托座、立座")
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
        # ⭐⭐ 2026-10-10 用户实测缺陷（严重）：「小说没有写牌匾上的内容你就自己生成一个，
        #    不要直接写原文啊」—— 便利店招牌上真的印出了「原文」两个字。
        # 根因：本条提示词**字面**写着「该处居中印有黑色粗体字 “原文”」——
        #    本意是「把**确切的原文**写在这里」，但扩散模型把引号里的「原文」当成
        #    要写的字照抄，于是画面里出现了占位词。这是**提示词自己制造**的缺陷。
        # 修法：① 删掉一切会被照抄的字面占位（“原文”/“文字”/“某某”）；
        #       ② 明确「上文给了确切字词就逐字照抄」；
        #       ③ 明确「没给就按场景语义写一个**合理的常见招牌字**（如 便利店/超市/
        #          药店/餐厅），或直接留白」——用户口径是「自己生成一个」，不是写占位词；
        #       ④ 显式列入**禁止出现的占位词**，把模型可能照抄的路径全部堵掉。
        rule = ("，若画面中出现文字（告示/标识/招牌/守则/铭牌/标语/屏幕等）："
                "若上文已给出该处的确切字词，必须逐字照抄（不得改写、不得增加）；"
                "若上文没有给出，则按该处的实际用途写一个**合理的常见招牌字词**"
                "（例如便利店/超市/药店/餐厅/营业中/出口），或直接不写文字；"
                "文字简短（不超过 8 个字）、字体清晰、笔画完整、高对比、横排，"
                "排版规整，字形与常用印刷体一致；"
                "**严禁把「原文」「文字」「字样」「某某」「XX」「待填」「TBD」"
                "这类占位词当成要写的字画进画面**；未指出的文字一律留白不写")
        return text.rstrip("。，,.;； ") + rule


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
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
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
    def _clarify_ink(text: str) -> str:
        """澄清「红色笔迹 ≠ 血迹」（见上方说明）。仅命中关键词时改写。"""
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        t = str(text or "")
        for _a, _b in ComfyUIClient._INK_NOT_BLOOD:
            if _a in t:
                t = t.replace(_a, _b)
                break
        return t


    @staticmethod
    def _strip_text_meta(text: str) -> str:
        """把画面描述里的「文字元描述词」换成不可绘制的说法（见上方说明）。"""
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        t = str(text or "")
        for _a, _b in ComfyUIClient._TEXT_META_DESC:
            t = t.replace(_a, _b)
        return t


    @staticmethod
    def build_storyboard_prompt_compact(shot: dict, ref_labels: List[str] = None,
                                        has_blocking_image: bool = False,
                                        has_characters: bool = True) -> str:
        """紧凑自然语言版分镜提示词（2026-10-18，默认启用）。

        动机（用户实测「生成的什么鬼」+ 官方 Qwen-Image 规范）：旧版是约 2.5k token 的
        分节指令墙（TASK/UNIQUENESS/ORIENTATION/VISUAL BASE/IDENTITY/ROLES/SCENE/
        LIGHTING/CINEMATOGRAPHY/STYLE/PRESERVE），各段互相稀释，还夹带互相矛盾的
        条目（例：ORIENTATION 段给**每个**角色都写 "pointing"，与「只画开头瞬间」冲突；
        实测 shot_01 因此把「羡进」画进画面、景别也从全景漂成中景）。官方口径是
        **自然语言、每张参考图职责唯一、一条主线指令**。本函数只保留四要素：
        景别 / 谁在画面里（人数·位置·朝向·<imageN> 绑定）/ 开头瞬间 / 风格与禁令；
        段名与 prompt_qc 契约（TASK/FRAMING/SCENE AND ACTION/STYLE/PRESERVE）保持兼容。
        旧实现保留在 build_storyboard_prompt（``MJSCXT_SB_PROMPT=legacy`` 回退）。
        """
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        shot = dict(shot or {})
        camera = str(shot.get("camera") or "中景").strip()
        cam_key = shot_framing(shot)
        cam_angle = camera_angle(camera)
        cam_spec = camera_spec(cam_key or camera)

        lines = ["TASK: Generate ONE storyboard frame — a single still image of the opening "
                 "instant of this shot."]
        if cam_key:
            lines.append(f"FRAMING (must be strictly followed): {cam_key} — {cam_spec}.")
        else:
            lines.append("FRAMING (not specified for this shot): follow the framing implied "
                         "by the scene description; do NOT default to a medium or wide shot.")
        # ⭐ 2026-10-20（用户实测镜 14 连续 3 次被拒「景别不符：差两档」）：近景系会被
        #    三样东西往下拉 —— ① 角色参考图是**全身立绘**，模型倾向照抄全身构图；
        #    ② first_frame 常写「立于石阶高处」这类站位；③ 场景描述里点名「白石台阶」。
        #    三者叠加 → 画成中远全景 + 大片地面。故追加一条**独立成句的取景边界硬约束**，
        #    不与 spec 合并（合并进长句会被稀释，正是 45 条景别教训的成因）。
        if cam_key and any(_k in cam_key for _k in ("特写", "近景")):
            lines.append(
                "CROP BOUNDARY (hard constraint — overrides every other instruction): "
                "this frame is a %s crop, so the subject is CUT OFF by the frame edge, "
                "never scaled down to fit the whole body. Show ONLY the body region "
                "above the waist (or tighter). The bottom edge of the image MUST cut "
                "through the subject's body. Do NOT draw legs, feet, shoes, skirt hem, "
                "floor, ground, stone steps, stairs or paving stones anywhere in the "
                "frame. The person must fill most of the frame height." % cam_key)
            lines.append(
                "REFERENCE SHEETS: the character references are FULL-BODY turnarounds — "
                "take identity, costume and colours from them, but NEVER inherit their "
                "full-body framing or standing pose.")
        if cam_angle:
            lines.append(f"CAMERA ANGLE (must be strictly followed): {cam_angle} — "
                         f"{_CAMERA_ANGLE_SPECS[cam_angle]}.")
        if has_blocking_image:
            lines.append(
                "PRIMARY CANVAS: Use <image1> as the primary canvas — it is a 3D blocking "
                "reference: copy ONLY its framing and spatial layout, never its mannequin "
                "appearance.")
        else:
            lines.append(
                "PRIMARY CANVAS: none — this is a NEWLY rendered frame. Every reference "
                "image below is an identity / material donor only — never the canvas, never "
                "a layout template; do not return, trace or lightly edit any reference image.")

        # ---------- 参考图职责（<imageN> 逐张绑定，职责唯一）----------
        _pos_name: Dict[int, str] = {}
        _roles: List[str] = []
        _scene_pos = 0
        for pos, raw in enumerate(ref_labels or [], start=1):
            lab = _ref_label_body(raw, pos)
            if not lab:
                continue
            if BLOCKING_REF_MARK in lab or IDENTITY_GRID_REF_MARK in lab:
                continue
            _m = re.search(r"角色「([^」]+)」", lab)
            if _m:
                nm = _m.group(1).strip()
                _pos_name[pos] = nm
                _roles.append(
                    f"<image{pos}> = 「{nm}」: preserve the exact identity from <image{pos}> "
                    f"(hairstyle, clothing, facial features); do NOT copy its sheet layout "
                    f"or framing.")
                continue
            _mi = re.search(r"物品「([^」]+)」", lab)
            if _mi:
                _roles.append(
                    f"<image{pos}> = prop「{_mi.group(1).strip()}」: keep its shape, material "
                    f"and colour; do not copy its layout.")
                continue
            _scene_pos = pos
            _roles.append(
                f"<image{pos}>: background / environment reference only "
                f"({_ref_label_purpose(lab) or 'scene'}); match its space, lighting, props "
                f"and viewing angle — do not copy any people from it.")
        if _roles:
            lines.append("REFERENCE ROLES: " + " ".join(_roles))

        # ---------- 主体清单（谁在画面里 × 几个人 × 在哪 × 朝哪）----------
        _FACING_EN = {
            "camera": "facing the camera",
            "left": "facing screen-LEFT",
            "right": "facing screen-RIGHT",
            "back": "seen from behind, back to camera",
        }
        _POS_XY = {"left": "on the left", "center": "in the center", "right": "on the right"}
        _POS_Z = {"front": "closer to the camera", "mid": "in the middle ground",
                  "back": "further back"}

        def _is_group(nm: str) -> bool:
            try:
                import character_tier
                return bool(character_tier.is_crowd_name(nm))
            except Exception:  # noqa: BLE001
                return any(t in nm for t in ("群像", "众", "们", "人群", "弟子群", "围观"))

        _blk = {}
        for _b in (shot.get("blocking") or []):
            if isinstance(_b, dict) and str(_b.get("name") or "").strip():
                _blk[str(_b["name"]).strip()] = _b
        _chars = [str(x).strip() for x in (shot.get("characters_in_shot") or [])
                  if str(x).strip()]
        _subjects: List[str] = []
        _n_people = 0
        _has_group = False
        for nm in _chars[:4]:
            pos = next((p for p, n in _pos_name.items() if n == nm), 0)
            b = _blk.get(nm) or {}
            seg = f"「{nm}」" + (f" (<image{pos}>)" if pos else "")
            if _is_group(nm):
                # 群体角色：剧本有时只写「一名弟子」、有时写「众人」——不锁人数，
                # 按本镜画面需要画一名或多名，但必须是**不同的脸**。
                _has_group = True
                seg += (" stands for the disciples as a GROUP — draw one or several "
                        "disciples from this group as the scene needs, each a DIFFERENT "
                        "person (never one face repeated).")
            else:
                _n_people += 1
                bits = [_POS_XY.get(str(b.get("x") or "").strip().lower(), ""),
                        _POS_Z.get(str(b.get("depth") or "").strip().lower(), ""),
                        _FACING_EN.get(str(b.get("facing") or "").strip().lower(), "")]
                bits = [x for x in bits if x]
                seg += (" stands " + ", ".join(bits) + ".") if bits else " stands in frame."
            _subjects.append(seg)

        _scene_bits = []
        if shot.get("location"):
            _scene_bits.append(str(shot["location"]).strip())
        # ⭐ 2026-10-20（用户实测「场景缺失：背景为整片纯白空白」）：追加场景的**视觉描述**
        #    （scenes[].appearance）。此前这里只有 location 那六个字的地名，模型根本不知道
        #    「青云宗外门广场」长什么样 → 画成纯白空背景 → 质检硬阻断（5/14 镜报废）。
        #    数据由 storyboard_helpers._with_scene_appearance 预先注入 shot。
        _sa = str(shot.get("scene_appearance") or "").strip()
        if _sa:
            _scene_bits.append(_sa)
        if _scene_pos:
            _scene_bits.append(f"environment per <image{_scene_pos}>")
        _light = ComfyUIClient._extract_light_hint(shot)
        if _light:
            _scene_bits.append(str(_light).strip())

        _ff = str(shot.get("first_frame") or "").strip()
        _para: List[str] = []
        if _scene_bits:
            _para.append("Scene: " + "; ".join(_scene_bits) + ".")
        _ff0 = str(shot.get("first_frame") or "").strip()
        _desc0 = str(shot.get("description") or "")
        # 开场画面若提到"两侧弟子/众人"等**名单外背景人物**：允许出现在背景里，其余情况
        # 一律禁止加人。否则要么漏画（实测 shot_09 两侧空无一人），要么把背景人画成主角。
        _extras_ok = (not any(_is_group(c) for c in _chars)) and any(
            t in (_ff0 + _desc0) for t in ("两侧", "弟子", "众人", "围观", "群人", "人群"))
        _tail_people = (" Background bystanders may appear ONLY where the opening frame "
                        "mentions them — keep them secondary and unnamed."
                        if _extras_ok else "")
        # ⭐ 2026-10-18（借鉴同类项目「提示词信噪比配额：必须满足≤4/避免≤3」）：把
        #    「站位精确」「每人只出现一次」「不多画人」「不许排排站」四条合并成**一句**——
        #    分句越多越互相稀释，模型反而只抓住其中一条。
        _people_rule = (" Place exactly these people at the stated sides/depths, each facing "
                        "the stated direction; each individual appears only once (no clones, "
                        "no duplicate bodies)"
                        + ("; no unrelated people beyond the opening frame's background "
                           "bystanders" if _extras_ok else "; no other people")
                        + "; never line the characters up facing the camera.")
        # ⚠️ 2026-10-18（局部镜治本修复）：detail/insert 镜只拍手部/道具，不得出现人脸。
        #   原先这里无条件写 "Exactly N person(s) in the frame — [角色…] stands…"，
        #   模型据此把「局部特写」画成带完整人脸的中近景 → 质检判「景别不符」→
        #   换多少种子都是同一结果（实测镜 13 连试 9 次全败）。局部镜改为
        #   「只入画肢体」的措辞，并跳过站位 / 人数 / 每人一次那套整身规则。
        _detail_cut = False
        try:
            import shot_camera as _sc
            _detail_cut = _sc.shot_framing(shot) == "局部"
        except Exception:  # noqa: BLE001
            _detail_cut = False
        if has_characters and _subjects and _detail_cut:
            _para.append(
                "BODY PARTS ONLY — these people appear in frame **only as body fragments**: "
                "hands, fingers, forearms, sleeve hems and the gesture holding the prop. "
                "**Do NOT draw any face, head, hairline, jaw, shoulder line, chest, "
                "full body or full-body silhouette.** Do NOT place anyone standing in the "
                "scene; this frame is a detail / insert close-up of hands and props, "
                "with the environment reduced to light direction, shadow and soft colour "
                "blocks only. Nobody may appear twice. " + " ".join(_subjects))
        elif has_characters and _subjects:
            _lead = ("People in the frame — " if _has_group
                     else "Exactly %d person(s) in the frame — " % _n_people)
            _para.append(_lead + " ".join(_subjects) + _tail_people + _people_rule)
        elif not has_characters:
            _para.append("No people in the frame at all — environment, objects and props only. "
                         "Do NOT draw any human figure, face or body.")
        if _ff:
            _para.append("Opening instant (draw exactly this static moment — the action has "
                         "NOT happened yet): " + _ff + ".")
        else:
            _desc = str(shot.get("description") or "").strip()
            if _desc:
                _para.append("Frame content: " + _desc + ".")
        if shot.get("emotion"):
            _para.append("Mood: %s." % str(shot["emotion"]).strip())
        if _dlg_text(shot.get("dialogue")) and has_characters:
            _para.append("%s is speaking one short line — only natural mouth movement and "
                         "subtle expression." % (_dlg_speaker(shot.get("dialogue")) or "The character"))
        # （「每人只出现一次 / 不多画人 / 站位不许排排站」已合并进上方一句，见 _people_rule）
        lines.append("SCENE AND ACTION:\n" + " ".join(_para))

        # ---------- STYLE + PRESERVE ----------
        _style_line = ""
        try:
            _st = style_kit.normalize_style(shot.get("style"))
            if _st and hasattr(style_kit, "style_suffix_en"):
                _style_line = style_kit.style_suffix_en(_st) or ""
        except Exception as _se:  # noqa: BLE001 - 风格解析失败回落空（旧行为）
            logger.debug("[分镜提示词] 风格解析失败（忽略）：%s", _se)
        lines.append("STYLE: " + ((_style_line + ". ") if _style_line else "")
                     + "Stylized hand-drawn / CG render of the given art style — NOT "
                       "live-action photography, not photo-real human skin.")
        lines.append("PRESERVE: Keep all untargeted content unchanged. The image must not "
                     "contain any text, subtitles, captions, logos or watermarks.")
        return "\n\n".join(lines)

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
        # ⭐ 2026-10-11：清掉画面描述里的「文字元描述词」（乱码 → 不可辨认的字符）。
        #   用浅拷贝，避免污染调用方持有的 shot（它还要用于质检/落盘）。
        from comfyui_client import ComfyUIClient  # D-3 拆类：惰性导入破环
        shot = dict(shot or {})
        # ⭐ 2026-10-11 系统排查补全：分镜提示词实际会用 19 个 shot 字段，其中带
        #   「文字/帧/动作」语义的有 6 个 —— description / visual_detail / first_frame /
        #   last_frame / action / surface_text。此前只清了前两个，而 first_frame、
        #   last_frame（首尾帧描述）和 action 同样会被**逐字写进提示词**，
        #   所以「乱码」「小字」「符号」这类元描述词从这几条路照样能漏进去。
        #   现统一按同一套清洗（元描述词 + 墨迹澄清）处理。
        #   ⚠️ surface_text 不清洗：它是**要求模型照抄的确切文字**（如「09:00」「2704」），
        #      清洗反而会破坏要写的字。
        for _fld in ("description", "visual_detail", "first_frame", "last_frame", "action"):
            _v = shot.get(_fld)
            if _v:
                shot[_fld] = ComfyUIClient._clarify_ink(ComfyUIClient._strip_text_meta(_v))
        # ⭐ 2026-10-18 重构：默认走**紧凑自然语言**提示词（官方口径，见
        #    build_storyboard_prompt_compact）。旧的分节长提示词实测互相稀释/矛盾
        #    （POSE 段给每个角色塞 pointing、与开头瞬间冲突 → 画错人、景别漂移）。
        #    回退：环境变量 MJSCXT_SB_PROMPT=legacy。
        if str(os.environ.get("MJSCXT_SB_PROMPT") or "").strip().lower() not in (
                "legacy", "v1", "old"):
            return ComfyUIClient.build_storyboard_prompt_compact(
                shot, ref_labels, has_blocking_image=has_blocking_image,
                has_characters=has_characters)
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
        # ---------- SUBJECT UNIQUENESS（2026-10-17 新增）----------
        # 实测失败镜（shot_8/14/20）共性：一格里把**同一个**主体画两遍 —— 2 个羡进 /
        # 2 个赵天霸 / 2 枚灵石；或把 A 画成 B（特征串味）。根因：角色参考图多为「多视角
        # 设定图」，模型把它的排布当画面结构、照着复制出多个同款主体。
        # 既有「REFERENCE LAYOUT IS NOT THE COMPOSITION」段（下方）已点到，但位置偏远、
        # 被画面内容稀释；这里在**提示词最前**补一条**强制**的唯一性约束，提高注意力权重。
        sections.append(
            "SUBJECT UNIQUENESS (must be strictly followed): each individual character "
            "and each prop appears EXACTLY ONCE in this frame. Never draw the same "
            "character twice — no second copy, no mirrored/reflected twin, no "
            "front-plus-back duplicate — and never duplicate a prop, no matter how many "
            "angles a character reference sheet shows. When several characters share the "
            "frame they are DIFFERENT people: keep each one's own face / hairstyle / "
            "costume, do not clone one character to fill another's place, and do not "
            "merge or swap features between them. A group/crowd character is drawn as "
            "several DISTINCT people, not as repeated copies of a single face.")
        # ---------- CHARACTER ORIENTATION & POSE（2026-10-17 新增，源头接线）----------
        # 剧本**早已**产出 shot.blocking[].facing（camera/left/right/back）+ shot.pose
        # （stand/walk/sit/…），但此前**只有 3D 构图基准图在消费**；3D 基准图一关，这些
        # 信息就完全没进分镜提示词 → 模型对「背影 / 朝向 / 走位」只能靠 description 猜。
        # 实测：shot_24 明明 facing=back（背对镜头）仍被画成正面朝镜头（42 次）。
        # 这里把它们翻成**简单直白的朝向/姿态指令**写进提示词，从源头把朝向钉死，并与
        # 「设定稿只供身份、不是构图模板」呼应（避免模型照着设定稿的正面画）。
        _FACING_EN = {
            "camera": "facing the camera (front view, face fully visible)",
            "left": "facing screen-LEFT (profile / three-quarter, away from camera)",
            "right": "facing screen-RIGHT (profile / three-quarter, away from camera)",
            "back": "seen FROM BEHIND — back to camera, moving away, the FACE MUST NOT be visible",
        }
        # ⚠️ 2026-10-18：**不再写 pose**（point/wave/kick…）。pose 是整镜动作 beat，
        #    而分镜图只画「开头瞬间」—— 旧实现给 blocking 里的**每个**角色都塞同一个 pose，
        #    实测 shot_01 里连不该动的「羡进」都被写成 pointing，模型照着画错人、画错动作。
        #    朝向（facing）是站位属性，保留；动作交给 SCENE AND ACTION 的开头瞬间描述。
        _orient_lines = []
        _blk_list = shot.get("blocking") if isinstance(shot.get("blocking"), list) else []
        for _bi in _blk_list:
            if not isinstance(_bi, dict):
                continue
            _parts = []
            _f = str(_bi.get("facing") or "").strip().lower()
            if _f in _FACING_EN:
                _parts.append(_FACING_EN[_f])
            if _parts:
                _nm = str(_bi.get("name") or "").strip()
                _orient_lines.append((f"「{_nm}」: " if _nm else "") + ", ".join(_parts) + ".")
        if _orient_lines:
            sections.append(
                "CHARACTER ORIENTATION (must be strictly followed — this OVERRIDES any "
                "reading of the reference sheets, whose front/side/back panels are identity "
                "references only, NOT the pose to draw): " + " ".join(_orient_lines))

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
            identity_grid_layout = None  # (cols, rows, cells) 动态布局（2026-10-13），None = 3×3
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
                    # 它的职责是「本镜出场角色的外观基准」，不进常规 IDENTITY/ROLES 句式。
                    # ⭐ 2026-10-13：布局可变（收尾镜单帧 → 1×3 / 2×2 / 2×3 …），从 label 解析。
                    identity_grid_pos = pos
                    identity_grid_layout = _parse_identity_grid_layout(lab)
                    continue
                # 身份锚点：官方句式 “Preserve the exact identity from <imageN>.”
                if "身份锚点" in lab:
                    # 官方 2.1 PE-I2I「Person into scene」标准配方（2026-10-15）：
                    #   "Keep hairstyle, clothing, and facial features identical."
                    # 实测差别很大：旧写法把 hairstyle 夹在 facial structure 后面当附属，
                    # 被同镜环境图（群像/群众）的「本场景男性发型统计印象」盖过 ——
                    # 赵天霸设定图明确是短寸黑发，成图却是中长蓬乱遮额，质检判官连续两轮报同一缺陷。
                    # 官方配方把 hairstyle **前置为首个列举项**，故照搬，并补一条硬锁定句。
                    identity_lines.append(
                        f"Keep hairstyle, clothing, and facial features of <image{pos}> "
                        f"identical ({_ref_label_purpose(lab)}). "
                        f"Hairstyle of <image{pos}> is LOCKED: keep its exact length, "
                        f"parting, and short/long tied/loose state — do NOT lengthen, "
                        f"add bangs, or restyle it, and do NOT take hairstyle cues from "
                        f"any other reference image.")
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
            # <image1> 是角色三视图设定稿（既不是身份网格画布、也不是 3D 基准/道具）时，
            # 需要额外的「禁止把设定稿当画布/构图模板」硬约束（见下方）。
            _img1_is_char_sheet = bool(identity_lines and not blocking_pos and not _ig_is_canvas)
            if _img1_is_char_sheet:
                # ⚠️ 关键（2026-10-17 二修）：<image1> 是角色三视图设定稿时**绝不能说**
                #    「use <image1> as the primary canvas」—— 那是"把这张图当底图/画布改"的
                #    指令；而本工作流（分镜生成_Qwen21）是**文生图 + 参考图条件**
                #    （KSampler 的 latent 来自 TextEncodeQwenImage21 的空 latent，不是 img2img），
                #    这句话只会诱导模型**原样返回设定稿**（实测反复：成品=三视图）。
                #    改成"生成全新画面，<image1> 只供身份"，消除这条自相矛盾的指令。
                body = ["THE OUTPUT IS A COMPLETELY NEW FRAME rendered from scratch. "
                        "<image1> is the character's identity reference ONLY — it is NOT the "
                        "canvas, NOT the picture to produce and NOT a layout template."]
            else:
                body = ["PRIMARY CANVAS: Use <image1> as the primary canvas."]
            if _ig_is_canvas:
                body[0] += (
                    " ⚠️ <image1> here is the APPEARANCE/ASPECT reference only — it is a "
                    "contact sheet of the character's canonical baseline views, NOT the "
                    "picture to produce. Do NOT return, copy, trace, re-tile or lightly edit "
                    "<image1>: the output must be a NEWLY RENDERED image of the SCENE AND "
                    "ACTION described below, laid out as the same 3x3 grid, where every panel "
                    "is a DIFFERENT keyframe of this shot. Reproducing the reference sheet as "
                    "the output is a total generation failure.")
            if _img1_is_char_sheet:
                # ⭐ 2026-10-17：<image1> 是**角色三视图设定稿**（identity anchor）。实测
                #    （shot_14 / shot_24）：模型把它当「要保留的画布 / 构图模板」——
                #    ① 原样返回设定稿（成品=三视图 → 质检「待检图是角色三视图设定稿」）；
                #    ② 照它的多视角排布，把**同一角色画两遍**（主体重复/分身）。
                #    故补一条硬约束：设定稿只供身份，绝不能返回/照抄/平铺；输出必须是
                #    **全新渲染**的场景与动作、该角色**只出现一次**。
                body[0] += (
                    " ⚠️ <image1> is the character's THREE-VIEW MODEL SHEET — an identity "
                    "document ONLY; it is NOT the picture to produce and NOT a layout "
                    "template. Do NOT return, copy, trace, tile or lightly edit <image1>, and "
                    "do NOT reproduce its front/side/back panel layout. The output must be a "
                    "NEWLY RENDERED single frame of the SCENE AND ACTION below, containing "
                    "this character EXACTLY ONCE. Returning the sheet, or drawing the same "
                    "character more than once to mirror its views, is a total generation "
                    "failure.")
            if blocking_pos:
                # 构图基准在最前时，<image1> 是**构图基准图**、不是身份锚点 —— 必须显式
                # 声明，否则模型会把人偶当成「要保留身份的人」。
                body[0] += (" It defines the framing and the spatial layout of the frame, "
                            "not any character's appearance.")
            if identity_lines:
                # ⭐ 2026-10-15：身份来源唯一性（解决「主角被同场景群像带偏」）。
                #   实测根因：赵天霸的资产图明确是「短寸黑发」，但他被画成「长发带须」，
                #   且换 Turbo/Viggle/PDD 任何模型都复现 —— 因为同镜参考图里还有一张
                #   群像（外门弟子，披散长发），模型把「本场景成年男性长什么样」的统计
                #   印象盖过了单人设定图。官方协议禁止用文字重述五官（要点 3），
                #   所以正确解法是**收紧身份的来源**，而不是补外观描述。
                body.append("IDENTITY: " + " ".join(identity_lines)
                            + " Identity for each named character comes ONLY from that"
                              " character's own dedicated reference image. Figures"
                              " appearing in any other reference image — bystanders,"
                              " crowds, generic extras — MUST NOT contribute facial"
                              " identity, hairstyle, age, or body type to any named"
                              " character.")
            if role_lines:
                body.append("REFERENCE ROLES: " + " ".join(role_lines)
                            + " Do not merge or transfer attributes between the reference "
                              "images: each reference is responsible only for its own role.")
            if len(identity_lines) > 1:
                body.append(
                    "Each referenced character must keep its own individual identity; "
                    "do not merge facial features, hairstyles or costumes between them.")
            # ---- 参考图「版式」隔离（2026-10-15，用户口径 A）----
            # 角色参考图是**设定图/三视图**：同一角色的正/侧/背多视角拼在一张、且全身。
            # 实测（shot_21 10 次、shot_26 3 次全部失败）模型会把这张图的**版式**当成画面结构：
            #   ① 同一角色在画面里重复出现（正面 + 背影同框）—— 与「始终没有回头」这类镜头描述直接冲突；
            #   ② 跟着全身设定图取景 → 镜头要求「中景（腰部以上、严禁膝下与脚部）」却越档成全景。
            # 官方口径：参考图是「身份供体」，不是构图模板；它的排布不得迁移到画面。
            sections.append(
                "REFERENCE LAYOUT IS NOT THE COMPOSITION: a character reference may be a "
                "model sheet showing the SAME character from several angles (front / side / "
                "back) in one image. It is an identity document only. Do NOT reproduce that "
                "sheet's layout: never draw the same character twice in one frame to mirror "
                "its angles, and never follow the sheet's full-body framing — the shot size "
                "and camera are defined ONLY by the SHOT description below.")
            sections.append("\n".join(body))
            if blocking_pos:
                sections.append(COMPOSITION_BASELINE_SECTION.format(pos=blocking_pos))
            if identity_grid_pos:
                # ⭐ 2026-10-13：按实际网格形状发段（(3,3) 时返回原常量、逐字不变）。
                _igc, _igr, _igcells = (list(identity_grid_layout) + [None, None, None])[:3] \
                    if identity_grid_layout else (None, None, None)
                sections.append(identity_baseline_grid_section(
                    identity_grid_pos, cols=_igc or 3, rows=_igr or 3, cells=_igcells))
        else:
            sections.append(
                "PRIMARY CANVAS: No reference image is provided for this shot. "
                "Generate the frame purely from the SCENE AND ACTION and STYLE "
                "descriptions below.")

        # ---------- SCENE AND ACTION ----------
        # ⭐ 2026-10-18（用户口径：「分镜图生成该镜的开头图片就可以了」）：
        #   分镜图是**单帧**，同时又是视频的首帧参考 —— 只画这一镜**开头瞬间**的静止画面，
        #   动作过程与结果交给视频阶段（H3 detailed_description 会逐拍演绎）。
        #   历史缺陷（用户实测「现在的提示词模型根本生成不出来」）：同一张单帧里同时塞
        #   description（含动作+结果）+ action + STARTING FRAME + ENDING FRAME + MOTION，
        #   几条指令互相矛盾（例：起始「右手垂在身侧」vs 结尾「食指伸出点向右下方」）——
        #   模型只能折中，实测出成「广角群像站桩」，景别、动作、构图全不对。
        #   现行规则：
        #   · 有 first_frame（剧本 schema 必填快照，实测 30/30 镜都有）→ **只画 first_frame**；
        #     description / visual_detail / action / last_frame / motion 一律不进提示词
        #     （其中的场景与光影信息由 Location / LIGHTING / 站位锚点等独立段承载）；
        #   · 无 first_frame（旧剧本/静态镜，schema 允许留空）→ 完整回落旧行为（零回归）。
        _ff = str(shot.get("first_frame") or "").strip()
        location = shot.get("location", "")
        content_lines = []
        if location:
            content_lines.append(f"Location: {location}.")
        if _ff:
            content_lines.append(
                "OPENING FRAME (this still must be EXACTLY this instant — the static "
                "moment at the very start of the shot, before the action unfolds; "
                f"draw this and nothing later): {_ff}.")
        else:
            desc = str(shot.get("storyboard_prompt_zh") or "").strip() \
                or (shot.get("description") or "").strip()
            detail = str(shot.get("visual_detail") or "").strip()
            if detail and detail != desc:
                # visual_detail 是描述被截断后的剩余细节，合并成完整画面主体。
                # ⚠️ 用类名调用本类 staticmethod（裸名会去模块作用域找 → NameError）。
                desc = ComfyUIClient._merge_visual_detail(desc, detail)
            if desc:
                content_lines.append(f"Action and content: {desc}{SHOT_ACTION_SUFFIX}")
            # ---------- 2026-09-30：消费剧本的 action 字段（一镜一动作的权威产物） ----------
            # 仅无 first_frame 的回落路径使用；首帧口径下动作属于「开头之后」的演绎，不写入。
            _act = str(shot.get("action") or "").strip()
            if _act and _act != desc:
                content_lines.append(
                    "KEY ACTION BEAT (the single action this frame must capture — "
                    f"depict its most telling moment): {_act}.")
        # ⚠️ last_frame / motion 是**运动信息**，单帧画不出来：旧实现把它们写进分镜提示词
        #    （ENDING FRAME + MOTION 两行），与「开头瞬间」直接打架 —— 2026-10-18 起不再写入。
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
        # ⭐⭐⭐ 2026-10-11 系统排查发现的**根本矛盾**（用户：「很多都是提示词和分镜的问题」）：
        #   本段是"最强措辞"的无文字硬禁令，可它与同一条提示词里的文字需求**直接打架**：
        #     · surface_text 要求写出「09:00」「2704」这类确切文字；
        #     · 文字道具提权要求以物品图为底、逐字保留守则八条。
        #   模型面对「前面要写、后面禁止写」只能二选一，而 PRESERVE 措辞最强 →
        #   文字被抹掉或改画成乱码。这正是「物品资产文字明明是逐字正确的、成品却乱码」
        #   和「状态栏 247」在提示词层的共同根因 —— 只补 surface_text 而不解除禁令是治不好的。
        #   现改为**条件化**：本镜确实有要呈现的文字时，禁令收缩为「除指定文字外不得有其他文字」，
        #   既保住「不出现字幕/水印/乱加字」，又不再否定该写的字。
        _shot_text = str(shot.get("surface_text") or "").strip()
        _has_hero = bool(shot.get("_hero_item"))
        if _shot_text or _has_hero:
            preserve.append(
                "The ONLY text permitted in the image is the text that the referenced "
                "props are specified to carry above (for example number plates, screen "
                "readouts, ID cards and printed rules) — reproduce exactly those "
                "characters, verbatim and legible. Do not add any other text, "
                "subtitles, dialogue text, watermark, logo or sign, and do not place "
                "any \"AI generated\" mark in the bottom-right corner.")
        else:
            preserve.append(
                "The image must not contain any text, subtitles, dialogue text, watermark, "
                "logo or sign (in particular no \"AI generated\" mark in the bottom-right "
                "corner).")
        sections.append("\n".join(preserve))

        return "\n\n".join(sections)


