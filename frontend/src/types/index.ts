// ============================================
// API 类型定义（匹配后端实际返回结构）
// ============================================

// --- Projects ---
export interface ProjectConfig {
  duration_per_shot: number;
  episode_duration_sec: number;
  episodes: number;
  fps: number;
  resolution: string;
  shots_per_episode: number;
  style: string;
  target_shots: number;
  voice_map: Record<string, string>;
}

export interface Project {
  config: ProjectConfig;
  created_at: string;
  dir_key: string;
  episode_count: number;
  episode_duration_sec: number;
  from_migration?: boolean;
  has_cover?: boolean;
  id: string;
  name: string;
  note?: string;
  novel_id: string;
  status?: 'running' | 'paused' | 'done' | 'failed';
}

export interface ProjectsResponse {
  success: boolean;
  total: number;
  index_path: string;
  projects: Project[];
}

// --- Novels ---
export interface Novel {
  bound: boolean;
  chapter_count: number;
  char_count: number;
  encoding: string;
  encoding_note?: string;
  ext: string;
  format: string;
  line_count: number;
  name: string;
  novel_id: string;
  project_id: string;
}

export interface NovelsResponse {
  success: boolean;
  count: number;
  all_count: number;
  project_id: string | null;
  novels: Novel[];
  supported_exts: string[];
  total_chars: number;
}

// --- Tasks ---
export interface Task {
  id: string;
  kind: 'keyframe' | 'storyboard' | 'video' | 'voice' | 'assemble' | 'qc';
  label: string;
  project: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  progress: number;
  created_at: string;
  // 审计 P2-40：未开始/未结束时后端为 null —— 旧声明为必填 string，
  // 调用方直接 toLocaleString() 会对 null 抛错
  started_at: string | null;
  finished_at: string | null;
  error: string;
  result_path: string;
  payload: Record<string, unknown>;
}

export interface TasksResponse {
  success: boolean;
  count: number;
  items: Task[];
  queue: Task[];
}

// --- Characters ---
/**
 * 与后端 app/character_manager.py 落盘的结构（characters.json → characters.<id>）对齐：
 *   { id, name, role, description, outfit, status, views, references, created_at, updated_at }
 *
 * ⚠️ 后端**没有 images 字段** —— 图片分别存在 views（五视图）与 references（参考图）里。
 * 这里曾把 images 声明成必填的 string[]，调用方照着类型写 `char.images.length`，
 * 运行时读到 undefined → TypeError → 整页白屏（测试报告 #1）。
 * 教训：类型必须跟后端真实结构一致，可为空的一律标可选，前端渲染再做兜底。
 */
export interface Character {
  id: string;
  name: string;
  /** 后端不做枚举约束（AI 可能产出其它值），别用字面量联合把类型写死 */
  role: string;
  description?: string;
  outfit?: string;
  status?: string;
  /** 五视图：front / three_quarter / side / back / expressions，未生成为 null */
  views?: Record<string, string | null>;
  /** 参考图路径列表（后端实际字段） */
  references?: string[];
  /** @deprecated 后端无此字段，仅为兼容历史前端数据保留；新代码请用 references / views */
  images?: string[];
  /** 仅请求参数使用：POST /api/characters 必带，缺了后端返回 400 */
  project?: string;
  project_id?: string;
  created_at?: string;
  updated_at?: string;
}

// --- Character Outfits（角色服装变体 / 衣柜） ---
/**
 * 服装变体条目：GET /api/assets/character/outfits。
 * 产物目录 output/assets/characters/<项目>/<角色名>/outfits/<outfit_key>/，
 * 内部布局与主设定目录一致（base.png + 切分档位图）。
 */
export interface CharacterOutfit {
  outfit_key: string;
  /** 服装描述（outfit.json 档案优先，回落 meta sidecar 提示词里的服装段） */
  desc: string;
  /** base.png 已就绪且非空 */
  ready: boolean;
  /** 各切分档位是否已生成（与主设定目录同名的 sheet_split 产物） */
  views: { front: boolean; left: boolean; back: boolean; half: boolean };
}

export interface CharacterOutfitsResponse {
  success: boolean;
  /** outfits 目录不存在时为空数组（fail-open） */
  outfits: CharacterOutfit[];
}

