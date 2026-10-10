# -*- coding: utf-8 -*-
"""资产参考图提示词 × 角色锁定设定的**外形一致性收敛**（2026-09-24）

真因（实测，逐环有证据）
------------------------------------------------------------------
``characters[].appearance`` 是**锁定设定**（``continuity.py`` 的 _LOCK_FIELDS），
同时是「镜头 description → 分镜提示词 / 质检口径」的唯一来源；
而**资产出图的依据**是 ``characters[].reference_prompt_zh``（见 ``app.py`` 资产生成
worker：``asset.get('reference_prompt_zh', ...)``）。两者一旦漂移就会出现：

    文字要求「黑寸头、铜瞳」    ← shot.description 抄自 appearance
    参考图却是「黑发高冠、深金瞳」← 资产图按 reference_prompt_zh 生成

分镜工作流是「参考图编辑」型（``cfg=1.0``）→ **参考图压过文字**。模型在两条互斥
指令之间摇摆，每次随机倒向一边，而质检必然抓到另一边 →
**无限重试，整集永远跑不过**。实测（2026-09-24，逆天系统第1集）：
分镜连续 20 次裁定全部阻断，判词在两个方向互相矛盾
（「设定为黑寸头，画面中是发髻」/「设定图为发髻，分镜图为寸头」）。

漂移来源（三处，都要收敛）
------------------------------------------------------------------
① ``novel_to_script`` 汇总设定时 LLM 自行改写发色/瞳色/发型；
② ``script_prompt_analyzer.analyze_asset_prompts`` 只拿到
   name/appearance/personality 就重写参考提示词，把「黑发马尾」paraphrase 成
   「黑发垂肩」、「黑寸头」写成「黑发高冠」；
③ **镜头 description 自身就混用两边取值** —— 写分镜的 LLM 同时看到了
   appearance 与 reference_prompt_zh，实测同一集里羡进一会「琥珀瞳」一会「黑瞳」、
   一会「黑发垂肩」。

收敛口径
------------------------------------------------------------------
**以 ``appearance`` 为准**（锁定字段，且分镜/质检都从它派生）。本模块只做
**外科式替换**：把「发色+发型」「瞳色」两个维度上**与 appearance 冲突的取值**
换成 appearance 的取值；版式、服装、风格、画面措辞一律原样保留。

⚠️ 为什么不整段重写：整段重写会丢掉 LLM 写好的版式约束与画面细节；
而只换这两个维度已足够解开死循环（这两个维度是**已被实证**的冲突类）。
脸型/服装不入表 —— 服装要随集变化（``current_outfit``），锁死会与剧情冲突。

死循环的另一半：**遗漏**（``ensure_canon_clause``）
------------------------------------------------------------------
「冲突」是提示词写了**错的**取值；「遗漏」是提示词**压根没写** canon 特征 ——
后者同样致命。实测林清雪 ``appearance`` 是「银白长发垂腰，冰蓝瞳，鹅蛋脸，**眉间朱砂**」，
参考提示词里却没有朱砂；而她那 10 个出场镜头里有 **8 个**的 description 都写了朱砂
（→ 质检口径要求它）。资产图（cfg=1.0 时压过文字）必然没有朱砂 → 同样是
「文字要、图里没有」的互斥死循环，只是成因不同。

故本模块最后一步**无条件**把 ``appearance + current_outfit`` 逐字组织成
「角色锁定设定（必须严格遵守）：…」条款，插在风格段**之前**（幂等，见
:func:`ensure_canon_clause`）。这样 canon 特征一定在出图提示词里，重出资产图即可对齐。

⚠️ 条款不含任何生成链路副作用：分镜提示词的 ``_appearance()`` 优先取 ``appearance``
字段，仅在字段缺失时才回落 ``reference_prompt_zh``（``comfyui_client._h3_picture_defs``）；
风格追加的幂等标记是「风格：」+ style marker（``style_kit``），条款插在它之前不会破坏。

⚠️ 另一个易踩的坑：**伪冲突**。字面不同但语义相同的写法（``垂腰``/``及腰``）必须
经 :data:`_HAIR_STYLE_CANON` 归一后再比，否则会把提示词无谓改一遍、守卫也跟着假红。

⚠️⚠️ **作用域是本模块最容易错的地方**：一个镜头的 description 往往是多角色同框的
散文，**必须只按「该镜头实际出场的角色」取计划**；否则会把 A 的 canon 当成 B 的
冲突来改（首发实现就是这么错的：216 条"冲突"里绝大多数是把 赵天霸 的 canon
「黑寸头」当成 羡进 的冲突，一改就会把赵天霸写成马尾）。
另加一道保险：**若某个"冲突取值"恰好等于任意角色的 canon 取值，则跳过** ——
说明它是别人的正确设定，不是漂移。

⚠️ 英文侧（``reference_prompt_en``，**只用于展示**，生成链路只吃 zh）只做
**带名词的短语级替换**（``shoulder-length hair`` / ``black eyes`` 这类）。
绝不放裸颜色词进替换表 —— 那会把 ``black eyes`` 里的 ``black`` 也换掉，
产出 ``black ponytail black eyes`` 这种乱码（首发实现踩过）。
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 维度词表
# --------------------------------------------------------------------------- #
#: 发色 / 瞳色（中文 → 英文）。正则按长度倒序拼装，避免「深金」被「金」提前截断。
_COLOR_ZH2EN: Dict[str, str] = {
    "深金": "deep gold", "银白": "silver-white", "雪白": "snow-white", "冰蓝": "ice-blue",
    "深棕": "dark brown", "墨": "ink-black", "碧": "jade", "赤": "crimson",
    "黑": "black", "白": "white", "银": "silver", "金": "gold", "铜": "copper",
    "琥珀": "amber", "棕": "brown", "红": "red", "蓝": "blue", "绿": "green",
    "紫": "purple", "灰": "grey", "青": "cyan",
}

#: 英文提示词里表达同一颜色时可能出现的写法（反向替换用，最长优先）
_COLOR_EN_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "深金": ("deep gold", "deep-gold", "dark gold"),
    "银白": ("silver-white", "silver white", "silvery white"),
    "雪白": ("snow-white", "snow white"),
    "冰蓝": ("ice-blue", "ice blue"),
    "深棕": ("dark brown", "dark-brown"),
    "墨": ("ink-black", "ink black"),
    "碧": ("jade",),
    "赤": ("crimson",),
    "黑": ("black",), "白": ("white",), "银": ("silver",), "金": ("gold",),
    "铜": ("copper",), "琥珀": ("amber",), "棕": ("brown",), "红": ("red",),
    "蓝": ("blue",), "绿": ("green",), "紫": ("purple",), "灰": ("grey", "gray"),
    "青": ("cyan",),
}

#: 发型（中文 → 英文）
_HAIR_STYLE_ZH2EN: Dict[str, str] = {
    "寸头": "buzz cut", "平头": "crew cut", "马尾": "ponytail",
    "高冠": "topknot with a crown", "发冠": "hair crown", "道冠": "daoist crown",
    "束发": "tied-up hair", "发髻": "hair bun", "丸子头": "hair bun",
    "长发": "long hair", "短发": "short hair",
    "垂肩": "shoulder-length hair", "披肩": "shoulder-length hair",
    "及腰": "waist-length hair", "垂腰": "waist-length hair",
    "卷发": "curly hair", "直发": "straight hair",
    "刘海": "bangs",
}

#: 英文提示词里表达同一发型时可能出现的写法（反向替换用，最长优先）
_HAIR_STYLE_EN_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "寸头": ("buzz cut", "buzzcut", "crew cut", "shaved head"),
    "平头": ("crew cut", "buzz cut"),
    "马尾": ("ponytail", "pony tail"),
    "高冠": ("topknot", "hair crown", "crown", "guan"),
    "发冠": ("hair crown", "topknot", "crown"),
    "道冠": ("daoist crown", "hair crown", "crown", "topknot"),
    "束发": ("tied-up hair", "tied up hair", "topknot"),
    "发髻": ("hair bun", "bun"),
    "丸子头": ("hair bun", "bun"),
    "长发": ("long hair",),
    "短发": ("short hair",),
    "垂肩": ("shoulder-length hair", "shoulder-length", "shoulder length"),
    "披肩": ("shoulder-length hair", "shoulder-length", "shoulder length"),
    "及腰": ("waist-length hair", "waist-length", "waist length"),
    "垂腰": ("waist-length hair", "waist-length", "waist length"),
    "卷发": ("curly hair",),
    "直发": ("straight hair",),
    "刘海": ("bangs", "fringe"),
}


#: 发型**同义归一**：字面不同但语义相同的写法归到同一个代表键。
#:
#: 为什么非有不可（两类伪冲突，都实测过）：
#:   ① canon 写「银白长发**垂腰**」而提示词写「银白长发**及腰**」—— 字面不等但语义
#:      一致（都是到腰长发），不归一会把提示词无谓改一遍、守卫也跟着假红；
#:   ② 镜头文本只写通称「银白长发」，而 canon 是下位写法「银白长发垂腰」——
#:      抽取结果一个是 ``银白长发``、一个是 ``银白长发垂腰``，同样会误判成冲突。
#: **长度类修饰一律归到「长发」**（它们描述的都是长发，只是到肩/到腰之别），
#: 真正必须严格区分的是**造型**（寸头 / 高冠 / 马尾 / 发髻…）—— 长度差异不会让
#: 质检死循环，造型差异才会。**颜色不做跨键归一**（「冰蓝」≠「蓝」，是不同取值）。
_HAIR_STYLE_CANON: Dict[str, str] = {
    # —— 造型类（互斥，必须严格区分）——
    "寸头": "寸头", "平头": "寸头",
    "高冠": "高冠", "发冠": "高冠", "道冠": "高冠",
    "发髻": "发髻", "丸子头": "发髻",
    "马尾": "马尾",
    "束发": "束发",
    "短发": "短发",
    "卷发": "卷发", "直发": "直发", "刘海": "刘海",
    # —— 长度类（统一归「长发」）——
    "长发": "长发", "及腰": "长发", "垂腰": "长发",
    "披肩": "长发", "垂肩": "长发",
}


def _alt(keys) -> str:
    """把词表键拼成正则分支：**最长键优先**，否则短键会吃掉长子串。"""
    return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))


#: ⚠️⚠️ **绝不要把「颜色 + 可选发字 + 发型」拼成一条正则**（首发实现就是这么错的）：
#: Python ``re`` 是回溯式左优先引擎、不是最长匹配。``银白长发垂腰`` 里 ``长`` 挡住了
#: 可选的 ``发``，style 分支先匹配到更靠前的 ``长发`` 就收工 → 抽成 ``银白长发``，
#: 把「垂腰」吃掉；同类写法 ``银白长马尾`` 更糟，只会抽到 ``马尾``（颜色一起丢）。
#: 正确做法是**分开三步**（见 :func:`_hair_parts`）：
#: ① :data:`_HAIR_STYLE_RE` 找出所有发型词；② 每个发型词往前找最靠左的、与它之间
#: 只隔 :data:`_HAIR_JOIN_RE` 连接字的发色词；③ 取整体 span 最长的候选。
_HAIR_STYLE_RE = re.compile(_alt(_HAIR_STYLE_ZH2EN))

#: 全部发色词（用于在发型词**之前**定位发色）
_COLOR_RE = re.compile(_alt(_COLOR_ZH2EN))

#: 发色与发型之间允许出现的「连接字」。
#: 必需：「银白长发垂腰」的正确切分是 发色「银白」+ 发型「垂腰」，中间的「长发」
#: 只是长度修饰。不允许连接字的话就只能切成 发色「银白」+ 发型「长发」，
#: 把真正要表达的「垂腰」丢掉。
_HAIR_JOIN_RE = re.compile(r"^(?:色|发|发丝|头发|长|短|齐)*$")

#: 颜色 + 瞳/眸。颜色**必须出现** —— 裸「瞳」（如「瞳孔」）不是设定取值。
#: （unit 只有两个取值、不存在重叠歧义，用一条正则即可。）
_EYE_RE = re.compile(
    r"(?P<color>" + _alt(_COLOR_ZH2EN) + r")(?:色)?(?P<unit>瞳|眸)"
)

#: 会被收敛的镜头文本字段（都会流进分镜提示词或质检口径）
_SHOT_TEXT_FIELDS = ("description", "visual_detail", "storyboard_prompt_zh",
                     "prompt_h3", "action")

#: 角色提示词字段（zh 参与真实生成，en 只用于展示）
_CHAR_PROMPT_FIELDS = ("reference_prompt_zh", "reference_prompt_en")

#: 「锁定设定条款」的幂等标记。这条款把 appearance+outfit **逐字**钉进参考提示词，
#: 解决「冲突」之外的另一半问题 —— **遗漏**：实测林清雪 appearance 的「眉间朱砂」
#: 没被 LLM 写进参考提示词，而她那 10 个镜头里有 8 个的 description 都写了朱砂
#: （→ 质检口径要求朱砂），参考图（cfg=1.0 时压过文字）又必然没有 → 同类死循环。
#: 加这条款后 canon 特征一定在提示词里，重新出图即可对齐。
_CANON_CLAUSE_MARKER = "角色锁定设定"
_CANON_CLAUSE_MARKER_EN = "locked character design"

#: 中/英的「风格段」标记（锁定设定条款要插在风格段**之前**，保持「外貌→设定→风格」顺序）
_STYLE_MARKERS = ("风格：", "风格:", "Style:", "style:")

#: 匹配已有的「锁定设定条款」（正文不含句号 —— :func:`canon_clause_text` 已把分句
#: 统一换成「，」并剥掉结尾标点）。用于幂等判断与**就地升级**旧版条款。
_CANON_CLAUSE_RE = re.compile(
    re.escape(_CANON_CLAUSE_MARKER) + r"（必须严格遵守）：[^。]*。?")


# --------------------------------------------------------------------------- #
# 性别（2026-09-28）：资产图性别画反的兜底
# --------------------------------------------------------------------------- #
# 真因：角色「三百年旧怨女子」（名字明确是女性）出的资产图却是**男性形象**（短发男性
# 脸型体格）。逐环取证：剧本 schema 里没有 gender 字段、appearance 与 reference_prompt_zh
# 的字段说明都只要求写发色/瞳色/服装（不要求写性别），本模块 grep「性别|gender|女性|男性」
# 零命中 → 整条链路**一个性别标记都没有**；名字里的「女子」又不进提示词（提示词只吃
# appearance 类字段）→ 文生图模型只能自由发挥。
#
# 本层是**兜底**（L2）：存量剧本 / 模型漏写 / 历史 bible 都没有 gender 字段，必须能从
# name / identity 推断。⚠️ 推断不出时**绝不注入** —— 宁可没有，也不要瞎猜一个性别。
#: 扮相 / 服装词：**判性别前先剔除** —— 「男装」「女装」「女扮男装」本身不含性别信息
#: （女性角色照样可以「身着男装」），留着会污染判定。
_GENDER_ZH_CAMOUFLAGE_RE = re.compile(r"女扮男装|男扮女装|扮男装|扮女装|男装|女装|中性")

#: 明确的性别**主体词**（全词）。⚠️ **绝不能含裸单字「女」「男」**：实测
#: ``{"gender":"女","appearance":"身着男装，束发，腰悬长刀，眉目清冷"}`` 会因为「男装」
#: 里的「男」命中裸字判据 → 被判成「已有性别词」→ 跳过注入 → **女性角色被反向推成男性**
#: （正是本次 bug 的同类翻版）。
_GENDER_ZH_FEMALE_RE = re.compile(r"女性|女子|女的|少女|妇人|夫人|老妪|姑娘|女孩")
_GENDER_ZH_MALE_RE = re.compile(r"男人|男性|男子|少年|公子|老者|老翁")

#: 全词性别主体词合集（幂等判据 B 与 name / identity 线索**共用同一口径**）。
#: ⚠️ 旧版含裸「女」「男」，被 QA 实测打穿，勿回退。
_GENDER_ZH_RE = re.compile(
    r"女性|女子|女的|少女|妇人|夫人|老妪|姑娘|女孩|男人|男性|男子|少年|公子|老者|老翁")

#: 「显式性别标记」—— 本模块注入的形式（``性别：女``），幂等判据 A，最可靠。
_GENDER_ZH_MARK_RE = re.compile(r"性别\s*[:：]\s*([男女])")

#: 英文侧同义判据。⚠️ 必须带**词边界**：否则 ``woman`` 里的 ``man``、``female`` 里的
#: ``male`` 会被误判成男性 —— 这正是「女性角色被画成男性」在英文侧的翻版。
_GENDER_EN_FEMALE_RE = re.compile(
    r"(?<![A-Za-z])(?:female|woman|women|girl|lady|ladies)(?![A-Za-z])", re.IGNORECASE)
_GENDER_EN_MALE_RE = re.compile(
    r"(?<![A-Za-z])(?:male|man|men|boy|gentleman|gentlemen)(?![A-Za-z])", re.IGNORECASE)
_GENDER_EN_RE = re.compile(
    r"(?<![A-Za-z])(?:female|woman|women|girl|lady|ladies"
    r"|male|man|men|boy|gentleman|gentlemen)(?![A-Za-z])", re.IGNORECASE)

#: 英文侧的扮相 / 服装短语（作用同 :data:`_GENDER_ZH_CAMOUFLAGE_RE`）：
#: ``in male attire`` 描述的是**女性角色的男装打扮**，不能当成「已有性别词」。
_GENDER_EN_CAMOUFLAGE_RE = re.compile(
    r"(?<![A-Za-z])(?:cross[\s-]?dressing|in male (?:attire|clothing|disguise)"
    r"|in female (?:attire|clothing|disguise)|androgynous)(?![A-Za-z])", re.IGNORECASE)

#: 注入到「锁定设定条款」开头的中文性别标记（须为最终字符串的字面子串）
_GENDER_ZH_PREFIX = "性别："

#: ``gender`` 字段的**白名单精确匹配表**（归一化后做**相等**比较，不做子串匹配）
_GENDER_WORDS_FEMALE = frozenset(
    ("女", "女性", "女子", "女的", "少女", "姑娘", "女孩", "妇人", "夫人",
     "female", "woman", "women", "girl", "lady", "ladies"))
_GENDER_WORDS_MALE = frozenset(
    ("男", "男性", "男子", "男人", "少年", "公子", "male", "man", "men",
     "boy", "gentleman", "gentlemen"))


def _strip_gender_camouflage(text: str) -> str:
    """剔除中文扮相 / 服装词（判性别前先跑这一步）。"""
    return _GENDER_ZH_CAMOUFLAGE_RE.sub("", str(text or ""))


def _strip_gender_camouflage_en(text: str) -> str:
    """剔除英文扮相 / 服装短语（判性别前先跑这一步）。"""
    return _GENDER_EN_CAMOUFLAGE_RE.sub("", str(text or ""))


def normalize_gender(raw) -> str:
    """把**整个值就是一个性别词**的写法归一成 ``"女"`` / ``"男"`` / ``""``。

    ⚠️ 必须**精确匹配**而不是子串匹配（QA 实测）：``"human"`` 会因含 ``"man"`` 被判成
    男性、``"womanizer"`` 会因含 ``"woman"`` 被判成女性 —— 与 :func:`gender_en` 的
    词边界口径保持一致。认不出就返回 ``""``（绝不瞎猜）。
    """
    s = re.sub(r"[\s　:：,，。;；、]+", "", str(raw or "")).lower()
    if s.startswith("性别"):
        s = s[2:]
    if not s:
        return ""
    if s in _GENDER_WORDS_FEMALE:
        return "女"
    if s in _GENDER_WORDS_MALE:
        return "男"
    return ""


def gender_marked(text: str) -> bool:
    """文本里是否**已经写明**性别（幂等判据，中英通吃）。

    两条**任一**命中即算已写明：
      A 显式标记「性别：X」（本模块注入的形式，或人写的中文标记）；
      B 全词性别主体词（女性 / 男子 / 少女…）—— **绝不含裸单字「女」「男」**。
    判定前先剔除「男装 / 女装 / 女扮男装 / in male attire」这类**扮相词**。
    """
    t = str(text or "")
    if _GENDER_ZH_MARK_RE.search(t):
        return True
    if _GENDER_ZH_RE.search(_strip_gender_camouflage(t)):
        return True
    return bool(_GENDER_EN_RE.search(_strip_gender_camouflage_en(t)))


def gender_zh(text: str) -> str:
    """从中文文本推断性别：``"女"`` / ``"男"`` / ``""``。

    顺序：① 显式标记「性别：X」→ ② 剔除扮相词后的全词性别主体词（女性线索优先）。
    """
    t = str(text or "")
    m = _GENDER_ZH_MARK_RE.search(t)
    if m:
        return m.group(1)
    t = _strip_gender_camouflage(t)
    if _GENDER_ZH_FEMALE_RE.search(t):
        return "女"
    if _GENDER_ZH_MALE_RE.search(t):
        return "男"
    return ""


def gender_en(text: str) -> str:
    """从英文文本推断性别（词边界安全：``a woman`` 判为女，不会被 ``man`` 抢走）。"""
    t = _strip_gender_camouflage_en(str(text or ""))
    if _GENDER_EN_FEMALE_RE.search(t):
        return "女"
    if _GENDER_EN_MALE_RE.search(t):
        return "男"
    return ""


def infer_gender(char: dict) -> str:
    """推断角色性别：``"女"`` / ``"男"`` / ``""``。

    优先级（2026-09-28）：① ``gender`` 字段（剧本 schema 已要求必须给出，**白名单精确匹配**）
    → ② ``name``（「三百年旧怨女子」这类称谓线索）→ ③ ``identity``。
    name / identity 都只认**全词**线索（见 :data:`_GENDER_ZH_RE`）—— 裸单字「男」「女」会
    误伤（QA 实测：``identity="女主角的师兄"`` 被裸「女」判成女性、``"男主的师妹"`` 被裸
    「男」判成男性）。全部失败返回 ``""`` —— **推断不出就不注入，绝不瞎猜**。
    """
    if not isinstance(char, dict):
        return ""
    g = normalize_gender(char.get("gender"))
    if g:
        return g
    g = gender_zh(str(char.get("name") or ""))
    if g:
        return g
    return gender_zh(str(char.get("identity") or ""))


def gender_hint(char: dict) -> str:
    """质检上下文用的性别标记：``"性别：女"`` / ``"性别：男"`` / ``""``。"""
    g = infer_gender(char)
    return f"{_GENDER_ZH_PREFIX}{g}" if g else ""


def ensure_gender_en(char: dict) -> bool:
    """确保 ``reference_prompt_en`` 带性别词（``female`` / ``male``）。返回是否改动。

    幂等判据是 :func:`gender_marked`（词边界 + 剔除 ``in male attire`` 这类扮相短语）。
    en 只用于展示（生成链路只吃 zh），补上是为了人工复核 / 前端也能看到性别。
    """
    if not isinstance(char, dict):
        return False
    en = str(char.get("reference_prompt_en") or "")
    if not en or gender_marked(en):
        return False
    g = infer_gender(char)
    if not g:
        return False
    char["reference_prompt_en"] = ("female character, " if g == "女" else "male character, ") + en
    return True


def ensure_prompt_gender(prompt: str, char: dict) -> str:
    """保证**出图提示词**里写明性别（与 :func:`gender_hint` 口径一致）。

    解决一处**不对称**（QA 实测）：``reference_prompt_zh`` 为空时 ``ensure_canon_clause``
    无处落条款（zh 为空直接返回 False）→ 生成侧提示词一个性别标记都没有，而质检侧
    ``gender_hint`` 仍返回「性别：女」→ **生成无约束、质检按性别判** → 反复判不过重画。
    这里在生成入口兜底补齐，保证「hint 说女，则 prompt 里必须有女」；prompt 为空时返回
    ``"性别：女"`` 这个**可用的最小值**。幂等：prompt 已有性别（任意写法）则原样返回。
    """
    p = str(prompt or "")
    g = infer_gender(char)
    if not g:
        return p
    if gender_marked(p):
        # 已有性别词：与推断结果**一致**（或读不出具体性别）→ 原样返回（幂等、不加冗余）；
        # ⚠️ **冲突**（如 hint=女 而 prompt 里写着「男性」）时**权威值必须显式出现在最前**：
        # 否则模型面对两条互斥指令又会随机倒向一边、质检必然抓到另一边 —— 正是本次 bug
        # 的死循环成因。这里只**前置**权威标记，不改写 LLM 写的正文（改写风险更大）。
        stated = gender_zh(p) or gender_en(p)
        if not stated or stated == g:
            return p
    prefix = f"{_GENDER_ZH_PREFIX}{g}；"
    return (prefix + p) if p else prefix.rstrip("；")


# --------------------------------------------------------------------------- #
# 场景 canon：以「地理参考可复用」为底线（2026-09-29）
#
# 参考外部视觉方法论提炼（location-plate 的地理优先、scene-system 的 actor lane）：
# **场景图不是一张漂亮的背景**，它要被后续镜头、多视角与视频反复当作同一个
# 可导航空间来引用。
#
# 与 ensure_prompt_gender 同范式：只补**短且确定**的底线条款，绝不去替 LLM 写
# 具体的布局内容描述（那会稀释提示词、还可能与正文自相矛盾）。
# --------------------------------------------------------------------------- #

# ⚠️ 职责边界（勿越界）：**去人**由 ``comfyui_client.sanitize_scene_prompt`` 负责 ——
#    它更强（按分句丢弃含人物的描述 + 追加 ``SCENE_NO_CHARACTER_SUFFIX``）。
#    这里**绝不再写一遍**「不得出现人物」：两条同源条款会互相叠加，且一旦一侧改口径
#    另一侧必然漂移。本函数只补既有链路完全缺失的那半：空间的**可读性与可走位性**。
_SCENE_LAYOUT_MARK = "留出可供角色走动的地面路径"

# 只写「空间可读底线」，不写具体是什么地方（具体布局由 LLM 依 scenes schema 撰写）
_SCENE_LAYOUT_SUFFIX = (
    "，保留入口、通道与固定结构，"
    "留出可供角色走动的地面路径，"
    "并交代前景、中景与背景的层次"
)


def scene_layout_marked(text: str) -> bool:
    """提示词里是否已含有布局底线条款（幂等判据）。"""
    return _SCENE_LAYOUT_MARK in str(text or "")


#: 场景**固定结构**条款的幂等标记（2026-10-10，场景空间关联需求③）。
#: 追加串必须以本串开头，幂等判定才成立。
SCENE_FIXED_STRUCTURE_MARKER = "，固定结构（跨镜头不变，须与同空间其他场景一致）："


def ensure_scene_layout(prompt: str, scene: dict = None) -> str:
    """保证**场景出图提示词**达到「可复用地理参考」的布局底线。

    依据外部视觉方法论提炼：场景不是一张漂亮背景，而是后续镜头要反复引用的
    **同一个可导航空间**。它必须留出能让角色进出与停留的路径、并交代清楚景深分层，
    否则后续把角色放进画面时会站不下、无处走位，换机位时也难以保持一致。

    与 :func:`ensure_prompt_gender` 同范式：只补**短且确定**的底线条款，
    绝不替 LLM 写具体的布局内容（那会稀释提示词，还可能与正文自相矛盾）。

    幂等：prompt 已含该条款则原样返回。
    """
    p = str(prompt or "").strip()
    if not p:
        return _SCENE_LAYOUT_SUFFIX.lstrip("，")
    # ⭐ 2026-10-10 固定结构（场景空间关联，用户需求③）：
    #    同一空间的不同位置（2704房间 / 2704门口 / 27F走廊）此前各自独立出图，
    #    提示词里**没有门框 / 地面 / 墙面这类固定结构**的约束，模型只能自由发挥
    #    —— 实测「2704房间」门框是深色的，而「2704门口」是斑驳木框，同一个地方
    #    被画成两处。fixed_structure 由剧本阶段（novel_to_script 的 scenes schema）
    #    或 scene_relations.relate_scenes 产出，这里把它拼进出图提示词作为硬约束。
    #    幂等标记见 SCENE_FIXED_STRUCTURE_MARKER。
    _fs = str((scene or {}).get("fixed_structure") or "").strip()
    if _fs and SCENE_FIXED_STRUCTURE_MARKER not in p:
        p = p.rstrip("。，,.;； ") + SCENE_FIXED_STRUCTURE_MARKER + _fs
    if scene_layout_marked(p):
        return p
    return p.rstrip("。，,.;； ") + _SCENE_LAYOUT_SUFFIX


# --------------------------------------------------------------------------- #
# 取值抽取
# --------------------------------------------------------------------------- #
def _hair_parts(text: str) -> Tuple[str, str, int, int]:
    """在文本里抽「发色 + 发型」，返回 ``(color, style, start, end)``。

    无发型词 → ``("", "", -1, -1)``。

    候选 span = 「往前找到的**最靠左**的、与发型词之间只隔连接字的发色词」→「发型词末尾」；
    多个候选取**整体 span 最长**者。这样 ``银白长发垂腰`` 整体胜出（span=6），
    而不是被更靠前的「长发」截成 ``银白长发``（span=4）。
    """
    text = str(text or "")
    best: Optional[Tuple[int, int, str, str]] = None
    if text:
        for m in _HAIR_STYLE_RE.finditer(text):
            s, e, style = m.start(), m.end(), m.group(0)
            start, color = s, ""
            # finditer 升序 → 第一个「合法」的发色词就是最靠左的那个
            for cm in _COLOR_RE.finditer(text, 0, s):
                if _HAIR_JOIN_RE.fullmatch(text[cm.end():s]):
                    start, color = cm.start(), cm.group(0)
                    break
            if best is None or (e - start) > (best[1] - best[0]):
                best = (start, e, color, style)
    if not best:
        return "", "", -1, -1
    return best[2], best[3], best[0], best[1]


def hair_zh(text: str) -> str:
    """取中文文本里的「发色+发型」短语（无则 ''）。

    ⚠️ **发型必须出现**，否则「黑发被冷风吹起」这种只是顺带提到发色的句子也会被
    命中 —— 那类句子本身与设定不冲突，不该动。
    """
    raw = str(text or "")
    _color, _style, s, e = _hair_parts(raw)
    return raw[s:e] if s >= 0 else ""


def eye_zh(text: str) -> str:
    """取中文文本里的「瞳色」短语（无则 ''）。"""
    m = _EYE_RE.search(str(text or ""))
    return m.group(0) if m else ""


def _split_hair(phrase: str) -> Tuple[str, str]:
    """把「黑发高冠」拆成 ('黑', '高冠')；颜色缺失时返回 ('', '高冠')。"""
    color, style, _s, _e = _hair_parts(phrase)
    return color, style


def _split_eye(phrase: str) -> Tuple[str, str]:
    m = _EYE_RE.fullmatch(str(phrase or ""))
    if not m:
        return "", ""
    return (m.group("color") or ""), (m.group("unit") or "瞳")


def _hair_key(phrase: str) -> str:
    """发型短语的**比较键**：``银白长发及腰`` → ``银白|及腰``。

    颜色按字面比、发型按 :data:`_HAIR_STYLE_CANON` 同义归一 —— 归一后相同的写法
    视为同一个设定，不产生替换计划（见该词表的说明）。
    """
    color, style = _split_hair(phrase)
    return f"{color}|{_HAIR_STYLE_CANON.get(style, style)}"


def _eye_key(phrase: str) -> str:
    """瞳色短语的比较键：``瞳``/``眸`` 同义（不算差异），颜色按字面比。"""
    color, _unit = _split_eye(phrase)
    return color


def _color_en(token: str) -> str:
    return _COLOR_EN_SYNONYMS.get(token, ())[0] if token in _COLOR_EN_SYNONYMS else token


def _style_en(token: str) -> str:
    return _HAIR_STYLE_EN_SYNONYMS.get(token, ())[0] \
        if token in _HAIR_STYLE_EN_SYNONYMS else _HAIR_STYLE_ZH2EN.get(token, token)


def _en_pairs(kind: str, bad: str, good: str) -> List[Tuple[Tuple[str, ...], str]]:
    """构造英文替换对 ``[(待替换写法, 替换成), ...]``。

    ⚠️ **只做带名词的短语级替换**（``shoulder-length hair`` / ``black eyes``）；
    颜色与发型分开成对，绝不让裸颜色词进表 —— 否则 ``black eyes`` 里的
    ``black`` 会被单独换掉，产出 ``black ponytail black eyes`` 乱码。
    """
    if kind == "eye":
        bc, _u1 = _split_eye(bad)
        gc, _u2 = _split_eye(good)
        if not bc or not gc or bc == gc:
            return []
        words: List[str] = []
        for syn in _COLOR_EN_SYNONYMS.get(bc, ()):
            words += [f"{syn} eyes", f"{syn} eye"]
        return [(tuple(words), f"{_color_en(gc)} eyes")]
    bc, bs = _split_hair(bad)
    gc, gs = _split_hair(good)
    pairs: List[Tuple[Tuple[str, ...], str]] = []
    if bs and gs and bs != gs:
        pairs.append((tuple(_HAIR_STYLE_EN_SYNONYMS.get(bs, ())), _style_en(gs)))
    if bc and gc and bc != gc:
        words = tuple(f"{syn} hair" for syn in _COLOR_EN_SYNONYMS.get(bc, ()))
        pairs.append((words, f"{_color_en(gc)} hair"))
    return [(w, g) for w, g in pairs if w and g]


# --------------------------------------------------------------------------- #
# 收敛计划
# --------------------------------------------------------------------------- #
def build_plan(appearance: str, drifted: str) -> Dict[str, dict]:
    """比较「锁定设定」与「待收敛文本」，返回需要替换的维度。

    返回形如 ``{"hair": {...}, "eye": {...}}``；两边取值一致（或任一侧取不到值）
    时不产出该维度 —— 宁可不改，也不要凭猜测替换。

    ⚠️ 一致性的判定走 :func:`_hair_key` / :func:`_eye_key` 的**归一比较键**，不是
    字面比较：``银白长发垂腰`` 与 ``银白长发及腰`` 语义相同，不该算冲突。
    """
    canon_hair, bad_hair = hair_zh(appearance), hair_zh(drifted)
    canon_eye, bad_eye = eye_zh(appearance), eye_zh(drifted)
    plan: Dict[str, dict] = {}
    if canon_hair and bad_hair and _hair_key(canon_hair) != _hair_key(bad_hair):
        plan["hair"] = {"kind": "hair", "bad_zh": bad_hair, "good_zh": canon_hair,
                        "en_pairs": _en_pairs("hair", bad_hair, canon_hair)}
    if canon_eye and bad_eye and _eye_key(canon_eye) != _eye_key(bad_eye):
        plan["eye"] = {"kind": "eye", "bad_zh": bad_eye, "good_zh": canon_eye,
                       "en_pairs": _en_pairs("eye", bad_eye, canon_eye)}
    return plan


def canon_tokens(characters: Sequence[dict]) -> set:
    """收集所有角色的 canon 外形取值（发型/瞳色），供伪冲突过滤。"""
    out = set()
    for c in (characters or []):
        if not isinstance(c, dict):
            continue
        ap = str(c.get("appearance") or "")
        for ph in (hair_zh(ap), eye_zh(ap)):
            if ph:
                out.add(ph)
    return out


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
def align_zh(text: str, plan: Dict[str, dict]) -> str:
    """按计划替换中文文本里冲突的外形取值（其余一字不动）。"""
    out = str(text or "")
    if not out or not plan:
        return out
    for kind in ("hair", "eye"):
        item = plan.get(kind)
        if item and item["bad_zh"] and item["bad_zh"] != item["good_zh"]:
            out = out.replace(item["bad_zh"], item["good_zh"])
    return out


def _en_sub(text: str, words, good: str) -> str:
    """英文提示词的词级替换：**最长写法优先**，忽略大小写，词边界安全。"""
    bad = [w for w in (words or ()) if w and str(w).lower() != str(good or "").lower()]
    if not bad or not good:
        return text
    pattern = re.compile(r"(?<![A-Za-z])(" + _alt(bad) + r")(?![A-Za-z])", re.IGNORECASE)
    return pattern.sub(good, text)


def align_en(text: str, plan: Dict[str, dict]) -> str:
    """按计划替换英文文本里冲突的外形取值（短语级，尽力而为）。

    ⚠️ ``reference_prompt_en`` 只用于展示（生成链路只吃 ``reference_prompt_zh``），
    因此这里允许「尽力而为」：LLM 的同义写法未必穷举得到，漏掉的写法由
    :func:`find_conflicts` 报出来，不阻断流程。
    """
    out = str(text or "")
    if not out or not plan:
        return out
    for kind in ("hair", "eye"):
        item = plan.get(kind)
        if not item:
            continue
        for words, good in (item.get("en_pairs") or []):
            out = _en_sub(out, words, good)
    return out


# --------------------------------------------------------------------------- #
# 锁定设定条款（解决「遗漏」：canon 特征没被 LLM 写进参考提示词）
# --------------------------------------------------------------------------- #
def canon_clause_text(char: dict) -> str:
    """把 ``appearance`` 组织成「锁定设定」条款正文（无内容则 ''）。

    ⚠️ **不含** ``current_outfit``：它是**分集可变**字段，写进「必须严格遵守」的
    锁定条款在语义上就是错的（服装本就该随剧情变）；实测把它拼进去还会与提示词
    里既有的服装描述形成**第三条互斥来源**（羡进：「蓝白外门长袍衣襟撕裂」vs
    「洗灰外门袍，衣领破损」），而质检口径就是这条提示词本身 → 又是一轮摇摆。
    仅在 ``appearance`` 完全缺失时才回落用它。
    """
    if not isinstance(char, dict):
        return ""
    body = str(char.get("appearance") or "").strip()
    if not body:
        body = str(char.get("current_outfit") or char.get("outfit") or "").strip()
    body = body.rstrip("。，,;； ").replace("。", "，")
    if not body:
        return ""
    # 性别兜底（2026-09-28）：appearance 里已有**明确性别词**才跳过（幂等）。
    # ⚠️ 判据必须走 gender_marked（全词 + 先剔除「男装/女装」这类扮相词），**绝不能用
    # 裸单字「女」「男」**：实测 {"gender":"女","appearance":"身着男装，束发，腰悬长刀"}
    # 会因为「男装」里的「男」被判成"已有性别词"→ 跳过注入 → 女性角色被反向推成男性。
    g = infer_gender(char)
    if g and not gender_marked(body):
        body = f"{_GENDER_ZH_PREFIX}{g}；{body}"
    return f"{_CANON_CLAUSE_MARKER}（必须严格遵守）：{body}"


def _insert_before_style(text: str, clause: str, joiner: str = "。") -> str:
    """把条款插在「风格段」之前，保持「外貌 → 锁定设定 → 风格」的可读顺序。"""
    segs = [s.strip() for s in str(text or "").split(joiner) if s.strip()]
    idx = next((i for i, s in enumerate(segs) if any(m in s for m in _STYLE_MARKERS)),
               len(segs))
    segs.insert(idx, clause.strip())
    return joiner.join(segs) + joiner


def ensure_canon_clause(char: dict) -> bool:
    """确保 ``reference_prompt_zh`` 带着**当前口径**的「锁定设定条款」。返回是否改动。

    幂等：条款已存在且正文一致 → 返回 False（零改动）。
    存在但正文不同（条款口径升级，或角色设定被改过）→ **就地替换**，避免旧条款
    永远留在提示词里（早期版本把 ``current_outfit`` 也拼进了条款，见
    :func:`canon_clause_text` 的说明）。

    ⚠️ 只对 zh 生效：``reference_prompt_en`` 是展示用字段，而 English 版条款需要
    把 appearance 翻译成英文，做不到确定性，故不动（不影响出图与质检）。
    """
    if not isinstance(char, dict):
        return False
    clause = canon_clause_text(char)
    if not clause:
        return False
    zh = str(char.get("reference_prompt_zh") or "")
    if not zh:
        return False
    if _CANON_CLAUSE_MARKER in zh:
        new = _CANON_CLAUSE_RE.sub(
            lambda m: clause + ("。" if m.group(0).endswith("。") else ""), zh, count=1)
        if new == zh:
            return False
        char["reference_prompt_zh"] = new
        return True
    char["reference_prompt_zh"] = _insert_before_style(zh, clause)
    return True


def _canon_clause_body(char) -> str:
    """取当前参考提示词里条款的完整匹配文本（无则 ''），供「新增/升级」计数。"""
    if not isinstance(char, dict):
        return ""
    m = _CANON_CLAUSE_RE.search(str(char.get("reference_prompt_zh") or ""))
    return m.group(0) if m else ""


def _has_canon_clause(char) -> bool:
    """该角色的参考提示词里是否已有「锁定设定条款」。"""
    if not isinstance(char, dict):
        return False
    return _CANON_CLAUSE_MARKER in str(char.get("reference_prompt_zh") or "")


def missing_canon_clause(script: dict) -> List[dict]:
    """列出「有 appearance 但参考提示词里没有锁定设定条款」的角色（供守卫）。"""
    out = []
    for char in ((script or {}).get("characters") or []):
        if not isinstance(char, dict):
            continue
        zh = str(char.get("reference_prompt_zh") or "")
        if canon_clause_text(char) and zh and not _has_canon_clause(char):
            out.append({"where": f"characters[{char.get('name')}].reference_prompt_zh",
                        "why": "缺少锁定设定条款（appearance/outfit 特征可能被 LLM 漏写）"})
    return out


def align_character(char: dict) -> Dict[str, dict]:
    """就地收敛单个角色：① 按 appearance 替换漂移取值 ② 补「锁定设定条款」。

    返回本次生效的**替换计划**（空 dict = 没有维度漂移）；条款是否新增另见
    :func:`ensure_canon_clause`，由 :func:`reconcile_script` 单独统计。

    ⚠️ 顺序有意为之：**先替换、后插条款**。条款正文含 appearance 原文，若先插进去
    就会污染 :func:`build_plan` 的取值抽取（正则取第一个匹配，可能落到条款上而漏掉
    真正的漂移）。
    """
    if not isinstance(char, dict):
        return {}
    appearance = str(char.get("appearance") or "")
    plan: Dict[str, dict] = {}
    if appearance:
        zh = str(char.get("reference_prompt_zh") or "")
        en = str(char.get("reference_prompt_en") or "")
        plan = build_plan(appearance, zh + "\n" + en)
        if plan:
            if zh:
                char["reference_prompt_zh"] = align_zh(zh, plan)
            if en:
                char["reference_prompt_en"] = align_en(en, plan)
    # ⚠️ **无条件**尝试补条款（与 plan 是否为空无关）：plan 为空只说明「没有冲突」，
    # 但「遗漏」类问题 —— canon 特征压根没进提示词 —— 同样会让分镜死循环。
    # 实测林清雪 appearance 的「眉间朱砂」没被写进参考提示词，而她那 10 个出场镜头
    # 里有 8 个的 description 都写了朱砂（→ 质检口径要求它），资产图必然没有。
    ensure_canon_clause(char)
    ensure_gender_en(char)
    return plan


def _shot_names(shot: dict, known: Sequence[str]) -> List[str]:
    """本镜相关角色名：``characters_in_shot`` ∪ 「名字出现在镜头文本里的角色」。

    后者为了覆盖「镜头只登记了 A，但画面描述里也写了未出场的 B」；
    因此必须配 :func:`_is_canon_of_any` 那道保险，否则会把 B 的 canon 误当 A 的冲突。
    """
    names = [str(n) for n in (shot.get("characters_in_shot") or []) if n]
    blob = " ".join(str(shot.get(f) or "") for f in _SHOT_TEXT_FIELDS)
    for n in (known or []):
        if n and n in blob and n not in names:
            names.append(n)
    return names


def _is_canon_of_any(value: str, canon: set) -> bool:
    """该取值是不是**某个角色的正确设定**（是 → 不许当成漂移去改）。

    ⚠️ 用**双向子串**判定，不能只做字面相等：``_hair_parts`` 取到的短语可能只是
    某个 canon 的一部分 —— 实测林清雪 canon 是「银白长发垂腰」，而镜头文本里出现
    「银白长发」（正是她的特征），只做字面相等时 ``银白长发`` 不在 canon 集合里，
    保险失效 → 于是把她的长发当成**羡进**的漂移，要改成「黑发马尾」（会把
    林清雪写成羡进的发型）。子串判定把这一例正确拦住。
    """
    if not value:
        return False
    for c in (canon or ()):
        if c and (value == c or value in c or c in value):
            return True
    return False


def _scan_shot_conflicts(script: dict) -> List[Tuple[dict, Dict[str, dict]]]:
    """扫出镜头文本里所有「可疑外形取值」：``[(shot, {kind: {"item":..., "goods":set}})]``。

    ``goods`` = 该取值在各出场角色的 canon 里对应的**全部**候选目标。只有一个候选才敢
    自动改；多个候选说明归属不唯一（见 :func:`find_ambiguous`）。
    """
    characters = [c for c in (script.get("characters") or []) if isinstance(c, dict)]
    known = [str(c.get("name") or "") for c in characters]
    canon = canon_tokens(characters)
    appearance_of = {str(c.get("name") or ""): str(c.get("appearance") or "")
                     for c in characters}
    out: List[Tuple[dict, Dict[str, dict]]] = []
    for shot in (script.get("shots") or []):
        if not isinstance(shot, dict):
            continue
        blob = " ".join(str(shot.get(f) or "") for f in _SHOT_TEXT_FIELDS)
        if not blob.strip():
            continue
        slots: Dict[str, dict] = {}
        for name in _shot_names(shot, known):
            ap = appearance_of.get(name) or ""
            if not ap:
                continue
            for kind, item in build_plan(ap, blob).items():
                if _is_canon_of_any(item["bad_zh"], canon):
                    continue   # 别人的正确设定，不是漂移
                slot = slots.setdefault(kind, {"item": item, "goods": set()})
                slot["goods"].add(item["good_zh"])
        if slots:
            out.append((shot, slots))
    return out


def iter_shot_conflicts(script: dict) -> List[Tuple[dict, Dict[str, dict]]]:
    """列出镜头文本里**可自动收敛**的条目：``[(shot, {kind: 计划项}), ...]``。

    候选计划的来源是「**本镜出场角色**的 ``appearance``（canon）直接对比**本镜文本**」，
    而不是「角色参考提示词的漂移值」—— 后者会漏掉「镜头文本自己写了第三种取值」
    （写分镜的 LLM 同时看到两边、可能另起一种写法）。

    ⚠️ 只返回**目标唯一**的条目：同一取值若对应多个角色的 canon（多角色同框的散文里
    很常见），归属无从判断 —— 改错会把另一个角色的外形写坏，因此交给
    :func:`find_ambiguous` 报出、不自动改。

    另外 `_is_canon_of_any` 会跳过「恰好是别人正确设定」的取值（含子串情形）。
    """
    result: List[Tuple[dict, Dict[str, dict]]] = []
    for shot, slots in _scan_shot_conflicts(script):
        plan = {k: s["item"] for k, s in slots.items() if len(s["goods"]) == 1}
        if plan:
            result.append((shot, plan))
    return result


def find_ambiguous(script: dict) -> List[dict]:
    """列出**归属不唯一、不敢自动改**的镜头冲突（供守卫 / 告警）。

    典型实测例（逆天系统第1集 shot_25，多角色同框）：
        「…羡进侧身一闪…他身体微侧，**黑发垂肩**被掌风扫起」
    「黑发垂肩」既可能是赵天霸 canon「黑寸头」的漂移、也可能是羡进 canon「黑发马尾」
    的漂移；纯文本无法判断指的是谁。此时**不自动收敛**（宁可漏改也不改错），
    但必须报出来，不能静默放过。
    """
    out: List[dict] = []
    for shot, slots in _scan_shot_conflicts(script):
        for kind, s in slots.items():
            if len(s["goods"]) > 1:
                out.append({"where": f"shots[{shot.get('shot_id')}]", "kind": kind,
                            "bad": s["item"]["bad_zh"],
                            "candidates": sorted(s["goods"]),
                            "why": "同一取值对应多个角色的 canon，无法判断归属"})
    return out


def align_shots(script: dict) -> int:
    """把计划应用到镜头文本字段（description 等），返回被改动的字段数。"""
    changed = 0
    for shot, plan in iter_shot_conflicts(script):
        for field in _SHOT_TEXT_FIELDS:
            val = shot.get(field)
            if isinstance(val, str) and val:
                new = align_zh(val, plan)
                if new != val:
                    shot[field] = new
                    changed += 1
    return changed


def find_conflicts(script: dict) -> List[dict]:
    """列出仍未收敛的冲突（供守卫 / 运行时告警）。返回每条含 where/kind/bad/good。"""
    out: List[dict] = []
    characters = [c for c in (script.get("characters") or []) if isinstance(c, dict)]
    canon = canon_tokens(characters)
    # ① 角色参考提示词（zh 判中文残留；en 判英文写法残留）
    for char in characters:
        name = str(char.get("name") or "")
        appearance = str(char.get("appearance") or "")
        if not appearance:
            continue
        for key in _CHAR_PROMPT_FIELDS:
            raw = str(char.get(key) or "")
            if not raw:
                continue
            plan = build_plan(appearance, raw)
            for kind, item in plan.items():
                if _is_canon_of_any(item["bad_zh"], canon):
                    continue
                if key.endswith("_en"):
                    residual = any(
                        re.search(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", raw,
                                  re.IGNORECASE)
                        for words, _g in (item.get("en_pairs") or []) for w in words)
                else:
                    residual = item["bad_zh"] in raw
                if residual:
                    out.append({"where": f"characters[{name}].{key}", "kind": kind,
                                "bad": item["bad_zh"], "good": item["good_zh"],
                                "character": name})
    # ② 镜头文本
    for shot, plan in iter_shot_conflicts(script):
        for kind, item in plan.items():
            out.append({"where": f"shots[{shot.get('shot_id')}]", "kind": kind,
                        "bad": item["bad_zh"], "good": item["good_zh"]})
    return out


def reconcile_script(script: dict, *, log: logging.Logger = None) -> dict:
    """一次收敛整份剧本：角色参考提示词 + 所有镜头文本字段。

    返回 ``{"characters": n, "fields": n, "clauses": n, "ambiguous": n, "plans": {...}}``。
    只在检测到真实冲突时才改动；无冲突且条款齐备时是纯粹的读操作（幂等）。
    """
    lg = log or logger
    if not isinstance(script, dict):
        return {"characters": 0, "fields": 0, "clauses": 0, "ambiguous": 0, "plans": {}}
    plans: Dict[str, Dict[str, dict]] = {}
    clauses = 0
    for char in (script.get("characters") or []):
        if not isinstance(char, dict):
            continue
        # 用「条款匹配文本前后是否变化」计数 —— 既覆盖**新增**也覆盖**升级**
        # （早期条款含 current_outfit，本次口径改动需要把旧条款换掉）。
        body_before = _canon_clause_body(char)
        plan = align_character(char)
        body_after = _canon_clause_body(char)
        if body_after != body_before:
            clauses += 1
            lg.info("[外形一致性] 角色「%s」参考提示词的「锁定设定」条款已%s：%s",
                    char.get("name"), "新增" if not body_before else "更新", body_after)
        if plan:
            plans[str(char.get("name") or "?")] = plan
            lg.info("[外形一致性] 角色「%s」参考提示词已收敛：%s", char.get("name"),
                    "；".join(f"{v['bad_zh']}→{v['good_zh']}" for v in plan.values()))
    fields = align_shots(script)
    # ⚠️ 归属不唯一的必须**报出来**，不能静默放过 —— 这类镜头仍可能触发质检摇摆，
    # 只是自动改的风险更大（会把另一个角色的外形写坏），故留给人工确认。
    amb = find_ambiguous(script)
    if amb:
        lg.warning("[外形一致性] %d 处镜头文本的外形取值归属不唯一，未自动收敛（需人工确认）：%s",
                   len(amb),
                   "；".join(f"{x['where']} {x['bad']}→{'/'.join(x['candidates'])}"
                             for x in amb[:3]))
    if plans or fields or clauses:
        lg.info("[外形一致性] 收敛完成：%d 个角色、%d 处镜头文本已对齐 appearance，"
                "%d 条锁定设定条款新增/更新", len(plans), fields, clauses)
    return {"characters": len(plans), "fields": fields, "clauses": clauses,
            "ambiguous": len(amb), "plans": plans}
