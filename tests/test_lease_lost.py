# -*- coding: utf-8 -*-
"""租约丢失（lease.lost）必须被流水线消费 —— t1 P0 回归（2026-10-12，测试层新增）。

背景（t4 验证门发现的 P0 遗漏项）：
    app/task_lease.py 会在心跳发现「租约易主 / 被回收」时置 Lease.lost，但
    app/pipeline.py 对它 **零引用** —— worker 崩溃或租约超时后，任务既不标失败、
    也不重试、更无告警。用户看到的就是「进度条永远停在那里，已 36 分钟未推进」。

本测试钉死修好之后的行为契约（任何一环被回退，这里都会红）：
    ① pipeline 必须引用并消费 lease.lost（LeaseLostError + _check_lease_alive +
       episode_lease_state 三者缺一不可）；
    ② 执行中途租约丢失：结果 status="lease_lost" / lease_lost=True / retryable=True，
       落 ERROR 级告警日志 + 集级日志事件 lease_lost/lease_recovered；
    ③ **不落死信**（可重跑续跑，不是「需人工介入」），且重跑能重新拿到锁；
    ④ episode_lease_state() 给出前端可读的「已失联」信号（state/lost/reason/signal）。

全程在临时目录内，不碰真实 output/、不发网络请求、不跑 ffmpeg。
"""
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import config              # noqa: E402
import task_lease          # noqa: E402
import paths               # noqa: E402
import pipeline            # noqa: E402


