#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""分镜多样性离线守卫（C-3）——参考无关的「面板雷同」量化门禁。

为什么需要
----------
分镜质检是 VLM「判官」：实测会抖（同一张图 45~92 分），缺陷榜首是
**「面板雷同 573 次」**（见 ``docs/性能画像_内存并发GPU_20261012.md`` §2）。
本工具提供一个**确定性、参考无关**的交叉核对：对同一目录（=同一集/场的分镜
图集）内的分镜图两两做感知哈希（dHash），把「几乎一模一样的两张」显式列出，
不依赖任何模型、不依赖人工看片。

设计要点（对齐本仓库既有守卫风格）
----------------------------------
  · **只读**：不写任何产物；**退出码即结论**（0=达标 / 1=有违规 / 2=用法或环境错误）。
  · **不导入 `app.config`**：避免 config 的导入期副作用（读 .env、探磁盘），故本工具
    与运行中的服务完全解耦，可随时离线跑。
  · **依赖轻**：只需要 Pillow（+ numpy，本仓库 requirements 均已有）。
  · **fail-open**：单张图读失败只跳过并计数，绝不中断整轮检查。
  · **可选人脸一致性**：若运行环境恰好装了 ``insightface``，额外做同目录人脸余弦
    相似度（越低越可疑）；未装则静默跳过——本仓库 requirements 未含 insightface，
    故默认只做感知哈希。

用法::
    python tools/verify_storyboard_diversity.py --root "<STORYBOARDS_DIR>/<项目>" [--strict]
    python tools/verify_storyboard_diversity.py --root "<...>/ep01" --max-distance 6 --json
    python tools/verify_storyboard_diversity.py --root "<...>" --cross     # 跨目录也两两比

参数
----
    --root           必填。分镜图根目录（项目目录或其下某集目录均可）。
    --max-distance   dHash 汉明距离阈值（0–64），≤ 视为「近重复」。默认 6（约 ≥90% 相似）。
    --cross          除「同目录内」外，再做「跨目录」全局两两比对（抓跨集复用同一张图）。
    --min-shots      目录内图片数 < 此值则跳过该目录。默认 2。
    --top            最多打印多少对近重复。默认 30。
    --strict         发现任一超阈值近重复对时退出码为 1（否则恒 0，纯报告）。
    --json           以 JSON 输出（便于接入 CI / 看板）。

退出码::
    0 = 未发现超阈值近重复（或报告模式）
    1 = 发现超阈值近重复（仅 --strict 时影响退出码）
    2 = 参数或环境错误（如缺 Pillow）
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

try:
    from PIL import Image  # noqa: F401
except ImportError as _e:  # 环境错误，明确退出码 2
    sys.stderr.write("错误：未安装 Pillow，无法计算感知哈希。请 `pip install Pillow`。\n")
    sys.exit(2)

try:
    import numpy as np
except ImportError:
    sys.stderr.write("错误：未安装 numpy。请 `pip install numpy`。\n")
    sys.exit(2)


# --------------------------------------------------------------------------- #
# 感知哈希
# --------------------------------------------------------------------------- #

def dhash_bits(path: str, hash_size: int = 8):
    """计算 dHash（差分哈希）：返回长度 hash_size*hash_size 的一维 bool 数组。

    dHash 对缩放/轻微亮度变化稳健，对「整张画面几乎一致」极敏感——正是「面板雷同」
    的判据。读失败抛异常，由调用方计数跳过。
    """
    with Image.open(path) as im:
        im = im.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
        arr = np.asarray(im, dtype=np.int16)
    # 水平相邻像素比较：左 < 右 记 1
    return (arr[:, 1:] > arr[:, :-1]).reshape(-1)


def hamming(a, b) -> int:
    """两个布尔哈希数组的汉明距离。"""
    return int(np.count_nonzero(a != b))


def similarity_pct(dist: int, bits: int) -> float:
    return round(100.0 * (1.0 - dist / float(bits)), 1)


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #

