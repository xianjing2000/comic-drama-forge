# -*- coding: utf-8 -*-
"""提示词模板中心（2026-10-07，借鉴 Moha 的 prompts/ 设计）。

分层：用户覆盖（数据目录）> 出厂默认（app/prompts/*.txt）> 代码内兜底常量。
- 变量渲染用「安全占位符替换」：只替换 {word} 形式且**注册在案**的变量，
  模板里其它花括号（JSON 示例等）一律原样保留 —— 绝不用 str.format（会被
  JSON 花括号炸掉）。
- 覆盖文件存 PROJECT_DATA_DIR/prompt_overrides/<name>.txt（不被打包覆盖，
  桌面版数据目录可写）。
- 模板文件的 `#` 头注释块（用途 / 调用位置 / 变量说明）是给人看的文档：
  load() 返回前会剥掉文件开头的注释行与其后的空行，注释不进提示词正文；
  编辑器接口（list/get）回显的则是**文件原文**（含头注释），所见即所存。
- 全部读路径 try/except 容错：读失败返回空串（或注册的 fallback），
  由调用方 `or 兜底常量` 接住，绝不因模板文件缺失/损坏阻断生成与质检链路。
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

#: 出厂模板目录（app/prompts/，随源码分发）
_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

#: 模板注册表：name → 元数据（title / file / variables）。
#: 代码内兜底正文**不在本表**，由各消费模块在导入期 ``register_fallback(name, text)``
#: 写进模块级 ``_FALLBACKS``（谁持有兜底谁注册，避免两处存同一份正文漂移）。
REGISTRY = {
    "script_rewrite_rules": {
        "title": "剧本改写规则（压缩提炼 / 一镜一动作 / 镜头语言克制 13 条）",
        "file": "script_rewrite_rules.txt",
        "variables": [],
    },
    "qc_script_check": {
        "title": "剧本质检提示词（结构 / 逻辑 / 风格 / 可执行性 / 短剧节奏）",
        "file": "qc_script_check.txt",
        # 与 qc_client.check_script 的 str.replace 注入链一一对应；
        # ⚠️ 改这里必须同步改 check_script 的 replace 链，反之亦然。
        "variables": ["style", "target_duration", "shot_duration_min",
                      "shot_duration_max", "shot_duration_silent",
                      "speech_budget", "script_data"],
    },
    "screenplay_generate": {
        "title": "文学剧本生成指令（场次剧本改写，人审层）",
        "file": "screenplay_generate.txt",
        "variables": [],
    },
    "script_generate": {
        "title": "分镜批量生成",
        "file": "script_generate.txt",
        # 与 novel_to_script.build_shots_for_chunk 的 render("script_generate", ...) 注入链
        # 一一对应（2026-10-07 提示词外置）；⚠️ 改这里必须同步改该 render 调用与模板正文，
        # 反之亦然。prev_block ~ preflight_block 五个块变量自带尾部换行（_ctx_line 口径，
        # 空块为空串）；target_shots / shots_cap 在正文各出现两处，render 会全部替换。
        "variables": ["rules", "drama_title", "chunk_title", "chunk_index", "chunk_total",
                      "target_shots", "shots_cap",
                      # ⭐ 2026-10-10：镜数口径由「固定下限」改为「可无下限」。这三条文案
                      # 在 shots_target<=0（不预设）与 >0（显式指定下限）时取值不同，
                      # 由 novel_to_script 算好后传入；目标是不再让一个默认数字把内容摊薄。
                      "shots_min_text", "shots_range_text", "shots_targeting_text",
                      "style", "style_guide",
                      "prev_block", "bible_block", "contract_block", "style_block",
                      "camera_block", "preflight_block", "prev_tail",
                      "char_brief_json", "item_brief_json", "scene_brief_json",
                      "chunk_text", "outline_summary", "key_beats_json",
                      "shot_type_count", "shot_type_enum", "speech_budget"],
    },
    "storyboard_grid_plan": {
        "title": "九宫格逐格规划（单镜 9 关键帧拆解 → 严格 JSON 数组，文本 LLM）",
        "file": "storyboard_grid_plan.txt",
        # 与 app.py._grid_panel_plan 的 render("storyboard_grid_plan", ...) 注入链一一对应；
        # ⚠️ 改这里必须同步该 render 调用与模板正文，反之亦然。
        # 输出消费：9 个 {"no","framing","tone","content"} 对象的 JSON 数组（panel_plans）。
        "variables": ["shot_id", "duration", "style", "characters",
                      "description", "action", "first_frame", "last_frame",
                      "motion", "emotion", "dialogue"],
    },
    "storyboard_grid_main": {
        "title": "九宫格分镜图主提示词（中文逐格版 · 单镜 9 关键帧 · 用户范本结构）",
        "file": "storyboard_grid_main.txt",
        # 与 comfyui_client.build_shot_grid_keyframes_prompt 的
        # render("storyboard_grid_main", ...) 注入链一一对应（panel_plans 非空时走本模板，
        # 为空时回落英文版路径，两路互不影响）。⚠️ 改这里必须同步该 render 调用与模板正文。
        "variables": ["style", "shot_summary", "characters_section",
                      "panel_plans", "color_arc", "speech_note"],
    },
    "scene_grid_main": {
        "title": "九宫格场景设定图主提示词（中文逐格版 · 单次出图 · 1.5MP）",
        "file": "scene_grid_main.txt",
        # 与 comfyui_client.build_scene_grid_prompt 的 render("scene_grid_main", ...) 一一对应。
        # ⚠️ 改这里必须同步该 render 调用与模板正文，反之亦然。
        "variables": ["style", "scene_prompt", "panel_plans"],
    },
}

#: 消费方注册的代码内兜底正文（模块导入期写入；load() 的最后一级回落）
_FALLBACKS = {}

#: 安全占位符：只匹配「花括号内是纯词字符」的形态（{style} / {script_data}）。
#: JSON 示例如 {"score": …} 因引号/空格/冒号不匹配，天然不会被误替换。
_VAR_RE = re.compile(r"\{(\w+)\}")


def register_fallback(name: str, text: str) -> None:
    """注册代码内兜底正文（各消费模块导入期调用；幂等，后注册者覆盖）。"""
    try:
        _FALLBACKS[name] = str(text or "")
    except Exception as e:  # noqa: BLE001 —— 注册失败只降级，不影响模块导入
        logger.warning("提示词兜底注册失败（name=%s）：%s", name, e)


def default_path(name: str) -> str:
    """出厂默认模板路径（app/prompts/<file>）；未知 name 返回 ""。"""
    entry = REGISTRY.get(name)
    if not entry:
        return ""
    return os.path.join(_PROMPTS_DIR, entry["file"])


def override_path(name: str) -> str:
    """用户覆盖模板路径（PROJECT_DATA_DIR/prompt_overrides/<file>）。

    ⚠️ 延迟 import config 防循环；config 未就绪时回落源根（桌面版数据目录
    不可用的极端情况下，覆盖功能降级但不抛异常）。
    """
    entry = REGISTRY.get(name)
    if not entry:
        return ""
    try:
        import config  # 延迟 import 防循环
        root = config.PROJECT_DATA_DIR
    except Exception as e:  # noqa: BLE001
        logger.warning("读取数据目录失败（prompt_overrides 回落源根）：%s", e)
        root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(root, "prompt_overrides", entry["file"])


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _strip_header(text: str) -> str:
    """剥掉模板文件**开头**的 `#` 头注释块与其后的空行（头注释是文档，不进提示词）。

    只处理文件最前面的注释行；正文里任何位置的 `#` 一律不动
    （screenplay_generate 的正文就含「用 ## 做大标题」字样）。
    无头注释的文件原样返回；全文件都是注释 → 返回 ""（调用方视为「非空校验失败」）。
    """
    if not text:
        return text
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n and lines[i].lstrip().startswith("#"):
        i += 1
    if i == 0:
        return text
    while i < n and not lines[i].strip():
        i += 1
    out = "\n".join(lines[i:])
    if out and text.endswith("\n"):
        out += "\n"
    return out


def load(name: str) -> str:
    """取模板**生效正文**（已剥头注释）：用户覆盖 > 出厂默认 > 注册的代码内兜底。

    - 「覆盖非空」按剥离头注释后的正文判：只有注释的覆盖文件视为空，继续回落；
    - 任一级读取失败只告警并降级，绝不抛异常；
    - 返回空串表示「没有可用模板」，调用方用 `or 兜底常量` 接住。
    """
    if name not in REGISTRY:
        return _FALLBACKS.get(name, "")
    try:
        op = override_path(name)
        if op and os.path.isfile(op):
            txt = _strip_header(_read_text(op))
            if txt.strip():
                return txt
    except Exception as e:  # noqa: BLE001
        logger.warning("提示词覆盖文件读取失败（回落出厂默认）：%s：%s", name, e)
    try:
        dp = default_path(name)
        if dp and os.path.isfile(dp):
            txt = _strip_header(_read_text(dp))
            if txt.strip():
                return txt
    except Exception as e:  # noqa: BLE001
        logger.warning("提示词出厂模板读取失败（回落代码内兜底）：%s：%s", name, e)
    return _FALLBACKS.get(name, "")


def render(name: str, **variables) -> str:
    """取模板并做**安全占位符替换**：只替换 variables 里出现的 {word} 键。

    - 模板里其它 {xxx}（JSON 示例的花括号、未注册变量）一律原样保留；
    - 值为 None 的变量视为未提供（占位符原样保留），便于调用方按需注入；
    - 当前 qc_client.check_script 走的是自己的 str.replace 注入链（机制等价），
      本函数供未来新模板与外部调用统一使用。
    """
    text = load(name)
    if not text or not variables:
        return text

    def _sub(m) -> str:
        key = m.group(1)
        if key in variables and variables[key] is not None:
            return str(variables[key])
        return m.group(0)

    return _VAR_RE.sub(_sub, text)


def source_of(name: str) -> str:
    """当前生效来源："override"（用户覆盖）/ "default"（出厂默认）；未知 name 返回 ""。"""
    if name not in REGISTRY:
        return ""
    try:
        op = override_path(name)
        if op and os.path.isfile(op):
            if _strip_header(_read_text(op)).strip():
                return "override"
    except Exception:  # noqa: BLE001 —— 判定失败按 default 展示，不影响功能
        pass
    return "default"


def save_override(name: str, text: str) -> str:
    """把编辑后的模板写为用户覆盖文件（utf-8 落盘），返回覆盖文件绝对路径。

    未知 name 抛 KeyError（调用方 /api/prompts/save 已先校验，正常到不了这里）。
    """
    if name not in REGISTRY:
        raise KeyError(f"未知的提示词模板：{name}")
    path = override_path(name)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info("提示词模板已保存用户覆盖：%s → %s（%d 字符）", name, path, len(text))
    return path


def reset_override(name: str) -> bool:
    """删除用户覆盖文件，恢复出厂默认。返回是否真的删了文件（失败只告警返回 False）。"""
    if name not in REGISTRY:
        raise KeyError(f"未知的提示词模板：{name}")
    try:
        path = override_path(name)
        if path and os.path.isfile(path):
            os.remove(path)
            logger.info("提示词模板已重置为出厂默认：%s（删除 %s）", name, path)
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("删除提示词覆盖文件失败（name=%s）：%s", name, e)
        return False
    return False


def list_prompts() -> list:
    """全部模板清单（供 /api/prompts/list 与编辑器回显）。

    text = **文件原文**（含 # 头注释，所见即所存，编辑后整页保存）；
    文件缺失/读失败时依次回落另一级文件、注册的代码内兜底，最后才是空串。
    """
    out = []
    for name, entry in REGISTRY.items():
        text = ""
        try:
            op = override_path(name)
            if op and os.path.isfile(op):
                text = _read_text(op)
        except Exception as e:  # noqa: BLE001
            logger.warning("提示词覆盖文件回显读取失败（name=%s）：%s", name, e)
            text = ""
        if not text.strip():
            try:
                dp = default_path(name)
                text = _read_text(dp) if (dp and os.path.isfile(dp)) else ""
            except Exception as e:  # noqa: BLE001
                logger.warning("提示词出厂模板回显读取失败（name=%s）：%s", name, e)
                text = ""
        if not text.strip():
            text = _FALLBACKS.get(name, "")
        out.append({
            "name": name,
            "title": entry["title"],
            "source": source_of(name),
            "text": text,
            "length": len(text),
            "variables": list(entry["variables"]),
        })
    return out


# ===================== script_generate（分镜批量生成）的代码内兜底正文（2026-10-07） =====================
# 骨架 = novel_to_script.build_shots_for_chunk 原 f-string 的静态部分（占位符见上方 REGISTRY
# 的 variables），与 app/prompts/script_generate.txt 剥离头注释后的正文**逐字一致**。
# ⚠️ novel_to_script 在模板三级都取不到时还有自己的原 f-string 兜底分支 —— 三处正文必须同文，
# 改任何一处都要同步另外两处（渲染差异会改变 prompt 内容指纹、打断分镜断点缓存命中）。
_DEFAULT_SCRIPT_GENERATE = """【任务】为漫剧《{drama_title}》的「{chunk_title}」（第 {chunk_index}/{chunk_total} 段）编写分镜：{shots_min_text}。把下方原文**压缩提炼**成可拍摄的镜头，只保留推动剧情的关键情节（冲突/转折/关键动作/金句），纯背景铺陈直接删去、勿逐句照搬。
【粒度口径（**务必先读**）】{shots_targeting_text}每镜 5~6 秒（不是每镜 2 秒的快切）。
⚠️ **镜数不设上下限**：既没有「至少 N 镜」的下限，也没有「不得超过 N 镜」的上限。唯一的准绳是
**原文的信息密度**与**剧情完整度** —— 原文里的冲突/转折/关键动作/金句必须全部落到镜头里
（覆盖率有硬校验），但不得为凑数灌水、也不得为省事合并丢掉情节。
  · **镜数少了不等于少写情节**：目标镜数变少时，请把相邻的连续情节**合并进同一个镜头**（一个镜头里可以容纳一个完整的动作过程、以及前后两段关键情节），而**不是**把原文情节丢掉。原文里的冲突/转折/关键动作/金句仍必须**全部**落到镜头里 —— 本系统对原文覆盖率有硬校验，漏情节会导致整集重跑。
  · **不要把一个完整动作拆成几个镜头**：「抬手→握拳→挥出」是**一个**镜头里的连续动作，不是三个镜头。只有当**空间/时间/视角真的发生跳跃**（换了地点、跳了时间、要强调另一个主体）时才切镜。
  · **不要为同一件事再补一个镜头**：已经拍过的道具/手部，不要为了「规避人脸」再单独切一个几乎同画面的插入镜。

