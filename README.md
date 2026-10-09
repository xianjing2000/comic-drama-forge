# 本地漫剧自动化生成系统

基于 **云端 LLM + ComfyUI（QwenImage2.1 + Qwen-Edit 多视角 + MiniMax H3 + FlashVSR）** 的完整 AI 漫剧生成流程

## 🎯 系统架构

```
┌────────────────────────────────────────────────────────────────────┐
│                    云端 LLM（OpenAI 兼容·任意厂商）                  │
│   输入：故事主题 → 输出：结构化 JSON 剧本                            │
│   （角色 / 物品 / 场景 / 镜头 分镜 + 各类生成提示词）                 │
└────────────────────────────────┬───────────────────────────────────┘
                                 ↓
┌────────────────────────────────────────────────────────────────────┐
│                     ComfyUI 本地执行（三阶段资产管线）                │
│                                                                    │
│  ┌───────────────────────── 图片资产（三类） ─────────────────────┐  │
│  │                                                              │  │
│  │  角色 Characters                    物品 Items               │  │
│  │  ┌──────────────┐  ┌─────────────┐  ┌──────────────┐         │  │
│  │  │QwenImage2.1  │→ │Qwen-Edit    │  │QwenImage2.1  │         │  │
│  │  │基础图生成(白底)│  │多视图生成    │  │基础图生成(白底)│        │  │
│  │  └──────────────┘  └─────────────┘  └────┬─────────┘         │  │
│  │   正面/左侧/右侧/背面              ┌─────▼─────────────┐       │  │
│  │                                   │Qwen-Edit          │       │  │
│  │   场景 Scenes                     │3D多视角生成        │       │  │
│  │   ┌──────────────┐  ┌─────────────┐└──────────────────┘       │  │
│  │   │QwenImage2.1  │→ │Qwen-Edit    │ 正面/左45°/右45°/俯视     │  │
│  │   │基础图生成     │  │3D多视角生成  │ （物品和场景共用此管线）   │  │
│  │   └──────────────┘  └─────────────┘                           │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────── 视频生成 ────────────────────────────┐   │
│  │  MiniMax H3 (Ref2VA) 整集一次生成，动态段数无缝视频            │   │
│  │  **唯一模式 = episode**（整集按「场景」分组逐场提交、最后拼接） │   │
│  │    · 逐镜头 per_shot / 关键帧 keyframe 已废弃并移出流程        │   │
│  │  参考图（分镜图 + 角色锚点 + 道具锚点）→ 视频 + 原生音频        │   │
│  │  同场内段间引导（尾 22 帧钉进下一段）+ 跨场末镜衔接            │   │
│  │  Turbo LoRA 8步加速 + H3ContinuousSeamlessJoin 无缝拼接      │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                    │
│  ┌─────────────────────── 后期处理 ────────────────────────────┐   │
│  │  FlashVSR 超分（2x/3x/4x，默认 2x，逐场先超分再流拼接）      │   │
│  │  → FFmpeg 合成 + 字幕                                       │   │
│  │  ⚠️ 8G 显存实测极慢（单场 46s 需数小时），可按需关闭         │   │
│  │     （enable_upscale=false，成片直接用 H3 原分辨率）         │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

## 🆕 能力增强（对标 GitHub 开源漫剧项目后的优化落地）

本节记录相对初版的实质性增强。所有条目均已在本机实测通过（含真实模型调用）。

### 风格一致性（端到端闭环）

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 风格统一注入 | `app/style_kit.py` | `normalize_style` / `with_style` / `apply_asset_style` / `style_emphasis`；风格只在单点定义，所有生成入口共用 |
| 分镜/资产/视频全链路 | `app/app.py` + `app/keyframe.py` | 分镜提示词、角色/场景/物品参考提示词、尾帧 keyframe 均注入风格后缀；视频重试用强化头 |
| 质检风格达标检测 | `app/qc_client.py` | `find_style_issues`（关键词+否定词兜底）/ `_apply_style_gate`（硬闸门强制 `passed=False`）/ `check_image` + `check_video` 接收 `style` 参数并注入 `{style}` 占位 |
| 风格不达标→改提示词 | `app/qc_client.py` + `app/prompt_memory.py` | `_qc_gate` 返回 `style_blocked`；`_record_qc_lesson` 注入「严格采用 XX 风格」强化建议；`prompt_memory` 支持「风格/画风」种子词召回 |
| 视频 worker 补齐反馈 | `app/app.py` `_video_generate_worker` | 重试时调 `learned_prompt(kind="video", ...)` 重写 prompt；保存 `orig_video_prompt` 作为稳定 key |

### 风格选择与质检反馈闭环（建项目可选风格 + 质检即时优化提示词）

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 建项目可选/自定义风格 | `frontend/src/pages/ProjectsPage.tsx` + `app/app.py` | 新建项目弹窗提供 6 个风格预设下拉 + 自定义风格输入；`_project_style` 三级兜底（plan.json > AI 设定面板 > `config.style`），`_style_aspect_confirmed` 认可 config.style 也算「风格已确认」，选风格后不再被 409 拦 |
| 质检失败即时优化提示词 | `app/app.py` `_optimize_prompt_from_qc` | 出图/出片质检不达标时，把**本次 verdict 的 issues** 喂给文本分析模型即时改写提示词，再重新生成；优先级 即时优化 > 历史召回（`prompt_memory.learned_prompt`）> 仅换种子；未配置模型或调用失败时 best-effort 返回 None 回落，绝不拖垮重试 |
| 角色/物品纯白背景 | `app/comfyui_client.py` `_ensure_fullbody_prompt` / `_ensure_item_white_bg` | 角色三视图与物品基础图强制纯白背景（无场景、地面、桌面、阴影、背景纹理），物品完整居中、边缘清晰，便于后续分镜/视频参考锁定 |

### 画面细节与光影（提示词质量）

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 画面细节独立字段 | `app/novel_to_script.py` | `visual_detail` = `description` 限长 200 字的**溢出位**。剧本 schema 让模型直接输出（描述压到 80 字），`_norm_shots` 优先取模型值、否则 `_overflow_detail` 兜底 —— 画面细节不再因截断而丢失 |
| 分镜图光影引导 | `app/comfyui_client.py` | `_LIGHT_KEYWORDS` 27 组（黄昏/雨夜/烛光/月光…）+ 无光源词时按情绪兜底，抽成独立「光影氛围」句，避免「视频有日落、分镜图却是正午平光」；`_light_hint_covered` 按首段短语幂等去重 |
| 细节合并去重 | `_merge_visual_detail` / `h3_prompt_kit._beats` | 两个来源（分析器写的 `storyboard_prompt_zh` + `visual_detail`）叠加时按分句/子串去重，同一句细节不会写两遍 |
| 连贯性同步 | `app/continuity.py` | 别名归一、state 抽取、相邻集校验、局部重写四处均带上 `visual_detail`；重写描述后旧细节同步换/清空，避免「新描述 + 旧细节」自相矛盾 |
| 动作镜时长 | `estimate_shot_duration` | `_ACTION_MARKERS` 动作词加成（约 +0.4~1.5s），打斗/追逐镜不再被压到 3 秒基准 |

### 工程底座

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 配置外置 | `app/config.py` + `.env.example` | 移除全部硬编码路径，`COMFYUI_ROOT` 单点配置自动推导 workflows/input/output/models；换机只改 `.env` |
| 部署档案 | `DEPLOY_PROFILE` | `8G` / `16G` / `server` 三档，影响超分与视频默认档位 |
| 密钥加密 | `app/secret_store.py` | Fernet 对称加密；优先级 环境变量 > 加密库 `output/secrets.enc` > json 明文（首次读取**自动迁移**）；加密不可用时拒绝明文落盘 |
| 持久化任务队列 | `app/task_store.py` | SQLite（`output/tasks.db`）+ 单元级进度 + 串行队列；启动自动 `recycle_interrupted()` |
| 断点续跑 | `is_unit_done` / `filter_pending_units` | 判据以**磁盘产物**为准——产物存在且非空即视为完成，状态表丢失也能正确跳过 |
| 统一环境加载 | `app/env_loader.py` | 任一模块单独导入（不经过 `config`）也能读到 `.env`，避免「换入口就丢密钥」 |
| 依赖自检（模型 + 插件） | `app/deps_check.py` + `GET /api/deps/check` | 自动核查 ComfyUI 侧**插件节点**（`custom_nodes/` 里 6 个包，映射自 `docs/依赖清单.md`）与**模型权重**（`models/` 下 H3 + QwenImage2.1 全链路文件）是否齐备。双通道：在线拉 `/object_info` 以「节点是否注册」为准；离线/强制离线则退化为扫描本地 `custom_nodes/` 目录名并标注「不可靠」。只读、绝不阻断，返回 `comfyui / plugins / models / workflows / summary` 结构化结果，缺什么一目了然 |

### 质量与一致性

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 跨镜头一致性校验 | `app/consistency.py` | 双通道：感知哈希（aHash/dHash，零依赖快筛）+ 多模态语义判定「是否同一角色」；输出差异点 |
| 一致性看板 | 步骤9 · 一致性看板 | 报告落盘 `output/continuity/<项目>/consistency.json`，按镜头/角色展示相似度与判定 |
| 原文承载归属 | `_shot_coverage_map` | 每个镜头承载了原文哪几句（4-gram 字面比对，与 `coverage.py` 同口径），画布上直接可见 |
| 单镜重跑 | `/api/storyboard/retry-shot`、`/api/video/retry-shot` | 只影响目标镜头，不触碰其它产物；分镜重跑同样受质检闸门约束 |
| 提示词预检（生成前质检） | `app/prompt_qc.py` + 生成链路 7 处接入 | 出图/出片**之前**先过一遍质检：分镜图 / H3 / 资产 / **尾帧** 四类提示词的确定性检查（骨架完整性、风格声明、参考图用途、台词泄漏、字幕类指令、质量空词、H3 六段结构与时间码、语言标记、尾帧的「锚定参考图 / 取景连贯 / 链式承接」语义）。可修的当场**自愈**，致命的按模式拦截 —— 把 GPU 花在有问题的提示词上是纯浪费，且出图后才发现就已经晚了。**覆盖全部图片生成路径**（资产图 / 分镜图 / 尾帧），手动链路与 autopilot 流水线一致 |
| 预检开关与模式 | `qc_config.json` | `prompt_enabled`（默认开）/ `prompt_mode`（`warn` 只记录 / `repair` 自愈后放行（默认）/ `block` 有问题即拦）。⚠️ 与图片/视频质检不同，这一层**不依赖质检接口**：纯确定性检查、零成本、零模型依赖，没配质检接口的项目也能用 |
| 按需预检接口 | `POST /api/qc/prompt` | 传 `kind`（`storyboard`/`h3`/`asset`/`keyframe`/`audio`）/`prompt`/`style`/`context`/`ref_count` 即返回 `verdict` + 自愈后的 `prompt` + `repairs` + `gate`；`repair=false` 可只检查不改写 |
| 音频客观质检 | `app/audio_qc.py` | 零模型依赖、毫秒级、**永远执行**：一次 ffmpeg 解码取全量指标（时长 / 平均电平 / 峰值 / 静音时长 → 有声占比），再做硬闸（时长 < 0.15s、有声占比 < 15%、平均电平 < -50dB）+ 软扣分（占比偏低 / 电平偏低 / 峰值触顶 / 时长偏差超上限）。⚠️ **解析不出平均电平即判失败** —— 无声轨的 mp4 不能因为读得出 Duration 就被放行 |
| 音频 AI 质检（可选） | `qc_client.check_audio` | 用 `showspectrumpic` + `showwavespic` 把音轨渲染成**频谱图 + 波形图**喂多模态模型（顺序固定，对齐 `DEFAULT_AUDIO_PROMPT`）。两层结论**单向收紧**：客观层判死的，AI 层说好也翻不回来；AI 层任何异常一律 fail-open，绝不拖垮出片 |
| 配音台词预检 | `prompt_qc`（kind=`audio`） | 送 TTS **之前**检查台词：结构化残留（`(S1) 说：` 这类说话人前缀、`[Chinese]` 语言标记）、舞台指示、零宽字符、emoji、超长、情绪未随 instruct 下发。能确定不是台词的当场剥掉，**语义缺陷不在文本层硬改**（改为提示从剧本重新取台词），空台词绝不自愈 |
| 音频质检配置 | `qc_config.json` | `audio_enabled` / `audio_min_speech_ratio` / `audio_min_mean_db` / `audio_max_drift`；⚠️ 开关按**字符串语义**解析（`"false"`/`"0"`/`"no"` = 关），否则 `bool("false")` 为真、开关形同虚设。阈值单一事实源在 `audio_qc.py` |
| 音频质检接口 | `POST /api/qc/audio` | 不传 `path` 时按 `mix`（带配音成片）> `merged`（整集音轨）> `line`（单句）推导；`with_ai=false` 只跑客观层。⚠️ **整轨口径自动关闭有声占比判定** —— 成片/整集天然有留白，按单句口径判会满屏误报「漏句」 |

### 连续性与画面质量（2026-10 深度攻坚，用户实测驱动）

本节记录一轮由「成片实看不合理」倒逼出的系统性修复。核心结论：**连续性与画面细节不能靠事后补**，
必须在**剧本改写与分镜规划阶段就写进文字**，生成层再补「看得见」的视觉锚点。

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 道具结构三要素 | `app/novel_to_script.py` | 物品模板新增 `structure` 字段（**功能端 / 尾端 / 使用朝向**）。缘由：旧哨子被画成反吹 —— 道具描述只有材质细节，没有「哪端是吹嘴、朝向哪」，分镜阶段有参考图侥幸画对，视频阶段多帧运动就漂了 |
| 道具从角色语义分离 | `app/h3_prompt_kit.py` | `_retention_analysis` 新增 `item_labels` 通道。缘由：道具参考图原先被塞进 subjects，于是被要求保留「脸型/眉形/鼻形」、写成「costume/palette/hairstyle」，而**真正的形态与朝向无人声明**。现道具单独成行，写明形态结构 + **朝向恒定**（“never flipped or reversed”） |
| 沉默显式标注 | `app/h3_prompt_kit.py` | `retention_analysis` 对**本镜出场但非说话人**的主体标注 “is silent in this segment — the lips stay closed”。参考官方手跑模板 `H3信号10段测试001.json` 的写法 |
| 发言显式声明 | `app/h3_prompt_kit.py` | `build_summary` 在有台词时写明 `<Subject N> speaks in this segment, delivering the dialogue in「情绪」; the speaker's mouth opens and moves`。旧实现写的是 `visual content only, not spoken` —— 与「人物确实在说台词」自相矛盾，是口型不同步的高概率成因 |
| 音色随人设 | `app/app.py` `_ensure_voice_bank_refs` | 参考音色生成原先 `instruct` 硬编码为空、且只传角色名，导致剧本里现成的 `voice_style`（如「清冷低缓，气息沉稳」）**完全没用上**，女角色甚至被分到男声。现读剧本 `characters` 传入 gender/age/personality/voice_style，有音色底稿走 `mode=design` |
| 音色扩到全角色 | `app/app.py` `_h3_common_ref_audios` | 原先只取「全段共用」的公共池角色 → 只在部分镜出现的角色音色生成了却挂不上（实测 3 个角色只有 1 个生效）。新增 `all_names` 补齐（**参考图仍维持全段共用口径**，避免挤占 9 个图片槽） |
| 分镜跨镜衔接 | `app/app.py` `_prep_shot` | 复用 `_qc_prev_shot_ref`（同场景 + 已通过质检）把**上一镜成品图**挂进本镜参考，要求服装/道具/光位/陈设接续。满槽时自动放弃，首镜/跨场景不挂 |
| 视频跨场衔接 | `app/app.py` `_shot_segment(extra_ref=…)` | 换场首镜额外挂**上一场末镜分镜图**，给 H3 一个真实的跨场视觉起点（原先只有 `transition_clause` 的**文字**衔接，画面无锚点）。⚠️ 必须在槽位截断**之前**插入，否则被静默截掉 |
| 剧本层跨镜承接 | `app/prompts/script_rewrite_rules.txt` | 新增规则 **12.1 跨镜状态承接**（每镜起点必须等于上一镜的结果态；严禁换发型/道具反向/陈设矛盾；状态变化必须在该镜内演出来）与 **12.2 场次切换过渡**（同主体带出 / 同物件同光线桥接 / 必须交代时空关系） |
| 分镜层首格承接 | `app/prompts/storyboard_grid_plan.txt` | 新增规则 **0**：第 1 格必须承接上一镜的第 9 格（服装/持物/位置朝向/道具形态朝向/光源/陈设）；换场首镜另需保留一处来自上一场的视觉线索作为过渡桥 |
| 手部质量约束 | `app/prompts/script_rewrite_rules.txt` + `storyboard_grid_plan.txt` | 实测手指畸变（**杵状指/指尖膨大**）全部出现在**手部特写格**，远景中景正常。故规则②把手部特写从「推荐」降为「慎用」（细节优先给物件/表情/姿态），分镜规划新增【手部质量】硬约束（不让手占到大特写比例、九格最多一格以手为主体、以手为主体须写明五指结构完整） |
| 手部质检 | `app/qc_client.py` | `STORYBOARD_GRID_ANALYSIS_NOTE` 新增【手部结构检查】，按**四类**畸变逐格查（①末端形态：杵状指/指尖膨大/钝化 ②数量：多指少指 ③分离度：粘连融合 ④比例：过长过细/粗细不均/关节反向）；关键词表同步补 `杵状指` / `指尖膨大` / `指尖钝化` / `手指粗细不均` / `指节模糊` 等长词（避免与正常构图描述撞车误杀） |

