// ============================================================================
// 诊断与补救建议（TriagePanel）
// ----------------------------------------------------------------------------
// 对接 POST /api/qc/triage。
//
// 决策逻辑（用户 2026-10-09 拍板）：先看画面（分镜图+视频，两类都算）→ 再看配音/音轨
//   → 最后看字幕校对，然后判定补救手段：
//     只有配音问题 → TTS 重合成（便宜）
//     只有画面问题 → 重出该镜
//     音画都有问题 → ⚠️ 改提示词重出（TTS 救不了画面）
//
// ⚠️ 严格「建议优先」：本组件**不自动执行任何补救**。
//    所有动作都是按钮，点了才发请求 —— 因为「音画都坏」时重配一遍音是纯浪费。
//
// ⭐ 2026-10-12（t28）：接上 t19 的音频体检契约
//   · audio.scanned 五类分类计数（line / voice_bank / merged / mix / final）：
//     旧实现只扫 <dub>/<项目>/*_lines/，而现行流水线的 tts_pre 只产 voice_bank →
//     现役链路的扫描数恒为 0（面板永远显示「无问题」）。这里把分类计数显式展示出来，
//     用户一眼能看出「到底扫到了哪些产物」。
//   · audio.failed[] 现带 kind / whole_track / metrics → 按类目分组，并给出平均电平/
//     有声占比，便于区分「整段无声」与「音量偏小」。
//   · 每项可点开「频谱/波形」弹窗：POST /api/qc/audio（with_ai=true）返回 visuals URL
//     数组（第 1 张频谱、第 2 张波形），直接 <img> 展示。
//   · 本组件原先硬编码的中文文案全部迁到 locales（audio.triage*），zh-CN / en-US 双语齐备。
//   · 错误展示统一走 describeError / sanitizeErrorMessage：不向用户暴露绝对路径。
// ============================================================================
import React, { useCallback, useMemo, useState } from 'react';
import { Badge, Button, Skeleton, ErrorState, Modal } from '@/components/ui';
import { AlertTriangle, CheckCircle2, RefreshCw, Lightbulb, Play } from '@/components/ui/icons';
import { useApp } from '@/context/AppContext';
import { qcApi, type QCTriageResult, type QCTriageAudioFailed } from '@/api/client';
import { describeError, sanitizeErrorMessage } from '@/utils/errorText';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface TriageAction { label?: string; method?: string; endpoint?: string; payload?: Record<string, unknown>; cost?: string }

//: 音轨产物类目（与后端 _audio_qc_candidates 的 kind 逐字一致，顺序即展示顺序）。
const AUDIO_KINDS = ['line', 'voice_bank', 'merged', 'mix', 'final'] as const;
//: kind → locales 键后缀（audio.triageKindLine / triageKindVoiceBank / ...）。
const KIND_KEY: Record<string, string> = {
  line: 'Line', voice_bank: 'VoiceBank', merged: 'Merged', mix: 'Mix', final: 'Final',
};
const COST_KEYS = ['none', 'low', 'mid', 'high'];
const VERDICT_KEYS = ['ok', 'audio_only', 'visual_only', 'both'];

interface VisualsState {
  open: boolean; title: string; kind: string; label: string;
  loading: boolean; error: string; images: string[];
}

