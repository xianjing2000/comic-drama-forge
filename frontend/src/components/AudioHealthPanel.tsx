// ============================================================================
// 音频体检（AudioHealthPanel，t9）
// ----------------------------------------------------------------------------
// 背景：t19 给成片挂了「整轨口径」的音频门禁（静音会被判 blocked、无声镜号可定位），
//   但这些结论此前只散落在交付页与诊断面板 —— 用户看不到「扫了多少音频、哪几镜没
//   声音、这条不合格到底会不会拦住交付」。
//
// 本面板一次性回答四个问题（全部只读，不改任何后端状态）：
//   ① 扫到了多少音频、按五类产物怎么分布：POST /api/qc/triage → audio.checked /
//      audio.scanned。**数字原样来自后端**（本组件只展示；派生分类见 utils/audioHealth.ts），
//      不重算总数、不四舍五入。
//   ② 哪几镜整段无声：交付清单 meta.audio_qc_silent_shots（t19 落盘 → t10 挂 meta），
//      点「定位分镜」跳到分镜页对应镜头（滚动 + 高亮）。
//   ③ 频谱/波形弹窗：POST /api/qc/audio（with_ai=true）→ 后端渲染的 PNG，直接 <img>。
//   ④ 这条不合格是「已阻断」还是「仅记录」。
//
// ⚠️ 判据（静音口径 / 门禁处置 / 类目状态）全部抽到 utils/audioHealth.ts —— 那是与
//    后端契约逐字对齐的纯逻辑，必须能被单独验证，不留在 UI 里。
//
// 三态齐全：首次加载 Skeleton / 主数据失败 ErrorState+重试 / 无音频可体检 EmptyState；
// 交付清单与质量状态是「增补」数据，取不到只降级对应区块（不把整页打成硬错误）。
// 文案全部走 locales（audio.health*，zh-CN + en-US）。
// ============================================================================
import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, EmptyState, ErrorState, Modal, Skeleton } from '@/components/ui';
import { AlertTriangle, CheckCircle2, Film, Music, RefreshCw, Volume2 } from '@/components/ui/icons';
import { autopilotApi, episodesApi, qcApi, qualityApi, type QCTriageResult, type QualityEpisodeRow } from '@/api/client';
import type { Deliverable } from '@/types';
import { describeError, sanitizeErrorMessage } from '@/utils/errorText';
import {
  AUDIO_KINDS, KIND_KEY, SILENT_MEAN_DB, SILENT_RATIO,
  isSilentFail, kindStateOf, pickDeliverable, resolveGate, summarizeAudioHealth,
  type GateState, type KindState,
} from '@/utils/audioHealth';

const cx = (...p: Array<string | false | null | undefined>): string => p.filter(Boolean).join(' ');

interface VisualsState {
  open: boolean; title: string; source: string; label: string;
  loading: boolean; error: string; images: string[];
}

