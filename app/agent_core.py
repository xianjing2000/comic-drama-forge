# -*- coding: utf-8 -*-
"""总控 AI 自主执行内核（function-calling agent）。

设计前提：用户要求「不手动确认，全交给总控 AI 自主判断并执行」。
因此这里**没有**任何 human-in-the-loop 弹窗，安全完全靠自动护栏保证：

1. **工具白名单**：只暴露本文件里声明的动作，删除项目 / 删角色 / 清记忆等
   破坏性能力压根不在 schema 里，模型想调也调不到。
2. **昂贵动作配额**：单轮最多 MAX_EXPENSIVE 次烧 GPU/耗时的动作（重生成、超分、
   配音、导出、整集生产），超了直接拒绝并告诉模型，逼它收敛。
3. **步数与时长上限**：MAX_STEPS / MAX_TURN_SEC，防死循环。
4. **同参数冷却**：完全相同的「工具 + 参数」在 COOLDOWN_SEC 内直接复用上次结果，
   避免模型反复重试把显卡烧穿。
5. **全局急停 + 单项目互斥**：POST /api/agent/kill 一键刹车；同一项目同时只允许
   一个 agent 任务在跑。
6. **全量审计**：每次工具调用落 output/agent/audit-<date>.jsonl，事后可追责。

工具调用走「进程内直连」（Flask test_client），不依赖端口、不走网络，
避免 agent 在后台线程里自己发 HTTP 打到自己导致的端口耦合与代理问题。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from urllib.parse import quote

logger = logging.getLogger(__name__)

# ===================== 护栏参数 =====================

MAX_STEPS = 12             # 单轮最多几步（1 步 = 一次模型决策）
MAX_EXPENSIVE = 4          # 单轮最多几次昂贵动作
MAX_TURN_SEC = 1800        # 单轮总时长上限（30 分钟）
COOLDOWN_SEC = 120         # 同参数冷却窗口
JOB_TTL_SEC = 3600         # 任务结果保留时长
MAX_RESULT_CHARS = 1600    # 单个工具结果喂回模型的字符上限

_HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_DIR = os.path.join(os.path.dirname(_HERE), "output", "agent")

# ===================== 运行时状态 =====================

_APP = None
_LOCK = threading.Lock()
_JOBS: dict = {}
_BUSY: set = set()
_KILL = {"on": False, "reason": "", "at": 0.0}
_COOLDOWN: dict = {}


def bind_app(flask_app):
    """由 app.py 在启动时注入 Flask 实例（避免循环 import）"""
    global _APP
    _APP = flask_app


# ===================== 工具定义 =====================
#
# call(args, ctx) -> (method, path, payload)
#   ctx = {"project": str, "episode": int|None}
# risk: safe=只读 / write=改配置 / expensive=烧 GPU 或耗时

def _p(name):
    return str(name or "").strip()


def _qp(project):
    return quote(_p(project), safe="")


def _settings_keys_hint() -> str:
    """把「创作设定」的合法字段名喂给模型。

    实测教训：模型会自造键名（例如 color_tone / camera_language），
    而后端 `normalize_settings()` 只保留白名单内的字段，其余**静默丢弃** ——
    用户明明说了「冷色调、克制镜头」，落盘后却只剩一个 style，
    等于白沟通一场。这里把合法键名直接写进工具描述。
    """
    try:
        import ai_chat  # 惰性引入：agent_core 与 ai_chat 之间避免顶层强耦合
        return "、".join(f["key"] for f in ai_chat.SETTING_FIELDS)
    except Exception:  # noqa: BLE001
        return "style、art_style、tone、color_palette、pacing、characters、extra_notes"


def _schema(props: dict, required: list = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}
_ARR_STR = {"type": "array", "items": {"type": "string"}}
_OBJ = {"type": "object"}


def _proj_prop(desc="项目名（缺省用当前项目）"):
    return {"type": "string", "description": desc}


def _read_production_log_call(a, c):
    """read_production_log 的参数构造（沿用 call → (method, path, payload) 分发约定）。

    episode_no 缺省时定位「最近在生产的集」：
    ① autopilot.status(project, brief=True) 的 current（正在生产的集；
       status() 对他项目在跑时会回 current=None，不会张冠李戴）；
    ② 兜底：output/autopilot/<项目>/episodes/ 下 mtime 最新的 epNN_log.jsonl。
    两处都取不到时 episode_no 传 0 —— 端点会以 exists:false + note 明确回答
    「该集还没有操作日志」，模型不会拿到误导性数据。
    """
    a = dict(a or {})
    project = _p(a.get("project") or c.get("project"))
    ep = 0
    try:
        ep = int(a.get("episode_no") or 0)
    except (TypeError, ValueError):
        ep = 0
    if ep <= 0:
        try:
            import autopilot as _ap  # 惰性引入：避免顶层导入链耦合
            cur = (_ap.status(project, brief=True) or {}).get("current") or {}
            if project and str(cur.get("project") or "") == project:
                ep = int(cur.get("episode") or 0)
        except Exception:  # noqa: BLE001
            ep = 0
    if ep <= 0:
        try:
            import autopilot as _ap
            base = os.path.join(_ap._autopilot_dir(project), "episodes")
            best_t = -1.0
            if project and os.path.isdir(base):
                for fn in os.listdir(base):
                    m = re.match(r"^ep(\d+)_log\.jsonl$", fn)
                    if not m:
                        continue
                    t = os.path.getmtime(os.path.join(base, fn))
                    if t > best_t:
                        best_t, ep = t, int(m.group(1))
        except Exception:  # noqa: BLE001
            ep = 0
    try:
        tail = max(1, min(int(a.get("tail") or 60), 500))
    except (TypeError, ValueError):
        tail = 60
    return ("GET", f"/api/autopilot/episode_log?project={_qp(project)}"
                   f"&episode_no={int(ep or 0)}&tail={tail}", {})


TOOLS = [
    # ---------- 只读探针 ----------
    {
        "name": "get_status",
        "description": "查询项目托管状态：自动生产开关、当前在做的事、**上一次生产的结果**（last_run：上一集是成功还是失败、失败原因、耗时）、待验收数、异常数。参数可选，不传则用当前项目上下文。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        # brief=1：只取状态判断必要的短字段 —— 工具结果只喂回前 1600 字符，
        # 完整 payload 里的 curve/totals 会把 last_run（上一集结果）挤出窗口。
        "call": lambda a, c: ("GET", f"/api/autopilot/status?brief=1&project={_qp(a.get('project') or c.get('project'))}", {}),
    },
    {
        "name": "get_progress",
        "description": "查询某个项目的生产进度（已完成集数、当前集、当前步骤、失败原因）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", f"/api/autopilot/progress/{_qp(a.get('project') or c.get('project'))}", {}),
    },
    {
        "name": "list_projects",
        "description": "列出所有项目及其关联小说，用于判断该让哪个项目跑 24 小时自动生产。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/projects", {}),
    },
    {
        "name": "list_plans",
        "description": "列出所有项目的托管计划（含是否开启托管 enabled、目标集数、章节数进度）。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/autopilot/plans", {}),
    },
    {
        "name": "get_plan",
        "description": "读取某项目的自动生产计划（目标集数、镜头数、风格、各步骤开关、超分倍率等）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", f"/api/autopilot/plan/{_qp(a.get('project') or c.get('project'))}", {}),
    },
    {
        "name": "get_novel",
        "description": "读取**本项目绑定的原著小说**：书名、章节数、开篇正文、已定风格。"
                       "沟通创作风格 / 定角色外形 / 判断题材基调之前**必须先调用它**——"
                       "不调你就不知道这本小说讲什么，只能靠猜（实测会给用户按别的小说定风格）。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/projects/" + _qp(a.get("project") or c.get("project"))
                              + "/novel-brief", {}),
    },
    {
        "name": "readiness_check",
        "description": "自检「是否具备无人值守条件」：还缺哪些配置/模型/环境。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/autopilot/ready", {}),
    },
    {
        "name": "get_shots",
        "description": "读取分镜画布：镜头列表、每镜的画面描述/时长/状态/已有产物。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/storyboard/canvas/" + _qp(a.get("project") or c.get("project"))
                              + (f"?episode_no={int(a['episode'])}" if a.get("episode") else ""), {}),
    },
    {
        "name": "get_qc_summary",
        "description": "读取质检总览：各镜头通过/未通过、重试次数、主要扣分原因。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/qc/project-summary?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_deliverables",
        "description": "列出已产出的成片交付物及其验收状态。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/autopilot/deliverables?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_exports",
        "description": "列出已导出的剪映/FCPXML/SRT 等文件。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/export/list?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_characters",
        "description": "列出项目角色卡（名称/定位/服装/描述/状态）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/characters?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_asset_definitions",
        "description": ("列出某集需要生成的**资产定义**（角色 / 物品 / 场景），"
                        "含外貌、参考图提示词（中英）、是否被设定集锁定。"
                        "用来查清「这一集到底要生成哪些资产、各自长什么样」。"),
        "parameters": _schema({
            "project": _proj_prop(),
            "episode_no": {"type": "integer", "description": "集号，默认 1"},
        }, []),
        "risk": "safe", "expensive": False,
        # 后端 GET /api/projects/<pid>/asset-definitions 读剧本顶层 characters/items/scenes
        # ⚠️ 不是剧本里的 assets 字段（那个是空的），见 routes/projects.py 的实现说明
        "call": lambda a, c: ("GET",
                              "/api/projects/"
                              + quote(_p(a.get("project") or c.get("project")), safe="")
                              + "/asset-definitions?episode_no="
                              + str(a.get("episode_no") or 1), {}),
    },
    {
        "name": "list_exceptions",
        "description": "列出自动生产中的异常（失败集、卡住的步骤），判断要不要处理。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/autopilot/exceptions", {}),
    },
    {
        "name": "read_production_log",
        "description": "读取某集生产操作日志（每步的开始/跳过/完成/失败与进度事件，"
                       "含时间戳与耗时）。生产卡住、进度长时间不动、需要向用户解释"
                       "某环节耗时或失败原因时调用。参数：project（项目键，必填）、"
                       "episode_no（集号，缺省=最近在生产的集）、tail（返回最近 N 条，"
                       "缺省 60，上限 500）。返回 exists:false 表示该集还没有操作日志。",
        "parameters": _schema({"project": _proj_prop(), "episode_no": _INT,
                               "tail": _INT}),
        "risk": "safe", "expensive": False,
        "call": _read_production_log_call,
    },
    {
        "name": "search_memory",
        "description": "在经验记忆库里检索历史成功/失败经验，用于决定这次怎么调参。",
        "parameters": _schema({"q": {"type": "string", "description": "检索关键词"}}, ["q"]),
        "risk": "safe", "expensive": False,
        # P2-2：端点读的是 `query` 参数（见 api_memory_search 的 request.args.get('query')），
        # 此前误发 `q` → 端点永远拿到空 query → 400「缺少 query 参数」→ 工具恒 ok=False。
        # 入参对用户仍叫 q（schema 不变），但请求要落到 `query` 上。
        "call": lambda a, c: ("GET",
                              "/api/memory/search?query=" + quote(_p(a.get("q")), safe=""), {}),
    },

    # ---------- 配置写入 ----------
    {
        "name": "update_plan",
        "description": "修改自动生产计划。patch 只接受计划字段，例如 "
                       "episodes（**选哪几章**：'all' 或 [1,2,3]，**不是总集数**）/ target_shots / "
                       "style / enable_upscale / "
                       "upscale_scale / enable_tts_pre / "
                       "step_max_retries / "
                       # ⭐⭐ 2026-10-10 用户**连续两次**纠正，勿再回退：
                       #   ① 此前只列参数名 → AI 总控把 target_shots 的默认值 12 当硬性方案报给用户
                       #      （用户问「为什么每集 12 个镜头」）；
                       #   ② 我第一次改成「给默认值 + 按题材给建议区间」—— 仍是在报数字，
                       #      用户再次纠正「这个不应该是根据章节内容的具体情况来吗，说了不要设置上限和固定值」。
                       #   故本次**彻底移除一切区间与建议值**，只保留「不许主动报数」这一条口径。
                       # ⭐⭐ 2026-10-10 用户纠正：「我不是说了一章节对应一集吗」—— AI 总控把项目配置里的
                        # episodes=10（被当成「总集数」）当成方案报成「计划产 10 集」，而 14 章的
                        # 小说按「一章一集」应为 14 集。故在此明确口径。
                        "⛔ **本方案的集数 = 选中章节数，一章 = 一集**（14 章 → 14 集）。"
                        "episodes 字段的语义是「选哪几章」而不是「总共几集」——"
                        "要全做就填 'all'，只做第 1~3 章就填 [1,2,3]。**不要**去设总量、"
                        "也不要把「计划产 N 集」当成可配置项报给用户；用户若只做一章就说「只做第 N 章」。\n"
                        "（另：**不要向用户报告或建议「每集镜头数」这个值，也不要在方案里列它。** 用户 2026-10-10"
                        "两次明确纠正：「为什么显示每集镜头数」「说了不要设置上限和固定值」。）"
                        "target_shots 的语义：**默认 0 = 不预设**，镜数由 AI 按本章节原文的"
                       "**信息密度**自行判定（冲突/转折/关键动作密集就多切，单薄就少切）：判据是「一个镜头"
                        "= 一个能在 5~6 秒内演完的完整内容单元」，密度锚点为平均每镜承载原文"
                        "约 config.CHARS_PER_SHOT 字；禁止静态画面单独成镜。允许范围 0~40。"
                       "**只有用户主动要求指定镜数时**才设它，并须说明「设了就固定了、会覆盖内容自适应判定」。"
                        "绝不主动报数、绝不给「建议提到 N 镜」。"
                        "另注：app.py 里的「上限 12 镜」是**单镜重跑**的防误触上限，"
                        "与每集镜数无关。"
                       "auto_accept(产出即自动验收，默认 false) / auto_revive_hours(失败集挂起多久后"
                       "自动复活重试，默认 6 小时，0=永不) / max_episode_attempts 等。"
                       "（enable_tts / enable_mix / video_mode 已废弃：tts/mix 步骤下线，"
                       "视频只有整集模式，设置了也不会生效。）",
        "parameters": _schema({"project": _proj_prop(), "patch": _OBJ},
                              ["patch"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST",
                              f"/api/autopilot/plan/{_qp(a.get('project') or c.get('project'))}",
                              dict(a.get("patch") or {})),
    },
    {
        "name": "apply_creative_settings",
        "description": "把创作设定（风格、画风、色调、节奏、角色外形等）写入项目，后续生成都会带上。"
                       "settings 的键**只能用这些**：" + _settings_keys_hint() +
                       "。用别的键名会被后端丢弃（不会报错但也不生效）。"
                       "例如「冷色调」→ color_palette，「镜头节奏克制」→ pacing，"
                       "「暗黑武侠」→ genre 或 tone。不确定的补充要求放 extra_notes。",
        "parameters": _schema({"project": _proj_prop(), "settings": _OBJ}, ["settings"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/ai/chat/apply",
                              {"project": a.get("project") or c.get("project"),
                               "settings": a.get("settings") or {}}),
    },
    {
        "name": "update_character",
        "description": "修改角色卡（例如换服装、改描述）。char_id 从 list_characters 拿。",
        "parameters": _schema({
            "char_id": {"type": "string", "description": "角色 id"},
            "project": _proj_prop(),
            "name": _STR, "role": _STR, "description": _STR, "outfit": _STR, "status": _STR,
        }, ["char_id"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("PUT", f"/api/characters/{quote(_p(a.get('char_id')), safe='')}",
                              {k: v for k, v in {
                                  "project": a.get("project") or c.get("project"),
                                  "name": a.get("name"), "role": a.get("role"),
                                  "description": a.get("description"),
                                  "outfit": a.get("outfit"), "status": a.get("status"),
                              }.items() if v is not None}),
    },
    {
        "name": "resolve_exception",
        "description": "处理一条生产异常（标记已解决，让托管继续往下跑）。"
                       "必须传 episode_no（异常清单里每条的集号，不是 id）。",
        "parameters": _schema({"project": _proj_prop(), "episode_no": _INT,
                               "note": _STR}, ["episode_no"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/exceptions/resolve",
                              {"project": a.get("project") or c.get("project"),
                               "episode_no": int(a.get("episode_no") or c.get("episode_no") or 0),
                               "note": a.get("note") or ""}),
    },
    {
        "name": "review_deliverable",
        "description": "验收/打回一条成片交付物。review 取值：accepted（验收通过）/ "
                       "rejected（打回重做）/ pending。必须传 episode_no。",
        "parameters": _schema({"project": _proj_prop(), "episode_no": _INT,
                               "review": {"type": "string",
                                          "description": "accepted / rejected / pending"},
                               "note": _STR}, ["episode_no", "review"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/deliverables/review",
                              {"project": a.get("project") or c.get("project"),
                               "episode_no": int(a.get("episode_no") or c.get("episode_no") or 0),
                               "review": a.get("review") or "pending",
                               "note": a.get("note") or ""}),
    },
    {
        # 2026-09-30：清空重做能力（用户需求）。安全边界：产物**移入回收站**（可恢复）
        # 而非真删；只动该集的产物；剧本默认保留，include_script=true 才一起清。
        "name": "reset_episode",
        "description": "清空重做指定一集：把该集已生成的产物（分镜图/尾帧/视频/成片/配音/质检记录/"
                       "交付记录）整体**移入回收站**（可恢复，不是永久删除），之后用 produce_episode "
                       "从头重产该集。⚠️ 调用前必须先 stop_production 停止该项目的生产，否则正在写入"
                       "的文件会清不掉（结果里 skipped 会如实列出）。include_script=true 时连剧本也"
                       "一并移入回收站（下一轮会重新生成剧本）——只有用户明确要求「连剧本一起重做」才传 true。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT,
                               "include_script": _BOOL}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/reset-episode",
                              {"project_name": a.get("project") or c.get("project"),
                               "episode_no": int(a.get("episode") or c.get("episode") or 1),
                               "include_script": bool(a.get("include_script"))}),
    },
    {
        # 单镜清空重做：清完之后 produce_episode 的断点续跑只会重做缺失的这一镜。
        "name": "reset_shot",
        "description": "清空重做单个镜头：把该镜的分镜图/尾帧/视频/配音/质检记录移入回收站"
                       "（可恢复，不是永久删除），之后用 produce_episode 重产该集时只会重做"
                       "缺失的这一镜（其余镜不重烧）。shot_id 形如 shot_3（从 get_shots 拿）。"
                       "⚠️ 调用前先 stop_production 停止该项目的生产。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT,
                               "shot_id": {"type": "string",
                                           "description": "镜号，形如 shot_3"}},
                              ["shot_id"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/reset-shot",
                              {"project_name": a.get("project") or c.get("project"),
                               "episode_no": int(a.get("episode") or c.get("episode") or 1),
                               "shot_id": _p(a.get("shot_id"))}),
    },
    {
        # 单资产清空重做：资产卡描述保留，只清已生成的图；下一轮生产自动补做。
        "name": "reset_asset",
        "description": "清空重做单个资产（角色/物品/场景）：把该资产的已生成图（基础图+多视图）"
                       "移入回收站（可恢复，不是永久删除），资产卡描述保留；之后用 produce_episode "
                       "重产时会因「基础图缺失」自动补做该资产。⚠️ 若命中跨项目资产库的形象指纹"
                       "可能直接复用旧图——要换形象请先用 update_character 改描述再重做。"
                       "调用前先 stop_production。",
        "parameters": _schema({"project": _proj_prop(),
                               "kind": {"type": "string",
                                        "description": "character / item / scene"},
                               "name": {"type": "string",
                                        "description": "资产名（从 list_characters 或剧本里拿）"}},
                              ["kind", "name"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/reset-asset",
                              {"project_name": a.get("project") or c.get("project"),
                               "kind": _p(a.get("kind")),
                               "name": _p(a.get("name"))}),
    },

    # ---------- 生产调度 ----------
    {
        "name": "start_production",
        "description": "启动全自动生产（24h 托管跑多集）。novel_id 可省略，会自动关联。",
        "parameters": _schema({"project": _proj_prop(), "novel_id": _STR}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autonomous/start",
                              {"project_name": a.get("project") or c.get("project"),
                               "novel_id": a.get("novel_id") or ""}),
    },
    {
        "name": "stop_production",
        "description": "停止全自动生产。⚠️ 实际是**全局暂停**（autopilot.pause），"
                       "会影响**所有项目**的托管生产，不只是当前项目；只想停当前项目请用 "
                       "disable_autopilot(project=…)。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autonomous/stop",
                              {"project_name": a.get("project") or c.get("project")}),
    },
    {
        "name": "resume_production",
        "description": "恢复被暂停的全自动生产。⚠️ 同 stop_production，这是**全局**恢复，"
                       "会影响所有项目。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autonomous/resume",
                              {"project_name": a.get("project") or c.get("project")}),
    },
    {
        "name": "enable_autopilot",
        "description": "开启某项目的托管（到点自动生产下一集）。",
        "parameters": _schema({"project": _proj_prop(), "novel_id": _STR}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/enable",
                              {"project": a.get("project") or c.get("project"),
                               "novel_id": a.get("novel_id") or ""}),
    },
    {
        "name": "disable_autopilot",
        "description": "关闭某项目的托管。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/disable",
                              {"project": a.get("project") or c.get("project")}),
    },
    {
        "name": "pause_autopilot",
        "description": "暂停**全局**托管（保留进度，可 resume）。⚠️ 这是全局开关，会影响所有项目；"
                       "若只想停某一个项目，请用 disable_autopilot(project=…)。",
        # ⚠️ 这里**不暴露 project**：/api/autopilot/pause 走的是 autopilot.pause()，
        # 它是全局开关（_STATE["paused"]），根本不看 project。
        # 之前 schema 里带了 project，会让模型以为能「只暂停某个项目」，
        # 实际却把全部项目一起停掉 —— 静默的越权，属于契约与语义不符。
        "parameters": _schema({}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/pause",
                              {"reason": "总控 AI 暂停托管"}),
    },

    # ---------- 昂贵动作（烧 GPU / 耗时） ----------
    {
        "name": "produce_episode",
        "description": "立即完整生产指定一集（走完整流水线 7 步：剧本→配音先行→资产→分镜→"
                       "视频→超分→成片）。耗时长，一次只能一集。注意：托管流水线以小说章节"
                       "为原文直接生产；「文学剧本→人工审核→改写」的两段式入口在前端剧集列表。",
        "parameters": _schema({"project": _proj_prop(),
                               "episode": {"type": "integer", "description": "章节序号，默认 1"}}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/autopilot/run-once",
                              {"project": a.get("project") or c.get("project"),
                               "episode_no": int(a.get("episode") or c.get("episode") or 1)}),
    },
    {
        "name": "retry_shot",
        "description": "重生成某一镜的分镜图（不动其它镜头）。用户说「第N镜重做」时用这个。",
        "parameters": _schema({"project": _proj_prop(),
                               "shot_id": {"type": "string", "description": "镜头 id，如 3 或 shot_03"},
                               "episode": _INT}, ["shot_id"]),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/storyboard/retry-shot",
                              {"project_name": a.get("project") or c.get("project"),
                               "shot_id": str(a.get("shot_id")),
                               "episode_no": a.get("episode") or c.get("episode")}),
    },
    {
        "name": "retry_shot_video",
        "description": "重生成某一镜的视频片段（分镜图不动）。mode: reference（默认）或 keyframe。",
        "parameters": _schema({"project": _proj_prop(),
                               "shot_id": {"type": "string", "description": "镜头 id"},
                               "episode": _INT,
                               "mode": _STR}, ["shot_id"]),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/video/retry-shot",
                              {"project_name": a.get("project") or c.get("project"),
                               "shot_id": str(a.get("shot_id")),
                               "mode": a.get("mode") or "reference",
                               "episode_no": a.get("episode") or c.get("episode")}),
    },
    {
        "name": "upscale_video",
        "description": "对成片做超分（FlashVSR）。scale 只支持 2/3/4。默认自动挑该项目最新成片，"
                       "并保留音轨。很慢，别对同一条片反复跑。",
        "parameters": _schema({"project": _proj_prop(),
                               "scale": {"type": "integer", "description": "倍率 2/3/4，默认 2"},
                               "video_path": {"type": "string", "description": "可选，指定输入视频绝对路径"}}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/upscale/video",
                              {k: v for k, v in {
                                  "project_name": a.get("project") or c.get("project"),
                                  "video_path": a.get("video_path"),
                                  "scale": int(a.get("scale") or 2),
                                  "attach_audio": True,
                              }.items() if v is not None}),
        "poll": {"status": "/api/upscale/status/{task_id}", "timeout": 3600},
    },
    {
        "name": "export_project",
        "description": "导出交付文件（剪映草稿 / FCPXML / SRT / 帧序列）。formats 为空则全导。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT,
                               "formats": _ARR_STR}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/export/run",
                              {k: v for k, v in {
                                  "project_name": a.get("project") or c.get("project"),
                                  "episode_no": a.get("episode") or c.get("episode"),
                                  "formats": a.get("formats"),
                              }.items() if v is not None}),
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        },
    }
    for t in TOOLS
]

# 降级模式（模型不支持 tools 协议时）用的工具清单文本
_TOOL_INDEX = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOLS)

SYSTEM_PROMPT = """你是这部漫剧的**总控导演 AI**，有权直接操作系统里的全部生产功能。