class LeaseLostBase(unittest.TestCase):
    """公共夹具：临时 output 目录 + 宿主 stub + 租约表清场。"""

    PROJECT = "t1_lease_probe"
    EP = 1

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="t1_lease_")
        # 1) 租约目录落临时目录（task_lease.lease_dir 惰性读 config.PROJECT_OUTPUT_DIR）
        self._orig_cfg_out = config.PROJECT_OUTPUT_DIR
        config.PROJECT_OUTPUT_DIR = self.td
        # 2) 宿主 stub：pipeline._A() 从 sys.modules["app"] 取宿主
        self._prev_app = sys.modules.get("app")
        stub = types.ModuleType("app")
        stub.PROJECT_OUTPUT_DIR = self.td
        stub.SCENE_GRID_MODE = False
        stub.analytics = types.SimpleNamespace(record_event=lambda *a, **k: None)
        sys.modules["app"] = stub
        # 3) 集级日志落临时目录（paths.autopilot_dir 是 import 期常量快照，须显式替换）
        self._orig_ap_dir = paths.autopilot_dir
        self.log_root = os.path.join(self.td, "episodes_log")
        os.makedirs(self.log_root, exist_ok=True)
        paths.autopilot_dir = lambda project="": self.log_root
        # 4) 进程内租约表清场（避免用例间串味）
        with pipeline._EP_LOCKS_GUARD:
            pipeline._EP_LEASES.clear()
        # 5) 步骤执行器 / 依赖替换（默认原样，子类按需覆盖）
        self._orig_runners = pipeline.STEP_RUNNERS
        self._orig_load_script = pipeline._load_script_into_ctx
        self._orig_prefetch = pipeline._prefetch_next_scripts

    def tearDown(self):
        pipeline.STEP_RUNNERS = self._orig_runners
        pipeline._load_script_into_ctx = self._orig_load_script
        pipeline._prefetch_next_scripts = self._orig_prefetch
        with pipeline._EP_LOCKS_GUARD:
            for lk in list(pipeline._EP_LEASES.values()):
                try:
                    lk.stop_heartbeat()
                except Exception:                                # noqa: BLE001
                    pass
            pipeline._EP_LEASES.clear()
        paths.autopilot_dir = self._orig_ap_dir
        config.PROJECT_OUTPUT_DIR = self._orig_cfg_out
        if self._prev_app is None:
            sys.modules.pop("app", None)
        else:
            sys.modules["app"] = self._prev_app
        shutil.rmtree(self.td, ignore_errors=True)

    # ---------------- 夹具工具 ----------------
    def _cfg(self, **over):
        raw = {"novel_id": "t1", "style": "t1 测试风格",
               "enable_assets": False, "enable_video": False, "enable_final": False,
               "enable_tts_pre": False, "enable_upscale": False}
        raw.update(over)
        return pipeline.normalize_config(raw, default_project_key=self.PROJECT)

    def _install_fake_runners(self, script_runner=None):
        """把 7 个步骤全部替换为可预测的假执行器（不碰 LLM / GPU / 文件产物）。"""
        def _skip(ctx):
            return {"ok": True, "skipped": True, "artifact": "", "detail": {}}
        runners = {s: _skip for s in pipeline.STEP_SEQUENCE}
        if script_runner is not None:
            runners["script"] = script_runner
        pipeline.STEP_RUNNERS = runners
        pipeline._load_script_into_ctx = lambda ctx: {}
        pipeline._prefetch_next_scripts = lambda ctx, limit=1: None

    def _events(self):
        p = pipeline._episode_log_path(self.PROJECT, self.EP)
        if not os.path.isfile(p):
            return []
        out = []
        with io.open(p, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        return out

    def _event_names(self):
        return [e.get("event") for e in self._events()]

    def _lease_path(self):
        return task_lease._lease_path(pipeline._episode_scope(self.PROJECT, self.EP))

    def _write_lease(self, token, *, pid=None, heartbeat_at=None, owner="other"):
        """直接落一份租约文件（模拟「另一个执行体持有 / 已崩溃留下的租约」）。"""
        rec = {"scope": pipeline._episode_scope(self.PROJECT, self.EP),
               "token": token, "owner": owner,
               "pid": int(pid if pid is not None else os.getpid()),
               "host": socket.gethostname(),
               "acquired_at": time.time(),
               "heartbeat_at": float(heartbeat_at if heartbeat_at is not None else time.time()),
               "ttl_sec": task_lease.DEFAULT_TTL_SEC}
        p = self._lease_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        return rec

    def _read_lease_file(self):
        with io.open(self._lease_path(), encoding="utf-8") as fh:
            return json.load(fh)


class TestPipelineConsumesLeaseLost(LeaseLostBase):

    # ---------------- ① 源引用（grep 证据的测试化） ----------------
    def test_pipeline_source_references_lease_lost(self):
        """pipeline 必须真的引用 lease.lost —— 从「零引用」变为「有引用」。"""
        with io.open(os.path.join(_APP, "pipeline.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertTrue("lease.lost" in src or "lease_lost" in src,
                        "pipeline.py 必须引用 lease.lost（此前为零引用）")
        self.assertIn("LeaseLostError", src)
        self.assertTrue(hasattr(pipeline, "LeaseLostError"))
        self.assertTrue(hasattr(pipeline, "_check_lease_alive"))
        self.assertTrue(hasattr(pipeline, "episode_lease_state"))
        self.assertTrue(hasattr(pipeline, "reclaim_lost_lease"))

    # ---------------- ②+③ 中途失联：失败 + 告警 + 不落死信 + 可重跑 ----------------
    def test_lease_lost_midrun_fails_task_with_alert_and_is_retryable(self):
        """模拟「第一步跑到一半，租约被别的执行体回收/抢占」。

        断言：任务被标记失败（status=lease_lost）且可重试；有 ERROR 告警日志；
        集级日志有 lease_lost 事件；**没有**落死信；重跑能重新拿到锁并记
        lease_recovered。
        """
        def _stealing_script(ctx):
            """第一步「正常完成」，但期间另一个执行体把租约抢走了（token 易主）。"""
            lease = pipeline._episode_lease(self.PROJECT, self.EP)
            self.assertIsNotNone(lease, "run_episode 应先真实持有文件租约")
            # 等价于 acquire 的过期回收 + 重建：token 变了、属于另一个 pid
            self._write_lease("stolen-by-other-executor", pid=999999,
                              heartbeat_at=time.time() - 4000, owner="other-executor")
            self.assertFalse(lease.heartbeat(), "心跳应立刻发现易主并返回 False")
            return {"ok": True, "artifact": "", "detail": {}}

        self._install_fake_runners(script_runner=_stealing_script)
        phases = []

        def _cb(message, percent, phase=None):
            phases.append((message, percent, phase))

        with self.assertLogs("pipeline", level="ERROR") as cap:
            r1 = pipeline.run_episode(self._cfg(), self.PROJECT, self.EP, {},
                                      {"title": "t1 测试"}, progress_cb=_cb)

        # —— 任务被标记失败、且明确可重试 ——
        self.assertEqual(r1.get("status"), "lease_lost")
        self.assertFalse(r1.get("ok"))
        self.assertTrue(r1.get("lease_lost"))
        self.assertTrue(r1.get("retryable"))
        self.assertIn("失联", str(r1.get("error") or ""))

        # —— 告警日志（实测输出：ERROR + 原因）——
        alerts = [m for m in cap.output if "失联" in m]
        self.assertTrue(alerts, "租约丢失必须留下 ERROR 级告警日志；实际：%r" % cap.output)

        # —— 可观测信号：结果快照 + 集级日志事件 ——
        self.assertIn("lease_state", r1)
        self.assertTrue(r1["lease_state"].get("lost"))
        self.assertIn(r1["lease_state"].get("state"), ("lost", "stale"))
        self.assertIn("lease_lost", self._event_names())
        _lost_evt = [e for e in self._events() if e.get("event") == "lease_lost"][0]
        self.assertEqual(_lost_evt.get("level"), "error")
        self.assertEqual(_lost_evt.get("status"), "lease_lost")
        # 前端「当前在做什么」被改成「已失联」（progress_cb 契约）
        self.assertIn("lease_lost", [p[2] for p in phases])

        # —— 不落死信（可重跑，不是需人工介入）——
        self.assertEqual(pipeline.list_dead_letters(self.PROJECT), [])
        self.assertFalse(pipeline.is_episode_running(self.PROJECT, self.EP))

        # —— 重跑实测：能重新拿到锁（不是 busy），并记 lease_recovered ——
        self._install_fake_runners()
        r2 = pipeline.run_episode(self._cfg(), self.PROJECT, self.EP, {},
                                  {"title": "t1 测试"}, progress_cb=lambda *a, **k: None)
        self.assertNotEqual(r2.get("status"), "busy", "失联后应可重跑，而不是被死锁住")
        self.assertFalse(r2.get("lease_lost"))
        self.assertIn("lease_recovered", self._event_names(),
                      "接管重跑时必须告警「上一次执行体已失联」")
        self.assertTrue(r2.get("previous_lease_lost"),
                        "重跑结果应带上「上一棒失联」的证据（previous_lease_lost）")

    # ---------------- 巡检入口：一次调用拿到「已失联」的集 ----------------
    def test_list_lost_leases_exposes_disconnected_episodes(self):
        """前端 / 运维巡检：list_lost_leases 必须报出失联的集，且按项目过滤。"""
        self.assertEqual(pipeline.list_lost_leases(self.PROJECT), [])

        # ① worker 崩溃残留的过期租约（没人回收也要如实报「已失联」）
        self._write_lease("dead-token", pid=999999, heartbeat_at=time.time() - 4000)
        items = pipeline.list_lost_leases(self.PROJECT)
        self.assertEqual(len(items), 1, "过期租约必须出现在失联清单里")
        self.assertTrue(items[0]["lost"])
        self.assertEqual(items[0]["state"], "stale")
        self.assertEqual(items[0]["signal"], "task_lease")
        self.assertTrue(items[0]["retryable"])
        self.assertEqual(pipeline.list_lost_leases("另一个项目"), [])

        # ② 租约已被回收/删除、只剩失联留痕时也要报出来
        os.remove(self._lease_path())
        task_lease.mark_lost(pipeline._episode_scope(self.PROJECT, self.EP),
                             "stale_reclaimed", {"previous_pid": 999999})
        items = pipeline.list_lost_leases(self.PROJECT)
        self.assertEqual(len(items), 1, "只有失联留痕时也必须报出来")
        self.assertTrue(items[0]["lost"])
        self.assertIn("失联", items[0]["reason"])

    # ---------------- ④ 前端可读的「已失联」信号 ----------------
    def test_episode_lease_state_reports_lost_signal(self):
        """state/lost/reason/signal 四件套：前端据此显示「已失联」而不是「还在跑」。"""
        # free
        st = pipeline.episode_lease_state(self.PROJECT, self.EP)
        self.assertEqual(st["state"], "free")
        self.assertFalse(st["lost"])
        self.assertEqual(st["signal"], "task_lease")
        self.assertTrue(st["retryable"])

        # held（本进程真实持锁）
        self.assertTrue(pipeline._acquire_episode_lock(self.PROJECT, self.EP))
        try:
            st = pipeline.episode_lease_state(self.PROJECT, self.EP)
            self.assertEqual(st["state"], "held")
            self.assertFalse(st["lost"])
            self.assertEqual(st["pid"], os.getpid())
            self.assertGreaterEqual(int(st["ttl_sec"]), 1)

            # lost：租约易主 —— 硬检查必须抛 LeaseLostError，状态切到 lost
            lease = pipeline._episode_lease(self.PROJECT, self.EP)
            self._write_lease("other-token", pid=999999,
                              heartbeat_at=time.time() - 4000, owner="other-executor")
            with self.assertRaises(pipeline.LeaseLostError):
                pipeline._check_lease_alive(self.PROJECT, self.EP)
            st = pipeline.episode_lease_state(self.PROJECT, self.EP)
            self.assertEqual(st["state"], "lost")
            self.assertTrue(st["lost"])
            self.assertTrue(st["retryable"])
            self.assertTrue(str(st["reason"]))
        finally:
            pipeline._release_episode_lock(self.PROJECT, self.EP)

        # stale：上一个执行体崩溃留下的过期租约（没人回收时也要如实报「已失联」）
        self._write_lease("dead-token", pid=999999, heartbeat_at=time.time() - 4000)
        st = pipeline.episode_lease_state(self.PROJECT, self.EP)
        self.assertEqual(st["state"], "stale")
        self.assertTrue(st["lost"])
        self.assertIn("失联", st["reason"])

    # ---------------- 租约层：回收留痕 + 取走即清 ----------------
    def test_task_lease_reclaim_leaves_lost_marker(self):
        """回收过期租约必须留痕；留痕被下一个执行体取走即清（一次性告警）。"""
        scope = pipeline._episode_scope(self.PROJECT, self.EP)
        self.assertEqual(task_lease.read_lost_marker(scope), {})
        self._write_lease("dead-token", pid=999999, heartbeat_at=time.time() - 4000)

        lk = task_lease.acquire(scope, owner="tester")
        self.assertIsNotNone(lk, "过期租约应可被回收后重新获得")
        try:
            marker = task_lease.read_lost_marker(scope)
            self.assertEqual(marker.get("reason"), "stale_reclaimed")
            self.assertEqual(marker.get("kind"), "lost_marker")
            self.assertEqual(marker.get("scope"), scope)
            # 留痕不是活跃租约：list_leases 不应把它当成一把锁列出来
            self.assertNotIn("lost_marker",
                             [r.get("kind") for r in task_lease.list_leases()])
            # 取走即清
            got = task_lease.consume_lost_marker(scope)
            self.assertEqual(got.get("reason"), "stale_reclaimed")
            self.assertEqual(task_lease.read_lost_marker(scope), {})
            self.assertEqual(task_lease.consume_lost_marker(scope), {})
        finally:
            lk.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