export default function AudioHealthPanel({ project, novelId, onLocateShot, className }: {
  /** 项目名（= 工作台 projectKey） */
  project: string;
  /** 小说 id：用于拉剧集列表（决定体检的集号） */
  novelId?: string;
  /** 点「定位分镜」的回调：由工作台切到分镜页并滚动/高亮该镜 */
  onLocateShot?: (shotId: string | number, episodeNo?: number) => void;
  className?: string;
}) {
  const { t } = useApp();

  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [errCode, setErrCode] = useState('');
  const [triage, setTriage] = useState<QCTriageResult | null>(null);
  const [deliverable, setDeliverable] = useState<Deliverable | null>(null);
  const [delivErr, setDelivErr] = useState('');
  const [qualityRow, setQualityRow] = useState<QualityEpisodeRow | null>(null);
  const [statusErr, setStatusErr] = useState('');
  const [episodes, setEpisodes] = useState<Array<Record<string, unknown>>>([]);
  const [episodeNo, setEpisodeNo] = useState<number | null>(null);
  const [deep, setDeep] = useState(false);
  const [running, setRunning] = useState(false);
  const [vis, setVis] = useState<VisualsState>({
    open: false, title: '', source: 'final', label: '', loading: false, error: '', images: [],
  });

  // ---- 剧集列表：体检结论按集分流（交付清单与质量状态都是集级） ----
  useEffect(() => {
    if (!novelId) { setEpisodes([]); setEpisodeNo((p) => p ?? 1); return; }
    let alive = true;
    episodesApi.list(novelId)
      .then((d) => {
        if (!alive) return;
        const list = (d?.episodes || []) as Array<Record<string, unknown>>;
        setEpisodes(list);
        setEpisodeNo((prev) => (prev ?? (Number(list[0]?.episode_no) || 1)));
      })
      .catch(() => { if (alive) { setEpisodes([]); setEpisodeNo((p) => p ?? 1); } });
    return () => { alive = false; };
  }, [novelId]);

  const load = useCallback(async (ep: number, deepMode: boolean) => {
    if (!project) return;
    setRunning(true); setErr(''); setErrCode('');
    // 主数据（扫描计数）必须成功；交付清单与质量状态是「增补」，取不到只降级对应区块
    const [tri, del, qua] = await Promise.allSettled([
      qcApi.triage({ project, episode_no: ep, deep: deepMode }),
      autopilotApi.deliverables(project),
      qualityApi.episodes(project),
    ]);
    if (tri.status === 'fulfilled') {
      setTriage(tri.value);
    } else {
      const { message, code } = describeError(tri.reason, t('audio.healthLoadFailed'));
      setErr(message); setErrCode(code);
    }
    if (del.status === 'fulfilled') {
      setDeliverable(pickDeliverable(del.value?.items || [], ep));
      setDelivErr('');
    } else {
      setDeliverable(null);
      setDelivErr(describeError(del.reason, t('common.loadFailed')).message);
    }
    if (qua.status === 'fulfilled') {
      const row = (qua.value?.episodes || []).find((e) => Number(e.episode_no) === Number(ep)) || null;
      setQualityRow(row);
      setStatusErr(row ? '' : t('audio.healthStatusNoRow', { n: ep }));
    } else {
      setQualityRow(null);
      setStatusErr(describeError(qua.reason, t('common.loadFailed')).message);
    }
    setLoading(false);
    setRunning(false);
  }, [project, t]);

  useEffect(() => {
    if (episodeNo == null) return;
    void load(episodeNo, false);
  }, [episodeNo, load]);

  // ---- 频谱 / 波形弹窗（后端渲染 PNG：第 1 张频谱、第 2 张波形）----
  const openVisuals = useCallback(async (source: string, label: string) => {
    if (!project) return;
    const src = (AUDIO_KINDS as readonly string[]).includes(source)
      ? (source as 'line' | 'voice_bank' | 'merged' | 'mix' | 'final')
      : 'final';
    setVis({
      open: true, source: src, label,
      title: t('audio.healthVisualsTitle', { name: label }),
      loading: true, error: '', images: [],
    });
    try {
      const j = await qcApi.checkAudio({ project_name: project, source: src, with_ai: true });
      const imgs = (j?.visuals || []).filter((x): x is string => typeof x === 'string' && !!x);
      setVis((p) => ({
        ...p, loading: false, images: imgs,
        error: imgs.length ? '' : t('audio.healthVisualsEmpty'),
      }));
    } catch (e) {
      const { message } = describeError(e, t('audio.healthVisualsFailed'));
      setVis((p) => ({ ...p, loading: false, error: message }));
    }
  }, [project, t]);

  // ---- 摘要（纯函数，口径见 utils/audioHealth.ts）----
  const s = summarizeAudioHealth(triage);

  const kindLabel = useCallback((k?: string) => {
    const suffix = KIND_KEY[String(k)];
    return suffix ? t('audio.triageKind' + suffix) : t('audio.triageKindOther');
  }, [t]);

  const kindStateView = (k: string): { variant: 'default' | 'success' | 'warning' | 'danger'; text: string } => {
    const st: KindState = kindStateOf(k, s);
    if (st.key === 'missing') return { variant: 'default', text: t('audio.healthKindStateMissing') };
    if (st.key === 'silent') return { variant: 'warning', text: t('audio.healthKindStateSilent', { n: st.silent }) };
    if (st.key === 'bad') return { variant: 'danger', text: t('audio.healthKindStateBad', { n: st.bad }) };
    return { variant: 'success', text: t('audio.healthKindStateOk') };
  };

  // ---- 门禁处置：已阻断 / 仅记录 / 未触发 / 无结论 ----
  const aq = deliverable?.meta?.audio_qc;
  const artifactExists = qualityRow?.artifact?.exists;
  const aStatus = String(qualityRow?.state?.A?.status || '');
  const releaseReady = qualityRow?.release?.ready;
  const gate: GateState = resolveGate({ audioQc: aq, artifactExists, stageAStatus: aStatus });
  const gateMeta: Record<GateState, { variant: 'default' | 'success' | 'warning' | 'danger'; labelKey: string; hintKey: string }> = {
    blocked: { variant: 'danger', labelKey: 'audio.healthGateBlocked', hintKey: 'audio.healthGateBlockedHint' },
    record_only: { variant: 'warning', labelKey: 'audio.healthGateRecordOnly', hintKey: 'audio.healthGateRecordOnlyHint' },
    untriggered: { variant: 'success', labelKey: 'audio.healthGateUntriggered', hintKey: 'audio.healthGateUntriggeredHint' },
    unknown: { variant: 'default', labelKey: 'audio.healthGateUnknown', hintKey: 'audio.healthGateUnknownHint' },
  };

  const silentShots = deliverable?.meta?.audio_qc_silent_shots || [];
  const metrics = aq?.metrics || {};
  const fmtNum = (v: unknown, digits = 1): string => (typeof v === 'number' ? v.toFixed(digits) : '-');
  const fmtRatio = (v: unknown): string => (typeof v === 'number' ? (v * 100).toFixed(0) + '%' : '-');

  const locate = (shotId?: string | number) => {
    if (shotId === undefined || shotId === null || shotId === '') return;
    onLocateShot?.(shotId, episodeNo ?? undefined);
  };

  return (
    <div className={cx('rounded-lg border border-line bg-surface', className)} role="region" aria-label={t('audio.healthTitle')}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="flex items-center gap-2 text-sm font-medium text-ink-1">
          <Volume2 className="h-4 w-4 text-ink-3" />{t('audio.healthTitle')}
        </span>
        <div className="flex flex-wrap items-center gap-2">
          {episodes.length > 1 ? (
            <label className="flex items-center gap-1 text-xs text-ink-2">
              <span>{t('sb.episode')}</span>
              <select
                value={String(episodeNo ?? '')}
                onChange={(e) => setEpisodeNo(Number(e.target.value) || 1)}
                className="rounded border border-line bg-surface px-1.5 py-0.5 text-xs text-ink-1"
              >
                {episodes.map((e) => (
                  <option key={String(e.episode_no)} value={String(e.episode_no)}>
                    {t('sb.episodeN', { n: Number(e.episode_no) })}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="flex items-center gap-1 text-xs text-ink-2">
            <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />
            {t('audio.healthDeep')}
          </label>
          <Button
            size="sm"
            variant="secondary"
            disabled={running || !project}
            onClick={() => void load(episodeNo ?? 1, deep)}
          >
            <RefreshCw className={cx('mr-1 h-3.5 w-3.5', running && 'animate-spin')} />
            {running ? t('audio.healthRefreshing') : t('audio.healthRefresh')}
          </Button>
        </div>
      </div>

      <div className="flex flex-col gap-3 p-3">
        {!project ? <div className="text-xs text-ink-3">{t('audio.healthNeedProject')}</div> : null}

        {/* 态 1：加载中 */}
        {loading && project ? (
          <div className="space-y-3">
            <Skeleton className="h-20" />
            <Skeleton className="h-28" />
          </div>
        ) : null}

        {/* 态 2：主数据（扫描计数）失败 —— 硬错误态，可原地重试 */}
        {!loading && err ? (
          <ErrorState
            title={t('audio.healthLoadFailed')}
            description={sanitizeErrorMessage(err)}
            code={errCode || undefined}
            onRetry={() => void load(episodeNo ?? 1, deep)}
          />
        ) : null}

        {/* 态 3：扫不到任何音频 —— 空态，明确告诉用户下一步做什么 */}
        {!loading && !err && triage && s.checked === 0 ? (
          <EmptyState
            icon={<Music className="h-10 w-10" />}
            title={t('audio.healthEmptyTitle')}
            description={t('audio.healthEmptyDesc')}
            action={(
              <Button size="sm" variant="secondary" onClick={() => void load(episodeNo ?? 1, deep)}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" />{t('audio.healthRefresh')}
              </Button>
            )}
          />
        ) : null}

        {/* 正常态：扫描总览 + 门禁处置 + 分类计数 + 无声镜号 + 不合格明细 */}
        {!loading && !err && triage && s.checked > 0 ? (
          <>
            {/* ① 扫描总览：数字原样来自后端（audio.checked / audio.scanned） */}
            <div className="rounded-md border border-line bg-surface-2 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-ink-1">{t('audio.healthScanSection')}</span>
                <Badge variant={s.normalCount === s.checked ? 'success' : 'warning'}>
                  {t('audio.healthScanTotal', { n: s.checked })}
                </Badge>
                <span className="text-xs text-ink-3">
                  {s.mode === 'live' ? t('audio.triageModeLive') : t('audio.triageModeHistory')}
                </span>
                <span className="ml-auto text-xs text-ink-3">{t('audio.healthScanTotalHint')}</span>
              </div>

              <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                {[
                  { label: t('audio.healthNormal'), n: s.normalCount, tone: 'text-success' },
                  { label: t('audio.healthSilent'), n: s.silentCount, tone: s.silentCount > 0 ? 'text-warning-strong' : 'text-ink-2' },
                  { label: t('audio.healthAbnormal'), n: s.abnormalCount, tone: s.abnormalCount > 0 ? 'text-danger-strong' : 'text-ink-2' },
                  { label: t('audio.healthMissing'), n: s.missingKinds.length, tone: s.missingKinds.length > 0 ? 'text-ink-3' : 'text-success' },
                ].map((c) => (
                  <div key={c.label} className="rounded border border-line bg-surface px-2 py-1.5">
                    <div className={cx('text-lg font-bold tabular-nums', c.tone)}>{c.n}</div>
                    <div className="text-xs text-ink-3">{c.label}</div>
                  </div>
                ))}
              </div>
              <p className="mt-1.5 text-xs text-ink-3">{t('audio.healthCountHint', {
                total: s.checked, failed: s.failedCount, silentDb: SILENT_MEAN_DB, ratio: Math.round(SILENT_RATIO * 100),
              })}</p>
              {s.truncated > 0 ? (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthFailedTruncated', { n: s.truncated })}</p>
              ) : null}
            </div>

            {/* ② 门禁处置：明确区分「已阻断」与「仅记录」（t19 默认只记录不阻断） */}
            <div
              className={cx('rounded-md border p-2.5',
                gate === 'blocked' ? 'border-danger/40 bg-danger/5'
                  : gate === 'record_only' ? 'border-warning/40 bg-warning/5'
                    : 'border-line bg-surface-2')}
            >
              <div className="flex flex-wrap items-center gap-2">
                {gate === 'blocked' ? <AlertTriangle className="h-4 w-4 text-danger" />
                  : gate === 'record_only' ? <AlertTriangle className="h-4 w-4 text-warning" />
                    : <CheckCircle2 className="h-4 w-4 text-success" />}
                <span className="text-xs font-medium text-ink-1">{t('audio.healthGateTitle')}</span>
                <Badge variant={gateMeta[gate].variant}>{t(gateMeta[gate].labelKey)}</Badge>
              </div>
              <p className="mt-1 text-xs text-ink-2">{t(gateMeta[gate].hintKey)}</p>
              <p className="mt-1 text-xs text-ink-3">{t('audio.healthGateDefaultNote')}</p>

              {/* 判据逐条列出：成片是否产出 / A 层状态 / 发布门禁 —— 用户可自行核对 */}
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-3">
                <span>{t('audio.healthGateEvidence')}</span>
                <span>
                  {artifactExists === true ? t('audio.healthGateEvDelivered')
                    : artifactExists === false ? t('audio.healthGateEvNotProduced')
                      : t('audio.healthGateEvUnknown')}
                </span>
                <span>{aStatus ? t('audio.healthGateEvStageA', { status: aStatus }) : t('audio.healthGateEvStageAUnknown')}</span>
                <span>
                  {typeof releaseReady === 'boolean'
                    ? t('audio.healthGateEvRelease', {
                      state: releaseReady ? t('audio.healthGateEvReleaseReady') : t('audio.healthGateEvReleaseBlocked'),
                    })
                    : t('audio.healthGateEvReleaseUnknown')}
                </span>
              </div>
              {statusErr ? <p className="mt-1 text-xs text-ink-3">{t('audio.healthStatusDegraded', { msg: statusErr })}</p> : null}
              {aq ? (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthGateMetrics', {
                  dur: fmtNum(metrics.duration, 1),
                  mean: fmtNum(metrics.mean_db, 1),
                  ratio: fmtRatio(metrics.speech_ratio),
                  silence: fmtNum(metrics.silence_sec, 2),
                })}</p>
              ) : (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthGateNoConclusion')}</p>
              )}
              {aq?.reason ? <p className="mt-1 text-xs text-ink-2">{aq.reason}</p> : null}
            </div>

            {/* ③ 分类计数：与后端 audio.scanned 逐字一致 + 类目状态 */}
            <div className="rounded-md border border-line bg-surface-2 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-ink-1">{t('audio.healthKindTableTitle')}</span>
                <span className="ml-auto text-xs text-ink-3">
                  {t('audio.healthKindMissingKinds', { n: s.missingKinds.length, total: AUDIO_KINDS.length })}
                </span>
              </div>
              <table className="mt-1.5 w-full text-xs">
                <thead>
                  <tr className="text-left text-ink-3">
                    <th className="py-0.5 font-normal">{t('audio.healthKindCol')}</th>
                    <th className="py-0.5 font-normal">{t('audio.healthKindCount')}</th>
                    <th className="py-0.5 font-normal">{t('audio.healthKindState')}</th>
                    <th className="py-0.5 font-normal" />
                  </tr>
                </thead>
                <tbody>
                  {AUDIO_KINDS.map((k) => {
                    const st = kindStateView(k);
                    const n = Number(s.scanned[k] || 0);
                    return (
                      <tr key={k} className="border-t border-line">
                        <td className="py-1 text-ink-2">{s.legend[k] || kindLabel(k)}</td>
                        <td className="py-1 tabular-nums text-ink-1">{n}</td>
                        <td className="py-1">
                          <Badge variant={st.variant}>{st.text}</Badge>
                        </td>
                        <td className="py-1 text-right">
                          {n > 0 ? (
                            <Button size="sm" variant="secondary" onClick={() => void openVisuals(k, kindLabel(k))}>
                              {t('audio.healthViewVisuals')}
                            </Button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="mt-1 text-xs text-ink-3">{t('audio.healthKindsHint')}</p>
            </div>

            {/* ④ 无声镜号（来自交付清单 meta.audio_qc_silent_shots）→ 可定位到具体镜 */}
            <div className="rounded-md border border-line bg-surface-2 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex items-center gap-1.5 text-xs font-medium text-ink-1">
                  <Film className="h-3.5 w-3.5 text-ink-3" />
                  {t('audio.healthSilentShotsTitle', { n: silentShots.length })}
                </span>
                {deliverable && aq?.available ? (
                  <span className="ml-auto text-xs text-ink-3">
                    {t('audio.healthSilentShotsAt', { at: String(aq?.generated_at || '-') })}
                  </span>
                ) : null}
              </div>

              {delivErr ? (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthDeliverableDegraded', { msg: delivErr })}</p>
              ) : !deliverable ? (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthSilentShotsPending')}</p>
              ) : silentShots.length === 0 ? (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthSilentShotsNone')}</p>
              ) : (
                <>
                  <p className="mt-1 text-xs text-ink-3">{t('audio.healthSilentShotsHint')}</p>
                  <div className="mt-1.5 space-y-1">
                    {silentShots.map((shot, i) => {
                      const sid = shot.shot_id ?? i;
                      return (
                        <div key={String(sid) + i} className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface px-2 py-1.5">
                          <Badge variant="danger">{t('audio.healthShotLabel', { n: String(sid) })}</Badge>
                          <span className="text-xs tabular-nums text-ink-3">
                            {t('audio.healthShotSlot', { start: fmtNum(shot.start, 1), end: fmtNum(shot.end, 1) })}
                          </span>
                          <span className="text-xs text-ink-3">{t('audio.healthShotRatio', { ratio: fmtRatio(shot.speech_ratio) })}</span>
                          <span className="text-xs text-ink-3">{t('audio.healthShotSilence', { sec: fmtNum(shot.silence_sec, 2) })}</span>
                          <span className="ml-auto flex items-center gap-1">
                            <Button
                              size="sm"
                              variant="secondary"
                              disabled={!onLocateShot}
                              title={onLocateShot ? t('audio.healthLocateShot') : t('audio.healthLocateUnavailable')}
                              onClick={() => locate(shot.shot_id)}
                            >
                              {t('audio.healthLocateShot')}
                            </Button>
                            <Button size="sm" variant="secondary" onClick={() => void openVisuals('final', t('audio.healthVisualsWholeTrack'))}>
                              {t('audio.healthViewVisuals')}
                            </Button>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>

            {/* ⑤ 不合格音轨明细（后端 audio.failed，最多 20 条） */}
            <div className="rounded-md border border-line bg-surface-2 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-ink-1">{t('audio.healthFailedTitle', { n: s.failedCount })}</span>
                {(aq?.silent_shot_count ?? 0) > 0 ? (
                  <Badge variant="danger">{t('audio.healthSilentShotCount', { n: aq?.silent_shot_count ?? 0 })}</Badge>
                ) : null}
              </div>
              {s.failed.length === 0 ? (
                <p className="mt-1 text-xs text-ink-3">{t('audio.healthFailedEmpty')}</p>
              ) : (
                <div className="mt-1.5 space-y-1">
                  {s.failed.map((f, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface px-2 py-1.5">
                      <Badge variant={isSilentFail(f) ? 'warning' : 'danger'}>
                        {isSilentFail(f) ? t('audio.healthSilent') : t('audio.healthAbnormal')}
                      </Badge>
                      <span className="text-xs text-ink-2">{kindLabel(f.kind)}</span>
                      <span className="min-w-0 flex-1 truncate text-xs text-ink-1">{f.line || '-'}</span>
                      {f.whole_track ? <span className="text-xs text-ink-3">{t('audio.triageWholeTrack')}</span> : null}
                      {f.metrics ? (
                        <span className="text-xs text-ink-3">{t('audio.triageMetrics', {
                          mean: fmtNum(f.metrics.mean_db, 1),
                          ratio: fmtRatio(f.metrics.speech_ratio),
                        })}</span>
                      ) : null}
                      <Button size="sm" variant="secondary" onClick={() => void openVisuals(String(f.kind || 'final'), kindLabel(f.kind))}>
                        {t('audio.healthViewVisuals')}
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        ) : null}

        {!loading && !err && !triage && project ? (
          <div className="text-xs text-ink-3">{t('audio.healthIntro')}</div>
        ) : null}
      </div>

      {/* 频谱 / 波形弹窗（后端渲染 PNG：第 1 张频谱、第 2 张波形） */}
      <Modal
        isOpen={vis.open}
        onClose={() => setVis((p) => ({ ...p, open: false }))}
        title={vis.title}
        size="lg"
      >
        <div className="space-y-3">
          {vis.loading ? <Skeleton className="h-48" /> : null}
          {!vis.loading && vis.error ? (
            <ErrorState
              description={sanitizeErrorMessage(vis.error)}
              onRetry={() => void openVisuals(vis.source, vis.label)}
            />
          ) : null}
          {!vis.loading && !vis.error ? (
            <>
              <p className="text-xs text-ink-3">{t('audio.healthVisualsHint')}</p>
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
