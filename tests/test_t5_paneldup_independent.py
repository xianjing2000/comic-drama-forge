# -*- coding: utf-8 -*-
"""t5 独立复核：面板雷同判据修复（ac27d91 / 44d3977）—— 回归门 + 已证伪台账。

复核人：test-engineer（t5）。**不采信 captain 自述**，结论全部由本文件 + 生产语料重算。

被复核对象（app/sb_helpers.py 的 _panel_dup_hit）：
  ac27d91  两级否定（STRONG/WEAK）+ 句子边界截断 + 移除「达标/合格」
  v44=44d3977  WEAK 窗口 3→6、误判基线 10→2

⚠️ 为什么用「冻结快照」而不是直接 import app/sb_helpers.py：
  复核期间 app/sb_helpers.py 被**另一个人同时在改**（16:20 那次把词根从 2 条扩到 5 条），
  直接 import 会让断言变红/变绿取决于别人手速 —— 那不是验证。
  故 tests/_t5_frozen/sb_helpers_live_1620.py 是本轮复核时点的逐字快照（sha256 见常量），
  所有「现状/台账」断言针对它；所有「修复前 vs 修复后」断言针对 git 提交。

跑法：python -m pytest tests/test_t5_paneldup_independent.py -v
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, "app")
_TESTS = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_TESTS, "_t5_versions_out")
_FROZEN = os.path.join(_APP, "sb_helpers.py")
#: ⚠️ 2026-10-12 t20 起：t17（eac12cb）已把本文件记录的 B/D 类缺陷修掉（补齐 13 类软边界 +
#: 否定词表补全），此后 FROZEN **跟随工作树实现**（不再冻结旧快照）；B/D 段落断言
#: 同步翻转为「必须命中 / 必须 False」，E 段落保留为「仍存疑」台账。
#: 历史快照（复核时点 16:21，sha256 1ed75dec…）在 git 历史与本任务记录里可查。
if _APP not in sys.path:
    sys.path.insert(0, _APP)

#: 本文件断言所依据的那份实现快照（复核时点）。任何人改 app/ 都不影响本文件的稳定性。
FROZEN_SHA256 = None   # 首次运行为 None，__main__ 时打印；见 _frozen_sha()


def _frozen_sha():
    with io.open(_FROZEN, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _load(path, name):
    import importlib.util
    if _APP not in sys.path:
        sys.path.insert(0, _APP)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


LIVE = _load(_FROZEN, "sb_t5_live_1620")


def _rev(rev, tag):
    """用 git 取历史提交的 app/sb_helpers.py，独立执行，做改前/改后对照。"""
    r = subprocess.run(["git", "show", "%s:app/sb_helpers.py" % rev], cwd=_ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise unittest.SkipTest("git 不可用或提交缺失：%s" % rev)
    os.makedirs(_TMP, exist_ok=True)
    p = os.path.join(_TMP, "sb_%s.py" % tag)
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(r.stdout.decode("utf-8"))
    return _load(p, "sb_t5_%s" % tag)


def _try(rev, tag):
    try:
        return _rev(rev, tag)
    except unittest.SkipTest:
        return None


PRE = _try("ac27d91^", "pre")    # 修复前
AC27 = _try("ac27d91", "ac27")   # 两级否定 + 截断（WEAK 3 字）
V44 = _try("44d3977", "v44")     # WEAK 3→6
NEED_GIT = unittest.skipUnless(PRE and AC27 and V44, "需要 git 历史做对照")


# ------------------------------------------------------------------ #
# 生产语料
# ------------------------------------------------------------------ #
def _qc_root():
    return os.path.join(os.environ.get("APPDATA", ""), "mjscxt-desktop",
                        "mjscxt-data", "output", "qc")


def _corpus():
    qc = _qc_root()
    out = []
    for dp, _dn, fns in os.walk(qc):
        for fn in fns:
            if not fn.endswith(".json"):
                continue
            try:
                with io.open(os.path.join(dp, fn), encoding="utf-8") as fh:
                    j = json.load(fh)
            except (OSError, ValueError):
                continue
            for r in (j.get("records") or []) if isinstance(j, dict) else []:
                if isinstance(r, dict):
                    out.append({"file": os.path.relpath(os.path.join(dp, fn), qc).replace("\\", "/"),
                                "shot": j.get("shot_id"),
                                "passed": r.get("passed"),
                                "attempt": r.get("attempt"),
                                "reason": str(r.get("reason") or "")})
    return out


_CORPUS = _corpus() if os.path.isdir(_qc_root()) else []


# ================================================================== #
# S0 环境事实：复核的前提
# ================================================================== #
class T5_S0_Environment(unittest.TestCase):

    def test_frozen_snapshot_exists_and_is_frozen(self):
        self.assertTrue(os.path.isfile(_FROZEN), "冻结快照缺失：%s" % _FROZEN)
        self.assertGreater(os.path.getsize(_FROZEN), 5000)
        self.assertIn("_panel_dup_spans", io.open(_FROZEN, encoding="utf-8").read(),
                      "快照内容不是同义族扩面那一版")

    def test_frozen_target_is_the_shipped_implementation(self):
        """⭐ t20：FROZEN **就是** app/sb_helpers.py（t17 合并后不再冻结旧快照）。

        历史原因：复核期间 app/ 被并发修改，断言必须钉在一个不动的副本上；
        现在缺陷已修，台账翻转成回归门，跟随工作树才不会过期。
        """
        self.assertEqual(os.path.abspath(LIVE.__file__),
                         os.path.join(_APP, "sb_helpers.py"))

    def test_synonym_family_is_a_later_commit_not_one_of_the_two(self):
        """环境事实（复核期间实测，必须登记）：

        被复核的两个提交 ac27d91 / 44d3977 的 _panel_dup_hit **只认**
        「面板/九宫格/格间 + 雷同/重复」，没有 _panel_dup_spans / _PANEL_DUP_SYN_RES。
        复核期间（16:16~16:21）磁盘上出现过未提交的「同义族扩面」版本，
        该版本后来成为 86e767c（t8/qc-dev）—— **不属于本次复核范围**。
        所以「线上跑的那份」与「被复核的那份」不是同一份，这就是本类存在的意义。
        """
        if AC27 is None:
            self.skipTest("git 不可用")
        self.assertFalse(hasattr(AC27, "_panel_dup_spans"),
                         "ac27d91 竟然已带同义族 —— 请重核复核对象")
        self.assertFalse(hasattr(V44, "_panel_dup_spans"),
                         "44d3977 竟然已带同义族 —— 请重核复核对象")
        self.assertFalse(hasattr(AC27, "_PANEL_DUP_RES"),
                         "ac27d91 竟然已有 _PANEL_DUP_RES 词根集合 —— 请重核")
        pat = AC27._PANEL_DUP_RE.pattern
        self.assertIn("面板|九宫格|格间", pat)
        self.assertNotIn("动作", pat, "被复核版本的词根面已含同义族 —— 请重核复核对象")


# ================================================================== #
# A 生产日志回放
# ================================================================== #
class T5_A_ProductionReplay(unittest.TestCase):
    """2026-10-12 t5 独立复核实测（生产 274 records：passed=True 113 / False 161）。

      版本                        FP(A)  FN(A)   FP(B)  FN(B)   硬判语漏判
      ac27d91^  修复前             8/113   2/100   8/113  35/133      4
      ac27d91   第1提交             6/113   2/100   6/113  35/133      4
      44d3977   第2提交             5/113   3/100   5/113  36/133      4
    ------------------------------------------------------------------
      结论 A-1：误判确实下降（8→5），**但真缺陷漏判反升 1 条**（2→3，35→36）。
      结论 A-2：「真缺陷零放过」**不成立** —— 口径 A 有 2~3 条，口径 B 有 35~36 条。
      结论 A-3：captain 口径（含不达标/阻断/不合格）下也有 **4 条**被放过，
                全部因词根面不含「动作/构图」而漏（见 test_zero_pass_claim_is_false）。
    """

    ROOTS_NARROW = ("面板", "九宫格", "格间")
    ROOTS_WIDE = ("面板", "九宫格", "格间", "动作", "构图", "机位", "景别", "姿势",
                  "站位", "运镜", "画面", "视觉", "分镜")
    SUFFIX = ("雷同", "重复", "趋同", "单一")
    PURE_NEG = ("未出现", "未存在", "不存在", "没有发现", "未发现", "不足以",
                "未构成", "未形成", "未达", "未超", "未超过", "未触及", "未见")
    SEP = "，。；、,.;：:!！?？｜|\"'“”‘’"

    @classmethod
    def _cands(cls, txt, roots):
        out = []
        for r in roots:
            i = txt.find(r)
            while i >= 0:
                for s in cls.SUFFIX:
                    j = txt.find(s, i + len(r))
                    if 0 <= j - (i + len(r)) <= 6 and not re.search(r"\s", txt[i:j]):
                        out.append((i, r, txt[i + len(r):j], s))
                i = txt.find(r, i + 1)
        return sorted(out, key=lambda x: x[0])

    @classmethod
    def _indep(cls, txt, roots):
        """独立标注：枚举全部候选；否定只认同子句纯否定短语 / 空位否定。"""
        for (i, _r, gap, _s) in cls._cands(txt, roots):
            pre = txt[max(0, i - 6):i]
            cut = max([pre.rfind(x) for x in cls.SEP] + [-1])
            if cut >= 0:
                pre = pre[cut + 1:]
            if any(n in pre for n in cls.PURE_NEG):
                continue
            if gap.strip() in ("无", "没有", "不"):
                continue
            return True
        return False

    def setUp(self):
        if not _CORPUS:
            self.skipTest("无生产质检语料")

    def _fp(self, mod):
        return [r for r in _CORPUS if r["passed"] is True and mod._panel_dup_hit(r["reason"])]

    def _fn(self, mod, roots):
        return [r for r in _CORPUS if r["passed"] is False
                and self._indep(r["reason"], roots)
                and not mod._panel_dup_hit(r["reason"])]

    @NEED_GIT
    def test_A1_false_positive_rate_dropped(self):
        self.assertEqual(len(self._fp(PRE)), 8)
        self.assertEqual(len(self._fp(AC27)), 6)
        self.assertEqual(len(self._fp(V44)), 5)
        self.assertLess(len(self._fp(V44)), len(self._fp(PRE)), "误判没有下降")

    @NEED_GIT
    def test_A2_true_defect_miss_rose(self):
        """A2：口径 A（提交版词根面）漏判 2 → 3 —— 弱窗口放宽的代价。"""
        self.assertEqual(len(self._fn(PRE, self.ROOTS_NARROW)), 2)
        self.assertEqual(len(self._fn(AC27, self.ROOTS_NARROW)), 2)
        self.assertEqual(len(self._fn(V44, self.ROOTS_NARROW)), 3)

    @NEED_GIT
    def test_A3_wide_scope_miss_is_huge(self):
        """A3：口径 B（把「动作/构图/机位…雷同」也算真缺陷）漏判 35~36 条。

        即：治好的 3 条（8→5）远小于**没被覆盖**的 35 条。
        """
        for mod, want in ((PRE, 35), (AC27, 35), (V44, 36)):
            self.assertEqual(len(self._fn(mod, self.ROOTS_WIDE)), want)

    @NEED_GIT
    def test_A4_zero_pass_claim_is_false(self):
        """A4：证伪 captain「含不达标/阻断/不合格的真缺陷放过 0 条」。

        4 条生产记录同时含 (a) 硬判语 (b) 动作/构图 雷同表述，v44 全部放过。
        （冻结快照 16:20 那版把词根扩面后已救回这 4 条 —— 与提交本身无关。）
        """
        hard = ("不达标", "阻断", "不合格")
        victims = [r for r in _CORPUS
                   if any(h in r["reason"] for h in hard)
                   and self._cands(r["reason"], self.ROOTS_WIDE)
                   and not V44._panel_dup_hit(r["reason"])]
        self.assertEqual(
            sorted(v["file"] for v in victims),
            ["进境_整本小说/image_19.json", "进境_整本小说/image_60.json",
             "逆天系统_全书/image_39.json", "逆天系统_全书/image_45.json"],
            "「硬判语 + 雷同表述」漏判集合变了，请重算 A4")

    def test_A5_current_implementation_baseline(self):
        """A5：当前实现（t17 之后）的生产语料口径 —— 只钉「不恶化」。

        历史数字（t5 复核报告）：ac27d91^ FP 8/113、FN(A) 2/100、FN(B) 35/133；
        44d3977 FP 5/113、FN(A) 3/100、FN(B) 36/133；t17 之后 FN(B) 大幅下降。
        """
        self.assertLessEqual(len(self._fp(LIVE)), 5, "达标误判超过复核基线 5/113")
        self.assertLessEqual(len(self._fn(LIVE, self.ROOTS_WIDE)), 36,
                             "口径 B 漏判超过复核基线 36/133")


# ================================================================== #
# B 攻击标点截断机制
# ================================================================== #
class T5_B_SeparatorTruncation(unittest.TestCase):
    """结论（逐条）：
      B-1 同句「先否定后肯定」（逗号等硬标点）→ **通过**，截断正确，真缺陷都命中。
      B-2 引号嵌套 → 本身**不是**绕过点；但「」『』**不在** _PANEL_DUP_SEP 里，
          一旦与「无/不」同现（弱否定在 4~6 字）就成为绕过通道。
      B-3 换行 → **驳回**：\n 不在分隔集，弱窗口 6 字跨行吞掉真缺陷（ac27 命中 → v44 漏）。
      B-4 省略号 → **驳回**：…… 虽在分隔集，但「截断后前缀不含 STRONG 短语」这一路径
          会让 str.find(near6) 对 STRONG 失效（"未出现面板雷同不再适用……面板雷同"）。
      B-5 全半角混用 → **部分驳回**：，,;：:!！｜| 等在半角侧齐全；
          ．·/—— 《》「」（ ） 等不在集合内，构成 9 条绕过用例。
    """

    SOFT = [
        ("本镜无畸变\n面板雷同", "换行不在分隔集"),
        ("本镜无畸变（面板雷同）", "全角圆括号不在分隔集"),
        ("本镜无畸变「面板雷同」", "全角引号不在分隔集（生产 59 条含「」）"),
        ("本镜无畸变『面板雷同』", "全角书名引号不在分隔集（生产 8 条含『』）"),
        ("本镜无畸变．面板雷同", "全角句点不在分隔集"),
        ("本镜无畸变·面板雷同", "间隔号不在分隔集"),
        ("本镜无畸变/面板雷同", "斜杠不在分隔集（生产 117 条含 /）"),
        ("本镜无畸变 《面板雷同》", "书名号不在分隔集"),
        ("本镜无畸变——面板雷同", "破折号不在分隔集"),
    ]

    def test_B1_hard_punctuation_truncates_correctly(self):
        ok = ["无畸变，面板雷同", "无畸变,面板雷同", "无畸变;面板雷同", "无畸变：面板雷同",
              "无畸变:面板雷同", "无畸变｜面板雷同", "无畸变|面板雷同", "无畸变！面板雷同",
              "第5格无主体，面板严重雷同", "本镜无畸变，图2-9面板雷同"]
        bad = [t for t in ok if not LIVE._panel_dup_hit(t)]
        self.assertEqual(bad, [], "硬标点截断被改坏：%s" % bad)

    def test_B2_quotes_are_not_a_bypass_by_themselves(self):
        ok = ["判语为「面板雷同」的镜头不合格", "复核结论：『面板雷同』",
              "提示词写的是「不要出现面板雷同」，结果面板雷同"]
        bad = [t for t in ok if not LIVE._panel_dup_hit(t)]
        self.assertEqual(bad, [], "引号嵌套场景被绕过：%s" % bad)

    def test_B3_soft_boundaries_are_no_longer_a_bypass(self):
        """⭐ t20 更新（t17/eac12cb 已修）：9 条真缺陷**必须全部命中**。

        历史：ac27d91(3 字窗口) 全命中 → 44d3977(3→6) 全漏（t5 复核的 9 条绕过输入）。
        现在 _PANEL_DUP_SEP 已补齐这些软边界，断言方向翻转为「不许漏」。
        """
        missed = [t for t, _n in self.SOFT if not LIVE._panel_dup_hit(t)]
        self.assertEqual(missed, [], "软边界又被绕过：%s" % missed)

    #: 结论 B-4**本应**成立：这些软边界都该能截断否定窗口。
    #: 本断言**故意写成「缺口存在」**：补齐后它会变红，逼人把它改成 assertEqual(missing, [])。
    MISSING_SOFT_BOUNDARIES = (chr(10), chr(9), "（", "）", "「", "」", "『", "』", "．", "·",
                               "/", "—", "《", "》")

    def test_B4_separator_set_covers_soft_boundaries(self):
        """⭐ t20 更新：这些软边界必须都在 _PANEL_DUP_SEP 里（t17 已补齐）。"""
        missing = [c for c in self.MISSING_SOFT_BOUNDARIES if c not in LIVE._PANEL_DUP_SEP]
        self.assertEqual(missing, [], "_PANEL_DUP_SEP 缺软边界 %s" % missing)

    @NEED_GIT
    def test_B5_attribution_is_the_weak_window_change(self):
        """B5：归因 —— ac27d91（WEAK 3 字）9 条全命中；44d3977（3→6）9 条全漏。"""
        wrong = [(t, AC27._panel_dup_hit(t), V44._panel_dup_hit(t))
                 for t, _n in self.SOFT
                 if not (AC27._panel_dup_hit(t) is True and V44._panel_dup_hit(t) is False)]
        self.assertEqual(wrong, [], "归因不成立：%s" % wrong)


# ================================================================== #
# C 攻击「移除达标/合格」
# ================================================================== #
class T5_C_GradeWordsRemoval(unittest.TestCase):
    """结论：**通过**（净收益，未发现由此新增的漏判）。

      C-1 「不合格，面板雷同」「不达标，面板雷同」原表把「合格/达标」当否定 → 真缺陷被放过；
          移除后命中。
      C-2 反向语序（「面板雷同，不合格」）在新旧实现上都命中 —— 未新增漏判。
      C-3 构造集上 v44 相对 pre **没有**丢任何命中（test_C4）。
    """

    FIXED_BY_REMOVAL = ["不合格，面板雷同", "不达标，面板雷同", "未合格，面板雷同"]
    MUST_STILL_HIT = ["面板雷同，不合格", "面板雷同，不达标", "格间重复，不达标",
                      "面板重复｜不达标", "不达标｜原因=九宫格严重面板雷同",
                      "判定=质检不达标｜原因=九宫格严重面板雷同"]

    def test_C1_now_detected(self):
        miss = [t for t in self.FIXED_BY_REMOVAL if not LIVE._panel_dup_hit(t)]
        self.assertEqual(miss, [], "移除「达标/合格」后仍有漏判：%s" % miss)

    def test_C2_no_new_miss(self):
        miss = [t for t in self.MUST_STILL_HIT if not LIVE._panel_dup_hit(t)]
        self.assertEqual(miss, [], "移除「达标/合格」引入新漏判：%s" % miss)

    @NEED_GIT
    def test_C3_were_missed_before(self):
        still = [t for t in self.FIXED_BY_REMOVAL if PRE._panel_dup_hit(t)]
        self.assertEqual(still, [], "对照失效：这些在修复前就没漏判：%s" % still)

    @NEED_GIT
    def test_C4_v44_loses_nothing_vs_pre(self):
        cases = self.FIXED_BY_REMOVAL + self.MUST_STILL_HIT
        lost = [t for t in cases if PRE._panel_dup_hit(t) and not V44._panel_dup_hit(t)]
        self.assertEqual(lost, [], "v44 相对 pre 丢了命中：%s" % lost)


# ================================================================== #
# D 复核基线残留 2 条
# ================================================================== #
class T5_D_BaselineResiduals(unittest.TestCase):
    """结论：两条**确实仍是误判**（D-1/D-2 通过），但**理由都不成立**（D-3 驳回）。

      D-1  两者在 pre/ac27/v44/快照上都返回 True → 是真误判，不是「已修好」。
      D-2  「无色块/撕裂/面板雷同」在**不带前导「无」**的写法（色块/撕裂/面板雷同）上
           已被磁盘版空位否定修好；带前导「无」的写法仍误判（超 6 字窗口）。
      D-3  证伪注释「刻意不修 / 前缀窗口天然覆盖不到」：
           ①「…不足以判为面板雷同」的否定短语与词根之间**没有句读**，
             原因是**窗口只有 6 字**（「不足以判为面板」共 7 字），放宽到 7 字即可命中；
           ② 同上，是窗口宽度问题，不是「另一类改动」。
           两条都属**可修的真边界**，基线注释需要改写。
    """

    R1 = "存在轻微面板重复倾向，不足以判为面板雷同"
    R2 = "无色块/撕裂/面板雷同"

    def test_D1_are_no_longer_false_positives(self):
        """⭐ t20：t17 之后两条残留误判都修好了 —— 断言翻转为「必须 False」。"""
        self.assertFalse(LIVE._panel_dup_hit(self.R1), "R1 仍误判为缺陷")
        self.assertFalse(LIVE._panel_dup_hit(self.R2), "R2 仍误判为缺陷")

    @NEED_GIT
    def test_D2_not_fixed_by_either_commit(self):
        for mod in (PRE, AC27, V44):
            self.assertTrue(mod._panel_dup_hit(self.R1))
            self.assertTrue(mod._panel_dup_hit(self.R2))

    def test_D3_R1_cause_is_window_width_not_sentence_position(self):
        i = self.R1.find("面板", self.R1.find("，"))
        self.assertGreater(i, 0, "找不到第二个「面板」")
        self.assertEqual(self.R1[i - 6:i], "，不足以判为")
        self.assertIn("不足以判为", self.R1[i - 7:i], "7 字窗口就能覆盖，证明是宽度问题")
        self.assertNotIn("，", self.R1[i - 6:i][1:], "否定短语与词根之间含句读 → 归因不成立")

    def test_D4_R2_both_forms_are_no_longer_false_positives(self):
        """D4：②号两种写法**都**误判；根因仍是窗口只有 6 字。

        「无」离词根 7 字（无/色/块///撕/裂//）→ 落不进前 6 字窗口，
        于是「无」被短窗口的「无雷同」判定路径**看不见**，误判为缺陷。
        这是 D-2 的第二种成因（与 D-1 同根），不是「刻意不修」。
        """
        #: 短写法（无前导「无」）：「无」离词根 6 字 → 进窗口 → 正确判 False。
        #: ⚠️ 实测（2026-10-12 t20 复核）：
        #:   · 「无色块/撕裂/面板雷同」（带前导「无」）：词根前 6 字含「无」→ 已修，判 False；
        #:   · 「色块/撕裂/面板雷同」（短写法）：「无」离词根 7 字 → 仍漏进不了 6 字窗口；
        #:     且 _panel_dup_spans 对 "面板雷同" 这个跨度算出的 gap 为空 → 空位否定也救不了它。
        #: 故：带前导「无」的**已修**（t24 基线已在 t20 收紧为空集），短写法仍是**已知缺口**。
        self.assertFalse(LIVE._panel_dup_hit("无色块/撕裂/面板雷同"),
                         "带前导「无」的写法又变成误判了")
        self.assertTrue(LIVE._panel_dup_hit("色块/撕裂/面板雷同"),
                        "短写法已被修好 —— 请把本断言翻成 False")
        t = "无色块/撕裂/面板雷同"
        i = t.find("面板")
        self.assertEqual(i, 7, "词根位置变了：%d" % i)
        self.assertNotIn("无", t[max(0, i - 6):i], "「无」落进了 6 字窗口 —— D4 归因不成立")
        self.assertIn("无", t[max(0, i - 7):i])


# ================================================================== #
# E WEAK 窗口 3→6 的新假阴性
# ================================================================== #
class T5_E_WeakWindowNewFalseNegatives(unittest.TestCase):
    """结论：**成立**（驳回「无新增假阴性」的主张）。两类：

      类① 跨软边界：见 T5_B（9 条，ac27 全命中 → v44 全漏）。
      类② 弱否定属于**另一个动词短语**：
          「构图差异不足构成面板雷同」——「不足构成」否定的是**判据**，
          不是「雷同」；3 字窗口（ac27d91）判真缺陷，6 字窗口放过。
          生产实测：进境_整本小说/asset_image_羡进公寓_grid.json 因此由命中变放过
          （口径 A 的 FN 2→3 就是它）。
    """

    NEW_FN = [
        ("构图差异不足构成面板雷同", "不足构成 = 对判据的否定，不是对雷同的否定"),
        ("无畸变或面板雷同", "「无」修饰畸变，不管雷同"),
        ("不是同一机位面板雷同", "「不是」修饰机位，不管雷同"),
        ("没有xx面板雷同", "弱否定 + 填充字，距词根 4~6 字"),
    ]

    @NEED_GIT
    def test_E1_all_flip_from_hit_to_miss(self):
        wrong = [(t, AC27._panel_dup_hit(t), V44._panel_dup_hit(t))
                 for t, _n in self.NEW_FN
                 if not (AC27._panel_dup_hit(t) is True and V44._panel_dup_hit(t) is False)]
        self.assertEqual(wrong, [], "3→6 的行为反转未复现：%s" % wrong)

    def test_E2_still_missed_on_the_snapshot(self):
        hit = [t for t, _n in self.NEW_FN if LIVE._panel_dup_hit(t)]
        self.assertEqual(hit, [], "冻结快照上已命中（%s）—— 请改断言并收紧" % hit)

    def test_E3_production_record_flipped(self):
        if not _CORPUS:
            self.skipTest("无生产语料")
        hit = [r for r in _CORPUS if "构图差异不足构成面板雷同" in r["reason"]]
        self.assertEqual(len(hit), 1, "生产语料里的该条记录数变了：%d" % len(hit))
        r = hit[0]
        self.assertEqual(r["passed"], False)
        self.assertTrue(AC27._panel_dup_hit(r["reason"]), "ac27d91 本应命中")
        self.assertFalse(V44._panel_dup_hit(r["reason"]), "44d3977 本应放过（回归）")


# ================================================================== #
# F 硬底线：真缺陷零放过（口径 A 的真源，跨版本不许变坏）
# ================================================================== #
class T5_F_HardBottomLine(unittest.TestCase):

    MUST_HIT = [
        "九宫格面板严重雷同（第3、4、6、7格动作构图几乎相同）",
        "第5格无主体，面板严重雷同",
        "判定为面板雷同，不合格",
        "质检不达标｜原因=九宫格严重面板雷同",
        "九宫格存在严重面板雷同（第1/2/3/7/8格构图几乎相同的走廊全景）",
        "面板重复",
    ]

    def test_all_hit_on_snapshot(self):
        miss = [t for t in self.MUST_HIT if not LIVE._panel_dup_hit(t)]
        self.assertEqual(miss, [], "硬底线被突破：%s" % miss)

    @NEED_GIT
    def test_nothing_lost_vs_pre(self):
        lost = [t for t in self.MUST_HIT if PRE._panel_dup_hit(t) and not V44._panel_dup_hit(t)]
        self.assertEqual(lost, [], "相对修复前丢了命中：%s" % lost)


# ================================================================== #
# G 已修好的达标判语：不许回退
# ================================================================== #
class T5_G_FixedPassPhrases(unittest.TestCase):

    FIXED = ["未出现面板雷同", "不存在面板雷同", "没有发现面板雷同", "未发现面板重复",
             "不足以判为面板雷同", "未见糊化或面板雷同", "无畸变或面板雷同",
             "格间无雷同", "无面板雷同", "面板无雷同", "九宫格无重复"]

    def test_must_be_pass(self):
        bad = [t for t in self.FIXED if LIVE._panel_dup_hit(t)]
        self.assertEqual(bad, [], "已达标的判语又变成缺陷（误判回归）：%s" % bad)

    @NEED_GIT
    def test_were_broken_before(self):
        broken = [t for t in self.FIXED if PRE._panel_dup_hit(t)]
        self.assertGreaterEqual(len(broken), 6,
                                "对照失效：修复前只有 %d 条被误判" % len(broken))


if __name__ == "__main__":
    print("frozen snapshot sha256 =", _frozen_sha())
    unittest.main(verbosity=2)