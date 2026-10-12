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
GRID_ZH_TEMPLATE_NAME = "storyboard_grid_main"
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
                                    source: str = GRID_ZH_TEMPLATE_NAME) -> None:
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
# ⚠️ 已知「该去重、但不在本轮 inScope」的重复（另行交接，**不属于**上面五类）：
#   · qc_client.CRITICAL_ISSUE_KEYWORDS <-> sb_helpers 的结构性缺陷词表（qc_client.py 注释
#     自述「同一套判据的两处，两处必须同时改」）；
#   · storyboard_helpers._storyboard_prompt_structurally_ok 里的逐格版判据串。
# --------------------------------------------------------------------------- #

