# -*- coding: utf-8 -*-
"""场景九宫格机位预览（2026-10-01，对标 BigBanana 的候选构图机制）。

把一张场景 base 图扩展为「9 个机位各一张同场景变体 + 一张 3x3 拼接预览」：
- 生成走现有 Qwen-Image-Edit 参考图编辑链路（base 图作参考，逐机位出全分辨率图）；
- 机位句子**单一来源 = config**（`SCENE_GRID_VIEW_KEYS/ANGLE_ZH/LABELS`，与自动流水线同源）；
- 拼接预览仅作选格参考，每张都是全分辨率、可直接「应用」为场景新 base；
- 「应用」= 选中机位图升级为 base.png（旧 base 移入回收站），后续分镜参考图与
  按机位出图自动沿用新视角。

设计依据（2026-10-01 调研）：qwenmultiangle/ComfyUI 官方均无可靠的单 prompt 九宫格——
可靠做法是 9 次独立生成 + 拼接（单角度质量远高于让模型画九宫格）；多角度提示词必须
与参考图同语言（中文机位句 + 中文场景描述，不要中英混拼）。
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 9 个机位档：**单一来源 = config**（`SCENE_GRID_VIEW_KEYS` + `SCENE_GRID_ANGLE_ZH` +
# `SCENE_GRID_LABELS`），本模块不再自己维护一份机位清单 —— 自动流水线（app.py 资产 worker
# 里的九宫格出图）与这里的手动机位预览必须逐字同源，否则「预览看到的机位」与「生产用的机位」
# 会静默分叉（旧实现就是两份字面量：预览里还有已废弃的 back/low/detail/depth，
# 而生产侧 2026-10-06 已按用户定义换成 全景/远景/中景/近景/特写A/特写B/左45/右45/俯视）。
# `sentence` 为该机位的中文画面描述（与 comfyui_client 场景多视角提示词同款格式）。
def _build_grid_angles() -> List[Dict[str, str]]:
    """从 config 派生机位清单（键序即九宫格格序）。"""
    from config import (SCENE_GRID_VIEW_KEYS, SCENE_GRID_ANGLE_ZH,
                        SCENE_GRID_LABELS)
    return [
        {"key": k,
         "label": SCENE_GRID_LABELS.get(k) or k,
         "sentence": SCENE_GRID_ANGLE_ZH.get(k) or ""}
        for k in SCENE_GRID_VIEW_KEYS
    ]


SCENE_GRID_ANGLES: List[Dict[str, str]] = _build_grid_angles()


def stitch_grid(image_paths: List[Optional[str]], out_path: str,
                cell_width: int = 640, cols: int = 3,
                slot_count: Optional[int] = None) -> str:
    """把机位图拼成 ``cols`` 列（默认 3 列）网格图。两种排布方式：

    · **定长格位模式**（``slot_count=n``，⚠️ 生产拼接**必须**用这种）：
      ``image_paths`` 被当作**按格序排列的定长槽位表**（长度应为 n），第 i 个元素对应第 i 格；
      元素为 ``None`` / 空串 / 文件不存在 → **该格留背景色**，**它后面的格不左移**。
      ⭐ 为什么必须定长：九宫格的格序**就是**用户在「场景 9 宫格多视角」里给的画面编号
      （第 5 格＝特写细节A、第 9 格＝俯视鸟瞰…）。若按「有几张排几张」紧凑排布，
      **中间缺一档会让它之后的机位整体错位一格**（内容与标签不符，比缺格更难发现）。
      （2026-10-06 修复：此前调用方「过滤后 append」+ 本函数紧凑排布，两者叠加会错位。）
    · **紧凑模式**（``slot_count=None``，默认）：按传入顺序排布、跳过不存在的文件，
      行数按实际张数算 —— 用于「有几张拼几张」的临时预览。

    返回拼好的图路径。任一张都读不到时抛 ``ValueError``（caller 已在外面兜异常）。
    """
    from PIL import Image
    if slot_count is not None:
        n = max(0, int(slot_count))
        slots: List[Optional[str]] = (list(image_paths) + [None] * n)[:n]
        avail = [p for p in slots if p and os.path.isfile(p)]
        if not avail:
            raise ValueError("没有可拼接的图片")
        probe = Image.open(avail[0]).convert("RGB")
        cell_h = int(cell_width * probe.height / max(1, probe.width))
        rows = max(1, (n + max(1, cols) - 1) // max(1, cols))
        sheet = Image.new("RGB", (max(1, cols) * cell_width, rows * cell_h), (12, 12, 16))
        for idx, p in enumerate(slots):
            if not (p and os.path.isfile(p)):
                continue                    # 缺格 → 留背景色，**不**左移后续格位
            im = Image.open(p).convert("RGB").resize((cell_width, cell_h))
            sheet.paste(im, ((idx % cols) * cell_width, (idx // cols) * cell_h))
    else:
        imgs = [Image.open(p).convert("RGB") for p in image_paths if p and os.path.isfile(p)]
        if not imgs:
            raise ValueError("没有可拼接的图片")
        cell_h = int(cell_width * imgs[0].height / max(1, imgs[0].width))
        cells = [im.resize((cell_width, cell_h)) for im in imgs]
        rows = max(1, (len(cells) + max(1, cols) - 1) // max(1, cols))
        sheet = Image.new("RGB", (max(1, cols) * cell_width, rows * cell_h), (12, 12, 16))
        for idx, im in enumerate(cells):
            sheet.paste(im, ((idx % cols) * cell_width, (idx // cols) * cell_h))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.save(out_path, "PNG")
    return out_path


def generate_scene_grid(client, project: str, scene_name: str,
                        base_image_path: str, scene_prompt: str, style: str,
                        asset_dir: str, seed: int = None, size=None,
                        progress_cb: Callable[[int, int, Dict], None] = None) -> Dict:
    """逐机位生成场景变体并拼接 3x3 预览。

    client: comfyui_client.ComfyUIClient 实例（app 模块级实例，由调用方传入避免循环导入）。
    scene_prompt: 场景内容描述（剧本 scenes[].reference_prompt_zh / appearance）。
    返回 {"grid": 预览图路径, "angles": [{key,label,path}], "failed": [...]}。
    """
    import style_kit
    from comfyui_client import SCENE_NO_CHARACTER_SUFFIX

    grid_dir = os.path.join(asset_dir, "grid")
    os.makedirs(grid_dir, exist_ok=True)
    base_name = client.upload_image(
        base_image_path,
        f"comic_drama_scenegrid_{project}_{scene_name}_base.png",
        image_type="output")
    # 参考图口径（2026-10-07）：场景基准图同样剥离色调/光影 token
    styled = style_kit.with_reference_style(scene_prompt, style, with_tail=False) if style else scene_prompt
    # 场景图必须去人（与 generate_scene_base 同一口径：人物属于分镜，不属于场景资产）
    styled = client.sanitize_scene_prompt(styled)

    total = len(SCENE_GRID_ANGLES)
    done_paths: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []
    for i, angle in enumerate(SCENE_GRID_ANGLES):
        if progress_cb:
            try:
                progress_cb(i, total, {"angle": angle["key"], "label": angle["label"]})
            except Exception:  # noqa: BLE001
                pass
        view_prompt = f"{styled}。本图机位（{angle['label']}）：{angle['sentence']}。"
        if SCENE_NO_CHARACTER_SUFFIX not in view_prompt:
            view_prompt = view_prompt.rstrip("。;； ") + SCENE_NO_CHARACTER_SUFFIX
        try:
            img_path = client._run_multiview_workflow(
                base_name, view_prompt, seed=seed, size=None,
                filename_prefix=f"comic_drama_scenegrid/{project}_{scene_name}_{angle['key']}")
            dst = os.path.join(grid_dir, f"{angle['key']}.png")
            if img_path and os.path.isfile(img_path):
                os.makedirs(grid_dir, exist_ok=True)
                shutil.copy2(img_path, dst)
                done_paths.append({"key": angle["key"], "label": angle["label"], "path": dst})
            else:
                failed.append({"angle": angle["key"], "error": "生成未返回文件"})
        except Exception as e:  # noqa: BLE001  单机位失败不阻断其余机位
            logger.warning("场景九宫格[%s/%s] 机位 %s 生成失败：%s: %s",
                           project, scene_name, angle["key"], type(e).__name__, e)
            failed.append({"angle": angle["key"], "error": str(e)[:200]})

    grid_path = ""
    if len(done_paths) >= 2:
        try:
            # ⚠️ 按**格序**定长拼接（不是「有几张排几张」）：某机位失败时它后面的格位
            #    不得左移，否则第 5 格之后的画面与标签不对应。缺格留背景色。
            _by_key = {d["key"]: d["path"] for d in done_paths}
            _slots = [_by_key.get(a["key"]) for a in SCENE_GRID_ANGLES]
            grid_path = stitch_grid(_slots, os.path.join(grid_dir, "grid_preview.png"),
                                    slot_count=len(SCENE_GRID_ANGLES))
        except Exception as e:  # noqa: BLE001
            logger.warning("场景九宫格预览拼接失败：%s", e)
    return {"grid": grid_path, "angles": done_paths, "failed": failed,
            "total": total, "ok_count": len(done_paths)}


def apply_grid_angle(asset_dir: str, angle_key: str, trash_move=None) -> Dict[str, str]:
    """把选中机位图升级为场景新 base（旧 base 交由 trash_move 移入回收站）。

    trash_move: app.py 的 _trash_move(src, category, trash_root, cleared, skipped)；
    传 None 则直接覆盖（不推荐，但保证函数可独立测试）。
    """
    src = os.path.join(asset_dir, "grid", f"{angle_key}.png")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"预览图不存在：{src}")
    base_png = os.path.join(asset_dir, "base.png")
    result = {"promoted": src, "replaced": ""}
    if os.path.isfile(base_png) and trash_move is not None:
        trash_move(base_png)
        result["replaced"] = base_png
    shutil.copy2(src, base_png)
    return result
