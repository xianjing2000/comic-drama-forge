# -*- coding: utf-8 -*-
"""跨集连贯性中心（相邻两章转剧本连贯性改进方案 A/B/C/D 落地）

落盘目录：output/continuity/<项目键>/
  bible.json                A① 项目级设定库（角色姓名/身份/外观/性格/称谓 + 当前服装状态；道具外观锁定；场景固定描述）
  style_guide.json          C⑥ 项目级唯一风格配置（全项目一份，不再每集各写一套）
  quotes.json               C⑦ 金句保留清单（按集组织，原文金句必须出现在对应集台词中）
  voice_dict.json           C⑧ 人物口吻词典（自称/他称/语气/口头禅/禁用表达）
  camera_terms.json         C⑧ 运镜术语表（景别 + 运镜 + 常用组合，供分镜 camera 字段取词）
  episodes/第N集_state.json       B④ state_in / state_out（时间/地点/角色状态/未回收伏笔）+ 剧情要点 + 关键事件
  episodes/第N集_summary.json     A② 上集摘要卡（本集生成后写入，供下一集读取）
  episodes/第N集_校验.json         B⑤ 时间线锚点校验 + D⑨ 跨集一致性校验 + 重写记录
  baseline/                       实测对比用的旧版剧本基线

设计约束：
- 只新增文件，不修改 ComfyUI 任何原始工作流文件；
- 所有 LLM 调用统一走 chat_json_robust（截断自动提额重试），事件写入 state/校验结果便于排查。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from config import SHOT_TYPES
from fs_atomic import atomic_write_json, read_json_strict
from llm_client import LLMError, LLMTruncatedError
from llm_client import thinking_token_floor as _thinking_token_floor
from novel_to_script import (
    CHAPTER_CHUNK_CHARS,
    CHAPTER_MAX_SUBCHUNKS,
    SHOT_DURATION_MAX,
    SHOT_DURATION_MIN,
    _norm_camera_motion,
    _norm_shot_type,
    convert_chapter_to_script,
    estimate_shot_duration,
    required_shot_duration,
    save_episode_script,
)
from common_util import as_dict as _as_dict

# 审计 P2-12（2026-09-29）：局部重写回写的角色/物品名必须走统一匹配器 ——
# 旧实现 characters 裸相等（近名静默丢）、items 完全不过滤（未登记名直接写回），
# 与「资产名匹配唯一实现 asset_name_match」的全库口径相反。
import asset_name_match
import chapter_preflight  # 前置解析（人物档案防 OOC）

# 原文覆盖率校验（④⑤：逐句核对是否被镜头承载 + 低于阈值自动补生成；只增不改，不删原文）
import coverage as coverage_mod

# P0-2 全局一致性契约 + 跨镜头校验器（借鉴 penshot；纯确定性、无 LLM、软告警不动重写）
# ⚠️ 放在模块尾部导入（与 coverage_mod 同模式）：continuity_contract 只依赖标准库，
#    不 import 本模块，无循环导入风险；此处顶层 import 即可（离线 import 已验证）。
import continuity_contract as cc_contract

logger = logging.getLogger(__name__)

# ---- 路径/JSON 小工具 + bible 读写已下沉到叶子模块 continuity_store（2026-10-08 解耦）----
# 保留同名再导出：app.py 与守卫脚本的既有 import 零改动；需要 bible 的新代码应直接
# from continuity_store import load_bible / save_bible（不再经过本模块）。
from continuity_store import (  # noqa: F401
    CONTINUITY_VERSION, _now, _path, _safe, bible_path, continuity_root, load_bible,
    load_json, save_bible, save_json)


SYSTEM_CONTINUITY = ("你是漫剧编剧组的『连贯性总监』，精通长篇小说改编中的跨集设定一致性、时间线锚点与伏笔管理，"
                     "只输出严格合法的 JSON。")

# C⑧ 运镜术语表（规则式内置，稳定可复用；分镜 camera 字段必须在此取词）
#
# ⚠️ 为什么要把「轻推 / 轻摇 / 跟随 / 轻手持」显式收进表里（2026-09-29）：生成端
# （h3_prompt_kit._CAMERA_MOVE_EN）与术语表是**同一条链的两端** —— 模型只写表内词，
# 词典只认表内词。参考片 92 镜拆解的运镜列里，非固定运镜几乎全是「轻推 / 轻摇 /
# 跟随 / 轻手持」，旧术语表却只有规范词（推镜 / 摇镜 / 跟镜 / 手持），于是
#   ① 模型按表取词，永远写不出参考片那种「轻推」的克制口气；
#   ② 模型偶尔越界写出「轻推 / 跟随」时，生成端词典又认不出 → 运镜整段丢失。
# 现在两端同词，且强度词（轻/缓/慢/急）有明确映射，克制运镜才真正落得到提示词里。
CAMERA_TERMS = {
    # ⚠️ 由 config.SHOT_TYPES（景别唯一权威表）派生，别再手写一份 —— 手写必然漂移
    #    （历史：此处 6 值含大特写、剧本白名单 5 值、分镜质检 5 值，三处互不相同）。
    "景别": list(SHOT_TYPES),
    # ⚠️ 只增不减：原有规范词（固定/推镜/拉镜/摇镜/移镜/跟镜/升降/环绕/变焦/手持/
    # 定格）一个都不能删 —— 存量剧本已按旧表写词，删词等于让它们「自造术语」；
    # 新词只是**追加**可选口径（含参考片的强度写法）。
    "运镜": ["固定", "定格", "推镜", "拉镜", "推入", "拉远", "摇镜", "移镜", "跟镜",
             "升降", "环绕", "变焦", "手持", "轻推", "缓推", "轻摇", "缓摇",
             "跟随", "轻移", "横移", "轻手持"],
    "常用组合": ["中景固定", "近景固定", "全景固定", "特写固定", "远景推进", "全景升降",
                 "中景跟拍", "中景跟随", "近景轻推", "特写推入", "近景环绕", "中景轻摇",
                 "手持跟拍", "手持跟随", "环绕中景", "拉镜远景", "定格特写"],
}

# 服装/外观颜色词（用于规则化的外观一致性校验，评估报告中最典型的冲突证据即颜色漂移）
COLOR_WORDS = ["黑", "白", "红", "橙", "黄", "绿", "青", "蓝", "紫", "灰", "金", "银", "褐", "棕",
               "粉", "碧", "墨", "赤", "靛", "琥珀", "透明", "玄", "素"]

# 转场/时空落点标记词（D 确定性规则：地点发生跳变时，本集前 3 镜必须出现其中之一，否则判「无过渡硬切」）
TRANSITION_MARKERS = ["转场", "切换", "过渡", "字幕", "旁白", "画外音", "时空", "回溯", "倒流",
                      "脚步声", "归途", "回程", "走出", "走进", "推开", "推门",
                      "踏入", "返回", "回到", "抵达", "来到", "现身", "醒来", "睁眼",
                      "次日", "翌日", "当夜", "深夜", "夜色", "黄昏", "清晨", "破晓", "片刻后",
                      "随后", "紧接着", "光幕", "废墟外", "黑幕", "一闪", "雨夜", "归家"]


# 六类跨集一致性比对维度（D⑨）
CROSS_CHECK_CATEGORIES = ["角色一致性", "剧情因果", "时间地点", "台词一致性", "伏笔回收", "风格统一"]

# ⚠️ 2026-10-10（用户要求「剧本要还原小说的所有细节，不能过度改写」）：
#    MAX_REWRITE_ROUNDS 1 → 2：局部重写只跑 1 轮时，重写后仍有问题的镜头就没人管了；
#    提到 2 轮，让「校验发现问题 → 重写 → 复检」真正形成闭环。
#    COVERAGE_MAX_ROUNDS 1 → 3：与 coverage.run_coverage_check 的 max_rounds 默认值对齐
#    （覆盖率阈值已由 0.70 提到 0.98，一轮补不干净就继续补）。
MAX_REWRITE_ROUNDS = 2
COVERAGE_MAX_ROUNDS = 3   # 原文覆盖率不足时的自动补生成轮次上限


def _ep_dir(continuity_dir: str, project_key: str) -> str:
    return os.path.join(continuity_root(continuity_dir, project_key), "episodes")


def shots_cache_dir(continuity_dir: str, project_key: str, episode_no) -> str:
    """提炼 / 分镜「断点缓存」目录（按集隔离，便于单集清理）。

    缓存由 novel_to_script 以**内容寻址**方式写入（文件名 = sha1(该次 prompt)），
    存在意义：网关偶发挂起会让 pipeline 的 script 步骤整体重试，没有缓存时每次
    都要把 提炼 + 设定 + 全部分镜 从头再烧 20+ 分钟 —— 于是「一抖就永远跑不完」。
    """
    return os.path.join(continuity_root(continuity_dir, project_key),
                        "shots_cache", f"ep{int(episode_no):02d}")


def _read_optional(path: str, what: str):
    """**只读视图**专用：损坏且无 .bak 时显式记 error 并降级为 None。

    仅用于「前端展示类」读取点 —— 它们由 app.py 直接调用（app.py 不在本次改动
    范围内，无法在其内部加保护），且读不到只是少一块展示信息，不影响数据完整性。
    生产链路的读取一律保持 fail-loud（`load_json` 直接抛错）。
    """
    try:
        return load_json(path, None)
    except (ValueError, OSError) as e:
        logger.error("%s 损坏且无可用 .bak，本次按「无数据」展示：%s", what, e)
        return None


def _json_call(client, prompt: str, label: str, system: str = None,
               temperature: float = 0.3, max_tokens: int = 3000,
               events: list = None) -> dict:
    """统一 JSON 调用：走 chat_json_robust（截断自动提额重试），记录重试事件

    ⚠️ max_tokens 会被抬到「思考水位」之上（llm_client.thinking_token_floor）：
    agnes/GLM 这类 always-on reasoning 模型在本业务的思考量可达上万 token，
    额度低于水位时模型会「只吐思考、正文为空」，整步卡死。各调用点传的 1600~7000
    都是按「正文长度」估的，没算思考开销 —— 这里统一兜住，避免逐个调用点漏改。
    """
    system = system or SYSTEM_CONTINUITY
    robust = getattr(client, "chat_json_robust", None)
    if robust is None:
        return client.chat_json(prompt, system=system, temperature=temperature, max_tokens=max_tokens)

    max_tokens = _thinking_token_floor(max_tokens, getattr(client, "reasoning_effort", ""))

    def _on_event(h):
        if events is not None and int(h.get("attempt") or 1) > 1:
            events.append({"label": label, "attempt": h.get("attempt"),
                           "max_tokens": h.get("max_tokens"),
                           "finish_reason": h.get("finish_reason"),
                           "truncated": bool(h.get("truncated"))})

    try:
        return _as_dict(robust(prompt, system=system, temperature=temperature,
                               max_tokens=max_tokens, on_event=_on_event))
    finally:
        meta = getattr(client, "last_json_meta", None)
        if events is not None and isinstance(meta, dict) and int(meta.get("attempts") or 0) > 1:
            seen = {(e.get("label"), e.get("attempt")) for e in events}
            if (label, meta.get("attempts")) not in seen:
                events.append({"label": label, "attempt": meta.get("attempts"),
                               "max_tokens": meta.get("max_tokens"),
                               "finish_reason": meta.get("finish_reason"),
                               "truncated": bool(meta.get("truncated"))})


def _norm_name(name) -> str:
    return re.sub(r"[\s·•、,，。\.]+", "", str(name or "")).strip()


def _locked_character_keys() -> tuple:
    """角色锁定字段：姓名 / 身份 / 外观 / 性格 / 称谓（跨集不再改动）"""
    return ("name", "gender", "identity", "appearance", "personality", "titles")


def merge_bible_from_episode(continuity_dir: str, project_key: str, script: dict,
                             episode_no: int, episode_bible: dict = None) -> dict:
    """把本集角色/物品/场景并入项目级设定库（A① 持久化复用）。

    - 角色：姓名/身份/外观/性格/称谓首次确定后锁定；每集仅更新「当前服装状态」last_seen/状态；
      本集外观与锁定值冲突时记入 conflicts（供 D 校验）；
    - 道具：首次定义后外观锁定，owner 可随剧情变化；
    - 场景：描述首次定义后固定。
    """
    bible = load_bible(continuity_dir, project_key)
    src_chars = (episode_bible or {}).get("characters") or script.get("characters") or []
    src_items = (episode_bible or {}).get("items") or script.get("items") or []
    src_scenes = (episode_bible or {}).get("scenes") or script.get("scenes") or []

    chars = {_norm_name(c.get("name")): c for c in (bible.get("characters") or [])
             if isinstance(c, dict) and _norm_name(c.get("name"))}
    items = {_norm_name(i.get("name")): i for i in (bible.get("items") or [])
             if isinstance(i, dict) and _norm_name(i.get("name"))}
    scenes = {_norm_name(s.get("name")): s for s in (bible.get("scenes") or [])
              if isinstance(s, dict) and _norm_name(s.get("name"))}

    added, conflicts = {"characters": [], "items": [], "scenes": []}, []

    for c in src_chars:
        if not isinstance(c, dict):
            continue
        nm = _norm_name(c.get("name"))
        if not nm:
            continue
        # 别名归一：'方源' 命中已有 '古月方源' 时并入同一角色，避免设定库出现重复条目
        canonical, _renamed = resolve_bible_name(
            c.get("name"), {k: str(v.get("name") or "") for k, v in chars.items()})
        if canonical:
            nm = _norm_name(canonical)
        outfit = str(c.get("outfit") or c.get("current_outfit") or "").strip()
        if nm not in chars:
            row = {k: str(c.get(k) or "").strip() for k in _locked_character_keys()}
            row["name"] = str(c.get("name") or "").strip()
            row["identity"] = str(c.get("identity") or c.get("role") or "").strip()
            row["locked"] = True
            row["appearance_locked"] = True
            row["first_episode"] = int(episode_no)
            row["current_outfit"] = outfit or str(c.get("appearance") or "").strip()
            row["outfit_by_episode"] = {str(int(episode_no)): row["current_outfit"]}
            row["last_seen_episode"] = int(episode_no)
            chars[nm] = row
            added["characters"].append(row["name"])
            continue
        row = chars[nm]
        # 锁定字段：仅补充，不覆盖（保证跨集姓名/外观/性格/称谓固定）
        for k in _locked_character_keys():
            if not str(row.get(k) or "").strip() and str(c.get(k) or "").strip():
                row[k] = str(c.get(k) or "").strip()
        row["last_seen_episode"] = int(episode_no)
        cur_app = str(c.get("appearance") or "").strip()
        locked_app = str(row.get("appearance") or "").strip()
        if cur_app and locked_app and _colors(cur_app) and _colors(cur_app) != _colors(locked_app):
            conflicts.append({"type": "character_appearance", "name": row.get("name"),
                              "locked": locked_app, "current": cur_app,
                              "episode_no": int(episode_no),
                              "detail": f"第{episode_no}集角色「{row.get('name')}」外观与设定库锁定值不一致"})
        if outfit and outfit != str(row.get("current_outfit") or "").strip():
            row["outfit_by_episode"] = {**(row.get("outfit_by_episode") or {}),
                                        str(int(episode_no)): outfit}
            row["current_outfit"] = outfit

    for i in src_items:
        if not isinstance(i, dict):
            continue
        nm = _norm_name(i.get("name"))
        if not nm:
            continue
        if nm not in items:
            row = {k: str(i.get(k) or "").strip() for k in ("name", "category", "appearance", "owner")}
            row["name"] = str(i.get("name") or "").strip()
            row["locked"] = True
            row["appearance_locked"] = True
            row["first_episode"] = int(episode_no)
            row["last_seen_episode"] = int(episode_no)
            items[nm] = row
            added["items"].append(row["name"])
            continue
        row = items[nm]
        owner = str(i.get("owner") or "").strip()
        if owner:
            row["owner"] = owner
        row["last_seen_episode"] = int(episode_no)

    for s in src_scenes:
        if not isinstance(s, dict):
            continue
        nm = _norm_name(s.get("name"))
        if not nm:
            continue
        if nm not in scenes:
            # ⭐ 2026-10-10：字段表补上空间关联三字段。此前只留 name/location/appearance，
            #    导致**剧本即便按新 schema 产出了 fixed_structure / space_group / refs，
            #    也会在这一步被静默丢弃** —— 关联必须从「剧本提取资产」这一刻就带进设定库。
            row = {k: str(s.get(k) or "").strip()
                   for k in ("name", "location", "appearance",
                             "fixed_structure", "space_group")}
            row["name"] = str(s.get("name") or "").strip()
            if s.get("refs"):
                row["refs"] = [str(x).strip() for x in (s.get("refs") or []) if str(x).strip()]
            row["locked"] = True
            row["appearance_locked"] = True
            row["first_episode"] = int(episode_no)
            row["last_seen_episode"] = int(episode_no)
            scenes[nm] = row
            added["scenes"].append(row["name"])
        else:
            _row = scenes[nm]
            # 存量场景补齐关联字段（不覆盖已有值；剧本新给的值优先补空缺）
            for k in ("fixed_structure", "space_group"):
                if not str(_row.get(k) or "").strip() and str(s.get(k) or "").strip():
                    _row[k] = str(s.get(k) or "").strip()
            if not _row.get("refs") and s.get("refs"):
                _row["refs"] = [str(x).strip() for x in (s.get("refs") or []) if str(x).strip()]
            _row["last_seen_episode"] = int(episode_no)

    bible["characters"] = list(chars.values())
    bible["items"] = list(items.values())
    bible["scenes"] = list(scenes.values())
    bible["episodes_seen"] = sorted(set(list(bible.get("episodes_seen") or []) + [int(episode_no)]))
    bible["title"] = bible.get("title") or script.get("title") or ""
    # ⭐ 2026-10-10 场景空间关联（用户明确要求）：**在剧本提取资产时**就完成识别，
    #    而不是拖到资产生成阶段。这样设定库一落地就带着 refs / fixed_structure，
    #    资产生成时直接可用（asset_worker 侧只保留兜底）。
    #    幂等：全部场景都已有字段就直接返回；fail-open：任何异常只记日志。
    _relate_scenes_in_bible(bible)
    save_bible(continuity_dir, project_key, bible)
    return {"added": added, "conflicts": conflicts, "bible": bible}


def _relate_scenes_in_bible(bible: dict) -> None:
    """剧本入库时补齐**场景空间关联**（用户需求①②③，2026-10-10）。

    为什么放在这里：关联关系到出图的一致性（门框 / 地面 / 墙面材质必须与同空间
    其他场景接上），它的**权威来源是剧本**——所以要在「剧本提取资产」这一刻就
    识别并写进设定库，而不是等资产生成时才补。

    幂等：全部场景都已有 refs/fixed_structure 时直接返回，不重复调模型。
    fail-open：模型不可用 / 调用失败 / 解析失败一律只记日志，**绝不阻断剧本入库**。
    就地修改 bible（调用方随后 save_bible）。
    """
    try:
        rows = [s for s in (bible.get("scenes") or []) if isinstance(s, dict)]
        if not rows:
            return
        need = [s for s in rows
                if not str(s.get("fixed_structure") or "").strip()
                and not (s.get("refs") or [])]
        if not need:
            return
        from shared_ai import _optional_llm_client
        import scene_relations as _sr
        _cli = _optional_llm_client()
        if _cli is None:
            logger.info("[场景关联] 无可用文本模型，跳过剧本阶段关联识别"
                        "（资产生成阶段仍会兜底重试）")
            return
        rel = _sr.relate_scenes(_cli, rows)
        stat = _sr.apply_scene_relations(bible, rel)
        if stat.get("updated"):
            logger.info("[场景关联] 剧本阶段已识别 %d 个场景（分组 / 参考 / 固定结构）",
                        stat["updated"])
    except Exception as e:  # noqa: BLE001  关联是增强，绝不阻断剧本入库
        logger.warning("[场景关联] 剧本阶段识别失败（不影响入库）：%s: %s",
                       type(e).__name__, e)


def _bible_name_index(bible: dict) -> dict:
    """规范名索引：normalized 名 → 设定库中的规范姓名"""
    idx = {}
    for c in (bible.get("characters") or []):
        if isinstance(c, dict) and _norm_name(c.get("name")):
            idx[_norm_name(c.get("name"))] = str(c.get("name") or "").strip()
    return idx


def resolve_bible_name(name, name_index: dict):
    """别名解析：'方源' → '古月方源'（双向子串匹配，取最长的规范名）。返回 (规范名|None, 是否发生改名)"""
    nm = _norm_name(name)
    if not nm:
        return None, False
    if nm in name_index:
        return name_index[nm], False
    cands = []
    for k, canonical in name_index.items():
        if len(k) >= 2 and len(nm) >= 2 and (nm in k or k in nm):
            cands.append((len(k), canonical))
    if cands:
        cands.sort(key=lambda x: -x[0])
        return cands[0][1], True
    return None, False


def align_script_assets(script: dict, bible: dict, episode_no: int) -> dict:
    """A① 确定性资产对齐：把本集剧本里的角色别名 / 服装 / 外观强制归一到项目级设定库。

    - 角色名：以设定库姓名为准（含姓氏），剧本与分镜里的简称统一改写为规范名；
    - 外观：一律采用设定库锁定值（防止模型自创外观）；
    - 服装：无剧中换装依据时沿用设定库「当前服装状态」，漂移记录进 outfit_fixed。
    该步骤不依赖模型，保证跨集硬一致；返回 alignment 明细供前端/报告展示。
    """
    name_index = _bible_name_index(bible)
    locked = {_norm_name(c.get("name")): c for c in (bible.get("characters") or [])
              if isinstance(c, dict) and _norm_name(c.get("name"))}
    aliases, outfits_fixed, appearance_fixed = [], [], []

    def _canon(name):
        canonical, renamed = resolve_bible_name(name, name_index)
        return canonical or (str(name or "").strip()), renamed

    for c in (script.get("characters") or []):
        if not isinstance(c, dict):
            continue
        old = str(c.get("name") or "").strip()
        canonical, renamed = _canon(old)
        if renamed and canonical and canonical != old:
            aliases.append({"from": old, "to": canonical})
            c["name"] = canonical
        row = locked.get(_norm_name(canonical))
        if not row:
            continue
        if str(row.get("appearance") or "").strip() and c.get("appearance") != row["appearance"]:
            old_app = str(c.get("appearance") or "").strip()
            c["appearance"] = row["appearance"]
            appearance_fixed.append({"name": canonical, "from": old_app, "to": row["appearance"]})
        if not str(c.get("identity") or "").strip() and row.get("identity"):
            c["identity"] = row["identity"]
        if str(row.get("identity") or "").strip():
            c["identity"] = row["identity"]
        if str(row.get("personality") or "").strip() and c.get("personality") != row["personality"]:
            c["personality"] = row["personality"]
        elif not str(c.get("personality") or "").strip() and row.get("personality"):
            c["personality"] = row["personality"]
        want = str(row.get("current_outfit") or "").strip()
        have = str(c.get("current_outfit") or c.get("outfit") or "").strip()
        if want and have != want:
            if have:
                outfits_fixed.append({"name": canonical, "from": have, "to": want})
            c["current_outfit"] = want
            c.pop("outfit", None)
        elif want:
            c["current_outfit"] = want
        c["locked_by_bible"] = True

    item_locked = {_norm_name(i.get("name")): i for i in (bible.get("items") or [])
                   if isinstance(i, dict) and _norm_name(i.get("name"))}
    item_fixed = []
    for it in (script.get("items") or []):
        if not isinstance(it, dict):
            continue
        row = item_locked.get(_norm_name(it.get("name")))
        if not row:
            continue
        want = str(row.get("appearance") or "").strip()
        have = str(it.get("appearance") or "").strip()
        if want and have != want:
            it["appearance"] = want
            it["locked_by_bible"] = True
            item_fixed.append({"name": it.get("name"), "from": have, "to": want})
        elif want:
            it["locked_by_bible"] = True

    scene_locked = {_norm_name(s.get("name")): s for s in (bible.get("scenes") or [])
                    if isinstance(s, dict) and _norm_name(s.get("name"))}
    scene_fixed = []
    for sc in (script.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        row = scene_locked.get(_norm_name(sc.get("name")))
        if not row:
            continue
        want = str(row.get("appearance") or "").strip()
        have = str(sc.get("appearance") or "").strip()
        if want and have != want:
            sc["appearance"] = want
            sc["locked_by_bible"] = True
            scene_fixed.append({"name": sc.get("name"), "from": have, "to": want})
        elif want:
            sc["locked_by_bible"] = True

    rename_map = {_norm_name(a["from"]): a["to"] for a in aliases}
    if rename_map:
        for s in (script.get("shots") or []):
            if not isinstance(s, dict):
                continue
            cast = s.get("characters_in_shot")
            if isinstance(cast, list):
                s["characters_in_shot"] = [rename_map.get(_norm_name(x), x) for x in cast]
            # ⚠️ visual_detail 必须一起改名：它是画面细节的载体（常写「林风袖口的血」），
            # 漏掉会导致同一角色在描述里叫规范名、在细节里叫别名，分镜图/视频提示词自相矛盾。
            for field in ("description", "visual_detail", "dialogue_text", "dialogue", "prompt_h3"):
                txt = s.get(field)
                if not isinstance(txt, str) or not txt:
                    continue
                for k, canonical in rename_map.items():
                    for src in list({k} | {a["from"] for a in aliases}):
                        if src and _norm_name(src) != _norm_name(canonical) and _norm_name(src) in _norm_name(txt):
                            txt = re.sub(re.escape(src), canonical, txt)
                s[field] = txt

    # 分镜层面：道具 / 场景引用名对齐设定库规范名（含别名归一与 location 归一）
    item_name_index = {k: str(v.get("name") or "") for k, v in item_locked.items()}
    scene_name_index = {k: str(v.get("name") or "") for k, v in scene_locked.items()}
    item_name_fixed, scene_name_fixed = [], []
    for s in (script.get("shots") or []):
        if not isinstance(s, dict):
            continue
        for field, idx, log in (("items_in_shot", item_name_index, item_name_fixed),
                                ("scenes_in_shot", scene_name_index, scene_name_fixed)):
            arr = s.get(field)
            if not isinstance(arr, list):
                continue
            new_arr = []
            for x in arr:
                canonical, renamed = resolve_bible_name(x, idx)
                if canonical:
                    if renamed and canonical != str(x or "").strip():
                        log.append({"from": str(x or "").strip(), "to": canonical,
                                    "shot_id": s.get("shot_id"), "field": field})
                    new_arr.append(canonical)
                else:
                    new_arr.append(x)
            s[field] = new_arr
        loc = s.get("location")
        if isinstance(loc, str) and loc.strip():
            canonical, renamed = resolve_bible_name(loc, scene_name_index)
            if canonical and canonical != loc.strip():
                scene_name_fixed.append({"from": loc.strip(), "to": canonical,
                                         "shot_id": s.get("shot_id"), "field": "location"})
                s["location"] = canonical

    return {"episode_no": int(episode_no), "aliases": aliases,
            "outfit_fixed": outfits_fixed, "appearance_fixed": appearance_fixed,
            "item_appearance_fixed": item_fixed, "scene_appearance_fixed": scene_fixed,
            "item_name_fixed": item_name_fixed, "scene_name_fixed": scene_name_fixed,
            "aligned_at": _now()}


def rule_dialogue_dedup(script: dict, episode_no: int, threshold: float = 0.5) -> list:
    """C⑥⑦/D⑨ 确定性规则：同一集内同一角色台词近似重复检测（不依赖模型，稳定可复现）。

    旧版问题「台词'此生/这次我不再失败'重复」即由此类规则兜底捕获，命中后按 high 级
    问题触发该集局部重写。
    """
    lines = []
    for s in (script.get("shots") or []):
        if not isinstance(s, dict):
            continue
        txt = str(s.get("dialogue_text") or s.get("dialogue") or "").strip()
        if not txt:
            continue
        for seg in re.split(r"[\n；;]+", txt):
            seg = seg.strip()
            if seg:
                lines.append((s.get("shot_id"), seg))
    issues, seen = [], []
    for sid, seg in lines:
        m = re.match(r"^([^：:]{1,10})[：:]", seg)
        name = m.group(1).strip() if m else ""
        body = seg[len(name) + 1:] if name else seg
        if len(_plain(body)) < 6:
            continue
        dup = None
        for pname, pbody, psid in seen:
            if name and pname and _norm_name(pname) != _norm_name(name):
                continue
            pa, pbb = _plain(body), _plain(pbody)
            lb, pb = len(pa), len(pbb)
            ratio = (min(lb, pb) / max(lb, pb)) if max(lb, pb) else 0.0
            contained = (pa in pbb or pbb in pa)
            if ratio >= 0.7 and (contained or _similar_text(body, pbody) >= threshold):
                dup = (pname, psid, pbody)
                break
        if dup:
            issues.append({
                "severity": "high", "category": "台词口吻", "episode_no": int(episode_no),
                "detail": f"第{episode_no}集内台词近似重复（#{dup[1]} 与 #{sid}）",
                "evidence": f"#{dup[1]}「{dup[2][:60]}」↔ #{sid}「{body[:60]}」",
                "fix": "改写其中一处台词，保留信息但不重复措辞", "shot_ids": [sid],
            })
        else:
            seen.append((name, body, sid))
    return issues


def align_state_with_bible(state: dict, bible: dict, episode_no: int) -> dict:
    """B④ 辅助：把 state_in / state_out 里的角色名与服装强制对齐项目级设定库（确定性）。

    避免同一角色在不同集的 state 中出现「方源/古月方源」「青衫/淡雅衣衫」这类锚点漂移，
    对齐轨迹写入 state["state_aligned"]，供前端与报告展示。
    """
    idx = _bible_name_index(bible)
    locked = {_norm_name(c.get("name")): c for c in (bible.get("characters") or [])
              if isinstance(c, dict) and _norm_name(c.get("name"))}
    notes = []
    for key in ("state_in", "state_out"):
        st = state.get(key) or {}
        if not isinstance(st, dict):
            continue
        for cs in (st.get("character_states") or []):
            if not isinstance(cs, dict):
                continue
            old = str(cs.get("name") or "").strip()
            canonical, renamed = resolve_bible_name(old, idx)
            if canonical:
                if renamed and canonical != old:
                    notes.append({"field": key, "from": old, "to": canonical})
                cs["name"] = canonical
            row = locked.get(_norm_name(cs.get("name")))
            if not row:
                continue
            want = str(row.get("current_outfit") or "").strip()
            if not want:
                continue
            have = str(cs.get("outfit") or "").strip()
            if have and have != want:
                notes.append({"field": key, "name": cs.get("name"),
                              "outfit_from": have, "outfit_to": want})
            cs["outfit"] = want
    state["state_aligned"] = notes
    state["aligned_episode_no"] = int(episode_no)
    return state


def merge_dedup_issues(validation: dict, script: dict, episode_no: int) -> dict:
    """把集内台词重复等确定性规则问题并入校验结果，并同步 stats / rewrite_needed"""
    extra = rule_dialogue_dedup(script, episode_no)
    if not extra:
        return validation
    validation.setdefault("issues", [])
    validation["issues"] = list(validation.get("issues") or []) + extra
    stats = validation.get("issue_stats") or {"high": 0, "medium": 0, "low": 0}
    for it in extra:
        sev = it.get("severity") or "low"
        stats[sev] = int(stats.get(sev) or 0) + 1
    validation["issue_stats"] = stats
    validation["rewrite_needed"] = True
    ids = list(validation.get("rewrite_shot_ids") or [])
    for it in extra:
        for sid in it.get("shot_ids") or []:
            if sid not in ids:
                ids.append(sid)
    validation["rewrite_shot_ids"] = ids
    return validation


def _similar_text(a: str, b: str) -> float:
    """字符 2-gram Jaccard 相似度（用于台词去重，确定性可复现）"""
    a, b = _plain(a), _plain(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / max(1, len(ga | gb))


def _colors(text: str) -> set:
    t = str(text or "")
    return {w for w in COLOR_WORDS if w in t}


# ===================== A①（补齐）服装状态 × 镜头对齐 =====================

# 服装词识别：颜色修饰 + 服装名词（用于在镜头文本里定位「碧绿大袍」「银白长袍」这类描述）
OUTFIT_MODIFIERS = ("血", "暗", "深", "浅", "淡", "素", "玄", "苍", "大", "小", "粗", "破",
                    "旧", "新", "薄", "厚", "染", "沾")
_OUF_COLOR_CHARS = "黑红白青蓝紫金银灰褐棕绿黄橙赤翠碧黛墨素皂玄苍绛绯"
_OUF_NOUNS = ("长袍|大袍|血袍|衣袍|外袍|锦袍|道袍|僧袍|长衫|青衫|白衫|衣衫|衣裳|衣服|服饰"
              "|衣装|劲装|战甲|甲衣|铠甲|披风|斗篷|大氅|长裙|短裙|裙装|袍子|袍|衫|裙")
_OUTFIT_PHRASE_RE = re.compile("[" + _OUF_COLOR_CHARS + "][\\u4e00-\\u9fa5]{0,2}?(?:" + _OUF_NOUNS + ")")

# 合法换装标记：镜头里出现这些动作词，说明是「当场换装」，新服装视为有效变更而非漂移
OUTFIT_CHANGE_MARKERS = ("换上", "换了", "换回", "换下", "披上", "穿上", "套上", "脱下", "脱去",
                         "褪去", "更衣", "换衣", "换装", "洗去")

_SHOT_TEXT_FIELDS = ("description", "prompt_h3", "prompt", "video_prompt", "action",
                     "shot_desc", "camera_note", "note", "dialogue_text", "dialogue")


def find_outfit_phrases(text: str) -> list:
    """返回文本中的服装短语 [(start, end, phrase)]，并向前吞掉紧邻的修饰字（血/暗/破…）"""
    t = str(text or "")
    out = []
    for m in _OUTFIT_PHRASE_RE.finditer(t):
        s, e = m.start(), m.end()
        if s > 0 and t[s - 1] in OUTFIT_MODIFIERS:
            s -= 1
        out.append((s, e, t[s:e]))
    return out


def _has_change_marker(text: str, pos: int, window: int = 14) -> bool:
    seg = str(text or "")[max(0, pos - window): pos + window]
    return any(w in seg for w in OUTFIT_CHANGE_MARKERS)


def _shot_text(s: dict) -> str:
    return "\n".join(str(s.get(f) or "") for f in _SHOT_TEXT_FIELDS if isinstance(s.get(f), str))


def _role_name_forms(canonical: str) -> set:
    """角色的可识别称呼：全名 + 去姓简称（「古月方源」→「方源」）"""
    nm = str(canonical or "").strip()
    forms = {_norm_name(nm)} if nm else set()
    if len(nm) >= 3:
        forms.add(_norm_name(nm[-2:]))
    return {f for f in forms if f}


def infer_episode_outfits(script: dict, bible: dict, episode_no: int) -> dict:
    """确定本集各角色「权威服装」：已锁定角色沿用设定库值；首次登场用剧本声明或镜头短语投票。"""
    rows = {_norm_name(c.get("name")): c for c in (bible.get("characters") or [])
            if isinstance(c, dict) and _norm_name(c.get("name"))}
    name_index = _bible_name_index(bible)
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    result = {}
    for c in (script.get("characters") or []):
        if not isinstance(c, dict):
            continue
        old = str(c.get("name") or "").strip()
        canonical, _ = resolve_bible_name(old, name_index)
        canonical = canonical or old
        key = _norm_name(canonical)
        if not key:
            continue
        row = rows.get(key) or {}
        locked = str(row.get("current_outfit") or "").strip()
        if locked:
            result[key] = {"name": canonical, "outfit": locked, "source": "bible_locked"}
            continue
        own = str(c.get("current_outfit") or c.get("outfit") or "").strip()
        if own:
            result[key] = {"name": canonical, "outfit": own, "source": "script_new"}
            continue
        forms = _role_name_forms(canonical) | (_role_name_forms(old) if old else set())
        votes = {}
        for s in shots:
            cast = {_norm_name(x) for x in (s.get("characters_in_shot") or []) if str(x).strip()}
            txt = _shot_text(s)
            txt_norm = _norm_name(txt)
            if not (cast & forms or any(f and f in txt_norm for f in forms)):
                continue
            for _, _, ph in find_outfit_phrases(txt):
                votes[ph] = votes.get(ph, 0) + 1
        if votes:
            best = sorted(votes.items(), key=lambda kv: (-kv[1], -len(kv[0])))[0][0]
            result[key] = {"name": canonical, "outfit": best, "source": "shot_vote"}
    return result


def reconcile_outfit_with_shots(script: dict, bible: dict, episode_no: int,
                                fields: tuple = None) -> dict:
    """A①（补齐）服装状态 × 镜头对齐：把镜头文本里的服装描述统一到本集权威服装。

    规则：1) 角色已在设定库锁定服装 → 全场沿用，镜头里的漂移描述改为锁定值并记录；
    2) 镜头出现「换上/披上/脱下」等换装动作 → 视为合法换装，新服装写回本集权威值与设定库；
    3) 全程确定性执行，不调用模型，保证跨集服装硬一致（旧版第2集「碧绿血袍」↔第3集
    「银白黑袍」这类漂移即由此规则消除）。
    """
    fields = fields or _SHOT_TEXT_FIELDS
    auth = infer_episode_outfits(script, bible, int(episode_no))
    name_index = _bible_name_index(bible)
    fixed, changes, aliases = [], [], []

    # 1) 剧本角色表：姓名归一到设定库规范名 + 服装统一到权威值
    for c in (script.get("characters") or []):
        if not isinstance(c, dict):
            continue
        old = str(c.get("name") or "").strip()
        canonical, renamed = resolve_bible_name(old, name_index)
        if canonical and renamed and canonical != old:
            aliases.append({"from": old, "to": canonical})
            c["name"] = canonical
        info = auth.get(_norm_name(c.get("name")))
        if not info:
            continue
        have = str(c.get("current_outfit") or c.get("outfit") or "").strip()
        if have != info["outfit"]:
            fixed.append({"episode_no": int(episode_no), "name": info["name"],
                          "field": "characters", "shot_id": None,
                          "from": have, "to": info["outfit"]})
        c["current_outfit"] = info["outfit"]
        c.pop("outfit", None)

    # 2) 逐镜头改写漂移服装（含换装动作识别）
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    checked = 0
    for s in shots:
        cast = {_norm_name(x) for x in (s.get("characters_in_shot") or []) if str(x).strip()}
        txt_all = _shot_text(s)
        txt_all_norm = _norm_name(txt_all)
        for key in list(auth.keys()):
            info = auth[key]
            canonical, outfit = info.get("name"), str(info.get("outfit") or "").strip()
            if not canonical or not outfit:
                continue
            forms = _role_name_forms(canonical)
            if not (cast & forms or any(f and f in txt_all_norm for f in forms)):
                continue
            checked += 1
            for f in fields:
                txt = s.get(f)
                if not isinstance(txt, str) or not txt or len(txt) > 1500:
                    continue
                new = txt
                for start, end, phrase in reversed(find_outfit_phrases(txt)):
                    if phrase in outfit or outfit in phrase:
                        continue
                    pc, oc = _colors(phrase), _colors(outfit)
                    if not pc or not oc or pc == oc:
                        continue
                    if _has_change_marker(txt, start):
                        changes.append({"episode_no": int(episode_no), "name": canonical,
                                        "shot_id": s.get("shot_id"), "field": f,
                                        "new_outfit": phrase, "from": outfit,
                                        "reason": "镜头含换装动作，登记为合法换装"})
                        auth[key] = {**info, "outfit": phrase, "source": "change_in_episode"}
                        info, outfit = auth[key], phrase
                        continue
                    new = new[:start] + outfit + new[end:]
                    fixed.append({"episode_no": int(episode_no), "name": canonical,
                                  "shot_id": s.get("shot_id"), "field": f,
                                  "from": phrase, "to": outfit})
                if new != txt:
                    s[f] = new

    # 3) 集内换装后的最终服装回写角色表（供下一集 state_in / bible 承接）
    for c in (script.get("characters") or []):
        if not isinstance(c, dict):
            continue
        info = auth.get(_norm_name(c.get("name")))
        if info and info.get("outfit"):
            c["current_outfit"] = info["outfit"]

    return {"episode_no": int(episode_no), "authoritative": auth, "aliases": aliases,
            "fixed": fixed, "changes": changes, "checked_shots": checked,
            "fixed_count": len(fixed), "change_count": len(changes), "aligned_at": _now()}


# ===================== A② 上集摘要卡 =====================

def state_path(continuity_dir: str, project_key: str, episode_no) -> str:
    return os.path.join(_ep_dir(continuity_dir, project_key), f"第{int(episode_no)}集_state.json")


def summary_path(continuity_dir: str, project_key: str, episode_no) -> str:
    return os.path.join(_ep_dir(continuity_dir, project_key), f"第{int(episode_no)}集_summary.json")


def validation_path(continuity_dir: str, project_key: str, episode_no) -> str:
    return os.path.join(_ep_dir(continuity_dir, project_key), f"第{int(episode_no)}集_校验.json")


def load_state(continuity_dir: str, project_key: str, episode_no):
    """读单集 state_in / state_out。

    A-4 边界决策：这是**只读**读取点（无任何写回），且被 app.py:7669/7674 的
    「单集连贯性视图 / 前端展示」直接调用（app.py 不在本次改动范围内）。
    因此在这里把 read_json_strict 的 fail-loud 抛错显式降级为 None 并记 error ——
    响亮降级，绝不静默；且因为没有写回，不会造成数据清空。
    """
    try:
        data = load_json(state_path(continuity_dir, project_key, episode_no), None)
    except (ValueError, OSError) as e:
        logger.error("连贯性 state 文件损坏且无可用 .bak（%s 第%s集），本次按「无 state」处理：%s",
                     project_key, episode_no, e)
        return None
    return data if isinstance(data, dict) else None


def save_state(continuity_dir: str, project_key: str, state: dict) -> str:
    return save_json(state_path(continuity_dir, project_key, state.get("episode_no") or 1), state)


def load_summary_card(continuity_dir: str, project_key: str, episode_no):
    """读「上集摘要卡」。

    A-4 边界决策：摘要卡是**只读派生数据**（可由上一集剧本重新生成），且出现在
    app.py 直接调用的 `episode_continuity_view` 里。故损坏时显式记 error 并降级为
    None（响亮降级，保持原「无卡片→None」契约），不阻断视图。
    """
    path = summary_path(continuity_dir, project_key, episode_no)
    try:
        data = load_json(path, None)
    except (ValueError, OSError) as e:
        logger.error("连贯性摘要卡损坏且无可用 .bak（%s 第%s集），本次按「无摘要卡」处理：%s",
                     project_key, episode_no, e)
        return None
    return data if isinstance(data, dict) else None


def build_summary_card(script: dict, state: dict = None, episode_no: int = None) -> dict:
    """由本集剧本 + state 生成「上集摘要卡」（剧情要点 / 结尾状态 / 未回收伏笔）"""
    state = state or {}
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    beats = []
    for s in shots[:4]:
        d = str(s.get("description") or "").strip()
        if d:
            beats.append(d[:60])
    plot_points = [str(x).strip()[:80] for x in (state.get("plot_points") or []) if str(x).strip()]
    if not plot_points:
        plot_points = beats[:5]
    out_state = state.get("state_out") or {}
    foreshadows = [f for f in (out_state.get("open_foreshadows") or []) if isinstance(f, dict)]
    return {
        "episode_no": int(episode_no or script.get("episode_no") or 1),
        "episode_title": script.get("episode_title") or "",
        "plot_points": plot_points[:6],
        "key_events": [str(x).strip()[:80] for x in (state.get("key_events") or [])][:8],
        "ending_state": {
            "time": out_state.get("time") or "",
            "location": out_state.get("location") or "",
            "summary": state.get("ending_hook") or "",
            "character_states": out_state.get("character_states") or [],
        },
        "open_foreshadows": foreshadows,
        "dialogue_highlights": [str(x).strip()[:40] for x in (state.get("dialogue_highlights") or [])][:5],
        "generated_at": _now(),
    }


# ===================== C⑥ 项目级风格指南 =====================

def style_guide_path(continuity_dir: str, project_key: str) -> str:
    return _path(continuity_dir, project_key, "style_guide.json")


def load_style_guide(continuity_dir: str, project_key: str) -> dict:
    data = load_json(style_guide_path(continuity_dir, project_key), None)
    return data if isinstance(data, dict) else {}


def ensure_style_guide(client, continuity_dir: str, project_key: str, bible: dict,
                       style: str, episode_no: int, events: list = None,
                       force: bool = False) -> dict:
    """⑥ style_guide 提升为项目级唯一配置：已存在即复用，不再每集各写一套"""
    cur = load_style_guide(continuity_dir, project_key)
    if cur.get("style_guide") and not force:
        cur["reused_by_episode"] = sorted(set(list(cur.get("reused_by_episode") or []) + [int(episode_no)]))
        cur["updated_at"] = _now()
        save_json(style_guide_path(continuity_dir, project_key), cur)
        return cur

    chars = [{"name": c.get("name"), "identity": c.get("identity") or c.get("role"),
              "appearance": c.get("appearance"), "personality": c.get("personality")}
             for c in (bible.get("characters") or [])[:8] if isinstance(c, dict)]
    prompt = f"""【任务】为漫剧项目《{bible.get('title') or ''}》制定**全项目唯一的**画面与叙事风格指南（style_guide）。
