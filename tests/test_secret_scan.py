# -*- coding: utf-8 -*-
"""密钥扫描器自身的回归测试。

背景（2026-10-10）：
  排查 GitHub PAT 暴露面时发现，光靠「翻代码」根本看不到密钥真正的落点，
  于是落了 tools/scan_secrets.py。这个扫描器如果自己会误报（把正则字面量
  当成密钥）或者漏报（只做 indexOf 前缀匹配），那它给的「干净」结论毫无价值。
  下面的用例把这些坑钉死。

注意：本文件用字符串拼接构造假密钥，文件里不出现任何可直接命中的字面量。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import scan_secrets as SS  # noqa: E402


def _fake_pat(tail='A' * 40):
    return 'github' + '_pat_' + '11CP6NHNI0BJg8yJLIT2MC_' + tail


class TestMask(unittest.TestCase):
    def test_mask_hides_middle(self):
        secret = 'github' + '_pat_' + 'B' * 40
        out = SS.mask(secret)
        self.assertNotIn(secret, out)
        self.assertNotIn('B' * 20, out)
        self.assertIn('len=%d' % len(secret), out)

    def test_mask_short_value_is_all_stars(self):
        self.assertEqual(SS.mask('abcd'), '**** (len=4)')


class TestPatterns(unittest.TestCase):
    def test_detects_fine_grained_pat(self):
        text = 'GH_TOKEN=' + _fake_pat()
        kinds = [h[0] for h in SS.scan_text(text)]
        self.assertIn('github_fine_grained_pat', kinds)

    def test_does_not_match_its_own_pattern_literal(self):
        """扫描器源码里的正则字面量不得被自己命中（否则每次扫描都自带噪声）。"""
        literal = 'github' + '_pat_' + '[A-Za-z0-9_]{20,}'
        kinds = [h[0] for h in SS.scan_text(literal)]
        self.assertNotIn('github_fine_grained_pat', kinds)

    def test_does_not_match_bare_prefixes(self):
        """单独的前缀拼接串是文档/代码里的常态，不该报。"""
        for text in ['pattern="github' + '_pat_|ghp_|gho_"',
                     "['ghp_', /gho_" + '_' + '[A-Za-z0-9]{30,}/g]',
                     'AKIA' + '[0-9A-Z]{16}']:
            kinds = [h[0] for h in SS.scan_text(text)]
            self.assertEqual(kinds, [], '误报：%r → %r' % (text, kinds))

    def test_detects_classic_pat_and_aws(self):
        text = 'a ghp_' + 'c' * 36 + ' b AKIA' + 'ABCDEFGHIJKLMNOP' + ' c'
        kinds = [h[0] for h in SS.scan_text(text)]
        self.assertIn('github_classic_pat', kinds)
        self.assertIn('aws_access_key_id', kinds)

    def test_sk_prefix_must_be_alnum_only(self):
        """sk-review-session-... 这类文件名不是 OpenAI key。"""
        self.assertEqual([h for h in SS.scan_text('file: sk-review-sess_256f6d9a-2bd2')], [])
        hits = SS.scan_text('key = sk-' + 'Z' * 40)
        self.assertIn('openai_style_key', [h[0] for h in hits])

    def test_placeholder_assignment_is_ignored(self):
        line = 'password = "' + 'p' * 30 + '"  # your_password_placeholder'
        self.assertEqual([h for h in SS.scan_text(line) if h[0] == 'keyword_assignment'], [])

    def test_real_looking_assignment_is_reported(self):
        line = 'api_key = "aB3xK9mQ7zP2wL5tR8yU1iO4eW6nQ0"'
        self.assertIn('keyword_assignment', [h[0] for h in SS.scan_text(line)])


class TestTreeScan(unittest.TestCase):
    def test_skips_skip_dirs_but_reports_env_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'node_modules').mkdir()
            (root / 'node_modules' / 'leak.env').write_text('T=' + _fake_pat(), encoding='utf-8')
            (root / '.env').write_text('T=' + _fake_pat(), encoding='utf-8')
            found = SS.scan_tree(root)
            paths = [f['path'] for f in found]
            self.assertIn('.env', paths)
            self.assertNotIn('node_modules\\leak.env', paths)
            self.assertNotIn('node_modules/leak.env', paths)

    def test_binary_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'blob.txt').write_bytes(b'\x00\x01' + _fake_pat().encode())
            self.assertEqual(SS.scan_tree(root), [])


class TestGitMetadataScan(unittest.TestCase):
    def test_detects_url_embedded_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            (root / '.git' / 'config').write_text(
                '[remote "origin"]\n\turl = https://xianjing2000:ghp_' + 'd' * 36 + '@github.com/a/b.git\n',
                encoding='utf-8')
            found = SS.scan_git_metadata(root)
            kinds = [f['kind'] for f in found]
            self.assertIn('url_embedded_credential', kinds)
            for f in found:
                self.assertNotIn('d' * 36, f['masked'])

    def test_clean_config_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            (root / '.git' / 'config').write_text(
                '[remote "origin"]\n\turl = git@github.com:a/b.git\n', encoding='utf-8')
            self.assertEqual(SS.scan_git_metadata(root), [])


class TestSessionScanGuards(unittest.TestCase):
    def test_missing_dir_returns_note_not_crash(self):
        findings, note = SS.scan_sessions(Path('Z:/definitely/not/here'))
        self.assertEqual(findings, [])
        self.assertIsNotNone(note)

    def test_missing_node_returns_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings, note = SS.scan_sessions(Path(tmp), node_exe=None)
            if SS.find_node() is None:
                self.assertIsNotNone(note)
            else:
                self.assertIsNone(note)


if __name__ == '__main__':
    unittest.main()
