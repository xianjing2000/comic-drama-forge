// ============================================
// API 客户端（TypeScript）
// ============================================
import type {
  Project, ProjectsResponse,
  Novel, NovelsResponse,
  Task, TasksResponse,
  Character,
  Memory, MemoryStats, PromptLesson, LessonQuery, LessonPage,
  AppSettings, I18nData,
  AnalyticsData,
  KeyframePlanResponse,
  StoryboardCanvasResponse,
  StoryboardShot,
  TTSEnv,
  QCConfig, QCResponse,
  Episode, EpisodeListResponse, NovelSplitPlanResponse, ComfyUIModelsResponse,
  LogSource, LogsResponse,
  AutopilotStatus, AutopilotProgress,
  Deliverable, DeliverablesResponse,
  Provider, ProvidersResponse,
  AIConfigResponse, AITestResult,
  UpscaleEnv, UpscaleSource, UpscaleSubmitResponse, UpscaleTask, UpscaleArtifact,
  AgentJob, AgentTool, AgentGuards, AgentKillState,
  CharacterOutfitsResponse, CharacterOutfitGenerateResponse,
  VoiceBankItem,
  VideoRetryBatchResponse,
  ShotGridStartResponse,
  ShotGridApplyResponse,
} from '../types';

const API_BASE = '/api';

