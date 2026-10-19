# -*- coding: utf-8 -*-
"""
script_v3.py — doubao-creative-drama 技能口径的 v3 剧本 schema 增强
（幂等、fail-open、向后兼容）。

对照技能标准补齐五要素（旧剧本 / 旧模型输出不报错，缺字段补默认或留空，绝不阻断主流程）：

  1. 场次级结构   ：每镜落盘 scene_no / scene_heading（复用 novel_to_script.assign_scene_numbers）
  2. 台词三层文本 ：dialogue[].source_text（原文） / text（display，剧本正文）
                    / spoken_text（TTS 归一层，配音链路可直接消费）
  3. 单集创作卡   ：顶层 episode_card —— core_drama_question / protagonist_goal / fail_cost /
                    urgency / info_gap（观众·主角·对手信息差）/ hook_line（可传播金句）
  4. 场次攻防契约 ：顶层 scene_contracts[] —— 每场 entry_goal / tactic / countermove /
                    turn / exit_value（目标→阻力→策略→反馈→变化）
  5. 证据链台账   ：顶层 evidence_ledger[] —— claim / source / evidence / reveal_at / consequence

调用方式：
  - 落盘前挂载：apply_episode_schema 末尾 try 调用 enrich_v3（任何异常只记日志，不阻断）；
  - 迁移工具：python app/script_v3.py --upgrade <in.json> <out.json> [--project-root DIR]
  - 幂等：重复调用结果一致；模型已给的新字段一律保留，程序只补缺失。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

#: 三层台词的 spoken 层清洗：只保留汉字 / 字母 / 数字 / 常用中文标点，其余不可发音符号剔除。
_SPOKEN_KEEP = re.compile(r"[^\w\u4e00-\u9fff，。！？、；：·…—\'\"]", re.UNICODE)


def _clean_spoken(text: str) -> str:
    """把 display 台词清洗为 TTS 可直接朗读的 spoken 层（保留语义标点，剔除装饰符号）。"""
    if not text:
        return text
    return _SPOKEN_KEEP.sub("", text).strip()


def _load_rules(project_root: str | None):
    """读取项目发音/别名规则（fail-open：读不到返回空表）。"""
    try:
        import text_rules as _tr
        if project_root:
            # text_rules.load(project) 以项目名取规则；root 用于兜底定位
            return _tr.load(os.path.basename(project_root.rstrip("/\\")), root=project_root)
        return _tr.load("")
    except Exception as _e:  # noqa: BLE001
        logger.debug("v3 spoken 规则读取失败（按纯文本清洗）：%s", _e)
        return {"pronounce": {}, "aliases": {}}


def _spoken_for(text: str, rules: dict) -> str:
    """生成 spoken_text：优先应用项目发音表，无规则则做纯文本清洗。"""
    if not text:
        return text
    try:
        import text_rules as _tr
        out = _tr.speak(text, (rules or {}).get("pronounce") or {},
                        (rules or {}).get("aliases") or {})
        if out and out != text:
            return out.strip()
    except Exception:  # noqa: BLE001
        pass
    return _clean_spoken(text)


def _assign_scene_numbers(shots: list) -> list:
    """场次编号落盘（延迟 import，避免模块级循环依赖）。"""
    try:
        from novel_to_script import assign_scene_numbers
        return assign_scene_numbers(shots)
    except Exception as _e:  # noqa: BLE001
        logger.warning("assign_scene_numbers 不可用（场次字段跳过）：%s", _e)
        return []


def _derive_episode_card(script: dict) -> dict:
    """从既有字段推导单集创作卡默认值；模型已给则原样保留。"""
    cur = script.get("episode_card")
    if isinstance(cur, dict) and cur:
        return cur
    emotion = script.get("emotion_curve") or {}
    outline = None
    for o in (script.get("episode_outline") or []):
        if isinstance(o, dict) and int(o.get("ep") or 0) == int(script.get("episode") or 1):
            outline = o
            break
    arc = str(emotion.get("arc_type") or "")
    core_payoff = str(emotion.get("core_payoff") or "")
    hook_end = str(emotion.get("hook_end") or "")
    goal = str((outline or {}).get("goal") or "")
    obstacle = str((outline or {}).get("obstacle") or "")
    card = {
        "core_drama_question": goal or (arc or ""),
        "protagonist_goal": goal,
        "obstacle": obstacle,
        "fail_cost": "",
        "urgency": "",
        "info_gap": {
            "audience": "",
            "protagonist": "",
            "antagonist": "",
        },
        "hook_line": "",
        "payoff": core_payoff,
        "hook_end": hook_end,
    }
    # 有真实内容才标记 derived；空卡也保留字段结构（供校验器识别缺填）
    if goal or core_payoff or hook_end or arc:
        card["derived"] = True
    return card


def _derive_scene_contracts(script: dict) -> list:
    """按场次表生成攻防契约；模型已给（非空数组）则原样保留。"""
    cur = script.get("scene_contracts")
    if isinstance(cur, list) and cur:
        return cur
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    flow = []
    last_no = None
    for sh in shots:
        try:
            no = int(sh.get("scene_no") or 0)
        except (TypeError, ValueError):
            no = 0
        if no != last_no:
            flow.append({"scene_no": no,
                         "location": str(sh.get("location") or "").strip(),
                         "heading": str(sh.get("scene_heading") or "").strip(),
                         "entry_goal": "",
                         "tactic": "",
                         "countermove": "",
                         "turn": "",
                         "exit_value": "",
                         "shot_ids": []})
            last_no = no
        if flow:
            flow[-1]["shot_ids"].append(sh.get("shot_id"))
    for fc in flow:
        fc["shot_count"] = len(fc["shot_ids"])
    if not flow:
        return []
    # 场次攻防的确定性兜底：首镜 edit_reason ≈ entry_goal，末镜 edit_reason ≈ exit_value
    for fc in flow:
        sids = [x for x in fc["shot_ids"] if x is not None]
        first = next((sh for sh in shots if sh.get("shot_id") == sids[0]), None) if sids else None
        last = next((sh for sh in shots if sh.get("shot_id") == sids[-1]), None) if sids else None
        if first:
            fc["entry_goal"] = str(first.get("edit_reason") or "")[:60]
        if last:
            fc["exit_value"] = str(last.get("edit_reason") or "")[:60]
    return flow


def _derive_evidence_ledger(script: dict) -> list:
    """从「指控 / 身份 / 能力证明」类台词推导证据链候选；模型已给则原样保留。"""
    cur = script.get("evidence_ledger")
    if isinstance(cur, list) and cur:
        return cur
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    ledger = []
    _CLAIM_HINTS = ("偷", "赃", "罪", "证据", "我见过", "你没", "怎么可能", "凭什么",
                    "叛", "杀", "骗", "不是", "配", "敢", "规矩", "交出来")
    for sh in shots:
        for d in (sh.get("dialogue") or []):
            if not isinstance(d, dict):
                continue
            t = str(d.get("text") or "").strip()
            if not t:
                continue
            if any(h in t for h in _CLAIM_HINTS):
                ledger.append({
                    "claim": t[:80],
                    "speaker": str(d.get("speaker") or ""),
                    "source": "",
                    "evidence": "",
                    "reveal_at": sh.get("shot_id"),
                    "consequence": "",
                    "derived": True,
                })
    return ledger[:12]


def _derive_emotion_curve(script: dict) -> dict:
    """从 shots 的 beat / emotion 推导情绪曲线（模型已给则原样保留）。

    输出结构对齐 doubao-creative-drama 技能的 emotion_curve：
    arc_type（情绪弧线）+ beat_points（每个节拍覆盖哪几镜、要拿到什么情绪）。
    """
    cur = script.get("emotion_curve")
    if isinstance(cur, dict) and cur.get("beat_points"):
        return cur
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    if not shots:
        return {}
    phase_of = {"开场": "开场", "触发": "冲突升级", "高潮": "爆发/爽点", "收尾": "收尾/悬念"}
    groups = []
    for sh in shots:
        beat = str(sh.get("beat") or "触发").strip()
        phase = phase_of.get(beat, "推进")
        sid = sh.get("shot_id")
        if groups and groups[-1]["phase"] == phase:
            groups[-1]["ids"].append(sid)
        else:
            groups.append({"phase": phase, "ids": [sid], "emotions": []})
        emo = str(sh.get("emotion") or "").strip()
        if emo:
            groups[-1]["emotions"].append(emo)
    beats = []
    for g in groups:
        ids = [x for x in g["ids"] if isinstance(x, int)]
        if not ids:
            continue
        rng = str(ids[0]) if len(ids) == 1 else "%d-%d" % (ids[0], ids[-1])
        emos = list(dict.fromkeys(g["emotions"]))
        beats.append({"shot_range": rng, "phase": g["phase"],
                      "target_emotion": "、".join(emos)[:30]})
    arc = "→".join(b["phase"] for b in beats) if beats else ""
    return {"arc_type": arc, "beat_points": beats, "derived": True}


def _has_content(v) -> bool:
    """判定创作字段是否「有真实内容」：None / 全空 dict / 全空字符串 dict / 空数组都视为无内容。
    用于空壳兜底：模型只输出空键或空条目时，仍触发程序推导而非保留空壳。嵌套递归。"""
    if v is None:
        return False
    if isinstance(v, dict):
        return any(_has_content(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_content(x) for x in v)
    return bool(str(v).strip())


def _refresh_scene_contracts(script: dict, cur: list) -> list:
    """场次契约刷新：保留已有条目的创作字段（entry_goal/tactic/…），但按当前 shots 的
    scene_no 流重建 shot_ids / shot_count / location / heading，并补上新增场次。
    解决「补生成 / 局部重写后新镜头带旧契约、甚至契约场数与镜头归属打架」的问题。"""
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    flow = []
    last_no = None
    for sh in shots:
        try:
            no = int(sh.get("scene_no") or 0)
        except (TypeError, ValueError):
            no = 0
        if no != last_no:
            flow.append({"scene_no": no,
                         "location": str(sh.get("location") or "").strip(),
                         "heading": str(sh.get("scene_heading") or "").strip(),
                         "entry_goal": "",
                         "tactic": "",
                         "countermove": "",
                         "turn": "",
                         "exit_value": "",
                         "shot_ids": []})
            last_no = no
        if flow:
            flow[-1]["shot_ids"].append(sh.get("shot_id"))
    for fc in flow:
        fc["shot_count"] = len(fc["shot_ids"])
    by_no = {}
    if isinstance(cur, list):
        for c in cur:
            if isinstance(c, dict):
                try:
                    by_no[int(c.get("scene_no") or 0)] = c
                except (TypeError, ValueError):
                    continue
    for fc in flow:
        keep = by_no.get(fc["scene_no"])
        if keep:
            for k, v in keep.items():
                if k in ("shot_ids", "shot_count", "scene_no"):
                    continue
                fc[k] = v
        else:
            fc["derived"] = True
    return flow


def llm_card_ledger(client, script: dict, project_root: str | None = None) -> dict:
    """LLM 二次补全 episode_card / evidence_ledger 实内容（生成收尾后一次小请求）。

    输入已定稿镜头表 → 只输出两个创作字段（token 少、不会被长 JSON 截断）。
    fail-open：模型/网络/解析任何失败都保留 enrich_v3 已有的结果，绝不阻断落盘。
    """
    if client is None:
        return script
    try:
        shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
        lines = []
        for sh in shots:
            for d in (sh.get("dialogue") or []):
                if isinstance(d, dict) and str(d.get("text") or "").strip():
                    lines.append("#%s %s：%s" % (
                        sh.get("shot_id"), d.get("speaker"),
                        str(d.get("text")).strip()[:60]))
        if not lines:
            return script
        here = os.path.dirname(os.path.abspath(__file__))
        tmpl_path = os.path.join(here, "prompts", "script_card_ledger.txt")
        if not os.path.exists(tmpl_path):
            tmpl_path = os.path.join(os.path.dirname(here), "prompts", "script_card_ledger.txt")
        with open(tmpl_path, encoding="utf-8") as f:
            tmpl = f.read()
        prompt = (tmpl
                  .replace("<TITLE>", str(script.get("title") or script.get("episode_title") or ""))
                  .replace("<EMOTION>", json.dumps(script.get("emotion_curve") or {}, ensure_ascii=False)[:500])
                  .replace("<CONTRACTS>", json.dumps(script.get("scene_contracts") or [], ensure_ascii=False)[:700])
                  .replace("<LINES>", "\n".join(lines[:100]))
                  .replace("<CARD_SKETCH>", json.dumps(script.get("episode_card") or {}, ensure_ascii=False)[:400]))
        raw = client.chat_json(prompt, system="你是短剧编剧，只做创作字段补全。",
                               temperature=0.3, max_tokens=4096)
        if not isinstance(raw, dict):
            return script
        ec = raw.get("episode_card")
        if isinstance(ec, dict) and _has_content(ec):
            script["episode_card"] = ec
        ev = raw.get("evidence_ledger")
        if isinstance(ev, list) and _has_content(ev):
            script["evidence_ledger"] = ev
        logger.info("v3 LLM 二次补全成功：episode_card 有内容=%s，evidence_ledger=%s 条",
                    _has_content(script.get("episode_card")),
                    len([x for x in (script.get("evidence_ledger") or []) if _has_content(x)]))
    except Exception as _e:  # noqa: BLE001
        logger.warning("v3 LLM 二次补全失败（保留 enrich 结果）：%s", _e)
    return script


def enrich_v3(script: dict, project_root: str | None = None) -> dict:
    """把剧本增强为 v3 技能口径。幂等：模型已给的字段一律保留，只补缺失。

    任何异常均吞掉并记日志 —— 本函数是增强层，绝不阻断剧本落盘。
    """
    if not isinstance(script, dict):
        return script
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    rules = _load_rules(project_root)

    # 1) 场次级结构
    if shots and not any(s.get("scene_no") for s in shots):
        _assign_scene_numbers(shots)

    # 2) 台词三层文本
    for sh in shots:
        for d in (sh.get("dialogue") or []):
            if not isinstance(d, dict):
                continue
            t = str(d.get("text") or "").strip()
            if not t:
                continue
            if not str(d.get("source_text") or "").strip():
                d["source_text"] = t
            if not str(d.get("spoken_text") or "").strip():
                d["spoken_text"] = _spoken_for(t, rules)

    # 3) 单集创作卡（空壳也兜底）
    if not _has_content(script.get("episode_card")):
        script["episode_card"] = _derive_episode_card(script)

    # 4) 场次攻防契约（已有内容也按当前镜头流刷新 shot_ids，并补新增场次）
    cur_contracts = script.get("scene_contracts")
    if not _has_content(cur_contracts):
        script["scene_contracts"] = _derive_scene_contracts(script)
    else:
        refreshed = _refresh_scene_contracts(script, cur_contracts)
        if refreshed:
            script["scene_contracts"] = refreshed

    # 5) 证据链台账（空条目也兜底）
    if not _has_content(script.get("evidence_ledger")):
        script["evidence_ledger"] = _derive_evidence_ledger(script)

    # 5.5) 情绪曲线（2026-10-19 补：对齐 v3 的 emotion_curve，此前完全缺失）
    if not script.get("emotion_curve"):
        ec = _derive_emotion_curve(script)
        if ec:
            script["emotion_curve"] = ec

    # 6) 顶层版本标记
    script.setdefault("schema_version", 3)
    return script


def upgrade_file(in_path: str, out_path: str, project_root: str | None = None) -> dict:
    """文件级迁移：读入旧剧本 JSON → enrich_v3 → 写回新文件（不覆盖原文件）。"""
    with open(in_path, "r", encoding="utf-8") as f:
        script = json.load(f)
    enriched = enrich_v3(script, project_root=project_root)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)
    return enriched


def _main(argv) -> int:
    if len(argv) < 3 or argv[1] != "--upgrade":
        print("用法: python app/script_v3.py --upgrade <in.json> <out.json> [--project-root DIR]")
        return 2
    in_path, out_path = argv[2], argv[3]
    root = None
    if "--project-root" in argv:
        root = argv[argv.index("--project-root") + 1]
    try:
        script = upgrade_file(in_path, out_path, root)
    except Exception as _e:  # noqa: BLE001
        logger.error("迁移失败（%s -> %s）：%s", in_path, out_path, _e)
        return 1
    shots = script.get("shots") or []
    dlg = sum(len([d for d in (s.get("dialogue") or []) if isinstance(d, dict)])
              for s in shots if isinstance(s, dict))
    print("迁移完成: %s -> %s" % (in_path, out_path))
    print("  schema_version=%s  shots=%d  dialogues=%d  scenes=%d  evidence=%d"
          % (script.get("schema_version"), len(shots), dlg,
             len(script.get("scene_contracts") or []),
             len(script.get("evidence_ledger") or [])))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Python 3.13 嵌入式解释器不会把脚本目录加入 sys.path —— 作为独立 CLI 运行时
    # 手动补上 app 目录，否则 from novel_to_script import ... 会 No module named。
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    sys.exit(_main(sys.argv))
