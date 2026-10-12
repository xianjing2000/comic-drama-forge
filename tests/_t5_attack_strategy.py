# -*- coding: utf-8 -*-
"""t5 对抗分析：实现的「取最左即返回」策略 vs 「全部候选」策略的差集。"""
import io, json, os, re, sys, collections
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _t5_versions import load

qc = os.path.join(os.environ["APPDATA"], "mjscxt-desktop", "mjscxt-data", "output", "qc")
SEP = "，。；、,.;：:!！?？｜|\"'“”‘’"
ROOTS = ("面板", "九宫格", "格间", "动作", "构图", "机位", "景别", "姿势", "站位", "运镜", "画面", "视觉", "分镜")
SUF = ("雷同", "重复", "趋同", "单一")
STRONG = ("未出现", "不存在", "没有发现", "未发现", "不足以", "未构成", "未形成",
          "未达", "未超", "未超过", "未触及", "未见", "未存在")
WEAK = ("无", "没有", "不")


def all_cands(txt):
    out = []
    for r in ROOTS:
        i = txt.find(r)
        while i >= 0:
            for s in SUF:
                j = txt.find(s, i + len(r))
                if j >= 0 and j - (i + len(r)) <= 6 and not re.search(r"\s", txt[i:j]):
                    out.append((i, j + len(s), r, txt[i + len(r):j], s))
            i = txt.find(r, i + 1)
    return sorted(out, key=lambda x: (x[0], -x[1]))


def hit_all(txt):
    for (st, en, r, gap, s) in all_cands(txt):
        n6 = txt[max(0, st - 6):st]
        cut = max([n6.rfind(x) for x in SEP] + [-1])
        if cut >= 0:
            n6 = n6[cut + 1:]
        if any(n in n6 for n in WEAK):
            continue
        if any(n in n6 for n in STRONG):
            continue
        g = gap.strip()
        if g and g in WEAK:
            continue
        return True
    return False


def corpus():
    out = []
    for dp, dn, fns in os.walk(qc):
        for fn in fns:
            if not fn.endswith(".json"):
                continue
            p = os.path.join(dp, fn)
            try:
                j = json.load(io.open(p, encoding="utf-8"))
            except Exception:
                continue
            for r in (j.get("records") or []) if isinstance(j, dict) else []:
                if isinstance(r, dict):
                    out.append({"file": os.path.relpath(p, qc), "shot": j.get("shot_id"),
                                "passed": r.get("passed"), "reason": str(r.get("reason") or "")})
    return out


recs = corpus()
mods = {t: load(rev, t) for rev, t in (("ac27d91^", "pre"), ("ac27d91", "ac27"),
                                       ("44d3977", "v44"), ("HEAD", "head"))}
print("records:", len(recs))
print()
print("%-6s %-10s %-10s %s" % ("ver", "hit(impl)", "hit(all)", "impl-missed"))
for tag in ("pre", "ac27", "v44", "head"):
    m = mods[tag]
    hi = sum(1 for r in recs if m._panel_dup_hit(r["reason"]))
    ha = sum(1 for r in recs if hit_all(r["reason"]))
    miss = [r for r in recs if hit_all(r["reason"]) and not m._panel_dup_hit(r["reason"])]
    print("%-6s %-10d %-10d %d" % (tag, hi, ha, len(miss)))

allmiss = [r for r in recs if hit_all(r["reason"]) and not any(
    mods[t]._panel_dup_hit(r["reason"]) for t in ("pre", "ac27", "v44", "head"))]
print()
print("=== 所有版本都漏掉的生产记录（num=%d）===" % len(allmiss))
for r in allmiss:
    print("  %s | %s | passed=%s" % (r["file"], r["shot"], r["passed"]))
    print("      %s" % r["reason"][:260])
    for (st, en, rt, gap, s) in all_cands(r["reason"])[:6]:
        print("       cand st=%d root=%s gap=%r suf=%s" % (st, rt, gap, s))

print()
print("=== 只被 v44/HEAD 漏掉、pre/ac27 能命中的（弱窗口 3->6 净损失）===")
for r in recs:
    a = mods["ac27"]._panel_dup_hit(r["reason"])
    c = mods["v44"]._panel_dup_hit(r["reason"])
    if a and not c:
        print("  %s | %s | passed=%s" % (r["file"], r["shot"], r["passed"]))
        print("      %s" % r["reason"][:320])