【目标风格】{style}
【主要角色】{json.dumps(chars, ensure_ascii=False)}
【要求】该指南将被本项目所有剧集共用，必须稳定、可复用、不与单集剧情绑定。
【输出要求】严格只输出一个 JSON 对象：
{{
  "style": "{style}",
  "style_guide": "画面与叙事风格说明（120 字以内，含色彩基调/光影/构图/节奏）",
  "visual_rules": ["画面统一规则 3-5 条，如：统一冷色调、夜景以青蓝为主光"],
  "narrative_rules": ["叙事统一规则 2-4 条，如：每集结尾留一个钩子、旁白忌大段解释"],
  "forbidden": ["全项目禁止项 2-4 条，如：禁止出现现代物品、禁止跳切式换装"]
}}"""
    data = _json_call(client, prompt, "style_guide", temperature=0.35, max_tokens=1600, events=events)
    data["style"] = str(data.get("style") or style)
    data["style_guide"] = str(data.get("style_guide") or "").strip()
    data["visual_rules"] = [str(x).strip() for x in (data.get("visual_rules") or []) if str(x).strip()][:6]
    data["narrative_rules"] = [str(x).strip() for x in (data.get("narrative_rules") or []) if str(x).strip()][:6]
    data["forbidden"] = [str(x).strip() for x in (data.get("forbidden") or []) if str(x).strip()][:6]
    data["locked"] = True
    data["source_episode"] = int(episode_no)
    data["created_at"] = _now()
    data["updated_at"] = _now()
    save_json(style_guide_path(continuity_dir, project_key), data)
    return data


# ===================== C⑦ 金句保留清单 =====================

def quotes_path(continuity_dir: str, project_key: str) -> str:
    return _path(continuity_dir, project_key, "quotes.json")


def load_quotes(continuity_dir: str, project_key: str) -> dict:
    data = load_json(quotes_path(continuity_dir, project_key), None)
    if not isinstance(data, dict):
        return {"quotes": []}
    if not isinstance(data.get("quotes"), list):
        data["quotes"] = []
    return data


def ensure_quotes_for_episode(client, continuity_dir: str, project_key: str, chapter_text: str,
                              episode_no: int, events: list = None) -> list:
    """⑦ 从本章原文抽取原文金句（必须是原文原句），要求出现在对应集台词中"""
    store = load_quotes(continuity_dir, project_key)
    exist = [q for q in store["quotes"] if int(q.get("episode_no") or 0) == int(episode_no)]
    if exist:
        return exist

    body = (chapter_text or "").strip()
    if len(body) > 4000:
        body = body[:2000] + "\n……（中略）……\n" + body[-2000:]
    prompt = f"""【任务】下面是小说第 {episode_no} 章的原文片段。请挑出 3-5 句最适合作为漫剧「金句」的**原文原句**。