export interface CharacterOutfitGenerateResponse {
  task_id: string;
  status: string;
  outfit_key?: string;
  character?: string;
  /** 已存在同 key 且 base.png 就绪时的幂等跳过（此时 task_id 为空串） */
  skipped?: boolean;
  message?: string;
}

// --- 参考音频克隆角色声线 ---
export interface VoiceBankItem {
  character: string;
  /** 参考音频绝对路径 */
  file: string;
  exists: boolean;
  /** 该音频里实际说出的那句话（填了克隆相似度更高） */
  ref_text: string;
  duration_sec?: number | null;
  original_filename?: string;
  updated_at?: string | null;
}

// 前端「角色关系」标签页（需求 R4）已下线，其关系条目类型一并移除；后端 /api/relations/* 保留。

// --- Memory ---
/** 后端 ai_memory 实际写入的类型集合（见 app/ai_memory.py 的 mem_type 说明） */
export type MemoryType = 'lesson' | 'success' | 'insight' | 'pattern' | 'failure' | 'test';

export interface Memory {
  /** 后端主键字段名是 mem_id / mem_type。
   *  此前的类型定义写成了 id / type，页面据此取值恒为 undefined，
   *  于是列表渲染出字面量 `memory.type.undefined`、React key 也为空。 */
  mem_id: string;
  mem_type: MemoryType;
  content: string;
  tags: string[];
  created_at: string;
  confidence?: number;
  source?: string;
  usage_count?: number;
  last_used?: string | null;
  context?: Record<string, unknown>;
  /** 兼容旧字段名（部分接口可能回传） */
  id?: string;
  type?: MemoryType;
}

export interface MemoryStats {
  total: number;
  lessons: number;
  successes: number;
  insights: number;
  /** 质检教训库（生成链路自动学习）条数 —— 与上面的手动记忆是两套数据 */
  promptLessons?: number;
}

/** 质检教训库条目（prompt_memory：质检不达标时自动沉淀，重试前召回改写提示词） */
export interface PromptLesson {
  ts?: string;
  project?: string;
  kind?: string;
  phash?: string;
  prompt?: string;
  issues?: string[];
  reason?: string;
  score?: number | null;
  terms?: string[];
  /** 确定性主键（T01 数据契约）：删除/召回计数回写的锚点，形如 "L"+sha1 前 16 位 */
  lesson_id?: string;
  /** 被生成链路自动引用（召回）的次数；0 = 从未被召回（即「死教训」） */
  use_count?: number;
  /** 最近一次被召回的时间（ISO）；从未被召回为 null */
  last_used?: string | null;
  /** 问题类别（categorize_issue 推断，或 record_with_context 显式传入） */
  category?: string;
  /** 优先级：high / medium / low */
  priority?: string;
  /** 来源上下文（record_with_context 传入的键值，如 project_name/style/episode_no） */
  context?: Record<string, unknown>;
}

/** 教训库查询参数（GET /api/memory/lessons 的 query 契约，见设计文档 §3.4） */
export interface LessonQuery {
  /** 环节多值，逗号分隔（如 "asset,storyboard"）；缺省 = 全部 */
  kind?: string;
  project?: string;
  /** 起始时间（ISO，含） */
  since?: string;
  /** 结束时间（ISO，含） */
  until?: string;
  /** 关键词检索（走 q；⚠️ 不复用 /memory/lessons/search —— 那是「按 prompt 召回试算」） */
  q?: string;
  limit?: number;
  offset?: number;
}

/** 教训库分页结果（GET /api/memory/lessons 返回信封体） */
export interface LessonPage {
  /** 全库总数（未过滤前） */
  total: number;
  /** 应用筛选后命中的条数 */
  filtered: number;
  /** 各环节分布（当前 kind 筛选口径） */
  by_kind: Record<string, number>;
  /** 死教训数（use_count === 0 的条数） */
  dead_lessons: number;
  lessons: PromptLesson[];
}

