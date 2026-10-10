# -*- coding: utf-8 -*-
"""回归测试：视觉探测图不得是 1x1 像素（上游会判为「无效图像」）。

背景（2026-10-11 真实故障，且我因此误判过一次）：
  「AI 设置 → 质检模型 → 测试连接」走 qc_client.test_vision()，它用一张内置的小 PNG 探测
  模型能否读图。该图原先是 **1x1 像素**，上游（FreeLLMAPI 网关）直接拒收：
      HTTP 400 {"code":"image_invalid",
               "gateway_hint":"image data rejected by upstream; use a real/valid image"}
  而 routes/ai.py 又把该 400 一律翻译成「该接口或模型不支持图像输入」，
  于是排查被引向「换模型」—— 但实测 cn:deepseek-v4.1-flash 完全支持视觉，
  换成 64x64 的真实小图后 test_vision 立即 success=True / vision=True。

  注意区分：探测的是「连通性 + 多模态能力」，图不必大，但必须是一张**真实有效**的图。
"""
import base64
import os
import struct
import sys
import unittest

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)


class TestVisionProbeImage(unittest.TestCase):
    def test_probe_is_valid_png_with_real_size(self):
        import qc_client
        raw = base64.b64decode(qc_client._PROBE_PNG_B64)
        # PNG magic
        self.assertEqual(raw[:8], b'\x89PNG\r\n\x1a\n', '探针图不是合法 PNG')
        # IHDR：宽/高各 4 字节大端
        w, h = struct.unpack('>II', raw[16:24])
        self.assertGreaterEqual(w, 16, '探针图宽 %d 太小，上游会判为无效图' % w)
        self.assertGreaterEqual(h, 16, '探针图高 %d 太小，上游会判为无效图' % h)
        # 体积约束：探测图不该很大（避免每次测试连接都传大图）
        self.assertLess(len(raw), 8192, '探针图 %d 字节过大，影响测试连接速度' % len(raw))


if __name__ == '__main__':
    unittest.main()