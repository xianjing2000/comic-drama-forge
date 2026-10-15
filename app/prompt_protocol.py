# -*- coding: utf-8 -*-
"""分镜图提示词「协议标记」—— 生成端与质检端共用的单一事实源（叶子模块）。

为什么单独拆出来（2026-10-08 解耦）：
    这些标记串原本定义在 prompt_qc 里。prompt_enhance（LLM 增强层）只为几个标记串就
    模块级 import 整个质检模块，于是 prompt_enhance 与 prompt_qc 互为静态依赖
    （prompt_qc 又要在 preflight 内部函数里 import prompt_enhance）。

    标记串是纯常量、不依赖任何业务逻辑，因此下沉为叶子模块：
      · prompt_qc 从这里 import 并保留同名再导出（app.py 等历史调用点零改动）；
      · prompt_enhance 从这里 import 标记，只有在真正需要 check_prompt / KIND_LABELS
        时才惰性 import prompt_qc → 静态环消除。

⚠️ 判据必须与生成端同源这条铁律不变：这些串与 comfyui_client.build_storyboard_prompt
   的骨架一一对应，改任一处都要同步。
"""
from typing import List, Tuple

# --------------------------------------------------------------------------- #
# 分镜图提示词：骨架标记（与 build_storyboard_prompt 一一对应）
# --------------------------------------------------------------------------- #
# ⚠️ 2026-09-25：图片链路切到 QwenImage2.1 后，提示词协议从中文分节（【取景】…）
# 改为官方 Prompt Rewriter 的英文 <imageN> 协议（TASK / PRIMARY CANVAS / IDENTITY /
# REFERENCE ROLES / PRESERVE，见 comfyui_client.build_storyboard_prompt 的 docstring）。
# 本层的判据必须与生成端**同源**，否则会出现「生成端一个标准、质检端另一个标准」的
# 历史坑（景别判定曾因两端标准不同，把 43% 的镜头误判为不合格）。
SB_MARK_TASK = "TASK:"
#: 景别硬约束（旧版标记，保留兼容存量数据：2026-09-25 之前生成的提示词里是中文）
SB_MARK_FRAMING_LEGACY = "景别（必须严格遵守）"
SB_MARK_FRAMING_NEW = "FRAMING (must be strictly followed)"
#: 景别**未指定**时的显式声明（camera 只给了机位/运镜）
#: ⚠️ 此时提示词里不含任何景别词，若仍按「必须有 FRAMING 硬约束」判，会整批假红。
SB_MARK_FRAMING_UNSPEC = "FRAMING (not specified"
#: 画面内容段（新旧两种写法）
SB_MARK_CONTENT = "SCENE AND ACTION:"
SB_MARK_CONTENT_LEGACY = "【画面内容】"
#: 参考图职责段（官方协议要求每张图有唯一职责）
SB_MARK_REF_USAGE = "REFERENCE ROLES:"
SB_MARK_REF_USAGE_LEGACY = "参考图用途"
#: 画布/身份锚点段
SB_MARK_CANVAS = "PRIMARY CANVAS:"
SB_MARK_IDENTITY = "IDENTITY:"
#: 3D 导演台「构图基准图」段（2026-09-27）：带了站位基准图就必须声明它的职责，
#: 否则模型会把人偶当成身份基准（复刻灰彩色身体/无面头部）。
SB_MARK_BLOCKING = "COMPOSITION BASELINE:"
#: 保留子句（官方 Preservation Clause）
SB_MARK_PRESERVE = "PRESERVE:"
SB_MARK_PRESERVE_LEGACY = "【禁令】"
#: 保留子句的 blanket 写法（官方推荐，避免逐项罗列反复触发生成）
SB_PRESERVE_BLANKET = "Keep all untargeted content unchanged"
#: 风格段（新旧两种写法）
SB_MARK_STYLE = "STYLE:"
SB_MARK_STYLE_LEGACY = "【风格】"
#: 无参考图时的显式声明
SB_MARK_NO_REF = "No reference image is provided for this shot"
SB_MARK_NO_REF_LEGACY = "本镜无参考图"
#: 身份必须指向参考图（官方要点：不要用文字重述五官）
SB_MARK_IDENTITY_FROM_REF = "Preserve the exact identity from"
#: 中文「逐格写死」九宫格范式（storyboard_grid_main，2026-10-07 引入）
#: ⚠️ 它是**新范式**，但段名是中文（不含 TASK:/PRESERVE:），因此 _new_protocol 必须
#: 额外认它 —— 否则会被当成「旧中文分节协议」，要求【画面内容】/「景别（必须严格遵守）」，
#: 而逐格版两者都没有 → **每镜假红 2 条**（2026-10-09 实跑实测到，10 条教训都栽在这上面）。
SB_MARK_GRID_ZH_LAYOUT = "画面布局与内容"
SB_MARK_GRID_ZH_PANEL = "分镜1（"
#: 逐格清单**最后一格**的编号前缀：prompt_qc 用它判「9 格齐整」（3×3 布局与左下角
#: 数字标注不会断档）。与 SB_MARK_GRID_ZH_PANEL 同族（生成端逐格写「分镜N（」），
#: 判据只差编号 —— 两串必须同处定义、一起改。
SB_MARK_GRID_ZH_PANEL_LAST = "分镜9（"
#: 逐格版九宫格「差异硬约束」的标题串（storyboard_grid_main 模板正文必须保留它；
#: prompt_qc 判「模型倾向复刻同一构图」看的正是这个字样）。
SB_MARK_GRID_ZH_DIFF = "格间差异"
#: 逐格版模板的「防文字上屏」措辞（有台词时注入）
SB_MARK_GRID_ZH_NO_TEXT = "严禁出现任何台词文字或字幕"
#: 逐格版**无台词**时的防文字措辞（生成端 speech_note 走无台词分支时注入它；
#: 此时本就不会画字，判据上它与 SB_MARK_GRID_ZH_NO_TEXT 认其一即可）。
SB_MARK_GRID_ZH_SPEECH_OFF = "不出现开口说话的口型"

