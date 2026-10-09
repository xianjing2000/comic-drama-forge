// ============================================================================
// 资产与配音环境面板（AssetPanel）
// ----------------------------------------------------------------------------
// 补齐两处此前前端未接的后端能力（响应按实测对齐）：
//   GET /api/tts/env?project_name=      → { available, comfyui_online, default_params,
//        expected_models:[{name,desc,found,matched}], voices:[], dub_dir, out_dir }
//   GET /api/keyframes/list/<project>   → { success, project, dir, count, items:[] }
// ⚠️ /api/tts/env 的参数名是 project_name（不是 project）—— 实测确认。
// ============================================================================
import React, { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { Mic, ImageIcon, RefreshCw, CheckCircle2, AlertTriangle } from '@/components/ui/icons';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface TtsModel { name?: string; desc?: string; found?: boolean; matched?: string[] }
interface Speaker { speaker?: string; label?: string; gender?: string }
// ⚠️ 实测：list_voices() 返回的是**对象**不是数组 ——
//    { speakers:[{speaker,label,gender}], languages:[], model_choices:[], modes:[{key,label}] }
interface VoiceInfo { speakers?: Speaker[]; languages?: string[]; model_choices?: string[]; modes?: Array<{ key?: string; label?: string }> }
interface TtsEnv {
  available?: boolean; comfyui_online?: boolean;
  default_params?: Record<string, unknown>;
  expected_models?: TtsModel[]; voices?: VoiceInfo; dub_dir?: string; out_dir?: string;
}
interface KfItem { name?: string; filename?: string; path?: string; shot_id?: number | string; [k: string]: unknown }

const jget = async <T,>(u: string): Promise<T> => (await (await fetch(u)).json()) as T;

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

export default function AssetPanel({ project, className }: { project: string; className?: string }) {
  const [env, setEnv] = useState<TtsEnv | null>(null);
  const [kf, setKf] = useState<KfItem[]>([]);
  const [kfCount, setKfCount] = useState(0);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!project) return;
    setErr('');
    try {
      const q = encodeURIComponent(project);
      const [t, k] = await Promise.all([
        jget<TtsEnv>(`/api/tts/env?project_name=${q}`).catch(() => null),
        jget<{ items?: KfItem[]; count?: number }>(`/api/keyframes/list/${q}`).catch(() => null),
      ]);
      setEnv(t); setKf(k?.items || []); setKfCount(k?.count || 0);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [project]);

  useEffect(() => { setLoading(true); void load(); }, [load]);

  if (!project) return <EmptyState title="请先选择项目" />;
  if (loading) return <Skeleton className="h-36" />;

  const models = env?.expected_models || [];
  const found = models.filter((m) => m.found).length;
  const vinfo = env?.voices;
  const speakers = vinfo?.speakers || [];

  return (
    <div className={cx('flex flex-col gap-3', className)}>
      <Section icon={<Mic className="h-4 w-4" />} title="配音环境自检"
        right={<Button size="sm" variant="ghost" onClick={() => void load()}><RefreshCw className="h-3.5 w-3.5" /></Button>}>
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className={cx('flex items-center gap-1.5', env?.comfyui_online ? 'text-success' : 'text-warning')}>
            {env?.comfyui_online ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
            ComfyUI {env?.comfyui_online ? '在线' : '离线'}
          </span>
          <span className="text-ink-3">模型 {found}/{models.length} 就绪</span>
          {speakers.length > 0 ? <Badge variant="info">{speakers.length} 个音色</Badge> : null}
          {env?.available ? <Badge variant="success">可配音</Badge> : <Badge variant="danger">不可用</Badge>}
        </div>
        {models.length > 0 ? (
          <div className="flex flex-col gap-1">
            {models.map((m, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span className={cx('h-1.5 w-1.5 shrink-0 rounded-full', m.found ? 'bg-success' : 'bg-danger')} />
                <span className="w-56 shrink-0 truncate text-ink-2">{m.name}</span>
                <span className="min-w-0 flex-1 truncate text-ink-3">{m.desc}</span>
                {(m.matched || []).length > 0 ? <span className="shrink-0 text-ink-3">命中 {m.matched!.length}</span> : null}
              </div>
            ))}
          </div>
        ) : <div className="text-xs text-ink-3">未返回模型清单。</div>}
      </Section>

      {/* ⚠️ 2026-10-09 已删除「可用音色」区块 —— 与 AudioTab 重复：
       *  AudioTab 已完整实现音色库管理（ttsApi.voiceBank / Upload / Preview / Delete）
       *  且同样调用 ttsApi.env 做环境自检。同一件事不再两处渲染。
       *  本面板只保留它独有的：① 模型逐条就绪明细 ② 关键帧清单。 */}

      <Section icon={<ImageIcon className="h-4 w-4" />} title={`关键帧（${kfCount}）`}>
        {kf.length === 0 ? (
          <div className="text-xs text-ink-3">还没有关键帧产物。</div>
        ) : (
          <div className="flex flex-col gap-0.5">
            {kf.slice(0, 30).map((it, i) => {
              const fn = String(it.filename || it.name || it.path || '');
              return (
                <div key={i} className="flex items-center gap-2 rounded-md px-2 py-1 text-xs hover:bg-surface-2">
                  <span className="w-16 shrink-0 text-ink-3">{it.shot_id != null ? `镜 ${it.shot_id}` : ''}</span>
                  <span className="min-w-0 flex-1 truncate text-ink-2">{fn}</span>
                </div>
              );
            })}
          </div>
        )}
      </Section>

      {err ? <ErrorState description={err} onRetry={() => void load()} /> : null}
    </div>
  );
}