// 后端统一返回 {success, error?, message?}。此前 request() 直接抛
// `HTTP 500: Internal Server Error`，把后端精心脱敏过的中文错误丢掉了，
// 界面上只能看到无信息的英文报错。这里优先取后端的可读文案。
async function readError(response: Response): Promise<string> {
  let detail = '';
  try {
    const data = await response.clone().json();
    const raw = data?.error || data?.message || data?.detail;
    if (typeof raw === 'string' && raw.trim()) detail = raw.trim();
    else if (raw) detail = JSON.stringify(raw);
    // 后端常带 `hint`（例如「这集可能是兜底生成的，没有台词」）或 `guide`
    // （例如视觉模型不适配的替代建议）。这些是给用户看的处置办法，
    // 只把 error 抛出去会让用户看到问题却不知道怎么办。
    const extra = data?.hint || data?.guide || data?.layout_hint;
    if (typeof extra === 'string' && extra.trim()) {
      detail = detail ? `${detail}（${extra.trim()}）` : extra.trim();
    }
  } catch {
    try {
      const text = (await response.text()).trim();
      if (text) detail = text;
    } catch {
      /* 响应体不可读，退化为状态码 */
    }
  }
  const status = `HTTP ${response.status}`;
  return detail ? `${detail}` : `${status} ${response.statusText || ''}`.trim();
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  // FormData 必须让浏览器自行生成 multipart boundary —— 一旦手工带上
  // Content-Type: application/json，boundary 就没了，后端 request.files 收到空列表。
  const isForm = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: isForm
      ? { ...options.headers }
      : { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json() as Promise<T>;
}

// --- Projects ---
export const projectsApi = {
  list: () => request<ProjectsResponse>('/projects'),
  get: (id: string) => request<{ success: boolean; project: Project }>(`/projects/${id}`).then(d => d.project),
  // 后端实际返回 {success, project, created}，此前类型写成 Project 会误导调用方
  create: (data: Partial<Project>) => request<{ success: boolean; project: Project; created?: boolean }>('/projects', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  // 删除统一走 deleteV2（POST /projects/<id>/delete）；旧 delete（DELETE /projects/<id>）
  // 后端该路由仅支持 GET、无对应删除端点且前端零调用，已作为死方法移除。
  deleteV2: (id: string, confirm?: boolean) =>
    request<{ success: boolean }>(`/projects/${id}/delete`, {
      method: 'POST',
      body: JSON.stringify({ confirm: confirm ?? true }),
    }),
  rename: (id: string, name: string) =>
    request<{ success: boolean; project: Project; note?: string }>(`/projects/${id}/rename`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  /**
   * 项目配置（含 **video_mode 视频生成方式** —— 新建项目时用户选择的那一项）。
   * 「生成视频」按它决定 episode（整集一次生成）/ per_shot（逐镜）/ keyframe（首尾帧）。
   */
  getConfig: (id: string) =>
    request<{ success: boolean; project: string; config: Record<string, any>; config_path?: string }>(
      `/projects/${id}/config`),
  /** 局部更新项目配置（后端会归一 video_mode，非法值回落默认，不会写脏值） */
  updateConfig: (id: string, patch: Record<string, any>) =>
    request<{ success: boolean; project: string; config: Record<string, any> }>(
      `/projects/${id}/config`, { method: 'POST', body: JSON.stringify(patch) }),
  coverUrl: (id: string) => `${API_BASE}/projects/${id}/cover`,
  generateCover: (id: string, seed?: number) =>
    request<{ success: boolean; cover_path: string; cover_url: string }>(`/projects/${id}/cover/generate`, {
      method: 'POST',
      body: JSON.stringify(seed != null ? { seed } : {}),
    }),
};

// --- Novels ---
export interface NovelUploadItem {
  filename: string;
  success: boolean;
  error?: string;
  novel?: Novel & { title?: string };
  project_id?: string;
  project_key?: string;
}
export interface NovelUploadResponse {
  success: boolean;
  uploaded: number;
  failed: number;
  results: NovelUploadItem[];
  supported_exts: string[];
}

export const novelsApi = {
  list: (projectId?: string) => {
    // 后端只认 project_id / project_name；原来的 ?project= 会被静默忽略，
    // 导致「按项目筛选小说」实际返回全量。
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return request<NovelsResponse>(`/novels${query}`);
  },
  /** 上传小说文件（后端路由是 /novels/upload，且要求 multipart 的 file 字段） */
  upload: async (
    file: File,
    opts: { projectId?: string; autoProject?: boolean } = {}
  ): Promise<NovelUploadResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    if (opts.projectId) formData.append('project_id', opts.projectId);
    if (opts.autoProject === false) formData.append('auto_project', '0');
    const response = await fetch(`${API_BASE}/novels/upload`, {
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    return response.json() as Promise<NovelUploadResponse>;
  },
};

// --- Tasks ---
export const tasksApi = {
  list: () => request<TasksResponse>('/tasks'),
  // 审计 P2：后端 api_task_detail 返回 { success, task, ... } 信封 —— 解包出 task，
  // 否则调用方拿 task.progress 等字段全是 undefined
  get: async (id: string): Promise<Task> => {
    const d = await request<{ success: boolean; task: Task }>(`/tasks/${id}`);
    return d?.task;
  },
};

// --- Characters ---
/**
 * 后端资产接口的返回形态并不统一：
 *  - /api/characters → { success, characters: { "<id>": {...} } }   （**字典**）
 *  - /api/items      → { success, items: { ... } } / { items: [ ... ] }
 *  - /api/scenes     → 同上
 * 早先 client.ts 把 list() 的返回类型直接声明成 Character[]，与运行时不符，
 * 调用方只能自己 `Object.values(res.characters)`。一旦后端少给一层字段，
 * `Object.values(undefined)` 就会抛错并导致整页白屏（测试报告 #1）。
 * 这里在 API 层统一归一化成数组，且对任何异常形态都退化为空数组，不再抛错。
 */
function toAssetArray<T>(payload: unknown, ...keys: string[]): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (!payload || typeof payload !== 'object') return [];
  const obj = payload as Record<string, unknown>;
  let inner: unknown = obj;
  for (const k of keys) {
    if (obj[k] !== undefined) { inner = obj[k]; break; }
  }
  if (Array.isArray(inner)) return inner as T[];
  if (inner && typeof inner === 'object') return Object.values(inner) as T[];
  return [];
}

export const charactersApi = {
  /** 后端返回字典，这里统一解包成数组（见 toAssetArray 说明） */
  list: async (projectId: string): Promise<Character[]> =>
    toAssetArray<Character>(
      await request<unknown>(`/characters?project=${encodeURIComponent(projectId)}`),
      'characters'
    ),
  // add/update（POST /characters、PUT /characters/<id>）前端零调用且无页面入口，
  // 已作为死方法移除；角色档案由剧本生成链路维护。
  // 旧 delete（DELETE /characters/<id>）后端无对应删除端点（单资源仅 PUT）
  // 且前端零调用，已作为死方法移除；角色删除如需支持应走后端新增端点。
};

// 前端「角色关系」标签页已按需求 R4 下线，此处的关系查询接口一并移除
// （后端 /api/relations/* 保留不动）。关系数据由剧本生成链路维护。

// --- AI Chat ---
export const chatApi = {
  send: (message: string, project?: string) =>
    request<{ success: boolean; reply: string; settings?: Record<string, unknown> }>(
      '/ai/chat',
      { method: 'POST', body: JSON.stringify({ message, project_name: project }) }
    ),
  /** 读取项目历史对话。
   *  后端 /ai/chat/history 实际返回 { success, state: { messages } }（state 里还有 draft/settings/fields，
   *  前端只用 messages）。旧实现按顶层 { messages } 取，d.messages 恒为 undefined → 重进项目历史永远为空，
   *  这里归一化回 { success, messages }，并把持久化消息的 `time` 字段对齐到前端渲染用的 `timestamp`。 */
  history: async (project?: string) => {
    const d = await request<{
      success: boolean;
      state?: {
        messages?: Array<{ role: string; content: string; time?: string; timestamp?: string; msg_id?: string }>;
      };
    }>(
      `/ai/chat/history${project ? `?project=${encodeURIComponent(project)}` : ''}`
    );
    const raw = d.state?.messages || [];
    return {
      success: d.success,
      messages: raw.map((m) => ({
        role: m.role,
        content: m.content,
        timestamp: m.time || m.timestamp || '',
      })),
    };
  },
  clearHistory: (project?: string) =>
    request<void>('/ai/chat/clear', {
      method: 'POST',
      body: JSON.stringify({ project: project || '' }),
    }),
  // applySettings（POST /ai/chat/apply）前端零调用 —— 「应用设定」由总控 AI 在对话
  // 链路内自动触发，无手动入口，已作为死方法移除。
};

// --- 总控 AI 自主执行（function-calling agent） ---
//
// 与 chatApi 的区别：chatApi 只聊天 + 抽取创作设定，不执行任何动作；
// agentApi 会把指令交给模型，由模型自己决定调哪些工具、直接把活干完。
// 安全靠后端护栏（工具白名单 / 昂贵动作配额 / 冷却 / 急停），不需要人工确认。
export const agentApi = {
  send: (message: string, project?: string) =>
    request<{ success: boolean; job_id: string; project: string }>('/agent/chat', {
      method: 'POST',
      // 后端 _chat_project 只认 project_name，这里必须用它，否则会落到「上一个活跃项目」
      body: JSON.stringify({ message, project_name: project || '' }),
    }),
  job: (jobId: string) => request<{ success: boolean } & AgentJob>(`/agent/job/${jobId}`),
  tools: () =>
    request<{
      success: boolean;
      count: number;
      tools: AgentTool[];
      guards: AgentGuards;
      kill: AgentKillState;
    }>('/agent/tools'),
  setKill: (on: boolean, reason?: string) =>
    request<{ success: boolean; kill: AgentKillState }>('/agent/kill', {
      method: 'POST',
      body: JSON.stringify({ on, reason: reason || '' }),
    }),
  log: (limit = 100) =>
    request<{ success: boolean; count: number; items: Record<string, unknown>[] }>(
      `/agent/log?limit=${limit}`
    ),
};

// --- Memory ---
//
// 注意：后端 /api/memory/list 返回的是信封对象 { success, memories: [...], total }，
// /api/memory/stats 返回 { success, stats: { total, by_type: {...} }, ... }。
// 这里必须在这一层拆封，否则页面拿到的是对象而非数组（列表渲染会崩），
// 统计卡片也会因为读不到 total/lessons 而全部显示为空——这正是导航恢复后
// 记忆页「数字全空 + 列表崩溃」的根因。
export const memoryApi = {
  stats: async (): Promise<MemoryStats> => {
    const d = await request<{ success?: boolean; stats?: Record<string, unknown> }>('/memory/stats');
    const s = (d?.stats || {}) as Record<string, unknown>;
    const by = (s.by_type || {}) as Record<string, number>;
    return {
      total: Number(s.total ?? 0),
      lessons: Number(by.lesson ?? 0),
      successes: Number(by.success ?? 0),
      insights: Number(by.insight ?? 0),
      promptLessons: Number(s.prompt_lessons ?? 0),
    };
  },
  /**
   * 质检教训库（生成链路自动学习成果；与手动记忆是两套数据）。
   *
   * 返回完整分页信封（total/filtered/by_kind/dead_lessons/lessons），供页面做
   * 「死教训数」统计卡与「加载更多」分页。关键词过滤走 `q`，
   * ⚠️ 不复用 /memory/lessons/search（那是「按 prompt 召回试算」，语义不同）。
   */
  lessons: async (params?: LessonQuery): Promise<LessonPage> => {
    const qs = new URLSearchParams();
    if (params?.kind) qs.set('kind', params.kind);
    if (params?.project) qs.set('project', params.project);
    if (params?.since) qs.set('since', params.since);
    if (params?.until) qs.set('until', params.until);
    if (params?.q) qs.set('q', params.q);
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString() ? `?${qs.toString()}` : '';
    const d = await request<{
      success?: boolean;
      total?: number;
      filtered?: number;
      offset?: number;
      limit?: number;
      by_kind?: Record<string, number>;
      dead_lessons?: number;
      lessons?: PromptLesson[];
    }>(`/memory/lessons${q}`);
    return {
      total: Number(d?.total ?? 0),
      filtered: Number(d?.filtered ?? 0),
      by_kind: d?.by_kind || {},
      dead_lessons: Number(d?.dead_lessons ?? 0),
      lessons: Array.isArray(d?.lessons) ? d.lessons : [],
    };
  },
  /** 删除单条教训（DELETE /api/memory/lessons/<lesson_id>；未命中 404） */
  deleteLesson: (lessonId: string) =>
    request<{ success: boolean; deleted?: number; lesson_id?: string }>(
      `/memory/lessons/${encodeURIComponent(lessonId)}`,
      { method: 'DELETE' }
    ),
  /** 清空某一环节的全部教训（POST /api/memory/lessons/clear body {kind}） */
  clearLessons: (kind: string) =>
    request<{ success: boolean; cleared?: number; kind?: string }>('/memory/lessons/clear', {
      method: 'POST',
      body: JSON.stringify({ kind }),
    }),
  /** 教训库统计（GET /api/memory/stats 的 `lessons` 节点） */
  lessonStats: async () => {
    const d = await request<{ success?: boolean; lessons?: Record<string, unknown> }>('/memory/stats');
    const l = (d?.lessons || {}) as Record<string, unknown>;
    return {
      total: Number(l.total ?? 0),
      by_kind: (l.by_kind || {}) as Record<string, number>,
      dead_lessons: Number(l.dead_lessons ?? 0),
      used_total: Number(l.used_total ?? 0),
    };
  },
  /** 试算：给定提示词会召回哪些历史修正建议（把「自动学习」变得可见可验证） */
  recall: async (params: { kind: string; prompt: string; project?: string; style?: string }) => {
    const qs = new URLSearchParams({ kind: params.kind, prompt: params.prompt });
    if (params.project) qs.set('project', params.project);
    if (params.style) qs.set('style', params.style);
    return request<{
      success?: boolean; hints?: string[]; learned_prompt?: string; changed?: boolean;
    }>(`/memory/lessons/search?${qs.toString()}`);
  },
  // 审计 P2：后端 api_memory_list 只认 type/limit —— query 会被静默忽略
  // （「按关键词过滤」实际不过滤），已移除避免误导调用方
  list: async (params?: { type?: string }): Promise<Memory[]> => {
    const qs = new URLSearchParams();
    if (params?.type) qs.set('type', params.type);
    const query = qs.toString() ? `?${qs.toString()}` : '';
    const d = await request<{ memories?: Memory[] } | Memory[]>(`/memory/list${query}`);
    if (Array.isArray(d)) return d;
    return d?.memories || [];
  },
  record: (data: Partial<Memory>) =>
    request<Memory>('/memory/record', { method: 'POST', body: JSON.stringify(data) }),
  // 字段名对齐后端契约（app.py api_memory_record_lesson）：
  // 后端读 project/prompt/issues/category，不再读 lesson/tags。
  // 旧签名 (lesson, tags) 已废弃，改为传结构化对象。
  recordLesson: (data: {
    project: string;
    prompt?: string;
    issues: string[];
    episode?: number;
    category?: string;
  }) =>
    request<Memory>('/memory/record-lesson', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  recordSuccess: (data: {
    project: string;
    prompt?: string;
    highlights: string[];
    episode?: number;
    category?: string;
  }) =>
    request<Memory>('/memory/record-success', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  clearOld: (days: number = 90) =>
    request<void>('/memory/clear-old', {
      method: 'POST',
      body: JSON.stringify({ days }),
    }),
  insights: () => request<Record<string, unknown>>('/memory/insights'),
};

// --- i18n ---
export const i18nApi = {
  get: (lang: string) => request<I18nData>(`/i18n/${lang}`),
};

// --- Analytics ---
export const analyticsApi = {
  // 后端没有 /api/analytics，聚合数据在 /api/analytics/summary
  get: () => request<AnalyticsData>('/analytics/summary'),
  // 手工登记一条耗时事件（字段与后端 api_analytics_record 对齐）
  record: (data: {
    kind?: string;
    project?: string;
    label?: string;
    duration_sec?: number;
    units?: number;
    success?: boolean;
    meta?: Record<string, unknown>;
  }) =>
    request<{ success: boolean }>('/analytics/event', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

// --- Keyframes ---
export const keyframesApi = {
  plan: (project: string, episode?: number) =>
    request<KeyframePlanResponse>(
      `/keyframes/plan?project_name=${encodeURIComponent(project)}${episode ? `&episode_no=${episode}` : ''}`
    ),
  generate: (data: { project_name: string; episode_no?: number; shots?: any[]; only_missing?: boolean }) =>
    request<{ success: boolean; task_id: string; total: number }>(
      '/keyframes/generate',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  // list/file（GET /keyframes/list/<project>、/keyframes/file/<file>）前端零调用
  // —— 关键帧产物直接由 plan 返回的 url 展示，已作为死方法移除。
};

// --- Storyboard ---
export const storyboardApi = {
  canvas: async (project: string, episode?: number) => {
    const data = await request<StoryboardCanvasResponse>(
      `/storyboard/canvas/${encodeURIComponent(project)}${episode ? `?episode_no=${episode}` : ''}`
    );
    // P3-2（Toonflow 借鉴）：分镜 schema 运行时校验——后端字段缺失时响亮降级
    // （丢弃异常卡片并 console.warn），而不是渲染到一半白屏。
    const raw = (data.cards || []) as StoryboardShot[];
    const cards = raw.filter(
      (c): c is NonNullable<typeof c> => !!c && typeof c === 'object' && !!String(c.shot_id || '').trim()
    );
    if (cards.length !== raw.length) {
      // eslint-disable-next-line no-console
      console.warn(`[storyboard] 丢弃 ${raw.length - cards.length} 张缺少 shot_id 的异常分镜卡片`);
    }
    return { ...data, cards };
  },
  reorder: (data: { project_name: string; episode_no?: number; order: string[] }) =>
    request<{ success: boolean; shot_order: string[] }>(
      '/storyboard/shot/reorder',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  retryShot: (data: { project_name: string; shot_id: string; episode_no?: number }) =>
    request<{ success: boolean }>(
      '/storyboard/retry-shot',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  file: (project: string, filename: string) =>
    `${API_BASE}/storyboards/file/${encodeURIComponent(project)}/${filename}`,
  // generateNineGrid / selectNineGridShot（POST /storyboard/nine-grid 与 /select）
  // 已随后端九宫格草案路由下线移除；九宫格候选构图改走 gridCandidates / gridApply。
  // ---- 分镜九宫格候选构图（2026-10-01，对标 BigBanana：一图 9 候选→选格裁切） ----
  // 返回类型对齐后端实际契约（app.py api_storyboard_grid_candidates / grid_apply）：
  // gridApply 实际回 {success, project, shot_id, cell, applied, url, cleared, hint}，
  // 此前只声明了 {success, applied, url}，调用方读 cell 会编译报错。
  gridCandidates: (projectName: string, episodeNo: number, shotId: string) =>
    request<ShotGridStartResponse>('/storyboard/grid-candidates', {
      method: 'POST',
      body: JSON.stringify({ project_name: projectName, episode_no: episodeNo, shot_id: shotId }),
    }),
  gridApply: (projectName: string, episodeNo: number, shotId: string, cell: number) =>
    request<ShotGridApplyResponse>('/storyboard/grid-apply', {
      method: 'POST',
      body: JSON.stringify({ project_name: projectName, episode_no: episodeNo, shot_id: shotId, cell }),
    }),
};

/** 视频生成方式（**项目级**设定，与后端 config.VIDEO_MODES 一致）
 *  - episode  整集一次提交，H3 原生段间衔接出一条连续整集视频
 *  - per_shot 逐镜独立生成，便于单镜返工
 *  - keyframe 首尾帧驱动（先生成尾帧，再在首尾之间插值出运动） */
export type VideoMode = 'episode' | 'per_shot' | 'keyframe';

// --- Video（单镜视频） ---
// 接口：POST /api/video/retry-shot（同步，直到 ComfyUI 出片才返回）
// 之前后端早已可用，但前端零引用 —— 用户对某一镜不满意时无法只重做这一镜。
// mode: reference（分镜图+主角锚点，默认）/ keyframe（首尾帧插值，需已生成尾帧）
export const videoApi = {
  retryShot: (data: {
    project_name: string;
    shot_id: string | number;
    episode_no?: number;
    mode?: 'reference' | 'keyframe';
    seed?: number;
    timeout?: number;
  }) =>
    request<{
      success: boolean;
      project: string;
      shot_id: string | number;
      seq: number;
      mode: string;
      path: string;
      /** 可直接播放/下载：/api/videos/<项目>/[epNN/]shot_NN.mp4 */
      url: string;
      ref_count: number;
      duration: number;
      /** 后端已把同集旧成片标记为「需重新合成」 */
      deliverable_marked_stale?: boolean;
    }>('/video/retry-shot', { method: 'POST', body: JSON.stringify(data) }),

  // ---- 批量重生成镜头视频（2026-10-02）：POST /api/video/retry-shots-batch ----
  // 与 retry-shot 一样是**同步端点**：逐镜等 ComfyUI 出片后才整体返回，可能耗时数分钟，
  // 前端必须给 loading 并防重复提交。单次最多 12 个；单镜失败不拖垮整批，
  // 逐镜看 results[].success / error。同集旧成片由后端标记为「需重新合成」。
  retryShotsBatch: (projectName: string, episodeNo: number | undefined, shotIds: string[]) =>
    request<VideoRetryBatchResponse>('/video/retry-shots-batch', {
      method: 'POST',
      body: JSON.stringify({ project_name: projectName, episode_no: episodeNo, shot_ids: shotIds }),
    }),

  /**
   * 生成该集视频：把该集 N 个镜头一次提交给 H3，**生成方式由 mode 决定**
   * （episode 整集一次出连续片 / per_shot 逐镜独立 / keyframe 首尾帧插值）。
   * 后端在 shots 为空时按 project_name + episode_no 自动读剧本兜底。
   * 异步：返回 task_id，进度走 generation/status/<task_id>。
   *
   * ⚠️ 历史缺陷：这里曾**硬编码 mode='episode'**，用户在任何地方都改不了生成方式；
   * 现在不传 mode 时由后端读**项目配置**（新建项目时选择），传了则以本次为准。
   */
  generateEpisode: (data: {
    project_name: string;
    episode_no?: number;
    mode?: VideoMode;
    timeout_per_segment?: number;
    /** 按场次生成（2026-10-03）：只生成/重做这些场（如 [3] = 只重做第 3 场） */
    only_scenes?: number[];
    /** true = 覆盖已有场次视频（「重做本场」必带） */
    overwrite?: boolean;
  }) =>
    request<{
      success: boolean;
      task_id: string;
      status: string;
      total: number;
      mode: string;
      project_name: string;
      episode_no: number;
    }>('/videos/generate', { method: 'POST', body: JSON.stringify(data) }),
};

// --- TTS ---
// 旧「TTS 配音任务」链路（plan/generate/status/tasks/list）已随后端路由一同下线移除；
// H3 成片自带角色配音（生成时注入 voice_bank 参考音色），本组只保留环境自检 /
// 试听 / 参考音色库管理 / 产物访问。
export const ttsApi = {
  env: (project?: string) =>
    request<TTSEnv>(`/tts/env${project ? `?project_name=${encodeURIComponent(project)}` : ''}`),
  voiceMap: (project: string, voiceMap: Record<string, unknown>) =>
    request<{ success: boolean }>('/tts/voice-map', {
      method: 'POST',
      body: JSON.stringify({ project_name: project, voice_map: voiceMap }),
    }),
  preview: (data: { project_name: string; text: string; character?: string }) =>
    request<{ success: boolean; url: string }>(
      '/tts/preview',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  file: (project: string, filename: string) =>
    `${API_BASE}/tts/file/${encodeURIComponent(project)}/${filename}`,
  // ---- 参考音频克隆角色声线（2026-10-06）----
  voiceBank: (project: string) =>
    request<{
      success: boolean;
      items: VoiceBankItem[];
      clone_available: boolean;
      supported_exts: string[];
    }>(`/tts/voice-bank?project_name=${encodeURIComponent(project)}`),
  /** 上传某角色的参考音频（ref_text = 该音频里实际说出的那句话，填了相似度更高） */
  voiceBankUpload: (data: {
    project_name: string;
    character: string;
    file: File;
    ref_text?: string;
  }) => {
    const fd = new FormData();
    fd.append('file', data.file);
    fd.append('project_name', data.project_name);
    fd.append('character', data.character);
    if (data.ref_text) fd.append('ref_text', data.ref_text);
    return request<{ success: boolean; character?: string; file?: string; duration_sec?: number; message?: string }>(
      '/tts/voice-bank/upload',
      { method: 'POST', body: fd }
    );
  },
  voiceBankDelete: (project: string, character: string) =>
    request<{ success: boolean; message?: string }>('/tts/voice-bank/delete', {
      method: 'POST',
      body: JSON.stringify({ project_name: project, character }),
    }),
  /** 用已绑定的参考音频试听克隆效果（不读 voice_map，直接以 clone 模式合成） */
  voiceBankPreview: (data: { project_name: string; character: string; text?: string }) =>
    request<{ success: boolean; url: string; text_used: string; voice: any }>(
      '/tts/voice-bank/preview',
      { method: 'POST', body: JSON.stringify(data) }
    ),
};

// --- Mix ---
// 旧「音画混音」任务链（/api/mix/* 全部路由）已下线，mixApi 整块移除；
// H3 成片自带配音与音效，无需再单独合成。

// --- QC ---
/** 音频质检结结论（两层：客观层 ffmpeg 指标 + AI 层频谱/波形送检） */
export interface QCAudioResult {
  success: boolean;
  project_name?: string;
  /** 检验对象来源：final=成片音轨 / mix=带配音成片 / merged=整集音轨 / line=单句 / path=显式路径 */
  source?: string;
  path?: string;
  passed: boolean | null;
  blocked: boolean;
  score: number | null;
  reason?: string;
  issues: string[];
  critical_issues: string[];
  metrics: {
    duration?: number;
    mean_db?: number | null;
    max_db?: number | null;
    silence_sec?: number;
    speech_ratio?: number | null;
    codec?: string;
    sample_rate?: number;
    channels?: number;
    size_bytes?: number;
  };
  /** AI 层是否真的参与了判定 */
  ai_used?: boolean;
  ai_skipped?: boolean;
  ai_skip_reason?: string;
  objective_only?: boolean;
  /** 频谱图 / 波形图（顺序固定：先频谱后波形），可直接作为 img src */
  visuals?: string[];
  expect_sec?: number | null;
  check_speech_ratio?: boolean;
  audio_qc_active?: boolean;
  audio_ai_active?: boolean;
  /** 原文件的可播放地址（成品可直接试听） */
  file_url?: string;
  error?: string;
}

export const qcApi = {
  config: () => request<QCResponse>('/qc/config'),
  updateConfig: (config: QCConfig) =>
    request<{ success: boolean }>('/qc/config', {
      method: 'POST',
      body: JSON.stringify({ config }),
    }),
  clearConfig: () => request<{ success: boolean }>('/qc/config/clear', { method: 'POST' }),
  resetEndpoint: () => request<{ success: boolean }>('/qc/config/reset-endpoint', { method: 'POST' }),
  syncFromAI: () => request<{ success: boolean }>('/qc/config/sync-from-ai', { method: 'POST' }),
  // 审计 P1-7（2026-09-29）：必须把 project_name 送到后端 —— api_qc_test 按
  // project_name（缺省回落共享命名空间 'project'）从分镜目录自动挑样张；
  // 旧实现恒发空包体，「重测」拿到的要么是连通性探测、要么是别的项目的样张结论。
  // shot_id 后端暂不支持按镜定位，仅用于前端本地展示。
  test: (data: { project?: string; shot_id?: string }) =>
    request<{ success: boolean; verdict: string; score: number }>(
      '/qc/test',
      { method: 'POST', body: JSON.stringify({ project_name: data?.project || '' }) }
    ),
  history: (project: string) =>
    request<QCResponse>(`/qc/project-summary?project=${encodeURIComponent(project)}`),
  // 审计 P2：frames 方法已删除 —— 后端 GET /api/qc/frames/<file> 是**图片回显**
  // 路由（send_file），没有「按项目列帧」的 JSON 端点；旧方法命中后 JSON 解析必炸。
  /** 音频成品质检：客观层（ffmpeg 指标）始终执行；AI 层需配置质检接口 */
  checkAudio: (data: {
    project_name?: string;
    /** 显式指定产物文件（必须在 output/ 内） */
    path?: string;
    /** 未给 path 时按此推导：final（成片音轨）> mix > merged > line */
    source?: 'final' | 'mix' | 'merged' | 'line';
    expect_sec?: number;
    line_text?: string;
    /** 有声占比下限判定。单句传 true；整集/成片必须 false（天然有留白） */
    check_speech_ratio?: boolean;
    /** false=只跑客观层（毫秒级、零模型调用） */
    with_ai?: boolean;
  }) => request<QCAudioResult>('/qc/audio', { method: 'POST', body: JSON.stringify(data) }),
  /** 提示词预检（生成前质检）：确定性检查 + 自愈。kind 支持 storyboard/h3/asset/keyframe/audio */
  promptCheck: (data: {
    kind: string;
    prompt: string;
    style?: string;
    context?: Record<string, unknown>;
    ref_count?: number;
    expect_refs?: boolean;
    repair?: boolean;
  }) => request<any>('/qc/prompt', { method: 'POST', body: JSON.stringify(data) }),
};

// --- Episode scenes（按场次生成，2026-10-03） ---
// 视频生产已改为「按场次生成，最后拼接成整集」：每场一个 scene_XX.mp4，
// 全部完成后拼成 epNN_full.mp4。GET /api/episode/scenes 按集列出场次与其就绪状态。
export interface EpisodeSceneInfo {
  scene_no: number;
  heading?: string;
  location?: string;
  int_ext?: string;
  time_of_day?: string;
  shot_ids: (number | string)[];
  shot_count: number;
  /** 分镜已完成的镜数（名字带 ok 但语义是**计数**；与 shot_count 相等即该场分镜齐全） */
  storyboard_ok: number;
  /** 该场视频（scene_XX.mp4）是否已生成 */
  video_ready: boolean;
  /** 场次视频可播放/下载地址（未生成时为空） */
  video_url?: string;
}

export interface EpisodeScenesResponse {
  success: boolean;
  scene_count: number;
  /** 该集整集成片（epNN_full.mp4）是否已拼接完成 */
  full_video_ready: boolean;
  scenes: EpisodeSceneInfo[];
}

// --- Episodes ---
export const episodesApi = {
  list: (novelId: string) =>
    request<EpisodeListResponse>(`/episodes/${encodeURIComponent(novelId)}`),
  get: (novelId: string, episodeNo: number) =>
    request<Episode>(`/episodes/${encodeURIComponent(novelId)}/${episodeNo}`),
  // 按场次生成视图（2026-10-03）：某集的场次列表 + 各场分镜/视频就绪状态。
  // project=项目键（后端 ?project= 约定），episode_no=集号。
  scenes: (projectKey: string, episodeNo: number) =>
    request<EpisodeScenesResponse>(
      `/episode/scenes?project=${encodeURIComponent(projectKey)}&episode_no=${episodeNo}`
    ),
  // 审计 P2：后端 api_novel_episodes_generate 必须带 chapters（章节号数组）或
  // start/end —— 空包体一律 400「未选择有效章节」（旧签名无 body，接线即坏）
  // use_screenplay=true：以「文学剧本」为原文改写成结构化分镜剧本；
  // 不传 / false 则沿用原文（小说正文）。可能以异步任务返回（带 task_id）。
  generate: (
    novelId: string,
    body: { chapters?: number[]; start?: number; end?: number; use_screenplay?: boolean }
  ) =>
    request<{ success: boolean; episode_count: number; task_id?: string }>(
      `/novels/${encodeURIComponent(novelId)}/episodes/generate`,
      { method: 'POST', body: JSON.stringify(body) }
    ),
};

// --- Screenplay（文学剧本：人审层；1章=1集，episode_no 即章号） ---
// GET  /api/novels/<novel_id>/screenplay/<episode_no>?project=<project_key>
//      → {exists, markdown, path, project_key}
// POST /api/novels/<novel_id>/screenplay/generate
//      body {chapter, project_id?/project_name?, style?} → {task_id, project_key, episode_no}
//      异步任务：轮询 generation/status/<task_id> 至 completed 后再 GET 取正文。
export interface ScreenplayDoc {
  exists: boolean;
  markdown: string;
  path?: string;
  project_key?: string;
}

export const screenplayApi = {
  get: (novelId: string, episodeNo: number, projectKey?: string) => {
    const q = projectKey ? `?project=${encodeURIComponent(projectKey)}` : '';
    return request<ScreenplayDoc>(
      `/novels/${encodeURIComponent(novelId)}/screenplay/${episodeNo}${q}`
    );
  },
  generate: (
    novelId: string,
    chapter: number,
    opts?: { projectId?: string; projectName?: string; style?: string }
  ) =>
    request<{ success?: boolean; task_id: string; project_key?: string; episode_no?: number }>(
      `/novels/${encodeURIComponent(novelId)}/screenplay/generate`,
      {
        method: 'POST',
        body: JSON.stringify({
          chapter,
          project_id: opts?.projectId,
          project_name: opts?.projectName,
          style: opts?.style,
        }),
      }
    ),
};

/** 前置解析结果（人物档案 + 故事梗概 + 关键事件 + 人物情绪） */
export interface ChapterPreflightResult {
  version: string;
  chapter_index: number;
  chapter_title: string;
  analyzed_at: string;
  characters: {
    name: string;
    gender: string;
    age: string;
    identity: string;
    appearance: string;
    personality: string;
    voice_style: string;
    emotions: { beat: string; emotion: string; intensity: number }[];
  }[];
  story_summary: string;
  key_events: string[];
  character_mood_arc: string;
}

export const preflightApi = {
  /** 运行前置解析（LLM 调用，耗时较长） */
  analyze: (novelId: string, chapterIndex: number, force?: boolean) =>
    request<{ success: boolean; result: ChapterPreflightResult; bible_added: string[]; bible_updated: string[] }>(
      `/novels/${encodeURIComponent(novelId)}/chapters/${chapterIndex}/preflight`,
      { method: 'POST', body: JSON.stringify({ force }) }
    ),
  /** 读取已有前置解析结果 */
  get: (novelId: string, chapterIndex: number) =>
    request<{ exists: boolean; result?: ChapterPreflightResult }>(
      `/novels/${encodeURIComponent(novelId)}/chapters/${chapterIndex}/preflight`
    ),
  /** 列出该项目所有已完成前置解析的章节 */
  list: (novelId: string) =>
    request<{ success: boolean; chapter_indices: number[]; count: number }>(
      `/novels/${encodeURIComponent(novelId)}/preflight/list`
    ),
};

/** P2-2 分集断点提议（与生产口径同源；不传 opts 即默认参数，提议 ≡ 实际生成） */
export const novelsSplitPlanApi = {
  get: (
    novelId: string,
    opts?: { maxSec?: number; maxShots?: number; fixedParts?: number }
  ) => {
    const qs = new URLSearchParams();
    if (opts?.maxSec != null) qs.set('max_sec', String(opts.maxSec));
    if (opts?.maxShots != null) qs.set('max_shots', String(opts.maxShots));
    if (opts?.fixedParts != null) qs.set('fixed_parts', String(opts.fixedParts));
    const q = qs.toString();
    return request<NovelSplitPlanResponse>(
      `/novels/${encodeURIComponent(novelId)}/split-plan${q ? `?${q}` : ''}`
    );
  },
};

// --- ComfyUI 模型 / 插件扫描（全局，与具体项目无关） ---
export const comfyuiModelsApi = {
  /** 扫描 ComfyUI 实际可用的模型槽位候选与已装自定义节点包。
   *  refresh=true 会绕过后端 object_info 缓存重新拉取（「重新扫描」按钮）。 */
  scan: (refresh = false) =>
    request<ComfyUIModelsResponse>(`/comfyui/models${refresh ? '?refresh=1' : ''}`),
  /** 手选模型。patch 形如 { unet_main: '<模型名>' }；
   *  传空串/null 表示清空该槽位，回落到工作流模板自身的取值。 */
  select: (patch: Record<string, string | null>) =>
    request<{ success: boolean; selection: Record<string, string> }>('/comfyui/models', {
      method: 'POST',
      body: JSON.stringify(patch),
    }),
};

// --- 后台服务日志 ---
export const logsApi = {
  sources: () => request<{ success: boolean; sources: LogSource[] }>('/logs/sources'),
  /** source 只接受白名单 key；since>0 时只返回新增内容（自动刷新用） */
  tail: (opts?: { source?: string; tail?: number; since?: number;
                  q?: string; level?: string }) => {
    const qs = new URLSearchParams();
    if (opts?.source) qs.set('source', opts.source);
    if (opts?.tail) qs.set('tail', String(opts.tail));
    if (opts?.since) qs.set('since', String(opts.since));
    if (opts?.q) qs.set('q', opts.q);
    if (opts?.level) qs.set('level', opts.level);
    const q = qs.toString();
    return request<LogsResponse>(`/logs${q ? `?${q}` : ''}`);
  },
};

// --- Continuity / Coverage / Script Consistency ---
// continuityApi / coverageApi / scriptConsistencyApi 三个只读报告接口前端零调用
// （连贯性/覆盖率/一致性结论已并入剧本生成结果与总控面板），整块移除。

// --- Autopilot ---
export const autopilotApi = {
  status: (project?: string) =>
    request<AutopilotStatus>(
      project ? `/autopilot/status?project=${encodeURIComponent(project)}` : '/autopilot/status'
    ),
  ready: () => request<{ ready: boolean }>('/autopilot/ready'),
  curve: () => request<any>('/autopilot/curve'),
  plans: () => request<{ success: boolean; plans: any[] }>('/autopilot/plans'),
  plan: (project: string) =>
    request<any>(`/autopilot/plan/${encodeURIComponent(project)}`),
  /**
   * 更新某项目的自动生产计划。
   *
   * ⚠️ 后端只接受 `autopilot.PLAN_DEFAULTS` 里声明过的字段
   * （app.py `api_autopilot_plan_set` 用 `k in PLAN_DEFAULTS` 过滤），
   * 传入未声明的键会被静默丢弃、甚至整体报「没有可更新字段」。
   * 因此新增可配置项时必须同时加进 PLAN_DEFAULTS。
   */
  setPlan: (project: string, patch: Record<string, unknown>) =>
    request<{ success: boolean; project: string; plan: Record<string, unknown> }>(
      `/autopilot/plan/${encodeURIComponent(project)}`,
      { method: 'POST', body: JSON.stringify(patch) }
    ),
  // 审计 P2：enable/disable 后端走 _project_or_400(data.get('project'))——
  // 空包体一律 400「缺少 project」，调用时必须带项目
  enable: (project: string) =>
    request<{ success: boolean }>('/autopilot/enable', {
      method: 'POST',
      body: JSON.stringify({ project }),
    }),
  disable: (project: string) =>
    request<{ success: boolean }>('/autopilot/disable', {
      method: 'POST',
      body: JSON.stringify({ project }),
    }),
  pause: () => request<{ success: boolean }>('/autopilot/pause', { method: 'POST' }),
  resume: () => request<{ success: boolean }>('/autopilot/resume', { method: 'POST' }),
  progress: () => request<{ success: boolean; count?: number; items: AutopilotProgress[] }>('/autopilot/progress'),
  progressByProject: (project: string) =>
    request<AutopilotProgress>(`/autopilot/progress/${encodeURIComponent(project)}`),
  /** 成片清单。传 project 只取该项目的（工作台用），不传则取全部项目。 */
  deliverables: (project?: string) =>
    request<DeliverablesResponse>(
      project ? `/autopilot/deliverables?project=${encodeURIComponent(project)}` : '/autopilot/deliverables'
    ),
  /**
   * 验收 / 打回成片。打回会在下次托管轮转时自动重跑该集。
   *
   * ⚠️ 字段名必须与后端一致（app.py `api_autopilot_review`）：
   *   project / episode_no(int) / review('accepted'|'rejected'|'pending') / note?
   * 这里曾经误写成 { project, deliverable, verdict } —— 接口一直是 400，
   * 即「验收/打回」功能从未真正生效过。
   */
  reviewDeliverable: (data: {
    project: string;
    episode_no: number;
    review: 'accepted' | 'rejected' | 'pending';
    note?: string;
  }) =>
    request<{ success: boolean; item: Deliverable }>('/autopilot/deliverables/review', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  exceptions: () =>
    request<{ success: boolean; exceptions: any[] }>('/autopilot/exceptions'),
  // 审计 P2：后端 api_autopilot_exception_resolve 读 project/project_name +
  // episode_no + note —— 没有 exception_id/action，旧签名接线必 404（第0集）
  resolveException: (data: { project: string; episode_no: number; note?: string }) =>
    request<{ success: boolean }>('/autopilot/exceptions/resolve', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  runOnce: (data: { project_name: string }) =>
    request<{ success: boolean; task_id: string }>(
      '/autopilot/run-once',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  planFromSettings: (project: string) =>
    request<{ success: boolean; plan: any }>(
      `/autopilot/plan-from-settings/${encodeURIComponent(project)}`,
      { method: 'POST' }
    ),
};

// --- Providers ---
export const providersApi = {
  list: () => request<ProvidersResponse>('/providers'),
  // 字段名对齐后端契约（app.py api_providers_select）：
  // 后端读 kind（image/video/tts）+ name（引擎名），不读 provider_id。
  select: (kind: 'image' | 'video' | 'tts', name: string) =>
    request<{ success: boolean; kind: string }>('/providers/select', {
      method: 'POST',
      body: JSON.stringify({ kind, name }),
    }),
};

// --- Generation ---
export const generationApi = {
  status: (taskId: string) =>
    request<{ success: boolean; task: any }>(`/generation/status/${taskId}`),
};

// --- Character Outfits（角色服装变体 / 衣柜，2026-10-02） ---
// 后端：POST /api/assets/character/outfit（生成，复用资产生成全链路，产物落到
//       characters/<项目>/<角色>/outfits/<outfit_key>/）；
//       GET /api/assets/character/outfits（列表；outfits 目录不存在时后端返回空数组）。
export const characterOutfits = {
  /** 列出角色已登记的服装变体（含 ready / 各档位是否已切分） */
  list: (projectName: string, character: string) => {
    const qs = new URLSearchParams({ project_name: projectName, character });
    return request<CharacterOutfitsResponse>(`/assets/character/outfits?${qs.toString()}`);
  },
  /**
   * 生成服装变体（后台任务，进度走 generationApi.status(task_id)）。
   * outfit_key 会被后端安全化（剔除 \ / : * ? " < > |、≤40 字符）；
   * 已存在同 key 且 base.png 就绪时后端默认幂等跳过（带 overwrite=true 强制重画）。
   */
  generate: (data: {
    project_name: string;
    character: string;
    outfit_key: string;
    outfit_desc: string;
    style?: string;
    overwrite?: boolean;
  }) =>
    request<CharacterOutfitGenerateResponse>('/assets/character/outfit', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

// --- 上传角色形象图 → 三视图（本地切分，零 GPU）---
export interface UploadSheetResponse {
  success: boolean;
  /** true = 已落 base.png 但版式不符、未能切分（下游回落整图） */
  derive_ok?: boolean;
  /** 已有 base.png 且未带 overwrite 时后端幂等跳过 */
  skipped?: boolean;
  character?: string;
  outfit_key?: string;
  base?: string;
  views?: Record<string, string>;
  view_files?: Record<string, string>;
  message?: string;
  error?: string;
  /** 期望的版式说明（切分失败时给用户看） */
  layout_hint?: string;
}
export const characterSheetUpload = {
  /**
   * 上传角色形象图。图片会**直接落 base.png** 再本地切分出 front/left/back/half，
   * 不做 GPU 重绘、不做图片质检 —— 人物外形 100% 保留。
   * 因此上传图需已按分档版式排好：上排正面/左侧/背面三全身 + 下排一格正面半身。
   * outfit_key 为空 = 挂到角色主设定；非空 = 挂到对应服装变体档。
   */
  upload: (data: {
    project_name: string;
    character: string;
    file: File;
    outfit_key?: string;
    overwrite?: boolean;
  }) => {
    const fd = new FormData();
    fd.append('file', data.file);
    fd.append('project_name', data.project_name);
    fd.append('character', data.character);
    if (data.outfit_key) fd.append('outfit_key', data.outfit_key);
    if (data.overwrite) fd.append('overwrite', '1');
    // 不手写 Content-Type：浏览器需自行补 multipart boundary
    return request<UploadSheetResponse>('/assets/character/upload-sheet', {
      method: 'POST',
      body: fd,
    });
  },
};

// --- 资产沉淀过程（时间线）---
// 资产不是「一下就有的」：抽取 → 写提示词 → 出图 → 质检（可能重画 N 次）→ 沉淀教训
// → 切分入库。后端把这些散落在质检历史 / 产物元数据 / 教训库三处的痕迹聚合成一条
// 时间线返回，供资产详情页可视化。
export type PrecipitationStatus = 'done' | 'failed' | 'pending' | 'skipped';

export interface PrecipitationItem {
  /** 键值型条目（extract/prompt/base/derive/store 用） */
  key?: string;
  value?: string;
  /** 质检条目（qc 步骤用） */
  attempt?: number | null;
  passed?: boolean;
  score?: number | null;
  reason?: string;
  issues?: string[];
  at?: string | null;
  seed?: number | null;
  /** 教训条目（lesson 步骤用） */
  issue?: string;
  category?: string;
  priority?: string;
  project?: string;
}

export interface PrecipitationStep {
  id: string;
  label: string;
  status: PrecipitationStatus;
  detail: string;
  items: PrecipitationItem[];
  at?: string | null;
}

export interface AssetPrecipitationResponse {
  success: boolean;
  kind: string;
  name: string;
  project: string;
  asset_dir?: string;
  script_path?: string;
  steps: PrecipitationStep[];
  summary: {
    total_steps: number;
    done: number;
    qc_attempts: number;
    qc_passed: boolean;
    lessons: number;
    views: string[];
    is_user_upload: boolean;
  };
}

export const assetPrecipitation = {
  /** 某资产的沉淀过程（纯只读；读失败后端降级为空步骤，不抛 500）。 */
  get: (project: string, kind: string, name: string) =>
    request<AssetPrecipitationResponse>(
      `/projects/${encodeURIComponent(project)}/asset-precipitation` +
        `?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`
    ),
};

// --- Export ---
export interface ExportedFile {
  format: string;
  filename: string;
  exists: boolean;
  path?: string;
  dir?: string;
  project?: string;
  exported_at?: string | null;
  shot_count?: number | null;
  total_sec?: number | null;
  size_mb?: number | null;
}
export const exportApi = {
  // generate（POST /export/<project>）已随后端项目级导出路由下线移除；
  // 后端仅保留 /api/export/{run,current,download,list}（前端暂无对应方法，按需补接）。
  listFiles: (projectName: string) =>
    request<{ success: boolean; files: ExportedFile[]; items?: any[] }>(
      `/export/list?project=${encodeURIComponent(projectName)}`
    ),
};

// --- AI Config (Unified: text / qc / chat) ---
// 注意：这里的路径不要再写 '/api' 前缀——request() 已经统一加了 API_BASE('/api')，
// 之前写成 '/api/ai/config' 实际会请求 /api/api/ai/config → 404，AI 配置页整体不可用。
export const aiConfigApi = {
  get: () => request<AIConfigResponse>('/ai/config'),
  /**
   * 按需回显某模块已保存的 api_key 明文（眼睛按钮点开时调用）。
   * 默认 get() 仍一律脱敏 —— 已保存密钥的前端 value 为空，圆点是 placeholder。
   */
  revealKey: (module: string) =>
    request<{ success: boolean; module: string; has_api_key: boolean; api_key: string }>(
      `/ai/config/reveal?module=${encodeURIComponent(module)}`
    ),
  /**
   * 保存单个模块。
   * reasoning_effort = 思考档位（'' | 'low' | 'high' | 'max'），只对「思考不可关闭」的模型
   * （如 GLM-5.3-Flash）有意义：留空 = 不注入该参数，由服务端取默认档。
   * 用 undefined 表示「不改动」，用空串表示「清空」——两者语义不同，别合并。
   */
  save: (module: string, base_url: string, model: string, api_key?: string, reasoning_effort?: string,
        fallbacks?: { base_url: string; model: string; api_key?: string; label?: string; reasoning_effort?: string }[]) =>
    request<{ success: boolean; module_config: any; config: AIConfigResponse['config']; message: string }>(
      '/ai/config',
      { method: 'POST', body: JSON.stringify({ module, base_url, model, api_key, reasoning_effort, fallbacks }) }
    ),
  clear: (module?: string) =>
    request<{ success: boolean; config: AIConfigResponse['config']; message: string }>(
      '/ai/config/clear',
      { method: 'POST', body: JSON.stringify({ module }) }
    ),
  test: (module: string, base_url: string, model: string, api_key?: string, probe?: string, timeout?: number,
         reasoning_effort?: string) =>
    request<AITestResult>(
      '/ai/test',
      { method: 'POST', body: JSON.stringify({ module, base_url, model, api_key, probe, timeout, reasoning_effort }) }
    ),
  /**
   * 测试「已保存的备用模型」连通性。
   * 前端视图里备用的 api_key 恒为脱敏值（拿不到明文），所以这里只传索引，
   * 由后端按 fallback_index 取那条备用已保存的 base_url / model / 解密后的 key 再探。
   */
  testFallback: (module: string, index: number, probe?: string, timeout?: number) =>
    request<AITestResult>(
      '/ai/test',
      { method: 'POST', body: JSON.stringify({ module, fallback_index: index, probe, timeout }) }
    ),
};

// --- System Settings (LLM engine, ComfyUI, Watermark) ---
export const settingsApi = {
  get: () => request<{ success: boolean; settings: Record<string, unknown> }>('/ai/settings'),
  update: (data: Record<string, unknown>) =>
    request<{ success: boolean; message?: string }>('/ai/settings', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

/**
 * 视频水印配置。此前 AI 配置页把水印开关塞进 `/ai/settings`，
 * 而那个接口的 GET 返回的是「创作设定」（art_style/genre/tone…），
 * 从来不包含 watermark_enabled —— 于是开关永远显示为关，
 * 保存也写不进真正的配置。这里直接对接专用的 /watermark/config。
 */
export const watermarkApi = {
  get: () =>
    request<{ success?: boolean; config: { enabled: boolean; text?: string; [k: string]: unknown } }>(
      '/watermark/config'
    ),
  update: (patch: Record<string, unknown>) =>
    request<{ success?: boolean; config: Record<string, unknown> }>('/watermark/config', {
      method: 'POST',
      body: JSON.stringify(patch),
    }),
};

/**
 * 提示词增强总开关（2026-09-30）：出图/出片前用「文本分析模型」把画面描述改写得更具体，
 * 再用「质检模型」对提示词做语义复审。任何一层失败都 fail-open（按原文继续），绝不阻断生成。
 * 保存后立即生效、无需重启（后端每次生成都重读开关文件）。
 */
export const promptEnhanceApi = {
  get: () =>
    request<{ success?: boolean; effective?: { enhance_enabled?: boolean; review_enabled?: boolean };
      file_config?: Record<string, unknown>; env_overridden?: { enhance?: boolean; review?: boolean } }>('/prompt_enhance/config'),
  update: (patch: { enhance_enabled?: boolean; review_enabled?: boolean }) =>
    request<{ success?: boolean; effective?: { enhance_enabled?: boolean; review_enabled?: boolean } }>('/prompt_enhance/config', {
      method: 'POST',
      body: JSON.stringify(patch),
    }),
};

/**
 * 提示词模板中心（2026-10-07，借鉴 Moha 的提示词管理布局）。
 *
 * 分层：用户覆盖 > 出厂默认（app/prompts/*.txt）> 代码内兜底常量（见 app/prompt_templates.py）。
 * list/get 回显的 text 是**文件原文**（含 `#` 头注释块）—— 头注释是给人看的文档，
 * 实际送模型的正文会在 load() 时剥掉，编辑器所见即所存，直接回显即可。
 * save 时后端校验占位符完整性：注册在案的变量（如 {style}）缺失会被 400 拒绝，
 * 错误文案经 readError() 透传给前端 toast。
 */
export interface PromptTemplateSummary {
  /** 模板键（prompt_templates.REGISTRY 的 key，如 qc_script_check） */
  name: string;
  title: string;
  /** 当前生效来源：override（用户覆盖）/ default（出厂默认） */
  source: string;
  /** 文件原文（含 # 头注释块），编辑器直接回显 */
  text: string;
  /** 正文字符数（后端统计，列表徽标用） */
  length: number;
  /** 注册在案的占位符变量名（不含花括号），如 ['style', 'script_data'] */
  variables: string[];
}

export const promptsApi = {
  list: () =>
    request<{ success: boolean; prompts: PromptTemplateSummary[] }>('/prompts/list'),
  get: (name: string) =>
    request<{
      success: boolean;
      name: string;
      title: string;
      text: string;
      variables: string[];
      source: string;
    }>(`/prompts/get?name=${encodeURIComponent(name)}`),
  /** 保存用户覆盖。后端校验占位符完整性，缺变量 → 400 {success:false,error} */
  save: (name: string, text: string) =>
    request<{ success: boolean; path?: string; message?: string }>('/prompts/save', {
      method: 'POST',
      body: JSON.stringify({ name, text }),
    }),
  /** 删除用户覆盖，回到出厂默认（对 default 模板调用是无害的 no-op） */
  reset: (name: string) =>
    request<{ success: boolean; message?: string }>('/prompts/reset', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
};

/**
 * 超分（FlashVSR）—— app/upscale_client.py 的界面入口。
 *
 * 后端 8 个 `/api/upscale/*` 端点早已实现且可用（含 TE-Speed 加速链路与
 * 旧链路自动回退），但此前前端零引用，属于「建好没入口」的能力。
 *
 * ⚠️ attach_audio 必须显式传 true：TE-Speed 链路默认 attach_audio=False，
 *    对「成片」超分时会把已合成的 TTS 配音整轨丢掉，产出无声视频。
 *    该参数此前也不在后端白名单里，已一并补上。
 */
export const upscaleApi = {
  /** 链路自检：ComfyUI 在线 / FlashVSR 模型 / 节点是否齐备 */
  env: () => request<UpscaleEnv>('/upscale/env'),

  /** 可作为超分输入的候选视频（成片、视频片段、已有超分产物、ComfyUI 产出） */
  sources: (projectName: string) =>
    request<{ success: boolean; project_name: string; items: UpscaleSource[] }>(
      `/upscale/sources?project_name=${encodeURIComponent(projectName)}`
    ),

  /** 已生成的超分产物 */
  list: (projectName: string) =>
    request<{ success: boolean; project_name: string; items: UpscaleArtifact[] }>(
      `/upscale/list?project_name=${encodeURIComponent(projectName)}`
    ),

  /** 发起超分（异步）：返回 task_id，用 status() 轮询 */
  submit: (data: {
    project_name: string;
    video_path: string;
    scale?: 2 | 3 | 4;
    mode?: string;
    engine?: 'te-speed-flashvsr' | 'legacy-flashvsr';
    /** 保留源视频音轨（成片必开，否则丢配音） */
    attach_audio?: boolean;
    [k: string]: unknown;
  }) =>
    request<UpscaleSubmitResponse>('/upscale/video', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  /** 查询单个超分任务进度/结果 */
  status: (taskId: string) =>
    request<UpscaleTask>(`/upscale/status/${encodeURIComponent(taskId)}`),

  /** 全部超分任务（按创建时间倒序） */
  tasks: () => request<{ success: boolean; items: UpscaleTask[] }>('/upscale/tasks'),
};
// --- 审片（四层质量状态 · 并排复核；2026-09-29） -------------------------------

export interface QualityStageInfo {
  status: string;
  name: string;
  label: string;
  at: string;
  note: string;
  has_binding: boolean;
}

export interface QualityReleaseInfo {
  ready: boolean;
  blockers: string[];
}

export interface QualityEpisodeRow {
  episode_no: number;
  title: string;
  shots_total: number;
  duration_sec: number;
  state: Record<string, QualityStageInfo>;
  release: QualityReleaseInfo;
  stale: Record<string, string>;
  artifact: { exists: boolean; name: string; url: string };
  review: { status?: string; stale?: boolean; exists?: boolean };
  preview: { exists: boolean; name?: string; url?: string };
}

export interface QualityEpisodesResponse {
  success: boolean;
  project: string;
  episodes: QualityEpisodeRow[];
  count: number;
  summary: { total: number; ready: number; awaiting_review: number };
}

export interface ReviewShotRef { kind: string; name: string; url: string }

export interface ReviewShotQcLatest {
  attempt?: number;
  ok?: boolean;
  passed?: boolean;
  score?: number;
  reason?: string;
  issues?: string[];
  critical_issues?: string[];
  style_mismatch?: boolean;
  duration?: number;
  time?: string;
  frames?: string[];
}

export interface ReviewShot {
  seq: number;
  shot_id: number | string;
  duration?: number;
  camera: string;
  location: string;
  description: string;
  dialogue_text: string;
  first_frame: string;
  last_frame: string;
  motion: string;
  emotion: string;
  beat: string;
  video: { exists: boolean; url: string };
  storyboard: { exists: boolean; url: string };
  refs: ReviewShotRef[];
  qc: { found: boolean; attempts: number; last_passed?: boolean | null; latest: ReviewShotQcLatest };
}

export interface QualityReviewResponse {
  success: boolean;
  project: string;
  episode: number;
  contract: { title: string; style: string; shots_total: number; duration_sec: number };
  shots: ReviewShot[];
  artifact: { exists: boolean; name: string; url: string };
  state: Record<string, QualityStageInfo>;
  release: QualityReleaseInfo;
  stale: Record<string, string>;
  preview: { exists: boolean; name?: string; url?: string };
}

export const qualityApi = {
  /** 集列表 + 每集四层状态（审片左栏） */
  episodes: (project: string) =>
    request<QualityEpisodesResponse>('/quality/episodes?project=' + encodeURIComponent(project)),

  /** 单集完整审片载荷（契约/分镜/参考/视频/质检） */
  review: (project: string, episode: number) =>
    request<QualityReviewResponse>(
      '/quality/review?project=' + encodeURIComponent(project) + '&episode=' + episode),

  /** 人工层状态写入（C 编辑复核 / D 发布批准） */
  setStage: (data: { project: string; episode: number; stage: string; status: string; note?: string }) =>
    request<{ success: boolean; state: Record<string, QualityStageInfo>; release: QualityReleaseInfo; stale: Record<string, string> }>(
      '/quality/stage', { method: 'POST', body: JSON.stringify(data) }),

  /** 批准预演产物 → 安排正式生产（两级生产第二阶段） */
  approvePreview: (data: { project: string; episode_no: number; note?: string }) =>
    request<{ success: boolean }>('/videos/preview/approve', { method: 'POST', body: JSON.stringify(data) }),
};

