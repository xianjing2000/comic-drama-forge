# -*- coding: utf-8 -*-
"""t5 独立复核：把 3 个版本（ac27d91 / 44d3977 / 当前工作树）分别注入 Python 并对比。

用法: python tests/_t5_versions.py
只读：不改 app/*.py，仅在 tests/ 下留痕。
"""
import io, os, subprocess, sys, tempfile, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
OUT = os.path.join(ROOT, "tests", "_t5_versions_out")


def git_show(rev, path):
    r = subprocess.run(["git", "show", "%s:%s" % (rev, path)], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise SystemExit("git show failed %s:%s -> %s" % (rev, path, r.stderr.decode("utf-8", "replace")))
    return r.stdout.decode("utf-8")


def load(rev, tag):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "sb_%s.py" % tag)
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write(git_show(rev, "app/sb_helpers.py"))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    if APP not in sys.path:
        sys.path.insert(0, APP)
    spec = importlib.util.spec_from_file_location("sb_%s" % tag, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sb_%s" % tag] = mod
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    for rev, tag in (("ac27d91", "ac27"), ("44d3977", "v44"), ("HEAD", "head")):
        m = load(rev, tag)
        print(tag, rev, m.__file__)
        print("   tables:", getattr(m, "_PANEL_DUP_NEG_STRONG", None), getattr(m, "_PANEL_DUP_NEG_WEAK", None))
        print("   sep   :", getattr(m, "_PANEL_DUP_SEP", None))