// --- Settings ---
/** ⚠️ 形状按 `/api/ai/settings` 的真实返回校正（项目级「创作设定」，不是系统参数）。
 *  旧版本声明的是 llm_provider / llm_api_key / comfyui_url / tts_provider /
 *  tts_api_key / watermark_enabled / watermark_text —— 这些字段后端**一个都不返回**
 *  （系统参数在 `/api/ai/config`，接口形态完全不同），纯属过时残留。
 *  真实的创作设定键（title/genre/style/...)都在 `settings` 里，随项目变化。 */
export interface AppSettings {
  success?: boolean;
  project_name?: string;
  active?: boolean;
  settings?: Record<string, unknown>;
  settings_file?: string;
  style_brief?: string;
}

// --- i18n ---
export interface I18nData {
  success: boolean;
  lang: string;
  available: string[];
  messages: Record<string, any>;
}

// --- Analytics ---
/** ⚠️ 形状按 `/api/analytics/summary` 的真实返回校正。
 *  旧版本声明的是 total_projects / total_tasks / total_cost / tasks_by_kind /
 *  projects_by_status —— 这些字段后端**一个都不返回**，纯属过时残留。 */
export interface AnalyticsData {
  success?: boolean;
  generated_at?: string;
  project?: string;
  total_seconds?: number;
  total_hours?: number;
  total_units?: number;
  event_count?: number;
  failed_count?: number;
  by_kind?: Record<string, number>;
  cost?: Record<string, unknown>;
  projects?: unknown[];
  recent?: unknown[];
  comfyui?: Record<string, unknown>;
  deploy_profile?: unknown;
}

// --- Keyframes ---
export interface KeyframePlan {
  shot_id: string;
  seq: number;
  has_start: boolean;
  has_end: boolean;
  need_gen: boolean;
  url?: string;
  status?: string;
}

export interface KeyframePlanResponse {
  success: boolean;
  project: string;
  shot_count: number;
  keyframes_dir: string;
  start_frames_ready: number;
  end_frames_ready: number;
  to_generate: number;
  plan: KeyframePlan[];
  // 审计 P2-40（2026-09-29）：后端 api_keyframes_plan 实际返回（链式模式信息）
  chain_mode?: string;
  chained_count?: number;
}

// --- Storyboard ---
// 审计 P2-40：以下可选字段为后端 canvas 卡片（api_storyboard_canvas）实际返回、
// 本类型此前未声明的部分 —— 标可选避免破坏现有渲染，供新代码使用时不再读 any。
export interface StoryboardShot {
  shot_id: string;
  seq: number;
  camera: string;
  duration: string;
  location: string;
  emotion: string;
  description: string;
  dialogue_text: string;
  // A1 拆分字段（剧本归一化产出）
  shot_type?: string;
  camera_motion?: string;
  visual_detail?: string;
  audio_cues?: string;
  /** 台词结构化数组（后端 dialogue 元素含 speaker+text） */
  dialogue?: { speaker?: string; text?: string }[];
  /** 画布排序值（metadata.shot_order 投影） */
  order?: number;
  characters_in_shot?: string[];
  items_in_shot?: string[];
  /** 参考图未命中告警（_note_ref_warning 记账，未命中必带） */
  _ref_warnings?: string[];
  storyboard?: {
    exists: boolean;
    url: string;
    qc?: Record<string, unknown>;
    success?: boolean;
    blocked?: boolean;
    /**
     * 「正在生成中」：正式产物尚未落盘（exists=false），但中间产物
     * （storyboard_scratch/shot_NN_tryK.png）已存在 → 可展示缩略图占位。
     * 与 exists 语义分离：exists 仍只代表正式产物已落盘。
     */
    generating?: boolean;
    /** 生成中缩略图地址（/api/storyboards/scratch/...）；未生成中时为空串 */
    scratch_url?: string;
    /** 该图是否为单镜九宫格（3×3）布局：灯箱叠加 1-9 编号覆盖层的判据 */
    grid?: boolean;
  };
  video?: {
    exists: boolean;
    url: string;
  };
  keyframe?: {
    start: boolean;
    end_exists: boolean;
    end_url: string;
  };
  consistency?: Record<string, unknown>;
  coverage?: Record<string, unknown>;
}

