# -*- coding: utf-8 -*-
# 后台 worker 包（2026-10-11 起从 app.py 下沉）。
#
# 为什么下沉：
#   app.py 原有 8 个 worker、合计 3784 行（占该文件 33%）：
#     _generate_asset_task 1046 ｜ _storyboard_worker 990 ｜ _video_generate_worker_body 1222
#     _episodes_worker 224 ｜ _dub_worker 142 ｜ _mix_worker 74 ｜ _video_generate_worker 56
#     _screenplay_worker 30
#   而 pipeline.py 直接引用其中的 _dub_worker / _storyboard_worker /
#   _video_generate_worker / _generate_asset_task —— 形成「业务模块依赖路由文件」的
#   倒置依赖（app.py 里 48 个私有成员被 pipeline 引用，根因就在这里）。
#
# 搬迁纪律：
#   · 函数体逻辑一字不改（除 app.logger → 模块 logger 这一处必要调整）；
#   · 共享状态一律来自 job_state（唯一来源），不在本包内新建；
#   · app.py 保留 from workers.X import _xxx 再导出，既有调用表面零改动。
from workers.screenplay import _screenplay_worker  # noqa: F401
from workers.episodes import _episodes_worker, _salvage_episode_script  # noqa: F401
from workers.audio import (  # noqa: F401
    _audio_line_expect_sec, _audio_qc_lines, _cleanup_scratch_dir,
    _dub_prompt_preflight, _dub_worker, _mix_audio_qc, _mix_audio_url, _mix_worker,
)

__all__ = [
    '_screenplay_worker', '_episodes_worker', '_salvage_episode_script',
    '_cleanup_scratch_dir', '_audio_line_expect_sec', '_dub_prompt_preflight',
    '_audio_qc_lines', '_mix_audio_qc', '_mix_audio_url', '_dub_worker', '_mix_worker',
]
