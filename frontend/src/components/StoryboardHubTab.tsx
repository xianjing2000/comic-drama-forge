/** StoryboardHubTab —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { useEpisodeList } from '@/pages/workbenchShared';
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
import { EpisodeSwitcher } from '@/components/EpisodeSwitcher';
import { ShotPromptEditor } from '@/components/ShotPromptEditor';
import { StoryboardTab } from '@/components/StoryboardTab';
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

export function StoryboardHubTab({ projectKey, novelId, onGotoOutput,
  focusShot, focusEpisodeNo, onFocusConsumed }: {
  projectKey: string;
  novelId?: string;
  onGotoOutput?: () => void;
  /** t9：音频体检面板带过来的镜号（要滚动+高亮的那一镜），消费后由 onFocusConsumed 清空 */
  focusShot?: string | null;
  /** t9：该镜所属集（跨集跳转时先切集再滚动，避免落到同号不同集的卡上） */
  focusEpisodeNo?: number | null;
  onFocusConsumed?: () => void;
}) {
  const { t } = useApp();
  // 2026-10-06：子标签收敛——九宫格草案（后端已删）与「关键帧」尾帧入口都下线。
  // 尾帧已由 H3 导演台在视频生成时逐镜产出（keyframe 步骤 2026-10-05 移出 7 步流水线），
  // 前端不再有独立的「关键帧」生成页；本标签只保留分镜序列（StoryboardTab）。
  // 选中集号：null = 未指定（沿用后端「最新一集」的兜底，兼容无剧集数据的纯项目）
  const [selectedEpisode, setSelectedEpisode] = useState<number | null>(null);
  const episodes = useEpisodeList(novelId);

  // 剧集列表到位后默认落到第 1 集：必须显式传集号，否则后端会漂到「最新一集」，
  // 与用户在概览页看到的选集不一致（同一部小说不同页显示不同集）。
  useEffect(() => {
    if (episodes.length > 0 && selectedEpisode === null) {
      setSelectedEpisode(episodes[0].episode_no);
    }
    // 选集被删（重跑时清过产物）时回落到第一个可用集
    if (episodes.length > 0 && selectedEpisode !== null
        && !episodes.some((e) => e.episode_no === selectedEpisode)) {
      setSelectedEpisode(episodes[0].episode_no);
    }
  }, [episodes, selectedEpisode]);

  // t9：体检面板点「定位分镜」时，若目标镜在别的集，先切到那一集再交给 StoryboardTab 滚动
  useEffect(() => {
    if (focusEpisodeNo == null) return;
    if (!episodes.some((e) => e.episode_no === focusEpisodeNo)) return;
    setSelectedEpisode((prev) => (prev === focusEpisodeNo ? prev : focusEpisodeNo));
  }, [focusEpisodeNo, episodes]);

  return (
    <div className="space-y-5">
      {/* 集切换器：分镜产物按集隔离，必须先在集之间分流 */}
      <EpisodeSwitcher
        episodes={episodes}
        value={selectedEpisode}
        onChange={setSelectedEpisode}
      />

      <StoryboardTab
        projectKey={projectKey}
        episodeNo={selectedEpisode}
        onGotoOutput={onGotoOutput}
        focusShot={focusShot}
        focusEpisodeNo={focusEpisodeNo}
        onFocusConsumed={onFocusConsumed}
      />
    </div>
  );
}