// ============================================================================
// 自主生产面板（AutonomousPanel）
// ----------------------------------------------------------------------------
// 补齐 /api/autonomous/* 整套能力（此前前端一条都没接，后端已完整实现）：
//   GET  /api/autonomous/status?project_name=      运行状态
//   POST /api/autonomous/start                     一键启动（可带计划覆盖项）
//   POST /api/autonomous/stop   / resume           停止 / 恢复
//   GET  /api/autonomous/report/<project>?episode_no=   生产报告
//   GET  /api/autonomous/report/export/<project>?format=json|md  导出报告
//
// 与「托管（autopilot）」的区别：托管按计划逐集跑既定步骤；自主模式把决策权交给
// 总控 AI（function-calling），由它自己判断下一步做什么（见 agent_core.py）。
// ============================================================================
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, Skeleton, EmptyState, Input } from '@/components/ui';
import { Play, Pause, RefreshCw, FileText, Download, AlertTriangle } from '@/components/ui/icons';

const POLL_MS = 3000;
const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

// GET /api/autonomous/status 的真实结构（实测 2026-10-09）：
//   { success, autopilot: { running, paused, current, totals:{episodes_done,failed,retries} },
//     projects: [{ name, enabled, episodes_done, episodes_failed, episodes_pending }] }
interface AutoTotals { episodes_done?: number; episodes_failed?: number; retries?: number }
interface AutoAutopilot { running?: boolean; paused?: boolean; current?: unknown; totals?: AutoTotals }
interface AutoProject { name?: string; enabled?: boolean; episodes_done?: number; episodes_failed?: number; episodes_pending?: number }
interface AutoStatus {
  success?: boolean;
  autopilot?: AutoAutopilot;
  projects?: AutoProject[];
  error?: string;
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  const j = await r.json();
  if (!r.ok || j?.success === false) throw new Error(j?.error || j?.message || `HTTP ${r.status}`);
  return j as T;
}