> **一个方法论教训**（写在这里供后续排查参考）：定位「手部畸变」这类**局部偶发**缺陷时，
> 按秒均匀抽帧会漏检（畸变集中在很窄的手部特写格）。必须**按镜头/按格**查 ——
> 正确姿势是先用剧本的 `duration` 累加出时间轴，把观察点映射到具体镜号，再查该镜的九宫格分镜。

### 生成可控性

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 关键帧驱动视频 | `app/keyframe.py` + 步骤6 | 先用分镜图生成「尾帧」，再把 `[首帧, 尾帧]` 注入 H3 参考图槽位，让运动在两端之间插值；缺尾帧自动退化为首帧单锚并标注 |
| 可视化分镜画布 | 步骤9 · 分镜画布 | 卡片展示分镜图/视频/尾帧/质检分/一致性分/承载原文；拖拽排序写回剧本 `metadata.shot_order` |
| 引擎 Provider 抽象 | `app/providers/` | `ImageProvider` / `VideoProvider` / `TTSProvider` 三接口；本地 ComfyUI 实现 + 云端预留（豆包/Wan/Kling/Vidu/Veo/CosyVoice）；`MJSCXT_PROVIDER_*` 切换 |
| 插件化扩展 | `app/plugin_registry.py` + `app/plugins/` | 生产环节抽象为可注册插件（含依赖拓扑排序）；放入 `app/plugins/*.py` 暴露 `register(reg)` 即被自动加载 |

