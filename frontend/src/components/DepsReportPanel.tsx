// ============================================================================
// 依赖就绪报告（DepsReportPanel）
// ----------------------------------------------------------------------------
// 对接既有端点 GET /api/deps/check（routes/system.py:28 + deps_check.py 21KB）。
//
// ⚠️ 该端点**早已实现**却从未被前端引用（属审计中 116 条「后端有能力、前端无入口」），
//    而用户需要的正是它：**按当前生产工作流**逐个比对 ComfyUI 的模型与自定义节点，
//    列出「匹配上了什么 / 缺什么」—— 而不是笼统地罗列 ComfyUI 里有什么。
//
// 实测响应形状：
//   { comfyui:{online,online_node_count,url,source}, docs,
//     models:{dir,dir_exists,ready,required_names[],missing_required[],items:[{name,found,path}]},
//     plugins:{ready,needed:[{plugin,node_type,repo,status,optional,legacy,note}],
//              missing_required[],missing_optional[]},
//     per_workflow_nodes:[{file,key,types[],error}],
//     summary:{all_ok,models_ok,plugins_ok,blockers[],workflows:[{file,key,found,types[]}]} }
// ============================================================================
import React, { useCallback, useEffect, useState } from 'react';
import { t } from '@/i18n';
import { sanitizeErrorMessage } from '@/utils/errorText';
import { Badge, Button, Skeleton, ErrorState } from '@/components/ui';
import { CheckCircle2, AlertTriangle, X, RefreshCw, ClipboardList } from '@/components/ui/icons';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface ModelItem { name?: string; found?: boolean; path?: string }
interface NeededNode { plugin?: string; node_type?: string; repo?: string; status?: string; optional?: boolean; legacy?: boolean; note?: string }
interface WfRow { file?: string; key?: string; found?: boolean; types?: string[]; error?: string }
interface DepsReport {
  success?: boolean; docs?: string; error?: string;
  comfyui?: { online?: boolean; online_node_count?: number; url?: string; source?: string };
  models?: { dir?: string; dir_exists?: boolean; ready?: boolean; required_names?: string[]; missing_required?: string[]; items?: ModelItem[] };
  plugins?: { ready?: boolean; needed?: NeededNode[]; missing_required?: string[]; missing_optional?: string[] };
  per_workflow_nodes?: WfRow[];
  summary?: { all_ok?: boolean; models_ok?: boolean; plugins_ok?: boolean; blockers?: string[]; workflows?: WfRow[] };
}

