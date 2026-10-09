// ============================================================================
// 总控 AI · 实时执行流（LiveExecutionFeed）
// ----------------------------------------------------------------------------
// 用户诉求：「无法从总控 AI 看到具体在执行什么 —— 想要能清晰看到正在执行什么、
// 在思考什么」。
//
// 数据源：GET /api/autopilot/live_feed?project=&episode_no=&tail=
//   后端把三处零散数据归一成一条时间序列（见 routes/autopilot.py 的 live_feed）：
//     now      —— 此刻在做什么（步骤/阶段/第几个·共几个/进度）
//     steps    —— 本集五阶段状态（待跑/进行中/已完成/跳过）
//     stream   —— 时间线；kind ∈ action | thinking | result | warn
//     counters —— 本轮 完成/失败/跳过 计数
//
// ⚠️ 「思考」类条目全部来自系统真实产出（提示词预检结论、自愈项、质检判据、
//    重试原因、模型实际收到的提示词原文），**绝不编造推理文字**。
// ============================================================================
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { Brain, BarChart3, AlertTriangle, CheckCircle2, Play, ChevronDown, ChevronRight } from '@/components/ui/icons';

const POLL_MS = 2000;

const cx = (...parts: Array<string | false | null | undefined>): string =>
  parts.filter(Boolean).join(' ');

type Kind = 'action' | 'thinking' | 'result' | 'warn';

interface FeedItem {
  t: string;
  kind: Kind;
  title: string;
  detail: string;
  meta: Record<string, unknown>;
}

interface NowState {
  task_id?: string;
  label?: string;
  status?: string;
  phase?: string;
  current?: number | null;
  total?: number | null;
  progress?: number | null;
  asset?: string;
  shot?: number | null;
  attempt?: number | null;
  prompt?: string;
  done?: boolean;
}

interface StepItem { step: string; label: string; state: string }

interface LiveFeed {
  success: boolean;
  now?: NowState;
  steps?: StepItem[];
  stream?: FeedItem[];
  counters?: { done?: number; failed?: number; skipped?: number };
  sources?: { episode_log?: boolean; live_task?: boolean };
  error?: string;
}

// ---- 事件类型 → 视觉语义（全部走语义色 token，不写 hex / dark:）------------
const KIND_STYLE: Record<Kind, { icon: React.ReactNode; dot: string; chip: string; label: string }> = {
  action:   { icon: <Play className="h-3.5 w-3.5" />,          dot: 'bg-info',    chip: 'border-info/40 bg-info/10 text-info',       label: '执行' },
  thinking: { icon: <Brain className="h-3.5 w-3.5" />,         dot: 'bg-accent',  chip: 'border-accent/40 bg-accent/10 text-accent', label: '思考' },
  result:   { icon: <CheckCircle2 className="h-3.5 w-3.5" />,   dot: 'bg-success', chip: 'border-success/40 bg-success/10 text-success', label: '完成' },
  warn:     { icon: <AlertTriangle className="h-3.5 w-3.5" />,  dot: 'bg-warning', chip: 'border-warning/40 bg-warning/10 text-warning', label: '告警' },
};

const STEP_DOT: Record<string, string> = {
  done: 'bg-success',
  running: 'bg-brand',
  skipped: 'bg-ink-3',
  failed: 'bg-danger',
  pending: 'bg-line-strong',
};

function StepChip({ s }: { s: StepItem }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2 py-1 text-xs text-ink-2">
      <span className={cx('h-1.5 w-1.5 rounded-full', STEP_DOT[s.state] || 'bg-line-strong')} />
      {s.label}
    </span>
  );
}

