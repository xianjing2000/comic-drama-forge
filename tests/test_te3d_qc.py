# -*- coding: utf-8 -*-
"""3D 构图基准图质检回归（2026-10-13）。

用户要求：基准图**先生成前质检**，没问题了再生成分镜图。本测试锁定判据行为：
  · 空白图 -> image_ok 不过
  · 人数不符 -> char_count 不过（有 diag 时）
  · 命中动作但姿势没应用 -> pose_applied 不过
  · 单角色站位错档 -> slot 不过
  · 景别越档 -> framing 不过
  · 缺 diag -> 相关判据记 skipped（None），不误杀
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from PIL import Image  # noqa: E402

import te_3d_qc as Q  # noqa: E402


def _mk(path, blocks, size=(640, 360), bg=(140, 140, 145)):
    """造一张「灰底 + 若干彩色块」的假基准图。blocks = [(x0,y0,x1,y1,color)]"""
    im = Image.new("RGB", size, bg)
    px = im.load()
    for x0, y0, x1, y1, col in blocks:
        for y in range(max(0, y0), min(size[1], y1)):
            for x in range(max(0, x0), min(size[0], x1)):
                px[x, y] = col
    im.save(path)
    return path


CHAR_BLUE = (47, 128, 255)


class TestBlockingQc(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.d = self.td.name

    def _p(self, name):
        return os.path.join(self.d, name)

    def test_blank_image_fails(self):
        p = _mk(self._p("blank.png"), [])
        r = Q.qc_blocking_image({"characters_in_shot": ["羡进"]}, p)
        self.assertFalse(r["ok"])
        self.assertIn("image_ok", [c["name"] for c in r["checks"] if c["ok"] is False])

    def test_char_count_from_diag(self):
        p = _mk(self._p("one.png"), [(260, 100, 380, 300, CHAR_BLUE)])
        ok = Q.qc_blocking_image({"characters_in_shot": ["羡进"]}, p, diag={"chars": 1})
        self.assertTrue(ok["ok"], ok["reasons"])
        bad = Q.qc_blocking_image({"characters_in_shot": ["羡进", "赵天霸"]}, p, diag={"chars": 1})
        self.assertFalse(bad["ok"])
        self.assertTrue(any("人偶" in x for x in bad["reasons"]))

    def test_pose_applied_required_when_action_matched(self):
        p = _mk(self._p("kneel.png"), [(260, 100, 380, 300, CHAR_BLUE)])
        shot = {"characters_in_shot": ["羡进"], "action": "他跪坐在地上"}
        bad = Q.qc_blocking_image(shot, p, diag={"chars": 1, "poseApplied": 0})
        self.assertFalse(bad["ok"])
        good = Q.qc_blocking_image(shot, p, diag={"chars": 1, "poseApplied": 41})
        self.assertTrue(good["ok"], good["reasons"])

    def test_slot_position(self):
        left = _mk(self._p("left.png"), [(40, 100, 140, 300, CHAR_BLUE)])
        right = _mk(self._p("right.png"), [(500, 100, 600, 300, CHAR_BLUE)])
        shot_left = {"characters_in_shot": ["羡进"], "blocking": [{"name": "羡进", "x": "left"}]}
        shot_right = {"characters_in_shot": ["羡进"], "blocking": [{"name": "羡进", "x": "right"}]}
        self.assertTrue(Q.qc_blocking_image(shot_left, left)["ok"])
        self.assertFalse(Q.qc_blocking_image(shot_left, right)["ok"])
        self.assertTrue(Q.qc_blocking_image(shot_right, right)["ok"])
        self.assertFalse(Q.qc_blocking_image(shot_right, left)["ok"])

    def test_framing_band(self):
        small = _mk(self._p("small.png"), [(280, 150, 340, 210, CHAR_BLUE)])
        big = _mk(self._p("big.png"), [(240, 20, 400, 340, CHAR_BLUE)])
        shot = {"characters_in_shot": ["羡进"]}
        self.assertFalse(Q.qc_blocking_image(shot, small, cam_key="近景")["ok"])
        self.assertTrue(Q.qc_blocking_image(shot, small, cam_key="全景")["ok"])
        self.assertTrue(Q.qc_blocking_image(shot, big, cam_key="近景")["ok"])
        self.assertFalse(Q.qc_blocking_image(shot, big, cam_key="远景")["ok"])

    def test_prop_count(self):
        p = _mk(self._p("prop.png"), [(260, 100, 380, 300, CHAR_BLUE)])
        shot = {"characters_in_shot": ["羡进"], "items_in_shot": ["旧木剑"]}
        self.assertTrue(Q.qc_blocking_image(shot, p, diag={"chars": 1, "props": 1})["ok"])
        bad = Q.qc_blocking_image(shot, p, diag={"chars": 1, "props": 0})
        self.assertFalse(bad["ok"])
        self.assertTrue(any("道具" in x for x in bad["reasons"]))

    def test_missing_diag_is_skipped_not_failed(self):
        p = _mk(self._p("nod.png"), [(260, 100, 380, 300, CHAR_BLUE)])
        r = Q.qc_blocking_image({"characters_in_shot": ["羡进"], "action": "他跪坐在地上"}, p)
        by = {c["name"]: c for c in r["checks"]}
        self.assertIsNone(by["char_count"]["ok"])
        self.assertIsNone(by["pose_applied"]["ok"])
        self.assertTrue(r["ok"], r["reasons"])

    def _plan(self):
        return {
            "camera": {"position": [0.0, 1.0, 4.0], "target": [0.0, 1.0, 0.0]},
            "scene": {"entities": [
                {"id": "char_1", "type": "character",
                 "transform": {"position": [0.0, 0.0, 0.0], "rotation": [0, 0, 0],
                               "scale": [1, 1, 1]},
                 "poseValues": {}, "currentPreset": ""},
                {"id": "cam_1", "type": "camera", "transform": {"position": [0, 1, 4]}},
            ]},
        }

    def test_repair_framing_moves_camera_closer(self):
        """近景但人物太小 -> 机位拉近（投影高度与距离成反比）。"""
        plan = self._plan()
        qc = {"checks": [{"name": "framing", "ok": False, "detail": ""}],
              "stats": {"h_ratio": 0.10}}
        shot = {"camera": "近景", "characters_in_shot": ["羡进"]}
        fixed, notes = Q.repair_plan(shot, plan, qc)
        self.assertTrue(notes)
        d0 = abs(plan["camera"]["position"][2] - plan["camera"]["target"][2])
        d1 = abs(fixed["camera"]["position"][2] - fixed["camera"]["target"][2])
        self.assertLess(d1, d0)

    def test_repair_slot_shifts_character_left(self):
        plan = self._plan()
        qc = {"checks": [{"name": "slot", "ok": False, "detail": ""}], "stats": {"cx": 0.5}}
        shot = {"characters_in_shot": ["羡进"], "blocking": [{"name": "羡进", "x": "left"}]}
        fixed, notes = Q.repair_plan(shot, plan, qc)
        self.assertTrue(notes)
        self.assertLess(fixed["scene"]["entities"][0]["transform"]["position"][0], 0.0)

    def test_repair_pose_writes_values(self):
        plan = self._plan()
        qc = {"checks": [{"name": "pose_applied", "ok": False, "detail": ""}], "stats": {}}
        shot = {"characters_in_shot": ["羡进"], "action": "他跪坐在地上"}
        fixed, notes = Q.repair_plan(shot, plan, qc)
        self.assertTrue(notes)
        ent = fixed["scene"]["entities"][0]
        self.assertTrue(ent["poseValues"])
        self.assertEqual(ent["currentPreset"], "kneelTwo")

    def test_clipped_detected_and_repaired(self):
        # 人偶顶到画面顶边 = 被裁
        p = _mk(self._p("clip.png"), [(240, 0, 400, 300, CHAR_BLUE)])
        r = Q.qc_blocking_image({"characters_in_shot": ["羡进"]}, p)
        self.assertFalse(r["ok"])
        self.assertTrue(any("裁" in x for x in r["reasons"]), r["reasons"])
        fixed, notes = Q.repair_plan({"characters_in_shot": ["羡进"]}, self._plan(), r)
        self.assertTrue(notes)
        d0 = abs(self._plan()["camera"]["position"][2] - self._plan()["camera"]["target"][2])
        d1 = abs(fixed["camera"]["position"][2] - fixed["camera"]["target"][2])
        self.assertGreater(d1, d0)      # 拉远

    def test_not_clipped_when_inside(self):
        p = _mk(self._p("inset.png"), [(260, 60, 380, 300, CHAR_BLUE)])
        r = Q.qc_blocking_image({"characters_in_shot": ["羡进"]}, p)
        by = {c["name"]: c for c in r["checks"]}
        self.assertTrue(by["clipped"]["ok"])

    def test_no_fixable_returns_none(self):
        fixed, notes = Q.repair_plan({}, self._plan(),
                                     {"checks": [{"name": "image_ok", "ok": False}],
                                      "stats": {}})
        self.assertIsNone(fixed)
        self.assertEqual(notes, [])

    def test_qc_and_repair_loop(self):
        """闭环：第 1 轮不通过 -> 调 render_fn 重渲 -> 第 2 轮通过。"""
        good = _mk(self._p("good.png"), [(200, 10, 440, 350, CHAR_BLUE)])
        calls = []

        def fake_render(plan, out_dir):
            calls.append(plan)
            return good

        small = _mk(self._p("small2.png"), [(300, 170, 330, 200, CHAR_BLUE)])
        shot = {"characters_in_shot": ["羡进"], "camera": "近景"}
        res = Q.qc_and_repair(shot, small, cam_key="近景", target_size=(640, 360),
                              out_dir=self.d, render_fn=fake_render, max_rounds=2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["path"], good)
        self.assertEqual(len(res["rounds"]), 2)

    def test_qc_and_repair_gives_up_when_unfixable(self):
        blank = _mk(self._p("blank2.png"), [])
        calls = []

        def fake_render(plan, out_dir):
            calls.append(plan)
            return blank

        res = Q.qc_and_repair({"characters_in_shot": ["羡进"]}, blank, target_size=(640, 360),
                              out_dir=self.d, render_fn=fake_render, max_rounds=2)
        self.assertFalse(res["ok"])
        # 空白图属于「不可自动修复」（没有任何可反推的参数）→ 不该白重渲
        self.assertEqual(calls, [])

    def test_alt_camera_when_repair_fails(self):
        """主板自愈无效时换机位俯仰角重渲（只改俯仰、不改景别）。"""
        bad = _mk(self._p("badalt.png"), [(300, 170, 330, 200, CHAR_BLUE)])
        good = _mk(self._p("goodalt.png"), [(240, 20, 400, 340, CHAR_BLUE)])
        seen = []

        def fake_render(plan, out_dir):
            seen.append(plan)
            return good          # 备选机位渲出来的图通过质检

        shot = {"characters_in_shot": ["羡进"], "camera": "近景"}
        res = Q.qc_repair_and_alt_cameras(shot, bad, cam_key="近景", target_size=(640, 360),
                                          out_dir=self.d, render_fn=fake_render,
                                          max_rounds=0, cam_angles=("仰拍",))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res.get("cameraAngle"), "仰拍")
        self.assertTrue(seen)

    def test_alt_camera_not_tried_when_ok(self):
        good = _mk(self._p("ok1.png"), [(240, 20, 400, 340, CHAR_BLUE)])
        called = []

        def fake_render(plan, out_dir):
            called.append(plan)
            return good

        res = Q.qc_repair_and_alt_cameras({"characters_in_shot": ["羡进"], "camera": "近景"},
                                          good, cam_key="近景", target_size=(640, 360),
                                          out_dir=self.d, render_fn=fake_render,
                                          max_rounds=0, cam_angles=("仰拍",))
        self.assertTrue(res["ok"])
        self.assertEqual(called, [])

    def test_enabled_switch(self):
        old = os.environ.get("MJSCXT_TE3D_QC")
        try:
            os.environ["MJSCXT_TE3D_QC"] = "0"
            self.assertFalse(Q.enabled())
            os.environ["MJSCXT_TE3D_QC"] = "1"
            self.assertTrue(Q.enabled())
        finally:
            if old is None:
                os.environ.pop("MJSCXT_TE3D_QC", None)
            else:
                os.environ["MJSCXT_TE3D_QC"] = old


if __name__ == "__main__":
    unittest.main()
