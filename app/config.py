"""
漫剧生成系统配置

配置来源（优先级从高到低）：
1. 环境变量（含项目根目录 .env，由 _load_dotenv 自动加载）
2. 代码内默认值

P0-2 改造：原先硬编码的 ComfyUI / 模型路径已全部改为环境变量驱动，
换机只需修改 .env（见项目根目录 .env.example），无需改代码。
"""
import json
import logging
import os
import re

# 环境加载与项目根目录统一由 env_loader 负责（导入即生效，避免模块导入顺序导致 .env 未加载）
from env_loader import PROJECT_ROOT_DIR, PROJECT_DATA_DIR, env as _env, env_int as _env_int  # noqa: E402

logger = logging.getLogger(__name__)


def _norm_path(p: str) -> str:
    """规范化路径：统一分隔符，兼容 Windows / Linux 写法"""
    return os.path.normpath(p) if p else ""


# ===================== ComfyUI 配置 =====================
COMFYUI_URL = _env("COMFYUI_URL", "http://127.0.0.1:8188")
MJSCXT_COMFYUI_DIR = _env("MJSCXT_COMFYUI_DIR", "D:\\ComfyUI_portable_TE_v260619\\ComfyUI")

# ComfyUI 安装根目录：其余路径默认基于此推导，只需配置这一项即可换机
COMFYUI_ROOT = _env("COMFYUI_ROOT", "")

# 是否启用路径推导（未显式配置各路径时，按 ComfyUI 根目录推导）
# 支持两种常见目录结构：
#   A) COMFYUI_ROOT/ComfyUI/ComfyUI/{workflows,input,output,models}   （portable 版）
#   B) COMFYUI_ROOT/{user/default/workflows,input,output,models}       （标准安装）
def _derive_comfyui_paths(root: str) -> dict:
    if not root:
        return {"workflows": "", "input": "", "output": "", "models": ""}
    candidates = [
        root,
        os.path.join(root, "ComfyUI", "ComfyUI"),
        os.path.join(root, "ComfyUI"),
    ]
    for base in candidates:
        # 只认“真实存在的目录”：避免旧逻辑在无网/未装 ComfyUI 时误判
        if os.path.isdir(os.path.join(base, "models")):
            return {
                "workflows": os.path.join(base, "user", "default", "workflows"),
                "input": os.path.join(base, "input"),
                "output": os.path.join(base, "output"),
                "models": os.path.join(base, "models"),
            }
    # 无法探测时按 portable 结构兜底（与旧行为一致，不改变未配置时的默认值）
    base = os.path.join(root, "ComfyUI", "ComfyUI")
    return {
        "workflows": os.path.join(base, "user", "default", "workflows"),
        "input": os.path.join(base, "input"),
        "output": os.path.join(base, "output"),
        "models": os.path.join(base, "models"),
    }


_DERIVED = _derive_comfyui_paths(COMFYUI_ROOT)

COMFYUI_WORKFLOWS_DIR = _norm_path(_env("COMFYUI_WORKFLOWS_DIR", _DERIVED["workflows"]))
COMFYUI_INPUT_DIR = _norm_path(_env("COMFYUI_INPUT_DIR", _DERIVED["input"]))
COMFYUI_OUTPUT_DIR = _norm_path(_env("COMFYUI_OUTPUT_DIR", _DERIVED["output"]))
# ComfyUI temp 目录（DLSS 补帧等节点在此建工作目录；被外部清理会导致收尾
# FileNotFoundError → 整集成片丢失，见 comfyui_client.wait_for_completion 的看门狗）
COMFYUI_TEMP_DIR = _norm_path(_env(
    "COMFYUI_TEMP_DIR",
    os.path.join(os.path.dirname(_DERIVED["output"]), "temp")))

# DLSS 补帧节点旁路（2026-10-01）：NvidiaDLSSFrameInterpolation 在 ComfyUI temp
# 下建工作目录，该目录被外部（TE 启动器/磁盘清理）周期性删除 → mkdtemp 报
# WinError 3 → 收尾文件不存在 → 整集成片丢失（实测连续 18 次提交全死于此）。
# 设为 True 后工作流跳过补帧节点（CreateVideo 直接进 SaveVideo），
# 24fps 原速出片（无 50fps 插帧），FlashVSR 超分不受影响。
# 2026-10-05：与 _env_bool 白名单语义对齐（非法值一律视为 False）；默认 '1' = DLSS 旁路生效
H3_DISABLE_DLSS = _env("H3_DISABLE_DLSS", "1").strip().lower() in ("1", "true", "yes", "on")

# ===================== 工作流模板目录（项目自包含） =====================
# ⚠️ 为什么要有这一段：工作流 JSON 以前只存在于本机 ComfyUI 的
#    `user/default/workflows/` 里，项目仓库一个都不带。后果是**别人下载项目后
#    直接不可用**（每个模板都找不到文件），而且换机 / 重装 ComfyUI 就会集体失效。
#    现在把项目引用的全部模板随项目一起分发，默认从项目内解析。
PROJECT_WORKFLOWS_DIR = _norm_path(os.path.join(PROJECT_ROOT_DIR, "workflows"))

# 解析优先级：
#   1) MJSCXT_WORKFLOWS_DIR 显式指定目录（最高，换机/调试用）
#   2) PROJECT_WORKFLOWS_DIR 项目内 workflows/（默认，保证开箱可用）
#   3) COMFYUI_WORKFLOWS_DIR 本机 ComfyUI 目录（回落）
# 设 MJSCXT_WORKFLOWS_PREFER=comfyui 可把 ComfyUI 目录提到项目目录之前，
# 便于「在 ComfyUI 里现调工作流、立刻让项目用上」的本地迭代。
_WORKFLOWS_DIR_EXPLICIT = _norm_path(_env("MJSCXT_WORKFLOWS_DIR", ""))
_WORKFLOWS_PREFER_COMFYUI = (
    _env("MJSCXT_WORKFLOWS_PREFER", "").strip().lower() == "comfyui")


def workflow_search_dirs() -> list:
    """工作流文件的查找目录顺序（去重、剔除空值）。"""
    dirs = []
    if _WORKFLOWS_DIR_EXPLICIT:
        dirs.append(_WORKFLOWS_DIR_EXPLICIT)
    dirs.extend([COMFYUI_WORKFLOWS_DIR, PROJECT_WORKFLOWS_DIR]
                if _WORKFLOWS_PREFER_COMFYUI
                else [PROJECT_WORKFLOWS_DIR, COMFYUI_WORKFLOWS_DIR])
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def resolve_workflow_path(workflow_file: str) -> str:
    """工作流文件名（或相对路径）→ 实际磁盘路径。

    按 ``workflow_search_dirs()`` 顺序取**第一个存在**的文件；全都不存在时返回
    项目内候选路径（让报错信息直接指向「模板应该放在哪」）。
    绝对路径原样返回，便于测试/临时替换。
    """
    if not workflow_file:
        return ""
    name = str(workflow_file)
    if os.path.isabs(name):
        return _norm_path(name)
    dirs = workflow_search_dirs()
    for d in dirs:
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return _norm_path(cand)
    base = PROJECT_WORKFLOWS_DIR if PROJECT_WORKFLOWS_DIR in dirs else (
        dirs[0] if dirs else "")
    return _norm_path(os.path.join(base, name))

# 模型路径
MODELS_DIR = _norm_path(_env("MODELS_DIR", _DERIVED["models"]))
QWEN_IMAGE_MODEL = os.path.join(MODELS_DIR, "diffusion_models", "qwen-image-2512",
                                "qwen_image_2512_fp8_e4m3fn.safetensors") if MODELS_DIR else ""
H3_MODEL = os.path.join(MODELS_DIR, "diffusion_models", "minimax-h3",
                        "minimax_h3_ref2va_pruned_int8_convrot.safetensors") if MODELS_DIR else ""
FLASHVSR_MODEL = os.path.join(MODELS_DIR, "FlashVSR-v1.1",
                              "diffusion_pytorch_model_streaming_dmd.safetensors") if MODELS_DIR else ""

# FlashVSR 超分模型目录（ComfyUI-FlashVSR_Ultra_Fast 约定：models/FlashVSR-v1.1）
FLASHVSR_MODEL_DIR = os.path.join(MODELS_DIR, "FlashVSR-v1.1") if MODELS_DIR else ""
FLASHVSR_REQUIRED_FILES = [
    "diffusion_pytorch_model_streaming_dmd.safetensors",   # DiT 主权重
    "Wan2.1_VAE.pth",                                      # VAE
    "LQ_proj_in.ckpt",                                     # 低质特征投影
    "TCDecoder.ckpt",                                      # 时序解码器
]

# LLM 配置（P0-3：密钥推荐走环境变量，代码内不保存明文）
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
WORKBUDDY_API_KEY = _env("WORKBUDDY_API_KEY")
LLM_PROVIDER = _env("LLM_PROVIDER", "anthropic")  # anthropic or workbuddy

# 部署档案（8G / 16G / server）：影响超分与视频生成的默认档位
DEPLOY_PROFILE = _env("DEPLOY_PROFILE", "16G")

# ===================== 输出目录（须先于各产物子目录定义） =====================
# 可写数据一律落数据根（MJSCXT_DATA_DIR 守卫，默认=源根）；只读资源仍留源根。
PROJECT_OUTPUT_DIR = os.path.join(PROJECT_DATA_DIR, "output")

# ===================== 小说上传与 LLM 配置路径 =====================
# 小说上传目录（原始文件 + 解析后的标准化文本 + 元数据索引）
NOVELS_DIR = os.path.join(PROJECT_DATA_DIR, "novels")

# 自定义 LLM API 配置（base_url / api_key / model 持久化，api_key 不明文回显）
LLM_CONFIG_PATH = os.path.join(PROJECT_DATA_DIR, "llm_config.json")

# AI 质检配置（总开关 / 图片·视频独立开关 / 模型 / 判定标准 / 最大重试次数）
QC_CONFIG_PATH = os.path.join(PROJECT_DATA_DIR, "qc_config.json")
QC_DIR = os.path.join(PROJECT_OUTPUT_DIR, "qc")   # 质检与重试历史 + 视频抽帧

# 视频水印配置（C 项：默认关闭；支持文案/图片、位置、字号、透明度、边距、全视频移动模式）
WATERMARK_CONFIG_PATH = os.path.join(PROJECT_DATA_DIR, "watermark_config.json")
WATERMARK_DIR = os.path.join(PROJECT_OUTPUT_DIR, "watermark")   # 带水印视频产物目录

# P0-4 持久化任务队列（SQLite）：任务全生命周期落盘，支持断点续跑
TASKS_DB_PATH = os.path.join(PROJECT_OUTPUT_DIR, "tasks.db")
TASK_QUEUE_CONCURRENCY = _env_int("TASK_QUEUE_CONCURRENCY", 1)   # 单 GPU 建议保持 1
TASK_UNIT_MIN_BYTES = _env_int("TASK_UNIT_MIN_BYTES", 1024)      # 单元产物视为有效的最小字节数

# 统一「AI 设置」：文本分析 / 质检 / 对话总控 三个相互独立的模型模块（各自 base_url / api_key / model）
AI_CONFIG_PATH = os.path.join(PROJECT_DATA_DIR, "ai_config.json")
AI_MODULES = ("text", "qc", "chat")