function StreamRow({ it, defaultOpen = false }: { it: FeedItem; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const st = KIND_STYLE[it.kind] || KIND_STYLE.action;
  const hasDetail = Boolean(it.detail);
  return (
    <li className="group flex gap-2 px-2 py-1.5 rounded-md hover:bg-surface-2">
      <span className="mt-[3px] flex h-4 w-4 shrink-0 items-center justify-center text-ink-3">
        <span className={cx('h-1.5 w-1.5 rounded-full', st.dot)} />
      </span>
      <span className="mt-[2px] w-[62px] shrink-0 font-mono text-xs tabular-nums text-ink-3">{it.t}</span>
      <span className={cx('mt-[1px] h-[18px] shrink-0 rounded border px-1.5 text-xs leading-[16px]', st.chip)}>
        {st.label}
      </span>
      <span className="min-w-0 flex-1">
        <button
          type="button"
          onClick={() => hasDetail && setOpen((v) => !v)}
          className={cx('flex w-full items-start gap-1 text-left text-xs text-ink-1',
                        hasDetail ? 'cursor-pointer' : 'cursor-default')}
        >
          {hasDetail ? (open ? <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-ink-3" />
                              : <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-ink-3" />) : null}
          <span className="min-w-0 flex-1 break-words">{it.title}</span>
        </button>
        {hasDetail && open ? (
          <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-surface-2 p-2 text-xs leading-relaxed text-ink-2">
            {it.detail}
          </pre>
        ) : null}
        {hasDetail && !open ? (
          <div className="mt-0.5 truncate text-xs text-ink-3">{it.detail}</div>
        ) : null}
      </span>
    </li>
  );
}

export default function LiveExecutionFeed({
  project,
  episodeNo = 1,
  className,
}: {
  project: string;
  episodeNo?: number;
  className?: string;
}) {
  const [feed, setFeed] = useState<LiveFeed | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    if (!project) return;
    try {
      const q = new URLSearchParams({ project, episode_no: String(episodeNo), tail: '200' });
      const r = await fetch(`/api/autopilot/live_feed?${q.toString()}`);
      const j = (await r.json()) as LiveFeed;
      if (!j.success) { setErr(j.error || '读取执行流失败'); return; }
      setFeed(j);
      setErr('');
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [project, episodeNo]);

  useEffect(() => {
    setLoading(true);
    void load();
    timer.current = window.setInterval(() => { void load(); }, POLL_MS);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [load]);

  // 只在用户停在顶部时自动跟随（避免打断向上翻阅）
  useEffect(() => {
    const el = scrollRef.current;
    if (el && autoScroll) el.scrollTop = 0;
  }, [feed, autoScroll]);

  if (!project) return <EmptyState title="请先选择项目" />;
  if (loading && !feed) return <div className="flex flex-col gap-2"><Skeleton className="h-20" /><Skeleton className="h-40" /></div>;
  if (err && !feed) return <ErrorState description={err} onRetry={() => void load()} />;

  const now = feed?.now || {};
  const stream = feed?.stream || [];
  const counters = feed?.counters || {};
  const running = Boolean(now.task_id) && !now.done;
  const pct = typeof now.progress === 'number' ? Math.max(0, Math.min(100, now.progress)) : null;

  return (
    <div className={cx('flex flex-col gap-3', className)}>
      {/* ── 此刻在做什么 ───────────────────────────────────────────── */}
      <div className="rounded-lg border border-line bg-surface-2 p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className={cx('h-2 w-2 shrink-0 rounded-full',
              running ? 'animate-pulse bg-brand' : (feed?.sources?.live_task ? 'bg-success' : 'bg-ink-3'))} />
            <span className="truncate text-sm font-medium text-ink-1">
              {running ? (now.phase || now.label || '执行中') : '总控待命（当前无任务）'}
            </span>
            {typeof now.attempt === 'number' && now.attempt > 0 ? (
              <Badge variant="warning">第 {now.attempt + 1} 次尝试</Badge>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {typeof counters.done === 'number' && counters.done > 0 ? <Badge variant="success">完成 {counters.done}</Badge> : null}
            {typeof counters.failed === 'number' && counters.failed > 0 ? <Badge variant="danger">失败 {counters.failed}</Badge> : null}
            {typeof counters.skipped === 'number' && counters.skipped > 0 ? <Badge variant="default">跳过 {counters.skipped}</Badge> : null}
          </div>
        </div>
        {pct !== null ? (
          <div className="mb-2 h-1.5 w-full overflow-hidden rounded-full bg-line">
            <div className="h-full rounded-full bg-brand transition-[width] duration-500" style={{ width: `${pct}%` }} />
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-3">
          {now.label ? <span>任务：{now.label}</span> : null}
          {now.phase ? <span>阶段：{now.phase}</span> : null}
          {now.asset ? <span>当前：{now.asset}</span> : null}
          {typeof now.shot === 'number' ? (
            <span>镜头 {now.shot}{typeof now.current === 'number' && typeof now.total === 'number' && now.total > 0 ? `（${now.current}/${now.total}）` : ''}</span>
          ) : (typeof now.current === 'number' && typeof now.total === 'number' && now.total > 0 ? <span>进度 {now.current}/{now.total}</span> : null)}
          {feed?.sources?.episode_log ? <span>日志已接入</span> : <span>暂无本集日志</span>}
        </div>
        {(feed?.steps?.length || 0) > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {feed!.steps!.map((s) => <StepChip key={s.step} s={s} />)}
          </div>
        ) : null}
      </div>

      {/* ── 执行流时间线 ───────────────────────────────────────────── */}
      <div className="rounded-lg border border-line bg-surface">
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-medium text-ink-1">
            <BarChart3 className="h-4 w-4 text-ink-3" />
            执行流
            <span className="text-xs font-normal text-ink-3">{stream.length} 条</span>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" onClick={() => setAutoScroll((v) => !v)}>
              {autoScroll ? '跟随最新' : '已暂停跟随'}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void load()}>刷新</Button>
          </div>
        </div>
        <div ref={scrollRef} className="max-h-[460px] overflow-auto p-1.5">
          {stream.length === 0 ? (
            <div className="px-2 py-8 text-center text-xs text-ink-3">
              本集还没有可展示的执行事件。启动生产后，这里会实时显示每一步动作与判断依据。
            </div>
          ) : (
            <ol className="flex flex-col">
              {stream.map((it, i) => (
                <StreamRow key={`${it.t}-${i}`} it={it} defaultOpen={it.kind === 'thinking' && i < 2} />
              ))}
            </ol>
          )}
        </div>
      </div>
      {err ? <div className="text-xs text-warning">轮询异常（将自动重试）：{err}</div> : null}
    </div>
  );
}
