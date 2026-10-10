# -*- coding: utf-8 -*-
"""剧本列表「正在生成」占位回归测试（2026-10-10）。

背景（用户实测：「前端剧本这里还是没有实时显示出来」）：
剧本是**整个步骤跑完才落盘**的。生产跑到「第 1 集剧本 18% · 第 2 轮补生成复检中」时
磁盘上什么都没有 —— /api/episodes 的 list_episodes 返回空列表，前端「剧本概览」
便一直显示「暂无剧集数据 / 请先启动自动生产」，用户以为根本没在跑。
（前端其实已有 10 秒静默重拉轮询，但重拉同样拿到空表，救不了。）

修复：接口读 autopilot 运行态，把**正在生成但尚未落盘**的那一集补成占位行
（status=producing + progress/step/message）。本测试钉死这个行为。
"""
import os
import sys
import unittest
from unittest import mock

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


class TestProducingPlaceholder(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("MJSCXT_DATA_DIR",
                              os.path.join(os.environ.get("APPDATA", ""),
                                           "mjscxt-desktop", "mjscxt-data"))
        os.environ["MJSCXT_AUTOPILOT"] = "0"

    def _client(self):
        import app as app_module
        flask_app = app_module.app
        flask_app.config["TESTING"] = True
        return flask_app.test_client()

    #: 假小说元数据（绕过真实小说库依赖，让接口能走到占位逻辑）
    FAKE_META = {"novel_id": "test_novel", "title": "测试小说", "name": "测试小说",
                 "chapter_count": 1,
                 "chapters": [{"index": 1, "title": "第一章", "start": 0, "end": 100,
                               "char_count": 100}]}

    def _call(self, episodes_on_disk, current):
        """在当前生产态 = current、磁盘剧本 = episodes_on_disk 下请求剧集列表。

        mock 掉小说元数据与产物扫描，只验「占位行是否被补上」这一件事。
        """
        client = self._client()
        with mock.patch("routes.episodes.get_novel", return_value=dict(self.FAKE_META)), \
             mock.patch("routes.episodes.project_store.find_by_novel", return_value=None), \
             mock.patch("routes.episodes.novel_to_script.list_episodes",
                        return_value=list(episodes_on_disk)), \
             mock.patch("routes.episodes._episode_progress",
                        return_value={"status": "pending", "completed_shots": 0}), \
             mock.patch("autopilot.status", return_value={"current": current}):
            resp = client.get("/api/episodes/test_novel")
        return resp

    def test_placeholder_added_when_generating(self):
        """磁盘空 + 正在生成第 1 集 → 列表应出现 status=producing 的占位行。"""
        cur = {"project": "测试项目", "episode": 1, "step": "script",
               "message": "第 2 轮补生成复检中", "percent": 18}
        resp = self._call([], cur)
        self.assertEqual(resp.status_code, 200, "接口应成功返回：%s" % resp.status_code)
        data = resp.get_json() or {}
        eps = data.get("episodes") or []
        self.assertTrue(any(int(e.get("episode_no") or 0) == 1 and e.get("status") == "producing"
                            for e in eps),
                        "正在生成第 1 集时，列表里应出现 producing 占位行：%s" % eps)
        row = next(e for e in eps if int(e.get("episode_no") or 0) == 1)
        self.assertEqual(row.get("progress"), 18)
        self.assertTrue(row.get("pending_script"))

    def test_no_placeholder_when_idle(self):
        """没在生产时不得凭空造行。"""
        resp = self._call([], None)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json() or {}
        self.assertEqual(data.get("episodes") or [], [],
                         "空闲时不该有占位行")

    def test_no_duplicate_when_script_on_disk(self):
        """剧本已落盘 → 不得再补一条重复的占位行。"""
        disk = [{"episode_no": 1, "status": "producing", "shot_count": 12,
                 "title": "入职", "file": "第1集.json"}]
        cur = {"project": "测试项目", "episode": 1, "step": "script",
               "message": "复检中", "percent": 40}
        resp = self._call(disk, cur)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json() or {}
        eps = data.get("episodes") or []
        n1 = [e for e in eps if int(e.get("episode_no") or 0) == 1]
        self.assertEqual(len(n1), 1, "第 1 集出现了重复行：%s" % n1)
        self.assertFalse(n1[0].get("pending_script"),
                         "剧本已落盘的行不该被标成 pending_script")


if __name__ == "__main__":
    unittest.main()
