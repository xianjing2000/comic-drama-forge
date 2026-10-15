# -*- coding: utf-8 -*-
"""资产高清增强：把「九宫格 / 三视图」整图切格后逐格放大重绘（2026-10-16）

动机（实测数据见 _ab_test/）：
  · 场景九宫格 grid.png @1.5MP → 单格仅 **554×309**（0.17MP）
  · 角色三视图 base.png  @1.5MP → 单视图约 **0.5MP**
  而分镜输出是 1.5MP —— 参考图细节不足会锁死输出画质上限。

做法（A/C/D/E 四方案实测对比后的最优路径 = E 方案）：
  1. 九宫格 / 三视图整图 → PIL 切格；
  2. 每格放大到目标像素预算（分镜 worklow 的 latent **继承参考图尺寸**，必须先放大）；
  3. 以该格为**单张**参考图 → generate_storyboard 重绘；
     ⚠️ 参考图必须是**单张**：喂整张九宫格会被「版式污染」，输出还是九宫格
       （实测 C 方案失败：要求「只取一个机位」仍返回 9 格），这是本模块先切格的根本原因。
  4. 落盘到 <asset_dir>/hd/，供分镜链路优先取用。

实测收益（2026-10-16，Qwen2.1 turbo + FunAcc 8 步）：
  · 单格 906×512 → **1664×928（1.54MP）**，清晰度指标 3362（全部实验最高）
  · 内容一致性：布局精准对应、无位移、无破坏性脑补（仅纹理细化）
  · 成本：约 33 秒/格

⚠️ **边界：视频链路不受影响**。
   视频走 asset_refs._pick_scene_view / _pick_char_view，仍取 grid.png / 三视图整图
   （用户 2026-10-16 口径）。本模块产物**只**被分镜链路优先取用。
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

#: 增强产物子目录名（与整图同级的 hd/）
HD_DIRNAME = "hd"

#: 场景九宫格增强文件名前缀（hd/cell01.png … cell09.png）
SCENE_HD_PREFIX = "cell"

#: 角色三视图增强文件名（正面/侧面/背面，与 ASSET_VIEW_STEMS 口径对齐）
CHARACTER_HD_VIEWS = ("front", "side", "back")


def hd_dir(asset_dir: str) -> str:
    """返回（并在需要时创建）资产的 hd/ 增强目录。"""
    d = os.path.join(asset_dir, HD_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def has_hd(asset_dir: str) -> bool:
    """该资产目录下是否已有增强产物（分镜取图据此判断是否优先用 hd/）。"""
    d = os.path.join(asset_dir or "", HD_DIRNAME)
    if not os.path.isdir(d):
        return False
    try:
        return any(f.lower().endswith(".png") for f in os.listdir(d))
    except OSError:
        return False


SCENE_HD_PROMPT = (
    "参考图1（<image1>）是某个场景的**一张**机位截图。请把它重绘为一张高清完整的单张画面："
    "保持图中的地形走势、建筑样式与位置、植被形态、道路/台阶走向、材质与光影方向完全一致，"
    "仅提升清晰度与细节，不要改变内容、不要增删主体；"
    "忽略图中左上角可能存在的数字标注；"
    "输出必须是**单张完整画面**，不得出现分格线、不得拼图、不得出现多个画面。")

CHAR_HD_PROMPT_TMPL = (
    "参考图1（<image1>）是角色三视图设定图中的**{view_zh}**视图。请把它重绘为一张高清的单张角色立绘："
    "严格保持人物的五官、脸型、发型、服装款式与配色、体型比例与参考图完全一致，"
    "仅提升清晰度与细节，不要改变身份特征、不要换装、不要改变姿态；"
    "输出必须是**单张完整画面**，不得出现分格线、不得拼图、不得出现多个视角。")

VIEW_ZH = {"front": "正面", "side": "侧面", "back": "背面"}


def _cut_grid(image_path: str, cols: int, rows: int) -> List:
    """把整图等切成 cols×rows 个 PIL Image（按行优先返回）。"""
    from PIL import Image
    g = Image.open(image_path).convert("RGB")
    W, H = g.size
    cw, ch = W // cols, H // rows
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append(g.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)))
    return out


def _upscale_to(im, size):
    """放大到目标尺寸（LANCZOS）。分镜 latent 继承参考图尺寸，故必须先放大。"""
    from PIL import Image
    return im.resize((int(size[0]), int(size[1])), Image.LANCZOS)


def _redraw(client, cell_path: str, prompt: str, out_path: str, seed: Optional[int] = None) -> bool:
    """以单张 cell 为参考重绘，成功则落到 out_path。"""
    try:
        r = client.generate_storyboard(
            prompt, [cell_path],
            filename_prefix="asset_hd/" + os.path.splitext(os.path.basename(out_path))[0],
            seed=seed)
        files = (r or {}).get("files") or []
        if files and os.path.exists(files[0]):
            import shutil
            shutil.copy2(files[0], out_path)
            return True
        logger.warning("[资产增强] 重绘无产物：%s", os.path.basename(cell_path))
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("[资产增强] 重绘失败（跳过该格）：%s — %s",
                       os.path.basename(cell_path), e)
        return False


def enhance_scene_grid(asset_dir: str, *, grid_name: str = "grid.png",
                       megapixels: float = 1.5, seed: Optional[int] = None,
                       client=None) -> dict:
    """场景九宫格 → 切 9 格 → 逐格放大重绘 → <asset_dir>/hd/cellNN.png

    :returns: {"ok": bool, "produced": [...], "failed": [...], "seconds": float}
    """
    import style_kit
    src = os.path.join(asset_dir, grid_name)
    if not os.path.exists(src):
        return {"ok": False, "reason": "无九宫格整图", "produced": [], "failed": [], "seconds": 0.0}
    if client is None:
        import comfyui_client
        client = comfyui_client.ComfyUIClient()
    t0 = time.time()
    cells = _cut_grid(src, 3, 3)
    target = style_kit.aspect_size(cells[0].size, megapixels)
    d = hd_dir(asset_dir)
    produced, failed = [], []
    logger.info("[资产增强] 场景九宫格 → %d 格，单格 → %dx%d（%.1fMP）",
                len(cells), target[0], target[1], megapixels)
    for i, cell in enumerate(cells, 1):
        name = "%s%02d.png" % (SCENE_HD_PREFIX, i)
        out = os.path.join(d, name)
        tmp = os.path.join(d, "_tmp_%s" % name)
        _upscale_to(cell, target).save(tmp)
        if _redraw(client, tmp, SCENE_HD_PROMPT, out, seed=seed):
            produced.append(name)
        else:
            failed.append(name)
        try:
            os.remove(tmp)
        except OSError:
            pass
    sec = time.time() - t0
    logger.info("[资产增强] 场景完成：成功 %d / 失败 %d，耗时 %.1f 秒（%s）",
                len(produced), len(failed), sec, os.path.basename(asset_dir))
    return {"ok": bool(produced), "produced": produced, "failed": failed, "seconds": round(sec, 1)}


def enhance_character_sheet(asset_dir: str, *, sheet_name: str = "base.png",
                            megapixels: float = 1.5, seed: Optional[int] = None,
                            client=None) -> dict:
    """角色三视图整图 → 切 3 栏 → 逐栏放大重绘 → <asset_dir>/hd/{front,side,back}.png

    三视图是「正/侧/背三张全身视图横排」，故按 1×3 等分切。
    """
    import style_kit
    src = os.path.join(asset_dir, sheet_name)
    if not os.path.exists(src):
        return {"ok": False, "reason": "无三视图整图", "produced": [], "failed": [], "seconds": 0.0}
    if client is None:
        import comfyui_client
        client = comfyui_client.ComfyUIClient()
    t0 = time.time()
    views = _cut_grid(src, 3, 1)
    target = style_kit.aspect_size(views[0].size, megapixels)
    d = hd_dir(asset_dir)
    produced, failed = [], []
    logger.info("[资产增强] 角色三视图 → %d 视图，单视图 → %dx%d（%.1fMP）",
                len(views), target[0], target[1], megapixels)
    for view, im in zip(CHARACTER_HD_VIEWS, views):
        name = "%s.png" % view
        out = os.path.join(d, name)
        tmp = os.path.join(d, "_tmp_%s" % name)
        _upscale_to(im, target).save(tmp)
        prompt = CHAR_HD_PROMPT_TMPL.format(view_zh=VIEW_ZH.get(view, view))
        if _redraw(client, tmp, prompt, out, seed=seed):
            produced.append(name)
        else:
            failed.append(name)
        try:
            os.remove(tmp)
        except OSError:
            pass
    sec = time.time() - t0
    logger.info("[资产增强] 角色完成：成功 %d / 失败 %d，耗时 %.1f 秒（%s）",
                len(produced), len(failed), sec, os.path.basename(asset_dir))
    return {"ok": bool(produced), "produced": produced, "failed": failed, "seconds": round(sec, 1)}


def enhance_asset_dir(asset_dir: str, kind: str, **kw) -> dict:
    """按资产类型分发：kind ∈ {scene, character}。物品与其它类型**不做增强**（原样返回）。"""
    if not asset_dir or not os.path.isdir(asset_dir):
        return {"ok": False, "reason": "目录不存在", "produced": [], "failed": [], "seconds": 0.0}
    if kind == "scene":
        return enhance_scene_grid(asset_dir, **kw)
    if kind == "character":
        return enhance_character_sheet(asset_dir, **kw)
    return {"ok": False, "reason": "该类型不参与增强（物品保持原样）",
    return {"ok": False, "reason": "该类型不参与增强（物品保持原样）",
            "produced": [], "failed": [], "seconds": 0.0}


# ===================== 项目级批量增强 + 命令行入口 =====================

def enhance_project_assets(project_name: str, kinds=("characters", "scenes"),
                           megapixels: float = 1.5, seed=None, client=None,
                           only_names=None) -> dict:
    """批量增强某项目下的资产（**物品不参与**）。

    扫描 <ASSETS_DIR>/<kind>/<项目名>/<资产名>/ 逐个增强。
    :returns: {kind: {资产名: 结果}}
    """
    import config as _cfg
    roots = {"characters": _cfg.CHARACTERS_DIR, "scenes": _cfg.SCENES_DIR}
    kind_map = {"characters": "character", "scenes": "scene"}
    if client is None:
        import comfyui_client
        client = comfyui_client.ComfyUIClient()
    out = {}
    for kind in kinds:
        root = roots.get(kind)
        k1 = kind_map.get(kind, "")
        res = {}
        if root and k1:
            pdir = os.path.join(root, project_name)
            if os.path.isdir(pdir):
                for name in sorted(os.listdir(pdir)):
                    if only_names and name not in only_names:
                        continue
                    d = os.path.join(pdir, name)
                    if not os.path.isdir(d):
                        continue
                    res[name] = enhance_asset_dir(d, k1, megapixels=megapixels,
                                                  seed=seed, client=client)
        out[kind] = res
    return out


def _cli():
    """命令行：python app/asset_enhance.py <项目名> [--kinds characters,scenes] [--megapixels 1.5]"""
    import argparse
    ap = argparse.ArgumentParser(description="资产高清增强（切格 → 放大重绘）")
    ap.add_argument("project", help="项目名")
    ap.add_argument("--kinds", default="characters,scenes",
                    help="逗号分隔，默认 characters,scenes（物品不参与）")
    ap.add_argument("--megapixels", type=float, default=1.5)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = enhance_project_assets(args.project, kinds=tuple(args.kinds.split(",")),
                               megapixels=args.megapixels, seed=args.seed)
    for kind, items in r.items():
        for name, res in items.items():
            print("[%s] %-20s ok=%s 成功=%d 失败=%d 耗时=%.1fs" % (
                kind, name, res.get("ok"), len(res.get("produced") or []),
                len(res.get("failed") or []), res.get("seconds") or 0))


if __name__ == "__main__":
    _cli()