工作方式（重要）：
- 你**不需要请示用户**，也不许问「要不要我帮你做？」。收到指令后自己判断要动哪些环节，
  直接调用工具执行，最后用中文汇报「做了什么 + 结果 + 下一步建议」。
- 信息不够时（比如没说第几集）**自己取合理默认值**（默认第 1 集 / 当前集），
  执行完在汇报里说明你取了什么默认值。
- 先查后做：不确定项目状态时，先用 get_status / get_progress / get_qc_summary 等只读工具看一眼，
  再决定动什么。只读工具不花钱不烧卡，多用没关系。
- 一次决策里可以并行调用多个**互不依赖**的只读工具。
- 昂贵动作（produce_episode / retry_shot / retry_shot_video / upscale_video / export_project）
  单轮有次数上限，超了会被系统拒绝。
  **不要为了「保险」重复调用同一个昂贵动作**——完全相同的参数在冷却期内会被直接复用缓存结果。
- 工具返回 success:false 时，先读 error 判断原因（环境没配好 / 依赖产物不存在 / 参数非法），
  能修的自己修（比如先补跑「配音先行」、补齐角色参考音色，再生成视频），修不了就如实告诉用户卡在哪、缺什么。
- 超分(upscale_video) 是**异步长任务**，系统已自动等到任务真正结束才把结果给你。
  若结果里出现 `timeout: true`，说明任务**仍在后台运行**：请如实汇报「正在后台合成中」+
  当前阶段与进度，并让用户稍后再问，**绝对不能说成「已完成」**。
