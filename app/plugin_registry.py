# -*- coding: utf-8 -*-
"""插件化扩展注册表（P2-4）

对标 Novella 的「自定义 Agent 扩展插件」能力：把漫剧流水线的每个环节
抽象为可注册的插件，使新增能力（表情包生成、音效自动配、字幕翻译……）
不必改动主流程。

内置插件
--------
声明式登记 12 个内置环节（剧本/资产/分镜/视频/质检/一致性/配音/混音/超分/
水印/成片合成/导出），每个带 meta：名称、类别、输入输出、依赖、是否已实现。

外部插件
--------
把 .py 文件放进 `app/plugins/`，实现 `register(registry)` 即被自动加载：
    # app/plugins/my_agent.py
    def register(registry):
        registry.add(PluginSpec(
            id="my.sfx", name="音效自动配", category="post",
            description="按镜头情绪自动匹配音效",
            run=lambda ctx: {...},
        ))

加载失败只记录日志，绝不影响主程序启动（插件是增强，不是必需）。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import traceback
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, List, Optional

logger = logging.getLogger(__name__)

PLUGINS_DIR = os.path.join(os.path.dirname(__file__), "plugins")

# 插件类别
CATEGORY_LABELS = {
    "script": "剧本",
    "asset": "资产",
    "storyboard": "分镜",
    "video": "视频",
    "audio": "音频",
    "post": "后期",
    "qc": "质检",
    "export": "导出",
    "other": "其他",
}


@dataclass
class PluginSpec:
    """插件描述"""

    id: str
    name: str
    category: str = "other"
    description: str = ""
    # 输入/输出：字段名列表，便于前端串流程
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    # 依赖的能力类别（如 video 依赖 image 先产出）
    depends_on: List[str] = field(default_factory=list)
    # 是否为内置环节（False 表示外部扩展）
    builtin: bool = True
    # 是否已实现（骨架插件为 False）
    implemented: bool = True
    # 调用入口：接收 context dict，返回 dict
    run: Optional[Callable] = None
    # 来源（文件路径或 builtin）
    source: str = "builtin"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("run", None)          # 函数不可序列化
        d["has_run"] = callable(self.run)
        d["category_label"] = CATEGORY_LABELS.get(self.category, self.category)
        return d


class PluginRegistry:
    """插件注册表"""

    def __init__(self):
        self._items: dict = {}
        self._load_errors: list = []

    # ---------- 注册 ----------

    def add(self, spec: PluginSpec) -> PluginSpec:
        if not isinstance(spec, PluginSpec):
            raise TypeError("add() 需要 PluginSpec 实例")
        if not spec.id:
            raise ValueError("插件 id 不能为空")
        if spec.id in self._items:
            logger.warning(f"插件 id 重复，后者覆盖前者：{spec.id}")
        self._items[spec.id] = spec
        return spec

    def remove(self, plugin_id: str) -> bool:
        return self._items.pop(plugin_id, None) is not None

    # ---------- 查询 ----------

    def get(self, plugin_id: str) -> Optional[PluginSpec]:
        return self._items.get(plugin_id)

    def list(self, category: str = None) -> List[PluginSpec]:
        items = list(self._items.values())
        if category:
            items = [p for p in items if p.category == category]
        return sorted(items, key=lambda p: (p.category, p.builtin is False, p.id))

    def catalog(self) -> dict:
        """按类别组织的目录（供前端渲染插件面板）"""
        grouped: dict = {}
        for p in self.list():
            grouped.setdefault(p.category, []).append(p.to_dict())
        return {
            "total": len(self._items),
            "categories": [{"key": k, "label": CATEGORY_LABELS.get(k, k),
                            "count": len(v), "items": v} for k, v in grouped.items()],
            "load_errors": list(self._load_errors),
        }

    def dependency_order(self) -> list:
        """按依赖关系拓扑排序（依赖在前），供一键跑全流程参考"""
        done, order = set(), []
        items = self.list()

        def visit(spec, stack):
            if spec.id in done:
                return
            if spec.id in stack:
                logger.warning(f"插件依赖存在环，忽略：{spec.id}")
                return
            stack.add(spec.id)
            for dep in spec.depends_on:
                dep_spec = next((x for x in items if x.id == dep), None)
                if dep_spec:
                    visit(dep_spec, stack)
            stack.discard(spec.id)
            done.add(spec.id)
            order.append(spec.id)

        for s in items:
            visit(s, set())
        return order

    # ---------- 执行 ----------

    def run(self, plugin_id: str, context: dict = None) -> dict:
        """执行插件（统一异常包装，避免插件故障影响调用方）"""
        spec = self.get(plugin_id)
        if not spec:
            return {"ok": False, "error": f"插件不存在：{plugin_id}"}
        if not callable(spec.run):
            return {"ok": False, "error": f"插件 {plugin_id} 未提供可执行入口"}
        try:
            result = spec.run(context or {})
            if isinstance(result, dict):
                return {"ok": result.get("ok", True), "plugin": plugin_id, **result}
            return {"ok": True, "plugin": plugin_id, "result": result}
        except Exception as e:  # noqa: BLE001  插件失败不得抛给调用方
            logger.exception(f"插件执行失败：{plugin_id}")
            return {"ok": False, "plugin": plugin_id, "error": str(e),
                    "traceback": traceback.format_exc(limit=3)}

    # ---------- 外部加载 ----------

    def load_builtin(self) -> int:
        """登记内置流水线环节"""
        builtins = [
            PluginSpec("builtin.script", "剧本生成", "script",
                       "小说/主题 → 结构化剧本（含原文覆盖率守门）",
                       inputs=["novel", "theme"], outputs=["script"]),
            PluginSpec("builtin.assets", "图片资产生成", "asset",
                       "角色多视图 / 物品·场景 3D 多视角",
                       inputs=["script"], outputs=["assets"], depends_on=["builtin.script"]),
            PluginSpec("builtin.storyboard", "分镜图生成", "storyboard",
                       "按镜头生成分镜图（引用角色/物品/场景资产）",
                       inputs=["script", "assets"], outputs=["storyboards"],
                       depends_on=["builtin.assets"]),
            PluginSpec("builtin.video", "视频生成", "video",
                       "H3 逐镜/整集/关键帧三种模式生成视频片段",
                       inputs=["script", "storyboards"], outputs=["videos"],
                       depends_on=["builtin.storyboard"]),
            PluginSpec("builtin.qc", "AI 质检", "qc",
                       "图片逐张 + 视频抽帧送多模态模型，不达标自动重试",
                       inputs=["images", "videos"], outputs=["qc_reports"]),
            PluginSpec("builtin.consistency", "一致性校验", "qc",
                       "跨镜头角色相似度 + 服饰状态比对",
                       inputs=["assets", "storyboards"], outputs=["consistency_report"],
                       depends_on=["builtin.storyboard"]),
            PluginSpec("builtin.tts", "配音合成", "audio",
                       "QwenTTS 逐角色语音合成",
                       inputs=["script"], outputs=["dub_audio"], depends_on=["builtin.script"]),
            PluginSpec("builtin.mix", "音画合成", "audio",
                       "按镜头时间轴把配音对齐到成片",
                       inputs=["videos", "dub_audio"], outputs=["dubbed_video"],
                       depends_on=["builtin.video", "builtin.tts"]),
            PluginSpec("builtin.upscale", "超分放大", "post",
                       "FlashVSR 2x/4x 超分（768p → 2K/4K）",
                       inputs=["videos"], outputs=["upscaled_videos"], depends_on=["builtin.video"]),
            PluginSpec("builtin.watermark", "水印处理", "post",
                       "文案/图片水印，支持全视频移动模式",
                       inputs=["videos"], outputs=["watermarked_videos"]),
            PluginSpec("builtin.final", "成片合成", "post",
                       "FFmpeg 合并片段 + 字幕烧录/外挂",
                       inputs=["videos"], outputs=["final_video"],
                       depends_on=["builtin.video"]),
            PluginSpec("builtin.export", "NLE 导出", "export",
                       "剪映草稿 / FCPXML / SRT / 帧序列清单",
                       inputs=["script", "videos"], outputs=["export_bundle"],
                       depends_on=["builtin.video"]),
        ]
        for s in builtins:
            s.source = "builtin"
            self.add(s)
        return len(builtins)

    def load_external(self) -> int:
        """从 app/plugins/ 加载外部插件（每个模块需实现 register(registry)）"""
        if not os.path.isdir(PLUGINS_DIR):
            return 0
        count = 0
        for finder, mod_name, is_pkg in pkgutil.iter_modules([PLUGINS_DIR]):
            if mod_name.startswith("_"):
                continue
            path = getattr(finder, "path", "") or os.path.join(PLUGINS_DIR, f"{mod_name}.py")
            try:
                spec = importlib.util.spec_from_file_location(
                    f"mjscxt_plugins.{mod_name}", os.path.join(PLUGINS_DIR, f"{mod_name}.py"))
                if not spec or not spec.loader:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                reg = getattr(mod, "register", None)
                if not callable(reg):
                    logger.warning(f"插件 {mod_name} 未实现 register(registry)，已跳过")
                    continue
                before = set(self._items.keys())
                reg(self)
                # 把本次新增的插件标记为外部来源
                for pid in set(self._items.keys()) - before:
                    self._items[pid].source = path
                    self._items[pid].builtin = False
                added = len(set(self._items.keys()) - before)
                count += added
                logger.info(f"已加载外部插件 {mod_name}（新增 {added} 项）")
            except Exception as e:  # noqa: BLE001  插件加载失败不影响主程序
                msg = f"{mod_name}: {type(e).__name__}: {e}"
                self._load_errors.append(msg)
                logger.warning(f"外部插件加载失败（已跳过）：{msg}")
        return count


# ===================== 全局单例 =====================

_REGISTRY: Optional[PluginRegistry] = None


def get_registry(reload: bool = False) -> PluginRegistry:
    """取全局插件注册表（首次调用时登记内置 + 加载外部插件）"""
    global _REGISTRY
    if _REGISTRY is None or reload:
        reg = PluginRegistry()
        n_builtin = reg.load_builtin()
        n_ext = reg.load_external()
        logger.info(f"插件注册表就绪：内置 {n_builtin} 项，外部 {n_ext} 项")
        _REGISTRY = reg
    return _REGISTRY


def catalog() -> dict:
    return get_registry().catalog()


def run(plugin_id: str, context: dict = None) -> dict:
    return get_registry().run(plugin_id, context)
