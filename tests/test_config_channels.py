# -*- coding: utf-8 -*-
"""配置通道一致性回归测试（2026-10-12 T11）

修的是什么
----------
「配置写了却不生效、且没有任何提示」：托管/自主两条链路对**同一组布尔开关**的接受
集合不一致，导致请求过了白名单、却在 autopilot.set_plan 被静默丢弃（接口 200 OK，
用户以为关掉了参考音色合成，实际照跑）。实测三处口径：

  · 通道 A：autopilot.PLAN_DEFAULTS       —— set_plan 实际接受的键（唯一权威）
  · 通道 B：routes/autopilot.py plan_set  —— 原先按 k in PLAN_DEFAULTS **静默过滤**
  · 通道 C：routes/autonomous.py start    —— 原先是另一张手写白名单
            （含 enable_keyframe / prewarm_asset_prompt / video_mode 等计划不接受的键）

修完以后的判定口径：三处一律走 autopilot.split_plan_patch / PLAN_DEFAULTS；
不在集合里的键**必须给出反馈** —— 计划接口整笔 400（不做部分生效），
一键启动把 rejected/reasons 写进响应并记日志。H3_AUDIO_SOURCE 定为保留字段
（零消费者，见 app/config.py 的说明与下方 TestH3AudioSourceReserved 的守卫）。

本测试不联网、不依赖 GPU / ComfyUI：Flask 只在进程内建 test_client，
数据根落临时目录。
运行：python -m unittest discover -s tests
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)
# 与其它测试一致：数据根落临时目录（set_plan 会往 <数据根>/autopilot/<项目>/plan.json 写）
os.environ.setdefault('MJSCXT_DATA_DIR', tempfile.mkdtemp(prefix='chantest_'))

import autopilot  # noqa: E402

from flask import Flask  # noqa: E402

import routes.autonomous as route_auto  # noqa: E402
import routes.autopilot as route_ap  # noqa: E402

_ACCEPTED = sorted(autopilot.PLAN_DEFAULTS)


def _route_src(mod):
    with io.open(mod.__file__, encoding='utf-8') as fh:
        return fh.read()


class TestAcceptSet(unittest.TestCase):
    """通道 A：接受集合就是 PLAN_DEFAULTS 的键（且 enable_tts / enable_mix 在内）"""

    def test_every_default_key_is_accepted(self):
        split = autopilot.split_plan_patch(dict(autopilot.PLAN_DEFAULTS))
        self.assertEqual(split["rejected"], {}, 'PLAN_DEFAULTS 自己的键被拒：%s'
                         % sorted(split["rejected"]))
        self.assertEqual(sorted(split["accepted"]), _ACCEPTED)

    def test_audio_switches_are_settable(self):
        """本轮口径的核心：enable_tts 必须真正可控；enable_mix 可写只是不再默认开。"""
        self.assertIn('enable_tts', autopilot.PLAN_DEFAULTS)
        self.assertIn('enable_tts_pre', autopilot.PLAN_DEFAULTS)
        self.assertIn('enable_mix', autopilot.PLAN_DEFAULTS)
        self.assertIs(autopilot.PLAN_DEFAULTS['enable_mix'], False,
                      '本轮口径要求 mix 保持关闭')

    def test_retired_keys_rejected_with_reason(self):
        self.assertTrue(autopilot.PLAN_RETIRED_KEYS, '原因表为空，等于没有解释')
        for k, why in autopilot.PLAN_RETIRED_KEYS.items():
            self.assertNotIn(k, autopilot.PLAN_DEFAULTS,
                             '%s 既在原因表又在接受集合里，口径自相矛盾' % k)
            split = autopilot.split_plan_patch({k: 'x'})
            self.assertEqual(split["rejected"], {k: 'x'})
            self.assertTrue(str(split["reasons"][k]).strip())
            self.assertEqual(split["reasons"][k], why)

    def test_unknown_key_gets_fallback_reason(self):
        split = autopilot.split_plan_patch({'definitely_not_a_plan_key': 1})
        self.assertEqual(split["accepted"], {})
        self.assertEqual(sorted(split["rejected"]), ['definitely_not_a_plan_key'])
        self.assertTrue(str(split["reasons"]['definitely_not_a_plan_key']).strip())


class TestRoundTrip(unittest.TestCase):
    """写入被接受的键后必须能读回（计划文件 + get_plan 两条路径）"""

    def setUp(self):
        self.project = 't11_rt_%d' % (id(self) % 100000)

    def test_enable_tts_roundtrip(self):
        self.assertIs(autopilot.get_plan(self.project)['enable_tts'], True)
        autopilot.set_plan(self.project, {'enable_tts': False})
        self.assertIs(autopilot.get_plan(self.project)['enable_tts'], False)
        # 落盘文件里也必须是 False（不是只改了内存）
        raw = json_load(autopilot.plan_path(self.project))
        self.assertIs(raw['enable_tts'], False)

    def test_enable_tts_pre_roundtrip(self):
        autopilot.set_plan(self.project, {'enable_tts_pre': False})
        self.assertIs(autopilot.get_plan(self.project)['enable_tts_pre'], False)
        autopilot.set_plan(self.project, {'enable_tts_pre': True})
        self.assertIs(autopilot.get_plan(self.project)['enable_tts_pre'], True)

    def test_enable_mix_default_closed_but_settable(self):
        self.assertIs(autopilot.get_plan(self.project)['enable_mix'], False)
        autopilot.set_plan(self.project, {'enable_mix': True})
        self.assertIs(autopilot.get_plan(self.project)['enable_mix'], True,
                      '写进去读不回来＝通道仍然不通')

    def test_non_strict_writes_accepted_and_reports_rejected(self):
        rep = autopilot.set_plan_report(self.project,
                                        {'enable_tts': False, 'enable_keyframe': True})
        self.assertTrue(rep['ok'])
        self.assertEqual(rep['accepted'], ['enable_tts'])
        self.assertEqual(sorted(rep['rejected']), ['enable_keyframe'])
        self.assertIs(autopilot.get_plan(self.project)['enable_tts'], False)

    def test_strict_is_all_or_nothing(self):
        rep = autopilot.set_plan_report(self.project,
                                        {'enable_tts': False, 'enable_keyframe': True},
                                        strict=True)
        self.assertFalse(rep['ok'])
        self.assertIn('enable_keyframe', rep['error'])
        self.assertIs(autopilot.get_plan(self.project)['enable_tts'], True,
                      'strict 模式下仍发生了部分写入')


def json_load(path):
    import json as _json
    with io.open(path, encoding='utf-8') as fh:
        return _json.load(fh)


class TestRouteChannels(unittest.TestCase):
    """通道 B / C：两个接口的接受集合与 PLAN_DEFAULTS 一致，且拒绝时都有明确反馈"""

    @classmethod
    def setUpClass(cls):
        cls.app = Flask('t11_channels')
        cls.app.register_blueprint(route_ap.autopilot_bp)
        cls.app.register_blueprint(route_auto.autonomous_bp)
        cls.client = cls.app.test_client()

    def test_plan_set_accepts_audio_switches_and_reads_back(self):
        r = self.client.post('/api/autopilot/plan/t11_ch', json={
            'enable_tts': False, 'enable_tts_pre': False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        body = r.get_json()
        self.assertEqual(body['accepted'], ['enable_tts', 'enable_tts_pre'])
        g = self.client.get('/api/autopilot/plan/t11_ch').get_json()['plan']
        self.assertIs(g['enable_tts'], False)
        self.assertIs(g['enable_tts_pre'], False)

    def test_plan_set_rejects_unknown_key_with_400(self):
        before = self.client.get('/api/autopilot/plan/t11_ch').get_json()['plan']
        r = self.client.post('/api/autopilot/plan/t11_ch',
                             json={'enable_tts': True, 'enable_keyframe': True})
        self.assertEqual(r.status_code, 400, '不支持字段却没有报错（旧行为是静默丢弃）')
        body = r.get_json()
        self.assertFalse(body['success'])
        self.assertEqual(sorted(body['rejected']), ['enable_keyframe'])
        self.assertIn('enable_keyframe', body['reasons'])
        self.assertEqual(body['accepted_keys'], _ACCEPTED,
                         '接口回给前端的可用字段与 PLAN_DEFAULTS 不一致')
        after = self.client.get('/api/autopilot/plan/t11_ch').get_json()['plan']
        self.assertEqual(after['enable_tts'], before['enable_tts'],
                         '整笔拒绝却仍写入了部分字段')

    def test_plan_set_ignores_readonly_echo_keys(self):
        """GET 会把 updated_at 一起返回；读回来再写回去不该被判非法。"""
        r = self.client.post('/api/autopilot/plan/t11_ch', json={
            'enable_tts': False, 'updated_at': '2026-10-12 00:00:00'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])

    def test_autonomous_start_reports_ignored_keys(self):
        with mock.patch.object(route_auto, '_style_aspect_guard',
                               lambda *a, **k: None), \
             mock.patch.object(route_auto.autonomous, 'start_autonomous',
                               mock.MagicMock(return_value={'success': True})) as m:
            r = self.client.post('/api/autonomous/start', json={
                'project_name': 't11_ch', 'novel_id': 'fake_novel',
                'enable_tts': False,
                'enable_keyframe': True, 'prewarm_asset_prompt': True,
                'enable_storyboard': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        body = r.get_json()
        self.assertEqual(sorted(body['rejected']),
                         ['enable_keyframe', 'enable_storyboard', 'prewarm_asset_prompt'])
        self.assertIn('enable_keyframe', body['ignored'])
        self.assertIn('未生效', body['warning'])
        # 真正有效的那个键必须原样传给 start_autonomous（三通道一致）
        passed = m.call_args[0][2]
        self.assertEqual(passed, {'enable_tts': False})

    def _enable(self, payload):
        """/enable 前面有一道 AI 配置门禁（测试环境没有 AI 配置）→ 这里只为通道判定放行门禁。

        ⚠️ 同时把 autopilot._ensure_thread 打掉：enable() 会拉起托管守护线程，
        测试进程里那条线程会一直扫 enabled_projects()（进而 _A() 抛「宿主模块 app 尚未加载」），
        给整个测试会话制造噪音与竞态 —— 本用例只验通道判定，不需要真起线程。
        """
        with mock.patch.object(route_ap, '_ai_gate_or_400', lambda *a, **k: None), \
             mock.patch.object(autopilot, '_ensure_thread', lambda *a, **k: None):
            return self.client.post('/api/autopilot/enable', json=payload)

    def test_enable_endpoint_rejects_unknown_key(self):
        """/enable 也是同一个通道（内部走 set_plan），同样不得静默丢弃。"""
        r = self._enable({'project': 't11_ch', 'enable_tts': False,
                          'enable_keyframe': True})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:300])
        body = r.get_json()
        self.assertEqual(sorted(body['rejected']), ['enable_keyframe'])
        self.assertEqual(body['accepted_keys'], _ACCEPTED)

    def test_enable_endpoint_accepts_audio_switch(self):
        r = self._enable({'project': 't11_ch', 'enable_mix': False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        p = self.client.get('/api/autopilot/plan/t11_ch').get_json()['plan']
        self.assertIs(p['enable_mix'], False)

    def test_routes_have_no_second_key_list(self):
        """两个路由都不得再手写一份键清单（这正是本次缺陷的成因）。"""
        for mod in (route_ap, route_auto):
            src = _route_src(mod)
            self.assertIn('split_plan_patch', src,
                          '%s 没有走 autopilot.split_plan_patch' % mod.__name__)
            # 只看「引号包起来的键名」＝真正的白名单/字典键；注释里提到这些名字不算
            for kw in ('enable_keyframe', 'prewarm_asset_prompt', 'video_mode'):
                for quoted in ("'%s'" % kw, '"%s"' % kw):
                    self.assertNotIn(quoted, src,
                                     '%s 又出现了手写白名单键 %s' % (mod.__name__, kw))


class TestH3AudioSourceReserved(unittest.TestCase):
    """H3_AUDIO_SOURCE = 保留字段（零消费者）：有结论、有说明、有守卫"""

    def test_flag_is_bool(self):
        import config
        self.assertIsInstance(config.H3_AUDIO_SOURCE, bool)
        self.assertIs(config.H3_AUDIO_SOURCE, False)

    def test_no_consumer_anywhere_in_app(self):
        hits = []
        for fn in sorted(os.listdir(_APP)):
            if not fn.endswith('.py'):
                continue
            with io.open(os.path.join(_APP, fn), encoding='utf-8') as fh:
                for i, line in enumerate(fh, 1):
                    if 'H3_AUDIO_SOURCE' in line:
                        hits.append('%s:%d' % (fn, i))
        files = sorted({h.split(':')[0] for h in hits})
        self.assertEqual(files, ['config.py'],
                         'H3_AUDIO_SOURCE 出现到了别的模块：要么真的接上消费者并更新'
                         'config.py 的「保留字段」说明，要么保持零消费者（实测 %s）' % hits)
        self.assertTrue(hits, 'H3_AUDIO_SOURCE 定义都不见了？')

    def test_source_explains_reserved_status_and_warns(self):
        with io.open(os.path.join(_APP, 'config.py'), encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('保留字段', src)
        self.assertIn('if H3_AUDIO_SOURCE:', src)
        self.assertIn('没有消费者', src)


if __name__ == '__main__':
    unittest.main()
