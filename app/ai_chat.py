"""
AI 对话（创作总控）：多轮对话敲定漫剧创作设定，并把确认结果结构化落盘为项目配置

- 会话历史：内存中按请求读写 + 落盘 output/ai_chat/chat_history.json
- 创作设定：output/ai_chat/project_settings.json（按项目名归档，含 active 标记）
- 模型调用：「AI 设置 · 对话总控模型」的独立 base_url / api_key / model（ai_config.json → modules.chat）
- 与文本分析 / 质检模型完全独立：对话链路只读 chat 模块，不受另两个模块影响
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# =====================================================================
# 并发保护（审计 S8）
# ---------------------------------------------------------------------
# 会话历史/项目设定都是「load → 改内存 → save」的读改写，此前**零锁**。
# 典型症状：AI 对话连发两条消息，两条都基于同一份旧历史 → 后写的把前一条覆盖掉
# （用户看到「我发的消息只留下最后一条」）；并且 `_write_json` 曾用固定 `.tmp` 名，
# 并发写者互相截断对方写了一半的临时文件 → `os.replace` 发布出损坏 JSON。
# =====================================================================

_CHAT_LOCK = threading.RLock()


def _locked(fn):
    """把读改写放回同一把可重入锁的临界区（函数级装饰，签名/文档不变）"""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _CHAT_LOCK:
            return fn(*args, **kwargs)
    return _wrapper

#: 热窗口上限（**不是「丢弃上限」**）：chat_history.json 每个项目桶只保留最近 40 条，
#: 保证读路径（load_history）耗时/内存不随总量增长。**全量留存在归档**
#: （archive/<canonical_key>/<YYYY-MM-DD>.jsonl，只增不减）——「不丢」由归档保证，
#: 不是由本上限保证。**本数值不可调大**（硬约束 C1；见 save_history 归档逻辑）。
HISTORY_MAX_MESSAGES = 40      # 热窗口上限：每项目桶保留最近 40 条
CONTEXT_MESSAGES = 20          # 每次请求送入模型的最大历史消息条数（与归档完全无关）
MAX_CHARS_PER_MESSAGE = 4000   # 单条消息送入模型时的截断长度

# 创作设定字段（顺序即前端展示顺序）；options 为 AI 可主动给出的候选项
SETTING_FIELDS = [
    {"key": "title", "label": "剧名 / 项目名", "hint": "整部漫剧的名称", "options": []},
    {"key": "genre", "label": "题材", "hint": "故事类型", "options": ["古风仙侠", "都市悬疑", "科幻未来", "校园青春", "玄幻热血", "民国传奇", "职场逆袭"]},
    {"key": "style", "label": "风格基调", "hint": "整体叙事与氛围基调", "options": ["爽感逆袭", "温情治愈", "暗黑悬疑", "轻松搞笑", "热血燃向", "虐心催泪"]},
    # ⚠️ 候选值必须与前端 ProjectsPage.STYLE_LIBRARY 的 value **字面值完全一致**
    # （同口径双入口，历史缺陷：此处曾写「3D 动漫渲染」带空格，前端是「3D动漫渲染」无空格，
    #  导致 AI 敲定的画风与新建项目风格库对不上）。这里只给最常用的 12 个代表项；
    #  完整 61 个（对标 pavo 风格库：2D 24 / 3D 11 / 真人 26）见前端 STYLE_LIBRARY，
    #  本字段是自由文本，AI 也可给出清单外的风格名。
    {"key": "art_style", "label": "画风", "hint": "画面美术风格", "options": [
        "2D古风国漫风", "2D现代都市风", "2D水墨国风", "2D赛璐璐动画风",
        "宫崎骏画风", "国风二次元新国潮风格", "赛博朋克数字插画风格",
        "3D写实CG仙侠风", "3D迪士尼动画风格", "3D游戏渲染风格",
        "古风仙侠写实风", "现代都市写实风"]},
    {"key": "era_world", "label": "世界观 / 时代背景", "hint": "故事发生的世界与年代", "options": ["古代架空", "现代都市", "近未来赛博", "末世废土", "仙侠三界"]},
    {"key": "tone", "label": "情绪基调", "hint": "情绪色彩", "options": ["热血", "温情", "紧张悬疑", "搞笑", "压抑", "明快"]},
    {"key": "audience", "label": "目标受众", "hint": "主要面向的人群", "options": ["男性向", "女性向", "全年龄", "青少年", "成年向"]},
    # 与前端 ProjectsPage.ASPECT_PRESETS 的 value 字面值保持一致（两入口同口径，
    # style_kit.aspect_ratio 按显式 "a:b" 解析，后缀词「竖屏/横幅/超宽」仅供人读）。
    {"key": "aspect_ratio", "label": "画面比例", "hint": "成片画幅",
     "options": ["1:1 方形", "2:3 竖幅", "3:2 横幅", "3:4 竖幅", "4:3 横幅",
                 "9:16 竖屏", "16:9 横屏", "21:9 超宽"]},
    # ⚠️ 2026-09-26 口径变更：每集时长改为「1~2 分钟、最长 3 分钟」的产品区间
    #（见 novel_to_script.EPISODE_MAX_SEC / EPISODE_TARGET_SEC）。选项同步为区间内取值，
    # 默认推荐值 90 秒（1.5 分钟）。集数不再由用户指定，而由章节内容自动拆分。
    {"key": "episode_duration", "label": "单集时长",
     "hint": "每集大致时长（1~2 分钟，最长 3 分钟；集数由章节内容自动拆分）",
     "options": ["60 秒", "90 秒", "2 分钟"]},
    {"key": "shots_per_episode", "label": "单集镜头数", "hint": "每集分镜数量", "options": ["8 个", "10 个", "12 个", "16 个"]},
    {"key": "pacing", "label": "分镜节奏", "hint": "镜头切换与叙事节奏", "options": ["快节奏卡点", "平缓叙事", "张弛有度"]},
    {"key": "color_palette", "label": "色彩倾向", "hint": "主色调倾向", "options": ["高饱和明快", "低饱和冷调", "暖色调复古", "黑白+点缀色"]},
    {"key": "characters", "label": "主要角色设定", "hint": "角色名 + 身份 + 外形 + 性格", "options": []},
    {"key": "forbidden", "label": "规避项 / 禁忌", "hint": "需要规避的内容或元素", "options": []},
    {"key": "extra_notes", "label": "补充说明", "hint": "其它创作要求", "options": []},
]

FIELD_LABELS = {f["key"]: f["label"] for f in SETTING_FIELDS}
LIST_FIELDS = {"characters", "forbidden"}
MAX_VALUE_CHARS = 240          # 单个字符串字段最长长度
MAX_LIST_ITEMS = 12            # 列表字段最多条目


def fields_meta() -> list:
    """给前端的字段元信息（含候选项）"""
    return [dict(f) for f in SETTING_FIELDS]


# ===================== 时间 / 文件 =====================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _read_json(path: str, default: dict) -> dict:
    if not path or not os.path.isfile(path):
        return default
    # ⚠️ Windows 上 `_atomic_replace` 换目录项的瞬间，另一线程 open() 会抛
    #    PermissionError（暂时拿不到 ≠ 文件坏了）。旧实现直接返回默认值 →
    #    表现为「聊天记录突然清空」。这里退避重试。
    err = None
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or default
        except PermissionError as e:
            err = e
            time.sleep(0.02 * (i + 1))
        except Exception as e:  # noqa: BLE001
            err = e
            break
    logger.warning(f"读取 {os.path.basename(path)} 失败（按默认值处理）：{err}")
    return default


def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试。

    ⚠️ Windows 上若目标文件正被另一个线程/进程打开读取（例如 `load_index` 刚读完、
    句柄尚未释放），`os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这属于「暂时拿不到」而不是「文件坏了」：直接失败会让新建项目/保存设置偶发 500
    （实测并发读写时必现）。退避重试即可。
    """
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:        # WinError 5 / 32：目标被占用
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


