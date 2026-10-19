/**
 * SSE 事件流单例客户端（E-1，2026-10-16）。
 *
 * 为什么是单例而不是每个组件各开一个 EventSource：整块工作台要消费同一份「任务进度」，
 * 每个组件各连一条 SSE 会让后端的 `stream_with_context` 生成器成倍增加。这里维护**唯一**
 * 连接 + 最新快照，供 React hook 与命令式代码共用。
 *
 * 配套后端：`app/routes/status_api.py` 的 `GET /api/events/stream`（推 `job_state.generation_state`
 * 快照）。断线由浏览器 `EventSource` 自动重连（约 3s）；本模块只镜像连接态。
 */

export interface StreamTask {
  task_id: string;
  status?: string;
  progress?: number;
  total?: number;
  phase?: string;
  project_name?: string;
  step?: string;
  episode_no?: number;
  error?: string;
  /** 分镜重试循环「当前正在用的提示词/镜头」快照，用于实时渲染 */
  live?: any;
}

export interface StreamSnapshot {
  tasks: StreamTask[];
  count: number;
  ts: number;
}

type Cb = (s: StreamSnapshot) => void;

let es: EventSource | null = null;
let latest: StreamSnapshot | null = null;
let connected = false;
const subs = new Set<Cb>();

function ensure(): void {
  if (typeof window === 'undefined' || typeof EventSource === 'undefined') return;
  if (es) return;
  try {
    es = new EventSource('/api/events/stream');
  } catch {
    es = null;
    return;
  }
  es.onopen = () => { connected = true; };
  es.onerror = () => { connected = false; }; // 浏览器会自动重连
  es.onmessage = (ev: MessageEvent) => {
    try {
      latest = JSON.parse(ev.data) as StreamSnapshot;
    } catch {
      return;
    }
    subs.forEach((f) => { try { f(latest as StreamSnapshot); } catch { /* 单个订阅者异常不影响其它 */ } });
  };
}

/** 订阅事件流；返回取消订阅函数。会立即用当前快照回调一次（若有）。 */
export function subscribeEvents(cb: Cb): () => void {
  ensure();
  subs.add(cb);
  if (latest) { try { cb(latest); } catch { /* ignore */ } }
  return () => { subs.delete(cb); };
}

/** 最新快照（可能为 null）。 */
export function getSnapshot(): StreamSnapshot | null { return latest; }

/** SSE 是否已连接（未连接时调用方应回退到轮询）。 */
export function isConnected(): boolean { return connected; }

/** 在最新快照里取某任务。 */
export function findTask(taskId: string): StreamTask | null {
  return latest?.tasks?.find((t) => t.task_id === taskId) ?? null;
}
