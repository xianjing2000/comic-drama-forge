# -*- coding: utf-8 -*-
"""t10 回归：H3 单次提交规模「分批」—— 批级 QC 作用域 + cache_name 唯一性（2026-10-13）

缺陷与契约（与代码逐条对应）
--------------------------------
A. **批级 QC 作用域（P0，开分批的前置条件）**
   video_helpers 的批循环此前**只在每个场开头**设置一次 _qc_scope["segs"] = _ssegs（整场段），
   而循环内每次提交只渲染**一批**；_seg_qc_fn 用 _qc_scope 算 expected_duration / 抽帧占比 /
   逐段时间区间 / 镜头摘要 / 教训库段文本 → 第 1 批实测约 25s 被拿去比整场期望约 146s
   → 偏差 83% > 阈值 30% → qc_client._video_objective_issues 判 duration_drift **fatal**
   → 每批都换种子重试（白烧 GPU）。故必须把作用域收窄到**本批**，且调用后复位为整场。

B. **默认启用分批**：config.H3_BATCH_SEGMENTS = 5（env MJSCXT_H3_BATCH_SEGMENTS 可覆盖，
   显式 0 = 关闭分批 → 走 else 分支的整场单次提交，行为与改动前一致）。

C. **cache_name 红线**：每批一个唯一 cache_name，截断到 64 字符后仍不撞名，
   且与「整场（batch=None）」的名字不同（不与旧缓存串用）。

本测试**不联网、不依赖 GPU / ComfyUI**：QC 判据用真实 qc_client 函数（音轨探测打桩）。
运行：python -W ignore::ResourceWarning -m unittest discover -s tests -p "test_*.py"
"""
import ast
import io
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ.setdefault('MJSCXT_DATA_DIR', tempfile.mkdtemp(prefix='t10test_'))

import config          # noqa: E402
import qc_client       # noqa: E402
import video_helpers   # noqa: E402
from comfyui_client import h3_cache_name, H3_CACHE_NAME_MAX_LEN   # noqa: E402

_VH_PATH = os.path.join(_APP, 'video_helpers.py')
_VH_SRC = io.open(_VH_PATH, encoding='utf-8').read()
_VH_TREE = ast.parse(_VH_SRC)


def _find_batch_loop():
    """定位批循环 for _bi, _bseg in enumerate(_batches): 的 AST 节点。"""
    for node in ast.walk(_VH_TREE):
        if not isinstance(node, ast.For):
            continue
        tgt = node.target
        if (isinstance(tgt, ast.Tuple) and len(tgt.elts) == 2
                and isinstance(tgt.elts[0], ast.Name) and tgt.elts[0].id == '_bi'
                and isinstance(tgt.elts[1], ast.Name) and tgt.elts[1].id == '_bseg'):
            return node
    return None


def _scope_assigns(body):
    """body 里所有 _qc_scope["<key>"] = <value> 赋值 → [(key, value_node, lineno)]。"""
    out = []
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                and t.value.id == '_qc_scope'):
            try:
                key = ast.literal_eval(t.slice)
            except (ValueError, SyntaxError):
                key = None
            out.append((key, node.value, node.lineno))
    return out


def _load_batch_shots():
    """用 AST 抽取闭包内的 _batch_shots 并 exec（只依赖 _shot_seq 与 shots）。"""
    for node in ast.walk(_VH_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == '_batch_shots':
            ns = {'_shot_seq': video_helpers._shot_seq}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         '<_batch_shots>', 'exec'), ns)
            return ns['_batch_shots']
    return None


