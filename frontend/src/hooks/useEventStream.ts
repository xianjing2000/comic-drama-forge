import { useEffect, useState } from 'react';
import { subscribeEvents, getSnapshot, isConnected, type StreamSnapshot } from '@/api/events';

export type { StreamTask, StreamSnapshot } from '@/api/events';

/**
 * 订阅后端 SSE 进度流（E-1，2026-10-16）。
 *
 * 复用 `@/api/events` 的**单例连接**（整个应用只开一条 EventSource），避免每个组件各连一条。
 *
 * 用法（把某个 task 的轮询换成订阅）：
 * ```tsx
 * const { snapshot, connected } = useEventStream();
 * const task = snapshot?.tasks.find((t) => t.task_id === taskId);
 * // task?.status / task?.phase / task?.progress / task?.live …
 * ```
 * 若环境不支持 EventSource 或连接失败，`connected=false`，调用方可回退到原有轮询。
 */
export function useEventStream(): { snapshot: StreamSnapshot | null; connected: boolean } {
  const [snapshot, setSnapshot] = useState<StreamSnapshot | null>(getSnapshot());
  const [connected, setConnected] = useState<boolean>(isConnected());

  useEffect(() => {
    const unsub = subscribeEvents((s) => setSnapshot(s));
    // 连接态没有事件回调，用轻量定时读（1s）镜像；未连接时调用方回退轮询。
    const t = setInterval(() => setConnected(isConnected()), 1000);
    return () => {
      unsub();
      clearInterval(t);
    };
  }, []);

  return { snapshot, connected };
}