export interface StoryboardCanvasResponse {
  success: boolean;
  project: string;
  episode_no?: number;
  episode_title?: string;
  title?: string;
  summary: {
    shot_count: number;
    storyboard_ready: number;
    video_ready: number;
    keyframe_end_ready: number;
    qc_blocked: number;
  };
  cards: StoryboardShot[];
  shot_order: string[];
}

// --- 分镜九宫格候选构图（2026-10-01，对标 BigBanana：一图 9 候选 → 选格裁切） ---
/**
 * POST /api/storyboard/grid-candidates 的返回（异步发起）：
 * body {project_name, episode_no, shot_id} → 任务写入 generation_state，
 * 进度用 GET /api/generation/status/<task_id> 轮询。
 */
export interface ShotGridStartResponse {
  success: boolean;
  task_id: string;
  status: string;
}

/**
 * generation_state 里九宫格任务的状态字段（app.py `_grid_worker` 写入）：
 * completed 时带 grid_url（= /api/storyboards/file/<项目>/shot_NN_grid.png，3x3 候选图）；
 * failed / cancelled 时带 error 原文。running 中只有 phase / project / shot 等展示字段。
 */
export interface ShotGridTaskState {
  /** running / completed / failed / cancelled（不写死字面量联合：后端可扩展新状态） */
  status: string;
  phase?: string;
  progress?: number;
  project?: string;
  shot?: string | number;
  started_at?: string;
  /** 完成态才有：九宫格候选网格图地址，可直接作为 <img src> */
  grid_url?: string;
  result?: { grid?: string };
  error?: string;
}

/**
 * GET /api/generation/status/<task_id>：历史上存在两种返回形态
 * （{success, task:{…}} 信封 与 状态字段平铺顶层），前端读取时按 `task ?? 顶层` 兼容。
 */
export interface ShotGridStatusResponse {
  success?: boolean;
  task?: ShotGridTaskState;
  [k: string]: unknown;
}

/**
 * POST /api/storyboard/grid-apply 的返回：把选中的格（cell 1-9，行优先）从九宫格图
 * 等分裁切为该镜正式分镜图（人工定稿，不再走 AI 质检；旧图移入回收站可恢复）。
 */
export interface ShotGridApplyResponse {
  success: boolean;
  project: string;
  shot_id: string;
  /** 实际应用的格号（1-9） */
  cell: number;
  /** 裁切产物落盘绝对路径（仅展示用，不要拿去请求） */
  applied: string;
  /** 新分镜图访问地址（/api/storyboards/file/<项目>/shot_NN.png） */
  url: string;
  /** 被移入回收站的旧产物清单 */
  cleared?: Array<{ category?: string; path?: string }>;
  hint?: string;
  error?: string;
}

// --- Video 批量重生成（2026-10-02） ---
/** POST /api/video/retry-shots-batch 逐镜结果：单镜失败不拖垮整批 */
export interface VideoRetryBatchItem {
  shot_id: string;
  success: boolean;
  path?: string;
  /** 可直接播放/下载的地址 */
  url?: string;
  error?: string;
}

/** POST /api/video/retry-shots-batch 整包返回（同步端点，全部镜头跑完才返回） */
export interface VideoRetryBatchResponse {
  success: boolean;
  /** 本次提交的镜头数 */
  total: number;
  /** 成功数 */
  ok_count: number;
  results: VideoRetryBatchItem[];
  /** 整包失败原因（如超过单次 12 个上限） */
  error?: string;
}

// --- TTS ---
// TTSPlanLine / TTSPlanResponse / TTSTask（旧「TTS 配音任务」链路）已随后端
// /api/tts/{plan,generate,status,tasks,list} 一同下线移除；仅保留环境自检类型。
export interface TTSEnv {
  available: boolean;
  reasons?: string[];
  voices?: string[];
  comfyui_online?: boolean;
  dub_dir?: string;
}

// --- Mix ---
// MixEnv / MixPlanResponse / MixTask / MixStatusResponse（旧「音画混音」任务链）
// 已随后端 /api/mix/* 全部路由一同下线移除。

// --- QC ---
export interface QCConfig {
  enabled: boolean;
  max_retries: number;
  /** best-of-N 分镜候选数：1=关闭（默认），>1 时每镜生成 N 张候选按质检分选最佳 */
  best_of?: number;
  threshold: number;
  endpoint?: string;
}

