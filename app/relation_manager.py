# -*- coding: utf-8 -*-
"""角色关系图谱管理器

核心功能:
1. 角色间关系定义 (亲属/朋友/敌人/恋人/师徒等)
2. 关系权重与双向关系
3. 关系图谱数据导出 (JSON/SVG)
4. 关系冲突检测

参考实现:
- dramaclaw: 角色关系边 + 关系强度
- AIComicBuilder: 角色关系管理
"""
import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 关系类型常量
RELATION_TYPES = {
    "family": "亲属",
    "friend": "朋友",
    "enemy": "敌人",
    "romance": "恋人",
    "mentor": "师徒",
    "colleague": "同事",
    "rival": "对手",
    "ally": "盟友",
    "stranger": "陌生人",
    "master": "主仆",
}
ALL_RELATION_TYPES = list(RELATION_TYPES.keys())

# 关系强度 (0-1)
RELATION_STRENGTH = {
    "close": 0.9,
    "strong": 0.7,
    "medium": 0.5,
    "weak": 0.3,
    "neutral": 0.0,
    "hostile": -0.5,
    "enemy": -0.8,
}


class RelationManager:
    """角色关系管理器"""

    def __init__(self, project_key: str, base_dir: str):
        self.project_key = project_key
        self.base_dir = Path(base_dir)
        self.relation_dir = self.base_dir / "characters" / "relations"
        self._load_data()

    def _load_data(self):
        """加载关系数据"""
        data_file = self.relation_dir / "relations.json"
        if data_file.exists():
            with open(data_file, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        else:
            self.data = {
                "project_key": self.project_key,
                "characters": {},
                "relations": {},
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "version": "1.0"
                }
            }

    def save(self):
        """保存关系数据"""
        self.relation_dir.mkdir(parents=True, exist_ok=True)
        data_file = self.relation_dir / "relations.json"
        with open(data_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def sync_characters(self, characters: dict):
        """同步角色数据到关系库"""
        self.data["characters"] = characters
        self.save()

    def add_relation(self, char_id_a: str, char_id_b: str, relation_type: str, strength: str = "medium", note: str = "") -> str:
        """添加角色关系"""
        if relation_type not in ALL_RELATION_TYPES:
            raise ValueError(f"无效的关系类型: {relation_type}")

        # 关系ID格式: char_a_char_b (按字母序排序确保唯一)
        pair = tuple(sorted([char_id_a, char_id_b]))
        rel_id = f"{pair[0]}_{pair[1]}"

        self.data["relations"][rel_id] = {
            "id": rel_id,
            "char_a": pair[0],
            "char_b": pair[1],
            "type": relation_type,
            "strength": strength,
            "strength_value": RELATION_STRENGTH.get(strength, 0.0),
            "note": note,
            "bidirectional": True,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }

        self.save()
        logger.info(f"添加关系: {pair[0]} --{relation_type}--> {pair[1]}")
        return rel_id

    def update_relation(self, rel_id: str, **kwargs):
        """更新角色关系"""
        if rel_id not in self.data["relations"]:
            raise ValueError(f"关系不存在: {rel_id}")

        rel = self.data["relations"][rel_id]
        valid_keys = {"type", "strength", "note", "bidirectional"}
        for key in valid_keys:
            if key in kwargs:
                rel[key] = kwargs[key]
                if key == "strength":
                    rel["strength_value"] = RELATION_STRENGTH.get(kwargs[key], 0.0)
        rel["updated_at"] = datetime.now().isoformat()
        self.save()

    def delete_relation(self, rel_id: str):
        """删除角色关系"""
        if rel_id not in self.data["relations"]:
            raise ValueError(f"关系不存在: {rel_id}")
        del self.data["relations"][rel_id]
        self.save()


    def get_all_relations(self) -> dict:
        """获取所有关系"""
        return self.data["relations"]

    def get_graph_data(self) -> dict:
        """获取图谱数据 (节点+边)"""
        nodes = []
        chars = self.data.get("characters", {})
        for char_id, char in chars.items():
            nodes.append({
                "id": char_id,
                "name": char.get("name", char_id),
                "role": char.get("role", ""),
                "x": None,
                "y": None
            })

        edges = []
        for rel in self.data["relations"].values():
            edges.append({
                "source": rel["char_a"],
                "target": rel["char_b"],
                "type": rel["type"],
                "strength": rel["strength"],
                "strength_value": rel["strength_value"],
                "label": RELATION_TYPES.get(rel["type"], rel["type"])
            })

        return {"nodes": nodes, "edges": edges}

    def detect_conflicts(self) -> list:
        """检测关系冲突 (同一对角色有多个关系)"""
        conflicts = []
        seen = {}
        for rel in self.data["relations"].values():
            pair = tuple(sorted([rel["char_a"], rel["char_b"]]))
            if pair in seen:
                conflicts.append({
                    "characters": pair,
                    "relations": [seen[pair]["id"], rel["id"]]
                })
            else:
                seen[pair] = rel
        return conflicts

    def export_svg(self, width: int = 800, height: int = 600) -> str:
        """导出关系图谱为SVG"""
        graph = self.get_graph_data()
        nodes = graph["nodes"]
        edges = graph["edges"]

        if not nodes:
            return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {} {}"><text x="400" y="300" text-anchor="middle" fill="#94a3b8">暂无角色关系</text></svg>'.format(width, height)

        # 简单力导向布局 (迭代)
        self._force_layout(nodes, edges, width, height, iterations=150)

        # 生成SVG
        svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">']

        # defs for markers
        svg_parts.append('''<defs>
  <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth">
    <path d="M0,0 L0,6 L9,3 z" fill="#666"/>
  </marker>
</defs>''')

        # 绘制边
        for edge in edges:
            src = next((n for n in nodes if n["id"] == edge["source"]), None)
            tgt = next((n for n in nodes if n["id"] == edge["target"]), None)
            if src and tgt and src.get("x") and tgt.get("x"):
                color = self._get_edge_color(edge["type"], edge["strength_value"])
                sw = max(1, min(4, abs(edge["strength_value"]) * 4 + 1))
                svg_parts.append(f'<line x1="{src["x"]}" y1="{src["y"]}" x2="{tgt["x"]}" y2="{tgt["y"]}" stroke="{color}" stroke-width="{sw}" opacity="0.7"/>')

        # 绘制节点
        for node in nodes:
            if node.get("x") is None:
                continue
            r = 25 if node["role"] == "主角" else 20
            fill = "#6366f1" if node["role"] == "主角" else "#8b5cf6"
            svg_parts.append(f'<circle cx="{node["x"]}" cy="{node["y"]}" r="{r}" fill="{fill}" stroke="#fff" stroke-width="2"/>')
            svg_parts.append(f'<text x="{node["x"]}" y="{node["y"] + 5}" text-anchor="middle" fill="#fff" font-size="12">{node["name"]}</text>')

        svg_parts.append('</svg>')
        return ''.join(svg_parts)

    def _force_layout(self, nodes, edges, width, height, iterations):
        """简单力导向布局算法"""
        # 初始化位置
        for node in nodes:
            node["x"] = random.randint(50, width - 50)
            node["y"] = random.randint(50, height - 50)
            node["vx"] = 0
            node["vy"] = 0

        for _ in range(iterations):
            # 斥力 (节点之间)
            for i, n1 in enumerate(nodes):
                for n2 in nodes[i+1:]:
                    dx = n2["x"] - n1["x"]
                    dy = n2["y"] - n1["y"]
                    dist = (dx*dx + dy*dy) ** 0.5
                    if dist < 1:
                        dist = 1
                    force = 500 / dist
                    fx = dx / dist * force
                    fy = dy / dist * force
                    n1["vx"] -= fx
                    n1["vy"] -= fy
                    n2["vx"] += fx
                    n2["vy"] += fy

            # 引力 (边连接)
            for edge in edges:
                src = next((n for n in nodes if n["id"] == edge["source"]), None)
                tgt = next((n for n in nodes if n["id"] == edge["target"]), None)
                if src and tgt:
                    dx = tgt["x"] - src["x"]
                    dy = tgt["y"] - src["y"]
                    dist = (dx*dx + dy*dy) ** 0.5
                    if dist < 1:
                        dist = 1
                    force = dist * 0.01
                    fx = dx / dist * force
                    fy = dy / dist * force
                    src["vx"] += fx
                    src["vy"] += fy
                    tgt["vx"] -= fx
                    tgt["vy"] -= fy

            # 中心引力
            for node in nodes:
                node["vx"] += (width/2 - node["x"]) * 0.001
                node["vy"] += (height/2 - node["y"]) * 0.001

            # 更新位置
            for node in nodes:
                node["vx"] *= 0.9
                node["vy"] *= 0.9
                node["x"] += node["vx"]
                node["y"] += node["vy"]
                # 边界约束
                node["x"] = max(30, min(width - 30, node["x"]))
                node["y"] = max(30, min(height - 30, node["y"]))

        # 清除临时属性
        for node in nodes:
            del node["vx"]
            del node["vy"]

        return nodes

    def _get_edge_color(self, rel_type: str, strength: float) -> str:
        """根据关系类型和强度返回颜色"""
        colors = {
            "family": "#10b981",    # 绿色
            "friend": "#3b82f6",    # 蓝色
            "enemy": "#ef4444",     # 红色
            "romance": "#ec4899",   # 粉色
            "mentor": "#8b5cf6",    # 紫色
            "colleague": "#f59e0b", # 橙色
            "rival": "#f97316",     # 深橙
            "ally": "#06b6d4",      # 青色
            "stranger": "#64748b",  # 灰色
            "master": "#a855f7",    # 紫罗兰
        }
        return colors.get(rel_type, "#64748b")


class RelationConflictDetector:
    """关系冲突检测器"""

    def __init__(self, relation_manager: RelationManager):
        self.rel_mgr = relation_manager

    def check_conflicts(self) -> list:
        """检查所有关系冲突"""
        return self.rel_mgr.detect_conflicts()

    def get_conflict_summary(self) -> dict:
        """获取冲突摘要"""
        conflicts = self.check_conflicts()
        return {
            "total_conflicts": len(conflicts),
            "conflicts": conflicts
        }