def collect_sets(root: str, min_shots: int):
    """返回 {目录: [图片路径...]}，仅保留图片数 >= min_shots 的目录。"""
    sets = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        imgs = sorted(
            os.path.join(dirpath, f)
            for f in filenames
            if os.path.splitext(f)[1].lower() in IMG_EXTS
        )
        if len(imgs) >= min_shots:
            sets[dirpath] = imgs
    return sets


def _rel(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def analyze(root: str, max_distance: int, cross: bool, min_shots: int, top: int,
            logger=print) -> dict:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)

    sets = collect_sets(root, min_shots)
    hashes = {}      # path -> bits
    unreadable = []  # path

    # 一次性算好所有分镜图的哈希（同图只算一次）
    for imgs in sets.values():
        for p in imgs:
            if p in hashes:
                continue
            try:
                hashes[p] = dhash_bits(p)
            except Exception as e:  # noqa: BLE001 —— 坏图跳过，不中断
                unreadable.append({"file": _rel(p, root), "error": str(e)})

    bits = 8 * 8
    violations = []   # 同目录内的近重复对
    set_stats = []

    for d, imgs in sorted(sets.items()):
        good = [p for p in imgs if p in hashes]
        n = len(good)
        if n < 2:
            continue
        dists = []
        local_viol = []
        for a, b in itertools.combinations(good, 2):
            dist = hamming(hashes[a], hashes[b])
            dists.append(dist)
            if dist <= max_distance:
                local_viol.append({
                    "dir": _rel(d, root),
                    "a": _rel(a, root), "b": _rel(b, root),
                    "distance": dist, "similarity_pct": similarity_pct(dist, bits),
                })
        violations.extend(local_viol)
        set_stats.append({
            "dir": _rel(d, root),
            "shots": n,
            "pairs": len(dists),
            "min_distance": min(dists) if dists else None,
            "mean_distance": round(sum(dists) / len(dists), 1) if dists else None,
            "near_dup_pairs": len(local_viol),
        })

    cross_viol = []
    if cross:
        all_good = [p for imgs in sets.values() for p in imgs if p in hashes]
        for a, b in itertools.combinations(sorted(set(all_good)), 2):
            if os.path.dirname(a) == os.path.dirname(b):
                continue  # 同目录已在上面统计
            dist = hamming(hashes[a], hashes[b])
            if dist <= max_distance:
                cross_viol.append({
                    "a": _rel(a, root), "b": _rel(b, root),
                    "distance": dist, "similarity_pct": similarity_pct(dist, bits),
                })

    # 可选：人脸一致性（仅在装了 insightface 时）
    face = _face_consistency(sets, hashes, logger)

    violations.sort(key=lambda x: x["distance"])
    cross_viol.sort(key=lambda x: x["distance"])

    report = {
        "root": root,
        "max_distance": max_distance,
        "bits": bits,
        "dirs_scanned": len(sets),
        "images_hashed": len(hashes),
        "unreadable": unreadable,
        "near_dup_total": len(violations),
        "near_dup_cross_dir": len(cross_viol),
        "sets": set_stats,
        "near_dup_pairs": violations[:top],
        "near_dup_cross": cross_viol[:top],
        "face_consistency": face,
    }
    _print_report(report, logger)
    return report


def _face_consistency(sets, hashes, logger):
    """若装了 insightface：对每个目录内的人脸算两两余弦，报告最低相似度。

    返回 None 表示未启用（未装 insightface）。失败一律 fail-open 返回 None。
    """
    try:
        import insightface  # noqa: F401
        import cv2  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    try:  # pragma: no cover —— 依赖外部模型，仅在装了的环境里跑
        app = insightface.app.FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1)
        out = []
        for d, imgs in sets.items():
            embs = []
            for p in imgs:
                if p not in hashes:
                    continue
                img = cv2.imread(p)
                if img is None:
                    continue
                for f in app.get(img):
                    embs.append(f.normed_embedding)
                    break  # 每图取最大脸
            if len(embs) < 2:
                continue
            sims = []
            for a, b in itertools.combinations(embs, 2):
                sims.append(float(np.dot(a, b)))
            out.append({"dir": d, "faces": len(embs),
                        "min_cosine": round(min(sims), 3),
                        "mean_cosine": round(sum(sims) / len(sims), 3)})
        return out
    except Exception as e:  # noqa: BLE001
        logger("人脸一致性检测失败（忽略）：%s" % e)
        return None


