# -*- coding: utf-8 -*-
"""H3 视频工作流 · 动态段数构建器

背景
----
项目内置的 ``H3信号10段测试001.json`` 是「10 段无缝拼接」工作流：
10 个 H3 子图实例经 9 个 ``H3ContinuousSeamlessJoinV14`` 串行拼接，
末端经 ``XHImagePrecision → CreateVideo → SaveVideo`` 输出**一条长视频**。

漫剧生成需要「一个分镜一段」，因此工作流段数必须按该集分镜数动态扩展/裁剪
（第 4 集 22 段、第 5 集 44 段），而不是固定 10 段。

本模块职责
----------
1. 解析模板的「段链 + join 链」结构；
2. 按目标段数 N 重建工作流：保留共享资源（UNET/CLIP/VAE/采样器/LoRA/末端封装等），
   生成 N 个段实例 + N-1 个 join 节点，并重建全部连线；
3. 每段配 4 个**外部注入节点**，保证 API 层可逐段注入且不依赖子图展开细节：

   ==================  ==========================  ==========================
   实例输入             外部节点                     注入字段
   ==================  ==========================  ==========================
   prompt               Text Multiline              ``inputs.text``
   duration             PrimitiveFloat              ``inputs.value``
   qwen_reference_1     LoadImage（分镜图）           ``inputs.image``
   qwen_reference_2     LoadImage（主角锚点图）        ``inputs.image``
   ==================  ==========================  ==========================

   共享资源（clip / vae / vae_1 / model / sampler / sigmas）仍按模板用 GetNode
   引用全局 Set 变量，无需逐段注入。

输出
----
``build(n_segments)`` 返回 ``(ui_workflow, layout)``，layout 给出每段的：
实例节点 id、注入节点 id、join 链、末端封装节点 id。
"""

import copy
import json
import logging
import math
import os
import uuid
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 段实例共享资源输入 → 全局 Set 变量名
SEG_SHARED_INPUT_VAR: Dict[str, str] = {
    "clip": "CLIP",
    "vae": "Video",
    "vae_1": "Audio",
    "model": "modle",
    "sampler": "采样器",
    "sigmas": "Sigma",
}
# 必须保留的全局 Set 变量（其余 Set 变量属于原固定 10 段模板的段专属资源，构建时丢弃）
KEEP_SET_VARS = tuple(SEG_SHARED_INPUT_VAR.values())

JOIN_TYPE = "H3ContinuousSeamlessJoinV14"


