# -*- coding: utf-8 -*-
"""H3 Director 工作流 · 单次调用构建器（程序化注入 timeline_data）

背景：为什么需要这个模块
------------------------
项目原先用 ``h3_episode_builder.H3EpisodeBuilder``：以 ``H3信号10段测试001.json``
为母版，按分镜数**重建整张图**（N 个子图实例 + N-1 个
``H3ContinuousSeamlessJoinV14``），靠 latent + handover 做段间无缝续接。

2026-09-27 用户要求把视频生成工作流换成自己的整合工作流
``minimax_h3_director_二采_加速.json``——官方
`ComfyUI_MiniMaxH3_Director <https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director>`_
插件。它的形态与旧母版**根本不同**：

======================  ==============================  ==============================
                        H3EpisodeBuilder（旧）            H3DirectorBuilder（本模块）
======================  ==============================  ==============================
工作流结构              10 个子图实例 + 9 个 join         扁平单实例（12 节点，单采）
段数来源                由调用方分镜数**重建拓扑**        一个 ``MiniMaxH3Director`` 吃整条 timeline
段间衔接                ``H3ContinuousSeamlessJoinV14``   插件原生「段间引导」（尾 22 帧钉进下一段）
参考图                  ``qwen_reference_1/2`` 两个槽      ``segment.refs`` **逐段**（最多 9 张/段）
二采                    子图内部自带                      单采模板无二采；二采回退模板外接 ``MiniMaxH3DirectorRefine``
======================  ==============================  ==============================

因此**不能**把 Director 工作流塞给 ``H3EpisodeBuilder``——它没有 ``definitions.subgraphs``，
``analyze()`` 会返回空段链并抛「模板中未找到 H3 段实例」。

本构建器的职责**不是重建拓扑**（Director 工作流本身单实例，无需复制），而是：

1. 按调用方分镜程序化生成 ``timeline_data``（``segments`` / ``totalFrames`` / refs）；
2. 注入**逐段**``segment.refs``（参考图走 ComfyUI ``input/`` 相对文件名，
   每段的第 j 张 = 该段提示词里的 ``<Picture {j+1}>``）；
3. 改写 SaveVideo 的 ``filename_prefix``（2026-10-04 起的单采模板输出链已是
   **单一成片**：``Director.images → CreateVideo → SaveVideo``；旧的二采回退模板
   输出链含 ``NvidiaDLSSFrameInterpolation`` 补帧，构建器原样保留，让
   「一次调用 = 一个 mp4」，保住 ``shot_XX.mp4`` 契约；
4. 把一采链上的 ``SolAttnPatch`` 参数**完全跟随模板**（``_SOLATTN_ALIGNED_*``）；
   LowVRAM/ChunkFS 等加速注入已退役删除（2026-10-05），build() 不再程序化注入任何加速节点。

关键协议事实（读插件源码 + 实测确认，改动前务必先看这些）
--------------------------------------------------------
* **只有顶层 ``segments`` 被读取**；``batchWorkspaces`` 是前端工作区镜像，
  Python 侧零引用。``timelineMode`` 必须让 ``task_key`` 落进 gen 视频批路径
  （``prompt_batch`` + r2v），否则会走「源视频时间轴」分支并在缺 ``video`` 时报错。
* **``durationSec`` 优先于 ``frameCount``**（video batch 任务）。帧数换算唯一权威是
  官方公式：``max(5, round(sec*fps))`` 再吸附到 **17k+5** 网格。本模块的
  ``frames_for_duration`` 就是它的纯函数复刻，保证 ``total_frames`` 诚实。
* **帧数必须与该公式自洽**：``durationSec`` 写 ``frames/FPS``，插件重算才能得到同一帧数。
* ⭐ **参考图挂在 ``segment.refs``，不是 ``global.refs``**：``timelineMode=prompt_batch``
  时插件**强制** ``edit_mode = "segment"``（``gen_timeline.py:392``），段 refs 只从
  ``seg_data["refs"]`` 读（``gen_timeline.py:529``）；``global.refs`` 仅在
  ``commonEnabled=true`` 时才会 merge 进来当公共底图（同 index 段级优先）。
  项目逐镜分镜图各不相同 → 必须逐段挂；否则整集所有镜头都会用上同一张图。
* **段 refs 条数上限 = 该段提示词实际声明的 ``<Picture N>`` 个数**（见
  :func:`picture_capacity`）。插件 ``reinforce_r2v_prompt`` 只在**完全没有**
  ``<Picture`` 标签时才补标签，多塞的图会以「有 tag、无声明」进 conditioning。
* **``refs[].imageFile`` 是 ComfyUI ``input/`` 下的相对文件名**，
  不支持绝对路径；``<Picture N>`` 的 N = ``refs[].index + 1``。
* **``control_after_generate`` / ``minimax_director_ui`` 是前端伪控件**，
  不在节点 INPUT_TYPES 里，必须从 ``widgets_values_named`` 剥掉，
  否则会作为未知输入进 API prompt（``validate_api_prompt`` 会记成 unexpected_inputs）。
* **``global.prompt`` 优先于节点 widget ``global_prompt``**。本模块**两处都留空**，
  把项目自己的完整提示词放进 ``segments[i].prompt``：``commonEnabled=false`` 时
  段提示词就是 ``seg_data.prompt`` 原样（``gen_timeline.py:527``），
  开启时才是 ``concat(global, seg)`` —— 留空保证两种设置下都不会被污染。
* **``output.audioMode``**：r2v 的 ``source`` 会去取「参考音频」，本项目不挂参考音频，
  会丢掉 H3 原生生成音效（项目要靠它做音效垫底）→ 默认显式写 ``generate``。
* **``output.continuityEnabled`` + ``continuityFromPrev``**：段间引导**必须串行**，
  上一段跑完（或在磁盘缓存里）才能引导下一段。``continuityOverlapFrames`` 只允许
  5 / 22 / 39 / 56（会 snap 到最近值）。
"""
import copy
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 视频帧率（H3 按 24 训练；插件默认亦为 24）
FPS_DEFAULT = 24
#: 单段最小帧数（源 ``MIN_GEN_VIDEO_FRAMES = 4``，但官方换算下限是 5）
MIN_SEGMENT_FRAMES = 5
#: 参考图槽位上限（equal 官方 Reference to Video autogrow：ref_image_0..8）
MAX_REFERENCE_IMAGES = 9
#: 参考音频槽位上限（官方 Reference to Video autogrow：ref_audio_0..2，``<Audio N>``）
MAX_REFERENCE_AUDIOS = 3
#: 段级 LoRA 条数上限（与插件 ``segment_loras.normalize_lora_rows`` 一致，=8）
MAX_SEGMENT_LORAS = 8
#: 段间引导可承接的帧数（插件 ``snap_context_frames`` 只认这四档）
CONTINUITY_OVERLAP_CHOICES = (5, 22, 39, 56)
CONTINUITY_OVERLAP_DEFAULT = 22
#: r2v 任务类型字符串（必须与插件 ``lib/task_prompts.py`` 的 combo 选项逐字一致）
TASK_TYPE_R2V = "r2v — 参考主体生视频(Reference to Video)"

#: 前端伪控件 —— 不是 INPUT_TYPES 声明的输入，绝不能进 API prompt
PSEUDO_WIDGET_KEYS = ("control_after_generate", "minimax_director_ui")