# 小说解析与转换参数
# ⚠️ 2026-10-05：实际分块以 novel_to_script.CHUNK_CHARS=2400 为唯一事实源。
# 旧 NOVEL_CHUNK_CHARS=3000 / NOVEL_MAX_CHUNKS=8 已随整本转剧本路径（/api/novels/<id>/convert）
# 下线而删除，勿再加回。
# 默认目标镜头数：**0 = 不预设**（2026-10-10 用户指定）。
# 语义：镜数交给模型按原文信息密度自行判定 —— 原文信息量大就多切、少就少切，
# 不再用一个固定数字当**下限**把内容摊薄或灌水。
# ⚠️ 此前这里的 12 会被写进提示词的【硬性约束】("shots 数组元素个数必须在 12 ~ 16 之间")，
#    而同一份模板里又写着「本片目标是每集 20~30 个镜头」—— 两条指令自相矛盾。
# 显式传 4~40 仍可按题材指定下限（如悬疑推理 18~30）。
NOVEL_DEFAULT_SHOTS = 0
NOVEL_PREVIEW_CHARS = 4000      # 前端预览单页字符数
NOVEL_BRIEF_CHARS = 800         # 「原著简报」正文取样字符数（喂给 AI 总控做风格判断，≤ agent 结果窗口）
# 单次 LLM 请求超时（秒）。⚠️ 必须可 env 覆盖：reasoning_effort=max + 长章节（数千字正文）
# 的剧本生成会一路提额 max_tokens（9300→12288→16384→24576），单次最重调用实测连 900s 都
# 不够（2026-09-19 ep002 第一节 3297 字，900s 仍 ReadTimeout 反复 5 次）。
# ⚠️ 2026-09-23 复测教训：默认 1800s 太激进 —— agnes 网关「只吐思考」/无响应时，单次请求
# 会挂满 30 分钟，期间进程看似无响应，实测触发一次「无报错日志的服务自动重启」。
# ⚠️ 2026-09-23 二次复测（本次）修正：当时把超时降到 600s 的前提是「空正文=网关挂起」，
# 但那个前提已被证伪 —— 空正文的真实原因是「max_tokens 低于思考水位」（已修，见
# llm_client.REASONING_ONLY_TOKEN_FLOOR），且修复后 reasoning-only 已 0 次发生（快速失败生效）。
# 而水位修到 16K 后**合法调用本身就要 4~5 分钟**（实测 19984 额度 → 276s，思考 26K 字符）。
# 600s 对「单次调用 + 一轮重试」已不够，会出现假 ReadTimeout 打断正常生成。
# 现设 1200s：覆盖「最重合法调用（~5min）+ 充分余量」，同时仍能在 20 分钟内暴露真挂起。
# 需要更严/更松仍可设 env LLM_REQUEST_TIMEOUT。
LLM_REQUEST_TIMEOUT = int(_env("LLM_REQUEST_TIMEOUT", "1200"))

# ===================== 项目级隔离（每部小说 = 一个独立项目） =====================
# 注册表与每项目配置/隔离目录；各产物仍落在既有 output/<kind>/<项目键>/ 下，
# 由「项目键（dir_key）」实现物理隔离，注册表负责把项目键与小说/剧本绑定起来。
PROJECTS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "projects")
PROJECT_INDEX_PATH = os.path.join(PROJECTS_DIR, "index.json")           # 项目列表（注册表）
PROJECT_TRASH_DIR = os.path.join(PROJECTS_DIR, "_trash")               # 删除项目的回收站（可恢复）
PROJECT_MIGRATE_REPORT = os.path.join(PROJECTS_DIR, "migration_report.json")   # 历史数据归属迁移报告

#: 景别（取景档）**唯一权威表**：按「由近到远」排列。
#:
#: 所有环节都必须以本表为源，禁止各写一份：剧本枚举（novel_to_script 白名单 +
#: 提示词文案）/ 运镜术语表（continuity.CAMERA_TERMS）/ 生成端构图规范
#: （comfyui_client.SHOT_CAMERA_SPECS）/ 分镜质检取景判定（prompt_qc.SB_FRAMING_KEYS）/
#: H3 英文名（h3_prompt_kit._CAMERA_EN）/ 3D 导演台取景档（te_3d_director._FRAMING_SPAN）。
#:
#: ⚠️ 2026-09-29 统一（审计 P2 #14）：此前五处口径互不相同 —— 术语表 6 值（含大特写）、
#: 剧本白名单 5 值、SB_FRAMING_KEYS 5 值、SHOT_CAMERA_SPECS 5 值、3D 导演台 5 值。
#: 而 REWRITE_RULES 明确要求模型「景别以中景/中近景/近景为主，局部近景用于情感道具回环」，
#: 模型真写了「中近景」却被白名单判为**枚举漂移丢弃**（生成端 H3 表里明明支持它）。
#: 参考片 92 镜拆解里「中近景 / 局部」正是主力景别（局部 = 手部/道具插入镜），
#: 旧口径下根本表达不出来。
SHOT_TYPES = ("大特写", "特写", "近景", "中近景", "局部", "中景", "全景", "远景", "大远景")

#: 景别中文说明（剧本提示词 / 前端展示共用；生成端构图规范另见
#: comfyui_client.SHOT_CAMERA_SPECS —— 那份是给模型看的「判定标准」）
SHOT_TYPE_LABELS = {
    "大特写": "极近距离：眼睛、手指等单点细节占满画面",
    "特写": "面部或手部/道具占画面 70% 以上，背景明显虚化",
    "近景": "人物胸部以上至头顶，面部细节清晰",
    "中近景": "腰部以上、胸部以下（介于近景与中景之间）",
    "局部": "只拍手部/道具/身体局部，不出现完整人脸（插入镜）",
    "中景": "人物腰部或膝部以上至头顶，可带部分环境",
    "全景": "完整全身及其所处环境",
    "远景": "人物较小、环境为主体，强调空间感",
    "大远景": "人物极小，环境与空间关系为主",
}


# ===================== 动作节拍（一镜一动作）唯一权威表 =====================
#
# 用途：① 生成期拆镜（novel_to_script._split_multi_action_row）—— 一镜塞了多个连续动作时
#       拆成相邻两镜，让成片真正产生「切换」；② 质检软告警（qc_client 第 ④ 层）——统计
#       多动作镜占比。
#
# ⚠️ 唯一权威表：两处必须都从这里取。历史教训：景别白名单曾在五处各写一份、口径互不相同
#   （见上方 SHOT_TYPES 注释），最终导致模型写对了也被判「枚举漂移」丢弃。动作词表同理——
#   生成期与质检期口径一旦漂移，拆镜与告警就会互相打架。
#
# 参考片口径：92 镜几乎每镜只有 1 个动作节拍，靠相邻镜切换推进。
#
# ⚠️ 只用**多字动作词**：单字词（冲/收/放/接/背/走/拍/看…）的误报率实测极高——
#   「雨水**冲**刷得发亮」被算成动作、「收银台」里的「收」被算成动作、「**背**景」里的
#   「背」被算成动作。而这个计数**会驱动生成期自动拆镜**，误报 = 把好镜头拆碎，
#   比不拆更糟。故宁可少认，不可错认：所有单字词一律换成语境明确的二字/三字词。
ACTION_WORDS = (
    # 位移
    "转身", "回头", "走近", "走到", "走向", "跑向", "奔向", "迈步", "跨过", "退后",
    "后退", "转身而去", "停下", "躲开", "扑向", "追去", "冲上", "冲进", "冲出", "蹲下",
    "躺下", "爬起", "起身", "坐下", "站起", "跪下", "站稳",
    # 上身与手势
    "弯腰", "俯身", "低头", "抬头", "抬起", "抬起手", "点头", "摇头", "侧身", "抬手", "伸手", "举手",
    "挥手", "推开", "拉住", "拽住", "按住", "按在", "拍向", "拍在", "敲响", "敲了",
    "翻找", "掏出", "取出", "收回", "收起", "收进", "递出", "接过", "攥紧", "握住",
    "握紧", "拿起", "放下", "捡起", "扔下", "抢过", "抱起", "扶住", "擦去", "抹去",
    "掀起", "举起", "撕开", "打开", "关上", "展开", "卷起",
    # 表情与状态
    "皱眉", "咬紧", "喘气", "颤抖", "苦笑", "冷笑", "笑了", "哭了", "落泪", "流泪",
    "瘫坐", "瘫倒",
    # 单字动作词：表达力强但误命中率高（「收」在收银台、「背」在背景、「冲」在冲刷、
    # 「站」在车站…）。它们必须配合下面 ACTION_NEGATIVE_WORDS 的剔除使用，
    # 见 action_clauses —— 绝不单独裸用。
    "走", "跑", "推", "拉", "拽", "按", "拍", "敲", "握", "攥", "举", "递", "接", "收", "放",
    "掏", "塞", "捡", "扔", "挥", "抢", "抱", "扶", "擦", "抹", "掀", "撕", "折", "看", "望",
    "盯", "瞥", "笑", "哭", "咬", "喘", "退", "躲", "扑", "追", "站", "坐", "蹲", "跪", "披",
    "戴", "背", "冲", "翻", "倒", "摔",
)

#: 非动作复合词：出现在短句里时，先从短句中**剔除**再做动作词匹配。
#: 例：「石阶被雨水冲刷得发亮」剔除「冲刷」后不再误命中「冲」；「收银台」剔除「收银」后
#: 不再误命中「收」。这是单字动作词能安全使用的前提。
ACTION_NEGATIVE_WORDS = (
    "冲刷", "冲突", "收银", "背景", "背影", "背书", "走廊", "直接",
    "放心", "放弃", "节拍", "拍打", "难看", "转折", "玩笑", "节奏", "车站", "坐落", "倒影",
    "翻涌", "披风", "摔打", "推敲", "拉锯", "接续", "抬头纹", "开口", "开口说", "站台",
    "看台", "看客", "望族", "笑纹", "咬文", "气喘", "退路", "躲闪", "追问", "扑克", "翻看",
)

#: 动作短句的断句符（数节拍与拆镜共用的切分口径）
ACTION_CLAUSE_RE = re.compile(r"[，,。；;！？!?\n]+")


def action_clauses(text: str) -> list:
    """切出文本里**含动作词**的短句列表（保序、去重）。

    ⚠️ 必须先按短句去重再计数：模型常把同一个动作在 description / visual_detail / motion
    里各写一遍（实测「林风握紧断剑」三处各写一次 → 旧口径算 3 个节拍，实际只有 1 个）。
    去重后同一动作只算一次。
    """
    if not text:
        return []
    out, seen = [], set()
    for seg in ACTION_CLAUSE_RE.split(str(text)):
        seg = seg.strip()
        if not seg or seg in seen:
            continue
        # 先剔除非动作复合词，再用词表匹配（单字动作词的安全前提）
        probe = seg
        for neg in ACTION_NEGATIVE_WORDS:
            if neg in probe:
                probe = probe.replace(neg, "")
        if any(w in probe for w in ACTION_WORDS):
            seen.add(seg)
            out.append(seg)
    return out


def count_action_beats(text: str) -> int:
    """数一段文本里可辨认的「动作节拍」个数（= 含动作词的互异短句数）。

    口径：先按中文断句符切成短句并去重，含动作词的短句各算 1 个节拍；同一短句里出现
    多个动作词仍只算 1 个节拍（「抬手擦了擦」是 1 个动作，不是 2 个）。

    这是「一镜一动作」的唯一判定口径：生成期拆镜与质检期告警都调它。
    """
    return len(action_clauses(text))

