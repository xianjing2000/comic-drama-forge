# -*- coding: utf-8 -*-
"""质检「重试止损」回归测试（2026-09-20）

背景：单镜质检不达标会「改提示词 + 换种子」重试（max_retries=5 → 最多 6 次尝试），
每次都要跑一轮 GPU。实测 ep02 shot_13 连续 6 次的缺陷**一字不差**（全是同一个
「景别不符」），等于十几分钟 GPU 全打水漂。若连续两次缺陷特征完全相同，就说明
「改提示词 + 换种子」根本没产生任何变化，继续重试只是重复烧卡。

新增 `_qc_retry_hopeless(attempts)` 判定，并在分镜 / 视频两处重试循环里接入止损。
本测试锁死它的判定口径（含「保守边界」）与两处接入的存在性。

运行：MJSCXT_AUTOPILOT=0 C:/Python314/python.exe .workbuddy/test/verify_qc_retry_stop.py
"""
import os
import sys

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))
sys.path.insert(0, APP_DIR)
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")

PASS = 0
FAIL = 0


def check(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s    <<< %s" % (name, detail))


def section(title):
    print("\n== %s ==" % title)


import app as app_mod  # noqa: E402

with open(os.path.join(APP_DIR, "app.py"), "r", encoding="utf-8") as f:
    APP_SRC = f.read()

HOPELESS = app_mod._qc_retry_hopeless
FEATURES = app_mod._qc_repeat_features


def rec(issues=None, critical=None, style_issues=None, score=70, seed=1):
    return {"attempt": 1, "seed": seed, "score": score,
            "issues": issues or [], "critical_issues": critical or [],
            "style_issues": style_issues or [], "reason": "x", "ok": True, "passed": False}


# ============ 1. 基本判定 ============
section("1 基本判定（连续两次缺陷完全相同 → 止损）")
check(HOPELESS(None) == (False, ""), "attempts=None → 不停（保守）")
check(HOPELESS([]) == (False, ""), "attempts=[] → 不停（保守）")
check(HOPELESS([rec(["景别不符"])]) == (False, ""), "仅 1 次 → 不停（不足 streak）")

_same = [rec(["景别不符：要求特写，实际为中景"]), rec(["景别不符：要求特写，实际为中景"])]
_ok, _detail = HOPELESS(_same)
check(_ok is True, "连续 2 次缺陷相同 → 止损", _detail)
check("景别不符" in _detail, "止损时回传缺陷摘要（可观测 / 可追溯）", _detail)

_diff = [rec(["景别不符"]), rec(["手指异常，出现六指"])]
check(HOPELESS(_diff) == (False, ""), "两次缺陷不同 → 继续重试（不误停）")

# ============ 2. 特征来源（issues / critical_issues / style_issues）============
section("2 特征来源（三类缺陷文本都参与判定）")
check(HOPELESS([rec(critical=["画面崩坏"]), rec(critical=["画面崩坏"])])[0] is True,
      "仅 critical_issues 相同 → 也判定止损")
check(HOPELESS([rec(style_issues=["风格不符"]), rec(style_issues=["风格不符"])])[0] is True,
      "仅 style_issues 相同 → 也判定止损")
check(HOPELESS([rec(["a"], critical=["b"]), rec(["a"], critical=["b"]),
                rec(["a"], critical=["b"])])[0] is True,
      "多类特征混合且完全一致 → 止损")
check(FEATURES(rec(["a"], critical=["b"], style_issues=["c"])) == frozenset({"a", "b", "c"}),
      "_qc_repeat_features 合并三类特征")

# ============ 3. 保守边界（宁可多试一次，也不要误停）============
section("3 保守边界")
check(HOPELESS([rec([]), rec([])]) == (False, ""),
      "★ 两次都没有缺陷文本（质检没给可用信息）→ 不停（空 ≠ 相同）")
check(HOPELESS([rec([], critical=["崩坏"]), rec([])]) == (False, ""),
      "空特征与非空不相等 → 不停")
check(HOPELESS([rec(["a", "b"]), rec(["a"])]) == (False, ""),
      "特征少一条 → 不算相同（必须集合完全相等）")
check(HOPELESS([rec(["a"]), rec(["a", "b"])]) == (False, ""),
      "特征多一条 → 不算相同")
check(HOPELESS([{"attempt": 1}, {"attempt": 2}]) == (False, ""),
      "记录缺 issues 字段（旧格式）→ 按空处理，不停")
check(HOPELESS(["脏数据", None, rec(["a"]), rec(["a"])])[0] is True,
      "非 dict 元素被忽略，不影响判定")
check(HOPELESS([rec(["a"]), rec(["a"])], streak=3) == (False, ""),
      "streak=3 时仅 2 次不足以判定")
check(HOPELESS([rec(["a"]), rec(["a"]), rec(["a"])], streak=3)[0] is True,
      "streak=3 且连续 3 次一致 → 止损")

# ============ 4. 不应被「换种子抖动」干扰 ============
section("4 换种子抖动不豁免（只看缺陷文本，不看 seed/score）")
check(HOPELESS([rec(["崩坏"], score=40, seed=1), rec(["崩坏"], score=55, seed=999)])[0] is True,
      "★ score/seed 不同但缺陷相同 → 仍判定止损（说明「改提示词+换种子」无效）")
check(FEATURES(rec(["a"], score=40)) == FEATURES(rec(["a"], score=90)),
      "特征集合不含 score/seed")
check(HOPELESS([rec(["a", "b"]), rec(["b", "a"])])[0] is True,
      "缺陷顺序不同但集合相同 → 视为相同（无需顺序一致）")

# ============ 5. 只影响「继续重试」，不改变闸门结论 ============
section("5 止损不改变闸门结论（该镜仍算未通过、仍不进正式目录）")
_verdict = {"ok": True, "passed": False, "accepted": False, "score": 50,
            "reason": "景别不符", "issues": ["景别不符：要求特写，实际为中景"]}
_gate = app_mod._qc_gate(_verdict)
check(_gate["accept"] is False, "不达标 verdict 的闸门仍然 accept=False")
check(HOPELESS(_same)[0] is True and _gate["accept"] is False,
      "止损与闸门并存：停止重试 ≠ 放行入库")
check(not app_mod._qc_gate({"ok": False, "error": "接口挂"}).get("accept"),
      "质检接口异常时闸门也阻断（止损不会把异常当通过）")

# ============ 5b. 摘要 label 体现止损 ============
section("5b 摘要 label 体现止损（用户能看到「为什么提前停了」）")
_rec_stop = {"attempt": 2, "seed": 9, "score": 50, "ok": True, "passed": False,
             "issues": ["景别不符"], "retry_stopped": True,
             "retry_stopped_features": "景别不符"}
_sum = app_mod._qc_summary([_rec_stop], True, True, 5)
check(_sum.get("retry_stopped") is True, "未通过且已止损 → summary.retry_stopped=True")
check("已停止重试" in _sum.get("label", ""), "label 写明「已停止重试」", _sum.get("label"))
check(_sum.get("retry_stopped_detail") == "景别不符", "summary 带回缺陷摘要供展示")
_sum_ok = app_mod._qc_summary([dict(_rec_stop, passed=True)], True, True, 5)
check(_sum_ok.get("retry_stopped") is False and _sum_ok.get("label") == "质检达标",
      "已通过的镜不受止损标记影响（label 仍是达标）", _sum_ok.get("label"))
_sum_norm = app_mod._qc_summary([{"attempt": 1, "passed": False, "issues": ["x"]}], True, True, 5)
check(_sum_norm.get("retry_stopped") is False and _sum_norm.get("label") == "质检不达标",
      "未止损的不达标镜 label 保持原样", _sum_norm.get("label"))
check(APP_SRC.count('rec["retry_stopped"] = True') == 2,
      "两处止损分支都给质检记录打了标记（供 _qc_summary 读取）",
      APP_SRC.count('rec["retry_stopped"] = True'))

# ============ 6. 两处接入（分镜 / 视频）============
section("6 接入点（分镜与视频两条重试循环都要止损）")
check(APP_SRC.count("_qc_retry_hopeless(attempts)") == 2,
      "★ 分镜与视频两处都接入了止损",
      APP_SRC.count("_qc_retry_hopeless(attempts)"))
check('item["qc_retry_stopped"]' in APP_SRC and 'video_item["qc_retry_stopped"]' in APP_SRC,
      "两处都把止损原因写进结果对象（前端/报告可读）")
check(APP_SRC.count('"连续两次缺陷完全相同') == 2,
      "两处止损文案一致（连续两次缺陷完全相同）")
# 止损必须在「达标入库」判断之后，否则会绕过通过的情形
_i_acc = APP_SRC.find('if gate["accept"]:', APP_SRC.find("def _storyboard_worker"))
_i_stop = APP_SRC.find("_qc_retry_hopeless(attempts)")
check(_i_acc != -1 and _i_stop != -1 and _i_acc < _i_stop,
      "★ 止损代码位于「gate['accept'] → 入库 break」之后（达标镜不受影响）")

print("\n" + "=" * 50)
print("总计 %d 项：PASS %d / FAIL %d" % (PASS + FAIL, PASS, FAIL))
if FAIL:
    print("存在失败项 ❌")
    sys.exit(1)
print("全部通过 ✅")
