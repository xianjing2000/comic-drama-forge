# -*- coding: utf-8 -*-
"""软放行词表同步回归测试（t22 测试层新增，2026-10-12）。

真缺陷（T18 P0 + T5 审计 F2 同一结论）：
    分镜层有**两张表**决定同一个问题的两个答案 ——
      · app/qc_client.CRITICAL_ISSUE_KEYWORDS       → 判官命中即**硬阻断**；
      · app/sb_helpers._SB_STRUCTURAL_DEFECT_KEYWORDS → 决定能否被 storyboard_soft_qc **软放行入库**。
    两张表本应同步（代码注释自己写着「必须同时改另一侧」，但**没有任何测试在管**）。
    实测后果：qc 侧有 13 条手指畸变词、sb 侧一条都没有 → 硬闸门已判「九宫格手指畸变
    关键缺陷阻断」，软放行闸门却不认得 → 畸变图照样入库（shot_44 实测）。

    这类缺陷的可怕之处：两表「各自都能跑」，日志里两条结论打架，
    只有把图翻出来肉眼比对才能发现 —— 必须由测试来钉。

分层（见 t22 测试策略）：
    · 词表同源性 / 子集关系 / 否定语境 —— 纯单元，零依赖。
    · 「缺陷族必须双侧可达」—— 把真缺陷的原文当样本，逐族断言两侧都判缺陷。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.abspath(os.path.join(_HERE, os.pardir, "app"))
if _APP not in sys.path:
    sys.path.insert(0, _APP)


class TestKeywordTableSync(unittest.TestCase):
    """两张判据表必须保持同源。"""

    @classmethod
    def setUpClass(cls):
        import qc_client
        import sb_helpers
        cls.qc = qc_client
        cls.sb = sb_helpers
        cls.qc_tbl = tuple(qc_client.CRITICAL_ISSUE_KEYWORDS)
        cls.sb_tbl = tuple(sb_helpers._SB_STRUCTURAL_DEFECT_KEYWORDS)

    def test_sb_table_is_subset_of_qc_table(self):
        """⭐ sb 侧每一条都必须在 qc 侧存在（子集关系）。

        反向不成立是**刻意**的：qc 侧包含「畸变 / 融合 / 错位」这类宽泛泛词，
        收进软放行表会造成大面积误杀。但 sb 侧出现 qc 侧没有的词，
        就意味着「这里判缺陷、判官却不判」= 两处口径打架。
        """
        only_sb = sorted(set(self.sb_tbl) - set(self.qc_tbl))
        self.assertEqual(
            only_sb, [],
            "以下词条只在软放行表、不在 qc 硬阻断表里 —— 两处判据打架：%s" % only_sb)

    #: 已知重复词条（**生产侧既有事实，不在本任务 inScope 内修**）：
    #:   qc_client.CRITICAL_ISSUE_KEYWORDS 里「六指」出现两次（2026-10-10 手部畸变批
    #:   与 T18 手指畸变批重叠）。功能上无影响（词表按 in 匹配，重复不改变判定），
    #:   故登记为**已接受基线**而不是让测试长期发红；清理后把这里改回空集即可收紧。
    KNOWN_DUPLICATES = {"qc_client.CRITICAL_ISSUE_KEYWORDS": {"六指"}}

    def test_duplicate_entries_match_known_baseline(self):
        """重复词条只能等于「已接受基线」——新出现的重复立刻变红。"""
        for name, tbl in (("qc_client.CRITICAL_ISSUE_KEYWORDS", self.qc_tbl),
                          ("sb_helpers._SB_STRUCTURAL_DEFECT_KEYWORDS", self.sb_tbl)):
            dupes = {w for w in tbl if list(tbl).count(w) > 1}
            self.assertEqual(
                dupes, self.KNOWN_DUPLICATES.get(name, set()),
                "%s 出现（新的）重复词条：%s" % (name, sorted(dupes)))

    def test_duplicate_detector_actually_detects(self):
        """自检：把「六指」人为重复一次，上面的判定确实会报出来。"""
        t = list(self.qc_tbl) + ["六指"]
        self.assertIn("六指", {w for w in t if t.count(w) > 1})

    def test_tables_are_non_trivial(self):
        """自检：两张表都不能空/被清空（清空后上面两条会静默变绿）。"""
        self.assertGreater(len(self.qc_tbl), 40, "qc 词表规模异常")
        self.assertGreater(len(self.sb_tbl), 20, "sb 词表规模异常")

    # ---------------- 按「缺陷族」逐族对齐 ----------------
    #: 每个缺陷族的**取样原文**（来自生产实测的判语），两侧都必须认。
    FAMILIES = {
        "九宫格场景/方位": ["九宫格场景不一致", "格间换场景",
                            "九宫格左右手镜像", "左右手互换", "镜像翻转"],
        "手指畸变（T18 P0 漏判族）": ["九宫格手指畸变", "手指粘连", "指头粘连",
                                      "手指融合", "多指畸形", "六指",
                                      "杵状指", "指尖膨大", "指尖钝化", "指尖球状",
                                      "手指粗细不均", "指节模糊"],
        "角色与设定不符": ["与设定不符", "与设定图不符", "与设定不一致",
                           "发型与设定不符", "服装与设定不一致",
                           "明显不符", "明显不一致",
                           "与上一镜不符", "与上一镜不连贯"],
    }

    def test_every_defect_family_hits_both_tables(self):
        """⭐ 逐族断言：同一句判语在**两张表**上都得判成缺陷。"""
        for family, samples in self.FAMILIES.items():
            for s in samples:
                with self.subTest(family=family, sample=s):
                    self.assertIn(s, self.qc_tbl,
                                  "[%s] qc 硬阻断表缺词条：%s" % (family, s))
                    self.assertIn(s, self.sb_tbl,
                                  "[%s] 软放行表缺词条：%s（→ 该缺陷会被软放行入库）"
                                  % (family, s))

    def test_soft_gate_flags_production_finger_defect_text(self):
        """⭐ 用生产原文（shot_44 同批判语）直接喂软放行判据，必须判为结构性缺陷。"""
        samples = [
            "九宫格手指畸变（第5格）",
            "第5格出现六指，手指粘连",
            "指尖钝化、指节模糊、手指粗细不均（第5格）",
            "角色发型与设定图及上一镜明显不符",
            "九宫格场景不一致（第2格换了场景）",
            "九宫格左右手镜像",
        ]
        for s in samples:
            with self.subTest(sample=s):
                # ⚠️ _sb_structural_defect 的入参是**质检 gate 字典**（不是裸字符串）：
                #    判定文本 = reason + label + critical_issues。第一版测试传字符串，
                #    函数按「非 dict → False」早返回，看起来像漏判其实是调用姿势错。
                gate = {"reason": s, "label": "", "critical_issues": []}
                self.assertTrue(self.sb._sb_structural_defect(gate),
                                "软放行判据没认出真缺陷原文：%r" % s)

    def test_soft_gate_ignores_normal_composition_text(self):
        """反向：正常构图描述不得被误杀（否则会大面积无谓重跑）。"""
        for s in ("人物走进走廊，手指向远方，双手交叠",
                  "角色与本镜设定相符，画面干净",
                  "构图稳定，未发现畸变"):
            with self.subTest(sample=s):
                self.assertFalse(self.sb._sb_structural_defect({"reason": s}),
                                 "正常描述被误判成结构性缺陷：%r" % s)

    def test_non_dict_gate_degrades_to_false(self):
        """契约：gate 非 dict 时返回 False（退化为可软放行，不改变既有行为）。"""
        for bad in (None, "", [], 0):
            self.assertFalse(self.sb._sb_structural_defect(bad))


class TestPanelDupSoftGate(unittest.TestCase):
    """「面板雷同」类的词根正则 + 否定语境（T18 P0 的第二种漏判）。"""

    @classmethod
    def setUpClass(cls):
        import sb_helpers
        cls.sb = sb_helpers

    def test_flags_defect_with_modifier_inserted(self):
        """⭐ 漏判样本：VLM 在词间插了修饰语「严重」，字面词条匹配不上。"""
        self.assertTrue(
            self.sb._panel_dup_hit("九宫格面板严重雷同（第3、4、6、7格动作构图几乎相同）"),
            "词根正则必须能穿过修饰语")

    def test_does_not_flag_passing_conclusion(self):
        """⭐ 误判样本：达标结论里出现「未达面板雷同阈值」——是**正面**结论。"""
        self.assertFalse(
            self.sb._panel_dup_hit("角色动作推进连贯，未达面板雷同阈值，可判合格。"),
            "否定语境未被排除 → 会把合格结论判成缺陷")

    def test_still_flags_real_defect_after_negation_window(self):
        """否定词只取紧邻窗口：远距离的「无」不得把真缺陷吞掉。"""
        self.assertTrue(
            self.sb._panel_dup_hit("第5格无主体，此后各格面板严重雷同"),
            "否定窗口放太宽会把真缺陷一起吞掉")

    def test_short_phrase_is_not_a_keyword(self):
        """取舍：单独的「雷同」「手指」不得进字面词表（会误杀）。"""
        self.assertNotIn("雷同", self.sb._SB_STRUCTURAL_DEFECT_KEYWORDS)
        self.assertNotIn("手指", self.sb._SB_STRUCTURAL_DEFECT_KEYWORDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
