import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { projectsApi, keyframesApi, storyboardApi, videoApi, qcApi, autopilotApi, upscaleApi, chatApi, agentApi, episodesApi, novelsSplitPlanApi, preflightApi, characterOutfits, characterSheetUpload, assetPrecipitation, generationApi, assetsApi, asOverwriteConfirm, overwriteScopeSummary, type OverwriteConfirmPayload, type VideoMode, type EpisodeScenesResponse, type ChapterPreflightResult, type AssetPrecipitationResponse, type PrecipitationStatus } from '@/api/client';
import { Button, ConfirmDialog, Input, EmptyState, ErrorState, Loading, Skeleton, Modal, Select, ReorderControls } from '@/components/ui';
// tab 图标統一走线性 SVG（方案 P2-10）：此前是 emoji，字号受系统字体影响且观感与全站割裂
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
import { OverviewTab } from '@/components/OverviewTab';
import { assetSrc, type AssetItem, type ProjectAssets } from './workbenchShared';
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

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

// t27：sanitizeErrorMessage 的实现已收敛到 @/utils/errorText —— 交付页、质检面板、
// 执行流都需要同一份脱敏逻辑，留在页面内部无法复用（复制多份必然漂移）。
import { describeError, sanitizeErrorMessage } from '@/utils/errorText';

// t12（2026-10-12）：分镜质量三类口径（真通过 / 软放行 / 阻断）—— 与后端
// storyboard_helpers._sb_quality_tally 同源。软放行必须与成功分开显示，不能再全绿。
// t25（2026-10-12）：canvas 现在自带三类质量计数 summary（与 manifest 同源同值），
// 优先用它、缺失时回落前端自算 —— 保证「canvas soft 计数 == manifest soft_accepted_count」。
import { summarizeStoryboardQuality, mergeBackendQuality, shotQuality, criticalIssuesOf, softReasonOf } from '@/utils/storyboardQuality';

// ========== Workbench Tab Types ==========
// 注意：'chat' 已移除 —— AI 总控改成了右侧常驻面板，不再是标签页（见 ChatPanel）
import AutonomousPanel from '@/components/AutonomousPanel';
import QualityPanel from '@/components/QualityPanel';
import FailurePanel from '@/components/FailurePanel';
import TriagePanel from '@/components/TriagePanel';
// t9：音频体检面板（扫描分类计数 + 无声镜号定位 + 频谱/波形弹窗 + 门禁处置）
import AudioHealthPanel from '@/components/AudioHealthPanel';
import ExportPanel from '@/components/ExportPanel';

type WorkbenchTab = 'overview' | 'storyboard' | 'qc' | 'upscale' | 'audio' | 'output';


// ========== Main Workbench Page ==========
interface ProjectWorkbenchPageProps {
  projectKey: string;
}

