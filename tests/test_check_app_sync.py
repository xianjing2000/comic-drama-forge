# -*- coding: utf-8 -*-
"""tools/check_app_sync.py 的回归测试（2026-10-12 t9）。

钉住本轮修的两处【覆盖缺口】——这两处正是 9 个文件长期漂移却报「一致」的根因：
  ① scan() 只 listdir 顶层、不递归子目录 → routes/ workers/ providers/ 整个漏掉；
  ② default_targets() 不含 electron-app/_backend/app → 那个副本根本没被监控。
以及打包产物（dist / 便携版）必须被标记为「允许陈旧」而不是失败。

只用 tmp 目录做源/目标，不碰真实四副本（不干扰生产）。
运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(os.path.dirname(_HERE), "tools")
_CHECKER = os.path.join(_TOOLS, "check_app_sync.py")


def _load():
    """按文件路径加载工具模块（tools/ 不是包，也没有 .py 之外的入口）。"""
    spec = importlib.util.spec_from_file_location("_check_app_sync_under_test", _CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAS = _load()


def _touch(root, rel, body=b"# x"):
    p = os.path.join(root, rel)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(body)
    return p


class TestScanRecursive(unittest.TestCase):
    """① 递归：子目录文件必须进比对表，且键是 POSIX 相对路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="syncscan_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_subdir_files_are_scanned(self):
        _touch(self.tmp, "top.py")
        _touch(self.tmp, "routes/agent.py")
        _touch(self.tmp, "routes/autopilot.py")
        _touch(self.tmp, "workers/deep.py")
        _touch(self.tmp, "providers/p1/nested.py")
        got = CAS.scan(self.tmp)
        self.assertIn("top.py", got)
        self.assertIn("routes/agent.py", got)
        self.assertIn("routes/autopilot.py", got)
        self.assertIn("workers/deep.py", got)
        self.assertIn("providers/p1/nested.py", got)

    def test_keys_use_posix_relative_paths(self):
        _touch(self.tmp, "routes/agent.py")
        got = CAS.scan(self.tmp)
        self.assertEqual(sorted(got), ["routes/agent.py"])
        # 反例钉死：键不能是纯文件名，也不能带反斜杠（否则跨机比对会假不一致）
        self.assertNotIn("agent.py", got)
        self.assertFalse(any(chr(92) in k for k in got))

    def test_pycache_and_snapshot_ignored(self):
        _touch(self.tmp, "real.py")
        _touch(self.tmp, "__pycache__/real.py")
        _touch(self.tmp, "real.pyc")
        _touch(self.tmp, "old.bak_20261012.py")
        got = CAS.scan(self.tmp)
        self.assertEqual(sorted(got), ["real.py"])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(CAS.scan(os.path.join(self.tmp, "nope")), {})

    def test_subdir_drift_is_detected(self):
        """子目录内容不同 → 必须判 diff（这正是旧实现的盲区）。"""
        src = os.path.join(self.tmp, "src")
        dst = os.path.join(self.tmp, "dst")
        _touch(src, "routes/agent.py", b"A")
        _touch(dst, "routes/agent.py", b"B")
        res = CAS.compare(CAS.scan(src), CAS.scan(dst))
        self.assertEqual(res["diff"], ["routes/agent.py"])
        self.assertEqual(res["missing"], [])
        self.assertEqual(res["extra"], [])

    def test_extra_and_missing_in_subdir(self):
        src = os.path.join(self.tmp, "src")
        dst = os.path.join(self.tmp, "dst")
        _touch(src, "routes/keep.py", b"K")
        _touch(src, "routes/gone.py", b"G")
        _touch(dst, "routes/keep.py", b"K")
        _touch(dst, "routes/only_here.py", b"H")
        res = CAS.compare(CAS.scan(src), CAS.scan(dst))
        self.assertEqual(res["missing"], ["routes/gone.py"])
        self.assertEqual(res["extra"], ["routes/only_here.py"])