export interface QCHistoryItem {
  shot_id: string;
  seq: number;
  score: number;
  verdict: 'pass' | 'fail' | 'retry';
  timestamp: string;
  error?: string;
}

export interface QCResponse {
  success: boolean;
  config: QCConfig;
  history: QCHistoryItem[];
  stats?: {
    total: number;
    passed: number;
    failed: number;
    retry_count: number;
  };
}

// --- Episodes ---
// ⚠️ 形状按 /api/episodes/<novel_id>（列表）真实返回校正（app.py api_list_episodes）：
// 每集行 = novel_to_script.list_episodes 的字段 + _episode_progress 推导的 status/completed_shots，
// 没有 created_at/updated_at，剧本正文不在列表行里。
export interface Episode {
  episode_no: number;
  title?: string;
  episode_title?: string;
  chapter_index?: number;
  status: 'pending' | 'producing' | 'done' | 'failed';
  shot_count: number;
  completed_shots: number;
  generated_at?: string;
  coverage_percent?: number | null;
  [k: string]: unknown;
}

/** 单集详情（/api/episodes/<novel_id>/<ep>）：剧本正文在 `script` 嵌套对象下，
 *  顶层没有 shots / script_content。 */
export type EpisodeDetail = Episode & {
  script?: {
    episode_no?: number;
    title?: string;
    episode_title?: string;
    shots?: any[];
    shot_count?: number;
    characters?: any[];
    items?: any[];
    scenes?: any[];
    [k: string]: unknown;
  };
  episode_title?: string;
  chapter_index?: number;
  stats?: { shot_count?: number; [k: string]: unknown };
}

export interface EpisodeListResponse {
  success: boolean;
  novel_id: string;
  episodes: Episode[];
  total: number;
}

/** P2-2 分集断点提议：与生产 autopilot.episode_units 同源（默认不带参数时完全一致）。
 *  来自 GET /api/novels/<id>/split-plan。 */
export interface NovelSplitPlanUnit {
  part: number;
  start: number;
  end: number;
  char_count: number;
  preview?: string;
  est_shots?: number;
  est_sec?: number;
  over_redline?: boolean;
  [k: string]: unknown;
}

export interface NovelSplitPlanChapter {
  index?: number;
  title?: string;
  char_count?: number;
  needs_confirm: boolean;
  total_parts: number;
  units: NovelSplitPlanUnit[];
  message?: string;
  [k: string]: unknown;
}

export interface NovelSplitPlanResponse {
  success: boolean;
  novel_id: string;
  chapter_count?: number;
  total_episodes?: number;
  needs_confirm_chapters?: number[];
  episodes_per_chapter?: Array<{ chapter_index?: number; title?: string; total_parts: number }>;
  params?: { max_sec?: number | null; max_shots?: number | null; fixed_parts?: number | null };
  chapters: NovelSplitPlanChapter[];
  [k: string]: unknown;
}

// --- ComfyUI 模型 / 插件扫描（手选生成模型） ---

export interface ComfyUIModelSlot {
  /** 槽位 key（如 unet_main），面向用户职责而非模板节点 id */
  key: string;
  label: string;
  hint: string;
  node_type: string;
  field: string;
  /** ComfyUI object_info 给出的**权威合法值**（含目录前缀） */
  values: string[];
  /** 当前已选；空串表示未覆盖，沿用工作流模板原值 */
  selected: string;
  /** 已选值是否仍在合法值列表内 —— 丢弃头工艺检测失效 hand-pick */
  selected_valid: boolean;
  available: boolean;
}

export interface ComfyUIPlugin {
  id: string;
  node_count: number;
  nodes: string[];
}

export interface ComfyUIModelsResponse {
  success: boolean;
  scanned_at?: string;
  slots: ComfyUIModelSlot[];
  plugins: ComfyUIPlugin[];
  core_node_count?: number;
  node_type_count?: number;
  elapsed_sec?: number;
  error?: string;
}

// --- 后台服务日志（只读查看） ---

export interface LogSource {
  key: string;
  label: string;
  desc: string;
  size: number;
  modified_at: string;
  exists: boolean;
}

