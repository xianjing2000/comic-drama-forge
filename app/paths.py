"""集中产物路径解析（2026-10-10 设计审查修复）。

## 为什么单独建这个模块
设计审查发现 autopilot ↔ pipeline 互相 import。分析后确认依赖是**不对称**的：
  · autopilot → pipeline 是大量真实依赖（run_episode / step_script / normalize_config /
    list_deliverables / record_deliverable / mark_dead_letter …），是正当的调用方向；
  · pipeline → autopilot 只用到 **2 个工具函数**：_autopilot_dir 与 chapters_and_text，
    与编排无关，纯属"顺手放在那儿"。
把这两个搬走后循环即断。

## 纪律（很重要）
本模块**只允许依赖 config**，不得 import 任何业务模块 —— 它是所有模块的公共下游，
一旦反向依赖就会重新制造循环。所有函数都是纯路径拼接，无副作用。
"""
from __future__ import annotations

import os

from config import PROJECT_OUTPUT_DIR


def autopilot_dir(project: str = "") -> str:
    """autopilot 的产物根目录（可按项目细分）。

    原实现在 autopilot._autopilot_dir，通过 _A() 延迟取宿主模块拿 PROJECT_OUTPUT_DIR；
    这里直接从 config 取，因此不依赖 app，pipeline 可安全引用。
    """
    root = os.path.join(PROJECT_OUTPUT_DIR, 'autopilot')
    return os.path.join(root, project) if project else root


def project_output_dir(*parts: str) -> str:
    """output/ 下的任意子路径（便捷拼接）。"""
    return os.path.join(PROJECT_OUTPUT_DIR, *[p for p in parts if p])
