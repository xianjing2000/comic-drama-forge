/** EpisodeSwitcher —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { FOCUS_RING } from '@/components/ui/focusRing';
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
import { ProductionProgress } from '@/components/ProductionProgress';
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

export function EpisodeSwitcher({
  episodes,
  value,
  onChange,
}: {
  episodes: any[];
  value: number | null;
  onChange: (ep: number) => void;
}) {
  const { t } = useApp();
  if (episodes.length === 0) return null;
  const cur = episodes.find((e) => e.episode_no === value);
  return (
    <div className="flex flex-wrap items-center gap-2 bg-surface-2 rounded-lg px-3 py-2">
      <span className="text-sm text-ink-2 shrink-0">{t('sb.episode')}</span>
      <div className="flex flex-wrap gap-1.5">
        {episodes.map((e) => {
          const active = e.episode_no === value;
          const shots = e.shots ?? e.shot_count ?? 0;
          return (
            <button
              key={e.episode_no}
              onClick={() => onChange(e.episode_no)}
              title={`${e.episode_title || e.title || ''}${e.chapter_index ? ` · ${t('sb.chapterN', { n: e.chapter_index })}` : ''}`}
              className={`px-2.5 py-1 rounded-md text-xs transition-all ${FOCUS_RING} ${
                active ? 'bg-brand text-white shadow' : 'bg-surface text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
              }`}
            >
              {t('sb.episodeN', { n: e.episode_no })}
              <span className={active ? ' text-white/80' : ' text-ink-3'}>{` · ${shots}`}</span>
            </button>
          );
        })}
      </div>
      {cur && (
        <span className="text-xs text-ink-3 ml-auto truncate max-w-[16rem]">
          {cur.episode_title || cur.title || ''}
        </span>
      )}
    </div>
  );
}