# -*- coding: utf-8 -*-
r"""回归：ComfyUI 派生路径必须在「只自动检测、未显式配置」时也正确。

2026-10-11 真实故障（画面已生成却全部入库失败）：

    config.py:52   COMFYUI_ROOT = _env("COMFYUI_ROOT", "")        # ← 没有回退到检测值
    config.py:85   _DERIVED = _derive_comfyui_paths(COMFYUI_ROOT)  # 传空 → 全空
    config.py:89   COMFYUI_OUTPUT_DIR = _norm_path(_env(..., _DERIVED["output"]))  # ''

链路后果：搬移 src 退化成相对路径 `comic_drama\项目\scene\x.png`，
相对**服务进程 CWD** 解析 → WinError 3（系统找不到指定的路径）：

    WARNING:app:产物搬移失败（已重试 5 次）：comic_drama\...\吊顶夹层通道_00001.png
        -> ...\assets_scratch\scene_吊顶夹层通道\base_try1.png：[WinError 3]
    ERROR:asset_worker:资产「吊顶夹层通道」生成失败（已隔离，继续后续资产）

而 ComfyUI 侧**出图完全正常**（它自己按相对路径解析到安装目录的 output/），
所以表面现象是「图生成了、但一个都进不了库、逐个资产被隔离」，
且 scratch 子目录只有 character_/item_ 而没有 scene_，极易误判成场景专有问题。
"""
import os
import sys
import unittest

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import config as C  # noqa: E402


class TestComfyUIDerivedPaths(unittest.TestCase):
    def test_root_falls_back_to_detected_dir(self):
        """只要检测到了 ComfyUI 根，COMFYUI_ROOT 就必须回退到它（而非留空）。"""
        if not C.MJSCXT_COMFYUI_DIR:
            self.skipTest('本机未检测到 ComfyUI 安装目录，无法验证派生路径')
        self.assertTrue(C.COMFYUI_ROOT,
                        'COMFYUI_ROOT 为空 —— 没有回退到自动检测出的 MJSCXT_COMFYUI_DIR')

    def test_output_dir_not_empty_when_root_known(self):
        """核心断言：输出目录为空 = 产物搬移会退化成相对路径 = 必然搬移失败。"""
        if not C.MJSCXT_COMFYUI_DIR:
            self.skipTest('本机未检测到 ComfyUI 安装目录')
        self.assertTrue(C.COMFYUI_OUTPUT_DIR,
                        'COMFYUI_OUTPUT_DIR 为空：搬移 src 会退化成相对路径，落盘必然失败')

    def test_derived_paths_under_root(self):
        """派生路径都应位于检测到的 ComfyUI 根之下（防止回退成别的盘符/空串）。"""
        if not C.MJSCXT_COMFYUI_DIR:
            self.skipTest('本机未检测到 ComfyUI 安装目录')
        root = os.path.normcase(os.path.normpath(C.MJSCXT_COMFYUI_DIR))
        for name in ('COMFYUI_OUTPUT_DIR', 'COMFYUI_INPUT_DIR', 'COMFYUI_TEMP_DIR'):
            p = os.path.normcase(os.path.normpath(getattr(C, name)))
            self.assertTrue(p.startswith(root),
                            '%s=%s 不在 ComfyUI 根 %s 之下' % (name, p, root))

    def test_output_dir_is_absolute(self):
        """必须是绝对路径 —— 相对路径正是本次故障的直接成因。"""
        if not C.COMFYUI_OUTPUT_DIR:
            self.skipTest('本机 COMFYUI_OUTPUT_DIR 未派生（未装 ComfyUI）')
        self.assertTrue(os.path.isabs(C.COMFYUI_OUTPUT_DIR),
                        'COMFYUI_OUTPUT_DIR 不是绝对路径：%s' % C.COMFYUI_OUTPUT_DIR)


if __name__ == '__main__':
    unittest.main()