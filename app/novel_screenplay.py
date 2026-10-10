"""文学剧本层（人审层，2026-10-03）

用户决策的两段式生产：
  章节正文 → ①文学剧本（给人看/人审，Markdown，前端展示）→ ②改写为分镜表（机器消费）

本模块只负责 ①：用 LLM 把一章正文改写成「场次剧本」——
格式为用户指定（见 GENERATE_INSTRUCTIONS）：核心情节分析 + 场次结构
（【第X场】内/外景·地点·时间 + △ 动作行 + 具名角色对白 + 画外音/字幕）。
产物落盘 output/screenplays/<项目键>/第N集_文学剧本.md，由前端展示；
②阶段（_episodes_worker）以本产物为「原文」走既有改写链路。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

# 提示词模板中心（2026-10-07 外置改造）：GENERATE_INSTRUCTIONS 的生效值从这里加载。
import prompt_templates

logger = logging.getLogger(__name__)

try:
    from config import PROJECT_OUTPUT_DIR as _OUTPUT_ROOT
except Exception:  # noqa: BLE001  config 未就绪时退回源根
    _OUTPUT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "output"))


def screenplays_root() -> str:
    return os.path.join(_OUTPUT_ROOT, "screenplays")


def _safe_project_key(project_key: str) -> str:
    """项目键清洗（防路径穿越）。

    2026-10-05 统一：与分镜剧本侧同一清洗规则，防止 screenplays/ 与 scripts/ 目录键分叉
    ——旧实现是「白名单替换 + 截 60 字符」，而 scripts/ 侧（novel_to_script.safe_project_name）
    是「替换 \\/:*?\"<>| 与空白 + 截 40 字符」，两套规则对超长或含特殊字符的项目键会推导出
    两个不同目录。现直接委托 safe_project_name，两侧目录键恒一致；路径穿越防护不变
    （反斜杠/正斜杠/冒号均被替换，screenplay_path 仍有 commonpath 包含校验兜底）。
    函数内延迟导入：novel_to_script 为下游重模块，避免加载顺序/循环导入耦合。
    """
    from novel_to_script import safe_project_name
    return safe_project_name(project_key)


def screenplay_path(project_key: str, episode_no: int) -> str:
    """产物路径：root/<安全项目键>/第N集_文学剧本.md；规范化并强制落在 root 内。"""
    root = os.path.realpath(screenplays_root())
    p = os.path.realpath(os.path.join(
        root, _safe_project_key(project_key), f"第{int(episode_no)}集_文学剧本.md"))
    if os.path.commonpath([p, root]) != root:
        raise ValueError(f"screenplay 路径越出数据根：{p}")
    return p


# 2026-10-07 提示词外置（app/prompts/screenplay_generate.txt）：常量改名保留为**代码内兜底**；
# 实际生效值在常量定义之后由 prompt_templates.load("screenplay_generate") 加载。
# 已核实本常量无任何运行时改写（全文件仅此一处赋值，消费点仅 generate_screenplay）。
_DEFAULT_GENERATE_INSTRUCTIONS = (
    "你是资深短剧/漫剧编剧。请把用户给出的小说章节正文，改编成「可拍摄的文学剧本」"
    "（供制片人/导演人审，后续再由另一个模型改写成机器分镜表）。硬性要求：\n"
    "一、先输出简短分析（共 4 小节，每节 2-4 条）：\n"
    "  1. 核心情节（本章结构：起承转合/关键转折）；\n"
    "  2. 叙事手法（视角、时序、对比、独白等）；\n"
    "  3. 人物功能（每个出场人物一行：身份 + 本章作用）；\n"
    "  4. 主题与悬念（主题、留给下一集的钩子）。\n"
    "二、然后输出「剧本改编」正文，规则：\n"
    "  1. 开头给 片名 与 改编说明（一句：时空结构/转场设计/取舍）；\n"
    "  2. 按场次组织：【第X场】单独一行，下一行写「内景或外景·地点·时间」；\n"
    "  3. 动作与画面用「△ 」开头的行描述（每行一个可拍摄的画面/动作，写清人物、"
    "位置、光线与关键细节）；\n"
    "  4. 对白格式「角色名：台词」，角色名**必须按原文具名**（原文写群雄甲/乙/丙/"
    "女仇敌就保留这些名字，禁止合并成「群雄」或「众人」）；\n"
    "  5. **原文对白逐字保留**，不得改写、不得合并、不得删除；原文没有对白的动作"
    "不要造台词；内心独白写「角色名（画外音）：」；\n"
    "  5.1 ⚠️ **「字幕：」只用于真正的屏幕 / 设备文字**（手机屏、电视屏、电脑弹窗、"
    "监控画面上的字），且**每条不超过 20 字、每场不超过 2 条**；\n"
    "  5.2 ⚠️⚠️ **画面里的实体文字一律不要写成「字幕：」**（2026-10-10 用户实测纠正）："
    "纸张、告示、守则、招牌、门牌、书页、铭牌、横幅、证件、宣传单上的字都属于**画面内容**，"
    "必须写进「△ 」动作行里（如「△ 特写：打印的 A4 纸，宋体小字排列整齐；纸面标题为"
    "「天合大厦A座 — 楼层安全守则」，正文为纵向编号的小字条款，末尾无落款」）；"
    "**严禁**把这类文字的全文拆成一长串「字幕：」连续输出（实测：八条规则被写成 9 行"
    "「字幕：」，既不是字幕，也会在生产链路里被当成越界内容丢弃）；\n"
    "  5.3 若某件道具的文字需在画面上逐字可读，请在 △ 行里写清「该道具表面呈现的确切"
    "文字为：<原文>」，供后续剧本改写阶段填入 surface_text；不要替它决定排版细节；\n"
    "  6. 场次划分按时空转换自然切分，每场 3-12 个 △ 行；\n"
    "  7. 直接输出 Markdown（用 ## 做大标题、### 做场次标题），不要解释你做了什么。"
)

# 2026-10-07 提示词外置：实际生效 = app/prompts/screenplay_generate.txt（可被
# PROJECT_DATA_DIR/prompt_overrides/screenplay_generate.txt 覆盖）；任一级读失败回落
# 上方 _DEFAULT_GENERATE_INSTRUCTIONS，行为与外置前逐字一致（load 已剥离模板头注释）。
GENERATE_INSTRUCTIONS = prompt_templates.load("screenplay_generate") or _DEFAULT_GENERATE_INSTRUCTIONS
prompt_templates.register_fallback("screenplay_generate", _DEFAULT_GENERATE_INSTRUCTIONS)


def generate_screenplay(client, book_title: str, chapter_title: str,
                        chapter_text: str, style: str = "") -> str:
    """调 LLM 把一章正文改写成文学剧本（Markdown）。失败抛异常，由调用方落任务态。"""
    user = (f"【书名】{book_title or '（未命名）'}\n"
            f"【目标风格】{style or '（未指定）'}\n"
            f"【本章标题】{chapter_title or ''}\n"
            f"【本章正文】\n{chapter_text}")
    resp = client.chat(
        [{"role": "system", "content": GENERATE_INSTRUCTIONS},
         {"role": "user", "content": user}],
        temperature=0.5, max_tokens=16384)
    md = str(resp or "").strip()
    if len(md) < 200:
        raise RuntimeError(f"文学剧本生成结果过短（{len(md)} 字符），疑似模型输出异常")
    return md


def save_screenplay(path: str, markdown: str) -> str:
    """原子落盘（唯一临时名 + replace）。path 必须来自 screenplay_path()（已含包含校验）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    Path(tmp).write_text(markdown, encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_screenplay(project_key: str, episode_no: int) -> str:
    p = screenplay_path(project_key, episode_no)
    if not os.path.isfile(p):
        return ""
    try:
        return Path(p).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