export default function TriagePanel({ project, episodeNo = 1, className }: {
  project: string; episodeNo?: number; className?: string;
}) {
  const { t } = useApp();
  const [rep, setRep] = useState<QCTriageResult | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(false);
  const [deep, setDeep] = useState(false);
  const [runMsg, setRunMsg] = useState('');
  const [vis, setVis] = useState<VisualsState>({
    open: false, title: '', kind: 'final', label: '', loading: false, error: '', images: [],
  });

  const verdictLabel = useCallback((v?: string) => {
    const key = VERDICT_KEYS.includes(String(v)) ? String(v) : 'unknown';
    return t('audio.triageVerdict.' + key);
  }, [t]);

  const costLabel = useCallback((c?: string) => {
    const key = COST_KEYS.includes(String(c)) ? String(c) : 'none';
    return t('audio.triageCost.' + key);
  }, [t]);

  const kindLabel = useCallback((k?: string) => {
    const suffix = KIND_KEY[String(k)];
    return suffix ? t('audio.triageKind' + suffix) : t('audio.triageKindOther');
  }, [t]);

  const run = useCallback(async (deepMode: boolean) => {
    if (!project) return;
    setLoading(true); setErr(''); setRunMsg('');
    try {
      const j = await qcApi.triage({ project, episode_no: episodeNo, deep: deepMode });
      if (j?.success === false) setErr(String(j?.error || ''));
      setRep(j);
    } catch (e) {
      const { message } = describeError(e, t('common.loadFailed'));
      setErr(message);
    } finally { setLoading(false); }
  }, [project, episodeNo, t]);

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
      if (r.ok && !j?.error) {
        setRunMsg(String(j?.message || '') || t('audio.triageSubmitted'));
      } else {
        setRunMsg(t('audio.triageExecFailed', {
          msg: sanitizeErrorMessage(String(j?.error || ('HTTP ' + r.status))),
        }));
      }
    } catch (e) {
      const { message } = describeError(e, t('audio.triageExecFailed', { msg: '' }));
      setRunMsg(message);
    }
  }, [t]);

  // 频谱/波形弹窗：直接消费既有接口（POST /api/qc/audio，with_ai=true → visuals 数组）
  const openVisuals = useCallback(async (kind: string, label: string) => {
    if (!project) return;
    const src = (AUDIO_KINDS as readonly string[]).includes(kind)
      ? (kind as 'line' | 'voice_bank' | 'merged' | 'mix' | 'final')
      : 'final';
    setVis({
      open: true, kind, label,
      title: t('audio.triageVisualsTitle', { name: label }),
      loading: true, error: '', images: [],
    });
    try {
      const j = await qcApi.checkAudio({ project_name: project, source: src, with_ai: true });
      const imgs = (j?.visuals || []).filter((x): x is string => typeof x === 'string' && !!x);
      setVis((p) => ({
        ...p, loading: false, images: imgs,
        error: imgs.length ? '' : t('audio.triageVisualsEmpty'),
      }));
    } catch (e) {
      const { message } = describeError(e, t('audio.triageVisualsFailed'));
      setVis((p) => ({ ...p, loading: false, error: message }));
    }
  }, [project, t]);

  const s = rep || {};
  const v = s.verdict || '';
  const okAll = v === 'ok';
  const actions: TriageAction[] = s.actions || [];
  const audio = s.audio || {};
  const scanned = audio.scanned || {};
  const failedByKind = useMemo(() => {
    const m: Record<string, QCTriageAudioFailed[]> = {};
    for (const f of (audio.failed || [])) {
      const k = String(f.kind || 'other');
      m[k] = m[k] || [];
      m[k].push(f);
    }
    return m;
  }, [audio.failed]);

  const metricsText = (f: QCTriageAudioFailed): string => {
    const m = f.metrics || {};
    const mean = typeof m.mean_db === 'number' ? String(m.mean_db) : '-';
    const ratio = typeof m.speech_ratio === 'number' ? (m.speech_ratio * 100).toFixed(0) + '%' : '-';
    return t('audio.triageMetrics', { mean, ratio });
  };

  return (
    <div className={cx('rounded-lg border border-line bg-surface', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="flex items-center gap-2 text-sm font-medium text-ink-1">
          <Lightbulb className="h-4 w-4 text-ink-3" />{t('audio.triageTitle')}
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-ink-2">
            <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />
            {t('audio.triageDeep')}
          </label>
          <Button size="sm" variant="secondary" disabled={loading || !project} onClick={() => void run(deep)}>
            <RefreshCw className={cx('mr-1 h-3.5 w-3.5', loading && 'animate-spin')} />
            {loading ? t('audio.triageRunning') : t('audio.triageRun')}
          </Button>
        </div>
      </div>

      <div className="flex flex-col gap-3 p-3">
        {!project ? <div className="text-xs text-ink-3">{t('audio.triageNeedProject')}</div> : null}
        {loading && !rep ? <Skeleton className="h-24" /> : null}
        {err && !rep ? <ErrorState description={err} onRetry={() => void run(deep)} /> : null}

        {rep && !loading ? (
          <>
            {/* 判定结论 */}
            <div className={cx('rounded-md border p-2.5', okAll ? 'border-success/40 bg-success/5' : 'border-warning/40 bg-warning/5')}>
              <div className="flex flex-wrap items-center gap-2">
                {okAll ? <CheckCircle2 className="h-4 w-4 text-success" /> : <AlertTriangle className="h-4 w-4 text-warning" />}
                <span className="text-sm font-medium text-ink-1">{verdictLabel(v)}</span>
                <Badge variant={okAll ? 'success' : 'warning'}>
                  {t('audio.triageRemedy', { name: s.remedy || '-' })}
                </Badge>
              </div>
              {s.reason ? <p className="mt-1 text-xs text-ink-2">{s.reason}</p> : null}
            </div>

            {/* 音轨体检（t28）：分类计数 + 分组失败 + 频谱/波形入口 */}
            <div className="rounded-md border border-line bg-surface-2 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-ink-1">{t('audio.triageAudioSection')}</span>
                <Badge variant={(audio.failed_count || 0) > 0 ? 'danger' : 'default'}>
                  {t('audio.triageScanned', { n: audio.checked || 0 })}
                </Badge>
                <span className="text-xs text-ink-3">
                  {audio.mode === 'live' ? t('audio.triageModeLive') : t('audio.triageModeHistory')}
                </span>
                <span className="ml-auto text-xs text-ink-3">{t('audio.triageScannedByKind')}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-1">
                {AUDIO_KINDS.map((k) => {
                  const n = Number(scanned[k] || 0);
                  return (
                    <span
                      key={k}
                      className={cx('rounded-full border px-2 py-0.5 text-xs',
                        n > 0 ? 'border-line-strong text-ink-1' : 'border-line text-ink-3')}
                    >
                      {(audio.legend && audio.legend[k]) || kindLabel(k)} {n}
                    </span>
                  );
                })}
              </div>
              <p className="mt-1 text-xs text-ink-3">{t('audio.triageKindsHint')}</p>

              {Object.keys(failedByKind).length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  <div className="text-xs font-medium text-ink-2">{t('audio.triageFailedByKind')}</div>
                  {Object.entries(failedByKind).map(([k, items]) => (
                    <div key={k} className="rounded border border-line bg-surface px-2 py-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="danger">{kindLabel(k)}</Badge>
                        <span className="text-xs text-ink-3">{items.length}</span>
                      </div>
                      {items.slice(0, 4).map((f, i) => (
                        <div key={i} className="mt-1 flex flex-wrap items-center gap-2">
                          <span className="min-w-0 flex-1 truncate text-xs text-danger-strong">{f.line || '-'}</span>
                          {f.whole_track ? <span className="text-xs text-ink-3">{t('audio.triageWholeTrack')}</span> : null}
                          <span className="text-xs text-ink-3">{metricsText(f)}</span>
                          <Button size="sm" variant="secondary" onClick={() => void openVisuals(k, kindLabel(k))}>
                            {t('audio.triageViewVisuals')}
                          </Button>
                        </div>
                      ))}
                      {items.length > 4 ? (
                        <div className="mt-1 text-xs text-ink-3">
                          {t('audio.dubSilentMore', { n: items.length - 4 })}
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mt-1.5 text-xs text-ink-3">{t('audio.triageNoAudioFail')}</p>
              )}
            </div>

            {/* 画面 + 字幕 */}
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              <div className="rounded-md border border-line bg-surface-2 p-2">
                <div className="text-xs font-medium text-ink-1">{t('audio.triageVisual')}</div>
                <div className="mt-0.5 text-xs text-ink-3">
                  {(s.visual?.failed_count || 0) > 0
                    ? t('audio.triageVisualFailed', { n: s.visual?.failed_count || 0 })
                    : t('audio.triageNoVisualFail')}
                </div>
                {(s.visual?.failed || []).slice(0, 3).map((f, i) => (
                  <div key={i} className="mt-1 text-xs text-danger">
                    {f.kind}/{f.key}（{f.attempts}）
                    <div className="text-ink-3">{String((f.issues || [])[0] || '').slice(0, 60)}</div>
                  </div>
                ))}
              </div>
              <div className="rounded-md border border-line bg-surface-2 p-2">
                <div className="text-xs font-medium text-ink-1">{t('audio.triageCaption')}</div>
                <div className="mt-0.5 text-xs text-ink-3">
                  {!s.caption?.available ? t('audio.triageCaptionEnv')
                    : s.caption?.ok === null ? (s.caption?.note || t('audio.triageCaptionPending'))
                    : s.caption?.ok ? t('audio.triageCaptionPass') : t('audio.triageCaptionFail')}
                </div>
                {(s.caption?.missing || []).slice(0, 3).map((m, i) => (
                  <div key={i} className="mt-1 text-xs text-danger">{t('audio.triageCaptionMissing', { text: String(m).slice(0, 50) })}</div>
                ))}
                {(s.caption?.extra || []).slice(0, 3).map((m, i) => (
                  <div key={i} className="mt-1 text-xs text-warning-strong">{t('audio.triageCaptionExtra', { text: String(m).slice(0, 50) })}</div>
                ))}
              </div>
            </div>

            {/* 建议动作（点了才执行） */}
            {actions.length > 0 ? (
              <div>
                <div className="mb-1.5 text-xs font-medium text-ink-1">{t('audio.triageActions', { n: actions.length })}</div>
                <div className="flex flex-col gap-1">
                  {actions.map((a, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface-2 px-2 py-1.5">
                      <Badge variant={a.cost === 'high' ? 'danger' : a.cost === 'none' ? 'default' : 'info'}>
                        {costLabel(a.cost)}
                      </Badge>
                      <span className="min-w-0 flex-1 text-xs text-ink-2">{a.label}</span>
                      <span className="font-mono text-xs text-ink-3">{a.endpoint}</span>
                      <Button size="sm" variant="secondary" onClick={() => void exec(a)}>
                        <Play className="mr-1 h-3 w-3" />{t('audio.triageExec')}
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
          <div className="text-xs text-ink-3">{t('audio.triageIntro')}</div>
        ) : null}
      </div>

      {/* 频谱 / 波形弹窗（既有静态图入口 /api/qc/frames/<rel> 由后端 visuals 数组给出） */}
      <Modal
        isOpen={vis.open}
        onClose={() => setVis((p) => ({ ...p, open: false }))}
        title={vis.title}
        size="lg"
      >
        <div className="space-y-3">
          {vis.loading ? <Skeleton className="h-48" /> : null}
          {!vis.loading && vis.error ? (
            <ErrorState description={vis.error} onRetry={() => void openVisuals(vis.kind, vis.label)} />
          ) : null}
          {!vis.loading && !vis.error ? (
            <>
              <p className="text-xs text-ink-3">{t('audio.triageVisualsHint')}</p>
              <div className="grid grid-cols-1 gap-3">
                {vis.images.map((img, i) => (
                  <figure key={img + i} className="rounded border border-line bg-surface-2 p-2">
                    <img
                      src={img}
                      alt={i === 0 ? t('audio.spectrogram') : t('audio.waveform')}
                      className="w-full rounded"
                    />
                    <figcaption className="mt-1 text-xs text-ink-3">
                      {i === 0 ? t('audio.spectrogram') : t('audio.waveform')}
                    </figcaption>
                  </figure>
                ))}
              </div>
            </>
          ) : null}
        </div>
      </Modal>
    </div>
  );
}