def _print_report(report: dict, logger):
    logger("=" * 68)
    logger("分镜多样性离线守卫（dHash 近重复检测）")
    logger("  根目录：%s" % report["root"])
    logger("  阈值：汉明距离 ≤ %d / %d（≈ ≥%.0f%% 相似视为近重复）"
           % (report["max_distance"], report["bits"],
              100.0 * (1 - report["max_distance"] / float(report["bits"]))))
    logger("  扫描目录：%d　图片：%d　不可读：%d"
           % (report["dirs_scanned"], report["images_hashed"], len(report["unreadable"])))
    logger("=" * 68)
    for s in report["sets"]:
        flag = "  ⚠ 近重复 %d 对" % s["near_dup_pairs"] if s["near_dup_pairs"] else ""
        logger("  %-40s 镜=%d 最小距离=%s 平均=%s%s"
               % (s["dir"][:40], s["shots"], s["min_distance"], s["mean_distance"], flag))
    if report["near_dup_pairs"]:
        logger("-" * 68)
        logger("可疑近重复（同目录，前 %d 对）：" % len(report["near_dup_pairs"]))
        for v in report["near_dup_pairs"]:
            logger("  d=%2d (%.1f%%)  %s  ⇔  %s"
                   % (v["distance"], v["similarity_pct"], v["a"], v["b"]))
    if report["near_dup_cross"]:
        logger("-" * 68)
        logger("可疑近重复（跨目录，前 %d 对）：" % len(report["near_dup_cross"]))
        for v in report["near_dup_cross"]:
            logger("  d=%2d (%.1f%%)  %s  ⇔  %s"
                   % (v["distance"], v["similarity_pct"], v["a"], v["b"]))
    if report.get("face_consistency"):
        logger("-" * 68)
        logger("人脸一致性（InsightFace 余弦，越低越可疑）：")
        for r in report["face_consistency"]:
            logger("  %-40s 脸=%d min=%s mean=%s"
                   % (r["dir"][:40], r["faces"], r["min_cosine"], r["mean_cosine"]))
    logger("=" * 68)
    n = report["near_dup_total"] + report["near_dup_cross_dir"]
    logger("结论：近重复总计 %d 对（同目录 %d + 跨目录 %d）"
           % (n, report["near_dup_total"], report["near_dup_cross_dir"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="分镜多样性离线守卫：用 dHash 量化「面板雷同」（只读，退出码即结论）")
    ap.add_argument("--root", required=True, help="分镜图根目录（项目目录或其下某集目录）")
    ap.add_argument("--max-distance", type=int, default=6, dest="max_distance",
                    help="dHash 汉明距离阈值（0-64），≤ 视为近重复。默认 6")
    ap.add_argument("--cross", action="store_true", help="额外做跨目录全局两两比对")
    ap.add_argument("--min-shots", type=int, default=2, dest="min_shots",
                    help="目录内图片数 < 此值则跳过。默认 2")
    ap.add_argument("--top", type=int, default=30, help="最多打印多少对。默认 30")
    ap.add_argument("--strict", action="store_true",
                    help="发现超阈值近重复时退出码为 1（默认纯报告，恒 0）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args(argv)

    if not (0 <= args.max_distance <= 64):
        sys.stderr.write("错误：--max-distance 必须在 0..64。\n")
        return 2
    if not os.path.isdir(args.root):
        sys.stderr.write("错误：--root 不是目录：%s\n" % args.root)
        return 2

    logger = (lambda *_a, **_k: None) if args.json else print
    try:
        report = analyze(args.root, args.max_distance, args.cross,
                         args.min_shots, args.top, logger=logger)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("错误：分析失败：%s\n" % e)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.strict and (report["near_dup_total"] or report["near_dup_cross_dir"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
