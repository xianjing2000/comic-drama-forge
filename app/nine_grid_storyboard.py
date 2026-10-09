# -*- coding: utf-8 -*-
"""九宫格分镜系统 - 对标 BigBanana 的镜头工作台

核心功能:
1. 生成9个候选镜头构图
2. 九宫格预览展示
3. 选择最优构图
4. 关键帧生成
"""
import json
import random
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime


class NineGridStoryboard:
    """九宫格分镜系统"""

    # 镜头构图预设
    COMPOSITIONS = [
        {"name": "全景", "angle": "wide", "zoom": "0.3", "description": "完整场景展示"},
        {"name": "中景", "angle": "medium", "zoom": "0.5", "description": "角色全身"},
        {"name": "近景", "angle": "close", "zoom": "0.7", "description": "角色上半身"},
        {"name": "特写", "angle": "extreme_close", "zoom": "0.9", "description": "面部特写"},
        {"name": "仰拍", "angle": "low_angle", "zoom": "0.6", "description": "从下往上拍"},
        {"name": "俯拍", "angle": "high_angle", "zoom": "0.6", "description": "从上往下拍"},
        {"name": "侧面", "angle": "profile", "zoom": "0.5", "description": "侧面视角"},
        {"name": "斜侧", "angle": "three_quarter", "zoom": "0.5", "description": "四分之三视角"},
        {"name": "过肩", "angle": "over_shoulder", "zoom": "0.4", "description": "过肩镜头"}
    ]

    def __init__(self, project_key: str, base_dir: str):
        self.project_key = project_key
        self.base_dir = Path(base_dir) / "storyboards"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def generate_nine_grid(self, scene_description: str, character_ids: List[str], 
                          emotion: str = "neutral") -> Dict[str, Any]:
        """生成九宫格分镜"""
        shots = []
        used_compositions = random.sample(self.COMPOSITIONS, 9)

        for i, comp in enumerate(used_compositions):
            shot = {
                "index": i,
                "composition": comp["name"],
                "camera_angle": comp["angle"],
                "zoom_level": comp["zoom"],
                "description": comp["description"],
                "scene": scene_description,
                "characters": character_ids,
                "emotion": emotion,
                "aspect_ratio": "16:9",
                "duration_sec": random.uniform(2, 5),
                "prompt_hint": f"{comp['description']}, {scene_description}, {emotion} mood",
                "selected": False
            }
            shots.append(shot)

        return {
            "project_key": self.project_key,
            "created_at": datetime.now().isoformat(),
            "scene": scene_description,
            "total_shots": len(shots),
            "shots": shots
        }

    def select_best_shot(self, nine_grid: Dict[str, Any], selected_index: int) -> Dict[str, Any]:
        """选择最佳镜头"""
        if selected_index < 0 or selected_index >= len(nine_grid['shots']):
            raise ValueError(f"无效的选择索引: {selected_index}")

        for shot in nine_grid['shots']:
            shot['selected'] = (shot['index'] == selected_index)

        selected_shot = nine_grid['shots'][selected_index]
        selected_shot['is_final'] = True

        return selected_shot