#: 无文字禁令（新旧两种措辞）
SB_MARK_NO_TEXT = "must not contain any text"
SB_MARK_NO_TEXT_LEGACY = "不得出现任何文字、字幕、台词文本、水印"


# --------------------------------------------------------------------------- #
# 中文逐格版模板守卫：判据串必须同时存在于**模板**里（2026-10-12 t7）
# --------------------------------------------------------------------------- #
# 背景（c4d3165 事故）：判据串是「生成端与质检端同一把尺子」的唯一凭据。改模板的人
# 顺手删掉这些字样 → render 出的提示词不再含该判据 → prompt_qc 把**模板缺陷**误报成
# **模型缺陷**（每镜假红，重试与自愈越修越远，还会把假教训写进教训库）。
# 故把「模板必须含这些字样」写成**断言**：改模板的人第一次跑链路就撞上。
#: 模板正文必须含的判据串（标签 → 判据串）。
#: ⚠️ 只收「模板静态持有」的串：逐格清单（SB_MARK_GRID_ZH_PANEL / _PANEL_LAST）与
#:    防文字措辞（SB_MARK_GRID_ZH_NO_TEXT / _SPEECH_OFF）由代码在 render 时注入，
#:    模板里本来就没有，**不能**在这里要求 —— 否则守卫会对自己误报。
GRID_ZH_TEMPLATE_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("逐格布局段", SB_MARK_GRID_ZH_LAYOUT),
    ("差异硬约束段", SB_MARK_GRID_ZH_DIFF),
)


def missing_grid_zh_template_markers(template_text: str) -> List[str]:
    """模板正文里缺失的判据串标签（空列表 = 齐全）。"""
    _t = str(template_text or "")
    return [label for label, mark in GRID_ZH_TEMPLATE_MARKERS if mark not in _t]