export interface LogsResponse {
  success: boolean;
  source?: string;
  path?: string;
  size?: number;
  modified_at?: string;
  /** 读到该处的文件字节偏移；下次拿它作 since 即可只取新增内容 */
  offset?: number;
  encoding?: string;
  truncated?: boolean;
  lines: string[];
  counts?: Record<string, number>;
  error?: string;
  available?: string[];
}

// --- Autopilot ---
/** 当前正在生产的一集（`current`）的字段，对应后端 `_set_current` 写入 + status() 增强。
 *  step 是英文技术标识符（script/assets/storyboard/...），前端用 i18n 映射成展示名。 */
export interface AutopilotCurrent {
  project?: string;
  episode?: number;
  title?: string;
  step?: string;
  message?: string;
  percent?: number;
  phase?: string;
  steps_done?: string[];
  retries?: number;
  started_at?: string;
  /** epoch 秒：最近一次进度推进时刻（后端 _set_current 自动写） */
  step_updated_at?: number;
  /** 当前步骤停滞时长（秒），status() 动态计算 */
  step_stalled_sec?: number;
  /** 停滞超阈值时的告警文案（空串=正常） */
  stall_warning?: string;
}

/** ⚠️ 形状按 `/api/autopilot/status` 的真实返回校正。
 *  旧版本声明的 `current_project` / `current_episode` / `totals{episodes_done,
 *  episodes_failed,retries}` 后端**均不返回**（真实是 `current` 对象 + `project`
 *  + `curve/cycle/delivered_total/enabled_count/exceptions/...`）。 */
export interface AutopilotStatus {
  running: boolean;
  paused: boolean;
  pause_reason?: string;
  project?: string;
  current?: AutopilotCurrent | null;
  curve?: unknown;
  cycle?: unknown;
  delivered_total?: number;
  enabled_count?: number;
  plan_count?: number;
  pending_review?: number;
  exceptions?: unknown;
  last_error?: string;
  checked_at?: string;
  /** 2026-10-08：单集失败自动暂停（stop_on_failure）详情 —— 非空 = 因失败被自动暂停 */
  failure_pause?: {
    project?: string;
    episode?: number;
    reason?: string;
    error?: string;
    at?: string;
  } | null;
  /** 需人工介入（failure_pause 非空 或 未处理异常 > 0） */
  needs_user?: boolean;
}

export interface AutopilotProgress {
  project: string;
  total_episodes: number;
  done: number;
  failed: number;
  current_episode?: number;
  exceptions?: Array<{ episode: number; error: string }>;
}

// --- Deliverables（成品验收） ---
/**
 * 成片清单条目，对应 pipeline.list_deliverables() 的返回。
 * 真实落盘结构（output/autopilot/<项目>/deliverables.json）里是：
 *   { project, episode_no, path, filename, size, meta:{title,...} }
 * 后端另外**计算**出两个字段：
 *   exists（文件是否真在磁盘上）
 *   url（/api/autopilot/deliverable/file/<项目>/<文件名>，已防目录穿越）
 * 播放直接用 url；下载用 url + '?download=1'。**不要自己拼文件名猜路径**。
 */
export interface Deliverable {
  project: string;
  episode_no: number;
  filename: string;
  /** 落盘绝对路径（仅展示用，不要拿去请求） */
  path?: string;
  /** 字节数 */
  size?: number;
  /** 元信息，含 title（章节标题）等 */
  meta?: {
    title?: string;
    chapter_index?: number;
    elapsed_sec?: number;
    retries?: number;
    /** 成片来源：final_video（合并）/ mix（带配音混音）/ probe 等 */
    source?: string;
    /** 成片时长（秒） */
    duration_sec?: number | null;
    /** 剧本镜头总数 / 已发现镜头视频数（后端自动登记时写入） */
    shots_total?: number;
    shots_ready?: number;
    /** 镜头数不齐时后端打的标记：true 表示成片可能不完整 */
    incomplete_shots?: boolean;
    /** 配合 incomplete_shots 的提示文案 */
    warning?: string;
    /** 成片登记后被标记为「已过期」（同集镜头重做过，成片需重新合成） */
    stale?: {
      reason?: string;
      marked_at?: string;
      detail?: { shot_id?: string | number; seq?: number; mode?: string; video?: string };
    };
  };
  /** 验收状态：pending 待验收 / accepted 已验收 / rejected 已打回 */
  review?: 'pending' | 'accepted' | 'rejected';
  /** ⚠️ 后端字段名是 `review_note`，不是 `note`（见 pipeline.set_deliverable_review） */
  review_note?: string;
  /** 最近一次验收/打回的时间 */
  reviewed_at?: string;
  created_at?: string;
  updated_at?: string;
  /** 后端计算：文件是否真实存在 */
  exists?: boolean;
  /** 后端计算：播放/下载地址 */
  url?: string;
}

