// ============================================================================
// 失败码看板（FailurePanel，E-2 前端）
// ----------------------------------------------------------------------------
// 把死信队列按**结构化失败码**归类展示 —— 此前 `failure_codes` 只在 pipeline
// 写死信时打 code/code_label，前端看不到「这一周最常见的是哪类失败」。
// 后端已提供：GET /api/analytics/failure_summary?project=<可选>
//   → {success, open_dead_letters, summary:{counts,top,unknown}, codes:[{code,label,count}]}
// 只读展示，不改任何写路径；取数/渲染全程 fail-open（失败显示 ErrorState）。
// ============================================================================
import React, { useCallback, useEffect, useState } from 'react';
import { Badge, EmptyState, ErrorState, Skeleton } from '@/components/ui';
import { AlertTriangle, RefreshCw } from '@/components/ui/icons';

interface CodeRow { code?: string; label?: string; count?: number }
interface Summary { total?: number; top?: string; counts?: Record<string, number>; unknown?: number }
interface Resp {
  success?: boolean;
  project?: string;
  open_dead_letters?: number;
  summary?: Summary;
  codes?: CodeRow[];
  error?: string;
}

/** 码 → 配色（危险/警告/信息）；未登记的码走 default。 */
const SEV: Record<string, 'default' | 'success' | 'warning' | 'danger' | 'info'> = {
  'F-OOM': 'danger', 'F-DEPS': 'danger', 'F-SUBMIT-API': 'danger',
  'F-QC-BLOCKED': 'warning', 'F-REF-MISSING': 'warning',
  'F-INTERRUPT': 'info', 'F-TIMEOUT': 'info', 'F-SUBMIT-NET': 'info',
};

export default function FailurePanel({ project }: { project?: string }) {
  const [data, setData] = useState<Resp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setErr('');
    try {
      const qs = project ? `?project=${encodeURIComponent(project)}` : '';
      const r = await fetch(`/api/analytics/failure_summary${qs}`);
      const j = (await r.json()) as Resp;
      if (!r.ok || j.success === false) {
        setErr(j.error || `读取失败（HTTP ${r.status}）`);
        setData(null);
      } else {
        setData(j);
      }
    } catch (e) {
      setErr(String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [project]);

  useEffect(() => { load(); }, [load]);

  const codes = (data?.codes || []).filter((c) => (c.count || 0) > 0);
  const open = data?.open_dead_letters || 0;
  const maxCount = Math.max(1, ...codes.map((c) => c.count || 0));

  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-ink-1">
          <AlertTriangle className="h-4 w-4" />
          <span className="font-medium">失败原因看板</span>
          {open > 0 && <Badge variant="danger">未解决 {open}</Badge>}
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          title="刷新"
          aria-label="刷新失败原因看板"
          className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-line text-ink-2 transition-colors hover:bg-surface-2 disabled:opacity-40"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {loading && !data ? (
        <Skeleton className="h-16 w-full" />
      ) : err ? (
        <ErrorState description={err} onRetry={load} />
      ) : codes.length === 0 ? (
        <EmptyState title="暂无未解决的失败" description="死信队列为空，或失败原因尚未归类。" />
      ) : (
        <div className="space-y-2">
          {codes.map((c) => (
            <div key={c.code || c.label || ''} className="flex items-center gap-2">
              <Badge variant={SEV[c.code || ''] || 'default'}>{c.code}</Badge>
              <span className="flex-1 truncate text-xs text-ink-2" title={c.label}>{c.label}</span>
              <div className="h-1.5 w-24 overflow-hidden rounded bg-surface-2">
                <div className="h-full rounded bg-danger"
                     style={{ width: `${((c.count || 0) / maxCount) * 100}%` }} />
              </div>
              <span className="w-6 text-right text-xs tabular-nums text-ink-1">{c.count}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
