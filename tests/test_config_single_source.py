# -*- coding: utf-8 -*-
"""配置「单一事实源」回归测试（2026-10-10）。

背景：用户指出「同一个值散在 5 处，改一处漏四处」。排查后确认两类问题：
  ① 真 bug：若干模块用 __file__ 的上级目录自算数据目录，绕过 PROJECT_DATA_DIR ——
     设置 MJSCXT_DATA_DIR（桌面版必设）后，它们指向代码目录，而产物在数据根。
     涉及 nle_export（视频/分镜/配音/导出）、consistency（连贯性）、script_generator
     （qc_config）、analytics（成本数据）、ai_selfcheck（AI 配置）内部路径。
  ② 重复定义：MODULES 三处同值 —— 已统一到 config.AI_MODULES。

本测试把这两类钉死：新增模块若再自算数据目录、或再重复定义这些常量，直接失败。
"""
import os
import sys
import unittest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


class TestPathsFollowDataRoot(unittest.TestCase):
    """产物目录必须跟随 PROJECT_DATA_DIR，不得用 __file__ 自算。"""

    def setUp(self):
        os.environ.setdefault("MJSCXT_DATA_DIR",
                              os.path.join(os.environ.get("APPDATA", ""),
                                           "mjscxt-desktop", "mjscxt-data"))
        os.environ["MJSCXT_AUTOPILOT"] = "0"
        import config
        self.C = config

    def test_nle_export_paths_match_config(self):
        """nle_export 的 4 个目录必须与 config 完全一致（曾经指向代码目录）。"""
        import nle_export as N
        for name in ("VIDEOS_DIR", "STORYBOARDS_DIR", "DUB_DIR", "EXPORT_DIR"):
            self.assertTrue(hasattr(self.C, name), "config 缺少 %s" % name)
            self.assertEqual(os.path.abspath(getattr(N, name)),
                             os.path.abspath(getattr(self.C, name)),
                             "nle_export.%s 与 config 不一致（绕过数据根？）" % name)

    def test_consistency_continuity_dir_matches_config(self):
        import consistency as S
        self.assertEqual(os.path.abspath(S.CONTINUITY_DIR),
                         os.path.abspath(self.C.CONTINUITY_DIR))

    def test_analytics_dir_follows_data_root(self):
        import analytics as A
        self.assertTrue(os.path.abspath(A.ANALYTICS_DIR)
                        .startswith(os.path.abspath(self.C.PROJECT_DATA_DIR)),
                        "analytics 目录不在数据根内")

    def test_script_generator_uses_config_qc_path(self):
        import script_generator as SG
        if hasattr(SG, "QC_CONFIG_PATH"):
            self.assertEqual(os.path.abspath(SG.QC_CONFIG_PATH),
                             os.path.abspath(self.C.QC_CONFIG_PATH))
        self.assertFalse(hasattr(SG, "_PROJECT_ROOT"),
                         "script_generator 又出现了自算的 _PROJECT_ROOT")

    def test_ai_selfcheck_uses_config_paths(self):
        import ai_selfcheck as AS
        self.assertEqual(os.path.abspath(AS._AI_CONFIG_PATH),
                         os.path.abspath(self.C.AI_CONFIG_PATH))
        self.assertEqual(os.path.abspath(AS._LLM_CONFIG_PATH),
                         os.path.abspath(self.C.LLM_CONFIG_PATH))

    def test_no_dead_root_vars(self):
        """清掉的死变量不得回归。"""
        import ai_qc_judge
        import plugin_registry
        self.assertFalse(hasattr(ai_qc_judge, "_PROJECT_ROOT"))
        self.assertFalse(hasattr(plugin_registry, "_ROOT_DIR"))


class TestSingleSourceConstants(unittest.TestCase):

    def test_modules_is_shared_object(self):
        """MODULES 必须三处共用同一个对象，而不是各自复制一份同值元组。"""
        import config as C
        import ai_config
        import ai_credentials_db
        import ai_selfcheck
        for m in (ai_config, ai_credentials_db, ai_selfcheck):
            self.assertIs(getattr(m, "MODULES"), C.AI_MODULES,
                          "%s.MODULES 不是 config.AI_MODULES" % m.__name__)

    def test_shot_count_alias_resolves(self):
        """镜数的三个历史名字必须都归一到同一正式键。"""
        import config_center as CC
        for k in ("target_shots", "shots_per_episode", "novel_default_shots"):
            self.assertEqual(CC.canonical_key(k), "novel_default_shots")

    def test_project_default_has_no_duplicate_shot_key(self):
        """项目默认配置里不得再出现第二个镜数键。"""
        import config as C
        d = C.PROJECT_DEFAULT_CONFIG
        self.assertIn("target_shots", d)
        self.assertNotIn("shots_per_episode", d,
                         "PROJECT_DEFAULT_CONFIG 又混入了第二个镜数键")

    def test_upscale_default_off(self):
        """超分默认关闭（用户 2026-10-10 指定）。"""
        import autopilot as A
        self.assertFalse(A.PLAN_DEFAULTS["enable_upscale"])

    def test_no_fixed_shot_suggestion_in_agent_core(self):
        """AI 总控的参数说明里不得再出现固定镜数建议（用户两次纠正）。"""
        p = os.path.join(APP, "agent_core.py")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        for bad in ("默认 12", "建议 18~30", "建议 8~14", "常规叙事 12~18"):
            self.assertNotIn(bad, text, "agent_core 又出现固定镜数建议：%s" % bad)


if __name__ == "__main__":
    unittest.main()