class BatchQcScopeSourceTests(unittest.TestCase):
    """A 组：批循环必须把 QC 作用域收窄到本批，并在调用后复位（源码级，防接线漏改）。"""

    def test_batch_loop_exists(self):
        loop = _find_batch_loop()
        self.assertIsNotNone(loop, '没有找到 for _bi, _bseg in enumerate(_batches) 批循环')

    def test_scope_set_to_batch_before_submit_and_restored_after(self):
        loop = _find_batch_loop()
        self.assertIsNotNone(loop)
        assigns = _scope_assigns(loop.body)

        # 提交调用（comfyui_client.generate_h3_sequence_sequential）的行号
        submit_lines = []
        for node in ast.walk(ast.Module(body=list(loop.body), type_ignores=[])):
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Attribute)
                        and f.attr == 'generate_h3_sequence_sequential'):
                    submit_lines.append(node.lineno)
        self.assertTrue(submit_lines, '批循环里没有找到 generate_h3_sequence_sequential 调用')
        _sub = min(submit_lines)

        # 1) 提交**之前**：_qc_scope["segs"] = _bseg（本批）
        before_segs = [(v, ln) for k, v, ln in assigns
                       if k == 'segs' and isinstance(v, ast.Name) and v.id == '_bseg']
        self.assertTrue(before_segs,
                        '批循环内没有 _qc_scope["segs"] = _bseg —— 这正是本任务要修的 P0 缺陷')
        self.assertTrue(all(ln < _sub for _, ln in before_segs),
                        '批级 _qc_scope["segs"] = _bseg 必须出现在提交调用**之前**')

        # 2) 提交**之前**：_qc_scope["shots"] = _batch_shots(_bseg)（镜头摘要同源收窄）；
        #    提交**之后**：复位为 None（不吃上一批的镜头）
        _is_none = lambda v: isinstance(v, ast.Constant) and v.value is None
        before_shots = [(v, ln) for k, v, ln in assigns if k == 'shots' and not _is_none(v)]
        after_shots = [(v, ln) for k, v, ln in assigns if k == 'shots' and _is_none(v)]
        self.assertTrue(before_shots, '批循环内没有设置 _qc_scope["shots"]（镜头摘要未按批收窄）')
        self.assertTrue(all(ln < _sub for _, ln in before_shots),
                        '_qc_scope["shots"] 必须在提交调用之前设置')
        self.assertTrue(after_shots and all(ln > _sub for _, ln in after_shots),
                        '_qc_scope["shots"] 必须在提交调用之后复位为 None')

        # 3) 提交**之后**：复位为整场 _qc_scope["segs"] = _ssegs
        after_segs = [(v, ln) for k, v, ln in assigns
                      if k == 'segs' and isinstance(v, ast.Name) and v.id == '_ssegs']
        self.assertTrue(after_segs, '批循环内没有把 _qc_scope["segs"] 复位为整场 _ssegs')
        self.assertTrue(all(ln > _sub for _, ln in after_segs),
                        '整场复位必须出现在提交调用**之后**')

    def test_scope_reset_at_scene_start(self):
        """场开头必须把 segs 设为整场、shots 清空（批内异常退出不残留到下一场）。"""
        hits = [(k, v, ln) for k, v, ln in _scope_assigns(_VH_TREE.body)
                if k in ('segs', 'shots')]
        segs_scene = [(v, ln) for k, v, ln in hits
                      if k == 'segs' and isinstance(v, ast.Name) and v.id == '_ssegs']
        shots_none = [(v, ln) for k, v, ln in hits
                      if k == 'shots' and isinstance(v, ast.Constant) and v.value is None]
        self.assertTrue(segs_scene, '缺少场级 _qc_scope["segs"] = _ssegs')
        self.assertTrue(shots_none, '缺少场级 _qc_scope["shots"] = None（复位）')

    def test_qc_fn_reads_scope_not_whole_episode(self):
        """_seg_qc_fn 的 expected_duration / 抽帧占比 / 镜头摘要都必须读作用域。"""
        src = _VH_SRC
        self.assertIn('_qc_segs = _qc_scope.get("segs") or segs', src,
                      'expected_duration/抽帧占比的来源不再是 _qc_scope（口径又按整集算了？）')
        self.assertIn('for _es in (_qc_scope.get("segs") or []):', src,
                      'expected_duration 的累加来源必须读 _qc_scope["segs"]')
        self.assertIn('_qc_scope.get("shots") or shots', src,
                      '镜头摘要（_episode_qc_desc）必须读 _qc_scope["shots"]')

    def test_zero_disables_batching_keeps_whole_scene_path(self):
        """关闭分批（<=0）时走 else 分支的整场单次提交 —— 旧行为逐位保留。"""
        self.assertIn('if _batch_segs > 0 and len(_ssegs) > _batch_segs:', _VH_SRC,
                      '分批开关的判据被改写（关闭语义可能已变）')
        self.assertIn('segments=_ssegs,', _VH_SRC,
                      'else 分支的整场提交（segments=_ssegs）不见了')
        self.assertIn('cache_name=_cn_scene,', _VH_SRC,
                      'else 分支的整场 cache_name（_cn_scene）不见了')


