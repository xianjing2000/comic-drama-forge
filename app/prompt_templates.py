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
        "title": "剧本改写规则（**合理改编·不得无脑删改** / 一镜一动作 / 镜头语言克制 13 条）",  # 2026-10-10 用户口径：改的是体裁形式，不是内容取舍
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


# ===================== script_generate（分镜批量生成）模板单一来源 =====================
# ⭐ 2026-10-12（t50 下沉单一来源）：原 _DEFAULT_SCRIPT_GENERATE 是硬编码副本，与
#   app/prompts/script_generate.txt 剥离头注释后的正文逐字一致 —— 两处存同一份正文，
#   改一处漏另一处就漂移（渲染差异会改 prompt 内容指纹、打断分镜断点缓存命中）。
#   现删除硬编码副本：script_generate 的唯一来源是 app/prompts/script_generate.txt
#   （load 回落链 = 用户覆盖 > 出厂默认文件 > 空串）。
#   文件缺失时 load 返回空串，novel_to_script 会 raise ScriptTemplateError 明确报错
#   （其 2026-10-10 第三步已删除 f-string 兜底，宁可停下也不静默用旧文本）。
