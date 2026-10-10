# -*- coding: utf-8 -*-
"""回归测试：AI 凭证解密绝不得把密文当明文返回。

背景（2026-10-11 真实故障）：
  ai_credentials_db._decipher 原实现把所有解密异常都当作「历史明文行」原样返回。
  主密钥更换后（Fernet InvalidToken），密文被当成明文 api_key：
    · 对外脱敏回显变成 gAAAAA****（泄露密文前缀 + 界面乱码）；
    · 调用 AI 时把密文发给网关，认证必然失败，且日志无任何解密失败提示。
本测试锁住修复后的契约：
  · 形如 Fernet token 且解密失败 → 返回空串；
  · 真正的历史明文行 → 原样返回；
  · 脱敏回显必须是明文形态，绝不出现 Fernet 前缀。
"""
import os
import sys
import unittest

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import ai_credentials_db as ACD  # noqa: E402


class TestFernetShape(unittest.TestCase):
    def test_looks_like_fernet(self):
        self.assertTrue(ACD._looks_like_fernet('gAAAAABqxziEjCyIO-NnDfTg3G1uUg'))
        self.assertFalse(ACD._looks_like_fernet('sk-legacy-plain-key'))
        self.assertFalse(ACD._looks_like_fernet('freellma-abc-1234'))
        self.assertFalse(ACD._looks_like_fernet(''))


class TestDecipherContract(unittest.TestCase):
    def test_plaintext_line_returned_as_is(self):
        """历史明文行（非 Fernet 形状）必须原样返回。"""
        self.assertEqual(ACD._decipher('sk-legacy-plain-key'), 'sk-legacy-plain-key')

    def test_broken_fernet_never_returns_cipher(self):
        """形如 Fernet 但解不开（主密钥更换）→ 必须返回空串，绝不返回密文。"""
        broken = 'gAAAAABqyd_LFAKEFAKEFAKEFAKEFAKE' + 'X' * 24
        out = ACD._decipher(broken)
        self.assertEqual(out, '', '解密失败时绝不可返回密文（会把密文当 api_key 用）')

    def test_empty(self):
        self.assertEqual(ACD._decipher(''), '')

    def test_public_view_mask_is_not_cipher(self):
        """脱敏回显不得出现 Fernet 前缀（泄露密文/界面乱码）。"""
        for m in ('text', 'qc', 'chat'):
            v = ACD.public_view(m)
            masked = v.get('api_key_masked') or ''
            self.assertFalse(masked.startswith('gAAAAA'),
                             '模块 %s 的脱敏值泄露了密文前缀：%r' % (m, masked))


if __name__ == '__main__':
    unittest.main()