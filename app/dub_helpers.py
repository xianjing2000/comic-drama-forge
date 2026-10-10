# -*- coding: utf-8 -*-
'''配音脚本解析助手（2026-10-11 从 app.py 下沉，助手域拆分第一批）。'''

# 为什么单独成模块：
#   app.py 里按域混杂着大量私有助手，它们相互纠缠，使得千行 worker 无法搬迁
#   （实测 _video_generate_worker_body 依赖 32 个 app 顶层名）。
#   本批先拆出**零外部依赖**的一组，验证「助手按域下沉」这条路径可行。
#
# 依赖清点（实测）：三个函数外部依赖均为 0 ——
#   _dub_resolve_script 54 行 ｜ _dub_line_speaker_from_script 31 行 ｜ _dub_character_desc 13 行
#   只用标准库与 json/re，因此可完全独立成模块。
# 函数体与下沉前逐字一致。
import json
import logging
import os
import re
# 2026-10-11 补齐搬迁时遗漏的模块级名字（自动扫描发现）
from config import PROJECT_OUTPUT_DIR
from config import SCRIPT_DIR
from tts_client import TTSError
import project_store

logger = logging.getLogger(__name__)

def _dub_resolve_script(data: dict) -> dict:
    """解析配音所用剧本：优先 body.script，其次 script_path（限项目输出目录内），最后自动匹配"""
    script = data.get("script")
    if isinstance(script, dict) and script.get("shots"):
        return {"script": script, "script_path": (data.get("script_path") or "").strip(),
                "source": "body"}

    script_path = (data.get("script_path") or "").strip()
    if script_path:
        # P0-4：与 project_store.bind_script / /api/final/video 同一校验函数
        if not project_store.is_path_inside_output(script_path):
            raise TTSError(f"剧本路径必须在项目输出目录内：{os.path.abspath(PROJECT_OUTPUT_DIR)}")
        p = os.path.abspath(script_path)
        if not os.path.exists(p):
            raise TTSError(f"剧本文件不存在：{p}")
        with open(p, "r", encoding="utf-8") as f:
            return {"script": json.load(f), "script_path": p, "source": "path"}

    # 自动匹配 output/scripts 下的剧本（优先路径含项目名，其次 episode_no 命中，最后取最新）
    project_name = data.get("project_name") or ""
    episode = data.get("episode")
    cands = []
    for dp, _dn, fn in os.walk(SCRIPT_DIR):
        for name in fn:
            if not name.lower().endswith(".json"):
                continue
            p = os.path.join(dp, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    s = json.load(f)
            except Exception:
                continue
            if not isinstance(s, dict) or not s.get("shots"):
                continue
            cands.append({"path": p, "mtime": os.path.getmtime(p), "script": s,
                          "episode_no": s.get("episode_no") or (s.get("metadata") or {}).get("episode_no")})
    if not cands:
        raise TTSError("未找到可用剧本（output/scripts 下无含 shots 的 JSON），请先生成剧本")

    # 严格匹配：有 project_name 时必须属于该项目，禁止跨项目回退
    if project_name:
        project_cands = [c for c in cands if project_name in c["path"]]
        if not project_cands:
            raise TTSError(f"项目 '{project_name}' 暂无剧本，请先生成剧本后再使用 TTS 功能")
        hit = project_cands
    else:
        hit = cands

    if episode:
        hit2 = [c for c in hit if str(c["episode_no"]) == str(episode)]
        if hit2:
            hit = hit2
    best = max(hit, key=lambda c: c["mtime"])
    return {"script": best["script"], "script_path": best["path"], "source": "auto"}


def _dub_line_speaker_from_script(ln: dict, project_name: str) -> str:
    """从剧本里找该句所属镜头登记的 speaker（dialogue[].speaker / shot.speaker）。

    取不到返回空串（调用方不做回填）。纯只读，永不抛异常。
    """
    shot_id = ln.get("shot_id")
    if not project_name or shot_id is None:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
    except Exception:  # noqa: BLE001
        return ""
    sid_str = str(shot_id)
    text = str(ln.get("text") or "").strip()
    shots = []
    for sc in (script.get("scenes") or []):
        shots.extend(sc.get("shots") or [])
    if not shots:
        shots = script.get("shots") or []
    for shot in shots:
        if str(shot.get("shot_id") or "") != sid_str:
            continue
        dlg_speaker = ""
        for row in (shot.get("dialogue") or []):
            if str(row.get("text") or "").strip() == text:
                dlg_speaker = str(row.get("speaker") or "").strip()
                if dlg_speaker:
                    break
        return dlg_speaker or str(shot.get("speaker") or "").strip()
    return ""


def _dub_character_desc(character: str, project_name: str) -> str:
    """取角色音色底稿描述（供 design 模式 instruct）；取不到返回空串。"""
    if not character or not project_name:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
        for ch in (script.get("characters") or []):
            if str(ch.get("name") or "") == str(character):
                return str(ch.get("description") or ch.get("tts_voice") or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("角色描述取值失败（忽略）：%s", e)
    return ""