def _write_json(path: str, data: dict) -> None:
    _ensure_dir(path)
    # ⚠️ 临时名必须每次唯一（固定 `.tmp` 会被并发写者互相截断，见文件头并发注释）
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(tmp, path)          # 同分区 replace 原子 + Windows 占用退避重试
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
        raise


# ===================== 会话历史 =====================

def default_history() -> dict:
    return {"version": 2, "messages": [], "projects": {}, "drafts": {},
            "active_project": "", "updated_at": None}


def _legacy_msg_id(m: dict) -> str:
    """为缺失 `msg_id` 的历史消息派生**确定性** id（同一条消息每次加载得到同一 id）。

    ⚠️ 必须确定性：旧文件（v1 / v2 初期）的消息没有 `msg_id`，而热/冷合并去重、
    归档水位线对齐都依赖稳定 id。用 (time, role, content) 派生可保证跨次加载一致；
    若用随机 id，同一条消息每次 load 都变 → 去重失效、归档重复、水位线错位。
    """
    raw = f"{m.get('time') or ''}\x1f{m.get('role') or ''}\x1f{m.get('content') or ''}"
    return "m_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _clean_messages(msgs) -> list:
    """清洗消息：只保留 user/assistant，并保证每条都有 `msg_id`（热/冷一致）。

    ⚠️ 不得在清洗时丢掉 `msg_id`：热窗口截断、归档水位线、`load_all_messages`
    去重全都依赖它。旧数据无 `msg_id` → 用 `_legacy_msg_id` 确定性派生。
    这里对缺失 id 的消息**就地补齐**（其 dict 来自 json 解析或本次内存构造），
    便于后续 `save_history` 把 id 一并落盘、下次加载即稳定。
    """
    if not isinstance(msgs, list):
        return []
    out = []
    for m in msgs:
        if not (isinstance(m, dict) and m.get("role") in ("user", "assistant")):
            continue
        if not m.get("msg_id"):
            m["msg_id"] = _legacy_msg_id(m)
        out.append(m)
    return out