- 绝对不要调用工具去删除任何东西；系统没有给你删除能力。

【当前流水线口径（务必遵守，绝对不要提旧步骤）】
自动流水**固定 7 步、顺序不可改动**，与后端 `pipeline.STEP_SEQUENCE` 逐位一致：
1. script 剧本生成 → 2. tts_pre 配音先行（为**每个角色**生成一段参考音色）→
3. assets 资产（角色/物品/场景）→ 4. storyboard 分镜图 →
5. video 视频生成（整集一次生成）→ 6. upscale 超分（FlashVSR，fail-open）→ 7. final 成片合成。
⚠️ 已下线的旧步骤：`tts`（配音合成）、`mix`（音画对齐混音）、`keyframe`（尾帧）**均不在流水线内**。
   描述「后续步骤」时**绝对不要**再说「配音 → 混音」——这是过时口径；尾帧的手动生成入口
   仍在（前端分镜操作里），但自动流水线不再包含尾帧步骤。
H3 视频自带原生音轨（生成时已注入角色参考音色），**成片即带配音**，
   不存在「口型」「混音合成」环节；被问到声音时按这个口径回答。
两段式生产（2026-10-04）：前端剧集列表支持「文学剧本 → 人工审核 → 改写分镜表 → 按场次
   生成/单场重做」；被问到剧本审改、单场重做时指引用户去工作台剧集列表操作。