export default function DepsReportPanel({ className }: { className?: string }) {
  const [rep, setRep] = useState<DepsReport | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  const [openWf, setOpenWf] = useState(false);
  const [openPlugins, setOpenPlugins] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const r = await fetch('/api/deps/check');
      const j = (await r.json()) as DepsReport;
      if (!r.ok && j?.success === false) setErr(j.error || `HTTP ${r.status}`);
      setRep(j);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (loading) return <Skeleton className="h-32" />;
  if (err && !rep) return <ErrorState description={err} onRetry={() => void load()} />;

  const s = rep?.summary || {};
  const m = rep?.models || {};
  const pl = rep?.plugins || {};
  const cf = rep?.comfyui || {};
  // ⚠️ 空数组是 truthy —— 必须按 length 判空再回退，否则 summary.workflows 为空时
  //    整段工作流列表会消失（实测 summary.workflows 可能为空数组）。
  const wfs = (s.workflows && s.workflows.length) ? s.workflows : (rep?.per_workflow_nodes || []);
  const needed = pl.needed || [];
  const missingModels = m.missing_required || [];
  const badPlugins = needed.filter((n) => n.status && n.status !== 'ok');

  const Dot = ({ ok }: { ok?: boolean }) => (
    <span className={cx('mt-[5px] h-2 w-2 shrink-0 rounded-full', ok ? 'bg-success' : 'bg-danger')} />
  );

  return (
    <div className={cx('rounded-lg border border-line bg-surface', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="flex items-center gap-2 text-sm font-medium text-ink-1">
          <ClipboardList className="h-4 w-4 text-ink-3" />{t('deps.title')}
        </span>
        <div className="flex items-center gap-2">
          <Badge variant={cf.online ? 'success' : 'danger'}>ComfyUI {cf.online ? t('deps.comfyOnline') : t('deps.comfyOffline')}</Badge>
          <Badge variant={s.all_ok ? 'success' : 'warning'}>{s.all_ok ? t('deps.allReady') : t('deps.hasMissing')}</Badge>
          <Button size="sm" variant="ghost" onClick={() => void load()}><RefreshCw className="h-3.5 w-3.5" /></Button>
        </div>
      </div>

      <div className="flex flex-col gap-3 p-3">
        {/* 阻塞项 */}
        {(s.blockers || []).length > 0 ? (
          <div className="rounded-md border border-danger/40 bg-danger/5 p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-danger">
              <X className="h-3.5 w-3.5" />{t('deps.blockers')}
            </div>
            <ul className="ml-4 list-disc space-y-0.5 text-xs text-danger/90">
              {(s.blockers || []).map((b, i) => <li key={i} className="break-words">{b}</li>)}
            </ul>
          </div>
        ) : (
          <div className="flex items-center gap-1.5 text-xs text-success">
            <CheckCircle2 className="h-3.5 w-3.5" />{t('deps.noBlockers')}
          </div>
        )}

        {/* 模型 */}
        <div>
          <div className="mb-1.5 flex items-center gap-2 text-xs font-medium text-ink-1">
            <Dot ok={m.ready} />
            {t('deps.modelsLabel')} {m.ready ? t('deps.modelsReady') : t('deps.modelsMissing', { n: missingModels.length })}
            <span className="font-normal text-ink-3">
              {t('deps.requiredN', { n: (m.required_names || []).length })} · {t('deps.dirLabel')} {m.dir_exists ? t('deps.dirExists') : t('deps.dirMissing')}
            </span>
          </div>
          {missingModels.length > 0 ? (
            <ul className="ml-4 list-disc space-y-0.5 text-xs text-danger">
              {missingModels.map((n, i) => <li key={i} className="break-all font-mono">{n}</li>)}
            </ul>
          ) : null}
          <div className="mt-1 break-all text-xs text-ink-3">{t('deps.dirLine', { dir: m.dir ?? '' })}</div>
        </div>

        {/* 插件 / 自定义节点 */}
        <div>
          <button type="button" onClick={() => setOpenPlugins((v) => !v)}
            className="mb-1.5 flex w-full items-center gap-2 text-left text-xs font-medium text-ink-1">
            <Dot ok={pl.ready} />
            {t('deps.pluginsLabel')} {pl.ready ? t('deps.modelsReady') : t('deps.pluginsBad', { n: badPlugins.length })}
            <span className="font-normal text-ink-3">{t('deps.nodesRequired', { n: needed.length })}</span>
            <span className="text-ink-3">{openPlugins ? t('common.collapse') : t('deps.expand')}</span>
          </button>
          {openPlugins ? (
            <div className="flex flex-col gap-1 rounded-md border border-line bg-surface-2 p-2">
              {needed.map((n, i) => (
                <div key={i} className="flex items-start gap-2 text-xs">
                  <span className={cx('mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full',
                    n.status === 'ok' ? 'bg-success' : 'bg-danger')} />
                  <span className="w-52 shrink-0 truncate font-mono text-ink-2">{n.node_type}</span>
                  <span className="w-56 shrink-0 truncate text-ink-3">{n.plugin}</span>
                  <span className="min-w-0 flex-1 truncate text-ink-3">{n.note || n.repo || ''}</span>
                  {n.optional ? <Badge variant="default">{t('deps.optional')}</Badge> : null}
                </div>
              ))}
            </div>
          ) : null}
        </div>

        {/* 逐工作流 */}
        <div>
          <button type="button" onClick={() => setOpenWf((v) => !v)}
            className="flex w-full items-center gap-2 text-left text-xs font-medium text-ink-1">
            <span className="mt-[5px] h-2 w-2 shrink-0 rounded-full bg-info" />
            {t('deps.workflowsN', { n: wfs.length })}
            <span className="text-ink-3">{openWf ? t('common.collapse') : t('deps.expand')}</span>
          </button>
          {openWf ? (
            <div className="mt-1.5 flex flex-col gap-1 rounded-md border border-line bg-surface-2 p-2">
              {wfs.map((w, i) => (
                <div key={i} className="flex items-start gap-2 text-xs">
                  <span className={cx('mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full',
                    w.found === false || w.error ? 'bg-danger' : 'bg-success')} />
                  <span className="w-64 shrink-0 truncate font-mono text-ink-2">{w.file}</span>
                  <span className="min-w-0 flex-1 truncate text-ink-3">
                    {w.error ? sanitizeErrorMessage(w.error) : `${t('deps.nodesN', { n: (w.types || []).length })}${(w.types || []).length ? '：' + (w.types || []).slice(0, 4).join(', ') : ''}`}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        {rep?.docs ? <div className="text-xs text-ink-3">{t('deps.docsLine')} {rep.docs}</div> : null}
      </div>
    </div>
  );
}
