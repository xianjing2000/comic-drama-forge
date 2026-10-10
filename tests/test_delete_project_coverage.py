# -*- coding: utf-8 -*-
"""删除项目「关联数据是否都跟着删」的回归测试（2026-10-10 排查）。

排查结论：
  · 主删除覆盖 25 类产物目录（project_kind_roots）+ 9 类关联簿记
    （ai_chat / task_store / autopilot / continuity / output 根散目录 /
     ai_memory / task_lease / agent_core / 孤儿小说），这部分是完整的。
  · 但 loose_purge 的「保留名」是**手工枚举的 11 个**，而 output/ 根下实际有
    25+ 个系统目录 —— 漏掉的 asset_lib（跨项目角色资产库！）/ analytics /
    caption_verify / video / workflows_export 一旦与项目名撞名，删项目会把
    **跨项目共享**的资源当产物移进回收站。
本测试钉死「保留名必须覆盖所有系统目录」。
"""
import os
import sys
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


def _reserved_names():
    """复刻 delete_project 里 loose_purge 的保留名推导。"""
    import config
    import project_store as PS
    out = os.path.abspath(config.PROJECT_OUTPUT_DIR)
    res = {os.path.basename(os.path.normpath(d)) for _, d in PS.project_kind_roots()}
    for cn in dir(config):
        if cn.startswith("_"):
            continue
        cv = getattr(config, cn, None)
        if isinstance(cv, str) and cv and os.path.abspath(cv).startswith(out + os.sep):
            res.add(os.path.relpath(os.path.abspath(cv), out).split(os.sep)[0])
    res.update({
        "projects", "novels", "ai_chat", "lessons", "memory", "comic_drama",
        "assets", "asset_lib", "analytics", "caption_verify", "workflows_export",
        "video", "temp", ".leases", "_te3d_render", "_trash",
    })
    return res


