# 归档：前端待补的守卫（2026-10-09）

| 守卫 | 通过/总数 | 唯一失败项 | 阻塞原因 |
|---|---|---|---|
| verify_reasoning_effort.py | 43 / 44 | client.ts 的 aiConfigApi.test 带 reasoning_effort | **前端未补参数**（后端与其余 43 项全绿） |

处置：移入本目录并保留文件（未删除）。等前端补上 `reasoning_effort` 后，把文件移回
.workbuddy/test/ 即可恢复巡检（预期一次通过）。

注：verify_progress_visibility.py **未归档** —— 它的 3 条失败里已有 1 条（工具中文名）修好，
剩 2 条（ProductionBar 组件 / 面板渲染）仍留在巡检里作为前端待办的真实红灯。