class H3EpisodeBuilder:
    """按分镜数动态生成 H3 多段拼接工作流。"""

    def __init__(self, template_path: str):
        self.template_path = template_path
        with open(template_path, "r", encoding="utf-8-sig") as f:
            self.template = json.load(f)
        self.subgraphs = {
            s.get("id"): s
            for s in ((self.template.get("definitions") or {}).get("subgraphs") or [])
        }
        self.nodes_by_id = {n.get("id"): n for n in (self.template.get("nodes") or [])}
        self.links_by_id: Dict[int, tuple] = {}
        for l in self.template.get("links") or []:
            if isinstance(l, dict):
                self.links_by_id[l["id"]] = (l["origin_id"], l["origin_slot"],
                                             l["target_id"], l["target_slot"])
            elif isinstance(l, (list, tuple)) and len(l) >= 5:
                self.links_by_id[l[0]] = (l[1], l[2], l[3], l[4])
        node_ids = [n["id"] for n in (self.template.get("nodes") or []) if isinstance(n.get("id"), int)]
        self._max_node_id = max(node_ids) if node_ids else 0
        link_ids = [k for k in self.links_by_id if isinstance(k, int)]
        self._max_link_id = max(link_ids) if link_ids else 0
        self._cursor_node = self._max_node_id
        self._cursor_link = self._max_link_id

    # ------------------------------------------------------------------ 基础工具
    def _is_inst(self, node: dict) -> bool:
        return node.get("type") in self.subgraphs

    @staticmethod
    def _slot_of(node: dict, name: str, kind: str = "outputs") -> Optional[int]:
        for i, s in enumerate(node.get(kind) or []):
            if s.get("name") == name:
                return i
        return None

    def _new_node_id(self) -> int:
        self._cursor_node += 1
        return self._cursor_node

    def _new_link_id(self) -> int:
        self._cursor_link += 1
        return self._cursor_link

    def _make_util_node(self, class_type: str, widget_name: str, widget_type: str,
                        value, pos: Tuple[float, float]) -> dict:
        """构造基础工具节点（Text Multiline / PrimitiveFloat / PrimitiveInt）。"""
        nid = self._new_node_id()
        return {
            "id": nid,
            "type": class_type,
            "pos": [pos[0], pos[1]],
            "size": [270, 90],
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [{
                "localized_name": widget_name, "name": widget_name,
                "type": widget_type, "widget": {"name": widget_name}, "link": None,
            }],
            "outputs": [{
                "localized_name": widget_name, "name": widget_type,
                "type": widget_type, "links": [],
            }],
            "properties": {"Node name for S&R": class_type},
            "widgets_values": [value],
            "widgets_values_named": {widget_name: value},
        }

    # ------------------------------------------------------------------ 分辨率
    def _template_resolution(self) -> Optional[Tuple[int, int]]:
        """按模板 ResolutionSelector 设置（16:9 × 0.5MP × 32）复算宽高。

        模板中段实例的 width/height 输入由 GetNode 引用全局「宽 / 高」Set 变量，
        而这两个变量源自 ResolutionSelector 节点；重建时该节点与变量都被丢弃，
        故在此按 ComfyUI 同款公式（comfy_extras/nodes_resolution.py）静态复算，
        写入每段实例的 widgets_values_named，使其与模板设置一致。
        """
        sel = next((n for n in self.template.get("nodes") or []
                    if n.get("type") == "ResolutionSelector"), None)
        if sel is None:
            return None
        named = sel.get("widgets_values_named") or {}
        vals = sel.get("widgets_values") or []

        def _pick(key: str, idx: int):
            return named.get(key, vals[idx] if len(vals) > idx else None)

        aspect = _pick("aspect_ratio", 0)
        megapixels = _pick("megapixels", 1)
        multiple = _pick("multiple", 2)
        ratios = {
            "1:1 (Square)": (1, 1),
            "2:3 (Portrait Photo)": (2, 3),
            "3:2 (Photo)": (3, 2),
            "3:4 (Portrait Standard)": (3, 4),
            "4:3 (Standard)": (4, 3),
            "9:16 (Portrait Widescreen)": (9, 16),
            "16:9 (Widescreen)": (16, 9),
            "21:9 (Ultrawide)": (21, 9),
        }
        if aspect not in ratios or megapixels is None or multiple is None:
            return None
        try:
            megapixels = float(megapixels)
            multiple = int(multiple)
        except (TypeError, ValueError):
            return None
        if multiple <= 0:
            return None
        w_ratio, h_ratio = ratios[aspect]
        scale = math.sqrt(megapixels * 1024 * 1024 / (w_ratio * h_ratio))
        width = round(w_ratio * scale / multiple) * multiple
        height = round(h_ratio * scale / multiple) * multiple
        return int(width), int(height)

    # ------------------------------------------------------------------ 结构解析
    def analyze(self) -> dict:
        """解析模板：段实例顺序、join 顺序、第一段 / 其余段模板。"""
        insts = [n for n in self.template["nodes"] if self._is_inst(n)]
        prev_of: Dict[int, int] = {}
        for n in insts:
            for inp in n.get("inputs") or []:
                if inp.get("name") == "previous_latent" and inp.get("link") in self.links_by_id:
                    prev_of[n["id"]] = self.links_by_id[inp["link"]][0]
        next_of = {v: k for k, v in prev_of.items()}
        seg_chain: List[int] = []
        heads = [n["id"] for n in insts if n["id"] not in prev_of]
        cur = heads[0] if heads else None
        while cur is not None:
            seg_chain.append(cur)
            cur = next_of.get(cur)

        joins = [n["id"] for n in self.template["nodes"] if n.get("type") == JOIN_TYPE]
        prev_join: Dict[int, int] = {}
        for jid in joins:
            for inp in self.nodes_by_id[jid].get("inputs") or []:
                if inp.get("name") == "previous_images" and inp.get("link") in self.links_by_id:
                    src = self.links_by_id[inp["link"]][0]
                    if self.nodes_by_id[src].get("type") == JOIN_TYPE:
                        prev_join[jid] = src
        jnext = {v: k for k, v in prev_join.items()}
        join_chain: List[int] = []
        jheads = [j for j in joins if j not in prev_join]
        cur = jheads[0] if jheads else None
        while cur is not None:
            join_chain.append(cur)
            cur = jnext.get(cur)

        return {
            "segments": seg_chain,
            "joins": join_chain,
            "first_segment": seg_chain[0] if seg_chain else None,
            "rest_segment": seg_chain[1] if len(seg_chain) > 1 else None,
            "first_join": join_chain[0] if join_chain else None,
            "segment_count": len(seg_chain),
            "join_count": len(join_chain),
        }

    def _frozen_ids(self) -> set:
        """需要在重建中保留的共享节点：模型/VAE/采样器/LoRA/末端封装 + 保留的 Set 变量。

        丢弃：原段实例、join、段专属 GetNode、段专属 SetNode（宽/高/秒/Reference/提示词N）、
        Text Multiline、原共享参考图 LoadImage（改由每段自带的注入节点承担）。
        """
        frozen = set()
        for n in self.template["nodes"]:
            nid, ntype = n.get("id"), n.get("type") or ""
            if self._is_inst(n) or ntype == JOIN_TYPE:
                continue
            if ntype in ("GetNode", "Text Multiline", "PrimitiveFloat",
                         "PrimitiveInt", "PrimitiveString", "ResolutionSelector"):
                continue
            if ntype == "SetNode":
                var = (n.get("widgets_values_named") or {}).get("Constant")
                if var not in KEEP_SET_VARS:
                    continue
            if ntype in ("LoadImage", "LoadImageOutput"):
                continue
            frozen.add(nid)
        return frozen

    # ------------------------------------------------------------------ 连线
    @staticmethod
    def _connect(links: List[list], index: Dict[int, dict], link_id: int,
                 origin_id: int, origin_name: str,
                 target_id: int, target_name: str) -> list:
        o, t = index[origin_id], index[target_id]
        os_ = H3EpisodeBuilder._slot_of(o, origin_name, "outputs")
        ts = H3EpisodeBuilder._slot_of(t, target_name, "inputs")
        if os_ is None or ts is None:
            raise KeyError(f"连线失败: {origin_id}.{origin_name} -> {target_id}.{target_name}")
        typ = (o["outputs"][os_].get("type") or t["inputs"][ts].get("type") or "*")
        lk = [link_id, origin_id, os_, target_id, ts, typ]
        links.append(lk)
        olinks = o["outputs"][os_].get("links")
        if olinks is None:
            o["outputs"][os_]["links"] = olinks = []
        olinks.append(link_id)
        t["inputs"][ts]["link"] = link_id
        return lk

    # ------------------------------------------------------------------ 子图副本
    def _clone_rest_subgraph(self, seg_index: int, n_segments: int) -> str:
        """为「非首段」生成一个**独立子图副本**，改写递增参数，返回新 UUID。

        为什么必须做（2026-09-24 根因）：模板「H3信号10段测试001.json」的 10 个段是
        **10 个不同子图**，每个子图内部 ``H3ContinuousSaveLatent.clip_index`` 递增
        （1..10），且末段 ``H3ContinuousStitchOutputV14.output_mode`` 是 ``Final Clip``
        （其余 ``Stitch Ready``）。若像旧实现那样把所有非首段都指向同一个「Clip 2」
        子图 UUID，则：
          - 所有段的 SaveLatent 都写 ``clip_00002.safetensors``（互相覆盖，永远没有
            clip_00003）；
          - 没有 ``Final Clip`` 收尾；
          - 「from Clip 1」的 Continue 配置被错误复用到第 3..N 段 → 冻结帧共识失败
            （stable_final_consensus_failed）→ 死循环。

        正确做法：每段深拷贝「第二段」子图，生成新 UUID，把 clip_index 改成 seg_index+1，
        末段 output_mode 改成 ``Final Clip``，并追加进 ``wf["definitions"]["subgraphs"]``。
        """
        src_uuid = self.nodes_by_id[self._rest_seg_id]["type"]
        sg = copy.deepcopy(self.subgraphs[src_uuid])
        new_uuid = str(uuid.uuid4())
        sg["id"] = new_uuid
        for sn in (sg.get("nodes") or []):
            st = sn.get("type") or ""
            if st == "H3ContinuousSaveLatent":
                clip_idx = seg_index + 1
                wv = sn.get("widgets_values") or []
                if len(wv) >= 2:
                    wv[1] = clip_idx
                sn["widgets_values"] = wv
                named = sn.get("widgets_values_named") or {}
                named["clip_index"] = clip_idx
                sn["widgets_values_named"] = named
            elif st == "H3ContinuousStitchOutputV14":
                mode = "Final Clip" if seg_index == n_segments - 1 else "Stitch Ready"
                sn["widgets_values"] = [mode]
                named = sn.get("widgets_values_named") or {}
                named["output_mode"] = mode
                sn["widgets_values_named"] = named
        self._extra_subgraphs.append(sg)
        return new_uuid

    # ------------------------------------------------------------------ 构建
    def build(self, n_segments: int, duration: float = 5.0,
              resolution_override: Optional[Tuple[int, int]] = None) -> Tuple[dict, dict]:
        """按目标段数重建工作流。

        n_segments: 目标段数（= 该集分镜数）
        duration: 段默认时长（秒）
        resolution_override: 可选 (宽, 高)，覆盖模板 ResolutionSelector 的分辨率
                             （用户设定竖屏 9:16 时传入 (544, 960)）
        """
        if n_segments < 1:
            raise ValueError(f"段数必须 >= 1，当前 {n_segments}")
        analysis = self.analyze()
        seg_ids = analysis["segments"]
        if not seg_ids:
            raise RuntimeError("模板中未找到 H3 段实例（子图实例），无法重建")
        # 分辨率：模板 ResolutionSelector 默认 16:9 × 0.5MP × 32 → 960×544 横屏。
        # 用户若敲定了竖屏 9:16，必须在这里覆写，否则提示词里写着「竖屏」而画布是横屏。
        resolution = (tuple(resolution_override) if resolution_override
                      else self._template_resolution())
        # ⚠️ 2026-10-18（8G 显存应急，凌晨自动修复）：模板 960×544 + 243 帧的 VAE 解码
        #   在 8G 卡上必然死锁（GPU 100%/32W/30+ 分钟无输出）。环境变量按比例降分辨率，
        #   例如 MJSCXT_H3_RES_SCALE=0.66 → 640×352，VAE 显存需求砍半。默认 1.0 不生效。
        try:
            _res_scale = float(os.getenv("MJSCXT_H3_RES_SCALE", "1.0") or 1.0)
        except ValueError:
            _res_scale = 1.0
        if resolution and 0.2 <= _res_scale < 1.0:
            _w = max(64, int(int(resolution[0]) * _res_scale) // 32 * 32)
            _h = max(64, int(int(resolution[1]) * _res_scale) // 32 * 32)
            logger.info("H3 段分辨率按 MJSCXT_H3_RES_SCALE=%.2f 降为 %d×%d（原 %d×%d）",
                        _res_scale, _w, _h, resolution[0], resolution[1])
            resolution = (_w, _h)
        if resolution_override:
            logger.info(f"H3 段分辨率已按用户设定覆写：{resolution[0]}×{resolution[1]}")
        elif resolution is None:
            logger.warning("模板未找到可用的 ResolutionSelector，段实例沿用模板自带宽高")
        else:
            logger.info(f"H3 段分辨率取自模板 ResolutionSelector：{resolution[0]}×{resolution[1]}")
        first_tpl = self.nodes_by_id[seg_ids[0]]
        rest_tpl = self.nodes_by_id[seg_ids[1]] if analysis["rest_segment"] else first_tpl
        join_tpl = self.nodes_by_id[analysis["first_join"]] if analysis["first_join"] else None
        # 供 _clone_rest_subgraph 定位「第二段」子图 UUID + 收集每段新子图副本
        self._rest_seg_id = seg_ids[1] if analysis["rest_segment"] else seg_ids[0]
        self._extra_subgraphs: List[dict] = []

        # 第 1 段的共享资源 GetNode 模板 & LoadImage 模板
        get_tpl: Dict[str, dict] = {}
        for inp in first_tpl.get("inputs") or []:
            lid = inp.get("link")
            if lid in self.links_by_id:
                src = self.nodes_by_id.get(self.links_by_id[lid][0])
                if src is not None and src.get("type") == "GetNode":
                    get_tpl[inp["name"]] = src
        load_tpl = next((n for n in self.template["nodes"]
                         if n.get("type") == "LoadImage"), None)
        if load_tpl is None:
            raise RuntimeError("模板中未找到 LoadImage 节点，无法为段实例挂参考图")

        frozen_ids = self._frozen_ids()
        new_nodes: List[dict] = [copy.deepcopy(self.nodes_by_id[i]) for i in sorted(frozen_ids)]
        index: Dict[int, dict] = {n["id"]: n for n in new_nodes}
        new_links: List[list] = []
        # 保留冻结节点之间的原始连线（模型/VAE/采样器 → Set 变量、211 → 58 → 59 等）
        for lid, (o, os_, t, ts) in self.links_by_id.items():
            if o in frozen_ids and t in frozen_ids and o in index and t in index:
                outs = index[o].get("outputs") or []
                typ = outs[os_].get("type") if os_ < len(outs) else "*"
                new_links.append([lid, o, os_, t, ts, typ])
        for n in new_nodes:
            for i, o in enumerate(n.get("outputs") or []):
                o["links"] = [l[0] for l in new_links if l[1] == n["id"] and l[2] == i]
            for i, inp in enumerate(n.get("inputs") or []):
                inp["link"] = next((l[0] for l in new_links if l[3] == n["id"] and l[4] == i), None)

        # ---------------- 逐段创建 ----------------
        seg_layout: List[dict] = []
        for i in range(n_segments):
            tpl = first_tpl if i == 0 else rest_tpl
            inst = copy.deepcopy(tpl)
            inst["id"] = self._new_node_id()
            # ⚠️ 非首段必须换用「独立子图副本」（clip_index 递增 + 末段 Final Clip），
            # 否则所有段共享同一个「Clip 2」子图 → SaveLatent 永远写 clip_00002、
            # 无 Final Clip 收尾、冻结帧共识失败死循环（2026-09-24 根因）。
            if i > 0:
                inst["type"] = self._clone_rest_subgraph(i, n_segments)
            named = dict(inst.get("widgets_values_named") or {})
            named.update({"duration": float(duration), "prompt": ""})
            if resolution is not None:
                named.update({"width": resolution[0], "height": resolution[1]})
            inst["widgets_values_named"] = named
            inst["widgets_values"] = [named.get(k) for k in ("width", "height", "duration", "prompt")]
            for inp in inst.get("inputs") or []:
                inp["link"] = None
            for o in inst.get("outputs") or []:
                o["links"] = []
            index[inst["id"]] = inst
            new_nodes.append(inst)
            base_x = 1000 * i
            base_y = 600

            # 1) 共享资源 GetNode
            get_ids: Dict[str, int] = {}
            for input_name, var in SEG_SHARED_INPUT_VAR.items():
                if input_name not in get_tpl:
                    continue
                g = copy.deepcopy(get_tpl[input_name])
                g["id"] = self._new_node_id()
                for inp in g.get("inputs") or []:
                    inp["link"] = None
                for o in g.get("outputs") or []:
                    o["links"] = []
                index[g["id"]] = g
                new_nodes.append(g)
                get_ids[var] = g["id"]
                self._connect(new_links, index, self._new_link_id(),
                              g["id"], g["outputs"][0]["name"], inst["id"], input_name)

            # 2) 提示词 / 时长注入节点
            p_node = self._make_util_node("Text Multiline", "text", "STRING", "", (base_x - 300, base_y))
            index[p_node["id"]] = p_node
            new_nodes.append(p_node)
            self._connect(new_links, index, self._new_link_id(),
                          p_node["id"], "STRING", inst["id"], "prompt")

            d_node = self._make_util_node("PrimitiveFloat", "value", "FLOAT",
                                          float(duration), (base_x - 300, base_y + 120))
            index[d_node["id"]] = d_node
            new_nodes.append(d_node)
            self._connect(new_links, index, self._new_link_id(),
                          d_node["id"], "FLOAT", inst["id"], "duration")

            # 3) 参考图：分镜图 + 主角锚点图
            load_ids: List[int] = []
            for ref_name in ("qwen_reference_1", "qwen_reference_2"):
                if self._slot_of(inst, ref_name, "inputs") is None:
                    continue
                ref_idx = len(load_ids)
                ld = copy.deepcopy(load_tpl)
                ld["id"] = self._new_node_id()
                ld["title"] = f"Ref{i + 1}_{'storyboard' if ref_idx == 0 else 'character'}"
                ld["pos"] = [base_x - 300, base_y + 480 + ref_idx * 400]
                for inp in ld.get("inputs") or []:
                    inp["link"] = None
                for o in ld.get("outputs") or []:
                    o["links"] = []
                index[ld["id"]] = ld
                new_nodes.append(ld)
                load_ids.append(ld["id"])
                self._connect(new_links, index, self._new_link_id(),
                              ld["id"], ld["outputs"][0]["name"], inst["id"], ref_name)

            # 4) 段间衔接
            if i > 0:
                prev_inst = index[seg_layout[i - 1]["inst"]]
                self._connect(new_links, index, self._new_link_id(),
                              prev_inst["id"], "output", inst["id"], "previous_latent")
                self._connect(new_links, index, self._new_link_id(),
                              prev_inst["id"], "handover", inst["id"], "handover")

            seg_layout.append({
                "index": i, "inst": inst["id"], "get_nodes": get_ids,
                "ref_nodes": load_ids, "prompt_node": p_node["id"],
                "duration_node": d_node["id"],
            })

        # ---------------- join 链 ----------------
        join_layout: List[int] = []
        if n_segments > 1:
            if join_tpl is None:
                raise RuntimeError("模板中未找到 H3ContinuousSeamlessJoinV14，无法拼接多段")
            for k in range(n_segments - 1):
                j = copy.deepcopy(join_tpl)
                j["id"] = self._new_node_id()
                for inp in j.get("inputs") or []:
                    inp["link"] = None
                for o in j.get("outputs") or []:
                    o["links"] = []
                index[j["id"]] = j
                new_nodes.append(j)
                cur_inst = index[seg_layout[k]["inst"]]
                nxt_inst = index[seg_layout[k + 1]["inst"]]
                if k == 0:
                    prev_img_src, prev_img_name = cur_inst["id"], "images"
                    prev_aud_src, prev_aud_name = cur_inst["id"], "audio"
                else:
                    prev_join = join_layout[k - 1]
                    prev_img_src, prev_img_name = prev_join, "images"
                    prev_aud_src, prev_aud_name = prev_join, "audio"
                pair = [
                    (prev_img_src, prev_img_name, "previous_images"),
                    (cur_inst["id"], "IMAGE", "previous_full_images"),
                    (prev_aud_src, prev_aud_name, "previous_audio"),
                    (cur_inst["id"], "handover", "previous_handover"),
                    (nxt_inst["id"], "IMAGE", "next_images"),
                    (nxt_inst["id"], "actual_head_context_frames", "next_head_context_frames"),
                    (nxt_inst["id"], "AUDIO", "next_audio"),
                    (nxt_inst["id"], "handover", "next_handover"),
                ]
                for src, sname, tname in pair:
                    self._connect(new_links, index, self._new_link_id(), src, sname, j["id"], tname)
                join_layout.append(j["id"])

        # ---------------- 末端封装 ----------------
        precision = next((n for n in new_nodes if n.get("type") == "XHImagePrecision"), None)
        create_video = next((n for n in new_nodes if n.get("type") == "CreateVideo"), None)
        if precision is None or create_video is None:
            raise RuntimeError("模板中未找到 XHImagePrecision / CreateVideo，无法收尾")
        last_src = join_layout[-1] if join_layout else seg_layout[-1]["inst"]
        last_node = index[last_src]
        img_name = "images" if self._slot_of(last_node, "images", "outputs") is not None else "IMAGE"
        aud_name = "audio" if self._slot_of(last_node, "audio", "outputs") is not None else "AUDIO"
        self._connect(new_links, index, self._new_link_id(),
                      last_src, img_name, precision["id"], "images")
        self._connect(new_links, index, self._new_link_id(),
                      last_src, aud_name, create_video["id"], "audio")

        # ---------------- 组装 ----------------
        wf = copy.deepcopy(self.template)
        wf["nodes"] = new_nodes
        wf["links"] = new_links
        wf["last_node_id"] = self._cursor_node
        wf["last_link_id"] = self._cursor_link
        wf["groups"] = []
        # 追加每段独立的「非首段」子图副本（clip_index 递增 / 末段 Final Clip）
        if self._extra_subgraphs:
            wf.setdefault("definitions", {}).setdefault("subgraphs", []).extend(
                self._extra_subgraphs)

        layout = {
            "template": self.template_path,
            "segment_count": n_segments,
            "resolution": ({"width": resolution[0], "height": resolution[1],
                            "source": "template:ResolutionSelector"}
                           if resolution is not None else None),
            "segments": seg_layout,
            "joins": join_layout,
            "precision_node": precision["id"],
            "create_video_node": create_video["id"],
            "save_video_node": next((n["id"] for n in new_nodes
                                     if n.get("type") == "SaveVideo"), None),
            "node_total": len(new_nodes),
            "link_total": len(new_links),
        }
        logger.info(f"H3 工作流已按 {n_segments} 段重建：节点 {len(new_nodes)} 个，"
                    f"连线 {len(new_links)} 条，join {len(join_layout)} 个")
        return wf, layout

    # ------------------------------------------------------------------ 落盘
    def build_to_file(self, n_segments: int, out_path: str, **kwargs) -> Tuple[str, dict]:
        wf, layout = self.build(n_segments, **kwargs)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False)
        layout["path"] = out_path
        return out_path, layout