def project_messages(history: dict, project: str = "") -> list:
    """取某项目的对话消息（按项目隔离；未指定项目时取全局，兼容旧调用）

    为什么必须按项目隔离：之前所有项目共用一份 messages，总控 AI 在为一个项目
    定风格时会看到别的项目的历史，实测出现「您说的是《剑冢》，但之前讨论的是
    《剑心初醒》，请问是替换还是并行项目」这类串台反问，直接影响设定质量。
    """
    key = history_project_key(project) if (project or "").strip() else ""
    if key:
        proj = (history.get("projects") or {}).get(key)
        if isinstance(proj, dict):
            return _clean_messages(proj.get("messages"))
        return []
    return _clean_messages(history.get("messages"))


def history_project_key(project: str) -> str:
    """历史/草稿的存储键：与直接落盘的 JSON 结构对齐（不做 safe_key 的大小写折叠）"""
    return canonical_project_key(project)


def _project_bucket(history: dict, project: str) -> dict:
    key = canonical_project_key(project) or "default"
    projects = history.setdefault("projects", {})
    bucket = projects.get(key)
    if not isinstance(bucket, dict):
        # archived_upto：归档水位线（已归档到的最后一条 msg_id）；archived_count：已归档条数
        # （用于归档行 seq 的项目内全量序号）。旧文件无这两个字段 → 读入容忍，见 save_history。
        bucket = {"messages": [], "archived_upto": "", "archived_count": 0}
        projects[key] = bucket
    return bucket


def load_history(path: str) -> dict:
    data = _read_json(path, default_history())
    out = default_history()
    out["messages"] = _clean_messages(data.get("messages"))
    # 按项目的历史（v2 结构）
    raw_projects = data.get("projects")
    if isinstance(raw_projects, dict):
        for k, v in raw_projects.items():
            if isinstance(v, dict):
                bucket = {"messages": _clean_messages(v.get("messages"))}
                # ⚠️ 必须带出归档水位线/计数：save_history 靠 archived_upto 判断「哪些还没归档」。
                # 旧文件无这两个字段 → 视为空（首次 save 全量补写归档），读入容忍。
                if v.get("archived_upto"):
                    bucket["archived_upto"] = v.get("archived_upto")
                if v.get("archived_count") is not None:
                    bucket["archived_count"] = v.get("archived_count")
                out["projects"][str(k)] = bucket
    # 旧数据迁移：v1 只有一份全局 messages，归到当时活跃的项目，避免历史丢失
    if not out["projects"] and out["messages"]:
        legacy_key = _legacy_canonical_key(data)
        out["projects"][legacy_key] = {"messages": list(out["messages"])}
        logger.info("AI 对话历史已迁移为按项目存储：%s（%d 条）",
                    legacy_key, len(out["messages"]))
    if isinstance(data.get("drafts"), dict):
        out["drafts"] = {}
        for k, v in data["drafts"].items():
            if isinstance(v, dict):
                # 草稿同样做旧键归一，避免与生效设定失配
                out["drafts"][canonical_project_key(k)] = v
    out["active_project"] = str(data.get("active_project") or "")
    out["updated_at"] = data.get("updated_at")
    return out


def _legacy_canonical_key(data: dict) -> str:
    """旧版单份历史归属的项目键：优先活跃项目，其次 default"""
    return canonical_project_key(str(data.get("active_project") or "")) or "default"


@_locked
def save_history(path: str, history: dict) -> dict:
    """落盘会话历史：**先把未归档的消息追加进归档，再截断热窗口并原子写**。

    ⚠️ 顺序：必须在 `msgs[-HISTORY_MAX_MESSAGES:]` 截断**之前**归档，否则被截掉的
    消息永远丢失（归档是「只增不减」的全量真相源，热文件只是有界窗口）。
    ⚠️ 归档必须在 _CHAT_LOCK 临界区内（本函数已 @_locked）：两个写者同时归档同一批
    消息会重复追加 —— 这正是归档追加不能单独加锁、只能内联在既有加锁函数里的原因。
    """
    root = os.path.dirname(os.path.abspath(path))
    for k, v in (history.get("projects") or {}).items():
        if not isinstance(v, dict):
            continue
        msgs = _clean_messages(v.get("messages"))
        akey = canonical_project_key(k) or str(k)
        upto = v.get("archived_upto") or ""
        start = 0
        if upto:
            for i, m in enumerate(msgs):
                if m.get("msg_id") == upto:
                    start = i + 1
                    break
            else:
                # 水位线丢失（回滚 / 换机 / 旧数据）：保守全量补写。
                # ⚠️ 已知边界：这可能让归档出现重复行（append-only 无法撤回），
                #    但 `load_all_messages` 按 msg_id 去重 → 对外仍是「不重不漏」。
                start = 0
        tail = msgs[start:]
        if tail:
            try:
                base_seq = int(v.get("archived_count") or 0)
            except (TypeError, ValueError):
                base_seq = 0
            _archive_append(root, akey, tail, base_seq)
            v["archived_upto"] = tail[-1].get("msg_id") or ""
            v["archived_count"] = base_seq + len(tail)
        v["messages"] = msgs[-HISTORY_MAX_MESSAGES:]      # 热窗口截断
    history["messages"] = _clean_messages(history.get("messages"))[-HISTORY_MAX_MESSAGES:]
    history["updated_at"] = _now()
    _write_json(path, history)
    return history


