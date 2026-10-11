# -*- coding: utf-8 -*-
"""提示词双份拷贝的一致性测试（2026-10-11 新增）。

背景：剧本生成提示词同时存在于两个地方 ——
  app/prompts/script_generate.txt   （供人工审阅/直接调用的模板文件）
  app/prompt_templates.py           （代码内嵌的同一段模板，实际注入用）
实测两者有 44 行长文本重复（txt 长行的 57%）。人工维护两份必然漂移：
本轮修「台词预算」时就必须同时改 script_generate.txt / prompt_templates.py / config.py
三处，漏一处口径就分叉，而分叉后**没有任何测试会报警**。

这些测试把「两条链路必须同口径」钉死：任何一边单独改动都会被立刻发现。
"""
import io
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.abspath(os.path.join(_HERE, os.pardir, "app"))


def _read(rel):
    p = os.path.join(_APP, rel)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return f.read()


class TestPromptDuplicationConsistency(unittest.TestCase):
    """同一段规则在 txt 与 py 两处必须都能找到（防止只改一边）。"""

    def setUp(self):
        self.txt = _read(os.path.join("prompts", "script_generate.txt"))
        self.py = _read("prompt_templates.py")
        if self.txt is None or self.py is None:
            self.skipTest("提示词文件缺失，跳过")

    def _assert_rule_in_both(self, key, hint):
        """规则的关键短语必须在两处都出现。"""
        self.assertIn(key, self.txt, "script_generate.txt 缺规则：%s" % hint)
        self.assertIn(key, self.py, "prompt_templates.py 缺规则：%s（只改了 txt？）" % hint)

    def test_speech_budget_is_duration_based(self):
        """台词预算必须按镜头时长算（2026-10-11 音画错配修复），两边都要改。"""
        self._assert_rule_in_both("按本镜时长算", "台词预算按本镜时长算")
        self._assert_rule_in_both("duration", "台词预算里出现 duration")

    def test_speech_budget_not_legacy_fixed(self):
        """旧口径「合计不超过 {speech_budget} 字（≈3.6 秒配音）」不得残留。"""
        legacy = "合计不超过 {speech_budget} 字"
        self.assertNotIn(legacy, self.txt,
                         "script_generate.txt 仍有旧固定预算口径")
        self.assertNotIn(legacy, self.py,
                         "prompt_templates.py 仍有旧固定预算口径")

    def test_speaker_slot_rule_present_in_both(self):
        """speaker 必须是已登记角色名（禁用旁白/众人）——两边一致。"""
        self._assert_rule_in_both("speaker 必须精确等于", "speaker 白名单规则")

    def test_no_narration_rule_present_in_both(self):
        """本系统不产出旁白（配音只读 dialogue）——两边一致。"""
        self._assert_rule_in_both("本系统不产出旁白", "无旁白规则")

    def test_caption_rule_present_in_both(self):
        """字幕（caption）滥用限制 —— 两边一致。"""
        self._assert_rule_in_both("字幕滥用会打断观感", "caption 限制")

    def test_shared_long_lines_are_high(self):
        """守卫性统计：两份拷贝的共有长行不应骤降（骤降说明有人只改了单边或删了模板）。"""
        lines = [l.strip() for l in self.txt.splitlines() if len(l.strip()) > 25]
        if not lines:
            self.skipTest("txt 无长行")
        shared = sum(1 for l in lines if l[:40] in self.py)
        ratio = shared / float(len(lines))
        self.assertGreater(
            ratio, 0.3,
            "两处共有长行占比仅 %.2f（历史值约 0.57）——提示词模板可能只改了一边或结构被破坏" % ratio)


if __name__ == "__main__":
    unittest.main()
