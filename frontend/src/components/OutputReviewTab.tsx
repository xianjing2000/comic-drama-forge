import React, { useState, useEffect, useCallback } from 'react';
import { useApp } from '@/context/AppContext';
import { autopilotApi, qualityApi, type QualityEpisodeRow } from '@/api/client';
import { Button, Textarea, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { ChevronDown, ChevronRight, ClipboardCheck, FileText, Film, FolderOpen } from '@/components/ui/icons';
import { EpisodeReviewPanel, STATUS_DOT } from '@/components/EpisodeReviewPanel';
import { useToast } from '@/components/ui/toast';
import { describeError, sanitizeErrorMessage } from '@/utils/errorText';
import type { Deliverable } from '@/types';

interface AssetItem {
  name: string;
  url?: string;
  file?: string;
  size?: number;
  [key: string]: any;
}

interface OutputReviewTabProps {
  projectKey: string;
  assets: { final?: AssetItem[]; counts?: Record<string, number> } | null;
}

export function OutputReviewTab({ projectKey, assets }: OutputReviewTabProps) {
  const { t } = useApp();
  const [deliverables, setDeliverables] = useState<Deliverable[]>([]);
  const [pending, setPending] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<number | null>(null);
  const [rejecting, setRejecting] = useState<number | null>(null);
  const [reason, setReason] = useState('');
  const [playing, setPlaying] = useState<string | null>(null);
  // 审片：每集四层状态（行内画 A/B/C/D 状态点，并决定该集是否有可复核内容）
  const [quality, setQuality] = useState<QualityEpisodeRow[]>([]);
  // 当前展开了「四层状态 · 逐镜复核」的集号（一次只展开一集，避免整页被撑得过长）
  const [expanded, setExpanded] = useState<number | null>(null);
  // ⭐ t28：交付页「无声镜号」点中的镜号 —— 用于高亮该 chip，并传给逐镜复核面板定位
  const [focusShot, setFocusShot] = useState<number | string | null>(null);
  const toast = useToast();

  const loadAll = useCallback(async () => {
    if (!projectKey) return;
    setLoading(true);
    try {
      // 项目级导出（/api/export/*）已下线：剪辑工程文件列表已移除，不再查询 exportApi
      const deliverRes = await autopilotApi.deliverables(projectKey);
      setDeliverables(deliverRes?.items || []);
      setPending(deliverRes?.pending || 0);
      // 审片数据是「增补」：取不到时成片验收照常可用，不把整页打成硬错误态
      try {
        const q = await qualityApi.episodes(projectKey);
        setQuality(q?.episodes || []);
      } catch {
        setQuality([]);
      }
    } catch (e) {
      // t27：统一脱敏 —— 后端异常原文里可能带本机绝对路径（t26 已在工作台侧收口，
      // 交付页此前是漏网的展示面之一）
      setError(describeError(e, t('common.loadFailed')).message);
    } finally {
      setLoading(false);
    }
  }, [projectKey]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const review = async (episodeNo: number, verdict: 'accepted' | 'rejected', note = '') => {
    setBusy(episodeNo);
    setError('');
    try {
      await autopilotApi.reviewDeliverable({
        project: projectKey,
        episode_no: episodeNo,
        review: verdict,
        note,
      });
      toast.success(verdict === 'accepted' ? t('deliver.acceptedOk') : t('deliver.rejectedOk'));
      setRejecting(null);
      setReason('');
      await loadAll();
    } catch (e) {
      const { message, code, status } = describeError(e, t('deliver.actionFailed'));
      setError(message);
      // t27：HTTP 409 + code=deliverable_stale 表示「成片已过期，后端硬拦」（t17）。
      // 此时按钮本应已禁用，这里是兜底（例如另一个窗口刚打上失效标记）：
      // 不能只说「操作失败」，要直接告诉用户下一步去做「重新合成」。
      toast.error((code === 'deliverable_stale' || status === 409)
        ? message + t('deliver.staleRerenderHint')
        : message);
    } finally {
      setBusy(null);
    }
  };

  /**
   * 重新合成该集成片（t27，F1 的「重新合成」动作）。
   *
   * 为什么走托管 run-once 而不是 /api/final/video：后者要求前端传 script_path，
   * 且该路径必须落在项目 output/ 内；前端只知道项目名与集号，硬拼服务器路径既脆弱
   * （命名约定会变）又会变成路径穿越面。run-once 由后端自己解析剧本，而 t17 写入的
   * 失效标记会让 probe_final / probe_upscale 判定「需重做」，已完成的其它步骤照常跳过
   * —— 正是「重新合成成片（含超分）」的语义。
   */
  /**
   * 定位到某一镜（t28）：展开该集的逐镜复核，并把镜号交给 EpisodeReviewPanel。
   *
   * 为什么用「展开 + 传镜号」而不是自己在外面滚 DOM：逐镜卡片由 EpisodeReviewPanel
   * 按场次分组渲染（默认只展开第一个场次），目标镜很可能躺在收起的场次里。
   * 该组件新增了一个**可选** focusShot prop：自动展开所在场次 + 滚动 + 高亮；
   * 不传时行为与改动前完全一致（零回归）。
   */
  const focusShotIn = (episodeNo: number, shotId?: number | string) => {
    if (shotId === undefined || shotId === null || shotId === '') return;
    setExpanded(episodeNo);
    setFocusShot(shotId);
  };

  const recompose = async (episodeNo: number) => {
    setBusy(episodeNo);
    setError('');
    try {
      await autopilotApi.runOnce({ project_name: projectKey, episode_no: episodeNo });
      toast.success(t('deliver.rerenderStarted', { n: episodeNo }));
      await loadAll();
    } catch (e) {
      const { message } = describeError(e, t('deliver.rerenderFailed'));
      setError(message);
      toast.error(message);
    } finally {
      setBusy(null);
    }
  };

  const sizeText = (n?: number) => (n ? `${(n / 1048576).toFixed(1)} MB` : '—');

  const statusBadge = (d: Deliverable) => {
    if (d.review === 'accepted') {
      return { text: t('deliver.accepted'), cls: 'bg-success-subtle text-success-strong' };
    }
    if (d.review === 'rejected') {
      return { text: t('deliver.rejected'), cls: 'bg-danger-subtle text-danger-strong' };
    }
    return { text: t('deliver.pending'), cls: 'bg-warning-subtle text-warning-strong' };
  };

  const triggerDownload = (url: string, filename?: string) => {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const finals = assets?.final || [];
  /** 成片验收行 = 有成片的集 ∪ 有审片数据的集（按集号升序）。
   *  为什么取并集：只列成片会漏掉「已有产物、但还没成片」的集 —— 那恰恰是最需要人工复核的
   *  那些集（例如视频刚出、卡在成片前）。若只列成片，「审片」合并进来后就等于没有入口了。 */
  const reviewRows = React.useMemo(() => {
    const byEp = new Map<number, { episode_no: number; deliverable?: Deliverable; quality?: QualityEpisodeRow }>();
    for (const q of quality) {
      if (!byEp.has(q.episode_no)) byEp.set(q.episode_no, { episode_no: q.episode_no });
      byEp.get(q.episode_no)!.quality = q;
    }
    for (const d of deliverables) {
      if (!byEp.has(d.episode_no)) byEp.set(d.episode_no, { episode_no: d.episode_no });
      byEp.get(d.episode_no)!.deliverable = d;
    }
    return [...byEp.values()].sort((a, b) => a.episode_no - b.episode_no);
  }, [quality, deliverables]);
  /** 是否还有任何数据可展示：决定 error 走「硬失败 ErrorState」还是「软失败行内提示条」 */
  const hasContent = finals.length > 0
    || deliverables.length > 0 || reviewRows.length > 0;

  // 加载态：沿用标题 + 两个区块卡片的形态
  if (loading) {
    return (
      <div className="space-y-6" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <Skeleton className="h-6 w-28" />
            <Skeleton className="h-4 w-40" />
          </div>
          <Skeleton className="h-8 w-16" />
        </div>
        <Skeleton className="h-56 rounded-lg" />
        <Skeleton className="h-56 rounded-lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-ink-1">{t('wb.output')}</h3>
          <p className="text-sm text-ink-2 mt-0.5">
            {t('deliver.exportSubtitle')}
          </p>
        </div>
        <Button size="sm" variant="secondary" onClick={loadAll} disabled={loading || busy !== null}>
          {t('common.refresh')}
        </Button>
      </div>

      {/* 软失败：页面仍有数据在展示（刷新/生成/验收动作失败），保留紧凑行内提示条，不吃掉已展示内容 */}
      {error && hasContent && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-sm text-danger-strong">
          {sanitizeErrorMessage(error)}
        </div>
      )}
      {/* 区域 1: 导出配置 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <h4 className="font-semibold text-ink-1 mb-3 flex items-center gap-2">
          <Film className="h-4 w-4" /> {t('deliver.exportConfig')}
        </h4>

        {/* 成片下载 */}
        <div className="mb-4">
          <h5 className="text-sm font-medium text-ink-1 mb-2 flex items-center gap-1.5">
            <FolderOpen className="h-4 w-4" /> {t('deliver.finalList', { n: finals.length })}
          </h5>
          {finals.length > 0 ? (
            <div className="space-y-2">
              {finals.map((item, idx) => (
                <div key={idx} className="flex items-center justify-between p-2 bg-surface-2 rounded">
                  <div className="min-w-0">
                    <p className="font-medium text-ink-1 truncate text-sm">{item.name}</p>
                    {item.size && <p className="text-xs text-ink-2">{sizeText(item.size)}</p>}
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <Button size="sm" variant="secondary" onClick={() => window.open(item.url, '_blank')}>
                      {t('common.preview')}
                    </Button>
                    <Button size="sm" onClick={() => triggerDownload(`${item.url}?download=1`, item.name)}>
                      {t('common.download')}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-2 py-2">{t('deliver.noFinalHint')}</p>
          )}
        </div>

        {/* 剪辑工程文件（/api/export/* 已下线） */}
        <div>
          <h5 className="text-sm font-medium text-ink-1 mb-2 flex items-center gap-1.5">
            <FileText className="h-4 w-4" /> {t('deliver.projectFiles', { n: 0 })}
          </h5>
          <p className="text-sm text-ink-2 py-2">
            {t('deliver.exportDisabledHint')}
          </p>
        </div>
      </div>

      {/* 区域 2: 成片验收（每集行内可展开「四层状态 + 逐镜复核」）
          2026-09-29：原独立「审片」标签页并入这里 —— 复核与验收是同一件事的前后两步；
          展开的是**该集自己的**四层状态与逐镜并排画面，不再另起一块。 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <h4 className="font-semibold text-ink-1 mb-3 flex items-center gap-2">
          <ClipboardCheck className="h-4 w-4" /> {t('deliver.finalReview')}
          <span className="ml-auto text-xs font-normal text-ink-2">
            {t('deliver.pendingOf', { pending, total: deliverables.length })}
          </span>
        </h4>

        {reviewRows.length === 0 ? (
          error ? (
            /* 硬失败：该区块没有任何数据可展示，且加载出错 → 整块错误态 + 重试 */
            <ErrorState
              title={t('project.loadingFailed')}
              description={sanitizeErrorMessage(error)}
              onRetry={loadAll}
            />
          ) : (
            <EmptyState
              icon={<ClipboardCheck className="h-10 w-10" />}
              title={t('deliver.noDeliverables')}
              description={t('deliver.noDeliverablesHint')}
              // t27（F4）：空态最需要「下一步能做什么」，ui/index.tsx 的 action 槽位
              // 本来就是为此预留（该组件注释写明），此前这里没传 → 用户只能干看着。
              action={
                <Button size="sm" variant="secondary" onClick={loadAll} disabled={busy !== null}>
                  {t('deliver.noDeliverablesAction')}
                </Button>
              }
            />
          )
        ) : (
          <div className="space-y-3">
            {reviewRows.map(({ episode_no, deliverable: d, quality: q }) => {
              const b = d ? statusBadge(d) : null;
              const canReview = busy === null;
              const playable = !!d?.url && d?.exists !== false;
              const isOpen = expanded === episode_no;
              const meta = d?.meta;
              // ⭐ t28：t19 后端把成片整轨音频结论（含无声镜号）挂进了交付物 meta
              const silentShots = meta?.audio_qc_silent_shots || [];
              const st = q?.state || {};
              // t27（F1）：成片被标记「已过期」= 后端 set_deliverable_review 会硬拒验收
              // （t17），界面必须同步禁用「通过/驳回」并给出「重新合成」入口，
              // 否则用户点下去只会收到一个 500/409 原始报错。
              const staleBlocked = Boolean(meta?.stale);
              return (
                <div key={episode_no} className="border border-line rounded-lg p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-ink-1">
                          {t('deliver.episodeNo', { n: episode_no })}
                        </span>
                        {meta?.title && (
                          <span className="text-sm text-ink-2">{meta.title}</span>
                        )}
                        {b && (
                          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${b.cls}`}>{b.text}</span>
                        )}
                        {!d && (
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-surface-2 text-ink-2">
                            {t('review.notDelivered')}
                          </span>
                        )}
                        {d?.exists === false && (
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-danger-subtle text-danger-strong">
                            {t('deliver.missing')}
                          </span>
                        )}
                        {meta?.stale && (
                          <span
                            className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong"
                            title={meta.stale.reason || t('deliver.stale')}
                          >
                            {t('deliver.stale')}
                          </span>
                        )}
                        {meta?.incomplete_shots && (
                          <span
                            className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong"
                            title={meta?.warning || t('deliver.incompleteTitle')}
                          >
                            {t('deliver.incompleteShots', { ready: meta?.shots_ready ?? '?', total: meta?.shots_total ?? '?' })}
                          </span>
                        )}
                        {/* 四层质量状态点：不展开也能一眼看出 A/B/C/D 卡在哪一层 */}
                        <span className="flex items-center gap-1" title={t('review.stagesTitle')}>
                          {(['A', 'B', 'C', 'D'] as const).map((s) => (
                            <span
                              key={s}
                              className={`h-2 w-2 rounded-full ${STATUS_DOT[st[s]?.status || 'pending']}`}
                              title={s + ': ' + (st[s]?.status || 'pending')}
                            />
                          ))}
                        </span>
                      </div>
                      {d && (
                        <p className="text-xs text-ink-2 mt-1">
                          {d.filename} · {sizeText(d.size)}
                          {typeof meta?.duration_sec === 'number' && meta.duration_sec > 0 && (
                            <span> · {meta.duration_sec.toFixed(1)}s</span>
                          )}
                        </p>
                      )}
                      {!d && (
                        <p className="text-xs text-ink-2 mt-1">
                          {t('review.noDeliverableHint')}
                        </p>
                      )}
                      {staleBlocked && (
                        <div className="mt-2 rounded-md border border-warning/40 bg-warning-subtle px-2 py-1.5">
                          <p className="text-xs font-medium text-warning-strong">
                            {t('deliver.staleBlockedHint')}
                          </p>
                          <p className="mt-0.5 text-xs text-ink-2">
                            {meta?.stale?.reason || t('deliver.staleReasonDefault')}
                            {meta?.stale?.detail?.shot_id != null && (
                              <span>{t('deliver.staleShotRef', { n: meta.stale.detail.shot_id })}</span>
                            )}
                            {t('deliver.staleRerenderHint')}
                          </p>
                        </div>
                      )}
                      {/* ⭐ t28：成片音轨客观层结论 + 无声镜号定位（数据来自 t19 后端契约） */}
                      {d && (meta?.audio_qc ? (
                        <div className="mt-2 rounded-md border border-line bg-surface-2 px-2 py-1.5">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-xs font-medium text-ink-1">{t('audio.dubSilentTitle')}</span>
                            {silentShots.length > 0 ? (
                              <span className="rounded-full border border-warning/40 bg-warning-subtle px-2 py-0.5 text-xs text-warning-strong">
                                {t('audio.dubSilentChip', { n: silentShots.length })}
                              </span>
                            ) : (
                              <span className="text-xs text-ink-3">{t('audio.dubSilentNone')}</span>
                            )}
                            {meta.audio_qc.metrics ? (
                              <span className="ml-auto text-xs text-ink-3">
                                {t('audio.dubSilentMetrics', {
                                  dur: meta.audio_qc.metrics.duration != null ? meta.audio_qc.metrics.duration.toFixed(1) : '-',
                                  mean: meta.audio_qc.metrics.mean_db != null ? meta.audio_qc.metrics.mean_db : '-',
                                  ratio: meta.audio_qc.metrics.speech_ratio != null ? (meta.audio_qc.metrics.speech_ratio * 100).toFixed(0) + '%' : '-',
                                })}
                              </span>
                            ) : null}
                          </div>
                          {silentShots.length > 0 ? (
                            <>
                              <p className="mt-0.5 text-xs text-ink-2">{t('audio.dubSilentHint')}</p>
                              <div className="mt-1 flex flex-wrap items-center gap-1">
                                {silentShots.slice(0, 20).map((s, i) => {
                                  const focused = expanded === episode_no && String(focusShot) === String(s.shot_id);
                                  return (
                                    <button
                                      key={String(s.shot_id ?? i)}
                                      type="button"
                                      onClick={() => focusShotIn(episode_no, s.shot_id)}
                                      className={focused
                                        ? 'rounded-full border border-warning bg-warning-subtle px-2 py-0.5 text-xs text-warning-strong'
                                        : 'rounded-full border border-line-strong px-2 py-0.5 text-xs text-ink-1 transition-colors hover:bg-surface-2'}
                                    >
                                      {t('audio.dubSilentChip', { n: s.shot_id ?? '?' })}
                                    </button>
                                  );
                                })}
                                {silentShots.length > 20 ? (
                                  <span className="text-xs text-ink-3">
                                    {t('audio.dubSilentMore', { n: silentShots.length - 20 })}
                                  </span>
                                ) : null}
                              </div>
                              {expanded === episode_no && focusShot !== null ? (
                                <p className="mt-1 text-xs text-warning-strong">
                                  {t('audio.dubSilentFocused', { n: focusShot })}
                                </p>
                              ) : null}
                            </>
                          ) : null}
                        </div>
                      ) : (
                        <p className="mt-1 text-xs text-ink-3">{t('audio.dubSilentPending')}</p>
                      ))}
                      {meta?.incomplete_shots && meta?.warning && (
                        <p className="text-xs text-warning-strong mt-1">
                          {meta.warning}
                        </p>
                      )}
                      {d?.review === 'rejected' && d?.review_note && (
                        <p className="text-xs text-ink-2 mt-1">
                          {t('deliver.rejectNoteInline', { note: d.review_note })}
                        </p>
                      )}
                    </div>

                    <div className="flex flex-wrap justify-end gap-2 shrink-0">
                      {playable && (
                        <>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => setPlaying(playing === d?.filename ? null : d!.filename)}
                          >
                            {playing === d?.filename ? t('common.close') : t('common.play')}
                          </Button>
                          <a
                            href={`${d!.url}?download=1`}
                            className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line-strong text-ink-1 hover:bg-surface-2 transition-colors"
                          >
                            {t('common.download')}
                          </a>
                        </>
                      )}
                      {d && staleBlocked && (
                        <Button
                          size="sm"
                          variant="brand"
                          loading={busy === episode_no}
                          onClick={() => void recompose(episode_no)}
                          title={t('deliver.rerenderHint')}
                        >
                          {t('deliver.rerenderFinal')}
                        </Button>
                      )}
                      {d && (
                        <>
                          <Button
                            size="sm"
                            onClick={() => review(episode_no, 'accepted')}
                            disabled={!canReview || staleBlocked || d.review === 'accepted'}
                            title={staleBlocked ? t('deliver.blockedApproveTip') : undefined}
                          >
                            {t('deliver.approve')}
                          </Button>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => {
                              setRejecting(rejecting === episode_no ? null : episode_no);
                              setReason('');
                            }}
                            disabled={!canReview || staleBlocked || d.review === 'rejected'}
                            title={staleBlocked ? t('deliver.blockedApproveTip') : undefined}
                          >
                            {t('deliver.reject')}
                          </Button>
                        </>
                      )}
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => setExpanded(isOpen ? null : episode_no)}
                      >
                        <span className="flex items-center gap-1">
                          {isOpen
                            ? <ChevronDown className="h-3.5 w-3.5" />
                            : <ChevronRight className="h-3.5 w-3.5" />}
                          {isOpen ? t('review.collapse') : t('review.expand')}
                        </span>
                      </Button>
                    </div>
                  </div>

                  {d && playing === d.filename && d.url && (
                    <video src={d.url} controls className="w-full mt-3 rounded-lg bg-black" />
                  )}

                  {d && rejecting === episode_no && (
                    <div className="mt-3 pt-3 border-t border-line space-y-2">
                      <Textarea
                        value={reason}
                        onChange={setReason}
                        label={t('deliver.rejectReasonLabel')}
                        rows={2}
                        placeholder={t('deliver.rejectReasonInput')}
                      />
                      <div className="flex gap-2">
                        <Button size="sm" onClick={() => review(episode_no, 'rejected', reason)} disabled={!canReview || staleBlocked}>
                          {t('deliver.confirmReject')}
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => {
                            setRejecting(null);
                            setReason('');
                          }}
                        >
                          {t('common.cancel')}
                        </Button>
                      </div>
                    </div>
                  )}

                  {/* 行内展开：该集自己的四层状态 + 逐镜并排复核（C 层复核 / D 层发布） */}
                  {isOpen && (
                    <div className="mt-3 pt-3 border-t border-line">
                      <EpisodeReviewPanel
                        projectKey={projectKey}
                        episode={episode_no}
                        onChanged={loadAll}
                        // ⭐ t28：点过「无声镜号」才传 —— 不传时该面板行为与改动前一致
                        focusShot={expanded === episode_no ? (focusShot ?? undefined) : undefined}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
