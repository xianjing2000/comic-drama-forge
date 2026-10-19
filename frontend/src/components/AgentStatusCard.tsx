/** AgentStatusCard —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { useProductionStatus } from '@/pages/workbenchShared';
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
import { StartProductionCard } from '@/components/StartProductionCard';
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

export function AgentStatusCard({ projectKey }: { projectKey: string }) {
  const current = useProductionStatus(projectKey);
  const comfy = useComfyProgress(!!current);

  if (!current && !comfy.active) return null;

  const pct = current ? Math.max(0, Math.min(100, Number(current.percent) || 0)) : 0;
  const stalled = current ? Number(current.step_stalled_sec) || 0 : 0;
  const stallMin = Math.floor(stalled / 60);

  return (
    <div
      className="glass glow-brand mx-3 mt-3 shrink-0 space-y-2.5 rounded-lg border border-line p-3"
      role="status"
      aria-live="polite"
      aria-label={t('wb.productionProgress')}
    >
      {current && (
        <>
          <div className="flex items-center gap-2 text-xs">
            <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-brand" />
            <span className="shrink-0 text-ink-2">{t('chat.producingNow')}</span>
            <span className="truncate font-medium text-ink-1">
              {current.describe || current.message || ''}
            </span>
            <span className="ml-auto shrink-0 tabular-nums text-ink-2">{pct}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
            <div
              className="progress-fill h-full rounded-full transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          {stallMin >= 3 && (
            <div className="text-xs text-warning-strong">
              {t('chat.producingStalled', { m: stallMin })}
            </div>
          )}
        </>
      )}
      {comfy.active && comfy.total > 0 && (
        <div className="flex items-center gap-2 text-xs">
          <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" />
          <span className="shrink-0 text-ink-2">{t('live.comfySampling')}</span>
          <span className="shrink-0 font-medium tabular-nums text-ink-1">
            {comfy.current}/{comfy.total}
          </span>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface-2">
            <div
              className="progress-fill h-full rounded-full transition-all duration-300"
              style={{ width: `${comfy.percent}%` }}
            />
          </div>
          <span className="shrink-0 tabular-nums text-ink-2">{comfy.percent}%</span>
        </div>
      )}
    </div>
  );
}