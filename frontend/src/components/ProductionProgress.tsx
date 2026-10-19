/** ProductionProgress —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { PRODUCTION_STEPS } from '@/pages/workbenchShared';
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
import { StartProductionCard } from '@/components/StartProductionCard';
import { ProductionModeCard } from '@/components/ProductionModeCard';
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

export function ProductionProgress({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const toast = useToast();
  const [cur, setCur] = useState<AutopilotCurrent | null>(null);
  const [paused, setPaused] = useState(false);
  const [ctrlBusy, setCtrlBusy] = useState(false);
  const [failurePause, setFailurePause] = useState<{ episode?: number; reason?: string; error?: string } | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setInterval> | null = null;
    const poll = async () => {
      try {
        const st = await autopilotApi.status(projectKey);
        if (!alive) return;
        setCur((st.current as AutopilotCurrent) || null);
        setPaused(Boolean(st.paused));
        // ⚠️ 2026-10-10：后端旧版会返回**空对象 {}**，而 JS 里 {} 是 truthy ——
        //    直接 `(x || null)` 会让横幅在"任务正常在跑"时也渲染（文案退化成
        //    「第集生产失败」）。这里改为「必须是含字段的非空对象」才算失败。
        const _fp = st.failure_pause as any;
        setFailurePause(
          _fp && typeof _fp === 'object' && Object.keys(_fp).length > 0 ? _fp : null,
        );
      } catch {
        // 轮询失败静默：状态刷新是锦上添花，不能因一次失败打断整页
      }
    };
    poll();
    timer = setInterval(poll, 3000);
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [projectKey]);

  // 托管 启动/暂停 控制（2026-10-08：API 早已具备，此前无 UI 入口，用户只能干等）
  const togglePause = async () => {
    if (ctrlBusy) return;
    setCtrlBusy(true);
    try {
      if (paused) await autopilotApi.resume();
      else await autopilotApi.pause();
      const st = await autopilotApi.status(projectKey);
      setPaused(Boolean(st.paused));
    } catch (e) {
      // ⭐ 2026-10-19：控制失败**不再静默** —— 此前 400/415 被 catch 吞掉，
      //    按钮「点了没反应」，用户完全不知道发生了什么。
      toast.error(describeError(e, '托管控制失败').message || '托管控制失败');
    } finally {
      setCtrlBusy(false);
    }
  };

  // ComfyUI 采样级实时进度：有生产任务时才轮询 comfyui 日志源（tqdm N/M）
  const comfy = useComfyProgress(!!cur);

  // ⚠️ 修复 t39 N2：失败挂起时后端 current 会被 _clear_current() 清空（cur=null），
  // 但 failure_pause 非空 —— 若仍「!cur 就 return null」，失败挂起横幅（含「处理并继续」）
  // 永远渲染不出来。改为：无进度且无失败挂起才整体隐藏；有失败挂起则保留横幅 + 恢复入口。
  if (!cur && !failurePause) return null;

  const percent = cur ? Math.max(0, Math.min(100, Number(cur.percent) || 0)) : 0;
  const stepsDone = cur ? (Array.isArray(cur.steps_done) ? cur.steps_done : []) : [];
  const stalled = cur ? (Number(cur.step_stalled_sec) || 0) : 0;
  const stalledMin = Math.floor(stalled / 60);
  const stalledSec = stalled % 60;
  // step 可能是环节内子阶段（如 outline 提炼大纲属 script 步骤）——子阶段
  // 不在步骤表里，此时不点亮任何环节（百分比与描述行仍准确），避免步骤链错位
  const _rawStep = (cur?.step || '').split(':')[0];
  const currentStepId = PRODUCTION_STEPS.some(s => s.id === _rawStep) ? _rawStep : null;
  const currentStep = PRODUCTION_STEPS.find(s => s.id === currentStepId);

  return (
    <div
      className="bg-surface rounded-lg border border-line p-4 mb-4"
      role="status"
      aria-live="polite"
      aria-label={t('wb.productionProgress')}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold text-ink-1 flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-info animate-pulse" />
          {t('wb.productionProgress')}
        </span>
        <div className="flex items-center gap-2">
          {cur && cur.episode != null && (
            <span className="text-sm text-ink-2">{t('wb.producingEpisode', { n: cur.episode })}</span>
          )}
          {/* 托管 启动/暂停 控制（2026-10-08：API 早已具备，此前无 UI 入口） */}
          <button
            type="button"
            onClick={togglePause}
            disabled={ctrlBusy}
            className={`
              inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium border transition-colors 
              ${paused ? 'border-success text-success hover:bg-success-subtle' : 'border-warning text-warning hover:bg-warning-subtle'}
              ${ctrlBusy ? 'opacity-50 cursor-wait' : ''}
            `}
          >
            {ctrlBusy ? (
              <span className="h-3 w-3 rounded-full border-2 border-current border-t-transparent animate-spin" />
            ) : paused ? (
              <Play className="h-3 w-3" />
            ) : (
              <Pause className="h-3 w-3" />
            )}
            {paused ? t('wb.resumeAutopilot') : t('wb.pauseAutopilot')}
          </button>
        </div>
      </div>

      {/* ⚠️ 2026-10-08：单集失败自动暂停（stop_on_failure）醒目横幅 */}
      {failurePause && (
        <div className="flex items-start gap-2.5 mt-3 p-3 rounded-lg bg-danger-subtle border border-danger text-danger-strong text-xs">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <div className="flex-1">
              <p className="font-semibold text-sm">{sanitizeErrorMessage(failurePause.reason || t('wb.failurePauseTitle', { n: failurePause.episode ?? '' }))}</p>
            {failurePause.error && (
              <p className="mt-1 break-all text-danger/80">{sanitizeErrorMessage(failurePause.error)}</p>
            )}
              <p className="mt-1.5 text-danger/70">{t('wb.failurePauseHint', { n: failurePause.episode ?? '' })}</p>
          </div>
          <button
            type="button"
            onClick={togglePause}
            disabled={ctrlBusy}
            className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium bg-danger text-white hover:opacity-90 transition-opacity disabled:cursor-wait disabled:opacity-50"
          >
            {ctrlBusy ? (
              <span className="h-3 w-3 rounded-full border-2 border-current border-t-transparent animate-spin" />
            ) : (
              <Play className="h-3 w-3" />
            )}
            {t('wb.failureResume')}
          </button>
        </div>
      )}

      {/* ⚠️ 2026-10-09 合并：进度条与步骤链已**移交** LiveExecutionFeed ——
       *  同一件事不再两套实现。本组件只保留它独有的三样：
       *    ① 单集失败自动暂停横幅（stop_on_failure）
       *    ② ComfyUI 采样级进度（解析 comfyui 日志 tqdm）
       *    ③ 步骤超时告警（stalled >= 900）
       *  另保留托管 暂停/恢复 按钮（LiveExecutionFeed 不管控制面）。 */}



      {/* 当前步骤消息 + 超时告警 */}
      {cur && cur.message && <p className="text-xs text-ink-2 mb-1">{cur.message}</p>}

      {/* ComfyUI 采样进度（如「比例分镜 10/19」）：解析 comfyui 日志的 tqdm 行 */}
      {cur && comfy.active && comfy.total > 0 && (
        <div className="mt-2 flex items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1.5 text-xs">
          <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent" />
          <span className="shrink-0 text-ink-2">{t('live.comfySampling')}</span>
          <span className="shrink-0 font-medium tabular-nums text-ink-1">
            {comfy.current}/{comfy.total}
          </span>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-line">
            <div className="progress-fill h-full rounded-full transition-all duration-300" style={{ width: `${comfy.percent}%` }} />
          </div>
          <span className="shrink-0 tabular-nums text-ink-2">{comfy.percent}%</span>
        </div>
      )}
      {cur && stalled >= 900 && (
        <div className="flex items-start gap-2 mt-2 p-2.5 rounded bg-warning-subtle text-warning-strong text-xs">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">
              {t('wb.currentStep')}
              {currentStep ? `：${t(currentStep.labelKey)}` : ''} ·{' '}
              {t('wb.stepStalled', { min: stalledMin, sec: stalledSec })}
            </p>
            <p className="text-ink-2 mt-0.5">{t('wb.stallHint')}</p>
          </div>
        </div>
      )}
    </div>
  );
}