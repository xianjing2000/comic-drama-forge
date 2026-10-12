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

⚠️ 2026-10-13 t14「语料口径」改造（本文件新增，专治假红）：
  A1~A5 的语料是**实时生产 qc 目录**（%APPDATA%\\mjscxt-desktop\\mjscxt-data\\output\\qc），
  它随生产增长：本文件 docstring 记的 274 records 已涨到 299（passed=False 161→175）。
  于是任何「绝对条数」断言都会随语料增长而上浮 —— test_A3 长期报「38 != 35」
  就是这么来的：三个版本同步上浮 35→38、36→39，而实现（git show 导出的历史模块）
  一字未动。**那是假红，不是回归**（本测试根本不加载工作区实现）。
  现全部改断稳定口径：**方向性**（newer vs older 的大小关系）、**下界**（>= 历史量级）、
  **比率**（占达标/不达标记录的比例）、**相对量级**（漏判 ≫ 修复收益）。
  这些口径对语料增长恒稳，且仍能抓到真回归（t14 注入缺陷验证：V44 行为退化、
  ROOTS_WIDE 被缩小、工作区实现被改坏，均能命中）。

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
# 生产语料（**实时**，随生产增长 —— 故禁止绝对条数断言）
#
# ⚠️ 2026-10-13 t14：本文件所有断言只允许用以下四种口径，因为它们对语料增长恒稳：
#   · 方向性：newer vs older 的大小关系（如 v44 >= ac27、fp_v44 <= fp_pre）；
#   · 下界：>= 历史台账量级（语料增长只会更安全）；
#   · 比率：占达标 / 不达标记录的比例（分子分母同步涨）；
#   · 相对量级：宽口径漏判 ≫ 修复收益（归一化的「≪」）。
# 绝对条数（8/6/5、35/35/36 …）只允许作为**注释里的历史台账**出现，不得进断言。
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

      ⚠️ 2026-10-13 t14：上表是 **274 records 语料**上的历史台账，只作背景、**不进断言**。
        语料涨到 299 后三列数字同步上浮（FP 8/6/5 不变但 FN(B) 35/35/36 → 38/38/39），
        绝对条数断言遂变假红。本类断言已全部改口径：方向性 / 下界 / 比率 / 相对量级。
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

    #: A1 的历史台账收益（8→5 = 3 条）。用**下界**：语料增长只会让收益更大，
    #: 只有真退化（误判没降下来）才红。
    FP_GAIN_FLOOR = 3
    #: A3：宽口径漏判相对「误判收益」的倍数下限 —— 原始意图「治好的少数 ≪ 没被
    #: 覆盖的多数」的可迁移形式（与语料条数无关）。
    WIDE_MISS_OVER_FP_GAIN = 10
    #: A3：宽口径漏判的历史量级下界（原台账 35 条级，取约一半做保守下界）与
    #: 占不达标记录的占比下界。刻意用**下界**：语料增长只会更安全；规模真的塌下去
    #: 才红 —— 那一刻该做的是重核复核结论，而不是让门禁悄悄放过。
    WIDE_MISS_FLOOR = 20
    WIDE_MISS_FAIL_RATIO = 0.10

    @NEED_GIT
    def test_A1_false_positive_rate_dropped(self):
        """A1：达标误判 pre → ac27 → v44 **单调下降**，且净收益 >= 3 条。

        ⚠️ t14：原断言是绝对条数 8/6/5。语料是**实时**的，新增记录也会带误判，
        绝对条数随语料增长上浮 → 假红（实测 +50 条后为 11/9/8，实现一字未动）。
        改断单调性 + 收益下界：既守住「两个提交确实在降误判」这个事实，
        又不把台账数字钉成语料快照。
        """
        fp_pre = len(self._fp(PRE))
        fp_ac27 = len(self._fp(AC27))
        fp_v44 = len(self._fp(V44))
        self.assertLessEqual(fp_ac27, fp_pre,
                             "第 1 提交没有降低误判：%d → %d" % (fp_pre, fp_ac27))
        self.assertLessEqual(fp_v44, fp_ac27,
                             "第 2 提交没有继续降低误判：%d → %d" % (fp_ac27, fp_v44))
        self.assertGreaterEqual(
            fp_pre - fp_v44, self.FP_GAIN_FLOOR,
            "两个提交合计只降了 %d 条误判（台账下界 %d）—— 修复收益消失"
            % (fp_pre - fp_v44, self.FP_GAIN_FLOOR))

    @NEED_GIT
    def test_A2_true_defect_miss_rose(self):
        """A2：口径 A（窄词根面）下 WEAK 3→6 让漏判**上升** —— 弱窗口放宽的代价。

        ⚠️ t14：原断言是 2/2/3 绝对条数（其中 ac27 == pre 是等式）。语料增长会
        让新记录参与计数，等式不再稳（实测含「不达标，面板雷同」句式的增长会把
        pre 的窄口径漏判抬到 7，而 ac27 仍是 2）。改断**方向性**：
          · ac27 不差于 pre（移除「达标/合格」否定只会多命中，不会少）；
          · v44 严格差于 ac27（这就是「3→6 引入新假阴性」本身）。
        """
        fn_pre = len(self._fn(PRE, self.ROOTS_NARROW))
        fn_ac27 = len(self._fn(AC27, self.ROOTS_NARROW))
        fn_v44 = len(self._fn(V44, self.ROOTS_NARROW))
        self.assertLessEqual(
            fn_ac27, fn_pre,
            "ac27 在窄口径下反而比 pre 漏得更多（%d > %d）—— 「移除达标/合格」结论被推翻"
            % (fn_ac27, fn_pre))
        self.assertGreater(
            fn_v44, fn_ac27,
            "WEAK 3→6 没有引入新的窄口径漏判（%d → %d）—— 归因（A-2）不成立"
            % (fn_ac27, fn_v44))

    @NEED_GIT
    def test_A3_wide_scope_miss_is_huge(self):
        """A3：口径 B（把「动作/构图/机位…雷同」也算真缺陷）漏判规模「巨大」。

        原始意图（**不弱化**）：治好的误判只有 3 条（8→5），远小于**没被覆盖**的
        宽口径漏判（历史台账 35~36 条）。本条守的就是这个「≪」关系。

        ⚠️ 2026-10-13 t14 —— 为什么不再断言 35/35/36：
          那两个数是**早期语料快照**（274 records）的产物。语料涨到 299 条后，
          三个版本的宽口径漏判同步上浮为 38/38/39，而实现（git 历史提交）
          **一字未动** —— 于是长期报「38 != 35」。这是魔数钉死语料快照造成的
          **假红**，不是回归：本测试用 git show 加载历史模块，根本不加载工作区实现。
          同理「差值恒定」（ac27-pre == 0、v44-ac27 == 1）也不能钉：它只在语料不变时
          恒定，实测对抗式增长后 v44-ac27 会变成 6。

        现在的稳定口径（均与语料条数无关，实测 +50/对抗增长下全稳）：
          ① 方向性：v44 是三者中最差的（WEAK 放宽只会多漏），ac27 不差于 pre；
          ② 规模：漏判条数仍达历史量级下界，且占不达标记录的比例不低于一成；
          ③ 归一化：漏判规模仍**数量级大于**修复收益（「≪」的量化形式）。
        """
        n_pre = len(self._fn(PRE, self.ROOTS_WIDE))
        n_ac27 = len(self._fn(AC27, self.ROOTS_WIDE))
        n_v44 = len(self._fn(V44, self.ROOTS_WIDE))
        fp_gain = len(self._fp(PRE)) - len(self._fp(V44))
        n_fail = sum(1 for r in _CORPUS if r["passed"] is False)

        # ① 方向性 —— 语料怎么长都不该反过来。
        #    刻意用**严格大于**：WEAK 窗口 3→6 的代价必须**真实存在**（v44 比 ac27 多漏）。
        #    若退化成相等（=把 v44 的行为改回 ac27），这里立刻红 —— 这就是「判定差值
        #    不再是恒定 1」之后仍能抓到「改动被回滚」的那条线（t14 注入 I1 验证）。
        self.assertGreater(
            n_v44, n_ac27,
            "v44 没有比 ac27 多漏（%d vs %d）—— WEAK 3→6 的代价不存在，或对照逻辑被改坏"
            % (n_v44, n_ac27))
        self.assertLessEqual(
            n_ac27, n_pre,
            "ac27 的宽口径漏判多于 pre（%d > %d）—— 移除「达标/合格」否定后反而更差"
            % (n_ac27, n_pre))

        # ② 规模（绝对量级下界 + 占不达标记录的比例下界）
        self.assertGreaterEqual(
            n_v44, self.WIDE_MISS_FLOOR,
            "宽口径漏判塌到 %d 条（下界 %d）—— 要么真被治好了（请重核 A-2/A-3 结论"
            "并同步收紧本条），要么 ROOTS_WIDE 的独立标注被改坏"
            % (n_v44, self.WIDE_MISS_FLOOR))
        self.assertGreaterEqual(
            n_v44, self.WIDE_MISS_FAIL_RATIO * n_fail,
            "宽口径漏判占不达标记录的比例降到 %.1f%%（下界 %.1f%%，%d/%d）—— "
            "复核结论「真缺陷零放过不成立」不再站得住"
            % (100.0 * n_v44 / max(n_fail, 1), 100.0 * self.WIDE_MISS_FAIL_RATIO,
               n_v44, n_fail))

        # ③ 归一化：「≫ 修复收益」（原始意图的量化形式）
        self.assertGreater(fp_gain, 0, "修复没有带来任何误判收益")
        self.assertGreaterEqual(
            n_v44, fp_gain * self.WIDE_MISS_OVER_FP_GAIN,
            "宽口径漏判（%d 条）不再数量级大于误判收益（%d 条）："
            "「治好的少数 ≪ 没被覆盖的多数」这一复核结论不再成立" % (n_v44, fp_gain))

    #: A4 复核台账（2026-10-12 t5）：这 4 个文件同时含 (a) 硬判语 (b) 动作/构图
    #: 雷同表述，而 v44 全部放过。
    #: ⚠️ t14：语料增长会带来**新成员**，所以断「台账 4 条仍是子集」而不是
    #: 「集合恰好相等」—— 后者碰上语料增长就假红（新成员把等式打破）。
    #: 子集口径不弱化：只要这 4 条被救回（=captain「零放过」说法成立），仍然红。
    A4_LEDGER = ("进境_整本小说/image_19.json", "进境_整本小说/image_60.json",
                 "逆天系统_全书/image_39.json", "逆天系统_全书/image_45.json")

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
        got = set(v["file"] for v in victims)
        lost = [fn for fn in self.A4_LEDGER if fn not in got]
        self.assertEqual(
            lost, [],
            "复核台账里这些「硬判语 + 雷同表述」被 v44 放过的事实不见了：%s"
            "（若实现真修好了，请重核 A-3 结论并同步更新 A4_LEDGER）" % lost)

    #: A5 的历史基线（t5 复核报告），一律用**分数**表达 —— 率才是「不恶化」的正确口径：
    #:   ac27d91^  FP 8/113 = 7.08%   FN(B) 35/133 = 26.32%
    #:   44d3977   FP 5/113 = 4.42%   FN(B) 36/133 = 27.07%  ← 取作上界
    #: 基线语料规模（t5 复核时点）：113 达标 / 133 不合格。
    FP_RATE_BASELINE = 5.0 / 113.0
    FN_WIDE_RATE_BASELINE = 36.0 / 133.0
    BASE_PASS_COUNT = 113
    BASE_FAIL_COUNT = 133

    def test_A5_current_implementation_baseline(self):
        """A5：现行实现跑**实时**生产语料 —— 只钉「率不恶化」。

        历史数字（t5 复核报告）：ac27d91^ FP 8/113、FN(A) 2/100、FN(B) 35/133；
        44d3977 FP 5/113、FN(A) 3/100、FN(B) 36/133；t17 之后 FN(B) 大幅下降。

        ⚠️ 2026-10-13 t14：原断言钉的是**绝对条数**（FP <= 5、FN(B) <= 36）。
        语料从 274 records（113/161）涨到 299（124/175）后，即使实现的误判**率**
        一字未变，绝对条数也会同步上涨 —— 那是语料增长的算术后果，不是回归
        （实测：只加 50 条新记录就能让 FP 由 3 涨到 6，旧口径直接红）。
        故改钉**率**（fp/达标数、fn/不合格数），并保留一条兜底：当实时语料规模
        未超基线时，额外钉更严的绝对上界，防止用比率把标准偷偷放宽。
        """
        n_pass = sum(1 for r in _CORPUS if r["passed"] is True)
        n_fail = sum(1 for r in _CORPUS if r["passed"] is False)
        self.assertGreater(n_pass, 0, "实时语料没有达标记录，基线无法计算")
        self.assertGreater(n_fail, 0, "实时语料没有不合格记录，基线无法计算")
        fp = self._fp(LIVE)
        fn = self._fn(LIVE, self.ROOTS_WIDE)
        fp_rate = len(fp) / n_pass
        fn_rate = len(fn) / n_fail
        self.assertLessEqual(
            fp_rate, self.FP_RATE_BASELINE,
            "达标误判率 %.2f%% 超过复核基线 %.2f%%（%d 条达标里误判 %d 条）"
            % (fp_rate * 100, self.FP_RATE_BASELINE * 100, n_pass, len(fp)))
        self.assertLessEqual(
            fn_rate, self.FN_WIDE_RATE_BASELINE,
            "口径 B 漏判率 %.2f%% 超过复核基线 %.2f%%（%d 条不合格里漏判 %d 条）"
            % (fn_rate * 100, self.FN_WIDE_RATE_BASELINE * 100, n_fail, len(fn)))
        # 语料规模未超基线时，绝对上界更严 —— 同时钉住，不许只靠比率兜底
        if n_pass <= self.BASE_PASS_COUNT:
            self.assertLessEqual(len(fp), 5,
                                 "语料规模未超基线，达标误判绝对条数超过 5")
        if n_fail <= self.BASE_FAIL_COUNT:
            self.assertLessEqual(len(fn), 36,
                                 "语料规模未超基线，口径 B 漏判绝对条数超过 36")


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
        """E3：生产语料里那类「不足构成 = 否定判据，不是否定雷同」的实证。

        ⚠️ t14：原断言 `len(hit) == 1` 是绝对条数 —— 语料再出现同类句式就假红。
        改断「至少一条，且**每一条**都复现 3→6 的行为反转」：语料增长只会多查几条，
        不会假红；行为一旦反过来（ac27 漏 / v44 命中）立刻红。
        """
        if not _CORPUS:
            self.skipTest("无生产语料")
        hit = [r for r in _CORPUS if "构图差异不足构成面板雷同" in r["reason"]]
        self.assertGreaterEqual(
            len(hit), 1,
            "实时语料里已找不到该句式 —— E3 失去实证依据，请重核归因")
        for r in hit:
            self.assertEqual(r["passed"], False)
            self.assertTrue(AC27._panel_dup_hit(r["reason"]),
                            "ac27d91 本应命中：%s" % r["file"])
            self.assertFalse(V44._panel_dup_hit(r["reason"]),
                             "44d3977 本应放过（回归）：%s" % r["file"])


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