def assert_grid_zh_template_markers(template_text: str,
                                    source: str = "") -> None:
    """守卫：模板必须含全部判据串，缺则 ``AssertionError``（**刻意不 fail-open**）。

    为什么不做成「只记一条日志」：这些字样缺失不会自愈，只会稳定地把模板缺陷记成
    模型缺陷（c4d3165 就是这么回归的）。失败要足够响，改模板的人才会当场发现。
    """
    _miss = missing_grid_zh_template_markers(template_text)
    if _miss:
        raise AssertionError(
            "九宫格逐格版模板缺少判据串：" + "、".join(_miss)
            + f"（模板来源：{source}）。这些字样必须与生成端同源 —— 删掉它们，"
              "render 出的提示词就不再含该判据，prompt_qc 会把模板缺陷误报成模型缺陷"
              "（2026-10-12 c4d3165）。"
        )


# --------------------------------------------------------------------------- #
# 提示词预检（prompt_qc）缓存：判据键 + 统计口径的**单一来源**（2026-10-12 t11）
# --------------------------------------------------------------------------- #
# 背景（用户可感知的后果）：预检缓存此前「看不见」——
#   · 缓存是否生效、命中率多少，日志与接口里都查不到（stats 函数零调用点）；
#   · 缓存键里**没有模式/开关**，同一份提示词先按 block 判、后按 repair 判会复用旧结论
#     （t7 实测：verify_prompt_qc.py 在 MJSCXT_PROMPT_QC_CACHE=0 下有 4 项失败，
#      开缓存后「消失」—— 不是修好了，是被旧结论顶替了）。
# 与「跨集串数据」是同一类风险：看不见的缓存最容易串。故这里把两件事单点化：
#   ① 键里必须包含的「影响判决的 cfg 字段」（prompt_qc 与将来任何缓存实现共用）；
#   ② 计数/命中率的字段名与诊断行格式（生产端 prompt_qc、消费端 qc_client 共用）。
# ⚠️ 项目/集/镜/提示词内容**不进键**的理由见 PROMPT_QC_CFG_DECISION_KEYS 上方注释。
# --------------------------------------------------------------------------- #
#: 预检判决只取决于这些 cfg 字段（prompt_qc.prompt_qc_ready / prompt_qc_mode）——
#: 它们是缓存键的一部分。改了这里的字段集合 = 改了缓存语义，必须同步 prompt_qc 的
#: _pf_cache_key（本常量是唯一来源）。
PROMPT_QC_CFG_DECISION_KEYS: Tuple[str, ...] = ("prompt_enabled", "prompt_mode")


def prompt_qc_cfg_decision_key(cfg) -> str:
    """把「影响预检判决的 cfg 字段」压成稳定的短串（进缓存键）。

    为什么必须进键：同一份提示词在不同模式下结论**不同**（warn 不改动 / repair 自愈后
    放行 / block 有任何 issue 就拦），开关还可能整个跳过预检。不把它们并入键，
    先 block 再 repair 就会直接复用 block 的旧结论，而日志只会说「命中缓存」。

    故意**只取这两个键**、不把整个 cfg 序列化：cfg 里还有 base_url / api_key / 超时 /
    重试轮数等与判决无关、却会被逐镜改动的字段，全量入键只会让命中率归零。
    取值口径与 prompt_qc 的读取方式同构（缺失/None 视为空串），保证同 cfg 同串。
    """
    _cfg = cfg if isinstance(cfg, dict) else {}
    parts = []
    for _k in PROMPT_QC_CFG_DECISION_KEYS:
        _v = _cfg.get(_k)
        parts.append("%s=%s" % (_k, "" if _v is None else str(_v)))
    return "|".join(parts)


#: 预检缓存统计的**稳定字段名**（key -> 含义）。消费方（qc_client 的统计/日志/诊断）
#: 一律按这些名字取值，不要再各自拼字典；数据由 prompt_qc.prompt_qc_cache_stats() 生产
#: （size/hit/miss/capacity），本模块只负责补 enabled/hit_rate 与统一措辞。
PROMPT_QC_CACHE_STAT_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("enabled", "缓存开关当前是否生效（MJSCXT_PROMPT_QC_CACHE）"),
    ("size", "缓存条目数"),
    ("capacity", "容量上限（FIFO 淘汰）"),
    ("hit", "命中次数"),
    ("miss", "未命中次数"),
    ("hit_rate", "命中率（hit/(hit+miss)，无请求时为 0.0）"),
    ("last_key", "最近一次缓存查询的键摘要（sha1 前 12 位，便于对账）"),
    ("last_kind", "最近一次缓存查询的提示词类型"),
)

