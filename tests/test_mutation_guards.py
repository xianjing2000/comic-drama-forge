# -*- coding: utf-8 -*-
"""「新增的测试真的拦得住老缺陷吗」—— 变异验证（mutation check）。

为什么需要这个文件（t22 的诚实性要求）：
    一个**永远不会红**的测试等于没有测试。本轮新增的测试都是针对
    **已经修好的缺陷**写的，跑绿只能说明「当前代码没问题」，
    不能说明「测试能拦住它」。所以这里用**运行时变异**（不改 app/ 生产代码、
    不写盘），把每个老缺陷的**旧实现**塞回模块命名空间，断言对应测试当场变红。

做法：
    · 老实现只 patch 在**内存里的模块属性**（unittest.mock.patch.object）；
    · 每个用例 = 注入老实现 → 跑那条测试 → 断言它 FAIL；跑完自动还原。
    · 若某天有人把断言删空/写软，这里会立刻红（因为老实现不再让测试失败）。

覆盖：
    ① _seg_sort_key 取扩展名里的数字（本会话已修的 bug）；
    ② read_json_strict 调用点漏传 default（AST 门禁的拦截面）；
    ③ _sb_structural_defect 词表缺手指畸变族（T18 P0 漏判）。
"""
import importlib
import os
import re
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.abspath(os.path.join(_HERE, os.pardir, "app"))
for _p in (_APP, _HERE):          # tests/ 也要进 path，才能 import 兄弟测试模块
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --------------------------------------------------------------------------- #
# ① _seg_sort_key：修复前的实现（直接 findall 取最后一个数字）
# --------------------------------------------------------------------------- #
def _old_seg_sort_key(path):
    """修复前的实现（2026-10-12 之前）：不剥扩展名。

    mp4 自带数字 4 → re.findall(r"[0-9]+", "a_00010_.mp4") = ['00010', '4']
    → nums[-1] 恒等于 4 → 所有文件排序键都一样 → 成片画面乱序。
    """
    nums = re.findall(r"(\d+)", str(path or ""))
    if not nums:
        return (1, 0)
    try:
        return (0, int(nums[-1]))
    except Exception:
        return (1, 0)


class TestMutationsAreCaught(unittest.TestCase):
    """每条：注入老实现 → 对应测试必须 FAIL。"""

    def _assert_test_fails_under_mutation(self, module, attr, mutant, testcase):
        import unittest as _ut
        case = _ut.TestLoader().loadTestsFromName(testcase)
        flat = list(_ut.TestSuite(case))
        self.assertTrue(flat, "用例加载不到：%s" % testcase)
        results = []
        for one in flat:
            with mock.patch.object(module, attr, mutant):
                r = _ut.TestResult()
                one.run(r)
            results.append(r)
        failed = [r for r in results if not r.wasSuccessful()]
        self.assertTrue(
            failed,
            "注入老实现后 %s 依然全绿 —— 这条测试拦不住该缺陷（等于没测）" % testcase)

    def test_seg_sort_key_old_impl_is_caught(self):
        """⭐ 老 _seg_sort_key（不剥扩展名）必须让排序测试变红。"""
        import video_helpers
        self._assert_test_fails_under_mutation(
            video_helpers, "_seg_sort_key", _old_seg_sort_key,
            "tests.test_video_concat_audio.TestSegSortKey")

    def test_good_seg_sort_key_passes(self):
        """对照：真实现下同一批测试全绿（证明上一条的红是变异引起的）。"""
        import unittest as _ut
        import video_helpers  # noqa: F401
        suite = _ut.TestLoader().loadTestsFromName(
            "tests.test_video_concat_audio.TestSegSortKey")
        r = _ut.TestResult()
        suite.run(r)
        self.assertTrue(r.wasSuccessful(),
                        "真实现下应全绿：%s" % (r.failures + r.errors))

    def test_read_json_strict_ast_gate_catches_missing_default(self):
        """⭐ 把 app/ 全部调用点改成单参数版本（AST 变异），契约测试必须变红。"""
        import ast
        target = os.path.join(_APP, "pipeline.py")
        with open(target, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=target)
        found = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else getattr(fn, "attr", None))
            if name == "read_json_strict":
                if node.args:
                    node.args = node.args[:1]           # 丢掉 default
                    node.keywords = []
                    found += 1
        self.assertGreater(found, 0, "pipeline.py 里没找到调用点，变异无效")
        mutated = ast.unparse(tree)

        # 把变异源码写到临时目录，并把契约测试的扫描根指过去（只扫这一个文件）。
        # ⚠️ 不能只改 case.files —— setUp() 每轮都会用扫描根重置它。
        import tempfile
        import test_recompose_closure as trc
        with tempfile.TemporaryDirectory() as td:
            mut_path = os.path.join(td, "pipeline_mutated.py")
            with open(mut_path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            case = trc.TestReadJsonStrictCallSites("test_every_call_passes_default")
            with mock.patch.object(trc, "_APP", td):
                r = unittest.TestResult()
                case.run(r)
        self.assertFalse(
            r.wasSuccessful(),
            "漏传 default 的调用点没被 AST 门禁抓到 —— 契约测试失效")

    def test_sb_table_missing_finger_family_is_caught(self):
        """⭐ 把 sb 词表里的手指畸变族删掉（复现 T18 P0），软放行测试必须变红。"""
        import sb_helpers
        import test_sb_soft_gate_sync as tsg
        stripped = tuple(k for k in sb_helpers._SB_STRUCTURAL_DEFECT_KEYWORDS
                         if k not in ("九宫格手指畸变", "手指粘连", "指头粘连",
                                      "手指融合", "多指畸形", "六指",
                                      "杵状指", "指尖膨大", "指尖钝化", "指尖球状",
                                      "手指粗细不均", "指节模糊"))
        self.assertLess(len(stripped), len(sb_helpers._SB_STRUCTURAL_DEFECT_KEYWORDS),
                        "一个词都没删掉，变异无效")
        case = tsg.TestKeywordTableSync("test_soft_gate_flags_production_finger_defect_text")
        case.setUpClass()
        with mock.patch.object(sb_helpers, "_SB_STRUCTURAL_DEFECT_KEYWORDS", stripped):
            r = unittest.TestResult()
            case.run(r)
        self.assertFalse(
            r.wasSuccessful(),
            "删掉手指畸变族后软放行测试依然全绿 —— 它就是当年漏判的那个盲区")


if __name__ == "__main__":
    unittest.main(verbosity=2)