def _new_msg_id() -> str:
    """新消息唯一 id：`m_<YYYYmmddTHHMMSS>_<6 hex>`（时间可读 + 随机段防同秒碰撞）。

    热/冷两处复用**同一** `msg_id`，这是水位线对齐与去重的唯一依据（设计 §2.2.2）。
    """
    return f"m_{time.strftime('%Y%m%dT%H%M%S')}_{os.urandom(3).hex()}"


def append_message(history: dict, role: str, content: str, project: str = "") -> dict:
    """追加一条消息。指定 project 时写入该项目独立的历史（避免跨项目串台）"""
    msg = {"role": role, "content": str(content or ""), "time": _now(),
           "msg_id": _new_msg_id()}
    if (project or "").strip():
        _project_bucket(history, project)["messages"].append(msg)
    else:
        history.setdefault("messages", []).append(msg)
    return history


def drop_last_message(history: dict, project: str = "") -> dict:
    """回滚最后一条消息（模型未配置 / 调用失败时避免脏历史）"""
    if (project or "").strip():
        msgs = _project_bucket(history, project)["messages"]
    else:
        msgs = history.setdefault("messages", [])
    if msgs:
        msgs.pop()
    return history


@_locked
def clear_history(path: str, keep_settings: bool = True, project: str = "") -> dict:
    """清空会话（指定 project 时只清该项目的历史，不动别的项目）"""
    history = load_history(path)
    if (project or "").strip():
        key = canonical_project_key(project)
        history.setdefault("projects", {}).pop(key, None)
        for k in project_key_candidates(project):
            if k != key:
                (history.get("projects") or {}).pop(k, None)
    else:
        history["messages"] = []
        history["projects"] = {}
    if not keep_settings:
        history["drafts"] = {}
        history["active_project"] = ""
    return save_history(path, history)


def purge_project(
    history_path: str,
    settings_path: str,
    names: list,
    archive_root: str = "",
) -> dict:
    """项目被删除时，摘除它散落在 AI 总控三处的按项目键组织的数据（关联清理）。

    软删项目（project_store.delete_project）会把 22 类产物目录移入回收站，但
    output/ai_chat/ 里这三处是「全局单文件、内部按项目键分桶」或「按项目键分子目录」，
    不在产物目录覆盖内，必须单独摘除，否则会残留：
      1. chat_history.json 的 projects[键] / drafts[键] 桶，及 active_project 指向该键时清空；
      2. project_settings.json 的 settings[键]，及 active_project 指向该键时清空；
      3. archive/<候选键>/*.jsonl 全量归档子目录（整目录移入 .trash_purged，可还原）。

    ``names`` 是该项目的全部别名（dir_key + 显示名），键匹配对每个名字取
    :func:`project_key_candidates`（新规则 → 旧规则 → 原样名）再并集，与读写一致，
    既清掉按规范键落盘的新数据，也清掉按旧规则/原样名落盘的历史数据。
    归档子目录采用「移动而非删除」，与软删项目的可还原模型自洽。
    返回摘除统计 {history_projects, history_drafts, settings, archived_dirs, active_cleared}。
    """
    cands = []
    for n in (names or []):
        for k in project_key_candidates(n):
            if k and k not in cands:
                cands.append(k)
    cset = set(cands)
    if not cset:
        return {"history_projects": 0, "history_drafts": 0, "settings": 0,
                "archived_dirs": 0, "active_cleared": False, "candidates": []}
    report = {"history_projects": 0, "history_drafts": 0, "settings": 0,
              "archived_dirs": 0, "active_cleared": False, "candidates": cands}

    # 1) 会话历史 + 草稿桶
    history = load_history(history_path)
    for k in list((history.get("projects") or {}).keys()):
        if k in cset:
            history["projects"].pop(k, None)
            report["history_projects"] += 1
    for k in list((history.get("drafts") or {}).keys()):
        if k in cset:
            history["drafts"].pop(k, None)
            report["history_drafts"] += 1
    act = str(history.get("active_project") or "")
    if act in cset:
        history["active_project"] = ""
        report["active_cleared"] = True
    save_history(history_path, history)

    # 2) 创作设定
    sdata = load_settings_file(settings_path)
    sitems = sdata.get("settings") or {}
    for k in list(sitems.keys()):
        if k in cset:
            sitems.pop(k, None)
            report["settings"] += 1
    sact = str(sdata.get("active_project") or "")
    if sact in cset:
        sdata["active_project"] = ""
        sdata["updated_at"] = _now()
        report["active_cleared"] = True
    _write_json(settings_path, sdata)

    # 3) 全量归档子目录（移动而非删除，保持可还原）
    # 归档布局是 <archive_root>/archive/<候选键>/，复用现成 archive_dir 工具（含 archive/ 层）
    if archive_root:
        import shutil
        for k in cands:
            adir = archive_dir(archive_root, k)
            if os.path.isdir(adir):
                try:
                    target = adir + ".trash_purged"
                    # 避免同名冲突：若目标已存在则加时间戳
                    if os.path.exists(target):
                        target = adir + ".trash_purged." + datetime.now().strftime("%Y%m%d_%H%M%S")
                    shutil.move(adir, target)
                    report["archived_dirs"] += 1
                    logger.info("AI 总控归档已随项目删除摘除：%s → %s", adir, target)
                except OSError as e:
                    logger.error("摘除 AI 总控归档子目录失败（%s）：%s", adir, e)

    return report


