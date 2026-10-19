# -*- coding: utf-8 -*-
"""跨场视觉衔接修复（2026-10-19）：extra_ref 必须同步进提示词的场景组

缺陷：`_shot_segment` 里「换场首镜挂上一场末镜」只把图追加进 `_seg_comps`
（→ refs），却没追加进 `_shot_scene_refs`（→ 提示词）。构建器按提示词声明的
最大 `<Picture N>` 截断参考图（`h3_director_builder.picture_capacity` 的
`names[:own_cap]`），于是这张「只挂图、无标签」的图被**静默丢弃** ——
跨场视觉衔接特性实际上从未生效，且零报错。

测试两条腿：
  A. 源码/AST 守卫：extra_ref 追加处必须同时把图并入 `_shot_scene_refs`；
  B. 行为：`_h3_picture_defs` 对带 ``desc`` 的场景图逐字采用用途声明
     （跨场图要说「只接续服装/道具/光位」，不能沿用通用的「环境参考」措辞 ——
     后者会诱导 H3 把上一场的空间结构搬进新场景）。
"""
import ast
import io
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ.setdefault('MJSCXT_DATA_DIR', tempfile.mkdtemp(prefix='xscene_'))

from comfyui_client import ComfyUIClient  # noqa: E402

_VH_PATH = os.path.join(_APP, 'video_helpers.py')
_VH_SRC = io.open(_VH_PATH, encoding='utf-8').read()
_VH_TREE = ast.parse(_VH_SRC)


def _find_extra_ref_if():
    """定位 `if extra_ref and len(_seg_comps) < _own_room:` 节点。"""
    for node in ast.walk(_VH_TREE):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.BoolOp) and isinstance(t.op, ast.And)
                and any(isinstance(v, ast.Name) and v.id == 'extra_ref'
                        for v in t.values)):
            return node
    return None


def _body_src(node):
    return ast.get_source_segment(_VH_SRC, node) or ''


class ExtraRefSourceGuardTests(unittest.TestCase):
    """A: 只挂图不声明 = 被 picture_capacity 静默截掉 —— 两件事必须成对出现。"""

    def test_extra_ref_appends_to_scene_refs_in_same_branch(self):
        node = _find_extra_ref_if()
        self.assertIsNotNone(node, '没找到 extra_ref 追加分支（跨场衔接入口被删/改名？）')
        body = ast.Module(body=list(node.body), type_ignores=[])
        assigns = []
        for n in ast.walk(body):
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                tgt = n.targets[0]
                if isinstance(tgt, ast.Name) and tgt.id == '_shot_scene_refs':
                    assigns.append(n)
        self.assertTrue(
            assigns,
            'extra_ref 分支里没有更新 _shot_scene_refs —— 提示词不声明这张图，'
            '构建器会按 picture_capacity 把它静默丢弃（特性等于没开）')
        seg = _body_src(node)
        self.assertIn('_seg_comps.append', seg)
        self.assertIn('_shot_scene_refs', seg)

    def test_extra_ref_carries_cross_scene_desc(self):
        """换场图必须带专用用途声明，不能吃通用「环境参考」措辞。"""
        self.assertIn('"desc": "上一场的最后一个分镜图', _VH_SRC,
                      '上一场末镜衔接图缺少 desc 用途声明（换场会被当成环境参考拷贝）')


class PictureDefsDescTests(unittest.TestCase):
    """B: _h3_picture_defs 的 desc 覆盖行为。"""

    def _defs(self, scene_refs):
        defs, _subjects, sb = ComfyUIClient._h3_picture_defs(
            [{'name': '甲', 'appearance': '青年男性'}],
            scene_refs,
            storyboard_ref={'name': 'shot_06'})
        return defs, sb

    def test_scene_desc_overrides_generic_wording(self):
        desc = '上一场的最后一个分镜图，仅作跨场衔接参考：服装、道具、光位与陈设应与它接续'
        defs, sb = self._defs([
            {'name': '本场场景'},
            {'name': '上一场末镜衔接', 'desc': desc},
        ])
        self.assertEqual(sb, '<Picture 1>')
        self.assertEqual([d[0] for d in defs],
                         ['<Picture 1>', '<Picture 2>', '<Picture 3>', '<Picture 4>'])
        self.assertIn('本场场景', defs[2][1])
        self.assertIn('环境参考', defs[2][1])
        self.assertEqual(defs[3][1], desc)
        self.assertNotIn('环境参考', defs[3][1])

    def test_scene_without_desc_keeps_generic_wording(self):
        defs, _sb = self._defs([{'name': '本场场景'}])
        self.assertIn('本场场景', defs[-1][1])
        self.assertIn('环境参考', defs[-1][1])

    def test_capacity_matches_all_declared_pictures(self):
        """带 desc 的额外场景图会抬高 picture_capacity = 实际槽位，图才不会被截。"""
        import h3_director_builder as hdb
        defs, _sb = self._defs([
            {'name': '本场场景'},
            {'name': '上一场末镜衔接', 'desc': '跨场衔接'},
        ])
        prompt = '\n'.join('%s is ...' % label for label, _ in defs)
        self.assertEqual(hdb.picture_capacity(prompt), 4)


if __name__ == '__main__':
    unittest.main()
