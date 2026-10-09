#!/usr/bin/env python3
"""
本地漫剧自动化生成系统
基于 MiniMax H3 + FlashVSR + 云端 LLM
作者: Qi (齐活林)
日期: 2026-09-09
"""

import json
import os
import sys
import subprocess
import requests
import time
from pathlib import Path
from typing import List, Dict, Optional
import argparse

# ==================== 配置 ====================
COMFYUI_PATH = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI"
WORKFLOW_PATH = os.path.join(COMFYUI_PATH, "user", "default", "workflows", "H3信号10段测试001.json")
OUTPUT_DIR = os.path.join(COMFYUI_PATH, "output", "comic_drama")
CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # 使用 Claude API
WORKBENCH_API_KEY = os.getenv("WORKBENCH_API_KEY", "")  # WorkBuddy API

# H3 模型配置
H3_MODEL_PATH = os.path.join(COMFYUI_PATH, "models", "diffusion_models", "minimax-h3")
FLASH_VSR_PATH = os.path.join(COMFYUI_PATH, "models", "FlashVSR-v1.1")
QWEN_TTS_MODEL = os.path.join(COMFYUI_PATH, "custom_nodes", "ComfyUI-Qwen-TTS")

# 分辨率设置 (H3 原生支持)
RESOLUTIONS = {
    "768p": (1344, 768),      # 16:9
    "768p_vertical": (768, 1344),  # 9:16 竖屏
    "480p": (858, 480),       # 16:9
    "480p_vertical": (480, 858),    # 9:16
}

# ==================== LLM 剧本生成器 ====================
class ScriptGenerator:
    """使用云端 LLM 生成剧本和分镜"""
    
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.anthropic.com/v1/messages"
    
    def generate_script(self, theme: str, episodes: int = 1, duration_per_episode: int = 60) -> Dict:
        """生成漫剧剧本"""
        prompt = f"""你是一位专业的AI漫剧编剧。请根据以下主题生成一部{episodes}集的短剧剧本。

主题：{theme}
每集时长：约{duration_per_episode}秒（竖屏9:16）
风格：日漫/国风，适合短视频平台

请输出JSON格式的分镜脚本：
{{
  "title": "剧集标题",
  "characters": [
    {{
      "name": "角色名",
      "appearance": "外貌描述（用于生成角色参考图）",
      "voice_style": "声线描述",
      "reference_prompt": "英文提示词，用于生成角色设定图"
    }}
  ],
  "scenes": [
    {{
      "shot_id": 1,
      "duration": 5,
      "camera": "景别（远景/全景/中景/近景/特写）",
      "description": "画面描述（详细，包含动作、表情、光影）",
      "dialogue": "台词",
      "emotion": "情绪",
      "audio_cues": "音效提示",
      "prompt_en": "英文图像生成提示词",
      "prompt_h3": "MiniMax H3视频生成提示词（中文）",
      "reference_images": ["需要参考的角色名列表"]
    }}
  ]
}}

要求：
1. 每集8-12个镜头，每个镜头4-8秒
2. 开头3秒必须有强钩子吸引观众
3. 角色外观描述要详细，便于AI生成一致的角色图
4. H3提示词要包含：画面内容、运镜方式、声音描述
5. 台词简洁有力，符合角色性格
6. 结尾要有悬念或反转

只输出JSON，不要其他内容。"""

        response = requests.post(
            self.base_url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]
            }
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result["content"][0]["text"]
            # 提取JSON
            start = content.find("{")
            end = content.rfind("}") + 1
            return json.loads(content[start:end])
        else:
            raise Exception(f"API Error: {response.status_code} - {response.text}")
    
    def generate_character_reference(self, character: Dict) -> str:
        """生成角色参考图提示词"""
        prompt = f"""Create a character reference sheet for AI image generation:
- Character name: {character['name']}
- Appearance: {character['appearance']}
- Style: Anime/Chinese animation style, clean background
- Views needed: front face, side face, full body
- Quality: high detail, consistent lighting, suitable for IPAdapter reference"""
        return prompt


