# -*- coding: utf-8 -*-
"""项目级文本规则（A2 角色名词表 + L2 发音替换表）。

两份**人工维护**的映射，按项目存放在 ``<output>/project_rules/<项目>.json``：

    {
      "aliases":   {"赵天监": "赵天霸", "赵安": "赵天霸"},
      "pronounce": {"重创": "崇创", "行长": "杭长"}
    }

* **aliases（A2 角色名词表）**：把「同一角色的不同写法」收敛到 bible 标准名。
  ``asset_name_match`` 的三级匹配（精确→归一化→唯一子串）只能处理标点/后缀差异，
  处理不了「赵天监」这类**完全不同的字符串**。本表在**读剧本时**把镜头里的
  角色/物品/说话人/场景名统一改写为标准名，避免同一个人在下游被当成两个角色
  （角色长相漂移、参考图各绑一半）。同时用于 **TTS 前**的台词文本替换。
* **pronounce（L2 发音替换表）**：Qwen3-TTS（FB_Qwen3TTSCustomVoice）**不支持
  SSML / 发音词典**，只能做**文本级**替换——把容易读错的多音字/生僻字/专名
  改写成「同音且不歧义」的写法（如「重创」→「崇创」）。**只影响送去 TTS 的朗读
  文本**，不改字幕、不改剧本、不进画面提示词。

设计约束（与项目既有纪律一致）：
* **fail-open**：文件缺失 / 损坏 / 类型不对 → 返回空表，行为与改动前**逐字一致**；
* **零依赖**：本模块不 import config / flask / 其它 app 模块（叶子模块，可被
  任意层安全引用）；项目根按需从 config 延迟取；
* **不改磁盘剧本**：只在**读入内存后**归一，磁盘上的剧本保持原样（可回滚）。
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

#: 规则目录名（位于 PROJECT_OUTPUT_DIR 下）
RULES_DIRNAME = "project_rules"

_EMPTY = {"aliases": {}, "pronounce": {}}


def _safe(project: str) -> str:
    """项目名 → 安全文件名段（与项目其它模块的保守口径一致：剔除路径分隔符等）。"""
    s = "".join(ch for ch in str(project or "").strip() if ch not in '\\/:*?"<>|')
    return s.strip(" .") or "_default"


def rules_path(project: str, *, root: str | None = None) -> str:
    """规则文件绝对路径（不判断存在性）。``root`` 缺省取 PROJECT_OUTPUT_DIR。"""
    if root is None:
        try:
            from config import PROJECT_OUTPUT_DIR as root  # noqa: PLC0415 延迟导入（叶子模块）
        except Exception:  # noqa: BLE001
            root = ""
    return os.path.join(root or "", RULES_DIRNAME, _safe(project) + ".json")


def _norm_map(raw) -> dict:
    """把任意来源的映射规范成 {str: str}：去空白、丢空值、丢「自己映射自己」。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        _k = str(k or "").strip()
        _v = str(v or "").strip()
        if _k and _v and _k != _v:
            out[_k] = _v
    return out


def load(project: str, *, root: str | None = None) -> dict:
    """读项目规则；任何异常一律返回空表（fail-open，调用方无需 try）。"""
    path = rules_path(project, root=root)
    try:
        if not path or not os.path.isfile(path):
            return dict(_EMPTY)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return dict(_EMPTY)
        return {"aliases": _norm_map(data.get("aliases")),
                "pronounce": _norm_map(data.get("pronounce"))}
    except Exception as e:  # noqa: BLE001
        logger.warning("项目文本规则读取失败（按空表处理）：%s：%s", path, e)
        return dict(_EMPTY)


def save(project: str, data: dict, *, root: str | None = None) -> str:
    """写项目规则（原子替换）。返回落盘路径；失败抛异常（写入口需要明确失败）。"""
    path = rules_path(project, root=root)
    payload = {"aliases": _norm_map((data or {}).get("aliases")),
               "pronounce": _norm_map((data or {}).get("pronounce"))}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ===================== A2：名字归一 =====================

