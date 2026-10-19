/** StartProductionCard —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { STATUS_POLL_MS, STEP_LABEL_KEY, type StartProductionCardProps } from '@/pages/workbenchShared';
import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { projectsApi, keyframesApi, storyboardApi, videoApi, qcApi, autopilotApi, upscaleApi, chatApi, agentApi, episodesApi, novelsSplitPlanApi, preflightApi, characterOutfits, characterSheetUpload, assetPrecipitation, generationApi, assetsApi, asOverwriteConfirm, overwriteScopeSummary, type OverwriteConfirmPayload, type VideoMode, type EpisodeScenesResponse, type ChapterPreflightResult, type AssetPrecipitationResponse, type PrecipitationStatus } from '@/api/client';
import { Button, ConfirmDialog, Input, EmptyState, ErrorState, Loading, Skeleton, Modal, Select, ReorderControls } from '@/components/ui';
import {
  AlertTriangle, BarChart3, Box, Check, CheckCircle2, Clapperboard, ClipboardCheck, ClipboardList, FileText, Film,
  FolderOpen, ImageIcon, MessageSquare, Mountain, Music, Pause, Play, RefreshCw, Share2, Upload, User, X, ZoomIn,
} from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import { useComfyProgress } from '@/hooks/useComfyProgress';
import { subscribeEvents, getSnapshot, isConnected } from '@/api/events';
import { ChatPanel } from '@/components/ChatPanel';
import { AgentStatusCard } from '@/components/AgentStatusCard';
import { ProductionModeCard } from '@/components/ProductionModeCard';
import { ProductionProgress } from '@/components/ProductionProgress';
import { EpisodeSwitcher } from '@/components/EpisodeSwitcher';
import { ShotPromptEditor } from '@/components/ShotPromptEditor';
import { StoryboardTab } from '@/components/StoryboardTab';
import { StoryboardHubTab } from '@/components/StoryboardHubTab';
import { OverviewTab } from '@/components/OverviewTab';
import { assetSrc, type AssetItem } from '@/pages/workbenchShared';
import { CharacterOutfitsSection } from '@/components/CharacterOutfitsSection';
import { AssetPrecipitationSection } from '@/components/AssetPrecipitationSection';
import { AssetPreviewModal } from '@/components/AssetPreviewModal';
import { UploadSheetModal } from '@/components/UploadSheetModal';
import { AssetCard } from '@/components/AssetCard';
import { QcTab } from '@/components/QcTab';
import { UpscaleTab } from '@/components/UpscaleTab';
import { AgentTrace } from '@/components/AgentTrace';
import { getAgentSession, type ChatMsg } from '@/agentSession';
import { OutputReviewTab } from '@/components/OutputReviewTab';
import { AudioTab } from '@/components/AudioTab';
import type { Project, Deliverable, UpscaleEnv, UpscaleSource, UpscaleTask, UpscaleArtifact, AgentStep, AutopilotCurrent, AutopilotStatus, CharacterOutfit, ShotGridStatusResponse, ShotGridTaskState } from '@/types';
import { describeError, sanitizeErrorMessage } from '@/utils/errorText';
import { summarizeStoryboardQuality, mergeBackendQuality, shotQuality, criticalIssuesOf, softReasonOf } from '@/utils/storyboardQuality';
import AutonomousPanel from '@/components/AutonomousPanel';
import QualityPanel from '@/components/QualityPanel';
import FailurePanel from '@/components/FailurePanel';
import TriagePanel from '@/components/TriagePanel';
import AudioHealthPanel from '@/components/AudioHealthPanel';
import ExportPanel from '@/components/ExportPanel';

export function StartProductionCard({
  projectKey,
  episodes,
  episodesLoading = false,
  episodesError = '',
  onRefreshEpisodes,
}: StartProductionCardProps) {
  const { t } = useApp();
  const toast = useToast();

  const [status, setStatus] = useState<AutopilotStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState('');
  const [selectedEp, setSelectedEp] = useState('');
  const [starting, setStarting] = useState(false);
  /** 本次会话内「已启动」的集号：点击后立刻可见，不必等下一次轮询 */
  const [started, setStarted] = useState<number | null>(null);
  const [actionError, setActionError] = useState('');

  const project = (projectKey || '').trim();

  const refreshStatus = React.useCallback(async () => {
    if (!project) {
      setStatus(null);
      setStatusLoading(false);
      return;
    }
    try {
      const st = await autopilotApi.status(project);
      setStatus(st);
      setStatusError('');
    } catch (e) {
      // 轮询失败不静默：本组件要据此决定「能不能开跑」，状态不明就不给点
      setStatusError(describeError(e, t('wb.startStatusFailed')).message);
    } finally {
      setStatusLoading(false);
    }
  }, [project, t]);

  useEffect(() => {
    let alive = true;
    const tick = async () => { if (alive) await refreshStatus(); };
    void tick();
    const timer = setInterval(() => { void tick(); }, STATUS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [refreshStatus]);

  // 剧本重新生成后旧集号可能消失：清掉选择，避免把不存在的集号提交给后端
  useEffect(() => {
    if (selectedEp === '') return;
    if (!episodes.some((n) => String(n) === selectedEp)) setSelectedEp('');
  }, [episodes, selectedEp]);

  const cur: AutopilotCurrent | null = (status?.current as AutopilotCurrent) || null;
  const otherProjectRunning = Boolean(
    (status as unknown as { other_project_running?: boolean } | null)?.other_project_running,
  );
  const running = Boolean(cur) || otherProjectRunning;
  const selected = selectedEp === '' ? null : Number(selectedEp);
  const statusKnown = status !== null;
  const noEpisodes = episodes.length === 0;

  const stepLabel = (step?: string) => {
    const base = String(step || '').split(':')[0];
    const key = STEP_LABEL_KEY[base];
    if (!key) return base;
    const label = t(key);
    // 语言包缺键时 t() 原样返回 key —— 别把 key 当文案显示出去
    return label === key ? base : label;
  };

  /** 前置条件 → 可读原因（空串 = 可以开始）。顺序即优先级：越靠前越先满足。 */
  const disabledReason = !project
    ? t('wb.startDisabledNoProject')
    : running
      ? (cur
        ? t('wb.startDisabledRunning', {
          n: cur.episode ?? '-',
          step: stepLabel(cur.step) || t('wb.startStepUnknown'),
        })
        : t('wb.startDisabledOtherProject'))
      : (episodesLoading && noEpisodes)
        ? t('wb.startEpisodesLoading')
        : (episodesError && noEpisodes)
          ? t('wb.startEpisodesFailed')
          : noEpisodes
            ? t('wb.startDisabledNoEpisodes')
            : selected === null
              ? t('wb.startDisabledNoEpisode')
              : !statusKnown
                ? (statusLoading ? t('wb.startDisabledStatusLoading') : t('wb.startDisabledStatusUnknown'))
                : '';
  const canStart = disabledReason === '' && !starting;

  const start = async () => {
    if (selected === null || starting) return;
    setStarting(true);
    setActionError('');
    try {
      // ⭐ 主路径唯一调用点：立即生产这一集（后端异步，返回 task_id 后置 current 供轮询）
      await autopilotApi.runOnce({ project_name: project, episode_no: selected });
      setStarted(selected);
      toast.success(t('wb.startStarted', { n: selected }));
      await refreshStatus();
    } catch (e) {
      // 后端的 400/409（未关联小说 / 该集不存在 / 该集已在生产 / 需人工介入）都带可读原因
      const { message } = describeError(e, t('wb.startFailed'));
      setActionError(message);
      toast.error(message);
    } finally {
      setStarting(false);
    }
  };

  const percent = Math.max(0, Math.min(100, Number(cur?.percent) || 0));

  return (
    <section
      className="rounded-lg border border-line bg-surface p-4"
      aria-labelledby="start-production-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id="start-production-title"
            className="flex items-center gap-2 text-sm font-semibold text-ink-1"
          >
            <Film className="h-4 w-4" />
            {t('wb.startProduction')}
          </h3>
          <p className="mt-0.5 max-w-2xl text-xs text-ink-2">{t('wb.startProductionHint')}</p>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
            running ? 'bg-state-running-subtle text-state-running-strong' : 'bg-state-pending-subtle text-state-pending-strong'
          }`}
        >
          {running && <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-state-running" aria-hidden="true" />}
          {running ? t('state.running') : t('state.pending')}
        </span>
      </div>

      {/* ── 加载态：状态没拿到之前不给按钮（状态不明时点击 = 盲提交，可能重复起任务） ── */}
      {!statusKnown && !statusError && (
        <div
          className="mt-3 space-y-2"
          role="status"
          aria-live="polite"
          aria-label={t('wb.startStatusLoading')}
        >
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-40" />
        </div>
      )}

      {/* ── 错误态（硬）：拿不到生产状态 → 区块级 ErrorState + 重试，文案已脱敏 ── */}
      {!statusKnown && statusError && (
        <div className="mt-3">
          <ErrorState
            title={t('wb.startStatusFailed')}
            description={t('wb.startStatusFailedHint')}
            code={statusError}
            onRetry={() => { void refreshStatus(); }}
          />
        </div>
      )}

      {statusKnown && (
        <div className="mt-3 space-y-3">
          {/* 软失败：状态读到了，但这次轮询失败（保留操作区，只提示） */}
          {statusError && (
            <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-subtle px-2.5 py-1.5 text-xs text-warning-strong">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="break-words">{sanitizeErrorMessage(statusError)}</span>
              <button
                type="button"
                className="ml-auto shrink-0 underline underline-offset-2"
                onClick={() => { void refreshStatus(); }}
              >
                {t('common.retry')}
              </button>
            </div>
          )}

          {/* 空态：还没有可生产的集 —— 说明「为什么是空的、下一步做什么」 */}
          {noEpisodes && !episodesLoading && (
            <EmptyState
              icon={<Film className="h-10 w-10" />}
              title={t('wb.startEmptyTitle')}
              // 父组件带下来的是原始 err.message（可能含本机绝对路径）→ 展示前必须脱敏
              description={episodesError
                ? `${t('wb.startEpisodesFailed')} ${sanitizeErrorMessage(episodesError)}`
                : t('wb.startEmptyHint')}
              action={onRefreshEpisodes ? (
                <Button size="sm" variant="secondary" onClick={onRefreshEpisodes}>
                  <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                  {t('wb.startEmptyAction')}
                </Button>
              ) : undefined}
            />
          )}

          <div className="flex flex-wrap items-end gap-3">
            <div className="w-56">
              <Select
                label={t('wb.startEpisodeLabel')}
                value={selectedEp}
                onChange={setSelectedEp}
                disabled={starting || noEpisodes || running}
                options={[
                  {
                    value: '',
                    label: episodesLoading && noEpisodes
                      ? t('wb.startEpisodesLoading')
                      : (noEpisodes ? t('wb.startDisabledNoEpisodes') : t('wb.startEpisodePlaceholder')),
                  },
                  ...episodes.map((n) => ({ value: String(n), label: t('wb.startEpisodeOption', { n }) })),
                ]}
              />
            </div>
            <Button
              variant="brand"
              size="lg"
              loading={starting}
              disabled={!canStart}
              onClick={() => { void start(); }}
              title={disabledReason || t('wb.startProductionHint')}
            >
              <Play className="h-4 w-4" />
              {starting ? t('wb.startStarting') : t('wb.startProduction')}
            </Button>
          </div>

          {/* 前置条件原因：禁用时必须让用户看到「为什么 + 下一步」 */}
          {disabledReason && (
            <p className="text-xs text-ink-2" role="note">{disabledReason}</p>
          )}
          {!disabledReason && (
            <p className="text-xs text-ink-3">{t('wb.startBusyHint')}</p>
          )}

          {/* 启动失败（后端已给可读原因；文案经脱敏，不含本机绝对路径） */}
          {actionError && (
            <div
              className="rounded-md border border-danger/30 bg-danger-subtle px-3 py-2 text-xs text-danger-strong"
              role="alert"
            >
              <span className="font-medium">{t('wb.startFailed')}</span>
              <span className="ml-2 break-words">{sanitizeErrorMessage(actionError)}</span>
              <p className="mt-0.5 text-ink-2">{t('wb.startFailedHint')}</p>
            </div>
          )}

          {/* 已启动 + 当前步骤：点击后原地可见（不依赖用户自己去看别处） */}
          {started !== null && (
            <div
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-success/30 bg-success-subtle px-3 py-2 text-xs text-success-strong"
              role="status"
              aria-live="polite"
            >
              <span className="flex items-center gap-1.5 font-medium">
                <CheckCircle2 className="h-4 w-4" />
                {t('wb.startStarted', { n: started })}
              </span>
              <span>
                {cur
                  ? t('wb.startCurrentStep', { step: stepLabel(cur.step) || t('wb.startStepUnknown') })
                  : t('wb.startQueued')}
              </span>
              {cur && (
                <span className="tabular-nums">{t('wb.startProgress', { percent })}</span>
              )}
              {cur?.message && (
                // 后端进度消息是运行时数据：走脱敏，避免异常消息里夹带本机路径
                <span className="text-ink-2">{sanitizeErrorMessage(String(cur.message))}</span>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}