【描述粒度口径（2026-10-06 用户指定：写关键动作分解、只写关键节点，**务必先读**）】
  · description 写「**起点 → 关键动作节点 → 结果**」：动作过程写**关键动作分解**（用→连接的 2~4 步，如：走到椅前→扶椅背转身→缓缓落座），只写关键节点、不写琐碎中间步（「拉开窗帘」写「走到窗前→拉开窗帘」即可，不写手指逐片推开的过程）。
  · 琐碎中间步与逐格推进**由视频/九宫格生成阶段负责**：5~6 秒镜头的 9 宫格就是把关键节点之间的时间自然切片，剧本层只写 2~4 个关键节点即可——把琐碎中间步写满反而让 9 个格全画同一个中间态（九宫格雷同，用户已纠正）。
  · 每镜 description **不设字数上下限** —— 内容需要多长就写多长，写清：主体、关键动作分解、关键构图位置、情绪落点。光线/氛围/背景**只在推动剧情或首次出场时写一句**，不逐句铺陈（不要为凑字数堆砌，也不要为压字数丢信息）。
{rules}
【全剧风格】{style}　【画面风格指南】{style_guide}
{prev_block}{bible_block}{contract_block}{style_block}{camera_block}{preflight_block}{prev_tail}【可用角色】{char_brief_json}
【可用物品】{item_brief_json}
【可用场景】{scene_brief_json}
【本段原文（先压缩提炼：只保留冲突/转折/关键动作/金句，纯背景铺陈直接删去，勿逐句照搬）】
{chunk_text}
【本段剧情摘要】{outline_summary}
【本段情节要点】{key_beats_json}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{"shots": [{"camera": "景别+运镜（必须取自上方运镜术语表，如 中景跟随/近景轻推，10 字以内）", "shot_type": "景别（只填以下 {shot_type_count} 值之一：{shot_type_enum}；局部=只拍手部/道具的插入镜；与 camera 里的景别词保持一致；本镜确实无法确定景别时写空字符串）", "camera_motion": "运镜（只填运镜词，优先用上方【克制运镜·推荐】的 固定/轻推/轻摇/跟随/轻手持；无运镜的静止镜头填「固定」）", "location": "所属场景名（必须来自可用场景）", "description": "画面内容描述（100~120字，下限100上限120，按「起点→关键动作节点→结果」写：谁做了什么、**动作过程写关键动作分解（用→连接的 2~4 步，如：走到椅前→扶椅背转身→缓缓落座）**、**做完后画面是什么状态**、在画面什么位置；只写关键节点不写琐碎中间步（琐碎中间步交给九宫格/视频阶段切片）；外貌衣着/环境光线只在推动剧情或首次出场时写一句，不逐句铺陈，禁止写背景陈述/世界观/来历评述）", "visual_detail": "画面补充细节（可选；当 description 之外还有本镜**结果态**里必须交代的关键环境/道具状态时写在这里，≤120字；不要重复动作过程；没有多余细节时写空字符串）", "dialogue": [{"speaker": "说话角色名（必须与可用角色完全一致）", "text": "该角色台词（≤30 字；原文对话尽量原样保留；角色的自语/心声写成该角色本人的台词）"}], "emotion": "情绪（8 字以内）", "edit_reason": "剪辑动机（20~30字，具体说明这一镜为什么切/承担什么叙事功能，禁止写可套用的空话，如：切掉环境只留下他的反应/用空间拉开取代告别对白/物件回环把十年压缩到一张纸上）", "beat": "叙事节拍（本镜所处节拍，只填「开场」「触发」「高潮」「收尾」四值之一；拿不准填「触发」）", "audio_cues": "音效/配乐提示（60 字以内，只写环境音/音效/配乐，不写人声）", "characters_in_shot": ["出场角色名"], "first_frame": "首帧画面（运动开始前那一刻的静态快照：画面主体与构图，40字以内；无明显运动变化写空字符串）", "last_frame": "末帧画面（运动结束后的终态，40字以内；与首帧相同或无运动时写空字符串）", "motion": "运动描述（严格区分【摄影机运动】推拉摇移跟升降 与【画面内运动】人物/物体自身动作；30字以内；静止镜头写空字符串）", "caption": "字幕（**默认写空对象 {}**；仅当本镜承担时空落点交代或集尾悬念时才写，形如 {"text": "字幕文字（≤20字）", "kind": "时间地点/回溯/悬念 三值之一"}。字幕是后期叠加的文字，不进画面描述、不产生人声）", "items_in_shot": ["出场物品名"]}]}
【禁止输出 prompt_h3 字段】视频提示词由程序在生成阶段按 H3 规范自动构建（它会结合当次实际传入的参考图，生成 subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 六段）。你在剧本阶段并不知道最终配几张参考图，写出来的英文提示词缺少 <Picture N> 标签，反而会覆盖规范提示词导致出片偏离设定。因此**不要写 prompt_h3、不要写英文提示词**；把画面信息全部写进 description 即可。
【站位与动作（3D 导演台依赖，逐镜必填）】每一镜都要写：

  ① blocking：本镜出场角色的**站位**，每个出场角色一条，形如 