汇报风格：简短、说人话、讲结果，不复述工具返回的原始 JSON。

汇报进度时必须说清「在生产什么」（重要）：
- 用户问「跑到哪了 / 在生成什么 / 进度如何」时，**禁止只回「已完成 N 步」「正在执行」**这类
  空话——用户要的是「现在在拍哪一集、走到哪个环节、几成」。
- get_status 返回的 `current` 里已经备好了这些字段，直接取用、别自己编：
  - `describe`：**已经拼好的一句话**（如「第 1 集 · 第01章 废物觉醒 · 原文覆盖率校验 · 18%」）
    —— 汇报时**优先原样用它**，不要改写、不要翻译成英文。
  - `episode` / `title`：集号与章节标题；`percent`：百分比。
  - `steps_done`：本集已完成的环节列表；`step_stalled_sec`：当前环节已耗时（秒）。
- 标准汇报格式（照这个说，用中文）：
  「正在生产**第 N 集《章节标题》**，当前环节：**<环节名>**，进度 **X%**；
   本集已完成：剧本 → 资产；当前环节已跑 M 分钟。」
- `current` 为空（null）＝**当前没有在生产**。此时如实说「当前没有在跑生产」，
  并顺带说明托管开关状态（`enabled_count`）与下一集是什么，**不要**把上一次的进度说成正在跑。