#: 与「跨模式串味」有关的两件事实，写在这里以免后人重犯：
#:   · prompt_qc._pf_cache_key 的当前口径 = sha1(kind|style|ref_count|expect_refs|
#:     ctx 的 8 个语义字段|prompt 全文) —— 判决输入齐全，但**不含 cfg 的模式/开关**；
#:   · 于是同一条提示词先按 block 判、再按 repair 判会复用旧结论（t7 已实测登记：
#:     .workbuddy/test/verify_prompt_qc.py 关缓存时 4 项失败、开缓存「转绿」）。
#: 本模块提供下面两个函数做**在野的等价保证**（不改 prompt_qc 源码也能不串味）：
#:   · prompt_qc_cfg_decision_key(cfg) —— 「影响判决的 cfg」的稳定标识；
#:   · isolate_prompt_qc_cache(cfg) —— 配置变化时立刻清空预检缓存。
#: ⚠️ 根治方案仍需把 PROMPT_QC_CFG_DECISION_KEYS 并进 _pf_cache_key（补丁见 t11 output）。


def prompt_qc_cache_hit_rate(hit, miss) -> float:
    """命中率（0.0~1.0）；一次请求都没有时返回 0.0（不返回 NaN，避免 JSON 里出现 nan）。"""
    try:
        _h = int(hit or 0)
        _m = int(miss or 0)
    except (TypeError, ValueError):
        return 0.0
    _n = _h + _m
    return round(_h / float(_n), 4) if _n > 0 else 0.0


def format_prompt_qc_cache_diag(stats: dict = None, source: str = "") -> str:
    """把预检缓存统计渲染成**一行**诊断文本（日志与接口共用同一措辞）。

    ``stats`` 为 None / 非 dict 时返回空串 —— 调用方据此静默跳过，不要写半截日志。
    """
    if not isinstance(stats, dict):
        return ""
    _src = ("｜来源=%s" % source) if source else ""
    _recent = ""
    if stats.get("last_key"):
        _recent = "｜最近查询=%s[%s]" % (stats.get("last_kind") or "?", stats.get("last_key"))
    return ("提示词预检缓存｜启用=%s｜命中=%s 未命中=%s 命中率=%.4f｜条目=%s/%s%s%s"
            % ("是" if stats.get("enabled") else "否",
               stats.get("hit", 0), stats.get("miss", 0),
               prompt_qc_cache_hit_rate(stats.get("hit"), stats.get("miss")),
               stats.get("size", 0), stats.get("capacity", 0), _recent, _src))


# --------------------------------------------------------------------------- #
# 跨模式/跨配置的缓存隔离（2026-10-12 t11）——**范围受限下的等价保证**
# --------------------------------------------------------------------------- #
# 为什么需要它：prompt_qc._pf_cache_key 的口径里**没有** cfg 的模式/开关字段，
# 于是同一条提示词从 block 切到 repair（或反向）会复用上一次的结论。
# t7 已实测登记：.workbuddy/test/verify_prompt_qc.py 在 MJSCXT_PROMPT_QC_CACHE=0 下
# 有 4 项失败（warn/block/关闭模式三项 + audio warn 一项），开缓存后「转绿」——
# 不是判定对了，是被旧结论顶替。这属于「不同集/不同镜复用错误结果」同类风险。
#
# 为什么要在这里做（而不是直接改 _pf_cache_key）：t11 的 inScope 是 qc_client.py +
# prompt_protocol.py（prompt_qc.py 被 t7 占用）。本函数提供**不改 prompt_qc 源码**
# 的等价保证：只要判决配置变了，立刻清空预检缓存 —— 宁可重算一次，也绝不复用
# 另一套模式下的结论。根治方案（把配置并进键，命中率不降）的补丁已随 t11 output
# 交接，落到 prompt_qc 后本函数即可退化为纯防御。
#
# 代价与边界：
#   · 只在**调用方显式调用**时检查（qc_client 的质检入口/统计出口各一处）；
#     未调用前若已发生配置切换，缓存仍可能短暂串味 —— 故它是「在野保证」，不是根治。
#   · 清空是整体清空（缓存实例不按配置分桶），切换后会有一轮未命中。
#   · 全程 fail-open：任何异常都不影响预检本身。
# --------------------------------------------------------------------------- #
_PQ_CACHE_ISOLATION = {"cfg": None, "resets": 0, "last_reason": ""}


