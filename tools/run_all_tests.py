# -*- coding: utf-8 -*-
"""本地一键门禁：跑全量 unittest discover（tests/ 下所有 test_*.py）。

为什么需要（t60）：仓库 .github/workflows 只有发布流水线，没有测试流水线，
今晚修的 cache_name 分场 / 软超时续等 / t2 合并 / fail-open / 统一隔离等都靠人跑。
本脚本把这些关键回归收敛成「一条命令跑全量」，本地/CI 都能一键触发。

用法（在项目根目录执行）：
    python tools/run_all_tests.py            # 跑全量（退出码 0=全绿）
    python tools/run_all_tests.py --verbose  # 逐条输出
    python tools/run_all_tests.py test_smoke test_prewarm  # 只跑名字匹配的

依赖：app/requirements.txt（flask/requests/Pillow/numpy/anthropic/openai/cryptography 等，
纯 CPU 依赖）。**不需要 torch / GPU / ComfyUI** —— 软超时续等、cache_name、t2 合并
都用 mock/仿真，不真连 ComfyUI；依赖生产数据的用例自带 skipUnless 自动跳过。

预计耗时：全量约 700 tests ≈ 15 秒（本机实测 707 tests / 13.9s）。
"""
import importlib.util
import os
import subprocess
import sys
import time


#: 关键依赖（缺了会 import 失败或大量 error）。app/requirements.txt 的纯 CPU 子集。
_REQUIRED = ["flask", "flask_cors", "requests", "PIL", "numpy", "anthropic",
             "openai", "cryptography", "dotenv", "waitress"]


def _check_deps():
    """检查关键依赖；缺失则打印安装命令并返回 False。"""
    missing = [m for m in _REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        print("[缺依赖] 请先安装 app/requirements.txt：")
        print("    pip install -r app/requirements.txt")
        print("  缺失模块：%s" % ", ".join(missing))
        return False
    return True


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args = sys.argv[1:]
    filters = [a for a in args if not a.startswith("--")]
    verbose = "--verbose" in args or "-v" in args

    if not _check_deps():
        return 2

    cmd = [sys.executable, "-W", "ignore::ResourceWarning",
           "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    if verbose:
        cmd.append("-v")
    if filters:
        # unittest 不支持按名过滤，退化为逐个跑匹配的模块
        import glob
        mods = []
        for pat in filters:
            mods.extend(glob.glob(os.path.join(root, "tests", "test_*%s*.py") % pat))
        if not mods:
            print("没有匹配的测试模块：%s" % filters)
            return 2
        cmd = [sys.executable, "-W", "ignore::ResourceWarning",
               "-m", "unittest"] + sorted(set(mods))

    print("=== 本地一键门禁：unittest discover -s tests ===")
    print("命令: %s" % " ".join(os.path.basename(c) for c in cmd[:4]) + " ...")
    print("依赖: app/requirements.txt（纯 CPU，无需 torch/GPU/ComfyUI）")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=root)
    dt = time.time() - t0
    print("")
    print("=== 用时 %.1fs，退出码 %d（0=全绿）===" % (dt, r.returncode))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
