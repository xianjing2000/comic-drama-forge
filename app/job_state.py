# -*- coding: utf-8 -*-
"""生成链共享状态（**唯一来源**）。

为什么单独成模块
----------------
生成链的 7 个域（/api/scripts · /api/generation · /api/assets · /api/storyboards ·
/api/keyframes · /api/videos · /api/scenes）与它们的 worker 共用同一份运行状态。
2026-10-09 两次把生成链整块搬进蓝图时，import 自检 / 路由完整性 / 端点冒烟三级全绿，
但守卫抓到了真实行为回归 —— 根因指向「状态被搬走后出现**两份绑定**」：
app 侧与蓝图侧各持一个名字，写入/读取不再指向同一对象。

因此这里把状态抽成**零依赖叶子模块**：
  · 所有消费方（app.py、routes/*.py、各 worker）统一 `from job_state import ...`；
  · 本模块**只放状态，不放业务函数**，避免反向依赖与循环导入；
  · 任何地方都不得重新赋值这两个名字（只能原地 mutate / 加锁）。
"""
import threading

#: 生成链运行状态表：按 task_id 记录进度 / 结果 / 产物路径。
#: worker 线程与请求线程共享**同一对象**，因此禁止整体重新赋值。
generation_state = {}

#: 保护 generation_state 的全局锁（不可重入；与 worker 共用）。
lock = threading.Lock()

# 2026-10-11 归位：以下两组状态原本定义在 app.py（L9669/L10355 附近），
# 但按本模块的定位（所有消费方统一从这里 import）它们本就该在这里。
# 归位后 app.py 改为 from job_state import ... 再导出，引用点零改动。
dub_tasks = {}
dub_lock = threading.Lock()

mix_tasks = {}
mix_lock = threading.Lock()