export interface DeliverablesResponse {
  success: boolean;
  count: number;
  /** 待验收条数 */
  pending: number;
  items: Deliverable[];
}

// --- Providers ---
/** ⚠️ 形状按 `/api/providers` 的真实返回校正。
 *  旧版本声明的 `providers: Provider[]` 与 `is_default` 后端**都不返回**
 *  —— 真实是 `kinds` / `env_keys`（按 image / tts / video 分组的可用性视图）。 */
export interface Provider {
  id: string;
  name: string;
  model: string;
  base_url: string;
}

export interface ProvidersResponse {
  success: boolean;
  /** 各能力是否已有可用 provider（key 为 image / tts / video） */
  kinds?: Record<string, unknown>;
  /** 各能力依赖的环境变量名（key 为 image / tts / video） */
  env_keys?: Record<string, string[]>;
}

// --- AI Config (Unified: text / qc / chat) ---
export interface AIFallbackModel {
  base_url: string;
  model: string;
  label?: string;
  reasoning_effort?: string;
  api_key?: string;        // 编辑中（未保存）
  api_key_masked?: string; // 已保存（脱敏）
  has_api_key?: boolean;
  index?: number;
}

export interface AIConfigModule {
  base_url: string;
  api_key: string;    // masked on read
  model: string;
  /** 思考档位：'' = 不注入（服务端默认），其余为 low / high / max。思考不可关闭的模型用 */
  reasoning_effort?: string;
  /** 备用模型故障转移链（顺序即优先级），主模型挂 3 次自动切下一个 */
  fallbacks?: AIFallbackModel[];
  updated_at: string | null;
  has_api_key?: boolean;  // whether a real key is stored
  key?: string;       // module key
  label?: string;
  desc?: string;
  need_vision?: boolean;
  placeholder_model?: string;
  used_by?: string[];
}

export interface AIConfigResponse {
  success: boolean;
  // 后端把三个模块放在 config.modules 下（不是平铺），模块内 api_key 一律脱敏为 api_key_masked
  config: {
    modules: Record<string, AIConfigModule>;
    module_order?: string[];
    modules_meta?: Record<string, {
      label: string;
      desc: string;
      need_vision: boolean;
      placeholder_model: string;
      used_by: string[];
    }>;
    config_path?: string;
    legacy_path?: string;
    updated_at?: string;
    migrated_from?: string;
    /** 思考档位可选项（含开头的空串，表示「不注入」） */
    reasoning_effort_options?: string[];
    /** ComfyUI 地址的实际来源（环境变量），因此只能只读展示 */
    comfyui?: {
      url: string;
      source: string;
      editable: boolean;
    };
  };
}

export interface AITestResult {
  success: boolean;
  module: string;
  probe: string;
  model?: string;
  base_url?: string;
  chat_url?: string;
  /** ⚠️ 后端真实字段是 latency_ms；response_time_ms 仅为兼容旧值保留 */
  latency_ms?: number;
  response_time_ms?: number;
  error?: string;
  guide?: string;
  /**
   * 后端给出的人类可读结论：
   * - `ok`：链路通且拿到了正文
   * - `reachable_but_no_content`：链路通，但模型这次没输出正文
   *   （允许思考时额度被思考吃掉）——**不要当成配置错误**
   * - `failed`：确实连不上 / 鉴权失败
   */
  verdict?: 'ok' | 'reachable_but_no_content' | 'failed';
  /** 模型回复片段（用于人工确认返回的确实是模型内容） */
  reply?: string;
  /** 配合非 ok 结论的处置建议 */
  hint?: string;
  /** 结束原因（length 表示被截断） */
  finish_reason?: string;
  truncated?: boolean;
  /** 该次探测实际使用的 max_tokens */
  max_tokens?: number;
  disable_thinking?: boolean;
  /** 视觉探测：true 支持 / false 不支持 / null 未确认（无正文） */
  vision?: boolean | null;
  /** 视觉探测未确认时为 true */
  uncertain?: boolean;
  attempts?: number;
  retries_used?: number;
}