【原文开始】
{body}
【原文结束】
【硬性要求】
1. text 必须是原文中**逐字出现**的连续句子（不得改写、不得拼接、不得翻译），单句 8-40 字；
2. 优先选择：人物标志性台词、主题句、情绪爆发句、伏笔句；
3. 不要选择叙述性描写句（除非极具张力）。
【输出要求】严格只输出一个 JSON 对象：
{{"quotes": [{{"text": "原文原句", "speaker_hint": "大概是谁说的或旁白", "why": "为何保留（15 字以内）"}}]}}"""
    data = _json_call(client, prompt, f"quotes#{episode_no}", temperature=0.3,
                      max_tokens=1600, events=events)
    rows = []
    for q in (data.get("quotes") or []):
        if not isinstance(q, dict):
            continue
        txt = str(q.get("text") or "").strip().strip("“”\"")
        if len(txt) < 6:
            continue
        rows.append({"episode_no": int(episode_no), "text": txt,
                     "speaker_hint": str(q.get("speaker_hint") or "").strip()[:20],
                     "why": str(q.get("why") or "").strip()[:30],
                     "source": "novel_chapter",
                     "in_script": False})
    rows = rows[:5]
    store["quotes"] = [q for q in store["quotes"] if int(q.get("episode_no") or 0) != int(episode_no)] + rows
    store["updated_at"] = _now()
    save_json(quotes_path(continuity_dir, project_key), store)
    return rows


def _plain(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or ""))


def check_quotes_in_script(continuity_dir: str, project_key: str, script: dict,
                           episode_no: int) -> dict:
    """校验金句是否落进本集台词（允许标点/空白差异）"""
    store = load_quotes(continuity_dir, project_key)
    rows = [q for q in store["quotes"] if int(q.get("episode_no") or 0) == int(episode_no)]
    if not rows:
        return {"required": 0, "hit": [], "missed": [], "hit_rate": 1.0}
    script_text = _plain(json.dumps(script.get("shots") or [], ensure_ascii=False))
    hit, missed = [], []
    for q in rows:
        key = _plain(q.get("text"))
        ok = bool(key) and (key in script_text or key[:-1] in script_text or key[1:] in script_text)
        q["in_script"] = ok
        (hit if ok else missed).append(q.get("text"))
    store["updated_at"] = _now()
    store["last_checked_episode"] = int(episode_no)
    save_json(quotes_path(continuity_dir, project_key), store)
    total = len(rows)
    return {"required": total, "hit": hit, "missed": missed,
            "hit_rate": round(len(hit) / total, 3) if total else 1.0}


# ===================== C⑧ 人物口吻词典 + 运镜术语表 =====================

def voice_dict_path(continuity_dir: str, project_key: str) -> str:
    return _path(continuity_dir, project_key, "voice_dict.json")


def load_voice_dict(continuity_dir: str, project_key: str) -> dict:
    data = load_json(voice_dict_path(continuity_dir, project_key), None)
    if not isinstance(data, dict):
        return {"characters": []}
    if not isinstance(data.get("characters"), list):
        data["characters"] = []
    return data


def ensure_voice_dict(client, continuity_dir: str, project_key: str, bible: dict,
                      episode_no: int, events: list = None) -> dict:
    """⑧ 人物口吻词典：自称 / 对他人的称呼 / 语气基调 / 口头禅 / 禁用表达（项目级沉淀，按角色增量补齐）"""
    store = load_voice_dict(continuity_dir, project_key)
    have = {_norm_name(c.get("name")) for c in store["characters"] if isinstance(c, dict)}
    chars = [c for c in (bible.get("characters") or [])
             if isinstance(c, dict) and _norm_name(c.get("name")) and _norm_name(c.get("name")) not in have]
    if not chars:
        return store

    brief = [{"name": c.get("name"), "identity": c.get("identity") or c.get("role"),
              "personality": c.get("personality"), "voice_style": c.get("voice_style")}
             for c in chars[:8]]
    prompt = f"""【任务】为以下角色建立『人物口吻词典』，用于保证跨集台词口吻统一。
