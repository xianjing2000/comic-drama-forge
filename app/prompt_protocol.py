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
#: 逐格版模板的「防文字上屏」措辞（有台词时注入；无台词时走「不出现开口说话的口型」）
SB_MARK_GRID_ZH_NO_TEXT = "严禁出现任何台词文字或字幕"

#: 无文字禁令（新旧两种措辞）
SB_MARK_NO_TEXT = "must not contain any text"
SB_MARK_NO_TEXT_LEGACY = "不得出现任何文字、字幕、台词文本、水印"