### 生态与交付

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 剪映草稿导出 | `app/nle_export.py` | `draft_content.json` + `draft_meta_info.json`，可直接在剪映打开继续精修 |
| Premiere 导出 | `nle_export.export_fcpxml` | FCPXML，供 Premiere / Final Cut |
| 字幕与帧清单 | `nle_export.export_srt` / `export_frames` | SRT 字幕 + 帧序列清单 |
| 成本与耗时看板 | `app/analytics.py` + 步骤9 | 任务完成钩子自动登记耗时；ComfyUI 调用计数（提交/失败/等待秒数/生成段数）；按部署档案估算电费 |
| 国际化 | `locales/zh-CN.json` / `en-US.json` + `/api/i18n` | 前端 `data-i18n` 键值替换，顶栏可切换 |
| Docker | `Dockerfile` + `docker-compose.yml` | 只容器化后端（ComfyUI 依赖 GPU 与数十 GB 权重，留在宿主机）；产物与任务库挂载持久化 |
| 桌面端 | `deploy/desktop/` | Electron 外壳，自动编排 ComfyUI + 后端双进程并在退出时回收 |

### 顺带修复的真实缺陷

| 缺陷 | 症状 | 修复 |
|------|------|------|
| ComfyUI 状态探测超时过长 | ComfyUI 离线时 `/api/status` 卡死约 4 分钟 | 3s 短超时 + `(connect, read)` 元组 |
| 任务库全局锁自锁 | `import app` 挂起约 2 分钟 | `threading.Lock` → `RLock` |
| 分镜图错配 | manifest 只记录部分镜头时，缺项镜头会**错配到别的镜头的分镜图**当关键帧首帧 | `_keyframe_sb_map` 改为合并式映射；`plan_keyframes` 取消数组下标兜底 |
| 嵌套明文密钥 | `qc_config.json` 的 `endpoint_override.api_key` 未被迁移，仍为明文 | `secret_store` 递归处理嵌套结构 |
| 单独导入即失败 | 只 `import qc_client` 时 `.env` 未加载，主密钥取不到、解密失败 | 新增 `env_loader.py` 统一加载 |
| 参考图静默丢失 | 前端传结构不完整的角色对象时，视频退化为无角色锚点 | 后端 `_collect_asset_refs` 从磁盘资产兜底 |
| 提示词分析器看不到画面细节 | 细节只写在 `visual_detail` 时，它写出的 `prompt_h3` / `storyboard_prompt_zh` 完全没有光影与时间描述 | `script_prompt_analyzer.build_shot_prompt` 改为合并 `description + visual_detail` 后喂入模型 |
| 重写镜头后细节错位 | 局部重写只换 `description`，旧 `visual_detail` 仍留着上一条描述的尾巴（新写正午、旧留黄昏逆光） | `rewrite_shots_for_issues` 同步刷新/清空 `visual_detail`，并把该字段纳入喂入内容与输出 schema |
| AI 总控不知道本项目在拍什么小说 | 新建项目（《铜铃巷》）后与 AI 总控沟通风格，总控却按**另一部小说**（《蛊真人》）给出整套风格方案 —— 因为它 31 个工具里**没有任何读取本项目原著的能力**，只能拿上下文里的项目名瞎猜 | 新增 `get_novel` 工具 + `GET /api/projects/<key>/novel-brief`（书名 / 章节数 / 开篇正文 / 已定风格），并在系统提示里要求「谈风格前必须先调用」 |
| 总控看得见别的项目在跑什么 | `GET /api/autopilot/status?project=X` 虽把计数/交付/曲线收敛到 X，但 `current` / `last_error` 仍直接来自全局状态，把**当时正在跑的另一个项目**的名字与剧情标题一起回显。该段落在 agent 工具结果的 1600 字符窗口内 → 模型把别的项目当成本项目 | `autopilot.status(project)` 收敛 `current`：正在生产的不是本项目时置空，只回一个中性 `other_project_running: true`；`last_error` 一并清空。前端只用不带 `project` 的全局视图，行为不变 |
| 成片被「画外音解说」淹没 | `narration` 被当成原文「心理活动 + 背景补叙 + 环境描写」的公共出口，再叠加「每镜必须有人声」的硬约束，原著所有叙述性文字都变成旁白解说。实测《蛊真人》ep04 旁白 2231 字 ≈ 496 秒铺在 100 秒画面上（**4.93x**），尾部被成片 `-shortest` 静默截掉；ep2/ep3 同为纯旁白集 | **产品决定：成片不再产出旁白**。剧本提示词新增第 8 条「本系统不产出旁白」（心理活动→角色自语台词/神态，背景与环境→画面），分镜 schema 删除 `narration`；`_norm_shots` 显式丢弃模型越界输出的 narration（硬不变量）；`tts_client` 旁白补声通道关闭并对旧剧本记警告；`audit_script` 的 ok 只认 dialogue |
| 镜头时长无视台词量 | `_norm_shots` 只要模型给了合法 duration（4~5 秒）就直接采用，从不校验这镜有多少台词 → 长台词硬贴短画面，配音沿时间轴溢出到后面几镜，尾部被静默截断（实测 ep04 21/21 镜的 duration 都取自模型值，与 `estimate_shot_duration` 返回值全不一致） | 新增 `required_shot_duration()`（不封顶的时长需求）；`_norm_shots` 取 `max(模型值, 实际需要值)`，超上限的镜头写 `duration_overflow_sec` 供体检告警；提示词新增「单镜台词合计 ≤ 30 字，超出要拆镜」 |
| 变速兜底是死代码 | `MIX_DEFAULT_PARAMS["max_line_sec"] = 0.0` 让 `dub_mix` 的 `if max_line > 0 and dur > max_line` 恒为假 → `atempo` 变速从未执行（实测 ep04 全部条目 `fit_ratio` 恒为 1.0），台词超预算时无人兜底 | 默认值改为 `8.0`（正常预算内不动、明显超预算才压），并保留每次压缩的告警 |
| 旁白体检口径与配音链路不一致 | 旧口径「无台词但有旁白」不算静默、但配音链路不念旁白；且「既无台词也无音效提示」的镜头反而不被点名 | `audit_script` 拆成 `silent`（台词/旁白/音效**三者全空**＝真缺陷）与 `no_voice`（有音效提示的合法留白，不再判失败），并新增 `overlong_speech` 溢出告警；`speaker_fallback` 改按最终音色判定，不再是一条永远为假的死规则 |

