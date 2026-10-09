// ============================================================================
// 质量面板（QualityPanel）：覆盖率 / 字幕校对 / 一致性 / 连贯性
// ----------------------------------------------------------------------------
// 补齐四组此前前端未接的后端能力（响应结构均按**实测**对齐，非按文档猜测）：
//   GET  /api/coverage/<novel_id>             → { episodes:[{episode_no,
//        coverage_percent, detail_coverage_percent, detail_passed, checked_at, ...}] }
//   GET  /api/caption_verify/status?project=&episode_no=  → { available, reasons[], result }
//   POST /api/caption_verify/run   {project, episode_no}
//   POST /api/consistency/run      {project_name, episode_no}
//   GET  /api/consistency/report/<project>   → 报告，或 {success:false,error:'暂无…'}
//   GET  /api/continuity/<novel_id>          → { bible:{characters:[…]}, … }
// ⚠️ coverage / continuity 用 novel_id；caption_verify / consistency 用项目名。
// ============================================================================
import React, { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { BarChart3, Mic, ClipboardCheck, BookOpen, Play, RefreshCw, AlertTriangle } from '@/components/ui/icons';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface CovEp {
  episode_no?: number; available?: boolean;
  coverage_percent?: number; detail_coverage_percent?: number; detail_passed?: boolean;
  detail_threshold_percent?: number; checked_at?: string; body_char_count?: number;
  covered_char_count?: number; episode_duration_sec?: number;
}
interface CaptionStatus {
  available?: boolean; reasons?: string[]; exe?: string; model?: string;
  result?: { status?: string; done?: boolean; message?: string; [k: string]: unknown } | null;
}
interface BibleChar { name?: string; gender?: string; identity?: string; current_outfit?: string; locked?: boolean }
interface Continuity { bible?: { characters?: BibleChar[] }; [k: string]: unknown }

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url);
  return (await r.json()) as T;
}
async function jpost<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok || j?.success === false) throw new Error(j?.error || j?.message || `HTTP ${r.status}`);
  return j as T;
}

function Bar({ pct }: { pct: number }) {
  const v = Math.max(0, Math.min(100, pct));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-line">
      <div className={cx('h-full rounded-full', v >= 85 ? 'bg-success' : v >= 70 ? 'bg-warning' : 'bg-danger')}
           style={{ width: `${v}%` }} />
    </div>
  );
}

function Section({ icon, title, right, children }: {
  icon: React.ReactNode; title: string; right?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <div className="flex items-center gap-2 text-sm font-medium text-ink-1">
          <span className="text-ink-3">{icon}</span>{title}
        </div>
        {right}
      </div>
      <div className="p-3">{children}</div>
    </div>
  );
}