class BatchQcExpectedDurationTests(unittest.TestCase):
    """A 组（行为）：批级 expected_duration 不会被真实 QC 闸门判 fatal。"""

    @staticmethod
    def _issues(duration, expected):
        fr = {'meta': {'duration': float(duration), 'fps': 24.0}}
        with mock.patch.object(qc_client, '_has_audio_stream', return_value=True):
            return qc_client._video_objective_issues(
                'x.mp4', float(expected), {'video_max_drift': 0.30}, fr)

    @staticmethod
    def _fatal_codes(items):
        return [it['code'] for it in items if it.get('fatal')]

    def test_defect_repro_whole_scene_scope_is_fatal(self):
        """防空转：按**整场**期望去比**单批**产物 → duration_drift fatal（缺陷机理）。"""
        batch_s, scene_s = 25.0, 146.0
        fatal = self._fatal_codes(self._issues(batch_s, scene_s))
        self.assertIn('duration_drift', fatal,
                      '整场作用域下本应被判 fatal —— 复现失败说明本测试没测到真机理')

    def test_batch_scope_is_not_fatal(self):
        """按**本批**期望去比本批产物 → 不判 fatal（修复后的口径）。"""
        segs_scene = [{'duration': 30.0}, {'duration': 30.0}, {'duration': 30.0},
                      {'duration': 30.0}, {'duration': 26.0}]      # 整场 5 段 146s
        batches = [segs_scene[:2], segs_scene[2:5]]               # 本批 60s / 86s
        for i, bseg in enumerate(batches):
            expected = sum(float(s['duration']) for s in bseg)
            measured = expected + 0.4                              # 真实产物允许微小误差
            fatal = self._fatal_codes(self._issues(measured, expected))
            self.assertNotIn('duration_drift', fatal,
                             '第 %d 批按本批期望仍被判 fatal（作用域没收窄）' % (i + 1))

    def test_scope_switch_changes_verdict(self):
        """同一段产物：整场口径 fatal、本批口径不 fatal —— 直接证明作用域就是判据来源。"""
        measured, batch_s, scene_s = 60.0, 60.0, 146.0
        self.assertIn('duration_drift', self._fatal_codes(self._issues(measured, scene_s)))
        self.assertNotIn('duration_drift', self._fatal_codes(self._issues(measured, batch_s)))