> ⚠️ **密钥提醒**：若 `ai_config.json` / `qc_config.json` 曾以明文形式进过 git、云盘同步或被分享，
> 请到对应平台**轮换密钥**——加密只防未来，已暴露的无法追回。

> 更多部署细节见 [`DESKTOP_SETUP.md`](DESKTOP_SETUP.md)。

## 📦 图片资产三类规范

### 1. 角色 Characters（多视图）
| 视图 | 文件名 | 说明 |
|------|--------|------|
| 基础图 | `base.png` | QwenImage2.1 文生图，含完整外貌描述，**纯白背景** |
| 正面 | `front.png` | 全身正面，eye-level |
| 左侧 | `left.png` | 左侧半侧面全身 |
| 右侧 | `right.png` | 右侧半侧面全身 |
| 背面 | `back.png` | 背面全身 |

**用途**：H3 视频生成的角色锁定参考图（Ref2VA 模式）
**画幅**：三视图横排拼版，**基础图/多视图均 1:1 方形**（避免横排贴边粘连）

### 2. 物品 Items（3D 多视角）
| 视图 | 文件名 | 说明 |
|------|--------|------|
| 基础图 | `base.png` | QwenImage2.1 文生图，3D渲染白底展示，**纯白背景无场景/地面/阴影** |
| 正面 | `front.png` | 正面视角 |
| 左前45° | `left45.png` | 左前45°视角 |
| 右前45° | `right45.png` | 右前45°视角 |
| 俯视 | `top.png` | 高角度俯视 |

