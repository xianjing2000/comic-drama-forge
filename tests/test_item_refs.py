# -*- coding: utf-8 -*-
r"""道具固定参考图 + 异常物件过渡镜（任务 t3）回归测试。

用户 2026-10-12 成片质检报告里的两条：
  · 「文字道具（员工卡 / 楼层安全守则）在不同镜头里长得不一样」→ 道具固定参考图复用；
  · 「渗血洞缺过渡镜头」→ 异常物件首现必须有承担过渡的镜头。

实测证据（真实产物，写进用例注释以便复现）：
  · output/storyboards/进境_整本小说/storyboard_manifest.json（60 镜）里，23 个带道具参考图
    的镜头，refs["道具"] **全是 "ref_canvas"**（画幅统一产物的目录名）——改造前无法从
    manifest 判断「第 12 镜与第 26 镜是否同一张员工卡」；
  · output/qc/进境_整本小说/ref_canvas/ 恰好 7 张 base_<sha1前8>_1664x928.png，按 sha1
    反查源图：4 张角色 + 3 张道具（天合科技员工卡 / 楼层安全守则 / 贴27号的无标签水瓶）；
  · 进境_整本小说 第1集真实镜头文本（shot#24 / #25）用于「前镜铺垫承担过渡」的判定用例：
      shot#24 description「镜头下推至纸面下半→第五条与第六条清晰呈现…右下角红色批注入画。」
      shot#25 description「镜头停在纸面右下角红色圆珠笔…批注上→笔迹急促、转折生硬、末笔戳破纸面留小洞。」
               visual_detail「小洞边缘纸纤维翘起，红墨洇开一小团。」
    判定结果：异常首现 shot#25，承担过渡的是 **shot#24**（下推铺垫）。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import asset_worker as AW  # noqa: E402
import storyboard_helpers as SB  # noqa: E402


# ---- 真实镜头样本（逐字抄自进境_整本小说 第1集剧情本；勿改写）----
REAL_SHOT_24 = {
    'shot_id': 24, 'scene_no': 1, 'shot_type': '局部', 'location': '羡进公寓',
    'items_in_shot': ['楼层安全守则'], 'characters_in_shot': ['羡进'],
    'description': '镜头下推至纸面下半→第五条与第六条清晰呈现：本栋楼无2704号房间，'
                   '请勿注视该门牌；不要相信穿着红色工牌的人。右下角红色批注入画。',
    'visual_detail': '第五节与第三节冲突，纸面被指尖捏出褶皱。',
}
REAL_SHOT_25 = {
    'shot_id': 25, 'scene_no': 1, 'shot_type': '局部', 'location': '羡进公寓',
    'items_in_shot': ['楼层安全守则'], 'characters_in_shot': ['羡进'],
    'description': '镜头停在纸面右下角红色圆珠笔批注上→笔迹急促、转折生硬、末笔戳破纸面留小洞。',
    'visual_detail': '小洞边缘纸纤维翘起，红墨洇开一小团。',
    'dialogue': [{'speaker': '羡进', 'text': '第五条和第三条矛盾。'}],
}


def _png(path, payload=b'PNGDATA'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(payload if isinstance(payload, bytes) else payload.encode('utf-8'))
    return path


class _TmpProjectCase(unittest.TestCase):
    """把资产根指到临时目录（隔离真实产物；不写任何生产文件）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='t3items_')
        self.project = '进境_整本小说'
        self.items_dir = os.path.join(self.tmp, 'assets', 'items')
        self.chars_dir = os.path.join(self.tmp, 'assets', 'characters')
        self.scenes_dir = os.path.join(self.tmp, 'assets', 'scenes')
        self._patches = [
            mock.patch.object(AW, 'ITEMS_DIR', self.items_dir),
            mock.patch.object(AW, 'CHARACTERS_DIR', self.chars_dir),
            mock.patch.object(AW, 'SCENES_DIR', self.scenes_dir),
            mock.patch.object(SB, 'ITEMS_DIR', self.items_dir),
            mock.patch.object(SB, 'CHARACTERS_DIR', self.chars_dir),
            mock.patch.object(SB, 'SCENES_DIR', self.scenes_dir),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        SB._REF_CANVAS_ORIGIN.clear()
        SB._REF_CANVAS_REVERSE_CACHE.clear()

    def asset(self, kind, name, filename='base.png', payload=None):
        root = {'items': self.items_dir, 'characters': self.chars_dir,
                'scenes': self.scenes_dir}[kind]
        return _png(os.path.join(root, self.project, name, filename),
                    payload or ('%s|%s|%s' % (kind, name, filename)))


# ============================ ① 资产身份串（稳定、与绝对路径无关） ============================

class TestItemRefIdentity(_TmpProjectCase):

    def test_maps_three_asset_kinds(self):
        for kind, root in (('items', self.items_dir), ('characters', self.chars_dir),
                           ('scenes', self.scenes_dir)):
            p = os.path.join(root, self.project, 'X', 'base.png')
            with self.subTest(kind=kind):
                self.assertEqual(AW.item_ref_identity(p, self.project),
                                 '%s/%s/X/base.png' % (kind, self.project))

    def test_identity_is_path_independent(self):
        """换输出根（桌面版 / 便携版 / 临时目录）身份串必须一致 —— 这是可比对的前提。"""
        rel = os.path.join(self.project, '楼层安全守则', 'base.png')
        with mock.patch.object(AW, 'ITEMS_DIR', self.items_dir):
            here = AW.item_ref_identity(os.path.join(self.items_dir, rel), self.project)
        other_root = os.path.join(tempfile.mkdtemp(prefix='t3other_'), 'assets', 'items')
        with mock.patch.object(AW, 'ITEMS_DIR', other_root):
            there = AW.item_ref_identity(os.path.join(other_root, rel), self.project)
        self.assertEqual(here, there)
        self.assertEqual(here, 'items/%s/楼层安全守则/base.png' % self.project)

    def test_rejects_other_roots_and_projects(self):
        self.assertEqual(AW.item_ref_identity(os.path.join(self.tmp, 'notassets', 'x.png'),
                                              self.project), '')
        other = os.path.join(self.items_dir, '别的项目', 'X', 'base.png')
        self.assertEqual(AW.item_ref_identity(other, self.project), '')

    def test_unknown_path_returns_empty(self):
        self.assertEqual(AW.item_ref_identity('', self.project), '')
        self.assertEqual(AW.item_ref_identity('C:/nowhere/base.png', self.project), '')


# ============================ ② 固定参考图解析三级降级 ============================

class TestResolveItemReference(_TmpProjectCase):

    def test_exact_dir_uses_base_png(self):
        self.asset('items', '楼层安全守则')
        r = AW.resolve_item_reference(self.project, '楼层安全守则')
        self.assertEqual(r['source'], 'base')
        self.assertEqual(r['identity'], 'items/%s/楼层安全守则/base.png' % self.project)
        self.assertTrue(os.path.isfile(r['path']))
        self.assertFalse(r['pinned'])

    def test_alias_name_resolves_to_same_dir(self):
        """脚本名与资产目录名不完全一致（如带后缀）时，仍落到**同一个**目录/文件。"""
        self.asset('items', '楼层安全守则（打印版）')
        r = AW.resolve_item_reference(self.project, '楼层安全守则')
        self.assertEqual(r['name'], '楼层安全守则（打印版）')
        self.assertEqual(r['file'], 'base.png')

    def test_missing_item_returns_error_not_guess(self):
        self.asset('items', '楼层安全守则')
        r = AW.resolve_item_reference(self.project, '员工卡')
        self.assertEqual(r['path'], '')
        self.assertIn('找不到', r['error'])

    def test_falls_back_to_first_image_when_base_missing(self):
        self.asset('items', '天合科技员工卡', filename='front.png', payload='FRONT')
        r = AW.resolve_item_reference(self.project, '天合科技员工卡')
        self.assertEqual(r['source'], 'first')
        self.assertEqual(r['file'], 'front.png')

    def test_pinned_meta_wins_over_default_file(self):
        self.asset('items', '天合科技员工卡', filename='v2.png', payload='V2')
        AW.save_item_meta_for_test = None  # 占位（避免误用）；真实写入走 pin_item_reference
        meta_path = os.path.join(self.items_dir, self.project, '天合科技员工卡', 'base.meta.json')
        _png(meta_path.replace('.meta.json', '.png'))   # base.png 也存在
        with io.open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({'ref_file': 'v2.png', 'ref_sha256': 'deadbeef'}, f)
        r = AW.resolve_item_reference(self.project, '天合科技员工卡')
        self.assertEqual((r['source'], r['file'], r['pinned']), ('meta', 'v2.png', True))

    def test_pin_target_missing_falls_back_and_reports(self):
        self.asset('items', '天合科技员工卡')
        meta_path = os.path.join(self.items_dir, self.project, '天合科技员工卡', 'base.meta.json')
        with io.open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({'ref_file': 'gone.png'}, f)
        r = AW.resolve_item_reference(self.project, '天合科技员工卡')
        self.assertEqual(r['source'], 'base')
        self.assertIn('不存在', r['error'])


# ============================ ③ 钉住（幂等、只写元数据） ============================

class TestPinItemReference(_TmpProjectCase):

    def _meta(self, item):
        p = os.path.join(self.items_dir, self.project, item, 'base.meta.json')
        with io.open(p, encoding='utf-8') as f:
            return json.load(f)

    def test_pin_writes_fields_and_is_idempotent(self):
        self.asset('items', '天合科技员工卡')
        first = AW.pin_item_reference(self.project, '天合科技员工卡')
        self.assertTrue(first['pinned'])
        self.assertTrue(first['changed'])
        meta = self._meta('天合科技员工卡')
        self.assertEqual(meta['ref_file'], 'base.png')
        self.assertEqual(len(meta['ref_sha256']), 64)
        self.assertEqual(meta['ref_bytes'], os.path.getsize(first['meta'].replace('.meta.json', '.png')))
        self.assertIn('ref_pinned_at', meta)
        again = AW.pin_item_reference(self.project, '天合科技员工卡')
        self.assertFalse(again['changed'], '同一张图重复 pin 不应再写盘')
        self.assertEqual(again['sha256'], first['sha256'])

    def test_pin_keeps_existing_meta_keys(self):
        self.asset('items', '楼层安全守则')
        meta_path = os.path.join(self.items_dir, self.project, '楼层安全守则', 'base.meta.json')
        with io.open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({'seed': 2027280402, 'qc': {'accept': True}, 'asset': '楼层安全守则'}, f)
        AW.pin_item_reference(self.project, '楼层安全守则')
        meta = self._meta('楼层安全守则')
        self.assertEqual(meta['seed'], 2027280402)
        self.assertEqual(meta['qc'], {'accept': True})
        self.assertEqual(meta['asset'], '楼层安全守则')

    def test_pin_does_not_touch_images(self):
        p = self.asset('items', '天合科技员工卡')
        before = open(p, 'rb').read()
        AW.pin_item_reference(self.project, '天合科技员工卡')
        self.assertEqual(open(p, 'rb').read(), before, '钉住只写元数据，不得改动任何图（非目标）')

    def test_pin_unknown_item_reports_error(self):
        r = AW.pin_item_reference(self.project, '不存在的道具')
        self.assertFalse(r['pinned'])
        self.assertTrue(r['error'])

    def test_pin_project_and_report(self):
        self.asset('items', '楼层安全守则')
        self.asset('items', '天合科技员工卡')
        out = AW.pin_project_item_references(self.project)
        self.assertEqual((out['total'], out['pinned'], out['changed']), (2, 2, 2))
        again = AW.pin_project_item_references(self.project)
        self.assertEqual(again['changed'], 0)
        rep = AW.item_reference_report(self.project)
        idents = {it['item']: it['identity'] for it in rep['items']}
        self.assertEqual(idents.get('楼层安全守则'),
                         'items/%s/楼层安全守则/base.png' % self.project)
        self.assertTrue(all(it['pinned'] for it in rep['items']))


# ============================ ④ 分镜侧：同一道具跨镜头同一张图 ============================

class TestStoryboardItemRefs(_TmpProjectCase):

    def test_identity_stable_across_shots(self):
        """验收①：同一道具在任意两个镜头里，manifest 记的身份串逐字相同。"""
        p = self.asset('items', '楼层安全守则')
        shot12, _ = SB.manifest_ref_maps([('道具', '道具【文字道具主体】', p)], self.project)
        shot26, _ = SB.manifest_ref_maps([('道具', '道具【文字道具主体】', p)], self.project)
        self.assertEqual(shot12['道具【文字道具主体】'], shot26['道具【文字道具主体】'])
        self.assertEqual(shot12['道具【文字道具主体】'],
                         'items/%s/楼层安全守则/base.png' % self.project)

    def test_manifest_paths_point_at_source_image(self):
        p = self.asset('items', '天合科技员工卡')
        ids, paths = SB.manifest_ref_maps([('道具', '道具', p)], self.project)
        self.assertEqual(os.path.abspath(paths['道具']), os.path.abspath(p))
        self.assertEqual(ids['道具'], 'items/%s/天合科技员工卡/base.png' % self.project)

    def test_unified_canvas_product_maps_back_to_source(self):
        """画幅统一产物（名里只有 sha1(源路径)前8）也要能反查回本源图身份。"""
        src = self.asset('items', '楼层安全守则')
        import hashlib
        h = hashlib.sha1(os.path.abspath(src).encode('utf-8')).hexdigest()[:8]
        unified = _png(os.path.join(self.tmp, 'qc', 'ref_canvas',
                                    'base_%s_1664x928.png' % h))
        ids, paths = SB.manifest_ref_maps([('道具', '道具', unified)], self.project)
        self.assertEqual(os.path.abspath(paths['道具']), os.path.abspath(src))
        self.assertEqual(ids['道具'], 'items/%s/楼层安全守则/base.png' % self.project)

    def test_pin_rewrites_variant_ref_to_fixed_reference(self):
        """道具槽位被解析成同目录的其它图（v2.png）时，重指回固定参考图 base.png。"""
        self.asset('items', '天合科技员工卡', filename='v2.png', payload='VARIANT')
        self.asset('items', '天合科技员工卡')
        refs = [('道具', '道具', os.path.join(self.items_dir, self.project,
                                              '天合科技员工卡', 'v2.png'))]
        new_refs, notes = SB.pin_item_refs_in_refs(refs, self.project)
        self.assertEqual(os.path.basename(new_refs[0][2]), 'base.png')
        self.assertTrue(notes[0]['changed'])
        self.assertEqual(new_refs[0][1], '道具', '标签必须原样保留（工作流按标签认槽位）')

    def test_non_item_refs_untouched(self):
        """角色/场景参考图不参与重指（换装与多机位是设计，不是漂移）。"""
        c = self.asset('characters', '羡进')
        s = self.asset('scenes', '羡进公寓')
        refs = [('主角色', '主角色', c), ('场景', '场景', s)]
        new_refs, notes = SB.pin_item_refs_in_refs(refs, self.project)
        self.assertEqual([r[2] for r in new_refs], [c, s])
        self.assertEqual(notes, [])

    def test_unknown_item_keeps_original_ref(self):
        """资产里找不到的道具：保持本镜原图并记 error（不猜、不丢槽位）。"""
        refs = [('道具', '道具', os.path.join(self.items_dir, self.project, '幽灵道具', 'base.png'))]
        new_refs, notes = SB.pin_item_refs_in_refs(refs, self.project)
        self.assertEqual(new_refs[0][2], refs[0][2])
        self.assertTrue(notes[0]['error'])


# ============================ ⑤ 异常物件过渡镜（渗血洞） ============================

class TestTransitionShot(_TmpProjectCase):

    def test_no_anomaly_no_plan(self):
        shots = [dict(REAL_SHOT_24), dict(REAL_SHOT_25)]
        plan = SB.plan_transition_shot(shots, keywords=('渗血洞', '血洞', '渗血'))
        self.assertFalse(plan['needed'])
        self.assertEqual(plan['targets'], [])

    def test_real_predecessor_carries_transition(self):
        """真实数据：纸面「小洞」（红墨洇开）首现于 #25，承担过渡的是 #24（镜头下推）。"""
        shots = [dict(REAL_SHOT_24), dict(REAL_SHOT_25)]
        plan = SB.plan_transition_shot(shots, keywords=('小洞',))
        self.assertEqual(len(plan['targets']), 1)
        t = plan['targets'][0]
        self.assertEqual((t['shot_id'], t['role'], t['transition_shot_id']),
                         (25, 'predecessor_setup', 24))
        self.assertFalse(plan['needed'], '已有承担过渡的镜头 → 不需要补镜')
        self.assertEqual(plan['shot_id'], 24)
        self.assertEqual(plan['for_shot_id'], 25)
        self.assertEqual(t['predecessor'], 24)

    def test_self_setup_counts(self):
        """异常首现镜自身就是推近揭示 → 由它自己承担过渡。"""
        shots = [
            {'shot_id': 7, 'description': '人物推门走进房间，环视四周。'},
            {'shot_id': 8, 'description': '镜头缓缓推近墙面，墙上血洞正在渗出暗红液体。'},
        ]
        plan = SB.plan_transition_shot(shots, keywords=('血洞',))
        self.assertEqual(plan['targets'][0]['role'], 'self_setup')
        self.assertEqual(plan['shot_id'], 8)
        self.assertFalse(plan['needed'])

    def test_needs_explicit_transition_marks_shot_and_builds_insert(self):
        """异常凭空出现（前后都没有铺垫/运镜）→ 指明该 shot_id 承担过渡 + 给出补镜结构。"""
        shots = [
            {'shot_id': 3, 'scene_no': 2, 'location': '旧电梯', 'shot_type': '中景',
             'description': '羡进站在电梯里，盯着楼层数字跳动。'},
            {'shot_id': 4, 'scene_no': 2, 'location': '旧电梯', 'shot_type': '局部',
             'items_in_shot': ['楼层安全守则'],
             'description': '电梯内壁上的渗血洞正往外冒血，暗红液体顺壁流下。'},
        ]
        plan = SB.plan_transition_shot(shots, keywords=('渗血洞',))
        self.assertTrue(plan['needed'])
        t = plan['targets'][0]
        self.assertEqual((t['shot_id'], t['role'], t['transition_shot_id']),
                         (4, 'needs_explicit_transition', 4))
        self.assertIn('过渡要求', t['directive'])
        ins = t['insert_shot']
        self.assertEqual(ins['shot_id'], 5, '补镜镜号必须避开既有镜号')
        self.assertEqual(ins['transition_for'], 4)
        self.assertEqual(ins['beat'], '过渡')
        self.assertEqual(ins['dialogue'], [], '过渡镜不引入新配音')
        self.assertIn('渗血洞', ins['transition_reason'])
        self.assertGreater(float(ins['duration']), 0)

    def test_apply_annotates_shots(self):
        shots = [
            {'shot_id': 3, 'location': '旧电梯', 'description': '羡进站在电梯里，盯着楼层数字跳动。'},
            {'shot_id': 4, 'location': '旧电梯', 'description': '电梯内壁上的渗血洞正往外冒血。'},
        ]
        plan = SB.plan_transition_shot(shots, keywords=('渗血洞',))
        report = SB.apply_transition_directive(shots, plan)
        self.assertEqual(report['annotated'], [4])
        self.assertEqual(shots[1]['transition_shot_id'], 4)
        self.assertEqual(shots[1]['transition_for'], 4)
        self.assertEqual(shots[1]['transition_note'], 'needs_explicit_transition')
        self.assertIn('过渡要求', shots[1]['transition_directive'])
        self.assertNotIn('transition_directive', shots[0])

    def test_apply_records_predecessor_as_carrier(self):
        shots = [dict(REAL_SHOT_24), dict(REAL_SHOT_25)]
        plan = SB.plan_transition_shot(shots, keywords=('小洞',))
        SB.apply_transition_directive(shots, plan)
        self.assertEqual(shots[0]['transition_shot_id'], 24)
        self.assertEqual(shots[0]['transition_for'], 25)
        self.assertEqual(shots[0]['transition_note'], 'predecessor_setup')
        self.assertNotIn('transition_directive', shots[1])

    def test_adjacent_hits_are_one_sequence(self):
        """同一段序列里的连续命中只认首现（否则一集里会刷一堆重复判定）。"""
        shots = [
            {'shot_id': 1, 'description': '墙面上的渗血洞渗出暗红液体，滴落在地。'},
            {'shot_id': 2, 'description': '特写：渗血洞边缘的血珠缓缓鼓起。'},
        ]
        plan = SB.plan_transition_shot(shots, keywords=('渗血洞',))
        self.assertEqual(len(plan['targets']), 1)
        self.assertEqual(plan['targets'][0]['shot_id'], 1)

    def test_policy_can_disable_and_cap(self):
        shots = [{'shot_id': i, 'description': '墙上的渗血洞在渗血。' if i % 2 else '空镜。'}
                 for i in range(1, 12)]
        off = SB.plan_transition_shot(shots, policy={'enabled': False})
        self.assertEqual(off['targets'], [])
        capped = SB.plan_transition_shot(shots, keywords=('渗血洞',),
                                        policy={'max_targets_per_episode': 1})
        self.assertEqual(len(capped['targets']), 1)

    def test_short_shots_are_skipped(self):
        """文本过短的镜头不参与判定（防误判）。"""
        shots = [{'shot_id': 1, 'description': '渗血洞'}]
        plan = SB.plan_transition_shot(shots, keywords=('渗血洞',))
        self.assertEqual(plan['targets'], [])


# ============================ ⑥ t16：真实用词「小洞」 + 项目级道具别名表 ============================
#
# 缺口 A：t3 词表在真实剧本里 0 命中（用户说的「渗血洞」是视觉描述，剧本写的是「小洞」）。
# 缺口 B：剧本里「员工证」26 次 /「员工卡」38 次，资产目录只有一个「天合科技员工卡」，
#         修复前 resolve_item_reference(员工证) 报错 → 这些镜头拿不到固定参考图。
# 处置：A 扩词表（含「被拒绝的词」清单）；B 显式别名表（**不放宽**近名匹配，未登记仍报错）。

#: 真实剧本里含「洞」但**不是**异常物件的镜头（逐字抄自 进境_整本小说 第1集.json）
REAL_SHOT_19_PEEPHOLE = {
    'shot_id': 19, 'scene_no': 1, 'location': '羡进公寓', 'items_in_shot': [],
    'characters_in_shot': ['羡进'],
    'description': '门传出金属指关节敲门声：三短一长，停顿两拍，再敲两下。'
                   '羡进半跪在茶几前猛地直起身→头转向画面右侧入户门，喉咙动了一下。',
    'visual_detail': '猫眼黑洞洞，门下缝隙透进更暗的光。',   # ← 「洞」单字会命中这里
}
REAL_SHOT_53_DOORWAY = {
    'shot_id': 53, 'scene_no': 10, 'location': '2704门口', 'items_in_shot': [],
    'characters_in_shot': ['羡进'],
    'description': '羡进近景，画面中央偏左，面朝黑门方向→脚步声已到门内咫尺处，'
                   '他猛地闭眼，默数三下→再睁开——画面右侧原本漆黑的门洞已经不见。',
}
#: 逆天系统_全书 里的「洞穴」＝修炼场所（正常场景）
REAL_SHOT_CAVE = {
    'shot_id': 8, 'scene_no': 3, 'location': '秘境', 'items_in_shot': [],
    'characters_in_shot': ['羡进'],
    'description': '当晚，羡进在秘境中找到了一处安全的洞穴，开始修炼。',
}


class TestTransitionKeywordCoverage(_TmpProjectCase):
    """A：真实用词必须被收录，泛词必须被拒绝。"""

    def test_real_word_is_in_table(self):
        self.assertIn('小洞', SB.TRANSITION_ANOMALY_KEYWORDS)

    def test_old_visual_words_are_kept(self):
        """t3 的视觉族一个都不删（未来续写里可能命中）。"""
        for kw in ('渗血洞', '血洞', '渗血', '血孔', '血痕', '渗血孔', '血洞孔'):
            with self.subTest(kw=kw):
                self.assertIn(kw, SB.TRANSITION_ANOMALY_KEYWORDS)

    def test_every_keyword_has_source_and_reason(self):
        """词表不是随便加的：每个词都必须登记来源与理由（与别名表同构）。"""
        for kw in SB.TRANSITION_ANOMALY_KEYWORDS:
            with self.subTest(kw=kw):
                note = SB.TRANSITION_ANOMALY_KEYWORD_NOTES.get(kw)
                self.assertIsInstance(note, dict, kw)
                self.assertTrue(note.get('source'), kw)
                self.assertTrue(note.get('reason'), kw)

    def test_real_word_hits_real_shot_but_old_table_does_not(self):
        """扩展前 0 命中 / 扩展后命中 —— 真实镜头样本（shot#25）。"""
        shots = [dict(REAL_SHOT_24), dict(REAL_SHOT_25)]
        old = SB.plan_transition_shot(shots, keywords=('渗血洞', '血洞', '渗血'))
        self.assertEqual(old['targets'], [], 't3 词表在真实剧本里 0 命中（这是缺口本身）')
        new = SB.plan_transition_shot(shots)
        self.assertEqual(len(new['targets']), 1)
        self.assertEqual(new['targets'][0]['shot_id'], 25)
        self.assertEqual(new['targets'][0]['keyword'], '小洞')

    # -------- 不误命中 --------

    def test_peephole_not_flagged(self):
        """猫眼黑洞洞（shot#19）不得被判为异常物件首现。"""
        plan = SB.plan_transition_shot([dict(REAL_SHOT_19_PEEPHOLE)])
        self.assertEqual(plan['targets'], [])

    def test_doorway_not_flagged(self):
        """门洞（shot#53）是建筑构件，不得被判为异常物件首现。"""
        plan = SB.plan_transition_shot([dict(REAL_SHOT_53_DOORWAY)])
        self.assertEqual(plan['targets'], [])

    def test_cave_not_flagged(self):
        """逆天系统_全书的「洞穴」（修炼场所）不得被判为异常。"""
        plan = SB.plan_transition_shot([dict(REAL_SHOT_CAVE)])
        self.assertEqual(plan['targets'], [])

    def test_bare_dan_char_is_rejected(self):
        """单字「洞」太泛 —— 必须明确登记为不收，且不得出现在词表里。"""
        self.assertNotIn('洞', SB.TRANSITION_ANOMALY_KEYWORDS)
        self.assertIn('洞', SB.TRANSITION_ANOMALY_KEYWORD_REJECTS)
        self.assertTrue(SB.TRANSITION_ANOMALY_KEYWORD_REJECTS['洞'])

    def test_audit_reports_hits_and_reject_hits(self):
        """反向检查函数：命中镜 + 若收录被拒绝词会误命中哪些镜。"""
        shots = [dict(REAL_SHOT_19_PEEPHOLE), dict(REAL_SHOT_24), dict(REAL_SHOT_25),
                 dict(REAL_SHOT_53_DOORWAY), dict(REAL_SHOT_CAVE)]
        audit = SB.audit_transition_keywords(shots)
        self.assertEqual([h['shot_id'] for h in audit['hits']], [25],
                         '只有真实异常镜被命中；猫眼/门洞/洞穴全部排除')
        hit_ids = [h['shot_id'] for h in audit['hits']]
        rej_ids = sorted(h['shot_id'] for h in audit['reject_hits'])
        self.assertIn(19, rej_ids, '单字「洞」会误命中猫眼 —— 这就是不收它的证据')
        self.assertIn(53, rej_ids, '单字「洞」会误命中门洞')
        self.assertIn(8, rej_ids, '「洞穴」会误命中逆天系统_全书的修炼场所')
        # 误命中＝只被拒绝词命中、不被正式词表命中（25 是真命中，单字「洞」也命中它属于正常）
        false_positives = sorted(set(rej_ids) - set(hit_ids))
        self.assertEqual(false_positives, [8, 19, 53])
        self.assertTrue(all(h['excerpt'] for h in audit['hits'] + audit['reject_hits']))

    def test_audit_accepts_custom_keyword_list(self):
        shots = [dict(REAL_SHOT_25)]
        self.assertEqual(SB.audit_transition_keywords(shots, keywords=('小洞',))['hits'][0]['shot_id'], 25)
        self.assertEqual(SB.audit_transition_keywords(shots, keywords=('渗血洞',))['hits'], [])


class TestItemAliasTable(_TmpProjectCase):
    """B：显式别名表（含来源/理由）；未登记仍报错，不改近名匹配算法。"""

    def test_registered_alias_resolves_to_asset_dir(self):
        """『员工证』→『天合科技员工卡』：修复前报错，修复后解析到同一张图。"""
        p = self.asset('items', '天合科技员工卡')
        r = AW.resolve_item_reference(self.project, '员工证')
        self.assertEqual(r['name'], '天合科技员工卡')
        self.assertEqual(r['identity'], 'items/%s/天合科技员工卡/base.png' % self.project)
        self.assertEqual(os.path.abspath(r['path']), os.path.abspath(p))
        self.assertEqual(r['via_alias'], '天合科技员工卡', '请求名≠资产目录名时必须留痕')
        self.assertTrue(r['alias_source'] and r['alias_reason'], '别名必须带来源与理由')

    def test_alias_and_direct_name_are_the_same_file(self):
        """别名解析结果必须与直接写资产目录名**完全一致**（这才是消歧的意义）。"""
        self.asset('items', '天合科技员工卡')
        a = AW.resolve_item_reference(self.project, '员工证')
        b = AW.resolve_item_reference(self.project, '天合科技员工卡')
        self.assertEqual(a['identity'], b['identity'])
        self.assertEqual(a['path'], b['path'])

    def test_alias_survives_pin_and_meta_path(self):
        """别名 + pin 组合：仍指向 pin 住的那张图。"""
        self.asset('items', '天合科技员工卡', filename='v2.png', payload='V2')
        self.asset('items', '天合科技员工卡')
        meta_path = os.path.join(self.items_dir, self.project, '天合科技员工卡', 'base.meta.json')
        with io.open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({'ref_file': 'v2.png', 'ref_sha256': 'x' * 64}, f)
        r = AW.resolve_item_reference(self.project, '员工证')
        self.assertEqual((r['source'], r['file']), ('meta', 'v2.png'))
        self.assertTrue(r['pinned'])

    def test_every_alias_has_source_and_reason(self):
        for alias, spec in AW.load_item_aliases(self.project).items():
            with self.subTest(alias=alias):
                self.assertTrue(spec.get('target'), alias)
                self.assertTrue(spec.get('source'), alias)
                self.assertTrue(spec.get('reason'), alias)

    def test_unregistered_alias_still_errors_no_guessing(self):
        """未登记的近名（含明令不登记的『红色工牌』）必须报错，不得猜一个目录。"""
        self.asset('items', '天合科技员工卡')
        for name in ('红色工牌', '红工牌', '门禁卡', '幽灵道具'):
            with self.subTest(name=name):
                r = AW.resolve_item_reference(self.project, name)
                self.assertEqual(r['path'], '')
                self.assertIn('找不到', r['error'])

    def test_rejects_are_documented(self):
        self.assertTrue(AW.ITEM_ALIAS_REJECTS)
        for alias, why in AW.ITEM_ALIAS_REJECTS.items():
            with self.subTest(alias=alias):
                self.assertTrue(why)
                self.assertNotIn(alias, AW.load_item_aliases(self.project))

    def test_alias_target_missing_dir_is_not_a_hit(self):
        """别名登记了但资产没生成 → 仍按『找不到』处理（不猜、不回落到不存在的目录）。"""
        self.asset('items', '天合科技员工卡')
        with mock.patch.object(AW, 'ITEM_ALIAS_TABLE',
                               {self.project: {'员工证': {'target': '天合科技员工卡',
                                                          'source': 't', 'reason': 'r'},
                                               '旧工牌': {'target': '尚未生成的道具',
                                                          'source': 't', 'reason': 'r'}}}):
            ok = AW.resolve_item_reference(self.project, '员工证')
            self.assertEqual(ok['name'], '天合科技员工卡')
            missing = AW.resolve_item_reference(self.project, '旧工牌')
            self.assertEqual(missing['path'], '')
            self.assertIn('找不到', missing['error'])

    def test_alias_is_project_scoped(self):
        """『员工证』在别的项目里没有登记 → 报错（项目级表，不跨项目串）。"""
        with mock.patch.object(AW, 'ITEM_ALIAS_TABLE', {}):
            self.asset('items', '天合科技员工卡')
            self.assertEqual(AW.resolve_item_alias(self.project, '员工证')['target'], '')
            self.assertIn('找不到', AW.resolve_item_reference(self.project, '员工证')['error'])

    def test_project_file_overrides_builtin_table(self):
        """项目文件 <ITEMS_DIR>/<项目>/item_aliases.json 可覆盖/补登记（不改代码）。"""
        self.asset('items', '天合科技员工卡')
        with io.open(AW._item_alias_file(self.project), 'w', encoding='utf-8') as f:
            json.dump({'员工证': {'target': '天合科技员工卡', 'source': '项目文件',
                                  'reason': '补登记'},
                       '临时牌': '天合科技员工卡'}, f)   # 简写形式也要支持
        table = AW.load_item_aliases(self.project)
        self.assertEqual(table['员工证']['source'], '项目文件', '文件优先于内嵌表')
        self.assertEqual(table['临时牌']['target'], '天合科技员工卡')
        self.assertEqual(AW.resolve_item_reference(self.project, '临时牌')['name'], '天合科技员工卡')

    def test_broken_alias_file_fails_open(self):
        self.asset('items', '天合科技员工卡')
        with io.open(AW._item_alias_file(self.project), 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assertEqual(AW.resolve_item_reference(self.project, '员工证')['name'],
                         '天合科技员工卡', '覆盖文件坏了只降级，不得让解析失败')

    def test_alias_report_lists_sources(self):
        self.asset('items', '天合科技员工卡')
        rep = AW.item_alias_report(self.project)
        self.assertTrue(rep['aliases'])
        for row in rep['aliases']:
            self.assertTrue(row['source'] and row['reason'])
        self.assertTrue(rep['rejects'])

    def test_storyboard_side_uses_alias_end_to_end(self):
        """分镜侧 pin_item_refs_in_refs 走同一个入口：别名也能对齐到固定参考图。"""
        self.asset('items', '天合科技员工卡')
        refs = [('道具', '道具', os.path.join(self.items_dir, self.project,
                                              '天合科技员工卡', 'base.png'))]
        new_refs, notes = SB.pin_item_refs_in_refs(refs, self.project)
        self.assertEqual(notes[0]['identity'], 'items/%s/天合科技员工卡/base.png' % self.project)
        self.assertFalse(notes[0]['error'])
        self.assertEqual(os.path.basename(new_refs[0][2]), 'base.png')


if __name__ == '__main__':
    unittest.main()
