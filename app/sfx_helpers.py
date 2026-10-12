# -*- coding: utf-8 -*-
'''音效隔离（新模块）（2026-10-11 从 app.py 下沉）。'''

# 由 tools/sink_helpers.py 自动生成：闭包展开 + 原样复制 import。
# 函数体与下沉前逐字一致（仅 app.logger -> logger）。
import logging

logger = logging.getLogger(__name__)

# t41（死代码清理）：原 _isolate_shot_sfx 是「单镜分离」唯一入口，但实测**无任何调用者**
#   （仅 app.py 顶层 re-export）。它背后的 sfx_isolate.isolate_sfx 分离步骤也因此无触发点，
#   sfx 音效功能是「半成品」——mix_helpers 装配 build_sfx_entries 读「已分离文件」，但分离从未
#   运行过，故 sfx_entries 恒为空（走「未找到分离音效轨」warning 分支）。此为 mix/sfx_isolate
#   半下线的一部分。函数体已删除，仅保留废弃占位以兼容 app.py 的历史 re-export
#   （from sfx_helpers import _isolate_shot_sfx），待 app.py 清理 re-export 后一并删除本占位。
_isolate_shot_sfx = None