export default function AutonomousPanel({
  project,
  novelId = '',
  className,
}: {
  project: string;
  novelId?: string;
  className?: string;
}) {
  const [st, setSt] = useState<AutoStatus | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);
  const [openCfg, setOpenCfg] = useState(false);
  const [report, setReport] = useState<string>('');
  const timer = useRef<number | null>(null);

  // 启动参数（对应后端 plan_overrides 白名单，字段名与后端逐字一致）
  const [style, setStyle] = useState('');
  const [targetShots, setTargetShots] = useState('');
  const [flags, setFlags] = useState({
    enable_assets: true, enable_storyboard: true, enable_video: true,
    enable_final: true, enable_tts: true, enable_tts_pre: true,
    // ⭐ 2026-10-10：集间流水线开关（用户可选）。True = 本集烧 GPU 时后台并行
    //    预热下一集剧本（纯 LLM、零 GPU、不依赖本集产物）。默认开启。
    prewarm_next_script: true,
    // ⭐ 资产提示词预热：本批资产生成期间后台把**全部**资产的增强提示词先算好
    //    （只填 prompt_enhance 缓存，纯 LLM 零 GPU）。默认开启。
    prewarm_asset_prompt: true,
  });

  const load = useCallback(async () => {
    if (!project) return;
    try {
      const j = await req<{ success: boolean } & AutoStatus>(
        `/api/autonomous/status?project_name=${encodeURIComponent(project)}`);
      setSt(j);
      setErr('');
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [project]);

  useEffect(() => {
    setLoading(true);
    void load();
    timer.current = window.setInterval(() => { void load(); }, POLL_MS);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [load]);

  const act = async (name: 'start' | 'stop' | 'resume') => {
    setBusy(name);
    setErr('');
    try {
      const body: Record<string, unknown> = { project_name: project };
      if (name === 'start') {
        if (novelId) body.novel_id = novelId;
        if (style.trim()) body.style = style.trim();
        const n = parseInt(targetShots, 10);
        if (Number.isFinite(n) && n > 0) body.target_shots = n;
        Object.assign(body, flags);
      }
      await req(`/api/autonomous/${name}`, { method: 'POST', body: JSON.stringify(body) });
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  const loadReport = async () => {
    setBusy('report');
    try {
      const j = await req<Record<string, unknown>>(
        `/api/autonomous/report/${encodeURIComponent(project)}`);
      setReport(JSON.stringify(j, null, 2));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally { setBusy(''); }
  };

  if (!project) return <EmptyState title="请先选择项目" />;
  if (loading && !st) return <Skeleton className="h-24" />;

  const ap = st?.autopilot || {};
  const proj = (st?.projects || []).find((p) => p.name === project) || {};
  const running = Boolean(ap.running);
  const paused = Boolean(ap.paused);
  const totals = ap.totals || {};
  const done = proj.episodes_done ?? totals.episodes_done ?? 0;
  const failed = proj.episodes_failed ?? totals.episodes_failed ?? 0;
  const pending = proj.episodes_pending ?? 0;

  return (
    <div className={cx('flex flex-col gap-3', className)}>
      {/* ── 状态 + 控制 ───────────────────────────────────────────── */}
      <div className={cx('rounded-lg border p-3', running ? 'border-brand/30 bg-surface' : 'border-line bg-surface-2')}>
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className={cx('h-2 w-2 shrink-0 rounded-full', running ? 'animate-pulse bg-brand' : 'bg-ink-3')} />
            <span className="truncate text-sm font-medium text-ink-1">
              {running ? (paused ? '自主生产（已暂停）' : '自主生产中') : '自主生产未启动'}
            </span>
            <Badge variant="success">完成 {done}</Badge>
            {failed > 0 ? <Badge variant="danger">失败 {failed}</Badge> : null}
            {pending > 0 ? <Badge variant="info">待产 {pending}</Badge> : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {running ? (
              <>
                <Button size="sm" variant="secondary" loading={busy === 'resume'} onClick={() => void act('resume')}>
                  <RefreshCw className="mr-1.5 h-3.5 w-3.5" />恢复
                </Button>
                <Button size="sm" variant="danger" loading={busy === 'stop'} onClick={() => void act('stop')}>
                  <Pause className="mr-1.5 h-3.5 w-3.5" />停止
                </Button>
              </>
            ) : (
              <Button size="sm" variant="brand" loading={busy === 'start'} onClick={() => void act('start')}>
                <Play className="mr-1.5 h-3.5 w-3.5" />一键启动
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => setOpenCfg((v) => !v)}>
              {openCfg ? '收起参数' : '启动参数'}
            </Button>
          </div>
        </div>
              </div>

      {/* ── 启动参数（默认收起，点开才显示）──────────────────────── */}
      {openCfg ? (
        <div className="flex flex-col gap-3 rounded-lg border border-line bg-surface-2 p-3">
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-3">画风（留空＝用项目已保存风格）</span>
              <Input value={style} onChange={setStyle} placeholder="如：超现实悬疑惊悚" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-3">目标镜头数（留空＝按剧本）</span>
              <Input value={targetShots} onChange={(v) => setTargetShots(v.replace(/\D/g, ''))} placeholder="如：24" />
            </label>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {([
              ['enable_assets', '资产图'], ['enable_storyboard', '分镜图'],
              ['enable_video', '视频'], ['enable_tts_pre', '参考音色'],
              ['enable_tts', '配音'], ['enable_final', '成片'],
              // ⭐ 集间流水线：本集烧 GPU 时后台预热下一集剧本（见后端 prewarm_next_script）
              ['prewarm_next_script', '预热下集剧本'],
              ['prewarm_asset_prompt', '预热资产提示词'],
            ] as const).map(([k, label]) => (
              <label key={k} className="flex cursor-pointer items-center gap-1.5 text-xs text-ink-2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand"
                  checked={flags[k]}
                  onChange={(e) => setFlags((f) => ({ ...f, [k]: e.target.checked }))}
                />
                {label}
              </label>
            ))}
          </div>
        </div>
      ) : null}

      {/* ── 生产报告 ─────────────────────────────────────────────── */}
      <div className="rounded-lg border border-line bg-surface">
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-medium text-ink-1">
            <FileText className="h-4 w-4 text-ink-3" />生产报告
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" loading={busy === 'report'} onClick={() => void loadReport()}>查看</Button>
            <a
              className="inline-flex h-8 items-center rounded-md border border-line px-2.5 text-xs text-ink-2 hover:bg-surface-2"
              href={`/api/autonomous/report/export/${encodeURIComponent(project)}?format=json`}
              target="_blank" rel="noreferrer"
            >
              <Download className="mr-1 h-3.5 w-3.5" />导出
            </a>
          </div>
        </div>
        {report ? (
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words p-3 text-xs leading-relaxed text-ink-2">{report}</pre>
        ) : (
          <div className="px-3 py-6 text-center text-xs text-ink-3">点「查看」加载本项目的生产报告</div>
        )}
      </div>

      {err ? (
        <div className="flex items-start gap-1.5 rounded-md border border-danger/40 bg-danger/5 p-2 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 flex-1 break-words">{err}</span>
        </div>
      ) : null}
    </div>
  );
}
