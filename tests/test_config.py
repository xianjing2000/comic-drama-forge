# -*- coding: utf-8 -*-
"""配置中心 / 体检 回归测试（2026-10-10）。

覆盖本轮修过的真实缺陷：
  · range 推断错误导致正常值写不进去（NOVEL_DEFAULT_SHOTS 曾被推成 (0,1)）；
  · 别名 key 读写（target_shots → novel_default_shots）；
  · must_match 跨模块同步（改一处两处一起改）；
  · 孤儿检测（可配置但未登记）；
  · REGISTRY 无重复 key。
运行：python -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'app'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: ⭐ t20/D5：数据隔离由 **tests/__init__.py**（两条入口都会先加载）统一负责 ——
#: 它在 import 期把 MJSCXT_DATA_DIR 指向临时目录，并装上「写真实数据根即抛错」的 sqlite3 写闸。
#: 本模块**不再**自己 mkdtemp + 改 env：那种写法表面上「用了临时目录」，实际上
#: 只有在「本模块恰好是第一个 import env_loader 的模块」时才成立；一旦别的模块先 import 了
#: config（全量跑时经常如此），数据根早已定死，本模块的 setenv 变成**空操作**，
#: 于是 CC.set_value(..., note="test-restore") 写进了**开发数据根** output/tasks.db
#: （t23 于 13:31 发现；t20 复核 16:37/16:39 复现）。删除它，而不是继续依赖这个隐含顺序。
import tests as _t20  # noqa: E402

import config_center as CC  # noqa: E402
import config_doctor as CD  # noqa: E402


def setUpModule():
    """⭐ t20/D5：**两条入口**都在这里再钉一次数据根。

    两条入口都走这里（pytest 不从包 __init__ 采集 fixture，已实测）：显式调用（幂等）。
    实测 discover 下 env_loader 会比 tests 包
    **先**被 import（那时 MJSCXT_DATA_DIR 还是空的）→ 数据根=仓库根 → 本模块的
    set_value 会写进开发数据根（正是 t23/D5 报的那条）。
    """
    _t20.ensure_isolated(reason="test_config.setUpModule")
    #: t20/D5 运行后对比：记下**真实数据根**指纹，供 TestRealDataRootUntouched 断言。
    global _REAL_ROOT_SNAPSHOT
    _REAL_ROOT_SNAPSHOT = {p: _t20._fingerprint(p) for p in _t20._real_db_paths()}


#: tests/test_config.py 是**唯一**会写 app_settings 的测试模块（D5 的污染源）。
#: setUpModule 记下真实数据根指纹，本模块跑完再比对。
_REAL_ROOT_SNAPSHOT = None


class TestRealDataRootUntouched(unittest.TestCase):
    """⭐ t20/D5 回归护栏：本模块跑完，真实数据根必须**逐字未变**。

    比对项：存在性 / 大小 / mtime / app_settings 全文。任何一项变了 = 测试又写到真实库。
    """

    def test_real_data_root_untouched(self):
        if _REAL_ROOT_SNAPSHOT is None:
            self.skipTest("本模块未记录快照（单独跑该类时）")
        now = {p: _t20._fingerprint(p) for p in _t20._real_db_paths()}
        dirty = _t20._diff_fingerprints(_REAL_ROOT_SNAPSHOT, now)
        self.assertEqual(dirty, [], "真实数据根被测试改动了（t20/D5）：%s" % dirty)


class TestRegistryShape(unittest.TestCase):
    def test_no_duplicate_literal_keys(self):
        with open(os.path.join(os.path.dirname(CD.__file__), 'config_doctor.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        i = src.index('REGISTRY: Dict[str, Dict[str, Any]] = {')
        j = src.index(chr(10) + '}', i)
        keys = re.findall(r"^    '([a-z0-9_]+)': \{", src[i:j], re.M)
        self.assertEqual(len(keys), len(set(keys)), 'REGISTRY 有重复 key: %s'
                         % [k for k in keys if keys.count(k) > 1])

    def test_every_entry_has_required_fields(self):
        for key, meta in CD.REGISTRY.items():
            for f in ('default', 'type', 'group', 'owner'):
                self.assertIn(f, meta, '%s 缺字段 %s' % (key, f))
            self.assertIn(meta['type'], ('int', 'float', 'bool', 'str'), key)

    def test_owners_resolvable(self):
        """每个 owner / must_match 都必须能逐段解析到真实属性。"""
        bad = []
        for key, meta in CD.REGISTRY.items():
            for dotted in [str(meta.get('owner') or '')] + list(meta.get('must_match') or []):
                parts = dotted.split('.')
                try:
                    obj = __import__(parts[0])
                    for p in parts[1:]:
                        obj = obj[p] if isinstance(obj, dict) else getattr(obj, p)
                except Exception as e:  # noqa: BLE001
                    bad.append('%s (%s)' % (dotted, type(e).__name__))
        self.assertEqual(bad, [], 'owner 解析失败: %s' % bad)

    def test_no_wrong_int_range(self):
        """int 类型不应出现 (0,1) 这种明显错误的范围（曾在 NOVEL_DEFAULT_SHOTS 发生）。"""
        bad = [k for k, m in CD.REGISTRY.items()
               if m.get('type') == 'int' and tuple(m.get('range') or ()) == (0, 1)]
        self.assertEqual(bad, [], 'int 项范围异常: %s' % bad)


class TestAliasAndSync(unittest.TestCase):
    """读写别名/范围/联动 —— **会写 app_settings**，故必须先证明数据根已隔离。"""

    def setUp(self):
        #: t20/D5 断言：本用例会调 CC.set_value 写库，写之前必须先证明写的是临时根。
        if os.environ.get("MJSCXT_T20_PRINT_ENV") == "1":
            _env = sys.modules.get("env_loader")
            print("DBG-DISCOVER env=%r pdd=%r cfg_db=%r modules=%s"
                  % (os.environ.get("MJSCXT_DATA_DIR"),
                     getattr(_env, "PROJECT_DATA_DIR", None), CC.db_path(),
                     sorted(m for m in sys.modules if m in (
                         "config", "env_loader", "config_center", "ai_credentials_db",
                         "secret_store", "ai_config", "task_store"))))
        rep = _t20.isolation_report()
        self.assertTrue(rep["is_temp"], "数据根未隔离：%s" % rep["data_dir"])
        self.assertTrue(rep["guard_active"], "sqlite3 写闸未装上")
        db = os.path.normcase(os.path.abspath(CC.db_path()))
        self.assertTrue(_t20.allow_data_root(db),
                        "config_center 的库路径 %s 不是临时根 —— 本用例会污染真实数据" % db)
        before = _t20.real_data_roots()
        for bad in before:
            self.assertFalse(db.startswith(bad + os.sep),
                             "库路径落在真实数据根 %s 内" % bad)

    def test_alias_maps_to_canonical(self):
        self.assertEqual(CC.canonical_key('target_shots'), 'novel_default_shots')
        self.assertEqual(CC.canonical_key('novel_default_shots'), 'novel_default_shots')

    def test_alias_read_write(self):
        CC.set_value('target_shots', 12, note='test')
        self.assertEqual(CC.get_value('novel_default_shots'), 12)
        CC.set_value('novel_default_shots', 0, note='test-restore')

    def test_range_rejects_absurd(self):
        r = CC.set_value('novel_default_shots', 999999, note='test')
        self.assertFalse(r.get('ok'))
        self.assertIn('范围', r.get('error', ''))

    def test_unregistered_key_rejected(self):
        r = CC.set_value('definitely_not_a_key', 1)
        self.assertFalse(r.get('ok'))

    def test_must_match_writes_both(self):
        """COVERAGE_MAX_ROUNDS 在 continuity 与 novel_to_script 两处，必须同步。"""
        import continuity
        import novel_to_script
        CC.set_value('coverage_max_rounds', 4, note='test')
        self.assertEqual(continuity.COVERAGE_MAX_ROUNDS, 4)
        self.assertEqual(novel_to_script.COVERAGE_MAX_ROUNDS, 4)
        CC.set_value('coverage_max_rounds', 3, note='test-restore')


class TestDoctor(unittest.TestCase):
    """⭐ t22/T-b：**注入式**配置体检 —— 不再拿「工作区真实配置」当断言。

    原实现的两个不稳定源（都实测过）：
      ① CD.doctor() 读的是 **live 的 app/ 源码目录**（孤儿扫描按
         os.path.dirname(config_doctor.__file__) 列目录）—— 队友正在改 app/ 时,
         扫描结果随他们的编辑瞬间变化 → 间歇红灯, 与测试自身无关；
      ② rep.ok 要求**每个登记项**的运行时值都等于登记默认值 —— 同一进程里
         别的用例（如 TestAudioSilenceLockstep.test_change_one_side_moves_both）
         会临时改 audio_qc.HARD_SILENT_RATIO 等全局常量, 顺序不同就红。

    改法：把「被检查的输入」全部注入 ——
      · 孤儿扫描 → 指向本用例自己写的临时 app 目录（内容固定）；
      · 登记表 → 只放本用例自己的 owner（值确定性 100%）；
      · 提示词目录 → 打桩为「无外部文件」（doctor 走代码兜底, mismatch=0）；
      · 数据根 → tests/__init__.py 的临时根。
    这样 ok 只反映**机制**, 不反映工作区状态。

    产物形态保留两条真实护栏：
      · test_live_owner_resolvability —— live 状态下 owner 必须都能解析、
        值必须等于登记默认（这才是「工作区真实漂移」的报警点）；
      · test_app_orphan_scan_against_controlled_dir —— 机制侧：植入一个未登记常量
        必须被点名, 已登记/实现类常量不许误报。
    """

    def setUp(self):
        rep = _t20.isolation_report()
        self.assertTrue(rep["is_temp"], "数据根未隔离：%s" % rep["data_dir"])
        self._orig_registry = CD.REGISTRY
        self._tmp = __import__("tempfile").mkdtemp(prefix="t20_doc_")
        self.addCleanup(self._restore)

    def _restore(self):
        CD.REGISTRY = self._orig_registry

    # ---------- 注入工具 ---------- #
    def _install_registry(self, entries):
        CD.REGISTRY = dict(entries)

    def _install_scan_dir(self, modules):
        """把孤儿扫描指向临时目录（内容固定, 不随队友改 app/ 波动）。"""
        for name, src in modules.items():
            with open(os.path.join(self._tmp, name + ".py"), "w", encoding="utf-8") as fh:
                fh.write(src)
        real_listdir = os.listdir
        real_open = open
        tmp = self._tmp

        def _listdir(path="."):
            if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(os.path.abspath(tmp)):
                return [n + ".py" for n in sorted(modules)]
            return real_listdir(path)

        def _open(path, *a, **k):
            try:
                p = os.path.normcase(os.path.abspath(str(path)))
            except Exception:  # noqa: BLE001
                return real_open(path, *a, **k)
            if p.startswith(os.path.normcase(os.path.abspath(tmp)) + os.sep):
                return real_open(os.path.join(tmp, os.path.basename(p)), *a, **k)
            return real_open(path, *a, **k)

        #: ⚠️ 关键一步：orphan_candidates 用 os.path.dirname(config_doctor.__file__) 定 base，
        #:   所以只 patch listdir 是**没用**的（base 仍然指 app/，会列出真实源码）。
        #:   必须把「对 config_doctor.__file__ 取 dirname」这一处改成临时目录。
        real_dirname = os.path.dirname
        cd_file = os.path.abspath(CD.__file__)

        def _dirname(path):
            try:
                if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(cd_file):
                    return tmp
            except Exception:  # noqa: BLE001
                pass
            return real_dirname(path)

        patcher_dirname = mock.patch.object(CD.os.path, "dirname", _dirname)
        patcher_listdir = mock.patch.object(CD.os, "listdir", _listdir)
        patcher_open = mock.patch("builtins.open", _open)
        patcher_dirname.start()
        patcher_listdir.start()
        patcher_open.start()
        self.addCleanup(patcher_open.stop)
        self.addCleanup(patcher_listdir.stop)
        self.addCleanup(patcher_dirname.stop)

    def _make_owner(self, values, name="mod_ok"):
        """造一个 owner 模块并注册进 sys.modules（供 CD._resolve 解析）。

        ⚠️ 模块名必须与孤儿扫描里的 dotted 名一致（"<模块>.<常量小写>"），
        否则同名常量会被登记表判成「未登记」而误报 —— 这是本用例踩过的坑。
        """
        mod = type(sys)(name)
        for k, v in values.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        self.addCleanup(lambda: sys.modules.pop(name, None))
        return name

    # ---------- 机制：孤儿扫描（注入临时目录） ---------- #
    def test_app_orphan_scan_against_controlled_dir(self):
        """机制侧：未登记的可配置常量必须被点名；已登记/实现类常量不许误报。"""
        self._make_owner({"registered_ratio": 0.5})
        #: 登记键 == 「模块名 + 常量名小写」，与孤儿扫描的 dotted 口径逐字一致。
        self._install_registry({
            "mod_ok.registered_ratio": {"owner": "mod_ok.registered_ratio", "default": 0.5,
                                       "type": "float", "group": "g"}})
        self._install_scan_dir({"mod_ok": chr(10).join([
            "registered_ratio = 0.5",
            "ORPHAN_MAX_ROUNDS = 3",
            "ASSET_DIR = 1",
            "NOT_A_ROUND_NUMBER = 4",
            "MAX_ITEMS = None",
        ])})
        rep = CD.orphan_candidates()
        names = {(x["module"], x["name"]) for x in (rep.get("items") or [])}
        self.assertIn(("mod_ok", "ORPHAN_MAX_ROUNDS"), names,
                      "植入的未登记常量没被点名：%s" % sorted(names))
        self.assertNotIn(("mod_ok", "registered_ratio"), names, "已登记的常量被误报")
        self.assertNotIn(("mod_ok", "ASSET_DIR"), names, "实现类常量（_DIR）被误报")
        self.assertNotIn(("mod_ok", "NOT_A_ROUND_NUMBER"), names, "非配置类常量被误报")
        self.assertNotIn(("mod_ok", "MAX_ITEMS"), names, "非数值常量被误报")

    def test_doctor_ok_with_injected_registry_and_prompts(self):
        """机制侧：注入登记表 + 固定源码后, doctor 应当 ok（不再看工作区）。"""
        self._make_owner({"registered_ratio": 0.5}, name="mod_clean")
        self._install_registry({
            "mod_clean.registered_ratio": {"owner": "mod_clean.registered_ratio", "default": 0.5,
                                           "type": "float", "group": "g"}})
        #: ⚠️ 常量名必须**全小写**：orphan_candidates 的 dotted 用常量原样名（"模块.REGISTERED_RATIO"），
        #:   而 REGISTRY 的 owner 惯例是小写键 → 大写常量名会被判成「未登记」（app 侧既有口径，不在本任务 inScope 改）。
        self._install_scan_dir({"mod_clean": "registered_ratio = 0.5"})
        with mock.patch.object(CD, "prompt_fingerprints",
                               lambda prompts_dir=None: {"checked": 0, "mismatch": []}):
            rep = CD.doctor()
        self.assertTrue(rep.get("ok"), "注入隔离输入后 doctor 仍不 ok：%s" % rep.get("warnings")[:3])
        self.assertEqual((rep.get("orphans") or {}).get("count"), 0,
                         "注入的固定源码里不该有孤儿：%s" % (rep.get("orphans") or {}).get("items"))
        self.assertEqual(rep.get("duplicates"), [])
        self.assertEqual(len(rep.get("params") or []), 1)

    # ---------- 真实护栏：owner 可解析 + 值不漂移 ---------- #
    def test_live_owner_resolvability(self):
        """live 状态：每个 owner / must_match 必须能解析, 且值等于登记默认。

        这条**就是**「工作区真实配置漂移」的报警点：它红了说明 app/ 里有常量
        与 config_doctor.REGISTRY 的登记值不一致（该改代码或该改登记）。
        """
        with mock.patch.object(CD, "orphan_candidates",
                               lambda: {"ok": True, "count": 0, "items": []}):
            with mock.patch.object(CD, "prompt_fingerprints",
                                   lambda prompts_dir=None: {"checked": 0, "mismatch": []}):
                rep = CD.doctor()
        bad = [r for r in (rep.get("params") or []) if r.get("error")]
        self.assertEqual(bad, [], "owner 解析失败：%s" % [(r["key"], r["error"]) for r in bad])
        drift = [(r["key"], r["expect"], r["actual"]) for r in (rep.get("params") or [])
                 if r.get("match") is False]
        self.assertEqual(drift, [], "登记值与运行时值不一致（改代码或改登记）：%s" % drift)
        self.assertEqual(rep.get("duplicates"), [],
                         "must_match 两侧不一致：%s" % rep.get("duplicates"))

    def test_doctor_never_raises(self):
        self.assertIsInstance(CD.doctor(), dict)
        self.assertIsInstance(CD.summary_line(), str)


class TestAudioSilenceLockstep(unittest.TestCase):
    """静音判据的跨模块等值（2026-10-12 T12/T14）。

    为什么必须成对：quality_stage.py:262-263 的 A 层 near-silent 是一个 or 表达式 ——
        mean_db < AUDIO_NEAR_SILENT_DB   or   ratio < AUDIO_HARD_SILENT_RATIO
    两个字段各自在 quality_stage 与 audio_qc 各定义一份（同义、只有作用域是整轨/单段）。
    只锁 ratio 不锁 dB，就会出现「dB 判死、ratio 判活」的自相矛盾。
    两条 dB 常量还是 orphan 扫描的**盲区**（关键词表不认 DB 类后缀），只能人工声明。
    """

    #: (REGISTRY 键, owner, must_match 目标)
    PAIRS = (('audio_hard_silent_ratio', 'quality_stage.AUDIO_HARD_SILENT_RATIO',
              'audio_qc.HARD_SILENT_RATIO'),
             ('audio_near_silent_db', 'quality_stage.AUDIO_NEAR_SILENT_DB',
              'audio_qc.NEAR_SILENT_MEAN_DB'))

    def test_both_halves_are_declared(self):
        for key, owner, other in self.PAIRS:
            meta = CD.REGISTRY.get(key) or {}
            self.assertEqual(meta.get('owner'), owner, key)
            self.assertIn(other, meta.get('must_match') or [],
                          '%s 未声明 must_match=%s（只锁一半会自相矛盾）' % (key, other))
        # audio_qc 侧的 owner 条目也必须登记（否则 orphan 扫描看不见 —— dB 因为命名盲区尤其危险）
        for key, owner in (('hard_silent_ratio', 'audio_qc.HARD_SILENT_RATIO'),
                           ('near_silent_mean_db', 'audio_qc.NEAR_SILENT_MEAN_DB')):
            self.assertEqual((CD.REGISTRY.get(key) or {}).get('owner'), owner, key)

    def test_values_unchanged(self):
        """数值不动：ratio 仍 0.15、dB 仍 -50.0（登记值 == 代码值）。"""
        import audio_qc
        import quality_stage
        self.assertEqual(CD.REGISTRY['hard_silent_ratio']['default'], 0.15)
        self.assertEqual(CD.REGISTRY['audio_hard_silent_ratio']['default'], 0.15)
        self.assertEqual(CD.REGISTRY['near_silent_mean_db']['default'], -50.0)
        self.assertEqual(CD.REGISTRY['audio_near_silent_db']['default'], -50.0)
        self.assertEqual(audio_qc.HARD_SILENT_RATIO, 0.15)
        self.assertEqual(quality_stage.AUDIO_HARD_SILENT_RATIO, 0.15)
        self.assertEqual(audio_qc.NEAR_SILENT_MEAN_DB, -50.0)
        self.assertEqual(quality_stage.AUDIO_NEAR_SILENT_DB, -50.0)

    def test_change_one_side_moves_both(self):
        """apply_to_runtime 回写 owner 时会一并写到 must_match 目标 —— 改一处两处一起变。"""
        import audio_qc
        import quality_stage
        before = (audio_qc.HARD_SILENT_RATIO, quality_stage.AUDIO_HARD_SILENT_RATIO,
                  audio_qc.NEAR_SILENT_MEAN_DB, quality_stage.AUDIO_NEAR_SILENT_DB)
        forced = {'audio_hard_silent_ratio': 0.25, 'audio_near_silent_db': -60.0}
        resolved = {}
        for k, v in forced.items():
            meta = CD.REGISTRY[k]
            resolved[k] = {'owner': meta['owner'],
                           'must_match': meta.get('must_match') or [],
                           'value': v, 'source': 'db'}
        try:
            with mock.patch.object(CC, 'resolve_all', lambda: dict(resolved)):
                CC.apply_to_runtime()
            self.assertEqual(audio_qc.HARD_SILENT_RATIO, 0.25)
            self.assertEqual(quality_stage.AUDIO_HARD_SILENT_RATIO, 0.25,
                             'ratio 两处未联动')
            self.assertEqual(audio_qc.NEAR_SILENT_MEAN_DB, -60.0)
            self.assertEqual(quality_stage.AUDIO_NEAR_SILENT_DB, -60.0,
                             'dB 两处未联动')
        finally:
            (audio_qc.HARD_SILENT_RATIO, quality_stage.AUDIO_HARD_SILENT_RATIO,
             audio_qc.NEAR_SILENT_MEAN_DB, quality_stage.AUDIO_NEAR_SILENT_DB) = before

    def test_single_sided_change_is_detected(self):
        """只改一处不改另一处 → doctor 必须报 duplicates 且 ok=False（护栏的意义）。"""
        import audio_qc
        old = audio_qc.HARD_SILENT_RATIO
        try:
            audio_qc.HARD_SILENT_RATIO = 0.99
            rep = CD.doctor()
            self.assertFalse(rep['ok'], '单边改动没有被体检拦下')
            self.assertTrue(
                any(d.get('other') == 'audio_qc.HARD_SILENT_RATIO'
                    for d in (rep.get('duplicates') or [])),
                'duplicates 未点名 audio_qc.HARD_SILENT_RATIO：%s' % rep.get('duplicates'))
        finally:
            audio_qc.HARD_SILENT_RATIO = old

    def test_silence_thresholds_are_not_orphans(self):
        """dB 是扫描盲区，登记后仍要确保它们不在孤儿名单里（防回退）。"""
        names = {(x['module'], x['name']) for x in (CD.orphan_candidates().get('items') or [])}
        self.assertNotIn(('audio_qc', 'NEAR_SILENT_MEAN_DB'), names)
        self.assertNotIn(('quality_stage', 'AUDIO_NEAR_SILENT_DB'), names)


class TestRegistrationMatchesCodeDefaults(unittest.TestCase):
    """登记值必须与代码实际默认一致（2026-10-12 T14）。

    背景：max_tokens_ceiling 的登记 default 长期是 32768，而 llm_client.py:99 的实际默认是 65536
    （可被 MJSCXT_MAX_TOKENS_CEILING 覆盖）—— 于是**冷进程** doctor() 恒 ok=False；
    只有在同进程里先跑过 config_center.apply_to_runtime()（把登记值回写进模块属性）才会变绿，
    这正好**掩盖**了登记漂移。本任务按契约「以更新登记解决、不改 llm_client.py」。
    """

    def test_max_tokens_ceiling_registration_and_desc(self):
        import llm_client
        meta = CD.REGISTRY['max_tokens_ceiling']
        self.assertEqual(meta['default'], 65536)
        self.assertEqual(meta['default'], llm_client.MAX_TOKENS_CEILING,
                         '登记 default 与 llm_client.MAX_TOKENS_CEILING 漂移')
        self.assertIn('MJSCXT_MAX_TOKENS_CEILING', meta['desc'],
                      'desc 未说明该上限可被环境变量覆盖')

    def test_doctor_has_no_registration_mismatch(self):
        """doctor 的「登记默认 vs 实际值」不符项必须为空（冷/热进程都成立）。"""
        rep = CD.doctor()
        bad = [(r['key'], r['expect'], r['actual'])
               for r in (rep.get('params') or []) if not r.get('match')]
        self.assertEqual(bad, [], '登记与代码实际值不符：%s' % bad)
        self.assertTrue(rep.get('ok'), 'doctor 未通过：%s' % (rep.get('warnings') or [])[:3])


if __name__ == '__main__':
    unittest.main()
