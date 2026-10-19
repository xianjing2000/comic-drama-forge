"""
剧本生成器 - 使用云端 LLM 生成结构化分镜脚本
图片资产分为三大类：角色（多视图）、物品（3D多视角）、场景（3D多视角）
"""
import json
import os
import logging
import uuid
from typing import List, Dict, Optional
from datetime import datetime

import fs_atomic

from config import (ANTHROPIC_API_KEY, LLM_PROVIDER, QC_CONFIG_PATH, SCRIPT_DIR,
                    PROJECT_OUTPUT_DIR)

# ⭐ 2026-10-10：原先这里自算 _PROJECT_ROOT（= 代码目录）并据此读 qc_config.json，
#   而真正的 qc_config.json 在数据根（config.QC_CONFIG_PATH）→ 设置 MJSCXT_DATA_DIR
#   后必然读不到。统一走 config，删掉这个第二事实源。

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Claude 模型（可用环境变量覆盖）
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
# 系统提示词：Anthropic Messages API 要求 system 必须是顶层参数，不能放进 messages
SYSTEM_PROMPT = "你是一个专业的AI漫剧编剧，擅长生成结构化的分镜脚本。你输出的JSON必须合法，不包含markdown代码块标记。"
# 免 Key 兜底剧本（按顺序探测）
FALLBACK_SCRIPT_NAMES = ["剑心初醒_兼容版.json", "测试_剑心初醒_兼容版.json"]