# ===================== 单镜时长模型（用户口径·唯一权威）=====================
#
# ⚠️ 唯一权威：novel_to_script（生成期推算）与 qc_client（质检 OK 区间）都从这里取。
# 历史缺陷：两处各写一份（剧本端 3~12 秒 vs 质检端 1~15 秒），约束互相打架、
# 正常剧本反被判「不可执行」（见 qc_client 旧注释）。
#
# 口径沿革（两轮方向相反的用户改动，都记在这里，别只留最后一版）：
#
# 【第一轮 2026-09-30】按参考片 92 镜实测重标定到 **2.0 秒下限**。
#   旧口径（MIN 3s / 静默基准 3s / 描述 +2s / 动作 +1.5s / 高潮 +2s）让每个镜头的
#   地板就是 4~6 秒，而参考片单镜中位只有 2.08 秒 → 用户反馈「分镜没有切换、
#   一镜演好几件事」，根因在时长模型而不在运镜。
#
# 【第二轮 2026-10-06】用户改口径：**每集 20~30 镜、每镜 5~6 秒**。
#   实测（桌面版 `进境_整本小说` 第 1 集，落盘剧本 + 运行日志）：
#       56 镜 / 总 158.13 秒 / 平均 2.82 秒，其中 **34 镜正好卡在 2.0 秒下限上**。
#   直接危害面（用户原话「9 宫格都有重复的了」）：分镜九宫格 = **单镜 9 关键帧**
#   （时间推进，整图即分镜本体）。2.8 秒的镜头硬切 9 帧，时间轴上根本调不出 9 个
#   互异的画面 → 大量格子几乎一样。**镜头越短，重复越明显**。
#   处置（用户选定「减镜增时长」）：下限 2.0 → 5.0 秒，配套三处「加镜」机制关掉
#   （见下方 `SHOT_GRANULARITY_*` 与 novel_to_script 的 REF_INSERT_RATIO /
#    SPLIT_ACTION_BEATS / CHARS_PER_SHOT）。回滚 = 把本值改回 2.0 并关掉那三处。
SHOT_DURATION_MIN = 5.0            # 单镜最短秒数（用户口径：每镜 5~6 秒）
SHOT_DURATION_MAX = 8.0            # 单镜最长秒数（留余量给长台词；>6 秒的镜属长尾）
SHOT_DURATION_SILENT = 0.6         # 无台词纯画面镜头的基准秒数（旧值 3.0s）
CHARS_PER_SECOND = 4.5             # 中文配音语速基准（字/秒），按台词长度推算时长
BEAT_CLIMAX_BONUS_SEC = 0.7        # 「高潮」节拍镜的画面停留加成（旧值 2.0s）
#: 画面描述带来的时长加成上限（旧值 2.0s）+ 折算系数（每多少字给 1 秒）
SHOT_DURATION_DESC_SEC_MAX = 0.5
SHOT_DURATION_DESC_CHARS_PER_SEC = 150.0
#: 动作复杂度带来的时长加成上限（旧值 1.5s）
SHOT_DURATION_ACTION_SEC_MAX = 0.5
#: 单镜台词字数预算（16 字 ≈ 3.6 秒配音）。配合规则 13 一起约束单镜台词长度。
SHOT_SPEECH_BUDGET_CHARS = 16

# ===================== 分镜粒度（2026-10-06 用户指定：减镜增时长）=====================
# 用户口径：**每集 20~30 镜、每镜 5~6 秒**（≈110~180 秒，正好落在 EPISODE_MAX_SEC=180 内）。
# 背景与后果见上方 SHOT_DURATION_MIN 的两轮口径沿革注释。
#
# ⚠️ 本组常量**不是**全部生效点，只是「口径声明 + 守卫锚点」——真正把镜数压下来的
#    是下面四处（都在 novel_to_script，各自有 env 回滚开关）：
#      · config.SHOT_DURATION_MIN = 5.0                ← 每镜秒数下限（本文件，决定性）
#      · CHARS_PER_SHOT = 240（原 120）                 ← 每镜承载原文翻倍 → 目标镜数近似减半
#      · REF_INSERT_RATIO = 0.0（原 0.22）              ← 不再补「局部插入镜」（同道具重复镜）
#      · SPLIT_ACTION_BEATS 关闭（原 3）                 ← 不再按动作节拍硬拆镜
#      · REF_SHOT_DURATION_MAX = 8.0（原 6.5）          ← 长台词拆镜只在真溢出时触发
#    改任一处前先读该处注释里的实测数据（桌面版第 1 集：56 镜 / 平均 2.82 秒，其中
#    **16 镜是补出来的「局部」插入镜**、**34 镜卡在 2.0 秒下限**）。
SHOT_GRANULARITY_TARGET_SHOTS = 25      # 每集目标镜数（20~30 的中值）
SHOT_GRANULARITY_MAX_SHOTS = 30         # 每集镜数上限（超过即视为「又切细了」，诊断时点名）
SHOT_GRANULARITY_TARGET_SEC = 5.5       # 每镜目标秒数（5~6 的中值）
#: 单块镜头数上限相对目标值的**放大倍数**（2026-10-06 由「2 倍 + 3」收紧而来）。
#: 原口径（`shots_target * 2 + 3`，上限 120）让模型「被允许」写到目标的 2 倍以上 ——
#: 实测第 1 集目标 38 镜、模型产出约 40 镜，再叠加插入镜/拆镜吹到 56 镜。
#: 收紧到 1.35 倍：既留出「模型因剧情需要多发几镜」的余量，又不再给它翻倍的空间。
SHOT_CAP_GROWTH = 1.35

#: 视频生成方式（**项目级设定**，新建项目时由用户选择；全链路唯一口径）。
#:
#: ⚠️ 2026-10-01：**只保留「整集一次生成」**（用户决策）。per_shot / keyframe 两种
#: 模式废弃（norm_video_mode 一律归一 episode）；保留 tuple 仅为兼容既有 import。
#: 2026-10-05：视频模式仅剩 episode（project_store.video_mode 仍在用）；
#: norm_video_mode 恒返回 episode，作为防御性归一保留。
VIDEO_MODES = ("episode",)

#: 视频生成方式的中文标签（后端日志 / 提示文案口径，避免与前端 i18n 两处文字漂移）
VIDEO_MODE_LABELS = {
    "episode": "整集一次生成（连续无缝）",
    "per_shot": "逐镜生成（便于单镜返工）",
    "keyframe": "首尾帧驱动（关键帧插值）",
}

#: 视频生成方式的近义写法（前端 / 接口历史值 / 用户口头词的容错映射）
_VIDEO_MODE_ALIASES = {
    "整集": "episode", "整集生成": "episode", "episode_full": "episode", "full": "episode",
    "逐镜": "per_shot", "per-shot": "per_shot", "single": "per_shot", "shot": "per_shot",
    "关键帧": "keyframe", "keyframes": "keyframe", "fl2v": "keyframe", "首尾帧": "keyframe",
}


def norm_video_mode(value, default: str = "episode") -> str:
    """视频生成方式归一。

    ⚠️ 2026-10-01 需求：**只保留「整集一次生成」**，逐镜（per_shot）/ 关键帧（keyframe）
    两种模式废弃。所有入口（新建项目、托管 plan、接口直传、历史残留）一律归一成 episode，
    避免再走早已弃用的分支、或让用户「选了整集却出单镜」。

    2026-10-05：视频模式仅剩 episode（project_store.video_mode 仍在用）；本函数恒返回
    episode，作为防御性归一保留。
    """
    return "episode"


# 新项目默认配置（每项目一份，落在 output/projects/<项目ID>/config.json）
PROJECT_DEFAULT_CONFIG = {
    "style": "3D动漫渲染",              # 创作风格
    "episodes": 1,                      # 目标集数
    "target_shots": 0,                  # 目标镜头数下限；**0 = 不预设**，由模型按原文信息密度判定
    "shots_per_episode": 12,
    "episode_duration_sec": 90,         # 每集期望时长（秒）
    # ⚠️ 2026-09-26 口径变更：每集时长产品口径 = **1~2 分钟、最长 3 分钟**
    #（单一事实来源是 novel_to_script.EPISODE_TARGET_SEC=90 / EPISODE_MAX_SEC=180）。
    # 这里 90 是「期望中位值」，不是硬约束 —— 实际每集落在 60~180 秒都合法，
    # 集数由章节内容按 EPISODE_MAX_SEC 自动拆分决定（一本 42 章约出 90+ 集）。
    # 旧值 60 是「一集 1 分钟」时代的默认，已随口径切换上调。
    # ⚠️ resolution：遗留展示字段——后端生成链路（app._project_style → style_kit）**不读取**
    # 本键；其 "vertical" 字面是 2026-09-28 翻转前的旧默认残留，与现行 16:9 默认不符，
    # 勿据它推断画幅。画幅唯一事实源 = 风格串（含下方 aspect_ratio 拼入的「画面比例：…」）
    # + style_kit.DEFAULT_RATIO=(16,9)。
    "resolution": "768p_vertical",
    # 画面比例（视频/分镜画幅）。⚠️ **活配置**：app._project_style 读取本键并拼成
    # 「画面比例：16:9 横屏」注入风格串，经 style_kit.aspect_ratio 解析后直接决定
    # 新项目的分镜/视频画幅（显式值优先于 DEFAULT_RATIO 兜底）。2026-09-28 用户拍板
    # 默认由 9:16 翻转为 16:9（与 style_kit.DEFAULT_RATIO=(16,9) 同源），故本值勿改回 9:16；
    # 空=未设置沿用默认。
    "aspect_ratio": "16:9 横屏",
    "fps": 24,
    "duration_per_shot": 5,             # 单镜头默认秒数
    # 视频生成方式（项目级）：**新建项目时由用户选择**，之后「整集生成视频」
    # 与托管生产都按它执行（取值见 VIDEO_MODES / norm_video_mode）。
    "video_mode": "episode",
    "voice_map": {},                    # 角色→音色映射（按项目隔离）
    # 成片硬字幕开关（按项目隔离）：默认关闭。
    # 2026-09-24：H3 提示词已不再往画面里引导字幕（旧版「严禁出现字幕」反而诱导模型自绘），
    # 但成片合成阶段仍会额外烧一层字幕（pipeline.step_final / video_postprocess.finalize_episode）。
    # 用户既然明确要求「不要生成字幕」，则默认不再烧；确需硬字幕的老项目可显式置 true。
    "subtitle_enabled": False,
    # 字幕/转场 caption（时空落点/回溯/集尾悬念）：剧情装置，默认开。
    # 与上面的台词字幕 subtitle_enabled 是两件事，可分别控制。
    "caption_burn_enabled": True,
}

# ===================== 跨集连贯性（相邻两章转剧本改进 A/B/C/D） =====================
# 项目级设定库 / 风格指南 / 金句清单 / 口吻词典 / 运镜术语表 / 各集 state 与校验结果
CONTINUITY_DIR = os.path.join(PROJECT_OUTPUT_DIR, "continuity")