def _prompt_qc_cache_clear() -> int:
    """清空 prompt_qc 的进程内预检缓存，返回清掉的条数（拿不到就返回 0）。

    只依赖公开/既有属性（_PF_CACHE/_PF_LOCK/_PF_CACHE_SIZE），不做任何猴子补丁、
    不替换函数 —— prompt_qc 未加载或结构变了就静默跳过。
    """
    try:
        import sys
        _mod = sys.modules.get("prompt_qc")
        if _mod is None:
            return 0
        _cache = getattr(_mod, "_PF_CACHE", None)
        if not isinstance(_cache, dict):
            return 0
        _lock = getattr(_mod, "_PF_LOCK", None)
        if _lock is not None:
            with _lock:
                _n = len(_cache)
                _cache.clear()
            return _n
        _n = len(_cache)
        _cache.clear()
        return _n
    except Exception:
        return 0


def isolate_prompt_qc_cache(cfg=None, force: bool = False) -> dict:
    """判决配置变了就清空预检缓存；返回
    ``{changed, cleared, cfg, resets, reason, first}``。

    ``changed``：本次是否判定为「需要清空」（**首次调用也算** —— 缓存出生时的配置未知，
                清空一次是唯一安全的起点；这只会发生在每进程第一次检查）。
    ``cleared``：真正清掉的条目数。
    ``first``：本次是否为进程内第一次检查。
    ``force=True``：无条件清空（诊断/手动失效用），仍会更新快照。
    """
    _key = prompt_qc_cfg_decision_key(cfg)
    _st = _PQ_CACHE_ISOLATION
    _prev = _st["cfg"]
    _first = _prev is None
    _changed = _first or (_prev != _key)
    _cleared = 0
    if _changed or force:
        _cleared = _prompt_qc_cache_clear()
        _st["resets"] = int(_st["resets"]) + 1
        if _first:
            _st["last_reason"] = "首次检查（缓存起点未知，清空一次）cfg=%s" % _key
        elif force and _prev == _key:
            _st["last_reason"] = "force（手动失效）cfg=%s" % _key
        else:
            _st["last_reason"] = "配置切换 %s -> %s" % (_prev, _key)
    if _prev != _key:
        _st["cfg"] = _key
    return {"changed": bool(_changed), "cleared": int(_cleared), "cfg": _key,
            "resets": int(_st["resets"]), "reason": _st["last_reason"],
            "first": bool(_first)}