【角色】{json.dumps(brief, ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象：
{{"characters": [{{"name": "角色名（必须与输入完全一致）",
  "self_call": "自称（如：我/老夫/本座）",
  "call_others": [{{"target": "对方名或身份", "call": "称呼方式"}}],
  "tone": "语气基调（10 字以内）",
  "catchphrases": ["口头禅或句式特征 1-3 条"],
  "forbidden": ["该角色不该说的话 1-2 条"]}}]}}
【硬性约束】只能输出输入列表中出现的角色，不得新增角色。"""
    data = _json_call(client, prompt, f"voice_dict#{episode_no}", temperature=0.4,
                      max_tokens=2000, events=events)
    rows = []
    for c in (data.get("characters") or []):
        if not isinstance(c, dict) or not _norm_name(c.get("name")):
            continue
        rows.append({
            "name": str(c.get("name")).strip(),
            "self_call": str(c.get("self_call") or "").strip()[:12],
            "call_others": [{"target": str(x.get("target") or "").strip()[:16],
                             "call": str(x.get("call") or "").strip()[:16]}
                            for x in (c.get("call_others") or []) if isinstance(x, dict)][:6],
            "tone": str(c.get("tone") or "").strip()[:20],
            "catchphrases": [str(x).strip()[:30] for x in (c.get("catchphrases") or []) if str(x).strip()][:3],
            "forbidden": [str(x).strip()[:30] for x in (c.get("forbidden") or []) if str(x).strip()][:3],
            "first_episode": int(episode_no),
        })
    if rows:
        store["characters"] = store["characters"] + rows
        store["updated_at"] = _now()
        save_json(voice_dict_path(continuity_dir, project_key), store)
    return store


def camera_terms_path(continuity_dir: str, project_key: str) -> str:
    return _path(continuity_dir, project_key, "camera_terms.json")


def ensure_camera_terms(continuity_dir: str, project_key: str) -> dict:
    """⑧ 运镜术语表（内置规则式，落盘可查，供分镜取词）

    ⚠️ 存量项目按**并集升级**，不是原样返回：内置词表扩充后（2026-09-29 补入
    「跟随 / 轻推 / 轻摇 / 轻手持」等），已有项目若继续读旧文件，生成端就永远
    拿不到新词 —— 术语表与生成端词典必须同步演进，否则「模型按旧词表写、
    词典按新词表认」两端漂移，运镜照样丢。并集只增不减，不推翻项目自定词。
    """
    cur = load_json(camera_terms_path(continuity_dir, project_key), None)
    if isinstance(cur, dict) and cur.get("景别"):
        merged = dict(cur)
        added = {}          # {维度: 本次新并入的词}，仅用于日志
        for _k, _vals in CAMERA_TERMS.items():
            _old = [str(x) for x in (merged.get(_k) or []) if str(x).strip()]
            _new = [x for x in _vals if x not in _old]
            if _new:
                merged[_k] = _old + _new
                added[_k] = _new
        if added:
            merged["updated_at"] = _now()
            save_json(camera_terms_path(continuity_dir, project_key), merged)
            logger.info("运镜术语表并集升级：%s 新增 %s",
                        project_key, {k: len(v) for k, v in added.items()})
        return merged
    data = {**CAMERA_TERMS, "created_at": _now(), "note": "分镜 camera 字段必须从此表取词或组合"}
    save_json(camera_terms_path(continuity_dir, project_key), data)
    return data


# ===================== A②③ 生成期上下文（注入 prompt） =====================

def _render_bible_block(bible: dict) -> str:
    if not bible:
        return ""
    chars = []
    for c in (bible.get("characters") or [])[:10]:
        if not isinstance(c, dict):
            continue
        chars.append({
            "name": c.get("name"),
            "性别": c.get("gender") or "",
            "身份": c.get("identity") or c.get("role") or "",
            "外观（锁定，禁止改动）": c.get("appearance") or "",
            "当前服装状态": c.get("current_outfit") or "",
            "性格": c.get("personality") or "",
            "称谓": c.get("titles") or "",
            "配音风格": c.get("voice_style") or "",
        })
    items = [{"name": i.get("name"), "类别": i.get("category"),
              "外观（锁定）": i.get("appearance"), "持有人": i.get("owner")}
             for i in (bible.get("items") or [])[:10] if isinstance(i, dict)]
    scenes = [{"name": s.get("name"), "地点": s.get("location"),
               "环境描述（固定）": s.get("appearance")}
              for s in (bible.get("scenes") or [])[:10] if isinstance(s, dict)]
    if not (chars or items or scenes):
        return ""
    return ("【项目级设定库（跨集锁定，禁止改名/改外观，人物身份与外观必须与此完全一致）】\n"
            "硬性要求：characters[].name 必须逐字复制设定库中的「name」（含姓氏，禁止简称/去姓，"
            "如设定库为「古月方源」，则剧本中一律写「古月方源」，不得写「方源」）；"
            "gender 必须与设定库「性别」完全一致（禁止改写性别）；"
            "appearance 必须使用设定库锁定文本；outfit/current_outfit 默认沿用设定库「当前服装状态」，"
            "仅当本集剧情明确发生换装时才可变化，且在画面或台词中说明原因。\n"
            f"角色：{json.dumps(chars, ensure_ascii=False)}\n"
            f"物品：{json.dumps(items, ensure_ascii=False)}\n"
            f"场景：{json.dumps(scenes, ensure_ascii=False)}")


def _render_prev_block(prev_card: dict, prev_state: dict) -> str:
    if not prev_card and not prev_state:
        return ""
    lines = ["【上一集摘要卡（必须承接，禁止重演上一集已发生的事件）】"]
    if prev_card:
        lines.append(f"上集：第{prev_card.get('episode_no')}集《{prev_card.get('episode_title') or ''}》")
        if prev_card.get("plot_points"):
            lines.append("上集剧情要点：" + "；".join(str(x) for x in prev_card["plot_points"][:6]))
        if prev_card.get("key_events"):
            lines.append("上集已发生事件（本集禁止重演）：" + "；".join(str(x) for x in prev_card["key_events"][:8]))
        es = prev_card.get("ending_state") or {}
        if es:
            lines.append(f"上集结尾状态：时间={es.get('time') or '未标注'}；地点={es.get('location') or '未标注'}；"
                         f"{es.get('summary') or ''}")
            if es.get("character_states"):
                lines.append("上集结尾时角色状态：" + json.dumps(es["character_states"], ensure_ascii=False))
        if prev_card.get("open_foreshadows"):
            lines.append("上集遗留未回收伏笔（本集需合理承接或部分推进）："
                         + json.dumps(prev_card["open_foreshadows"], ensure_ascii=False))
        if prev_card.get("dialogue_highlights"):
            lines.append("上集台词摘录（避免逐字重复）：" + "；".join(str(x) for x in prev_card["dialogue_highlights"][:5]))
    if prev_state:
        so = prev_state.get("state_out") or {}
        if so:
            lines.append("上集 state_out（本集 state_in 必须与之衔接，不得倒退或冲突）："
                         + json.dumps({"time": so.get("time"), "location": so.get("location"),
                                       "character_states": so.get("character_states") or [],
                                       "open_foreshadows": so.get("open_foreshadows") or []},
                                      ensure_ascii=False))
    return "\n".join(lines)


CONTRACT_TEXT = (
    "【衔接契约（后续集·硬性约束）】\n"
    "1. 本集开场必须承接上一集结尾的状态（时间、地点、人物处境、情绪），不得跳接、不得倒退；\n"
    "2. 严禁重演上一集已经发生的事件（包括重生、觉醒、夺宝、首次登场等），如需提及只能用回忆/一句话带过；\n"
    "3. 上一集遗留的未回收伏笔，本集必须至少推进一条，且不得给出与设定库冲突的新设定；\n"
    "4. 角色姓名 / 身份 / 外观 / 性格 / 称谓一律以设定库为准，服装只允许在本集剧情内有明确原因时变化，并需在台词或画面中体现变化原因；\n"
    "5. 道具外观首次定义后固定，不得改色、改形、改材质；场景环境描述固定。\n"
    "6. 同一句标志性台词不得在相邻集重复出现（除刻意回环呼应外，须改动措辞），"
    "同一集内同一角色的台词也不得近似重复（不得只替换时间词/语气词重复表述）；\n"
    "7. 本集首镜必须显式交代与上一集结尾的时空关系：若发生时间推进或空间转换（如重生、回溯、"
    "换场），须在首镜用镜头语言、字幕或一句旁白点明落点（时间+地点），禁止无过渡直接跳切。"
    "首镜 description 必须以「（承接：上集结尾…→本集开场…，转场方式…）」的形式写出承接说明；"
    "当上集结尾地点与本集开场地点不同（如从战场回到房间），必须给出可画出的移动/转场过程"
    "（走出战场、时空回溯、雨夜归途等），不得两个地点直接硬切，否则视为断裂；\n"
    "8. 本集结尾若仍有未回收伏笔（open_foreshadows），允许用一句旁白/字幕点出悬念以维持观感连续，"
    "但不得新增剧情事件、不得超前消耗后续剧情。"
)

CONTRACT_TEXT_FIRST_EPISODE = (
    "【开篇契约（硬性约束）】\n"
    "1. 本集是全剧第一集（开篇），**没有上一集**。开场直接从故事起点切入，**禁止**出现「承接上集」"
    "「承接前情」「上集结尾」等字样，也**禁止**假设任何前情（如『此前负伤离场』『上集已发生的事件』）；\n"
    "2. 如需交代背景，只能用**本集画面内**的物件/台词/一句旁白带过，不得引用「未发生」的上集剧情；\n"
    "3. 同一句标志性台词不得在集内重复（除刻意回环呼应外，须改动措辞）；\n"
    "4. 角色姓名 / 身份 / 外观 / 性格 / 称谓一律以设定库为准；道具外观首次定义后固定。"
)


def _render_dialogue_block(style_guide: dict, voice_dict: dict, quotes: list) -> str:
    lines = ["【台词与风格固化要求】"]
    if style_guide.get("style_guide"):
        lines.append(f"项目级风格指南（全项目唯一，必须遵守）：{style_guide.get('style_guide')}")
    if style_guide.get("forbidden"):
        lines.append("项目禁止项：" + "；".join(str(x) for x in style_guide["forbidden"][:4]))
    chars = [c for c in (voice_dict.get("characters") or []) if isinstance(c, dict)]
    if chars:
        brief = [{"name": c.get("name"), "自称": c.get("self_call"), "语气": c.get("tone"),
                  "称呼他人": c.get("call_others") or [], "口头禅": c.get("catchphrases") or [],
                  "禁用": c.get("forbidden") or []} for c in chars[:8]]
        lines.append("人物口吻词典（台词必须符合）：" + json.dumps(brief, ensure_ascii=False))
    if quotes:
        lines.append("本集必须出现的原文金句（须原样出现在对应角色台词中，不得改写）："
                     + json.dumps([q.get("text") for q in quotes if isinstance(q, dict)], ensure_ascii=False))
    return "\n".join(lines)


#: 参考片 92 镜实测的「克制运镜」白名单：非固定运镜 100% 落在这些词上。
CAMERA_MOTION_PREFERRED = ("固定", "轻推", "轻摇", "跟随", "轻手持")
#: 参考片 92 镜里**一次都没用**的运镜（保留在词表中供存量剧本/特殊需求使用，但提示慎用）。
CAMERA_MOTION_DISCOURAGED = ("推镜", "拉镜", "摇镜", "移镜", "升降", "环绕", "变焦", "定格",
                             "轻移", "横移", "缓推", "缓摇")


def _render_camera_block(camera_terms: dict) -> str:
    if not camera_terms:
        return ""
    return ("【运镜术语表（camera 字段必须从此表取词或组合，禁止自造术语）】\n"
            f"景别：{'、'.join(camera_terms.get('景别') or [])}\n"
            f"运镜：{'、'.join(camera_terms.get('运镜') or [])}\n"
            f"常用组合：{'、'.join(camera_terms.get('常用组合') or [])}\n"
            "【克制运镜·推荐】" + " / ".join(CAMERA_MOTION_PREFERRED) +
            "（参考片 92 镜的非固定运镜全部是这 4 种；运镜以固定为主，约 75%）\n"
            "【慎用运镜】" + " / ".join(CAMERA_MOTION_DISCOURAGED) +
            "（参考片 92 镜一次未用；只在明确的空间意图下才用，日常默认固定）\n"
            "【景别 × 运镜配对（越近越静，运镜长在中景）】\n"
            "  · 特写 → 固定（把观众钉在表情上，参考片特写 100% 固定）\n"
            "  · 局部 / 中近景 / 近景 → 基本固定\n"
            "  · 中景 → 运镜主力：人物位移用「跟随」、关系变化用「轻摇」、情绪递进用「轻推」\n"
            "  · 全景 → 只用于跟人走路（跟随）\n"
            "【景别占比参考（参考片 92 镜实测）】近景 26% / 中近景 25% / 局部 25%"
            "（局部 = 只拍手部/道具、不出现完整人脸的插入镜）/ 中景 17% / 全景 3% / 特写 2%"
            " / 大特写·远景·大远景 0%\n"
            "【一镜一动作】每镜只推进 1 个动作节拍；一段里连续完成 2 个以上动作时，"
            "按动作先后拆成相邻两镜，不要在一镜内堆叠多个动作")


def _render_appearance_anchor_block(bible: dict) -> str:
    """外观锚点块（优化#5 跨集角色一致性传递，2026-10-01）。

    bible 的角色外观在第 N 集首次出现时即锁定（merge_bible_from_episode「仅补充不覆盖」），
    但生成端此前只拿到设定库原文，缺一条「逐字钉死、禁止改写」的强约束 —— LLM 写长剧本时
    改写发色/瞳色/服装款式造成跨集漂移。本块把锁定外观 + 当前服装以最高优先级显式重申，
    拼在 bible_block 尾部随行注入（novel_to_script 无需改动即生效）。
    """
    chars = [c for c in ((bible or {}).get("characters") or [])
             if isinstance(c, dict) and str(c.get("name") or "").strip()]
    lines = []
    for c in chars[:12]:
        name = str(c.get("name") or "").strip()
        app_txt = str(c.get("appearance") or "").strip()
        outfit = str(c.get("current_outfit") or "").strip()
        if not (app_txt or outfit):
            continue
        seg = f"  · {name}："
        if app_txt:
            seg += f"外观（逐字锁定，禁止任何改写/换色/换发型）＝{app_txt}"
        if outfit:
            seg += f"；本阶段服装＝{outfit}"
        lines.append(seg)
    if not lines:
        return ""
    return ("\n【角色外观锚点·最高优先级】以下为设定库锁定值，本集所有镜头必须逐字一致，"
            "禁止在画面描述/外观字段中改写（含发色、瞳色、发型、体格、服装款式与配色）；"
            "如剧情要求换装，只允许改 outfit/服装字段，不得改写外观锚点本身：\n"
            + "\n".join(lines) + "\n")


def build_continuity_context(continuity_dir: str, project_key: str, episode_no: int,
                             style: str, quotes: list = None) -> dict:
    """组装注入生成流程的上下文（②上集摘要卡 + ①设定库 + ③衔接契约 + ⑥⑦⑧风格/金句/口吻/运镜）"""
    bible = load_bible(continuity_dir, project_key)
    style_guide = load_style_guide(continuity_dir, project_key)
    voice_dict = load_voice_dict(continuity_dir, project_key)
    camera_terms = ensure_camera_terms(continuity_dir, project_key)
    prev_card = load_summary_card(continuity_dir, project_key, int(episode_no) - 1)
    prev_state = load_state(continuity_dir, project_key, int(episode_no) - 1)
    # 前置解析（若已运行）：人物档案 / 情绪基线 / 关键事件注入生成约束
    _preflight = chapter_preflight.load_preflight(continuity_dir, project_key, int(episode_no))
    _preflight_block = chapter_preflight.build_preflight_injection_block(_preflight)
    return {
        "enabled": True,
        "version": CONTINUITY_VERSION,
        "project_key": project_key,
        "episode_no": int(episode_no),
        "prev_episode_no": int(episode_no) - 1 if prev_card or prev_state else None,
        # 优化#5：外观锚点块拼在 bible_block 尾部（随行注入，生成端无需单独接线）
        "bible_block": _render_bible_block(bible) + _render_appearance_anchor_block(bible),
        "appearance_anchor_block": _render_appearance_anchor_block(bible),
        "prev_block": _render_prev_block(prev_card, prev_state),
        # 首集（无上集 state/摘要卡）用「开篇契约」：禁止 LLM 编造不存在的上集前情；
        # 后续集才用「衔接契约」（承接上集结尾）。
        "contract_block": CONTRACT_TEXT if (prev_card or prev_state) else CONTRACT_TEXT_FIRST_EPISODE,
        "style_block": _render_dialogue_block(style_guide, voice_dict, quotes or []),
        "dialogue_block": _render_dialogue_block(style_guide, voice_dict, quotes or []),
        "camera_block": _render_camera_block(camera_terms),
        "style_guide_text": style_guide.get("style_guide") or "",
        "quotes": [q.get("text") for q in (quotes or []) if isinstance(q, dict)],
        "bible": bible,
        "style_guide": style_guide,
        "voice_dict": voice_dict,
        "camera_terms": camera_terms,
        "prev_card": prev_card,
        "prev_state": prev_state,
        "preflight_block": _preflight_block,
        "preflight": _preflight,
        "built_at": _now(),
    }


# ===================== B④ state_in / state_out =====================

STATE_SCHEMA_HINT = """{
  "state_in": {"time": "本集开场时间（必须给出可比较锚点：如『紧接上集·当夜』『次日清晨』『三日后黄昏』，禁止填『未说明』）",
    "location": "本集开场地点",
    "transition": "从上集结尾地点/时间到本集开场的衔接方式（一句话，说明移动或转场过程；首集填『开篇』）",
    "character_states": [{"name": "角色名", "status": "处境/情绪", "outfit": "服装状态", "goal": "当前目标"}],
    "open_foreshadows": [{"id": "F1", "desc": "伏笔描述", "from_episode": 1}]},
  "state_out": {"time": "本集结尾时间", "location": "本集结尾地点",
    "transition": "本集结尾到下一集开场的建议承接方式（一句话）",
    "character_states": [{"name": "角色名", "status": "结尾处境/情绪", "outfit": "结尾服装状态", "goal": "下一步目标"}],
    "open_foreshadows": [{"id": "F1", "desc": "尚未回收的伏笔", "from_episode": 1}]},
  "key_events": ["本集实际发生的关键事件（3-6 条，供下一集禁止重演）"],
  "plot_points": ["本集剧情要点（4-6 条）"],
  "dialogue_highlights": ["本集最具代表性的台词 3-5 句（逐字摘录）"],
  "ending_hook": "本集结尾状态一句话（供下一集开场承接）"
}"""


def extract_episode_state(client, script: dict, prev_state: dict = None, episode_no: int = 1,
                          events: list = None) -> dict:
    """④ 由本集剧本产出 state_in / state_out（时间、地点、主要角色状态、未回收伏笔）"""
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    brief = [{
        "shot_id": s.get("shot_id"),
        "camera": s.get("camera"),
        "location": s.get("location"),
        "description": str(s.get("description") or "")[:80],
        # 画面细节：时间/天气/光源方向常被写在 visual_detail（description 限长后的扩展位），
        # 不带上会让 state 的「时间」项丢失依据（例如「黄昏」只存在于 visual_detail）。
        "visual_detail": str(s.get("visual_detail") or "")[:60],
        "dialogue": str(s.get("dialogue_text") or "")[:80],
        "characters": s.get("characters_in_shot") or [],
        "items": s.get("items_in_shot") or [],
    } for s in shots[:40]]
    chars = [{"name": c.get("name"), "appearance": c.get("appearance"),
              "outfit": c.get("outfit") or c.get("current_outfit"),
              "personality": c.get("personality")}
             for c in (script.get("characters") or [])[:10] if isinstance(c, dict)]
    prev_txt = ""
    if isinstance(prev_state, dict) and prev_state:
        prev_txt = ("【上一集 state_out（本集 state_in 必须承接）】\n"
                    + json.dumps(prev_state.get("state_out") or {}, ensure_ascii=False)
                    + "\n【上一集未回收伏笔】\n"
                    + json.dumps(prev_state.get("open_foreshadows")
                                 or (prev_state.get("state_out") or {}).get("open_foreshadows") or [],
                                 ensure_ascii=False))
    _first_note = ('' if prev_state else
        '\n【重要】本集是全剧第一集（开篇），没有上一集：state_in 必须基于本集分镜内容本身书写（如「本集正午·开篇」），'
        '禁止出现「承接上集」「承接前情」「上集结尾」等字样，不得假设任何前情。')
    prompt = f"""【任务】下面是漫剧第 {episode_no} 集《{script.get('episode_title') or ''}》的分镜脚本。请抽取本集的「时间线锚点」：state_in（开场状态）与 state_out（结尾状态）。{_first_note}
{prev_txt}
【本集角色表】{json.dumps(chars, ensure_ascii=False)}
【本集分镜】{json.dumps(brief, ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象：
{STATE_SCHEMA_HINT}
【硬性约束】1) 只依据分镜内容，不得编造未出现的信息；2) state_in 必须与上一集 state_out 衔接；3) 伏笔要写清 id 与来源集；4) 结尾若仍存在未回收伏笔，必须列在 state_out.open_foreshadows；5) state_in.time / state_out.time 禁止填「未说明」「无」「不确定」，必须给出可比较的时间锚点（原文无明确时间时，用相对锚点，如「紧接上集·当夜」「次日清晨」）；6) state_in.transition 必填：若本集开场地点与上一集结尾地点不同，必须写明移动/转场方式（如「战斗结束后时空回溯，落于雨夜房间」），若相同则写「承接上集同一地点」。"""
    data = _json_call(client, prompt, f"state#{episode_no}", temperature=0.25,
                      max_tokens=7000, events=events)
    si = data.get("state_in") if isinstance(data.get("state_in"), dict) else {}
    so = data.get("state_out") if isinstance(data.get("state_out"), dict) else {}
    for st in (si, so):
        if not isinstance(st.get("character_states"), list):
            st["character_states"] = []
        if not isinstance(st.get("open_foreshadows"), list):
            st["open_foreshadows"] = []
        st["time"] = str(st.get("time") or "").strip()
        st["location"] = str(st.get("location") or "").strip()
        st["transition"] = str(st.get("transition") or "").strip()
    out = {
        "episode_no": int(episode_no),
        "episode_title": script.get("episode_title") or "",
        "state_in": si,
        "state_out": so,
        "key_events": [str(x).strip()[:100] for x in (data.get("key_events") or []) if str(x).strip()][:8],
        "plot_points": [str(x).strip()[:100] for x in (data.get("plot_points") or []) if str(x).strip()][:8],
        "dialogue_highlights": [str(x).strip()[:60] for x in (data.get("dialogue_highlights") or [])
                                if str(x).strip()][:6],
        "ending_hook": str(data.get("ending_hook") or "").strip()[:200],
        "generated_at": _now(),
    }
    return out


# ===================== B⑤ / D⑨ 校验 =====================

def rule_timeline_check(prev_state: dict, cur_state: dict, cur_script: dict = None) -> list:
    """规则化时间线/外观校验（快、稳，作为 LLM 校验的前置筛查）

    cur_script 可选：传入本集剧本后，可判定「上集结尾地点 ≠ 本集开场地点」是否
    在前 3 镜给出了转场/落点交代（D⑨ 确定性规则 location_jump_unexplained）。
    """
    issues = []
    if not isinstance(prev_state, dict) or not isinstance(cur_state, dict):
        return issues
    so = prev_state.get("state_out") or {}
    si = cur_state.get("state_in") or {}
    prev_outfits = {_norm_name(c.get("name")): str(c.get("outfit") or "")
                    for c in (so.get("character_states") or []) if isinstance(c, dict)}
    for c in (si.get("character_states") or []):
        if not isinstance(c, dict):
            continue
        nm = _norm_name(c.get("name"))
        cur_outfit = str(c.get("outfit") or "")
        old = prev_outfits.get(nm) or ""
        if nm and cur_outfit and old:
            c1, c2 = _colors(cur_outfit), _colors(old)
            if c1 and c2 and c1 != c2:
                issues.append({
                    "severity": "high", "category": "角色一致性", "type": "outfit_color_drift",
                    "detail": f"角色「{c.get('name')}」服装配色与上集结尾不一致：上集「{old}」→ 本集「{cur_outfit}」",
                    "evidence": f"上集 state_out.character_states[{c.get('name')}].outfit={old}；"
                                f"本集 state_in.character_states[{c.get('name')}].outfit={cur_outfit}",
                    "fix": "统一服装配色；若剧情确有换装，需在台词或画面中补出换装原因"})
    # 上一集结尾未回收伏笔：本集 state_in 是否承接（未承接仅提示）
    prev_fs = so.get("open_foreshadows") or []
    cur_txt = json.dumps(si, ensure_ascii=False)
    for f in prev_fs:
        if not isinstance(f, dict):
            continue
        key = _plain(str(f.get("desc") or ""))[:6]
        if key and key not in _plain(cur_txt):
            issues.append({
                "severity": "medium", "category": "伏笔回收", "type": "foreshadow_not_carried",
                "detail": f"上集未回收伏笔「{f.get('desc')}」在本集开场状态中未见承接线索",
                "evidence": f"上集 state_out.open_foreshadows={json.dumps(f, ensure_ascii=False)}",
                "fix": "在本集前 3 个镜头补一句呼应，或明确延后并保留伏笔状态"})
    # ---- 时空跳变（确定性）：上集结尾地点 ≠ 本集开场地点，且本集前 3 镜无转场落点交代
    p_loc, c_loc = _norm_name(so.get("location")), _norm_name(si.get("location"))
    if p_loc and c_loc and p_loc != c_loc and p_loc not in c_loc and c_loc not in p_loc:
        head_txt = str(si.get("transition") or "")
        if isinstance(cur_script, dict):
            # 只在「画面层」找转场交代：state_in.transition + 前 3 镜的 description/camera/location
            # （台词里的「回到从前」等不算转场画面，故不纳入匹配，避免误判）
            head_txt += " " + " ".join(
                str(s.get("description") or "") + " " + str(s.get("camera") or "")
                + " " + str(s.get("location") or "")
                for s in [x for x in (cur_script.get("shots") or []) if isinstance(x, dict)][:3])
        if not any(mk in head_txt for mk in TRANSITION_MARKERS):
            issues.append({
                "severity": "medium", "category": "时间地点", "type": "location_jump_unexplained",
                "detail": f"上集结尾在「{so.get('location')}」，本集开场直接切到「{si.get('location')}」，前 3 镜无转场交代",
                "evidence": f"上集 state_out.location={so.get('location')}；本集 state_in.location={si.get('location')}；"
                            f"本集前 3 镜文本={head_txt[:200] or '（缺剧本）'}",
                "fix": "首镜补转场镜头（移动/时空回溯/归途）并注明落点，或改为承接同一地点",
                "shot_ids": [1]})
    # ---- 时间锚点缺失（确定性）：两侧均无可用时间锚点，无法判断时间是否延续
    def _t_missing(t) -> bool:
        t = _plain(str(t or ""))
        return (not t) or t in ("未说明", "无", "不确定", "未知", "未提及", "未标注", "na")
    if _t_missing(so.get("time")) and _t_missing(si.get("time")):
        issues.append({
            "severity": "low", "category": "时间地点", "type": "time_anchor_missing",
            "detail": "上集结尾与本集开场均无可用时间锚点，无法判断时间是否延续",
            "evidence": f"上集 state_out.time={so.get('time') or '（空）'}；本集 state_in.time={si.get('time') or '（空）'}",
            "fix": "按 STATE_SCHEMA 补相对时间锚点（如「紧接上集·当夜」「次日清晨」）"})
    return issues


def validate_continuity(client, cur_script: dict, prev_script: dict, prev_state: dict,
                        cur_state: dict, episode_no: int, events: list = None,
                        rule_issues: list = None) -> dict:
    """⑤ 时间线锚点校验 + ⑨ 相邻集六类比对，输出问题清单（含 severity / 证据 / 修正建议）"""
    cur_shots = [{"shot_id": s.get("shot_id"), "camera": s.get("camera"),
                  "location": s.get("location"),
                  "description": str(s.get("description") or "")[:90],
                  "visual_detail": str(s.get("visual_detail") or "")[:60],
                  "dialogue": str(s.get("dialogue_text") or "")[:90],
                  "characters": s.get("characters_in_shot") or []}
                 for s in (cur_script.get("shots") or []) if isinstance(s, dict)][:40]
    prev_shots = [{"shot_id": s.get("shot_id"), "location": s.get("location"),
                   "description": str(s.get("description") or "")[:90],
                   "visual_detail": str(s.get("visual_detail") or "")[:60],
                   "dialogue": str(s.get("dialogue_text") or "")[:90],
                   "characters": s.get("characters_in_shot") or []}
                  for s in ((prev_script or {}).get("shots") or []) if isinstance(s, dict)][:40]
    prev_txt = ""
    if prev_state:
        prev_txt = ("【上一集 state_out】" + json.dumps(prev_state.get("state_out") or {}, ensure_ascii=False)
                    + "\n【上一集关键事件（禁止重演）】"
                    + json.dumps(prev_state.get("key_events") or [], ensure_ascii=False))

    prompt = f"""【任务】你是连贯性总监。请对漫剧第 {episode_no} 集与第 {max(1, episode_no - 1)} 集做**相邻集一致性比对**，共六类：角色一致性 / 剧情因果 / 时间地点 / 台词一致性 / 伏笔回收 / 风格统一。
{prev_txt}
【本集 state_in】{json.dumps(cur_state.get('state_in') or {}, ensure_ascii=False)}
【上一集分镜摘要】{json.dumps(prev_shots, ensure_ascii=False)}
【本集分镜摘要】{json.dumps(cur_shots, ensure_ascii=False)}
【判定口径】只把**有明确证据**的问题列为 issue；没有问题的类别 issues 留空数组；问题严重度：
  high = 明显冲突（外观/身份前后矛盾、事件重演、时间倒退、伏笔被误回收）
  medium = 影响观感（台词重复、口吻漂移、地点衔接生硬）
  low = 建议优化