# ---------------------------------------------------------------- 覆盖率阈值（唯一事实源）
def env_float(name: str, default: float, floor: float = 0.0, ceiling: float = 1.0) -> float:
    """读 [floor, ceiling] 区间的浮点环境变量；缺失 / 非法 / 越界一律回落 default。

    2026-10-08：从 novel_to_script._env_float 提到 config。COVERAGE_THRESHOLD 这类
    「两个模块都要读同一个 env」的常量必须有唯一事实源，否则 coverage 只能反向 import
    novel_to_script —— 那正是 coverage → novel_to_script 环边的成因。
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if floor <= val <= ceiling else default


#: 原文覆盖率阈值（低于该值自动补生成缺失片段）；env MJSCXT_COVERAGE_THRESHOLD 可覆盖
COVERAGE_THRESHOLD = env_float("MJSCXT_COVERAGE_THRESHOLD", 0.70, floor=0.0, ceiling=1.0)
SCRIPT_DIR = os.path.join(PROJECT_OUTPUT_DIR, "scripts")
ASSETS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "assets")
CHARACTERS_DIR = os.path.join(ASSETS_DIR, "characters")   # 角色资产（含多视图）
ITEMS_DIR = os.path.join(ASSETS_DIR, "items")             # 物品资产（含3D多视角）
SCENES_DIR = os.path.join(ASSETS_DIR, "scenes")           # 场景资产（含3D多视角）
STORYBOARDS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "storyboards")  # 分镜图（按项目名分子目录）
# 关键帧目录（P1-2 关键帧驱动视频模式）：output/keyframes/<项目>/shot_NN_start.png + shot_NN_end.png
KEYFRAMES_DIR = os.path.join(PROJECT_OUTPUT_DIR, "keyframes")
VIDEOS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "videos")
FINAL_DIR = os.path.join(PROJECT_OUTPUT_DIR, "final")
# 超分结果目录（与原始视频严格隔离：原始在 videos/ final/，超分结果在 upscale/）
UPSCALE_DIR = os.path.join(PROJECT_OUTPUT_DIR, "upscale")

# 超分默认参数（前端可覆盖；mode 对应 FlashVSR Ultra-Fast 的 tiny / tiny-long / full）
UPSCALE_DEFAULT_PARAMS = {
    "scale": 2,                     # 倍率：2 或 4
    "mode": "tiny",                 # tiny / tiny-long / full（8G 显存笔记本默认 tiny）
    "tile_size": 256,
    "tile_overlap": 24,
    "tiled_vae": True,
    "tiled_dit": True,
    "unload_dit": False,
    "color_fix": True,
    "sparse_ratio": 2.0,
    "kv_ratio": 3.0,
    "local_range": 11,
    "precision": "bf16",
    "attention_mode": "sparse_sage_attention",
    "force_offload": True,
    "seed": 0,
    "timeout": 3600,                # 单次超分等待上限（秒）
}

# ===================== 模型常驻（生成完成后不卸载） =====================
def _env_bool(key: str, default: bool) -> bool:
    """布尔型环境变量：1/true/yes/on 为真；0/false/no/off 为假；未设置取 default。"""
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# 生成完成后**保留模型**在内存里，不向 ComfyUI 发 /free（unload_models）。
#
# 为什么要默认开：ComfyUI 的 /free(unload_models=True) 会走 unload_all_models()
# → free_memory(1e30) → cleanup_models_gc() → gc.collect()，把模型**连 RAM 里的权重
# 一起释放**，不只是腾显存。下一次生成就得从磁盘重读 19.5GB 的 H3 模型（每次几分钟），
# 批量生产下这项开销被放大几十倍，是「每次生成都要重新加载模型」的根因。
#
# 关掉卸载**不会**造成显存不够：ComfyUI 自己的显存管理会在下一个任务需要显存时，
# 把暂不用的模型自动 offload 到 RAM（load_models_gpu → free_memory），
# 只是不再把权重丢回磁盘。真正吃紧时它仍会按需腾挪。
#
# 仍想保留旧行为（例如 8G 显存 + 大内存压力）：MJSCXT_KEEP_MODEL_LOADED=0
KEEP_MODEL_LOADED = _env_bool("MJSCXT_KEEP_MODEL_LOADED", True)


# ===================== ComfyUI 任务历史自动清理 =====================
# ComfyUI 界面的「任务历史」面板**只增不减**：质检每失败一次重跑就多一条记录，
# 跑几轮下来面板里几百条，极易被误读成「生成了大量废图」（实测面板 162 条时，
# 磁盘上真正残留的废弃分镜图 0 张）。所以在**任务收尾**（分镜/资产批量跑完、
# 流水线成片收尾）按节流清一次历史列表。
#
# 只清历史记录，不碰磁盘产物，也不影响正在执行/排队中的任务。
# 想保留完整历史用于排查：MJSCXT_CLEAR_COMFYUI_HISTORY=0
CLEAR_COMFYUI_HISTORY = _env_bool("MJSCXT_CLEAR_COMFYUI_HISTORY", True)
#: 两次清理之间的最小间隔（秒）。太频繁会让「刚跑完那一镜」的现场也被清掉。
CLEAR_COMFYUI_HISTORY_INTERVAL_SEC = 300.0


# ===================== 跨项目角色资产库（2026-09-29，借鉴 NiliX） =====================
# 角色设定图（三视图 + 半身档）是**纯确定性**产物：同一段外貌描述 + 同一风格 +
# 同一画幅 + 同一模板，目标形象就应当是同一张图。此前每个项目都要重新渲染一遍 ——
# 同一角色的多项目复用、同一部小说的续集，全在重复烧 GPU。
#
# 开启后：生成角色基础图前先按「形象指纹」查库，命中直接复用（零渲染）；
# 未命中则正常渲染，成功后入库。指纹**包含风格** —— 否则「国漫风」的角色图会被
# 复用进「写实风」项目，直接把画风带错（最坏那类静默错）。
#
# 关掉即恢复原行为：MJSCXT_ASSET_LIBRARY=0
ASSET_LIBRARY_ENABLED = _env_bool("MJSCXT_ASSET_LIBRARY", True)

# 分镜图是否用「单镜九宫格（9 关键帧）」生成（2026-10-02 用户指定）。
# 用户原话「一个 5 秒的分镜就是用的 9 宫格」+「分镜图本体就是九宫格」：
#   一个镜头 = 一张 3x3 九宫格 = 该镜头内随时间推进的 9 个关键帧（不是 9 个镜头、不是 9 机位候选），
#   九宫格整图即分镜图本体，H3 视频直接拿整图当构图参考（不裁切、不选格）。
# ⚠️ 配套：九宫格模式下景别自动裁剪（auto_crop_framing）不适用（会破坏 9 格结构），已禁用。
# 关掉即恢复旧行为（一镜一张单帧）：MJSCXT_SB_GRID=0
SB_GRID_MODE = _env_bool("MJSCXT_SB_GRID", True)


# ===================== 两级生产：预演 → 批准 → 正式（2026-09-29，借鉴 ai-manga-factory） =====================
# 默认**关**（零行为变更）。打开后每集先出一版低成本预演（分辨率减半 + 每段时长压到
# 上限，段数不变所以每一镜都看得到），人工看过并批准后才排正式生产。
#
# 铁律：预演产物**永不可交付** —— 它长得像成片但不是成片，混进交付物索引会让用户
# 拿一版糊图去发布。该不变量由 app/preview_gate.deliverable_ok 在登记入口硬拦。
#
# 打开：MJSCXT_PREVIEW_BEFORE_FINAL=1
PREVIEW_BEFORE_FINAL = _env_bool("MJSCXT_PREVIEW_BEFORE_FINAL", False)



# ===================== 超分引擎：TE-Speed-flashVSR 加速链路（默认） =====================
# 模板来源（只读解析，不修改 ComfyUI 原始工作流文件）：
#   D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\TE-Speed-flashVSR 视频超分放大加速工作流.json
# 加速链：TEFlashVSRModelLoader(mode=tiny/precision=bf16) → TEFlashVSRTuning(sparse_sage2 稀疏注意力
#         + 分块 max_tile_edge/blend_overlap) → TEFlashVSRRestore(scale/color_fix) → TESpeedVideoCombine
UPSCALE_ENGINE = _env("UPSCALE_ENGINE", "te-speed-flashvsr")   # te-speed-flashvsr / legacy-flashvsr
# 2026-10-09 修复「项目自包含」缺口：原实现直连 COMFYUI_WORKFLOWS_DIR（ComfyUI 安装目录），
# 换机/重装 ComfyUI 即失效，且下载项目后开箱不可用 —— 与 2026-09-27 的全量收敛目标不一致
#（其余模板都已走 resolve_workflow_path，只有这一条漏了）。
# 模板已复制到项目内 workflows/，解析顺序：MJSCXT_WORKFLOWS_DIR → 项目 workflows/ → ComfyUI 回落。
UPSCALE_TE_TEMPLATE_PATH = resolve_workflow_path(
    "TE-Speed-flashVSR 视频超分放大加速工作流.json")

# TE-Speed-flashVSR 默认参数（与模板工作流 JSON 中的接线值一致；前端可覆盖）
# 模板实测值：ModelLoader=[FlashVSR-v1.1, tiny, bf16, auto]
#             Tuning=[balanced, 1, auto, auto, 2, 3, 11, 256, 24, 4, sparse_sage2]
#             Restore=[scale 2, color_fix True]
TE_UPSCALE_DEFAULT_PARAMS = {
    "scale": 2,                     # 倍率：2 / 3 / 4
    "mode": "tiny",                 # tiny / tiny-long / full（加速工作流的显存档位，模板=tiny）
    "precision": "bf16",            # bf16 / fp16
    "device": "auto",               # auto / cuda:0
    "quality_profile": "balanced",  # detail / balanced / throughput
    "intensity": 1.0,               # 0.0 - 1.0
    "spatial_strategy": "auto",     # auto / full_frame / adaptive_tiles（低显存优先 adaptive_tiles）
    "memory_policy": "auto",        # auto / resident / staged（低显存可选 staged）
    "attention_backend": "sparse_sage2",   # 加速关键：稀疏 SageAttention2
    "attention_budget": 2.0,        # 1.0 - 2.0
    "kv_retention": 3.0,            # 1.0 - 3.0
    "local_radius": 11,             # 9 - 11
    "max_tile_edge": 256,           # 128 - 2048（分块最大边长，越小越省显存）
    "blend_overlap": 24,            # 0 - 512
    "preprocess_batch": 4,          # 1 - 32
    "color_fix": True,
    "quality_value": 3,             # TESpeedVideoCombine 压缩质量档（1-8）
    "frame_load_cap": 0,            # 0 = 全部帧
    "skip_first_frames": 0,
    "free_vram": not KEEP_MODEL_LOADED,   # 提交前请求 ComfyUI /free（默认关：见 KEEP_MODEL_LOADED）
    "seed": 0,
    "timeout": 3600,                # 单次超分等待上限（秒）
}

# 8G 显存低显存档（前端一键切换；显存吃紧时用）
TE_UPSCALE_LOWVRAM_PARAMS = {
    "mode": "tiny",
    "spatial_strategy": "adaptive_tiles",
    "memory_policy": "staged",
    "attention_budget": 1.0,
    "kv_retention": 1.0,
    "local_radius": 9,
    "max_tile_edge": 256,
    "blend_overlap": 24,
    "preprocess_batch": 2,
    "quality_profile": "throughput",
}

# 超分耗时标定（离线预估用）：output/upscale/calibration.json
UPSCALE_CALIBRATION_PATH = os.path.join(UPSCALE_DIR, "calibration.json")



# ===================== 配音（QwenTTS） =====================
# 配音产物目录：output/dub/<项目>/lines/ 单句 + output/dub/<项目>/<项目>_epNN_配音.wav 合并音轨
DUB_DIR = os.path.join(PROJECT_OUTPUT_DIR, "dub")

# QwenTTS 默认合成参数（前端可按角色覆盖 speaker / instruct / seed 等）
TTS_DEFAULT_PARAMS = {
    "model_choice": "1.7B",         # 1.7B / 0.6B
    "language": "Chinese",
    "precision": "bf16",
    "device": "auto",
    "attention": "auto",
    "temperature": 1.0,
    "top_p": 0.8,
    "top_k": 20,
    "repetition_penalty": 1.05,
    "max_new_tokens": 2048,
    "batch_lines": 4,               # 一次 ComfyUI 提交内合并的台词句数（模型只加载一次）
    # 情绪化配音：把剧本每镜的 emotion 送进 TTS，有情绪时自动切 VoiceDesign 模式。
    # 关掉则退回「每个角色一个固定 preset 音色」的老行为（所有台词一个调）。
    "emotion_aware": True,
    "keep_model_loaded": KEEP_MODEL_LOADED,   # 批次结束后保留模型（默认开：见 KEEP_MODEL_LOADED）
    "timeout": 1800,                # 单批配音等待上限（秒）
}

# ===================== 音画合成（配音轨 × 成片视频） =====================
# 带配音成片落盘目录：output/final_dub/<项目>/<成片名>_dubbed.mp4
DUB_MIX_DIR = os.path.join(PROJECT_OUTPUT_DIR, "final_dub")

# 合成默认参数
MIX_DEFAULT_PARAMS = {
    "mode": "timeline",         # timeline = 按镜头时间轴对齐；concat = 顺次拼接（整轨）
    "lead_in_sec": 0.0,         # 逐句整体提前/延后（秒，可正可负）
    "gap_sec": 0.0,             # 每句之间额外间隔（秒）
    "max_line_sec": 8.0,        # 单句最长占用（0 = 不限制；超出按 atempo 变速压缩，不裁切内容）。
    #                            ⚠️ 2026-09-19 之前这里是 0.0，导致 dub_mix.build_timeline_entries
    #                            里的 `if max_line > 0 and dur > max_line` 恒为假 —— 变速兜底是死代码
    #                            （实测 ep04 全部条目 fit_ratio 恒为 1.0）。台词写超预算时不再有人兜底，
    #                            只能沿时间轴溢出到后面几镜，尾部被成片 `-shortest` 静默截掉。
    #                            取值依据：单镜台词预算 16 字（SHOT_SPEECH_BUDGET_CHARS）≈ 3.6 秒；
    #                            上限 8.0 秒对齐 SHOT_DURATION_MAX，即「正常预算内的台词不动，明显超预算的才压」。
    "video_codec": "copy",      # copy = 不重编码画面（快）；reencode = libx264 重编码
    "audio_bitrate": "192k",
    "sample_rate": 48000,
    "keep_original_audio": True,    # 成片原音轨（H3 生成的环境音/打斗音效）保留并垫底，
                                    # 2026-09-17 由 False 改为 True：此前直接丢弃，导致成片只剩 TTS 人声
    "original_audio_volume": 0.3,   # 原音效垫底音量（0~1）。1.0 会盖住台词；0.3 是"听得到但不抢戏"
    "timeout": 900,             # 单次合成等待上限（秒）
    # 逐句微调（离线标定）：{line_id: 秒}，正值延后
    "line_offsets": {},
}

# 超分 / 合成任务的离线校准（人工听感微调后落盘，可复用）
MIX_CALIBRATION_PATH = os.path.join(PROJECT_OUTPUT_DIR, "final_dub", "mix_calibration.json")

# ===================== H3 视频音频策略 =====================
# H3 是「音视频联合生成」模型（主模型 minimax_h3_ref2va ＋ 专属音频 VAE
# minimax_h3_audio_vae_fp32），工作流里 VAEDecodeAudio → CreateVideo.audio 线路本来就是通的
# → 画面与音效（打斗/雨声/环境音）**本来就一起产出**。
#
# 2026-09-17 起改为「分层保留」：
#   保留 H3 原生音轨 → 用 HDEMUCS 人声分离剔掉 H3 自己生成的对白人声
#   （否则会与全剧统一的 QwenTTS 音色打架）→ 音效以 MIX_DEFAULT_PARAMS.original_audio_volume
#   垫底，QwenTTS 台词叠在上面。
# 旧策略是 H3_EMIT_AUDIO=False + H3_STRIP_AUDIO=True **全部丢弃**，代价是成片完全没有音效
# （实测 output/videos/** 与 output/final/** 全部无音频流，唯一音轨来自 TTS）。
H3_EMIT_AUDIO = True                # True = 保留 CreateVideo.audio 输入（H3 正常产出音轨）
H3_STRIP_AUDIO = False              # False = 不再 ffprobe 剥离音轨（改由人声分离环节处理）
# 末镜音轨人声分离（只留音效/环境声，去掉 H3 自带的说话声）
H3_SFX_ISOLATE = True               # True = 逐镜跑 HDEMUCS 分离；失败 **fail-open** 保留原音轨
H3_SFX_STEMS = (1, 2)               # HDEMUCS 输出下标：0=Bass 1=Drums 2=Other(音效/环境) 3=Vocals
                                    # 取 Drums+Other：打斗的撞击/鼓点常被判进 Drums
H3_SFX_DIR = os.path.join(PROJECT_OUTPUT_DIR, "sfx")   # 分离产物：output/sfx/<项目>/...
# 视频生成是否用**参考音频驱动口型**（audioMode=source，2026-09-27 用户拍板）：
#   True  = 把逐镜 QwenTTS 配音写进 segment.refAudios，H3 用音频驱动口型/节奏。
#           需要该集配音已先行合成（enable_tts_pre），否则自动回退 generate/mute。
#   False = 维持旧行为（H3 不出人声，配音统一后期 dub_mix 混音）。
H3_AUDIO_SOURCE = _env_bool("H3_AUDIO_SOURCE", False)

# H3 视频二采（MiniMaxH3DirectorRefine）开关（2026-09-28 用户拍板：默认关）：
#   False = 构建时裁掉二采专属模型链 + 断开 Director.refine 输入，Director.images
#           自动落一采帧；画质改由 FlashVSR 超分环节承担（见 ENABLE_UPSCALE）。
#           8GB 显存跑二采吃力，故默认关。
#   True  = 维持完整二采链（1088×720 latent 补采样）。
H3_ENABLE_REFINE = _env_bool("H3_ENABLE_REFINE", False)

# H3 Director **公共参考图**（2026-09-30 用户拍板：默认开）。
#   True  = 一次提交里**每一段都在用、且用的是同一张图**的资产（角色/物品/场景）
#           走插件公共参数（``timeline.global.refs`` + ``commonEnabled=true``），
#           占槽位 index 0..K-1 且全段编号恒定；各段自带的私有项从 index K 起。
#           收益：同一资产的 ``<Picture N>`` 在整集里一致（不再随每镜声明顺序漂移），
#           且「公共用哪些资产」在工作流 JSON 里显式可查（便于人工排查）。
#           ⚠️ 判据是**交集**（全段都在）+ **图片路径一致**，不是「出场多」：
#              公共图会被 merge 进每一段，放一个只在部分镜头出场的配角进去，
#              其余镜头都会多出它的锚点（模型会把锚点里的人画进画面）。
#              路径一致的要求也不能省 —— 角色按景别取半身/全身档、场景按机位取
#              四档，只按资产名判交集会把某一镜的档位焊死给全集，等于废掉
#              2026-09-25「景别对档」与 2026-09-29「场景按机位出图」两轮工作。
#   False = 完全走原路径（逐段 refs 从 index 0 起，``commonEnabled=false``）。
#           一键回退用，不改任何其它口径。
H3_COMMON_REFS = _env_bool("H3_COMMON_REFS", True)
# 公共池上限（槽位）：9 格总量里要给「1 张分镜图 + 至少 1 个段级私有项」留位，
# 故实际生效值被 clamp 到 ``H3_COMMON_REFS_MAX <= 9 - 2 = 7``（见 h3_common_refs.plan_common_refs）。
H3_COMMON_REFS_MAX = max(0, _env_int("H3_COMMON_REFS_MAX", 6))

# AI 对话（创作总控）：多轮对话历史 + 已生效的项目创作设定
AI_CHAT_DIR = os.path.join(PROJECT_OUTPUT_DIR, "ai_chat")
AI_CHAT_HISTORY_PATH = os.path.join(AI_CHAT_DIR, "chat_history.json")
AI_SETTINGS_PATH = os.path.join(AI_CHAT_DIR, "project_settings.json")

# 工作流文件
WORKFLOW_TEMPLATE = {
    # H3 视频生成（2026-10-04 起）：官方 Director 插件工作流，**扁平单实例 · 单采**——
    # 一个 MiniMaxH3Director 节点吃整条 timeline_data，段数由 timeline.segments 决定
    # （1 段=逐镜头；整集=N 段），段间由插件原生「段间引导」衔接。
    # 现役模板为用户**亲自跑通**的 12 节点单采工作流（单实例、无二采链、
    # 无 BasicScheduler / MiniMaxH3DirectorRefine / DLSS）。
    # 工作流**不重建拓扑**，只程序化注入 timeline（见 app/h3_director_builder.py）。
    # 旧二采模板 minimax_h3_director_二采_加速.json 保留为**回退**（见下方 h3_video_refine）。
    # 旧母版「H3信号10段测试001.json」（10 个子图实例 + H3ContinuousSeamlessJoinV14
    # 按分镜数重建）仍留在 workflows/ 里，可用 MJSCXT_H3_BUILDER=legacy 一键回退。
    # 注意：判「走哪条构建路径」只看模板**结构**（有无 MiniMaxH3Director 节点），
    # 不看文件名——改这里的值不会让分流逻辑失效。
    "h3_video": "h3_director_r2v_单采.json",
    # H3 视频 · 二采模板（**回退用，勿删**）：23 节点版（含 BasicScheduler /
    # MiniMaxH3DirectorRefine / DLSS 补帧）。与 h3_video **互换即可切回二采模板**：
    # 把本键的值填进 h3_video、或设 MJSCXT_WORKFLOW_MAPPING 覆盖 h3_video 皆可。
    "h3_video_refine": "minimax_h3_director_二采_加速.json",
    # 旧连续拼接母版（回退用，勿删）
    "h3_video_legacy": "H3信号10段测试001.json",
    # ---- 图片链路：2026-09-23 起统一切换到 QwenImage2.1 + TE-Speed 加速链 ----
    # 母版参考 ComfyUI 工作流「TE-Speed-QwenImage21 加速插件-提速30%(1).json」；
    # *_Qwen21.json 由 .workbuddy/tools/build_qwen21_workflows.py 生成（可复现）。
    # 旧的 2512 / 2511 工作流文件**保留在同目录**，改回本表即可整体回滚。
    #
    # ⚠️ 新模板的提示词节点是 TextEncodeQwenImage21：**正负同体**（prompt + negative_prompt
    #    在同一节点），参考图槽位是 autogrow 点号键（images.image_1..3）。
    #    改图片链路前务必先跑 verify_qwen21_migration.py / verify_watermark_slot.py。
    "character_gen": "角色生成_Qwen21.json",     # QwenImage2.1 角色基础图（T2I）
    "item_gen": "物品生成_Qwen21.json",          # QwenImage2.1 物品基础图（T2I）
    # 物品「主人形象」专用模板（2026-10-06）：物品生成_Qwen21.json 纯 T2I 的副本 +
    # 1 个参考图槽（LoadImageOutput → TextEncodeQwenImage21.images.image_1），
    # **保留 1:1 画幅节点**。仅当物品表面承载某角色肖像（owner_photo=True）且能
    # 解析到该角色参考图时启用（见 app._item_owner_ref_image / 资产 worker）。
    # 由 .workbuddy/tools/build_item_ref_workflow.py 生成（可复现）。
    "item_ref_gen": "物品生成_参考图_Qwen21.json",  # QwenImage2.1 物品主人形象（T2I + 1 参考图槽）
    "scene_gen": "场景生成_Qwen21.json",         # QwenImage2.1 场景基础图（T2I）
    "multiview_gen": "分镜生成_Qwen21.json",     # QwenImage2.1 多视角编辑（角色多视图/物品场景3D多视角）
    "storyboard_gen": "分镜生成_Qwen21.json",    # QwenImage2.1 分镜生成（参考图编辑）
}

# ===================== 工作流映射外置（2026-09-29，借鉴 lumenx 的 workflow_mapping） =====================
# 为什么外置：模板名此前**写死在源码里**。换一套工作流要改 Python + 重启，而且改完
# 没有任何「这个项目跑的是哪套工作流」的留痕。
#
# 约定（刻意保守）：
#   * 只允许覆盖**已存在的键** —— 打错的键名只 warning，绝不静默接受。
#     否则一个 typo 会让人以为换了工作流、实际还在跑旧的（最坏那类静默错）；
#   * 缺字段一律回落内置默认 → 可以只写想改的那一条；
#   * 文件不存在 = 完全维持现状；**删掉该文件即回滚**，不需要改代码；
#   * 解析失败 fail-open + warning：坏配置文件绝不阻断启动。
#
# 文件位置：<项目根>/config/workflow_mapping.json（可用 MJSCXT_WORKFLOW_MAPPING 换路径）
# 格式（两种都认）：
#   {"h3_video": "我的H3.json"}
#   {"workflows": {"h3_video": {"template": "我的H3.json"}}}
WORKFLOW_MAPPING_PATH = _norm_path(_env(
    "MJSCXT_WORKFLOW_MAPPING",
    os.path.join(PROJECT_ROOT_DIR, "config", "workflow_mapping.json")))


def _load_workflow_mapping(path: str = None) -> dict:
    """读取可选的 workflow_mapping.json，返回 {键: 文件名}（**只含合法覆盖项**）。

    任何异常都 fail-open（返回空 dict = 沿用内置默认），绝不阻断启动。
    """
    p = path or WORKFLOW_MAPPING_PATH
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}                       # 没有该文件 = 维持现状（最常见的情形）
    except (OSError, ValueError) as e:
        logger.warning("workflow_mapping.json 解析失败（沿用内置默认）：%s", e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("workflow_mapping.json 顶层必须是对象（沿用内置默认）")
        return {}
    table = raw.get("workflows") if isinstance(raw.get("workflows"), dict) else raw
    out = {}
    for k, v in table.items():
        if k not in WORKFLOW_TEMPLATE:
            logger.warning("workflow_mapping.json 含未知键 %r（已忽略）；合法键：%s",
                           k, ", ".join(sorted(WORKFLOW_TEMPLATE)))
            continue
        name = v.get("template") if isinstance(v, dict) else v
        if not isinstance(name, str) or not name.strip():
            logger.warning("workflow_mapping.json 的 %r 取值非法（应为非空字符串），已忽略", k)
            continue
        out[k] = name.strip()
    return out


def apply_workflow_mapping(mapping: dict = None) -> dict:
    """把外置映射**就地**覆盖进 WORKFLOW_TEMPLATE，返回实际生效的覆盖项。

    ⚠️ 必须**就地 update**：其它模块写的是 `from config import WORKFLOW_TEMPLATE`，
    拿到的是**同一个 dict 对象**。若这里改成重新绑定一个新 dict，那些模块仍指向旧对象，
    外置配置会静默失效 —— 这是本功能最容易写错的一处（守卫 H 段专钉它）。
    """
    m = _load_workflow_mapping() if mapping is None else dict(mapping or {})
    applied = {}
    for k, v in m.items():
        if k in WORKFLOW_TEMPLATE and isinstance(v, str) and v.strip():
            old, new = WORKFLOW_TEMPLATE[k], v.strip()
            WORKFLOW_TEMPLATE[k] = new
            applied[k] = {"from": old, "to": new}
            # 顺带体检：映射到的文件是否真的解析得到（解析不到只 warning，不阻断）
            try:
                if not os.path.isfile(resolve_workflow_path(new)):
                    logger.warning("workflow_mapping.json 把 %r 指向 %r，但该工作流文件解析"
                                   "不到（请确认它已在 workflows/ 下，或路径写对）", k, new)
            except Exception as e:                           # noqa: BLE001
                logger.warning("检查映射 %r → %r 时出错（忽略）：%s", k, new, e)
    if applied:
        logger.info("工作流映射外置已生效：%s", json.dumps(applied, ensure_ascii=False))
    return applied


# 导入时应用一次；没有该文件就是**零变化**（内置默认原样保留）
APPLIED_WORKFLOW_MAPPING = apply_workflow_mapping()

# 关键帧「跨镜链式」默认模式：上一镜尾帧 = 下一镜首帧（与参考工作流一致）
#   auto   = 仅相邻两镜同场景时串帧（默认，跨场景切场不串，避免把上一场的画面带进新场）
#   always = 无条件串帧
#   off    = 关闭（旧行为：每镜用自己的分镜图当首帧，镜与镜画面各画各的）
KEYFRAME_CHAIN_MODE = _env("KEYFRAME_CHAIN_MODE", "auto").strip().lower() or "auto"

# TE MAN 3D导演台「程序化站位」（2026-09-26）：把每镜 shot 的角色站位/机位/景别
# 结构化翻译成 scene_json（te_3d_director.py），并派生出「空间锚点」文本注入
# 分镜图提示词（软约束，增强构图稳定性）。
#   True  = 分镜提示词的 SCENE AND ACTION 段追加一条 "Blocking — …" 空间锚点行
#   False = 关闭（纯文本描述，不注入结构化站位）
# ⚠️ 这是**软约束**：TE_3D_Director 节点本身不产图，scene_json 的价值是结构化、
#    跨镜一致；注入的锚点只增强构图，不改变 TASK/PRESERVE 等既有硬约束协议。
ENABLE_BLOCKING_ANNOTATION = _env_bool("ENABLE_BLOCKING_ANNOTATION", True)

# TE MAN 3D导演台「构图基准图」（2026-09-27）：在 ENABLE_BLOCKING_ANNOTATION 的文字
# 锚点之上，再**真渲染**一张 3D 站位/机位预演图，作为分镜生成的 <image1> 构图基准。
#   True  = 每镜先渲一张站位图 → 作 <image1> → 分镜提示词追加 COMPOSITION BASELINE 段
#           → 质检时一并送检该基准图，核对「人物数量/左右位置/前后层次/景别/机位角度」
#   False = 完全回退到现状（不出站位图、提示词无该段、质检不送基准图）
# ⚠️ 渲染是**增强项**：无浏览器 / 资产缺失 / 超时 / 全黑 → 自动降级为「只注入文字站位锚点」，
#    绝不阻断分镜主链路（见 app/te_3d_render.py 的失败静默降级口径）。
ENABLE_3D_BLOCKING_IMAGE = _env_bool("ENABLE_3D_BLOCKING_IMAGE", True)

# 多视角生成配置
#
# ⚠️ 实测结论（2026-09-23，8 组对照实验；详见 .workbuddy/memory/2026-09-23.md）
#     QwenImage2.1 的参考图编辑**只复刻参考图里已可见的机位**，不会凭空补全没见过的面。
#     用同一张「正面」基础图 + 明确指令（背面 / 俯视）去跑，输出仍然是正面：
#       · 换语言（中/英）、换语序（机位前置/后置）、换命令式与编辑式措辞 —— 无效；
#       · 断开 TextEncodeQwenImage21 的 vae（keep_vision 模式）—— 无效且身份漂移；
#       · 覆写 cfg —— 直接 RuntimeError（该节点正向带参考图视觉 token、负向不带，长度不等）；
#       · 跨对象对照：把参考图换成「俯视机位」的油纸伞，输出立刻跟着变俯视。
#     机制：KSampler cfg=1.0 → comfy/samplers.py 的 sampling_function 在 cfg≈1 时
#     `uncond_ = None`（负向根本不评估）→ 没有任何引导放大 → 参考图条件压过文字。
#     ★ 想要真正的多视角，必须在**基础图**阶段就带入目标角度，别指望多视角这一步变魔术；
#       也不要因为「多视角图看起来都一样」反复改下面的词（已试过 8 种，全部无效）。
#
#   下面各视角仍写全 label/zh/azimuth/elevation/distance（对更强的模型保留正确口径）；
#   zh 供 _build_multiview_prompt 生成中文机位句，azimuth 等英文词用于英文机位句，
#   **两者必须同口径**（历史 bug：top 的中文是「俯视」而 azimuth 却是 "front view"，自相矛盾）。
MULTIVIEW_CONFIG = {
    # 角色多视图：正面 / 左侧面 / 右侧面 / 背面（三视图）
    "character_views": [
        {"key": "front", "label": "正面全身",
         "zh": "相机正对人物、镜头平视，看到完整正面",
         "azimuth": "front view", "elevation": "eye-level", "distance": "full-body shot"},
        {"key": "left", "label": "左侧半侧面",
         "zh": "相机移到人物左侧约 45 度、镜头平视，同时看到左侧面与正面",
         "azimuth": "three-quarter view from the left",
         "elevation": "eye-level", "distance": "full-body shot"},
        {"key": "right", "label": "右侧半侧面",
         "zh": "相机移到人物右侧约 45 度、镜头平视，同时看到右侧面与正面",
         "azimuth": "three-quarter view from the right",
         "elevation": "eye-level", "distance": "full-body shot"},
        {"key": "back", "label": "背面全身",
         "zh": "相机转到人物正后方、镜头平视，只看到后脑、背部与后摆",
         "azimuth": "back view", "elevation": "eye-level", "distance": "full-body shot"},
    ],
    # 物品/场景 3D 多视角：正面 / 左45° / 右45° / 俯视（3D环绕）
    "item_scene_views": [
        {"key": "front", "label": "正面视角",
         "zh": "相机正对物件、镜头平视，看到完整正面",
         "azimuth": "front view", "elevation": "eye-level", "distance": "medium shot"},
        {"key": "left45", "label": "左前45°视角",
         "zh": "相机移到物件左前方约 45 度、镜头平视，同时看到左侧面与正面",
         "azimuth": "three-quarter view from the left",
         "elevation": "eye-level", "distance": "medium shot"},
        {"key": "right45", "label": "右前45°视角",
         "zh": "相机移到物件右前方约 45 度、镜头平视，同时看到右侧面与正面",
         "azimuth": "three-quarter view from the right",
         "elevation": "eye-level", "distance": "medium shot"},
        {"key": "top", "label": "俯视视角",
         "zh": "相机升到物件正上方、镜头垂直向下俯拍，画面以顶面为主，物件下方与地面不可见",
         # 历史 bug：这里曾是 "front view" —— 与中文「俯视」自相矛盾，已修正
         "azimuth": "top-down view, bird's eye view",
         "elevation": "high-angle shot", "distance": "wide shot"},
    ],
}

# ⚠️⚠️ 以下三个常量是「多视角」的**当前唯一在生产链路生效的口径**（2026-09-24 改造）
# ---------------------------------------------------------------------------
# 改造前：资产链路会为每个资产跑 `generate_multiview` 逐视角**重渲染**（4 次 GPU + 4 次质检）。
# 实测（output/assets/characters/逆天系统/*/）4 张「视角图」与 base.png **内容一致**
# —— 参考图编辑在 cfg=1.0 下只复刻参考图里已可见的机位（机制见上方 MULTIVIEW_CONFIG 注释），
# 产物零新增信息，且多视角不达标会把**整个资产判失败**（旧 `success = not blocked_views`）。
#
# 改造后（见 app/sheet_split.py 模块头）：
#   · 角色：基础图（三视图整图）出图后**本地列投影切分**出各视角单图，零 GPU 零质检；
#   · 物品 / 场景：基础图本身就是单主体图，**不再产出任何视角图**。
# `MULTIVIEW_CONFIG` 与 `comfyui_client.generate_multiview` 目前**无生产调用方**
# （仅守卫脚本引用），保留仅为回滚与历史对照；新增资产链路请勿再调用它。
# ---------------------------------------------------------------------------

#: 角色设定图内的格位顺序（**先上排从左到右、再下排**），即 sheet_split 切分产出的视角键。
#: ⚠️ 必须与 comfyui_client._ensure_fullbody_prompt 的版式描述**逐字同序**：
#:    上排「正面、左侧面、背面」三张全身视图；下排一格「正面半身（胸像）」。
#: 改这里必须同步改那边的提示词串，否则切出来的 front/left/back/half 会与实际格位错位。
#
# 2026-09-25（景别对档改造）：由「三张全身横排」改为「上排 3 全身 + 下排 1 半身」两层版式。
#   动机（详见 2026-09-25 工作日志「需求 H/I」）：
#     `<image1>` 是主画布，而近方形全身立绘 cover 到 9:16 竖屏要左右各裁一半 →
#     模型为保「全身」把人物缩小 → 系统性偏全景/远景，近景/特写被反向拉回。
#   业界通行做法即「按景别分档出图」（正脸特写/半身/全身），本改造补齐**半身档**。
CHARACTER_SHEET_LAYOUT_ZH = "上排正面、左侧面、背面三张全身视图横排，下排一格正面半身胸像"
CHARACTER_SHEET_VIEWS = ("front", "left", "back", "half")

#: 角色设定图的分层版式（行 × 列）。上排 3 格全身 + 下排 1 格半身。
#: ⚠️ 为什么半身放**独立的下排横带**而不是 2x2 网格的右下格：
#:    2x2 网格里半身的 y 区间与上排全身**重叠** → 行投影会把上下排粘成一段
#:    （实测合成图与真实图都如此），切分不可靠。让下排独占一条横带后，
#:    行投影稳定得到 2 段、行内列投影得到各排格数，两向都不碰内容。
CHARACTER_SHEET_GRID = (2, 3)   # (rows, cols)：2 行；上排 3 格，下排 1 格
#: 网格中各格位的占位（行, 列，0 起）：与 CHARACTER_SHEET_VIEWS 一一对应。
CHARACTER_SHEET_CELLS = ((0, 0), (0, 1), (0, 2), (1, 0))

#: 半身档所占的纵向比例（上排全身占 1 - 此值）。即下排横带的高度占比。
#: 用于切分时定位上下排边界（投影失败时的确定性兜底），并与提示词描述同口径。
CHARACTER_SHEET_HALF_BAND = 0.42

#: 「半身档」视角键集合 —— 近景/特写/中景镜头取它们作主画布（画幅与景别同向）。
CHARACTER_HALF_VIEWS = ("half",)

#: 角色/物品/场景资产目录里「派生视角图」的固定名白名单。
#: sheet_split.prune_stale_views 只在这个白名单内清理陈旧文件（不递归、不通配），
#: 用于清掉旧实现遗留的 right.png（右侧半侧面，新实现不再产出）。
#: 2026-09-25 增补 "half"（正面半身胸像，景别对档用）。
ASSET_VIEW_STEMS = ("front", "left", "right", "back", "half")

# ===================== 场景「按机位出图」（2026-09-29 恢复） =====================
# 需求：场景资产要能被分镜按**镜头机位**取图 —— 俯拍镜头给俯视档、斜侧镜头给斜侧档。
#
# ⚠️⚠️ 与上面 2026-09-24 废除的那条路**不是同一件事**，勿混、勿回滚成它：
#   · 已废除（`generate_multiview`）：基础图出好后，用**参考图编辑**逐视角重渲染。
#     机制上必然无效 —— 多视角模板 KSampler cfg=1.0 时 uncond 根本不评估
#     （comfy/samplers.py: cfg≈1 → uncond_=None），**没有任何引导放大**，参考图条件
#     压过文字，只复刻参考图里已可见的机位。实测 4 张产物与 base.png 内容一致，已用
#     8 组对照实验排除措辞/语言/语序/cfg 覆写等因素（见上方 MULTIVIEW_CONFIG 注释）。
#   · 本档（新）：在**基础图阶段就把机位写进正向提示词，逐档独立出图**。场景基础图跑的是
#     `scene_gen`（QwenImage2.1 **T2I**，无参考图槽位）→ 不存在「参考图机位先验」这个
#     压迫源，提示词里的「俯拍 / 左前 45°」真正生效。这正是上方那句实测结论
#     「真要拿到多视角，得在**基础图**阶段带入角度」的落地。
#
# 各档内容一致性：逐档出图是**独立采样**，跨档会各自长出细节差异，故统一
#   **同 seed**（沿用基础图那一颗）以最大化结构相关性；且分镜槽位话术已显式声明
#   场景参考图是「空间结构与光照锚点、不锁定机位」（见 app._allocate_storyboard_refs），
#   差异被限制在陈设细节层面，不会让分镜把场景换掉。
# 未命中档位时下游逐级回落 front → base（见 app._pick_scene_view），**绝不返回空**。

#: 场景机位档的键名。⚠️ 直接复用 `MULTIVIEW_CONFIG["item_scene_views"]` 的 key
#: （`front / left45 / right45 / top`）——**单一来源**，避免第三份名字副本漂移；
#: label 同样从那里取（`SCENE_VIEW_LABELS`），改那边即全链路同步。
SCENE_VIEW_KEYS = tuple(v["key"] for v in MULTIVIEW_CONFIG["item_scene_views"])

#: 场景机位档 → 中文标签（给日志、UI 与分镜槽位话术用）。
SCENE_VIEW_LABELS = {v["key"]: v["label"] for v in MULTIVIEW_CONFIG["item_scene_views"]}

#: 场景机位档 → **出图**机位句（追加进正向提示词）。
#: ⚠️ 与 `MULTIVIEW_CONFIG` 里的 `zh` 是**两种口径**，不可混用：
#:   · 那边的 `zh` 是「**编辑**指令」（相机绕物件变换位置后重新取景），配参考图编辑用；
#:   · 这里的机位句是「**出图**指令」（直接描述这张图画的是哪个机位），配 T2I 用。
#: 措辞必须显式给出**相机位置 + 可见面**，并声明「仍是同一个空间、陈设不变」——
#: 场景档之间是同一场地的四个机位，一旦被模型理解成四个不同场地，
#: 分镜换机位就等于换场景（比没有视角档更糟）。
SCENE_VIEW_ANGLE_ZH = {
    "front": "相机正对场景正面、镜头平视，看到场地的正面全貌",
    # ⚠️ 2026-10-03 强化（B 方案）：left45 / right45 的旧措辞「相机位于场景左/右前方约 45 度」
    # 是**纯角度**描述，实测与 front 取景几乎一样（≈base，见 SCENE_VIEW_DUP_PHASH_MAX 注释）。
    # 照搬**唯一被实证有效**的 top 档范式 —— **给结果（画面长什么样），而不只是给角度**：
    # 显式给出「透视消失点偏向哪侧 + 露出哪面侧墙与纵深」，并**排斥旧构图**
    # （「不要正对的正视对称构图」），逼模型真的换站位。front / top **逐字不动**
    # （top 是唯一被实证有效的档，动它就是引入回归）。
    "left45": "相机移到场地左前侧斜角、镜头沿纵深方向拍摄：透视消失点明显偏向画面右侧，"
              "画面左侧露出侧向墙面与纵深，能同时看到场地左侧面与正面，"
              "左右两侧呈现不同的墙面与纵深；不要正对的正视对称构图",
    "right45": "相机移到场地右前侧斜角、镜头沿纵深方向拍摄：透视消失点明显偏向画面左侧，"
               "画面右侧露出侧向墙面与纵深，能同时看到场地右侧面与正面，"
               "左右两侧呈现不同的墙面与纵深；不要正对的正视对称构图",
    "top": "相机升到场景正上方俯拍（鸟瞰机位），镜头垂直向下，"
           "画面以地面布局与陈设的顶面为主",
}

#: 镜头**机位**（`comfyui_client.camera_angle` 的解析结果）→ 场景机位档。
#: ⚠️ 映射的**单一来源**：分镜侧 `app._pick_scene_view` 只查这张表，不另写一套判据。
#: 未列出的机位（含未指定即空串）一律回落 front（正面档画幅对抗最小）。
#:   · 仰拍 → front：没有仰视档，正面档比俯视档接近得多（俯视与仰拍是**反向**机位，
#:     拿俯视图当仰拍镜头的锚点会把构图拽反 —— 这正是「误配比丢图更糟」的典型）；
#:   · 过肩 / 斜侧 → right45：两者都是「明显偏斜」的非正面机位，取斜侧档作静帧代表；
#:   · 环绕 → left45：环绕是**运镜**而非固定机位，静帧取左前 45° 作代表（与斜侧分档，
#:     避免所有非正面机位都挤到同一档、白拿一张同图）。
SCENE_ANGLE_TO_VIEW = {
    "俯拍": "top",
    "斜侧": "right45",
    "过肩": "right45",
    "环绕": "left45",
    "平视": "front",
    "仰拍": "front",
}

#: 场景逐机位出图总开关（默认开）。关掉后场景只出 base.png，分镜恒用正面档 ——
#: 与 2026-09-24~2026-09-29 之间的行为**逐字一致**（一键回滚只需改这一个值）。
SCENE_VIEWS_ENABLED = _env_bool("SCENE_VIEWS_ENABLED", True)

#: 单个场景机位档的**最大重试次数**（不含首轮）。
#: ⚠️ 与角色的「视角不达标 → 整个资产判 failed」旧口径**刻意不同**：
#:    机位档是**加值**而非必需（缺档时下游逐级回落 base.png，链路照跑），
#:    把加值项做成硬闸门曾让整批资产因一张俯视图全判失败（旧 success = not blocked_views）。
#:    故这里重试用尽仍不达标 → **丢弃该档**（不落盘、不阻断资产），只记日志。
SCENE_VIEW_MAX_RETRIES = max(0, _env_int("SCENE_VIEW_MAX_RETRIES", 1))

#: 场景机位档 vs base 的**相似度粗筛**上限（phash，0~100）。相似度 **> 该值** 即视为
#: 「几乎没换构图」（撞车）→ 丢弃该档（不落盘、不判 failed、不写 lesson，分镜回落正面档）。
#: ⚠️⚠️ **不能调到 80**（实测依据，勿「顺手调紧」）：真换了构图的 `top` 档 phash≈81.2、
#:   几乎没变的 `left45` 档 phash≈84.4 —— **80~95 区间不可分**；若用 80 做阈值会**误杀 top**
#:   （把唯一有效的档丢掉）。故本值只作「明显撞车」的**下限粗筛**：只拦 >95
#:   （如「对峙空地 right45」≈99.2 这类近重复），80~95 一律放行。
SCENE_VIEW_DUP_PHASH_MAX = 95.0

# ===================== 场景九宫格多视角（2026-10-05 用户指定） =====================
# 用户要求把场景多视角从「逐档独立出 4 张高清图（front/left45/right45/top）」改为
# 「一张 3×3 九宫格总图（9 个机位）作为场景资产本体，下游整图直接用」。
#
# 技术前提（务必先读，它决定了下面的出图方式）：场景出图是**纯 T2I**
# （``comfyui_client.generate_scene_base`` 跑 scene_gen 工作流，无参考图槽位）。
# 单张 prompt 让模型「一张图画 9 个机位」不可靠（``scene_grid.py`` 调研已证伪：
# 「qwenmultiangle/ComfyUI 官方均无可靠的单 prompt 九宫格，可靠做法是 9 次独立生成 +
# 拼接」）。故九宫格只能由 **9 次独立出图（逐机位）→ PIL 拼接成一张** 产出
# （拼接复用 ``scene_grid.stitch_grid``）。
#
# ⚠️⚠️ 代价（用户 2026-10-05 三轮确认知悉并选择「彻底关机位对档·纯整图」）：
#   · 下游（分镜/H3）拿到的场景参考图从「多张独立高清图」变成「一张九宫格总图」，
#     单格清晰度下降；
#   · **关闭** ``_pick_scene_view`` 的按机位对档（``RULES_A §A7`` 的核心价值）——
#     网格模式下所有镜头机位都喂整张九宫格，不再俯拍拿俯视图。
#   为可回滚，全部改动挂在 ``SCENE_GRID_MODE`` 开关下（**默认 True = 走九宫格**）；
#   关掉即回落到上面 4 档逐档独立出图 + 按机位选档的旧行为（旧常量 SCENE_VIEW_* 保留）。
SCENE_GRID_MODE = _env_bool("MJSCXT_SCENE_GRID", True)

#: ⭐ 2026-10-07（用户拍板）：场景九宫格改「**1 次 T2I 直出整图**」——
#:   把 9 个机位**逐格写死**进一句提示词（范式同 storyboard_grid_main），
#:   实测 57 秒出图且机位比「9 次独立出图 + 拼接」（8 分 16 秒）更准。
#:   关闭则回落到 9 档逐档独立出图 + stitch_grid 拼接的旧行为（旧路径原样保留）。
SCENE_GRID_ONESHOT = _env_bool("MJSCXT_SCENE_GRID_ONESHOT", True)

#: 九宫格各机位的键序（**即九宫格格序**，与 ``scene_grid.stitch_grid`` 的 3×3 排布一致）。
#: 单一来源：机位句与标签都从这里取（见 SCENE_GRID_ANGLE_ZH / SCENE_GRID_LABELS），
#: 出图循环与拼接循环共用同一序列，避免两份清单漂移。
#:
#: ⭐ 2026-10-06（用户指定）：按「场景 9 宫格多视角」新定义**重排为 9 档**，格序即用户
#:    给的画面编号 1~9：
#:      1 `front`    全景（主视角）—— 场景完整大环境 / 整体构图 / 建筑 / 空间布局 / 光影基调（基准参考）
#:      2 `wide`     远景 —— 拉远，展示场景与周边环境的关系、氛围透视
#:      3 `mid`      中景 —— 核心活动区域，主体陈设与空间的配比（漫剧最常用）
#:      4 `near`     近景 —— 局部环境：近处墙面、道具、陈设
#:      5 `detail_a` 特写细节 A —— 场景标志性物件（大门 / 招牌 / 特殊装饰）
#:      6 `detail_b` 特写细节 B —— 材质纹理（墙面肌理 / 地面 / 特殊纹路）
#:      7 `left45`   左 45° 侧视全景 —— 补全主视角看不到的空间结构
#:      8 `right45`  右 45° 侧视全景 —— 另一侧，空间对称 / 结构参考
#:      9 `top`      俯视鸟瞰 —— 场地平面布局，供后续分镜调度
#:    沿革：2026-10-05 曾用 8 档（front/left45/right45/top/wide/low/detail/depth，删掉了
#:    「back 背面反打」——封闭空间的背面反打在 T2I 下必出纯黑格）；本次按用户定义恢复 9 档
#:    并**换成上面这套语义**（`low`/`detail`/`depth` 三档被 `mid`/`near`/`detail_a`/`detail_b`
#:    取代，`top` 以「俯视鸟瞰」身份回到第 9 格）。
SCENE_GRID_VIEW_KEYS = ("front", "wide", "mid", "near", "detail_a",
                        "detail_b", "left45", "right45", "top")

#: 九宫格各机位 → **出图**机位句（追加进正向 T2I 提示词，决定这张格画哪个机位）。
#: 前 4 档中 front/left45/right45/top 直接**复用** SCENE_VIEW_ANGLE_ZH 的口径
#: （front/top 是实证有效措辞、left45/right45 是 2026-10-03 B 方案强措辞，逐字不动）；
#: 其余档取自「场景 9 宫格多视角」的用户定义。
#: ⚠️ 每条句**不得出现** ``CHARACTER_WORDS``/``CHARACTER_QTY_RE`` 能命中的词
#: （人物/人影/人群/士兵…），否则被 ``sanitize_scene_prompt`` 整句丢弃、机位静默失效
#: （与 SCENE_VIEW_ANGLE_ZH 同约束，改措辞前先跑 probe_scene_views.py）。
#: ⚠️ 用户定义里第 3/4 档原话带「人物」二字，这里**必须改写掉**（「人物」在
#: CHARACTER_WORDS 里，整句会被丢弃）—— 换成「核心活动区域 / 近处陈设」这类不含禁词的写法。
#: ⭐ 2026-10-06 二次打磨：新增的 5 档（wide/mid/near/detail_a/detail_b）按 A7 实证范式
#:   补上「**排斥主视角构图**」的尾巴 —— A7 定论是机位档失效的真根因＝**措辞只给位置、
#:   不给结果**（left45/right45 曾与 base 取景几乎一样，加了「透视消失点偏向哪侧 +
#:   不要正对的正视对称构图」才修好）。这 5 档都是**同一场地的不同景别**，最容易被模型
#:   顺手画成 base 那张正面全景，故每档都显式声明「不再是全貌 / 不要沿用主视角构图」。
#:   4 档复用项（front/left45/right45/top）**逐字不动**（不引入回归）。
SCENE_GRID_ANGLE_ZH = {
    "front": SCENE_VIEW_ANGLE_ZH["front"],
    "left45": SCENE_VIEW_ANGLE_ZH["left45"],
    "right45": SCENE_VIEW_ANGLE_ZH["right45"],
    "top": SCENE_VIEW_ANGLE_ZH["top"],
    "wide": "拉远到大远景机位：整个场地与四周的周边环境一并入画，交代场地与外界的关系；"
            "画面范围明显比主视角更开阔，四周环境占画面大半，"
            "不要与主视角相同的取景范围",
    "mid": "中景机位：镜头只截取场地的一段核心区域，交代这一片的尺度、"
           "主要陈设与地面通道的配比，是漫剧最常用的取景距离；"
           "画面不再是整个场地的全貌，不要沿用主视角的正面全景构图",
    "near": "近景机位：镜头贴近场地局部，只拍到近处的墙面、道具与陈设细节，"
            "背景只保留少量环境信息；画面里看不到场地的整体轮廓，不要出现全景",
    "detail_a": "特写机位：镜头抵近场地中最有标志性的物件"
                "（大门、招牌、匾额或特殊装饰），该物件占满画面绝大部分、"
                "背景浅景深虚化；不要拍到场地全貌",
    "detail_b": "特写机位：镜头抵近拍摄场地的材质与纹理"
                "（墙面肌理、地面铺装、砖缝或特殊纹路），纹理细节占满画面、"
                "背景浅景深虚化；不要拍到场地全貌",
}

#: 九宫格各机位 → 中文标签（供日志/UI/机位句前缀用）。
SCENE_GRID_LABELS = {
    "front": "全景（主视角）", "wide": "远景", "mid": "中景", "near": "近景",
    "detail_a": "特写细节A", "detail_b": "特写细节B",
    "left45": "左45°侧视全景", "right45": "右45°侧视全景", "top": "俯视鸟瞰",
}

#: 九宫格第 1 格（``front``）在**磁盘上**的实际来源文件名。
#: ⚠️ 场景资产不单独产出 ``front.png``（正面档直接复用已过质检的基础图，
#: 见 app.py 资产 worker 的 scene 分支与 `_build_asset_index` 的「front 别名到 base」）——
#: 所以拼接时必须按本表把 ``front`` 解析到 ``base.png``，否则第一格会被当成「文件不存在」
#: 跳过，全景点就不在九宫格里了（2026-10-06 修复：当时 8 档下实际只有 7 格入图，
#: 3×3 里空着 2 格纯黑）。
SCENE_GRID_CELL_FILE_STEM = {"front": "base"}

#: ⭐⭐ 九宫格拼接的两条**硬契约**（2026-10-06 两处修复的结论，动拼接前先读）：
#:   ① **按格序定长拼接**：槽位表长度恒 = 9，缺档填 ``None`` → 该格留空、
#:      **后续格位不左移**（``scene_grid.stitch_grid(..., slot_count=9)``）。
#:      否则中间缺一档会让它之后的机位**整体错位一格**（第 5 格＝特写细节A 变成别的
#:      画面，比空黑格更难发现）。调用方构造槽位时**不得**「过滤后 append」。
#:   ② **网格模式下不因「与 base 相似」丢档**：9 格里每一格都是用户指定的画面，
#:      相似度只作**软告警**（提示该机位可能没真换构图 → 回去改机位措辞），绝不丢档 ——
#:      丢档就是九宫格空黑格。旧 4 档模式的 phash 粗筛（``SCENE_VIEW_DUP_PHASH_MAX``）
#:      在网格模式下**不生效**（其原始前提在网格模式已由「整图直接用」取代）。
#: 九宫格主图的文件名（落 ``<场景目录>/grid.png``；作为 SCENE_GRID_MODE 下的场景主图，
#: 资产索引 ``_build_asset_index`` 与下游 ``_pick_scene_view`` 都认它）。
SCENE_GRID_FILENAME = "grid.png"

#: 九宫格主图 meta 的 derive_mode 标注（``_write_artifact_meta`` 的 extra），
#: 表明它是「逐机位独立出图 + PIL 拼接」而来（区别于 base.png 的单张 T2I）。
SCENE_GRID_DERIVE_MODE = "grid_stitch"

# ===================== H3 公共参数「相关说明」提示词（2026-10-05） =====================
# H3 Director 公共区（global.refs + global.refAudios + commonEnabled）此前只登记参考图/
# 音频，提示词说明是**写死的通用模板**（"环境参考…/三视图设定图…"），本集专属的
# `appearance` 设定没并进去、公共音色音频也没有一句用途说明、场景说明还是旧"正面档"
# 口径（与场景九宫格 SCENE_GRID_MODE 脱节）。本开关打开后，comfyui_client._h3_picture_defs
# 的公共块做三处增强：① 公共角色/物品/场景说明并入该资产的 `appearance` 设定文案；
# ② 公共场景说明改九宫格多视角口径（"9 机位总览，同一场地的各观察角度都在这张图里"）；
# ③ 公共音色参考音频补一句 `<Audio N>` 用途说明（驱动该角色声音）。
# ⭐ 只改说明文本、不动 <Picture N>/<Audio N> 编号与槽位顺序（零错位风险）；关掉回落到
# 旧的固定模板说明（回滚点 = 置 False）。
H3_COMMON_NOTE_MODE = _env_bool("MJSCXT_H3_COMMON_NOTE", True)

# ===================== 正负提示词冲突清理（P0：风格冲突修复） =====================
# 物品生成.json / 分镜生成.json 的负向词表含「3D渲染、二次元动漫」，场景生成.json 含「3D」，
# 而正向提示词要求「国漫3D渲染风格」——正负自相矛盾会把 3D 风格压掉（画面风格撕裂）。
# 提交前从负向槽位剔除这些词（按长度降序匹配，避免「3D渲染」被「3D」提前截断）。
CONFLICT_NEGATIVE_TOKENS = ("3D渲染", "二次元动漫", "3D动漫", "3D渲染风格", "3D")

# ===================== 生成前提示词 LLM 增强 + 质检模型复审（2026-09-30） =====================
# 在 prompt_qc.preflight（确定性预检）之外补的两道模型侧闸门，对所有已接预检的生成
# 路径生效（分镜图 / 资产图 / 尾帧 / H3 视频；台词 audio 永不参与——台词会被 TTS
# 逐字念出，绝不能改写）。任何一层失败都 fail-open（按原提示词继续生成），绝不阻断管线。
# 2026-09-30 晚曾因旧网关持续超时临时关闭；当晚用户更换 LLM 提供商后已恢复默认开启。
#: 出图/出片前用「文本分析模型」增强提示词（动作/空间/光影更具体；协议骨架强制保留）
PROMPT_ENHANCE_ENABLED = _env_bool("MJSCXT_PROMPT_ENHANCE", True)
#: 增强后用「质检模型」对提示词做语义复审；复审不通过且给出改进版、改进版复检不降分才采纳
PROMPT_MODEL_REVIEW_ENABLED = _env_bool("MJSCXT_PROMPT_MODEL_REVIEW", True)
#: 单次增强/复审调用的超时（秒）。预检在生成链路里同步执行，超时给得保守，失败即回落原文
PROMPT_ENHANCE_TIMEOUT_SEC = _env_int("PROMPT_ENHANCE_TIMEOUT_SEC", 90)
#: 增强/复审结果的进程内缓存条数（同一条提示词的生成重试/质检重试不再重复打模型）
PROMPT_ENHANCE_CACHE_SIZE = max(0, _env_int("PROMPT_ENHANCE_CACHE_SIZE", 256))

# 提示词增强的**文件级总开关**（2026-09-30，前端「AI 配置」页可改，优先级：文件 > env > 代码默认）。
# 文件缺失 = 跟随上方 env/默认值；文件存在则以其 enabled 为准 —— 这是给非运维用户的开关。
PROMPT_ENHANCE_CONFIG_PATH = os.path.join(PROJECT_DATA_DIR, "prompt_enhance_config.json")
PROMPT_ENHANCE_FILE_DEFAULTS = {"enhance_enabled": None, "review_enabled": None}


def _prompt_enhance_file_flags() -> dict:
    """读 prompt_enhance_config.json；缺失/损坏返回 {}（此时跟随 env 默认值）。"""
    raw = {}
    if os.path.isfile(PROMPT_ENHANCE_CONFIG_PATH):
        try:
            with open(PROMPT_ENHANCE_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("提示词增强配置读取失败（跟随代码默认）：%s", e)
            raw = {}
    out = dict(PROMPT_ENHANCE_FILE_DEFAULTS)
    for k in ("enhance_enabled", "review_enabled"):
        v = raw.get(k)
        out[k] = (None if v is None else bool(v))
    return out


def save_prompt_enhance_config(patch: dict) -> dict:
    """保存提示词增强开关（只认 enhance_enabled / review_enabled 两个布尔字段）。"""
    cfg = _prompt_enhance_file_flags()
    for k in ("enhance_enabled", "review_enabled"):
        if k in patch and patch.get(k) is not None:
            cfg[k] = bool(patch.get(k))
    import datetime as _dt
    cfg["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(PROMPT_ENHANCE_CONFIG_PATH)), exist_ok=True)
    with open(PROMPT_ENHANCE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg

DEFAULT_PARAMS = {
    "resolution": "768p_vertical",  # 768p_vertical, 768p_horizontal, 480p
    "steps": 8,  # Turbo LoRA 默认 8 步
    "seed": -1,  # -1 表示随机
    "reference_strength": 0.9,  # 参考强度
    "fps": 24,
    "duration_per_shot": 5,  # 每个镜头默认 5 秒
}

# 创建必要的目录
def ensure_dirs():
    for d in [PROJECT_OUTPUT_DIR, SCRIPT_DIR, ASSETS_DIR, CHARACTERS_DIR,
              ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR, VIDEOS_DIR, FINAL_DIR, NOVELS_DIR, QC_DIR,
              AI_CHAT_DIR, UPSCALE_DIR, DUB_DIR, DUB_MIX_DIR, PROJECTS_DIR, PROJECT_TRASH_DIR,
              CONTINUITY_DIR]:
        try:
            os.makedirs(d, exist_ok=True)
        except PermissionError as e:
            # ⭐ 2026-10-05：桌面版嵌入式 Python 运行时 PROJECT_DATA_DIR 可能是
            #   尚未创建的深层路径（%APPDATA%\mjscxt-desktop\mjscxt-data），
            #   旧代码直接 makedirs 抛 PermissionError → 模块导入失败 → 进程崩溃。
            #   现在捕获并降级（记录警告，不阻断启动），后续代码若因目录缺失出错，
            #   各路径已在模块顶部定义，不会二次崩溃。
            import logging
            logging.getLogger(__name__).warning(
                "无法创建数据目录 %s（权限不足），该目录相关功能将不可用：%s", d, e)
ensure_dirs()