# ===================== 会话归档（只增不减的全量真相源） =====================
#
# 分层（设计 §2.2.1）：
#   热 = chat_history.json 每项目桶最近 HISTORY_MAX_MESSAGES 条（读路径有界）
#   冷 = archive/<canonical_key>/<YYYY-MM-DD>.jsonl（只增不减，全量）
# 归档仅在 save_history（已加锁）内、在热窗口截断**之前**追加；水位线为
# history["projects"][key]["archived_upto"]（已归档到的最后一条 msg_id）。
#
# ⚠️ 追加写不是原子 `os.replace`（append-only 无法替换），但同样抗损：
#    每次追加后 flush()+os.fsync()，崩溃最多丢最后一行，不会写坏整文件。

ARCHIVE_SUBDIR = "archive"

#: 默认归档根目录（= AI 对话目录，如 output/ai_chat）。由 app 层注入；
#: 测试/导出可给 `load_all_messages` 显式传 root。
_DEFAULT_ARCHIVE_ROOT = ""


def set_archive_root(root: str) -> None:
    """注入默认归档根目录（app.py 启动时调用一次）。"""
    global _DEFAULT_ARCHIVE_ROOT
    _DEFAULT_ARCHIVE_ROOT = root or ""


def archive_dir(root: str, key: str) -> str:
    """某项目归档目录：`<root>/archive/<canonical_key>/`"""
    return os.path.join(root, ARCHIVE_SUBDIR, key)


def archive_path(root: str, key: str, date: str) -> str:
    """某项目某天的归档文件：`<root>/archive/<key>/<YYYY-MM-DD>.jsonl`"""
    return os.path.join(archive_dir(root, key), f"{date}.jsonl")


