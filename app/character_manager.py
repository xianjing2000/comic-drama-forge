# -*- coding: utf-8 -*-
"""角色一致性管理系统 - 对标 AIComicBuilder 四视图 + BigBanana 定妆照

核心功能:
1. 角色定义与信息管理
2. 角色参考图管理 (多角度定妆照)
3. 角色ID映射 (用于跨镜头一致性)
4. 角色资产导出 (提示词+参考图)

参考实现:
- AIComicBuilder: 角色四视图 (正面/侧面/背面/表情)
- BigBanana: 定妆照系统 + 衣橱系统
"""
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 角色状态常量
CHARACTER_STATUS_NEW = "new"
CHARACTER_STATUS_CONFIRMED = "confirmed"
CHARACTER_STATUS_PENDING = "pending"

# 视角类型
VIEW_FRONTSIDE = "front"
VIEW_THREEQUARTER = "three_quarter"
VIEW_SIDE = "side"
VIEW_BACK = "back"
VIEW_EXPRESSIONS = "expressions"
ALL_VIEWS = [VIEW_FRONTSIDE, VIEW_THREEQUARTER, VIEW_SIDE, VIEW_BACK, VIEW_EXPRESSIONS]


class CharacterManager:
    """角色管理器 - 管理项目中所有角色及其一致性资产"""

    def __init__(self, project_key: str, base_dir: str):
        self.project_key = project_key
        self.base_dir = Path(base_dir)
        self.character_dir = self.base_dir / "characters"
        self.reference_dir = self.character_dir / "references"
        self._load_data()

    def _load_data(self):
        """加载角色数据"""
        data_file = self.character_dir / "characters.json"
        if data_file.exists():
            with open(data_file, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        else:
            self.data = {
                "project_key": self.project_key,
                "characters": {},
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "version": "1.0"
                }
            }

    def save(self):
        """保存角色数据"""
        self.character_dir.mkdir(parents=True, exist_ok=True)
        self.reference_dir.mkdir(parents=True, exist_ok=True)

        data_file = self.character_dir / "characters.json"
        with open(data_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def add_character(
        self,
        name: str,
        role: str,
        description: str,
        outfit: str = "",
        status: str = CHARACTER_STATUS_NEW
    ) -> str:
        """添加新角色"""
        char_id = f"char_{len(self.data['characters']) + 1:03d}"

        self.data['characters'][char_id] = {
            "id": char_id,
            "name": name,
            "role": role,
            "description": description,
            "outfit": outfit,
            "status": status,
            "views": {view: None for view in ALL_VIEWS},
            "references": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }

        self.save()
        logger.info(f"添加角色: {name} (ID: {char_id})")
        return char_id

    def update_character(
        self,
        char_id: str,
        name: Optional[str] = None,
        role: Optional[str] = None,
        description: Optional[str] = None,
        outfit: Optional[str] = None,
        status: Optional[str] = None
    ):
        """更新角色信息"""
        if char_id not in self.data['characters']:
            raise ValueError(f"角色不存在: {char_id}")

        char = self.data['characters'][char_id]
        if name:
            char['name'] = name
        if role:
            char['role'] = role
        if description:
            char['description'] = description
        if outfit:
            char['outfit'] = outfit
        if status:
            char['status'] = status
        char['updated_at'] = datetime.now().isoformat()

        self.save()

    def set_reference(self, char_id: str, view_type: str, image_path: str):
        """设置角色参考图"""
        if char_id not in self.data['characters']:
            raise ValueError(f"角色不存在: {char_id}")

        char = self.data['characters'][char_id]
        if view_type not in ALL_VIEWS:
            raise ValueError(f"无效的视角类型: {view_type}")

        # 复制图片到参考目录
        src = Path(image_path)
        dst = self.reference_dir / f"{char_id}_{view_type}{src.suffix}"
        if src.exists():
            shutil.copy2(src, dst)
            char['views'][view_type] = str(dst)
            char['references'].append({
                "view": view_type,
                "path": str(dst),
                "added_at": datetime.now().isoformat()
            })
        self.save()

    def get_character_prompt(self, char_id: str) -> str:
        """生成角色生成提示词"""
        if char_id not in self.data['characters']:
            raise ValueError(f"角色不存在: {char_id}")

        char = self.data['characters'][char_id]
        prompt_parts = [
            f"{char['name']}, {char['role']}",
            char['description'],
            f"wearing {char['outfit']}" if char['outfit'] else ""
        ]
        return ", ".join(p for p in prompt_parts if p)

    def get_character_reference_urls(self, char_id: str) -> list:
        """获取角色参考图URL列表"""
        if char_id not in self.data['characters']:
            return []

        char = self.data['characters'][char_id]
        return [v for v in char['views'].values() if v]


    def get_all_characters(self) -> dict:
        """获取所有角色"""
        return self.data['characters']