# --------------------------------------------------------------------------- #
# 刻意**不去重**的跨文件重复中文串（2026-10-12 t7 盘点）
# --------------------------------------------------------------------------- #
# 盘点口径：``app/*.py`` 里长度 >= 4、单行的中文字符串字面量，出现在 >= 2 个文件的
# 共 **185** 个（AST 扫描）。全部抽常量属于过度工程 —— 下面五类重复是**巧合**，不是
# 「同一判据的两处」，抽到公共模块只会把无关业务绑死：
#   ① 日志/告警文案（含 %s 占位符）：例「清理临时文件失败（忽略）：%s」9 处、
#      「ffprobe 失败」2 处。每条日志的上下文与生命周期独立，措辞属实现细节。
#   ② 外部工具契约串：例「ffprobe 返回 」「ffmpeg 调用失败：」。它们对齐的是外部程序
#      的输出，不是内部判据，抽常量并不消除漂移风险。
#   ③ 展示名/阶段标签：例「剧本生成」5 处、「配音合成」3 处。同一个词在不同模块含义
#      可能不同（统计桶名 / 日志前缀 / 插件标题），强绑成一处反而更脆。
#   ④ 领域数据与示例文本：例「红色墨水（普通墨水，绝非血迹）」（生成端与剧本端各一份）、
#      「天合科技员工证」。这些是**值**不是判据，重复出于巧合。
#   ⑤ 文件名与编号模式：例「minimax_h3_director_二采_加速.json」「h3_director_r2v_单采.json」。
# 与它们相反，**判据串必须单一来源**（上面的 SB_MARK_*）：判据漂移会直接造成假红/漏判，
# 而「两端判据不同」正是本项目最大的历史坑（景别判定曾把 43% 的镜头误判为不合格）。
#
# ⚠️ 已下沉（2026-10-12 t34）：qc_client.CRITICAL_ISSUE_KEYWORDS ↔ sb_helpers 的
#   结构性缺陷词表已合并为下方 STRUCTURAL_DEFECT_KEYWORDS，两侧只引用同一常量。
#   仍待交接：storyboard_helpers._storyboard_prompt_structurally_ok 里的逐格版判据串。
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 结构性 / 关键缺陷词表（单一来源，2026-10-12 t34）
# --------------------------------------------------------------------------- #
# 背景：此前「同一套判据」存在两处拷贝 ——
#   · qc_client.CRITICAL_ISSUE_KEYWORDS（质检硬阻断，find_critical_issues 带否定语境过滤）；
#   · sb_helpers._SB_STRUCTURAL_DEFECT_KEYWORDS（分镜软放行豁免，_sb_structural_defect）。
# 两侧靠注释手工同步，已多次漂移：T18 实测「九宫格手指畸变」硬闸门判阻断、软放行闸门
# 却不认 → 畸变图照样被软放行入库。现下沉为唯一一份，两侧只引用本常量，禁止再各自维护。
#
# ⚠️ 匹配口径：本表含「畸变/崩坏/变形/错位」等**短词**，必须走
#   qc_client.find_critical_issues（带否定语境过滤）匹配；**禁止**裸子串匹配 ——
#   「无明显畸变」「无崩坏」这类正面结论会被短词误判成缺陷（原 sb_helpers 因此只敢收长词）。
#
# 刻意**不收**面板雷同 / 面板重复：2026-10-09 已从硬阻断降级为 warning，软放行豁免侧
#   由 sb_helpers._panel_dup_hit（词根正则 + 否定语境）单独判定。
STRUCTURAL_DEFECT_KEYWORDS: Tuple[str, ...] = (
    # 通用畸形 / 五官
    "畸变", "扭曲", "崩坏", "崩了", "脸部崩", "面部崩",
    "五官错位", "五官畸变", "五官错误", "五官不自然", "五官不协调", "五官不对称",
    # 肢体
    "多手", "多脚", "多头", "缺手", "缺脚", "断肢", "肢体错位", "肢体断裂", "手指异常", "六指",
    # 画质
    "融合", "粘连", "穿模", "黑屏", "白屏", "纯色块", "大色块", "严重糊", "糊化", "失焦",
    # 伪影 / 主体
    "撕裂", "闪烁", "重影", "鬼影", "变形", "比例失调", "主体缺失", "主体不完整", "残影",
    "拼接痕迹", "画面崩", "错位",
    # 拼接 / 人物重复（P0 独立复核补充）
    "拼接", "画面异常", "人物重复", "重复人物", "人物多出", "多出人物", "多余人物",
    "人物多余", "人物重叠", "人物数量异常", "多个人物", "分身",
    # 九宫格场景 / 方位
    "九宫格场景不一致", "格间换场景",
    "九宫格左右手镜像", "左右手互换", "左右手颠倒", "镜像翻转", "朝向翻转",
    # 手部畸变（含手指末端膨大型）
    "九宫格手指畸变", "手指粘连", "指头粘连", "手指融合", "多指畸形",
    "杵状指", "指尖膨大", "指尖钝化", "指尖球状", "手指粗细不均", "指节模糊",
    # 角色与设定不符
    "与设定不符", "与设定图不符", "与设定不一致", "与设定图不一致",
    "角色设定不符", "角色设定不一致",
    "发型与设定不符", "发型与设定不一致", "服装与设定不符", "服装与设定不一致",
    "外观与设定不符", "外观与设定不一致", "形象与设定不符",
    "明显不符", "明显不一致",
    # 跨镜连续性
    "与上一镜不符", "与上一镜不一致", "与上一镜不连贯", "与上一镜不连续",
    "与设定图及上一镜不符",
)

