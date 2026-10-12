# -*- coding: utf-8 -*-
"""提示词单一来源一致性测试（2026-10-11 新增；2026-10-12 t50 改为单一来源契约）。

背景：剧本生成提示词原同时存在于两处 ——
  app/prompts/script_generate.txt   （供人工审阅/直接调用的模板文件）
  app/prompt_templates.py           （代码内嵌 _DEFAULT_SCRIPT_GENERATE 硬编码副本）
实测两者有 44 行长文本重复，人工维护两份必然漂移。

2026-10-12（t50）下沉单一来源：删除 prompt_templates.py 的硬编码副本，
script_generate 的唯一来源是 app/prompts/script_generate.txt；
prompt_templates.load("script_generate") 与 novel_to_script 的 render 都引用该文件。

这些测试把**新契约**钉死：
  1. 规则存在于**唯一来源** script_generate.txt；
  2. prompt_templates.py **不再**硬编码同一份正文（无 _DEFAULT_SCRIPT_GENERATE 赋值）；
  3. load("script_generate") 返回的生效正文与 txt 剥离头注释后一致（引用方一致）。
"""
import io
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.abspath(os.path.join(_HERE, os.pardir, "app"))


def _read(rel):
    p = os.path.join(_APP, rel)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return f.read()


class TestPromptSingleSourceConsistency(unittest.TestCase):
    """规则只在唯一来源 script_generate.txt，引用方（load）与之一致。"""

    def setUp(self):
        self.txt = _read(os.path.join("prompts", "script_generate.txt"))
        self.py = _read("prompt_templates.py")
        if self.txt is None or self.py is None:
            self.skipTest("提示词文件缺失，跳过")

    def _effective(self):
        """load("script_generate") 返回的生效正文（= txt 剥离头注释）。"""
        if _APP not in sys.path:
            sys.path.insert(0, _APP)
        import prompt_templates
        return prompt_templates.load("script_generate")

    def _assert_rule_in_source(self, key, hint):
        """规则的关键短语必须在唯一来源（txt）与生效正文（load）中都出现。"""
        self.assertIn(key, self.txt, "script_generate.txt 缺规则：%s" % hint)
        eff = self._effective()
        self.assertIn(key, eff,
                      "load('script_generate') 生效正文缺规则：%s（引用方与来源不一致？）" % hint)

    def test_single_source_no_hardcoded_copy(self):
        """prompt_templates.py 不得再硬编码 script_generate 正文（单一来源已下沉）。"""
        self.assertNotIn("_DEFAULT_SCRIPT_GENERATE =", self.py,
                         "prompt_templates.py 仍有 _DEFAULT_SCRIPT_GENERATE 赋值（硬编码副本未删除）")
        self.assertNotIn('register_fallback("script_generate"', self.py,
                         "prompt_templates.py 仍在注册 script_generate 代码兜底（单一来源已下沉）")

    def test_speech_budget_is_duration_based(self):
        """台词预算必须按镜头时长算（2026-10-11 音画错配修复）。"""
        self._assert_rule_in_source("按本镜时长算", "台词预算按本镜时长算")
        self._assert_rule_in_source("duration", "台词预算里出现 duration")

    def test_speech_budget_not_legacy_fixed(self):
        """旧口径「合计不超过 {speech_budget} 字（≈3.6 秒配音）」不得残留。"""
        legacy = "合计不超过 {speech_budget} 字"
        self.assertNotIn(legacy, self.txt,
                         "script_generate.txt 仍有旧固定预算口径")
        eff = self._effective()
        self.assertNotIn(legacy, eff,
                         "load('script_generate') 生效正文仍有旧固定预算口径")

    def test_speaker_slot_rule_present_in_both(self):
        """speaker 必须是已登记角色名（禁用旁白/众人）。"""
        self._assert_rule_in_source("speaker 必须精确等于", "speaker 白名单规则")

    def test_no_narration_rule_present_in_both(self):
        """本系统不产出旁白（配音只读 dialogue）。"""
        self._assert_rule_in_source("本系统不产出旁白", "无旁白规则")

    def test_caption_rule_present_in_both(self):
        """字幕（caption）滥用限制。"""
        self._assert_rule_in_source("字幕滥用会打断观感", "caption 限制")

    def test_shared_long_lines_are_high(self):
        """守卫性统计：生效正文应保留 txt 正文（剥离头注释后）的全部长行。"""
        # 只取剥离头注释后的正文（头注释里的长行本就不进生效正文）
        txt_lines = self.txt.splitlines()
        i = 0
        while i < len(txt_lines) and txt_lines[i].lstrip().startswith("#"):
            i += 1
        while i < len(txt_lines) and not txt_lines[i].strip():
            i += 1
        lines = [l.strip() for l in txt_lines[i:] if len(l.strip()) > 25]
        if not lines:
            self.skipTest("txt 正文无长行")
        eff = self._effective()
        eff_lines = set(l.strip() for l in eff.splitlines() if l.strip())
        missing = [l for l in lines if l not in eff_lines]
        self.assertEqual(
            len(missing), 0,
            "load('script_generate') 生效正文丢失 %d 条 txt 正文长行（提示词模板结构可能被破坏）" % len(missing))

    def test_effective_equals_txt_stripped(self):
        """生效正文 = txt 剥离头注释（引用方与来源一致，无漂移）。"""
        txt_lines = self.txt.splitlines()
        i = 0
        while i < len(txt_lines) and txt_lines[i].lstrip().startswith("#"):
            i += 1
        while i < len(txt_lines) and not txt_lines[i].strip():
            i += 1
        stripped = "\n".join(txt_lines[i:])
        eff = self._effective()
        self.assertEqual(stripped.rstrip("\n"), eff.rstrip("\n"),
                         "load('script_generate') 生效正文与 txt 剥离头注释后不一致")


if __name__ == "__main__":
    unittest.main()