【时空衔接强制口径】若本集开场的时间或地点与上集结尾不同，而本集前 3 镜没有可画出的转场/落点交代（无移动过程、无时空回溯、无字幕/旁白点明时间与地点），必须判为「时间地点」类 medium（观众会明显困惑断裂时判 high），并在 timeline 中记 ok=false；不得因为"本集内部自洽"就忽略该项，也不得据此给满分。
【评分上限】存在任何 high 级问题或 timeline.ok=false 时，score 不得高于 6.0；存在未交代的时空跳变（地点/时间直接硬切）时，score 不得高于 7.0。
【输出要求】严格只输出一个 JSON 对象：
{{"score": 0-10 的连贯性总分,
  "categories": {{"角色一致性": {{"ok": true/false, "issues": [{{"severity": "high|medium|low", "detail": "问题（30 字以内）", "evidence": "证据（引用两边原文片段）", "fix": "修正建议（20 字以内）", "shot_ids": [本集中涉及的镜头号]}}]}},
    "剧情因果": {{"ok": true/false, "issues": []}}, "时间地点": {{"ok": true/false, "issues": []}},
    "台词一致性": {{"ok": true/false, "issues": []}}, "伏笔回收": {{"ok": true/false, "issues": []}},
    "风格统一": {{"ok": true/false, "issues": []}}}},
  "timeline": {{"ok": true/false, "issues": [{{"severity": "high|medium|low", "detail": "本集 state_in 与上集 state_out 的冲突或重演问题", "evidence": "", "fix": "", "shot_ids": []}}]}},
  "rewrite_needed": true/false,
  "rewrite_shot_ids": [需要局部重写的镜头号]
}}
【硬性约束】每个类别最多 3 条 issues；rewrite_needed 仅在存在 high 级问题或 timeline.ok=false 时为 true。"""
    data = _json_call(client, prompt, f"check#{episode_no}", temperature=0.2,
                      max_tokens=3200, events=events)

    cats = {}
    flat = []
    raw_cats = data.get("categories") if isinstance(data.get("categories"), dict) else {}
    for name in CROSS_CHECK_CATEGORIES:
        c = raw_cats.get(name) if isinstance(raw_cats.get(name), dict) else {}
        issues = []
        for it in (c.get("issues") or []):
            if not isinstance(it, dict):
                continue
            row = _norm_issue(it, name, episode_no)
            issues.append(row)
            flat.append(row)
        cats[name] = {"ok": bool(c.get("ok", not issues)), "issues": issues[:3]}

    timeline = data.get("timeline") if isinstance(data.get("timeline"), dict) else {}
    tl_issues = [_norm_issue(it, "时间线锚点", episode_no) for it in (timeline.get("issues") or [])
                 if isinstance(it, dict)]
    for it in (rule_issues or []):
        tl_issues.append(_norm_issue(it, it.get("category") or "时间线锚点", episode_no))
    flat.extend(tl_issues)

    highs = [i for i in flat if i.get("severity") == "high"]
    mids = [i for i in flat if i.get("severity") == "medium"]
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        score = None
    rewrite_ids = []
    for x in (data.get("rewrite_shot_ids") or []):
        try:
            rewrite_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not rewrite_ids:
        for it in highs + tl_issues:
            for sid in (it.get("shot_ids") or []):
                try:
                    rewrite_ids.append(int(sid))
                except (TypeError, ValueError):
                    continue
    # 确定性评分封顶（D 兜底）：模型可能忽略口径仍给高分，此处按硬证据封顶
    #   high 级问题 / timeline 断裂 → ≤6.0；未交代的时空跳变 → ≤7.0
    if score is not None:
        cap = None
        if highs or timeline.get("ok") is False:
            cap = 6.0
        elif any(str(i.get("type") or "") == "location_jump_unexplained" for i in flat):
            cap = 7.0
        if cap is not None and score > cap:
            score = cap
    rewrite_needed = bool(data.get("rewrite_needed")) or bool(highs) or bool(timeline.get("ok") is False)
    # 结论文本化：便于前端/报告直接展示（high=断裂；无 high 且无 medium 且高分=连贯；其余=部分连贯）
    if score is None:
        verdict = "unknown"
    elif highs:
        verdict = "断裂"
    elif score >= 8.5 and not mids:
        verdict = "连贯"
    elif score >= 6.0:
        verdict = "部分连贯"
    else:
        verdict = "断裂"
    result = {
        "episode_no": int(episode_no),
        "prev_episode_no": int(episode_no) - 1,
        "checked_at": _now(),
        "score": score,
        "verdict": verdict,
        "timeline": {"ok": not tl_issues and bool(timeline.get("ok", True)), "issues": tl_issues},
        "categories": cats,
        "issues": flat,
        "issue_stats": {"high": len(highs), "medium": len(mids),
                        "low": len([i for i in flat if i.get("severity") == "low"])},
        "rewrite_needed": bool(rewrite_needed),
        "rewrite_shot_ids": sorted(set(rewrite_ids)),
    }
    return result


def _norm_issue(it: dict, category: str, episode_no: int) -> dict:
    sev = str(it.get("severity") or "medium").lower()
    if sev not in ("high", "medium", "low"):
        sev = "medium"
    ids = []
    for sid in (it.get("shot_ids") or []):
        try:
            ids.append(int(sid))
        except (TypeError, ValueError):
            continue
    return {
        "severity": sev,
        "category": category,
        "detail": str(it.get("detail") or "").strip()[:120],
        "evidence": str(it.get("evidence") or "").strip()[:300],
        "fix": str(it.get("fix") or "").strip()[:120],
        "shot_ids": sorted(set(ids)),
        "episode_no": int(episode_no),
    }


# ===================== D⑨ 局部重写 =====================

def rewrite_shots_for_issues(client, script: dict, issues: list, episode_no: int,
                             continuity_ctx: dict = None, events: list = None,
                             shot_ids: list = None) -> dict:
    """按校验问题清单对本集问题镜头做局部重写（保留其余镜头不变）"""
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    if not shots:
        return {"rewritten_shot_ids": [], "script": script, "error": "本集无镜头"}
    ids = [int(x) for x in (shot_ids or []) if str(x).strip().lstrip("-").isdigit()]
    if not ids:
        ids = sorted({int(x) for it in issues for x in (it.get("shot_ids") or [])
                      if str(x).strip().lstrip("-").isdigit()})
    target = [s for s in shots if int(s.get("shot_id") or 0) in ids] if ids else shots
    if not target:
        target = shots

    ctx = continuity_ctx or {}
    issues_txt = json.dumps([{k: it.get(k) for k in ("severity", "category", "detail", "evidence", "fix")}
                             for it in issues][:12], ensure_ascii=False)
    prompt = f"""【任务】漫剧第 {episode_no} 集的以下镜头存在跨集连贯性问题，请**只重写这些镜头**，其余镜头保持原样。
