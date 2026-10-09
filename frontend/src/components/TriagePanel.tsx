// ============================================================================
// 诊断与补救建议（TriagePanel）
// ----------------------------------------------------------------------------
// 对接 POST /api/qc/triage（本次新建）。
//
// 决策逻辑（用户 2026-10-09 拍板）：先看画面（分镜图+视频，两类都算）→ 再看逐句配音
//   → 最后看字幕校对，然后判定补救手段：
//     只有配音问题 → TTS 重合成（便宜）
//     只有画面问题 → 重出该镜
//     音画都有问题 → ⚠️ 改提示词重出（TTS 救不了画面）
//
// ⚠️ 严格「建议优先」：本组件**不自动执行任何补救**。
//    所有动作都是按钮，点了才发请求 —— 因为「音画都坏」时重配一遍音是纯浪费。
// ============================================================================
import React, { useCallback, useState } from 'react';
import { Badge, Button, Input, Skeleton, ErrorState } from '@/components/ui';
import { AlertTriangle, CheckCircle2, RefreshCw, Lightbulb, Play } from '@/components/ui/icons';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface TriageAction { label?: string; method?: string; endpoint?: string; payload?: Record<string, unknown>; cost?: string }
interface TriageResult {
  success?: boolean; project?: string; episode_no?: number; deep?: boolean; error?: string;
  visual?: { ok?: boolean; failed_count?: number; failed?: Array<{ key?: string; kind?: string; attempts?: number; issues?: string[] }> };
  audio?: { ok?: boolean; checked?: number; failed_count?: number; mode?: string; failed?: Array<{ line?: string; reason?: string[] }> };
  caption?: { available?: boolean; ok?: boolean | null; note?: string; missing?: string[]; extra?: string[]; similarity?: number | null };
  verdict?: string; remedy?: string; reason?: string; actions?: TriageAction[];
}

const VERDICT_LABEL: Record<string, string> = {
  ok: '无问题', audio_only: '仅配音有问题', visual_only: '仅画面有问题', both: '音画都有问题',
};
const COST_LABEL: Record<string, string> = { none: '只读', low: '便宜', mid: '中等', high: '昂贵（GPU）' };