class ScriptGenerator:
    """剧本生成器"""

    def __init__(self, api_key: str = ANTHROPIC_API_KEY, provider: str = LLM_PROVIDER):
        self.provider = provider
        self.api_key = api_key
        self.client = None

        if provider == "anthropic":
            if not api_key:
                logger.warning("ANTHROPIC_API_KEY 未设置，剧本生成将在调用时报错")
            else:
                import anthropic  # 延迟导入
                self.client = anthropic.Anthropic(api_key=api_key)

    def generate_script(self,
                       theme: str,
                       episodes: int = 1,
                       duration_per_episode: int = 60,
                       style: str = "古风仙侠",
                       target_audience: str = "年轻观众",
                       lessons_hint: str = "") -> Dict:
        """生成完整剧本；云端失败或无 Key 时自动回退到本地兜底剧本

        lessons_hint：历史质检教训文案（可选，空串 → 零行为变更），由重试链路注入，
        追加到生成提示词末尾，供模型规避已发生过的缺陷。
        """

        if self.provider != "anthropic":
            raise ValueError(f"不支持的 LLM 提供商: {self.provider}")

        if self.client is None:
            logger.warning("ANTHROPIC_API_KEY 未设置，改用本地兜底剧本")
            fallback = self.load_fallback_script()
            if fallback:
                return fallback
            raise RuntimeError("ANTHROPIC_API_KEY 未设置，且未找到本地兜底剧本，无法生成剧本")

        try:
            return self._generate_with_claude(theme, episodes, duration_per_episode,
                                              style, target_audience, lessons_hint)
        except Exception as e:
            logger.error(f"Claude 生成剧本失败: {e}")
            fallback = self.load_fallback_script()
            if fallback:
                logger.warning("已回退到本地兜底剧本（免 Key 演示模式）")
                fallback.setdefault("metadata", {})["fallback_reason"] = str(e)
                return fallback
            raise

    def load_fallback_script(self) -> Optional[Dict]:
        """加载本地兜底剧本（A 版 Schema，含 shots[] / prompt_h3）"""
        if not os.path.isdir(SCRIPT_DIR):
            return None
        for name in FALLBACK_SCRIPT_NAMES:
            path = os.path.join(SCRIPT_DIR, name)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    script = json.load(f)
                logger.info(f"已加载兜底剧本: {path}")
                return script
        # 兜底：目录中任意含 shots 的剧本
        for fn in sorted(os.listdir(SCRIPT_DIR)):
            if not fn.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(SCRIPT_DIR, fn), "r", encoding="utf-8") as f:
                    script = json.load(f)
                if script.get("shots"):
                    logger.info(f"已加载兜底剧本: {fn}")
                    return script
            except Exception:
                continue
        logger.warning("未找到可用的本地兜底剧本")
        return None

    # ===== 风格专属提示词分支（按大类覆盖全部61种风格） =====
    _STYLE_SPECIFIC_PROMPTS = {
        # ========== 2D 古风国漫类 ==========
        "2d_gufeng": """
【2D古风国漫风格专属要求】
- 景别分布：全景（宗门/山水场景）约20%，中景（人物对话/对峙）约50%，特写（情绪爆发/打脸瞬间）约30%
- 运镜节奏：以固定镜头+轻推为主，少用手持；打脸/觉醒瞬间可用慢镜头（轻推+定格）
- 角色描述：必须写清发型制式（束发/披发/冠巾/发带）、服饰层次（里衣/外袍/腰带/玉佩）、材质质感
- 分镜节奏：前3镜铺垫世界观与主角处境，中间4-5镜冲突升级（被羞辱/被压制），最后2-3镜打脸高潮+悬念收尾
- 光影：自然光为主，日出/黄昏/月光作为情绪节点；打脸高潮时可用逆光剪影
- 爽点设计：至少安排1次明确的"打脸"镜头（反派表情从傲慢→震惊→恐惧的三连变化）
""",
        # ========== 2D 现代都市类 ==========
        "2d_urban": """
【2D现代都市风格专属要求】
- 景别分布：近景/特写为主（约60%），中景为辅（约30%），全景（街头/办公室）约10%
- 运镜节奏：手持感更强，快切；对话戏用正反打（over-the-shoulder）
- 角色描述：必须写清穿搭风格（通勤装/运动装/西装）、发型细节、配饰（手表/耳机/包）
- 分镜节奏：开门见山，第1镜直接进入冲突；3镜内完成"被欺负→反击"的完整小闭环
- 光影：室内暖光/办公室冷白光/街头霓虹夜景交替；情绪压抑时用冷色调，反击时切暖光
- 爽点设计：打脸要快，1-2镜内完成；多用怼脸特写拍反派表情崩溃
""",
        # ========== 2D 黑白漫画类 ==========
        "2d_bw_comic": """
【2D黑白漫画风格专属要求】
- 景别分布：大特写+全景交替对比（约40%大特写，30%全景），中景为辅
- 分镜框感：要有"漫画格"意识——压抑的格子小而挤，爆发的格子大而满
- 角色描述：重点写线条感、阴影块面、发丝走向；黑白对比要强
- 分镜节奏：快慢对比强烈——铺垫用多个小快切，爆点用一个大格慢镜
- 光影：高对比度，大量黑色块和留白；不用柔和过渡
- 爽点设计：打脸瞬间用"满格大特写"——反派脸占满整个画面，配合汗滴/震惊线条
""",
        # ========== 2D 韩漫类 ==========
        "2d_korean": """
【2D韩漫风格专属要求】
- 景别分布：近景怼脸为主（约50%），中景为辅（约40%），全景少（约10%）
- 运镜：慢推+柔焦，情绪流为主；男主出场必给脸部特写
- 角色描述：五官精致度优先——眼型、唇形、下颌线、发丝光泽必须写细
- 分镜节奏：细腻情绪流，对话节奏慢；每集至少1个"男主眼神特写"作为名场面
- 光影：柔焦打光，韩剧式侧逆光；肤色通透感
- 爽点设计：打脸后给一个"男主侧脸+眼神变冷"的慢镜头，让观众消化爽感
""",
        # ========== 2D 赛博朋克类 ==========
        "2d_cyberpunk": """
【2D赛博朋克风格专属要求】
- 景别分布：中景+特写为主，荷兰角（倾斜构图）常用
- 运镜：快切+跟随，信息密度高；霓虹反射要写进光影
- 角色描述：机械义体、霓虹反光、服装材质（皮革/化纤/发光条）必须写清
- 分镜节奏：快节奏，每镜信息量大；多用反射/屏幕作为转场
- 光影：紫/粉/蓝霓虹主色，雨夜反射；明暗对比强烈
- 爽点设计：觉醒/变身时给霓虹闪烁+义体展开的特写
""",
        # ========== 2D 日漫类 ==========
        "2d_anime": """
【2D日式动画风格专属要求】
- 景别分布：中景为主（约45%），近景特写（约35%），全景（约20%）
- 运镜：动感强，常用快速推拉和摇镜；战斗场面用动态线和速度线
- 角色描述：大眼睛、夸张表情、发丝飞扬感；校服/战斗服细节要写
- 分镜节奏：节奏明快，每3-5镜一个小高潮；情绪爆发时用静止帧+呐喊
- 光影：赛璐璐风格，平涂+硬边阴影；情绪高潮时背景放光效
- 爽点设计：变身/觉醒时给全身发光+发丝飞扬的大特写，配合呐喊音效
""",
        # ========== 2D 美漫类 ==========
        "2d_american_comic": """
【2D美漫风格专属要求】
- 景别分布：大特写+全景交替（约45%大特写，25%全景），构图硬朗
- 运镜：力量感强，常用倾斜构图和冲击性镜头
- 角色描述：肌肉线条明显、下颌线锋利、服装夸张；色块对比强烈
- 分镜节奏：节奏有力，每镜都有冲击感；爆点用整页大格
- 光影：硬边阴影，色块分明，有印刷网点质感
- 爽点设计：一拳打飞用"冲击线+速度线"满格，反派飞出画面外
""",
        # ========== 2D 水彩/水墨类 ==========
        "2d_ink_wash": """
【2D水墨国风风格专属要求】
- 景别分布：远景/大远景为主（约35%），中景为辅（约45%），特写少（约20%）
- 运镜：舒缓、留白感强；多用固定镜头和缓慢横移
- 角色描述：重点写衣袂飘飘感、水墨晕染质感；线条要写意
- 分镜节奏：节奏舒缓，重意境而非快节奏；情绪靠空镜和留白表达
- 光影：水墨晕染、云雾缭绕；不用硬光，用墨色浓淡表现层次
- 爽点设计：爽点用"动静对比"——前面慢镜铺垫，突然一个快速动作，然后又回到水墨留白
""",
        # ========== 3D 写实CG仙侠类 ==========
        "3d_xianxia_cg": """
【3D写实CG仙侠风格专属要求】
- 景别分布：全景（仙山/宗门大场景）约25%，中景（人物对话）约45%，特写（情绪/功法爆发）约30%
- 运镜：电影感强，多用环绕镜头和推拉；功法释放时用慢镜头+粒子特效
- 角色描述：必须写清五官精致度、皮肤质感、服装材质（丝绸/锦缎/轻纱）、发丝物理效果
- 分镜节奏：电影化叙事，前3镜建立世界观，中间冲突升级，高潮用大场面特效，收尾留悬念
- 光影：电影级打光，体积光、丁达尔效应；功法爆发时用金色/蓝色能量光效
- 爽点设计：觉醒/突破时给全身能量爆发特写，衣袍翻飞+头发飘起+粒子特效
""",
        # ========== 3D 游戏渲染类 ==========
        "3d_game_render": """
【3D游戏渲染风格专属要求】
- 景别分布：第三人称视角为主，中景+近景（约70%），全景（约20%），特写（约10%）
- 运镜：跟随镜头为主，战斗时用动态运镜和视角切换
- 角色描述：游戏建模质感，次世代渲染；服装细节、武器质感、材质反射要写
- 分镜节奏：游戏化节奏，操作感强；每镜都有明确的"动作-反馈"
- 光影：PBR材质，真实光影，环境反射；暗部有环境光遮蔽
- 爽点设计：击杀/胜利时给慢动作特写+暴击数字弹出感
""",
        # ========== 3D 卡通迪士尼类 ==========
        "3d_cartoon": """
【3D卡通渲染风格专属要求】
- 景别分布：中景+近景为主（约65%），表情特写多（约25%），全景少
- 运镜：活泼轻快，常用夸张的推拉和摇镜
- 角色描述：Q版/卡通比例，大眼睛、夸张表情；材质柔和圆润
- 分镜节奏：轻松明快，喜剧感强；情绪表达夸张
- 光影：柔和全局光，阴影边缘模糊；色彩鲜艳饱和
- 爽点设计：打脸后给反派"眼冒金星+转圈"的卡通化夸张表现
""",
        # ========== 真人文艺/电影类 ==========
        "live_action_cinema": """
【真人影视风格专属要求】
- 景别分布：电影感构图，中近景为主（约50%），特写（约25%），全景（约25%）
- 运镜：电影级运镜，稳定器+轨道；情绪戏用手持微晃增加真实感
- 角色描述：演员真实感，皮肤纹理、妆容细节、穿搭质感要写
- 分镜节奏：电影化叙事，重情绪和氛围；对话戏用正反打和过肩镜头
- 光影：电影级打光，自然光为主；逆光、侧光制造氛围
- 爽点设计：情绪爆发时给演员面部特写，微表情变化要写细
""",
        # ========== 真人古装影视类 ==========
        "live_action_period": """
【真人古装影视风格专属要求】
- 景别分布：全景（宫廷/古建筑）约20%，中景（对话）约50%，特写（情绪）约30%
- 运镜：庄重感，多用对称构图和缓慢推拉
- 角色描述：古装形制准确，发型妆造细节；服装材质（丝绸/锦缎/刺绣）
- 分镜节奏：正剧节奏，重礼仪和氛围；宫廷戏对称构图，武打戏利落
- 光影：烛光/宫灯暖光，自然光；宫廷戏明暗对比强
- 爽点设计：打脸/反转时给反派表情特写，从得意到惊慌的变化
""",
    }

    @classmethod
    def _get_style_specific_prompt(cls, style: str) -> str:
        """根据风格关键词匹配对应的专属提示词。匹配不到就返回空串。"""
        s = style.lower()

        # ===== 2D 古风国漫类 =====
        if any(k in s for k in ["古风", "仙侠", "武侠", "国风", "神话", "水墨", "guofeng", "xianxia", "chinese_mythology", "ink_guofeng", "shanghai_studio", "guochao"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_gufeng"]

        # ===== 2D 现代都市类 =====
        if any(k in s for k in ["都市", "现代", "职场", "校园", "urban", "romance", "90s_anime", "cel_anime", "anime"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_urban"]

        # ===== 2D 黑白漫画类 =====
        if any(k in s for k in ["黑白", "漫画", "条漫", "bw", "comic", "ink", "sketch", "stick_figure", "crayon", "tezuka"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_bw_comic"]

        # ===== 2D 韩漫类 =====
        if any(k in s for k in ["韩漫", "韩式", "korean", "webtoon"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_korean"]

        # ===== 2D 赛博朋克类 =====
        if any(k in s for k in ["赛博", "霓虹", "科幻", "cyberpunk", "retro_futurism", "post_apocalyptic"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_cyberpunk"]

        # ===== 2D 日漫类 =====
        if any(k in s for k in ["日漫", "日系", "日式", "宫崎骏", "miyazaki", "otomo", "pixel", "shadow_play"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_anime"]

        # ===== 2D 美漫类 =====
        if any(k in s for k in ["美漫", "美式", "american", "retro_american", "american_comic"]):
            return cls._STYLE_SPECIFIC_PROMPTS["2d_american_comic"]

        # ===== 3D 写实CG仙侠类 =====
        if any(k in s for k in ["3d", "cg", "写实", "次世代", "ue5", "game_render", "aaa_concept", "dark_fantasy", "luxury_fantasy", "xianxia_cg", "guofeng_cg", "ue5_urban"]):
            return cls._STYLE_SPECIFIC_PROMPTS["3d_xianxia_cg"]

        # ===== 3D 卡通类 =====
        if any(k in s for k in ["disney", "claymation", "clay", "fruit_person", "卡通", "Q版"]):
            return cls._STYLE_SPECIFIC_PROMPTS["3d_cartoon"]

        # ===== 真人古装类 =====
        if any(k in s for k in ["古装", "宫廷", "古代", "ancient", "court", "costume", "xianxia_real"]):
            return cls._STYLE_SPECIFIC_PROMPTS["live_action_period"]

        # ===== 真人影视类（兜底） =====
        if any(k in s for k in ["真人", "影视", "电影", "real", "photography", "korean_drama", "hongkong", "war", "horror", "suspense", "wilderness", "russian", "quentin", "hollywood", "high_key", "blue_orange", "koreeda", "60s", "80s", "90s", "upturn", "vintage"]):
            return cls._STYLE_SPECIFIC_PROMPTS["live_action_cinema"]

        return ""

    @staticmethod
    def _build_script_prompt(theme: str, episodes: int, duration: int, style: str,
                             audience: str, lessons_hint: str = "") -> str:
        """拼装剧本生成提示词（Claude 与 OpenAI 兼容两条链路共用同一份 prompt）。

        lessons_hint：历史质检教训，追加在 JSON 格式说明**之后**、结尾（不破坏 JSON
        输出格式要求）；空串 → 零行为变更。
        """
        style_prompt = ScriptGenerator._get_style_specific_prompt(style)

        prompt = f"""你是一个专业的AI漫剧编剧，擅长用分镜讲故事。请为以下主题生成详细的分镜脚本。

主题：{theme}
风格：{style}
目标观众：{audience}
集数：{episodes}集
每集约{duration}秒

【先做情绪曲线设计，再写分镜】
在写分镜之前，先在脑子里完成以下三步：
1. 情绪曲线：这一集的情绪是怎么走的？（压抑→愤怒→爆发→爽快？还是 平静→悬念→惊吓？）
2. 爽点/爆点：这一集的核心爽点是什么？放在第几个分镜？前面怎么铺垫？
3. 人物弧光：主角从开场到结束，状态发生了什么变化？

请严格按照以下JSON格式输出（不要添加任何其他内容）：

{{
  "title": "剧集标题",
  "theme": "{theme}",
  "style": "{style}",
  "emotion_curve": {{
    "arc_type": "情绪曲线类型（如：压抑→觉醒→反杀→悬念 / 平静→冲突→高潮→收尾）",
    "beat_points": [
      {{"shot_range": "1-3", "phase": "铺垫/压抑", "target_emotion": "观众应该感到什么"}},
      {{"shot_range": "4-6", "phase": "冲突升级", "target_emotion": "观众应该感到什么"}},
      {{"shot_range": "7-9", "phase": "爆发/爽点", "target_emotion": "观众应该感到什么"}},
      {{"shot_range": "10-12", "phase": "收尾/悬念", "target_emotion": "观众应该感到什么"}}
    ],
    "core_payoff": "本集核心爽点/爆点是什么（一句话）",
    "hook_end": "章末钩子/悬念是什么"
  }},
  "characters": [
    {{
      "name": "角色名",
      "age": "年龄描述",
      "appearance": "外貌描述：发型、五官、体型、服装、配饰（用于生成角色参考图，需详细具体）",
      "key_visual_tags": ["3-5个最核心的视觉识别标签，每镜都要重复（如：黑色束发/白色长袍/腰佩玉佩）"],
      "personality": "性格特点",
      "voice_style": "声音特点",
      "reference_prompt_zh": "中文角色图生成提示词（含：视角、姿态、表情、服装细节、画风）",
      "reference_prompt_en": "English character image generation prompt"
    }}
  ],
  "items": [
    {{
      "name": "物品名",
      "category": "武器/法宝/道具/服饰",
      "appearance": "物品外观描述：形状、材质、颜色、纹理、尺寸",
      "owner": "所属角色（可为空）",
      "reference_prompt_zh": "中文物品图生成提示词。只写物品本体：造型/材质/颜色/纹样/尺寸感，外加 3D渲染、白底展示、材质细节；严禁写容器、托盘、盒子、底座、承托物、摆放位置、所处环境、光照氛围、人物或手。原因：这些属于分镜内容，写进参考图会让它变成「物品在某容器里」，并被当成该物品的规范外观带进后面每一镜。",
      "reference_prompt_en": "English item image generation prompt"
    }}
  ],
  "scenes": [
    {{
      "name": "场景名",
      "location": "地点类型：室内/室外/山川/建筑",
      "appearance": "场景外观描述：环境布局、建筑风格、光照氛围、季节天气",
      "reference_prompt_zh": "中文场景图生成提示词（含：构图、透视、光影、氛围）",
      "reference_prompt_en": "English scene image generation prompt"
    }}
  ],
  "shots": [
    {{
      "shot_id": 1,
      "episode": 1,
      "duration": 5,
      "camera": "景别+运镜复合词（如「中景轻推」「特写固定」；景别只允许：大特写/特写/近景/中近景/局部/中景/全景/远景/大远景）",
      "shot_type": "景别（与 camera 中的景别部分一致，单列一字段便于下游精确读取）",
      "camera_motion": "运镜（只允许克制词表：固定/轻推/轻摇/跟随/轻手持）",
      "location": "场景名（对应scenes中的name）",
      "description": "画面描述（详细，50字以内）。必须包含主角的核心视觉标签（key_visual_tags里的内容），确保每镜角色一致",
      "visual_detail": "扩展画面细节（时间/天气/光源方向/动作过程补全；没有则空串）",
      "dialogue": "角色台词",
      "emotion": "情绪（与emotion_curve对应阶段一致）",
      "beat": "叙事节拍（触发/铺垫/对峙/爆发/收尾/打脸 等）",
      "edit_reason": "这一镜为什么切（剪辑动机，20~30字）",
      "scene_lighting": "本镜光影（光源方向/色温/氛围）",
      "audio_cues": "音效/音乐提示",
      "prompt_h3": "H3视频生成提示词（英文，包含角色动作、镜头运动、环境描述，必须包含主角核心视觉标签）",
      "characters_in_shot": ["角色名列表"],
      "items_in_shot": ["物品名列表"],
      "blocking": [{{"name": "角色名", "x": "left/center/right", "depth": "front/mid/back", "facing": "camera/left/right/back"}}],
      "action": "本镜动作beat（谁做了什么、动作从哪到哪；纯对话镜也写神态小动作，不得留空）"
    }}
  ],
  "production_notes": {{
    "total_shots": 12,
    "estimated_duration": 60,
    "style_guide": "整体风格说明",
    "emotion_curve_summary": "整集情绪节奏一句话总结"
  }}
}}

【通用硬性要求】
1. characters：每个角色要有详细的外貌描述 + 3-5个核心视觉标签（key_visual_tags），这些标签在每一分镜的description和prompt_h3里都要重复出现，确保角色跨镜一致
2. items：提取剧情中出现的重要物品（武器、法宝、关键道具），通常2-5个
3. scenes：提取剧情涉及的主要场景，通常3-6个
4. shots：分镜数量根据剧情需要自然决定，不设下限；每镜默认10秒，最后一镜如果内容不足可以灵活缩短，不用硬凑10秒；标注出场角色和物品
5. 一镜一动作：每镜只承载一个动作节拍（action 字段），连续动作拆成相邻两镜
6. 情绪曲线必须落地：每镜的emotion字段必须与emotion_curve对应阶段一致，不能前后矛盾
7. 爽点必须给够：核心打脸/觉醒/反转镜头，必须用特写或大特写，配慢推运镜
8. 输出必须是合法的JSON格式，不要包含任何注释
{style_prompt}"""

        if lessons_hint:
            prompt += f"\n\n【历史质检教训，务必规避（不要照抄进正文）】\n{lessons_hint}"
        return prompt

    def _generate_with_claude(self, theme: str, episodes: int,
                               duration: int, style: str, audience: str,
                               lessons_hint: str = "") -> Dict:
        """使用 Claude API（Anthropic SDK）生成剧本。仅供 CLI / 独立链路使用，
        Web 端主流程已改走 OpenAI 兼容的「文本分析模型」（见 generate_script_with_client）。"""
        prompt = self._build_script_prompt(theme, episodes, duration, style, audience,
                                            lessons_hint)

        message = self.client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            # 修复：Anthropic Messages API 要求 system 为顶层参数，messages 中不得出现 system role
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        content = message.content[0].text if message.content else ""

        # 解析 JSON
        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            script = json.loads(content.strip())
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            logger.error(f"原始内容: {content[:500]}")
            script = self._parse_fallback(content)

        return script

    def generate_script_with_client(self, client, theme: str, episodes: int = 1,
                                    duration_per_episode: int = 60,
                                    style: str = "古风仙侠",
                                    target_audience: str = "年轻观众",
                                    lessons_hint: str = "") -> Dict:
        """走「OpenAI 兼容」客户端生成剧本（Web 端主流程用前端 AI 设置的密钥）。

        client 为 llm_client.LLMClient（由 app._current_llm_client() 构造，密钥取自
        前端「文本分析模型」）。与 _generate_with_claude 共用同一份 prompt，仅把
        底层调用换成 client.chat_json_robust（OpenAI chat/completions 协议，自带
        截断感知 + 自动提高 max_tokens 重试），因此任意 OpenAI 兼容模型均可驱动。
        """
        if client is None:
            raise ValueError("generate_script_with_client 需要一个 LLMClient 实例")
        prompt = self._build_script_prompt(theme, episodes, duration_per_episode, style,
                                           target_audience, lessons_hint)
        return client.chat_json_robust(prompt, system=SYSTEM_PROMPT, max_tokens=8000)

    def _parse_fallback(self, content: str) -> Dict:
        """降级解析"""
        try:
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                return json.loads(content[start:end+1])
        except Exception as e:
            logger.debug("JSON 切片解析失败（试下一个候选）：%s", e)
        # 2026-10-12 t40：不再静默返回空 shots 剧本（会落盘一个 0 镜头的「剧本」）。
        # 抛 ValueError 让 _generate_with_claude 的调用方（generate_script）走 except 回退
        # 到本地兜底剧本，失败可见。
        raise ValueError(
            f"剧本 JSON 解析失败：模型输出既非合法 JSON，也无法切片提取（前 200 字：{content[:200]!r}）")

    def save_script(self, script: Dict, project_name: str, provider: str = None) -> str:
        """保存剧本到文件；provider 缺省时用 self.provider（CLI 链路），
        Web 端 OpenAI 兼容链路可显式传入「openai-compatible」以如实记录来源。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        # t102：文件名加 uuid 后缀，避免同秒内并发生成同 project 覆盖同名文件。
        filename = f"{project_name}_{timestamp}_{uuid.uuid4().hex[:6]}.json"
        filepath = os.path.join(SCRIPT_DIR, filename)

        # 2026-10-12 t40：合并而非覆盖 —— 兜底剧本等自带 metadata（如 fallback_reason）
        # 的字段不应被抹掉，只更新/新增生成元信息。
        _meta = script.setdefault("metadata", {})
        _meta.update({
            "generated_at": datetime.now().isoformat(),
            "project_name": project_name,
            "provider": provider if provider else self.provider,
        })

        # t102：原子写（与 save_episode_script 同口径），崩溃/断电不留半截 JSON。
        fs_atomic.atomic_write_json(filepath, script)

        logger.info(f"剧本已保存: {filepath}")
        return filepath

    def load_script(self, filepath: str) -> Dict:
        """加载剧本文件"""
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)

    # ===== 资产提示词提取 =====