**用途**：武器、法宝、关键道具的资产库，供分镜图和视频参考

### 3. 场景 Scenes（3D 多视角）
| 视图 | 文件名 | 说明 |
|------|--------|------|
| 基础图 | `base.png` | QwenImage2.1 文生图，含构图光影 |
| 正面 | `front.png` | 正面视角 |
| 左前45° | `left45.png` | 左前45°视角 |
| 右前45° | `right45.png` | 右前45°视角 |
| 俯视 | `top.png` | 高角度俯视全景 |

**用途**：场景氛围锁定，H3 视频生成的环境参考图

### 资产目录结构
```
output/
├── scripts/                    # 剧本 JSON
│   └── 修仙少年复仇记_20260909.json
├── assets/
│   ├── characters/             # 角色资产
│   │   └── {项目名}/
│   │       └── {角色名}/
│   │           ├── base.png    # 基础图
│   │           ├── front.png   # 正面
│   │           ├── left.png    # 左侧
│   │           ├── right.png   # 右侧
│   │           └── back.png    # 背面
│   ├── items/                  # 物品资产
│   │   └── {项目名}/
│   │       └── {物品名}/
│   │           ├── base.png
│   │           ├── front.png
│   │           ├── left45.png
│   │           ├── right45.png
│   │           └── top.png
│   └── scenes/                 # 场景资产
│       └── {项目名}/
│           └── {场景名}/
│               ├── base.png
│               ├── front.png
│               ├── left45.png
│               ├── right45.png
│               └── top.png
├── videos/                     # 视频片段（按「场」组织）
│   └── {项目名}/
│       ├── scene_01.mp4        # 第 1 场（该场各镜由 H3 原生段间衔接）
│       ├── scene_02.mp4        # 第 2 场
│       └── ep01_full.mp4       # 整集拼接（各场依次流拼接）
├── upscale/                    # 超分产物（与源片严格隔离；关闭超分时为空）
│   └── {项目名}/
│       ├── ep01_scene01_upscaled.mp4   # 逐场超分产物
│       └── ep01_upscaled.mp4           # 超分后整集
├── qc/                         # 质检中间产物（抽帧 / 参考图归一 / 3D 站位）
│   └── {项目名}/
└── final/                      # 最终成片
    └── {项目名}/
        ├── ep01_final.mp4
        └── ep01_final.mp4.srt
```