export default function QualityPanel({ project, className }: { project: string; className?: string }) {
  const [novelId, setNovelId] = useState('');
  const [cov, setCov] = useState<CovEp[]>([]);
  const [cap, setCap] = useState<CaptionStatus | null>(null);
  const [cont, setCont] = useState<Continuity | null>(null);
  const [cons, setCons] = useState<string>('');
  const [consErr, setConsErr] = useState('');
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');

  const load = useCallback(async () => {
    if (!project) return;
    setErr('');
    try {
      // ① novel_id（coverage / continuity 用它，不用项目名）
      const nv = await jget<{ novels?: Array<{ novel_id?: string; project_key?: string; project_name?: string }> }>('/api/novels');
      const hit = (nv.novels || []).find((n) => n.project_key === project || n.project_name === project);
      const nid = hit?.novel_id || '';
      setNovelId(nid);
      // ② 字幕校对环境 + ③ 一致性报告（都用项目名）
      const [c1, c3] = await Promise.all([
        jget<CaptionStatus>(`/api/caption_verify/status?project=${encodeURIComponent(project)}&episode_no=1`).catch(() => null),
        jget<{ success?: boolean; error?: string }>(`/api/consistency/report/${encodeURIComponent(project)}`).catch(() => null),
      ]);
      setCap(c1);
      setCons(c3 && c3.success ? JSON.stringify(c3, null, 2) : '');
      setConsErr(c3 && !c3.success ? (c3.error || '') : '');
      // ④ 覆盖率 + ⑤ 连贯性（用 novel_id）
      if (nid) {
        const [c2, c4] = await Promise.all([
          jget<{ episodes?: CovEp[] }>(`/api/coverage/${encodeURIComponent(nid)}`).catch(() => null),
          jget<Continuity>(`/api/continuity/${encodeURIComponent(nid)}`).catch(() => null),
        ]);
        setCov(c2?.episodes || []);
        setCont(c4);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally { setLoading(false); }
  }, [project]);

  useEffect(() => { setLoading(true); void load(); }, [load]);

  const runCaption = async () => {
    setBusy('caption'); setErr('');
    try { await jpost('/api/caption_verify/run', { project, episode_no: 1 }); await load(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(''); }
  };
  const runConsistency = async () => {
    setBusy('consistency'); setErr('');
    try { await jpost('/api/consistency/run', { project_name: project }); await load(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(''); }
  };

  if (!project) return <EmptyState title="请先选择项目" />;
  if (loading) return <Skeleton className="h-40" />;

  const chars = cont?.bible?.characters || [];
  const capOk = Boolean(cap?.available);

  return (
    <div className={cx('flex flex-col gap-3', className)}>
      {/* 原文覆盖率 */}
      <Section icon={<BarChart3 className="h-4 w-4" />} title="原文覆盖率"
        right={<Button size="sm" variant="ghost" onClick={() => void load()}><RefreshCw className="h-3.5 w-3.5" /></Button>}>
        {cov.length === 0 ? (
          <div className="text-xs text-ink-3">还没有覆盖率数据（生成剧本后自动记录）。</div>
        ) : (
          <div className="flex flex-col gap-2.5">
            {cov.map((e) => (
              <div key={String(e.episode_no)} className="flex items-center gap-3">
                <span className="w-14 shrink-0 text-xs text-ink-2">第 {e.episode_no} 集</span>
                <div className="min-w-0 flex-1"><Bar pct={e.detail_coverage_percent ?? e.coverage_percent ?? 0} /></div>
                <span className="w-14 shrink-0 text-right font-mono text-xs tabular-nums text-ink-2">
                  {(e.detail_coverage_percent ?? e.coverage_percent ?? 0).toFixed(1)}%
                </span>
                <span className="w-16 shrink-0 text-right">
                  {e.detail_passed ? <Badge variant="success">达标</Badge> : <Badge variant="warning">偏低</Badge>}
                </span>
              </div>
            ))}
            <div className="text-xs text-ink-3">
              阈值 {cov[0]?.detail_threshold_percent ?? '—'}%；覆盖字数 {cov.reduce((a, e) => a + (e.covered_char_count || 0), 0)} / 原文 {cov.reduce((a, e) => a + (e.body_char_count || 0), 0)}
            </div>
          </div>
        )}
      </Section>

      {/* 字幕校对 */}
      <Section icon={<Mic className="h-4 w-4" />} title="字幕校对"
        right={<Button size="sm" variant={capOk ? 'brand' : 'ghost'} disabled={!capOk} loading={busy === 'caption'} onClick={() => void runCaption()}>
          <Play className="mr-1.5 h-3.5 w-3.5" />开始校对</Button>}>
        {capOk ? (
          <div className="text-xs text-ink-2">环境就绪{cap?.model ? ` · 模型 ${cap.model}` : ''}</div>
        ) : (
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-1.5 text-xs font-medium text-warning">
              <AlertTriangle className="h-3.5 w-3.5" />校对环境未就绪，无法启动
            </div>
            <ul className="ml-4 list-disc space-y-0.5 text-xs text-ink-3">
              {(cap?.reasons || ['未返回原因']).map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </div>
        )}
      </Section>

      {/* 一致性校验 */}
      <Section icon={<ClipboardCheck className="h-4 w-4" />} title="跨集一致性校验"
        right={<Button size="sm" variant="secondary" loading={busy === 'consistency'} onClick={() => void runConsistency()}>
          <Play className="mr-1.5 h-3.5 w-3.5" />执行校验</Button>}>
        {cons ? (
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-surface-2 p-2 text-xs leading-relaxed text-ink-2">{cons}</pre>
        ) : (
          <div className="text-xs text-ink-3">{consErr || '暂无一致性报告，点「执行校验」生成。'}</div>
        )}
      </Section>

      {/* 连贯性 */}
      <Section icon={<BookOpen className="h-4 w-4" />} title="项目连贯性（人物锁定状态）">
        {chars.length === 0 ? (
          <div className="text-xs text-ink-3">暂无连贯性数据。</div>
        ) : (
          <div className="flex flex-col gap-2">
            {chars.map((c, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span className={cx('h-1.5 w-1.5 shrink-0 rounded-full', c.locked ? 'bg-success' : 'bg-ink-3')} />
                <span className="w-20 shrink-0 truncate font-medium text-ink-1">{c.name}</span>
                <span className="w-20 shrink-0 truncate text-ink-3">{c.identity || c.gender || ''}</span>
                <span className="min-w-0 flex-1 truncate text-ink-2">{c.current_outfit || ''}</span>
                {c.locked ? <Badge variant="success">已锁定</Badge> : <Badge variant="default">未锁定</Badge>}
              </div>
            ))}
          </div>
        )}
      </Section>

      {err ? <ErrorState description={err} onRetry={() => void load()} /> : null}
    </div>
  );
}