class TestDefaultTargets(unittest.TestCase):
    """② default_targets() 必须含 electron-app/_backend/app，且打包产物标 allow_stale。"""

    def test_targets_are_path_allow_stale_pairs(self):
        for item in CAS.default_targets():
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)
            self.assertIsInstance(item[1], bool)

    def test_electron_backend_app_is_monitored(self):
        paths = [str(p) for p, _ in CAS.default_targets()]
        sep = chr(92)
        hit = [p for p in paths
               if p.endswith("electron-app" + sep + "_backend" + sep + "app")
               or p.endswith("electron-app/_backend/app")]
        self.assertEqual(len(hit), 1,
                         "electron-app/_backend/app 必须在默认目标里：%s" % paths)

    def test_real_copies_are_strict(self):
        strict = [str(p) for p, allow in CAS.default_targets() if not allow]
        self.assertTrue(any("mjscxt_p139" in p for p in strict))
        self.assertTrue(any("resource-mirror" in p for p in strict))
        self.assertTrue(any("_backend" in p for p in strict))

    def test_packaged_artifacts_are_allow_stale(self):
        stale = [str(p) for p, allow in CAS.default_targets() if allow]
        self.assertTrue(any("win-unpacked" in p for p in stale), stale)
        self.assertTrue(any("_dist" in p for p in stale), stale)

    def test_stale_entries_come_after_strict(self):
        """防回归：不要把 electron-app/_backend/app 归到允许陈旧那一组。"""
        for p, allow in CAS.default_targets():
            if str(p).endswith("electron-app" + chr(92) + "_backend" + chr(92) + "app"):
                self.assertFalse(allow)


class TestIgnoredExtra(unittest.TestCase):
    """已判定的历史残留豁免：只豁免 extra，绝不豁免 diff / missing。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="syncign_")
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        _touch(self.src, "a.py", b"A")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_declared_residue_is_registered(self):
        self.assertIn("templates/build_v2.py", CAS.IGNORED_EXTRA)

    def test_ignored_extra_not_reported(self):
        _touch(self.dst, "a.py", b"A")
        _touch(self.dst, "templates/build_v2.py", b"legacy")
        res = CAS.compare(CAS.scan(self.src), CAS.scan(self.dst), CAS.IGNORED_EXTRA)
        self.assertEqual(res["extra"], [])
        self.assertEqual(res["ignored_extra"], ["templates/build_v2.py"])

    def test_unignored_extra_still_reported(self):
        """防回归：未登记的新增文件必须报出来，不能被豁免名单吃掉。"""
        _touch(self.dst, "a.py", b"A")
        _touch(self.dst, "templates/something_new.py", b"new")
        res = CAS.compare(CAS.scan(self.src), CAS.scan(self.dst), CAS.IGNORED_EXTRA)
        self.assertEqual(res["extra"], ["templates/something_new.py"])

    def test_ignore_never_hides_diff_or_missing(self):
        """豁免名单只能用于 extra；diff / missing 必须照报。"""
        _touch(self.dst, "a.py", b"CHANGED")
        _touch(self.dst, "templates/build_v2.py", b"legacy")
        res = CAS.compare(CAS.scan(self.src), CAS.scan(self.dst), CAS.IGNORED_EXTRA)
        self.assertEqual(res["diff"], ["a.py"])
        self.assertEqual(res["extra"], [])

    def test_missing_still_reported_with_ignore(self):
        _touch(self.dst, "templates/build_v2.py", b"legacy")
        res = CAS.compare(CAS.scan(self.src), CAS.scan(self.dst), CAS.IGNORED_EXTRA)
        self.assertEqual(res["missing"], ["a.py"])


class TestMainExitCode(unittest.TestCase):
    """退出码语义：严格目标不一致 → 1；一致 → 0；源不存在 → 2。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="syncmain_")
        self.src = os.path.join(self.tmp, "src")
        self.strict_dst = os.path.join(self.tmp, "strict")
        self.stale_dst = os.path.join(self.tmp, "stale")
        _touch(self.src, "a.py", b"A")
        _touch(self.src, "routes/b.py", b"B")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explicit_target_subdir_mismatch_returns_1(self):
        _touch(self.strict_dst, "a.py", b"A")           # 顶层一致
        _touch(self.strict_dst, "routes/b.py", b"X")    # 子目录漂移
        self.assertEqual(CAS.main(["--source", self.src,
                                   "--target", self.strict_dst]), 1)

    def test_explicit_target_identical_returns_0(self):
        _touch(self.strict_dst, "a.py", b"A")
        _touch(self.strict_dst, "routes/b.py", b"B")
        self.assertEqual(CAS.main(["--source", self.src,
                                   "--target", self.strict_dst]), 0)

    def test_json_mode_returns_1_on_drift(self):
        _touch(self.strict_dst, "a.py", b"A")
        _touch(self.strict_dst, "routes/b.py", b"X")
        self.assertEqual(CAS.main(["--source", self.src, "--target",
                                   self.strict_dst, "--json"]), 1)

    def test_missing_source_returns_2(self):
        self.assertEqual(CAS.main(["--source",
                                   os.path.join(self.tmp, "no_such_dir")]), 2)


if __name__ == "__main__":
    unittest.main()