> ⚠️ **一章 = 一集**；**一场 = 一段连续时空**（`scene_no`）。视频按场提交、逐场拼接成整集，
> 这是「同场内动作连贯、相邻场自然过渡」的结构基础。`shot_NN.mp4` 是旧版逐镜头模式的遗留命名，
> 当前流程不再产出。

## 🔄 完整流程（7 步）

> 步骤序列的唯一事实源是 `app/pipeline.py` 的 `STEP_SEQUENCE`：
> `script → tts_pre → assets → storyboard → video → upscale → final`。
> 历史步骤 `keyframe`（尾帧）与 `mix`（混音）已移出序列（函数保留，仅作回滚与手工端点用）。

| # | 步骤 | 功能 | 技术 | 工作流模板 |
|---|------|------|------|-----------|
| 1 | 剧本生成 `script` | 主题 → 结构化 JSON（角色/物品/场景/镜头） | 文本分析模型（OpenAI 兼容·任意厂商） | — |
| 2 | 配音先行 `tts_pre` | 为**每个角色**生成一段参考音色音频（落 `voice_bank`），供视频生成锁定角色音色 | QwenTTS | — |
| 3 | 资产 `assets` | 角色/物品/场景的基础图 + 多视角（正/左45/右45/俯视） | QwenImage2.1 + TE-Speed | 角色生成_Qwen21.json / 物品生成_Qwen21.json / 场景生成_Qwen21.json + 分镜生成_Qwen21.json |
| 4 | 分镜图 `storyboard` | 九宫格分镜图逐镜出图（带 AI 质检与不合格重生成；跨镜引入上一镜成品图做衔接） | QwenImage2.1 + TE-Speed | 分镜生成_Qwen21.json |
| 5 | 视频生成 `video` | 整集一次生成：**按场分组逐场提交**（同场内 H3 原生段间衔接，跨场挂上一场末镜参考），**自带 H3 原生音轨（角色对白）** | MiniMax H3 Director（Ref2VA） | h3_director_r2v_单采.json |
| 6 | 超分 `upscale` | **逐场先超分 → 流拼接成整集**（`upscale_per_scene`，默认开）；fail-open：环境不可用/超时/失败只记跳过，不拖垮成片 | FlashVSR | TE-Speed-flashVSR 视频超分放大加速工作流.json |
| 7 | 成片合成 `final` | **优先采用超分产物**（落空回退整集原片）+ 字幕，输出到 `final/` | FFmpeg | — |

> **配音与音画混音不是独立环节**：H3 视频自带角色对白原生音轨，`tts_pre` 只负责备好每角色参考音色
> （视频生成时以 `audioMode=generate` 用参考音色**锁定音色**、台词由模型按各段**自生成**）。
> `step_tts` / `step_mix` 的函数保留在代码里（不在流水线序列内），仅作回滚与手工端点用。

## 📝 剧本 JSON 格式

```json
{
  "title": "修仙少年复仇记",
  "characters": [
    {
      "name": "林风",
      "appearance": "18岁少年，黑发束起，左眉有伤疤，青色道袍",
      "reference_prompt_zh": "18岁古风少年，正面全身像，黑发束起..."
    }
  ],
  "items": [
    {
      "name": "青霜剑",
      "category": "武器",
      "reference_prompt_zh": "古风长剑，3D渲染白底展示，剑身青色..."
    }
  ],
  "scenes": [
    {
      "name": "云霄宗山门",
      "location": "室外",
      "reference_prompt_zh": "仙侠风山门，云雾缭绕，石阶向上..."
    }
  ],
  "shots": [
    {
      "shot_id": 1,
      "duration": 5,
      "camera": "中景",
      "description": "林风站在悬崖边俯瞰宗门",
      "dialogue": "三年了，我回来了。",
      "prompt_h3": "A young man stands at cliff edge...",
      "characters_in_shot": ["林风"],
      "items_in_shot": ["青霜剑"]
    }
  ]
}
```

## 🚀 快速开始

### 1. 启动 ComfyUI（手动）
```batch
cd D:\ComfyUI_portable_TE_v260619\ComfyUI
.\run_nvidia_gpu_fixed.bat
```

> 启动脚本的实测配置（RTX 5060 8G）：`--windows-standalone-build --disable-auto-launch
> --preview-method auto --fast fp16_accumulation --disable-pinned-memory --lowvram --reserve-vram 1`。
> ⚠️ **不要加 `--cuda-malloc`**：本机实测与 comfy_aimdo 的显存映射层冲突，会以
> `cuMemSetAccess failed (600)` → `VRAM Allocation failed` → 进程退出。

### 2. 启动应用（后端）
```powershell
cd "<项目根>\app"
pip install -r requirements.txt
python serve.py          # 生产入口（waitress）；python app.py 为开发入口
```