// ========== 超分（FlashVSR） ==========
// 后端能力早已完整实现（app/upscale_client.py + 8 个 /api/upscale/* 端点），
// 但此前没有任何界面入口。以下类型对齐 app.py `api_upscale_*` 的真实返回。

/** GET /api/upscale/env —— 链路自检 */
export interface UpscaleEnv {
  available: boolean;
  /** ComfyUI 是否在线 */
  comfy_online: boolean;
  comfy_version?: string;
  comfy_url?: string;
  /** FlashVSR 模型文件是否齐备 */
  model_ready: boolean;
  model_dir?: string;
  model_files?: { name: string; exists: boolean; size_mb: number }[];
  /** TE-Speed 加速链路是否可用 */
  te_ready: boolean;
  /** 旧 FlashVSR 链路是否可用 */
  legacy_ready: boolean;
  te_nodes?: Record<string, boolean>;
  nodes?: Record<string, boolean>;
  /** 不可用原因（给人看的中文说明） */
  reasons: string[];
  default_engine?: string;
  te_defaults?: Record<string, unknown>;
  defaults?: Record<string, unknown>;
}

/** GET /api/upscale/sources —— 可作为超分输入的候选视频 */
export interface UpscaleSource {
  /** 分类标签：成片 / 视频片段 / 超分产物 / ComfyUI/xxx */
  kind: string;
  name: string;
  path: string;
  url: string;
  size_mb: number;
  mtime: string;
}

/** POST /api/upscale/video 的返回 */
export interface UpscaleSubmitResponse {
  success: boolean;
  task_id: string;
  input_path: string;
  scale: number;
  engine: string;
  error?: string;
}

/** GET /api/upscale/status/<task_id> —— 任务状态（pending/running/done/error） */
export interface UpscaleTask {
  task_id: string;
  status: 'pending' | 'running' | 'done' | 'error';
  progress: number;
  message: string;
  project_name?: string;
  input_path?: string;
  scale?: number;
  engine?: string;
  error?: string;
  result?: {
    output_path?: string;
    output_url?: string;
    input_url?: string;
    output_filename?: string;
    engine?: string;
    accelerated?: boolean;
    elapsed_sec?: number;
    size_delta_mb?: number;
    before?: { width?: number; height?: number; duration?: number; size_mb?: number; has_audio?: boolean };
    after?: { width?: number; height?: number; duration?: number; size_mb?: number; has_audio?: boolean };
  };
}

/** GET /api/upscale/list —— 已生成的超分产物 */
export interface UpscaleArtifact {
  name: string;
  path: string;
  url: string;
  size_mb: number;
  mtime: string;
}

// ===================== 总控 AI 自主执行（agent） =====================

/** 一次工具调用的执行记录 */
export interface AgentStep {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  cached?: boolean;
  blocked?: boolean;
  elapsed_sec?: number;
  summary: string;
  result?: string;
}

/** GET /api/agent/job/<id> —— 总控任务状态 */
export interface AgentJob {
  id: string;
  project: string;
  message: string;
  status: 'running' | 'done' | 'failed' | 'killed' | 'timeout';
  steps: AgentStep[];
  reply: string;
  error: string;
  created: number;
  updated: number;
  expensive_used: number;
}

/** GET /api/agent/tools —— 工具清单 */
export interface AgentTool {
  name: string;
  description: string;
  risk: 'safe' | 'write' | 'expensive';
  expensive: boolean;
}

export interface AgentGuards {
  max_steps: number;
  max_expensive: number;
  max_turn_sec: number;
  cooldown_sec: number;
}

export interface AgentKillState {
  on: boolean;
  reason: string;
  at: number;
}