def canon_name(name: str, aliases: dict) -> str:
    """单个名字按别名表归一（精确匹配；命中不到原样返回）。"""
    if not aliases or not name:
        return name
    s = str(name).strip()
    return aliases.get(s, name)


def _ordered(aliases: dict) -> list:
    """长键优先（避免「赵天」先于「赵天霸」被替换，造成半截替换）。"""
    return sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True)


def canon_text(text: str, aliases: dict) -> str:
    """自由文本里的别名替换（长键优先；不做模糊/子串之外的猜测）。"""
    if not aliases or not text:
        return text
    out = str(text)
    for k, v in _ordered(aliases):
        if k in out:
            out = out.replace(k, v)
    return out


def canon_script(script: dict, aliases: dict) -> int:
    """**就地**把剧本里的角色/物品名归一，返回改写次数（0 = 无变化）。

    覆盖面（与下游真正消费名字的字段一致）：
      · 顶层 ``characters[].name`` / ``items[].name``（bible 名）
      · 每镜 ``dialogue[].speaker`` / ``characters_in_shot`` / ``items_in_shot``
        / ``items_state`` 的键 / ``blocking[].name`` / ``location``
    ⚠️ 刻意**不改场景名**（``scenes[].name``）：场景一旦改名，历史分镜图/3D 基准
    目录全部对不上，代价远大于收益；确需改场景请走项目重命名流程。
    """
    if not aliases or not isinstance(script, dict):
        return 0
    n = 0

    def _nm(v):
        nonlocal n
        if isinstance(v, str) and v.strip():
            new = aliases.get(v.strip())
            if new and new != v:
                n += 1
                return new
        return v

    for key in ("characters", "items"):
        for row in script.get(key) or []:
            if isinstance(row, dict) and row.get("name"):
                row["name"] = _nm(row["name"])

    for shot in script.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        for d in shot.get("dialogue") or []:
            if isinstance(d, dict) and d.get("speaker"):
                d["speaker"] = _nm(d["speaker"])
        for fld in ("characters_in_shot", "items_in_shot"):
            lst = shot.get(fld)
            if isinstance(lst, list):
                new_lst = []
                for x in lst:
                    y = _nm(x)
                    if y not in new_lst:
                        new_lst.append(y)
                if new_lst != lst:
                    n += 1
                    shot[fld] = new_lst
        st = shot.get("items_state")
        if isinstance(st, dict) and st:
            new_st, changed = {}, False
            for k, v in st.items():
                k2 = _nm(k)
                changed = changed or (k2 != k)
                new_st[k2] = v
            if changed:
                n += 1
                shot["items_state"] = new_st
        # 旧剧本的纯文本台词（无结构化 dialogue 时 TTS 会用它）同样要替换别名
        if isinstance(shot.get("dialogue_text"), str) and shot["dialogue_text"]:
            _dt = canon_text(shot["dialogue_text"], aliases)
            if _dt != shot["dialogue_text"]:
                n += 1
                shot["dialogue_text"] = _dt
        for b in shot.get("blocking") or []:
            if isinstance(b, dict) and b.get("name"):
                b["name"] = _nm(b["name"])
        if shot.get("location"):
            shot["location"] = _nm(shot["location"])
    return n


# ===================== L2：发音（TTS 前文本替换） =====================

def speak(text: str, pronounce: dict, aliases: dict | None = None) -> str:
    """把「要念出来的文本」改写成 TTS 友好的写法（别名 → 发音表，长键优先）。

    只用于**送去 TTS 的文本**：字幕、剧本、画面提示词都不受影响。
    """
    if not text:
        return text
    out = canon_text(text, aliases or {})
    if not pronounce:
        return out
    for k, v in _ordered(pronounce):
        if k in out:
            out = out.replace(k, v)
    return out


def describe(project: str, *, root: str | None = None) -> dict:
    """给接口/日志用的摘要（不返回整表以外的敏感信息，规则本身即用户数据）。"""
    data = load(project, root=root)
    return {"project": project,
            "path": rules_path(project, root=root),
            "aliases": data["aliases"],
            "pronounce": data["pronounce"],
            "alias_count": len(data["aliases"]),
            "pronounce_count": len(data["pronounce"])}
