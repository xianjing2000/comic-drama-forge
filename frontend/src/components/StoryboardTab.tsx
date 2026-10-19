/** StoryboardTab —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { StoryboardNoticeBar } from '@/components/StoryboardNoticeBar';
import { StoryboardQualityBar } from '@/components/StoryboardQualityBar';
import { ShotCard } from '@/components/ShotCard';
import { ShotLightboxModal } from '@/components/ShotLightboxModal';
import { BATCH_RETRY_LIMIT, PROJECT_VIDEO_MODE_OPTIONS } from '@/pages/workbenchShared';
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

export function StoryboardTab({ projectKey, episodeNo, onGotoOutput,
  focusShot, focusEpisodeNo, onFocusConsumed }: {
  projectKey: string;
  episodeNo?: number | null;
  /** t27（F5）：返工使成片/超分失效时，「去成品验收重新合成」的一键跳转出口 */
  onGotoOutput?: () => void;
  /** t9：要定位（滚动+高亮）的镜号；来自音频体检面板的「定位分镜」 */
  focusShot?: string | null;
  /** t9：该镜所属集：与当前集号不一致时先不滚（等集切换完成，避免落到同号不同集的卡上） */
  focusEpisodeNo?: number | null;
  /** t9：定位完成后回调（父层清空焦点，避免每次 re-render 重复滚动） */
  onFocusConsumed?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [cards, setCards] = useState<any[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  /** 正在重做的镜头：`${shot_id}:image` / `${shot_id}:video` */
  const [busy, setBusy] = useState<string | null>(null);
  /** 每镜的视频重做模式（reference=分镜图驱动 / keyframe=首尾帧插值） */
  const [videoMode, setVideoMode] = useState<Record<string, 'reference' | 'keyframe'>>({});
  /** 正在内嵌预览视频的镜头 id（null=全部收起）：按镜头单开，避免多卡同时播放 */
  const [playingVideo, setPlayingVideo] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [shotError, setShotError] = useState('');
  // t27（F5）：本次返工是否让「成片/超分」失效。为真时 notice 里额外给一个
  // 「去成品验收」按钮 —— 只给文案用户还得自己找入口，闭环少了最后一步。
  const [outputsInvalidated, setOutputsInvalidated] = useState(false);
  /** 整集一次提交（mode=episode）：触发中标记 + 返回的 task_id */
  const [episodeGenerating, setEpisodeGenerating] = useState(false);
  const [episodeTaskId, setEpisodeTaskId] = useState<string | null>(null);
  /** 项目级视频生成方式（新建项目时选的 video_mode）：本页「生成视频」按它执行。
   *  ⚠️ 不要与上面每镜的 videoMode（reference/keyframe 单镜重做）混用，两者不是一个东西。 */
  const [projectVideoMode, setProjectVideoMode] = useState<VideoMode>('episode');

  // ---- 批量重生成（POST /api/video/retry-shots-batch，单次 ≤12 个）----
  /** 已勾选待批量重生成的镜头 id（String(card.shot_id)） */
  const [selectedShots, setSelectedShots] = useState<string[]>([]);
  /** 批量请求进行中：同步端点可能耗时数分钟，期间防重复提交 */
  const [batchRunning, setBatchRunning] = useState(false);
  /** 批量重生成确认弹窗 */
  const [batchConfirmOpen, setBatchConfirmOpen] = useState(false);
  /** 最近一次批量结果（成功 N/共 M + 失败明细），展示在工具栏下方 */
  const [batchResult, setBatchResult] = useState<{
    total: number;
    ok: number;
    failures: { shot_id: string; error: string }[];
  } | null>(null);

  // ---- 分镜图点击放大（lightbox）----
  /** 正在放大的分镜图：正式图或生成中 scratch 图；null = 弹窗关闭。
   *  分镜缩略图原先没有任何点击交互（点了没反应），这里补上全屏放大查看。 */
  const [zoomImg, setZoomImg] = useState<{ src: string; seq: number; grid?: boolean } | null>(null);

  // ---- t9：音频体检「定位分镜」的落地（滚动 + 高亮）----
  /** 卡片列表容器：用 data-shot-id 在本容器内查找目标卡（不跨组件、不摸全局 DOM） */
  const cardsRef = useRef<HTMLDivElement | null>(null);
  /** 当前高亮的镜号（2.6s 后自动褪去）；null = 无高亮 */
  const [focusHighlight, setFocusHighlight] = useState<string | null>(null);
  /** t12：软放行镜清单是否展开（质检不达标但已入库的镜，默认收起、徽标常显） */
  const [softOpen, setSoftOpen] = useState(false);
  /** t12：本集三类质量计数（真通过 / 软放行 / 阻断）—— 软放行绝不并入成功 */
  // t25：后端 summary 给出 success_count/soft_accepted_count/... 时**以后端为准**
  // （同源同值，避免前后端两套算法规格漂移）；旧后端没给则回落前端自算。
  const sbQuality = mergeBackendQuality(summarizeStoryboardQuality(cards as any[]), summary);
  useEffect(() => {
    if (!focusShot) return;
    // 目标镜不在本集（跨集跳转刚切集、卡片还没刷新）→ 先不滚，等卡片到位
    if (focusEpisodeNo != null && episodeNo != null && Number(focusEpisodeNo) !== Number(episodeNo)) return;
    const sid = String(focusShot);
    if (!cards.some((c: any) => String(c.shot_id) === sid)) return;   // 卡片未加载 → cards 更新后会再跑
    const el = cardsRef.current?.querySelector('[data-shot-id="' + sid + '"]') as HTMLElement | null;
    if (!el) return;
    try { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch { /* 老浏览器无此 API：仍高亮 */ }
    setFocusHighlight(sid);
    const timer = setTimeout(() => { setFocusHighlight(null); onFocusConsumed?.(); }, 2600);
    return () => clearTimeout(timer);
  }, [focusShot, focusEpisodeNo, episodeNo, cards, onFocusConsumed]);

  // ---- 分镜画布自动刷新（轮询）----
  /**
   * 画布自动刷新句柄。
   * 背景：分镜图是「整步落盘」——未完成时 storyboards/<项目>/ 里没有文件，
   * 生成中的图只在 QC_DIR/<项目>/storyboard_scratch/。若只靠手动刷新，
   * 用户会长时间看到「无分镜图」而误以为卡死。这里每 3s 重拉一次 canvas，
   * 生成中卡片即可看到 generating/scratch_url，完成后自动变成正式图。
   */
  const canvasPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  /**
   * 轮询是否「有理由继续」的最新快照（避免把 loading/cards 放进 useEffect 依赖：
   * 那会在每次 setCards 后重建定时器，轮询节奏被打乱）。
   */
  const canvasPollStateRef = useRef<{ generating: number; total: number; stepActive: boolean }>({
    generating: 0, total: 0, stepActive: false,
  });

  // ---- 镜头排序（POST /api/storyboard/shot/reorder）----
  /** 排序请求在途：期间禁止再次拖拽/移动 */
  const [reorderBusy, setReorderBusy] = useState(false);
  /** 上一条的 ref 快照：静默轮询要读它，但不该进 useEffect 依赖（否则每次排序都重建定时器） */
  const reorderBusyRef = useRef(false);
  /** 正在被拖拽的镜头 id（null=无拖拽） */
  const [dragSid, setDragSid] = useState<string | null>(null);
  /** 拖拽悬停到的镜头 id：只用来高亮落点 */
  const [dragOverSid, setDragOverSid] = useState<string | null>(null);

  /**
   * 分镜域统一错误脱敏：后端异常原文可能带本机绝对路径（AppData / 项目目录 / 暂存目录），
   * 一律先过 sanitizeErrorMessage 再进界面 —— 用户不需要知道自己的文件系统长什么样。
   * （与工作台其它区块的 sanitizeError 同口径，但这里额外剥掉路径）
   */
  const sbError = (e: unknown, fallback: string) => {
    const raw = e instanceof Error ? e.message : (typeof e === 'string' ? e : '');
    return sanitizeErrorMessage(raw || fallback) || fallback;
  };

  const stopCanvasPoll = () => {
    if (canvasPollRef.current) { clearInterval(canvasPollRef.current); canvasPollRef.current = null; }
  };

  const fetchCanvas = async (opts?: { silent?: boolean }) => {
    if (!projectKey) return;
    const silent = !!opts?.silent;
    // 排序请求在途时不让静默轮询回灌：后端还没落盘的新顺序会在这里被旧顺序覆盖，
    // 用户会看到「刚拖完又弹回去」。排序成功后由本地顺序兜底，下一轮轮询自然对齐。
    if (silent && reorderBusyRef.current) return;
    // 静默轮询不翻转 loading：否则每 3 秒整页闪一次骨架屏
    if (!silent) setLoading(true);
    if (!silent) setError('');
    try {
      // ⭐ 必须带集号：不带时后端只认「已生成集里最新的那集」，
      //    用户切到第 2 集却看到第 N 集的分镜（与集切换器显示的集不一致）。
      const data = await storyboardApi.canvas(projectKey, episodeNo ?? undefined);
      const nextCards = data.cards || [];
      setCards(nextCards);
      setSummary((data as any).summary || null);
      if (silent) setError('');
      // 记录「是否值得继续轮询」：有生成中卡片 → 继续
      const gen = nextCards.filter((c: any) => c?.storyboard?.generating).length;
      const total = nextCards.length;
      canvasPollStateRef.current = {
        generating: gen,
        total,
        stepActive: canvasPollStateRef.current.stepActive,
      };
    } catch (err) {
      if (silent) return; // 静默轮询失败：留给下一轮，不打断用户
      // 404 判定看**原始**文案（脱敏会折叠空白，判定要在脱敏之前做）；
      // 展示一律走脱敏后的文案（不把后端绝对路径甩给用户）
      const rawMsg = err instanceof Error ? err.message : '';
      if (rawMsg.includes('404')) {
        setError('no-data');
      } else {
        setError(sbError(err, t('sb.fetchFailed')));
      }
    } finally {
      if (!silent) setLoading(false);
    }
  };

  // 换集必须重拉：cards 是按集落盘的分镜图/视频，混用会张冠李戴。
  // ⭐ 同时启动自动刷新轮询：生成过程中画布会持续变化（生成中卡片出现 → 转正），
  //    不轮询则用户必须手动点刷新才能看到新图。
  useEffect(() => {
    if (!projectKey) return;
    canvasPollStateRef.current = { generating: 0, total: 0, stepActive: canvasPollStateRef.current.stepActive };
    fetchCanvas();
    stopCanvasPoll();
    // 5s：比九宫格轮询（3s，用户在前台等一张图）慢一档；分镜整步动辄 20+ 镜、
    // 每镜 2-3 分钟，5s 足够让「生成中→完成」的观感接近实时，又不至于压后端。
    canvasPollRef.current = setInterval(() => { fetchCanvas({ silent: true }); }, 5000);
    return () => stopCanvasPoll();
    // fetchCanvas 是每次渲染新建的闭包（内部读 projectKey/episodeNo），
    // 只以这两个值为准重启动轮询；闭包捕获的是本次渲染的最新值，不会读到陈旧集号。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey, episodeNo]);

  // 项目级视频生成方式：进页面就回显（用户「新建项目」时选的），改成什么就按什么生成
  useEffect(() => {
    if (!projectKey) return;
    projectsApi.getConfig(projectKey)
      .then((d) => setProjectVideoMode(((d.config?.video_mode as VideoMode) || 'episode')))
      .catch(() => null);
  }, [projectKey]);

  const handleRetryImage = async (card: any) => {
    const key = `${card.shot_id}:image`;
    setBusy(key);
    setShotError('');
    setNotice('');
    try {
      await storyboardApi.retryShot({
        project_name: projectKey,
        shot_id: String(card.shot_id),
        // 单镜重跑也带集号：后端据此决定写 <项目>/ 还是 <项目>/epNN/，
        // 漏传会把第 2 集的图写进第 1 集目录（覆盖第 1 集同号镜头的图）。
        episode_no: episodeNo ?? undefined,
      });
      setNotice(t('sb.imageRedone', { seq: card.seq }));
      await fetchCanvas();
    } catch (e) {
      setShotError(sbError(e, t('sb.imageRedoFailed')));
    } finally {
      setBusy(null);
    }
  };

  const handleRetryVideo = async (card: any) => {
    const key = `${card.shot_id}:video`;
    const mode = videoMode[String(card.shot_id)] || 'reference';
    if (mode === 'keyframe' && !card.keyframe?.end_exists) {
      setShotError(t('sb.noKeyframeForInterp', { seq: card.seq }));
      return;
    }
    setBusy(key);
    setShotError('');
    setNotice('');
    setOutputsInvalidated(false);
    try {
      const r = await videoApi.retryShot({
        project_name: projectKey,
        shot_id: card.shot_id,
        mode,
        episode_no: episodeNo ?? undefined,
      });
      // t27（F5）：t17 起后端回传 outputs_invalidated（本次**实际**失效的产物清单，
      // 如 ["final","upscale"]）。旧字段 deliverable_marked_stale 只说「成片被标记」，
      // 分不出成片还是超分；新字段才能给出准确后果与下一步。
      const invalidated = Array.isArray(r.outputs_invalidated) ? r.outputs_invalidated : [];
      setOutputsInvalidated(invalidated.length > 0 || Boolean(r.deliverable_marked_stale));
      setNotice(
        t('sb.videoRedone', {
          seq: card.seq,
          mode: r.mode === 'keyframe' ? t('sb.modeKeyframe') : t('sb.modeReference'),
          refCount: r.ref_count,
          duration: r.duration,
        }) + (invalidated.length > 0
          ? t('sb.videoOutputsInvalidated')
          : (r.deliverable_marked_stale ? t('sb.videoRedoneStale') : ''))
      );
      await fetchCanvas();
    } catch (e) {
      setShotError(sbError(e, t('sb.videoRedoFailed')));
    } finally {
      setBusy(null);
    }
  };

  /** 改项目级视频生成方式：写进项目配置，之后「生成视频」与托管生产都按它执行 */
  const handleProjectVideoModeChange = async (v: string) => {
    if (!projectKey) return;
    const prev = projectVideoMode;
    setProjectVideoMode(v as VideoMode);
    try {
      const d = await projectsApi.updateConfig(projectKey, { video_mode: v });
      const saved = (d.config?.video_mode as VideoMode) || (v as VideoMode);
      setProjectVideoMode(saved);
      toast.success(t('sb.videoModeSaved'));
    } catch (e) {
      // 保存失败必须回滚 UI，否则界面显示的模式与实际生成方式不一致（最难查的一类漂移）
      setProjectVideoMode(prev);
      toast.error(sbError(e, t('sb.videoModeSaveFailed')));
    }
  };

  // 生成该集视频：方式取**项目级设定**（episode 整集一次出连续片 / per_shot 逐镜 /
  // keyframe 首尾帧插值）。旧实现把 mode 写死成 episode，用户在新建设置里选什么都无效。
  const handleGenerateEpisode = async () => {
    if (!projectKey) return;
    setEpisodeGenerating(true);
    setShotError('');
    try {
      const r = await videoApi.generateEpisode({
        project_name: projectKey,
        episode_no: episodeNo ?? undefined,
        mode: projectVideoMode,
      });
      setEpisodeTaskId(r.task_id);
      toast.success(t('sb.episodeGenerateStarted', { total: r.total }));
    } catch (e) {
      setShotError(sbError(e, t('sb.videoRedoFailed')));
    } finally {
      setEpisodeGenerating(false);
    }
  };

  // ---- 批量重生成 ----
  // 刷新/切集后清掉已不在当前列表里的勾选（防止带着旧集的镜头去批量重做）
  useEffect(() => {
    const ids = new Set(cards.map((c: any) => String(c.shot_id)));
    setSelectedShots((prev) => prev.filter((id) => ids.has(id)));
  }, [cards]);

  const toggleShotSelected = (sid: string) => {
    setSelectedShots((prev) =>
      prev.includes(sid) ? prev.filter((s) => s !== sid) : [...prev, sid]
    );
  };

  // 确认弹窗里点「确认」后执行。与单镜重跑一样是同步等待（逐镜等 ComfyUI 出片，
  // 可能耗时数分钟）：ConfirmDialog loading 期间不可关闭，按钮 loading 防重复提交。
  const runBatchRetry = async () => {
    if (batchRunning || selectedShots.length === 0) return;
    setBatchRunning(true);
    setShotError('');
    setNotice('');
    setOutputsInvalidated(false);
    try {
      const r = await videoApi.retryShotsBatch(projectKey, episodeNo ?? undefined, selectedShots);
      const results = Array.isArray(r.results) ? r.results : [];
      const ok = typeof r.ok_count === 'number'
        ? r.ok_count
        : results.filter((x) => x.success).length;
      const failures = results
        .filter((x) => !x.success)
        // 失败原因是后端逐镜回传的原文 → 同样脱敏（可能含本机路径），再进结果条
        .map((x) => ({ shot_id: String(x.shot_id),
                       error: sanitizeErrorMessage(x.error || '') || t('video.batchRetry.unknownError') }));
      const total = results.length || selectedShots.length;
      // t27（F5）：批量端点把单镜 payload 展开进 results，逐条读失效清单即可
      setOutputsInvalidated(results.some(
        (x) => Array.isArray(x.outputs_invalidated) && x.outputs_invalidated.length > 0
      ));
      setBatchResult({ total, ok, failures });
      if (failures.length === 0) {
        toast.success(t('video.batchRetry.done', { ok, total }));
      } else {
        toast.warning(t('video.batchRetry.partial', { ok, total }));
      }
      setSelectedShots([]);
      await fetchCanvas();
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('video.batchRetry.submitFailed');
      setShotError(msg);
      toast.error(msg);
    } finally {
      setBatchRunning(false);
      setBatchConfirmOpen(false);
    }
  };

  // ---- 镜头排序：乐观更新 + 失败回滚（后端 POST /api/storyboard/shot/reorder）----
  /**
   * 落盘顺序的两条通路：手柄拖拽（大跨度）与 ↑/↓（精确挪一格）。
   * 两者都先本地重排（用户立刻看到结果），再请求后端；失败**整表回滚**并给可读原因。
   * 成功不重拉画布 —— 本地顺序就是权威，5s 轮询会在下一拍自然对齐后端。
   * 注意：这里的顺序是「生成与成片顺序」，生产跑着时调整可能与本轮生成交错，
   * 界面在生成中会给出提醒（不硬禁用：用户可能就是想立刻纠正顺序）。
   */
  const applyShotOrder = async (nextOrder: string[]) => {
    if (!projectKey || reorderBusyRef.current) return;
    const prevCards = cards;
    const bySid = new Map(prevCards.map((c: any) => [String(c.shot_id), c]));
    const nextCards = nextOrder.map((sid) => bySid.get(sid)).filter(Boolean) as any[];
    // 与本地卡片对不上（数量不符 / 出现未知 id）：不动本地顺序，直接把后端真相拉回来，
    // 否则界面显示的排序与落盘内容会分叉，而用户无从察觉。
    if (nextCards.length !== prevCards.length) {
      setShotError(t('sb.order.staleList'));
      await fetchCanvas();
      return;
    }
    reorderBusyRef.current = true;
    setReorderBusy(true);
    setShotError('');
    setCards(nextCards); // 乐观更新
    try {
      await storyboardApi.reorder({
        project_name: projectKey,
        episode_no: episodeNo ?? undefined,
        order: nextOrder,
      });
      toast.success(t('sb.order.saved'));
    } catch (e) {
      setCards(prevCards); // 失败回滚：UI 绝不与落盘内容不一致
      setShotError(t('sb.order.saveFailedRollback', { error: sbError(e, t('sb.order.saveFailed')) }));
      toast.error(t('sb.order.saveFailed'));
    } finally {
      reorderBusyRef.current = false;
      setReorderBusy(false);
    }
  };

  /** ↑/↓：与相邻镜头交换一格（越界直接忽略，按钮本身也已禁用） */
  const moveShot = (sid: string, delta: -1 | 1) => {
    const ids = cards.map((c: any) => String(c.shot_id));
    const i = ids.indexOf(sid);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= ids.length) return;
    const next = ids.slice();
    [next[i], next[j]] = [next[j], next[i]];
    void applyShotOrder(next);
  };

  /** 拖拽落点：把被拖的镜头插到目标位置（其余顺延） */
  const dropShotOn = (targetSid: string) => {
    const from = dragSid;
    setDragSid(null);
    setDragOverSid(null);
    if (!from || from === targetSid) return;
    const ids = cards.map((c: any) => String(c.shot_id));
    const fi = ids.indexOf(from);
    const ti = ids.indexOf(targetSid);
    if (fi < 0 || ti < 0) return;
    const next = ids.slice();
    next.splice(fi, 1);
    next.splice(ti, 0, from);
    void applyShotOrder(next);
  };

  /** 正在生成的镜数：排序提醒用（生成中调序可能与本轮交错） */
  const generatingShots = Number(summary?.storyboard_generating ?? 0);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">{t('wb.storyboardHub')}</h3>
        <div className="flex items-center gap-2">
          {/* 视频生成方式（项目级）：默认取「新建项目」时选的值，这里可改并即刻落盘 */}
          <Select
            value={projectVideoMode}
            onChange={handleProjectVideoModeChange}
            options={PROJECT_VIDEO_MODE_OPTIONS.map(o => ({ value: o.value, label: t(o.labelKey) }))}
            disabled={loading || episodeGenerating}
            className="w-40"
          />
          <Button
            size="sm"
            onClick={handleGenerateEpisode}
            disabled={loading || episodeGenerating}
            className="bg-brand hover:bg-brand-strong"
          >
            {episodeGenerating ? t('common.generating') : t('sb.generateVideo')}
          </Button>
          {/* 批量重生成：无选中禁用；超上限在复选框层已挡，这里再兜底 */}
          {selectedShots.length > 0 && (
            <span className="text-xs text-ink-2 whitespace-nowrap">
              {t('video.batchRetry.selectedCount', { n: selectedShots.length, max: BATCH_RETRY_LIMIT })}
            </span>
          )}
          <Button
            size="sm"
            variant="secondary"
            onClick={() => { setBatchResult(null); setBatchConfirmOpen(true); }}
            disabled={loading || batchRunning || selectedShots.length === 0 || selectedShots.length > BATCH_RETRY_LIMIT}
            title={t('video.batchRetry.buttonHint')}
          >
            {batchRunning ? t('video.batchRetry.running') : t('video.batchRetry.button')}
          </Button>
          <Button size="sm" onClick={fetchCanvas} disabled={loading}>{t('common.refresh')}</Button>
        </div>
      </div>

      {/* 加载态（此前首屏只剩标题栏，无任何反馈）：对齐真实区块的三列分镜卡 */}
      {loading && cards.length === 0 && (
        <div role="status" aria-live="polite" aria-label={t('common.loading')}>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-56 rounded-lg" />
            ))}
          </div>
        </div>
      )}

      {summary && (
        <div className="flex flex-wrap gap-4 text-sm text-ink-2">
          <span>{t('sb.shotCount', { n: summary.shot_count })}</span>
          <span>{t('sb.storyboardCount', { n: summary.storyboard_ready ?? 0 })}</span>
          <span>{t('sb.videoCount', { n: summary.video_ready ?? 0 })}</span>
          <span>{t('sb.keyframeCount', { n: summary.keyframe_end_ready ?? 0 })}</span>
          {(summary.qc_blocked ?? 0) > 0 && (
            <span className="text-warning-strong">{t('storyboard.qcBlocked')} {summary.qc_blocked}</span>
          )}
        </div>
      )}

      {/* ⭐ t12（2026-10-12）：分镜质量**三类分离** —— 软放行（质检不达标但仍入库）
          绝不再冒充成功。旧界面只显示「分镜图 46/46」，实测 37% 是「放行了但有问题」
          的镜（含带 critical_issues 的严重问题镜），用户以为整集合格。 */}
      {cards.length > 0 && (
        <StoryboardQualityBar quality={sbQuality} softOpen={softOpen} onToggleSoft={() => setSoftOpen((v) => !v)} />
      )}

      {notice && (
        <StoryboardNoticeBar notice={notice} canGotoOutput={!!outputsInvalidated} onGotoOutput={onGotoOutput} />
      )}
      {/* 软失败：重做单镜失败时卡片仍在展示，只能用紧凑行内条，不能顶掉内容 */}
      {shotError && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
          {shotError}
        </div>
      )}

      {/* 批量重生成结果：成功 N/共 M + 失败明细（错误可能整段话，行内展示比 toast 从容） */}
      {batchResult && (
        <div className={`p-3 rounded-lg border text-sm ${
          batchResult.failures.length === 0
            ? 'bg-success-subtle border-success/30 text-success-strong'
            : 'bg-warning-subtle border-warning/30 text-warning-strong'
        }`}>
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">
              {batchResult.failures.length === 0
                ? t('video.batchRetry.done', { ok: batchResult.ok, total: batchResult.total })
                : t('video.batchRetry.partial', { ok: batchResult.ok, total: batchResult.total })}
            </span>
            <button
              type="button"
              onClick={() => setBatchResult(null)}
              aria-label={t('common.close')}
              className={`shrink-0 opacity-60 hover:opacity-100 ${FOCUS_RING}`}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          {batchResult.failures.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs">
              {batchResult.failures.map((f) => (
                <li key={f.shot_id} className="text-danger-strong break-all">
                  {t('video.batchRetry.failedItem', { shot_id: f.shot_id, error: f.error })}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {error === 'no-data' && (
        <EmptyState
          icon={<Clapperboard className="h-10 w-10" />}
          title={t('sb.noData')}
          description={t('sb.noDataHint')}
          // 空状态必须给出下一步：分镜产物是「整步落盘」，用户常误以为卡死，
          // 这里至少给一个手动重拉入口（自动轮询 5s 仍在跑）
          action={<Button size="sm" onClick={() => fetchCanvas()}>{t('common.refresh')}</Button>}
        />
      )}

      {/* 硬失败：分镜数据整体没取到且无卡片可展示 → ErrorState；
          已有卡片时的刷新失败 → 行内条（软失败） */}
      {error && error !== 'no-data' && cards.length === 0 && (
        <ErrorState
          title={t('project.loadingFailed')}
          description={sanitizeErrorMessage(error)}
          onRetry={fetchCanvas}
        />
      )}
      {error && error !== 'no-data' && cards.length > 0 && (
        /* t27（F6）：这是**刻意**的软失败设计（见上方注释：已有卡片时的刷新失败走行内条，
           不吃掉已展示内容）—— 保留形态，只补「重试」动作与脱敏，不改整块 ErrorState。 */
        <div className="flex items-start gap-3 p-4 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong">
          <span className="min-w-0 flex-1">{sanitizeErrorMessage(error)}</span>
          <Button size="sm" variant="secondary" onClick={() => void fetchCanvas()}>{t('common.retry')}</Button>
        </div>
      )}

      {/* 排序入口说明：拖拽手柄 / ↑↓ 两种方式（触屏与键盘场景拖拽不可靠，故两套都给）。
          ≤1 镜时没有可排序的东西，整条不显示，避免噪音。 */}
      {cards.length > 1 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-3">
          <span>{t('sb.order.hint')}</span>
          {reorderBusy && <span className="text-brand">{t('sb.order.saving')}</span>}
          {generatingShots > 0 && (
            <span className="text-warning-strong">
              {t('sb.order.generatingWarn', { n: generatingShots })}
            </span>
          )}
        </div>
      )}

      <div ref={cardsRef} className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card: any, cardIndex: number) => (
          <ShotCard
            key={card.seq}
            card={card} cardIndex={cardIndex} total={cards.length}
            busy={busy} videoMode={videoMode} selectedShots={selectedShots}
            dragSid={dragSid} dragOverSid={dragOverSid} reorderBusy={reorderBusy} batchRunning={batchRunning}
            focusHighlight={focusHighlight}
            playingVideo={playingVideo} setPlayingVideo={setPlayingVideo} setVideoMode={setVideoMode}
            toggleShotSelected={toggleShotSelected} dropShotOn={dropShotOn} moveShot={moveShot}
            setDragSid={setDragSid} setDragOverSid={setDragOverSid} setZoomImg={setZoomImg}
            handleRetryImage={handleRetryImage} handleRetryVideo={handleRetryVideo}
          />
        ))}
      </div>

      {/* 批量重生成确认：与 QcTab 清空配置同款模式 —— loading 期间不可关闭，防重复提交 */}
      <ConfirmDialog
        isOpen={batchConfirmOpen}
        onClose={() => setBatchConfirmOpen(false)}
        onConfirm={runBatchRetry}
        title={t('video.batchRetry.confirmTitle')}
        message={
          <>
            <p>{t('video.batchRetry.confirmBody', { n: selectedShots.length, shots: selectedShots.join(', ') })}</p>
            <p className="mt-2 text-xs text-ink-3">{t('video.batchRetry.confirmHint')}</p>
          </>
        }
        confirmText={batchRunning ? t('video.batchRetry.running') : t('video.batchRetry.button')}
        loading={batchRunning}
      />

      {/* 九宫格候选构图弹窗已下线（后端 7701cd7 随「一镜 3x3」整体移除，2026-10-19） */}

      {/* 分镜图放大 lightbox（已抽到 @/components/ShotLightboxModal，E-5） */}
      <ShotLightboxModal img={zoomImg} onClose={() => setZoomImg(null)} />
    </div>
  );
}