export default function TriagePanel({ project, episodeNo = 1, className }: {
  project: string; episodeNo?: number; className?: string;
}) {
  const [rep, setRep] = useState<TriageResult | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [deep, setDeep] = useState(false);
  const [runMsg, setRunMsg] = useState('');

  const run = useCallback(async (deepMode: boolean) => {
    if (!project) return;
    setLoading(true); setErr(''); setRunMsg('');
    try {
      const r = await fetch('/api/qc/triage', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project, episode_no: episodeNo, deep: deepMode }),
      });
      const j = (await r.json()) as TriageResult;
      if (!r.ok || j.success === false) setErr(j.error || `HTTP ${r.status}`);
      setRep(j);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [project, episodeNo]);

  const exec = useCallback(async (a: TriageAction) => {
    if (!a.endpoint) return;
    setRunMsg('');
    try {
      const r = await fetch(a.endpoint, {
        method: a.method || 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(a.payload || {}),
      });
      const j = await r.json().catch(() => ({}));
      setRunMsg((j?.message || j?.error || (r.ok ? '已提交' : `失败 HTTP ${r.status}`)) + '');
    } catch (e) { setRunMsg('执行失败：' + (e instanceof Error ? e.message : String(e))); }
  }, []);

  const s = rep || {};
  const v = s.verdict || '';
  const okAll = v === 'ok';
  const actions = s.actions || [];

  return (
    <div className={cx('rounded-lg border border-line bg-surface', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="flex items-center gap-2 text-sm font-medium text-ink-1">
          <Lightbulb className="h-4 w-4 text-ink-3" />诊断与补救建议
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-ink-2">
            <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />
            逐句实跑（慢）
          </label>
          <Button size="sm" variant="secondary" disabled={loading || !project} onClick={() => void run(deep)}>
            <RefreshCw className={cx('mr-1 h-3.5 w-3.5', loading && 'animate-spin')} />
            {loading ? '诊断中…' : '开始诊断'}
          </Button>
        </div>
      </div>

      <div className="flex flex-col gap-3 p-3">
        {!project ? <div className="text-xs text-ink-3">请先选择项目</div> : null}
        {loading && !rep ? <Skeleton className="h-24" /> : null}
        {err && !rep ? <ErrorState description={err} onRetry={() => void run(deep)} /> : null}

        {rep && !loading ? (
          <>
            {/* 判定结论 */}
            <div className={cx('rounded-md border p-2.5', okAll ? 'border-success/40 bg-success/5' : 'border-warning/40 bg-warning/5')}>
              <div className="flex flex-wrap items-center gap-2">
                {okAll ? <CheckCircle2 className="h-4 w-4 text-success" /> : <AlertTriangle className="h-4 w-4 text-warning" />}
                <span className="text-sm font-medium text-ink-1">{VERDICT_LABEL[v] || v || '未知'}</span>
                <Badge variant={okAll ? 'success' : 'warning'}>补救：{s.remedy || '-'}</Badge>
              </div>
              {s.reason ? <p className="mt-1 text-xs text-ink-2">{s.reason}</p> : null}
            </div>

            {/* 三层体检 */}
            <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
              <div className="rounded-md border border-line bg-surface-2 p-2">
                <div className="text-xs font-medium text-ink-1">画面</div>
                <div className="mt-0.5 text-xs text-ink-3">
                  {(s.visual?.failed_count || 0) > 0 ? `失败 ${s.visual?.failed_count} 处` : '无失败记录'}
                </div>
                {(s.visual?.failed || []).slice(0, 3).map((f, i) => (
                  <div key={i} className="mt-1 text-xs text-danger">
                    {f.kind}/{f.key}（尝试{f.attempts}次）
                    <div className="text-ink-3">{String((f.issues || [])[0] || '').slice(0, 60)}</div>
                  </div>
                ))}
              </div>
              <div className="rounded-md border border-line bg-surface-2 p-2">
                <div className="text-xs font-medium text-ink-1">配音</div>
                <div className="mt-0.5 text-xs text-ink-3">
                  扫描 {s.audio?.checked || 0} 句 / 失败 {s.audio?.failed_count || 0} 处（{s.audio?.mode === 'live' ? '实跑' : '读历史'}）
                </div>
                {(s.audio?.failed || []).slice(0, 3).map((a, i) => (
                  <div key={i} className="mt-1 text-xs text-danger">{a.line}<div className="text-ink-3">{String((a.reason || [])[0] || '')}</div></div>
                ))}
              </div>
              <div className="rounded-md border border-line bg-surface-2 p-2">
                <div className="text-xs font-medium text-ink-1">字幕校对</div>
                <div className="mt-0.5 text-xs text-ink-3">
                  {!s.caption?.available ? '环境不可用' : s.caption?.ok === null ? (s.caption?.note || '尚未校对') : s.caption?.ok ? '通过' : '不通过'}
                </div>
                {(s.caption?.missing || []).slice(0, 3).map((m, i) => <div key={i} className="mt-1 text-xs text-danger">缺：{String(m).slice(0, 50)}</div>)}
                {(s.caption?.extra || []).slice(0, 3).map((m, i) => <div key={i} className="mt-1 text-xs text-warning-strong">多：{String(m).slice(0, 50)}</div>)}
              </div>
            </div>

            {/* 建议动作（点了才执行） */}
            {actions.length > 0 ? (
              <div>
                <div className="mb-1.5 text-xs font-medium text-ink-1">
                  建议动作 {actions.length} 条 · <span className="font-normal text-ink-3">点了才会执行，不会自动跑</span>
                </div>
                <div className="flex flex-col gap-1">
                  {actions.map((a, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface-2 px-2 py-1.5">
                      <Badge variant={a.cost === 'high' ? 'danger' : a.cost === 'none' ? 'default' : 'info'}>
                        {COST_LABEL[a.cost || ''] || a.cost || '-'}
                      </Badge>
                      <span className="min-w-0 flex-1 text-xs text-ink-2">{a.label}</span>
                      <span className="font-mono text-xs text-ink-3">{a.endpoint}</span>
                      <Button size="sm" variant="secondary" onClick={() => void exec(a)}>
                        <Play className="mr-1 h-3 w-3" />执行
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            {runMsg ? <div className="text-xs text-info-strong">{runMsg}</div> : null}
          </>
        ) : null}

        {!rep && !loading && !err && project ? (
          <div className="text-xs text-ink-3">
            点「开始诊断」后：读画面质检历史 + 逐句配音 + 字幕校对，判定该用哪种补救手段。
            「逐句实跑」会真的跑 ffmpeg 逐句检查（慢，但能发现历史里没有的新问题）。
          </div>
        ) : null}
      </div>
    </div>
  );
}
