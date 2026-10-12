# -*- coding: utf-8 -*-
"""t18/t22 软超时续等回归（2026-10-12 修复）。

背景：H3 单段 ≈17min、10 段 ≈3h；旧实现到点后**无条件** interrupt + 判失败，
而总耗时与旧公式恰好重合 → 每次都在最后一刻取消「马上就好」的任务、重投，
最后一段白跑并重新排队（用户看到「永远出不来 mp4」）。超分（FlashVSR 逐帧）同理。

修复：到点先复查远端 —— 已完成 → 取回；仍在跑 → 续等（默认 3 × 1800s）；
续等用尽才 interrupt + 判超时。绝不误杀「刚好差一点」的任务。
本测试用仿真时间轴（mock time.time / cancellation.sleep）覆盖核心控制流，
不真睡、不联网、不起 ComfyUI。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import comfyui_client as cc  # noqa: E402
import upscale_client as uc  # noqa: E402


class _Clock:
    """仿真时钟：time.time() 返回当前仿真时刻；sleep 推进仿真时刻（不真睡）。"""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


def _success_entry():
    return {"status": {"completed": True, "status_str": "success"}, "outputs": {}}


def _env_ext(extensions="3", extend_sec="5"):
    """临时设置软超时续等 env，返回一个可退出清理的恢复函数。"""
    names = ("MJSCXT_WAIT_EXTENSIONS", "MJSCXT_WAIT_EXTEND_SECONDS")
    saved = {k: os.environ.get(k) for k in names}
    os.environ["MJSCXT_WAIT_EXTENSIONS"] = extensions
    os.environ["MJSCXT_WAIT_EXTEND_SECONDS"] = extend_sec

    def _restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return _restore


class TestRecheckAfterTimeout(unittest.TestCase):
    """t18：_recheck_after_timeout 的四态判定（done/error/running/gone）。"""

    def _client(self, history, queue):
        cli = cc.ComfyUIClient(base_url="http://127.0.0.1:9")
        cli.get_history = lambda pid: history
        cli._get = lambda path, *a, **k: queue if path == "/queue" else {}
        return cli

    def test_done(self):
        entry = _success_entry()
        cli = self._client({"p1": entry}, {})
        st, e = cli._recheck_after_timeout("p1")
        self.assertEqual(st, "done")
        self.assertIs(e, entry)

    def test_error(self):
        entry = {"status": {"status_str": "error"}, "outputs": {}}
        cli = self._client({"p1": entry}, {})
        st, _ = cli._recheck_after_timeout("p1")
        self.assertEqual(st, "error")

    def test_running(self):
        cli = self._client({}, {"queue_running": [[0, "p1", {}, {}, {}]], "queue_pending": []})
        st, _ = cli._recheck_after_timeout("p1")
        self.assertEqual(st, "running")

    def test_gone(self):
        cli = self._client({}, {"queue_running": [], "queue_pending": []})
        st, _ = cli._recheck_after_timeout("p1")
        self.assertEqual(st, "gone")


class TestH3SoftTimeout(unittest.TestCase):
    """t18：wait_for_completion 的软超时续等控制流。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t18_soft_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_client(self, clock, pid, done_at):
        entry = _success_entry()
        cli = cc.ComfyUIClient(base_url="http://127.0.0.1:9")
        cli.interrupts = []

        def get_history(p):
            return {p: entry} if clock.now >= done_at else {}

        def _get(path, *a, **k):
            if path == "/queue":
                alive = clock.now < done_at
                return {"queue_running": [[0, pid, {}, {}, {}]] if alive else [],
                        "queue_pending": []}
            return {}

        def interrupt(p=None):
            cli.interrupts.append(p)

        cli.get_history = get_history
        cli._get = _get
        cli.interrupt = interrupt
        return cli

    def _patches(self, clock):
        return [
            mock.patch.object(cc.time, "time", clock.time),
            mock.patch.object(cc.cancellation, "sleep", clock.sleep),
            mock.patch.object(cc.cancellation, "should_stop", return_value=False),
            mock.patch.object(cc, "COMFYUI_TEMP_DIR", self.tmp),
        ]

    def test_recheck_done_returns_without_interrupt(self):
        """到点复查发现已完成 → 直接取回，绝不取消（不误杀「刚好差一点」的任务）。"""
        clock = _Clock()
        pid = "p1"
        cli = self._make_client(clock, pid, done_at=11.0)  # deadline=10 之后才完成
        with contextlib.ExitStack() as stack:
            for p in self._patches(clock):
                stack.enter_context(p)
            result = cli.wait_for_completion(pid, timeout=10)
        self.assertEqual(result, _success_entry())
        self.assertEqual(cli.interrupts, [], "到点已完成的任务绝不能被 interrupt")

    def test_recheck_running_extends_then_returns(self):
        """到点复查仍在跑 → 续等 → 最终完成（deadline 被推后，不取消）。"""
        clock = _Clock()
        pid = "p1"
        cli = self._make_client(clock, pid, done_at=20.0)  # 远超 deadline=10
        self.addCleanup(_env_ext(extensions="3", extend_sec="5"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(clock):
                stack.enter_context(p)
            result = cli.wait_for_completion(pid, timeout=10)
        self.assertEqual(result, _success_entry())
        self.assertEqual(cli.interrupts, [], "续等期间绝不能被 interrupt")

    def test_extend_exhausted_then_interrupt(self):
        """续等用尽 → 真超时：interrupt 并返回空（不无限等）。"""
        clock = _Clock()
        pid = "p1"
        cli = self._make_client(clock, pid, done_at=999.0)  # 永不完成
        self.addCleanup(_env_ext(extensions="1", extend_sec="5"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(clock):
                stack.enter_context(p)
            result = cli.wait_for_completion(pid, timeout=10)
        self.assertEqual(result, {})
        self.assertEqual(cli.interrupts, [pid], "真超时后必须清理远端")


class _QueueResp:
    """mock upscale_client 里的 requests.get 返回值。"""

    def __init__(self, queue_running, queue_pending=None):
        self._q = {"queue_running": queue_running, "queue_pending": queue_pending or []}

    def json(self):
        return self._q


class TestUpscaleSoftTimeout(unittest.TestCase):
    """t22：upscale_client._wait 的软超时续等控制流（与 H3 同名同语义）。"""

    def _make_upscaler(self, clock, pid, done_at):
        entry = _success_entry()
        fake = cc.ComfyUIClient(base_url="http://127.0.0.1:9")
        fake.interrupts = []

        def get_history(p):
            return {p: entry} if clock.now >= done_at else {}

        def _get(path, *a, **k):
            if path == "/queue":
                alive = clock.now < done_at
                return {"queue_running": [[0, pid, {}, {}, {}]] if alive else [],
                        "queue_pending": []}
            return {}

        def interrupt(p=None):
            fake.interrupts.append(p)

        fake.get_history = get_history
        fake._get = _get
        fake.interrupt = interrupt

        up = object.__new__(uc.VideoUpscaler)
        up.client = fake
        up.base_url = "http://127.0.0.1:9"
        return up, fake

    def _patches(self, clock, pid):
        return [
            mock.patch.object(uc.time, "time", clock.time),
            mock.patch.object(uc.cancellation, "sleep", clock.sleep),
            mock.patch.object(uc.cancellation, "should_stop", return_value=False),
            mock.patch.object(uc.requests, "get",
                              return_value=_QueueResp([[0, pid, {}, {}, {}]])),
        ]

    def test_slow_upscale_not_killed(self):
        """慢超分不再被误杀：到点复查 → 仍在跑 → 续等 → 取回结果。"""
        clock = _Clock()
        pid = "up-pid"
        up, fake = self._make_upscaler(clock, pid, done_at=11.0)
        self.addCleanup(_env_ext(extensions="3", extend_sec="5"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(clock, pid):
                stack.enter_context(p)
            entry = up._wait(pid, 1, lambda n, p: None, clock.time())  # 1s 就"超时"，11s 才完成
        self.assertEqual((entry.get("status") or {}).get("status_str"), "success")
        self.assertEqual(fake.interrupts, [], "续等期间绝不能被 interrupt")

    def test_extend_exhausted_raises_upscale_error(self):
        """续等用尽 → 真超时：interrupt + UpscaleError（报错带续等次数与可调 env）。"""
        clock = _Clock()
        pid = "up-pid"
        up, fake = self._make_upscaler(clock, pid, done_at=999.0)
        self.addCleanup(_env_ext(extensions="1", extend_sec="5"))
        with contextlib.ExitStack() as stack:
            for p in self._patches(clock, pid):
                stack.enter_context(p)
            with self.assertRaises(uc.UpscaleError) as cm:
                up._wait(pid, 1, lambda n, p: None, clock.time())
        self.assertIn("已续等", str(cm.exception))
        self.assertIn("MJSCXT_UPSCALE_TIMEOUT", str(cm.exception))
        self.assertEqual(fake.interrupts, [pid], "真超时后必须清理远端")


if __name__ == "__main__":
    unittest.main()
