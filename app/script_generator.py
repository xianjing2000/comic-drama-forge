"""
剧本生成器 - 使用云端 LLM 生成结构化分镜脚本
图片资产分为三大类：角色（多视图）、物品（3D多视角）、场景（3D多视角）
"""
import json
import os
import logging
from typing import List, Dict, Optional
from datetime import datetime

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

    def generate_script_with_qc(self, theme: str, episodes: int = 1,
                                 duration_per_episode: int = 60,
                                 style: str = "国漫古风",
                                 target_audience: str = "年轻观众",
                                 project_name: str = "",
                                 enable_qc: bool = True,
                                 max_qc_retries: int = 3) -> Dict:
        """生成剧本并进行质检，质检失败由主AI判断如何处理
        
        参数:
            theme: 主题
            episodes: 集数
            duration_per_episode: 每集时长（秒）
            style: 创作风格
            target_audience: 目标观众
            project_name: 项目名称
            enable_qc: 是否启用水印质检
            max_qc_retries: 最大质检重试次数
        
        返回:
            dict: {
                "script": dict,           # 剧本数据
                "qc_result": dict,        # 质检结果
                "judgment": dict,         # 主AI判断结果
                "attempts": int,          # 尝试次数
                "passed": bool,           # 是否通过质检
            }
        """
        # 延迟导入，避免循环依赖
        import qc_client
        import ai_qc_judge
        import prompt_memory
        
        # 获取质检配置
        qc_config_path = QC_CONFIG_PATH
        qc_cfg = qc_client.load_config(qc_config_path)
        
        attempts = []
        last_script = None
        last_qc_result = None
        last_judgment = None
        
        for attempt in range(max_qc_retries):
            logger.info(f"剧本生成尝试 {attempt + 1}/{max_qc_retries}")

            # 重试前召回历史教训（首版 attempt==0 不必召回）。
            # 用上一版剧本的 JSON 串作稳定 phash 键（与下方 record_with_context 同键）。
            lessons_hint = ""
            if attempt > 0:
                try:
                    lessons_hint = prompt_memory.learned_prompt(
                        kind="script",
                        prompt=json.dumps(last_script, ensure_ascii=False)[:2000],
                        project=project_name or "unknown",
                        root_dir=PROJECT_OUTPUT_DIR,
                    )
                except Exception:
                    lessons_hint = ""

            # 生成剧本
            try:
                script = self.generate_script(theme, episodes, duration_per_episode,
                                              style, target_audience,
                                              lessons_hint=lessons_hint)
                last_script = script
            except Exception as e:
                logger.error(f"剧本生成失败: {e}")
                attempts.append({"attempt": attempt + 1, "error": str(e)})
                continue
            
            # 如果未启用水印质检，直接返回
            if not enable_qc:
                return {
                    "script": script,
                    "qc_result": {"skipped": True, "reason": "水印质检未启用"},
                    "judgment": None,
                    "attempts": attempt + 1,
                    "passed": True,
                }
            
            # 执行剧本质检
            qc_result = qc_client.check_script(
                script_data=script,
                style=style,
                target_duration=duration_per_episode,
                cfg=qc_cfg,
            )
            last_qc_result = qc_result
            
            attempts.append({
                "attempt": attempt + 1,
                "score": qc_result.get("score"),
                "passed": qc_result.get("passed"),
                "issues_count": len(qc_result.get("issues", [])),
            })
            
            # 如果质检通过，直接返回
            if qc_result.get("passed"):
                logger.info(f"剧本质检通过 (得分: {qc_result.get('score')})")
                return {
                    "script": script,
                    "qc_result": qc_result,
                    "judgment": None,
                    "attempts": attempt + 1,
                    "passed": True,
                }
            
            # 质检未通过，由主AI判断如何处理
            logger.warning(f"剧本质检未通过 (得分: {qc_result.get('score')}), 由主AI判断处理方式")
            
            # 调用主AI判断模块
            context = {
                "attempt": attempt + 1,
                "max_retries": max_qc_retries,
                "project_name": project_name,
                "style": style,
                "target_audience": target_audience,
                "duration_per_episode": duration_per_episode,
                "is_final": False,
            }
            
            judgment = ai_qc_judge.judge_qc_result(qc_result, context)
            last_judgment = judgment
            
            # 记录到记忆模块
            try:
                prompt_memory.record_with_context(
                    project=project_name or "unknown",
                    kind="script",
                    category=ai_qc_judge.assess_severity(qc_result),
                    priority=judgment.get("severity", "medium"),
                    context=context,
                    prompt=json.dumps(script, ensure_ascii=False)[:2000],
                    issues=qc_result.get("issues", []),
                    reason=qc_result.get("reason", ""),
                    score=qc_result.get("score"),
                    root_dir=PROJECT_OUTPUT_DIR,
                )
            except Exception as e:
                logger.warning(f"记录质检问题到记忆模块失败: {e}")
            
            # 根据主AI判断执行 - 24小时自动执行模式，无需人工干预
            logger.info(f"主AI判断: 严重程度={judgment.get('severity')}, 策略={judgment.get('strategy')}")
            
            # 记录判断结果
            if judgment.get("requires_human"):
                # 在24小时自动执行模式下，不转人工，而是记录警告并继续
                logger.warning(f"主AI判断原本需要人工处理，但在自动执行模式下继续: {judgment.get('strategy')}")
            
            if judgment.get("should_continue"):
                # 可以继续流程（仅记录问题）
                logger.info("主AI判断可以继续流程")
                return {
                    "script": script,
                    "qc_result": qc_result,
                    "judgment": judgment,
                    "attempts": attempt + 1,
                    "passed": True,  # 虽然质检未通过，但主AI允许继续
                    "with_warnings": True,
                }
            
            # 需要重试，继续循环
            logger.info(f"主AI判断需要重试: {judgment.get('strategy')}")
        
        # 达到最大重试次数
        logger.warning(f"达到最大质检重试次数 ({max_qc_retries})")
        
        # 最后一次尝试的结果
        return {
            "script": last_script,
            "qc_result": last_qc_result,
            "judgment": last_judgment,
            "attempts": max_qc_retries,
            "passed": False,
            "max_retries_reached": True,
            "attempts_history": attempts,
        }

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

    @staticmethod
    def _build_script_prompt(theme: str, episodes: int, duration: int, style: str,
                             audience: str, lessons_hint: str = "") -> str:
        """拼装剧本生成提示词（Claude 与 OpenAI 兼容两条链路共用同一份 prompt）。

        lessons_hint：历史质检教训，追加在 JSON 格式说明**之后**、结尾（不破坏 JSON
        输出格式要求）；空串 → 零行为变更。
        """
        prompt = f"""你是一个专业的AI漫剧编剧。请为以下主题生成详细的分镜脚本。

主题：{theme}
风格：{style}
目标观众：{audience}
集数：{episodes}集
每集约{duration}秒

请严格按照以下JSON格式输出（不要添加任何其他内容）：

{{
  "title": "剧集标题",
  "theme": "{theme}",
  "style": "{style}",
  "characters": [
    {{
      "name": "角色名",
      "age": "年龄描述",
      "appearance": "外貌描述：发型、五官、体型、服装、配饰（用于生成角色参考图，需详细具体）",
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
      "description": "画面描述（详细，50字以内）",
      "visual_detail": "扩展画面细节（时间/天气/光源方向/动作过程补全；没有则空串）",
      "dialogue": "角色台词",
      "emotion": "情绪",
      "beat": "叙事节拍（触发/铺垫/对峙/爆发/收尾 等）",
      "edit_reason": "这一镜为什么切（剪辑动机，20~30字）",
      "scene_lighting": "本镜光影（光源方向/色温/氛围）",
      "audio_cues": "音效/音乐提示",
      "prompt_h3": "H3视频生成提示词（英文，包含角色动作、镜头运动、环境描述）",
      "characters_in_shot": ["角色名列表"],
      "items_in_shot": ["物品名列表"],
      "blocking": [{{"name": "角色名", "x": "left/center/right", "depth": "front/mid/back", "facing": "camera/left/right/back"}}],
      "action": "本镜动作beat（谁做了什么、动作从哪到哪；纯对话镜也写神态小动作，不得留空）"
    }}
  ],
  "production_notes": {{
    "total_shots": 12,
    "estimated_duration": 60,
    "style_guide": "整体风格说明"
  }}
}}

要求：
1. characters：每个角色要有详细的外貌描述（发型/五官/服装/配饰），方便生成多视图参考图
2. items：提取剧情中出现的重要物品（武器、法宝、关键道具），通常2-5个
3. scenes：提取剧情涉及的主要场景，通常3-6个
4. shots：至少10-15个分镜，每个分镜5-8秒，标注出场角色和物品
5. 一镜一动作：每镜只承载一个动作节拍（action 字段），连续动作拆成相邻两镜；
   景别分布以 近景/中近景/局部 为主（各约25%），特写+全景合计不超过10%，
   大特写/远景/大远景不用；运镜只用固定/轻推/轻摇/跟随/轻手持，特写与局部一律固定
6. 输出必须是合法的JSON格式，不要包含任何注释"""

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
        return {
            "title": "未命名剧本",
            "characters": [],
            "items": [],
            "scenes": [],
            "shots": [],
            "production_notes": {}
        }

    def save_script(self, script: Dict, project_name: str, provider: str = None) -> str:
        """保存剧本到文件；provider 缺省时用 self.provider（CLI 链路），
        Web 端 OpenAI 兼容链路可显式传入「openai-compatible」以如实记录来源。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        filename = f"{project_name}_{timestamp}.json"
        filepath = os.path.join(SCRIPT_DIR, filename)

        script["metadata"] = {
            "generated_at": datetime.now().isoformat(),
            "project_name": project_name,
            "provider": provider if provider else self.provider
        }

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(script, f, ensure_ascii=False, indent=2)

        logger.info(f"剧本已保存: {filepath}")
        return filepath

    def load_script(self, filepath: str) -> Dict:
        """加载剧本文件"""
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)

    # ===== 资产提示词提取 =====