- ⚠️ **`current` 为空时必须接着看 `last_run`（上一集生产结果）** —— 它是唯一能说明
  「上一集到底成没成」的字段，`current` 被清空后不会留下任何其它痕迹：
  - `last_run.ok == false` → **主动汇报**「第 N 集生产失败，原因：<last_run.error>」，
    并给出下一步建议（改配置 / 换 key / 处理「需人工介入」后重跑）。
    **绝对不许**只回一句「当前没有在跑生产」就把一次失败带过去 —— 用户会觉得你根本没在管。
  - `last_run.ok == true` → 简短说明上一集已完成、交付物在哪，再问是否需要继续下一集。
  - `last_run` 为空对象 → 说明这个项目**从未跑完过任何一集**，如实这么讲。
  - 生产的**失败原因**还可能出现在 `exceptions`（需人工介入的集）里，一并读出来告诉用户。
- `current.stall_warning` 非空时，说明该环节长时间没推进（多为模型重试 / ComfyUI 排队）：
  如实告知用户「卡在 XX 环节，已 N 分钟没推进」，**不要**说成失败，也**不要**说成正常。
- `other_project_running: true` 时（见下方项目纪律），只说别的项目在跑，**绝不**把它当本项目的进度。

启动自动生产前的沟通要求（重要）：
- 讨论风格 / 角色 / 题材之前，**先调用 get_novel** 读本项目绑定的原著（书名 + 开篇正文），
  用原著里的年代、人物、基调说话。**绝不允许**凭上下文里出现的其它项目名去猜这本小说是什么。
- 当用户说「开始生产」「启动托管」「开始跑」等指令时，**不要立即调用 start_production / enable_autopilot**
- 先调用 get_status 或 get_plan 查看当前项目状态，然后向用户询问/确认以下生产风格参数：
  1. 目标集数（要生产几集？）
  2. 风格偏好（画风、色调、镜头语言，从项目配置中读取已有设定）
  3. 是否需要超分
  4. 其他特殊要求
- 只有当用户明确确认了风格参数后，才调用生产工具
- 如果项目已有配置的风格，直接引用并确认，不要重复询问
- 汇报时把确认的风格参数清晰列给用户看，让用户知道你将按什么风格生产

项目纪律（极重要）：
- 你**只服务于当前会话绑定的那个项目**。所有带 project 参数的工具都必须指向它，
  不要因为某个项目「查不到」就换成别的项目继续干。
- 如果连不上当前项目（工具返回 404 / success:false），就**如实报告这个项目查不到**，
  并说明可能原因；**绝不允许**拿其他项目的数据来汇报。
- get_status 返回 `other_project_running: true` 表示**别的项目**正在生产，与本项目无关：
  不要把它当成本项目的进度、小说或报错来汇报（这正是「我新建项目、总控却谈旧项目」的成因）。