class BatchSizeConfigTests(unittest.TestCase):
    """B 组：默认 5 段/批 + env 覆盖（含显式 0 = 关闭）。"""

    def test_default_is_five(self):
        self.assertEqual(int(config.H3_BATCH_SEGMENTS), 5,
                         'H3_BATCH_SEGMENTS 默认值不是 5（用户授权的切小提交规模未生效）')

    def test_const_declared_with_env_default_five(self):
        src = io.open(os.path.join(_APP, 'config.py'), encoding='utf-8').read()
        self.assertIn('H3_BATCH_SEGMENTS = max(0, _env_int("MJSCXT_H3_BATCH_SEGMENTS", 5))',
                      src, 'config 里的 H3_BATCH_SEGMENTS 声明被改写（env 覆盖/下限语义变化）')

    def test_video_helpers_uses_config_default_and_env_priority(self):
        self.assertIn('_batch_segs = int(H3_BATCH_SEGMENTS)', _VH_SRC,
                      '批大小没有取 config 默认值')
        self.assertIn('os.environ.get("MJSCXT_H3_BATCH_SEGMENTS")', _VH_SRC,
                      'env 覆盖入口不见了')

    def test_env_override_in_subprocess(self):
        """子进程验证：env 缺省=5 / 显式 0=0 / 显式 12=12（隔离，不污染本进程）。"""
        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "import config; print(int(config.H3_BATCH_SEGMENTS))" % _APP)
        cases = [('', 5), ('0', 0), ('12', 12)]
        for env_val, want in cases:
            env = dict(os.environ)
            env['MJSCXT_DATA_DIR'] = tempfile.mkdtemp(prefix='t10sub_')
            env.pop('MJSCXT_H3_BATCH_SEGMENTS', None)
            if env_val != '':
                env['MJSCXT_H3_BATCH_SEGMENTS'] = env_val
            p = subprocess.run([sys.executable, '-W', 'ignore::ResourceWarning', '-c', code],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', env=env, timeout=300)
            self.assertEqual(p.returncode, 0,
                             '子进程导入 config 失败：%s' % (p.stderr or '')[-400:])
            got = int((p.stdout or '').strip().splitlines()[-1])
            self.assertEqual(got, want,
                             'env MJSCXT_H3_BATCH_SEGMENTS=%r 时得到 %d，期望 %d'
                             % (env_val, got, want))


class BatchCacheNameTests(unittest.TestCase):
    """C 组：每批 cache_name 唯一，且 64 字符截断后不撞名、不与整场串用。"""

    def test_each_batch_unique_within_scene(self):
        names = [h3_cache_name('逆天系统_全书', 1, 3, batch=i) for i in range(12)]
        self.assertEqual(len(set(names)), 12, '同场各批 cache_name 有重复')

    def test_truncated_names_still_unique(self):
        names = [h3_cache_name('逆天系统_全书', 1, 3, batch=i)[:H3_CACHE_NAME_MAX_LEN]
                 for i in range(12)]
        self.assertEqual(len(set(names)), 12, '截断到 64 字符后各批 cache_name 撞名')

    def test_truncated_unique_with_long_project_name(self):
        proj = '超长项目名' * 60          # 300 字符，远超 64
        names = [h3_cache_name(proj, 2, 7, batch=i)[:H3_CACHE_NAME_MAX_LEN]
                 for i in range(12)]
        self.assertEqual(len(set(names)), 12,
                         '长项目名下截断后各批 cache_name 撞名（缓存会互相覆盖）')

    def test_batch_name_differs_from_whole_scene_name(self):
        whole = h3_cache_name('proj', 1, 3)
        for i in range(3):
            self.assertNotEqual(whole, h3_cache_name('proj', 1, 3, batch=i),
                                '批名与整场名相同 → 会与整场缓存目录串用')

    def test_cross_scene_truncated_unique(self):
        for batch in (None, 0, 1):
            names = [h3_cache_name('proj', 1, sn, batch=batch)[:H3_CACHE_NAME_MAX_LEN]
                     for sn in range(1, 21)]
            self.assertEqual(len(set(names)), 20,
                             'batch=%r 时跨场次 cache_name 截断后撞名' % (batch,))


class BatchShotsHelperTests(unittest.TestCase):
    """D 组：_batch_shots 的映射口径（本批段名 → 所属镜头）。"""

    def setUp(self):
        self.fn = _load_batch_shots()
        self.assertIsNotNone(self.fn, '没有找到 _batch_shots 辅助函数')
        self.shots = [{'shot_id': i + 1} for i in range(6)]
        self.fn.__globals__['shots'] = self.shots

    def test_maps_batch_segments_to_shots(self):
        out = self.fn([{'name': 'shot_01'}, {'name': 'shot_02'}])
        self.assertEqual([s['shot_id'] for s in out], [1, 2])

    def test_long_shot_suffix_still_maps(self):
        """长镜切段的后缀（shot_07a / shot_07b）归一到 shot_07。"""
        self.fn.__globals__['shots'] = [{'shot_id': i + 1} for i in range(7)]
        out = self.fn([{'name': 'shot_07a'}, {'name': 'shot_07b'}])
        self.assertEqual([s['shot_id'] for s in out], [7])

    def test_unmappable_returns_empty(self):
        self.assertEqual(self.fn([{'name': 'unknown'}, {}]), [],
                         '解析不出镜头时必须返回 []（回落全集口径，不得误报 0 镜）')
        self.assertEqual(self.fn([]), [])


class BatchLoopRuntimeTests(unittest.TestCase):
    """A 组（端到端仿真）：真跑批循环，验证每次提交的 QC 作用域=本批、结束后复位。

    做法：用 AST 抽出真实的批循环体（for _bi, _bseg in enumerate(_batches)），在受控
    命名空间里 exec（只 stub 掉 ComfyUI / 日志 / 锁 / 任务状态），由假客户端在**每次
    提交时**回调真 qc_fn（探针）——探针与批循环共享同一个 _qc_scope 对象，于是能读到
    批循环「当次」的作用域。这既是 acceptance 的行为证据，也是日志样例的来源。
    """

    @staticmethod
    def _fatal(duration, expected):
        fr = {'meta': {'duration': float(duration), 'fps': 24.0}}
        with mock.patch.object(qc_client, '_has_audio_stream', return_value=True):
            items = qc_client._video_objective_issues(
                'x.mp4', float(expected), {'video_max_drift': 0.30}, fr)
        return [it['code'] for it in items if it.get('fatal')]

    def _run(self, durs, batch_size, tail_png=None):
        loop = _find_batch_loop()
        self.assertIsNotNone(loop)
        segs = [{'name': 'shot_%02d' % (i + 1), 'duration': float(d), 'prompt': 'p%d' % i}
                for i, d in enumerate(durs)]
        batches = [segs[i:i + batch_size] for i in range(0, len(segs), batch_size)]
        scope = {'segs': list(segs), 'shots': None}
        seen = []
        calls = []

        def probe(video_path, shot_desc, cfg, style):
            cur = scope.get('segs') or []
            seen.append((len(cur),
                         round(sum(float(s.get('duration') or 0) for s in cur), 3),
                         [s['name'] for s in cur]))
            return {'passed': True, 'verdict': {'ok': True}, 'gate': {'accept': True}}

        tmp = tempfile.mkdtemp(prefix='t10loop_')

        class _Client:
            @staticmethod
            def generate_h3_sequence_sequential(**kw):
                calls.append(dict(kw))
                kw['qc_fn'](os.path.join(tmp, 'x.mp4'), '', {}, '')
                p = os.path.join(tmp, 'out_%d.mp4' % len(calls))
                with open(p, 'wb') as fh:
                    fh.write(b'x')
                return {'files': [p], 'error': ''}

        logs = []

        class _Log:
            @staticmethod
            def _rec(msg, *a, **k):
                try:
                    logs.append(str(msg) % a if a else str(msg))
                except Exception:
                    logs.append(str(msg))

            info = warning = error = debug = _rec

        batch_shots = _load_batch_shots()
        self.assertIsNotNone(batch_shots)
        batch_shots.__globals__['shots'] = [{'shot_id': i + 1} for i in range(len(durs))]

        ns = {
            '_qc_scope': scope,
            '_batch_shots': batch_shots,
            'comfyui_client': _Client,
            'h3_cache_name': lambda *a, **k: 'cn',
            'comfyui_job_store': types.SimpleNamespace(get_or_create_seed=lambda *a, **k: 1),
            '_secrets': types.SimpleNamespace(randbelow=lambda n: 1),
            'logger': _Log(),
            'lock': threading.RLock(),
            'generation_state': {'t': {}},
            'task_id': 't',
            'os': os,
            'qc_on': True,
            '_seg_qc_fn': probe,
            'qc_cfg': {},
            'eff_style': '',
            'max_retries': 1,
            '_ep_qc_stop_cb': None,
            'timeout_per_segment': 900,
            '_size': (960, 544),
            '_common': [],
            '_common_audio_paths': [],
            '_common_prompt': '',
            'episode_tag': 'ep01',
            'project_name': 'proj',
            '_epn': 1,
            '_gi': 0,
            '_scene_groups': [(1, segs)],
            '_sn': 1,
            '_ssegs': segs,
            'videos_dir': tmp,
            # 2026-10-19 批间末帧锚定：批循环新调用的模块级助手必须进命名空间。
            # 默认用真实的抽取函数（假 mp4 内容 → fail-open 返回 ""，不影响既有断言）；
            # 传入 tail_png 时替换为假抽取器，用于验证「第 2 批起首段挂末帧」。
            '_BATCH_TAIL_DIRNAME': video_helpers._BATCH_TAIL_DIRNAME,
            '_extract_batch_tail_frame': (
                (lambda *a, **k: tail_png) if tail_png
                else video_helpers._extract_batch_tail_frame),
            '_anchor_batch_head_to_prev_tail':
                video_helpers._anchor_batch_head_to_prev_tail,
            # for 语句**之前**的初始化（原本在 if 分支体内，本仿真直接提供）
            '_bfiles': [],
            '_berr': '',
        }
        # 直接 exec 真实的 for 语句本身（保留 _bi/_bseg 绑定与 break/continue 语义）
        ns['_batches'] = batches
        wrapper = ast.Module(body=[loop], type_ignores=[])
        ast.fix_missing_locations(wrapper)
        exec(compile(wrapper, '<t10_batch_loop>', 'exec'), ns)
        return {'seen': seen, 'scope': scope, 'segs': segs, 'batches': batches,
                'logs': logs, 'calls': calls}

    def test_scope_is_per_batch_and_restored_after(self):
        r = self._run([30.0, 30.0, 30.0, 30.0, 26.0], 2)      # 整场 146s → 3 批(2/2/1)
        self.assertEqual(len(r['calls']), 3, '5 段按 2 段/批应提交 3 次')
        self.assertEqual([n for n, _, _ in r['seen']], [2, 2, 1],
                         'QC 回调读到的段数不是本批段数')
        self.assertEqual([d for _, d, _ in r['seen']], [60.0, 60.0, 26.0],
                         'QC 期望时长不是本批时长（仍按整场 146s 算？）')
        self.assertEqual(len(r['scope']['segs']), 5,
                         '批循环结束后 _qc_scope[segs] 没有复位为整场')
        self.assertIsNone(r['scope']['shots'], '批循环结束后 _qc_scope[shots] 没有复位')

    def test_no_fatal_with_batch_scope_but_fatal_with_scene_scope(self):
        r = self._run([30.0, 30.0, 30.0, 30.0, 26.0], 2)
        scene_dur = sum(float(s['duration']) for s in r['segs'])
        for _n, dur, _names in r['seen']:
            measured = dur + 0.2                      # 真实产物的微小偏差
            self.assertIn('duration_drift', self._fatal(measured, scene_dur),
                          '整场口径本应 fatal —— 复现失败说明测试没测到真机理')
            self.assertNotIn('duration_drift', self._fatal(measured, dur),
                             '本批口径仍被判 fatal —— 作用域没收窄')

    def test_batch_log_lines_emitted(self):
        r = self._run([30.0, 30.0, 30.0, 30.0, 26.0], 2)
        blob = '\n'.join(r['logs'])
        self.assertIn('QC 作用域=本批 2 镜 / 期望 60.0s，整场 146.0s', blob)
        self.assertIn('QC 作用域=本批 1 镜 / 期望 26.0s，整场 146.0s', blob)
        self.assertIn('第 1/3 批', blob)
        self.assertIn('第 3/3 批', blob)

    def test_single_batch_scope_equals_whole_scene(self):
        """只切出 1 批时（_bseg 就是整场）作用域等于整场 —— 与旧行为一致。"""
        r = self._run([30.0, 30.0, 30.0], 5)
        self.assertEqual(len(r['calls']), 1)
        self.assertEqual(r['seen'], [(3, 90.0, ['shot_01', 'shot_02', 'shot_03'])],
                         '单批场景的 QC 作用域应等于整场（3 段 / 90s）')

    def test_tail_anchor_applied_from_second_batch_only(self):
        """2026-10-19：第 2 批起，首段必须挂上一批末帧；第 1 批不受影响。

        插件「段间引导」只在同一次提交内传递尾帧 → 批间必须由这里补锚点，
        否则第 5 段与第 6 段之间就是无引导硬切（用户实测卡顿）。
        """
        tmp = tempfile.mkdtemp(prefix='t10tail_')
        tail = os.path.join(tmp, 'tail.png')
        with open(tail, 'wb') as fh:
            fh.write(b'\x89PNG\r\n\x1a\n')
        with mock.patch.dict(os.environ, {'MJSCXT_H3_BATCH_TAIL_ANCHOR': '1'}):
            r = self._run([10.0, 10.0, 10.0, 10.0, 10.0], 2, tail_png=tail)
        self.assertEqual(len(r['calls']), 3)
        self.assertNotIn('batch_tail_anchor', r['calls'][0]['segments'][0],
                         '第 1 批不该有上一批末帧（不存在「上一批」）')
        for i, c in enumerate(r['calls'][1:], start=2):
            head = c['segments'][0]
            self.assertEqual(head.get('batch_tail_anchor'), tail,
                             '第 %d 批首段没有挂上一批末帧' % i)
            self.assertEqual(head['reference_images'][-1], tail,
                             '第 %d 批首段 reference_images 末尾不是末帧图' % i)
            self.assertIn('<Picture 1>', head['prompt'],
                          '第 %d 批首段提示词没有声明末帧图（会被构建器静默丢弃）' % i)
        # 共享段 dict（_ssegs / _batches 同一对象）不得被污染
        self.assertNotIn('batch_tail_anchor', r['segs'][2])
        self.assertNotIn('reference_images', r['segs'][2])


if __name__ == '__main__':
    unittest.main()
