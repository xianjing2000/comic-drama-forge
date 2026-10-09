// ============================================================================
// 导出与交付面板（ExportPanel）
// ----------------------------------------------------------------------------
// 补齐「生成 → 质检 → 交付」闭环的最后一段（响应结构均按实测对齐）：
//   GET  /api/autopilot/deliverables?project=   → {success,count,items:[],pending}
//   POST /api/export/run    {project_name, episode_no?, formats:[jianying|fcpxml|srt|frames]}
//   GET  /api/export/list?project=              → {success,count,items:[],files:[]}
//   GET  /api/export/download/<filename>
//   GET  /api/final/<filename>?download=1       成片下载（支持 Range 内联播放）
// ============================================================================
import React, { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { Download, Film, FolderOpen, Share2, RefreshCw, Play, AlertTriangle } from '@/components/ui/icons';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface ExportItem { filename?: string; name?: string; path?: string; format?: string; created_at?: string; size?: number; [k: string]: unknown }

const FORMATS = [['jianying', '剪映草稿'], ['fcpxml', 'FCPXML'], ['srt', 'SRT 字幕'], ['frames', '帧序列清单']] as const;

async function jget<T>(u: string): Promise<T> { return (await (await fetch(u)).json()) as T; }
async function jpost<T>(u: string, b: unknown): Promise<T> {
  const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
  const j = await r.json();
  if (!r.ok || j?.success === false) throw new Error(j?.error || j?.message || `HTTP ${r.status}`);
  return j as T;
}
function Section({ icon, title, right, children }: { icon: React.ReactNode; title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <div className="flex items-center gap-2 text-sm font-medium text-ink-1"><span className="text-ink-3">{icon}</span>{title}</div>
        {right}
      </div>
      <div className="p-3">{children}</div>
    </div>
  );
}
function FileRow({ label, href, sub }: { label: string; href: string; sub?: string }) {
  return (
    <div className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-surface-2">
      <FolderOpen className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      <span className="min-w-0 flex-1 truncate text-xs text-ink-1">{label}</span>
      {sub ? <span className="shrink-0 text-xs text-ink-3">{sub}</span> : null}
      <a className="inline-flex h-7 shrink-0 items-center rounded-md border border-line px-2 text-xs text-ink-2 hover:bg-surface-2" href={href} target="_blank" rel="noreferrer">
        <Download className="mr-1 h-3 w-3" />下载
      </a>
    </div>
  );
}

export default function ExportPanel({ project, episodeNo = 1, className }: { project: string; episodeNo?: number; className?: string }) {
  const [exports_, setExports] = useState<ExportItem[]>([]);
  const [picked, setPicked] = useState<string[]>(['jianying', 'srt']);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!project) return;
    setErr('');
    try {
      const q = encodeURIComponent(project);
      const [e] = await Promise.all([
        jget<{ items?: ExportItem[] }>(`/api/export/list?project=${q}`).catch(() => null),
      ]);
      setExports(e?.items || []);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [project]);

  useEffect(() => { setLoading(true); void load(); }, [load]);

  const doExport = async () => {
    setBusy('export'); setErr(''); setMsg('');
    try {
      const r = await jpost<{ files?: unknown[]; count?: number }>('/api/export/run',
        { project_name: project, episode_no: episodeNo, formats: picked });
      setMsg(`导出完成，共 ${r?.count ?? (r?.files || []).length} 个文件`);
      await load();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(''); }
  };
  const doFinal = async () => {
    setBusy('final'); setErr(''); setMsg('');
    try { await jpost('/api/final/video', { project_name: project, episode_no: episodeNo }); setMsg('成片合成已提交'); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(''); }
  };

  if (!project) return <EmptyState title="请先选择项目" />;
  if (loading) return <Skeleton className="h-36" />;

  return (
    <div className={cx('flex flex-col gap-3', className)}>
      <Section icon={<Share2 className="h-4 w-4" />} title="导出（剪辑软件 / 字幕）"
        right={<Button size="sm" variant="brand" loading={busy === 'export'} onClick={() => void doExport()}>
          <Download className="mr-1.5 h-3.5 w-3.5" />导出本集</Button>}>
        <div className="mb-3 flex flex-wrap gap-x-4 gap-y-2">
          {FORMATS.map(([k, label]) => (
            <label key={k} className="flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
              <input type="checkbox" className="h-3.5 w-3.5 accent-brand" checked={picked.includes(k)}
                onChange={(e) => setPicked((v) => e.target.checked ? [...v, k] : v.filter((x) => x !== k))} />
              {label}
            </label>
          ))}
        </div>
        {exports_.length === 0 ? (
          <div className="text-xs text-ink-3">还没有导出记录。</div>
        ) : (
          <div className="flex flex-col gap-0.5">
            {exports_.map((it, i) => {
              const fn = String(it.filename || it.name || it.path || '');
              return <FileRow key={i} label={fn} href={`/api/export/download/${encodeURIComponent(fn)}`}
                              sub={it.format ? String(it.format) : undefined} />;
            })}
          </div>
        )}
      </Section>

      <Section icon={<Film className="h-4 w-4" />} title="成片"
        right={<Button size="sm" variant="secondary" loading={busy === 'final'} onClick={() => void doFinal()}>
          <Play className="mr-1.5 h-3.5 w-3.5" />合成成片</Button>}>
        <div className="flex items-center gap-2 text-xs text-ink-3">
          <span>第 {episodeNo} 集成片：</span>
          <a className="inline-flex h-7 items-center rounded-md border border-line px-2 text-xs text-ink-2 hover:bg-surface-2"
             href={`/api/final/ep${String(episodeNo).padStart(2, '0')}_final.mp4`} target="_blank" rel="noreferrer">
            <Play className="mr-1 h-3 w-3" />在浏览器打开
          </a>
          <a className="inline-flex h-7 items-center rounded-md border border-line px-2 text-xs text-ink-2 hover:bg-surface-2"
             href={`/api/final/ep${String(episodeNo).padStart(2, '0')}_final.mp4?download=1`}>
            <Download className="mr-1 h-3 w-3" />下载
          </a>
        </div>
      </Section>

      {msg ? <div className="text-xs text-success">{msg}</div> : null}
      {err ? (
        <div className="flex items-start gap-1.5 rounded-md border border-danger/40 bg-danger/5 p-2 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /><span className="min-w-0 flex-1 break-words">{err}</span>
        </div>
      ) : null}
    </div>
  );
}