{"name": 角色名, "x": left/center/right, "depth": front/mid/back, "facing": camera/left/right/back}。

     · x = 画面左右（left=画面左）；depth = 离镜头远近（front=更靠近镜头）；facing = 朝向，留空表示朝内。

     · **左右顺序与前后层次必须与本镜情节一致**（谁在左、谁更靠近镜头），
禁止所有镜头套用同一套站位；角色互换攻守、走近/退开时，站位要跟着变。

  ② action：本镜的**动作 beat**——谁做了什么、动作从哪到哪（例：「羡进抬头直视赵天霸，右手缓缓握拳」）。
**不得留空**，纯对话镜也要写神态与小动作。

【台词要求】dialogue 必须是数组，数组元素为 {"speaker": 角色名, "text": 台词}；speaker 必须精确等于「可用角色」中的名字，禁止写“旁白/众人”等未登记角色；无台词的镜头 dialogue 写 []（空数组），禁止写成字符串或 null。角色的心理活动改写成该角色**本人**的自语台词时，speaker 仍写角色名（不要写成「旁白」，本系统没有旁白角色）。dialogue **只承载**：原文对话、以及原文明确心理活动/独白改写的第一人称自语——第三人称叙述与背景补叙**禁止**写成任何角色开口的台词（改写规则 8）。
【台词预算（防成片截断）】单个镜头的 dialogue **合计不超过 {speech_budget} 字**（≈3.6 秒配音，按 config.CHARS_PER_SECOND=4.5 字/秒）。台词过多时**先精简冗余语气词与重复表述**，仍超预算才拆成相邻镜头——配音是按镜头时间轴铺的，单镜台词超出镜头时长会被成片尾部静默截掉。
【音轨说明（本系统不产出旁白）】成片没有画外音解说，配音链路**只读 dialogue**：audio_cues 里写「雨声」「风声」这类音效**不会产生人声**。因此：① 有对话或自语的镜头必须写 dialogue，禁止把台词塞进 description / visual_detail / audio_cues；② 纯画面/纯动作镜头允许没有台词（该镜成片留白，由音效与配乐铺底），但**必须**在 audio_cues 写明音效/配乐提示；③ **严禁**凭空编造原文里没有的台词来「凑人声」——宁可留白，也不要无中生有。
【字幕/转场（caption）】本系统不产出旁白，**时空跳跃靠字幕点明落点**。只在三种情况写 caption：
①上下集之间时间/地点发生跳变（kind=时间地点，如「一年后」「青茅山·古月山寨」）；
②本镜是时空回溯的落点（kind=回溯，如「春秋蝉，逆转时光。」）；
③本集结尾仍有未回收伏笔、需要留住悬念（kind=悬念）。
**其余镜头一律写空对象**——字幕滥用会打断观感。caption.text **不设字数上限**，只写交代时空或悬念的短句；**禁止**复述台词、禁止写画面描述、禁止把台词搬进字幕。
【硬性约束】{shots_range_text}。⭐ **原文里的全部内容都要落到镜头里（2026-10-10 用户口径：「剧本要还原小说的所有细节，不能过度改写」）**：推动剧情的冲突/转折/关键动作/金句**必须**；背景补叙与环境描写**也要**——可以并入相邻镜头（不要求单独成镜），但**不得删去**。**合并镜头可以，丢弃原文信息不可以。**