或直接双击 `run_app.bat`

> **文本分析模型的密钥不在这里配**：Web 主流程统一走「AI 设置 → 文本分析模型」，
> 填入 base_url / api_key / model 即生效，任何 OpenAI 兼容厂商均可（DeepSeek / 通义 / GLM / OpenAI…）。
> `ANTHROPIC_API_KEY` 仅独立 CLI `comic_drama_pipeline.py` 需要，Web 流程不依赖。

### 3. 访问 Web 界面
打开 **http://127.0.0.1:5211**（端口由 `APP_PORT` 决定，默认 5211；桌面版由外壳自动注入）

> 桌面版（Electron）会自动拉起后端并在退出时回收，无需手动执行第 2 步。

### 4. 操作流程
1. 输入故事主题 → 点击"生成剧本"（云端 LLM）
2. 点击"生成角色资产" → 自动生成角色多视图
3. 点击"生成物品资产" → 自动生成物品 3D 多视角
4. 点击"生成场景资产" → 自动生成场景 3D 多视角
5. 点击"开始生成视频" → H3 批量生成视频片段
6. 点击"生成最终视频" → FFmpeg 合并 + 字幕

## 📦 环境要求

| 项目 | 要求 |
|------|------|
| GPU | **8GB 显存可跑通全流程**（本机实测 RTX 5060 Laptop 8G / sm_120 Blackwell）；
|     | 超分另需余量，8G 下建议关闭（见下）。推荐 16GB+ 以获得可用速度 |
| 内存 | 32GB+ |
| 硬盘 | SSD 200GB+（H3 权重近 20GB，QwenImage2.1 与 FlashVSR 另计） |
| ComfyUI | ≥ 0.38.0（**用户自启动**；本机实测 0.38.0 + torch 2.14.0+cu132） |
| Python | 3.10+（本机 3.13） |
| FFmpeg | 已安装并加入 PATH |

> **8G 显存的两条实测经验**：
> 1. **H3 视频可用**：走 tiny 档 + `--lowvram --reserve-vram 1`，19.9GB 的 H3 以分块换入方式跑得动；
>    整集按场提交正是为了控制单次输入规模。
> 2. **FlashVSR 超分不可用**：960×544 的 46 秒单场会被切成 15 个 spatial tile、每块约 24 分钟，
>    单场 1 小时超时上限直接击穿并回落整集（更慢）。故 8G 机器建议 `enable_upscale=false`，
>    成片直接用 H3 原分辨率；换到更大显存后再开启。
| FFmpeg | 已安装并加入 PATH |
| 文本分析模型 Key（OpenAI 兼容·任意厂商） | 必需（剧本生成，在「AI 设置」里配置） |

> **关于密钥**：Web 端主流程（剧本生成 / 质检 / 对话）统一走「AI 设置 → 文本分析模型」
> 的密钥，**任何支持 OpenAI 兼容调用的模型均可**（DeepSeek / 通义 / GLM / OpenAI 等），
> 在「AI 设置」里填 base_url / api_key / model 后保存即生效。
> **Claude（Anthropic）Key 仅在独立 CLI `comic_drama_pipeline.py` 场景需要**（走
> `ANTHROPIC_API_KEY` 环境变量），Web 主流程不再依赖它。

## 🔧 模型与自定义节点（当前版本）

> 权威清单见 [`docs/依赖清单.md`](docs/依赖清单.md)；**检测状态用 `GET /api/deps/check` 一键查**（下方只是参考快照，以接口返回为准）。

### 模型（ComfyUI `models/` 下）
```
models/diffusion_models/
├── minimaxH3Singularity_prunedInt8-d8a59c68df78.safetensors   # H3 UNET（视频）
├── qwen_image_2.1_int8_convrot.safetensors                    # QwenImage2.1 UNET（图片）
models/clip/
├── minimax-h3/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors    # H3 CLIP
├── qwen3vl_8b_int8_convrot.safetensors                        # QwenImage2.1 CLIP
models/loras/
├── minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors # H3 4 步加速 LoRA
models/vae/
├── minimax-h3/minimax_h3_audio_vae_fp32.safetensors           # H3 音频 VAE（⚠ 不能接 TRT 视频 VAE）
├── minimax_h3_vae_encoder.engine / minimax_h3_vae_decoder_w4a16_awq.engine  # TRT 视频 VAE（⚠ GPU 架构专用）
├── qwen_image_2.1_vae_bf16.safetensors                        # QwenImage2.1 VAE
models/FlashVSR-v1.1/                                          # 超分（仅 enable_upscale=true 才用到）
```

### 自定义节点（ComfyUI `custom_nodes/`，跑现役链路需 6 个包）
```
custom_nodes/
├── ComfyUI_MiniMaxH3_Director/      # H3 Director 核心（MiniMaxH3Director / Refine / BasicScheduler）
├── ComfyUI-H3VAE_TRT/               # 视频 VAE 走 TRT（MiniMaxH3TRTVAELoader）
├── ComfyUI-NVIDIA-DLSS-Frame-Interpolation/   # 补帧（NvidiaDLSSFrameInterpolation）
├── ComfyUI-KJNodes/                 # SageAttention + EasyCache + XHImagePrecision
├── ComfyUI-SolAttn_triton/          # SolAttn 注意力（morton=False）
└── TE-Speed-MiniMaxH3 / TE-Speed-QwenImage21/  # 视频/图片 TE 加速
（可选：ComfyUI_BFSNodes；legacy 回退才需 Herrgotts-H3-Infinite-Continuation-Suite）
```

## ⚡ 性能优化