def _archive_append(root: str, key: str, msgs: list, base_seq: int = 0) -> int:
    """把 msgs 按 `m["time"][:10]` 分组，**追加**写入归档 jsonl；返回追加行数。

    - 行字段照设计 §3.1：`msg_id/role/content/time/date/project/project_key/seq`；
    - `seq`：项目内全量序号（`base_seq + 组内 1-based 下标`），便于导出排序；可选字段。

    ⚠️ **普通私有函数，不得加 @_locked**：`verify_atomic_write_locks.py` 断言
    `ai_chat.py` 中 `@_locked` 恒为 3。它只在 `save_history` 的 _CHAT_LOCK 临界区内被调用。
    """
    if not msgs:
        return 0
    groups: dict = {}
    for m in msgs:
        if not isinstance(m, dict):
            continue
        date = str(m.get("time") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        groups.setdefault(date, []).append(m)
    written = 0
    for date, items in groups.items():
        path = archive_path(root, key, date)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for i, m in enumerate(items):
                    rec = {
                        "msg_id": m.get("msg_id") or "",
                        "role": m.get("role") or "",
                        "content": m.get("content") or "",
                        "time": m.get("time") or "",
                        "date": date,
                        # project：归档只有规范键可用，故与 project_key 同值（设计 §3.1 示例两者相等）
                        "project": key,
                        "project_key": key,
                        "seq": base_seq + i + 1,
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
                f.flush()
                os.fsync(f.fileno())        # 追加后落盘，避免崩溃丢整段
        except Exception as e:  # noqa: BLE001
            logger.warning("归档追加失败（%s / %s）：%s", key, date, e)
    return written


def archive_count(root: str, key: str, date: str) -> int:
    """某项目某天的归档条数（只读行数，不解析内容）。"""
    path = archive_path(root, key, date)
    if not os.path.isfile(path):
        return 0
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("统计归档失败（%s）：%s", path, e)
    return n


def archive_dates(root: str, key: str) -> list:
    """列出该项目归档的日期与每日条数（按日期升序）。

    返回 `[{"date": "YYYY-MM-DD", "count": N}, ...]`（设计 §2.2.4）。
    """
    d = archive_dir(root, key)
    out: list = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        date = name[:-len(".jsonl")]
        if not date:
            continue
        out.append({"date": date, "count": archive_count(root, key, date)})
    return out


def load_archive(root: str, key: str, date: str, limit: int = 0, offset: int = 0) -> list:
    """读取某项目某天归档的消息（行序；`limit=0` 表示全部，`offset` 先跳过）。

    ⚠️ 读路径按需懒加载（只读被请求的那一天），不随归档总量增长（REQ-1.6）。
    """
    path = archive_path(root, key, date)
    out: list = []
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取归档失败（%s）：%s", path, e)
        return out
    if offset:
        out = out[offset:]
    if limit:
        out = out[:limit]
    return out


def _merge_messages(*groups) -> list:
    """按 `msg_id` 去重 + 按 (time, seq) 排序，合并多组消息（前面的组优先保留）。"""
    seen = set()
    merged: list = []
    for g in groups:
        for m in g or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("msg_id") or "")
            if mid:
                if mid in seen:
                    continue
                seen.add(mid)
            merged.append(m)

    def _order(m: dict):
        try:
            seq = int(m.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        return (str(m.get("time") or ""), seq)

    merged.sort(key=_order)
    return merged


def load_all_messages(history: dict, project: str = "", root: str = None) -> list:
    """热窗口 + 全部归档按时间合并去重（验收 / 导出用）。

    ⚠️ 「不丢」由归档保证，而非 `load_history`：`load_history` 保持有界（热窗口 40），
    本函数把热窗口与全量归档按 `msg_id` 去重合并 → 全量条数（设计 Q-arch-1）。
    `root` 缺省用 `set_archive_root` 注入的默认目录（app 启动时注入 output/ai_chat）。
    """
    key = canonical_project_key(project) if (project or "").strip() else ""
    hot = project_messages(history, project)
    if not key:
        return _merge_messages(hot)
    root = root or _DEFAULT_ARCHIVE_ROOT
    archived: list = []
    if root:
        for d in archive_dates(root, key):
            archived.extend(load_archive(root, key, d["date"]))
    # 归档在前：同 msg_id 时保留归档记录（带 seq，便于导出排序）
    return _merge_messages(archived, hot)


# ===================== 创作设定（草稿 / 生效） =====================

def canonical_project_key(project_name: str) -> str:
    """项目键（规范规则）：与项目注册表 project_store.safe_key 完全一致

    必须与「产物目录键」一致。AI 创作设定、对话草稿、托管生产计划都以项目为键，
    两套规则一旦不同就会出现静默失配：项目名含《》书名号、空格或「·」时，
    设定被写到 A 键、而查询按 B 键去找，表现为「明明谈好了风格，一键设定却报没有设定」。
    """
    cleaned = re.sub(r"[《》〈〉【】「」『』]", "", str(project_name or "")).strip()
    key = "".join(c if c.isalnum() or c in "_-" else "_" for c in cleaned)
    return (key.strip("_") or "project")[:60]


def legacy_project_key(project_name: str) -> str:
    """旧规则（仅替换文件系统非法字符，保留空格与《》等）

    仅用于兼容读取历史落盘数据；新写入一律使用 canonical_project_key。
    """
    name = (project_name or "").strip() or "default"
    return re.sub(r"[\\/:*?\"<>|]+", "_", name)[:60] or "default"


def project_key(project_name: str) -> str:
    """项目键（写入用）"""
    return canonical_project_key(project_name)


def project_key_candidates(project_name: str) -> list:
    """读取用候选键（去重保序）：新规则 → 旧规则 → 原样名

    读取必须宽松：历史数据可能按旧规则落盘，宽松匹配才能既修好新数据又不丢老配置。
    """
    out = []
    for k in (canonical_project_key(project_name),
              legacy_project_key(project_name),
              str(project_name or "").strip()[:60]):
        if k and k not in out:
            out.append(k)
    return out


def normalize_settings(raw: dict) -> dict:
    """只保留已知字段，做长度与类型清洗，全部转成可 JSON 落盘的值"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for f in SETTING_FIELDS:
        k = f["key"]
        if k not in raw:
            continue
        v = raw[k]
        if v in (None, "", [], {}):
            continue
        if k in LIST_FIELDS:
            if isinstance(v, str):
                items = [s.strip() for s in re.split(r"[\n;；,，]+", v) if s.strip()]
            elif isinstance(v, list):
                items = []
                for it in v:
                    if isinstance(it, dict):
                        txt = "；".join(f"{kk}：{vv}" for kk, vv in it.items() if vv)
                    else:
                        txt = str(it)
                    if txt.strip():
                        items.append(txt.strip()[:MAX_VALUE_CHARS])
            else:
                items = [str(v).strip()]
            if items:
                out[k] = items[:MAX_LIST_ITEMS]
        else:
            if isinstance(v, (dict, list)):
                txt = json.dumps(v, ensure_ascii=False)
            else:
                txt = str(v)
            txt = txt.strip()[:MAX_VALUE_CHARS]
            if txt:
                out[k] = txt
    return out


def merge_settings(base: dict, patch: dict) -> dict:
    """合并设定：字符串字段后值覆盖前值；列表字段按去重追加"""
    merged = dict(base or {})
    for k, v in normalize_settings(patch).items():
        if k in LIST_FIELDS:
            old = list(merged.get(k) or [])
            for item in v:
                if item not in old:
                    old.append(item)
            merged[k] = old[:MAX_LIST_ITEMS]
        else:
            merged[k] = v
    return merged


def get_draft(history: dict, project_name: str) -> dict:
    """取草稿：按候选键宽松查找，兼容历史旧规则键"""
    drafts = history.get("drafts") or {}
    for k in project_key_candidates(project_name):
        rec = drafts.get(k)
        if rec:
            return normalize_settings(rec)
    return {}


def set_draft(history: dict, project_name: str, settings: dict) -> dict:
    """写草稿：按规范键写入，并清掉同项目的旧规则键（避免出现两份草稿）"""
    drafts = history.setdefault("drafts", {})
    key = project_key(project_name)
    drafts[key] = normalize_settings(settings)
    for k in project_key_candidates(project_name):
        if k != key:
            drafts.pop(k, None)
    return history


def load_settings_file(path: str) -> dict:
    data = _read_json(path, {"version": 1, "settings": {}})
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    return data


@_locked
def save_project_settings(path: str, project_name: str, settings: dict) -> dict:
    """把确认后的创作设定落盘为项目配置（同项目覆盖更新，保留其它项目）"""
    data = load_settings_file(path)
    key = project_key(project_name)
    clean = normalize_settings(settings)
    data["settings"][key] = {
        "project_name": (project_name or "").strip() or "default",
        "settings": clean,
        "updated_at": _now(),
    }
    # 迁移：同名项目若曾按旧规则落盘，合并过去并删除旧键，避免出现两份互相打架的设定
    for old in project_key_candidates(project_name):
        if old == key:
            continue
        legacy = data["settings"].pop(old, None)
        if isinstance(legacy, dict) and (legacy.get("settings") or {}):
            merged = {**normalize_settings(legacy.get("settings")), **clean}
            data["settings"][key]["settings"] = merged
    data["active_project"] = key
    data["updated_at"] = _now()
    _write_json(path, data)
    return data


def active_settings(path: str, project_name: str = "") -> dict:
    """读取当前生效的创作设定：指定项目优先，否则取最近应用的项目

    项目查找按候选键宽松匹配，兼容历史旧规则落盘的键。
    """
    data = load_settings_file(path)
    items = data.get("settings") or {}
    keys = (project_key_candidates(project_name) if (project_name or "").strip()
            else [data.get("active_project") or ""])
    key, rec = "", None
    for k in keys:
        if k and isinstance(items.get(k), dict):
            key, rec = k, items[k]
            break
    if not isinstance(rec, dict):
        return {"project_name": "", "settings": {}, "active": False,
                "settings_file": os.path.abspath(path), "updated_at": data.get("updated_at")}
    return {
        "project_name": rec.get("project_name") or key,
        "settings": normalize_settings(rec.get("settings") or {}),
        "active": True,
        "settings_file": os.path.abspath(path),
        "updated_at": rec.get("updated_at"),
    }


def style_brief(settings: dict) -> str:
    """把生效设定压成一段风格纲要，供剧本生成 / 提示词 / 分镜链路追加引用"""
    s = normalize_settings(settings or {})
    if not s:
        return ""
    tail = {
        "genre": "题材", "style": "风格基调", "art_style": "画风", "era_world": "世界观",
        "tone": "情绪基调", "audience": "目标受众", "aspect_ratio": "画面比例",
        "episode_duration": "单集时长", "shots_per_episode": "单集镜头数",
        "pacing": "分镜节奏", "color_palette": "色彩倾向",
    }
    parts = []
    for k, label in tail.items():
        if s.get(k):
            parts.append(f"{label}：{s[k]}")
    for k, label in (("characters", "主要角色"), ("forbidden", "规避项"), ("extra_notes", "补充要求")):
        if s.get(k):
            val = s[k]
            parts.append(f"{label}：" + ("、".join(val) if isinstance(val, list) else str(val)))
    if not parts:
        return ""
    title = f"《{s['title']}》" if s.get("title") else ""
    return f"创作设定{title}—" + "；".join(parts)


def settings_view(path: str, project_name: str = "") -> dict:
    """生效设定的展示视图（供界面「当前已生效设定」区）"""
    active = active_settings(path, project_name)
    s = active.get("settings") or {}
    rows = []
    for f in SETTING_FIELDS:
        v = s.get(f["key"])
        if v in (None, "", []):
            continue
        rows.append({"key": f["key"], "label": f["label"],
                     "value": v, "display": "、".join(v) if isinstance(v, list) else str(v)})
    return {
        "active": active.get("active"),
        "project_name": active.get("project_name"),
        "settings": s,
        "rows": rows,
        "filled": len(rows),
        "total": len(SETTING_FIELDS),
        "missing": [f["label"] for f in SETTING_FIELDS if s.get(f["key"]) in (None, "", [])],
        "style_brief": style_brief(s),
        "settings_file": active.get("settings_file"),
        "updated_at": active.get("updated_at"),
    }


# ===================== 模型调用（对话总控） =====================

def _field_spec_text() -> str:
    lines = []
    for f in SETTING_FIELDS:
        opt = f"（候选：{' / '.join(f['options'])}）" if f["options"] else ""
        lines.append(f"- {f['key']}｜{f['label']}：{f['hint']}{opt}")
    return "\n".join(lines)


SYSTEM_PROMPT = """你是漫剧（AI 动态漫画短剧）创作总控导演，负责通过对话帮用户敲定整部作品的创作设定。

【工作方式】
1. 每轮聚焦 1~2 个尚未确定的设定项，用简洁中文推进，不要一次抛出所有问题。
2. 主动给出 2~4 个候选方案供用户选择（可带一句话理由），用户也能自由输入其它答案。
3. 用户给出模糊回答时，先复述你的理解再确认；用户明确否定就换方向重提案。
4. 保持对话连贯：结合已确认的设定给出专业建议，语气简洁、专业、不啰嗦。
5. 当主要设定（题材 / 风格 / 画风 / 世界观 / 角色 / 分镜节奏）基本齐全时，提醒用户可以点击「应用设定」落盘生效。

【可敲定的设定项】
{fields}

【当前流水线口径（务必遵守，勿提旧步骤）】
系统现为 7 步自动流水：剧本(script) → 配音先行(tts_pre) → 资产(assets) → 分镜(storyboard) → 视频(video) → 超分(upscale) → 成片(final)。（2026-10-09 口径校正：关键帧步已并入分镜/视频阶段，此前提示层仍复述旧口径。）
H3 视频自带原生音轨（生成时已注入角色参考音色），成片即带配音，**不存在**「口型」「混音合成」环节。
用户问「怎么配音 / 口型 / 混音 / 声音没进去」时，一律答：当前是自动流水，配音随视频生成自动完成，无需手动口型/混音；个别台词没声时，到「声音处理」页检查该角色参考音色绑定与台词。
**不要**输出「本机体验提醒」「跑一次口型」「跑混音合成」这类过时建议。

【每轮回复格式】
先写 1~3 句自然语言（说明当前进度、候选方案或确认结果），然后在最末尾追加一个 JSON 代码块，只包含本轮新确认或修改的字段（键用上面英文 key，未变更的字段不要出现；没有新确认内容时输出空对象 {}）：
```json
{"genre": "都市悬疑", "art_style": "现代都市写实风"}
```
JSON 必须是合法 JSON，不要写注释。

【当前已确认的创作设定（草稿）】
{draft}
"""


def build_messages(history: dict, draft: dict, project_name: str = "") -> list:
    draft_txt = json.dumps(normalize_settings(draft) or {}, ensure_ascii=False, indent=2) if draft else "（暂无）"
    system = SYSTEM_PROMPT.replace("{fields}", _field_spec_text()).replace("{draft}", draft_txt)
    messages = [{"role": "system", "content": system}]
    for m in project_messages(history, project_name)[-CONTEXT_MESSAGES:]:
        messages.append({"role": m.get("role"), "content": str(m.get("content") or "")[:MAX_CHARS_PER_MESSAGE]})
    return messages


def extract_settings(reply: str) -> dict:
    """从模型回复中抽取结构化设定（取最后一个 JSON 代码块 / JSON 对象）"""
    text = (reply or "").strip()
    candidates = []
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.S):
        candidates.append(m.group(1).strip())
    start, end = text.rfind("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in reversed(candidates):
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", cand))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            known = normalize_settings(data)
            if known or not data:
                return known
    return {}


def strip_json_block(reply: str) -> str:
    """去掉回复末尾用于结构化的 JSON 代码块，避免在聊天气泡里重复展示"""
    text = re.sub(r"```(?:json)?\s*.+?```\s*$", "", (reply or "").strip(), flags=re.S).strip()
    return text or (reply or "").strip()