#: ``MiniMaxH3Director`` 关键 widget 在 ``widgets_values``（位置化）里的下标。
#: ``to_api()`` 走 ``widgets_values_named``，但落盘 JSON 要与前端一致，
#: 所以两处一起改（下标取自本机实测的 26 项序列化顺序）。
_DIRECTOR_POS: Dict[str, int] = {
    "task_type": 0, "global_prompt": 1, "cfg": 3, "seed": 4,
    "frame_rate": 6, "width": 7, "height": 8, "ref_max_size": 9,
    "total_frames": 10, "timeline_data": 11,
    "steps": 13, "sampler": 14, "scheduler": 15,
    "shift_video": 16, "shift_audio": 17,
}
#: SolAttnPatch 的目标参数（采链上的唯一 SolAttnPatch）。
#: 2026-10-04 用户实测定档：视频模板换成用户**亲自跑通**的 12 节点单采工作流
#: ``h3_director_r2v_单采.json``（节点 id=87），SolAttnPatch 口径**逐字段跟随
#: 该实测模板**（不再沿用 2026-09-27 二采模板的 tau=1.2/int8_qk=False/exact_kv/
#: verbose=True/dense_blocks=0-5）。实测值：``tau=1.3 / int8_qk=True /
#: sink_conditioning=exact_kv_and_rows / verbose=False / dense_blocks=0-2,-1``。
#: ⚠️ 安全提示（2026-09-27 竖屏实测，**继续保留**）：``morton=True`` 在本机 8GB
#: 竖屏下曾稳定 ``Fatal Python error: Aborted``（栈顶 ``_morton_h3.py``）。
#: 实测模板把 ``morton`` 设为 ``False``（且 ``morton_curve=3d``），用户选择
#: **完全跟随**，故这里也保持 ``morton=False``；改这里＝改所有出片口径，
#: 只在用户明确要求时动。
#: 下方位置列表与 named 字典必须**逐项一一对应（共 12 项、同序）**：
#: 位置下标取自模板 ``SolAttnPatch.inputs`` 里带 widget 的字段声明顺序。
_SOLATTN_ALIGNED_WV = [1.3, 0.2, 0.9, 4096, True, "exact_kv_and_rows", False,
                       "3d", True, False, False, "0-2,-1"]
_SOLATTN_ALIGNED_NAMED = {
    "tau": 1.3, "start_percent": 0.2, "end_percent": 0.9, "min_tokens": 4096,
    "int8_qk": True, "sink_conditioning": "exact_kv_and_rows", "morton": False,
    "morton_curve": "3d", "int8_pv": True, "verbose": False,
    "use_tma": False, "dense_blocks": "0-2,-1",
}
# 代码内断言：位置列表与 named 字典**逐项同序一致**（防日后单边改一处）。
assert list(_SOLATTN_ALIGNED_NAMED.keys()) == [
    "tau", "start_percent", "end_percent", "min_tokens", "int8_qk",
    "sink_conditioning", "morton", "morton_curve", "int8_pv", "verbose",
    "use_tma", "dense_blocks",
], "SolAttnPatch named 字段顺序与位置列表不再一一对应"
assert list(_SOLATTN_ALIGNED_NAMED.values()) == _SOLATTN_ALIGNED_WV, (
    "SolAttnPatch 位置列表与 named 字典取值不一致")
def align_frames(n: int) -> int:
    """把帧数吸附到 MiniMax 官方 **17k+5** 网格（与插件 ``frame_align.py`` 同式）。"""
    n = max(MIN_SEGMENT_FRAMES, int(n))
    return n + (5 - (n % 17)) % 17


def frames_for_duration(seconds: float, fps: float = FPS_DEFAULT) -> int:
    """秒 → 帧数，复刻插件 ``_duration_to_minimax_frames``（唯一权威换算）。

    必须是纯函数且与插件逐字同式：构建器写进 ``durationSec`` 的是
    ``frames / fps``，插件再用本式重算一次——只有两式一致，
    ``total_frames`` 才等于各段实际帧数之和（否则报告与缓存键都会错位）。
    """
    a = max(0.1, float(seconds or 0.1))
    rate = max(1.0, float(fps or FPS_DEFAULT))
    return align_frames(int(round(a * rate)))