export function ProjectWorkbenchPage({ projectKey }: ProjectWorkbenchPageProps) {
  const { t } = useApp();
  const [project, setProject] = useState<Project | null>(null);
  const [assets, setAssets] = useState<ProjectAssets | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('overview');
  // AI 总控默认展开为右侧常驻面板（不占标签页）；用户可收起，收起后右侧只剩一个竖条按钮
  const [chatOpen, setChatOpen] = useState(true);
  /**
   * t9：音频体检面板点「定位分镜」时带过来的镜号（含所属集）。
   * 为什么提升到工作台：体检面板在 audio 标签内，而分镜卡片在 storyboard 标签内
   * —— 跨标签定位必须由父层切标签 + 把镜号透传给分镜页，面板自己滚不到另一个标签的 DOM。
   * 消费后（分镜页滚动+高亮完成）回调清空，避免每次 re-render 重复滚动。
   */
  const [shotFocus, setShotFocus] = useState<{ shotId: string; episodeNo: number | null } | null>(null);
  const gotoShotInStoryboard = React.useCallback((shotId: string | number, episodeNo?: number) => {
    setShotFocus({ shotId: String(shotId), episodeNo: episodeNo ?? null });
    setActiveTab('storyboard');
  }, []);

  // 资产单独抽成函数：生产启动后可在不整页刷新的情况下重新拉取（缺陷 D3）
  const reloadAssets = React.useCallback(async () => {
    if (!projectKey) return;
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(projectKey)}/assets`);
      if (r.ok) setAssets(await r.json());
    } catch {
      /* 资产刷新失败不阻塞页面 */
    }
  }, [projectKey]);

  // ⭐ 资产自动刷新（2026-10-04 首版 / 2026-10-05 增强）：此前资产只随页面挂载拉一次
  //    （缺陷 D3 只解决了「生产启动后能手动重拉」，用户仍要点「刷新」才看到新资产）。
  //    现在三重保障：
  //      ① 可见时每 12s 静默重拉（页面隐藏时跳过，不浪费请求）；
  //      ② 窗口重新获得焦点 / 切回本标签页 **立刻**重拉一次（用户切走又回来无需等）；
  //      ③ 图片 URL 带 ?v=<mtime>（见 assetSrc）—— 这是关键：轮询虽会换回新 JSON，
  //         但旧代码 <img src> 恒定、浏览器命中旧缓存，用户看到的还是旧图。
  //    失败静默（reloadAssets 内部已 catch），不弹提示 —— 这是后台轮询。
  useEffect(() => {
    if (!projectKey) return;
    let alive = true;
    const tick = () => {
      if (!alive) return;
      if (typeof document !== 'undefined' && document.hidden) return; // 隐藏页不轮询
      void reloadAssets();
    };
    const timer = setInterval(tick, 12000);
    const onFocus = () => tick();
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [projectKey, reloadAssets]);

  // Load project data
  useEffect(() => {
    if (!projectKey) return;
    setLoading(true);
    Promise.all([
      projectsApi.get(projectKey).then(d => setProject(d as Project)).catch(() => null),
      reloadAssets(),
    ]).finally(() => setLoading(false));
  }, [projectKey, reloadAssets]);

  const tabs: { id: WorkbenchTab; icon: React.ReactNode; label: string }[] = [
    { id: 'overview', icon: <BarChart3 className="h-4 w-4" />, label: t('wb.overview') },
    // 自动生产已移除独立标签页 —— 改为 AI总控 内的子功能，启动前AI会先与用户沟通风格
    // 分镜管理：合并 九宫格构图 + 关键帧生成 + 分镜序列 三个子标签（见 StoryboardHubTab）
    { id: 'storyboard', icon: <Clapperboard className="h-4 w-4" />, label: t('wb.storyboardHub') },
    { id: 'qc', icon: <CheckCircle2 className="h-4 w-4" />, label: t('wb.qc') },
    // 超分：后端 upscale_client 与其 8 个端点早已可用，但前端此前零引用 ——
    // 与已删除的孤儿页面同属「建好没入口」的能力，这里补上手工入口。
    { id: 'upscale', icon: <ZoomIn className="h-4 w-4" />, label: t('wb.upscale') },
    // 参考音色：H3 成片自带角色配音，本页管理各角色的参考音色库 + 音频质检
    { id: 'audio', icon: <Music className="h-4 w-4" />, label: t('wb.audio') },
    // 输出与验收：合并导出 + 成品验收
    { id: 'output', icon: <Share2 className="h-4 w-4" />, label: t('wb.output') },
    // AI总控 不再是标签页 —— 已改为右侧常驻面板（默认展开，见下方 ChatPanel）
  ];

  if (loading) return (
    // 骨架沿用真实内容的外层布局（左列 + 右侧常驻面板），避免「白屏 → 内容」的高度跳变
    <div className="fade-in" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex-1 min-w-0 space-y-4">
          <div className="flex items-center justify-between">
            <div className="min-w-0 space-y-2">
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-4 w-64" />
            </div>
            <Skeleton className="h-9 w-32" />
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-20 rounded-lg" />
            ))}
          </div>
          <div className="flex flex-wrap gap-2 border-b border-line pb-4">
            {[0, 1, 2, 3, 4, 5, 6].map((i) => (
              <Skeleton key={i} className="h-9 w-24 rounded-lg" />
            ))}
          </div>
          <Skeleton className="h-64 rounded-lg" />
        </div>
        <Skeleton className="min-h-[420px] w-full rounded-xl lg:h-[calc(100vh-7rem)] lg:w-[340px] lg:shrink-0" />
      </div>
    </div>
  );
  if (!project) return (
    <EmptyState
      icon={<AlertTriangle className="h-10 w-10" />}
      title={t('wb.projectNotFound')}
      description={projectKey}
      action={(
        <Button size="sm" variant="secondary" onClick={() => { window.location.hash = '#projects'; }}>
          {t('wb.backToProjects')}
        </Button>
      )}
    />
  );

  return (
    <div className="fade-in">
      {/* 主体：左列（项目头 + 统计 + 标签内容） + 右列「AI总控」常驻面板。
          头部与统计放进左列，右侧面板才能从顶部一直贯通到底部，不会变成悬空小盒。
          ⚠️ < lg 时改为上下堆叠：面板固定 340px，375 视口扣掉侧边栏后只剩 311px，
          横排必然把页面撑出横向滚动条。 */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex-1 min-w-0 space-y-4">
          {/* Header */}
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-gradient text-2xl font-bold">{project.name}</h2>
              <p className="text-sm text-ink-2 mt-1">
                {t('project.style')}: {project.config?.style} • {project.episode_count} {t('ep.suffix')}
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => { window.location.hash = '#/projects'; }}
            >
              ← {t('wb.backToProjects')}
            </Button>
          </div>

          {/* Stats Bar —— 窄屏折成两行，避免 4 列挤压成一竖条（方案 P1-8） */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { label: t('wb.characters'), count: assets?.counts?.characters || 0, color: 'text-brand' },
              { label: t('wb.items'), count: assets?.counts?.items || 0, color: 'text-success-strong' },
              { label: t('wb.scenes'), count: assets?.counts?.scenes || 0, color: 'text-warning-strong' },
              { label: t('wb.storyboard'), count: assets?.counts?.storyboards || 0, color: 'text-info-strong' },
            ].map((stat) => (
              <div key={stat.label} className="bg-surface rounded-lg p-4 border border-line transition-all hover:-translate-y-0.5 hover:shadow-md hover:border-line-strong">
                <div className={`text-2xl font-bold tabular-nums ${stat.color}`}>{stat.count}</div>
                <div className="text-sm text-ink-2">{stat.label}</div>
              </div>
            ))}
          </div>

          {/* Tab Navigation */}
          <div className="flex flex-wrap gap-2 border-b border-line pb-4">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${FOCUS_RING} ${
                  activeTab === tab.id
                    ? 'bg-brand-subtle text-brand shadow-xs'
                    : 'text-ink-2 hover:bg-surface-2 hover:text-ink-1'
                }`}
              >
                <span className="flex shrink-0">{tab.icon}</span>
                <span>{tab.label}</span>
              </button>
            ))}
          </div>

          {/* Tab Content */}
          <div className="min-h-[400px]">
            {activeTab === 'overview' && (
              <OverviewTab
                assets={assets}
                projectKey={projectKey}
                novelId={project.novel_id}
                onRefreshAssets={reloadAssets}
              />
            )}
            {activeTab === 'storyboard' && (
              <StoryboardHubTab projectKey={projectKey} novelId={project.novel_id}
                                onGotoOutput={() => setActiveTab('output')}
                                focusShot={shotFocus?.shotId ?? null}
                                focusEpisodeNo={shotFocus?.episodeNo ?? null}
                                onFocusConsumed={() => setShotFocus(null)} />
            )}
            {activeTab === 'qc' && (
              <>
                <QcTab projectKey={projectKey} />

                {/* 质量：原文覆盖率 / 字幕校对 / 跨集一致性 / 项目连贯性 */}
                <QualityPanel project={projectKey} />

                {/* 失败原因看板（E-2）：死信按结构化失败码归类展示 */}
                <FailurePanel project={projectKey} />

                {/* 诊断与补救建议（POST /api/qc/triage）—— 只给建议，点了才执行 */}
                <TriagePanel project={projectKey} />
              </>
            )}
            {activeTab === 'audio' && (
              <>
                {/* 音频体检（t9）：扫描分类计数 + 无声镜号（可定位分镜）+ 频谱/波形 + 门禁处置 */}
                <AudioHealthPanel
                  project={projectKey}
                  novelId={project.novel_id}
                  onLocateShot={gotoShotInStoryboard}
                />

                <AudioTab projectKey={projectKey} />
              </>
            )}
            {activeTab === 'output' && (
              <>
                <OutputReviewTab projectKey={projectKey} assets={assets} />

                {/* 导出与交付：剪映草稿 / FCPXML / SRT / 成片 / 交付物 */}
                <ExportPanel project={projectKey} />
              </>
            )}
            {activeTab === 'upscale' && (
              <UpscaleTab projectKey={projectKey} />
            )}
          </div>
        </div>

        {/* AI总控：右侧常驻面板（默认展开，可收起为竖条） */}
        {chatOpen ? (
          <ChatPanel projectKey={projectKey} onClose={() => setChatOpen(false)} />
        ) : (
          <button
            onClick={() => setChatOpen(true)}
            title={t('wb.expandChat')}
            className={`sticky top-0 shrink-0 w-11 h-[calc(100vh-7rem)] min-h-[420px] flex flex-col items-center gap-3 py-4 rounded-xl border border-line bg-surface text-ink-2 hover:text-brand hover:border-brand transition-colors ${FOCUS_RING}`}
          >
            <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center"><MessageSquare className="h-4 w-4" /></span>
            <span className="text-xs tracking-wide" style={{ writingMode: 'vertical-rl' }}>{t('wb.aiControl')}</span>
          </button>
        )}
      </div>
    </div>
  );
}