【问题清单】{issues_txt}
{ctx.get('prev_block') or ''}
{ctx.get('bible_block') or ''}
{ctx.get('contract_block') or ''}
{ctx.get('style_block') or ''}
{ctx.get('camera_block') or ''}
【待重写镜头（原内容）】{json.dumps([{k: s.get(k) for k in
    ('shot_id', 'camera', 'location', 'description', 'visual_detail', 'dialogue', 'emotion',
     'audio_cues', 'characters_in_shot', 'items_in_shot', 'prompt_h3')} for s in target], ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象：
{{"shots": [{{"shot_id": 镜头号（必须与输入一致）, "camera": "景别与运镜（取自运镜术语表）", "location": "场景名",
  "description": "修正后的画面描述（**不设字数上下限**，内容需要多长就写多长；把主体、关键动作分解（用→连接的 2~4 步，如：走到椅前→扶椅背转身→缓缓落座）、外貌衣着、环境与光线、构图与景别都写清楚，不要为凑字数堆砌背景）",
  "visual_detail": "画面补充细节（可选；光源方向/时间天气/动作过程等更细的描写写这里，≤120 字；没有就写空字符串）",
  "dialogue": [{{"speaker": "角色名", "text": "台词"}}],
  "emotion": "情绪", "audio_cues": "音效", "characters_in_shot": ["角色名"], "items_in_shot": ["物品名"],
  "fix_note": "本次修正点（15 字以内）"}}]}}
【不要输出 prompt_h3】视频提示词由程序在生成期按 H3 规范自动构建（结合当次实际参考图生成六段式），
你在这里写的英文描述缺少 <Picture N> 标签，反而会覆盖规范提示词导致出片偏离设定。画面信息写进 description 即可。
【硬性约束】只输出输入镜头号对应的镜头；修正后必须消除问题清单中的冲突（外观统一、不重演、不重复台词、承接上集结尾）。"""
    data = _json_call(client, prompt, f"rewrite#{episode_no}", temperature=0.5,
                      max_tokens=3600, events=events)

    from dialogue_utils import normalize_lines as _dlg_lines
    chars = [c.get("name") for c in (script.get("characters") or []) if isinstance(c, dict)]
    items = [i.get("name") for i in (script.get("items") or []) if isinstance(i, dict)]
    new_rows = data.get("shots") if isinstance(data.get("shots"), list) else []
    if not new_rows:
        return {"rewritten_shot_ids": [], "script": script, "error": "模型未返回重写镜头"}
    by_id = {int(s.get("shot_id") or 0): s for s in shots}
    changed, notes = [], []
    for r in new_rows:
        if not isinstance(r, dict):
            continue
        try:
            sid = int(r.get("shot_id"))
        except (TypeError, ValueError):
            continue
        old = by_id.get(sid)
        if not old:
            continue
        # 注意：**不回写 prompt_h3**。视频提示词由生成期 h3_prompt_kit 按当次参考图规范构建，
        # 这里若是把模型现写的英文描述写回去，会再次出现「薄英文顶掉结构化构建器」的老问题。
        _prev_cam = str(old.get("camera") or "").strip()
        for k in ("camera", "location", "description", "emotion", "audio_cues"):
            if str(r.get(k) or "").strip():
                old[k] = str(r.get(k)).strip()[:400]
        # 审计 P1-9（2026-09-29）：重写只回写 camera 复合串，但下游取景别/运镜的
        # 权威入口优先读 shot_type / camera_motion（comfyui_client.shot_framing、
        # h3_prompt_kit._camera_en/_camera_move_en）—— 不同步刷新的话，重写给出的
        # 新镜头语言会被旧权威字段整个顶掉（D⑨ 重写闭环对镜头语言的修正静默失效）。
        # 归一未命中时 _norm_shot_type 返回空串（宁可留空让下游回退解析**新** camera，
        # 也不保留过期的旧景别），并打枚举漂移告警。
        _new_cam = str(old.get("camera") or "").strip()
        if _new_cam and _new_cam != _prev_cam:
            old["shot_type"] = _norm_shot_type(_new_cam)
            old["camera_motion"] = _norm_camera_motion(_new_cam)
        # ⚠️ description 一旦被重写，旧的 visual_detail 就是**上一条描述的尾巴**，必须同步处理，
        # 否则 build_storyboard_prompt 会把「新描述 + 旧细节」拼成画面主体，自相矛盾
        #（实测场景：重写后新描述写「正午平光」，旧尾巴仍留着「黄昏暖调逆光」）。
        # 规则：模型给了新细节就用新的；没给就清空（新描述本身已承载画面信息）。
        if str(r.get("description") or "").strip():
            old["visual_detail"] = str(r.get("visual_detail") or "").strip()[:400]
        if r.get("dialogue") is not None:
            old["dialogue"] = _dlg_lines(r.get("dialogue"), chars, chars)
            old["dialogue_text"] = " ".join(
                (f"{d['speaker']}：{d['text']}" if d.get("speaker") else d.get("text") or "")
                for d in old["dialogue"]).strip()
        if isinstance(r.get("characters_in_shot"), list) and r["characters_in_shot"]:
            # 审计 P2-12（2026-09-29）：走 asset_name_match 三级匹配（与全库同源），
            # 替代裸相等 —— 旧写法把「古月方源（青年）」这类近名静默丢弃且无告警。
            _rs_chars, _miss_chars, _ = asset_name_match.resolve_names(
                r["characters_in_shot"], chars, "character")
            if _miss_chars:
                logger.warning("重写镜 %s：角色 %s 未命中已登记角色，已忽略（防误配）",
                               sid, _miss_chars)
            old["characters_in_shot"] = _rs_chars or old["characters_in_shot"]
        if isinstance(r.get("items_in_shot"), list):
            # 审计 P2-12：物品同样走统一匹配器 —— 旧实现完全不过滤，模型发明的
            # 未登记物品名直接写回剧本（下游只能靠 no_reference 告警兜底）。
            if r["items_in_shot"]:
                _rs_items, _miss_items, _ = asset_name_match.resolve_names(
                    r["items_in_shot"], items, "item")
                if _miss_items:
                    logger.warning("重写镜 %s：物品 %s 未命中已登记物品，已忽略（防误配）",
                                   sid, _miss_items)
                old["items_in_shot"] = _rs_items or old["items_in_shot"]
            else:
                old["items_in_shot"] = []
        # 审计 P2-11（2026-09-29）：重写可能改长/改短台词与描述，但旧实现不重算时长
        # —— duration 与 duration_overflow_sec 停在旧值，audit_script 读过期字段，
        # 配音溢出不可见。与 _norm_shots 同口径重算（保持四舍五入到 0.5s 与溢出记账）。
        _auto_dur = estimate_shot_duration(old)
        _model_dur = None
        try:
            _model_dur = float(old.get("duration"))
        except (TypeError, ValueError):
            _model_dur = None
        if _model_dur and SHOT_DURATION_MIN <= _model_dur <= SHOT_DURATION_MAX:
            old["duration"] = round(max(_model_dur, _auto_dur) * 2) / 2.0
        else:
            old["duration"] = _auto_dur
        _need = required_shot_duration(old)
        if _need > SHOT_DURATION_MAX:
            old["duration_overflow_sec"] = round(_need - SHOT_DURATION_MAX, 2)
        else:
            old.pop("duration_overflow_sec", None)
        changed.append(sid)
        if r.get("fix_note"):
            notes.append({"shot_id": sid, "fix_note": str(r["fix_note"]).strip()[:40]})
    script["shots"] = shots
    return {"rewritten_shot_ids": sorted(set(changed)), "script": script, "notes": notes}


# ===================== 编排：带连贯性的一集转换 =====================

def _run_parallel_steps(steps: list) -> list:
    """并行执行 [(名称, fn), ...]，全部 join 后按 steps 原顺序返回各 fn() 的结果。

    失败语义与串行版完全一致：join 全部线程后，按 steps 原顺序找到**第一个**抛异常
    的步骤原样 re-raise（串行版即「首个失败中断整步」），其余失败仅记 warning ——
    它们的缓存副作用（各自 ensure_* 的落盘文件）幂等可复用，多写无害。
    各线程 daemon=True：上层异常上抛时不因等待并行步骤而阻塞进程收尾。
    某步线程启动失败（如系统线程耗尽）时该步退回内联执行，整体退化为串行、语义不变。
    """
    results = [None] * len(steps)
    threads = []
    for i, (name, fn) in enumerate(steps):
        def _run(i=i, fn=fn):
            try:
                results[i] = ("ok", fn())
            except Exception as e:  # noqa: BLE001
                results[i] = ("err", e)
        t = threading.Thread(target=_run, name=f"continuity:{name}", daemon=True)
        try:
            t.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("并行步骤 %s 线程启动失败，退回内联执行：%s", name, e)
            _run()
            continue
        threads.append(t)
    for t in threads:
        t.join()
    # 按原顺序找第一个 err：与串行版「首个失败即该次生产失败」的失败语义一致
    first_err = None
    for i, r in enumerate(results):
        if r is None or r[0] != "err":
            continue
        if first_err is None:
            first_err = r[1]
        else:
            logger.warning("并行步骤 %s 失败（非首个失败，不改变主失败语义）：%s",
                           steps[i][0], r[1])
    if first_err is not None:
        raise first_err
    return [r[1] if isinstance(r, tuple) else None for r in results]


def convert_chapter_with_continuity(client, novel_meta: dict, novel_text: str, chapter: dict,
                                    project_key: str, continuity_dir: str, *,
                                    style: str = "3D动漫渲染", target_shots: int = 0,
                                    episode_no: int = 1, save_dir: str = None,
                                    enable_rewrite: bool = True,
                                    progress_cb=None, force_refresh_assets: bool = False,
                                    screenplay_text: str = None) -> dict:
    """A/B/C/D 全流程单集生成：资产加载 → 上下文注入生成 → state 抽取 → 校验 → 必要时局部重写 → 落盘。

    返回：{script, script_path, state, summary, validation, assets, events, elapsed_sec}

    参数：
    - screenplay_text: 文学剧本改写稿（若存在）。用作 coverage 与 script_consistency 的比对基准；
      剧本生成仍以原著 chapter 为准，质检初筛保留原著基准。
    """
    t0 = time.time()
    events = []

    def report(phase, msg, pct, cur=1, tot=1):
        if progress_cb:
            try:
                progress_cb(phase, cur, tot, msg, pct)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    # 原著章节正文（剧本生成、质检初筛基准）
    seg = (novel_text or "")[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
    # 文学剧本改写稿（coverage / script_consistency 基准）：存在时覆盖，为空则回退原著
    ref_text = screenplay_text if (screenplay_text and screenplay_text.strip()) else seg

    # ---- 1) 项目级资产：bible / style_guide / quotes / voice_dict / camera_terms
    bible = load_bible(continuity_dir, project_key)
    report("assets", f"第{episode_no}集：加载项目级设定库与风格配置…", 4)
    # 三件套并行（2026-10-06）：style_guide / voice_dict / quotes 是三次互不依赖的 LLM 调用，
    # 缓存文件两两不相交（ensure_style_guide 只读写 style_guide.json；ensure_voice_dict 只读写
    # voice_dict.json；ensure_quotes_for_episode 只读写 quotes.json；三者都不读写 bible.json
    # —— bible 以内存入参传入，bible.json 的写入只发生在下方 3.1 merge_bible_from_episode），
    # 线程并行把串行 3 次 LLM 往返压成约 1 次往返（网络等待期 GIL 已释放，真并行）。
    # camera_terms 是零 LLM 的内置规则表（可能做并集升级落盘），保持内联零开销。
    # 失败语义：_run_parallel_steps 在全部 join 后按原顺序（style_guide → voice_dict → quotes）
    # 重抛首个异常，与串行版「首个失败即整步失败」一致；其余失败记 warning。
    # events 跨线程 append（list.append 为 GIL 原子）安全，仅条目先后按完成时间交错。
    style_guide, voice_dict, quotes = _run_parallel_steps([
        ("style_guide", lambda: ensure_style_guide(
            client, continuity_dir, project_key,
            {**(bible or {}), "title": novel_meta.get("title") or ""},
            style, episode_no, events=events, force=force_refresh_assets)),
        ("voice_dict", lambda: ensure_voice_dict(
            client, continuity_dir, project_key, bible, episode_no, events=events)),
        ("quotes", lambda: ensure_quotes_for_episode(
            client, continuity_dir, project_key, seg, episode_no, events=events)),
    ])
    camera_terms = ensure_camera_terms(continuity_dir, project_key)

    # ---- 2) 组装上下文并生成剧本（注入 ①②③⑥⑦⑧）
    ctx = build_continuity_context(continuity_dir, project_key, episode_no, style, quotes=quotes)
    report("context", f"第{episode_no}集：注入上集摘要卡 / 设定库 / 衔接契约 / 金句清单…", 8)

    script = convert_chapter_to_script(
        client, novel_meta, novel_text, chapter, style=style, target_shots=target_shots,
        episode_no=episode_no, progress_cb=progress_cb, continuity_ctx=ctx,
        # 断点缓存：pipeline 整步重试时跳过已完成的块（网关偶发挂起的主要止血点）
        cache_dir=shots_cache_dir(continuity_dir, project_key, episode_no),
    )

    # ---- 3) 服装状态 × 镜头对齐（A① 补齐：并入设定库前先消解本集服装漂移 / 登记合法换装）
    outfit_reconcile = reconcile_outfit_with_shots(script, bible, int(episode_no))
    if outfit_reconcile.get("fixed_count") or outfit_reconcile.get("change_count"):
        report("assets", f"第{episode_no}集：镜头服装对齐（改写 {outfit_reconcile.get('fixed_count')} 处、"
                         f"合法换装 {outfit_reconcile.get('change_count')} 处）", 88)

    # ---- 3.1) bible 并入项目级设定库（A① 持久化复用）
    merged = merge_bible_from_episode(continuity_dir, project_key, script, episode_no,
                                      episode_bible={"characters": script.get("characters"),
                                                     "items": script.get("items"),
                                                     "scenes": script.get("scenes")})
    bible = merged["bible"]
    # 口吻词典随新角色增量补齐
    voice_dict = ensure_voice_dict(client, continuity_dir, project_key, bible, episode_no, events=events)

    # ---- 4) 角色字段强制对齐项目级锁定值（A①：别名归一 + 外观/服装回写，确定性不依赖模型）
    alignment = align_script_assets(script, bible, int(episode_no))
    alignment["outfit_reconcile"] = outfit_reconcile
    if alignment.get("aliases") or alignment.get("outfit_fixed") or alignment.get("appearance_fixed"):
        report("assets", f"第{episode_no}集：资产对齐（改名 {len(alignment.get('aliases') or [])}、"
                         f"服装归一 {len(alignment.get('outfit_fixed') or [])}、"
                         f"外观归一 {len(alignment.get('appearance_fixed') or [])}）", 89)

    # ---- 4.5) 覆盖率校验后台并行启动（2026-10-06 改造二；在 7.5 收敛）
    # 原串行顺序：state 抽取 → 校验 → 局部重写 → coverage。coverage 是全程最长的静默环节
    # （2026-10-06 第2集静默 34 分钟实录），遂在「资产对齐完成」时点提前后台启动，与主线程
    # 的 state 抽取 / 校验全程重叠，在「局部重写轮之前」join 收结果（见 7.5）。
    # run_coverage_check 的全部实参原样搬入 worker（progress_cb / events 原样传入；结果与
    # 异常装进 dict 盒子）。
    # join 点在重写之前：镜头表 script["shots"] 任何时刻只有一个写方 —— coverage 的判定 /
    # 补生成（rebind shots）/ 复检全部在 join 前收敛完毕，对 shots 的重写只发生在 join 后，
    # 无并发写冲突；仅当重写被触发时（少数路径）主线程需在 7.5 等 coverage 完成，
    # coverage 与 state 抽取 / 校验的公共路径重叠收益不受影响。
    # 线程内不加 should_stop/取消检查：与现状 run_coverage_check 内部一致（取消信号仍在
    # 主线程各 LLM 调用点生效；主线程取消/失败时，daemon 线程不强行打断，后台收敛完
    # 当前链路后自然结束）。线程内也不新增文件写入面：只写「第N集_覆盖率.json」与
    # script["metadata"]["coverage"]，与主线程在本窗口的写入（quotes.json / state 字段）互不相交。
    coverage_box = {"report": None, "error": None}

    def _coverage_worker():
        try:
            coverage_box["report"] = coverage_mod.run_coverage_check(
                client, ref_text, script, episode_no=int(episode_no), threshold=None,
                max_rounds=COVERAGE_MAX_ROUNDS, events=events,
                detail_threshold=None,
                continuity_dir=continuity_dir, project_key=project_key, save=True,
                # 2026-10-06：coverage 全程此前零进度回报（托管停滞指标在该环节恒等于全程时长，
                # 「卡住 vs 慢」无法区分，2026-10-06 第2集静默 34 分钟实录）——把初检/补生成/
                # 复检节点透传给流水线进度条。
                progress_cb=lambda msg: report("coverage", msg, 97))
        except Exception as e:  # noqa: BLE001
            coverage_box["error"] = e

    report("coverage", f"第{episode_no}集：原文覆盖率校验（逐句核对是否被镜头承载）…", 97)
    coverage_thread = threading.Thread(target=_coverage_worker,
                                       name=f"continuity:coverage-ep{int(episode_no)}",
                                       daemon=True)
    try:
        coverage_thread.start()
    except Exception as e:  # noqa: BLE001
        # 线程启动失败（如系统线程耗尽）：退回内联执行，行为与串行版等价
        logger.warning("第 %s 集：coverage 后台线程启动失败，退回内联执行：%s", episode_no, e)
        _coverage_worker()
        coverage_thread = None

    # ---- 5) state_in / state_out（B④）
    report("state", f"第{episode_no}集：抽取时间线锚点 state_in / state_out…", 90)
    prev_state = load_state(continuity_dir, project_key, int(episode_no) - 1)
    state = extract_episode_state(client, script, prev_state, episode_no, events=events)
    state = align_state_with_bible(state, bible, int(episode_no))
    script["state_in"] = state["state_in"]
    script["state_out"] = state["state_out"]

    # ---- 6) 校验（B⑤ + D⑨）
    prev_script = None
    if prev_state:
        prev_script = _load_prev_script(save_dir, project_key, int(episode_no) - 1)
    rule_issues = rule_timeline_check(prev_state, state, script) if prev_state else []
    report("validate", f"第{episode_no}集：时间线锚点校验 + 相邻集六类比对…", 93)
    if prev_state:
        validation = validate_continuity(client, script, prev_script, prev_state, state,
                                         episode_no, events=events, rule_issues=rule_issues)
    else:
        # 无上集锚点（首集，或上集 state 缺失）时不做相邻集比对：
        # 显式标记 no_prev，避免"无对象可比"被模型误判为满分（旧实现会给出 10 分）
        validation = {
            "episode_no": int(episode_no), "prev_episode_no": int(episode_no) - 1,
            "checked_at": _now(), "score": None, "verdict": "no_prev",
            "note": "无上集 state/剧本，跳过相邻集六类比对（首集或上集数据缺失）",
            "timeline": {"ok": True, "issues": []}, "categories": {},
            "issues": [], "issue_stats": {"high": 0, "medium": 0, "low": 0},
            "rewrite_needed": False, "rewrite_shot_ids": [],
        }

    # ---- 6.5) 集内台词近似重复检测（C⑥/D⑨ 确定性规则，命中按 high 级触发局部重写）
    merge_dedup_issues(validation, script, int(episode_no))

    # ---- 6.6) P0-2 全局一致性契约 + 跨镜头校验（借鉴 penshot；纯确定性软告警）
    # ⚠️ 只把 low 级 issue 追加进 validation 桶 + 更新 issue_stats["low"]，**不动**
    #    rewrite_needed / rewrite_shot_ids —— 角色 gap / 场景跳切不是改单镜能修的，
    #    进 LLM 局部重写反而可能改坏；这里是「可见地报告、由人/下游决策」的软告警。
    cc_contract.merge_contract_issues(validation, script, bible, int(episode_no))

    # ---- 7) 金句落位校验（C⑦）
    quote_check = check_quotes_in_script(continuity_dir, project_key, script, episode_no)
    validation["quotes"] = quote_check
    if quote_check.get("missed"):
        validation["issues"].append({
            "severity": "medium", "category": "风格统一", "episode_no": int(episode_no),
            "detail": f"金句清单中 {len(quote_check['missed'])} 句未出现在本集台词中",
            "evidence": "；".join(quote_check["missed"])[:300],
            "fix": "把缺失金句自然嵌入对应角色台词", "shot_ids": [],
        })
        validation["issue_stats"]["medium"] = int(validation["issue_stats"].get("medium") or 0) + 1
        validation["rewrite_needed"] = validation.get("rewrite_needed") or True

    # ---- 7.5) 收敛覆盖率线程（计算已在 4.5 提前并行；join 点刻意放在局部重写轮之前）
    # worker 异常原样 re-raise —— 与串行版「coverage 失败 = 整步失败」语义一致。
    # join 后才进入重写轮：script["shots"] 任何时刻只有一个写方 —— coverage 的判定 /
    # 补生成（rebind shots）/ 复检全部在 join 前收敛完毕，对 shots 的重写只发生在 join 后，
    # 无并发写冲突；仅当重写被触发时（少数路径）主线程需在此等 coverage 完成，
    # coverage 与 state 抽取 / 校验的公共路径重叠收益不受影响。
    if coverage_thread is not None:
        coverage_thread.join()
    if coverage_box["error"] is not None:
        raise coverage_box["error"]
    coverage_report = coverage_box["report"]

    # ---- 8) 局部重写（D⑨：high 级问题 / 时间线冲突 / 金句缺失）
    rewrite_info = {"triggered": False, "rounds": 0, "rewritten_shot_ids": [], "notes": []}
    if enable_rewrite and validation.get("rewrite_needed"):
        rounds = 0
        while rounds < MAX_REWRITE_ROUNDS and validation.get("rewrite_needed"):
            rounds += 1
            report("rewrite", f"第{episode_no}集：命中连贯性问题，触发局部重写（第 {rounds} 轮）…", 95)
            issues = [i for i in validation.get("issues") or []
                      if i.get("severity") in ("high", "medium")]
            if not issues:
                break
            rw = rewrite_shots_for_issues(client, script, issues, episode_no,
                                          continuity_ctx=ctx, events=events,
                                          shot_ids=validation.get("rewrite_shot_ids"))
            if not rw.get("rewritten_shot_ids"):
                rewrite_info["error"] = rw.get("error") or "未产生修改"
                break
            rewrite_info["triggered"] = True
            rewrite_info["rewritten_shot_ids"] = rw["rewritten_shot_ids"]
            rewrite_info["notes"] = rw.get("notes") or []
            # 重写后重算 state 与校验，形成闭环证据
            state = extract_episode_state(client, script, prev_state, episode_no, events=events)
            state = align_state_with_bible(state, bible, int(episode_no))
            script["state_in"] = state["state_in"]
            script["state_out"] = state["state_out"]
            rule_issues = rule_timeline_check(prev_state, state, script) if prev_state else []
            validation = validate_continuity(client, script, prev_script, prev_state, state,
                                             episode_no, events=events, rule_issues=rule_issues)
            validation["quotes"] = check_quotes_in_script(continuity_dir, project_key, script, episode_no)
            merge_dedup_issues(validation, script, int(episode_no))
            # P0-2 契约软告警随重写后的新 shots 重新扫一遍（同样只追加 low、不动重写闸门）。
            cc_contract.merge_contract_issues(validation, script, bible, int(episode_no))
        rewrite_info["rounds"] = rounds

    # ---- 8.5) 覆盖率结果汇总与后处理（coverage 计算已在 4.5 提前并行、7.5 收敛）
    # 此处按原顺序接回 coverage 之后的全部既有逻辑（report 汇总、supplement_shots 判断、
    # 补生成后 state 重抽、consistency），逻辑一字未改；coverage_report 已在 7.5 从盒子取回。
    report("coverage", f"第{episode_no}集：情节级覆盖 {coverage_report.get('plot_coverage_percent')}%"
                       f"（{coverage_report.get('plot_covered_count')}/{coverage_report.get('plot_unit_count')} 单元）、"
                       f"细节级覆盖 {coverage_report.get('detail_coverage_percent')}%，"
                       f"遗漏 {coverage_report.get('missing_count')} 条、补生成 "
                       f"{coverage_report.get('supplement_shots')} 镜（{coverage_report.get('supplement_rounds')} 轮）", 98)
    if coverage_report.get("supplement_shots"):
        # 补生成镜头后：重做资产对齐与 state 抽取，保证新增镜头与项目设定库一致
        # 加固：该刷新失败不得阻断整集落盘（覆盖率校验已完成，剧本必须写出）
        try:
            alignment = align_script_assets(script, bible, int(episode_no))
            alignment["outfit_reconcile"] = outfit_reconcile
            state = extract_episode_state(client, script, prev_state, episode_no, events=events)
            state = align_state_with_bible(state, bible, int(episode_no))
            script["state_in"] = state["state_in"]
            script["state_out"] = state["state_out"]
        except Exception as e:  # noqa: BLE001
            note = (f"补生成后重抽 state 失败，保留补生成前 state："
                    f"{type(e).__name__}: {str(e)[:200]}")
            logger.warning(f"第 {episode_no} 集：{note}")
            events.append({"label": "coverage-state-refresh", "attempt": 0, "max_tokens": 0,
                           "finish_reason": "error", "note": note})
            meta_warn = script.setdefault("metadata", {}).setdefault("warnings", [])
            if isinstance(meta_warn, list):
                meta_warn.append(note)
        report("coverage", f"第{episode_no}集：覆盖率不足，已自动补生成 "
                           f"{coverage_report.get('supplement_shots')} 镜，"
                           f"复检情节级 {coverage_report.get('plot_coverage_percent')}% / "
                           f"细节级 {coverage_report.get('detail_coverage_percent')}%", 98)

    # ---- 8.6) 剧本↔原著一致性校验（P0-3：章节锚定 / 元信息泄漏 / 要素覆盖 + 定向修复闭环）
    #      泄漏走局部重写、要素缺失走定向补生成（只增不删），绝不整集重生成；
    #      章节锚定偏差根因在章节切分（P0-1 面），只记告警、不自动改。
    #      coverage 与 script_consistency 的比对基准改为文学剧本改写稿（ref_text）；
    #      剧本生成、质检初筛仍以原著 seg 为准。
    consistency_summary = {}
    try:
        import script_consistency as sc_mod
        consistency_report = sc_mod.run_script_consistency_check(
            client, script, novel_meta=novel_meta, chapter_text=ref_text, chapter=chapter,
            episode_no=int(episode_no), auto_fix=bool(enable_rewrite), events=events,
            continuity_dir=continuity_dir, project_key=project_key, save=True,
            continuity_ctx=ctx)
        consistency_summary = sc_mod.summary_for_meta(
            consistency_report, consistency_report.get("report_path"))
        report("consistency",
               f"第{episode_no}集：剧本一致性校验——元信息泄漏 "
               f"{(consistency_report.get('leak') or {}).get('hit_count')} 处、要素覆盖率 "
               f"{(consistency_report.get('elements') or {}).get('coverage_percent')}%、"
               f"定向修复 {consistency_report.get('fix_rounds')} 轮"
               f"（{'已修复' if consistency_report.get('fixed') else '留告警'}）", 98.5)
        if consistency_report.get("fix_rounds"):
            # 修复改动了镜头：重做资产对齐与 state 抽取（失败不阻断整集落盘）
            try:
                alignment = align_script_assets(script, bible, int(episode_no))
                alignment["outfit_reconcile"] = outfit_reconcile
                state = extract_episode_state(client, script, prev_state, episode_no, events=events)
                state = align_state_with_bible(state, bible, int(episode_no))
                script["state_in"] = state["state_in"]
                script["state_out"] = state["state_out"]
            except Exception as e:  # noqa: BLE001
                note = (f"一致性修复后重抽 state 失败，保留修复前 state："
                        f"{type(e).__name__}: {str(e)[:200]}")
                logger.warning(f"第 {episode_no} 集：{note}")
                events.append({"label": "consistency-state-refresh", "attempt": 0,
                               "max_tokens": 0, "finish_reason": "error", "note": note})
                meta_warn = script.setdefault("metadata", {}).setdefault("warnings", [])
                if isinstance(meta_warn, list):
                    meta_warn.append(note)
    except Exception as e:  # noqa: BLE001
        note = (f"剧本一致性校验失败（已跳过，剧本仍按原结果落盘）："
                f"{type(e).__name__}: {str(e)[:200]}")
        logger.warning(f"第 {episode_no} 集：{note}")
        meta_warn = script.setdefault("metadata", {}).setdefault("warnings", [])
        if isinstance(meta_warn, list):
            meta_warn.append(note)

    # ---- 9) 落盘：剧本 + state + 摘要卡 + 校验
    meta = script.setdefault("metadata", {})
    meta["continuity"] = {
        "version": CONTINUITY_VERSION,
        "project_key": project_key,
        "episode_no": int(episode_no),
        "prev_episode_no": ctx.get("prev_episode_no"),
        "bible_characters": len(bible.get("characters") or []),
        "bible_items": len(bible.get("items") or []),
        "bible_scenes": len(bible.get("scenes") or []),
        "style_guide_locked": bool((ctx.get("style_guide") or {}).get("style_guide")),
        "quotes_required": [q.get("text") for q in quotes if isinstance(q, dict)],
        "bible_added": merged.get("added"),
        "bible_conflicts": merged.get("conflicts"),
        "asset_alignment": alignment,
        "validation_score": validation.get("score"),
        "issue_stats": validation.get("issue_stats"),
        "rewrite": rewrite_info,
        "coverage": coverage_mod.summary_for_meta(coverage_report, coverage_report.get("report_path")),
        "consistency": consistency_summary,
        "truncation_events": events,
    }
    script["continuity"] = {
        "state_in": state["state_in"],
        "state_out": state["state_out"],
        "key_events": state.get("key_events") or [],
        "plot_points": state.get("plot_points") or [],
        "open_foreshadows": (state.get("state_out") or {}).get("open_foreshadows") or [],
    }
    script_path = None
    if save_dir:
        script_path = save_episode_script(script, save_dir, project_key, episode_no, project_key)

    summary = build_summary_card(script, state, episode_no)
    save_state(continuity_dir, project_key, state)
    save_json(summary_path(continuity_dir, project_key, episode_no), summary)
    validation["asset_alignment"] = alignment
    validation["rewrite"] = rewrite_info
    validation["generated_at"] = _now()
    save_json(validation_path(continuity_dir, project_key, episode_no), validation)
    if script_path:
        save_episode_script(script, save_dir, project_key, episode_no, project_key)

    report("done", f"第{episode_no}集完成：连贯性评分 "
                   f"{validation.get('score') if validation.get('score') is not None else '—'}，"
                   f"原文覆盖率 情节级 {coverage_report.get('plot_coverage_percent')}% / "
                   f"细节级 {coverage_report.get('detail_coverage_percent')}%"
                   f"（{'达标' if coverage_report.get('passed') else '未达标'}，阈值 "
                   f"{coverage_report.get('threshold_percent')}%，遗漏 "
                   f"{coverage_report.get('missing_count')} 条），"
                   f"问题 {len(validation.get('issues') or [])} 条"
                   + (f"，局部重写 {len(rewrite_info['rewritten_shot_ids'])} 镜" if rewrite_info["triggered"] else ""),
           100)
    return {
        "script": script, "script_path": script_path, "state": state, "summary": summary,
        "validation": validation, "assets": {"bible": bible, "style_guide": style_guide,
                                             "voice_dict": voice_dict, "camera_terms": camera_terms,
                                             "quotes": quotes},
        "events": events, "rewrite": rewrite_info, "coverage": coverage_report,
        "elapsed_sec": round(time.time() - t0, 1),
    }


def _load_prev_script(save_dir: str, project_key: str, prev_episode_no: int):
    if not save_dir or prev_episode_no < 1:
        return None
    path = os.path.join(os.path.abspath(save_dir), _safe(project_key), f"第{int(prev_episode_no)}集.json")
    return load_json(path, None)


# ===================== 前端/API 视图 =====================

def continuity_overview(continuity_dir: str, project_key: str) -> dict:
    """项目级连贯性总览（前端可查：bible / style_guide / quotes / voice_dict / camera_terms / 各集 state 与校验）"""
    root = continuity_root(continuity_dir, project_key)
    bible = load_bible(continuity_dir, project_key)
    style_guide = load_style_guide(continuity_dir, project_key)
    quotes = load_quotes(continuity_dir, project_key)
    voice_dict = load_voice_dict(continuity_dir, project_key)
    camera_terms = load_camera_terms(continuity_dir, project_key)
    eps = []
    ep_dir = _ep_dir(continuity_dir, project_key)
    if os.path.isdir(ep_dir):
        for fn in sorted(os.listdir(ep_dir)):
            m = re.match(r"^第(\d+)集_(state|summary|校验|覆盖率)\.json$", fn)
            if not m:
                continue
            eps.append({"episode_no": int(m.group(1)), "kind": m.group(2),
                        "path": os.path.abspath(os.path.join(ep_dir, fn))})
    return {
        "project_key": project_key,
        "root": root,
        "bible": bible,
        "style_guide": style_guide,
        "quotes": quotes,
        "voice_dict": voice_dict,
        "camera_terms": camera_terms,
        "episode_files": eps,
        "counts": {
            "characters": len(bible.get("characters") or []),
            "items": len(bible.get("items") or []),
            "scenes": len(bible.get("scenes") or []),
            "quotes": len(quotes.get("quotes") or []),
            "voices": len(voice_dict.get("characters") or []),
            "episodes": len({e["episode_no"] for e in eps}),
            "coverage_reports": len([e for e in eps if e["kind"] == "覆盖率"]),
        },
    }


def load_camera_terms(continuity_dir: str, project_key: str) -> dict:
    data = load_json(camera_terms_path(continuity_dir, project_key), None)
    return data if isinstance(data, dict) and data.get("景别") else dict(CAMERA_TERMS)


def episode_continuity_view(continuity_dir: str, project_key: str, episode_no: int) -> dict:
    """单集连贯性视图（前端可查：上集摘要卡 / 本集 state_in·state_out / 校验结果 / 原文覆盖率）"""
    ep = int(episode_no)
    return {
        "project_key": project_key,
        "episode_no": ep,
        "state": load_state(continuity_dir, project_key, ep),
        "state_path": os.path.abspath(state_path(continuity_dir, project_key, ep)),
        "summary_card": load_summary_card(continuity_dir, project_key, ep),
        "summary_path": os.path.abspath(summary_path(continuity_dir, project_key, ep)),
        "prev_summary_card": load_summary_card(continuity_dir, project_key, ep - 1),
        "validation": _read_optional(
            validation_path(continuity_dir, project_key, ep), "连贯性校验结果"),
        "validation_path": os.path.abspath(validation_path(continuity_dir, project_key, ep)),
        # ④⑤ 原文覆盖率摘要（字段已规整：覆盖率/阈值/达标/遗漏预览/补生成/复检轨迹，前端可直读）
        "coverage": coverage_mod.summary_for_meta(
            coverage_mod.load_coverage_report(continuity_dir, project_key, ep)) or None,
        "coverage_path": os.path.abspath(
            coverage_mod.coverage_report_path(continuity_dir, project_key, ep)),
    }


def episode_coverage_view(continuity_dir: str, project_key: str, episode_no: int) -> dict:
    """单集原文覆盖率视图（⑤：前端可查看覆盖率百分比、阈值、遗漏清单与补生成记录）"""
    ep = int(episode_no)
    report = coverage_mod.load_coverage_report(continuity_dir, project_key, ep)
    if not report:
        return {"project_key": project_key, "episode_no": ep, "available": False,
                "coverage_path": os.path.abspath(
                    coverage_mod.coverage_report_path(continuity_dir, project_key, ep)),
                "note": "该集尚无覆盖率校验报告（未按新流程重跑，或报告文件缺失）"}
    return {
        "project_key": project_key,
        "episode_no": ep,
        "available": True,
        "coverage_path": os.path.abspath(
            coverage_mod.coverage_report_path(continuity_dir, project_key, ep)),
        "summary": coverage_mod.summary_for_meta(report),
        "missing": report.get("missing") or [],
        "supplement": report.get("supplement") or {},
        "rounds": report.get("rounds") or [],
        "checked_at": report.get("checked_at"),
    }
