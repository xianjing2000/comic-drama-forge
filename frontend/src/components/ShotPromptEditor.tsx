/** ShotPromptEditor —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
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

export function ShotPromptEditor({ shot, novelId, episodeNo, onSaved }: {
  shot: any; novelId?: string; episodeNo: number; onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [desc, setDesc] = useState(shot.description || '');
  const [motion, setMotion] = useState(shot.motion || '');
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const toast = useToast();

  // G10b（2026-09-30）：质检改写的提示词**实时渲染** —— 出图/重试循环每改写一次 prompt，
  // 后端任务状态里的 live 字段就更新一次。
  // ⭐ E-1（2026-10-16）：SSE 可用时改由事件流推送（任务出现→更新 live；消失→结束），
  // 并保留一条兜底轮询覆盖「极短任务/流异常」；SSE 不可用时逐字回退原 3s 轮询。
  const [live, setLive] = useState<any>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const liveUnsubRef = useRef<(() => void) | null>(null);
  const liveDoneRef = useRef(false);
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; stopPolling(); }, []);

  const stopPolling = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (liveUnsubRef.current) { try { liveUnsubRef.current(); } catch { /* ignore */ } liveUnsubRef.current = null; }
  };

  const finishLive = () => {
    if (liveDoneRef.current) return;
    liveDoneRef.current = true;
    setLive((prev: any) => (prev && prev.shot === shot.shot_id && !prev.done ? { ...prev, done: true } : prev));
    stopPolling();
    onSaved();
  };

  const pollTask = (taskId: string) => {
    stopPolling();
    liveDoneRef.current = false;
    const applyLive = (d: any) => {
      const lv = d?.live;
      if (lv && lv.shot === shot.shot_id && lv.prompt) setLive(lv);
    };
    if (isConnected()) {
      liveUnsubRef.current = subscribeEvents((snap: any) => {
        if (!mountedRef.current) { stopPolling(); return; }
        const t = snap?.tasks?.find((x: any) => x.task_id === taskId);
        if (t) { applyLive(t); return; }
        finishLive();   // 曾出现、现已消失 → 结束
      });
      // 兜底：SSE 尚未给出该任务时用轮询；任务一出现在流里即不再轮询。
      const fallback = setInterval(async () => {
        if (!mountedRef.current) { stopPolling(); return; }
        if (getSnapshot()?.tasks?.some((x: any) => x.task_id === taskId)) return;
        try {
          const r = await fetch(`/api/generation/status/${encodeURIComponent(taskId)}`);
          const d: any = await r.json();
          applyLive(d);
          if (d?.status && d.status !== 'running') finishLive();
        } catch { /* ignore */ }
      }, 3000);
      pollRef.current = fallback;
      return;
    }
    // 回退：原 3s 轮询（逐字保留）
    const tick = async () => {
      if (!mountedRef.current) return stopPolling();
      try {
        const r = await fetch(`/api/generation/status/${encodeURIComponent(taskId)}`);
        const d: any = await r.json();
        if (!mountedRef.current) return stopPolling();
        applyLive(d);
        if (d?.status && d.status !== 'running') finishLive();
      } catch { /* 网络抖动：继续下一轮 */ }
    };
    tick();
    pollRef.current = setInterval(tick, 3000);
  };

  const save = async () => {
    setSaving(true);
    try {
      const r = await fetch(
        `/api/episodes/${encodeURIComponent(novelId || '')}/${episodeNo}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ shots: [{ shot_id: shot.shot_id, description: desc.trim() || null, motion: motion.trim() || null }] }),
        }
      );
      const d = await r.json();
      if (!d.success) throw new Error(d.error || t('wb.promptSaveFailed'));
      toast.success(t('wb.promptSaved', { n: shot.shot_id }));
      onSaved();
    } catch (e: any) {
      toast.error(e.message || t('wb.promptSaveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const regenerate = async () => {
    setRegenerating(true);
    try {
      const r = await fetch('/api/storyboards/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_name: novelId || '',
          shots: [{ shot_id: shot.shot_id }],
          limit: 1,
        }),
      });
      const d = await r.json();
      if (d.task_id) {
        setLive(null);
        pollTask(d.task_id);
        toast.info(t('wb.shotRegenStarted', { n: shot.shot_id }));
      } else {
        throw new Error(d.error || t('wb.regenStartFailed'));
      }
    } catch (e: any) {
      toast.error(e.message || t('wb.regenFailed'));
    } finally {
      setRegenerating(false);
    }
  };

  // G10b：live 面板 —— 正在跑的出图任务实时暴露「当前提示词」（含质检改写后的版本）
  const livePanel = live && live.shot === shot.shot_id ? (
    <div className={`mt-2 rounded border p-2 ${live.done ? 'border-success/40' : 'border-brand/40'}`}>
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-medium ${live.done ? 'text-success' : 'text-brand'}`}>
          {live.done
            ? t('wb.promptFinal')
            : t('wb.promptLiveRewrite', { n: (live.attempt ?? 0) + 1, phase: live.phase === 'regenerating' ? t('wb.phaseRegenerating') : live.phase === 'checking' ? t('wb.phaseChecking') : t('wb.phaseGenerating') })}
        </span>
        {!live.done && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />}
      </div>
      <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-4 text-ink-2">{live.prompt}</pre>
    </div>
  ) : null;

  if (!editing) {
    return (
      <div className="mt-1 space-y-1">
        <div className="flex items-start gap-2">
          <p className="text-sm text-ink-1 flex-1">{shot.description}</p>
          <button
            className="text-xs text-brand hover:underline shrink-0 cursor-pointer"
            onClick={() => { setDesc(shot.description || ''); setMotion(shot.motion || ''); setEditing(true); }}
          >
            {t('common.editBtn')}
          </button>
        </div>
        {shot.motion && <p className="text-xs text-ink-3 ml-2">{shot.motion}</p>}
        {livePanel}
      </div>
    );
  }

  return (
    <div className="mt-2 bg-surface rounded border border-line p-3 space-y-2">
      <label className="text-xs font-medium text-ink-2 block">{t('wb.shotDescLabel')}</label>
      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        rows={2}
        className="w-full text-sm bg-surface border border-line rounded p-2 text-ink-1 resize-y"
        placeholder={t('wb.shotDescPlaceholder')}
      />
      <label className="text-xs font-medium text-ink-2 block">{t('wb.motionLabel')}</label>
      <textarea
        value={motion}
        onChange={(e) => setMotion(e.target.value)}
        rows={2}
        className="w-full text-sm bg-surface border border-line rounded p-2 text-ink-1 resize-y"
        placeholder={t('wb.motionPlaceholder')}
      />
      {livePanel}
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={save} disabled={saving || !desc.trim()}>
          {saving ? t('common.saving') : t('wb.promptSave')}
        </Button>
        <Button size="sm" variant="secondary" onClick={regenerate} disabled={regenerating || !desc.trim()}>
          {regenerating ? t('common.generating') : t('wb.regenShot')}
        </Button>
        <button className="text-xs text-ink-3 hover:text-ink-1 cursor-pointer" onClick={() => setEditing(false)}>{t('common.cancel')}</button>
      </div>
    </div>
  );
}