| 优化项 | 配置 | 效果 |
|--------|------|------|
| H3 Turbo LoRA | 8步（默认20步） | 速度提升 2-3x |
| Qwen Lightning LoRA | 4步 | 图像生成加速 |
| TE-Speed-MiniMaxH3 | 已启用 | TE 编码加速 45% |
| SageAttention | 已启用 | 注意力计算加速 |
| MiniMaxLowVRAMAttention | head_chunks=4 | 低显存支持 |

## 🔗 相关资源

- [MiniMax H3 本地部署](https://platform.minimaxi.com/docs/guides/local-deploy-h3)
- [FlashVSR](https://github.com/OpenImagingLab/FlashVSR)
- [Qwen-Image-Edit-2511-Multiple-Angles-LoRA](https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA)
- [ComfyUI-qwenmultiangle](https://github.com/jtydhr88/ComfyUI-qwenmultiangle)

---



## 📦 打包与分发（新增）

本项目提供两种桌面打包方式，适配不同场景：

### 方式一：PyInstaller 单文件（推荐用于纯后端服务 + 浏览器访问）

⚠️ **必须在纯 ASCII 路径下构建并运行**（中文路径会导致 PyInstaller 6.x 单文件 bootloader 报错 "Could not create temporary directory!"）

```bash
# 1. 准备纯 ASCII 构建目录（示例）
D:\build\mjscxt_src
# 2. 复制源码到纯 ASCII 目录
xcopy /E /I "C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统" D:\build\mjscxt_src\

# 3. 进入构建目录
cd D:\build\mjscxt_src

# 4. 安装依赖
pip install -r requirements.txt
pip install pyinstaller

# 5. 执行打包（自动部署到 ~/mjscxt_desktop/msjcxt.exe）
python build_exe.py

# 6. 产物位置
# dist/漫剧工坊.exe          (中文路径，仅供备用，双击会崩)
# ~/mjscxt_desktop/msjcxt.exe  (纯 ASCII，可直接双击运行)
# ~/mjscxt_desktop/datadir.txt  (指向数据根，需指向纯 ASCII 路径，如 D:\mjscxt_data)
```

**运行要求**：
- 双击 `msjcxt.exe`，后端自动起在 `http://127.0.0.1:5211`
- 浏览器手动打开或脚本自动拉起（策略允许时）
- 数据根建议设为纯 ASCII（如 `D:\mjscxt_data`），`datadir.txt` 写入该路径

---

### 方式二：Electron + electron-builder（推荐用于自带窗口的桌面应用）

⚠️ **构建目录、输出目录、Electron 资源目录必须全程纯 ASCII**，否则会出现：
- `registration_protocol_win.cc(108) CreateFile: 系统找不到指定的文件`
- 单实例锁冲突（`singleInstanceLock=false` 直接退出）
- 自解压临时目录创建失败

```bash
# 1. 准备纯 ASCII 构建目录（示例：D:\build\mjscxt\）
mkdir D:\build\mjscxt

# 2. 复制 electron-app 到纯 ASCII 目录
xcopy /E /I "C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\electron-app" D:\build\mjscxt\electron-app\

# 3. 进入构建目录
cd D:\build\mjscxt\electron-app

# 4. 安装依赖
npm install

# 5. 关键：确认 main.js 端口为 5211（与后端 APP_PORT 一致）
#    const DEFAULT_PORT = 5211;

# 6. 执行打包
npm run build:win

# 7. 产物位置（均在 D:\build\mjscxt\dist\）
# comic-drama-forge-Setup-1.4.2.exe        # NSIS 安装包 (~133 MB)
# comic-drama-forge-Portable-1.4.2.exe     # 单文件便携版 (~132 MB，双击即用)
# win-unpacked/漫剧工坊.exe                # 解压版绿色启动最快 (~180 MB)
```

**运行要求**：
- 首次运行前建议清理残留锁：`Remove-Item -Recurse -Force "$env:APPDATA\mjscxt-desktop"`
- 双击 `comic-drama-forge-Portable-1.4.2.exe` 或 `win-unpacked/漫剧工坊.exe`
- 自动检测 5211 端口：若已有后端则复用，否则自动启动内置 Python 后端
- 主窗口加载 `http://127.0.0.1:5211`，无需浏览器

---

### 两种方式对比

| 特性 | PyInstaller (msjcxt.exe) | Electron (便携版/安装版) |
|------|--------------------------|---------------------------|
| **前端形态** | 浏览器访问 5211 端口 | 自带 Chromium 窗口，无需浏览器 |
| **体积** | ~52 MB | ~132 MB (便携) / ~180 MB (解压版) |
| **启动速度** | ~5 秒 | ~30 秒 (首次自解压/播种资源镜像) |
| **后端复用** | 固定 5211，单实例 | 复用 5211 或自启内置 Python |
| **适用场景** | 服务器/无人值守/远程桌面 | 本地桌面交互、演示、离线使用 |
| **更新机制** | 手动替换 EXE | 内置自动更新（资源增量/整包） |
| **路径要求** | 纯 ASCII 部署目录 + datadir.txt | 纯 ASCII 构建目录 + 输出目录 |

---

### GitHub Release 发布流程

```bash
# 1. 完成上述两种打包
# 2. 创建 tag 并推送
git tag v1.4.2
git push origin v1.4.2

# 3. GitHub Actions 自动运行 .github/workflows/desktop-release.yml
#    - 烘焙 Python + 收集后端资源
#    - Electron 打包
#    - 创建 Release 并上传 3 个产物
# 4. 本地验证：下载 Release 页面的 Portable exe，双击测试
```

> **注意**：Electron 更新配置在 `electron-app/update-config.js`，需与实际发 Release 的仓库 `owner/repo` 一致，否则客户端更新会静默空转。

---


**版本**: 1.4.2（桌面版） | **日期**: 2026-10-10