# ==================== ComfyUI 控制器 ====================
class ComfyUIController:
    """控制 ComfyUI 进行视频生成"""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 8188):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
    
    def is_running(self) -> bool:
        """检查 ComfyUI 是否运行"""
        try:
            resp = requests.get(f"{self.base_url}/system_stats", timeout=5)
            return resp.status_code == 200
        except:
            return False
    
    def load_workflow(self, workflow_path: str) -> Dict:
        """加载工作流"""
        with open(workflow_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def queue_prompt(self, prompt: Dict, extra: Dict = None) -> str:
        """排队执行工作流"""
        if extra:
            prompt.update(extra)
        resp = requests.post(
            f"{self.base_url}/prompt",
            json={"prompt": prompt}
        )
        if resp.status_code == 200:
            return resp.json().get("prompt_id", "")
        raise Exception(f"Queue failed: {resp.text}")
    
    def wait_for_completion(self, prompt_id: str, timeout: int = 600) -> Dict:
        """等待任务完成"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                resp = requests.get(f"{self.base_url}/history/{prompt_id}", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if prompt_id in data:
                        return data[prompt_id]
            except:
                pass
            time.sleep(2)
        raise TimeoutError(f"Task {prompt_id} timed out")
    
    def get_output_files(self, prompt_id: str) -> List[str]:
        """获取输出文件列表"""
        history = self.wait_for_completion(prompt_id)
        outputs = []
        for node_id, output in history.get("outputs", {}).items():
            if "videos" in output:
                for video in output["videos"]:
                    outputs.append(video["filename"])
            if "images" in output:
                for img in output["images"]:
                    outputs.append(img["filename"])
        return outputs
    
    def update_workflow_params(self, workflow: Dict, params: Dict) -> Dict:
        """更新工作流参数"""
        nodes = workflow.get("nodes", [])
        for node in nodes:
            node_id = str(node["id"])
            # 更新提示词
            if node["type"] == "Text Multiline" and "prompt" in params:
                node["widgets_values"][0] = params["prompt"]
            # 更新参考图路径
            if node["type"] == "LoadImage" and "reference_image" in params:
                node["inputs"][0]["value"] = params["reference_image"]
            # 更新分辨率
            if node["type"] == "ResolutionSelector" and "resolution" in params:
                w, h = RESOLUTIONS.get(params["resolution"], (1344, 768))
                node["widgets_values"][0] = f"{w}x{h}"
            # 更新步数
            if node["type"] == "BasicScheduler" and "steps" in params:
                node["widgets_values"][1] = params["steps"]
        return workflow
    
    def generate_video_segment(self, prompt: str, reference_image: str = None, 
                                resolution: str = "768p_vertical", steps: int = 20) -> str:
        """生成单个视频片段"""
        workflow = self.load_workflow(WORKFLOW_PATH)
        
        # 更新参数
        params = {"prompt": prompt, "resolution": resolution, "steps": steps}
        if reference_image:
            params["reference_image"] = reference_image
        workflow = self.update_workflow_params(workflow, params)
        
        # 执行
        prompt_id = self.queue_prompt(workflow)
        history = self.wait_for_completion(prompt_id)
        
        # 获取输出
        outputs = self.get_output_files(prompt_id)
        if outputs:
            return os.path.join(self.base_url, "view", outputs[0])
        return None


# ==================== FlashVSR 超分辨率 ====================
class FlashVSRProcessor:
    """使用 FlashVSR 进行视频超分辨率"""
    
    def __init__(self, comfyui_controller: ComfyUIController):
        self.comfyui = comfyui_controller
    
    def upscale_video(self, input_video: str, output_path: str, scale: int = 4) -> bool:
        """对视频进行**真实**超分辨率

        说明：原实现拼装的 AILab_FlashVSR_Advanced 链路依赖 models/FlashVSR 权重，
        本机未部署（仅部署了 models/FlashVSR-v1.1），属不可用占位；
        现统一委托已实跑验证的 app/upscale_client.VideoUpscaler
        （FlashVSR Ultra-Fast：VHS_LoadVideoPath → FlashVSRInitPipe → FlashVSRNodeAdv → VHS_VideoCombine）。
        失败会打印明确原因并返回 False，不再静默成功。
        """
        import sys
        import shutil

        app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
        if app_dir not in sys.path:
            sys.path.insert(0, app_dir)
        from upscale_client import VideoUpscaler  # noqa: E402

        project = os.path.basename(os.path.dirname(os.path.abspath(output_path))) or "comic_drama"
        try:
            res = VideoUpscaler().upscale(input_video, project_name=project, scale=int(scale))
        except Exception as e:
            print(f"FlashVSR error: {e}")
            return False

        if output_path and os.path.abspath(output_path) != os.path.abspath(res["output_path"]):
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            shutil.copy2(res["output_path"], output_path)
        b, a = res["before"], res["after"]
        print(f"   ✓ 超分完成: {b['width']}x{b['height']} -> {a['width']}x{a['height']}，"
              f"耗时 {res['elapsed_sec']}s，产物 {res['output_path']}")
        return True


# ==================== 主流程 ====================
class ComicDramaPipeline:
    """漫剧生成主流程"""
    
    def __init__(self, claude_api_key: str = None):
        self.script_gen = ScriptGenerator(claude_api_key or CLAUDE_API_KEY)
        self.comfyui = ComfyUIController()
        self.flashvsr = FlashVSRProcessor(self.comfyui)
        self.output_dir = Path(OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def run(self, theme: str, episodes: int = 1, duration_per_episode: int = 60,
            do_upscale: bool = True, resolution: str = "768p_vertical"):
        """运行完整流程"""
        print("=" * 60)
        print("🎬 本地漫剧自动生成系统")
        print("=" * 60)
        
        # Step 1: 生成剧本
        print("\n📝 Step 1: 生成剧本...")
        script = self.script_gen.generate_script(theme, episodes, duration_per_episode)
        script_path = self.output_dir / "script.json"
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(script, f, ensure_ascii=False, indent=2)
        print(f"   ✓ 剧本已保存: {script_path}")
        
        # Step 2: 生成角色参考图
        print("\n👤 Step 2: 准备角色参考...")
        character_folders = {}
        for char in script.get("characters", []):
            char_folder = self.output_dir / "characters" / char["name"]
            char_folder.mkdir(parents=True, exist_ok=True)
            character_folders[char["name"]] = str(char_folder)
            print(f"   • {char['name']}: {char_folder}")
        
        # Step 3: 生成视频片段
        print("\n🎥 Step 3: 生成视频片段...")
        video_segments = []
        for scene in script.get("scenes", []):
            print(f"   [{scene['shot_id']:02d}] 生成中...", end=" ")
            
            # 构建 H3 提示词
            h3_prompt = f"""{scene['prompt_h3']}
时长: {scene['duration']}秒
景别: {scene['camera']}
情绪: {scene['emotion']}
对白: {scene['dialogue']}"""
            
            # 获取参考图
            ref_images = []
            for char_name in scene.get("reference_images", []):
                char_folder = character_folders.get(char_name, "")
                if char_folder:
                    ref_files = list(Path(char_folder).glob("*.png"))
                    if ref_files:
                        ref_images.append(str(ref_files[0]))
            
            # 生成视频
            video_path = self.comfyui.generate_video_segment(
                prompt=h3_prompt,
                reference_image=ref_images[0] if ref_images else None,
                resolution=resolution
            )
            
            if video_path:
                video_segments.append({
                    "shot_id": scene["shot_id"],
                    "path": video_path,
                    "duration": scene["duration"],
                    "dialogue": scene["dialogue"]
                })
                print("✓")
            else:
                print("✗ 失败")
        
        # Step 4: 超分辨率（可选）
        if do_upscale and video_segments:
            print("\n🔍 Step 4: 超分辨率处理...")
            for seg in video_segments:
                output_path = str(self.output_dir / f"seg_{seg['shot_id']:02d}_upscaled.mp4")
                if self.flashvsr.upscale_video(seg["path"], output_path, scale=4):
                    seg["path"] = output_path
                    print(f"   ✓ 片段 {seg['shot_id']:02d} 超分完成")
        
        # Step 5: 合并视频
        print("\n🎞️ Step 5: 合并视频...")
        final_video = self.output_dir / f"{script['title']}.mp4"
        self._merge_videos(video_segments, str(final_video))
        print(f"   ✓ 成品: {final_video}")
        
        print("\n" + "=" * 60)
        print("✅ 漫剧生成完成！")
        print(f"   总镜头数: {len(video_segments)}")
        print(f"   总时长: {sum(s['duration'] for s in video_segments)}秒")
        print(f"   输出目录: {self.output_dir}")
        print("=" * 60)
        
        return {
            "script": script,
            "videos": video_segments,
            "output_dir": str(self.output_dir)
        }
    
    def _merge_videos(self, segments: List[Dict], output_path: str):
        """合并视频片段"""
        # 创建 concat 文件列表
        concat_file = self.output_dir / "concat.txt"
        with open(concat_file, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"file '{seg['path']}'\n")
        
        # 使用 FFmpeg 合并
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            output_path
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"FFmpeg error: {e}")
            raise


# ==================== CLI ====================
def main():
    parser = argparse.ArgumentParser(description="本地漫剧自动生成系统")
    parser.add_argument("--theme", required=True, help="漫剧主题")
    parser.add_argument("--episodes", type=int, default=1, help="集数")
    parser.add_argument("--duration", type=int, default=60, help="每集时长(秒)")
    parser.add_argument("--no-upscale", action="store_true", help="跳过超分辨率")
    parser.add_argument("--resolution", choices=list(RESOLUTIONS.keys()), 
                       default="768p_vertical", help="分辨率")
    parser.add_argument("--claude-key", help="Claude API Key")
    
    args = parser.parse_args()
    
    # 检查依赖
    if not requests.get("http://127.0.0.1:8188/system_stats", timeout=5).ok:
        print("❌ ComfyUI 未运行，请先启动 ComfyUI")
        sys.exit(1)
    
    api_key = args.claude_key or CLAUDE_API_KEY
    if not api_key:
        print("❌ 需要 Claude API Key，请设置环境变量 ANTHROPIC_API_KEY 或使用 --claude-key")
        sys.exit(1)
    
    # 运行流程
    pipeline = ComicDramaPipeline(api_key)
    pipeline.run(
        theme=args.theme,
        episodes=args.episodes,
        duration_per_episode=args.duration,
        do_upscale=not args.no_upscale,
        resolution=args.resolution
    )


if __name__ == "__main__":
    main()
