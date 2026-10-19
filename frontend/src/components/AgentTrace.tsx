import { useState, useEffect } from 'react';
import { t } from '@/i18n';
import { AlertTriangle, Check, X } from '@/components/ui/icons';
import type { AgentStep } from '@/types';
import { FOCUS_RING } from '@/components/ui/focusRing';

// 工具名 → 人话。用户在总控面板看到的应该是「生产一集」而不是 `produce_episode`。
// `t()` 找不到键时会**原样返回 key**，所以缺映射时回退到工具名本身（不显示 `toolLabel.xxx`）。
function toolLabel(t: (k: string, p?: Record<string, string | number>) => string, name: string): string {
  const key = `toolLabel.${name}`;
  const hit = t(key);
  return hit === key ? name : hit;
}

/**
 * 总控 Agent 执行轨迹（2026-10-16 E-5：从 ProjectWorkbenchPage 抽出，逐字搬运，零行为变更）。
 * 展示一次总控运行的工具调用时间线；`live=true` 时每秒重渲染让耗时走秒。
 */
export function AgentTrace({ steps, status, startedAt, live = false }: {
  steps: AgentStep[];
  status: string;
  startedAt?: number;
  live?: boolean;
}) {
  const [open, setOpen] = useState(true);
  // 执行中每秒重渲染一次，让标题里的耗时走秒
  const [, tick] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => tick(v => v + 1), 1000);
    return () => window.clearInterval(id);
  }, [live]);

  const running = status === 'running';
  const failed = steps.some((s) => !s.ok && !s.blocked);
  const elapsed = live && startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : null;
  const titleKey =
    running ? 'live.agentRunning'
    : status === 'failed' ? 'live.agentRunFailed'
    : status === 'killed' ? 'live.agentRunKilled'
    : status === 'timeout' ? 'live.agentTimeout'
    : 'live.agentRunDone';

  return (
    <div className={`mr-6 rounded-lg border bg-surface ${running ? 'glow-brand border-brand/30' : 'border-line'}`}>
      <div className="flex items-center gap-2 px-3 py-2 text-xs">
        {running ? (
          <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-brand/40 border-t-brand" aria-hidden="true" />
        ) : (
          <span className={`h-2 w-2 shrink-0 rounded-full ${failed ? 'bg-danger' : 'bg-success'}`} aria-hidden="true" />
        )}
        <span className={`shrink-0 font-medium tabular-nums ${running ? 'text-brand' : 'text-ink-2'}`}>
          {t(titleKey, { n: steps.length, s: elapsed ?? 0 })}
        </span>
        {!running && (
          <button
            type="button"
            onClick={() => setOpen(v => !v)}
            aria-expanded={open}
            title={open ? t('live.collapse') : t('live.expand')}
            className={`ml-auto rounded p-0.5 text-ink-3 transition-colors hover:text-ink-1 ${FOCUS_RING}`}
          >
            <svg
              className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
        )}
      </div>
      {open && (
        <ol className="relative mx-3 mb-3 space-y-2 border-l border-line pl-3.5">
          {steps.map((s, i) => {
            const isCurrent = live && i === steps.length - 1;
            return (
              <li key={i} className="relative text-xs leading-snug">
                <span
                  className={`absolute -left-[22px] top-0 flex h-3.5 w-3.5 items-center justify-center rounded-full border bg-surface ${
                    s.blocked
                      ? 'border-warning/50 text-warning-strong'
                      : s.ok
                        ? 'border-success/50 text-success-strong'
                        : 'border-danger/50 text-danger-strong'
                  }`}
                  aria-hidden="true"
                >
                  {s.blocked
                    ? <AlertTriangle className="h-2.5 w-2.5" />
                    : s.ok
                      ? <Check className="h-2.5 w-2.5" />
                      : <X className="h-2.5 w-2.5" />}
                </span>
                <div className={isCurrent ? 'rounded-md bg-brand-subtle/40 px-1.5 py-1' : ''}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded bg-brand-subtle px-1.5 py-0.5 font-medium text-brand" title={s.tool}>
                      {toolLabel(t, s.tool)}
                    </span>
                    {s.elapsed_sec != null && (
                      <span className="tabular-nums text-ink-3">{t('live.elapsed', { s: Math.round(s.elapsed_sec) })}</span>
                    )}
                  </div>
                  <p className="mt-0.5 break-all text-ink-2">
                    {s.summary}
                    {s.cached ? t('chat.cached') : ''}
                  </p>
                </div>
              </li>
            );
          })}
          {running && (
            <li className="relative text-xs text-ink-3">
              <span
                className="absolute -left-[22px] top-0 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-brand/40 bg-surface"
                aria-hidden="true"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
              </span>
              {t('live.waitingNext')}
            </li>
          )}
        </ol>
      )}
    </div>
  );
}
