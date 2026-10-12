# -*- coding: utf-8 -*-
"""「交付物 stale 后仍可验收」闭环回归测试（t22 测试层新增，2026-10-12）。

背景（t11 审计 + t17 修复）：
    单镜返工后成片是旧的，交付登记里只把 meta.stale 写成一个**界面提示**，
    review 仍是 accepted —— 用户以为自己批的是当前这版，其实批的是返工前那版，
    旧结论被静默延续到新内容上。这是「给返工前的画面盖章交付」。
    修法（app/pipeline.py mark_deliverable_stale / set_deliverable_review）：
      · 打标时把 review 从 accepted/rejected 降回 pending 并摘掉 approval 绑定；
      · 验收时再硬拦一道：meta.stale 非空即拒绝 accepted。

本测试把这条状态机钉死：**任何一环被回退，这里都会红。**
不依赖网络 / 不依赖 ffmpeg，只碰临时目录下的 deliverables.json。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)


def _load_pipeline():
    """导入宿主 app + pipeline（pipeline._A() 需要 sys.modules['app'] 存在）。"""
    import app  # noqa: F401  仅为满足 pipeline._A() 的宿主解析
    import pipeline
    return app, pipeline


class TestStaleDeliverableNotAcceptable(unittest.TestCase):

    PROJECT = "t22_stale_probe"
    EP = 1

    @classmethod
    def setUpClass(cls):
        cls.app, cls.pipeline = _load_pipeline()

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="t22_stale_")
        self._orig_out = getattr(self.app, "PROJECT_OUTPUT_DIR", None)
        self.app.PROJECT_OUTPUT_DIR = self.td
        self.idx = os.path.join(self.td, "autopilot", self.PROJECT, "deliverables.json")
        os.makedirs(os.path.dirname(self.idx), exist_ok=True)
        # 假的成片文件（登记时需要能取 size）
        self.film = os.path.join(self.td, "ep01_full.mp4")
        with open(self.film, "wb") as f:
            f.write(b"FAKE-FILM-CONTENT")

    def tearDown(self):
        if self._orig_out is None:
            try:
                del self.app.PROJECT_OUTPUT_DIR
            except AttributeError:
                pass
        else:
            self.app.PROJECT_OUTPUT_DIR = self._orig_out
        shutil.rmtree(self.td, ignore_errors=True)

    # ---------------- 工具 ----------------
    def _item(self):
        with open(self.idx, "r", encoding="utf-8") as f:
            return (json.load(f).get("items") or {}).get(str(self.EP)) or {}

    def _register(self, meta=None):
        return self.pipeline.record_deliverable(self.PROJECT, self.EP, self.film,
                                                meta=meta or {})

    def _mark_stale(self, reason="单镜返工：shot_07 已重做"):
        return self.pipeline.mark_deliverable_stale(
            self.PROJECT, self.EP, reason, detail={"shot_id": "7"})

    def _review(self, review, note=""):
        return self.pipeline.set_deliverable_review(self.PROJECT, self.EP, review, note=note)

    # ---------------- 用例 ----------------
    def test_fresh_deliverable_can_be_accepted(self):
        """基线：刚登记（无 stale）的成片可以正常验收 —— 别把正常路一起堵死。"""
        self._register()
        item = self._review("accepted")
        self.assertEqual(item.get("review"), "accepted")
        self.assertTrue(item.get("approval"), "验收应写入哈希绑定")

    def test_marking_stale_demotes_accepted_to_pending(self):
        """⭐ 打标必须让「已验收」在状态机里真实失效（不只是界面提示）。"""
        self._register()
        self._review("accepted")
        self.assertEqual(self._item().get("review"), "accepted")

        result = self._mark_stale()
        self.assertTrue(result, "打标应返回该集条目")
        item = self._item()
        self.assertEqual(item.get("review"), "pending",
                         "review 未降级 → 旧结论会被静默延续到返工前的成片")
        self.assertNotIn("approval", item, "打标必须摘掉验收绑定")
        self.assertTrue((item.get("meta") or {}).get("stale"),
                        "meta.stale 需保留（界面据此显示原因）")

    def test_accepting_a_stale_deliverable_is_refused(self):
        """⭐⭐ 核心断言：过期成片点「通过」必须被硬拦（这是当年漏掉的那道闸）。"""
        self._register()
        self._mark_stale("单镜返工：shot_07 已重做")
        with self.assertRaises(self.pipeline.PipelineError) as ctx:
            self._review("accepted")
        msg = str(ctx.exception)
        self.assertIn("过期", msg, "报错应说明「成片已过期」而不是泛泛失败")
        self.assertIn("shot_07", msg, "报错应带上失效原因，便于用户定位")
        self.assertEqual(self._item().get("review"), "pending",
                         "被拦下的验收不得留下任何状态改动")

    def test_rejecting_a_stale_deliverable_is_still_allowed(self):
        """打回（rejected）永远允许 —— 过期成片也要能被否掉。"""
        self._register()
        self._mark_stale()
        item = self._review("rejected", note="画面是旧的")
        self.assertEqual(item.get("review"), "rejected")

    def test_recompose_then_reregister_clears_stale_and_allows_accept(self):
        """闭环：重新合成 → 重新登记 → stale 消失 → 可以验收。"""
        self._register()
        self._mark_stale()
        with self.assertRaises(self.pipeline.PipelineError):
            self._review("accepted")

        # 重新合成后重新登记（record_deliverable 会用新的 meta 覆盖旧的）
        self._register(meta={"reason": "重新合成完成"})
        self.assertFalse((self._item().get("meta") or {}).get("stale"),
                         "重新登记必须清掉 meta.stale")
        item = self._review("accepted")
        self.assertEqual(item.get("review"), "accepted", "重拼后应能正常验收")

    def test_validate_review_reports_unbound_before_acceptance(self):
        """验收有效性校验：未验收 → unbound（不是 invalid，别误伤）。"""
        self._register()
        r = self.pipeline.validate_deliverable_review(self.PROJECT, self.EP)
        self.assertEqual(r.get("status"), "unbound")

    def test_validate_review_none_when_episode_absent(self):
        self._register()
        r = self.pipeline.validate_deliverable_review(self.PROJECT, 99)
        self.assertEqual(r.get("status"), "none")

    def test_stale_marker_survives_until_recompose(self):
        """record_deliverable 不主动清 meta.stale —— 只有重新登记才替换 meta。"""
        self._register()
        self._mark_stale("镜头重做")
        meta = (self._item().get("meta") or {})
        self.assertEqual((meta.get("stale") or {}).get("reason"), "镜头重做")


if __name__ == "__main__":
    unittest.main(verbosity=2)