def snap_overlap_frames(n: Any) -> int:
    """段间引导承接帧数取最接近的合法档（5 / 22 / 39 / 56）。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return CONTINUITY_OVERLAP_DEFAULT
    return min(CONTINUITY_OVERLAP_CHOICES, key=lambda c: abs(c - v))


#: 提示词里 ``<Picture N>`` 标签的出现形式（大小写不敏感，与插件 reinforce 判据同源）
_PICTURE_TAG_RE = re.compile(r"<\s*picture\s+(\d+)\s*>", re.IGNORECASE)


def picture_capacity(prompt: str) -> Optional[int]:
    """提示词里实际声明到第几张参考图？无标签返回 ``None``。

    ⭐ 为什么必须按提示词来定参考图条数：插件 ``reinforce_r2v_prompt`` 只在**完全
    没有** ``<Picture`` 标签时才补标签；多塞的图会以「有 tag、无声明」的状态进
    conditioning —— 模型看到一张没被告知用途的图，等于给画面加了不可控变量。
    项目约定是「有几张图就在提示词里声明几张」（见 ``h3_prompt_kit``），
    所以段级参考图条数取「声明数的上限」，多出来的直接丢。
    """
    if not prompt:
        return None
    nums = [int(m.group(1)) for m in _PICTURE_TAG_RE.finditer(str(prompt))]
    return max(nums) if nums else None


#: 模板类型判定缓存：{绝对路径: 是否 Director 模板}。
#: 判定要读 88KB JSON，`generate_h3_sequence` 每次调用都会问一次，缓存掉以免重复 IO。
_TEMPLATE_KIND_CACHE: Dict[str, bool] = {}


def is_director_template(path: str) -> bool:
    """模板里有没有 ``MiniMaxH3Director`` 节点？

    ⭐ 判「走哪个构建器」只看**结构**，绝不看文件名——文件名可以改，
    结构不会骗人；旧母版（子图连续拼接）与 Director 模板必须能自动区分，
    否则一旦有人改了 ``WORKFLOW_TEMPLATE`` 里的文件名，就是一句难查的
    「模板中未找到 H3 段实例」。读不出来时按「非 Director」处理并告警
    （宁可走旧路径报明确错误，也不要在这里静默改变行为）。
    """
    key = os.path.abspath(str(path or ""))
    if not key:
        return False
    if key in _TEMPLATE_KIND_CACHE:
        return _TEMPLATE_KIND_CACHE[key]
    kind = False
    try:
        with open(key, "r", encoding="utf-8-sig") as f:
            wf = json.load(f)
        kind = any(n.get("type") == "MiniMaxH3Director"
                   for n in (wf.get("nodes") or []))
    except (OSError, ValueError) as e:  # noqa: BLE001
        logger.warning("读取模板判定类型失败（按非 Director 处理）：%s -> %s", key, e)
    _TEMPLATE_KIND_CACHE[key] = kind
    return kind


class H3DirectorBuilder:
    """以 Director 工作流为模板，注入一条 timeline，返回可提交的 UI 工作流。"""

    def __init__(self, template_path: str):
        self.template_path = template_path
        with open(template_path, "r", encoding="utf-8-sig") as f:
            self.template = json.load(f)
        if not isinstance(self.template.get("nodes"), list) or not self.template["nodes"]:
            raise ValueError(f"不是合法的 ComfyUI UI 工作流（缺 nodes）：{template_path}")
        if self._first("MiniMaxH3Director") is None:
            raise ValueError(
                f"模板里没有 MiniMaxH3Director 节点，无法注入 timeline：{template_path}"
            )

    # ------------------------------------------------------------------ 基础工具
    def _first(self, ntype: str) -> Optional[dict]:
        return next((n for n in self.template.get("nodes") or []
                     if n.get("type") == ntype), None)

    @staticmethod
    def _slot_index(node: dict, name: str, kind: str = "outputs") -> Optional[int]:
        for i, s in enumerate(node.get(kind) or []):
            if s.get("name") == name:
                return i
        return None

    @staticmethod
    def _set_widget(node: dict, pos_map: Dict[str, int], key: str, value: Any) -> None:
        """同时写 ``widgets_values_named``（to_api 用）与 ``widgets_values``（前端用）。"""
        named = dict(node.get("widgets_values_named") or {})
        named[key] = value
        node["widgets_values_named"] = named
        idx = pos_map.get(key)
        wv = list(node.get("widgets_values") or [])
        if idx is not None and idx < len(wv):
            wv[idx] = value
            node["widgets_values"] = wv

    @staticmethod
    def _strip_pseudo_widgets(node: dict) -> List[str]:
        """剥掉前端伪控件（返回被剥掉的键，便于日志）。"""
        removed = []
        named = dict(node.get("widgets_values_named") or {})
        for k in PSEUDO_WIDGET_KEYS:
            if k in named:
                named.pop(k)
                removed.append(k)
        if removed:
            node["widgets_values_named"] = named
        return removed

    @staticmethod
    def _widget_pos_map(node: dict) -> Dict[str, int]:
        """推导 widget 名 → ``widgets_values`` 下标的映射。

        UI 工作流里 ``widgets_values`` 的排列顺序 == inputs 中**带 widget 键**的
        声明顺序（非 widget 的输入是从别处连线过来的，不占位）。

        既有代码多用硬编码 pos_map（如 ``_DIRECTOR_POS``）；这里改成通用推导，
        免得每新增一个模型槽位就要手工维护一份下标常量、且极易写错。
        """
        pm: Dict[str, int] = {}
        idx = 0
        for inp in node.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            name = inp.get("name")
            if not name:
                continue
            if isinstance(inp.get("widget"), dict):
                pm[name] = idx
                idx += 1
        return pm

    def apply_model_overrides(self, overrides: dict) -> List[dict]:
        """按 ``{node_id: {field: value}}`` 覆盖模板里模型加载节点的取值。

        用途：用户在前端扫到的 ComfyUI 合法模型里手选后，用这里把选择覆盖到
        工作流模板上，避免模板写死文件名与实际磁盘布局（子目录会带前缀）不符
        导致的节点校验失败。

        ``overrides`` 为空时什么也不做，保证「未手选」== 改动前行为。
        返回实际发生变更的记录列表，便于调用方打日志。
        """
        if not overrides:
            return []
        by_id = {n.get("id"): n for n in self.template.get("nodes") or []}
        applied: List[dict] = []
        for nid, patch in (overrides or {}).items():
            node = by_id.get(nid)
            if node is None:
                logger.warning(f"[H3-Director] 模型覆盖跳过：模板里没有节点 id={nid}")
                continue
            if not isinstance(patch, dict) or not patch:
                continue
            pos_map = self._widget_pos_map(node)
            named_before = dict(node.get("widgets_values_named") or {})
            for field, value in patch.items():
                old = named_before.get(field)
                if str(old) == str(value):
                    continue  # 已是目标值，不产生噪音日志
                self._set_widget(node, pos_map, field, value)
                applied.append({
                    "node": nid,
                    "type": node.get("type"),
                    "field": field,
                    "from": old,
                    "to": value,
                })
        return applied

    # ------------------------------------------------------------------ 帧数
    def _segment_frames(self, seg: dict, fps: float) -> int:
        """单段帧数：显式 ``frames`` > ``duration``（走官方换算）。"""
        if seg.get("frames"):
            try:
                return align_frames(int(seg["frames"]))
            except (TypeError, ValueError):
                pass
        return frames_for_duration(float(seg.get("duration") or 0.0) or 1.0, fps)

    # ------------------------------------------------------------------ timeline
    def _build_timeline(self, seg_list: List[dict], *, refs: Sequence[str],
                        seg_refs: Optional[Sequence[Sequence[str]]],
                        seg_audios: Optional[Sequence[Sequence[str]]],
                        common_ref_audios: Optional[Sequence[str]] = None,
                        common_prompt: Optional[str] = None,
                        common_enabled: bool,
                        seg_ref_start: int = 0,
                        width: int, height: int, fps: float,
                        frames_each: List[int], ref_max_size: int,
                        continuity: bool, overlap: int,
                        audio_mode: str, export_mode: str,
                        live_tae_preview: bool) -> dict:
        tpl_tl = {}
        director = self._first("MiniMaxH3Director")
        named = director.get("widgets_values_named") or {}
        raw_tl = named.get("timeline_data")
        if isinstance(raw_tl, str) and raw_tl.strip():
            try:
                tpl_tl = json.loads(raw_tl)
            except (TypeError, ValueError):
                logger.warning("模板 timeline_data 不是合法 JSON，将按空模板重建")
        # ``output`` / ``video`` 以模板为底（保留 UI 侧其它参数），只覆盖我们要控的字段
        base_output = dict(tpl_tl.get("output") or {})
        base_video = dict(tpl_tl.get("video") or {})

        # ⭐ 公共参考图（global.refs）必须**先**算出来：它占 index 0..K-1，
        #    段级 refs 要从 index K 起编号，两边的 index 空间必须严格错开，
        #    否则插件 merge_indexed_refs 会按同 index 让段级把公共项逐槽覆盖
        #    （公共图一张不生效且零报错）。K 由调用方传入（= len(global refs)）。
        ref_items = self._ref_items(refs)
        _seg_start = max(0, int(seg_ref_start or 0))
        # ⭐ 公共参考音色（global.refAudios，2026-10-02 用户指定「取代逐段配音」）：
        #    占 index 0..M-1（M = 公共音色数），公共段级共用同一套音色驱动口型/节奏；
        #    取代逐段 QwenTTS 后段级 refAudios 置空（见 build() 的 common_ref_audios 分支）。
        global_ref_audio_items = self._ref_audio_items(common_ref_audios)

        segments: List[dict] = []
        start = 0
        for i, (seg, frames) in enumerate(zip(seg_list, frames_each)):
            seg_start, start = start, start + frames
            # ⭐ 段级 LoRA 透传（可选键）：调用方按场景在 ``seg["loras"]`` 挂规则行
            #    （见 h3_segment_loras）。**仅当非空时**才写入段字典 —— 空则完全不加
            #    该键 → 对没有场景 LoRA 的项目（旧 JSON / 关闭该功能的调用方）零行为变更。
            #    插件在 ``editMode == "segment"`` 时按段采纳 ``segments[i].loras``。
            _seg_loras = self._normalize_seg_loras(seg.get("loras"))
            segments.append({
                "id": f"mscxt{i:04d}",
                "start": seg_start,
                "length": frames,
                "frameCount": frames,
                "durationSec": round(frames / float(fps), 6),
                "prompt": seg.get("prompt") or "",
                "negativePrompt": seg.get("negative_prompt") or "",
                # 留空 = 继承 global.taskType（插件语义，已核对源码）
                "taskType": "",
                # ⭐ 逐段参考图：``prompt_batch`` 时插件强制 edit_mode=segment，
                #    refs 只认 ``segment.refs``（``gen_timeline.py:392/529``）。
                #    这就是「每个镜头用自己的分镜图」得以成立的机制。
                #    index 从 _seg_start 起 = 让开公共块（见上方注释）。
                "refs": self._ref_items((seg_refs or [])[i]
                                        if seg_refs is not None and i < len(seg_refs)
                                        else (), start=_seg_start),
                # ⭐ 段级参考音频（audioMode=source 时驱动 H3 口型/节奏）：
                #    逐镜 QwenTTS 配音，``<Audio N>`` 标签对应。见 _ref_audio_items。
                "refAudios": self._ref_audio_items((seg_audios or [])[i]
                                                   if seg_audios is not None
                                                   and i < len(seg_audios) else ()),
                "refVideos": [],
                "genImage": {"imageFile": "", "fileName": ""},
                "startImage": None,
                "endImage": None,
                # 第 1 段设 true 也无效果（插件对 index<=0 直接返回 False），
                # 统一写 i > 0 以免读者误解
                "continuityFromPrev": bool(i > 0),
                "refImageSize": "match",
            })
            if _seg_loras:
                segments[-1]["loras"] = _seg_loras

        total = start
        output = {
            **base_output,
            "mode": "fixed",
            "width": width,
            "height": height,
            "maxExportFrames": 0,
            "exportMode": export_mode,
            "audioMode": audio_mode,
            "exportSourceImages": False,
            "exportPreFaceRefine": False,
            "refImageSize": "match",
            "continuityEnabled": bool(continuity and len(segments) > 1),
            "continuityOverlapFrames": snap_overlap_frames(overlap),
            "continuityMode": base_output.get("continuityMode") or "guide",
            "continuityRedraw": base_output.get("continuityRedraw", 0.1),
            "continuityKeepTail": base_output.get("continuityKeepTail", True),
        }
        # 段间引导：第一段没有「上一段」，只有 2 段以上才有意义
        if not output["continuityEnabled"]:
            for s in segments:
                s["continuityFromPrev"] = False

        tl = {
            **tpl_tl,
            "version": 5,
            # 显式 segment（prompt_batch 下插件自己也会强制成 segment，这里写出来便于人读）
            "editMode": "segment",
            # ⚠️ 必须让 task_key 落进 gen 视频批路径，否则走「源视频时间轴」分支
            "timelineMode": "prompt_batch",
            "totalFrames": total,
            "frameRate": fps,
            "video": base_video,
            "videoClips": [],
            "global": {
                **(tpl_tl.get("global") or {}),
                "taskType": TASK_TYPE_R2V,
                # ⭐ 公共提示词（subject lock，2026-10-02 用户指定）：commonEnabled 时插件
                #    把全局提示词**拼在每段提示词前面**（plan.py:concat_common_segment_prompt）。
                #    这里由调用方传入「角色锁定 / subject_definitions」句（含
                #    ``<Picture N>``/``<Audio N>`` 指代公共项），与段级提示词拼接成
                #    「公共锁定 + 本镜叙事」。不传（None）→ 空串（零行为变更，段提示词=原样）。
                "prompt": str(common_prompt or ""),
                # ⭐ 全局 refs = **公共参考图**（用户 2026-09-30 拍板），只在
                #    commonEnabled=true 时才会 merge 进每段（``gen_timeline.py:530``）：
                #    插件按 index 合并、同 index 段级优先，故公共项占 index 0..K-1，
                #    段级项从 K 起（见 _ref_items / _build_timeline 顶部注释）。
                #    common_enabled=False（默认）时 global.refs 不参与任何段。
                "refs": ref_items,
                "referenceVideo": {"videoFile": "", "fileName": "",
                                   "type": "input", "subfolder": ""},
                "continuousReference": False,
                "genImage": {"imageFile": ""},
                "sourceWidth": width,
                "sourceHeight": height,
                "refAudios": global_ref_audio_items,
                "commonEnabled": bool(common_enabled),
                "commonCollapsed": True,
                "refVideos": [],
            },
            "output": output,
            "runSelectEnabled": False,
            "runSelection": [],
            "segments": segments,
            "width": width,
            "height": height,
            "refMaxSize": ref_max_size,
            "gen": {**(tpl_tl.get("gen") or {}), "defaultFrameCount": frames_each[0]
                    if frames_each else 124},
            # ⚠️ fl2v 用的两套字段清空：留着旧数据容易让人误判（gen 路径不读它们）
            "shots": [],
            "keyframes": [],
            "durationSec": round(total / float(fps), 6),
            "liveTaePreview": bool(live_tae_preview),
            "batchDetailMode": tpl_tl.get("batchDetailMode") or "solo",
            # 前端工作区镜像，Python 侧零引用；清空避免带着模板的旧素材组让人误解
            "batchWorkspaces": {},
        }
        return tl

    @staticmethod
    def _ref_items(names: Sequence[str], start: int = 0) -> List[dict]:
        """参考图相对文件名 → 插件 ``refs`` 条目。

        ``index`` **从 ``start`` 起** = ``<Picture {index+1}>``。
        为什么需要 ``start``（2026-09-30）：开了公共参数（``global.refs`` +
        ``commonEnabled``）时，公共项占 index ``0..K-1``，段级项必须从 ``K`` 起 ——
        插件 ``merge_indexed_refs`` 是**按 index 合并、同 index 段级优先**
        （``director/plan.py:214``），段级若也从 0 起会把公共项**逐槽覆盖**：
        一张公共图都不生效，而且**不报任何错**（静默失效，最难查的一类）。
        另外 ``_load_refs`` 会丢掉 index ≥ 9 的条目，故上限按剩余槽位算。

        ⚠️ 先**清洗再编号**（而非编号后过滤）：官方的 ``<Picture N>`` 是**位置序号**
        （``comfy/text_encoders/minimax.py`` 里按 ``minimax_ref_items`` 的出现次序
        ``counters["image"] += 1`` 打标签），一旦 index 出现空洞，位置与 ``index+1``
        就不再相等 —— 提示词按 ``index+1`` 写的标签会整体指错图。原实现
        ``enumerate`` 后过滤，遇到空串就会留洞。
        """
        base = max(0, int(start or 0))
        room = max(0, MAX_REFERENCE_IMAGES - base)
        clean = [str(x).strip() for x in (names or []) if str(x or "").strip()][:room]
        return [
            {"index": base + i, "imageFile": name, "fileName": "",
             "type": "input", "subfolder": ""}
            for i, name in enumerate(clean)
        ]

    @staticmethod
    def _ref_audio_items(names: Sequence[str]) -> List[dict]:
        """参考音频相对文件名 → 插件 ``refAudios`` 条目。

        与 ``_ref_items`` 对称，字段名是 ``audioFile``（不是 ``imageFile``），
        见插件 ``director/pack.py:724`` / ``director/plan.py:_reference_audio_file``。
        ``index`` 从 0 起 = ``<Audio N+1>``（最多 ``MAX_REFERENCE_AUDIOS=3``）。
        """
        return [
            {"index": i, "audioFile": str(name), "fileName": "",
             "type": "input", "subfolder": ""}
            for i, name in enumerate(list(names or [])[:MAX_REFERENCE_AUDIOS])
            if str(name or "").strip()
        ]

    @staticmethod
    def _normalize_seg_loras(raw: Any) -> List[dict]:
        """段级 LoRA 透传归一化（复刻插件 ``segment_loras.normalize_lora_rows`` 口径）。

        调用方（``app/app.py``）在段字典上挂一个可选键 ``loras``——由
        ``h3_segment_loras.select_loras_for_shot`` 按场景生成的规则行。本方法把它
        归一化成插件真正接受的形状，**口径与插件一致**（不 import 插件，它在仓库外）：

        * ``name``：取 ``name`` / ``lora`` / ``lora_name`` 第一个非空者（字符串化并去空白）；
        * ``strength``：float，默认 ``1.0``，非法值回退 ``1.0``，最后 clamp 到 ``[-10, 10]``；
        * ``active``：bool，默认 ``True``（字符串按 falsy 词判定，健壮化）；
        * 空 ``name`` 跳过该行；
        * 最多 ``MAX_SEGMENT_LORAS``（=8）条。

        返回 ``list[dict]``；``raw`` 非 list/tuple 或全部无效 → ``[]``。
        """
        if not isinstance(raw, (list, tuple)):
            return []
        out: List[dict] = []
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                strength = 1.0
                active = True
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("lora")
                           or item.get("lora_name") or "").strip()
                try:
                    strength = float(item.get("strength", 1.0))
                except (TypeError, ValueError):
                    strength = 1.0
                raw_active = item.get("active", True)
                if isinstance(raw_active, str):
                    active = raw_active.strip().lower() not in (
                        "", "0", "false", "no", "off")
                else:
                    active = bool(raw_active)
            else:
                continue
            if not name:
                continue
            strength = max(-10.0, min(10.0, strength))
            out.append({"name": name, "strength": strength, "active": active})
            if len(out) >= MAX_SEGMENT_LORAS:
                break
        return out

    # ------------------------------------------------------------------ 图改造
    def _drop_pre_refine_branch(self, nodes: List[dict], links: List[list]) -> List[int]:
        """裁掉「一采（放大/精修前）」那路 CreateVideo → SaveVideo。

        为什么：``MiniMaxH3Director`` 同时输出 ``images``（二采后）与
        ``images_pre_refine``（一采），模板把两路都接了 SaveVideo → 一次调用落
        **两个** mp4。项目契约是「一个分镜一个 ``shot_XX.mp4``」，调用方只取
        ``files[0]``，多出来的那路会让取回的成片随机（排序不保证）。
        只保留二采成片，契约才稳。想看一采时把 ``keep_pre_refine=True`` 打开。
        """
        director = self._first("MiniMaxH3Director")
        pre_slot = self._slot_index(director, "images_pre_refine", "outputs")
        if pre_slot is None:
            return []
        dropped: List[int] = []
        for n in nodes:
            if n.get("type") != "CreateVideo":
                continue
            src = self._input_source(n, links, "images")
            if src and src[0] == director.get("id") and src[1] == pre_slot:
                dropped.append(n["id"])
        # 连带删掉这些 CreateVideo 的下游（SaveVideo）
        for nid in list(dropped):
            for l in links:
                if isinstance(l, (list, tuple)) and len(l) >= 5 and l[1] == nid:
                    for n in nodes:
                        if n.get("id") == l[3]:
                            dropped.append(n["id"])
        return sorted(set(dropped))

    def _drop_refine_chain(self, wf: dict) -> List[int]:
        """关闭二采：裁掉二采专属模型链 + 断开 Director.refine 输入（2026-09-28 新增）。

        为什么能安全裁：模板里一采链（node 1/16/14/39/53/41 → Director）与二采链
        （node 54→55→60→63→62→{33,38} → Director.refine）是**两条独立分支**，共享
        的只有 Director 本体与其 model/video_vae/audio_vae/clip 输入。判「专属」= 该
        节点**所有出边**都指向 Refine 或已判专属的上游（迭代不动点），一采链节点的
        出边指向 Director 本体所以不会被误纳。

        插件侧行为已核实（ComfyUI_MiniMaxH3_Director/nodes/director.py +
        director/refine_pack.py）：refine 输入未接线时 ``normalize_refine_pack`` 返回
        None → ``refine_will_sample=False`` → 二采整段跳过，Director ``images`` 输出
        自动落一采帧 → 成片链（CreateVideo/DLSS/SaveVideo）一行不用改；被裁的专属
        链成孤儿，ComfyUI 从输出反向溯源时不执行、不加载二采 UNET/Lora、不占显存。
        """
        refine = next((n for n in wf["nodes"] if n.get("type") == "MiniMaxH3DirectorRefine"), None)
        if refine is None:
            return []  # 模板本无二采节点，无需处理
        links = wf.get("links") or []

        def node_out_link_ids(nid):
            """节点的有效出边 link id（links 表 ∩ outputs[].links 声明；无声明则全出边）。"""
            all_out = {l[0] for l in links
                       if isinstance(l, (list, tuple)) and len(l) >= 5 and l[1] == nid}
            n = next((x for x in wf["nodes"] if x.get("id") == nid), None)
            decl = set()
            for o in ((n or {}).get("outputs") or []):
                if isinstance(o, dict):
                    decl.update(x for x in (o.get("links") or []) if x is not None)
            return decl & all_out if decl else all_out

        # 迭代不动点：从 Refine 反向扩——「出边全指向 dead」的节点判专属
        dead = {refine["id"]}
        changed = True
        while changed:
            changed = False
            for n in wf["nodes"]:
                nid = n.get("id")
                if nid in dead or n.get("type") in ("MiniMaxH3Director", "SaveVideo",
                                                     "CreateVideo", "NvidiaDLSSFrameInterpolation",
                                                     "DLSSNR_Video", "PreviewAny"):
                    continue
                out_ids = node_out_link_ids(nid)
                if not out_ids:
                    continue  # 源头节点（无出边/未知）不动
                targets = {l[3] for l in links
                            if isinstance(l, (list, tuple)) and len(l) >= 5
                            and l[0] in out_ids}
                if targets and targets <= dead:
                    dead.add(nid)
                    changed = True
        self._remove_nodes(wf, sorted(dead))
        # 兜底：把 Director.refine 输入置空（_remove_nodes 一般已清，防「悬空输入」误报）
        director = next((n for n in wf["nodes"] if n.get("type") == "MiniMaxH3Director"), None)
        if director is not None:
            for s in (director.get("inputs") or []):
                if isinstance(s, dict) and s.get("name") == "refine":
                    s["link"] = None
        logger.info("Director：二采已关闭（裁二采专属链 %s，Director.images 落一采帧；"
                    "画质改由 FlashVSR 超分承担）", sorted(dead))
        return sorted(dead)

    @staticmethod
    def _input_source(node: dict, links: List[list], input_name: str):
        """返回某输入连线的 (origin_id, origin_slot)，没有则 None。"""
        idx = H3DirectorBuilder._slot_index(node, input_name, "inputs")
        if idx is None:
            return None
        lid = (node.get("inputs") or [])[idx].get("link")
        if lid is None:
            return None
        for l in links:
            if isinstance(l, (list, tuple)) and len(l) >= 5 and l[0] == lid:
                return (l[1], l[2])
            if isinstance(l, dict) and l.get("id") == lid:
                return (l.get("origin_id"), l.get("origin_slot", 0))
        return None

    @staticmethod
    def _remove_nodes(wf: dict, node_ids: Sequence[int]) -> None:
        """删节点并清掉所有相关连线，保持 links / outputs[].links / inputs[].link 三方一致。"""
        if not node_ids:
            return
        dead = set(node_ids)
        nodes = wf["nodes"]
        wf["nodes"] = [n for n in nodes if n.get("id") not in dead]
        kept = []
        for l in wf.get("links") or []:
            if isinstance(l, (list, tuple)) and len(l) >= 5:
                if l[1] in dead or l[3] in dead:
                    continue
            elif isinstance(l, dict):
                if l.get("origin_id") in dead or l.get("target_id") in dead:
                    continue
            kept.append(l)
        wf["links"] = kept
        # 重算 bookkeeping：只保留仍存在的 link id
        alive = {l[0] for l in kept if isinstance(l, (list, tuple)) and l}
        for n in wf["nodes"]:
            for s in n.get("outputs") or []:
                if s.get("links"):
                    s["links"] = [lid for lid in s["links"] if lid in alive]
            for s in n.get("inputs") or []:
                if s.get("link") is not None and s["link"] not in alive:
                    s["link"] = None

    # ------------------------------------------------------------------ 主入口
    def build(self, segments: Sequence[dict], *, refs: Sequence[str] = (),
              seg_refs: Optional[Sequence[Sequence[str]]] = None,
              seg_audios: Optional[Sequence[Sequence[str]]] = None,
              common_ref_audios: Optional[Sequence[str]] = None,
              common_prompt: Optional[str] = None,
              common_enabled: Optional[bool] = None,
              width: Optional[int] = None, height: Optional[int] = None,
              frame_rate: float = FPS_DEFAULT, seed: Optional[int] = None,
              filename_prefix: str = "", keep_pre_refine: bool = False,
              continuity: bool = True,
              continuity_overlap: Any = CONTINUITY_OVERLAP_DEFAULT,
              ref_max_size: Optional[int] = None,
              audio_mode: str = "generate",
              export_mode: str = "all",
              live_tae_preview: bool = False,
              align_accel_chain: bool = True,
              enable_refine: bool = True) -> Tuple[dict, dict]:
        """注入一条 timeline，返回 ``(ui_workflow, layout)``。

        segments: ``[{"prompt": str, "duration": 秒, "name": str,
                      "reference_images": [本地路径(仅用于校验), ...]}, ...]``
                  **顺序 = 时间轴顺序**；相邻段由插件「段间引导」衔接。
        seg_refs: ⭐ **逐段参考图**（ComfyUI ``input/`` 下的相对文件名，调用方先 upload）：
                  ``seg_refs[i]`` 服务 ``segments[i]``，第 j 个 → ``ref_image_{j}``
                  → 该段提示词里的 ``<Picture {j+1}>``。长度可与 segments 不等
                  （短的按空处理）。这是**主用法**：整集模式下每个镜头有自己的分镜图。
        seg_audios: ⭐ **逐段参考音频**（同 seg_refs 格式，``audioFile`` 相对名）：
                  ``seg_audios[i]`` 服务 ``segments[i]``，第 j 个 → ``ref_audio_{j}``
                  → 该段提示词里的 ``<Audio {j+1}>``（最多 3 个）。仅当
                  ``audio_mode="source"`` 时插件才会用参考音频驱动口型/节奏；
                  逐镜 QwenTTS 配音走这里。长度可与 segments 不等（短的按空处理）。
        refs:     可选**全局**参考图（同格式）= **公共参考图**（2026-09-30 用户拍板）：
                  一次提交里「每一段都在用、且用的是同一张图」的资产。开启后
                  （``common_enabled=True``）它们占 index ``0..K-1``
                  （K = ``len(refs)``），各段自带的 ``seg_refs`` 从 index ``K`` 起 ——
                  调用方必须先按同一顺序把公共项排进每段提示词的 ``<Picture 1..K>``。
                  不传（默认）＝完全走原路径（段级 index 从 0 起、``commonEnabled=false``）。
        common_enabled: 是否把 ``global.prompt`` 前缀拼接到段提示词，
                  并让 ``global.refs`` merge 进每段。
                  默认 ``bool(refs)``——没传全局 refs 就不拼，段提示词 = 原样。
        seed:     采样种子；None = 沿用模板值。
        """
        seg_list = [dict(s or {}) for s in (segments or [])]
        if not seg_list:
            raise ValueError("H3DirectorBuilder.build: segments 不能为空")
        fps = float(frame_rate or FPS_DEFAULT) or FPS_DEFAULT
        if fps <= 0:
            raise ValueError(f"H3DirectorBuilder.build: frame_rate 非法 {frame_rate}")

        if common_enabled is None:
            _has_common = (
                bool([x for x in (refs or []) if str(x or "").strip()])
                or bool([x for x in (common_ref_audios or []) if str(x or "").strip()])
                or bool(str(common_prompt or "").strip()))
            common_enabled = _has_common
        common_enabled = bool(common_enabled)
        # ⭐ 公共块占用 index 0..K-1（K = 公共图张数，与 _ref_items 的清洗口径一致）。
        #    段级 refs 的 index 必须让开这 K 个槽位（见 _ref_items / _build_timeline）。
        ref_offset = len(self._ref_items(refs))
        if refs and not common_enabled:
            logger.warning(
                "H3DirectorBuilder：传了 %d 张公共参考图但 common_enabled=False，"
                "它们会被插件完全忽略（每段仍只有自己的图）", ref_offset)
        if common_enabled and ref_offset == 0:
            logger.warning("H3DirectorBuilder：common_enabled=True 但没有公共参考图 → "
                           "段级 refs 仍从 index 0 起，与不开启时等价")

        # ⭐ 段级参考图条数上限 = 该段提示词实际声明的 ``<Picture N>`` 个数。
        #    多塞的图会以「有 tag、无声明」的状态进 conditioning（见 picture_capacity）。
        #    段自带的 ``reference_images`` 只用来提示「提示词声明数 < 传图数」这种不一致。
        #    ⚠️ 开了公共块后，提示词里声明的编号是「公共 1..K + 本段 K+1..K+m」，
        #    故本段**自己的**预算是 ``cap - ref_offset``（而不是 cap），
        #    否则这道闸门会被公共块抬高而失效。同时受 9 槽总量约束。
        seg_ref_lists: List[List[str]] = []
        room = max(0, MAX_REFERENCE_IMAGES - ref_offset)
        for i, seg in enumerate(seg_list):
            raw = list((seg_refs[i] if seg_refs is not None and i < len(seg_refs) else []) or [])
            names = [str(x).strip() for x in raw if str(x or "").strip()]
            cap = picture_capacity(seg.get("prompt") or "")
            own_cap = room if cap is None else max(0, min(room, cap - ref_offset))
            declared = len(seg.get("reference_images") or [])
            if len(names) > own_cap:
                logger.warning(
                    "段%d(%s)：参考图 %d 张 > 本段可用槽位 %d（提示词声明至 <Picture %s>，"
                    "其中前 %d 张为公共图），多余 %d 张已丢弃",
                    i + 1, seg.get("name") or f"seg{i + 1}", len(names), own_cap,
                    cap if cap is not None else "?", ref_offset,
                    len(names) - own_cap)
                names = names[:own_cap]
            if declared and len(names) < declared:
                logger.warning(
                    "段%d(%s)：可用参考图 %d 张 < 传入 %d 张（部分文件缺失或未上传成功）",
                    i + 1, seg.get("name") or f"seg{i + 1}", len(names), declared)
            seg_ref_lists.append(names)

        wf = copy.deepcopy(self.template)
        nodes = wf["nodes"]
        links = wf["links"]
        director = next(n for n in nodes if n.get("type") == "MiniMaxH3Director")
        refine = next((n for n in nodes if n.get("type") == "MiniMaxH3DirectorRefine"), None)

        d_named = director.get("widgets_values_named") or {}
        w = int(width or d_named.get("width") or 544)
        h = int(height or d_named.get("height") or 960)
        rmax = int(ref_max_size or max(w, h))

        frames_each = [self._segment_frames(s, fps) for s in seg_list]
        total = sum(frames_each)

        # 段级参考音频：normalize 成与 segments 等长（缺的按空），供 _build_timeline 注入 refAudios
        seg_audio_lists: List[List[str]] = [
            [str(x).strip() for x in ((seg_audios[i] if seg_audios is not None
                                       and i < len(seg_audios) else []) or [])
             if str(x or "").strip()]
            for i in range(len(seg_list))
        ]

        timeline = self._build_timeline(
            seg_list, refs=refs, seg_refs=seg_ref_lists, seg_audios=seg_audio_lists,
            common_ref_audios=common_ref_audios,
            common_prompt=common_prompt,
            common_enabled=common_enabled, seg_ref_start=ref_offset,
            width=w, height=h, fps=fps, frames_each=frames_each,
            ref_max_size=rmax, continuity=continuity, overlap=continuity_overlap,
            audio_mode=audio_mode, export_mode=export_mode,
            live_tae_preview=live_tae_preview)

        # ---- 守卫 A：commonEnabled 时 global.prompt 会被**拼在段提示词前面** ----
        # 插件 ``concat_common_segment_prompt(common, segment)``（plan.py:201）在
        # r2v/r2i + commonEnabled 时把全局提示词接到每段提示词**之前**。
        # 2026-10-02：``global.prompt`` 改由调用方主动传入 ``common_prompt``（subject lock）。
        # 故守卫只在「未主动提供公共提示词（common_prompt 为空）但 global.prompt 仍非空」
        # 时清空 —— 那种非空只能是模板残留（会污染全集），主动提供的要保留。
        _gbl_prompt = str((timeline.get("global") or {}).get("prompt") or "").strip()
        _intentional_common_prompt = bool(str(common_prompt or "").strip())
        if timeline.get("global", {}).get("commonEnabled") and _gbl_prompt \
                and not _intentional_common_prompt:
            logger.warning(
                "H3DirectorBuilder：commonEnabled=true 但 global.prompt 非空（%d 字）且未主动"
                "提供公共提示词，疑似模板残留 → 已强制清空", len(_gbl_prompt))
            timeline["global"]["prompt"] = ""

        # ---- 守卫 B：槽位 index 与提示词 ``<Picture N>`` 必须一一对应 ----
        # 这是本特性最容易静默出错的地方（公共块少一张 → 后续编号整体左移），
        # 故在构建期就把「提示词声明了几张图 / 实际槽位是哪些」对齐检查一遍并落进 layout。
        ref_layout_issues = self.check_ref_layout(timeline)
        for _iss in ref_layout_issues:
            # 未开公共块时只记 info：既有路径（keyframe 模式的「首帧+尾帧」两句式 refs、
            # 无分镜图分支的全局 refs）历史上本就不保证「声明数 == 槽位数」，那是**存量**
            # 问题，不该借本次改动制造一片 warning 噪音；新特性（common_enabled）才升级为
            # 阻断，因为它一旦错位就是整集静默错图。
            (logger.warning if common_enabled else logger.info)(
                "H3DirectorBuilder 参考图槽位自检：%s", _iss)
        if ref_layout_issues and common_enabled:
            # 开了公共块还错位 → 提示词的 <Picture N> 会成片指错图，没有「凑合跑」的余地。
            raise ValueError(
                "H3DirectorBuilder：公共参考图槽位自检未通过（"
                + "；".join(ref_layout_issues[:3])
                + "）。公共项 index 必须 0..K-1 连续、段级项紧随其后，"
                  "且与提示词 <Picture N> 一一对应；请检查调用方传入的 prompt/seg_refs 是否同序。")

        # ---- Director 节点注入 ----
        self._set_widget(director, _DIRECTOR_POS, "task_type", TASK_TYPE_R2V)
        # 与 timeline.global.prompt 一致置空（插件优先读 timeline，两处都空才不会被拼进段提示词）
        self._set_widget(director, _DIRECTOR_POS, "global_prompt", "")
        self._set_widget(director, _DIRECTOR_POS, "width", w)
        self._set_widget(director, _DIRECTOR_POS, "height", h)
        self._set_widget(director, _DIRECTOR_POS, "ref_max_size", rmax)
        self._set_widget(director, _DIRECTOR_POS, "frame_rate", fps)
        self._set_widget(director, _DIRECTOR_POS, "total_frames", total)
        self._set_widget(director, _DIRECTOR_POS, "timeline_data",
                         json.dumps(timeline, ensure_ascii=False))
        stripped = self._strip_pseudo_widgets(director)
        if seed is not None:
            self._set_widget(director, _DIRECTOR_POS, "seed", int(seed))
            named = dict(director.get("widgets_values_named") or {})
            # seed 变可复现：伪控件已剥掉，这里补回「固定」语义靠的是 seed 本身
            named["control_after_generate"] = "fixed"
            director["widgets_values_named"] = {
                k: v for k, v in named.items() if k not in PSEUDO_WIDGET_KEYS}

        if refine is not None:
            self._strip_pseudo_widgets(refine)

        # ---- 二采开关（2026-09-28 用户定档：8GB 显存跑二采太吃力，画质改用 FlashVSR 超分补） ----
        # enable_refine=False 时只断开 Director 的 refine 输入连线：插件源码确认
        # （normalize_refine_pack 未接线返回 None → refine_will_sample=False），
        # 二采整段跳过、Director.images 输出自动落一采帧，二采专属模型链成孤儿不被执行。
        if refine is not None and not bool(enable_refine):
            self._drop_refine_chain(wf)
            refine = None  # layout 的 refine_node 随之报 None（二采关）

        # ---- 「一采」那路 SaveVideo ----
        save_video_pre = None
        if not keep_pre_refine:
            drop = self._drop_pre_refine_branch(nodes, links)
            if drop:
                self._remove_nodes(wf, drop)
                logger.info("Director：已裁掉一采（放大前）那路 CreateVideo/SaveVideo：%s", drop)
            nodes = wf["nodes"]
            # 裁掉后就不该再报这个节点（否则调用方会去 index 一个不存在的 id）
            save_video_pre = None
        else:
            pre = [n for n in nodes if n.get("type") == "SaveVideo"]
            save_video_pre = pre[-1]["id"] if len(pre) > 1 else None

        # ---- 加速链对齐（2026-10-04 用户定档：换单采模板，纯跟随） ----
        # 视频模板已切到用户**亲自跑通**的 12 节点单采工作流
        # ``h3_director_r2v_单采.json``（单实例、**单采**，无二采链）：
        #   模型链：UNETLoader → LoraLoader → PathchSage → TESpeed → SolAttnPatch → EasyCache → Director
        #   输出链：Director.images → CreateVideo → SaveVideo
        # 模板**不含** MiniMaxH3DirectorRefine / BasicScheduler / NvidiaDLSSFrameInterpolation /
        # 第二通道 UNETLoader/LoraLoader，也不含 MiniMaxLowVRAMAttention /
        # MiniMaxChunkFeedForward / MemoryEfficientSagePatch。
        # 因此 LowVRAM/ChunkFS 等加速注入已退役删除（2026-10-05），build() 不再
        # 程序化注入任何加速节点——只同步 SolAttnPatch 参数
        # （完全跟随模板，见 _SOLATTN_ALIGNED_*；模板字面已与该常量一致）。
        # 旧二采模板 ``minimax_h3_director_二采_加速.json`` 保留为回退
        # （``WORKFLOW_TEMPLATE["h3_video_refine"]``），本构建器对两种模板都能工作
        # （refine 节点缺失时 refine=None，二采开关自动降级为 no-op）。
        accel_added: Dict[str, int] = {}
        if align_accel_chain:
            sol = next((n for n in nodes if n.get("type") == "SolAttnPatch"), None)
            if sol is not None:
                sol["widgets_values"] = list(_SOLATTN_ALIGNED_WV)
                sol["widgets_values_named"] = dict(_SOLATTN_ALIGNED_NAMED)

        # ---- 成片文件名 ----
        save_videos = [n for n in wf["nodes"] if n.get("type") == "SaveVideo"]
        save_video = None
        for n in save_videos:
            nid = n["id"]
            prefix = filename_prefix
            if save_video_pre is not None and nid == save_video_pre:
                prefix = (filename_prefix or "video/MiniMaxH3_Director") + "_pre"
            if prefix:
                self._set_widget(n, {"filename_prefix": 0, "codec": 4}, "filename_prefix", prefix)
            if save_video is None or nid != save_video_pre:
                save_video = nid

        layout = {
            "template": self.template_path,
            "director_node": director["id"],
            "refine_node": refine["id"] if refine is not None else None,
            "save_video_node": save_video,
            "save_video_node_pre": save_video_pre,
            "accel_added": accel_added,
            "stripped_pseudo_widgets": stripped,
            "width": w, "height": h, "ref_max_size": rmax,
            "frame_rate": fps,
            "total_frames": total,
            "duration_sec": round(total / fps, 6),
            "refs": [r.get("imageFile") for r in timeline["global"]["refs"]],
            # 段级 refs 的 index 起点（= 公共图张数 K）。报表/自检都按它读，
            # 不要再假设「段级 index 从 0 起」。
            "seg_ref_start": ref_offset,
            "common_enabled": bool(common_enabled),
            "ref_layout_issues": ref_layout_issues,
            "segment_refs": [self._ref_items(names, start=ref_offset)
                             for names in seg_ref_lists],
            "segment_audios": [self._ref_audio_items(names) for names in seg_audio_lists],
            "continuity": timeline["output"]["continuityEnabled"],
            "continuity_overlap_frames": timeline["output"]["continuityOverlapFrames"],
            "export_mode": export_mode,
            "audio_mode": audio_mode,
            "segments": [
                {"index": i, "name": seg_list[i].get("name") or f"seg{i + 1:02d}",
                 "start": timeline["segments"][i]["start"],
                 "frames": frames_each[i],
                 "duration_sec": timeline["segments"][i]["durationSec"],
                 "prompt_len": len(seg_list[i].get("prompt") or ""),
                 "ref_count": len(seg_ref_lists[i]),
                 "audio_count": len(seg_audio_lists[i]),
                 "refs": [r["imageFile"] for r in timeline["segments"][i]["refs"]],
                 "audios": [a["audioFile"] for a in timeline["segments"][i]["refAudios"]]}
                for i in range(len(seg_list))
            ],
            "node_total": len(wf["nodes"]),
            "link_total": len(wf["links"]),
        }
        # ⚠️ 参考图是**逐段**挂在 ``segment.refs``（见本文件顶部协议）；
        # ``layout["refs"]`` 统计的是 ``global.refs``（默认空，只有 commonEnabled 时才 merge），
        # 拿它打日志会把「生产其实带了 N 张参考图」误报成「参考图 0 张」，误导排查。
        n_seg_refs = sum(len(names) for names in seg_ref_lists)
        logger.info(
            "H3 Director 工作流已就绪：%d 段 / %d 帧（%.2fs）/ 参考图 %d 张（段级）"
            "+ %d 张（公共）/ 段间引导 %s(%s) / 节点 %d 连线 %d",
            len(seg_list), total, total / fps, n_seg_refs, len(layout["refs"]),
            layout["continuity"], layout["continuity_overlap_frames"],
            len(wf["nodes"]), len(wf["links"]))
        return wf, layout

    @staticmethod
    def check_ref_layout(timeline: dict) -> List[str]:
        """自检：参考图槽位 index 与提示词 ``<Picture N>`` 是否一一对应 → 问题列表。

        ## 为什么必须有这道检查（2026-09-30）

        插件的编号规则是**位置制**：``comfy/text_encoders/minimax.py`` 按
        ``minimax_ref_items`` 的出现次序打标签（``counters["image"] += 1``），而
        ``minimax_ref_items`` 的顺序 = 插件合并后 refs 的 **index 升序**
        （``director/executor_core.py`` 逐槽 ``ref_image_{idx}`` → 官方节点
        ``for img in ref_images.values()``）。也就是说：

            ``<Picture N>`` 里的 N = 该图在「合并后 index 升序列表」里的**第几位**。

        index 连续（0..N-1）时它恰好等于 ``index+1``（官方 tooltip 的口径），
        但**一旦出现空洞，位置就与 index+1 脱节**，提示词按 index+1 写的标签会整体
        指错图 —— 而且模型照样出片，没有任何报错。历史上 ``_drop_missing_director_refs``
        摘图就属于这类「静默错位」。所以构建期先自己核一遍。

        判据（逐段）：
          1. 合并后槽位（global.refs ∪ segment.refs，同 index 段级优先）的 index
             必须是连续的 ``0..N-1``；
          2. 提示词里 ``<Picture>`` 声明的最大编号必须等于 N（声明少了 = 有图没声明，
             声明多了 = 声明了没图）；
          3. index 必须 < ``MAX_REFERENCE_IMAGES``（插件 ``_load_refs`` 直接丢 ≥9 的）。
        """
        issues: List[str] = []
        gbl_refs = (timeline.get("global") or {}).get("refs") or []
        for i, seg in enumerate(timeline.get("segments") or []):
            name = seg.get("id") or f"seg{i + 1}"
            by_idx: Dict[int, dict] = {}
            bad_idx = []
            for item in list(gbl_refs) + list(seg.get("refs") or []):
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index", 0))
                except (TypeError, ValueError):
                    bad_idx.append(repr(item.get("index")))
                    continue
                by_idx[idx] = item
            if bad_idx:
                issues.append(f"{name}：refs 出现非数字 index {bad_idx[:3]}")
                continue
            if any(k >= MAX_REFERENCE_IMAGES for k in by_idx):
                issues.append(
                    f"{name}：index ≥ {MAX_REFERENCE_IMAGES} 会被插件直接丢弃"
                    f"（{sorted(k for k in by_idx if k >= MAX_REFERENCE_IMAGES)}）")
            idxs = sorted(by_idx)
            if idxs and idxs != list(range(len(idxs))):
                issues.append(f"{name}：槽位 index 不连续 {idxs} → 提示词编号会整体错位")
            declared = picture_capacity(seg.get("prompt") or "")
            if declared is None:
                if idxs:
                    issues.append(f"{name}：挂了 {len(idxs)} 张参考图但提示词没有任何 "
                                  f"<Picture N> 声明（会被插件补标签，用途不可控）")
                continue
            if declared != len(idxs):
                issues.append(
                    f"{name}：提示词声明到 <Picture {declared}>，但实际槽位 {len(idxs)} 个"
                    f"（index {idxs[:12]}）")
        return issues

    # ------------------------------------------------------------------ 落盘
    def build_to_file(self, out_path: str, segments: Sequence[dict],
                      **kwargs) -> Tuple[str, dict]:
        wf, layout = self.build(segments, **kwargs)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False)
        layout["path"] = out_path
        return out_path, layout
