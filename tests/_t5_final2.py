# -*- coding: utf-8 -*-
"""t5 最终测量：历史提交 + 冻结的当前实现，一次算清所有 A–E 数字。"""
import io, importlib.util, json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _t5_versions import load

FROZEN = os.path.join(HERE, "_t5_frozen", "sb_helpers_live_1620.py")


def load_frozen():
    spec = importlib.util.spec_from_file_location("sb_t5_frozen", FROZEN)
    m = importlib.util.module_from_spec(spec)
    sys.modules["sb_t5_frozen"] = m
    spec.loader.exec_module(m)
    return m


M = [("pre", load("ac27d91^", "pre")), ("ac27", load("ac27d91", "ac27")),
     ("v44", load("44d3977", "v44")), ("live", load_frozen())]
NAMES = [n for n, _ in M]

SEP = "，。；、,.;：:!！?？｜|\"'“”‘’"
ROOTS_B = ("面板", "九宫格", "格间", "动作", "构图", "机位", "景别", "姿势", "站位", "运镜", "画面", "视觉", "分镜")
ROOTS_A = ("面板", "九宫格", "格间")
SUF = ("雷同", "重复", "趋同", "单一")
PURE = ("未出现", "未存在", "不存在", "没有发现", "未发现", "不足以", "未构成",
        "未形成", "未达", "未超", "未超过", "未触及", "未见")


def cands(t, roots):
    out = []
    for r in roots:
        i = t.find(r)
        while i >= 0:
            for s in SUF:
                j = t.find(s, i + len(r))
                if 0 <= j - (i + len(r)) <= 6 and not re.search(r"\s", t[i:j]):
                    out.append((i, r, t[i + len(r):j], s))
            i = t.find(r, i + 1)
    return sorted(out, key=lambda x: x[0])


def indep(t, roots):
    for (i, r, gap, s) in cands(t, roots):
        pre = t[max(0, i - 6):i]
        cut = max([pre.rfind(x) for x in SEP] + [-1])
        if cut >= 0:
            pre = pre[cut + 1:]
        if any(n in pre for n in PURE):
            continue
        if gap.strip() in ("无", "没有", "不"):
            continue
        return True
    return False


qc = os.path.join(os.environ["APPDATA"], "mjscxt-desktop", "mjscxt-data", "output", "qc")
recs = []
for dp, dn, fns in os.walk(qc):
    for fn in fns:
        if fn.endswith(".json"):
            try:
                j = json.load(io.open(os.path.join(dp, fn), encoding="utf-8"))
            except Exception:
                continue
            for r in (j.get("records") or []) if isinstance(j, dict) else []:
                if isinstance(r, dict):
                    recs.append({"file": os.path.relpath(os.path.join(dp, fn), qc).replace("\\", "/"),
                                 "shot": j.get("shot_id"), "passed": r.get("passed"),
                                 "reason": str(r.get("reason") or "")})

print("生产语料 %d（passed=True %d / False %d）" % (
    len(recs), sum(1 for r in recs if r["passed"] is True), sum(1 for r in recs if r["passed"] is False)))
print()
print("%-6s | %-10s %-10s | %-10s %-10s | %s" % ("版本", "FP(A)", "FN(A)", "FP(B)", "FN(B)", "硬判语漏判"))
for n, m in M:
    fpA = [r for r in recs if r["passed"] is True and m._panel_dup_hit(r["reason"])]
    fnA = [r for r in recs if r["passed"] is False and indep(r["reason"], ROOTS_A) and not m._panel_dup_hit(r["reason"])]
    tA = [r for r in recs if r["passed"] is False and indep(r["reason"], ROOTS_A)]
    fnB = [r for r in recs if r["passed"] is False and indep(r["reason"], ROOTS_B) and not m._panel_dup_hit(r["reason"])]
    tB = [r for r in recs if r["passed"] is False and indep(r["reason"], ROOTS_B)]
    hard = [r for r in recs if any(h in r["reason"] for h in ("不达标", "阻断", "不合格"))
            and cands(r["reason"], ROOTS_B) and not m._panel_dup_hit(r["reason"])]
    print("%-6s | %-10s %-10s | %-10s %-10s | %d" % (
        n, "%d/113" % len(fpA), "%d/%d" % (len(fnA), len(tA)),
        "%d/113" % len(fpA), "%d/%d" % (len(fnB), len(tB)), len(hard)))

print()
print("=== 硬判语（不达标/阻断/不合格）+ 雷同表述 仍被放过（live）===")
live = M[3][1]
for r in recs:
    if any(h in r["reason"] for h in ("不达标", "阻断", "不合格")) and cands(r["reason"], ROOTS_B) \
            and not live._panel_dup_hit(r["reason"]):
        print("   ", r["file"], r["shot"], "|", r["reason"][:120])

print()
CASES = [
    ("换行", "本镜无畸变\n面板雷同"),
    ("换行+纯否定", "未出现畸变\n面板雷同"),
    ("全角括号", "本镜无畸变（面板雷同）"),
    ("全角引号", "本镜无畸变「面板雷同」"),
    ("全角句点", "本镜无畸变．面板雷同"),
    ("间隔号", "本镜无畸变·面板雷同"),
    ("斜杠", "本镜无畸变/面板雷同"),
    ("书名号", "本镜无畸变 《面板雷同》"),
    ("破折号", "本镜无畸变——面板雷同"),
    ("省略号截断", "未出现面板雷同不再适用……面板雷同"),
    ("同义-不足构成", "构图差异不足构成面板雷同"),
    ("同义-无畸变或", "无畸变或面板雷同"),
    ("同义-不是同一机位", "不是同一机位面板雷同"),
    ("同义-没有xx", "没有xx面板雷同"),
    ("同义-不xx", "不xx面板雷同"),
    ("同义-动作构图雷同", "九宫格9格动作构图几乎雷同，未体现动作推进，判不合格。"),
    ("同义-姿势构图雷同", "九宫格多格姿势与构图高度雷同、缺乏动作推进，未达可直接使用标准。"),
    ("达标-不足以判为", "存在轻微面板重复倾向，不足以判为面板雷同"),
    ("达标-无色块/撕裂", "无色块/撕裂/面板雷同"),
    ("达标-无色块/撕裂(短)", "色块/撕裂/面板雷同"),
    ("真缺陷-硬标点", "无畸变，面板雷同"),
    ("真缺陷-面板无雷同", "面板无雷同"),
    ("真缺陷-格间无雷同", "格间无雷同"),
]
print("=== 攻击用例矩阵（期望见括号）===")
print("%-22s %-8s %-8s %-8s %-8s" % ("用例", "pre", "ac27", "v44", "live"))
for label, t in CASES:
    print("%-22s %-8s %-8s %-8s %-8s" % (
        label, *[str(m._panel_dup_hit(t)) for _n, m in M]))
