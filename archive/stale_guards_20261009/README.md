# 归档的失效守卫（2026-10-09）

这两条守卫断言依赖的 API **在当前代码里已完全不存在**（不是搬迁导致的引用失效，而是早已被删除/改名）：

| 守卫 | 断言的 API | 现状 |
|---|---|---|
| verify_qc_retry_stop.py | app._qc_repeat_features | 全仓库仅在旧备份 app/app.py.bak_chapteraudit 里存在；当前代码无此名字 |
| verify_h3_prompt_kit.py | h3_prompt_kit.lang_tag | 该模块当前公开 clamp_prompt / clamp_h3_prompt / fmt_ts / dialogue_lines / speaker_slots / build_soundscape / build_music，无语言判定 |

处置：移入本目录（**保留文件，未删除**），从巡检集合移出，避免长期占红灯噪声。
如需恢复：把文件移回 .workbuddy/test/ 并先**重新实现**对应 API（或改断言）。