- 用户说「这个项目」「我的项目」时，指的就是会话绑定的项目，不要反问是哪个。
- 生产失败要**主动大声提醒**，不许静默跳过（2026-10-08 用户报的真实坑）：
  get_status 里若 `failure_pause` 非空（或 `needs_user` 为真 / exceptions > 0），
  说明某集生产失败已被系统自动暂停（stop_on_failure）—— 你**必须**在回复开头
  醒目标注「⚠ 第N集生产失败，任务已自动暂停」并说明失败原因，再给用户
  「查看/重跑/跳过」的处理建议。绝不能只汇报「当前空闲/正在做别的集」而把
  失败的集悄悄带过 —— 用户无法感知失败，这是被点名的缺陷。"""


# ===================== 内部调用 =====================

def _trim(obj, limit: int = MAX_RESULT_CHARS) -> str:
    """把工具结果压成喂给模型的短文本，避免上下文爆炸"""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(obj)
    if len(s) > limit:
        s = s[:limit] + f"…（已截断，原长 {len(s)} 字符）"
    return s


def _call_api(method: str, path: str, payload: dict = None) -> dict:
    """进程内直连调用自身路由（不经过网络）"""
    if _APP is None:
        return {"ok": False, "error": "agent 内核未绑定 Flask 应用"}
    try:
        client = _APP.test_client()
        fn = getattr(client, method.lower())
        resp = fn(path) if method.upper() == "GET" else fn(path, json=payload or {})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"内部调用异常：{e}",
                "trace": traceback.format_exc()[-400:]}
    try:
        data = resp.get_json(silent=True)
    except Exception:  # noqa: BLE001
        data = None
    if data is None:
        data = {"raw": (resp.data or b"")[:400].decode("utf-8", "ignore")}
    ok = 200 <= resp.status_code < 300
    if ok and isinstance(data, dict) and data.get("success") is False:
        ok = False
    return {"ok": ok, "status": resp.status_code, "data": data}


# ===================== 异步任务等待 =====================
#
# ⚠️ 2026-09-17 实测教训：配音 / 混音 / 超分这三个接口是**异步**的，POST 只返回
# {"task_id": ..., "status": "started"} 就立刻返回。而 agent 之前把「HTTP 200」等同于
# 「任务完成」，于是用户听到的是「配音已完成」——实际上后台任务还卡在「准备配音」阶段，
# 一个音频文件都没产出，下游混音随即 400「未找到配音清单」，用户永远拿不到带声成片。
# 这里给异步工具补上「派发 → 轮询状态接口到终态 → 如实汇报」的闭环。

POLL_INTERVAL_SEC = 5
POLL_DEFAULT_TIMEOUT_SEC = 1500      # 单轮等待上限（25 分钟，与 MAX_TURN_SEC 对齐）
_TERMINAL_OK = ("completed", "done", "success", "ok", "finished")
_TERMINAL_BAD = ("failed", "error", "cancelled", "canceled", "timeout")


def _extract_task_id(data) -> str:
    """从异步接口响应里取出任务 id（兼容 task_id / id / job_id 三种命名）"""
    if not isinstance(data, dict):
        return ""
    for k in ("task_id", "id", "job_id"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _poll_state(data: dict, pick: str) -> dict:
    """取出状态对象：mix 接口把任务嵌在 task 下，其余为平铺"""
    if not isinstance(data, dict):
        return {}
    if pick == "task" and isinstance(data.get("task"), dict):
        return data["task"]
    return data


def _await_task(poll: dict, task_id: str) -> dict:
    """轮询异步任务到终态；超时则如实返回「仍在后台运行」，绝不谎报完成"""
    tmpl = str(poll.get("status") or "")
    if not tmpl or not task_id:
        return {"ok": True, "task_id": task_id, "note": "已派发（该任务无状态接口，无法确认完成）"}
    timeout = float(poll.get("timeout") or POLL_DEFAULT_TIMEOUT_SEC)
    pick = str(poll.get("pick") or "flat")
    path = tmpl.format(task_id=quote(str(task_id), safe=""))
    t0 = time.time()
    state = {}
    while True:
        r = _call_api("GET", path)
        state = _poll_state(r.get("data") or {}, pick) or state
        status = str((state or {}).get("status") or "").lower()
        el = round(time.time() - t0, 1)
        if status in _TERMINAL_OK:
            return {"ok": True, "task_id": task_id, "status": status, "elapsed_sec": el,
                    "note": "异步任务已完成", "data": state}
        if status in _TERMINAL_BAD:
            return {"ok": False, "task_id": task_id, "status": status, "elapsed_sec": el,
                    "error": str((state or {}).get("error") or (state or {}).get("message")
                                 or "异步任务失败"),
                    "data": state}
        if el > timeout:
            return {"ok": False, "task_id": task_id, "status": status or "unknown",
                    "elapsed_sec": el, "timeout": True, "data": state,
                    "error": (f"等待 {int(timeout / 60)} 分钟仍未完成，任务仍在后台运行"
                              f"（当前阶段：{(state or {}).get('phase') or '未知'}，"
                              f"进度：{(state or {}).get('progress') or 0}%）。"
                              "请如实告知用户「正在后台合成中」，**不要声称已完成**。")}
        time.sleep(POLL_INTERVAL_SEC)


def _canon(args: dict) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(args)


def _audit(project: str, job_id: str, name: str, args: dict, result: dict):
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        path = os.path.join(AUDIT_DIR, f"audit-{datetime.now().strftime('%Y%m%d')}.jsonl")
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "job_id": job_id, "project": project, "tool": name,
            "args": args,
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "elapsed_sec": result.get("elapsed_sec"),
            "error": (result.get("error") or "")[:300],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("追加智能体执行记录失败（忽略）：%s", e)


# ===================== 工具执行（含护栏） =====================

def execute_tool(name: str, args: dict, ctx: dict, stats: dict, job_id: str = "") -> dict:
    tool = TOOL_MAP.get(name)
    if not tool:
        return {"ok": False, "error": f"未知工具：{name}（可用工具见工具清单）"}

    if _KILL["on"]:
        return {"ok": False, "blocked": True,
                "error": f"急停开关已触发（{_KILL['reason'] or '手动刹车'}），本轮后续动作全部中止"}

    if tool["expensive"] and stats.get("expensive", 0) >= MAX_EXPENSIVE:
        return {"ok": False, "blocked": True,
                "error": f"本轮昂贵动作已达上限 {MAX_EXPENSIVE} 次，拒绝执行 {name}。"
                         f"请先向用户汇报当前结果，需要继续请让用户重新下指令。"}

    # ⚠️ 项目钉死（2026-09-17 实测教训）：
    # 本会话绑定的项目就是用户当前打开的项目，绝不允许模型自己换成别的项目。
    # 之前 get_status 因 URL 拼接错误恒 404，模型于是改调 list_projects，
    # 然后「抓一个能读到的项目」继续干活 —— 用户看到的就是「我新建项目，对话却报告旧项目」。
    # 这里把 args.project 强制归一到会话项目，并留审计痕迹。
    bound = _p(ctx.get("project"))
    if bound and isinstance(args, dict):
        asked = _p(args.get("project"))
        if asked and asked != bound:
            _audit(bound, job_id, f"{name}#project_pinned",
                   {"requested": asked, "forced": bound},
                   {"ok": True, "note": "跨项目请求已钉回当前项目"})
            args = {**args, "project": bound}

    key = hashlib.md5((name + "|" + _canon({**args, "__p": ctx.get("project")})).encode("utf-8")).hexdigest()
    now = time.time()
    hit = _COOLDOWN.get(key)
    # ⚠️ 只读探针不进冷却：状态类数据缓存了会让模型看到 2 分钟前的旧进度，
    # 从而做出错误判断。冷却只用于「重复执行会真花钱/烧卡」的动作。
    if hit and tool["risk"] != "safe" and (now - hit[0]) < COOLDOWN_SEC:
        left = int(COOLDOWN_SEC - (now - hit[0]))
        return {"ok": True, "cached": True, "status": hit[1].get("status"),
                "note": f"{left}s 内同参数已执行过，直接复用上次结果（防重复烧 GPU）",
                "data": hit[1].get("data")}

    try:
        method, path, payload = tool["call"](args or {}, ctx)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"参数构造失败：{e}"}

    t0 = time.time()
    res = _call_api(method, path, payload)
    res["elapsed_sec"] = round(time.time() - t0, 1)
    if tool["expensive"]:
        stats["expensive"] = stats.get("expensive", 0) + 1

    # 异步工具：POST 返回 200 只代表「派发成功」，必须轮询到终态才能向用户汇报结果。
    # 否则会出现「配音已完成」这类谎报（实测教训，见上方说明）。
    if res.get("ok") and tool.get("poll"):
        tid = _extract_task_id(res.get("data"))
        if not tid:
            res = {"ok": False, "status": res.get("status"), "data": res.get("data"),
                   "error": (f"{name} 未返回任务 id，无法确认是否真的开始执行；"
                             "请如实告知用户「结果未知」，不要声称已完成")}
        else:
            dispatch = res.get("data")
            waited = _await_task(tool["poll"], tid)
            res = {
                "ok": bool(waited.get("ok")),
                "status": res.get("status"),        # 保留 HTTP 状态码（审计字段语义不变）
                "task_id": tid,
                "task_status": waited.get("status"),
                "dispatch": dispatch,               # 派发时的原始响应，便于排查
                "data": waited.get("data") or dispatch,
            }
            if waited.get("note"):
                res["note"] = waited["note"]
            if waited.get("error"):
                res["error"] = waited["error"]
            if waited.get("timeout"):
                res["timeout"] = True
            res["elapsed_sec"] = round(time.time() - t0, 1)

    if res.get("ok") and tool["risk"] != "safe":
        # 第 3 位记归属项目键（purge_project 按它摘除）；读取侧只取 [0]/[1]，
        # 旧形状 2 元组由 purge 侧 len(v) > 2 守卫兼容，TTL 120s 内自然淘汰。
        _COOLDOWN[key] = (now, res, _p(ctx.get("project")))
    _audit(ctx.get("project", ""), job_id, name, args or {}, res)
    return res


# ===================== 任务执行 =====================

def _new_job(project: str, message: str) -> dict:
    jid = hashlib.md5(f"{project}|{message}|{time.time()}".encode("utf-8")).hexdigest()[:12]
    job = {
        "id": jid, "project": project, "message": message,
        "status": "running", "steps": [], "reply": "", "error": "",
        "created": time.time(), "updated": time.time(),
        "expensive_used": 0,
    }
    with _LOCK:
        _JOBS[jid] = job
    return job


def get_job(jid: str):
    with _LOCK:
        job = _JOBS.get(jid)
        return dict(job) if job else None


def _persist_agent_reply(job):
    """P1-1：任务收尾时**立即**把总控最终回复写进聊天历史（在 agent 线程内完成）。

    旧实现只在 /api/agent/job/<id> 被前端轮询到时才落盘（app.py 的收尾钩子）——
    用户若在长任务（实测 13 分钟）期间离开页面/关标签，轮询中断，回复就永远没写进
    历史（当天 14:46/14:51 两条用户消息至今无 assistant 回复，即此因）。改为线程内
    收尾即落盘；前端轮询钩子仅作幂等兜底。

    竞态处理：线程与轮询钩子都可能在「已收尾」后各调一次本函数。为避免重复追加，
    先持锁**认领**（_persisting）再落盘，成功后置 _persisted；失败则回滚 _persisting，
    让兜底方再试。已认领/已完成时直接返回。
    """
    reply = (job.get("reply") or "").strip()
    if not reply:
        return
    with _LOCK:
        if job.get("_persisted") or job.get("_persisting"):
            return
        job["_persisting"] = True
        _JOBS[job["id"]] = job
    project = job.get("project") or ""
    try:
        import ai_chat
        import config
        history = ai_chat.load_history(config.AI_CHAT_HISTORY_PATH)
        ai_chat.append_message(history, "assistant", reply, project)
        ai_chat.save_history(config.AI_CHAT_HISTORY_PATH, history)
        with _LOCK:
            job["_persisted"] = True
            job.pop("_persisting", None)
            _JOBS[job["id"]] = job
    except Exception as e:  # noqa: BLE001
        # 落盘失败不阻断任务收尾；回滚认领，让前端轮询兜底再试一次。
        with _LOCK:
            job.pop("_persisting", None)
            _JOBS[job["id"]] = job
        _log_persist_fail(e)


def _log_persist_fail(e):
    try:
        if _APP is not None and getattr(_APP, "logger", None):
            _APP.logger.warning("总控回复落历史失败（线程内）：%s", e)
        else:
            import logging
            logging.getLogger(__name__).warning("总控回复落历史失败（线程内）：%s", e)
    except Exception:  # noqa: BLE001
        pass


def persist_job_reply(job_id: str):
    """前端轮询兜底入口（幂等）：线程内已落盘时直接返回，否则再试一次。

    与线程内 `_finish` 的即时落盘共用同一份认领/去重逻辑，保证最终回复
    **只追加一次**到聊天历史（不重复、不丢失）。
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
    _persist_agent_reply(job)