【忠实原文·改写边界（2026-10-10 用户口径，权重最高）】
允许并鼓励的改写（**形式转换**）：
  · 把小说叙述改写成剧本可拍格式：场次头（内/外景·地点·时间）＋「△ 」动作行＋具名对白；
  · 提取人物 / 物品 / 场景，登记进 characters / items / scenes 数组（这是必要的结构化）；
  · 把心理描写改写成可拍的外部动作、神态，或第一人称自语（本系统无旁白）；
  · 把一段叙述拆成多个镜头，或把相邻几件事合并进同一镜头（镜头是手段，不是目的）；
  · 补充**画面化细节**（光线方向、构图位置、动作分解、景别运镜）—— 只要不与原文冲突、不新增情节。
禁止的改写（**内容删改**）：
  · 改写 / 合并 / 删除原文对白（必须逐字保留，语气、断句、标点均不得动）；
  · 丢弃原文的情节、冲突、转折、关键动作、金句；
  · 丢弃原文的环境描写与背景补叙（可并入相邻镜头，但**不得删去**）；
  · 新增原文没有的情节、角色、道具、地点；
  · 改变原文的人物动机、因果链与结局。
一句话：形式上从「小说」变成「剧本」，内容上**只能增、不能减**。name 字段必须与上面「可用角色/物品/场景」中的名字完全一致，不要新造名字。若上方给出「本集必须出现的原文金句」，必须把每句**原样**写进对应角色的 dialogue.text（不得改写、不得拆分、不得省略）。**原文的角色对白同样逐字保留**，不得改写语气、不得合并、不得删减。上一集已发生的事件禁止在本集重演。
【关键情节自检】写完回看上方「剧情摘要/情节要点」，确认每个关键情节都有对应镜头；纯背景补叙、纯环境描写若未推进剧情应当已删去，**不要求逐句覆盖原文**。记住：本系统没有旁白，背景补叙与环境描写靠画面承载、绝不写成台词，心理活动靠神态动作或第一人称角色自语承载。"""
register_fallback("script_generate", _DEFAULT_SCRIPT_GENERATE)
