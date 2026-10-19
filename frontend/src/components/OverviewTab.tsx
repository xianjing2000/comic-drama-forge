/** OverviewTab —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { EpisodeListPanel } from '@/components/EpisodeListPanel';
import { AssetsGallery } from '@/components/AssetsGallery';
import { ChapterPreflightCard } from '@/components/ChapterPreflightCard';
import { SplitPlanCard } from '@/components/SplitPlanCard';
import { type ProjectAssets } from '@/pages/workbenchShared';
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
import { EpisodeSwitcher } from '@/components/EpisodeSwitcher';
import { ShotPromptEditor } from '@/components/ShotPromptEditor';
import { StoryboardTab } from '@/components/StoryboardTab';
import { StoryboardHubTab } from '@/components/StoryboardHubTab';
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

export function OverviewTab({
  assets,
  projectKey,
  novelId,
  onRefreshAssets,
}: {
  assets: ProjectAssets | null;
  projectKey: string;
  novelId?: string;
  onRefreshAssets: () => Promise<void> | void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [preview, setPreview] = useState<{ item: AssetItem; type: 'character' | 'item' | 'scene' } | null>(null);
  // 上传形象图 → 三视图（零 GPU 本地切分）
  const [uploadOpen, setUploadOpen] = useState(false);
  // 可上传的角色名：优先取已有角色资产（用户大概率是给已抽出的角色换图），
  // 没有资产时也给个空列表让用户手填 —— 支持「先上传形象图再跑剧本」的用法。
  const uploadCharacters = React.useMemo(
    () => (assets?.gallery?.characters || []).map((c) => c.name).filter(Boolean),
    [assets]
  );

  // 剧本相关状态
  const [episodes, setEpisodes] = useState<any[]>([]);
  const [totalEpisodes, setTotalEpisodes] = useState(0);
  const [scriptLoading, setScriptLoading] = useState(true);
  const [scriptError, setScriptError] = useState('');
  const [selectedEpisode, setSelectedEpisode] = useState<number | null>(null);
  const [episodeDetail, setEpisodeDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  /** 详情加载失败的 HTTP 状态码（404 = 剧本尚未生成；0 = 非 HTTP 失败/成功清空） */
  const [detailStatus, setDetailStatus] = useState(0);
  /** 记下最近一次请求的集号：详情加载失败后 ErrorState 的「重试」要能定位回同一集 */
  const lastDetailEpRef = React.useRef<number | null>(null);

  // P2-2 分集断点提议（与生产口径同源的只读预览，不触发任何生成）
  const [splitPlanOpen, setSplitPlanOpen] = useState(false);
  const [splitPlan, setSplitPlan] = useState<any | null>(null);
  const [splitPlanLoading, setSplitPlanLoading] = useState(false);
  const [splitPlanError, setSplitPlanError] = useState('');
  // 前置解析（chapter pre-flight）：人物档案 + 故事梗概 + 关键事件 + 情绪基线
  const [preflightDone, setPreflightDone] = useState<number[]>([]);
  const [preflightRunning, setPreflightRunning] = useState<number | null>(null);
  const [preflightDetail, setPreflightDetail] = useState<ChapterPreflightResult | null>(null);
  const [preflightOpen, setPreflightOpen] = useState(false);

  // --- 按场次生成（2026-10-03）：集行可展开「第1场/第2场/…」场次层级 ---
  // 视频生产已改为按场次生成：每场一个 scene_XX.mp4，全部完成后拼成 epNN_full.mp4。
  // 展开时拉 GET /api/episode/scenes（每次展开都重新拉：分镜/视频状态随生产推进变化）。
  const [scenesOpenEps, setScenesOpenEps] = useState<number[]>([]);
  const [scenesByEp, setScenesByEp] = useState<Record<number, EpisodeScenesResponse | null>>({});
  const [scenesLoadingEp, setScenesLoadingEp] = useState<number | null>(null);
  const [scenesErrByEp, setScenesErrByEp] = useState<Record<number, string>>({});
  /** 「重做本场」进行中的键（"集:场"）；非空时所有重做按钮禁用，防止并发占满 ComfyUI */
  const [redoBusy, setRedoBusy] = useState<string | null>(null);
  const redoReqRef = useRef(0);

  const runPreflight = async (chIdx: number) => {
    if (!novelId) return;
    setPreflightRunning(chIdx);
    try {
      const d = await preflightApi.analyze(novelId, chIdx);
      if (d.success && d.result) {
        setPreflightDetail(d.result);
        setPreflightDone(prev => [...new Set([...prev, chIdx])].sort((a, b) => a - b));
        toast.success(t('wb.preflightDone', { n: chIdx, chars: d.result.characters?.length ?? 0 }));
      } else {
        toast.error(t('wb.preflightFailed', { err: (d as any).error ?? '' }));
      }
    } catch (err: any) {
      toast.error(t('wb.preflightFailed', { err: err?.message ?? '' }));
    } finally {
      setPreflightRunning(null);
    }
  };

  const openPreflightDetail = async (chIdx: number) => {
    if (!novelId) return;
    const d = await preflightApi.get(novelId, chIdx);
    if (d.exists && d.result) {
      setPreflightDetail(d.result);
      setPreflightOpen(true);
    }
  };

  // 标签切换会卸载本组件：卸载后让任务轮询让位，不再 setState
  const spAliveRef = useRef(true);
  useEffect(() => {
    spAliveRef.current = true;
    return () => { spAliveRef.current = false; };
  }, []);

  /** 轮询 generation 任务至终态（15 分钟兜底）。
   *  ⚠️ /api/generation/status 历史上有 {success, task:{…}} 信封与顶层平铺两种形态，
   *  按 `task ?? 顶层` 兼容读取（与九宫格候选轮询同口径）。
   *  ⭐ E-1（2026-10-16）：SSE 可用时改**事件驱动**（任务出现→消失即做一次确认查询拿终态），
   *  否则**逐字回退**原 3s 轮询 —— 终态判据始终来自同一 `/api/generation/status`，语义不变。 */
  /** failMsg/timeoutMsg：场次重做等非剧本场景传入各自的错误文案（缺省回落剧本文案） */
  const pollGenerationTask = async (taskId: string, isCurrent: () => boolean, failMsg?: string, timeoutMsg?: string) => {
    const deadline = Date.now() + 15 * 60 * 1000;
    const readTask = async (): Promise<any> => {
      const d: any = await generationApi.status(taskId);
      return ((d?.task ?? d) || {}) as any;
    };
    const confirm = async () => {
      const task = await readTask();
      if (task.status === 'completed') return;
      if (task.status === 'failed' || task.status === 'cancelled') {
        throw new Error(task.error || failMsg || t('wb.actionFailed'));
      }
      throw new Error(failMsg || t('wb.actionFailed'));
    };
    if (!isConnected()) {
      for (;;) {
        await new Promise(r => setTimeout(r, 3000));
        if (!spAliveRef.current || !isCurrent()) throw new Error(failMsg || t('wb.actionFailed'));
        const task = await readTask();
        if (task.status === 'completed') return;
        if (task.status === 'failed' || task.status === 'cancelled') {
          throw new Error(task.error || failMsg || t('wb.actionFailed'));
        }
        if (Date.now() > deadline) throw new Error(timeoutMsg || t('wb.actionFailed'));
      }
    }
    await new Promise<void>((resolve, reject) => {
      let seen = false;
      let done = false;
      let unsub: () => void = () => {};
      let timer: ReturnType<typeof setInterval> | null = null;
      const close = () => { done = true; try { unsub(); } catch { /* ignore */ } if (timer) clearInterval(timer); };
      const check = (snap: any) => {
        if (done) return;
        if (!spAliveRef.current || !isCurrent()) { close(); reject(new Error(failMsg || t('wb.actionFailed'))); return; }
        const present = !!snap?.tasks?.some((x: any) => x.task_id === taskId);
        if (present) { seen = true; return; }
        if (seen) { close(); confirm().then(resolve, reject); }
      };
      unsub = subscribeEvents(check);
      if (done) unsub();
      timer = setInterval(() => {
        if (done) return;
        if (Date.now() > deadline) { close(); reject(new Error(timeoutMsg || t('wb.actionFailed'))); return; }
        if (!seen) { close(); confirm().then(resolve, reject); }  // 极短任务可能在两次事件之间跑完
      }, 3000);
      check(getSnapshot());
    });
  };

  // 加载剧集列表
  // 抽成具名函数：错误态需要「重试」入口，而 useEffect 无法被手动重新触发。
  // 取数逻辑与原实现逐字一致，仅补一次错误清理，避免重试成功后旧的失败文案残留。
  // silent=true：后台轮询用 —— 不置 loading（否则每 10 秒闪一次转圈）、失败也不弹错。
  const fetchEpisodes = React.useCallback((silent = false) => {
    if (!novelId) return;
    if (!silent) {
      setScriptLoading(true);
      setScriptError('');
    }
    episodesApi.list(novelId)
      .then(data => {
        setEpisodes(data.episodes || []);
        setTotalEpisodes(data.total || 0);
      })
      .catch(err => {
        if (!silent) setScriptError(err instanceof Error ? err.message : t('project.loadingFailed'));
      })
      .finally(() => {
        if (!silent) setScriptLoading(false);
      });
  }, [novelId, t]);

  useEffect(() => { fetchEpisodes(); }, [fetchEpisodes]);

  // t4：「开始生产」的可选集号 —— 与剧集列表同源；useMemo 保证引用稳定，
  // 否则每次渲染都会给 StartProductionCard 一个新数组、触发其内部 effect 抖动。
  const startEpisodes = React.useMemo(
    () => (episodes || [])
      .map((ep: any) => Number(ep?.episode_no))
      .filter((n: number) => Number.isFinite(n) && n > 0)
      .sort((a: number, b: number) => a - b),
    [episodes],
  );

  // ⭐ 2026-10-10（用户实测：「剧本生产完成后不实时加载出来」）：
  //    剧本是 autopilot 在**后台**异步生成的 —— 页面挂载时往往还没产出，而
  //    fetchEpisodes 的依赖只有 novelId（剧本生成不改变它），所以「暂无剧集数据」
  //    会一直挂着，直到用户手动切走再切回。
  //    这里在「面板为空 + 有小说」时每 10 秒静默重拉，拿到数据即自动停；
  //    首次重拉延迟 8 秒，避免与挂载时那次请求贴太近。
  useEffect(() => {
    if (!novelId || episodes.length > 0) return;
    const timer = setInterval(() => { fetchEpisodes(true); }, 10000);
    return () => clearInterval(timer);
  }, [novelId, episodes.length, fetchEpisodes]);

  // 拉取分集断点提议：默认不带参数，与生产 autopilot.episode_units 完全同源，
  // 保证「提议 ≡ 实际生成」，不会提议说 1 集、真生成拆 3 集。
  const fetchSplitPlan = React.useCallback(() => {
    if (!novelId) return;
    setSplitPlanLoading(true);
    setSplitPlanError('');
    novelsSplitPlanApi.get(novelId)
      .then(data => {
        setSplitPlan(data);
        setSplitPlanOpen(true);
      })
      .catch(err => {
        setSplitPlan(null);
        setSplitPlanError(err instanceof Error ? err.message : t('project.loadingFailed'));
        setSplitPlanOpen(true);
      })
      .finally(() => setSplitPlanLoading(false));
  }, [novelId, t]);

  // --- 场次层级（按场次生成）：展开拉取 + 重做本场 ---
  const fetchScenes = React.useCallback(async (episodeNo: number, silent = false) => {
    if (!projectKey) return;
    if (!silent) setScenesLoadingEp(episodeNo);
    try {
      const d = await episodesApi.scenes(projectKey, episodeNo);
      setScenesByEp(prev => ({ ...prev, [episodeNo]: d }));
      setScenesErrByEp(prev => { const next = { ...prev }; delete next[episodeNo]; return next; });
    } catch (err) {
      // 静默刷新失败时保留旧数据（展开着的面板不该因一次轮询失败闪成报错）
      if (!silent) {
        setScenesErrByEp(prev => ({ ...prev, [episodeNo]: err instanceof Error ? err.message : t('wb.scenesLoadFailed') }));
      }
    } finally {
      if (!silent) setScenesLoadingEp(null);
    }
  }, [projectKey, t]);

  const toggleScenes = (episodeNo: number) => {
    const willOpen = !scenesOpenEps.includes(episodeNo);
    setScenesOpenEps(prev => (willOpen ? [...prev, episodeNo] : prev.filter(n => n !== episodeNo)));
    if (willOpen) void fetchScenes(episodeNo);
  };

  /** 「重做本场」：只重生成这一场（POST /videos/generate only_scenes=[X] overwrite=true），
   *  轮询 generation/status 至终态后静默刷新场次状态。直接执行不加确认 ——
   *  与资产「重新生成」同款项目习惯（误重做可再重做一次，非不可逆操作）。 */
  const redoScene = async (episodeNo: number, sceneNo: number) => {
    if (redoBusy) return;
    const key = `${episodeNo}:${sceneNo}`;
    const token = ++redoReqRef.current;
    const isCurrent = () => spAliveRef.current && token === redoReqRef.current;
    setRedoBusy(key);
    try {
      const r = await videoApi.generateEpisode({
        project_name: projectKey,
        episode_no: episodeNo,
        only_scenes: [sceneNo],
        overwrite: true,
      });
      if (r?.task_id) {
        await pollGenerationTask(String(r.task_id), isCurrent, t('wb.sceneRedoFailed'), t('wb.sceneRedoTimeout'));
      }
      if (isCurrent()) toast.success(t('wb.sceneRedoDone', { n: sceneNo }));
    } catch (err) {
      if (isCurrent()) toast.error(err instanceof Error ? err.message : t('wb.sceneRedoFailed'));
    } finally {
      // 与场次重做同款：组件已卸载（切标签页）就不再 setState / 刷新
      if (isCurrent()) {
        setRedoBusy(null);
        void fetchScenes(episodeNo, true);
      }
    }
  };

  // 集行「整集已拼接」徽标：列表加载完后对每集静默拉一次场次状态，
  // 未展开也能看到哪些集已拼出整集成片。只读、失败静默（徽标是锦上添花）。
  useEffect(() => {
    if (scriptLoading || episodes.length === 0 || !projectKey) return;
    let alive = true;
    Promise.all(
      episodes.map((ep: any) =>
        episodesApi.scenes(projectKey, ep.episode_no)
          .then(d => ({ no: ep.episode_no as number, d }))
          .catch(() => null)
      )
    ).then(results => {
      if (!alive) return;
      const ok = results.filter(Boolean) as { no: number; d: EpisodeScenesResponse }[];
      if (ok.length === 0) return;
      setScenesByEp(prev => {
        const next = { ...prev };
        for (const { no, d } of ok) next[no] = d;
        return next;
      });
    });
    return () => { alive = false; };
  }, [scriptLoading, episodes, projectKey]);

  // 加载单集详情
  // ⚠️ 后端 /api/episodes/<novel>/<ep> 的剧本正文嵌在 `script` 对象下（shots/characters/items/scenes），
  // 且列表行才带 status/completed_shots（_episode_progress 推导），详情接口本身不返回这两个字段。
  // 这里摊平成视图直接可读的结构，并从已加载的剧集列表补进度字段，避免详情恒显「暂无剧本内容」。
  const loadEpisodeDetail = async (episodeNo: number) => {
    if (!novelId) return;
    lastDetailEpRef.current = episodeNo;
    setDetailLoading(true);
    setDetailError('');
    setDetailStatus(0);
    try {
      const detail = await episodesApi.get(novelId, episodeNo);
      const script = (detail as any).script || {};
      const shots: any[] = script.shots || [];
      // 从剧集列表找本行进度（status / completed_shots / created_at）
      const row = episodes.find((e: any) => e.episode_no === episodeNo);
      const normalized: any = {
        ...detail,
        ...script,
        episode_no: detail.episode_no ?? episodeNo,
        title: detail.episode_title || detail.project_name || script.title,
        chapter_title: detail.episode_title,
        shots,
        shot_count: (detail as any).stats?.shot_count || script.shot_count || shots.length,
        status: row?.status ?? (detail as any).status ?? 'pending',
        completed_shots: row?.completed_shots ?? (detail as any).completed_shots ?? 0,
        created_at: row?.generated_at ?? (detail as any).created_at,
      };
      setEpisodeDetail(normalized);
      setSelectedEpisode(episodeNo);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : t('wb.loadDetailFailed'));
      // 结构化判定「尚未生成」：后端 EpisodeNotFoundError → HTTP 404，不再匹配中文文案
      const st = (err as { status?: unknown } | null)?.status;
      setDetailStatus(typeof st === 'number' ? st : 0);
    } finally {
      setDetailLoading(false);
    }
  };

  // 返回列表
  const goBack = () => {
    setSelectedEpisode(null);
    setEpisodeDetail(null);
    setDetailError('');
    setDetailStatus(0);
  };


  // 显示单集详情
  // 资产总数（E-5：原由 groups 派生，groups 已移入 AssetsGallery，这里直接按三类求和）
  const total = (assets?.gallery?.characters?.length || 0)
    + (assets?.gallery?.items?.length || 0)
    + (assets?.gallery?.scenes?.length || 0);

  if (selectedEpisode !== null && episodeDetail && !detailError) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" onClick={goBack}>
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          {t('wb.backToList')}
        </Button>

        <div className="bg-surface rounded-lg border border-line p-6">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="text-xl font-bold text-ink-1">
                {t('wb.episodeNo', { n: episodeDetail.episode_no })}
                {episodeDetail.title && <span className="ml-2 text-lg font-normal text-ink-2">{episodeDetail.title}</span>}
              </h3>
              {episodeDetail.chapter_title && (
                <p className="text-sm text-ink-2 mt-1">{t('wb.chapterLabel')}{episodeDetail.chapter_title}</p>
              )}
            </div>
            <span className={`px-3 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
              episodeDetail.status === 'done' ? 'bg-success-subtle text-success-strong' :
              episodeDetail.status === 'producing' ? 'bg-info-subtle text-info-strong' :
              episodeDetail.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
              'bg-surface-2 text-ink-1'
            }`}>
              {episodeDetail.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
               episodeDetail.status === 'producing' ? t('wb.producing') :
               episodeDetail.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
               t('ep.pending')}
            </span>
          </div>

          <div className="mt-4 flex items-center gap-4 text-sm text-ink-2">
            <span>{t('wb.shotProgress', { done: episodeDetail.completed_shots, total: episodeDetail.shot_count })}</span>
            {episodeDetail.created_at && (
              <span>{t('wb.createdAt', { time: episodeDetail.created_at.split('T')[0] })}</span>
            )}
          </div>

          {episodeDetail.shot_count > 0 && (
            <div className="mt-3 w-full bg-line rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-all ${
                  episodeDetail.status === 'done' ? 'bg-success' :
                  episodeDetail.status === 'failed' ? 'bg-danger' :
                  'bg-brand'
                }`}
                style={{ width: `${(episodeDetail.completed_shots / episodeDetail.shot_count) * 100}%` }}
              ></div>
            </div>
          )}
        </div>


        <div className="bg-surface rounded-lg border border-line p-6">
          <h4 className="font-semibold text-ink-1 mb-4">{t('wb.scriptContent')}</h4>

          {(episodeDetail.shots && episodeDetail.shots.length > 0) ? (
            <div className="space-y-4">
              {episodeDetail.shots.map((shot: any, idx: number) => (
                <div key={idx} className="border-l-4 border-brand pl-4 py-2">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="px-2 py-0.5 bg-brand-subtle text-brand text-xs font-medium rounded">
                      {t('wb.shotN', { n: shot.shot_id ?? idx + 1 })}
                    </span>
                    {shot.camera && (
                      <span className="text-xs text-ink-2">{shot.camera}</span>
                    )}
                    {shot.location && (
                      <span className="text-xs text-ink-2">· {shot.location}</span>
                    )}
                    {shot.duration != null && (
                      <span className="text-xs text-ink-3">· {shot.duration}s</span>
                    )}
                  </div>
                  <ShotPromptEditor
                      shot={shot}
                      novelId={novelId}
                      episodeNo={selectedEpisode!}
                      onSaved={() => loadEpisodeDetail(selectedEpisode!)}
                    />
                  {shot.dialogue_text && (
                    <p className="text-sm text-ink-1 mt-1 pl-2 border-l-2 border-line-strong">
                      {shot.dialogue_text}
                    </p>
                  )}
                  {shot.visual_detail && (
                    <p className="text-xs text-ink-2 mt-1">
                      {t('wb.visualDetail', { text: shot.visual_detail })}
                    </p>
                  )}
                  {shot.audio_cues && (
                    <p className="text-xs text-ink-3 mt-1">
                      {t('wb.audioCues', { text: shot.audio_cues })}
                    </p>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-ink-2 text-sm">
              {t('wb.noShotsInEpisode')}
            </p>
          )}
        </div>
      </div>
    );
  }

  if (detailError) {
    // 2026-10-12：拆开「尚未生成」与「真的出错」—— 前者是**生产过程中的正常态**，
    //   旧实现把它当成红色错误态（ErrorState）甩给用户，还把后端异常里的
    //   **绝对文件路径** 原样当描述显示（用户反馈：「这个界面需要优化现在太丑了」）。
    //   现在：尚未生成 → 友好空态 + 明确的下一步；其他才是错误态，且描述做脱敏。
    //   t46：判定改结构化 —— 后端 EpisodeNotFoundError 固定回 HTTP 404，用状态码判定，
    //   不再正则匹配「尚未生成/不存在」等中文文案（后端文案一变就会失效）。
    const notGenerated = detailStatus === 404;
    const epNo = lastDetailEpRef.current;
    if (notGenerated) {
      return (
        <div className="space-y-4">
          <Button variant="ghost" onClick={goBack}>
            {t('wb.backToList')}
          </Button>
          <div className="rounded-lg border border-line bg-surface p-6">
            <EmptyState
              icon={<FileText className="h-10 w-10" />}
              title={epNo != null ? t('wb.episodeNotGenerated', { n: epNo }) : t('wb.episodeNotGeneratedNoN')}
              description={t('wb.episodeNotGeneratedHint')}
              action={(
                <div className="flex items-center gap-3">
                  <Button onClick={goBack}>{t('wb.startGenerate')}</Button>
                  <Button
                    variant="ghost"
                    onClick={() => {
                      const n = lastDetailEpRef.current;
                      if (n != null) loadEpisodeDetail(n);
                    }}
                  >
                    {t('common.retry')}
                  </Button>
                </div>
              )}
            />
          </div>
        </div>
      );
    }
    // 硬失败：单集详情整块取不到数据，用 ErrorState 顶掉内容区（不是把已渲染内容盖掉）
    return (
      <div className="space-y-4">
        <ErrorState
          title={t('project.loadingFailed')}
          description={t('wb.loadDetailFailedHint')}
          code={sanitizeErrorMessage(detailError)}
          onRetry={() => {
            const n = lastDetailEpRef.current;
            if (n != null) loadEpisodeDetail(n);
          }}
        />
        <Button variant="link" className="text-sm" onClick={goBack}>
          {t('wb.backToList')}
        </Button>
      </div>
    );
  }

  if (detailLoading) {
    return (
      <div className="space-y-4" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <Skeleton className="h-9 w-28" />
        <Skeleton className="h-40 rounded-lg" />
        <Skeleton className="h-64 rounded-lg" />
      </div>
    );
  }

  if (total === 0 && episodes.length === 0) {
    // t4：空项目（无资产、无剧集）恰恰是最需要「开始生产」入口的场景 ——
    // 旧实现在这里整块 return，把生产 CTA 一并挡掉了，新用户只能去 AI 总控说话。
    return (
      <div className="space-y-6">
        <StartProductionCard
          projectKey={projectKey}
          episodes={startEpisodes}
          episodesLoading={scriptLoading}
          episodesError={scriptError}
          onRefreshEpisodes={() => fetchEpisodes()}
        />
        <EmptyState
          icon={<FolderOpen className="h-10 w-10" />}
          title={t('wb.noAssets')}
          description={t('wb.noAssetsHint')}
          action={
            <p className="text-sm text-brand">
              {t('wb.noAssetsAction')}
            </p>
          }
        />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* t4：主路径 CTA —— 「开始生产」（runOnce 的唯一入口，默认标签页首屏可见） */}
      <StartProductionCard
        projectKey={projectKey}
        episodes={startEpisodes}
        episodesLoading={scriptLoading}
        episodesError={scriptError}
        onRefreshEpisodes={() => fetchEpisodes()}
      />

      {/* 生产模式（托管 / 自主 二选一，见 ProductionModeCard）*/}
      <ProductionModeCard projectKey={projectKey} />


      {/* 资产展示（已抽到 @/components/AssetsGallery，E-5） */}
      <AssetsGallery assets={assets} preview={preview} setPreview={setPreview} projectKey={projectKey} onRefreshAssets={onRefreshAssets} onUpload={() => setUploadOpen(true)} />
      {/* 上传形象图模态挂在 total>0 之外：空项目也能先上传角色形象图再跑剧本 */}
      <UploadSheetModal
        isOpen={uploadOpen}
        onClose={() => setUploadOpen(false)}
        projectKey={projectKey}
        characters={uploadCharacters}
        onUploaded={onRefreshAssets}
      />

      {/* 剧本概览 */}
      <div className="border-t border-line pt-8">
        <h3 className="text-lg font-semibold text-ink-1 mb-4 flex items-center gap-2">
          <FileText className="h-5 w-5" /> {t('wb.script')}
        </h3>

        {scriptLoading ? (
          // 骨架对齐真实区块：4 张统计卡 → 进度条 → 剧集列表卡
          <div role="status" aria-live="polite" aria-label={t('common.loading')}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-20 rounded-lg" />
              ))}
            </div>
            <Skeleton className="mt-4 h-16 rounded-lg" />
            <Skeleton className="mt-4 h-48 rounded-lg" />
          </div>
        ) : scriptError ? (
          <ErrorState
            title={t('project.loadingFailed')}
            description={sanitizeErrorMessage(scriptError)}
            onRetry={fetchEpisodes}
          />
        ) : episodes.length === 0 ? (
          <EmptyState
            icon={<ClipboardList className="h-10 w-10" />}
            title={t('episodes.noEpisodes')}
            description={t('wb.startAutoFirst')}
            action={(
              <Button size="sm" variant="secondary" onClick={() => fetchEpisodes()}>
                {t('common.refresh')}
              </Button>
            )}
          />
        ) : (
          <>
            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
              <div className="bg-surface rounded-lg border border-line p-4">
                <div className="text-2xl font-bold text-ink-1">{totalEpisodes}</div>
                <div className="text-sm text-ink-2">{t('wb.totalEpisodes')}</div>
              </div>
              {(() => {
                const stats = episodes.reduce((acc: any, ep: any) => {
                  acc[ep.status] = (acc[ep.status] || 0) + 1;
                  return acc;
                }, {} as Record<string, number>);
                return [
                  { label: t('episodes.done'), count: stats['done'] || 0, color: 'text-success-strong' },
                  { label: t('episodes.producing'), count: stats['producing'] || 0, color: 'text-info-strong' },
                  { label: t('episodes.failed'), count: stats['failed'] || 0, color: 'text-danger-strong' },
                  { label: t('episodes.pending'), count: stats['pending'] || 0, color: 'text-ink-2' },
                ].map(s => (
                  <div key={s.label} className="bg-surface rounded-lg border border-line p-4">
                    <div className={`text-2xl font-bold ${s.color}`}>{s.count}</div>
                    <div className="text-sm text-ink-2">{s.label}</div>
                  </div>
                ));
              })()}
            </div>

            {/* 进度条 */}
            {totalEpisodes > 0 && (
              <div className="bg-surface rounded-lg border border-line p-4 mb-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-medium text-ink-1">{t('wb.overallProgress')}</span>
                  <span className="text-sm text-ink-2">{Math.round(((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100)}%</span>
                </div>
                <div className="w-full bg-line rounded-full h-2">
                  <div
                    className="bg-success h-2 rounded-full transition-all"
                    style={{ width: `${((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100}%` }}
                  ></div>
                </div>
              </div>
            )}

            {/* 前置解析卡片（已抽到 @/components/ChapterPreflightCard，E-5） */}
            <ChapterPreflightCard open={preflightOpen} detail={preflightDetail} onClose={() => setPreflightOpen(false)} />
            {/* 剧集列表（已抽到 @/components/EpisodeListPanel，E-5） */}
            <EpisodeListPanel
              episodes={episodes}
              splitPlanOpen={splitPlanOpen} setSplitPlanOpen={setSplitPlanOpen} fetchSplitPlan={fetchSplitPlan}
              splitPlanLoading={splitPlanLoading} splitPlanError={splitPlanError} splitPlan={splitPlan}
              preflightOpen={preflightOpen} setPreflightOpen={setPreflightOpen}
              loadEpisodeDetail={loadEpisodeDetail} toggleScenes={toggleScenes}
              runPreflight={runPreflight} openPreflightDetail={openPreflightDetail}
              scenesOpenEps={scenesOpenEps} scenesByEp={scenesByEp} scenesLoadingEp={scenesLoadingEp}
              scenesErrByEp={scenesErrByEp} fetchScenes={fetchScenes}
              redoScene={redoScene} redoBusy={redoBusy} preflightDone={preflightDone} preflightRunning={preflightRunning}
            />
          </>
        )}
      </div>

    </div>
  );
}