class TestLoosePurgeReserved(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("MJSCXT_DATA_DIR",
                              os.path.join(os.environ.get("APPDATA", ""),
                                           "mjscxt-desktop", "mjscxt-data"))
        os.environ["MJSCXT_AUTOPILOT"] = "0"

    def test_shared_dirs_protected(self):
        """跨项目共享目录必须在保留名里 —— 误删会毁掉所有项目的复用资产。"""
        res = _reserved_names()
        for name in ("asset_lib", "analytics", "caption_verify",
                     "workflows_export", "video", "lessons", "memory",
                     "ai_chat", "comic_drama"):
            self.assertIn(name, res, "%s 未受保护，项目名撞上会被误删" % name)

    def test_all_kind_roots_protected(self):
        """project_kind_roots 的每一类根目录名都必须在保留名里。"""
        import project_store as PS
        res = _reserved_names()
        for kind, d in PS.project_kind_roots():
            base = os.path.basename(os.path.normpath(d))
            self.assertIn(base, res, "kind_roots 的 %s(%s) 未受保护" % (kind, base))

    def test_config_output_dirs_protected(self):
        """config 里所有位于 PROJECT_OUTPUT_DIR 之下的目录常量都受保护。"""
        import config
        out = os.path.abspath(config.PROJECT_OUTPUT_DIR)
        res = _reserved_names()
        for cn in dir(config):
            if cn.startswith("_"):
                continue
            cv = getattr(config, cn, None)
            if not isinstance(cv, str) or not cv:
                continue
            ap = os.path.abspath(cv)
            if not ap.startswith(out + os.sep):
                continue
            top = os.path.relpath(ap, out).split(os.sep)[0]
            self.assertIn(top, res, "config.%s(%s) 未受保护" % (cn, top))

    def test_delete_nonexistent_raises_keyerror(self):
        """不存在的项目 → KeyError（先于 confirm 检查）。"""
        import project_store as PS
        with self.assertRaises(KeyError):
            PS.delete_project("__不存在的项目__", confirm=False)

    def test_delete_existing_requires_confirm(self):
        """存在的项目、未显式 confirm → PermissionError（防误触；不会真删）。"""
        import project_store as PS
        idx = PS.load_index() or {}
        recs = idx.get("projects") or []
        if not recs:
            self.skipTest("当前没有项目可供验证 confirm 守卫")
        ref = recs[0].get("id")
        with self.assertRaises(PermissionError):
            PS.delete_project(ref, confirm=False)


class TestProjectKindRootsCoverage(unittest.TestCase):

    def test_covers_known_artifact_dirs(self):
        """已知的产物目录类别必须在 project_kind_roots 里（漏登记会导致删除残留）。"""
        import project_store as PS
        kinds = {k for k, _ in PS.project_kind_roots()}
        for k in ("scripts", "screenplays", "characters", "items", "scenes",
                  "storyboards", "videos", "final", "upscale", "dub", "qc",
                  "keyframes", "continuity", "sfx", "autopilot", "autonomous",
                  "exports", "export"):
            self.assertIn(k, kinds, "产物类别 %s 未登记 → 删除会残留" % k)


class TestAutoScanOutputSubdirs(unittest.TestCase):
    """**自动扫描**：代码里出现的每一个 output/ 一级目录，必须要么登记为产物类别
    （project_kind_roots，删除时按项目名收走），要么在保留清单里（跨项目共享，
    删除时必须保留）。两条都不占 → 测试失败。

    为什么需要它：project_kind_roots 的注释写着「新增产物目录时必须同步登记到这里，
    否则删除会再次漏」—— 说明历史上已因漏登记踩过坑（screenplays / sfx 都是事后补的，
    caption_verify 是 2026-10-10 扫描才发现的）。手工维护的清单迟早会漏，
    故用代码扫描把它变成**自动守卫**：新增目录若没登记，CI 直接失败。
    """

    #: 跨项目共享 / 非项目产物：删除项目时**必须保留**（不进 kind_roots）
    SHARED_DIRS = {
        "asset_lib",      # 跨项目角色资产库（按形象指纹分）
        "analytics",      # 全局成本/事件
        "lessons",        # 教训库
        "memory",         # AI 记忆
        "projects",       # 项目工作区本身（由主删除单独处理）
        "ai_chat",        # 会话数据（由 ai_chat.purge_project 单独处理）
        "assets",         # 容器目录（characters/items/scenes 在 kind_roots 里）
        "comic_drama",    # 容器目录（comic_drama/* 在 kind_roots 里）
    }

    def _scan_output_subdirs(self):
        """扫出代码里 os.path.join(PROJECT_OUTPUT_DIR, "字面量") 的一级名。"""
        import re
        app = APP
        pat = re.compile(
            r"""os\.path\.join\(\s*PROJECT_OUTPUT_DIR\s*,\s*["']([A-Za-z0-9_\-]+)["']""")
        hits = {}
        for dp, dn, fn in os.walk(app):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fn:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(dp, f)
                rel = os.path.relpath(fp, app).replace(os.sep, "/")
                for i, line in enumerate(open(fp, encoding="utf-8").read().splitlines(), 1):
                    for m in pat.finditer(line):
                        hits.setdefault(m.group(1), []).append("%s:%d" % (rel, i))
        return hits

    def test_every_output_subdir_is_registered_or_shared(self):
        import project_store as PS
        kinds = {k for k, _ in PS.project_kind_roots()}
        # kind_roots 的根目录名（含两层结构的首段，如 comic_drama）
        kind_dirs = set()
        for _, d in PS.project_kind_roots():
            kind_dirs.add(os.path.basename(os.path.normpath(d)))
            try:
                import config
                rel = os.path.relpath(os.path.abspath(d),
                                      os.path.abspath(config.PROJECT_OUTPUT_DIR))
                kind_dirs.add(rel.split(os.sep)[0])
            except Exception:  # noqa: BLE001
                pass
        hits = self._scan_output_subdirs()
        self.assertTrue(hits, "扫描没抓到任何目录，正则可能需要更新")
        unregistered = []
        for name, where in sorted(hits.items()):
            if name in self.SHARED_DIRS:
                continue
            if name in kind_dirs:
                continue
            # 也允许 config 常量存在（那说明它是系统目录，保留清单会覆盖）
            unregistered.append((name, where[0]))
        self.assertEqual(
            unregistered, [],
            "以下 output 一级目录既未登记为产物类别、也不在共享清单里 —— "
            "新增产物目录请登记到 project_store.project_kind_roots()；"
            "若是跨项目共享目录，请加入本测试的 SHARED_DIRS：%s" % unregistered)

    def test_shared_dirs_never_in_kind_roots(self):
        """共享目录不得出现在 kind_roots 里 —— 否则删项目会误删跨项目资源。"""
        import project_store as PS
        dirs = {os.path.basename(os.path.normpath(d)) for _, d in PS.project_kind_roots()}
        for name in ("asset_lib", "analytics", "lessons", "memory"):
            self.assertNotIn(name, dirs,
                             "%s 是跨项目共享目录，不该被当成项目产物删除" % name)


if __name__ == "__main__":
    unittest.main()
