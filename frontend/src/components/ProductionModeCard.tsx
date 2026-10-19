/** ProductionModeCard —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
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

export function ProductionModeCard({ projectKey }: { projectKey: string }) {
  const [mode, setMode] = useState<'autopilot' | 'autonomous'>('autopilot');
  const seg = (active: boolean) =>
    `px-3 py-1 text-xs rounded transition-colors ${active
       ? 'bg-brand text-white font-medium'
       : 'text-ink-2 hover:bg-surface-2'}`;
  return (
    <div className="mb-4">
      <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface px-3 py-2">
        <span className="text-sm font-semibold text-ink-1">{t('wb.productionMode')}</span>
        <div className="inline-flex rounded-md border border-line p-0.5">
          <button type="button" className={seg(mode === 'autopilot')} onClick={() => setMode('autopilot')}>
            {t('wb.modeAutopilot')}
          </button>
          <button type="button" className={seg(mode === 'autonomous')} onClick={() => setMode('autonomous')}>
            {t('wb.modeAutonomous')}
          </button>
        </div>
        <span className="min-w-0 flex-1 text-xs text-ink-3">
          {mode === 'autopilot'
            ? t('wb.modeAutopilotDesc')
            : t('wb.modeAutonomousDesc')}
        </span>
      </div>
      {mode === 'autopilot'
        ? <ProductionProgress projectKey={projectKey} />
        : <AutonomousPanel project={projectKey} />}
    </div>
  );
}