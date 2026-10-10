# -*- coding: utf-8 -*-
"""shared_*.py 自由名（漏 import）回归测试。

背景（2026-10-10 实测缺陷）：_shared.py 拆分出的 shared_episode.py 漏了 import os ——
而 os 只在 _load_legacy_flat_script 「函数内部」使用，因此：
  · 模块级 import shared_episode 完全正常（真实 import 测试抓不到）；
  · 只有真正去读剧本时才抛 NameError → 剧本读取失败 → 前端「剧本概览」永远
    「暂无剧集数据」，而生产其实跑得好好的。
同类坑此前在 shared_upscale 也踩过一次（漏 FINAL_DIR/UPSCALE_DIR/VIDEOS_DIR/threading）。

本测试用 AST 扫「整个模块」（含函数体）的加载名，减去定义/导入/内置名，
任何剩余者即为漏 import —— 无论它在模块级还是函数内，一律失败。
"""
import ast
import builtins
import os
import sys
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


def free_names(path):
    """返回该模块中「加载但未定义、未导入、非内置」的名字。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    defined, imported = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.arg):
            defined.add(n.arg)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            defined.add(n.id)
        if isinstance(n, ast.alias):
            imported.add(n.asname or n.name.split(".")[0])
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                imported.add(a.asname or a.name.split(".")[0])
        if isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
    return sorted({
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    } - defined - imported - set(dir(builtins)))


class TestSharedModulesHaveNoFreeNames(unittest.TestCase):

    def test_all_shared_modules_clean(self):
        bad = {}
        for f in sorted(os.listdir(APP)):
            if f.startswith("shared_") and f.endswith(".py"):
                miss = free_names(os.path.join(APP, f))
                if miss:
                    bad[f] = miss
        self.assertEqual(bad, {},
                         "以下模块有未解析的自由名（多为漏 import，且只在函数被调用时才炸）：%s" % bad)

    def test_shared_episode_has_os(self):
        """钉死本次缺陷：shared_episode 必须能用 os（它在函数内使用）。"""
        import shared_episode as SE
        self.assertTrue(hasattr(SE, "os"),
                        "shared_episode 又丢了 os —— 读剧本会 NameError")

    def test_shared_upscale_has_config_dirs(self):
        """钉死此前那次的缺陷：shared_upscale 的模块级常量依赖这些 config 名。"""
        import shared_upscale as SU
        for n in ("FINAL_DIR", "UPSCALE_DIR", "VIDEOS_DIR", "COMFYUI_OUTPUT_DIR"):
            self.assertTrue(hasattr(SU, n), "shared_upscale 少了 %s" % n)


class TestSharedModulesAllImportable(unittest.TestCase):

    def test_import_every_shared_module(self):
        """每个 shared_*.py 都必须能真实导入（模块级错误在这里暴露）。"""
        failures = {}
        for f in sorted(os.listdir(APP)):
            if f.startswith("shared_") and f.endswith(".py"):
                mod = f[:-3]
                try:
                    __import__(mod)
                except Exception as e:  # noqa: BLE001
                    failures[mod] = "%s: %s" % (type(e).__name__, e)
        self.assertEqual(failures, {}, "以下 shared 模块导入失败：%s" % failures)


if __name__ == "__main__":
    unittest.main()