def _finish(job, status, reply="", error=""):
    job["status"] = status
    job["reply"] = reply
    job["error"] = error
    job["updated"] = time.time()
    with _LOCK:
        _JOBS[job["id"]] = job
    # P1-1：有最终回复就立即落盘（不再依赖前端轮询触发）。
    _persist_agent_reply(job)


def _fallback_parse_actions(text: str):
    """模型不支持 tools 协议时的降级解析：从正文里抠 JSON 动作块"""
    m = re.search(r"```(?:json)?\s*(.+?)```", text or "", re.S)
    raw = m.group(1) if m else (text or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return [], text
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return [], text
    actions = obj.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    out = []
    for a in actions:
        if isinstance(a, dict) and a.get("tool"):
            out.append({"name": str(a["tool"]), "args": a.get("args") or {}})
    return out, obj.get("reply") or re.sub(r"```.*?```", "", text or "", flags=re.S).strip()


def run_job(job: dict, ep: dict, history: list, timeout: int):
    """agent 主循环（在后台线程里跑）"""
    project = job["project"]
    ctx = {"project": project, "episode": None}
    stats = {"expensive": 0}
    started = time.time()
    text_mode = False

    try:
        from llm_client import LLMClient, LLMError
    except Exception as e:  # noqa: BLE001
        _finish(job, "failed", error=f"加载 LLM 客户端失败：{e}")
        return

    try:
        # config 直接由调用方传入（已配置好的 chat 模块 endpoint），config_path 不再读盘
        client = LLMClient("", config=ep, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        _finish(job, "failed", error=f"初始化模型失败：{e}")
        return

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-8:]:
        role = h.get("role")
        if role in ("user", "assistant") and h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:2000]})
    messages.append({"role": "user", "content": job["message"]})

    for step in range(MAX_STEPS):
        if _KILL["on"]:
            job["steps"].append({"tool": "_kill", "ok": False, "summary": "急停，本轮中止"})
            _finish(job, "killed", reply="已触发急停，本轮动作全部中止。")
            return
        if time.time() - started > MAX_TURN_SEC:
            job["steps"].append({"tool": "_timeout", "ok": False, "summary": "超出单轮时长上限"})
            _finish(job, "timeout", reply=f"单轮执行超过 {MAX_TURN_SEC}s，已自动停止。"
                                          f"已完成 {len(job['steps'])} 步，请查看上面的执行记录。")
            return

        # ---- 让模型决策 ----
        try:
            if text_mode:
                content = client.chat(messages, temperature=0.4, max_tokens=2048,
                                      timeout=min(timeout, 120))
                calls, final_reply = _fallback_parse_actions(content)
            else:
                resp = client.chat_tools(messages, TOOL_SCHEMAS, temperature=0.3,
                                         max_tokens=2048, timeout=min(timeout, 120))
                calls = [{"id": c["id"], "name": c["name"], "args": _safe_args(c["arguments"])}
                         for c in (resp.get("tool_calls") or [])]
                final_reply = resp.get("content") or ""
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if not text_mode and ("400" in msg or "tool" in msg.lower() or "unsupported" in msg.lower()):
                text_mode = True
                messages.append({
                    "role": "user",
                    "content": "（系统提示：当前模型不支持 function-calling 协议。"
                               "请改为直接输出一个 JSON 代码块，格式：\n"
                               "```json\n{\"actions\":[{\"tool\":\"工具名\",\"args\":{...}}],"
                               "\"reply\":\"给用户的话\"}\n```\n"
                               "没有要执行的动作时 actions 填空数组。"
                               f"可用工具：\n{_TOOL_INDEX}）",
                })
                # 模型决策本身可能耗时很久（网关卡顿 / 大上下文），
                # 回到循环顶部前再做一次时间检查，避免卡死。
                if time.time() - started > MAX_TURN_SEC:
                    job["steps"].append({"tool": "_timeout", "ok": False,
                                         "summary": "模型决策超上限，中止本轮"})
                    _finish(job, "timeout",
                             reply=f"模型决策耗时过长（超过 {MAX_TURN_SEC}s），已自动停止。")
                    return
                continue
            _finish(job, "failed", error=f"模型调用失败：{msg[:300]}")
            return

        # 模型决策本身耗时检查：防止 LLM 网关长时间阻塞导致任务永远 running
        if time.time() - started > MAX_TURN_SEC:
            job["steps"].append({"tool": "_timeout", "ok": False,
                                 "summary": "模型决策超上限，中止本轮"})
            _finish(job, "timeout",
                   reply=f"模型决策耗时过长（超过 {MAX_TURN_SEC}s），已自动停止。")
            return

        # ---- 没有动作 → 结束 ----
        if not calls:
            _finish(job, "done", reply=final_reply or "（模型没有给出回复）")
            return

        # ---- 执行动作 ----
        messages.append({
            "role": "assistant",
            "content": final_reply or " ",
            **({"tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                for c in calls]} if not text_mode else {}),
        })

        for c in calls:
            name, args = c["name"], c.get("args") or {}
            res = execute_tool(name, args, ctx, stats, job_id=job["id"])
            job["steps"].append({
                "tool": name, "args": args,
                "ok": bool(res.get("ok")),
                "cached": bool(res.get("cached")),
                "blocked": bool(res.get("blocked")),
                "elapsed_sec": res.get("elapsed_sec"),
                "summary": res.get("note") or (res.get("error") or "")[:200]
                or (f"ok ({res.get('elapsed_sec')}s)" if res.get("ok") else "失败"),
                "result": _trim(res.get("data")),
            })
            job["updated"] = time.time()
            tool_msg = {"role": "tool", "tool_call_id": c["id"],
                        "content": _trim({"ok": res.get("ok"), "error": res.get("error"),
                                          "note": res.get("note"), "data": res.get("data")}, 2400)}
            if text_mode:
                messages.append({"role": "user", "content":
                    f"[工具结果] {name}: {tool_msg['content']}"})
            else:
                messages.append(tool_msg)

        job["expensive_used"] = stats.get("expensive", 0)

        # 动作执行可能阻塞很久（异步工具等任务结束），回到循环顶部前再查时间
        if time.time() - started > MAX_TURN_SEC:
            job["steps"].append({"tool": "_timeout", "ok": False,
                                 "summary": "动作执行超上限，中止本轮"})
            _finish(job, "timeout",
                   reply=f"动作执行耗时过长（超过 {MAX_TURN_SEC}s），已自动停止。")
            return

    _finish(job, "done",
            reply=f"已完成 {len(job['steps'])} 步，但单轮步数达到上限 {MAX_STEPS}。"
                  f"如需继续请再下一条指令。")


def _safe_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ===================== 对外入口 =====================

def start_job(message: str, project: str, ep: dict, history: list, timeout: int = 120) -> dict:
    """启动一个 agent 任务（后台线程）。同一项目同时只能跑一个。"""
    proj_key = project or "__global__"
    if _KILL["on"]:
        return {"ok": False, "error": "急停开关处于开启状态，请先关闭后再下发指令"}
    with _LOCK:
        if proj_key in _BUSY:
            return {"ok": False, "error": "该项目已有一个 AI 任务在执行，请等它结束"}
        _BUSY.add(proj_key)

    job = _new_job(project, message)
    job["steps"] = []

    def _runner():
        try:
            run_job(job, ep, history, timeout)
        except Exception as e:  # noqa: BLE001
            _finish(job, "failed", error=f"内核异常：{e}")
        finally:
            with _LOCK:
                _BUSY.discard(proj_key)

    threading.Thread(target=_runner, daemon=True).start()
    return {"ok": True, "job_id": job["id"], "project": project}


def set_kill(on: bool, reason: str = ""):
    _KILL["on"] = bool(on)
    _KILL["reason"] = reason
    _KILL["at"] = time.time()
    return dict(_KILL)


def kill_state():
    return dict(_KILL)


def tool_index():
    return [{"name": t["name"], "description": t["description"],
             "risk": t["risk"], "expensive": bool(t["expensive"])} for t in TOOLS]


def cleanup_jobs():
    now = time.time()
    with _LOCK:
        for jid in [k for k, v in _JOBS.items() if now - v.get("updated", now) > JOB_TTL_SEC]:
            _JOBS.pop(jid, None)


def purge_project(names: list) -> dict:
    """项目删除时摘除总控内核里按项目键组织的**进程内运行态**（关联清理钩子）。

    agent_core 自身**不持久化**按项目隔离的对话：总控对话（用户消息 / 最终回复）
    都写进 ai_chat 的 chat_history.json 项目桶与 archive 归档（由 ai_chat.purge_project
    摘除，见 project_store.delete_project）。本模块只剩三份**进程内**状态按项目键组织：

      1. _JOBS     —— 任务表（steps / 最终回复），TTL 1 小时；不摘的话同名重建后
                      /api/agent/job/<id> 仍能读到旧项目任务（含旧回复）。
      2. _BUSY     —— 单项目互斥占位（start_job 的 proj_key）；旧项目任务若在删除时
                      仍在跑，不摘会让同名重建的项目拿到「该项目已有一个 AI 任务在执行」。
      3. _COOLDOWN —— 同参数冷却缓存（键 md5 内嵌项目键，见 execute_tool）；
                      不摘的话同名重建后 120s 冷却窗口内同参数工具会直接复用
                      **旧项目**的执行结果。

    ``names`` 是项目的别名并集（dir_key / 显示名 / 各侧目录键变体，来自
    project_store._project_alias_names）；匹配时对每个别名再取 canonical
    （与 ai_chat.project_key 同规则，惰性导入失败则退回原样名，只降级不阻断）。
    审计流水 output/agent/audit-<date>.jsonl 是**跨项目**审计记录（条目内嵌 project
    字段，仅 /api/agent/log 运维视图读取，不进对话面板），**保留不删**。
    本函数永不抛错（关联簿记语义：失败不阻断项目删除），返回摘除统计供汇总。
    """
    alias: set = set()
    for n in (names or []):
        n = str(n or "").strip()
        if not n:
            continue
        alias.add(n)
        try:
            import ai_chat  # 惰性导入：与 _settings_keys_hint 同风格，避免顶层耦合
            alias.add(ai_chat.canonical_project_key(n))
        except Exception:  # noqa: BLE001 - 规则库不可用就按原样名匹配（降级，不阻断）
            pass
    if not alias:
        return {"jobs_removed": 0, "busy_cleared": 0, "cooldown_cleared": 0,
                "names": []}

    with _LOCK:
        jobs_removed = 0
        for jid in [jid for jid, j in _JOBS.items()
                    if str(j.get("project") or "").strip() in alias]:
            _JOBS.pop(jid, None)
            jobs_removed += 1
        busy_cleared = len(_BUSY & alias)
        _BUSY.difference_update(alias)
        cooldown_cleared = 0
        for k in [k for k, v in _COOLDOWN.items()
                  if len(v) > 2 and str(v[2] or "").strip() in alias]:
            _COOLDOWN.pop(k, None)
            cooldown_cleared += 1
    return {"jobs_removed": jobs_removed, "busy_cleared": busy_cleared,
            "cooldown_cleared": cooldown_cleared, "names": sorted(alias)}
